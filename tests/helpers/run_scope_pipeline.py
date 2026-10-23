"""Scoped execution (2B) through ComfyUI's real queue, executor and Sonder's queue hooks.

A stand-in Editor asks Sonder's own helpers, at execution time, the questions the
real Editor asks: is a queue-completing save reached (consume or peek), and which
of its outputs are used. The answers come from the scope the release record
froze from ComfyUI's ``outputs_to_execute``, with no registration at all. A
two-pass graph is run whole and as a branch on its first-pass preview, under
the classic cache and under no cache.
"""
import asyncio
import importlib
import json
from pathlib import Path
import sys
import types

comfy, repo, cache_name = sys.argv[1:]
sys.argv = [sys.argv[0], "--cpu"]
sys.path.insert(0, comfy)

import execution
import nodes
from comfy_api.latest import io


class FakeServer:
    def __init__(self):
        self.client_id = None
        self.last_node_id = None
        self.prompt_queue = execution.PromptQueue(self)

    def queue_updated(self):
        pass

    def send_sync(self, event, data, sid=None):
        pass


server_module = types.ModuleType("server")
server_module.PromptServer = type("PromptServer", (), {})
server_module.PromptServer.instance = FakeServer()
sys.modules["server"] = server_module

pkg = types.ModuleType("sonder_scope_testpkg")
pkg.__path__ = [str(Path(repo))]
sys.modules[pkg.__name__] = pkg
importlib.import_module(f"{pkg.__name__}.nodes.io_nodes")
prompt_graph = importlib.import_module(f"{pkg.__name__}.server.prompt_graph")
run_scope = importlib.import_module(f"{pkg.__name__}.server.run_scope")
SERVER = server_module.PromptServer.instance
executed, decisions = [], []


class Editor(io.ComfyNode):
    """Answers what the real Editor answers, from inside a real execution."""

    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="SonderEditor", inputs=[io.Int.Input("nonce")],
                         outputs=[io.Int.Output("project"), io.Int.Output("frames")],
                         hidden=[io.Hidden.prompt, io.Hidden.unique_id])

    @classmethod
    def execute(cls, nonce):
        prompt, unique_id = cls.hidden.prompt, cls.hidden.unique_id
        scope = run_scope.for_prompt(prompt)
        decisions.append({
            "consume": prompt_graph.execution_reaches_save_with_mark(prompt, unique_id, True, scope),
            "scope": sorted(scope) if scope is not None else None,
        })
        executed.append(unique_id)
        return io.NodeOutput(nonce, nonce)


class Pass(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="SonderScopeTestPass", inputs=[io.Int.Input("value")], outputs=[io.Int.Output()],
                         hidden=[io.Hidden.unique_id])

    @classmethod
    def execute(cls, value):
        executed.append(cls.hidden.unique_id)
        return io.NodeOutput(value + 1)


class Preview(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="SonderScopeTestPreview", inputs=[io.Int.Input("value")], outputs=[],
                         is_output_node=True, hidden=[io.Hidden.unique_id])

    @classmethod
    def execute(cls, value):
        executed.append(cls.hidden.unique_id)
        return io.NodeOutput(ui={"images": [{"filename": f"p{value}.png", "subfolder": "", "type": "temp"}]})


class Save(io.ComfyNode):
    """Stands in for Sonder Save Video: a marked save that also passes a value on."""

    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="SonderSaveVideo", inputs=[io.Int.Input("project"), io.Int.Input("frames"),
                                                             io.Boolean.Input("mark_queue_complete")],
                         outputs=[io.Int.Output()], is_output_node=True, hidden=[io.Hidden.unique_id])

    @classmethod
    def execute(cls, project, frames, mark_queue_complete):
        executed.append(cls.hidden.unique_id)
        return io.NodeOutput(frames)


class LazySwitch(io.ComfyNode):
    """A two-pass bypass: only the chosen input is ever requested."""

    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="SonderScopeTestLazySwitch",
                         inputs=[io.Boolean.Input("use_first"), io.Int.Input("first", lazy=True),
                                 io.Int.Input("second", lazy=True)],
                         outputs=[io.Int.Output()], hidden=[io.Hidden.unique_id])

    @classmethod
    def check_lazy_status(cls, use_first, first=None, second=None):
        if use_first and first is None:
            return ["first"]
        if not use_first and second is None:
            return ["second"]
        return []

    @classmethod
    def execute(cls, use_first, first=None, second=None):
        executed.append(cls.hidden.unique_id)
        return io.NodeOutput(first if use_first else second)


