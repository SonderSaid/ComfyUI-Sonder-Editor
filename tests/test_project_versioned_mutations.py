"""A stale client precondition is refused by default, and rebased only for the queue.

`_apply_project_versioned_sync` used to swallow the `ProjectVersionConflict` its own
initial load raised, which is the client's `If-Match` failure and nothing else. That is
correct only where an operation is addressed by durable identity and is idempotent —
true of the queue routes it was written for, false in general.
"""
import asyncio
import json
from pathlib import Path

import pytest

from test_project_mutation_pipeline_backend import (
    DummyRequest, _load_route_module, _route_handler)
from server import project_manager as pm


def _project_overtaken_by_another_writer(tmp_path):
    """Return (project_dir_name, version the client still believes in)."""
    project = pm.create_project("Versioned", 24, 512, 512, "free", str(tmp_path))
    project_dir = project.project_dir
    stale_version = str(project.modified_at)

    other = pm.load_project(project_dir)
    other.name = "Moved on"
    pm.save_project(other, notify=False)
    assert str(pm.load_project(project_dir).modified_at) != stale_version

    return project_dir, Path(project_dir).name, stale_version


def test_stale_precondition_is_refused_rather_than_rebased(tmp_path, monkeypatch):
    routes = _load_route_module(monkeypatch)
    project_dir, dir_name, stale_version = _project_overtaken_by_another_writer(tmp_path)
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))
    reached = []

    def apply_fn(loaded):
        reached.append(str(loaded.name))
        loaded.name = "Applied against a document the caller never saw"
        return True, {}

    request = DummyRequest(match_info={"project_id": dir_name},
                           method="POST", headers={"If-Match": stale_version})

    with pytest.raises(routes.ProjectVersionConflict):
        routes._apply_project_versioned_sync(request, apply_fn)

    # The point of the refusal: the mutation never ran, so it could not resolve a
    # position or an id against state the caller had not seen.
    assert reached == []
    assert pm.load_project(project_dir).name == "Moved on"


def test_queue_mutations_still_rebase_a_stale_precondition(tmp_path, monkeypatch):
    """Preserve 5eab30c: coalesced queue gestures legitimately lag their own write."""
    routes = _load_route_module(monkeypatch)
    project_dir, dir_name, stale_version = _project_overtaken_by_another_writer(tmp_path)
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))
    reached = []

    def apply_fn(loaded):
        reached.append(str(loaded.name))
        loaded.name = "Rebased"
        return True, {}

    request = DummyRequest(match_info={"project_id": dir_name},
                           method="POST", headers={"If-Match": stale_version})

    project, _payload = routes._apply_queue_versioned_sync(request, apply_fn)

    assert reached == ["Moved on"], "queue path must rebase onto the newer document"
    assert pm.load_project(project_dir).name == "Rebased"
    assert project is not None


def test_a_cas_retry_remembers_the_object_that_committed(tmp_path, monkeypatch):
    """The version header must name the object that committed, not the first load.

    `_load_project_from_request` already remembers the object it loaded, so a test
    without a retry proves nothing — it passes with or without the helper's own
    `_remember_request_project`. Only a real CAS retry discriminates, because the
    reload goes through `load_project` directly and remembers nothing.
    """
    routes = _load_route_module(monkeypatch)
    project = pm.create_project("Retried", 24, 512, 512, "free", str(tmp_path))
    project_dir = project.project_dir
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))

    real_save = routes.save_project
    saves = []

    def save_losing_the_first_race(saved, **kwargs):
        saves.append(saved)
        if len(saves) == 1:
            # Another writer lands between this load and this commit, so the real
            # compare-and-swap below refuses and the helper must reload and retry.
            other = pm.load_project(project_dir)
            other.name = "Concurrent"
            pm.save_project(other, notify=False)
        return real_save(saved, **kwargs)

    monkeypatch.setattr(routes, "save_project", save_losing_the_first_race)
    seen = []

    def apply_fn(loaded):
        seen.append(loaded)
        loaded.name = "Committed"
        return True, {}

    request = DummyRequest(match_info={"project_id": Path(project_dir).name}, method="POST")
    committed, _payload = routes._apply_project_versioned_sync(
        request, apply_fn, addressing="identity")

    assert len(saves) == 2, "the first commit must have been refused by the CAS"
    assert len(seen) == 2, "the mutation must re-run against the reloaded document"
    assert committed is seen[-1]
    assert request.get("sonder_editor_project") is committed
    # The discriminating assertion: without the helper's own remember, the request
    # would still hold the object the initial load stamped on it.
    assert request.get("sonder_editor_project") is not seen[0]
    assert pm.load_project(project_dir).name == "Committed"


