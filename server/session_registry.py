import asyncio
import copy
import hmac
import logging
import os
import time
import uuid
from collections import defaultdict, deque
from typing import Any, Callable

from aiohttp import web

from . import control_descriptors

logger = logging.getLogger("sonder_editor")

SESSION_TTL_SECONDS = 30.0
CANVAS_HOST_TTL_SECONDS = 90.0
HANDOFF_TTL_SECONDS = 30.0
ORPHAN_TTL_SECONDS = 1800.0
SWEEP_INTERVAL_SECONDS = 30.0

CONTROLS_MAX_BYTES = 512 * 1024
CONTROLS_MAX_PENDING = 32
CONTROLS_ACK_HISTORY = 128
CONTROLS_MAX_SEQ = 2 ** 53 - 1

_DEBUG_SESSION_ENABLED = os.environ.get("SONDER_DEBUG_SESSION", "").strip().lower() in ("1", "true", "yes", "on")

WIDGET_FIELD_NAMES = {
    "scene_id",
    "selection_start",
    "selection_end",
    "pre_context_frames",
    "post_context_frames",
    "mask_pre_offset",
    "mask_post_offset",
    "take_placement_mode",
    "take_placement_linked",
    "take_placement_muted",
    "take_fit_mode",
    "take_crop_position",
    "render_queue_active",
}

HostKey = tuple[str, str]

# Host modes whose stale owner is orphaned (kept for ORPHAN_TTL_SECONDS) rather than
# released: a standalone page that a background-throttled browser tab may starve.
ORPHANING_HOST_MODES = frozenset({"tab", "sdk"})

# Set by the SDK runner: maps an SDK session's host id to its secret, or None when
# the host id is not an SDK session. Injected so this module never imports the runner.
_sdk_secret_for_host: Callable[[str], str | None] | None = None


def set_sdk_secret_resolver(resolver: Callable[[str], str | None] | None) -> None:
    global _sdk_secret_for_host
    _sdk_secret_for_host = resolver


def _sdk_secret(host_id: str) -> str | None:
    resolver = _sdk_secret_for_host
    if resolver is None:
        return None
    try:
        return resolver(host_id)
    except Exception:
        logger.exception("SDK secret resolver failed")
        return None

_sessions: dict[HostKey, dict[str, Any]] = {}
_handoffs: dict[str, dict[str, Any]] = {}
_subscribers: dict[str, set[web.WebSocketResponse]] = defaultdict(set)
_widget_state: dict[HostKey, dict[str, Any]] = {}
_widget_sources: dict[HostKey, str] = {}
_canvas_hosts: dict[HostKey, dict[str, Any]] = {}
# Ephemeral transport only. The canvas graph owns the values. Pending requests
# survive poll/socket interruptions until the canvas acknowledges them.
_controls_relay: dict[HostKey, dict[str, Any]] = {}
_debug_events: deque[dict[str, Any]] = deque(maxlen=120)
_diag_events: deque[dict[str, Any]] = deque(maxlen=2048)
_diag_boot: dict[str, Any] | None = None
_lock = asyncio.Lock()
_event_loop: asyncio.AbstractEventLoop | None = None
_sweeper_task: asyncio.Task | None = None


if _DEBUG_SESSION_ENABLED:
    _diag_boot = {
        "kind": "boot",
        "t_wall": time.time() * 1000.0,
        "t_mono": time.monotonic() * 1000.0,
        "build_marker": "backend",
        "session_ttl": SESSION_TTL_SECONDS,
        "canvas_host_ttl": CANVAS_HOST_TTL_SECONDS,
        "orphan_ttl": ORPHAN_TTL_SECONDS,
        "sweep_interval": SWEEP_INTERVAL_SECONDS,
    }
    _diag_events.append(dict(_diag_boot))


def remember_event_loop(loop: asyncio.AbstractEventLoop | None = None) -> None:
    global _event_loop, _sweeper_task
    try:
        active_loop = loop or asyncio.get_running_loop()
    except RuntimeError:
        return
    _event_loop = active_loop
    if active_loop.is_running() and (_sweeper_task is None or _sweeper_task.done()):
        _sweeper_task = active_loop.create_task(_sweep_loop())


def _now() -> float:
    return time.time()


def _clean_id(value: str | None) -> str:
    return str(value or "").strip()


def _resolve_host_id(host_id: str | None = "", source_node_id: str | None = "") -> str:
    return _clean_id(host_id) or _clean_id(source_node_id)


def _host_key(project_id: str, host_id: str | None = "", source_node_id: str | None = "") -> HostKey:
    return _clean_id(project_id), _resolve_host_id(host_id, source_node_id)


