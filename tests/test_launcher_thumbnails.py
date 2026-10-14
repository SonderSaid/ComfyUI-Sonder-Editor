"""The Launcher's project thumbnails (``server/launcher_thumbnails.py``).

* The key follows the saved state and the opening scene; output is published under the
  key of the document actually loaded, which is the key the listing computes.
* Jobs are folder names resolved again when they run, through the project routes' real
  resolver: a folder that stopped resolving (unlinked, links turned off) is dropped and
  never written to.
* The worker waits while ComfyUI's queue has work before a job, and gives a render up
  and requeues it (never a remembered failure) if generation starts mid-render.
* Thumbnail work never creates folders in a project: no ``cache/`` means no tile, and
  only ``cache/launcher`` is ever made. Leftover temporary files are swept.
* The routes refuse a bad key, extension or folder, and serve published files only.
"""

import asyncio
import json
import os
import shutil
import sys
import types
from pathlib import Path

import cv2
import numpy as np
import pytest

from server import external_links, launcher_thumbnails as lt, project_manager as pm, routes, sdk_templates
from server.timeline_state import Asset, ClipReference, Scene
from legacy_projects import create_legacy_project

LINKS = {"on": False}


@pytest.fixture(autouse=True)
def user_dir(tmp_path, monkeypatch):
    user = tmp_path / "user"
    out = tmp_path / "out"
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(
        get_user_directory=lambda: str(user), get_output_directory=lambda: str(out)))
    LINKS["on"] = False
    monkeypatch.setattr(external_links, "is_enabled", lambda: LINKS["on"])
    return user


@pytest.fixture
def base(tmp_path):
    root = tmp_path / "out" / "sonder-projects"
    root.mkdir(parents=True)
    return root


def write_clip(path, frames=30, size=(64, 36)):
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 24.0, size)
    for index in range(frames):
        writer.write(np.full((size[1], size[0], 3), (index * 8) % 255, dtype=np.uint8))
    writer.release()


def project_with_clip(base, name="Film", *, frames=30):
    project = create_legacy_project(name, base)
    write_clip(Path(project.project_dir) / "media" / "clip.mp4", frames)
    project.assets = [Asset(asset_id="clip", asset_type="video", path="media/clip.mp4", fps=24.0,
                            frame_count=frames, width=64, height=36, duration_sec=frames / 24)]
    scene = Scene(scene_id="aaaa1111", name="One", order=0, fps=24.0, duration_frames=frames)
    scene.clips = [ClipReference(source_path="media/clip.mp4", timeline_start_frame=0, timeline_end_frame=frames)]
    project.scenes = [scene]
    pm.save_project(project, notify=False)
    return project


def fake_render(calls=None, busy_at_call=None):
    count = {"n": 0}

    def render(project, scene, folder, key, *, check_idle):
        count["n"] += 1
        if busy_at_call == count["n"]:
            raise lt._Busy()
        check_idle()
        if calls is not None:
            calls.append((key, scene.scene_id))
        paths = []
        for ext in ("mp4", "jpg"):
            path = os.path.join(folder, f"{key}.x.{ext}.tmp")
            Path(path).write_bytes(ext.encode())
            paths.append(path)
        return tuple(paths)
    return render


def make_worker(**kwargs):
    kwargs.setdefault("queue_busy", lambda: False)
    worker = lt.ThumbnailWorker(resolve=routes._project_dir_for_folder, **kwargs)
    worker.start = lambda: None  # jobs are driven by hand
    return worker


def document(project):
    return json.loads((Path(project.project_dir) / "project.json").read_text(encoding="utf-8"))


def name(project):
    return Path(project.project_dir).name


