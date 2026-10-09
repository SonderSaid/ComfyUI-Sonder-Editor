"""Controls provenance: the section each generated asset records for its run.

Pure tests over hand-built API prompts carrying a Sonder Control Relay in its
export form, with fake node classes injected into the descriptor authority.
"""

import copy
import json

import pytest

from server import control_descriptors as cd
from server import controls_snapshot as cs
from server import prompt_graph


class KSampler:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL",),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF, "control_after_generate": True}),
                "steps": ("INT", {"default": 20, "min": 1, "max": 100}),
                "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 30.0}),
                "sampler_name": (["euler", "dpmpp_2m"],),
            }
        }


class TextNode:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"text": ("STRING", {"multiline": True}), "clip": ("CLIP",)}}


class Plain:
    @classmethod
    def INPUT_TYPES(cls):
        return {"optional": {"value": ("STRING", {}), "images": ("IMAGE",), "project": ("SONDER_PROJECT",),
                             "filename_prefix": ("STRING", {})}}


class SonderEditor:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"project": ("STRING", {})}, "optional": {"fps": ("FLOAT", {"min": 1.0, "max": 120.0})}}


CLASSES = {"KSampler": KSampler, "CLIPTextEncode": TextNode, "SonderEditor": SonderEditor,
           "VAEDecode": Plain, "SonderSaveVideo": Plain, "SonderSaveBridge": Plain, "SaveImage": Plain,
           "SonderMetadataCollector": Plain}


def lookup(class_type):
    return CLASSES.get(class_type)


def _node(class_type, inputs=None, title=None):
    node = {"class_type": class_type, "inputs": dict(inputs or {})}
    if title:
        node["_meta"] = {"title": title}
    return node


def _binding(path, name, *, label="", seed_mode=None, **presentation):
    entry = {"label": label, "status": "ok", "targets": [{"path": path, "input": name}]}
    if seed_mode:
        entry["seed_mode"] = seed_mode
    entry.update(presentation)
    return entry


def _relay(bindings, **extra):
    return _node("SonderControlRelay", {"manifest": json.dumps({"v": 1, "bindings": bindings, **extra})})


def _prompt(bindings=None, **extra):
    """Editor 1 → sampler 3 → decode 4 → Save Video 9; a second branch 5 → save 8."""
    prompt = {
        "1": _node("SonderEditor", {"project": "P", "fps": 24.0}),
        "2": _node("CLIPTextEncode", {"text": "a red fox", "clip": ["6", 0]}),
        "3": _node("KSampler", {"model": ["6", 0], "seed": 41, "steps": 20, "cfg": 7.5,
                                "sampler_name": "euler", "positive": ["2", 0]}, title="Sampler"),
        "4": _node("VAEDecode", {"images": ["3", 0]}),
        "5": _node("KSampler", {"model": ["6", 0], "seed": 7, "steps": 5, "cfg": 1.0, "sampler_name": "euler"}),
        "6": _node("CheckpointLoaderSimple", {"ckpt_name": "x"}),
        "8": _node("SaveImage", {"images": ["5", 0]}),
        "9": _node("SonderSaveVideo", {"project": ["1", 0], "images": ["4", 0], "filename_prefix": "take"}),
    }
    prompt["20"] = _relay(bindings if bindings is not None else {
        "steps": _binding("3", "steps", label="Steps", group="Sampling", order=1),
        "cfg": _binding("3", "cfg", label="CFG", group="Sampling", order=2),
        "seed": _binding("3", "seed", label="Seed", seed_mode="increment"),
        "text": _binding("2", "text", label="Prompt"),
        # Stored "editor", but SonderEditor is not an editor class: Workflow.
        "fps": _binding("1", "fps", label="Frame rate", section="editor"),
        # Save Video is: Editor, whatever was stored (section by node class).
        "save": _binding("9", "filename_prefix", label="File name"),
        "other": _binding("5", "steps", label="Other steps"),
    }, **extra)
    return prompt


def snap(prompt, consumers=("9",), **kwargs):
    return cs.controls_snapshot(prompt, list(consumers), class_lookup=lookup, **kwargs)


def by_id(section):
    """Each record with its defaults filled in and its value read from ``fields``."""
    return {control["binding_id"]: {"label": control["field"], "group": "", "section": "workflow",
                                    "origin": "authored", **control, "value": section["fields"][control["field"]]}
            for control in section["controls"]}


