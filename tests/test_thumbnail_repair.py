import asyncio
import importlib
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from aiohttp import web
import server
import server.routes as routes
import server.thumbnail_service as thumbnail_service
from server.project_manager import load_project, save_project
from server.timeline_state import Asset, TimelineProject


def _project(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    (project_dir / "cache" / "thumbnails").mkdir(parents=True)
    (project_dir / "cache" / "waveforms").mkdir(parents=True)
    return TimelineProject(project_dir=str(project_dir), name="Thumbnail Repair")


def _route_handler(route_module, method, path):
    for route in route_module.routes:
        if route.method == method and route.path == path:
            return route.handler
    raise AssertionError(f"Route not found: {method} {path}")


class _Request:
    def __init__(self, project_id, asset_id):
        self.match_info = {"project_id": project_id, "asset_id": asset_id}
        self.query = {}


def test_thumbnail_generation_is_limited_to_two_process_wide(tmp_path, monkeypatch):
    lock = threading.Lock()
    release = threading.Event()
    two_started = threading.Event()
    active = 0
    maximum = 0

    def generate(_source, output):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                two_started.set()
        assert release.wait(5)
        with open(output, "wb") as handle:
            handle.write(b"thumb")
        with lock:
            active -= 1
        return True

    monkeypatch.setattr(thumbnail_service, "generate_image_thumbnail", generate)
    outputs = [tmp_path / f"thumb-{index}.png" for index in range(3)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(thumbnail_service.ensure_thumbnail, "image", "source.png", str(output))
            for output in outputs
        ]
        assert two_started.wait(5)
        with lock:
            assert active == 2
        release.set()
        assert all(future.result(timeout=5) for future in futures)

    assert maximum == 2
    assert all(output.is_file() for output in outputs)


def test_concurrent_same_asset_regeneration_decodes_once(tmp_path, monkeypatch):
    project = _project(tmp_path)
    source = tmp_path / "project" / "media" / "source.png"
    source.write_bytes(b"source")
    signature = routes._media_probe_signature(str(source))
    asset = Asset(
        asset_id="asset-one",
        asset_type="image",
        path="media/source.png",
        media_probe_signature=signature,
    )
    project.add_asset(asset)
    save_project(project)
    original_modified_at = load_project(project.project_dir).modified_at

    calls = 0
    calls_lock = threading.Lock()

    def generate(_asset_type, _source_path, output_path):
        nonlocal calls
        with calls_lock:
            calls += 1
        with open(output_path, "wb") as handle:
            handle.write(b"thumbnail")
        return True

    monkeypatch.setattr(routes, "ensure_thumbnail", generate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(routes._regenerate_thumbnail_if_current, project.project_dir, asset.asset_id, signature)
            for _ in range(2)
        ]
        for future in futures:
            future.result(timeout=5)

    assert calls == 1
    assert (tmp_path / "project" / "cache" / "thumbnails" / "asset-one.png").read_bytes() == b"thumbnail"
    assert load_project(project.project_dir).modified_at == original_modified_at


def test_existing_thumbnail_get_repairs_cache_without_project_mutation(tmp_path, monkeypatch):
    fake_prompt_server = SimpleNamespace(instance=SimpleNamespace(routes=web.RouteTableDef()))
    monkeypatch.setattr(server, "PromptServer", fake_prompt_server, raising=False)
    route_module = importlib.reload(routes)
    project = _project(tmp_path)
    source = tmp_path / "project" / "media" / "source.png"
    source.write_bytes(b"source")
    asset = Asset(
        asset_id="asset-route",
        asset_type="image",
        path="media/source.png",
        media_probe_signature=route_module._media_probe_signature(str(source)),
    )
    project.add_asset(asset)
    save_project(project)
    project_file = tmp_path / "project" / "project.json"
    project_bytes = project_file.read_bytes()
    project_mtime = project_file.stat().st_mtime_ns

    def generate(_asset_type, _source_path, output_path):
        with open(output_path, "wb") as handle:
            handle.write(b"thumbnail")
        return True

    monkeypatch.setattr(route_module, "_fast_cached_asset_response", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda _request: load_project(project.project_dir))
    monkeypatch.setattr(route_module, "ensure_thumbnail", generate)
    monkeypatch.setattr(
        route_module,
        "save_project",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("thumbnail GET must not save")),
    )

    response = asyncio.run(_route_handler(
        route_module,
        "GET",
        "/sonder-editor/project/{project_id}/thumbnail/{asset_id}",
    )(_Request("project", asset.asset_id)))

    assert response.status == 200
    assert (tmp_path / "project" / "cache" / "thumbnails" / "asset-route.png").read_bytes() == b"thumbnail"
    assert project_file.read_bytes() == project_bytes
    assert project_file.stat().st_mtime_ns == project_mtime


