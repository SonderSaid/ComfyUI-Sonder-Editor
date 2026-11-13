"""Run the cache pipeline's preparation before ComfyUI builds a prompt's cache keys.

ComfyUI offers no hook between creating a prompt's ``DynamicPrompt`` and building
its cache keys. ``PromptExecutor.execute_async`` constructs ``IsChangedCache``
in exactly that window, after it reset the interrupt flag, set ``client_id`` and
registered the web progress handler, and it looks the class up in module globals
when it runs. This module replaces ``execution.IsChangedCache`` with a subclass
whose constructor calls ``cache_pipeline.prepare`` under a guard, then the stock
constructor. The hook's only job is calling ``prepare`` safely; eligibility,
planning and Editor knowledge live in ``cache_pipeline``.

Narrow by construction, in the ``kj_preview_adapter`` pattern:

- **Shape gate** (``check_shape``): signatures and source tokens of every
  executor name the hook and the pipeline call, never a version pin. One
  function serves the runtime and ``tests/test_executor_hook.py``. A failing
  gate leaves ComfyUI untouched and logs once at INFO.
- **Never stacks**: a foreign ``IsChangedCache`` replacement is unsupported, and
  ``status`` reports ``degraded`` when something later restored or replaced ours.
- **Guard**: catches ``Exception`` and ComfyUI's ``InterruptProcessingException``
  (a ``BaseException``) and never lets either escape, because nothing in this
  window catches them and an escape kills the prompt worker. Every caught
  interrupt is re-armed so the first scheduled node stops. Other
  ``BaseException``s propagate as in stock. Bypassed under ``--cache-none``
  (``NullCache``), so the opt-out is exactly today's path.

- **Carriers** (``check_carrier``, a fourth gate): when the pipeline hands the
  Editor's media to ephemeral carriers, the hook arms a one-shot instance
  attribute on this prompt's outputs cache that wraps its ``set_prompt``: the
  stock method runs with the carrier ids appended, so they are keyed like any
  node, then each carrier's entry is stored. Any fault rolls the graph back
  with dictionary operations, while no cache key is being built, and runs the
  stock method again with the submitted ids. A failed gate disables only carriers.

Expiry: retire this hook when ComfyUI offers a supported hook that runs after
the ``DynamicPrompt`` exists and before ``set_prompt`` builds cache keys.
"""

from __future__ import annotations

import inspect
import logging
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

from . import cache_pipeline

logger = logging.getLogger(__name__)

MARKER = "_sonder_cache_pipeline"
STATE_OK, STATE_DEGRADED, STATE_UNSUPPORTED, STATE_ABSENT = "ok", "degraded", "unsupported", "absent"

# execute_async must do these in this order (Phase 0 qualified the window).
_EXECUTE_ASYNC_ORDER = (
    "interrupt_processing(False)",
    "self.server.client_id = ",
    "add_progress_handler(",
    "IsChangedCache(",
    ".set_prompt(",
)

_LOCK = threading.Lock()
_STATUS: dict[str, Any] = {"state": STATE_ABSENT}
_INSTALLED: dict[str, Any] = {}  # execution, stock, hooked, runtime, null_cache, prompt_server
_FAULT_LOGGED = False


def status() -> dict[str, Any]:
    """``ok | degraded | unsupported(reason) | absent``; re-checks the class is still ours."""
    with _LOCK:
        result = dict(_STATUS)
        execution = _INSTALLED.get("execution")
        hooked = _INSTALLED.get("hooked")
    if result.get("state") == STATE_OK and execution is not None and getattr(execution, "IsChangedCache", None) is not hooked:
        result.update(state=STATE_DEGRADED, reason="IsChangedCache replaced after install")
    return result


def _set(state: str, **fields: Any) -> dict[str, Any]:
    with _LOCK:
        _STATUS.clear()
        _STATUS.update(state=state, **fields)
        return dict(_STATUS)


def _params(function: Any) -> list[str]:
    return list(inspect.signature(function).parameters)


def _source(function: Any) -> str:
    return inspect.getsource(function)


