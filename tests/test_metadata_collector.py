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


# --- Fixtures -------------------------------------------------------------------------
# The collector now resolves its OWN inputs from the executed prompt (ComfyUI has already
# collapsed Set/Get/Reroute indirection there), so every prompt fixture must include the
# collector node keyed by its unique_id with resolved `value_N`/`project` links.

def _collector_prompt(*, collector_id="20", project=None, values=None, upstreams=None):
    """Build a prompt containing the upstream origin node(s) AND the collector entry."""
    prompt = {}
    for origin_id, spec in (upstreams or {}).items():
        prompt[origin_id] = spec
    collector_inputs = {}
    if project is not None:
        collector_inputs["project"] = project
    for index, link in (values or {}).items():
        collector_inputs[f"value_{index}"] = link
    prompt[collector_id] = {"class_type": "SonderMetadataCollector", "inputs": collector_inputs}
    return prompt


def _origin(class_type="TestNode", inputs=None):
    return {"class_type": class_type, "inputs": inputs if inputs is not None else {"strength": 1.0}}


def _run(module, project, prompt, *, unique_id="20", workflow=None, **kwargs):
    return module.SonderMetadataCollector().collect(
        project,
        prompt=prompt,
        extra_pnginfo={"workflow": workflow or {}},
        unique_id=unique_id,
        **kwargs,
    )


def _chain(project, module, owner="20"):
    return project._execution_context[module.TRACKED_METADATA_CONTEXT_KEY][owner]


# --- Single-input resolution ----------------------------------------------------------

