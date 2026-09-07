import copy
from concurrent.futures import ThreadPoolExecutor

import pytest

from server import project_manager as pm, project_storage as ps
from server.timeline_state import Asset


def split_project(tmp_path):
    p = pm.create_project("Automatic", base_dir=str(tmp_path))
    p.assets = [Asset(asset_id=str(i), generation_params={"fps": 24, "prompt": "original", "future": [i]}) for i in range(3)]
    pm.save_project(p, notify=False)
    return pm.load_project(p.project_dir)


def test_full_access_is_cached_and_thread_safe(tmp_path, monkeypatch):
    p = split_project(tmp_path)
    original = ps.read_component
    reads = []
    def counted(*args, **kwargs):
        reads.append(args[1])
        return original(*args, **kwargs)
    monkeypatch.setattr(ps, "read_component", counted)
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: p.assets[0].generation_params, range(24)))
    assert all(value is values[0] for value in values)
    assert values[0] == {"fps": 24, "prompt": "original", "future": [0]}
    assert len(reads) == 1
    assert bool(p.assets[0].generation_params)
    assert set(p.assets[0].generation_params) == {"fps", "prompt", "future"}
    p.assets[0].generation_params["future"].append("new")
    pm.save_project(p, notify=False)
    assert pm.load_project(p.project_dir).assets[0].generation_params["future"] == [0, "new"]


def test_lists_color_and_saves_do_not_trigger_hydration(tmp_path, monkeypatch):
    from server.routes import _asset_payloads, _asset_payload
    from server.media_helpers import resolve_source_color_interpretation
    p = split_project(tmp_path)
    original = ps.hydrate_asset
    hydrated = []
    def counted(asset):
        hydrated.append(asset.asset_id)
        return original(asset)
    monkeypatch.setattr(ps, "hydrate_asset", counted)
    assert all("generation_params" not in item for item in _asset_payloads(p, p.assets))
    for asset in p.assets:
        resolve_source_color_interpretation(asset=asset, allow_probe=False)
    p.name = "scene save equivalent"
    pm.save_project(p, notify=False)
    assert hydrated == []
    assert _asset_payload(p, p.assets[1])["generation_params"]["future"] == [1]
    assert hydrated == ["1"]


def test_replacement_loads_original_and_stale_replacement_conflicts(tmp_path):
    p = split_project(tmp_path)
    stale = pm.load_project(p.project_dir)
    p.assets[0].generation_params = {"prompt": "replacement"}
    assert p.assets[0]._generation_original["prompt"] == "original"
    pm.save_project(p, notify=False)
    stale.assets[0].generation_params = {"prompt": "stale"}
    with pytest.raises(pm.ProjectVersionConflict):
        pm.save_project(stale, notify=False)
    assert pm.load_project(p.project_dir).assets[0].generation_params == {"prompt": "replacement"}


def test_corrupt_automatic_read_refuses_and_does_not_certify(tmp_path):
    from pathlib import Path
    p = split_project(tmp_path)
    asset = p.assets[0]
    (Path(p.project_dir) / asset._generation_descriptor["path"]).write_bytes(b"bad")
    with pytest.raises(ps.ProjectStorageError):
        asset.generation_params.get("unknown")
    assert asset._generation_unhydrated
    assert "_generation_original" not in asset.__dict__


def test_copy_does_not_force_hydration(tmp_path):
    p = split_project(tmp_path)
    clone = copy.deepcopy(p.assets[0])
    assert clone._generation_unhydrated
    assert clone.generation_params["prompt"] == "original"
    assert p.assets[0]._generation_unhydrated


@pytest.mark.parametrize("unrelated_save", [False, True])
def test_committed_absence_is_a_conflict_base(tmp_path, unrelated_save):
    p = split_project(tmp_path)
    p.assets[0].generation_params = {}
    pm.save_project(p, notify=False)
    other = pm.load_project(p.project_dir)
    other.assets[0].generation_params = {"prompt": "other"}
    pm.save_project(other, notify=False)
    if unrelated_save:
        p.name = "unrelated"
        pm.save_project(p, notify=False)
    p.assets[0].generation_params = {"prompt": "mine"}
    with pytest.raises(pm.ProjectVersionConflict):
        pm.save_project(p, notify=False)
    assert pm.load_project(p.project_dir).assets[0].generation_params == {"prompt": "other"}
