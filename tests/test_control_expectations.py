"""Controls edits carry what their gesture saw (control_expectations): an
unrelated save since then passes; the same setting, its inputs, or a clicked
Mode's effect changing refuses with its own reason."""
import json

import pytest

from server import control_descriptors as cd, control_expectations as ce
from test_control_descriptors import CLASSES, _binding, _node
from test_control_execution import preset_view, switch_prompt, switches_view


def steps_view(steps=20, extra=None):
    prompt = switch_prompt()
    prompt["6"]["inputs"]["steps"] = steps
    manifest = json.loads(prompt["9"]["inputs"]["manifest"])
    manifest["bindings"] = {"steps": _binding("6", "steps"), **(extra or {})}
    prompt["9"] = _node("SonderControlRelay", {"manifest": json.dumps(manifest)})
    return switches_view(prompt), prompt


STEPS = {"targets": [["6", "steps"]], "prior": "20"}


def check(view, changes, expect):
    return ce.check(view, changes, ce.clean(expect, changes))


def test_an_unrelated_change_passes_and_the_same_setting_changed_elsewhere_refuses():
    view, _ = steps_view(20)
    assert check(view, {"binding:steps:value": "31"}, {"binding:steps:value": STEPS}) == []
    view, _ = steps_view(25)
    refused = check(view, {"binding:steps:value": "31"}, {"binding:steps:value": STEPS})
    assert refused[0]["target"] == "binding:steps:value" and "changed to 25 elsewhere" in refused[0]["reason"]
    # The same edit landed from elsewhere: nothing to protect.
    view, _ = steps_view(31)
    assert check(view, {"binding:steps:value": "31"}, {"binding:steps:value": STEPS}) == []


def test_a_setting_that_now_sets_another_input_or_became_linked_refuses():
    view, _ = steps_view(20)
    moved = {"targets": [["8", "steps"]], "prior": "20"}
    assert "now sets a different input" in check(view, {"binding:steps:value": "31"}, {"binding:steps:value": moved})[0]["reason"]
    # Linked: batch validation owns the reason; the check leaves it alone.
    prompt = switch_prompt()
    prompt["6"]["inputs"]["steps"] = ["7", 0]
    manifest = json.loads(prompt["9"]["inputs"]["manifest"])
    manifest["bindings"] = {"steps": _binding("6", "steps")}
    prompt["9"] = _node("SonderControlRelay", {"manifest": json.dumps(manifest)})
    linked = switches_view(prompt)
    assert cd.validate_binding_changes(linked, {"binding:steps:value": "31"}, prompt=prompt)[1]
    assert check(linked, {"binding:steps:value": "31"}, {"binding:steps:value": STEPS}) == []


def test_a_switch_compares_its_nodes_and_mode():
    view = switches_view(switch_prompt())
    members = {"prior": "enabled", "members": ["6", "8"]}
    assert check(view, {"execution:g:mode": "muted"}, {"execution:g:mode": members}) == []
    assert "covers different nodes" in check(view, {"execution:g:mode": "muted"},
                                             {"execution:g:mode": {**members, "members": ["6"]}})[0]["reason"]
    assert "switched to Enabled elsewhere" in check(view, {"execution:g:mode": "muted"},
                                                    {"execution:g:mode": {**members, "prior": "bypassed"}})[0]["reason"]


def test_a_mode_click_refuses_when_its_effect_changed_after_the_click():
    preset = {"name": "Mask Off", "members": {}, "execution": {"g": {"modes": {"6": "bypassed"}}}}
    view, _ = preset_view({"off": preset})
    token = view["presets"][0]["token"]
    assert isinstance(token, str) and len(token) == ce.TOKEN_CHARS
    assert check(view, {"preset:off:apply": True}, {"preset:off:apply": {"token": token}}) == []
    # A node joins the group after the click: the effect is no longer the clicked one.
    switch = next(d for d in view["execution"] if d["execution_id"] == "g")
    switch["members"] = switch["members"][:1]
    from server import control_descriptors
    view["presets"] = control_descriptors.preset_states(view, view["_presets"])
    assert view["presets"][0]["token"] != token
    refused = check(view, {"preset:off:apply": True}, {"preset:off:apply": {"token": token}})
    assert "changed after you clicked it" in refused[0]["reason"] and "Mask Off" in refused[0]["reason"]


