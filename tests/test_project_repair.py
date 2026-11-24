"""Check and repair of a project whose stored components are missing or damaged."""
import asyncio
import copy
import gc
import hashlib
import json
import os
from pathlib import Path

import pytest

from server import project_manager as pm, project_repair as repair, project_storage as ps
from server.project_file_lock import ProjectBusyError
from server.timeline_state import Asset, GenerationJob
from test_project_mutation_pipeline_backend import DummyRequest, _load_route_module, _route_handler


def make(tmp_path, name="Repair"):
    project = pm.create_project(name, base_dir=str(tmp_path))
    project.assets = [Asset(asset_id=i, name=f"{i}.mp4", path=f"media/{i}.mp4",
                            generation_params={"prompt": i, "workflow": {"n": i}, "fps": 24.0})
                      for i in ("a", "b")]
    project.generation_queue = [GenerationJob(job_id="j1"), GenerationJob(job_id="j2", status="running")]
    ps.stage_prompt_history(project, [{"hash": "one"}, {"hash": "two", "future": "kept"}])
    pm.save_project(project, notify=False)
    project_dir = project.project_dir
    del project
    gc.collect()
    return project_dir


def document(project_dir):
    return json.loads((Path(project_dir) / "project.json").read_bytes())


def component(project_dir, name):
    return Path(project_dir) / document(project_dir)["storage"]["components"][name]["path"]


def entries(project_dir):
    index = document(project_dir)["storage"]["components"]["prompt_history"]
    return [Path(project_dir) / entry["path"]
            for entry in ps.read_component(project_dir, "prompt_history", index)["entries"]]


def damage(path, how):
    if how == "absent":
        path.unlink()
    else:
        path.write_bytes(b"damaged")


def tree(project_dir):
    return {path: path.read_bytes() for path in Path(project_dir).rglob("*") if path.is_file()}


def fix(project_dir):
    report = repair.inspect_storage(project_dir)
    assert report["status"] == "damaged"
    return report, repair.repair_storage(project_dir, report["digest"])


def assert_usable(project_dir):
    """Loads, hydrates, saves and collects."""
    project = pm.load_project(project_dir)
    for asset in project.assets:
        asset.generation_params
    for job in project.generation_queue:
        ps.hydrate_job(project, job)
    ps.read_prompt_history(project)
    project.name = "edited after the repair"
    pm.save_project(project, expected_modified_at=project.modified_at, notify=False)
    ps.collect_unreferenced_components(project_dir)
    return pm.load_project(project_dir)


ASSET_A, JOB_1 = ps.asset_component_name("a"), ps.job_component_name("j1")


def test_a_healthy_project_is_reported_and_not_touched(tmp_path):
    project_dir = make(tmp_path)
    before = tree(project_dir)
    report = repair.inspect_storage(project_dir)
    assert (report["status"], report["problems"]) == ("healthy", [])
    assert tree(project_dir) == before
    with pytest.raises(repair.RepairRefused, match="healthy"):
        repair.repair_storage(project_dir, report["digest"])
    assert tree(project_dir) == before


def test_a_legacy_project_has_nothing_to_check(tmp_path):
    from legacy_projects import create_legacy_project
    project = create_legacy_project("Legacy", tmp_path)
    assert repair.inspect_storage(project.project_dir)["status"] == "healthy"


@pytest.mark.parametrize("how", ["absent", "corrupt"])
def test_lost_details_keep_the_asset(tmp_path, how):
    project_dir = make(tmp_path)
    data = document(project_dir)
    data["assets"][0]["generation_summary"] = {"has_embedded_workflow": True}  # gone with the details
    (Path(project_dir) / "project.json").write_text(json.dumps(data), encoding="utf-8")
    damage(component(project_dir, ASSET_A), how)
    with pytest.raises(ps.ProjectStorageError):
        pm.load_project(project_dir)
    report, result = fix(project_dir)
    assert report["problems"] == [
        {"kind": "details", "state": how, "outcome": "dropped", "asset_id": "a", "name": "a.mp4"}]
    record = document(project_dir)["assets"][0]
    assert "has_frozen_provenance" not in record and record["generation_summary"] == {"has_embedded_workflow": None}
    project = assert_usable(project_dir)
    assert [asset.asset_id for asset in project.assets] == ["a", "b"]
    assert project.assets[0].path == "media/a.mp4"
    assert project.assets[0].generation_params == {"fps": 24.0}  # what was never cold
    assert project.assets[1].generation_params["prompt"] == "b"
    assert result["left_in_place"] == []


@pytest.mark.parametrize("how", ["absent", "corrupt"])
def test_a_queue_job_without_its_snapshot_is_removed(tmp_path, how):
    project_dir = make(tmp_path)
    damage(component(project_dir, JOB_1), how)
    # Both jobs carry the same empty snapshot, so one file backs both.
    report, _ = fix(project_dir)
    assert [(item["kind"], item["job_id"], item["status"]) for item in report["problems"]] == [
        ("job", "j1", "pending"), ("job", "j2", "running")]
    assert assert_usable(project_dir).generation_queue == []


@pytest.mark.parametrize("how", ["absent", "corrupt"])
def test_lost_history_entries_keep_the_rest(tmp_path, how):
    project_dir = make(tmp_path)
    damage(entries(project_dir)[0], how)
    report, _ = fix(project_dir)
    assert report["problems"] == [
        {"kind": "history", "state": "entries", "outcome": "entries_dropped", "kept": 1, "lost": 1}]
    assert ps.read_prompt_history(assert_usable(project_dir)) == [{"hash": "two", "future": "kept"}]


def test_a_lost_index_is_restored_from_the_same_bytes_under_the_other_spelling(tmp_path):
    project_dir = make(tmp_path)
    index = component(project_dir, "prompt_history")
    index.rename(index.with_name("prompt_history-" + index.name))
    report, _ = fix(project_dir)
    assert report["problems"][0]["outcome"] == "restored"
    assert [entry["hash"] for entry in ps.read_prompt_history(assert_usable(project_dir))] == ["one", "two"]