def test_records_relevant_bindings_with_values_labels_and_seed_mode():
    section = snap(_prompt())
    assert section["label"] == "Controls at run"
    assert section["display_type"] == "controls"
    controls = by_id(section)
    # "other" sets a sampler that cannot reach Save Video 9.
    assert set(controls) == {"steps", "cfg", "seed", "text", "fps", "save"}
    assert controls["seed"]["value"] == "41" and controls["seed"]["seed_mode"] == "increment"
    assert controls["steps"]["value"] == "20" and controls["steps"]["kind"] == "INT"
    assert controls["cfg"]["value"] == 7.5
    assert controls["text"]["value"] == "a red fox"
    assert controls["fps"]["section"] == "workflow"
    assert controls["save"]["section"] == "editor"
    assert controls["steps"]["targets"] == [{"path": "3", "input": "steps"}]
    assert section["fields"] == {"Prompt": "a red fox", "Seed": "41", "Steps": "20", "CFG": 7.5, "Frame rate": 24.0,
                                 "File name": "take"}
    # Workflow before Editor, ungrouped before groups, then order.
    assert [c["binding_id"] for c in section["controls"]] == ["fps", "text", "seed", "steps", "cfg", "save"]
    assert "raw_widget_text" not in section
    assert "note" not in json.dumps(section["controls"])
    # Defaults and values are not repeated in the detail records.
    raw = {control["binding_id"]: control for control in section["controls"]}
    assert raw["steps"] == {"binding_id": "steps", "field": "Steps", "kind": "INT", "group": "Sampling",
                            "targets": [{"path": "3", "input": "steps"}]}
    assert raw["save"]["section"] == "editor" and "value" not in raw["save"]
    assert "section" not in raw["fps"]  # the default (workflow) is not repeated


def test_snapshot_is_pure():
    prompt = _prompt()
    before = copy.deepcopy(prompt)
    snap(prompt, tracked=[{"label": "x", "source_node_id": "3", "fields": {"steps": 20}, "display_type": None}])
    assert prompt == before


def test_nothing_without_a_relay_or_with_a_relay_error():
    prompt = _prompt()
    del prompt["20"]
    assert snap(prompt) is None
    broken = _prompt()
    broken["20"]["inputs"]["manifest"] = "{not json"
    assert snap(broken) is None
    two = _prompt()
    two["21"] = copy.deepcopy(two["20"])
    assert snap(two) is None
    assert snap(_prompt(bindings={})) is None


def test_bindings_that_are_not_ok_are_left_out():
    prompt = _prompt()
    prompt["3"]["inputs"]["steps"] = ["6", 0]  # linked: not a Control this run
    del prompt["2"]  # muted: absent from the prompt
    controls = by_id(snap(prompt))
    assert "steps" not in controls and "text" not in controls
    assert "seed" in controls


def test_seed_exactness_beyond_2_53():
    prompt = _prompt()
    prompt["3"]["inputs"]["seed"] = 2 ** 64 - 1
    assert by_id(snap(prompt))["seed"]["value"] == "18446744073709551615"


def test_flattened_subgraph_keys_are_walked():
    prompt = {
        "1": _node("SonderEditor", {"project": "P"}),
        "433:8": _node("KSampler", {"model": ["6", 0], "seed": 3, "steps": 9, "cfg": 2.0, "sampler_name": "euler"}),
        "433:9": _node("VAEDecode", {"images": ["433:8", 0]}),
        "9": _node("SonderSaveVideo", {"project": ["1", 0], "images": ["433:9", 0]}),
        "20": _relay({"s": _binding("433:8", "steps", label="Inner steps")}),
    }
    assert by_id(snap(prompt))["s"]["value"] == "9"
    assert snap(prompt, consumers=("missing",)) is None


def test_bridge_consumers_reach_the_generation_branch():
    prompt = _prompt()
    del prompt["9"]
    prompt["10"] = _node("SonderSaveBridge", {"project": ["1", 0]})
    prompt["11"] = _node("SaveImage", {"images": ["4", 0], "filename_prefix": ["10", 1]})
    # The Bridge alone sees only its project branch.
    alone = by_id(snap(prompt, consumers=("10",)))
    assert set(alone) == {"fps"}
    consumers = ["10", *sorted(prompt_graph.downstream_set(prompt, "10"))]
    assert consumers == ["10", "11"]
    assert {"seed", "steps", "fps"} <= set(by_id(snap(prompt, consumers=consumers)))


