"""A Mode choice that includes another category's choice (0.8 hardening, Phase 6A).

The worked example in miniature: **Passes → Upscale** includes **Mask → Mask
Off**. Applying it sets both rows in one batch; it matches only while Mask Off
holds too; the rules (one level, other categories, no overlap, the 64-target
budget, an existing and usable included choice) read at projection and refuse
the writes that would break them. JS parity of the reader is pinned at the end.
"""

import copy
import json
import subprocess
from pathlib import Path

import pytest

from server import control_cleanup, control_descriptors as cd, control_manifest as cm
from server import control_preset_categories as pc, control_presets as cp
from test_control_descriptors import CLASSES, _binding, _node
from test_control_execution import member, switch


def choice(name, members, order=0, **extra):
    return {"name": name, "description": "", "order": order, "members": members, **extra}


def presets():
    return {"refine": choice("Refine", {"pass": {"value": "a"}}, 0),
            "upscale": choice("Upscale", {"pass": {"value": "b"}}, 1, includes=["mask_off"]),
            "mask_off": choice("Mask Off", {"mask": {"value": "a"}}, 2),
            "mask_char": choice("Mask Character", {"mask": {"value": "b"}}, 3)}


def categories():
    return {"passes": {"name": "Passes", "description": "", "order": 0, "choices": ["refine", "upscale"]},
            "masks": {"name": "Mask", "description": "", "order": 1, "choices": ["mask_off", "mask_char"]}}


def prompt_of(choices=None, groups=None, values=("a", "a"), execution=None):
    manifest = {"v": 1, "notes": {},
                "bindings": {"pass": _binding("p", "mode", label="Pass"), "mask": _binding("m", "mode", label="Mask mode"),
                             "amount": _binding("p", "amount", label="Amount"), "steps": _binding("6", "steps", label="Steps")},
                "presets": presets() if choices is None else choices,
                "preset_categories": categories() if groups is None else groups}
    if execution is not None:
        manifest["execution"] = {"v": 1, "controls": execution}
    return {"p": _node("OtherInputValidated", {"mode": values[0], "amount": 1.0}),
            "m": _node("OtherInputValidated", {"mode": values[1], "amount": 1.0}),
            "6": _node("KSampler", {"steps": 20, "seed": 5}, title="Pass 1"),
            "8": _node("KSampler", {"steps": 4}),
            "9": _node("SonderControlRelay", {"manifest": json.dumps(manifest)})}


def project(prompt):
    view = cd.describe_bindings(prompt, class_lookup=CLASSES.get, authoring={})
    view["presets"], view["preset_categories"] = cd.preset_projection(
        view, view["_presets"], view["_preset_categories"], prompt=prompt)
    return view


def rows(view):
    return {row["id"]: row for row in view["presets"]}


def write(view, prompt, changes, structural="canvas"):
    return cd.validate_binding_changes(view, changes, prompt=prompt, structural=structural)


# ── Reading ──────────────────────────────────────────────────────────────────

def test_includes_read_rules():
    base = choice("P", {"steps": {"value": "1"}})
    assert cm.read_presets({"p": base}) == {"p": base}
    assert cm.read_presets({"p": {**base, "includes": ["a", 3, "a", "constructor", "b c", "b"]}})["p"]["includes"] == ["a", "b"]
    # An empty list reads as none; anything but a list of at most 16 skips the choice (kept as saved).
    assert "includes" not in cm.read_presets({"p": {**base, "includes": []}})["p"]
    for bad in (None, "a", {"a": 1}, [f"x{i}" for i in range(17)], [3] * 17):
        assert cm.read_presets({"p": {**base, "includes": bad}}) == {}
    assert cm.read_presets({"p": {**base, "includes": [f"x{i}" for i in range(16)]}})["p"]["includes"][-1] == "x15"
    # Includes never count as a member: a choice keeps one of its own.
    assert cm.read_presets({"p": {**base, "members": {}, "includes": ["a"]}}) == {}


# ── Apply and matching ───────────────────────────────────────────────────────

def test_apply_sets_own_and_included_members_in_one_batch():
    prompt = prompt_of(values=("a", "b"))
    view = project(prompt)
    cleaned, rejected = write(view, prompt, {"preset:upscale:apply": True})
    assert not rejected and cleaned == {"binding:pass:value": "b", "binding:mask:value": "a"}
    # The Launcher's simulations apply a choice's own members only.
    own, _ = cp.validate_preset_changes(view, {"preset:upscale:apply": True}, prompt=prompt, structural="sdk", own_only=True)
    assert own == {"binding:pass:value": "b"}


