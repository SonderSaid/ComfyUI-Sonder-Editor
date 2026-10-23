"""Phase 1B: node switches in the Sonder App, through the runner and sidecar.

The export carries a routing capsule (control_routing); the app stores node
modes in its sidecar, compiles them into each run, keeps settings on switched-
off nodes editable, skips their seeds, and refuses when no Sonder save runs.
"""

import asyncio
import json
import sys
import types

import pytest

from server import control_execution as ce, sdk_runs, sdk_templates
from server.sdk_runner import SdkError
from test_control_descriptors import CLASSES, _binding, _node, describe
from test_sdk_runner import World

ROUTING = {"v": 1,
           "nodes": {"6": {"i": [], "o": [["LATENT"]]},
                     "7": {"i": [["latent_image", "LATENT", {"m": ["6", 0, "LATENT"]}]], "o": [["LATENT"]]},
                     "2": {"i": [["project", "SONDER_PROJECT", {"a": ["1", 0]}]], "o": []}},
           "edges": [["7", "latent_image", "6", 0, "LATENT"], ["3", "samples", "7", 0, "LATENT"]]}


def member(path, class_type="KSampler", title=None):
    return {"path": path, "class_type": class_type, "mode": "enabled", "kind": "node", "title": title or class_type}


def switch(members, kind="node", **extra):
    target = {"kind": kind, **({"group_id": extra.pop("group_id")} if kind == "group" else {})}
    return {"label": "", "group": "", "order": 0, "advanced": False, "note": "", "target": target, "status": "ok",
            "members": members, "member_count": len(members), **extra}


def template(routing=ROUTING, presets=None, off_at_export=False):
    first = member("6", title="Pass 1")
    if off_at_export:
        first = {**first, "mode": "muted"}
    execution = {"v": 1, "controls": {
        "pass": switch([first]),
        "both": switch([member("6", title="Pass 1"), member("7", title="Pass 2")], kind="group", group_id=4,
                       title="Passes"),
        "save": switch([member("2", "SonderSaveVideo")]),
    }}
    if routing is not None:
        execution["routing"] = json.dumps(routing)
    manifest = {"v": 1, "bindings": {"steps": _binding("6", "steps", label="Steps"),
                                     "seed": _binding("6", "seed", label="Seed", seed_mode="increment"),
                                     "steps2": _binding("7", "steps", label="Pass 2 steps")},
                "notes": {}, "execution": execution, "presets": presets or {}}
    prompt = {"1": _node("SonderEditor", {"project": "P"}),
              "2": _node("SonderSaveVideo", {"project": ["1", 0], "mark_queue_complete": False}),
              "6": _node("KSampler", {"seed": 5, "steps": 20}, title="Pass 1"),
              "7": _node("KSampler", {"seed": 6, "steps": 10, "latent_image": ["6", 0]}, title="Pass 2"),
              "3": _node("TextNode", {"text": "x", "samples": ["7", 0]}),
              "8": _node("SonderControlRelay", {"manifest": json.dumps(manifest)})}
    if off_at_export:
        del prompt["6"]
        prompt["7"]["inputs"].pop("latent_image")
    return prompt


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(get_user_directory=lambda: str(tmp_path)))
    # Preset projection and the curated export describe with the node registry.
    from server import control_descriptors as cd
    monkeypatch.setattr(cd, "_comfy_class_lookup", CLASSES.get)
    sdk_runs.reset_for_tests()
    result = World()
    result.runner._describe = describe
    yield result
    result.runner.close()
    sdk_runs.reset_for_tests()


def install(world, prompt):
    sdk_templates.replace_template(world.template_id, json.dumps(prompt).encode(), validate=lambda p: {"ok": True})


def change(world, session, changes):
    return asyncio.run(world.runner.change_controls(session.session_id, session.secret, "tab-1", changes))


def open_session(world):
    return asyncio.run(world.open())


def generate(world, session):
    return asyncio.run(world.runner.generate(session, tab_session_id="tab-1", count=1))


def by_id(view, key, identity):
    return next(d for d in view[key] if d.get("binding_id" if key == "bindings" else "execution_id") == identity)


