"""Preset categories (Controls Modes, Phase A): reader, grammar, joint
validator, match conditions and authoring warnings."""
import copy
import json
import random

import pytest

from server import control_descriptors as cd, control_manifest as cm, control_presets as cp
from server import control_preset_categories as pc


def preset(name, members, order=0, **extra):
    return {"name": name, "description": "", "order": order, "members": members, **extra}


def category(name, choices, order=0, description=""):
    return {"name": name, "description": description, "order": order, "choices": choices}


def view(presets=None, categories=None):
    return {"relay": {"status": "ok"}, "notes": [], "bindings": [
        {"binding_id": "steps", "label": "Steps", "kind": "INT", "status": "ok", "value": 20, "min": 1, "max": 100},
        {"binding_id": "seed", "label": "Seed", "kind": "SEED", "status": "ok", "value": 7, "seed_mode": "fixed", "min": 0},
        {"binding_id": "text", "label": "Prompt", "kind": "STRING", "status": "ok", "value": "a"},
    ], "_presets": presets if presets is not None else {
        "a": preset("Alpha", {"steps": {"value": "20"}}), "b": preset("Beta", {"steps": {"value": "30"}}, 1),
        "c": preset("Gamma", {"text": {"value": "c"}}, 2)},
        "_preset_categories": categories if categories is not None else {"task": category("Task", ["a", "b"])}}


def validate(v, changes, structural="canvas"):
    return cd.validate_binding_changes(v, changes, structural=structural)


# ── Reader ───────────────────────────────────────────────────────────────────

def test_reader_is_tolerant_and_never_rewrites():
    raw = {"ok": category("Task", ["a", "bad id", 3, "a", "__proto__"]), "noname": {"choices": []},
           "blank": category("﻿\x1c", []), "long": category("x", [], description="d" * 301),
           "toomany": category("x", [f"p{i}" for i in range(33)]), "badorder": category("x", [], order=True),
           "notlist": {"name": "x", "choices": "a"}, "constructor": category("x", []), "bad id": category("x", []),
           "whole": category("Whole", [], order=5.0)}
    original = copy.deepcopy(raw)
    read = cm.read_preset_categories(raw)
    assert raw == original
    assert read == {"ok": category("Task", ["a", "a"]), "whole": category("Whole", [], order=5)}
    assert cm.read_preset_categories([]) == {} and cm.read_preset_categories(None) == {}
    # Manifest hygiene still refuses a prototype key anywhere.
    with pytest.raises(cm.ManifestError, match="reserved"):
        cm.parse_manifest(json.dumps({"preset_categories": raw}))
    del raw["constructor"]
    assert cm.parse_manifest(json.dumps({"preset_categories": raw}))["preset_categories"] == read
    assert cm.empty_manifest()["preset_categories"] == {}


def test_reader_caps_valid_entries_in_javascript_key_order():
    raw = {"bad": {}} | {f"c{i}": category(f"C{i}", []) for i in range(15)} | {"7": category("Seven", []), "10": category("Ten", [])}
    read = cm.read_preset_categories(raw)
    # JavaScript lists index-like keys first, so they take two of the 16 places.
    assert len(read) == 16 and list(read)[:2] == ["7", "10"] and "c13" in read and "c14" not in read


def test_read_presets_uses_javascript_order_whole_floats_and_one_blank_rule():
    raw = {f"p{i}": preset(f"P{i}", {"steps": {"value": "1"}}) for i in range(31)} | {"5": preset("Five", {"steps": {"value": "1"}}),
                                                                                    "9": preset("Nine", {"steps": {"value": "1"}})}
    read = cm.read_presets(raw)
    assert len(read) == 32 and list(read)[:2] == ["5", "9"] and "p30" not in read
    assert cm.read_presets({"p": preset("P", {"steps": {"value": "1"}}, order=3.0)})["p"]["order"] == 3
    for blank in ("﻿", "\x1c", "\x1f", "\x85", "  ", " 　"):
        assert not cm.read_presets({"p": preset(blank, {"steps": {"value": "1"}})}), repr(blank)
        assert cm.blank_name(blank)
    assert not cm.blank_name("﻿A")


