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

    project_dir = tmp_path / "sonder-projects" / "project"
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

    def fake_decode(_path, start, end, **_kwargs):
        for frame_no in range(start, end):
            yield np.full((2, 2, 3), frame_no, dtype=np.uint8)

    monkeypatch.setattr(bridge, "decode_video_range", fake_decode)


def test_driver_selector_and_bridge_contracts(tmp_path, monkeypatch):
    bridge = _import_driver_bridge(tmp_path, monkeypatch)

    assert bridge.SonderDriverSelector.RETURN_TYPES == ("SONDER_DRIVER_REF", "INT")
    assert bridge.SonderDriverSelector.RETURN_NAMES == ("driver_ref", "has_driver")
    assert "project" in bridge.SonderDriverSelector.INPUT_TYPES()["required"]
    assert "driver_lane_index" in bridge.SonderDriverSelector.INPUT_TYPES()["optional"]
    assert "driver_selector_overrides_json" in bridge.SonderDriverSelector.INPUT_TYPES()["optional"]

    assert bridge.SonderDriverBridge.RETURN_TYPES == ("IMAGE", "INT", "FLOAT")
    assert bridge.SonderDriverBridge.RETURN_NAMES == ("driver_images", "driver_idx", "driver_strength")
    assert "driver_ref" in bridge.SonderDriverBridge.INPUT_TYPES()["required"]
    assert "project" not in bridge.SonderDriverBridge.INPUT_TYPES()["required"]


def test_driver_bridge_emits_selected_lane_segment(tmp_path, monkeypatch):
    bridge, _timeline_state, project, _scene = _make_project(
        tmp_path,
        monkeypatch,
        lane_count=2,
        clip_lane=1,
    )
    _stub_decode(monkeypatch, bridge)

    driver_ref, has_driver = bridge.SonderDriverSelector().execute(
        project,
        driver_lane_index=1,
    )
    images, frame_idx, strength = bridge.SonderDriverBridge().execute(driver_ref)

    assert tuple(images.shape) == (3, 4, 6, 3)
    assert frame_idx == 0
    assert strength == pytest.approx(0.42)
    assert has_driver == 1
    assert float(images[0].max()) > 0.0


def test_driver_ref_carries_rate_data_and_decode_resamples_to_scene_length(tmp_path, monkeypatch):
    bridge, timeline_state, project, _scene = _make_project(
        tmp_path,
        monkeypatch,
        lane_count=1,
        clip_lane=0,
    )
    project.assets = [
        timeline_state.Asset(
            asset_id="driver-asset",
            asset_type="video",
            path=os.path.join("media", "driver.mp4"),
            fps=30.0,
            frame_count=100,
            duration_sec=100 / 30.0,
        )
    ]
    decoded_ranges = []

    def fake_decode(_path, start, end, **_kwargs):
        decoded_ranges.append((start, end))
        for frame_no in range(start, end):
            yield np.full((2, 2, 3), frame_no, dtype=np.uint8)

    import numpy as np
    monkeypatch.setattr(bridge, "decode_video_range", fake_decode)

    driver_ref, has_driver = bridge.SonderDriverSelector().execute(project, driver_lane_index=0)
    images, _frame_idx, _strength = bridge.SonderDriverBridge().execute(driver_ref)

    assert has_driver == 1
    assert driver_ref["rate_ratio"] == pytest.approx(30.0 / 24.0)
    assert driver_ref["native_frame_count"] == 100
    assert decoded_ranges == [(15, 19)]
    assert tuple(images.shape) == (3, 4, 6, 3)
    assert [float(images[index, 0, 0, 0]) for index in range(3)] == pytest.approx(
        [15 / 255.0, 16 / 255.0, 18 / 255.0]
    )


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
    selector = bridge.SonderDriverSelector()
    node = bridge.SonderDriverBridge()

    missing_ref, missing_has_driver = selector.execute(project, driver_lane_index=2)
    missing_lane = node.execute(missing_ref)
    assert missing_has_driver == 0
    assert missing_lane[2] == pytest.approx(0.0)

    inherited_ref, inherited_has_driver = selector.execute(project, driver_lane_index=0)
    assert inherited_has_driver == 0
    inherited_hidden_muted = node.execute(inherited_ref)
    assert inherited_hidden_muted[2] == pytest.approx(0.0)

    forced_ref, forced_has_driver = selector.execute(
        project,
        driver_lane_index=0,
        driver_selector_overrides_json='{"drivers":{"lane:0":{"muted":false}}}',
    )
    forced_on = node.execute(forced_ref)
    assert forced_has_driver == 1
    assert forced_on[2] == pytest.approx(0.42)

    forced_muted_ref, forced_muted_has_driver = selector.execute(
        project,
        driver_lane_index=0,
        driver_selector_overrides_json='{"drivers":{"lane:0":{"muted":true}}}',
    )
    assert forced_muted_has_driver == 0
    forced_muted = node.execute(forced_muted_ref)
    assert forced_muted[2] == pytest.approx(0.0)

    scene.clips = []
    forced_missing_ref, forced_missing_has_driver = selector.execute(
        project,
        driver_lane_index=0,
        driver_selector_overrides_json='{"drivers":{"lane:0":{"muted":false}}}',
    )
    assert forced_missing_has_driver == 0
    forced_missing_clip = node.execute(forced_missing_ref)
    assert forced_missing_clip[2] == pytest.approx(0.0)


