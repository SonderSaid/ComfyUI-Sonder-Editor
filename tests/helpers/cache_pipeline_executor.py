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
media_carrier = sonder("nodes.media_carrier")
manager = sonder("server.project_manager")
timeline = sonder("server.timeline_state")
from comfy_extras.nodes_preview_any import PreviewAny  # noqa: E402
from comfy_extras.nodes_string import StringConcatenate  # noqa: E402

SonderEditor = editor_node.SonderEditor
CALLS = {}
NOTES = []
RESOLVER_CALLS = {}


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


class CPMediaDigest:
    """A media consumer whose output is a digest of the exact pixels it received."""
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    CATEGORY = "CPH"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"frames": ("IMAGE",)}, "hidden": {"unique_id": "UNIQUE_ID"}}

    def run(self, frames, unique_id=None):
        import hashlib
        count("media", unique_id)
        tensor = frames.detach().cpu().contiguous()
        return (f"{list(tensor.shape)}:{hashlib.sha256(tensor.numpy().tobytes()).hexdigest()[:16]}",)


class CPAudioDigest:
    """An audio consumer whose output is a digest of the exact samples and rate it received."""
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    CATEGORY = "CPH"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"audio": ("AUDIO",)}, "hidden": {"unique_id": "UNIQUE_ID"}}

    def run(self, audio, unique_id=None):
        import hashlib
        count("audio", unique_id)
        wave = audio["waveform"].detach().cpu().contiguous()
        return (f"{audio['sample_rate']}:{list(wave.shape)}:{hashlib.sha256(wave.numpy().tobytes()).hexdigest()[:16]}",)


class CPPixelSum:
    """A frames consumer without UNIQUE_ID: ComfyUI may share its entry across nodes."""
    RETURN_TYPES = ("FLOAT",)
    FUNCTION = "run"
    CATEGORY = "CPH"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"frames": ("IMAGE",)}}

    def run(self, frames):
        count("pixel_sum", "-")
        return (round(float(frames.double().sum()), 6),)


class CPTwo:
    """Consumes two Editor media outputs."""
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    CATEGORY = "CPH"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"frames": ("IMAGE",), "audio": ("AUDIO",)}, "hidden": {"unique_id": "UNIQUE_ID"}}

    def run(self, frames, audio, unique_id=None):
        count("two", unique_id)
        return (f"{list(frames.shape)}|{round(float(frames.double().sum()), 4)}|{round(float(audio['waveform'].double().abs().sum()), 4)}",)


class CPMixWidth:
    """Consumes Editor frames and an Editor number (a carrier link and an M1 literal on one node)."""
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    CATEGORY = "CPH"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"frames": ("IMAGE",), "width": ("INT", {"min": 0, "max": 99999})},
                "hidden": {"unique_id": "UNIQUE_ID"}}

    def run(self, frames, width, unique_id=None):
        count("mix_width", unique_id)
        dynprompt = CURRENT.get("dynprompt")
        node = dynprompt.get_node(unique_id) if dynprompt is not None else {}
        NOTES.append({"mix_width": unique_id, "width_literal": not pipeline.is_link(node.get("inputs", {}).get("width")),
                      "frames_link": node.get("inputs", {}).get("frames")})
        return (f"{list(frames.shape)}:{round(float(frames.double().sum()), 4)}:w{width}",)


class CPPromptKeys:
    """Records the node ids of hidden PROMPT (the submitted graph)."""
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    CATEGORY = "CPH"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"anchor": ("STRING", {"forceInput": True})}, "hidden": {"prompt": "PROMPT"}}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def run(self, anchor, prompt=None):
        NOTES.append({"prompt_keys": sorted(prompt or {})})
        return {"ui": {"text": (anchor,)}, "result": (anchor,)}


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


class CPAfter:
    """Passes frames on after its anchors: a saver that depends on a bridge chain."""
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "run"
    CATEGORY = "CPH"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"image": ("IMAGE",), "a": ("*", {})},
                "optional": {name: ("*", {}) for name in "bcdef"}}

    @classmethod
    def VALIDATE_INPUTS(cls, input_types=None, **kwargs):
        return True

    def run(self, image, a, **anchors):
        return (image,)


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
    "CPDyn": CPDyn, "CPGuideBody": CPGuideBody, "CPIdV3": CPIdV3, "CPAfter": CPAfter,
    "CPMediaDigest": CPMediaDigest,
    # Registered by the pack's __init__ in ComfyUI; the hook plans no carrier without it.
    "SonderMediaCarrier": media_carrier.SonderMediaCarrier,
    "CPAudioDigest": CPAudioDigest, "CPPixelSum": CPPixelSum, "CPTwo": CPTwo, "CPMixWidth": CPMixWidth,
    "CPPromptKeys": CPPromptKeys,
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
          "plan_fault": False, "resolver_fault": None, "interrupt_in": None, "bridge_pass_fault": False}


def recording_prepare(runtime, prompt_id, dynprompt, *args):
    CURRENT["dynprompt"] = dynprompt
    try:
        record = _real_prepare(runtime, prompt_id, dynprompt, *args)
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


WORK = {"render": 0, "mix": 0}
_real_load_audio = SonderEditor._load_scene_audio


def instrumented_load_audio(self, proj, scene, start, end, **kwargs):
    WORK["mix"] += 1
    return _real_load_audio(self, proj, scene, start, end, **kwargs)


def instrumented_render(self, proj, scene, start, end, **kwargs):
    WORK["render"] += 1
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
    if FRAME_WATCH["on"]:
        # Early release: is any earlier render's frame batch still resident when this one starts?
        FRAME_WATCH["alive_at_start"].append(any(not ref.expired() for ref in FRAME_WATCH["refs"]))
    frames = _real_render(self, proj, scene, start, end, **kwargs)
    if FRAME_WATCH["on"]:
        from torch.multiprocessing.reductions import StorageWeakRef
        FRAME_WATCH["refs"].append(StorageWeakRef(frames.untyped_storage()))
    return frames


FRAME_WATCH = {"on": False, "refs": [], "alive_at_start": []}


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
SonderEditor._load_scene_audio = instrumented_load_audio
editor_node.save_project = instrumented_save_project

CACHE = {"classic": (execution.CacheType.CLASSIC, {}), "lru": (execution.CacheType.LRU, {"lru": 10}),
         "ram": (execution.CacheType.RAM_PRESSURE, {}), "none": (execution.CacheType.NONE, {})}[MODE]


def production_ram_args():
    """main.py's RAM defaults: the active headroom 10% of RAM (2-10 GiB), the inactive headroom all of it."""
    import comfy.system_memory
    total = comfy.system_memory.virtual_memory_total()
    return {"ram": min(10.0, max(2.0, total * 0.10 / 1024 ** 3)), "ram_inactive": min(128.0, total / 1024 ** 3)}


def new_executor():
    cache_type, args = CACHE
    base = {"ram": 0, "ram_inactive": 0}
    if MODE == "ram" and os.environ.get("SONDER_CACHE_PIPELINE_RAM_HEADROOM") == "production":
        base = production_ram_args()  # the early release then frees inactive entries, as in production
    executor = execution.PromptExecutor(SERVER, cache_type=cache_type, cache_args={**base, **args})
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
                               params={"snapshot_version": job.get("snapshot", 1), **job.get("params", {})},
                               scene_width=job.get("width", 64), scene_height=64, **job.get("fields", {}))
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