def test_an_unavailable_choice_has_no_token():
    view, _ = preset_view({"off": {"name": "Off", "members": {"gone": {"value": 1}}, "execution": {}}})
    assert view["presets"][0]["state"] == "unavailable" and view["presets"][0]["token"] is None


def test_an_advancing_seed_and_a_row_list_skip_the_value_comparison():
    prompt = switch_prompt()
    prompt["6"]["inputs"]["seed"] = 99
    manifest = json.loads(prompt["9"]["inputs"]["manifest"])
    manifest["bindings"] = {"seed": _binding("6", "seed", seed_mode="increment")}
    prompt["9"] = _node("SonderControlRelay", {"manifest": json.dumps(manifest)})
    expect = {"binding:seed:value": {"targets": [["6", "seed"]], "prior": "5"}}
    view = switches_view(prompt)
    assert next(d for d in view["bindings"] if d["binding_id"] == "seed")["seed_mode"] == "increment"
    # A run advanced it between the gesture and the write: not a conflict.
    assert check(view, {"binding:seed:value": "7"}, expect) == []
    manifest["bindings"] = {"seed": _binding("6", "seed", seed_mode="fixed")}
    prompt["9"] = _node("SonderControlRelay", {"manifest": json.dumps(manifest)})
    assert "changed to 99 elsewhere" in check(switches_view(prompt), {"binding:seed:value": "7"}, expect)[0]["reason"]


@pytest.mark.parametrize("expect", [
    "x", {"binding:other:value": STEPS}, {"binding:steps:value": {"prior": 1}},
    {"binding:steps:value": {"targets": [["6"]], "prior": 1}}, {"binding:steps:label": {"targets": []}},
    {"execution:g:mode": {"prior": "off", "members": []}}, {"preset:p:apply": {"token": ""}},
    {"binding:steps:value": {"targets": [["6", "steps"]] * 65}},
    {"binding:steps:value": {"targets": [["6", "steps"]], "prior": "x" * (ce.MAX_EXPECT_BYTES + 1)}},
])
def test_malformed_expectations_are_refused(expect):
    changes = {"binding:steps:value": "31", "binding:steps:label": "S", "execution:g:mode": "muted",
               "preset:p:apply": True}
    with pytest.raises(cd.ControlValueError):
        ce.clean(expect, changes)
    assert ce.clean(None, changes) == {}


def test_the_describe_route_refuses_a_conflicting_edit_and_passes_an_unrelated_one(monkeypatch):
    from server import routes
    _, prompt = steps_view(20)
    original = cd.describe_bindings
    monkeypatch.setattr(cd, "describe_bindings", lambda prompt, relay_census=None, **kw: original(
        prompt, relay_census=relay_census, class_lookup=CLASSES.get, **kw))
    body = {"prompt": prompt, "relay_census": {"active": 1}, "changes": {"binding:steps:value": "31"},
            "expect": {"binding:steps:value": STEPS}}
    assert routes._describe_controls_body(json.dumps(body).encode("utf-8"))["ok"] is True
    prompt["6"]["inputs"]["steps"] = 25
    result = routes._describe_controls_body(json.dumps(body).encode("utf-8"))
    assert result["ok"] is False and "changed to 25 elsewhere" in result["rejected"][0]["reason"]
    body["expect"] = {"binding:steps:value": {"prior": 1}}
    assert routes._describe_controls_body(json.dumps(body).encode("utf-8"))["rejected"][0]["reason"] == "Invalid expected state."


def test_a_mode_token_covers_the_inputs_its_settings_write():
    """A rebind between the click and the write moves a setting's input: the click no longer applies."""
    preset = {"name": "Fast", "members": {"steps": {"value": "8"}}, "execution": {}}
    view, _ = preset_view({"fast": preset})
    token = view["presets"][0]["token"]
    steps = next(d for d in view["bindings"] if d["binding_id"] == "steps")
    steps["targets"] = [{"path": "8", "input": "steps"}]
    from server import control_descriptors
    view["presets"] = control_descriptors.preset_states(view, view["_presets"])
    assert view["presets"][0]["token"] != token
    assert "changed after you clicked it" in check(view, {"preset:fast:apply": True},
                                                   {"preset:fast:apply": {"token": token}})[0]["reason"]
