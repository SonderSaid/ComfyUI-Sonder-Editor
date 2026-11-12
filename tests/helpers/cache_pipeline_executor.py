"""Execution cache pipeline through ComfyUI's real queue, executor and caches (CPU).

Run by ``tests/test_cache_pipeline_executor.py`` in a subprocess from the ComfyUI
root, once per cache mode: ``python cache_pipeline_executor.py <comfy> <repo>
<mode> <out.json> [case,case]``. Modes: classic, lru, ram, none.

The real ``SonderEditor``, ``SonderSaveVideo``, Sonder lazy nodes and Guides
Bridge run beside counted probe consumers and native consumers (V1
``EmptyLatentImage``, V3 ``StringConcatenate``). Sonder's queue hooks are
installed by importing ``io_nodes``, which also installs the executor hook into
this process's real ``execution`` module. Each oracle arm gets a fresh
``PromptExecutor``; the "stock" arm uninstalls the hook.

Every case appends ``{case, check, ok, detail}`` rows; the test asserts that no
row failed. A case that raises records one failed row with its traceback.
"""
import asyncio
import copy
import importlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import traceback
import types
import uuid

comfy_root, repo, MODE, out_path = sys.argv[1:5]
ONLY = set(sys.argv[5].split(",")) if len(sys.argv) > 5 and sys.argv[5] else None
sys.argv = [sys.argv[0], "--cpu"]
sys.path.insert(0, comfy_root)

import execution  # noqa: E402
import folder_paths  # noqa: E402
import nodes  # noqa: E402
import comfy.model_management as mm  # noqa: E402
from comfy_api.latest import io  # noqa: E402
from comfy_execution import progress as comfy_progress  # noqa: E402
from comfy_execution.progress import get_progress_state  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="sonder-cache-pipeline-"))
for name, setter in (("output", folder_paths.set_output_directory), ("temp", folder_paths.set_temp_directory),
                     ("input", folder_paths.set_input_directory)):
    (TMP / name).mkdir()
    setter(str(TMP / name))


class FakeServer:
    def __init__(self):
        self.client_id = None
        self.last_node_id = None
        self.sockets_metadata = {}
        self.events = []
        self.prompt_queue = execution.PromptQueue(self)

    def queue_updated(self):
        pass

    def send_sync(self, event, data, sid=None):
        self.events.append((event, copy.copy(data) if isinstance(data, dict) else data, sid))


server_module = types.ModuleType("server")
server_module.PromptServer = type("PromptServer", (), {})
server_module.PromptServer.instance = FakeServer()
sys.modules["server"] = server_module
SERVER = server_module.PromptServer.instance

pkg = types.ModuleType("sonder_cache_pipeline_pkg")
pkg.__path__ = [str(Path(repo))]
sys.modules[pkg.__name__] = pkg


def sonder(name):
    return importlib.import_module(f"{pkg.__name__}.{name}")


io_nodes = sonder("nodes.io_nodes")
editor_node = sonder("nodes.editor_node")
lazy = sonder("nodes.lazy_switches")
bridges = sonder("nodes.bridge_nodes")
collector = sonder("nodes.metadata_collector")
hook = sonder("server.executor_hook")
pipeline = sonder("server.cache_pipeline")
sdk_runs = sonder("server.sdk_runs")
manager = sonder("server.project_manager")
timeline = sonder("server.timeline_state")
from comfy_extras.nodes_preview_any import PreviewAny  # noqa: E402
from comfy_extras.nodes_string import StringConcatenate  # noqa: E402

SonderEditor = editor_node.SonderEditor
CALLS = {}
NOTES = []


def count(label, node_id):
    CALLS[f"{label}:{node_id}"] = CALLS.get(f"{label}:{node_id}", 0) + 1


# --- Probe nodes ----------------------------------------------------------------------


def _v1(name, kind, label):
    class Probe:
        RETURN_TYPES = (kind,)
        FUNCTION = "run"
        CATEGORY = "CPH"

        @classmethod
        def INPUT_TYPES(cls):
            spec = {"STRING": ("STRING", {"forceInput": True}), "INT": ("INT", {"min": -(2 ** 62), "max": 2 ** 62}),
                    "FLOAT": ("FLOAT", {"min": -1e9, "max": 1e9, "step": 0.001})}[kind]
            return {"required": {"value": spec}, "hidden": {"unique_id": "UNIQUE_ID"}}

        def run(self, value, unique_id=None):
            count(label, unique_id)
            NOTES.append({"probe": unique_id, "value": value, "type": type(value).__name__})
            return (value,)

    Probe.__name__ = name
    return Probe


CPText = _v1("CPText", "STRING", "text")
CPInt = _v1("CPInt", "INT", "int")
CPFloat = _v1("CPFloat", "FLOAT", "float")


class CPImage:
    """Mock generation keyed by text, width and seed."""
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "run"
    CATEGORY = "CPH"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"text": ("STRING", {"forceInput": True}), "width": ("INT", {"min": 8, "max": 4096}),
                             "seed": ("INT", {"min": 0, "max": 2 ** 31})},
                "hidden": {"unique_id": "UNIQUE_ID"}}

    def run(self, text, width, seed, unique_id=None):
        import hashlib
        import torch
        count("generation", unique_id)
        digest = hashlib.sha256(f"{seed}\n{text}".encode()).digest()
        frames = torch.tensor(list(digest[:3]), dtype=torch.float32) / 255
        return (frames.view(1, 1, 1, 3).expand(2, 16, width, 3).contiguous(),)


class CPImageTap:
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    CATEGORY = "CPH"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"frames": ("IMAGE",)}, "hidden": {"unique_id": "UNIQUE_ID"}}

    def run(self, frames, unique_id=None):
        count("image_tap", unique_id)
        NOTES.append({"tap": unique_id, "inference": bool(frames.is_inference()), "shape": list(frames.shape)})
        return (f"{list(frames.shape)}",)


class CPFail:
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    CATEGORY = "CPH"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"text": ("STRING", {})}, "hidden": {"unique_id": "UNIQUE_ID"}}

    def run(self, text, unique_id=None):
        count("fail", unique_id)
        raise RuntimeError("CPFail: deliberate unrelated failure")


class CPMark:
    """An independent output node no earlier prompt can have cached."""
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    CATEGORY = "CPH"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"text": ("STRING", {})}, "hidden": {"unique_id": "UNIQUE_ID"}}

    def run(self, text, unique_id=None):
        count("mark", unique_id)
        return {"ui": {"text": (text,)}, "result": (text,)}


