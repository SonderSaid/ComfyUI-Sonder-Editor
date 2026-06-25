"""Tests for SonderMasksBridge per-channel mask-time gating.

The bridge module is pure-Python (no torch/cv2, no relative imports), so it
imports through the fake package like the other bridge tests for consistency.
"""

import importlib
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "video_editor_testpkg"

sys.path.insert(0, str(ROOT))

from server.timeline_state import GenerationJob, Scene, TimelineProject  # noqa: E402


def _import_masks_bridge():
    if TEST_PACKAGE not in sys.modules:
        pkg = types.ModuleType(TEST_PACKAGE)
        pkg.__path__ = [str(ROOT)]
        sys.modules[TEST_PACKAGE] = pkg
    importlib.invalidate_caches()
    return importlib.import_module(f"{TEST_PACKAGE}.nodes.masks_bridge")


def _project(*, project_fps=24.0, scene_fps=0.0, mask_start=12, mask_end=36,
             ctx="default", queue_job=None):
    """Build a project + execution context for the bridge.

    ctx="default" -> a valid context with the mask frames;
    ctx="missing-keys" -> a context dict without the mask keys;
    ctx=None -> no execution context at all.
    """
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=100)
    scene.fps = scene_fps
    project = TimelineProject(project_dir="", name="Project", fps=project_fps, scenes=[scene])
    if queue_job is not None:
        project.generation_queue = [queue_job]
    if ctx == "default":
        project._execution_context = {
            "scene_id": "scene-1",
            "mask_start_frame": mask_start,
            "mask_end_frame": mask_end,
            "queue_job_ref_id": queue_job.job_id if queue_job is not None else "",
        }
    elif ctx == "missing-keys":
        project._execution_context = {"scene_id": "scene-1"}
    # ctx is None -> leave _execution_context unset
    return project, scene


def test_both_edit_passes_full_window():
    mb = _import_masks_bridge()
    project, _ = _project()  # fps 24, mask 12..36 -> 0.5s .. 1.5s
    r = mb.resolve_mask_times(project, edit_video=True, edit_audio=True)
    assert r["video_mask_start_time"] == pytest.approx(0.5)
    assert r["video_mask_end_time"] == pytest.approx(1.5)
    assert r["audio_mask_start_time"] == pytest.approx(0.5)
    assert r["audio_mask_end_time"] == pytest.approx(1.5)


def test_freeze_video_collapses_video_only():
    mb = _import_masks_bridge()
    project, _ = _project()
    r = mb.resolve_mask_times(project, edit_video=False, edit_audio=True)
    # video collapses to start; audio untouched
    assert r["video_mask_start_time"] == pytest.approx(0.5)
    assert r["video_mask_end_time"] == pytest.approx(0.5)
    assert r["audio_mask_start_time"] == pytest.approx(0.5)
    assert r["audio_mask_end_time"] == pytest.approx(1.5)


def test_freeze_audio_collapses_audio_only():
    mb = _import_masks_bridge()
    project, _ = _project()
    r = mb.resolve_mask_times(project, edit_video=True, edit_audio=False)
    assert r["video_mask_start_time"] == pytest.approx(0.5)
    assert r["video_mask_end_time"] == pytest.approx(1.5)
    assert r["audio_mask_start_time"] == pytest.approx(0.5)
    assert r["audio_mask_end_time"] == pytest.approx(0.5)


def test_both_freeze_collapses_both():
    mb = _import_masks_bridge()
    project, _ = _project()
    r = mb.resolve_mask_times(project, edit_video=False, edit_audio=False)
    assert r["video_mask_start_time"] == pytest.approx(0.5)
    assert r["video_mask_end_time"] == pytest.approx(0.5)
    assert r["audio_mask_start_time"] == pytest.approx(0.5)
    assert r["audio_mask_end_time"] == pytest.approx(0.5)


def test_scene_fps_override_drives_seconds():
    mb = _import_masks_bridge()
    project, _ = _project(project_fps=24.0, scene_fps=48.0)  # 12/48=0.25, 36/48=0.75
    r = mb.resolve_mask_times(project)
    assert r["video_mask_start_time"] == pytest.approx(0.25)
    assert r["video_mask_end_time"] == pytest.approx(0.75)


def test_queued_snapshot_frozen_fps_wins_over_live_scene():
    mb = _import_masks_bridge()
    job = GenerationJob(
        job_id="job-1", scene_id="scene-1", scene_fps=12.0,
        params={"snapshot_version": 1},
    )
    # Live scene fps is 48, but the frozen snapshot fps (12) must win.
    project, _ = _project(project_fps=24.0, scene_fps=48.0, queue_job=job)
    r = mb.resolve_mask_times(project)
    assert r["video_mask_start_time"] == pytest.approx(1.0)   # 12/12
    assert r["video_mask_end_time"] == pytest.approx(3.0)     # 36/12


def test_missing_execution_context_raises():
    mb = _import_masks_bridge()
    project, _ = _project(ctx=None)
    with pytest.raises(RuntimeError):
        mb.resolve_mask_times(project)


def test_context_without_mask_keys_raises():
    mb = _import_masks_bridge()
    project, _ = _project(ctx="missing-keys")
    with pytest.raises(RuntimeError):
        mb.resolve_mask_times(project)


def test_zero_fps_guard_yields_zero_seconds():
    mb = _import_masks_bridge()
    project, _ = _project(project_fps=0.0, scene_fps=0.0)
    r = mb.resolve_mask_times(project)
    assert r["video_mask_start_time"] == 0.0
    assert r["video_mask_end_time"] == 0.0
    assert r["audio_mask_start_time"] == 0.0
    assert r["audio_mask_end_time"] == 0.0


def test_execute_returns_four_floats_and_records_provenance():
    mb = _import_masks_bridge()
    project, _ = _project()
    node = mb.SonderMasksBridge()
    out = node.execute(project, edit_video=True, edit_audio=False)
    assert out == (pytest.approx(0.5), pytest.approx(1.5), pytest.approx(0.5), pytest.approx(0.5))

    prov = project._execution_context.get("masks_bridge")
    assert prov is not None
    assert prov["edit_video"] is True
    assert prov["edit_audio"] is False
    assert prov["video_mask"] == [pytest.approx(0.5), pytest.approx(1.5)]
    assert prov["audio_mask"] == [pytest.approx(0.5), pytest.approx(0.5)]


def test_node_contract():
    mb = _import_masks_bridge()
    node_cls = mb.SonderMasksBridge
    assert node_cls.RETURN_TYPES == ("FLOAT", "FLOAT", "FLOAT", "FLOAT")
    assert node_cls.RETURN_NAMES == (
        "video_mask_start_time", "video_mask_end_time",
        "audio_mask_start_time", "audio_mask_end_time",
    )
    required = node_cls.INPUT_TYPES()["required"]
    assert "project" in required
    assert "edit_video" in required and "edit_audio" in required
    assert required["edit_video"][1]["default"] is True
    assert required["edit_audio"][1]["default"] is True