def test_driver_bridge_negative_lane_index_is_absent(tmp_path, monkeypatch):
    bridge, _timeline_state, project, _scene = _make_project(
        tmp_path,
        monkeypatch,
        lane_count=1,
        clip_lane=0,
    )
    _stub_decode(monkeypatch, bridge)

    driver_ref, has_driver = bridge.SonderDriverSelector().execute(
        project,
        driver_lane_index=-1,
    )
    _images, _frame_idx, strength = bridge.SonderDriverBridge().execute(driver_ref)

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

    driver_ref, has_driver = bridge.SonderDriverSelector().execute(
        project,
        driver_lane_index=0,
    )
    _images, frame_idx, strength = bridge.SonderDriverBridge().execute(driver_ref)

    assert frame_idx == 1
    assert strength == pytest.approx(0.0)
    assert has_driver == 0


@pytest.mark.parametrize("queue_job_id", ["", "job-1"])
def test_driver_bridge_uses_queue_snapshot_by_ref_id(tmp_path, monkeypatch, queue_job_id):
    bridge, timeline_state, project, scene = _make_project(tmp_path, monkeypatch, lane_count=1)
    _stub_decode(monkeypatch, bridge)

    (Path(project.project_dir) / "media" / "snapshot.mp4").write_bytes(b"snapshot")
    project.assets = [
        timeline_state.Asset(
            asset_id="snapshot-asset",
            asset_type="video",
            path=os.path.join("media", "snapshot.mp4"),
            fps=30.0,
            frame_count=100,
            duration_sec=100 / 30.0,
        )
    ]
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
        scene_fps=30.0,
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

    driver_ref, has_driver = bridge.SonderDriverSelector().execute(
        project,
        driver_lane_index=0,
    )
    images, frame_idx, strength = bridge.SonderDriverBridge().execute(driver_ref)

    assert tuple(images.shape) == (4, 4, 6, 3)
    assert frame_idx == 0
    assert strength == pytest.approx(0.7)
    assert has_driver == 1
    assert driver_ref["rate_ratio"] == pytest.approx(1.0)


def test_driver_bridge_decode_failure_for_effective_clip_raises(tmp_path, monkeypatch):
    bridge, _timeline_state, project, scene = _make_project(tmp_path, monkeypatch, lane_count=1)
    scene.clips[0].source_path = os.path.join("media", "missing.mp4")

    driver_ref, has_driver = bridge.SonderDriverSelector().execute(project, driver_lane_index=0)
    assert has_driver == 1

    with pytest.raises(RuntimeError, match="Driver media not found"):
        bridge.SonderDriverBridge().execute(driver_ref)


