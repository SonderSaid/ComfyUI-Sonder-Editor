"""Real ComfyUI queue + executor run through Sonder's put/get/task_done and send_sync hooks.

The pieces under test are the ones the unit tests cannot see: ComfyUI's own
event order (executing before executed, cached nodes without executing, the
final sweep replaying cached intermediate outputs outside the selected branch)
reaching Sonder's observer through the real wraps, with a client id set so
ComfyUI sends those events at all.
"""
import asyncio
import importlib
import json
from pathlib import Path
import sys
import types

comfy, repo = sys.argv[1:]
sys.argv = [sys.argv[0], "--cpu"]
sys.path.insert(0, comfy)

import execution
import nodes
from comfy_api.latest import io

# A stand-in for ComfyUI's top-level server module: Sonder's hooks find the
# prompt queue and send_sync on PromptServer.instance, as in a running server.
sent = []


class FakeServer:
    def __init__(self):
        self.client_id = None
        self.last_node_id = None
        self.prompt_queue = execution.PromptQueue(self)

    def queue_updated(self):
        pass

    def send_sync(self, event, data, sid=None):
        sent.append((event, data))


server_module = types.ModuleType("server")
server_module.PromptServer = type("PromptServer", (), {})
server_module.PromptServer.instance = FakeServer()
sys.modules["server"] = server_module

pkg = types.ModuleType("sonder_pipeline_testpkg")
pkg.__path__ = [str(Path(repo))]
sys.modules[pkg.__name__] = pkg
io_nodes = importlib.import_module(f"{pkg.__name__}.nodes.io_nodes")
sdk_runs = importlib.import_module(f"{pkg.__name__}.server.sdk_runs")
run_outputs = importlib.import_module(f"{pkg.__name__}.server.run_outputs")
run_scope = importlib.import_module(f"{pkg.__name__}.server.run_scope")
SERVER = server_module.PromptServer.instance
assert getattr(SERVER.prompt_queue.put, "_sonder_bridge_wrapped", False), "put hook not installed"


class Publisher(io.ComfyNode):
    """An intermediate (non-output) node that publishes a preview, like a V3 previewer."""

    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="SonderPipeTestPublisher", inputs=[io.Int.Input("value")],
                         outputs=[io.Int.Output()], has_intermediate_output=True)

    @classmethod
    def execute(cls, value):
        return io.NodeOutput(value, ui={"images": [{"filename": f"pub{value}.png", "subfolder": "", "type": "temp"}]})


class Output(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="SonderPipeTestOutput", inputs=[io.Int.Input("value")], outputs=[], is_output_node=True)

    @classmethod
    def execute(cls, value):
        return io.NodeOutput(ui={"images": [{"filename": f"out{value}.png", "subfolder": "", "type": "output"}]})


class FakeEditor(io.ComfyNode):
    """Carries the receipt the way the canvas stamps it on a real Editor; never executed."""

    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="SonderEditor", inputs=[], outputs=[io.Int.Output()])

    @classmethod
    def execute(cls):
        return io.NodeOutput(0)


nodes.NODE_CLASS_MAPPINGS.update({"SonderPipeTestPublisher": Publisher, "SonderPipeTestOutput": Output,
                                  "SonderEditor": FakeEditor})
run_scope.reset_for_tests({"SonderPipeTestPublisher": False, "SonderPipeTestOutput": False, "SonderEditor": False})
OWNER = ("host", "proj", "host-1")
executor = execution.PromptExecutor(SERVER, cache_type=execution.CacheType.CLASSIC,
                                    cache_args={"ram": 0, "ram_inactive": 0})


def prompt_for(a_value, receipt):
    return {
        "ed": {"class_type": "SonderEditor", "inputs": {}, "_meta": {"sonder_receipt": receipt}},
        "a": {"class_type": "SonderPipeTestPublisher", "inputs": {"value": a_value}},
        "outA": {"class_type": "SonderPipeTestOutput", "inputs": {"value": ["a", 0]}},
        "b": {"class_type": "SonderPipeTestPublisher", "inputs": {"value": 2}},
        "outB": {"class_type": "SonderPipeTestOutput", "inputs": {"value": ["b", 0]}},
    }


