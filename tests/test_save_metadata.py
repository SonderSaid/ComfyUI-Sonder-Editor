import importlib
import sys
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


def _timeline_state():
    return importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")


def _project(tmp_path, with_scene=False):
    timeline_state = _timeline_state()
    scenes = []
    if with_scene:
        scenes = [timeline_state.Scene(scene_id="scene-1", name="Scene 1", duration_frames=24)]
    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True, exist_ok=True)
    (project_dir / "cache" / "thumbnails").mkdir(parents=True, exist_ok=True)
    return timeline_state.TimelineProject(project_dir=str(project_dir), name="Metadata Test", scenes=scenes)


def _fake_encode(io_nodes, calls):
    def fake(frames_iter, *, preset_id, output_path, fps, audio_path=None, custom_options=None, timeout=90, embed_metadata=None, **_kwargs):
        Path(output_path).write_bytes(b"video")
        calls.append({"embed_metadata": embed_metadata, "output_path": output_path})
        return io_nodes.metadata_for_save_preset(preset_id, custom_options)
    return fake


def _patch_save_deps(io_nodes, monkeypatch, calls):
    thumbnail_service = importlib.import_module(f"{TEST_PACKAGE}.server.thumbnail_service")
    monkeypatch.setattr(io_nodes, "encode_video", _fake_encode(io_nodes, calls))
    monkeypatch.setattr(io_nodes, "save_project", lambda project: None)
    monkeypatch.setattr(thumbnail_service, "ensure_thumbnail", lambda *args, **kwargs: None)


def _clear_bridge_state(io_nodes):
    with io_nodes._BRIDGE_REGISTRY_LOCK:
        io_nodes._BRIDGE_REGISTRY.clear()
        io_nodes._BRIDGE_PROMPT_WATCHERS.clear()
        io_nodes._BRIDGE_PROMPT_KEY_BY_OBJECT_ID.clear()
        io_nodes._BRIDGE_PROMPT_OBJECT_IDS_BY_KEY.clear()
    io_nodes._BRIDGE_HOOKED_PROMPT_QUEUE_ID = None


