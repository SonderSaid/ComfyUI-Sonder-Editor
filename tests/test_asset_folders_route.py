"""`GET /assets/folders`: the folder list alone, for folder pickers.

The Save Bridge folder box (and, later, the Controls folder picker) used to read
`assets/dormant`, which builds every asset's payload to return a list of names.
This route answers from the same `_collect_asset_folders` and must build no
asset payload and write nothing.
"""
import asyncio
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request

from server import project_manager as pm
from server.timeline_state import Asset
from test_project_mutation_pipeline_backend import _load_route_module, _route_handler
from test_project_storage_route_policy import _full_stack

ROUTE = "/sonder-editor/project/{project_id}/assets/folders"


@pytest.fixture
def route_module(monkeypatch, tmp_path):
    module = _load_route_module(monkeypatch)
    monkeypatch.setattr(module, "_get_base_dir", lambda: str(tmp_path))
    return module


def _project(tmp_path):
    created = pm.create_project("Folders", base_dir=str(tmp_path))
    project = pm.load_project(created.project_dir)
    project.metadata["asset_folders"] = ["Empty/Declared", "/Shots/"]
    project.assets = [
        Asset(asset_id="a", name="a.png", asset_type="image", path="media/a.png", folder="Shots"),
        Asset(asset_id="b", name="b.png", asset_type="image", path="media/b.png", folder="Takes\\Day 1"),
        Asset(asset_id="c", name="c.png", asset_type="image", path="media/c.png"),
    ]
    pm.save_project(project, notify=False)
    return pm.load_project(project.project_dir)


def _call(module, project_id):
    handler = _full_stack(module, _route_handler(module, "GET", ROUTE))
    request = make_mocked_request("GET", "/api" + ROUTE.format(project_id=project_id),
                                  headers={"Host": "localhost"}, match_info={"project_id": project_id})
    response = asyncio.run(handler(request))
    return response.status, json.loads(response.body.decode("utf-8"))


def test_folders_route_lists_the_dormant_folders_without_asset_payloads(tmp_path, route_module, monkeypatch):
    project = _project(tmp_path)
    project_id = Path(project.project_dir).name
    expected = route_module._collect_asset_folders(project)
    assert expected, "the fixture must declare folders"

    built, saved = [], []
    monkeypatch.setattr(route_module, "_asset_payloads",
                        lambda *args, **kwargs: built.append(args) or [])
    monkeypatch.setattr(route_module, "save_project",
                        lambda *args, **kwargs: saved.append(args))
    before = Path(project.project_dir, "project.json").read_bytes()

    status, body = _call(route_module, project_id)

    assert status == 200, body
    assert body == {"project_id": project.project_id, "modified_at": project.modified_at,
                    "folders": expected}
    assert built == [], "no asset payload is built for a folder list"
    assert saved == [] and Path(project.project_dir, "project.json").read_bytes() == before


def test_folders_route_refuses_an_unknown_project(tmp_path, route_module):
    status, body = _call(route_module, "nope")
    assert status == 404, body


def test_the_folder_picker_reads_the_folders_route():
    source = (Path(__file__).resolve().parents[1] / "web" / "js" / "extension.js").read_text(encoding="utf-8")
    picker = source.split("async function listProjectAssetFolders", 1)[1].split("\n}\n", 1)[0]
    assert "/assets/folders`" in picker and "/assets/dormant" not in picker
