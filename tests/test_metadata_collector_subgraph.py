"""Metadata Collector provenance for origins inside subgraphs.

Fixtures under tests/fixtures/ are real frontend 1.53.6 captures (graphToPrompt
output plus the trimmed workflow) from the probe instance:
- subgraph_qwen_edit_2509.json: the native Qwen Image Edit 2509 template, a
  collector wired to the subgraph's IMAGE output.
- subgraph_nested.json: "Prompt Builder" containing "Inner Builder", built by
  converting the outer subgraph first (1.53.6 drops links when a selection
  already holds a subgraph); a root collector and one inside the outer subgraph.
- subgraph_multi.json: two caption subgraphs with different promoted values and
  a LoRA subgraph with on- and off-branch Power Lora Loaders. rgthree adds rows
  through its own widget UI, so the lora_N rows were injected into the prompt in
  the node's serialized shape.
"""

import copy
import importlib
import json
import time
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
TEST_PACKAGE = "video_editor_testpkg"


def _module(name):
    if TEST_PACKAGE not in sys.modules:
        pkg = types.ModuleType(TEST_PACKAGE)
        pkg.__path__ = [str(ROOT)]
        sys.modules[TEST_PACKAGE] = pkg
    importlib.invalidate_caches()
    return importlib.import_module(f"{TEST_PACKAGE}.nodes.{name}")


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class _Project:
    pass


def _collect(data, *, unique_id=None, values=None, labels=None, workflow="fixture"):
    collector = _module("metadata_collector")
    uid = unique_id or data["collector"]
    inputs = data["prompt"][uid]["inputs"]
    project = _Project()
    collector.collect_metadata(
        project,
        prompt=data["prompt"],
        extra_pnginfo={"workflow": data["workflow"] if workflow == "fixture" else workflow},
        unique_id=uid,
        values=values or {key: object() for key in inputs if "value_" in key},
        labels=labels or {},
        capacity=32,
    )
    return project._execution_context[collector.TRACKED_METADATA_CONTEXT_KEY][uid]


def _labels(sections):
    return [section["label"] for section in sections]


# --- Real captures -----------------------------------------------------------------------

def test_qwen_template_records_the_subgraph_interface_not_its_vae_decode():
    sections = _collect(_fixture("subgraph_qwen_edit_2509.json"))

    # The producer is VAEDecode, which has no widget values of its own.
    assert len(sections) == 1
    section = sections[0]
    assert section["label"] == "Image Edit (Qwen 2509)"
    assert section["display_type"] == "subgraph"
    assert section["source_node_id"] == "433"
    assert section["source_node_class"] == "Image Edit (Qwen 2509)"
    assert section["fields"] == {
        "image": "← Load Image: image_qwen_image_edit_2509_input_image.png",
        "positive_prompt": "Replace the cat with a dalmatian, keeping the environment and scene consistent",
        "negative_prompt": "",
        "seed": 362225868152841,
        "unet_name": "qwen_image_edit_2509_fp8_e4m3fn.safetensors",
        "clip_name": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
        "vae_name": "qwen_image_vae.safetensors",
        "enable_turbo_mode": True,
        "lightning_lora": "Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors",
    }
    assert section["subgraph"] == {
        "definition_id": "eba40a3a-f6c5-48ac-b58e-55525d06b373",
        "definition_name": "Image Edit (Qwen 2509)",
        "instance_key": "433",
        "output_names": ["IMAGE"],
        "producer_node_id": "433:8",
    }
    # Unwired optional images and the unused lora_name input are omitted.
    assert "image2 (optional)" not in section["fields"]
    assert "lora_name" not in section["fields"]


def test_values_come_from_the_executed_prompt_not_widget_values():
    data = _fixture("subgraph_qwen_edit_2509.json")
    data["prompt"]["433:3"]["inputs"]["seed"] = 7  # e.g. control_after_generate

    assert _collect(data)[0]["fields"]["seed"] == 7


