import hashlib
import math
import os
import threading

import numpy as np
import pytest
import torch

import server.timeline_renderer as timeline_renderer
from server import render_cache
from server.render_cache import (
    CACHE_FORMAT_VERSION,
    CACHE_PIPELINE_VERSION,
    RenderCacheError,
    block_frame_count,
    cache_store,
    delete_render_cache_entry,
    list_render_cache_entries,
    stage_block,
    discard_staged,
    validate_block_payload,
)
from server.timeline_renderer import TimelineRenderCancelled, render_scene_frames
from server.timeline_state import (
    Asset,
    AudioTrack,
    ClipReference,
    GuideFrame,
    LaneConfig,
    PromptSection,
    Scene,
    TimelineProject,
)


def _project_scene(tmp_path, *, duration=64, resolution=(1, 1)):
    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    base_path = media_dir / "base.mp4"
    base_path.write_bytes(b"base")
    project = TimelineProject(project_dir=str(project_dir), resolution=resolution, fps=24.0)
    project.assets = [
        Asset(
            asset_id="base",
            name="base.mp4",
            path=os.path.join("media", "base.mp4"),
            asset_type="video",
            fps=24.0,
            frame_count=duration,
            color_space="rgb",
            color_range="pc",
            color_probed=True,
        )
    ]
    scene = Scene(
        scene_id="scene-1",
        duration_frames=duration,
        video_lane_configs=[LaneConfig()],
    )
    scene.clips = [
        ClipReference(
            clip_id="base-clip",
            source_path=os.path.join("media", "base.mp4"),
            timeline_start_frame=0,
            timeline_end_frame=duration,
        )
    ]
    project.scenes = [scene]
    return project, scene, base_path


class _FrameCapture:
    def __init__(self, path, calls, fail_frame=None, cancel_frame=None):
        self.path = path
        self.calls = calls
        self.frame = 0
        self.fail_frame = fail_frame
        self.cancel_frame = cancel_frame
        calls.append(path)

    def isOpened(self):
        return True

    def set(self, _prop, value):
        self.frame = int(value)
        return True

    def read(self):
        if self.cancel_frame is not None and self.frame == self.cancel_frame[0]:
            self.cancel_frame[1].set()
        if self.fail_frame is not None and self.frame == self.fail_frame:
            return False, None
        base = 100 if os.path.basename(self.path).startswith("take") else 0
        value = (base + self.frame) % 256
        return True, np.array([[[value, value, value]]], dtype=np.uint8)

    def release(self):
        pass


def _capture_factory(calls, *, fail_frame=None, cancel_frame=None):
    return lambda path: _FrameCapture(path, calls, fail_frame=fail_frame, cancel_frame=cancel_frame)


def _block_digests(store_path):
    result = {}
    if not os.path.isdir(store_path):
        return result
    for name in os.listdir(store_path):
        if name.startswith("block_") and name.endswith(".pt"):
            with open(os.path.join(store_path, name), "rb") as handle:
                result[name] = hashlib.sha256(handle.read()).hexdigest()
    return result


def _block_mtimes(store_path):
    return {
        name: os.stat(os.path.join(store_path, name)).st_mtime_ns
        for name in os.listdir(store_path)
        if name.startswith("block_") and name.endswith(".pt")
    }


def test_resolution_aware_block_frame_count():
    assert block_frame_count(1280, 720) == 32
    assert block_frame_count(1920, 1080) == 32
    assert block_frame_count(3840, 2160) == 10
    assert block_frame_count(20000, 20000) == 1


def test_store_identity_preserves_distinct_close_fps_values(tmp_path):
    project, scene, _path = _project_scene(tmp_path)
    first = cache_store(project, scene.scene_id, 1, 1, 24.0)
    second = cache_store(project, scene.scene_id, 1, 1, math.nextafter(24.0, math.inf))
    assert first.token != second.token


