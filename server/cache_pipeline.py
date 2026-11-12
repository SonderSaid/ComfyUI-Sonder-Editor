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
4. A pure planner (``plan_substitutions``) replaces each consumer's link to an
   ``EXACT`` Editor output with the literal value (M1), when the consumer's
   declared input type is exactly the output's literal type.
5. Overrides go through ``DynamicPrompt.override_node``, which feeds key
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
import logging
import math
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from . import run_scope, sdk_runs
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


def certain_closure(roots, get_node: Callable[[str], dict], is_lazy: Callable[[str, str], bool]) -> set[str]:
    """Nodes reached from ``roots`` over non-lazy links: ComfyUI's ``add_node`` rule.

    Under the Editor's NaN every node between a root and the Editor is uncached,
    so membership here equals "stock ComfyUI would run the Editor".
    """
    seen: set[str] = set()
    stack = [str(root) for root in roots]
    while stack:
        node_id = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        for name, value in (get_node(node_id).get("inputs") or {}).items():
            if is_link(value) and not is_lazy(node_id, name):
                stack.append(str(value[0]))
    return seen


def _hidden_names(class_def: Any) -> set[str]:
    hidden = (class_def.INPUT_TYPES() or {}).get("hidden") or {}
    return {run_scope._hidden_name(value) for value in hidden.values()}


# --- The pure M1 planner ---------------------------------------------------------


def plan_substitutions(nodes_view: dict[str, dict], editor_id: str, result: tuple,
                       slot_policy: Callable[[int], Any], slot_type: Callable[[int], str],
                       input_type_of: Callable[[str, str], Any]) -> tuple[dict[str, dict], list[dict]]:
    """``(overrides, skips)``: literal replacements for links to ``EXACT`` Editor outputs.

    A link is replaced only when the consumer declares the input's type exactly
    as the Editor slot's literal type (``STRING``/``INT``/``FLOAT``/``BOOLEAN``),
    the value's ``type`` is exactly that Python type, and a float is finite.
    ``rawLink`` inputs, wildcard, COMBO and unknown consumers are skipped. All
    substitutions for one node merge into one deep copy of its current node.
    Pure: reads only its arguments and raises only on a broken argument.
    """
    overrides: dict[str, dict] = {}
    skips: list[dict] = []
    for node_id, node in nodes_view.items():
        if node_id == editor_id or not isinstance(node, dict):
            continue
        replacement = None
        for name, value in (node.get("inputs") or {}).items():
            if not is_link(value) or str(value[0]) != editor_id:
                continue
            slot = _slot_index(value)

            def skip(reason: str) -> None:
                skips.append({"node": node_id, "input": name, "slot": slot, "reason": reason})

            if slot is None or slot >= len(result):
                skip("slot")
                continue
            if slot_policy(slot) != cache_policy.EXACT:
                continue  # declared otherwise: nothing to substitute, nothing skipped
            socket = slot_type(slot)
            expected = cache_policy.LITERAL_TYPES.get(socket)
            if expected is None:
                skip("socket_type")
                continue
            try:
                info = input_type_of(node_id, name)
            except Exception:
                info = None
            if not info:
                skip("consumer_unknown")
                continue
            consumer_type, extra = info
            if isinstance(extra, dict) and extra.get("rawLink"):
                skip("raw_link")
                continue
            # Base str equality: ComfyUI's IO.ANY ("*") overrides != to match any
            # type, which would hand a wildcard consumer a literal.
            if not isinstance(consumer_type, str) or str.__eq__(consumer_type, socket) is not True:
                skip("consumer_type")
                continue
            emitted = result[slot]
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
                              "skipped": 0, "skip_reasons": {}, "timings_ms": timings}
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

        closure = certain_closure(roots, dynprompt.get_node, is_lazy)
        timings["closure"] = _ms(step)
        if editor_id not in closure:
            raise _Skip("not_certain")
        mappings = runtime.class_mappings()
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

        overrides, skips = plan_substitutions(
            nodes_view, editor_id, result,
            lambda slot: cache_policy.slot_policy(editor_class, slot),
            lambda slot: editor_class.RETURN_TYPES[slot],
            input_type_of,
        )
        # Applied only once the whole plan exists, so a planning fault leaves none.
        for node_id, node in overrides.items():
            dynprompt.override_node(node_id, node)
        timings["plan"] = _ms(step)
        record.update(outcome=PREPARED, substituted=len(overrides), skipped=len(skips))
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
