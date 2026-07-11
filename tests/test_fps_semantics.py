"""Focused regression coverage for scene-time FPS semantics."""

import asyncio
import importlib
import json
import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
import server.routes as routes
from server.timeline_renderer import iter_scene_frames
from server.timeline_state import (
    Asset,
    AudioTrack,
    ClipReference,
    GenerationJob,
    GuideFrame,
    PromptSection,
    Scene,
    TimelineProject,
    effective_scene_fps,
    media_timeline_frames,
    retime_scene_geometry,
)


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


def _payload(response):
    return json.loads(response.body.decode("utf-8"))


def test_effective_fps_and_media_timeline_frames_use_half_up_scene_time():
    project = TimelineProject(fps=24.0)
    scene = Scene(fps=30.0)
    video = Asset(asset_type="video", fps=30.0, frame_count=690, duration_sec=23.0)
    audio = Asset(asset_type="audio", duration_sec=23.0)

    assert effective_scene_fps(project, scene) == 30.0
    assert media_timeline_frames(video, 30.0) == 690
    assert media_timeline_frames(audio, 30.0) == 690
    assert media_timeline_frames(video, 24.0) == 552
    assert media_timeline_frames(audio, 24.0) == 552
    assert media_timeline_frames(video, 8.0) == 184
    assert media_timeline_frames(Asset(asset_type="video", fps=30.0, frame_count=5), 24.0) == 4
    assert media_timeline_frames(Asset(asset_type="image", frame_count=1), 24.0) == 0


def test_retime_scene_geometry_scales_endpoints_and_preserves_special_fields():
    first = ClipReference(
        clip_id="first",
        timeline_start_frame=0,
        timeline_end_frame=15,
        source_in_frame=3,
        source_out_frame=18,
        source_origin_frame=3,
        total_source_frames=30,
    )
    second = ClipReference(
        clip_id="second",
        timeline_start_frame=15,
        timeline_end_frame=30,
        source_in_frame=18,
        source_out_frame=33,
        source_origin_frame=18,
        total_source_frames=15,
    )
    audio = AudioTrack(
        timeline_start_frame=30,
        timeline_end_frame=60,
        source_in_frame=3,
        source_origin_frame=3,
        total_source_frames=30,
    )
    guides = [
        GuideFrame(guide_id="early", frame_index=1),
        GuideFrame(guide_id="later", frame_index=2),
        GuideFrame(guide_id="last", frame_index=-1),
    ]
    sections = [
        PromptSection(start_frame=0, end_frame=1, prompt="a"),
        PromptSection(start_frame=1, end_frame=2, prompt="b"),
    ]
    selections = [{
        "name": "range",
        "start": 15,
        "end": 45,
        "pre_context_frames": 8,
        "post_context_frames": 16,
        "mask_pre_offset": 4,
        "mask_post_offset": 12,
    }]
    scene = Scene(
        duration_frames=90,
        clips=[first, second],
        audio_tracks=[audio],
        guide_frames=guides,
        prompt_sections=sections,
        saved_selections=selections,
    )

    retime_scene_geometry(scene, 30.0, 24.0)

    assert scene.duration_frames == 72
    assert first.timeline_end_frame == second.timeline_start_frame == 12
    assert first.total_source_frames == 24  # round((3+30)*.8) - round(3*.8)
    assert first.source_origin_frame == 2
    assert audio.timeline_start_frame == 24
    assert [guide.guide_id for guide in guides] == ["early", "later", "last"]
    assert guides[2].frame_index == -1
    assert guides[0].frame_index != guides[1].frame_index
    assert sections[0].end_frame <= sections[1].start_frame
    assert all(section.end_frame > section.start_frame for section in sections)
    assert selections[0]["start"] == 12
    assert selections[0]["end"] == 36
    assert selections[0]["pre_context_frames"] == 8
    assert selections[0]["mask_post_offset"] == 12


def test_retime_preserves_zero_duration_sentinel():
    scene = Scene(duration_frames=0)
    retime_scene_geometry(scene, 24.0, 48.0)
    assert scene.duration_frames == 0


