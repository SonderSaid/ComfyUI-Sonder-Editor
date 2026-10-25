"""What each registered run published: the observation data behind Output Views.

``sdk_runs`` owns runs: registrations, the *bound run* per queued prompt (owners,
order, workflow, lifecycle, scope) and release records. This module keeps only
what was *seen* for a bound run, keyed by its prompt id: normalized output
entries per source node, whether each was produced by this run or replayed from
ComfyUI's cache, which Editors each source depends on, and the Editor context the
run executed with. Its data goes when ``sdk_runs`` evicts the run; it has no
lifecycle of its own.

Isolation, not authorization. Reads answer only their owner scope (a canvas host
key, or an SDK run id the runner resolves to its current session). Any page on
the same ComfyUI can read its history and files anyway; the point is that a
result never lands in another host, workflow or project by mistake.

Freshness comes from the event order ComfyUI uses: a node that ran sends
``executing`` before ``executed``; a cached node, or one its final sweep replays,
sends ``executed`` alone. ``execution_cached`` is not trusted (it lists the whole
prompt at start). An ``executed`` outside the run's execution scope is a sweep
replay of a node the run never selected, and is ignored.

Graph walks (scope, a source's Editors) run outside this module's lock; the lock
covers dict work only, because the event loop takes it too (``put``, register,
holds). The observer side runs on ComfyUI's prompt-worker thread. Stdlib only.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
import uuid
from typing import Any, Callable, Iterable

from . import output_media, run_scope, sdk_runs
from .prompt_graph import editor_keys, upstream_set

logger = logging.getLogger(__name__)

MAX_HOLDS_PER_OWNER = 8
HOLD_IDLE_SEC = 30 * 60
MAX_SOURCES_PER_READ = 32

OUTCOME_SUCCESS, OUTCOME_ERROR, OUTCOME_INTERRUPTED = "success", "error", "interrupted"
# Bound, then deleted or cleared from ComfyUI's queue before it started.
OUTCOME_REMOVED = sdk_runs.OUTCOME_REMOVED

_LOCK = threading.RLock()
_DATA: dict[str, dict[str, Any]] = {}
_HOLDS: dict[str, dict[str, Any]] = {}
_LISTENERS: list[Callable[[dict[str, Any]], None]] = []


def _new_data() -> dict[str, Any]:
    return {"executing": set(), "nodes": {}, "entry_count": 0, "truncated": 0,
            "editor_context": {}, "source_editors": {}}


def _prune_locked(live: set[str]) -> None:
    """Drop what was observed for runs ``sdk_runs`` no longer keeps."""
    for prompt_id in [pid for pid in _DATA if pid not in live]:
        del _DATA[prompt_id]


# --- Observation (prompt-worker thread) -----------------------------------------

def observe(event: str, data: Any) -> None:
    if not isinstance(data, dict):
        return
    prompt_id = str(data.get("prompt_id") or "")
    if not prompt_id or not sdk_runs.is_observing(prompt_id):
        return
    if event == "execution_start":
        sdk_runs.mark_bound_started(prompt_id)
    elif event == "executing":
        with _LOCK:
            executing = _DATA.setdefault(prompt_id, _new_data())["executing"]
            for key in ("node", "display_node"):
                value = data.get(key)
                if value not in (None, ""):
                    executing.add(str(value))
    elif event == "executed":
        _notify(_record_output(prompt_id, data.get("node"), data.get("display_node"), data.get("output")))


def _source_editors(prompt_id: str, source: str) -> list[str] | None:
    """The Editors ``source`` is upstream-reachable from; one walk per source, outside the lock."""
    with _LOCK:
        known = _DATA.get(prompt_id, {}).get("source_editors", {}).get(source)
    if known is not None:
        return known
    prompt = sdk_runs.bound_prompt(prompt_id)
    if not isinstance(prompt, dict):
        return None
    editors = editor_keys(prompt)
    if not editors:
        return []
    upstream = upstream_set(prompt, [source])
    return [key for key in editors if key in upstream]


def _record_output(prompt_id: str, node: Any, display_node: Any, output: Any) -> dict | None:
    node = str(node or "")
    source = str(display_node or node)
    if not source:
        return None
    entries, dropped = output_media.normalize(source, node, output)
    if not entries and not dropped:
        return None  # nothing published (a cached node with no UI): no slot, no hint
    scope, _keys = sdk_runs.bound_scope(prompt_id, run_scope.compute)
    if scope is not None and source not in scope:
        return None  # a sweep replay of a node this run did not select
    editors = _source_editors(prompt_id, source)
    with _LOCK:
        data = _DATA.setdefault(prompt_id, _new_data())
        fresh = node in data["executing"] or source in data["executing"]
        slot = data["nodes"].setdefault(source, {"entries": [], "fresh": False, "at": 0.0})
        if editors is not None:
            data["source_editors"].setdefault(source, editors)
        known = {_entry_key(entry) for entry in slot["entries"]}
        added = 0
        for entry in entries:
            identity = _entry_key(entry)
            if identity in known:
                continue
            if data["entry_count"] >= output_media.MAX_ENTRIES_PER_RUN or len(slot["entries"]) >= output_media.MAX_ENTRIES_PER_NODE:
                dropped += 1
                continue
            slot["entries"].append(dict(entry, index=len(slot["entries"])))
            data["entry_count"] += 1
            known.add(identity)
            added += 1
        slot["fresh"] = slot["fresh"] or fresh
        slot["at"] = time.time()
        data["truncated"] += dropped
    # Only new media is worth a hint; finish sends the last one either way.
    return _hint(sdk_runs.bound_run(prompt_id)) if added else None


def _entry_key(entry: dict) -> tuple:
    """Dedup key: the file, or the channel for an entry with no file (unsupported)."""
    identity = output_media.file_identity(entry)
    return identity if identity is not None else ("unsupported", entry.get("channel"))


def note_editor_context(prompt: Any, unique_id: Any, context: dict[str, Any],
                        content: Callable[[], dict | None] | None = None) -> None:
    """The Editor's executed window, recorded once per run through its trusted key.
    ``content`` builds the run's content record (Output Views' Previous
    context); it is called only for a run being observed."""
    key = sdk_runs.trusted_key_for_prompt(prompt)
    snapshot = sdk_runs.record_snapshot(key) if key else None
    prompt_id = str((snapshot or {}).get("prompt_id") or "")
    if not prompt_id or not sdk_runs.is_observing(prompt_id):
        return
    context = dict(context)
    if content is not None:
        try:
            record = content()
        except Exception:
            logger.exception("Recording the run's content digest failed")
            record = None
        if record is not None:
            context["content"] = record
    with _LOCK:
        _DATA.setdefault(prompt_id, _new_data())["editor_context"].setdefault(str(unique_id), context)


def graph_revision(prompt: Any) -> str:
    """sha256 of the canonical API prompt without any node's ``_meta``."""
    if not isinstance(prompt, dict):
        return ""
    stripped = {str(key): ({k: v for k, v in node.items() if k != "_meta"} if isinstance(node, dict) else node)
                for key, node in prompt.items()}
    text = json.dumps(stripped, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def finish(prompt_key: Any, prompt_id: Any, history_result: Any, outcome: str) -> None:
    """Reconcile a bound run with ComfyUI's final outputs and end it, before its release closes.

    ``history_result.outputs`` is the run's complete UI set (an event can be
    missed); ``meta`` maps each executed id to its display node. Worker thread.
    """
    prompt_id = str(prompt_id or "")
    if not sdk_runs.is_observing(prompt_id):
        return
    outputs = history_result.get("outputs") if isinstance(history_result, dict) else None
    meta = history_result.get("meta") if isinstance(history_result, dict) else None
    if isinstance(outputs, dict):
        for node, output in outputs.items():
            info = meta.get(node) if isinstance(meta, dict) else None
            display = info.get("display_node") if isinstance(info, dict) else None
            _record_output(prompt_id, node, display or node, output)
    graph_rev = graph_revision(sdk_runs.bound_prompt(prompt_id))
    record = sdk_runs.record_snapshot(prompt_key) or {}
    hint = sdk_runs.finish_bound(prompt_id, outcome or OUTCOME_SUCCESS, graph_rev,
                                 bool(record.get("saved_to_project")), str(record.get("committed_modified_at") or ""))
    live = sdk_runs.bound_ids()
    with _LOCK:
        if prompt_id in _DATA:
            _DATA[prompt_id]["executing"] = set()
        _prune_locked(live)
    _notify(_hint(hint))


def reap_removed(present: set[str] | None) -> None:
    """End bound runs removed from ComfyUI's queue unrun (``present`` = ids it holds)."""
    if present is None:
        return
    ended = sdk_runs.reap_removed_bound(present)
    if not ended:
        return
    live = sdk_runs.bound_ids()
    with _LOCK:
        _prune_locked(live)
    for bound in ended:
        _notify(_hint(bound))


def queued_prompt_ids(prompt_queue: Any = None) -> set[str] | None:
    """Prompt ids ComfyUI is running or holding, or None when it cannot say."""
    try:
        if prompt_queue is None:
            from server import PromptServer
            prompt_queue = PromptServer.instance.prompt_queue
        running, queued = prompt_queue.get_current_queue_volatile()
    except Exception:
        return None
    return {str(item[1]) for item in [*running, *queued] if isinstance(item, (list, tuple)) and len(item) > 1}


# --- Reads ------------------------------------------------------------------------

def _with_scope(bound: dict) -> dict:
    """A run's scope view, computed outside the lock for runs still in flight."""
    if not bound["finished"] and not bound["scope_known"]:
        scope, keys = sdk_runs.bound_scope(bound["prompt_id"], run_scope.compute)
        bound = dict(bound, scope=scope, prompt_keys=keys, scope_known=True)
    elif not bound["finished"] and bound["scope"] is None and bound["prompt_keys"] is None:
        _scope, keys = sdk_runs.bound_scope(bound["prompt_id"], run_scope.compute)
        bound = dict(bound, prompt_keys=keys)
    return bound


def _in_scope(bound: dict, data: dict, source: str) -> bool:
    if source in data["nodes"]:
        return True
    if bound["scope"] is not None:
        return source in bound["scope"]
    # Unknown scope is the whole prompt, never more: a node it lacks was not in the run.
    return bound["prompt_keys"] is None or source in bound["prompt_keys"]


def _context_for(data: dict, source: str) -> dict | None:
    editors = data["source_editors"].get(source) or ()
    contexts = {key: data["editor_context"][key] for key in editors if key in data["editor_context"]}
    return contexts or None


def _inherited_context(runs: list[dict], bound: dict, source: str, entries: list[dict]) -> dict | None:
    """A reused result keeps the context of the owner's earlier run that made those files."""
    identities = {output_media.file_identity(entry) for entry in entries} - {None}
    for earlier in runs:
        if earlier["seq"] >= bound["seq"]:
            continue
        slot = _DATA.get(earlier["prompt_id"], {}).get("nodes", {}).get(source)
        if slot and slot["fresh"] and identities & {output_media.file_identity(entry) for entry in slot["entries"]}:
            return _context_for(_DATA[earlier["prompt_id"]], source)
    return None


def _run_public(bound: dict) -> dict:
    return {"prompt_id": bound["prompt_id"], "seq": bound["seq"], "purpose": bound["purpose"],
            "workflow_id": bound["workflow_id"], "graph_rev": bound["graph_rev"],
            "finished": bound["finished"], "outcome": bound["outcome"],
            "saved_to_project": bound["saved_to_project"], "committed_modified_at": bound["committed_modified_at"]}


def _result(runs: list[dict], bound: dict, data: dict, source: str) -> dict:
    slot = data["nodes"][source]
    fresh = bool(slot["fresh"])
    context = _context_for(data, source) if fresh else _inherited_context(runs, bound, source, slot["entries"])
    return {**_run_public(bound), "fresh": fresh, "entries": [dict(entry) for entry in slot["entries"]],
            "editor_context": context}


def read_outputs(owners: Iterable[tuple], workflow_id: str = "", sources: Iterable[str] = ()) -> dict[str, Any]:
    """The latest result per source for an owner scope, by registration order."""
    wanted = [str(source) for source in sources if str(source)][:MAX_SOURCES_PER_READ]
    runs = [_with_scope(bound) for bound in sdk_runs.visible_bound(owners, workflow_id)]
    # A run removed from the queue never ran: it hides nothing earlier.
    ran = [bound for bound in runs if bound["outcome"] != OUTCOME_REMOVED]
    with _LOCK:
        answer = {}
        for source in wanted:
            relevant = [bound for bound in ran if _in_scope(bound, _DATA.get(bound["prompt_id"]) or _new_data(), source)]
            published = next((bound for bound in relevant
                              if (_DATA.get(bound["prompt_id"]) or {}).get("nodes", {}).get(source, {}).get("entries")), None)
            newest = relevant[0] if relevant else None
            if newest is None:
                # The owner ran something, just never this source.
                state = "not_in_run" if ran else "none"
            elif newest is published:
                state = "ok"
            elif not newest["finished"]:
                state = "pending"
            elif newest["outcome"] == OUTCOME_SUCCESS:
                state = "not_executed"
            else:
                state = "failed"
            answer[source] = {
                "state": state,
                "result": _result(runs, published, _DATA[published["prompt_id"]], source) if published else None,
                "latest_run": _run_public(newest) if newest else None,
            }
        return {"sources": answer, "runs": [_run_public(bound) for bound in runs[:32]]}


def read_run(owners: Iterable[tuple], prompt_id: str, workflow_id: str = "") -> dict[str, Any] | None:
    """One run's entries for its owner; None when unknown to that owner."""
    runs = sdk_runs.visible_bound(owners, workflow_id)
    bound = next((item for item in runs if item["prompt_id"] == str(prompt_id or "")), None)
    if bound is None:
        return None
    with _LOCK:
        data = _DATA.get(bound["prompt_id"]) or _new_data()
        return {**_run_public(bound), "sources": {source: _result(runs, bound, data, source) for source in data["nodes"]},
                "truncated": data["truncated"]}


def tombstone_for(owners: Iterable[tuple], prompt_id: str, workflow_id: str = "") -> dict | None:
    """An evicted run's identity, so its outputs can be re-read from ComfyUI history."""
    return sdk_runs.bound_tombstone(owners, prompt_id, workflow_id)


def entries_from_history(entry: dict, stone: dict) -> dict:
    """Normalize a history entry for a tombstoned run (no freshness: history has none)."""
    outputs, meta = entry.get("outputs") or {}, entry.get("meta") or {}
    scope, keys = stone.get("scope"), stone.get("prompt_keys")
    sources: dict[str, dict] = {}
    for node, output in outputs.items() if isinstance(outputs, dict) else ():
        info = meta.get(node) if isinstance(meta, dict) else None
        source = str((info or {}).get("display_node") or node) if isinstance(info, dict) else str(node)
        if (scope is not None and source not in scope) or (scope is None and keys is not None and source not in keys):
            continue
        found, _dropped = output_media.normalize(source, node, output)
        sources.setdefault(source, {"entries": [], "fresh": None, "editor_context": None})["entries"].extend(found)
    return {"prompt_id": stone["prompt_id"], "seq": stone["seq"], "finished": True,
            "outcome": stone.get("outcome", ""), "recovered": True, "sources": sources}


# --- Holds ------------------------------------------------------------------------

def hold(owners: Iterable[tuple], owner: tuple, prompt_id: str, source: str, index: int, stat: tuple | None) -> tuple[str, str]:
    """Pin one entry. Returns ``(code, hold_id)``; ``stat`` is the file's (size, mtime_ns)."""
    owners = [item for item in owners if isinstance(item, tuple)]
    found = read_run(owners, prompt_id)
    if found is None:
        return "unknown_run", ""
    found_source = found["sources"].get(str(source)) or {}
    entries = found_source.get("entries") or []
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(entries):
        return "unknown_entry", ""
    entry = entries[index]
    if entry.get("file") is None:
        return "unsupported", ""
    if stat is None:
        return "missing_file", ""
    now = time.time()
    run = {name: found[name] for name in ("prompt_id", "seq", "purpose", "workflow_id", "graph_rev", "finished",
                                          "outcome", "saved_to_project", "committed_modified_at")}
    with _LOCK:
        _expire_holds_locked(now)
        if sum(1 for item in _HOLDS.values() if item["owner"] == owner) >= MAX_HOLDS_PER_OWNER:
            return "too_many", ""
        hold_id = uuid.uuid4().hex
        _HOLDS[hold_id] = {"owner": owner, "prompt_id": found["prompt_id"], "source": str(source), "index": index,
                           "entry": dict(entry), "stat": tuple(stat), "run": run, "created": now, "seen": now,
                           # A held result links like a fresh one (Phase 3B), after its run's data is pruned.
                           "editor_context": copy.deepcopy(found_source.get("editor_context")),
                           "fresh": found_source.get("fresh")}
        return "ok", hold_id


def release(owners: Iterable[tuple], hold_id: str) -> bool:
    owners = {item for item in owners if isinstance(item, tuple)}
    with _LOCK:
        item = _HOLDS.get(str(hold_id or ""))
        if item is None or item["owner"] not in owners:
            return False
        del _HOLDS[hold_id]
        return True


def holds_for(owners: Iterable[tuple], keep: Iterable[str] | None = None) -> list[dict]:
    """The owner's live holds. A read refreshes only the holds its page still
    shows (``keep``); the others, a closed page's, age out after
    ``HOLD_IDLE_SEC``. ``keep=None`` refreshes every hold."""
    owners = {item for item in owners if isinstance(item, tuple)}
    kept = None if keep is None else {str(hold_id) for hold_id in keep}
    now = time.time()
    with _LOCK:
        _expire_holds_locked(now)
        found = []
        for hold_id, item in _HOLDS.items():
            if item["owner"] in owners:
                if kept is None or hold_id in kept:
                    item["seen"] = now
                found.append({"hold_id": hold_id, **{k: v for k, v in item.items() if k != "seen"}})
        return found


def _expire_holds_locked(now: float) -> None:
    # A hold nobody has read for this long belongs to a host that went away.
    for hold_id, item in list(_HOLDS.items()):
        if now - item["seen"] > HOLD_IDLE_SEC:
            del _HOLDS[hold_id]


# --- Change hints --------------------------------------------------------------------

def _hint(bound: dict | None) -> dict | None:
    if not bound:
        return None
    return {"prompt_id": bound["prompt_id"], "seq": bound["seq"], "owners": list(bound["owners"]),
            "project_ids": list(bound["project_ids"]), "finished": bool(bound["finished"])}


def announce(prompt_id: str) -> None:
    """Hint a run's owners now (a registration adopted a run observed before it)."""
    _notify(_hint(sdk_runs.bound_run(prompt_id)))


def add_listener(listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
    """``listener(hint)`` after a run records something; runs on the caller's thread."""
    with _LOCK:
        _LISTENERS.append(listener)

    def remove() -> None:
        with _LOCK:
            if listener in _LISTENERS:
                _LISTENERS.remove(listener)
    return remove


def _notify(snapshot: dict | None) -> None:
    if not snapshot or not snapshot["owners"]:
        return
    with _LOCK:
        listeners = list(_LISTENERS)
    for listener in listeners:
        try:
            listener(dict(snapshot))
        except Exception:
            logger.exception("Run output listener failed")


def reset_for_tests() -> None:
    with _LOCK:
        _DATA.clear()
        _HOLDS.clear()
        _LISTENERS.clear()
