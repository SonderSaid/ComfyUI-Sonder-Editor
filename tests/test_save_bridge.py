"""Tests for the Sonder save bridge backend registration flow."""

import importlib
import inspect
import json
import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "video_editor_testpkg"


def test_save_bridge_resolves_completed_split_queue_job(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    pm = importlib.import_module(f"{TEST_PACKAGE}.server.project_manager")
    ts = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    project = pm.create_project("Terminal snapshot", base_dir=str(tmp_path))
    project.generation_queue = [ts.GenerationJob(job_id="terminal", status="completed",
        params={"snapshot_version": 1}, prompt_sections=[{"prompt": "kept"}])]
    pm.save_project(project, notify=False)
    loaded = pm.load_project(project.project_dir)
    resolved = io_nodes._find_queue_job(loaded, "terminal")
    assert resolved.status == "completed"
    assert resolved.prompt_sections == [{"prompt": "kept"}]


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
        for timer, _expires_at in io_nodes._BRIDGE_PREVIEW_CLEANUP_TIMERS.values():
            timer.cancel()
        io_nodes._BRIDGE_REGISTRY.clear()
        io_nodes._BRIDGE_PROMPT_WATCHERS.clear()
        io_nodes._BRIDGE_PREVIEW_CLEANUP_TIMERS.clear()
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


def _write_workflow_png(path: Path, workflow: dict):
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo

    path.parent.mkdir(parents=True, exist_ok=True)
    pnginfo = PngInfo()
    pnginfo.add_text("workflow", json.dumps(workflow), zip=False)
    Image.new("RGB", (2, 2), color=(32, 64, 96)).save(path, pnginfo=pnginfo)


def _load_saved_project(project_manager, project):
    return project_manager.load_project(project.project_dir)


def test_compact_guide_collision_provenance_never_claims_disabled_offset(tmp_path, monkeypatch):
    io_nodes, _timeline_state, _project_manager, project = _make_project(tmp_path, monkeypatch)
    context = {
        "guide_injection": {
            "auto_offset_enabled": False,
            "collision_count": 1,
            "driver_driver_collision_count": 0,
            "predicted_unresolved": True,
            "entries": [{
                "guide_id": "g1",
                "original_local_idx": 0,
                "effective_local_idx": 2,
                "collided": True,
                "collided_with": "driver:d1",
            }],
        }
    }

    export = io_nodes._compact_editor_export(project, context)

    assert export["guide_collision"]["applied"] == []
    assert export["guide_collision"]["detected"][0]["from"] == 0
    assert export["guide_collision"]["detected"][0]["to"] == 2


def test_guide_bleed_diagnostic_is_strictly_armed_and_bounded(tmp_path, monkeypatch):
    io_nodes, _timeline_state, _project_manager, _project = _make_project(tmp_path, monkeypatch)
    events = []
    monkeypatch.setattr(io_nodes, "record_diag_event", lambda kind, **details: events.append((kind, details)))
    check = {
        "armed": True,
        "expected_frame_count": 121,
        "step": 8,
        "max_excess_latents": 1,
        "guide_ids": ["g1"],
        "project_id": "p1",
    }

    assert io_nodes._record_guide_bleed_if_needed(check, 129, "test") is True
    assert events[0][0] == "guide_bleed_suspected"
    assert io_nodes._record_guide_bleed_if_needed({**check, "armed": False}, 129, "test") is False
    assert io_nodes._record_guide_bleed_if_needed(check, 137, "test") is False


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


def test_target_folder_outside_the_declared_combo_is_accepted(tmp_path, monkeypatch):
    """A real folder label must survive ComfyUI's prompt validation.

    `target_folder` is a combo whose declared options are only [""] — the project's
    actual folders are injected client-side by installBridgeFolderPicker and are never
    part of INPUT_TYPES. ComfyUI validates widget values against that *static* list and
    refuses the prompt with `value_not_in_list` before any node runs, unless the class
    declares VALIDATE_INPUTS naming the input or taking **kwargs. Without that hatch
    every folder a user can actually pick fails the render, and only Root works.

    The registration tests above call prepare_output() directly, so they pass either
    way; this asserts the contract ComfyUI itself reads.
    """
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    bridge_cls = io_nodes.SonderSaveBridge
    label = "FreshTake"

    declared = bridge_cls.INPUT_TYPES()["required"]["target_folder"][0]
    assert isinstance(declared, list), "target_folder is expected to stay a combo"
    assert label not in declared, (
        "declaration alone would accept the label, so this test no longer proves anything"
    )

    validate = getattr(bridge_cls, "VALIDATE_INPUTS", None)
    assert validate is not None, (
        "SonderSaveBridge must declare VALIDATE_INPUTS or ComfyUI refuses every folder label"
    )

    # Mirror ComfyUI's own guard in execution.validate_inputs: the combo membership
    # check is skipped only when the argspec names the input or accepts **kwargs.
    spec = inspect.getfullargspec(validate)
    assert "target_folder" in spec.args or spec.varkw is not None

    assert validate(target_folder=label) is True


def test_bridge_retains_native_preview_source_until_scheduled_cleanup(tmp_path, monkeypatch):
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)
    scheduled = []
    monkeypatch.setattr(
        io_nodes,
        "_schedule_bridge_preview_cleanup",
        lambda project_arg, bridge_dir, expires_at: scheduled.append((project_arg, bridge_dir, expires_at)),
    )

    output_dir, _ = io_nodes.SonderSaveBridge().prepare_output(
        project,
        prompt={},
        unique_id="bridge-1",
    )
    preview_path = Path(output_dir) / "native_00001_.png"
    _write_png(preview_path)

    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = _load_saved_project(project_manager, project)
    durable_path = Path(project.project_dir) / restored.assets[0].path
    assert preview_path.is_file()
    assert durable_path.is_file()
    assert preview_path.read_bytes() == durable_path.read_bytes()
    payload = io_nodes._read_bridge_sidecar_payload(output_dir)
    assert payload["existed"] == ["native_00001_.png"]
    assert payload["preview_expires_at"] > time.time()
    assert len(scheduled) == 1
    assert Path(scheduled[0][1]) == Path(output_dir)
    assert scheduled[0][2] == payload["preview_expires_at"]


