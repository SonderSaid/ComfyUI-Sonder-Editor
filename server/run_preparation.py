"""Execution preparation for Sonder App runs (contract v1).

A Sonder App run submits an execution copy of its compiled prompt without the
inputs that provably will not run. ComfyUI validates every connected input
before lazy nodes choose, and refuses an unknown class anywhere in the
prompt, so without this a switched-off Task still needs its files and node
packs. Launcher readiness, Generate and branch runs all judge this same copy
(``sdk_templates.run_readiness``). The canvas, fullscreen and mounted keep
ComfyUI's native validation and never call this.

The copy is for one run only. Describe, the Controls panel and every write
keep reading the authored prompt: an input absent here is not absent there.

Rules, each applied only when the deciding inputs are literals:

- ``SonderLazySwitch``: an ``int`` ``select`` whose ``branches.item<select>``
  is connected drops every other ``branches.item*`` input.
- ``SonderLazyCluster``: a ``select``, ``branches`` and ``lanes`` that pass the
  node's own state check, with every ``b<select>_l<lane>`` connected, drop
  every other ``b<i>_l<j>`` input.
- ``SonderGate``: a lane whose ``when_X`` is unwired, ``None``, ``False`` or a
  numeric zero drops ``value_X``.
- Native LoRA stacks: rows ``control_rows.row_is_noop`` calls off are dropped
  and the rest packed to 0..n-1; a stack with every row off and consumers is
  bypassed: its consumers take its ``model``/``clip`` link and it stays in the
  copy with nothing consuming it, so ComfyUI never validates its rows and
  Controls at run can still describe it.

Literals are read through core Primitives and through ``SonderSelector``
(``selection`` and ``index``). Anything else is unresolved and its node keeps
every input: arbitrary-node conditions, Driver and Reference selectors,
third-party switches. ``volatile`` inputs (the seed targets each take rewrites
after preparation) are unresolved too.

Then only what reaches a root is kept. Roots are every known output class, the
Relay, and every node nothing consumes unless it is an installed non-output
class (ComfyUI never runs one; a switched-off Mode's chain often ends in one).
An unknown node nothing consumes stays a root: it might be an output once
installed. A branch run's roots are its targets and the Relay. A known output
is never dropped. An unknown-class node reached only through a cut input is
dropped and listed in ``removed``: it could only have run through that input.

Any doubt (a root or output gone, a link left to a dropped node, an
exception) returns the compiled prompt unchanged with ``fallback`` set: the
run is then judged as it was before preparation existed.

Pure: imports only the standard library and other pure server modules.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any, Callable, Iterable

from . import control_rows, prompt_graph
from .lora_metadata import NATIVE_LORA_STACKS

PREPARATION_VERSION = 1
SWITCH = "SonderLazySwitch"
CLUSTER = "SonderLazyCluster"
GATE = "SonderGate"
SELECTOR = "SonderSelector"
RELAY = "SonderControlRelay"
STACK_PASSTHROUGH = {"LoadLoraModel": "model", "LoadLoraTextEncoder": "clip"}
_SWITCH_KEY = re.compile(r"^branches\.item(0|[1-9][0-9]*)$")
_CLUSTER_KEY = re.compile(r"^b(0|[1-9][0-9]*)_l(0|[1-9][0-9]*)$")
_ROW_KEY = re.compile(r"^loras\.(0|[1-9][0-9]*)\.([^.]+)$")
_LINK_DEPTH = 8
logger = logging.getLogger(__name__)

UNRESOLVED = prompt_graph.UNRESOLVED


def parse_choice_list(choice_list: Any) -> list[str]:
    """``nodes/selector.py::_parse_choice_list``, mirrored (parity-tested):
    ``server`` never imports ``nodes``."""
    return [line.strip() for line in str(choice_list or "").splitlines() if line.strip()]


def _link(value: Any) -> tuple[str, Any] | None:
    node_id = prompt_graph.linked_node_id(value)
    if node_id is None:
        return None
    return node_id, value[1] if len(value) > 1 else 0


def _resolve(prompt: dict, node_id: str, name: str, volatile: set, depth: int = 0) -> Any:
    """The literal ``node_id``'s input ``name`` carries at execution, or UNRESOLVED."""
    if depth > _LINK_DEPTH or (node_id, name) in volatile:
        return UNRESOLVED
    inputs = prompt_graph.node_inputs(prompt, node_id)
    if name not in inputs:
        return UNRESOLVED
    value = inputs[name]
    link = _link(value)
    if link is None:
        return value
    source, slot = link
    node = prompt.get(source)
    if not isinstance(node, dict):
        return UNRESOLVED
    class_type = node.get("class_type")
    if class_type in prompt_graph.PRIMITIVE_CLASS_TYPES:
        return _primitive_value(class_type, _resolve(prompt, source, "value", volatile, depth + 1))
    if class_type == SELECTOR and slot in (0, 1):
        choice = _resolve(prompt, source, "choice", volatile, depth + 1)
        choices = _resolve(prompt, source, "choice_list", volatile, depth + 1)
        if not isinstance(choice, str) or not isinstance(choices, str):
            return UNRESOLVED
        listed = parse_choice_list(choices)
        selected = choice.strip()
        if not listed or len(set(listed)) != len(listed) or selected not in listed:
            return UNRESOLVED  # the Selector refuses at run time; cut nothing
        return listed.index(selected) if slot == 1 else selected
    return UNRESOLVED


