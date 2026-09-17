"""Migration must not park the HTTP event loop behind a project write lock."""
import asyncio
import copy
import json
import threading

from test_component_migration import stored_project
from test_project_mutation_pipeline_backend import DummyRequest, _load_route_module, _route_handler
from server import project_manager as pm, project_storage as ps


def test_route_migration_keeps_loop_live_and_concurrent_reader_off_lock(tmp_path, monkeypatch):
    project, path, history = stored_project(tmp_path)
    routes = _load_route_module(monkeypatch)
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))
    original_load = routes._load_project_from_request
    entered = threading.Event()
    release = threading.Event()
    save_thread = []
    load_threads = []
    original_repack = ps._repack_components

    def repack(*args):
        save_thread.append(threading.get_ident())
        entered.set()
        assert release.wait(5), "event loop failed to release migrating writer"
        return original_repack(*args)

    def load(*args, **kwargs):
        load_threads.append(threading.get_ident())
        return original_load(*args, **kwargs)

    # `api_list_projects` calls `list_projects`, never `_load_project_from_request`,
    # so without its own recorder the listing route contributed nothing to the thread
    # assertion below — its only check was `status == 200`.
    original_list = routes.list_projects

    def listing_load(*args, **kwargs):
        load_threads.append(threading.get_ident())
        return original_list(*args, **kwargs)

    monkeypatch.setattr(ps, "_repack_components", repack)
    monkeypatch.setattr(routes, "_load_project_from_request", load)
    monkeypatch.setattr(routes, "list_projects", listing_load)
    update = _route_handler(routes, "PUT", "/sonder-editor/project/{project_id}")
    get = _route_handler(routes, "GET", "/sonder-editor/project/{project_id}")
    listing = _route_handler(routes, "GET", "/sonder-editor/projects")
    # Deadlock guard only. Its window must stay clear of the assertion it protects:
    # the writer waits 5s, the assertion runs ~30ms after `entered`, and a watchdog
    # that fired first would release the writer and fail the test for the wrong
    # reason. 30s is far outside both.
    watchdog = threading.Timer(30, release.set)
    watchdog.start()

    async def run():
        loop_thread = threading.get_ident()
        request = DummyRequest(match_info={"project_id": path.parent.name},
            body={"name": "Renamed"}, method="PUT", headers={"If-Match": project.modified_at})
        writing = asyncio.create_task(update(request))
        assert await asyncio.to_thread(entered.wait, 2)
        reading = asyncio.create_task(get(DummyRequest(
            match_info={"project_id": path.parent.name}, method="GET")))
        listing_task = asyncio.create_task(listing(DummyRequest(method="GET")))
        await asyncio.sleep(.03)
        assert not release.is_set(), "HTTP reader blocked the event loop on the write lock"
        release.set()
        response, read_response, list_response = await asyncio.gather(writing, reading, listing_task)
        assert response.status == read_response.status == list_response.status == 200
        assert all(t != loop_thread for t in save_thread + load_threads), (
            "a project read or the migrating save ran on the event loop")
        assert json.loads(response.body)["name"] == "Renamed"
        # The real route still honors its normal version-conflict contract.
        stale = DummyRequest(match_info={"project_id": path.parent.name}, body={"name": "stale"},
            method="PUT", headers={"If-Match": project.modified_at})
        response = await routes._project_error_middleware(stale, update)
        assert response.status == 409

    try:
        asyncio.run(run())
    finally:
        release.set()
        watchdog.cancel()
    assert json.loads(path.read_bytes())["storage"]["format_version"] == 3
    assert ps.read_prompt_history(str(path.parent)) == history
