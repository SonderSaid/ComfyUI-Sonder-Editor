import asyncio
import importlib
import logging
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from aiohttp import web

import server
from server.path_security import (
    normalize_project_relative_path,
    path_within,
    project_bridge_path,
    project_bridge_root,
    project_cache_path,
    project_media_path,
    project_media_root,
    project_root,
    resolve_comfy_input_path,
    resolve_existing_project_path,
    resolve_project_path,
    resolve_static_path,
)
from server.timeline_state import Asset, TimelineProject
import server.routes as routes
from server.timeline_renderer import _resolve_project_media_path


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


@pytest.mark.parametrize(
    "raw_path",
    [
        r"C:\Windows\win.ini",
        "C:media\\clip.mp4",
        r"\Windows\win.ini",
        "/etc/passwd",
        "../secret.mp4",
        "media/../secret.mp4",
        "media//clip.mp4",
        "media/clip.mp4:stream",
        "media/%2f/clip.mp4",
        "media/%5c/clip.mp4",
    ],
)
def test_project_relative_paths_reject_hostile_forms(raw_path):
    with pytest.raises(ValueError):
        normalize_project_relative_path(raw_path)


def test_resolve_project_path_returns_empty_and_logs_for_hostile_path(tmp_path, caplog):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    caplog.set_level(logging.WARNING, logger="sonder_editor")

    resolved = resolve_project_path(str(project_dir), "../outside.mp4", purpose="test asset")

    assert resolved == ""
    assert "Security quarantine" in caplog.text


def test_asset_abspath_quarantines_absolute_paths_and_delete_skips_external_file(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    external = tmp_path / "external.mp4"
    external.write_bytes(b"external")
    asset = Asset(asset_id="asset-1", path=str(external), asset_type="video")
    project = TimelineProject(project_dir=str(project_dir), assets=[asset])

    assert routes._asset_abspath(project, asset) == ""

    routes._delete_project_asset(project, asset)

    assert external.exists()
    assert project.get_asset("asset-1") is None


def _make_hostile_asset_id_project(tmp_path, *, trashed: bool = False, folder: str = ""):
    project_dir = tmp_path / "project"
    (project_dir / "cache" / "thumbnails").mkdir(parents=True)
    (project_dir / "cache" / "waveforms").mkdir(parents=True)
    external_files = [
        tmp_path / "outside.png",
        tmp_path / "outside_strip.jpg",
        tmp_path / "outside_strip.jpg.json",
        tmp_path / "outside.json",
        tmp_path / "outside_audio.wav",
    ]
    for path in external_files:
        path.write_bytes(b"external")
    hostile_asset_id = os.path.join("..", "..", "..", "outside")
    asset = Asset(
        asset_id=hostile_asset_id,
        path=os.path.join("media", "missing.mp4"),
        asset_type="video",
        folder=folder,
        trashed_at=(datetime.now() - timedelta(days=1)).isoformat() if trashed else "",
    )
    project = TimelineProject(project_dir=str(project_dir), assets=[asset])
    if folder:
        project.metadata["asset_folders"] = [folder]
    return project, asset, external_files


def test_permanent_delete_skips_hostile_asset_id_cache_paths(tmp_path):
    project, asset, external_files = _make_hostile_asset_id_project(tmp_path)

    routes._delete_project_asset(project, asset)

    assert project.get_asset(asset.asset_id) is None
    assert all(path.exists() for path in external_files)


def test_bulk_permanent_delete_skips_hostile_asset_id_cache_paths(tmp_path):
    project, asset, external_files = _make_hostile_asset_id_project(tmp_path)

    for target in list(project.assets):
        routes._delete_project_asset(project, target)

    assert project.get_asset(asset.asset_id) is None
    assert all(path.exists() for path in external_files)


def test_empty_trash_skips_hostile_asset_id_cache_paths(tmp_path):
    project, asset, external_files = _make_hostile_asset_id_project(tmp_path, trashed=True)

    for target in list(routes._project_trashed_assets(project)):
        routes._delete_project_asset(project, target)

    assert project.get_asset(asset.asset_id) is None
    assert all(path.exists() for path in external_files)


def test_trash_purge_skips_hostile_asset_id_cache_paths(tmp_path):
    project, asset, external_files = _make_hostile_asset_id_project(tmp_path, trashed=True)

    changed = routes._purge_expired_trashed_assets(project, retention_days=0)

    assert changed is True
    assert project.get_asset(asset.asset_id) is None
    assert all(path.exists() for path in external_files)


def test_prepare_video_audio_asset_skips_hostile_asset_id_destination(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    (media_dir / "clip.mp4").write_bytes(b"video")
    external_audio = tmp_path / "outside_audio.wav"
    external_audio.write_bytes(b"x" * 2048)
    asset = Asset(
        asset_id=os.path.join("..", "..", "outside"),
        path=os.path.join("media", "clip.mp4"),
        asset_type="video",
    )
    project = TimelineProject(project_dir=str(project_dir), assets=[asset])

    monkeypatch.setattr(routes, "_extract_audio_from_video", lambda *_args, **_kwargs: pytest.fail("extraction should be skipped"))
    monkeypatch.setattr(routes, "_get_audio_duration", lambda *_args, **_kwargs: 0.0)

    assert routes._prepare_video_audio_asset(project, asset) is None
    assert external_audio.exists()


def test_thumbnail_route_does_not_serve_symlinked_cache_escape(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    cache_dir = project_dir / "cache"
    cache_dir.mkdir(parents=True)
    external_dir = tmp_path / "external-thumbnails"
    external_dir.mkdir()
    (external_dir / "asset-1.png").write_bytes(b"external thumbnail")
    try:
        (cache_dir / "thumbnails").symlink_to(external_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available in this environment")
    asset = Asset(asset_id="asset-1", path=os.path.join("media", "missing.png"), asset_type="image")
    project = TimelineProject(project_dir=str(project_dir), assets=[asset])
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda _request: project)
    monkeypatch.setattr(route_module, "ensure_thumbnail", lambda *_args, **_kwargs: pytest.fail("thumbnail generation should be skipped"))

    handler = _route_handler(route_module, "GET", "/sonder-editor/project/{project_id}/thumbnail/{asset_id}")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project", "asset_id": "asset-1"},
        query={"path": str(project_dir)},
    )))

    assert response.status == 404


