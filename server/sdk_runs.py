"""In-process registry for prompt runs: release records, claim leases and SDK run tokens.

One ComfyUI prompt execution is one *run*. The prompt queue hook in
``nodes/io_nodes.py`` opens a release record when ComfyUI takes a prompt off the
queue and closes it in ``task_done``'s ``finally``, after bridge finalization. That
close is the single "the take has landed" moment: ComfyUI writes its history
*before* Sonder finalizes bridges, so neither ComfyUI history nor an SDK status can
stand in for it.

- **Release records** exist for every prompt, keyed by the queue item id ComfyUI
  passes to both ``get`` and ``task_done``. A key is *trusted* only while its record
  is open, which means only the queue hook can have minted it.
- **Leases** tie a trusted key to the queue job its editor claimed. Closing the
  record hands the leases back so the caller can return a still-``running`` job to
  ``pending``: no browser has to notice a failed or interrupted run.
- **Run tokens** identify an SDK-submitted run. They are registered before submit,
  consumed by the first execute that presents one, refused afterwards, and removed
  when the run's record closes. One that never executes expires.
- **First results** let a node with one-shot side effects run once per prompt.
  ComfyUI can execute a finished node again within the same prompt (``--cache-none``,
  or RAM-pressure eviction, when a lazy input or a node expansion links to it late);
  the repeat gets the first result instead of claiming, saving or writing again.
  They live on the open record, so they go with it.
- **Registrations** say who owns a run before it is submitted: an SDK token (above)
  or a canvas *receipt* the browser stamps on its Editor nodes. Both carry a
  process-wide ``seq``, the order "latest" means, never arrival order. The queue's
  ``put`` hook binds a registration to the prompt id ComfyUI queued it under, so a
  run that finishes before the submit reply is still known. A receipt is a
  correlation id, not a credential: it sits in the prompt and in history.
- **Bound runs** are what binding creates: per prompt id, the run's owners,
  ``seq``, workflow and lifecycle (queued, started, finished or removed), bounded
  and fair across owners. This is the one record of a registered run;
  ``run_outputs`` keeps only what was observed for it.
- **Execution roots** are the output nodes ComfyUI will run (``outputs_to_execute``),
  frozen on every record at ``get`` whether or not anyone registered the run. The
  scope they imply is computed once per prompt and shared by its release record
  and its bound run.

Stdlib only and thread-safe: the hook runs on ComfyUI's prompt-worker thread and
routes run on the event loop. Everything here is ephemeral by design; ComfyUI's
queue does not survive a restart either.
"""

import collections
import itertools
import logging
import re
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_RELEASES: dict[str, dict[str, Any]] = {}
_KEY_BY_PROMPT_OBJECT: dict[int, str] = {}
_TOKENS: dict[str, dict[str, Any]] = {}
_SEQ = itertools.count(1)
# receipt -> registration. Unbound ones expire; bound ones are kept a while so a
# re-queued prompt carrying the same receipt is recognised and not observed twice.
_RECEIPTS: dict[str, dict[str, Any]] = {}
# receipt -> a queued prompt whose registration has not arrived yet (the browser
# registers in parallel with its submit, so either may come first).
_PARKED: dict[str, dict[str, Any]] = {}
_SCOPE_UNSET = object()

RECEIPT_UNBOUND_TTL_SEC = 60.0
RECEIPT_BOUND_RETENTION_SEC = 30 * 60
MAX_UNBOUND_PER_OWNER = 8
MAX_UNBOUND_TOTAL = 256
MAX_PARKED = 256
_RECEIPT_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
EDITOR_CLASS_TYPE = "SonderEditor"
# prompt_id -> ("open" | "closed", when). Lets the SDK runner tell a prompt that
# ComfyUI never took off its queue (deleted, cleared) from one that is running or
# finalizing after ComfyUI's history was wiped. Closed entries are pruned.
_RELEASE_STATE_BY_PROMPT: dict[str, tuple[str, float]] = {}
_RUN_FINISHED_LISTENERS: list[Callable[[dict[str, Any]], None]] = []

RELEASE_OPEN = "open"
RELEASE_CLOSED = "closed"
RELEASE_UNKNOWN = "unknown"
CLOSED_RELEASE_RETENTION_SEC = 30 * 60

# A registered token whose run never executes (canceled while queued, or lost to a
# restart of the proxy) is refused after this long, so a replay cannot use it. The
# SDK runner forgets a token as soon as it knows the run ended; this is the backstop.
REGISTERED_TOKEN_TTL_SEC = 12 * 60 * 60

TOKEN_OK = "ok"
TOKEN_UNKNOWN = "unknown"
TOKEN_USED = "used"
TOKEN_MISMATCH = "mismatch"


