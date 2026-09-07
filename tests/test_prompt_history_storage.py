import copy
import json
from pathlib import Path

import pytest

from server import project_manager as pm, project_storage as ps


def indexed(tmp_path):
    p = pm.create_project("History", base_dir=str(tmp_path))
    history = [{"hash": str(i), "ts": "old", "future": "x" * 2000} for i in range(8)]
    ps.stage_prompt_history(p, history)
    pm.save_project(p, notify=False)
    return p, history


def test_changed_entry_publishes_only_entry_and_index(tmp_path, monkeypatch):
    p, history = indexed(tmp_path)
    written = []
    publish = ps.publish_bytes
    def record(path, data):
        written.append((Path(path).name, len(data)))
        return publish(path, data)
    monkeypatch.setattr(ps, "publish_bytes", record)
    history[0]["ts"] = "new"
    ps.stage_prompt_history(p, history)
    pm.save_project(p, notify=False)
    assert len(written) == 2
    assert {name.split("-")[0] for name, _ in written} == {"history_entry", "prompt_history"}
    assert sum(size for _, size in written) < len(json.dumps(history)) / 2
    assert ps.read_prompt_history(p.project_dir) == history


def test_format_one_upgrades_only_on_history_change(tmp_path):
    p, history = indexed(tmp_path)
    path = Path(p.project_dir) / "project.json"
    raw = json.loads(path.read_bytes())
    old = ps.publish_component(p.project_dir, "prompt_history", history)
    raw["storage"]["format_version"] = 1
    raw["storage"]["components"]["prompt_history"] = old
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = path.read_bytes()
    p = pm.load_project(p.project_dir)
    assert path.read_bytes() == before
    assert ps.read_prompt_history(p) == history
    pm.save_project(p, notify=False)
    assert json.loads(path.read_bytes())["storage"]["format_version"] == 1
    ps.stage_prompt_history(p, history + [{"unknown": [1, 2]}])
    with pytest.raises(ps.ProjectStorageError, match="no-bump"):
        pm.save_project(p, bump_modified_at=False, notify=False)
    pm.save_project(p, notify=False)
    assert json.loads(path.read_bytes())["storage"]["format_version"] == 2
    assert ps.read_prompt_history(p)[-1] == {"unknown": [1, 2]}


def test_migration_keeps_over_cap_and_duplicate_unknown_entries(tmp_path):
    p = pm.create_project("Over cap", base_dir=str(tmp_path))
    history = [{"hash": "same", "unknown": i} for i in range(205)] + [None, "opaque"]
    ps.stage_prompt_history(p, history)
    pm.save_project(p, notify=False)
    assert ps.read_prompt_history(p) == history


@pytest.mark.parametrize("boundary", ["entry", "index", "root"])
def test_history_publication_failure_preserves_root_and_shadow(tmp_path, monkeypatch, boundary):
    p, history = indexed(tmp_path)
    path = Path(p.project_dir) / "project.json"
    before = path.read_bytes()
    shadow = copy.deepcopy(p._raw_data)
    ps.stage_prompt_history(p, history + [{"new": True}])
    original = ps.publish_bytes
    def fail(path, data):
        original(path, data)
        if (boundary == "entry" and Path(path).name.startswith("history_entry-")) or (boundary == "index" and Path(path).name.startswith("prompt_history-")):
            raise OSError("injected")
    monkeypatch.setattr(ps, "publish_bytes", fail)
    if boundary == "root":
        monkeypatch.setattr(pm, "atomic_replace", lambda *_: (_ for _ in ()).throw(OSError("injected")))
    with pytest.raises(OSError):
        pm.save_project(p, notify=False)
    assert path.read_bytes() == before
    assert p._raw_data == shadow
    assert ps.read_prompt_history(p.project_dir) == history


def test_missing_index_child_refuses_load_and_history_read(tmp_path):
    p, _ = indexed(tmp_path)
    index = ps.read_component(p.project_dir, "prompt_history", p._raw_data["storage"]["components"]["prompt_history"])
    (Path(p.project_dir) / index["entries"][0]["path"]).unlink()
    with pytest.raises(ps.ProjectStorageError):
        pm.load_project(p.project_dir)
    with pytest.raises(ps.ProjectStorageError):
        ps.read_prompt_history(p.project_dir)
