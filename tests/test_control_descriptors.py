"""Sonder Control coercion, kinds, descriptors and the seed policy.

Pure tests with fake node classes. The real ComfyUI validation path and the
real node run in `test_sonder_control_runtime.py` against the test install.
"""

import ast
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


def _control(value_text="", kind="STRING", seed=0, control_id="c0000001", order=0, group="", title=None):
    return _node(
        "SonderControl",
        {
            "value_text": value_text,
            "seed": seed,
            "kind": kind,
            "panel_order": order,
            "panel_group": group,
            "control_id": control_id,
        },
        title=title,
    )


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


class ImageLoader:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"image": (["a.png", "b.png"], {"image_upload": True})}}

    @classmethod
    def VALIDATE_INPUTS(cls, image):
        if not image.endswith(".png"):  # raises AttributeError for a linked None
            return "Invalid image file"
        return True


class KwargsValidated:
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


class SonderMasksBridge:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "project": ("SONDER_PROJECT",),
                "edit_video": ("BOOLEAN", {"default": True, "label_on": "Edit", "label_off": "Freeze"}),
                "edit_audio": ("BOOLEAN", {"default": True, "label_on": "Edit", "label_off": "Freeze"}),
            }
        }


class SonderEditor:
    RETURN_TYPES = ("SONDER_PROJECT",)

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}


class SonderControl:
    RETURN_TYPES = ("*",)

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}


CLASSES = {
    cls.__name__: cls
    for cls in (
        KSampler, TextNode, AnySink, ImageLoader, KwargsValidated, OtherInputValidated,
        SonderSaveVideo, SonderMasksBridge, SonderEditor, SonderControl,
    )
}


def direct_runner(requests):
    """Call V1 validators the way /prompt does: linked inputs arrive as None."""
    outcomes = []
    for request in requests:
        validator = request["class_def"].VALIDATE_INPUTS
        kwargs = {
            name: (None if isinstance(value, list) else value)
            for name, value in request["inputs"].items()
        }
        import inspect

        spec = inspect.getfullargspec(validator)
        if spec.varkw is None:
            kwargs = {name: value for name, value in kwargs.items() if name in spec.args}
        try:
            result = validator(**kwargs)
        except Exception as exc:
            outcomes.append((False, f"{type(exc).__name__}: {exc}"))
            continue
        outcomes.append((result is True, None if result in (True, False) else str(result)))
    return outcomes


def describe(prompt, runner=direct_runner):
    return cd.describe_controls(prompt, class_lookup=CLASSES.get, validator_runner=runner)


def only_control(prompt, **kwargs):
    controls = describe(prompt, **kwargs)["controls"]
    assert len(controls) == 1, controls
    return controls[0]


# ── Module shape ─────────────────────────────────────────────────────────────

def test_module_level_imports_are_standard_library_and_prompt_graph_only():
    tree = ast.parse((REPO_ROOT / "server" / "control_descriptors.py").read_text(encoding="utf-8"))
    allowed = {"__future__", "asyncio", "inspect", "logging", "math", "random", "dataclasses", "decimal", "typing"}
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert {alias.name.split(".")[0] for alias in node.names} <= allowed
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                assert node.module is None and [a.name for a in node.names] == ["prompt_graph"]
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

def test_derive_kind_and_compatibility():
    assert cd.derive_kind([]) == cd.ANY_KIND
    assert cd.derive_kind(["*", "*"]) == cd.ANY_KIND
    assert cd.derive_kind(["INT", "*"]) == "INT"
    assert cd.derive_kind(["INT", "SEED"]) == "SEED"
    assert cd.derive_kind(["COMBO", "COMBO"]) == "COMBO"
    assert cd.derive_kind(["INT", "FLOAT"]) is None
    assert cd.derive_kind(["STRING", "COMBO"]) is None

    assert cd.kind_compatible("SEED", "INT") and cd.kind_compatible("INT", "SEED")
    assert cd.kind_compatible("FLOAT", cd.ANY_KIND)
    assert not cd.kind_compatible("INT", "FLOAT")
    assert not cd.kind_compatible("STRING", "COMBO")
    assert not cd.kind_compatible("", "STRING")
    assert not cd.kind_compatible("BOGUS", cd.ANY_KIND)


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

