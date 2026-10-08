"""Manage (Phase 3B): candidates, structural targets and Sonder App curation (A1).

Server half. The panel, canvas writer and Relay rows are covered by the JS
harnesses (test_controls_panel_js.py, test_controls_host_canvas_js.py,
test_control_relay_rows_js.py).
"""

import asyncio
import json

import pytest

from server import control_descriptors as cd, sdk_templates, session_registry as sr
from test_control_descriptors import CLASSES, _binding, _node, _relay, describe
from test_controls_relay import CANVAS, HID, PID, TAB, publication, registry, setup  # noqa: F401 (fixture)
from test_sdk_controls_routes import binding_of, client_for, headers, install, prompt, world  # noqa: F401 (fixture)




@pytest.mark.parametrize("changes", [
    {"binding:steps:remove": True},
    {"note:n1:remove": True},
    {"binding:gone:remove": True, "note:gone:remove": True},
    {"binding:steps:rebind": {"candidate": {"path": "6", "input": "steps"}}},
    {"binding:steps:expose": {"candidate": {"path": "6", "input": "steps"}}},
    {"note:n1:create": {"text": "Hi"}},
    {"binding:orphan:expose": {"section": "workflow"}},
])
def test_mounted_structural_intent_survives_both_validation_boundaries(registry, changes):
    async def run():
        await setup()
        graph = manage_graph()
        graph["8"] = _relay({"steps": _binding("6", "steps")}, notes={"n1": {"text": "Hi"}},
                            suggestions=[{"binding_id": "orphan", "targets": [{"path": "6", "input": "cfg"}]}])
        body = publication()
        body["view"] = describe(graph)
        assert (await sr.publish_controls_view(PID, HID, CANVAS, body))["ok"]
        queued = await sr.post_control_changes(PID, HID, TAB, changes)
        assert queued["ok"], queued
        pending = (await sr.get_control_changes(PID, HID, CANVAS))["pending"][0]["changes"]
        assert pending == changes  # no internal {remove:true} or {noop:true} on the wire
        cleaned, rejected = validate(graph, pending)
        assert not rejected, rejected
        assert cleaned
        # A client cannot forge the internal no-op result to skip validation.
        forged = {next(iter(changes)): {"noop": True}}
        if next(iter(changes)).endswith((":remove", ":rebind", ":create")):
            _, rejected = validate(graph, forged)
            assert rejected

    asyncio.run(run())


def test_a_real_rebind_of_an_active_item_is_still_refused_by_the_canvas():
    graph = manage_graph()
    changes = {"binding:steps:rebind": {"candidate": {"path": "6", "input": "cfg"}}}
    assert not cd.validate_binding_changes(describe(graph), changes, structural="relay")[1]
    assert "Only a setting that is off" in validate(graph, changes)[1][0]["reason"]


def test_restore_in_sdk_retires_the_suggestion_and_preserves_its_id():
    graph = manage_graph()
    cleaned, rejected = validate(graph, {"binding:orphan:expose": {"section": "workflow"}}, mode="sdk")
    assert not rejected
    entry = cleaned["binding:orphan:expose"]
    entry["targets"] = [{**t, "class_type": graph[t["path"]]["class_type"]} for t in entry["targets"]]
    view = describe(graph, local={"orphan": entry})
    assert binding_of(view, "orphan")["origin"] == "local"
    assert all(s["binding_id"] != "orphan" for s in view["suggestions"])
    assert validate(graph, {"binding:orphan:expose": {"section": "workflow"}},
                    mode="sdk", view=view)[0] == {"binding:orphan:expose": {"noop": True}}


def test_deleted_relay_anchors_are_restore_suggestions_only():
    graph = manage_graph()
    exported = graph.pop("8")["inputs"]["manifest"]
    view = describe(graph, orphan_manifest=exported)
    assert view["relay"]["status"] == "none" and not view["bindings"]
    assert [s["binding_id"] for s in view["suggestions"]] == ["orphan"]
    assert not cd.validate_binding_changes(view, {"binding:orphan:expose": {}},
                                           prompt=graph, structural="canvas", inventory=inventory(graph))[1]