def save(project, frames, embed=False, mark=True, mode="Video"):
    # Only a Take carries the execution context (bridge provenance) into its params.
    return n("SonderSaveVideo", project=project, frames=frames, filename_prefix="cph", fps=24.0, mode=mode,
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
    RESOLVER_CALLS.clear()
    WORK.update(render=0, mix=0)
    sdk_runs_settled.clear()
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
        "progress_calls": list(PROGRESS_CALLS), "resolver_calls": dict(RESOLVER_CALLS),
        "prompt_unchanged": prompt == submitted or _without_is_changed(prompt) == submitted,
        "last_node_id": SERVER.last_node_id, "events": [event for event, _, _ in SERVER.events],
        "progress_state": {node_id: str(state.get("state")) for node_id, state in
                           (getattr(get_progress_state(), "nodes", {}) or {}).items()},
        "history_json": json.dumps(executor.history_result or {}, default=str),
        "node_errors": json.dumps(valid[3] if len(valid) > 3 else {}, default=str)[:4000],
        "displayed": sorted({str(data.get("display_node")) for event, data, _ in SERVER.events
                             if event == "executing" and data.get("node")}),
        "work": dict(WORK),
        "settled": (sdk_runs_settled.get("value") or {}),
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
    INJECT.update(conflicts=0, render_error=False, interrupt_at=None, ram_release_in_render=False, plan_fault=False,
                  resolver_fault=None, interrupt_in=None, bridge_pass_fault=False)
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
    # A wildcard consumer (an accepted miss) and a media consumer (carried, Phase 4a).
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
    check(case, "the wildcard consumer still reruns (accepted miss); the frames consumer is carried and hits",
          "wild" in executed_nodes(identical) and "tap" not in executed_nodes(identical), identical["executing"])
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
    # Phase 4b: the render is deferred, so the cancel lands while set_prompt settles it,
    # which stores the cancellation and rolls the carriers back (none mode: the early run).
    check(case, "the run stored a cancellation (early run, or while settling)",
          prep(row).get("outcome") == "cancelled" or row["settled"].get("outcome") == "rollback",
          {"prep": prep(row), "settled": row["settled"]})
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
    global editor_media_module
    editor_media_module = sonder("nodes.editor_media")
    case = "misc"
    set_pipeline(True)
    fx = fixture("noclient", [{"text": "No client"}] * 2)
    row = await run(new_executor(), core(fx), client=False)
    check(case, "no client_id: nothing broadcast, last_node_id unset", "executing" not in row["events"]
          and row["last_node_id"] is None and prep(row).get("outcome") == "prepared", {"events": row["events"],
                                                                                         "last": row["last_node_id"]})

    # A third-party node swallows the re-armed interrupt: the scheduled Editor
    # replays the cancellation and never re-runs its body or claims again.
    # The render runs in the early run here (deferral off): only then is the cancel
    # re-armed before set_prompt, where this node's IS_CHANGED clears it.
    fx = fixture("swallow", [{"text": "Swallow A", "end": 6}] * 2, frames=8, clip=True)
    graph = core(fx, tap=True, selection_end=6)
    graph["sw"] = n("CPSwallow", text="swallow")
    INJECT["interrupt_at"] = 2
    real_requested = editor_media_module.deferral_requested
    editor_media_module.deferral_requested = lambda unique_id: False
    try:
        row = await run(new_executor(), graph)
    finally:
        editor_media_module.deferral_requested = real_requested
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
    # Deferred (Phase 4b): the cancel lands while settling, after every key (and this
    # node's IS_CHANGED); the stored cancellation and the re-armed flag stop the run,
    # and the Editor body and claim ran once.
    fx = fixture("swallowd", [{"text": "Swallow D", "end": 6}] * 2, frames=8, clip=True)
    graph = core(fx, tap=True, selection_end=6)
    graph["sw"] = n("CPSwallow", text="swallow")
    INJECT["interrupt_at"] = 2
    row = await run(new_executor(), graph)
    reset_injections()
    check(case, "deferred cancel while settling: interrupted, body and claim once, the job returned to pending",
          row["error"] and row["error"]["event"] == "execution_interrupted" and len(row["editor_runs"]) == 1
          and row["claims"] == 1 and row["settled"].get("outcome") == "rollback"
          and jobs_of(fx)["swallowd-1"]["status"] == "pending",
          {"error": row["error"], "runs": row["editor_runs"], "settled": row["settled"], "jobs": jobs_of(fx)})

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


# --- Phase 2: bridges -------------------------------------------------------------------------------

relay_mod = sonder("nodes.prompt_bridge")
masks_mod = sonder("nodes.masks_bridge")
driver_mod = sonder("nodes.driver_bridge")
reference_mod = sonder("nodes.reference_bridge_v3")
policy = sonder("nodes.cache_policy")

BRIDGE_CLASSES = {
    "SonderPromptRelayBridge": relay_mod.SonderPromptRelayBridge,
    "SonderMasksBridge": masks_mod.SonderMasksBridge,
    "SonderDriverSelector": driver_mod.SonderDriverSelector,
    "SonderDriverBridge": driver_mod.SonderDriverBridge,
    "SonderReferenceSelector": reference_mod.SonderReferenceSelector,
    "SonderReferencePromptBridge": reference_mod.SonderReferencePromptBridge,
    "SonderReferenceImageBridge": reference_mod.SonderReferenceImageBridge,
}
nodes.NODE_CLASS_MAPPINGS.update(BRIDGE_CLASSES)
nodes.NODE_CLASS_MAPPINGS["SonderSelector"] = sonder("nodes.selector").SonderSelector
for _cls in BRIDGE_CLASSES.values():
    if hasattr(_cls, "GET_SCHEMA"):
        _cls.GET_SCHEMA()
PREPARED_CLASSES = {name: cls for name, cls in BRIDGE_CLASSES.items() if policy.prepare_policy(cls) is not None}

# The bridges' resolvers, wrapped where each bridge looks them up: counted, and
# stubbed from value tables for the generated acceptance (execute stays real).
RESOLVERS = {"relay": (relay_mod, "build_window_relay_payload"), "masks": (masks_mod, "resolve_mask_times"),
             "driver": (driver_mod, "resolve_driver_ref"), "reference": (reference_mod, "resolve_reference_set"),
             "prompts": (reference_mod, "decode_reference_prompts")}
REAL_RESOLVERS = {key: getattr(module, name) for key, (module, name) in RESOLVERS.items()}
BRIDGE_STUB = {"on": False, "changed": set()}

BRIDGE_VALUES = {
    ("SonderPromptRelayBridge", "global_prompt"): ("global A", "global B"),
    ("SonderPromptRelayBridge", "smart_prompt"): ("[0-2] smart A", "[0-2] smart B"),
    ("SonderPromptRelayBridge", "local_prompts"): ("local A", "local B"),
    ("SonderPromptRelayBridge", "segment_lengths"): ("2", "3"),
    ("SonderMasksBridge", "video_mask_start_time"): (0.25, 0.5),
    ("SonderMasksBridge", "video_mask_end_time"): (0.75, 1.0),
    ("SonderMasksBridge", "audio_mask_start_time"): (0.25, 0.125),
    ("SonderMasksBridge", "audio_mask_end_time"): (0.75, 0.625),
    ("SonderDriverSelector", "has_driver"): (0, 1),
    ("SonderReferenceSelector", "has_reference"): (1, 0),
    ("SonderReferenceSelector", "reference_strength"): (1.0, 0.5),
    ("SonderReferencePromptBridge", "reference_prompt"): ("ref A", "ref B"),
    ("SonderReferencePromptBridge", "reference_names"): ("Hero", "Villain"),
    **{("SonderReferencePromptBridge", f"p{index:02d}"): (f"p{index} A", f"p{index} B") for index in range(1, 17)},
}


def stub_values(class_name):
    cls = PREPARED_CLASSES[class_name]
    return {name: BRIDGE_VALUES[(class_name, name)][1 if (class_name, name) in BRIDGE_STUB["changed"] else 0]
            for slot, name in enumerate(cls.RETURN_NAMES) if (class_name, name) in BRIDGE_VALUES}


def _stub_relay(project):
    v = stub_values("SonderPromptRelayBridge")
    return {**v, "segments": [], "labels_on": False, "source": "stub", "window_start": 0, "window_end": 2}


def _stub_masks(project, edit_video=True, edit_audio=True):
    v = stub_values("SonderMasksBridge")
    return {**v, "edit_video": bool(edit_video), "edit_audio": bool(edit_audio), "video_mask_frames": (0, 1),
            "audio_mask_frames": (0, 1), "frame_count": 2, "fps": 24.0, "frame_count_padding": 0}


def _stub_driver(project, driver_lane_index=0, driver_selector_overrides_json="{}"):
    return {"has_driver": stub_values("SonderDriverSelector")["has_driver"], "driver_idx": 0, "strength": 0.0,
            "source": "stub"}


def _stub_reference(project, reference_lanes="0"):
    v = stub_values("SonderReferenceSelector")
    return {"has_reference": v["has_reference"], "strength": v["reference_strength"], "conflicts": [],
            "lanes": [], "stub": True}


def _stub_prompts(reference_set):
    v = stub_values("SonderReferencePromptBridge")
    return tuple(v[name] for name in PREPARED_CLASSES["SonderReferencePromptBridge"].RETURN_NAMES)


STUBS = {"relay": _stub_relay, "masks": _stub_masks, "driver": _stub_driver, "reference": _stub_reference,
         "prompts": _stub_prompts}


def _counted_resolver(key):
    real = REAL_RESOLVERS[key]

    def resolver(*args, **kwargs):
        RESOLVER_CALLS[key] = RESOLVER_CALLS.get(key, 0) + 1
        if INJECT.get("resolver_fault") == key:
            raise RuntimeError(f"Injected {key} resolver failure")
        if INJECT.get("interrupt_in") == key:
            mm.interrupt_current_processing(True)  # a cancel arriving while this bridge resolves
        return (STUBS[key] if BRIDGE_STUB["on"] else real)(*args, **kwargs)

    return resolver


for _key, (_module, _name) in RESOLVERS.items():
    setattr(_module, _name, _counted_resolver(_key))

_real_select_bridges = pipeline.select_bridges


def faulty_select_bridges(*args, **kwargs):
    if INJECT.get("bridge_pass_fault"):
        raise ValueError("Injected bridge-pass fault")
    return _real_select_bridges(*args, **kwargs)


pipeline.select_bridges = faulty_select_bridges


class StubProject:
    """What the stubbed Editor returns as ``project``: enough context for real bridges."""

    def __init__(self):
        self._execution_context = {"scene_id": "stub", "_render_raster_hw": (16, 16)}
        STUB_PROJECTS.append(self)


STUB_PROJECTS = []


def bridge_stub_execute_once(self, project, *args, **kwargs):
    result = list(stub_execute_once(self, project, *args, **kwargs))
    result[0] = StubProject()
    return tuple(result)


BRIDGE_NODES = {"SonderPromptRelayBridge": "rl", "SonderMasksBridge": "mk", "SonderDriverSelector": "ds",
                "SonderReferenceSelector": "rs", "SonderReferencePromptBridge": "rp"}
PROBE = {"STRING": "CPText", "INT": "CPInt", "FLOAT": "CPFloat"}


def bridge_acceptance_graph():
    graph = {"1": n("SonderEditor", project="+ Create New", project_name="Unused", fps=24.0, width=64, height=64),
             "rl": n("SonderPromptRelayBridge", project=["1", 0]),
             "mk": n("SonderMasksBridge", project=["1", 0], edit_video=True, edit_audio=True),
             "ds": n("SonderDriverSelector", project=["1", 0], driver_lane_index=0,
                     driver_selector_overrides_json="{}"),
             "rs": n("SonderReferenceSelector", project=["1", 0], reference_lanes="0"),
             "rp": n("SonderReferencePromptBridge", reference_set=["rs", 0])}
    for class_name, node in BRIDGE_NODES.items():
        cls = PREPARED_CLASSES[class_name]
        for slot in policy.exact_slots(cls):
            graph[f"{node}{slot}"] = n(PROBE[cls.RETURN_TYPES[slot]], value=[node, slot])
            graph[f"{node}{slot}p"] = n("PreviewAny", source=[f"{node}{slot}", 0])
    return graph


def bridge_exact_outputs():
    """(class, output name, consumer node) for every EXACT output of a prepared class."""
    return [(class_name, cls.RETURN_NAMES[slot], f"{BRIDGE_NODES[class_name]}{slot}")
            for class_name, cls in PREPARED_CLASSES.items() for slot in policy.exact_slots(cls)]


async def bridge_acceptance_sequence(enabled):
    set_pipeline(enabled)
    SonderEditor._execute_once = bridge_stub_execute_once
    BRIDGE_STUB["on"] = True
    executor = new_executor()
    rows = []
    try:
        BRIDGE_STUB["changed"] = set()
        rows.append(("base", await run(executor, bridge_acceptance_graph()), list(STUB_PROJECTS)))
        STUB_PROJECTS.clear()
        rows.append(("identical", await run(executor, bridge_acceptance_graph()), list(STUB_PROJECTS)))
        for class_name, name, _consumer in bridge_exact_outputs():
            BRIDGE_STUB["changed"] = {(class_name, name)}
            STUB_PROJECTS.clear()
            rows.append((f"changed:{class_name}.{name}", await run(executor, bridge_acceptance_graph()), []))
            BRIDGE_STUB["changed"] = set()
            rows.append((f"reverted:{class_name}.{name}", await run(executor, bridge_acceptance_graph()), []))
    finally:
        SonderEditor._execute_once = instrumented_execute_once
        BRIDGE_STUB.update(on=False, changed=set())
        STUB_PROJECTS.clear()
    return rows


async def case_bridges():
    case = "bridges"
    outputs = bridge_exact_outputs()
    check(case, "the value tables cover every EXACT output of every prepared bridge (from the declarations)",
          {(class_name, name) for class_name, name, _ in outputs} == set(BRIDGE_VALUES)
          and set(BRIDGE_NODES) == set(PREPARED_CLASSES),
          {"missing": sorted({(c, nm) for c, nm, _ in outputs} - set(BRIDGE_VALUES)),
           "classes": sorted(PREPARED_CLASSES)})
    pipeline_rows = await bridge_acceptance_sequence(True)
    stock_rows = await bridge_acceptance_sequence(False)
    consumers = {consumer for _, _, consumer in outputs}
    for (label, row, _), (_, stock, _) in zip(pipeline_rows, stock_rows):
        check(case, f"{label}: pipeline run succeeds", row.get("success"), row.get("error") or row.get("invalid"))
        check(case, f"{label}: differential oracle (every consumer equals stock)", row["outputs"] == stock["outputs"],
              _diff(row["outputs"], stock["outputs"]))
        check(case, f"{label}: stock reruns every bridge consumer", consumers <= executed_nodes(stock),
              sorted(consumers - executed_nodes(stock)))
    if MODE == "none":
        check(case, "--cache-none bypasses bridge preparation", all(not row["preparations"] for _, row, _ in pipeline_rows))
        check(case, "--cache-none reruns every bridge consumer",
              all(consumers <= executed_nodes(row) for _, row, _ in pipeline_rows))
        return
    (_, base, _), (_, identical, projects) = pipeline_rows[0], pipeline_rows[1]
    check(case, "the first run prepares all five bridges", prep(base).get("bridges", {}).get("prepared") == 5,
          {"bridges": prep(base).get("bridges"), "reasons": prep(base).get("bridge_reasons")})
    check(case, "an identical second job hits every bridge consumer", not (consumers & executed_nodes(identical)),
          sorted(consumers & executed_nodes(identical)))
    check(case, "bridges whose consumers all took literals are not scheduled",
          not (set(BRIDGE_NODES.values()) & executed_nodes(identical)), identical["executing"])
    check(case, "each bridge resolved once per prompt (prepared, unscheduled)",
          identical["resolver_calls"] == {"relay": 1, "masks": 1, "driver": 1, "reference": 1, "prompts": 1},
          identical["resolver_calls"])
    ctx = projects[0]._execution_context if projects else {}
    check(case, "provenance is published by preparation while the bridges are unscheduled",
          ctx.get("prompt_relay", {}).get("global") == "global A" and "mk" in (ctx.get("masks_bridge_by_node") or {})
          and ctx.get("driver_selector", {}).get("has_driver") == 0, {key: ctx.get(key) for key in
                                                                     ("prompt_relay", "masks_bridge_by_node", "driver_selector")})
    rows = {label: row for label, row, _ in pipeline_rows}
    for class_name, name, consumer in outputs:
        changed = rows[f"changed:{class_name}.{name}"]
        others = consumers - {consumer}
        check(case, f"{class_name}.{name}: a change misses exactly its consumer",
              consumer in executed_nodes(changed) and not (others & executed_nodes(changed)),
              {"ran": sorted(executed_nodes(changed) & consumers), "expected": consumer})
        reverted = rows[f"reverted:{class_name}.{name}"]
        check(case, f"{class_name}.{name}: reverting reruns nothing else",
              not (others & executed_nodes(reverted)), sorted(executed_nodes(reverted) & consumers))


# --- Real bridges on fixture projects ------------------------------------------------------------

def edit_project(fx, change):
    project = manager.load_project(fx["dir"])
    change(project, project.scenes[0])
    manager.save_project(project)


def add_reference(project, scene, override=""):
    from PIL import Image
    media = Path(project.project_dir) / "media"
    media.mkdir(exist_ok=True)
    Image.new("RGB", (16, 16), (0, 0, 255)).save(media / "subject.png")
    asset = timeline.Asset(name="subject.png", path="media/subject.png", asset_type="image", width=16, height=16)
    project.assets.append(asset)
    member = timeline.ReferenceMember(member_id="member", asset_id=asset.asset_id, prompt="blue subject")
    project.references = [timeline.ReferenceEntity(reference_id="entity", name="Hero", members=[member])]
    definition = next(row for row in timeline.REFERENCE_RECIPE_PRESETS if row["media_kind"] == "image")
    scene.reference_lane_count = 1
    scene.reference_lane_configs = [timeline.LaneConfig()]
    scene.reference_lane_recipes = [timeline.ReferenceLaneRecipe(
        media_kind="image", recipe_id=definition["id"],
        recipe={"name": definition["name"], "hard": dict(definition["hard"]), "soft": dict(definition["soft"])})]
    scene.reference_items = [timeline.ReferenceItem(
        reference_item_id="item", lane_index=0, start_frame=0, end_frame=-1,
        members=[{"entity_id": "entity", "member_id": "member"}], prompt_override=override)]


def add_driver(project, scene):
    media = Path(project.project_dir) / "media"
    media.mkdir(exist_ok=True)
    write_clip(media / "driver.mkv", 8)
    asset = timeline.Asset(name="driver.mkv", path="media/driver.mkv", asset_type="video", width=64, height=64,
                           frame_count=8, fps=24, color_space="rgb", color_range="pc", color_probed=True)
    project.assets.append(asset)
    scene.motion_driver_lane_count = 1
    scene.motion_driver_lane_configs = [timeline.LaneConfig()]
    scene.clips.append(timeline.ClipReference(clip_id="driver-1", source_path="media/driver.mkv",
                                              timeline_start_frame=0, timeline_end_frame=8, source_out_frame=8,
                                              total_source_frames=8, track_index=0, role="motion_driver",
                                              strength=0.5))


FROZEN_FIELDS = {"scene_prompt": "frozen global",
                 "prompt_sections": [{"start_frame": 0, "end_frame": 4, "prompt": "frozen walks"}],
                 "scene_fps": 30.0}


def bridge_fixture(label, runs, reference=True, driver=False, guides=None, text="Bridge job", frozen=False,
                   muted_driver=False):
    # The same job text on both arms, so the oracle compares like with like. A frozen
    # job (snapshot 1) carries its own prompt and fps: the queued-run path.
    job = {"text": text, "snapshot": 1 if frozen else 0, "end": 4 if guides else 2}
    if frozen:
        job.update(fields=FROZEN_FIELDS, params={"prompt_channel_labels": False})
    fx = fixture(label, [job] * runs, guides=guides)

    def build(project, scene):
        scene.set_global_prompt("global style")
        scene.prompt_sections = [timeline.PromptSection(0, 4, prompt="walks"), timeline.PromptSection(4, 8, prompt="runs")]
        if reference:
            add_reference(project, scene)
        if driver:
            add_driver(project, scene)
            if muted_driver:
                scene.clips[-1].muted = True

    edit_project(fx, build)
    return fx


REAL_CONSUMERS = {"g", "sm", "lo", "sg", "mvs", "mve", "mas", "mae", "hd", "hr", "st", "rpp", "rpn", "rp1"}


def real_bridge_graph(fx, width=64, edit_audio=True):
    graph = {
        "1": editor(fx, width=width),
        "rl": n("SonderPromptRelayBridge", project=["1", 0]),
        "mk": n("SonderMasksBridge", project=["1", 0], edit_video=True, edit_audio=edit_audio),
        "ds": n("SonderDriverSelector", project=["1", 0], driver_lane_index=0, driver_selector_overrides_json="{}"),
        "rs": n("SonderReferenceSelector", project=["1", 0], reference_lanes="0"),
        "rp": n("SonderReferencePromptBridge", reference_set=["rs", 0]),
        "3": n("CPInt", value=["1", 8]),
        "4": n("CPImage", text=["g", 0], width=["3", 0], seed=17),
        # The saver waits for every bridge's consumer, so stock's order cannot save
        # before a bridge has published (the parity compared here).
        "after": n("CPAfter", image=["4", 0], a=["mae", 0], b=["hd", 0], c=["rpp", 0], d=["hr", 0], e=["st", 0]),
        "7": save(["1", 0], ["after", 0], mode="Take"),
    }
    for node, (source, slot, kind) in {
            "g": ("rl", 0, "STRING"), "sm": ("rl", 1, "STRING"), "lo": ("rl", 2, "STRING"), "sg": ("rl", 3, "STRING"),
            "mvs": ("mk", 0, "FLOAT"), "mve": ("mk", 1, "FLOAT"), "mas": ("mk", 2, "FLOAT"), "mae": ("mk", 3, "FLOAT"),
            "hd": ("ds", 1, "INT"), "hr": ("rs", 1, "INT"), "st": ("rs", 2, "FLOAT"),
            "rpp": ("rp", 0, "STRING"), "rpn": ("rp", 1, "STRING"), "rp1": ("rp", 2, "STRING")}.items():
        graph[node] = n(PROBE[kind], value=[source, slot])
        graph[f"{node}p"] = n("PreviewAny", source=[node, 0])
    return graph


PROVENANCE_KEYS = ("prompt_relay", "masks_bridge", "masks_bridge_by_node", "driver_selector")
VOLATILE = {"created_at", "exported_at", "scene_id", "prompt_id", "generated_at", "execution_seconds", "render_seconds",
            "asset_id", "take_id", "job_id", "queue_job_id", "queue_job_ref_id", "project_id", "output_path", "seconds",
            "base_modified_at"}


def stable_params(value):
    if isinstance(value, dict):
        return {key: stable_params(item) for key, item in value.items() if key not in VOLATILE and "time" not in key}
    if isinstance(value, list):
        return [stable_params(item) for item in value]
    if isinstance(value, str) and "ZZ-CachePipeline-" in value:
        return "<fixture>"
    return value


def last_take_params(fx):
    project = manager.load_project(fx["dir"])
    takes = [asset for asset in project.assets if asset.asset_type == "video"]
    return dict(takes[-1].generation_params or {}) if takes else {}


async def real_bridge_sequence(enabled, label):
    set_pipeline(enabled)
    fx = bridge_fixture(f"{label}-{int(enabled)}", 8)
    executor = new_executor()
    rows = []

    async def step(name, graph):
        row = await run(executor, graph)
        rows.append((name, row, last_take_params(fx)))

    await step("base", real_bridge_graph(fx))
    await step("identical", real_bridge_graph(fx))
    edit_project(fx, lambda project, scene: scene.set_global_prompt("global style, edited"))
    await step("global edit", real_bridge_graph(fx))
    await step("width only", real_bridge_graph(fx, width=72))
    await step("freeze audio", real_bridge_graph(fx, width=72, edit_audio=False))

    def override(project, scene):
        scene.reference_items[0].prompt_override = "custom words"

    edit_project(fx, override)
    await step("prompt override edit", real_bridge_graph(fx, width=72, edit_audio=False))
    return rows


async def case_bridges_snapshot():
    """The real bridges on frozen jobs (the queued-run path): prepared equals scheduled."""
    case = "bridges_snapshot"
    arms = {}
    for enabled in (True, False):
        set_pipeline(enabled)
        fx = bridge_fixture(f"snapshot-{int(enabled)}", 4, frozen=True)
        executor = new_executor()
        rows = []
        for name, graph in (("base", real_bridge_graph(fx)), ("identical", real_bridge_graph(fx)),
                            ("freeze audio", real_bridge_graph(fx, edit_audio=False))):
            row = await run(executor, graph)
            rows.append((name, row, last_take_params(fx)))
        arms[enabled] = rows
    for (label, row, params), (_, stock, stock_params) in zip(arms[True], arms[False]):
        check(case, f"{label}: both arms succeed", row.get("success") and stock.get("success"),
              {"pipeline": row.get("error") or row.get("invalid"), "stock": stock.get("error") or stock.get("invalid")})
        check(case, f"{label}: differential oracle (every consumer equals stock)", row["outputs"] == stock["outputs"],
              _diff(row["outputs"], stock["outputs"]))
        check(case, f"{label}: the frozen job's provenance equals stock",
              (params.get("prompt_relay") or {}).get("source") == "snapshot"
              and (params.get("prompt_relay") or {}).get("global") == "frozen global"
              and stable_params({key: params.get(key) for key in PROVENANCE_KEYS})
              == stable_params({key: stock_params.get(key) for key in PROVENANCE_KEYS}),
              _diff(stable_params({key: params.get(key) for key in PROVENANCE_KEYS}),
                    stable_params({key: stock_params.get(key) for key in PROVENANCE_KEYS})))
        check(case, f"{label}: every bridge is prepared and none is scheduled",
              prep(row).get("bridges", {}).get("prepared") == 5
              and not ({"rl", "mk", "ds", "rs", "rp"} & executed_nodes(row)), prep(row).get("bridge_reasons"))
    identical = arms[True][1][1]
    check(case, "identical frozen job: no bridge consumer reruns", not (executed_nodes(identical) & REAL_CONSUMERS),
          sorted(executed_nodes(identical) & REAL_CONSUMERS))
    check(case, "frozen fps reaches the mask times (30 fps job)",
          arms[True][0][1]["outputs"].get("mvep") == arms[False][0][1]["outputs"].get("mvep")
          and (arms[True][0][2].get("masks_bridge") or {}).get("video_mask") is not None,
          [arms[True][0][1]["outputs"].get("mvep"), (arms[True][0][2].get("masks_bridge") or {}).get("video_mask")])


async def case_bridges_negative():
    """Negative control: if preparation published into another project object, the
    provenance checks must fail. Proves they can."""
    case = "bridges_negative"
    original = pipeline._run_bridge

    def publish_elsewhere(runtime, prompt_id, dynprompt, node_id, class_def, sources):
        sources = dict(sources)
        for key, value in list(sources.items()):
            if value and hasattr(value[0], "_execution_context"):
                clone = copy.copy(value[0])
                clone._execution_context = dict(value[0]._execution_context)
                sources[key] = (clone,) + tuple(value[1:])
        return original(runtime, prompt_id, dynprompt, node_id, class_def, sources)

    pipeline._run_bridge = publish_elsewhere
    try:
        arms = await both_arms(real_bridge_graph, runs=1, label="negative")
    finally:
        pipeline._run_bridge = original
    (row, params), (stock, stock_params) = arms[True][1][0], arms[False][1][0]
    check(case, "with publication diverted, the take loses the bridges' provenance (the check can fail)",
          row["success"] and stock["success"] and "prompt_relay" not in params and "prompt_relay" in stock_params
          and stable_params({key: params.get(key) for key in PROVENANCE_KEYS})
          != stable_params({key: stock_params.get(key) for key in PROVENANCE_KEYS}),
          {"pipeline": sorted(key for key in PROVENANCE_KEYS if key in params),
           "stock": sorted(key for key in PROVENANCE_KEYS if key in stock_params)})


async def case_bridges_real():
    case = "bridges_real"
    pipeline_rows = await real_bridge_sequence(True, "real")
    stock_rows = await real_bridge_sequence(False, "real")
    expected = {"identical": set(), "global edit": {"g"}, "width only": set(), "freeze audio": {"mae"},
                "prompt override edit": {"rpp"}}
    for (label, row, params), (_, stock, stock_params) in zip(pipeline_rows, stock_rows):
        check(case, f"{label}: both arms succeed", row.get("success") and stock.get("success"),
              {"pipeline": row.get("error") or row.get("invalid"), "stock": stock.get("error") or stock.get("invalid")})
        check(case, f"{label}: differential oracle (every consumer equals stock)", row["outputs"] == stock["outputs"],
              _diff(row["outputs"], stock["outputs"]))
        check(case, f"{label}: the saved take's bridge provenance equals stock",
              all(key in params for key in PROVENANCE_KEYS)
              and stable_params({key: params.get(key) for key in PROVENANCE_KEYS})
              == stable_params({key: stock_params.get(key) for key in PROVENANCE_KEYS}),
              _diff(stable_params({key: params.get(key) for key in PROVENANCE_KEYS}),
                    stable_params({key: stock_params.get(key) for key in PROVENANCE_KEYS})))
        check(case, f"{label}: generation_params equal stock (volatile fields aside)",
              stable_params(params) == stable_params(stock_params), _diff(stable_params(params), stable_params(stock_params)))
        check(case, f"{label}: every bridge is prepared and none is scheduled",
              prep(row).get("bridges", {}).get("prepared") == 5
              and not ({"rl", "mk", "ds", "rs", "rp"} & executed_nodes(row)),
              {"bridges": prep(row).get("bridges"), "reasons": prep(row).get("bridge_reasons"),
               "executing": row["executing"]})
        if label in expected:
            ran = executed_nodes(row) & REAL_CONSUMERS
            check(case, f"{label}: exactly the changed bridge outputs' consumers rerun", ran == expected[label],
                  {"ran": sorted(ran), "expected": sorted(expected[label])})


# --- Graph shapes ----------------------------------------------------------------------------------

async def both_arms(build, runs=2, fixture_args=None, label="shape"):
    """Run ``build(fx)`` ``runs`` times per arm on its own fixture; rows per arm."""
    arms = {}
    for enabled in (True, False):
        set_pipeline(enabled)
        fx = bridge_fixture(f"{label}-{int(enabled)}", runs + 1, **(fixture_args or {}))
        executor = new_executor()
        rows = []
        for _ in range(runs):
            row = await run(executor, build(fx))
            rows.append((row, last_take_params(fx)))
        arms[enabled] = (fx, rows)
    return arms


async def case_bridges_shapes():
    case = "bridges_shapes"

    # H3 shape: a Selector that stays scheduled for an Image Bridge feeds a prepared,
    # unscheduled Prompt Bridge.
    def h3(fx):
        return {"1": editor(fx), "rs": n("SonderReferenceSelector", project=["1", 0], reference_lanes="0"),
                "ri": n("SonderReferenceImageBridge", reference_set=["rs", 0], unused_slots="placeholder"),
                "rit": n("CPImageTap", frames=["ri", 0]), "ritp": n("PreviewAny", source=["rit", 0]),
                "rp": n("SonderReferencePromptBridge", reference_set=["rs", 0]),
                "rp1": n("CPText", value=["rp", 2]), "rp1p": n("PreviewAny", source=["rp1", 0]),
                "7": save(["1", 0], ["ri", 0], mode="Take")}

    arms = await both_arms(h3, label="h3")
    (_, rows), (_, stock_rows) = arms[True], arms[False]
    (first, _), (second, _) = rows
    check(case, "H3 shape: results equal stock", all(row["success"] and row["outputs"] == stock["outputs"]
                                                     for (row, _), (stock, _) in zip(rows, stock_rows)),
          [_diff(row["outputs"], stock["outputs"]) for (row, _), (stock, _) in zip(rows, stock_rows)])
    # Phase 3: the Image Bridge takes a token, so no handle consumer keeps the Selector
    # scheduled (Phase 2 left it scheduled, resolving twice).
    check(case, "H3 shape: the Selector, Prompt Bridge and Image Bridge all leave the schedule",
          not ({"rs", "rp", "ri"} & executed_nodes(second)), second["executing"])
    check(case, "H3 shape: the per-member prompt consumer hits on an identical job", "rp1" not in executed_nodes(second),
          second["executing"])
    check(case, "H3 shape: the Selector resolves once, in preparation",
          second["resolver_calls"].get("reference") == 1, second["resolver_calls"])

    # H3 starter shape: only reference_set is consumed, so the Selector is unused.
    def starter(fx):
        return {"1": editor(fx), "rs": n("SonderReferenceSelector", project=["1", 0], reference_lanes="0"),
                "ri": n("SonderReferenceImageBridge", reference_set=["rs", 0], unused_slots="placeholder"),
                "7": save(["1", 0], ["ri", 0], mode="Take")}

    arms = await both_arms(starter, runs=1, label="starter")
    row, _ = arms[True][1][0]
    # Phase 3: the Selector is prepared for the identified Image Bridge, which then
    # takes a token, so the Selector is no longer scheduled: it still resolves once.
    check(case, "H3 starter shape: the Selector is prepared for the Image Bridge's identity and resolves once",
          row["success"] and prep(row).get("bridge_reasons") == {} and prep(row).get("bridges", {}).get("prepared") == 1
          and prep(row).get("identities", {}).get("identified") == 1
          and row["resolver_calls"].get("reference") == 1 and "rs" not in executed_nodes(row),
          {"prep": prep(row), "calls": row["resolver_calls"], "executing": row["executing"]})

    # A Driver Selector whose has_driver is a switch's select: the active branch pulls
    # the Selector back in lazily through the Driver Bridge.
    def lazy_driver(fx):
        return {"1": editor(fx), "ds": n("SonderDriverSelector", project=["1", 0], driver_lane_index=0,
                                          driver_selector_overrides_json="{}"),
                "db": n("SonderDriverBridge", driver_ref=["ds", 0]), "dt": n("CPImageTap", frames=["db", 0]),
                "o": n("StringConcatenate", string_a="no driver", string_b="", delimiter=""),
                "sw": n("SonderLazySwitch", **{"select": ["ds", 1], "branches.item0": ["o", 0],
                                               "branches.item1": ["dt", 0]}),
                "swp": n("PreviewAny", source=["sw", 0]),
                "after": n("CPAfter", image=["1", 1], a=["sw", 0]), "7": save(["1", 0], ["after", 0], mode="Take")}

    arms = await both_arms(lazy_driver, fixture_args={"driver": True, "reference": False}, label="driver")
    (_, rows), (_, stock_rows) = arms[True], arms[False]
    check(case, "lazy Driver branch: has_driver=1 selects the Driver branch, results equal stock",
          all(row["success"] and row["outputs"] == stock["outputs"] for (row, _), (stock, _) in zip(rows, stock_rows))
          and "[" in (rows[0][0]["outputs"].get("swp") or ""),
          [(row["outputs"], stock["outputs"], row.get("error")) for (row, _), (stock, _) in zip(rows, stock_rows)])
    check(case, "lazy Driver branch: the Selector was prepared and the switch took its literal",
          prep(rows[0][0]).get("bridges", {}).get("prepared") == 1, prep(rows[0][0]))
    check(case, "lazy Driver branch: driver provenance equals stock",
          stable_params(rows[0][1].get("driver_selector")) == stable_params(stock_rows[0][1].get("driver_selector"))
          and rows[0][1].get("driver_selector", {}).get("has_driver") == 1,
          [rows[0][1].get("driver_selector"), stock_rows[0][1].get("driver_selector")])

    # A muted Driver clip: has_driver=0 selects the other branch; results and provenance equal stock.
    arms = await both_arms(lazy_driver, fixture_args={"driver": True, "muted_driver": True, "reference": False},
                           label="muted")
    (_, rows), (_, stock_rows) = arms[True], arms[False]
    check(case, "muted Driver: has_driver=0, results and provenance equal stock",
          all(row["success"] and row["outputs"] == stock["outputs"] for (row, _), (stock, _) in zip(rows, stock_rows))
          and rows[0][0]["outputs"].get("swp") == "no driver"
          and stable_params(rows[0][1].get("driver_selector")) == stable_params(stock_rows[0][1].get("driver_selector"))
          and rows[0][1].get("driver_selector", {}).get("has_driver") == 0,
          [(row["outputs"], row.get("error"), params.get("driver_selector")) for (row, params) in rows])

    # has_driver=0 with a literal consumer: the Selector is unscheduled, provenance present.
    def no_driver(fx):
        return {"1": editor(fx), "ds": n("SonderDriverSelector", project=["1", 0], driver_lane_index=0,
                                          driver_selector_overrides_json="{}"),
                "hd": n("CPInt", value=["ds", 1]), "hdp": n("PreviewAny", source=["hd", 0]),
                "7": save(["1", 0], ["1", 1], mode="Take")}

    arms = await both_arms(no_driver, label="nodriver", fixture_args={"reference": False})
    (_, rows), (_, stock_rows) = arms[True], arms[False]
    # The saver does not depend on the Selector, so stock can save before it runs;
    # the prepared provenance is there either way (a documented gain).
    check(case, "has_driver=0: the Selector is unscheduled, its provenance present (stock: equal or absent)",
          "ds" not in executed_nodes(rows[1][0]) and (rows[1][1].get("driver_selector") or {}).get("has_driver") == 0
          and stock_rows[1][1].get("driver_selector") in (None, rows[1][1].get("driver_selector")),
          {"executing": rows[1][0]["executing"], "pipeline": rows[1][1].get("driver_selector"),
           "stock": stock_rows[1][1].get("driver_selector")})

    # A bridge with a flattened subgraph id.
    def subgraph(fx):
        return {"1": editor(fx), "40:4": n("SonderPromptRelayBridge", project=["1", 0]),
                "40:5": n("CPText", value=["40:4", 0]), "40:5p": n("PreviewAny", source=["40:5", 0]),
                "after": n("CPAfter", image=["1", 1], a=["40:5", 0]), "7": save(["1", 0], ["after", 0], mode="Take")}

    arms = await both_arms(subgraph, label="subgraph")
    (_, rows), (_, stock_rows) = arms[True], arms[False]
    check(case, "subgraph id: prepared, hits on an identical job, equal to stock",
          "40:5" not in executed_nodes(rows[1][0]) and rows[1][0]["outputs"] == stock_rows[1][0]["outputs"]
          and stable_params(rows[1][1].get("prompt_relay")) == stable_params(stock_rows[1][1].get("prompt_relay")),
          {"executing": rows[1][0]["executing"], "outputs": [rows[1][0]["outputs"], stock_rows[1][0]["outputs"]]})

    # A Guides loop body consumer cloned with a bridge literal.
    def guides(fx):
        graph = core(fx)
        graph["7"] = save(["1", 0], ["4", 0], mode="Take")
        graph.update({
            "ds": n("SonderDriverSelector", project=["1", 0], driver_lane_index=0, driver_selector_overrides_json="{}"),
            "gs": n("SonderGuidesBridgeStart", project=["1", 0], iteration_index=0, value_0="start"),
            "gb": n("CPGuideBody", image=["gs", 1], index=["gs", 2], width=["ds", 1], previous=["gs", 4]),
            "ge": n("SonderGuidesBridgeEnd", project=["1", 0], flow_control=["gs", 0], value_0=["gb", 0]),
            "gep": n("PreviewAny", source=["ge", 0]),
        })
        return graph

    arms = await both_arms(guides, runs=1, label="guides", fixture_args={"reference": False, "guides": [0, 2]})
    (row, _), (stock, _) = arms[True][1][0], arms[False][1][0]
    bodies = [note for note in row["notes"] if "guide_body" in note]
    check(case, "Guides loop: every body clone saw the bridge literal and the result equals stock",
          row["success"] and row["outputs"].get("gep") == stock["outputs"].get("gep") and len(bodies) >= 2
          and all(body["width_literal"] for body in bodies),
          {"pipeline": row["outputs"].get("gep"), "stock": stock["outputs"].get("gep"), "bodies": bodies,
           "error": row.get("error")})

    # Two Masks Bridges: both prepared, both recorded by node.
    def two_masks(fx):
        graph = {"1": editor(fx), "after": n("CPAfter", image=["1", 1], a=["m1c", 0], b=["m2c", 0]),
                 "7": save(["1", 0], ["after", 0], mode="Take")}
        for node, audio in (("m1", True), ("m2", False)):
            graph[node] = n("SonderMasksBridge", project=["1", 0], edit_video=True, edit_audio=audio)
            graph[f"{node}c"] = n("CPFloat", value=[node, 3])
            graph[f"{node}p"] = n("PreviewAny", source=[f"{node}c", 0])
        return graph

    arms = await both_arms(two_masks, runs=1, label="twomasks", fixture_args={"reference": False})
    (row, params), (stock, stock_params) = arms[True][1][0], arms[False][1][0]
    check(case, "two Masks Bridges: both recorded by node, equal to stock",
          row["success"] and set(params.get("masks_bridge_by_node") or {}) == {"m1", "m2"}
          and stable_params(params.get("masks_bridge_by_node")) == stable_params(stock_params.get("masks_bridge_by_node"))
          and row["outputs"] == stock["outputs"],
          {"pipeline": params.get("masks_bridge_by_node"), "stock": stock_params.get("masks_bridge_by_node")})


# --- Fail-open and safety ---------------------------------------------------------------------------

async def case_bridges_safety():
    case = "bridges_safety"

    def reference_graph(fx):
        return {"1": editor(fx), "rs": n("SonderReferenceSelector", project=["1", 0], reference_lanes="0"),
                "hr": n("CPInt", value=["rs", 1]), "hrp": n("PreviewAny", source=["hr", 0]),
                "7": save(["1", 0], ["1", 1], mode="Take")}

    # A bridge that raises: stock's error at the scheduled bridge, nothing substituted.
    results = {}
    for enabled in (True, False):
        set_pipeline(enabled)
        fx = bridge_fixture(f"refail-{int(enabled)}", 2)
        INJECT["resolver_fault"] = "reference"
        results[enabled] = (await run(new_executor(), reference_graph(fx)), jobs_of(fx))
        reset_injections()
    (row, jobs), (stock, stock_jobs) = results[True], results[False]
    check(case, "a failing bridge: the same error at the same bridge as stock",
          row["error"] and stock["error"] and row["error"]["node"] == stock["error"]["node"] == "rs"
          and row["error"]["message"] == stock["error"]["message"], {"pipeline": row["error"], "stock": stock["error"]})
    check(case, "a failing bridge: recorded as bridge_failed, nothing stored, job states equal stock",
          prep(row).get("bridge_reasons") == {"bridge_failed": 1}
          and [job["status"] for job in jobs.values()] == [job["status"] for job in stock_jobs.values()],
          {"prep": prep(row), "jobs": jobs, "stock": stock_jobs})

    # Masks with a linked latent: not prepared; its consumers rerun; results equal stock.
    def linked_masks(fx):
        return {"1": editor(fx), "lat": n("EmptyLatentImage", width=64, height=64, batch_size=1),
                "mk": n("SonderMasksBridge", project=["1", 0], edit_video=True, edit_audio=True, video_latent=["lat", 0]),
                "mve": n("CPFloat", value=["mk", 1]), "mvep": n("PreviewAny", source=["mve", 0]),
                "7": save(["1", 0], ["1", 1], mode="Take")}

    arms = await both_arms(linked_masks, label="linkedmasks", fixture_args={"reference": False})
    (_, rows), (_, stock_rows) = arms[True], arms[False]
    check(case, "Masks with a linked latent: not prepared, its consumer reruns, results and provenance equal stock",
          prep(rows[1][0]).get("bridge_reasons") == {"linked_input": 1} and "mve" in executed_nodes(rows[1][0])
          and rows[1][0]["outputs"] == stock_rows[1][0]["outputs"]
          and stable_params(rows[1][1].get("masks_bridge")) == stable_params(stock_rows[1][1].get("masks_bridge")),
          {"prep": prep(rows[1][0]), "executing": rows[1][0]["executing"]})

    # A bridge behind an unselected lazy branch: never prepared, publishes nothing.
    def behind_switch(fx):
        return {"1": editor(fx), "rl": n("SonderPromptRelayBridge", project=["1", 0]),
                "g": n("CPText", value=["rl", 0]),
                "o": n("StringConcatenate", string_a="other", string_b="", delimiter=""),
                "sw": n("SonderLazySwitch", **{"select": 0, "branches.item0": ["o", 0], "branches.item1": ["g", 0]}),
                "swp": n("PreviewAny", source=["sw", 0]), "7": save(["1", 0], ["1", 1], mode="Take")}

    arms = await both_arms(behind_switch, runs=1, label="behind", fixture_args={"reference": False})
    (row, params), (stock, stock_params) = arms[True][1][0], arms[False][1][0]
    check(case, "a bridge on an unselected branch: not prepared, no provenance, as stock",
          row["success"] and prep(row).get("bridge_reasons") == {"not_certain": 1}
          and "prompt_relay" not in params and "prompt_relay" not in stock_params
          and row["resolver_calls"].get("relay", 0) == 0 and row["outputs"] == stock["outputs"],
          {"prep": prep(row), "calls": row["resolver_calls"], "keys": sorted(params)})

    # A bridge that stays scheduled (a wildcard consumer) runs twice, identically.
    def still_scheduled(fx):
        return {"1": editor(fx), "rl": n("SonderPromptRelayBridge", project=["1", 0]),
                "g": n("CPText", value=["rl", 0]), "gp": n("PreviewAny", source=["g", 0]),
                "wp": n("PreviewAny", source=["rl", 1]),
                "after": n("CPAfter", image=["1", 1], a=["g", 0], b=["wp", 0]),
                "7": save(["1", 0], ["after", 0], mode="Take")}

    arms = await both_arms(still_scheduled, label="still", fixture_args={"reference": False})
    (_, rows), (_, stock_rows) = arms[True], arms[False]
    second = rows[1][0]
    check(case, "a still-scheduled bridge: runs twice, its literal consumer hits, results and provenance equal stock",
          "rl" in executed_nodes(second) and "g" not in executed_nodes(second)
          and second["resolver_calls"].get("relay") == 2 and second["outputs"] == stock_rows[1][0]["outputs"]
          and stable_params(rows[1][1].get("prompt_relay")) == stable_params(stock_rows[1][1].get("prompt_relay")),
          {"executing": second["executing"], "calls": second["resolver_calls"]})

    # An interrupt raised in bridge preparation (a cancel caught at the next bridge's
    # check), and a cancel set after the last bridge's check.
    for label, inject in (("raised in the bridge pass", "masks"), ("set after the last bridge", "relay")):
        set_pipeline(True)
        fx = bridge_fixture(f"cancel-{inject}", 3, reference=False)
        graph = {"1": editor(fx), "mk": n("SonderMasksBridge", project=["1", 0], edit_video=True, edit_audio=True),
                 "mve": n("CPFloat", value=["mk", 1]), "rl": n("SonderPromptRelayBridge", project=["1", 0]),
                 "g": n("CPText", value=["rl", 0]), "gp": n("PreviewAny", source=["g", 0]),
                 "mvep": n("PreviewAny", source=["mve", 0]), "7": save(["1", 0], ["1", 1], mode="Take")}
        executor = new_executor()
        INJECT["interrupt_in"] = inject
        row = await run(executor, graph)
        reset_injections()
        # Phase 4b: a cancel set after the last bridge is caught by the deferred
        # render's own check before the early render, so preparation raises too.
        expected_outcome = "raised"
        check(case, f"cancel {label}: interrupted, no consumer ran, the job returned to pending",
              row["error"] and row["error"]["event"] == "execution_interrupted"
              and not any(key.split(":")[0] in ("text", "float", "int") for key in row["calls"])
              and jobs_of(fx)[f"cancel-{inject}-1"]["status"] == "pending"
              and prep(row).get("outcome") == expected_outcome,
              {"error": row["error"], "calls": row["calls"], "jobs": jobs_of(fx), "prep": prep(row)})
        following = await run(executor, graph)
        check(case, f"cancel {label}: the executor survives and the next prompt succeeds",
              following["success"], following.get("error"))

    # A fault in the bridge pass keeps the Editor's plan.
    results = {}
    for enabled in (True, False):
        set_pipeline(enabled)
        fx = bridge_fixture(f"passfault-{int(enabled)}", 3, reference=False)
        graph = {"1": editor(fx), "2": n("CPText", value=["1", 5]), "2p": n("PreviewAny", source=["2", 0]),
                 "rl": n("SonderPromptRelayBridge", project=["1", 0]), "g": n("CPText", value=["rl", 0]),
                 "gp": n("PreviewAny", source=["g", 0]), "7": save(["1", 0], ["1", 1], mode="Take")}
        executor = new_executor()
        INJECT["bridge_pass_fault"] = True
        rows = [await run(executor, graph), await run(executor, graph)]
        reset_injections()
        results[enabled] = rows
    rows, stock_rows = results[True], results[False]
    check(case, "bridge-pass fault: the Editor plan still applies, bridge consumers rerun, results equal stock",
          prep(rows[1]).get("bridge_reasons") == {"bridge_pass_error": 1} and prep(rows[1]).get("substituted", 0) >= 1
          and "2" not in executed_nodes(rows[1]) and "g" in executed_nodes(rows[1])
          and [row["outputs"] for row in rows] == [row["outputs"] for row in stock_rows],
          {"prep": prep(rows[1]), "executing": rows[1]["executing"]})


async def case_bridges_ram():
    case = "bridges_ram"

    # RAM eviction after a still-scheduled bridge: a late consumer re-stages it.
    def evicting(fx):
        return {"1": editor(fx), "rl": n("SonderPromptRelayBridge", project=["1", 0]),
                "g": n("CPText", value=["rl", 0]), "ev": n("CPEvict", anchor=["g", 0]),
                "late": n("StringConcatenate", string_a=["ev", 0], string_b=["rl", 1], delimiter="|"),
                "latep": n("PreviewAny", source=["late", 0]),
                "after": n("CPAfter", image=["1", 1], a=["late", 0]), "7": save(["1", 0], ["after", 0], mode="Take")}

    arms = await both_arms(evicting, runs=1, label="evict", fixture_args={"reference": False})
    (row, params), (stock, stock_params) = arms[True][1][0], arms[False][1][0]
    check(case, "eviction re-stages a still-scheduled bridge: equal outputs and provenance",
          row["success"] and row["outputs"] == stock["outputs"]
          and stable_params(params.get("prompt_relay")) == stable_params(stock_params.get("prompt_relay")),
          {"pipeline": row["outputs"], "stock": stock["outputs"], "error": row.get("error")})


# --- Phase 3: media identity -------------------------------------------------------------------

identity_mod = sonder("nodes.cache_identity")
IDENTIFIED = {"db", "ri", "gs"}
MEDIA_CONSUMERS = {"dd", "dst", "rd", "gd"}


def identity_graph(fx):
    """Driver, Reference Image and Guides Start, each with consumers of its media only."""
    return {
        "1": editor(fx),
        "ds": n("SonderDriverSelector", project=["1", 0], driver_lane_index=0, driver_selector_overrides_json="{}"),
        "db": n("SonderDriverBridge", driver_ref=["ds", 0]),
        "dd": n("CPMediaDigest", frames=["db", 0]), "ddp": n("PreviewAny", source=["dd", 0]),
        "dst": n("CPFloat", value=["db", 2]), "dstp": n("PreviewAny", source=["dst", 0]),
        "rs": n("SonderReferenceSelector", project=["1", 0], reference_lanes="0"),
        "ri": n("SonderReferenceImageBridge", reference_set=["rs", 0], unused_slots="placeholder"),
        "rd": n("CPMediaDigest", frames=["ri", 0]), "rdp": n("PreviewAny", source=["rd", 0]),
        "gs": n("SonderGuidesBridgeStart", project=["1", 0], iteration_index=0, value_0="start"),
        "gd": n("CPMediaDigest", frames=["gs", 1]),
        "ge": n("SonderGuidesBridgeEnd", project=["1", 0], flow_control=["gs", 0], value_0=["gd", 0]),
        "gep": n("PreviewAny", source=["ge", 0]),
        # The saver depends on every chain, so both arms save after every bridge ran.
        "after": n("CPAfter", image=["db", 0], a=["rd", 0], b=["ge", 0], c=["dst", 0]),
        "7": save(["1", 0], ["after", 0], mode="Take"),
    }


def replace_reference_image(fx, color):
    """A different image at the same path, its old modification time copied back."""
    from PIL import Image
    path = Path(fx["dir"]) / "media" / "subject.png"
    stat = path.stat()
    Image.new("RGB", (16, 16), color).save(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


async def identity_sequence(enabled, label):
    set_pipeline(enabled)
    fx = bridge_fixture(f"{label}-{int(enabled)}", 8, driver=True, guides=[0])
    executor = new_executor()
    rows = []

    async def step(name):
        rows.append((name, await run(executor, identity_graph(fx)), last_take_params(fx)))

    await step("base")
    await step("identical")
    replace_reference_image(fx, (0, 255, 0))
    await step("reference replaced at the same path")

    def driver_strength(project, scene):
        next(clip for clip in scene.clips if clip.role == "motion_driver").strength = 0.75

    edit_project(fx, driver_strength)
    await step("driver strength")

    def guide_strength(project, scene):
        scene.guide_frames[0].strength = 0.3

    edit_project(fx, guide_strength)
    await step("guide strength")
    return rows


async def case_identity():
    case = "identity"
    pipeline_rows = await identity_sequence(True, "identity")
    stock_rows = await identity_sequence(False, "identity")
    # What each step reruns among the identified bridges and their media consumers.
    expected = {"identical": set(), "reference replaced at the same path": {"ri", "rd"},
                "driver strength": {"db", "dd", "dst"}, "guide strength": {"gs", "gd"}}
    watched = IDENTIFIED | MEDIA_CONSUMERS | {"ds", "rs"}
    for (label, row, params), (_, stock, stock_params) in zip(pipeline_rows, stock_rows):
        check(case, f"{label}: both arms succeed", row.get("success") and stock.get("success"),
              {"pipeline": row.get("error") or row.get("invalid"), "stock": stock.get("error") or stock.get("invalid")})
        check(case, f"{label}: differential oracle (every consumer equals stock)", row["outputs"] == stock["outputs"],
              _diff(row["outputs"], stock["outputs"]))
        check(case, f"{label}: driver provenance equals stock",
              stable_params(params.get("driver_selector")) == stable_params(stock_params.get("driver_selector"))
              and (params.get("driver_selector") or {}).get("has_driver") == 1,
              [params.get("driver_selector"), stock_params.get("driver_selector")])
        check(case, f"{label}: stock runs every bridge", IDENTIFIED <= executed_nodes(stock), stock["executing"])
        if MODE == "none":
            check(case, f"{label}: --cache-none bypasses identity", not row["preparations"]
                  and IDENTIFIED <= executed_nodes(row), {"prep": prep(row), "executing": row["executing"]})
            continue
        check(case, f"{label}: three bridges identified",
              prep(row).get("identities") == {"identified": 3, "skipped": 0},
              {"identities": prep(row).get("identities"), "reasons": prep(row).get("identity_reasons")})
        if label in expected and MODE in ("classic", "ram"):
            reran = executed_nodes(row) & watched
            check(case, f"{label}: exactly the changed media reruns", reran == expected[label],
                  {"ran": sorted(reran), "expected": sorted(expected[label])})
    if MODE != "none":
        base = pipeline_rows[0][1]
        check(case, "base: the Selectors leave the schedule once their handles took tokens",
              not ({"ds", "rs"} & executed_nodes(base)), base["executing"])
        replaced = pipeline_rows[2][1]
        check(case, "a replaced Reference image (same path, copied mtime) reaches the consumer",
              replaced["outputs"].get("rdp") != pipeline_rows[1][1]["outputs"].get("rdp"),
              [replaced["outputs"].get("rdp"), pipeline_rows[1][1]["outputs"].get("rdp")])


async def case_identity_shapes():
    """Graph shapes the identity must keep equal to stock (classic)."""
    case = "identity_shapes"

    # A three-guide loop: End clones the token-overridden Start per guide; each
    # clone decodes its own guide from the recipe. Clones carry tensors, so they
    # rerun; the results must equal stock.
    def loop(fx):
        return {"1": editor(fx, selection_end=4),
                "gs": n("SonderGuidesBridgeStart", project=["1", 0], iteration_index=0, value_0="start"),
                "gd": n("CPMediaDigest", frames=["gs", 1]),
                "gb": n("StringConcatenate", string_a=["gs", 4], string_b=["gd", 0], delimiter="|"),
                "ge": n("SonderGuidesBridgeEnd", project=["1", 0], flow_control=["gs", 0], value_0=["gb", 0]),
                "gep": n("PreviewAny", source=["ge", 0]),
                "after": n("CPAfter", image=["1", 1], a=["ge", 0]), "7": save(["1", 0], ["after", 0], mode="Take")}

    arms = await both_arms(loop, fixture_args={"reference": False, "guides": [0, 1, 2]}, label="idloop")
    (_, rows), (_, stock_rows) = arms[True], arms[False]
    check(case, "three-guide loop: every guide's result equals stock",
          all(row["success"] and row["outputs"] == stock["outputs"] for (row, _), (stock, _) in zip(rows, stock_rows))
          and (rows[0][0]["outputs"].get("gep") or "").count("|") == 3,
          [(row["outputs"], stock["outputs"], row.get("error")) for (row, _), (stock, _) in zip(rows, stock_rows)])
    check(case, "three-guide loop: Start is identified and its clones resolve the token",
          prep(rows[0][0]).get("identities", {}).get("identified") == 1, prep(rows[0][0]).get("identity_reasons"))

    # A loop whose value comes from the Editor's frames: carried since Phase 4a, so an
    # identical second job reuses its Start (and the frames consumer feeding it).
    def media_loop(fx):
        graph = loop(fx)
        # Takes placed muted: since Phase 4b the frames' identity is the timeline's recipe,
        # so a placed take is a new timeline even when it renders the same pixels.
        graph["1"] = editor(fx, selection_end=4, take_placement_muted=True)
        graph["tap"] = n("CPImageTap", frames=["1", 1])
        graph["gs"]["inputs"]["value_0"] = ["tap", 0]
        return graph

    arms = await both_arms(media_loop, fixture_args={"reference": False, "guides": [0]}, label="idmedia")
    (_, rows), (_, stock_rows) = arms[True], arms[False]
    check(case, "a loop fed by Editor frames equals stock and its Start is reused on the second job",
          all(row["success"] and row["outputs"] == stock["outputs"] for (row, _), (stock, _) in zip(rows, stock_rows))
          and not ({"tap", "gs"} & executed_nodes(rows[1][0])),
          [(row["outputs"], row.get("error"), row["executing"]) for (row, _) in rows])

    # A Reference Bridge behind a Cluster whose select comes from a Sonder Selector.
    def selected(fx):
        return {"1": editor(fx),
                "sel": n("SonderSelector", choice="Option 2", choice_list="Option 1\nOption 2"),
                "rs": n("SonderReferenceSelector", project=["1", 0], reference_lanes="0"),
                "ri": n("SonderReferenceImageBridge", reference_set=["rs", 0], unused_slots="placeholder"),
                "rd": n("CPMediaDigest", frames=["ri", 0]),
                "o": n("StringConcatenate", string_a="no reference", string_b="", delimiter=""),
                "cl": n("SonderLazyCluster", select=["sel", 1], branches=2, lanes=1, b0_l0=["o", 0], b1_l0=["rd", 0]),
                "clp": n("PreviewAny", source=["cl", 0]),
                "after": n("CPAfter", image=["1", 1], a=["cl", 0]), "7": save(["1", 0], ["after", 0], mode="Take")}

    arms = await both_arms(selected, label="idselector")
    (_, rows), (_, stock_rows) = arms[True], arms[False]
    check(case, "Reference behind a Selector-driven Cluster: equal to stock, identified, reused on the second job",
          all(row["success"] and row["outputs"] == stock["outputs"] for (row, _), (stock, _) in zip(rows, stock_rows))
          and prep(rows[0][0]).get("identities", {}).get("identified") == 1
          and not ({"ri", "rd"} & executed_nodes(rows[1][0])),
          {"prep": prep(rows[0][0]).get("identity_reasons"), "second": rows[1][0]["executing"]})

    # Setting changes miss: the Driver's fit and in point, the Image Bridge's unused_slots.
    set_pipeline(True)
    fx = bridge_fixture("idsettings", 6, driver=True, guides=[0])
    executor = new_executor()
    await run(executor, identity_graph(fx))

    def fit(project, scene):
        next(clip for clip in scene.clips if clip.role == "motion_driver").fit_mode = "cover"

    def in_point(project, scene):
        next(clip for clip in scene.clips if clip.role == "motion_driver").source_in_frame = 1

    for label, change, expected in (("Driver fit", fit, {"db"}), ("Driver in point", in_point, {"db"})):
        edit_project(fx, change)
        row = await run(executor, identity_graph(fx))
        check(case, f"{label}: the Driver Bridge decodes again, the others do not",
              row["success"] and expected <= executed_nodes(row) and not ({"ri", "gs"} & executed_nodes(row)),
              row["executing"])
    graph = identity_graph(fx)
    graph["ri"]["inputs"]["unused_slots"] = "nothing"
    row = await run(executor, graph)
    check(case, "unused_slots: the Image Bridge decodes again, the others do not",
          row["success"] and "ri" in executed_nodes(row) and not ({"db", "gs"} & executed_nodes(row)), row["executing"])


async def case_identity_lazy():
    case = "identity_lazy"

    def lazy_driver(fx):
        return {"1": editor(fx), "ds": n("SonderDriverSelector", project=["1", 0], driver_lane_index=0,
                                          driver_selector_overrides_json="{}"),
                "db": n("SonderDriverBridge", driver_ref=["ds", 0]), "dd": n("CPMediaDigest", frames=["db", 0]),
                "o": n("StringConcatenate", string_a="no driver", string_b="", delimiter=""),
                "cl": n("SonderLazyCluster", select=["ds", 1], branches=2, lanes=1, b0_l0=["o", 0], b1_l0=["dd", 0]),
                "clp": n("PreviewAny", source=["cl", 0]),
                "after": n("CPAfter", image=["1", 1], a=["cl", 0]), "7": save(["1", 0], ["after", 0], mode="Take")}

    arms = await both_arms(lazy_driver, fixture_args={"driver": True, "reference": False}, label="idlazy")
    (_, rows), (_, stock_rows) = arms[True], arms[False]
    (first, _), (second, _) = rows
    check(case, "Driver behind a Cluster selected by has_driver: results equal stock",
          all(row["success"] and row["outputs"] == stock["outputs"] for (row, _), (stock, _) in zip(rows, stock_rows)),
          [(row["outputs"], stock["outputs"], row.get("error")) for (row, _), (stock, _) in zip(rows, stock_rows)])
    check(case, "the Cluster's requested branch is certain: the Driver Bridge is identified",
          prep(first).get("identities", {}).get("identified") == 1 and prep(first).get("lazy_followed", 0) >= 1,
          {"identities": prep(first).get("identities"), "reasons": prep(first).get("identity_reasons"),
           "lazy": prep(first).get("lazy_followed")})
    check(case, "identical second job: the Driver Bridge and its consumer are reused",
          not ({"db", "dd"} & executed_nodes(second)), second["executing"])

    arms = await both_arms(lazy_driver, fixture_args={"driver": True, "muted_driver": True, "reference": False},
                           label="idmuted")
    (_, rows), (_, stock_rows) = arms[True], arms[False]
    row = rows[0][0]
    check(case, "muted Driver: the branch is not requested, so nothing is hashed or decoded",
          row["success"] and row["outputs"] == stock_rows[0][0]["outputs"] and "db" not in executed_nodes(row)
          and prep(row).get("identity_reasons") == {"not_certain": 1} and not prep(row).get("identity_bytes"),
          {"prep": prep(row), "executing": row["executing"]})


async def case_identity_safety():
    case = "identity_safety"
    # A token typed into a prompt names no file: the bridge refuses it.
    fx = bridge_fixture("forged", 2, driver=True, reference=False)
    set_pipeline(True)
    forged = {"1": editor(fx),
              "db": n("SonderDriverBridge", driver_ref=identity_mod.TOKEN_PREFIX + "0" * 64),
              "dd": n("CPMediaDigest", frames=["db", 0]), "ddp": n("PreviewAny", source=["dd", 0]),
              "7": save(["1", 0], ["1", 1])}
    row = await run(new_executor(), forged)
    check(case, "a forged token fails at the bridge with the documented message",
          not row.get("success") and (row.get("error") or {}).get("node") == "db"
          and "did not create" in (row.get("error") or {}).get("message", ""), row.get("error") or row.get("invalid"))

    # A source changed between preparation and decode fails the run; the next run works.
    fx = bridge_fixture("changed", 3, driver=True, guides=[0])
    original = pipeline._identity_pass

    def then_touch(*args, **kwargs):
        tokens = original(*args, **kwargs)
        path = Path(fx["dir"]) / "media" / "subject.png"
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
        return tokens

    pipeline._identity_pass = then_touch
    try:
        row = await run(new_executor(), identity_graph(fx))
    finally:
        pipeline._identity_pass = original
    check(case, "a source changed after hashing fails the run at its bridge",
          not row.get("success") and (row.get("error") or {}).get("node") == "ri"
          and "changed while this run was reading it" in (row.get("error") or {}).get("message", ""), row.get("error"))
    row = await run(new_executor(), identity_graph(fx))
    check(case, "the next run hashes the new state and succeeds", row.get("success"), row.get("error"))

    # A cancel while hashing, at the real cadence: the Reference source is larger than
    # one check interval, the cancel lands as its hash starts, and the hash's own
    # interrupt check stops it. The interrupt is re-armed; no bridge or consumer runs.
    fx = bridge_fixture("cancel", 3, driver=True, guides=[0])
    subject = Path(fx["dir"]) / "media" / "subject.png"
    original_png = subject.read_bytes()
    subject.write_bytes(original_png + b"\0" * ((identity_mod.CHECK_EVERY + 4) * identity_mod.CHUNK))
    real_digest = identity_mod.digest_file
    checks_inside = []

    def cancelled(path, check_interrupt=None):
        if path.endswith("subject.png"):
            mm.interrupt_current_processing(True)

            def counted():
                checks_inside.append(path)
                check_interrupt()

            return real_digest(path, counted)
        return real_digest(path, check_interrupt)

    identity_mod.digest_file = cancelled
    try:
        row = await run(new_executor(), identity_graph(fx))
    finally:
        identity_mod.digest_file = real_digest
        mm.interrupt_current_processing(False)
        subject.write_bytes(original_png)
    check(case, "a cancel during a long hash stops it at the hash's own interrupt check",
          not row.get("success") and (row.get("error") or {}).get("event") == "execution_interrupted"
          and len(checks_inside) == 1 and not ((IDENTIFIED | MEDIA_CONSUMERS) & executed_nodes(row)),
          {"error": row.get("error"), "checks_inside": len(checks_inside), "executing": row["executing"]})
    check(case, "the cancelled job returns to pending", any(job["status"] == "pending" for job in jobs_of(fx).values()),
          jobs_of(fx))
    row = await run(new_executor(), identity_graph(fx))
    check(case, "the next run after a cancel succeeds", row.get("success"), row.get("error"))


async def case_identity_ram():
    case = "identity_ram"

    # RAM eviction after the Image Bridge ran with its token: a late consumer re-stages
    # it, and it decodes again from the recipe its run registered.
    def evicting(fx):
        return {"1": editor(fx), "rs": n("SonderReferenceSelector", project=["1", 0], reference_lanes="0"),
                "ri": n("SonderReferenceImageBridge", reference_set=["rs", 0], unused_slots="placeholder"),
                "rd": n("CPMediaDigest", frames=["ri", 0]), "ev": n("CPEvict", anchor=["rd", 0]),
                "late": n("CPMediaDigest", frames=["ri", 0]), "cat": n("StringConcatenate", string_a=["ev", 0],
                                                                       string_b=["late", 0], delimiter="|"),
                "catp": n("PreviewAny", source=["cat", 0]),
                "after": n("CPAfter", image=["1", 1], a=["cat", 0]), "7": save(["1", 0], ["after", 0], mode="Take")}

    arms = await both_arms(evicting, runs=1, label="idevict")
    (row, _), (stock, _) = arms[True][1][0], arms[False][1][0]
    check(case, "eviction re-stages an identified bridge: it decodes from its recipe, outputs equal stock",
          row["success"] and row["outputs"] == stock["outputs"] and prep(row).get("identities", {}).get("identified") == 1,
          {"pipeline": row["outputs"], "stock": stock["outputs"], "error": row.get("error"), "prep": prep(row)})


# --- Phase 4a: Editor media carried by content ----------------------------------------------------

content_mod = sonder("nodes.cache_content")
MEDIA = {"fd", "fs", "gd", "ad", "two", "mw"}
FRAME_CONSUMERS, GUIDE_CONSUMERS, AUDIO_CONSUMERS = {"fd", "fs", "two", "mw"}, {"gd"}, {"ad", "two"}


def write_tone(path, seconds=1.0, rate=48000, amplitude=8000):
    import math
    import struct
    import wave
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for index in range(int(seconds * rate)):
            value = int(amplitude * math.sin(2 * math.pi * 440 * index / rate))
            frames += struct.pack("<hh", value, value)
        handle.writeframes(bytes(frames))


def content_fixture(label, runs):
    fx = fixture(label, [{"text": "Content job", "end": 4, "snapshot": 0}] * runs, clip=True, guides=[0])

    def add_audio(project, scene):
        media = Path(project.project_dir) / "media"
        write_tone(media / "tone.wav")
        scene.audio_tracks = [timeline.AudioTrack(source_path="media/tone.wav", timeline_start_frame=0,
                                                  timeline_end_frame=8, total_source_frames=24)]

    edit_project(fx, add_audio)
    return fx


def content_graph(fx, ed="1"):
    return {
        # First in prompt order: its key is built first, downstream of a rewired consumer only.
        "first": n("PreviewAny", source=["fd", 0]),
        # Takes placed muted: a placed take would cover the clip and change the next render.
        ed: editor(fx, selection_end=4, take_placement_muted=True),
        "fd": n("CPMediaDigest", frames=[ed, 1]), "fdp": n("PreviewAny", source=["fd", 0]),
        "fs": n("CPPixelSum", frames=[ed, 1]), "fsp": n("PreviewAny", source=["fs", 0]),
        "gd": n("CPMediaDigest", frames=[ed, 2]), "gdp": n("PreviewAny", source=["gd", 0]),
        "ad": n("CPAudioDigest", audio=[ed, 10]), "adp": n("PreviewAny", source=["ad", 0]),
        "two": n("CPTwo", frames=[ed, 1], audio=[ed, 10]), "twop": n("PreviewAny", source=["two", 0]),
        "mw": n("CPMixWidth", frames=[ed, 1], width=[ed, 8]), "mwp": n("PreviewAny", source=["mw", 0]),
        "gi": n("CPText", value=[ed, 3]), "gip": n("PreviewAny", source=["gi", 0]),
        "pk": n("CPPromptKeys", anchor=["fd", 0]),
        # The saver depends on every consumer, so both arms save after all of them ran.
        "after": n("CPAfter", image=[ed, 1], a=["fd", 0], b=["gd", 0], c=["ad", 0], d=["two", 0], e=["mw", 0],
                   f=["fs", 0]),
        "7": save([ed, 0], ["after", 0], mode="Take", embed=True),
    }


def replace_guide_image(fx, color):
    from PIL import Image
    Image.new("RGB", (16, 16), color).save(Path(fx["dir"]) / "media" / "guide.png")


CONTENT_STEPS = ("base", "identical", "clip opacity", "guide moved", "guide image replaced", "audio volume")
CONTENT_EXPECTED = {"identical": set(), "clip opacity": FRAME_CONSUMERS, "guide moved": set(),
                    "guide image replaced": GUIDE_CONSUMERS, "audio volume": AUDIO_CONSUMERS}


async def content_sequence(enabled, label, build=content_graph):
    set_pipeline(enabled)
    fx = content_fixture(f"{label}-{int(enabled)}", len(CONTENT_STEPS) + 1)
    executor = new_executor()
    rows = []
    for name in CONTENT_STEPS:
        if name == "clip opacity":
            edit_project(fx, lambda project, scene: setattr(scene.clips[0], "opacity", 0.5))
        elif name == "guide moved":
            edit_project(fx, lambda project, scene: setattr(scene.guide_frames[0], "frame_index", 1))
        elif name == "guide image replaced":
            replace_guide_image(fx, (0, 0, 255))
        elif name == "audio volume":
            edit_project(fx, lambda project, scene: setattr(scene.audio_tracks[0], "volume", 0.5))
        rows.append((name, await run(executor, build(fx)), last_take_params(fx)))
    return rows


def _no_carrier_anywhere(row, params):
    seen = [note["prompt_keys"] for note in row["notes"] if "prompt_keys" in note]
    return (row["prompt_unchanged"] and "sonder-media" not in row["history_json"]
            and "sonder-media" not in json.dumps(params, default=str)
            and bool(seen) and not any(key.startswith("sonder-media") for keys in seen for key in keys))


async def case_content():
    case = "content"
    pipeline_rows = await content_sequence(True, "content")
    stock_rows = await content_sequence(False, "content")
    for (label, row, params), (_, stock, _stock_params) in zip(pipeline_rows, stock_rows):
        check(case, f"{label}: both arms succeed", row.get("success") and stock.get("success"),
              {"pipeline": row.get("error") or row.get("invalid"), "stock": stock.get("error") or stock.get("invalid")})
        check(case, f"{label}: differential oracle (every consumer equals stock)", row["outputs"] == stock["outputs"],
              _diff(row["outputs"], stock["outputs"]))
        check(case, f"{label}: stock reruns every media consumer", MEDIA <= executed_nodes(stock), stock["executing"])
        check(case, f"{label}: no carrier in the prompt, hidden PROMPT, history or the take",
              _no_carrier_anywhere(row, params), {"prompt_unchanged": row["prompt_unchanged"]})
        if MODE == "none":
            check(case, f"{label}: --cache-none bypasses carriers", not row["preparations"]
                  and MEDIA <= executed_nodes(row), {"prep": prep(row), "executing": row["executing"]})
            continue
        check(case, f"{label}: three media outputs carried",
              prep(row).get("content", {}).get("carried") == 3,
              {"content": prep(row).get("content"), "reasons": prep(row).get("content_reasons")})
        check(case, f"{label}: the Editor body ran once", len(row["editor_runs"]) == 1, len(row["editor_runs"]))
        if label in CONTENT_EXPECTED and MODE in ("classic", "ram"):
            reran = executed_nodes(row) & MEDIA
            check(case, f"{label}: exactly the changed output's consumers rerun", reran == CONTENT_EXPECTED[label],
                  {"ran": sorted(reran), "expected": sorted(CONTENT_EXPECTED[label])})
            check(case, f"{label}: no carrier executes (each entry is stored before the run)",
                  not any(str(node).startswith("sonder-media") for node in row["executing"]), row["executing"])
    if MODE in ("classic", "ram"):
        moved = pipeline_rows[3][1]
        check(case, "guide moved: guide_idx's consumer reruns, guide image consumers hit (layout independent)",
              "gi" in executed_nodes(moved) and "gd" not in executed_nodes(moved), moved["executing"])
        base = pipeline_rows[0][1]
        notes = [note for note in base["notes"] if "mix_width" in note]
        check(case, "a consumer takes an M1 literal and a carrier link on one node",
              notes and notes[0]["width_literal"] and str(notes[0]["frames_link"][0]).startswith("sonder-media"), notes)


async def case_content_negative():
    """Negative control: a digest that ignores the last frame must fail the oracle."""
    case = "content_negative"
    real = content_mod._tensor_digest

    def without_last_frame(torch_module, tensor, check_interrupt):
        return real(torch_module, tensor[:-1], check_interrupt)

    content_mod._tensor_digest = without_last_frame
    try:
        rows = {}
        for enabled in (True, False):
            set_pipeline(enabled)
            fx = content_fixture(f"negative-{int(enabled)}", 3)
            executor = new_executor()
            await run(executor, content_graph(fx))
            replace_guide_image(fx, (0, 0, 255))  # guide_images holds one frame: the broken digest ignores it
            rows[enabled] = await run(executor, content_graph(fx))
    finally:
        content_mod._tensor_digest = real
    check(case, "the broken digest is caught: the stale guide consumer differs from stock",
          rows[True]["outputs"].get("gdp") != rows[False]["outputs"].get("gdp") and "gd" not in executed_nodes(rows[True]),
          {"pipeline": rows[True]["outputs"].get("gdp"), "stock": rows[False]["outputs"].get("gdp")})


async def case_content_shapes():
    """Graph shapes that must equal stock with carriers (classic)."""
    case = "content_shapes"

    # Editor frames on a Sonder Switch's lazy input: requested on the first lazy check.
    def switched(fx):
        return {"1": editor(fx, selection_end=4, take_placement_muted=True),
                "fd": n("CPMediaDigest", frames=["1", 1]),
                "o": n("StringConcatenate", string_a="nothing", string_b="", delimiter=""),
                "sw": n("SonderLazySwitch", **{"select": 1, "branches.item0": ["fd", 0], "branches.item1": ["o", 0]}),
                "swp": n("PreviewAny", source=["sw", 0]),
                "after": n("CPAfter", image=["1", 1], a=["sw", 0]), "7": save(["1", 0], ["after", 0], mode="Take")}

    # Phase 3's video-to-video Guides Start: its value_0 is Editor frames.
    def v2v(fx):
        return {"1": editor(fx, selection_end=4, take_placement_muted=True),
                "gs": n("SonderGuidesBridgeStart", project=["1", 0], iteration_index=0, value_0=["1", 1]),
                "gd": n("CPMediaDigest", frames=["gs", 1]), "vd": n("CPMediaDigest", frames=["gs", 4]),
                "gb": n("StringConcatenate", string_a=["vd", 0], string_b=["gd", 0], delimiter="|"),
                "ge": n("SonderGuidesBridgeEnd", project=["1", 0], flow_control=["gs", 0], value_0=["gb", 0]),
                "gep": n("PreviewAny", source=["ge", 0]),
                "after": n("CPAfter", image=["1", 1], a=["ge", 0]), "7": save(["1", 0], ["after", 0], mode="Take")}

    # A Guides loop whose body takes Editor frames directly: clones come from DYNPROMPT.
    def loop_body(fx):
        return {"1": editor(fx, selection_end=4, take_placement_muted=True),
                "gs": n("SonderGuidesBridgeStart", project=["1", 0], iteration_index=0, value_0="start"),
                "body": n("CPAfter", image=["1", 1], a=["gs", 4]),
                "bd": n("CPMediaDigest", frames=["body", 0]),
                "gb": n("StringConcatenate", string_a=["gs", 4], string_b=["bd", 0], delimiter="|"),
                "ge": n("SonderGuidesBridgeEnd", project=["1", 0], flow_control=["gs", 0], value_0=["gb", 0]),
                "gep": n("PreviewAny", source=["ge", 0]),
                "after": n("CPAfter", image=["1", 1], a=["ge", 0]), "7": save(["1", 0], ["after", 0], mode="Take")}

    for label, build, guides, reused in (("lazy switch", switched, [0], {"fd"}), ("video-to-video Guides Start", v2v, [0], {"gs"}),
                                         ("Guides loop body", loop_body, [0, 1, 2], set())):
        rows = {}
        for enabled in (True, False):
            set_pipeline(enabled)
            fx = fixture(f"cshape-{label[:6]}-{int(enabled)}", [{"text": "Shape", "end": 4, "snapshot": 0}] * 3, clip=True, guides=guides)
            executor = new_executor()
            rows[enabled] = [await run(executor, build(fx)) for _ in range(2)]
        pipeline_rows, stock_rows = rows[True], rows[False]
        check(case, f"{label}: every run equals stock",
              all(row.get("success") and row["outputs"] == stock["outputs"] for row, stock in zip(pipeline_rows, stock_rows)),
              [(row.get("error"), _diff(row["outputs"], stock["outputs"])) for row, stock in zip(pipeline_rows, stock_rows)])
        check(case, f"{label}: carried, and the second job reuses what it should",
              prep(pipeline_rows[0]).get("content", {}).get("carried", 0) >= 1
              and not (reused & executed_nodes(pipeline_rows[1])),
              {"content": prep(pipeline_rows[0]).get("content"), "reasons": prep(pipeline_rows[0]).get("content_reasons"),
               "identities": prep(pipeline_rows[1]).get("identities"),
               "identity_reasons": prep(pipeline_rows[1]).get("identity_reasons"),
               "second": pipeline_rows[1]["executing"]})


async def case_content_safety():
    case = "content_safety"
    set_pipeline(True)
    fx = content_fixture("csafety", 12)

    # A carrier in a submitted prompt is refused by validation.
    graph = content_graph(fx)
    graph["c"] = n("SonderMediaCarrier", token=content_mod.TOKEN_PREFIX + "0" * 64)
    graph["fd"]["inputs"]["frames"] = ["c", 0]
    row = await run(new_executor(), graph)
    # ComfyUI drops the outputs that depend on a refused node and runs the rest.
    refused = (row.get("invalid") or "") + row.get("node_errors", "")
    check(case, "a submitted carrier is refused by validation and nothing that depends on it runs",
          "cannot be part of a submitted" in refused and not ({"c", "fd", "first", "pk", "after", "7"} & executed_nodes(row)),
          {key: row.get(key) for key in ("invalid", "error", "success", "executing")})

    # The carrier's own execute refuses a forged token and another prompt's token.
    class Context:
        def __init__(self, prompt_id):
            self.prompt_id = prompt_id

    refusals = []
    real_context = media_carrier.get_executing_context
    for context in (None, Context("not-a-running-prompt")):
        media_carrier.get_executing_context = lambda context=context: context
        try:
            media_carrier.SonderMediaCarrier.execute(content_mod.TOKEN_PREFIX + "1" * 64)
            refusals.append("accepted")
        except RuntimeError as exc:
            refusals.append("did not create" in str(exc))
        finally:
            media_carrier.get_executing_context = real_context
    check(case, "the carrier refuses a forged token and a token of no running prompt", refusals == [True, True], refusals)

    # A keying fault through the real hook: rollback, no worker death, equal results, next run fine.
    executor = new_executor()
    await run(executor, content_graph(fx))
    from comfy_execution import caching
    real_add_keys = caching.CacheKeySetInputSignature.add_keys

    async def skipping_carriers(self, node_ids):
        return await real_add_keys(self, [node for node in node_ids if not str(node).startswith("sonder-media")])

    edit_project(fx, lambda project, scene: setattr(scene.clips[0], "opacity", 0.75))
    caching.CacheKeySetInputSignature.add_keys = skipping_carriers
    try:
        faulted = await run(executor, content_graph(fx))
    finally:
        caching.CacheKeySetInputSignature.add_keys = real_add_keys
    set_pipeline(False)
    stock = await run(new_executor(), content_graph(fx))
    set_pipeline(True)
    record = sdk_runs  # noqa: F841 - the preparation record is read below through the row
    check(case, "a keying fault rolls back: the prompt completes, equals stock, and no carrier runs",
          faulted.get("success") and faulted["outputs"] == stock["outputs"]
          and not any(str(node).startswith("sonder-media") for node in faulted["executing"])
          and MEDIA <= executed_nodes(faulted),
          {"error": faulted.get("error"), "diff": _diff(faulted["outputs"], stock["outputs"]),
           "executing": faulted["executing"]})
    after = await run(executor, content_graph(fx))
    check(case, "after a rollback the next run carries again and reuses the media consumers",
          after.get("success") and prep(after).get("content", {}).get("carried") == 3, prep(after).get("content"))

    # A carrier that must execute (no stored entry) and fails: reported at the Editor; the worker survives.
    real_arm = hook._arm_carriers

    def arm_without_store(carriers, cache, dynprompt, plan):
        real_arm(carriers, cache, dynprompt, plan)
        plan.payloads.clear()  # nothing to store: carriers execute when a consumer needs them
        plan.settle = None  # Phase 4b: deferred carriers are left for the carrier's own net

    real_media_for = sdk_runs.media_for
    completed_before_failures = sum(job["status"] == "completed" for job in jobs_of(fx).values())
    for failure in ("raise", "cancel"):
        def failing_media_for(prompt_id, token, failure=failure):
            if failure == "cancel":
                mm.interrupt_current_processing(True)
                mm.throw_exception_if_processing_interrupted()
            raise RuntimeError("injected carrier failure")

        edit_project(fx, lambda project, scene, failure=failure: setattr(
            scene.clips[0], "opacity", 0.6 if failure == "raise" else 0.65))
        hook._arm_carriers = arm_without_store
        sdk_runs.media_for = failing_media_for
        try:
            row = await run(executor, content_graph(fx))
        finally:
            hook._arm_carriers = real_arm
            sdk_runs.media_for = real_media_for
            mm.interrupt_current_processing(False)
        event = "execution_interrupted" if failure == "cancel" else "execution_error"
        check(case, f"a carrier {failure} is reported at the Editor and the worker survives",
              not row.get("success") and (row.get("error") or {}).get("node") == "1"
              and (row.get("error") or {}).get("event") == event and "raised" not in row,
              {"error": row.get("error"), "executing": row["executing"]})
        check(case, f"a carrier {failure}: the canvas shows the Editor while the carrier runs",
              any(str(node).startswith("sonder-media") for node in row["executing"]) and "1" in row["displayed"]
              and not any(str(node).startswith("sonder-media") for node in row["displayed"]),
              {"executing": row["executing"], "displayed": row["displayed"]})
        after = await run(executor, content_graph(fx))
        check(case, f"after a carrier {failure} the next run succeeds", after.get("success"), after.get("error"))
    jobs = jobs_of(fx)
    check(case, "no job is left running after the failed runs (each returned to pending or completed)",
          not any(job["status"] == "running" for job in jobs.values())
          and sum(job["status"] == "completed" for job in jobs.values()) == completed_before_failures + 2, jobs)

    # A carrier that executes and succeeds: it serves the memo's own frames, and every
    # consumer equals the stock arm on the same timeline.
    edit_project(fx, lambda project, scene: setattr(scene.clips[0], "opacity", 0.45))
    hook._arm_carriers = arm_without_store
    try:
        carried = await run(executor, content_graph(fx))
    finally:
        hook._arm_carriers = real_arm
    set_pipeline(False)
    stock = await run(new_executor(), content_graph(fx))
    set_pipeline(True)
    check(case, "a carrier that executes serves the frames: the run equals stock",
          carried.get("success") and any(str(node).startswith("sonder-media") for node in carried["executing"])
          and carried["outputs"] == stock["outputs"] and FRAME_CONSUMERS <= executed_nodes(carried),
          {"error": carried.get("error"), "diff": _diff(carried["outputs"], stock["outputs"]),
           "executing": carried["executing"]})

    # A cancel while hashing: re-armed, nothing runs past it, the job returns to pending.
    real_digest = content_mod.digest

    def cancelling(socket, value, check_interrupt=None):
        mm.interrupt_current_processing(True)
        return real_digest(socket, value, check_interrupt)

    edit_project(fx, lambda project, scene: setattr(scene.clips[0], "opacity", 0.55))
    content_mod.digest = cancelling
    try:
        row = await run(executor, content_graph(fx))
    finally:
        content_mod.digest = real_digest
        mm.interrupt_current_processing(False)
    check(case, "a cancel during hashing interrupts the run before any consumer",
          not row.get("success") and (row.get("error") or {}).get("event") == "execution_interrupted"
          and not (MEDIA & executed_nodes(row)), {"error": row.get("error"), "executing": row["executing"]})
    row = await run(executor, content_graph(fx))
    check(case, "the next run after a cancel during hashing succeeds", row.get("success"), row.get("error"))


# --- Phase 4b: the Editor's render and mix deferred to set_prompt ---------------------------------

sdk_runs_settled = {}
_real_note_settled = pipeline._note_settled


def recording_note_settled(key, hits, misses, outcome):
    sdk_runs_settled["value"] = {"hits": hits, "misses": misses, "outcome": outcome or "settled"}
    return _real_note_settled(key, hits, misses, outcome)


pipeline._note_settled = recording_note_settled
editor_media = sonder("nodes.editor_media")


class CPFramesProject:
    """A frames consumer that also takes the Editor's project: it can never hit."""
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    CATEGORY = "CPH"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"frames": ("IMAGE",), "project": ("SONDER_PROJECT",)},
                "hidden": {"unique_id": "UNIQUE_ID"}}

    def run(self, frames, project, unique_id=None):
        import hashlib
        count("media", unique_id)
        tensor = frames.detach().cpu().contiguous()
        return (f"{list(tensor.shape)}:{hashlib.sha256(tensor.numpy().tobytes()).hexdigest()[:16]}",)