def test_control_ok_with_typed_value_bounds_and_label():
    prompt = {
        "5": _control("30", kind="INT", title="Steps", order=2),
        "9": _node("KSampler", {"steps": ["5", 0], "model": ["1", 0]}),
    }
    control = only_control(prompt)
    assert control["status"] == cd.STATUS_OK
    assert control["label"] == "Steps"
    assert control["value"] == 30 and control["error"] is None
    assert control["target_kind"] == "INT"
    assert (control["min"], control["max"]) == (1, 100)
    assert control["order"] == 2 and control["group"] == ""
    assert control["control_id"] == "c0000001"
    assert control["targets"] == [
        {"key": "9", "class_type": "KSampler", "input": "steps", "kind": "INT", "status": "ok"},
    ]
    assert "class_def" not in str(control)


def test_control_value_error_is_reported_not_raised():
    prompt = {
        "5": _control("500", kind="INT"),
        "9": _node("KSampler", {"steps": ["5", 0]}),
        "6": _control("delta", kind="COMBO", control_id="c0000002"),
        "10": _node("KSampler", {"sampler_name": ["6", 0]}),
    }
    controls = {c["key"]: c for c in describe(prompt)["controls"]}
    assert controls["5"]["status"] == cd.STATUS_OK
    assert controls["5"]["value"] is None
    assert "above the maximum of 100" in controls["5"]["error"]
    assert "not one of the options" in controls["6"]["error"]
    assert controls["6"]["options"] == ["euler", "dpmpp_2m"]


def test_combo_options_intersect_across_targets():
    class OtherSampler:
        @classmethod
        def INPUT_TYPES(cls):
            return {"required": {"sampler_name": ("COMBO", {"options": ["dpmpp_2m", "euler", "lcm"]})}}

    classes = dict(CLASSES, OtherSampler=OtherSampler)
    prompt = {
        "5": _control("euler", kind="COMBO"),
        "9": _node("KSampler", {"sampler_name": ["5", 0]}),
        "10": _node("OtherSampler", {"sampler_name": ["5", 0]}),
    }
    control = cd.describe_controls(prompt, class_lookup=classes.get, validator_runner=direct_runner)["controls"][0]
    assert control["status"] == cd.STATUS_OK
    assert control["options"] == ["euler", "dpmpp_2m"]


def test_statuses_unlinked_mixed_stale_and_wildcard():
    prompt = {
        "1": _control("x", control_id="unlinked"),
        "2": _control("3", kind="INT", control_id="mixed"),
        "20": _node("KSampler", {"steps": ["2", 0], "cfg": ["2", 0]}),
        "3": _control("3", kind="INT", control_id="stale"),
        "30": _node("KSampler", {"cfg": ["3", 0]}),
        "4": _control("anything", kind="STRING", control_id="wild"),
        "40": _node("AnySink", {"value_1": ["4", 0], "lane": ["4", 0]}),
        "5": _control("", kind="SEED", seed=777, control_id="seedint"),
        "50": _node("KSampler", {"seed": ["5", 0], "steps": ["5", 0]}),
    }
    by_id = {c["control_id"]: c for c in describe(prompt)["controls"]}
    assert by_id["unlinked"]["status"] == cd.STATUS_UNLINKED
    assert by_id["mixed"]["status"] == cd.STATUS_MIXED and by_id["mixed"]["target_kind"] is None
    assert by_id["stale"]["status"] == cd.STATUS_STALE and by_id["stale"]["target_kind"] == "FLOAT"
    assert by_id["wild"]["status"] == cd.STATUS_OK and by_id["wild"]["target_kind"] == cd.ANY_KIND
    # SEED drives a seed and an INT together; the value must fit both ranges.
    assert by_id["seedint"]["status"] == cd.STATUS_OK
    assert by_id["seedint"]["target_kind"] == "SEED"
    assert "above the maximum of 100" in by_id["seedint"]["error"]