def _clean_widget_values(values: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(values, dict):
        return {}
    return {str(name): value for name, value in values.items() if str(name) in WIDGET_FIELD_NAMES}


def _record_debug_event(action: str, key: HostKey, **details: Any) -> None:
    _debug_events.append({
        "at": _now(),
        "action": action,
        "project_id": key[0],
        "host_id": key[1],
        **details,
    })


def _record_diag_event(kind: str, key: HostKey | None = None, **details: Any) -> None:
    if not _DEBUG_SESSION_ENABLED:
        return
    _diag_events.append({
        "t_wall": time.time() * 1000.0,
        "t_mono": time.monotonic() * 1000.0,
        "kind": kind,
        "project_id": key[0] if key else "",
        "host_id": key[1] if key else "",
        **details,
    })


def record_diag_event(kind: str, project_id: str = "", host_id: str = "", **details: Any) -> None:
    """Public emitter for diagnostic events from other server modules.

    No-op when `SONDER_DEBUG_SESSION` is disabled at module import time."""
    if not _DEBUG_SESSION_ENABLED:
        return
    key: HostKey | None = (_clean_id(project_id), _clean_id(host_id)) if (project_id or host_id) else None
    _record_diag_event(kind, key, **details)


def get_diag_state(project_id: str = "") -> dict[str, Any]:
    project_id = _clean_id(project_id)
    if project_id:
        events = [
            dict(event) for event in _diag_events
            if not event.get("project_id") or event.get("project_id") == project_id
        ]
    else:
        events = [dict(event) for event in _diag_events]
    return {
        "enabled": _DEBUG_SESSION_ENABLED,
        "boot": dict(_diag_boot) if _diag_boot else None,
        "events": events,
        "ring_size": len(_diag_events),
        "ring_max": _diag_events.maxlen,
    }


def _public_owner(owner: dict[str, Any] | None) -> dict[str, Any] | None:
    if not owner:
        return None
    return {
        "project_id": owner.get("project_id", ""),
        "host_id": owner.get("host_id", ""),
        "session_id": owner.get("session_id", ""),
        "host_mode": owner.get("host_mode", ""),
        "source_node_id": owner.get("source_node_id", ""),
        "workflow_id": owner.get("workflow_id", ""),
        "workflow_label": owner.get("workflow_label", ""),
        "browser_instance_id": owner.get("browser_instance_id", ""),
        "claimed_at": owner.get("claimed_at", 0.0),
        "last_seen": owner.get("last_seen", 0.0),
        "status": owner.get("status", "active") or "active",
        "orphan_expires_at": owner.get("orphan_expires_at", 0.0),
    }


def _public_host(host: dict[str, Any] | None) -> dict[str, Any] | None:
    if not host:
        return None
    return {
        "project_id": host.get("project_id", ""),
        "host_id": host.get("host_id", ""),
        "source_node_id": host.get("source_node_id", ""),
        "session_id": host.get("session_id", ""),
        "workflow_id": host.get("workflow_id", ""),
        "workflow_label": host.get("workflow_label", ""),
        "connected_at": host.get("connected_at", 0.0),
        "last_seen": host.get("last_seen", 0.0),
    }


def _is_stale(owner: dict[str, Any] | None) -> bool:
    return bool(owner) and (_now() - float(owner.get("last_seen", 0.0) or 0.0)) > SESSION_TTL_SECONDS


def _is_host_stale(host: dict[str, Any] | None) -> bool:
    return bool(host) and (_now() - float(host.get("last_seen", 0.0) or 0.0)) > CANVAS_HOST_TTL_SECONDS


def _make_owner(
    project_id: str,
    host_id: str,
    session_id: str,
    host_mode: str,
    info: dict[str, Any] | None,
) -> dict[str, Any]:
    now = _now()
    info = dict(info or {})
    return {
        "project_id": project_id,
        "host_id": host_id,
        "session_id": session_id,
        "host_mode": host_mode,
        "source_node_id": str(info.get("source_node_id") or ""),
        "workflow_id": str(info.get("workflow_id") or ""),
        "workflow_label": str(info.get("workflow_label") or ""),
        "browser_instance_id": str(info.get("browser_instance_id") or ""),
        "claimed_at": now,
        "last_seen": now,
        "status": "active",
        "orphan_expires_at": 0.0,
    }


def _make_host(
    project_id: str,
    host_id: str,
    source_node_id: str,
    session_id: str,
    workflow_id: str = "",
    workflow_label: str = "",
) -> dict[str, Any]:
    now = _now()
    return {
        "project_id": _clean_id(project_id),
        "host_id": _clean_id(host_id),
        "source_node_id": _clean_id(source_node_id),
        "session_id": _clean_id(session_id),
        "workflow_id": str(workflow_id or ""),
        "workflow_label": str(workflow_label or ""),
        "connected_at": now,
        "last_seen": now,
    }


def _session_changed_event(key: HostKey, owner: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "type": "session_changed",
        "project_id": key[0],
        "host_id": key[1],
        "owner": _public_owner(owner),
    }


def _host_presence_event(key: HostKey, host: dict[str, Any] | None) -> dict[str, Any]:
    source_node_id = ""
    if host:
        source_node_id = str(host.get("source_node_id") or "")
    if not source_node_id:
        source_node_id = _widget_sources.get(key, "")
    return {
        **_controls_presence_locked(key),
        "type": "host_presence_changed",
        "project_id": key[0],
        "host_id": key[1],
        "source_node_id": source_node_id,
        "canvas_host_connected": bool(host),
        "host": _public_host(host),
    }


def _widget_state_payload(
    key: HostKey,
    session_id: str = "",
    values: dict[str, Any] | None = None,
    source_node_id: str = "",
) -> dict[str, Any]:
    state = dict(_widget_state.get(key, {}))
    host = _canvas_hosts.get(key)
    if host and _is_host_stale(host):
        host = None
    return {
        "ok": True,
        "project_id": key[0],
        "host_id": key[1],
        "source_node_id": source_node_id or _widget_sources.get(key, ""),
        "session_id": str(session_id or ""),
        "values": dict(state if values is None else values),
        "state": state,
        **_controls_presence_locked(key),
        "canvas_host_connected": bool(host),
        "host": _public_host(host),
    }


def _clear_project_widget_state_locked(project_id: str, host_id: str | None = None) -> None:
    project_id = _clean_id(project_id)
    host_id = None if host_id is None else _clean_id(host_id)
    for key in list(_widget_state.keys()):
        if key[0] == project_id and (host_id is None or key[1] == host_id):
            _widget_state.pop(key, None)
            _widget_sources.pop(key, None)


def _matching_session_key_locked(project_id: str, host_id: str = "", source_node_id: str = "") -> HostKey | None:
    key = _host_key(project_id, host_id, source_node_id)
    if not key[0]:
        return None
    if key[1]:
        return key
    matches = [candidate for candidate in _sessions.keys() if candidate[0] == key[0]]
    return matches[0] if len(matches) == 1 else None


def _release_owner_locked(
    key: HostKey,
    events: list[tuple[str, dict[str, Any]]],
    *,
    clear_widget_state: bool = True,
    reason: str = "release",
) -> dict[str, Any] | None:
    owner = _sessions.get(key)
    if not owner:
        return None
    _sessions.pop(key, None)
    _clear_controls_locked(key)
    if clear_widget_state:
        _clear_project_widget_state_locked(key[0], key[1])
    _record_debug_event(
        reason,
        key,
        session_id=owner.get("session_id", ""),
        previous_mode=owner.get("host_mode", ""),
        previous_status=owner.get("status", "active"),
    )
    _record_diag_event(
        "owner_release",
        key,
        reason=reason,
        session_id=owner.get("session_id", ""),
        previous_mode=owner.get("host_mode", ""),
        previous_status=owner.get("status", "active"),
        cleared_widget_state=clear_widget_state,
    )
    events.append((key[0], _session_changed_event(key, None)))
    return owner


def _orphan_owner_locked(key: HostKey, owner: dict[str, Any], events: list[tuple[str, dict[str, Any]]]) -> None:
    last_seen = float(owner.get("last_seen", 0.0) or 0.0)
    owner["status"] = "orphaned"
    owner["orphan_expires_at"] = _now() + ORPHAN_TTL_SECONDS
    _record_debug_event(
        "orphan",
        key,
        session_id=owner.get("session_id", ""),
        host_mode=owner.get("host_mode", ""),
    )
    _record_diag_event(
        "owner_orphan",
        key,
        session_id=owner.get("session_id", ""),
        host_mode=owner.get("host_mode", ""),
        last_seen_age=_now() - last_seen,
        orphan_expires_at=owner["orphan_expires_at"],
    )
    events.append((key[0], _session_changed_event(key, owner)))


def _reactivate_owner_locked(key: HostKey, owner: dict[str, Any], events: list[tuple[str, dict[str, Any]]]) -> None:
    previous_last_seen = float(owner.get("last_seen", 0.0) or 0.0)
    owner["status"] = "active"
    owner["orphan_expires_at"] = 0.0
    owner["last_seen"] = _now()
    _record_debug_event(
        "reactivate",
        key,
        session_id=owner.get("session_id", ""),
        host_mode=owner.get("host_mode", ""),
    )
    _record_diag_event(
        "owner_reactivate",
        key,
        session_id=owner.get("session_id", ""),
        host_mode=owner.get("host_mode", ""),
        previous_last_seen_age=_now() - previous_last_seen,
    )
    events.append((key[0], _session_changed_event(key, owner)))


def _advance_owner_lifecycle_locked(key: HostKey, events: list[tuple[str, dict[str, Any]]]) -> int:
    owner = _sessions.get(key)
    if not owner:
        return 0
    status = owner.get("status", "active") or "active"
    if status == "orphaned":
        orphan_expires_at = float(owner.get("orphan_expires_at", 0.0) or 0.0)
        if orphan_expires_at and _now() >= orphan_expires_at:
            _release_owner_locked(key, events, clear_widget_state=True, reason="orphan_expired")
            return 1
        return 0
    if not _is_stale(owner):
        return 0
    if owner.get("host_mode") in ORPHANING_HOST_MODES:
        _orphan_owner_locked(key, owner, events)
    else:
        _release_owner_locked(key, events, clear_widget_state=True, reason="stale_fullscreen")
    return 1


def _evict_stale_host_locked(key: HostKey, events: list[tuple[str, dict[str, Any]]], *, trigger: str = "") -> None:
    host = _canvas_hosts.get(key)
    if host and _is_host_stale(host):
        last_seen = float(host.get("last_seen", 0.0) or 0.0)
        _canvas_hosts.pop(key, None)
        _clear_controls_locked(key)
        _record_diag_event(
            "canvas_host_evict",
            key,
            trigger=trigger or "unspecified",
            session_id=host.get("session_id", ""),
            source_node_id=host.get("source_node_id", ""),
            last_seen_age=_now() - last_seen,
            ttl=CANVAS_HOST_TTL_SECONDS,
        )
        events.append((key[0], _host_presence_event(key, None)))


async def _emit_events(events: list[tuple[str, dict[str, Any]]]) -> None:
    for project_id, event in events:
        await broadcast_project_event(project_id, event)


def _schedule_events(events: list[tuple[str, dict[str, Any]]]) -> None:
    for project_id, event in events:
        schedule_project_event(project_id, event)


async def claim_session(
    project_id: str,
    session_id: str,
    host_mode: str,
    info: dict[str, Any] | None = None,
    handoff_token: str = "",
    host_id: str = "",
    sdk_secret: str = "",
) -> dict[str, Any]:
    project_id = _clean_id(project_id)
    session_id = _clean_id(session_id)
    host_mode = _clean_id(host_mode) or "fullscreen"
    info = dict(info or {})
    host_id = _resolve_host_id(host_id or info.get("host_id"), info.get("source_node_id"))
    if not project_id or not session_id:
        _record_diag_event("claim_reject", None, code="invalid_session", project_id=project_id, session_id=session_id)
        return {"ok": False, "code": "invalid_session", "owner": None}
    # An SDK session's host id is claimable only with its secret, whatever mode the
    # claim names; with it, the claim takes over the current owner (reload,
    # duplicated tab, Take over). The secret is tab identity, not network auth.
    expected_secret = _sdk_secret(host_id) if host_id else None
    if host_mode == "sdk" or expected_secret is not None:
        if expected_secret is None or not hmac.compare_digest(str(sdk_secret or ""), expected_secret):
            _record_diag_event("claim_reject", None, code="invalid_sdk_session", project_id=project_id, session_id=session_id, host_mode=host_mode)
            return {"ok": False, "code": "invalid_sdk_session", "owner": None}
        return await _claim_sdk_session(project_id, session_id, host_id, info)
    if host_mode == "tab" and not handoff_token:
        _record_diag_event("claim_reject", None, code="invalid_handoff_token", project_id=project_id, session_id=session_id, host_mode=host_mode)
        return {"ok": False, "code": "invalid_handoff", "owner": None}
    if not host_id:
        _record_diag_event("claim_reject", None, code="invalid_host", project_id=project_id, session_id=session_id, host_mode=host_mode)
        return {"ok": False, "code": "invalid_host", "owner": None}

    key = _host_key(project_id, host_id)
    events: list[tuple[str, dict[str, Any]]] = []
    async with _lock:
        _advance_owner_lifecycle_locked(key, events)
        owner = _sessions.get(key)

        if handoff_token:
            handoff_key = str(handoff_token)
            handoff = _handoffs.get(handoff_key)
            valid_handoff = (
                handoff
                and handoff.get("project_id") == project_id
                and handoff.get("host_id") == host_id
                and (_now() - float(handoff.get("created_at", 0.0) or 0.0)) <= HANDOFF_TTL_SECONDS
                and owner
                and owner.get("session_id") == handoff.get("from_session_id")
            )
            if not valid_handoff:
                _record_diag_event(
                    "claim_reject", key,
                    code="invalid_handoff", session_id=session_id, host_mode=host_mode,
                    has_handoff=bool(handoff),
                    has_owner=bool(owner),
                    owner_session_id=(owner.get("session_id", "") if owner else ""),
                )
                result = {"ok": False, "code": "invalid_handoff", "owner": _public_owner(owner)}
            else:
                _handoffs.pop(handoff_key, None)
                if owner.get("session_id") != session_id:
                    _clear_controls_locked(key)
                _sessions[key] = _make_owner(project_id, host_id, session_id, host_mode, info)
                _record_debug_event("claim", key, session_id=session_id, host_mode=host_mode, source_node_id=info.get("source_node_id", ""))
                _record_diag_event(
                    "claim_ok", key,
                    source="handoff", session_id=session_id, host_mode=host_mode,
                    source_node_id=info.get("source_node_id", ""),
                )
                result = {"ok": True, "owner": _public_owner(_sessions[key])}
                events.append((project_id, _session_changed_event(key, _sessions[key])))
        elif owner and owner.get("session_id") != session_id:
            _record_debug_event("claim_locked", key, session_id=session_id, host_mode=host_mode, owner_session_id=owner.get("session_id", ""))
            _record_diag_event(
                "claim_reject", key,
                code="locked", session_id=session_id, host_mode=host_mode,
                owner_session_id=owner.get("session_id", ""),
                owner_status=owner.get("status", ""),
            )
            result = {"ok": False, "code": "locked", "owner": _public_owner(owner)}
        else:
            if not owner:
                _clear_controls_locked(key)
            _sessions[key] = _make_owner(project_id, host_id, session_id, host_mode, info)
            _record_debug_event("claim", key, session_id=session_id, host_mode=host_mode, source_node_id=info.get("source_node_id", ""))
            _record_diag_event(
                "claim_ok", key,
                source="direct", session_id=session_id, host_mode=host_mode,
                source_node_id=info.get("source_node_id", ""),
                replaced_owner=bool(owner),
            )
            result = {"ok": True, "owner": _public_owner(_sessions[key])}
            events.append((project_id, _session_changed_event(key, _sessions[key])))

    _schedule_events(events)
    return result


async def _claim_sdk_session(project_id: str, session_id: str, host_id: str, info: dict[str, Any]) -> dict[str, Any]:
    key = _host_key(project_id, host_id)
    events: list[tuple[str, dict[str, Any]]] = []
    async with _lock:
        _advance_owner_lifecycle_locked(key, events)
        previous = _sessions.get(key)
        _sessions[key] = _make_owner(project_id, host_id, session_id, "sdk", info)
        took_over = bool(previous) and previous.get("session_id") != session_id
        _record_diag_event(
            "claim_ok", key,
            source="sdk", session_id=session_id, host_mode="sdk",
            replaced_owner=bool(previous), took_over=took_over,
        )
        event = _session_changed_event(key, _sessions[key])
        if took_over:
            event["reason"] = "taken_over"
        events.append((project_id, event))
        result = {"ok": True, "owner": _public_owner(_sessions[key]), "took_over": took_over}
    _schedule_events(events)
    return result


async def heartbeat_session(project_id: str, session_id: str, host_id: str = "", source_node_id: str = "") -> dict[str, Any]:
    events: list[tuple[str, dict[str, Any]]] = []
    async with _lock:
        key = _matching_session_key_locked(project_id, host_id, source_node_id)
        if not key:
            _record_diag_event(
                "heartbeat_reject", None,
                code="invalid_host", project_id=project_id, host_id=host_id,
                source_node_id=source_node_id, session_id=session_id,
            )
            result = {"ok": False, "code": "invalid_host", "owner": None}
        else:
            owner = _sessions.get(key)
            if owner and owner.get("status") == "orphaned":
                orphan_expires_at = float(owner.get("orphan_expires_at", 0.0) or 0.0)
                if orphan_expires_at and _now() >= orphan_expires_at:
                    _release_owner_locked(key, events, clear_widget_state=True, reason="orphan_expired")
                    owner = None
            if owner and owner.get("status", "active") != "orphaned" and _is_stale(owner):
                if owner.get("host_mode") in ORPHANING_HOST_MODES and owner.get("session_id") == session_id:
                    owner["last_seen"] = _now()
                else:
                    _advance_owner_lifecycle_locked(key, events)
                    owner = _sessions.get(key)
            if not owner:
                _record_diag_event("heartbeat_reject", key, code="no_owner", session_id=session_id)
                result = {"ok": False, "code": "no_owner", "owner": None}
            elif owner.get("session_id") != session_id:
                _record_debug_event("heartbeat_locked", key, session_id=session_id, owner_session_id=owner.get("session_id", ""))
                _record_diag_event(
                    "heartbeat_reject", key,
                    code="locked", session_id=session_id,
                    owner_session_id=owner.get("session_id", ""),
                    owner_status=owner.get("status", ""),
                )
                result = {"ok": False, "code": "locked", "owner": _public_owner(owner)}
            else:
                previous_last_seen = float(owner.get("last_seen", 0.0) or 0.0)
                was_orphaned = owner.get("status") == "orphaned"
                if was_orphaned:
                    _reactivate_owner_locked(key, owner, events)
                else:
                    owner["last_seen"] = _now()
                _record_diag_event(
                    "heartbeat_ok", key,
                    session_id=session_id,
                    host_mode=owner.get("host_mode", ""),
                    reactivated_from_orphan=was_orphaned,
                    previous_last_seen_age=_now() - previous_last_seen,
                )
                result = {"ok": True, "owner": _public_owner(owner)}
    _schedule_events(events)
    return result


async def release_session(
    project_id: str,
    session_id: str,
    force: bool = False,
    host_id: str = "",
    source_node_id: str = "",
) -> dict[str, Any]:
    events: list[tuple[str, dict[str, Any]]] = []
    async with _lock:
        key = _matching_session_key_locked(project_id, host_id, source_node_id)
        if not key:
            _record_diag_event(
                "release_reject", None,
                code="invalid_host", project_id=project_id, host_id=host_id,
                session_id=session_id, force=force,
            )
            result = {"ok": False, "code": "invalid_host", "owner": None}
        else:
            owner = _sessions.get(key)
            if not owner:
                _clear_controls_locked(key)
                _record_diag_event("release_noop", key, session_id=session_id, force=force)
                result = {"ok": True, "owner": None}
            elif not force and owner.get("session_id") != session_id:
                _record_diag_event(
                    "release_reject", key,
                    code="locked", session_id=session_id, force=force,
                    owner_session_id=owner.get("session_id", ""),
                )
                result = {"ok": False, "code": "locked", "owner": _public_owner(owner)}
            else:
                _release_owner_locked(
                    key,
                    events,
                    clear_widget_state=True,
                    reason="force_release" if force else "release",
                )
                result = {"ok": True, "owner": None}
    _schedule_events(events)
    return result


async def get_owner(project_id: str, host_id: str = "", source_node_id: str = "") -> dict[str, Any] | None:
    events: list[tuple[str, dict[str, Any]]] = []
    async with _lock:
        key = _matching_session_key_locked(project_id, host_id, source_node_id)
        if not key:
            result = None
        else:
            _advance_owner_lifecycle_locked(key, events)
            result = _public_owner(_sessions.get(key))
    _schedule_events(events)
    return result


async def create_handoff(project_id: str, session_id: str, host_id: str = "", source_node_id: str = "") -> dict[str, Any]:
    events: list[tuple[str, dict[str, Any]]] = []
    async with _lock:
        key = _matching_session_key_locked(project_id, host_id, source_node_id)
        if not key:
            result = {"ok": False, "code": "invalid_host", "owner": None}
        else:
            _advance_owner_lifecycle_locked(key, events)
            owner = _sessions.get(key)
            if not owner or owner.get("session_id") != session_id:
                result = {"ok": False, "code": "not_owner", "owner": _public_owner(owner)}
            else:
                token = uuid.uuid4().hex
                _handoffs[token] = {
                    "token": token,
                    "project_id": key[0],
                    "host_id": key[1],
                    "from_session_id": session_id,
                    "created_at": _now(),
                }
                result = {"ok": True, "token": token, "owner": _public_owner(owner)}
    _schedule_events(events)
    return result


def _clear_controls_locked(key: HostKey) -> None:
    _controls_relay.pop(key, None)


def _new_controls_state() -> dict:
    return {"view": None, "view_seq": 0, "prompt_hash": "", "visible": False,
            "pending": [], "outcomes": [], "publication": None}


def _controls_presence_locked(key: HostKey) -> dict:
    state = _controls_relay.get(key, {})
    owner = _sessions.get(key)
    host = _canvas_hosts.get(key)
    visible = bool(state.get("visible") and owner and owner.get("host_mode") == "tab"
                   and owner.get("status", "active") == "active" and not _is_stale(owner)
                   and host and not _is_host_stale(host))
    return {"controls_view_seq": state.get("view_seq", 0), "controls_visible": visible}


def _controls_payload_locked(key: HostKey) -> dict:
    state = _controls_relay.get(key, {})
    outcomes = state.get("outcomes", [])
    return {"ok": True, "project_id": key[0], "host_id": key[1],
            **_controls_presence_locked(key), "view": state.get("view"),
            "view_seq": state.get("view_seq", 0), "prompt_hash": state.get("prompt_hash", ""),
            "applied": [o["request_id"] for o in outcomes if not o["rejected"]],
            "rejected": [row for o in outcomes for row in o["rejected"]]}


def _controls_gate_locked(key: HostKey, session_id: str, role: str, events: list) -> str:
    if not key[0] or not key[1] or not session_id:
        return "invalid_host"
    _advance_owner_lifecycle_locked(key, events)
    _evict_stale_host_locked(key, events, trigger="controls_relay")
    host, owner = _canvas_hosts.get(key), _sessions.get(key)
    if _sdk_secret(key[1]) is not None or (owner and owner.get("host_mode") == "sdk"):
        return "invalid_host"
    if not host:
        return "canvas_host_disconnected"
    if role == "canvas":
        return "" if host.get("session_id") == session_id else "not_canvas_host"
    if not owner or owner.get("session_id") != session_id or owner.get("host_mode") != "tab":
        return "not_owner"
    return "" if owner.get("status", "active") == "active" else "session_orphaned"


def _prepare_control_publication(body: Any) -> dict:
    """Validate/copy the wire snapshot off the loop; never retain caller objects."""
    if not isinstance(body, dict):
        raise ValueError("The publication must be an object.")
    seq, prompt_hash = body.get("view_seq"), body.get("prompt_hash")
    if isinstance(seq, bool) or not isinstance(seq, int) or not 1 <= seq <= CONTROLS_MAX_SEQ:
        raise ValueError("view_seq must be a positive safe integer.")
    if not isinstance(prompt_hash, str) or not 1 <= len(prompt_hash) <= 128:
        raise ValueError("A prompt hash is required.")
    view = body.get("view")
    if not isinstance(view, dict):
        raise ValueError("The view must be an object.")
    for name in ("controls", "fields", "notes"):
        if not isinstance(view.get(name), list) or not all(isinstance(d, dict) for d in view[name]):
            raise ValueError(f"{name} must be a list of descriptors.")
        for descriptor in view[name]:
            identity = descriptor.get("note_id" if name == "notes" else "control_id") if name != "fields" else None
            if name != "fields" and not isinstance(identity, str):
                raise ValueError("Descriptor identities must be text.")
            if name == "fields" and not all(isinstance(descriptor.get(n), str) for n in ("class_type", "field")):
                raise ValueError("Field identities must be text.")
            if not isinstance(descriptor.get("status"), str):
                raise ValueError("A descriptor status is required.")
            kind = descriptor.get("kind")
            if kind is not None and not isinstance(kind, str):
                raise ValueError("Descriptor kinds must be text.")
            if name != "notes":
                if "options" in descriptor and not isinstance(descriptor["options"], (list, tuple)):
                    raise ValueError("Descriptor options must be a list.")
                if "options_truncated" in descriptor and not isinstance(descriptor["options_truncated"], bool):
                    raise ValueError("The options preview flag must be boolean.")
                for bound in ("min", "max"):
                    if descriptor.get(bound) is not None:
                        numeric_kind = "INT" if kind in ("INT", "SEED") else "FLOAT"
                        control_descriptors.coerce_control_value(numeric_kind, descriptor[bound])
    view = control_descriptors.json_safe(view)
    for name, field in (("controls", "note"), ("notes", "text")):
        for descriptor in view[name]:
            text = descriptor.get(field)
            if text is not None and not isinstance(text, str):
                raise ValueError("Notes must be text.")
            if isinstance(text, str) and len(text) > control_descriptors.PANEL_NOTE_MAX_CHARS:
                descriptor[field] = text[:control_descriptors.PANEL_NOTE_MAX_CHARS]
                descriptor["note_truncated"] = True
    applied, rejected = body.get("applied", []), body.get("rejected", [])
    if not isinstance(applied, list) or len(applied) > CONTROLS_MAX_PENDING or not all(isinstance(r, str) for r in applied):
        raise ValueError("applied must list up to 32 request ids.")
    if not isinstance(rejected, list) or len(rejected) > CONTROLS_MAX_PENDING * control_descriptors.MAX_CONTROL_CHANGES:
        raise ValueError("Too many request refusals.")
    for row in rejected:
        if not isinstance(row, dict) or not all(isinstance(row.get(n), str) for n in ("request_id", "target", "reason")):
            raise ValueError("A refusal needs a request id, target and reason.")
    return {"view": view, "view_seq": seq, "prompt_hash": prompt_hash,
            "applied": list(dict.fromkeys(applied)),
            "rejected": [{"request_id": r["request_id"], "target": r["target"], "reason": r["reason"][:500]} for r in rejected]}


async def get_controls_view(project_id: str, host_id: str) -> dict:
    key, events = _host_key(project_id, host_id), []
    async with _lock:
        if not all(key):
            return {"ok": False, "code": "invalid_host"}
        _advance_owner_lifecycle_locked(key, events)
        _evict_stale_host_locked(key, events, trigger="get_controls_view")
        payload = _controls_payload_locked(key)
    _schedule_events(events)
    return await asyncio.to_thread(copy.deepcopy, payload)


async def publish_controls_view(project_id: str, host_id: str, session_id: str, body: Any) -> dict:
    key, events = _host_key(project_id, host_id), []
    async with _lock:
        code = _controls_gate_locked(key, session_id, "canvas", events)
        host_before, owner_before = _canvas_hosts.get(key), _sessions.get(key)
    _schedule_events(events)
    if code:
        return {"ok": False, "code": code}
    try:
        publication = await asyncio.to_thread(_prepare_control_publication, body)
    except (ValueError, OverflowError, TypeError, RecursionError) as exc:
        return {"ok": False, "code": "invalid_view", "error": str(exc)}
    key, events = _host_key(project_id, host_id), []
    async with _lock:
        code = _controls_gate_locked(key, session_id, "canvas", events)
        if not code and (_canvas_hosts.get(key) is not host_before or _sessions.get(key) is not owner_before):
            code = "host_changed"
        state = _controls_relay.get(key)
        if not code and state and publication["view_seq"] <= state["view_seq"]:
            if publication == state.get("publication"):
                result = {"ok": True, "view_seq": state["view_seq"], "duplicate": True}
            else:
                result = {"ok": False, "code": "stale_view", "view_seq": state["view_seq"]}
        elif code:
            result = {"ok": False, "code": code}
        else:
            state = state or _new_controls_state()
            pending = {r["request_id"]: r for r in state["pending"]}
            known = {o["request_id"]: o for o in state["outcomes"]}
            applied = set(publication["applied"])
            refusals = {}
            for row in publication["rejected"]:
                refusals.setdefault(row["request_id"], []).append(row)
            acknowledged = applied | set(refusals)
            invalid = applied & set(refusals) or acknowledged - pending.keys() - known.keys()
            for rid, rows in refusals.items():
                changes = pending.get(rid, {}).get("changes")
                if changes is not None and any(row["target"] not in changes for row in rows):
                    invalid.add(rid)
            for rid in acknowledged & known.keys():
                if known[rid]["rejected"] != refusals.get(rid, []):
                    invalid.add(rid)
            if invalid:
                result = {"ok": False, "code": "invalid_ack"}
            else:
                outcomes = list(state["outcomes"])
                for request in state["pending"]:
                    rid = request["request_id"]
                    if rid in acknowledged:
                        outcomes.append({"request_id": rid, "rejected": refusals.get(rid, [])})
                state.update(view=publication["view"], view_seq=publication["view_seq"],
                             prompt_hash=publication["prompt_hash"], publication=publication,
                             pending=[r for r in state["pending"] if r["request_id"] not in acknowledged],
                             outcomes=outcomes)
                _controls_relay[key] = state
                result = {"ok": True, "view_seq": state["view_seq"]}
                events.append((key[0], {"type": "controls_changed", "project_id": key[0],
                                       "host_id": key[1], "view_seq": state["view_seq"]}))
    _schedule_events(events)
    return result


async def post_control_changes(project_id: str, host_id: str, session_id: str, changes: Any) -> dict:
    key, events = _host_key(project_id, host_id), []
    async with _lock:
        code = _controls_gate_locked(key, session_id, "tab", events)
        state = _controls_relay.get(key)
        view = state.get("view") if state else None
        seq = state.get("view_seq", 0) if state else 0
    _schedule_events(events)
    if code:
        return {"ok": False, "code": code}
    if view is None:
        return {"ok": False, "code": "view_unavailable"}
    cleaned, rejected = await asyncio.to_thread(control_descriptors.validate_control_changes, view, changes)
    events = []
    async with _lock:
        code = _controls_gate_locked(key, session_id, "tab", events)
        current = _controls_relay.get(key)
        if code:
            result = {"ok": False, "code": code}
        elif current is not state or current.get("view_seq") != seq:
            result = {"ok": False, "code": "stale_view"}
        elif rejected:
            result = {"ok": False, "code": "invalid_change", "rejected": rejected}
        elif (len(state["pending"]) >= CONTROLS_MAX_PENDING
              or len(state["pending"]) + len(state["outcomes"]) >= CONTROLS_ACK_HISTORY):
            # Reserve one outcome slot at enqueue. Never forget an unseen result
            # to make room: the owner confirms receipts via controls_visibility.
            result = {"ok": False, "code": "queue_full"}
        else:
            rid = uuid.uuid4().hex
            state["pending"].append({"request_id": rid, "session_id": session_id,
                                     "view_seq": seq, "prompt_hash": state["prompt_hash"], "changes": cleaned})
            result = {"ok": True, "request_id": rid}
            events.append((key[0], {"type": "control_changes_pending", "project_id": key[0], "host_id": key[1]}))
    _schedule_events(events)
    return result


async def get_control_changes(project_id: str, host_id: str, session_id: str) -> dict:
    key, events = _host_key(project_id, host_id), []
    async with _lock:
        code = _controls_gate_locked(key, session_id, "canvas", events)
        state = _controls_relay.get(key, {})
        result = {"ok": False, "code": code} if code else {
            "ok": True, "project_id": key[0], "host_id": key[1],
            **_controls_presence_locked(key), "pending": list(state.get("pending", []))}
    _schedule_events(events)
    return await asyncio.to_thread(control_descriptors.json_safe, result)


async def set_controls_visibility(project_id: str, host_id: str, session_id: str,
                                  visible: Any, seen: Any = None) -> dict:
    if not isinstance(visible, bool):
        return {"ok": False, "code": "invalid_visibility"}
    if seen is not None and (not isinstance(seen, list) or len(seen) > CONTROLS_ACK_HISTORY
                             or not all(isinstance(rid, str) for rid in seen)):
        return {"ok": False, "code": "invalid_receipts"}
    receipts = set(seen or [])
    key, events = _host_key(project_id, host_id), []
    async with _lock:
        code = _controls_gate_locked(key, session_id, "tab", events)
        if code:
            result = {"ok": False, "code": code}
        else:
            state = _controls_relay.setdefault(key, _new_controls_state())
            # Only terminal outcomes can be confirmed. Unknown ids and repeated
            # receipts are harmless; early receipts never remove pending writes.
            state["outcomes"] = [o for o in state["outcomes"] if o["request_id"] not in receipts]
            changed = state["visible"] != visible
            state["visible"] = visible
            result = {"ok": True, **_controls_presence_locked(key)}
            if changed:
                events.append((key[0], {"type": "controls_visibility_changed", "project_id": key[0],
                                       "host_id": key[1], **_controls_presence_locked(key)}))
    _schedule_events(events)
    return result


async def seed_widget_state(
    project_id: str,
    source_node_id: str,
    session_id: str = "",
    values: dict[str, Any] | None = None,
    host_id: str = "",
) -> dict[str, Any]:
    key = _host_key(project_id, host_id, source_node_id)
    cleaned = _clean_widget_values(values)
    events: list[tuple[str, dict[str, Any]]] = []
    async with _lock:
        if not key[0] or not key[1]:
            return {"ok": False, "code": "invalid_host", "values": {}, "state": {}}
        _evict_stale_host_locked(key, events, trigger="seed_widget_state")
        if source_node_id:
            _widget_sources[key] = _clean_id(source_node_id)
        _widget_state[key] = dict(cleaned)
        payload = _widget_state_payload(key, session_id, cleaned, source_node_id)
    events.append((key[0], {"type": "widget_state_changed", **payload}))
    _schedule_events(events)
    return payload


async def update_widget_state(
    project_id: str,
    source_node_id: str,
    session_id: str = "",
    values: dict[str, Any] | None = None,
    host_id: str = "",
) -> dict[str, Any]:
    key = _host_key(project_id, host_id, source_node_id)
    cleaned = _clean_widget_values(values)
    events: list[tuple[str, dict[str, Any]]] = []
    async with _lock:
        if not key[0] or not key[1]:
            _record_diag_event("widget_update_reject", key, code="invalid_host", session_id=session_id)
            return {"ok": False, "code": "invalid_host", "values": {}, "state": {}}
        _advance_owner_lifecycle_locked(key, events)
        _evict_stale_host_locked(key, events, trigger="update_widget_state")
        host = _canvas_hosts.get(key)
        owner = _sessions.get(key)
        is_canvas_host_session = bool(host and session_id and host.get("session_id") == session_id)
        is_owner_session = bool(owner and session_id and owner.get("session_id") == session_id)
        if owner and not is_owner_session and not is_canvas_host_session:
            _record_diag_event(
                "widget_update_reject", key, code="locked", session_id=session_id,
                owner_session_id=owner.get("session_id", ""), owner_status=owner.get("status", ""),
            )
            result = {
                **_widget_state_payload(key, session_id, {}, source_node_id),
                "ok": False,
                "code": "locked",
                "owner": _public_owner(owner),
            }
        elif owner and is_owner_session and owner.get("status", "active") != "active":
            _record_diag_event(
                "widget_update_reject", key, code="session_orphaned", session_id=session_id,
                owner_status=owner.get("status", ""),
            )
            result = {
                **_widget_state_payload(key, session_id, {}, source_node_id),
                "ok": False,
                "code": "session_orphaned",
                "owner": _public_owner(owner),
            }
        elif owner and owner.get("host_mode") == "tab" and is_owner_session and not host:
            _record_diag_event(
                "widget_update_reject", key, code="canvas_host_disconnected", session_id=session_id,
                owner_status=owner.get("status", ""), host_mode=owner.get("host_mode", ""),
            )
            result = {
                **_widget_state_payload(key, session_id, {}, source_node_id),
                "ok": False,
                "code": "canvas_host_disconnected",
                "owner": _public_owner(owner),
            }
        elif session_id and not owner and not is_canvas_host_session:
            code = "no_owner" if host else "canvas_host_disconnected"
            _record_diag_event("widget_update_reject", key, code=code, session_id=session_id, has_host=bool(host))
            result = {
                **_widget_state_payload(key, session_id, {}, source_node_id),
                "ok": False,
                "code": code,
            }
        else:
            if source_node_id:
                _widget_sources[key] = _clean_id(source_node_id)
            state = _widget_state.setdefault(key, {})
            state.update(cleaned)
            payload = _widget_state_payload(key, session_id, cleaned, source_node_id)
            result = payload
            _record_diag_event(
                "widget_update_accept", key,
                session_id=session_id, field_count=len(cleaned),
                fields=sorted(cleaned.keys()),
                owner_session_id=(owner.get("session_id", "") if owner else ""),
                is_canvas_host_session=is_canvas_host_session,
                is_owner_session=is_owner_session,
            )
            if cleaned:
                events.append((key[0], {"type": "widget_state_changed", **payload}))
    _schedule_events(events)
    return result


async def get_widget_state(project_id: str, source_node_id: str = "", host_id: str = "") -> dict[str, Any]:
    key = _host_key(project_id, host_id, source_node_id)
    events: list[tuple[str, dict[str, Any]]] = []
    async with _lock:
        if not key[0] or not key[1]:
            return {"ok": False, "code": "invalid_host", "values": {}, "state": {}}
        _evict_stale_host_locked(key, events, trigger="get_widget_state")
        payload = _widget_state_payload(key, source_node_id=source_node_id)
    _schedule_events(events)
    return payload


async def clear_widget_state(project_id: str, source_node_id: str | None = None, host_id: str | None = None) -> None:
    key = _host_key(project_id, host_id or "", source_node_id or "")
    async with _lock:
        _clear_project_widget_state_locked(key[0], key[1] or None)


async def register_canvas_host(
    project_id: str,
    source_node_id: str,
    session_id: str,
    workflow_id: str = "",
    workflow_label: str = "",
    host_id: str = "",
) -> dict[str, Any]:
    key = _host_key(project_id, host_id, source_node_id)
    events: list[tuple[str, dict[str, Any]]] = []
    async with _lock:
        if not key[0] or not key[1]:
            return {"ok": False, "code": "invalid_host", "canvas_host_connected": False}
        _evict_stale_host_locked(key, events, trigger="register_canvas_host")
        previous = _canvas_hosts.get(key)
        if previous and previous.get("session_id") != session_id:
            _clear_controls_locked(key)
        _widget_sources[key] = _clean_id(source_node_id)
        host = _make_host(key[0], key[1], source_node_id, session_id, workflow_id, workflow_label)
        _canvas_hosts[key] = host
        _record_debug_event("canvas_host_register", key, session_id=session_id, source_node_id=source_node_id)
        _record_diag_event(
            "canvas_host_register", key,
            session_id=session_id,
            source_node_id=source_node_id,
            workflow_id=workflow_id,
            workflow_label=workflow_label,
        )
        event = _host_presence_event(key, host)
    events.append((key[0], event))
    _schedule_events(events)
    return {"ok": True, **event}


async def refresh_canvas_host(project_id: str, host_id: str, source_node_id: str, session_id: str = "") -> dict[str, Any]:
    key = _host_key(project_id, host_id, source_node_id)
    events: list[tuple[str, dict[str, Any]]] = []
    async with _lock:
        if not key[0] or not key[1]:
            return {"ok": False, "code": "invalid_host", "canvas_host_connected": False}
        _evict_stale_host_locked(key, events, trigger="refresh_canvas_host")
        host = _canvas_hosts.get(key)
        if not host or (session_id and host.get("session_id") != session_id):
            _record_debug_event("canvas_host_refresh_miss", key, session_id=session_id, existing_session_id=host.get("session_id", "") if host else "")
            _record_diag_event(
                "canvas_host_refresh_miss", key,
                session_id=session_id,
                existing_session_id=(host.get("session_id", "") if host else ""),
                had_host=bool(host),
            )
            result = {"ok": False, "code": "no_canvas_host", **_host_presence_event(key, host)}
        else:
            previous_last_seen = float(host.get("last_seen", 0.0) or 0.0)
            host["last_seen"] = _now()
            _record_diag_event(
                "canvas_host_refresh_ok", key,
                session_id=session_id,
                last_seen_age_before=_now() - previous_last_seen,
            )
            result = {"ok": True, **_host_presence_event(key, host)}
    _schedule_events(events)
    return result


async def unregister_canvas_host(project_id: str, source_node_id: str, session_id: str = "", host_id: str = "") -> dict[str, Any]:
    key = _host_key(project_id, host_id, source_node_id)
    async with _lock:
        host = _canvas_hosts.get(key)
        if host and (not session_id or host.get("session_id") == session_id):
            _canvas_hosts.pop(key, None)
            _clear_controls_locked(key)
            _record_debug_event("canvas_host_unregister", key, session_id=session_id, source_node_id=source_node_id)
            _record_diag_event(
                "canvas_host_unregister", key,
                session_id=session_id,
                source_node_id=source_node_id,
            )
            removed = True
        else:
            _record_diag_event(
                "canvas_host_unregister_miss", key,
                session_id=session_id,
                source_node_id=source_node_id,
                had_host=bool(host),
            )
            removed = False
        event = _host_presence_event(key, None if removed else _canvas_hosts.get(key))
    if removed:
        schedule_project_event(key[0], event)
    return {"ok": True, **event}


async def get_canvas_host(project_id: str, source_node_id: str = "", host_id: str = "") -> dict[str, Any] | None:
    key = _host_key(project_id, host_id, source_node_id)
    events: list[tuple[str, dict[str, Any]]] = []
    async with _lock:
        _evict_stale_host_locked(key, events, trigger="get_canvas_host")
        result = _public_host(_canvas_hosts.get(key))
    _schedule_events(events)
    return result


async def get_project_debug_state(project_id: str, source_node_id: str = "", host_id: str = "") -> dict[str, Any]:
    project_id = _clean_id(project_id)
    requested_key = _host_key(project_id, host_id, source_node_id)
    events: list[tuple[str, dict[str, Any]]] = []
    async with _lock:
        for key in list(_canvas_hosts.keys()):
            if key[0] == project_id:
                _evict_stale_host_locked(key, events, trigger="get_project_debug_state")
        for key in list(_sessions.keys()):
            if key[0] == project_id:
                _advance_owner_lifecycle_locked(key, events)
        hosts = [
            _public_host(host)
            for key, host in sorted(_canvas_hosts.items())
            if key[0] == project_id
        ]
        owners = [
            _public_owner(owner)
            for key, owner in sorted(_sessions.items())
            if key[0] == project_id
        ]
        widget_keys = [
            {
                "project_id": key[0],
                "host_id": key[1],
                "source_node_id": _widget_sources.get(key, ""),
                "fields": sorted(_widget_state.get(key, {}).keys()),
            }
            for key in sorted(_widget_state.keys())
            if key[0] == project_id
        ]
        matching_host = _public_host(_canvas_hosts.get(requested_key))
        subscriber_count = len(_subscribers.get(project_id, set()))
        recent_events = [
            dict(event)
            for event in _debug_events
            if event.get("project_id") == project_id
        ][-20:]
    _schedule_events(events)
    return {
        "project_id": project_id,
        "requested": {
            "host_id": requested_key[1],
            "source_node_id": _clean_id(source_node_id),
        },
        "matching_host": matching_host,
        "canvas_host_connected": bool(matching_host),
        "hosts": [host for host in hosts if host],
        "owners": [owner for owner in owners if owner],
        "widget_keys": widget_keys,
        "subscriber_count": subscriber_count,
        "recent_events": recent_events,
    }


async def subscribe(project_id: str, ws: web.WebSocketResponse) -> None:
    async with _lock:
        _subscribers[project_id].add(ws)


async def unsubscribe(project_id: str, ws: web.WebSocketResponse) -> None:
    async with _lock:
        subscribers = _subscribers.get(project_id)
        if not subscribers:
            return
        subscribers.discard(ws)
        if not subscribers:
            _subscribers.pop(project_id, None)


# High-rate status events kept out of the ws_broadcast diagnostic ring, which
# would otherwise be flooded by progress and lose the events it exists to trace.
_UNRECORDED_BROADCAST_TYPES = frozenset({"generation_status"})


async def broadcast_project_event(project_id: str, event: dict[str, Any]) -> None:
    async with _lock:
        subscribers = list(_subscribers.get(project_id, set()))
    # #36 diagnostic: every fan-out of a project/widget event to WS subscribers.
    # Pairs with `project_saved` events to trace writer -> broadcast -> client refresh.
    event_type = str(event.get("type", "") if isinstance(event, dict) else "")
    try:
        if event_type not in _UNRECORDED_BROADCAST_TYPES:
            record_diag_event(
                "ws_broadcast",
                project_id=project_id,
                event_type=event_type,
                subscriber_count=len(subscribers),
                event_modified_at=str(event.get("modified_at", "") if isinstance(event, dict) else ""),
            )
    except Exception:
        logger.debug("broadcast_project_event failed to emit diag event", exc_info=True)
    if not subscribers:
        return
    dead = []
    for ws in subscribers:
        try:
            if ws.closed:
                dead.append(ws)
                continue
            await ws.send_json(event)
        except Exception:
            dead.append(ws)
    if dead:
        async with _lock:
            for ws in dead:
                _subscribers.get(project_id, set()).discard(ws)


def project_event_aliases(project: Any) -> list[str]:
    """The ids a project's events go to: its canonical id and its folder name.

    Sync sockets subscribe under either, so every project event is sent to both.
    """
    canonical = str(getattr(project, "project_id", "") or "")
    project_dir = str(getattr(project, "project_dir", "") or "")
    folder = os.path.basename(os.path.normpath(project_dir)) if project_dir else ""
    return [alias for alias in dict.fromkeys((canonical, folder)) if alias]


def schedule_project_event(project_id: str, event: dict[str, Any]) -> None:
    if not project_id:
        return
    loop = _event_loop
    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcast_project_event(project_id, event), loop)