def check_shape(execution: Any) -> str | None:
    """None when ``execution`` has the shape this hook was written for, else the reason."""
    try:
        executor = getattr(execution, "PromptExecutor", None)
        execute_async = getattr(executor, "execute_async", None)
        if execute_async is None:
            return "PromptExecutor.execute_async missing"
        source = _source(execute_async)
        last = -1
        for token in _EXECUTE_ASYNC_ORDER:
            at = source.find(token, last + 1)
            if at < 0:
                return f"execute_async order: {token}"
            last = at

        current = getattr(execution, "IsChangedCache", None)
        if not inspect.isclass(current):
            return "IsChangedCache missing"
        if not getattr(current, MARKER, False):
            if current.__module__ != getattr(execution, "__name__", "execution"):
                return "foreign IsChangedCache"
            if _params(current.__init__) != ["self", "prompt_id", "dynprompt", "outputs_cache"]:
                return "IsChangedCache.__init__ signature"
        if "IsChangedCache(prompt_id, dynamic_prompt, self.caches.outputs)" not in source:
            return "execute_async IsChangedCache arguments"

        get_input_data = getattr(execution, "get_input_data", None)
        if not callable(get_input_data) or _params(get_input_data) != [
                "inputs", "class_def", "unique_id", "execution_list", "dynprompt", "extra_data"]:
            return "get_input_data signature"
        if "return input_data_all, missing_keys, v3_data" not in _source(get_input_data):
            return "get_input_data return"

        dynamic_prompt = getattr(execution, "DynamicPrompt", None)
        for name in ("override_node", "get_node", "get_original_prompt", "get_display_node_id"):
            if not callable(getattr(dynamic_prompt, name, None)):
                return f"DynamicPrompt.{name}"
        if "node_overrides" not in _source(dynamic_prompt.get_node):
            return "DynamicPrompt.get_node reads no overrides"

        execution_list = getattr(execution, "ExecutionList", None)
        get_input_info = getattr(execution_list, "get_input_info", None)
        if not callable(get_input_info) or _params(get_input_info) != ["self", "unique_id", "input_name"]:
            return "ExecutionList.get_input_info signature"
        if "self.dynprompt.get_node(" not in _source(get_input_info):
            return "ExecutionList.get_input_info reads no dynprompt"

        context = getattr(execution, "CurrentNodeContext", None)
        if not inspect.isclass(context) or _params(context.__init__) != ["self", "prompt_id", "node_id", "list_index"]:
            return "CurrentNodeContext signature"

        get_progress_state = getattr(execution, "get_progress_state", None)
        progress = sys.modules.get(getattr(get_progress_state, "__module__", ""))
        registry = getattr(progress, "ProgressRegistry", None)
        if not callable(get_progress_state) or not all(
                callable(getattr(registry, name, None)) for name in ("start_progress", "finish_progress")):
            return "progress registry"
        handler = getattr(progress, "WebUIProgressHandler", None)
        handler_source = _source(handler) if inspect.isclass(handler) else ""
        if 'super().__init__("webui")' not in handler_source or "self.server_instance = server_instance" not in handler_source:
            return "WebUIProgressHandler server"

        execute_source = _source(getattr(execution, "execute"))
        if "server.last_node_id = display_node_id" not in execute_source or 'server.send_sync("executing"' not in execute_source:
            return "execute executing message"

        comfy_nodes = getattr(execution, "nodes", None)
        if not callable(getattr(comfy_nodes, "before_node_execution", None)):
            return "nodes.before_node_execution"
        if not isinstance(getattr(comfy_nodes, "NODE_CLASS_MAPPINGS", None), dict):
            return "nodes.NODE_CLASS_MAPPINGS"

        if not inspect.isclass(getattr(execution, "NullCache", None)):
            return "NullCache"

        model_management = getattr(getattr(execution, "comfy", None), "model_management", None)
        interrupt = getattr(model_management, "InterruptProcessingException", None)
        if not (inspect.isclass(interrupt) and issubclass(interrupt, BaseException)):
            return "InterruptProcessingException"
        for name in ("interrupt_current_processing", "processing_interrupted"):
            if not callable(getattr(model_management, name, None)):
                return f"model_management.{name}"
        return None
    except Exception as exc:
        return f"gate error: {type(exc).__name__}"


