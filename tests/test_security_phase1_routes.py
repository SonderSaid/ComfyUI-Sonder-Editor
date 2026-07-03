import asyncio
import importlib
import json
import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from aiohttp import web

import server
import server.routes as routes
from server.project_manager import create_project, list_projects
from server.timeline_state import Asset, TimelineProject


class DummyRequest:
    def __init__(self, *, match_info=None, query=None, body=None, method="GET", headers=None):
        self.match_info = match_info or {}
        self.query = query or {}
        self.headers = headers or {}
        self.method = method
        self._body = body
        self.content_length = 0 if body is None else len(json.dumps(body).encode("utf-8"))

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


def _install_fake_folder_paths(monkeypatch, *, input_dir, output_dir):
    fake = SimpleNamespace(
        get_input_directory=lambda: str(input_dir),
        get_output_directory=lambda: str(output_dir),
    )
    monkeypatch.setitem(sys.modules, "folder_paths", fake)
    return fake


def _valid_video_metadata():
    return {
        "width": 16,
        "height": 16,
        "frame_count": 4,
        "fps": 24.0,
        "duration_sec": 4 / 24.0,
        "sample_rate": 0,
        "has_audio": False,
    }


def test_project_route_rejects_path_query(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    base_dir = tmp_path / "projects"
    project = create_project("Locked Project", base_dir=str(base_dir))
    monkeypatch.setattr(route_module, "_get_base_dir", lambda: str(base_dir))

    handler = _route_handler(route_module, "GET", "/sonder-editor/project/{project_id}")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": os.path.basename(project.project_dir)},
        query={"path": str(project.project_dir)},
    )))

    assert response.status == 400
    assert "path" in _response_json(response)["error"]

    empty_response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": os.path.basename(project.project_dir)},
        query={"path": ""},
    )))
    assert empty_response.status == 400


def test_project_route_rejects_traversal_project_id(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    base_dir = tmp_path / "projects"
    base_dir.mkdir()
    monkeypatch.setattr(route_module, "_get_base_dir", lambda: str(base_dir))

    handler = _route_handler(route_module, "GET", "/sonder-editor/project/{project_id}")
    response = asyncio.run(handler(DummyRequest(match_info={"project_id": ".."})))

    assert response.status == 400


def test_project_list_and_create_reject_public_base_dir(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    configured_base = tmp_path / "configured"
    external_base = tmp_path / "external"
    configured_base.mkdir()
    external_base.mkdir()
    monkeypatch.setattr(route_module, "_get_base_dir", lambda: str(configured_base))

    list_handler = _route_handler(route_module, "GET", "/sonder-editor/projects")
    list_response = asyncio.run(list_handler(DummyRequest(query={"base_dir": str(external_base)})))
    assert list_response.status == 400

    create_handler = _route_handler(route_module, "POST", "/sonder-editor/project")
    reject_response = asyncio.run(create_handler(DummyRequest(body={
        "name": "Rejected",
        "base_dir": str(external_base),
    })))
    assert reject_response.status == 400
    assert not (external_base / "Rejected").exists()

    ok_response = asyncio.run(create_handler(DummyRequest(body={"name": "Accepted"})))
    assert ok_response.status == 201
    assert (configured_base / "Accepted" / "project.json").is_file()


def test_list_projects_rejects_symlink_escape_when_supported(tmp_path):
    configured_base = tmp_path / "configured"
    external_base = tmp_path / "external"
    configured_base.mkdir()
    external_base.mkdir()
    external_project = create_project("External", base_dir=str(external_base))
    try:
        (configured_base / "linked").symlink_to(external_project.project_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available in this environment")

    projects = list_projects(str(configured_base))

    assert all(project["name"] != "External" for project in projects)
    assert all("external" not in os.path.realpath(project["path"]).lower() for project in projects)


def test_import_asset_rejects_absolute_host_path(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project = TimelineProject(project_dir=str(tmp_path / "project"))
    (tmp_path / "project" / "media").mkdir(parents=True)
    external = tmp_path / "outside.mp4"
    external.write_bytes(b"video")
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda _request: project)

    handler = _route_handler(route_module, "POST", "/sonder-editor/project/{project_id}/assets/import")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project"},
        body={"source_path": str(external)},
    )))

    assert response.status == 400
    assert project.assets == []


