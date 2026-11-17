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
- **Deferred Editor media** (``check_deferral``, a fifth gate, Phase 4b): the
  Editor's frames and audio are not rendered by the early run; after the stock
  keying, the armed ``set_prompt`` settles each deferred carrier from the
  outputs cache (``get_local``: a hit renders nothing) or by rendering it now,
  before ``clean_unused`` and before any node runs. A failed gate disables only
  deferral: Phase 4a's carriers keep working.
- **Early release** (``check_early_release``, a sixth gate): before that render,
  the armed ``set_prompt`` makes the calls ComfyUI makes next (``clean_unused``,
  and in RAM mode ``ram_release`` to the executor's inactive headroom), so the
  previous prompt's unused entries are not resident during the render. The
  executor is read from the awaiting ``execute_async`` frame. A failed gate
  disables only the early release.

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


# execute_async settles a deferred carrier inside the outputs cache's set_prompt, before
# clean_unused drops the previous prompt's unused entries.
_DEFERRAL_EXECUTE_ASYNC_ORDER = (
    "await cache.set_prompt(dynamic_prompt, prompt.keys(), is_changed_cache)",
    "cache.clean_unused()",
)


def check_deferral(execution: Any) -> str | None:
    """None when a carrier's previous entry can be looked up inside ``set_prompt``, else the reason.

    Separate from ``check_carrier``: a mismatch only stops the Editor deferring
    its render and mix; Phase 4a's content carriers keep working.
    """
    try:
        caching = sys.modules.get("comfy_execution.caching")
        if caching is None:
            from comfy_execution import caching
        source = _source(execution.PromptExecutor.execute_async)
        last = -1
        for token in _DEFERRAL_EXECUTE_ASYNC_ORDER:
            at = source.find(token, last + 1)
            if at < 0:
                return f"deferral execute_async order: {token}"
            last = at
        basic = caching.BasicCache
        if _params(basic.get_local) != ["self", "node_id"]:
            return "BasicCache.get_local signature"
        get_local = _source(basic.get_local)
        for token in ("cache_key = self.cache_key_set.get_data_key(node_id)", "return self.cache[cache_key]"):
            if token not in get_local:
                return f"BasicCache.get_local: {token}"
        if "BasicCache.get_local(cache, node_id)" not in _source(caching.HierarchicalCache.get_local):
            return "HierarchicalCache.get_local"
        for name in ("LRUCache", "RAMPressureCache"):
            if "get_local" in vars(getattr(caching, name)):
                return f"{name}.get_local overridden"
        if "self.cache = {}" not in _source(basic.__init__):
            return "BasicCache.cache"
        if tuple(getattr(execution.CacheEntry, "_fields", ())) != ("ui", "outputs"):
            return "CacheEntry fields"
        return None
    except Exception as exc:
        return f"deferral gate error: {type(exc).__name__}"


# execute_async releases the outputs cache this way: clean_unused right after keying, and in
# RAM mode ram_release to the inactive headroom after every node that succeeds. Early release
# makes the same calls a step sooner, inside the armed set_prompt, on a deferred miss.
# Expiry: the early release (this gate, _early_release_runtime, _awaiting_executor,
# _release_obsolete and the frame read in _arm_carriers) exists only because the deferred
# render runs inside set_prompt, before ComfyUI's own cleanup. Remove it when that render
# leaves set_prompt, or when ComfyUI releases obsolete entries before set_prompt returns.
_EARLY_RELEASE_EXECUTE_ASYNC_ORDER = (
    'ram_inactive_headroom = int(self.cache_args["ram_inactive"] * (1024 ** 3))',
    "ram_release_callback = self.caches.outputs.ram_release if self.cache_type == CacheType.RAM_PRESSURE else None",
    "await cache.set_prompt(dynamic_prompt, prompt.keys(), is_changed_cache)",
    "cache.clean_unused()",
    "ram_release_callback(ram_inactive_headroom)",
)


def check_early_release(execution: Any) -> str | None:
    """None when releasing obsolete entries before a deferred render does what ComfyUI does next.

    Separate from ``check_deferral``: a mismatch only stops the early release; the
    deferred render then runs with the previous prompt's unused entries resident,
    as before.
    """
    try:
        caching = sys.modules.get("comfy_execution.caching")
        if caching is None:
            from comfy_execution import caching
        executor = execution.PromptExecutor
        source = _source(executor.execute_async)
        last = -1
        for token in _EARLY_RELEASE_EXECUTE_ASYNC_ORDER:
            at = source.find(token, last + 1)
            if at < 0:
                return f"early release execute_async order: {token}"
            last = at
        init = _source(executor.__init__)
        for token in ("self.cache_args = cache_args", "self.cache_type = cache_type"):
            if token not in init:
                return f"PromptExecutor.__init__: {token}"
        if "self.caches = CacheSet(cache_type=self.cache_type, cache_args=self.cache_args)" not in _source(
                executor.reset):
            return "PromptExecutor.reset"
        basic = _source(caching.BasicCache.clean_unused)
        if "self._clean_cache()" not in basic or "self._clean_subcaches()" not in basic:
            return "BasicCache.clean_unused"
        if "clean_unused" in vars(caching.HierarchicalCache):
            return "HierarchicalCache.clean_unused overridden"
        lru = caching.LRUCache
        if "self.min_generation < self.generation" not in _source(lru.clean_unused):
            return "LRUCache.clean_unused"
        lru_keys = _source(lru.set_prompt)
        if "self.generation += 1" not in lru_keys or "self._mark_used(node_id)" not in lru_keys:
            return "LRUCache.set_prompt"
        ram = caching.RAMPressureCache
        ram_clean = _source(ram.clean_unused)
        if "self._clean_subcaches()" not in ram_clean or "_clean_cache" in ram_clean:
            return "RAMPressureCache.clean_unused"
        if _params(ram.ram_release) != ["self", "target", "free_active", "min_entry_size"]:
            return "RAMPressureCache.ram_release signature"
        if inspect.signature(ram.ram_release).parameters["free_active"].default is not False:
            return "RAMPressureCache.ram_release free_active default"
        # RAM keys through LRUCache.set_prompt, which marks this prompt's entries current.
        if "await super().set_prompt(dynprompt, node_ids, is_changed_cache)" not in _source(ram.set_prompt):
            return "RAMPressureCache.set_prompt"
        if "if not free_active and self.used_generation[key] == self.generation:" not in _source(ram.ram_release):
            return "RAMPressureCache.ram_release keeps the current generation"
        if not hasattr(getattr(execution, "CacheType", None), "RAM_PRESSURE"):
            return "CacheType.RAM_PRESSURE"
        if not callable(getattr(execution.comfy.model_management, "processing_interrupted", None)):
            return "model_management.processing_interrupted"
        return None
    except Exception as exc:
        return f"early release gate error: {type(exc).__name__}"


def _early_release_runtime(execution: Any) -> dict[str, Any] | None:
    """What the armed ``set_prompt`` needs to release early, or None (logged once) when the gate fails."""
    reason = check_early_release(execution)
    if reason is None:
        try:
            from comfy_execution import caching
            return {
                # The awaiting frame's code: the stock body, through any functools.wraps wrapper.
                "code": inspect.unwrap(execution.PromptExecutor.execute_async).__code__,
                "ram_type": caching.RAMPressureCache,
                "ram_cache_type": execution.CacheType.RAM_PRESSURE,
                "interrupted": lambda: execution.comfy.model_management.processing_interrupted(),
            }
        except Exception as exc:
            reason = f"early release runtime: {type(exc).__name__}"
    logger.info("Sonder cache pipeline: when the Sonder Editor renders a changed timeline, the previous "
                "run's unused results stay in memory until ComfyUI frees them on this ComfyUI (%s).", reason)
    return None


def _awaiting_executor(early: dict[str, Any], frame: Any, outputs_cache: Any) -> Any:
    """The ``PromptExecutor`` whose ``execute_async`` awaits this ``set_prompt``, or None.

    Read from the awaiting frame, never copied, so the release uses the
    executor's live ``cache_type`` and ``cache_args`` (expiry: drop with the
    early release when the render leaves ``set_prompt``).
    """
    try:
        if frame is None or frame.f_code is not early["code"]:
            return None
        owner = frame.f_locals.get("self")
        if getattr(getattr(owner, "caches", None), "outputs", None) is not outputs_cache:
            return None
        return owner
    except Exception:
        return None


_EARLY_RELEASE_SKIP_LOGGED = False


def _release_obsolete(early: dict[str, Any], outputs_cache: Any, executor: Any) -> dict[str, Any]:
    """Release what ComfyUI releases next, before a deferred render. Never raises.

    ``clean_unused`` on every cache type (Classic drops entries this prompt does
    not use, LRU only generations over capacity, RAM only subcaches; the stock
    call right after then removes nothing more). RAM also ``ram_release`` to the
    executor's inactive headroom with ``free_active=False``, the call the
    executor makes after every successful node, unless a cancel is pending.
    """
    global _EARLY_RELEASE_SKIP_LOGGED
    record: dict[str, Any] = {}
    try:
        before = len(outputs_cache.cache)
        outputs_cache.clean_unused()
        record["cleaned"] = max(0, before - len(outputs_cache.cache))
    except Exception as exc:
        record["clean_error"] = type(exc).__name__
        logger.debug("Sonder cache pipeline: the early clean_unused failed", exc_info=True)
    if type(outputs_cache) is not early["ram_type"]:
        return record
    try:
        if executor is None or executor.cache_type != early["ram_cache_type"]:
            record["ram"] = "executor_unknown"
            if not _EARLY_RELEASE_SKIP_LOGGED:
                _EARLY_RELEASE_SKIP_LOGGED = True
                logger.info("Sonder cache pipeline: the previous run's unused results stay in memory while the "
                            "Sonder Editor renders (this ComfyUI's executor was not recognised).")
        elif early["interrupted"]():
            record["ram"] = "interrupted"
        else:
            headroom = int(executor.cache_args["ram_inactive"] * (1024 ** 3))
            record["ram_freed"] = int(outputs_cache.ram_release(headroom) or 0)
            record["ram"] = "released"
    except Exception as exc:
        record["ram"] = "error"
        record["ram_error"] = type(exc).__name__
        logger.debug("Sonder cache pipeline: the early ram_release failed", exc_info=True)
    return record


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
            deferral = check_deferral(execution)
            if deferral is not None:
                logger.info("Sonder cache pipeline: the Sonder Editor renders on every run on this ComfyUI (%s); "
                            "nodes after it still reuse unchanged media.", deferral)
            return {
                "cache_types": {getattr(caching, name): getattr(caching, name).set_prompt
                                for name in _CARRIER_CACHE_TYPES},
                "key_class": caching.CacheKeySetInputSignature,
                "entry": execution.CacheEntry,
                "carrier": media_carrier.SonderMediaCarrier,
                "mappings": lambda: execution.nodes.NODE_CLASS_MAPPINGS,
                "defer": deferral is None,
                "early_release": _early_release_runtime(execution) if deferral is None else None,
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


def _note_rollback(key: str, exc: BaseException | None, reason: str = "keying_rolled_back") -> None:
    """Merge a carrier rollback into the run's preparation record (note_preparation replaces it)."""
    try:
        from . import sdk_runs
        record = sdk_runs.preparation(key) or {}
        reasons = dict(record.get("content_reasons") or {})
        reasons[reason] = reasons.get(reason, 0) + 1
        content = dict(record.get("content") or {})
        content.update(carried=0, skipped=int(content.get("skipped", 0)) + int(content.get("carried", 0)))
        record.update(content_reasons=reasons, content=content)
        if exc is not None:
            record["content_error"] = type(exc).__name__
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
    early = carriers.get("early_release")
    carrier_ids = list(plan.carrier_ids)

    def roll_back(is_changed_cache) -> None:
        cache_pipeline.rollback_carriers(dynprompt, plan)
        memo = getattr(is_changed_cache, "is_changed", None)
        if isinstance(memo, dict):
            for node_id in (*carrier_ids, *plan.restore):
                memo.pop(node_id, None)
        plan.payloads.clear()

    async def set_prompt(prompt_dynprompt, node_ids, is_changed_cache):
        outputs_cache.__dict__.pop("set_prompt", None)
        if prompt_dynprompt is not dynprompt:
            return await stock(outputs_cache, prompt_dynprompt, node_ids, is_changed_cache)
        # execute_async awaits this directly: its frame names the executor (the RAM headroom).
        # Early release only; expiry with it (see _EARLY_RELEASE_EXECUTE_ASYNC_ORDER).
        executor = None
        if early is not None:
            try:
                frame = sys._getframe(1)
            except Exception:
                frame = None
            executor = _awaiting_executor(early, frame, outputs_cache)
            del frame
        submitted = list(node_ids)
        try:
            await stock(outputs_cache, prompt_dynprompt, submitted + carrier_ids, is_changed_cache)
            key_set = outputs_cache.cache_key_set
            if any(key_set.get_data_key(carrier_id) is None for carrier_id in carrier_ids):
                raise LookupError("a media carrier has no cache key")
        except Exception as exc:
            roll_back(is_changed_cache)
            logger.debug("Sonder cache pipeline: carrier keying failed; Editor media runs uncached", exc_info=True)
            _note_rollback(plan.key, exc)
            return await stock(outputs_cache, prompt_dynprompt, submitted, is_changed_cache)
        # An optimisation: a carrier whose entry is stored never executes. Each
        # carrier on its own, so one that fails (or is deferred) never stops another.
        for carrier_id, payload in list(plan.payloads.items()):
            try:
                outputs_cache.set_local(carrier_id, entry(ui=None, outputs=[[payload]]))
            except Exception:
                logger.debug("Sonder cache pipeline: a carrier entry was not stored; it runs if needed",
                             exc_info=True)
        plan.payloads.clear()
        settle = getattr(plan, "settle", None)
        if getattr(plan, "deferred", None) and callable(settle):
            # Phase 4b: the Editor's deferred frames and audio, from this cache on a
            # hit or rendered now, before any node runs. settle never raises. On a
            # miss it first releases what ComfyUI releases next (early release).
            release = None
            if early is not None:
                release = lambda: _release_obsolete(early, outputs_cache, executor)  # noqa: E731
            if settle(outputs_cache, entry, release) == "rollback":
                roll_back(is_changed_cache)
                # The Editor failed (stored for replay): its value substitutions go too, so
                # every consumer waits for the scheduled Editor, as after a failed early run.
                memo = getattr(is_changed_cache, "is_changed", None)
                for node_id in getattr(plan, "value_overrides", None) or ():
                    dynprompt.node_overrides.pop(node_id, None)
                    if isinstance(memo, dict):
                        memo.pop(node_id, None)
                _note_rollback(plan.key, None, "settle_rolled_back")
                return await stock(outputs_cache, prompt_dynprompt, submitted, is_changed_cache)

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
            arm=lambda plan: _arm_carriers(carriers, outputs_cache, dynprompt, plan),
            defer=bool(carriers and carriers.get("defer")))
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
    global _FAULT_LOGGED, _EARLY_RELEASE_SKIP_LOGGED
    with _LOCK:
        execution, stock, hooked = _INSTALLED.get("execution"), _INSTALLED.get("stock"), _INSTALLED.get("hooked")
        if execution is not None and stock is not None and getattr(execution, "IsChangedCache", None) is hooked:
            execution.IsChangedCache = stock
        _INSTALLED.clear()
        _STATUS.clear()
        _STATUS["state"] = STATE_ABSENT
        _FAULT_LOGGED = False
        _EARLY_RELEASE_SKIP_LOGGED = False


def reset_for_tests() -> None:
    uninstall_for_tests()