def test_nested_subgraphs_resolve_through_both_interface_levels():
    data = _fixture("subgraph_nested.json")

    root = _collect(data)
    assert _labels(root) == ["Prompt Builder", "Prompt Builder › Concatenate Text"]
    assert root[0]["source_node_id"] == "7"
    # string_b arrives from the root text node through two subgraph inputs; one
    # hop resolves the primitive to its text.
    assert root[0]["fields"] == {"string_b": "outer text"}
    assert root[1]["source_node_id"] == "7:6"

    inner = _collect(data, unique_id=data["innerCollector"])
    assert _labels(inner) == ["Inner Builder", "Inner Builder › Concatenate Text"]
    assert inner[0]["source_node_id"] == "7:8"
    assert inner[0]["subgraph"]["producer_node_id"] == "7:8:5"
    assert inner[1]["fields"]["delimiter"] == " + "


def test_each_instance_keeps_its_own_promoted_values_and_lora_branch():
    sections = _collect(_fixture("subgraph_multi.json"))

    assert _labels(sections) == [
        "Shot A", "Shot A › Concatenate Text",
        "Shot B", "Shot B › Concatenate Text",
        "LoRA Stack › ModelSamplingAuraFlow",
        "LoRA Stack › Style LoRAs",
    ]
    assert sections[0]["fields"] == {"string_b": "wide lens"}
    assert sections[1]["fields"] == {"string_a": ["15:4", 0], "delimiter": ", "}
    assert sections[2]["fields"] == {"string_b": "close up"}
    loras = sections[5]
    assert loras["display_type"] == "power_loras"
    assert [row["name"] for row in loras["fields"]["power_loras"]] == ["film_grain.safetensors", "sketch.safetensors"]
    # The loader on the other subgraph output never feeds the tapped branch.
    assert "unused_branch.safetensors" not in json.dumps(sections)


def test_two_instances_sharing_one_definition_resolve_separately():
    data = _fixture("subgraph_multi.json")
    prompt = data["prompt"]
    shared = next(node["type"] for node in data["workflow"]["nodes"] if node["id"] == 15)
    for node in data["workflow"]["nodes"]:
        if node["id"] == 16:
            node["type"] = shared
    # Instance 16 now runs definition 15's inner nodes (ids 4 and 5).
    prompt["16:4"] = prompt.pop("16:7")
    prompt["16:5"] = prompt.pop("16:8")
    prompt["16:5"]["inputs"]["string_a"] = ["16:4", 0]
    prompt[data["collector"]]["inputs"]["value_1"] = ["16:5", 0]

    sections = _collect(data)

    shots = [section for section in sections if section["display_type"] == "subgraph"]
    assert [(s["label"], s["fields"]["string_b"]) for s in shots] == [("Shot A", "wide lens"), ("Shot B", "close up")]
    assert shots[0]["subgraph"]["definition_id"] == shots[1]["subgraph"]["definition_id"]


# --- Labels, dedupe, capping ---------------------------------------------------------------

def test_collector_label_names_the_subgraph_and_its_siblings():
    sections = _collect(_fixture("subgraph_multi.json"), labels={"label_0": "Opening caption"})

    assert _labels(sections)[:2] == ["Opening caption", "Opening caption › Concatenate Text"]


def test_two_inputs_tapping_one_unit_append_its_sections_once():
    data = _fixture("subgraph_multi.json")
    inputs = data["prompt"][data["collector"]]["inputs"]
    inputs["value_3"] = list(inputs["value_0"])

    sections = _collect(data)

    assert _labels(sections) == [
        "Shot A", "Shot A › Concatenate Text",
        "Shot B", "Shot B › Concatenate Text",
        "LoRA Stack › ModelSamplingAuraFlow",
        "LoRA Stack › Style LoRAs",
    ]


