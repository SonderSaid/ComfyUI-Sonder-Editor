import importlib
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "video_editor_testpkg"


def _ensure_package():
    if TEST_PACKAGE not in sys.modules:
        pkg = types.ModuleType(TEST_PACKAGE)
        pkg.__path__ = [str(ROOT)]
        sys.modules[TEST_PACKAGE] = pkg
    importlib.invalidate_caches()


def _import_collector():
    _ensure_package()
    return importlib.import_module(f"{TEST_PACKAGE}.nodes.metadata_collector")


def _import_timeline_state():
    _ensure_package()
    return importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")


def _project(tmp_path):
    timeline_state = _import_timeline_state()
    return timeline_state.TimelineProject(project_dir=str(tmp_path), name="Collector Test")


def _workflow(upstream=None, collector=None):
    return {
        "nodes": [
            upstream or {
                "id": 10,
                "type": "TestNode",
                "outputs": [{"name": "out", "links": [1]}],
            },
            collector or {
                "id": 20,
                "type": "SonderMetadataCollector",
                "inputs": [{"name": "project", "link": 2}, {"name": "value_0", "link": 1}],
            },
        ]
    }


def _prompt(inputs=None, class_type="TestNode", node_id="10"):
    return {node_id: {"class_type": class_type, "inputs": inputs or {"strength": 1.0}}}


