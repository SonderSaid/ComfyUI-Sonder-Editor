import asyncio
import importlib
import json
import os
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
import server.routes as routes
import server.timeline_export as timeline_export
from server.project_manager import load_project, save_project
from server.timeline_export import (
    TimelineExportManager,
    _output_path,
    _place_embedded_audio_take,
    _register_export_asset,
    _resolve_committed_asset_id,
)
from server.timeline_renderer import TimelineRenderCancelled, iter_scene_frames, render_scene_frames
from server.timeline_state import Asset, AudioTrack, ClipReference, GuideFrame, LaneConfig, Scene, TimelineProject, classify_asset_path


class DummyRequest:
    def __init__(self, *, match_info=None, query=None, body=None):
        self.match_info = match_info or {}
        self.query = query or {}
        self._body = body

    async def json(self):
        return self._body


def _load_route_module(monkeypatch):
    fake_prompt_server = SimpleNamespace(instance=SimpleNamespace(routes=web.RouteTableDef()))
    monkeypatch.setattr(server, "PromptServer", fake_prompt_server, raising=False)
    return importlib.reload(routes)


def _route_handler(route_module, method, path):
    for route in route_module.routes:
        if route.method == method and route.path == path:
            return route.handler
    raise AssertionError(f"Route not found: {method} {path}")


def _response_json(response):
    return json.loads(response.body.decode("utf-8"))


def test_m4a_classifies_as_audio():
    assert classify_asset_path("media/export.m4a") == ("audio", "")


def test_timeline_export_resolves_committed_asset_id_remap():
    project = TimelineProject()
    setattr(project, "_asset_id_remap", {"produced-id": "committed-id"})

    assert _resolve_committed_asset_id(project, "produced-id") == "committed-id"
    assert _resolve_committed_asset_id(project, "other-id") == "other-id"


def test_timeline_export_take_fit_defaults_fall_back_to_fixed_constants():
    project = TimelineProject()
    scene = Scene(scene_id="scene-1", name="Scene")
    asset = Asset(path="media/take.mp4", frame_count=4)

    clip = timeline_export._place_video_take(
        project,
        scene,
        asset,
        0,
        4,
        fit_mode="not-a-fit-mode",
        crop_position="not-a-crop-position",
    )

    assert clip.fit_mode == "pad_edge"
    assert clip.crop_position == "center"


