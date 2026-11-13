"""Execution cache pipeline: prepare a prompt's Sonder values before ComfyUI builds cache keys.

The Editor's ``IS_CHANGED`` is NaN because it does per-run work (SDK token, queue
claim and lease, execution context, Output Views record). ComfyUI folds every
ancestor's signature into a node's cache key, so everything downstream of the
Editor reruns on every prompt. This module restores native caching for the
Editor's text and number outputs without touching that guard:

1. ``executor_hook`` calls ``prepare`` once per prompt, after ComfyUI reset its
   interrupt flag and registered the progress handler, before ``set_prompt``.
2. Cheap eligibility checks, then the certain-to-execute closure: the Editor runs
   early only when stock ComfyUI would certainly run it.
3. The real ``SonderEditor.execute`` runs through its existing memo
   (``prompt_once``), under the context the scheduled call will have, so token
   check, claim, lease and render happen exactly once. The scheduled Editor, if
   it is still scheduled, returns the memoized result.
4. **Bridges** that declare ``PREPARES`` (``nodes/cache_policy.py``) and are
   certain to run, take only the Editor's ``project`` or a prepared handle, and
   have a consumer to gain, run their own ``execute`` here, exactly as the
   executor would call it. That one function publishes their provenance, so a
   bridge whose consumers all take literals and is no longer scheduled still
   leaves its provenance on the take. The bridge pass is isolated: a fault in it
   drops only bridge substitutions, never the Editor's.
5. A pure planner (``plan_substitutions``) replaces each consumer's link to an
   ``EXACT`` output of the Editor or a prepared bridge with the literal value
   (M1), when the consumer's declared input type is exactly the output's literal
   type.
6. Overrides go through ``DynamicPrompt.override_node``, which feeds key
   construction and input resolution while hidden ``PROMPT``, history and display
   ids keep the submitted graph.

Fail-open: anything this module cannot prove leaves the prompt exactly as
submitted. **A miss is acceptable; a false hit never is.** Only an exception
raised from inside ``SonderEditor.execute`` is stored for replay; a failure in
this module's own code stores nothing and substitutes nothing, so the Editor
runs on the stock path.

No second queue, resolver, memo or project store: the prepared result *is* the
``run_once_per_prompt`` memo. Policies come from ``nodes/cache_policy.py``.
"""

from __future__ import annotations

import copy
import inspect
import logging
import math
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from . import run_preparation, run_scope, sdk_runs
from ..nodes import cache_policy, prompt_once

logger = logging.getLogger(__name__)

EDITOR_CLASS_TYPE = "SonderEditor"
EDITOR_HIDDEN = frozenset({"PROMPT", "UNIQUE_ID"})

PREPARED = "prepared"
SKIPPED = "skipped"
EDITOR_FAILED = "editor_failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"
PIPELINE_ERROR = "pipeline_error"

# Reason code -> what a user can act on, logged once per process at INFO.
REASON_TEXT = {
    "editor_count": "the workflow holds more than one Sonder Editor",
    "is_changed": "a node in the prompt already carries an is_changed value",
    "linked_editor_input": "an input of the Sonder Editor is wired to another node",
    "untrusted": "the prompt did not come through ComfyUI's prompt queue",
    "roots_unknown": "ComfyUI did not say which outputs this prompt runs",
    "scope_unknown": "the workflow holds a node that edits the graph while it runs",
    "not_certain": "the Sonder Editor only runs on a branch chosen at run time",
    "editor_hidden": "the Sonder Editor declares hidden inputs this pipeline cannot supply",
    "editor_undeclared": "another node is registered under the Sonder Editor's id",
    "missing_inputs": "the Sonder Editor's inputs could not be resolved before the run",
    "prompt_identity": "the Sonder Editor's prompt could not be matched to its run",
    "call_key": "the Sonder Editor's run could not be identified",
    "result_shape": "the Sonder Editor returned an unexpected result",
}
_QUIET_REASONS = frozenset({"no_editor"})
_LOGGED_REASONS: set[str] = set()
_LOGGED_LOCK = threading.Lock()


@dataclass(frozen=True)
class ExecutorRuntime:
    """The executor names the pipeline calls, all checked by ``executor_hook``'s shape gate."""

    get_input_data: Callable[..., Any]
    node_context: Callable[[str, str, int], Any]
    progress_state: Callable[[], Any]
    server: Callable[[], Any]
    before_node_execution: Callable[[], None]
    class_mappings: Callable[[], dict]
    input_info: Callable[[Any, str, str], Any]
    interrupt_type: type
    rearm_interrupt: Callable[[], None]
    inference_mode: Callable[[], Any]
    # Bridge preparation (Phase 2). None when the gate cannot vouch for the
    # executor's V3 call sequence: V3 bridges are then never prepared.
    is_v3: Callable[[type], bool] | None = None
    call_v3: Callable[[type, dict, Any], Any] | None = None
    own_lazy_status: Callable[[type], bool] | None = None
    node_output_type: type | None = None
    # Media identity (Phase 3): True only when the gate vouched that a bridge can
    # find its run through the executing context (executor_hook.check_identity_context).
    identity: bool = False


@dataclass(frozen=True)
class Producer:
    """A prepared node's result and how to read its slots, for the planner."""

    result: tuple
    slot_policy: Callable[[int], Any]
    slot_type: Callable[[int], str]


def producer_for(class_def: Any, result: tuple) -> Producer:
    return Producer(result, lambda slot: cache_policy.slot_policy(class_def, slot),
                    lambda slot: class_def.RETURN_TYPES[slot])


