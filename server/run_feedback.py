"""Run feedback: what a bound run is doing, for one updating notification per run.

Live-workflow Phase 3. A projection with no lifecycle of its own, keyed by prompt
id and kept only while ``sdk_runs`` keeps the bound run (pruned with
``sdk_runs.bound_ids()``). It owns only what nothing else holds:

- the stage the working node *declared* (``note_stage``: the Editor's rendering
  and preparing_inputs, Save Video's encoding and saving), never inferred from
  which node happens to send progress;
- the latest native count of the running node (``sdk_progress`` listener);
- when the run was queued and started;
- the window and the queued job the Editor noted before it rendered;
- the Modes rows captured at submission by each host's own Controls authority;
- the terminal record from the run's release, kept for late joiners until prune.

Lifecycle, owners and project ids are read from ``sdk_runs``; the outcome comes
from the release's one ``_run_outcome`` call. Sonder App runs keep their state
and outcome in ``SdkRunner``; for those this module supplies stage, count and
receipt only (the App joins them by prompt id).

Delivery is the ``run_progress`` project event: at most ``PERIOD_SEC`` apart per
prompt, a trailing update built when it fires, every state change forced. ``seq``
orders a prompt's records; hosts drop a lower one. Receipt fields travel only when
they change (``receipt_rev``) and in reads; exception detail is never broadcast.

Locking: this module never calls another module while holding its own lock, and
it is called only after ``sdk_runs`` calls have returned (never from inside them).
"""

from __future__ import annotations

import collections
import logging
import threading
import time
import uuid
from typing import Any, Callable

from . import sdk_progress, sdk_runs

logger = logging.getLogger(__name__)

PERIOD_SEC = 0.25
REAP_INTERVAL_SEC = 5.0
PENDING_MODES_TTL_SEC = 120.0
MAX_PENDING_MODES = 256
MAX_RECORDS = 1024
# A process identity: a host holding a run from another epoch knows the server restarted.
EPOCH = uuid.uuid4().hex[:12]
TOKEN_PREFIX = "run:"  # Expiry: retire when sdk_progress keys watches by prompt id alone.

STAGES = frozenset({"rendering", "preparing_inputs", "encoding", "saving"})
# Stages that show their node's native count; the others never show one (the
# inline proxy reports a packet count under ``saving``).
COUNTED_STAGES = frozenset({"rendering", "encoding"})
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "removed"})
_OUTCOME_STATE = {"success": "completed", "error": "failed", "interrupted": "cancelled",
                  sdk_runs.OUTCOME_REMOVED: "removed"}

_LOCK = threading.RLock()
_DATA: "collections.OrderedDict[str, dict[str, Any]]" = collections.OrderedDict()
_PENDING_MODES: "collections.OrderedDict[str, tuple[list, float]]" = collections.OrderedDict()
_REAP = {"timer": None}
_GENERATION = [0]  # bumped by reset: a trailing update scheduled before it is dropped
_HOOKS: dict[str, Callable | None] = {"emit": None, "queued_ids": None}


def _emit_event(alias: str, event: dict) -> None:
    hook = _HOOKS["emit"]
    if hook is not None:
        hook(alias, event)
        return
    from .session_registry import schedule_project_event
    schedule_project_event(alias, event)


def _new(prompt_id: str) -> dict[str, Any]:
    now = time.time()
    return {
        "prompt_id": prompt_id, "seq": 0, "receipt_rev": 1, "sent_receipt_rev": 0,
        "state": "queued", "queued_at": now, "started_at": 0.0,
        "stage": "", "stage_node": "", "node": "", "display": "", "count": None,
        "queued_receipt": [], "windows": {}, "jobs": {}, "modes": None,
        "aliases": [], "terminal": None, "terminal_sent": False,
        "last_sent": 0.0, "trailing": False,
    }


# --- Bind, registration, start ---------------------------------------------------