def test_cached_and_uncached_outputs_are_exact_for_all_uint8_values(tmp_path):
    project, scene, _path = _project_scene(tmp_path, duration=256)
    uncached_calls = []
    cached_calls = []
    uncached = render_scene_frames(
        project, scene, 0, 256,
        use_cache=False,
        video_capture_factory=_capture_factory(uncached_calls),
    )
    cached = render_scene_frames(
        project, scene, 0, 256,
        use_cache=True,
        video_capture_factory=_capture_factory(cached_calls),
    )
    assert torch.equal(cached, uncached)
    second_calls = []
    second = render_scene_frames(
        project, scene, 0, 256,
        use_cache=True,
        video_capture_factory=_capture_factory(second_calls),
    )
    assert torch.equal(second, uncached)
    assert second_calls == []


def test_muted_take_writes_nothing_and_visible_tail_rewrites_only_intersecting_blocks(tmp_path):
    project, scene, _path = _project_scene(tmp_path, duration=1000)
    calls = []
    render_scene_frames(project, scene, 0, 1000, video_capture_factory=_capture_factory(calls))
    assert len(calls) == 1
    store = cache_store(project, scene.scene_id, 1, 1, 24.0)
    initial = _block_digests(store.path)
    assert len(initial) == 32

    take_path = tmp_path / "project" / "media" / "take.mp4"
    take_path.write_bytes(b"take")
    project.assets.append(Asset(
        asset_id="take",
        name="take.mp4",
        path=os.path.join("media", "take.mp4"),
        asset_type="video",
        fps=24.0,
        frame_count=90,
        color_space="rgb",
        color_range="pc",
        color_probed=True,
    ))
    take = ClipReference(
        clip_id="take-clip",
        source_path=os.path.join("media", "take.mp4"),
        timeline_start_frame=910,
        timeline_end_frame=1000,
        track_index=1,
        muted=True,
    )
    scene.clips.append(take)
    scene.video_lane_configs.append(LaneConfig())

    muted_calls = []
    render_scene_frames(project, scene, 0, 1000, video_capture_factory=_capture_factory(muted_calls))
    assert muted_calls == []
    assert _block_digests(store.path) == initial

    take.source_in_frame = 17
    take.opacity = 0.25
    edited_muted_calls = []
    render_scene_frames(project, scene, 0, 1000, video_capture_factory=_capture_factory(edited_muted_calls))
    assert edited_muted_calls == []
    assert _block_digests(store.path) == initial

    take.muted = False
    visible_calls = []
    render_scene_frames(project, scene, 0, 1000, video_capture_factory=_capture_factory(visible_calls))
    changed = _block_digests(store.path)
    changed_indices = {
        int(name[len("block_"):-len(".pt")])
        for name in initial
        if initial[name] != changed[name]
    }
    assert changed_indices == {28, 29, 30, 31}


def test_non_renderer_mutations_do_not_invalidate_or_touch_blocks(tmp_path):
    project, scene, _path = _project_scene(tmp_path, duration=64)
    render_scene_frames(project, scene, 0, 64, video_capture_factory=_capture_factory([]))
    store = cache_store(project, scene.scene_id, 1, 1, 24.0)
    initial_digests = _block_digests(store.path)
    initial_mtimes = _block_mtimes(store.path)

    scene.prompt = "changed global prompt"
    scene.prompt_sections = [PromptSection(0, 64, prompt="changed section")]
    scene.guide_frames = [GuideFrame(frame_index=12, asset_id="base", strength=0.25)]
    scene.audio_tracks = [AudioTrack(source_path="media/audio.wav", timeline_end_frame=64)]
    scene.video_lane_configs.append(LaneConfig(hidden=True))
    scene.clips.extend([
        ClipReference(
            clip_id="hidden",
            source_path=os.path.join("media", "base.mp4"),
            timeline_start_frame=0,
            timeline_end_frame=64,
            track_index=1,
        ),
        ClipReference(
            clip_id="driver",
            source_path=os.path.join("media", "base.mp4"),
            timeline_start_frame=0,
            timeline_end_frame=64,
            role="motion_driver",
        ),
        ClipReference(
            clip_id="out-of-range",
            source_path=os.path.join("media", "base.mp4"),
            timeline_start_frame=64,
            timeline_end_frame=80,
        ),
    ])

    calls = []
    render_scene_frames(project, scene, 0, 64, video_capture_factory=_capture_factory(calls))
    assert calls == []
    assert _block_digests(store.path) == initial_digests
    assert _block_mtimes(store.path) == initial_mtimes


