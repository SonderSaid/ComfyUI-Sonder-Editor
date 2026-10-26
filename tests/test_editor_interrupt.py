"""The Editor stops on ComfyUI's interrupt, treats it as a cancellation (never a
job failure), and reports its render through ComfyUI's native progress bar."""

import importlib
import os
import sys
import types
from pathlib import Path

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "video_editor_testpkg"
FRAMES = 12


class FakeInterrupt(BaseException):
    """ComfyUI 0.20+ derives InterruptProcessingException from BaseException."""


class FakeModelManagement(types.ModuleType):
    InterruptProcessingException = FakeInterrupt

    def __init__(self):
        super().__init__("comfy.model_management")
        self.requested = False

    def processing_interrupted(self):
        return self.requested

    def throw_exception_if_processing_interrupted(self):
        if self.requested:
            self.requested = False
            raise FakeInterrupt()


class FakeProgressBar:
    """ComfyUI 0.42's ProgressBar (comfy/utils.py) with a recording hook: the first
    and final values always reach the hook, others only after 100 ms and 0.5 %
    of progress, and the hook raises the interrupt when one was requested."""

    bars = []
    raise_at = None   # a value whose update raises ComfyUI's interrupt, as its hook does
    model_management = None
    clock = [0.0]     # advanced by the tests instead of real time

    def __init__(self, total, node_id=None):
        self.total = total
        self.values = []  # what the hook received
        self.last_sent = -1
        self.last_time = 0.0
        FakeProgressBar.bars.append(self)

    def _hook(self, value):
        mm = FakeProgressBar.model_management
        if mm is not None:
            mm.throw_exception_if_processing_interrupted()
        if FakeProgressBar.raise_at is not None and value == FakeProgressBar.raise_at:
            raise FakeInterrupt()
        self.values.append((value, self.total))

    def update_absolute(self, value, total=None, preview=None):
        if total is not None:
            self.total = total
        value = min(value, self.total)
        now = FakeProgressBar.clock[0]
        if self.last_sent < 0 or value >= self.total:
            self._hook(value)
            self.last_sent, self.last_time = value, now
            return
        percent = (value - max(0, self.last_sent)) / self.total * 100 if self.total > 0 else 100
        if now - self.last_time >= 0.1 and percent >= 0.5:
            self._hook(value)
            self.last_sent, self.last_time = value, now


@pytest.fixture
def comfy(monkeypatch):
    model_management = FakeModelManagement()
    utils = types.ModuleType("comfy.utils")
    utils.ProgressBar = FakeProgressBar
    package = types.ModuleType("comfy")
    package.model_management = model_management
    package.utils = utils
    monkeypatch.setitem(sys.modules, "comfy", package)
    monkeypatch.setitem(sys.modules, "comfy.model_management", model_management)
    monkeypatch.setitem(sys.modules, "comfy.utils", utils)
    FakeProgressBar.bars = []
    FakeProgressBar.raise_at = None
    FakeProgressBar.model_management = model_management
    FakeProgressBar.clock = [0.0]
    return model_management


def _import_editor_node(tmp_path, monkeypatch):
    pytest.importorskip("torch")
    pytest.importorskip("cv2")
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(get_output_directory=lambda: str(tmp_path)))
    if TEST_PACKAGE not in sys.modules:
        pkg = types.ModuleType(TEST_PACKAGE)
        pkg.__path__ = [str(ROOT)]
        sys.modules[TEST_PACKAGE] = pkg
    importlib.invalidate_caches()
    return importlib.import_module(f"{TEST_PACKAGE}.nodes.editor_node")


class Decoder:
    """A capture that advances on read and can request an interrupt at a frame."""

    def __init__(self, log, model_management, interrupt_at):
        self.log, self.model_management, self.interrupt_at = log, model_management, interrupt_at
        self.position = 0
        log["opened"] += 1

    def isOpened(self):
        return True

    def set(self, _prop, value):
        self.position = int(value)
        return True

    def grab(self):
        self.position += 1
        return True

    def read(self):
        frame = self.position
        self.position += 1
        self.log["reads"].append(frame)
        FakeProgressBar.clock[0] += 0.2
        if frame == self.interrupt_at:
            self.model_management.requested = True
        return True, np.full((4, 4, 3), frame, dtype=np.uint8)

    def release(self):
        self.log["released"] += 1


