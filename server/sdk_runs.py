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

Stdlib only and thread-safe: the hook runs on ComfyUI's prompt-worker thread and
routes run on the event loop. Everything here is ephemeral by design; ComfyUI's
queue does not survive a restart either.
"""

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_RELEASES: dict[str, dict[str, Any]] = {}
_KEY_BY_PROMPT_OBJECT: dict[int, str] = {}
_TOKENS: dict[str, dict[str, Any]] = {}
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


def open_release(key: Any, prompt_id: Any = "", prompt: Any = None) -> bool:
    """Open the record for a prompt ComfyUI just took off its queue.

    A registered run token in the prompt tells the record its project up front, so
    the run is announced even when it fails before the editor executes.
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


def reset_for_tests() -> None:
    with _LOCK:
        _RELEASES.clear()
        _KEY_BY_PROMPT_OBJECT.clear()
        _TOKENS.clear()
        _RELEASE_STATE_BY_PROMPT.clear()
        _RUN_FINISHED_LISTENERS.clear()
