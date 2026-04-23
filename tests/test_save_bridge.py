"""Tests for the Sonder save bridge backend registration flow."""

import importlib
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "video_editor_testpkg"


def _import_io_nodes(tmp_path, monkeypatch):
    pytest.importorskip("torch")
    pytest.importorskip("cv2")

    temp_dir = tmp_path / "temp"
    input_dir = tmp_path / "input"
    temp_dir.mkdir(exist_ok=True)
    input_dir.mkdir(exist_ok=True)
    folder_paths = types.SimpleNamespace(
        get_output_directory=lambda: str(tmp_path),
        get_temp_directory=lambda: str(temp_dir),
        get_input_directory=lambda: str(input_dir),
        filter_files_content_types=lambda files, content_types: files,
    )
    monkeypatch.setitem(sys.modules, "folder_paths", folder_paths)

    if TEST_PACKAGE not in sys.modules:
        pkg = types.ModuleType(TEST_PACKAGE)
        pkg.__path__ = [str(ROOT)]
        sys.modules[TEST_PACKAGE] = pkg

    importlib.invalidate_caches()
    return importlib.import_module(f"{TEST_PACKAGE}.nodes.io_nodes")


def _clear_bridge_state(io_nodes):
    with io_nodes._BRIDGE_REGISTRY_LOCK:
        io_nodes._BRIDGE_REGISTRY.clear()
        io_nodes._BRIDGE_PROMPT_WATCHERS.clear()
        io_nodes._BRIDGE_PROMPT_KEY_BY_OBJECT_ID.clear()
        io_nodes._BRIDGE_PROMPT_OBJECT_IDS_BY_KEY.clear()
    io_nodes._BRIDGE_HOOKED_PROMPT_QUEUE_ID = None


def _make_project(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    project_manager = importlib.import_module(f"{TEST_PACKAGE}.server.project_manager")
    thumbnail_service = importlib.import_module(f"{TEST_PACKAGE}.server.thumbnail_service")
    monkeypatch.setattr(thumbnail_service, "ensure_thumbnail", lambda *args, **kwargs: None)

    base_dir = tmp_path / "projects"
    base_dir.mkdir(exist_ok=True)
    project = project_manager.create_project("Bridge Test", base_dir=str(base_dir))
    return io_nodes, timeline_state, project_manager, project


def _write_png(path: Path):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2, 2), color=(32, 64, 96)).save(path)


def _load_saved_project(project_manager, project):
    return project_manager.load_project(project.project_dir)


def test_bridge_registers_image_output(tmp_path, monkeypatch):
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)

    prompt = {}
    bridge = io_nodes.SonderSaveBridge()
    output_dir, _filename_prefix = bridge.prepare_output(project, target_folder="FreshTake", prompt=prompt, unique_id="bridge-1")
    _write_png(Path(output_dir) / "out.png")

    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = _load_saved_project(project_manager, project)
    assert len(restored.assets) == 1
    asset = restored.assets[0]
    assert asset.asset_type == "image"
    assert asset.folder == "FreshTake"
    assert asset.path.startswith(os.path.join("media", "Bridge_Test_"))
    assert asset.path.endswith("_0001.png")


def test_bridge_registers_artifact_for_unknown_extension(tmp_path, monkeypatch):
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)

    prompt = {}
    bridge = io_nodes.SonderSaveBridge()
    output_dir, _filename_prefix = bridge.prepare_output(project, prompt=prompt, unique_id="bridge-1")
    target = Path(output_dir) / "tensor.xyz"
    target.write_text("opaque", encoding="utf-8")

    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = _load_saved_project(project_manager, project)
    assert len(restored.assets) == 1
    asset = restored.assets[0]
    assert asset.asset_type == "artifact"
    assert asset.artifact_kind == "other"


def test_bridge_no_output_fails_terminal_queue_job(tmp_path, monkeypatch):
    io_nodes, timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)

    queue_job = timeline_state.GenerationJob(job_id="job-1", scene_id="scene-1", status="running")
    project.generation_queue.append(queue_job)
    project._execution_context = {"queue_job_id": "job-1"}
    project_manager.save_project(project)

    bridge = io_nodes.SonderSaveBridge()
    bridge.prepare_output(project, mark_queue_complete=True, prompt={}, unique_id="bridge-1")

    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = _load_saved_project(project_manager, project)
    assert restored.generation_queue[0].status == "failed"
    assert restored.generation_queue[0].error == "Bridge terminal produced no files"
    assert restored.generation_queue[0].result_asset_id == ""


