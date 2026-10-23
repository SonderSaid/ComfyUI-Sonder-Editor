"""Node switches in the Sonder App: native mode routing over an API prompt.

An API export holds only the nodes that ran, with every link already resolved,
so it can't say what a muted or bypassed node would have changed. The canvas
exports a bounded *routing capsule* beside it (``execution.routing``, built by
web/js/control_bindings.js ``exportRouting``; JSON text, so its nesting never
counts against the manifest's depth cap): for each switch member, its
ports and where each input resolves, and every API input whose resolution
first reaches a member's output. ``compile_modes`` replays ComfyUI's own
serializer (frontend 1.57.1 ``executionUtil.graphToPrompt`` and
``ExecutableNodeDTO.resolveInput/resolveOutput``) for the members' modes:

- a muted node yields nothing; a bypassed one passes the input that
  ``bypass_index`` picks, resolved with that input's own type;
- an input that resolves to nothing keeps its node's widget value of the same
  name (the capsule's fallback), or is left out;
- a muted or bypassed node, or every node of a muted or bypassed instance, is
  dropped, and so is any link left pointing at a dropped node.

Only root nodes and root subgraph instances are members (Phase 1). Pure and
standard-library only.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

ROUTING_VERSION = 1
MAX_ROUTING_NODES = 512
MAX_ROUTING_EDGES = 2048
MAX_PORTS = 256
# The capsule's own text; the whole manifest stays under its 128 KiB cap.
MAX_ROUTING_BYTES = 48 * 1024
MODES = ("enabled", "muted", "bypassed")
OFF_MODES = ("muted", "bypassed")
ERRORS = {
    "budget": "This workflow is too large to switch nodes in the Sonder App.",
    "unsupported": "This workflow routes its links in a way the Sonder App can't switch.",
}
_ROOT_PATH_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_EXEC_PATH_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}(?::[A-Za-z0-9_-]{1,64}){0,15}$")
_FORBIDDEN = frozenset({"__proto__", "constructor", "prototype"})


class RoutingError(ValueError):
    """The capsule can't route this combination (a cycle, a missing port)."""


# ── Native rules ─────────────────────────────────────────────────────────────

def _wild(slot_type: Any) -> bool:
    # ``type == ''`` (loose: also 0 and false) or ``'*'`` becomes 0, then any
    # falsy type matches everything.
    return slot_type in (None, "", "*", 0, False)


def is_valid_connection(type_a: Any, type_b: Any) -> bool:
    """``LiteGraph.isValidConnection`` without the EVENT/ACTION pair."""
    if _wild(type_a) or _wild(type_b) or type_a == type_b:
        return True
    a, b = str(type_a).lower(), str(type_b).lower()
    if "," not in a and "," not in b:
        return a == b
    return any(is_valid_connection(x, y) for x in a.split(",") for y in b.split(","))


def bypass_index(input_types: list, output_type: Any, slot: int, slot_type: Any) -> int:
    """``ExecutableNodeDTO._getBypassSlotIndex``: which input a bypassed node
    passes to its output ``slot`` for a target of ``slot_type``; -1 for none."""
    if slot_type in ("*", ""):
        return slot if len(input_types) > slot else 0
    if slot < len(input_types) and is_valid_connection(input_types[slot], output_type) \
            and is_valid_connection(input_types[slot], slot_type):
        return slot
    for index, input_type in enumerate(input_types):
        if input_type == slot_type:
            return index
    for index, input_type in enumerate(input_types):
        if is_valid_connection(input_type, output_type) and is_valid_connection(input_type, slot_type):
            return index
    return -1


# ── Reading the capsule ──────────────────────────────────────────────────────

def _type(value: Any) -> Any:
    if value is None or (isinstance(value, str) and len(value) <= 256):
        return value
    raise ValueError