def test_driver_bridge_rejects_forged_project_dir_outside_configured_base(tmp_path, monkeypatch):
    bridge, _timeline_state, project, _scene = _make_project(tmp_path, monkeypatch, lane_count=1)
    _stub_decode(monkeypatch, bridge)
    driver_ref, has_driver = bridge.SonderDriverSelector().execute(project, driver_lane_index=0)
    outside_project = tmp_path / "outside-project"
    outside_media = outside_project / "media"
    outside_media.mkdir(parents=True)
    (outside_media / "driver.mp4").write_bytes(b"outside")
    driver_ref["project_dir"] = str(outside_project)

    assert has_driver == 1
    with pytest.raises(RuntimeError, match="outside the configured project base"):
        bridge.SonderDriverBridge().execute(driver_ref)


def test_driver_selector_treats_quarantined_source_path_as_absent(tmp_path, monkeypatch):
    bridge, _timeline_state, project, scene = _make_project(tmp_path, monkeypatch, lane_count=1)
    scene.clips[0].source_path = os.path.join("..", "outside.mp4")

    driver_ref, has_driver = bridge.SonderDriverSelector().execute(project, driver_lane_index=0)
    _images, _frame_idx, strength = bridge.SonderDriverBridge().execute(driver_ref)

    assert has_driver == 0
    assert driver_ref["has_driver"] == 0
    assert strength == pytest.approx(0.0)


# --- Grid padding tail and short media ---
# The Editor pads its render to the template grid at the tensor tail, after post-context,
# holding its last frame. A Driver reaching the end of the real window carries the same
# tail, so frame i of the Driver lines up with frame i of the render.


def _window(project, start, end, frame_count, padding):
    project._execution_context.update({
        "context_start": start,
        "context_end": end,
        "frame_count": frame_count,
        "frame_count_padding": padding,
    })


def _place_clip(scene, start, end, source_in=0):
    clip = scene.clips[0]
    clip.timeline_start_frame = start
    clip.timeline_end_frame = end
    clip.source_in_frame = source_in


def _values(images):
    return [round(float(images[index, 0, 0, 0]) * 255.0) for index in range(images.shape[0])]


def _run(bridge, project):
    driver_ref, has_driver = bridge.SonderDriverSelector().execute(project, driver_lane_index=0)
    images, frame_idx, _strength = bridge.SonderDriverBridge().execute(driver_ref)
    return driver_ref, has_driver, images, frame_idx


@pytest.mark.parametrize(
    ("real", "frame_count", "padding"),
    [(10, 17, 7), (6, 22, 16)],  # LTX 8n+1 and H3 17n+5
)
def test_driver_reaching_the_window_end_holds_its_last_frame_over_the_padding(
    tmp_path, monkeypatch, real, frame_count, padding,
):
    bridge, _timeline_state, project, scene = _make_project(tmp_path, monkeypatch, lane_count=1)
    _stub_decode(monkeypatch, bridge)
    _window(project, 0, real, frame_count, padding)
    _place_clip(scene, 0, real + 5)

    driver_ref, has_driver, images, frame_idx = _run(bridge, project)

    assert has_driver == 1
    assert driver_ref["overlap_len"] == real
    assert driver_ref["overlap_tail_len"] == padding
    assert frame_idx == 0
    assert images.shape[0] == frame_count
    assert _values(images) == list(range(real)) + [real - 1] * padding


def test_driver_ending_before_the_window_end_keeps_its_own_span(tmp_path, monkeypatch):
    bridge, _timeline_state, project, scene = _make_project(tmp_path, monkeypatch, lane_count=1)
    _stub_decode(monkeypatch, bridge)
    _window(project, 0, 10, 17, 7)
    _place_clip(scene, 0, 6)

    driver_ref, _has_driver, images, frame_idx = _run(bridge, project)

    assert driver_ref["overlap_tail_len"] == 0
    assert (frame_idx, images.shape[0]) == (0, 6)
    assert _values(images) == list(range(6))


