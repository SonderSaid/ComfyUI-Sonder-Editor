"""The cache pipeline's executor hook against ComfyUI's real ``execution`` module.

The shape gate must pass on the installed source and fail on each mutation it
exists to catch; install must be idempotent and never stack on a foreign
replacement; the guard must never let a pipeline fault or ComfyUI's interrupt
escape, and must re-arm the interrupt; ``NullCache`` bypasses the pipeline.

ComfyUI's ``nodes`` and ``execution`` collide with this repository's packages,
so the checks run in a subprocess from the ComfyUI root (``SONDER_COMFYUI_ROOT``
or the install whose venv runs this test), and skip without one.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _comfy_root() -> Path | None:
    candidates = []
    if os.environ.get("SONDER_COMFYUI_ROOT"):
        candidates.append(Path(os.environ["SONDER_COMFYUI_ROOT"]))
    candidates.extend(Path(sys.executable).resolve().parents[:3])
    for root in candidates:
        if (root / "execution.py").is_file() and (root / "comfy_api").is_dir():
            return root
    return None


SCRIPT = r'''
import importlib, json, sys, tempfile, textwrap, types
repo, comfy_root = sys.argv[1], sys.argv[2]
sys.path.insert(0, comfy_root)
import execution
import comfy.model_management as mm
from comfy_execution.graph import DynamicPrompt

package = types.ModuleType("sonder_hook_pkg")
package.__path__ = [repo]
sys.modules["sonder_hook_pkg"] = package
hook = importlib.import_module("sonder_hook_pkg.server.executor_hook")
pipeline = importlib.import_module("sonder_hook_pkg.server.cache_pipeline")
out = {}
stock = execution.IsChangedCache

out["gate_installed"] = hook.check_shape(execution)


def fake(**overrides):
    module = types.ModuleType("execution")
    module.__dict__.update(execution.__dict__)
    module.__dict__.update(overrides)
    return module


def from_source(name, source):
    path = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=tempfile.gettempdir())
    path.write(textwrap.dedent(source))
    path.close()
    spec = importlib.util.spec_from_file_location(f"mutant_{name}", path.name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reordered = from_source("order", """
    class PromptExecutor:
        async def execute_async(self, prompt, prompt_id, extra_data={}, execute_outputs=[]):
            nodes.interrupt_processing(False)
            self.server.client_id = extra_data["client_id"]
            is_changed_cache = IsChangedCache(prompt_id, dynamic_prompt, self.caches.outputs)
            add_progress_handler(WebUIProgressHandler(self.server))
            await cache.set_prompt(dynamic_prompt, prompt.keys(), is_changed_cache)
""")
signature = from_source("signature", """
    class IsChangedCache:
        def __init__(self, prompt_id, dynprompt):
            pass
""")
signature.IsChangedCache.__module__ = "execution"
optional = from_source("optional", """
    class IsChangedCache:
        def __init__(self, prompt_id, dynprompt, outputs_cache, extra=None):
            pass
""")
optional.IsChangedCache.__module__ = "execution"
foreign = from_source("foreign", """
    class IsChangedCache:
        def __init__(self, prompt_id, dynprompt, outputs_cache):
            pass
""")
input_data = from_source("input_data", """
    def get_input_data(inputs, class_def, unique_id, execution_list=None, extra_data={}):
        return input_data_all, missing_keys, v3_data
""")
no_override = type("DynamicPrompt", (), {k: v for k, v in vars(DynamicPrompt).items() if k != "override_node"})
context = from_source("context", """
    class CurrentNodeContext:
        def __init__(self, node_id, list_index=None):
            pass