def test_bridge_publication_falls_back_to_atomic_copy(tmp_path, monkeypatch):
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)
    monkeypatch.setattr(io_nodes, "_schedule_bridge_preview_cleanup", lambda *args, **kwargs: None)
    monkeypatch.setattr(io_nodes.os, "link", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no hardlinks")))

    output_dir, _ = io_nodes.SonderSaveBridge().prepare_output(project, prompt={}, unique_id="bridge-1")
    preview_path = Path(output_dir) / "fallback.png"
    _write_png(preview_path)

    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = _load_saved_project(project_manager, project)
    durable_path = Path(project.project_dir) / restored.assets[0].path
    assert preview_path.is_file()
    assert durable_path.is_file()
    assert preview_path.read_bytes() == durable_path.read_bytes()
    assert not os.path.samefile(preview_path, durable_path)
    assert list(durable_path.parent.glob(".*.bridge-publish.tmp")) == []


def test_bridge_upgrades_blank_same_path_placeholder(tmp_path, monkeypatch):
    io_nodes, timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)
    monkeypatch.setattr(io_nodes, "_build_bridge_naming_stem", lambda *args, **kwargs: "fixed_stem")

    placeholder_path = os.path.join("media", "fixed_stem_0001.png")
    project.assets.append(timeline_state.Asset(
        asset_id="auto-sync-placeholder",
        name="fixed_stem_0001.png",
        asset_type="image",
        path=placeholder_path,
    ))
    project_manager.save_project(project)

    bridge = io_nodes.SonderSaveBridge()
    output_dir, _filename_prefix = bridge.prepare_output(project, target_folder="Test Transfer", prompt={}, unique_id="bridge-1")
    _write_png(Path(output_dir) / "out.png")

    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = _load_saved_project(project_manager, project)
    same_path = [asset for asset in restored.assets if asset.path == placeholder_path]
    assert [asset.asset_id for asset in same_path] == ["auto-sync-placeholder"]
    upgraded = same_path[0]
    assert upgraded.folder == "Test Transfer"
    importlib.import_module(f"{TEST_PACKAGE}.server.project_storage").hydrate_asset(upgraded)
    assert upgraded.generation_params["editor_export"]["produced_by"]["tool"] == "sonder-editor"


