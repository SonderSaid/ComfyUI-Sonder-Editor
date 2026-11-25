"""Pure three-way scene-history merge and short-lived restore receipts."""

from __future__ import annotations

import copy
import hashlib
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, NamedTuple


_MISSING = object()
_DURABLE_RECEIPT_LIMIT = 128


# The token response and merge executor share this authority, never a JS mirror.
MERGED_WRITE_FIELDS = {
    "scalar": (
        "name", "duration_frames", "generation_params",
        "prompt_context_profile_id", "prompt_context_profile_config",
        "guide_track_config",
        "prompt_track_config", "global_prompt_track_config",
    ),
    "mapping": ("global_channel_docs",),
    "collection": (
        ("clips", "clip_id", True, frozenset(), ()),
        ("audio_tracks", "track_id", True, frozenset(), ()),
        ("guide_frames", "guide_id", True, frozenset(), ()),
        ("reference_items", "reference_item_id", True, frozenset(), ()),
        ("prompt_sections", "prompt_id", True,
         frozenset({"prompt", "channels"}), ("channel_docs",)),
        ("linked_item_groups", "group_id", False, frozenset(), ()),
        ("global_attachments", "attachment_id", False, frozenset(), ()),
    ),
    "bundle": (
        ("video_lane_family", ("video_lane_count", "video_lane_configs")),
        ("motion_driver_lane_family", (
            "motion_driver_lane_count", "motion_driver_lane_configs")),
        ("audio_lane_family", ("audio_lane_count", "audio_lane_configs")),
        ("reference_lane_family", (
            "reference_lane_count", "reference_lane_configs",
            "reference_lane_recipes")),
    ),
}
MERGED_DERIVED_FIELDS = ("global_channels", "prompt")


def scene_history_write_fields() -> list[str]:
    return [*MERGED_WRITE_FIELDS["scalar"], *MERGED_WRITE_FIELDS["mapping"],
            *(spec[0] for spec in MERGED_WRITE_FIELDS["collection"]),
            *(field for _, fields in MERGED_WRITE_FIELDS["bundle"] for field in fields)]


class SceneMergeConflict(Exception):
    """The stored scene changed on a value the history operation must reverse."""

    def __init__(self, conflicts: list[dict]):
        self.conflicts = copy.deepcopy(conflicts)
        paths = ", ".join(str(item.get("path") or "scene") for item in conflicts[:3])
        suffix = "" if len(conflicts) <= 3 else f" and {len(conflicts) - 3} more"
        reload = (" Its lanes cannot be told apart: reload the editor."
                  if any(item.get("code") == "lane_identity_missing" for item in conflicts)
                  else "")
        super().__init__(f"Scene changed elsewhere at {paths}{suffix}.{reload}")


def _same(left: Any, right: Any) -> bool:
    if left is _MISSING or right is _MISSING:
        return left is right
    return left == right


def _describe(value: Any) -> Any:
    if value is _MISSING:
        return {"absent": True}
    if isinstance(value, LaneRef):
        return {"lane": value.lane_id} if value.lane_id is not None else {"past_lanes": value.past}
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


# Each lane family's members: (count field, collection, id, lane index, clip role).
_LANE_MEMBERS = (
    ("video_lane_count", "clips", "clip_id", "track_index", "render"),
    ("motion_driver_lane_count", "clips", "clip_id", "track_index", "motion_driver"),
    ("audio_lane_count", "audio_tracks", "track_id", "lane_index", None),
    ("reference_lane_count", "reference_items", "reference_item_id", "lane_index", None),
)
_LANE_COLLECTIONS = {
    collection: (id_field, index_field, {
        row_role: row_count for row_count, row_collection, _, _, row_role in _LANE_MEMBERS
        if row_collection == collection})
    for _, collection, id_field, index_field, _ in _LANE_MEMBERS
}


def _lane_families() -> dict:
    """(conflict path, fields) of each lane family by its count field."""
    return {keys[0]: (path, keys) for path, keys in MERGED_WRITE_FIELDS["bundle"]}


class LaneRef(NamedTuple):
    """Where a timeline item stands, while a step is merged: on the lane with this id,
    or this far past its family's last lane (an item tolerated on a missing lane)."""
    family: str
    lane_id: str | None
    past: int = 0

    def __deepcopy__(self, memo):
        return self