def test_import_asset_accepts_comfy_input_handle(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    source = input_dir / "clip.mp4"
    source.write_bytes(b"video")
    _install_fake_folder_paths(monkeypatch, input_dir=input_dir, output_dir=output_dir)
    project = TimelineProject(project_dir=str(tmp_path / "project"))
    (tmp_path / "project" / "media").mkdir(parents=True)
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda _request: project)
    monkeypatch.setattr(route_module, "_extract_asset_media_metadata", lambda *_args, **_kwargs: _valid_video_metadata())
    monkeypatch.setattr(route_module, "ensure_thumbnail", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(route_module, "save_project", lambda *_args, **_kwargs: None)

    handler = _route_handler(route_module, "POST", "/sonder-editor/project/{project_id}/assets/import")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project"},
        body={"source_path": "clip.mp4"},
    )))

    assert response.status == 201
    assert len(project.assets) == 1
    assert project.assets[0].path.replace("\\", "/").startswith("media/")
    assert os.path.isfile(os.path.join(project.project_dir, project.assets[0].path))


def test_replace_asset_rejects_absolute_host_path(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    (media_dir / "clip.mp4").write_bytes(b"old")
    external = tmp_path / "replacement.mp4"
    external.write_bytes(b"new")
    asset = Asset(asset_id="asset-1", asset_type="video", path=os.path.join("media", "clip.mp4"))
    project = TimelineProject(project_dir=str(project_dir), assets=[asset])
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda _request: project)

    handler = _route_handler(route_module, "POST", "/sonder-editor/project/{project_id}/assets/{asset_id}/replace")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project", "asset_id": "asset-1"},
        body={"source_path": str(external)},
    )))

    assert response.status == 400
    assert (media_dir / "clip.mp4").read_bytes() == b"old"


def test_replace_asset_accepts_comfy_input_handle(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    replacement = input_dir / "replacement.mp4"
    replacement.write_bytes(b"new")
    _install_fake_folder_paths(monkeypatch, input_dir=input_dir, output_dir=output_dir)
    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    (media_dir / "clip.mp4").write_bytes(b"old")
    asset = Asset(asset_id="asset-1", asset_type="video", path=os.path.join("media", "clip.mp4"))
    project = TimelineProject(project_dir=str(project_dir), assets=[asset])
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda _request: project)
    monkeypatch.setattr(route_module, "_extract_asset_media_metadata", lambda *_args, **_kwargs: _valid_video_metadata())
    monkeypatch.setattr(route_module, "ensure_thumbnail", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(route_module, "save_project", lambda *_args, **_kwargs: None)

    handler = _route_handler(route_module, "POST", "/sonder-editor/project/{project_id}/assets/{asset_id}/replace")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project", "asset_id": "asset-1"},
        body={"source_path": "replacement.mp4"},
    )))

    assert response.status == 200
    assert (media_dir / "clip.mp4").read_bytes() == b"new"


def test_delete_asset_rejects_registered_non_media_source_path(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    protected = project_dir / "project.json"
    protected.write_bytes(b"protected")
    asset = Asset(asset_id="asset-1", asset_type="video", path="project.json")
    project = TimelineProject(project_dir=str(project_dir), assets=[asset])
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda _request: project)
    monkeypatch.setattr(route_module, "save_project", lambda *_args, **_kwargs: pytest.fail("save should not run"))

    handler = _route_handler(route_module, "DELETE", "/sonder-editor/project/{project_id}/assets/{asset_id}")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project", "asset_id": "asset-1"},
    )))

    assert response.status == 400
    assert "under project media" in _response_json(response)["error"]
    assert asset.trashed_at == ""
    assert protected.read_bytes() == b"protected"