class CPSwallow:
    """A badly behaved node that clears ComfyUI's interrupt flag.

    Its IS_CHANGED runs in set_prompt, right after the hook re-armed a cancel and
    before any node body: once it clears the flag, only the stored cancellation
    stops the scheduled Editor from running its body (and claiming) again.
    """
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    CATEGORY = "CPH"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"text": ("STRING", {})}, "hidden": {"unique_id": "UNIQUE_ID"}}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        if mm.processing_interrupted():
            mm.interrupt_current_processing(False)
            NOTES.append({"swallowed": True})
        return float("nan")

    def run(self, text, unique_id=None):
        count("swallow", unique_id)
        return {"ui": {"text": (text,)}, "result": (text,)}


class CPEvict:
    """Empties the RAM-pressure cache, active entries included, then passes on."""
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    CATEGORY = "CPH"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"anchor": ("STRING", {"forceInput": True})}, "hidden": {"unique_id": "UNIQUE_ID"}}

    def run(self, anchor, unique_id=None):
        count("evict", unique_id)
        cache = CURRENT["executor"].caches.outputs
        freed = cache.ram_release(float("inf"), free_active=True) if hasattr(cache, "ram_release") else None
        NOTES.append({"evict": unique_id, "freed": freed})
        return (anchor,)


class CPProjectTap:
    """Consumes the Editor's project output late (after an eviction)."""
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    CATEGORY = "CPH"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"project": ("SONDER_PROJECT",), "anchor": ("STRING", {"forceInput": True})},
                "hidden": {"unique_id": "UNIQUE_ID"}}

    def run(self, project, anchor, unique_id=None):
        count("project_tap", unique_id)
        return {"ui": {"text": (anchor,)}, "result": (anchor,)}


class CPDyn:
    """Sees the dynamic prompt: the run's scope is unknown."""
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    CATEGORY = "CPH"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"text": ("STRING", {"forceInput": True})}, "hidden": {"dynprompt": "DYNPROMPT"}}

    def run(self, text, dynprompt=None):
        return (text,)


class CPGuideBody:
    """Guides Bridge loop body that also consumes an Editor scalar."""
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    CATEGORY = "CPH"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"image": ("IMAGE",), "index": ("INT", {"min": -1, "max": 99999}),
                             "width": ("INT", {"min": 0, "max": 99999}),
                             "previous": ("STRING", {"forceInput": True})},
                "hidden": {"unique_id": "UNIQUE_ID"}}

    def run(self, image, index, width, previous, unique_id=None):
        count("guide_body", unique_id)
        # Not a DYNPROMPT input: that would make the run's scope unknown.
        dynprompt = CURRENT.get("dynprompt")
        node = dynprompt.get_node(unique_id) if dynprompt is not None else {}
        NOTES.append({"guide_body": unique_id, "width_literal": not pipeline.is_link(node.get("inputs", {}).get("width"))})
        return (f"{previous}|{index}:w{width}",)


class CPIdV3(io.ComfyNode):
    """A V3 node that bakes its own id into its output (the id-less key bug)."""

    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="CPIdV3", inputs=[io.String.Input("text")], outputs=[io.String.Output()],
                         hidden=[io.Hidden.unique_id])

    @classmethod
    def execute(cls, text):
        count("id_v3", cls.hidden.unique_id)
        return io.NodeOutput(f"{text}@{cls.hidden.unique_id}")


nodes.NODE_CLASS_MAPPINGS.update({
    "SonderEditor": SonderEditor, "SonderSaveVideo": io_nodes.SonderSaveVideo,
    "SonderPreviewVideo": io_nodes.SonderPreviewVideo, "SonderMetadataCollector": collector.SonderMetadataCollector,
    "SonderGuidesBridgeStart": bridges.SonderGuidesBridgeStart, "SonderGuidesBridgeEnd": bridges.SonderGuidesBridgeEnd,
    "PreviewAny": PreviewAny, "StringConcatenate": StringConcatenate,
    "CPText": CPText, "CPInt": CPInt, "CPFloat": CPFloat, "CPImage": CPImage, "CPImageTap": CPImageTap,
    "CPFail": CPFail, "CPMark": CPMark, "CPSwallow": CPSwallow, "CPEvict": CPEvict, "CPProjectTap": CPProjectTap,
    "CPDyn": CPDyn, "CPGuideBody": CPGuideBody, "CPIdV3": CPIdV3,
})
nodes.NODE_CLASS_MAPPINGS.update(lazy.LAZY_NODE_CLASS_MAPPINGS)
StringConcatenate.GET_SCHEMA()
CPIdV3.GET_SCHEMA()

# --- Instrumentation ----------------------------------------------------------------------

PREPARATIONS = []
EDITOR_RUNS = []
CLAIMS = []
CURRENT = {"executor": None}
_real_prepare = pipeline.prepare
_real_execute_once = SonderEditor._execute_once
_real_consume = SonderEditor._consume_queue_job
_real_render = SonderEditor._render_scene_frames
_real_save_project = editor_node.save_project
INJECT = {"conflicts": 0, "render_error": False, "interrupt_at": None, "ram_release_in_render": False,
          "plan_fault": False}


def recording_prepare(runtime, prompt_id, dynprompt):
    CURRENT["dynprompt"] = dynprompt
    try:
        record = _real_prepare(runtime, prompt_id, dynprompt)
    except BaseException as exc:
        PREPARATIONS.append({"outcome": "raised", "error": type(exc).__name__})
        raise
    PREPARATIONS.append(record)
    return record


def instrumented_execute_once(self, *args, **kwargs):
    result = _real_execute_once(self, *args, **kwargs)
    context = getattr(result[0], "_execution_context", None) or {}
    EDITOR_RUNS.append({"job_id": context.get("queue_job_id", ""), "job_ref_id": context.get("queue_job_ref_id", ""),
                        "prompt": result[5]})
    return result


def instrumented_consume(self, proj):
    CLAIMS.append(proj.project_dir)
    return _real_consume(self, proj)


def instrumented_render(self, proj, scene, start, end, **kwargs):
    if INJECT["render_error"]:
        raise RuntimeError("Injected render failure after the claim")
    progress = kwargs.get("progress")
    if INJECT["interrupt_at"] is not None and progress is not None:
        def interrupting(done, total):
            if done >= INJECT["interrupt_at"] and not mm.processing_interrupted():
                NOTES.append({"interrupt_at": done, "total": total})
                mm.interrupt_current_processing(True)
            return progress(done, total)
        kwargs["progress"] = interrupting
    if INJECT["ram_release_in_render"]:
        cache = CURRENT["executor"].caches.outputs
        NOTES.append({"ram_release_in_render": cache.ram_release(float("inf"), free_active=True)})
    return _real_render(self, proj, scene, start, end, **kwargs)


def instrumented_save_project(proj, *args, **kwargs):
    if INJECT["conflicts"] > 0 and CLAIMS and kwargs.get("notify") is not False:
        INJECT["conflicts"] -= 1
        raise editor_node.ProjectVersionConflict(project_dir=proj.project_dir, expected_modified_at="x",
                                                 actual_modified_at="y")
    return _real_save_project(proj, *args, **kwargs)


_real_plan = pipeline.plan_substitutions