class _Lanes:
    """One side's lane family, or an unidentified one (``ids`` is None)."""

    def __init__(self, document: dict, keys: tuple[str, ...]):
        count_key, configs_key, *recipe_keys = keys
        self.values = {key: document.get(key, _MISSING) for key in keys}
        self.ids, self.index, self.configs, self.recipes = None, {}, {}, {}
        self.count = document.get(count_key, _MISSING)
        configs = document.get(configs_key)
        recipes = document.get(recipe_keys[0]) if recipe_keys else None
        if (isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 0
                or not isinstance(configs, list) or len(configs) < self.count
                or (recipe_keys and (not isinstance(recipes, list) or len(recipes) < self.count))):
            return
        owners = recipes if recipe_keys else configs
        ids = [str(row.get("lane_id") or "") if isinstance(row, dict) else ""
               for row in owners[:self.count]]
        if (not all(ids) or len(set(ids)) != len(ids)
                or not all(isinstance(row, dict) for row in configs[:self.count])):
            return
        self.ids = ids
        self.index = {lane_id: index for index, lane_id in enumerate(ids)}
        self.configs = dict(zip(ids, configs))
        self.recipes = dict(zip(ids, recipes)) if recipe_keys else {}
        self.spare_configs = configs[self.count:]
        self.spare_recipes = recipes[self.count:] if recipe_keys else []

    def lane(self, lane_id):
        """Everything one lane holds, or ``_MISSING``."""
        if lane_id not in self.index:
            return _MISSING
        return {"config": self.configs[lane_id], "recipe": self.recipes.get(lane_id)}

    def without_config_ids(self) -> dict:
        def plain(value):
            return [{key: item for key, item in row.items() if key != "lane_id"}
                    if isinstance(row, dict) else row for row in value]
        return {key: plain(value) if key.endswith("_configs") and isinstance(value, list)
                else value for key, value in self.values.items()}


def lane_identities(document: dict, count_key: str) -> list[str] | None:
    """The ids of one family's lanes in order, or None when they do not name them."""
    return _Lanes(document, _lane_families()[count_key][1]).ids


def _lane_family_of(collection: str, member: dict) -> str | None:
    families = _LANE_COLLECTIONS[collection][2]
    if None in families:
        return families[None]
    # An unknown role belongs to neither family, as the writers that shift lane
    # indices treat it: its number is left alone.
    return families.get(str(member.get("role") or "render"))


def _with_lane_refs(document: dict, families: dict[str, _Lanes]) -> dict:
    """``document`` with the items of ``families`` naming lanes instead of numbers.

    Only the rows that change are copied, and only one level: the rest is shared."""
    result = dict(document)
    for collection, (_, index_field, _) in _LANE_COLLECTIONS.items():
        rows = document.get(collection)
        if not isinstance(rows, list):
            continue
        named = []
        for row in rows:
            family = _lane_family_of(collection, row) if isinstance(row, dict) else None
            lanes = families.get(family)
            index = row.get(index_field) if lanes is not None else None
            if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
                ref = (LaneRef(family, lanes.ids[index]) if index < lanes.count
                       else LaneRef(family, None, index - lanes.count))
                row = {**row, index_field: ref}
            named.append(row)
        result[collection] = named
    return result


def _keep_unmoved_past_items(base: dict, target: dict, base_raw: dict, target_raw: dict) -> None:
    """An item past the last lane on both sides, at one number, was not moved: only the
    lane count under it changed."""
    for collection, (id_field, index_field, _) in _LANE_COLLECTIONS.items():
        def rows(document):
            members = document.get(collection)
            return {str(row.get(id_field) or ""): (position, row) for position, row in enumerate(members)
                    if isinstance(row, dict)} if isinstance(members, list) else {}
        base_rows, raw_rows = rows(base), rows(base_raw)
        target_raw_rows = rows(target_raw)
        for member_id, (position, row) in rows(target).items():
            if member_id not in base_rows:
                continue
            before, after = base_rows[member_id][1].get(index_field), row.get(index_field)
            if (isinstance(before, LaneRef) and isinstance(after, LaneRef)
                    and before.lane_id is None and after.lane_id is None
                    and before.family == after.family
                    and raw_rows[member_id][1].get(index_field)
                    == target_raw_rows[member_id][1].get(index_field)):
                target[collection][position] = {**row, index_field: before}