def test_second_output_of_one_unit_joins_its_section():
    data = _fixture("subgraph_multi.json")
    prompt = data["prompt"]
    # Give the caption subgraph a second output: the inner text node.
    definition = next(d for d in data["workflow"]["definitions"]["subgraphs"]
                      if any(n["id"] == 4 for n in d["nodes"]))
    definition["outputs"].append({"name": "CAPTION", "type": "STRING", "linkIds": [900]})
    definition["links"].append({"id": 900, "origin_id": 4, "origin_slot": 0,
                                "target_id": -20, "target_slot": 1, "type": "STRING"})
    prompt[data["collector"]]["inputs"]["value_3"] = ["15:4", 0]

    sections = _collect(data)

    shot_a = [s for s in sections if s["label"] == "Shot A"]
    assert len(shot_a) == 1
    assert shot_a[0]["subgraph"]["output_names"] == ["STRING", "CAPTION"]
    assert "Shot A › Caption Text" in _labels(sections)


def test_untitled_instances_of_one_definition_get_distinct_labels():
    data = _fixture("subgraph_multi.json")
    for node in data["workflow"]["nodes"]:
        if node["id"] in (15, 16):
            node.pop("title", None)
    for definition in data["workflow"]["definitions"]["subgraphs"]:
        definition["name"] = "Caption"

    labels = [s["label"] for s in _collect(data) if s["display_type"] == "subgraph"]

    assert labels == ["Caption", "Caption [16]"]


def test_duplicate_interface_labels_are_numbered():
    data = _fixture("subgraph_qwen_edit_2509.json")
    definition = data["workflow"]["definitions"]["subgraphs"][0]
    for interface_input in definition["inputs"]:
        if interface_input["name"] in {"prompt", "prompt_1"}:
            interface_input["label"] = "prompt"

    fields = _collect(data)[0]["fields"]

    assert fields["prompt"].startswith("Replace the cat")
    assert fields["prompt #2"] == ""


def test_fan_out_input_reads_its_first_resolvable_target():
    data = _fixture("subgraph_qwen_edit_2509.json")
    for key in ("433:110", "433:111"):
        data["prompt"][key]["inputs"]["image2"] = ["78", 0]

    fields = _collect(data)[0]["fields"]

    assert fields["image2 (optional)"] == "← Load Image: image_qwen_image_edit_2509_input_image.png"


def test_linked_interface_value_that_is_not_a_primitive_names_its_source():
    data = _fixture("subgraph_qwen_edit_2509.json")
    data["prompt"]["80"] = {"class_type": "StringConcatenate", "_meta": {"title": "Join Prompt"},
                            "inputs": {"string_a": "a", "string_b": "b", "delimiter": ""}}
    data["prompt"]["433:111"]["inputs"]["prompt"] = ["80", 0]

    assert _collect(data)[0]["fields"]["positive_prompt"] == "← Join Prompt [80]"


def test_long_interface_value_is_capped_with_copyable_spans():
    collector = _module("metadata_collector")
    data = _fixture("subgraph_qwen_edit_2509.json")
    long_prompt = "harbor " * 600
    data["prompt"]["433:111"]["inputs"]["prompt"] = long_prompt

    section = _collect(data)[0]

    assert section["fields"]["positive_prompt"].endswith(collector.TRUNCATED_MARKER)
    start, end = section["raw_field_spans"]["positive_prompt"]
    raw = section["raw_widget_text"].encode("utf-16-le")
    assert raw[start * 2:end * 2].decode("utf-16-le") == long_prompt
    assert "full_fields" not in section


def test_v3_dotted_inputs_match_v1():
    data = _fixture("subgraph_multi.json")
    v1 = _collect(copy.deepcopy(data))
    inputs = data["prompt"][data["collector"]]["inputs"]
    for key in [key for key in inputs if key.startswith("value_")]:
        inputs[f"values.{key}"] = inputs.pop(key)

    assert _collect(data) == v1


# --- Fallbacks ----------------------------------------------------------------------------