class _Skip(Exception):
    """Fail open with a reason code; never escapes ``prepare``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def is_link(value: Any) -> bool:
    """ComfyUI's link shape: ``[node_id: str, output_index: int | float]``."""
    return (isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)
            and isinstance(value[1], (int, float)) and not isinstance(value[1], bool))


def _slot_index(value: Any) -> int | None:
    slot = value[1]
    if isinstance(slot, float):
        if not slot.is_integer():
            return None
        slot = int(slot)
    return slot if slot >= 0 else None


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _note_reason(reason: str) -> None:
    if reason in _QUIET_REASONS:
        return
    with _LOGGED_LOCK:
        if reason in _LOGGED_REASONS:
            return
        _LOGGED_REASONS.add(reason)
    logger.info("Sonder cache pipeline: nodes after the Sonder Editor rerun on every run while %s (%s).",
                REASON_TEXT.get(reason, reason), reason)


# --- Eligibility and the certain-to-execute closure ---------------------------------


def _cheap_exits(prompt: dict) -> tuple[str, str, tuple[str, ...]]:
    """``(editor_id, key, roots)``; raises ``_Skip`` in cheapest-first order."""
    editors = [str(node_id) for node_id, node in prompt.items()
               if isinstance(node, dict) and node.get("class_type") == EDITOR_CLASS_TYPE]
    if not editors:
        raise _Skip("no_editor")
    if len(editors) != 1:
        raise _Skip("editor_count")
    if any(isinstance(node, dict) and "is_changed" in node for node in prompt.values()):
        raise _Skip("is_changed")
    editor_id = editors[0]
    inputs = prompt[editor_id].get("inputs") or {}
    if any(is_link(value) for value in inputs.values()):
        raise _Skip("linked_editor_input")
    key = sdk_runs.trusted_key_for_prompt(prompt)
    if not key:
        raise _Skip("untrusted")
    roots = sdk_runs.release_roots(key)
    if not roots:
        raise _Skip("roots_unknown")
    return editor_id, key, roots


def certain_closure(roots, get_node: Callable[[str], dict], is_lazy: Callable[[str, str], bool],
                    requested: Callable[[str], Any] | None = None) -> set[str]:
    """Nodes reached from ``roots`` over non-lazy links: ComfyUI's ``add_node`` rule.

    Under the Editor's NaN every node between a root and the Editor is uncached,
    so membership here equals "stock ComfyUI would run the Editor". A lazy input
    is followed only when ``requested(node_id)`` names it: a Sonder router whose
    own rule, given values it will certainly receive, asks for that input on the
    scheduler's first lazy check (``router_requests``).
    """
    seen: set[str] = set()
    stack = [str(root) for root in roots]
    while stack:
        node_id = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        follow = frozenset(requested(node_id) or ()) if requested is not None else frozenset()
        for name, value in (get_node(node_id).get("inputs") or {}).items():
            if is_link(value) and (name in follow or not is_lazy(node_id, name)):
                stack.append(str(value[0]))
    return seen


def router_requests(class_def: Any, inputs: dict, resolve: Callable[[str], Any],
                    is_lazy_input: Callable[[str], bool]) -> list[str]:
    """The lazy inputs a Sonder router will certainly request on its first lazy check, or ``[]``.

    Each ``deciding`` input must be known: a literal in ``inputs`` (the view after
    the M1 plan, so an Editor or bridge value the planner substituted into this
    router counts), or a link ``resolve`` proves (``run_preparation``'s static
    resolver: Primitives and ``SonderSelector``). An absent input is passed as
    absent, as the scheduler does. ``connected`` is every linked lazy input:
    under the Editor's NaN, a branch that reaches the Editor or a bridge is
    unevaluated at that check. Anything unknown, or a raise from the rule,
    follows nothing (today's behaviour). Only names in ``connected`` are returned.
    """
    declared = cache_policy.lazy_policy(class_def)
    if declared is None:
        return []
    method = getattr(class_def, declared.method, None)
    if not callable(method):
        return []
    deciding: dict[str, Any] = {}
    for name in declared.deciding:
        if name not in inputs:
            continue
        value = inputs[name]
        if is_link(value):
            value = resolve(name)
            if value is run_preparation.UNRESOLVED:
                return []
        deciding[name] = value
    connected = frozenset(name for name, value in inputs.items() if is_link(value) and is_lazy_input(name))
    try:
        names = method(deciding, connected)
    except Exception:
        return []
    return [name for name in (names or ()) if isinstance(name, str) and name in connected]


def _hidden_names(class_def: Any) -> set[str]:
    hidden = (class_def.INPUT_TYPES() or {}).get("hidden") or {}
    return {run_scope._hidden_name(value) for value in hidden.values()}


# --- The pure M1 planner ---------------------------------------------------------


def consumer_rejects(input_type_of: Callable[[str, str], Any], node_id: str, name: str, socket: Any) -> str | None:
    """Why input ``name`` of ``node_id`` cannot take a literal of socket type ``socket``, or None."""
    if cache_policy.LITERAL_TYPES.get(socket) is None:
        return "socket_type"
    try:
        info = input_type_of(node_id, name)
    except Exception:
        info = None
    if not info:
        return "consumer_unknown"
    consumer_type, extra = info
    if isinstance(extra, dict) and extra.get("rawLink"):
        return "raw_link"
    # Base str equality: ComfyUI's IO.ANY ("*") overrides != to match any type,
    # which would hand a wildcard consumer a literal.
    if not isinstance(consumer_type, str) or str.__eq__(consumer_type, socket) is not True:
        return "consumer_type"
    return None