def _queued_project(tmp_path, monkeypatch, editor_node, *, interrupt_at=None, model_management=None):
    """A project with a 2-job batch whose first job the Editor claims."""
    from server.project_manager import create_project, load_project, save_project
    from server.timeline_state import Asset, ClipReference, GenerationJob, LaneConfig, Scene

    base = tmp_path / "projects"
    base.mkdir()
    created = create_project("Interrupts", 24.0, 4, 4, "free", str(base))
    project = load_project(created.project_dir)
    media = Path(created.project_dir) / "media"
    media.mkdir(exist_ok=True)
    (media / "clip.mp4").write_bytes(b"video")
    project.assets.append(Asset(asset_id="clip", name="clip.mp4", path=os.path.join("media", "clip.mp4"),
                                asset_type="video", fps=24.0, frame_count=48, color_space="rgb",
                                color_range="pc", color_probed=True))
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=FRAMES, video_lane_configs=[LaneConfig()])
    scene.clips = [ClipReference(source_path=os.path.join("media", "clip.mp4"), timeline_start_frame=0,
                                 timeline_end_frame=FRAMES)]
    project.add_scene(scene)
    project.generation_queue = [
        GenerationJob(job_id="job-1", scene_id="scene-1", status="running", batch_id="b", batch_index=0,
                      batch_total=2, selection_start=0, selection_end=FRAMES),
        GenerationJob(job_id="job-2", scene_id="scene-1", status="pending", batch_id="b", batch_index=1,
                      batch_total=2, selection_start=0, selection_end=FRAMES),
    ]
    save_project(project, notify=False)
    loaded = load_project(created.project_dir)
    claims = []

    def claim(self, proj):
        claims.append(proj.generation_queue[0].job_id)
        return proj, proj.generation_queue[0]

    monkeypatch.setattr(editor_node, "load_project", lambda _dir: loaded)
    monkeypatch.setattr(editor_node, "_resolve_project_choice_dir", lambda _base, _choice: created.project_dir)
    monkeypatch.setattr(editor_node.SonderEditor, "_execution_reaches_terminal_save",
                        lambda self, prompt, unique_id, scope=None: True)
    monkeypatch.setattr(editor_node.SonderEditor, "_consume_queue_job", claim)
    monkeypatch.setattr(editor_node.SonderEditor, "_load_scene_audio",
                        lambda self, proj, scene, start, end, **_kwargs: editor_node._make_silent_audio(0.5))
    log = {"reads": [], "opened": 0, "released": 0}
    renderer = sys.modules[f"{TEST_PACKAGE}.server.timeline_renderer"]
    monkeypatch.setattr(renderer, "_default_video_capture",
                        lambda _path: Decoder(log, model_management, interrupt_at))
    return created.project_dir, log, claims


def _execute(editor_node):
    return editor_node.SonderEditor().execute(
        project="Interrupts", project_name="Interrupts", fps=24.0, width=4, height=4, scene_id="scene-1",
        selection_start=0, selection_end=FRAMES)


def _jobs(project_dir):
    from server.project_manager import load_project
    return {job.job_id: (job.status, job.error) for job in load_project(project_dir).generation_queue}


def test_an_interrupt_mid_render_stops_it_and_leaves_the_claimed_job_to_its_release(tmp_path, monkeypatch, comfy):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    project_dir, log, _claims = _queued_project(tmp_path, monkeypatch, editor_node, interrupt_at=3,
                                                model_management=comfy)
    with pytest.raises(FakeInterrupt):
        _execute(editor_node)
    assert log["reads"] == [0, 1, 2, 3]          # stopped at the next frame's check
    assert log["released"] == log["opened"] == 1
    assert comfy.requested is False              # raised and cleared, as native nodes do
    # Not a failure: the run's release returns job-1 to pending; job-2 is untouched.
    assert _jobs(project_dir) == {"job-1": ("running", ""), "job-2": ("pending", "")}