def test_removing_earlier_nonoverlapping_clip_does_not_rewrite_later_blocks(tmp_path):
    project, scene, _path = _project_scene(tmp_path, duration=64)
    early = ClipReference(
        clip_id="early",
        source_path=os.path.join("media", "base.mp4"),
        timeline_start_frame=0,
        timeline_end_frame=16,
        source_in_frame=8,
        track_index=1,
    )
    scene.clips.insert(0, early)
    scene.video_lane_configs.append(LaneConfig())
    render_scene_frames(project, scene, 0, 64, video_capture_factory=_capture_factory([]))
    store = cache_store(project, scene.scene_id, 1, 1, 24.0)
    initial = _block_digests(store.path)
    later_mtime = _block_mtimes(store.path)["block_00000001.pt"]

    scene.clips.remove(early)
    render_scene_frames(project, scene, 0, 64, video_capture_factory=_capture_factory([]))
    current = _block_digests(store.path)
    assert initial["block_00000000.pt"] != current["block_00000000.pt"]
    assert initial["block_00000001.pt"] == current["block_00000001.pt"]
    assert _block_mtimes(store.path)["block_00000001.pt"] == later_mtime


@pytest.mark.parametrize(
    ("clip_start", "clip_end", "expected_changed"),
    [(31, 32, {0}), (32, 33, {1})],
)
def test_half_open_block_boundaries_invalidate_only_the_intersecting_block(
    tmp_path, clip_start, clip_end, expected_changed,
):
    project, scene, _path = _project_scene(tmp_path, duration=64)
    render_scene_frames(project, scene, 0, 64, video_capture_factory=_capture_factory([]))
    store = cache_store(project, scene.scene_id, 1, 1, 24.0)
    initial = _block_digests(store.path)
    scene.clips.append(ClipReference(
        clip_id="boundary",
        source_path=os.path.join("media", "base.mp4"),
        timeline_start_frame=clip_start,
        timeline_end_frame=clip_end,
        source_in_frame=10,
        track_index=1,
    ))
    scene.video_lane_configs.append(LaneConfig())

    render_scene_frames(project, scene, 0, 64, video_capture_factory=_capture_factory([]))
    current = _block_digests(store.path)
    changed = {
        int(name[len("block_"):-len(".pt")])
        for name in initial
        if initial[name] != current[name]
    }
    assert changed == expected_changed


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fit_mode", "stretch"),
        ("crop_position", "top"),
        ("opacity", 0.5),
        ("source_in_frame", 7),
        ("track_index", -1),
    ],
)
def test_visible_render_parameter_changes_only_invalidate_overlapping_blocks(tmp_path, field, value):
    project, scene, _path = _project_scene(tmp_path, duration=64)
    overlay = ClipReference(
        clip_id="overlay",
        source_path=os.path.join("media", "base.mp4"),
        timeline_start_frame=4,
        timeline_end_frame=16,
        source_in_frame=20,
        track_index=1,
    )
    scene.clips.append(overlay)
    scene.video_lane_configs.append(LaneConfig())
    render_scene_frames(project, scene, 0, 64, video_capture_factory=_capture_factory([]))
    store = cache_store(project, scene.scene_id, 1, 1, 24.0)
    initial = _block_digests(store.path)

    setattr(overlay, field, value)
    render_scene_frames(project, scene, 0, 64, video_capture_factory=_capture_factory([]))
    current = _block_digests(store.path)
    assert initial["block_00000000.pt"] != current["block_00000000.pt"]
    assert initial["block_00000001.pt"] == current["block_00000001.pt"]


