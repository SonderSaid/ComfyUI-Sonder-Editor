"""Route-level coverage for Phase 4.3 clip role fields."""

import asyncio
import copy
import importlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
import server.routes as routes
from server import minimax_h3, prompt_context
from server.timeline_state import (
    Asset,
    AudioTrack,
    BatchConfig,
    ClipReference,
    GenerationJob,
    GuideFrame,
    LaneConfig,
    PromptSection,
    ReferenceItem,
    ReferenceLaneRecipe,
    Scene,
    TimelineProject,
)


class DummyRequest:
    def __init__(self, *, match_info=None, query=None, body=None, headers=None,
                 method="GET", path=""):
        self.match_info = match_info or {}
        self.query = query or {}
        self._body = body
        self.headers = headers or {}
        self.method = method
        self.path = path

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


def test_saved_selection_routes_preserve_mask_offsets(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene 1", duration_frames=120)
    project = TimelineProject(project_dir=str(tmp_path / "project"), name="Project", scenes=[scene])
    saved_projects = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saved_projects.append(saved_project))

    post_handler = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/saved_selections",
    )
    post_response = asyncio.run(post_handler(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={
            "name": "Masked",
            "start": 10,
            "end": 42,
            "pre_context_frames": 8,
            "post_context_frames": 16,
            "mask_pre_offset": 4,
            "mask_post_offset": 12,
        },
    )))
    post_payload = _response_json(post_response)

    assert post_response.status == 200
    assert post_payload["entry"]["mask_pre_offset"] == 4
    assert post_payload["entry"]["mask_post_offset"] == 12
    assert scene.saved_selections[0]["mask_pre_offset"] == 4
    assert scene.saved_selections[0]["mask_post_offset"] == 12

    put_handler = _route_handler(
        route_module,
        "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/saved_selections/{index}",
    )
    put_response = asyncio.run(put_handler(DummyRequest(
        match_info={"scene_id": "scene-1", "index": "0"},
        body={"mask_pre_offset": 6, "mask_post_offset": 18},
    )))
    put_payload = _response_json(put_response)

    assert put_response.status == 200
    assert put_payload["mask_pre_offset"] == 6
    assert put_payload["mask_post_offset"] == 18
    assert len(saved_projects) == 2


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
    scene = Scene(scene_id="scene-1", name="Scene", motion_driver_lane_count=2)
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
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("drivers must not dual-drop audio")),
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
        body={"role": "motion_driver", "track_index": 1, "strength": 0.42},
    )))
    updated_json = _response_json(updated)
    assert updated.status == 200
    assert updated_json["role"] == "motion_driver"
    assert updated_json["strength"] == 0.42

    duplicate_driver = asyncio.run(add_clip(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"asset_id": "asset-1", "role": "motion_driver", "track_index": 1},
    )))
    assert duplicate_driver.status == 409
    assert _response_json(duplicate_driver)["code"] == "driver_lane_occupied"

    render_for_collision = asyncio.run(add_clip(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"asset_id": "asset-1", "timeline_start_frame": 6},
    )))
    render_for_collision_json = _response_json(render_for_collision)
    collision_id = render_for_collision_json["clip_id"]
    collision_update = asyncio.run(update_clip(DummyRequest(
        match_info={"scene_id": "scene-1", "clip_id": collision_id},
        body={"role": "motion_driver", "track_index": 1},
    )))
    assert collision_update.status == 409
    assert _response_json(collision_update)["code"] == "driver_lane_occupied"
    collision_clip = next(clip for clip in scene.clips if clip.clip_id == collision_id)
    assert collision_clip.role == "render"

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

    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **kwargs: project)
    monkeypatch.setattr(route_module, "save_project", lambda project, **kwargs: None)

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

    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **kwargs: project)
    monkeypatch.setattr(route_module, "save_project", lambda project, **kwargs: None)

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


def test_scene_put_rejects_duplicate_driver_lane_state(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(scene_id="scene-1", name="Scene", motion_driver_lane_count=1)
    scene.clips = [
        ClipReference(clip_id="driver-a", source_path="media/a.mp4", timeline_end_frame=10, role="motion_driver"),
        ClipReference(clip_id="driver-b", source_path="media/b.mp4", timeline_end_frame=10, role="motion_driver"),
    ]
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    save_calls = []

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: save_calls.append(project))

    update_scene = _route_handler(
        route_module,
        "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}",
    )
    response = asyncio.run(update_scene(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"name": "Still invalid"},
    )))
    payload = _response_json(response)

    assert response.status == 409
    assert payload["code"] == "driver_lane_occupied"
    assert save_calls == []


def test_scene_restore_accepts_guide_and_prompt_track_config(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(scene_id="scene-1", name="Scene")
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])

    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **kwargs: project)
    monkeypatch.setattr(route_module, "save_project", lambda project, **kwargs: None)

    restore_scene = _route_handler(
        route_module,
        "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore",
    )
    base = scene.to_dict()
    target = copy.deepcopy(base)
    target.update({
            "name": "Restored",
            "guide_track_config": {"locked": True, "hidden": False},
            "prompt_track_config": {"locked": False, "hidden": True},
        })
    token = route_module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")
    response = asyncio.run(restore_scene(DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        body={"base_scene": base, "target_scene": target,
              "restore_token": token},
    )))
    payload = _response_json(response)["scene"]

    assert response.status == 200
    assert payload["name"] == "Restored"
    assert payload["guide_track_config"]["locked"] is True
    assert payload["guide_track_config"]["hidden"] is False
    assert payload["prompt_track_config"]["locked"] is False
    assert payload["prompt_track_config"]["hidden"] is True