def test_a_switch_mutes_its_node_in_the_run_and_its_settings_stay_editable(world):
    install(world, template())
    session = open_session(world)
    view = change(world, session, {"execution:pass:mode": "muted"})["view"]
    assert sdk_templates.read_control_values(world.template_id)["node_modes"] == {"6": {"mode": "muted", "class_type": "KSampler"}}
    pass_switch, group = by_id(view, "execution", "pass"), by_id(view, "execution", "both")
    assert not pass_switch["read_only"] and pass_switch["mode"] == "muted"
    assert group["mode"] == "mixed"  # it shares node 6: the last action wins
    steps = by_id(view, "bindings", "steps")
    assert steps["participation"] == "inactive" and steps["inactive_reason"] == {"mode": "muted", "by": "self", "label": "Pass 1"}
    assert "participation" not in by_id(view, "bindings", "steps2")
    # Edit a setting on the muted node: it saves, and the node stays off.
    change(world, session, {"binding:steps:value": "31"})
    seed_before = sdk_templates.read_control_values(world.template_id)["bindings"].get("seed")
    generate(world, session)
    graph = world.client.submitted[-1]
    assert "6" not in graph and "latent_image" not in graph["7"]["inputs"] and graph["3"]["inputs"]["samples"] == ["7", 0]
    # Its seed did not run, so it neither advanced nor was written.
    stored = sdk_templates.read_control_values(world.template_id)["bindings"]
    assert stored.get("seed") == seed_before
    # Enabled again: the edited value runs.
    change(world, session, {"execution:pass:mode": "enabled"})
    generate(world, session)
    graph = world.client.submitted[-1]
    assert graph["6"]["inputs"]["steps"] == 31 and graph["7"]["inputs"]["latent_image"] == ["6", 0]
    assert sdk_templates.read_control_values(world.template_id)["node_modes"] == {}


def test_bypass_routes_through_and_a_group_sets_every_node(world):
    install(world, template())
    session = open_session(world)
    change(world, session, {"execution:both:mode": "bypassed"})
    assert sdk_templates.read_control_values(world.template_id)["node_modes"] == {"6": {"mode": "bypassed", "class_type": "KSampler"},
                                                                         "7": {"mode": "bypassed", "class_type": "KSampler"}}
    generate(world, session)
    graph = world.client.submitted[-1]
    # Pass 2 bypassed passes its LATENT input; Pass 1 bypassed has none to pass.
    assert "6" not in graph and "7" not in graph and "samples" not in graph["3"]["inputs"]


def test_presets_apply_values_and_switches_in_one_sidecar_write(world):
    presets = {"skip": {"name": "Skip first pass", "members": {"steps2": {"value": "12"}},
                        "execution": {"pass": {"mode": "muted", "node": {"path": "6", "class_type": "KSampler"}}}}}
    install(world, template(presets=presets))
    session = open_session(world)
    before = sdk_templates.read_control_values(world.template_id)["seq"]
    view = change(world, session, {"preset:skip:apply": True})["view"]
    stored = sdk_templates.read_control_values(world.template_id)
    assert stored["seq"] == before + 1 and stored["node_modes"] == {"6": {"mode": "muted", "class_type": "KSampler"}} and stored["bindings"]["steps2"]["value"] == 12
    assert view["presets"][0]["state"] == "match"
    change(world, session, {"execution:pass:mode": "enabled"})
    assert change(world, session, {"execution:pass:label": "First pass"})["view"]["presets"][0]["state"] == "differs"
    # A hidden switch's modes never run here, so a preset holding it can't apply.
    assert change(world, session, {"execution:pass:hide": True})["view"]["presets"][0]["state"] == "unavailable"


def test_generate_refuses_when_no_sonder_save_runs(world):
    install(world, template())
    session = open_session(world)
    change(world, session, {"execution:save:mode": "muted"})
    with pytest.raises(SdkError) as refused:
        generate(world, session)
    assert refused.value.code == "generate_refused"
    assert any("turn off every Sonder Save Video" in p for p in refused.value.payload()["problems"])


def test_hidden_switches_run_as_exported_and_structure_stays_on_the_canvas(world):
    install(world, template())
    session = open_session(world)
    change(world, session, {"execution:both:mode": "muted"})
    view = change(world, session, {"execution:both:hide": True})["view"]
    assert by_id(view, "execution", "both")["hidden"]
    # Node 7 is only the hidden group's, so it runs as exported; node 6 is
    # also a visible switch's, which still shows and applies its mode.
    assert by_id(view, "execution", "pass")["mode"] == "muted"
    generate(world, session)
    assert "6" not in world.client.submitted[-1] and "7" in world.client.submitted[-1]
    with pytest.raises(SdkError) as refused:
        change(world, session, {"execution:new:expose": {"candidate": {"kind": "node", "path": "3"}}})
    assert refused.value.payload()["rejected"][0]["reason"] == ce.SDK_STRUCTURAL_REASON
    # App presentation of a switch is an overlay, like a setting's.
    view = change(world, session, {"execution:both:label": "Both passes", "execution:both:advanced": True})["view"]
    assert by_id(view, "execution", "both")["label"] == "Both passes" and by_id(view, "execution", "both")["advanced"]
    assert by_id(change(world, session, {"execution:both:label": ""})["view"], "execution", "both")["label"] == "Passes"


