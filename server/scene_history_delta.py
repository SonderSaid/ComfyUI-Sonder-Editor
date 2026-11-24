"""What a scene was before one commit, as a delta against what it became.

An Undo step needs both sides of its own write from the same commit. The editor's
copy from before the gesture is not that: it lacks anything the server saved that
the editor had not read (a take, another process's write), and it holds paint of
writes the server may still refuse.

The delta is small because most members of a scene do not change; a second full
scene would double the response. ``web/js/scene_history_delta.js`` rebuilds the
before-scene from it.

``capture`` avoids a second ``Scene.to_dict``, which copies every clip's provenance
twice. ``marshal`` serves only as a fast in-memory copy: its bytes never leave the
process, and nothing read from disk or a request is ever passed to ``marshal.loads``.
"""

from __future__ import annotations

import copy
import logging
import marshal

from .scene_history_merge import MERGED_WRITE_FIELDS
from .timeline_state import _overlay_unknown_keyed_records

logger = logging.getLogger(__name__)

DELTA_VERSION = 1
# The members that carry generation provenance, or are cheap to treat the same way.
_CAPTURED_MEMBERS = {"clips": "clip_id", "audio_tracks": "track_id"}
_MEMBER_KEYS = {spec[0]: spec[1] for spec in MERGED_WRITE_FIELDS["collection"]}
_MARSHAL_VERSION = 2  # no object references, so equal values are mostly equal bytes


def empty_delta() -> dict:
    return {"v": DELTA_VERSION, "fields": {}, "absent": [], "members": {}}


def _keyed(members, key) -> dict | None:
    """Members by id, or None where ids cannot address them."""
    if not isinstance(members, list):
        return None
    indexed = {}
    for member in members:
        member_id = member.get(key) if isinstance(member, dict) else None
        if not isinstance(member_id, str) or not member_id or member_id in indexed:
            return None
        indexed[member_id] = member
    return indexed


def _member_changes(before: dict, after: dict) -> tuple[dict, list]:
    changed = {field: value for field, value in before.items()
               if field not in after or after[field] != value}
    return changed, [field for field in after if field not in before]


def _add_members(delta: dict, name: str, order: list, after_ids: list,
                 changes: dict, removed: list) -> None:
    fields = {member_id: changed for member_id, (changed, _) in changes.items() if changed}
    absent = {member_id: names for member_id, (_, names) in changes.items() if names}
    if order == after_ids and not fields and not absent and not removed:
        return
    delta["members"][name] = {"order": order, "fields": fields, "absent": absent,
                              "removed": removed}


def _add_fields(delta: dict, before: dict, after: dict, skip=()) -> None:
    for name, value in before.items():
        if name in skip or (name in after and after[name] == value):
            continue
        key = _MEMBER_KEYS.get(name)
        before_members = _keyed(value, key) if key else None
        after_members = _keyed(after.get(name), key) if key else None
        if before_members is None or after_members is None:
            delta["fields"][name] = value
            continue
        _add_members(
            delta, name, list(before_members), list(after_members),
            {member_id: _member_changes(member, after_members[member_id])
             for member_id, member in before_members.items() if member_id in after_members},
            [member for member_id, member in before_members.items()
             if member_id not in after_members])
    delta["absent"] = [name for name in after if name not in before]


def delta_between(before: dict, after: dict) -> dict:
    """The delta from two ``Scene.to_dict`` results."""
    delta = empty_delta()
    _add_fields(delta, before, after)
    return delta


def is_empty(delta: dict) -> bool:
    return not delta["fields"] and not delta["absent"] and not delta["members"]


class _Capture:
    def __init__(self, scene):
        raw = scene._raw_data if isinstance(scene._raw_data, dict) else {}
        self.raw_members = {name: raw.get(name) for name in _CAPTURED_MEMBERS}
        self.members = {}
        for name, key in _CAPTURED_MEMBERS.items():
            self.members[name] = [
                (member_id, marshal.dumps(member, _MARSHAL_VERSION))
                for member in map(_to_dict, getattr(scene, name))
                for member_id in (member.get(key),)]
        light = copy.copy(scene)
        for name in _CAPTURED_MEMBERS:
            setattr(light, name, [])
        light._raw_data = {name: value for name, value in raw.items()
                           if name not in _CAPTURED_MEMBERS}
        # Detached: `_preserve_scene_unknown_fields` copies every value it returns.
        self.rest = light.to_dict()


def _to_dict(member) -> dict:
    return member.to_dict()


def _addressable(ids) -> bool:
    return all(isinstance(value, str) and value for value in ids) and len(set(ids)) == len(ids)


def capture(scene):
    """The scene as it is now, for a later ``delta_from_capture``; None if it cannot be
    captured. Never raises: a save must not fail for its history."""
    try:
        return _Capture(scene)
    except Exception:  # noqa: BLE001 - see docstring
        logger.warning("A scene's state before a change could not be kept; its Undo step "
                       "falls back to the editor's own copy", exc_info=True)
        return None


def _captured_delta(captured: _Capture, scene, after: dict) -> dict:
    delta = empty_delta()
    _add_fields(delta, captured.rest, after, skip=_CAPTURED_MEMBERS)
    for name, key in _CAPTURED_MEMBERS.items():
        members = captured.members[name]
        ids = [member_id for member_id, _ in members]
        current = {}
        after_ids = []
        for member in map(_to_dict, getattr(scene, name)):
            after_ids.append(member.get(key))
            current.setdefault(member.get(key), member)

        def whole(wanted):
            members = [marshal.loads(data) for data in wanted]
            # The overlay copies every value again; only a record with a field no model
            # knows needs it.
            raw = captured.raw_members[name]
            known = {member.get(key): member for member in members}
            extra = [row for row in (raw if isinstance(raw, list) else [])
                     if isinstance(row, dict)
                     and row.keys() - known.get(row.get(key), row).keys()]
            wanted_ids = {row.get(key) for row in extra}
            return [_overlay_unknown_keyed_records(extra, [member], key)[0]
                    if member.get(key) in wanted_ids else member for member in members]

        if not _addressable(ids) or not _addressable(after_ids):
            before = whole([data for _, data in members])
            if before != after.get(name):
                delta["fields"][name] = before
            continue
        changes, removed = {}, []
        for member_id, data in members:
            member = current.get(member_id)
            if member is None:
                removed.append(data)
            elif marshal.dumps(member, _MARSHAL_VERSION) != data:
                changes[member_id] = _member_changes(marshal.loads(data), member)
        _add_members(delta, name, ids, after_ids, changes, whole(removed))
    return delta


def delta_from_capture(captured, scene, after: dict) -> dict | None:
    """The delta from a capture to ``scene``, whose ``to_dict`` is ``after``; None when
    there is none to give. Never raises."""
    if captured is None:
        return None
    try:
        return _captured_delta(captured, scene, after)
    except Exception:  # noqa: BLE001 - as capture
        logger.warning("A scene's state before a change could not be compared; its Undo "
                       "step falls back to the editor's own copy", exc_info=True)
        return None


def delta_between_or_none(before: dict, after: dict) -> dict | None:
    try:
        return delta_between(before, after)
    except Exception:  # noqa: BLE001 - as capture
        logger.warning("A restored scene could not be compared with the one before it",
                       exc_info=True)
        return None
