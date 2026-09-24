from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from typing import Any

from .subgraph_provenance import (
    checked_workflow_node,
    find_workflow_node,
    is_link,
    prompt_title,
    resolve_subgraph_origin,
    upstream_keys,
)


class _AnyType(str):
    def __ne__(self, other):  # noqa: D401 - make any type comparison succeed
        return False


_ANY = _AnyType("*")
# Internal execution-context sentinel. Holds a dict keyed by each collector's canonical
# prompt key: {owner_key: [section, ...]}, where each list is the full branch chain
# accumulated up to and including that collector (its parent collector's chain + its own
# sections). Never serialized into project.json: the key is "_"-prefixed so
# `_public_execution_context` strips it; the public `editor_export.tracked_metadata` is
# derived per consumer at asset-write time via `collector_chain_for_consumer`.
TRACKED_METADATA_CONTEXT_KEY = "_tracked_metadata_internal"
FIELD_VALUE_LIMIT = 2048
TRUNCATED_MARKER = "... [truncated, full in raw_widget_text]"


def _resolve_max_collector_inputs() -> int:
    raw = os.environ.get("SONDER_COLLECTOR_MAX_INPUTS", "12") or "12"
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 12
    return max(1, min(32, value))


MAX_COLLECTOR_INPUTS = _resolve_max_collector_inputs()
MAX_V3_COLLECTOR_INPUTS = 32


def _as_id(value: Any) -> str:
    return str(value or "")


def _find_collector_workflow_node(workflow: dict | None, unique_id: str) -> dict | None:
    return find_workflow_node(workflow, _as_id(unique_id))


def _prompt_node(prompt: dict | None, prompt_key: str) -> tuple[str, dict | None]:
    if not isinstance(prompt, dict) or not prompt_key:
        return prompt_key, None
    candidates = [prompt_key]
    if ":" in prompt_key:
        candidates.append(prompt_key.split(":", 1)[1])
    for candidate in candidates:
        node = prompt.get(candidate)
        if isinstance(node, dict):
            return candidate, node
    return prompt_key, None


def _json_safe(value: Any) -> Any:
    try:
        cloned = copy.deepcopy(value)
        json.dumps(cloned)
        return cloned
    except Exception:
        return str(value)


def _cap_field_value(value: Any, *, structured: bool = False) -> Any:
    safe = _json_safe(value)
    if structured:
        return _cap_structured(safe)
    try:
        encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True)
    except Exception:
        encoded = str(safe)
        safe = encoded
    if len(encoded) <= FIELD_VALUE_LIMIT:
        return safe
    text = str(safe)
    return f"{text[:FIELD_VALUE_LIMIT]}{TRUNCATED_MARKER}"