def faulty_plan(*args, **kwargs):
    if INJECT["plan_fault"]:
        raise ValueError("Injected planner fault")
    return _real_plan(*args, **kwargs)


PROGRESS_CALLS = []
_real_start_progress = comfy_progress.ProgressRegistry.start_progress
_real_finish_progress = comfy_progress.ProgressRegistry.finish_progress


def recording_start_progress(self, node_id):
    PROGRESS_CALLS.append(("start", node_id))
    return _real_start_progress(self, node_id)


def recording_finish_progress(self, node_id):
    PROGRESS_CALLS.append(("finish", node_id))
    return _real_finish_progress(self, node_id)


comfy_progress.ProgressRegistry.start_progress = recording_start_progress
comfy_progress.ProgressRegistry.finish_progress = recording_finish_progress
pipeline.prepare = recording_prepare
pipeline.plan_substitutions = faulty_plan
SonderEditor._execute_once = instrumented_execute_once
SonderEditor._consume_queue_job = instrumented_consume
SonderEditor._render_scene_frames = instrumented_render
editor_node.save_project = instrumented_save_project

CACHE = {"classic": (execution.CacheType.CLASSIC, {}), "lru": (execution.CacheType.LRU, {"lru": 10}),
         "ram": (execution.CacheType.RAM_PRESSURE, {}), "none": (execution.CacheType.NONE, {})}[MODE]


def new_executor():
    cache_type, args = CACHE
    executor = execution.PromptExecutor(SERVER, cache_type=cache_type, cache_args={"ram": 0, "ram_inactive": 0, **args})
    CURRENT["executor"] = executor
    return executor


def set_pipeline(enabled):
    hook.uninstall_for_tests()
    if enabled:
        state = hook.install(execution, prompt_server=SERVER)
        assert state["state"] == "ok", state


# --- Fixtures --------------------------------------------------------------------------------

def write_clip(path, frames=8):
    import subprocess
    ffmpeg = sonder("server.media_helpers").get_ffmpeg_path()
    subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-f", "rawvideo", "-pixel_format", "rgb24",
                    "-video_size", "64x64", "-framerate", "24", "-i", "pipe:0", "-c:v", "ffv1",
                    "-pix_fmt", "bgr0", str(path)],
                   input=bytes([200, 40, 40]) * 64 * 64 * frames, capture_output=True, check=True, timeout=60,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def fixture(label, jobs, fps=24, frames=8, guides=None, clip=False):
    """A project with one scene and the given queued jobs; returns its identity."""
    base = Path(folder_paths.get_output_directory()) / "sonder-projects"
    base.mkdir(exist_ok=True)
    project = manager.create_project(f"ZZ-CachePipeline-{label}-{uuid.uuid4().hex[:6]}", fps=fps, width=64,
                                     height=64, base_dir=str(base))
    project = manager.load_project(project.project_dir)
    project.fps = fps  # an integer project fps exercises the Editor's normalisation
    scene = timeline.Scene(name="Cache pipeline fixture", duration_frames=frames, width=64, height=64)
    scene.set_global_prompt("Live prompt deliberately differs from the frozen jobs.")
    project.scenes = [scene]
    project.active_scene_id = scene.scene_id
    if clip:
        # A real clip: an empty scene renders zeros at once, with no per-frame progress.
        media = Path(project.project_dir) / "media"
        media.mkdir(exist_ok=True)
        write_clip(media / "timeline.mkv", frames)
        video = timeline.Asset(name="timeline.mkv", path="media/timeline.mkv", asset_type="video", width=64,
                               height=64, frame_count=frames, fps=24, color_space="rgb", color_range="pc",
                               color_probed=True)
        project.assets.append(video)
        scene.asset_ids = [video.asset_id]
        scene.clips = [timeline.ClipReference(source_path="media/timeline.mkv", timeline_end_frame=frames,
                                              source_out_frame=frames, total_source_frames=frames)]
    if guides:
        from PIL import Image
        media = Path(project.project_dir) / "media"
        media.mkdir(exist_ok=True)
        Image.new("RGB", (16, 16), (255, 0, 0)).save(media / "guide.png")
        asset = timeline.Asset(name="guide.png", path="media/guide.png", asset_type="image", width=16, height=16)
        project.assets.append(asset)
        scene.guide_frames = [timeline.GuideFrame(frame_index=index, asset_id=asset.asset_id) for index in guides]
    project.generation_queue = [
        timeline.GenerationJob(job_id=f"{label}-{index + 1}", scene_id=scene.scene_id, selection_start=0,
                               selection_end=job.get("end", 2), prompt=job["text"],
                               params={"snapshot_version": job.get("snapshot", 1)},
                               scene_width=job.get("width", 64), scene_height=64)
        for index, job in enumerate(jobs)
    ]
    manager.save_project(project)
    return {"project": Path(project.project_dir).name, "dir": project.project_dir, "scene": scene.scene_id}


def jobs_of(fx):
    project = manager.load_project(fx["dir"])
    return {job.job_id: {"status": job.status, "error": (job.error or "")[:200]} for job in project.generation_queue}


def n(class_type, **inputs):
    return {"class_type": class_type, "inputs": inputs}


def editor(fx, **extra):
    inputs = dict(project=fx["project"], project_name="Unused", fps=24.0, width=64, height=64, scene_id=fx["scene"],
                  selection_start=0, selection_end=2, render_queue_active=True, render_cache_max_bytes=0)
    inputs.update(extra)
    return n("SonderEditor", **inputs)


def save(project, frames, embed=False, mark=True):
    return n("SonderSaveVideo", project=project, frames=frames, filename_prefix="cph", fps=24.0, mode="Video",
             place_audio_on_timeline=False, mark_queue_complete=mark, embed_metadata=embed,
             color_drift_correction=False)


def core(fx, seed=17, ed="1", tap=False, **editor_extra):
    graph = {
        ed: editor(fx, **editor_extra),
        "2": n("CPText", value=[ed, 5]), "2p": n("PreviewAny", source=["2", 0]),
        "3": n("CPInt", value=[ed, 8]), "3p": n("PreviewAny", source=["3", 0]),
        "4": n("CPImage", text=["2", 0], width=["3", 0], seed=seed),
        "7": save([ed, 0], ["4", 0]),
    }
    if tap:
        # A rendered_frames consumer: without one the Editor does not render.
        graph["tap"] = n("CPImageTap", frames=[ed, 1])
        graph["tapp"] = n("PreviewAny", source=["tap", 0])
    return graph


# --- Running prompts -----------------------------------------------------------------------------