def test_bridge_marks_asset_workflow_when_downstream_file_embeds_it(tmp_path, monkeypatch):
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)

    workflow = {"nodes": [{"id": 1, "type": "DownstreamSave"}]}
    bridge = io_nodes.SonderSaveBridge()
    output_dir, _filename_prefix = bridge.prepare_output(project, prompt={}, unique_id="bridge-1")
    _write_workflow_png(Path(output_dir) / "out.png", workflow)

    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = _load_saved_project(project_manager, project)
    importlib.import_module(f"{TEST_PACKAGE}.server.project_storage").hydrate_asset(restored.assets[0])
    editor_export = restored.assets[0].generation_params["editor_export"]
    assert editor_export["has_embedded_workflow"] is True
    assert "workflow_sha256" in editor_export
    assert "workflow" not in editor_export
    assert restored.assets[0].prompt == ""


def test_bridge_section_display_type_propagates(tmp_path, monkeypatch):
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)

    section = {
        "label": "Power LoRAs",
        "source_node_id": "10",
        "source_node_class": "Power Lora Loader (rgthree)",
        "source_node_title": "",
        "raw_widget_text": "lora_1: a.safetensors",
        "fields": {"power_loras": [{"slot": 1, "name": "a.safetensors", "enabled": True}]},
        "display_type": "power_loras",
    }
    project._execution_context = {io_nodes.TRACKED_METADATA_CONTEXT_KEY: {"C": [section]}}
    prompt = {"bridge-1": {"class_type": "SonderSaveBridge", "inputs": {"project": ["C", 0]}}}

    bridge = io_nodes.SonderSaveBridge()
    output_dir, _filename_prefix = bridge.prepare_output(project, prompt=prompt, unique_id="bridge-1")
    _write_png(Path(output_dir) / "out.png")
    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = _load_saved_project(project_manager, project)
    importlib.import_module(f"{TEST_PACKAGE}.server.project_storage").hydrate_asset(restored.assets[0])
    tracked = restored.assets[0].generation_params["editor_export"]["tracked_metadata"]
    assert tracked[0]["display_type"] == "power_loras"
    assert tracked[0]["fields"]["power_loras"][0]["name"] == "a.safetensors"


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
    output_dir, _ = bridge.prepare_output(project, mark_queue_complete=True, prompt={}, unique_id="bridge-1")

    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = _load_saved_project(project_manager, project)
    assert restored.generation_queue[0].status == "failed"
    assert restored.generation_queue[0].error == "Bridge terminal produced no files"
    assert restored.generation_queue[0].result_asset_id == ""
    assert not Path(output_dir).exists()


def test_bridge_unprobeable_output_cleans_staging_without_registration(tmp_path, monkeypatch):
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)

    output_dir, _ = io_nodes.SonderSaveBridge().prepare_output(project, prompt={}, unique_id="bridge-1")
    (Path(output_dir) / "broken.png").write_bytes(b"not an image")

    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = _load_saved_project(project_manager, project)
    assert restored.assets == []
    assert not Path(output_dir).exists()


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