# ── Grammar ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("field", sorted(pc.FIELDS))
def test_category_targets_are_presentation_only(field):
    target = f"preset_category:task:{field}"
    assert cd.parse_target(target) == ("preset_category", "task", field)
    assert cd.is_structural(target) is False


@pytest.mark.parametrize("target", ["preset_category:bad id:name", "preset_category:task:apply", "preset_category:task:value",
                                    "preset_category:task:label", "preset_category:__proto__:name"])
def test_grammar_and_validator_negatives(target):
    assert validate(view(), {target: "X"})[1]


# ── Joint validator ──────────────────────────────────────────────────────────

def test_create_a_choice_and_list_it_in_one_request():
    v = view()
    v["bindings"][0]["value"] = 40
    changes = {"preset:d:create": {"name": "Delta", "members": ["steps"]},
               "preset_category:task:choices": ["a", "b", "d"]}
    cleaned, rejected = validate(v, changes)
    assert not rejected and cleaned["preset_category:task:choices"] == ["a", "b", "d"]
    created, rejected = validate(v, {"preset_category:look:create": {"name": "  Look ", "choices": ["c"]}})
    assert not rejected
    assert created["preset_category:look:create"] == {"name": "Look", "description": "", "choices": ["c"], "order": 1}


@pytest.mark.parametrize("order", [0, 1])
def test_move_a_choice_between_categories_in_either_order(order):
    v = view(categories={"task": category("Task", ["a", "b"]), "look": category("Look", ["c"], 1)})
    targets = [("preset_category:task:choices", ["a"]), ("preset_category:look:choices", ["c", "b"])]
    changes = dict(targets if order == 0 else targets[::-1])
    assert validate(v, changes) == (changes, [])


def test_a_duplicate_the_batch_makes_is_refused_but_an_existing_one_blocks_nothing():
    v = view(categories={"task": category("Task", ["a", "b"]), "look": category("Look", ["c"], 1)})
    _, rejected = validate(v, {"preset_category:look:choices": ["c", "a"]})
    assert rejected and "two categories" in rejected[0]["reason"] and rejected[0]["target"] == "preset_category:look:choices"
    _, rejected = validate(v, {"preset_category:new:create": {"name": "New", "choices": ["a"]}})
    assert rejected
    existing = view(categories={"task": category("Task", ["a", "b"]), "look": category("Look", ["a", "c"], 1),
                                "other": category("Other", [], 2)})
    for changes in ({"preset_category:other:name": "Renamed"}, {"preset:a:name": "Alpha 2"},
                    {"preset:a:capture": {"members": ["steps"]}}, {"preset_category:look:choices": ["c", "a"]}):
        assert not validate(existing, changes)[1], changes
    assert validate(existing, {"preset_category:look:choices": ["a", "c", "b"]})[1]  # b joins a second category


def test_remove_category_leaves_choices_and_preset_remove_drops_the_id():
    v = view()
    assert validate(v, {"preset_category:task:remove": True}) == ({"preset_category:task:remove": True}, [])
    # Removing a choice drops it from the batch's categories: listing it later fails.
    _, rejected = validate(v, {"preset:a:remove": True, "preset_category:task:choices": ["b", "a"]})
    assert rejected and "no longer exists" in rejected[0]["reason"]
    categories = {"task": category("Task", ["a", "b", "a"]), "look": category("Look", ["c"]), "odd": {"choices": "a"}}
    assert pc.drop_choice(categories, "a") == {"task"}
    assert categories["task"]["choices"] == ["b"] and categories["odd"] == {"choices": "a"}