def test_scene_restore_accepts_last_frame_guide_sentinel(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(
        scene_id="scene-1", name="After", duration_frames=24,
        guide_frames=[GuideFrame(guide_id="last", frame_index=-1)],
    )
    project = TimelineProject(
        project_id="project", project_dir=str(tmp_path), scenes=[scene])
    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **kwargs: project)
    monkeypatch.setattr(route_module, "save_project", lambda project, **kwargs: None)
    base = scene.to_dict()
    target = copy.deepcopy(base)
    target["name"] = "Before"
    token = route_module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")
    restore = _route_handler(
        route_module, "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")

    response = asyncio.run(restore(DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        body={"base_scene": base, "target_scene": target,
              "restore_token": token})))

    assert response.status == 200
    assert _response_json(response)["scene"]["guide_frames"] == [{
        "guide_id": "last", "frame_index": -1, "asset_id": "",
        "source": "", "strength": 1.0, "muted": False,
        "fit_mode": "pad_edge", "crop_position": "center",
    }]


def test_scene_restore_duration_change_conflicts_with_concurrent_last_frame_guide(
        tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    target = Scene(scene_id="scene-1", duration_frames=24).to_dict()
    base = copy.deepcopy(target)
    base["duration_frames"] = 48
    stored = copy.deepcopy(base)
    stored["guide_frames"] = [GuideFrame(
        guide_id="concurrent-last", frame_index=-1).to_dict()]
    project = TimelineProject(
        project_id="project", project_dir=str(tmp_path),
        scenes=[Scene.from_dict(stored)])
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **kwargs: project)
    monkeypatch.setattr(route_module, "save_project",
                        lambda project, **kwargs: saves.append(project))
    token = route_module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")
    restore = _route_handler(
        route_module, "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")

    response = asyncio.run(restore(DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        body={"base_scene": base, "target_scene": target,
              "restore_token": token})))

    payload = _response_json(response)
    assert response.status == 409
    assert payload["code"] == "scene_merge_conflict"
    assert payload["conflicts"][0]["path"] == (
        "guide_frames[concurrent-last].effective_frame_index")
    assert saves == []
    assert project.scenes[0].duration_frames == 48


def test_scene_restore_atomically_restores_reference_lane_ids_and_h3_setup(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(
        scene_id="scene-1", name="Scene", reference_lane_count=1,
        reference_lane_configs=[LaneConfig()],
        reference_lane_recipes=[ReferenceLaneRecipe(
            lane_id="current", recipe={"soft": {"physical_population": "pictures"}})],
        minimax_h3_conditioning_setups=[{
            "schema": "minimax_h3_setup_v1", "setup_id": "setup",
            "name": "Setup", "mode": "reference", "task_mode": "T2VA",
            "picture_lane_ids": ["current"], "video_lane_ids": [],
            "audio_lane_ids": [],
        }],
        active_minimax_h3_setup_id="setup",
    )
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **kwargs: project)
    monkeypatch.setattr(route_module, "save_project", lambda project, **kwargs: None)
    restore_scene = _route_handler(
        route_module, "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")

    base = scene.to_dict()
    target = copy.deepcopy(base)
    target.update({
            "reference_lane_count": 2,
            "reference_lane_configs": [{}, {}],
            "reference_lane_recipes": [
                {"lane_id": "old-a", "media_kind": "image", "recipe": {
                    "soft": {"physical_population": "pictures"}}},
                {"lane_id": "old-b", "media_kind": "image", "recipe": {
                    "soft": {"physical_population": "pictures"}}},
            ],
            "minimax_h3_conditioning_setups": [{
                "schema": "minimax_h3_setup_v1", "setup_id": "setup",
                "name": "Setup", "mode": "reference", "task_mode": "T2VA",
                "picture_lane_ids": ["old-a", "old-b"],
                "video_lane_ids": [], "audio_lane_ids": [],
            }],
            "active_minimax_h3_setup_id": "setup",
        })
    token = route_module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")
    response = asyncio.run(restore_scene(DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        body={"base_scene": base, "target_scene": target,
              "restore_token": token},
    )))
    payload = _response_json(response)["scene"]
    assert response.status == 200
    assert [value["lane_id"] for value in payload["reference_lane_recipes"]] == [
        "old-a", "old-b"]
    assert payload["minimax_h3_conditioning_setups"][0]["picture_lane_ids"] == [
        "old-a", "old-b"]


