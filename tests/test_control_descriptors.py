"""Controls coercion, kinds, binding descriptors, change validation and seeds.

Pure tests with fake node classes and hand-built API prompts carrying a Sonder
Control Relay manifest in its export form (what ``control_relay.js`` emits).
"""

import ast
import json
import math
import random
from pathlib import Path

import pytest

from server import control_descriptors as cd

REPO_ROOT = Path(__file__).resolve().parents[1]


def _node(class_type, inputs=None, title=None):
    node = {"class_type": class_type, "inputs": dict(inputs or {})}
    if title is not None:
        node["_meta"] = {"title": title}
    return node


def _binding(path, name, *, label="", status="ok", seed_mode=None, **presentation):
    entry = {"label": label, "status": status, "targets": [{"path": path, "input": name}] if path else []}
    if seed_mode:
        entry["seed_mode"] = seed_mode
    entry.update(presentation)
    return entry


def _relay(bindings=None, notes=None, suggestions=None, **extra):
    manifest = {"v": 1, "bindings": bindings or {}, "notes": notes or {}, **extra}
    if suggestions is not None:
        manifest["suggestions"] = suggestions
    return _node("SonderControlRelay", {"manifest": json.dumps(manifest)})


class KSampler:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL",),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF, "control_after_generate": True}),
                "steps": ("INT", {"default": 20, "min": 1, "max": 100}),
                "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 30.0, "step": 0.1}),
                "sampler_name": (["euler", "dpmpp_2m"],),
                "scheduler": ("COMBO", {"options": ["normal", "karras"]}),
                "denoise": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0}),
            }
        }

    RETURN_TYPES = ("LATENT",)


class TextNode:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"text": ("STRING", {"multiline": True})},
            "optional": {"enabled": ("BOOLEAN", {"default": True, "label_on": "On", "label_off": "Off"})},
        }


class AnySink:
    @classmethod
    def INPUT_TYPES(cls):
        return {"optional": {"value_1": ("*",), "lane": ("COMFY_MATCHTYPE_V3", {})}}