def test_a_positional_caller_cannot_acquire_a_retry_by_omission(tmp_path, monkeypatch):
    """`addressing` sets both re-application policies, so neither is opt-out."""
    routes = _load_route_module(monkeypatch)
    project = pm.create_project("Positional", 24, 512, 512, "free", str(tmp_path))
    project_dir = project.project_dir
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))

    real_save = routes.save_project
    saves = []

    def save_losing_the_first_race(saved, **kwargs):
        saves.append(saved)
        if len(saves) == 1:
            other = pm.load_project(project_dir)
            other.name = "Concurrent"
            pm.save_project(other, notify=False)
        return real_save(saved, **kwargs)

    monkeypatch.setattr(routes, "save_project", save_losing_the_first_race)
    seen = []

    def apply_fn(loaded):
        seen.append(loaded)
        loaded.name = "Committed"
        return True, {}

    request = DummyRequest(match_info={"project_id": Path(project_dir).name}, method="POST")

    # Default addressing is positional: the conflict surfaces instead of the
    # operation being re-resolved against a document the caller never saw.
    with pytest.raises(routes.ProjectVersionConflict):
        routes._apply_project_versioned_sync(request, apply_fn)

    assert len(seen) == 1, "a positional mutation must not be re-applied"
    assert pm.load_project(project_dir).name == "Concurrent"


def test_addressing_rejects_an_unknown_policy(tmp_path, monkeypatch):
    routes = _load_route_module(monkeypatch)
    with pytest.raises(ValueError, match="addressing"):
        routes._apply_project_versioned_sync(
            DummyRequest(match_info={"project_id": "x"}), lambda p: (False, {}),
            addressing="whatever")


# ---------------------------------------------------------------------------
# Route-level regressions for the handlers phase 3 left without a precondition.
# ---------------------------------------------------------------------------

def _project_with_scenes(tmp_path, *names):
    from server.timeline_state import Scene
    project = pm.create_project("Concurrent", 24, 512, 512, "free", str(tmp_path))
    project_dir = project.project_dir
    for name in names:
        loaded = pm.load_project(project_dir)
        loaded.add_scene(Scene(name=name))
        pm.save_project(loaded, notify=False)
    ids = {scene.name: scene.scene_id for scene in pm.load_project(project_dir).scenes}
    return project_dir, Path(project_dir).name, ids


def test_concurrent_deletes_of_different_scenes_both_survive(tmp_path, monkeypatch):
    """The regression phase 3 introduced: both requests returned 200, one was lost."""
    routes = _load_route_module(monkeypatch)
    project_dir, dir_name, ids = _project_with_scenes(tmp_path, "scene-A", "scene-B")
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))
    delete = _route_handler(
        routes, "DELETE", "/sonder-editor/project/{project_id}/scenes/{scene_id}")

    async def run():
        async def delete_one(name):
            return await delete(DummyRequest(
                match_info={"project_id": dir_name, "scene_id": ids[name]}, method="DELETE"))
        return await asyncio.gather(delete_one("scene-A"), delete_one("scene-B"))

    responses = asyncio.run(run())

    assert [response.status for response in responses] == [200, 200]
    assert [scene.name for scene in pm.load_project(project_dir).scenes] == [], (
        "a delete that reported success was discarded by the other writer")


def test_concurrent_duplicates_do_not_collide_on_the_copy_name(tmp_path, monkeypatch):
    """Each attempt re-resolves the unique name against whatever landed meanwhile."""
    routes = _load_route_module(monkeypatch)
    project_dir, dir_name, ids = _project_with_scenes(tmp_path, "Source")
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))
    duplicate = _route_handler(
        routes, "POST", "/sonder-editor/project/{project_id}/scenes/{scene_id}/duplicate")

    async def run():
        async def duplicate_once():
            return await duplicate(DummyRequest(
                match_info={"project_id": dir_name, "scene_id": ids["Source"]}, method="POST"))
        return await asyncio.gather(duplicate_once(), duplicate_once())

    responses = asyncio.run(run())

    assert [response.status for response in responses] == [201, 201]
    names = [scene.name for scene in pm.load_project(project_dir).scenes]
    assert sorted(names) == ["Source", "Source (copy)", "Source (copy) 2"], names
    scene_ids = [scene.scene_id for scene in pm.load_project(project_dir).scenes]
    assert len(set(scene_ids)) == len(scene_ids), "duplicate minted a colliding scene id"