def plan_substitutions(nodes_view: dict[str, dict], producers: dict[str, Producer],
                       input_type_of: Callable[[str, str], Any]) -> tuple[dict[str, dict], list[dict]]:
    """``(overrides, skips)``: literal replacements for links to ``EXACT`` outputs of ``producers``.

    ``producers`` maps a prepared node (the Editor, prepared bridges) to its
    result. A link is replaced only when the consumer declares the input's type
    exactly as the slot's literal type (``STRING``/``INT``/``FLOAT``/``BOOLEAN``),
    the value's ``type`` is exactly that Python type, and a float is finite.
    ``rawLink`` inputs, wildcard, COMBO and unknown consumers are skipped;
    ``HANDLE``, ``PER_RUN`` and ``INHERITS`` slots are never substituted. All
    substitutions for one node, from any producer, merge into one deep copy of its
    current node. Pure: reads only its arguments and raises only on a broken argument.
    """
    overrides: dict[str, dict] = {}
    skips: list[dict] = []
    for node_id, node in nodes_view.items():
        if node_id in producers or not isinstance(node, dict):
            continue
        replacement = None
        for name, value in (node.get("inputs") or {}).items():
            if not is_link(value):
                continue
            producer_id = str(value[0])
            producer = producers.get(producer_id)
            if producer is None:
                continue
            slot = _slot_index(value)

            def skip(reason: str) -> None:
                skips.append({"node": node_id, "input": name, "producer": producer_id, "slot": slot,
                              "reason": reason})

            if slot is None or slot >= len(producer.result):
                skip("slot")
                continue
            if producer.slot_policy(slot) != cache_policy.EXACT:
                continue  # declared otherwise: nothing to substitute, nothing skipped
            socket = producer.slot_type(slot)
            expected = cache_policy.LITERAL_TYPES.get(socket)
            reason = consumer_rejects(input_type_of, node_id, name, socket)
            if reason is not None:
                skip(reason)
                continue
            emitted = producer.result[slot]
            if type(emitted) is not expected:
                skip("value_type")
                continue
            if expected is float and not math.isfinite(emitted):
                skip("non_finite")
                continue
            if replacement is None:
                replacement = copy.deepcopy(node)
            replacement["inputs"][name] = emitted
        if replacement is not None:
            overrides[node_id] = replacement
    return overrides, skips


# --- Bridges: which to prepare, and running their own execute ----------------------

PREPARED_HIDDEN = frozenset({"PROMPT", "UNIQUE_ID"})


class _BridgeSkip(Exception):
    """One bridge is not prepared, for a reason code; the rest of the pass goes on."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def shape_reason(runtime: ExecutorRuntime, class_def: Any) -> str | None:
    """Why the pipeline cannot call ``class_def`` as the executor would, or None.

    Sync ``execute``; no list mapping in or out; no lazy input and no own
    ``check_lazy_status`` (every V3 node inherits one, so only an override
    counts); hidden inputs only ``PROMPT``/``UNIQUE_ID`` (``get_input_data`` runs
    with ``extra_data={}``); a V3 class only when the gate vouched for the
    executor's V3 call sequence.
    """
    if runtime.is_v3 is None:
        return "v3_unknown"  # cannot tell V3 from V1: never guess the call
    v3 = bool(runtime.is_v3(class_def))
    if v3 and (runtime.call_v3 is None or runtime.own_lazy_status is None or runtime.node_output_type is None):
        return "v3_call"
    if getattr(class_def, "INPUT_IS_LIST", False):
        return "input_is_list"
    if any(bool(flag) for flag in (getattr(class_def, "OUTPUT_IS_LIST", None) or ())):
        return "output_is_list"
    method = getattr(class_def, "execute" if v3 else str(getattr(class_def, "FUNCTION", "") or ""), None)
    if not callable(method) or inspect.iscoroutinefunction(method):
        return "callable"
    if runtime.own_lazy_status(class_def) if v3 else hasattr(class_def, "check_lazy_status"):
        return "lazy_status"
    types_ = class_def.INPUT_TYPES() or {}
    for category in ("required", "optional"):
        for spec in (types_.get(category) or {}).values():
            if isinstance(spec, (tuple, list)) and len(spec) > 1 and isinstance(spec[1], dict) and spec[1].get("lazy"):
                return "lazy_input"
    hidden = {run_scope._hidden_name(value) for value in (types_.get("hidden") or {}).values()}
    if not hidden <= PREPARED_HIDDEN:
        return "hidden"
    return None


def _has_literal_consumer(nodes_view: dict[str, dict], producer_id: str, class_def: Any,
                          input_type_of: Callable[[str, str], Any]) -> bool:
    """Whether some consumer would take one of the producer's ``EXACT`` outputs (a dry run of the planner)."""
    for node_id, node in nodes_view.items():
        if node_id == producer_id or not isinstance(node, dict):
            continue
        for name, value in (node.get("inputs") or {}).items():
            if not is_link(value) or str(value[0]) != producer_id:
                continue
            slot = _slot_index(value)
            if (slot is None or slot >= len(class_def.RETURN_TYPES)
                    or cache_policy.slot_policy(class_def, slot) != cache_policy.EXACT):
                continue
            if consumer_rejects(input_type_of, node_id, name, class_def.RETURN_TYPES[slot]) is None:
                return True
    return False


