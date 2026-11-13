"""Uncached CPU regression probe for the real ComfyUI validation/execution boundary."""
import asyncio
import copy
import importlib
import json
from pathlib import Path
import re
import sys
import types
from types import SimpleNamespace
from unittest.mock import Mock

comfy, repo = sys.argv[1:]
sys.argv = [sys.argv[0], "--cpu"]
sys.path.insert(0, comfy)

import execution
import nodes
from comfy_api.latest import io
from comfyui_version import __version__

# Unknown runtime versions fail explicitly instead of silently relaxing laziness.
match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:$|[-+])", __version__)
assert match, f"Unrecognized ComfyUI version: {__version__}"
requires_lazy = tuple(map(int, match.groups())) >= (0, 40, 0)
pkg = types.ModuleType("sonder_executor_testpkg")
pkg.__path__ = [str(Path(repo))]
sys.modules[pkg.__name__] = pkg
lazy = importlib.import_module(f"{pkg.__name__}.nodes.lazy_switches")
prompt_once = importlib.import_module(f"{pkg.__name__}.nodes.prompt_once")
sdk_runs = importlib.import_module(f"{pkg.__name__}.server.sdk_runs")
executed, sunk = [], {}


class Source(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="SonderTestSource", inputs=[
            io.Int.Input("value"), io.Boolean.Input("tuple_payload", default=False),
        ], outputs=[io.AnyType.Output()])

    @classmethod
    def execute(cls, value, tuple_payload=False):
        executed.append(value)
        return io.NodeOutput((None, "branches.item1") if tuple_payload else value)


class Selection(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="SonderTestSelection", inputs=[io.Int.Input("value")], outputs=[io.Int.Output()])

    @classmethod
    def execute(cls, value):
        return io.NodeOutput(value)


class Sink(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="SonderTestSink", inputs=[
            io.AnyType.Input("value"), io.String.Input("name"),
        ], outputs=[], is_output_node=True)

    @classmethod
    def execute(cls, value, name):
        sunk[name] = value
        return io.NodeOutput()


class OnceSource:
    """A source with one-shot side effects, guarded like the Editor and the save nodes."""

    RETURN_TYPES = ("INT",)
    FUNCTION = "run"
    CATEGORY = "testing"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("INT", {"default": 0})},
                "hidden": {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"}}

    def run(self, value, prompt=None, unique_id=None):
        def body():
            executed.append(value)
            return (value,)
        return prompt_once.run_once_per_prompt(prompt, unique_id, "SonderTestOnce", body)


class NanSource:
    """A branch source that never caches: the Editor's own mechanism, a V1 NaN ``IS_CHANGED``."""

    RETURN_TYPES = ("INT",)
    FUNCTION = "run"
    CATEGORY = "testing"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("INT", {"default": 0})}}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def run(self, value):
        executed.append(value)
        return (value,)


nodes.NODE_CLASS_MAPPINGS.update(lazy.LAZY_NODE_CLASS_MAPPINGS)
nodes.NODE_CLASS_MAPPINGS.update({"SonderTestSource": Source, "SonderTestSelection": Selection,
                                  "SonderTestSink": Sink, "SonderTestOnce": OnceSource,
                                  "SonderTestNanSource": NanSource})


def source(value, tuple_payload=False):
    return {"class_type": "SonderTestSource", "inputs": {"value": value, "tuple_payload": tuple_payload}}


def sink(slot=0, name="result"):
    return {"class_type": "SonderTestSink", "inputs": {"value": ["subject", slot], "name": name}}


def switch_prompt(selected, linked=False, indices=(0, 1, 2), tuple_payload=False):
    prompt = {f"s{i}": source(i, tuple_payload) for i in indices}
    inputs = {f"branches.item{i}": [f"s{i}", 0] for i in indices}
    if linked:
        prompt["selection"] = {"class_type": "SonderTestSelection", "inputs": {"value": selected}}
    inputs["select"] = ["selection", 0] if linked else selected
    prompt["subject"] = {"class_type": "SonderLazySwitch", "inputs": inputs}
    prompt["sink"] = sink()
    return prompt


