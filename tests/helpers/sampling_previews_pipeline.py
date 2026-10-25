"""Live sampling previews through ComfyUI's real preview path and KJ's real wrapper.

Run in a subprocess (``tests/test_sampling_previews_pipeline.py``) with the
test install's interpreter: ``<comfy root> <repo root> <kjnodes dir>``. CPU only,
no model: a stub model patcher carries a real ``comfy.latent_formats`` format,
so ComfyUI's own Latent2RGB previewer and KJ's own decode paths run.

* Native: ``latent_preview.prepare_callback`` -> ``ProgressBar`` -> ComfyUI's
  ``hijack_progress`` hook (extracted from ``main.py`` by AST, not copied) ->
  the progress registry's ``WebUIProgressHandler`` -> ``send_sync`` event 4 or 2,
  with and without a metadata-advertising client, and with previews off.
* KJ: the installed ``_PreviewOverrideWrapper`` driven by a stub OUTER_SAMPLE
  executor (boundary 0, per-step encoder submissions, suppression, shutdown),
  and the installed ``_AsyncPreviewEncoder`` with a worker held while another
  prompt's context is active on the submitting thread.
"""
import ast
import asyncio
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
import types

comfy_root, repo, kj_dir = sys.argv[1:]
sys.argv = [sys.argv[0], "--cpu"]
sys.path.insert(0, comfy_root)

import torch  # noqa: E402

import comfy.latent_formats  # noqa: E402
import comfy.model_management  # noqa: E402
import comfy.utils  # noqa: E402
import latent_preview  # noqa: E402
from comfy_api import feature_flags  # noqa: E402
from comfy_execution.graph import DynamicPrompt  # noqa: E402
from comfy_execution.progress import add_progress_handler, get_progress_state, reset_progress_state, WebUIProgressHandler  # noqa: E402
from comfy_execution.utils import CurrentNodeContext, get_executing_context  # noqa: E402
from protocol import BinaryEventTypes  # noqa: E402
import execution  # noqa: E402

sent = []
METADATA_CLIENT, PLAIN_CLIENT = "client-meta", "client-plain"


class FakeServer:
    def __init__(self):
        self.client_id = None
        self.last_node_id = None
        self.last_prompt_id = None
        self.sockets_metadata = {METADATA_CLIENT: {"feature_flags": {"supports_preview_metadata": True}}}
        self.prompt_queue = execution.PromptQueue(self)

    def queue_updated(self):
        pass

    def send_sync(self, event, data, sid=None):
        sent.append((event, sid))


server_module = types.ModuleType("server")
server_module.PromptServer = type("PromptServer", (), {})
server_module.PromptServer.instance = FakeServer()
server_module.BinaryEventTypes = BinaryEventTypes
sys.modules["server"] = server_module
SERVER = server_module.PromptServer.instance

# ComfyUI's own progress hook, taken from main.py without running main.
main_source = (Path(comfy_root) / "main.py").read_text(encoding="utf-8")
hook_def = next(node for node in ast.parse(main_source).body
                if isinstance(node, ast.FunctionDef) and node.name == "hijack_progress")
hook_ns = {"get_executing_context": get_executing_context, "get_progress_state": get_progress_state,
           "feature_flags": feature_flags, "BinaryEventTypes": BinaryEventTypes}
import comfy as comfy_pkg  # noqa: E402
hook_ns["comfy"] = comfy_pkg
exec(compile(ast.Module(body=[hook_def], type_ignores=[]), "main.py", "exec"), hook_ns)

pkg = types.ModuleType("sonder_previews_testpkg")
pkg.__path__ = [str(Path(repo))]
sys.modules[pkg.__name__] = pkg
importlib.import_module(f"{pkg.__name__}.nodes.io_nodes")  # installs the send_sync wrap and preview observers
sdk_runs = importlib.import_module(f"{pkg.__name__}.server.sdk_runs")
run_outputs = importlib.import_module(f"{pkg.__name__}.server.run_outputs")
run_scope = importlib.import_module(f"{pkg.__name__}.server.run_scope")
sampling_previews = importlib.import_module(f"{pkg.__name__}.server.sampling_previews")
adapter = importlib.import_module(f"{pkg.__name__}.server.kj_preview_adapter")
hook_ns["hijack_progress"](SERVER)  # after the wrap, as in a running server
assert getattr(SERVER.send_sync, "_sonder_sdk_progress", False), "send_sync wrap not installed"

