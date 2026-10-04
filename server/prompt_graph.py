"""Structural reads over a ComfyUI API-format prompt graph.

An API prompt maps node ids to ``{"class_type", "inputs"}``; a linked input is an
``[upstream_node_id, output_slot]`` pair. These helpers answer which node an input
links to, which nodes take their project from a given editor, and what literal a
widget input carries when a Sonder Control or a core Primitive feeds it.

The module itself imports only the standard library, so it can be read without
the editor node (torch, cv2). Importing it through the ``server`` package still
loads ``server.routes`` and, through it, torch.

``SonderEditor`` keeps one-line delegates to these functions, which own the
upstream walk. Two editor helpers still parse prompts on their own:
``_execution_reaches_color_correcting_save_video`` scans Save Video nodes itself
(it uses this walk), and ``_referenced_output_slots`` applies a stricter link
rule (it rejects bool source ids).
"""

from typing import Any

SONDER_SAVE_CLASS_TYPES = frozenset({"SonderSaveVideo", "SonderSaveBridge"})
SONDER_EDITOR_CLASS_TYPE = "SonderEditor"
SONDER_CONTROL_CLASS_TYPE = "SonderControl"
# ComfyUI's core Primitive nodes hold their value in one ``value`` input.
PRIMITIVE_CLASS_TYPES = frozenset({
    "PrimitiveBoolean",
    "PrimitiveInt",
    "PrimitiveFloat",
    "PrimitiveString",
    "PrimitiveStringMultiline",
})
_LITERAL_LINK_DEPTH = 8


class _Unresolved:
    def __repr__(self) -> str:
        return "UNRESOLVED"


UNRESOLVED = _Unresolved()


def linked_node_id(value: Any) -> str | None:
    """Return the upstream node id of a linked input, or None for a literal."""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    node_id = value[0]
    if isinstance(node_id, (str, int)):
        return str(node_id)
    return None


def prompt_bool(value: Any) -> bool:
    """Read a prompt literal as a boolean; anything unrecognised is False."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def node_inputs(prompt: Any, node_id: Any) -> dict:
    """Return a node's ``inputs`` dict, or ``{}`` when the node or inputs are malformed."""
    if not isinstance(prompt, dict):
        return {}
    node = prompt.get(str(node_id))
    if not isinstance(node, dict):
        node = prompt.get(node_id)
    if not isinstance(node, dict):
        return {}
    inputs = node.get("inputs", {})
    return inputs if isinstance(inputs, dict) else {}


def upstream_reaches(prompt: Any, source_id: Any, target_id: Any) -> bool:
    """Whether walking upstream from ``source_id`` over linked inputs reaches ``target_id``."""
    if source_id is None:
        return False
    target = str(target_id)
    stack = [str(source_id)]
    seen = set()
    while stack:
        current_id = stack.pop()
        if current_id == target:
            return True
        if current_id in seen:
            continue
        seen.add(current_id)
        for upstream in node_inputs(prompt, current_id).values():
            upstream_id = linked_node_id(upstream)
            if upstream_id and upstream_id not in seen:
                stack.append(upstream_id)
    return False


def resolve_literal(prompt: Any, value: Any) -> Any:
    """Read an input as the literal it will carry at execution.

    A literal is returned as is. A link is followed only through sources whose
    output is their own widget value: a Sonder Control (its ``value_text``) and
    ComfyUI's core Primitive nodes (their ``value``). Any other link returns
    ``UNRESOLVED``, because its value exists only at execution.
    """
    for _ in range(_LITERAL_LINK_DEPTH):
        source_id = linked_node_id(value)
        if source_id is None:
            return value
        node = prompt.get(source_id) if isinstance(prompt, dict) else None
        if not isinstance(node, dict):
            return UNRESOLVED
        inputs = node_inputs(prompt, source_id)
        class_type = node.get("class_type")
        if class_type == SONDER_CONTROL_CLASS_TYPE:
            value = inputs.get("value_text")
        elif class_type in PRIMITIVE_CLASS_TYPES:
            value = inputs.get("value")
        else:
            return UNRESOLVED
    return UNRESOLVED


def editor_keys(prompt: Any) -> list[str]:
    """Prompt keys of every Sonder Editor, in prompt order."""
    if not isinstance(prompt, dict):
        return []
    return [
        str(key)
        for key, node in prompt.items()
        if isinstance(node, dict) and node.get("class_type") == SONDER_EDITOR_CLASS_TYPE
    ]


def nodes_reaching(prompt: Any, editor_id: Any, class_types: Any) -> list[str]:
    """Prompt keys of nodes of the given class types whose ``project`` input
    takes its project from this editor."""
    if not isinstance(prompt, dict) or editor_id in {None, ""}:
        return []
    wanted = frozenset(class_types or ())
    target_id = str(editor_id)
    found = []
    for key, node in prompt.items():
        if not isinstance(node, dict) or node.get("class_type") not in wanted:
            continue
        project_source = linked_node_id(node_inputs(prompt, key).get("project"))
        if upstream_reaches(prompt, project_source, target_id):
            found.append(str(key))
    return found


def execution_reaches_save_with_mark(prompt: Any, editor_id: Any, expected_mark_queue_complete: bool) -> bool:
    """Whether a Sonder save whose ``mark_queue_complete`` equals the expected value
    takes its project from this editor.

    A mark fed by a Sonder Control or a core Primitive reads as that node's
    value. Any other link is unknown until execution and reads as unmarked.
    """
    if not isinstance(prompt, dict) or editor_id in {None, ""}:
        return False
    for key in nodes_reaching(prompt, editor_id, SONDER_SAVE_CLASS_TYPES):
        mark = resolve_literal(prompt, node_inputs(prompt, key).get("mark_queue_complete"))
        if prompt_bool(mark) is expected_mark_queue_complete:
            return True
    return False
