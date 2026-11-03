"""Prepared vs unprepared prompts through ComfyUI's real validator and executor (CPU).

Each case validates the compiled prompt as submitted today, then the execution
copy ``server.run_preparation.prepare`` makes, and runs whatever validates.
Model folders point at a temporary folder holding one fixture LoRA, so a
"missing" file is really missing and no real model is ever listed or loaded.
"""
import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
from types import SimpleNamespace
from unittest.mock import Mock

comfy, repo = sys.argv[1:]
sys.argv = [sys.argv[0], "--cpu"]
sys.path.insert(0, comfy)

import execution
import folder_paths
import nodes
from comfy_api.latest import io

fixture = Path(tempfile.mkdtemp(prefix="sonder-prep-"))
(fixture / "present.safetensors").write_bytes(b"")
for name in ("loras", "checkpoints"):
    folder_paths.folder_names_and_paths[name] = ([str(fixture)], {".safetensors"})
folder_paths.filename_list_cache.clear()

pkg = types.ModuleType("sonder_prep_testpkg")
pkg.__path__ = [str(Path(repo))]
sys.modules[pkg.__name__] = pkg
lazy = importlib.import_module(f"{pkg.__name__}.nodes.lazy_switches")
selector = importlib.import_module(f"{pkg.__name__}.nodes.selector")
rp = importlib.import_module(f"{pkg.__name__}.server.run_preparation")
stack_module = importlib.import_module("comfy_extras.nodes_lora_stack")
executed, sunk = [], {}
MODEL = object()


class Source(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="SonderPrepSource", inputs=[io.Int.Input("value")], outputs=[io.AnyType.Output()])

    @classmethod
    def execute(cls, value):
        executed.append(value)
        return io.NodeOutput(value)


class FileUser(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="SonderPrepFile", inputs=[
            io.Combo.Input("lora_name", options=folder_paths.get_filename_list("loras")),
        ], outputs=[io.AnyType.Output()])

    @classmethod
    def execute(cls, lora_name):
        executed.append(lora_name)
        return io.NodeOutput(lora_name)


class ModelSource(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="SonderPrepModel", inputs=[], outputs=[io.Model.Output()])

    @classmethod
    def execute(cls):
        executed.append("model")
        return io.NodeOutput(MODEL)


class Sink(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="SonderPrepSink", inputs=[
            io.AnyType.Input("value"), io.String.Input("name"),
        ], outputs=[], is_output_node=True)

    @classmethod
    def execute(cls, value, name):
        sunk[name] = "MODEL" if value is MODEL else value
        return io.NodeOutput()


nodes.NODE_CLASS_MAPPINGS.update(lazy.LAZY_NODE_CLASS_MAPPINGS)
nodes.NODE_CLASS_MAPPINGS.update({"SonderSelector": selector.SonderSelector,
                                  "SonderPrepSource": Source, "SonderPrepFile": FileUser,
                                  "SonderPrepModel": ModelSource, "SonderPrepSink": Sink,
                                  "LoadLoraModel": stack_module.LoadLoraModel})


def lookup(class_type):
    return nodes.NODE_CLASS_MAPPINGS.get(class_type)


def n(class_type, **inputs):
    return {"class_type": class_type, "inputs": inputs}


def sink(source, name="result", slot=0):
    return n("SonderPrepSink", value=[source, slot], name=name)


def switch(select, bad):
    return {"good": n("SonderPrepSource", value=1), "bad": bad,
            "sw": n("SonderLazySwitch", select=select, **{"branches.item0": ["bad", 0], "branches.item1": ["good", 0]}),
            "out": sink("sw")}


def lora_rows(*rows):
    inputs = {"model": ["m", 0]}
    for index, (name, strength, enabled) in enumerate(rows):
        inputs.update({f"loras.{index}.lora_name": name, f"loras.{index}.strength": strength,
                       f"loras.{index}.enabled": enabled})
    return {"m": n("SonderPrepModel"), "st": n("LoadLoraModel", **inputs), "out": sink("st")}