def test_late_driver_reaching_the_window_end_fills_to_frame_count(tmp_path, monkeypatch):
    bridge, _timeline_state, project, scene = _make_project(tmp_path, monkeypatch, lane_count=1)
    _stub_decode(monkeypatch, bridge)
    _window(project, 0, 10, 17, 7)
    _place_clip(scene, 4, 10, source_in=20)

    _driver_ref, _has_driver, images, frame_idx = _run(bridge, project)

    assert frame_idx == 4
    assert frame_idx + images.shape[0] == 17
    assert _values(images) == list(range(20, 26)) + [25] * 7


def test_single_frame_overlap_at_the_window_end_stays_single(tmp_path, monkeypatch):
    bridge, _timeline_state, project, scene = _make_project(tmp_path, monkeypatch, lane_count=1)
    _stub_decode(monkeypatch, bridge)
    _window(project, 0, 10, 17, 7)
    _place_clip(scene, 9, 14)

    driver_ref, _has_driver, images, frame_idx = _run(bridge, project)

    assert driver_ref["overlap_tail_len"] == 0
    assert (frame_idx, images.shape[0]) == (9, 1)


def test_mixed_fps_driver_holds_its_last_resampled_frame(tmp_path, monkeypatch):
    bridge, timeline_state, project, scene = _make_project(tmp_path, monkeypatch, lane_count=1)
    _stub_decode(monkeypatch, bridge)
    project.assets = [
        timeline_state.Asset(
            asset_id="driver-asset",
            asset_type="video",
            path=os.path.join("media", "driver.mp4"),
            fps=30.0,
            frame_count=100,
        )
    ]
    _window(project, 0, 10, 17, 7)
    _place_clip(scene, 0, 10)

    _driver_ref, _has_driver, images, _frame_idx = _run(bridge, project)

    assert _values(images) == [0, 1, 3, 4, 5, 6, 8, 9, 10, 11] + [11] * 7


def test_queue_snapshot_driver_carries_the_padding_tail(tmp_path, monkeypatch):
    bridge, timeline_state, project, scene = _make_project(tmp_path, monkeypatch, lane_count=1)
    _stub_decode(monkeypatch, bridge)
    scene.clips = []
    snapshot_clip = timeline_state.ClipReference(
        clip_id="snapshot-driver",
        source_path=os.path.join("media", "driver.mp4"),
        timeline_start_frame=4,
        timeline_end_frame=12,
        source_in_frame=30,
        track_index=0,
        role="motion_driver",
    )
    project.generation_queue = [timeline_state.GenerationJob(
        job_id="job-1",
        scene_id="scene-1",
        selection_start=4,
        selection_end=10,
        params={"snapshot_version": 1},
        driver_clip_snapshots=[snapshot_clip.to_dict()],
        driver_lane_count=1,
        driver_lane_configs=[timeline_state.LaneConfig().to_dict()],
    )]
    project._execution_context["queue_job_ref_id"] = "job-1"
    _window(project, 4, 10, 9, 3)

    driver_ref, _has_driver, images, frame_idx = _run(bridge, project)

    assert driver_ref["source"] != "live"
    assert frame_idx == 0
    assert _values(images) == list(range(30, 36)) + [35] * 3


def test_missing_padding_key_means_no_tail_and_warns(tmp_path, monkeypatch, caplog):
    bridge, _timeline_state, project, scene = _make_project(tmp_path, monkeypatch, lane_count=1)
    _stub_decode(monkeypatch, bridge)
    project._execution_context.update({"context_start": 0, "context_end": 10, "frame_count": 17})
    _place_clip(scene, 0, 10)

    with caplog.at_level("WARNING"):
        driver_ref, _has_driver, images, _frame_idx = _run(bridge, project)

    assert driver_ref["overlap_tail_len"] == 0
    assert images.shape[0] == 10
    assert "does not match frame_count" in caplog.text


def _short_media(monkeypatch, bridge, timeline_state, project, *, recorded, real, fail_after=None):
    import numpy as np

    project.assets = [
        timeline_state.Asset(
            asset_id="driver-asset",
            asset_type="video",
            path=os.path.join("media", "driver.mp4"),
            fps=24.0,
            frame_count=recorded,
        )
    ]
    calls = []

    def fake_decode(_path, start, end, **_kwargs):
        # Mirrors decode_video_range: it stops quietly at the real end of the file,
        # and raises after the last frame when ffmpeg exits nonzero.
        calls.append((start, end))
        for frame_no in range(start, min(end, real)):
            yield np.full((2, 2, 3), frame_no, dtype=np.uint8)
        if fail_after is not None:
            raise RuntimeError(fail_after)

    monkeypatch.setattr(bridge, "decode_video_range", fake_decode)
    return calls


