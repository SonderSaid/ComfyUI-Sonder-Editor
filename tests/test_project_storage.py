"""Frozen-store proofs: durable content, races, and publication boundaries."""
import copy
import json
import shutil
from pathlib import Path

import pytest

from server import project_manager as pm, project_storage as ps
from server.timeline_state import GenerationJob, Asset, Scene


def legacy_project(tmp_path):
    project = pm.create_project("Legacy", base_dir=str(tmp_path))
    path = Path(project.project_dir) / "project.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["metadata"] = {"other": {"future": [7]}, "prompt_history": [
        {"hash": "a", "sections": [{"future": {"value": [1, 2]}}]}]}
    path.write_text(json.dumps(data, indent=3), encoding="utf-8")
    return pm.load_project(project.project_dir), path


def test_whole_folder_portability_and_root_only_copy_refusal(tmp_path):
    project, path = legacy_project(tmp_path)
    project.assets = [Asset(asset_id="portable", generation_params={"fps": 24, "prompt": "kept"})]
    pm.save_project(project, notify=False)
    destination = tmp_path / "portable-copy"
    shutil.copytree(path.parent, destination)
    copied = pm.load_project(str(destination))
    assert ps.read_prompt_history(copied) == ps.read_prompt_history(project)
    assert ps.hydrate_asset(copied.assets[0]).generation_params == {"fps": 24, "prompt": "kept"}
    root_only = tmp_path / "incomplete-copy"
    root_only.mkdir()
    shutil.copy2(path, root_only / "project.json")
    with pytest.raises(ps.ProjectStorageError):
        pm.load_project(str(root_only))


def test_history_no_rewrite_at_rest_and_no_silent_drop_on_legacy_layout_write(tmp_path):
    project, path = legacy_project(tmp_path)
    original = path.read_bytes()
    expected = copy.deepcopy(project.metadata)
    assert ps.read_prompt_history(project.project_dir) == expected["prompt_history"]
    pm.list_projects(str(tmp_path))
    assert path.read_bytes() == original
    pm.save_project(project, bump_modified_at=False, notify=False)
    assert "storage" not in json.loads(path.read_bytes())
    assert pm.load_project(project.project_dir).metadata == expected