async def run(executor, graph, targets=None, client=True, trusted=True, extra=None):
    prompt = copy.deepcopy(graph)
    submitted = copy.deepcopy(graph)
    prompt_id = str(uuid.uuid4())
    CURRENT["dynprompt"] = None
    for store in (PREPARATIONS, EDITOR_RUNS, CLAIMS, NOTES, PROGRESS_CALLS):
        store.clear()
    CALLS.clear()
    SERVER.events.clear()
    valid = await execution.validate_prompt(prompt_id, prompt, targets)
    if not valid[0]:
        return {"invalid": json.dumps(valid[1:], default=str)[:2000]}
    extra_data = {"client_id": "c"} if client else {}
    extra_data.update(extra or {})
    if trusted:
        SERVER.prompt_queue.put((0, prompt_id, prompt, extra_data, valid[2], {}))
        item, item_id = SERVER.prompt_queue.get(timeout=1)
        await executor.execute_async(item[2], item[1], item[3], item[4])
        SERVER.prompt_queue.task_done(item_id, executor.history_result, status=execution.PromptQueue.ExecutionStatus(
            status_str="success" if executor.success else "error", completed=executor.success,
            messages=executor.status_messages))
    else:
        await executor.execute_async(prompt, prompt_id, extra_data, valid[2])
    messages = executor.status_messages
    cached = next((data["nodes"] for event, data in messages if event == "execution_cached"), [])
    error = next(({"event": event, "node": data.get("node_id"), "type": data.get("exception_type", ""),
                   "message": (data.get("exception_message") or "")[:300]}
                  for event, data in messages if event in ("execution_error", "execution_interrupted")), None)
    executing = [data.get("node") for event, data, _ in SERVER.events if event == "executing" and data.get("node")]
    outputs = {node_id: info.get("text", [None])[0] if isinstance(info, dict) else None
               for node_id, info in (executor.history_result or {}).get("outputs", {}).items()}
    return {
        "prompt_id": prompt_id, "success": executor.success, "error": error, "cached": sorted(cached),
        "executing": executing, "outputs": outputs, "calls": dict(CALLS), "preparations": list(PREPARATIONS),
        "editor_runs": list(EDITOR_RUNS), "claims": len(CLAIMS), "notes": list(NOTES),
        "progress_calls": list(PROGRESS_CALLS),
        "prompt_unchanged": prompt == submitted or _without_is_changed(prompt) == submitted,
        "last_node_id": SERVER.last_node_id, "events": [event for event, _, _ in SERVER.events],
        "progress_state": {node_id: str(state.get("state")) for node_id, state in
                           (getattr(get_progress_state(), "nodes", {}) or {}).items()},
    }


def _without_is_changed(prompt):
    return {node_id: {key: value for key, value in node.items() if key != "is_changed"} for node_id, node in prompt.items()}


def _diff(left, right, path=""):
    """The differing leaves of two JSON-like values, for a readable failure."""
    if isinstance(left, dict) and isinstance(right, dict):
        found = {}
        for key in sorted(set(left) | set(right)):
            found.update(_diff(left.get(key), right.get(key), f"{path}.{key}"))
        return found
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        found = {}
        for index, (a, b) in enumerate(zip(left, right)):
            found.update(_diff(a, b, f"{path}[{index}]"))
        return found
    return {} if left == right else {path: [left, right]}


def prep(row):
    return row["preparations"][0] if row.get("preparations") else {}


def ran(row, label, node):
    return row["calls"].get(f"{label}:{node}", 0) > 0


CHECKS = []


def check(case, name, ok, detail=None):
    CHECKS.append({"mode": MODE, "case": case, "check": name, "ok": bool(ok),
                   "detail": json.loads(json.dumps(detail, default=str)) if detail is not None else None})


def reset_injections():
    INJECT.update(conflicts=0, render_error=False, interrupt_at=None, ram_release_in_render=False, plan_fault=False)
    mm.interrupt_current_processing(False)


# --- Cases ---------------------------------------------------------------------------------------

STUB = {}
EXACT_SLOTS = [slot for slot, name in enumerate(SonderEditor.RETURN_NAMES)
               if sonder("nodes.cache_policy").output_policy(SonderEditor, name) == "EXACT"]
SLOT_KIND = {slot: SonderEditor.RETURN_TYPES[slot] for slot in EXACT_SLOTS}
BASE_VALUES = {3: "0,8", 4: "1.0,0.5", 5: "a prompt", 6: 1, 7: 24, 8: 64, 9: 64, 11: 0.25, 12: -0.0}
CHANGED_VALUES = {3: "0,4", 4: "0.5,0.5", 5: "another prompt", 6: 17, 7: 30.0, 8: 72, 9: 56, 11: 0.5, 12: 1.75}


def stub_execute_once(self, project, *args, **kwargs):
    import torch
    EDITOR_RUNS.append({"stub": True})
    values = dict(BASE_VALUES)
    values.update(STUB)
    frames = torch.zeros(2, 16, 16, 3)
    audio = {"waveform": torch.zeros(1, 2, 8), "sample_rate": 48000}
    return ("PROJECT", frames, frames, values[3], values[4], values[5], values[6], values[7], values[8], values[9],
            audio, values[11], values[12])


def acceptance_graph():
    graph = {"1": n("SonderEditor", project="+ Create New", project_name="Unused", fps=24.0, width=64, height=64)}
    probe = {"STRING": "CPText", "INT": "CPInt", "FLOAT": "CPFloat"}
    for slot in EXACT_SLOTS:
        graph[f"c{slot}"] = n(probe[SLOT_KIND[slot]], value=["1", slot])
        graph[f"c{slot}p"] = n("PreviewAny", source=[f"c{slot}", 0])
    # Real consumers: native V1 on width/height, native V3 on prompt.
    graph["lat"] = n("EmptyLatentImage", width=["1", 8], height=["1", 9], batch_size=1)
    graph["latp"] = n("PreviewAny", source=["lat", 0])
    graph["cat"] = n("StringConcatenate", string_a=["1", 5], string_b="!", delimiter="")
    graph["catp"] = n("PreviewAny", source=["cat", 0])
    # A wildcard consumer and a media consumer: accepted misses.
    graph["wild"] = n("PreviewAny", source=["1", 5])
    graph["tap"] = n("CPImageTap", frames=["1", 1])
    graph["tapp"] = n("PreviewAny", source=["tap", 0])
    # Two identical V3 consumers that bake their id.
    graph["id1"] = n("CPIdV3", text=["1", 5])
    graph["id1p"] = n("PreviewAny", source=["id1", 0])
    graph["id2"] = n("CPIdV3", text=["1", 5])
    graph["id2p"] = n("PreviewAny", source=["id2", 0])
    return graph


def consumers_of(slot):
    found = {f"c{slot}"}
    if slot in (8, 9):
        found.add("lat")
    if slot == 5:
        found.add("cat")
    return found


# Two identical V3 consumers that bake their id share one cache entry within a
# prompt (upstream, bug tracker): asserted on their own, never as plain consumers.
ID_LESS = ("id1", "id2")