@pytest.mark.parametrize("changes,reason", [
    ({"preset:a:apply": True, "preset_category:task:name": "X"}, "Apply one preset"),
    ({"preset_category:t2:create": {"name": "T", "choices": ["zz"]}}, "no longer exists"),
    ({"preset_category:ghost:name": "X"}, "no longer exists"),
    ({"preset_category:task:reset": True}, "Sonder App"),
    ({"preset_category:task:name": "﻿"}, "category name"),
    ({"preset:a:name": "﻿"}, "preset name"),
    ({"preset_category:task:description": "d" * 301}, "at most 300"),
    ({"preset_category:task:choices": ["a", "a"]}, "different choices"),
    ({"preset_category:task:move": {"before": "task"}}, "another category"),
    ({"preset_category:task:move": {"before": "ghost"}}, "another category"),
    ({"preset_category:task:remove": "yes"}, "Send true"),
    ({"preset_category:task:create": {"name": "Again"}}, "already in use"),
    ({"preset_category:x:create": {"name": "X", "extra": 1}}, "Name the new category"),
])
def test_category_refusals(changes, reason):
    _, rejected = validate(view(), changes)
    assert rejected and any(reason in r["reason"] for r in rejected), rejected


def test_category_cap_and_mixed_batch_with_a_setting():
    full = view(categories={f"c{i}": category(f"C{i}", [], i) for i in range(16)})
    assert "16 categories" in validate(full, {"preset_category:x:create": {"name": "X"}})[1][0]["reason"]
    # A setting change beside a category edit validates once, without recursion.
    cleaned, rejected = validate(view(), {"binding:steps:value": "30", "preset_category:task:name": "Job"})
    assert not rejected and cleaned == {"binding:steps:value": 30, "preset_category:task:name": "Job"}


def test_relay_forwards_shapes_and_refuses_bad_ones():
    bare = {"relay": {"status": "ok"}, "bindings": [], "notes": []}
    for field, value in [("create", {"name": "Task", "choices": ["a"]}), ("name", "Job"), ("description", "d"),
                         ("choices", ["a", "b"]), ("move", {"before": None}), ("remove", True)]:
        change = {f"preset_category:task:{field}": value}
        cleaned, rejected = validate(bare, change, "relay")
        assert not rejected, (field, rejected)
    for field, value in [("name", 3), ("choices", "a"), ("move", {"before": 1}), ("reset", True), ("remove", False)]:
        assert validate(bare, {f"preset_category:task:{field}": value}, "relay")[1], field


def test_edits_and_the_sdk_reset():
    categories = {"a": category("A", ["x"], 0), "b": category("B", ["y"], 1), "c": category("C", [], 2)}
    pc.edit_categories(categories, "c", "move", {"before": "a"})
    assert [cid for cid in pc.ordered(categories)] == ["c", "a", "b"]
    pc.edit_categories(categories, "a", "move", {"before": None})
    assert pc.ordered(categories) == ["c", "b", "a"]
    assert validate(view(), {"preset_category:task:reset": True}, "sdk") == ({"preset_category:task:reset": True}, [])


# ── Warnings ─────────────────────────────────────────────────────────────────

def rows(v, prompt=None, **kwargs):
    return cp.preset_projection(v, v["_presets"], v["_preset_categories"], prompt=prompt, **kwargs)[1]


def codes(row):
    return [w["code"] for w in row["warnings"]]


def test_warnings_too_few_duplicate_missing_and_claims():
    v = view(categories={"task": category("Task", ["a", "a", "ghost", "b"]), "look": category("Look", ["b", "c"], 1),
                         "empty": category("Empty", [], 2)})
    task, look, empty = rows(v)
    assert task["choices"] == ["a", "b"] and look["choices"] == ["c"]
    assert set(codes(task)) >= {"duplicate", "missing"} and "too_few" not in codes(task)
    assert [w for w in look["warnings"] if w["code"] == "duplicate"][0]["other"] == "task"
    assert "too_few" in codes(look) and "too_few" in codes(empty)
    missing = [w for w in task["warnings"] if w["code"] == "missing"][0]
    assert missing["choices"] == ["ghost"] and missing["message"] == "1 listed choice no longer exists."
    # A choice the app removed itself leaves without a warning.
    dropped = view(categories={"task": category("Task", ["a", "gone", "b"])})
    assert "missing" not in codes(rows(dropped, dropped={"gone"})[0])


