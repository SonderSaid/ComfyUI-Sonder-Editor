"""Subgraph structure for Metadata Collector provenance.

The backend never sees a subgraph: the frontend flattens it into prompt keys
prefixed by the instance path ("433:8", nested "8:7:5"). The only record of
the subgraph itself is `extra_pnginfo.workflow` — the root instance node plus
`definitions.subgraphs[]` (inputs, outputs, nodes, links; link origin -10 is
the subgraph-input node, target -20 the subgraph-output node).

This module reads that structure only. Every value comes from the executed
prompt, and every workflow-derived node is cross-checked against the prompt's
`class_type`, so a stale workflow omits or falls back rather than inventing
provenance. The workflow is user-controlled JSON: every shape is validated, a
definition may not recur on its own descent path, and a step budget bounds the
whole resolve, so callers treat None as "use the inner-node section".
"""

from __future__ import annotations

from typing import Any

MAX_DEPTH = 8
MAX_STEPS = 5000
SUBGRAPH_INPUT_NODE = -10


def _as_id(value: Any) -> str:
    return "" if value is None else str(value)


def _dicts(value: Any) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def is_link(value: Any) -> bool:
    return (isinstance(value, list) and len(value) == 2
            and isinstance(value[0], str) and _int(value[1]) is not None)


def _link_record(link: Any) -> dict | None:
    """Normalize a subgraph link: dict form, or the root's list form."""
    if isinstance(link, dict):
        return link
    if isinstance(link, list) and len(link) >= 5:
        return {"id": link[0], "origin_id": link[1], "origin_slot": link[2],
                "target_id": link[3], "target_slot": link[4]}
    return None


def _find_node(nodes: list[dict], node_id: str) -> dict | None:
    for node in nodes:
        if _as_id(node.get("id")) == node_id:
            return node
    return None


def _definitions(workflow: Any) -> dict[str, dict]:
    if not isinstance(workflow, dict):
        return {}
    container = workflow.get("definitions")
    raw = container.get("subgraphs") if isinstance(container, dict) else workflow.get("subgraphs")
    return {_as_id(item.get("id")): item for item in _dicts(raw) if _as_id(item.get("id"))}


class _BudgetExceeded(Exception):
    pass


class _Resolve:
    """One resolve's caches and step budget over an untrusted workflow."""

    def __init__(self, workflow: Any, prompt: dict):
        self.workflow = workflow if isinstance(workflow, dict) else {}
        self.prompt = prompt
        self.definitions = _definitions(workflow)
        self._links: dict[str, dict[str, dict]] = {}
        self.steps = 0

    def step(self) -> None:
        self.steps += 1
        if self.steps > MAX_STEPS:
            raise _BudgetExceeded

    def links(self, definition: dict) -> dict[str, dict]:
        key = _as_id(definition.get("id"))
        if key not in self._links:
            table = {}
            for link in definition.get("links") if isinstance(definition.get("links"), list) else []:
                record = _link_record(link)
                if record is not None:
                    table[_as_id(record.get("id"))] = record
            self._links[key] = table
        return self._links[key]

    def entry(self, key: str) -> dict | None:
        entry = self.prompt.get(key)
        return entry if isinstance(entry, dict) else None

    def inputs(self, key: str) -> dict:
        inputs = (self.entry(key) or {}).get("inputs")
        return inputs if isinstance(inputs, dict) else {}


def prompt_title(prompt: dict, key: str) -> str:
    entry = prompt.get(key) if isinstance(prompt, dict) else None
    entry = entry if isinstance(entry, dict) else {}
    meta = entry.get("_meta")
    title = meta.get("title") if isinstance(meta, dict) else None
    return str(title or entry.get("class_type") or key)


def unit_key(collector_key: str, origin_key: str) -> str | None:
    """The outermost subgraph instance on origin's path that does not contain the collector.

    Collector `2` and origin `8:7:5` give `8`; collector `7:9` and origin `7:8:5`
    give `7:8`; collector `7:9` and origin `5:2` give `5`; a plain node at or
    above the collector's own graph (`8:12` / `8:5`) gives None.
    """
    collector_path = collector_key.split(":")[:-1] if collector_key else []
    origin = origin_key.split(":") if origin_key else []
    shared = 0
    while shared < min(len(collector_path), len(origin) - 1) and collector_path[shared] == origin[shared]:
        shared += 1
    if len(origin) - shared < 2:
        return None
    return ":".join(origin[: shared + 1])


