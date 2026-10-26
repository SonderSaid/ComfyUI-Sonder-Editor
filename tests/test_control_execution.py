"""Phase 1A: inactive settings keep their binding; node switches; presets with modes.

The canvas resolver (web/js/control_bindings.js) exports participation and each
switch's members; these tests pin the server's side of that contract: parsing,
the authoring snapshot of inactive bodies, switch validation, preset capture /
match / apply with modes, cleanup eligibility and the Sonder App's interim
read-only view. JS parity for the shared readers lives in test_control_execution_js.py.
"""

import json

import pytest

from server import control_cleanup, control_execution as ce, control_manifest as cm
from server import control_descriptors as cd, control_presets as cp, controls_snapshot
from test_control_descriptors import CLASSES, _binding, _node


def member(path, mode="enabled", class_type="KSampler", kind="node", title=None):
    return {"path": path, "class_type": class_type, "mode": mode, "kind": kind, "title": title or class_type}


def switch(kind="node", members=(), status="ok", group_id=None, **extra):
    target = {"kind": kind, **({"group_id": group_id} if kind == "group" else {})}
    return {"label": "", "group": "", "order": 0, "advanced": False, "note": "", "target": target,
            "status": status, "members": list(members), "member_count": len(members), **extra}


def relay(bindings=None, execution=None, presets=None):
    manifest = {"v": 1, "bindings": bindings or {}, "notes": {}}
    if execution is not None:
        manifest["execution"] = {"v": 1, "controls": execution}
    if presets is not None:
        manifest["presets"] = presets
    return _node("SonderControlRelay", {"manifest": json.dumps(manifest)})


def describe(prompt, **kwargs):
    return cd.describe_bindings(prompt, class_lookup=CLASSES.get, **kwargs)


def base_prompt():
    return {"1": _node("SonderEditor"), "6": _node("KSampler", {"steps": 20, "seed": 5}, title="Pass 1"),
            "7": _node("TextNode", {"text": "hi"})}


# ── Manifest ──────────────────────────────────────────────────────────────────

def test_execution_section_parses_and_newer_versions_turn_off_only_itself():
    text = json.dumps({"v": 1, "bindings": {"steps": _binding("6", "steps")},
                       "execution": {"v": 1, "controls": {"x": switch(members=[member("6")], label="Pass one")}}})
    parsed = cm.parse_manifest(text)
    assert parsed["execution"]["x"]["target"] == {"kind": "node"} and parsed["execution_error"] is None
    assert parsed["execution"]["x"]["members"][0] == {"path": "6", "class_type": "KSampler", "mode": "enabled",
                                                    "kind": "node", "title": "KSampler"}
    newer = cm.parse_manifest(json.dumps({"v": 1, "bindings": {"steps": _binding("6", "steps")},
                                          "execution": {"v": 2, "controls": {}}}))
    assert newer["execution"] == {} and "another Sonder version" in newer["execution_error"] and "steps" in newer["bindings"]
    # A damaged entry is skipped on its own, like a preset; the rest still read.
    damaged = cm.parse_manifest(json.dumps({"execution": {"controls": {"x": {"target": {"kind": "planet"}},
                                                                       "y": switch(members=[member("6")])}}}))
    assert list(damaged["execution"]) == ["y"] and damaged["execution_error"] is None
    too_many = {f"s{i}": switch(members=[member(str(i))]) for i in range(cm.MAX_EXECUTION + 1)}
    capped = cm.parse_manifest(json.dumps({"execution": {"controls": too_many}}))
    assert len(capped["execution"]) == cm.MAX_EXECUTION and capped["execution_error"] is None
    bad_group = cm.parse_manifest(json.dumps({"execution": {"controls": {"g": {"target": {"kind": "group", "group_id": True}}}}}))
    assert bad_group["execution"] == {} and bad_group["execution_error"] is None


def test_binding_participation_export_fields_are_read_tolerantly():
    entry = cm._binding({**_binding("6", "steps"), "participation": "inactive",
                         "inactive_reason": {"mode": "bypassed", "by": "parent", "label": "Sub" * 100}})
    assert entry["participation"] == "inactive" and entry["inactive_reason"]["mode"] == "bypassed"
    assert len(entry["inactive_reason"]["label"]) == cm.LABEL_MAX
    junk = cm._binding({**_binding("6", "steps"), "participation": "asleep", "inactive_reason": {"mode": "off"}})
    assert "participation" not in junk and "inactive_reason" not in junk