def test_scene_restore_does_not_normalize_untouched_h3_setup_data(
        tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    setup = {
        "schema": "minimax_h3_setup_v1", "setup_id": "setup",
        "name": "Setup", "mode": "reference", "task_mode": "T2VA",
        "picture_lane_ids": [], "video_lane_ids": [], "audio_lane_ids": [],
        "future_field": {"preserve": True},
    }
    scene = Scene(scene_id="scene-1", name="After",
                  minimax_h3_conditioning_setups=[copy.deepcopy(setup)],
                  active_minimax_h3_setup_id="setup")
    project = TimelineProject(project_id="project", project_dir=str(tmp_path),
                              scenes=[scene])
    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **kwargs: project)
    monkeypatch.setattr(route_module, "save_project", lambda project, **kwargs: None)
    base = scene.to_dict()
    target = copy.deepcopy(base)
    target["name"] = "Before"
    token = route_module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")
    restore = _route_handler(
        route_module, "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")

    response = asyncio.run(restore(DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        body={"base_scene": base, "target_scene": target,
              "restore_token": token})))

    assert response.status == 200
    assert _response_json(response)["scene"][
        "minimax_h3_conditioning_setups"][0]["future_field"] == {
            "preserve": True}


def test_scene_restore_preserves_unknown_scene_and_typed_member_fields(
        tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    stored = Scene(
        scene_id="scene-1", name="After", duration_frames=24,
        clips=[ClipReference(
            clip_id="clip", timeline_start_frame=0,
            timeline_end_frame=8)],
    ).to_dict()
    stored["future_scene"] = {"owner": "future"}
    stored["clips"][0]["future_clip"] = {"owner": "future"}
    scene = Scene.from_dict(stored)
    project = TimelineProject(
        project_id="project", project_dir=str(tmp_path), scenes=[scene])
    saved = []
    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **kwargs: project)
    monkeypatch.setattr(route_module, "save_project",
                        lambda project, **kwargs: saved.append(project.to_dict()))
    base = scene.to_dict()
    target = copy.deepcopy(base)
    target["name"] = "Before"
    token = route_module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")
    restore = _route_handler(
        route_module, "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")

    response = asyncio.run(restore(DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        body={"base_scene": base, "target_scene": target,
              "restore_token": token})))
    payload = _response_json(response)["scene"]

    assert response.status == 200
    assert payload["future_scene"] == {"owner": "future"}
    assert payload["clips"][0]["future_clip"] == {"owner": "future"}
    assert saved[0]["scenes"][0]["future_scene"] == {"owner": "future"}
    assert saved[0]["scenes"][0]["clips"][0]["future_clip"] == {
        "owner": "future"}


def test_scene_put_preserves_omitted_reference_lane_id_and_setup_binding(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(
        scene_id="scene-1", reference_lane_count=1,
        reference_lane_configs=[LaneConfig()],
        reference_lane_recipes=[ReferenceLaneRecipe(
            lane_id="stable", recipe={"soft": {"physical_population": "pictures"}})],
        minimax_h3_conditioning_setups=[{
            "schema": "minimax_h3_setup_v1", "setup_id": "setup",
            "name": "Setup", "mode": "reference", "task_mode": "T2VA",
            "picture_lane_ids": ["stable"], "video_lane_ids": [],
            "audio_lane_ids": [],
        }], active_minimax_h3_setup_id="setup",
    )
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)
    update_scene = _route_handler(
        route_module, "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}")
    response = asyncio.run(update_scene(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"reference_lane_recipes": [{
            "media_kind": "image", "recipe_id": "edited",
            "recipe": {"soft": {"physical_population": "pictures"}},
        }]},
    )))
    payload = _response_json(response)
    assert response.status == 200
    assert payload["reference_lane_recipes"][0]["lane_id"] == "stable"
    assert payload["minimax_h3_conditioning_setups"][0]["picture_lane_ids"] == [
        "stable"]


def test_scene_put_count_only_shrink_drops_removed_h3_population_lane(tmp_path, monkeypatch):
    """Shrinking the lane count removes the lane, and its population with it.

    There is no setup binding to prune: membership is derived from each
    recipe's declared model input, so the removed lane simply stops being a
    candidate.
    """
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    recipes = [ReferenceLaneRecipe(
        lane_id=lane_id,
        recipe={"soft": {
            "compatible_profiles": ["minimax_h3_ref@1"],
            "physical_population": "pictures",
        }},
    ) for lane_id in ("keep", "remove")]
    scene = Scene(
        scene_id="scene-1", duration_frames=24,
        reference_lane_count=2,
        reference_lane_configs=[LaneConfig(), LaneConfig()],
        reference_lane_recipes=recipes,
    )
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)
    update_scene = _route_handler(
        route_module, "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}")

    response = asyncio.run(update_scene(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"reference_lane_count": 1},
    )))
    payload = _response_json(response)
    assert response.status == 200
    assert [value["lane_id"] for value in payload["reference_lane_recipes"]] == [
        "keep"]
    assert minimax_h3.population_lane_ids(
        payload["reference_lane_recipes"], "pictures") == ["keep"]


def test_scene_restore_rejects_duplicate_driver_clip_snapshot(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(scene_id="scene-1", name="Scene", motion_driver_lane_count=1)
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    save_calls = []

    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **kwargs: project)
    monkeypatch.setattr(route_module, "save_project",
                        lambda project, **kwargs: save_calls.append(project))

    restore_scene = _route_handler(
        route_module,
        "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore",
    )
    base = scene.to_dict()
    target = copy.deepcopy(base)
    target.update({
            "clips": [
                {"clip_id": "driver-a", "source_path": "media/a.mp4", "timeline_end_frame": 10, "role": "motion_driver"},
                {"clip_id": "driver-b", "source_path": "media/b.mp4", "timeline_end_frame": 10, "role": "motion_driver"},
            ],
            "motion_driver_lane_count": 1,
        })
    token = route_module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")
    response = asyncio.run(restore_scene(DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        body={"base_scene": base, "target_scene": target,
              "restore_token": token},
    )))
    payload = _response_json(response)

    assert response.status == 409
    assert payload["code"] == "scene_merge_invalid"
    assert payload["invariant"] == "driver_lane_occupied"
    assert save_calls == []