# The scheduler's V3 call (``_async_map_node_over_list``), in this order. Stock
# passes a node instance (``obj = class_def()``), so it takes the instance branch;
# the class branch must say the same. Bridge preparation repeats the sequence
# exactly, so a prepared V3 bridge sees what a scheduled one does.
_V3_CALL_ORDER = (
    "obj.VALIDATE_CLASS()",
    "class_clone = obj.PREPARE_CLASS_CLONE(v3_data)",
    "type_obj = type(obj)",
    "type_obj.VALIDATE_CLASS()",
    "class_clone = type_obj.PREPARE_CLASS_CLONE(v3_data)",
    "f = make_locked_method_func(type_obj, func, class_clone)",
    "inputs = _io.build_nested_inputs(inputs, v3_data, input_is_list=input_is_list)",
)


def check_v3_call(execution: Any) -> str | None:
    """None when the executor calls V3 nodes the way ``call_v3`` repeats it, else the reason.

    Separate from ``check_shape``: a mismatch only stops V3 bridges being
    prepared; the hook, the Editor and V1 bridges keep working.
    """
    try:
        mapper = getattr(execution, "_async_map_node_over_list", None)
        if mapper is None:
            return "_async_map_node_over_list missing"
        source = _source(mapper)
        last = -1
        for token in _V3_CALL_ORDER:
            at = source.find(token, last + 1)
            if at < 0:
                return f"V3 call order: {token}"
            last = at
        locked = getattr(execution, "make_locked_method_func", None)
        if not callable(locked) or _params(locked) != ["type_obj", "func", "class_clone"]:
            return "make_locked_method_func signature"
        nested = getattr(getattr(execution, "_io", None), "build_nested_inputs", None)
        if not callable(nested) or _params(nested)[:2] != ["values", "v3_data"]:
            return "build_nested_inputs signature"
        for name in ("first_real_override", "is_class"):
            if not callable(getattr(execution, name, None)):
                return name
        for name in ("_ComfyNodeInternal", "_NodeOutputInternal"):
            if not inspect.isclass(getattr(execution, name, None)):
                return name
        return None
    except Exception as exc:
        return f"V3 gate error: {type(exc).__name__}"


def _v3_runtime(execution: Any) -> dict[str, Any]:
    """The V3 fields of ``ExecutorRuntime``. ``is_v3`` whenever ComfyUI names its V3
    base, so a V3 class is never taken for V1; the call itself only when
    ``check_v3_call`` passes (otherwise V3 bridges are not prepared)."""
    base = getattr(execution, "_ComfyNodeInternal", None)
    fields: dict[str, Any] = {}
    if inspect.isclass(base):
        fields["is_v3"] = lambda class_def: inspect.isclass(class_def) and issubclass(class_def, base)
    reason = check_v3_call(execution)
    if reason is not None:
        logger.info("Sonder cache pipeline: V3 bridges are not prepared on this ComfyUI (%s); "
                    "their consumers rerun on every run as before.", reason)
        return fields

    def own_lazy_status(class_def):
        return execution.first_real_override(class_def, "check_lazy_status") is not None

    def call_v3(class_def, args, v3_data):
        # The executor's own sequence for a V3 class (``_V3_CALL_ORDER``).
        class_def.VALIDATE_CLASS()
        clone = class_def.PREPARE_CLASS_CLONE(v3_data)
        function = execution.make_locked_method_func(class_def, class_def.FUNCTION, clone)
        if v3_data is not None:
            args = execution._io.build_nested_inputs(args, v3_data, input_is_list=False)
        return function(**args)

    return {**fields, "own_lazy_status": own_lazy_status, "call_v3": call_v3,
            "node_output_type": execution._NodeOutputInternal}


def check_identity_context(execution: Any) -> str | None:
    """None when an identified bridge can find its run from inside ``execute``, else the reason.

    A media token resolves through ``get_executing_context().prompt_id``
    (``nodes/cache_identity.py``), which only holds while the executor sets that
    context with the same ``CurrentNodeContext`` the bridge reads. Separate from
    ``check_shape``: a mismatch only stops media identities.
    """
    try:
        from comfy_execution import utils

        from ..nodes import cache_identity
        if not callable(getattr(utils, "get_executing_context", None)):
            return "get_executing_context missing"
        if getattr(execution, "CurrentNodeContext", None) is not getattr(utils, "CurrentNodeContext", None):
            return "CurrentNodeContext is not comfy_execution.utils'"
        if cache_identity.get_executing_context is not utils.get_executing_context:
            return "cache_identity reads another executing context"
        return None
    except Exception as exc:
        return f"identity gate error: {type(exc).__name__}"