def test_a_parent_matches_only_while_its_included_choice_holds():
    view = project(prompt_of(values=("b", "a")))
    states = {pid: row["state"] for pid, row in rows(view).items()}
    assert states == {"refine": "differs", "upscale": "match", "mask_off": "match", "mask_char": "differs"}
    modes = pc.derive_modes(view)["rows"]
    assert [(r["name"], r["status"], (r["selected"] or {}).get("id")) for r in modes] == [
        ("Passes", "selected", "upscale"), ("Mask", "selected", "mask_off")]
    # Mask changed by hand: the parent no longer matches and Passes reads Custom.
    view = project(prompt_of(values=("b", "b")))
    assert rows(view)["upscale"]["state"] == "differs"
    assert [r["status"] for r in pc.derive_modes(view)["rows"]] == ["custom", "selected"]


def test_rows_keep_own_members_and_list_includes():
    row = rows(project(prompt_of()))["upscale"]
    assert [m["binding_id"] for m in row["members"]] == ["pass"] and row["execution"] == []
    assert row["includes"] == [{"id": "mask_off", "name": "Mask Off", "status": "ok"}]
    assert "includes" not in rows(project(prompt_of()))["refine"]


# ── Problems at projection ───────────────────────────────────────────────────

def _with(change):
    choices = presets()
    change(choices)
    return choices


@pytest.mark.parametrize("change,groups,text", [
    (lambda c: c["upscale"].update(includes=["ghost"]), None, "no longer exists"),
    (lambda c: c["upscale"].update(includes=["upscale"]), None, "can't include itself"),
    (lambda c: c["mask_off"].update(includes=["mask_char"]) or c["upscale"].update(includes=["mask_off"]), None,
     "includes other choices itself"),
    (lambda c: c["upscale"].update(includes=["refine"]), None, "own category"),
    (lambda c: c["upscale"].update(includes=["mask_off", "mask_char"]), None, "are in one category"),
    (lambda c: c["mask_off"]["members"].update({"pass": {"value": "a"}}), None, "sets a Control this choice sets too"),
])
def test_each_problem_makes_the_parent_unavailable_with_its_reason(change, groups, text):
    row = rows(project(prompt_of(_with(change), groups)))["upscale"]
    assert row["state"] == "unavailable" and text in row["reason"]


def test_two_included_choices_that_overlap_and_the_target_budget():
    choices = presets()
    choices["extra"] = choice("Extra", {"mask": {"value": "a"}}, 4)
    choices["upscale"]["includes"] = ["mask_off", "extra"]
    row = rows(project(prompt_of(choices)))["upscale"]
    assert row["state"] == "unavailable" and "“Mask Off” and “Extra” set the same Control" in row["reason"]
    # 40 own targets plus 30 included: past 64 together, fine apart.
    choices = presets()
    choices["upscale"]["members"] = {f"own{i}": {"value": "1"} for i in range(40)}
    choices["mask_off"]["members"] = {f"inc{i}": {"value": "1"} for i in range(30)}
    problems = cp.inclusion_problems_of(choices, categories(), "upscale")
    assert [p["rule"] for p in problems] == ["budget"]


def test_switch_and_node_overlap_count_too():
    execution = {"a": switch(members=[member("6")]), "g": switch("group", [member("6"), member("8")], group_id=2)}
    choices = presets()
    choices["upscale"]["execution"] = {"a": {"mode": "muted", "node": {"path": "6", "class_type": "KSampler"}}}
    choices["mask_off"]["execution"] = {"g": {"modes": {"6": "muted", "8": "muted"}}}
    row = rows(project(prompt_of(choices, execution=execution)))["upscale"]
    assert row["state"] == "unavailable" and "sets a Control this choice sets too" in row["reason"]
    choices["mask_off"]["execution"] = {"a": {"mode": "muted", "node": {"path": "6", "class_type": "KSampler"}}}
    assert [p["rule"] for p in cp.inclusion_problems_of(choices, categories(), "upscale")] == ["overlap"]


def test_an_unavailable_included_choice_names_itself():
    prompt = prompt_of()
    prompt["m"] = _node("OtherInputValidated", {"mode": ["x", 0], "amount": 1.0})  # linked: Mask mode unavailable
    view = project(prompt)
    row = rows(view)["upscale"]
    assert row["state"] == "unavailable" and row["reason"].startswith("Included “Mask Off”: Unavailable Controls: Mask mode")
    assert row["includes"][0]["status"] == "unavailable"
    assert write(view, prompt, {"preset:upscale:apply": True})[1][0]["reason"].startswith("Included “Mask Off”:")


