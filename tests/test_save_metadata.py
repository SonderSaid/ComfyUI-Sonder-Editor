import importlib
import json
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


def test_save_video_preserves_collector_copy_span_without_full_prompt_duplicate(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    collector = importlib.import_module(f"{TEST_PACKAGE}.nodes.metadata_collector")
    torch = importlib.import_module("torch")
    calls = []
    _patch_save_deps(io_nodes, monkeypatch, calls)
    project = _project(tmp_path)
    full_prompt = "P" * 3000
    section = collector._section_from_origin(
        "10", {"class_type": "Prompt Node", "inputs": {"prompt": full_prompt}}, None, "Prompt Node")
    project._execution_context = {io_nodes.TRACKED_METADATA_CONTEXT_KEY: {"C": [section]}}
    prompt = {"S": {"class_type": "SonderSaveVideo", "inputs": {"project": ["C", 0]}}}

    io_nodes.SonderSaveVideo().save_video(
        project, torch.zeros(1, 2, 2, 3), embed_metadata=False, prompt=prompt, unique_id="S")

    saved = project.assets[0].generation_params["editor_export"]["tracked_metadata"][0]
    start, end = saved["raw_field_spans"]["prompt"]
    assert saved["raw_widget_text"][start:end] == full_prompt
    assert "full_fields" not in saved


def test_save_video_persists_a_collected_subgraph_section(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    collector = importlib.import_module(f"{TEST_PACKAGE}.nodes.metadata_collector")
    torch = importlib.import_module("torch")
    _patch_save_deps(io_nodes, monkeypatch, [])
    project = _project(tmp_path)
    fixture = json.loads((ROOT / "tests" / "fixtures" / "subgraph_qwen_edit_2509.json").read_text(encoding="utf-8"))
    collector.collect_metadata(
        project, prompt=fixture["prompt"], extra_pnginfo={"workflow": fixture["workflow"]},
        unique_id=fixture["collector"], values={"value_0": object()}, labels={}, capacity=12)
    collected = project._execution_context[collector.TRACKED_METADATA_CONTEXT_KEY][fixture["collector"]]
    prompt = {"S": {"class_type": "SonderSaveVideo", "inputs": {"project": [fixture["collector"], 0]}}}

    io_nodes.SonderSaveVideo().save_video(
        project, torch.zeros(1, 2, 2, 3), embed_metadata=False, prompt=prompt, unique_id="S")

    saved = project.assets[0].generation_params["editor_export"]["tracked_metadata"]
    assert saved == collected
    assert saved[0]["display_type"] == "subgraph"
    assert saved[0]["subgraph"]["output_names"] == ["IMAGE"]


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


# ── Controls at run (Phase 3C) ───────────────────────────────────────────────

class _FakeSampler:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "seed": ("INT", {"min": 0, "max": 0xFFFFFFFFFFFFFFFF, "control_after_generate": True}),
            "steps": ("INT", {"min": 1, "max": 100}),
        }}


def _controls_prompt(consumer="S", consumer_class="SonderSaveVideo"):
    manifest = {"v": 1, "bindings": {
        "seedB": {"label": "Seed", "status": "ok", "seed_mode": "increment", "targets": [{"path": "K", "input": "seed"}]},
        "stepsB": {"label": "Steps", "status": "ok", "targets": [{"path": "K", "input": "steps"}]},
    }}
    return {
        "C": {"class_type": "SonderMetadataCollector", "inputs": {}},
        "K": {"class_type": "FakeSampler", "inputs": {"seed": 2 ** 60 + 1, "steps": 12}},
        "D": {"class_type": "VAEDecode", "inputs": {"samples": ["K", 0]}},
        consumer: {"class_type": consumer_class, "inputs": {"project": ["C", 0], "images": ["D", 0]}},
        "R": {"class_type": "SonderControlRelay", "inputs": {"manifest": json.dumps(manifest)}},
    }


def _fake_classes(monkeypatch):
    descriptors = importlib.import_module(f"{TEST_PACKAGE}.server.control_descriptors")
    monkeypatch.setattr(descriptors, "_comfy_class_lookup", {"FakeSampler": _FakeSampler}.get)


def _controls_section(tracked):
    sections = [section for section in tracked if section.get("display_type") == "controls"]
    assert len(sections) == 1
    return sections[0]


