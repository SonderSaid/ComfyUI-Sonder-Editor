"""Route-level coverage for Phase 4.3 clip role fields."""

import asyncio
import importlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
import server.routes as routes
from server.timeline_state import (
    Asset,
    AudioTrack,
    BatchConfig,
    ClipReference,
    GenerationJob,
    GuideFrame,
    LaneConfig,
    PromptSection,
    Scene,
    TimelineProject,
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


def _response_json(response):
    return json.loads(response.body.decode("utf-8"))


def test_workflow_endpoint_extracts_from_png_when_cache_empty(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    workflow = {"nodes": [{"id": 1, "type": "Node"}]}
    route_module.write_png(
        str(media_dir / "source.png"),
        np.zeros((1, 1, 3), dtype=np.uint8),
        metadata={"workflow": json.dumps(workflow)},
    )
    asset = Asset(asset_id="asset-1", name="source.png", asset_type="image", path=os.path.join("media", "source.png"))
    project = TimelineProject(project_dir=str(project_dir), name="Project", assets=[asset])
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)

    handler = _route_handler(route_module, "GET", "/sonder-editor/project/{project_id}/assets/{asset_id}/workflow")
    response = asyncio.run(handler(DummyRequest(match_info={"asset_id": "asset-1"})))
    payload = _response_json(response)

    assert response.status == 200
    assert payload == {"workflow": workflow, "source": "embedded"}


def test_workflow_endpoint_returns_404_when_unavailable(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    (media_dir / "source.png").write_bytes(b"not a png")
    asset = Asset(asset_id="asset-1", name="source.png", asset_type="image", path=os.path.join("media", "source.png"))
    project = TimelineProject(project_dir=str(project_dir), name="Project", assets=[asset])
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)

    handler = _route_handler(route_module, "GET", "/sonder-editor/project/{project_id}/assets/{asset_id}/workflow")
    response = asyncio.run(handler(DummyRequest(match_info={"asset_id": "asset-1"})))
    payload = _response_json(response)

    assert response.status == 404
    assert payload["reason"] == "unavailable"


def test_workflow_endpoint_extracts_from_mp4_when_cache_empty(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    (media_dir / "source.mp4").write_bytes(b"video")
    workflow = {"nodes": [{"id": 2, "type": "VideoNode"}]}
    asset = Asset(asset_id="asset-1", name="source.mp4", asset_type="video", path=os.path.join("media", "source.mp4"))
    project = TimelineProject(project_dir=str(project_dir), name="Project", assets=[asset])
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "_extract_video_workflow_metadata", lambda path: workflow)

    handler = _route_handler(route_module, "GET", "/sonder-editor/project/{project_id}/assets/{asset_id}/workflow")
    response = asyncio.run(handler(DummyRequest(match_info={"asset_id": "asset-1"})))
    payload = _response_json(response)

    assert response.status == 200
    assert payload == {"workflow": workflow, "source": "embedded"}


def test_workflow_video_extraction_falls_back_to_ffmpeg_ffmetadata(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    workflow = {"nodes": [{"id": 3, "type": "FallbackNode"}]}
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[0] == "missing-ffprobe":
            raise FileNotFoundError("ffprobe")
        stdout = ";FFMETADATA1\nworkflow=" + json.dumps(workflow) + "\n"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(route_module, "get_ffprobe_path", lambda: "missing-ffprobe")
    monkeypatch.setattr(route_module, "get_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert route_module._extract_video_workflow_metadata("source.mp4") == workflow
    assert calls[0][0] == "missing-ffprobe"
    assert calls[1][0] == "ffmpeg"


def test_clip_post_put_role_validation_and_defaults(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(scene_id="scene-1", name="Scene")
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    project.assets = [
        Asset(asset_id="asset-1", asset_type="video", path="media/clip.mp4", frame_count=12),
        Asset(asset_id="image-1", asset_type="image", path="media/ref.png", frame_count=1),
    ]

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project, **kwargs: None)

    add_clip = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips",
    )
    update_clip = _route_handler(
        route_module,
        "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips/{clip_id}",
    )

    invalid = asyncio.run(add_clip(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"asset_id": "asset-1", "role": "bad"},
    )))
    assert invalid.status == 400

    invalid_driver_asset = asyncio.run(add_clip(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"asset_id": "image-1", "role": "motion_driver"},
    )))
    assert invalid_driver_asset.status == 400

    monkeypatch.setattr(
        route_module,
        "_extract_audio_from_video",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("motion drivers must not dual-drop audio")),
    )
    motion_driver = asyncio.run(add_clip(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={
            "asset_id": "asset-1",
            "role": "motion_driver",
            "timeline_start_frame": 1,
            "dual_drop": True,
        },
    )))
    motion_driver_json = _response_json(motion_driver)
    assert motion_driver.status == 201
    assert motion_driver_json["role"] == "motion_driver"
    assert "audio_track" not in motion_driver_json
    assert scene.audio_tracks == []

    created = asyncio.run(add_clip(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"asset_id": "asset-1", "timeline_start_frame": 3},
    )))
    created_json = _response_json(created)
    assert created_json["role"] == "render"
    assert created_json["strength"] == 1.0

    clip_id = created_json["clip_id"]
    invalid_update = asyncio.run(update_clip(DummyRequest(
        match_info={"scene_id": "scene-1", "clip_id": clip_id},
        body={"role": "bad"},
    )))
    assert invalid_update.status == 400

    updated = asyncio.run(update_clip(DummyRequest(
        match_info={"scene_id": "scene-1", "clip_id": clip_id},
        body={"role": "motion_driver", "strength": 0.42},
    )))
    updated_json = _response_json(updated)
    assert updated_json["role"] == "motion_driver"
    assert updated_json["strength"] == 0.42

    image_clip = ClipReference(clip_id="image-clip", source_path="media/ref.png")
    scene.clips.append(image_clip)
    invalid_image_update = asyncio.run(update_clip(DummyRequest(
        match_info={"scene_id": "scene-1", "clip_id": "image-clip"},
        body={"role": "motion_driver"},
    )))
    assert invalid_image_update.status == 400