def test_an_included_choice_in_no_category_works():
    groups = categories()
    groups["masks"]["choices"] = ["mask_char"]
    prompt = prompt_of(groups=groups)
    assert rows(project(prompt))["upscale"]["state"] != "unavailable"
    assert write(project(prompt), prompt, {"preset:upscale:apply": True})[0]["binding:mask:value"] == "a"


def test_warnings_link_on_own_conditions_and_overlap_on_merged_ones():
    view = project(prompt_of())
    codes = {row["id"]: [w["code"] for w in row["warnings"]] for row in view["preset_categories"]}
    assert codes == {"passes": [], "masks": []}  # the inclusion links no rows
    # Same own settings, different inclusions: they never both match, so no overlap.
    choices = presets()
    choices["upscale2"] = choice("Upscale 2", {"pass": {"value": "b"}}, 5, includes=["mask_char"])
    groups = categories()
    groups["passes"]["choices"].append("upscale2")
    view = project(prompt_of(choices, groups))
    assert [w["code"] for w in view["preset_categories"][0]["warnings"]] == []
    choices["upscale2"].pop("includes")
    view = project(prompt_of(choices, groups))
    assert "overlap" in [w["code"] for w in view["preset_categories"][0]["warnings"]]


# ── Writes ───────────────────────────────────────────────────────────────────

def test_includes_write_create_and_edit_definitions():
    choices = presets()
    choices["upscale"].pop("includes")
    prompt = prompt_of(choices)
    view = project(prompt)
    cleaned, rejected = write(view, prompt, {"preset:upscale:includes": ["mask_off"]})
    assert not rejected and cleaned == {"preset:upscale:includes": ["mask_off"]}
    assert not cd.is_structural("preset:upscale:includes")
    definitions = copy.deepcopy(view["_presets"])
    cp.edit_definitions(definitions, "upscale", "includes", ["mask_off"])
    assert definitions["upscale"]["includes"] == ["mask_off"]
    cp.edit_definitions(definitions, "upscale", "includes", [])
    assert "includes" not in definitions["upscale"]
    created, rejected = write(view, prompt, {"preset:new:create": {"name": "New", "members": ["steps"], "includes": ["mask_off"]}})
    assert not rejected and created["preset:new:create"]["includes"] == ["mask_off"]
    for bad in ("mask_off", ["a"] * 2, [f"x{i}" for i in range(17)], ["constructor"]):
        assert write(view, prompt, {"preset:upscale:includes": bad})[1]


@pytest.mark.parametrize("changes,text", [
    ({"preset:upscale:includes": ["mask_off", "mask_char"]}, "are in one category"),
    ({"preset:upscale:includes": ["refine"]}, "own category"),
    ({"preset:new:create": {"name": "New", "members": ["mask"], "includes": ["mask_off"]}}, "sets a Control this choice sets too"),
    # Capturing the included side so it now sets the parent's setting.
    ({"preset:mask_off:capture": {"members": ["mask", "pass"]}}, "sets a Control this choice sets too"),
    # A category edit that puts the included choice in the parent's category.
    ({"preset_category:passes:choices": ["refine", "upscale", "mask_off"],
      "preset_category:masks:choices": ["mask_char"]}, "own category"),
    ({"preset:mask_off:includes": ["mask_char"]}, "includes other choices itself"),
])
def test_writes_that_make_a_problem_are_refused(changes, text):
    prompt = prompt_of()
    cleaned, rejected = write(project(prompt), prompt, changes)
    assert cleaned == {} and rejected and text in rejected[0]["reason"]


def test_a_category_move_or_remove_that_merges_parent_and_included_is_refused():
    groups = categories()
    # Both also sit in a later category: moving it first makes it their home.
    groups["both"] = {"name": "Both", "description": "", "order": 2, "choices": ["upscale", "mask_off"]}
    prompt = prompt_of(groups=groups)
    view = project(prompt)
    assert rows(view)["upscale"]["state"] != "unavailable"
    rejected = write(view, prompt, {"preset_category:both:move": {"before": "passes"}})[1]
    assert rejected and "own category" in rejected[0]["reason"]
    rejected = write(view, prompt, {"preset_category:passes:remove": True, "preset_category:masks:remove": True})[1]
    assert rejected and "own category" in rejected[0]["reason"]
    # Removing one category alone leaves them apart: accepted.
    assert not write(view, prompt, {"preset_category:masks:remove": True})[1]