@pytest.mark.parametrize("scene_fps, expected", [(30.0, 690), (24.0, 552), (8.0, 184)])
def test_add_clip_dual_drop_and_audio_route_match_effective_scene_fps(tmp_path, monkeypatch, scene_fps, expected):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", fps=scene_fps, duration_frames=1000)
    video = Asset(
        asset_id="video",
        asset_type="video",
        path="media/video.mp4",
        fps=30.0,
        frame_count=690,
        duration_sec=23.0,
        has_audio=True,
    )
    derived_audio = Asset(
        asset_id="video-audio",
        asset_type="audio",
        path="media/video_audio.wav",
        duration_sec=23.0,
    )
    project = TimelineProject(project_dir=str(tmp_path), fps=24.0, scenes=[scene], assets=[video, derived_audio])
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda _request: project)
    monkeypatch.setattr(route_module, "_prepare_video_audio_asset", lambda *_args: derived_audio)
    monkeypatch.setattr(route_module, "save_project", lambda _project: None)

    handler = _route_handler(route_module, "POST", "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips")
    response = asyncio.run(handler(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"asset_id": "video", "timeline_start_frame": 10, "dual_drop": True},
    )))
    payload = _payload(response)

    assert response.status == 201
    assert payload["timeline_end_frame"] - payload["timeline_start_frame"] == expected
    assert payload["source_out_frame"] == expected
    assert payload["total_source_frames"] == expected
    assert payload["audio_track"]["timeline_end_frame"] - payload["audio_track"]["timeline_start_frame"] == expected
    assert payload["audio_track"]["total_source_frames"] == expected


def test_add_audio_track_route_uses_scene_override(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", fps=30.0, duration_frames=1000)
    audio = Asset(asset_id="audio", asset_type="audio", path="media/audio.wav", duration_sec=23.0)
    project = TimelineProject(project_dir=str(tmp_path), fps=24.0, scenes=[scene], assets=[audio])
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda _request: project)
    monkeypatch.setattr(route_module, "save_project", lambda _project: None)
    handler = _route_handler(route_module, "POST", "/sonder-editor/project/{project_id}/scenes/{scene_id}/audio_tracks")

    response = asyncio.run(handler(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"asset_id": "audio", "timeline_start_frame": 5, "lane_index": 0},
    )))
    payload = _payload(response)
    assert response.status == 201
    assert payload["timeline_end_frame"] - payload["timeline_start_frame"] == 690
    assert payload["total_source_frames"] == 690


