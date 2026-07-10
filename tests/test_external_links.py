"""Regression coverage for opt-in external project-link trust mode."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

import server
from server import external_links, routes
from server.path_security import (
    path_within,
    project_media_path,
    project_media_root,
    resolve_under_root,
)
from server.project_manager import create_project, list_projects
from server.timeline_state import Asset, TimelineProject


@pytest.fixture(autouse=True)
def _reset_external_link_cache():
    external_links._flag_cache.update(value=False, expires=0.0)
    yield
    external_links._flag_cache.update(value=False, expires=0.0)


def _make_symlink(link_path, target):
    try:
        link_path.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available in this environment")


class _DummyRequest:
    def __init__(self, *, match_info=None, query=None, body=None):
        self.match_info = match_info or {}
        self.query = query or {}
        self._body = body

    async def json(self):
        return self._body


def _route_handler(route_module, method, path):
    for route in route_module.routes:
        if route.method == method and route.path == path:
            return route.handler
    raise AssertionError(f"Route not found: {method} {path}")


def _response_json(response):
    return json.loads(response.body.decode("utf-8"))


def _load_route_module(monkeypatch):
    fake_prompt_server = SimpleNamespace(instance=SimpleNamespace(routes=web.RouteTableDef()))
    monkeypatch.setattr(server, "PromptServer", fake_prompt_server, raising=False)
    return importlib.reload(routes)


def test_server_setting_roundtrip_and_cache_invalidation(tmp_path, monkeypatch):
    user_dir = tmp_path / "user"
    fake_folder_paths = SimpleNamespace(get_user_directory=lambda: str(user_dir))
    monkeypatch.setitem(sys.modules, "folder_paths", fake_folder_paths)

    assert external_links.is_enabled() is False
    external_links.set_enabled(True)

    settings_path = user_dir / "sonder-editor" / "settings.json"
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "allow_external_project_links": True,
    }
    assert external_links.is_enabled() is True

    external_links.set_enabled(False)
    assert external_links.is_enabled() is False


def test_server_settings_and_link_routes_enforce_opt_in(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    user_dir = tmp_path / "user"
    output_dir = tmp_path / "output"
    fake_folder_paths = SimpleNamespace(
        get_user_directory=lambda: str(user_dir),
        get_output_directory=lambda: str(output_dir),
    )
    monkeypatch.setitem(sys.modules, "folder_paths", fake_folder_paths)

    get_settings = _route_handler(route_module, "GET", "/sonder-editor/server-settings")
    update_settings = _route_handler(route_module, "PUT", "/sonder-editor/server-settings")
    link_project = _route_handler(route_module, "POST", "/sonder-editor/projects/link")

    assert _response_json(asyncio.run(get_settings(_DummyRequest()))) == {
        "allow_external_project_links": False,
    }
    blocked = asyncio.run(link_project(_DummyRequest(body={"path": str(tmp_path / "missing")})))
    assert blocked.status == 403
    assert "Enable 'Allow external project links'" in _response_json(blocked)["error"]

    updated = asyncio.run(update_settings(_DummyRequest(body={"allow_external_project_links": True})))
    assert updated.status == 200
    assert _response_json(updated) == {"allow_external_project_links": True}


def test_trust_mode_keeps_junction_spelling_and_off_remains_strict(tmp_path, monkeypatch):
    root = tmp_path / "sonder-projects"
    root.mkdir()
    external_project = tmp_path / "external-project"
    asset = external_project / "media" / "clip.mp4"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"linked media")
    (external_project / "project.json").write_text("{}", encoding="utf-8")
    link = root / "linked-project"
    _make_symlink(link, external_project)

    rel = "linked-project/media/clip.mp4"
    monkeypatch.setattr(external_links, "is_enabled", lambda: False)
    assert path_within(str(root), str(link)) is False
    assert resolve_under_root(str(root), rel, must_exist=True, log=False) == ""

    monkeypatch.setattr(external_links, "is_enabled", lambda: True)
    resolved = resolve_under_root(str(root.resolve()), rel, must_exist=True, log=False)
    assert resolved == os.path.abspath(root.resolve() / rel)
    assert path_within(str(root.resolve()), resolved) is True
    assert os.path.samefile(resolved, asset)


def test_project_subroots_and_snapshot_follow_asset_link_only_when_enabled(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    external_file = tmp_path / "external.mp4"
    external_file.write_bytes(b"external media")
    _make_symlink(media_dir / "linked.mp4", external_file)
    project = TimelineProject(project_dir=str(project_dir))

    monkeypatch.setattr(external_links, "is_enabled", lambda: False)
    assert project_media_path(project, "media/linked.mp4", must_exist=True, log=False) == ""
    assert "media/linked.mp4" not in routes._project_media_snapshot(project)

    monkeypatch.setattr(external_links, "is_enabled", lambda: True)
    resolved = project_media_path(project, "media/linked.mp4", must_exist=True, log=False)
    assert resolved == os.path.abspath(media_dir / "linked.mp4")
    snapshot = routes._project_media_snapshot(project)
    assert snapshot["media/linked.mp4"]["size"] == len(b"external media")
    assert snapshot["media/linked.mp4"]["path"] == str(media_dir / "linked.mp4")
    assert project_media_root(project) == os.path.abspath(media_dir)


def test_same_extension_replace_swaps_a_link_without_overwriting_external_media(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    external_file = tmp_path / "external.png"
    external_file.write_bytes(b"external original")
    linked_asset = media_dir / "linked.png"
    _make_symlink(linked_asset, external_file)
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "replacement.png").write_bytes(b"replacement")
    fake_folder_paths = SimpleNamespace(
        get_input_directory=lambda: str(input_dir),
        get_output_directory=lambda: str(tmp_path / "output"),
    )
    monkeypatch.setitem(sys.modules, "folder_paths", fake_folder_paths)
    monkeypatch.setattr(external_links, "is_enabled", lambda: True)
    monkeypatch.setattr(
        routes,
        "_extract_asset_media_metadata",
        lambda *_args, **_kwargs: {
            "width": 1,
            "height": 1,
            "frame_count": 1,
            "fps": 0.0,
            "duration_sec": 0.0,
            "sample_rate": 0,
            "has_audio": False,
        },
    )
    asset = Asset(asset_id="asset-1", asset_type="image", path="media/linked.png")
    project = TimelineProject(project_dir=str(project_dir), assets=[asset])

    routes._replace_project_asset(project, asset, "replacement.png")

    assert not os.path.islink(linked_asset)
    assert linked_asset.read_bytes() == b"replacement"
    assert external_file.read_bytes() == b"external original"


def test_list_projects_exposes_link_only_when_enabled(tmp_path, monkeypatch):
    base_dir = tmp_path / "sonder-projects"
    external_base = tmp_path / "external"
    linked_project = create_project("External Project", base_dir=str(external_base))
    base_dir.mkdir()
    _make_symlink(base_dir / "linked", Path(linked_project.project_dir))

    monkeypatch.setattr(external_links, "is_enabled", lambda: False)
    assert list_projects(str(base_dir)) == []

    monkeypatch.setattr(external_links, "is_enabled", lambda: True)
    projects = list_projects(str(base_dir))
    assert len(projects) == 1
    assert projects[0]["linked"] is True
    assert projects[0]["path"] == os.path.abspath(base_dir / "linked")


def test_create_and_remove_project_link_only_removes_the_reparse_point(tmp_path, monkeypatch):
    output_dir = tmp_path / "output"
    user_dir = tmp_path / "user"
    external_base = tmp_path / "external"
    target = create_project("Outside", base_dir=str(external_base))
    fake_folder_paths = SimpleNamespace(
        get_output_directory=lambda: str(output_dir),
        get_user_directory=lambda: str(user_dir),
    )
    monkeypatch.setitem(sys.modules, "folder_paths", fake_folder_paths)
    monkeypatch.setattr(external_links, "is_enabled", lambda: True)

    linked = external_links.create_project_link(target.project_dir)
    root = output_dir / "sonder-projects"
    link_path = root / linked["name"]
    assert external_links.is_reparse_child(str(root), linked["name"])
    assert (link_path / "project.json").is_file()

    assert external_links.remove_project_link(linked["name"]) is True
    assert not os.path.lexists(link_path)
    assert (external_base / "Outside" / "project.json").is_file()


def test_link_name_rejects_reserved_windows_devices(tmp_path, monkeypatch):
    monkeypatch.setattr(external_links, "is_enabled", lambda: True)
    with pytest.raises(external_links.LinkError, match="reserved Windows device"):
        external_links._validate_link_name("CON.json")