def test_deleting_an_included_choice_is_refused_naming_its_parents():
    choices = presets()
    choices["upscale2"] = choice("Upscale 2", {"pass": {"value": "b"}, "steps": {"value": "3"}}, 5, includes=["mask_off"])
    prompt = prompt_of(choices)
    view = project(prompt)
    rejected = write(view, prompt, {"preset:mask_off:remove": True})[1]
    assert rejected == [{"target": "preset:mask_off:remove",
                         "reason": "“Upscale” and “Upscale 2” include this choice. Take it out there first."}]
    # Taken out first in the same batch: accepted.
    assert not write(view, prompt, {"preset:upscale:includes": [], "preset:upscale2:includes": [],
                                    "preset:mask_off:remove": True})[1]
    sdk = write(view, prompt, {"preset:mask_off:remove": True}, structural="sdk")[1]
    assert sdk and "Take it out there first" in sdk[0]["reason"]


def test_a_problem_already_there_never_blocks_an_unrelated_edit():
    choices = presets()
    choices["upscale"]["includes"] = ["ghost", "mask_off"]
    prompt = prompt_of(choices)
    view = project(prompt)
    assert not write(view, prompt, {"preset:refine:name": "Refine 2"})[1]
    assert not write(view, prompt, {"preset:upscale:name": "Upscale 2"})[1]
    assert not write(view, prompt, {"preset:upscale:capture": {"members": ["pass"]}})[1]
    # A reset can't be checked: it is accepted, and its problems read later.
    assert not write(view, prompt, {"preset:mask_off:reset": True}, structural="sdk")[1]


def test_mounted_relay_checks_shape_only():
    prompt = prompt_of()
    view = project(prompt)
    assert write(view, prompt, {"preset:upscale:includes": ["upscale"]}, structural="relay") == (
        {"preset:upscale:includes": ["upscale"]}, [])
    assert write(view, prompt, {"preset:upscale:includes": "x"}, structural="relay")[1]


def test_cleanup_drops_a_deleted_choice_from_every_includes():
    definitions = {"upscale": {"includes": ["gone", "mask_off"]}, "other": {"includes": ["gone"]}, "x": {"members": {}}}
    assert control_cleanup.drop_included(definitions, {"gone"}) == {"upscale", "other"}
    assert definitions == {"upscale": {"includes": ["mask_off"]}, "other": {}, "x": {"members": {}}}


# ── JS mirror ────────────────────────────────────────────────────────────────