def test_unsupported_targets_name_their_reason():
    prompt = {
        "1": _control("x", control_id="image"),
        "10": _node("KSampler", {"model": ["1", 0]}),
        "2": _control("x", control_id="missing"),
        "20": _node("NotInstalled", {"a": ["2", 0]}),
        "3": _control("x", control_id="noinput"),
        "30": _node("TextNode", {"nope": ["3", 0]}),
    }
    by_id = {c["control_id"]: c for c in describe(prompt)["controls"]}
    assert by_id["image"]["status"] == cd.STATUS_UNSUPPORTED
    assert "takes MODEL" in by_id["image"]["targets"][0]["reason"]
    assert "NotInstalled is not installed" in by_id["missing"]["targets"][0]["reason"]
    assert "no input named nope" in by_id["noinput"]["targets"][0]["reason"]


def test_validator_dry_run_marks_only_refusing_targets():
    calls = []

    def recording_runner(requests):
        calls.extend((r["key"], sorted(r["inputs"])) for r in requests)
        return direct_runner(requests)

    prompt = {
        "1": _control("a.png", kind="COMBO", control_id="loader"),
        "10": _node("ImageLoader", {"image": ["1", 0]}),
        "2": _control("out", kind="COMBO", control_id="kwargs"),
        "20": _node("KwargsValidated", {"folder": ["2", 0], "count": 3}),
        "3": _control("1.5", kind="FLOAT", control_id="uncovered"),
        "30": _node("OtherInputValidated", {"mode": "a", "amount": ["3", 0]}),
    }
    by_id = {c["control_id"]: c for c in describe(prompt, runner=recording_runner)["controls"]}
    assert by_id["loader"]["status"] == cd.STATUS_UNSUPPORTED
    assert by_id["loader"]["targets"][0]["reason"] == cd.VALIDATOR_REFUSAL
    assert "AttributeError" in by_id["loader"]["targets"][0]["detail"]
    assert "detail" not in by_id["kwargs"]["targets"][0]
    assert by_id["kwargs"]["status"] == cd.STATUS_OK
    # A validator that never receives the linked input is not dry-run.
    assert by_id["uncovered"]["status"] == cd.STATUS_OK
    assert {key for key, _ in calls} == {"10", "20"}


def test_a_node_failing_for_its_own_reasons_does_not_refuse_the_control():
    class NodeWide:
        @classmethod
        def INPUT_TYPES(cls):
            return {"required": {"mode": (["a", "b"],), "text": ("STRING", {})}}

        @classmethod
        def VALIDATE_INPUTS(cls, mode, text):
            return mode == "a"  # never looks at the linked input

    classes = dict(CLASSES, NodeWide=NodeWide)
    prompt = {
        "1": _control("hello", control_id="wide"),
        "10": _node("NodeWide", {"mode": "z", "text": ["1", 0]}),
        "2": _control("", kind="COMBO", control_id="loader"),
        "20": _node("ImageLoader", {"image": ["2", 0]}),
    }
    by_id = {
        c["control_id"]: c
        for c in cd.describe_controls(prompt, class_lookup=classes.get, validator_runner=direct_runner)["controls"]
    }
    # The node fails with or without the link, so the link is not to blame.
    assert by_id["wide"]["status"] == cd.STATUS_OK
    # An empty Control value fails too, but the first option passes: refused.
    assert by_id["loader"]["status"] == cd.STATUS_UNSUPPORTED


def test_comfy_runner_refuses_a_running_event_loop():
    import asyncio

    async def on_loop():
        cd.comfy_validator_runner([])

    with pytest.raises(RuntimeError, match="to_thread"):
        asyncio.run(on_loop())


def test_validator_runner_gets_literals_cast_and_links_kept():
    seen = {}

    def runner(requests):
        for request in requests:
            seen[request["key"]] = request["inputs"]
        return [(True, None)] * len(requests)

    class Casting:
        @classmethod
        def INPUT_TYPES(cls):
            return {"required": {"n": ("INT", {}), "s": ("STRING", {}), "v": ("*",)}}

        @classmethod
        def VALIDATE_INPUTS(cls, **_kwargs):
            return True

    classes = dict(CLASSES, Casting=Casting)
    prompt = {
        "1": _control("x"),
        "10": _node("Casting", {"n": "4", "s": {"__value__": ["a", "b"]}, "v": ["1", 0]}),
    }
    cd.describe_controls(prompt, class_lookup=classes.get, validator_runner=runner)
    assert seen["10"] == {"n": 4, "s": "['a', 'b']", "v": ["1", 0]}


