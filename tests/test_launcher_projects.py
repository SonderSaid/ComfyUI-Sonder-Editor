"""The Launcher's project scan, format status and routes.

* ``list_projects`` keeps its output; ``scan_projects`` adds what could not be read
  (a reason code, never exception text or a path) and how many linked projects
  were skipped while external links are off. A bad project no longer fails the list.
* ``next_edit_upgrade`` predicts what the next edit does to a project's storage
  format from the same rule the save path uses, and the prediction matches real saves.
* Every save records the Sonder version that wrote it (``written_with``).
* ``/sonder-editor/launcher/projects`` returns folders (no paths), hidden flags, the
  editor's scene and the last workflow; ``/launcher/hidden`` remembers hidden items.
"""

import asyncio
import json
import os
import re
import sys
import types
from pathlib import Path

import pytest

from server import external_links, launcher_state, project_manager as pm, project_storage as ps, sdk_templates
from server.pack_version import pack_version
from server.timeline_state import GenerationJob

LIST_KEYS = {"project_id", "name", "path", "linked", "fps", "resolution", "scene_count", "clip_count",
             "asset_count", "modified_at"}


@pytest.fixture(autouse=True)
def user_dir(tmp_path, monkeypatch):
    user = tmp_path / "user"
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(get_user_directory=lambda: str(user)))
    monkeypatch.setattr(external_links, "is_enabled", lambda: False)
    return user


@pytest.fixture
def base(tmp_path):
    root = tmp_path / "sonder-projects"
    root.mkdir()
    return root


def make(base, name):
    project = pm.create_project(name, base_dir=str(base))
    return project, Path(project.project_dir) / "project.json"


def rewrite(path, mutate):
    raw = json.loads(path.read_text(encoding="utf-8"))
    mutate(raw)
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")


# ── Scan ─────────────────────────────────────────────────────────────────────

def test_list_projects_keeps_its_fields_and_skips_what_it_cannot_read(base):
    make(base, "Good")
    (base / "Broken JSON").mkdir()
    (base / "Broken JSON" / "project.json").write_text("{nope", encoding="utf-8")
    (base / "Not UTF8").mkdir()
    (base / "Not UTF8" / "project.json").write_bytes(b'{"name": "\xff\xfe"}')
    (base / "A List").mkdir()
    (base / "A List" / "project.json").write_text("[1, 2]", encoding="utf-8")
    (base / "Bad Scenes").mkdir()
    (base / "Bad Scenes" / "project.json").write_text('{"scenes": [3]}', encoding="utf-8")
    listed = pm.list_projects(str(base))
    assert [item["name"] for item in listed] == ["Good"]
    assert set(listed[0]) == LIST_KEYS


def test_scan_reports_unreadable_projects_by_code_only(base):
    make(base, "Good")
    (base / "Broken JSON").mkdir()
    (base / "Broken JSON" / "project.json").write_text("{nope", encoding="utf-8")
    (base / "Not UTF8").mkdir()
    (base / "Not UTF8" / "project.json").write_bytes(b'{"name": "\xff\xfe"}')
    (base / "A List").mkdir()
    (base / "A List" / "project.json").write_text("[1, 2]", encoding="utf-8")
    entries, unreadable, linked_hidden = pm.scan_projects(str(base), detail=True)
    assert [entry["folder"] for entry in entries] == ["Good"]
    assert unreadable == [
        {"folder": "A List", "reason": "not_a_project", "linked": False},
        {"folder": "Broken JSON", "reason": "invalid_json", "linked": False},
        {"folder": "Not UTF8", "reason": "invalid_json", "linked": False},
    ]
    assert linked_hidden == 0


def test_scan_counts_linked_projects_skipped_while_links_are_off(base, monkeypatch):
    make(base, "Linked One")
    make(base, "Plain")
    monkeypatch.setattr(external_links, "is_reparse_child", lambda parent, name: name == "Linked-One")
    entries, unreadable, linked_hidden = pm.scan_projects(str(base))
    assert [entry["name"] for entry in entries] == ["Plain"] and unreadable == [] and linked_hidden == 1


def test_detail_reports_format_version_and_too_new_projects(base):
    _, legacy = make(base, "Legacy")
    _, future = make(base, "Future")
    rewrite(future, lambda raw: raw.update(storage={"format_version": 4, "components": {}}, written_with="9.9.9"))
    entries = {entry["folder"]: entry for entry in pm.scan_projects(str(base), detail=True)[0]}
    assert entries["Legacy"]["format_version"] == 0 and entries["Legacy"]["too_new"] is False
    assert entries["Future"]["format_version"] == 4 and entries["Future"]["too_new"] is True
    assert entries["Future"]["upgrade"] is None and entries["Future"]["written_with"] == "9.9.9"
    # Still listed for the editor's own project list.
    assert {item["name"] for item in pm.list_projects(str(base))} == {"Legacy", "Future"}