def _editor_requests(prompt: Any) -> tuple[list, list]:
    """Each Editor's requested range from the bound prompt (literals only), and its
    registration keys (receipts and run tokens)."""
    requests, keys = [], []
    if not isinstance(prompt, dict):
        return requests, keys
    for node_id, node in prompt.items():
        if not isinstance(node, dict) or node.get("class_type") != sdk_runs.EDITOR_CLASS_TYPE:
            continue
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
        for key in (meta.get("sonder_receipt"), meta.get("sonder_run")):
            if isinstance(key, str) and key.strip():
                keys.append(key.strip())

        def literal(name, kind):
            value = inputs.get(name)
            return value if isinstance(value, kind) and not isinstance(value, bool) else None

        queue_flag = inputs.get("render_queue_active", True)
        requests.append({
            "editor": str(node_id),
            "scene_id": literal("scene_id", str),
            "selection_start": literal("selection_start", int),
            "selection_end": literal("selection_end", int),
            # A linked input is set by the graph; the receipt says so rather than guess.
            "linked": any(isinstance(inputs.get(name), list) for name in ("scene_id", "selection_start", "selection_end")),
            "render_queue_active": queue_flag if isinstance(queue_flag, bool) else None,
        })
    return requests, keys


def on_bound(prompt_id: Any, prompt: Any) -> None:
    """The ``put`` hook bound a prompt (owned or provisional): start watching it."""
    prompt_id = str(prompt_id or "")
    if not prompt_id or not sdk_runs.is_bound(prompt_id):
        return
    requests, keys = _editor_requests(prompt)
    now = time.time()
    with _LOCK:
        data = _DATA.get(prompt_id)
        if data is None:
            data = _DATA[prompt_id] = _new(prompt_id)
        data["queued_receipt"] = requests
        for key in keys:
            pending = _PENDING_MODES.pop(key, None)
            if pending is not None and now - pending[1] <= PENDING_MODES_TTL_SEC:
                data["modes"] = pending[0]
        evicted = []
        while len(_DATA) > MAX_RECORDS:
            evicted.append(_DATA.popitem(last=False)[0])
    for old in evicted:
        sdk_progress.unwatch(old, TOKEN_PREFIX + old)
    sdk_progress.watch(prompt_id, TOKEN_PREFIX + prompt_id)
    _publish(prompt_id, force=True)
    _ensure_reap_timer()


def on_registered(prompt_id: Any) -> None:
    """A registration adopted an already bound run: its host may have missed
    everything so far, terminal included. Send the full record now."""
    prompt_id = str(prompt_id or "")
    if prompt_id:
        _publish(prompt_id, force=True, full=True)


def on_started(prompt_id: Any) -> None:
    """ComfyUI took the prompt off its queue (``get`` hook; needs no ``send_sync`` wrap)."""
    _transition(prompt_id, "running", started=True)


def observe(event: str, data: Any) -> None:
    """``send_sync`` observer: the run is finishing once execution ended."""
    if event in ("execution_success", "execution_error", "execution_interrupted") and isinstance(data, dict):
        _transition(data.get("prompt_id"), "finishing")


def on_task_done(prompt_id: Any) -> None:
    """ComfyUI finished the prompt (``task_done`` hook, before bridge finalization
    and the release): finishing, also when the ``send_sync`` wrap is missing."""
    _transition(prompt_id, "finishing")


def _transition(prompt_id: Any, state: str, started: bool = False) -> None:
    prompt_id = str(prompt_id or "")
    with _LOCK:
        data = _DATA.get(prompt_id)
        if data is None or data["state"] in TERMINAL_STATES or data["state"] == state:
            return
        if state == "running" and data["state"] == "finishing":
            return
        data["state"] = state
        if started and not data["started_at"]:
            data["started_at"] = time.time()
        if state == "finishing":
            data["stage"], data["stage_node"], data["count"] = "", "", None
    _publish(prompt_id, force=True)