def _slot(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < MAX_PORTS:
        raise ValueError
    return value


def _exec_path(value: Any) -> str:
    if not isinstance(value, str) or not _EXEC_PATH_RE.fullmatch(value) or any(
            part in _FORBIDDEN for part in value.split(":")):
        raise ValueError
    return value


def _root(value: Any) -> str:
    if not isinstance(value, str) or not _ROOT_PATH_RE.fullmatch(value) or value in _FORBIDDEN:
        raise ValueError
    return value


def _hop(raw: Any) -> dict | None:
    """``{a: [origin, slot]}`` (an output that runs), ``{m: [member, slot,
    type]}`` (a member's output: resolve it by mode), ``{w: value}`` (a
    promoted widget's value), ``{x: 1}`` (a route the capsule can't replay) or
    None (nothing: the input keeps its widget value or is left out)."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or len(raw) != 1:
        raise ValueError
    (key, value), = raw.items()
    if key == "a" and isinstance(value, list) and len(value) == 2:
        return {"a": [_exec_path(value[0]), _slot(value[1])]}
    if key == "m" and isinstance(value, list) and len(value) == 3:
        return {"m": [_root(value[0]), _slot(value[1]), _type(value[2])]}
    if key == "w":
        return {"w": copy.deepcopy(value)}
    if key == "x" and value == 1:
        return {"x": 1}
    raise ValueError


def read_routing(raw: Any) -> tuple[dict | None, str | None]:
    """The capsule (JSON text, or its parsed object), or why it can't be used:
    ``(routing, None)``, ``(None, problem)`` with a problem from ``ERRORS``, or
    ``(None, "missing")`` for an export made before switches could be routed.
    Never raises."""
    if raw is None:
        return None, "missing"
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_ROUTING_BYTES:
            return None, "budget"
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            return None, "unsupported"
    if not isinstance(raw, dict) or raw.get("v") != ROUTING_VERSION or isinstance(raw.get("v"), bool):
        return None, "unsupported"
    if raw.get("error") is not None:
        return None, raw["error"] if raw["error"] in ERRORS else "unsupported"
    try:
        nodes_raw, edges_raw = raw.get("nodes"), raw.get("edges")
        if not isinstance(nodes_raw, dict) or len(nodes_raw) > MAX_ROUTING_NODES:
            raise ValueError
        if not isinstance(edges_raw, list) or len(edges_raw) > MAX_ROUTING_EDGES:
            raise ValueError
        nodes = {}
        for path, node in nodes_raw.items():
            if not isinstance(node, dict) or not isinstance(node.get("i"), list) or not isinstance(node.get("o"), list) \
                    or len(node["i"]) > MAX_PORTS or len(node["o"]) > MAX_PORTS:
                raise ValueError
            inputs = []
            for port in node["i"]:
                if not isinstance(port, list) or len(port) != 3 or not isinstance(port[0], str) or len(port[0]) > 256:
                    raise ValueError
                inputs.append((port[0], _type(port[1]), _hop(port[2])))
            outputs = []
            for port in node["o"]:
                if not isinstance(port, list) or len(port) not in (1, 2):
                    raise ValueError
                answers = None
                if len(port) == 2:
                    # A host's output, answered per asking type: a bypass inside
                    # the definition picks its input by that type.
                    if not isinstance(port[1], list) or len(port[1]) > MAX_PORTS:
                        raise ValueError
                    answers = []
                    for answer in port[1]:
                        if not isinstance(answer, list) or len(answer) != 2:
                            raise ValueError
                        answers.append((_type(answer[0]), _hop(answer[1])))
                outputs.append((_type(port[0]), answers, len(port) == 2))
            nodes[_root(path)] = {"inputs": inputs, "outputs": outputs}
        edges = []
        for edge in edges_raw:
            if not isinstance(edge, list) or len(edge) not in (5, 6) or not isinstance(edge[1], str) or len(edge[1]) > 256:
                raise ValueError
            member = _root(edge[2])
            if member not in nodes:
                raise ValueError
            edges.append({"consumer": _exec_path(edge[0]), "input": edge[1], "member": member, "slot": _slot(edge[3]),
                          "type": _type(edge[4]), "fallback": edge[5] if len(edge) == 6 else None,
                          "has_fallback": len(edge) == 6})
    except ValueError:
        return None, "unsupported"
    return {"nodes": nodes, "edges": edges}, None


def covers(routing: dict | None, paths) -> bool:
    """Whether the capsule can switch every one of these member paths."""
    return routing is not None and all(path in routing["nodes"] for path in paths)


# ── Compiling ────────────────────────────────────────────────────────────────

def _resolve(routing: dict, modes: dict, member: str, slot: int, slot_type: Any, seen: set):
    """``resolveOutput`` of a member's output under ``modes``: ``("a", [origin,
    slot])``, ``("w", value)`` or None."""
    key = (member, slot)
    if key in seen:
        raise RoutingError("A switch makes a loop of links.")
    seen = seen | {key}
    node = routing["nodes"].get(member)
    if node is None:
        raise RoutingError("A switched node is not in the workflow's routing.")
    if slot >= len(node["outputs"]):
        raise RoutingError("A switched node lost an output.")
    mode = modes.get(member, "enabled")
    if mode == "muted":
        return None
    output_type, answers, is_host = node["outputs"][slot]
    if mode == "bypassed":
        index = bypass_index([port[1] for port in node["inputs"]], output_type, slot, slot_type)
        if index == -1:
            return None
        if index >= len(node["inputs"]):
            raise RoutingError("A bypassed node has no input to pass on.")
        return _follow(routing, modes, node["inputs"][index][2], seen)
    if is_host:
        for asked, hop in answers:
            if asked == slot_type:
                return _follow(routing, modes, hop, seen)
        raise RoutingError(ERRORS["unsupported"])
    return ("a", [member, slot])


def _follow(routing: dict, modes: dict, hop: dict | None, seen: set):
    if hop is None:
        return None
    if "a" in hop:
        return ("a", list(hop["a"]))
    if "w" in hop:
        return ("w", copy.deepcopy(hop["w"]))
    if "x" in hop:
        raise RoutingError(ERRORS["unsupported"])
    member, slot, slot_type = hop["m"]
    return _resolve(routing, modes, member, slot, slot_type, seen)


def _wrap(value: Any) -> Any:
    # graphToPrompt boxes an array value, so it never reads as a link.
    return {"__value__": value} if isinstance(value, list) else value


_ABSENT = object()


def _dropped(path: str, off: set) -> bool:
    return path.split(":", 1)[0] in off


def compile_modes(prompt: dict, routing: dict, modes: dict) -> dict:
    """The prompt ComfyUI's serializer would make with these member modes.

    ``prompt`` is the all-enabled export (stored values already applied);
    ``modes`` maps member paths to ``muted`` or ``bypassed`` (absent: as
    exported). Returns a new prompt; raises ``RoutingError`` for a route the
    capsule can't replay, never half-compiles.
    """
    off = {path for path, mode in modes.items() if mode in OFF_MODES}
    if not off:
        return copy.deepcopy(prompt)
    missing = off - set(routing["nodes"])
    if missing:
        raise RoutingError("A switched node is not in the workflow's routing.")
    out = {key: copy.deepcopy(node) for key, node in prompt.items() if not _dropped(key, off)}
    for edge in routing["edges"]:
        consumer = out.get(edge["consumer"])
        if consumer is None or not isinstance(consumer.get("inputs"), dict):
            continue
        inputs = consumer["inputs"]
        # An edge owns a link the export resolved, or exactly what the export
        # left when the route resolved to nothing: no input, or the fallback
        # widget value. Any other literal (the session's project, a stored
        # value) is not the capsule's to rewrite, whatever an imported file says.
        current = inputs.get(edge["input"], _ABSENT)
        if not (isinstance(current, list) and len(current) == 2):
            left = edge["fallback"] if edge["has_fallback"] else _ABSENT
            if current is _ABSENT or left is _ABSENT:
                if current is not left:
                    continue
            elif current != left:
                continue
        try:
            result = _resolve(routing, modes, edge["member"], edge["slot"], edge["type"], frozenset())
        except RecursionError:
            raise RoutingError("The switches' links are nested too deeply to route.") from None
        if result is None:
            if edge["has_fallback"]:
                inputs[edge["input"]] = copy.deepcopy(edge["fallback"])
            else:
                inputs.pop(edge["input"], None)
        elif result[0] == "w":
            inputs[edge["input"]] = _wrap(result[1])
        else:
            inputs[edge["input"]] = result[1]
    # graphToPrompt's last pass: a link to a node that is not in the output.
    for node in out.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for name, value in list(inputs.items()):
            if isinstance(value, list) and len(value) == 2 and str(value[0]) not in out:
                del inputs[name]
    return out