def test_history_migration_backup_and_roundtrip(tmp_path):
    project, path = legacy_project(tmp_path)
    original = path.read_bytes()
    expected = copy.deepcopy(project.metadata)
    pm.save_project(project, notify=False)
    data = json.loads(path.read_bytes())
    assert "prompt_history" not in data["metadata"]
    assert data["metadata"]["other"] == expected["other"]
    assert ps.read_prompt_history(pm.load_project(project.project_dir)) == expected["prompt_history"]
    backups = list((path.parent / "state" / "legacy").glob("*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


def test_undeclared_history_uses_disk_descriptor_for_independently_loaded_bare_writer(tmp_path):
    project, path = legacy_project(tmp_path)
    stale_legacy = pm.load_project(project.project_dir)
    pm.save_project(project, notify=False)
    stale_split = pm.load_project(project.project_dir)
    writer = pm.load_project(project.project_dir)
    updated = ps.read_prompt_history(writer) + [{"hash": "new", "sections": []}]
    ps.stage_prompt_history(writer, updated)
    pm.save_project(writer, notify=False)
    for stale in (stale_legacy, stale_split):
        stale.name = "bare writer"
        pm.save_project(stale, notify=False)
        assert ps.read_prompt_history(stale.project_dir) == updated


def test_staged_history_conflict_refuses_lost_append_without_opt_in_cas(tmp_path):
    project, path = legacy_project(tmp_path)
    pm.save_project(project, notify=False)
    first = pm.load_project(project.project_dir)
    second = pm.load_project(project.project_dir)
    ps.stage_prompt_history(first, ps.read_prompt_history(first) + [{"hash": "first"}])
    ps.stage_prompt_history(second, ps.read_prompt_history(second) + [{"hash": "second"}])
    pm.save_project(first, notify=False)
    before = path.read_bytes()
    with pytest.raises(pm.ProjectVersionConflict):
        pm.save_project(second, notify=False)
    assert path.read_bytes() == before


def test_no_bump_staging_refused(tmp_path):
    project, path = legacy_project(tmp_path)
    ps.stage_prompt_history(project, [{"hash": "new"}])
    before = path.read_bytes()
    with pytest.raises(ps.ProjectStorageError, match="no-bump"):
        pm.save_project(project, bump_modified_at=False, notify=False)
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure", ["component", "root"])
def test_failure_before_root_commit_keeps_exact_legacy_authority(tmp_path, monkeypatch, failure):
    project, path = legacy_project(tmp_path)
    before = path.read_bytes()
    shadow = copy.deepcopy(project._raw_data)
    target = ps if failure == "component" else pm
    def fail(*args, **kwargs):
        raise OSError("injected publication failure")
    monkeypatch.setattr(target, "atomic_replace", fail)
    with pytest.raises(OSError, match="injected"):
        pm.save_project(project, notify=False)
    assert path.read_bytes() == before
    assert project._raw_data == shadow
    assert not list(path.parent.rglob("*.tmp"))


@pytest.mark.parametrize("damage", ["missing", "length", "hash"])
def test_corrupt_history_component_fails_closed(tmp_path, damage):
    project, path = legacy_project(tmp_path)
    pm.save_project(project, notify=False)
    descriptor = json.loads(path.read_bytes())["storage"]["components"]["prompt_history"]
    component = path.parent / descriptor["path"]
    if damage == "missing":
        component.unlink()
    elif damage == "length":
        component.write_bytes(b"[]")
    else:
        payload = component.read_bytes()
        component.write_bytes(b" " + payload[1:])
    with pytest.raises(ps.ProjectStorageError, match="prompt_history"):
        pm.load_project(project.project_dir)


def test_unknown_storage_format_is_never_legacy(tmp_path):
    project, path = legacy_project(tmp_path)
    data = json.loads(path.read_bytes())
    data["storage"] = {"format_version": 99, "components": {}}
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ps.ProjectStorageError):
        pm.load_project(project.project_dir)
    with pytest.raises(ps.ProjectStorageError):
        pm.save_project(project, notify=False)
    assert path.read_bytes() == before


def test_gc_keeps_current_previous_and_permanent_legacy_backup(tmp_path):
    project, path = legacy_project(tmp_path)
    pm.save_project(project, notify=False)
    for index in range(4):
        ps.stage_prompt_history(project, [{"hash": str(index)}])
        pm.save_project(project, notify=False)
    removed = ps.collect_unreferenced_components(project.project_dir)
    assert removed
    assert ps.read_prompt_history(project.project_dir) == [{"hash": "3"}]
    assert list((path.parent / "state" / "legacy").glob("*.json"))
    data = json.loads(path.read_bytes())
    for descriptor in data["storage"]["recovery_previous"].values():
        assert (path.parent / descriptor["path"]).is_file()


def test_staging_alone_persists_history_on_project_without_inline_history(tmp_path):
    project = pm.create_project("Empty", base_dir=str(tmp_path))
    ps.stage_prompt_history(project, [{"hash": "new"}])
    pm.save_project(project, expected_modified_at=project.modified_at, notify=False)
    assert ps.read_prompt_history(project.project_dir) == [{"hash": "new"}]
    assert not project._staged_components


def test_missing_required_descriptor_cannot_become_empty_history(tmp_path):
    project, path = legacy_project(tmp_path)
    pm.save_project(project, notify=False)
    data = json.loads(path.read_bytes())
    data["storage"]["components"].clear()
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ps.ProjectStorageError, match="descriptor is missing"):
        pm.load_project(project.project_dir)
    with pytest.raises(ps.ProjectStorageError, match="descriptor is missing"):
        ps.read_prompt_history(project.project_dir)


def test_save_refuses_component_deleted_after_load(tmp_path):
    project, path = legacy_project(tmp_path)
    pm.save_project(project, notify=False)
    descriptor = json.loads(path.read_bytes())["storage"]["components"]["prompt_history"]
    (path.parent / descriptor["path"]).unlink()
    before = path.read_bytes()
    with pytest.raises(ps.ProjectStorageError, match="prompt_history"):
        pm.save_project(project, notify=False)
    assert path.read_bytes() == before