# KJ's module, imported as its package would (relative imports resolve).
kj_pkg = types.ModuleType("kjnodes_testpkg")
kj_pkg.__path__ = [kj_dir]
sys.modules[kj_pkg.__name__] = kj_pkg
kj_nodes = types.ModuleType("kjnodes_testpkg.nodes")
kj_nodes.__path__ = [str(Path(kj_dir) / "nodes")]
sys.modules[kj_nodes.__name__] = kj_nodes
kj = importlib.import_module("kjnodes_testpkg.nodes.preview_override_node")

OWNER = ("host", "proj", "host-1")
run_scope.reset_for_tests({"SonderEditor": False, "KSampler": False, "ModelPreviewOverrideKJ": False, "Out": False})
report = {}


def bind(prompt_id, receipt, kj_keys=("kj",)):
    prompt = {"ed": {"class_type": "SonderEditor", "inputs": {}, "_meta": {"sonder_receipt": receipt}},
              "load": {"class_type": "Out", "inputs": {}}}
    model = ["load", 0]
    for key in kj_keys:
        prompt[key] = {"class_type": "ModelPreviewOverrideKJ", "inputs": {"model": model}}
        model = [key, 0]
    prompt["s"] = {"class_type": "KSampler", "inputs": {"model": model}}
    prompt["out"] = {"class_type": "Out", "inputs": {"v": ["s", 0], "p": ["ed", 0]}}
    assert sdk_runs.register_receipt(receipt, OWNER, ["ed"])[0] == "ok"
    sdk_runs.bind_queued(prompt_id, prompt, ["out"], workflow_id="wf")
    SERVER.send_sync("execution_start", {"prompt_id": prompt_id}, None)  # through the wrap: every observer
    return prompt


def live(*sources):
    return run_outputs.read_live([OWNER], "", list(sources))["sources"]


class StubModel:
    def __init__(self, latent_format):
        self.latent_format = latent_format
        self.diffusion_model = object()


class StubPatcher:
    load_device = torch.device("cpu")

    def __init__(self, latent_format):
        self.model = StubModel(latent_format)


def native_run(prompt_id, receipt, client, method):
    # ComfyUI's order: execute() sets the prompt's preview method, then sends execution_start.
    latent_preview.set_preview_method(method)
    prompt = bind(prompt_id, receipt, kj_keys=())
    SERVER.client_id = client
    reset_progress_state(prompt_id, DynamicPrompt(prompt))
    add_progress_handler(WebUIProgressHandler(SERVER))
    sent.clear()
    with CurrentNodeContext(prompt_id, "s"):
        callback = latent_preview.prepare_callback(StubPatcher(comfy.latent_formats.SD15()), 6)
        for step in range(6):
            x0 = torch.randn(1, 4, 8, 8)
            callback(step, x0, x0, 6)
    events = [event for event, _sid in sent if event in (2, 4)]
    return {"events": events, "live": live("s")["s"]}