def link_to(target_dir, link):
    try:  # a junction, as Link makes on Windows; a symlink elsewhere
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target_dir), str(link))
        else:
            link.symlink_to(target_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory links are not available in this environment")


def unlink(link):
    if os.name == "nt":
        os.rmdir(link)
    else:
        os.unlink(link)


# ── Key and scene ───────────────────────────────────────────────────────────

def test_the_key_follows_the_saved_state_and_the_opening_scene():
    key = lt.thumbnail_key("2026-10-07T10:00:00", "aaaa1111")
    assert lt.KEY_RE.match(key)
    assert key == lt.thumbnail_key("2026-10-07T10:00:00", "aaaa1111")
    assert key != lt.thumbnail_key("2026-10-07T10:00:01", "aaaa1111")
    assert key != lt.thumbnail_key("2026-10-07T10:00:00", "bbbb2222")


def test_opening_scene_prefers_the_editor_scene_then_the_first_by_order():
    scenes = [{"scene_id": "b", "order": 2}, {"scene_id": "a", "order": 1}]
    assert lt.opening_scene(scenes, "b")["scene_id"] == "b"
    assert lt.opening_scene(scenes, "gone")["scene_id"] == "a"
    assert lt.opening_scene([Scene(scene_id="x", order=3), Scene(scene_id="y", order=1)], "").scene_id == "y"
    assert lt.opening_scene([], "a") is None


# ── Worker ──────────────────────────────────────────────────────────────────

def test_output_is_published_under_the_loaded_key_and_never_requeued(base):
    project = project_with_clip(base)
    folder = name(project)
    worker = make_worker(render=fake_render(calls := []))
    requested_key = lt.document_key(document(project), folder)
    # The project is edited after the request: the worker renders and names what it loads.
    project.name = "Edited"
    pm.save_project(project, notify=False)
    worker._process(folder)

    loaded_key = lt.document_key(document(project), folder)
    assert loaded_key != requested_key and calls == [(loaded_key, "aaaa1111")]
    assert lt.published_keys(project.project_dir) == [loaded_key]
    assert worker.describe(folder, project.project_dir, loaded_key)["state"] == lt.READY
    assert worker.describe(folder, project.project_dir, requested_key) == {
        "state": lt.STALE, "key": requested_key, "shown": loaded_key}
    assert not worker.pending

    # A published key is not rendered again, and an older one is removed on publish.
    worker._process(folder)
    assert len(calls) == 1
    sdk_templates.write_editor_state(folder, {"scene_id": "aaaa1111"})
    project.name = "Again"
    pm.save_project(project, notify=False)
    worker._process(folder)
    assert lt.published_keys(project.project_dir) == [lt.document_key(document(project), folder)]
    assert sorted(os.listdir(Path(project.project_dir) / "cache" / "launcher")) == sorted(
        f"{lt.published_keys(project.project_dir)[0]}.{ext}" for ext in ("jpg", "mp4"))


def test_a_document_without_modified_at_is_published_under_the_listing_key(base):
    project = project_with_clip(base)
    folder = name(project)
    raw = document(project)
    del raw["modified_at"]  # the model would fill in "now"; the key must not
    (Path(project.project_dir) / "project.json").write_text(json.dumps(raw), encoding="utf-8")
    worker = make_worker(render=fake_render(calls := []))
    worker._process(folder)
    worker._process(folder)
    assert lt.published_keys(project.project_dir) == [lt.document_key(raw, folder)]
    assert len(calls) == 1


def test_the_worker_waits_while_comfyui_has_work_before_a_job(base):
    project = project_with_clip(base)
    busy = [True, True, True]
    sleeps = []
    worker = make_worker(queue_busy=lambda: busy.pop() if busy else False,
                         render=fake_render(calls := []), sleep=sleeps.append)
    worker.wait_idle()
    assert len(sleeps) == 3 and calls == []
    worker._process(name(project))
    assert len(calls) == 1


def test_generation_starting_mid_render_requeues_instead_of_failing(base):
    project = project_with_clip(base)
    folder = name(project)
    worker = make_worker(render=fake_render(calls := [], busy_at_call=1))
    worker.request([folder])
    worker.stopped = False

    # One pass of the run loop: the render gives up, the job goes back to the front.
    folder_popped, _ = worker.pending.popitem(last=False)
    try:
        worker._process(folder_popped)
    except lt._Busy:
        with worker.condition:
            worker.pending[folder_popped] = None
            worker.status[folder_popped] = {"state": lt.QUEUED, "key": ""}
    assert list(worker.pending) == [folder] and worker.status[folder]["state"] == lt.QUEUED
    assert not [n for n in os.listdir(Path(project.project_dir) / "cache" / "launcher") if n.endswith(".tmp")]
    worker._process(folder)
    assert len(calls) == 1 and lt.published_keys(project.project_dir)


def test_the_run_loop_requeues_a_busy_render(base):
    project = project_with_clip(base)
    folder = name(project)
    worker = lt.ThumbnailWorker(resolve=routes._project_dir_for_folder, queue_busy=lambda: False,
                                render=fake_render(calls := [], busy_at_call=1))
    worker.request([folder])  # starts the real thread
    for _ in range(200):
        if lt.published_keys(project.project_dir):
            break
        import time
        time.sleep(0.05)
    worker.stop()
    assert len(calls) == 1 and lt.published_keys(project.project_dir)
    assert worker.status[folder]["state"] == lt.READY


def test_a_stopped_worker_abandons_its_render_and_leaves_no_files(base):
    project = project_with_clip(base)
    worker = make_worker(queue_busy=lambda: True, render=fake_render(), sleep=lambda _s: None)
    worker.stopped = True
    with pytest.raises(lt._Stopped):
        worker._process(name(project))
    assert lt.published_keys(project.project_dir) == []
    assert not [n for n in os.listdir(Path(project.project_dir) / "cache" / "launcher") if n.endswith(".tmp")]


def test_an_empty_scene_or_a_project_without_scenes_is_none(base):
    empty = pm.create_project("Empty", base_dir=str(base))
    empty.scenes = [Scene(scene_id="cccc3333", name="Blank", duration_frames=48)]
    pm.save_project(empty, notify=False)
    bare = pm.create_project("Bare", base_dir=str(base))
    worker = make_worker()
    for project in (empty, bare):
        folder = name(project)
        worker._process(folder)
        key = lt.document_key(document(project), folder)
        assert worker.describe(folder, project.project_dir, key)["state"] == lt.NONE
        assert lt.published_keys(project.project_dir) == []


def test_thumbnail_work_never_creates_project_folders(base):
    project = project_with_clip(base)
    folder = name(project)
    # Strip every folder but media/ and the document: a minimal hand-made project.
    for entry in os.listdir(project.project_dir):
        if entry not in ("media", "project.json"):
            shutil.rmtree(Path(project.project_dir) / entry)
    before = sorted(p.name for p in Path(project.project_dir).iterdir())
    pm.load_project(project.project_dir, ensure_subdirs=False)
    worker = make_worker(render=fake_render())
    worker._process(folder)
    assert sorted(p.name for p in Path(project.project_dir).iterdir()) == before
    assert worker.describe(folder, project.project_dir, lt.document_key(document(project), folder))["state"] == lt.FAILED

    # With cache/ present, only cache/launcher is made.
    (Path(project.project_dir) / "cache").mkdir()
    worker = make_worker(render=fake_render())
    worker._process(folder)
    assert sorted(p.name for p in Path(project.project_dir).iterdir()) == sorted(before + ["cache"])
    assert os.listdir(Path(project.project_dir) / "cache") == ["launcher"]


def test_a_linked_project_is_rendered_only_while_links_are_trusted(base, tmp_path):
    target = project_with_clip(tmp_path / "external", "Outside")
    shutil.rmtree(Path(target.project_dir) / "renders")
    link_to(target.project_dir, base / "Outside")
    before = sorted(p.name for p in Path(target.project_dir).iterdir())
    cache_before = sorted(os.listdir(Path(target.project_dir) / "cache"))

    # Queued while trusted, run after links were turned off: dropped, nothing written.
    LINKS["on"] = True
    worker = make_worker(render=fake_render(calls := []))
    worker.request(["Outside"])
    LINKS["on"] = False
    worker._process(worker.pending.popitem(last=False)[0])
    assert calls == [] and "Outside" not in worker.status
    assert sorted(os.listdir(Path(target.project_dir) / "cache")) == cache_before

    # Trusted: rendered, and no folder other than cache/launcher appears.
    LINKS["on"] = True
    worker._process("Outside")
    assert sorted(p.name for p in Path(target.project_dir).iterdir()) == before
    assert len(calls) == 1 and lt.published_keys(target.project_dir)


def test_a_project_unlinked_mid_render_is_forgotten_and_can_be_queued_again(base, tmp_path):
    target = project_with_clip(tmp_path / "external", "Outside")
    LINKS["on"] = True
    link_to(target.project_dir, base / "Outside")
    real = fake_render()

    def render_then_unlink(*args, **kwargs):
        result = real(*args, **kwargs)
        unlink(base / "Outside")
        return result

    worker = make_worker(render=render_then_unlink)
    worker._process("Outside")
    assert "Outside" not in worker.status
    assert lt.published_keys(target.project_dir) == []
    assert not [n for n in os.listdir(Path(target.project_dir) / "cache" / "launcher") if n.endswith(".tmp")]
    worker.request(["Outside"])
    assert list(worker.pending) == ["Outside"]


def test_leftover_temporary_files_are_swept_once_per_project(base):
    project = project_with_clip(base)
    cache = Path(project.project_dir) / "cache" / "launcher"
    cache.mkdir()
    (cache / "abc.mp4.tmp").write_bytes(b"x")
    make_worker(render=fake_render())._process(name(project))
    assert not (cache / "abc.mp4.tmp").exists()


def test_a_real_render_writes_a_480_wide_clip_and_poster(base):
    project = project_with_clip(base, frames=30)
    folder = name(project)
    make_worker(render=lt.render_thumbnail)._process(folder)
    key = lt.document_key(document(project), folder)
    assert lt.published_keys(project.project_dir) == [key]
    poster = cv2.imread(lt.media_path(project.project_dir, key, "jpg"))
    assert poster.shape[:2] == (270, 480)
    capture = cv2.VideoCapture(lt.media_path(project.project_dir, key, "mp4"))
    try:
        assert int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) == 30
        assert int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)) == 480
    finally:
        capture.release()