def test_preset_execution_members_read_with_effects_budget_and_conflicts():
    node = {"mode": "muted", "node": {"path": "6", "class_type": "KSampler"}}
    group = {"modes": {"6": "muted", "8": "enabled"}}
    presets = cm.read_presets({
        "only_switch": {"name": "Skip", "members": {}, "execution": {"a": node}},
        "both": {"name": "Both", "members": {"steps": {"value": "3"}}, "execution": {"a": node, "g": group}},
        "conflict": {"name": "Bad", "members": {}, "execution": {"a": node, "g": {"modes": {"6": "bypassed"}}}},
        "junk": {"name": "Junk", "members": {}, "execution": {"a": {"mode": "asleep", "node": {"path": "6", "class_type": "K"}}}},
        "over": {"name": "Over", "members": {f"b{i}": {"value": 1} for i in range(63)},
                 "execution": {"g": {"modes": {"1": "muted", "2": "muted"}}}},
    })
    assert set(presets) == {"only_switch", "both"}
    # One node set the same way by two switches counts once.
    assert cm.execution_effects(presets["both"]["execution"]) == {"6": "muted", "8": "enabled"}


# ── Participation and the authoring snapshot ─────────────────────────────────

def inactive_body(class_type="KSampler", inputs=None, mode="muted", by="self", label="Pass 1"):
    return {"class_type": class_type, "inputs": inputs or {"steps": 20, "seed": 5}, "_meta": {"title": label},
            "mode": mode, "by": by, "label": label}


def test_a_muted_target_stays_ok_inactive_and_validates_against_its_body():
    prompt = base_prompt()
    del prompt["6"]  # the native export omits a muted node
    prompt["9"] = relay({"steps": {**_binding("6", "steps"), "participation": "inactive"}})
    authoring = cd.clean_authoring({"6": inactive_body()}, prompt)
    view = describe(prompt, authoring=authoring)
    steps = view["bindings"][0]
    assert steps["status"] == "ok" and steps["value"] == 20 and steps["participation"] == "inactive"
    assert steps["inactive_reason"] == {"mode": "muted", "by": "self", "label": "Pass 1"}
    readable = cd.authoring_prompt(prompt, authoring)
    assert "6" not in prompt and "6" in readable  # never merged into what runs
    cleaned, rejected = cd.validate_binding_changes(view, {"binding:steps:value": "31"}, prompt=readable, structural="canvas")
    assert not rejected and cleaned == {"binding:steps:value": 31}
    _, refused = cd.validate_binding_changes(view, {"binding:steps:value": "500"}, prompt=readable, structural="canvas")
    assert refused and "maximum" in refused[0]["reason"]
    # Not missing, so cleanup never offers it.
    assert control_cleanup.inventory(view)["bindings"] == []


def test_partial_participation_and_parent_reasons():
    prompt = base_prompt()
    prompt["9"] = relay({"both": {"label": "", "status": "ok", "targets": [{"path": "6", "input": "steps"},
                                                                         {"path": "5:2", "input": "steps"}]}})
    view = describe(prompt, authoring={"5:2": inactive_body(by="parent", label="Upscale")})
    both = view["bindings"][0]
    assert both["status"] == "ok" and both["participation"] == "partial"
    assert both["inactive_reason"] == {"mode": "muted", "by": "parent", "label": "Upscale"}
    assert describe(base_prompt() | {"9": relay({"s": _binding("6", "steps")})}, authoring={})["bindings"][0]["participation"] == "active"


def test_inactive_bodies_keep_structural_refusals():
    prompt = base_prompt()
    del prompt["6"]
    prompt["9"] = relay({"steps": _binding("6", "steps")})
    linked = describe(prompt, authoring={"6": inactive_body(inputs={"steps": ["3", 0], "seed": 5})})
    assert linked["bindings"][0]["status"] == "linked" and linked["bindings"][0]["participation"] == "inactive"
    assert describe(prompt, authoring={})["bindings"][0]["status"] == "missing"


@pytest.mark.parametrize("raw", [
    {"1": inactive_body()},                       # the prompt already holds this key
    {"6": {**inactive_body(), "mode": "on"}},
    {"6": {**inactive_body(), "inputs": []}},
    {"__proto__": inactive_body()},
    "nope",
])
def test_clean_authoring_refuses_collisions_and_malformed_bodies(raw):
    with pytest.raises(cd.ControlValueError):
        cd.clean_authoring(raw, base_prompt())
    with pytest.raises(cd.ControlValueError):
        cd.clean_authoring({str(i): inactive_body() for i in range(cd.MAX_AUTHORING_BODIES + 1)}, {})