def test_missing_workflow_keeps_the_inner_node_section():
    sections = _collect(_fixture("subgraph_nested.json"), workflow=None)

    assert [(s["source_node_id"], s["display_type"]) for s in sections] == [("7:6", None)]


def test_stale_workflow_falls_back_instead_of_inventing_provenance():
    data = _fixture("subgraph_nested.json")
    for definition in data["workflow"]["definitions"]["subgraphs"]:
        for node in definition["nodes"]:
            if node["id"] == 6:
                node["type"] = "SomethingElse"

    sections = _collect(data)

    assert [(s["source_node_id"], s["display_type"]) for s in sections] == [("7:6", None)]


def test_stale_interface_target_is_omitted():
    data = _fixture("subgraph_qwen_edit_2509.json")
    data["prompt"]["433:3"]["class_type"] = "KSamplerAdvanced"

    assert "seed" not in _collect(data)[0]["fields"]


def test_cyclic_and_malformed_definitions_never_fail_the_run():
    data = _fixture("subgraph_nested.json")
    outer, inner = data["workflow"]["definitions"]["subgraphs"]
    inner["nodes"].append({"id": 99, "type": outer["id"], "inputs": []})  # a definition cycle
    outer["links"] = [None, "bogus", [1, 2], {"id": "x"}] + outer["links"]
    outer["inputs"][0]["linkIds"] = "not-a-list"
    outer["outputs"] = {"not": "a list"}

    sections = _collect(data)
    assert sections, "falls back or resolves, but always records something"

    data["workflow"]["definitions"]["subgraphs"] = "garbage"
    assert [s["source_node_id"] for s in _collect(data)] == ["7:6"]


def test_self_referential_descent_is_bounded():
    provenance = _module("subgraph_provenance")
    fan_out = 8
    loop = {"id": "D", "name": "Loop", "outputs": [],
            "inputs": [{"name": f"in{k}", "linkIds": list(range(fan_out))} for k in range(3)],
            "nodes": [{"id": 1, "type": "D", "inputs": [{"name": f"in{k}"} for k in range(3)]}],
            "links": [{"id": n, "origin_id": -10, "origin_slot": 0, "target_id": 1, "target_slot": 0}
                      for n in range(fan_out)]}
    workflow = {"nodes": [{"id": 5, "type": "D"}, {"id": 6, "type": "Producer"}],
                "definitions": {"subgraphs": [loop]}}
    prompt = {"5:2": {"class_type": "Producer", "inputs": {"x": 1}}}
    loop["nodes"].append({"id": 2, "type": "Producer", "inputs": []})

    started = time.perf_counter()
    origin = provenance.resolve_subgraph_origin("20", ["5:2", 0], prompt, workflow)
    assert time.perf_counter() - started < 1.0
    # Resolution got past the producer check, so the descent really ran.
    assert origin is not None and origin["fields"] == {}


def test_self_referencing_instance_path_falls_back():
    data = _fixture("subgraph_nested.json")
    outer = data["workflow"]["definitions"]["subgraphs"][0]
    for node in outer["nodes"]:
        if node["id"] == 8:
            node["type"] = outer["id"]  # "Inner Builder" now claims to be its own parent
    # Give the bogus path a matching producer so only the cycle guard rejects it.
    outer["nodes"].append({"id": 5, "type": "StringConcatenate", "inputs": [], "outputs": []})

    sections = _collect(data, unique_id=data["innerCollector"])

    assert [(s["label"], s["source_node_id"], s["display_type"]) for s in sections] == [
        ("StringConcatenate", "7:8:5", None),
    ]


def test_resolver_exception_falls_back(monkeypatch):
    collector = _module("metadata_collector")

    def boom(*_args, **_kwargs):
        raise RecursionError("hostile workflow")

    monkeypatch.setattr(collector, "resolve_subgraph_origin", boom)
    sections = _collect(_fixture("subgraph_qwen_edit_2509.json"))

    assert [(s["source_node_id"], s["display_type"]) for s in sections] == [("433:8", None)]


