import asyncio
import importlib
import json
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


def test_sequential_imports_advertise_committed_versions(tmp_path, monkeypatch):
    route_module = _reload_routes(monkeypatch)
    project = _saved_project(tmp_path)
    monkeypatch.setattr(route_module, "_configured_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(upload_streaming, "UPLOAD_DISK_RESERVE_BYTES", 0)
    monkeypatch.setattr(route_module, "_extract_asset_media_metadata", lambda *_a, **_kw: _valid_video_metadata())
    monkeypatch.setattr(route_module, "_regenerate_thumbnail_if_current", lambda *_a, **_kw: None)

    async def scenario():
        app = web.Application(middlewares=[
            route_module._project_error_middleware,
            route_module._project_version_header_middleware,
        ])
        path = "/sonder-editor/project/{project_id}/assets/import"
        app.router.add_post(path, _route_handler(route_module, "POST", path))
        async with TestClient(TestServer(app)) as client:
            version = ""
            observations = []
            for index in range(8):
                before = load_project(project.project_dir).modified_at
                form = FormData()
                form.add_field("file", b"video", filename=f"clip-{index}.mp4")
                response = await client.post(
                    "/sonder-editor/project/project/assets/import", data=form,
                    headers={"If-Match": version} if version else {},
                )
                await response.read()
                header = response.headers.get("X-Sonder-Project-Modified-At")
                after = load_project(project.project_dir).modified_at
                observations.append((response.status, before, header, after))
                assert header, observations
                version = max(version, header)
            assert [row[0] for row in observations] == [201] * 8, observations
            assert all(header == after for _, _, header, after in observations), observations
            assert len(load_project(project.project_dir).assets) == 8

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["import", "replace"])
@pytest.mark.parametrize("stale", [False, True])
def test_streamed_routes_return_committed_header_with_stale_client(tmp_path, monkeypatch, operation, stale):
    route_module = _reload_routes(monkeypatch)
    asset = Asset(asset_id="asset-1", name="clip.mp4", asset_type="video", path="media/clip.mp4")
    project = _saved_project(tmp_path, asset=asset)
    Path(project.project_dir, asset.path).write_bytes(b"old")
    monkeypatch.setattr(route_module, "_configured_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(upload_streaming, "UPLOAD_DISK_RESERVE_BYTES", 0)
    monkeypatch.setattr(route_module, "_extract_asset_media_metadata", lambda *_a, **_kw: _valid_video_metadata())
    monkeypatch.setattr(route_module, "_regenerate_thumbnail_if_current", lambda *_a, **_kw: None)
    before = project.modified_at

    async def scenario():
        app = web.Application(middlewares=[route_module._project_error_middleware,
                                          route_module._project_version_header_middleware])
        suffix = "import" if operation == "import" else "{asset_id}/replace"
        path = "/sonder-editor/project/{project_id}/assets/" + suffix
        app.router.add_post(path, _route_handler(route_module, "POST", path))
        async with TestClient(TestServer(app)) as client:
            form = FormData()
            form.add_field("file", b"replacement", filename="new.mp4")
            response = await client.post(path.replace("{project_id}", "project").replace("{asset_id}", "asset-1"),
                                         data=form, headers={"If-Match": "2000-01-01T00:00:00" if stale else before})
            assert response.status == (201 if operation == "import" else 200), await response.text()
            saved = load_project(project.project_dir)
            assert saved.modified_at != before
            assert response.headers["X-Sonder-Project-Modified-At"] == saved.modified_at
            assert response.headers["X-Sonder-Project-Id"] == saved.project_id
            payload = await response.json()
            result = payload if operation == "import" else payload["asset"]
            assert Path(project.project_dir, result["path"]).read_bytes() == b"replacement"

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["backup_cleanup", "post_commit_exception", "save"])
def test_replace_rollback_authority_ends_at_commit(tmp_path, monkeypatch, failure):
    asset = Asset(asset_id="asset-1", name="clip.mp4", asset_type="video", path="media/clip.mp4")
    project = _saved_project(tmp_path, asset=asset)
    source = Path(project.project_dir, asset.path)
    source.write_bytes(b"old-video")
    initial_signature = routes._media_probe_signature(str(source))
    staging = source.parent / upload_streaming.UPLOAD_STAGING_DIRNAME
    staging.mkdir()
    staged = staging / ("4" * 32 + ".part.mp4")
    staged.write_bytes(b"new-video-with-different-length")
    monkeypatch.setattr(routes, "_extract_asset_media_metadata", lambda *_a, **_kw: _valid_video_metadata())
    real_remove = os.remove
    real_save = routes.save_project
    cleanup_attempts = []

    def remove(path, *args, **kwargs):
        if ".rollback." in str(path):
            cleanup_attempts.append(str(path))
            if failure == "backup_cleanup":
                raise PermissionError("injected backup cleanup failure")
            if failure == "post_commit_exception":
                raise RuntimeError("injected post-commit failure")
        return real_remove(path, *args, **kwargs)

    def save(project, **kwargs):
        if failure == "save":
            raise OSError("injected save failure")
        return real_save(project, **kwargs)

    monkeypatch.setattr(routes.os, "remove", remove)
    monkeypatch.setattr(routes, "save_project", save)

    def commit():
        return routes._streamed_replace_commit(project.project_dir, asset.asset_id, "video", asset.path,
                                              initial_signature, str(staged), "new.mp4")

    if failure == "backup_cleanup":
        commit()
    else:
        with pytest.raises(RuntimeError if failure == "post_commit_exception" else OSError):
            commit()
    saved = load_project(project.project_dir)
    if failure == "save":
        assert source.read_bytes() == b"old-video"
        assert staged.read_bytes() == b"new-video-with-different-length"
        assert saved.modified_at == project.modified_at
    else:
        assert cleanup_attempts
        assert source.read_bytes() == b"new-video-with-different-length"
        assert saved.modified_at != project.modified_at
        assert saved.get_asset(asset.asset_id).media_probe_signature == routes._media_probe_signature(str(source))
        assert not staged.exists()