# --- Notes from the nodes doing the work ------------------------------------------

def _prompt_id_for(prompt: Any) -> str:
    key = sdk_runs.trusted_key_for_prompt(prompt)
    record = sdk_runs.record_snapshot(key) if key else None
    prompt_id = str((record or {}).get("prompt_id") or "")
    with _LOCK:
        return prompt_id if prompt_id in _DATA else ""


def note_stage(prompt: Any, node_id: Any, stage: str) -> None:
    """The executing node says which stage it is in. A no-op for unobserved prompts."""
    if stage not in STAGES:
        return
    prompt_id = _prompt_id_for(prompt)
    if not prompt_id:
        return
    with _LOCK:
        data = _DATA.get(prompt_id)
        if data is None or data["state"] in TERMINAL_STATES:
            return
        data["stage"], data["stage_node"] = stage, str(node_id or "")
        if data["state"] == "queued":
            data["state"] = "running"
            data["started_at"] = data["started_at"] or time.time()
        if stage not in COUNTED_STAGES:
            data["count"] = None
    _publish(prompt_id, force=True)


def note_window(prompt: Any, node_id: Any, window: dict, aliases=()) -> None:
    """The Editor's executed window and queue mode, before it renders."""
    prompt_id = _prompt_id_for(prompt)
    if not prompt_id or not isinstance(window, dict):
        return
    keep = ("scene_id", "scene_name", "generation_start", "generation_end", "render_start", "render_end",
            "frame_count", "frame_count_padding", "source_frame_count", "fps", "queue")
    with _LOCK:
        data = _DATA.get(prompt_id)
        if data is None:
            return
        data["windows"][str(node_id or "")] = {key: window.get(key) for key in keep if key in window}
        for alias in aliases or ():
            if alias and alias not in data["aliases"]:
                data["aliases"].append(str(alias))
        data["receipt_rev"] += 1
    _publish(prompt_id, force=True)


def note_job(prompt: Any, node_id: Any, job: Any, mode: str) -> None:
    """The queued job the Editor claimed (consume) or previewed (peek)."""
    prompt_id = _prompt_id_for(prompt)
    if not prompt_id or job is None:
        return
    entry = {
        "job_id": str(getattr(job, "job_id", "") or ""), "scene_name": str(getattr(job, "scene_name", "") or ""),
        "batch_id": str(getattr(job, "batch_id", "") or ""), "mode": str(mode or ""),
        "batch_index": int(getattr(job, "batch_index", 0) or 0), "batch_total": int(getattr(job, "batch_total", 0) or 0),
        "selection_start": int(getattr(job, "selection_start", 0) or 0),
        "selection_end": int(getattr(job, "selection_end", 0) or 0),
    }
    with _LOCK:
        data = _DATA.get(prompt_id)
        if data is None:
            return
        data["jobs"][str(node_id or "")] = entry
        data["receipt_rev"] += 1
    _publish(prompt_id, force=True)


def note_modes(rows: Any, prompt_id: Any = "", key: Any = "") -> None:
    """Modes rows captured at submission, by prompt id, or by the run's receipt or
    token when the prompt is not bound yet (attached at ``on_bound``)."""
    if not isinstance(rows, list):
        return
    prompt_id, key = str(prompt_id or ""), str(key or "")
    if not prompt_id and key:
        receipt = sdk_runs.receipt_state(key)
        prompt_id = str((receipt or {}).get("prompt_id") or "")
    now = time.time()
    with _LOCK:
        data = _DATA.get(prompt_id) if prompt_id else None
        if data is None:
            if key:
                _PENDING_MODES.pop(key, None)
                _PENDING_MODES[key] = (rows, now)
                for stale in [k for k, (_rows, at) in _PENDING_MODES.items() if now - at > PENDING_MODES_TTL_SEC]:
                    del _PENDING_MODES[stale]
                while len(_PENDING_MODES) > MAX_PENDING_MODES:
                    _PENDING_MODES.popitem(last=False)
            return
        if data["modes"] is not None:
            return  # captured at submission once; never re-derived
        data["modes"] = rows
        data["receipt_rev"] += 1
    _publish(prompt_id, force=True)