def test_candidate_cache_separates_definition_authorities_and_nested_results():
    graph = manage_graph()
    good = inventory(graph)
    unavailable = cd.candidate_inventory(graph, cd.sdk_candidates(graph), class_lookup=lambda _: None)
    assert any(c["status"] == "ok" for c in good)
    assert not any(c["status"] == "ok" for c in unavailable)
    good[0]["targets"][0]["input"] = "changed"
    assert inventory(graph)[0]["targets"][0]["input"] != "changed"


def test_restore_suggestions_tolerate_malformed_or_missing_target_nodes():
    graph = manage_graph()
    graph["6"] = None
    view = describe(graph)
    assert view["suggestions"][0]["section"] == "workflow"


def test_concurrent_candidate_cache_evictions():
    from concurrent.futures import ThreadPoolExecutor
    def read(i):
        graph = manage_graph()
        graph["6"]["inputs"]["steps"] = i + 1
        return inventory(graph)
    with ThreadPoolExecutor(max_workers=12) as pool:
        assert all(pool.map(read, range(64)))


def inventory(graph, candidates=None):
    return cd.candidate_inventory(graph, candidates if candidates is not None else cd.sdk_candidates(graph),
                                  class_lookup=CLASSES.get)


def manage_graph():
    return {
        "1": _node("SonderEditor", {"project": "P", "selection_start": 0}),
        "2": _node("SonderSaveVideo", {"project": ["1", 0], "mark_queue_complete": False,
                                       "place_audio_on_timeline": True}, title="Save"),
        "6": _node("KSampler", {"seed": 5, "steps": 20, "cfg": 7.0, "model": ["4", 0]}, title="Sampler"),
        "7": _node("Multi", {"tags": ["a", "b"]}),
        "5:3": _node("KSampler", {"seed": 1, "steps": 9}),
        "8": _relay({"steps": _binding("6", "steps", label="Steps")},
                    suggestions=[{"binding_id": "orphan", "targets": [{"path": "6", "input": "cfg"}]}]),
    }


# ── Candidates ───────────────────────────────────────────────────────────────

def test_sdk_candidates_are_root_level_literals_only_and_skip_the_legacy_primitive():
    graph = manage_graph()
    graph["9"] = _node("PrimitiveNode", {"value": 3})
    found = {(c["path"], c["input"]) for c in cd.sdk_candidates(graph)}
    assert ("6", "steps") in found and ("6", "seed") in found
    assert ("6", "model") not in found and ("2", "project") not in found      # links
    assert not any(path == "5:3" for path, _ in found)                         # inside a subgraph
    assert not any(path == "9" for path, _ in found)                           # legacy PrimitiveNode


def test_inventory_checks_each_candidate_and_manage_lists_only_ok_unbound_ones_recommended_first():
    graph = manage_graph()
    entries = {(e["path"], e["input"]): e for e in inventory(graph)}
    assert entries[("6", "steps")]["status"] == "ok" and entries[("6", "steps")]["kind"] == "INT"
    assert entries[("6", "seed")]["kind"] == "SEED" and entries[("6", "seed")]["label"] == "Sampler · seed"
    assert entries[("1", "selection_start")]["status"] == "denied"
    assert entries[("7", "tags")]["status"] == "unsupported"
    advance = entries[("2", "mark_queue_complete")]
    assert advance["recommended"] and advance["section"] == "editor" and advance["label"] == "Advance queue after take"
    view = describe(graph)
    listed, truncated = cd.manage_candidates(inventory(graph), view)
    keys = [(c["path"], c["input"]) for c in listed]
    assert not truncated
    assert ("6", "steps") not in keys                       # already bound
    assert ("1", "selection_start") not in keys and ("7", "tags") not in keys   # nothing greyed out
    assert keys[:2] == [("2", "mark_queue_complete"), ("2", "place_audio_on_timeline")]
    filtered, _ = cd.manage_candidates(inventory(graph), view, query="SAMPLER")
    assert {c["input"] for c in filtered} == {"seed", "cfg"}
    capped, truncated = cd.manage_candidates(inventory(graph), view, limit=2)
    assert len(capped) == 2 and truncated