def test_save_video_records_controls_at_run(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    _fake_classes(monkeypatch)
    torch = importlib.import_module("torch")
    _patch_save_deps(io_nodes, monkeypatch, [])
    project = _project(tmp_path)
    context = {io_nodes.TRACKED_METADATA_CONTEXT_KEY: {"C": [
        {"label": "Sampler", "source_node_id": "K", "display_type": None, "fields": {"steps": 12}}]}}
    project._execution_context = context
    before = json.dumps(context, sort_keys=True)

    io_nodes.SonderSaveVideo().save_video(project, torch.zeros(1, 2, 2, 3), embed_metadata=False,
                                         prompt=_controls_prompt(), unique_id="S")

    tracked = project.assets[0].generation_params["editor_export"]["tracked_metadata"]
    assert tracked[0]["label"] == "Sampler"
    section = _controls_section(tracked)
    assert section["fields"] == {"Seed": str(2 ** 60 + 1), "Steps": "12"}
    assert section["supplied"] == [{"section_index": 0, "field": "steps", "binding_id": "stepsB"}]
    # Nothing new enters the execution context or its public copy.
    assert json.dumps(context, sort_keys=True) == before
    assert "controls" not in json.dumps(io_nodes._public_execution_context(context))


def test_save_video_without_a_relay_records_no_controls(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    _fake_classes(monkeypatch)
    torch = importlib.import_module("torch")
    _patch_save_deps(io_nodes, monkeypatch, [])
    project = _project(tmp_path)
    prompt = _controls_prompt()
    del prompt["R"]

    io_nodes.SonderSaveVideo().save_video(project, torch.zeros(1, 2, 2, 3), embed_metadata=False,
                                         prompt=prompt, unique_id="S")

    assert project.assets[0].generation_params["editor_export"]["tracked_metadata"] == []


def test_a_failing_snapshot_never_fails_the_save(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    _patch_save_deps(io_nodes, monkeypatch, [])

    def boom(*_args, **_kwargs):
        raise RuntimeError("describe failed")

    monkeypatch.setattr(io_nodes, "controls_capture", boom)
    project = _project(tmp_path)
    io_nodes.SonderSaveVideo().save_video(project, torch.zeros(1, 2, 2, 3), embed_metadata=False,
                                         prompt=_controls_prompt(), unique_id="S")
    assert project.assets[0].generation_params["editor_export"]["tracked_metadata"] == []


def test_take_copies_and_png_sequence_carry_controls(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    _fake_classes(monkeypatch)
    torch = importlib.import_module("torch")
    _patch_save_deps(io_nodes, monkeypatch, [])
    project = _project(tmp_path, with_scene=True)
    project._execution_context = {"scene_id": "scene-1", "scene_name": "Scene 1", "selection_start": 0, "selection_end": 1}

    io_nodes.SonderSaveVideo().save_video(project, torch.zeros(1, 2, 2, 3), mode="Take", embed_metadata=False,
                                         prompt=_controls_prompt(), unique_id="S")

    clip = project.scenes[0].clips[0]
    for params in (project.assets[0].generation_params, clip.generation_params, clip.take_metadata):
        assert _controls_section(params["editor_export"]["tracked_metadata"])["fields"]["Steps"] == "12"

    sequence = _project(tmp_path / "seq")
    io_nodes.SonderSaveVideo().save_video(
        sequence, torch.zeros(2, 2, 2, 3), embed_metadata=False,
        save_preset=io_nodes.CUSTOM_SAVE_VIDEO_PRESET, custom_output_kind=io_nodes.CUSTOM_OUTPUT_KIND_PNG_SEQUENCE,
        prompt=_controls_prompt(), unique_id="S")
    assert len(sequence.assets) == 2
    for asset in sequence.assets:
        assert _controls_section(asset.generation_params["editor_export"]["tracked_metadata"])["fields"]["Seed"]


class _FakeTextNode:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"text": ("STRING", {"multiline": True})}}


LYRICS = ("[Verse 1]\r\nThree in the morning, rain on the pavement\r\n차가운 밤이 날 삼켜 👩‍👩‍👧\n" * 40)


def _long_text_prompt(consumer="S", consumer_class="SonderSaveVideo", categories=None):
    """``_controls_prompt`` plus a long STRING Control upstream of the save, and
    optionally Mode categories in the manifest."""
    prompt = _controls_prompt(consumer, consumer_class)
    manifest = json.loads(prompt["R"]["inputs"]["manifest"])
    manifest["bindings"]["lyricsB"] = {"label": "Lyrics", "status": "ok", "targets": [{"path": "T", "input": "text"}]}
    if categories is not None:
        manifest["presets"] = {"pa": {"name": "A", "order": 0, "members": {}}}
        manifest["preset_categories"] = categories
    prompt["R"]["inputs"]["manifest"] = json.dumps(manifest)
    prompt["T"] = {"class_type": "FakeText", "inputs": {"text": LYRICS}}
    prompt["K"]["inputs"]["positive"] = ["T", 0]
    return prompt


def _fake_text_classes(monkeypatch):
    descriptors = importlib.import_module(f"{TEST_PACKAGE}.server.control_descriptors")
    monkeypatch.setattr(descriptors, "_comfy_class_lookup", {"FakeSampler": _FakeSampler, "FakeText": _FakeTextNode}.get)


MODES_ROWS = [{"id": "t", "name": "Task", "status": "selected", "choice": "Edit With Words", "matches": ["Edit With Words"]}]


def _types(params):
    return [s.get("display_type") for s in params["editor_export"]["tracked_metadata"]]


def test_a_take_keeps_full_text_and_modes_on_the_asset_only(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    _fake_text_classes(monkeypatch)
    torch = importlib.import_module("torch")
    _patch_save_deps(io_nodes, monkeypatch, [])
    run_feedback = importlib.import_module(f"{TEST_PACKAGE}.server.run_feedback")
    monkeypatch.setattr(run_feedback, "modes_for_save", lambda prompt, timeout=2.0: [dict(r) for r in MODES_ROWS])
    seen = {}
    real_payload = io_nodes._file_metadata_payload
    monkeypatch.setattr(io_nodes, "_file_metadata_payload",
                        lambda prompt, workflow, export: seen.setdefault("payload", real_payload(prompt, workflow, export)))
    project = _project(tmp_path, with_scene=True)
    project._execution_context = {"scene_id": "scene-1", "scene_name": "Scene 1", "selection_start": 0, "selection_end": 1}

    io_nodes.SonderSaveVideo().save_video(project, torch.zeros(1, 2, 2, 3), mode="Take", embed_metadata=True,
                                         prompt=_long_text_prompt(), unique_id="S", extra_pnginfo={"workflow": {}})

    asset = project.assets[0].generation_params
    assert asset["controls_text"] == {"v": 1, "values": {"lyricsB": LYRICS}}
    assert _types(asset)[-2:] == ["controls", "modes"]
    modes = asset["editor_export"]["tracked_metadata"][-1]
    assert modes["fields"] == {"Task": "Edit With Words"}
    clip = project.scenes[0].clips[0]
    for params in (clip.generation_params, clip.take_metadata):
        assert "controls_text" not in params and "modes" not in _types(params)
        assert _controls_section(params["editor_export"]["tracked_metadata"])["fields"]["Lyrics"].endswith("source prompt]")
    # The asset kept both after its clip copies were built.
    assert "controls_text" in project.assets[0].generation_params and "modes" in _types(project.assets[0].generation_params)
    # The file's embedded metadata has neither (it was built before the encode).
    export = json.loads(seen["payload"]["editor_export"])
    assert "controls_text" not in export and "modes" not in [s.get("display_type") for s in export["tracked_metadata"]]


def test_a_png_sequence_keeps_full_text_and_modes_on_frame_one(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    _fake_text_classes(monkeypatch)
    torch = importlib.import_module("torch")
    _patch_save_deps(io_nodes, monkeypatch, [])
    run_feedback = importlib.import_module(f"{TEST_PACKAGE}.server.run_feedback")
    monkeypatch.setattr(run_feedback, "modes_for_save", lambda prompt, timeout=2.0: [dict(r) for r in MODES_ROWS])
    sequence = _project(tmp_path / "seq")
    io_nodes.SonderSaveVideo().save_video(
        sequence, torch.zeros(3, 2, 2, 3), embed_metadata=False,
        save_preset=io_nodes.CUSTOM_SAVE_VIDEO_PRESET, custom_output_kind=io_nodes.CUSTOM_OUTPUT_KIND_PNG_SEQUENCE,
        prompt=_long_text_prompt(), unique_id="S")
    first, *rest = sorted(sequence.assets, key=lambda a: a.generation_params["sequence_index"])
    assert first.generation_params["controls_text"]["values"]["lyricsB"] == LYRICS
    assert "modes" in _types(first.generation_params)
    for asset in rest:
        assert "controls_text" not in asset.generation_params and "modes" not in _types(asset.generation_params)
        assert "controls" in _types(asset.generation_params)  # previews stay on every frame


def test_modes_not_recorded_when_the_run_has_no_record(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    _fake_text_classes(monkeypatch)
    torch = importlib.import_module("torch")
    _patch_save_deps(io_nodes, monkeypatch, [])
    project = _project(tmp_path)
    project._execution_context = {}
    prompt = _long_text_prompt(categories={"c": {"name": "Task", "order": 0, "choices": ["pa"]}})
    io_nodes.SonderSaveVideo().save_video(project, torch.zeros(1, 2, 2, 3), embed_metadata=False,
                                         prompt=prompt, unique_id="S")
    modes = project.assets[0].generation_params["editor_export"]["tracked_metadata"][-1]
    assert modes["display_type"] == "modes" and modes["status"] == "not_recorded" and modes["fields"] == {}
    plain = _project(tmp_path / "plain")
    plain._execution_context = {}
    io_nodes.SonderSaveVideo().save_video(plain, torch.zeros(1, 2, 2, 3), embed_metadata=False,
                                         prompt=_long_text_prompt(), unique_id="S")
    assert "modes" not in _types(plain.assets[0].generation_params)


def test_a_failing_modes_read_never_fails_the_save(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    _patch_save_deps(io_nodes, monkeypatch, [])
    run_feedback = importlib.import_module(f"{TEST_PACKAGE}.server.run_feedback")

    def boom(*_a, **_k):
        raise RuntimeError("feedback broke")

    monkeypatch.setattr(run_feedback, "modes_for_save", boom)
    project = _project(tmp_path)
    project._execution_context = {}
    io_nodes.SonderSaveVideo().save_video(project, torch.zeros(1, 2, 2, 3), embed_metadata=False,
                                         prompt=_controls_prompt(), unique_id="S")
    assert len(project.assets) == 1


def test_embedded_metadata_survives_a_lone_surrogate_and_keeps_ordinary_bytes(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    media_helpers = importlib.import_module(f"{TEST_PACKAGE}.server.media_helpers")
    bad = {"N": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "before \ud800 after"}}}
    payload = io_nodes._file_metadata_payload(bad, {"nodes": []}, {"tracked_metadata": []})
    for value in payload.values():
        value.encode("utf-8")
    assert json.loads(payload["prompt"]) == bad
    path = media_helpers._write_ffmetadata_file(payload)
    Path(path).unlink()
    ordinary = {"N": {"class_type": "X", "inputs": {"value": "夜明け 👩‍👩‍👧"}}}
    assert io_nodes._file_metadata_payload(ordinary, None, {})["prompt"] == json.dumps(
        ordinary, ensure_ascii=False, separators=(",", ":"))


def test_a_lone_surrogate_anywhere_in_a_run_saves_and_the_project_still_opens(tmp_path, monkeypatch):
    """Embedding on, a lone surrogate in a bound Control, an unconnected node, the
    canvas workflow and a collector field: the take saves, the project is written
    by the real (strict) writer, loads, and serializes for its text routes."""
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    _fake_text_classes(monkeypatch)
    torch = importlib.import_module("torch")
    calls = []
    monkeypatch.setattr(io_nodes, "encode_video", _fake_encode(io_nodes, calls))
    thumbnail_service = importlib.import_module(f"{TEST_PACKAGE}.server.thumbnail_service")
    monkeypatch.setattr(thumbnail_service, "ensure_thumbnail", lambda *args, **kwargs: None)
    project_manager = importlib.import_module(f"{TEST_PACKAGE}.server.project_manager")
    timeline_state = _timeline_state()
    project = project_manager.create_project("Surrogate", base_dir=str(tmp_path / "projects"))
    project.scenes = [timeline_state.Scene(scene_id="scene-1", name="Scene 1", duration_frames=24)]
    project_manager.save_project(project)
    project = project_manager.load_project(project.project_dir)
    project._execution_context = {
        "scene_id": "scene-1", "scene_name": "Scene 1", "selection_start": 0, "selection_end": 1,
        io_nodes.TRACKED_METADATA_CONTEXT_KEY: {"C": [{"label": "Notes", "source_node_id": "K", "display_type": None,
                                                       "fields": {"note": "x\ud800y"}, "raw_widget_text": "note: x\ud800y"}]}}
    prompt = _long_text_prompt()
    prompt["T"]["inputs"]["text"] = "lyric \ud800 " + "z" * 600
    prompt["N"] = {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "before \ud800 after"}}
    workflow = {"nodes": [{"id": 1, "widgets_values": ["before \ud800 after"]}]}
    seen = {}
    real_payload = io_nodes._file_metadata_payload
    monkeypatch.setattr(io_nodes, "_file_metadata_payload",
                        lambda p, w, e: seen.setdefault("payload", real_payload(p, w, e)))

    io_nodes.SonderSaveVideo().save_video(project, torch.zeros(1, 2, 2, 3), mode="Take", embed_metadata=True,
                                         prompt=prompt, unique_id="S", extra_pnginfo={"workflow": workflow})

    reloaded = project_manager.load_project(project.project_dir)
    text = json.dumps(reloaded.to_dict(), ensure_ascii=False)  # what GET /project/{id} serializes
    text.encode("utf-8")
    assert "�" in json.dumps(reloaded.scenes[0].clips[0].generation_params, ensure_ascii=False)
    asset = reloaded.assets[0]
    storage = importlib.import_module(f"{TEST_PACKAGE}.server.project_storage")
    storage.hydrate_asset(asset)
    params = asset.generation_params
    json.dumps(params, ensure_ascii=False).encode("utf-8")
    assert params["controls_text"] == {"v": 1, "values": {}, "omitted": 1}
    assert params["editor_export"]["workflow_sha256"]
    # The embedded metadata kept the exact text, escaped only where it had to be.
    for value in seen["payload"].values():
        value.encode("utf-8")
    assert json.loads(seen["payload"]["prompt"])["N"]["inputs"]["value"] == "before \ud800 after"


def test_the_workflow_digest_survives_a_lone_surrogate_and_is_unchanged_otherwise(tmp_path, monkeypatch):
    import hashlib
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    normal = {"nodes": [{"widgets_values": ["夜明け 👩‍👩‍👧"]}]}
    expected = hashlib.sha256(json.dumps(normal, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    assert io_nodes._workflow_digest(normal) == expected
    assert len(io_nodes._workflow_digest({"nodes": [{"widgets_values": ["a\ud800b"]}]})) == 64


def test_a_bridge_keeps_full_text_on_its_first_file_and_scrubs_every_file(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    params = {"controls_text": {"v": 1, "values": {"b": "x" * 600}},
              "editor_export": {"tracked_metadata": [{"display_type": "controls", "fields": {"a": "b\ud800"}}]}}
    modes = {"label": "Modes at run", "display_type": "modes", "fields": {"T": "E"}, "modes": []}
    first = io_nodes._bridge_asset_params(params, modes, keep_full_text=True)
    later = io_nodes._bridge_asset_params(params, modes, keep_full_text=False)
    assert first["controls_text"] == params["controls_text"] and "controls_text" not in later
    for out in (first, later):
        assert [s["display_type"] for s in out["editor_export"]["tracked_metadata"]] == ["controls", "modes"]
        assert out["editor_export"]["tracked_metadata"][0]["fields"]["a"] == "b�"
    assert params["editor_export"]["tracked_metadata"][0]["fields"]["a"] == "b\ud800"  # input untouched


def test_modes_are_awaited_only_when_the_workflow_declares_categories(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    run_feedback = importlib.import_module(f"{TEST_PACKAGE}.server.run_feedback")
    timeouts = []
    monkeypatch.setattr(run_feedback, "modes_for_save", lambda prompt, timeout=2.0: timeouts.append(timeout))
    io_nodes._modes_section_for_save(_long_text_prompt())
    io_nodes._modes_section_for_save(_long_text_prompt(categories={"c": {"name": "T", "order": 0, "choices": ["pa"]}}))
    assert timeouts == [0, run_feedback.MODES_SAVE_WAIT_SEC]


def test_save_bridge_reads_modes_at_finalization(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    run_feedback = importlib.import_module(f"{TEST_PACKAGE}.server.run_feedback")
    monkeypatch.setattr(run_feedback, "prompt_id_for", lambda prompt: "pid-1")
    monkeypatch.setattr(run_feedback, "modes_of", lambda pid: [dict(r) for r in MODES_ROWS] if pid == "pid-1" else None)
    prompt = _long_text_prompt(categories={"c": {"name": "Task", "order": 0, "choices": ["pa"]}})
    run = io_nodes._bridge_modes_run(prompt)
    assert run == {"prompt_id": "pid-1", "declared": True, "rows": MODES_ROWS}
    section = io_nodes._bridge_modes_section({"modes_run": run})
    assert section["fields"] == {"Task": "Edit With Words"}
    # Pruned by a late finalization: the record noted at prepare is the fallback.
    monkeypatch.setattr(run_feedback, "modes_of", lambda pid: None)
    assert io_nodes._bridge_modes_section({"modes_run": run})["fields"] == {"Task": "Edit With Words"}
    assert io_nodes._bridge_modes_section({"modes_run": {**run, "rows": None}})["status"] == "not_recorded"
    assert io_nodes._bridge_modes_section({}) is None  # an orphan entry
    export = {"tracked_metadata": [{"display_type": "controls"}]}
    out = io_nodes._with_modes_at_run(export, section)
    assert [s["display_type"] for s in out["tracked_metadata"]] == ["controls", "modes"]
    assert len(export["tracked_metadata"]) == 1


def test_save_bridge_keeps_full_text_on_its_params(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    _fake_text_classes(monkeypatch)
    _clear_bridge_state(io_nodes)
    project = _project(tmp_path)
    project._execution_context = {"scene_id": "scene-1", "scene_name": "Scene 1"}
    prompt = _long_text_prompt(consumer="B", consumer_class="SonderSaveBridge")
    prompt["B"]["inputs"] = {"project": ["C", 0]}
    prompt["I"] = {"class_type": "SaveImage", "inputs": {"images": ["D", 0], "filename_prefix": ["B", 1]}}
    io_nodes.SonderSaveBridge().prepare_output(project, prompt=prompt, unique_id="B")
    entry = next(value for key, value in io_nodes._BRIDGE_REGISTRY.items() if key[1] == "B")
    assert entry["generation_params"]["controls_text"]["values"]["lyricsB"] == LYRICS
    assert "controls_text" not in json.dumps(entry["generation_params"]["editor_export"])
    _clear_bridge_state(io_nodes)


def test_save_bridge_controls_follow_its_consumers(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    _fake_classes(monkeypatch)
    _clear_bridge_state(io_nodes)
    project = _project(tmp_path)
    project._execution_context = {"scene_id": "scene-1", "scene_name": "Scene 1"}
    prompt = _controls_prompt(consumer="B", consumer_class="SonderSaveBridge")
    # The generation branch reaches a native save wired from the Bridge, not the Bridge.
    prompt["B"]["inputs"] = {"project": ["C", 0]}
    prompt["I"] = {"class_type": "SaveImage", "inputs": {"images": ["D", 0], "filename_prefix": ["B", 1]}}

    io_nodes.SonderSaveBridge().prepare_output(project, prompt=prompt, unique_id="B")
    entry = next(value for key, value in io_nodes._BRIDGE_REGISTRY.items() if key[1] == "B")
    section = _controls_section(entry["generation_params"]["editor_export"]["tracked_metadata"])
    assert section["fields"] == {"Seed": str(2 ** 60 + 1), "Steps": "12"}
    _clear_bridge_state(io_nodes)



def test_native_controls_rows_flow_through_video_save_without_collector(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module('torch')
    _patch_save_deps(io_nodes, monkeypatch, [])
    project = _project(tmp_path)
    project._execution_context = {}
    prompt = {'L': {'class_type':'LoadLoraModel','inputs':{'loras.0.lora_name':'probe.safetensors',
              'loras.0.strength':0.75,'loras.0.enabled':True}},
              'S': {'class_type':'SonderSaveVideo','inputs':{'frames':['L',0]}},
              'R': {'class_type':'SonderControlRelay','inputs':{'manifest':json.dumps({'v':1,'bindings':{}})}}}
    descriptor={'binding_id':'stack','status':'ok','kind':'ROWS','label':'Stack','group':'','order':0,
                'targets':[{'path':'L','input':'loras'}],
                'value':[{'lora_name':'probe.safetensors','strength':0.75,'enabled':True}]}
    original = io_nodes.controls_capture
    monkeypatch.setattr(io_nodes,'controls_capture',lambda p,c,**kw:original(p,c,**kw,
                        describe=lambda *a,**k:{'bindings':[descriptor]}))
    io_nodes.SonderSaveVideo().save_video(project,torch.zeros(2,2,2,3),embed_metadata=False,prompt=prompt,unique_id='S')
    tracked=project.assets[0].generation_params['editor_export']['tracked_metadata']
    assert len(tracked)==1
    section=_controls_section(tracked)
    assert section['fields']['Stack'][0]['name']=='probe.safetensors'
    assert section['controls'][0]['display_type']=='native_lora_stack'