def test_bridge_finalized_preview_marker_skips_adoption_and_expires(tmp_path, monkeypatch):
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    bridge_root = Path(project.project_dir) / "cache" / "bridge_out"
    preview_dir = bridge_root / "finalized-preview"
    preview_dir.mkdir()
    _write_png(preview_dir / "preview.png")
    io_nodes._write_bridge_sidecar(
        str(preview_dir),
        ["preview.png"],
        preview_expires_at=time.time() - 1,
    )
    monkeypatch.setattr(
        io_nodes,
        "_finalize_bridge_entry",
        lambda *_args, **_kwargs: pytest.fail("Finalized preview aliases must not be re-adopted"),
    )

    io_nodes._cleanup_stale_bridge_dirs(project.project_dir)

    assert not preview_dir.exists()
    assert _load_saved_project(project_manager, project).assets == []


def test_bridge_future_preview_marker_is_preserved_and_rescheduled(tmp_path, monkeypatch):
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    bridge_root = Path(project.project_dir) / "cache" / "bridge_out"
    preview_dir = bridge_root / "future-preview"
    preview_dir.mkdir()
    _write_png(preview_dir / "preview.png")
    expires_at = time.time() + 30
    io_nodes._write_bridge_sidecar(
        str(preview_dir),
        ["preview.png"],
        preview_expires_at=expires_at,
    )
    scheduled = []
    monkeypatch.setattr(
        io_nodes,
        "_schedule_bridge_preview_cleanup",
        lambda project_arg, bridge_dir, expiry: scheduled.append((project_arg, bridge_dir, expiry)),
    )

    io_nodes._cleanup_stale_bridge_dirs(project.project_dir)

    assert preview_dir.is_dir()
    assert scheduled == [(project.project_dir, str(preview_dir.resolve()), expires_at)]
    assert _load_saved_project(project_manager, project).assets == []


def test_bridge_preview_cleanup_timer_is_daemon_and_deletes_at_expiry(tmp_path, monkeypatch):
    io_nodes, _timeline_state, _project_manager, project = _make_project(tmp_path, monkeypatch)
    bridge_root = Path(project.project_dir) / "cache" / "bridge_out"
    preview_dir = bridge_root / "scheduled-preview"
    preview_dir.mkdir()
    _write_png(preview_dir / "preview.png")
    io_nodes._write_bridge_sidecar(
        str(preview_dir),
        ["preview.png"],
        preview_expires_at=160.0,
    )
    now = {"value": 100.0}
    monkeypatch.setattr(io_nodes.time, "time", lambda: now["value"])
    created = []

    class FakeTimer:
        def __init__(self, delay, callback):
            self.delay = delay
            self.callback = callback
            self.daemon = False
            self.started = False
            created.append(self)

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started

        def cancel(self):
            self.started = False

    monkeypatch.setattr(io_nodes.threading, "Timer", FakeTimer)

    timer = io_nodes._schedule_bridge_preview_cleanup(project, str(preview_dir), 160.0)

    assert timer is created[0]
    assert timer.delay == 60.0
    assert timer.daemon is True
    assert timer.started is True
    assert io_nodes._schedule_bridge_preview_cleanup(project, str(preview_dir), 160.0) is timer
    assert len(created) == 1
    now["value"] = 161.0
    timer.callback()
    assert not preview_dir.exists()
    timer_key = os.path.normcase(os.path.realpath(preview_dir))
    assert timer_key not in io_nodes._BRIDGE_PREVIEW_CLEANUP_TIMERS