def _merge_lane_order(base: _Lanes, target: _Lanes, stored: _Lanes, kept: list[str],
                      conflicts: list[dict], path: str) -> list[str]:
    """Stored's order of ``kept`` lanes, with the step's own reorder of them applied."""
    common = [lane_id for lane_id in kept if lane_id in base.index and lane_id in target.index]
    before = sorted(common, key=base.index.get)
    after = sorted(common, key=target.index.get)
    if before == after or common == after:
        return kept
    if common != before:
        _conflict(conflicts, f"{path}.order", before, after, common)
        return kept
    moved = iter(after)
    places = set(common)
    return [next(moved) if lane_id in places else lane_id for lane_id in kept]


def _place_returning_lanes(order: list[str], target: _Lanes) -> list[str]:
    """Put each lane only target has beside the neighbour it had there."""
    order = list(order)
    present = set(order)
    for index, lane_id in enumerate(target.ids):
        if lane_id in present:
            continue
        before = next((value for value in reversed(target.ids[:index]) if value in present), None)
        after = next((value for value in target.ids[index + 1:] if value in present), None)
        if before is not None:
            at = order.index(before) + 1
        else:
            at = order.index(after) if after is not None else len(order)
        order.insert(at, lane_id)
        present.add(lane_id)
    return order


def _merge_lane_family(result: dict, keys: tuple[str, ...], base: _Lanes, target: _Lanes,
                       stored: _Lanes, conflicts: list[dict], path: str) -> list[str]:
    """Apply the step to stored's lanes one by one; returns the merged order of ids."""
    if base.values == target.values:
        return stored.ids
    lanes = {lane_id: copy.deepcopy(stored.lane(lane_id)) for lane_id in stored.ids}
    for lane_id in [*base.ids, *(value for value in target.ids if value not in base.index)]:
        lane_path = f"{path}[{lane_id}]"
        before, after, now = base.lane(lane_id), target.lane(lane_id), stored.lane(lane_id)
        if _same(before, after) or _same(now, after):
            continue
        if before is _MISSING or after is _MISSING or now is _MISSING:
            if not _same(now, before):
                _conflict(conflicts, lane_path, before, after, now)
            elif after is _MISSING:
                lanes.pop(lane_id, None)
            else:
                lanes[lane_id] = copy.deepcopy(after)
            continue
        lanes[lane_id]["config"] = _merge_dict_fields(
            before["config"], after["config"], now["config"], conflicts, lane_path)
        _merge_value(lanes[lane_id], "recipe", before, after, now, conflicts,
                     f"{lane_path}.recipe")

    kept = [lane_id for lane_id in stored.ids if lane_id in lanes]
    order = _merge_lane_order(base, target, stored, kept, conflicts, path)
    order = [lane_id for lane_id in _place_returning_lanes(order, target)
             if lane_id in lanes]
    if not order:
        _conflict(conflicts, path, "at least one lane", "at least one lane", "no lanes")
        return order

    count_key, configs_key, *recipe_keys = keys
    result[count_key] = len(order)
    columns = [(configs_key, "config", "spare_configs")]
    if recipe_keys:
        columns.append((recipe_keys[0], "recipe", "spare_recipes"))
    spares = []
    for _, _, spare_name in columns:
        before, after, spare = (getattr(side, spare_name) for side in (base, target, stored))
        # Settings kept past the last lane are no lane yet. A clash keeps stored's.
        spares.append(after if before != after and spare == before else spare)
    # One a lane has since taken over is not kept beside it: its id, in the list that
    # holds this family's ids, is one of the lanes'.
    adopted = {index for index, row in enumerate(spares[-1]) if isinstance(row, dict)
               and str(row.get("lane_id") or "") in lanes}
    for (key, part, _), spare in zip(columns, spares):
        result[key] = [lanes[lane_id][part] for lane_id in order] + copy.deepcopy(
            [row for index, row in enumerate(spare) if index not in adopted])
    return order


def _merge_unidentified_family(base: _Lanes, target: _Lanes, stored: _Lanes,
                               conflicts: list[dict], path: str) -> None:
    """A side whose lanes have no ids: a page older than the server, or damaged data.

    Values cannot tell two default lanes apart, so a step that changes the family is
    refused, unless stored already reads as its result and no lane came or went.
    """
    before, after, now = (side.without_config_ids() for side in (base, target, stored))
    if before == after or (now == after and _same(base.count, target.count)):
        return
    _conflict(conflicts, path, base.values, target.values, stored.values)
    conflicts[-1]["code"] = "lane_identity_missing"