def test_replace_asset_rejects_registered_non_media_source_path(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    protected = project_dir / "project.json"
    protected.write_bytes(b"protected")
    asset = Asset(asset_id="asset-1", asset_type="video", path="project.json")
    project = TimelineProject(project_dir=str(project_dir), assets=[asset])
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda _request: project)
    monkeypatch.setattr(route_module, "save_project", lambda *_args, **_kwargs: pytest.fail("save should not run"))

    handler = _route_handler(route_module, "POST", "/sonder-editor/project/{project_id}/assets/{asset_id}/replace")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project", "asset_id": "asset-1"},
        body={"source_path": "replacement.mp4"},
    )))

    assert response.status == 400
    assert "under project media" in _response_json(response)["error"]
    assert asset.path == "project.json"
    assert protected.read_bytes() == b"protected"


def test_extract_frame_rejects_unregistered_absolute_source(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project = TimelineProject(project_dir=str(tmp_path / "project"))
    (tmp_path / "project" / "media").mkdir(parents=True)
    external = tmp_path / "outside.mp4"
    external.write_bytes(b"video")
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda _request: project)

    handler = _route_handler(route_module, "POST", "/sonder-editor/project/{project_id}/assets/extract_frame")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project"},
        body={"source_path": str(external), "frame_index": 0},
    )))

    assert response.status == 400


def test_extract_frame_accepts_registered_asset_id(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    (media_dir / "clip.mp4").write_bytes(b"video")
    asset = Asset(asset_id="asset-1", asset_type="video", path=os.path.join("media", "clip.mp4"))
    project = TimelineProject(project_dir=str(project_dir), assets=[asset])
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda _request: project)
    monkeypatch.setattr(route_module, "decode_video_frame", lambda *_args, **_kwargs: np.zeros((2, 2, 3), dtype=np.uint8))
    monkeypatch.setattr(route_module, "ensure_thumbnail", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(route_module, "save_project", lambda *_args, **_kwargs: None)

    handler = _route_handler(route_module, "POST", "/sonder-editor/project/{project_id}/assets/extract_frame")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project"},
        body={"asset_id": "asset-1", "frame_index": 0},
    )))

    assert response.status == 201
    assert len(project.assets) == 2
    assert project.assets[-1].path.replace("\\", "/").startswith("media/")


def test_widget_state_rejects_empty_path_query(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    handler = _route_handler(route_module, "GET", "/sonder-editor/project/{project_id}/widget_state")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project"},
        query={"path": ""},
    )))

    assert response.status == 400


def test_widget_state_rejects_traversal_project_id(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    handler = _route_handler(route_module, "GET", "/sonder-editor/project/{project_id}/widget_state")
    response = asyncio.run(handler(DummyRequest(match_info={"project_id": ".."})))

    assert response.status == 400


def test_widget_state_get_uses_raw_route_id_without_project_load(monkeypatch):
    # Regression (2026-07-03 mounted-tab "Canvas not connected"): the session
    # registry is raw-string keyed by the folder-basename id every frontend
    # sends; canonicalizing to project.project_id (a UUID) misses hosts
    # registered under the folder id, and a per-poll project load spams
    # "Loaded project" every 2s on the event loop.
    route_module = _load_route_module(monkeypatch)
    seen = {}

    async def fake_get_widget_state(project_id, source_node_id="", host_id=""):
        seen["project_id"] = project_id
        return {"ok": True, "values": {}, "state": {}}

    def fail_load(_request, **_kwargs):
        raise AssertionError("widget_state must not load the project from disk")

    monkeypatch.setattr(route_module, "get_widget_state", fake_get_widget_state)
    monkeypatch.setattr(route_module, "_load_project_from_request", fail_load)

    handler = _route_handler(route_module, "GET", "/sonder-editor/project/{project_id}/widget_state")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "folder-name"},
        query={"host_id": "host-1"},
    )))

    assert response.status == 200
    assert seen["project_id"] == "folder-name"


