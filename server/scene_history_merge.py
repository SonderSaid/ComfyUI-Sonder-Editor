"""Pure three-way scene-history merge and short-lived restore receipts."""

from __future__ import annotations

import copy
import hashlib
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


_MISSING = object()
_DURABLE_RECEIPT_LIMIT = 128


class SceneMergeConflict(Exception):
    """The stored scene changed on a value the history operation must reverse."""

    def __init__(self, conflicts: list[dict]):
        self.conflicts = copy.deepcopy(conflicts)
        paths = ", ".join(str(item.get("path") or "scene") for item in conflicts[:3])
        suffix = "" if len(conflicts) <= 3 else f" and {len(conflicts) - 3} more"
        super().__init__(f"Scene changed elsewhere at {paths}{suffix}.")


def _same(left: Any, right: Any) -> bool:
    if left is _MISSING or right is _MISSING:
        return left is right
    return left == right


def _describe(value: Any) -> Any:
    if value is _MISSING:
        return {"absent": True}
    if isinstance(value, dict):
        return {key: _describe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_describe(item) for item in value]
    if isinstance(value, tuple):
        return [_describe(item) for item in value]
    return copy.deepcopy(value)


def _conflict(conflicts: list[dict], path: str, base: Any, target: Any,
              stored: Any) -> None:
    conflicts.append({
        "path": path,
        "base": _describe(base),
        "target": _describe(target),
        "stored": _describe(stored),
    })


def _apply_value(result: dict, key: str, target: Any) -> None:
    if target is _MISSING:
        result.pop(key, None)
    else:
        result[key] = copy.deepcopy(target)


def _merge_value(result: dict, key: str, base: dict, target: dict, stored: dict,
                 conflicts: list[dict], path: str | None = None, *,
                 write: bool = True) -> None:
    base_value = base.get(key, _MISSING)
    target_value = target.get(key, _MISSING)
    if base_value is _MISSING and target_value is _MISSING:
        return
    stored_value = stored.get(key, _MISSING)
    if _same(target_value, base_value):
        return
    # Reapplying the same three-way patch after a lost receipt/server restart
    # is a durable no-op when stored already equals the requested target.
    if _same(stored_value, target_value):
        return
    if _same(stored_value, base_value):
        if write:
            _apply_value(result, key, target_value)
        return
    _conflict(conflicts, path or key, base_value, target_value, stored_value)


def _bundle(document: dict, keys: tuple[str, ...]) -> dict:
    return {key: document.get(key, _MISSING) for key in keys}


def _same_bundle(left: dict, right: dict) -> bool:
    return all(_same(left[key], right[key]) for key in left)


def _merge_bundle(result: dict, keys: tuple[str, ...], base: dict, target: dict,
                  stored: dict, conflicts: list[dict], path: str) -> None:
    base_value = _bundle(base, keys)
    target_value = _bundle(target, keys)
    if all(value is _MISSING for value in base_value.values()) and all(
            value is _MISSING for value in target_value.values()):
        return
    stored_value = _bundle(stored, keys)
    if _same_bundle(target_value, base_value):
        return
    if _same_bundle(stored_value, target_value):
        return
    if _same_bundle(stored_value, base_value):
        for key in keys:
            _apply_value(result, key, target_value[key])
        return
    _conflict(conflicts, path, base_value, target_value, stored_value)


def _index_members(raw: Any, key: str, path: str,
                   conflicts: list[dict]) -> tuple[list[str], dict[str, dict]]:
    order: list[str] = []
    indexed: dict[str, dict] = {}
    if not isinstance(raw, list):
        _conflict(conflicts, path, [], [], raw)
        return order, indexed
    for index, member in enumerate(raw):
        if not isinstance(member, dict):
            _conflict(conflicts, f"{path}[{index}]", {}, {}, member)
            continue
        member_id = str(member.get(key) or "")
        if not member_id:
            _conflict(conflicts, f"{path}[{index}].{key}", "stable id", "stable id", member_id)
            continue
        if member_id in indexed:
            _conflict(conflicts, f"{path}[{member_id}]", "unique id", "unique id", "duplicate id")
            continue
        order.append(member_id)
        indexed[member_id] = member
    return order, indexed