@pytest.mark.parametrize("concurrent_kind", ["new_clip", "observed_take"])
def test_scene_restore_preserves_concurrent_generated_work(
        concurrent_kind, tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    target_scene = Scene(scene_id="scene-1", duration_frames=24, clips=[
        ClipReference(clip_id="edited", source_path="media/a.mp4",
                      timeline_start_frame=0, timeline_end_frame=8),
    ])
    target = target_scene.to_dict()
    base = copy.deepcopy(target)
    base["clips"][0].update({"timeline_start_frame": 4,
                              "timeline_end_frame": 12})
    stored = copy.deepcopy(base)
    if concurrent_kind == "new_clip":
        stored["clips"].append(ClipReference(
            clip_id="generated", source_path="media/take.mp4",
            timeline_start_frame=12, timeline_end_frame=20,
            is_generated=True).to_dict())
    else:
        stored["clips"][0]["takes"].append({"asset_id": "generated-take"})
    scene = Scene.from_dict(stored)
    project = TimelineProject(project_id="project", project_dir=str(tmp_path),
                              scenes=[scene])
    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **kwargs: project)
    monkeypatch.setattr(route_module, "save_project", lambda project, **kwargs: None)
    handler = _route_handler(
        route_module, "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")
    token = route_module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")

    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        body={"base_scene": base, "target_scene": target,
              "restore_token": token})))
    payload = _response_json(response)["scene"]

    assert response.status == 200
    assert payload["clips"][0]["timeline_start_frame"] == 0
    if concurrent_kind == "new_clip":
        assert [clip["clip_id"] for clip in payload["clips"]] == [
            "edited", "generated"]
    else:
        assert payload["clips"][0]["takes"] == [{"asset_id": "generated-take"}]


def test_scene_restore_same_field_conflict_saves_nothing(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    target_scene = Scene(scene_id="scene-1", duration_frames=24, clips=[
        ClipReference(clip_id="clip", timeline_start_frame=0,
                      timeline_end_frame=8),
    ])
    target = target_scene.to_dict()
    base = copy.deepcopy(target)
    base["clips"][0]["timeline_start_frame"] = 4
    stored = copy.deepcopy(base)
    stored["clips"][0]["timeline_start_frame"] = 7
    project = TimelineProject(project_id="project", project_dir=str(tmp_path),
                              scenes=[Scene.from_dict(stored)])
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **kwargs: project)
    monkeypatch.setattr(route_module, "save_project",
                        lambda project, **kwargs: saves.append(project))
    handler = _route_handler(
        route_module, "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")
    token = route_module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")

    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        body={"base_scene": base, "target_scene": target,
              "restore_token": token})))
    payload = _response_json(response)

    assert response.status == 409
    assert payload["code"] == "scene_merge_conflict"
    assert payload["conflicts"][0]["path"] == "clips[clip].timeline_start_frame"
    assert saves == []


def test_scene_restore_refuses_dangling_concurrent_link_group(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    target_scene = Scene(scene_id="scene-1", duration_frames=24, clips=[
        ClipReference(clip_id="d", timeline_start_frame=0,
                      timeline_end_frame=8),
    ])
    target = target_scene.to_dict()
    base = copy.deepcopy(target)
    base["clips"].append(ClipReference(
        clip_id="c", timeline_start_frame=8,
        timeline_end_frame=16).to_dict())
    stored = copy.deepcopy(base)
    stored["linked_item_groups"] = [{
        "group_id": "concurrent", "items": [
            {"type": "clip", "id": "c"},
            {"type": "clip", "id": "d"},
        ],
    }]
    project = TimelineProject(
        project_id="project", project_dir=str(tmp_path),
        scenes=[Scene.from_dict(stored)])
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **kwargs: project)
    monkeypatch.setattr(route_module, "save_project",
                        lambda project, **kwargs: saves.append(project))
    handler = _route_handler(
        route_module, "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")
    token = route_module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")

    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        body={"base_scene": base, "target_scene": target,
              "restore_token": token})))

    assert response.status == 409
    assert _response_json(response)["code"] == "scene_merge_invalid"
    assert saves == []
    assert project.scenes[0].linked_item_groups == stored["linked_item_groups"]