def test_a_lost_index_falls_back_to_a_retained_one(tmp_path):
    project_dir = make(tmp_path)
    project = pm.load_project(project_dir)
    ps.stage_prompt_history(project, [{"hash": "newest"}])
    pm.save_project(project, notify=False)
    del project
    component(project_dir, "prompt_history").unlink()
    report, _ = fix(project_dir)
    assert report["problems"] == [
        {"kind": "history", "state": "absent", "outcome": "previous", "kept": 2, "lost": 0}]
    assert [entry["hash"] for entry in ps.read_prompt_history(assert_usable(project_dir))] == ["one", "two"]


def test_a_lost_index_is_salvaged_from_the_newest_one_left_in_the_folder(tmp_path):
    project_dir = make(tmp_path)
    old = ps.publish_history(project_dir, [{"hash": "older"}], format_version=3)
    new = ps.publish_history(project_dir, [{"hash": "newer"}, {"hash": "gone"}], format_version=3)
    os.utime(Path(project_dir) / old["path"], (1, 1))
    (Path(project_dir) / ps.read_component(project_dir, "prompt_history", new)["entries"][1]["path"]).unlink()
    data = document(project_dir)
    for group in ("recovery_previous", "authoring_previous"):
        data["storage"][group].pop("prompt_history", None)
    (Path(project_dir) / "project.json").write_text(json.dumps(data), encoding="utf-8")
    component(project_dir, "prompt_history").unlink()
    report, _ = fix(project_dir)
    assert report["problems"] == [
        {"kind": "history", "state": "absent", "outcome": "salvaged", "kept": 1, "lost": 1}]
    assert ps.read_prompt_history(assert_usable(project_dir)) == [{"hash": "newer"}]


def test_a_lost_index_with_nothing_to_use_leaves_an_empty_history_and_keeps_its_entries(tmp_path):
    project_dir = make(tmp_path)
    children = entries(project_dir)
    data = document(project_dir)
    for group in ("recovery_previous", "authoring_previous"):
        data["storage"][group] = {}
    (Path(project_dir) / "project.json").write_text(json.dumps(data), encoding="utf-8")
    component(project_dir, "prompt_history").unlink()
    for path in (Path(project_dir) / "state").glob("*.json"):  # the empty index a new project starts with
        if path not in children and b"prompt_history_index" in path.read_bytes():
            path.unlink()
    report, _ = fix(project_dir)
    assert report["problems"][0]["outcome"] == "emptied"
    assert ps.read_prompt_history(assert_usable(project_dir)) == []
    for _ in range(3):  # edits and sweeps with no delay still leave what only the old document knew
        project = pm.load_project(project_dir)
        ps.stage_prompt_history(project, [{"hash": "later"}])
        pm.save_project(project, notify=False)
        del project
        gc.collect()
        ps.collect_unreferenced_components(project_dir, retention_seconds=0)
    assert all(child.exists() for child in children)


def test_a_hash_valid_payload_its_reader_refuses_counts_as_corrupt(tmp_path):
    project_dir = make(tmp_path)
    data = document(project_dir)
    data["storage"]["components"][ASSET_A] = ps.publish_component(project_dir, ASSET_A, [1], format_version=3)
    data["storage"]["components"][JOB_1] = ps.publish_component(project_dir, JOB_1, {}, format_version=3)
    (Path(project_dir) / "project.json").write_text(json.dumps(data), encoding="utf-8")
    report, _ = fix(project_dir)
    assert [(item["kind"], item["state"]) for item in report["problems"]] == [("details", "corrupt"), ("job", "corrupt")]
    assert [job.job_id for job in assert_usable(project_dir).generation_queue] == ["j2"]


def test_a_component_no_record_names_is_dropped(tmp_path):
    project_dir = make(tmp_path)
    data = document(project_dir)
    extra = ps.asset_component_name("gone")
    data["storage"]["components"][extra] = ps.publish_component(project_dir, extra, {"x": 1}, format_version=3)
    (Path(project_dir) / "project.json").write_text(json.dumps(data), encoding="utf-8")
    component(project_dir, extra).unlink()
    report, _ = fix(project_dir)
    assert report["problems"] == [
        {"kind": "unused", "state": "absent", "outcome": "dropped", "component": extra}]
    assert_usable(project_dir)


def test_broken_retained_components_are_dropped_with_the_repair(tmp_path):
    project_dir = make(tmp_path)
    data = document(project_dir)
    ghost = {"path": "state/" + "e" * 64 + ".json", "sha256": "e" * 64, "bytes": 3}
    data["storage"]["recovery_previous"]["prompt_history"] = ghost
    data["storage"]["authoring_previous"][ASSET_A] = {"path": "nonsense"}
    (Path(project_dir) / "project.json").write_text(json.dumps(data), encoding="utf-8")
    component(project_dir, ASSET_A).unlink()
    fix(project_dir)
    storage = document(project_dir)["storage"]
    assert "prompt_history" not in storage["recovery_previous"]
    assert ASSET_A not in storage["authoring_previous"]
    assert_usable(project_dir)


def test_two_assets_sharing_one_damaged_file_both_lose_it_and_nothing_else_moves(tmp_path):
    project_dir = make(tmp_path)
    project = pm.load_project(project_dir)
    for asset in project.assets:
        asset.generation_params = {"prompt": "same"}
    pm.save_project(project, notify=False)
    del project
    shared = component(project_dir, ASSET_A)
    assert shared == component(project_dir, ps.asset_component_name("b"))
    damage(shared, "corrupt")
    report, _ = fix(project_dir)
    assert [item["asset_id"] for item in report["problems"]] == ["a", "b"]
    assert shared.with_suffix(".bad").read_bytes() == b"damaged" and not shared.exists()
    # The same content can be published again: the damaged file no longer stands in its way.
    project = pm.load_project(project_dir)
    project.assets[0].generation_params = {"prompt": "same"}
    pm.save_project(project, notify=False)
    assert pm.load_project(project_dir).assets[0].generation_params["prompt"] == "same"