def select_bridges(nodes_view: dict[str, dict], closure: set[str], editor_id: str, editor_class: Any,
                   class_of: Callable[[str], Any], input_type_of: Callable[[str, str], Any],
                   shape_of: Callable[[Any], str | None],
                   feeds_identity: Callable[[str], bool] | None = None,
                   ) -> tuple[list[str], dict[str, set[str]], dict[str, str]]:
    """``(order, needs, reasons)``: the bridges to prepare, producers first. Pure.

    A node is selected when its class declares ``PREPARES``, it is in the
    certain-to-execute closure (under the Editor's NaN, "stock would run it"),
    its shape is callable, every link input is the Editor's ``PER_RUN`` project or
    a ``HANDLE`` of another selected node, and it has a consumer to gain (a
    literal consumer of an ``EXACT`` output, a handle feeding a node that has
    one, or a handle feeding a node the identity pass may identify:
    ``feeds_identity(node_id)``). ``reasons`` names why each other ``PREPARES``
    node was left alone.
    """
    reasons: dict[str, str] = {}
    candidates: dict[str, Any] = {}
    for node_id, node in nodes_view.items():
        if node_id == editor_id or not isinstance(node, dict):
            continue
        class_def = class_of(node.get("class_type"))
        if class_def is not None and cache_policy.prepare_policy(class_def) is not None:
            candidates[node_id] = class_def
    needs: dict[str, set[str]] = {}
    editor_types = tuple(editor_class.RETURN_TYPES)
    for node_id, class_def in candidates.items():
        if node_id not in closure:
            reasons[node_id] = "not_certain"
            continue
        if shape_of(class_def) is not None:
            reasons[node_id] = "shape"
            continue
        wanted: set[str] = set()
        for value in (nodes_view[node_id].get("inputs") or {}).values():
            if not is_link(value):
                continue
            source, slot = str(value[0]), _slot_index(value)
            if (source == editor_id and slot is not None and slot < len(editor_types)
                    and editor_types[slot] == "SONDER_PROJECT"
                    and cache_policy.slot_policy(editor_class, slot) == cache_policy.PER_RUN):
                continue
            if (source in candidates and source != node_id and slot is not None
                    and cache_policy.slot_policy(candidates[source], slot) == cache_policy.HANDLE):
                wanted.add(source)
                continue
            reasons[node_id] = "linked_input"
            break
        else:
            needs[node_id] = wanted
    # A producer that is not selectable takes its dependents with it.
    changed = True
    while changed:
        changed = False
        for node_id in list(needs):
            if any(source not in needs for source in needs[node_id]):
                del needs[node_id]
                reasons[node_id] = "producer_failed"
                changed = True
    useful = {node_id for node_id in needs
              if _has_literal_consumer(nodes_view, node_id, candidates[node_id], input_type_of)
              or (feeds_identity is not None and feeds_identity(node_id))}
    changed = True
    while changed:
        changed = False
        for node_id in list(useful):
            for source in needs[node_id] - useful:
                useful.add(source)
                changed = True
    for node_id in needs:
        if node_id not in useful:
            reasons[node_id] = "unused"
    order: list[str] = []
    placed: set[str] = set()

    def place(node_id: str, visiting: frozenset) -> bool:
        if node_id in placed:
            return True
        if node_id in visiting:
            return False  # a cycle cannot pass ComfyUI's validation; refuse it anyway
        if not all(place(source, visiting | {node_id}) for source in sorted(needs[node_id])):
            return False
        placed.add(node_id)
        order.append(node_id)
        return True

    for node_id in sorted(useful):
        if not place(node_id, frozenset()):
            reasons[node_id] = "linked_input"
    return [node_id for node_id in order if node_id not in reasons], needs, reasons


def _bridge_values(runtime: ExecutorRuntime, class_def: Any, out: Any) -> tuple:
    if runtime.node_output_type is not None and isinstance(out, runtime.node_output_type):
        # A value result only: an expansion, a block or a UI output would be lost
        # (or wrongly kept) when the bridge goes unscheduled.
        if any(getattr(out, name, None) is not None for name in ("expand", "block_execution", "ui")):
            raise _BridgeSkip("result_shape")
        out = tuple(out.args)
    if isinstance(out, dict):
        raise _BridgeSkip("result_shape")  # a V1 {"ui", "result"} return
    if not isinstance(out, tuple) or len(out) != len(class_def.RETURN_TYPES):
        raise _BridgeSkip("result_shape")
    return out


def _run_bridge(runtime: ExecutorRuntime, prompt_id: str, dynprompt: Any, node_id: str, class_def: Any,
                sources: dict[str, tuple]) -> tuple:
    """Call the bridge's own ``execute`` as the executor would; its values, or ``_BridgeSkip``.

    Pipeline code here (input resolution, the context, the shape of the result)
    propagates its faults to the bridge pass. Only a raise from the bridge itself
    becomes ``bridge_failed``; nothing is stored, because the scheduled bridge
    raises the same error with the same inputs, or never runs at all.
    """
    inputs = dynprompt.get_node(node_id).get("inputs") or {}
    resolved, missing, v3_data = runtime.get_input_data(inputs, class_def, node_id, None, dynprompt, {})
    args: dict[str, Any] = {}
    for name, values in resolved.items():
        if name in missing:
            link = inputs.get(name)
            source = sources.get(str(link[0])) if is_link(link) else None
            slot = _slot_index(link) if is_link(link) else None
            if source is None or slot is None or slot >= len(source):
                raise _BridgeSkip("linked_input")
            args[name] = source[slot]
            continue
        if not isinstance(values, (list, tuple)) or len(values) != 1:
            raise _BridgeSkip("shape")
        args[name] = values[0]
    if any(name not in args for name in missing):
        raise _BridgeSkip("linked_input")
    v3 = bool(runtime.is_v3 is not None and runtime.is_v3(class_def))
    with runtime.node_context(prompt_id, node_id, 0):
        runtime.before_node_execution()  # stock's cancel check; its interrupt propagates
        with runtime.inference_mode():
            try:
                if v3:
                    out = runtime.call_v3(class_def, args, v3_data)
                else:
                    out = getattr(class_def(), class_def.FUNCTION)(**args)
            except runtime.interrupt_type:
                raise
            except Exception as exc:
                logger.debug("Sonder cache pipeline: bridge %s (%s) failed before cache keys: %s",
                             node_id, getattr(class_def, "__name__", class_def), type(exc).__name__)
                raise _BridgeSkip("bridge_failed") from None
    return _bridge_values(runtime, class_def, out)