def test_a_promoted_recommended_field_is_recommended_too():
    graph = {"4:2": _node("SonderSaveVideo", {"project": "P", "mark_queue_complete": False,
                                              "place_audio_on_timeline": True})}
    entry = inventory(graph, [{"path": "4", "input": "mark_queue_complete", "node_title": "Encode",
                               "targets": [{"path": "4:2", "input": "mark_queue_complete"}]}])[0]
    assert entry["recommended"] and entry["section"] == "editor" and entry["node_title"] == "Encode"
    mixed = inventory(graph, [{"path": "4", "input": "x", "targets": [
        {"path": "4:2", "input": "mark_queue_complete"}, {"path": "4:2", "input": "place_audio_on_timeline"}]}])[0]
    assert not mixed["recommended"]


def test_inventory_is_cached_per_prompt_and_candidates():
    graph = manage_graph()
    calls = []

    def lookup(name):
        calls.append(name)
        return CLASSES.get(name)

    first = cd.candidate_inventory(graph, cd.sdk_candidates(graph), class_lookup=lookup)
    count = len(calls)
    assert cd.candidate_inventory(graph, cd.sdk_candidates(graph), class_lookup=lookup) == first
    assert len(calls) == count
    first[0]["label"] = "mutated"  # callers get copies
    assert cd.candidate_inventory(graph, cd.sdk_candidates(graph), class_lookup=lookup)[0]["label"] != "mutated"


def test_candidates_from_a_request_are_checked():
    assert cd.clean_candidates([{"path": "6", "input": "steps"}])[0]["targets"] == [{"path": "6", "input": "steps"}]
    for bad in ([{"path": "5:3", "input": "steps"}], [{"path": "6"}], "x",
                [{"path": "6", "input": "s", "targets": []}], [{"path": "6", "input": "s", "targets": [{"path": 6}]}]):
        with pytest.raises(cd.ControlValueError):
            cd.clean_candidates(bad)


# ── Structural targets ───────────────────────────────────────────────────────

def validate(graph, changes, mode="canvas", **kwargs):
    view = kwargs.pop("view", None) or describe(graph, **kwargs.pop("describe", {}))
    return cd.validate_binding_changes(view, changes, prompt=graph, structural=mode,
                                       inventory=kwargs.pop("inventory", inventory(graph)))


def test_expose_returns_targets_kind_default_label_and_the_next_order():
    cleaned, rejected = validate(manage_graph(), {
        "binding:new1:expose": {"candidate": {"path": "6", "input": "seed"}, "section": "workflow"},
        "binding:new2:expose": {"candidate": {"path": "2", "input": "mark_queue_complete"}, "section": "editor",
                                "label": "  Advance  "}})
    assert rejected == []
    assert cleaned["binding:new1:expose"] == {
        "candidate": {"path": "6", "input": "seed"}, "targets": [{"path": "6", "input": "seed", "class_type": "KSampler"}],
        "kind": "SEED", "section": "workflow", "label": "Sampler · seed", "order": 1}
    assert cleaned["binding:new2:expose"]["label"] == "Advance" and cleaned["binding:new2:expose"]["order"] == 2