def _lane_refs_to_numbers(result: dict, target: dict, stored: dict, merged: dict[str, list[str]],
                          sides: dict[str, tuple], conflicts: list[dict]) -> None:
    """Give every item its lane's number again, refusing one whose lane is gone."""
    reference = "reference_lane_count"
    final_recipes = (_Lanes(result, _lane_families()[reference][1]).recipes
                     if reference in sides else {})
    for collection, (id_field, index_field, _) in _LANE_COLLECTIONS.items():
        rows = result.get(collection)
        if not isinstance(rows, list):
            continue
        before, after = ({str(row.get(id_field) or ""): row.get(index_field)
                          for row in (side.get(collection) or []) if isinstance(row, dict)}
                         if isinstance(side.get(collection), list) else {}
                         for side in (stored, target))
        for row in rows:
            ref = row.get(index_field) if isinstance(row, dict) else None
            if not isinstance(ref, LaneRef):
                continue
            member_id = str(row.get(id_field) or "")
            path = f"{collection}[{member_id}].{index_field}"
            order = merged[ref.family]
            if ref.lane_id is None:
                row[index_field] = len(order) + ref.past
            elif ref.lane_id in order:
                row[index_field] = order.index(ref.lane_id)
            else:
                _conflict(conflicts, path, after.get(member_id, _MISSING),
                          after.get(member_id, _MISSING), before.get(member_id, _MISSING))
                continue
            if collection != "reference_items" or ref.lane_id is None:
                continue
            # A lane's media kind may change only while the lane is empty, so an item
            # stands on a kind it was placed for: the step's, or the stored one.
            _, target_lanes, stored_lanes = sides[ref.family]
            kind = final_recipes.get(ref.lane_id, {}).get("media_kind")
            placed = [lanes.recipes[ref.lane_id].get("media_kind")
                      for lanes, refs in ((target_lanes, after), (stored_lanes, before))
                      if ref.lane_id in lanes.recipes and refs.get(member_id) == ref]
            if placed and kind not in placed:
                _conflict(conflicts, f"{path}.media_kind", placed[0], placed[0], kind)


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


def _restore_removed_document_mirrors(result: dict, target: dict, stored: dict) -> None:
    """The text mirror of a prompt document the merge removed becomes the target's.

    ``channels`` and ``global_channels`` are derived, so the merge leaves them as stored.
    The constructors rebuild a missing document from its mirror, so stored's mirror would
    bring the undone text back under a new node id. A target that holds the text without
    a document (older than documents) gets that text; usually it holds neither.
    """
    def restore(owner, wanted, before, docs_field, mirror_field):
        before_docs, docs = before.get(docs_field), owner.get(docs_field)
        mirror, wanted_mirror = owner.get(mirror_field), wanted.get(mirror_field)
        if not isinstance(before_docs, dict) or not isinstance(mirror, dict):
            return False
        kept = docs if isinstance(docs, dict) else {}
        removed = [key for key in before_docs if key not in kept]
        if not removed:
            return False
        mirror = {key: value for key, value in mirror.items() if key not in removed}
        if isinstance(wanted_mirror, dict):
            mirror.update({key: copy.deepcopy(wanted_mirror[key])
                           for key in removed if key in wanted_mirror})
        owner[mirror_field] = mirror
        return not mirror

    def sections(scene):
        rows = scene.get("prompt_sections")
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    if restore(result, target, stored, "global_channel_docs", "global_channels"):
        # An empty mirror is reseeded from the flat prompt.
        result["prompt"] = copy.deepcopy(target.get("prompt", ""))
    stored_rows = {str(row.get("prompt_id") or ""): row for row in sections(stored)}
    target_rows = {str(row.get("prompt_id") or ""): row for row in sections(target)}
    for row in sections(result):
        row_id = str(row.get("prompt_id") or "")
        if row_id in stored_rows and row_id in target_rows:
            restore(row, target_rows[row_id], stored_rows[row_id], "channel_docs", "channels")