nodes.NODE_CLASS_MAPPINGS.update({"SonderEditor": Editor, "SonderScopeTestPass": Pass,
                                  "SonderScopeTestPreview": Preview, "SonderSaveVideo": Save,
                                  "SonderScopeTestLazySwitch": LazySwitch})
run_control = importlib.import_module(f"{pkg.__name__}.server.run_control")
cache_type = {"classic": execution.CacheType.CLASSIC, "none": execution.CacheType.NONE}[cache_name]
executor = execution.PromptExecutor(SERVER, cache_type=cache_type, cache_args={"ram": 0, "ram_inactive": 0})


def two_pass(nonce):
    return {
        "ed": {"class_type": "SonderEditor", "inputs": {"nonce": nonce}},
        "pass1": {"class_type": "SonderScopeTestPass", "inputs": {"value": ["ed", 1]}},
        "preview": {"class_type": "SonderScopeTestPreview", "inputs": {"value": ["pass1", 0]}},
        "pass2": {"class_type": "SonderScopeTestPass", "inputs": {"value": ["pass1", 0]}},
        "save": {"class_type": "SonderSaveVideo", "inputs": {"project": ["ed", 0], "frames": ["pass2", 0],
                                                            "mark_queue_complete": True}},
        "after": {"class_type": "SonderScopeTestPreview", "inputs": {"value": ["save", 0]}},
        "other": {"class_type": "SonderScopeTestPreview", "inputs": {"value": 7}},
        # Skip-first-pass shape: a lazy switch whose unchosen input is pass two.
        "pick": {"class_type": "SonderScopeTestLazySwitch",
                 "inputs": {"use_first": True, "first": ["pass1", 0], "second": ["pass2", 0]}},
        "picked": {"class_type": "SonderScopeTestPreview", "inputs": {"value": ["pick", 0]}},
    }


async def run(prompt_id, prompt, targets=None, cancel_after_get=False):
    executed.clear()
    decisions.clear()
    valid = await execution.validate_prompt(prompt_id, prompt, targets)
    assert valid[0], valid
    SERVER.prompt_queue.put((0, prompt_id, prompt, {"client_id": "c"}, valid[2], {}))
    item, item_id = SERVER.prompt_queue.get(timeout=1)
    cancel = None
    if cancel_after_get:
        # The window between get and ComfyUI resetting its interrupt flag: the
        # interrupt lands now, the reset clears it, execution_start re-applies it.
        cancel = run_control.cancel_prompt(prompt_id, SERVER.prompt_queue)
    SERVER.client_id = "c"
    await executor.execute_async(item[2], item[1], item[3], item[4])
    SERVER.prompt_queue.task_done(item_id, executor.history_result, status=execution.PromptQueue.ExecutionStatus(
        status_str="success" if executor.success else "error", completed=executor.success,
        messages=executor.status_messages))
    if not cancel_after_get:
        assert executor.success, executor.status_messages
    return {"executed": sorted(executed), "decisions": list(decisions), "success": executor.success,
            "cancel": cancel, "messages": [message[0] for message in executor.status_messages]}


async def main():
    report = {
        "full": await run("11111111-1111-4111-8111-000000000001", two_pass(1)),
        "branch": await run("11111111-1111-4111-8111-000000000002", two_pass(2), ["preview"]),
        "after_save": await run("11111111-1111-4111-8111-000000000003", two_pass(3), ["after"]),
        "unrelated": await run("11111111-1111-4111-8111-000000000004", two_pass(4), ["other"]),
        "lazy": await run("11111111-1111-4111-8111-000000000005", two_pass(5), ["picked"]),
        "cancelled": await run("11111111-1111-4111-8111-000000000006", two_pass(6), ["preview"], cancel_after_get=True),
    }
    print("SONDER_RUN_SCOPE_REPORT=" + json.dumps(report))


asyncio.run(main())
