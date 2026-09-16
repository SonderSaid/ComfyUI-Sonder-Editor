"""Real format-2 roots, independent of the new mint path, and format-3 peers."""
import copy
import gc
import hashlib
import json
from pathlib import Path

import pytest

from server import project_manager as pm, project_storage as ps
from server.timeline_state import Asset, GenerationJob


def stored_project(tmp_path, version=2):
    project = pm.create_project("Migration", base_dir=str(tmp_path))
    root = Path(project.project_dir)
    path = root / "project.json"
    raw = json.loads(path.read_bytes())

    def write(name, value):
        # Deliberately noncanonical: reserializing would change the digest.
        payload = json.dumps(value, indent=3, ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        relative = f"state/{name}-{digest}.json" if version == 2 else f"state/{digest}.json"
        (root / relative).write_bytes(payload)
        return {"path": relative, "sha256": digest, "bytes": len(payload)}

    history = [{"hash": "same", "future": [1, 2]}, {"hash": "same", "future": [1, 2]}, None]
    index = {"kind": "prompt_history_index", "schema_version": 1, "future_index": "keep",
             "entries": [write("history_entry", entry) for entry in history]}
    components = {"prompt_history": write("prompt_history", index)}
    raw["assets"] = []
    for asset_id in ("a", "b"):
        asset = Asset(asset_id=asset_id, generation_params={"fps": 24}).to_dict()
        asset.update(has_frozen_provenance=True, generation_summary={"has_embedded_workflow": True})
        raw["assets"].append(asset)
        components[ps.asset_component_name(asset_id)] = write(ps.asset_component_name(asset_id),
            {"prompt": "original " + asset_id, "editor_export": {"has_embedded_workflow": True}})
    job = GenerationJob(job_id="job", prompt_sections=[{"prompt": "queued"}]).to_dict()
    components[ps.job_component_name("job")] = write(ps.job_component_name("job"),
        {field: job.pop(field) for field in ps.FROZEN_JOB_FIELDS})
    raw["generation_queue"] = [job]
    raw["storage"] = {"format_version": version, "partitions": ["assets", "queue"],
                      "components": components}
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return pm.load_project(str(root)), path, history


@pytest.mark.parametrize("version", [2, 3])
def test_roundtrip_provenance_queue_and_history(tmp_path, version):
    project, path, history = stored_project(tmp_path, version)
    original_root = path.read_bytes()
    original = copy.deepcopy(project._raw_data["storage"]["components"])
    original_bytes = {name: (path.parent / desc["path"]).read_bytes() for name, desc in original.items()}
    history_index = ps.read_component(project.project_dir, "prompt_history", original["prompt_history"])
    history_bytes = [(path.parent / child["path"]).read_bytes() for child in history_index["entries"]]
    pm.save_project(project, notify=False)
    raw = json.loads(path.read_bytes())
    assert raw["storage"]["format_version"] == 3
    assert ps.read_prompt_history(project) == history
    for name, desc in raw["storage"]["components"].items():
        assert desc["path"] == f"state/{desc['sha256']}.json"
        assert len(Path(desc["path"]).name) == 69
        if name != "prompt_history":
            assert (desc["sha256"], desc["bytes"]) == (original[name]["sha256"], original[name]["bytes"])
            assert (path.parent / desc["path"]).read_bytes() == original_bytes[name]
    index = ps.read_component(project.project_dir, "prompt_history", raw["storage"]["components"]["prompt_history"])
    assert index["future_index"] == "keep"
    for child, payload in zip(index["entries"], history_bytes):
        assert child["path"] == f"state/{child['sha256']}.json"
        assert (path.parent / child["path"]).read_bytes() == payload
    if version == 2:
        assert raw["storage"]["recovery_previous"] == raw["storage"]["authoring_previous"] == original
        assert len(Path(original[ps.asset_component_name("a")]["path"]).name) == 145
        backups = list((path.parent / "state").glob("*.bak"))
        assert len(backups) == 1 and backups[0].read_bytes() == original_root
    loaded = pm.load_project(project.project_dir)
    assert loaded.assets[0].generation_params["prompt"] == "original a"
    assert ps.resolve_queue_job(loaded, "job").prompt_sections == [{"prompt": "queued"}]


@pytest.mark.parametrize("version", [2, 3])
def test_provenance_edit_on_first_and_following_save(tmp_path, version):
    project, path, _ = stored_project(tmp_path, version)
    project.assets[0].generation_params["prompt"] = "first edit"
    pm.save_project(project, notify=False)
    # b was cold during migration: its descriptor and lease must both rebase.
    b = project.assets[1]
    assert b._generation_unhydrated
    assert b._generation_descriptor["path"] == f"state/{b._generation_descriptor['sha256']}.json"
    assert b._storage_lease.descriptor == b._generation_descriptor
    b.generation_params["prompt"] = "second edit"
    pm.save_project(project, notify=False)
    raw = json.loads(path.read_bytes())
    assert all(asset["generation_summary"]["has_embedded_workflow"] is True for asset in raw["assets"])
    assert [a.generation_params["prompt"] for a in pm.load_project(project.project_dir).assets] == ["first edit", "second edit"]


@pytest.mark.parametrize("version", [2, 3])
def test_stale_writer_conflicts_after_an_actual_content_change(tmp_path, version):
    project, path, _ = stored_project(tmp_path, version)
    stale = pm.load_project(project.project_dir)
    project.assets[0].generation_params["prompt"] = "current"
    pm.save_project(project, notify=False)
    pm.save_project(stale, notify=False)  # undeclared payload must not become authority
    stale.assets[0].generation_params["prompt"] = "stale edit"
    before = path.read_bytes()
    with pytest.raises(pm.ProjectVersionConflict):
        pm.save_project(stale, notify=False)
    assert path.read_bytes() == before


def test_no_bump_does_not_migrate_and_history_never_downgrades(tmp_path):
    project, path, history = stored_project(tmp_path)
    before = copy.deepcopy(project._raw_data["storage"]["components"])
    pm.save_project(project, bump_modified_at=False, notify=False)
    raw = json.loads(path.read_bytes())
    assert raw["storage"]["format_version"] == 2
    assert raw["storage"]["components"] == before
    ps.stage_prompt_history(project, history + [{"new": 1}])
    pm.save_project(project, notify=False)
    ps.stage_prompt_history(project, history + [{"new": 2}])
    pm.save_project(project, notify=False)
    raw = json.loads(path.read_bytes())
    assert raw["storage"]["format_version"] == 3
    assert ps.read_prompt_history(project)[-1] == {"new": 2}
    index = ps.read_component(project.project_dir, "prompt_history", raw["storage"]["components"]["prompt_history"])
    assert all(child["path"] == f"state/{child['sha256']}.json" for child in index["entries"])


def test_migration_gc_waits_for_retained_roots_and_detached_readers(tmp_path):
    project, path, history = stored_project(tmp_path)
    stale = pm.load_project(project.project_dir)
    detached = copy.deepcopy(stale.assets[0])
    legacy_paths = set(path.parent.joinpath("state").glob("*-*.json"))
    pm.save_project(project, notify=False)
    ps.collect_unreferenced_components(project.project_dir)
    assert all(item.exists() for item in legacy_paths)
    pm.save_project(project, notify=False)
    ps.collect_unreferenced_components(project.project_dir)
    assert all(item.exists() for item in legacy_paths)
    assert ps.read_prompt_history(stale) == history
    del stale
    gc.collect()
    ps.collect_unreferenced_components(project.project_dir)
    assert detached.generation_params["prompt"] == "original a"
    detached_path = path.parent / detached._generation_descriptor["path"]
    assert detached_path.exists()
    project_dir = project.project_dir
    del detached, project
    gc.collect()
    ps.collect_unreferenced_components(project_dir)
    assert not any(item.exists() for item in legacy_paths)
    ps.validate_storage(project_dir, json.loads(path.read_bytes()))


@pytest.mark.parametrize("boundary", ["child", "index", "provenance", "validation", "root", "backup"])
def test_failed_repack_preserves_old_root_and_collects_short_orphans(tmp_path, monkeypatch, boundary):
    project, path, _ = stored_project(tmp_path)
    before = path.read_bytes()
    shadow = copy.deepcopy(project._raw_data)
    old_files = {item: item.read_bytes() for item in (path.parent / "state").glob("*.json")}
    publish = ps._publish_component_bytes
    def fail(project_dir, name, payload, **kwargs):
        result = publish(project_dir, name, payload, **kwargs)
        if ((boundary == "child" and name == "history_entry")
                or (boundary == "index" and name == "prompt_history")
                or (boundary == "provenance" and name.startswith("provenance_"))):
            raise OSError("injected repack failure")
        if boundary == "validation" and name.startswith("provenance_"):
            (Path(project_dir) / result["path"]).write_bytes(b"damaged")
        return result
    monkeypatch.setattr(ps, "_publish_component_bytes", fail)
    if boundary == "root":
        monkeypatch.setattr(pm, "atomic_replace", lambda *_: (_ for _ in ()).throw(OSError("injected")))
    if boundary == "backup":
        digest = hashlib.sha256(before).hexdigest()
        backup = path.parent / "state" / f"{digest}.bak"
        backup.write_bytes(b"wrong existing backup")
    with pytest.raises((OSError, ps.ProjectStorageError)):
        pm.save_project(project, notify=False)
    assert path.read_bytes() == before and project._raw_data == shadow
    assert all(item.read_bytes() == payload for item, payload in old_files.items())
    ps.collect_unreferenced_components(project.project_dir)
    assert set((path.parent / "state").glob("*.json")) == set(old_files)


def test_identical_components_share_content_and_detached_lease(tmp_path):
    project = pm.create_project("Shared", base_dir=str(tmp_path))
    project.assets = [Asset(asset_id=i, generation_params={"prompt": "shared"}) for i in ("a", "b")]
    pm.save_project(project, notify=False)
    descriptors = [asset._generation_descriptor for asset in project.assets]
    assert descriptors[0] == descriptors[1]
    stale = pm.load_project(project.project_dir)
    detached = stale.assets[0]
    del stale
    for asset in project.assets:
        asset.generation_params = {"prompt": "different " + asset.asset_id}
    pm.save_project(project, notify=False)
    pm.save_project(project, notify=False)
    shared = Path(project.project_dir) / descriptors[0]["path"]
    ps.collect_unreferenced_components(project.project_dir)
    assert shared.exists() and detached.generation_params == {"prompt": "shared"}
    del detached
    gc.collect()
    assert shared.name in ps.collect_unreferenced_components(project.project_dir)


def test_existing_shared_destination_corruption_fails_closed(tmp_path):
    project, path, _ = stored_project(tmp_path)
    desc = project.assets[0]._generation_descriptor
    short = path.parent / "state" / f"{desc['sha256']}.json"
    short.write_bytes(b"corrupt destination")
    before = path.read_bytes()
    with pytest.raises(ps.ProjectStorageError, match="mismatch"):
        pm.save_project(project, notify=False)
    assert path.read_bytes() == before


def test_first_component_save_at_183_character_directory(tmp_path, monkeypatch):
    name = "p" * (183 - len(str(tmp_path)) - 1)
    assert name
    project = pm.create_project(name, base_dir=str(tmp_path))
    root = Path(project.project_dir)
    assert len(str(root)) == 183
    original = (root / "project.json").read_bytes()
    replace = ps.atomic_replace
    def bounded_replace(source, target):
        assert len(str(source)) <= 259
        assert len(str(target)) <= 259
        replace(source, target)
    monkeypatch.setattr(ps, "atomic_replace", bounded_replace)
    project.assets = [Asset(asset_id="asset", generation_params={"prompt": "new"})]
    pm.save_project(project, notify=False)
    assert pm.load_project(project.project_dir).assets[0].generation_params == {"prompt": "new"}
    backups = list((root / "state").glob("*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    old_backup = root / "state" / "legacy" / "old.json"
    old_backup.parent.mkdir()
    old_backup.write_bytes(b"prior backup")
    ps.collect_unreferenced_components(project.project_dir)
    assert backups[0].read_bytes() == original
    assert old_backup.read_bytes() == b"prior backup"
