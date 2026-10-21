"""Row-group Controls: a node's dynamic row group as one ``ROWS`` Control.

The fake finalization mirrors ComfyUI's ``_DynamicGroup._expand_schema_for_dynamic``
(comfy_api/latest/_io.py, ComfyUI 0.40): the finalized schema holds only the
flat ``<group>.<i>.<field>`` keys of the rows present, and finalizing outside
min..max raises. Without it ``_class_inputs`` would return the base schema and
these tests would pass against a schema the real runtime never shows.
Filenames are synthetic.
"""

import asyncio
import copy
import json
import sys
import types

import pytest

from server import control_descriptors as cd
from server import control_manifest as cm
from server import control_presets, control_rows, controls_snapshot, model_requirements as mr, sdk_templates
from server import session_registry
# Mounted transport fixtures; `registry` is autouse and resets only the registry.
from test_controls_relay import CANVAS, HID, PID, TAB, publication, registry, setup  # noqa: F401 (fixture)

LORAS = ["sonder_test_a.safetensors", "sonder_test_b.safetensors", "sonder_test_c.safetensors"]


class _Internal:
    pass


def _template():
    return {"required": {
        "lora_name": ("COMBO", {"options": list(LORAS)}),
        "strength": ("FLOAT", {"default": 1.0, "min": -100.0, "max": 100.0, "step": 0.01}),
        "enabled": ("BOOLEAN", {"default": True}),
    }}


class LoadLoraModel(_Internal):
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": ("MODEL",),
            "loras": ("COMFY_DYNAMICGROUP_V3", {"template": _template(), "min": 1, "max": 20, "group_name": "LoRA"}),
        }}


class SeedRows(_Internal):
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"rows": ("COMFY_DYNAMICGROUP_V3", {
            "template": {"required": {"seed": ("INT", {"min": 0, "max": 10})}}, "min": 0, "max": 3})}}


class OptionalRows(_Internal):
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"rows": ("COMFY_DYNAMICGROUP_V3", {
            "template": {"required": {"a": ("INT", {})}, "optional": {"b": ("INT", {})}}, "min": 0, "max": 3})}}


class Sink:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"model": ("MODEL",)}}


CLASSES = {"LoadLoraModel": LoadLoraModel, "SeedRows": SeedRows, "OptionalRows": OptionalRows, "Sink": Sink,
           "SonderControlRelay": Sink}


def lookup(class_type):
    return CLASSES.get(class_type)


def _finalize(schema, live):
    """ComfyUI's dynamic-group finalization, reduced to what Controls reads."""
    out = {"required": {}, "optional": {}}
    for category in ("required", "optional"):
        for name, info in (schema.get(category) or {}).items():
            if info[0] != "COMFY_DYNAMICGROUP_V3":
                out[category][name] = info
                continue
            extra = info[1]
            fields = {**extra["template"].get("required", {}), **extra["template"].get("optional", {})}
            present = set()
            for key in live:
                if key.startswith(name + "."):
                    index, _, field = key[len(name) + 1:].partition(".")
                    if not index.isdecimal() or field not in fields:
                        raise ValueError(f"Invalid DynamicGroup input key '{key}'")
                    present.add(int(index))
            if not extra.get("min", 0) <= len(present) <= extra.get("max", 20):
                raise ValueError(f"DynamicGroup input '{name}' received {len(present)} rows")
            for row in sorted(present):
                for field, spec in fields.items():
                    out[category][f"{name}.{row}.{field}"] = spec
    return out, None, {}


@pytest.fixture(autouse=True)
def finalization(monkeypatch, tmp_path):
    internal = types.ModuleType("comfy_api.internal")
    internal._ComfyNodeInternal = _Internal
    internal.first_real_override = lambda cls, name: None
    monkeypatch.setitem(sys.modules, "comfy_api.internal", internal)
    monkeypatch.setitem(sys.modules, "comfy_api.latest",
                        types.SimpleNamespace(_io=types.SimpleNamespace(get_finalized_class_inputs=_finalize)))
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(
        get_user_directory=lambda: str(tmp_path), folder_names_and_paths={}, get_filename_list=lambda key: []))