async def acceptance_sequence(enabled):
    """identical, identical, then one changed slot at a time (each followed by its revert)."""
    set_pipeline(enabled)
    SonderEditor._execute_once = stub_execute_once
    executor = new_executor()
    rows = []
    try:
        STUB.clear()
        rows.append(("base", await run(executor, acceptance_graph())))
        rows.append(("identical", await run(executor, acceptance_graph())))
        for slot in EXACT_SLOTS:
            STUB.clear()
            STUB[slot] = CHANGED_VALUES[slot]
            rows.append((f"changed:{slot}", await run(executor, acceptance_graph())))
            STUB.clear()
            rows.append((f"reverted:{slot}", await run(executor, acceptance_graph())))
    finally:
        SonderEditor._execute_once = instrumented_execute_once
        STUB.clear()
    return rows


def executed_nodes(row):
    return set(row["executing"])


async def case_acceptance():
    case = "acceptance"
    pipeline_rows = await acceptance_sequence(True)
    stock_rows = await acceptance_sequence(False)
    all_consumers = {node for slot in EXACT_SLOTS for node in consumers_of(slot)}
    for (label, row), (_, stock) in zip(pipeline_rows, stock_rows):
        check(case, f"{label}: pipeline run succeeds", row.get("success"), row.get("error") or row.get("invalid"))
        # Differential oracle: every preview equals the stock arm's, except the
        # V3 id-less consumers, whose sharing differs by design (asserted below).
        compared = {node: value for node, value in row["outputs"].items() if node not in ("id1p", "id2p")}
        stock_compared = {node: value for node, value in stock["outputs"].items() if node not in ("id1p", "id2p")}
        check(case, f"{label}: differential oracle (every consumer equals stock)", compared == stock_compared,
              {"pipeline": compared, "stock": stock_compared} if compared != stock_compared else None)
        check(case, f"{label}: stock arm reruns every Editor consumer", all_consumers <= executed_nodes(stock),
              sorted(all_consumers - executed_nodes(stock)))
    if MODE == "none":
        check(case, "--cache-none bypasses the pipeline", all(not row["preparations"] for _, row in pipeline_rows),
              [row["preparations"] for _, row in pipeline_rows][:2])
        check(case, "--cache-none reruns every consumer", all(all_consumers <= executed_nodes(row)
                                                              for _, row in pipeline_rows))
        return
    base, identical = pipeline_rows[0][1], pipeline_rows[1][1]
    check(case, "the first run prepares", prep(base).get("outcome") == "prepared", prep(base))
    check(case, "an identical second job hits every EXACT consumer", not (all_consumers & executed_nodes(identical)),
          sorted(all_consumers & executed_nodes(identical)))
    check(case, "the Editor body ran once per prompt", all(len(row["editor_runs"]) == 1 for _, row in pipeline_rows),
          [len(row["editor_runs"]) for _, row in pipeline_rows])
    check(case, "wildcard and media consumers still rerun (accepted misses)",
          {"wild", "tap"} <= executed_nodes(identical), identical["executing"])
    check(case, "integer project fps is normalised and its consumer caches",
          "c7" not in executed_nodes(identical) and identical["outputs"].get("c7p") == "24.0", identical["outputs"].get("c7p"))
    check(case, "-0.0 is canonicalised to 0.0", base["outputs"].get("c12p") == "0.0", base["outputs"].get("c12p"))
    for index, slot in enumerate(EXACT_SLOTS):
        changed = pipeline_rows[2 + 2 * index][1]
        reverted = pipeline_rows[3 + 2 * index][1]
        mine = consumers_of(slot)
        others = all_consumers - mine
        check(case, f"slot {slot}: a change misses exactly its consumers",
              mine <= executed_nodes(changed) and not (others & executed_nodes(changed)),
              {"ran": sorted(executed_nodes(changed) & all_consumers), "expected": sorted(mine)})
        check(case, f"slot {slot}: reverting hits again (classic) or reruns only its consumers",
              not (others & executed_nodes(reverted)), sorted(executed_nodes(reverted) & all_consumers))
        if slot == 5:
            check(case, "slot 5: the V3 id-less pair reruns (as one shared entry)",
                  bool(set(ID_LESS) & executed_nodes(changed)), changed["executing"])
    # V3 consumers that bake their id: stock shares one entry within a prompt (every
    # Editor consumer carries the same NaN), the pipeline extends it across prompts.
    stock_base = stock_rows[0][1]
    check(case, "V3 id-less: stock already shares the entry within a prompt (upstream)",
          stock_base["outputs"].get("id1p") == stock_base["outputs"].get("id2p"),
          [stock_base["outputs"].get("id1p"), stock_base["outputs"].get("id2p")])
    check(case, "V3 id-less: the pipeline shares it across prompts too (upstream, extended)",
          identical["outputs"].get("id1p") == identical["outputs"].get("id2p") == base["outputs"].get("id1p")
          and not ({"id1", "id2"} & executed_nodes(identical)),
          [base["outputs"].get("id1p"), identical["outputs"].get("id1p"), identical["outputs"].get("id2p")])


async def case_fail_open():
    case = "fail_open"
    set_pipeline(True)
    SonderEditor._execute_once = stub_execute_once
    try:
        def graph_with(**extra):
            graph = acceptance_graph()
            graph.update(extra)
            return graph

        two = graph_with(**{"9": n("SonderEditor", project="+ Create New", project_name="Unused", fps=24.0,
                                   width=64, height=64), "9c": n("CPText", value=["9", 5]), "9p": n("PreviewAny", source=["9c", 0])})
        linked = graph_with(src=n("StringConcatenate", string_a="s", string_b="x", delimiter=""))
        linked["1"]["inputs"]["scene_id"] = ["src", 0]
        flagged = acceptance_graph()
        flagged["c5"]["is_changed"] = [0.5]
        dynamic = graph_with(dyn=n("CPDyn", text=["c5", 0]), dynp=n("PreviewAny", source=["dyn", 0]))
        cases = [("two Editors", two, True, "editor_count"), ("linked Editor input", linked, True, "linked_editor_input"),
                 ("pre-populated is_changed", flagged, True, "is_changed"), ("unknown scope", dynamic, True, "scope_unknown"),
                 ("untrusted prompt", acceptance_graph(), False, "untrusted")]
        for label, graph, trusted, reason in cases:
            executor = new_executor()
            first = await run(executor, graph, trusted=trusted)
            second = await run(executor, graph, trusted=trusted)
            check(case, f"{label}: fails open with reason {reason}",
                  prep(first).get("reason") == reason and prep(first).get("outcome") == "skipped", prep(first))
            check(case, f"{label}: takes the stock path (consumers rerun)", "c5" in executed_nodes(second)
                  and second["success"], {"executing": second["executing"], "error": second["error"]})
    finally:
        SonderEditor._execute_once = instrumented_execute_once