def test_source_replacement_during_generation_cannot_publish_stale_thumbnail(tmp_path, monkeypatch):
    project = _project(tmp_path)
    source = tmp_path / "project" / "media" / "source.png"
    source.write_bytes(b"source")
    signature = routes._media_probe_signature(str(source))
    asset = Asset(
        asset_id="asset-stale",
        asset_type="image",
        path="media/source.png",
        media_probe_signature=signature,
    )
    project.add_asset(asset)
    save_project(project)

    def replace_during_generation(_asset_type, _source_path, output_path):
        with open(output_path, "wb") as handle:
            handle.write(b"stale-thumbnail")
        source.write_bytes(b"replacement-source-with-a-new-signature")
        return True

    monkeypatch.setattr(routes, "ensure_thumbnail", replace_during_generation)
    routes._regenerate_thumbnail_if_current(project.project_dir, asset.asset_id, signature)

    assert not (tmp_path / "project" / "cache" / "thumbnails" / "asset-stale.png").exists()
    assert not list((tmp_path / "project" / "cache" / "thumbnails").glob("*.tmp.png"))


def test_missing_sources_and_unsupported_assets_fail_without_cache_publication(tmp_path):
    project = _project(tmp_path)
    missing = Asset(asset_id="asset-missing", asset_type="image", path="media/missing.png")
    artifact_source = tmp_path / "project" / "media" / "notes.txt"
    artifact_source.write_bytes(b"notes")
    artifact = Asset(asset_id="asset-artifact", asset_type="artifact", path="media/notes.txt")
    project.add_asset(missing)
    project.add_asset(artifact)
    save_project(project)

    routes._regenerate_thumbnail_if_current(project.project_dir, missing.asset_id, "missing-signature")
    routes._regenerate_thumbnail_if_current(
        project.project_dir,
        artifact.asset_id,
        routes._media_probe_signature(str(artifact_source)),
    )

    thumbnail_dir = tmp_path / "project" / "cache" / "thumbnails"
    assert not (thumbnail_dir / "asset-missing.png").exists()
    assert not (thumbnail_dir / "asset-artifact.png").exists()


def test_media_folder_discovery_registers_without_eager_thumbnail(tmp_path, monkeypatch):
    project = _project(tmp_path)
    source = tmp_path / "project" / "media" / "discovered.png"
    source.write_bytes(b"image")
    metadata = {
        "width": 32,
        "height": 24,
        "frame_count": 1,
        "fps": 0.0,
        "duration_sec": 0.0,
        "sample_rate": 0,
        "has_audio": False,
    }
    monkeypatch.setattr(routes, "_extract_asset_media_metadata", lambda *_args, **_kwargs: metadata)
    monkeypatch.setattr(
        routes,
        "ensure_thumbnail",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("discovery must stay registration-only")),
    )

    assert routes._sync_media_folder(project, purge_trashed=False) is True
    assert len(project.assets) == 1
    assert project.assets[0].path.replace("\\", "/") == "media/discovered.png"
    assert not (tmp_path / "project" / "cache" / "thumbnails" / f"{project.assets[0].asset_id}.png").exists()