async def run(prompt_id, prompt, targets=None, register_first=True):
    receipt = prompt["ed"]["_meta"]["sonder_receipt"]
    if register_first:
        code, adopted = sdk_runs.register_receipt(receipt, OWNER, ["ed"])
        assert code == "ok" and adopted == "", (code, adopted)
    valid = await execution.validate_prompt(prompt_id, prompt, targets)
    assert valid[0], valid
    extra = {"client_id": "pipeline-client"}
    SERVER.prompt_queue.put((0, prompt_id, prompt, extra, valid[2], {}))
    if not register_first:
        code, adopted = sdk_runs.register_receipt(receipt, OWNER, ["ed"])
        assert code == "ok" and adopted == prompt_id, (code, adopted)
        run_outputs.announce(prompt_id)
    item, item_id = SERVER.prompt_queue.get(timeout=1)
    SERVER.client_id = item[3]["client_id"]
    await executor.execute_async(item[2], item[1], item[3], item[4])
    SERVER.prompt_queue.task_done(item_id, executor.history_result, status=execution.PromptQueue.ExecutionStatus(
        status_str="success" if executor.success else "error", completed=executor.success,
        messages=executor.status_messages))
    assert executor.success, executor.status_messages


def view(result):
    item = dict(result)
    if item.get("result"):
        res = item["result"]
        item["result"] = {"prompt_id": res["prompt_id"], "fresh": res["fresh"],
                          "files": [entry["file"]["filename"] for entry in res["entries"]]}
    item["latest_run"] = (item.get("latest_run") or {}).get("prompt_id")
    return item


async def main():
    report = {}
    sources = ["a", "b", "outA", "outB"]
    await run("11111111-1111-4111-8111-111111111111", prompt_for(1, "aaaaaaaa-0000-4000-8000-000000000001"))
    report["full"] = {key: view(value) for key, value in run_outputs.read_outputs([OWNER], "", sources)["sources"].items()}
    # Queue selected output nodes on outA only: a and outA come back from cache;
    # the final sweep replays b's cached preview, which this run did not select.
    sent.clear()
    await run("22222222-2222-4222-8222-222222222222", prompt_for(1, "aaaaaaaa-0000-4000-8000-000000000002"),
              targets=["outA"], register_first=False)
    report["swept_b_replayed"] = any(event == "executed" and data.get("node") == "b"
                                     and data.get("prompt_id", "").startswith("2222") for event, data in sent)
    report["partial_cached"] = {key: view(value) for key, value in run_outputs.read_outputs([OWNER], "", sources)["sources"].items()}
    report["partial_run_sources"] = sorted(run_outputs.read_run([OWNER], "22222222-2222-4222-8222-222222222222")["sources"])
    await run("33333333-3333-4333-8333-333333333333", prompt_for(5, "aaaaaaaa-0000-4000-8000-000000000003"), targets=["outA"])
    report["partial_changed"] = {key: view(value) for key, value in run_outputs.read_outputs([OWNER], "", sources)["sources"].items()}
    report["foreign"] = run_outputs.read_outputs([("host", "proj", "other")], "", sources)["sources"]["a"]["state"]
    # ComfyUI's own V3 loop nodes declare their runtime-graph inputs as tuples;
    # read without a seeded memo, they make a prompt's scope unknown.
    from comfy_extras import nodes_loop
    run_scope.reset_for_tests()
    loops = {name: cls for name, cls in vars(nodes_loop).items() if name in {"StartLoop", "LoopResult", "EndLoop"}}
    nodes.NODE_CLASS_MAPPINGS.update(loops)
    report["loops_see_runtime_graph"] = {name: run_scope._class_sees_runtime_graph(name) for name in sorted(loops)}
    loop_prompt = {"l": {"class_type": "EndLoop", "inputs": {}}, **prompt_for(1, "x")}
    report["loop_scope"] = run_scope.compute(loop_prompt, ["outA"])
    plain = prompt_for(1, "x")
    del plain["outB"], plain["b"]
    report["plain_scope"] = sorted(run_scope.compute(plain, ["outA"]))
    print("SONDER_RUN_OUTPUTS_REPORT=" + json.dumps(report))


asyncio.run(main())