async def case_tokens():
    case = "tokens"
    results = {}
    for enabled in (True, False):
        set_pipeline(enabled)
        fx = fixture(f"token-{int(enabled)}", [{"text": "Token A"}] * 2)
        graph = core(fx)
        graph["1"]["_meta"] = {"title": "Sonder Editor", "sonder_run": "never-registered-token"}
        results[enabled] = (await run(new_executor(), graph), jobs_of(fx))
    (row, jobs), (stock, stock_jobs) = results[True], results[False]
    check(case, "an unknown SDK token is refused at the Editor, as stock", row["error"] and stock["error"]
          and row["error"]["node"] == stock["error"]["node"] == "1"
          and row["error"]["message"] == stock["error"]["message"], {"pipeline": row["error"], "stock": stock["error"]})
    check(case, "no claim and no job touched", row["claims"] == 0 and all(job["status"] == "pending" for job in jobs.values())
          and jobs == {key.replace("token-0", "token-1"): value for key, value in stock_jobs.items()},
          {"jobs": jobs, "claims": row["claims"]})
    check(case, "the refusal is replayed, never re-run", prep(row).get("outcome") == "editor_failed", prep(row))


async def case_cancel():
    case = "cancel"
    set_pipeline(True)
    fx = fixture("cancel", [{"text": "Cancel A", "end": 6}] * 2, frames=8, clip=True)
    executor = new_executor()
    graph = core(fx, tap=True, selection_end=6)
    for index in range(3):
        graph[f"u{index}"] = n("CPMark", text=f"unique {uuid.uuid4().hex}")
    INJECT["interrupt_at"] = 2
    row = await run(executor, graph)
    reset_injections()
    interrupt_note = next((note for note in row["notes"] if "interrupt_at" in note), None)
    check(case, "the interrupt landed mid-render", interrupt_note and interrupt_note["interrupt_at"] < interrupt_note["total"],
          interrupt_note)
    check(case, "the prompt reports an interrupt", row["error"] and row["error"]["event"] == "execution_interrupted",
          row["error"])
    check(case, "no other node ran after the cancel", not any(key.split(":")[0] in ("mark", "text", "int", "generation")
                                                            for key in row["calls"]), row["calls"])
    check(case, "the claimed job returned to pending", jobs_of(fx)[f"cancel-1"]["status"] == "pending", jobs_of(fx))
    check(case, "the early run stored a cancellation", prep(row).get("outcome") == "cancelled", prep(row))
    following = await run(executor, core(fx, tap=True, selection_end=6))
    check(case, "the executor survives and the next prompt claims the returned job",
          following["success"] and following["editor_runs"] and following["editor_runs"][0]["job_ref_id"] == "cancel-1"
          and jobs_of(fx)["cancel-1"]["status"] == "completed", {"runs": following["editor_runs"], "jobs": jobs_of(fx),
                                                                "error": following["error"]})


async def case_errors():
    case = "errors"
    # An injected fault in pipeline code (after the Editor ran): no replay, no
    # substitution, the stock result through the memo.
    set_pipeline(True)
    fx = fixture("fault", [{"text": "Fault A"}] * 2)
    INJECT["plan_fault"] = True
    row = await run(new_executor(), core(fx))
    reset_injections()
    check(case, "pipeline fault: the prompt succeeds on the stock path", row["success"] and prep(row).get("outcome") == "raised",
          {"error": row["error"], "prep": prep(row)})
    check(case, "pipeline fault: the Editor body and claim ran once (memo)", len(row["editor_runs"]) == 1 and row["claims"] == 1,
          {"runs": row["editor_runs"], "claims": row["claims"]})
    check(case, "pipeline fault: consumers took the linked value", row["outputs"].get("2p") == "Fault A", row["outputs"])

    # A real exhausted claim conflict matches stock.
    results = {}
    for enabled in (True, False):
        set_pipeline(enabled)
        fx = fixture(f"conflict-{int(enabled)}", [{"text": "Conflict A"}] * 2)
        INJECT["conflicts"] = 99
        results[enabled] = (await run(new_executor(), core(fx)), jobs_of(fx))
        reset_injections()
    (row, jobs), (stock, stock_jobs) = results[True], results[False]
    check(case, "exhausted claim conflict: same error at the same node as stock",
          row["error"] and stock["error"] and row["error"]["node"] == stock["error"]["node"]
          and row["error"]["type"] == stock["error"]["type"], {"pipeline": row["error"], "stock": stock["error"]})
    check(case, "exhausted claim conflict: job states equal stock",
          [value["status"] for value in jobs.values()] == [value["status"] for value in stock_jobs.values()],
          {"pipeline": jobs, "stock": stock_jobs})

    # A genuine Editor error after the claim.
    set_pipeline(True)
    fx = fixture("genuine", [{"text": "Genuine A"}] * 2)
    INJECT["render_error"] = True
    executor = new_executor()
    row = await run(executor, core(fx, tap=True))
    reset_injections()
    jobs = jobs_of(fx)
    check(case, "genuine error: reported at the Editor", row["error"] and row["error"]["node"] == "1"
          and "Injected render failure" in row["error"]["message"], row["error"])
    check(case, "genuine error: the job failed, the next is untouched",
          jobs["genuine-1"]["status"] == "failed" and jobs["genuine-2"]["status"] == "pending", jobs)
    check(case, "genuine error: claimed once, never again", row["claims"] == 1, row["claims"])
    calls = row["progress_calls"]
    check(case, "genuine error: the early run finished the Editor's progress before the replay restarted it",
          calls[:2] == [("start", "1"), ("finish", "1")], calls)

    # An independent output node that fails first: stock leaves the job alone; the
    # pipeline already claimed it and returns it to pending with a note.
    results = {}
    for enabled in (True, False):
        set_pipeline(enabled)
        fx = fixture(f"independent-{int(enabled)}", [{"text": "Independent A"}] * 2)
        graph = core(fx)
        graph["f"] = n("CPFail", text="independent")
        results[enabled] = (await run(new_executor(), graph), jobs_of(fx))
    (row, jobs), (stock, stock_jobs) = results[True], results[False]
    check(case, "independent failure: both fail at the failing node", row["error"] and stock["error"]
          and row["error"]["node"] == stock["error"]["node"] == "f", {"pipeline": row["error"], "stock": stock["error"]})
    first_job, stock_first = jobs["independent-1-1"], stock_jobs["independent-0-1"]
    check(case, "independent failure: stock leaves the job untouched", stock_first["status"] == "pending"
          and not stock_first["error"], stock_jobs)
    check(case, "independent failure: the pipeline's job is pending with the note",
          first_job["status"] == "pending" and "before this job completed" in first_job["error"], jobs)


