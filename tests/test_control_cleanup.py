"""Cleanup's deletion boundary: fresh eligibility, per-member intent, one write."""
import asyncio
import copy
import json
import subprocess
from pathlib import Path

import pytest

from server import control_cleanup as cc, control_descriptors as cd
from test_control_presets import app_template, definition


def canvas_view():
    view = {"relay": {"status": "ok"}, "notes": [], "bindings": [
        {"binding_id": status, "label": status, "status": status} for status in
        ("missing", "ok", "linked", "no_widget", "unsupported", "duplicate", "shadowed", "denied")],
        "suggestions": [{"binding_id": "orphan"}],
        "_presets": {"p": definition({"missing": {"value": 1}, "gone": {"value": 2}, "ok": {"value": 3}})}}
    view["cleanup"] = cc.inventory(view, definition_anchors=["definition", "missing", "ok", "orphan"])
    return view


def validate(view, intent, mode="canvas"):
    return cd.validate_binding_changes(view, {"workflow:cleanup": intent}, structural=mode)


def test_inventory_only_missing_and_orphan_anchors_and_members():
    v = canvas_view()
    assert [r["id"] for r in v["cleanup"]["bindings"]] == ["missing"]
    assert [r["id"] for r in v["cleanup"]["definition_anchors"]] == ["definition"]
    assert [r["binding_id"] for r in v["cleanup"]["preset_members"]] == ["missing", "gone"]
    assert not validate(v, {"bindings": ["missing"], "preset_members": [{"preset_id": "p", "binding_id": "missing"}]})[1]
    assert validate(v, {"preset_members": [{"preset_id": "p", "binding_id": "missing"}]})[1]
    assert not validate(v, {"preset_members": [{"preset_id": "p", "binding_id": "gone"}]})[1]
    for status in ("ok", "linked", "no_widget", "unsupported", "duplicate", "shadowed", "denied"):
        assert validate(v, {"bindings": [status]})[1]


@pytest.mark.parametrize("mode", ["canvas", "sdk", "relay"])
@pytest.mark.parametrize("error", ["relay", "view", "none"])
def test_cleanup_refuses_unreadable_relay_in_every_host(mode, error):
    v = canvas_view()
    if error == "view": v["error"] = "Offline"
    else: v["relay"]["status"] = "none" if error == "none" else "error"
    assert validate(v, {"bindings": ["missing"]}, mode)[1]
    assert not any(cc.inventory(v).values())


def test_stale_selection_and_batch_shape_refused():
    v = canvas_view()
    v["bindings"][0]["status"] = "ok"
    v["cleanup"] = cc.inventory(v)
    assert validate(v, {"bindings": ["missing"]})[1]
    for intent in ({}, {"bindings": ["missing", "missing"]}, {"values": ["__proto__"]},
                   {"surprise": []}, {"preset_members": [{"preset_id": "p"}]}, {"hidden": ["binding:constructor"]}):
        assert validate(v, intent)[1]
    assert cd.validate_binding_changes(canvas_view(), {"workflow:cleanup": {"bindings": ["missing"]},
        "workflow:description": "x"}, structural="canvas")[1]
    assert cd.is_structural("workflow:cleanup")


def test_app_cleanup_one_mutation_keeps_authored_current_and_unchecked_members(app_template):
    st, identity, prompt, manifest, validation, snapshot, write = app_template
    def seed(data):
        data["local"]["lost"] = {"targets": [{"path": "404", "input": "steps", "class_type": "Sampler"}], "label": "Lost"}
        data["bindings"].update(old={"value": "18446744073709551615", "future": "keep until explicit"}, steps={"value": 30})
        data["hidden"].update({"binding:gone": True, "binding:seed": True})
        data["presets"]["p"] = definition({"lost": {"value": 1}, "old": {"value": 2}, "steps": {"value": 20}})
        data["unknown"] = {"leave": True}
    st.update_control_values(identity, seed)
    current, before, view = snapshot()
    assert [r["id"] for r in view["cleanup"]["bindings"]] == ["lost"]
    assert "18446744073709551615" in view["cleanup"]["values"][0]["preview"]
    source = st.export_curated(identity)
    write({"workflow:cleanup": {"bindings": ["lost"], "values": ["old"], "hidden": ["binding:gone"],
                               "preset_members": [{"preset_id": "p", "binding_id": "old"}]}})
    _, after, view = snapshot()
    assert after["seq"] == before["seq"] + 1
    assert not after["local"] and "old" not in after["bindings"]
    assert after["bindings"]["steps"] == before["bindings"]["steps"]
    assert after["hidden"] == {"binding:seed": True}
    assert set(after["presets"]["p"]["members"]) == {"lost", "steps"}
    assert view["presets"][0]["state"] == "unavailable"
    assert after["unknown"] == before["unknown"]
    # Removing all remaining dangling members is an explicit preset delete,
    # not a silently invalid zero-member definition.
    st.update_control_values(identity, lambda data: data["presets"].update(q=definition({"gone": {"value": 9}})))
    write({"workflow:cleanup": {"preset_members": [{"preset_id": "q", "binding_id": "gone"}]}})
    assert snapshot()[1]["presets"]["q"] == {"removed": True}