def test_an_imported_file_without_the_body_explains_how_to_recover():
    prompt = base_prompt()
    del prompt["6"]
    prompt["9"] = relay({"steps": {**_binding("6", "steps"), "participation": "inactive",
                                   "inactive_reason": {"mode": "bypassed", "by": "self", "label": "Pass 1"}}})
    steps = describe(prompt)["bindings"][0]  # the Sonder App: no authoring snapshot
    assert steps["status"] == "missing" and "bypassed when this workflow was exported" in steps["reason"]
    assert "participation" not in steps


def test_run_provenance_never_records_an_inactive_setting():
    prompt = base_prompt()
    del prompt["6"]
    prompt["9"] = relay({"steps": {**_binding("6", "steps"), "participation": "inactive"},
                         "text": _binding("7", "text", label="Text")})
    section = controls_snapshot.controls_snapshot(prompt, ["7"], class_lookup=CLASSES.get)
    assert section is not None and "Text" in section["fields"] and len(section["fields"]) == 1


# ── Node switches ─────────────────────────────────────────────────────────────

def switches_view(prompt):
    return describe(prompt, authoring={})


def test_switch_descriptors_check_members_against_the_prompt():
    prompt = base_prompt()
    prompt["9"] = relay(execution={
        "node": switch(members=[member("6", title="Pass 1")]),
        "gone": switch(members=[member("6", "muted")]),          # muted per export, yet in the prompt
        "lost": switch(members=[member("44")]),                   # enabled per export, absent
        "infra": switch(members=[member("1", class_type="SonderEditor")]),
        "group": switch("group", [member("6"), member("40", "bypassed")], group_id=3, title="First pass"),
        "empty": switch("group", [], group_id=4),
        "many": switch("group", [member("6")], group_id=5, member_count=65),
        "kind": switch("instance", [member("6")]),
        "inst": switch("instance", [member("20", class_type="uuid-sub", kind="instance")]),
        "missing": switch(status="missing"),
    })
    prompt["20:1"] = _node("KSampler", {"steps": 1})
    by_id = {d["execution_id"]: d for d in switches_view(prompt)["execution"]}
    assert by_id["node"]["status"] == "ok" and by_id["node"]["mode"] == "enabled" and by_id["node"]["label"] == "Pass 1"
    assert by_id["gone"]["status"] == "missing" and by_id["lost"]["status"] == "missing"
    assert by_id["infra"]["status"] == "unsupported" and "Editor" in by_id["infra"]["reason"]
    assert by_id["group"]["mode"] == "mixed" and by_id["group"]["label"] == "First pass" and by_id["group"]["group_id"] == 3
    assert by_id["empty"]["status"] == "unsupported" and by_id["many"]["status"] == "unsupported"
    assert by_id["kind"]["status"] == "unsupported" and by_id["inst"]["status"] == "ok"
    assert by_id["missing"]["status"] == "missing"
    assert ce.mode_summary(by_id["group"]) == "Mixed (1 of 2 off)"


def test_a_damaged_section_reports_without_hiding_settings():
    prompt = base_prompt()
    prompt["9"] = _node("SonderControlRelay", {"manifest": json.dumps({"v": 1, "bindings": {"s": _binding("6", "steps")},
                                                                       "execution": {"v": 7}})})
    view = switches_view(prompt)
    assert view["bindings"][0]["status"] == "ok" and view["execution"] == [] and "another Sonder version" in view["execution_error"]


def test_only_a_running_relay_carries_controls():
    # An authoring body never stands in for the Relay: a muted Relay has no Controls.
    prompt = base_prompt()
    body = {**relay({"s": _binding("6", "steps")}), "mode": "muted", "by": "self", "label": "Relay"}
    view = describe(prompt, authoring=cd.clean_authoring({"9": body}, prompt))
    assert view["relay"]["status"] != "ok" and view["bindings"] == []


