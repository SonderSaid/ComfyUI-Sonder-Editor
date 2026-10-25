"""Observe ComfyUI's unthrottled per-node messages for SDK runs only.

No ComfyUI imports. State is ephemeral and protected across the prompt worker
and aiohttp threads. The runner marshals listener snapshots onto its loop.
Dev aid: SONDER_SDK_PROGRESS_CAPTURE=<path> appends watched raw messages with
monotonic timestamps as JSONL. It is off by default; captures may contain node
ids and are diagnostic artifacts, never project data or browser payloads.
"""
from __future__ import annotations

import functools
import json
import logging
import math
import os
import threading
import time

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_WATCHED = {}
_LISTENERS = set()
_WARNED = set()
_EVENTS = {"progress", "progress_state", "executing"}
# Other in-process observers ride this one wrap (never a second one): the run
# output observer reads executed results. Each sees every message after ComfyUI
# sent it and must stay cheap, since it runs on the sending thread.
_OBSERVED_EVENTS = frozenset({"executing", "executed", "execution_start", "execution_success",
                              "execution_error", "execution_interrupted"})
_OBSERVERS: list = []
# Sampling previews (Output Views Phase 4) ride the same wrap: their observer
# names the events it takes, ComfyUI's binary preview ints included, so this
# module stays free of ComfyUI imports. Int events and tuple data never reach
# the observers above.
_PREVIEW_EVENTS: set = set()
_PREVIEW_OBSERVERS: list = []
_WRAPPED = {"installed": False}


def is_installed() -> bool:
    """Whether a ``send_sync`` wrap is in place (observers see nothing otherwise)."""
    return _WRAPPED["installed"]


def add_preview_observer(observer, events):
    """``observer(event, data)`` for each message whose event is in ``events``. Idempotent.

    Expiry: retire with the preview dispatch when ComfyUI offers in-process
    preview subscribers that carry the prompt's identity.
    """
    with _LOCK:
        _PREVIEW_EVENTS.update(events)
        if observer not in _PREVIEW_OBSERVERS:
            _PREVIEW_OBSERVERS.append(observer)


def add_observer(observer):
    """``observer(event, data)`` for each message in ``_OBSERVED_EVENTS``. Idempotent."""
    with _LOCK:
        if observer not in _OBSERVERS:
            _OBSERVERS.append(observer)


def _warn_once(key):
    if key not in _WARNED:
        _WARNED.add(key)
        logger.warning("SDK progress %s unavailable; generation is unaffected", key, exc_info=True)


def add_listener(listener):
    with _LOCK:
        _LISTENERS.add(listener)
    def remove():
        with _LOCK:
            _LISTENERS.discard(listener)
    return remove


def watch(prompt_id, run_token):
    if prompt_id and run_token:
        with _LOCK:
            _WATCHED.setdefault(str(prompt_id), {"token": str(run_token), "node": "", "display": "", "nodes": {}, "count": None})


def unwatch(prompt_id, run_token=None):
    with _LOCK:
        state = _WATCHED.get(str(prompt_id))
        if state and (run_token is None or state["token"] == run_token):
            _WATCHED.pop(str(prompt_id), None)


def _count(data):
    value, maximum = data.get("value"), data.get("max")
    if (isinstance(value, (float, int)) and not isinstance(value, bool)
            and isinstance(maximum, (float, int)) and not isinstance(maximum, bool)
            and math.isfinite(value) and math.isfinite(maximum) and maximum > 1 and value >= 0):
        return {"value": value, "max": maximum}
    return None


def _belongs(node, current, nodes):
    """Same node or descendant; display identity handles expanded graph ids."""
    if not node or not current:
        return False
    display = nodes.get(current, {}).get("display_node_id")
    seen = set()
    while node and node not in seen:
        if node == current:
            return True
        seen.add(node)
        info = nodes.get(node, {})
        if display and info.get("display_node_id") == display:
            return True
        node = str(info.get("parent_node_id") or "")
    return False


def observe(event, data):
    if event not in _EVENTS or not isinstance(data, dict):
        return
    prompt_id = str(data.get("prompt_id") or "")
    with _LOCK:
        state = _WATCHED.get(prompt_id)
        if state is None:
            return
        capture = os.environ.get("SONDER_SDK_PROGRESS_CAPTURE")
        if capture:
            try:
                with open(capture, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"at": time.monotonic(), "event": event, "data": data}, ensure_ascii=False) + "\n")
            except Exception:
                _warn_once("capture")
        if event == "executing":
            state["node"] = str(data.get("node") or "")
            state["display"] = str(data.get("display_node") or state["node"])
            state["count"] = None
        elif event == "progress_state":
            nodes = data.get("nodes")
            if not isinstance(nodes, dict):
                return
            state["nodes"] = nodes = {str(k): v for k, v in nodes.items() if isinstance(v, dict)}
            running = [k for k, v in nodes.items() if v.get("state") == "running"]
            current = state["node"]
            if current not in running:
                # Expanding parents remain running. The latest runnable child
                # is the useful identity; its display id names the outer node.
                current = running[-1] if running else ""
            state["node"] = current
            state["display"] = str(nodes.get(current, {}).get("display_node_id") or current)
            state["count"] = None
            for node in reversed(running):
                count = _count(nodes[node])
                if count and _belongs(node, current, nodes):
                    state["count"] = count
                    break
        else:
            node = str(data.get("node") or "")
            if not _belongs(node, state["node"], state["nodes"]):
                return
            state["count"] = _count(data)
        snapshot = {"prompt_id": prompt_id, "run_token": state["token"],
                    "node_id": state["node"], "display_node_id": state["display"],
                    "count": dict(state["count"]) if state["count"] else None}
        listeners = list(_LISTENERS)
    for listener in listeners:
        try:
            listener(snapshot)
        except Exception:
            _warn_once("listener")


def install(prompt_server):
    """Add a transparent observer, or degrade to state text if unavailable.

    Expiry: retire this wrap when ComfyUI or the SDK offers unthrottled per-node
    progress with display identity to an in-process prompt subscriber.
    """
    try:
        original = getattr(prompt_server, "send_sync", None)
        if not callable(original):
            return False
        if getattr(original, "_sonder_sdk_progress", False):
            _WRAPPED["installed"] = True
            return True
        @functools.wraps(original)
        def send_sync(event, data, *args, **kwargs):
            result = original(event, data, *args, **kwargs)
            try:
                observe(event, data)
            except Exception:
                _warn_once("observer")
            if event in _OBSERVED_EVENTS:
                with _LOCK:
                    observers = list(_OBSERVERS)
                for observer in observers:
                    try:
                        observer(event, data)
                    except Exception:
                        _warn_once("run observer")
            elif event in _PREVIEW_EVENTS:
                with _LOCK:
                    observers = list(_PREVIEW_OBSERVERS)
                for observer in observers:
                    try:
                        observer(event, data)
                    except Exception:
                        _warn_once("preview observer")
            return result
        send_sync._sonder_sdk_progress = True
        prompt_server.send_sync = send_sync
        _WRAPPED["installed"] = True
        return True
    except Exception:
        _warn_once("installation")
        return False


def reset_for_tests():
    with _LOCK:
        _WATCHED.clear()
        _OBSERVERS.clear()
        _PREVIEW_EVENTS.clear()
        _PREVIEW_OBSERVERS.clear()
        _WRAPPED["installed"] = False
        _LISTENERS.clear()
        _WARNED.clear()