async def judge(name, prompt, run):
    executed.clear()
    sunk.clear()
    valid, _, outputs, node_errors = await execution.validate_prompt(name, prompt, None)
    success = None
    if valid and run:
        executor = execution.PromptExecutor(
            SimpleNamespace(client_id=None, send_sync=Mock(), last_node_id=None),
            cache_type=execution.CacheType.NONE, cache_args={"ram": 0, "ram_inactive": 0})
        await executor.execute_async(prompt, name, execute_outputs=outputs)
        success = executor.success
    return {"valid": bool(valid), "errors": sorted(node_errors), "success": success,
            "outputs": dict(sunk), "executed": sorted(map(str, executed))}


async def case(name, prompt, *, before, after, outputs=None, run=True):
    prepared = rp.prepare(prompt, class_lookup=lookup, node_lookup=lookup)
    first = await judge(name + "-compiled", prompt, run)
    second = await judge(name + "-prepared", prepared["prompt"], run)
    passed = first["valid"] is before and second["valid"] is after
    if after and run:
        passed = passed and second["success"] is True and (outputs is None or second["outputs"] == outputs)
    return {"name": name, "passed": passed, "compiled": first, "prepared": second,
            "removed": prepared["removed"], "fallback": prepared["fallback"]}


async def main():
    missing_file = n("SonderPrepFile", lora_name="absent.safetensors")
    unknown = n("NotInstalledNode", value=1)
    cases = [
        await case("switch-missing-class", switch(1, unknown), before=False, after=True, outputs={"result": 1}),
        await case("switch-missing-file", switch(1, missing_file), before=False, after=True, outputs={"result": 1}),
        await case("switch-selected-missing-file", switch(0, missing_file), before=False, after=False),
    ]
    selected = switch(1, unknown)
    selected["sel"] = n("SonderSelector", choice="Second", choice_list="First\nSecond")
    selected["sw"]["inputs"]["select"] = ["sel", 1]
    cases.append(await case("selector-missing-class", selected, before=False, after=True, outputs={"result": 1}))
    cluster = {"good": n("SonderPrepSource", value=2), "bad": missing_file,
               "cl": n("SonderLazyCluster", select=1, branches=2, lanes=1, b0_l0=["bad", 0], b1_l0=["good", 0]),
               "out": sink("cl")}
    cases.append(await case("cluster-missing-file", cluster, before=False, after=True, outputs={"result": 2}))
    gate = {"bad": missing_file, "g": n("SonderGate", value_A=["bad", 0], when_A=False), "out": sink("g")}
    cases.append(await case("gate-closed-missing-file", gate, before=False, after=True, outputs={"result": None}))
    shared = switch(1, unknown)
    shared["second"] = sink("bad", "second")  # the missing node also feeds a second output
    cases.append(await case("missing-class-feeds-second-output", shared, before=False, after=False))
    cases.append(await case("stack-all-off-missing-file", lora_rows(("absent.safetensors", 1.0, False)),
                            before=False, after=True, outputs={"result": "MODEL"}))
    cases.append(await case("stack-zero-strength-missing-file", lora_rows(("absent.safetensors", 0.0, True)),
                            before=False, after=True, outputs={"result": "MODEL"}))
    # Validation only: an active row would load the (empty) fixture file.
    cases.append(await case("stack-mixed", lora_rows(("absent.safetensors", 1.0, False),
                                                     ("present.safetensors", 0.5, True)),
                            before=False, after=True, run=False))
    cases.append(await case("stack-active-missing-file", lora_rows(("absent.safetensors", 1.0, True)),
                            before=False, after=False, run=False))
    print("SONDER_PREP_REPORT=" + json.dumps({"cases": cases}, default=str))
    return 0 if all(c["passed"] for c in cases) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
