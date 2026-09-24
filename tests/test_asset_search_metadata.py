"""Lean `has_provenance` and the bounded tracked-metadata search route.

The gallery decides whether an asset needs a detail request from the lean list alone, and
builds a complete `tracked:`/`field:` search projection from `/assets/search-metadata`
batches that it checks against the list's per-asset provenance revisions. These tests hold
the three things that depends on: the flag never hydrates, the route's revisions are the
list's revisions, and the route returns the raw `tracked_metadata` value — the gallery's
`trackedMetadataEntries` is the only authority for which values count as entries.
"""
import asyncio
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request

from server import project_manager as pm, project_storage as ps
from server.timeline_state import Asset
from test_project_mutation_pipeline_backend import _load_route_module, _route_handler
from test_project_storage_route_policy import _delete_component, _full_stack

ROUTE = "/sonder-editor/project/{project_id}/assets/search-metadata"

TRACKED = [
    {"label": "LoRA Stack", "display_type": "power_loras",
     "fields": {"lora": ["a.safetensors"], "strength": 0.8}, "raw_widget_text": "a:0.8",
     "future_field": {"kept": True}},
    "not-an-entry",
    ["an", "array", "entry"],
    None,
    {"label": "Seed", "fields": {"seed": 7}},
]


def _project(tmp_path, *, cold=True):
    """Three assets: tracked metadata, provenance without tracked metadata, and none."""
    created = pm.create_project("Search metadata", base_dir=str(tmp_path))
    project = pm.load_project(created.project_dir)
    project.assets = [
        Asset(asset_id="tracked", name="t.mp4", asset_type="video", path="media/t.mp4",
              generation_params={"fps": 24, "editor_export": {"tracked_metadata": TRACKED}}),
        Asset(asset_id="plain", name="p.mp4", asset_type="video", path="media/p.mp4",
              generation_params={"prompt": "no tracked section"}),
        Asset(asset_id="bare", name="b.png", asset_type="image", path="media/b.png"),
    ]
    # A no-bump save keeps a legacy project inline; a bumping save separates provenance.
    pm.save_project(project, notify=False, bump_modified_at=cold)
    return pm.load_project(project.project_dir)


def _call(module, project_dir, ids):
    project_id = Path(project_dir).name
    query = "&".join(f"asset_id={asset_id}" for asset_id in ids)
    handler = _full_stack(module, _route_handler(module, "GET", ROUTE))
    request = make_mocked_request("GET", "/api" + ROUTE.format(project_id=project_id) + f"?{query}",
                                  headers={"Host": "localhost"}, match_info={"project_id": project_id})
    response = asyncio.run(handler(request))
    return response.status, json.loads(response.body.decode("utf-8"))


@pytest.fixture
def route_module(monkeypatch, tmp_path):
    module = _load_route_module(monkeypatch)
    monkeypatch.setattr(module, "_get_base_dir", lambda: str(tmp_path))
    return module


@pytest.mark.parametrize("cold", [True, False])
def test_lean_flag_names_provenance_without_hydrating(tmp_path, monkeypatch, cold):
    from server.routes import _asset_payloads
    project = _project(tmp_path, cold=cold)
    assert bool(json.loads(Path(project.project_dir, "project.json").read_bytes()).get("storage")) is cold
    hydrated = []
    original = ps.hydrate_asset
    monkeypatch.setattr(ps, "hydrate_asset", lambda asset: hydrated.append(asset.asset_id) or original(asset))
    payload = {item["asset_id"]: item for item in _asset_payloads(project, project.assets)}
    assert {key: value["has_provenance"] for key, value in payload.items()} == {
        "tracked": True, "plain": True, "bare": False}
    assert hydrated == []
    assert all("generation_params" not in item for item in payload.values())


@pytest.mark.parametrize("cold", [True, False])
def test_route_returns_the_raw_value_and_the_lists_revisions(tmp_path, route_module, cold):
    from server.routes import _asset_payloads
    project = _project(tmp_path, cold=cold)
    listed = {item["asset_id"]: item["provenance_revision"] for item in _asset_payloads(project, project.assets)}
    status, body = _call(route_module, project.project_dir, ["tracked", "plain", "bare"])
    assert status == 200, body
    assert body["modified_at"] == project.modified_at
    # The gallery adopts a batch only when every revision equals the list's.
    assert body["revisions"] == listed
    tracked = body["tracked_metadata"]
    # Unfiltered: the JS filter (which keeps arrays) decides what an entry is, and a
    # registered matcher may read any field of one, including unknown ones.
    assert tracked["tracked"] == TRACKED
    assert tracked["plain"] is None and tracked["bare"] is None
    assert "provenance" not in body and "generation_params" not in json.dumps(body)


