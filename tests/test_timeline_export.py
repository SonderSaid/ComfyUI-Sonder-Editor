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
from server.project_manager import load_project, save_project
from server.timeline_export import TimelineExportManager
from server.timeline_renderer import TimelineRenderCancelled, render_scene_frames
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


def test_audio_mix_single_track_avoids_unsupported_amix_normalize(tmp_path, monkeypatch):
    import server.timeline_renderer as timeline_renderer

    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    audio_path = project_dir / "media" / "audio.wav"
    audio_path.write_bytes(b"audio")
    project = TimelineProject(project_dir=str(project_dir), project_id="project-1", name="Project")
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=24)
    scene.audio_tracks = [
        AudioTrack(
            source_path=os.path.join("media", "audio.wav"),
            timeline_start_frame=0,
            timeline_end_frame=24,
        )
    ]

    captured = {}

    def fake_run_ffmpeg(cmd, **_kwargs):
        captured["cmd"] = cmd

    monkeypatch.setattr(timeline_renderer, "run_ffmpeg_command", fake_run_ffmpeg)

    contributors = timeline_renderer.mix_scene_audio_to_wav(
        project,
        scene,
        0,
        24,
        str(project_dir / "media" / "mixed.wav"),
    )

    filter_complex = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
    assert len(contributors) == 1
    assert "normalize=" not in filter_complex
    assert "amix=" not in filter_complex


def test_render_timeline_routes_return_job_payload(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    asset = Asset(asset_id="asset-1", name="export.mp4", asset_type="video", path="media/export.mp4")
    scene = Scene(scene_id="scene-1", name="Scene")
    project = TimelineProject(project_dir=str(project_dir), project_id="project-1", name="Project", scenes=[scene], assets=[asset])

    class FakeJob:
        job_id = "job-1"
        status = "completed"
        phase = "done"
        result_asset_id = "asset-1"
        result_scene_id = "scene-1"
        placed_clip = {"clip_id": "clip-1"}
        warnings = ["warn"]

        def public_status(self):
            return {"job_id": self.job_id, "status": self.status, "phase": self.phase}

    class FakeManager:
        def start(self, _project, _body):
            return SimpleNamespace(job_id="job-1", status="running", phase="queued")

        def get(self, _job_id):
            return FakeJob()

        def cancel(self, _job_id):
            return SimpleNamespace(public_status=lambda: {"job_id": "job-1", "status": "running", "phase": "cancelling"})

    monkeypatch.setattr(route_module, "_TIMELINE_EXPORTS", FakeManager())
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda _request: project)

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
    assert payload["result"]["warnings"] == ["warn"]

    cancel_resp = asyncio.run(cancel(DummyRequest(match_info={"project_id": "project-1", "job_id": "job-1"})))
    assert _response_json(cancel_resp)["phase"] == "cancelling"


def test_timeline_export_registration_reloads_current_project(tmp_path, monkeypatch):
    import torch
    import server.timeline_export as timeline_export

    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=4, video_lane_configs=[LaneConfig()])
    project = TimelineProject(project_dir=str(project_dir), project_id="project-1", name="Project", scenes=[scene], resolution=(2, 2))
    save_project(project)

    encode_started = threading.Event()
    continue_encode = threading.Event()

    def fake_render(*_args, **_kwargs):
        return torch.zeros(4, 2, 2, 3, dtype=torch.float32)

    def fake_encode(frames_iter, *, output_path, **_kwargs):
        encode_started.set()
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

    monkeypatch.setattr(timeline_export, "render_scene_frames", fake_render)
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


def test_timeline_export_non_take_writes_under_media_exports(tmp_path, monkeypatch):
    import torch
    import server.timeline_export as timeline_export

    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=4, video_lane_configs=[LaneConfig()])
    project = TimelineProject(project_dir=str(project_dir), project_id="project-1", name="Project", scenes=[scene], resolution=(2, 2))
    save_project(project)

    def fake_render(*_args, **_kwargs):
        return torch.zeros(4, 2, 2, 3, dtype=torch.float32)

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

    monkeypatch.setattr(timeline_export, "render_scene_frames", fake_render)
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
    assert "editor_export" in asset.generation_params


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
    import torch
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

    def fake_render(*_args, **_kwargs):
        return torch.zeros(4, 2, 2, 3, dtype=torch.float32)

    def fake_mix(_project, _scene, _start, _end, output_wav, **_kwargs):
        with open(output_wav, "wb") as handle:
            handle.write(b"mixed")
        return []

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

    monkeypatch.setattr(timeline_export, "render_scene_frames", fake_render)
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
    import torch
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

    def fake_render(*_args, **_kwargs):
        return torch.zeros(4, 2, 2, 3, dtype=torch.float32)

    def fake_mix(_project, _scene, _start, _end, output_wav, **_kwargs):
        with open(output_wav, "wb") as handle:
            handle.write(b"mixed")
        return []

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

    def fake_run_ffmpeg(cmd, **_kwargs):
        with open(cmd[-1], "wb") as handle:
            handle.write(b"audio" * 512)

    monkeypatch.setattr(timeline_export, "render_scene_frames", fake_render)
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