def repeat_prompt(class_type):
    """A shared upstream node read eagerly (the Cluster's select) and lazily (its branch).

    Under the null cache the lazy request re-stages the finished upstream node, the
    shape that re-ran the Editor in Sonder App takes (2026-10-21).
    """
    upstream = ({"class_type": class_type, "inputs": {"value": 0}} if class_type == "SonderTestOnce"
                else source(0))
    return {
        "shared": upstream,
        "selection": {"class_type": "SonderTestSelection", "inputs": {"value": ["shared", 0]}},
        "dormant": source(9),
        "subject": {"class_type": "SonderLazyCluster", "inputs": {
            "select": ["selection", 0], "branches": 2, "lanes": 1,
            "b0_l0": ["shared", 0], "b1_l0": ["dormant", 0]}},
        "sink": sink(),
    }


async def run_case(name, prompt, expected_sources=(), expected_outputs=None, error=None, switch=False,
                   queue_key=""):
    executed.clear()
    sunk.clear()
    sdk_runs.reset_for_tests()
    valid, validation_error, outputs, node_errors = await execution.validate_prompt(name, prompt, None)
    executor = None
    if valid and queue_key:
        # What the prompt-queue hook does when ComfyUI takes the prompt off its queue.
        sdk_runs.open_release(queue_key, name, prompt)
    if valid:
        executor = execution.PromptExecutor(
            SimpleNamespace(client_id=None, send_sync=Mock(), last_node_id=None),
            cache_type=execution.CacheType.NONE, cache_args={"ram": 0, "ram_inactive": 0},
        )
        await executor.execute_async(prompt, name, execute_outputs=outputs)
    messages = executor.status_messages if executor else []
    outcome = {"name": name, "valid": valid, "sources": sorted(executed), "outputs": dict(sunk)}
    try:
        if error == "validation":
            assert not valid and node_errors, (validation_error, node_errors)
            assert "subject" in node_errors, node_errors
            assert not executed and not sunk
        elif error == "execution":
            assert valid and executor is not None and not executor.success, (validation_error, messages)
            failures = [detail for kind, detail in messages if kind == "execution_error"]
            assert failures and failures[0]["node_id"] == "subject", messages
            assert "Selected branch" in failures[0]["exception_message"], failures
            assert not sunk
            if requires_lazy:
                assert not executed, executed
        else:
            assert valid and executor is not None and executor.success, (validation_error, node_errors, messages)
            assert sunk == expected_outputs, (sunk, expected_outputs)
            if requires_lazy or not switch:
                assert sorted(executed) == sorted(expected_sources), (executed, expected_sources)
            else:
                # Old-core routing is checked, but its known eager scheduler is not patched here.
                assert set(expected_sources).issubset(executed), (executed, expected_sources)
        outcome["passed"] = True
    except AssertionError as exc:
        outcome.update(passed=False, failure=str(exc))
    return outcome


def prediction_states():
    """(name, router class type, deciding literals, lazy inputs) the scheduler must agree with."""
    for select in (0, 2):
        yield f"predict-switch-{select}", "SonderLazySwitch", {"select": select}, \
            [f"branches.item{i}" for i in range(3)]
    for select, lanes in ((0, 1), (1, 2), (2, 2)):
        yield f"predict-cluster-{select}-{lanes}", "SonderLazyCluster", \
            {"select": select, "branches": 3, "lanes": lanes}, [f"b{b}_l{lane}" for b in range(3) for lane in range(2)]
    for label, whens in (("open-closed", {"when_A": True, "when_B": 0}), ("closed-open", {"when_A": False, "when_B": "x"}),
                         ("unwired", {})):
        yield f"predict-gate-{label}", "SonderGate", whens, ["value_A", "value_B"]