def test_queue_roundtrip_and_no_resurrection_by_overlay(tmp_path):
    project, path = legacy_project(tmp_path)
    job = GenerationJob(job_id="terminal", status="completed",
        params={"snapshot_version": 1}, prompt_sections=[{"prompt": "frozen"}],
        reference_input_snapshots=[{"kind": "asset", "value": {"asset_id": "kept"}}])
    expected = job.to_dict()
    project.generation_queue.append(job)
    pm.save_project(project, notify=False)
    loaded = pm.load_project(project.project_dir)
    assert loaded.generation_queue[0]._frozen_unhydrated
    from server.frozen_prompt import classify_frozen_prompt, FrozenPromptEnvelopeError
    with pytest.raises(FrozenPromptEnvelopeError, match="hydrated"):
        classify_frozen_prompt(loaded.generation_queue[0])
    with pytest.raises(ps.ProjectStorageError, match="hydrated"):
        list(loaded.generation_queue[0].prompt_sections)
    pm.save_project(loaded, notify=False)
    root = json.loads(path.read_bytes())
    assert all(field not in root["generation_queue"][0] for field in ps.FROZEN_JOB_FIELDS)
    assert ps.resolve_queue_job(loaded, "terminal").to_dict() == expected
    pm.save_project(loaded, notify=False)
    assert all(field not in json.loads(path.read_bytes())["generation_queue"][0]
               for field in ps.FROZEN_JOB_FIELDS)


def test_queue_no_bump_legacy_write_keeps_complete_inline_payload(tmp_path):
    project, path = legacy_project(tmp_path)
    project.generation_queue = [GenerationJob(prompt_sections=[{"prompt": "keep"}])]
    pm.save_project(project, bump_modified_at=False, notify=False)
    data = json.loads(path.read_bytes())
    assert "storage" not in data
    assert data["generation_queue"][0]["prompt_sections"] == [{"prompt": "keep"}]


def test_queue_frozen_edit_refused_and_header_edit_preserved(tmp_path):
    project, path = legacy_project(tmp_path)
    project.generation_queue = [GenerationJob(job_id="one", prompt_sections=[{"prompt": "keep"}])]
    pm.save_project(project, notify=False)
    loaded = pm.load_project(project.project_dir)
    job = ps.resolve_queue_job(loaded, "one")
    job.status = "completed"
    pm.save_project(loaded, notify=False)
    job.prompt_sections[0]["prompt"] = "changed"
    before = path.read_bytes()
    with pytest.raises(ps.ProjectStorageError, match="cannot be modified"):
        pm.save_project(loaded, notify=False)
    assert path.read_bytes() == before


def test_no_bump_queue_membership_removal_keeps_history_and_remaining_jobs(tmp_path):
    project, path = legacy_project(tmp_path)
    project.generation_queue = [GenerationJob(job_id="one"), GenerationJob(job_id="two")]
    pm.save_project(project, notify=False)
    loaded = pm.load_project(project.project_dir)
    loaded.generation_queue.pop(0)
    pm.save_project(loaded, bump_modified_at=False, notify=False)
    data = json.loads(path.read_bytes())
    assert ps.job_component_name("one") not in data["storage"]["components"]
    assert ps.resolve_queue_job(pm.load_project(project.project_dir), "two") is not None
    assert ps.read_prompt_history(project.project_dir)


def test_missing_queue_partition_never_reinterprets_snapshots_as_empty(tmp_path):
    project, path = legacy_project(tmp_path)
    project.generation_queue = [GenerationJob(job_id="one", prompt_sections=[{"prompt": "frozen"}])]
    pm.save_project(project, notify=False)
    data = json.loads(path.read_bytes())
    del data["storage"]["partitions"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ps.ProjectStorageError, match="partition declaration is missing"):
        pm.load_project(project.project_dir)


