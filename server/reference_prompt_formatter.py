"""Shared deterministic formatter for Reference recipes and Context chips."""

from __future__ import annotations

import re
import unicodedata


PROJECT_SCOPED_PROMPT_TOKENS = (
    "{subject_n}", "{picture_n}", "{audio_n}", "{speaker_n}",
)


def prompt_label_token(value: str) -> str:
    """Return a deterministic prompt-safe Unicode label component."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    normalized = re.sub(r"[^\w-]+", "_", normalized, flags=re.UNICODE)
    return re.sub(r"_+", "_", normalized).strip("_")


def reference_member_labels(entity_name: str, member_name: str = "") -> dict:
    entity_display = str(entity_name or "").strip()
    member_display = str(member_name or "").strip()
    entity_token = prompt_label_token(entity_display)
    member_token = prompt_label_token(member_display)
    token = "_".join(value for value in (entity_token, member_token) if value)
    display = " · ".join(value for value in (entity_display, member_display) if value)
    return {
        "entity_name": entity_token,
        "member_name": member_token,
        "name": token,
        "display_name": display,
    }


def member_prompt_fragment(pattern: str, index: int, prompt: str, name: str,
                           registry_numbers: dict | None = None,
                           member_name: str = "") -> str:
    member_prompt = str(prompt or "").strip()
    labels = reference_member_labels(name, member_name)
    composite_name = labels["name"]
    label = member_prompt or composite_name
    if not pattern:
        return label
    numbers = registry_numbers if isinstance(registry_numbers, dict) else {}

    def expand(prompt_value):
        return (
            str(pattern)
            .replace("{subject_n}", str(numbers.get("subject_n", 0) or 0))
            .replace("{picture_n}", str(numbers.get("picture_n", 0) or 0))
            .replace("{audio_n}", str(numbers.get("audio_n", 0) or 0))
            .replace("{speaker_n}", str(numbers.get("speaker_n", 0) or 0))
            .replace("{n}", str(index + 1))
            .replace("{index}", str(index))
            .replace("{prompt}", prompt_value)
            .replace("{entity_name}", labels["entity_name"])
            .replace("{member_name}", labels["member_name"])
            .replace("{name}", composite_name)
        )

    expanded = expand(member_prompt)
    if any(token in pattern for token in (
            "{prompt}", "{name}", "{entity_name}", "{member_name}")):
        if not member_prompt and composite_name and "{prompt}" in pattern:
            expanded = expand(composite_name)
        return expanded.strip()
    return f"{expanded}: {label}" if label else expanded


def format_reference_prompt(*, item=None, members=None, recipe=None) -> tuple[str, list[str]]:
    """Return aggregate + per-member strings from plain serializable records."""
    item = item if isinstance(item, dict) else {}
    recipe = recipe if isinstance(recipe, dict) else {}
    records = members if isinstance(members, list) else []
    soft = recipe.get("soft") if isinstance(recipe.get("soft"), dict) else {}
    pattern = str(soft.get("prompt_tokens") or "")
    fragments = [member_prompt_fragment(
        pattern,
        int(record.get("slot_index")) if isinstance(record.get("slot_index"), int)
        and record.get("slot_index") >= 0 else index,
        str(record.get("prompt") or ""),
        str(record.get("entity_name") or record.get("name") or ""),
        record.get("registry_numbers") if isinstance(record, dict) else None,
        str(record.get("member_name") or ""))
        for index, record in enumerate(records) if isinstance(record, dict)]
    override = str(item.get("prompt_override") or "").strip()
    prefix = str(soft.get("prompt_prefix") or "").strip()
    suffix = str(soft.get("prompt_suffix") or "").strip()
    # `prefix . body . suffix` is one recipe-owned grammar, not two independent
    # leads: Ingredients' `Generated video:` exists only to close
    # `Reference sheet:`. Three consequences are deliberate, not oversights.
    # An override suppresses the suffix exactly as it already suppresses the
    # prefix - "Edit as override" seeds the override from this aggregate, so
    # re-appending would emit the closing label twice. `p01..p16` carry
    # `fragments`, never the aggregate, because the suffix closes the whole
    # block rather than each member. And multi-lane decoding sources the suffix
    # from lane 0 only, as `reference_core.decode_reference_prompts` already does
    # for the prefix.
    aggregate = override or " ".join(value for value in (
        prefix, ", ".join(value for value in fragments if value), suffix) if value)
    return aggregate, fragments


def as_plain_record(value) -> dict:
    """Return a serializable record view without choosing any authority."""
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return {}


def _field(value, name, default=""):
    return value.get(name, default) if isinstance(value, dict) \
        else getattr(value, name, default)


def build_reference_formatter_context(*, winners, catalog_records, assets,
                                      recipes, setup_data=None) -> dict:
    """Build registry numbers and formatted lane records from resolved inputs.

    This helper is intentionally authority-blind: callers must already choose
    live versus frozen data and resolve winning lane items.  It never loads a
    project, locates a scene, or resolves a setup.
    """
    setup_data = setup_data if isinstance(setup_data, dict) else {}
    seed = setup_data.get("registry") if isinstance(
        setup_data.get("registry"), dict) else {}
    registry = {
        "subjects": dict(seed.get("subjects") or {}),
        "pictures": dict(seed.get("pictures") or {}),
        "audios": dict(seed.get("audios") or {}),
        "speakers": dict(seed.get("speakers") or {}),
    }
    references = list(catalog_records or [])
    asset_lookup = {str(_field(asset, "asset_id") or ""): asset
                    for asset in assets or []}
    member_lookup = {}
    for reference in references:
        for member in _field(reference, "members", []) or []:
            member_id = str(_field(member, "member_id") or "")
            if member_id:
                member_lookup[member_id] = (reference, member)

    resolved = []
    for lane_index, winner in enumerate(winners or []):
        if not isinstance(winner, dict):
            continue
        item = as_plain_record(winner.get("item"))
        records = []
        for member_ref in item.get("members") or []:
            if not isinstance(member_ref, dict):
                continue
            pair = member_lookup.get(str(member_ref.get("member_id") or ""))
            if pair is not None:
                records.append(pair)
        resolved.append((lane_index, item, records))

    def assign(population, key):
        if key and key not in registry[population]:
            existing = [int(value) for value in registry[population].values()
                        if isinstance(value, int) and value > 0]
            registry[population][key] = (max(existing) if existing else 0) + 1

    for _lane_index, _item, records in resolved:
        for reference, member in records:
            entity_id = str(_field(reference, "reference_id") or "")
            member_id = str(_field(member, "member_id") or "")
            asset = asset_lookup.get(str(_field(member, "asset_id") or ""))
            is_audio = str(_field(asset, "asset_type") or "") == "audio"
            assign("subjects", entity_id)
            assign("audios" if is_audio else "pictures", member_id)
            tags = [str(value) for value in (_field(member, "tags", []) or [])]
            if is_audio and "sonder:voice_identity" in tags:
                assign("speakers", entity_id)

    recipe_values = list(recipes or [])
    lanes = []
    generic = {}
    for lane_index, item, records in resolved:
        wrapper = as_plain_record(recipe_values[lane_index]) \
            if lane_index < len(recipe_values) else {}
        recipe = wrapper.get("recipe") if isinstance(
            wrapper.get("recipe"), dict) else {}
        soft = recipe.get("soft") if isinstance(recipe.get("soft"), dict) else {}
        members = []
        for reference, member in records:
            entity_id = str(_field(reference, "reference_id") or "")
            member_id = str(_field(member, "member_id") or "")
            members.append({
                "name": str(_field(reference, "name") or ""),
                "entity_name": str(_field(reference, "name") or ""),
                "member_name": str(_field(member, "name") or ""),
                "prompt": str(_field(member, "prompt") or ""),
                "member_id": member_id,
                "entity_id": entity_id,
                "registry_numbers": {
                    "subject_n": registry["subjects"].get(entity_id, 0),
                    "picture_n": registry["pictures"].get(member_id, 0),
                    "audio_n": registry["audios"].get(member_id, 0),
                    "speaker_n": registry["speakers"].get(entity_id, 0),
                },
            })
        aggregate, fragments = format_reference_prompt(
            item=item, members=members, recipe=recipe)
        lane = {"lane_index": lane_index, "item": item, "wrapper": wrapper,
                "recipe": recipe, "members": members,
                "prompt": aggregate, "member_prompts": fragments}
        lanes.append(lane)
        item_id = str(item.get("reference_item_id") or "")
        if item_id:
            generic[item_id] = {
                "reference_item_id": item_id,
                "lane_index": lane_index,
                "lane_id": str(wrapper.get("lane_id") or ""),
                "recipe_id": str(wrapper.get("recipe_id") or ""),
                "prompt": aggregate,
                "member_prompts": fragments,
                "members": members,
                "compatible_profiles": [str(value) for value in
                                        soft.get("compatible_profiles", ["generic@1"])],
                "physical_population": str(
                    soft.get("physical_population") or "none"),
                "exposed_capabilities": [str(value) for value in
                                          soft.get("exposed_capabilities",
                                                   ["derived_prompt"])],
                "role_fields": [str(value) for value in
                                soft.get("role_fields", [])],
            }
    return {"registry": registry, "lanes": lanes,
            "generic_references": generic}