def row(name="sonder_test_a.safetensors", strength=1.0, enabled=True):
    return {"lora_name": name, "strength": strength, "enabled": enabled}


def flat(rows, group="loras"):
    inputs = {}
    control_rows.apply_rows(inputs, group, rows)
    return inputs


def relay(bindings=None, presets=None):
    manifest = {"v": 1, "bindings": bindings or {}, "notes": {}}
    if presets is not None:
        manifest["presets"] = presets
    return {"class_type": "SonderControlRelay", "inputs": {"manifest": json.dumps(manifest)}}


def binding(path="2", name="loras", **extra):
    return {"label": "", "status": "ok", "targets": [{"path": path, "input": name}], **extra}


def prompt(rows=(row(),), bindings=None, presets=None, title="Load LoRA (Model)"):
    return {
        "1": {"class_type": "Sink", "inputs": {"model": ["9", 0]}},
        "2": {"class_type": "LoadLoraModel", "inputs": {"model": ["1", 0], **flat(list(rows))},
              "_meta": {"title": title}},
        "3": {"class_type": "Sink", "inputs": {"model": ["2", 0]}},
        "8": relay({"stack": binding()} if bindings is None else bindings, presets),
    }


def describe(p, **kwargs):
    return cd.describe_bindings(p, class_lookup=lookup, **kwargs)


def stack(view):
    return next(d for d in view["bindings"] if d["binding_id"] == "stack")


# ── The row authority ────────────────────────────────────────────────────────

def test_spec_comes_from_the_base_schema_template():
    spec, reason = control_rows.build_spec(LoadLoraModel.INPUT_TYPES()["required"]["loras"][1], "loras")
    assert reason is None
    assert (spec["kind"], spec["group_name"], spec["min"], spec["max"]) == ("ROWS", "LoRA", 1, 20)
    assert [f["id"] for f in spec["fields"]] == ["lora_name", "strength", "enabled"]
    assert [f["kind"] for f in spec["fields"]] == ["COMBO", "FLOAT", "BOOLEAN"]
    assert spec["new_row"] == {"lora_name": LORAS[0], "strength": 1.0, "enabled": True}


def test_seed_fields_are_plain_ints_and_optional_fields_are_refused():
    spec, _ = control_rows.build_spec(SeedRows.INPUT_TYPES()["required"]["rows"][1], "rows")
    assert spec["fields"][0]["kind"] == "INT" and spec["group_name"] == "rows"
    spec, reason = control_rows.build_spec(OptionalRows.INPUT_TYPES()["required"]["rows"][1], "rows")
    assert spec is None and "optional" in reason


def test_rows_read_in_index_order_dropping_gaps_and_flag_links():
    inputs = {"loras.3.lora_name": "c", "loras.3.strength": 0.5, "loras.3.enabled": False,
              "loras.0.lora_name": "a", "loras.0.strength": 1, "loras.0.enabled": True, "other": 1}
    rows, linked = control_rows.rows_from_inputs(inputs, "loras", ["lora_name", "strength", "enabled"])
    assert [r["lora_name"] for r in rows] == ["a", "c"] and not linked
    inputs["loras.0.strength"] = ["7", 0]
    assert control_rows.rows_from_inputs(inputs, "loras", ["lora_name", "strength", "enabled"])[1]


def test_apply_rows_replaces_every_flat_key():
    inputs = {"model": ["1", 0], **flat([row(), row(LORAS[1]), row(LORAS[2])])}
    control_rows.apply_rows(inputs, "loras", [row(LORAS[2], 0.25, False)])
    assert inputs == {"model": ["1", 0], "loras.0.lora_name": LORAS[2], "loras.0.strength": 0.25,
                      "loras.0.enabled": False}


def test_canonical_ignores_key_order_and_integer_floats():
    assert control_rows.canonical([{"b": 1.0, "a": True}]) == control_rows.canonical([{"a": True, "b": 1}])
    assert control_rows.canonical([{"a": 0.5}]) != control_rows.canonical([{"a": 0.25}])