def _identity_runtime(execution: Any) -> dict[str, Any]:
    reason = check_identity_context(execution)
    if reason is not None:
        logger.info("Sonder cache pipeline: bridge media is not identified on this ComfyUI (%s); "
                    "its consumers rerun on every run as before.", reason)
        return {}
    return {"identity": True}


# execute_async builds keys this way (a carrier is keyed by the outputs cache's set_prompt).
_CARRIER_EXECUTE_ASYNC_ORDER = (
    "IsChangedCache(prompt_id, dynamic_prompt, self.caches.outputs)",
    "for cache in self.caches.all:",
    "await cache.set_prompt(dynamic_prompt, prompt.keys(), is_changed_cache)",
)
_CARRIER_CACHE_TYPES = ("HierarchicalCache", "LRUCache", "RAMPressureCache")


def check_carrier(execution: Any, stock_is_changed: Any = None) -> str | None:
    """None when the executor keys and runs ephemeral carriers the way the hook arms them, else the reason.

    Separate from ``check_shape``: a mismatch only stops carriers; Editor media
    consumers then rerun on every run as before.
    """
    try:
        caching = sys.modules.get("comfy_execution.caching")
        if caching is None:
            from comfy_execution import caching
        source = _source(execution.PromptExecutor.execute_async)
        last = -1
        for token in _CARRIER_EXECUTE_ASYNC_ORDER:
            at = source.find(token, last + 1)
            if at < 0:
                return f"carrier execute_async order: {token}"
            last = at
        if "self.all = [self.outputs, self.objects]" not in _source(execution.CacheSet.__init__):
            return "CacheSet.all order"
        basic = caching.BasicCache
        if _params(basic.set_prompt) != ["self", "dynprompt", "node_ids", "is_changed_cache"]:
            return "BasicCache.set_prompt signature"
        basic_source = _source(basic.set_prompt)
        assign = basic_source.find("self.cache_key_set = self.key_class(dynprompt, node_ids, is_changed_cache)")
        keyed = basic_source.find("await self.cache_key_set.add_keys(node_ids)")
        if assign < 0 or keyed < assign:
            return "BasicCache.set_prompt keys"
        if "set_prompt" in vars(caching.HierarchicalCache):
            return "HierarchicalCache.set_prompt overridden"
        for name in ("LRUCache", "RAMPressureCache"):
            if "await super().set_prompt(dynprompt, node_ids, is_changed_cache)" not in _source(
                    getattr(caching, name).set_prompt):
                return f"{name}.set_prompt"
        for name in ("BasicCache", "HierarchicalCache", "LRUCache", "RAMPressureCache"):
            if _params(getattr(caching, name).set_local) != ["self", "node_id", "value"]:
                return f"{name}.set_local signature"
        add_keys = _source(caching.CacheKeySetInputSignature.add_keys)
        if "if not self.dynprompt.has_node(node_id)" not in add_keys or "get_node_signature" not in add_keys:
            return "CacheKeySetInputSignature.add_keys"
        if "return self.keys.get(node_id, None)" not in _source(caching.CacheKeySet.get_data_key):
            return "CacheKeySet.get_data_key"
        if tuple(getattr(execution.CacheEntry, "_fields", ())) != ("ui", "outputs"):
            return "CacheEntry fields"
        dynamic_prompt = execution.DynamicPrompt
        init = _source(dynamic_prompt.__init__)
        for token in ("self.node_overrides = {}", "self.ephemeral_prompt = {}", "self.ephemeral_display = {}"):
            if token not in init:
                return f"DynamicPrompt.__init__: {token}"
        if "self.ephemeral_prompt" not in _source(dynamic_prompt.has_node):
            return "DynamicPrompt.has_node"
        if "self.ephemeral_display" not in _source(dynamic_prompt.get_display_node_id):
            return "DynamicPrompt.get_display_node_id"
        if "get_real_node_id" not in vars(dynamic_prompt) or "__slots__" in vars(dynamic_prompt):
            return "DynamicPrompt.get_real_node_id"
        execute_source = _source(execution.execute)
        if "real_node_id = dynprompt.get_real_node_id(unique_id)" not in execute_source:
            return "execute real_node_id"
        # The attribution works only while an error names the node by its real id and the
        # handler looks that id up in the submitted prompt (an unattributed carrier kills the worker).
        if execute_source.count('"node_id": real_node_id') < 2:
            return "execute error node_id"
        handler = _source(execution.PromptExecutor.handle_execution_error)
        if 'node_id = error["node_id"]' not in handler or 'prompt[node_id]["class_type"]' not in handler:
            return "handle_execution_error lookup"
        is_changed = stock_is_changed if stock_is_changed is not None else execution.IsChangedCache
        if "self.is_changed = {}" not in _source(is_changed.__init__):
            return "IsChangedCache.is_changed"
        if "validate_inputs(prompt_id, prompt, o, validated)" not in _source(execution.validate_prompt):
            return "validate_prompt validates submitted outputs"
        return None
    except Exception as exc:
        return f"carrier gate error: {type(exc).__name__}"