@pytest.mark.parametrize("changes, reason", [
    ({"binding:x:expose": {"candidate": {"path": "6", "input": "steps"}, "section": "workflow"}}, "already a Control"),
    ({"binding:x:expose": {"candidate": {"path": "6", "input": "seed"}, "section": "workflow"},
      "binding:y:expose": {"candidate": {"path": "6", "input": "seed"}, "section": "workflow"}}, "same setting"),
    ({"binding:steps:expose": {"candidate": {"path": "6", "input": "seed"}, "section": "workflow"}}, "already in use"),
    ({"binding:x:expose": {"candidate": {"path": "1", "input": "selection_start"}, "section": "workflow"}}, "Sonder sets"),
    ({"binding:x:expose": {"candidate": {"path": "6", "input": "nope"}, "section": "workflow"}}, "no longer"),
    ({"binding:x:expose": {"candidate": {"path": "6", "input": "seed"}, "section": "side"}}, "section"),
    ({"binding:x:expose": {"candidate": {"path": "5:3", "input": "seed"}, "section": "workflow"}}, "Choose a setting"),
    ({"binding:__proto__:expose": {"candidate": {"path": "6", "input": "seed"}}}, "invalid"),
    ({"binding:x:hide": True}, "Sonder App"),
    ({"binding:steps:rebind": {"candidate": {"path": "6", "input": "seed"}}}, "off can be rebound"),
    ({"binding:steps:remove": "yes"}, "Send true"),
    ({"note:n:create": {"text": "x" * 4001}}, "4000"),
])
def test_structural_refusals(changes, reason):
    cleaned, rejected = validate(manage_graph(), changes)
    assert rejected, cleaned
    assert any(reason in row["reason"] for row in rejected), rejected


def test_structural_ops_are_idempotent_for_retries():
    graph = manage_graph()
    noop = {"noop": True}
    cleaned, rejected = validate(graph, {
        "binding:steps:expose": {"candidate": {"path": "6", "input": "steps"}, "section": "workflow"},
        "binding:gone:remove": True,
        "note:gone:remove": True})
    assert rejected == [] and all(value == noop for value in cleaned.values())
    graph["8"] = _relay({"steps": _binding("6", "steps")}, notes={"n1": {"text": "Hi"}})
    cleaned, rejected = validate(graph, {"note:n1:create": {"text": "Hi"}, "binding:steps:remove": True})
    assert cleaned == {"note:n1:create": noop, "binding:steps:remove": {"remove": True}}
    _, rejected = validate(graph, {"note:n1:create": {"text": "Different"}})
    assert "already in use" in rejected[0]["reason"]


def test_restore_keeps_a_suggestions_id_and_rebind_moves_a_missing_binding():
    graph = manage_graph()
    cleaned, rejected = validate(graph, {"binding:orphan:expose": {"section": "workflow"}})
    assert rejected == [] and cleaned["binding:orphan:expose"]["targets"] == [{"path": "6", "input": "cfg"}]
    _, rejected = validate(graph, {"binding:unknown:expose": {"section": "workflow"}})
    assert "no longer" in rejected[0]["reason"]
    graph["8"] = _relay({"lost": _binding("99", "steps", label="Lost")})
    cleaned, rejected = validate(graph, {"binding:lost:rebind": {"candidate": {"path": "6", "input": "steps"}}})
    assert rejected == [] and cleaned["binding:lost:rebind"]["targets"][0]["path"] == "6"


def test_notes_create_and_remove_and_counts_are_capped():
    graph = manage_graph()
    cleaned, rejected = validate(graph, {"note:n2:create": {"text": "**Read me**", "group": " Tips "}})
    assert rejected == [] and cleaned["note:n2:create"] == {"text": "**Read me**", "group": "Tips", "order": 1}
    view = describe(graph)
    view["bindings"] = [dict(view["bindings"][0], binding_id=f"b{i}") for i in range(256)]
    _, rejected = validate(graph, {"binding:x:expose": {"candidate": {"path": "6", "input": "seed"}}}, view=view)
    assert "256" in rejected[0]["reason"]


