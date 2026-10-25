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

Live sampling previews (Phase 4) are observation data too: the newest preview
per source of a run, by reference, attributed by ``sampling_previews``. A
payload is dropped when a newer run of the same canvas host takes its source,
or oldest first past a process-wide budget; its metadata stays, so a pane can
say why nothing is shown.
"""

from __future__ import annotations

import collections
import copy
import hashlib
import itertools
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

# Live sampling previews.
LIVE_BUDGET_BYTES = 64 * 1024 * 1024
MAX_LIVE_SOURCES_PER_RUN = 16
MAX_SEEN = 32
_LIVE_N = itertools.count(1)
_LIVE_BYTES = {"total": 0}
# Payload-holding records, oldest first: the budget evicts from the front.
_PAYLOADS: "collections.OrderedDict[tuple[str, str], int]" = collections.OrderedDict()
# (canvas host owner, workflow, source) -> (seq, prompt_id) of the newest run that
# published it: a newer run drops the older payload. Sonder App owners are one per
# run, so ("sdk", workflow, source) only reorders the budget: an older run's payload,
# which no read shows any more, is evicted first.
_LIVE_LATEST: dict[tuple, tuple[int, str]] = {}
_LIVE_LISTENERS: list[Callable[[dict[str, Any]], None]] = []


def _new_data() -> dict[str, Any]:
    return {"executing": set(), "nodes": {}, "entry_count": 0, "truncated": 0,
            "editor_context": {}, "source_editors": {},
            # Phase 4: newest preview per source, first-record source facts, each
            # sampler's upstream Override nodes, the run's preview method, and the
            # nodes that actually ran (kept past finish, for From cache).
            "live": {}, "live_info": {}, "kj_chain": {}, "display_ids": {}, "preview_method": "", "ran": None}


def _prune_locked(live: set[str]) -> None:
    """Drop what was observed for runs ``sdk_runs`` no longer keeps."""
    for prompt_id in [pid for pid in _DATA if pid not in live]:
        for source, record in _DATA[prompt_id]["live"].items():
            _drop_payload_locked(prompt_id, source, record)
        del _DATA[prompt_id]
    for key, (_seq, prompt_id) in list(_LIVE_LATEST.items()):
        if prompt_id not in _DATA:
            del _LIVE_LATEST[key]


def _drop_payload_locked(prompt_id: str, source: str, record: dict, state: str | None = None) -> None:
    """The one place a payload leaves memory, so the budget counter stays exact."""
    if record.get("payload") is not None:
        _LIVE_BYTES["total"] -= int(record.get("size") or 0)
        record["payload"] = None
        record["size"] = 0
    _PAYLOADS.pop((prompt_id, source), None)
    if state is not None:
        record["state"] = state


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
    with _LOCK:
        # Before the run reads finished: a read in between must not take "nothing ran".
        if prompt_id in _DATA:
            _DATA[prompt_id]["ran"] = frozenset(_DATA[prompt_id]["executing"])
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


# --- Live sampling previews (Phase 4) ------------------------------------------------
#
# ``sampling_previews`` attributes each preview and calls in here from the
# sending thread: dict work under the lock, nothing else.

def _writable_locked(prompt_id: str) -> dict | None:
    """The run's data to record into; never recreates data a prune already dropped."""
    data = _DATA.get(prompt_id)
    if data is None and sdk_runs.is_bound(prompt_id):
        data = _DATA[prompt_id] = _new_data()
    return data


def live_display_id(prompt_id: str, node_id: str) -> str | None:
    with _LOCK:
        return (_DATA.get(prompt_id) or {}).get("display_ids", {}).get(node_id)


def note_live_display_id(prompt_id: str, node_id: str, display: str) -> None:
    with _LOCK:
        data = _writable_locked(prompt_id)
        if data is not None and len(data["display_ids"]) < 4096:
            data["display_ids"].setdefault(node_id, display)


def live_source_info(prompt_id: str, source: str) -> dict | None:
    """First-record facts of a source: ``{class_type, title}``, ``{}`` for a source
    that is no key of the prompt, None when not yet looked at."""
    with _LOCK:
        return (_DATA.get(prompt_id) or {}).get("live_info", {}).get(source)


def note_live_source_info(prompt_id: str, source: str, info: dict) -> None:
    with _LOCK:
        data = _writable_locked(prompt_id)
        if data is not None:
            data["live_info"].setdefault(source, dict(info))


def live_kj_chain(prompt_id: str, via: str) -> frozenset | None:
    with _LOCK:
        return (_DATA.get(prompt_id) or {}).get("kj_chain", {}).get(via)


def note_live_kj_chain(prompt_id: str, via: str, chain: frozenset) -> None:
    with _LOCK:
        data = _writable_locked(prompt_id)
        if data is not None:
            data["kj_chain"].setdefault(via, frozenset(chain))


def note_preview_method(prompt_id: str, method: str) -> None:
    with _LOCK:
        data = _writable_locked(prompt_id)
        if data is not None:
            data["preview_method"] = str(method or "")[:32]