def test_bridge_cleanup_failure_restores_preview_marker(tmp_path, monkeypatch):
    io_nodes, _timeline_state, _project_manager, project = _make_project(tmp_path, monkeypatch)
    bridge_root = Path(project.project_dir) / "cache" / "bridge_out"
    preview_dir = bridge_root / "locked-preview"
    preview_dir.mkdir()
    _write_png(preview_dir / "preview.png")
    expires_at = time.time() - 1
    io_nodes._write_bridge_sidecar(
        str(preview_dir),
        ["preview.png"],
        preview_expires_at=expires_at,
    )

    def fail_after_marker_removal(path):
        (Path(path) / io_nodes.BRIDGE_SCAN_SIDECAR).unlink()
        raise PermissionError("preview file is locked")

    monkeypatch.setattr(io_nodes.shutil, "rmtree", fail_after_marker_removal)

    assert io_nodes._cleanup_bridge_output_dir(project, str(preview_dir)) is False
    restored_payload = io_nodes._read_bridge_sidecar_payload(str(preview_dir))
    assert restored_payload["preview_expires_at"] == expires_at
    assert restored_payload["existed"] == ["preview.png"]


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


def test_bridge_mark_complete_with_empty_queue_job_id_logs_warning(tmp_path, monkeypatch, caplog):
    io_nodes, timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)

    queue_job = timeline_state.GenerationJob(job_id="job-1", scene_id="scene-1", status="running")
    project.generation_queue.append(queue_job)
    project._execution_context = {"queue_job_id": ""}
    project_manager.save_project(project)

    bridge = io_nodes.SonderSaveBridge()
    output_dir, _ = bridge.prepare_output(project, mark_queue_complete=True, prompt={}, unique_id="bridge-1")
    _write_png(Path(output_dir) / "orphan.png")

    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    caplog.set_level("WARNING", logger="sonder_editor")
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = _load_saved_project(project_manager, project)
    assert len(restored.assets) == 1
    assert restored.generation_queue[0].status == "running"
    assert restored.generation_queue[0].result_asset_id == ""
    assert "no queue_job_id" in caplog.text
    assert "registered=1" in caplog.text


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


def _leased_bridge_run(tmp_path, monkeypatch, *, with_file=False, job_status="running", leased=True):
    """A Bridge with Mark queue complete whose run leased job-1 of a 2-job batch."""
    io_nodes, timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)
    monkeypatch.setattr(io_nodes, "schedule_project_event", lambda *args, **kwargs: None)
    runs = importlib.import_module(f"{TEST_PACKAGE}.server.sdk_runs")
    runs.reset_for_tests()
    project.generation_queue = [
        timeline_state.GenerationJob(job_id="job-1", scene_id="s", status=job_status, batch_id="b",
                                     batch_index=0, batch_total=2),
        timeline_state.GenerationJob(job_id="job-2", scene_id="s", status="pending", batch_id="b",
                                     batch_index=1, batch_total=2),
    ]
    project._execution_context = {"queue_job_id": "job-1"}
    project_manager.save_project(project)
    output_dir, _ = io_nodes.SonderSaveBridge().prepare_output(
        project, mark_queue_complete=True, prompt={}, unique_id="bridge-1")
    if with_file:
        _write_png(Path(output_dir) / "partial.png")
    key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    runs.open_release(key, "prompt-x")
    runs.note_project(key, project.project_dir, ["p"])
    if leased:
        runs.take_lease(key, project.project_dir, "job-1")
    return io_nodes, project_manager, project, key


def _end_run(io_nodes, key, status):
    """What the task_done wrap does: finalize Bridges with the outcome, then release."""
    io_nodes._finalize_prompt_bridges(key, source="task_done", outcome=io_nodes._run_outcome(status)[0])
    io_nodes._release_prompt_run(key, status)


def _queue_state(project_manager, project):
    return {job.job_id: (job.status, job.error)
            for job in _load_saved_project(project_manager, project).generation_queue}


@pytest.mark.parametrize("messages,note", [
    ([("execution_error", {"node_type": "KSampler"})], "The run failed in KSampler before this job completed."),
    ([("execution_interrupted", {})], "The run was interrupted before this job completed."),
])
def test_a_bridge_run_that_errors_or_is_interrupted_returns_its_job_to_pending(tmp_path, monkeypatch, messages, note):
    io_nodes, project_manager, project, key = _leased_bridge_run(tmp_path, monkeypatch)
    _end_run(io_nodes, key, types.SimpleNamespace(status_str="error", messages=messages))
    assert _queue_state(project_manager, project) == {"job-1": ("pending", note), "job-2": ("pending", "")}


