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
from server.timeline_state import ClipReference, GuideFrame, LaneConfig, Scene, TimelineProject


class DummyRequest(dict):
    def __init__(self, *, match_info=None, query=None, body=None, method="POST", path="/sonder-editor/project/proj"):
        super().__init__()
        self.match_info = match_info or {}
        self.query = query or {}
        self._body = body
        self.method = method
        self.path = path

    async def json(self):
        return self._body


def _load_route_module(monkeypatch):
    fake_prompt_server = SimpleNamespace(
        instance=SimpleNamespace(routes=web.RouteTableDef(), app=web.Application())
    )
    monkeypatch.setattr(server, "PromptServer", fake_prompt_server, raising=False)
    return importlib.reload(routes)


def _route_handler(route_module, method, path):
    for route in route_module.routes:
        if route.method == method and route.path == path:
            return route.handler
    raise AssertionError(f"Route not found: {method} {path}")


def _response_json(response):
    return json.loads(response.body.decode("utf-8"))


def test_project_version_header_middleware_attaches_loaded_project_version(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project = TimelineProject(project_dir="", name="Project")
    project.project_id = "project-1"
    project.modified_at = "version-1"

    async def handler(request):
        route_module._remember_request_project(request, project)
        return web.json_response({"status": "ok"})

    request = DummyRequest(
        match_info={"project_id": "project-1"},
        method="GET",
        path="/sonder-editor/project/project-1/scenes/scene-1",
    )
    response = asyncio.run(route_module._project_version_header_middleware(request, handler))

    assert response.headers["X-Sonder-Project-Id"] == "project-1"
    assert response.headers["X-Sonder-Project-Modified-At"] == "version-1"


def test_scene_mutation_remove_lane_is_single_save_and_reindexes(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.video_lane_count = 3
    scene.video_lane_configs = [LaneConfig(name="V0"), LaneConfig(name="V1"), LaneConfig(name="V2")]
    scene.clips = [
        ClipReference(clip_id="clip-0", timeline_start_frame=0, timeline_end_frame=10, track_index=0),
        ClipReference(clip_id="clip-1", timeline_start_frame=0, timeline_end_frame=10, track_index=1),
        ClipReference(clip_id="clip-2", timeline_start_frame=0, timeline_end_frame=10, track_index=2),
    ]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    handler = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/mutations",
    )
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={
            "operations": [{
                "type": "remove_lane",
                "lane_type": "video",
                "lane_index": 1,
                "item_policy": "move_items",
                "target_lane": 0,
            }],
        },
    )))

    assert response.status == 200
    assert len(saves) == 1
    assert scene.video_lane_count == 2
    assert [cfg.name for cfg in scene.video_lane_configs] == ["V0", "V2"]
    assert {clip.clip_id: clip.track_index for clip in scene.clips} == {
        "clip-0": 0,
        "clip-1": 0,
        "clip-2": 1,
    }


def test_scene_mutation_guide_identity_mismatch_rejects_before_save(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.guide_frames = [GuideFrame(frame_index=5, asset_id="asset-a", strength=0.7)]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    handler = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/mutations",
    )
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={
            "operations": [{
                "type": "move_guide",
                "from_frame_index": 5,
                "to_frame_index": 8,
                "expected": {"frame_index": 5, "asset_id": "different"},
            }],
        },
    )))
    payload = _response_json(response)

    assert response.status == 409
    assert payload["code"] == "identity_mismatch"
    assert saves == []
    assert [(guide.frame_index, guide.asset_id) for guide in scene.guide_frames] == [(5, "asset-a")]


def test_scene_mutation_move_guide_replaces_destination_frame(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.guide_frames = [
        GuideFrame(frame_index=5, asset_id="asset-a", strength=0.7),
        GuideFrame(frame_index=8, asset_id="asset-b", strength=0.4),
    ]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    handler = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/mutations",
    )
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={
            "operations": [{
                "type": "move_guide",
                "from_frame_index": 5,
                "to_frame_index": 8,
                "expected": {"frame_index": 5, "asset_id": "asset-a"},
                "asset_id": "asset-a",
                "source": "asset",
                "strength": 0.7,
            }],
        },
    )))
    payload = _response_json(response)

    assert response.status == 200
    assert len(saves) == 1
    assert [(guide.frame_index, guide.asset_id) for guide in scene.guide_frames] == [(8, "asset-a")]
    assert [(guide["frame_index"], guide["asset_id"]) for guide in payload["scene"]["guide_frames"]] == [(8, "asset-a")]


def test_scene_mutation_create_prompt_section_returns_reconciled_scene(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    handler = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/mutations",
    )
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={
            "operations": [{
                "type": "create_prompt_section",
                "fields": {"start_frame": 10, "end_frame": 20, "prompt": "hello"},
            }],
        },
    )))
    payload = _response_json(response)

    assert response.status == 200
    assert len(saves) == 1
    assert payload["scene"]["prompt_sections"] == [
        {"start_frame": 10, "end_frame": 20, "prompt": "hello"}
    ]


def test_queue_batch_route_appends_all_jobs_with_single_save(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project = TimelineProject(project_dir=str(tmp_path), name="Project")
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    handler = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/queue/batch",
    )
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj"},
        body={
            "jobs": [
                {
                    "scene_id": "scene-1",
                    "selection_start": 0,
                    "selection_end": 16,
                    "batch_id": "batch-1",
                    "batch_total": 2,
                    "batch_index": 0,
                    "template_id": "ltx-2.3",
                    "frame_constraint": {"step": 8, "offset": 1},
                },
                {
                    "scene_id": "scene-1",
                    "selection_start": 16,
                    "selection_end": 32,
                    "batch_id": "batch-1",
                    "batch_total": 2,
                    "batch_index": 1,
                    "template_id": "ltx-2.3",
                    "frame_constraint": {"step": 8, "offset": 1},
                },
            ],
        },
    )))
    payload = _response_json(response)

    assert response.status == 201
    assert payload["count"] == 2
    assert len(payload["jobs"]) == 2
    assert len(project.generation_queue) == 2
    assert len(saves) == 1
    assert [job.batch_index for job in project.generation_queue] == [0, 1]
    assert all(job.frame_constraint == {"step": 8, "offset": 1} for job in project.generation_queue)
