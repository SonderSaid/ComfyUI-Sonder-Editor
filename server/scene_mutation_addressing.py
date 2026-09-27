"""How each scene mutation names its target, and therefore whether the server may
re-apply it after a compare-and-swap conflict.

`durable_rules.md`: *a mutation's re-application policy is decided by the evidence
in the request, and a weaker request can only get the weaker policy.* This module
is the server's reading of that rule for `POST .../scenes/{id}/mutations`, and it
decides the `addressing=` that `_apply_scene_mutations_sync` hands to
`_apply_project_versioned_sync`:

* ``"identity"`` -- every operation names its row by a durable id, or by a value
  snapshot a branch validator compares where it resolves the row, so a batch that
  lost a save race may be reloaded and re-applied.
* ``"positional"`` -- at least one operation names a list position, a lane index
  or an absolute count read from the caller's own document. One attempt, under a
  compare-and-swap against the version this request loaded.

`web/js/scene_mutation_addressing.js` is the declared mirror of this module and
carries the traced evidence for each entry: the validator each promotion relies
on, the decoys, and why each positional operation stays positional. That prose
stays beside the table the browser loads because
`tests/test_scene_mutation_retry_policy.py` checks every citation in it against
`routes.py` and `GUARD_CONTRACTS`. The decisions are duplicated deliberately, and
`tests/test_scene_mutation_addressing_parity.py` holds both halves to one table
of payloads. **Change one side only together with the other.**

Media-I/O creates (`create_clip` / `create_audio_track`) are positional here, so a
batch that can invoke ffmpeg is never re-applied: `_media_io_operation_count`'s
extraction would run a second time against a reloaded document. The parity suite
pins that every operation the media budget can count stays positional.
"""

from __future__ import annotations

from dataclasses import dataclass

from .lane_registry import descriptor_for_lane_type

DURABLE = "durable"
POSITIONAL = "positional"

# A caller may decline a replay the addressing would grant (the JS mirror says
# why). Demote-only: `derive_batch_addressing` reads the header only to weaken
# its own answer, so no header value can acquire a replay.
REPLAY_DECLINED_HEADER = "X-Sonder-Mutation-Replay"
REPLAY_DECLINED_VALUE = "declined"
COLLECTION = "collection"
PER_ITEM = "per_item"
UNADDRESSED = "unaddressed"


@dataclass(frozen=True)
class Guard:
    """One `expected*` bag a branch validator compares, and what must be in it."""
    bag: str
    identifying: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    validator: str = ""


@dataclass(frozen=True)
class Entry:
    addressing: str
    ids: tuple[str, ...] = ()
    promoted_by: tuple[Guard, ...] = ()
    refine: str = ""    # name of a payload-dependent refinement below


# Lane-count fields `_apply_scene_fields` can write; see the JS mirror.
# Expiry: delete when `_apply_scene_fields` no longer accepts count fields.
LANE_COUNT_FIELDS = (
    "video_lane_count", "audio_lane_count",
    "motion_driver_lane_count", "reference_lane_count",
)

# A durable row id says WHAT moves, not WHERE it lands; see the JS mirror.
# Expiry: delete when those branches compare a lane identity.
LANE_DESTINATION_FIELD = {
    "update_clip": "track_index",
    "update_audio_track": "lane_index",
    "update_reference_item": "lane_index",
}