def _key(value: Any) -> str:
    return str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""


# --- Release records -----------------------------------------------------------

def _prompt_run_token(prompt: Any) -> str:
    if not isinstance(prompt, dict):
        return ""
    for node in prompt.values():
        meta = node.get("_meta") if isinstance(node, dict) else None
        token = meta.get("sonder_run") if isinstance(meta, dict) else None
        if isinstance(token, str) and token.strip():
            return token.strip()
    return ""


def open_release(key: Any, prompt_id: Any = "", prompt: Any = None, roots: Any = None) -> bool:
    """Open the record for a prompt ComfyUI just took off its queue.

    A registered run token in the prompt tells the record its project up front, so
    the run is announced even when it fails before the editor executes. ``roots``
    is the queue item's ``outputs_to_execute``; None when the caller has none.
    """
    key = _key(key)
    if not key:
        return False
    with _LOCK:
        previous = _RELEASES.pop(key, None)
        if previous is not None:
            _KEY_BY_PROMPT_OBJECT.pop(previous.get("prompt_object_id"), None)
        record = {
            "key": key,
            "prompt_id": str(prompt_id or ""),
            "opened_at": time.time(),
            "prompt_object_id": id(prompt) if isinstance(prompt, dict) else None,
            "projects": {},
            "leases": [],
            "token": "",
            "hinted_token": "",
            "token_project_ids": [],
            "results": {},
            # O(1) here: a cancel can race the interval between get and ComfyUI
            # resetting its interrupt flag, so nothing heavy runs in this hook.
            "roots": _clean_roots(roots),
            "prompt_ref": prompt if isinstance(prompt, dict) else None,
            "scope": _SCOPE_UNSET,
            "saved_to_project": False,
            "committed_modified_at": "",
        }
        hinted_token = _prompt_run_token(prompt)
        hinted = _TOKENS.get(hinted_token)
        if hinted is not None:
            # Kept so a run that ends before its editor executes still reports its
            # token, and so closing the record retires it (no later replay).
            record["hinted_token"] = hinted_token
            record["token_project_ids"] = list(hinted["project_ids"])
        _RELEASES[key] = record
        if record["prompt_object_id"] is not None:
            _KEY_BY_PROMPT_OBJECT[record["prompt_object_id"]] = key
        if record["prompt_id"]:
            _RELEASE_STATE_BY_PROMPT[record["prompt_id"]] = (RELEASE_OPEN, time.time())
    return True


def trusted_key_for_prompt(prompt: Any) -> str:
    """The open record's key for this exact prompt object, or "" when untrusted."""
    if not isinstance(prompt, dict):
        return ""
    with _LOCK:
        key = _KEY_BY_PROMPT_OBJECT.get(id(prompt), "")
        return key if key in _RELEASES else ""


def is_trusted(key: Any) -> bool:
    with _LOCK:
        return _key(key) in _RELEASES


def note_project(key: Any, project_dir: str, project_ids) -> None:
    """Record which project a run touched, so its release can be announced there."""
    key = _key(key)
    ids = [str(item) for item in project_ids if item]
    with _LOCK:
        record = _RELEASES.get(key)
        if record is None or not project_dir:
            return
        record["projects"][str(project_dir)] = sorted(set(ids))


def take_lease(key: Any, project_dir: str, job_id: str) -> bool:
    """Lease ``job_id`` to a trusted run. Call before the claim's save.

    A retried claim (version conflict) re-leases under the same key, replacing the
    lease for the job it gave up rather than adding to it.
    """
    key = _key(key)
    if not job_id or not project_dir:
        return False
    with _LOCK:
        record = _RELEASES.get(key)
        if record is None:
            return False
        record["leases"] = [
            lease for lease in record["leases"] if lease["project_dir"] != str(project_dir)
        ]
        record["leases"].append({"project_dir": str(project_dir), "job_id": str(job_id)})
    return True


def settle_lease(project_dir: str, job_id: str) -> bool:
    """Drop the lease on a job a save node just completed or failed in memory.

    Release then has nothing to reset, so a successful run never reloads the
    project. If that save node's own project save later fails, the job stays
    ``running`` until the next consume resets it, as it did before leases existed.
    """
    project_dir, job_id = str(project_dir or ""), str(job_id or "")
    dropped = False
    with _LOCK:
        for record in _RELEASES.values():
            kept = [lease for lease in record["leases"]
                    if not (lease["project_dir"] == project_dir and lease["job_id"] == job_id)]
            dropped = dropped or len(kept) != len(record["leases"])
            record["leases"] = kept
    return dropped