def test_full_scene_blocks_are_reused_by_unaligned_selection(tmp_path):
    project, scene, _path = _project_scene(tmp_path, duration=64)
    render_scene_frames(project, scene, 0, 64, video_capture_factory=_capture_factory([]))
    calls = []
    selected = render_scene_frames(project, scene, 17, 49, video_capture_factory=_capture_factory(calls))
    assert calls == []
    assert tuple(selected.shape) == (32, 1, 1, 3)
    expected = torch.arange(17, 49, dtype=torch.float32).reshape(32, 1, 1, 1).repeat(1, 1, 1, 3) / 255.0
    assert torch.equal(selected, expected)


def test_mixed_hit_and_miss_runs_preserve_output_order(tmp_path):
    project, scene, _path = _project_scene(tmp_path, duration=96)
    render_scene_frames(project, scene, 32, 64, video_capture_factory=_capture_factory([]))
    calls = []
    result = render_scene_frames(project, scene, 0, 96, video_capture_factory=_capture_factory(calls))
    assert len(calls) == 2
    expected = torch.arange(96, dtype=torch.float32).reshape(96, 1, 1, 1).repeat(1, 1, 1, 3) / 255.0
    assert torch.equal(result, expected)


def test_requested_range_is_clamped_to_scene_and_zero_length_keeps_fallback(tmp_path):
    project, scene, _path = _project_scene(tmp_path, duration=64)
    result = render_scene_frames(
        project, scene, -10, 100,
        use_cache=False,
        video_capture_factory=_capture_factory([]),
    )
    assert tuple(result.shape) == (64, 1, 1, 3)

    zero_project, zero_scene, _path = _project_scene(tmp_path / "zero", duration=64)
    zero = render_scene_frames(zero_project, zero_scene, 40, 40)
    assert tuple(zero.shape) == (1, 1, 1, 3)
    assert not (tmp_path / "zero" / "project" / "cache" / "renders").exists()


def test_source_revision_change_invalidates_overlapping_blocks(tmp_path):
    project, scene, source_path = _project_scene(tmp_path, duration=32)
    render_scene_frames(project, scene, 0, 32, video_capture_factory=_capture_factory([]))
    calls = []
    source_path.write_bytes(b"base-replaced")
    render_scene_frames(project, scene, 0, 32, video_capture_factory=_capture_factory(calls))
    assert calls


@pytest.mark.parametrize(
    "mutate_asset",
    [
        lambda asset: setattr(asset, "fps", 30.0),
        lambda asset: setattr(asset, "frame_count", 19),
        lambda asset: (setattr(asset, "color_space", "bt709"), setattr(asset, "color_range", "tv")),
    ],
)
def test_decode_metadata_changes_invalidate_blocks(tmp_path, mutate_asset):
    project, scene, _path = _project_scene(tmp_path, duration=32)
    render_scene_frames(project, scene, 0, 32, video_capture_factory=_capture_factory([]))
    mutate_asset(project.assets[0])
    calls = []
    render_scene_frames(project, scene, 0, 32, video_capture_factory=_capture_factory(calls))
    assert calls


def test_source_revision_change_during_render_retries_once_and_publishes_retry(tmp_path):
    project, scene, source_path = _project_scene(tmp_path, duration=8)
    calls = []
    changed = {"done": False}

    class RevisingCapture(_FrameCapture):
        def read(self):
            if self.frame == 3 and not changed["done"]:
                source_path.write_bytes(b"replacement-during-render")
                changed["done"] = True
            return super().read()

    render_scene_frames(
        project,
        scene,
        0,
        8,
        video_capture_factory=lambda path: RevisingCapture(path, calls),
    )
    assert len(calls) == 2
    repeat_calls = []
    render_scene_frames(project, scene, 0, 8, video_capture_factory=_capture_factory(repeat_calls))
    assert repeat_calls == []