def test_media_repair_rebuilds_clip_and_audio_lengths_in_owning_scene_units(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    video_path = media_dir / "video.mp4"
    audio_path = media_dir / "audio.wav"
    video_path.write_bytes(b"video")
    audio_path.write_bytes(b"audio")
    video = Asset(asset_id="video", asset_type="video", path="media/video.mp4")
    audio = Asset(asset_id="audio", asset_type="audio", path="media/audio.wav")
    scene = Scene(scene_id="scene", fps=8.0, duration_frames=300)
    scene.clips = [ClipReference(source_path=video.path, timeline_start_frame=10, timeline_end_frame=10)]
    scene.audio_tracks = [AudioTrack(source_path=audio.path, timeline_start_frame=20, timeline_end_frame=21)]
    project = TimelineProject(project_dir=str(project_dir), fps=24.0, scenes=[scene], assets=[video, audio])
    snapshot = {
        "media/video.mp4": {"path": str(video_path), "signature": "5:1"},
        "media/audio.wav": {"path": str(audio_path), "signature": "5:1"},
    }
    monkeypatch.setattr(route_module, "_project_media_snapshot", lambda _project: snapshot)
    monkeypatch.setattr(route_module, "_get_audio_duration", lambda _path: 23.0)
    monkeypatch.setattr(route_module, "_asset_thumbnail_path", lambda *_args: "")
    monkeypatch.setattr(route_module, "_extract_asset_media_metadata", lambda *_args, **_kwargs: {
        "width": 1920,
        "height": 1080,
        "frame_count": 690,
        "fps": 30.0,
        "duration_sec": 23.0,
        "sample_rate": 0,
        "has_audio": False,
        "color_space": "",
        "color_transfer": "",
        "color_primaries": "",
        "color_range": "",
        "color_probed": False,
    })

    assert route_module._sync_media_folder(project, purge_trashed=False) is True
    assert scene.clips[0].timeline_end_frame - scene.clips[0].timeline_start_frame == 184
    assert scene.clips[0].total_source_frames == 184
    assert scene.audio_tracks[0].timeline_end_frame - scene.audio_tracks[0].timeline_start_frame == 184
    assert scene.audio_tracks[0].total_source_frames == 184


def test_replace_sources_use_target_scene_rate():
    scene = Scene(scene_id="scene", fps=24.0)
    clip = ClipReference(clip_id="clip", timeline_start_frame=10, timeline_end_frame=700)
    track = AudioTrack(track_id="track", timeline_start_frame=20, timeline_end_frame=710)
    scene.clips = [clip]
    scene.audio_tracks = [track]
    video = Asset(asset_id="video", asset_type="video", path="media/video.mp4", fps=30.0, frame_count=690, duration_sec=23.0)
    audio = Asset(asset_id="audio", asset_type="audio", path="media/audio.wav", duration_sec=23.0)
    project = TimelineProject(fps=30.0, scenes=[scene], assets=[video, audio])

    routes._apply_replace_clip_source(project, scene, "clip", "video")
    routes._apply_replace_audio_source(project, scene, "track", "audio")

    assert clip.timeline_end_frame - clip.timeline_start_frame == 552
    assert clip.total_source_frames == 552
    assert track.timeline_end_frame - track.timeline_start_frame == 552
    assert track.total_source_frames == 552


def test_fps_change_retimes_inherit_transition_and_refuses_active_queue():
    clip = ClipReference(timeline_start_frame=0, timeline_end_frame=240, source_out_frame=240, total_source_frames=240)
    scene = Scene(scene_id="scene", fps=0.0, duration_frames=240, clips=[clip])
    project = TimelineProject(fps=24.0, scenes=[scene])

    routes._apply_scene_fields(project, scene, {"fps": 48.0})
    assert scene.fps == 48.0
    assert scene.duration_frames == 480
    assert clip.timeline_end_frame == 480

    routes._apply_scene_fields(project, scene, {"fps": 0.0})
    assert scene.fps == 0.0
    assert scene.duration_frames == 240
    assert clip.timeline_end_frame == 240
    routes._apply_scene_fields(project, scene, {"fps": 48.0})

    project.generation_queue = [GenerationJob(scene_id="scene", status="pending")]
    with pytest.raises(routes.ProjectMutationRequestError) as exc_info:
        routes._apply_scene_fields(project, scene, {"fps": 24.0})
    assert exc_info.value.status == 409
    assert exc_info.value.code == "queue_jobs_pending"
    assert scene.fps == 48.0
    assert scene.duration_frames == 480


def test_legacy_scene_put_retimes_and_returns_queue_refusal(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene", fps=30.0, duration_frames=900)
    scene.clips = [ClipReference(timeline_start_frame=300, timeline_end_frame=600, source_out_frame=300, total_source_frames=300)]
    project = TimelineProject(project_dir=str(tmp_path), fps=24.0, scenes=[scene])
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda _request: project)
    monkeypatch.setattr(route_module, "save_project", lambda _project: None)
    handler = _route_handler(route_module, "PUT", "/sonder-editor/project/{project_id}/scenes/{scene_id}")

    response = asyncio.run(handler(DummyRequest(match_info={"scene_id": "scene"}, body={"fps": 24.0})))
    payload = _payload(response)
    assert response.status == 200
    assert payload["duration_frames"] == 720
    assert payload["clips"][0]["timeline_start_frame"] == 240
    assert payload["clips"][0]["timeline_end_frame"] == 480

    project.generation_queue = [GenerationJob(scene_id="scene", status="running")]
    refused = asyncio.run(handler(DummyRequest(match_info={"scene_id": "scene"}, body={"fps": 30.0})))
    refused_payload = _payload(refused)
    assert refused.status == 409
    assert refused_payload["code"] == "queue_jobs_pending"
    assert scene.fps == 24.0
    assert scene.duration_frames == 720


def test_renderer_maps_scene_units_to_native_frames_and_ratio_one_is_identical(tmp_path):
    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    (media_dir / "clip.mp4").write_bytes(b"video")
    asset = Asset(
        asset_id="video",
        asset_type="video",
        path=os.path.join("media", "clip.mp4"),
        fps=30.0,
        frame_count=5,
        duration_sec=5 / 30,
    )
    project = TimelineProject(project_dir=str(project_dir), fps=24.0, resolution=(2, 2), assets=[asset])
    scene = Scene(scene_id="scene", fps=24.0, duration_frames=4)
    scene.clips = [ClipReference(source_path=asset.path, timeline_start_frame=0, timeline_end_frame=4)]
    positions = []

    class Capture:
        def __init__(self, _path):
            self.position = 0

        def isOpened(self):
            return True

        def set(self, _prop, value):
            self.position = int(value)
            positions.append(self.position)

        def read(self):
            return True, np.full((2, 2, 3), self.position, dtype=np.uint8)

        def release(self):
            pass

    list(iter_scene_frames(project, scene, 0, 4, video_capture_factory=Capture))
    assert positions == [0, 1, 3, 4]

    positions.clear()
    project.assets = []  # unregistered-media compatibility fallback is ratio 1.0
    list(iter_scene_frames(project, scene, 0, 4, video_capture_factory=Capture))
    assert positions == [0, 1, 2, 3]
