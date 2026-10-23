"""SDK sessions and runs: Generate through comfy-sdk → comfy-api-proxy → this ComfyUI.

An SDK *session* is one editor tab opened from the Launcher on one workflow
(template) and one project. Its host id is claimable in the session registry
only with its secret. Generate builds the workflow from the stored template, the
tab's editor values and the template's Control values, then submits it N times
through the SDK, advancing seeds after each accepted submit as ComfyUI's batch
count does. It never picks a job: the editor chooses live, peek or consume when
it executes, exactly as for a canvas Run.

**Outcome authority.** An SDK or proxy status is never trusted alone (the proxy
can report a spurious ``expired`` at completion). A run is final when:

- ``run_finished`` arrives (``sdk_runs`` listener, from ComfyUI's prompt-worker
  thread, marshalled onto the loop) — this overrides anything inferred; or
- the prompt is absent from ComfyUI's queue *and* history *and* no release record
  ever opened for it, for longer than a grace period: it was deleted or cleared
  before it ran.

A prompt whose release record is open or closed without ``run_finished`` yet is
*finishing* (bridges are finalizing), even if ComfyUI's history was wiped.

ComfyUI's in-process send_sync observer supplies node progress for registered
SDK prompts; the proxy remains the submit/cancel transport.

**A submit whose outcome is unknown** (the proxy's 503 ``upstream_unreachable``,
a timeout, a dropped connection) may still have reached ComfyUI's queue. Such a
run keeps its token and waits *unconfirmed*: presence looks for its token in
ComfyUI's queue and adopts the prompt it finds; ``run_finished`` matches it by
token; only when neither appears does it fail. A definite rejection (a 4xx from
the proxy, such as an invalid workflow) forgets the token at once.
"""

from __future__ import annotations

import asyncio
import copy
import hmac
import logging
import os
import secrets
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from . import control_descriptors, controls_snapshot, launcher_state, prompt_graph, sdk_proxy, sdk_progress, sdk_runs, sdk_templates, session_registry
from .editor_inputs import clean_editor_value, clean_editor_values, editor_optional_inputs
from .sdk_install import install_state, uninstall_running

logger = logging.getLogger("sonder_editor")

async def await_template_io(func, *args, **kwargs):
    """Finish a disk mutation before its caller releases the template lock.

    to_thread cannot stop a running worker. Shield it and drain even repeated
    cancellation, then propagate cancellation after the file operation ends.
    """
    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    canceled = False
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            canceled = True
            continue
        except Exception:
            if canceled:
                raise asyncio.CancelledError from None
            raise
        if canceled:
            raise asyncio.CancelledError
        return result


MAX_SESSIONS = 8
MAX_COUNT = 8
MAX_UNFINISHED_PER_SESSION = 16
FINISHED_RUNS_KEPT = 24
ABSENT_GRACE_SEC = 5.0
UNCONFIRMED_GRACE_SEC = 60.0
UNCONFIRMED_SUBMIT_GRACE_SEC = 10.0
CLOSED_WITHOUT_FINISH_SEC = 60.0
PRESENCE_INTERVAL_SEC = 1.0
STATUS_MIN_INTERVAL_SEC = 0.2
PROXY_IDLE_STOP_SEC = 30 * 60
SESSION_IDLE_SEC = 30 * 60
# A session another tab opened this recently may still be loading its page:
# a switch never takes its (workflow, project) pair away from it.
SWITCH_PAIR_GRACE_SEC = 120.0
HOUSEKEEPING_INTERVAL_SEC = 60.0
SEED_MAX = 2 ** 64 - 1

SUBMITTING, UNCONFIRMED = "submitting", "unconfirmed"
QUEUED, RUNNING, FINISHING = "queued", "running", "finishing"
LANDED, FAILED, CANCELED, ENDED = "landed", "failed", "canceled", "ended"
FINAL_STATES = frozenset({LANDED, FAILED, CANCELED, ENDED})


class SdkError(Exception):
    def __init__(self, message: str, code: str, status: int = 400, **details: Any):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details

    def payload(self) -> dict:
        return {"ok": False, "code": self.code, "error": str(self), **self.details}


@dataclass
class SdkRun:
    run_id: str
    token: str
    session_id: str
    index: int
    count: int
    state: str = SUBMITTING
    prompt_id: str = ""
    job: Any = None
    position: int | None = None
    progress_node: str = ""
    progress_count: dict | None = None
    node_labels: dict = field(default_factory=dict)
    detail: str = ""
    cancel_requested: bool = False
    job_id: str = ""
    outcome: str = ""
    note: str = ""
    seeds: dict = field(default_factory=dict)
    submitted_at: float = 0.0
    accepted_at: float = 0.0
    absent_since: float = 0.0
    history_unconfirmed_since: float = 0.0
    closed_since: float = 0.0
    unconfirmed_since: float = 0.0
    finished_at: float = 0.0
    client: Any = None
    # The workflow this run was submitted with: a session can switch workflows
    # while earlier runs are still generating.
    template_id: str = ""
    template_name: str = ""

    @property
    def final(self) -> bool:
        return self.state in FINAL_STATES

    def public(self) -> dict:
        return {
            "run_id": self.run_id,
            "index": self.index,
            "count": self.count,
            "state": self.state,
            "canceling": self.cancel_requested and not self.final,
            "position": self.position,
            "progress_node": self.progress_node,
            **({"progress_count": dict(self.progress_count)} if self.progress_count else {}),
            "prompt_id": self.prompt_id,
            "job_id": self.job_id,
            "outcome": self.outcome,
            "note": self.note,
            "template_id": self.template_id,
            "template_name": self.template_name,
        }


@dataclass
class SdkSession:
    session_id: str
    secret: str
    template_id: str
    project: str
    project_dir: str
    aliases: list
    created: float
    last_activity: float
    runs: list = field(default_factory=list)
    seq: int = 0
    last_emit: float = 0.0
    emit_scheduled: bool = False
    cancel_epoch: int = 0
    # Absorb bookkeeping. Stamps come from one runner counter, so "claimed since
    # its last Open" never depends on clock resolution; ``opened_at`` dates the
    # last Open for the grace a loading page gets.
    opened_at: float = 0.0
    opened_stamp: int = 0
    claimed_stamp: int = 0
    generating: int = 0  # Generates of this session between arrival and their last submit

    def unfinished(self) -> list:
        return [run for run in self.runs if not run.final]

    def public(self, template_name: str = "") -> dict:
        return {
            "session_id": self.session_id,
            "template_id": self.template_id,
            "template_name": template_name,
            "project": self.project,
            "seq": self.seq,
            "runs": [run.public() for run in self.runs],
        }


# ── The SDK client (built without touching os.environ) ───────────────────────