def test_budget_and_stale_switches_read_their_own_reasons():
    prompt = base_prompt()
    prompt["9"] = relay(execution={"big": switch("group", [], status="unsupported", group_id=3, problem="budget"),
                                   "stale": switch(members=[member("6", "muted")])})
    view = switches_view(prompt)
    by_id = {d["execution_id"]: d for d in view["execution"]}
    assert by_id["big"]["status"] == "unsupported" and by_id["big"]["reason"] == ce.REASONS["budget"]
    assert by_id["stale"]["status"] == "missing" and by_id["stale"]["problem"] == "stale"
    # The canvas still resolves a stale switch, so Cleanup never offers it.
    assert control_cleanup.inventory(view)["execution"] == []


def switch_prompt():
    prompt = base_prompt()
    prompt["8"] = _node("KSampler", {"steps": 4})
    prompt["9"] = relay(execution={"a": switch(members=[member("6")]),
                                   "g": switch("group", [member("6"), member("8")], group_id=2)})
    return prompt


def test_mode_changes_validate_and_one_node_takes_one_mode():
    view = switches_view(switch_prompt())
    cleaned, rejected = cd.validate_binding_changes(view, {"execution:a:mode": "muted"}, structural="canvas")
    assert cleaned == {"execution:a:mode": "muted"} and not rejected
    assert cd.validate_binding_changes(view, {"execution:a:mode": "off"}, structural="canvas")[1]
    # The per-member form is a preset's own expansion only.
    assert cd.validate_binding_changes(view, {"execution:g:modes": {"6": "muted", "8": "enabled"}}, structural="canvas")[1]
    assert not cd.validate_binding_changes(view, {"execution:g:modes": {"6": "muted", "8": "enabled"}},
                                           structural="canvas", expanded=True)[1]
    assert cd.validate_binding_changes(view, {"execution:g:modes": {"6": "muted"}}, structural="canvas", expanded=True)[1]
    _, clash = cd.validate_binding_changes(view, {"execution:a:mode": "muted", "execution:g:mode": "bypassed"},
                                           structural="canvas")
    assert clash and "differently" in clash[0]["reason"]
    assert not cd.validate_binding_changes(view, {"execution:a:mode": "muted", "execution:g:mode": "muted"},
                                           structural="canvas")[1]
    assert cd.validate_binding_changes(view, {"execution:a:label": "Skip pass one"}, structural="canvas")[0]
    assert cd.parse_target("execution:a:mode") == ("execution", "a", "mode") and cd.is_structural("execution:a:expose")


def candidate(kind="node", path="6", group_id=None, classes=("KSampler",), count=1, anchored_by=None, title="Pass 1"):
    entry = {"kind": kind, "title": title, "classes": list(classes), "member_count": count, "anchored_by": anchored_by}
    entry.update({"group_id": group_id} if kind == "group" else {"path": path})
    return entry


def test_structural_switch_targets():
    view = switches_view(switch_prompt())
    inventory = ce.clean_candidates([candidate(path="7"), candidate(path="1", classes=["SonderEditor"]),
                                     candidate(path="6", anchored_by="a"), candidate("group", group_id=9, count=3),
                                     candidate("group", group_id=2, count=2)])
    validate = lambda changes, mode="canvas": cd.validate_binding_changes(view, changes, structural=mode, execution_inventory=inventory)
    cleaned, rejected = validate({"execution:new:expose": {"candidate": {"kind": "node", "path": "7"}}})
    assert not rejected and cleaned["execution:new:expose"]["candidate"] == {"kind": "node", "path": "7"}
    assert "already a switch" in validate({"execution:n2:expose": {"candidate": {"kind": "node", "path": "6"}}})[1][0]["reason"]
    assert "Editor" in validate({"execution:n3:expose": {"candidate": {"kind": "node", "path": "1"}}})[1][0]["reason"]
    assert "already a switch" in validate({"execution:n4:expose": {"candidate": {"kind": "group", "group_id": 2}}})[1][0]["reason"]
    assert not validate({"execution:n5:expose": {"candidate": {"kind": "group", "group_id": 9}}})[1]
    assert validate({"execution:x:expose": {"candidate": {"kind": "node", "path": "7"}},
                     "execution:y:expose": {"candidate": {"kind": "node", "path": "7"}}})[1]
    assert validate({"execution:a:remove": True})[0] == {"execution:a:remove": {"remove": True}}
    assert validate({"execution:zz:remove": True})[0] == {"execution:zz:remove": {"noop": True}}
    assert "off can be rebound" in validate({"execution:a:rebind": {"candidate": {"kind": "node", "path": "7"}}})[1][0]["reason"]
    # The mounted pre-check is shape only; the canvas writer rechecks.
    assert not validate({"execution:q:expose": {"candidate": {"kind": "node", "path": "99"}}}, mode="relay")[1]
    # A retried expose whose first attempt landed changes nothing.
    assert validate({"execution:a:expose": {"candidate": {"kind": "node", "path": "6"}}})[0] == {"execution:a:expose": {"noop": True}}
    assert validate({"execution:g:expose": {"candidate": {"kind": "group", "group_id": 2}}})[0] == {"execution:g:expose": {"noop": True}}
    assert "already in use" in validate({"execution:a:expose": {"candidate": {"kind": "node", "path": "7"}}})[1][0]["reason"]
    assert validate({"execution:q:expose": {"candidate": {"kind": "node", "path": "a:b"}}}, mode="relay")[1]
    listed, _ = ce.manage_candidates(inventory, view, query="")
    assert [c.get("path", c.get("group_id")) for c in listed] == ["7", 9]
    with_reason, _ = ce.manage_candidates(inventory, view, query="Pass")
    assert any(c.get("reason") for c in with_reason)