def test_a_wrong_descriptor_over_a_good_file_is_not_repaired(tmp_path):
    project_dir = make(tmp_path)
    data = document(project_dir)
    data["storage"]["components"][ASSET_A]["bytes"] += 1
    (Path(project_dir) / "project.json").write_text(json.dumps(data), encoding="utf-8")
    before = tree(project_dir)
    assert repair.inspect_storage(project_dir)["reason"] == "invalid_storage"
    with pytest.raises(repair.RepairRefused):
        repair.repair_storage(project_dir, "")
    assert {path: data for path, data in tree(project_dir).items() if path.name != "project.lock"} == {
        path: data for path, data in before.items() if path.name != "project.lock"}


# ---------------------------------------------------------------------------
# What is not damage
# ---------------------------------------------------------------------------

def test_a_file_that_cannot_be_read_now_is_not_missing(tmp_path, monkeypatch):
    project_dir = make(tmp_path)
    target = component(project_dir, ASSET_A)
    component(project_dir, "prompt_history").unlink()  # beside real damage: still not repairable
    before = tree(project_dir)
    real = ps._read_bytes

    def locked(path):
        if os.path.normcase(str(path)) == os.path.normcase(str(target)):
            raise PermissionError("held by another program")
        return real(path)

    monkeypatch.setattr(ps, "_read_bytes", locked)
    report = repair.inspect_storage(project_dir)
    assert (report["status"], report["reason"], report["problems"]) == ("unrepairable", "unavailable", [])
    with pytest.raises(repair.RepairRefused, match="unavailable"):
        repair.repair_storage(project_dir, report["digest"])
    assert tree(project_dir) == before


@pytest.mark.parametrize("how", ["missing", "unlistable"])
def test_a_state_folder_that_cannot_be_listed_is_not_empty(tmp_path, monkeypatch, how):
    project_dir = make(tmp_path)
    if how == "missing":
        (Path(project_dir) / "state").rename(tmp_path / "moved")
    else:
        monkeypatch.setattr(ps, "_component_entries", lambda _root: (_ for _ in ()).throw(OSError("drive gone")))
    report = repair.inspect_storage(project_dir)
    assert (report["status"], report["reason"]) == ("unrepairable", "state_unreadable")


@pytest.mark.parametrize("content, reason", [
    (b"{", "invalid_json"), (b"[]", "invalid_json"), (b"\xff\xfe\x00", "invalid_json"),
    (b'{"storage": {"format_version": 99, "components": {}}}', "too_new"),
    (b'{"storage": {"format_version": 3, "components": {}}}', "invalid_storage"),
    (b'{"storage": []}', "invalid_storage"),
    (b'\xef\xbb\xbf{}', "invalid_json"),                 # the loader refuses a BOM
    ('{"name": "x"}'.encode("utf-16"), "invalid_json"),  # and anything but UTF-8
    (b'{"assets": null, "storage": {"format_version": 3, "components": {"prompt_history": {}}}}', "invalid_storage"),
    (b'{"assets": ["x"], "storage": {"format_version": 3, "components": {"prompt_history": {}}}}', "invalid_storage"),
    (b'{"storage": {"format_version": 3, "components": {"prompt_history": {}}, "partitions": [[]]}}', "invalid_storage"),
    (b'{"assets": [{"has_frozen_provenance": true}], "storage": {"format_version": 3, "components": '
     b'{"prompt_history": {"path": "x"}}, "partitions": ["assets"]}}', "invalid_storage"),
])
def test_documents_a_repair_must_leave_alone(tmp_path, content, reason):
    project_dir = make(tmp_path)
    (Path(project_dir) / "project.json").write_bytes(content)
    before = tree(project_dir)
    report = repair.inspect_storage(project_dir)
    assert (report["status"], report["reason"]) == ("unrepairable", reason)
    with pytest.raises(repair.RepairRefused):
        repair.repair_storage(project_dir, report["digest"])
    assert tree(project_dir) == before