def test_structural_targets_need_a_host_mode_and_a_readable_relay():
    graph = manage_graph()
    change = {"binding:x:expose": {"candidate": {"path": "6", "input": "seed"}, "section": "workflow"}}
    _, rejected = cd.validate_binding_changes(describe(graph), change, prompt=graph)
    assert "unavailable" in rejected[0]["reason"]
    _, rejected = validate(graph, change, describe={"relay_census": {"active": 2}})
    assert "Fix the Sonder Control Relay" in rejected[0]["reason"]


def test_the_mounted_pre_check_validates_shape_and_leaves_the_graph_to_the_canvas_writer():
    graph = manage_graph()
    view = describe(graph)
    cleaned, rejected = cd.validate_binding_changes(
        view, {"binding:x:expose": {"candidate": {"path": "6", "input": "anything"}, "section": "editor"},
               "binding:missing-suggestion:expose": {"section": "workflow"}}, structural="relay")
    assert rejected == []
    assert cleaned["binding:x:expose"]["candidate"] == {"path": "6", "input": "anything"}
    _, rejected = cd.validate_binding_changes(view, {"binding:x:expose": {"candidate": {"path": "6"}}},
                                              structural="relay")
    assert rejected


def test_describe_route_lists_candidates_only_in_manage_and_validates_an_expose(monkeypatch):
    from server import routes
    actual = cd.describe_bindings
    monkeypatch.setattr(cd, "describe_bindings", lambda p, **kw: actual(p, **({"class_lookup": CLASSES.get} | kw)))
    actual_inventory = cd.candidate_inventory
    monkeypatch.setattr(cd, "candidate_inventory", lambda p, c, **kw: actual_inventory(p, c, class_lookup=CLASSES.get))
    graph = manage_graph()
    candidates = [{"path": "6", "input": "seed", "node_title": "Sampler"},
                  {"path": "6", "input": "steps"}, {"path": "1", "input": "selection_start"}]
    body = lambda **extra: json.dumps({"prompt": graph, "candidates": candidates, **extra}).encode()
    view = routes._describe_controls_body(body())
    assert "candidates" not in view
    view = routes._describe_controls_body(body(manage=True, candidates_truncated=True))
    assert [c["input"] for c in view["candidates"]] == ["seed"] and view["candidates_truncated"] is True
    result = routes._describe_controls_body(body(changes={
        "binding:abc:expose": {"candidate": {"path": "6", "input": "seed"}, "section": "workflow"}}))
    assert result["ok"] and result["changes"]["binding:abc:expose"]["kind"] == "SEED"
    with pytest.raises(routes._ControlsRequestError):
        routes._describe_controls_body(json.dumps({"prompt": graph, "candidates": [{"path": "1:2", "input": "x"}]}).encode())


# ── App-only bindings in describe ────────────────────────────────────────────

def local_entry(path, name, class_type="KSampler", **presentation):
    return {"targets": [{"path": path, "input": name, "class_type": class_type}], "section": "workflow",
            "label": "", "group": "", "order": 0, "note": "", **presentation}


def test_local_bindings_describe_with_origin_and_shadow_or_go_missing():
    graph = manage_graph()
    view = describe(graph, local={"l1": local_entry("6", "seed", label="App seed"),
                                  "l2": local_entry("6", "steps"),
                                  "l3": local_entry("6", "cfg", class_type="TextNode"),
                                  "steps": local_entry("6", "cfg"),
                                  "bad id!": local_entry("6", "cfg")},
                    local_notes={"ln": {"text": "Local note"}})
    seed = binding_of(view, "l1")
    assert seed["origin"] == "local" and seed["status"] == "ok" and seed["label"] == "App seed"
    assert binding_of(view, "l2")["status"] == "shadowed"          # the workflow binds steps itself
    assert binding_of(view, "steps")["origin"] == "authored"       # the authored id wins
    assert binding_of(view, "l3")["status"] == "missing"           # the node's class changed
    assert not any(b["binding_id"] == "bad id!" for b in view["bindings"])
    assert view["notes"][-1] == {"source": "note", "note_id": "ln", "origin": "local", "text": "Local note",
                                 "group": "", "order": 0, "status": "ok"}