def walk_instance_path(workflow: Any, instance_key: str) -> list[tuple[dict, dict]] | None:
    """[(instance node, its definition), ...] from the root to `instance_key`."""
    definitions = _definitions(workflow)
    nodes = _dicts(workflow.get("nodes")) if isinstance(workflow, dict) else []
    path = instance_key.split(":") if instance_key else []
    if not path or len(path) > MAX_DEPTH:
        return None
    chain: list[tuple[dict, dict]] = []
    seen: set[str] = set()
    for segment in path:
        node = _find_node(nodes, segment)
        definition = definitions.get(_as_id((node or {}).get("type")))
        definition_id = _as_id((definition or {}).get("id"))
        if node is None or definition is None or definition_id in seen:
            return None
        seen.add(definition_id)
        chain.append((node, definition))
        nodes = _dicts(definition.get("nodes"))
    return chain


def find_workflow_node(workflow: Any, prompt_key: str) -> dict | None:
    """The workflow node for a (possibly nested) prompt key."""
    if not isinstance(workflow, dict) or not prompt_key:
        return None
    parts = prompt_key.split(":")
    if len(parts) == 1:
        return _find_node(_dicts(workflow.get("nodes")), parts[0])
    chain = walk_instance_path(workflow, ":".join(parts[:-1]))
    return _find_node(_dicts(chain[-1][1].get("nodes")), parts[-1]) if chain else None


def checked_workflow_node(workflow: Any, prompt: dict, prompt_key: str) -> dict | None:
    """The workflow node for prompt_key only when its type matches what executed."""
    node = find_workflow_node(workflow, prompt_key)
    entry = prompt.get(prompt_key) if isinstance(prompt, dict) else None
    if node is None or not isinstance(entry, dict):
        return None
    return node if _as_id(node.get("type")) == _as_id(entry.get("class_type")) else None


def _concrete_target(ctx: _Resolve, definition, prefix, node_id, slot, path: frozenset) -> tuple[str, str] | None:
    """Follow a definition link target down to a real prompt node and input name."""
    ctx.step()
    node = _find_node(_dicts(definition.get("nodes")), node_id)
    inputs = _dicts((node or {}).get("inputs"))
    index = _int(slot)
    if node is None or index is None or not 0 <= index < len(inputs):
        return None
    input_name = _as_id(inputs[index].get("name"))
    key = f"{prefix}{node_id}"
    nested = ctx.definitions.get(_as_id(node.get("type")))
    if nested is None:
        entry = ctx.entry(key)
        if entry is None or _as_id(entry.get("class_type")) != _as_id(node.get("type")):
            return None
        return key, input_name
    nested_id = _as_id(nested.get("id"))
    if nested_id in path or len(path) >= MAX_DEPTH:
        return None
    # A nested instance input maps to its definition input by name, not index.
    for nested_input in _dicts(nested.get("inputs")):
        if _as_id(nested_input.get("name")) == input_name:
            return _first_target(ctx, nested, f"{key}:", nested_input, path | {nested_id})
    return None


def _first_target(ctx: _Resolve, definition, prefix, interface_input, path: frozenset) -> tuple[str, str] | None:
    links = ctx.links(definition)
    link_ids = interface_input.get("linkIds")
    for link_id in link_ids if isinstance(link_ids, list) else []:
        link = links.get(_as_id(link_id))
        if link is None or _int(link.get("origin_id")) != SUBGRAPH_INPUT_NODE:
            continue
        target = _concrete_target(
            ctx, definition, prefix, _as_id(link.get("target_id")), link.get("target_slot"), path,
        )
        if target is not None:
            return target
    return None


def _is_primitive(entry: dict) -> bool:
    return _as_id(entry.get("class_type")).startswith("Primitive")


def _field_value(ctx: _Resolve, key: str, input_name: str) -> tuple[bool, Any]:
    inputs = ctx.inputs(key)
    if input_name not in inputs:
        return False, None
    value = inputs[input_name]
    if not is_link(value):
        return True, value
    source_key = value[0]
    source = ctx.entry(source_key) or {}
    source_inputs = ctx.inputs(source_key)
    title = prompt_title(ctx.prompt, source_key)
    if len(source_inputs) == 1:
        (only,) = source_inputs.values()
        if not is_link(only):
            # One hop: a primitive is the value itself; any other single-value
            # node (a loader's filename) is named so it is not read as literal.
            return True, only if _is_primitive(source) else f"← {title}: {only}"
    return True, f"← {title} [{source_key}]"