def _on_progress(snapshot: dict) -> None:
    """``sdk_progress`` listener (prompt-worker thread): the running node and its count."""
    prompt_id = str(snapshot.get("prompt_id") or "")
    with _LOCK:
        data = _DATA.get(prompt_id)
        if data is None or data["state"] in TERMINAL_STATES:
            return
        node, display = str(snapshot.get("node_id") or ""), str(snapshot.get("display_node_id") or "")
        data["node"], data["display"], data["count"] = node, display, snapshot.get("count")
        started = data["state"] == "queued"
        if started:
            data["state"] = "running"
            data["started_at"] = data["started_at"] or time.time()
    # A node change is throttled too: a graph of many fast nodes must not
    # broadcast once per node. Only the state change is forced.
    _publish(prompt_id, force=started)


# --- Release and removal ------------------------------------------------------------

def _removed_terminal() -> dict:
    return {"outcome": sdk_runs.OUTCOME_REMOVED, "note": "", "node_type": "", "claimed_job_ids": [],
            "returned_job_ids": [], "take_saved": False}


def on_released(prompt_id: Any, outcome: str, note: str = "", claimed_job_ids=(), returned_job_ids=(),
                take_saved: bool = False, aliases=(), node_type: str = "") -> None:
    """The run's release closed: its terminal record, from the release's one outcome.
    ``node_type`` names a failing node's class only, never its exception text."""
    prompt_id = str(prompt_id or "")
    with _LOCK:
        data = _DATA.get(prompt_id)
        if data is None:
            return
        for alias in aliases or ():
            if alias and alias not in data["aliases"]:
                data["aliases"].append(str(alias))
        data["terminal"] = {
            "outcome": str(outcome or ""), "note": str(note or ""), "node_type": str(node_type or "")[:200],
            "claimed_job_ids": [str(item) for item in claimed_job_ids or ()],
            "returned_job_ids": [str(item) for item in returned_job_ids or ()],
            "take_saved": bool(take_saved),
        }
        data["state"] = _OUTCOME_STATE.get(str(outcome or ""), "failed")
        data["stage"], data["stage_node"], data["count"] = "", "", None
    _publish(prompt_id, force=True, full=True)
    sdk_progress.unwatch(prompt_id, TOKEN_PREFIX + prompt_id)


def reconcile() -> None:
    """End records whose bound run ended unrun (deleted, cleared or evicted), and
    drop records ``sdk_runs`` no longer keeps. Any thread; never under a lock.

    Candidates are read before any bound run, and each run is read on its own: a
    prompt a concurrent ``put`` binds after the candidate list is simply not a
    candidate yet, so it can never be taken for one that left (audit 3A #1). A run
    ComfyUI already took (``started_at``) is ended by its release, never here.
    """
    with _LOCK:
        candidates = [pid for pid, data in _DATA.items() if data["state"] not in TERMINAL_STATES]
    stragglers = []
    for prompt_id in candidates:
        bound = sdk_runs.bound_run(prompt_id)
        with _LOCK:
            data = _DATA.get(prompt_id)
            if data is None or data["state"] in TERMINAL_STATES:
                continue
            if data["started_at"]:
                if bound is None:
                    stragglers.append(prompt_id)
                continue
            if bound is not None and not (bound["finished"] and bound["outcome"] == sdk_runs.OUTCOME_REMOVED):
                continue
            # Ended unrun: deleted or cleared from the queue, evicted, or a
            # provisional run whose registration never came (it has no host).
            data["terminal"] = _removed_terminal()
            data["state"] = "removed"
        _publish(prompt_id, force=True, full=True, bound=bound)
        sdk_progress.unwatch(prompt_id, TOKEN_PREFIX + prompt_id)
    if stragglers:
        # Started, then forgotten by sdk_runs without a release reaching us (a
        # failed hook): drop once ComfyUI no longer holds the prompt either.
        present = _queued_ids()
        if present is not None:
            for prompt_id in stragglers:
                if prompt_id in present:
                    continue
                with _LOCK:
                    _DATA.pop(prompt_id, None)
                sdk_progress.unwatch(prompt_id, TOKEN_PREFIX + prompt_id)
    live = sdk_runs.bound_ids()
    with _LOCK:
        gone = [pid for pid, data in _DATA.items() if pid not in live and data["state"] in TERMINAL_STATES]
        for prompt_id in gone:
            del _DATA[prompt_id]