def _bridge_rounds(runtime: ExecutorRuntime, prompt_id: str, dynprompt: Any, nodes_view: dict[str, dict],
                   roots, editor_id: str, editor_class: Any, editor_result: tuple,
                   mappings: dict, input_type_of: Callable[[str, str], Any], is_lazy: Callable[[str, str], bool],
                   requested_by: Callable[[dict], Callable[[str], list[str]]], editor_overrides: dict[str, dict],
                   record: dict[str, Any], feeds_identity: Callable[[str], bool] | None = None,
                   ) -> tuple[dict[str, Producer], set[str]]:
    """Prepare bridges in rounds; ``(producers, final closure)``.

    Each round recomputes stock's closure with every Sonder router decided by the
    values substituted so far (the Editor's, then each round's bridges'), so a
    bridge behind a router whose ``select`` is ``has_driver`` becomes certain once
    the Driver Selector is prepared. Rounds stop when no new bridge is selectable;
    there are at most one more than the routers in view.
    """
    editor_producer = {editor_id: producer_for(editor_class, editor_result)}
    sources: dict[str, tuple] = {editor_id: editor_result}
    producers: dict[str, Producer] = {}
    failed: dict[str, str] = {}
    reasons: dict[str, str] = {}
    view_overrides = editor_overrides
    routers = sum(1 for node in nodes_view.values() if isinstance(node, dict)
                  and cache_policy.lazy_policy(mappings.get(node.get("class_type"))) is not None)
    for _round in range(routers + 1):
        closure = certain_closure(roots, dynprompt.get_node, is_lazy, requested_by(view_overrides))
        order, needs, reasons = select_bridges(
            nodes_view, closure, editor_id, editor_class, mappings.get, input_type_of,
            lambda class_def: shape_reason(runtime, class_def), feeds_identity)
        new = [node_id for node_id in order if node_id not in producers and node_id not in failed]
        if not new:
            break
        for node_id in new:
            if any(source not in producers for source in needs[node_id]):
                failed[node_id] = "producer_failed"
                continue
            class_def = mappings[nodes_view[node_id]["class_type"]]
            try:
                values = _run_bridge(runtime, prompt_id, dynprompt, node_id, class_def, sources)
            except _BridgeSkip as skip:
                failed[node_id] = skip.reason
                continue
            sources[node_id] = values
            producers[node_id] = producer_for(class_def, values)
        view_overrides, _skips = plan_substitutions(nodes_view, {**editor_producer, **producers}, input_type_of)
    # The closure the identity pass reads: every router decided by every value prepared.
    closure = certain_closure(roots, dynprompt.get_node, is_lazy, requested_by(view_overrides))
    final = {node_id: reason for node_id, reason in reasons.items() if node_id not in producers}
    final.update(failed)
    outcomes = record["bridges"]
    outcomes["prepared"] = len(producers)
    for reason in final.values():
        record["bridge_reasons"][reason] = record["bridge_reasons"].get(reason, 0) + 1
    outcomes["skipped"] = len(final)
    return producers, closure


# --- Media identity (M2): bridges whose outputs are a recipe plus file content --------------


def _consumed(nodes_view: dict[str, dict], node_id: str) -> bool:
    return any(is_link(value) and str(value[0]) == node_id
               for other_id, node in nodes_view.items() if other_id != node_id and isinstance(node, dict)
               for value in (node.get("inputs") or {}).values())


def _identity_candidate(nodes_view: dict[str, dict], node_id: str, class_def: Any) -> str | None:
    """Why ``node_id`` can never be identified, from the graph alone, or None.

    Its recipe inputs must be literals (the recipe is computed before any other
    node runs) and some node must consume it (skipping its decode is the gain).
    """
    declared = cache_policy.identity_policy(class_def)
    inputs = nodes_view[node_id].get("inputs") or {}
    if not is_link(inputs.get(declared.token_input)):
        return "linked_input"
    if any(is_link(inputs.get(name)) for name in declared.recipe_inputs):
        return "linked_input"
    if not _consumed(nodes_view, node_id):
        return "unused"
    return None


def identity_feeds(nodes_view: dict[str, dict], class_of: Callable[[str], Any]) -> Callable[[str], bool]:
    """``feeds_identity`` for ``select_bridges``: a producer whose ``HANDLE`` reaches an
    identity candidate's token input is worth preparing even with no literal consumer."""
    feeding: set[str] = set()
    for node_id, node in nodes_view.items():
        class_def = class_of(node.get("class_type")) if isinstance(node, dict) else None
        declared = cache_policy.identity_policy(class_def) if class_def is not None else None
        if declared is None or _identity_candidate(nodes_view, node_id, class_def) is not None:
            continue
        link = (node.get("inputs") or {}).get(declared.token_input)
        source_class = class_of((nodes_view.get(str(link[0])) or {}).get("class_type"))
        slot = _slot_index(link)
        if source_class is not None and slot is not None and \
                cache_policy.slot_policy(source_class, slot) == cache_policy.HANDLE:
            feeding.add(str(link[0]))
    return feeding.__contains__