def _merge_dict_fields(base_member: dict, target_member: dict, stored_member: dict,
                       conflicts: list[dict], path: str, *,
                       excluded: frozenset[str] = frozenset()) -> dict:
    result = copy.deepcopy(stored_member)
    for field in set(base_member) | set(target_member):
        if field in excluded:
            continue
        _merge_value(result, field, base_member, target_member, stored_member,
                     conflicts, f"{path}.{field}")
    return result


def _merge_mapping(result_member: dict, field: str, base_member: dict,
                   target_member: dict, stored_member: dict,
                   conflicts: list[dict], path: str) -> None:
    base_map = base_member.get(field, _MISSING)
    target_map = target_member.get(field, _MISSING)
    if base_map is _MISSING and target_map is _MISSING:
        return
    stored_map = stored_member.get(field, _MISSING)
    if not all(value is _MISSING or isinstance(value, dict)
               for value in (base_map, target_map, stored_map)):
        _merge_value(result_member, field, base_member, target_member,
                     stored_member, conflicts, path)
        return
    base_values = {} if base_map is _MISSING else base_map
    target_values = {} if target_map is _MISSING else target_map
    stored_values = {} if stored_map is _MISSING else stored_map
    merged = copy.deepcopy(stored_values)
    for key in set(base_values) | set(target_values):
        _merge_value(merged, key, base_values, target_values, stored_values,
                     conflicts, f"{path}.{key}")
    if target_map is _MISSING and not merged:
        result_member.pop(field, None)
    else:
        result_member[field] = merged


def _merge_collection(result: dict, field: str, key: str, base: dict,
                      target: dict, stored: dict, conflicts: list[dict], *,
                      per_field: bool, excluded: frozenset[str] = frozenset(),
                      mapping_fields: tuple[str, ...] = ()) -> None:
    base_raw = base.get(field, _MISSING)
    target_raw = target.get(field, _MISSING)
    if base_raw is _MISSING and target_raw is _MISSING:
        return
    stored_raw = stored.get(field, _MISSING)
    if not all(value is _MISSING or isinstance(value, list)
               for value in (base_raw, target_raw, stored_raw)):
        _merge_value(result, field, base, target, stored, conflicts, field)
        return
    base_list = [] if base_raw is _MISSING else base_raw
    target_list = [] if target_raw is _MISSING else target_raw
    stored_list = [] if stored_raw is _MISSING else stored_raw
    base_order, base_items = _index_members(base_list, key, field, conflicts)
    target_order, target_items = _index_members(target_list, key, field, conflicts)
    stored_order, stored_items = _index_members(stored_list, key, field, conflicts)
    merged_items = {member_id: copy.deepcopy(member)
                    for member_id, member in stored_items.items()}

    for member_id in base_order + [value for value in target_order if value not in base_items]:
        base_member = base_items.get(member_id, _MISSING)
        target_member = target_items.get(member_id, _MISSING)
        stored_member = stored_items.get(member_id, _MISSING)
        member_path = f"{field}[{member_id}]"
        if _same(target_member, base_member):
            continue
        if _same(stored_member, target_member):
            continue
        if base_member is _MISSING or target_member is _MISSING:
            if _same(stored_member, base_member):
                if target_member is _MISSING:
                    merged_items.pop(member_id, None)
                else:
                    merged_items[member_id] = copy.deepcopy(target_member)
                continue
            _conflict(conflicts, member_path, base_member, target_member, stored_member)
            continue
        if stored_member is _MISSING:
            _conflict(conflicts, member_path, base_member, target_member, stored_member)
            continue
        if not per_field:
            if _same(stored_member, base_member):
                merged_items[member_id] = copy.deepcopy(target_member)
            else:
                _conflict(conflicts, member_path, base_member, target_member, stored_member)
            continue
        merged_member = _merge_dict_fields(
            base_member, target_member, stored_member, conflicts, member_path,
            excluded=excluded | frozenset({key}) | frozenset(mapping_fields),
        )
        for mapping_field in mapping_fields:
            _merge_mapping(merged_member, mapping_field, base_member,
                           target_member, stored_member, conflicts,
                           f"{member_path}.{mapping_field}")
        merged_items[member_id] = merged_member

    merged_order = [member_id for member_id in stored_order if member_id in merged_items]
    merged_order.extend(member_id for member_id in target_order
                        if member_id in merged_items and member_id not in merged_order)
    result[field] = [merged_items[member_id] for member_id in merged_order]