def test_summary_counts_rows_and_enabled():
    spec = stack(describe(prompt()))
    assert control_rows.summary([row(), row(enabled=False), row()], spec) == "3 LoRAs · 2 on"
    assert control_rows.summary([row()], spec) == "1 LoRA · 1 on"
    assert control_rows.summary([{"seed": 1}], {"group_name": "rows", "fields": [{"id": "seed", "kind": "INT"}]}) == "1 rows"


# ── Describe ─────────────────────────────────────────────────────────────────

def test_a_group_binding_describes_as_one_rows_control():
    view = describe(prompt([row(), row(LORAS[1], 0.5, False)]))
    d = stack(view)
    assert (d["status"], d["kind"], d["error"]) == ("ok", "ROWS", None)
    assert d["label"] == "Load LoRA (Model) · LoRAs"
    assert d["value"] == [row(), row(LORAS[1], 0.5, False)]
    assert d["fields"][0]["options"] == LORAS and d["min"] == 1 and d["max"] == 20


def test_zero_rows_stay_ok_with_an_error_so_the_app_can_store_rows():
    # A frontend without rows (1.55) exports none; finalization would raise.
    d = stack(describe(prompt([])))
    assert d["status"] == "ok" and d["value"] == [] and d["error"] == "Add at least 1 LoRA."


def test_a_row_value_the_target_refuses_is_reported_not_hidden():
    d = stack(describe(prompt([row("gone.safetensors")])))
    assert d["status"] == "ok" and "LoRA 1 lora_name" in d["error"] and d["value"][0]["lora_name"] == "gone.safetensors"


def test_a_single_row_field_binding_is_refused():
    d = stack(describe(prompt(bindings={"stack": binding(name="loras.0.strength")})))
    assert d["status"] == "unsupported" and "expose “loras”" in d["reason"]
    # The canvas exports it as unsupported without a reason; the server names the group.
    d = stack(describe(prompt(bindings={"stack": binding(name="loras.0.strength", status="unsupported")})))
    assert d["status"] == "unsupported" and "expose “loras”" in d["reason"]


def test_a_linked_row_field_makes_the_group_linked():
    p = prompt()
    p["2"]["inputs"]["loras.0.strength"] = ["1", 0]
    assert stack(describe(p))["status"] == "linked"


def test_a_row_list_sets_one_node_only():
    p = prompt(bindings={"stack": {"label": "", "status": "ok", "targets": [
        {"path": "2", "input": "loras"}, {"path": "4", "input": "loras"}]}})
    p["4"] = copy.deepcopy(p["2"])
    assert stack(describe(p))["status"] == "unsupported"


# ── Candidates ───────────────────────────────────────────────────────────────

def test_sdk_candidates_list_the_group_once_instead_of_its_fields():
    names = [(c["path"], c["input"]) for c in cd.sdk_candidates(prompt([row(), row()]), lookup)]
    assert ("2", "loras") in names and not any("." in name for _, name in names)
    # A template exported by a frontend without rows still offers the group.
    assert ("2", "loras") in [(c["path"], c["input"]) for c in cd.sdk_candidates(prompt([]), lookup)]


def test_the_app_runner_offers_the_group_from_comfyuis_registry(monkeypatch):
    # sdk_routes builds the runner without a class lookup; + Setting must still see the group.
    from server import sdk_runner

    monkeypatch.setattr(cd, "_comfy_class_lookup", lookup)
    runner = sdk_runner.SdkRunner()
    try:
        inventory = runner._inventory(prompt([], bindings={}))
    finally:
        runner.close()
    entry = next(e for e in inventory if (e["path"], e["input"]) == ("2", "loras"))
    assert (entry["status"], entry["kind"]) == ("ok", "ROWS")