def record_live(bound: dict, source: str, record: dict, payload: tuple | None, size: int) -> None:
    """Keep the newest preview of ``source`` for the bound run, then hint its owners."""
    prompt_id = bound["prompt_id"]
    with _LOCK:
        data = _writable_locked(prompt_id)
        if data is None:
            return
        live = data["live"]
        if source not in live and len(live) >= MAX_LIVE_SOURCES_PER_RUN:
            return
        previous = live.get(source)
        if previous is not None:
            _drop_payload_locked(prompt_id, source, previous)
        entry = dict(record)
        entry.setdefault("state", "ok")
        entry.update(n=next(_LIVE_N), at=time.time(), prompt_id=prompt_id,
                     payload=payload, size=int(size) if payload is not None else 0)
        live[source] = entry
        if payload is not None:
            _PAYLOADS[(prompt_id, source)] = entry["size"]
            _LIVE_BYTES["total"] += entry["size"]
        for owner in bound["owners"]:
            if not (isinstance(owner, tuple) and owner):
                continue
            if owner[0] == "sdk":
                _prefer_eviction_locked(("sdk", bound["workflow_id"], source), _live_order(bound), prompt_id, source)
                continue
            if owner[0] != "host":
                continue
            key = (owner, bound["workflow_id"], source)
            latest = _LIVE_LATEST.get(key)
            if latest is not None and latest[1] != prompt_id:
                if latest[0] > _live_order(bound):
                    # A newer run already shows this source: a late payload is never shown.
                    _drop_payload_locked(prompt_id, source, entry, "evicted")
                    entry["superseded"] = True
                    continue
                older = (_DATA.get(latest[1]) or {}).get("live", {}).get(source)
                if older is not None:
                    _drop_payload_locked(latest[1], source, older, "evicted")
                    older["superseded"] = True
            _LIVE_LATEST[key] = (_live_order(bound), prompt_id)
        while _LIVE_BYTES["total"] > LIVE_BUDGET_BYTES and _PAYLOADS:
            oldest = next(iter(_PAYLOADS))
            if oldest == (prompt_id, source):
                if len(_PAYLOADS) == 1:
                    break
                _PAYLOADS.move_to_end(oldest)
                continue
            evicted = (_DATA.get(oldest[0]) or {}).get("live", {}).get(oldest[1])
            if evicted is None:
                _PAYLOADS.pop(oldest, None)
                continue
            _drop_payload_locked(oldest[0], oldest[1], evicted, "evicted")
        listeners = list(_LIVE_LISTENERS)
    hint = _hint(bound)
    if not hint or not hint["owners"]:
        return
    for listener in listeners:
        try:
            listener(dict(hint))
        except Exception:
            logger.exception("Live preview listener failed")


def _live_order(bound: dict) -> tuple:
    """Which of two runs' previews is newer: the one that started later.

    ComfyUI runs one prompt at a time but not in registration order (Queue
    Front), so start order decides; registration ``seq`` breaks ties and orders
    runs that have not started.
    """
    return (int(bound.get("started_seq") or 0), int(bound.get("seq") or 0))


def _prefer_eviction_locked(key: tuple, order: tuple, prompt_id: str, source: str) -> None:
    """Sonder App runs: put the payload no read shows any more at the front of the budget."""
    latest = _LIVE_LATEST.get(key)
    if latest is None or latest[1] == prompt_id:
        _LIVE_LATEST[key] = (order, prompt_id)
        return
    if latest[0] > order:
        stale = (prompt_id, source)
    else:
        stale = (latest[1], source)
        _LIVE_LATEST[key] = (order, prompt_id)
    if stale in _PAYLOADS:
        _PAYLOADS.move_to_end(stale, last=False)


def _live_public(record: dict) -> dict:
    public = {key: value for key, value in record.items() if key not in ("payload", "size")}
    public["has_payload"] = record.get("payload") is not None
    return public