def drop_missing_reference_members(base: dict, target: dict, stored: dict,
                                   library_member_ids) -> tuple[dict, dict, int]:
    """``base`` and ``target`` without Reference members the Library no longer has.

    Returns thinned copies (or the inputs, where nothing changes) and how many members
    the step would have restored but cannot. A member the stored item still holds stays:
    ``stored`` is never edited. See the plan's table (Undo Across Takes, Phase 1).
    """
    library = {str(value) for value in library_member_ids}

    def rows(scene):
        members = scene.get("reference_items")
        if not isinstance(members, list):
            return None
        indexed = {}
        for row in members:
            row_id = str(row.get("reference_item_id") or "") if isinstance(row, dict) else ""
            if not row_id or row_id in indexed or not isinstance(row.get("members"), list):
                return None
            indexed[row_id] = row
        return indexed

    base_rows, target_rows, stored_rows = rows(base), rows(target), rows(stored)
    if base_rows is None or target_rows is None or stored_rows is None:
        return base, target, 0

    def ids(row):
        return [str(member.get("member_id") or "") if isinstance(member, dict) else ""
                for member in row["members"]]

    new_base, new_target, dropped = dict(base_rows), dict(target_rows), 0
    for row_id, target_row in target_rows.items():
        base_row, stored_row = base_rows.get(row_id), stored_rows.get(row_id)
        held = library | set(ids(stored_row) if stored_row is not None else ())

        def thinned(row):
            return {**row, "members": [
                member for member, member_id in zip(row["members"], ids(row))
                if member_id in held]}

        gone = [member_id for member_id in ids(target_row) if member_id not in held]
        target_thin = thinned(target_row)
        emptied = bool(target_row["members"]) and not target_thin["members"]
        if base_row is None:
            if stored_row is None and gone:
                dropped += len(gone)
                if emptied:
                    del new_target[row_id]
                else:
                    new_target[row_id] = target_thin
            continue
        if target_row["members"] == base_row["members"] and stored_row is not None:
            continue
        base_thin = thinned(base_row)
        base_emptied = bool(base_row["members"]) and not base_thin["members"]
        if stored_row is not None:
            if emptied:
                target_thin["members"] = list(base_thin["members"])
                dropped += len(gone)
            else:
                dropped += len(set(gone) - set(ids(base_row)))
            new_base[row_id], new_target[row_id] = base_thin, target_thin
        elif emptied and base_emptied:
            dropped += len(gone)
            del new_base[row_id], new_target[row_id]
        elif base_emptied:
            dropped += len(gone)
            del new_base[row_id]
            new_target[row_id] = target_thin
    if new_base == base_rows and new_target == target_rows:
        return base, target, dropped
    return ({**base, "reference_items": list(new_base.values())},
            {**target, "reference_items": list(new_target.values())}, dropped)


def merge_scene_history(base: dict, target: dict, stored: dict) -> dict:
    """Apply the base→target reversal onto stored without touching unrelated work.

    ``base`` is the authoritative scene after the original operation, ``target``
    is the scene before it, and ``stored`` is current durable state. The function
    performs no I/O and deliberately uses ordinary recursive Python equality.
    """
    if not all(isinstance(value, dict) for value in (base, target, stored)):
        raise TypeError("Scene history merge inputs must be objects")

    conflicts: list[dict] = []
    # A lane number means a different lane on each side once lanes come, go or move,
    # so items are merged by lane id and numbered again afterwards.
    families, sides = _lane_families(), {}
    for count_key, (path, keys) in families.items():
        family = tuple(_Lanes(document, keys) for document in (base, target, stored))
        if all(lanes.ids is not None for lanes in family):
            sides[count_key] = family
        else:
            _merge_unidentified_family(*family, conflicts, path)
    raw_base, raw_target = base, target
    base, target, stored = (
        _with_lane_refs(document, {key: family[side] for key, family in sides.items()})
        for side, document in enumerate((base, target, stored)))
    _keep_unmoved_past_items(base, target, raw_base, raw_target)
    result = copy.deepcopy(stored)

    for field in MERGED_WRITE_FIELDS["scalar"]:
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

    for field in MERGED_WRITE_FIELDS["mapping"]:
        _merge_mapping(result, field, base, target, stored, conflicts, field)

    collection_specs = MERGED_WRITE_FIELDS["collection"]
    for field, key, per_field, excluded, mapping_fields in collection_specs:
        _merge_collection(result, field, key, base, target, stored, conflicts,
                          per_field=per_field, excluded=excluded,
                          mapping_fields=mapping_fields)
    _restore_removed_document_mirrors(result, target, stored)

    merged_lanes = {
        count_key: _merge_lane_family(result, families[count_key][1], *family,
                                      conflicts, families[count_key][0])
        for count_key, family in sides.items()}
    _lane_refs_to_numbers(result, target, stored, merged_lanes, sides, conflicts)
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
