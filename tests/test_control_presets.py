"""Preset contracts at parser, validation, overlay and publication boundaries."""
import copy
import json
import subprocess
from pathlib import Path

import pytest

from server import control_descriptors as cd, control_manifest as cm, control_presets as cp


def definition(members=None, **extra):
    return {"name": "Detailing", "description": "Captured settings", "order": 0,
            "members": members or {"steps": {"value": "20"}}, **extra}


def view(preset=None):
    return {"relay": {"status": "ok"}, "notes": [], "bindings": [
        {"binding_id": "steps", "label": "Steps", "kind": "INT", "status": "ok", "value": 20, "min": 1, "max": 100},
        {"binding_id": "seed", "label": "Seed", "kind": "SEED", "status": "ok", "value": 7, "seed_mode": "randomize", "min": 0},
        {"binding_id": "text", "label": "Prompt", "kind": "STRING", "status": "ok", "value": "{a|b}"},
        {"binding_id": "choice", "label": "Model", "kind": "COMBO", "status": "ok", "value": "a", "options": ["a", "b"]},
    ], "_presets": {"p": preset or definition()}}


def validate(v, field, raw, **kwargs):
    return cd.validate_binding_changes(v, {f"preset:p:{field}": raw}, structural="canvas", **kwargs)


def test_tolerant_read_and_raw_hygiene():
    raw = {"bad": {"name": 1}, "p": definition({"steps": {"value": "20"}, "bad": {"seed_mode": "oops"}})}
    original = copy.deepcopy(raw)
    assert cm.read_presets(raw) == {"p": definition()}
    assert raw == original
    assert cm.parse_manifest(json.dumps({"presets": raw}))["presets"] == {"p": definition()}
    assert cm.read_presets([]) == {}
    for value, message in [({"presets": {"constructor": {}}}, "reserved"),
                           ({"presets": {"p": definition({"steps": {"value": [{"deep": {"x": 1}}]}})}}, "deeply")]:
        with pytest.raises(cm.ManifestError, match=message):
            cm.parse_manifest(json.dumps(value))


def test_caps_count_valid_entries_and_seed_targets():
    raw = {f"bad{i}": {} for i in range(40)} | {f"p{i}": definition() for i in range(33)}
    assert len(cm.read_presets(raw)) == 32
    assert "p31" in cm.read_presets(raw)
    for count, seeded, expected in [(64, False, True), (65, False, False), (32, True, True), (33, True, False)]:
        members = {f"b{i}": {"value": "0", **({"seed_mode": "fixed"} if seeded else {})} for i in range(count)}
        assert bool(cm.read_presets({"p": definition(members)})) is expected


@pytest.mark.parametrize("source,field,want", [("preset", "create", True), ("preset", "capture", True),
    ("preset", "apply", True), ("preset", "remove", False), ("preset", "move", False),
    ("note", "expose", False), ("binding", "remove", True), ("group", "move", True)])
def test_source_first_classification(source, field, want):
    assert cd.is_structural(f"{source}:p:{field}") is want


def test_capture_uses_live_values_and_decimal_seed_members():
    v = view()
    v["_presets"] = {}
    v["bindings"][1]["value"] = 18446744073709551615
    changes, refused = validate(v, "create", {"name": "Seeds", "members": ["seed", "text"]})
    assert not refused
    assert changes["preset:p:create"]["members"] == {"seed": {"value": "18446744073709551615", "seed_mode": "randomize"}, "text": {"value": "{a|b}"}}
    for raw in [{"name": "Empty", "members": []}, {"name": "No", "members": ["missing"]}]:
        assert validate(v, "create", raw)[1]


def test_preset_names_are_stored_trimmed_and_blank_ones_refused():
    v = view()
    v["_presets"] = {}
    changes, refused = validate(v, "create", {"name": "  Create a Clean Plate ", "members": ["steps"]})
    assert not refused and changes["preset:p:create"]["name"] == "Create a Clean Plate"
    assert validate(view(), "name", " Detailing v2  ") == ({"preset:p:name": "Detailing v2"}, [])
    assert validate(view(), "name", "   ")[1]
    # Only a length past the cap after trimming is refused.
    assert validate(view(), "name", " " + "x" * cm.LABEL_MAX + " ")[0] == {"preset:p:name": "x" * cm.LABEL_MAX}