def test_source_revision_change_after_hit_loading_retries_from_new_snapshot(tmp_path, monkeypatch):
    project, scene, source_path = _project_scene(tmp_path, duration=8)
    render_scene_frames(project, scene, 0, 8, video_capture_factory=_capture_factory([]))
    real_changed = timeline_renderer._source_revisions_changed
    checks = {"count": 0}

    def replace_after_hit(prepared):
        checks["count"] += 1
        if checks["count"] == 1:
            source_path.write_bytes(b"replacement-after-hit")
        return real_changed(prepared)

    monkeypatch.setattr(timeline_renderer, "_source_revisions_changed", replace_after_hit)
    calls = []
    render_scene_frames(project, scene, 0, 8, video_capture_factory=_capture_factory(calls))
    assert len(calls) == 1
    repeat_calls = []
    render_scene_frames(project, scene, 0, 8, video_capture_factory=_capture_factory(repeat_calls))
    assert repeat_calls == []


def test_partial_decode_failure_returns_fallback_without_cache_commit(tmp_path):
    project, scene, _path = _project_scene(tmp_path, duration=8)
    result = render_scene_frames(
        project, scene, 0, 8,
        video_capture_factory=_capture_factory([], fail_frame=3),
    )
    assert torch.equal(result[3], torch.zeros_like(result[3]))
    store = cache_store(project, scene.scene_id, 1, 1, 24.0)
    assert _block_digests(store.path) == {}


@pytest.mark.parametrize("failure", ["containment", "open", "seek"])
def test_source_access_failures_return_fallback_without_cache_commit(tmp_path, failure):
    project, scene, _path = _project_scene(tmp_path, duration=8)

    if failure == "containment":
        scene.clips[0].source_path = os.path.join("..", "outside.mp4")
        capture_factory = _capture_factory([])
    elif failure == "open":
        class ClosedCapture:
            def isOpened(self):
                return False

            def release(self):
                pass

        capture_factory = lambda _path: ClosedCapture()
    else:
        class SeekFailureCapture(_FrameCapture):
            def set(self, _prop, value):
                super().set(_prop, value)
                return False

        capture_factory = lambda path: SeekFailureCapture(path, [])

    result = render_scene_frames(project, scene, 0, 8, video_capture_factory=capture_factory)
    assert tuple(result.shape) == (8, 1, 1, 3)
    store = cache_store(project, scene.scene_id, 1, 1, 24.0)
    assert _block_digests(store.path) == {}


def test_late_cancellation_publishes_no_blocks_or_temps(tmp_path):
    project, scene, _path = _project_scene(tmp_path, duration=64)
    cancel_event = threading.Event()
    with pytest.raises(TimelineRenderCancelled):
        render_scene_frames(
            project, scene, 0, 64,
            cancel_event=cancel_event,
            video_capture_factory=_capture_factory([], cancel_frame=(40, cancel_event)),
        )
    root = tmp_path / "project" / "cache" / "renders"
    assert not root.exists() or list(root.iterdir()) == []


def test_late_cancellation_preserves_existing_finalized_blocks(tmp_path):
    project, scene, _path = _project_scene(tmp_path, duration=64)
    render_scene_frames(project, scene, 0, 64, video_capture_factory=_capture_factory([]))
    store = cache_store(project, scene.scene_id, 1, 1, 24.0)
    initial = _block_digests(store.path)
    scene.clips[0].source_in_frame = 5
    cancel_event = threading.Event()

    with pytest.raises(TimelineRenderCancelled):
        render_scene_frames(
            project,
            scene,
            0,
            64,
            cancel_event=cancel_event,
            video_capture_factory=_capture_factory([], cancel_frame=(40, cancel_event)),
        )
    assert _block_digests(store.path) == initial
    assert not any(name.endswith(".tmp") for name in os.listdir(store.path))