""")
mutants = {
    "order": fake(PromptExecutor=reordered.PromptExecutor),
    "signature": fake(IsChangedCache=signature.IsChangedCache),
    "optional_param": fake(IsChangedCache=optional.IsChangedCache),
    "foreign": fake(IsChangedCache=foreign.IsChangedCache),
    "get_input_data": fake(get_input_data=input_data.get_input_data),
    "override_node": fake(DynamicPrompt=no_override),
    "context": fake(CurrentNodeContext=context.CurrentNodeContext),
    "null_cache": fake(NullCache=None),
}
out["gate_mutants"] = {name: hook.check_shape(module) for name, module in mutants.items()}

# The V3 call gate: passes on the installed source, names each mutation, and only
# a passing gate hands bridge preparation the V3 call.
out["v3_gate_installed"] = hook.check_v3_call(execution)
mapper_source = textwrap.dedent(hook._source(execution._async_map_node_over_list))
no_clone = from_source("no_clone", mapper_source.replace(
    "class_clone = obj.PREPARE_CLASS_CLONE(v3_data)", "class_clone = obj"))
# Stock passes an instance: a change to that branch alone must fail the gate too.
no_instance_clone = from_source("no_instance_clone", mapper_source.replace(
    "class_clone = type_obj.PREPARE_CLASS_CLONE(v3_data)", "class_clone = type_obj"))
v3_mutants = {
    "no_clone": fake(_async_map_node_over_list=no_clone._async_map_node_over_list),
    "no_instance_clone": fake(_async_map_node_over_list=no_instance_clone._async_map_node_over_list),
    "locked": fake(make_locked_method_func=lambda type_obj, func: None),
    "nested": fake(_io=types.SimpleNamespace(build_nested_inputs=None)),
    "output": fake(_NodeOutputInternal=None),
}
out["v3_gate_mutants"] = {name: hook.check_v3_call(module) for name, module in v3_mutants.items()}
out["v3_runtime_fields"] = sorted(hook._v3_runtime(execution))
out["v3_runtime_mutant"] = sorted(hook._v3_runtime(v3_mutants["output"]))

# The identity gate (Phase 3): a bridge finds its run through the same executing
# context the executor sets; another CurrentNodeContext turns identity off.
out["identity_gate_installed"] = hook.check_identity_context(execution)
out["identity_gate_mutant"] = hook.check_identity_context(fake(CurrentNodeContext=context.CurrentNodeContext))
out["identity_runtime"] = sorted(hook._identity_runtime(execution))
out["identity_runtime_mutant"] = sorted(hook._identity_runtime(fake(CurrentNodeContext=context.CurrentNodeContext)))

# The carrier gate (Phase 4a): passes on the installed source, names each mutation.
from comfy_execution import caching
out["carrier_gate_installed"] = hook.check_carrier(execution, stock)
no_loop = from_source("no_loop", """
    class PromptExecutor:
        async def execute_async(self, prompt, prompt_id, extra_data={}, execute_outputs=[]):
            is_changed_cache = IsChangedCache(prompt_id, dynamic_prompt, self.caches.outputs)
            await self.caches.outputs.set_prompt(dynamic_prompt, prompt.keys(), is_changed_cache)
""")
cache_set = from_source("cache_set", """
    class CacheSet:
        def __init__(self, cache_type=None, cache_args={}):
            self.all = [self.objects, self.outputs]
""")
real_execute = from_source("real_execute", """
    async def execute(server, dynprompt, caches, current_item, *rest):
        real_node_id = current_item
""")
validate = from_source("validate", """
    async def validate_prompt(prompt_id, prompt, partial_execution_list):
        return (True, None, [], {})
""")
no_memo = from_source("no_memo", """
    class IsChangedCache:
        def __init__(self, prompt_id, dynprompt, outputs_cache):
            self.memo = {}
""")
import collections
no_real = type("DynamicPrompt", (), {k: v for k, v in vars(DynamicPrompt).items() if k != "get_real_node_id"})
carrier_mutants = {
    "order": hook.check_carrier(fake(PromptExecutor=no_loop.PromptExecutor), stock),
    "cache_set": hook.check_carrier(fake(CacheSet=cache_set.CacheSet), stock),
    "entry": hook.check_carrier(fake(CacheEntry=collections.namedtuple("CacheEntry", "outputs ui")), stock),
    "real_node_id": hook.check_carrier(fake(execute=real_execute.execute), stock),
    "dynprompt": hook.check_carrier(fake(DynamicPrompt=no_real), stock),
    "is_changed": hook.check_carrier(execution, no_memo.IsChangedCache),
    "validate": hook.check_carrier(fake(validate_prompt=validate.validate_prompt), stock),
}
real_set_prompt = caching.BasicCache.set_prompt


set_prompt_mutants = from_source("set_prompt_mutants", """
    async def reordered_set_prompt(self, dynprompt, node_ids, is_changed_cache):
        await self.cache_key_set.add_keys(node_ids)
        self.cache_key_set = self.key_class(dynprompt, node_ids, is_changed_cache)

    async def lru_without_super(self, dynprompt, node_ids, is_changed_cache):
        self.generation += 1