SCENE_MUTATION_ADDRESSING: dict[str, Entry] = {
    # --- named by a durable id ---------------------------------------------
    "update_clip": Entry(DURABLE, ids=("clip_id",), refine="lane_destination"),
    "delete_clip": Entry(DURABLE, ids=("clip_id",)),
    "replace_clip_source": Entry(DURABLE, ids=("clip_id", "asset_id")),
    "split_clip": Entry(DURABLE, ids=("clip_id",)),
    "update_audio_track": Entry(DURABLE, ids=("track_id",), refine="lane_destination"),
    "delete_audio_track": Entry(DURABLE, ids=("track_id",)),
    "replace_audio_source": Entry(DURABLE, ids=("track_id", "asset_id")),
    "split_audio_track": Entry(DURABLE, ids=("track_id",)),
    "update_reference_item": Entry(DURABLE, ids=("reference_item_id",),
                                   refine="lane_destination"),
    "delete_reference_item": Entry(DURABLE, ids=("reference_item_id",)),
    "split_reference_item": Entry(DURABLE, ids=("reference_item_id",)),
    "delete_link_group": Entry(DURABLE, ids=("group_id",)),
    "delete_prompt_semantic_unit_if_unreferenced": Entry(
        DURABLE, ids=("semantic_unit_id",)),
    "create_prompt_semantic_unit": Entry(DURABLE, ids=("unit.semantic_unit_id",)),

    # --- names no row and no document-derived destination ------------------
    "update_scene_fields": Entry(UNADDRESSED, refine="lane_count_fields"),
    "import_prompt_context_dependencies": Entry(UNADDRESSED),

    # --- positional, promoted by a snapshot the server compares ------------
    "update_guide": Entry(POSITIONAL, promoted_by=(
        Guard("expected", identifying=("guide_id",),
              validator="_validate_guide_identity"),)),
    "delete_guide": Entry(POSITIONAL, promoted_by=(
        Guard("expected", identifying=("guide_id",),
              validator="_validate_guide_identity"),)),
    "move_guide": Entry(POSITIONAL, promoted_by=(
        Guard("expected", identifying=("guide_id",), required=("replaces_guide_id",),
              validator="_validate_guide_destination"),)),
    "create_guide": Entry(POSITIONAL, promoted_by=(
        Guard("expected", required=("replaces_guide_id",),
              validator="_validate_guide_creation_identity"),)),
    "update_prompt_section": Entry(POSITIONAL, promoted_by=(
        Guard("expected", identifying=("prompt_id",),
              validator="_validate_prompt_identity"),)),
    "delete_prompt_section": Entry(POSITIONAL, promoted_by=(
        Guard("expected", identifying=("prompt_id",),
              validator="_validate_prompt_identity"),)),
    "split_prompt_section": Entry(POSITIONAL, promoted_by=(
        Guard("expected", identifying=("prompt_id",),
              validator="_validate_prompt_identity"),)),
    "swap_guides": Entry(POSITIONAL, promoted_by=(
        Guard("expected_a", identifying=("guide_id",),
              validator="_validate_guide_identity"),
        Guard("expected_b", identifying=("guide_id",),
              validator="_validate_guide_identity"),
    )),
    "swap_prompt_sections": Entry(POSITIONAL, promoted_by=(
        Guard("expected_a", identifying=("prompt_id",),
              validator="_validate_prompt_identity"),
        Guard("expected_b", identifying=("prompt_id",),
              validator="_validate_prompt_identity"),
    )),
    "remove_lane": Entry(POSITIONAL),
    "move_lane": Entry(POSITIONAL, promoted_by=(
        Guard("expected", identifying=("from_lane_id", "to_lane_id"),
              validator="_move_media_lane"),)),
    "update_lane_config": Entry(POSITIONAL, promoted_by=(
        Guard("expected", identifying=("lane_id",), validator="_apply_lane_config"),),
        refine="lane_family"),

    # --- positional, and nothing in the request compares the row -----------
    "set_lane_count": Entry(POSITIONAL),
    "update_lane_configs": Entry(COLLECTION),
    "consolidate_items": Entry(POSITIONAL),
    "create_clip": Entry(POSITIONAL),
    "create_audio_track": Entry(POSITIONAL),
    "create_reference_item": Entry(POSITIONAL),
    "create_prompt_section": Entry(POSITIONAL),

    # --- a whole collection, with a compared structure ---------------------
    "replace_prompt_sections": Entry(COLLECTION, promoted_by=(
        Guard("expected", required=("sections",),
              validator="_validate_prompt_replacement_identity"),)),

    # --- one decision per member -------------------------------------------
    "bulk_delete_items": Entry(PER_ITEM),
    "create_link_group": Entry(PER_ITEM),
    "unlink_items": Entry(PER_ITEM),
}

# Per-member identity for the `items[]` operations; see the JS mirror.
PER_ITEM_IDENTITY = {
    "clip": (True, ()),
    "audio": (True, ()),
    "reference": (True, ()),
    "guide": (False, ("guide_id",)),
    "prompt": (False, ("prompt_id",)),
}


# Exactly the characters JS `String.prototype.trim` removes (WhiteSpace plus
# LineTerminator). Python's bare `str.strip()` differs in both directions -- it
# keeps U+FEFF and strips U+001C-U+001F and U+0085 -- and the two sides must
# agree on what "names no id" means.
_JS_TRIM_CHARS = ("\t\n\v\f\r         "
                  "         　﻿")


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip(_JS_TRIM_CHARS) == "")


def _read_path(source, path: str):
    value = source
    for part in str(path).split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _decision(op_type: str, addressing: str, retryable: bool, reason: str) -> dict:
    return {"type": op_type, "addressing": addressing,
            "retryable": bool(retryable), "reason": reason}