def _queued_ids():
    hook = _HOOKS["queued_ids"]
    if hook is not None:
        return hook()
    from . import run_outputs
    return run_outputs.queued_prompt_ids()


def _reap_tick() -> None:
    with _LOCK:
        _REAP["timer"] = None
    try:
        from . import run_outputs
        run_outputs.reap_removed(_queued_ids())
        reconcile()
    except Exception:
        logger.exception("Run feedback reap failed")
    _ensure_reap_timer()


def _ensure_reap_timer() -> None:
    """While any record waits in the queue, reap every few seconds: a deletion this
    process did not see (another ComfyUI path) still ends its notification."""
    with _LOCK:
        waiting = any(data["state"] == "queued" for data in _DATA.values())
        if not waiting or _REAP["timer"] is not None:
            return
        timer = threading.Timer(REAP_INTERVAL_SEC, _reap_tick)
        timer.daemon = True
        _REAP["timer"] = timer
    timer.start()


# --- Composition and delivery ---------------------------------------------------------

def _class_display_name(class_type: str) -> str:
    """ComfyUI's display name for a node class, else the class itself."""
    try:
        import nodes as comfy_nodes  # ComfyUI's module
        return str(getattr(comfy_nodes, "NODE_DISPLAY_NAME_MAPPINGS", {}).get(class_type) or class_type)
    except Exception:
        return class_type


def _title(prompt: Any, node: str, display: str) -> str:
    if not isinstance(prompt, dict):
        return ""
    for key in (display, node, node.split(".", 1)[0] if node else ""):
        entry = prompt.get(key) if key else None
        if isinstance(entry, dict):
            meta = entry.get("_meta") if isinstance(entry.get("_meta"), dict) else {}
            title = meta.get("title") or _class_display_name(str(entry.get("class_type") or ""))
            if title:
                return str(title)[:120]
    return ""


def _owner_ids(owners) -> tuple[list, list]:
    host_ids = [owner[2] for owner in owners if owner and owner[0] == "host" and len(owner) > 2]
    run_ids = [owner[1] for owner in owners if owner and owner[0] == "sdk" and len(owner) > 1]
    return host_ids, run_ids


def _receipt(data: dict) -> dict:
    return {
        "requested": [dict(item) for item in data["queued_receipt"]],
        "windows": [dict(value, editor=key) for key, value in data["windows"].items()],
        "jobs": [dict(value, editor=key) for key, value in data["jobs"].items()],
        "modes": [dict(row) for row in data["modes"]] if isinstance(data["modes"], list) else None,
    }