def test_one_descriptor_per_control_id_for_shared_subgraph_definitions():
    prompt = {
        "733:5": _control("20", kind="INT", control_id="shared01", title="Steps"),
        "733:9": _node("KSampler", {"steps": ["733:5", 0]}),
        "734:5": _control("20", kind="INT", control_id="shared01", title="Steps"),
        "734:9": _node("KSampler", {"steps": ["734:5", 0]}),
    }
    control = only_control(prompt)
    assert control["keys"] == ["733:5", "734:5"]
    assert [t["key"] for t in control["targets"]] == ["733:9", "734:9"]
    assert control["status"] == cd.STATUS_OK


def test_two_different_controls_sharing_an_id_are_flagged():
    prompt = {
        "1": _control("20", kind="INT", control_id="same0001"),
        "10": _node("KSampler", {"steps": ["1", 0]}),
        "2": _control("30", kind="INT", control_id="same0001"),
        "20": _node("KSampler", {"steps": ["2", 0]}),
    }
    control = only_control(prompt)
    assert control["status"] == cd.STATUS_DUPLICATE
    assert control["error"]


def test_controls_sort_by_panel_order_then_label():
    prompt = {
        "1": _control("a", control_id="c1", order=5, title="Zeta"),
        "2": _control("b", control_id="c2", order=0, title="beta"),
        "3": _control("c", control_id="c3", order=0, title="Alpha"),
    }
    assert [c["label"] for c in describe(prompt)["controls"]] == ["Alpha", "beta", "Zeta"]


def test_label_falls_back_and_group_is_trimmed():
    prompt = {"1": _control("a", group="  Advanced ")}
    control = only_control(prompt)
    assert control["label"] == cd.DEFAULT_CONTROL_LABEL
    assert control["group"] == "Advanced"


def test_describe_refuses_a_non_object_prompt():
    with pytest.raises(ValueError):
        cd.describe_controls(["not", "a", "prompt"], class_lookup=CLASSES.get, validator_runner=direct_runner)


# ── Panel metadata and HTTP transport ──────────────────────────────────────

def test_control_notes_and_seed_mode_are_optional_metadata():
    prompt = {"1": _control("20", "INT"), "2": _node("KSampler", {"steps": ["1", 0]})}
    before = only_control(prompt)
    assert before["note"] == "" and before["seed_mode"] is None
    prompt["1"]["inputs"].update(panel_note="**Recommended** 10–20", seed_mode="decrement")
    after = only_control(prompt)
    assert after["note"] == "**Recommended** 10–20" and after["seed_mode"] == "decrement"
    assert after["value"] == before["value"] == 20
    prompt["1"]["inputs"]["seed_mode"] = "unknown"
    assert only_control(prompt)["seed_mode"] is None


def test_notes_merge_shared_instances_and_keep_order_and_group():
    note = _node("SonderPanelNote", {"text": "# Instructions", "note_id": "shared01",
                                   "panel_order": -5, "panel_group": "  Advanced  "})
    result = describe({"733:5": note, "734:5": note, "9": _node("SonderPanelNote", {})})
    first, missing = result["notes"]
    assert first["keys"] == ["733:5", "734:5"] and first["text"] == "# Instructions"
    assert first["order"] == -5 and first["group"] == "Advanced" and first["status"] == "ok"
    assert missing["status"] == "missing_id" and missing["reason"]


@pytest.mark.parametrize("source, text_field, id_field", [
    ("SonderPanelNote", "text", "note_id"), ("SonderControl", "panel_note", "control_id"),
])
def test_note_truncation_is_read_only_and_different_tails_do_not_merge(source, text_field, id_field):
    import copy
    node = _control("20", "INT") if source == "SonderControl" else _node(source)
    node["inputs"].update({id_field: "shared01", text_field: "x" * 4001})
    prompt = {"733:5": node, "734:5": copy.deepcopy(node)}
    prompt["734:5"]["inputs"][text_field] += "different tail"
    before = copy.deepcopy(prompt)
    descriptor = describe(prompt)["controls" if source == "SonderControl" else "notes"][0]
    assert descriptor["status"] == "duplicate_id" and descriptor["note_truncated"] is True
    assert descriptor["note" if source == "SonderControl" else "text"] == "x" * 4000
    assert prompt == before


