"""Tests for the Sonder Driver Bridge node."""

import importlib
import os
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "video_editor_testpkg"


def _import_driver_bridge(tmp_path, monkeypatch):
    pytest.importorskip("torch")
    pytest.importorskip("cv2")

    folder_paths = types.SimpleNamespace(get_output_directory=lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "folder_paths", folder_paths)

    if TEST_PACKAGE not in sys.modules:
        pkg = types.ModuleType(TEST_PACKAGE)
        pkg.__path__ = [str(ROOT)]
        sys.modules[TEST_PACKAGE] = pkg

    importlib.invalidate_caches()
    return importlib.import_module(f"{TEST_PACKAGE}.nodes.driver_bridge")


def _timeline_state():
    return importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")


def _make_project(tmp_path, monkeypatch, *, lane_count=2, clip_lane=0, hidden=False, muted=False):
    bridge = _import_driver_bridge(tmp_path, monkeypatch)
    timeline_state = _timeline_state()

    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "driver.mp4").write_bytes(b"driver")

    scene = timeline_state.Scene(
        scene_id="scene-1",
        name="Scene 1",
        duration_frames=24,
        motion_driver_lane_count=lane_count,
    )
    scene.motion_driver_lane_configs = [
        timeline_state.LaneConfig(hidden=(hidden and index == clip_lane))
        for index in range(lane_count)
    ]
    scene.clips = [
        timeline_state.ClipReference(
            clip_id="driver-1",
            source_path=os.path.join("media", "driver.mp4"),
            timeline_start_frame=3,
            timeline_end_frame=8,
            source_in_frame=10,
            track_index=clip_lane,
            role="motion_driver",
            strength=0.42,
            muted=muted,
        ),
    ]
    project = timeline_state.TimelineProject(
        project_dir=str(project_dir),
        name="Drivers",
        fps=24.0,
        resolution=(6, 4),
        scenes=[scene],
    )
    project._execution_context = {
        "scene_id": "scene-1",
        "context_start": 5,
        "context_end": 10,
        "frame_count": 5,
        "queue_job_id": "",
        "queue_job_ref_id": "",
    }
    return bridge, timeline_state, project, scene


def _stub_decode(monkeypatch, bridge):
    import numpy as np

    def fake_decode(_path, start, end):
        for frame_no in range(start, end):
            yield np.full((2, 2, 3), frame_no, dtype=np.uint8)

    monkeypatch.setattr(bridge, "decode_video_range", fake_decode)


def test_driver_bridge_emits_selected_lane_segment(tmp_path, monkeypatch):
    bridge, _timeline_state, project, _scene = _make_project(
        tmp_path,
        monkeypatch,
        lane_count=2,
        clip_lane=1,
    )
    _stub_decode(monkeypatch, bridge)

    images, frame_idx, strength, has_driver = bridge.SonderDriverBridge().execute(
        project,
        driver_lane_index=1,
    )

    assert tuple(images.shape) == (3, 4, 6, 3)
    assert frame_idx == 0
    assert strength == pytest.approx(0.42)
    assert has_driver == 1
    assert float(images[0].max()) > 0.0


def test_driver_bridge_missing_hidden_muted_and_override_states(tmp_path, monkeypatch):
    bridge, _timeline_state, project, scene = _make_project(
        tmp_path,
        monkeypatch,
        lane_count=1,
        clip_lane=0,
        hidden=True,
        muted=True,
    )
    _stub_decode(monkeypatch, bridge)
    node = bridge.SonderDriverBridge()

    missing_lane = node.execute(project, driver_lane_index=2)
    assert missing_lane[3] == 0
    assert missing_lane[2] == pytest.approx(0.0)

    inherited_hidden_muted = node.execute(project, driver_lane_index=0)
    assert inherited_hidden_muted[3] == 0

    forced_on = node.execute(
        project,
        driver_lane_index=0,
        driver_bridge_overrides_json='{"drivers":{"lane:0":{"muted":false}}}',
    )
    assert forced_on[3] == 1
    assert forced_on[2] == pytest.approx(0.42)

    forced_muted = node.execute(
        project,
        driver_lane_index=0,
        driver_bridge_overrides_json='{"drivers":{"lane:0":{"muted":true}}}',
    )
    assert forced_muted[3] == 0

    scene.clips = []
    forced_missing_clip = node.execute(
        project,
        driver_lane_index=0,
        driver_bridge_overrides_json='{"drivers":{"lane:0":{"muted":false}}}',
    )
    assert forced_missing_clip[3] == 0