# ComfyUI converts a Primitive's ``value`` by its input type before execution
# (``execution.validate_inputs``), so a PrimitiveString's False runs as "False".
_PRIMITIVE_CONVERSIONS = {"PrimitiveBoolean": bool, "PrimitiveInt": int, "PrimitiveFloat": float,
                          "PrimitiveString": str, "PrimitiveStringMultiline": str}


def _primitive_value(class_type: str, value: Any) -> Any:
    if value is UNRESOLVED:
        return UNRESOLVED
    convert = _PRIMITIVE_CONVERSIONS.get(class_type)
    try:
        return convert(value) if convert else UNRESOLVED
    except (TypeError, ValueError, OverflowError):
        return UNRESOLVED


def _int_literal(value: Any) -> int | None:
    if isinstance(value, bool) or value is UNRESOLVED:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _cut_switch(prompt: dict, key: str, volatile: set) -> list[str]:
    inputs = prompt[key]["inputs"]
    selected = _int_literal(_resolve(prompt, key, "select", volatile))
    if selected is None or selected < 0 or f"branches.item{selected}" not in inputs:
        return []
    cut = [name for name in inputs if _SWITCH_KEY.match(name) and name != f"branches.item{selected}"]
    for name in cut:
        del inputs[name]
    return cut


def _cut_cluster(prompt: dict, key: str, volatile: set) -> list[str]:
    inputs = prompt[key]["inputs"]
    selected, branches, lanes = (_int_literal(_resolve(prompt, key, name, volatile))
                                 for name in ("select", "branches", "lanes"))
    # The node's own state check (``_validate_cluster_state``), without its
    # configurable caps: a value past them fails native validation regardless.
    if None in (selected, branches, lanes) or branches < 2 or lanes < 1 or not 0 <= selected < branches:
        return []
    if any(f"b{selected}_l{lane}" not in inputs for lane in range(lanes)):
        return []
    cut = [name for name in inputs if (match := _CLUSTER_KEY.match(name)) and int(match.group(1)) != selected]
    for name in cut:
        del inputs[name]
    return cut


def _gate_closed(value: Any) -> bool:
    """``lazy_switches._gate_open`` for literals: unwired, None, False or a numeric zero close."""
    if value is None:
        return True
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value == 0
    return False


def _cut_gate(prompt: dict, key: str, volatile: set) -> list[str]:
    inputs = prompt[key]["inputs"]
    cut = []
    for name in list(inputs):
        if not name.startswith("value_"):
            continue
        when = "when_" + name[len("value_"):]
        if when in inputs:
            condition = _resolve(prompt, key, when, volatile)
            if condition is UNRESOLVED or not _gate_closed(condition):
                continue
        elif (key, when) in volatile:
            continue
        del inputs[name]
        cut.append(name)
    return cut


def _stack_rows(inputs: dict) -> dict[int, dict]:
    rows: dict[int, dict] = {}
    for name, value in inputs.items():
        match = _ROW_KEY.match(name) if isinstance(name, str) else None
        if match:
            rows.setdefault(int(match.group(1)), {})[match.group(2)] = value
    return rows