def _validate_duration_sentinels(base: dict, target: dict, stored: dict,
                                 conflicts: list[dict]) -> None:
    base_duration = base.get("duration_frames", _MISSING)
    target_duration = target.get("duration_frames", _MISSING)
    if _same(base_duration, target_duration):
        return
    if _same(stored.get("duration_frames", _MISSING), target_duration):
        return
    _, base_items = _index_members(base.get("reference_items", []),
                                   "reference_item_id", "reference_items", conflicts)
    _, target_items = _index_members(target.get("reference_items", []),
                                     "reference_item_id", "reference_items", conflicts)
    _, stored_items = _index_members(stored.get("reference_items", []),
                                     "reference_item_id", "reference_items", conflicts)
    for member_id, stored_member in stored_items.items():
        if stored_member.get("end_frame") != -1:
            continue
        base_member = base_items.get(member_id)
        target_member = target_items.get(member_id)
        if (base_member is None or target_member is None
                or base_member.get("end_frame") != -1
                or target_member.get("end_frame") != -1):
            _conflict(conflicts, f"reference_items[{member_id}].effective_end_frame",
                      base_member if base_member is not None else _MISSING,
                      target_member if target_member is not None else _MISSING,
                      stored_member)

    _, base_guides = _index_members(base.get("guide_frames", []),
                                    "guide_id", "guide_frames", conflicts)
    _, target_guides = _index_members(target.get("guide_frames", []),
                                      "guide_id", "guide_frames", conflicts)
    _, stored_guides = _index_members(stored.get("guide_frames", []),
                                      "guide_id", "guide_frames", conflicts)
    for member_id, stored_member in stored_guides.items():
        if stored_member.get("frame_index") != -1:
            continue
        base_member = base_guides.get(member_id)
        target_member = target_guides.get(member_id)
        if (base_member is None or target_member is None
                or base_member.get("frame_index") != -1
                or target_member.get("frame_index") != -1):
            _conflict(conflicts, f"guide_frames[{member_id}].effective_frame_index",
                      base_member if base_member is not None else _MISSING,
                      target_member if target_member is not None else _MISSING,
                      stored_member)