def test_local_bindings_work_without_a_relay():
    graph = manage_graph()
    del graph["8"]
    view = describe(graph, local={"l1": local_entry("6", "seed")})
    assert view["relay"]["status"] == "none" and binding_of(view, "l1")["status"] == "ok"


# ── Sonder App: Manage over HTTP ─────────────────────────────────────────────

def put(client, session, changes, query=""):
    return client.put(f"/sonder-editor/sdk/session/{session.session_id}/controls{query}",
                      headers=headers(session), json={"changes": changes})


def sdk_world_lookup(world, monkeypatch):
    actual = cd.candidate_inventory
    monkeypatch.setattr(cd, "candidate_inventory", lambda p, c, **kw: actual(p, c, class_lookup=CLASSES.get))


def test_sdk_manage_lists_root_candidates_and_exposes_an_app_only_seed_that_generate_advances(world, monkeypatch):
    sdk_world_lookup(world, monkeypatch)
    graph = prompt()
    graph["6"]["inputs"]["cfg"] = 7.0
    graph["5:3"] = _node("KSampler", {"seed": 1, "steps": 9})
    install(world, graph)

    async def run():
        session = await world.open()
        async with client_for(world, monkeypatch) as client:
            path = f"/sonder-editor/sdk/session/{session.session_id}/controls"
            plain = await (await client.get(path, headers=headers(session))).json()
            assert "candidates" not in plain["view"]
            listed = await (await client.get(path + "?manage=1&filter=cfg", headers=headers(session))).json()
            assert [(c["path"], c["input"]) for c in listed["view"]["candidates"]] == [("6", "cfg")]
            everything = await (await client.get(path + "?manage=1", headers=headers(session))).json()
            keys = {(c["path"], c["input"]) for c in everything["view"]["candidates"]}
            assert ("6", "seed") not in keys and not any(p == "5:3" for p, _ in keys)
            assert ("2", "place_audio_on_timeline") in keys
            response = await put(client, session, {
                "binding:app1:expose": {"candidate": {"path": "6", "input": "cfg"}, "section": "workflow",
                                        "label": "CFG"}}, "?manage=1")
            exposed = await response.json()
            assert response.status == 200, exposed
            cfg = binding_of(exposed["view"], "app1")
            assert cfg["origin"] == "local" and cfg["label"] == "CFG" and cfg["value"] == 7.0
            assert not any(c["input"] == "cfg" for c in exposed["view"]["candidates"])
            # A retry after a lost answer changes nothing.
            again = await put(client, session, {
                "binding:app1:expose": {"candidate": {"path": "6", "input": "cfg"}, "section": "workflow"}})
            assert again.status == 200
            response = await put(client, session, {"binding:app1:value": 4.5, "binding:app1:label": "Guidance",
                                                   "binding:app1:note": "Lower is looser."})
            assert response.status == 200
            await world.runner.generate(session, tab_session_id="tab-1", count=1)
        return session

    asyncio.run(run())
    stored = sdk_templates.read_control_values(world.template_id)
    assert stored["local"]["app1"]["label"] == "Guidance" and stored["local"]["app1"]["note"] == "Lower is looser."
    assert stored["local"]["app1"]["targets"] == [{"path": "6", "input": "cfg", "class_type": "KSampler"}]
    assert "binding:app1" not in stored["labels"]          # app-only presentation is not an overlay
    assert world.client.submitted[-1]["6"]["inputs"]["cfg"] == 4.5