def test_an_unleased_job_keeps_the_old_rule_when_its_run_errors(tmp_path, monkeypatch):
    # No release would return an unleased job, so failing it stays the visible outcome.
    io_nodes, project_manager, project, key = _leased_bridge_run(tmp_path, monkeypatch, leased=False)
    _end_run(io_nodes, key, types.SimpleNamespace(status_str="error", messages=[("execution_interrupted", {})]))
    assert _queue_state(project_manager, project)["job-1"] == ("failed", "Bridge terminal produced no files")


def test_a_bridge_run_that_succeeds_without_files_still_fails_its_job(tmp_path, monkeypatch):
    io_nodes, project_manager, project, key = _leased_bridge_run(tmp_path, monkeypatch)
    _end_run(io_nodes, key, types.SimpleNamespace(status_str="success", messages=[]))
    state = _queue_state(project_manager, project)
    assert state["job-1"] == ("failed", "Bridge terminal produced no files")
    assert state["job-2"][0] == "failed"


def test_bridge_files_complete_the_job_even_when_the_run_is_interrupted_after(tmp_path, monkeypatch):
    io_nodes, project_manager, project, key = _leased_bridge_run(tmp_path, monkeypatch, with_file=True)
    _end_run(io_nodes, key, types.SimpleNamespace(status_str="error", messages=[("execution_interrupted", {})]))
    assert _queue_state(project_manager, project)["job-1"][0] == "completed"


def test_a_job_the_editor_already_failed_stays_failed_through_bridge_finalization(tmp_path, monkeypatch):
    io_nodes, project_manager, project, key = _leased_bridge_run(tmp_path, monkeypatch, job_status="failed")
    _end_run(io_nodes, key, types.SimpleNamespace(status_str="error",
                                                  messages=[("execution_error", {"node_type": "SonderEditor"})]))
    assert _queue_state(project_manager, project)["job-1"][0] == "failed"


def test_the_task_done_hook_passes_the_run_outcome_to_bridge_finalization(tmp_path, monkeypatch):
    seen = []

    class FakePromptQueue:
        currently_running = {}

        def get(self):
            return ((0, "prompt-err", {}, {}, []), "prompt-err")

        def task_done(self, item_id, *_args, **_kwargs):
            return item_id

    fake_queue = FakePromptQueue()
    fake_server = types.ModuleType("server")
    fake_server.PromptServer = types.SimpleNamespace(instance=types.SimpleNamespace(prompt_queue=fake_queue))
    monkeypatch.setitem(sys.modules, "server", fake_server)
    sys.modules.pop(f"{TEST_PACKAGE}.nodes.io_nodes", None)
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_finalize_prompt_bridges",
                        lambda key, source="unknown", outcome="": seen.append((key, source, outcome)))
    monkeypatch.setattr(io_nodes, "_release_prompt_run", lambda *args, **kwargs: None)
    assert io_nodes._install_bridge_prompt_queue_hooks() is True
    fake_queue.task_done("prompt-err", None,
                         types.SimpleNamespace(status_str="error", messages=[("execution_interrupted", {})]))
    assert seen == [("prompt-err", "task_done", "interrupted")]


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


def test_bridge_prepare_sanitizes_prompt_and_node_ids(tmp_path, monkeypatch):
    io_nodes, _timeline_state, _project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)
    monkeypatch.setattr(io_nodes, "_resolve_bridge_prompt_key", lambda _prompt: ("../prompt:key", "test"))

    output_dir, filename_prefix = io_nodes.SonderSaveBridge().prepare_output(
        project,
        prompt={},
        unique_id="../bridge:id",
    )

    bridge_root = (Path(project.project_dir) / "cache" / "bridge_out").resolve()
    output_path = Path(output_dir).resolve()
    assert output_path.is_relative_to(bridge_root)
    assert ".." not in filename_prefix.replace("\\", "/").split("/")
    assert "prompt_key_bridge_id" in str(output_path)