""")
caching.BasicCache.set_prompt = set_prompt_mutants.reordered_set_prompt
carrier_mutants["set_prompt"] = hook.check_carrier(execution, stock)
caching.BasicCache.set_prompt = real_set_prompt
real_lru = caching.LRUCache.set_prompt


caching.LRUCache.set_prompt = set_prompt_mutants.lru_without_super
carrier_mutants["lru"] = hook.check_carrier(execution, stock)
caching.LRUCache.set_prompt = real_lru

# The rest of the gate's tokens, one mutant each: patched on the real classes, then restored.
more = from_source("more_mutants", """
    async def set_prompt_extra(self, dynprompt, node_ids, is_changed_cache, extra=None):
        self.cache_key_set = self.key_class(dynprompt, node_ids, is_changed_cache)
        await self.cache_key_set.add_keys(node_ids)

    async def own_set_prompt(self, dynprompt, node_ids, is_changed_cache):
        self.cache_key_set = self.key_class(dynprompt, node_ids, is_changed_cache)
        await self.cache_key_set.add_keys(node_ids)

    async def ram_without_super(self, dynprompt, node_ids, is_changed_cache):
        self.active_evictions = False

    def set_local_renamed(self, key, value):
        pass

    async def add_keys_unchecked(self, node_ids):
        for node_id in node_ids:
            self.keys[node_id] = None

    def get_data_key_other(self, node_id):
        return self.keys[node_id]

    class NoEphemeral:
        def __init__(self, original_prompt):
            self.original_prompt = original_prompt
            self.node_overrides = {}
            self.ephemeral_display = {}

        def has_node(self, node_id):
            return node_id in self.original_prompt

        def get_display_node_id(self, node_id):
            return node_id

        def get_real_node_id(self, node_id):
            return node_id

    async def execute_unique_id(server, dynprompt, caches, current_item, *rest):
        real_node_id = dynprompt.get_real_node_id(unique_id)
        error_details = {"node_id": unique_id}
        error_details = {"node_id": unique_id}

    class Handler:
        def handle_execution_error(self, prompt_id, prompt, current_outputs, executed, error, ex):
            node_id = error["node_id"]
            class_type = "unknown"
""")


def patched(owner, name, value, **gate):
    original = owner.__dict__.get(name, None)
    had = name in owner.__dict__
    setattr(owner, name, value)
    try:
        return hook.check_carrier(gate.get("module", execution), stock)
    finally:
        if had:
            setattr(owner, name, original)
        else:
            delattr(owner, name)


carrier_mutants["set_prompt_signature"] = patched(caching.BasicCache, "set_prompt", more.set_prompt_extra)
carrier_mutants["hierarchical_override"] = patched(caching.HierarchicalCache, "set_prompt", more.own_set_prompt)
carrier_mutants["ram"] = patched(caching.RAMPressureCache, "set_prompt", more.ram_without_super)
carrier_mutants["set_local"] = patched(caching.LRUCache, "set_local", more.set_local_renamed)
carrier_mutants["add_keys"] = patched(caching.CacheKeySetInputSignature, "add_keys", more.add_keys_unchecked)
carrier_mutants["get_data_key"] = patched(caching.CacheKeySet, "get_data_key", more.get_data_key_other)
carrier_mutants["dynprompt_init"] = hook.check_carrier(fake(DynamicPrompt=more.NoEphemeral), stock)
carrier_mutants["error_node_id"] = hook.check_carrier(fake(execute=more.execute_unique_id), stock)
handler_executor = type("PromptExecutor", (execution.PromptExecutor,),
                        {"handle_execution_error": more.Handler.handle_execution_error})
carrier_mutants["handler"] = hook.check_carrier(fake(PromptExecutor=handler_executor), stock)
out["carrier_gate_mutants"] = carrier_mutants

# The deferral gate (Phase 4b): passes on the installed source, names each mutation.
out["deferral_gate_installed"] = hook.check_deferral(execution)
deferral = from_source("deferral_mutants", """
    class PromptExecutor:
        async def execute_async(self, prompt, prompt_id, extra_data={}, execute_outputs=[]):
            for cache in self.caches.all:
                cache.clean_unused()
                await cache.set_prompt(dynamic_prompt, prompt.keys(), is_changed_cache)

    def get_local_extra(self, node_id, default=None):
        cache_key = self.cache_key_set.get_data_key(node_id)
        return self.cache[cache_key]

    def get_local_elsewhere(self, node_id):
        cache_key = self.cache_key_set.get_data_key(node_id)
        return self.store.get(cache_key)

    def get_local_unkeyed(self, node_id):
        return self.cache[node_id]

    def hierarchical_get_local(self, node_id):
        return None

    def lru_get_local(self, node_id):
        return None

    def init_without_cache(self, key_class, enable_providers=False):
        self.key_class = key_class
        self.store = {}