nodes.NODE_CLASS_MAPPINGS["CPFramesProject"] = CPFramesProject

DEFERRED_STEPS = ("base", "identical", "clip opacity", "audio volume", "guide strength", "guide image replaced",
                  "identical again")
# (renders, mixes) the pipeline arm does at each step: a hit does neither.
DEFERRED_WORK = {"base": (1, 1), "identical": (0, 0), "clip opacity": (1, 0), "audio volume": (0, 1),
                 "guide strength": (0, 0), "guide image replaced": (0, 0), "identical again": (0, 0)}
DEFERRED_RERUN = {"identical": set(), "clip opacity": FRAME_CONSUMERS, "audio volume": AUDIO_CONSUMERS,
                  "guide strength": set(), "guide image replaced": GUIDE_CONSUMERS, "identical again": set()}


async def deferred_sequence(enabled, label, build=content_graph, fixture_args=None):
    set_pipeline(enabled)
    fx = content_fixture(f"{label}-{int(enabled)}", len(DEFERRED_STEPS) + 1)
    executor = new_executor()
    rows = []
    for name in DEFERRED_STEPS:
        if name == "clip opacity":
            edit_project(fx, lambda project, scene: setattr(scene.clips[0], "opacity", 0.5))
        elif name == "audio volume":
            edit_project(fx, lambda project, scene: setattr(scene.audio_tracks[0], "volume", 0.5))
        elif name == "guide strength":
            edit_project(fx, lambda project, scene: setattr(scene.guide_frames[0], "strength", 0.4))
        elif name == "guide image replaced":
            replace_guide_image(fx, (0, 255, 0))
        sdk_runs_settled.clear()
        rows.append((name, await run(executor, build(fx)), last_take_params(fx)))
    return rows