def test_scene_restore_reorders_prompt_sections_after_merged_range_change(
        tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    first = PromptSection(0, 8)
    first.prompt_id = "first"
    second = PromptSection(8, 16)
    second.prompt_id = "second"
    base_scene = Scene(scene_id="scene-1", duration_frames=24,
                       prompt_sections=[first, second])
    base = base_scene.to_dict()
    target = copy.deepcopy(base)
    target["prompt_sections"][0].update({"start_frame": 8, "end_frame": 16})
    target["prompt_sections"][1].update({"start_frame": 0, "end_frame": 8})
    project = TimelineProject(
        project_id="project", project_dir=str(tmp_path),
        scenes=[Scene.from_dict(base)])
    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **kwargs: project)
    monkeypatch.setattr(route_module, "save_project", lambda project, **kwargs: None)
    handler = _route_handler(
        route_module, "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")
    token = route_module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")

    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        body={"base_scene": base, "target_scene": target,
              "restore_token": token})))
    sections = _response_json(response)["scene"]["prompt_sections"]

    assert response.status == 200
    assert [section["prompt_id"] for section in sections] == ["second", "first"]


def test_scene_restore_remerges_after_save_conflict(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    target_scene = Scene(scene_id="scene-1", duration_frames=24, name="Before")
    target = target_scene.to_dict()
    base = copy.deepcopy(target)
    base["name"] = "After"
    initial = TimelineProject(
        project_id="project", project_dir=str(tmp_path),
        modified_at="v1", scenes=[Scene.from_dict(base)])
    latest_scene = Scene.from_dict(base)
    latest_scene.clips.append(ClipReference(
        clip_id="generated", timeline_start_frame=0, timeline_end_frame=8))
    latest = TimelineProject(
        project_id="project", project_dir=str(tmp_path),
        modified_at="v2", scenes=[latest_scene])
    calls = []

    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **kwargs: initial)
    monkeypatch.setattr(route_module, "load_project", lambda directory: latest)

    def save_with_conflict(project, **kwargs):
        calls.append((project, kwargs.get("expected_modified_at")))
        if len(calls) == 1:
            raise route_module.ProjectVersionConflict(
                project_dir=str(tmp_path), expected_modified_at="v1",
                actual_modified_at="v2", current_data=latest.to_dict())

    monkeypatch.setattr(route_module, "save_project", save_with_conflict)
    handler = _route_handler(
        route_module, "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")
    token = route_module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")

    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        body={"base_scene": base, "target_scene": target,
              "restore_token": token})))
    payload = _response_json(response)["scene"]

    assert response.status == 200
    assert len(calls) == 2
    assert calls[0][1] == "v1"
    assert calls[1][1] == "v2"
    assert payload["name"] == "Before"
    assert [clip["clip_id"] for clip in payload["clips"]] == ["generated"]


def test_scene_restore_save_conflict_retry_is_bounded_and_receipted(
        tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="After")
    base = scene.to_dict()
    target = copy.deepcopy(base)
    target["name"] = "Before"
    versions = iter(["v2", "v3"])
    initial = TimelineProject(
        project_id="project", project_dir=str(tmp_path),
        modified_at="v1", scenes=[Scene.from_dict(base)])
    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **kwargs: initial)

    def load_latest(_directory):
        return TimelineProject(
            project_id="project", project_dir=str(tmp_path),
            modified_at=next(versions), scenes=[Scene.from_dict(base)])

    monkeypatch.setattr(route_module, "load_project", load_latest)
    saves = []

    def always_conflict(project, **kwargs):
        expected = kwargs.get("expected_modified_at")
        saves.append(expected)
        actual = f"v{len(saves) + 1}"
        current_data = project.to_dict(include_internal=True)
        current_data["modified_at"] = actual
        raise route_module.ProjectVersionConflict(
            project_dir=str(tmp_path), expected_modified_at=expected,
            actual_modified_at=actual,
            current_data=current_data)

    monkeypatch.setattr(route_module, "save_project", always_conflict)
    handler = _route_handler(
        route_module, "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")
    token = route_module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")
    request = DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        method="PUT", path="/sonder-editor/project/project/scenes/scene-1/restore",
        body={"base_scene": base, "target_scene": target,
              "restore_token": token})

    response = asyncio.run(route_module._project_conflict_middleware(request, handler))
    receipt = route_module._SCENE_RESTORE_RECEIPTS.get(token, "project", "scene-1")

    assert response.status == 409
    response_payload = _response_json(response)
    assert response_payload["code"] == "project_version_conflict"
    expected_keys = {
        "project_id", "modified_at",
        "prompt_semantic_units", "prompt_context_profiles",
    }
    assert set(response_payload["project"]) == expected_keys
    assert response_payload["project"] == {
        "project_id": "project",
        "modified_at": "v4",
        "prompt_semantic_units": [],
        "prompt_context_profiles": [],
    }
    assert response.headers["X-Sonder-Project-Id"] == "project"
    assert response.headers["X-Sonder-Project-Modified-At"] == "v4"
    assert saves == ["v1", "v2", "v3"]
    assert receipt.status == "refused"
    assert receipt.payload["code"] == "project_version_conflict"
    assert set(receipt.payload["project"]) == expected_keys
    assert receipt.payload["project"] == response_payload["project"]