def test_bridge_consumers_through_an_intermediate_node():
    prompt = _prompt()
    del prompt["9"]
    prompt["10"] = _node("SonderSaveBridge", {"project": ["1", 0]})
    prompt["12"] = _node("StringConcatenate", {"string_a": ["10", 1], "string_b": "_x"})
    prompt["11"] = _node("SaveImage", {"images": ["4", 0], "filename_prefix": ["12", 0]})
    assert prompt_graph.downstream_set(prompt, "10") == {"11", "12"}
    consumers = ["10", *sorted(prompt_graph.downstream_set(prompt, "10"))]
    assert {"seed", "steps", "fps"} <= set(by_id(snap(prompt, consumers=consumers)))
    assert prompt_graph.downstream_set(prompt, "missing") == set()


def test_long_text_is_capped_with_a_marker():
    prompt = _prompt()
    prompt["2"]["inputs"]["text"] = "x" * 2000
    value = by_id(snap(prompt))["text"]["value"]
    assert value == "x" * cs.TEXT_VALUE_LIMIT + cs.TRUNCATED_MARKER
    assert "full_fields" not in snap(prompt)


def test_binding_count_and_byte_caps():
    prompt = {"1": _node("SonderEditor", {"project": "P"}),
              "9": _node("SonderSaveVideo", {"project": ["1", 0]})}
    bindings = {}
    for index in range(200):
        key = str(100 + index)
        prompt[key] = _node("CLIPTextEncode", {"text": "t" * 400})
        prompt["9"]["inputs"][f"in{index}"] = [key, 0]
        bindings[f"b{index}"] = _binding(key, "text", label=f"Label {index:03d} " + "L" * 100)
    prompt["20"] = _relay(bindings)
    section = snap(prompt)
    size = len(json.dumps(section, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert size <= cs.MAX_SECTION_BYTES
    assert section["truncated"] == 200 - len(section["controls"])
    assert len(section["controls"]) < cs.MAX_BINDINGS
    assert set(section["fields"]) == {c["field"] for c in section["controls"]}


def test_duplicate_labels_get_distinct_fields():
    section = snap(_prompt(bindings={
        "a": _binding("3", "steps", label="Steps"),
        "b": _binding("3", "cfg", label="Steps"),
    }))
    assert sorted(section["fields"]) == ["Steps", "Steps [b]"]


def test_supplied_matches_generic_collector_fields_only():
    tracked = [
        {"label": "Sampler", "source_node_id": "3", "display_type": None,
         "fields": {"steps": 20, "cfg": 7.5, "denoise": 1.0}},
        {"label": "LoRAs", "source_node_id": "3", "display_type": "power_loras", "fields": {"steps": 1}},
        {"label": "Unit", "source_node_id": "3", "display_type": "subgraph", "fields": {"cfg": 7.5}},
    ]
    assert "supplied" not in snap(_prompt())
    section = snap(_prompt(), tracked=tracked)
    assert section["supplied"] == [
        {"section_index": 0, "field": "steps", "binding_id": "steps"},
        {"section_index": 0, "field": "cfg", "binding_id": "cfg"},
    ]


# ── Sonder App delta ─────────────────────────────────────────────────────────

def _sdk_view(prompt, **stored):
    from server import sdk_templates
    described = sdk_templates.describe_stored(prompt, stored, lambda p, **kw: cd.describe_bindings(p, class_lookup=lookup, **kw))
    return sdk_templates.apply_stored_values(
        prompt, described, stored, describe=lambda p, **kw: cd.describe_bindings(p, class_lookup=lookup, **kw))


def test_sdk_delta_carries_overrides_local_bindings_and_hidden_ids():
    prompt = _prompt()
    view = _sdk_view(prompt, labels={"binding:steps": "Quality"}, layout={"binding:cfg": {"group": "Look"}},
                     hidden={"binding:text": True},
                     local={"loc1": {"targets": [{"path": "3", "input": "sampler_name", "class_type": "KSampler"}],
                                     "section": "workflow", "label": "Sampler", "group": "", "order": 0}})
    delta = cs.sdk_overlay_delta(view, prompt)
    assert delta["v"] == 1
    assert delta["labels"] == {"steps": "Quality"}
    assert delta["groups"] == {"cfg": "Look"}
    assert delta["hidden"] == ["text"]
    assert delta["local"]["loc1"]["targets"] == [{"path": "3", "input": "sampler_name", "class_type": "KSampler"}]
    assert cs.sdk_overlay_delta(_sdk_view(_prompt()), _prompt()) is None

    stamped = copy.deepcopy(prompt)
    stamped["1"]["_meta"] = {"sonder_controls": delta}
    controls = by_id(snap(stamped))
    assert "text" not in controls
    assert controls["steps"]["label"] == "Quality"
    assert controls["cfg"]["group"] == "Look"
    assert controls["loc1"]["origin"] == "local" and controls["loc1"]["value"] == "euler"


def test_sdk_seed_modes_and_order_come_from_the_app():
    prompt = _prompt()
    view = _sdk_view(prompt, bindings={"seed": {"seed_mode": "fixed", "sig": {
        "class_type": "KSampler", "input": "seed", "kind": "SEED"}}},
        layout={"binding:text": {"order": 50}},
        local={"loc1": {"targets": [{"path": "5", "input": "seed", "class_type": "KSampler"}], "label": "Other seed"}})
    delta = cs.sdk_overlay_delta(view, prompt)
    # The exported mode is "increment"; the app runs "fixed". App-only seeds default to randomize.
    assert delta["seed_modes"] == {"seed": "fixed", "loc1": "randomize"}
    assert delta["orders"] == {"text": 50}
    stamped = copy.deepcopy(prompt)
    stamped["1"]["_meta"] = {"sonder_controls": delta}
    section = snap(stamped)
    controls = by_id(section)
    assert controls["seed"]["seed_mode"] == "fixed"
    assert [c["binding_id"] for c in section["controls"]] == ["fps", "seed", "text", "steps", "cfg", "save"]
    # "Other seed" sets sampler 5, which cannot reach Save Video 9.
    assert "loc1" not in controls


def test_hostile_local_delta_is_bounded():
    prompt = _prompt()
    many = {f"loc{i}": {"targets": [{"path": "3", "input": "steps", "class_type": "KSampler"}] * 9}
            for i in range(10)}
    many.update({f"ok{i}": {"targets": [{"path": "3", "input": "cfg", "class_type": "KSampler"}]}
                 for i in range(200)})
    prompt["1"]["_meta"] = {"sonder_controls": {"v": 1, "local": many, "seed_modes": {"seed": "sideways"},
                                                "orders": {"steps": True, "cfg": 10 ** 9}}}
    delta = cs.read_sdk_delta(prompt)
    assert len(delta["local"]) == cs.MAX_LOCAL_BINDINGS
    assert not any(key.startswith("loc") for key in delta["local"])
    assert delta["seed_modes"] == {} and delta["orders"] == {"cfg": 9999}


def test_sdk_local_bindings_record_without_a_relay():
    prompt = _prompt()
    del prompt["20"]
    prompt["1"]["_meta"] = {"sonder_controls": {"v": 1, "local": {
        "loc": {"targets": [{"path": "3", "input": "steps", "class_type": "KSampler"}], "label": "Steps"}}}}
    assert by_id(snap(prompt))["loc"]["value"] == "20"


@pytest.mark.parametrize("raw", [
    "nope", {"v": 2, "labels": {"steps": "X"}}, {"v": 1, "labels": {"__proto__": "X", "bad id!": "Y"}},
    {"v": 1, "hidden": "steps"}, {"v": 1, "local": ["x"]},
])
def test_hostile_delta_only_changes_labels_safely(raw):
    prompt = _prompt()
    prompt["1"]["_meta"] = {"sonder_controls": raw}
    controls = by_id(snap(prompt))
    assert controls["steps"]["label"] == "Steps"
    assert "steps" in controls


def test_delta_labels_are_capped():
    prompt = _prompt()
    prompt["1"]["_meta"] = {"sonder_controls": {"v": 1, "labels": {"steps": "Q" * 500}}}
    assert by_id(snap(prompt))["steps"]["label"] == "Q" * 120


def test_graph_for_run_stamps_the_delta_only_when_present():
    from server.sdk_runner import SdkRunner
    prompt = _prompt()
    build = {"prompt": prompt, "editor_key": "1", "seed_bindings": [], "controls_delta": None}
    assert "_meta" not in SdkRunner._graph_for_run(build, {})["1"]
    # A delta already in the template (built from a take's prompt) never survives.
    prompt["1"]["_meta"] = {"title": "Editor", "sonder_controls": {"v": 1, "hidden": ["steps"]}}
    assert SdkRunner._graph_for_run(build, {})["1"]["_meta"] == {"title": "Editor"}
    prompt["1"]["_meta"] = "not a dict"
    build["controls_delta"] = {"v": 1, "hidden": ["text"]}
    assert SdkRunner._graph_for_run(build, {})["1"]["_meta"] == {"sonder_controls": {"v": 1, "hidden": ["text"]}}
    del prompt["1"]["_meta"]
    build["controls_delta"] = None
    build["controls_delta"] = {"v": 1, "hidden": ["text"]}
    graph = SdkRunner._graph_for_run(build, {})
    assert graph["1"]["_meta"]["sonder_controls"] == {"v": 1, "hidden": ["text"]}
    assert "_meta" not in prompt["1"]