def _compose(prompt_id: str, bound: dict | None, prompt: Any, full: bool,
             broadcast: bool = True) -> tuple[dict | None, list]:
    """The public record and its aliases; ``seq`` taken here. Under the lock.
    A read (``broadcast=False``) never marks the receipt sent: the broadcast that
    a note scheduled must still carry it (audit 3A #12)."""
    data = _DATA.get(prompt_id)
    if data is None:
        return None, []
    owners = list((bound or {}).get("owners") or [])
    if not owners:
        return None, []  # no host yet: a provisional run waits for its registration
    aliases = list(dict.fromkeys([*(bound or {}).get("project_ids", []), *data["aliases"]]))
    data["seq"] += 1
    now = time.time()
    node, display = data["node"], data["display"]
    on_stage_node = bool(data["stage_node"]) and data["stage_node"] in (node, display, node.split(".", 1)[0])
    stage = data["stage"] if (on_stage_node or not node) else ""
    counted = (stage in COUNTED_STAGES) or (not stage and data["state"] == "running")
    host_ids, run_ids = _owner_ids(owners)
    record = {
        "prompt_id": prompt_id, "seq": data["seq"], "epoch": EPOCH, "state": data["state"],
        "purpose": (bound or {}).get("purpose") or "run", "host_ids": host_ids, "run_ids": run_ids,
        "stage": stage, "node": node, "node_title": "" if stage else _title(prompt, node, display),
        "count": dict(data["count"]) if counted and isinstance(data["count"], dict) else None,
        "waited_ms": int(max(0.0, (data["started_at"] or now) - data["queued_at"]) * 1000),
        "elapsed_ms": int(max(0.0, now - data["started_at"]) * 1000) if data["started_at"] else 0,
        "receipt_rev": data["receipt_rev"],
    }
    if data["terminal"] is not None:
        record["terminal"] = dict(data["terminal"])
    if full or data["receipt_rev"] != data["sent_receipt_rev"]:
        record["receipt"] = _receipt(data)
        if broadcast:
            data["sent_receipt_rev"] = data["receipt_rev"]
    return record, aliases


def _publish(prompt_id: str, force: bool = False, full: bool = False, bound: dict | None = None) -> None:
    """Send the record now, or leave one trailing update to be built when it fires."""
    now = time.monotonic()
    with _LOCK:
        data = _DATA.get(prompt_id)
        if data is None:
            return
        throttled = not force and now - data["last_sent"] < PERIOD_SEC
        trailing = throttled and not data["trailing"]
        if trailing:
            data["trailing"] = True
            delay, generation = PERIOD_SEC - (now - data["last_sent"]), _GENERATION[0]
    if throttled:
        if trailing:
            _schedule_trailing(prompt_id, delay, generation)
        return
    if bound is None:
        bound = sdk_runs.bound_run(prompt_id)
    prompt = sdk_runs.bound_prompt(prompt_id)
    with _LOCK:
        data = _DATA.get(prompt_id)
        if data is None or (data["terminal_sent"] and data["state"] in TERMINAL_STATES and not full):
            return
        record, aliases = _compose(prompt_id, bound, prompt, full)
        if record is None:
            return
        data["last_sent"] = time.monotonic()
        if data["state"] in TERMINAL_STATES:
            data["terminal_sent"] = True
    for alias in aliases:
        try:
            _emit_event(alias, {"type": "run_progress", "project_id": alias, "prompt_id": prompt_id,
                                "seq": record["seq"], "record": record})
        except Exception:
            logger.exception("Sending run feedback for %s failed", prompt_id)


def _schedule_trailing(prompt_id: str, delay: float, generation: int) -> None:
    """Fire the trailing update on the server's loop (no thread per update); a
    thread only when there is no loop (tests, early startup)."""
    loop = None
    try:
        from . import session_registry
        loop = session_registry._event_loop
    except Exception:
        loop = None
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(loop.call_later, max(0.0, delay), _fire_trailing, prompt_id, generation)
        return
    timer = threading.Timer(max(0.0, delay), _fire_trailing, (prompt_id, generation))
    timer.daemon = True
    timer.start()


def _fire_trailing(prompt_id: str, generation: int = 0) -> None:
    with _LOCK:
        if generation != _GENERATION[0]:
            return  # scheduled before a reset
        data = _DATA.get(prompt_id)
        if data is None:
            return
        data["trailing"] = False
        if data["terminal_sent"]:
            return  # a terminal already went out; a stale trailing update must not follow it
    _publish(prompt_id, force=True)


# --- Modes captured at submission ------------------------------------------------------