def _scene_restore_invariant_case(case):
    if case == "empty_lane":
        base = Scene.from_dict(
            Scene(scene_id="scene-1", duration_frames=24).to_dict()).to_dict()
        target = copy.deepcopy(base)
        target["video_lane_count"] = 0
        target["video_lane_configs"] = []
        return base, target, copy.deepcopy(base)
    if case == "lane":
        base_scene = Scene(scene_id="scene-1", duration_frames=24,
                           video_lane_count=2,
                           video_lane_configs=[LaneConfig(), LaneConfig()])
        base = base_scene.to_dict()
        target = copy.deepcopy(base)
        target["video_lane_count"] = 1
        target["video_lane_configs"] = target["video_lane_configs"][:1]
        stored = copy.deepcopy(base)
        stored["clips"].append(ClipReference(
            clip_id="concurrent", timeline_start_frame=0,
            timeline_end_frame=8, track_index=1).to_dict())
        return base, target, stored
    if case == "prompt":
        base_scene = Scene(scene_id="scene-1", duration_frames=24,
                           prompt_sections=[PromptSection(0, 4)])
        base_scene.prompt_sections[0].prompt_id = "edited"
        base = base_scene.to_dict()
        target = copy.deepcopy(base)
        target["prompt_sections"][0]["end_frame"] = 8
        stored = copy.deepcopy(base)
        concurrent = PromptSection(4, 8).to_dict()
        concurrent["prompt_id"] = "concurrent"
        stored["prompt_sections"].append(concurrent)
        return base, target, stored
    if case == "guide":
        base_scene = Scene(scene_id="scene-1", duration_frames=24,
                           guide_frames=[GuideFrame(guide_id="edited", frame_index=3)])
        base = base_scene.to_dict()
        target = copy.deepcopy(base)
        target["guide_frames"][0]["frame_index"] = 5
        stored = copy.deepcopy(base)
        stored["guide_frames"].append(GuideFrame(
            guide_id="concurrent", frame_index=5).to_dict())
        return base, target, stored
    base_scene = Scene(scene_id="scene-1", duration_frames=24)
    base = base_scene.to_dict()
    target = copy.deepcopy(base)
    target["duration_frames"] = 12
    stored = copy.deepcopy(base)
    stored["clips"].append(ClipReference(
        clip_id="concurrent", timeline_start_frame=16,
        timeline_end_frame=20).to_dict())
    return base, target, stored


@pytest.mark.parametrize("case", ["empty_lane", "lane", "prompt", "guide", "duration"])
def test_scene_restore_rejects_invalid_merged_scene(case, tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    base, target, stored = _scene_restore_invariant_case(case)
    project = TimelineProject(project_id="project", project_dir=str(tmp_path),
                              scenes=[Scene.from_dict(stored)])
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **kwargs: project)
    monkeypatch.setattr(route_module, "save_project",
                        lambda project, **kwargs: saves.append(project))
    handler = _route_handler(
        route_module, "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")
    token = route_module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")

    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        body={"base_scene": base, "target_scene": target,
              "restore_token": token})))
    payload = _response_json(response)

    assert response.status == 409
    assert payload["code"] == "scene_merge_invalid"
    assert saves == []


def test_scene_restore_missing_base_requires_reload(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project = TimelineProject(project_id="project", project_dir=str(tmp_path),
                              scenes=[Scene(scene_id="scene-1")])
    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **kwargs: project)
    handler = _route_handler(
        route_module, "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")

    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        body={"target_scene": project.scenes[0].to_dict()})))
    payload = _response_json(response)

    assert response.status == 400
    assert "base_scene" in payload["error"]
    assert "reload" in payload["error"].lower()


def test_scene_restore_absent_field_round_trips_without_null(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", duration_frames=24, clips=[
        ClipReference(clip_id="clip", timeline_start_frame=0,
                      timeline_end_frame=8, takes=[{"asset_id": "take"}]),
    ])
    base = scene.to_dict()
    target = copy.deepcopy(base)
    target["clips"][0].pop("takes")
    project = TimelineProject(project_id="project", project_dir=str(tmp_path),
                              scenes=[Scene.from_dict(base)])
    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **kwargs: project)
    monkeypatch.setattr(route_module, "save_project", lambda project, **kwargs: None)
    handler = _route_handler(
        route_module, "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")
    token = route_module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")

    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        body={"base_scene": base, "target_scene": target,
              "restore_token": token})))
    payload = _response_json(response)["scene"]

    assert response.status == 200
    assert payload["clips"][0]["takes"] == []


def test_scene_restore_token_is_idempotent_and_queryable(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="After")
    project = TimelineProject(project_id="project", project_dir=str(tmp_path),
                              scenes=[scene])
    base = scene.to_dict()
    target = copy.deepcopy(base)
    target["name"] = "Before"
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **kwargs: project)
    monkeypatch.setattr(route_module, "save_project",
                        lambda project, **kwargs: saves.append(project.to_dict()))
    restore = _route_handler(
        route_module, "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")
    receipt_handler = _route_handler(
        route_module, "GET",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore-token/{token}")
    token = route_module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")
    request = DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        body={"base_scene": base, "target_scene": target,
              "restore_token": token})

    first = asyncio.run(restore(request))
    second = asyncio.run(restore(request))
    route_module._SCENE_RESTORE_RECEIPTS = route_module.SceneRestoreReceiptStore()
    restart_token = route_module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")
    after_restart = asyncio.run(restore(DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        body={"base_scene": base, "target_scene": target,
              "restore_token": restart_token})))
    route_module._SCENE_RESTORE_RECEIPTS = route_module.SceneRestoreReceiptStore()
    project.scenes[0].name = "Concurrent after restore"
    receipt = asyncio.run(receipt_handler(DummyRequest(match_info={
        "project_id": "project", "scene_id": "scene-1", "token": restart_token})))

    assert first.status == second.status == after_restart.status == receipt.status == 200
    assert _response_json(first) == _response_json(second)
    assert _response_json(after_restart)["scene"]["name"] == "Before"
    assert _response_json(receipt)["status"] == "committed"
    assert _response_json(receipt)["scene"]["name"] == "Concurrent after restore"
    # The fresh no-op token is also persisted so a later response loss remains
    # provable after restart and a same-field concurrent edit.
    assert len(saves) == 2