def test_app_cleanup_rechecks_under_storage_lock_and_refuses_whole_write(app_template):
    st, identity, prompt, manifest, validation, snapshot, write = app_template
    st.update_control_values(identity, lambda data: data["bindings"].update(old={"value": 4}))
    current, before, view = snapshot()
    cleaned, refused = validate(view, {"values": ["old"]}, "sdk")
    assert not refused
    st.update_control_values(identity, lambda data: data["local"].update(old={"targets": [{"path": "3", "input": "steps", "class_type": "Sampler"}]}))
    before = st.read_control_values(identity)
    with pytest.raises(st.TemplateError, match="no longer unavailable"):
        st.store_binding_changes(identity, cleaned, view, current)
    assert st.read_control_values(identity) == before


def test_app_authored_missing_stays(app_template):
    st, identity, prompt, manifest, validation, snapshot, write = app_template
    manifest["bindings"]["steps"]["status"] = "missing"
    prompt["9"]["inputs"]["manifest"] = json.dumps(manifest)
    st.replace_template(identity, json.dumps(prompt).encode(), validate=validation)
    view = snapshot()[2]
    assert not view["cleanup"]["bindings"]
    assert validate(view, {"bindings": ["steps"]}, "sdk")[1]


@pytest.mark.parametrize("origin", ["app", "workflow"])
def test_cleanup_preserves_raw_unselected_preset_members_and_metadata(app_template, origin):
    st, identity, prompt, manifest, validation, snapshot, write = app_template
    raw = definition({"gone": {"value": 1}, "steps": {"value": 20, "future": "kept"},
                      "unreadable": {"value": {"future": True}}}, future="kept")
    if origin == "app":
        st.update_control_values(identity, lambda data: data["presets"].update(q=copy.deepcopy(raw)))
    else:
        # An invalid scalar member stays within raw hygiene depth (object value
        # would fail manifest hygiene before the tolerant per-member reader).
        raw["members"]["unreadable"] = {"seed_mode": "future-mode"}
        manifest["presets"]["q"] = raw
        prompt["9"]["inputs"]["manifest"] = json.dumps(manifest)
        st.replace_template(identity, json.dumps(prompt).encode(), validate=validation)
    write({"workflow:cleanup": {"preset_members": [{"preset_id": "q", "binding_id": "gone"}]}})
    expected = copy.deepcopy(raw)
    expected["members"].pop("gone")
    assert snapshot()[1]["presets"]["q"] == expected


def test_saturated_cleanup_publication_keeps_every_reviewed_member():
    from server import control_manifest as cm
    raw = {"presets": {f"p{i}": definition({f"b{j}": {"value": j} for j in range(64)}, name="N" * 120)
                       for i in range(32)}}
    parsed = cm.parse_manifest(json.dumps(raw))
    view = {"relay": {"status": "ok"}, "bindings": [], "notes": [], "_presets": parsed["presets"]}
    view["presets"] = cd.preset_states(view, view["_presets"])
    view["cleanup"] = cc.inventory(view)
    module = (Path(__file__).resolve().parents[1] / "web/js/controls_canvas_relay.js").as_uri()
    script = f'''import assert from 'node:assert/strict';
    import {{fitPublicationView}} from {json.dumps(module)};
    const view=JSON.parse(await new Promise(r=>{{let s='';process.stdin.on('data',d=>s+=d);process.stdin.on('end',()=>r(s));}}));
    const fitted=fitPublicationView(view,{{view_seq:1,prompt_hash:'hash',applied:[],rejected:[]}});
    assert.ok(!fitted.error,fitted.error);assert.equal(fitted.cleanup.preset_members.length,2048);
    assert.ok(new TextEncoder().encode(JSON.stringify(fitted)).length < 512*1024);
    console.log(new TextEncoder().encode(JSON.stringify(fitted)).length);'''
    result = subprocess.run(["node", "--input-type=module", "-e", script], input=json.dumps(cd.json_safe(view)), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_mounted_forwards_original_cleanup_for_live_revalidation(monkeypatch):
    import server.session_registry as sr
    from test_controls_relay import registry, setup, publish, publication, post, pending
    registry.__wrapped__(monkeypatch)
    async def run():
        await setup()
        body = publication()
        body["view"] = canvas_view()
        assert (await publish(body))["ok"]
        changes = {"workflow:cleanup": {"bindings": ["missing"], "definition_anchors": ["definition"]}}
        result = await post(changes)
        assert result["ok"], result
        assert (await pending())["pending"][0]["changes"] == changes
    asyncio.run(run())