def test_duplicate_job_ids_refuse_migration_without_losing_either_snapshot(tmp_path):
    project, path = legacy_project(tmp_path)
    project.generation_queue = [GenerationJob(job_id="same", prompt_sections=[{"prompt": text}])
                                for text in ("first", "second")]
    pm.save_project(project, bump_modified_at=False, notify=False)
    before = path.read_bytes()
    with pytest.raises(ps.ProjectStorageError, match="unique nonempty"):
        pm.save_project(project, notify=False)
    assert path.read_bytes() == before


@pytest.mark.parametrize("fail_after", [1, 2])
def test_crash_after_each_queue_publication_keeps_legacy_root(tmp_path, monkeypatch, fail_after):
    project, path = legacy_project(tmp_path)
    project.generation_queue = [GenerationJob(job_id=str(index)) for index in range(2)]
    pm.save_project(project, bump_modified_at=False, notify=False)
    before = path.read_bytes()
    publish = ps.publish_component
    published = []
    def publish_then_crash(project_dir, name, value):
        descriptor = publish(project_dir, name, value)
        if name.startswith("queue_"):
            published.append(descriptor)
            if len(published) == fail_after:
                raise OSError("crashed after queue publication")
        return descriptor
    monkeypatch.setattr(ps, "publish_component", publish_then_crash)
    with pytest.raises(OSError, match="crashed"):
        pm.save_project(project, notify=False)
    assert path.read_bytes() == before
    assert len(pm.load_project(project.project_dir).generation_queue) == 2


def test_cold_only_provenance_preserves_all_generated_predicates_and_wire(tmp_path):
    from server import project_commit as pc, routes
    project, path = legacy_project(tmp_path)
    full = {"editor_export": {"snapshot": True}, "future": {"x": [1]}}
    project.assets = [Asset(asset_id="snapshot", generation_params=copy.deepcopy(full))]
    pm.save_project(project, notify=False)
    loaded = pm.load_project(project.project_dir)
    asset = loaded.assets[0]
    assert asset.inline_generation_params == {}
    assert pc._asset_is_generated(asset)
    assert pc._asset_has_generated_registration(asset)
    assert not pc._same_path_placeholder_can_upgrade(asset)
    assert not routes._same_path_import_placeholder(asset)
    payload = routes._asset_payload(loaded, asset, lean=True)
    assert asset._generation_unhydrated
    assert "generation_params" not in payload
    assert payload["generation_summary"]["has_embedded_workflow"] is None
    assert ps.read_asset_provenance(project.project_dir, asset.asset_id)["generation_params"] == full
    pm.save_project(loaded, notify=False)
    assert ps.hydrate_asset(pm.load_project(project.project_dir).assets[0]).generation_params == full


@pytest.mark.parametrize("replace", [False, True])
def test_partial_provenance_write_refuses_instead_of_dropping_cold_keys(tmp_path, replace):
    project, path = legacy_project(tmp_path)
    project.assets = [Asset(asset_id="a", generation_params={"fps": 24, "prompt": "irreplaceable"})]
    pm.save_project(project, notify=False)
    loaded = pm.load_project(project.project_dir)
    if replace:
        object.__setattr__(loaded.assets[0], "generation_params", dict(loaded.assets[0].generation_params_for_storage))
    else:
        loaded.assets[0].generation_params_for_storage["fps"] = 30
    before = path.read_bytes()
    with pytest.raises(ps.ProjectStorageError, match="unhydrated"):
        pm.save_project(loaded, notify=False)
    assert path.read_bytes() == before


def test_new_take_hydrates_complete_metadata_and_inline_color_keys_stay_available(tmp_path):
    from server.timeline_export import _place_video_take
    project, path = legacy_project(tmp_path)
    full = {"codec": "libx264", "pix_fmt": "yuv420p", "color_managed": True,
            "editor_export": {"prompt": "frozen"}, "unknown": [1, 2]}
    project.assets = [Asset(asset_id="a", frame_count=12, generation_params=copy.deepcopy(full))]
    project.scenes = [Scene(scene_id="scene", duration_frames=24)]
    pm.save_project(project, notify=False)
    loaded = pm.load_project(project.project_dir)
    assert loaded.assets[0].generation_params["codec"] == "libx264"
    clip = _place_video_take(loaded, loaded.scenes[0], loaded.assets[0], 0, 12)
    assert clip.take_metadata == full
    assert clip.generation_params == full