@pytest.mark.parametrize("host_kind", ["fullscreen", "dormant"])
def test_upload_errors_keep_codes_and_name_failed_file(host_kind):
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser helper tests")
    source = (Path(__file__).parents[1] / "web/js/editor_widget.js").read_text(encoding="utf-8")
    helpers = source[source.index("const PROJECT_ERROR_MESSAGES"):source.index("export function buildProjectAssetViewURL")]
    uploads = source[source.index("export async function importFileIntoProject"):source.index("// ── Constants")]
    method = source[source.index("    async _importFilesWithProgressWithinGesture("):source.index("    async _importFile(file")]
    if host_kind == "dormant":
        controller = (Path(__file__).parents[1] / "web/js/editor_node_controller.js").read_text(encoding="utf-8")
        start = controller.index("    async importFiles(")
        end = controller.index("\n    }", start) + len("\n    }")
        method = controller[start:end]
    script = helpers.replace("export ", "") + uploads.replace("export ", "") + "\nconst host = {" + method + "};\n" + r'''
const assert = (await import("node:assert/strict")).default;
const api = {apiURL: (url) => url};
const markProjectAssetMutation = () => {};
const withEditorMutationDiagnostics = (init) => init;
let requests = 0;
globalThis.fetch = async () => {
    requests++;
    return new Response(JSON.stringify({error:"project_version_conflict",code:"project_version_conflict"}), {status:409});
};
const file = new Blob(["bad"]); file.name = "failed clip.mp4";
for (const action of [() => importFileIntoProject("project", file), () => replaceAssetInProject("project", "asset", file)]) {
    await assert.rejects(action, e => e.code === "project_version_conflict" && e.status === 409
        && e.message.includes("project changed") && !e.message.includes("project_version_conflict"));
}
assert.equal(requests, 2); // No re-upload/retry layer.
const raw = await readResponseError(new Response("Disk full", {status:507}));
assert.equal(raw.message, "Disk full"); assert.equal(raw.status, 507);
assert.equal(importFailureMessage({file, error:raw}), "failed clip.mp4: Disk full");
assert.match(projectErrorMessage({code:"project_version_conflict"}, "fallback", "preview"), /preview/);
assert.match(projectErrorMessage({code:"project_version_conflict"}, "fallback", "history"), /scene history/);
let resolved; let refreshed = 0;
const notifyProgress = () => ({update() {}, resolve(value) { resolved = value; }});
host.projectDir = "project"; host._fetchAssets = async () => { refreshed++; };
globalThis.fetch = async () => ++requests % 2
    ? new Response("{}", {status:201})
    : new Response(JSON.stringify({error:"Cannot probe media"}), {status:400});
if (host.importFiles) {
    host.state = {projectDir: "project"};
    host._invalidateModules = () => {};
    host._reloadExpandedModuleIfNeeded = () => {};
    host._refreshAfterAssetMutation = host._fetchAssets;
    await host.importFiles([file, file]);
} else {
    await host._importFilesWithProgressWithinGesture({}, [file, file]);
}
assert.equal(refreshed, 1);
assert.equal(resolved.tier, "warning");
assert.equal(resolved.message, "Imported 1 of 2 files. failed clip.mp4: Cannot probe media");
console.log(JSON.stringify({ok:true}));
'''
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True}