async def sweep_stale_sessions_once() -> int:
    events: list[tuple[str, dict[str, Any]]] = []
    evicted = 0
    sweep_started_at = time.monotonic() * 1000.0
    sessions_count = 0
    hosts_count = 0
    async with _lock:
        sessions_count = len(_sessions)
        hosts_count = len(_canvas_hosts)
        for key in list(_sessions.keys()):
            evicted += _advance_owner_lifecycle_locked(key, events)
        for key, host in list(_canvas_hosts.items()):
            if not _is_host_stale(host):
                continue
            last_seen = float(host.get("last_seen", 0.0) or 0.0)
            _canvas_hosts.pop(key, None)
            _clear_controls_locked(key)
            _record_diag_event(
                "canvas_host_evict",
                key,
                trigger="sweeper",
                session_id=host.get("session_id", ""),
                source_node_id=host.get("source_node_id", ""),
                last_seen_age=_now() - last_seen,
                ttl=CANVAS_HOST_TTL_SECONDS,
            )
            events.append((key[0], _host_presence_event(key, None)))
            evicted += 1
    if _DEBUG_SESSION_ENABLED:
        _record_diag_event(
            "sweeper_run", None,
            evicted=evicted,
            sessions_scanned=sessions_count,
            hosts_scanned=hosts_count,
            duration_ms=time.monotonic() * 1000.0 - sweep_started_at,
        )
    await _emit_events(events)
    return evicted


async def _sweep_loop() -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        await sweep_stale_sessions_once()