def test_post_guide_to_scene_with_empty_guide_frames(tmp_path, monkeypatch):
    # #5: POST /guides must accept additions to a scene with no existing guides.
    # Regression guard for the "delete all guides → drag broken" repro shape;
    # confirms the backend POST is not gated on a non-empty guide list.
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=48)
    scene.guide_frames = []
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    project.assets = [
        Asset(asset_id="img-1", asset_type="image", path="media/ref.png"),
    ]

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

    add_guide = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/guides",
    )
    response = asyncio.run(add_guide(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"frame_index": 5, "asset_id": "img-1", "source": "asset", "strength": 0.8},
    )))

    assert response.status == 201
    assert len(scene.guide_frames) == 1
    assert scene.guide_frames[0].frame_index == 5
    assert scene.guide_frames[0].asset_id == "img-1"


def test_clip_put_accepts_same_lane_move_when_no_other_clip_overlaps(tmp_path, monkeypatch):
    # #35: single-clip move forward/backward must not be rejected when nothing collides.
    # Confirms the backend stays permissive — frontend owns overlap policy.
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    clip = ClipReference(
        clip_id="lonely",
        source_path="media/a.mp4",
        timeline_start_frame=0,
        timeline_end_frame=10,
        source_out_frame=10,
        total_source_frames=10,
        track_index=0,
    )
    scene = Scene(scene_id="scene-1", name="Scene", video_lane_count=1)
    scene.video_lane_configs = [LaneConfig(name="Lane 1")]
    scene.clips = [clip]
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

    update_clip = _route_handler(
        route_module,
        "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips/{clip_id}",
    )

    # Forward move
    forward = asyncio.run(update_clip(DummyRequest(
        match_info={"scene_id": "scene-1", "clip_id": "lonely"},
        body={"timeline_start_frame": 20, "timeline_end_frame": 30},
    )))
    assert forward.status == 200
    assert clip.timeline_start_frame == 20
    assert clip.timeline_end_frame == 30

    # Backward move
    backward = asyncio.run(update_clip(DummyRequest(
        match_info={"scene_id": "scene-1", "clip_id": "lonely"},
        body={"timeline_start_frame": 5, "timeline_end_frame": 15},
    )))
    assert backward.status == 200
    assert clip.timeline_start_frame == 5
    assert clip.timeline_end_frame == 15


def test_clip_put_round_trips_cross_lane_swap(tmp_path, monkeypatch):
    # #35: full position+lane swap commits as two PUTs. Anchor moves to target's
    # original (start, lane); target moves to anchor's original (start, lane).
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    anchor = ClipReference(
        clip_id="anchor",
        source_path="media/a.mp4",
        timeline_start_frame=0,
        timeline_end_frame=10,
        source_out_frame=10,
        total_source_frames=10,
        track_index=0,
    )
    target = ClipReference(
        clip_id="target",
        source_path="media/b.mp4",
        timeline_start_frame=20,
        timeline_end_frame=30,
        source_out_frame=10,
        total_source_frames=10,
        track_index=1,
    )
    scene = Scene(scene_id="scene-1", name="Scene", video_lane_count=2)
    scene.video_lane_configs = [LaneConfig(name="Lane 1"), LaneConfig(name="Lane 2")]
    scene.clips = [anchor, target]
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

    update_clip = _route_handler(
        route_module,
        "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips/{clip_id}",
    )

    # Frontend commits both halves of the swap as two PUTs
    anchor_resp = asyncio.run(update_clip(DummyRequest(
        match_info={"scene_id": "scene-1", "clip_id": "anchor"},
        body={"timeline_start_frame": 20, "timeline_end_frame": 30, "track_index": 1},
    )))
    target_resp = asyncio.run(update_clip(DummyRequest(
        match_info={"scene_id": "scene-1", "clip_id": "target"},
        body={"timeline_start_frame": 0, "timeline_end_frame": 10, "track_index": 0},
    )))

    assert anchor_resp.status == 200
    assert target_resp.status == 200
    assert anchor.timeline_start_frame == 20
    assert anchor.track_index == 1
    assert target.timeline_start_frame == 0
    assert target.track_index == 0