async def case_shapes():
    case = "shapes"
    set_pipeline(True)
    fx = fixture("subgraph", [{"text": "Sub"}] * 3)
    executor = new_executor()
    first, second = await run(executor, core(fx, ed="30:1")), await run(executor, core(fx, ed="30:1"))
    check(case, "flattened Editor id prepares", prep(first).get("outcome") == "prepared" and first["success"], prep(first))
    check(case, "flattened ids: identical second job hits the text chain", "2" not in executed_nodes(second)
          and second["success"], second["executing"])

    fx = fixture("guides", [{"text": "Guides", "end": 4, "snapshot": 0}] * 3, guides=[0, 2])
    graph = core(fx)
    graph.update({
        "gs": n("SonderGuidesBridgeStart", project=["1", 0], iteration_index=0, value_0="start"),
        "gb": n("CPGuideBody", image=["gs", 1], index=["gs", 2], width=["1", 8], previous=["gs", 4]),
        "ge": n("SonderGuidesBridgeEnd", project=["1", 0], flow_control=["gs", 0], value_0=["gb", 0]),
        "gep": n("PreviewAny", source=["ge", 0]),
    })
    set_pipeline(False)
    stock = await run(new_executor(), graph)
    set_pipeline(True)
    adapted = await run(new_executor(), graph)
    bodies = [note for note in adapted["notes"] if "guide_body" in note]
    check(case, "Guides Bridge expansion over an overridden body gives the stock result",
          adapted["success"] and adapted["outputs"].get("gep") == stock["outputs"].get("gep")
          and "|0:" in (stock["outputs"].get("gep") or ""), {"stock": stock["outputs"].get("gep"),
                                                             "pipeline": adapted["outputs"].get("gep"),
                                                             "error": adapted["error"],
                                                             "stock_error": stock.get("error") or stock.get("invalid")})
    check(case, "every executed body, clones included, saw the literal width",
          len(bodies) >= 2 and all(body["width_literal"] for body in bodies), bodies)

    fx = fixture("preview", [{"text": "Preview only"}] * 2)
    executor = new_executor()
    first = await run(executor, core(fx), targets=["2p"])
    second = await run(executor, core(fx), targets=["2p"])
    check(case, "preview-only: the early run peeks, nothing is claimed", prep(first).get("outcome") == "prepared"
          and first["claims"] == 0 and first["editor_runs"] and first["editor_runs"][0]["job_id"] == ""
          and jobs_of(fx)["preview-1"]["status"] == "pending", {"prep": prep(first), "runs": first["editor_runs"]})
    check(case, "preview-only: the Editor is not scheduled after preparation",
          first["executing"].count("1") == 1, first["executing"])
    check(case, "preview-only: an identical second run hits the preview chain", "2" not in executed_nodes(second)
          and "2p" in second["cached"], {"executing": second["executing"], "cached": second["cached"]})


async def case_lazy():
    case = "lazy"
    # The Editor only on an unselected branch: no early run, no claim, as stock.
    for label, build in (
        ("switch", lambda: n("SonderLazySwitch", **{"select": 1, "branches.item0": ["2", 0],
                                                     "branches.item1": ["o", 0]})),
        ("cluster", lambda: n("SonderLazyCluster", select=1, branches=2, lanes=1, b0_l0=["2", 0], b1_l0=["o", 0])),
        ("gate", lambda: n("SonderGate", when_A=False, value_A=["2", 0], when_B=True, value_B=["o", 0])),
    ):
        results = {}
        for enabled in (True, False):
            set_pipeline(enabled)
            fx = fixture(f"lazy-{label}-{int(enabled)}", [{"text": "Lazy A"}] * 2)
            graph = {"1": editor(fx), "2": n("CPText", value=["1", 5]),
                     "o": n("StringConcatenate", string_a="other", string_b="", delimiter=""),
                     "sw": build(), "swp": n("PreviewAny", source=["sw", 0])}
            results[enabled] = (await run(new_executor(), graph), jobs_of(fx))
        (row, jobs), (stock, stock_jobs) = results[True], results[False]
        check(case, f"{label}: Editor on an unselected branch: no early run, no claim",
              prep(row).get("reason") == "not_certain" and row["claims"] == 0 and not row["editor_runs"],
              {"prep": prep(row), "runs": row["editor_runs"], "invalid": row.get("invalid"), "error": row.get("error")})
        check(case, f"{label}: result identical to stock", row.get("outputs") == stock.get("outputs")
              and row.get("success") == stock.get("success"), {"pipeline": row.get("outputs"), "stock": stock.get("outputs")})

    # A switch whose select is a prepared literal and whose branches carry only
    # Editor values hits; with a project-fed branch it misses (accepted miss).
    set_pipeline(True)
    SonderEditor._execute_once = stub_execute_once
    try:
        def switch_graph(project_branch):
            # select is the Editor's frame_count (1), substituted as a literal;
            # the selected branch carries only Editor values.
            graph = {"1": n("SonderEditor", project="+ Create New", project_name="Unused", fps=24.0, width=64, height=64),
                     "b0": n("CPText", value=["1", 5]), "b1": n("CPText", value=["1", 3]),
                     "sw": n("SonderLazySwitch", **{"select": ["1", 6], "branches.item0": ["b0", 0],
                                                    "branches.item1": ["b1", 0]}),
                     "swp": n("PreviewAny", source=["sw", 0])}
            if project_branch:
                # The unselected branch is fed by the project: ComfyUI keys every
                # linked ancestor, lazy or not, so the switch never caches.
                graph["pt"] = n("CPProjectTap", project=["1", 0], anchor=["b0", 0])
                graph["sw"]["inputs"]["branches.item0"] = ["pt", 0]
            return graph

        for project_branch in (False, True):
            executor = new_executor()
            await run(executor, switch_graph(project_branch))
            second = await run(executor, switch_graph(project_branch))
            label = "with a project-fed branch misses" if project_branch else "with Editor-value branches hits"
            hit = "sw" not in executed_nodes(second)
            check(case, f"prepared literal switch {label}", hit != project_branch,
                  {"executing": second["executing"], "error": second.get("error"), "invalid": second.get("invalid")})
    finally:
        SonderEditor._execute_once = instrumented_execute_once


async def case_ram():
    case = "ram"
    set_pipeline(True)
    fx = fixture("evict", [{"text": "Evict A"}] * 2)
    graph = core(fx)
    graph.update({"ev": n("CPEvict", anchor=["2", 0]), "pt": n("CPProjectTap", project=["1", 0], anchor=["ev", 0])})
    row = await run(new_executor(), graph)
    check(case, "eviction mid-prompt: the prompt succeeds", row["success"], row["error"])
    check(case, "eviction mid-prompt: the Editor body and claim ran once each",
          len(row["editor_runs"]) == 1 and row["claims"] == 1, {"runs": row["editor_runs"], "claims": row["claims"]})
    fx = fixture("release", [{"text": "Release A"}] * 2)
    executor = new_executor()
    await run(executor, core(fx))
    INJECT["ram_release_in_render"] = True
    row = await run(executor, core(fx))
    reset_injections()
    check(case, "a forced ram_release during the early render does not raise", row["success"]
          and prep(row).get("outcome") == "prepared", {"error": row["error"], "prep": prep(row), "notes": row["notes"]})