def test_inventory_offers_the_group_and_refuses_fields():
    p = prompt(bindings={})
    inventory = cd.candidate_inventory(p, [{"path": "2", "input": "loras", "targets": [{"path": "2", "input": "loras"}]},
                                           {"path": "2", "input": "loras.0.strength",
                                            "targets": [{"path": "2", "input": "loras.0.strength"}]}],
                                       class_lookup=lookup)
    assert (inventory[0]["status"], inventory[0]["kind"], inventory[0]["label"]) == ("ok", "ROWS", "Load LoRA (Model) · LoRAs")
    assert inventory[1]["status"] == "unsupported"


# ── Changes ──────────────────────────────────────────────────────────────────

def test_a_row_change_is_validated_against_its_base():
    view = describe(prompt([row()]))
    rows = [row(), row(LORAS[1], 0.5)]
    cleaned, rejected = cd.validate_binding_changes(view, {"binding:stack:value": {"rows": rows, "base": [row(strength=1)]}})
    assert not rejected and cleaned["binding:stack:value"] == rows
    cleaned, rejected = cd.validate_binding_changes(view, {"binding:stack:value": {"rows": rows, "base": []}})
    assert not cleaned and "changed" in rejected[0]["reason"]


def test_queued_mounted_row_edits_carry_their_base_to_the_canvas(registry):
    # Two quick edits from a mounted tab: the second starts from the first,
    # which the published view does not show yet. The pre-check must not
    # refuse it, and must not drop the base the canvas checks when it lands.
    async def run():
        await setup()
        body = publication()
        body["view"] = cd.json_safe(describe(prompt([row()])))
        assert (await session_registry.publish_controls_view(PID, HID, CANVAS, body))["ok"]
        first, second = [row(), row(LORAS[1])], [row(), row(LORAS[1], 0.5)]
        for rows, base in ((first, [row()]), (second, first)):
            queued = await session_registry.post_control_changes(
                PID, HID, TAB, {"binding:stack:value": {"rows": rows, "base": base}})
            assert queued["ok"], queued
        pending = (await session_registry.get_control_changes(PID, HID, CANVAS))["pending"]
        changes = [item["changes"]["binding:stack:value"] for item in pending]
        assert changes == [{"rows": first, "base": [row()]}, {"rows": second, "base": first}]
        # The canvas writer's fresh check: the second applies once the first
        # landed, and is refused if something else changed the rows meanwhile.
        assert not cd.validate_binding_changes(describe(prompt(first)), pending[1]["changes"])[1]
        _, rejected = cd.validate_binding_changes(describe(prompt([row(LORAS[2])])), pending[1]["changes"])
        assert "changed" in rejected[0]["reason"]
        # Its rows are still checked before they are queued.
        refused = await session_registry.post_control_changes(
            PID, HID, TAB, {"binding:stack:value": {"rows": [], "base": first}})
        assert refused["code"] == "invalid_change" and "at least 1" in refused["rejected"][0]["reason"]

    asyncio.run(run())


@pytest.mark.parametrize("rows, reason", [
    ([], "at least 1"),
    ([row()] * 21, "At most 20"),
    ([row("nope.safetensors")], "not one of the options"),
    ([row(strength=101)], "above the maximum"),
    ([{"lora_name": LORAS[0], "strength": 1.0}], "must set"),
    ([row(enabled="yes")], "on or off"),
])
def test_row_changes_are_refused_per_field(rows, reason):
    view = describe(prompt())
    _, rejected = cd.validate_binding_changes(view, {"binding:stack:value": rows})
    assert reason in rejected[0]["reason"]


def test_a_truncated_publication_skips_membership_and_takes_text_bounds():
    view = cd.json_safe(describe(prompt()))
    field = view["bindings"][0]["fields"][0]
    field["options"], field["options_truncated"] = LORAS[:1], True
    cleaned, rejected = cd.validate_binding_changes(view, {"binding:stack:value": [row(LORAS[2])]})
    assert not rejected
    session_registry._prepare_control_publication({"view_seq": 1, "prompt_hash": "h", "view": view})
    broken = copy.deepcopy(view)
    broken["bindings"][0]["fields"] = [{"id": 1}]
    with pytest.raises(ValueError):
        session_registry._prepare_control_publication({"view_seq": 1, "prompt_hash": "h", "view": broken})