def test_collector_single_input_default_label(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    result = module.SonderMetadataCollector().collect(
        project,
        prompt=_prompt(class_type="Sampler"),
        extra_pnginfo={"workflow": _workflow()},
        unique_id="20",
        value_0="connected",
    )

    assert result[0] is project
    section = project._execution_context[module.TRACKED_METADATA_CONTEXT_KEY][0]
    assert section["label"] == "Sampler"
    assert section["source_node_id"] == "10"
    assert section["fields"] == {"strength": 1.0}


def test_collector_user_label_overrides(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    module.SonderMetadataCollector().collect(
        project,
        prompt=_prompt(),
        extra_pnginfo={"workflow": _workflow()},
        unique_id="20",
        value_0="connected",
        label_0="My LoRAs",
    )

    section = project._execution_context[module.TRACKED_METADATA_CONTEXT_KEY][0]
    assert section["label"] == "My LoRAs"


def test_collector_fallback_to_title(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    upstream = {"id": 10, "type": "TestNode", "title": "Power LoRAs", "outputs": [{"links": [1]}]}
    module.SonderMetadataCollector().collect(
        project,
        prompt=_prompt(class_type="Power Lora Loader"),
        extra_pnginfo={"workflow": _workflow(upstream=upstream)},
        unique_id="20",
        value_0="connected",
    )

    section = project._execution_context[module.TRACKED_METADATA_CONTEXT_KEY][0]
    assert section["label"] == "Power LoRAs"
    assert section["source_node_title"] == "Power LoRAs"


def test_collector_power_lora_loader_adds_readable_summary(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    module.SonderMetadataCollector().collect(
        project,
        prompt=_prompt(
            {
                "lora_1": "detail_boost.safetensors",
                "strength_1": 0.8,
                "on_1": True,
                "lora_2": "disabled_style.safetensors",
                "strength_model_2": 0.45,
                "strength_clip_2": 0.25,
                "on_2": False,
            },
            class_type="Power Lora Loader (rgthree)",
        ),
        extra_pnginfo={"workflow": _workflow()},
        unique_id="20",
        value_0="connected",
    )

    section = project._execution_context[module.TRACKED_METADATA_CONTEXT_KEY][0]
    assert section["fields"] == {
        "power_loras": [
            {"slot": 1, "name": "detail_boost.safetensors", "enabled": True, "strength": 0.8},
            {
                "slot": 2,
                "name": "disabled_style.safetensors",
                "enabled": False,
                "model_strength": 0.45,
                "clip_strength": 0.25,
            },
        ],
        "enabled_lora_count": 1,
        "total_lora_count": 2,
    }


def test_collector_power_lora_loader_supports_nested_slot_objects(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    module.SonderMetadataCollector().collect(
        project,
        prompt=_prompt(
            {
                "PowerLoraLoaderHeaderWidget": {"type": "PowerLoraLoader"},
                "lora_1": {"on": True, "lora": "flux_detail.safetensors", "strength": 0.7},
                "lora_2": {"on": False, "lora": "flux_style.safetensors", "strength_model": 0.4, "strength_clip": 0.2},
                "Add Lora": "",
                "model": ["100", 0],
            },
            class_type="Power Lora Loader (rgthree)",
        ),
        extra_pnginfo={"workflow": _workflow()},
        unique_id="20",
        value_0="connected",
    )

    section = project._execution_context[module.TRACKED_METADATA_CONTEXT_KEY][0]
    assert section["fields"] == {
        "power_loras": [
            {"slot": 1, "name": "flux_detail.safetensors", "enabled": True, "strength": 0.7},
            {
                "slot": 2,
                "name": "flux_style.safetensors",
                "enabled": False,
                "model_strength": 0.4,
                "clip_strength": 0.2,
            },
        ],
        "enabled_lora_count": 1,
        "total_lora_count": 2,
    }
    assert "lora_1" not in section["fields"]
    assert "model" not in section["fields"]


def test_collector_multiple_inputs_preserve_order(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    workflow = {
        "nodes": [
            {"id": 10, "type": "A", "outputs": [{"links": [1]}]},
            {"id": 11, "type": "B", "outputs": [{"links": [2]}]},
            {"id": 12, "type": "C", "outputs": [{"links": [3]}]},
            {"id": 20, "type": "SonderMetadataCollector", "inputs": [
                {"name": "value_0", "link": 1},
                {"name": "value_1", "link": 2},
                {"name": "value_2", "link": 3},
            ]},
        ]
    }
    prompt = {
        "10": {"class_type": "A", "inputs": {"a": 1}},
        "11": {"class_type": "B", "inputs": {"b": 2}},
        "12": {"class_type": "C", "inputs": {"c": 3}},
    }
    module.SonderMetadataCollector().collect(
        project,
        prompt=prompt,
        extra_pnginfo={"workflow": workflow},
        unique_id="20",
        value_0="a",
        value_1="b",
        value_2="c",
    )

    sections = project._execution_context[module.TRACKED_METADATA_CONTEXT_KEY]
    assert [section["source_node_id"] for section in sections] == ["10", "11", "12"]


def test_collector_subgraph_prompt_key(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    workflow = {
        "nodes": [{"id": 100, "type": "subgraph-1"}],
        "definitions": {
            "subgraphs": [{
                "id": "subgraph-1",
                "nodes": [
                    {"id": 10, "type": "InnerNode", "outputs": [{"links": [5]}]},
                    {"id": 20, "type": "SonderMetadataCollector", "inputs": [{"name": "value_0", "link": 5}]},
                ],
            }]
        },
    }
    module.SonderMetadataCollector().collect(
        project,
        prompt={"100:10": {"class_type": "InnerNode", "inputs": {"inner": True}}},
        extra_pnginfo={"workflow": workflow},
        unique_id="100:20",
        value_0="connected",
    )

    section = project._execution_context[module.TRACKED_METADATA_CONTEXT_KEY][0]
    assert section["source_node_id"] == "100:10"
    assert section["fields"] == {"inner": True}


def test_collector_chained_appends(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    collector = module.SonderMetadataCollector()
    collector.collect(project, prompt=_prompt({"a": 1}), extra_pnginfo={"workflow": _workflow()}, unique_id="20", value_0="x")
    collector.collect(project, prompt=_prompt({"b": 2}), extra_pnginfo={"workflow": _workflow()}, unique_id="20", value_0="x")

    sections = project._execution_context[module.TRACKED_METADATA_CONTEXT_KEY]
    assert [section["fields"] for section in sections] == [{"a": 1}, {"b": 2}]


def test_collector_raw_widget_text_format(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    module.SonderMetadataCollector().collect(
        project,
        prompt=_prompt({"seed": 123, "cfg": 7}),
        extra_pnginfo={"workflow": _workflow()},
        unique_id="20",
        value_0="connected",
    )

    section = project._execution_context[module.TRACKED_METADATA_CONTEXT_KEY][0]
    assert section["raw_widget_text"] == "seed: 123, cfg: 7"


def test_collector_field_size_cap(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    long_value = "x" * 3000
    module.SonderMetadataCollector().collect(
        project,
        prompt=_prompt({"prompt": long_value}),
        extra_pnginfo={"workflow": _workflow()},
        unique_id="20",
        value_0="connected",
    )

    section = project._execution_context[module.TRACKED_METADATA_CONTEXT_KEY][0]
    assert module.TRUNCATED_MARKER in section["fields"]["prompt"]
    assert long_value in section["raw_widget_text"]


def test_collector_missing_upstream(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    module.SonderMetadataCollector().collect(
        project,
        prompt={},
        extra_pnginfo={"workflow": _workflow()},
        unique_id="20",
        value_0="connected",
    )

    assert project._execution_context[module.TRACKED_METADATA_CONTEXT_KEY] == []


def test_collector_passes_project_through_without_persisted_mutation(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    before = project.to_dict()
    result = module.SonderMetadataCollector().collect(
        project,
        prompt=_prompt(),
        extra_pnginfo={"workflow": _workflow()},
        unique_id="20",
        value_0="connected",
    )

    assert result[0] is project
    assert project.to_dict() == before