async def main():
    report["native_metadata"] = native_run("n1", "cccccccc-0000-4000-8000-000000000001", METADATA_CLIENT, "latent2rgb")
    report["native_plain"] = native_run("n2", "cccccccc-0000-4000-8000-000000000002", PLAIN_CLIENT, "latent2rgb")
    report["native_off"] = native_run("n3", "cccccccc-0000-4000-8000-000000000003", PLAIN_CLIENT, "none")
    report["native_off"]["preview_method"] = report["native_off"]["live"]["run"]["preview_method"]
    latent_preview.set_preview_method(None)
    report["native_bytes"] = len(sampling_previews.encode_payload(
        run_outputs.live_payload([OWNER], "n2", "s")[2]) or b"")

    # --- KJ: the adapter on the installed module ---
    report["shape"] = adapter.check_shape(kj)
    report["install"] = adapter.install(kj)["state"]
    report["install_again"] = adapter.install(kj)["state"]
    report["marked"] = getattr(kj._AsyncPreviewEncoder.__dict__["submit"], adapter.MARKER, False)

    # The real wrapper through a stub OUTER_SAMPLE executor, image and video latents.
    for name, latent_format, shape, frames in (
            ("kj_image", comfy.latent_formats.SD15(), (1, 4, 8, 8), 1),
            ("kj_video", comfy.latent_formats.Wan21(), (1, 16, 5, 8, 8), 4)):
        prompt_id = f"{name}-pid"
        bind(prompt_id, "cccccccc-0000-4000-8000-0000000000" + ("10" if frames == 1 else "11"))
        wrapper = kj._PreviewOverrideWrapper(64, "kj", 80, True, preview_frames=frames, preview_fps=8)
        guider = types.SimpleNamespace(model_patcher=StubPatcher(latent_format), conds={})
        suppressed = []
        unsuppressed = latent_preview.LatentPreviewer.decode_latent_to_preview_image
        # The sampler's own native callback, as ComfyUI hands it to the wrapper:
        # KJ suppresses native previews while it samples, so none may arrive.
        latent_preview.set_preview_method("latent2rgb")
        reset_progress_state(prompt_id, DynamicPrompt(sdk_runs.bound_prompt(prompt_id)))
        add_progress_handler(WebUIProgressHandler(SERVER))
        SERVER.client_id = METADATA_CLIENT
        native_callback = latent_preview.prepare_callback(StubPatcher(latent_format), 4)

        def executor(noise, latent_image, sampler, sigmas, denoise_mask, callback, disable_pbar, seed, latent_shapes=None):
            suppressed.append(latent_preview.LatentPreviewer.decode_latent_to_preview_image is not unsuppressed)
            for step in range(4):
                callback(step, torch.randn(*shape), torch.randn(*shape), 4)
                time.sleep(0.05)
            return torch.zeros(*shape)

        executor.class_obj = guider
        sent.clear()
        with CurrentNodeContext(prompt_id, "s"):
            wrapper(executor, torch.randn(*shape), torch.zeros(*shape), types.SimpleNamespace(extra_options={}),
                    torch.linspace(1.0, 0.0, 5), None, native_callback, True, 0, None)
        time.sleep(0.3)
        latent_preview.set_preview_method(None)
        record = live("kj")["kj"]
        native_view = live("s")["s"]
        report[name] = {"kj_events": sum(1 for event, _sid in sent if event == "kj_preview_override"),
                        "suppressed": all(suppressed) and bool(suppressed),
                        "native_events": sum(1 for event, _sid in sent if event in (2, 4)),
                        "native_state": native_view["state"],
                        "suppressed_by": native_view["run"]["suppressed_by"],
                        "state": record["state"], "via": record["live"]["via"] if record["live"] else None,
                        "step": record["live"]["step"] if record["live"] else None,
                        "mime": record["live"]["mime"] if record["live"] else None,
                        "reattributed": record["live"]["reattributed"] if record["live"] else None}

    # A held worker sends after another prompt's context became active.
    bind("held-a", "cccccccc-0000-4000-8000-000000000020")
    bind("held-b", "cccccccc-0000-4000-8000-000000000021")
    gate = threading.Event()
    encoder = None
    with CurrentNodeContext("held-a", "s"):
        encoder = kj._AsyncPreviewEncoder()

        def late_send():
            gate.wait(5)
            server_module.PromptServer.instance.send_sync(
                "kj_preview_override", {"node_id": "kj", "image": "UklGRgAAAABXRUJQ", "mime": "image/webp",
                                        "step": 7, "total": 9}, SERVER.client_id)
        encoder.submit(late_send)
    with CurrentNodeContext("held-b", "s"):
        SERVER.client_id = "someone-else"
        gate.set()
        encoder.shutdown(5)
    report["held"] = {"a": live("kj")["kj"]["run"]["prompt_id"], "a_step": (live("kj")["kj"]["live"] or {}).get("step"),
                      "b_has": "held-b" in json.dumps(run_outputs.read_live([OWNER], "", ["kj"]))}
    with run_outputs._LOCK:
        report["held"]["a_record"] = run_outputs._DATA["held-a"]["live"].get("kj", {}).get("step")
        report["held"]["b_record"] = run_outputs._DATA.get("held-b", {}).get("live", {}).get("kj")

    # Without the adapter the same send has no context: dropped and counted, never guessed.
    original = kj._AsyncPreviewEncoder.__dict__["submit"].__wrapped__
    bind("bare", "cccccccc-0000-4000-8000-000000000030")
    before = sampling_previews.COUNTERS["unattributed_kj"]
    with CurrentNodeContext("bare", "s"):
        bare = kj._AsyncPreviewEncoder()
        original(bare, lambda: server_module.PromptServer.instance.send_sync(
            "kj_preview_override", {"node_id": "kj", "image": "UklGRgAAAABXRUJQ", "mime": "image/webp"}, None))
        bare.shutdown(5)
    report["bare_unattributed"] = sampling_previews.COUNTERS["unattributed_kj"] - before
    report["bare_degraded"] = adapter.status()["state"]

    # The wrap's own cost per event (sending thread), with a bound run.
    started = time.perf_counter()
    count = 2000
    with CurrentNodeContext("n2", "s"):
        tiny = ("JPEG", latent_preview.Image.new("RGB", (8, 8)), 512)
        for _ in range(count):
            SERVER.send_sync(BinaryEventTypes.UNENCODED_PREVIEW_IMAGE, tiny, PLAIN_CLIENT)
    bound_us = (time.perf_counter() - started) / count * 1e6
    started = time.perf_counter()
    for _ in range(count):
        SERVER.send_sync("progress", {"value": 1, "max": 2, "prompt_id": "unbound", "node": "s"}, None)
    other_us = (time.perf_counter() - started) / count * 1e6
    registry = get_progress_state()
    reset_progress_state("n2", DynamicPrompt(sdk_runs.bound_prompt("n2") or {"s": {"class_type": "KSampler", "inputs": {}}}))
    started = time.perf_counter()
    with CurrentNodeContext("n2", "s"):
        for _ in range(count):
            SERVER.send_sync(BinaryEventTypes.PREVIEW_IMAGE_WITH_METADATA,
                             (tiny, {"prompt_id": "n2", "node_id": "s", "display_node_id": "s"}), METADATA_CLIENT)
    event4_us = (time.perf_counter() - started) / count * 1e6
    started = time.perf_counter()
    with CurrentNodeContext("kj_image-pid", "s"):
        for _ in range(count):
            SERVER.send_sync("kj_preview_override", {"node_id": "kj", "image": "UklGRgAAAABXRUJQ",
                                                     "mime": "image/webp", "step": 1, "total": 4}, None)
    kj_us = (time.perf_counter() - started) / count * 1e6
    started = time.perf_counter()
    with CurrentNodeContext("never-bound", "s"):
        for _ in range(count):
            SERVER.send_sync(BinaryEventTypes.UNENCODED_PREVIEW_IMAGE, tiny, PLAIN_CLIENT)
    unbound_us = (time.perf_counter() - started) / count * 1e6
    report["bench_us"] = {"bound_event2": round(bound_us, 2), "bound_event4": round(event4_us, 2),
                          "bound_kj": round(kj_us, 2), "unbound_event2": round(unbound_us, 2),
                          "unrelated_event": round(other_us, 2)}
    report["budget_bytes"] = run_outputs.live_bytes_in_use()


asyncio.run(main())
print("SONDER_SAMPLING_PREVIEWS_REPORT=" + json.dumps(report))
