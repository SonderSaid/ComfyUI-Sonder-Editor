from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from typing import Any


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


def _as_id(value: Any) -> str:
    return str(value or "")


def _workflow_nodes(workflow: dict | None) -> list[dict]:
    nodes = workflow.get("nodes") if isinstance(workflow, dict) else None
    return [node for node in nodes if isinstance(node, dict)] if isinstance(nodes, list) else []


def _workflow_subgraphs(workflow: dict | None) -> list[dict]:
    if not isinstance(workflow, dict):
        return []
    definitions = workflow.get("definitions")
    if isinstance(definitions, dict) and isinstance(definitions.get("subgraphs"), list):
        return [item for item in definitions["subgraphs"] if isinstance(item, dict)]
    if isinstance(workflow.get("subgraphs"), list):
        return [item for item in workflow["subgraphs"] if isinstance(item, dict)]
    return []


def _node_id(node: dict | None) -> str:
    return _as_id((node or {}).get("id"))


def _find_node_by_id(nodes: list[dict], node_id: str) -> dict | None:
    for node in nodes:
        if _node_id(node) == node_id:
            return node
    return None


def _subgraph_type_keys(subgraph: dict) -> list[str]:
    keys = []
    for key in ("id", "type", "name"):
        value = subgraph.get(key)
        if value is not None:
            keys.append(_as_id(value))
    return keys


def _find_collector_workflow_node(workflow: dict | None, unique_id: str) -> dict | None:
    unique = _as_id(unique_id)
    if not unique:
        return None
    main_nodes = _workflow_nodes(workflow)
    if ":" not in unique:
        return _find_node_by_id(main_nodes, unique)

    parent_id, child_id = unique.split(":", 1)
    parent_node = _find_node_by_id(main_nodes, parent_id)
    parent_type = _as_id((parent_node or {}).get("type") or (parent_node or {}).get("class_type"))
    for subgraph in _workflow_subgraphs(workflow):
        keys = set(_subgraph_type_keys(subgraph))
        if parent_type and parent_type not in keys:
            continue
        found = _find_node_by_id(_workflow_nodes(subgraph), child_id)
        if found:
            return found
    return None


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


def _raw_widget_text(inputs: dict) -> str:
    return ", ".join(f"{key}: {value}" for key, value in inputs.items())


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
        "power_loras": _cap_field_value(summary, structured=True),
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
    fields: dict | None = None
    display_type: str | None = None
    if handler is not None:
        try:
            transformed = handler["transform"](inputs)
        except Exception:
            transformed = None
        if transformed:
            fields = transformed
            display_type = handler["display_type"]
    if fields is None:
        fields = {str(key): _cap_field_value(value) for key, value in inputs.items()}
    return {
        "label": section_label,
        "source_node_id": prompt_key,
        "source_node_class": class_type,
        "source_node_title": title,
        "raw_widget_text": _raw_widget_text(inputs),
        "fields": fields,
        "display_type": display_type,
    }


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
        prompt = kwargs.get("prompt")
        extra_pnginfo = kwargs.get("extra_pnginfo")
        workflow = extra_pnginfo.get("workflow") if isinstance(extra_pnginfo, dict) else None
        if prompt is None or workflow is None:
            return float("NaN")
        labels = {
            key: value
            for key, value in kwargs.items()
            if isinstance(key, str) and key.startswith("label_")
        }
        try:
            payload = json.dumps(
                {"prompt": prompt, "workflow": workflow, "labels": labels},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        except Exception:
            return float("NaN")
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def collect(self, project, prompt=None, extra_pnginfo=None, unique_id=None, **kwargs):
        context = getattr(project, "_execution_context", None)
        if not isinstance(context, dict):
            context = {}
            project._execution_context = context

        workflow = extra_pnginfo.get("workflow") if isinstance(extra_pnginfo, dict) else None
        chains = context.setdefault(TRACKED_METADATA_CONTEXT_KEY, {})
        if not isinstance(chains, dict):
            chains = {}
            context[TRACKED_METADATA_CONTEXT_KEY] = chains

        # Resolve THIS collector's own inputs from the executed prompt. ComfyUI has already
        # collapsed Set/Get/Reroute indirection into prompt links, so a wired value_N is
        # [origin_id, slot] pointing at the real upstream node -- unlike the workflow link
        # graph, which still contains the virtual indirection node.
        owner_key, my_entry = _prompt_node(prompt, _as_id(unique_id))
        my_inputs = my_entry.get("inputs") if isinstance(my_entry, dict) else None
        my_inputs = my_inputs if isinstance(my_inputs, dict) else {}

        # Inherit the upstream branch chain so each consumer sees only its own lineage;
        # overwriting our own entry keeps re-runs idempotent (no duplicate sections).
        parent_key, _parent_entry = _prompt_node(prompt, _project_input_origin_id(my_inputs))
        own_chain = list(chains.get(parent_key, [])) if parent_key else []

        for index in range(MAX_COLLECTOR_INPUTS):
            value_name = f"value_{index}"
            if value_name not in kwargs:
                continue
            origin_ref = my_inputs.get(value_name)
            if not (isinstance(origin_ref, list) and origin_ref):
                continue
            resolved_key, prompt_entry = _prompt_node(prompt, _as_id(origin_ref[0]))
            if not prompt_entry:
                continue
            origin_workflow_node = _find_collector_workflow_node(workflow, resolved_key)
            section = _section_from_origin(
                resolved_key,
                prompt_entry,
                origin_workflow_node,
                kwargs.get(f"label_{index}", ""),
            )
            own_chain.append(section)

        chains[owner_key] = own_chain
        return (project,)