async def case_deferred():
    """Phase 4b: an unchanged timeline renders and mixes nothing; a change renders exactly its slot."""
    case = "deferred"
    pipeline_rows = await deferred_sequence(True, "deferred")
    stock_rows = await deferred_sequence(False, "deferred")
    for (label, row, params), (_, stock, _stock_params) in zip(pipeline_rows, stock_rows):
        check(case, f"{label}: both arms succeed", row.get("success") and stock.get("success"),
              {"pipeline": row.get("error") or row.get("invalid"), "stock": stock.get("error") or stock.get("invalid")})
        check(case, f"{label}: differential oracle (every consumer equals stock)", row["outputs"] == stock["outputs"],
              _diff(row["outputs"], stock["outputs"]))
        check(case, f"{label}: stock renders and mixes every run", stock["work"] == {"render": 1, "mix": 1},
              stock["work"])
        check(case, f"{label}: no carrier in the prompt, hidden PROMPT, history or the take",
              _no_carrier_anywhere(row, params), {"prompt_unchanged": row["prompt_unchanged"]})
        if MODE == "none":
            check(case, f"{label}: --cache-none bypasses deferral (renders and mixes as stock)",
                  not row["preparations"] and row["work"] == {"render": 1, "mix": 1}, row["work"])
            continue
        content = prep(row).get("content", {})
        check(case, f"{label}: frames and audio carried by their pre-render identity",
              content.get("carried") == 3 and content.get("deferred") == 2,
              {"content": content, "reasons": prep(row).get("content_reasons")})
        renders, mixes = DEFERRED_WORK[label]
        check(case, f"{label}: renders {renders}, mixes {mixes}", row["work"] == {"render": renders, "mix": mixes},
              {"work": row["work"], "settled": row["settled"]})
        check(case, f"{label}: settled in set_prompt", row["settled"].get("outcome") == "settled"
              and row["settled"].get("hits", 0) + row["settled"].get("misses", 0) == 2, row["settled"])
        check(case, f"{label}: the Editor body ran once", len(row["editor_runs"]) == 1, len(row["editor_runs"]))
        if label in DEFERRED_RERUN and MODE in ("classic", "ram"):
            reran = executed_nodes(row) & MEDIA
            check(case, f"{label}: exactly the changed output's consumers rerun", reran == DEFERRED_RERUN[label],
                  {"ran": sorted(reran), "expected": sorted(DEFERRED_RERUN[label])})
            check(case, f"{label}: no carrier executes", not any(str(node).startswith("sonder-media")
                                                                for node in row["executing"]), row["executing"])
    if MODE != "none":
        # The Editor's progress opens for the early run and for the scheduled Editor;
        # a miss opens it once more while settling, a hit never.
        hit, miss = pipeline_rows[1][1], pipeline_rows[2][1]
        check(case, "a hit opens no render stage at the Editor; a miss opens one while settling",
              miss["progress_calls"].count(("start", "1")) == hit["progress_calls"].count(("start", "1")) + 1,
              {"hit": hit["progress_calls"], "miss": miss["progress_calls"]})