def test_bridge_multi_in_one_prompt_isolation(tmp_path, monkeypatch):
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)

    prompt = {}
    bridge = io_nodes.SonderSaveBridge()
    output_a, _ = bridge.prepare_output(project, prompt=prompt, unique_id="bridge-a")
    output_b, _ = bridge.prepare_output(project, prompt=prompt, unique_id="bridge-b")
    _write_png(Path(output_a) / "a.png")
    _write_png(Path(output_b) / "b.png")

    prompt_keys = {prompt_key for (prompt_key, _node_id) in io_nodes._BRIDGE_REGISTRY.keys()}
    assert len(prompt_keys) == 1
    io_nodes._finalize_prompt_bridges(next(iter(prompt_keys)))

    restored = _load_saved_project(project_manager, project)
    names = sorted(asset.name for asset in restored.assets)
    assert len(names) == 2
    assert all(name.startswith("Bridge_Test_") for name in names)
    assert any(name.endswith("_0001.png") for name in names)


def test_bridge_nested_subdir_scan(tmp_path, monkeypatch):
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)

    bridge = io_nodes.SonderSaveBridge()
    output_dir, _ = bridge.prepare_output(project, prompt={}, unique_id="bridge-1")
    _write_png(Path(output_dir) / "nested" / "frame.png")

    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = _load_saved_project(project_manager, project)
    assert restored.assets[0].path.startswith(os.path.join("media", "Bridge_Test_"))
    assert restored.assets[0].path.endswith("_0001.png")


def test_bridge_filename_collision_suffix(tmp_path, monkeypatch):
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)
    monkeypatch.setattr(io_nodes, "_build_bridge_naming_stem", lambda *args, **kwargs: "fixed_stem")

    existing = Path(project.project_dir) / "media" / "fixed_stem_0001.png"
    _write_png(existing)

    bridge = io_nodes.SonderSaveBridge()
    output_dir, _ = bridge.prepare_output(project, prompt={}, unique_id="bridge-1")
    _write_png(Path(output_dir) / "out.png")

    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = _load_saved_project(project_manager, project)
    assert restored.assets[0].path == os.path.join("media", "fixed_stem_0001_1.png")


def test_bridge_sidecar_ignore(tmp_path, monkeypatch):
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)

    bridge = io_nodes.SonderSaveBridge()
    output_dir, _ = bridge.prepare_output(project, prompt={}, unique_id="bridge-1")
    output_path = Path(output_dir)
    with open(output_path / io_nodes.BRIDGE_SCAN_SIDECAR, "w", encoding="utf-8") as handle:
        json.dump({"existed": ["ignored.txt"]}, handle)
    (output_path / "ignored.txt").write_text("old", encoding="utf-8")
    (output_path / "fresh.txt").write_text("new", encoding="utf-8")

    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = _load_saved_project(project_manager, project)
    assert len(restored.assets) == 1
    only_asset = restored.assets[0]
    assert only_asset.name.startswith("Bridge_Test_")
    assert only_asset.name.endswith("_0001.txt")


def test_bridge_cleanup_adopts_stale_dirs(tmp_path, monkeypatch):
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)

    stale_dir = Path(project.project_dir) / "cache" / "bridge_out" / "stale_0_13"
    stale_dir.mkdir(parents=True, exist_ok=True)
    _write_png(stale_dir / "leftover.png")
    old_ts = time.time() - (io_nodes.BRIDGE_STALE_DIR_TTL_SEC + 60)
    os.utime(stale_dir / "leftover.png", (old_ts, old_ts))
    os.utime(stale_dir, (old_ts, old_ts))

    io_nodes._cleanup_stale_bridge_dirs(project.project_dir)

    assert not stale_dir.exists()

    restored = _load_saved_project(project_manager, project)
    assert len(restored.assets) == 1
    adopted = restored.assets[0]
    assert adopted.asset_type == "image"
    assert adopted.name.startswith("bridge_recovered_")
    assert adopted.name.endswith("_0001.png")


def test_bridge_terminal_marks_queue_complete_after_registration(tmp_path, monkeypatch):
    io_nodes, timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)

    queue_job = timeline_state.GenerationJob(job_id="job-1", scene_id="scene-1", status="running")
    project.generation_queue.append(queue_job)
    project._execution_context = {"queue_job_id": "job-1", "prompt": "bridge prompt"}
    project_manager.save_project(project)

    observed = []
    actual_save = project_manager.save_project
    monkeypatch.setattr(io_nodes, "save_project", lambda project_obj: (observed.append((project_obj.generation_queue[0].status, len(project_obj.assets), project_obj.generation_queue[0].result_asset_id)), actual_save(project_obj)))

    bridge = io_nodes.SonderSaveBridge()
    output_dir, _ = bridge.prepare_output(project, mark_queue_complete=True, prompt={}, unique_id="bridge-1")
    _write_png(Path(output_dir) / "final.png")

    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = _load_saved_project(project_manager, project)
    assert restored.generation_queue[0].status == "completed"
    assert restored.generation_queue[0].result_asset_id == restored.assets[0].asset_id
    assert observed[-1][0] == "completed"
    assert observed[-1][1] == 1
    assert observed[-1][2] == restored.assets[0].asset_id