def _cap_structured(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _cap_structured(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_cap_structured(item) for item in value]
    if isinstance(value, str) and len(value) > FIELD_VALUE_LIMIT:
        return f"{value[:FIELD_VALUE_LIMIT]}{TRUNCATED_MARKER}"
    return value


def _utf16_units(text: str) -> int:
    """Count JavaScript string indices, including surrogate pairs."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _raw_widget_text_and_spans(inputs: dict) -> tuple[str, dict[str, list[int]]]:
    parts: list[str] = []
    spans: dict[str, list[int]] = {}
    offset = 0
    for key, value in inputs.items():
        if parts:
            parts.append(", ")
            offset += 2
        key_text = str(key)
        prefix = f"{key_text}: "
        value_text = str(value)
        parts.extend((prefix, value_text))
        offset += _utf16_units(prefix)
        start = offset
        offset += _utf16_units(value_text)
        spans[key_text] = [start, offset]
    return "".join(parts), spans


def _raw_widget_text(inputs: dict) -> str:
    return _raw_widget_text_and_spans(inputs)[0]


def _workflow_title(workflow_node: dict | None) -> str:
    title = (workflow_node or {}).get("title")
    return str(title or "").strip()


def _is_power_lora_loader(class_type: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", str(class_type or "").lower())
    return "powerlora" in normalized and "loader" in normalized


def _coerce_lora_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    text = str(value).strip().lower()
    if text in {"false", "0", "off", "disabled", "none"}:
        return False
    return True


def _power_lora_summary(inputs: dict) -> list[dict]:
    grouped: dict[int, dict[str, Any]] = {}
    for key, value in inputs.items():
        match = re.match(r"^(.*?)[_\-\s]?(\d+)$", str(key))
        if not match:
            continue
        base = re.sub(r"[^a-z0-9]+", "_", match.group(1).strip().lower()).strip("_")
        index = int(match.group(2))
        slot = grouped.setdefault(index, {})
        if base == "lora" and isinstance(value, dict):
            for nested_key, nested_value in value.items():
                nested_base = re.sub(r"[^a-z0-9]+", "_", str(nested_key).strip().lower()).strip("_")
                if nested_base:
                    slot[nested_base] = nested_value
        else:
            slot[base] = value

    rows = []
    for index in sorted(grouped):
        values = grouped[index]
        name = (
            values.get("lora")
            or values.get("lora_name")
            or values.get("name")
            or values.get("model")
        )
        if name is None or str(name).strip() in {"", "None", "none"}:
            continue
        row = {
            "slot": index,
            "name": str(name),
            "enabled": _coerce_lora_enabled(
                values.get("on", values.get("enabled", values.get("active", True)))
            ),
        }
        if "strength" in values:
            row["strength"] = values["strength"]
        if "strength_model" in values:
            row["model_strength"] = values["strength_model"]
        elif "model_strength" in values:
            row["model_strength"] = values["model_strength"]
        if "strength_clip" in values:
            row["clip_strength"] = values["strength_clip"]
        elif "clip_strength" in values:
            row["clip_strength"] = values["clip_strength"]
        rows.append(row)
    return rows


def _power_lora_transform(inputs: dict) -> dict | None:
    summary = _power_lora_summary(inputs)
    if not summary:
        return None
    return {
        "power_loras": summary,
        "enabled_lora_count": sum(1 for row in summary if row.get("enabled") is not False),
        "total_lora_count": len(summary),
    }


# Compatibility registry. Add a new node-pack handler by appending to this list.
# Each entry: {"display_type", "predicate", "transform"}.
#   - predicate(class_type: str) -> bool  : matches the node's prompt class_type.
#   - transform(inputs: dict)    -> dict|None : structured fields dict, or None to fall through to generic dump.
# First match wins. Schema is additive: handlers don't bump editor_export.schema_version.
COMPAT_HANDLERS: list[dict] = [
    {
        "display_type": "power_loras",
        "predicate": _is_power_lora_loader,
        "transform": _power_lora_transform,
    },
]


def _resolve_compat_handler(class_type: str) -> dict | None:
    for handler in COMPAT_HANDLERS:
        try:
            if handler["predicate"](class_type):
                return handler
        except Exception:
            continue
    return None


def _section_from_origin(prompt_key: str, prompt_entry: dict, workflow_node: dict | None, label: str) -> dict:
    inputs = prompt_entry.get("inputs")
    if not isinstance(inputs, dict):
        inputs = {}
    class_type = str(prompt_entry.get("class_type") or "")
    title = _workflow_title(workflow_node)
    section_label = str(label or "").strip() or title or class_type or prompt_key

    handler = _resolve_compat_handler(class_type)
    source_fields: dict | None = None
    display_type: str | None = None
    if handler is not None:
        try:
            transformed = handler["transform"](inputs)
        except Exception:
            transformed = None
        if transformed:
            source_fields = transformed
            display_type = handler["display_type"]
    if source_fields is None:
        source_fields = inputs

    return _assemble_section(
        {
            "label": section_label,
            "source_node_id": prompt_key,
            "source_node_class": class_type,
            "source_node_title": title,
        },
        inputs,
        source_fields,
        display_type,
        structured=display_type is not None,
    )


def _assemble_section(header: dict, raw_inputs: dict, source_fields: dict, display_type, *, structured: bool) -> dict:
    raw_widget_text, input_spans = _raw_widget_text_and_spans(raw_inputs)
    fields: dict = {}
    has_capped_scalar = False
    full_fields: dict = {}
    for key, value in source_fields.items():
        field_key = str(key)
        safe = _json_safe(value)
        capped = _cap_field_value(safe, structured=structured)
        fields[field_key] = capped
        if capped != safe:
            # Generic scalars already exist verbatim in raw_widget_text. Spans
            # avoid repeating a long prompt in every asset/take provenance copy.
            if not structured and not isinstance(safe, (dict, list, tuple)) and field_key in input_spans:
                has_capped_scalar = True
            else:
                # Transformed rows and containers need their JSON shape for Copy.
                full_fields[field_key] = safe

    section = {
        **header,
        "raw_widget_text": raw_widget_text,
        "fields": fields,
        "display_type": display_type,
    }
    if has_capped_scalar:
        # The complete ordered partition lets the gallery validate a capped
        # field's boundaries even when another raw value contains ", key: ".
        section["raw_field_spans"] = input_spans
    if full_fields:
        section["full_fields"] = full_fields
    return section


def _has_literal_input(prompt_entry: Any) -> bool:
    inputs = prompt_entry.get("inputs") if isinstance(prompt_entry, dict) else None
    return isinstance(inputs, dict) and any(not is_link(value) for value in inputs.values())


def _subgraph_sections(collector_key: str, origin_ref, prompt: dict | None, workflow: dict | None,
                       label: str, emitted: dict) -> list[dict] | None:
    """Sections for an origin inside a subgraph instance, or None to use the inner node.

    The subgraph's interface becomes one section per instance (every tapped
    output is recorded on it). The producing node keeps its own values, minus
    those the interface already shows, as a sibling, and compat-handled
    ancestors on the tapped branch follow. `emitted` spans one collector run:
    an origin whose sections were all emitted by an earlier input returns [],
    which is resolved, not a fallback.
    """
    try:
        origin = resolve_subgraph_origin(collector_key, origin_ref, prompt, workflow)
        if origin is None:
            return None
        return _subgraph_origin_sections(origin, prompt, workflow, label, emitted)
    except Exception:
        # Workflow JSON is user-controlled; malformed shapes must never fail a run.
        return None


def _subgraph_origin_sections(origin: dict, prompt: dict, workflow, label: str, emitted: dict) -> list[dict] | None:
    keys: set = emitted.setdefault("keys", set())
    units: dict = emitted.setdefault("units", {})
    labels: set = emitted.setdefault("labels", set())
    sections: list[dict] = []
    resolved = False

    instance_key = origin["instance_key"]
    unit_label = str(label or "").strip() or origin["instance_title"] or origin["definition_name"]
    if instance_key not in units and unit_label.lower() in labels:
        # Untitled instances of one definition would share a label and pin key.
        unit_label = f"{unit_label} [{instance_key}]"
    fields = origin["fields"]
    if fields:
        resolved = True
        existing = units.get(instance_key)
        if existing is not None:
            names = existing["subgraph"]["output_names"]
            if origin["output_name"] and origin["output_name"] not in names:
                names.append(origin["output_name"])
            unit_label = existing["label"]
        else:
            section = _assemble_section(
                {
                    "label": unit_label,
                    "source_node_id": instance_key,
                    "source_node_class": origin["definition_name"],
                    "source_node_title": origin["instance_title"],
                },
                fields,
                fields,
                "subgraph",
                structured=False,
            )
            section["subgraph"] = {
                "definition_id": origin["definition_id"],
                "definition_name": origin["definition_name"],
                "instance_key": instance_key,
                "output_names": [origin["output_name"]] if origin["output_name"] else [],
                "producer_node_id": origin["producer_key"],
            }
            units[instance_key] = section
            labels.add(unit_label.lower())
            sections.append(section)

    producer_key = origin["producer_key"]
    producer_entry = prompt.get(producer_key)
    shown = {name for key, name in origin["interface_targets"] if key == producer_key}
    producer_inputs = producer_entry.get("inputs") if isinstance(producer_entry, dict) else None
    remaining = {
        name: value for name, value in (producer_inputs if isinstance(producer_inputs, dict) else {}).items()
        if name not in shown
    }
    remaining_entry = {**producer_entry, "inputs": remaining} if isinstance(producer_entry, dict) else None
    if _has_literal_input(remaining_entry):
        resolved = True
        if ("node", producer_key) not in keys:
            keys.add(("node", producer_key))
            producer_label = _workflow_title(origin["producer_node"]) or prompt_title(prompt, producer_key)
            sections.append(_section_from_origin(
                producer_key, remaining_entry, origin["producer_node"], f"{unit_label} \u203a {producer_label}",
            ))

    for ancestor_key in upstream_keys(prompt, producer_key, origin["prefix"]):
        entry = prompt.get(ancestor_key)
        if not isinstance(entry, dict) or _resolve_compat_handler(str(entry.get("class_type") or "")) is None:
            continue
        resolved = True
        if ("node", ancestor_key) in keys:
            continue
        keys.add(("node", ancestor_key))
        node = checked_workflow_node(workflow, prompt, ancestor_key)
        ancestor_label = _workflow_title(node) or prompt_title(prompt, ancestor_key)
        sections.append(_section_from_origin(ancestor_key, entry, node, f"{unit_label} \u203a {ancestor_label}"))
    return sections if resolved else None


def _project_input_origin_id(inputs: dict | None) -> str:
    project_ref = (inputs or {}).get("project")
    if isinstance(project_ref, list) and project_ref:
        return _as_id(project_ref[0])
    return ""


def collector_chain_for_consumer(context: dict | None, prompt: dict | None, consumer_unique_id) -> list:
    """Branch-correct tracked-metadata sections for a node that consumes a project (e.g. a
    save node). Returns the chain accumulated by the collector directly feeding the
    consumer's project input, in editor->leaf order; [] when no such collector exists or
    the wiring cannot be resolved from the prompt."""
    chains = (context or {}).get(TRACKED_METADATA_CONTEXT_KEY)
    if not isinstance(chains, dict):
        return []
    _consumer_key, consumer_entry = _prompt_node(prompt, _as_id(consumer_unique_id))
    consumer_inputs = consumer_entry.get("inputs") if isinstance(consumer_entry, dict) else None
    parent_key, _parent_entry = _prompt_node(
        prompt,
        _project_input_origin_id(consumer_inputs if isinstance(consumer_inputs, dict) else {}),
    )
    chain = chains.get(parent_key) if parent_key else None
    if not isinstance(chain, list):
        return []
    return [section for section in chain if isinstance(section, dict)]


def collector_fingerprint(
    prompt: dict | None,
    extra_pnginfo: dict | None,
    labels: dict[str, Any] | None,
):
    workflow = extra_pnginfo.get("workflow") if isinstance(extra_pnginfo, dict) else None
    if prompt is None or workflow is None:
        return float("NaN")
    try:
        payload = json.dumps(
            {
                "prompt": prompt,
                "workflow": workflow,
                "labels": labels if isinstance(labels, dict) else {},
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    except Exception:
        return float("NaN")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalized_value_names(values: Any) -> set[str]:
    if not isinstance(values, dict):
        return set()
    normalized = set()
    for key in values:
        name = str(key)
        if name.startswith("values."):
            name = name.split(".", 1)[1]
        if re.fullmatch(r"value_\d+", name):
            normalized.add(name)
    return normalized


def _collector_origin_ref(inputs: dict, value_name: str):
    direct = inputs.get(value_name)
    if direct is not None:
        return direct
    dotted = inputs.get(f"values.{value_name}")
    if dotted is not None:
        return dotted
    nested = inputs.get("values")
    if isinstance(nested, dict):
        return nested.get(value_name)
    return None


def collect_metadata(
    project,
    *,
    prompt: dict | None,
    extra_pnginfo: dict | None,
    unique_id: Any,
    values: dict[str, Any] | None,
    labels: dict[str, Any] | None,
    capacity: int,
):
    context = getattr(project, "_execution_context", None)
    if not isinstance(context, dict):
        context = {}
        project._execution_context = context

    workflow = extra_pnginfo.get("workflow") if isinstance(extra_pnginfo, dict) else None
    chains = context.setdefault(TRACKED_METADATA_CONTEXT_KEY, {})
    if not isinstance(chains, dict):
        chains = {}
        context[TRACKED_METADATA_CONTEXT_KEY] = chains

    # Resolve this collector's own inputs from the executed prompt. V1 uses
    # value_N while V3 Autogrow flattens them to values.value_N.
    owner_key, my_entry = _prompt_node(prompt, _as_id(unique_id))
    my_inputs = my_entry.get("inputs") if isinstance(my_entry, dict) else None
    my_inputs = my_inputs if isinstance(my_inputs, dict) else {}

    # Overwriting our own chain makes repeated execution idempotent.
    parent_key, _parent_entry = _prompt_node(prompt, _project_input_origin_id(my_inputs))
    own_chain = list(chains.get(parent_key, [])) if parent_key else []
    value_names = _normalized_value_names(values)
    label_values = labels if isinstance(labels, dict) else {}
    emitted: dict = {}

    for index in range(max(0, int(capacity))):
        value_name = f"value_{index}"
        if value_name not in value_names:
            continue
        origin_ref = _collector_origin_ref(my_inputs, value_name)
        if not (isinstance(origin_ref, list) and origin_ref):
            continue
        resolved_key, prompt_entry = _prompt_node(prompt, _as_id(origin_ref[0]))
        if not prompt_entry:
            continue
        label = label_values.get(f"label_{index}", "")
        if resolved_key == _as_id(origin_ref[0]):
            subgraph_sections = _subgraph_sections(owner_key, origin_ref, prompt, workflow, label, emitted)
            if subgraph_sections is not None:
                own_chain.extend(subgraph_sections)
                continue
        origin_workflow_node = _find_collector_workflow_node(workflow, resolved_key)
        own_chain.append(_section_from_origin(resolved_key, prompt_entry, origin_workflow_node, label))

    chains[owner_key] = own_chain
    return project


class SonderMetadataCollector:
    CATEGORY = "Sonder/IO"
    FUNCTION = "collect"
    RETURN_TYPES = ("SONDER_PROJECT",)
    RETURN_NAMES = ("project",)
    DESCRIPTION = "Collects explicitly wired upstream widget values into Sonder asset metadata."

    @classmethod
    def INPUT_TYPES(cls):
        optional = {}
        for index in range(MAX_COLLECTOR_INPUTS):
            optional[f"value_{index}"] = (_ANY,)
            optional[f"label_{index}"] = ("STRING", {"default": "", "multiline": False})
        return {
            "required": {
                "project": ("SONDER_PROJECT",),
            },
            "optional": optional,
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
                "unique_id": "UNIQUE_ID",
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, **_kwargs):
        return True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        labels = {
            key: value
            for key, value in kwargs.items()
            if isinstance(key, str) and key.startswith("label_")
        }
        return collector_fingerprint(
            kwargs.get("prompt"),
            kwargs.get("extra_pnginfo"),
            labels,
        )

    def collect(self, project, prompt=None, extra_pnginfo=None, unique_id=None, **kwargs):
        values = {
            key: value
            for key, value in kwargs.items()
            if isinstance(key, str) and key.startswith("value_")
        }
        labels = {
            key: value
            for key, value in kwargs.items()
            if isinstance(key, str) and key.startswith("label_")
        }
        result = collect_metadata(
            project,
            prompt=prompt,
            extra_pnginfo=extra_pnginfo,
            unique_id=unique_id,
            values=values,
            labels=labels,
            capacity=MAX_COLLECTOR_INPUTS,
        )
        return (result,)