def test_hidden_bindings_run_the_workflow_value_do_not_advance_and_unhide_restores(world, monkeypatch):
    sdk_world_lookup(world, monkeypatch)

    async def run():
        session = await world.open()
        async with client_for(world, monkeypatch) as client:
            assert (await put(client, session, {"binding:seed:value": "77", "binding:steps:value": "31"})).status == 200
            hidden = await (await put(client, session, {"binding:seed:hide": True, "note:intro:hide": True})).json()
            assert binding_of(hidden["view"], "seed")["hidden"] is True and hidden["view"]["notes"][0]["hidden"]
            response = await put(client, session, {"binding:seed:remove": True})
            assert response.status == 400 and "hidden here" in (await response.json())["rejected"][0]["reason"]
            await world.runner.generate(session, tab_session_id="tab-1", count=2)
            assert sdk_templates.read_control_values(world.template_id)["bindings"]["seed"]["value"] == 77
            shown = await (await put(client, session, {"binding:seed:unhide": True})).json()
            assert not binding_of(shown["view"], "seed").get("hidden")
            assert binding_of(shown["view"], "seed")["value"] == "77"

    asyncio.run(run())
    for graph in world.client.submitted:
        assert graph["6"]["inputs"]["seed"] == 9007199254740993   # the workflow's own seed
        assert graph["6"]["inputs"]["steps"] == 31


def test_app_only_notes_and_removal(world, monkeypatch):
    sdk_world_lookup(world, monkeypatch)

    async def run():
        session = await world.open()
        async with client_for(world, monkeypatch) as client:
            created = await (await put(client, session, {"note:tip:create": {"text": "Use 20–30 steps."}})).json()
            note = next(n for n in created["view"]["notes"] if n["note_id"] == "tip")
            assert note["origin"] == "local" and note["text"] == "Use 20–30 steps."
            assert (await put(client, session, {"note:tip:text": "Edited", "note:tip:group": "Tips"})).status == 200
            response = await put(client, session, {"note:intro:remove": True})
            assert response.status == 400
            removed = await (await put(client, session, {"note:tip:remove": True})).json()
            assert not any(n["note_id"] == "tip" for n in removed["view"]["notes"])

    asyncio.run(run())
    assert sdk_templates.read_control_values(world.template_id)["local_notes"] == {}


def test_replace_keeps_curation_and_counts_missing_and_shadowed_app_only_bindings(world, monkeypatch):
    sdk_world_lookup(world, monkeypatch)
    graph = prompt()
    graph["6"]["inputs"]["cfg"] = 7.0
    graph["10"] = _node("TextNode", {"text": "hello"})
    install(world, graph)

    async def run():
        session = await world.open()
        async with client_for(world, monkeypatch) as client:
            assert (await put(client, session, {
                "binding:a1:expose": {"candidate": {"path": "6", "input": "cfg"}, "section": "workflow"},
                "binding:a2:expose": {"candidate": {"path": "10", "input": "text"}, "section": "workflow"},
                "binding:seed:hide": True})).status == 200

    asyncio.run(run())
    changed = prompt(bindings={"steps": _binding("6", "steps"), "seed": _binding("6", "seed"),
                               "cfg": _binding("6", "cfg", label="Authored CFG")})
    changed["6"]["inputs"]["cfg"] = 7.0
    changed["10"] = _node("KSampler", {"seed": 3})          # a different node took the id
    replaced = sdk_templates.replace_template(world.template_id, json.dumps(changed).encode(),
                                              validate=lambda p: {"ok": True}, describe=describe)
    assert replaced["local_shadowed"] == 1 and replaced["local_missing"] == 1
    stored = sdk_templates.read_control_values(world.template_id)
    assert set(stored["local"]) == {"a1", "a2"} and stored["hidden"] == {"binding:seed": True}
    view = sdk_templates.apply_stored_values(changed, sdk_templates.describe_stored(changed, stored, describe), stored,
                                             describe=describe)
    assert binding_of(view, "a1")["status"] == "shadowed" and binding_of(view, "a2")["status"] == "missing"