# ---------------------------------------------------------------------------
# Both multipart branches build their response payload with `_asset_payload`, inside
# the `try`, and `_asset_payload` reads `generation_params` — the attribute whose lazy
# hydration is a component read. Their `except (ValueError, MediaProbeError)` arm then
# reported a durable-storage failure as a 400 validation error and rendered its
# message. These need the real-app harness: the payload build sits inside
# `async with receive_project_upload(...)`, which a DummyRequest cannot drive.
# ---------------------------------------------------------------------------

STORAGE_SENTINEL_PATH = r"C:\__SECRET_ABSOLUTE_PATH__\state\ab.json"


@pytest.mark.parametrize("operation", ["import", "replace"])
def test_streamed_routes_report_unreadable_storage_as_a_server_error(tmp_path, monkeypatch, operation):
    from server.project_storage import ProjectStorageError

    route_module = _reload_routes(monkeypatch)
    asset = Asset(asset_id="asset-1", name="clip.mp4", asset_type="video", path="media/clip.mp4")
    project = _saved_project(tmp_path, asset=asset)
    Path(project.project_dir, asset.path).write_bytes(b"old")
    monkeypatch.setattr(route_module, "_configured_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(upload_streaming, "UPLOAD_DISK_RESERVE_BYTES", 0)
    monkeypatch.setattr(route_module, "_extract_asset_media_metadata", lambda *_a, **_kw: _valid_video_metadata())
    monkeypatch.setattr(route_module, "_regenerate_thumbnail_if_current", lambda *_a, **_kw: None)

    def unreadable_provenance(*_args, **_kwargs):
        raise ProjectStorageError(
            f"provenance_ab: missing or unreadable component: {STORAGE_SENTINEL_PATH}")

    # The real raise point is an attribute read inside this call; patching the payload
    # builder puts the failure exactly where lazy hydration puts it, without having to
    # stage a component that disappears mid-request.
    monkeypatch.setattr(route_module, "_asset_payload", unreadable_provenance)

    async def scenario():
        app = web.Application(middlewares=[route_module._project_error_middleware,
                                           route_module._project_version_header_middleware])
        suffix = "import" if operation == "import" else "{asset_id}/replace"
        path = "/sonder-editor/project/{project_id}/assets/" + suffix
        app.router.add_post(path, _route_handler(route_module, "POST", path))
        async with TestClient(TestServer(app)) as client:
            form = FormData()
            form.add_field("file", b"payload", filename="new.mp4")
            response = await client.post(
                path.replace("{project_id}", "project").replace("{asset_id}", "asset-1"),
                data=form)
            text = await response.text()
            assert response.status == 500, text
            body = json.loads(text)
            assert body["code"] == "project_storage_unreadable"
            assert body["error"] == route_module.PROJECT_STORAGE_UNREADABLE_MESSAGE
            # Decoded, not raw: JSON escapes the backslashes in a Windows path, so a
            # substring check against the wire text passes while the path is in it.
            assert all(STORAGE_SENTINEL_PATH not in str(value) for value in body.values())

    asyncio.run(scenario())