def _edit(project_dir, change):
    data = document(project_dir)
    change(data)
    (Path(project_dir) / "project.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("special", ["reparse", "uncontained", "other_case"])
def test_a_file_that_is_there_but_is_not_an_ordinary_one_is_not_missing(tmp_path, monkeypatch, special):
    project_dir = make(tmp_path)
    target = component(project_dir, ASSET_A)
    real_entries, real_path = ps._component_entries, repair.project_state_path

    def listing(root):
        found = real_entries(root)
        info = found.pop(target.name)
        if special == "other_case":
            found[target.name.upper()] = info
        else:
            found[target.name] = os.stat_result((0o120777, *info[1:10])) if special == "reparse" else info
        return found

    monkeypatch.setattr(ps, "_component_entries", listing)
    if special == "uncontained":
        monkeypatch.setattr(repair, "project_state_path",
                            lambda d, name, **k: "" if name == target.name else real_path(d, name, **k))
    assert repair.inspect_storage(project_dir)["reason"] == "unavailable"


def test_a_load_refusal_this_check_has_no_repair_for_is_not_healthy(tmp_path):
    project_dir = make(tmp_path)
    good = ps.publish_component(project_dir, ASSET_A, {"x": 1}, format_version=3)
    _edit(project_dir, lambda data: data["storage"]["components"].update({"history_entry": good}))
    with pytest.raises(ps.ProjectStorageError):
        pm.load_project(project_dir)
    assert repair.inspect_storage(project_dir)["reason"] == "invalid_storage"


def test_a_momentary_refusal_does_not_make_a_healthy_project_invalid(tmp_path, monkeypatch):
    project_dir = make(tmp_path)
    real, calls = ps.validate_storage, []

    def once(*args):
        calls.append(1)
        if len(calls) == 1:
            raise ps.ProjectStorageError("held for a moment")
        return real(*args)

    monkeypatch.setattr(ps, "validate_storage", once)
    assert repair.inspect_storage(project_dir)["status"] == "healthy"


def test_a_queue_kept_inline_must_be_complete(tmp_path):
    project_dir = make(tmp_path)

    def inline(data):
        data["storage"]["partitions"] = ["assets"]
        for name in [name for name in data["storage"]["components"] if name.startswith("queue_")]:
            del data["storage"]["components"][name]

    _edit(project_dir, inline)
    assert repair.inspect_storage(project_dir)["reason"] == "invalid_storage"
    component(project_dir, ASSET_A).unlink()  # beside damage it could repair, too
    assert repair.inspect_storage(project_dir)["reason"] == "invalid_storage"


def test_a_retained_part_that_cannot_be_read_now_stops_a_repair_at_the_check(tmp_path, monkeypatch):
    project_dir = make(tmp_path)
    project = pm.load_project(project_dir)
    project.assets[1].generation_params = {"prompt": "changed"}
    pm.save_project(project, notify=False)
    del project
    old = Path(project_dir) / document(project_dir)["storage"]["recovery_previous"][ps.asset_component_name("b")]["path"]
    real = ps._read_bytes

    def locked(path):
        if os.path.normcase(str(path)) == os.path.normcase(str(old)):
            raise PermissionError("held")
        return real(path)

    monkeypatch.setattr(ps, "_read_bytes", locked)
    assert repair.inspect_storage(project_dir)["status"] == "healthy"  # nothing loads it
    component(project_dir, ASSET_A).unlink()
    assert repair.inspect_storage(project_dir)["reason"] == "unavailable"


def test_a_held_list_that_cannot_be_read_is_reported_by_the_check(tmp_path):
    project_dir = make(tmp_path)
    component(project_dir, ASSET_A).unlink()
    (Path(project_dir) / "state" / "held.json").write_bytes(b"{")
    assert repair.inspect_storage(project_dir)["reason"] == "held_unreadable"


@pytest.mark.parametrize("group", [None, [], "x", {"prompt_history": "x"}, {"prompt_history": {"path": "nonsense"}}])
def test_a_retained_set_in_any_shape_never_stops_a_repair(tmp_path, group):
    project_dir = make(tmp_path)
    _edit(project_dir, lambda data: data["storage"].update({"recovery_previous": group}))
    component(project_dir, ASSET_A).unlink()
    component(project_dir, "prompt_history").unlink()
    _report, result = fix(project_dir)
    assert result["reopened"]
    assert document(project_dir)["storage"]["recovery_previous"] == {}
    assert_usable(project_dir)


def test_a_component_without_its_marker_is_still_its_assets(tmp_path):
    project_dir = make(tmp_path)
    _edit(project_dir, lambda data: data["assets"][0].pop("has_frozen_provenance"))
    component(project_dir, ASSET_A).unlink()
    report, _ = fix(project_dir)
    assert [(item["kind"], item.get("asset_id")) for item in report["problems"]] == [("details", "a")]
    assert_usable(project_dir)


# ---------------------------------------------------------------------------
# Which history
# ---------------------------------------------------------------------------

def _lose_the_index(project_dir, **retained):
    _edit(project_dir, lambda data: data["storage"].update(
        {group: {"prompt_history": retained[group]} if group in retained else {}
         for group in ("recovery_previous", "authoring_previous")}))
    component(project_dir, "prompt_history").unlink()


def test_the_newer_retained_set_comes_first(tmp_path):
    project_dir = make(tmp_path)
    first = ps.publish_history(project_dir, [{"hash": "recovery"}], format_version=3)
    second = ps.publish_history(project_dir, [{"hash": "authoring"}], format_version=3)
    _lose_the_index(project_dir, recovery_previous=first, authoring_previous=second)
    fix(project_dir)
    assert ps.read_prompt_history(project_dir) == [{"hash": "recovery"}]


def test_a_retained_index_with_nothing_left_gives_way_to_one_that_has(tmp_path):
    project_dir = make(tmp_path)
    gone = ps.publish_history(project_dir, [{"hash": "gone"}], format_version=3)
    for child in ps.read_component(project_dir, "prompt_history", gone)["entries"]:
        (Path(project_dir) / child["path"]).unlink()
    left = ps.publish_history(project_dir, [{"hash": str(n)} for n in range(8)], format_version=3)
    os.utime(Path(project_dir) / left["path"], (4e9, 4e9))
    _lose_the_index(project_dir, recovery_previous=gone)
    report, _ = fix(project_dir)
    assert (report["problems"][0]["outcome"], report["problems"][0]["kept"]) == ("salvaged", 8)


def test_a_leftover_that_is_not_what_its_name_says_is_never_salvaged(tmp_path):
    project_dir = make(tmp_path)
    entry = ps.publish_component(project_dir, "history_entry", {"hash": "planted"}, format_version=3)
    planted = Path(project_dir) / "state" / ("d" * 64 + ".json")
    planted.write_text(json.dumps({"kind": "prompt_history_index", "schema_version": 1, "entries": [entry]}))
    os.utime(planted, (4e9, 4e9))
    junk = json.dumps({"kind": "prompt_history_index", "schema_version": 1, "entries": ["x", 3]}).encode()
    named = Path(project_dir) / "state" / f"{hashlib.sha256(junk).hexdigest()}.json"
    named.write_bytes(junk)
    os.utime(named, (3.9e9, 3.9e9))
    for path in (Path(project_dir) / "state").glob("*.json"):
        if path not in (planted, named, component(project_dir, "prompt_history")) and b"history_index" in path.read_bytes():
            path.unlink()
    _lose_the_index(project_dir)
    report, _ = fix(project_dir)
    assert report["problems"][0]["outcome"] == "emptied"
    assert_usable(project_dir)


def test_an_indexed_project_takes_back_a_retained_list_of_the_first_format(tmp_path):
    project_dir = make(tmp_path)
    old = ps.publish_component(project_dir, "prompt_history", [{"hash": "v1 a"}, {"hash": "v1 b"}], format_version=1)
    _lose_the_index(project_dir, recovery_previous=old)
    report, _ = fix(project_dir)
    assert (report["problems"][0]["outcome"], report["problems"][0]["kept"]) == ("previous", 2)
    assert ps.read_prompt_history(assert_usable(project_dir)) == [{"hash": "v1 a"}, {"hash": "v1 b"}]


def test_a_format_two_index_keeps_the_long_name(tmp_path):
    from test_component_migration import stored_project
    project, path, _history = stored_project(tmp_path)
    project_dir = project.project_dir
    del project
    entries(project_dir)[0].unlink()
    fix(project_dir)
    assert component(project_dir, "prompt_history").name.startswith("prompt_history-")
    pm.load_project(project_dir)


# ---------------------------------------------------------------------------
# Confirming, failing, recovering
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("change", ["recovered", "more_damage", "edited", "salvage", "retained"])
def test_a_plan_that_changed_since_the_check_is_refused(tmp_path, change):
    project_dir = make(tmp_path)
    target = component(project_dir, ASSET_A)
    payload = target.read_bytes()
    target.unlink()
    if change == "retained":
        project_file = Path(project_dir) / "project.json"
        data = document(project_dir)
        spare = ps.publish_component(project_dir, ASSET_A, {"spare": 1}, format_version=3)
        data["storage"]["recovery_previous"] = {ASSET_A: spare}
        project_file.write_text(json.dumps(data), encoding="utf-8")
    if change == "salvage":
        component(project_dir, "prompt_history").unlink()
        data = document(project_dir)
        for group in ("recovery_previous", "authoring_previous"):
            data["storage"][group] = {}
        (Path(project_dir) / "project.json").write_text(json.dumps(data), encoding="utf-8")
    report = repair.inspect_storage(project_dir)
    if change == "recovered":
        target.write_bytes(payload)
    elif change == "retained":
        (Path(project_dir) / spare["path"]).write_bytes(b"damaged after the check")
    elif change == "more_damage":
        entries(project_dir)[0].unlink()
    elif change == "edited":
        data = document(project_dir)
        data["name"] = "edited elsewhere"
        (Path(project_dir) / "project.json").write_text(json.dumps(data), encoding="utf-8")
    else:
        ps.publish_history(project_dir, [{"hash": "appeared"}], format_version=3)
    before = tree(project_dir)
    with pytest.raises(repair.RepairPlanStale):
        repair.repair_storage(project_dir, report["digest"])
    assert {p: d for p, d in tree(project_dir).items() if p.name != "project.lock"} == {
        p: d for p, d in before.items() if p.name != "project.lock"}


STEPS = ["backup_root", "publish_component", "write_held", "validate_storage", "restart_retention", "root"]


@pytest.mark.parametrize("step", STEPS)
def test_a_repair_that_fails_before_the_root_changes_nothing_the_project_reads(tmp_path, monkeypatch, step):
    project_dir = make(tmp_path)
    damage(component(project_dir, ASSET_A), "corrupt")
    entries(project_dir)[0].unlink()
    before = tree(project_dir)
    report = repair.inspect_storage(project_dir)

    def fail(*_args, **_kwargs):
        raise OSError("injected")

    with monkeypatch.context() as patch:
        if step == "root":
            patch.setattr(pm, "atomic_replace", fail)
        else:
            patch.setattr(ps, step, fail)
        with pytest.raises(OSError, match="injected"):
            repair.repair_storage(project_dir, report["digest"])
    after = tree(project_dir)
    assert {path: after.get(path) for path in before if path.name != "project.lock"} == {
        path: data for path, data in before.items() if path.name != "project.lock"}
    fix(project_dir)  # and it can be tried again
    assert_usable(project_dir)


def test_the_old_document_and_every_file_there_was_stay(tmp_path):
    project_dir = make(tmp_path)
    root = (Path(project_dir) / "project.json").read_bytes()
    damaged = component(project_dir, ASSET_A)
    damage(damaged, "corrupt")
    entries(project_dir)[0].unlink()
    stored = {path.name for path in (Path(project_dir) / "state").glob("*.json")}
    _report, result = fix(project_dir)
    assert (Path(project_dir) / result["backup"]).read_bytes() == root
    assert result["backup"] == f"state/{hashlib.sha256(root).hexdigest()}.bak"
    for index in range(4):
        project = pm.load_project(project_dir)
        project.name = f"edit {index}"
        ps.stage_prompt_history(project, [{"hash": f"later {index}"}])
        pm.save_project(project, notify=False)
        del project
        gc.collect()
        ps.collect_unreferenced_components(project_dir, retention_seconds=0)
    left = {path.name for path in (Path(project_dir) / "state").glob("*.json")}
    assert stored - {damaged.name, "held.json", "retention.json"} <= left
    assert damaged.with_suffix(".bad").exists()


def test_a_second_repair_keeps_what_the_first_held(tmp_path):
    project_dir = make(tmp_path)
    damage(component(project_dir, ASSET_A), "absent")
    fix(project_dir)
    first = ps.read_held(project_dir)
    damage(component(project_dir, ps.asset_component_name("b")), "absent")
    fix(project_dir)
    assert first.items() <= ps.read_held(project_dir).items()
    assert_usable(project_dir)


@pytest.mark.parametrize("content", [b"{", b"[]", b'{"v": 2, "held": {}}', b'{"v": 1, "held": {"../x": ""}}'])
def test_a_held_list_that_cannot_be_read_stops_collection(tmp_path, content):
    project_dir = make(tmp_path)
    ps.collect_unreferenced_components(project_dir, retention_seconds=0)
    orphan = ps.publish_component(project_dir, "history_entry", {"orphan": 1}, format_version=3)
    (Path(project_dir) / "state" / "held.json").write_bytes(content)
    assert ps.collect_unreferenced_components(project_dir, retention_seconds=0) == []
    assert (Path(project_dir) / orphan["path"]).exists()
    (Path(project_dir) / "state" / "held.json").unlink()
    assert ps.collect_unreferenced_components(project_dir, retention_seconds=0) == [Path(orphan["path"]).name]


def test_a_corrupt_file_that_cannot_be_renamed_is_reported_after_the_repair(tmp_path, monkeypatch):
    project_dir = make(tmp_path)
    target = component(project_dir, ASSET_A)
    damage(target, "corrupt")
    report = repair.inspect_storage(project_dir)
    with monkeypatch.context() as patch:
        patch.setattr(repair.os, "rename", lambda *_: (_ for _ in ()).throw(PermissionError("open")))
        result = repair.repair_storage(project_dir, report["digest"])
    assert result["left_in_place"] == [target.name] and target.read_bytes() == b"damaged"
    assert_usable(project_dir)
    # It is still reported, because it would fail a save that publishes its content.
    report, result = fix(project_dir)
    assert report["problems"] == [{"kind": "leftover", "state": "corrupt", "outcome": "set_aside", "count": 1}]
    assert result["left_in_place"] == [] and target.with_suffix(".bad").read_bytes() == b"damaged"
    project = pm.load_project(project_dir)
    project.assets[0].generation_params = {"prompt": "a", "workflow": {"n": 0}, "fps": 24.0}
    pm.save_project(project, notify=False)
    assert_usable(project_dir)


def test_a_file_set_aside_never_replaces_an_earlier_one(tmp_path):
    project_dir = make(tmp_path)
    target = component(project_dir, ASSET_A)
    target.with_suffix(".bad").write_bytes(b"from an earlier repair")
    damage(target, "corrupt")
    fix(project_dir)
    assert target.with_suffix(".bad").read_bytes() == b"from an earlier repair"
    assert target.with_suffix(".bad2").read_bytes() == b"damaged"


def test_a_file_the_repaired_document_names_is_never_set_aside(tmp_path):
    """The other spelling is restored: the damaged one goes, the good one stays."""
    project_dir = make(tmp_path)
    target = component(project_dir, ASSET_A)
    other = target.with_name(f"{ASSET_A}-{target.name}")
    other.write_bytes(target.read_bytes())
    damage(target, "corrupt")
    report, _ = fix(project_dir)
    assert [(item["kind"], item["outcome"]) for item in report["problems"]] == [("details", "restored")]
    assert other.exists() and target.with_suffix(".bad").exists()
    assert assert_usable(project_dir).assets[0].generation_params["prompt"] == "a"


def test_the_same_bytes_under_the_other_spelling_are_restored_everywhere(tmp_path):
    project_dir = make(tmp_path)
    data = document(project_dir)
    data["storage"]["components"][ASSET_A]["future"] = "kept"
    (Path(project_dir) / "project.json").write_text(json.dumps(data), encoding="utf-8")
    for name, path in ((ASSET_A, component(project_dir, ASSET_A)), (JOB_1, component(project_dir, JOB_1)),
                       ("history_entry", entries(project_dir)[0])):
        path.rename(path.with_name(f"{name}-{path.name}"))
    report, _ = fix(project_dir)
    assert [(item["kind"], item["outcome"]) for item in report["problems"]] == [
        ("history", "restored"), ("details", "restored"), ("job", "restored"), ("job", "dropped")]
    assert document(project_dir)["storage"]["components"][ASSET_A]["future"] == "kept"
    project = assert_usable(project_dir)
    assert project.assets[0].generation_params["prompt"] == "a"
    assert [job.job_id for job in project.generation_queue] == ["j1"]  # j2 is named differently
    assert [entry["hash"] for entry in ps.read_prompt_history(project)] == ["one", "two"]


@pytest.mark.parametrize("where", ["current", "retained", "both_spellings"])
def test_an_empty_history_is_written_beside_a_damaged_file_of_the_same_name(tmp_path, where):
    project = pm.create_project("New", base_dir=str(tmp_path))  # its history is the empty index
    project_dir = project.project_dir
    del project
    empty = component(project_dir, "prompt_history")
    if where == "retained":
        project = pm.load_project(project_dir)
        ps.stage_prompt_history(project, [{"hash": "one"}])
        pm.save_project(project, notify=False)
        del project
        children = entries(project_dir)
        component(project_dir, "prompt_history").unlink()
        children[0].unlink()
    empty.write_bytes(b"")
    if where == "both_spellings":
        empty.with_name("prompt_history-" + empty.name).write_bytes(b"x")
    fix(project_dir)
    assert ps.read_prompt_history(assert_usable(project_dir)) == []


def test_a_mutable_copy_of_unknown_document_fields_survives(tmp_path):
    project_dir = make(tmp_path)
    data = document(project_dir)
    data["future"] = {"kept": 1}
    data["storage"]["future"] = [1]
    (Path(project_dir) / "project.json").write_text(json.dumps(data), encoding="utf-8")
    component(project_dir, ASSET_A).unlink()
    fix(project_dir)
    assert document(project_dir)["future"] == {"kept": 1} and document(project_dir)["storage"]["future"] == [1]


def test_hooks_fire_once_after_the_lock_and_open_editors_conflict(tmp_path, monkeypatch):
    project_dir = make(tmp_path)
    open_editor = pm.load_project(project_dir)
    base = open_editor.modified_at
    damage(component(project_dir, ASSET_A), "absent")
    fired = []

    def hook(project):
        pm.save_project(pm.load_project(project_dir), notify=False)  # deadlocks if the lock is held
        fired.append(project.modified_at)

    monkeypatch.setattr(pm, "_PROJECT_SAVED_HOOKS", [hook])
    fix(project_dir)
    assert len(fired) == 1 and fired[0] > base
    with pytest.raises(pm.ProjectVersionConflict):
        pm.save_project(open_editor, expected_modified_at=base, notify=False)


@pytest.mark.parametrize("failure", ["reload", "hook", "rename"])
def test_nothing_after_the_root_write_is_reported_as_a_failed_repair(tmp_path, monkeypatch, failure):
    project_dir = make(tmp_path)
    damage(component(project_dir, ASSET_A), "corrupt")
    report = repair.inspect_storage(project_dir)

    def fail(*_args, **_kwargs):
        raise FileNotFoundError("after the root")

    with monkeypatch.context() as patch:
        if failure == "reload":
            patch.setattr(pm, "load_project", fail)
        elif failure == "hook":
            patch.setattr(pm, "_PROJECT_SAVED_HOOKS", [fail])
        else:
            patch.setattr(repair.os, "rename", fail)
        result = repair.repair_storage(project_dir, report["digest"])
    assert result["reopened"] is (failure != "reload")
    assert bool(result["left_in_place"]) is (failure == "rename")
    assert_usable(project_dir)


def test_a_repaired_document_that_would_not_load_is_not_written(tmp_path, monkeypatch):
    from server.timeline_state import TimelineProject
    project_dir = make(tmp_path)
    component(project_dir, ASSET_A).unlink()
    report = repair.inspect_storage(project_dir)
    root = (Path(project_dir) / "project.json").read_bytes()
    monkeypatch.setattr(TimelineProject, "from_dict", lambda *_a, **_k: (_ for _ in ()).throw(KeyError("scene")))
    with pytest.raises(repair.RepairRefused, match="invalid_storage"):
        repair.repair_storage(project_dir, report["digest"])
    assert (Path(project_dir) / "project.json").read_bytes() == root


def test_the_hooks_get_the_project_that_is_on_disk(tmp_path, monkeypatch):
    project_dir = make(tmp_path)
    component(project_dir, ASSET_A).unlink()
    seen = []
    monkeypatch.setattr(pm, "_PROJECT_SAVED_HOOKS", [seen.append])
    fix(project_dir)
    assert len(seen) == 1 and seen[0].project_dir == project_dir
    assert seen[0].modified_at == document(project_dir)["modified_at"]


def test_the_version_advances_past_a_token_from_the_future(tmp_path):
    project_dir = make(tmp_path)
    _edit(project_dir, lambda data: data.update({"modified_at": "2999-01-01T00:00:00"}))
    component(project_dir, ASSET_A).unlink()
    fix(project_dir)
    assert document(project_dir)["modified_at"] > "2999-01-01T00:00:00"


def test_a_repair_restarts_the_retention_of_what_it_names(tmp_path):
    project_dir = make(tmp_path)
    salvage = ps.publish_history(project_dir, [{"hash": "unused for long"}], format_version=3)
    names = {Path(salvage["path"]).name} | {
        Path(child["path"]).name for child in ps.read_component(project_dir, "prompt_history", salvage)["entries"]}
    os.utime(Path(project_dir) / salvage["path"], (4e9, 4e9))
    ps._write_retention(project_dir, {name: 1.0 for name in names})
    _lose_the_index(project_dir)
    fix(project_dir)
    assert not names & ps._read_retention(project_dir).keys()


def test_a_lone_surrogate_no_save_could_write_is_not_repaired(tmp_path):
    project_dir = make(tmp_path)
    data = document(project_dir)
    data["name"] = "\ud800"
    (Path(project_dir) / "project.json").write_text(json.dumps(data), encoding="utf-8")  # escaped, so it loads
    component(project_dir, ASSET_A).unlink()
    assert repair.inspect_storage(project_dir)["reason"] == "invalid_storage"


def test_a_check_holds_the_project_lock(tmp_path, monkeypatch):
    project_dir = make(tmp_path)
    held = []
    real = repair._Plan._inspect
    monkeypatch.setattr(repair._Plan, "_inspect", lambda self: (
        held.append(pm._project_write_lock(project_dir).locked()), real(self))[1])
    repair.inspect_storage(project_dir)
    assert held == [True]


def test_a_busy_project_is_not_repaired(tmp_path, monkeypatch):
    project_dir = make(tmp_path)
    damage(component(project_dir, ASSET_A), "absent")
    report = repair.inspect_storage(project_dir)
    before = tree(project_dir)
    monkeypatch.setattr(pm, "COMMIT_LOCK_WAIT_SECONDS", 0)
    monkeypatch.setattr(pm.ProjectFileLock, "try_acquire", lambda self: "contended")
    monkeypatch.setattr(pm._project_commit_lock.__wrapped__, "__kwdefaults__", {"wait": 0})
    with pytest.raises(ProjectBusyError):
        repair.repair_storage(project_dir, report["digest"])
    assert tree(project_dir) == before


def test_a_filesystem_without_locks_is_checked_but_not_repaired(tmp_path, monkeypatch):
    project_dir = make(tmp_path)
    damage(component(project_dir, ASSET_A), "absent")
    report = repair.inspect_storage(project_dir)
    monkeypatch.setattr(pm.ProjectFileLock, "try_acquire", lambda self: "unsupported")
    with pytest.raises(repair.RepairRefused, match="no_locks"):
        repair.repair_storage(project_dir, report["digest"])


def test_the_path_budget_of_a_deep_project(tmp_path, monkeypatch):
    if len(str(tmp_path)) > 181:
        pytest.skip("the test folder itself is too deep")
    name = "p" * (183 - len(str(tmp_path)) - 1)
    project_dir = make(tmp_path, name)
    assert len(project_dir) == 183
    damage(component(project_dir, ASSET_A), "corrupt")
    for module, name in ((pm, "atomic_replace"), (ps, "atomic_replace"), (repair.os, "rename")):
        real = getattr(module, name)

        def bounded(source, target, real=real):
            assert len(str(source)) <= 259 and len(str(target)) <= 259
            real(source, target)

        monkeypatch.setattr(module, name, bounded)
    fix(project_dir)
    assert_usable(project_dir)


@pytest.mark.parametrize("version", [1, 2])
def test_older_formats_keep_their_format(tmp_path, version):
    from test_component_migration import stored_project
    project, path, _history = stored_project(tmp_path)
    project_dir = project.project_dir
    del project
    gc.collect()
    data = document(project_dir)
    if version == 1:
        data["storage"]["format_version"] = 1
        data["storage"]["components"]["prompt_history"] = ps.publish_component(
            project_dir, "prompt_history", [{"hash": "v1"}], format_version=1)
        for group in ("recovery_previous", "authoring_previous"):
            data["storage"][group] = {}
        path.write_text(json.dumps(data), encoding="utf-8")
    assert document(project_dir)["storage"]["format_version"] == version
    component(project_dir, "prompt_history").unlink()
    fix(project_dir)
    assert document(project_dir)["storage"]["format_version"] == version
    pm.load_project(project_dir)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
def routes(tmp_path, monkeypatch):
    module = _load_route_module(monkeypatch)
    monkeypatch.setattr(module, "_get_base_dir", lambda: str(tmp_path))
    return module


def call(module, method, folder, body=None):
    path = "/sonder-editor/project/{project_id}/storage/" + ("check" if method == "GET" else "repair")
    request = DummyRequest(match_info={"project_id": folder}, method=method, path=path, body=body)

    async def answer():
        return await module._project_error_middleware(request, _route_handler(module, method, path))

    response = asyncio.run(answer())
    return response.status, json.loads(response.body)


def test_the_routes_check_repair_and_refuse_a_stale_plan(tmp_path, routes):
    project_dir = make(tmp_path)
    damage(component(project_dir, ASSET_A), "absent")
    folder = os.path.basename(project_dir)
    status, report = call(routes, "GET", folder)
    assert status == 200 and report["ok"] and report["status"] == "damaged"
    assert call(routes, "POST", folder, {"digest": "0" * 64}) == (409, {
        "ok": False, "code": "repair_plan_stale", "error": "The project changed since it was checked. Check it again."})
    status, result = call(routes, "POST", folder, {"digest": report["digest"]})
    assert status == 200 and result["backup"].startswith("state/")
    assert call(routes, "GET", folder)[1]["status"] == "healthy"
    assert call(routes, "POST", folder, {"digest": call(routes, "GET", folder)[1]["digest"]})[1]["code"] == "repair_refused"


def test_the_routes_work_by_folder_on_a_document_that_does_not_parse(tmp_path, routes):
    project_dir = make(tmp_path)
    (Path(project_dir) / "project.json").write_bytes(b"{")
    assert call(routes, "GET", os.path.basename(project_dir))[1]["reason"] == "invalid_json"


def test_the_routes_run_on_the_project_pool(tmp_path, routes, monkeypatch):
    from server.project_storage_lifecycle import on_project_executor_thread
    project_dir = make(tmp_path)
    seen = []
    for name in ("inspect_storage", "repair_storage"):
        monkeypatch.setattr(routes.project_repair, name,
                            lambda *_args: seen.append(on_project_executor_thread()) or {"status": "healthy"})
    call(routes, "GET", os.path.basename(project_dir))
    call(routes, "POST", os.path.basename(project_dir), {"digest": "x"})
    assert seen == [True, True]


def test_the_routes_refuse_a_path_like_every_project_route(tmp_path, routes):
    project_dir = make(tmp_path)
    path = "/sonder-editor/project/{project_id}/storage/check"
    request = DummyRequest(match_info={"project_id": os.path.basename(project_dir)}, query={"path": "x"},
                           method="GET", path=path)
    assert asyncio.run(_route_handler(routes, "GET", path)(request)).status == 400


@pytest.mark.parametrize("folder", ["missing", "..", "a/b"])
def test_the_routes_answer_404_outside_the_projects_folder(routes, folder):
    assert call(routes, "GET", folder)[0] == 404
    assert call(routes, "POST", folder, {"digest": "x"})[0] == 404


@pytest.mark.parametrize("body", [None, [], {}, {"digest": 1}, {"digest": ""}])
def test_a_repair_needs_the_digest_of_a_check(tmp_path, routes, body):
    project_dir = make(tmp_path)
    assert call(routes, "POST", os.path.basename(project_dir), body)[0] == 400


@pytest.mark.parametrize("error, status, code", [
    (OSError("C:\\secret\\disk"), 500, "repair_failed"),
    (FileNotFoundError("C:\\secret\\state"), 500, "repair_failed"),
    (ProjectBusyError(), 503, "project_busy"),
    (ps.ProjectStorageError("C:\\secret\\state"), 500, "project_storage_unreadable"),
])
def test_a_failed_repair_never_names_a_path(tmp_path, routes, monkeypatch, error, status, code):
    project_dir = make(tmp_path)
    monkeypatch.setattr(routes.project_repair, "repair_storage", lambda *_: (_ for _ in ()).throw(error))
    answer = call(routes, "POST", os.path.basename(project_dir), {"digest": "x"})
    assert (answer[0], answer[1]["code"]) == (status, code)
    assert "secret" not in json.dumps(answer[1])


def test_no_answer_names_a_path(tmp_path, routes):
    project_dir = make(tmp_path)
    damage(component(project_dir, ASSET_A), "corrupt")
    entries(project_dir)[0].unlink()
    folder = os.path.basename(project_dir)
    report = call(routes, "GET", folder)[1]
    result = call(routes, "POST", folder, {"digest": report["digest"]})[1]
    for answer in (copy.deepcopy(report), result):
        assert str(tmp_path) not in json.dumps(answer) and "\\\\" not in json.dumps(answer)