def test_bridge_not_terminal_does_not_mark_queue_complete(tmp_path, monkeypatch):
    io_nodes, timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)

    queue_job = timeline_state.GenerationJob(job_id="job-1", scene_id="scene-1", status="running")
    project.generation_queue.append(queue_job)
    project._execution_context = {"queue_job_id": "job-1"}
    project_manager.save_project(project)

    bridge = io_nodes.SonderSaveBridge()
    output_dir, _ = bridge.prepare_output(project, mark_queue_complete=False, prompt={}, unique_id="bridge-1")
    _write_png(Path(output_dir) / "kept.png")

    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = _load_saved_project(project_manager, project)
    assert restored.generation_queue[0].status == "running"
    assert restored.generation_queue[0].result_asset_id == ""


def test_bridge_uses_hidden_unique_id_and_backend_prompt_key(tmp_path, monkeypatch):
    io_nodes, _timeline_state, _project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)

    prompt = {}
    bridge = io_nodes.SonderSaveBridge()
    output_dir, filename_prefix = bridge.prepare_output(project, prompt=prompt, unique_id="bridge-123")

    ((prompt_key, bridge_node_id), entry), = io_nodes._BRIDGE_REGISTRY.items()
    assert prompt_key
    assert bridge_node_id == "bridge-123"
    assert entry["prompt_key"] == prompt_key
    assert output_dir.endswith(f"{prompt_key}_bridge-123")
    assert f"{prompt_key}_bridge-123/" in filename_prefix
    assert filename_prefix.endswith(entry["naming_stem"])
    assert entry["naming_stem"].startswith("Bridge_Test_")


def test_bridge_uses_prompt_queue_task_done_hook(tmp_path, monkeypatch):
    prompt = {
        "editor-1": {
            "class_type": "SonderEditor",
            "inputs": {},
        },
    }

    class FakePromptQueue:
        def __init__(self, prompt_graph):
            self.prompt_graph = prompt_graph
            self.currently_running = {}

        def get(self):
            self.currently_running["prompt-abc"] = (self.prompt_graph,)
            return ((0, "prompt-abc", self.prompt_graph, {}, []), "prompt-abc")

        def task_done(self, item_id, *_args, **_kwargs):
            self.currently_running.pop(str(item_id), None)
            return item_id

    fake_prompt_queue = FakePromptQueue(prompt)
    fake_prompt_server = types.SimpleNamespace(instance=types.SimpleNamespace(prompt_queue=fake_prompt_queue))
    fake_server_module = types.ModuleType("server")
    fake_server_module.PromptServer = fake_prompt_server
    monkeypatch.setitem(sys.modules, "server", fake_server_module)
    sys.modules.pop(f"{TEST_PACKAGE}.nodes.io_nodes", None)

    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)

    assert io_nodes._install_bridge_prompt_queue_hooks() is True
    fake_prompt_queue.get()

    bridge = io_nodes.SonderSaveBridge()
    output_dir, _ = bridge.prepare_output(project, prompt=prompt, unique_id="bridge-1")
    _write_png(Path(output_dir) / "hooked.png")

    ((prompt_key, _bridge_node_id), _entry), = io_nodes._BRIDGE_REGISTRY.items()
    assert prompt_key == "prompt-abc"
    assert "prompt-abc" in io_nodes._BRIDGE_PROMPT_WATCHERS

    fake_prompt_queue.task_done("prompt-abc")

    restored = _load_saved_project(project_manager, project)
    assert len(restored.assets) == 1
    assert restored.assets[0].name.startswith("Bridge_Test_")
    assert restored.assets[0].name.endswith("_0001.png")
    assert not io_nodes._BRIDGE_REGISTRY


def test_multi_terminal_first_wins(tmp_path, monkeypatch):
    io_nodes, timeline_state, _project_manager, _project = _make_project(tmp_path, monkeypatch)

    queue_job = timeline_state.GenerationJob(job_id="job-1", status="running")
    project = timeline_state.TimelineProject(generation_queue=[queue_job])

    first = io_nodes._mark_queue_job_completed(project, "job-1", "asset-a")
    second = io_nodes._mark_queue_job_completed(project, "job-1", "asset-b")

    assert first is True
    assert second is False
    assert project.generation_queue[0].status == "completed"
    assert project.generation_queue[0].result_asset_id == "asset-a"