@pytest.mark.parametrize("prompt,reason", [
    (lambda: template(routing=None), ce.REEXPORT_REASON),
    (lambda: template(routing={"v": 1, "error": "budget"}), ce.ROUTING_REASONS["budget"]),
    (lambda: template(off_at_export=True), ce.OFF_AT_EXPORT_REASON),
])
def test_switches_the_export_cannot_route_stay_read_only_with_the_fix(world, prompt, reason):
    install(world, prompt())
    session = open_session(world)
    view = asyncio.run(world.runner.controls_view(session))["view"]
    assert by_id(view, "execution", "pass")["read_only"] and by_id(view, "execution", "pass")["reason"] == reason
    with pytest.raises(SdkError):
        change(world, session, {"execution:pass:mode": "muted"})
    # The Launcher says so too.
    from server import control_descriptors as cd
    described = cd.describe_bindings(prompt(), class_lookup=CLASSES.get)
    ce.apply_sdk_state(described, prompt(), {}, set())
    _, warnings = sdk_templates.controls_summary_of(described)
    assert any(reason in w for w in warnings)


def test_curated_export_keeps_every_node_and_the_routing_and_takes_app_presentation(world):
    install(world, template())
    session = open_session(world)
    change(world, session, {"execution:pass:mode": "muted"})
    change(world, session, {"execution:both:label": "Both passes", "execution:save:hide": True})
    exported = json.loads(sdk_templates.export_curated(world.template_id))
    assert "6" in exported and exported["7"]["inputs"]["latent_image"] == ["6", 0]
    section = json.loads(exported["8"]["inputs"]["manifest"])["execution"]
    assert json.loads(section["routing"]) == ROUTING
    assert section["controls"]["both"]["label"] == "Both passes" and "save" not in section["controls"]


def test_a_reimport_that_renumbers_nodes_never_moves_a_mode(world):
    install(world, template())
    session = open_session(world)
    change(world, session, {"execution:pass:mode": "muted"})
    # The new export's node 6 is another kind of node under the same switch.
    renumbered = template()
    manifest = json.loads(renumbered["8"]["inputs"]["manifest"])
    manifest["execution"]["controls"]["pass"]["members"] = [member("6", "TextNode", title="Notes")]
    renumbered["8"]["inputs"]["manifest"] = json.dumps(manifest)
    renumbered["6"] = _node("TextNode", {"text": "x"})
    routing = json.loads(json.dumps(ROUTING))
    manifest["execution"]["routing"] = json.dumps(routing)
    renumbered["8"]["inputs"]["manifest"] = json.dumps(manifest)
    install(world, renumbered)
    view = asyncio.run(world.runner.controls_view(session))["view"]
    assert by_id(view, "execution", "pass")["mode"] == "enabled"
    generate(world, session)
    assert "6" in world.client.submitted[-1]


def test_an_app_export_drops_hidden_switches_from_presets_and_cleanup_repairs_old_ones(world):
    presets = {"skip": {"name": "Skip", "members": {}, "execution": {
                   "pass": {"mode": "muted", "node": {"path": "6", "class_type": "KSampler"}}}},
               "mixed": {"name": "Mixed", "members": {"steps2": {"value": "12"}}, "execution": {
                   "pass": {"mode": "muted", "node": {"path": "6", "class_type": "KSampler"}}}},
               "ghost": {"name": "Ghost", "members": {"steps2": {"value": "9"}}, "execution": {
                   "gone": {"mode": "muted", "node": {"path": "6", "class_type": "KSampler"}}}}}
    install(world, template(presets=presets))
    session = open_session(world)
    change(world, session, {"execution:pass:hide": True})
    exported = json.loads(json.loads(sdk_templates.export_curated(world.template_id))["8"]["inputs"]["manifest"])
    assert "skip" not in exported["presets"] and "execution" not in exported["presets"]["mixed"]
    # A member whose switch is gone is listed for Cleanup in the app.
    view = asyncio.run(world.runner.controls_view(session))["view"]
    assert {"preset_id": "ghost", "execution_id": "gone"} in view["cleanup"]["preset_members"]
    after = change(world, session, {"workflow:cleanup": {"preset_members": [{"preset_id": "ghost", "execution_id": "gone"}]}})
    ghost = next(p for p in after["view"]["presets"] if p["id"] == "ghost")
    assert ghost["execution"] == [] and ghost["state"] != "unavailable"


def test_an_instance_holding_the_editor_is_never_a_switch():
    prompt = {"20:1": _node("SonderEditor"), "20:2": _node("KSampler")}
    descriptor = ce.describe_one(prompt, "inst", {**switch([{**member("20", "uuid-sub"), "kind": "instance"}], kind="instance")})
    assert descriptor["status"] == "unsupported" and descriptor["problem"] == "infrastructure"