def test_stale_bare_asset_writer_preserves_newer_cold_provenance(tmp_path):
    project, path = legacy_project(tmp_path)
    project.assets = [Asset(asset_id="a", generation_params={"prompt": "first"})]
    pm.save_project(project, notify=False)
    stale = pm.load_project(project.project_dir)
    writer = pm.load_project(project.project_dir)
    ps.hydrate_asset(writer.assets[0]).generation_params["prompt"] = "second"
    pm.save_project(writer, notify=False)
    stale.name = "stale bare edit"
    pm.save_project(stale, notify=False)
    assert ps.hydrate_asset(pm.load_project(project.project_dir).assets[0]).generation_params["prompt"] == "second"


@pytest.mark.parametrize("legacy_stale", [False, True])
def test_second_save_of_stale_hydrated_asset_cannot_overwrite_newer_provenance(tmp_path, legacy_stale):
    project, path = legacy_project(tmp_path)
    project.assets = [Asset(asset_id="a", generation_params={"prompt": "first"})]
    pm.save_project(project, bump_modified_at=False, notify=False)
    stale_legacy = pm.load_project(project.project_dir)
    pm.save_project(project, notify=False)
    stale = stale_legacy if legacy_stale else pm.load_project(project.project_dir)
    writer = pm.load_project(project.project_dir)
    ps.hydrate_asset(stale.assets[0])
    ps.hydrate_asset(writer.assets[0]).generation_params["prompt"] = "second"
    pm.save_project(writer, notify=False)
    stale.name = "unrelated stale edit"
    pm.save_project(stale, notify=False)
    before = path.read_bytes()
    stale.assets[0].generation_params["extra"] = "new"
    with pytest.raises(pm.ProjectVersionConflict):
        pm.save_project(stale, notify=False)
    assert path.read_bytes() == before
    assert ps.hydrate_asset(pm.load_project(project.project_dir).assets[0]).generation_params == {"prompt": "second"}


@pytest.mark.parametrize("flag", [True, False, None])
def test_lean_summary_and_batch_revision_cover_inline_fields(tmp_path, flag):
    from server.routes import _asset_payload
    project, path = legacy_project(tmp_path)
    project.assets = [Asset(asset_id="a", generation_params={"fps":24,
        "editor_export": {"has_embedded_workflow": flag}, "cold": "kept"})]
    pm.save_project(project, notify=False)
    loaded = pm.load_project(project.project_dir)
    before = _asset_payload(loaded, loaded.assets[0], lean=True)
    batch = ps.read_asset_provenance_batch(project.project_dir, ["a"])
    assert before["generation_summary"]["has_embedded_workflow"] is flag
    assert before["provenance_revision"] == batch["revisions"]["a"]
    assert "generation_params" not in before
    ps.hydrate_asset(loaded.assets[0]).generation_params["fps"] = 30
    pm.save_project(loaded, notify=False)
    after = _asset_payload(loaded, loaded.assets[0], lean=True)
    assert before["provenance_revision"] != after["provenance_revision"]
    assert ps.read_asset_provenance_batch(project.project_dir, ["a"])["revisions"]["a"] == after["provenance_revision"]


def test_provenance_marker_cannot_be_reinterpreted_without_partition(tmp_path):
    project, path = legacy_project(tmp_path)
    project.assets = [Asset(asset_id="a", generation_params={"prompt": "irreplaceable"})]
    pm.save_project(project, notify=False)
    data = json.loads(path.read_bytes())
    data["storage"]["partitions"].remove("assets")
    del data["storage"]["components"][ps.asset_component_name("a")]
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ps.ProjectStorageError, match="marker requires"):
        pm.load_project(project.project_dir)
    with pytest.raises(ps.ProjectStorageError, match="marker requires"):
        pm.save_project(project, notify=False)
    assert path.read_bytes() == before
