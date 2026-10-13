"""What the Launcher remembers (server/launcher_state.py).

* Install-level file under ``<comfy user dir>/sonder-editor/launcher.json``.
* Projects are keyed by folder name, workflows by template id; anything else refuses.
* Maps keep their newest MAX_ENTRIES; unknown keys survive; a damaged file is moved
  aside and the Launcher starts fresh.
* The preselected workflow: the project's last, else the last used anywhere, else
  the first available; hidden or deleted workflows are skipped.
"""

import json
import sys
import types

import pytest

from server import launcher_state

T1, T2, T3 = "a" * 16, "b" * 16, "c" * 16


@pytest.fixture(autouse=True)
def user_dir(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(get_user_directory=lambda: str(tmp_path)))
    return tmp_path


def stored(user_dir):
    return json.loads((user_dir / "sonder-editor" / "launcher.json").read_text(encoding="utf-8"))


def test_an_empty_install_reads_as_empty_state(user_dir):
    state = launcher_state.read_state()
    assert state["hidden"] == {"projects": [], "workflows": []}
    assert state["last_workflow"] == {"by_project": {}, "by_template": {}, "any": {}}
    assert not (user_dir / "sonder-editor" / "launcher.json").exists()


def test_hiding_and_unhiding_projects_and_workflows(user_dir):
    launcher_state.set_hidden("project", "ZZ-Old Test", True)
    launcher_state.set_hidden("project", "ZZ-Old Test", True)  # idempotent
    launcher_state.set_hidden("workflow", T1, True)
    data = stored(user_dir)
    assert data["hidden"] == {"projects": ["ZZ-Old Test"], "workflows": [T1]}
    launcher_state.set_hidden("project", "ZZ-Old Test", False)
    assert launcher_state.read_state()["hidden"]["projects"] == []


@pytest.mark.parametrize("kind, item", [
    ("project", "../escape"), ("project", "a/b"), ("project", "C:evil"), ("project", ""),
    ("project", "x" * 256), ("workflow", "not-an-id"), ("workflow", "A" * 16), ("nope", "x"),
])
def test_unknown_items_and_kinds_refuse(kind, item):
    with pytest.raises(launcher_state.LauncherStateError):
        launcher_state.set_hidden(kind, item, True)


def test_record_open_remembers_per_project_per_template_and_overall():
    launcher_state.record_open("Songs", T1, clock=lambda: 100.0)
    launcher_state.record_open("Night Market", T2, clock=lambda: 200.0)
    launcher_state.record_open("Songs", T3, clock=lambda: 300.0)
    last = launcher_state.read_state()["last_workflow"]
    assert last["by_project"] == {"Night Market": {"template_id": T2, "at": 200.0},
                                  "Songs": {"template_id": T3, "at": 300.0}}
    assert last["by_template"] == {T1: 100.0, T2: 200.0, T3: 300.0}
    assert last["any"] == {"template_id": T3, "at": 300.0}


def test_maps_keep_only_the_newest_entries(monkeypatch):
    monkeypatch.setattr(launcher_state, "MAX_ENTRIES", 3)
    for index in range(5):
        launcher_state.record_open(f"P{index}", f"{index:016x}", clock=lambda i=index: 10.0 + i)
        launcher_state.set_hidden("project", f"P{index}", True)
    state = launcher_state.read_state()
    assert list(state["last_workflow"]["by_project"]) == ["P2", "P3", "P4"]
    assert list(state["last_workflow"]["by_template"]) == [f"{i:016x}" for i in (2, 3, 4)]
    assert state["hidden"]["projects"] == ["P2", "P3", "P4"]


def test_unknown_keys_survive_and_bad_entries_are_dropped(user_dir):
    path = user_dir / "sonder-editor" / "launcher.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "version": 1, "future": {"keep": True},
        "hidden": {"projects": ["ok", "../bad", 7], "workflows": ["nope", T1], "later": [1]},
        "last_workflow": {"by_project": {"ok": {"template_id": T1, "at": 5}, "a/b": {"template_id": T1, "at": 1},
                                         "ok2": {"template_id": "bad", "at": 1}},
                          "by_template": {T1: "x", "bad": 3}, "any": {"template_id": "bad"}},
    }), encoding="utf-8")
    launcher_state.set_hidden("project", "new", True)
    data = stored(user_dir)
    assert data["future"] == {"keep": True} and data["hidden"]["later"] == [1]
    assert data["hidden"]["projects"] == ["ok", "new"] and data["hidden"]["workflows"] == [T1]
    assert data["last_workflow"]["by_project"] == {"ok": {"template_id": T1, "at": 5.0}}
    assert data["last_workflow"]["by_template"] == {T1: 0.0} and data["last_workflow"]["any"] == {}


def test_a_damaged_file_is_moved_aside_and_the_launcher_starts_fresh(user_dir):
    path = user_dir / "sonder-editor" / "launcher.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert launcher_state.read_state()["hidden"]["projects"] == []
    assert any(p.name.startswith("launcher.json.corrupt-") for p in path.parent.iterdir())


def test_preferred_workflow_falls_back_past_hidden_and_deleted_ones():
    launcher_state.record_open("Songs", T1, clock=lambda: 1.0)
    launcher_state.record_open("Other", T2, clock=lambda: 2.0)
    state = launcher_state.read_state()
    assert launcher_state.preferred_workflow(state, "Songs", [T1, T2, T3]) == T1
    # Never opened: the last used anywhere.
    assert launcher_state.preferred_workflow(state, "Fresh", [T1, T2, T3]) == T2
    # Its own was deleted: the last used anywhere.
    assert launcher_state.preferred_workflow(state, "Songs", [T2, T3]) == T2
    # Its own needs nodes that aren't installed: never preselected.
    assert launcher_state.preferred_workflow(state, "Songs", [T1, T2, T3], {T1}) == T2
    assert launcher_state.preferred_workflow(state, "Songs", [T1, T2, T3], {T1, T2}) == T3
    # Both hidden: the first remaining.
    state = launcher_state.set_hidden("workflow", T1, True)
    state = launcher_state.set_hidden("workflow", T2, True)
    assert launcher_state.preferred_workflow(state, "Songs", [T1, T2, T3]) == T3
    assert launcher_state.preferred_workflow(state, "Songs", [T1, T2]) == ""


def test_without_a_user_directory_reads_are_empty_and_writes_refuse(monkeypatch):
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(get_user_directory=lambda: ""))
    assert launcher_state.read_state()["hidden"]["projects"] == []
    with pytest.raises(launcher_state.LauncherStateUnavailable):
        launcher_state.set_hidden("project", "x", True)