def test_any_staging_failure_suppresses_the_whole_request_publication(tmp_path, monkeypatch):
    project, scene, _path = _project_scene(tmp_path, duration=64)
    real_stage = timeline_renderer.stage_block

    def fail_second_block(*args, **kwargs):
        if kwargs.get("block_index") == 1:
            raise OSError("simulated disk failure")
        return real_stage(*args, **kwargs)

    monkeypatch.setattr(timeline_renderer, "stage_block", fail_second_block)
    result = render_scene_frames(project, scene, 0, 64, video_capture_factory=_capture_factory([]))
    assert tuple(result.shape) == (64, 1, 1, 3)
    root = tmp_path / "project" / "cache" / "renders"
    assert not root.exists() or list(root.iterdir()) == []


def test_staging_copies_storage_backed_views(tmp_path):
    project, scene, _path = _project_scene(tmp_path, duration=8, resolution=(2, 2))
    store = cache_store(project, scene.scene_id, 2, 2, 24.0)
    backing = torch.zeros((16, 2, 2, 3), dtype=torch.uint8)
    view = backing[:8]
    staged = stage_block(
        store,
        request_id="a" * 32,
        block_index=0,
        start=0,
        end=8,
        width=2,
        height=2,
        fingerprint="fingerprint",
        frames=view,
    )
    payload = torch.load(staged.temp_path, weights_only=True, map_location="cpu")
    frames = payload["frames"]
    assert frames.untyped_storage().nbytes() == frames.numel()
    assert os.path.getsize(staged.temp_path) < backing.numel() + 16 * 1024
    discard_staged([staged])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format_version", -1),
        ("fingerprint", "wrong"),
        ("end", 7),
        ("frames", torch.zeros((8, 1, 1, 3), dtype=torch.float32)),
        ("frames", torch.zeros((7, 1, 1, 3), dtype=torch.uint8)),
    ],
)
def test_malformed_payloads_are_rejected(field, value):
    payload = {
        "format_version": CACHE_FORMAT_VERSION,
        "pipeline_version": CACHE_PIPELINE_VERSION,
        "fingerprint": "expected",
        "block_index": 0,
        "start": 0,
        "end": 8,
        "width": 1,
        "height": 1,
        "frames": torch.zeros((8, 1, 1, 3), dtype=torch.uint8),
    }
    payload[field] = value
    assert validate_block_payload(
        payload,
        block_index=0,
        start=0,
        end=8,
        width=1,
        height=1,
        fingerprint="expected",
    ) is None


def test_scene_shrink_prunes_wholly_out_of_range_tail_blocks(tmp_path):
    project, scene, _path = _project_scene(tmp_path, duration=70)
    render_scene_frames(project, scene, 0, 70, video_capture_factory=_capture_factory([]))
    store = cache_store(project, scene.scene_id, 1, 1, 24.0)
    assert set(_block_digests(store.path)) == {
        "block_00000000.pt",
        "block_00000001.pt",
        "block_00000002.pt",
    }

    scene.duration_frames = 33
    scene.clips[0].timeline_end_frame = 33
    project.assets[0].frame_count = 33
    render_scene_frames(project, scene, 0, 33, video_capture_factory=_capture_factory([]))
    assert set(_block_digests(store.path)) == {
        "block_00000000.pt",
        "block_00000001.pt",
    }

    scene.duration_frames = 65
    scene.clips[0].timeline_end_frame = 65
    project.assets[0].frame_count = 65
    render_scene_frames(project, scene, 0, 65, video_capture_factory=_capture_factory([]))
    assert set(_block_digests(store.path)) == {
        "block_00000000.pt",
        "block_00000001.pt",
        "block_00000002.pt",
    }


@pytest.mark.parametrize("reparse_name", ["cache", "renders"])
def test_cache_root_reparse_ancestors_are_rejected(tmp_path, monkeypatch, reparse_name):
    project, scene, _path = _project_scene(tmp_path)
    cache_dir = tmp_path / "project" / "cache"
    renders_dir = cache_dir / "renders"
    renders_dir.mkdir(parents=True)
    real_detector = render_cache.external_links.is_reparse_child

    def flagged_child(parent, name):
        if name == reparse_name:
            return True
        return real_detector(parent, name)

    monkeypatch.setattr(render_cache.external_links, "is_reparse_child", flagged_child)
    with pytest.raises(RenderCacheError):
        cache_store(project, scene.scene_id, 1, 1, 24.0)