def test_widget_state_put_uses_raw_route_id_without_project_load(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    seen = {}

    async def fake_update_widget_state(project_id, source_node_id, session_id, values, host_id=""):
        seen["project_id"] = project_id
        seen["values"] = values
        return {"ok": True}

    def fail_load(_request, **_kwargs):
        raise AssertionError("widget_state must not load the project from disk")

    monkeypatch.setattr(route_module, "update_widget_state", fake_update_widget_state)
    monkeypatch.setattr(route_module, "_load_project_from_request", fail_load)

    handler = _route_handler(route_module, "PUT", "/sonder-editor/project/{project_id}/widget_state")
    response = asyncio.run(handler(DummyRequest(
        method="PUT",
        match_info={"project_id": "folder-name"},
        body={
            "host_id": "host-1",
            "session_id": "tab-1",
            "values": {"scene_id": "scene-a"},
        },
    )))

    assert response.status == 200
    assert seen["project_id"] == "folder-name"
    assert seen["values"] == {"scene_id": "scene-a"}


def test_render_timeline_status_rejects_empty_path_query(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    handler = _route_handler(route_module, "GET", "/sonder-editor/project/{project_id}/render_timeline/{job_id}")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project", "job_id": "job-1"},
        query={"path": ""},
    )))

    assert response.status == 400


def test_render_timeline_status_rejects_cross_project_job(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project = TimelineProject(project_dir=str(tmp_path / "project-a"), project_id="project-a")
    job = SimpleNamespace(
        job_id="job-1",
        project_id="project-a",
        project_dir=str(tmp_path / "project-b"),
        status="running",
        phase="queued",
        public_status=lambda: {"job_id": "job-1", "status": "running", "phase": "queued"},
    )
    manager = SimpleNamespace(get=lambda _job_id: job)
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda _request: project)
    monkeypatch.setattr(route_module, "_TIMELINE_EXPORTS", manager)

    handler = _route_handler(route_module, "GET", "/sonder-editor/project/{project_id}/render_timeline/{job_id}")
    response = asyncio.run(handler(DummyRequest(match_info={"project_id": "project-a", "job_id": "job-1"})))

    assert response.status == 404


def test_render_timeline_cancel_rejects_cross_project_job_without_cancelling(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project = TimelineProject(project_dir=str(tmp_path / "project-a"), project_id="project-a")
    job = SimpleNamespace(
        job_id="job-1",
        project_id="project-a",
        project_dir=str(tmp_path / "project-b"),
        status="running",
        phase="queued",
        public_status=lambda: {"job_id": "job-1", "status": "running", "phase": "queued"},
    )
    manager = SimpleNamespace(get=lambda _job_id: job, cancelled=False)

    def cancel(_job_id):
        manager.cancelled = True
        return job

    manager.cancel = cancel
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda _request: project)
    monkeypatch.setattr(route_module, "_TIMELINE_EXPORTS", manager)

    handler = _route_handler(route_module, "POST", "/sonder-editor/project/{project_id}/render_timeline/{job_id}/cancel")
    response = asyncio.run(handler(DummyRequest(match_info={"project_id": "project-a", "job_id": "job-1"})))

    assert response.status == 404
    assert manager.cancelled is False


def test_static_route_rejects_symlink_escape_when_supported(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    fake_server_dir = tmp_path / "server"
    fake_web_dir = tmp_path / "web"
    external_dir = tmp_path / "external"
    fake_server_dir.mkdir()
    fake_web_dir.mkdir()
    external_dir.mkdir()
    external_file = external_dir / "secret.js"
    external_file.write_text("secret", encoding="utf-8")
    try:
        (fake_web_dir / "linked.js").symlink_to(external_file)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available in this environment")
    monkeypatch.setattr(route_module, "__file__", str(fake_server_dir / "routes.py"))

    handler = _route_handler(route_module, "GET", "/sonder-editor/static/{filename:.*}")
    response = asyncio.run(handler(DummyRequest(match_info={"filename": "linked.js"})))

    assert response.status == 400