def test_scene_restore_durable_receipt_survives_restart_then_same_field_edit(
        tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = TimelineProject(
        project_id="project", project_dir=str(project_dir),
        scenes=[Scene(scene_id="scene-1", name="After")])
    route_module.save_project(project)
    project_box = [route_module.load_project(str(project_dir))]
    monkeypatch.setattr(
        route_module, "_load_project_from_request",
        lambda request, **kwargs: project_box[0])
    restore = _route_handler(
        route_module, "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")
    receipt_handler = _route_handler(
        route_module, "GET",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore-token/{token}")
    base = project_box[0].scenes[0].to_dict()
    target = copy.deepcopy(base)
    target["name"] = "Before"
    token = route_module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")
    request = DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        body={"base_scene": base, "target_scene": target,
              "restore_token": token})

    assert asyncio.run(restore(request)).status == 200
    route_module._SCENE_RESTORE_RECEIPTS = route_module.SceneRestoreReceiptStore()
    concurrent = route_module.load_project(str(project_dir))
    concurrent.scenes[0].name = "Concurrent"
    route_module.save_project(concurrent)
    project_box[0] = route_module.load_project(str(project_dir))
    version_before_retry = project_box[0].modified_at

    receipt = asyncio.run(receipt_handler(DummyRequest(match_info={
        "project_id": "project", "scene_id": "scene-1", "token": token})))
    retry = asyncio.run(restore(request))
    public_project = project_box[0].to_dict()
    persisted = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))

    assert receipt.status == retry.status == 200
    assert _response_json(receipt)["status"] == "committed"
    assert _response_json(receipt)["scene"]["name"] == "Concurrent"
    assert _response_json(retry)["scene"]["name"] == "Concurrent"
    assert project_box[0].modified_at == version_before_retry
    assert "_scene_restore_receipts" not in public_project
    assert token not in json.dumps(persisted)
    assert persisted["_scene_restore_receipts"]