def test_note_at_limit_is_editable_and_missing_control_id_is_not():
    prompt = {"1": _control("20", "INT", control_id=""),
              "2": _node("SonderPanelNote", {"text": "x" * 4000, "note_id": "n1"})}
    result = describe(prompt)
    assert result["controls"][0]["status"] == "missing_id"
    assert "note_truncated" not in result["notes"][0]


def test_duplicate_control_panel_metadata_is_not_silently_chosen():
    import copy
    first = _control("20", "INT")
    second = copy.deepcopy(first)
    second["inputs"]["seed_mode"] = "fixed"
    assert only_control({"1": first, "2": second})["status"] == "duplicate_id"


def test_json_safe_copies_exact_integers_field_defaults_and_nonfinite_values():
    import json
    large = 2**63 + 123
    raw = {"controls": [{"source": "control", "kind": "SEED", "seed": large,
                          "value": large, "min": 0, "max": 2**64 - 1, "step": 1},
                         {"source": "control", "kind": "FLOAT", "value": 0.5,
                          "min": -math.inf, "max": math.inf}],
           "fields": [{"source": "field", "kind": "INT", "value": large,
                       "default": large, "min": 0, "max": large},
                      {"source": "field", "kind": "FLOAT", "default": 1, "min": 0,
                       "value": math.nan}], "other": [math.inf, {"n": math.nan, "enabled": True}]}
    safe = cd.json_safe(raw)
    encoded = json.loads(json.dumps(safe, allow_nan=False))
    seed = encoded["controls"][0]
    assert seed == {"source": "control", "kind": "SEED", "seed": str(large),
                    "value": str(large), "min": "0", "max": str(2**64 - 1), "step": "1"}
    assert encoded["controls"][1]["value"] == 0.5
    assert encoded["controls"][1]["min"] is None and encoded["controls"][1]["max"] is None
    assert encoded["fields"][0]["default"] == str(large)
    assert encoded["fields"][1]["default"] == "1" and encoded["fields"][1]["value"] is None
    assert encoded["other"] == [None, {"n": None, "enabled": True}]
    safe["controls"][0]["seed"] = "changed"
    assert raw["controls"][0]["seed"] == large and raw["controls"][0]["max"] == 2**64 - 1


@pytest.mark.parametrize("count", [2000, 2001])
def test_combo_transport_cap_preserves_full_runtime_validation(count):
    class LongCombo:
        @classmethod
        def INPUT_TYPES(cls):
            return {"required": {"option": ([f"opt-{i}" for i in range(count)],)}}
    classes = dict(CLASSES, LongCombo=LongCombo)
    prompt = {"1": _control(f"opt-{count-1}", "COMBO"),
              "2": _node("LongCombo", {"option": ["1", 0]})}
    raw = cd.describe_controls(prompt, class_lookup=classes.get, validator_runner=direct_runner)
    assert raw["controls"][0]["error"] is None and len(raw["controls"][0]["options"]) == count
    safe = cd.json_safe(raw)
    assert len(safe["controls"][0]["options"]) == 2000
    assert safe["controls"][0].get("options_truncated", False) == (count > 2000)
    assert cd.resolve_control_value(prompt, "1", kind="COMBO", value_text=f"opt-{count-1}",
                                    seed=0, class_lookup=classes.get) == f"opt-{count-1}"


def test_describe_route_uses_json_safe_without_changing_python_authority(monkeypatch):
    import json
    from server import routes
    original = cd.describe_controls
    monkeypatch.setattr(cd, "describe_controls", lambda prompt: original(prompt, class_lookup=CLASSES.get, validator_runner=direct_runner))
    prompt = {"1": _control("", "SEED", seed=2**63 + 7),
              "2": _node("KSampler", {"seed": ["1", 0]})}
    result = routes._describe_controls_body(json.dumps({"prompt": prompt}).encode("utf-8"))
    assert result["controls"][0]["seed"] == str(2**63 + 7)
    assert result["controls"][0]["value"] == str(2**63 + 7)
    assert result["controls"][0]["max"] == str(2**64 - 1)


# ── Registry fields ──────────────────────────────────────────────────────────

