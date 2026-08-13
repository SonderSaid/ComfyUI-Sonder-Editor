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
        pattern, index, str(record.get("prompt") or ""),
        str(record.get("entity_name") or record.get("name") or ""),
        record.get("registry_numbers") if isinstance(record, dict) else None,
        str(record.get("member_name") or ""))
        for index, record in enumerate(records) if isinstance(record, dict)]
    override = str(item.get("prompt_override") or "").strip()
    prefix = str(soft.get("prompt_prefix") or "").strip()
    aggregate = override or " ".join(value for value in (
        prefix, ", ".join(value for value in fragments if value)) if value)
    return aggregate, fragments