def test_clip_right_trim_extends_when_split_ceiling_allows(tmp_path, monkeypatch):
    # #9: end-trim restore. A clip with total_source_frames=20 trimmed to source_out_frame=5
    # must accept a PUT that re-extends source_out_frame back up to 20.
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    clip = ClipReference(
        clip_id="trim-restore",
        source_path="media/clip.mp4",
        timeline_start_frame=0,
        timeline_end_frame=5,
        source_in_frame=0,
        source_out_frame=5,
        total_source_frames=20,
        track_index=0,
    )
    scene = Scene(scene_id="scene-1", name="Scene", video_lane_count=1)
    scene.video_lane_configs = [LaneConfig(name="Lane 1")]
    scene.clips = [clip]
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

    update_clip = _route_handler(
        route_module,
        "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips/{clip_id}",
    )
    response = asyncio.run(update_clip(DummyRequest(
        match_info={"scene_id": "scene-1", "clip_id": "trim-restore"},
        body={"timeline_end_frame": 20, "source_out_frame": 20},
    )))
    payload = _response_json(response)

    assert response.status == 200
    assert payload["timeline_end_frame"] == 20
    assert payload["source_out_frame"] == 20
    assert clip.timeline_end_frame == 20
    assert clip.source_out_frame == 20


def test_dual_drop_skips_audio_when_video_asset_has_no_audio(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    scene = Scene(scene_id="scene-1", name="Scene")
    video_asset = Asset(
        asset_id="asset-1",
        name="silent.mp4",
        asset_type="video",
        path=os.path.join("media", "silent.mp4"),
        frame_count=12,
        has_audio=False,
    )
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    project.assets = [video_asset]

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)
    monkeypatch.setattr(
        route_module,
        "_extract_audio_from_video",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no-audio assets must not extract")),
    )

    add_clip = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips",
    )
    response = asyncio.run(add_clip(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"asset_id": "asset-1", "timeline_start_frame": 3, "dual_drop": True},
    )))
    payload = _response_json(response)

    assert response.status == 201
    assert "audio_track" not in payload
    assert len(scene.clips) == 1
    assert scene.audio_tracks == []
    assert [asset.asset_id for asset in project.assets] == ["asset-1"]


def test_clip_post_rejects_video_asset_with_invalid_duration_metadata(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(scene_id="scene-1", name="Scene")
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    project.assets = [
        Asset(
            asset_id="asset-1",
            name="bad.webm",
            asset_type="video",
            path=os.path.join("media", "bad.webm"),
            frame_count=-221360928884514624,
            duration_sec=-9223372036854776.0,
        ),
    ]
    save_calls = []

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: save_calls.append(saved_project))

    add_clip = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips",
    )
    response = asyncio.run(add_clip(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"asset_id": "asset-1", "timeline_start_frame": 3},
    )))
    payload = _response_json(response)

    assert response.status == 400
    assert "invalid duration metadata" in payload["error"]
    assert scene.clips == []
    assert save_calls == []


def test_audio_track_post_rejects_audio_asset_with_invalid_duration(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(scene_id="scene-1", name="Scene")
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    project.assets = [
        Asset(
            asset_id="asset-1",
            name="bad.mp3",
            asset_type="audio",
            path=os.path.join("media", "bad.mp3"),
            duration_sec=0.0,
            duration_checked=True,
        ),
    ]
    save_calls = []

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: save_calls.append(saved_project))

    add_audio = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/audio_tracks",
    )
    response = asyncio.run(add_audio(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"asset_id": "asset-1", "timeline_start_frame": 3},
    )))
    payload = _response_json(response)

    assert response.status == 400
    assert "invalid duration metadata" in payload["error"]
    assert scene.audio_tracks == []
    assert save_calls == []