def _make_client(url: str, token: str):
    """An ``AsyncComfy`` aimed at ``url``.

    comfy-sdk 0.4.0's ``AsyncComfy`` reads its base URL from ``COMFY_BASE_URL``
    only; mutating the process environment is unsafe in a multithreaded server,
    so this builds the same object around an explicit ``AsyncComfyLow``. The
    httpx client ignores system proxy settings (``trust_env=False``), which
    could otherwise route loopback traffic through a corporate proxy.
    Expiry: remove when comfy-sdk accepts a base URL (pinned 0.4.x).
    """
    import httpx
    from comfy_low.transport import AsyncComfyLow
    from comfy_sdk.assets import AsyncAssetFactory
    from comfy_sdk.client import AsyncComfy
    from comfy_sdk.jobs import AsyncJobFactory
    from comfy_sdk.models import AsyncModels
    from comfy_sdk.retry import DEFAULT_RETRY
    from comfy_sdk.workflows import WorkflowFactory

    class SonderComfyClient(AsyncComfy):
        def __init__(self) -> None:  # noqa: D401 - deliberately skips AsyncComfy.__init__
            self._http = httpx.AsyncClient(timeout=30.0, follow_redirects=True, trust_env=False)
            self._low = AsyncComfyLow(url, token or None, client=self._http, timeout=30.0)
            self.assets = AsyncAssetFactory(self._low)
            self.workflows = WorkflowFactory()
            self.jobs = AsyncJobFactory(self._low)
            self.models = AsyncModels(self._low, DEFAULT_RETRY)

        async def aclose(self) -> None:
            await self._low.aclose()
            await self._http.aclose()

    return SonderComfyClient()


def _default_queue():
    from server import PromptServer  # ComfyUI's server module

    return PromptServer.instance.prompt_queue


def _item_token(item, tokens: set) -> str:
    prompt = item[2] if len(item) > 2 and isinstance(item[2], dict) else {}
    for node in prompt.values():
        meta = node.get("_meta") if isinstance(node, dict) else None
        token = meta.get("sonder_run") if isinstance(meta, dict) else None
        if token in tokens:
            return token
    return ""


def presence_snapshot(queue, prompt_ids: list[str], tokens: list[str] = ()) -> dict:
    """Where each prompt is: ``running``, ``queued`` (with position) or ``history``.

    One mutex hold for the queue; history is checked per id with a map function
    so ComfyUI does not deep-copy each entry. Absent prompts are left out.
    ``tokens`` (runs whose submit outcome is unknown) are looked up in the queue
    items' prompts and reported under ``"tokens"`` as ``{token: prompt_id}``.
    """
    wanted = set(prompt_ids)
    wanted_tokens = set(tokens)
    found: dict = {}
    by_token: dict[str, str] = {}
    if not wanted and not wanted_tokens:
        return found
    running, queued = queue.get_current_queue_volatile()
    ordered = sorted(queued, key=lambda entry: entry[0])
    for item in running:
        if len(item) > 1 and wanted_tokens and (token := _item_token(item, wanted_tokens)):
            by_token[token] = item[1]
            wanted.add(item[1])
        if len(item) > 1 and item[1] in wanted:
            found[item[1]] = ("running", None)
    for position, item in enumerate(ordered, start=1):
        if len(item) > 1 and wanted_tokens and (token := _item_token(item, wanted_tokens)):
            by_token[token] = item[1]
            wanted.add(item[1])
        if len(item) > 1 and item[1] in wanted and item[1] not in found:
            found[item[1]] = ("queued", position)
    for prompt_id in wanted - set(found):
        if queue.get_history(prompt_id=prompt_id, map_function=lambda _entry: True):
            found[prompt_id] = ("history", None)
    if wanted_tokens:
        found["tokens"] = by_token
    return found


def _definitely_rejected(exc: Exception) -> bool:
    """Whether a failed submit certainly never reached ComfyUI's queue.

    A 4xx answer (invalid workflow, queue full after retries, bad request) is a
    refusal; a 5xx, a timeout or a transport error may follow an enqueue.
    """
    status = getattr(exc, "http_status", None)
    return isinstance(status, int) and 400 <= status < 500