# ── Sonder App ───────────────────────────────────────────────────────────────

def _sdk_describe(p, **kwargs):
    return describe(p, **kwargs)


def test_stored_rows_replace_the_templates_rows_and_mark_overridden():
    p = prompt([row(), row(LORAS[1]), row(LORAS[2])])
    stored_rows = [row(LORAS[2], 0.5, False)]
    view = describe(p)
    stored = {"bindings": {"stack": {"value": stored_rows, "sig": cd.binding_signature(stack(view))}}}
    result = sdk_templates.apply_stored_values(p, view, stored, describe=_sdk_describe)
    assert {k: v for k, v in p["2"]["inputs"].items() if k.startswith("loras.")} == flat(stored_rows)
    assert stack(result)["value"] == stored_rows and stack(result)["overridden"] is True


def test_stored_rows_fill_a_template_without_rows():
    p = prompt([])
    view = describe(p)
    stored = {"bindings": {"stack": {"value": [row()], "sig": cd.binding_signature(stack(view))}}}
    result = sdk_templates.apply_stored_values(p, view, stored, describe=_sdk_describe)
    assert stack(result)["error"] is None and p["2"]["inputs"]["loras.0.lora_name"] == LORAS[0]


def test_stored_rows_a_target_refuses_reset_to_the_templates():
    p = prompt([row()])
    view = describe(p)
    stored = {"bindings": {"stack": {"value": [row("gone.safetensors")], "sig": cd.binding_signature(stack(view))}}}
    result = sdk_templates.apply_stored_values(p, view, stored, describe=_sdk_describe)
    assert stack(result)["reset"] is True and p["2"]["inputs"]["loras.0.lora_name"] == LORAS[0]


# ── Presets and the manifest ─────────────────────────────────────────────────

def test_manifest_depth_fits_a_preset_row_list_exactly():
    presets = {"look": {"name": "Look", "order": 0, "members": {"stack": {"value": [row()]}}}}
    parsed = cm.parse_manifest(json.dumps({"v": 1, "bindings": {"stack": {}}, "presets": presets}))
    assert parsed["presets"]["look"]["members"]["stack"]["value"] == [row()]
    deeper = {"look": {"name": "Look", "order": 0, "members": {"stack": {"value": [{"a": {"b": 1}}]}}}}
    with pytest.raises(cm.ManifestError, match="nested too deeply"):
        cm.parse_manifest(json.dumps({"v": 1, "bindings": {}, "presets": deeper}))


def test_preset_members_accept_only_flat_row_lists():
    assert control_rows.value_shape_ok([row()])
    assert not control_rows.value_shape_ok([row()] * 21)
    assert not control_rows.value_shape_ok([{"a": [1]}])
    read = cm.read_presets({"p": {"name": "P", "members": {"a": {"value": [row()]}, "b": {"value": [[1]]}}}})
    assert set(read["p"]["members"]) == {"a"}


def test_presets_capture_match_and_preview_row_lists():
    view = describe(prompt([row(), row(LORAS[1], 0.5, False)]))
    captured = control_presets._capture(view, ["stack"])
    assert captured["stack"]["value"] == [row(), row(LORAS[1], 0.5, False)]
    presets = {"look": {"name": "Look", "description": "", "order": 0, "members": captured}}
    state = cd.preset_states(view, presets)[0]
    assert state["state"] == "match" and state["members"][0]["preview"] == "2 LoRAs · 1 on"
    other = describe(prompt([row()]))
    assert cd.preset_states(other, presets)[0]["state"] == "differs"
    assert state["members"][0]["differs"] is False
    # Two lists with one summary still differ: Manage's hint reads this flag.
    same_summary = describe(prompt([row(LORAS[2]), row(LORAS[1], 0.25, False)]))
    member = cd.preset_states(same_summary, presets)[0]["members"][0]
    assert member["preview"] == "2 LoRAs · 1 on" and member["differs"] is True


