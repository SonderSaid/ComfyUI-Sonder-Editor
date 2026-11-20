"""ComfyUI's validation refusal, in the Sonder App's words.

When ComfyUI refuses a prompt at ``/prompt`` it answers with ``node_errors``:
``{node_id: {"errors": [{type, message, details, extra_info}], "class_type", ...}}``.
``describe_node_errors`` turns them into a few short lines naming each
setting the way the user sees it: the Control's label when one exposes it,
otherwise the node's title and input.

Only fields that hold the user's own workflow values are shown. ``details``,
``exception_message`` and tracebacks are never shown: an exception raised
while validating can carry server paths, and the lines reach browsers on other
machines. ComfyUI's console keeps the full report.

Pure: no ComfyUI imports. Shared by Generate and Output Views branch runs
(``sdk_runner``).
"""

from __future__ import annotations

from typing import Any

from . import control_descriptors

import re

MAX_LINES = 6
MAX_LINE_CHARS = 300
MAX_VALUE_CHARS = 120
# Invisible format characters the tooltip cleaner keeps: bidi marks, the
# Arabic letter mark, zero-width characters and the word joiner / BOM.
_INVISIBLE = re.compile("[​-‏؜⁠﻿]")


def _clean(value: Any) -> str:
    """One interpolated value as plain text on one line: controls, bidi and
    zero-width characters dropped, whitespace collapsed, capped."""
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(item) for item in value)
    elif value is None:
        text = ""
    else:
        text = str(value)
    text = _INVISIBLE.sub("", control_descriptors._TOOLTIP_DROP.sub("", text))
    text = " ".join(text.split())
    if len(text) > MAX_VALUE_CHARS:
        text = text[:MAX_VALUE_CHARS - 1].rstrip() + "…"
    return text


def _cap(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS - 1].rstrip() + "…"


def _control_labels(view: dict | None) -> dict[tuple[str, str], str]:
    """(node id, input) → the label of the Control that sets it."""
    labels: dict[tuple[str, str], str] = {}
    for binding in (view or {}).get("bindings") or []:
        if not isinstance(binding, dict) or binding.get("hidden"):
            continue
        label = binding.get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        for target in binding.get("targets") or []:
            if isinstance(target, dict) and target.get("path") is not None and target.get("input"):
                labels.setdefault((str(target["path"]), str(target["input"])), label)
    return labels


def _title(prompt: dict, node_id: str, entry: dict) -> str:
    node = prompt.get(node_id) if isinstance(prompt, dict) else None
    fallback = entry.get("class_type") if isinstance(entry.get("class_type"), str) else ""
    if isinstance(node, dict):
        fallback = node.get("class_type") if isinstance(node.get("class_type"), str) else fallback
        return _clean(control_descriptors._node_title(node, fallback or "A node"))
    return _clean(fallback or "A node")


def _bound(input_config: Any, key: str) -> Any:
    # ComfyUI sends the input's declaration, ``[type, {min, max, ...}]``.
    if isinstance(input_config, (list, tuple)) and len(input_config) > 1 and isinstance(input_config[1], dict):
        value = input_config[1].get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def _line(reason: dict, title: str, input_name: str, label: str | None) -> str | None:
    kind = reason.get("type") if isinstance(reason.get("type"), str) else ""
    extra = reason.get("extra_info") if isinstance(reason.get("extra_info"), dict) else {}
    name = _clean(label) if label else (f"{title} · {_clean(input_name)}" if input_name else title)
    message = _clean(reason.get("message")) if isinstance(reason.get("message"), str) else ""
    if kind.startswith("exception"):
        return f"ComfyUI couldn't check “{title}”. The ComfyUI console has the details."
    if kind == "custom_validation_failed":
        # The node's own check: ComfyUI repeats one fixed message for every
        # input it passed (execution.py validate_inputs) and keeps the node's
        # reason in ``details``, which is never shown. One line per node.
        return f"“{title}” failed its own check. The ComfyUI console has the details."
    if kind == "value_not_in_list" and "received_value" in extra:
        return (f"“{name}”: “{_clean(extra['received_value'])}” isn't one of its choices on this ComfyUI. "
                "A node pack or model file may be missing.")
    if kind in ("value_smaller_than_min", "value_bigger_than_max") and "received_value" in extra:
        smaller = kind == "value_smaller_than_min"
        bound = _bound(extra.get("input_config"), "min" if smaller else "max")
        if bound is None:
            return f"“{name}”: {message}" if message else None
        return f"“{name}”: {_clean(extra['received_value'])} is {'below its minimum' if smaller else 'above its maximum'}, {bound}."
    if kind == "required_input_missing" and input_name:
        return f"“{title}” is missing its {_clean(input_name)} input."
    if message:
        return f"“{name}”: {message}"
    return None


def describe_node_errors(node_errors: Any, prompt: Any, view: dict | None = None, *,
                         limit: int = MAX_LINES) -> list[str]:
    """Plain-text lines for ComfyUI's ``node_errors``, at most ``limit`` plus a
    "+N more" line; empty when nothing usable is there. Never raises on an
    unexpected shape: what it can't read is skipped."""
    if not isinstance(node_errors, dict):
        return []
    prompt = prompt if isinstance(prompt, dict) else {}
    labels = _control_labels(view)
    lines: list[str] = []
    for node_id, entry in node_errors.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("errors"), list):
            continue
        node_id = str(node_id)
        title = _title(prompt, node_id, entry)
        for reason in entry["errors"]:
            if not isinstance(reason, dict):
                continue
            extra = reason.get("extra_info") if isinstance(reason.get("extra_info"), dict) else {}
            input_name = extra.get("input_name") if isinstance(extra.get("input_name"), str) else ""
            line = _line(reason, title, input_name, labels.get((node_id, input_name)) if input_name else None)
            if line:
                line = _cap(line)
                if line not in lines:
                    lines.append(line)
    if len(lines) > limit:
        more = len(lines) - limit
        lines = lines[:limit] + [f"+{more} more. The ComfyUI console lists every one."]
    return lines