def test_an_interrupt_raised_by_the_progress_hook_is_also_a_cancellation(tmp_path, monkeypatch, comfy):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    project_dir, log, _claims = _queued_project(tmp_path, monkeypatch, editor_node, model_management=comfy)
    FakeProgressBar.raise_at = 5
    with pytest.raises(FakeInterrupt):
        _execute(editor_node)
    assert max(log["reads"]) == 4
    assert log["released"] == log["opened"] == 1
    assert _jobs(project_dir) == {"job-1": ("running", ""), "job-2": ("pending", "")}


def test_an_interrupt_before_the_claim_saves_no_claim(tmp_path, monkeypatch, comfy):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    _project_dir, log, claims = _queued_project(tmp_path, monkeypatch, editor_node, model_management=comfy)
    comfy.requested = True
    with pytest.raises(FakeInterrupt):
        _execute(editor_node)
    assert claims == []
    assert log["reads"] == []


def test_an_interrupt_during_the_audio_mix_is_a_cancellation(tmp_path, monkeypatch, comfy):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    project_dir, _log, _claims = _queued_project(tmp_path, monkeypatch, editor_node, model_management=comfy)

    def mixing(self, proj, scene, start, end, *, cancel_event=None):
        comfy.requested = True
        assert cancel_event.is_set()
        raise editor_node.MediaOperationCancelled("Audio processing cancelled")

    monkeypatch.setattr(editor_node.SonderEditor, "_load_scene_audio", mixing)
    with pytest.raises(FakeInterrupt):
        _execute(editor_node)
    assert _jobs(project_dir) == {"job-1": ("running", ""), "job-2": ("pending", "")}


def test_an_ordinary_render_error_still_fails_the_claimed_job_and_its_batch(tmp_path, monkeypatch, comfy):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    project_dir, _log, _claims = _queued_project(tmp_path, monkeypatch, editor_node, model_management=comfy)

    def broken(*_args, **_kwargs):
        raise RuntimeError("decoder exploded")

    monkeypatch.setattr(editor_node.SonderEditor, "_render_scene_frames", broken)
    with pytest.raises(RuntimeError):
        _execute(editor_node)
    jobs = _jobs(project_dir)
    assert jobs["job-1"] == ("failed", "decoder exploded")
    assert jobs["job-2"][0] == "failed"


def test_the_render_bar_reaches_max_only_when_the_render_and_padding_are_done(tmp_path, monkeypatch, comfy):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    _queued_project(tmp_path, monkeypatch, editor_node, model_management=comfy)
    _execute(editor_node)
    (bar,) = FakeProgressBar.bars
    values = [value for value, _total in bar.values]
    assert values[0] == 0 and values[-1] == FRAMES
    assert values[:-1] == sorted(values[:-1]) and max(values[:-1]) == FRAMES - 1
    assert {total for _value, total in bar.values} == {FRAMES}


def test_a_restarted_count_gets_a_new_bar_and_an_unavailable_bar_never_fails(tmp_path, monkeypatch, comfy):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    # A reused bar would freeze: ComfyUI's throttle drops a value below the last sent one.
    reused = FakeProgressBar(10)
    for value in (0, 6, 2):
        FakeProgressBar.clock[0] += 1
        reused.update_absolute(value)
    assert reused.values == [(0, 10), (6, 10)]
    FakeProgressBar.bars = []

    sink = editor_node._RenderProgressBar(comfy)
    for done in (0, 3, 6, 0, 2):
        FakeProgressBar.clock[0] += 1
        sink(done, 10)
    sink.finish(10)
    assert [bar.values for bar in FakeProgressBar.bars] == [[(0, 10), (3, 10), (6, 10)], [(0, 10), (2, 10), (10, 10)]]

    # Held below max, a near-complete render never sends the final value early.
    held = editor_node._RenderProgressBar(comfy)
    FakeProgressBar.clock[0] += 1
    held(10, 10)
    assert FakeProgressBar.bars[-1].values == [(9, 10)]
    held.finish(10)
    assert FakeProgressBar.bars[-1].values == [(9, 10), (10, 10)]

    no_bar = types.ModuleType("comfy.utils")  # a core without ProgressBar
    monkeypatch.setitem(sys.modules, "comfy.utils", no_bar)
    monkeypatch.setattr(sys.modules["comfy"], "utils", no_bar)
    broken = editor_node._RenderProgressBar(comfy)
    broken(1, 10)
    broken.finish(10)
    assert broken.unavailable