# ── Provenance and models ────────────────────────────────────────────────────

def test_controls_at_run_records_native_rows_once_without_a_collector():
    section = controls_snapshot.controls_snapshot(prompt([row(), row(LORAS[1], 0.5, False)]), ["3"],
                                                  class_lookup=lookup)
    label = "Load LoRA (Model) · LoRAs"
    assert section["fields"][label] == [
        {"slot": 1, "name": LORAS[0], "enabled": True, "strength": 1.0},
        {"slot": 2, "name": LORAS[1], "enabled": False, "strength": 0.5, "skipped": True}]
    record = section["controls"][0]
    assert record["display_type"] == "native_lora_stack" and record["kind"] == "ROWS"
    assert not {"rows", "value"} & record.keys()
    assert not {"full_fields", "raw_widget_text"} & section.keys()


def test_a_missing_row_lora_names_the_loras_folder(monkeypatch):
    p = prompt([row("absent.safetensors")])
    missing = mr.missing_models(p, lookup)
    assert missing and missing[0]["folder"] == "loras" and missing[0]["input"] == "loras.0.lora_name"


def test_controls_at_run_marks_a_native_stacks_rows_as_control_set():
    tracked = [{"label": "Load LoRA (Model)", "display_type": "native_lora_stack", "source_node_id": "2",
                "source_group": "loras", "fields": {"power_loras": [], "total_lora_count": 0}},
               {"label": "Power", "display_type": "power_loras", "source_node_id": "2", "fields": {"power_loras": []}}]
    section = controls_snapshot.controls_snapshot(prompt([row()]), ["3"], tracked=tracked, class_lookup=lookup)
    assert section["supplied"] == [{"section_index": 0, "field": "power_loras", "binding_id": "stack"}]


@pytest.mark.parametrize("class_type", ["LoadLoraModel", "LoadLoraTextEncoder"])
def test_native_snapshot_preserves_executed_literals_despite_options_and_frontend_hints(monkeypatch, class_type):
    monkeypatch.setitem(CLASSES, "LoadLoraTextEncoder", LoadLoraModel)
    p = prompt([row("folder/模型 % old.safetensors", -0.75), row(LORAS[1], 0), row(LORAS[2], 1, False), row("")],
               bindings={"stack": binding(rows_unavailable=True)})
    p["2"]["class_type"] = class_type
    # Sparse flattened indices still preserve prompt order.
    p["2"]["inputs"] = {key.replace("loras.1.", "loras.8."): value for key, value in p["2"]["inputs"].items()}
    d = stack(describe(p))
    assert d["rows_unavailable"] is True and d["error"]
    section = controls_snapshot.controls_snapshot(p, ["3"], class_lookup=lookup)
    rows = next(iter(section["fields"].values()))
    assert [(r["slot"], r["name"], r.get("skipped", False)) for r in rows] == [
        (1, "folder/模型 % old.safetensors", False), (2, LORAS[2], True), (3, LORAS[1], True)]
    p["2"]["inputs"]["loras.0.strength"] = 99
    assert rows[0]["strength"] == -0.75


@pytest.mark.parametrize("count", [0, 20])
def test_native_snapshot_handles_empty_and_full_stacks(count):
    p = prompt([row(f"style_{i}.safetensors", i / 10) for i in range(count)])
    section = controls_snapshot.controls_snapshot(p, ["3"], class_lookup=lookup)
    assert len(next(iter(section["fields"].values()))) == count
    assert section["controls"][0]["display_type"] == "native_lora_stack"


def test_native_snapshot_keeps_long_names_and_roundtrips():
    name = "folder/" + "é" * 700 + ".safetensors"
    section = controls_snapshot.controls_snapshot(prompt([row(name)]), ["3"], class_lookup=lookup)
    assert next(iter(json.loads(json.dumps(section))["fields"].values()))[0]["name"] == name
    from server.lora_metadata import native_lora_fields
    assert next(iter(section["fields"].values())) == native_lora_fields(flat([row(name)]))["power_loras"]