def test_detail_reports_a_storage_block_it_cannot_load_as_unreadable(base):
    _, path = make(base, "Half Split")
    rewrite(path, lambda raw: raw.update(storage={"format_version": 3, "components": {}}))  # no prompt_history
    entries, unreadable, _ = pm.scan_projects(str(base), detail=True)
    assert entries == [] and unreadable == [{"folder": "Half-Split", "reason": "storage", "linked": False}]


# ── Format upgrades ──────────────────────────────────────────────────────────

def test_a_legacy_project_without_payloads_upgrades_only_when_prompt_history_is_recorded(base):
    project, path = make(base, "Plain Legacy")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "storage" not in raw
    assert ps.next_edit_upgrade(raw) == {"from": 0, "to": 3, "when": "generation"}
    # An ordinary edit keeps it legacy...
    project.name = "Renamed"
    pm.save_project(project, notify=False)
    assert "storage" not in json.loads(path.read_text(encoding="utf-8"))
    # ...and recording prompt history migrates it, as predicted.
    project = pm.load_project(project.project_dir)
    ps.stage_prompt_history(project, [{"hash": "h"}])
    pm.save_project(project, notify=False)
    assert json.loads(path.read_text(encoding="utf-8"))["storage"]["format_version"] == 3


def test_a_legacy_project_with_a_queue_upgrades_on_any_edit(base):
    project, path = make(base, "Queued Legacy")
    job = GenerationJob(job_id="job-1", prompt_sections=[{"prompt": "queued"}]).to_dict()
    rewrite(path, lambda raw: raw.update(generation_queue=[job]))
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "storage" not in raw
    assert ps.next_edit_upgrade(raw) == {"from": 0, "to": 3, "when": "edit"}
    project = pm.load_project(project.project_dir)
    project.name = "Edited"
    pm.save_project(project, notify=False)
    assert json.loads(path.read_text(encoding="utf-8"))["storage"]["format_version"] == 3


def test_a_format_2_project_upgrades_on_any_edit_and_format_3_never_does(tmp_path):
    import test_component_migration as fixtures

    project, path, _ = fixtures.stored_project(tmp_path, version=2)
    assert ps.next_edit_upgrade(json.loads(path.read_bytes())) == {"from": 2, "to": 3, "when": "edit"}
    pm.save_project(project, notify=False)
    raw = json.loads(path.read_bytes())
    assert raw["storage"]["format_version"] == 3
    assert ps.next_edit_upgrade(raw) is None


def test_format_1_upgrades_to_2_only_with_prompt_history():
    data = {"storage": {"format_version": 1, "components": {"prompt_history": {}}}}
    assert ps.next_edit_upgrade(data) == {"from": 1, "to": 2, "when": "prompt_history"}


def test_saved_format_version_is_what_assemble_for_save_uses(monkeypatch):
    """The predicate and the save path share one rule: patching it changes migration."""
    calls = []
    original = ps.saved_format_version

    def spy(*args):
        calls.append(args[2:])
        return original(*args)

    monkeypatch.setattr(ps, "saved_format_version", spy)
    project = types.SimpleNamespace(_staged_components={}, project_dir="", _raw_data={})
    assert ps.assemble_for_save(project, {"name": "x"}, {}, bump_modified_at=True) == {"name": "x"}
    assert calls == [((), True)]


@pytest.mark.parametrize("payload", ["history", "provenance"])
def test_a_legacy_project_with_inline_history_or_provenance_upgrades_on_any_edit(base, payload):
    project, path = make(base, f"Legacy {payload}")

    def add(raw):
        if payload == "history":
            raw.setdefault("metadata", {})["prompt_history"] = [{"hash": "h"}]
        else:
            raw["assets"] = [{"asset_id": "a1", "name": "take.mp4", "asset_type": "video",
                              "source_path": "media/take.mp4", "generation_params": {"prompt": "p", "fps": 24}}]

    rewrite(path, add)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert ps.next_edit_upgrade(raw) == {"from": 0, "to": 3, "when": "edit"}
    project = pm.load_project(project.project_dir)
    project.name = "Edited"
    pm.save_project(project, notify=False)
    assert json.loads(path.read_text(encoding="utf-8"))["storage"]["format_version"] == 3