def test_driver_media_a_frame_short_of_its_record_holds_its_last_frame(tmp_path, monkeypatch, caplog):
    bridge, timeline_state, project, scene = _make_project(tmp_path, monkeypatch, lane_count=1)
    _short_media(monkeypatch, bridge, timeline_state, project, recorded=57, real=56)
    scene.duration_frames = 57
    _window(project, 0, 57, 57, 0)
    _place_clip(scene, 0, 57)

    with caplog.at_level("WARNING"):
        _driver_ref, _has_driver, images, _frame_idx = _run(bridge, project)

    assert images.shape[0] == 57
    assert _values(images)[-2:] == [55, 55]
    assert "decodes 56 frames but records 57" in caplog.text
    assert project.assets[0].frame_count == 57


def test_driver_overlap_starting_past_the_real_end_looks_back_for_the_last_frame(tmp_path, monkeypatch):
    bridge, timeline_state, project, scene = _make_project(tmp_path, monkeypatch, lane_count=1)
    calls = _short_media(monkeypatch, bridge, timeline_state, project, recorded=57, real=56)
    scene.duration_frames = 57
    _window(project, 56, 57, 57, 0)
    _place_clip(scene, 0, 57)

    _driver_ref, _has_driver, images, _frame_idx = _run(bridge, project)

    assert calls == [(56, 57), (54, 56)]
    assert _values(images) == [55]


def test_driver_media_far_shorter_than_recorded_still_fails(tmp_path, monkeypatch):
    bridge, timeline_state, project, scene = _make_project(tmp_path, monkeypatch, lane_count=1)
    _short_media(monkeypatch, bridge, timeline_state, project, recorded=57, real=50)
    scene.duration_frames = 57
    _window(project, 0, 57, 57, 0)
    _place_clip(scene, 0, 57)

    with pytest.raises(RuntimeError, match="decoded 50 of 57 recorded frames"):
        _run(bridge, project)


def test_driver_media_that_decodes_nothing_still_fails(tmp_path, monkeypatch):
    bridge, timeline_state, project, scene = _make_project(tmp_path, monkeypatch, lane_count=1)
    _short_media(monkeypatch, bridge, timeline_state, project, recorded=57, real=0)
    _window(project, 0, 10, 10, 0)
    _place_clip(scene, 0, 10)

    with pytest.raises(RuntimeError, match="Driver media ended before expected frame 0"):
        _run(bridge, project)


def test_driver_decoder_error_after_a_partial_read_still_fails(tmp_path, monkeypatch):
    bridge, timeline_state, project, scene = _make_project(tmp_path, monkeypatch, lane_count=1)
    _short_media(
        monkeypatch, bridge, timeline_state, project,
        recorded=57, real=56, fail_after="ffmpeg: corrupt packet",
    )
    scene.duration_frames = 57
    _window(project, 0, 57, 57, 0)
    _place_clip(scene, 0, 57)

    with pytest.raises(RuntimeError, match="corrupt packet"):
        _run(bridge, project)


def test_driver_overlap_far_past_a_truncated_end_names_where_decoding_stopped(tmp_path, monkeypatch):
    # Recorded 57, file truncated to 10, overlap from native 49: the look-back finds
    # nothing either, so the error must not claim 47 frames decoded.
    bridge, timeline_state, project, scene = _make_project(tmp_path, monkeypatch, lane_count=1)
    calls = _short_media(monkeypatch, bridge, timeline_state, project, recorded=57, real=10)
    scene.duration_frames = 57
    _window(project, 49, 57, 8, 0)
    _place_clip(scene, 0, 57)

    with pytest.raises(RuntimeError, match="nothing decodes from frame 47; 57 frames recorded"):
        _run(bridge, project)
    assert calls == [(49, 57), (47, 49)]
