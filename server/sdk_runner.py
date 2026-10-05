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

The SDK's own event stream is used for progress only, one stream per session,
because the proxy serves at most eight streams in total.

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

from . import control_descriptors, sdk_proxy, sdk_runs, sdk_templates, session_registry
from .editor_inputs import clean_editor_value, editor_optional_inputs
from .sdk_install import install_state, uninstall_running

logger = logging.getLogger("sonder_editor")

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
    progress: float | None = None
    progress_message: str = ""
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
            "progress": self.progress,
            "progress_message": self.progress_message,
            "prompt_id": self.prompt_id,
            "job_id": self.job_id,
            "outcome": self.outcome,
            "note": self.note,
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
    stream_task: Any = None
    stream_run_id: str = ""
    cancel_epoch: int = 0

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
        sdk_ready: Callable[[], bool] = lambda: bool(install_state()["sdk"]["supported"]),
        uninstalling: Callable[[], bool] = uninstall_running,
        describe: Callable[[dict], dict] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._resolve_project = resolve_project
        self._client_factory = client_factory
        self._ensure_proxy = ensure_proxy
        self._queue_provider = queue_provider
        self._emit_event = emit
        self._get_owner = get_owner
        self._sdk_ready = sdk_ready
        self._uninstalling = uninstalling
        self._generating = 0  # Generates between their gate and their submit
        self._describe = describe or control_descriptors.describe_controls
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
        self._remove_listener = sdk_runs.add_run_finished_listener(self._on_run_finished_threadsafe)
        session_registry.set_sdk_secret_resolver(self.secret_for_host)

    def close(self) -> None:
        self._remove_listener()
        session_registry.set_sdk_secret_resolver(None)

    # ── Registry hook ────────────────────────────────────────────────────────

    def secret_for_host(self, host_id: str) -> str | None:
        session = self._sessions.get(str(host_id or ""))
        return session.secret if session else None

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
            if session.unfinished() or now - session.last_activity < SESSION_IDLE_SEC:
                continue
            owner = await self._get_owner(session.project, host_id=session.session_id)
            if owner and owner.get("status") in ("active", "orphaned"):
                continue  # a tab still holds it, perhaps throttled in the background
            self._drop_session(session)
        if self._sessions:
            self._idle_since = 0.0
        elif self._idle_since and now - self._idle_since > PROXY_IDLE_STOP_SEC:
            self._idle_since = 0.0
            await asyncio.to_thread(sdk_proxy.stop_owned_proxy)
            await self.aclose_clients()

    async def open_session(self, template_id: str, project: str) -> dict:
        self._remember_loop()
        # Serialized: eviction awaits, and two opens must not both pass the cap
        # or both create a session for one workflow and project.
        async with self._open_lock:
            return await self._open_session_locked(template_id, project)

    async def _open_session_locked(self, template_id: str, project: str) -> dict:
        template_id = sdk_templates.check_template_id(template_id)
        _, meta = await asyncio.to_thread(sdk_templates.load_template, template_id)
        if not meta.get("validation", {}).get("ok", True):
            raise SdkError("This workflow can't drive the editor.", "invalid_template", 409)
        if self._resolve_project is None:
            raise SdkError("Projects are unavailable.", "projects_unavailable", 500)
        # An async resolver: it loads the project through the project I/O pool.
        project_dir, aliases = await self._resolve_project(project)
        if not project_dir:
            raise SdkError("Unknown project.", "unknown_project", 404)
        # The folder's own spelling: the editor's project choice must match it exactly.
        project = os.path.basename(os.path.normpath(project_dir))
        self._template_names[template_id] = meta.get("name", "")
        now = self._clock()
        for session in self._sessions.values():
            if session.template_id == template_id and session.project == project:
                session.last_activity = now
                session.project_dir, session.aliases = project_dir, list(aliases)
                return self._open_result(session)
        if len(self._sessions) >= MAX_SESSIONS:
            await self._evict_one()
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
        self._sessions[session.session_id] = session
        self._idle_since = 0.0
        asyncio.ensure_future(self._warm_proxy())
        return self._open_result(session)

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

    async def _warm_proxy(self) -> None:
        if self._uninstalling():
            return  # the packages are going; Generate reports it
        try:
            await self._ensure_proxy()
        except Exception as exc:  # reported again, with its reason, by Generate
            logger.info("SDK proxy not ready at session open: %s", exc)

    async def _evict_one(self) -> None:
        candidates = []
        for session in self._sessions.values():
            if session.unfinished():
                continue
            owner = await self._get_owner(session.project, host_id=session.session_id)
            if owner and owner.get("status") in ("active", "orphaned"):
                continue
            candidates.append(session)
        if not candidates:
            raise SdkError("Too many Sonder App sessions are busy. Close one and try again.", "sdk_sessions_busy", 429)
        oldest = min(candidates, key=lambda s: s.last_activity)
        self._drop_session(oldest)

    def _drop_session(self, session: SdkSession) -> None:
        self._sessions.pop(session.session_id, None)
        self._cancel_stream(session)
        for run in session.runs:
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
        count: Any = 1,
        editor_values: Any = None,
        render_cache_max_bytes: Any = 0,
    ) -> dict:
        self._remember_loop()
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_COUNT:
            raise SdkError(f"Generate count must be 1 to {MAX_COUNT}.", "invalid_count")
        # Gate and count with no await between them: an uninstall reserved
        # before this point is seen, and one reserved after waits for it.
        self._check_not_uninstalling()
        self._generating += 1
        try:
            return await self._generate(session, count, editor_values, render_cache_max_bytes)
        finally:
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

    async def _generate(self, session: SdkSession, count: int, editor_values: Any, render_cache_max_bytes: Any) -> dict:
        if not await asyncio.to_thread(self._sdk_ready):
            raise SdkError("The comfy-sdk package is not installed.", "sdk_not_installed", 409)
        valid, render_cache_max_bytes = clean_editor_value("render_cache_max_bytes", render_cache_max_bytes)
        if not valid:
            render_cache_max_bytes = 0

        async with self.template_lock(session.template_id):
            if len(session.unfinished()) + count > MAX_UNFINISHED_PER_SESSION:
                raise SdkError("Too many takes are already waiting. Wait or cancel some.", "too_many_runs", 429)
            build = await asyncio.to_thread(self._build_base, session, editor_values, render_cache_max_bytes)
            refusals = [problem for problem in map(sdk_templates.generate_refusal, build["controls"]) if problem]
            if refusals:
                raise SdkError("This workflow can't generate yet.", "generate_refused", 409, problems=refusals)
            try:
                target = await self._ensure_proxy()
            except sdk_proxy.ProxyUnavailable as exc:
                raise SdkError(str(exc), exc.code, 503) from exc
            client = await self._client(target)
            return await self._submit_runs(session, client, build, count)

    def _build_base(self, session: SdkSession, editor_values: Any, render_cache_max_bytes: int) -> dict:
        """The prompt every run of this Generate shares, plus its Control descriptors."""
        if editor_values is not None:
            values = sdk_templates.write_editor_state(session.project, editor_values)
        else:
            values = sdk_templates.read_editor_state(session.project)
        prompt, meta = sdk_templates.load_template(session.template_id)
        self._template_names[session.template_id] = meta.get("name", "")
        stored = sdk_templates.read_control_values(session.template_id)
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
        described = self._describe(prompt)
        controls = described.get("controls", [])
        seed_controls = []
        for control in controls:
            entry = stored["controls"].get(control.get("control_id") or "")
            if not isinstance(entry, dict):
                entry = {}
            for key in control.get("keys", []):
                inputs = prompt[key]["inputs"]
                if isinstance(entry.get("value_text"), str):
                    inputs["value_text"] = entry["value_text"]
            if control.get("kind") == "SEED" and control.get("control_id"):
                seed = entry.get("seed")
                if isinstance(seed, bool) or not isinstance(seed, int):
                    seed = control.get("seed") if isinstance(control.get("seed"), int) else 0
                mode = entry.get("seed_mode") if entry.get("seed_mode") in control_descriptors.SEED_MODES else "randomize"
                seed_controls.append({
                    "control_id": control["control_id"],
                    "keys": list(control.get("keys", [])),
                    "seed": seed,
                    "mode": mode,
                    "min": control.get("min", 0),
                    "max": control.get("max", SEED_MAX),
                    "step": control.get("step") or 1,
                })
        for field_descriptor in described.get("fields", []):
            if field_descriptor.get("status") != control_descriptors.STATUS_OK:
                continue
            name = f"{field_descriptor.get('class_type')}/{field_descriptor.get('field')}"
            if name in stored["fields"]:
                try:
                    value = control_descriptors.coerce_control_value(field_descriptor["kind"], stored["fields"][name])
                except control_descriptors.ControlValueError:
                    continue
                prompt[field_descriptor["key"]]["inputs"][field_descriptor["field"]] = value
        if any(stored["controls"].get(c.get("control_id") or "", {}).get("value_text") is not None for c in controls):
            controls = self._describe(prompt).get("controls", [])  # re-check with the stored values applied
        return {"prompt": prompt, "editor_key": editors[0], "controls": controls, "seed_controls": seed_controls}

    async def _submit_runs(self, session: SdkSession, client, build: dict, count: int) -> dict:
        seed_controls = build["seed_controls"]
        run_ids, seeds_used, failure = [], [], None
        epoch = session.cancel_epoch
        try:
            for index in range(1, count + 1):
                if session.cancel_epoch != epoch:
                    break  # Cancel all arrived during this batch: submit no more.
                seeds = {c["control_id"]: c["seed"] for c in seed_controls}
                graph = await asyncio.to_thread(self._graph_for_run, build, seeds)
                token = secrets.token_urlsafe(24)
                run = SdkRun(
                    run_id=f"run-{uuid.uuid4().hex[:12]}", token=token, session_id=session.session_id,
                    index=index, count=count, seeds=seeds, submitted_at=self._clock(), client=client,
                )
                graph[build["editor_key"]].setdefault("_meta", {})["sonder_run"] = token
                # The run exists before the submit: ComfyUI can finish (or fail) a
                # prompt before the proxy's reply comes back, and run_finished must
                # find it.
                sdk_runs.register_token(token, session.aliases, session_id=session.session_id, run_id=run.run_id)
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
                        await self._advance_seeds(session, seed_controls)
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
                    run.state = QUEUED
                run_ids.append(run.run_id)
                seeds_used.append({cid: str(value) for cid, value in seeds.items()})
                if run.cancel_requested or session.cancel_epoch != epoch:
                    await self._cancel_run(run)
                await self._advance_seeds(session, seed_controls)
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
        for control in build["seed_controls"]:
            for key in control["keys"]:
                graph[key]["inputs"]["seed"] = seeds[control["control_id"]]
        return graph

    async def _advance_seeds(self, session: SdkSession, seed_controls: list) -> None:
        changed = {}
        for control in seed_controls:
            if control["mode"] == "fixed":
                continue
            control["seed"] = control_descriptors.next_seed_value(
                control["mode"], control["seed"],
                minimum=control["min"], maximum=control["max"], step=control["step"],
            )
            changed[control["control_id"]] = control["seed"]
        if not changed:
            return

        def mutate(data: dict) -> None:
            for control_id, seed in changed.items():
                entry = data["controls"].get(control_id)
                if not isinstance(entry, dict):
                    entry = data["controls"][control_id] = {}
                entry["seed"] = seed

        try:
            await asyncio.to_thread(sdk_templates.update_control_values, session.template_id, mutate)
        except sdk_templates.TemplateError:
            logger.info("Workflow %s was deleted during Generate; seeds not saved.", session.template_id)
        except Exception:
            # The run is already in ComfyUI's queue: tracking it matters more
            # than this seed write. The next Generate starts from the old seed.
            logger.exception("Could not save the advanced seeds of workflow %s", session.template_id)

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
        self._finish(run, state)

    def _finish(self, run: SdkRun, state: str) -> None:
        run.state = state
        run.position = None
        run.finished_at = self._clock()
        sdk_runs.forget_token(run.token)
        session = self._sessions.get(run.session_id)
        if session is not None:
            if session.stream_run_id == run.run_id:
                self._cancel_stream(session)
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
            self._manage_stream(session)
            if session.session_id in changed:
                self._emit(session)

    # ── Progress stream (one per session) ────────────────────────────────────

    def _manage_stream(self, session: SdkSession) -> None:
        running = next((run for run in session.runs if run.state == RUNNING and run.job is not None), None)
        if running is None:
            if session.stream_task is not None and not any(r.run_id == session.stream_run_id and not r.final for r in session.runs):
                self._cancel_stream(session)
            return
        if session.stream_run_id == running.run_id and session.stream_task is not None and not session.stream_task.done():
            return
        self._cancel_stream(session)
        session.stream_run_id = running.run_id
        session.stream_task = asyncio.ensure_future(self._stream_progress(session, running))

    def _cancel_stream(self, session: SdkSession) -> None:
        task = session.stream_task
        session.stream_task, session.stream_run_id = None, ""
        if task is not None and not task.done():
            task.cancel()

    async def _stream_progress(self, session: SdkSession, run: SdkRun) -> None:
        try:
            from comfy_sdk import Progress

            async for event in run.job.events():
                if run.final:
                    return
                if isinstance(event, Progress):
                    value = event.value if isinstance(event.value, (int, float)) else None
                    run.progress = max(0.0, min(1.0, float(value))) if value is not None else None
                    run.progress_message = str(event.message or "")
                    self._emit(session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # progress is optional: 429, disconnects, anything
            logger.debug("SDK progress stream ended for %s: %s", run.prompt_id, exc)

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