def drift_fixture(label, runs):
    fx = fixture(label, [{"text": "Drift job", "end": 12, "snapshot": 0}] * runs, clip=True, frames=16)
    return fx


def drift_graph(fx, mask_pre=0):
    return {
        "1": editor(fx, selection_start=4, selection_end=12, pre_context_frames=4, post_context_frames=4,
                    mask_pre_offset=mask_pre, take_placement_muted=True),
        "fd": n("CPMediaDigest", frames=["1", 1]), "fdp": n("PreviewAny", source=["fd", 0]),
        "after": n("CPAfter", image=["1", 1], a=["fd", 0], b=["fd", 0], c=["fd", 0], d=["fd", 0], e=["fd", 0],
                   f=["fd", 0]),
        "7": {"class_type": "SonderSaveVideo", "inputs": dict(
            project=["1", 0], frames=["after", 0], filename_prefix="cph", fps=24.0, mode="Take",
            place_audio_on_timeline=False, mark_queue_complete=True, embed_metadata=False,
            color_drift_correction=True)},
    }


def drift_record(params):
    record = params.get(sonder("server.color_correction").COLOR_DRIFT_METADATA_KEY) if params else None
    return json.loads(json.dumps(record, default=str, sort_keys=True)) if record is not None else None


async def case_deferred_shapes():
    """Graph shapes with deferred media that must equal stock (classic)."""
    case = "deferred_shapes"

    def with_project_reader(fx):
        graph = content_graph(fx)
        graph["fp"] = n("CPFramesProject", frames=["1", 1], project=["1", 0])
        graph["fpp"] = n("PreviewAny", source=["fp", 0])
        return graph

    rows = {}
    for enabled in (True, False):
        set_pipeline(enabled)
        fx = content_fixture(f"dshape-{int(enabled)}", 3)
        executor = new_executor()
        rows[enabled] = [await run(executor, with_project_reader(fx)) for _ in range(2)]
    for index, label in enumerate(("first", "identical")):
        row, stock = rows[True][index], rows[False][index]
        check(case, f"a carried consumer and a project reader of one slot ({label}): equals stock",
              row.get("success") and row["outputs"] == stock["outputs"], _diff(row["outputs"], stock["outputs"]))
    check(case, "a carried consumer and a project reader of one slot: one render on a miss, none on a hit",
          rows[True][0]["work"]["render"] == 1 and rows[True][1]["work"]["render"] == 0,
          [row["work"] for row in rows[True]])
    check(case, "the project reader reruns and still gets the frames",
          "fp" in executed_nodes(rows[True][1]), rows[True][1]["executing"])

    # A fractional clip start: no identity, rendered as today, equal to stock.
    frac = {}
    for enabled in (True, False):
        set_pipeline(enabled)
        fx = content_fixture(f"dfrac-{int(enabled)}", 3)
        edit_project(fx, lambda project, scene: setattr(scene.clips[0], "timeline_start_frame", 0.5))
        executor = new_executor()
        frac[enabled] = [await run(executor, content_graph(fx)) for _ in range(2)]
    # Its frames have no recipe, so the early run renders them and they are carried by
    # content (Phase 4a); the audio keeps its pre-render identity.
    check(case, "a fractional clip start gets no identity, renders every run, and equals stock",
          all(row.get("success") for row in frac[True])
          and all(a["outputs"] == b["outputs"] for a, b in zip(frac[True], frac[False]))
          and prep(frac[True][1]).get("content", {}).get("deferred") == 1
          and [row["work"]["render"] for row in frac[True]] == [1, 1],
          {"content": prep(frac[True][1]).get("content"), "work": [row["work"] for row in frac[True]],
           "diff": _diff(frac[True][1]["outputs"], frac[False][1]["outputs"])})

    # A colour-correcting Save Video with context frames: the take's drift record equals stock on a hit
    # and after a mask offset change (which changes neither recipe).
    drift = {}
    for enabled in (True, False):
        set_pipeline(enabled)
        fx = drift_fixture(f"ddrift-{int(enabled)}", 4)
        executor = new_executor()
        drift[enabled] = []
        for mask_pre in (0, 0, 2):
            row = await run(executor, drift_graph(fx, mask_pre))
            drift[enabled].append((row, drift_record(last_take_params(fx))))
    for index, label in enumerate(("first", "identical (hit)", "mask offset changed")):
        (row, record), (stock, stock_record) = drift[True][index], drift[False][index]
        check(case, f"colour drift ({label}): the take's drift record equals stock",
              row.get("success") and record == stock_record and record is not None,
              {"pipeline": record, "stock": stock_record, "error": row.get("error")})
    check(case, "colour drift: the identical run and the mask offset change render nothing",
          [row["work"]["render"] for row, _ in drift[True]] == [1, 0, 0], [row["work"] for row, _ in drift[True]])