def select_identified(nodes_view: dict[str, dict], closure: set[str], editor_id: str, editor_class: Any,
                      producers: dict[str, Producer], prepared_candidates: set[str],
                      class_of: Callable[[str], Any], shape_of: Callable[[Any], str | None],
                      ) -> tuple[list[str], dict[str, str]]:
    """``(selected, reasons)``: the bridges whose media gets an identity this prompt. Pure.

    A node is selected when its class declares ``IDENTIFIES``, it is in the final
    closure (under the Editor's NaN, "stock would run it"), its shape is callable,
    its ``token_input`` links to the Editor's ``PER_RUN`` project or to a prepared
    producer's ``HANDLE``, every recipe input is a literal, and something consumes it.
    """
    selected: list[str] = []
    reasons: dict[str, str] = {}
    for node_id in sorted(nodes_view):
        node = nodes_view[node_id]
        class_def = class_of(node.get("class_type")) if isinstance(node, dict) else None
        declared = cache_policy.identity_policy(class_def) if class_def is not None else None
        if declared is None:
            continue
        if node_id not in closure:
            reasons[node_id] = "not_certain"
            continue
        if shape_of(class_def) is not None:
            reasons[node_id] = "shape"
            continue
        reason = _identity_candidate(nodes_view, node_id, class_def)
        if reason is not None:
            reasons[node_id] = reason
            continue
        link = node["inputs"][declared.token_input]
        source, slot = str(link[0]), _slot_index(link)
        if source == editor_id:
            ok = (slot is not None and slot < len(editor_class.RETURN_TYPES)
                  and editor_class.RETURN_TYPES[slot] == "SONDER_PROJECT"
                  and cache_policy.slot_policy(editor_class, slot) == cache_policy.PER_RUN)
        elif source in producers:
            ok = slot is not None and producers[source].slot_policy(slot) == cache_policy.HANDLE
        else:
            reasons[node_id] = "producer_failed" if source in prepared_candidates else "linked_input"
            continue
        if not ok:
            reasons[node_id] = "linked_input"
            continue
        selected.append(node_id)
    return selected, reasons


def _identity_args(runtime: ExecutorRuntime, dynprompt: Any, node_id: str, class_def: Any,
                   handle: Any) -> dict[str, Any]:
    """``cache_recipe``'s arguments: the token input's value and the recipe inputs, as scheduled."""
    declared = cache_policy.identity_policy(class_def)
    inputs = dynprompt.get_node(node_id).get("inputs") or {}
    resolved, missing, _v3_data = runtime.get_input_data(inputs, class_def, node_id, None, dynprompt, {})
    args: dict[str, Any] = {declared.token_input: handle}
    for name in declared.recipe_inputs:
        if name not in resolved:
            continue  # absent optional input: cache_recipe's default, as execute's
        values = resolved[name]
        if name in missing or not isinstance(values, (list, tuple)) or len(values) != 1:
            raise _BridgeSkip("linked_input")
        args[name] = values[0]
    return args


def _identity_pass(runtime: ExecutorRuntime, prompt_id: str, key: str, dynprompt: Any,
                   nodes_view: dict[str, dict], closure: set[str], editor_id: str, editor_class: Any,
                   editor_result: tuple, producers: dict[str, Producer], prepared_candidates: set[str],
                   mappings: dict, overrides: dict[str, dict], record: dict[str, Any]) -> dict[str, dict]:
    """Identify every selected bridge; their token overrides, merged over ``overrides``.

    For each: ``cache_recipe`` (pure) under the node's context, then the full
    content hash of every source (``cache_identity.identify``, which checks for an
    interrupt between chunks), then registration on the run record. Any failure
    leaves that bridge on its stock path; nothing is stored for it.
    """
    from ..nodes import cache_identity

    selected, reasons = select_identified(
        nodes_view, closure, editor_id, editor_class, producers, prepared_candidates, mappings.get,
        lambda class_def: shape_reason(runtime, class_def))
    tokens: dict[str, dict] = {}
    memo: dict[str, tuple] = {}
    for node_id in selected:
        class_type = nodes_view[node_id]["class_type"]
        class_def = mappings[class_type]
        declared = cache_policy.identity_policy(class_def)
        link = nodes_view[node_id]["inputs"][declared.token_input]
        source, slot = str(link[0]), _slot_index(link)
        handle = editor_result[slot] if source == editor_id else producers[source].result[slot]
        try:
            args = _identity_args(runtime, dynprompt, node_id, class_def, handle)
            with runtime.node_context(prompt_id, node_id, 0):
                runtime.before_node_execution()
                with runtime.inference_mode():
                    try:
                        recipe = class_def.cache_recipe(**args)
                    except runtime.interrupt_type:
                        raise
                    except Exception as exc:
                        logger.debug("Sonder cache pipeline: %s (%s) has no media identity: %s",
                                     node_id, class_type, type(exc).__name__)
                        raise _BridgeSkip("recipe_failed") from None
                try:
                    token, recipe_json, snapshots = cache_identity.identify(
                        class_type, recipe, runtime.before_node_execution, memo)
                except cache_identity.RecipeNotCanonical:
                    raise _BridgeSkip("recipe_not_json") from None
                except cache_identity.SourceUnstable:
                    raise _BridgeSkip("source_unstable") from None
                except cache_identity.SourceTooLarge:
                    raise _BridgeSkip("source_too_large") from None
                except (cache_identity.SourceMissing, OSError):
                    raise _BridgeSkip("source_missing") from None
        except _BridgeSkip as skip:
            reasons[node_id] = skip.reason
            continue
        refused = sdk_runs.register_identity(key, prompt_id, token, class_type, recipe_json, snapshots)
        if refused:
            reasons[node_id] = refused
            continue
        base = overrides.get(node_id) or nodes_view[node_id]
        replacement = copy.deepcopy(base)
        replacement["inputs"][declared.token_input] = token
        tokens[node_id] = replacement
    record["identities"] = {"identified": len(tokens), "skipped": len(reasons)}
    for reason in reasons.values():
        record["identity_reasons"][reason] = record["identity_reasons"].get(reason, 0) + 1
    record["identity_bytes"] = sum(int(snapshot[0]) for _digest, snapshot in memo.values())
    return tokens