def test_prepare_video_audio_asset_dedupes_existing_asset_with_mixed_slashes(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    audio_path = media_dir / "asset-1_audio.wav"
    audio_path.write_bytes(b"a" * 2048)
    video_asset = Asset(
        asset_id="asset-1",
        name="with-audio.mp4",
        asset_type="video",
        path=os.path.join("media", "with-audio.mp4"),
        has_audio=True,
    )
    audio_asset = Asset(
        asset_id="audio-1",
        name="with-audio.mp4 (audio)",
        asset_type="audio",
        path="media\\asset-1_audio.wav",
        duration_sec=0.0,
    )
    project = TimelineProject(project_dir=str(project_dir), name="Project")
    project.assets = [video_asset, audio_asset]

    monkeypatch.setattr(
        route_module,
        "_extract_audio_from_video",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("existing audio should be reused")),
    )
    monkeypatch.setattr(route_module, "_get_audio_duration", lambda *_args, **_kwargs: 1.5)
    monkeypatch.setattr(route_module, "ensure_thumbnail", lambda *args, **kwargs: None)

    result = route_module._prepare_video_audio_asset(project, video_asset)

    assert result is audio_asset
    assert result.duration_sec == 1.5
    assert result.duration_checked is True
    assert len([asset for asset in project.assets if asset.asset_type == "audio"]) == 1


def test_dual_drop_uses_target_audio_lane_lock_only(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    (project_dir / "media" / "with-audio.mp4").write_bytes(b"video")
    scene = Scene(
        scene_id="scene-1",
        name="Scene",
        video_lane_count=1,
        audio_lane_count=2,
    )
    scene.video_lane_configs = [LaneConfig(name="Video")]
    scene.audio_lane_configs = [LaneConfig(name="Locked", locked=True), LaneConfig(name="Target")]
    video_asset = Asset(
        asset_id="asset-1",
        name="with-audio.mp4",
        asset_type="video",
        path=os.path.join("media", "with-audio.mp4"),
        frame_count=12,
        has_audio=True,
    )
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    project.assets = [video_asset]

    def fake_extract(_video_path, output_path):
        with open(output_path, "wb") as handle:
            handle.write(b"a" * 2048)
        return True

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)
    monkeypatch.setattr(route_module, "_extract_audio_from_video", fake_extract)
    monkeypatch.setattr(route_module, "_get_audio_duration", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(route_module, "ensure_thumbnail", lambda *args, **kwargs: None)

    add_clip = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips",
    )
    response = asyncio.run(add_clip(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={
            "asset_id": "asset-1",
            "timeline_start_frame": 3,
            "dual_drop": True,
            "audio_lane_index": 1,
        },
    )))
    payload = _response_json(response)

    assert response.status == 201
    assert payload["audio_track"]["lane_index"] == 1
    assert len(scene.clips) == 1
    assert len(scene.audio_tracks) == 1
    assert scene.audio_tracks[0].lane_index == 1
    assert len(scene.linked_item_groups) == 1


def test_dual_drop_rejects_locked_target_audio_lane_before_clip_creation(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    (project_dir / "media" / "with-audio.mp4").write_bytes(b"video")
    scene = Scene(
        scene_id="scene-1",
        name="Scene",
        video_lane_count=1,
        audio_lane_count=1,
    )
    scene.video_lane_configs = [LaneConfig(name="Video")]
    scene.audio_lane_configs = [LaneConfig(name="Locked", locked=True)]
    video_asset = Asset(
        asset_id="asset-1",
        name="with-audio.mp4",
        asset_type="video",
        path=os.path.join("media", "with-audio.mp4"),
        frame_count=12,
        has_audio=True,
    )
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    project.assets = [video_asset]
    save_calls = []

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: save_calls.append(project))
    monkeypatch.setattr(
        route_module,
        "_extract_audio_from_video",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("locked lane must preflight before extraction")),
    )

    add_clip = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips",
    )
    response = asyncio.run(add_clip(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={
            "asset_id": "asset-1",
            "timeline_start_frame": 3,
            "dual_drop": True,
            "audio_lane_index": 0,
        },
    )))
    payload = _response_json(response)

    assert response.status == 409
    assert payload["code"] == "track_locked"
    assert scene.clips == []
    assert scene.audio_tracks == []
    assert save_calls == []