def test_overlap_uses_coerced_values_and_is_capped():
    v = view({"a": preset("A", {"steps": {"value": "20"}}), "b": preset("B", {"steps": {"value": 20}}, 1),
              "c": preset("C", {"steps": {"value": "30"}}, 2)}, {"task": category("Task", ["a", "b", "c"])})
    overlap = [w for w in rows(v)[0]["warnings"] if w["code"] == "overlap"]
    assert overlap and overlap[0]["pairs"] == [["a", "b"]] and overlap[0]["count"] == 1
    many = {f"p{i}": preset(f"P{i}", {"text": {"value": "same"}}, i) for i in range(6)}
    capped = view(many, {"task": category("Task", list(many))})
    warning = [w for w in rows(capped)[0]["warnings"] if w["code"] == "overlap"][0]
    assert warning["count"] == 15 and len(warning["pairs"]) == 4 and "And 11 more pairs." in warning["message"]


def test_seed_conditions_follow_the_host_rule():
    def pair(a, b, host_mode="fixed"):
        v = view({"a": preset("A", {"seed": a}), "b": preset("B", {"seed": b}, 1)}, {"task": category("Task", ["a", "b"])})
        v["bindings"][1]["seed_mode"] = host_mode
        return "overlap" in codes(rows(v)[0])
    assert pair({"value": "7", "seed_mode": "randomize"}, {"value": "8", "seed_mode": "randomize"})  # mode only
    assert not pair({"value": "7", "seed_mode": "fixed"}, {"value": "8", "seed_mode": "fixed"})
    assert not pair({"value": "7", "seed_mode": "randomize"}, {"value": "7", "seed_mode": "fixed"})
    # A host binding without a mode widget compares numbers only.
    assert not pair({"value": "7", "seed_mode": "randomize"}, {"value": "8", "seed_mode": "randomize"}, host_mode=None)


def test_linked_only_when_values_differ():
    presets = {"t1": preset("Remove People", {"steps": {"value": "20"}, "text": {"value": "x"}}),
               "t2": preset("New Video", {"steps": {"value": "30"}}, 1),
               "e1": preset("Video only", {"text": {"value": "x"}}, 2), "e2": preset("Audio only", {"text": {"value": "y"}}, 3)}
    linked = view(presets, {"task": category("Task", ["t1", "t2"]), "edit": category("Edit", ["e1", "e2"], 1)})
    task, edit = rows(linked)
    warning = [w for w in task["warnings"] if w["code"] == "linked"][0]
    assert warning["categories"] == [{"id": "edit", "controls": [{"kind": "binding", "id": "text"}]}] and warning["count"] == 1
    assert warning["message"] == ("Shares Prompt with Edit: a choice here can change Edit, and Edit can change this row.")
    assert [w["categories"][0]["id"] for w in edit["warnings"] if w["code"] == "linked"] == ["task"]
    same = copy.deepcopy(presets)
    same["e2"]["members"]["text"]["value"] = "x"
    same["e2"]["members"]["steps"] = {"value": "20"}
    same["e1"]["members"]["steps"] = {"value": "20"}
    same["t2"]["members"]["steps"]["value"] = "20"
    unlinked = view(same, {"task": category("Task", ["t1", "t2"]), "edit": category("Edit", ["e1", "e2"], 1)})
    assert all("linked" not in codes(row) for row in rows(unlinked))


def test_one_linked_warning_per_row_and_bounded_bytes():
    # 16 categories of 2 choices, every pair sharing 64 long CJK-labelled settings with different values.
    bindings = [{"binding_id": f"b{n}", "label": "設" * 120, "kind": "INT", "status": "ok", "value": 0, "min": 0, "max": 999}
                for n in range(64)]
    presets = {f"p{p}": preset("名" * 120, {f"b{n}": {"value": str((p + n) % 999)} for n in range(64)}, p) for p in range(32)}
    categories = {f"c{c}": category("類" * 120, [f"p{2 * c}", f"p{2 * c + 1}"], c) for c in range(16)}
    v = {"relay": {"status": "ok"}, "notes": [], "bindings": bindings, "_presets": presets, "_preset_categories": categories}
    result = rows(v)
    for row in result:
        linked = [w for w in row["warnings"] if w["code"] == "linked"]
        assert len(linked) == 1 and linked[0]["count"] == 15 and len(linked[0]["categories"]) == 4
        assert "and 11 more" in linked[0]["message"]
    size = len(json.dumps([row["warnings"] for row in result], ensure_ascii=False).encode())
    assert size < 64 * 1024, size