def test_collector_single_input_default_label(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    prompt = _collector_prompt(values={0: ["10", 0]}, upstreams={"10": _origin("Sampler")})

    result = _run(module, project, prompt, value_0="connected")

    assert result[0] is project
    section = _chain(project, module)[0]
    assert section["label"] == "Sampler"
    assert section["source_node_id"] == "10"
    assert section["fields"] == {"strength": 1.0}


def test_collector_user_label_overrides(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    prompt = _collector_prompt(values={0: ["10", 0]}, upstreams={"10": _origin()})

    _run(module, project, prompt, value_0="connected", label_0="My LoRAs")

    assert _chain(project, module)[0]["label"] == "My LoRAs"


def test_collector_fallback_to_title(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    prompt = _collector_prompt(values={0: ["10", 0]}, upstreams={"10": _origin("Power Lora Loader")})
    # Title is recovered from the ORIGIN node in the workflow, by prompt-resolved id.
    workflow = {"nodes": [{"id": 10, "type": "Power Lora Loader", "title": "Power LoRAs"}]}

    _run(module, project, prompt, workflow=workflow, value_0="connected")

    section = _chain(project, module)[0]
    assert section["label"] == "Power LoRAs"
    assert section["source_node_title"] == "Power LoRAs"


def test_collector_power_lora_loader_adds_readable_summary(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    prompt = _collector_prompt(
        values={0: ["10", 0]},
        upstreams={"10": _origin("Power Lora Loader (rgthree)", {
            "lora_1": "detail_boost.safetensors",
            "strength_1": 0.8,
            "on_1": True,
            "lora_2": "disabled_style.safetensors",
            "strength_model_2": 0.45,
            "strength_clip_2": 0.25,
            "on_2": False,
        })},
    )

    _run(module, project, prompt, value_0="connected")

    section = _chain(project, module)[0]
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
    prompt = _collector_prompt(
        values={0: ["10", 0]},
        upstreams={"10": _origin("Power Lora Loader (rgthree)", {
            "PowerLoraLoaderHeaderWidget": {"type": "PowerLoraLoader"},
            "lora_1": {"on": True, "lora": "flux_detail.safetensors", "strength": 0.7},
            "lora_2": {"on": False, "lora": "flux_style.safetensors", "strength_model": 0.4, "strength_clip": 0.2},
            "Add Lora": "",
            "model": ["100", 0],
        })},
    )

    _run(module, project, prompt, value_0="connected")

    section = _chain(project, module)[0]
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
    prompt = _collector_prompt(
        values={0: ["10", 0], 1: ["11", 0], 2: ["12", 0]},
        upstreams={
            "10": _origin("A", {"a": 1}),
            "11": _origin("B", {"b": 2}),
            "12": _origin("C", {"c": 3}),
        },
    )

    _run(module, project, prompt, value_0="a", value_1="b", value_2="c")

    sections = _chain(project, module)
    assert [section["source_node_id"] for section in sections] == ["10", "11", "12"]


def test_collector_subgraph_prompt_key(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    # Subgraph node ids are namespaced parent:child in both unique_id and prompt keys.
    prompt = _collector_prompt(
        collector_id="100:20",
        values={0: ["100:10", 0]},
        upstreams={"100:10": _origin("InnerNode", {"inner": True})},
    )

    _run(module, project, prompt, unique_id="100:20", value_0="connected")

    section = _chain(project, module, owner="100:20")[0]
    assert section["source_node_id"] == "100:10"
    assert section["fields"] == {"inner": True}


def test_collector_reruns_are_idempotent(tmp_path):
    # Re-running the same collector (e.g. across queued prompts on a reused context)
    # overwrites its own chain rather than appending duplicates.
    module = _import_collector()
    project = _project(tmp_path)
    prompt_a = _collector_prompt(values={0: ["10", 0]}, upstreams={"10": _origin("A", {"a": 1})})
    prompt_b = _collector_prompt(values={0: ["10", 0]}, upstreams={"10": _origin("B", {"b": 2})})

    _run(module, project, prompt_a, value_0="x")
    _run(module, project, prompt_b, value_0="x")

    chain = _chain(project, module)
    assert [section["fields"] for section in chain] == [{"b": 2}]


def test_collector_chain_inherits_parent(tmp_path):
    # A downstream collector inherits the upstream collector's chain (parent + own),
    # in editor->leaf order.
    module = _import_collector()
    project = _project(tmp_path)
    prompt = {
        "A": _origin("A", {"a": 1}),
        "464": {"class_type": "SonderMetadataCollector", "inputs": {"value_0": ["A", 0]}},
        "B": _origin("B", {"b": 2}),
        "481": {"class_type": "SonderMetadataCollector", "inputs": {"project": ["464", 0], "value_0": ["B", 0]}},
    }

    _run(module, project, prompt, unique_id="464", value_0="x")
    _run(module, project, prompt, unique_id="481", value_0="y")

    parent_chain = _chain(project, module, owner="464")
    child_chain = _chain(project, module, owner="481")
    assert [s["source_node_id"] for s in parent_chain] == ["A"]
    assert [s["source_node_id"] for s in child_chain] == ["A", "B"]


def test_fan_out_branch_isolation(tmp_path):
    # editor -> 464 -> 481 -> {479, 477}; each save consumer sees ONLY its own branch.
    module = _import_collector()
    project = _project(tmp_path)
    project._execution_context = {}
    prompt = {
        "E": {"class_type": "SonderEditor", "inputs": {}},
        "A": _origin("A", {"a": 1}),
        "B": _origin("B", {"b": 2}),
        "C479": _origin("Ident479", {"x": 479}),
        "C477": _origin("Ident477", {"x": 477}),
        "464": {"class_type": "SonderMetadataCollector", "inputs": {"project": ["E", 0], "value_0": ["A", 0]}},
        "481": {"class_type": "SonderMetadataCollector", "inputs": {"project": ["464", 0], "value_0": ["B", 0]}},
        "479": {"class_type": "SonderMetadataCollector", "inputs": {"project": ["481", 0], "value_0": ["C479", 0]}},
        "477": {"class_type": "SonderMetadataCollector", "inputs": {"project": ["481", 0], "value_0": ["C477", 0]}},
        "SAVE479": {"class_type": "SonderSaveBridge", "inputs": {"project": ["479", 0]}},
        "SAVE477": {"class_type": "SonderSaveBridge", "inputs": {"project": ["477", 0]}},
    }
    for uid in ["464", "481", "479", "477"]:
        _run(module, project, prompt, unique_id=uid, value_0="c")

    chain479 = module.collector_chain_for_consumer(project._execution_context, prompt, "SAVE479")
    chain477 = module.collector_chain_for_consumer(project._execution_context, prompt, "SAVE477")

    assert [s["source_node_id"] for s in chain479] == ["A", "B", "C479"]
    assert [s["source_node_id"] for s in chain477] == ["A", "B", "C477"]
    # No sibling cross-contamination.
    assert "C477" not in [s["source_node_id"] for s in chain479]
    assert "C479" not in [s["source_node_id"] for s in chain477]


def test_collector_resolves_value_through_indirection(tmp_path):
    # Headline fix: the workflow link points at a virtual GetNode (absent from the prompt),
    # but the executed prompt resolves value_0 to the real origin -> section is emitted.
    module = _import_collector()
    project = _project(tmp_path)
    prompt = _collector_prompt(values={0: ["405", 0]}, upstreams={"405": _origin("Power Lora Loader (rgthree)", {"lora_1": "a.safetensors", "strength_1": 1.0, "on_1": True})})
    # Workflow still contains the virtual GetNode the value wire physically passes through.
    workflow = {"nodes": [
        {"id": 418, "type": "GetNode", "title": "Get_Identity"},
        {"id": 20, "type": "SonderMetadataCollector", "inputs": [{"name": "value_0", "link": 3787}]},
    ]}

    _run(module, project, prompt, workflow=workflow, value_0="connected", label_0="Feature Transfer")

    section = _chain(project, module)[0]
    assert section["label"] == "Feature Transfer"
    assert section["source_node_id"] == "405"
    assert section["display_type"] == "power_loras"


def test_consumer_with_no_collector_parent_is_empty(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    project._execution_context = {}
    prompt = {
        "E": {"class_type": "SonderEditor", "inputs": {}},
        "SAVE_EDITOR": {"class_type": "SonderSaveBridge", "inputs": {"project": ["E", 0]}},
        "SAVE_NONE": {"class_type": "SonderSaveBridge", "inputs": {}},
    }
    assert module.collector_chain_for_consumer(project._execution_context, prompt, "SAVE_EDITOR") == []
    assert module.collector_chain_for_consumer(project._execution_context, prompt, "SAVE_NONE") == []


def test_collector_raw_widget_text_format(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    prompt = _collector_prompt(values={0: ["10", 0]}, upstreams={"10": _origin("Node", {"seed": 123, "cfg": 7})})

    _run(module, project, prompt, value_0="connected")

    assert _chain(project, module)[0]["raw_widget_text"] == "seed: 123, cfg: 7"


def test_collector_field_size_cap(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    long_value = "x" * 3000
    prompt = _collector_prompt(values={0: ["10", 0]}, upstreams={"10": _origin("Node", {"prompt": long_value})})

    _run(module, project, prompt, value_0="connected")

    section = _chain(project, module)[0]
    assert module.TRUNCATED_MARKER in section["fields"]["prompt"]
    assert long_value in section["raw_widget_text"]


def test_collector_missing_upstream(tmp_path):
    # Collector is in the prompt but its value origin is not -> no sections emitted.
    module = _import_collector()
    project = _project(tmp_path)
    prompt = _collector_prompt(values={0: ["10", 0]}, upstreams={})  # no "10" entry

    _run(module, project, prompt, value_0="connected")

    assert _chain(project, module) == []


def test_collector_passes_project_through_without_persisted_mutation(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    before = project.to_dict()
    prompt = _collector_prompt(values={0: ["10", 0]}, upstreams={"10": _origin()})

    result = _run(module, project, prompt, value_0="connected")

    assert result[0] is project
    assert project.to_dict() == before


def test_section_carries_display_type_for_known_class(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    prompt = _collector_prompt(
        values={0: ["10", 0]},
        upstreams={"10": _origin("Power Lora Loader (rgthree)", {"lora_1": "a.safetensors", "strength_1": 1.0, "on_1": True})},
    )

    _run(module, project, prompt, value_0="connected")

    assert _chain(project, module)[0]["display_type"] == "power_loras"


def test_section_display_type_none_for_unknown_class(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    prompt = _collector_prompt(values={0: ["10", 0]}, upstreams={"10": _origin("Sampler")})

    _run(module, project, prompt, value_0="connected")

    assert _chain(project, module)[0]["display_type"] is None


def test_registry_first_match_wins(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    stub = {
        "display_type": "stub_first",
        "predicate": lambda _class_type: True,
        "transform": lambda _inputs: {"stub": "yes"},
    }
    module.COMPAT_HANDLERS.insert(0, stub)
    try:
        prompt = _collector_prompt(
            values={0: ["10", 0]},
            upstreams={"10": _origin("Power Lora Loader (rgthree)", {"lora_1": "a.safetensors", "strength_1": 1.0, "on_1": True})},
        )
        _run(module, project, prompt, value_0="connected")
    finally:
        module.COMPAT_HANDLERS.remove(stub)

    section = _chain(project, module)[0]
    assert section["display_type"] == "stub_first"
    assert section["fields"] == {"stub": "yes"}


def test_power_lora_large_stack_preserves_list_shape(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    inputs = {}
    for slot in range(1, 51):
        inputs[f"lora_{slot}"] = f"FluxKlein\\very_long_lora_filename_for_slot_{slot:03d}.safetensors"
        inputs[f"strength_{slot}"] = round(0.1 * (slot % 9 + 1), 2)
        inputs[f"on_{slot}"] = bool(slot % 2)
    prompt = _collector_prompt(values={0: ["10", 0]}, upstreams={"10": _origin("Power Lora Loader (rgthree)", inputs)})

    _run(module, project, prompt, value_0="connected")

    section = _chain(project, module)[0]
    assert isinstance(section["fields"]["power_loras"], list), "structured cap must preserve list shape for matcher"
    assert len(section["fields"]["power_loras"]) == 50
    first = section["fields"]["power_loras"][0]
    assert first["name"].startswith("FluxKlein\\")
    assert section["fields"]["enabled_lora_count"] == 25
    assert section["fields"]["total_lora_count"] == 50


def test_shared_collector_core_accepts_v3_dotted_autogrow_inputs(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    prompt = {
        "10": _origin("ImageNode", {"seed": 10}),
        "11": _origin("ModelNode", {"name": "model.safetensors"}),
        "20": {
            "class_type": "SonderMetadataCollectorV3",
            "inputs": {
                "project": ["1", 0],
                "values.value_7": ["11", 0],
                "values.value_0": ["10", 0],
            },
        },
    }

    result = module.collect_metadata(
        project,
        prompt=prompt,
        extra_pnginfo={"workflow": {}},
        unique_id="20",
        values={"value_7": object(), "value_0": "text"},
        labels={"label_7": "Model", "label_0": "Image"},
        capacity=module.MAX_V3_COLLECTOR_INPUTS,
    )

    assert result is project
    chain = _chain(project, module)
    assert [section["label"] for section in chain] == ["Image", "Model"]
    assert [section["source_node_id"] for section in chain] == ["10", "11"]


def test_shared_collector_core_supports_full_v3_capacity(tmp_path):
    module = _import_collector()
    project = _project(tmp_path)
    prompt = {
        "99": _origin("LastNode", {"value": 99}),
        "20": {
            "class_type": "SonderMetadataCollectorV3",
            "inputs": {"values.value_31": ["99", 0]},
        },
    }

    module.collect_metadata(
        project,
        prompt=prompt,
        extra_pnginfo={"workflow": {}},
        unique_id="20",
        values={"values.value_31": object()},
        labels={"label_31": "Last"},
        capacity=module.MAX_V3_COLLECTOR_INPUTS,
    )

    assert module.MAX_V3_COLLECTOR_INPUTS == 32
    assert _chain(project, module)[0]["label"] == "Last"


def test_collector_fingerprint_shared_contract_and_nan_fallback():
    module = _import_collector()
    prompt = {"20": {"class_type": "SonderMetadataCollector", "inputs": {}}}
    extra = {"workflow": {"nodes": []}}

    first = module.collector_fingerprint(prompt, extra, {"label_0": "A"})
    second = module.collector_fingerprint(prompt, extra, {"label_0": "A"})
    changed = module.collector_fingerprint(prompt, extra, {"label_0": "B"})
    missing = module.collector_fingerprint(prompt, {}, {"label_0": "A"})

    assert first == second
    assert first != changed
    assert isinstance(missing, float)
    assert missing != missing