def test_output_fed_straight_from_a_subgraph_input_stays_a_plain_section():
    data = _fixture("subgraph_qwen_edit_2509.json")
    # The IMAGE output now passes the outer LoadImage straight through.
    data["prompt"][data["collector"]]["inputs"]["value_0"] = ["78", 0]

    sections = _collect(data)

    assert [(s["source_node_id"], s["display_type"]) for s in sections] == [("78", None)]


def test_sibling_at_the_collectors_own_level_stays_a_plain_section():
    data = _fixture("subgraph_nested.json")
    inner = data["innerCollector"]
    data["prompt"][inner]["inputs"]["value_0"] = ["7:6", 0]

    sections = _collect(data, unique_id=inner)

    assert [(s["source_node_id"], s["display_type"]) for s in sections] == [("7:6", None)]


def test_unit_is_the_instance_at_the_collectors_own_level():
    provenance = _module("subgraph_provenance")

    assert provenance.unit_key("2", "8:7:5") == "8"
    assert provenance.unit_key("7:9", "7:8:5") == "7:8"
    assert provenance.unit_key("7:9", "5:2") == "5"
    assert provenance.unit_key("8:12", "8:5") is None   # sibling at the collector's level
    assert provenance.unit_key("20", "10") is None
    assert provenance.unit_key("100:20", "100:10") is None


def test_nested_workflow_titles_resolve_for_plain_sections():
    collector = _module("metadata_collector")
    data = _fixture("subgraph_nested.json")

    node = collector._find_collector_workflow_node(data["workflow"], "7:8")
    assert node["title"] == "Inner Builder"
    assert collector._find_collector_workflow_node(data["workflow"], "7:8:4")["type"] == "PrimitiveStringMultiline"


# --- Gallery side -------------------------------------------------------------------------

def _node_json(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for gallery tests")
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


def test_gallery_copies_a_capped_subgraph_field_and_renders_its_output_line():
    data = _fixture("subgraph_qwen_edit_2509.json")
    long_prompt = "\U0001F319 harbor " * 400
    data["prompt"]["433:111"]["inputs"]["prompt"] = long_prompt
    section = _collect(data)[0]
    copy_url = (ROOT / "web/js/gallery_copy_values.js").as_uri()
    renderers_url = (ROOT / "web/js/tracked_metadata_renderers.js").as_uri()

    result = _node_json(f"""
globalThis.document = {{ createElement: () => ({{ textContent: '' }}) }};
const {{ resolveTrackedFieldCopy }} = await import({json.dumps(copy_url)});
const {{ renderTrackedSectionBody }} = await import({json.dumps(renderers_url)});
const section = {json.dumps(section)};
const body = renderTrackedSectionBody(section, {{ style: (el) => el, CHROME: {{ textDim: '#888' }} }});
console.log(JSON.stringify({{
  prompt: resolveTrackedFieldCopy(section, 'positive_prompt'),
  seed: resolveTrackedFieldCopy(section, 'seed'),
  body: {{ text: body.dom.textContent, consumed: body.consumedFields }},
}}));
""")

    assert result["prompt"] == {"kind": "value", "label": "Copy value", "text": long_prompt}
    assert result["seed"]["text"] == "362225868152841"
    assert result["body"] == {"text": "Subgraph \u00b7 IMAGE output", "consumed": []}


def test_gallery_search_finds_subgraph_interface_values():
    section = _collect(_fixture("subgraph_multi.json"))[0]
    gallery = (ROOT / "web/js/shared_asset_gallery.js").read_text(encoding="utf-8")
    start = gallery.index("function trackedMetadataBlob(")
    end = gallery.index("\n}\n", start) + 3
    blob_fn = gallery[start:end]

    result = _node_json(f"""
const trackedBlobMemo = new WeakMap();
{blob_fn}
const entries = [{json.dumps(section)}];
console.log(JSON.stringify(trackedMetadataBlob(entries)));
""")

    assert "shot a" in result.lower() and "wide lens" in result