def test_switches_tell_choices_apart_and_link_categories():
    switches = [switch("x", "5", "enabled")]
    run = preset("Run", {"steps": {"value": "20"}}, 0, execution={"x": {"mode": "enabled", "node": {"path": "5", "class_type": "K"}}})
    skip = preset("Skip", {"steps": {"value": "20"}}, 1, execution={"x": {"mode": "muted", "node": {"path": "5", "class_type": "K"}}})
    fast = preset("Fast", {"text": {"value": "a"}}, 2, execution={"x": {"mode": "muted", "node": {"path": "5", "class_type": "K"}}})
    other = preset("Other", {"text": {"value": "b"}}, 3)
    v = view({"run": run, "skip": skip, "fast": fast, "other": other},
             {"pass": category("Pass", ["run", "skip"]), "speed": category("Speed", ["fast", "other"], 1)})
    v["execution"] = switches
    first, second = rows(v)
    assert "overlap" not in codes(first)  # only the switch tells them apart
    linked = [w for w in first["warnings"] if w["code"] == "linked"][0]
    assert linked["categories"][0]["controls"] == [{"kind": "node", "id": "5"}]
    assert "Switch x" in linked["message"]


def test_duplicate_warnings_are_one_per_choice():
    v = view(categories={"task": category("Task", ["a", "b"]), "look": category("Look", ["a", "a", "a", "c"], 1)})
    look = rows(v)[1]
    assert [w["choice"] for w in look["warnings"] if w["code"] == "duplicate"] == ["a"]


def test_introduced_duplicates_are_pinned_on_the_targets_that_made_them():
    v = view(categories={"task": category("Task", ["a", "b"]), "look": category("Look", ["c"], 1)})
    _, rejected = validate(v, {"preset_category:task:name": "Job", "preset_category:look:choices": ["c", "a"]})
    assert [r["target"] for r in rejected] == ["preset_category:look:choices"]


def test_no_categories_costs_nothing_and_rows_match_preset_states():
    v = view(categories={})
    presets_only, category_rows = cp.preset_projection(v, v["_presets"], {})
    assert category_rows == [] and presets_only == cd.preset_states(v, v["_presets"])


# ── Mounted publication ──────────────────────────────────────────────────────

def publish(extra):
    from server import session_registry as sr
    return sr._prepare_control_publication({"view_seq": 1, "prompt_hash": "h",
                                            "view": {"bindings": [], "notes": [], **extra}})


def test_publication_checks_categories_only_when_present():
    # The error views a relay falls back to carry no categories.
    assert "preset_categories" not in publish({"error": "too large"})["view"]
    row = {"id": "task", "name": "Task", "description": "", "order": 0, "origin": "workflow", "choices": ["a", "b"],
           "warnings": [{"code": "overlap", "message": "x", "pairs": [["a", "b"]], "count": 1}]}
    assert publish({"preset_categories": [row]})["view"]["preset_categories"] == [row]
    for broken in ({"preset_categories": {}}, {"preset_categories": [row] * 17},
                   {"preset_categories": [{**row, "id": 3}]}, {"preset_categories": [{**row, "choices": [1]}]},
                   {"preset_categories": [{**row, "choices": ["a"] * 33}]},
                   {"preset_categories": [{**row, "warnings": [{"code": "x"}]}]},
                   {"preset_categories": [{**row, "warnings": "x"}]}):
        with pytest.raises(ValueError):
            publish(broken)


# ── Differential: two choices that both match are always warned ──────────────

def switch(identity, path, mode):
    return {"execution_id": identity, "source": "execution", "label": f"Switch {identity}", "status": "ok", "kind": "node",
            "mode": mode, "members": [{"path": path, "mode": mode, "class_type": "K", "kind": "node"}]}