def _refine(name: str, operation: dict) -> dict | None:
    op_type = str(operation.get("type") or "")
    if name == "lane_destination":
        lane_field = LANE_DESTINATION_FIELD.get(op_type)
        fields = operation.get("fields")
        if not lane_field or not isinstance(fields, dict) or lane_field not in fields:
            return None
        return _decision(op_type, POSITIONAL, False,
                         f"fields names `{lane_field}`, a lane position nothing compares")
    if name == "lane_count_fields":
        fields = operation.get("fields")
        if not isinstance(fields, dict):
            return None
        counts = [one for one in LANE_COUNT_FIELDS if one in fields]
        if not counts:
            return None
        return _decision(op_type, POSITIONAL, False,
                         f"fields names {', '.join(counts)}, an absolute lane count")
    if name == "lane_family":
        lane_type = str(operation.get("lane_type") or "")
        descriptor = descriptor_for_lane_type(lane_type)
        if descriptor is None:
            return None     # unknown lane type: the branch 400s
        # Mirrors `_apply_lane_config`, which tests `fixed_config_attr` first.
        if descriptor.fixed_config_attr:
            return _decision(op_type, DURABLE, True,
                             f"lane_type {lane_type!r} is a fixed-config lane")
        if not descriptor.recipe_attr:
            return _decision(op_type, POSITIONAL, False,
                             f"lane_type {lane_type!r} has no recipe_attr, so its "
                             "`expected` is never compared")
        return None
    raise KeyError(name)


def _guard_satisfied(operation: dict, guard: Guard) -> bool:
    bag = operation.get(guard.bag)
    if not isinstance(bag, dict):
        return False
    if any(key not in bag for key in guard.required):
        return False
    if not guard.identifying:
        return True
    return all(not _is_blank(bag.get(key)) for key in guard.identifying)


def _per_item_evidence(op_type: str, operation: dict) -> dict:
    items = operation.get("items")
    if not isinstance(items, list) or not items:
        return _decision(op_type, PER_ITEM, False, "no items to decide on")
    for item in items:
        item_type = str((item or {}).get("type") or "") if isinstance(item, dict) else ""
        identity = PER_ITEM_IDENTITY.get(item_type)
        if identity is None:
            return _decision(op_type, PER_ITEM, False,
                             f"member type {item_type!r} is not classified")
        durable, identifying = identity
        if durable:
            if _is_blank(item.get("id")):
                return _decision(op_type, PER_ITEM, False,
                                 f"a {item_type} member carries no durable id")
            continue
        expected = item.get("expected")
        if not (isinstance(expected, dict)
                and any(not _is_blank(expected.get(key)) for key in identifying)):
            return _decision(op_type, PER_ITEM, False,
                             f"a {item_type} member is resolved positionally")
    return _decision(op_type, PER_ITEM, True,
                     "every member is named by a durable id or a compared identity")


def scene_mutation_retry_evidence(operation) -> dict:
    """Whether one operation may be re-applied to a document the caller never read."""
    if not isinstance(operation, dict):
        return _decision("", "", False, "operation is not an object")
    op_type = str(operation.get("type") or "")
    entry = SCENE_MUTATION_ADDRESSING.get(op_type)
    if entry is None:
        return _decision(op_type, "", False, "operation type is not classified")

    if entry.refine:
        refined = _refine(entry.refine, operation)
        if refined is not None:
            return refined

    if entry.addressing == DURABLE:
        missing = [path for path in entry.ids if _is_blank(_read_path(operation, path))]
        if missing:
            return _decision(op_type, POSITIONAL, False, f"names no {', '.join(missing)}")
        return _decision(op_type, DURABLE, True, f"named by durable {', '.join(entry.ids)}")

    if entry.addressing == PER_ITEM:
        return _per_item_evidence(op_type, operation)

    if entry.addressing == UNADDRESSED:
        return _decision(op_type, UNADDRESSED, True,
                         "names no row and no document-derived destination")

    if not entry.promoted_by:
        return _decision(op_type, entry.addressing, False,
                         "positional, and nothing in the request names the row")
    for guard in entry.promoted_by:
        if not _guard_satisfied(operation, guard):
            return _decision(op_type, entry.addressing, False,
                             f"{guard.bag} names no identity `{guard.validator}` compares")
    return _decision(op_type, entry.addressing, True,
                     "positional, with a snapshot the server compares")


def derive_batch_addressing(operations, *, replay_declined: bool = False) -> str:
    """The `addressing=` for one batch: every operation must qualify.

    The batch is re-applied whole or not at all, so one weak operation makes the
    whole batch positional. An empty or malformed batch is positional too, so
    "nothing to decide on" never reads as permission. `replay_declined` is the
    caller's demote-only opt-out (`REPLAY_DECLINED_HEADER`): the Prompt tool's
    whole-array writes decline so a conflict is replanned rather than re-applied,
    because `_validate_prompt_replacement_identity` does not see section content.
    """
    if replay_declined:
        return "positional"
    if not isinstance(operations, list) or not operations:
        return "positional"
    if all(scene_mutation_retry_evidence(operation)["retryable"]
           for operation in operations):
        return "identity"
    return "positional"