def test_the_launcher_summary_is_computed_on_read_from_curation(world, monkeypatch):
    sdk_world_lookup(world, monkeypatch)
    monkeypatch.setattr(cd, "describe_bindings", lambda p, _real=cd.describe_bindings, **kw:
                        _real(p, **({"class_lookup": CLASSES.get} | kw)))
    meta_path = sdk_templates._template_paths(world.template_id)[1]
    meta = json.load(open(meta_path, encoding="utf-8"))
    meta["validation"] = {"ok": True, "errors": [], "blocking": [], "warnings": [],
                          "controls": [{"binding_id": "seed", "label": "Seed"}]}
    json.dump(meta, open(meta_path, "w", encoding="utf-8"))

    async def run():
        session = await world.open()
        async with client_for(world, monkeypatch) as client:
            assert (await put(client, session, {"binding:seed:hide": True, "binding:steps:label": "Steps (app)"})).status == 200

    asyncio.run(run())
    listed = next(t for t in sdk_templates.list_templates() if t["template_id"] == world.template_id)
    labels = [c["label"] for c in listed["validation"]["controls"]]
    assert "Seed" not in labels and "Steps (app)" in labels
    assert any("No seed exposed" in w for w in listed["validation"]["warnings"])


# ── Mounted transport ────────────────────────────────────────────────────────

def test_mounted_visibility_carries_manage_and_filter_only_while_visible(registry):
    async def run():
        await setup()
        result = await sr.set_controls_visibility(PID, HID, TAB, True, manage=True, filter="seed")
        assert result["controls_manage"] is True and result["controls_filter"] == "seed"
        assert registry[-1][1]["type"] == "controls_visibility_changed"
        pending = await sr.get_control_changes(PID, HID, CANVAS)
        assert pending["controls_manage"] is True and pending["controls_filter"] == "seed"
        hidden = await sr.set_controls_visibility(PID, HID, TAB, False)
        assert hidden["controls_manage"] is False and hidden["controls_filter"] == ""
        for manage, text in ((1, None), (None, "x" * 121), (None, 5)):
            refused = await sr.set_controls_visibility(PID, HID, TAB, True, manage=manage, filter=text)
            assert refused == {"ok": False, "code": "invalid_visibility"}

    asyncio.run(run())


def test_publications_may_carry_candidates_within_the_cap(registry):
    async def run():
        await setup()
        body = publication()
        body["view"]["candidates"] = [{"path": "6", "input": "cfg"}]
        body["view"]["candidates_truncated"] = True
        assert (await sr.publish_controls_view(PID, HID, CANVAS, body))["ok"]
        body = publication(2)
        body["view"]["candidates"] = [{}] * (cd.MAX_CANDIDATES + 1)
        assert (await sr.publish_controls_view(PID, HID, CANVAS, body))["code"] == "invalid_view"
        body = publication(2)
        body["view"]["candidates"] = "nope"
        assert (await sr.publish_controls_view(PID, HID, CANVAS, body))["code"] == "invalid_view"

    asyncio.run(run())


def test_a_mounted_expose_is_shape_checked_then_queued_for_the_canvas(registry):
    async def run():
        await setup()
        assert (await sr.publish_controls_view(PID, HID, CANVAS, publication()))["ok"]
        queued = await sr.post_control_changes(PID, HID, TAB, {
            "binding:new:expose": {"candidate": {"path": "6", "input": "cfg"}, "section": "workflow"}})
        assert queued["ok"]
        pending = await sr.get_control_changes(PID, HID, CANVAS)
        assert pending["pending"][0]["changes"]["binding:new:expose"]["candidate"] == {"path": "6", "input": "cfg"}
        refused = await sr.post_control_changes(PID, HID, TAB, {"binding:new:hide": True})
        assert refused["code"] == "invalid_change"

    asyncio.run(run())