@pytest.mark.parametrize("mode,number,state", [("randomize", 999, "match"), ("increment", 7, "differs"), ("fixed", 7, "differs")])
def test_advancing_seed_matches_mode_only(mode, number, state):
    v = view(definition({"seed": {"value": "7", "seed_mode": "randomize"}}))
    v["bindings"][1].update(value=number, seed_mode=mode)
    assert cd.preset_states(v, v["_presets"])[0]["state"] == state


def test_fixed_seed_matches_number_and_skips_absent_mode():
    v = view(definition({"seed": {"value": "7", "seed_mode": "fixed"}}))
    v["bindings"][1]["seed_mode"] = "fixed"
    assert cd.preset_states(v, v["_presets"])[0]["state"] == "match"
    v["bindings"][1]["value"] = 8
    assert cd.preset_states(v, v["_presets"])[0]["state"] == "differs"
    v["bindings"][1].update(value=7, seed_mode=None)
    assert cd.preset_states(v, v["_presets"])[0]["state"] == "match"
    assert validate(v, "apply", True) == ({"binding:seed:value": 7}, [])


@pytest.mark.parametrize("damage", ["missing", "hidden", "linked", "refused"])
def test_atomic_apply_refuses_every_value_and_names_member(damage):
    v = view(definition({"steps": {"value": "40"}, "choice": {"value": "b"}}))
    if damage == "missing":
        v["bindings"] = v["bindings"][:-1]
    elif damage == "hidden":
        v["bindings"][-1]["hidden"] = True
    elif damage == "linked":
        v["bindings"][-1]["status"] = "linked"
    else:
        v["bindings"][-1]["options"] = ["a"]
    changes, refused = validate(v, "apply", True)
    assert changes == {} and refused
    assert ("choice" if damage == "missing" else "Model") in refused[0]["reason"]
    assert cd.preset_states(v, v["_presets"])[0]["state"] == ("refused" if damage == "refused" else "unavailable")


def test_apply_alone_nonmembers_untouched_and_private_definitions_omitted():
    v = view()
    assert validate(v, "apply", True) == ({"binding:steps:value": 20}, [])
    assert cd.validate_binding_changes(v, {"preset:p:apply": True, "binding:text:value": "x"}, structural="canvas")[1]
    assert "_presets" not in cd.json_safe(v)
    v["bindings"][2]["value"] = "other"
    assert cd.preset_states(v, v["_presets"])[0]["state"] == "match"


def test_capture_keeps_checked_missing_member_and_removes_unchecked():
    v = view(definition({"gone": {"value": "old"}, "steps": {"value": "10"}}))
    changes, refused = validate(v, "capture", {"members": ["gone", "steps"]})
    assert not refused
    assert changes["preset:p:capture"]["members"] == {"gone": {"value": "old"}, "steps": {"value": "20"}}
    changes, refused = validate(v, "capture", {"members": ["steps"]})
    assert not refused and "gone" not in changes["preset:p:capture"]["members"]


def test_mounted_raw_intents_and_clean_presentation():
    v = {"relay": {"status": "ok"}, "bindings": [], "notes": []}
    for field, value in [("create", {"name": "A", "members": ["seed"]}), ("capture", {"members": ["seed"]}),
                         ("apply", True), ("remove", True), ("name", "Edited")]:
        change = {f"preset:p:{field}": value}
        assert cd.validate_binding_changes(v, change, structural="relay") == (change, [])


def test_effective_copy_reset_tombstone_and_order():
    authored = {"p": definition()}
    edited = definition(name="Mine")
    effective, origin = cp.effective_presets(authored, {"p": edited, "new": definition(name="New")})
    assert effective["p"]["name"] == "Mine" and origin == {"p": "app_copy", "new": "app"}
    assert cp.effective_presets(authored, {"p": {"removed": True}}) == ({}, {})
    assert cp.effective_presets(authored, {})[0] == authored
    cp.edit_definitions(effective, "new", "move", {"before": "p"})
    assert effective["new"]["order"] < effective["p"]["order"]