def read_live(owners: Iterable[tuple], workflow_id: str = "", sources: Iterable[str] = ()) -> dict[str, Any]:
    """The preview a pane shows per source, the state it is in, and the sources
    this owner's retained runs saw publish (for the picker)."""
    wanted = [str(source) for source in sources if str(source)][:MAX_SOURCES_PER_READ]
    runs = [_with_scope(bound) for bound in sdk_runs.visible_bound(owners, workflow_id)]
    ran = [bound for bound in runs if bound["outcome"] != OUTCOME_REMOVED]
    # Newest first by the order runs started in (Queue Front reorders them);
    # `latest_run` stays the owner's newest registration.
    live_order = sorted(ran, key=_live_order, reverse=True)
    with _LOCK:
        empty = _new_data()
        answer = {}
        for source in wanted:
            relevant = [bound for bound in live_order
                        if source in (_DATA.get(bound["prompt_id"]) or empty)["live"]
                        or _in_scope(bound, _DATA.get(bound["prompt_id"]) or empty, source)]
            if not relevant:
                answer[source] = {"state": "none", "live": None, "run": None,
                                  "latest_run": _latest_public(ran[0], relevant) if ran else None}
                continue
            # A run still waiting in ComfyUI's queue does not make the one sampling
            # now "previous": the newest run that started (or ended) decides.
            started = [bound for bound in relevant if bound["state"] != sdk_runs.BOUND_QUEUED]
            newest = started[0] if started else relevant[0]
            newest_data = _DATA.get(newest["prompt_id"]) or empty
            shown = next((bound for bound in relevant
                          if source in (_DATA.get(bound["prompt_id"]) or empty)["live"]), None)
            record = _DATA[shown["prompt_id"]]["live"][source] if shown is not None else None
            has = record is not None and record.get("payload") is not None
            if shown is newest:
                if has:
                    state = "last" if newest["finished"] else "live"
                elif record["state"] in ("too_large", "unsupported"):
                    state = record["state"]
                elif record["state"] == "starting":
                    state = "no_previews" if newest["finished"] else "waiting"
                else:
                    state = "evicted"
            elif not newest["finished"]:
                state = "previous" if has else "waiting"
            elif (has and newest["outcome"] == OUTCOME_SUCCESS
                  and record.get("via", source) not in (newest_data["ran"] or frozenset())):
                state = "cached"  # the sampler came from ComfyUI's cache: its last preview stands
            else:
                state = "no_previews"
            suppressed_by = next((other for other, item in newest_data["live"].items()
                                  if other != source and item.get("publisher") == "kj"
                                  and item.get("via") == source), None)
            ran_set = newest_data["ran"]
            answer[source] = {
                "state": state,
                "live": _live_public(record) if record is not None else None,
                "run": {"prompt_id": newest["prompt_id"], "seq": newest["seq"], "finished": newest["finished"],
                        "outcome": newest["outcome"], "preview_method": newest_data["preview_method"],
                        "suppressed_by": suppressed_by, "in_scope": True,
                        # Whether the node itself executed (null until the run says): a
                        # node served from ComfyUI's cache sends no previews.
                        "source_ran": (source in ran_set) if ran_set is not None else None},
                "latest_run": _latest_public(ran[0], relevant),
            }
        seen: dict[str, dict] = {}
        for bound in ran:
            for source, record in (_DATA.get(bound["prompt_id"]) or empty)["live"].items():
                if source not in seen and len(seen) < MAX_SEEN:
                    seen[source] = {"source": source, "class_type": record.get("class_type", ""),
                                    "title": record.get("title", ""), "publisher": record.get("publisher", "")}
        return {"sources": answer, "seen": list(seen.values())}


def _latest_public(bound: dict, relevant: list[dict]) -> dict:
    """The owner's newest run, and whether this source was in it (a newer run may have left it out)."""
    return {"prompt_id": bound["prompt_id"], "seq": bound["seq"], "finished": bound["finished"],
            "outcome": bound["outcome"], "in_scope": any(item is bound for item in relevant)}


def live_payload(owners: Iterable[tuple], prompt_id: str, source: str) -> tuple[str, dict | None, tuple | None]:
    """``(code, record, payload)`` for the newest preview of one run's source, for its owner."""
    prompt_id, source = str(prompt_id or ""), str(source or "")
    if not any(bound["prompt_id"] == prompt_id for bound in sdk_runs.visible_bound(owners)):
        return "unknown_run", None, None
    with _LOCK:
        record = (_DATA.get(prompt_id) or {}).get("live", {}).get(source)
        if record is None:
            return "unknown_source", None, None
        if record.get("payload") is None:
            return ("superseded" if record.get("superseded") else "unavailable"), _live_public(record), None
        return "ok", _live_public(record), record["payload"]


def store_live_bytes(prompt_id: str, source: str, n: int, body: bytes) -> None:
    """Keep the encoded bytes in place of the image or base64 they came from."""
    with _LOCK:
        record = (_DATA.get(prompt_id) or {}).get("live", {}).get(source)
        if (record is None or record.get("n") != n or record.get("payload") is None
                or record["payload"][0] == "bytes"):
            return
        _LIVE_BYTES["total"] += len(body) - int(record.get("size") or 0)
        record["payload"], record["size"] = ("bytes", body), len(body)
        _PAYLOADS[(prompt_id, source)] = len(body)


def live_bytes_in_use() -> int:
    with _LOCK:
        return _LIVE_BYTES["total"]


def add_live_listener(listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
    """``listener(hint)`` after a run records a live preview; runs on the sending thread."""
    with _LOCK:
        _LIVE_LISTENERS.append(listener)

    def remove() -> None:
        with _LOCK:
            if listener in _LIVE_LISTENERS:
                _LIVE_LISTENERS.remove(listener)
    return remove


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
        _LIVE_LISTENERS.clear()
        _PAYLOADS.clear()
        _LIVE_LATEST.clear()
        _LIVE_BYTES["total"] = 0