def test_js_reader_writer_and_cleanup_mirror(tmp_path):
    root = Path(__file__).resolve().parents[1]
    base = choice("P", {"steps": {"value": "1"}})
    raw = {"ok": {**base, "includes": ["a", 3, "a", "constructor", "b"]}, "empty": {**base, "includes": []},
           "none": base, "bad": {**base, "includes": "a"}, "long": {**base, "includes": [3] * 17},
           "only": {**base, "members": {}, "includes": ["a"]}, "null": {**base, "includes": None}}
    script = f'''import assert from 'node:assert/strict';
    const b = await import({json.dumps((root / 'web/js/control_bindings.js').as_uri())});
    assert.deepEqual(b.readPresets({json.dumps(raw)}), {json.dumps(cm.read_presets(raw))});
    const m = {{presets: {json.dumps(presets())}}};
    b.applyPresetEdit(m, 'refine', 'includes', ['mask_off']);
    assert.deepEqual(m.presets.refine.includes, ['mask_off']);
    b.applyPresetEdit(m, 'refine', 'includes', []);
    assert.equal('includes' in m.presets.refine, false);
    b.applyCleanupPlan(m, {{preset_members: [{{preset_id: 'mask_off', binding_id: 'mask'}}]}});
    assert.equal(m.presets.mask_off, undefined);
    assert.equal('includes' in m.presets.upscale, false);
    '''
    path = tmp_path / "includes.mjs"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run(["node", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# ── Sonder App ───────────────────────────────────────────────────────────────

@pytest.fixture
def app(tmp_path, monkeypatch):
    import sys
    import types
    from server import sdk_templates as st
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(get_user_directory=lambda: str(tmp_path)))
    monkeypatch.setattr(cd, "_comfy_class_lookup", CLASSES.get)
    monkeypatch.setattr(cd, "comfy_class_lookup", CLASSES.get)
    choices = presets()
    choices["upscale"].pop("includes")
    prompt = prompt_of(choices, values=("a", "b"))
    identity = st.import_template(json.dumps(prompt).encode(), validate=lambda p: {"ok": True})["template_id"]

    def snapshot():
        stored = st.read_control_values(identity)
        current, _ = st.load_template(identity)
        return current, stored, st.apply_stored_values(current, st.describe_stored(current, stored), stored)

    def store(changes):
        current, _, projected = snapshot()
        cleaned, refused = cd.validate_binding_changes(projected, changes, prompt=current, structural="sdk")
        assert not refused, refused
        return st.store_binding_changes(identity, cleaned, projected, current)

    return st, identity, snapshot, store


def test_app_includes_edit_makes_a_copy_and_apply_is_one_mutation(app):
    st, identity, snapshot, store = app
    data = store({"preset:upscale:includes": ["mask_off"]})
    assert data["presets"]["upscale"]["includes"] == ["mask_off"]
    view = snapshot()[2]
    assert rows(view)["upscale"]["origin"] == "app_copy" and rows(view)["upscale"]["includes"][0]["id"] == "mask_off"
    seq = snapshot()[1]["seq"]
    store({"preset:upscale:apply": True})
    _, stored, view = snapshot()
    assert stored["seq"] == seq + 1
    assert {r["id"]: r["state"] for r in view["presets"]}["mask_off"] == "match"
    assert [r["status"] for r in pc.derive_modes(view)["rows"]] == ["selected", "selected"]
    # A later rename keeps the copy's inclusion.
    assert store({"preset:upscale:name": "Up"})["presets"]["upscale"]["includes"] == ["mask_off"]
    exported = cm.find_relay(json.loads(st.export_curated(identity)))["manifest"]
    assert exported["presets"]["upscale"]["includes"] == ["mask_off"]


def test_app_export_prunes_includes_to_exported_choices(app):
    st, identity, snapshot, store = app
    store({"preset:upscale:includes": ["mask_off"]})
    _, stored, view = snapshot()
    view["_presets"] = {pid: entry for pid, entry in view["_presets"].items() if pid != "mask_off"}
    current, _ = st.load_template(identity)
    manifest = st._curated_manifest(current, cm.find_relay(current), stored, view)
    assert "includes" not in manifest["presets"]["upscale"]
    assert snapshot()[2]["_presets"]["upscale"]["includes"] == ["mask_off"]  # the view's own copy is untouched


def test_app_delete_is_refused_and_cleanup_drops_the_id(app):
    st, identity, snapshot, store = app
    store({"preset:upscale:includes": ["mask_off"]})
    current, _, view = snapshot()
    assert cd.validate_binding_changes(view, {"preset:mask_off:remove": True}, prompt=current, structural="sdk")[1]

    # An app-only choice whose one setting is gone, included by Upscale.
    def plant(data):
        data["presets"]["temp"] = choice("Temp", {"gone": {"value": "a"}}, 9)
        data["presets"]["upscale"]["includes"] = ["temp", "mask_off"]
    st.update_control_values(identity, plant)
    view = snapshot()[2]
    assert rows(view)["upscale"]["state"] == "unavailable"
    offered = [row for row in view["cleanup"]["preset_members"] if row.get("preset_id") == "temp"]
    assert offered
    store({"workflow:cleanup": {"preset_members": [{"preset_id": "temp", "binding_id": "gone"}]}})
    data = st.read_control_values(identity)
    assert data["presets"]["temp"] == {"removed": True}
    assert data["presets"]["upscale"]["includes"] == ["mask_off"]


# ── Audit follow-ups ─────────────────────────────────────────────────────────

def test_a_reset_skips_only_its_own_problems():
    prompt = prompt_of()
    view = project(prompt)
    # The includes write is still checked beside an unrelated reset.
    rejected = write(view, prompt, {"preset:mask_char:reset": True, "preset:upscale:includes": ["refine"]}, structural="sdk")[1]
    assert rejected and "own category" in rejected[0]["reason"]
    # A problem naming the reset choice is left to show at read.
    assert not write(view, prompt, {"preset:mask_off:reset": True}, structural="sdk")[1]


def test_a_category_problem_still_counts_the_choice_for_overlap():
    choices = presets()
    choices["upscale"]["includes"] = ["refine"]  # same category, and both set Pass
    rules = [p["rule"] for p in cp.inclusion_problems_of(choices, categories(), "upscale")]
    assert rules == ["category", "overlap"]