def random_view(rng):
    bindings = [
        {"binding_id": "i", "label": "Int", "kind": "INT", "status": "ok", "value": 1, "min": 0, "max": 9},
        {"binding_id": "s", "label": "Seed", "kind": "SEED", "status": "ok", "value": 1, "seed_mode": "fixed", "min": 0},
        {"binding_id": "n", "label": "Seed no mode", "kind": "SEED", "status": "ok", "value": 1, "seed_mode": None, "min": 0},
        {"binding_id": "t", "label": "Text", "kind": "STRING", "status": "ok", "value": "a"},
        {"binding_id": "b", "label": "Flag", "kind": "BOOLEAN", "status": "ok", "value": True},
    ]
    presets = {}
    for p in range(4):
        members = {}
        for bid in rng.sample(["i", "s", "n", "t", "b"], rng.randint(1, 3)):
            if bid == "i":
                members[bid] = {"value": rng.choice(["1", "2", 1, 2])}
            elif bid in ("s", "n"):
                members[bid] = {"value": str(rng.randint(1, 2)), "seed_mode": rng.choice(["fixed", "randomize"])}
            elif bid == "t":
                members[bid] = {"value": rng.choice(["a", "b"])}
            else:
                members[bid] = {"value": rng.choice([True, False])}
        execution = {}
        if rng.random() < 0.5:
            execution["x"] = {"mode": rng.choice(["enabled", "muted"]), "node": {"path": "5", "class_type": "K"}}
        if rng.random() < 0.4:
            execution["g"] = {"modes": {"6": rng.choice(["enabled", "bypassed"]), "7": rng.choice(["enabled", "muted"])}}
        presets[f"p{p}"] = preset(f"P{p}", members, p, **({"execution": execution} if execution else {}))
    v = {"relay": {"status": "ok"}, "notes": [], "bindings": bindings,
         "execution": [switch("x", "5", "enabled"), group_switch("g", {"6": "enabled", "7": "enabled"})],
         "_presets": presets, "_preset_categories": {"all": category("All", list(presets))}}
    return v


def group_switch(identity, modes):
    return {"execution_id": identity, "source": "execution", "label": f"Group {identity}", "status": "ok", "kind": "group",
            "mode": "mixed", "members": [{"path": path, "mode": mode, "class_type": "K", "kind": "node"} for path, mode in modes.items()]}


def live_after(v, pid):
    """The view as it reads once a choice's captured values are in place."""
    live = copy.deepcopy(v)
    chosen = v["_presets"][pid]
    for descriptor in live["bindings"]:
        member = chosen["members"].get(descriptor["binding_id"])
        if member is None:
            continue
        descriptor["value"] = cd.coerce_binding_value(descriptor, member["value"])
        if descriptor.get("seed_mode") is not None and "seed_mode" in member:
            descriptor["seed_mode"] = member["seed_mode"]
    for eid, member in (chosen.get("execution") or {}).items():
        live["execution"] = [d for d in live["execution"] if d["execution_id"] != eid]
        live["execution"].append(group_switch(eid, member["modes"]) if "modes" in member
                                 else switch(eid, member["node"]["path"], member["mode"]))
    return live


def test_two_matching_choices_always_carry_an_overlap_warning():
    rng = random.Random(20261025)
    both_matched = 0
    for _ in range(300):
        v = random_view(rng)
        for pid in v["_presets"]:
            live = live_after(v, pid)
            presets_rows, category_rows = cp.preset_projection(live, live["_presets"], live["_preset_categories"])
            matched = [row["id"] for row in presets_rows if row["state"] == "match"]
            assert pid in matched
            overlap = [w for w in category_rows[0]["warnings"] if w["code"] == "overlap"]
            listed = {tuple(sorted(pair)) for w in overlap for pair in w["pairs"]}
            for other in matched:
                if other != pid:
                    both_matched += 1
                    assert overlap, (v["_presets"], pid, other)
                    # Every pair is listed while there are at most four.
                    assert tuple(sorted((pid, other))) in listed or overlap[0]["count"] > pc.WARNING_NAMES, (v["_presets"], pid, other)
    assert both_matched > 50  # the fixtures do reach both-match states