def test_bridge_finalize_skips_symlinked_output_escape_when_supported(tmp_path, monkeypatch):
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)

    output_dir, _ = io_nodes.SonderSaveBridge().prepare_output(project, prompt={}, unique_id="bridge-1")
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    external_file = external_dir / "secret.png"
    _write_png(external_file)
    try:
        (Path(output_dir) / "linked.png").symlink_to(external_file)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available in this environment")

    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = _load_saved_project(project_manager, project)
    assert restored.assets == []
    assert external_file.is_file()
    assert list((Path(project.project_dir) / "media").iterdir()) == []


def test_save_video_sanitizes_prefix_to_project_media(tmp_path, monkeypatch):
    io_nodes, _timeline_state, _project_manager, project = _make_project(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")

    def fake_encode(_frames, *, output_path, preset_id, custom_options=None, **_kwargs):
        Path(output_path).write_bytes(b"video")
        return io_nodes.metadata_for_save_preset(preset_id, custom_options)

    monkeypatch.setattr(io_nodes, "encode_video", fake_encode)
    monkeypatch.setattr(io_nodes, "save_project", lambda _project: None)

    result = io_nodes.SonderSaveVideo().save_video(
        project,
        torch.zeros(1, 2, 2, 3),
        filename_prefix="../outside/escape:name",
        embed_metadata=False,
    )

    output_path = Path(result["result"][0])
    assert output_path.parent.resolve() == (Path(project.project_dir) / "media").resolve()
    assert output_path.name.startswith("outside_escape_name_")
    assert ".." not in project.assets[0].path.replace("\\", "/").split("/")
    assert project.assets[0].path.replace("\\", "/").startswith("media/outside_escape_name_")
    assert not (tmp_path / "outside").exists()


def test_save_video_rejects_symlinked_media_root_when_supported(tmp_path, monkeypatch):
    io_nodes, _timeline_state, _project_manager, project = _make_project(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    media_dir = Path(project.project_dir) / "media"
    shutil.rmtree(media_dir)
    external_dir = tmp_path / "external-media"
    external_dir.mkdir()
    try:
        media_dir.symlink_to(external_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available in this environment")

    monkeypatch.setattr(io_nodes, "encode_video", lambda *args, **kwargs: pytest.fail("encode should not run"))

    with pytest.raises(ValueError, match="Invalid project media directory"):
        io_nodes.SonderSaveVideo().save_video(
            project,
            torch.zeros(1, 2, 2, 3),
            filename_prefix="safe",
            embed_metadata=False,
        )


def test_save_video_rejects_media_root_symlink_to_project_sibling_when_supported(tmp_path, monkeypatch):
    io_nodes, _timeline_state, _project_manager, project = _make_project(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    media_dir = Path(project.project_dir) / "media"
    cache_dir = Path(project.project_dir) / "cache"
    shutil.rmtree(media_dir)
    cache_dir.mkdir(exist_ok=True)
    try:
        media_dir.symlink_to(cache_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available in this environment")

    monkeypatch.setattr(io_nodes, "encode_video", lambda *args, **kwargs: pytest.fail("encode should not run"))

    with pytest.raises(ValueError, match="Invalid project media directory"):
        io_nodes.SonderSaveVideo().save_video(
            project,
            torch.zeros(1, 2, 2, 3),
            filename_prefix="safe",
            embed_metadata=False,
        )


def test_bridge_prepare_omits_filename_prefix_outside_comfy_output_root(tmp_path, monkeypatch):
    io_nodes, _timeline_state, _project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)
    output_root = tmp_path / "comfy-output"
    output_root.mkdir()
    monkeypatch.setattr(sys.modules["folder_paths"], "get_output_directory", lambda: str(output_root))

    output_dir, filename_prefix = io_nodes.SonderSaveBridge().prepare_output(
        project,
        prompt={},
        unique_id="bridge-1",
    )

    bridge_root = (Path(project.project_dir) / "cache" / "bridge_out").resolve()
    assert Path(output_dir).resolve().is_relative_to(bridge_root)
    assert filename_prefix == ""


def test_bridge_cleanup_rejects_bridge_root_symlink_to_project_when_supported(tmp_path, monkeypatch):
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    bridge_root = Path(project.project_dir) / "cache" / "bridge_out"
    if bridge_root.is_symlink() or bridge_root.is_file():
        bridge_root.unlink()
    elif bridge_root.exists():
        shutil.rmtree(bridge_root)
    bridge_root.parent.mkdir(parents=True, exist_ok=True)
    victim_dir = Path(project.project_dir) / "old-victim"
    victim_dir.mkdir()
    _write_png(victim_dir / "leftover.png")
    old_ts = time.time() - (io_nodes.BRIDGE_STALE_DIR_TTL_SEC + 60)
    os.utime(victim_dir / "leftover.png", (old_ts, old_ts))
    os.utime(victim_dir, (old_ts, old_ts))
    try:
        bridge_root.symlink_to(Path(project.project_dir), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available in this environment")

    io_nodes._cleanup_stale_bridge_dirs(project.project_dir)

    assert victim_dir.exists()
    assert (victim_dir / "leftover.png").is_file()
    restored = _load_saved_project(project_manager, project)
    assert restored.assets == []


def test_bridge_finalize_does_not_register_output_swapped_to_symlink_when_supported(tmp_path, monkeypatch):
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)

    output_dir, _ = io_nodes.SonderSaveBridge().prepare_output(project, prompt={}, unique_id="bridge-1")
    source_file = Path(output_dir) / "out.png"
    _write_png(source_file)
    external_file = tmp_path / "external.png"
    _write_png(external_file)
    probe_link = tmp_path / "probe-link.png"
    try:
        probe_link.symlink_to(external_file)
        probe_link.unlink()
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available in this environment")

    original_move = io_nodes.shutil.move

    def swap_to_symlink_before_move(src, dst, *args, **kwargs):
        source_path = Path(src)
        source_path.unlink()
        source_path.symlink_to(external_file)
        return original_move(src, dst, *args, **kwargs)

    monkeypatch.setattr(io_nodes.shutil, "move", swap_to_symlink_before_move)

    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = _load_saved_project(project_manager, project)
    assert restored.assets == []
    assert external_file.is_file()
    assert list((Path(project.project_dir) / "media").iterdir()) == []


def test_bridge_repeated_within_one_prompt_keeps_files_already_written(tmp_path, monkeypatch):
    # ComfyUI can re-execute a finished bridge in the same prompt (--cache-none or
    # RAM-pressure eviction, pulled in by a lazy input). Preparing again would list
    # the saver's files as pre-existing, so they would never register.
    io_nodes, _timeline_state, project_manager, project = _make_project(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)
    runs = io_nodes.sdk_runs
    runs.reset_for_tests()
    prompt = {"bridge-1": {"class_type": "SonderSaveBridge", "inputs": {}}}
    io_nodes._set_bridge_prompt_key_for_object("41", prompt)
    runs.open_release("41", "prompt-41", prompt)

    first = io_nodes.SonderSaveBridge().prepare_output(project, prompt=prompt, unique_id="bridge-1")
    _write_png(Path(first[0]) / "frame.png")
    again = io_nodes.SonderSaveBridge().prepare_output(project, prompt=prompt, unique_id="bridge-1")
    assert again == first

    io_nodes._finalize_prompt_bridges("41")
    restored = _load_saved_project(project_manager, project)
    assert len(restored.assets) == 1
    runs.reset_for_tests()