def test_dual_drop_rejects_partial_audio_extraction(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    (project_dir / "media" / "with-audio.mp4").write_bytes(b"video")
    scene = Scene(scene_id="scene-1", name="Scene")
    video_asset = Asset(
        asset_id="asset-1",
        name="with-audio.mp4",
        asset_type="video",
        path=os.path.join("media", "with-audio.mp4"),
        frame_count=12,
        has_audio=True,
    )
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    project.assets = [video_asset]

    def fake_extract(_video_path, output_path):
        with open(output_path, "wb") as handle:
            handle.write(b"tiny")
        return True

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)
    monkeypatch.setattr(route_module, "_extract_audio_from_video", fake_extract)
    monkeypatch.setattr(
        route_module,
        "_get_audio_duration",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("tiny WAV should not be probed")),
    )

    add_clip = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips",
    )
    response = asyncio.run(add_clip(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"asset_id": "asset-1", "timeline_start_frame": 3, "dual_drop": True},
    )))
    payload = _response_json(response)
    audio_path = project_dir / "media" / "asset-1_audio.wav"

    assert response.status == 201
    assert "audio_track" not in payload
    assert len(scene.clips) == 1
    assert scene.audio_tracks == []
    assert [asset.asset_id for asset in project.assets] == ["asset-1"]
    assert not audio_path.exists()


def test_scene_put_accepts_motion_driver_lane_config(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(scene_id="scene-1", name="Scene")
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

    update_scene = _route_handler(
        route_module,
        "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}",
    )
    response = asyncio.run(update_scene(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={
            "motion_driver_lane_count": 2,
            "motion_driver_lane_configs": [
                {"name": "Driver", "color": "#2a9b9e", "locked": True, "hidden": True},
            ],
            "guide_track_config": {"locked": True, "hidden": True},
            "prompt_track_config": {"locked": True, "hidden": False},
        },
    )))
    payload = _response_json(response)

    assert payload["motion_driver_lane_count"] == 2
    assert len(payload["motion_driver_lane_configs"]) == 2
    assert payload["motion_driver_lane_configs"][0] == {
        "name": "Driver",
        "color": "#2a9b9e",
        "locked": True,
        "hidden": True,
    }
    assert payload["motion_driver_lane_configs"][1] == {
        "name": "",
        "color": "",
        "locked": False,
        "hidden": False,
    }
    assert payload["guide_track_config"] == {
        "name": "",
        "color": "",
        "locked": True,
        "hidden": True,
    }
    assert payload["prompt_track_config"] == {
        "name": "",
        "color": "",
        "locked": True,
        "hidden": False,
    }


def test_scene_restore_accepts_guide_and_prompt_track_config(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(scene_id="scene-1", name="Scene")
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

    restore_scene = _route_handler(
        route_module,
        "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore",
    )
    response = asyncio.run(restore_scene(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={
            "name": "Restored",
            "guide_track_config": {"locked": True, "hidden": False},
            "prompt_track_config": {"locked": False, "hidden": True},
        },
    )))
    payload = _response_json(response)

    assert response.status == 200
    assert payload["name"] == "Restored"
    assert payload["guide_track_config"]["locked"] is True
    assert payload["guide_track_config"]["hidden"] is False
    assert payload["prompt_track_config"]["locked"] is False
    assert payload["prompt_track_config"]["hidden"] is True