async def case_deferred_safety():
    """Failures while settling deferred media (classic, ram)."""
    case = "deferred_safety"
    set_pipeline(True)
    fx = content_fixture("dsafety", 12)
    executor = new_executor()
    base = await run(executor, content_graph(fx))
    check(case, "base run succeeds", base.get("success"), base.get("error"))

    # A render that raises while settling: replayed at the Editor, its job failed once, the worker survives.
    edit_project(fx, lambda project, scene: setattr(scene.clips[0], "opacity", 0.7))
    INJECT["render_error"] = True
    try:
        row = await run(executor, content_graph(fx))
    finally:
        INJECT["render_error"] = False
    check(case, "a render raise while settling is reported at the Editor",
          not row.get("success") and (row.get("error") or {}).get("node") == "1"
          and "Injected render failure" in (row.get("error") or {}).get("message", ""),
          {"error": row.get("error"), "settled": row["settled"]})
    check(case, "a render raise while settling rolls the carriers back",
          row["settled"].get("outcome") == "rollback", {"settled": row["settled"]})
    # Its value substitutions go too: the guide_idx consumer ("gi") waits for the Editor,
    # which replays the failure, so it never runs (as after a failed early run).
    check(case, "a render raise while settling: no consumer of an Editor value runs",
          "gi" not in executed_nodes(row) and not any(key.startswith("text:") for key in row["calls"]),
          {"executing": row["executing"], "calls": row["calls"]})
    failed = [job for job in jobs_of(fx).values() if job["status"] == "failed"]
    check(case, "a render raise while settling marks its job failed once", len(failed) == 1, jobs_of(fx))
    after = await run(executor, content_graph(fx))
    check(case, "the next run after a settle raise works", after.get("success"), after.get("error"))

    # A cancel while settling: interrupted, its job returned to pending, the next run works.
    edit_project(fx, lambda project, scene: setattr(scene.clips[0], "opacity", 0.8))
    statuses_before = jobs_of(fx)
    INJECT["interrupt_at"] = 1
    try:
        row = await run(executor, content_graph(fx))
    finally:
        INJECT["interrupt_at"] = None
        mm.interrupt_current_processing(False)
    check(case, "a cancel while settling interrupts the run",
          not row.get("success") and (row.get("error") or {}).get("event") == "execution_interrupted",
          {"error": row.get("error"), "settled": row["settled"]})
    check(case, "a cancel while settling leaves no job failed or running",
          [job["status"] for job in jobs_of(fx).values() if job["status"] in ("failed", "running")]
          == [job["status"] for job in statuses_before.values() if job["status"] in ("failed", "running")],
          jobs_of(fx))
    after = await run(executor, content_graph(fx))
    check(case, "the next run after a settle cancel works", after.get("success"), after.get("error"))

    # A source rewritten between its hash and the render: the run fails, nothing is cached, the job is pending.
    edit_project(fx, lambda project, scene: setattr(scene.clips[0], "opacity", 0.9))
    clip_path = Path(fx["dir"]) / "media" / "timeline.mkv"
    real_identify = editor_media.PendingMedia.identify

    def identify_then_touch(self, slot, check_interrupt=None):
        result = real_identify(self, slot, check_interrupt)
        if result[0] and self.plans[slot].kind == "frames":
            with open(clip_path, "ab") as handle:
                handle.write(b"\0" * 16)
        return result

    editor_media.PendingMedia.identify = identify_then_touch
    try:
        row = await run(executor, content_graph(fx))
    finally:
        editor_media.PendingMedia.identify = real_identify
    check(case, "a source changed after its hash fails the run at the Editor",
          not row.get("success") and "changed while this run was reading it" in (row.get("error") or {}).get("message", ""),
          {"error": row.get("error"), "settled": row["settled"]})
    check(case, "a source changed after its hash marks no job failed",
          sum(job["status"] == "failed" for job in jobs_of(fx).values()) == 1, jobs_of(fx))
    after = await run(executor, content_graph(fx))
    check(case, "the run after a changed source renders again and succeeds",
          after.get("success") and after["work"]["render"] == 1, {"error": after.get("error"), "work": after["work"]})

    # Pipeline faults: each ends in a correct render, equal to stock.
    real_entry_value, real_plan_carriers, real_leftovers = (
        pipeline._entry_value, pipeline.plan_carriers, pipeline._settle_leftovers)

    def broken(*args, **kwargs):
        raise ValueError("injected pipeline fault")

    faults = {"settle fault (carrier net)": ("_entry_value", broken),
              "media pass fault (rendered early)": ("plan_carriers", broken)}
    for index, (label, (name, replacement)) in enumerate(faults.items()):
        rows = {}
        for enabled in (True, False):
            set_pipeline(enabled)
            fxf = content_fixture(f"dfault{index}-{int(enabled)}", 3)
            executor_f = new_executor()
            setattr(pipeline, name, replacement)
            try:
                rows[enabled] = await run(executor_f, content_graph(fxf))
            finally:
                pipeline._entry_value, pipeline.plan_carriers = real_entry_value, real_plan_carriers
        row = rows[True]
        check(case, f"{label}: the run succeeds and equals stock",
              row.get("success") and row["outputs"] == rows[False]["outputs"],
              {"error": row.get("error"), "diff": _diff(row["outputs"], rows[False]["outputs"]),
               "settled": row["settled"], "reasons": prep(row).get("content_reasons")})
        check(case, f"{label}: rendered and mixed once", row["work"] == {"render": 1, "mix": 1}, row["work"])
    # The media pass and the early render both fault: the scheduled Editor settles.
    rows = {}
    for enabled in (True, False):
        set_pipeline(enabled)
        fxf = content_fixture(f"dfault9-{int(enabled)}", 3)
        executor_f = new_executor()
        pipeline.plan_carriers, pipeline._settle_leftovers = broken, broken
        try:
            rows[enabled] = await run(executor_f, content_graph(fxf))
        finally:
            pipeline.plan_carriers, pipeline._settle_leftovers = real_plan_carriers, real_leftovers
    check(case, "every pipeline fault: the scheduled Editor's net renders, equal to stock",
          rows[True].get("success") and rows[True]["outputs"] == rows[False]["outputs"]
          and rows[True]["work"] == {"render": 1, "mix": 1},
          {"error": rows[True].get("error"), "work": rows[True]["work"],
           "diff": _diff(rows[True]["outputs"], rows[False]["outputs"])})