def test_deleting_an_already_deleted_scene_on_a_retry_reports_success(tmp_path, monkeypatch):
    """A completed delete must not be reported as a 404 just because we lost a race."""
    routes = _load_route_module(monkeypatch)
    project_dir, dir_name, ids = _project_with_scenes(tmp_path, "Doomed", "Bystander")
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))

    real_save = routes.save_project
    saves = []

    def save_losing_the_first_race(saved, **kwargs):
        saves.append(saved)
        if len(saves) == 1:
            # Another writer removes the same scene AND bumps the version, so our
            # commit is refused and the retry finds the target already gone.
            other = pm.load_project(project_dir)
            other.remove_scene(ids["Doomed"])
            pm.save_project(other, notify=False)
        return real_save(saved, **kwargs)

    monkeypatch.setattr(routes, "save_project", save_losing_the_first_race)
    delete = _route_handler(
        routes, "DELETE", "/sonder-editor/project/{project_id}/scenes/{scene_id}")

    response = asyncio.run(delete(DummyRequest(
        match_info={"project_id": dir_name, "scene_id": ids["Doomed"]}, method="DELETE")))

    assert response.status == 200
    remaining = [scene.name for scene in pm.load_project(project_dir).scenes]
    assert remaining == ["Bystander"], remaining


def test_a_positional_saved_selection_delete_refuses_rather_than_shifting(tmp_path, monkeypatch):
    """`idx` means what the caller's document said; refuse rather than re-resolve it."""
    from server.timeline_state import Scene
    routes = _load_route_module(monkeypatch)
    project = pm.create_project("Selections", 24, 512, 512, "free", str(tmp_path))
    project_dir = project.project_dir
    loaded = pm.load_project(project_dir)
    scene = Scene(name="Only")
    scene.saved_selections = [{"name": "keep-0"}, {"name": "target-1"}, {"name": "keep-2"}]
    loaded.add_scene(scene)
    pm.save_project(loaded, notify=False)
    scene_id = pm.load_project(project_dir).scenes[0].scene_id
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))

    real_save = routes.save_project
    saves = []

    def save_losing_the_first_race(saved, **kwargs):
        saves.append(saved)
        if len(saves) == 1:
            other = pm.load_project(project_dir)
            other.scenes[0].saved_selections.insert(0, {"name": "inserted"})
            pm.save_project(other, notify=False)
        return real_save(saved, **kwargs)

    monkeypatch.setattr(routes, "save_project", save_losing_the_first_race)
    delete = _route_handler(
        routes, "DELETE",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/saved_selections/{index}")

    with pytest.raises(routes.ProjectVersionConflict):
        asyncio.run(delete(DummyRequest(
            match_info={"project_id": Path(project_dir).name, "scene_id": scene_id, "index": "1"},
            method="DELETE")))

    # The refusal is the point: index 1 now names "keep-0", not "target-1".
    names = [item["name"] for item in pm.load_project(project_dir).scenes[0].saved_selections]
    assert names == ["inserted", "keep-0", "target-1", "keep-2"], names


def test_a_project_without_a_version_is_refused_rather_than_saved_unguarded(tmp_path, monkeypatch):
    """`save_project` skips the CAS on an empty precondition, so never reach it.

    An empty `modified_at` would silently downgrade a versioned mutation to an
    unguarded write, making `addressing` meaningless with no error anywhere.
    """
    routes = _load_route_module(monkeypatch)
    project = pm.create_project("Unversioned", 24, 512, 512, "free", str(tmp_path))
    project_dir = project.project_dir
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))

    real_load = routes._load_project_from_request

    def load_with_a_blank_version(request, **kwargs):
        loaded = real_load(request, **kwargs)
        loaded.modified_at = ""
        return loaded

    monkeypatch.setattr(routes, "_load_project_from_request", load_with_a_blank_version)
    saves = []
    monkeypatch.setattr(routes, "save_project", lambda saved, **kwargs: saves.append(kwargs))

    with pytest.raises(RuntimeError, match="modified_at"):
        routes._apply_project_versioned_sync(
            DummyRequest(match_info={"project_id": Path(project_dir).name}, method="POST"),
            lambda loaded: (True, {}), addressing="identity")

    assert saves == [], "an unguarded save must never be reached"