def test_unknown_ids_are_absent_rather_than_empty(tmp_path, route_module):
    project = _project(tmp_path)
    status, body = _call(route_module, project.project_dir, ["tracked", "gone"])
    assert status == 200
    assert set(body["revisions"]) == {"tracked"}
    assert set(body["tracked_metadata"]) == {"tracked"}


def test_duplicate_and_empty_ids_read_once_and_resolve_nothing(tmp_path, route_module):
    project = _project(tmp_path)
    status, body = _call(route_module, project.project_dir, ["tracked", "tracked", ""])
    assert status == 200
    assert set(body["revisions"]) == {"tracked"}


@pytest.mark.parametrize("value", ["a string", {"not": "a list"}, 7])
def test_non_list_tracked_values_pass_through_for_the_gallery_to_reject(tmp_path, route_module, value):
    created = pm.create_project("Odd shapes", base_dir=str(tmp_path))
    project = pm.load_project(created.project_dir)
    project.assets = [Asset(asset_id="odd", generation_params={"editor_export": {"tracked_metadata": value}})]
    pm.save_project(project, notify=False)
    status, body = _call(route_module, project.project_dir, ["odd"])
    assert status == 200 and body["tracked_metadata"]["odd"] == value


@pytest.mark.parametrize("cold", [True, False])
def test_revision_moves_when_provenance_changes(tmp_path, route_module, cold):
    project = _project(tmp_path, cold=cold)
    _, before = _call(route_module, project.project_dir, ["tracked"])
    ps.hydrate_asset(project.assets[0]).generation_params["editor_export"]["tracked_metadata"] = [
        {"label": "Seed", "fields": {"seed": 8}}]
    pm.save_project(project, notify=False, bump_modified_at=cold)
    _, after = _call(route_module, project.project_dir, ["tracked"])
    assert after["revisions"]["tracked"] != before["revisions"]["tracked"]
    assert after["tracked_metadata"]["tracked"] == [{"label": "Seed", "fields": {"seed": 8}}]
    assert (after["modified_at"] > before["modified_at"]) is cold


@pytest.mark.parametrize("count", [0, 65])
def test_batch_is_bounded(tmp_path, route_module, count):
    project = _project(tmp_path)
    status, body = _call(route_module, project.project_dir, [f"id{i}" for i in range(count)])
    assert status == 400
    assert "between 1 and 64" in body["error"]


def test_sixty_four_ids_are_accepted(tmp_path, route_module):
    project = _project(tmp_path)
    status, body = _call(route_module, project.project_dir, ["tracked"] + [f"id{i}" for i in range(63)])
    assert status == 200 and set(body["revisions"]) == {"tracked"}


def test_corrupt_component_is_the_shaped_storage_error(tmp_path, route_module):
    created = pm.create_project("Corrupt search", base_dir=str(tmp_path))
    project = pm.load_project(created.project_dir)
    project.assets = [Asset(asset_id="tracked", generation_params={"editor_export": {"tracked_metadata": TRACKED}})]
    pm.save_project(project, notify=False)
    leaked = _delete_component(project.project_dir, "provenance_")
    status, body = _call(route_module, project.project_dir, ["tracked"])
    assert status == 500
    assert body["code"] == "project_storage_unreadable"
    assert set(body) == {"error", "code"}
    assert leaked not in json.dumps(body) and str(tmp_path) not in " ".join(map(str, body.values()))


def test_missing_project_is_a_fixed_not_found(tmp_path, route_module):
    status, body = _call(route_module, str(tmp_path / "No Such Project"), ["tracked"])
    assert status == 404
    assert body["error"] == "Project not found"


def test_project_vanishing_after_resolution_does_not_leak_its_path(tmp_path, route_module, monkeypatch):
    project = _project(tmp_path)
    def vanished(project_dir, ids):
        raise FileNotFoundError(str(Path(project_dir) / "project.json"))
    monkeypatch.setattr(ps, "read_asset_search_metadata_batch", vanished)
    status, body = _call(route_module, project.project_dir, ["tracked"])
    assert status == 404 and body["error"] == "Project not found"
    assert str(tmp_path) not in json.dumps(body)


def test_full_payload_carries_the_flag_beside_its_params(tmp_path):
    from server.routes import _asset_payload
    project = _project(tmp_path)
    full = _asset_payload(project, project.assets[0])
    assert full["has_provenance"] is True
    assert full["generation_params"]["editor_export"]["tracked_metadata"] == TRACKED