# --- Early release: obsolete entries released before a deferred render ---------------------------

RELEASES = []
_real_release_obsolete = hook._release_obsolete


def recording_release_obsolete(early, outputs_cache, executor):
    """The hook's release, with its RAM call spied on (target and arguments)."""
    calls = []
    real = getattr(outputs_cache, "ram_release", None)
    if real is not None:
        def spy(target, *args, **kwargs):
            calls.append({"target": target, "args": list(args), "kwargs": sorted(kwargs)})
            return real(target, *args, **kwargs)
        outputs_cache.ram_release = spy
    try:
        result = _real_release_obsolete(early, outputs_cache, executor)
    finally:
        if real is not None:
            outputs_cache.__dict__.pop("ram_release", None)
    RELEASES.append({"result": result, "ram_calls": calls})
    return result


RELEASED_RECORDS = []
_real_note_released = pipeline._note_released


def recording_note_released(key, released):
    """The real merge, then the run's settled record as sdk_runs now holds it."""
    _real_note_released(key, released)
    RELEASED_RECORDS.append((sdk_runs.preparation(key) or {}).get("settled"))


pipeline._note_released = recording_note_released


def memory_graph(fx, ed="1"):
    """No node passes the Editor's frames through, so only cache entries can hold them."""
    return {
        ed: editor(fx, selection_end=4, take_placement_muted=True),
        "fd": n("CPMediaDigest", frames=[ed, 1]), "fdp": n("PreviewAny", source=["fd", 0]),
        "gd": n("CPMediaDigest", frames=[ed, 2]), "gdp": n("PreviewAny", source=["gd", 0]),
        "ad": n("CPAudioDigest", audio=[ed, 10]), "adp": n("PreviewAny", source=["ad", 0]),
        "el": n("EmptyLatentImage", width=64, height=64, batch_size=1), "elp": n("PreviewAny", source=["el", 0]),
    }