def test_a_format_2_save_goes_through_the_shared_rule(tmp_path, monkeypatch):
    import test_component_migration as fixtures

    project, path, _ = fixtures.stored_project(tmp_path, version=2)
    seen = []
    original = ps.saved_format_version

    def spy(old, data, staged, bump):
        result = original(old, data, staged, bump)
        seen.append((old["format_version"] if old else 0, result))
        return result

    monkeypatch.setattr(ps, "saved_format_version", spy)
    pm.save_project(project, notify=False)
    assert seen == [(2, 3)]
    assert json.loads(path.read_bytes())["storage"]["format_version"] == 3


def test_a_save_that_drifts_from_the_shared_rule_fails_closed(tmp_path, monkeypatch):
    import test_component_migration as fixtures

    project, path, _ = fixtures.stored_project(tmp_path, version=2)
    before = path.read_bytes()
    monkeypatch.setattr(ps, "saved_format_version", lambda *args: 4)  # a rule no save step reaches
    with pytest.raises(ps.ProjectStorageError):
        pm.save_project(project, notify=False)
    assert path.read_bytes() == before


def test_stored_format_version_reads_raw_documents():
    assert ps.stored_format_version({}) == 0
    assert ps.stored_format_version({"storage": {"format_version": 3}}) == 3
    assert ps.stored_format_version({"storage": {"format_version": "3"}}) == -1
    assert ps.stored_format_version({"storage": []}) == -1


def test_every_readable_format_has_a_first_release():
    assert set(ps.FORMAT_FIRST_RELEASE) == set(ps.READABLE_FORMAT_VERSIONS)
    assert ps.CURRENT_FORMAT_VERSION == max(ps.READABLE_FORMAT_VERSIONS)


# ── written_with ─────────────────────────────────────────────────────────────

def test_pack_version_matches_pyproject():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert pack_version() == re.search(r'^version = "([^"]+)"', text, re.MULTILINE).group(1)


def test_every_save_records_the_writing_version_and_replaces_an_older_stamp(base):
    project, path = make(base, "Stamped")
    assert json.loads(path.read_text(encoding="utf-8"))["written_with"] == pack_version()
    rewrite(path, lambda raw: raw.update(written_with="0.0.1"))
    project = pm.load_project(project.project_dir)
    pm.save_project(project, notify=False, bump_modified_at=False)
    assert json.loads(path.read_text(encoding="utf-8"))["written_with"] == pack_version()


# ── Routes ───────────────────────────────────────────────────────────────────

def folder_resolver(base):
    """Stands in for ``routes._project_dir_for_folder``: a safe name with a project.json."""
    from server.path_security import PathSecurityError, safe_route_token

    def resolve(folder):
        try:
            safe_route_token(folder, "project id")
        except PathSecurityError:
            return None
        path = base / folder
        return str(path) if (path / "project.json").is_file() else None

    return resolve


def _client(base):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from server.launcher_routes import register_launcher_routes

    from server import routes as route_module

    routes = web.RouteTableDef()
    register_launcher_routes(routes, projects_dir=lambda: str(base), project_dir_for_folder=folder_resolver(base))
    # The editor's real Origin check and security headers, as ComfyUI mounts them.
    app = web.Application(middlewares=[route_module._sonder_security_middleware])
    app.add_routes(routes)
    return TestClient(TestServer(app))


def test_the_projects_route_gives_folders_scenes_hidden_flags_and_last_workflows(base, user_dir):
    project, _ = make(base, "Songs")
    from server.timeline_state import Scene

    project.scenes = [Scene(scene_id="aaaa1111", name="Verse", order=1),
                      Scene(scene_id="bbbb2222", name="Chorus", order=2)]
    pm.save_project(project, notify=False)
    make(base, "Other")
    (base / "Broken").mkdir()
    (base / "Broken" / "project.json").write_text("{", encoding="utf-8")
    sdk_templates.write_editor_state("Songs", {"scene_id": "bbbb2222"})
    launcher_state.record_open("Songs", "a" * 16)
    launcher_state.set_hidden("project", "Other", True)

    async def run():
        async with _client(base) as client:
            payload = await (await client.get("/sonder-editor/launcher/projects")).json()
            hidden = await (await client.put("/sonder-editor/launcher/hidden",
                                             json={"kind": "project", "id": "Other", "hidden": False})).json()
            refused = await client.put("/sonder-editor/launcher/hidden", json={"kind": "project", "id": "../x",
                                                                                "hidden": True})
            state = await (await client.get("/sonder-editor/launcher/state")).json()
        return payload, hidden, refused.status, state

    payload, hidden, refused, state = asyncio.run(run())
    projects = {item["folder"]: item for item in payload["projects"]}
    assert set(projects) == {"Songs", "Other"}
    songs = projects["Songs"]
    assert "path" not in songs and songs["scene"] == {"id": "bbbb2222", "name": "Chorus"}
    assert songs["last_workflow"] == "a" * 16 and songs["hidden"] is False
    assert songs["written_with"] == pack_version() and songs["format_version"] == 0
    # The server's create makes no scene (the Launcher adds Scene 1 itself).
    assert projects["Other"]["hidden"] is True and projects["Other"]["scene"] == {}
    assert payload["unreadable"] == [{"folder": "Broken", "reason": "invalid_json", "linked": False, "hidden": False}]
    assert payload["current_format"] == 3 and payload["format_first_release"]["3"] == "0.6.0"
    assert payload["linked_hidden_count"] == 0 and payload["can_manage"] is True
    assert hidden["hidden"]["projects"] == [] and refused == 400
    assert state["last_workflow"]["any"]["template_id"] == "a" * 16