def _consumers(prompt: dict) -> dict[str, list[tuple[str, str]]]:
    found: dict[str, list[tuple[str, str]]] = {}
    for key, node in prompt.items():
        for name, value in prompt_graph.node_inputs(prompt, key).items():
            link = _link(value)
            if link is not None:
                found.setdefault(link[0], []).append((str(key), name))
    return found


def _volatile_set(volatile: Iterable) -> set:
    """``(path, input)`` pairs as strings. ``_resolve`` checks every input it
    reads, a Primitive's ``value`` included, so a seeded Primitive feeding a
    ``select`` is unresolved too."""
    return {(str(path), str(name)) for path, name in volatile or ()}


def _outputs(prompt: dict, class_lookup: Callable[[str], Any] | None) -> set[str]:
    found = set()
    if class_lookup is None:
        return found
    for key, node in prompt.items():
        try:
            if getattr(class_lookup(node.get("class_type", "")), "OUTPUT_NODE", False) is True:
                found.add(str(key))
        except Exception:  # noqa: BLE001 - a broken class is judged by validation, not here
            continue
    return found


def prepare(prompt: dict, *, class_lookup: Callable[[str], Any] | None = None,
            node_lookup: Callable[[str], Any] | None = None, targets: Iterable | None = None,
            volatile: Iterable = ()) -> dict:
    """The execution copy of a compiled prompt and what preparation changed.

    Returns ``{"version", "prompt", "removed", "cut", "packed_rows", "fallback"}``.
    ``prompt`` is never mutated. ``class_lookup`` names known output classes;
    ``node_lookup`` decides presence (unknown classes). ``targets`` makes a
    branch run's roots. ``packed_rows`` maps a stack still in the run (or
    bypassed for a consumer still in it, and kept unconsumed) to ``{"rows", "bypassed"}``: its
    authored rows, for provenance.
    """
    result = {"version": PREPARATION_VERSION, "prompt": prompt, "removed": [], "cut": {},
              "packed_rows": {}, "fallback": None}
    if not isinstance(prompt, dict) or not prompt:
        return result
    try:
        return _prepare(prompt, class_lookup, node_lookup or class_lookup, targets, volatile, result)
    except Exception as exc:  # noqa: BLE001 - preparation must never block a run it can't judge
        logger.warning("Sonder App run preparation fell back to the compiled prompt", exc_info=True)
        return {**result, "prompt": prompt, "removed": [], "cut": {}, "packed_rows": {},
                "fallback": f"error: {type(exc).__name__}"}


def _fallback(result: dict, prompt: dict, reason: str) -> dict:
    logger.warning("Sonder App run preparation fell back to the compiled prompt: %s", reason)
    return {**result, "prompt": prompt, "removed": [], "cut": {}, "packed_rows": {}, "fallback": reason}