MEMORY_NODES = {"fd", "gd", "ad", "el"}
GIB = 1024 ** 3


def memory_arms():
    """(label, cache_args, retains): whether this arm keeps the previous run's results by its own policy."""
    if MODE == "classic":
        return [("classic", {}, False)]
    if MODE == "lru":
        return [("lru over capacity", {"lru": 2}, False), ("lru within capacity", {"lru": 100}, True)]
    return [("ram production", production_ram_args(), False),
            # No inactive headroom: the executor keeps inactive entries, so must the release.
            ("ram no inactive headroom", {"ram": 0, "ram_inactive": 0}, True),
            # A real threshold far below what is free: nothing inactive is released either.
            ("ram threshold met", {"ram": 0, "ram_inactive": 1 / 1024}, True)]


async def run_isolated(executor, graph):
    """One event loop per prompt, as ComfyUI's prompt_worker (PromptExecutor.execute -> asyncio.run)."""
    return await asyncio.to_thread(asyncio.run, run(executor, graph))


# (step, clip opacity, audio volume, (renders, mixes) for an arm that does not retain, consumers that rerun).
MEMORY_STEPS = (
    ("base", 1.0, 1.0, (1, 1), MEMORY_NODES),
    ("identical", 1.0, 1.0, (0, 0), set()),
    ("clip edit", 0.5, 1.0, (1, 0), {"fd"}),
    ("clip revert", 1.0, 1.0, (1, 0), {"fd"}),
    ("audio edit", 1.0, 0.5, (0, 1), {"ad"}),
    ("both edited", 0.25, 0.25, (1, 1), {"fd", "ad"}),
)


async def memory_sequence(enabled, label, args):
    set_pipeline(enabled)
    fx = content_fixture(f"dmem-{label.replace(' ', '-')}-{int(enabled)}", len(MEMORY_STEPS) + 6)
    cache_type, mode_args = CACHE
    executor = execution.PromptExecutor(SERVER, cache_type=cache_type,
                                        cache_args={"ram": 0, "ram_inactive": 0, **mode_args, **args})
    CURRENT["executor"] = executor
    rows = []
    for name, opacity, volume, _work, _rerun in MEMORY_STEPS:
        def edit(project, scene, opacity=opacity, volume=volume):
            scene.clips[0].opacity = opacity
            scene.audio_tracks[0].volume = volume
        edit_project(fx, edit)
        RELEASES.clear()
        RELEASED_RECORDS.clear()
        FRAME_WATCH["alive_at_start"] = []
        renders_before = len(FRAME_WATCH["refs"])
        row = await run_isolated(executor, memory_graph(fx))
        # Non-vacuous residency: the frames this prompt rendered are the ones its cache now holds.
        held = (len(FRAME_WATCH["refs"]) == renders_before + 1 and not FRAME_WATCH["refs"][-1].expired()
                if row["work"]["render"] else None)
        rows.append((name, row, list(RELEASES), list(FRAME_WATCH["alive_at_start"]),
                     list(RELEASED_RECORDS), held))
    return fx, executor, rows


def no_release(early, outputs_cache, executor):
    RELEASES.append({"result": {}, "ram_calls": []})
    return {}


async def case_deferred_memory():
    """A deferred miss releases what ComfyUI releases next, before it renders (classic, lru, ram)."""
    case = "deferred_memory"
    hook._release_obsolete = recording_release_obsolete
    FRAME_WATCH.update(on=True, refs=[], alive_at_start=[])
    try:
        if MODE == "classic":
            # Negative control: with the release a no-op, the previous frames are resident at render start.
            hook._release_obsolete = no_release
            try:
                _, _, control = await memory_sequence(True, "release disabled", {})
            finally:
                hook._release_obsolete = recording_release_obsolete
            FRAME_WATCH["refs"] = []
            alive = {name: alive for name, _row, _releases, alive, _records, _held in control}
            check(case, "negative control (release disabled): the previous frames are resident at every re-render",
                  alive["clip edit"] == alive["clip revert"] == alive["both edited"] == [True], alive)
        for arm, args, retains in memory_arms():
            fx, executor, rows = await memory_sequence(True, arm, args)
            FRAME_WATCH["refs"] = []
            _, _, stock_rows = await memory_sequence(False, arm, args)
            FRAME_WATCH["refs"] = []
            set_pipeline(True)  # the oracle arm uninstalled the hook; the checks below need it
            for (name, row, releases, alive, records, held), (_, stock, *_rest) in zip(rows, stock_rows):
                step = next(item for item in MEMORY_STEPS if item[0] == name)
                renders, mixes = step[3]
                rerun = step[4]
                if retains and name == "clip revert":
                    renders, rerun = 0, set()  # this arm still holds the base run's frames: a hit
                label = f"{arm}, {name}"
                check(case, f"{label}: succeeds and every consumer equals stock",
                      row.get("success") and stock.get("success") and row["outputs"] == stock["outputs"],
                      {"error": row.get("error"), "diff": _diff(row["outputs"], stock["outputs"])})
                check(case, f"{label}: renders {renders}, mixes {mixes}", row["work"] == {"render": renders, "mix": mixes},
                      {"work": row["work"], "settled": row["settled"]})
                check(case, f"{label}: exactly the changed outputs' consumers rerun; every hit is kept",
                      executed_nodes(row) & MEMORY_NODES == rerun,
                      {"ran": sorted(executed_nodes(row) & MEMORY_NODES), "expected": sorted(rerun)})
                missed = renders + mixes > 0 and name != "base"
                check(case, f"{label}: the release runs once on a miss and never on a hit",
                      len(releases) == (1 if renders + mixes > 0 else 0), releases)
                if renders:
                    check(case, f"{label}: the rendered frames are the ones the cache holds after the run",
                          held is True, held)
                if renders and name != "base":
                    check(case, f"{label}: the previous frames are {'still' if retains else 'no longer'} resident "
                                f"when the render starts", alive == [retains], alive)
                if releases:
                    record = records[0] if len(records) == 1 else None
                    check(case, f"{label}: the run's settled record carries the release",
                          isinstance(record, dict) and record.get("released") == releases[0]["result"]
                          and record.get("outcome") == "settled" and record.get("misses") == renders + mixes,
                          {"records": records, "release": releases[0]["result"]})
                if missed and releases and MODE == "ram":
                    target = int(args["ram_inactive"] * GIB)
                    result = releases[0]["result"]
                    check(case, f"{label}: ram_release once, to the executor's inactive headroom, free_active False",
                          result.get("ram") == "released"
                          and releases[0]["ram_calls"] == [{"target": target, "args": [], "kwargs": []}],
                          releases[0])
                elif missed and releases:
                    check(case, f"{label}: clean_unused only", "ram" not in releases[0]["result"]
                          and not releases[0]["ram_calls"], releases[0])

            if retains:
                continue
            # A raise and a cancel during the render, after the release: rolled back, the next run works.
            edit_project(fx, lambda project, scene: setattr(scene.clips[0], "opacity", 0.6))
            INJECT["render_error"] = True
            RELEASES.clear()
            try:
                row = await run_isolated(executor, memory_graph(fx))
            finally:
                INJECT["render_error"] = False
            check(case, f"{arm}: a render raise after the release is reported at the Editor",
                  not row.get("success") and (row.get("error") or {}).get("node") == "1" and len(RELEASES) == 1,
                  {"error": row.get("error"), "releases": list(RELEASES)})
            after = await run_isolated(executor, memory_graph(fx))
            check(case, f"{arm}: the run after a raise renders and succeeds",
                  after.get("success") and after["work"]["render"] == 1, {"error": after.get("error"),
                                                                           "work": after["work"]})
            edit_project(fx, lambda project, scene: setattr(scene.clips[0], "opacity", 0.7))
            INJECT["interrupt_at"] = 1
            RELEASES.clear()
            try:
                row = await run_isolated(executor, memory_graph(fx))
            finally:
                INJECT["interrupt_at"] = None
                mm.interrupt_current_processing(False)
            check(case, f"{arm}: a cancel during the render after the release interrupts the run",
                  not row.get("success") and (row.get("error") or {}).get("event") == "execution_interrupted"
                  and len(RELEASES) == 1, {"error": row.get("error"), "releases": list(RELEASES)})
            # Back to the last completed timeline: what the cancelled prompt released is gone, so it
            # renders again (in RAM mode stock keeps inactive entries after a failed prompt: the accepted cost).
            edit_project(fx, lambda project, scene: setattr(scene.clips[0], "opacity", 0.6))
            after = await run_isolated(executor, memory_graph(fx))
            check(case, f"{arm}: after a cancel, going back renders again and succeeds",
                  after.get("success") and after["work"]["render"] == 1,
                  {"error": after.get("error"), "work": after["work"]})
            if MODE == "ram":
                # A cancel already pending when the release runs: clean_unused only, no ram_release.
                def cancel_then_release(early, outputs_cache, executor):
                    mm.interrupt_current_processing(True)
                    return recording_release_obsolete(early, outputs_cache, executor)

                edit_project(fx, lambda project, scene: setattr(scene.clips[0], "opacity", 0.45))
                hook._release_obsolete = cancel_then_release
                RELEASES.clear()
                try:
                    row = await run_isolated(executor, memory_graph(fx))
                finally:
                    hook._release_obsolete = recording_release_obsolete
                    mm.interrupt_current_processing(False)
                check(case, f"{arm}: with a cancel pending the release skips ram_release and the run is interrupted",
                      not row.get("success") and (row.get("error") or {}).get("event") == "execution_interrupted"
                      and len(RELEASES) == 1 and RELEASES[0]["result"].get("ram") == "interrupted"
                      and not RELEASES[0]["ram_calls"], {"error": row.get("error"), "releases": list(RELEASES)})
                edit_project(fx, lambda project, scene: setattr(scene.clips[0], "opacity", 0.6))
                after = await run_isolated(executor, memory_graph(fx))
                check(case, f"{arm}: the run after a pending cancel succeeds", after.get("success"), after.get("error"))
            # After ComfyUI's cache reset (Free model and node cache): the release still applies.
            executor.reset()
            CURRENT["executor"] = executor
            FRAME_WATCH["refs"] = []
            cold = await run_isolated(executor, memory_graph(fx))
            edit_project(fx, lambda project, scene: setattr(scene.clips[0], "opacity", 0.35))
            RELEASES.clear()
            FRAME_WATCH["alive_at_start"] = []
            row = await run_isolated(executor, memory_graph(fx))
            check(case, f"{arm}: after executor.reset() the release still runs before the render",
                  cold.get("success") and row.get("success") and len(RELEASES) == 1
                  and FRAME_WATCH["alive_at_start"] == [False]
                  and (MODE != "ram" or RELEASES[0]["result"].get("ram") == "released"),
                  {"releases": list(RELEASES), "alive": FRAME_WATCH["alive_at_start"], "error": row.get("error"),
                   "work": row["work"], "settled": row["settled"], "content": prep(row).get("content"),
                   "reasons": prep(row).get("content_reasons"), "outcome": prep(row).get("outcome")})
    finally:
        hook._release_obsolete = _real_release_obsolete
        FRAME_WATCH.update(on=False, refs=[], alive_at_start=[])


NOTES_OUT = {}
CASES = {"acceptance": case_acceptance, "fail_open": case_fail_open, "tokens": case_tokens, "cancel": case_cancel,
         "errors": case_errors, "shapes": case_shapes, "lazy": case_lazy, "ram": case_ram, "misc": case_misc,
         "bridges": case_bridges, "bridges_real": case_bridges_real, "bridges_shapes": case_bridges_shapes,
         "bridges_safety": case_bridges_safety, "bridges_ram": case_bridges_ram,
         "bridges_snapshot": case_bridges_snapshot, "bridges_negative": case_bridges_negative,
         "identity": case_identity, "identity_lazy": case_identity_lazy, "identity_safety": case_identity_safety,
         "identity_ram": case_identity_ram, "identity_shapes": case_identity_shapes,
         "content": case_content, "content_negative": case_content_negative, "content_shapes": case_content_shapes,
         "content_safety": case_content_safety, "deferred": case_deferred,
         "deferred_shapes": case_deferred_shapes, "deferred_safety": case_deferred_safety,
         "deferred_memory": case_deferred_memory}
BY_MODE = {
    "classic": ["acceptance", "fail_open", "tokens", "cancel", "errors", "shapes", "lazy", "misc",
                "bridges", "bridges_real", "bridges_snapshot", "bridges_shapes", "bridges_safety", "bridges_negative",
                "identity", "identity_lazy", "identity_safety", "identity_shapes",
                "content", "content_negative", "content_shapes", "content_safety",
                "deferred", "deferred_shapes", "deferred_safety", "deferred_memory"],
    "lru": ["acceptance", "cancel", "bridges", "identity", "content", "deferred", "deferred_memory"],
    "ram": ["acceptance", "cancel", "errors", "ram", "bridges", "bridges_real", "bridges_snapshot", "bridges_ram",
            "identity", "identity_ram", "content", "content_safety", "deferred", "deferred_safety",
            "deferred_memory"],
    "none": ["acceptance", "bridges", "identity", "content", "deferred"],
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
