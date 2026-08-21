"""MiniMax H3 conditioning setup normalization and physical slot planning."""

from __future__ import annotations

import copy
import math
import uuid

from .reference_resolution import resolve_effective_references
from .reference_prompt_formatter import as_plain_record
from .reference_prompt_formatter import reference_member_labels


SETUP_VERSION = "minimax_h3_setup_v1"
TASK_MODES = {"T2VA", "I2VA", "FL2VA", "L2VA"}
SETUP_MODES = {"base", "reference"}
MAX_PICTURES = 9
MAX_VIDEOS = 3
MAX_STANDALONE_AUDIO = 3

PICTURES_POPULATION = "pictures"
VIDEOS_POPULATION = "videos"
STANDALONE_AUDIOS_POPULATION = "standalone_audios"

# Neither H3 mode needs an authored setup, so compilation synthesizes one.  That
# synthetic setup must be byte-identical on every compile: a fresh UUID would
# change `setup_manifest`, and therefore the compiled content hash, on each
# preview, and would disagree with the id the live Bridge selector resolves.
IMPLICIT_BASE_SETUP_ID = "implicit_minimax_h3_base"
IMPLICIT_BASE_SETUP_NAME = "MiniMax H3 Base"
IMPLICIT_REFERENCE_SETUP_ID = "implicit_minimax_h3_reference"
IMPLICIT_REFERENCE_SETUP_NAME = "MiniMax H3 Full Reference"