# --- Preparation -------------------------------------------------------------------


def prepare(runtime: ExecutorRuntime, prompt_id: str, dynprompt: Any) -> dict[str, Any]:
    """Prepare one prompt; returns the preparation record. Raises only what the hook guards.

    Pipeline faults propagate to ``executor_hook``'s guard (no substitution was
    applied, because overrides are applied last). The Editor's own failures are
    stored here for replay and never propagate.
    """
    started = time.perf_counter()
    timings: dict[str, float] = {}
    record: dict[str, Any] = {"outcome": SKIPPED, "reason": "", "editor": "", "substituted": 0,
                              "skipped": 0, "skip_reasons": {}, "bridges": {}, "bridge_reasons": {},
                              "identities": {}, "identity_reasons": {}, "timings_ms": timings}
    key = ""
    try:
        prompt = dynprompt.get_original_prompt()
        if not isinstance(prompt, dict):
            raise _Skip("untrusted")
        step = time.perf_counter()
        editor_id, key, roots = _cheap_exits(prompt)
        record["editor"] = editor_id
        if run_scope.for_prompt(prompt) is None:
            raise _Skip("scope_unknown")
        timings["eligibility"] = _ms(step)

        step = time.perf_counter()
        info_memo: dict[tuple[str, str], Any] = {}

        def input_info(node_id: str, name: str):
            memo_key = (node_id, name)
            if memo_key not in info_memo:
                info_memo[memo_key] = runtime.input_info(dynprompt, node_id, name)
            return info_memo[memo_key]

        def is_lazy(node_id: str, name: str) -> bool:
            info = input_info(node_id, name)
            extra = info[2] if isinstance(info, tuple) and len(info) == 3 else None
            return isinstance(extra, dict) and bool(extra.get("lazy"))

        mappings = runtime.class_mappings()
        followed: set[tuple[str, str]] = set()

        def requested_by(view: dict[str, dict]) -> Callable[[str], list[str]]:
            """Lazy inputs each Sonder router in ``view`` (node id -> current node) will request."""
            def requested(node_id: str) -> list[str]:
                node = view.get(node_id) or dynprompt.get_node(node_id)
                class_def = mappings.get(node.get("class_type")) if isinstance(node, dict) else None
                if class_def is None or cache_policy.lazy_policy(class_def) is None:
                    return []
                names = router_requests(
                    class_def, node.get("inputs") or {},
                    lambda name: run_preparation.resolve_input_literal(prompt, node_id, name),
                    lambda name: is_lazy(node_id, name))
                followed.update((node_id, name) for name in names)
                return names
            return requested

        # Stock's graph, with routers decided by literals and static values only:
        # the Editor has not run, so none of its values is known yet.
        closure = certain_closure(roots, dynprompt.get_node, is_lazy, requested_by({}))
        timings["closure"] = _ms(step)
        if editor_id not in closure:
            raise _Skip("not_certain")
        editor_class = mappings[EDITOR_CLASS_TYPE]
        if not cache_policy.declared_policies(editor_class):
            # Another class registered under the Editor's id: its repeat-safety
            # (the per-prompt memo) is unknown, so it never runs early.
            raise _Skip("editor_undeclared")
        if _hidden_names(editor_class) != EDITOR_HIDDEN:
            raise _Skip("editor_hidden")

        step = time.perf_counter()
        outcome, result = _run_editor_early(runtime, prompt_id, dynprompt, prompt, editor_id, editor_class, key)
        timings["editor"] = _ms(step)
        if outcome != PREPARED:
            record["outcome"] = outcome
            return record
        if not isinstance(result, tuple) or len(result) != len(editor_class.RETURN_TYPES):
            raise _Skip("result_shape")

        step = time.perf_counter()
        scope = run_scope.for_prompt(prompt) or frozenset()
        nodes_view = {node_id: dynprompt.get_node(node_id) for node_id in prompt if node_id in scope}

        def input_type_of(node_id: str, name: str):
            info = input_info(node_id, name)
            if not isinstance(info, tuple) or len(info) != 3 or info[0] is None:
                return None
            consumer_type, _category, extra = info
            return (consumer_type if isinstance(consumer_type, str) else None), (extra or {})

        editor_producer = {editor_id: producer_for(editor_class, result)}
        overrides, skips = plan_substitutions(nodes_view, editor_producer, input_type_of)
        timings["plan"] = _ms(step)

        # The bridge pass is isolated: a fault in it keeps the Editor's plan, so
        # Phase 2 can never regress Phase 1. An interrupt still propagates.
        step = time.perf_counter()
        producers: dict[str, Producer] = {}
        try:
            producers, closure = _bridge_rounds(
                runtime, prompt_id, dynprompt, nodes_view, roots, editor_id, editor_class, result,
                mappings, input_type_of, is_lazy, requested_by, overrides, record,
                identity_feeds(nodes_view, mappings.get) if runtime.identity else None)
            if producers:
                overrides, skips = plan_substitutions(nodes_view, {**editor_producer, **producers}, input_type_of)
        except runtime.interrupt_type:
            raise
        except Exception as exc:
            producers = {}
            record["bridge_reasons"]["bridge_pass_error"] = 1
            record["bridge_error"] = type(exc).__name__
            logger.debug("Sonder cache pipeline: the bridge pass failed; only the Editor's values are reused",
                         exc_info=True)
        timings["bridges"] = _ms(step)
        record["lazy_followed"] = len(followed)

        # Media identity, isolated like the bridge pass: a fault keeps every value
        # substitution and drops only the tokens. An interrupt still propagates.
        substituted = len(overrides)
        step = time.perf_counter()
        if runtime.identity:
            try:
                candidates = {node_id for node_id, node in nodes_view.items() if isinstance(node, dict)
                              and cache_policy.prepare_policy(mappings.get(node.get("class_type"))) is not None}
                tokens = _identity_pass(runtime, prompt_id, key, dynprompt, nodes_view, closure, editor_id,
                                        editor_class, result, producers, candidates - set(producers), mappings,
                                        overrides, record)
                overrides = {**overrides, **tokens}
            except runtime.interrupt_type:
                raise
            except Exception as exc:
                record["identity_reasons"]["identity_pass_error"] = 1
                record["identity_error"] = type(exc).__name__
                logger.debug("Sonder cache pipeline: the identity pass failed; bridge media decodes as before",
                             exc_info=True)
        timings["identity"] = _ms(step)

        # Applied only once the whole plan exists, so a planning fault leaves none.
        for node_id, node in overrides.items():
            dynprompt.override_node(node_id, node)
        record.update(outcome=PREPARED, substituted=substituted, skipped=len(skips))
        for item in skips:
            record["skip_reasons"][item["reason"]] = record["skip_reasons"].get(item["reason"], 0) + 1
        return record
    except _Skip as skip:
        record.update(outcome=SKIPPED, reason=skip.reason)
        _note_reason(skip.reason)
        return record
    except BaseException as exc:
        # The hook's guard decides what happens; the record says what it was.
        record["outcome"] = INTERRUPTED if isinstance(exc, runtime.interrupt_type) else PIPELINE_ERROR
        record["reason"] = type(exc).__name__
        raise
    finally:
        timings["total"] = _ms(started)
        if key:
            sdk_runs.note_preparation(key, record)
        logger.debug("Sonder cache pipeline preparation for prompt %s: %s", prompt_id, record)