def test_the_sonder_app_cannot_switch_an_export_without_routing():
    # An export made before routing capsules: the app can't know what a
    # bypass routes, so its switches stay read-only with how to fix it.
    prompt = switch_prompt()
    view = switches_view(prompt)
    ce.apply_sdk_state(view, prompt, {}, set())
    assert all(d["read_only"] and d["reason"] == ce.REEXPORT_REASON for d in view["execution"])
    _, rejected = cd.validate_binding_changes(view, {"execution:a:mode": "muted"}, structural="sdk")
    assert rejected and rejected[0]["reason"] == ce.REEXPORT_REASON
    assert cd.validate_binding_changes(view, {"execution:a:label": "x"}, structural="sdk")[1]
    _, structural = cd.validate_binding_changes(view, {"execution:new:expose": {"candidate": {"kind": "node", "path": "7"}}},
                                                structural="sdk")
    assert structural[0]["reason"] == ce.SDK_STRUCTURAL_REASON


# ── Presets with modes ────────────────────────────────────────────────────────

def preset_view(presets, prompt=None):
    prompt = prompt or switch_prompt()
    manifest = json.loads(prompt["9"]["inputs"]["manifest"])
    manifest["bindings"] = {"steps": _binding("6", "steps")}
    manifest["presets"] = presets
    prompt["9"] = _node("SonderControlRelay", {"manifest": json.dumps(manifest)})
    view = switches_view(prompt)
    view["presets"] = cd.preset_states(view, view["_presets"], prompt=prompt)
    return view, prompt


def test_capture_switch_modes_with_identity_and_exact_group_members():
    view, prompt = preset_view({})
    changes = {"preset:p:create": {"name": "Skip", "members": ["steps"], "execution": ["a", "g"]}}
    cleaned, rejected = cp.validate_preset_changes(view, changes, prompt=prompt, structural="canvas")
    assert not rejected
    entry = cleaned["preset:p:create"]
    assert entry["members"] == {"steps": {"value": "20"}}
    assert entry["execution"] == {"a": {"mode": "enabled", "node": {"path": "6", "class_type": "KSampler"}},
                                  "g": {"modes": {"6": "enabled", "8": "enabled"}}}
    assert cm.read_presets({"p": entry})["p"]["execution"] == entry["execution"]
    only = cp.validate_preset_changes(view, {"preset:q:create": {"name": "Q", "members": [], "execution": ["a"]}},
                                      prompt=prompt, structural="canvas")
    assert not only[1] and "steps" not in only[0]["preset:q:create"]["members"]
    assert cp.validate_preset_changes(view, {"preset:r:create": {"name": "R", "members": [], "execution": []}},
                                      prompt=prompt, structural="canvas")[1]


def skip_preset(group_modes=None, node_mode="muted", path="6"):
    return {"name": "Skip", "order": 0, "members": {"steps": {"value": "20"}},
            "execution": {"a": {"mode": node_mode, "node": {"path": path, "class_type": "KSampler"}},
                          "g": {"modes": group_modes or {"6": node_mode, "8": "bypassed"}}}}