def _prepare(original: dict, class_lookup, node_lookup, targets, volatile, result: dict) -> dict:
    prompt = copy.deepcopy(original)
    volatile = _volatile_set(volatile)
    consumers_before = _consumers(prompt)
    outputs = _outputs(prompt, class_lookup)
    relays = {str(k) for k, n in prompt.items() if isinstance(n, dict) and n.get("class_type") == RELAY}
    editors = set(prompt_graph.editor_keys(prompt))
    if targets is not None:
        # The Editor carries the run's token (``_meta.sonder_run``): a branch
        # whose output doesn't read it still sends it. ComfyUI checks only its
        # class outside the targets' closure.
        roots = {str(t) for t in targets} | relays | editors
    else:
        # A leaf is a root unless it is an installed class that is not an
        # output: ComfyUI never runs such a node (it runs outputs and what they
        # need), so it only drags in what it reads. An unknown leaf might be an
        # output once installed, so it stays and still refuses, as today.
        leaves = {str(k) for k in prompt if str(k) not in consumers_before}
        roots = outputs | relays | {k for k in leaves if not _inert(prompt[k], class_lookup, node_lookup)}

    cut: dict[str, list[str]] = {}
    for key, node in list(prompt.items()):
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        class_type = node.get("class_type")
        removed_inputs = (_cut_switch(prompt, key, volatile) if class_type == SWITCH
                          else _cut_cluster(prompt, key, volatile) if class_type == CLUSTER
                          else _cut_gate(prompt, key, volatile) if class_type == GATE else [])
        if removed_inputs:
            cut[str(key)] = sorted(removed_inputs)

    packed: dict[str, dict] = {}
    bypassed: dict[str, Any] = {}
    for key, node in list(prompt.items()):
        class_type = node.get("class_type") if isinstance(node, dict) else None
        if class_type not in NATIVE_LORA_STACKS:
            continue
        inputs = node["inputs"]
        rows = _stack_rows(inputs)
        if not rows:
            continue
        authored = [rows[index] for index in sorted(rows)]
        kept = [row for row in authored if not control_rows.row_is_noop(row)]
        if len(kept) == len(authored):
            continue
        if not kept:
            passthrough = inputs.get(STACK_PASSTHROUGH.get(class_type, ""))
            if _link(passthrough) is None or str(key) not in consumers_before or str(key) in roots:
                continue  # no link to hand on, or nothing consumes it: leave it to validation
            bypassed[str(key)] = list(passthrough)
            packed[str(key)] = {"rows": copy.deepcopy(authored), "bypassed": True}
            continue
        control_rows.apply_rows(inputs, "loras", kept)
        packed[str(key)] = {"rows": copy.deepcopy(authored), "bypassed": False}

    if bypassed:
        for key, node in prompt.items():
            if key in bypassed:
                continue  # kept unconnected: provenance still describes it
            for name, value in list(node.get("inputs", {}).items()):
                link = _link(value)
                seen = set()
                while link is not None and link[0] in bypassed and link[0] not in seen:
                    seen.add(link[0])
                    value = bypassed[link[0]]
                    link = _link(value)
                if link is not None and link[0] in bypassed:
                    return _fallback(result, original, "bypass cycle")
                node["inputs"][name] = value

    live = prompt_graph.upstream_set(prompt, roots)

    def feeds_the_run(key: str, seen: set) -> bool:
        # A bypassed stack counts when a consumer runs, directly or through
        # another bypassed stack.
        if key in live:
            return True
        if key not in bypassed or key in seen:
            return False
        seen.add(key)
        return any(feeds_the_run(consumer, seen) for consumer, _ in consumers_before.get(key, ()))

    # A bypassed stack the run used stays as a node nothing consumes. ComfyUI
    # validates only what its outputs need (and every class's presence), so its
    # rows are never checked, and Controls at run can still describe it.
    kept_bypassed = {key for key in bypassed if feeds_the_run(key, set())}
    live |= prompt_graph.upstream_set(prompt, kept_bypassed)
    removed = sorted(set(str(k) for k in prompt) - live)
    prepared = {key: node for key, node in prompt.items() if str(key) in live}

    # Coverage: every root and output kept, the Editor kept, no dangling link.
    missing_roots = sorted((roots & set(map(str, original))) - set(map(str, prepared)))
    if missing_roots:
        return _fallback(result, original, f"root dropped: {', '.join(missing_roots[:5])}")
    if not outputs <= set(map(str, prepared)) and targets is None:
        return _fallback(result, original, "output dropped")
    if not editors <= set(map(str, prepared)):
        return _fallback(result, original, "editor dropped")
    for key, node in prepared.items():
        for value in node.get("inputs", {}).values():
            link = _link(value)
            if link is not None and link[0] not in prepared and link[0] in original:
                return _fallback(result, original, f"dangling link from {key}")

    unknown = [key for key in removed if node_lookup is not None and _absent(node_lookup, original[key])]
    if unknown:
        logger.info("Sonder App run preparation dropped uninstalled nodes outside what runs: %s",
                    ", ".join(f"{k} ({original[k].get('class_type')})" for k in unknown[:20]))
    kept_packed = {key: entry for key, entry in packed.items() if key in prepared}
    return {**result, "prompt": prepared, "removed": removed,
            "cut": {k: v for k, v in cut.items() if k in prepared}, "packed_rows": kept_packed}


def _inert(node: Any, class_lookup, node_lookup) -> bool:
    """An installed class that is not an output node: ComfyUI never runs it
    unless an output needs it. Unknown answers are never inert."""
    if class_lookup is None or node_lookup is None or _absent(node_lookup, node):
        return False
    try:
        return getattr(class_lookup(node.get("class_type", "")), "OUTPUT_NODE", False) is not True
    except Exception:  # noqa: BLE001
        return False


def _absent(node_lookup, node: Any) -> bool:
    try:
        return node_lookup(node.get("class_type", "")) is None
    except Exception:  # noqa: BLE001
        return True