def _carrier_runtime(execution: Any, stock_is_changed: Any) -> dict[str, Any] | None:
    """What the hook needs to arm carriers, or None (logged once) when the gate fails."""
    reason = check_carrier(execution, stock_is_changed)
    if reason is None:
        try:
            from ..nodes import media_carrier
        except Exception as exc:  # no comfy_api: the class is not registered either
            reason = f"carrier class unavailable: {type(exc).__name__}"
    if reason is None:
        try:
            from comfy_execution import caching
            return {
                "cache_types": {getattr(caching, name): getattr(caching, name).set_prompt
                                for name in _CARRIER_CACHE_TYPES},
                "key_class": caching.CacheKeySetInputSignature,
                "entry": execution.CacheEntry,
                "carrier": media_carrier.SonderMediaCarrier,
                "mappings": lambda: execution.nodes.NODE_CLASS_MAPPINGS,
            }
        except Exception as exc:  # never costs Phases 1-3 their hook
            reason = f"carrier runtime: {type(exc).__name__}"
    logger.info("Sonder cache pipeline: nodes after the Sonder Editor rerun on every run while Editor media "
                "cannot be carried by content on this ComfyUI (%s).", reason)
    return None


def _carrier_precondition(carriers: dict[str, Any] | None, outputs_cache: Any) -> str:
    """Empty when this prompt's outputs cache can take carriers, else the reason code."""
    if carriers is None:
        return "carrier_gate"
    cache_type = type(outputs_cache)
    if cache_type not in carriers["cache_types"] or getattr(outputs_cache, "key_class", None) is not carriers["key_class"]:
        return "cache_type"
    if cache_type.set_prompt is not carriers["cache_types"][cache_type]:
        return "cache_type"  # set_prompt replaced on the class after install
    if "set_prompt" in getattr(outputs_cache, "__dict__", {}):
        return "set_prompt_armed"
    if carriers["mappings"]().get(cache_pipeline.CARRIER_CLASS_TYPE) is not carriers["carrier"]:
        return "carrier_class_missing"
    return ""


def _note_rollback(key: str, exc: BaseException) -> None:
    """Merge the keying rollback into the run's preparation record (note_preparation replaces it)."""
    try:
        from . import sdk_runs
        record = sdk_runs.preparation(key) or {}
        reasons = dict(record.get("content_reasons") or {})
        reasons["keying_rolled_back"] = reasons.get("keying_rolled_back", 0) + 1
        content = dict(record.get("content") or {})
        content.update(carried=0, skipped=int(content.get("skipped", 0)) + int(content.get("carried", 0)))
        record.update(content_reasons=reasons, content=content, content_error=type(exc).__name__)
        sdk_runs.note_preparation(key, record)
    except Exception:
        logger.debug("Sonder cache pipeline could not record a carrier rollback", exc_info=True)