def test_apply_expands_values_and_modes_into_one_batch():
    view, prompt = preset_view({"p": skip_preset()})
    cleaned, rejected = cp.validate_preset_changes(view, {"preset:p:apply": True}, prompt=prompt, structural="canvas")
    assert not rejected
    assert cleaned == {"binding:steps:value": 20, "execution:a:mode": "muted",
                       "execution:g:modes": {"6": "muted", "8": "bypassed"}}
    assert view["presets"][0]["state"] == "differs"
    assert [m["preview"] for m in view["presets"][0]["execution"]] == ["Muted", "Mixed (2 of 2 off)"]


def test_matching_drift_and_conflicts():
    view, _ = preset_view({"p": skip_preset(node_mode="enabled", group_modes={"6": "enabled", "8": "enabled"})})
    assert view["presets"][0]["state"] == "match"
    drifted, _ = preset_view({"p": skip_preset(group_modes={"6": "muted"})})
    row = drifted["presets"][0]
    assert row["state"] == "unavailable" and "g" in row["unavailable"]
    assert "changed since this choice was captured. Update the choice" in row["reason"]
    moved, _ = preset_view({"p": skip_preset(path="8", group_modes={"6": "muted", "8": "muted"})})
    assert "a" in moved["presets"][0]["unavailable"]
    # A node one switch mutes and its group bypasses: unreadable, never half-applied.
    assert "p" not in cm.read_presets({"p": skip_preset(group_modes={"6": "bypassed", "8": "muted"})})


def test_effects_budget_counts_distinct_nodes():
    prompt = base_prompt()
    for i in range(10, 74):
        prompt[str(i)] = _node("KSampler", {"steps": 1})
    prompt["9"] = relay({"steps": _binding("6", "steps")},
                        execution={"g": switch("group", [member(str(i)) for i in range(10, 74)], group_id=1)})
    view = switches_view(prompt)
    _, rejected = cp.validate_preset_changes(view, {"preset:p:create": {"name": "Big", "members": ["steps"], "execution": ["g"]}},
                                             prompt=prompt, structural="canvas")
    assert rejected and "each node a switch sets" in rejected[0]["reason"]
    assert not cp.validate_preset_changes(view, {"preset:p:create": {"name": "Big", "members": [], "execution": ["g"]}},
                                          prompt=prompt, structural="canvas")[1]


def test_sonder_app_presets_with_unroutable_switches_read_unavailable():
    view, prompt = preset_view({"p": skip_preset()})
    ce.apply_sdk_state(view, prompt, {}, set())  # no routing in this export
    rows = cd.preset_states(view, view["_presets"], prompt=prompt)
    assert rows[0]["state"] == "unavailable"


# ── Cleanup and readiness ─────────────────────────────────────────────────────

def test_cleanup_offers_missing_switches_and_their_preset_members():
    prompt = switch_prompt()
    manifest = json.loads(prompt["9"]["inputs"]["manifest"])
    manifest["execution"]["controls"]["lost"] = switch(status="missing")
    manifest["presets"] = {"p": {"name": "P", "members": {}, "execution": {
        "lost": {"mode": "muted", "node": {"path": "6", "class_type": "KSampler"}},
        "ghost": {"mode": "muted", "node": {"path": "6", "class_type": "KSampler"}}}}}
    prompt["9"] = _node("SonderControlRelay", {"manifest": json.dumps(manifest)})
    view = switches_view(prompt)
    view["cleanup"] = inventory = control_cleanup.inventory(view)
    assert inventory["execution"] == [{"id": "lost", "label": "Node"}]
    assert {(r["preset_id"], r.get("execution_id"), r.get("requires_execution")) for r in inventory["preset_members"]} == {
        ("p", "lost", "lost"), ("p", "ghost", None)}
    plan = control_cleanup.validate(view, {"execution": ["lost"], "preset_members": [
        {"preset_id": "p", "execution_id": "lost"}, {"preset_id": "p", "execution_id": "ghost"}]}, mode="canvas")
    assert plan["execution"] == ["lost"]
    with pytest.raises(cd.ControlValueError):  # a member whose switch stays needs the switch selected
        control_cleanup.validate(view, {"preset_members": [{"preset_id": "p", "execution_id": "lost"}]}, mode="canvas")
    presets = {"p": {"name": "P", "members": {}, "execution": {"lost": {}, "ghost": {}}}}
    assert control_cleanup.remove_members(presets, plan["preset_members"]) == {"p"} and presets == {}