def _validate_lane_shrink_members(result: dict, base: dict, target: dict,
                                  stored: dict, conflicts: list[dict]) -> None:
    """Refuse a history-owned shrink that would strand concurrent members."""
    specs = (
        ("video_lane_count", "clips", "clip_id", "track_index", "render"),
        ("motion_driver_lane_count", "clips", "clip_id", "track_index",
         "motion_driver"),
        ("audio_lane_count", "audio_tracks", "track_id", "lane_index", None),
        ("reference_lane_count", "reference_items", "reference_item_id",
         "lane_index", None),
    )
    for count_field, collection, id_field, index_field, role in specs:
        try:
            base_count = int(base.get(count_field))
            target_count = int(target.get(count_field))
            stored_count = int(stored.get(count_field))
            result_count = int(result.get(count_field))
        except (TypeError, ValueError):
            continue
        # Only guard the shrink this merge is applying. Existing over-cap data
        # remains tolerated when the operation does not own a lane reduction.
        if (base_count <= target_count or stored_count != base_count
                or result_count != target_count):
            continue

        base_items = {
            str(member.get(id_field) or ""): member
            for member in base.get(collection, [])
            if isinstance(member, dict) and member.get(id_field)
        }
        target_items = {
            str(member.get(id_field) or ""): member
            for member in target.get(collection, [])
            if isinstance(member, dict) and member.get(id_field)
        }
        stored_items = {
            str(member.get(id_field) or ""): member
            for member in stored.get(collection, [])
            if isinstance(member, dict) and member.get(id_field)
        }
        for member in result.get(collection, []):
            if not isinstance(member, dict):
                continue
            member_role = str(member.get("role") or "render")
            if role is not None and member_role != role:
                continue
            member_id = str(member.get(id_field) or "")
            try:
                lane_index = int(member.get(index_field, 0))
            except (TypeError, ValueError):
                continue
            if not member_id or lane_index < target_count:
                continue
            target_member = target_items.get(member_id)
            try:
                target_index = int(target_member.get(index_field, 0)) \
                    if target_member is not None else -1
            except (TypeError, ValueError):
                target_index = -1
            if target_member is not None and target_index >= target_count:
                continue
            _conflict(
                conflicts,
                f"{collection}[{member_id}].{index_field}",
                base_items.get(member_id, _MISSING),
                target_member if target_member is not None else _MISSING,
                stored_items.get(member_id, member),
            )


def merge_scene_history(base: dict, target: dict, stored: dict) -> dict:
    """Apply the base→target reversal onto stored without touching unrelated work.

    ``base`` is the authoritative scene after the original operation, ``target``
    is the scene before it, and ``stored`` is current durable state. The function
    performs no I/O and deliberately uses ordinary recursive Python equality.
    """
    if not all(isinstance(value, dict) for value in (base, target, stored)):
        raise TypeError("Scene history merge inputs must be objects")

    result = copy.deepcopy(stored)
    conflicts: list[dict] = []

    for field in (
        "name", "duration_frames", "generation_params",
        "prompt_context_profile_id", "prompt_context_profile_config",
        "active_minimax_h3_setup_id", "guide_track_config",
        "prompt_track_config", "global_prompt_track_config",
    ):
        _merge_value(result, field, base, target, stored, conflicts)

    for field in ("width", "height"):
        _merge_value(result, field, base, target, stored, conflicts,
                     write=False)
    base_fps = base.get("fps", _MISSING)
    target_fps = target.get("fps", _MISSING)
    if not _same(base_fps, target_fps):
        # FPS mutations retime every frame-based field before returning their
        # scene. Restoring those fields while retaining stored FPS corrupts the
        # timebase, so history must refuse until geometry-aware undo exists.
        _conflict(conflicts, "fps", base_fps, target_fps,
                  stored.get("fps", _MISSING))

    _merge_mapping(result, "global_channel_docs", base, target, stored,
                   conflicts, "global_channel_docs")

    collection_specs = (
        ("clips", "clip_id", True, frozenset(), ()),
        ("audio_tracks", "track_id", True, frozenset(), ()),
        ("guide_frames", "guide_id", True, frozenset(), ()),
        ("reference_items", "reference_item_id", True, frozenset(), ()),
        ("prompt_sections", "prompt_id", True,
         frozenset({"prompt", "channels"}), ("channel_docs",)),
        ("linked_item_groups", "group_id", False, frozenset(), ()),
        ("global_attachments", "attachment_id", False, frozenset(), ()),
        ("minimax_h3_conditioning_setups", "setup_id", False, frozenset(), ()),
    )
    for field, key, per_field, excluded, mapping_fields in collection_specs:
        _merge_collection(result, field, key, base, target, stored, conflicts,
                          per_field=per_field, excluded=excluded,
                          mapping_fields=mapping_fields)

    for path, keys in (
        ("video_lane_family", ("video_lane_count", "video_lane_configs")),
        ("motion_driver_lane_family", (
            "motion_driver_lane_count", "motion_driver_lane_configs")),
        ("audio_lane_family", ("audio_lane_count", "audio_lane_configs")),
        ("reference_lane_family", (
            "reference_lane_count", "reference_lane_configs",
            "reference_lane_recipes")),
    ):
        _merge_bundle(result, keys, base, target, stored, conflicts, path)

    _validate_lane_shrink_members(result, base, target, stored, conflicts)
    _validate_duration_sentinels(base, target, stored, conflicts)
    if conflicts:
        raise SceneMergeConflict(conflicts)
    return result