def test_a_project_whose_editor_scene_is_gone_falls_back_to_its_first_scene(base):
    project, _ = make(base, "Moved")
    from server.timeline_state import Scene

    project.scenes = [Scene(scene_id="cccc3333", name="Late", order=5), Scene(scene_id="dddd4444", name="Early", order=1)]
    pm.save_project(project, notify=False)
    sdk_templates.write_editor_state("Moved", {"scene_id": "deleted0"})

    async def run():
        async with _client(base) as client:
            return await (await client.get("/sonder-editor/launcher/projects")).json()

    payload = asyncio.run(run())
    assert payload["projects"][0]["scene"] == {"id": "dddd4444", "name": "Early"}


def test_launcher_writes_refuse_a_foreign_origin_and_oversized_bodies(base):
    async def run():
        async with _client(base) as client:
            foreign = await client.put("/sonder-editor/launcher/hidden", headers={"Origin": "https://evil.example"},
                                       json={"kind": "project", "id": "x", "hidden": True})
            big = await client.put("/sonder-editor/launcher/hidden", data=b"{" + b" " * 20000 + b"}",
                                   headers={"Content-Type": "application/json"})
            bad = await client.put("/sonder-editor/launcher/hidden", json={"kind": "project", "id": "x", "hidden": "yes"})
        return foreign.status, big.status, bad.status

    assert asyncio.run(run()) == (403, 413, 400)
    assert launcher_state.read_state()["hidden"]["projects"] == []


def test_can_manage_is_false_for_a_relayed_request(base):
    async def run():
        async with _client(base) as client:
            return await (await client.get("/sonder-editor/launcher/projects",
                                           headers={"X-Forwarded-For": "203.0.113.9"})).json()

    assert asyncio.run(run())["can_manage"] is False


def test_the_preselected_workflow_follows_last_use_and_skips_hidden_ones(base, user_dir, monkeypatch):
    from server import launcher_routes

    make(base, "Songs")
    make(base, "Fresh")
    t_old, t_new, t_hidden = "1" * 16, "2" * 16, "3" * 16
    templates = [{"template_id": t_old, "name": "Old", "imported_at": 10},
                 {"template_id": t_new, "name": "New", "imported_at": 20},
                 {"template_id": t_hidden, "name": "Hidden", "imported_at": 30}]
    monkeypatch.setattr(launcher_routes.sdk_templates, "list_templates", lambda: templates)
    launcher_state.record_open("Songs", t_old, clock=lambda: 100.0)
    launcher_state.set_hidden("workflow", t_hidden, True)
    payload = launcher_routes.launcher_projects(str(base))
    # Last used first, then never-used ones by newest import (hidden ones keep their place).
    assert payload["workflow_order"] == [t_old, t_hidden, t_new]
    by_folder = {item["folder"]: item for item in payload["projects"]}
    assert by_folder["Songs"]["workflow"] == t_old
    assert by_folder["Fresh"]["workflow"] == t_old  # the last used anywhere
    launcher_state.set_hidden("workflow", t_old, True)
    payload = launcher_routes.launcher_projects(str(base))
    assert {item["workflow"] for item in payload["projects"]} == {t_new}


def test_request_locality_refuses_relayed_and_remote_peers():
    from types import SimpleNamespace

    from server.request_locality import is_loopback_request

    def request(remote, host, **headers):
        return SimpleNamespace(remote=remote, headers={"Host": host, **headers})

    assert is_loopback_request(request("127.0.0.1", "127.0.0.1:8888"))
    assert not is_loopback_request(request("192.168.1.20", "127.0.0.1:8888"))
    assert not is_loopback_request(request("127.0.0.1", "evil.example:8888"))
    assert not is_loopback_request(request("127.0.0.1", "127.0.0.1:8888", Via="1.1 proxy"))