class KwargsValidated:
    """A third-party node whose combo options a frontend fills: native skips its checks."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"folder": (["out"],), "count": ("INT", {"min": 0, "max": 9})}}

    @classmethod
    def VALIDATE_INPUTS(cls, **_kwargs):
        return True


class OtherInputValidated:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"mode": (["a", "b"],), "amount": ("FLOAT", {"min": 0.0, "max": 2.0})}}

    @classmethod
    def VALIDATE_INPUTS(cls, mode):
        return mode in ("a", "b")


class SonderSelector:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"choice": (["Option 1", "Option 2"],), "choice_list": ("STRING", {"multiline": True})}}

    @classmethod
    def VALIDATE_INPUTS(cls, choice="", choice_list="", **kwargs):
        return True


class SonderSaveVideo:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "project": ("SONDER_PROJECT",),
                "place_audio_on_timeline": ("BOOLEAN", {"default": True}),
                "mark_queue_complete": ("BOOLEAN", {"default": False}),
            }
        }


class SonderEditor:
    RETURN_TYPES = ("SONDER_PROJECT",)

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}, "optional": {"selection_start": ("INT", {"default": 0})}}


class SonderControlRelay:
    @classmethod
    def INPUT_TYPES(cls):
        return {"optional": {"manifest": ("STRING", {"multiline": True})}}


class Multi:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"tags": ("COMBO", {"options": ["a", "b", "c"], "multi_select": True})}}


CLASSES = {
    cls.__name__: cls
    for cls in (
        KSampler, TextNode, AnySink, KwargsValidated, OtherInputValidated, SonderSelector,
        SonderSaveVideo, SonderEditor, SonderControlRelay, Multi,
    )
}


def describe(prompt, **kwargs):
    return cd.describe_bindings(prompt, class_lookup=CLASSES.get, **kwargs)


def binding(prompt, binding_id, **kwargs):
    return next(b for b in describe(prompt, **kwargs)["bindings"] if b["binding_id"] == binding_id)


# ── Module shape ─────────────────────────────────────────────────────────────

def test_module_level_imports_are_standard_library_and_pure_siblings_only():
    tree = ast.parse((REPO_ROOT / "server" / "control_descriptors.py").read_text(encoding="utf-8"))
    allowed = {"copy", "threading", "__future__", "hashlib", "inspect", "json", "logging", "math", "random", "re", "dataclasses", "decimal", "typing"}
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert {alias.name.split(".")[0] for alias in node.names} <= allowed
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                assert node.module is None
                assert {a.name for a in node.names} <= {"binding_policy", "control_manifest", "prompt_graph"}
            else:
                assert (node.module or "").split(".")[0] in allowed, node.module


# ── Coercion ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "kind, raw, expected",
    [
        ("STRING", "a cat", "a cat"),
        ("STRING", "", ""),
        ("STRING", None, ""),
        ("COMBO", "euler", "euler"),
        ("INT", "42", 42),
        ("INT", " -7 ", -7),
        ("INT", "3.0", 3),
        ("INT", 12, 12),
        ("INT", 4.0, 4),
        ("INT", "18446744073709551615", 18446744073709551615),
        ("FLOAT", "7.5", 7.5),
        ("FLOAT", "3", 3.0),
        ("FLOAT", 2, 2.0),
        ("BOOLEAN", "true", True),
        ("BOOLEAN", " Off ", False),
        ("BOOLEAN", "1", True),
        ("BOOLEAN", False, False),
        ("BOOLEAN", 0, False),
    ],
)
def test_coerce_converts_each_kind(kind, raw, expected):
    value = cd.coerce_control_value(kind, raw)
    assert value == expected
    assert type(value) is type(expected)


def test_coerce_seed_uses_the_seed_widget():
    assert cd.coerce_control_value("SEED", "ignored", seed=1234) == 1234
    with pytest.raises(cd.ControlValueError):
        cd.coerce_control_value("SEED", "", seed=None)
    with pytest.raises(cd.ControlValueError):
        cd.coerce_control_value("SEED", "", seed=True)


@pytest.mark.parametrize(
    "kind, raw",
    [
        ("INT", "3.5"),
        ("INT", "seven"),
        ("INT", ""),
        ("INT", True),
        ("INT", "inf"),
        ("INT", None),
        # Refused before conversion: int(Decimal("1e1000000")) takes over 30 s.
        ("INT", "1e1000000"),
        ("INT", "1e41"),
        ("INT", "9" * 65),
        ("FLOAT", "1" * 65),
        ("FLOAT", "nan"),
        ("FLOAT", "inf"),
        ("FLOAT", "x"),
        ("FLOAT", ""),
        ("FLOAT", True),
        ("BOOLEAN", "maybe"),
        ("BOOLEAN", ""),
        ("BOOLEAN", 2),
        ("COMBO", 3),
        ("STRING", 3),
        ("NOPE", "x"),
    ],
)
def test_coerce_refuses_rather_than_guessing(kind, raw):
    with pytest.raises(cd.ControlValueError):
        cd.coerce_control_value(kind, raw)


def test_target_checks_apply_range_and_options():
    steps = {"kind": "INT", "min": 1, "max": 100}
    cd.check_value_for_target(100, steps)
    with pytest.raises(cd.ControlValueError, match="above the maximum of 100"):
        cd.check_value_for_target(101, steps)
    with pytest.raises(cd.ControlValueError, match="below the minimum of 1"):
        cd.check_value_for_target(0, steps)
    combo = {"kind": "COMBO", "options": ["euler", "dpmpp_2m"]}
    cd.check_value_for_target("euler", combo)
    with pytest.raises(cd.ControlValueError, match="not one of the options"):
        cd.check_value_for_target("delta", combo)
    cd.check_value_for_target(10 ** 30, {"kind": "*"})


# ── Kinds and specs ──────────────────────────────────────────────────────────

def test_derive_kind():
    assert cd.derive_kind([]) == cd.ANY_KIND
    assert cd.derive_kind(["*", "*"]) == cd.ANY_KIND
    assert cd.derive_kind(["INT", "*"]) == "INT"
    assert cd.derive_kind(["INT", "SEED"]) == "SEED"
    assert cd.derive_kind(["COMBO", "COMBO"]) == "COMBO"
    assert cd.derive_kind(["INT", "FLOAT"]) is None
    assert cd.derive_kind(["STRING", "COMBO"]) is None


def test_spec_reads_both_combo_shapes_seeds_and_wildcards():
    assert cd.spec_from_input_info(["a", "b"], {})["options"] == ["a", "b"]
    assert cd.spec_from_input_info("COMBO", {"options": ["x"]}) == {"type": "COMBO", "kind": "COMBO", "options": ["x"]}
    assert cd.spec_from_input_info("INT", {"control_after_generate": True})["kind"] == "SEED"
    assert cd.spec_from_input_info("INT", {"control_after_generate": "fixed"})["kind"] == "SEED"
    assert cd.spec_from_input_info("INT", {}, "noise_seed")["kind"] == "SEED"
    # An explicit flag wins over the name, as the frontend's `??` does.
    assert cd.spec_from_input_info("INT", {"control_after_generate": False}, "seed")["kind"] == "INT"
    assert cd.spec_from_input_info("INT", {"control_after_generate": None}, "seed")["kind"] == "SEED"
    assert cd.spec_from_input_info("INT", {"min": 1, "max": 4})["kind"] == "INT"
    assert cd.spec_from_input_info("*", None)["kind"] == cd.ANY_KIND
    assert cd.spec_from_input_info("COMFY_MATCHTYPE_V3", {})["kind"] == cd.ANY_KIND
    assert cd.spec_from_input_info("IMAGE", {})["kind"] is None
    multiline = cd.spec_from_input_info("STRING", {"multiline": True, "default": "x"})
    assert multiline["multiline"] is True and multiline["default"] == "x"



# ── Describe ─────────────────────────────────────────────────────────────────

def test_binding_ok_with_typed_value_bounds_label_and_targets():
    prompt = {
        "1": _relay({"b_steps": _binding("9", "steps", label="Steps", order=2, group="Sampling", note="**More** detail")}),
        "9": _node("KSampler", {"steps": 30, "model": ["4", 0]}, title="Sampler"),
    }
    view = describe(prompt)
    assert view["relay"] == {"status": "ok", "key": "1", "error": None}
    b = view["bindings"][0]
    assert b["status"] == cd.STATUS_OK and b["reason"] is None
    assert (b["label"], b["order"], b["group"], b["note"], b["section"]) == ("Steps", 2, "Sampling", "**More** detail", "workflow")
    assert b["kind"] == "INT" and b["value"] == 30 and b["error"] is None
    assert (b["min"], b["max"]) == (1, 100)
    assert b["targets"] == [{"path": "9", "input": "steps", "class_type": "KSampler", "status": "ok"}]
    assert "class_def" not in str(view) and "literal" not in str(b)


def test_default_label_names_the_node_and_input():
    prompt = {"1": _relay({"b": _binding("9", "cfg")}), "9": _node("KSampler", {"cfg": 7.5}, title="Sampler")}
    assert binding(prompt, "b")["label"] == "Sampler · cfg"


def test_value_out_of_range_is_reported_not_raised():
    prompt = {"1": _relay({"b": _binding("9", "steps"), "c": _binding("9", "sampler_name")}),
              "9": _node("KSampler", {"steps": 500, "sampler_name": "delta"})}
    view = {b["binding_id"]: b for b in describe(prompt)["bindings"]}
    assert view["b"]["status"] == cd.STATUS_OK and "above the maximum of 100" in view["b"]["error"]
    assert view["b"]["value"] == 500
    assert "not one of the options" in view["c"]["error"]


@pytest.mark.parametrize("inputs, status", [
    ({"steps": ["4", 0]}, cd.STATUS_LINKED),
    ({}, cd.STATUS_MISSING),
])
def test_linked_and_absent_inputs(inputs, status):
    prompt = {"1": _relay({"b": _binding("9", "steps")}), "9": _node("KSampler", inputs)}
    b = binding(prompt, "b")
    assert b["status"] == status and b["reason"] and b["kind"] is None and b["value"] is None


def test_a_forged_ok_is_checked_against_the_prompt():
    # The export claims ok for a node the prompt lacks, a linked input and an
    # input Sonder owns: every one is refused by the server's own reading.
    prompt = {
        "1": _relay({
            "gone": _binding("77", "steps"),
            "linked": _binding("9", "model"),
            "denied": _binding("2", "selection_start"),
            "nowidget": _binding("9", "model_name"),
        }),
        "2": _node("SonderEditor", {"selection_start": 3}),
        "9": _node("KSampler", {"model": ["4", 0], "model_name": "x"}),
    }
    view = {b["binding_id"]: b for b in describe(prompt)["bindings"]}
    assert view["gone"]["status"] == cd.STATUS_MISSING
    assert view["linked"]["status"] == cd.STATUS_LINKED
    assert view["denied"]["status"] == cd.STATUS_DENIED
    assert view["nowidget"]["status"] == cd.STATUS_UNSUPPORTED
    assert "no input named" in view["nowidget"]["reason"]


@pytest.mark.parametrize("status", ["missing", "duplicate", "linked", "no_widget", "unsupported"])
def test_exported_problem_statuses_are_kept_with_a_reason(status):
    prompt = {"1": _relay({"b": _binding(None, "steps", status=status)}), "9": _node("KSampler", {"steps": 3})}
    b = binding(prompt, "b")
    assert b["status"] == status and b["reason"] == cd.STATUS_REASONS[status]


def test_wildcards_non_values_and_multiselect_are_unsupported():
    prompt = {"1": _relay({"any": _binding("5", "value_1"), "model": _binding("9", "model"),
                           "multi": _binding("6", "tags")}),
              "5": _node("AnySink", {"value_1": 3}), "9": _node("KSampler", {"model": "x"}),
              "6": _node("Multi", {"tags": ["a", "b", "c"]})}
    view = {b["binding_id"]: b for b in describe(prompt)["bindings"]}
    assert view["any"]["status"] == cd.STATUS_UNSUPPORTED and "any type" in view["any"]["reason"]
    assert view["model"]["status"] == cd.STATUS_UNSUPPORTED and "not a value" in view["model"]["reason"]
    assert view["multi"]["status"] == cd.STATUS_UNSUPPORTED and "Lists of choices" in view["multi"]["reason"]


def test_promoted_input_with_two_consumers_merges_bounds_and_options():
    class OtherSampler:
        @classmethod
        def INPUT_TYPES(cls):
            return {"required": {"sampler_name": ("COMBO", {"options": ["dpmpp_2m", "euler", "lcm"]}),
                                 "steps": ("INT", {"min": 5, "max": 50})}}

    classes = dict(CLASSES, OtherSampler=OtherSampler)
    prompt = {"1": _relay({"s": {"label": "Sampler", "status": "ok",
                                  "targets": [{"path": "7:3", "input": "sampler_name"}, {"path": "7:4", "input": "sampler_name"}]},
                           "n": {"label": "Steps", "status": "ok",
                                  "targets": [{"path": "7:3", "input": "steps"}, {"path": "7:4", "input": "steps"}]}}),
              "7:3": _node("KSampler", {"sampler_name": "euler", "steps": 20}),
              "7:4": _node("OtherSampler", {"sampler_name": "euler", "steps": 20})}
    view = {b["binding_id"]: b for b in cd.describe_bindings(prompt, class_lookup=classes.get)["bindings"]}
    assert view["s"]["options"] == ["euler", "dpmpp_2m"] and view["s"]["status"] == "ok"
    assert (view["n"]["min"], view["n"]["max"]) == (5, 50)
    assert len(view["n"]["targets"]) == 2


def test_mixed_target_kinds_are_unsupported():
    prompt = {"1": _relay({"m": {"label": "M", "status": "ok", "targets": [{"path": "9", "input": "steps"},
                                                                          {"path": "9", "input": "cfg"}]}}),
              "9": _node("KSampler", {"steps": 3, "cfg": 2.0})}
    assert binding(prompt, "m")["status"] == cd.STATUS_UNSUPPORTED


def test_seed_binding_reads_exact_value_and_mirrored_mode():
    large = 2 ** 64 - 1
    prompt = {"1": _relay({"seed": _binding("9", "seed", seed_mode="increment")}),
              "9": _node("KSampler", {"seed": large})}
    b = binding(prompt, "seed")
    assert b["kind"] == "SEED" and b["value"] == large and b["seed_mode"] == "increment"
    assert cd.json_safe({"bindings": [b]})["bindings"][0]["value"] == str(large)


def test_boolean_reads_only_a_real_boolean():
    prompt = {"1": _relay({"on": _binding("5", "enabled"), "word": _binding("6", "enabled")}),
              "5": _node("TextNode", {"text": "", "enabled": False}), "6": _node("TextNode", {"text": "", "enabled": "false"})}
    view = {b["binding_id"]: b for b in describe(prompt)["bindings"]}
    assert view["on"]["value"] is False and view["on"]["error"] is None
    assert view["on"]["label_on"] == "On"
    assert view["word"]["error"]


def test_selector_options_come_from_its_effective_choice_list():
    prompt = {"1": _relay({"c": _binding("5", "choice")}),
              "5": _node("SonderSelector", {"choice": "Detail", "choice_list": "Base\nDecompression\n\nDetail\n"})}
    b = binding(prompt, "c")
    assert b["options"] == ["Base", "Decompression", "Detail"] and b["provider"] is True
    assert b["value"] == "Detail" and b["error"] is None and not b.get("checked_at_run")


def test_selector_with_a_linked_choice_list_is_checked_at_run():
    prompt = {"1": _relay({"c": _binding("5", "choice")}),
              "5": _node("SonderSelector", {"choice": "Detail", "choice_list": ["8", 0]})}
    b = binding(prompt, "c")
    assert b["checked_at_run"] is True and "options" not in b and b["status"] == cd.STATUS_OK


def test_validator_covered_inputs_skip_server_bounds_and_options():
    # Native validation skips min/max/combo for inputs a validator covers
    # (execution.py:1029), so the panel must not refuse what a run accepts.
    prompt = {"1": _relay({"f": _binding("5", "folder"), "n": _binding("5", "count"),
                           "a": _binding("6", "amount"), "m": _binding("6", "mode")}),
              "5": _node("KwargsValidated", {"folder": "renders/today", "count": 99}),
              "6": _node("OtherInputValidated", {"mode": "a", "amount": 1.0})}
    view = {b["binding_id"]: b for b in describe(prompt)["bindings"]}
    for name in ("f", "n"):
        assert view[name]["checked_at_run"] is True and view[name]["error"] is None
        assert "options" not in view[name] and "max" not in view[name]
    assert view["f"]["value"] == "renders/today"
    assert view["m"]["checked_at_run"] is True
    assert not view["a"].get("checked_at_run") and view["a"]["max"] == 2.0


def test_bindings_and_notes_sort_and_notes_and_suggestions_are_listed():
    prompt = {"1": _relay(
        {"z": _binding("9", "steps", label="Alpha", order=5), "y": _binding("9", "cfg", label="beta", order=-1),
         "x": _binding("9", "denoise", label="Gamma", order=-1)},
        notes={"n2": {"text": "Two", "order": 3}, "n1": {"text": "# One", "group": "Intro", "order": -2}},
        suggestions=[{"binding_id": "orphan1", "targets": [{"path": "9", "input": "scheduler"}]},
                     {"binding_id": "z", "targets": []}]),
        "9": _node("KSampler", {"steps": 3, "cfg": 2.0, "denoise": 1.0, "scheduler": "normal"}, title="Sampler")}
    view = describe(prompt)
    assert [b["binding_id"] for b in view["bindings"]] == ["y", "x", "z"]
    assert [n["note_id"] for n in view["notes"]] == ["n1", "n2"]
    assert view["notes"][0] == {"source": "note", "note_id": "n1", "origin": "authored", "text": "# One", "group": "Intro", "order": -2, "status": "ok"}
    assert view["suggestions"] == [{"binding_id": "orphan1", "targets": [{"path": "9", "input": "scheduler"}],
                                    "label": "Sampler · scheduler", "section": "workflow"}]


@pytest.mark.parametrize("prompt, error", [
    ({"9": _node("KSampler")}, None),
    ({"1": _relay(), "2": _relay()}, "more than one"),
    ({"4:1": _relay()}, "inside a subgraph"),
    ({"1": _node("SonderControlRelay", {"manifest": "{nope"})}, "not valid JSON"),
    ({"1": _relay(error="resolver failed")}, "resolver failed"),
])
def test_relay_problems_are_view_errors(prompt, error):
    view = describe(prompt)
    if error is None:
        assert view["relay"]["status"] == "none" and "error" not in view
    else:
        assert view["relay"]["status"] == "error" and error in view["error"]
    assert view["bindings"] == [] and view["notes"] == []


@pytest.mark.parametrize("census, error", [
    ({"active": 0, "muted": 1, "bypassed": 0, "nested": 0}, "muted or bypassed"),
    ({"active": 2, "muted": 0, "bypassed": 0, "nested": 0}, "more than one"),
    ({"active": 1, "muted": 0, "bypassed": 0, "nested": 1}, "inside a subgraph"),
    ({"active": 1, "muted": 1, "bypassed": 0, "nested": 0}, None),
])
def test_canvas_census_reports_what_an_export_cannot_show(census, error):
    prompt = {"1": _relay({"b": _binding("9", "steps")}), "9": _node("KSampler", {"steps": 3})}
    view = describe(prompt, relay_census=census)
    assert (view["relay"]["status"] == "error") == (error is not None)
    if error:
        assert error in view["error"]


def test_describe_refuses_a_non_object_prompt():
    with pytest.raises(ValueError):
        cd.describe_bindings([1, 2])


# ── Change validation ────────────────────────────────────────────────────────

def _selector_prompt():
    return {"1": _relay({"c": _binding("5", "choice"), "l": _binding("5", "choice_list"),
                         "s": _binding("9", "steps"), "seed": _binding("9", "seed"),
                         "on": _binding("6", "enabled"), "gone": _binding("77", "steps")},
                        notes={"n": {"text": "x"}}),
            "5": _node("SonderSelector", {"choice": "Detail", "choice_list": "Base\nDecompression\nDetail"}),
            "6": _node("TextNode", {"text": "", "enabled": True}),
            "9": _node("KSampler", {"steps": 20, "seed": 5})}


def test_change_grammar_and_coercion():
    prompt = _selector_prompt()
    view = describe(prompt)
    cleaned, rejected = cd.validate_binding_changes(view, {
        "binding:s:value": "30", "binding:seed:value": str(2 ** 64 - 1), "binding:seed:seed_mode": "fixed",
        "binding:on:value": False, "binding:c:label": "  Feature  ", "binding:c:note": "Pick one",
        "binding:c:order": "4", "binding:c:group": " Look ", "note:n:text": "# New", "binding:gone:label": "Kept",
    }, prompt=prompt)
    assert rejected == []
    assert cleaned == {"binding:s:value": 30, "binding:seed:value": 2 ** 64 - 1, "binding:seed:seed_mode": "fixed",
                       "binding:on:value": False, "binding:c:label": "Feature", "binding:c:note": "Pick one",
                       "binding:c:order": 4, "binding:c:group": "Look", "note:n:text": "# New",
                       "binding:gone:label": "Kept"}


@pytest.mark.parametrize("changes, reason", [
    ({"binding:s:value": "300"}, "above the maximum"),
    ({"binding:on:value": "false"}, "on or off"),
    ({"binding:gone:value": 3}, "unavailable"),
    ({"binding:s:seed_mode": "fixed"}, "unavailable"),
    ({"binding:seed:seed_mode": "sideways"}, "Choose fixed"),
    ({"binding:c:label": "x" * 121}, "120 characters"),
    ({"binding:c:note": "x" * 4001}, "4000"),
    ({"binding:c:order": 10000}, "-9999 and 9999"),
    ({"control:abc:value": "1"}, "unavailable"),
    ({"field:SonderSaveVideo/mark_queue_complete": True}, "unavailable"),
    ({"binding:c:value": "Gamma"}, "not one of the options"),
])
def test_change_refusals(changes, reason):
    prompt = _selector_prompt()
    cleaned, rejected = cd.validate_binding_changes(describe(prompt), changes, prompt=prompt)
    assert reason in rejected[0]["reason"]


def test_batch_size_limits():
    view = describe(_selector_prompt())
    assert cd.validate_binding_changes(view, {})[1][0]["reason"] == "Send 1 to 64 changes."
    many = {f"binding:x{i}:label": "a" for i in range(65)}
    assert cd.validate_binding_changes(view, many)[1][0]["reason"] == "Send 1 to 64 changes."


def test_a_new_choice_list_and_a_choice_in_it_are_accepted_together():
    prompt = _selector_prompt()
    view = describe(prompt)
    cleaned, rejected = cd.validate_binding_changes(
        view, {"binding:l:value": "Alpha\nGamma", "binding:c:value": "Gamma"}, prompt=prompt)
    assert rejected == [] and cleaned["binding:c:value"] == "Gamma"
    # Without the prompt (the mounted relay's pre-check) the same batch defers
    # the option check to the canvas writer instead of refusing it.
    safe = cd.json_safe(view)
    _, rejected = cd.validate_binding_changes(safe, {"binding:l:value": "Alpha\nGamma", "binding:c:value": "Gamma"})
    assert rejected == []
    _, rejected = cd.validate_binding_changes(safe, {"binding:c:value": "Gamma"})
    assert "not one of the options" in rejected[0]["reason"]


def test_validation_accepts_json_safe_bounds_and_capped_previews():
    prompt = _selector_prompt()
    safe = cd.json_safe(describe(prompt))
    cleaned, rejected = cd.validate_binding_changes(safe, {"binding:s:value": "100"})
    assert rejected == [] and cleaned["binding:s:value"] == 100
    _, rejected = cd.validate_binding_changes(safe, {"binding:s:value": "101"})
    assert "above the maximum" in rejected[0]["reason"]


def test_binding_signature_tracks_target_class_input_and_kind():
    prompt = _selector_prompt()
    b = binding(prompt, "s")
    assert cd.binding_signature(b) == {"class_type": "KSampler", "input": "steps", "kind": "INT"}
    assert cd.binding_signature(binding(prompt, "gone")) is None


def test_json_safe_copies_exact_integers_and_nonfinite_values():
    large = 2**63 + 123
    raw = {"bindings": [{"kind": "SEED", "value": large, "min": 0, "max": 2**64 - 1, "step": 1, "default": large},
                        {"kind": "FLOAT", "value": 0.5, "min": -math.inf, "max": math.inf}],
           "changes": {"binding:a:value": large, "binding:a:label": "x"},
           "other": [math.inf, {"n": math.nan, "enabled": True}]}
    safe = cd.json_safe(raw)
    encoded = json.loads(json.dumps(safe, allow_nan=False))
    assert encoded["bindings"][0] == {"kind": "SEED", "value": str(large), "min": "0", "max": str(2**64 - 1),
                                      "step": "1", "default": str(large)}
    assert encoded["bindings"][1] == {"kind": "FLOAT", "value": 0.5, "min": None, "max": None}
    assert encoded["changes"] == {"binding:a:value": str(large), "binding:a:label": "x"}
    assert encoded["other"] == [None, {"n": None, "enabled": True}]
    safe["bindings"][0]["value"] = "changed"
    assert raw["bindings"][0]["value"] == large


@pytest.mark.parametrize("count", [2000, 2001])
def test_combo_transport_cap_preserves_full_validation(count):
    class LongCombo:
        @classmethod
        def INPUT_TYPES(cls):
            return {"required": {"option": ([f"opt-{i}" for i in range(count)],)}}

    classes = dict(CLASSES, LongCombo=LongCombo)
    prompt = {"1": _relay({"o": _binding("2", "option")}), "2": _node("LongCombo", {"option": f"opt-{count - 1}"})}
    raw = cd.describe_bindings(prompt, class_lookup=classes.get)
    assert raw["bindings"][0]["error"] is None and len(raw["bindings"][0]["options"]) == count
    safe = cd.json_safe(raw)
    assert len(safe["bindings"][0]["options"]) == 2000
    assert safe["bindings"][0].get("options_truncated", False) == (count > 2000)
    # A capped preview defers membership to the full view instead of refusing.
    cleaned, rejected = cd.validate_binding_changes(safe, {"binding:o:value": f"opt-{count - 1}"})
    assert rejected == []


def test_describe_route_validates_with_the_prompt_and_returns_json_safe(monkeypatch):
    from server import routes
    original = cd.describe_bindings
    monkeypatch.setattr(cd, "describe_bindings", lambda prompt, relay_census=None, **kw: original(
        prompt, relay_census=relay_census, class_lookup=CLASSES.get, **kw))
    prompt = _selector_prompt()
    prompt["9"]["inputs"]["seed"] = 2**63 + 7
    body = {"prompt": prompt, "relay_census": {"active": 1}, "changes": {
        "binding:l:value": "A\nB", "binding:c:value": "B", "binding:seed:value": str(2**63 + 9)}}
    result = routes._describe_controls_body(json.dumps(body).encode("utf-8"))
    seed = next(b for b in result["bindings"] if b["binding_id"] == "seed")
    assert seed["value"] == str(2**63 + 7) and seed["max"] == str(2**64 - 1)
    assert result["ok"] is True and result["changes"]["binding:seed:value"] == str(2**63 + 9)
    with pytest.raises(routes._ControlsRequestError):
        routes._describe_controls_body(json.dumps({"prompt": prompt, "relay_census": [1]}).encode("utf-8"))


def test_recommended_editor_fields_are_data():
    assert {(e.class_type, e.field) for e in cd.FIELD_CONTROLS} == {
        ("SonderSaveVideo", "mode"),
        ("SonderSaveVideo", "filename_prefix"),
        ("SonderSaveVideo", "save_preset"),
        ("SonderSaveVideo", "mark_queue_complete"),
        ("SonderSaveVideo", "place_audio_on_timeline"),
        ("SonderSaveBridge", "prefix"),
        ("SonderSaveBridge", "mark_queue_complete"),
        ("SonderMasksBridge", "edit_video"),
        ("SonderMasksBridge", "edit_audio"),
        ("SonderSelector", "choice"),
    }
    # A Selector is named after its node (it says which feature it picks).
    selector = next(e for e in cd.FIELD_CONTROLS if e.class_type == "SonderSelector")
    assert cd.recommended_label(selector, {"class_type": "SonderSelector", "_meta": {"title": "Style"}}) == "Style"
    assert cd.recommended_label(selector, {"class_type": "SonderSelector"}) == "SonderSelector"


@pytest.mark.parametrize("classes,stored,expected", [
    (["SonderSaveVideo"], "workflow", "editor"),
    (["SonderSaveBridge", "SonderMasksBridge"], "workflow", "editor"),
    (["SonderSaveVideo", "KSampler"], "editor", "workflow"),
    (["SonderSelector"], "editor", "workflow"),
    (["SonderEditor"], "editor", "workflow"),
    ([None], "editor", "editor"),  # node gone: the stored section is the fallback
    ([], "sidebar", "workflow"),
])
def test_section_follows_the_target_node_class(classes, stored, expected):
    prompt = {str(i): {"class_type": c, "inputs": {}} for i, c in enumerate(classes) if c}
    targets = [{"path": str(i), "input": "x"} for i in range(len(classes))]
    assert cd.section_for(prompt, targets, stored) == expected


def test_section_ignores_the_target_status():
    # A linked Save Video setting keeps Editor: the class comes from the prompt
    # node, not from the per-status read.
    prompt = {"9": {"class_type": "SonderSaveVideo", "inputs": {"filename_prefix": ["3", 0]}},
              "20": {"class_type": "SonderControlRelay", "inputs": {"manifest": json.dumps(
                  {"v": 1, "bindings": {"p": {"label": "", "targets": [{"path": "9", "input": "filename_prefix"}]}}})}}}
    view = cd.describe_bindings(prompt, class_lookup=lambda c: None)
    assert view["bindings"][0]["status"] != "ok"
    assert view["bindings"][0]["section"] == "editor"


# ── Seeds ────────────────────────────────────────────────────────────────────

def _frontend_next_number(mode, value, minimum=None, maximum=None, step=None, r=0.0):
    """computeNextNumberValue from frontend 1.53.10 (settingStore bundle), with
    Math.random() fixed to ``r``; kept literal so drift is visible."""
    if mode == "fixed":
        return value
    n = 0 if minimum is None else minimum
    rmax = 1 if maximum is None else maximum
    i = 1 if step is None else step
    a = min(0x4000000000000, rmax)
    o = max(-0x4000000000000, n)
    s = (a - o) / i
    c = value
    if mode in ("increment", "increment-wrap"):
        c += i
    elif mode == "decrement":
        c -= i
    elif mode == "randomize":
        c = math.floor(r * s) * i + o
    return min(max(c, o), a)


class _FixedRandom:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


@pytest.mark.parametrize("mode", ["fixed", "increment", "increment-wrap", "decrement", "randomize"])
@pytest.mark.parametrize(
    "value, minimum, maximum, step",
    [
        (1000, 0, 0xFFFFFFFFFFFFFFFF, None),
        (0, 0, 0xFFFFFFFFFFFFFFFF, None),
        (4294967295, 0, 4294967295, None),
        (5, 1, 10, 2),
        (2 ** 50, 0, 0xFFFFFFFFFFFFFFFF, None),
        (-3, -10, 10, None),
        (7, None, None, None),
    ],
)
@pytest.mark.parametrize("r", [0.0, 0.5, 0.999999])
def test_next_seed_matches_frontend_algorithm(mode, value, minimum, maximum, step, r):
    expected = _frontend_next_number(mode, value, minimum, maximum, step, r)
    actual = cd.next_seed_value(mode, value, minimum=minimum, maximum=maximum, step=step, rng=_FixedRandom(r))
    assert actual == expected


def test_next_seed_randomize_respects_a_32_bit_target():
    rng = random.Random(4)
    for _ in range(200):
        seed = cd.next_seed_value("randomize", 1, minimum=0, maximum=4294967295, rng=rng)
        assert 0 <= seed <= 4294967295 and isinstance(seed, int)


def test_next_seed_leaves_unknown_modes_and_non_numbers_alone():
    assert cd.next_seed_value("sideways", 5, minimum=0, maximum=9) == 5
    assert cd.next_seed_value("increment", "5", minimum=0, maximum=9) == "5"
    assert cd.next_seed_value("increment", True, minimum=0, maximum=9) is True


# ── Audit follow-ups (Phase 3A) ──────────────────────────────────────────────

class Encoder:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"prompt": ("STRING", {"multiline": True})}, "hidden": {"unique_id": "UNIQUE_ID"}}


def test_a_widget_named_prompt_is_bindable_and_a_hidden_input_is_unsupported():
    classes = dict(CLASSES, Encoder=Encoder)
    prompt = {"1": _relay({"p": _binding("5", "prompt"), "h": _binding("5", "unique_id")}),
              "5": _node("Encoder", {"prompt": "a cat", "unique_id": "5"})}
    view = {b["binding_id"]: b for b in cd.describe_bindings(prompt, class_lookup=classes.get)["bindings"]}
    assert view["p"]["status"] == cd.STATUS_OK and view["p"]["value"] == "a cat"
    assert view["h"]["status"] == cd.STATUS_UNSUPPORTED


def test_coverage_is_per_target_so_an_uncovered_consumer_keeps_its_checks():
    # One promoted value feeds a covered node and an ordinary one: the ordinary
    # node's range still applies, while the covered one is checked at run.
    prompt = {"1": _relay({"n": {"label": "Count", "status": "ok",
                                 "targets": [{"path": "5", "input": "count"}, {"path": "9", "input": "steps"}]}}),
              "5": _node("KwargsValidated", {"folder": "out", "count": 50}),
              "9": _node("KSampler", {"steps": 50})}
    b = binding(prompt, "n")
    assert b["checked_at_run"] is True and (b["min"], b["max"]) == (1, 100)
    _, rejected = cd.validate_binding_changes(describe(prompt), {"binding:n:value": 101}, prompt=prompt)
    assert "above the maximum of 100" in rejected[0]["reason"]


def test_two_bindings_on_one_input_are_duplicates():
    prompt = {"1": _relay({"a": _binding("9", "steps"), "b": _binding("9", "steps"), "c": _binding("9", "cfg")}),
              "9": _node("KSampler", {"steps": 3, "cfg": 2.0})}
    view = {b["binding_id"]: b for b in describe(prompt)["bindings"]}
    assert view["a"]["status"] == view["b"]["status"] == cd.STATUS_DUPLICATE
    assert view["c"]["status"] == cd.STATUS_OK


def test_a_new_list_that_drops_the_current_choice_is_refused_unless_the_choice_moves_too():
    prompt = _selector_prompt()  # choice "Detail"
    view = describe(prompt)
    _, rejected = cd.validate_binding_changes(view, {"binding:l:value": "Alpha\nGamma"}, prompt=prompt)
    assert rejected[0]["target"] == "binding:l:value" and "'Detail' is not in the new list" in rejected[0]["reason"]
    cleaned, rejected = cd.validate_binding_changes(view, {"binding:l:value": "Alpha\nDetail"}, prompt=prompt)
    assert rejected == [] and cleaned == {"binding:l:value": "Alpha\nDetail"}
    cleaned, rejected = cd.validate_binding_changes(
        view, {"binding:l:value": "Alpha\nGamma", "binding:c:value": "Gamma"}, prompt=prompt)
    assert rejected == []


def test_stored_values_beyond_the_request_cap_validate_in_one_batch():
    many = {f"binding:x{i}:label": "a" for i in range(200)}
    view = {"bindings": [{"binding_id": f"x{i}", "status": "ok"} for i in range(200)], "notes": []}
    assert cd.validate_binding_changes(view, many)[1][0]["reason"] == "Send 1 to 64 changes."
    cleaned, rejected = cd.validate_binding_changes(view, many, max_changes=None)
    assert rejected == [] and len(cleaned) == 200


def test_group_order_puts_explicit_groups_first_then_first_appearance():
    view = {"groups": {"Output": {"order": 2}, "Models": {"order": -1}},
            "bindings": [{"binding_id": "a", "label": "Steps", "order": 1, "group": "Sampling"},
                         {"binding_id": "b", "label": "Seed", "order": 0, "group": ""},
                         {"binding_id": "c", "label": "Model", "order": 5, "group": "Models"}],
            "notes": [{"note_id": "n", "order": 0, "group": "Intro"}]}
    assert cd.group_order(view) == ["Models", "Output", "Intro", "Sampling"]


def test_advanced_is_a_flag_validated_on_its_own():
    view = {"bindings": [{"binding_id": "s", "status": "ok", "kind": "INT", "targets": []},
                         {"binding_id": "off", "status": "missing", "kind": None, "targets": []}]}
    cleaned, rejected = cd.validate_binding_changes(view, {"binding:s:advanced": True, "binding:off:advanced": False})
    assert rejected == [] and cleaned == {"binding:s:advanced": True, "binding:off:advanced": False}
    for bad in ("true", 1, None):
        _, rejected = cd.validate_binding_changes(view, {"binding:s:advanced": bad})
        assert rejected and "true or false" in rejected[0]["reason"]


# ── Phase 4B: group grammar ──────────────────────────────────────────────────

@pytest.mark.parametrize("target, parsed", [
    ("binding:aB-_9:value", ("binding", "aB-_9", "value")),
    ("note:n1:create", ("note", "n1", "create")),
    ("group:Look and feel:rename", ("group", "Look and feel", "rename")),
    ("group:Ünïcode 😀:move", ("group", "Ünïcode 😀", "move")),
    ("workflow:description", ("workflow", "", "description")),
    ("group::rename", None), ("group:a:b:rename", None), ("binding:bad id:value", None),
    ("binding:a", None), ("workflow:other", None), (3, None), ("control:a:value", None),
])
def test_parse_target_is_the_one_grammar(target, parsed):
    assert cd.parse_target(target) == parsed


def test_structural_targets_include_every_group_op():
    assert all(cd.is_structural(f"group:G:{op}") for op in ("create", "rename", "ungroup", "move"))
    assert not cd.is_structural("group:G:order") and not cd.is_structural("binding:a:group")
    assert cd.is_structural("note:n:create") and cd.is_structural("binding:a:hide")
    assert not cd.is_structural("workflow:description")


def _grouped_view():
    return {"relay": {"status": "ok"}, "groups": {"Look": {"order": 1}},
            "bindings": [{"binding_id": f"b{i}", "status": "ok", "kind": "INT", "label": f"B{i}", "order": i,
                          "group": "Look" if i < 70 else "Speed", "section": "workflow", "targets": []} for i in range(72)]
            + [{"binding_id": "e", "status": "ok", "kind": "INT", "label": "E", "order": 0, "group": "Hidden",
                "section": "editor", "targets": []}],
            "notes": [{"note_id": "n", "status": "ok", "text": "x", "order": 0, "group": "Guide"}]}


def test_group_ops_validate_to_one_unexpanded_plan_each():
    view = _grouped_view()
    view["group_order"] = cd.group_order(view)
    # An Editor-section binding's group shows nowhere, so it is not a group.
    assert view["group_order"] == ["Look", "Guide", "Speed"]
    for mode in ("canvas", "sdk"):
        # Renaming a 70-member group is one target: no 64-change cap applies.
        cleaned, rejected = cd.validate_binding_changes(view, {"group:Look:rename": " Style "}, structural=mode)
        assert rejected == [] and cleaned == {"group:Look:rename": {"rename": "Style"}}
        cleaned, rejected = cd.validate_binding_changes(view, {"group:Guide:ungroup": True}, structural=mode)
        assert cleaned == {"group:Guide:ungroup": {"ungroup": True}}
        cleaned, rejected = cd.validate_binding_changes(view, {"group:Speed:move": {"before": "Look"}}, structural=mode)
        assert cleaned == {"group:Speed:move": {"move": True, "before": "Look"}}
        cleaned, rejected = cd.validate_binding_changes(view, {"group:New:create": {}}, structural=mode)
        assert cleaned == {"group:New:create": {"create": True}}


@pytest.mark.parametrize("changes", [
    {"group:Look:create": {}},                 # already there: a retry
    {"group:Gone:rename": "Look"},             # landed rename retried
    {"group:Gone:ungroup": True},              # landed ungroup retried
    {"group:Look:move": {"before": "Guide"}},  # already in place
    {"group:Speed:move": {"before": None}},    # already last
    {"group:Look:rename": "Look"},
])
def test_landed_group_ops_retry_as_no_ops(changes):
    view = _grouped_view()
    cleaned, rejected = cd.validate_binding_changes(view, changes, structural="canvas")
    assert rejected == [] and list(cleaned.values()) == [{"noop": True}]


@pytest.mark.parametrize("changes, reason", [
    ({"group:Look:rename": "Guide"}, "already exists"),
    ({"group:Gone:rename": "Other"}, "no longer in the Controls"),
    ({"group:Look:move": {"before": "Gone"}}, "order changed"),
    ({"group:Gone:move": {"before": None}}, "no longer in the Controls"),
    ({"group:Look:move": {"before": "Look"}}, "before itself"),
    ({"group:Look:move": "Guide"}, "where to move"),
    ({"group:Look:rename": "a:b"}, "colon"),
    ({"group:Look:rename": "constructor"}, "reserved"),
    ({"group:Look:rename": "x" * 121}, "at most 120"),
    ({"group:Look:rename": "   "}, "needs a name"),
    ({"group:Look:rename": 5}, "must be text"),
    ({"group:Look:ungroup": False}, "Send true"),
    ({"group:New:create": True}, "Send {}"),
    ({"group: Look:ungroup": True}, "invalid"),
    ({"group:Bell\x07:create": {}}, "control characters"),
    ({"group:Look:rename": "Style", "group:Style:create": {}}, "same group"),
    ({"group:Look:order": 3}, "unavailable"),
    ({"binding:b0:group": "Tab\tName"}, "control characters"),
    ({"binding:b0:group": "a:b"}, "colon"),
    ({"note:x:create": {"text": "t", "group": "__proto__"}}, "reserved"),
])
def test_group_names_and_ops_are_refused_with_a_reason(changes, reason):
    view = _grouped_view()
    _, rejected = cd.validate_binding_changes(view, changes, structural="canvas")
    assert rejected and reason in rejected[-1]["reason"], rejected


def test_group_ops_need_a_structural_host_and_a_readable_relay():
    view = _grouped_view()
    _, rejected = cd.validate_binding_changes(view, {"group:New:create": {}})
    assert "unavailable" in rejected[0]["reason"]
    view["relay"] = {"status": "error", "error": "Two Relays."}
    _, rejected = cd.validate_binding_changes(view, {"group:New:create": {}}, structural="sdk")
    assert "Fix the Sonder Control Relay first: Two Relays." in rejected[0]["reason"]


def test_the_mounted_precheck_checks_group_shape_only():
    # The canvas validates existence against its fresh view; a stale mounted view must not refuse.
    view = _grouped_view()
    cleaned, rejected = cd.validate_binding_changes(
        view, {"group:Gone:rename": "Guide", "group:Other:move": {"before": "Nowhere"}}, structural="relay")
    assert rejected == [] and cleaned["group:Gone:rename"] == {"rename": "Guide"}
    _, rejected = cd.validate_binding_changes(view, {"group:Gone:rename": "a:b"}, structural="relay")
    assert "colon" in rejected[0]["reason"]


def test_writes_refuse_a_group_past_max_groups():
    names = [f"g{i}" for i in range(cd.control_manifest.MAX_GROUPS)]
    view = {"relay": {"status": "ok"}, "groups": {name: {"order": 0} for name in names},
            "bindings": [{"binding_id": "a", "status": "ok", "kind": "INT", "label": "A", "order": 0, "group": "",
                          "targets": []}], "notes": []}
    for changes in ({"group:extra:create": {}}, {"binding:a:group": "extra"},
                    {"note:n:create": {"text": "t", "group": "extra"}}):
        _, rejected = cd.validate_binding_changes(view, changes, structural="canvas")
        assert rejected and "At most 256 groups" in rejected[0]["reason"], changes
    # Moving into an existing group, or renaming, never adds one.
    cleaned, rejected = cd.validate_binding_changes(
        view, {"binding:a:group": "g3", "group:g4:rename": "fresh"}, structural="canvas")
    assert rejected == []