def test_duplicate_scene_route_deep_copies_scene_and_regenerates_child_ids(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    source = Scene(
        scene_id="scene-1",
        name="Scene",
        order=3,
        duration_frames=48,
        prompt="scene prompt",
        generation_params={"seed": 123},
        batch_config=BatchConfig(max_frames=33, context_overlap=4, frame_alignment=8),
        asset_ids=["asset-1", "asset-2"],
        is_bridge=True,
        video_lane_count=2,
        motion_driver_lane_count=1,
        audio_lane_count=2,
        width=1280,
        height=720,
        fps=30.0,
    )
    source.prompt_sections = [PromptSection(start_frame=0, end_frame=24, prompt="section")]
    source.guide_frames = [GuideFrame(frame_index=8, asset_id="guide-1", source="asset", strength=0.75)]
    source.clips = [
        ClipReference(
            clip_id="clip-1",
            source_path="media/a.mp4",
            timeline_start_frame=4,
            timeline_end_frame=20,
            source_in_frame=2,
            source_out_frame=18,
            total_source_frames=32,
            source_origin_frame=2,
            opacity=0.8,
            track_index=1,
            role="render",
            strength=1.0,
            prompt="clip prompt",
            is_generated=True,
            generation_params={"cfg": 7},
            takes=["take-a"],
            active_take=0,
            take_metadata={"scene_id": "scene-1"},
        )
    ]
    source.audio_tracks = [
        AudioTrack(
            track_id="track-1",
            source_path="media/a.wav",
            timeline_start_frame=6,
            timeline_end_frame=30,
            source_in_frame=1,
            total_source_frames=48,
            source_origin_frame=1,
            volume=0.5,
            muted=True,
            lane_index=1,
        )
    ]
    source.video_lane_configs = [LaneConfig(name="V0"), LaneConfig(name="V1", hidden=True)]
    source.motion_driver_lane_configs = [LaneConfig(name="Driver", color="#ffaa00")]
    source.audio_lane_configs = [LaneConfig(name="A0"), LaneConfig(name="A1", locked=True)]
    source.guide_track_config = LaneConfig(locked=True, hidden=True)
    source.prompt_track_config = LaneConfig(locked=True, hidden=False)
    source.saved_selections = [
        {"name": "Sel", "start": 4, "end": 20, "pre_context_frames": 2, "post_context_frames": 3}
    ]
    project = TimelineProject(
        project_dir=str(project_dir),
        name="Project",
        scenes=[Scene(scene_id="scene-0", name="Other", order=1), source],
    )

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

    duplicate_scene = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/duplicate",
    )
    response = asyncio.run(duplicate_scene(DummyRequest(match_info={"scene_id": "scene-1"})))
    payload = _response_json(response)

    assert response.status == 201
    assert payload["scene_id"] != source.scene_id
    assert payload["name"] == "Scene (copy)"
    assert payload["order"] == 4
    assert len(project.scenes) == 3

    source_clip_ids = {clip.clip_id for clip in source.clips}
    duplicate_clip_ids = {clip["clip_id"] for clip in payload["clips"]}
    assert len(payload["clips"]) == len(source.clips)
    assert source_clip_ids.isdisjoint(duplicate_clip_ids)

    source_track_ids = {track.track_id for track in source.audio_tracks}
    duplicate_track_ids = {track["track_id"] for track in payload["audio_tracks"]}
    assert len(payload["audio_tracks"]) == len(source.audio_tracks)
    assert source_track_ids.isdisjoint(duplicate_track_ids)

    for key in [
        "duration_frames",
        "prompt",
        "prompt_sections",
        "generation_params",
        "batch_config",
        "guide_frames",
        "asset_ids",
        "is_bridge",
        "video_lane_count",
        "motion_driver_lane_count",
        "audio_lane_count",
        "video_lane_configs",
        "motion_driver_lane_configs",
        "audio_lane_configs",
        "guide_track_config",
        "prompt_track_config",
        "width",
        "height",
        "fps",
        "saved_selections",
    ]:
        assert payload[key] == source.to_dict()[key]
    assert payload["clips"][0]["source_path"] == source.clips[0].source_path
    assert payload["clips"][0]["role"] == source.clips[0].role
    assert payload["audio_tracks"][0]["source_path"] == source.audio_tracks[0].source_path


def test_guide_swap_route_swaps_frames_and_respects_lock(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(
        scene_id="scene-1",
        name="Scene",
        guide_frames=[
            GuideFrame(frame_index=10, asset_id="guide-a", strength=0.4),
            GuideFrame(frame_index=20, asset_id="guide-b", strength=0.8),
        ],
    )
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

    swap_guides = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/guides/swap",
    )
    response = asyncio.run(swap_guides(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"frame_a": 10, "frame_b": 20},
    )))

    assert response.status == 200
    assert [(guide.frame_index, guide.asset_id) for guide in scene.guide_frames] == [
        (10, "guide-b"),
        (20, "guide-a"),
    ]

    scene.guide_track_config = LaneConfig(locked=True)
    locked = asyncio.run(swap_guides(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"frame_a": 10, "frame_b": 20},
    )))
    assert locked.status == 409


def test_clip_split_preserves_motion_driver_role_and_strength(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    clip = ClipReference(
        clip_id="clip-1",
        source_path="media/driver.mp4",
        timeline_start_frame=0,
        timeline_end_frame=10,
        source_in_frame=0,
        source_out_frame=10,
        total_source_frames=10,
        track_index=0,
        role="motion_driver",
        strength=0.42,
    )
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.clips = [clip]
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

    split_clip = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips/{clip_id}/split",
    )
    response = asyncio.run(split_clip(DummyRequest(
        match_info={"scene_id": "scene-1", "clip_id": "clip-1"},
        body={"frame": 4},
    )))
    payload = _response_json(response)

    assert payload["left"]["role"] == "motion_driver"
    assert payload["right"]["role"] == "motion_driver"
    assert payload["left"]["strength"] == 0.42
    assert payload["right"]["strength"] == 0.42


def test_clear_queue_route_removes_only_completed_jobs(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = TimelineProject(project_dir=str(project_dir), name="Project")
    project.generation_queue = [
        GenerationJob(job_id="pending-1", status="pending"),
        GenerationJob(job_id="running-1", status="running"),
        GenerationJob(job_id="completed-1", status="completed"),
        GenerationJob(job_id="failed-1", status="failed"),
        GenerationJob(job_id="skipped-1", status="skipped"),
    ]

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project, **kwargs: None)

    clear_queue = _route_handler(
        route_module,
        "DELETE",
        "/sonder-editor/project/{project_id}/queue",
    )
    response = asyncio.run(clear_queue(DummyRequest(match_info={"project_id": "project"})))
    payload = _response_json(response)

    assert payload["status"] == "cleared"
    assert payload["removed"] == 1
    assert [job["job_id"] for job in payload["queue"]] == [
        "pending-1",
        "running-1",
        "failed-1",
        "skipped-1",
    ]
    assert [job.job_id for job in project.generation_queue] == [
        "pending-1",
        "running-1",
        "failed-1",
        "skipped-1",
    ]