def test_save_video_editor_export_includes_schema_version(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    calls = []
    _patch_save_deps(io_nodes, monkeypatch, calls)
    project = _project(tmp_path)

    io_nodes.SonderSaveVideo().save_video(project, torch.zeros(1, 2, 2, 3), prompt={}, extra_pnginfo={"workflow": {}})

    editor_export = project.assets[0].generation_params["editor_export"]
    assert editor_export["schema_version"] == "1.0"
    assert editor_export["produced_by"]["tool"] == "sonder-editor"


def test_save_video_default_embed_file_payload_includes_prompt_workflow(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    calls = []
    _patch_save_deps(io_nodes, monkeypatch, calls)
    project = _project(tmp_path)
    prompt = {"1": {"class_type": "Node", "inputs": {"seed": 1}}}
    workflow = {"nodes": [{"id": 1, "type": "Node"}]}

    io_nodes.SonderSaveVideo().save_video(project, torch.zeros(1, 2, 2, 3), prompt=prompt, extra_pnginfo={"workflow": workflow})

    payload = calls[-1]["embed_metadata"]
    assert payload and "prompt" in payload and "workflow" in payload and "editor_export" in payload
    editor_export = project.assets[0].generation_params["editor_export"]
    assert editor_export["has_embedded_workflow"] is True
    assert "workflow_sha256" in editor_export
    assert "prompt" not in editor_export
    assert "workflow" not in editor_export


def test_save_video_embed_off_omits_raw_graph_keys(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    calls = []
    _patch_save_deps(io_nodes, monkeypatch, calls)
    project = _project(tmp_path)

    io_nodes.SonderSaveVideo().save_video(
        project,
        torch.zeros(1, 2, 2, 3),
        embed_metadata=False,
        prompt={"1": {}},
        extra_pnginfo={"workflow": {"nodes": []}},
    )

    assert calls[-1]["embed_metadata"] is None
    editor_export = project.assets[0].generation_params["editor_export"]
    assert editor_export["has_embedded_workflow"] is False
    assert "prompt" not in editor_export
    assert "workflow" not in editor_export


def test_save_video_tracked_metadata_propagates(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    calls = []
    _patch_save_deps(io_nodes, monkeypatch, calls)
    project = _project(tmp_path)
    project._execution_context = {
        io_nodes.TRACKED_METADATA_CONTEXT_KEY: {"C": [{"label": "Sampler", "fields": {"cfg": 7}}]},
    }
    prompt = {"S": {"class_type": "SonderSaveVideo", "inputs": {"project": ["C", 0]}}}

    io_nodes.SonderSaveVideo().save_video(project, torch.zeros(1, 2, 2, 3), embed_metadata=False, prompt=prompt, unique_id="S")

    tracked = project.assets[0].generation_params["editor_export"]["tracked_metadata"]
    assert tracked == [{"label": "Sampler", "fields": {"cfg": 7}}]


def test_save_video_display_type_propagates(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    calls = []
    _patch_save_deps(io_nodes, monkeypatch, calls)
    project = _project(tmp_path)
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
    prompt = {"S": {"class_type": "SonderSaveVideo", "inputs": {"project": ["C", 0]}}}

    io_nodes.SonderSaveVideo().save_video(project, torch.zeros(1, 2, 2, 3), embed_metadata=False, prompt=prompt, unique_id="S")

    tracked = project.assets[0].generation_params["editor_export"]["tracked_metadata"]
    assert tracked[0]["display_type"] == "power_loras"
    assert tracked[0]["fields"]["power_loras"][0]["name"] == "a.safetensors"


def test_save_video_take_mode_does_not_leak_sentinel(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    calls = []
    _patch_save_deps(io_nodes, monkeypatch, calls)
    project = _project(tmp_path, with_scene=True)
    project._execution_context = {
        "scene_id": "scene-1",
        "scene_name": "Scene 1",
        "selection_start": 0,
        "selection_end": 1,
        io_nodes.TRACKED_METADATA_CONTEXT_KEY: {"C": [{"label": "Tracked"}]},
    }
    prompt = {"S": {"class_type": "SonderSaveVideo", "inputs": {"project": ["C", 0]}}}

    io_nodes.SonderSaveVideo().save_video(project, torch.zeros(1, 2, 2, 3), mode="Take", embed_metadata=False, prompt=prompt, unique_id="S")

    params = project.assets[0].generation_params
    assert io_nodes.TRACKED_METADATA_CONTEXT_KEY not in params
    assert params["editor_export"]["tracked_metadata"] == [{"label": "Tracked"}]
    assert io_nodes.TRACKED_METADATA_CONTEXT_KEY not in project.scenes[0].clips[0].generation_params


def test_save_bridge_does_not_cache_prompt_workflow(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    _clear_bridge_state(io_nodes)
    project = _project(tmp_path)
    project._execution_context = {
        "scene_id": "scene-1",
        "scene_name": "Scene 1",
        "selection_start": 4,
        "selection_end": 8,
        "prompt": "editor prompt should not become bridge asset prompt",
        io_nodes.TRACKED_METADATA_CONTEXT_KEY: {"C": [{"label": "Bridge Tracked"}]},
    }
    prompt = {"bridge-1": {"class_type": "SonderSaveBridge", "inputs": {"project": ["C", 0]}}}

    io_nodes.SonderSaveBridge().prepare_output(project, prompt=prompt, unique_id="bridge-1")
    entry = next(value for key, value in io_nodes._BRIDGE_REGISTRY.items() if key[1] == "bridge-1")
    params = entry["generation_params"]
    editor_export = params["editor_export"]

    assert set(params) == {"scene_id", "scene_name", "editor_export"}
    assert params["scene_id"] == "scene-1"
    assert params["scene_name"] == "Scene 1"
    assert editor_export["tracked_metadata"] == [{"label": "Bridge Tracked"}]
    assert "prompt" not in editor_export
    assert "workflow" not in editor_export
    assert entry["prompt_text"] == ""


def test_png_sequence_only_first_asset_advertises_embedded_workflow(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    Image = importlib.import_module("PIL.Image")
    calls = []
    _patch_save_deps(io_nodes, monkeypatch, calls)
    project = _project(tmp_path)

    io_nodes.SonderSaveVideo().save_video(
        project,
        torch.zeros(3, 2, 2, 3),
        save_preset=io_nodes.CUSTOM_SAVE_VIDEO_PRESET,
        custom_output_kind=io_nodes.CUSTOM_OUTPUT_KIND_PNG_SEQUENCE,
        prompt={"1": {"class_type": "Node", "inputs": {}}},
        extra_pnginfo={"workflow": {"nodes": [{"id": 1, "type": "Node"}]}},
    )

    assert len(project.assets) == 3
    flags = [
        asset.generation_params["editor_export"]["has_embedded_workflow"]
        for asset in project.assets
    ]
    assert flags == [True, False, False]
    first_path = Path(project.project_dir) / project.assets[0].path
    later_path = Path(project.project_dir) / project.assets[1].path
    with Image.open(first_path) as image:
        assert "workflow" in image.info
    with Image.open(later_path) as image:
        assert "workflow" not in image.info


def test_png_sequence_runs_prediction_gated_frame_check_before_return(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    calls = []
    _patch_save_deps(io_nodes, monkeypatch, calls)
    project = _project(tmp_path)
    project._execution_context = {
        "guide_injection": {
            "predicted_unresolved": True,
            "frame_count": 1,
            "frame_constraint": {"step": 8, "offset": 1},
            "max_excess_latents": 1,
            "entries": [],
        }
    }
    observed = []
    monkeypatch.setattr(
        io_nodes,
        "_record_guide_bleed_if_needed",
        lambda check, actual, path: observed.append((check, actual, path)) or False,
    )

    io_nodes.SonderSaveVideo().save_video(
        project,
        torch.zeros(1, 2, 2, 3),
        save_preset=io_nodes.CUSTOM_SAVE_VIDEO_PRESET,
        custom_output_kind=io_nodes.CUSTOM_OUTPUT_KIND_PNG_SEQUENCE,
    )

    assert observed == [(
        {
            "armed": True,
            "expected_frame_count": 1,
            "step": 8,
            "max_excess_latents": 1,
            "guide_ids": [],
            "project_id": project.project_id,
        },
        1,
        "save_video_tensor",
    )]