async def prediction_cases():
    """The cache pipeline's prediction (``requested_lazy_inputs`` with every linked lazy
    input connected) equals what the real scheduler runs, on a cold and on a warm cache.
    Every branch source is NaN-fingerprinted, as anything downstream of the Editor is.
    The second run also shares one source with an eager consumer, so it is evaluated
    before the router asks; the rule still decides each input alone."""
    results = []
    executor = execution.PromptExecutor(
        SimpleNamespace(client_id=None, send_sync=Mock(), last_node_id=None),
        cache_type=execution.CacheType.CLASSIC, cache_args={"ram": 0, "ram_inactive": 0})
    for name, class_type, deciding, names in prediction_states():
        cls = nodes.NODE_CLASS_MAPPINGS[class_type]
        predicted = cls.requested_lazy_inputs(deciding, frozenset(names))
        prompt = {f"src{index}": {"class_type": "SonderTestNanSource", "inputs": {"value": index}}
                  for index in range(len(names))}
        prompt["subject"] = {"class_type": class_type,
                             "inputs": dict(deciding, **{lazy_name: [f"src{index}", 0]
                                                         for index, lazy_name in enumerate(names)})}
        prompt["sink"] = sink()
        prompt["eager"] = {"class_type": "SonderTestSink", "inputs": {"value": ["src0", 0], "name": "eager"}}
        expected = sorted(names.index(lazy_name) for lazy_name in predicted)
        outcome = {"name": name, "predicted": predicted}
        try:
            for attempt, with_eager in (("cold", False), ("warm", False), ("shared", True)):
                executed.clear()
                sunk.clear()
                # A fresh prompt per run, as from the queue: ComfyUI writes ``is_changed``
                # into the node dicts it evaluates (execution.py IsChangedCache.get).
                run = copy.deepcopy({key: value for key, value in prompt.items() if with_eager or key != "eager"})
                valid, error, outputs, node_errors = await execution.validate_prompt(f"{name}-{attempt}", run, None)
                assert valid, (error, node_errors)
                await executor.execute_async(run, f"{name}-{attempt}", execute_outputs=outputs)
                assert executor.success, executor.status_messages
                ran = sorted(set(executed))
                wanted = sorted(set(expected) | ({0} if with_eager else set()))
                assert ran == wanted, (attempt, ran, wanted)
            outcome["passed"] = True
        except AssertionError as exc:
            outcome.update(passed=False, failure=str(exc))
        results.append(outcome)
    return results


async def main():
    cases = []
    for linked in (False, True):
        for selected in (0, 1, 2, 7, 31):
            indices = (0, 1, 2) if selected < 3 else (0, selected)
            cases.append(await run_case(
                f"switch-{'linked' if linked else 'literal'}-{selected}",
                switch_prompt(selected, linked, indices), [selected], {"result": selected}, switch=True,
            ))
        for selected, indices, label in ((1, (0, 2), "missing"), (-1, (0, 1), "negative"), (32, (0, 1), "out-of-range")):
            cases.append(await run_case(
                f"switch-{'linked' if linked else 'literal'}-{label}",
                switch_prompt(selected, linked, indices), error="execution" if linked else "validation", switch=True,
            ))
    cases.append(await run_case("switch-tuple", switch_prompt(1, tuple_payload=True), [1],
                                {"result": (None, "branches.item1")}, switch=True))

    for selected in (0, 1, 2):
        prompt = {f"s{i}": source(i) for i in range(6)}
        inputs = {f"b{b}_l{lane}": [f"s{b * 2 + lane}", 0] for b in range(3) for lane in range(2)}
        # An inactive lane on the selected branch must stay dormant as well.
        prompt["inactive"] = source(99)
        inputs[f"b{selected}_l2"] = ["inactive", 0]
        inputs.update(select=selected, branches=3, lanes=2)
        prompt["subject"] = {"class_type": "SonderLazyCluster", "inputs": inputs}
        prompt.update(sinkA=sink(0, "A"), sinkB=sink(1, "B"))
        cases.append(await run_case(f"cluster-{selected}", prompt, [selected * 2, selected * 2 + 1],
                                    {"A": selected * 2, "B": selected * 2 + 1}))

    for condition in (True, False, None):
        prompt = {"s0": source(0), "s1": source(1)}
        inputs = {"value_A": ["s0", 0], "value_B": ["s1", 0], "when_B": False}
        if condition is not None:
            inputs["when_A"] = condition
        prompt["subject"] = {"class_type": "SonderGate", "inputs": inputs}
        prompt.update(sinkA=sink(0, "A"), sinkB=sink(1, "B"))
        cases.append(await run_case(f"gate-{condition}", prompt, [0] if condition else [],
                                    {"A": 0 if condition else None, "B": None}))

    # Characterization: the null cache runs a finished upstream node again when a lazy
    # branch pulls it in. If ComfyUI stops doing this, the guard below is moot.
    cases.append(await run_case("repeat-unguarded", repeat_prompt("SonderTestSource"), [0, 0], {"result": 0}))
    # The guard: one execution per prompt, same output, once the queue hook opened the run.
    cases.append(await run_case("repeat-guarded", repeat_prompt("SonderTestOnce"), [0], {"result": 0},
                                queue_key="77"))
    # Without a queue run (no trusted key) the guard stays out of the way.
    cases.append(await run_case("repeat-untrusted", repeat_prompt("SonderTestOnce"), [0, 0], {"result": 0}))
    cases.extend(await prediction_cases())
    print("SONDER_LAZY_REPORT=" + json.dumps({"core": __version__, "requires_lazy": requires_lazy, "cases": cases}))
    return 0 if all(case["passed"] for case in cases) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