def _arm_carriers(carriers: dict[str, Any], outputs_cache: Any, dynprompt: Any, plan: Any) -> None:
    """Arm this prompt's outputs cache to key ``plan``'s carriers in its next ``set_prompt``.

    A one-shot instance attribute: it removes itself when called, and passes any
    other prompt straight to the stock method. The stock method runs with the
    carrier ids appended, so carriers are keyed like any node (and LRU and RAM
    mark them used); then each carrier's entry is stored, holding the same
    object as the Editor's memo. If keying fails, the graph is rolled back with
    dictionary operations, while no key is being built, and the stock method runs
    again with the submitted ids: every consumer then misses on its own inputs.
    """
    stock = type(outputs_cache).set_prompt
    entry = carriers["entry"]
    carrier_ids = list(plan.carrier_ids)

    async def set_prompt(prompt_dynprompt, node_ids, is_changed_cache):
        outputs_cache.__dict__.pop("set_prompt", None)
        if prompt_dynprompt is not dynprompt:
            return await stock(outputs_cache, prompt_dynprompt, node_ids, is_changed_cache)
        submitted = list(node_ids)
        try:
            await stock(outputs_cache, prompt_dynprompt, submitted + carrier_ids, is_changed_cache)
            key_set = outputs_cache.cache_key_set
            if any(key_set.get_data_key(carrier_id) is None for carrier_id in carrier_ids):
                raise LookupError("a media carrier has no cache key")
        except Exception as exc:
            cache_pipeline.rollback_carriers(dynprompt, plan)
            memo = getattr(is_changed_cache, "is_changed", None)
            if isinstance(memo, dict):
                for node_id in (*carrier_ids, *plan.restore):
                    memo.pop(node_id, None)
            logger.debug("Sonder cache pipeline: carrier keying failed; Editor media runs uncached", exc_info=True)
            _note_rollback(plan.key, exc)
            plan.payloads.clear()
            return await stock(outputs_cache, prompt_dynprompt, submitted, is_changed_cache)
        try:
            # An optimisation: a carrier whose entry is stored never executes.
            for carrier_id in carrier_ids:
                outputs_cache.set_local(carrier_id, entry(ui=None, outputs=[[plan.payloads[carrier_id]]]))
        except Exception:
            logger.debug("Sonder cache pipeline: a carrier entry was not stored; it runs if needed", exc_info=True)
        finally:
            plan.payloads.clear()

    outputs_cache.set_prompt = set_prompt


def _build_runtime(execution: Any, prompt_server: Any) -> cache_pipeline.ExecutorRuntime:
    # Every name is looked up when called, as the executor itself does, so a pack
    # that wraps one after Sonder loads is seen by the early run too.
    model_management = execution.comfy.model_management

    def server():
        handler = getattr(execution.get_progress_state(), "handlers", {}).get("webui")
        instance = getattr(handler, "server_instance", None)
        return instance if instance is not None else prompt_server

    def input_info(dynprompt, node_id, name):
        # The scheduler's own lookup with a duck-typed self: same lazy semantics,
        # V3 dynamic inputs finalised exactly as the scheduler finalises them.
        return execution.ExecutionList.get_input_info(SimpleNamespace(dynprompt=dynprompt), node_id, name)

    def inference_mode():
        import torch
        return torch.inference_mode()

    return cache_pipeline.ExecutorRuntime(
        get_input_data=lambda *args: execution.get_input_data(*args),
        node_context=lambda *args: execution.CurrentNodeContext(*args),
        progress_state=lambda: execution.get_progress_state(),
        server=server,
        before_node_execution=lambda: execution.nodes.before_node_execution(),
        class_mappings=lambda: execution.nodes.NODE_CLASS_MAPPINGS,
        input_info=input_info,
        interrupt_type=model_management.InterruptProcessingException,
        rearm_interrupt=lambda: model_management.interrupt_current_processing(True),
        inference_mode=inference_mode,
        **_v3_runtime(execution),
        **_identity_runtime(execution),
    )


def _guarded_prepare(prompt_id: Any, dynprompt: Any, outputs_cache: Any) -> None:
    """Call ``prepare``; never let an ``Exception`` or ComfyUI's interrupt escape."""
    global _FAULT_LOGGED
    with _LOCK:
        runtime = _INSTALLED.get("runtime")
        null_cache = _INSTALLED.get("null_cache")
        carriers = _INSTALLED.get("carriers")
    if runtime is None:
        return
    started = time.perf_counter()
    try:
        if null_cache is not None and isinstance(outputs_cache, null_cache):
            return  # --cache-none: exactly today's path
        media = cache_pipeline.MediaHost(
            reason=_carrier_precondition(carriers, outputs_cache),
            arm=lambda plan: _arm_carriers(carriers, outputs_cache, dynprompt, plan))
        cache_pipeline.prepare(runtime, str(prompt_id), dynprompt, media)
    except runtime.interrupt_type:
        # Raising cleared ComfyUI's flag: set it again so the first scheduled node stops.
        try:
            runtime.rearm_interrupt()
        except Exception:
            logger.warning("Sonder cache pipeline could not re-arm a cancel", exc_info=True)
    except Exception:
        if not _FAULT_LOGGED:
            _FAULT_LOGGED = True
            logger.warning("Sonder cache pipeline failed; this prompt runs uncached as before", exc_info=True)
        else:
            logger.debug("Sonder cache pipeline failed; this prompt runs uncached as before", exc_info=True)
    finally:
        logger.debug("Sonder cache pipeline hook for prompt %s took %.3f ms", prompt_id,
                     (time.perf_counter() - started) * 1000.0)