def test_timeline_renderer_rejects_absolute_source_path(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    external = tmp_path / "external.mp4"
    external.write_bytes(b"external")
    project = TimelineProject(project_dir=str(project_dir))

    assert _resolve_project_media_path(project, str(external)) == ""


def test_timeline_renderer_resolves_contained_project_source(tmp_path):
    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    clip = media_dir / "clip.mp4"
    clip.write_bytes(b"video")
    project = TimelineProject(project_dir=str(project_dir))

    resolved = _resolve_project_media_path(project, os.path.join("media", "clip.mp4"))

    assert resolved
    assert path_within(str(project_dir), resolved)
    assert os.path.samefile(resolved, clip)


def test_shared_future_root_helpers_reject_escape_paths(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    static_root = tmp_path / "static"
    static_root.mkdir()
    input_root = tmp_path / "input"
    input_root.mkdir()

    assert project_root(str(project_dir)) == os.path.realpath(project_dir)
    assert project_cache_path(str(project_dir), "thumbnails/asset.png")
    assert project_bridge_path(str(project_dir), "prompt/node/output.png")
    assert resolve_static_path(str(static_root), "app.js", must_exist=False)
    assert resolve_comfy_input_path("upload.png", input_root=str(input_root), must_exist=False)

    assert project_cache_path(str(project_dir), "../outside.png", log=False) == ""
    assert project_bridge_path(str(project_dir), "../outside.png", log=False) == ""
    assert resolve_static_path(str(static_root), "../outside.js", log=False) == ""
    assert resolve_comfy_input_path("../outside.png", input_root=str(input_root), must_exist=False, log=False) == ""


def test_project_subroot_helpers_reject_in_project_symlink_redirects(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    cache_dir = project_dir / "cache"
    cache_dir.mkdir()
    media_dir = project_dir / "media"
    try:
        media_dir.symlink_to(cache_dir, target_is_directory=True)
        (cache_dir / "bridge_out").symlink_to(project_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available in this environment")

    assert project_media_root(str(project_dir)) == ""
    assert project_media_path(str(project_dir), "clip.png", log=False) == ""
    assert project_bridge_root(str(project_dir)) == ""
    assert project_bridge_path(str(project_dir), "prompt/out.png", log=False) == ""


def test_snapshot_skips_symlink_escape_when_supported(tmp_path):
    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    external_file = external_dir / "outside.mp4"
    external_file.write_bytes(b"external")
    link_path = media_dir / "linked.mp4"
    try:
        link_path.symlink_to(external_file)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available in this environment")

    project = TimelineProject(project_dir=str(project_dir))
    snapshot = routes._project_media_snapshot(project)

    assert "media/linked.mp4" not in snapshot
    assert resolve_existing_project_path(project, os.path.join("media", "linked.mp4"), log=False) == ""