def test_store_listing_and_flat_safe_deletion(tmp_path):
    project, scene, _path = _project_scene(tmp_path, duration=8)
    render_scene_frames(project, scene, 0, 8, video_capture_factory=_capture_factory([]))
    store = cache_store(project, scene.scene_id, 1, 1, 24.0)
    entries = list_render_cache_entries(project)
    assert [entry["filename"] for entry in entries] == [store.token]
    assert entries[0]["size_bytes"] > 0

    unknown = os.path.join(store.path, "do-not-delete.txt")
    with open(unknown, "wb") as handle:
        handle.write(b"user")
    with pytest.raises(RenderCacheError):
        delete_render_cache_entry(project, store.token)
    assert os.path.isfile(unknown)
    os.remove(unknown)
    assert delete_render_cache_entry(project, store.token)["deleted"] is True
    assert not os.path.exists(store.path)


def test_cache_outcome_diagnostics_cover_disabled_ready_and_fallbacks(tmp_path, monkeypatch, caplog):
    project, scene, _path = _project_scene(tmp_path, duration=40)
    caplog.set_level("INFO", logger="sonder_editor")

    render_scene_frames(
        project, scene, 0, 40,
        use_cache=False,
        video_capture_factory=_capture_factory([]),
    )
    assert "Render cache outcome=disabled" in caplog.text

    caplog.clear()
    render_scene_frames(
        project, scene, 0, 40,
        use_cache=True,
        video_capture_factory=_capture_factory([]),
    )
    assert "Render cache outcome=ready" in caplog.text
    assert "block_frames=32" in caplog.text
    assert "requested_blocks=[0, 1]" in caplog.text
    assert "hit_blocks=[]" in caplog.text
    assert "miss_blocks=[0, 1]" in caplog.text
    assert "staged=2 published=2" in caplog.text

    caplog.clear()
    render_scene_frames(
        project, scene, 0, 40,
        use_cache=True,
        video_capture_factory=_capture_factory([]),
    )
    assert "hit_blocks=[0, 1]" in caplog.text
    assert "miss_blocks=[]" in caplog.text
    assert "staged=0 published=0" in caplog.text

    real_cache_store = timeline_renderer.cache_store
    caplog.clear()
    monkeypatch.setattr(
        timeline_renderer,
        "cache_store",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RenderCacheError("blocked root")),
    )
    render_scene_frames(
        project, scene, 0, 1,
        use_cache=True,
        video_capture_factory=_capture_factory([]),
    )
    assert "outcome=store_unavailable" in caplog.text
    assert "reason=blocked root" in caplog.text

    monkeypatch.setattr(timeline_renderer, "cache_store", real_cache_store)
    caplog.clear()
    monkeypatch.setattr(
        timeline_renderer,
        "prepare_store",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RenderCacheError("prepare denied")),
    )
    render_scene_frames(
        project, scene, 0, 1,
        use_cache=True,
        video_capture_factory=_capture_factory([]),
    )
    assert "outcome=prepare_failed" in caplog.text
    assert "reason=prepare denied" in caplog.text


def test_cache_publication_failure_is_diagnostic_and_returns_output(tmp_path, monkeypatch, caplog):
    project, scene, _path = _project_scene(tmp_path, duration=8)
    caplog.set_level("INFO", logger="sonder_editor")
    monkeypatch.setattr(
        timeline_renderer,
        "publish_staged",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("publish denied")),
    )
    result = render_scene_frames(
        project, scene, 0, 8,
        use_cache=True,
        video_capture_factory=_capture_factory([]),
    )
    assert tuple(result.shape) == (8, 1, 1, 3)
    assert "outcome=publication_failed" in caplog.text
    assert "published=partial_or_unknown" in caplog.text
    assert "reason=publish denied" in caplog.text