def _interface_fields(ctx: _Resolve, definition, prefix) -> tuple[dict[str, Any], set[tuple[str, str]]]:
    fields: dict[str, Any] = {}
    targets: set[tuple[str, str]] = set()
    root = frozenset({_as_id(definition.get("id"))})
    for interface_input in _dicts(definition.get("inputs")):
        target = _first_target(ctx, definition, prefix, interface_input, root)
        if target is None:
            continue
        present, value = _field_value(ctx, *target)
        if not present:
            continue
        targets.add(target)
        base = _as_id(interface_input.get("label") or interface_input.get("name")) or "input"
        label, suffix = base, 2
        while label in fields:
            label, suffix = f"{base} #{suffix}", suffix + 1
        fields[label] = value
    return fields, targets


def _output_origin(ctx: _Resolve, definition, prefix, output_index, path: frozenset) -> tuple[str, int] | None:
    ctx.step()
    outputs = _dicts(definition.get("outputs"))
    if not 0 <= output_index < len(outputs):
        return None
    links = ctx.links(definition)
    link_ids = outputs[output_index].get("linkIds")
    for link_id in link_ids if isinstance(link_ids, list) else []:
        link = links.get(_as_id(link_id))
        origin_slot = _int((link or {}).get("origin_slot"))
        if link is None or origin_slot is None or _int(link.get("origin_id")) == SUBGRAPH_INPUT_NODE:
            continue
        origin_id = _as_id(link.get("origin_id"))
        node = _find_node(_dicts(definition.get("nodes")), origin_id)
        nested = ctx.definitions.get(_as_id((node or {}).get("type")))
        if nested is None:
            return f"{prefix}{origin_id}", origin_slot
        nested_id = _as_id(nested.get("id"))
        if nested_id not in path and len(path) < MAX_DEPTH:
            return _output_origin(ctx, nested, f"{prefix}{origin_id}:", origin_slot, path | {nested_id})
    return None


def _tapped_output(ctx: _Resolve, definition, prefix, origin_ref) -> str:
    target = (origin_ref[0], origin_ref[1])
    root = frozenset({_as_id(definition.get("id"))})
    for index, output in enumerate(_dicts(definition.get("outputs"))):
        if _output_origin(ctx, definition, prefix, index, root) == target:
            return _as_id(output.get("label") or output.get("name"))
    return ""


def upstream_keys(prompt: dict, start_key: str, prefix: str) -> list[str]:
    """Prompt keys feeding start_key inside one unit, nearest first."""
    ordered: list[str] = []
    seen = {start_key}
    frontier = [start_key]
    while frontier:
        key = frontier.pop(0)
        entry = prompt.get(key)
        inputs = entry.get("inputs") if isinstance(entry, dict) else None
        for value in inputs.values() if isinstance(inputs, dict) else []:
            if is_link(value) and value[0].startswith(prefix) and value[0] not in seen:
                seen.add(value[0])
                ordered.append(value[0])
                frontier.append(value[0])
    return ordered


def resolve_subgraph_origin(
    collector_key: str, origin_ref: Any, prompt: dict | None, workflow: dict | None,
) -> dict | None:
    """Describe the subgraph instance that produced origin_ref, or None to fall back."""
    if not isinstance(prompt, dict) or not is_link(origin_ref):
        return None
    instance = unit_key(collector_key, origin_ref[0])
    chain = walk_instance_path(workflow, instance) if instance else None
    if not chain:
        return None
    instance_node, definition = chain[-1]
    producer = checked_workflow_node(workflow, prompt, origin_ref[0])
    if producer is None:
        return None
    prefix = f"{instance}:"
    ctx = _Resolve(workflow, prompt)
    try:
        fields, targets = _interface_fields(ctx, definition, prefix)
        output_name = _tapped_output(ctx, definition, prefix, origin_ref)
    except _BudgetExceeded:
        return None
    return {
        "instance_key": instance,
        "instance_title": _as_id(instance_node.get("title")),
        "definition_id": _as_id(definition.get("id")),
        "definition_name": _as_id(definition.get("name")) or "Subgraph",
        "fields": fields,
        "interface_targets": targets,
        "output_name": output_name,
        "producer_key": origin_ref[0],
        "producer_node": producer,
        "prefix": prefix,
    }