def test_a_version_conflict_409_carries_the_editor_security_headers(tmp_path, monkeypatch):
    """The conflict middleware is outermost, so nothing downstream adds them for it.

    `_sonder_security_middleware` applies the headers on its way out, which never runs
    when a handler raises: the exception unwinds past it to the conflict middleware,
    which builds the 409 itself. Versioned mutations make that an ordinary response.
    """
    routes = _load_route_module(monkeypatch)

    async def raising_handler(request):
        raise routes.ProjectVersionConflict(
            project_dir=str(tmp_path), expected_modified_at="ours",
            actual_modified_at="theirs", current_data={})

    request = DummyRequest(match_info={"project_id": "project"}, method="PUT",
                           path="/sonder-editor/project/project")
    response = asyncio.run(routes._project_conflict_middleware(request, raising_handler))

    assert response.status == 409
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"]
    assert response.headers["Cross-Origin-Resource-Policy"]
    assert "Content-Security-Policy" in response.headers
    # The version headers it already carried must survive.
    assert response.headers["X-Sonder-Project-Modified-At"] == "theirs"


def _project_with_selections(tmp_path, selections):
    """A one-scene project whose saved selections are exactly `selections`."""
    from server.timeline_state import Scene
    project = pm.create_project("Selections", 24, 512, 512, "free", str(tmp_path))
    project_dir = project.project_dir
    loaded = pm.load_project(project_dir)
    scene = Scene(name="Only")
    scene.saved_selections = [dict(entry) for entry in selections]
    loaded.add_scene(scene)
    pm.save_project(loaded, notify=False)
    return project_dir, pm.load_project(project_dir).scenes[0].scene_id


def _selection(name, start=0, end=10):
    return {"name": name, "start": start, "end": end, "pre_context_frames": 0,
            "post_context_frames": 0, "mask_pre_offset": 0, "mask_post_offset": 0}


def _delete_selection(routes, project_dir, scene_id, index, body=None):
    handler = _route_handler(
        routes, "DELETE",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/saved_selections/{index}")
    return asyncio.run(handler(DummyRequest(
        match_info={"project_id": Path(project_dir).name,
                    "scene_id": scene_id, "index": str(index)},
        method="DELETE", body=body)))


def _selection_names(project_dir):
    return [item["name"] for item in
            pm.load_project(project_dir).scenes[0].saved_selections]


def _lose_the_first_save_to(routes, monkeypatch, project_dir, mutate):
    """Make the first save lose a race to `mutate`, as a real competing writer would.

    Writing the competing state synchronously and then delegating to the real
    `save_project` produces a genuine `ProjectVersionConflict` out of the
    compare-and-swap rather than a synthesized one, so the retry path under test is the
    production path.
    """
    real_save = routes.save_project
    saves = []

    def save_losing_the_first_race(saved, **kwargs):
        saves.append(saved)
        if len(saves) == 1:
            other = pm.load_project(project_dir)
            mutate(other)
            pm.save_project(other, notify=False)
        return real_save(saved, **kwargs)

    monkeypatch.setattr(routes, "save_project", save_losing_the_first_race)


def test_a_snapshot_lets_a_saved_selection_delete_survive_an_unrelated_commit(
        tmp_path, monkeypatch):
    """The gesture's usability problem: any concurrent commit refused it.

    The compare-and-swap fails on *any* newer version, not only one that touched
    `saved_selections`, and the prompt worker commits job status out of band on every
    queue transition. Without an identity snapshot the only safe answer was a refusal,
    so during a render the user was told no by a writer that never read this list.
    """
    routes = _load_route_module(monkeypatch)
    project_dir, scene_id = _project_with_selections(
        tmp_path, [_selection("keep-0"), _selection("target-1"), _selection("keep-2")])
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))

    def rename_the_project(other):
        other.name = "Renamed by a writer that never read this list"

    _lose_the_first_save_to(routes, monkeypatch, project_dir, rename_the_project)

    response = _delete_selection(routes, project_dir, scene_id, 1,
                                 body={"expected": _selection("target-1")})

    assert response.status == 200
    assert _selection_names(project_dir) == ["keep-0", "keep-2"]
    # The competing write survives: the retry rebased onto it rather than clobbering it.
    assert pm.load_project(project_dir).name == "Renamed by a writer that never read this list"


