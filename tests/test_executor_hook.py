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

# Install: idempotent, not stacked.
first = hook.install(execution)
hooked = execution.IsChangedCache
second = hook.install(execution)
out["install"] = {"first": first, "second": second, "same_class": execution.IsChangedCache is hooked,
                  "subclass": issubclass(hooked, stock), "status": hook.status()}

calls = []
real_prepare = pipeline.prepare
dyn = DynamicPrompt({"1": {"class_type": "SaveImage", "inputs": {}}})


def spy(runtime, prompt_id, dynprompt):
    calls.append(prompt_id)
    return {}


pipeline.prepare = spy
hooked("p-null", dyn, execution.NullCache())
hooked("p-real", dyn, execution.HierarchicalCache(execution.CacheKeySetInputSignature))
out["null_cache_bypass"] = list(calls)

# The guard: a pipeline fault and an interrupt outside the Editor never escape.
escaped = []
for kind in ("fault", "interrupt"):
    def broken(runtime, prompt_id, dynprompt, kind=kind):
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
def exiting(runtime, prompt_id, dynprompt):
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