def test_newer_requests_go_first_and_memory_stays_bounded(monkeypatch):
    worker = make_worker()
    worker.request(["a", "b"])
    worker.request(["c", "b"])
    assert list(worker.pending) == ["c", "b", "a"]
    assert worker.describe("b", "")["state"] == lt.QUEUED

    monkeypatch.setattr(lt, "MAX_QUEUE", 3)
    worker.request(["d"])
    assert list(worker.pending) == ["d", "c", "b"] and "a" not in worker.status
    for folder in ("x", "y", "z"):
        worker.status[folder] = {"state": lt.READY, "key": ""}
    worker.request(["e"])
    assert len(worker.status) <= 4 and all(f in worker.status for f in worker.pending)


# ── Routes ──────────────────────────────────────────────────────────────────

def _client(base):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from server.launcher_routes import register_launcher_routes

    table = web.RouteTableDef()
    register_launcher_routes(table, projects_dir=lambda: str(base), project_dir_for_folder=routes._project_dir_for_folder)
    app = web.Application(middlewares=[routes._sonder_security_middleware])
    app.add_routes(table)
    return TestClient(TestServer(app))


def test_routes_serve_published_files_and_refuse_bad_names(base, monkeypatch):
    project = project_with_clip(base)
    folder = name(project)
    worker = make_worker(render=fake_render())
    monkeypatch.setattr(lt, "_worker", worker)
    worker._process(folder)
    key = lt.published_keys(project.project_dir)[0]

    async def run():
        async with _client(base) as client:
            projects = await (await client.get("/sonder-editor/launcher/projects")).json()
            ok = await client.get(f"/sonder-editor/launcher/thumbnail/{folder}/{key}.jpg")
            body = await ok.read()
            statuses = [
                (await client.get(f"/sonder-editor/launcher/thumbnail/{folder}/{file}")).status
                for file in (f"{key}.png", "ZZ.jpg", f"{key}.jpg.tmp", f"{'0' * 16}.mp4")
            ]
            statuses.append((await client.get(f"/sonder-editor/launcher/thumbnail/..%2F{folder}/{key}.jpg")).status)
            statuses.append((await client.get(f"/sonder-editor/launcher/thumbnail/Missing/{key}.jpg")).status)
            described = await (await client.post("/sonder-editor/launcher/thumbnails/status",
                                                  json={"folders": [folder, "Missing"]})).json()
            queued = await (await client.post("/sonder-editor/launcher/thumbnails/request",
                                              json={"folders": [folder, "Missing", "../x"]})).json()
            too_many = await client.post("/sonder-editor/launcher/thumbnails/request",
                                         json={"folders": ["a"] * 201})
            return projects, ok, body, statuses, described, queued, too_many.status

    projects, ok, body, statuses, described, queued, too_many = asyncio.run(run())
    entry = next(p for p in projects["projects"] if p["folder"] == folder)
    assert entry["thumbnail"] == {"state": lt.READY, "key": key, "shown": key}
    assert ok.status == 200 and body == b"jpg"
    assert "immutable" in ok.headers["Cache-Control"] and ok.headers["Content-Type"] == "image/jpeg"
    assert statuses == [404, 404, 404, 404, 404, 404]
    assert described["thumbnails"] == {folder: {"state": lt.READY, "key": key, "shown": key}}
    assert queued["queued"] == 1 and list(worker.pending) == [folder]
    assert too_many == 400
    assert worker.resolve is routes._project_dir_for_folder  # registration configured it


def test_a_linked_folder_is_neither_queued_nor_served_while_links_are_off(base, tmp_path, monkeypatch):
    target = project_with_clip(tmp_path / "external", "Outside")
    link_to(target.project_dir, base / "Outside")
    LINKS["on"] = True
    worker = make_worker(render=fake_render())
    monkeypatch.setattr(lt, "_worker", worker)
    worker._process("Outside")
    key = lt.published_keys(target.project_dir)[0]
    LINKS["on"] = False

    async def run():
        async with _client(base) as client:
            served = (await client.get(f"/sonder-editor/launcher/thumbnail/Outside/{key}.jpg")).status
            queued = await (await client.post("/sonder-editor/launcher/thumbnails/request",
                                              json={"folders": ["Outside"]})).json()
            return served, queued

    served, queued = asyncio.run(run())
    assert served == 404 and queued["queued"] == 0