def test_a_snapshot_follows_the_row_when_the_list_shifts_underneath(tmp_path, monkeypatch):
    """Why the index alone could never be retried.

    A writer inserting ahead of the target makes `idx` name a different row. The
    snapshot re-resolves to the row the caller actually clicked, wherever it drifted.
    """
    routes = _load_route_module(monkeypatch)
    project_dir, scene_id = _project_with_selections(
        tmp_path, [_selection("keep-0"), _selection("target-1"), _selection("keep-2")])
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))

    def insert_ahead_of_the_target(other):
        other.scenes[0].saved_selections.insert(0, _selection("inserted"))

    _lose_the_first_save_to(routes, monkeypatch, project_dir, insert_ahead_of_the_target)

    response = _delete_selection(routes, project_dir, scene_id, 1,
                                 body={"expected": _selection("target-1")})

    assert response.status == 200
    # Index 1 named "keep-0" by the time the retry ran. Position did not decide.
    assert _selection_names(project_dir) == ["inserted", "keep-0", "keep-2"]


def test_two_indistinguishable_selections_refuse_rather_than_guess(tmp_path, monkeypatch):
    """Identical rows are separated only by position, and position is what moved."""
    routes = _load_route_module(monkeypatch)
    twin = _selection("twin", 4, 8)
    project_dir, scene_id = _project_with_selections(
        tmp_path, [twin, _selection("between"), twin])
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))

    def insert_ahead_of_both(other):
        other.scenes[0].saved_selections.insert(0, _selection("inserted"))

    _lose_the_first_save_to(routes, monkeypatch, project_dir, insert_ahead_of_both)

    response = _delete_selection(routes, project_dir, scene_id, 0, body={"expected": twin})

    assert response.status == 409
    assert json.loads(response.body)["code"] == "identity_ambiguous"
    assert _selection_names(project_dir) == ["inserted", "twin", "between", "twin"], (
        "an ambiguous delete removed a row anyway")


def test_a_row_that_changed_under_the_caller_is_an_identity_mismatch(tmp_path, monkeypatch):
    """Re-resolution is not permission to delete whatever occupies the slot now."""
    routes = _load_route_module(monkeypatch)
    project_dir, scene_id = _project_with_selections(
        tmp_path, [_selection("keep-0"), _selection("target-1")])
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))

    def rename_the_target(other):
        other.scenes[0].saved_selections[1]["name"] = "renamed by someone else"

    _lose_the_first_save_to(routes, monkeypatch, project_dir, rename_the_target)

    response = _delete_selection(routes, project_dir, scene_id, 1,
                                 body={"expected": _selection("target-1")})

    assert response.status == 409
    assert json.loads(response.body)["code"] == "identity_mismatch"
    assert _selection_names(project_dir) == ["keep-0", "renamed by someone else"]


def test_a_snapshot_that_constrains_nothing_does_not_buy_a_retry(tmp_path, monkeypatch):
    """The omission hazard, from the other direction.

    `addressing` now follows the request body, so a field-less `expected` must NOT be
    treated as identity: it would grant re-resolution on the strength of a snapshot
    that matches every row. Degrading to positional is the safe direction.
    """
    routes = _load_route_module(monkeypatch)
    project_dir, scene_id = _project_with_selections(
        tmp_path, [_selection("keep-0"), _selection("target-1")])
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))

    def insert_ahead(other):
        other.scenes[0].saved_selections.insert(0, _selection("inserted"))

    _lose_the_first_save_to(routes, monkeypatch, project_dir, insert_ahead)

    with pytest.raises(routes.ProjectVersionConflict):
        _delete_selection(routes, project_dir, scene_id, 1, body={"expected": {}})

    assert _selection_names(project_dir) == ["inserted", "keep-0", "target-1"]


def test_only_a_snapshot_carrying_identity_fields_counts_as_one(monkeypatch):
    """The gate `addressing` now depends on, pinned directly."""
    routes = _load_route_module(monkeypatch)
    for vacuous in ({}, {"unrelated": "field"}, "not a dict", None, 7, []):
        assert routes.saved_selection_identity_snapshot(vacuous) is None, vacuous
    usable = {"name": "x"}
    assert routes.saved_selection_identity_snapshot(usable) is usable
    assert routes.saved_selection_identity_snapshot({"start": 0}) is not None