def _declares_categories(prompt: dict, orphan_manifest: Any) -> bool:
    """A cheap look before describing: does any Relay manifest name categories?"""
    from .control_manifest import RELAY_CLASS_TYPE
    texts = [orphan_manifest] if isinstance(orphan_manifest, str) else []
    for node in prompt.values():
        if isinstance(node, dict) and node.get("class_type") == RELAY_CLASS_TYPE:
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            texts.append(inputs.get("manifest"))
    return any(isinstance(text, str) and '"preset_categories"' in text for text in texts)


def capture_canvas_modes(controls: Any) -> list | None:
    """The Modes a canvas run was queued with, read exactly as the Controls panel
    reads them (muted and bypassed targets come from the authoring snapshot, never
    from their absence in the export). Blocking: call off the event loop.

    ``[]`` when the workflow has no categories; None when it can't be read (the
    receipt then omits Modes; registration never fails over it).
    """
    if not isinstance(controls, dict) or not isinstance(controls.get("prompt"), dict):
        return None
    prompt = controls["prompt"]
    census = controls.get("relay_census")
    if census is not None and not isinstance(census, dict):
        return None
    editor_key = controls.get("editor_key")
    if editor_key is not None and not (isinstance(editor_key, str) and isinstance(prompt.get(editor_key), dict)
                                       and prompt[editor_key].get("class_type") == sdk_runs.EDITOR_CLASS_TYPE):
        editor_key = None
    if not _declares_categories(prompt, controls.get("orphan_manifest")):
        return []  # no Modes to show: skip the describe entirely
    try:
        from . import control_descriptors, control_preset_categories
        view, readable, _authoring = control_descriptors.describe_for_projection(
            prompt, relay_census=census, inactive=controls.get("inactive"), editor_key=editor_key,
            orphan_manifest=controls.get("orphan_manifest"))
        categories = view.get("_preset_categories") or {}
        if not categories:
            return []
        view["presets"], view["preset_categories"] = control_descriptors.preset_projection(
            view, view.get("_presets", {}), categories, prompt=readable)
        return control_preset_categories.modes_receipt(view)
    except Exception:
        logger.warning("A run's Modes could not be read; its receipt omits them", exc_info=True)
        return None


def view_modes(view: Any) -> list | None:
    """Modes rows from an already projected view (the Sonder App's build)."""
    try:
        from . import control_preset_categories
        return control_preset_categories.modes_receipt(view)
    except Exception:
        logger.warning("A run's Modes could not be read; its receipt omits them", exc_info=True)
        return None


# --- Reads ---------------------------------------------------------------------------

def records_for(owners) -> list[dict]:
    """The owners' records, full receipts included: live ones, and terminals still kept."""
    reconcile()
    owners = {owner for owner in owners or () if isinstance(owner, tuple)}
    with _LOCK:
        prompt_ids = list(_DATA)
    found = []
    for prompt_id in prompt_ids:
        bound = sdk_runs.bound_run(prompt_id)
        if bound is None or not any(owner in owners for owner in bound["owners"]):
            continue
        prompt = sdk_runs.bound_prompt(prompt_id)
        with _LOCK:
            if prompt_id not in _DATA:
                continue
            record, _aliases = _compose(prompt_id, bound, prompt, True, broadcast=False)
        if record is not None:
            found.append(record)
    return found


def install() -> None:
    """Listen to native progress and execution ends. Idempotent."""
    sdk_progress.add_listener(_on_progress)  # a set: adding twice is one listener
    sdk_progress.add_observer(observe)


def reset_for_tests() -> None:
    with _LOCK:
        timer = _REAP["timer"]
        _REAP["timer"] = None
        _DATA.clear()
        _PENDING_MODES.clear()
        _HOOKS["emit"] = None
        _HOOKS["queued_ids"] = None
        _GENERATION[0] += 1
    if timer is not None:
        timer.cancel()