""")


def deferral_patched(owner, name, value):
    original = owner.__dict__.get(name, None)
    had = name in owner.__dict__
    setattr(owner, name, value)
    try:
        return hook.check_deferral(execution)
    finally:
        if had:
            setattr(owner, name, original)
        else:
            delattr(owner, name)


out["deferral_gate_mutants"] = {
    "order": hook.check_deferral(fake(PromptExecutor=deferral.PromptExecutor)),
    "signature": deferral_patched(caching.BasicCache, "get_local", deferral.get_local_extra),
    "lookup": deferral_patched(caching.BasicCache, "get_local", deferral.get_local_elsewhere),
    "keyed": deferral_patched(caching.BasicCache, "get_local", deferral.get_local_unkeyed),
    "hierarchical": deferral_patched(caching.HierarchicalCache, "get_local", deferral.hierarchical_get_local),
    "lru": deferral_patched(caching.LRUCache, "get_local", deferral.lru_get_local),
    "ram": deferral_patched(caching.RAMPressureCache, "get_local", deferral.lru_get_local),
    "cache_dict": deferral_patched(caching.BasicCache, "__init__", deferral.init_without_cache),
    "entry": hook.check_deferral(fake(CacheEntry=collections.namedtuple("CacheEntry", "outputs ui"))),
}
# A failed deferral gate keeps the carriers: the carrier runtime still exists, with defer off.
real_get_local = caching.BasicCache.get_local
caching.BasicCache.get_local = deferral.get_local_unkeyed
try:
    runtime_without_deferral = hook._carrier_runtime(execution, stock)
finally:
    caching.BasicCache.get_local = real_get_local
out["deferral_off_keeps_carriers"] = (runtime_without_deferral is not None
                                      and runtime_without_deferral.get("defer") is False)
out["deferral_on"] = bool((hook._carrier_runtime(execution, stock) or {}).get("defer"))

# The early-release gate: passes on the installed source, names each mutation.
out["early_gate_installed"] = hook.check_early_release(execution)
early_mut = from_source("early_mutants", """
    class PromptExecutor:
        async def execute_async(self, prompt, prompt_id, extra_data={}, execute_outputs=[]):
            ram_inactive_headroom = int(self.cache_args["ram_inactive"] * (1024 ** 3))
            ram_release_callback = self.caches.outputs.ram_release if self.cache_type == CacheType.RAM_PRESSURE else None
            for cache in self.caches.all:
                await cache.set_prompt(dynamic_prompt, prompt.keys(), is_changed_cache)
                cache.clean_unused()

    def init_without_args(self, server, cache_type=False, cache_args=None, asset_manager=None):
        self.cache_type = cache_type
        self.reset()

    def init_without_type(self, server, cache_type=False, cache_args=None, asset_manager=None):
        self.cache_args = cache_args
        self.reset()

    def ram_release_active_default(self, target, free_active=True, min_entry_size=0):
        for key in list(self.cache):
            if not free_active and self.used_generation[key] == self.generation:
                continue
        return 0

    async def ram_set_prompt_unmarked(self, dynprompt, node_ids, is_changed_cache):
        self.active_evictions = False

    def reset_elsewhere(self):
        self.caches = CacheSet(cache_type=self.cache_type, cache_args={})

    def basic_clean_cache_only(self):
        assert self.initialized
        self._clean_cache()

    def own_clean_unused(self):
        self._clean_cache()
        self._clean_subcaches()

    def lru_clean_everything(self):
        while len(self.cache) > self.max_size:
            self.cache.clear()

    async def lru_set_prompt_unmarked(self, dynprompt, node_ids, is_changed_cache):
        await super().set_prompt(dynprompt, node_ids, is_changed_cache)

    def ram_clean_all(self):
        self._clean_cache()
        self._clean_subcaches()

    def ram_release_short(self, target, free_active=False):
        return 0

    def ram_release_all(self, target, free_active=False, min_entry_size=0):
        for key in list(self.cache):
            del self.cache[key]
        return 0