def _run_editor_early(runtime: ExecutorRuntime, prompt_id: str, dynprompt: Any, prompt: dict,
                      editor_id: str, editor_class: Any, key: str) -> tuple[str, Any]:
    """Run the real Editor once, as the scheduler would. ``(outcome, result)``.

    Two boundaries: everything here except ``editor_class().execute`` is pipeline
    code, whose faults propagate (nothing stored); the Editor's own raise is
    stored for the scheduled call to replay.
    """
    inputs = dynprompt.get_node(editor_id).get("inputs") or {}
    # dynprompt is required: without it hidden PROMPT becomes {} and the run's key
    # is untrusted, so the scheduled Editor would claim again.
    resolved, missing, _v3_data = runtime.get_input_data(inputs, editor_class, editor_id, None, dynprompt, {})
    if missing:
        raise _Skip("missing_inputs")
    args = {name: values[0] for name, values in resolved.items()}
    if args.get("prompt") is not prompt or sdk_runs.trusted_key_for_prompt(args.get("prompt")) != key:
        raise _Skip("prompt_identity")
    display_id = dynprompt.get_display_node_id(editor_id)
    progress = runtime.progress_state()
    started_progress = False
    try:
        with runtime.node_context(prompt_id, editor_id, 0):
            call_key = prompt_once.call_key(editor_id)
            if not call_key:
                raise _Skip("call_key")
            progress.start_progress(editor_id)
            started_progress = True
            server = runtime.server()
            if server is not None and getattr(server, "client_id", None) is not None:
                server.last_node_id = display_id
                server.send_sync("executing", {"node": editor_id, "display_node": display_id,
                                               "prompt_id": prompt_id}, server.client_id)
            runtime.before_node_execution()
            editor = editor_class()
            with runtime.inference_mode():
                # The replay boundary: only SonderEditor.execute itself.
                try:
                    result = editor.execute(**args)
                except runtime.interrupt_type:
                    sdk_runs.remember_failure(key, call_key, sdk_runs.CANCELLED)
                    runtime.rearm_interrupt()
                    return CANCELLED, None
                except Exception as exc:
                    _clear_tracebacks(exc)
                    sdk_runs.remember_failure(key, call_key, exc)
                    return EDITOR_FAILED, None
    finally:
        if started_progress:
            progress.finish_progress(editor_id)
    return PREPARED, result


def _clear_tracebacks(exc: BaseException) -> None:
    """Free the locals of every frame in the exception and its chain.

    A stored failure lives until ``close_release``; render tensors held by its
    frames must not.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        traceback.clear_frames(exc.__traceback__)
        exc = exc.__cause__ or exc.__context__


def reset_for_tests() -> None:
    with _LOGGED_LOCK:
        _LOGGED_REASONS.clear()