def close_release(key: Any) -> dict[str, Any] | None:
    """Close and return the record, with its leases and projects, or None."""
    key = _key(key)
    with _LOCK:
        record = _RELEASES.pop(key, None)
        if record is None:
            return None
        # First results can hold the run's frames; nothing reads them after close.
        record.pop("results", None)
        record.pop("prompt_ref", None)
        if record.get("scope") is _SCOPE_UNSET:
            record["scope"] = None
        _KEY_BY_PROMPT_OBJECT.pop(record.get("prompt_object_id"), None)
        for token in (record.get("token"), record.get("hinted_token")):
            if token:
                _TOKENS.pop(token, None)
        now = time.time()
        if record.get("prompt_id"):
            _RELEASE_STATE_BY_PROMPT[record["prompt_id"]] = (RELEASE_CLOSED, now)
        _prune_release_states_locked(now)
    return record


def first_result(key: Any, node_id: Any) -> tuple[bool, Any]:
    """``(True, value)`` when this open run already recorded ``node_id``'s result."""
    key, node_id = _key(key), _key(node_id)
    with _LOCK:
        record = _RELEASES.get(key)
        results = record.get("results") if record is not None else None
        if not node_id or results is None or node_id not in results:
            return False, None
        return True, results[node_id]


def remember_result(key: Any, node_id: Any, value: Any) -> bool:
    """Record ``node_id``'s first result on its open run. A later value never replaces it."""
    key, node_id = _key(key), _key(node_id)
    with _LOCK:
        record = _RELEASES.get(key)
        results = record.get("results") if record is not None else None
        if not node_id or results is None:
            return False
        results.setdefault(node_id, value)
        return True


def _clean_roots(roots: Any) -> tuple[str, ...] | None:
    if not isinstance(roots, (list, tuple, set, frozenset)):
        return None
    return tuple(sorted({str(item) for item in roots if isinstance(item, (str, int)) and not isinstance(item, bool)}))


def execution_scope(key: Any, compute: Callable[[dict, tuple], Any]) -> frozenset | None:
    """The open run's execution scope, computed once by ``compute(prompt, roots)``.

    None means unknown: no roots, a closed record, or ``compute`` declined. Callers
    treat unknown as the whole prompt. ``compute`` runs outside the lock (it walks
    the prompt); a racing second computation of the same inputs is harmless.
    """
    key = _key(key)
    with _LOCK:
        record = _RELEASES.get(key)
        if record is None:
            return None
        bound = _BOUND.get(record.get("prompt_id") or "")
        if record["scope"] is _SCOPE_UNSET and bound is not None and bound["scope"] is not _SCOPE_UNSET:
            record["scope"] = bound["scope"]  # one memo per prompt
        if record["scope"] is not _SCOPE_UNSET:
            return record["scope"]
        prompt, roots = record.get("prompt_ref"), record.get("roots")
    scope = _compute_scope(prompt, roots, compute, key)
    with _LOCK:
        record = _RELEASES.get(key)
        if record is not None and record["scope"] is _SCOPE_UNSET:
            record["scope"] = scope
        result = record["scope"] if record is not None else scope
        bound = _BOUND.get(record.get("prompt_id") or "") if record is not None else None
        if bound is not None and bound["scope"] is _SCOPE_UNSET:
            bound["scope"] = result
        return result


def note_saved_to_project(key: Any, committed_modified_at: str = "") -> None:
    """A save node of this open run committed its output to the project."""
    with _LOCK:
        record = _RELEASES.get(_key(key))
        if record is not None:
            record["saved_to_project"] = True
            if committed_modified_at:
                record["committed_modified_at"] = str(committed_modified_at)


def record_snapshot(key: Any) -> dict[str, Any] | None:
    """A copy of the open record's plain fields (no prompt, no results)."""
    with _LOCK:
        record = _RELEASES.get(_key(key))
        if record is None:
            return None
        return {name: value for name, value in record.items()
                if name not in {"prompt_ref", "results", "leases", "projects"}
                and value is not _SCOPE_UNSET}


def run_token_of(record: dict[str, Any]) -> str:
    """The token a closed record reports: the claimed one, else the queue-time hint."""
    return str(record.get("token") or record.get("hinted_token") or "")


def _prune_release_states_locked(now: float) -> None:
    for prompt_id, (state, when) in list(_RELEASE_STATE_BY_PROMPT.items()):
        if state == RELEASE_CLOSED and now - when > CLOSED_RELEASE_RETENTION_SEC:
            del _RELEASE_STATE_BY_PROMPT[prompt_id]