""")
import enum


def early_patched(owner, name, value):
    had = name in owner.__dict__
    original = owner.__dict__.get(name, None)
    setattr(owner, name, value)
    try:
        return hook.check_early_release(execution)
    finally:
        if had:
            setattr(owner, name, original)
        else:
            delattr(owner, name)


def executor_with(name, value):
    return fake(PromptExecutor=type("PromptExecutor", (execution.PromptExecutor,), {name: value}))


def without_order_token(index):
    """execute_async with every early-release order token except one."""
    kept = [token for at, token in enumerate(hook._EARLY_RELEASE_EXECUTE_ASYNC_ORDER) if at != index]
    body = "\n".join("            " + token for token in kept)
    module = from_source(f"early_order_{index}", (
        "\n    class PromptExecutor:\n"
        "        async def execute_async(self, prompt, prompt_id, extra_data={}, execute_outputs=[]):\n"
        + body + "\n"))
    return hook.check_early_release(fake(PromptExecutor=module.PromptExecutor))


out["early_order_mutants"] = [without_order_token(index)
                              for index in range(len(hook._EARLY_RELEASE_EXECUTE_ASYNC_ORDER))]
out["early_order_tokens"] = list(hook._EARLY_RELEASE_EXECUTE_ASYNC_ORDER)
out["early_gate_mutants"] = {
    "order": hook.check_early_release(fake(PromptExecutor=early_mut.PromptExecutor)),
    "init": hook.check_early_release(executor_with("__init__", early_mut.init_without_args)),
    "init_type": hook.check_early_release(executor_with("__init__", early_mut.init_without_type)),
    "ram_free_active": early_patched(caching.RAMPressureCache, "ram_release", early_mut.ram_release_active_default),
    "ram_keys": early_patched(caching.RAMPressureCache, "set_prompt", early_mut.ram_set_prompt_unmarked),
    "reset": hook.check_early_release(executor_with("reset", early_mut.reset_elsewhere)),
    "basic_clean": early_patched(caching.BasicCache, "clean_unused", early_mut.basic_clean_cache_only),
    "hierarchical": early_patched(caching.HierarchicalCache, "clean_unused", early_mut.own_clean_unused),
    "lru_clean": early_patched(caching.LRUCache, "clean_unused", early_mut.lru_clean_everything),
    "lru_keys": early_patched(caching.LRUCache, "set_prompt", early_mut.lru_set_prompt_unmarked),
    "ram_clean": early_patched(caching.RAMPressureCache, "clean_unused", early_mut.ram_clean_all),
    "ram_signature": early_patched(caching.RAMPressureCache, "ram_release", early_mut.ram_release_short),
    "ram_current": early_patched(caching.RAMPressureCache, "ram_release", early_mut.ram_release_all),
    "cache_type": hook.check_early_release(fake(CacheType=enum.Enum("CacheType", "CLASSIC LRU NONE"))),
    "interrupted": hook.check_early_release(fake(comfy=types.SimpleNamespace(model_management=types.SimpleNamespace()))),
}
# A failed early-release gate keeps deferral and carriers; a passing one arms the release.
real_ram_release = caching.RAMPressureCache.ram_release
caching.RAMPressureCache.ram_release = early_mut.ram_release_all
try:
    runtime_without_early = hook._carrier_runtime(execution, stock)
finally:
    caching.RAMPressureCache.ram_release = real_ram_release
out["early_off_keeps_deferral"] = (runtime_without_early is not None and runtime_without_early.get("defer") is True
                                   and runtime_without_early.get("early_release") is None)
early = (hook._carrier_runtime(execution, stock) or {}).get("early_release")
out["early_on"] = early is not None and early["code"] is execution.PromptExecutor.execute_async.__code__

# The release: what it calls on each cache type, and never raising.
GIB = 1024 ** 3


def ram_cache(release_result=123, raise_release=False, raise_clean=False):
    cache = caching.RAMPressureCache(execution.CacheKeySetInputSignature)
    calls = []

    def ram_release(*args, **kwargs):
        calls.append([list(args), sorted(kwargs)])
        if raise_release:
            raise RuntimeError("ram_release")
        return release_result

    def clean_unused():
        calls.append("clean_unused")
        if raise_clean:
            raise RuntimeError("clean_unused")

    cache.ram_release, cache.clean_unused = ram_release, clean_unused
    return cache, calls


ram_executor = types.SimpleNamespace(cache_type=execution.CacheType.RAM_PRESSURE, cache_args={"ram_inactive": 2.5})
classic_executor = types.SimpleNamespace(cache_type=execution.CacheType.CLASSIC, cache_args={"ram_inactive": 2.5})
release = {}
cache, calls = ram_cache()
release["ram"] = [hook._release_obsolete(early, cache, ram_executor), calls]
cache, calls = ram_cache()
release["ram_interrupted"] = [hook._release_obsolete(dict(early, interrupted=lambda: True), cache, ram_executor), calls]
cache, calls = ram_cache()
release["ram_no_executor"] = [hook._release_obsolete(early, cache, None), calls]
cache, calls = ram_cache()
release["ram_wrong_type"] = [hook._release_obsolete(early, cache, classic_executor), calls]
cache, calls = ram_cache(raise_release=True)
release["ram_raises"] = [hook._release_obsolete(early, cache, ram_executor), calls]
cache, calls = ram_cache(raise_clean=True)
release["clean_raises"] = [hook._release_obsolete(early, cache, ram_executor), calls]
classic_cache = execution.HierarchicalCache(execution.CacheKeySetInputSignature)
classic_cache.cache.update(old=1, kept=2)
classic_cache.clean_unused = lambda: classic_cache.cache.pop("old")
release["classic"] = [hook._release_obsolete(early, classic_cache, classic_executor), sorted(classic_cache.cache)]
lru_cache = execution.LRUCache(execution.CacheKeySetInputSignature, 2)
lru_calls = []
lru_cache.clean_unused = lambda: lru_calls.append("clean_unused")
lru_cache.ram_release = lambda *a, **k: lru_calls.append("ram_release")
release["lru"] = [hook._release_obsolete(early, lru_cache, ram_executor), lru_calls]
out["early_release"] = release

# The executor is read only from a frame running the stock execute_async for this outputs cache.
owner = types.SimpleNamespace(caches=types.SimpleNamespace(outputs=classic_cache))
frame = lambda code, owner: types.SimpleNamespace(f_code=code, f_locals={"self": owner})  # noqa: E731
out["awaiting_executor"] = {
    "match": hook._awaiting_executor(early, frame(early["code"], owner), classic_cache) is owner,
    "other_code": hook._awaiting_executor(early, frame(hook.check_shape.__code__, owner), classic_cache),
    "other_cache": hook._awaiting_executor(early, frame(early["code"], owner), lru_cache),
    "no_frame": hook._awaiting_executor(early, None, classic_cache),
    "no_self": hook._awaiting_executor(early, types.SimpleNamespace(f_code=early["code"], f_locals={}), classic_cache),
}

# Per-prompt preconditions: the outputs cache must be a stock type, unarmed, with the carrier registered.
import nodes as comfy_nodes
carrier_runtime = hook._carrier_runtime(execution, stock)
carrier_module = importlib.import_module("sonder_hook_pkg.nodes.media_carrier")
classic = execution.HierarchicalCache(execution.CacheKeySetInputSignature)
preconditions = {"unregistered": hook._carrier_precondition(carrier_runtime, classic)}
comfy_nodes.NODE_CLASS_MAPPINGS["SonderMediaCarrier"] = carrier_module.SonderMediaCarrier
preconditions["classic"] = hook._carrier_precondition(carrier_runtime, classic)
preconditions["lru"] = hook._carrier_precondition(carrier_runtime, execution.LRUCache(execution.CacheKeySetInputSignature, 4))
preconditions["ram"] = hook._carrier_precondition(carrier_runtime, caching.RAMPressureCache(execution.CacheKeySetInputSignature))
preconditions["id_keys"] = hook._carrier_precondition(carrier_runtime, execution.HierarchicalCache(caching.CacheKeySetID))
subclass = type("Sub", (execution.HierarchicalCache,), {})
preconditions["subclass"] = hook._carrier_precondition(carrier_runtime, subclass(execution.CacheKeySetInputSignature))
armed = execution.HierarchicalCache(execution.CacheKeySetInputSignature)
armed.set_prompt = lambda *args: None
preconditions["armed"] = hook._carrier_precondition(carrier_runtime, armed)
preconditions["no_gate"] = hook._carrier_precondition(None, classic)
comfy_nodes.NODE_CLASS_MAPPINGS["SonderMediaCarrier"] = object
preconditions["foreign_class"] = hook._carrier_precondition(carrier_runtime, classic)
comfy_nodes.NODE_CLASS_MAPPINGS.pop("SonderMediaCarrier", None)
out["carrier_preconditions"] = preconditions

# Install: idempotent, not stacked.
first = hook.install(execution)
hooked = execution.IsChangedCache
second = hook.install(execution)
out["install"] = {"first": first, "second": second, "same_class": execution.IsChangedCache is hooked,
                  "subclass": issubclass(hooked, stock), "status": hook.status()}

calls = []
real_prepare = pipeline.prepare
dyn = DynamicPrompt({"1": {"class_type": "SaveImage", "inputs": {}}})


def spy(runtime, prompt_id, dynprompt, *media):
    calls.append(prompt_id)
    return {}


pipeline.prepare = spy
hooked("p-null", dyn, execution.NullCache())
hooked("p-real", dyn, execution.HierarchicalCache(execution.CacheKeySetInputSignature))
out["null_cache_bypass"] = list(calls)

# The guard: a pipeline fault and an interrupt outside the Editor never escape.
escaped = []
for kind in ("fault", "interrupt"):
    def broken(runtime, prompt_id, dynprompt, *media, kind=kind):
        if kind == "fault":
            raise ValueError("pipeline bug")
        raise mm.InterruptProcessingException()

    pipeline.prepare = broken
    mm.interrupt_current_processing(False)
    try:
        instance = hooked("p-" + kind, dyn, execution.HierarchicalCache(execution.CacheKeySetInputSignature))
        escaped.append([kind, None, instance.dynprompt is dyn])
    except BaseException as exc:
        escaped.append([kind, type(exc).__name__, False])
    out["rearmed_" + kind] = mm.processing_interrupted()
mm.interrupt_current_processing(False)
out["guard"] = escaped

# A changed constructor call reaches the stock class unchanged, never a
# TypeError from the subclass (which would sit outside the guard).
pipeline.prepare = spy
calls.clear()
try:
    hooked("p-4", dyn, execution.HierarchicalCache(execution.CacheKeySetInputSignature), "extra")
    out["four_args"] = "accepted"
except TypeError as exc:
    out["four_args"] = "stock TypeError" if "positional" in str(exc) else repr(exc)
out["four_args_prepared"] = list(calls)

# Another BaseException still propagates as in stock.
def exiting(runtime, prompt_id, dynprompt, *media):
    raise KeyboardInterrupt()

pipeline.prepare = exiting
try:
    hooked("p-exit", dyn, execution.HierarchicalCache(execution.CacheKeySetInputSignature))
    out["keyboard_interrupt"] = "swallowed"
except KeyboardInterrupt:
    out["keyboard_interrupt"] = "propagated"
pipeline.prepare = real_prepare

# A later restore reads as degraded; a foreign replacement is never stacked on.
execution.IsChangedCache = stock
out["status_after_restore"] = hook.status()["state"]
hook.uninstall_for_tests()
execution.IsChangedCache = foreign.IsChangedCache
out["install_over_foreign"] = hook.install(execution)
execution.IsChangedCache = stock
hook.uninstall_for_tests()
out["restored_stock"] = execution.IsChangedCache is stock
print("HOOK_RESULT " + json.dumps(out))
'''


@pytest.fixture(scope="module")
def result():
    comfy_root = _comfy_root()
    if comfy_root is None:
        pytest.skip("needs a ComfyUI install (SONDER_COMFYUI_ROOT or the test install's venv)")
    completed = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(REPO_ROOT), str(comfy_root)],
        cwd=str(comfy_root), capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    line = next((item for item in completed.stdout.splitlines() if item.startswith("HOOK_RESULT ")), None)
    assert line is not None, completed.stderr[-4000:]
    return json.loads(line[len("HOOK_RESULT "):])


def test_the_gate_passes_on_the_installed_comfyui(result):
    assert result["gate_installed"] is None


def test_the_gate_names_each_mutation(result):
    assert result["gate_mutants"] == {
        # The handler now follows IsChangedCache, so no IsChangedCache( follows it.
        "order": "execute_async order: IsChangedCache(",
        "signature": "IsChangedCache.__init__ signature",
        "optional_param": "IsChangedCache.__init__ signature",
        "foreign": "foreign IsChangedCache",
        "get_input_data": "get_input_data signature",
        "override_node": "DynamicPrompt.override_node",
        "context": "CurrentNodeContext signature",
        "null_cache": "NullCache",
    }


def test_the_identity_gate_passes_installed_and_fails_on_another_context(result):
    assert result["identity_gate_installed"] is None
    assert result["identity_gate_mutant"] == "CurrentNodeContext is not comfy_execution.utils'"
    assert result["identity_runtime"] == ["identity"]
    assert result["identity_runtime_mutant"] == []


def test_the_v3_call_gate_passes_installed_and_names_each_mutation(result):
    assert result["v3_gate_installed"] is None
    assert result["v3_gate_mutants"] == {
        "no_clone": "V3 call order: class_clone = obj.PREPARE_CLASS_CLONE(v3_data)",
        "no_instance_clone": "V3 call order: class_clone = type_obj.PREPARE_CLASS_CLONE(v3_data)",
        "locked": "make_locked_method_func signature",
        "nested": "build_nested_inputs signature",
        "output": "_NodeOutputInternal",
    }
    # Only a passing gate gives bridge preparation the V3 call; telling V3 from V1
    # needs only ComfyUI's V3 base, so a failed gate never makes a V3 class look V1.
    assert result["v3_runtime_fields"] == ["call_v3", "is_v3", "node_output_type", "own_lazy_status"]
    assert result["v3_runtime_mutant"] == ["is_v3"]


def test_the_carrier_gate_passes_installed_and_names_each_mutation(result):
    assert result["carrier_gate_installed"] is None
    assert result["carrier_gate_mutants"] == {
        "order": "carrier execute_async order: for cache in self.caches.all:",
        "cache_set": "CacheSet.all order",
        "entry": "CacheEntry fields",
        "real_node_id": "execute real_node_id",
        "dynprompt": "DynamicPrompt.get_real_node_id",
        "is_changed": "IsChangedCache.is_changed",
        "validate": "validate_prompt validates submitted outputs",
        "set_prompt": "BasicCache.set_prompt keys",
        "lru": "LRUCache.set_prompt",
        "set_prompt_signature": "BasicCache.set_prompt signature",
        "hierarchical_override": "HierarchicalCache.set_prompt overridden",
        "ram": "RAMPressureCache.set_prompt",
        "set_local": "LRUCache.set_local signature",
        "add_keys": "CacheKeySetInputSignature.add_keys",
        "get_data_key": "CacheKeySet.get_data_key",
        "dynprompt_init": "DynamicPrompt.__init__: self.ephemeral_prompt = {}",
        "error_node_id": "execute error node_id",
        "handler": "handle_execution_error lookup",
    }


def test_the_deferral_gate_passes_installed_and_names_each_mutation(result):
    assert result["deferral_gate_installed"] is None
    assert result["deferral_gate_mutants"] == {
        "order": "deferral execute_async order: cache.clean_unused()",
        "signature": "BasicCache.get_local signature",
        "lookup": "BasicCache.get_local: return self.cache[cache_key]",
        "keyed": "BasicCache.get_local: cache_key = self.cache_key_set.get_data_key(node_id)",
        "hierarchical": "HierarchicalCache.get_local",
        "lru": "LRUCache.get_local overridden",
        "ram": "RAMPressureCache.get_local overridden",
        "cache_dict": "BasicCache.cache",
        "entry": "CacheEntry fields",
    }


def test_a_failed_deferral_gate_disables_only_deferral(result):
    assert result["deferral_on"] is True
    assert result["deferral_off_keeps_carriers"] is True


def test_the_early_release_gate_passes_installed_and_names_each_mutation(result):
    assert result["early_gate_installed"] is None
    assert result["early_gate_mutants"] == {
        "order": "early release execute_async order: ram_release_callback(ram_inactive_headroom)",
        "init": "PromptExecutor.__init__: self.cache_args = cache_args",
        "init_type": "PromptExecutor.__init__: self.cache_type = cache_type",
        "ram_free_active": "RAMPressureCache.ram_release free_active default",
        "ram_keys": "RAMPressureCache.set_prompt",
        "reset": "PromptExecutor.reset",
        "basic_clean": "BasicCache.clean_unused",
        "hierarchical": "HierarchicalCache.clean_unused overridden",
        "lru_clean": "LRUCache.clean_unused",
        "lru_keys": "LRUCache.set_prompt",
        "ram_clean": "RAMPressureCache.clean_unused",
        "ram_signature": "RAMPressureCache.ram_release signature",
        "ram_current": "RAMPressureCache.ram_release keeps the current generation",
        "cache_type": "CacheType.RAM_PRESSURE",
        "interrupted": "model_management.processing_interrupted",
    }


def test_the_early_release_gate_names_each_missing_execute_async_token(result):
    tokens = result["early_order_tokens"]
    assert len(tokens) == 5
    assert result["early_order_mutants"] == [f"early release execute_async order: {token}" for token in tokens]


def test_a_failed_early_release_gate_disables_only_the_early_release(result):
    assert result["early_on"] is True
    assert result["early_off_keeps_deferral"] is True


def test_the_early_release_makes_comfyuis_next_calls_and_never_raises(result):
    release = result["early_release"]
    # RAM: clean_unused, then ram_release to the executor's live inactive headroom, free_active left False.
    assert release["ram"] == [{"cleaned": 0, "ram_freed": 123, "ram": "released"},
                              ["clean_unused", [[int(2.5 * 1024 ** 3)], []]]]
    # A pending cancel, an unknown or non-RAM executor: clean_unused only.
    assert release["ram_interrupted"] == [{"cleaned": 0, "ram": "interrupted"}, ["clean_unused"]]
    assert release["ram_no_executor"] == [{"cleaned": 0, "ram": "executor_unknown"}, ["clean_unused"]]
    assert release["ram_wrong_type"] == [{"cleaned": 0, "ram": "executor_unknown"}, ["clean_unused"]]
    # Faults are recorded, never raised; a failed clean still lets the RAM release run.
    assert release["ram_raises"][0] == {"cleaned": 0, "ram": "error", "ram_error": "RuntimeError"}
    assert release["clean_raises"][0] == {"clean_error": "RuntimeError", "ram_freed": 123, "ram": "released"}
    # Classic and LRU: clean_unused only (LRU's own call evicts only generations over capacity).
    assert release["classic"] == [{"cleaned": 1}, ["kept"]]
    assert release["lru"] == [{"cleaned": 0}, ["clean_unused"]]


def test_the_executor_is_read_only_from_the_stock_execute_async_frame_of_this_cache(result):
    assert result["awaiting_executor"] == {"match": True, "other_code": None, "other_cache": None,
                                           "no_frame": None, "no_self": None}


def test_carriers_are_planned_only_on_a_stock_unarmed_cache_with_the_carrier_registered(result):
    assert result["carrier_preconditions"] == {
        "unregistered": "carrier_class_missing", "classic": "", "lru": "", "ram": "", "id_keys": "cache_type",
        "subclass": "cache_type", "armed": "set_prompt_armed", "no_gate": "carrier_gate",
        "foreign_class": "carrier_class_missing",
    }


def test_install_is_idempotent_and_subclasses_the_stock_class(result):
    install = result["install"]
    assert install["first"]["state"] == "ok" and install["second"]["state"] == "ok"
    assert install["same_class"] and install["subclass"]
    assert install["status"]["state"] == "ok"


def test_a_changed_constructor_call_is_passed_to_the_stock_class(result):
    # The stock class itself rejects the extra argument; the hook adds no error
    # of its own and does not prepare a call it does not understand.
    assert result["four_args"] == "stock TypeError"
    assert result["four_args_prepared"] == []


def test_null_cache_bypasses_the_pipeline(result):
    assert result["null_cache_bypass"] == ["p-real"]


def test_a_pipeline_fault_or_interrupt_never_escapes_and_the_interrupt_is_rearmed(result):
    assert result["guard"] == [["fault", None, True], ["interrupt", None, True]]
    assert result["rearmed_fault"] is False
    assert result["rearmed_interrupt"] is True
    assert result["keyboard_interrupt"] == "propagated"


def test_a_restored_or_foreign_class_is_reported_and_never_stacked_on(result):
    assert result["status_after_restore"] == "degraded"
    assert result["install_over_foreign"] == {"state": "unsupported", "reason": "foreign IsChangedCache"}
    assert result["restored_stock"] is True