def test_bridge_builds_naming_stem_from_context(tmp_path, monkeypatch):
    io_nodes, _timeline_state, _project_manager, project = _make_project(tmp_path, monkeypatch)

    project.name = "My Test!"
    context = {"scene_name": "Scene / One"}
    stem = io_nodes._build_bridge_naming_stem(project, context, "hero")

    parts = stem.split("_")
    assert parts[0] == "hero"
    assert parts[1] == "My"
    assert parts[2] == "Test"
    assert parts[3] == "Scene"
    assert parts[4] == "One"
    date_part = parts[-1]
    assert len(date_part) == 15 and date_part[8] == "-"


def test_bridge_naming_stem_omits_empty_components(tmp_path, monkeypatch):
    io_nodes, _timeline_state, _project_manager, project = _make_project(tmp_path, monkeypatch)

    project.name = "MyProj"
    stem = io_nodes._build_bridge_naming_stem(project, {}, "")
    parts = stem.split("_")
    assert parts[0] == "MyProj"
    assert len(parts) == 2
    assert len(parts[1]) == 15 and parts[1][8] == "-"


def test_bridge_renames_files_to_stem_on_move(tmp_path, monkeypatch):
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)
    monkeypatch.setattr(io_nodes, "_build_bridge_naming_stem", lambda *args, **kwargs: "renamed_stem")

    bridge = io_nodes.SonderSaveBridge()
    output_dir, _ = bridge.prepare_output(project, prompt={}, unique_id="bridge-1")
    _write_png(Path(output_dir) / "whatever_the_node_wrote_00001_.png")
    _write_png(Path(output_dir) / "another.png")

    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = _load_saved_project(project_manager, project)
    names = sorted(asset.name for asset in restored.assets)
    assert names == ["renamed_stem_0001.png", "renamed_stem_0002.png"]


def test_bridge_stale_dir_adopted_not_deleted(tmp_path, monkeypatch):
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)

    stale_dir = Path(project.project_dir) / "cache" / "bridge_out" / "6_13"
    stale_dir.mkdir(parents=True, exist_ok=True)
    _write_png(stale_dir / "orphan1.png")
    _write_png(stale_dir / "orphan2.png")
    old_ts = time.time() - (io_nodes.BRIDGE_STALE_DIR_TTL_SEC + 60)
    for child in stale_dir.iterdir():
        os.utime(child, (old_ts, old_ts))
    os.utime(stale_dir, (old_ts, old_ts))

    io_nodes._cleanup_stale_bridge_dirs(project.project_dir)

    assert not stale_dir.exists()
    restored = _load_saved_project(project_manager, project)
    assert len(restored.assets) == 2
    names = sorted(asset.name for asset in restored.assets)
    assert all(name.startswith("bridge_recovered_") for name in names)
    assert names[0].endswith("_0001.png")
    assert names[1].endswith("_0002.png")


def test_bridge_watcher_starts_regardless_of_prompt_key_source(tmp_path, monkeypatch):
    io_nodes, _timeline_state, _project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)

    started = []
    monkeypatch.setattr(
        io_nodes,
        "_ensure_prompt_bridge_watcher",
        lambda prompt_key, prompt: started.append(prompt_key),
    )
    monkeypatch.setattr(
        io_nodes,
        "_resolve_bridge_prompt_key",
        lambda prompt: ("fake-prompt-key", "queue_hook"),
    )

    bridge = io_nodes.SonderSaveBridge()
    bridge.prepare_output(project, prompt={}, unique_id="bridge-1")

    assert started == ["fake-prompt-key"]


def test_bridge_rename_handles_extensionless_and_multi_extension(tmp_path, monkeypatch):
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)
    monkeypatch.setattr(io_nodes, "_build_bridge_naming_stem", lambda *args, **kwargs: "stem")

    bridge = io_nodes.SonderSaveBridge()
    output_dir, _ = bridge.prepare_output(project, prompt={}, unique_id="bridge-1")
    (Path(output_dir) / "archive.tar.gz").write_bytes(b"opaque")
    (Path(output_dir) / "rawdata").write_bytes(b"opaque")

    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = _load_saved_project(project_manager, project)
    names = sorted(asset.name for asset in restored.assets)
    assert names == ["stem_0001.gz", "stem_0002"]


def test_bridge_sanitize_collapses_repeated_separators(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    assert io_nodes._sanitize_bridge_component("foo///bar") == "foo_bar"
    assert io_nodes._sanitize_bridge_component("__abc__") == "abc"
    assert io_nodes._sanitize_bridge_component("!!!") == ""
    assert io_nodes._sanitize_bridge_component("A-B_C") == "A-B_C"
