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

SETUP_LANE_POPULATIONS = {
    "picture_lane_ids": "pictures",
    "video_lane_ids": "videos",
    "audio_lane_ids": "standalone_audios",
}

# MiniMax H3 Base needs no authored setup, so compilation synthesizes one.  That
# synthetic setup must be byte-identical on every compile: a fresh UUID would
# change `setup_manifest`, and therefore the compiled content hash, on each
# preview, and would disagree with the id the live Bridge selector resolves.
IMPLICIT_BASE_SETUP_ID = "implicit_minimax_h3_base"
IMPLICIT_BASE_SETUP_NAME = "MiniMax H3 Base"


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


def _lane_population(recipe_wrapper) -> str:
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


def repoint_setup_lane_ids(setups, replacements) -> list[dict]:
    """Re-point or prune setup lane ids in the same scene mutation.

    ``replacements`` maps an old durable lane id to its replacement.  A blank
    replacement removes the binding.  Unrelated setup data is preserved so
    this helper can safely run before the setup is otherwise normalized.
    """
    replacements = {
        str(old or ""): str(new or "")
        for old, new in dict(replacements or {}).items()
        if str(old or "")
    }
    if not replacements:
        return [copy.deepcopy(value) for value in setups or []
                if isinstance(value, dict)]
    result = []
    for raw_setup in setups or []:
        if not isinstance(raw_setup, dict):
            continue
        setup = copy.deepcopy(raw_setup)
        for key in SETUP_LANE_POPULATIONS:
            values = []
            for raw_lane_id in setup.get(key) or []:
                lane_id = str(raw_lane_id or "")
                lane_id = replacements.get(lane_id, lane_id)
                if lane_id and lane_id not in values:
                    values.append(lane_id)
            setup[key] = values
        result.append(setup)
    return result


def repair_setup_lane_bindings(setups, lane_recipes) -> tuple[list[dict], list[dict]]:
    """Repair stale setup bindings from ordered lane population evidence.

    A missing id is rebound only when the remaining lanes of that population
    make the mapping unambiguous.  Otherwise the dead binding is dropped so a
    stale project remains usable and the repair is reported to the load log.
    """
    candidates = {population: [] for population in SETUP_LANE_POPULATIONS.values()}
    all_lane_ids = set()
    for lane_index, raw_recipe in enumerate(lane_recipes or []):
        recipe = as_plain_record(raw_recipe)
        lane_id = str(recipe.get("lane_id") or "")
        population = _lane_population(recipe)
        if lane_id:
            all_lane_ids.add(lane_id)
        if lane_id and population in candidates:
            candidates[population].append((lane_index, lane_id))

    repaired = []
    warnings = []
    for raw_setup in setups or []:
        if not isinstance(raw_setup, dict):
            continue
        setup = copy.deepcopy(raw_setup)
        setup_id = str(setup.get("setup_id") or "")
        if str(setup.get("mode") or "") != "reference":
            repaired.append(setup)
            continue
        for key, population in SETUP_LANE_POPULATIONS.items():
            source_ids = [str(value or "") for value in setup.get(key) or []
                          if str(value or "")]
            population_rows = candidates[population]
            valid_ids = {lane_id for _index, lane_id in population_rows}
            used = {lane_id for lane_id in source_ids if lane_id in valid_ids}
            # An id that still exists on a different population is not a
            # missing lane. Preserve it so resolve_setup reports the authored
            # population mismatch instead of silently guessing another lane.
            missing_positions = [index for index, lane_id in enumerate(source_ids)
                                 if lane_id not in all_lane_ids]
            available = [lane_id for _index, lane_id in population_rows
                         if lane_id not in used]
            replacements = {}
            if missing_positions and len(missing_positions) == len(available):
                replacements = dict(zip(missing_positions, available))

            next_ids = []
            for index, lane_id in enumerate(source_ids):
                if lane_id in all_lane_ids:
                    resolved = lane_id
                else:
                    resolved = replacements.get(index, "")
                    warnings.append({
                        "code": ("rebound_setup_lane" if resolved
                                 else "dropped_setup_lane"),
                        "setup_id": setup_id,
                        "population": population,
                        "old_lane_id": lane_id,
                        "lane_id": resolved,
                    })
                if resolved and resolved not in next_ids:
                    next_ids.append(resolved)
            setup[key] = next_ids
        repaired.append(setup)
    return repaired, warnings


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


def default_reference_setup(picture_lane_ids=(), video_lane_ids=(), audio_lane_ids=()) -> dict:
    return normalize_setup({
        "name": "MiniMax H3 Reference Setup", "mode": "reference",
        "picture_lane_ids": list(picture_lane_ids),
        "video_lane_ids": list(video_lane_ids),
        "audio_lane_ids": list(audio_lane_ids),
    })


def active_setup(scene_or_dict) -> dict | None:
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

    def collect(lane_ids, declaration):
        population = str((declaration or {}).get("key") or "")
        cap = int((declaration or {}).get("cap") or 0)
        expected_types = set((declaration or {}).get("media_kinds") or [])
        rows = []
        for lane_id in lane_ids:
            lane_value = recipe_lookup.get(lane_id)
            if lane_value is None:
                errors.append({"code": "missing_setup_lane", "lane_id": lane_id,
                               "message": "A conditioning setup lane no longer exists."})
                continue
            lane_index, recipe_wrapper = lane_value
            recipe = (as_plain_record(recipe_wrapper.get("recipe"))
                      if recipe_wrapper.get("recipe") is not None
                      else recipe_wrapper)
            soft = as_plain_record(recipe.get("soft"))
            compatible_profiles = [str(entry) for entry in
                                   soft.get("compatible_profiles", [])]
            physical_population = str(soft.get("physical_population") or "none")
            if compatible_profiles and active_profile_key not in compatible_profiles:
                errors.append({"code": "setup_lane_profile_incompatible", "lane_id": lane_id,
                               "message": "A conditioning lane recipe is not compatible with MiniMax H3 Full Reference."})
                continue
            if physical_population != population:
                errors.append({"code": "setup_lane_population_mismatch", "lane_id": lane_id,
                               "message": f"This setup slot requires a {population} recipe declaration."})
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

    pictures_decl = populations_by_key.get(SETUP_LANE_POPULATIONS["picture_lane_ids"])
    videos_decl = populations_by_key.get(SETUP_LANE_POPULATIONS["video_lane_ids"])
    audio_decl = populations_by_key.get(SETUP_LANE_POPULATIONS["audio_lane_ids"])
    pictures = collect(value["picture_lane_ids"], pictures_decl) if pictures_decl else []
    videos = collect(value["video_lane_ids"], videos_decl) if videos_decl else []
    standalone = collect(value["audio_lane_ids"], audio_decl) if audio_decl else []
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