def _restore_token_digest(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def has_durable_scene_restore_receipt(project, token: str, project_id: str,
                                      scene_id: str) -> bool:
    """Return whether the project's atomic ledger proves this restore committed."""
    token_digest = _restore_token_digest(token)
    if not token or not token_digest:
        return False
    for receipt in getattr(project, "_scene_restore_receipts", []) or []:
        if not isinstance(receipt, dict):
            continue
        if (receipt.get("token_digest") == token_digest
                and str(receipt.get("project_id") or "") == str(project_id or "")
                and str(receipt.get("scene_id") or "") == str(scene_id or "")):
            return True
    return False


def remember_durable_scene_restore_receipt(project, token: str, project_id: str,
                                           scene_id: str) -> None:
    """Stage a bounded hashed commit marker for the same save as the scene."""
    token_digest = _restore_token_digest(token)
    if not token or not token_digest:
        raise ValueError("Scene restore token is required")
    receipts = [
        copy.deepcopy(item)
        for item in (getattr(project, "_scene_restore_receipts", []) or [])
        if isinstance(item, dict) and item.get("token_digest") != token_digest
    ]
    receipts.append({
        "token_digest": token_digest,
        "project_id": str(project_id or ""),
        "scene_id": str(scene_id or ""),
        "committed_at": time.time(),
    })
    project._scene_restore_receipts = receipts[-_DURABLE_RECEIPT_LIMIT:]


@dataclass
class SceneRestoreReceipt:
    project_id: str
    scene_id: str
    created_at: float
    status: str = "pending"
    payload: dict | None = None


class SceneRestoreReceiptStore:
    """Bounded in-memory receipts used only to reconcile lost HTTP responses."""

    def __init__(self, *, max_entries: int = 2048, ttl_seconds: float = 3600.0):
        self.max_entries = max(1, int(max_entries))
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self._entries: OrderedDict[str, SceneRestoreReceipt] = OrderedDict()

    def _prune(self) -> None:
        cutoff = time.monotonic() - self.ttl_seconds
        for token in list(self._entries):
            if self._entries[token].created_at < cutoff:
                self._entries.pop(token, None)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def issue(self, project_id: str, scene_id: str) -> str:
        self._prune()
        token = secrets.token_urlsafe(24)
        self._entries[token] = SceneRestoreReceipt(
            project_id=str(project_id or ""),
            scene_id=str(scene_id or ""),
            created_at=time.monotonic(),
        )
        self._prune()
        return token

    def get(self, token: str, project_id: str, scene_id: str) -> SceneRestoreReceipt | None:
        self._prune()
        receipt = self._entries.get(str(token or ""))
        if receipt is None:
            return None
        if (receipt.project_id != str(project_id or "")
                or receipt.scene_id != str(scene_id or "")):
            return None
        self._entries.move_to_end(str(token))
        return receipt

    def finish(self, token: str, project_id: str, scene_id: str, *,
               status: str, payload: dict) -> SceneRestoreReceipt | None:
        receipt = self.get(token, project_id, scene_id)
        if receipt is None:
            return None
        receipt.status = str(status)
        receipt.payload = copy.deepcopy(payload)
        return receipt