def test_python_js_read_parity_and_source_table(tmp_path):
    root = Path(__file__).resolve().parents[1]
    raw = {"p": definition(), "bad": {}, "unicode": definition(name="😀" * 120),
           "bad_bool": definition(order=True), "partial": definition({"bad": {}, "steps": {"value": "1"}}),
           "rows": definition({"stack": {"value": [{"lora_name": "sonder_test_a.safetensors", "strength": 0.5, "enabled": True}]},
                               "deep": {"value": [{"a": [1]}]}, "long": {"value": [{"a": 1}] * 21}})}
    script = f'''import assert from 'node:assert/strict';
    const b = await import({json.dumps((root / 'web/js/control_bindings.js').as_uri())});
    const t = await import({json.dumps((root / 'web/js/control_targets.js').as_uri())});
    assert.deepEqual(b.readPresets({json.dumps(raw)}), {json.dumps(cm.read_presets(raw))});
    const table = {json.dumps({s: sorted(ops) for s, ops in cd.STRUCTURAL_OPS.items()})};
    for (const [source, ops] of Object.entries(table)) for (const field of ['create','remove','apply','capture','move','value','description','reset','expose','rebind','hide','unhide','rename','ungroup'])
        assert.equal(t.isStructuralTarget(`${{source}}:p:${{field}}`), ops.includes(field), `${{source}}:${{field}}`);
    assert.equal(t.changesRun('preset:p:apply'), true); assert.equal(t.changesRun('preset:p:remove'), false);
    const m = {{presets: 3}}; b.applyPresetEdit(m, 'p', 'create', {json.dumps(definition())});
    assert.ok(b.readPresets(m.presets).p);
    assert.throws(()=>b.applyPresetEdit(m, 'p', 'capture', {{name:'Bad', members:{{}},order:0}}));
    const text = JSON.stringify({{bindings:{{}},notes:{{}},presets:3}});
    assert.equal(JSON.parse(b.editManifest(text, m=>{{m.description='x';}})).presets, 3);
    '''
    path = tmp_path / 'parity.mjs'
    path.write_text(script, encoding='utf-8')
    result = subprocess.run(['node', str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def category(name, choices, order=0, description=""):
    return {"name": name, "description": description, "order": order, "choices": choices}


def test_category_reader_edits_and_grammar_mirror_the_frontend(tmp_path):
    from server import control_preset_categories as pc
    root = Path(__file__).resolve().parents[1]
    raw = {"ok": category("Task", ["a", "bad id", 3, "a", "__proto__"]), "noname": {"choices": []},
           "long": category("x", [], description="d" * 301), "toomany": category("x", [f"p{i}" for i in range(33)]),
           "badorder": category("x", [], order=True), "notlist": {"name": "x", "choices": "a"}, "bad id": category("x", []),
           "whole": category("Whole", [], order=5.0), "unicode": category("😀" * 120, [], description="é" * 300),
           **{f"blank{i}": category(text, []) for i, text in enumerate(["﻿", "\x1c", "\x1f", "\x85", " ", " 　 "])},
           **{f"c{i}": category(f"C{i}", ["a"], i) for i in range(14)}, "7": category("Seven", []), "10": category("Ten", [])}
    presets = {"5": definition(), "p": definition(name="﻿P"), "q": definition(name="\x85"), "r": definition(order=2.0)}
    # One edit sequence through both writers.
    steps = [("x", "create", {"name": "X", "description": "", "choices": ["a"], "order": 3}), ("y", "create", category("Y", [], 4)),
             ("y", "move", {"before": "w"}), ("x", "choices", ["a", "b"]), ("w", "name", "Renamed"), ("x", "move", {"before": None}),
             ("y", "description", "Text"), ("w", "remove", True)]
    start = {"w": category("W", ["a", "b"], 1), "v": category("V", ["b"], 1)}
    expected = copy.deepcopy(start)
    for cid, field, value in steps:
        pc.edit_categories(expected, cid, field, value)
    dropped = copy.deepcopy(expected)
    pc.drop_choice(dropped, "a")
    targets = {f: (cd.is_structural(f"preset_category:c:{f}"), cd.parse_target(f"preset_category:c:{f}")) for f in sorted(pc.FIELDS)}
    script = f'''import assert from 'node:assert/strict';
    const b = await import({json.dumps((root / 'web/js/control_bindings.js').as_uri())});
    const t = await import({json.dumps((root / 'web/js/control_targets.js').as_uri())});
    assert.deepEqual(b.readPresetCategories({json.dumps(raw)}), {json.dumps(cm.read_preset_categories(raw))});
    assert.deepEqual(b.readPresets({json.dumps(presets)}), {json.dumps(cm.read_presets(presets))});
    for (const text of {json.dumps(["﻿", "\x1c", "\x1f", "\x85", " ", " ", "﻿A", "A"])})
        assert.equal(b.blankName(text), !/A/.test(text), JSON.stringify(text));
    for (const [field, [structural, parsed]] of Object.entries({json.dumps(targets)})) {{
        const target = `preset_category:c:${{field}}`;
        assert.equal(t.isStructuralTarget(target), structural); assert.equal(t.changesRun(target), false);
        assert.deepEqual(t.parseTarget(target), {{source: parsed[0], id: parsed[1], field: parsed[2]}});
    }}
    const m = {{preset_categories: {json.dumps(start)}, keep: 1}};
    for (const [cid, field, value] of {json.dumps(steps)}) b.applyCategoryEdit(m, cid, field, value);
    assert.deepEqual(m.preset_categories, {json.dumps(expected)}); assert.equal(m.keep, 1);
    b.dropChoiceFromCategories(m, 'a');
    assert.deepEqual(m.preset_categories, {json.dumps(dropped)});
    const odd = {{preset_categories: 3}}; b.dropChoiceFromCategories(odd, 'a'); assert.equal(odd.preset_categories, 3);
    for (const [cid, field, value] of [['__proto__','create',{{name:'X',choices:[]}}], ['ghost','name','X'], ['x','reset',true],
                                       ['x','move',{{before:'toString'}}], ['x','name','﻿'], ['x','value',1]])
        assert.throws(() => b.applyCategoryEdit(m, cid, field, value), `${{cid}} ${{field}}`);
    assert.equal(Object.getPrototypeOf(m.preset_categories), Object.prototype);
    const full = {{preset_categories: Object.fromEntries(Array.from({{length: 16}}, (_, i) => [`c${{i}}`, {{name: 'C', order: i, choices: []}}]))}};
    for (const id of ['extra', '7'])  // an index-like id sorts first: the cap is counted before it lands
        assert.throws(() => b.applyCategoryEdit(full, id, 'create', {{name: 'X', description: '', choices: [], order: 17}}), id);
    assert.equal(Object.keys(b.readPresetCategories(full.preset_categories)).length, 16);
    // Builtin names never reach a prototype.
    for (const field of ['description', 'choices', 'move', 'name'])
        assert.throws(() => b.applyCategoryEdit(full, 'toString', field, field === 'move' ? {{before: null}} : field === 'choices' ? [] : 'x'), field);
    assert.equal(Object.prototype.toString.description, undefined); assert.equal(Object.prototype.toString.choices, undefined);
    // An unreadable entry under an id is replaced by an explicit create, as on the server.
    const damaged = {{preset_categories: {{x: {{name: ''}}}}}};
    b.applyCategoryEdit(damaged, 'x', 'create', {{name: 'X', description: '', choices: [], order: 0}});
    assert.equal(damaged.preset_categories.x.name, 'X');
    const odd2 = {{preset_categories: 'saved'}}; b.applyCategoryEdit(odd2, 'x', 'remove', true); assert.equal(odd2.preset_categories, 'saved');
    // JavaScript's key order caps presets at the same 32 as Python's reader.
    const many = {json.dumps({**{f"p{i}": definition(name=f"P{i}") for i in range(31)}, "5": definition(), "9": definition()})};
    assert.deepEqual(Object.keys(b.readPresets(many)), {json.dumps(list(cm.read_presets({**{f"p{i}": definition(name=f"P{i}") for i in range(31)}, "5": definition(), "9": definition()})))});
    const withPresets = {{presets: {{a: {json.dumps(definition())}}}, preset_categories: {{w: {{name: 'W', order: 0, choices: ['a']}}}}}};
    b.applyPresetEdit(withPresets, 'a', 'remove', true); assert.deepEqual(withPresets.preset_categories.w.choices, []);
    const cleaned = {{bindings: {{}}, presets: {{a: {json.dumps(definition())}}}, preset_categories: {{w: {{name: 'W', order: 0, choices: ['a']}}}}}};
    b.applyCleanupPlan(cleaned, {{preset_members: [{{preset_id: 'a', binding_id: 'steps'}}]}});
    assert.ok(!cleaned.presets.a); assert.deepEqual(cleaned.preset_categories.w.choices, []);
    '''
    path = tmp_path / 'categories.mjs'
    path.write_text(script, encoding='utf-8')
    result = subprocess.run(['node', str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def app_template(tmp_path, monkeypatch):
    import sys
    import types
    from server import sdk_templates as st
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(get_user_directory=lambda: str(tmp_path)))
    class Sampler:
        @classmethod
        def INPUT_TYPES(cls):
            return {"required": {"steps": ("INT", {"min": 1, "max": 100}), "seed": ("INT", {"min": 0})}}
    monkeypatch.setattr(cd, "_comfy_class_lookup", lambda name: Sampler)
    manifest = {"bindings": {"steps": {"label": "Steps", "targets": [{"path": "3", "input": "steps"}]},
                              "seed": {"label": "Seed", "targets": [{"path": "3", "input": "seed"}]}},
                "presets": {"p": definition({"steps": {"value": "20"}, "seed": {"value": "7", "seed_mode": "randomize"}})}}
    prompt = {"3": {"class_type": "Sampler", "inputs": {"steps": 20, "seed": 7}},
              "9": {"class_type": "SonderControlRelay", "inputs": {"manifest": json.dumps(manifest)}}}
    validation = lambda p: {"ok": True, "warnings": [], "errors": [], "description": ""}
    identity = st.import_template(json.dumps(prompt).encode(), validate=validation)["template_id"]
    def snapshot():
        stored = st.read_control_values(identity)
        current, _ = st.load_template(identity)
        projected = st.apply_stored_values(current, st.describe_stored(current, stored), stored)
        return current, stored, projected
    def write(changes):
        current, stored, projected = snapshot()
        cleaned, refused = cd.validate_binding_changes(projected, changes, prompt=current, structural="sdk")
        assert not refused, refused
        return st.store_binding_changes(identity, cleaned, projected, current)
    return st, identity, prompt, manifest, validation, snapshot, write


def test_app_copy_survives_replace_reports_shadow_reset_and_export(app_template):
    st, identity, prompt, manifest, validation, snapshot, write = app_template
    assert snapshot()[2]["presets"][0]["state"] == "match"  # mode resolved before matching
    data = write({"preset:p:name": "My copy"})
    assert data["presets"]["p"]["members"] == manifest["presets"]["p"]["members"]
    assert snapshot()[2]["presets"][0]["origin"] == "app_copy"
    manifest["presets"]["p"]["name"] = "New workflow version"
    prompt["9"]["inputs"]["manifest"] = json.dumps(manifest)
    result = st.replace_template(identity, json.dumps(prompt).encode(), validate=validation)
    assert result["presets_kept"] == 1
    assert snapshot()[2]["presets"][0]["name"] == "My copy"
    exported = cm.find_relay(json.loads(st.export_curated(identity)))["manifest"]
    assert exported["presets"]["p"]["name"] == "My copy"
    write({"preset:p:reset": True})
    assert snapshot()[2]["presets"][0]["name"] == "New workflow version"
    assert snapshot()[2]["presets"][0]["origin"] == "workflow"


def test_app_atomic_apply_single_mutation_hidden_export_keeps_members(app_template):
    st, identity, prompt, manifest, validation, snapshot, write = app_template
    write({"binding:steps:value": 44, "binding:seed:seed_mode": "fixed"})
    seq = snapshot()[1]["seq"]
    write({"preset:p:apply": True})
    current, stored, projected = snapshot()
    assert stored["seq"] == seq + 1
    assert current["3"]["inputs"]["steps"] == 20
    assert projected["presets"][0]["state"] == "match"
    write({"binding:steps:hide": True})
    assert snapshot()[2]["presets"][0]["state"] == "unavailable"
    exported = cm.find_relay(json.loads(st.export_curated(identity)))["manifest"]
    assert "steps" not in exported["bindings"]
    assert "steps" in exported["presets"]["p"]["members"]
    write({"preset:p:capture": {"members": ["seed"]}})
    assert snapshot()[2]["presets"][0]["state"] == "match"


def test_app_preset_caps_fail_before_persistence(app_template):
    st, identity, prompt, manifest, validation, snapshot, write = app_template
    for i in range(31):
        write({f"preset:p{i}:create": {"name": f"Preset {i}", "members": ["steps"]}})
    current, stored, projected = snapshot()
    cleaned, refused = cd.validate_binding_changes(projected, {"preset:extra:create": {"name": "Extra", "members": ["steps"]}}, structural="sdk")
    assert refused and "32" in refused[0]["reason"]
    assert snapshot()[1]["seq"] == stored["seq"]
    # Unknown raw author metadata counts against the same export cap.
    manifest["future"] = "x" * (cm.MAX_MANIFEST_BYTES - len(json.dumps(manifest).encode()) - 70)
    prompt["9"]["inputs"]["manifest"] = json.dumps(manifest)
    before = st.read_control_values(identity)
    source = Path(st.template_source_path(identity)).read_bytes()
    with pytest.raises(st.TemplateError, match="128 KiB"):
        st.replace_template(identity, json.dumps(prompt).encode(), validate=validation)
    assert st.read_control_values(identity) == before
    assert Path(st.template_source_path(identity)).read_bytes() == source
    manifest.pop("future")
    manifest["presets"]["new"] = definition(name="New authored")
    prompt["9"]["inputs"]["manifest"] = json.dumps(manifest)
    with pytest.raises(st.TemplateError, match="32 presets"):
        st.replace_template(identity, json.dumps(prompt).encode(), validate=validation)
    assert st.read_control_values(identity) == before
    assert Path(st.template_source_path(identity)).read_bytes() == source
    exported = cm.find_relay(json.loads(st.export_curated(identity)))["manifest"]["presets"]
    assert len(exported) == 32 and "p30" in exported
    # Externally corrupted/over-cap storage must refuse export, never truncate.
    st.update_control_values(identity, lambda data: data["presets"].update(extra=definition()))
    with pytest.raises(st.TemplateError, match="32 presets"):
        st.export_curated(identity)


# ── Categories (Controls Modes) in the Sonder App ─────────────────────────────

def category_rows(snapshot):
    return {row["id"]: row for row in snapshot()[2]["preset_categories"]}


def test_app_categories_copy_on_write_reset_tombstone_and_replace(app_template):
    st, identity, prompt, manifest, validation, snapshot, write = app_template
    manifest["presets"]["q"] = definition({"steps": {"value": "30"}}, name="Other", order=1)
    manifest["preset_categories"] = {"task": {"name": "Task", "order": 0, "choices": ["p", "q"]}}
    prompt["9"]["inputs"]["manifest"] = json.dumps(manifest)
    st.replace_template(identity, json.dumps(prompt).encode(), validate=validation)
    assert category_rows(snapshot)["task"]["origin"] == "workflow"
    data = write({"preset_category:task:name": "Job"})
    assert data["preset_categories"]["task"]["name"] == "Job" and data["presets"] == {}
    assert category_rows(snapshot)["task"]["origin"] == "app_copy"
    manifest["preset_categories"]["task"]["description"] = "New workflow text"
    prompt["9"]["inputs"]["manifest"] = json.dumps(manifest)
    assert st.replace_template(identity, json.dumps(prompt).encode(), validate=validation)["categories_kept"] == 1
    assert category_rows(snapshot)["task"]["name"] == "Job"
    write({"preset_category:task:reset": True})
    assert category_rows(snapshot)["task"]["description"] == "New workflow text"
    write({"preset_category:task:remove": True})
    assert "task" not in category_rows(snapshot)
    assert snapshot()[1]["preset_categories"]["task"] == {"removed": True}


def test_app_choice_move_rewrites_only_categories_and_removed_choices_stay_authored(app_template):
    st, identity, prompt, manifest, validation, snapshot, write = app_template
    manifest["presets"]["q"] = definition({"steps": {"value": "30"}}, name="Other", order=1)
    manifest["preset_categories"] = {"task": {"name": "Task", "order": 0, "choices": ["p", "q"]},
                                     "look": {"name": "Look", "order": 1, "choices": []}}
    prompt["9"]["inputs"]["manifest"] = json.dumps(manifest)
    st.replace_template(identity, json.dumps(prompt).encode(), validate=validation)
    data = write({"preset_category:task:choices": ["p"], "preset_category:look:choices": ["q"]})
    assert data["presets"] == {} and set(data["preset_categories"]) == {"task", "look"}
    write({"preset_category:task:reset": True, "preset_category:look:reset": True})
    # Removing a choice in the app leaves its category the workflow's.
    data = write({"preset:q:remove": True})
    assert data["preset_categories"] == {}
    task = category_rows(snapshot)["task"]
    assert task["origin"] == "workflow" and task["choices"] == ["p"] and not any(w["code"] == "missing" for w in task["warnings"])
    write({"preset:q:reset": True})
    assert category_rows(snapshot)["task"]["choices"] == ["p", "q"]
    exported = cm.find_relay(json.loads(st.export_curated(identity)))["manifest"]
    assert exported["preset_categories"]["task"]["choices"] == ["p", "q"]
    write({"preset:q:remove": True})
    exported = cm.find_relay(json.loads(st.export_curated(identity)))["manifest"]
    assert exported["preset_categories"]["task"]["choices"] == ["p"]
    # A new list can't name a choice the app removed; the copy keeps it for Reset.
    write({"preset:r:create": {"name": "Third", "members": ["steps"]}})
    data = write({"preset_category:task:choices": ["r", "p"]})
    assert data["preset_categories"]["task"]["choices"] == ["r", "p", "q"]
    assert category_rows(snapshot)["task"]["choices"] == ["r", "p"]
    write({"preset:q:reset": True})
    assert category_rows(snapshot)["task"]["choices"] == ["r", "p", "q"]


def test_app_category_caps_curation_and_unknown_fields(app_template):
    st, identity, prompt, manifest, validation, snapshot, write = app_template
    assert not st._has_curation({"preset_categories": {}})
    assert st._has_curation({"preset_categories": {"c": {"removed": True}}})
    for i in range(16):
        write({f"preset_category:c{i}:create": {"name": f"C{i}", "choices": ["p"] if i == 0 else []}})
    current, stored, projected = snapshot()
    assert len(projected["preset_categories"]) == 16
    _, refused = cd.validate_binding_changes(projected, {"preset_category:x:create": {"name": "X"}}, structural="sdk")
    assert refused and "16" in refused[0]["reason"]
    with pytest.raises(st.TemplateError, match="unavailable"):
        st.store_binding_changes(identity, {"preset_category:c0:value": 1}, projected, current)
    # Externally over-cap storage refuses export, never truncates.
    st.update_control_values(identity, lambda data: data["preset_categories"].update(
        extra={"name": "Extra", "order": 99, "choices": []}))
    with pytest.raises(st.TemplateError, match="16 categories"):
        st.export_curated(identity)


def test_app_export_creates_a_relay_for_categories_alone(tmp_path, monkeypatch):
    import sys
    import types
    from server import sdk_templates as st
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(get_user_directory=lambda: str(tmp_path)))
    prompt = {"3": {"class_type": "Sampler", "inputs": {"steps": 20}}}
    validation = lambda p: {"ok": True, "warnings": [], "errors": [], "description": ""}
    identity = st.import_template(json.dumps(prompt).encode(), validate=validation)["template_id"]
    st.update_control_values(identity, lambda data: data["preset_categories"].update(
        only={"name": "Only", "order": 0, "choices": []}))
    exported = cm.find_relay(json.loads(st.export_curated(identity)))
    assert exported["status"] == "ok" and exported["manifest"]["preset_categories"]["only"]["name"] == "Only"