def normalize_setup(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    # Default only *absent* values.  Coercing an explicit unknown mode or task
    # mode to base/T2VA silently rewrites authored intent and makes the
    # invalid-mode validation below unreachable; `setup_validation_errors`
    # reports the preserved value instead.
    mode = str(raw.get("mode") or "").strip() or "base"
    task_mode = str(raw.get("task_mode") or "").strip().upper() or "T2VA"

    def ids(key):
        result = []
        for value in raw.get(key) or []:
            value = str(value or "").strip()
            if value and value not in result:
                result.append(value)
        return result

    return {
        "schema": SETUP_VERSION,
        "setup_id": str(raw.get("setup_id") or "").strip() or uuid.uuid4().hex,
        "name": str(raw.get("name") or "MiniMax H3 Reference Setup"),
        "mode": mode,
        "task_mode": task_mode,
        "first_guide_id": str(raw.get("first_guide_id") or ""),
        "last_guide_id": str(raw.get("last_guide_id") or ""),
        "picture_lane_ids": ids("picture_lane_ids"),
        "video_lane_ids": ids("video_lane_ids"),
        "audio_lane_ids": ids("audio_lane_ids"),
    }


def lane_population(recipe_wrapper) -> str:
    """The physical population one lane recipe serves, or "" for a generic lane.

    Mirrored in `web/js/reference_lane_identity.js` as `lanePopulation`.
    """
    wrapper = as_plain_record(recipe_wrapper)
    recipe = (as_plain_record(wrapper.get("recipe"))
              if wrapper.get("recipe") is not None else wrapper)
    soft = as_plain_record(recipe.get("soft"))
    population = str(soft.get("physical_population") or "")
    if population:
        return population
    recipe_id = str(wrapper.get("recipe_id") or recipe.get("id") or "")
    return {
        "sonder:minimax_h3_picture": "pictures",
        "sonder:minimax_h3_video": "videos",
        "sonder:minimax_h3_audio": "standalone_audios",
    }.get(recipe_id, "")


def population_lane_ids(lane_recipes, population) -> list[str]:
    """Durable lane ids serving one physical population, in lane order.

    Membership is the lane recipe's own declared model input.  There is no
    setup registration: a lane that declares a population participates, a lane
    that declares none is a generic Reference lane serving the graph through
    the Selector/Bridge.  Lane order is what numbers the population's ordinals.
    """
    population = str(population or "")
    if not population:
        return []
    result = []
    for raw_recipe in lane_recipes or []:
        recipe = as_plain_record(raw_recipe)
        lane_id = str(recipe.get("lane_id") or "")
        if (lane_id and lane_id not in result
                and lane_population(recipe) == population):
            result.append(lane_id)
    return result


def setup_validation_errors(setup) -> list[dict]:
    """Report preserved-but-unsupported setup values as controlled diagnostics."""
    value = setup if isinstance(setup, dict) else {}
    errors = []
    mode = str(value.get("mode") or "")
    if mode not in SETUP_MODES:
        errors.append({"code": "invalid_h3_setup_mode",
                       "message": f"MiniMax H3 setup mode {mode!r} is not supported."})
    task_mode = str(value.get("task_mode") or "")
    if task_mode not in TASK_MODES:
        errors.append({"code": "invalid_h3_task_mode",
                       "message": f"MiniMax H3 task mode {task_mode!r} is not supported."})
    return errors


def implicit_base_setup(task_mode="T2VA") -> dict:
    """The deterministic Base setup used when a scene authored none."""
    return normalize_setup({
        "setup_id": IMPLICIT_BASE_SETUP_ID,
        "name": IMPLICIT_BASE_SETUP_NAME,
        "mode": "base",
        "task_mode": task_mode,
    })


def implicit_reference_setup() -> dict:
    """The deterministic Full Reference setup.  There is no authored variant.

    Reference mode reads nothing from a stored setup record: `task_mode` is
    Base-only, the guide ids are never resolved, and lane membership now comes
    from each lane recipe's declared model input.  Any `minimax_h3_conditioning
    _setups` entry a project still carries is left untouched at rest and simply
    never consulted here.
    """
    return normalize_setup({
        "setup_id": IMPLICIT_REFERENCE_SETUP_ID,
        "name": IMPLICIT_REFERENCE_SETUP_NAME,
        "mode": "reference",
    })


def active_setup(scene_or_dict) -> dict | None:
    """The scene's authored setup.  Base-only: Reference mode never calls this."""
    if isinstance(scene_or_dict, dict):
        setups = scene_or_dict.get("minimax_h3_conditioning_setups") or []
        active_id = str(scene_or_dict.get("active_minimax_h3_setup_id") or "")
    else:
        setups = getattr(scene_or_dict, "minimax_h3_conditioning_setups", []) or []
        active_id = str(getattr(scene_or_dict, "active_minimax_h3_setup_id", "") or "")
    normalized = [normalize_setup(value) for value in setups if isinstance(value, dict)]
    if active_id:
        return next((value for value in normalized
                     if value["setup_id"] == active_id), None)
    return normalized[0] if len(normalized) == 1 else None


def _entity_lookup(references):
    entities = {}
    members = {}
    for reference in references or []:
        ref = as_plain_record(reference)
        entity_id = str(ref.get("reference_id") or "")
        if entity_id:
            entities[entity_id] = ref
        for member in ref.get("members") or []:
            member = as_plain_record(member)
            member_id = str(member.get("member_id") or "")
            if member_id:
                members[member_id] = (ref, member)
    return entities, members


def _asset_lookup(assets):
    result = {}
    for value in assets or []:
        asset = as_plain_record(value)
        asset_id = str(asset.get("asset_id") or "")
        if asset_id:
            result[asset_id] = asset
    return result


def _recipe_lookup(recipes):
    result = {}
    for index, value in enumerate(recipes or []):
        recipe = as_plain_record(value)
        lane_id = str(recipe.get("lane_id") or "")
        if lane_id:
            result[lane_id] = (index, recipe)
    return result


def _member_slots(winner, lane_id, population, member_lookup, asset_lookup):
    if not winner:
        return []
    item = as_plain_record(winner.get("item"))
    item_id = str(item.get("reference_item_id") or "")
    result = []
    for member_ref in item.get("members") or []:
        if not isinstance(member_ref, dict):
            continue
        member_id = str(member_ref.get("member_id") or "")
        entity_id = str(member_ref.get("entity_id") or "")
        reference, member = member_lookup.get(member_id, ({}, {}))
        asset_id = str(member.get("asset_id") or "")
        asset = asset_lookup.get(asset_id, {})
        labels = reference_member_labels(
            str(reference.get("name") or ""), str(member.get("name") or ""))
        result.append({
            "slot_id": f"{lane_id}:{item_id}:{member_id}",
            "lane_id": lane_id,
            "lane_index": int(winner.get("laneIndex", 0)),
            "reference_item_id": item_id,
            "entity_id": entity_id or str(reference.get("reference_id") or ""),
            "member_id": member_id,
            "asset_id": asset_id,
            "asset_type": str(asset.get("asset_type") or ""),
            "entity_name": labels["entity_name"],
            "member_name": labels["member_name"],
            "prompt_name": labels["name"],
            "display_name": labels["display_name"],
            "handle": str(member.get("handle") or ""),
            # Authored Library prose, not either display/prompt-safe name.
            # Prompt Context consumes this only after the effective attachment
            # configuration and explicit prompt-identity definition are empty.
            "member_prompt": str(member.get("prompt") or "").strip(),
            "population": population,
            "source_start_sec": float(member.get("source_start_sec") or 0.0),
            "source_end_sec": member.get("source_end_sec"),
            "role": str(member_ref.get("role") or ""),
            "visual_intent": str(member_ref.get("visual_intent") or
                                 member.get("visual_intent") or
                                 reference.get("visual_intent") or "preserve"),
            "audio_intent": str(member_ref.get("audio_intent") or
                                member.get("audio_intent") or
                                reference.get("audio_intent") or
                                "reference_characteristics"),
        })
    return result


def _ordinal_aliases(target, slot, ordinal):
    for key in (slot.get("slot_id"), slot.get("reference_item_id"),
                slot.get("member_id"), slot.get("video_member_id"),
                slot.get("asset_id")):
        if key and key not in target:
            target[key] = ordinal


def _video_audio_state(asset) -> tuple[bool, bool]:
    """`(appears_silent, probe_actually_ran)` for a video asset.

    `has_audio` defaults to False, which is why `has_audio_checked` exists: an
    asset registered before the probe existed, or whose probe failed, is
    indistinguishable from a genuinely silent one. Refusing on the unprobed case
    would block a legitimate job over missing metadata, so that stays a warning
    and the decoder fails loudly if the media really is silent.
    """
    asset = asset if isinstance(asset, dict) else {}
    return (not bool(asset.get("has_audio", False)),
            bool(asset.get("has_audio_checked", False)))


def _floor_video_frames(asset, start_sec, end_sec, fps=24.0):
    try:
        duration = float(asset.get("duration_sec") or 0.0)
    except (TypeError, ValueError, OverflowError):
        duration = 0.0
    start = max(0.0, float(start_sec or 0.0))
    end = duration if end_sec is None else min(duration or float(end_sec), float(end_sec))
    seconds = max(0.0, end - start)
    decoded = max(0, int(math.floor(seconds * fps + 1e-9)))
    if decoded < 5:
        return 5
    return 5 + 17 * ((decoded - 5) // 17)


def resolve_setup(*, setup, guide_frames=None, reference_items=None,
                   lane_recipes=None, lane_configs=None, lane_count=1,
                   scene_duration=0, window_start=0, window_end=0,
                   references=None, assets=None, semantic_units=None,
                   frame_threshold_pct=0.0, profile=None) -> dict:
    """Resolve one frozen physical presentation plan and ordinal manifest."""
    # Import lazily to keep the pure Prompt compiler independent of the physical
    # resolver at module import time. Production callers pass the resolved
    # profile; the fallback preserves the direct resolver/test API.
    from . import prompt_context

    profile = (profile if isinstance(profile, dict) else
               prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"])
    population_declarations = prompt_context.physical_population_declarations(profile)
    populations_by_key = {
        str(declaration.get("key") or ""): declaration
        for declaration in population_declarations if isinstance(declaration, dict)
    }
    active_profile_key = prompt_context.profile_key(profile)
    value = normalize_setup(setup)
    errors, warnings = [], []
    manifest = {"setup": copy.deepcopy(value), "guides": [], "pictures": [],
                 "videos": [], "standalone_audios": [],
                 "presentation": []}
    ordinals = {"subjects": {}, "pictures": {}, "videos": {}, "audios": {}}
    for declaration in population_declarations:
        manifest.setdefault(str(declaration.get("key") or ""), [])
        ordinals.setdefault(str(declaration.get("ordinal_key") or ""), {})
    guide_lookup = {str(as_plain_record(g).get("guide_id") or ""): as_plain_record(g)
                    for g in guide_frames or []}
    invalid = setup_validation_errors(value)
    if invalid:
        # An unsupported mode has no trustworthy slot plan, so stop before the
        # base/reference branch rather than resolving under a guessed mode.
        return {"setup_manifest": manifest, "ordinal_manifest": ordinals,
                "unit_picture_ordinals": {}, "unit_source_labels": {},
                "unit_source_members": {},
                "errors": invalid, "warnings": warnings}
    if value["mode"] == "base":
        required_first = value["task_mode"] in {"I2VA", "FL2VA"}
        required_last = value["task_mode"] in {"FL2VA", "L2VA"}
        for role, guide_id, required in (
                ("first", value["first_guide_id"], required_first),
                ("last", value["last_guide_id"], required_last)):
            guide = guide_lookup.get(guide_id)
            if guide and not guide.get("muted"):
                manifest["guides"].append({"role": role, **copy.deepcopy(guide)})
            elif required:
                errors.append({"code": f"missing_{role}_guide",
                               "message": f"{value['task_mode']} requires a {role}-frame Guide."})
        return {"setup_manifest": manifest, "ordinal_manifest": ordinals,
                "errors": errors, "warnings": warnings}

    recipe_lookup = _recipe_lookup(lane_recipes)
    winners = resolve_effective_references(
        reference_items=reference_items or [], lane_count=lane_count,
        scene_duration=scene_duration, window_start=window_start,
        window_end=window_end, lane_configs=lane_configs,
        frame_threshold_pct=frame_threshold_pct)
    _entities, member_lookup = _entity_lookup(references)
    assets_by_id = _asset_lookup(assets)

    def collect(declaration):
        population = str((declaration or {}).get("key") or "")
        cap = int((declaration or {}).get("cap") or 0)
        expected_types = set((declaration or {}).get("media_kinds") or [])
        rows = []
        # Lane ids come from the recipes themselves, so a missing lane and a
        # population that disagrees with its own declaration are both
        # unreachable here.  `lane_population` is the one derivation: reading
        # `soft["physical_population"]` directly would diverge from it for a
        # lane whose recipe body is bare and only carries `recipe_id`.
        for lane_id in population_lane_ids(lane_recipes, population):
            lane_index, recipe_wrapper = recipe_lookup[lane_id]
            recipe = (as_plain_record(recipe_wrapper.get("recipe"))
                      if recipe_wrapper.get("recipe") is not None
                      else recipe_wrapper)
            soft = as_plain_record(recipe.get("soft"))
            compatible_profiles = [str(entry) for entry in
                                   soft.get("compatible_profiles", [])]
            # Reachable on a lane the user never opted into a setup, because a
            # custom recipe may declare a population under a format that does
            # not want it.  Refuse loudly rather than feed foreign slots.
            if compatible_profiles and active_profile_key not in compatible_profiles:
                errors.append({"code": "setup_lane_profile_incompatible", "lane_id": lane_id,
                               "message": "A conditioning lane recipe is not compatible with MiniMax H3 Full Reference."})
                continue
            winner = winners[lane_index] if lane_index < len(winners) else None
            lane_rows = _member_slots(winner, lane_id, population,
                                      member_lookup, assets_by_id)
            for row in lane_rows:
                row["exposed_capabilities"] = [str(entry) for entry in
                                               soft.get("exposed_capabilities", [])]
                row["role_fields"] = [str(entry) for entry in
                                      soft.get("role_fields", [])]
                try:
                    row["recommended_duration_min_sec"] = max(
                        0.0, float(soft.get("recommended_duration_min_sec") or 0.0))
                    row["recommended_duration_max_sec"] = max(
                        0.0, float(soft.get("recommended_duration_max_sec") or 0.0))
                except (TypeError, ValueError, OverflowError):
                    row["recommended_duration_min_sec"] = 0.0
                    row["recommended_duration_max_sec"] = 0.0
            rows.extend(lane_rows)
        if len(rows) > cap:
            errors.append({"code": f"{population}_slot_cap", "message":
                           f"MiniMax H3 accepts at most {cap} {population} slots."})
        rows = rows[:cap]
        for row in rows:
            if row["asset_type"] not in expected_types:
                errors.append({"code": "invalid_setup_media", "slot_id": row["slot_id"],
                               "message": f"{population} requires {sorted(expected_types)} media."})
                continue
            asset = assets_by_id.get(row["asset_id"], {})
            if population == "standalone_audios" and row["asset_type"] == "video":
                silent, checked = _video_audio_state(asset)
                if silent and checked:
                    # A silent video in an Audio slot decodes to nothing; refuse
                    # it rather than emitting an <Audio N> label for silence.
                    errors.append({
                        "code": "silent_video_audio_slot", "slot_id": row["slot_id"],
                        "message": "This Audio slot uses a video that contains no audio.",
                    })
                    continue
                if silent:
                    warnings.append({
                        "code": "unverified_video_audio_slot",
                        "slot_id": row["slot_id"],
                        "message": ("This Audio slot uses a video whose audio has "
                                    "not been probed; run an asset Refresh if it "
                                    "decodes to silence."),
                    })
            try:
                asset_duration = max(0.0, float(asset.get("duration_sec") or 0.0))
                start = max(0.0, float(row.get("source_start_sec") or 0.0))
                end_value = row.get("source_end_sec")
                end = asset_duration if end_value is None else min(
                    asset_duration or float(end_value), float(end_value))
                duration = max(0.0, end - start)
            except (TypeError, ValueError, OverflowError):
                duration = 0.0
            minimum = row.get("recommended_duration_min_sec", 0.0)
            maximum = row.get("recommended_duration_max_sec", 0.0)
            if duration and minimum and duration < minimum:
                warnings.append({"code": "recipe_duration_below_recommendation",
                                 "slot_id": row["slot_id"],
                                 "message": f"This source is shorter than the recipe's {minimum:g}s recommendation."})
            if duration and maximum and duration > maximum:
                warnings.append({"code": "recipe_duration_above_recommendation",
                                 "slot_id": row["slot_id"],
                                 "message": f"This source is longer than the recipe's {maximum:g}s recommendation."})
        return rows

    pictures_decl = populations_by_key.get(PICTURES_POPULATION)
    videos_decl = populations_by_key.get(VIDEOS_POPULATION)
    audio_decl = populations_by_key.get(STANDALONE_AUDIOS_POPULATION)
    pictures = collect(pictures_decl) if pictures_decl else []
    videos = collect(videos_decl) if videos_decl else []
    standalone = collect(audio_decl) if audio_decl else []
    manifest["pictures"] = pictures
    manifest["videos"] = videos
    manifest["standalone_audios"] = standalone

    for number, slot in enumerate(pictures, 1):
        ordinal_field = f"{pictures_decl['token_kind']}_ordinal"
        slot[ordinal_field] = number
        _ordinal_aliases(ordinals[pictures_decl["ordinal_key"]], slot, number)
        manifest["presentation"].append({
            "kind": pictures_decl["key"][:-1], **slot})
    for video_number, slot in enumerate(videos, 1):
        ordinal_field = f"{videos_decl['token_kind']}_ordinal"
        slot[ordinal_field] = video_number
        slot["decoded_frames_24fps"] = _floor_video_frames(
            assets_by_id.get(slot["asset_id"], {}),
            slot["source_start_sec"], slot["source_end_sec"])
        _ordinal_aliases(ordinals[videos_decl["ordinal_key"]], slot, video_number)
        manifest["presentation"].append({
            "kind": videos_decl["key"][:-1], **slot})
    for audio_number, slot in enumerate(standalone, 1):
        ordinal_field = f"{audio_decl['token_kind']}_ordinal"
        slot[ordinal_field] = audio_number
        _ordinal_aliases(ordinals[audio_decl["ordinal_key"]], slot, audio_number)
        manifest["presentation"].append({
            "kind": audio_decl["key"][:-1], **slot})

    slots_by_member = {}
    for row in manifest["presentation"]:
        member_id = str(row.get("member_id") or row.get("video_member_id") or "")
        declaration = populations_by_key.get(str(row.get("population") or ""))
        if not member_id or declaration is None:
            continue
        ordinal_field = f"{declaration['token_kind']}_ordinal"
        slots_by_member.setdefault(member_id, []).append({
            "population": str(declaration.get("key") or ""),
            "slot_id": str(row.get("slot_id") or ""),
            "lane_id": str(row.get("lane_id") or ""),
            "ordinal": int(row.get(ordinal_field) or 0),
            "label": prompt_context.declared_label(
                declaration, row.get(ordinal_field)),
        })
    duplicate_member_slots = {
        member_id: rows for member_id, rows in slots_by_member.items()
        if any(sum(1 for candidate in rows
                   if candidate["population"] == row["population"]) > 1
               for row in rows)
    }
    if duplicate_member_slots:
        manifest["duplicate_member_slots"] = duplicate_member_slots

    # Subject order: first contributing physical slot, then explicit unit order.
    physical_order = {str(row.get("member_id") or
                          row.get("video_member_id")): index
                      for index, row in enumerate(manifest["presentation"])
                      if row.get("member_id") or row.get("video_member_id")}
    units = []
    for unit in semantic_units or []:
        unit = as_plain_record(unit)
        contributions = unit.get("sources") or []
        first = min((physical_order.get(str(value.get("member_id")), 10**9)
                     for value in contributions if isinstance(value, dict)),
                    default=10**9)
        # A Subject is applicable only when at least one of its stable member
        # contributions survived the one-winner-per-lane resolver for this
        # execution window.  Numbering every project Subject here would let an
        # unstaged character leak into a combined prompt and would make the
        # prompt disagree with the physical slots exposed by the Bridges.
        if first < 10**9:
            units.append((first, int(unit.get("order") or 0),
                          str(unit.get("semantic_unit_id") or ""), unit))
    units.sort(key=lambda row: row[:3])
    unit_picture_ordinals = {}
    unit_source_labels = {}
    # Index-aligned with `unit_source_labels`: the member each rendered source
    # label stands for. `unit_source_labels` holds STRINGS, which is fine for
    # compiling but useless to anything that has to turn a source back into a
    # live `@handle` — the member id is in this loop and was thrown away.
    # Additive on purpose: `prompt_tokens._unit_label_ordinal` still parses the
    # rendered strings and no fixture has to supply this.
    unit_source_members = {}
    for subject_number, (_first, _order, unit_id, unit) in enumerate(units, 1):
        if not unit_id:
            continue
        ordinals["subjects"][unit_id] = subject_number
        member_ids = {str(value.get("member_id") or "")
                      for value in unit.get("sources") or []
                      if isinstance(value, dict)}
        picture_ordinal_field = f"{pictures_decl['token_kind']}_ordinal"
        unit_picture_ordinals[unit_id] = [
            row[picture_ordinal_field] for row in pictures
            if row.get("member_id") in member_ids
        ]
        labels = []
        members = []
        for row in manifest["presentation"]:
            member_id = str(row.get("member_id") or row.get(
                "video_member_id") or "")
            if member_id not in member_ids:
                continue
            declaration = populations_by_key.get(str(row.get("population") or ""))
            if declaration is None:
                continue
            ordinal_field = f"{declaration['token_kind']}_ordinal"
            label = prompt_context.declared_label(
                declaration, row.get(ordinal_field))
            # The dedupe is on the LABEL, and one label can be reached by more
            # than one member, so the two lists must move together or every
            # index after the first duplicate points at the wrong member.
            if label in labels:
                continue
            labels.append(label)
            members.append(member_id)
        unit_source_labels[unit_id] = labels
        unit_source_members[unit_id] = members
    return {"setup_manifest": manifest, "ordinal_manifest": ordinals,
            "unit_picture_ordinals": unit_picture_ordinals,
            "unit_source_labels": unit_source_labels,
            "unit_source_members": unit_source_members,
            "errors": errors, "warnings": warnings}