def test_export_readiness_lists_what_an_export_leaves_out():
    prompt = switch_prompt()
    del prompt["8"]
    manifest = json.loads(prompt["9"]["inputs"]["manifest"])
    manifest["execution"]["controls"]["g"]["members"][1]["mode"] = "bypassed"
    manifest["bindings"] = {"steps": {**_binding("6", "steps"), "label": "Steps"}}
    prompt["9"] = _node("SonderControlRelay", {"manifest": json.dumps(manifest)})
    del prompt["6"]
    manifest["execution"]["controls"]["a"]["members"][0]["mode"] = "muted"
    manifest["execution"]["controls"]["g"]["members"][0]["mode"] = "muted"
    prompt["9"] = _node("SonderControlRelay", {"manifest": json.dumps(manifest)})
    view = describe(prompt, authoring={"6": inactive_body()})
    ready = ce.export_readiness(view)
    assert [s["label"] for s in ready["settings"]] == ["Steps"]
    assert {s["execution_id"] for s in ready["execution"]} == {"a", "g"}


def test_launcher_warns_about_switches_off_at_export():
    from server import sdk_templates
    prompt = switch_prompt()
    manifest = json.loads(prompt["9"]["inputs"]["manifest"])
    manifest["execution"]["controls"]["g"]["members"][1]["mode"] = "muted"
    prompt["9"] = _node("SonderControlRelay", {"manifest": json.dumps(manifest)})
    del prompt["8"]
    view = describe(prompt)
    ce.apply_sdk_state(view, prompt, {}, set())
    _, warnings = sdk_templates.controls_summary_of(view)
    assert any("can't be switched here: Its nodes were off when this workflow was exported" in w for w in warnings)


# ── Describe route ────────────────────────────────────────────────────────────

def test_describe_route_reads_inactive_bodies_lists_switch_candidates_and_readiness(monkeypatch):
    from server import routes
    original = cd.describe_bindings
    monkeypatch.setattr(cd, "describe_bindings", lambda p, **kw: original(p, class_lookup=CLASSES.get, **kw))
    prompt = switch_prompt()
    del prompt["6"]
    manifest = json.loads(prompt["9"]["inputs"]["manifest"])
    manifest["bindings"] = {"steps": {**_binding("6", "steps"), "label": "Steps"}}
    manifest["execution"]["controls"]["a"]["members"][0]["mode"] = "muted"
    manifest["execution"]["controls"]["g"]["members"][0]["mode"] = "muted"
    prompt["9"] = _node("SonderControlRelay", {"manifest": json.dumps(manifest)})
    body = {"prompt": prompt, "inactive": {"6": inactive_body()}, "manage": True, "filter": "",
            "candidates": [], "execution_candidates": [candidate(path="7", title="Text")],
            "changes": {"binding:steps:value": "31", "execution:a:mode": "enabled"}}
    view = routes._describe_controls_body(json.dumps(body).encode())
    assert view["ok"] and view["changes"] == {"binding:steps:value": "31", "execution:a:mode": "enabled"}
    assert view["bindings"][0]["participation"] == "inactive"
    assert [c["path"] for c in view["execution_candidates"]] == ["7"]
    assert view["export_readiness"]["settings"][0]["label"] == "Steps"
    # A body for a node the prompt holds is refused: the prompt decides what runs.
    with pytest.raises(routes._ControlsRequestError):
        routes._describe_controls_body(json.dumps({**body, "inactive": {"7": inactive_body()}}).encode())


def test_preset_apply_expands_through_the_route_into_one_validated_batch(monkeypatch):
    from server import routes
    _, prompt = preset_view({"p": skip_preset()})
    original = cd.describe_bindings
    monkeypatch.setattr(cd, "describe_bindings", lambda p, **kw: original(p, class_lookup=CLASSES.get, **kw))
    body = {"prompt": prompt, "inactive": {}, "execution_candidates": [], "changes": {"preset:p:apply": True}}
    view = routes._describe_controls_body(json.dumps(body).encode())
    assert view["ok"], view.get("rejected")
    # Values come back in display form, as for every route batch.
    assert view["changes"] == {"binding:steps:value": "20", "execution:a:mode": "muted",
                               "execution:g:modes": {"6": "muted", "8": "bypassed"}}
    # The per-member form never comes from a client.
    raw = {**body, "changes": {"execution:g:modes": {"6": "muted", "8": "bypassed"}}}
    assert not routes._describe_controls_body(json.dumps(raw).encode())["ok"]