def test_driver_bridge_negative_lane_index_is_absent(tmp_path, monkeypatch):
    bridge, _timeline_state, project, _scene = _make_project(
        tmp_path,
        monkeypatch,
        lane_count=1,
        clip_lane=0,
    )
    _stub_decode(monkeypatch, bridge)

    _images, _frame_idx, strength, has_driver = bridge.SonderDriverBridge().execute(
        project,
        driver_lane_index=-1,
    )

    assert strength == pytest.approx(0.0)
    assert has_driver == 0


def test_driver_bridge_fallback_index_respects_template_and_avoids_guides(tmp_path, monkeypatch):
    bridge, timeline_state, project, scene = _make_project(tmp_path, monkeypatch, lane_count=1)
    scene.clips = []
    scene.guide_frames = [
        timeline_state.GuideFrame(frame_index=9, asset_id="guide-1", strength=1.0),
    ]
    project._execution_context.update({
        "context_start": 0,
        "context_end": 17,
        "frame_count": 17,
        "frame_constraint": {"step": 8, "offset": 1},
    })

    _images, frame_idx, strength, has_driver = bridge.SonderDriverBridge().execute(
        project,
        driver_lane_index=0,
    )

    assert frame_idx == 1
    assert strength == pytest.approx(0.0)
    assert has_driver == 0


@pytest.mark.parametrize("queue_job_id", ["", "job-1"])
def test_driver_bridge_uses_queue_snapshot_by_ref_id(tmp_path, monkeypatch, queue_job_id):
    bridge, timeline_state, project, scene = _make_project(tmp_path, monkeypatch, lane_count=1)
    _stub_decode(monkeypatch, bridge)

    (Path(project.project_dir) / "media" / "snapshot.mp4").write_bytes(b"snapshot")
    scene.clips = []
    snapshot_clip = timeline_state.ClipReference(
        clip_id="snapshot-driver",
        source_path=os.path.join("media", "snapshot.mp4"),
        timeline_start_frame=4,
        timeline_end_frame=8,
        source_in_frame=20,
        track_index=0,
        role="motion_driver",
        strength=0.7,
    )
    job = timeline_state.GenerationJob(
        job_id="job-1",
        scene_id="scene-1",
        selection_start=4,
        selection_end=8,
        params={"snapshot_version": 1},
        driver_clip_snapshots=[snapshot_clip.to_dict()],
        driver_lane_count=1,
        driver_lane_configs=[timeline_state.LaneConfig().to_dict()],
    )
    project.generation_queue = [job]
    project._execution_context.update({
        "context_start": 4,
        "context_end": 10,
        "frame_count": 6,
        "queue_job_id": queue_job_id,
        "queue_job_ref_id": "job-1",
    })

    images, frame_idx, strength, has_driver = bridge.SonderDriverBridge().execute(
        project,
        driver_lane_index=0,
    )

    assert tuple(images.shape) == (4, 4, 6, 3)
    assert frame_idx == 0
    assert strength == pytest.approx(0.7)
    assert has_driver == 1


def test_driver_bridge_decode_failure_for_effective_clip_raises(tmp_path, monkeypatch):
    bridge, _timeline_state, project, scene = _make_project(tmp_path, monkeypatch, lane_count=1)
    scene.clips[0].source_path = os.path.join("media", "missing.mp4")

    with pytest.raises(RuntimeError, match="Driver media not found"):
        bridge.SonderDriverBridge().execute(project, driver_lane_index=0)