def test_timeline_export_output_rejects_symlinked_media_root(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    external_dir = tmp_path / "external-media"
    external_dir.mkdir()
    try:
        (project_dir / "media").symlink_to(external_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available in this environment")
    scene = Scene(scene_id="scene-1", name="Scene")

    with pytest.raises(ValueError):
        _output_path(str(project_dir), scene, "export", ".mp4", "")


def test_timeline_export_embedded_audio_skips_hostile_asset_id(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    (media_dir / "clip.mp4").write_bytes(b"video")
    project = TimelineProject(project_dir=str(project_dir), fps=24.0)
    scene = Scene(scene_id="scene-1", name="Scene")
    video_asset = Asset(
        asset_id=os.path.join("..", "..", "outside"),
        asset_type="video",
        path=os.path.join("media", "clip.mp4"),
        has_audio=True,
        fps=24.0,
    )
    project.assets = [video_asset]
    monkeypatch.setattr(timeline_export, "run_ffmpeg_command", lambda *_args, **_kwargs: pytest.fail("ffmpeg should be skipped"))

    assert _place_embedded_audio_take(project, scene, video_asset, 0, 24, "", {}, cleanup_paths=[]) is None


def test_register_export_asset_skips_symlinked_thumbnail_cache(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    output_path = media_dir / "export.mp4"
    output_path.write_bytes(b"video")
    cache_dir = project_dir / "cache"
    cache_dir.mkdir()
    external_dir = tmp_path / "external-thumbnails"
    external_dir.mkdir()
    try:
        (cache_dir / "thumbnails").symlink_to(external_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available in this environment")
    project = TimelineProject(project_dir=str(project_dir))
    monkeypatch.setattr(timeline_export, "ensure_thumbnail", lambda *_args, **_kwargs: pytest.fail("thumbnail generation should be skipped"))

    asset = _register_export_asset(
        project,
        str(output_path),
        asset_type="video",
        folder="",
        technical_metadata={},
        generation_params={},
    )

    assert asset.path == "media/export.mp4"


def test_render_scene_frames_cancel_avoids_cache_commit(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    (project_dir / "media" / "clip.mp4").write_bytes(b"video")
    project = TimelineProject(project_dir=str(project_dir), resolution=(2, 2))
    scene = Scene(scene_id="scene-1", duration_frames=1, video_lane_configs=[LaneConfig()])
    scene.clips = [
        ClipReference(
            source_path=os.path.join("media", "clip.mp4"),
            timeline_start_frame=0,
            timeline_end_frame=1,
        )
    ]
    cancel_event = threading.Event()

    class CancellingCapture:
        def __init__(self, _path):
            pass

        def isOpened(self):
            return True

        def set(self, _prop, _value):
            pass

        def read(self):
            cancel_event.set()
            return True, np.zeros((2, 2, 3), dtype=np.uint8)

        def release(self):
            pass

    with pytest.raises(TimelineRenderCancelled):
        render_scene_frames(
            project,
            scene,
            0,
            1,
            cancel_event=cancel_event,
            video_capture_factory=CancellingCapture,
        )

    cache_dir = project_dir / "cache" / "renders"
    assert not cache_dir.exists() or list(cache_dir.iterdir()) == []


def test_iter_scene_frames_close_releases_capture(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    (project_dir / "media" / "clip.mp4").write_bytes(b"video")
    project = TimelineProject(project_dir=str(project_dir), resolution=(2, 2))
    scene = Scene(scene_id="scene-1", duration_frames=2, video_lane_configs=[LaneConfig()])
    scene.clips = [
        ClipReference(
            source_path=os.path.join("media", "clip.mp4"),
            timeline_start_frame=0,
            timeline_end_frame=2,
        )
    ]
    captures = []

    class ReleasingCapture:
        def __init__(self, _path):
            self.released = False
            captures.append(self)

        def isOpened(self):
            return True

        def set(self, _prop, _value):
            pass

        def read(self):
            return True, np.zeros((2, 2, 3), dtype=np.uint8)

        def release(self):
            self.released = True

    frames = iter_scene_frames(
        project,
        scene,
        0,
        2,
        video_capture_factory=ReleasingCapture,
    )

    assert next(frames).shape == (2, 2, 3)
    frames.close()
    assert captures and captures[0].released is True


def test_audio_mix_single_track_keeps_level_and_pads_output_window(tmp_path):
    from scipy.io import wavfile
    import numpy as np
    from server.timeline_renderer import mix_scene_audio_to_wav
    rate = 48000
    wavfile.write(tmp_path / "audio.wav", rate, np.full((rate // 2, 2), .2, np.float32))
    project = TimelineProject(project_dir=str(tmp_path), fps=24)
    scene = Scene(audio_tracks=[AudioTrack(source_path="audio.wav", timeline_start_frame=i * 12,
                                          timeline_end_frame=(i+1) * 12) for i in range(1)])
    contributors = mix_scene_audio_to_wav(project, scene, 0, 48, str(tmp_path / "mix.wav"))
    actual_rate, mixed = wavfile.read(tmp_path / "mix.wav")
    assert len(contributors) == 1
    assert actual_rate == rate and len(mixed) == rate * 2
    np.testing.assert_allclose(mixed[:1 * rate // 2], .2, atol=1e-7)
    assert not np.any(mixed[1 * rate // 2:])



def test_audio_mix_sequential_tracks_keep_unscaled_level_and_pad_output_window(tmp_path):
    from scipy.io import wavfile
    import numpy as np
    from server.timeline_renderer import mix_scene_audio_to_wav
    rate = 48000
    wavfile.write(tmp_path / "audio.wav", rate, np.full((rate // 2, 2), .2, np.float32))
    project = TimelineProject(project_dir=str(tmp_path), fps=24)
    scene = Scene(audio_tracks=[AudioTrack(source_path="audio.wav", timeline_start_frame=i * 12,
                                          timeline_end_frame=(i+1) * 12) for i in range(2)])
    contributors = mix_scene_audio_to_wav(project, scene, 0, 48, str(tmp_path / "mix.wav"))
    actual_rate, mixed = wavfile.read(tmp_path / "mix.wav")
    assert len(contributors) == 2
    assert actual_rate == rate and len(mixed) == rate * 2
    np.testing.assert_allclose(mixed[:2 * rate // 2], .2, atol=1e-7)
    assert not np.any(mixed[2 * rate // 2:])



def test_audio_mix_real_ffmpeg_short_source_pads_to_export_duration(tmp_path):
    import shutil
    import wave

    import server.timeline_renderer as timeline_renderer
    from server.media_helpers import get_ffmpeg_path, run_ffmpeg_command

    ffmpeg = get_ffmpeg_path()
    if ffmpeg == "ffmpeg" and shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg unavailable")

    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    source = media_dir / "short.wav"
    output = media_dir / "mixed.wav"
    run_ffmpeg_command(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.5",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-c:a",
            "pcm_s16le",
            str(source),
        ],
        timeout=30,
    )

    project = TimelineProject(project_dir=str(project_dir), project_id="project-1", name="Project", fps=24)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=48)
    scene.audio_tracks = [
        AudioTrack(
            source_path=os.path.join("media", "short.wav"),
            timeline_start_frame=0,
            timeline_end_frame=48,
        )
    ]

    contributors = timeline_renderer.mix_scene_audio_to_wav(
        project,
        scene,
        0,
        48,
        str(output),
    )

    from scipy.io import wavfile
    rate, samples = wavfile.read(output)
    duration = len(samples) / rate
    assert len(contributors) == 1
    assert duration == pytest.approx(2.0, abs=1 / 44100)


def test_render_timeline_routes_return_job_payload(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    asset = Asset(asset_id="asset-1", name="export.mp4", asset_type="video", path="media/export.mp4")
    scene = Scene(scene_id="scene-1", name="Scene")
    project = TimelineProject(project_dir=str(project_dir), project_id="project-1", name="Project", scenes=[scene], assets=[asset])
    project_dir_str = str(project_dir)

    class FakeJob:
        job_id = "job-1"
        project_id = "project-1"
        project_dir = project_dir_str
        status = "completed"
        phase = "done"
        result_asset_id = "asset-1"
        result_scene_id = "scene-1"
        placed_clip = {"clip_id": "clip-1"}
        warnings = ["warn"]

        def public_status(self):
            return {"job_id": self.job_id, "status": self.status, "phase": self.phase, "warnings": self.warnings, "alerts": []}

    class FakeManager:
        def start(self, _project, _body):
            return SimpleNamespace(job_id="job-1", status="running", phase="queued")

        def get(self, _job_id):
            return FakeJob()

        def cancel(self, _job_id):
            return SimpleNamespace(public_status=lambda: {"job_id": "job-1", "status": "running", "phase": "cancelling"})

    monkeypatch.setattr(route_module, "_TIMELINE_EXPORTS", FakeManager())
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda _request, **kwargs: project)

    start = _route_handler(route_module, "POST", "/sonder-editor/project/{project_id}/render_timeline")
    status = _route_handler(route_module, "GET", "/sonder-editor/project/{project_id}/render_timeline/{job_id}")
    cancel = _route_handler(route_module, "POST", "/sonder-editor/project/{project_id}/render_timeline/{job_id}/cancel")

    start_resp = asyncio.run(start(DummyRequest(match_info={"project_id": "project-1"}, body={"scene_id": "scene-1"})))
    assert _response_json(start_resp) == {"job_id": "job-1", "status": "running", "phase": "queued"}

    status_resp = asyncio.run(status(DummyRequest(match_info={"project_id": "project-1", "job_id": "job-1"})))
    payload = _response_json(status_resp)
    assert payload["result"]["asset"]["asset_id"] == "asset-1"
    assert payload["result"]["scene"]["scene_id"] == "scene-1"
    assert payload["result"]["placed_clip"]["clip_id"] == "clip-1"
    assert payload["warnings"] == ["warn"]
    assert payload["alerts"] == []
    assert "warnings" not in payload["result"]

    cancel_resp = asyncio.run(cancel(DummyRequest(match_info={"project_id": "project-1", "job_id": "job-1"})))
    assert _response_json(cancel_resp)["phase"] == "cancelling"


def test_timeline_export_streams_without_render_scene_frames_or_cache(tmp_path, monkeypatch):
    import server.timeline_export as timeline_export

    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=4, video_lane_configs=[LaneConfig()])
    project = TimelineProject(project_dir=str(project_dir), project_id="project-1", name="Project", scenes=[scene], resolution=(2, 2))
    save_project(project)

    consumed = []

    def fail_render(*_args, **_kwargs):
        raise AssertionError("timeline export must not call render_scene_frames")

    def fake_iter(_project, _scene, start, end, **_kwargs):
        assert (start, end) == (0, 4)
        for idx in range(start, end):
            yield np.full((2, 2, 3), idx, dtype=np.uint8)

    def fake_encode(frames_iter, *, output_path, timeout, progress_callback=None, **_kwargs):
        assert timeout == 390
        for frame in frames_iter:
            consumed.append(int(frame[0, 0, 0]))
            if progress_callback:
                progress_callback(len(consumed))
        with open(output_path, "wb") as handle:
            handle.write(b"video")
        return {
            "save_preset": "Compatible MP4",
            "codec": "libx264",
            "pix_fmt": "yuv420p",
            "container": "mp4",
            "tensor_mode": "round",
            "browser_preview_compatible": True,
        }

    monkeypatch.setattr(timeline_export, "render_scene_frames", fail_render, raising=False)
    monkeypatch.setattr(timeline_export, "iter_scene_frames", fake_iter)
    monkeypatch.setattr(timeline_export, "encode_video", fake_encode)
    monkeypatch.setattr(timeline_export, "ensure_thumbnail", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(timeline_export, "_technical_video_metadata", lambda _path, fallback: dict(fallback))

    manager = TimelineExportManager(max_workers=1, ttl_seconds=60)
    job = manager.start(load_project(str(project_dir)), {
        "scene_id": "scene-1",
        "range": {"start": 0, "end": 4},
        "include_video": True,
        "include_audio": False,
        "save_preset": "Compatible MP4",
        "place_as_take": False,
    })

    job.future.result(timeout=5)
    cache_dir = project_dir / "cache" / "renders"
    assert job.status == "completed"
    assert job.frames_done == 4
    assert consumed == [0, 1, 2, 3]
    assert not cache_dir.exists() or list(cache_dir.glob("*.pt")) == []


def test_timeline_export_audio_only_skips_video_stream(tmp_path, monkeypatch):
    import server.timeline_export as timeline_export

    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    (project_dir / "media" / "audio.wav").write_bytes(b"audio")
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=24)
    scene.audio_tracks = [
        AudioTrack(
            source_path=os.path.join("media", "audio.wav"),
            timeline_start_frame=0,
            timeline_end_frame=24,
        )
    ]
    project = TimelineProject(project_dir=str(project_dir), project_id="project-1", name="Project", scenes=[scene], resolution=(2, 2))
    save_project(project)

    def fail_iter(*_args, **_kwargs):
        raise AssertionError("audio-only export must not create video frames")

    def fake_mix(_project, _scene, _start, _end, output_wav, **_kwargs):
        with open(output_wav, "wb") as handle:
            handle.write(b"mixed")
        return []

    def fake_encode_audio(_input_wav, output_path, **_kwargs):
        with open(output_path, "wb") as handle:
            handle.write(b"audio")

    monkeypatch.setattr(timeline_export, "iter_scene_frames", fail_iter)
    monkeypatch.setattr(timeline_export, "mix_scene_audio_to_wav", fake_mix)
    monkeypatch.setattr(timeline_export, "encode_audio", fake_encode_audio)
    monkeypatch.setattr(timeline_export, "ensure_thumbnail", lambda *_args, **_kwargs: True)

    manager = TimelineExportManager(max_workers=1, ttl_seconds=60)
    job = manager.start(load_project(str(project_dir)), {
        "scene_id": "scene-1",
        "range": {"start": 0, "end": 24},
        "include_video": False,
        "include_audio": True,
        "save_preset": "Compatible MP4",
    })

    job.future.result(timeout=5)
    saved = load_project(str(project_dir))
    asset = saved.get_asset(job.result_asset_id)
    assert job.status == "completed"
    assert asset.asset_type == "audio"
    assert asset.folder == "Exports"
    assert asset.path.replace("\\", "/").startswith("media/Exports/")


def test_timeline_export_registration_reloads_current_project(tmp_path, monkeypatch):
    import server.timeline_export as timeline_export

    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=4, video_lane_configs=[LaneConfig()])
    project = TimelineProject(project_dir=str(project_dir), project_id="project-1", name="Project", scenes=[scene], resolution=(2, 2))
    save_project(project)

    encode_started = threading.Event()
    continue_encode = threading.Event()

    def fake_iter(*_args, **_kwargs):
        return iter(np.zeros((4, 2, 2, 3), dtype=np.uint8))

    def fake_encode(frames_iter, *, output_path, **_kwargs):
        encode_started.set()
        assert not isinstance(frames_iter, np.ndarray)
        assert continue_encode.wait(timeout=5)
        with open(output_path, "wb") as handle:
            handle.write(b"video")
        return {
            "save_preset": "Compatible MP4",
            "codec": "libx264",
            "pix_fmt": "yuv420p",
            "container": "mp4",
            "tensor_mode": "round",
            "browser_preview_compatible": True,
        }

    monkeypatch.setattr(timeline_export, "iter_scene_frames", fake_iter)
    monkeypatch.setattr(timeline_export, "encode_video", fake_encode)
    monkeypatch.setattr(timeline_export, "ensure_thumbnail", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(timeline_export, "_technical_video_metadata", lambda _path, fallback: dict(fallback))

    manager = TimelineExportManager(max_workers=1, ttl_seconds=60)
    job = manager.start(load_project(str(project_dir)), {
        "scene_id": "scene-1",
        "range": {"start": 0, "end": 4},
        "include_video": True,
        "include_audio": False,
        "save_preset": "Compatible MP4",
        "place_as_take": True,
        "save_provenance": True,
        "take_fit_mode": "cover",
        "take_crop_position": "top",
    })

    assert encode_started.wait(timeout=5)
    current = load_project(str(project_dir))
    current.get_scene("scene-1").guide_frames.append(GuideFrame(frame_index=2, asset_id="guide-1"))
    save_project(current)
    continue_encode.set()
    job.future.result(timeout=5)

    saved = load_project(str(project_dir))
    saved_scene = saved.get_scene("scene-1")
    assert job.status == "completed"
    assert saved.get_asset(job.result_asset_id).folder == ""
    assert [guide.frame_index for guide in saved_scene.guide_frames] == [2]
    assert len(saved_scene.clips) == 1
    assert saved_scene.clips[0].timeline_start_frame == 0
    assert saved_scene.clips[0].timeline_end_frame == 4
    assert saved_scene.clips[0].fit_mode == "cover"
    assert saved_scene.clips[0].crop_position == "top"


def test_timeline_export_non_take_writes_under_media_exports(tmp_path, monkeypatch):
    import server.timeline_export as timeline_export

    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=4, video_lane_configs=[LaneConfig()])
    project = TimelineProject(project_dir=str(project_dir), project_id="project-1", name="Project", scenes=[scene], resolution=(2, 2))
    save_project(project)

    def fake_iter(*_args, **_kwargs):
        return iter(np.zeros((4, 2, 2, 3), dtype=np.uint8))

    def fake_encode(frames_iter, *, output_path, **_kwargs):
        with open(output_path, "wb") as handle:
            handle.write(b"video")
        return {
            "save_preset": "Compatible MP4",
            "codec": "libx264",
            "pix_fmt": "yuv420p",
            "container": "mp4",
            "tensor_mode": "round",
            "browser_preview_compatible": True,
        }

    monkeypatch.setattr(timeline_export, "iter_scene_frames", fake_iter)
    monkeypatch.setattr(timeline_export, "encode_video", fake_encode)
    monkeypatch.setattr(timeline_export, "ensure_thumbnail", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(timeline_export, "_technical_video_metadata", lambda _path, fallback: dict(fallback))

    manager = TimelineExportManager(max_workers=1, ttl_seconds=60)
    job = manager.start(load_project(str(project_dir)), {
        "scene_id": "scene-1",
        "range": {"start": 0, "end": 4},
        "include_video": True,
        "include_audio": False,
        "save_preset": "Compatible MP4",
        "place_as_take": False,
        "save_provenance": False,
    })

    job.future.result(timeout=5)
    saved = load_project(str(project_dir))
    asset = saved.get_asset(job.result_asset_id)
    assert job.status == "completed"
    assert asset.folder == "Exports"
    assert asset.path.replace("\\", "/").startswith("media/Exports/")
    assert os.path.isfile(project_dir / asset.path)
    from server.project_storage import hydrate_asset
    assert "editor_export" in hydrate_asset(asset).generation_params


def test_timeline_export_registration_reuses_same_path_asset(tmp_path, monkeypatch):
    import server.timeline_export as timeline_export

    project_dir = tmp_path / "project"
    output_dir = project_dir / "media" / "Exports"
    output_dir.mkdir(parents=True)
    output_path = output_dir / "export.mp4"
    output_path.write_bytes(b"video")
    existing = Asset(
        asset_id="asset-existing",
        name="export.mp4",
        asset_type="video",
        path=os.path.join("media", "Exports", "export.mp4"),
    )
    project = TimelineProject(project_dir=str(project_dir), project_id="project-1", name="Project", assets=[existing])

    monkeypatch.setattr(timeline_export, "ensure_thumbnail", lambda *_args, **_kwargs: True)

    asset = timeline_export._register_export_asset(
        project,
        str(output_path),
        asset_type="video",
        folder="Exports",
        technical_metadata={"width": 2, "height": 2, "frame_count": 4, "fps": 24.0, "duration_sec": 4 / 24, "has_audio": False},
        generation_params={"save_preset": "Compatible MP4"},
    )

    assert asset.asset_id == "asset-existing"
    assert len(project.assets) == 1
    assert project.assets[0].folder == "Exports"


def test_timeline_export_cleans_temp_audio_after_success(tmp_path, monkeypatch):
    import server.timeline_export as timeline_export

    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    (project_dir / "media" / "audio.wav").write_bytes(b"audio")
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=4, video_lane_configs=[LaneConfig()])
    scene.audio_tracks = [
        AudioTrack(
            source_path=os.path.join("media", "audio.wav"),
            timeline_start_frame=0,
            timeline_end_frame=4,
        )
    ]
    project = TimelineProject(project_dir=str(project_dir), project_id="project-1", name="Project", scenes=[scene], resolution=(2, 2))
    save_project(project)

    def fake_iter(*_args, **_kwargs):
        return iter(np.zeros((4, 2, 2, 3), dtype=np.uint8))

    def fake_mix(_project, _scene, _start, _end, output_wav, **_kwargs):
        with open(output_wav, "wb") as handle:
            handle.write(b"mixed")
        return [{"volume": 1.0, "sample_count": 8000}]

    def fake_encode(frames_iter, *, output_path, audio_path=None, **_kwargs):
        assert audio_path and os.path.isfile(audio_path)
        with open(output_path, "wb") as handle:
            handle.write(b"video")
        return {
            "save_preset": "Compatible MP4",
            "codec": "libx264",
            "pix_fmt": "yuv420p",
            "container": "mp4",
            "tensor_mode": "round",
            "browser_preview_compatible": True,
        }

    monkeypatch.setattr(timeline_export, "iter_scene_frames", fake_iter)
    monkeypatch.setattr(timeline_export, "mix_scene_audio_to_wav", fake_mix)
    monkeypatch.setattr(timeline_export, "encode_video", fake_encode)
    monkeypatch.setattr(timeline_export, "ensure_thumbnail", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(timeline_export, "_technical_video_metadata", lambda _path, fallback: dict(fallback))

    manager = TimelineExportManager(max_workers=1, ttl_seconds=60)
    job = manager.start(load_project(str(project_dir)), {
        "scene_id": "scene-1",
        "range": {"start": 0, "end": 4},
        "include_video": True,
        "include_audio": True,
        "save_preset": "Compatible MP4",
        "place_as_take": False,
        "save_provenance": True,
    })

    job.future.result(timeout=5)
    assert job.status == "completed"
    assert list((project_dir / "media").glob("_tmp_export_audio_*.wav")) == []


def test_timeline_export_take_with_audio_adds_paired_audio_track(tmp_path, monkeypatch):
    import server.timeline_export as timeline_export

    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    (project_dir / "media" / "audio.wav").write_bytes(b"audio")
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=4, video_lane_configs=[LaneConfig()])
    scene.audio_tracks = [
        AudioTrack(
            source_path=os.path.join("media", "audio.wav"),
            timeline_start_frame=0,
            timeline_end_frame=4,
        )
    ]
    project = TimelineProject(project_dir=str(project_dir), project_id="project-1", name="Project", scenes=[scene], resolution=(2, 2))
    save_project(project)

    def fake_iter(*_args, **_kwargs):
        return iter(np.zeros((4, 2, 2, 3), dtype=np.uint8))

    def fake_mix(_project, _scene, _start, _end, output_wav, **_kwargs):
        with open(output_wav, "wb") as handle:
            handle.write(b"mixed")
        return [{"volume": 1.0, "sample_count": 8000}]

    def fake_encode(frames_iter, *, output_path, audio_path=None, **_kwargs):
        assert audio_path and os.path.isfile(audio_path)
        with open(output_path, "wb") as handle:
            handle.write(b"video")
        from scipy.io import wavfile
        wavfile.write(_kwargs["audio_sidecar_path"], 48000, np.full((8000, 2), .2, np.float32))
        return {
            "audio_processing": {"sample_rate": 48000, "gain": 1.0},
            "audio_sidecar_ready": True,
            "save_preset": "Compatible MP4",
            "codec": "libx264",
            "pix_fmt": "yuv420p",
            "container": "mp4",
            "tensor_mode": "round",
            "browser_preview_compatible": True,
        }

    def fake_run_ffmpeg(cmd, **_kwargs):
        pytest.fail("Take sidecar must bypass the encoded video")

    monkeypatch.setattr(timeline_export, "iter_scene_frames", fake_iter)
    monkeypatch.setattr(timeline_export, "mix_scene_audio_to_wav", fake_mix)
    monkeypatch.setattr(timeline_export, "encode_video", fake_encode)
    monkeypatch.setattr(timeline_export, "run_ffmpeg_command", fake_run_ffmpeg)
    monkeypatch.setattr(timeline_export, "ensure_thumbnail", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(timeline_export, "_technical_video_metadata", lambda _path, fallback: dict(fallback))

    manager = TimelineExportManager(max_workers=1, ttl_seconds=60)
    job = manager.start(load_project(str(project_dir)), {
        "scene_id": "scene-1",
        "range": {"start": 0, "end": 4},
        "include_video": True,
        "include_audio": True,
        "save_preset": "Compatible MP4",
        "place_as_take": True,
        "save_provenance": True,
    })

    job.future.result(timeout=5)
    saved = load_project(str(project_dir))
    saved_scene = saved.get_scene("scene-1")
    assert job.status == "completed"
    assert len(saved_scene.clips) == 1
    video_asset = saved.get_asset(job.result_asset_id)
    assert video_asset.folder == ""
    assert os.path.dirname(video_asset.path).replace("\\", "/") == "media"
    paired_audio_path = os.path.join(os.path.dirname(video_asset.path), f"{video_asset.asset_id}_audio.wav").replace("\\", "/")
    paired_tracks = [track for track in saved_scene.audio_tracks if track.source_path == paired_audio_path]
    assert len(paired_tracks) == 1
    assert paired_tracks[0].timeline_start_frame == 0
    assert paired_tracks[0].timeline_end_frame == 4
    assert os.path.isfile(project_dir / paired_tracks[0].source_path)


@pytest.mark.parametrize('mode', ['empty', 'zero_volume', 'outside', 'muted', 'hidden', 'custom_none'])
def test_real_video_export_does_not_manufacture_audio_stream_or_take(tmp_path, monkeypatch, mode):
    from server import media_helpers as media
    project_dir = tmp_path / 'project'
    (project_dir / 'media').mkdir(parents=True)
    media.write_audio_wav(project_dir / 'media' / 'source.wav', np.full((2, 8000), .125, np.float32), 48000)
    scene = Scene(scene_id='scene', name='Scene', duration_frames=48,
                  video_lane_configs=[LaneConfig()], audio_lane_configs=[LaneConfig(hidden=mode == 'hidden')])
    if mode != 'empty':
        scene.audio_tracks = [AudioTrack(source_path='media/source.wav', timeline_start_frame=24 if mode == 'outside' else 0,
            timeline_end_frame=28 if mode == 'outside' else 4, volume=0 if mode == 'zero_volume' else 1,
            muted=mode == 'muted')]
    before = len(scene.audio_tracks)
    project = TimelineProject(project_dir=str(project_dir), project_id='project', name='Project',
                              scenes=[scene], resolution=(32, 32), fps=24)
    save_project(project)
    monkeypatch.setattr(timeline_export, '_transient_temp_dir', lambda: str(tmp_path))
    monkeypatch.setattr(timeline_export, 'iter_scene_frames', lambda *a, **k: iter(np.zeros((4, 32, 32, 3), np.uint8)))
    monkeypatch.setattr(timeline_export, 'ensure_thumbnail', lambda *a, **k: True)
    manager = TimelineExportManager(max_workers=1, ttl_seconds=60)
    body = {'scene_id': 'scene', 'range': {'start': 0, 'end': 4}, 'include_video': True,
            'include_audio': True, 'place_as_take': True, 'save_preset': 'Compatible MP4'}
    if mode == 'custom_none':
        body.update(save_preset='Custom', custom_options={'custom_audio_codec': 'none'})
    job = manager.start(project, body)
    job.future.result(timeout=30)
    assert job.status == 'completed', job.error
    saved = load_project(str(project_dir))
    asset = saved.get_asset(job.result_asset_id)
    assert not asset.has_audio
    assert 'Audio:' not in media._ffmpeg_input_text(project_dir / asset.path)
    assert len(saved.scenes[0].audio_tracks) == before
    assert len(saved.scenes[0].clips) == 1
    assert not saved.scenes[0].linked_item_groups
    assert not [a for a in saved.assets if a.asset_type == 'audio']
    assert not list(tmp_path.glob('_tmp_export_*.wav'))
    if mode != 'custom_none':
        assert any('no audible audio' in warning for warning in job.warnings)
    else:
        assert not job.warnings


def test_real_audio_only_empty_window_produces_silent_file_and_notice(tmp_path, monkeypatch):
    from server import media_helpers as media
    project_dir = tmp_path / 'project'
    (project_dir / 'media').mkdir(parents=True)
    scene = Scene(scene_id='scene', name='Scene', duration_frames=4)
    project = TimelineProject(project_dir=str(project_dir), project_id='project', name='Project', scenes=[scene], fps=24)
    save_project(project)
    monkeypatch.setattr(timeline_export, '_transient_temp_dir', lambda: str(tmp_path))
    monkeypatch.setattr(timeline_export, 'ensure_thumbnail', lambda *a, **k: True)
    manager = TimelineExportManager(max_workers=1, ttl_seconds=60)
    job = manager.start(project, {'scene_id': 'scene', 'range': {'start': 0, 'end': 4},
        'include_video': False, 'include_audio': True, 'save_preset': 'Compatible MP4'})
    job.future.result(timeout=30)
    assert job.status == 'completed', job.error
    saved = load_project(str(project_dir))
    asset = saved.get_asset(job.result_asset_id)
    samples, rate = media.decode_audio_samples(project_dir / asset.path)
    assert rate == 48000 and not np.any(samples)
    assert asset.asset_type == 'audio'
    assert not saved.scenes[0].audio_tracks
    assert any('exported audio is silent' in warning for warning in job.warnings)


def test_take_sidecar_rejects_garbage_larger_than_an_empty_header(tmp_path, monkeypatch):
    (tmp_path / 'media').mkdir()
    (tmp_path / 'media/video.mp4').write_bytes(b'video')
    source = tmp_path / 'garbage.wav'
    source.write_bytes(b'garbage' * 100)
    project = TimelineProject(project_dir=str(tmp_path))
    scene = Scene()
    asset = Asset(asset_id='video', asset_type='video', path='media/video.mp4', has_audio=True)
    cleanup = []
    monkeypatch.setattr(timeline_export, 'get_ffmpeg_path', lambda: pytest.fail('prepared sidecar does not need an extraction command'))
    assert _place_embedded_audio_take(project, scene, asset, 0, 24, '', {},
        cleanup_paths=cleanup, prepared_audio_path=str(source)) is None
    assert not project.assets and not scene.audio_tracks
    assert not (tmp_path / 'media/video_audio.wav').exists() and not cleanup


def test_failed_timeline_audio_placement_rolls_back_audio_asset_and_lane(tmp_path, monkeypatch):
    from server import media_helpers as media
    project_dir = tmp_path / 'project'
    (project_dir / 'media').mkdir(parents=True)
    media.write_audio_wav(project_dir / 'media/source.wav', np.full((2, 8000), .1, np.float32), 48000)
    scene = Scene(scene_id='scene', name='Scene', duration_frames=4,
        audio_tracks=[AudioTrack(source_path='media/source.wav', timeline_end_frame=4)],
        audio_lane_configs=[LaneConfig()], video_lane_configs=[LaneConfig()])
    project = TimelineProject(project_dir=str(project_dir), name='Project', scenes=[scene], fps=24, resolution=(32,32))
    save_project(project)
    monkeypatch.setattr(timeline_export, '_transient_temp_dir', lambda: str(tmp_path))
    monkeypatch.setattr(timeline_export, 'iter_scene_frames', lambda *a, **k: iter(np.zeros((4,32,32,3),np.uint8)))
    monkeypatch.setattr(timeline_export, 'ensure_thumbnail', lambda *a, **k: True)
    original = timeline_export.ensure_lane_index
    def failed_lane(scene, family, *args):
        original(scene, family, *args)
        if family == 'audio':
            raise RuntimeError('test failed audio placement after lane creation')
    monkeypatch.setattr(timeline_export, 'ensure_lane_index', failed_lane)
    manager = TimelineExportManager(max_workers=1, ttl_seconds=60)
    job = manager.start(project, {'scene_id':'scene','range':{'start':0,'end':4},
        'include_video':True,'include_audio':True,'place_as_take':True,'save_preset':'Compatible MP4'})
    job.future.result(timeout=30)
    assert job.status == 'completed' and job.alerts
    saved = load_project(str(project_dir))
    assert len(saved.assets) == 1 and saved.assets[0].asset_type == 'video'
    assert (project_dir / saved.assets[0].path).is_file()
    assert len(saved.scenes[0].audio_tracks) == 1
    assert len(saved.scenes[0].audio_lane_configs) == 1
    assert not list((project_dir / 'media').glob('*_audio.wav'))


def test_embedded_take_extraction_preserves_native_mono_samples(tmp_path, monkeypatch):
    from server import audio_pipeline as audio, media_helpers as media
    (tmp_path / 'media').mkdir()
    samples = np.array([[.25, -.125, 1.25, -1.375] * 1000], np.float32)
    source = tmp_path / 'source.wav'
    media.write_audio_wav(source, samples, 32000)
    video = tmp_path / 'media' / 'mono.mov'
    media.run_ffmpeg_command([media.get_ffmpeg_path(), '-y', '-f', 'lavfi', '-i', 'color=s=32x32:r=24:d=0.125',
        '-i', str(source), '-c:v', 'libx264', '-c:a', 'pcm_f32le', str(video)], timeout=30)
    monkeypatch.setattr(timeline_export, 'ensure_thumbnail', lambda *a, **k: True)
    project = TimelineProject(project_dir=str(tmp_path), fps=24)
    scene = Scene()
    asset = Asset(asset_id='mono', asset_type='video', path='media/mono.mov', has_audio=True, duration_sec=.125)
    result = _place_embedded_audio_take(project, scene, asset, 0, 3, '', {})
    assert result is not None
    track, _ = result
    with audio.mapped_float_wav(tmp_path / track.source_path) as (rate, prepared):
        assert rate == 32000
        np.testing.assert_array_equal(np.array(prepared.T, copy=True), samples)


def _real_export_project(tmp_path, monkeypatch, *, audio=False):
    from server import media_helpers as media
    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    scene = Scene(scene_id="scene", name="Scene", duration_frames=4,
                  video_lane_configs=[LaneConfig()], audio_lane_configs=[LaneConfig()])
    if audio:
        media.write_audio_wav(project_dir / "media/source.wav", np.full((2, 8000), .125, np.float32), 48000)
        scene.audio_tracks = [AudioTrack(source_path="media/source.wav", timeline_end_frame=4)]
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene],
                              resolution=(32, 32), fps=24)
    save_project(project, notify=False)
    monkeypatch.setattr(timeline_export, "_transient_temp_dir", lambda: str(tmp_path))
    return project, project_dir


def _run_real_export(project, *, video=True, audio=False, take=False):
    manager = TimelineExportManager(max_workers=1, ttl_seconds=60)
    try:
        job = manager.start(project, {"scene_id": "scene", "range": {"start": 0, "end": 4},
            "include_video": video, "include_audio": audio or not video,
            "save_preset": "Compatible MP4", "place_as_take": take, "save_provenance": False})
        job.future.result(timeout=30)
        return job
    finally:
        manager._executor.shutdown(wait=True)


def _assert_four_frame_video(path):
    import cv2
    capture = cv2.VideoCapture(str(path))
    shapes = []
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            shapes.append(frame.shape)
    finally:
        capture.release()
    assert shapes == [(32, 32, 3)] * 4


@pytest.mark.parametrize("mode", ["video", "audio", "take_av"])
def test_failed_provenance_write_must_not_destroy_a_completed_export(tmp_path, monkeypatch, mode):
    from server import project_storage as storage, media_helpers as media
    project, root = _real_export_project(tmp_path, monkeypatch, audio=mode == "take_av")
    before = (root / "project.json").read_bytes()
    published = []
    fired = []
    original = storage.publish_component

    def fail_provenance(project_dir, name, value):
        if not name.startswith("provenance_"):
            return original(project_dir, name, value)
        fired.append(name)
        outputs = [p for p in (root / "media").rglob("*") if p.is_file() and p.name != "source.wav"]
        assert len(outputs) == (2 if mode == "take_av" else 1)
        for path in outputs:
            if path.suffix == ".mp4":
                _assert_four_frame_video(path)
            else:
                samples, rate = media.decode_audio_samples(path)
                # AAC decoding may include the final codec block's padding.
                assert rate == 48000 and samples.shape[-1] >= 8000
            published.append((path, path.read_bytes()))
        raise FileNotFoundError(2, "injected provenance publication failure", "state/component.json")

    monkeypatch.setattr(storage, "publish_component", fail_provenance)
    job = _run_real_export(project, video=mode != "audio", audio=mode == "take_av", take=mode == "take_av")
    assert len(fired) == 1
    assert job.status == "failed" and job.code == "export_registration_failed"
    assert "injected provenance publication failure" in job.error
    assert (root / "project.json").read_bytes() == before
    assert not load_project(str(root)).assets
    assert job.retained_path in job.retained_paths
    assert set(job.retained_paths) == {path.relative_to(root).as_posix() for path, _ in published}
    for path, payload in published:
        assert path.read_bytes() == payload
        assert ".tmp." not in path.name
    assert job.public_status()["retained_paths"] == job.retained_paths
    assert not list(root.rglob("*.tmp*"))
    assert not list(tmp_path.glob("_tmp_export_*.wav"))


@pytest.mark.parametrize("when", ["before_publish", "after_publish", "audio_copy", "after_audio_placement"])
def test_export_cancellation_respects_publication_boundary(tmp_path, monkeypatch, when):
    project, root = _real_export_project(tmp_path, monkeypatch, audio=when in {"audio_copy", "after_audio_placement"})
    before = (root / "project.json").read_bytes()
    fired = []
    if when == "before_publish":
        original = timeline_export.encode_video
        def cancel_encode(*args, **kwargs):
            result = original(*args, **kwargs)
            fired.append(True)
            kwargs["cancel_event"].set()
            return result
        monkeypatch.setattr(timeline_export, "encode_video", cancel_encode)
    elif when == "after_publish":
        original = TimelineExportManager._check_cancel
        def cancel_published(self, job):
            if job.retained_path:
                fired.append(True)
                job.cancel_event.set()
            original(self, job)
        monkeypatch.setattr(TimelineExportManager, "_check_cancel", cancel_published)
    elif when == "audio_copy":
        original = timeline_export.copy_audio_file
        def cancel_copy(*args, **kwargs):
            fired.append(True)
            kwargs["cancel_event"].set()
            return original(*args, **kwargs)
        monkeypatch.setattr(timeline_export, "copy_audio_file", cancel_copy)
    else:
        original = timeline_export._place_embedded_audio_take
        def cancel_placed(*args, **kwargs):
            result = original(*args, **kwargs)
            assert result is not None
            fired.append(True)
            kwargs["cancel_event"].set()
            return result
        monkeypatch.setattr(timeline_export, "_place_embedded_audio_take", cancel_placed)
    with_audio = when in {"audio_copy", "after_audio_placement"}
    job = _run_real_export(project, audio=with_audio, take=True)
    assert len(fired) == 1 and job.status == "cancelled"
    assert (root / "project.json").read_bytes() == before
    saved = load_project(str(root))
    assert not saved.assets and not saved.scenes[0].clips
    outputs = [p for p in (root / "media").rglob("*") if p.is_file() and p.name != "source.wav"]
    if when == "before_publish":
        assert not outputs and not job.retained_path
        assert "retained_path" not in job.public_status()
    else:
        assert len(outputs) == (2 if when == "after_audio_placement" else 1)
        _assert_four_frame_video(root / job.retained_path)
        assert set(job.public_status()["retained_paths"]) == {p.relative_to(root).as_posix() for p in outputs}
    assert not list(root.rglob("*.tmp*"))
    assert not list(tmp_path.glob("_tmp_export_*.wav"))


def test_error_after_project_commit_preserves_referenced_export(tmp_path, monkeypatch):
    from server import project_manager as pm
    project, root = _real_export_project(tmp_path, monkeypatch)
    original = pm.atomic_replace
    fired = []
    def fail_after_replace(src, dst):
        original(src, dst)
        fired.append(dst)
        raise OSError("injected error after root replacement")
    monkeypatch.setattr(pm, "atomic_replace", fail_after_replace)
    job = _run_real_export(project)
    assert len(fired) == 1 and job.status == "failed"
    assert job.code == "export_registration_failed"
    saved = load_project(str(root))
    assert len(saved.assets) == 1
    assert saved.assets[0].path.replace("\\", "/") == job.retained_path
    _assert_four_frame_video(root / job.retained_path)
    assert "could not be confirmed" in job.error


def test_failed_audio_registration_after_validation_cleans_unplaced_wav(tmp_path, monkeypatch):
    project, root = _real_export_project(tmp_path, monkeypatch, audio=True)
    original = timeline_export._register_export_asset
    fired = []
    def fail_audio(project, output_path, **kwargs):
        if kwargs["asset_type"] == "audio":
            from server.audio_pipeline import mapped_float_wav
            with mapped_float_wav(output_path) as (_, samples):
                assert len(samples) > 0
            fired.append(output_path)
            raise OSError("injected audio registration failure after validation")
        return original(project, output_path, **kwargs)
    monkeypatch.setattr(timeline_export, "_register_export_asset", fail_audio)
    job = _run_real_export(project, audio=True, take=True)
    assert len(fired) == 1 and job.status == "completed" and job.alerts
    assert not list((root / "media").glob("*_audio.wav"))
    saved = load_project(str(root))
    assert len(saved.assets) == 1 and len(saved.scenes[0].audio_tracks) == 1
    assert "retained_path" not in job.public_status()
