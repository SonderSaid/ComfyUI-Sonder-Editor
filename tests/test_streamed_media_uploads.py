import asyncio
import importlib
import os
from types import SimpleNamespace
from pathlib import Path

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

import server
from server import routes
from server import upload_streaming
from server.project_manager import ProjectVersionConflict, load_project, save_project
from server.timeline_state import Asset, TimelineProject


def _valid_video_metadata():
    return {
        "width": 1280,
        "height": 720,
        "frame_count": 48,
        "fps": 24.0,
        "duration_sec": 2.0,
        "sample_rate": 48000,
        "has_audio": True,
    }


def _saved_project(tmp_path: Path, *, asset=None):
    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    (project_dir / "cache" / "thumbnails").mkdir(parents=True)
    project = TimelineProject(project_dir=str(project_dir))
    if asset is not None:
        project.assets.append(asset)
    save_project(project)
    return project


def _reload_routes(monkeypatch):
    fake_prompt_server = SimpleNamespace(instance=SimpleNamespace(routes=web.RouteTableDef()))
    monkeypatch.setattr(server, "PromptServer", fake_prompt_server, raising=False)
    return importlib.reload(routes)


def _route_handler(route_module, method, path):
    for route in route_module.routes:
        if route.method == method and route.path == path:
            return route.handler
    raise AssertionError(f"Route not found: {method} {path}")


