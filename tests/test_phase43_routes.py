"""Route-level coverage for Phase 4.3 clip role fields."""

import asyncio
import importlib
import json
import os
import sys
from types import SimpleNamespace

from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
import server.routes as routes
from server.timeline_state import Asset, ClipReference, Scene, TimelineProject


class DummyRequest:
    def __init__(self, *, match_info=None, body=None):
        self.match_info = match_info or {}
        self.query = {}
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
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

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