def release_state_for_prompt(prompt_id: Any) -> str:
    """``open`` while ComfyUI runs or finalizes the prompt, ``closed`` after, else ``unknown``."""
    with _LOCK:
        entry = _RELEASE_STATE_BY_PROMPT.get(str(prompt_id or ""))
        return entry[0] if entry else RELEASE_UNKNOWN


def open_release_count() -> int:
    with _LOCK:
        return len(_RELEASES)


# --- run_finished listeners ----------------------------------------------------

def add_run_finished_listener(listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
    """Call ``listener(payload)`` once per closed run. Returns a remover.

    Listeners run on ComfyUI's prompt-worker thread, inside ``task_done``'s
    ``finally``: they must only hand the payload to their own thread or loop.
    """
    with _LOCK:
        _RUN_FINISHED_LISTENERS.append(listener)

    def remove() -> None:
        with _LOCK:
            if listener in _RUN_FINISHED_LISTENERS:
                _RUN_FINISHED_LISTENERS.remove(listener)

    return remove


def notify_run_finished(payload: dict[str, Any]) -> None:
    with _LOCK:
        listeners = list(_RUN_FINISHED_LISTENERS)
    for listener in listeners:
        try:
            listener(dict(payload))
        except Exception:
            logger.exception("run_finished listener failed")


# --- Run tokens ----------------------------------------------------------------

def _expire_tokens_locked(now: float) -> None:
    for token, entry in list(_TOKENS.items()):
        if entry["state"] == "registered" and now - entry["registered_at"] > REGISTERED_TOKEN_TTL_SEC:
            del _TOKENS[token]


def register_token(token: str, project_ids, **details: Any) -> None:
    """Register an SDK run before it is submitted."""
    token = str(token or "").strip()
    if not token:
        raise ValueError("run token must be a non-empty string")
    with _LOCK:
        _expire_tokens_locked(time.time())
        _TOKENS[token] = {
            "project_ids": sorted({str(item) for item in project_ids if item}),
            "state": "registered",
            "job_id": "",
            "key": "",
            "registered_at": time.time(),
            "seq": next(_SEQ),
            "observed_prompt_id": "",
            **details,
        }


def claim_token(token: str, project_ids, key: Any = "") -> str:
    """Consume a token at its first execute. Returns one of the TOKEN_* verdicts."""
    token = str(token or "").strip()
    wanted = {str(item) for item in project_ids if item}
    with _LOCK:
        _expire_tokens_locked(time.time())
        entry = _TOKENS.get(token)
        if entry is None:
            return TOKEN_UNKNOWN
        if entry["state"] != "registered":
            return TOKEN_USED
        if not wanted & set(entry["project_ids"]):
            return TOKEN_MISMATCH
        entry["state"] = "executing"
        entry["key"] = _key(key)
        record = _RELEASES.get(entry["key"])
        if record is not None:
            record["token"] = token
    return TOKEN_OK


def note_token_job(token: str, job_id: str) -> None:
    """Record the job an SDK run's editor claimed, on the token and on its release.

    The release keeps it because a save node that completes the job settles (drops)
    the lease, so ``run_finished`` could otherwise not say which job the run took.
    """
    with _LOCK:
        entry = _TOKENS.get(str(token or "").strip())
        if entry is not None:
            entry["job_id"] = str(job_id or "")
            record = _RELEASES.get(entry.get("key", ""))
            if record is not None:
                record["claimed_job_id"] = str(job_id or "")


def token_state(token: str) -> dict[str, Any] | None:
    with _LOCK:
        entry = _TOKENS.get(str(token or "").strip())
        return dict(entry) if entry is not None else None


def forget_token(token: str) -> None:
    with _LOCK:
        _TOKENS.pop(str(token or "").strip(), None)


# --- Registrations and bound runs -------------------------------------------------
#
# A registration says who owns a run before ComfyUI queues it. The queue's ``put``
# hook binds the registrations a prompt carries to the id it was queued under,
# creating that prompt's *bound run*: its owners, its order (``seq``) and its
# lifecycle, queued → started → finished (or removed from the queue unrun). This
# is the one record of a registered run; ``run_outputs`` keeps only what was
# observed for it, keyed by the same prompt id, and drops it with the run.

REGISTER_OK = "ok"
BOUND_QUEUED, BOUND_STARTED, BOUND_FINISHED = "queued", "started", "finished"
OUTCOME_REMOVED = "removed"
MAX_FINISHED_BOUND = 256
MAX_UNFINISHED_BOUND = 512
MAX_BOUND_TOMBSTONES = 512
PROVISIONAL_TTL_SEC = RECEIPT_UNBOUND_TTL_SEC
REMOVED_GRACE_SEC = 5.0

_BOUND: "collections.OrderedDict[str, dict[str, Any]]" = collections.OrderedDict()
_BOUND_TOMBSTONES: "collections.deque[dict[str, Any]]" = collections.deque(maxlen=MAX_BOUND_TOMBSTONES)


def is_receipt(value: Any) -> bool:
    return isinstance(value, str) and bool(_RECEIPT_RE.match(value))


def _expire_receipts_locked(now: float) -> None:
    for receipt, entry in list(_RECEIPTS.items()):
        age = now - entry["registered_at"]
        if (not entry["prompt_id"] and age > RECEIPT_UNBOUND_TTL_SEC) or age > RECEIPT_BOUND_RETENTION_SEC:
            del _RECEIPTS[receipt]
    for receipt, parked in list(_PARKED.items()):
        if now - parked["at"] > RECEIPT_UNBOUND_TTL_SEC:
            del _PARKED[receipt]


def _add_unique(target: list, items) -> None:
    for item in items or ():
        if item and item not in target:
            target.append(item)


def _adopt_locked(bound: dict[str, Any], owner: tuple, editors=(), purpose: str = "", project_ids=()) -> None:
    if owner not in bound["owners"]:
        bound["owners"].append(owner)
    bound["provisional_until"] = 0.0
    _add_unique(bound["editors"], editors)
    _add_unique(bound["project_ids"], [str(item) for item in project_ids or ()])
    if purpose == "branch":
        bound["purpose"] = "branch"


def register_receipt(receipt: str, owner: tuple, editors=(), purpose: str = "run") -> tuple[str, str]:
    """Register a canvas run's receipt. Returns ``(code, prompt_id)``.

    ``prompt_id`` is set when ComfyUI already queued the prompt (``put`` came
    first): its bound run was observed unowned until now and takes this owner.
    """
    if not is_receipt(receipt):
        return "invalid_receipt", ""
    if not (isinstance(owner, tuple) and owner and all(isinstance(part, str) and part for part in owner)):
        return "invalid_owner", ""
    now = time.time()
    editors = [str(item) for item in editors][:16]
    purpose = "branch" if purpose == "branch" else "run"
    project_ids = [owner[1]] if owner[0] == "host" and len(owner) > 1 else []
    with _LOCK:
        _expire_receipts_locked(now)
        if receipt in _RECEIPTS:
            return "duplicate", ""
        parked = _PARKED.pop(receipt, None)
        if parked is None:
            unbound = [entry for entry in _RECEIPTS.values() if not entry["prompt_id"]]
            if len(unbound) >= MAX_UNBOUND_TOTAL or sum(1 for entry in unbound if entry["owner"] == owner) >= MAX_UNBOUND_PER_OWNER:
                return "too_many", ""
        _RECEIPTS[receipt] = {
            "owner": owner, "editors": editors, "purpose": purpose, "project_ids": project_ids,
            "registered_at": now,
            # A prompt queued before its registration keeps the order it was queued in.
            "seq": parked["seq"] if parked else next(_SEQ),
            "prompt_id": parked["prompt_id"] if parked else "",
        }
        if parked is None:
            return REGISTER_OK, ""
        bound = _BOUND.get(parked["prompt_id"])
        if bound is not None:
            _adopt_locked(bound, owner, editors, purpose, project_ids)
        return REGISTER_OK, parked["prompt_id"]


def bind_queued(prompt_id: Any, prompt: Any, roots: Any = None, workflow_id: str = "", client_id: str = "") -> dict[str, Any] | None:
    """Bind the registrations a prompt carries to the id ComfyUI queued it under.

    Called from the queue's ``put`` hook, before ComfyUI can start it, so a run
    that finishes before its submitter hears back is still owned. Each
    registration binds once: a re-queued prompt carrying a used receipt or token
    is not observed again, and a prompt id already bound (ComfyUI accepts a
    client-chosen id without checking it is new) is refused rather than merged
    into another run. Cheap: one pass over the Editor nodes, no hashing.
    Returns the bound run's public fields, or None when nothing bound.
    """
    prompt_id = _key(prompt_id)
    if not prompt_id or not isinstance(prompt, dict):
        return None
    receipts, tokens = [], []
    for node in prompt.values():
        if not isinstance(node, dict) or node.get("class_type") != EDITOR_CLASS_TYPE:
            continue
        meta = node.get("_meta")
        if not isinstance(meta, dict):
            continue
        receipt, token = meta.get("sonder_receipt"), meta.get("sonder_run")
        if is_receipt(receipt) and receipt not in receipts:
            receipts.append(receipt)
        if isinstance(token, str) and token.strip() and token.strip() not in tokens:
            tokens.append(token.strip())
    if not receipts and not tokens:
        return None
    now = time.time()
    with _LOCK:
        _expire_receipts_locked(now)
        if prompt_id in _BOUND:
            logger.warning("Prompt id %s is already bound to a run; this prompt is not observed", prompt_id)
            return None
        bound = {
            "prompt_id": prompt_id, "seq": 0, "owners": [], "editors": [], "purpose": "run",
            "workflow_id": str(workflow_id or ""), "client_id": str(client_id or ""), "project_ids": [],
            "roots": _clean_roots(roots), "prompt_ref": prompt, "scope": _SCOPE_UNSET, "prompt_keys": None,
            "state": BOUND_QUEUED, "outcome": "", "opened_at": now, "provisional_until": 0.0,
            "finished_at": 0.0, "graph_rev": "", "saved_to_project": False, "committed_modified_at": "",
            "cancel_requested": prompt_id in _CANCELS,
        }
        seqs = []
        for token in tokens:
            entry = _TOKENS.get(token)
            if entry is None or entry.get("observed_prompt_id") or not entry.get("run_id"):
                continue
            entry["observed_prompt_id"] = prompt_id
            seqs.append(entry["seq"])
            # The Sonder App's workflow is its template.
            bound["workflow_id"] = str(entry.get("template_id") or bound["workflow_id"])
            _adopt_locked(bound, ("sdk", str(entry["run_id"])), (), entry.get("purpose") or "run",
                          entry.get("project_ids") or ())
        parked_any = False
        for receipt in receipts:
            entry = _RECEIPTS.get(receipt)
            if entry is None:
                if receipt not in _PARKED and len(_PARKED) < MAX_PARKED:
                    _PARKED[receipt] = {"prompt_id": prompt_id, "at": now, "seq": next(_SEQ)}
                    seqs.append(_PARKED[receipt]["seq"])
                    parked_any = True
                continue
            if entry["prompt_id"]:
                logger.info("Run receipt already used by %s; prompt %s is not observed", entry["prompt_id"], prompt_id)
                continue
            entry["prompt_id"] = prompt_id
            seqs.append(entry["seq"])
            _adopt_locked(bound, entry["owner"], entry["editors"], entry["purpose"], entry["project_ids"])
        if not bound["owners"] and not parked_any:
            return None
        # Two Editors of one prompt can register twice; the earlier registration orders it.
        bound["seq"] = min(seqs)
        if not bound["owners"]:
            # Observed from now on, owned by nobody until its registration arrives:
            # a fast run must not finish unseen.
            bound["provisional_until"] = now + PROVISIONAL_TTL_SEC
        _BOUND[prompt_id] = bound
        _evict_bound_locked(now)
        return _bound_public(bound)


def receipt_state(receipt: str) -> dict[str, Any] | None:
    with _LOCK:
        entry = _RECEIPTS.get(str(receipt or ""))
        return dict(entry) if entry is not None else None


def _bound_public(bound: dict[str, Any]) -> dict[str, Any]:
    return {
        "prompt_id": bound["prompt_id"], "seq": bound["seq"], "owners": list(bound["owners"]),
        "editors": list(bound["editors"]), "purpose": bound["purpose"], "workflow_id": bound["workflow_id"],
        "client_id": bound["client_id"], "project_ids": list(bound["project_ids"]), "state": bound["state"],
        "outcome": bound["outcome"], "finished": bound["state"] == BOUND_FINISHED,
        "scope": None if bound["scope"] is _SCOPE_UNSET else bound["scope"],
        "scope_known": bound["scope"] is not _SCOPE_UNSET, "prompt_keys": bound["prompt_keys"],
        "graph_rev": bound["graph_rev"], "saved_to_project": bound["saved_to_project"],
        "committed_modified_at": bound["committed_modified_at"], "cancel_requested": bound["cancel_requested"],
    }


def bound_run(prompt_id: Any) -> dict[str, Any] | None:
    """The bound run's public fields (no prompt), or None."""
    with _LOCK:
        bound = _BOUND.get(_key(prompt_id))
        return _bound_public(bound) if bound is not None else None


def is_observing(prompt_id: Any) -> bool:
    """Whether a bound run is still queued or running (its events are recorded)."""
    with _LOCK:
        bound = _BOUND.get(_key(prompt_id))
        return bound is not None and bound["state"] != BOUND_FINISHED


def bound_ids() -> set[str]:
    with _LOCK:
        return set(_BOUND)


def bound_prompt(prompt_id: Any) -> dict | None:
    """The queued prompt of an unfinished bound run, for read-only walks; None after."""
    with _LOCK:
        bound = _BOUND.get(_key(prompt_id))
        return bound["prompt_ref"] if bound is not None else None


def _release_for_prompt_locked(prompt_id: str) -> dict[str, Any] | None:
    for record in _RELEASES.values():
        if record.get("prompt_id") == prompt_id:
            return record
    return None


def bound_scope(prompt_id: Any, compute: Callable[[dict, tuple], Any]) -> tuple[frozenset | None, frozenset | None]:
    """``(scope, prompt_keys)`` of a bound run; ``prompt_keys`` only when scope is unknown.

    One memo with the open release record of the same prompt. ``compute`` runs
    outside the lock.
    """
    prompt_id = _key(prompt_id)
    with _LOCK:
        bound = _BOUND.get(prompt_id)
        if bound is None:
            return None, None
        if bound["scope"] is _SCOPE_UNSET:
            record = _release_for_prompt_locked(prompt_id)
            if record is not None and record["scope"] is not _SCOPE_UNSET:
                bound["scope"] = record["scope"]
        if bound["scope"] is not _SCOPE_UNSET:
            return bound["scope"], _prompt_keys_locked(bound)
        prompt, roots = bound["prompt_ref"], bound["roots"]
    scope = _compute_scope(prompt, roots, compute, prompt_id)
    with _LOCK:
        bound = _BOUND.get(prompt_id)
        if bound is None:
            return scope, None
        if bound["scope"] is _SCOPE_UNSET:
            bound["scope"] = scope
        return bound["scope"], _prompt_keys_locked(bound)


def _prompt_keys_locked(bound: dict[str, Any]) -> frozenset | None:
    if bound["scope"] is not None:
        return None
    if bound["prompt_keys"] is None and isinstance(bound["prompt_ref"], dict):
        # Unknown scope is the whole prompt, never more.
        bound["prompt_keys"] = frozenset(str(key) for key in bound["prompt_ref"])
    return bound["prompt_keys"]


def _compute_scope(prompt: Any, roots: Any, compute: Callable[[dict, tuple], Any], label: str) -> frozenset | None:
    if not isinstance(prompt, dict) or roots is None:
        return None
    try:
        value = compute(prompt, roots)
        return frozenset(value) if value is not None else None
    except Exception:
        logger.exception("Execution scope for run %s could not be computed", label)
        return None


def mark_bound_started(prompt_id: Any) -> None:
    with _LOCK:
        bound = _BOUND.get(_key(prompt_id))
        if bound is not None and bound["state"] == BOUND_QUEUED:
            bound["state"] = BOUND_STARTED


def _end_bound_locked(bound: dict[str, Any], outcome: str) -> None:
    _prompt_keys_locked(bound)
    if bound["scope"] is _SCOPE_UNSET:
        bound["scope"] = None
        _prompt_keys_locked(bound)
    bound["state"] = BOUND_FINISHED
    bound["outcome"] = outcome
    bound["finished_at"] = time.time()
    bound["prompt_ref"] = None


def finish_bound(prompt_id: Any, outcome: str, graph_rev: str = "", saved_to_project: bool = False,
                 committed_modified_at: str = "") -> dict[str, Any] | None:
    """End a bound run at ``task_done``. Returns its public fields, or None."""
    with _LOCK:
        bound = _BOUND.get(_key(prompt_id))
        if bound is None or bound["state"] == BOUND_FINISHED:
            return None
        bound["graph_rev"] = str(graph_rev or "")
        bound["saved_to_project"] = bool(saved_to_project)
        bound["committed_modified_at"] = str(committed_modified_at or "")
        _end_bound_locked(bound, outcome or "success")
        public = _bound_public(bound)
        _evict_bound_locked(time.time())
        return public


def reap_removed_bound(present: set[str], grace: float | None = None) -> list[dict[str, Any]]:
    """End bound runs a user deleted or cleared from ComfyUI's queue before they started.

    Nothing else would: such a prompt never reaches ``get`` or ``task_done``.
    ``present`` is every prompt id ComfyUI is running or holding. The grace covers
    ``put`` binding just before the item lands in the queue; a prompt ComfyUI took
    has a release state and is left alone. Returns the ended runs.
    """
    cutoff = time.time() - (REMOVED_GRACE_SEC if grace is None else grace)
    ended = []
    with _LOCK:
        for prompt_id, bound in list(_BOUND.items()):
            if bound["state"] != BOUND_QUEUED or bound["opened_at"] >= cutoff or prompt_id in present:
                continue
            if prompt_id in _RELEASE_STATE_BY_PROMPT:
                continue
            _end_bound_locked(bound, OUTCOME_REMOVED)
            ended.append(_bound_public(bound))
        if ended:
            _evict_bound_locked(time.time())
    return ended


def visible_bound(owners, workflow_id: str = "") -> list[dict[str, Any]]:
    """The owners' bound runs, newest registration first.

    With a workflow, only that workflow's runs: a run that names none matches
    only a read that names none.
    """
    owners = {owner for owner in owners or () if isinstance(owner, tuple)}
    workflow_id = str(workflow_id or "")
    with _LOCK:
        found = [_bound_public(bound) for bound in _BOUND.values()
                 if any(owner in owners for owner in bound["owners"])
                 and (not workflow_id or bound["workflow_id"] == workflow_id)]
    return sorted(found, key=lambda bound: bound["seq"], reverse=True)


def bound_tombstone(owners, prompt_id: Any, workflow_id: str = "") -> dict[str, Any] | None:
    """An evicted run's identity, so its outputs can be re-read from ComfyUI history."""
    owners = {owner for owner in owners or () if isinstance(owner, tuple)}
    prompt_id, workflow_id = _key(prompt_id), str(workflow_id or "")
    with _LOCK:
        for stone in reversed(_BOUND_TOMBSTONES):
            if (stone["prompt_id"] == prompt_id and any(owner in owners for owner in stone["owners"])
                    and (not workflow_id or stone["workflow_id"] == workflow_id)):
                return dict(stone)
    return None


def _evict_bound_locked(now: float) -> None:
    # An unowned run whose registration never came.
    for prompt_id, bound in list(_BOUND.items()):
        if bound["provisional_until"] and now > bound["provisional_until"] and not bound["owners"]:
            del _BOUND[prompt_id]
    # Unfinished runs are bounded too: past the cap, the oldest never-started ones end.
    unfinished = [bound for bound in _BOUND.values() if bound["state"] == BOUND_QUEUED]
    excess = sum(1 for bound in _BOUND.values() if bound["state"] != BOUND_FINISHED) - MAX_UNFINISHED_BOUND
    for bound in unfinished[:max(0, excess)]:
        _end_bound_locked(bound, OUTCOME_REMOVED)
    # Finished runs: past the cap, the oldest run of whichever owner holds the most
    # goes first, so one busy owner cannot evict everyone else's latest result.
    finished = [bound for bound in _BOUND.values() if bound["state"] == BOUND_FINISHED]
    excess = len(finished) - MAX_FINISHED_BOUND
    if excess <= 0:
        return
    by_owner: dict[Any, list[dict[str, Any]]] = {}
    for bound in finished:
        by_owner.setdefault(bound["owners"][0] if bound["owners"] else None, []).append(bound)
    for _ in range(excess):
        runs = max(by_owner.values(), key=len)
        bound = runs.pop(0)
        del _BOUND[bound["prompt_id"]]
        _BOUND_TOMBSTONES.append({
            "prompt_id": bound["prompt_id"], "seq": bound["seq"], "owners": list(bound["owners"]),
            "workflow_id": bound["workflow_id"], "scope": bound["scope"], "prompt_keys": bound["prompt_keys"],
            "outcome": bound["outcome"],
        })


# --- Cancel flags ---------------------------------------------------------------------
#
# A cancel names a prompt id that may not be queued yet (a client-chosen id still
# in validation) or may have just been taken off the queue. The flag outlives
# both windows; ``run_control`` acts on it at ``put`` and at ``execution_start``.

CANCEL_FLAG_TTL_SEC = 30 * 60
MAX_CANCEL_FLAGS = 1024
_CANCELS: "collections.OrderedDict[str, float]" = collections.OrderedDict()


def request_cancel(prompt_id: Any) -> None:
    prompt_id = _key(prompt_id)
    if not prompt_id:
        return
    now = time.time()
    with _LOCK:
        _CANCELS.pop(prompt_id, None)
        _CANCELS[prompt_id] = now
        while len(_CANCELS) > MAX_CANCEL_FLAGS or (_CANCELS and now - next(iter(_CANCELS.values())) > CANCEL_FLAG_TTL_SEC):
            _CANCELS.popitem(last=False)
        bound = _BOUND.get(prompt_id)
        if bound is not None:
            bound["cancel_requested"] = True


def cancel_requested(prompt_id: Any) -> bool:
    with _LOCK:
        return _key(prompt_id) in _CANCELS


def reset_for_tests() -> None:
    with _LOCK:
        _CANCELS.clear()
        _RELEASES.clear()
        _KEY_BY_PROMPT_OBJECT.clear()
        _TOKENS.clear()
        _RELEASE_STATE_BY_PROMPT.clear()
        _RUN_FINISHED_LISTENERS.clear()
        _RECEIPTS.clear()
        _PARKED.clear()
        _BOUND.clear()
        _BOUND_TOMBSTONES.clear()