def test_manual_multipart_streaming_bypasses_aiohttp_client_max_size(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_streaming, "UPLOAD_DISK_RESERVE_BYTES", 0)
    destination = tmp_path / "media"
    destination.mkdir()
    observed = {}

    async def handler(request):
        async with upload_streaming.receive_project_upload(
            request,
            str(destination),
            allowed_text_fields={"folder"},
        ) as upload:
            observed.update(
                size=upload.size,
                filename=upload.filename,
                folder=upload.fields.get("folder"),
                read_bytes=request._read_bytes,
                staged_path=upload.path,
            )
            assert Path(upload.path).read_bytes() == b"x" * 4096
        return web.json_response({"ok": True})

    async def scenario():
        app = web.Application(client_max_size=64)
        app.router.add_post("/upload", handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            form = FormData()
            form.add_field("folder", "References")
            form.add_field("file", b"x" * 4096, filename="very-long.mov", content_type="video/quicktime")
            response = await client.post("/upload", data=form)
            assert response.status == 200
        finally:
            await client.close()

    asyncio.run(scenario())
    assert observed == {
        "size": 4096,
        "filename": "very-long.mov",
        "folder": "References",
        "read_bytes": None,
        "staged_path": observed["staged_path"],
    }
    assert not Path(observed["staged_path"]).exists()


def test_actual_import_route_streams_above_app_limit(tmp_path, monkeypatch):
    route_module = _reload_routes(monkeypatch)
    monkeypatch.setattr(upload_streaming, "UPLOAD_DISK_RESERVE_BYTES", 0)
    project = _saved_project(tmp_path)
    monkeypatch.setattr(
        route_module,
        "_load_project_from_request",
        lambda _request, **_kwargs: load_project(project.project_dir),
    )
    monkeypatch.setattr(route_module, "_extract_asset_media_metadata", lambda *_args, **_kwargs: _valid_video_metadata())
    monkeypatch.setattr(route_module, "_regenerate_thumbnail_if_current", lambda *_args, **_kwargs: None)
    handler = _route_handler(route_module, "POST", "/sonder-editor/project/{project_id}/assets/import")

    async def scenario():
        app = web.Application(client_max_size=64)
        app.router.add_post("/sonder-editor/project/{project_id}/assets/import", handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            form = FormData()
            form.add_field("folder", "References")
            form.add_field("file", b"v" * 4096, filename="clip.mp4", content_type="video/mp4")
            response = await client.post("/sonder-editor/project/project/assets/import", data=form)
            assert response.status == 201
            payload = await response.json()
            assert payload["folder"] == "References"
            return payload
        finally:
            await client.close()

    payload = asyncio.run(scenario())
    assert Path(project.project_dir, payload["path"]).read_bytes() == b"v" * 4096


def test_actual_import_route_returns_507_without_leaking_stage(tmp_path, monkeypatch):
    route_module = _reload_routes(monkeypatch)
    project = _saved_project(tmp_path)
    monkeypatch.setattr(
        route_module,
        "_load_project_from_request",
        lambda _request, **_kwargs: load_project(project.project_dir),
    )

    def reject_storage(*_args, **_kwargs):
        raise upload_streaming.UploadRequestError(507, "reserved storage")

    monkeypatch.setattr(upload_streaming, "_check_disk_space", reject_storage)
    handler = _route_handler(route_module, "POST", "/sonder-editor/project/{project_id}/assets/import")

    async def scenario():
        app = web.Application()
        app.router.add_post("/sonder-editor/project/{project_id}/assets/import", handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            form = FormData()
            form.add_field("file", b"video", filename="clip.mp4")
            response = await client.post("/sonder-editor/project/project/assets/import", data=form)
            assert response.status == 507
            assert (await response.json())["error"] == "reserved storage"
        finally:
            await client.close()

    asyncio.run(scenario())
    staging_dir = Path(project.project_dir) / "media" / upload_streaming.UPLOAD_STAGING_DIRNAME
    assert not list(staging_dir.glob("*.part.*"))


def test_streaming_preserves_extension_after_long_filename_sanitization(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_streaming, "UPLOAD_DISK_RESERVE_BYTES", 0)
    destination = tmp_path / "media"
    destination.mkdir()
    observed = {}

    async def handler(request):
        async with upload_streaming.receive_project_upload(request, str(destination), allowed_text_fields=set()) as upload:
            observed["filename"] = upload.filename
            observed["staging_name"] = os.path.basename(upload.path)
        return web.Response()

    async def scenario():
        app = web.Application(client_max_size=32)
        app.router.add_post("/upload", handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            form = FormData()
            form.add_field("file", b"video", filename=("a" * 300) + ".MP4")
            response = await client.post("/upload", data=form)
            assert response.status == 200
        finally:
            await client.close()

    asyncio.run(scenario())
    assert observed["filename"].endswith(".mp4")
    assert len(observed["filename"]) <= 120
    assert observed["staging_name"].endswith(".part.mp4")


def test_streaming_rejects_oversized_aggregate_text_and_cleans_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_streaming, "UPLOAD_DISK_RESERVE_BYTES", 0)
    monkeypatch.setattr(upload_streaming, "UPLOAD_TEXT_BYTES", 8)
    destination = tmp_path / "media"
    destination.mkdir()

    async def handler(request):
        try:
            async with upload_streaming.receive_project_upload(
                request,
                str(destination),
                allowed_text_fields={"folder"},
            ):
                pass
        except upload_streaming.UploadRequestError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        return web.Response()

    async def scenario():
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_post("/upload", handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            form = FormData()
            form.add_field("file", b"video", filename="clip.mp4")
            form.add_field("folder", "0123456789")
            response = await client.post("/upload", data=form)
            assert response.status == 413
        finally:
            await client.close()

    asyncio.run(scenario())
    staging_dir = destination / upload_streaming.UPLOAD_STAGING_DIRNAME
    assert not list(staging_dir.glob("*.part.*"))


def test_streamed_import_commits_without_comfy_input_copy(tmp_path, monkeypatch):
    project = _saved_project(tmp_path)
    staging_dir = Path(project.project_dir) / "media" / upload_streaming.UPLOAD_STAGING_DIRNAME
    staging_dir.mkdir()
    staged = staging_dir / ("0" * 32 + ".part.mp4")
    staged.write_bytes(b"new-video")
    monkeypatch.setattr(routes, "_extract_asset_media_metadata", lambda *_args, **_kwargs: _valid_video_metadata())

    committed, asset = routes._streamed_import_commit(
        project.project_dir,
        str(staged),
        "My Clip.mp4",
        "References",
    )

    assert asset.asset_id == committed.assets[0].asset_id
    assert asset.folder == "References"
    assert asset.path.replace("\\", "/").startswith("media/")
    assert Path(project.project_dir, asset.path).read_bytes() == b"new-video"
    assert not staged.exists()


def test_media_snapshot_prunes_reserved_upload_staging(tmp_path):
    project = _saved_project(tmp_path)
    media_dir = Path(project.project_dir) / "media"
    (media_dir / "visible.mp4").write_bytes(b"video")
    staging_dir = media_dir / upload_streaming.UPLOAD_STAGING_DIRNAME
    staging_dir.mkdir()
    (staging_dir / ("2" * 32 + ".part.mp4")).write_bytes(b"partial")

    snapshot = routes._project_media_snapshot(project)

    assert "media/visible.mp4" in snapshot
    assert all(upload_streaming.UPLOAD_STAGING_DIRNAME not in path for path in snapshot)


def test_streamed_import_retry_upgrades_sync_placeholder(tmp_path, monkeypatch):
    project = _saved_project(tmp_path)
    staging_dir = Path(project.project_dir) / "media" / upload_streaming.UPLOAD_STAGING_DIRNAME
    staging_dir.mkdir()
    staged = staging_dir / ("3" * 32 + ".part.mp4")
    staged.write_bytes(b"new-video")
    monkeypatch.setattr(routes, "_extract_asset_media_metadata", lambda *_args, **_kwargs: _valid_video_metadata())
    real_save = routes.save_project
    calls = {"count": 0}

    def sync_then_conflict(project_to_save, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            concurrent = load_project(project_to_save.project_dir)
            imported = project_to_save.assets[-1]
            concurrent.assets.append(Asset(
                asset_id="sync-id",
                name=os.path.basename(imported.path),
                asset_type="video",
                path=imported.path,
            ))
            real_save(concurrent, expected_modified_at=concurrent.modified_at)
            raise ProjectVersionConflict(
                project_dir=project_to_save.project_dir,
                expected_modified_at=kwargs.get("expected_modified_at", ""),
                actual_modified_at=concurrent.modified_at,
                current_data=concurrent.to_dict(),
            )
        return real_save(project_to_save, **kwargs)

    monkeypatch.setattr(routes, "save_project", sync_then_conflict)
    committed, asset = routes._streamed_import_commit(
        project.project_dir,
        str(staged),
        "clip.mp4",
        "References",
    )

    assert calls["count"] == 2
    assert len(committed.assets) == 1
    assert asset.asset_id == "sync-id"
    assert asset.folder == "References"
    assert asset.media_probe_signature


def test_streamed_same_path_replace_retries_conflict_and_preserves_metadata(tmp_path, monkeypatch):
    asset = Asset(
        asset_id="asset-1",
        name="Custom Name",
        asset_type="video",
        path=os.path.join("media", "clip.mp4"),
        folder="References",
        favorite=True,
    )
    project = _saved_project(tmp_path, asset=asset)
    source = Path(project.project_dir) / asset.path
    source.write_bytes(b"old-video")
    project = load_project(project.project_dir)
    project.assets[0].media_probe_signature = routes._media_probe_signature(str(source))
    save_project(project)
    initial_signature = routes._media_probe_signature(str(source))

    staging_dir = source.parent / upload_streaming.UPLOAD_STAGING_DIRNAME
    staging_dir.mkdir()
    staged = staging_dir / ("1" * 32 + ".part.mp4")
    staged.write_bytes(b"new-video")
    monkeypatch.setattr(routes, "_extract_asset_media_metadata", lambda *_args, **_kwargs: _valid_video_metadata())
    real_save = routes.save_project
    calls = {"count": 0}

    def conflict_once(project_to_save, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ProjectVersionConflict(
                project_dir=project_to_save.project_dir,
                expected_modified_at=kwargs.get("expected_modified_at", ""),
                actual_modified_at="newer",
                current_data={},
            )
        return real_save(project_to_save, **kwargs)

    monkeypatch.setattr(routes, "save_project", conflict_once)
    committed, replaced = routes._streamed_replace_commit(
        project.project_dir,
        "asset-1",
        "video",
        asset.path,
        initial_signature,
        str(staged),
        "replacement.mp4",
    )

    assert calls["count"] == 2
    assert source.read_bytes() == b"new-video"
    assert replaced.asset_id == "asset-1"
    assert replaced.name == "Custom Name"
    assert replaced.folder == "References"
    assert replaced.favorite is True
    assert committed.get_asset("asset-1").media_probe_signature == routes._media_probe_signature(str(source))
    assert not list(staging_dir.glob("*.rollback.*"))


def test_frontend_import_and_replace_post_directly_to_sonder():
    source = Path(__file__).parents[1] / "web" / "js" / "editor_widget.js"
    text = source.read_text(encoding="utf-8")
    helper_region = text[text.index("export async function importFileIntoProject"):text.index("const GALLERY_HEIGHT")]
    assert 'formData.append("file", file, file.name)' in helper_region
    assert 'formData.append("folder", folder)' in helper_region
    assert 'api.apiURL("/upload/image")' not in helper_region
    assert "uploadFileToComfyInput" not in text