def test_oversized_first_stack_shrinks_before_it_evicts_later_controls():
    p = prompt([row("界" * 6000)], bindings={"stack": binding(label="First", order=0),
                                          "seed": binding(path="4", name="rows", label="Later", order=1)})
    p["4"] = {"class_type": "SeedRows", "inputs": flat([{"seed": 2}], "rows")}
    p["3"]["inputs"]["other"] = ["4", 0]
    section = controls_snapshot.controls_snapshot(p, ["3"], class_lookup=lookup)
    assert set(section["fields"]) == {"First", "Later"} and "truncated" not in section
    first = next(c for c in section["controls"] if c["binding_id"] == "stack")
    assert first["rows_omitted"] is True and "display_type" not in first
    assert section["fields"]["First"] == "1 LoRA · 1 on"
    assert isinstance(section["fields"]["Later"], str)
    assert len(json.dumps(section, ensure_ascii=False, separators=(",", ":")).encode()) <= controls_snapshot.MAX_SECTION_BYTES


def test_new_malformed_native_fallback_marks_clipped_text():
    p = prompt([row("x" * 1000, float("nan"))])
    section = controls_snapshot.controls_snapshot(p, ["3"], class_lookup=lookup)
    value = next(iter(section["fields"].values()))
    assert value.endswith(controls_snapshot.TRUNCATED_MARKER)
    assert "display_type" not in section["controls"][0]


def test_multiple_stacks_with_duplicate_control_labels_preserve_each_value():
    p = prompt([row()], bindings={"stack": binding(label="Stack"), "second": binding(path="4", label="Stack")})
    p["4"] = {"class_type": "LoadLoraModel", "inputs": {"model": ["2", 0], **flat([row(LORAS[1], 0.5)])}}
    p["3"]["inputs"]["model"] = ["4", 0]
    section = controls_snapshot.controls_snapshot(p, ["3"], class_lookup=lookup)
    assert len(section["fields"]) == 2
    assert {rows[0]["name"] for rows in section["fields"].values()} == set(LORAS[:2])
    assert len({c["field"] for c in section["controls"]}) == 2


@pytest.mark.parametrize("bad", [None, {}, [None], [row(strength=True)], [row(strength=float("inf"))],
                                  [row(enabled=1)], [row(name=4)], [row(strength=10 ** 1000)]])
def test_native_capture_refuses_unrepresentable_rows(bad):
    from server.lora_metadata import captured_native_lora_rows
    assert captured_native_lora_rows(bad) is None



def test_native_stack_near_byte_cap_reserves_the_final_truncation_count():
    # Fill the one-Control envelope to just below the cap, including UTF-8 bytes.
    p = prompt([row("x")], bindings={"stack": binding(label="Stack")})
    base = controls_snapshot.controls_snapshot(p, ["3"], class_lookup=lookup)
    encoded = lambda section: len(json.dumps(section, ensure_ascii=False, separators=(",", ":")).encode())
    name = "x" * (controls_snapshot.MAX_SECTION_BYTES - encoded(base) - 3)
    p["2"]["inputs"]["loras.0.lora_name"] = name
    alone = controls_snapshot.controls_snapshot(p, ["3"], class_lookup=lookup)
    assert alone["controls"][0]["display_type"] == "native_lora_stack"
    manifest = json.loads(p["8"]["inputs"]["manifest"])
    manifest["bindings"]["later"] = binding(path="4", name="rows", label="Later", order=10)
    p["8"]["inputs"]["manifest"] = json.dumps(manifest)
    p["4"] = {"class_type": "SeedRows", "inputs": flat([{"seed": 2}], "rows")}
    p["3"]["inputs"]["other"] = ["4", 0]
    section = controls_snapshot.controls_snapshot(p, ["3"], class_lookup=lookup)
    assert section is not None and encoded(section) <= controls_snapshot.MAX_SECTION_BYTES
    assert set(section["fields"]) == {"Stack", "Later"} and "truncated" not in section
    assert section["controls"][0]["rows_omitted"] is True
