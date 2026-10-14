"""Uncached CPU regression probe for the real ComfyUI validation/execution boundary."""
import asyncio
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


nodes.NODE_CLASS_MAPPINGS.update(lazy.LAZY_NODE_CLASS_MAPPINGS)
nodes.NODE_CLASS_MAPPINGS.update({"SonderTestSource": Source, "SonderTestSelection": Selection, "SonderTestSink": Sink})


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


async def run_case(name, prompt, expected_sources=(), expected_outputs=None, error=None, switch=False):
    executed.clear()
    sunk.clear()
    valid, validation_error, outputs, node_errors = await execution.validate_prompt(name, prompt, None)
    executor = None
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
    print("SONDER_LAZY_REPORT=" + json.dumps({"core": __version__, "requires_lazy": requires_lazy, "cases": cases}))
    return 0 if all(case["passed"] for case in cases) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