def _make_subclass(stock: type) -> type:
    class SonderIsChangedCache(stock):
        """ComfyUI's ``IsChangedCache``, constructed after the Sonder cache pipeline prepared the prompt."""

        def __init__(self, *args, **kwargs):
            # Takes whatever the executor passes, so a changed call reaches the
            # stock constructor (and its own error) instead of a TypeError here,
            # outside every guard, that would kill the prompt worker.
            if len(args) == 3 and not kwargs:
                _guarded_prepare(*args)
            super().__init__(*args, **kwargs)

    setattr(SonderIsChangedCache, MARKER, True)
    SonderIsChangedCache.__module__ = __name__
    return SonderIsChangedCache


def install(execution: Any = None, prompt_server: Any = None) -> dict[str, Any]:
    """Install once, or report why not. Idempotent; never imports ComfyUI itself."""
    try:
        execution = execution if execution is not None else sys.modules.get("execution")
        if execution is None or not hasattr(execution, "PromptExecutor"):
            return _set(STATE_ABSENT)  # not loaded yet: the startup call retries
        with _LOCK:
            if prompt_server is not None:
                _INSTALLED["prompt_server"] = prompt_server
            current = getattr(execution, "IsChangedCache", None)
            if getattr(current, MARKER, False) and _INSTALLED.get("hooked") is current:
                return dict(_STATUS)
        reason = check_shape(execution)
        if reason is not None or getattr(current, MARKER, False):
            reason = reason or "IsChangedCache already replaced by another copy of this hook"
            logger.info("Sonder cache pipeline is unavailable on this ComfyUI (%s); "
                        "nodes after the Sonder Editor rerun on every run as before.", reason)
            return _set(STATE_UNSUPPORTED, reason=reason)
        with _LOCK:
            runtime = _build_runtime(execution, _INSTALLED.get("prompt_server"))
            carriers = _carrier_runtime(execution, current)
            hooked = _make_subclass(current)
            _INSTALLED.update(execution=execution, stock=current, hooked=hooked, runtime=runtime,
                              null_cache=execution.NullCache, carriers=carriers)
            execution.IsChangedCache = hooked
        return _set(STATE_OK)
    except Exception:
        logger.warning("Sonder cache pipeline hook unavailable", exc_info=True)
        return _set(STATE_UNSUPPORTED, reason="install failed")


def install_on_startup(prompt_server: Any) -> None:
    """Retry once the server starts, in case ``execution`` was not loaded yet."""
    app = getattr(prompt_server, "app", None)
    hooks = getattr(app, "on_startup", None)
    if hooks is None or getattr(app, "_sonder_cache_pipeline_startup", False):
        return
    try:
        async def _startup(_app):
            install(prompt_server=prompt_server)

        hooks.append(_startup)
        app._sonder_cache_pipeline_startup = True
    except Exception:
        logger.debug("Sonder cache pipeline startup hook unavailable", exc_info=True)


def uninstall_for_tests() -> None:
    """Restore the stock class (the differential oracle's "pipeline off" arm)."""
    global _FAULT_LOGGED
    with _LOCK:
        execution, stock, hooked = _INSTALLED.get("execution"), _INSTALLED.get("stock"), _INSTALLED.get("hooked")
        if execution is not None and stock is not None and getattr(execution, "IsChangedCache", None) is hooked:
            execution.IsChangedCache = stock
        _INSTALLED.clear()
        _STATUS.clear()
        _STATUS["state"] = STATE_ABSENT
        _FAULT_LOGGED = False


def reset_for_tests() -> None:
    uninstall_for_tests()