class SdkRunner:
    def __init__(
        self,
        *,
        resolve_project: Callable[[str], Any] | None = None,
        client_factory: Callable[[str, str], Any] = _make_client,
        ensure_proxy: Callable[[], Any] = sdk_proxy.ensure_proxy,
        queue_provider: Callable[[], Any] = _default_queue,
        emit: Callable[[str, dict], None] = session_registry.schedule_project_event,
        get_owner: Callable[..., Any] = session_registry.get_owner,
        set_owner_label: Callable[..., Any] = session_registry.set_owner_workflow_label,
        sdk_ready: Callable[[], bool] = lambda: bool(install_state()["sdk"]["supported"]),
        uninstalling: Callable[[], bool] = uninstall_running,
        describe: Callable[..., dict] | None = None,
        clock: Callable[[], float] = time.time,
        class_lookup: Callable[[str], Any] | None = None,
        # Which node classes are installed, for the missing-node refusals of Open,
        # switch and Generate; ComfyUI's registry by default.
        node_lookup: Callable[[str], Any] | None = None,
    ) -> None:
        self._resolve_project = resolve_project
        self._client_factory = client_factory
        self._ensure_proxy = ensure_proxy
        self._queue_provider = queue_provider
        self._emit_event = emit
        self._get_owner = get_owner
        self._set_owner_label = set_owner_label
        self._sdk_ready = sdk_ready
        self._uninstalling = uninstalling
        self._generating = 0  # Generates between their gate and their submit
        self._describe = describe or control_descriptors.describe_bindings
        self._class_lookup = class_lookup
        self._node_lookup = node_lookup
        self._clock = clock
        self._sessions: dict[str, SdkSession] = {}
        self._runs_by_token: dict[str, SdkRun] = {}
        self._clients: dict[tuple[str, str], Any] = {}
        self._template_locks: dict[str, asyncio.Lock] = {}
        self._template_names: dict[str, str] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._presence_task: asyncio.Task | None = None
        self._housekeeping_task: asyncio.Task | None = None
        self._open_lock = asyncio.Lock()
        self._idle_since = self._clock()
        self._stamp = 0
        self._remove_listener = sdk_runs.add_run_finished_listener(self._on_run_finished_threadsafe)
        self._remove_progress_listener = sdk_progress.add_listener(self._on_progress_threadsafe)
        session_registry.set_sdk_secret_resolver(self.secret_for_host)
        session_registry.set_sdk_claim_listener(self.note_claim)

    def close(self) -> None:
        self._remove_listener()
        self._remove_progress_listener()
        for run in self._runs_by_token.values():
            sdk_progress.unwatch(run.prompt_id, run.token)
        session_registry.set_sdk_secret_resolver(None)
        session_registry.set_sdk_claim_listener(None)

    # ── Registry hooks ───────────────────────────────────────────────────────

    def secret_for_host(self, host_id: str) -> str | None:
        session = self._sessions.get(str(host_id or ""))
        return session.secret if session else None

    def _next_stamp(self) -> int:
        self._stamp += 1
        return self._stamp

    def note_claim(self, host_id: str) -> None:
        """A tab claimed this session (registry lock held): it is no longer just opened."""
        session = self._sessions.get(str(host_id or ""))
        if session is not None:
            session.claimed_stamp = self._next_stamp()

    # ── Sessions ─────────────────────────────────────────────────────────────

    def _remember_loop(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._housekeeping_task is None or self._housekeeping_task.done():
            self._housekeeping_task = asyncio.ensure_future(self._housekeeping_loop())

    async def _housekeeping_loop(self) -> None:
        while True:
            await asyncio.sleep(HOUSEKEEPING_INTERVAL_SEC)
            try:
                await self.housekeeping()
            except Exception:
                logger.exception("SDK housekeeping failed")

    async def housekeeping(self) -> None:
        """Drop sessions idle for SESSION_IDLE_SEC (no run in flight, no tab
        holding them), and stop the proxy Sonder started once no session has
        existed for PROXY_IDLE_STOP_SEC."""
        now = self._clock()
        for session in list(self._sessions.values()):
            if not self._idle(session):
                continue
            owner = await self._get_owner(session.project, host_id=session.session_id)
            if owner and owner.get("status") in ("active", "orphaned"):
                continue  # a tab still holds it, perhaps throttled in the background
            # Rechecked after the owner read: an Open may have reused it, absorbed
            # takes into it or started a Generate meanwhile.
            if self._sessions.get(session.session_id) is session and self._idle(session):
                self._drop_session(session)
        if self._sessions:
            self._idle_since = 0.0
        elif self._idle_since and now - self._idle_since > PROXY_IDLE_STOP_SEC:
            self._idle_since = 0.0
            await asyncio.to_thread(sdk_proxy.stop_owned_proxy)
            await self.aclose_clients()

    def _idle(self, session: SdkSession) -> bool:
        return (not session.unfinished() and not session.generating
                and self._clock() - session.last_activity >= SESSION_IDLE_SEC)

    async def open_session(self, template_id: str, project: str) -> dict:
        self._remember_loop()
        # Serialized: eviction awaits, and two opens must not both pass the cap
        # or both create a session for one workflow and project.
        async with self._open_lock:
            return await self._open_session_locked(template_id, project)

    async def _open_session_locked(self, template_id: str, project: str) -> dict:
        template_id = sdk_templates.check_template_id(template_id)
        meta = await self._runnable_meta(template_id)
        if self._resolve_project is None:
            raise SdkError("Projects are unavailable.", "projects_unavailable", 500)
        # An async resolver: it loads the project through the project I/O pool.
        project_dir, aliases = await self._resolve_project(project)
        if not project_dir:
            raise SdkError("Unknown project.", "unknown_project", 404)
        # The folder's own spelling: the editor's project choice must match it exactly.
        project = os.path.basename(os.path.normpath(project_dir))
        self._template_names[template_id] = meta.get("name", "")
        reused = next((s for s in self._sessions.values()
                       if s.template_id == template_id and s.project == project), None)
        if reused is not None:
            # Before any await: an idle reused session must not look abandoned
            # to housekeeping while the owners below are read.
            reused.last_activity = self._clock()
        candidates = await self._absorb_candidates(project, exclude=reused)
        if reused is not None and self._sessions.get(reused.session_id) is not reused:
            reused = None  # dropped meanwhile: open a new one, never a dead one
        now = self._clock()
        if reused is not None:
            session = reused
            session.last_activity = now
            session.project_dir, session.aliases = project_dir, list(aliases)
        else:
            session = SdkSession(
                session_id=f"sdk-{uuid.uuid4().hex}",
                secret=secrets.token_urlsafe(32),
                template_id=template_id,
                project=project,
                project_dir=project_dir,
                aliases=list(aliases),
                created=now,
                last_activity=now,
            )
        session.opened_at, session.opened_stamp = now, self._next_stamp()
        # Before the cap check: an absorbed sibling is dropped, freeing its slot.
        absorbed = self._absorb(session, candidates)
        if reused is None:
            # Absorbing dropped at least one session, so the cap cannot be reached
            # and the moved runs never wait, outside any session, on an eviction.
            if len(self._sessions) >= MAX_SESSIONS:
                await self._evict_one()
            self._sessions[session.session_id] = session
            self._idle_since = 0.0
            asyncio.ensure_future(self._warm_proxy())
        if absorbed:
            self._emit(session, force=True)
        await self._record_open(session)
        return self._open_result(session)

    # ── Absorbing a sibling session's unfinished takes ───────────────────────

    def _absorbable(self, sibling: SdkSession, claimed_stamp: int) -> bool:
        """Checked with no await before the move. The owner read (done earlier)
        found no tab; a claim since that read, a Generate still sending into
        the sibling, or an Open whose page may still be loading keeps it."""
        if self._sessions.get(sibling.session_id) is not sibling or not sibling.unfinished():
            return False
        if sibling.claimed_stamp != claimed_stamp or sibling.generating:
            return False
        return (sibling.claimed_stamp > sibling.opened_stamp
                or self._clock() - sibling.opened_at > SWITCH_PAIR_GRACE_SEC)

    async def _absorb_candidates(self, project: str, exclude: SdkSession | None = None) -> list:
        """Sibling sessions on this project with unfinished takes and no live tab,
        each with the claim stamp seen before its owner read."""
        found = []
        for sibling in list(self._sessions.values()):
            if sibling is exclude or sibling.project != project or not sibling.unfinished():
                continue
            claimed_stamp = sibling.claimed_stamp
            owner = await self._get_owner(sibling.project, host_id=sibling.session_id)
            if owner and owner.get("status") in ("active", "orphaned"):
                continue
            found.append((sibling, claimed_stamp))
        return found

    def _absorb(self, session: SdkSession, candidates: list) -> int:
        """Move each absorbable sibling's unfinished runs into ``session`` and drop
        the sibling. Synchronous: nothing can claim, submit into or open the
        sibling between the checks and the move. Finished runs stay behind (their
        outcomes were already announced). The unfinished cap does not apply;
        Generate stays refused until the count is under it."""
        moved = 0
        for sibling, claimed_stamp in candidates:
            if sibling is session or not self._absorbable(sibling, claimed_stamp):
                continue
            moved += self._move_unfinished(session, sibling)
        return moved

    def _move_unfinished(self, session: SdkSession, sibling: SdkSession) -> int:
        runs = sibling.unfinished()
        for run in runs:
            sibling.runs.remove(run)
            run.session_id = session.session_id
            session.runs.append(run)
        # Only the sibling's finished runs remain for the drop to unwatch.
        self._drop_session(sibling)
        logger.info("Sonder App session %s took %d unfinished takes from %s", session.session_id, len(runs),
                    sibling.session_id)
        return len(runs)

    async def _record_open(self, session: SdkSession) -> None:
        """The Launcher preselects this workflow for this project next time. Only an open
        that succeeded is recorded, and a failed note never fails the open."""
        try:
            await asyncio.to_thread(launcher_state.record_open, session.project, session.template_id)
        except Exception:
            logger.warning("Recording the Launcher's last workflow failed", exc_info=True)

    def _open_result(self, session: SdkSession) -> dict:
        return {
            "ok": True,
            "session_id": session.session_id,
            "secret": session.secret,
            "project": session.project,
            "template_id": session.template_id,
            "tab_url": (
                f"/sonder-editor/tab/{urllib.parse.quote(session.project, safe='')}"
                f"?mode=sdk&session={session.session_id}"
            ),
        }

    async def switch_workflow(self, session_id: str, secret: str, tab_session_id: str, template_id: Any,
                              expected_template_id: Any) -> dict:
        """Point this session at another imported workflow, keeping its project and runs.

        Takes the old workflow's lock first, so a Generate or Controls write
        already queued on it finishes first and anything queued after is refused
        as ``workflow_changed``; then ``_open_lock``, held only for the check and
        the change, so no open creates the same pair meanwhile and a long Generate
        never stalls other Opens. Only this path takes both locks, always in this
        order. Earlier runs keep their own workflow. Queued Sonder jobs record
        none: each renders with the workflow whose Generate takes it.
        """
        self._remember_loop()
        new_id = sdk_templates.check_template_id(template_id)
        session = await self.authorized(session_id, secret, tab_session_id)
        old_id = session.template_id
        self._check_workflow(session, old_id, expected_template_id)
        async with self.template_lock(old_id), self._open_lock:
            await self.authorized(session_id, secret, tab_session_id)
            self._check_workflow(session, old_id, expected_template_id)
            if new_id != old_id:
                # Refuse before taking anything over from the pair's session.
                meta = await self._runnable_meta(new_id)
                await self._release_pair(session, new_id)
                session.template_id = new_id
                self._template_names[new_id] = meta.get("name", "")
                await self._record_open(session)
                label = self.template_name(session)
                try:
                    await self._set_owner_label(session.project, session.session_id, tab_session_id,
                                                f"Sonder App · {label}" if label else "Sonder App")
                except Exception:
                    logger.warning("Updating the Sonder App tab's workflow label failed", exc_info=True)
                self._emit(session, force=True)
            return {"ok": True, "template_id": session.template_id, "template_name": self.template_name(session)}

    async def _runnable_meta(self, template_id: str) -> dict:
        """An Open or a switch refuses a workflow with nodes missing now (the Launcher
        lists them) or one that can't drive the editor."""
        try:
            _, meta = await asyncio.to_thread(sdk_templates.runnable_template, template_id, self._node_lookup)
        except sdk_templates.TemplateError as exc:
            if exc.code == "missing_nodes":
                raise SdkError(str(exc), exc.code, exc.status, **exc.details) from None
            if exc.code == "invalid_template":
                raise SdkError(str(exc), exc.code, exc.status) from None
            raise
        return meta

    async def _release_pair(self, session: SdkSession, template_id: str) -> None:
        """Keep one session per (workflow, project): refuse while a tab holds the
        pair; take over the takes still generating in an abandoned one; drop an
        abandoned one, as housekeeping would."""
        for other in list(self._sessions.values()):
            if other is session or other.template_id != template_id or other.project != session.project:
                continue
            claimed_stamp = other.claimed_stamp
            owner = await self._get_owner(other.project, host_id=other.session_id)
            live = owner and owner.get("status") in ("active", "orphaned")
            if live or other.claimed_stamp != claimed_stamp:
                raise SdkError("That workflow is already open on this project in another tab.",
                               "workflow_open_elsewhere", 409)
            if self._sessions.get(other.session_id) is not other:
                continue  # dropped while the owner was read
            just_opened = SdkError("That workflow was just opened on this project in another tab. "
                                   "Try again in a moment.", "workflow_open_elsewhere", 409)
            if other.generating:
                # Its Generate may still add runs: neither take them nor drop it.
                raise SdkError("That workflow is still starting takes. Try again in a moment.",
                               "workflow_open_elsewhere", 409)
            if other.unfinished():
                if not self._absorbable(other, claimed_stamp):
                    raise just_opened
                self._move_unfinished(session, other)
                continue
            if self._clock() - other.last_activity < SWITCH_PAIR_GRACE_SEC:
                raise just_opened
            self._drop_session(other)

    async def _warm_proxy(self) -> None:
        if self._uninstalling():
            return  # the packages are going; Generate reports it
        try:
            await self._ensure_proxy()
        except Exception as exc:  # reported again, with its reason, by Generate
            logger.info("SDK proxy not ready at session open: %s", exc)

    async def _evict_one(self) -> None:
        candidates = []
        for session in list(self._sessions.values()):
            if session.unfinished() or session.generating:
                continue
            owner = await self._get_owner(session.project, host_id=session.session_id)
            if owner and owner.get("status") in ("active", "orphaned"):
                continue
            candidates.append(session)
        # Rechecked after the owner reads, with no await before the drop.
        candidates = [s for s in candidates if self._sessions.get(s.session_id) is s
                      and not s.unfinished() and not s.generating]
        if not candidates:
            raise SdkError("Too many Sonder App sessions are busy. Close one and try again.", "sdk_sessions_busy", 429)
        oldest = min(candidates, key=lambda s: s.last_activity)
        self._drop_session(oldest)

    def _drop_session(self, session: SdkSession) -> None:
        self._sessions.pop(session.session_id, None)
        for run in session.runs:
            sdk_progress.unwatch(run.prompt_id, run.token)
            self._runs_by_token.pop(run.token, None)
        if not self._sessions:
            self._idle_since = self._clock()

    def session_for(self, session_id: str, secret: str) -> SdkSession:
        session = self._sessions.get(str(session_id or ""))
        if session is None or not hmac.compare_digest(str(secret or ""), session.secret):
            raise SdkError("This Sonder App session has ended.", "session_ended", 404)
        session.last_activity = self._clock()
        return session

    async def authorized(self, session_id: str, secret: str, tab_session_id: str) -> SdkSession:
        """The session, when the caller holds its secret and is its current owner tab."""
        self._remember_loop()
        session = self.session_for(session_id, secret)
        owner = await self._get_owner(session.project, host_id=session.session_id)
        if not owner or owner.get("session_id") != str(tab_session_id or ""):
            raise SdkError("This session is open in another tab.", "not_owner", 409)
        return session

    def template_name(self, session: SdkSession) -> str:
        return self._template_names.get(session.template_id, "")

    def status(self, session: SdkSession) -> dict:
        return {"ok": True, **session.public(self.template_name(session))}

    def _controls_snapshot(self, session: SdkSession, manage: bool = False, query: str = "",
                           template_id: str | None = None) -> dict:
        template_id = template_id or session.template_id
        with sdk_templates.template_lock(template_id):
            prompt, _ = sdk_templates.load_template(template_id)
            stored = sdk_templates.read_control_values(template_id)
            view = sdk_templates.apply_stored_values(
                prompt, sdk_templates.describe_stored(prompt, stored, self._describe), stored, describe=self._describe)
            # ``prompt`` now carries the stored values: changes are validated
            # against it, so a new choice list and a choice in it agree.
            snapshot = {"view": view, "seq": stored["seq"], "prompt": prompt, "template_id": template_id}
            if manage:
                # Amendment A1: root-level literal inputs only, never greyed out.
                listed, truncated = control_descriptors.manage_candidates(
                    self._inventory(prompt), view, query=query)
                view.update(candidates=listed, candidates_truncated=truncated)
            return snapshot

    def _inventory(self, prompt: dict) -> list:
        return control_descriptors.candidate_inventory(
            prompt, control_descriptors.sdk_candidates(prompt, self._class_lookup), class_lookup=self._class_lookup)

    async def controls_view(self, session: SdkSession, *, manage: bool = False, query: str = "") -> dict:
        template_id = session.template_id
        async with self.template_lock(template_id):
            # The view names its workflow, so a page that has switched since
            # can drop it.
            return await asyncio.to_thread(self._controls_snapshot, session, manage, query, template_id)

    @staticmethod
    def _check_workflow(session: SdkSession, template_id: str, expected: Any = None) -> None:
        """One workflow per request: the id read when the request arrived, which the
        page must have meant and the session must still have after any wait."""
        if (expected is not None and str(expected) != template_id) or session.template_id != template_id:
            raise SdkError("The workflow changed. Try again.", "workflow_changed", 409,
                           template_id=session.template_id)

    async def change_controls(self, session_id: str, secret: str, tab_session_id: str, changes: Any, *,
                              manage: bool = False, query: str = "", expected_template_id: Any = None) -> dict:
        session = await self.authorized(session_id, secret, tab_session_id)
        template_id = session.template_id
        self._check_workflow(session, template_id, expected_template_id)
        async with self.template_lock(template_id):
            # Ownership and the workflow may change while waiting behind a
            # Generate batch or a switch.
            await self.authorized(session_id, secret, tab_session_id)
            self._check_workflow(session, template_id)
            snapshot = await asyncio.to_thread(self._controls_snapshot, session, False, "", template_id)
            # An expose or rebind needs the template's candidate inventory to validate.
            structural = isinstance(changes, dict) and any(
                (parsed := control_descriptors.parse_target(t)) and parsed[0] != "group"
                and parsed[2] in ("expose", "rebind") for t in changes)
            inventory = await asyncio.to_thread(self._inventory, snapshot["prompt"]) if structural else None
            cleaned, rejected = await asyncio.to_thread(
                control_descriptors.validate_binding_changes, snapshot["view"], changes, prompt=snapshot["prompt"],
                structural="sdk", inventory=inventory)
            if rejected:
                raise SdkError("Some changes could not be saved.", "invalid_change", 400, rejected=rejected)
            await self.authorized(session_id, secret, tab_session_id)
            await await_template_io(sdk_templates.store_binding_changes, template_id, cleaned,
                                    snapshot["view"], snapshot["prompt"])
            return await asyncio.to_thread(self._controls_snapshot, session, manage, query, template_id)

    # ── Generate ─────────────────────────────────────────────────────────────

    def template_lock(self, template_id: str) -> asyncio.Lock:
        """Held by Generate for a whole batch, and by template replace and delete."""
        lock = self._template_locks.get(template_id)
        if lock is None:
            lock = self._template_locks[template_id] = asyncio.Lock()
        return lock

    async def _client(self, target) -> Any:
        key = (target.url, target.token)
        client = self._clients.get(key)
        if client is None:
            # Off the loop: the first one imports comfy-sdk.
            client = await asyncio.to_thread(self._client_factory, target.url, target.token)
            client = self._clients.setdefault(key, client)
        return client

    async def generate(
        self,
        session: SdkSession,
        *,
        tab_session_id: str,
        count: Any = 1,
        editor_values: Any = None,
        editor_values_seq: Any = None,
        render_cache_max_bytes: Any = 0,
        expected_template_id: Any = None,
    ) -> dict:
        epoch = session.cancel_epoch
        # Read once: every later step of this request uses this workflow.
        template_id = session.template_id
        self._remember_loop()
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_COUNT:
            raise SdkError(f"Generate count must be 1 to {MAX_COUNT}.", "invalid_count")
        # Gate and count with no await between them: an uninstall reserved
        # before this point is seen, and one reserved after waits for it.
        self._check_not_uninstalling()
        self._generating += 1
        # Until its last submit, this Generate may still add runs to the session:
        # no other session absorbs it meanwhile.
        session.generating += 1
        try:
            return await self._generate(session, template_id, expected_template_id, count, editor_values,
                                        render_cache_max_bytes, tab_session_id, editor_values_seq, epoch)
        finally:
            session.generating -= 1
            self._generating -= 1

    def _check_not_uninstalling(self) -> None:
        if self._uninstalling():
            raise SdkError("The Sonder App packages are being removed.", "sdk_uninstalling", 409)

    async def wait_idle(self, timeout: float) -> bool:
        """Wait until no Generate is between its gate and its submit; False on timeout."""
        deadline = time.monotonic() + timeout
        while self._generating:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    async def _generate(self, session: SdkSession, template_id: str, expected_template_id: Any, count: int,
                        editor_values: Any, render_cache_max_bytes: Any, tab_session_id: str, editor_values_seq: Any,
                        epoch: int) -> dict:
        self._check_workflow(session, template_id, expected_template_id)
        if not await asyncio.to_thread(self._sdk_ready):
            raise SdkError("The comfy-sdk package is not installed.", "sdk_not_installed", 409)
        valid, render_cache_max_bytes = clean_editor_value("render_cache_max_bytes", render_cache_max_bytes)
        if not valid:
            render_cache_max_bytes = 0

        writer = sdk_templates.clean_editor_writer({"tab": tab_session_id, "seq": editor_values_seq}) if editor_values is not None else None
        async with self.template_lock(template_id):
            await self.authorized(session.session_id, session.secret, tab_session_id)
            # A switch queued ahead of this Generate moved the session on.
            self._check_workflow(session, template_id)
            if len(session.unfinished()) + count > MAX_UNFINISHED_PER_SESSION:
                raise SdkError("Too many takes are already waiting. Wait or cancel some.", "too_many_runs", 429)
            build = await asyncio.to_thread(self._build_base, session, editor_values, render_cache_max_bytes, writer,
                                           template_id)
            self._check_submit_epoch(session, epoch, build)
            # A Replace may have brought in nodes this ComfyUI lacks: name them
            # now rather than let ComfyUI refuse the submit.
            missing_nodes = sdk_templates.missing_node_classes(build["prompt"], self._node_lookup)
            if missing_nodes:
                raise SdkError(sdk_templates.missing_nodes_message(missing_nodes), "missing_nodes", 409,
                               missing_nodes=missing_nodes, stale=build["stale"])
            refusals = sdk_templates.generate_refusals(build["view"])
            if not prompt_graph.nodes_reaching(build["prompt"], build["editor_key"], prompt_graph.SONDER_SAVE_CLASS_TYPES):
                refusals.append("The current node switches turn off every Sonder Save Video and Save Bridge "
                                "that takes its project from the editor, so takes would not land. Switch one on.")
            from .model_requirements import missing_models, missing_model_message
            missing = await asyncio.to_thread(missing_models, build["prompt"], executable_only=True)
            refusals.extend(map(missing_model_message, missing))
            if refusals:
                raise SdkError("This workflow can't generate yet.", "generate_refused", 409, problems=refusals, stale=build["stale"])
            try:
                target = await self._ensure_proxy()
            except sdk_proxy.ProxyUnavailable as exc:
                raise SdkError(str(exc), exc.code, 503) from exc
            client = await self._client(target)
            self._check_submit_epoch(session, epoch, build)
            return {**await self._submit_runs(session, client, build, count, epoch), "stale": build["stale"]}

    @staticmethod
    def _check_submit_epoch(session, epoch, build):
        if session.cancel_epoch != epoch:
            raise SdkError("Canceled before it was sent.", "canceled_before_submit", 409, stale=build["stale"])

    def _build_base(self, session: SdkSession, editor_values: Any, render_cache_max_bytes: Any, writer,
                    template_id: str | None = None) -> dict:
        template_id = template_id or session.template_id
        with sdk_templates.template_lock(template_id):
            return self._build_base_locked(session, editor_values, render_cache_max_bytes, writer, template_id)

    def _build_base_locked(self, session: SdkSession, editor_values: Any, render_cache_max_bytes: int, writer,
                           template_id: str) -> dict:
        """The prompt every run of this Generate shares, plus its Controls view."""
        stale = False
        if editor_values is not None:
            result = sdk_templates.write_editor_state(session.project, editor_values, writer=writer)
            stale = result["stale"]
            # The click's snapshot still drives its run, even when a newer save
            # already owns the stored position.
            values = {**result["values"], **clean_editor_values(editor_values, sdk_templates.EDITOR_STATE_FIELDS)}
        else:
            values = sdk_templates.read_editor_state(session.project)
        prompt, meta = sdk_templates.load_template(template_id)
        self._template_names[template_id] = meta.get("name", "")
        stored = sdk_templates.read_control_values(template_id)
        editors = [key for key, node in prompt.items() if node.get("class_type") == "SonderEditor"]
        if len(editors) != 1:
            raise SdkError("The workflow needs exactly one Sonder Editor.", "invalid_template", 409)
        editor_inputs = prompt[editors[0]]["inputs"]
        # The exported position belongs to the author's project: start from the
        # editor's defaults, then this project's stored SDK position.
        defaults = editor_optional_inputs()
        for name in sdk_templates.EDITOR_STATE_FIELDS:
            editor_inputs[name] = defaults[name][1]["default"]
        editor_inputs.update(values)
        editor_inputs["project"] = session.project
        editor_inputs["render_cache_max_bytes"] = render_cache_max_bytes
        view = sdk_templates.apply_stored_values(
            prompt, sdk_templates.describe_stored(prompt, stored, self._describe), stored, describe=self._describe)
        # The switches' node modes, compiled as ComfyUI's serializer would: a
        # switched-off node is not in what runs.
        prompt = sdk_templates.run_prompt(prompt, view)
        # The session's own Editor inputs stand whatever the switches compiled.
        if editors[0] in prompt:
            prompt[editors[0]]["inputs"].update({name: editor_inputs[name] for name in (
                *sdk_templates.EDITOR_STATE_FIELDS, "project", "render_cache_max_bytes")})
        # Seeds are keyed by binding; each target (one per consumer of a
        # promoted seed) is only the address a run writes. A hidden seed is
        # not exposed: the workflow's own value runs and nothing advances. A
        # seed whose nodes are all switched off does not run, so it neither
        # advances nor is recorded; a partly off one writes the targets that run.
        seed_bindings = []
        for binding in view.get("bindings", []):
            if binding.get("kind") != "SEED" or binding.get("status") != control_descriptors.STATUS_OK:
                continue
            if binding.get("hidden") or binding.get("participation") == "inactive":
                continue
            seed = binding.get("value")
            if isinstance(seed, bool) or not isinstance(seed, int):
                seed = 0
            seed_bindings.append({
                "binding_id": binding["binding_id"],
                "targets": [(t["path"], t["input"]) for t in binding.get("targets", []) if t["path"] in prompt],
                "sig": control_descriptors.binding_signature(binding),
                "seed": seed,
                "mode": binding["seed_mode"],
                "min": binding.get("min", 0),
                "max": binding.get("max", SEED_MAX),
                "step": binding.get("step") or 1,
            })
        return {"prompt": prompt, "editor_key": editors[0], "view": view, "seed_bindings": seed_bindings, "stale": stale,
                "template_id": template_id, "template_name": meta.get("name", ""),
                # What this app's curation changes about the workflow's Controls,
                # for the provenance each saved take records (controls_snapshot).
                "controls_delta": controls_snapshot.sdk_overlay_delta(view, prompt)}

    async def _submit_runs(self, session: SdkSession, client, build: dict, count: int, epoch: int) -> dict:
        labels = {str(key): control_descriptors._node_title(node, str(node.get("class_type") or ""))
                  for key, node in build["prompt"].items()}
        seed_bindings = build["seed_bindings"]
        run_ids, seeds_used, failure = [], [], None
        try:
            for index in range(1, count + 1):
                if session.cancel_epoch != epoch:
                    if not run_ids:
                        self._check_submit_epoch(session, epoch, build)
                    break  # Cancel all arrived during this batch: submit no more.
                seeds = {b["binding_id"]: b["seed"] for b in seed_bindings}
                graph = await asyncio.to_thread(self._graph_for_run, build, seeds)
                if session.cancel_epoch != epoch:
                    if not run_ids:
                        self._check_submit_epoch(session, epoch, build)
                    break
                token = secrets.token_urlsafe(24)
                run = SdkRun(
                    run_id=f"run-{uuid.uuid4().hex[:12]}", token=token, session_id=session.session_id,
                    index=index, count=count, seeds=seeds, submitted_at=self._clock(), client=client, node_labels=labels,
                    template_id=build["template_id"], template_name=build["template_name"],
                )
                graph[build["editor_key"]].setdefault("_meta", {})["sonder_run"] = token
                # The run exists before the submit: ComfyUI can finish (or fail) a
                # prompt before the proxy's reply comes back, and run_finished must
                # find it.
                sdk_runs.register_token(token, session.aliases, session_id=session.session_id, run_id=run.run_id,
                                        template_id=build["template_id"])
                session.runs.append(run)
                self._runs_by_token[token] = run
                try:
                    job = await client.submit(client.workflows.from_json(graph))
                except asyncio.CancelledError:
                    self._mark_unconfirmed(run)
                    raise
                except Exception as exc:
                    if _definitely_rejected(exc):
                        sdk_runs.forget_token(token)
                        session.runs.remove(run)
                        self._runs_by_token.pop(token, None)
                        failure = SdkError(_submit_error_message(exc), "submit_failed", 502).payload()
                        logger.warning("SDK submit refused: %s", exc)
                    else:
                        self._mark_unconfirmed(run)
                        run_ids.append(run.run_id)
                        seeds_used.append({cid: str(value) for cid, value in seeds.items()})
                        await self._advance_seeds(build["template_id"], seed_bindings)
                        failure = SdkError(
                            "ComfyUI did not confirm this run. It will show here if it started.",
                            "submit_unconfirmed", 502,
                        ).payload()
                        logger.warning("SDK submit outcome unknown: %s", exc)
                    break
                run.job = job
                run.prompt_id = str(getattr(job, "id", "") or "")
                run.accepted_at = self._clock()
                if not run.final:
                    sdk_progress.watch(run.prompt_id, run.token)
                    if run.state != RUNNING:
                        run.state = QUEUED
                run_ids.append(run.run_id)
                seeds_used.append({cid: str(value) for cid, value in seeds.items()})
                if run.cancel_requested or session.cancel_epoch != epoch:
                    await self._cancel_run(run)
                await self._advance_seeds(build["template_id"], seed_bindings)
        finally:
            self._trim_finished(session)
            self._ensure_presence_loop()
            self._emit(session, force=True)
        return {"ok": failure is None or bool(run_ids), "run_ids": run_ids, "seeds_used": seeds_used, "error": failure}

    def _mark_unconfirmed(self, run: SdkRun) -> None:
        if not run.final:
            run.state = UNCONFIRMED
            run.unconfirmed_since = self._clock()

    @staticmethod
    def _graph_for_run(build: dict, seeds: dict) -> dict:
        graph = copy.deepcopy(build["prompt"])
        for binding in build["seed_bindings"]:
            for path, name in binding["targets"]:
                graph[path]["inputs"][name] = seeds[binding["binding_id"]]
        # ComfyUI reads only ``_meta.title``, so this never affects caching. A
        # delta already in the template (one built from a take's prompt) is
        # dropped: provenance must describe this app's curation only.
        editor = graph[build["editor_key"]]
        meta = editor.get("_meta")
        if isinstance(meta, dict):
            meta.pop(controls_snapshot.SDK_META_KEY, None)
        delta = build.get("controls_delta")
        if delta:
            if not isinstance(meta, dict):
                meta = editor["_meta"] = {}
            meta[controls_snapshot.SDK_META_KEY] = copy.deepcopy(delta)
        return graph

    async def _advance_seeds(self, template_id: str, seed_bindings: list) -> None:
        changed = {}
        for binding in seed_bindings:
            if binding["mode"] == "fixed":
                continue
            binding["seed"] = control_descriptors.next_seed_value(
                binding["mode"], binding["seed"],
                minimum=binding["min"], maximum=binding["max"], step=binding["step"],
            )
            changed[binding["binding_id"]] = (binding["seed"], binding["sig"])
        if not changed:
            return

        def mutate(data: dict) -> None:
            for binding_id, (seed, sig) in changed.items():
                entry = data["bindings"].get(binding_id)
                if not isinstance(entry, dict):
                    entry = data["bindings"][binding_id] = {}
                entry["value"] = seed
                entry["sig"] = sig

        try:
            await await_template_io(sdk_templates.update_control_values, template_id, mutate)
        except sdk_templates.TemplateError:
            logger.info("Workflow %s was deleted during Generate; seeds not saved.", template_id)
        except Exception:
            # The run is already in ComfyUI's queue: tracking it matters more
            # than this seed write. The next Generate starts from the old seed.
            logger.exception("Could not save the advanced seeds of workflow %s", template_id)

    def _trim_finished(self, session: SdkSession) -> None:
        finished = [run for run in session.runs if run.final]
        for run in finished[:-FINISHED_RUNS_KEPT] if len(finished) > FINISHED_RUNS_KEPT else []:
            session.runs.remove(run)
            self._runs_by_token.pop(run.token, None)

    # ── Cancel ───────────────────────────────────────────────────────────────

    async def cancel(self, session: SdkSession) -> dict:
        """Cancel every unfinished run, and stop a Generate batch still submitting."""
        session.cancel_epoch += 1
        pending = session.unfinished()
        for run in pending:
            run.cancel_requested = True
        self._emit(session, force=True)
        # Queued ones first, so ComfyUI does not start the next while the running
        # one is being interrupted.
        for run in sorted(pending, key=lambda r: r.state == RUNNING):
            await self._cancel_run(run)
        self._ensure_presence_loop()
        return {"ok": True, "canceling": [run.run_id for run in pending]}

    async def cancel_by_secret(self, session_id: str, secret: str) -> dict:
        """Launcher departure cancellation: a secret never displaces a live tab."""
        self._remember_loop()
        session = self.session_for(session_id, secret)
        owner = await self._get_owner(session.project, host_id=session.session_id)
        if owner and owner.get("status") in ("active", "orphaned"):
            raise SdkError("This session is open in another tab.", "not_owner", 409)
        # cancel marks the epoch/runs before its first await. An owner arriving
        # later cannot race the authorization and cancellation decision.
        return await self.cancel(session)

    async def _cancel_run(self, run: SdkRun) -> None:
        if run.final or not run.prompt_id:
            return  # a submitting or unconfirmed run is canceled when presence adopts it
        try:
            if run.job is None and run.client is not None:
                run.job = await run.client.jobs.get(run.prompt_id)
            if run.job is not None:
                await run.job.cancel()
        except Exception as exc:  # the run still ends through run_finished or presence
            logger.info("SDK cancel of %s failed: %s", run.prompt_id, exc)

    # ── Outcome ──────────────────────────────────────────────────────────────

    def _on_run_finished_threadsafe(self, payload: dict) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self.apply_run_finished, payload)

    def apply_run_finished(self, payload: dict) -> None:
        run = self._runs_by_token.get(str(payload.get("run_token") or ""))
        if run is None:
            prompt_id = str(payload.get("prompt_id") or "")
            run = next((r for s in self._sessions.values() for r in s.runs if prompt_id and r.prompt_id == prompt_id), None)
        if run is None:
            return
        payload_prompt = str(payload.get("prompt_id") or "")
        if run.prompt_id and payload_prompt and payload_prompt != run.prompt_id:
            return  # another prompt carrying this token (a re-queued copy): not this run
        if payload_prompt and not run.prompt_id:
            run.prompt_id = payload_prompt
        outcome = str(payload.get("outcome") or "")
        leased = list(payload.get("leased_job_ids") or [])
        returned = list(payload.get("returned_job_ids") or [])
        run.job_id = str(payload.get("claimed_job_id") or "") or (leased[0] if leased else run.job_id)
        run.outcome = outcome
        if outcome == "success" and returned:
            # It ran, but its job went back to pending: no take for that job.
            state = ENDED
            run.note = str(payload.get("note") or "The run finished without completing its job.")
        elif outcome == "success":
            state = LANDED
            run.note = ""
        elif outcome == "interrupted" and run.cancel_requested:
            state = CANCELED
            run.note = "Canceled."
        else:
            state = FAILED
            run.note = str(payload.get("note") or "The run failed.")
        run.detail = str(payload.get("detail") or "")[:2000]
        self._finish(run, state)

    def _finish(self, run: SdkRun, state: str) -> None:
        run.state = state
        run.position = None
        run.finished_at = self._clock()
        sdk_runs.forget_token(run.token)
        sdk_progress.unwatch(run.prompt_id, run.token)
        run.progress_node, run.progress_count = "", None
        session = self._sessions.get(run.session_id)
        if session is not None:
            self._emit(session, force=True)

    def _ensure_presence_loop(self) -> None:
        if self._presence_task is None or self._presence_task.done():
            self._presence_task = asyncio.ensure_future(self._presence_loop())

    async def _presence_loop(self) -> None:
        while True:
            pending = [run for s in self._sessions.values() for run in s.unfinished()]
            if not pending:
                return
            await asyncio.sleep(PRESENCE_INTERVAL_SEC)
            try:
                await self.presence_tick()
            except Exception:
                logger.exception("SDK presence check failed")

    async def presence_tick(self) -> None:
        pending = [run for s in self._sessions.values() for run in s.unfinished()]
        accepted = [run for run in pending if run.prompt_id]
        unconfirmed = [run for run in pending if not run.prompt_id and run.state == UNCONFIRMED]
        if not accepted and not unconfirmed:
            return
        queue = self._queue_provider()
        snapshot = await asyncio.to_thread(
            presence_snapshot, queue, [run.prompt_id for run in accepted], [run.token for run in unconfirmed],
        )
        now = self._clock()
        changed = set()
        adopted = snapshot.get("tokens", {})
        for run in unconfirmed:
            if run.final:
                continue
            if run.token in adopted:
                # The submit did reach ComfyUI: track it like any accepted run.
                run.prompt_id, run.accepted_at, run.note = adopted[run.token], now, ""
                sdk_progress.watch(run.prompt_id, run.token)
                accepted.append(run)
                if run.cancel_requested:
                    asyncio.ensure_future(self._cancel_run(run))
            elif now - run.unconfirmed_since > UNCONFIRMED_SUBMIT_GRACE_SEC:
                run.note, run.outcome = "ComfyUI did not accept the run.", "error"
                self._finish(run, FAILED)
                changed.add(run.session_id)
        for run in accepted:
            if run.final:
                continue
            before = (run.state, run.position)
            where, position = snapshot.get(run.prompt_id, (None, None))
            release = sdk_runs.release_state_for_prompt(run.prompt_id)
            if where == "running":
                run.state, run.position, run.absent_since = RUNNING, None, 0.0
            elif where == "queued":
                run.state, run.position, run.absent_since = QUEUED, position, 0.0
            elif release == sdk_runs.RELEASE_OPEN:
                # Taken off the queue: wait for run_finished, even if history was wiped.
                run.state, run.position, run.absent_since = FINISHING, None, 0.0
            elif release == sdk_runs.RELEASE_CLOSED:
                # Released: run_finished is on its way (marshalled from the worker
                # thread). If it never comes, end the run rather than wait forever.
                run.state, run.position, run.absent_since = FINISHING, None, 0.0
                run.closed_since = run.closed_since or now
                if now - run.closed_since > CLOSED_WITHOUT_FINISH_SEC:
                    run.note = "The run ended, but Sonder could not confirm its take."
                    self._finish(run, ENDED)
            elif where == "history":
                # Ran, but no release record was seen (the queue hook missed it):
                # nothing will announce it, so end it after a while, unconfirmed.
                run.state, run.position = FINISHING, None
                run.history_unconfirmed_since = run.history_unconfirmed_since or now
                if now - run.history_unconfirmed_since > UNCONFIRMED_GRACE_SEC:
                    run.note = "The run ended, but Sonder could not confirm its take."
                    self._finish(run, ENDED)
            else:
                run.absent_since = run.absent_since or now
                if now - run.absent_since > ABSENT_GRACE_SEC:
                    run.note = "Canceled before it started." if run.cancel_requested else "Removed from ComfyUI's queue before it started."
                    run.outcome = "canceled"
                    self._finish(run, CANCELED)
            if (run.state, run.position) != before:
                changed.add(run.session_id)
        for session in self._sessions.values():
            if session.session_id in changed:
                self._emit(session)

    # ── In-process node progress ────────────────────────────────────────────

    def _on_progress_threadsafe(self, snapshot: dict) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self.apply_progress, snapshot)

    @staticmethod
    def _progress_label(labels: dict, node: str, display: str) -> str:
        for key in (node, node.split(".", 1)[0], display):
            if key in labels:
                return labels[key]
        return ""

    def apply_progress(self, snapshot: dict) -> None:
        run = self._runs_by_token.get(snapshot["run_token"])
        if run is None or run.final:
            return
        prompt_id = snapshot["prompt_id"]
        if run.prompt_id and run.prompt_id != prompt_id:
            return
        run.prompt_id = prompt_id
        run.state, run.position = RUNNING, None
        run.progress_node = self._progress_label(run.node_labels, snapshot["node_id"], snapshot["display_node_id"])
        run.progress_count = snapshot["count"] if run.progress_node else None
        session = self._sessions.get(run.session_id)
        if session is not None:
            self._emit(session)

    # ── Status events ────────────────────────────────────────────────────────

    def _emit(self, session: SdkSession, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - session.last_emit < STATUS_MIN_INTERVAL_SEC:
            if not session.emit_scheduled and self._loop is not None:
                session.emit_scheduled = True
                delay = STATUS_MIN_INTERVAL_SEC - (now - session.last_emit)
                self._loop.call_later(delay, self._emit_scheduled, session.session_id)
            return
        session.last_emit = now
        session.seq += 1
        snapshot = session.public(self.template_name(session))
        for alias in session.aliases:
            self._emit_event(alias, {"type": "generation_status", "project_id": alias, **snapshot})

    def _emit_scheduled(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.emit_scheduled = False
        self._emit(session, force=True)

    async def aclose_clients(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            try:
                await client.aclose()
            except Exception:
                logger.debug("Closing an SDK client failed", exc_info=True)

    def has_unfinished_runs(self) -> bool:
        return any(session.unfinished() for session in self._sessions.values())


def _submit_error_message(exc: Exception) -> str:
    text = str(exc).strip()
    if len(text) > 300:
        text = text[:300] + "…"
    return f"ComfyUI did not accept the run: {text}" if text else "ComfyUI did not accept the run."


_runner: SdkRunner | None = None


def get_runner(**kwargs: Any) -> SdkRunner:
    global _runner
    if _runner is None:
        _runner = SdkRunner(**kwargs)
    return _runner


def reset_runner_for_tests() -> None:
    global _runner
    if _runner is not None:
        _runner.close()
    _runner = None