def test_delete_last_clip_compacts_empty_video_lane(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    delete_me = ClipReference(
        clip_id="delete-me",
        source_path="media/a.mp4",
        timeline_start_frame=0,
        timeline_end_frame=10,
        track_index=1,
        role="render",
    )
    higher = ClipReference(
        clip_id="higher",
        source_path="media/b.mp4",
        timeline_start_frame=0,
        timeline_end_frame=10,
        track_index=2,
        role="render",
    )
    driver = ClipReference(
        clip_id="driver",
        source_path="media/driver.mp4",
        timeline_start_frame=0,
        timeline_end_frame=10,
        track_index=1,
        role="motion_driver",
    )
    scene = Scene(scene_id="scene-1", name="Scene", video_lane_count=3)
    scene.video_lane_configs = [
        LaneConfig(name="Lane 1"),
        LaneConfig(name="Lane 2"),
        LaneConfig(name="Lane 3"),
    ]
    scene.clips = [delete_me, higher, driver]
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

    delete_clip = _route_handler(
        route_module,
        "DELETE",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips/{clip_id}",
    )
    response = asyncio.run(delete_clip(DummyRequest(
        match_info={"scene_id": "scene-1", "clip_id": "delete-me"},
    )))

    assert response.status == 200
    assert scene.video_lane_count == 2
    assert higher.track_index == 1
    assert driver.track_index == 1
    assert [config.name for config in scene.video_lane_configs] == ["Lane 1", "Lane 3"]


def test_delete_clip_preserve_lane_keeps_empty_video_lane(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    clip = ClipReference(
        clip_id="clip-1",
        source_path="media/a.mp4",
        timeline_start_frame=0,
        timeline_end_frame=10,
        track_index=1,
        role="render",
    )
    scene = Scene(scene_id="scene-1", name="Scene", video_lane_count=2)
    scene.video_lane_configs = [LaneConfig(name="Lane 1"), LaneConfig(name="Lane 2")]
    scene.clips = [clip]
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

    delete_clip = _route_handler(
        route_module,
        "DELETE",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips/{clip_id}",
    )
    response = asyncio.run(delete_clip(DummyRequest(
        match_info={"scene_id": "scene-1", "clip_id": "clip-1"},
        query={"preserve_lane": "1"},
    )))

    assert response.status == 200
    assert scene.video_lane_count == 2
    assert scene.clips == []
    assert [config.name for config in scene.video_lane_configs] == ["Lane 1", "Lane 2"]


def test_delete_last_audio_track_compacts_empty_audio_lane(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    delete_me = AudioTrack(
        track_id="delete-me",
        source_path="media/a.wav",
        timeline_start_frame=0,
        timeline_end_frame=10,
        lane_index=0,
    )
    higher = AudioTrack(
        track_id="higher",
        source_path="media/b.wav",
        timeline_start_frame=0,
        timeline_end_frame=10,
        lane_index=2,
    )
    scene = Scene(scene_id="scene-1", name="Scene", audio_lane_count=3)
    scene.audio_lane_configs = [
        LaneConfig(name="Audio 1"),
        LaneConfig(name="Audio 2"),
        LaneConfig(name="Audio 3"),
    ]
    scene.audio_tracks = [delete_me, higher]
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

    delete_audio = _route_handler(
        route_module,
        "DELETE",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/audio_tracks/{track_id}",
    )
    response = asyncio.run(delete_audio(DummyRequest(
        match_info={"scene_id": "scene-1", "track_id": "delete-me"},
    )))

    assert response.status == 200
    assert scene.audio_lane_count == 2
    assert higher.lane_index == 1
    assert [config.name for config in scene.audio_lane_configs] == ["Audio 2", "Audio 3"]


def test_bridge_guides_route_returns_all_scene_guides(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(
        scene_id="scene-1",
        name="Scene",
        duration_frames=120,
        guide_frames=[
            GuideFrame(frame_index=10, asset_id="asset-1", strength=0.7, muted=False),
            GuideFrame(frame_index=50, asset_id="asset-1", strength=1.0, muted=True),
            GuideFrame(frame_index=110, asset_id="asset-1", strength=0.5, muted=False),
        ],
    )
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    project.assets = [Asset(asset_id="asset-1", asset_type="image", path="media/g.png", name="GuideRef")]

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)

    handler = _route_handler(
        route_module,
        "GET",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/bridge-guides",
    )

    # Default request: all three scene guides returned with full-scene window.
    response = asyncio.run(handler(DummyRequest(match_info={"scene_id": "scene-1"})))
    assert response.status == 200
    payload = _response_json(response)
    assert payload["source"] == "live"
    assert payload["window_start"] == 0
    assert payload["window_end"] == 120
    assert len(payload["guides"]) == 3
    keys = [row["guide_key"] for row in payload["guides"]]
    assert keys == ["asset-1:10", "asset-1:50", "asset-1:110"]
    assert payload["all_guide_keys"] == ["asset-1:10", "asset-1:50", "asset-1:110"]
    muted_row = next(row for row in payload["guides"] if row["frame_index"] == 50)
    assert muted_row["editor_muted"] is True
    assert muted_row["asset_name"] == "GuideRef"

    # Legacy selection params are accepted but ignored: still all three guides.
    response = asyncio.run(handler(DummyRequest(
        match_info={"scene_id": "scene-1"},
        query={"selection_start": "40", "selection_end": "60", "pre_context": "0", "post_context": "0"},
    )))
    payload = _response_json(response)
    assert [row["frame_index"] for row in payload["guides"]] == [10, 50, 110]
    assert payload["all_guide_keys"] == ["asset-1:10", "asset-1:50", "asset-1:110"]

    scene.guide_track_config = LaneConfig(hidden=True)
    response = asyncio.run(handler(DummyRequest(match_info={"scene_id": "scene-1"})))
    payload = _response_json(response)
    assert all(row["editor_muted"] is True for row in payload["guides"])


def test_bridge_guides_route_uses_running_job_snapshot(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(
        scene_id="scene-1",
        name="Scene",
        duration_frames=120,
        guide_frames=[
            GuideFrame(frame_index=10, asset_id="asset-1", strength=1.0, muted=False),
        ],
    )
    snapshot_dicts = [
        {"frame_index": 30, "asset_id": "asset-1", "source": "asset", "strength": 0.8, "muted": False},
        {"frame_index": 70, "asset_id": "asset-1", "source": "asset", "strength": 1.0, "muted": True},
    ]
    running_job = GenerationJob(
        job_id="job-running",
        scene_id="scene-1",
        status="running",
        guide_frame_snapshots=snapshot_dicts,
        params={"snapshot_version": 1},
    )
    project = TimelineProject(
        project_dir=str(project_dir),
        name="Project",
        scenes=[scene],
        generation_queue=[running_job],
    )
    project.assets = [Asset(asset_id="asset-1", asset_type="image", path="media/g.png", name="Snap")]

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    handler = _route_handler(
        route_module,
        "GET",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/bridge-guides",
    )

    response = asyncio.run(handler(DummyRequest(match_info={"scene_id": "scene-1"})))
    payload = _response_json(response)
    assert response.status == 200
    assert payload["source"] == "snapshot"
    keys = [row["guide_key"] for row in payload["guides"]]
    assert keys == ["asset-1:30", "asset-1:70"]
    # Snapshot key set comes from the snapshot, not the live scene guides.
    assert payload["all_guide_keys"] == ["asset-1:30", "asset-1:70"]
    # Live guide at frame 10 is NOT included while a running job is active.
    assert "asset-1:10" not in keys

    scene.guide_track_config = LaneConfig(hidden=True)
    response = asyncio.run(handler(DummyRequest(match_info={"scene_id": "scene-1"})))
    payload = _response_json(response)
    # Snapshot rows use their frozen muted flags, not the live guide-track hidden flag.
    assert [row["editor_muted"] for row in payload["guides"]] == [False, True]


def test_bridge_guides_route_resolves_minus_one_frame_index(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(
        scene_id="scene-1",
        name="Scene",
        duration_frames=100,
        guide_frames=[
            GuideFrame(frame_index=-1, asset_id="asset-1", strength=1.0, muted=False),
        ],
    )
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    project.assets = [Asset(asset_id="asset-1", asset_type="image", path="media/g.png", name="LastFrame")]

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    handler = _route_handler(
        route_module,
        "GET",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/bridge-guides",
    )

    response = asyncio.run(handler(DummyRequest(match_info={"scene_id": "scene-1"})))
    payload = _response_json(response)
    # `-1` sentinel resolves to `duration - 1` for both the rendered row key and
    # the unfiltered `all_guide_keys` set, so frontend pruning matches the row.
    assert [row["guide_key"] for row in payload["guides"]] == ["asset-1:99"]
    assert [row["frame_index"] for row in payload["guides"]] == [99]
    assert payload["all_guide_keys"] == ["asset-1:99"]