async def case_misc():
    case = "misc"
    set_pipeline(True)
    fx = fixture("noclient", [{"text": "No client"}] * 2)
    row = await run(new_executor(), core(fx), client=False)
    check(case, "no client_id: nothing broadcast, last_node_id unset", "executing" not in row["events"]
          and row["last_node_id"] is None and prep(row).get("outcome") == "prepared", {"events": row["events"],
                                                                                         "last": row["last_node_id"]})

    # A third-party node swallows the re-armed interrupt: the scheduled Editor
    # replays the cancellation and never re-runs its body or claims again.
    fx = fixture("swallow", [{"text": "Swallow A", "end": 6}] * 2, frames=8, clip=True)
    graph = core(fx, tap=True, selection_end=6)
    graph["sw"] = n("CPSwallow", text="swallow")
    INJECT["interrupt_at"] = 2
    row = await run(new_executor(), graph)
    reset_injections()
    check(case, "swallowed interrupt: a node really cleared the re-armed flag",
          any(note.get("swallowed") for note in row["notes"]), row["notes"])
    check(case, "swallowed interrupt: the Editor body and claim ran once", not row["editor_runs"]
          and row["claims"] == 1 and prep(row).get("outcome") == "cancelled",
          {"runs": row["editor_runs"], "claims": row["claims"], "prep": prep(row), "calls": row["calls"]})
    check(case, "swallowed interrupt: the scheduled Editor replays the cancellation", row["error"]
          and row["error"]["event"] == "execution_interrupted" and row["error"]["node"] == "1", row["error"])
    check(case, "swallowed interrupt: the job returned to pending", jobs_of(fx)["swallow-1"]["status"] == "pending",
          jobs_of(fx))

    # Inference mode on the prepared path, as on stock.
    taps = {}
    for enabled in (True, False):
        set_pipeline(enabled)
        fx = fixture(f"inference-{int(enabled)}", [{"text": "Inference"}] * 2)
        row = await run(new_executor(), core(fx, tap=True))
        taps[enabled] = next((note["inference"] for note in row.get("notes", []) if "tap" in note),
                             row.get("invalid") or row.get("error"))
    # The executor already runs the hook inside inference_mode, so this records
    # that assumption; it cannot tell the pipeline's own inference_mode apart.
    check(case, "rendered_frames are inference tensors on both arms (executor assumption)",
          taps[True] is True and taps[False] is True, taps)

    # Provenance: the saved take and the embedded PROMPT keep the submitted graph.
    results = {}
    for enabled in (True, False):
        set_pipeline(enabled)
        fx = fixture(f"provenance-{int(enabled)}", [{"text": "Provenance"}] * 2)
        graph = core(fx)
        graph["7"] = save(["mc", 0], ["4", 0], embed=True)
        graph["mc"] = n("SonderMetadataCollector", project=["1", 0], value_0=["2", 0], label_0="text")
        row = await run(new_executor(), graph)
        project = manager.load_project(fx["dir"])
        takes = [asset for asset in project.assets if asset.asset_type == "video"]
        params = dict(takes[-1].generation_params or {}) if takes else {}
        embedded = b""
        if takes:
            embedded = (Path(fx["dir"]) / takes[-1].path).read_bytes()
        results[enabled] = (row, params, embedded)
    (row, params, embedded), (stock, stock_params, stock_embedded) = results[True], results[False]
    volatile = {"created_at", "exported_at", "scene_id", "prompt_id", "generated_at", "execution_seconds", "render_seconds", "asset_id",
                "take_id", "job_id", "queue_job_id", "queue_job_ref_id", "project_id", "output_path", "seconds"}

    def stable(value):
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items() if key not in volatile and "time" not in key}
        if isinstance(value, list):
            return [stable(item) for item in value]
        if isinstance(value, str) and ("provenance-1" in value or "provenance-0" in value):
            return "<fixture>"
        return value

    check(case, "provenance: both arms saved a take", row["success"] and stock["success"] and params and stock_params,
          {"pipeline": row["error"], "stock": stock["error"]})
    check(case, "provenance: generation_params equal stock (volatile fields aside)",
          stable(params) == stable(stock_params), _diff(stable(params), stable(stock_params)))
    # Save Video embeds compact JSON: the consumer's link, never the literal.
    link, literal = b'"value":["1",5]', b'"value":"Provenance"'
    check(case, "provenance: the embedded PROMPT keeps the consumer's link, not a literal",
          link in embedded and link in stock_embedded and literal not in embedded,
          {"pipeline_has_link": link in embedded, "stock_has_link": link in stock_embedded,
           "pipeline_has_literal": literal in embedded, "bytes": len(embedded)})
    check(case, "provenance: the submitted prompt object is not mutated beyond stock's is_changed",
          row["prompt_unchanged"], None)

    # A prompt with no Sonder nodes stays on the cheap-exit path.
    set_pipeline(True)
    executor = new_executor()
    plain = {"a": n("StringConcatenate", string_a="x", string_b="y", delimiter=""), "ap": n("PreviewAny", source=["a", 0])}
    rows = [await run(executor, plain) for _ in range(5)]
    totals = [prep(row).get("timings_ms", {}).get("total") for row in rows]
    check(case, "non-Sonder prompt: cheap exit, no Editor work", all(prep(row).get("reason") == "no_editor" for row in rows),
          [prep(row) for row in rows][:1])
    NOTES_OUT["non_sonder_hook_ms"] = totals


NOTES_OUT = {}
CASES = {"acceptance": case_acceptance, "fail_open": case_fail_open, "tokens": case_tokens, "cancel": case_cancel,
         "errors": case_errors, "shapes": case_shapes, "lazy": case_lazy, "ram": case_ram, "misc": case_misc}
BY_MODE = {
    "classic": ["acceptance", "fail_open", "tokens", "cancel", "errors", "shapes", "lazy", "misc"],
    "lru": ["acceptance", "cancel"],
    "ram": ["acceptance", "cancel", "errors", "ram"],
    "none": ["acceptance"],
}


async def main():
    for name in BY_MODE[MODE]:
        if ONLY and name not in ONLY:
            continue
        try:
            await CASES[name]()
        except BaseException as exc:  # noqa: BLE001 - a case failure is a result, not a crash
            check(name, "case raised", False, traceback.format_exc()[-3000:])
            if not isinstance(exc, Exception):
                mm.interrupt_current_processing(False)
        finally:
            reset_injections()
            set_pipeline(True)
    Path(out_path).write_text(json.dumps({"mode": MODE, "checks": CHECKS, "notes": NOTES_OUT}, indent=1), encoding="utf-8")


try:
    asyncio.run(main())
finally:
    shutil.rmtree(TMP, ignore_errors=True)