def test_registry_fields_only_for_nodes_reaching_an_editor():
    prompt = {
        "1": _node("SonderEditor"),
        "2": _node("SonderSaveVideo", {"project": ["1", 0], "mark_queue_complete": True, "place_audio_on_timeline": False}),
        "3": _node("SonderMasksBridge", {"project": ["1", 0], "edit_video": True, "edit_audio": False}),
        "4": _node("SonderSaveVideo", {"mark_queue_complete": True, "place_audio_on_timeline": True}),
    }
    result = describe(prompt)
    fields = {(f["key"], f["field"]): f for f in result["fields"]}
    assert set(fields) == {
        ("2", "mark_queue_complete"), ("2", "place_audio_on_timeline"),
        ("3", "edit_video"), ("3", "edit_audio"),
    }
    assert fields[("2", "mark_queue_complete")]["label"] == "Advance queue after take"
    assert fields[("2", "mark_queue_complete")]["value"] is True
    assert fields[("2", "place_audio_on_timeline")]["value"] is False
    assert fields[("3", "edit_audio")]["label_off"] == "Freeze"
    assert all(f["editor_key"] == "1" and f["status"] == cd.STATUS_OK for f in fields.values())
    assert result["editors"] == ["1"]


def test_linked_registry_field_defers_to_its_control_or_is_unsupported():
    prompt = {
        "1": _node("SonderEditor"),
        "5": _control("true", kind="BOOLEAN", title="Advance"),
        "2": _node("SonderSaveVideo", {"project": ["1", 0], "mark_queue_complete": ["5", 0], "place_audio_on_timeline": ["7", 0]}),
        "7": _node("SomeLogic"),
    }
    result = describe(prompt)
    fields = {f["field"]: f for f in result["fields"]}
    assert "mark_queue_complete" not in fields  # the Control describes it
    assert fields["place_audio_on_timeline"]["status"] == cd.STATUS_UNSUPPORTED
    assert result["controls"][0]["value"] is True


def test_registry_entries_are_data():
    assert {(e.class_type, e.field) for e in cd.FIELD_CONTROLS} == {
        ("SonderSaveVideo", "mark_queue_complete"),
        ("SonderSaveBridge", "mark_queue_complete"),
        ("SonderSaveVideo", "place_audio_on_timeline"),
        ("SonderMasksBridge", "edit_video"),
        ("SonderMasksBridge", "edit_audio"),
    }


# ── Execute-time self-check ──────────────────────────────────────────────────

def test_resolve_value_checks_kind_range_and_options_against_the_prompt():
    prompt = {
        "5": _control("30", kind="INT", title="Steps"),
        "9": _node("KSampler", {"steps": ["5", 0]}),
    }
    resolve = cd.resolve_control_value
    assert resolve(prompt, "5", kind="INT", value_text="30", seed=0, class_lookup=CLASSES.get) == 30
    with pytest.raises(cd.ControlValueError, match=r"Sonder Control 'Steps': 101 is above the maximum of 100 for KSampler steps\."):
        resolve(prompt, "5", kind="INT", value_text="101", seed=0, class_lookup=CLASSES.get)
    with pytest.raises(cd.ControlValueError, match="'Steps': It is set up for FLOAT but feeds KSampler steps, which takes INT"):
        resolve(prompt, "5", kind="FLOAT", value_text="3", seed=0, class_lookup=CLASSES.get)
    with pytest.raises(cd.ControlValueError, match="'Steps': The value is empty. Set a whole number."):
        resolve(prompt, "5", kind="INT", value_text="", seed=0, class_lookup=CLASSES.get)


def test_resolve_value_refuses_a_non_value_target_and_passes_unknown_ids():
    prompt = {"5": _control("x"), "9": _node("KSampler", {"model": ["5", 0]})}
    with pytest.raises(cd.ControlValueError, match="It cannot feed KSampler model"):
        cd.resolve_control_value(prompt, "5", kind="STRING", value_text="x", seed=0, class_lookup=CLASSES.get)
    # An id absent from the prompt (an expanded node) still converts its value.
    assert cd.resolve_control_value({}, "77", kind="BOOLEAN", value_text="on", seed=0, class_lookup=CLASSES.get) is True


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