def test_scene_restore_token_endpoint_issues_scene_scoped_receipt(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project = TimelineProject(project_id="project", project_dir=str(tmp_path),
                              scenes=[Scene(scene_id="scene-1")])
    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **kwargs: project)
    issue = _route_handler(
        route_module, "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore-token")
    query = _route_handler(
        route_module, "GET",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore-token/{token}")

    issue_response = asyncio.run(issue(DummyRequest(match_info={
        "project_id": "project", "scene_id": "scene-1"})))
    token = _response_json(issue_response)["restore_token"]
    query_response = asyncio.run(query(DummyRequest(match_info={
        "project_id": "project", "scene_id": "scene-1", "token": token})))
    wrong_scene = asyncio.run(query(DummyRequest(match_info={
        "project_id": "project", "scene_id": "other", "token": token})))

    assert issue_response.status == query_response.status == 200
    assert _response_json(query_response)["status"] == "pending"
    assert wrong_scene.status == 404


def test_scene_restore_real_disk_merge_is_durable_through_conflict_middleware(
        tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(scene_id="scene-1", duration_frames=24, clips=[
        ClipReference(clip_id="edited", timeline_start_frame=0,
                      timeline_end_frame=8),
    ])
    project = TimelineProject(project_id="project", project_dir=str(project_dir),
                              scenes=[scene])
    route_module.save_project(project)
    target = copy.deepcopy(scene.to_dict())

    after = route_module.load_project(str(project_dir))
    after.scenes[0].clips[0].timeline_start_frame = 4
    after.scenes[0].clips[0].timeline_end_frame = 12
    route_module.save_project(after)
    base = copy.deepcopy(after.scenes[0].to_dict())
    stale_version = after.modified_at

    latest = route_module.load_project(str(project_dir))
    latest.scenes[0].clips.append(ClipReference(
        clip_id="generated", timeline_start_frame=12,
        timeline_end_frame=20, is_generated=True))
    route_module.save_project(latest)
    monkeypatch.setattr(route_module, "_get_base_dir", lambda: str(tmp_path))
    token = route_module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")
    handler = _route_handler(
        route_module, "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")
    request = DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        headers={"If-Match": stale_version}, method="PUT",
        path="/sonder-editor/project/project/scenes/scene-1/restore",
        body={"base_scene": base, "target_scene": target,
              "restore_token": token})

    async def through_conflict_middleware(inner_request):
        return await route_module._project_conflict_middleware(inner_request, handler)

    response = asyncio.run(route_module._project_version_header_middleware(
        request, through_conflict_middleware))
    restored = route_module.load_project(str(project_dir))

    assert response.status == 200
    assert response.headers["X-Sonder-Project-Modified-At"] == restored.modified_at
    assert response.headers["X-Sonder-Project-Modified-At"] != stale_version
    assert restored.scenes[0].clips[0].timeline_start_frame == 0
    assert [clip.clip_id for clip in restored.scenes[0].clips] == [
        "edited", "generated"]


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
    reference_attachment = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"reference_item_id": "reference-item"},
    })
    linked_reference_attachment = prompt_context.normalize_attachment({
        **reference_attachment, "attachment_id": "linked-copy",
        "emission_group_id": reference_attachment["emission_group_id"],
    })
    source.prompt_sections = [
        PromptSection(start_frame=0, end_frame=24, prompt="section",
                      attachments=[reference_attachment]),
        PromptSection(start_frame=24, end_frame=48, prompt="section two",
                      attachments=[linked_reference_attachment]),
    ]
    source.reference_items = [ReferenceItem(
        reference_item_id="reference-item", lane_index=0,
        start_frame=0, end_frame=24, members=[])]
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

    assert payload["reference_items"][0]["reference_item_id"] != "reference-item"
    duplicated_references = [section["attachments"][0]
                             for section in payload["prompt_sections"]]
    assert all(value["source"]["reference_item_id"]
               == payload["reference_items"][0]["reference_item_id"]
               for value in duplicated_references)
    assert duplicated_references[0]["attachment_id"] != duplicated_references[1]["attachment_id"]
    assert duplicated_references[0]["emission_group_id"] == duplicated_references[1]["emission_group_id"]

    for key in [
        "duration_frames",
        "prompt",
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


def test_clip_split_rejects_motion_driver_atomically(tmp_path, monkeypatch):
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
    save_calls = []
    monkeypatch.setattr(route_module, "save_project", lambda project: save_calls.append(project))

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

    assert response.status == 409
    assert payload["code"] == "driver_clip_split_refused"
    assert len(scene.clips) == 1
    assert scene.clips[0] is clip
    assert clip.timeline_start_frame == 0
    assert clip.timeline_end_frame == 10
    assert save_calls == []


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


def test_bridge_drivers_route_returns_live_driver_lanes(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=120, motion_driver_lane_count=2)
    scene.motion_driver_lane_configs = [
        LaneConfig(name="Canny", hidden=False),
        LaneConfig(name="Depth", hidden=True),
    ]
    scene.clips = [
        ClipReference(
            clip_id="driver-1",
            source_path="media/driver.mp4",
            timeline_start_frame=10,
            timeline_end_frame=40,
            track_index=1,
            role="motion_driver",
            strength=0.45,
            muted=False,
        ),
    ]
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    project.assets = [Asset(asset_id="asset-1", asset_type="video", path="media/driver.mp4", name="Depth.mov")]

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    handler = _route_handler(
        route_module,
        "GET",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/bridge-drivers",
    )

    response = asyncio.run(handler(DummyRequest(match_info={"scene_id": "scene-1"})))
    payload = _response_json(response)

    assert response.status == 200
    assert payload["source"] == "live"
    assert payload["driver_lane_count"] == 2
    assert payload["all_driver_keys"] == ["lane:0", "lane:1"]
    assert payload["drivers"][0]["has_clip"] is False
    assert payload["drivers"][0]["lane_name"] == "Canny"
    assert payload["drivers"][1]["has_clip"] is True
    assert payload["drivers"][1]["lane_name"] == "Depth"
    assert payload["drivers"][1]["asset_name"] == "Depth.mov"
    assert payload["drivers"][1]["editor_muted"] is True
    assert payload["drivers"][1]["strength"] == 0.45


def test_bridge_drivers_route_uses_running_job_snapshot(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=120, motion_driver_lane_count=1)
    scene.clips = [
        ClipReference(
            clip_id="live-driver",
            source_path="media/live.mp4",
            timeline_start_frame=5,
            timeline_end_frame=15,
            track_index=0,
            role="motion_driver",
        ),
    ]
    snapshot_clip = ClipReference(
        clip_id="snap-driver",
        source_path="media/snapshot.mp4",
        timeline_start_frame=20,
        timeline_end_frame=50,
        track_index=0,
        role="motion_driver",
        strength=0.8,
        muted=True,
    )
    running_job = GenerationJob(
        job_id="job-running",
        scene_id="scene-1",
        status="running",
        params={"snapshot_version": 1},
        driver_clip_snapshots=[snapshot_clip.to_dict()],
        driver_lane_count=1,
        driver_lane_configs=[{"name": "Frozen", "hidden": False}],
    )
    project = TimelineProject(
        project_dir=str(project_dir),
        name="Project",
        scenes=[scene],
        generation_queue=[running_job],
    )
    project.assets = [Asset(asset_id="asset-1", asset_type="video", path="media/snapshot.mp4", name="Snapshot.mov")]

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    handler = _route_handler(
        route_module,
        "GET",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/bridge-drivers",
    )

    response = asyncio.run(handler(DummyRequest(match_info={"scene_id": "scene-1"})))
    payload = _response_json(response)

    assert response.status == 200
    assert payload["source"] == "snapshot"
    assert payload["drivers"][0]["clip_id"] == "snap-driver"
    assert payload["drivers"][0]["asset_name"] == "Snapshot.mov"
    assert payload["drivers"][0]["editor_muted"] is True
    assert payload["drivers"][0]["source"] == "snapshot"


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