def test_outside_comfyui_nothing_is_interrupted_and_no_bar_is_needed(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "comfy", None)
    signal = editor_node._ComfyInterruptSignal(editor_node._comfy_model_management())
    assert signal.model_management is None
    assert signal.is_set() is False
    signal.raise_if_set()
    with pytest.raises(RuntimeError):
        signal.raise_cancelled(RuntimeError("cancelled elsewhere"))


def test_an_interrupted_render_is_returned_to_pending_by_the_runs_release(tmp_path, monkeypatch, comfy):
    """End to end: the claim is leased under the run's release record, the Editor is
    interrupted mid-render, and the release returns the job with the interrupted note."""
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    project_dir, _log, _claims = _queued_project(tmp_path, monkeypatch, editor_node, interrupt_at=3,
                                                 model_management=comfy)
    runs = sys.modules[f"{TEST_PACKAGE}.server.sdk_runs"]
    runs.reset_for_tests()
    runs.open_release("run-1", "prompt-1")
    runs.note_project("run-1", project_dir, ["p"])

    def leased_claim(self, proj):
        runs.take_lease("run-1", proj.project_dir, proj.generation_queue[0].job_id)
        return proj, proj.generation_queue[0]

    monkeypatch.setattr(editor_node.SonderEditor, "_consume_queue_job", leased_claim)
    with pytest.raises(FakeInterrupt):
        _execute(editor_node)
    assert runs.holds_lease(project_dir, "job-1")

    folder_paths = sys.modules["folder_paths"]
    for name, sub in (("get_temp_directory", "temp"), ("get_input_directory", "input")):
        (tmp_path / sub).mkdir(exist_ok=True)
        setattr(folder_paths, name, lambda sub=sub: str(tmp_path / sub))
    folder_paths.filter_files_content_types = lambda files, _types: files
    io_nodes = importlib.import_module(f"{TEST_PACKAGE}.nodes.io_nodes")
    monkeypatch.setattr(io_nodes, "schedule_project_event", lambda *args, **kwargs: None)
    io_nodes._release_prompt_run("run-1", types.SimpleNamespace(
        status_str="error", messages=[("execution_interrupted", {})]))
    assert _jobs(project_dir) == {
        "job-1": ("pending", "The run was interrupted before this job completed."),
        "job-2": ("pending", ""),
    }
    runs.reset_for_tests()


def test_an_interrupt_requested_during_the_claim_stops_before_the_render(tmp_path, monkeypatch, comfy):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    _project_dir, log, _claims = _queued_project(tmp_path, monkeypatch, editor_node, model_management=comfy)

    def claim_then_interrupt(self, proj):
        comfy.requested = True
        return proj, proj.generation_queue[0]

    monkeypatch.setattr(editor_node.SonderEditor, "_consume_queue_job", claim_then_interrupt)
    with pytest.raises(FakeInterrupt):
        _execute(editor_node)
    assert log["reads"] == []


def test_an_interrupt_requested_as_the_render_returns_stops_before_inputs_are_prepared(tmp_path, monkeypatch, comfy):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    _queued_project(tmp_path, monkeypatch, editor_node, model_management=comfy)
    reached = []

    def render_then_interrupt(self, proj, scene, start, end, **_kwargs):
        comfy.requested = True
        import torch
        return torch.zeros(end - start, 4, 4, 3)

    monkeypatch.setattr(editor_node.SonderEditor, "_render_scene_frames", render_then_interrupt)
    monkeypatch.setattr(editor_node.SonderEditor, "_load_scene_audio",
                        lambda *args, **kwargs: reached.append("audio"))
    monkeypatch.setattr(editor_node, "build_context_reference_stash",
                        lambda *args, **kwargs: reached.append("stash"))
    with pytest.raises(FakeInterrupt):
        _execute(editor_node)
    assert reached == []
