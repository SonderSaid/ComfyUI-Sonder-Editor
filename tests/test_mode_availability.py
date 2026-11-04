"""Which Mode choices can't run on this install (0.8 hardening, Phase 12A).

The judge is exercised end to end with the real describe over a fake class
registry, as in ``test_sdk_preparation.py``. Real ``folder_paths``, the real LoRA
stack and the shipped starters run in ``test_mode_availability_real.py``.
"""

import asyncio
import json

import pytest

from server import sdk_templates
from server.sdk_runner import SdkError
from test_control_descriptors import _binding, _node
from test_sdk_preparation import CLASSES, choose, describe, with_passes, workflow, world  # noqa: F401 - a fixture


def lookup_of(world):  # noqa: F811
    return lambda name: None if name in world.missing_classes else CLASSES.get(name, object)


def judge(world, *, fresh=False):  # noqa: F811
    return sdk_templates.check_availability(world.template_id, fresh=fresh, class_lookup=CLASSES.get, describe=describe,
                                            node_lookup=lookup_of(world))


def replace(world, prompt):  # noqa: F811
    sdk_templates.replace_template(world.template_id, json.dumps(prompt).encode(), validate=lambda p: {"ok": True})
    sdk_templates._availability_cache.clear()


def files(needs):
    return [item["file"] for item in needs["missing_models"]]


def test_the_choice_that_needs_more_is_greyed_and_names_only_what_it_adds(world):  # noqa: F811
    # Task on Edit (the selected choice) needs a node and a file that Clean doesn't.
    result = judge(world)
    assert result["run"]["missing_nodes"] == ["NotInstalledNode"] and files(result["run"]) == ["missing.ckpt"]
    assert set(result["choices"]) == {"edit"}
    assert result["choices"]["edit"]["missing_nodes"] == ["NotInstalledNode"] and files(result["choices"]["edit"]) == ["missing.ckpt"]
    choose(world, "clean")
    sdk_templates._availability_cache.clear()
    result = judge(world)
    # The run is fine now, and Edit is still the one that can't run.
    assert result["run"] == {"missing_nodes": [], "missing_models": [], "problems": []}
    assert set(result["choices"]) == {"edit"}


def test_something_every_choice_needs_stays_on_the_run_line(world):  # noqa: F811
    # A base model both branches need greys neither.
    prompt = workflow(select=1)
    prompt["5"]["inputs"]["ckpt_name"] = "base.ckpt"
    prompt["4"]["inputs"]["ckpt_name"] = "base.ckpt"
    prompt["x"] = _node("Seeded", {"seed": 1, "model": ["4", 0]})
    replace(world, prompt)
    result = judge(world)
    assert files(result["run"]) == ["base.ckpt"]
    assert result["choices"] == {}


def test_a_custom_row_whose_choices_all_need_something_is_greyed(world):  # noqa: F811
    # The current state (a select no choice sets) needs nothing, and both
    # choices need the file: the run line is silent, so the choices must say it.
    prompt = workflow(select=2)
    prompt["5"]["inputs"]["ckpt_name"] = "missing.ckpt"
    prompt["x"] = _node("Seeded", {"seed": 1, "model": ["4", 0]})
    prompt["other"] = _node("Loader", {"ckpt_name": "installed.ckpt"})
    prompt["sw"]["inputs"]["branches.item2"] = ["other", 0]
    replace(world, prompt)
    result = judge(world)
    assert files(result["run"]) == []
    assert set(result["choices"]) == {"edit", "clean"}
    assert files(result["choices"]["edit"]) == files(result["choices"]["clean"]) == ["missing.ckpt"]


def test_a_row_of_one_selected_choice_greys_nothing_and_the_run_says_it(world):  # noqa: F811
    prompt = workflow()
    manifest = json.loads(prompt["9"]["inputs"]["manifest"])
    manifest["preset_categories"]["tasks"]["choices"] = ["edit"]
    del manifest["presets"]["clean"]
    prompt["9"]["inputs"]["manifest"] = json.dumps(manifest)
    replace(world, prompt)
    result = judge(world)
    assert files(result["run"]) == ["missing.ckpt"]
    assert result["choices"] == {}


def test_a_choice_in_another_row_doesnt_grey_for_what_the_current_choice_needs(world):  # noqa: F811
    # Passes' choices both carry Task → Edit's needs, since Task stays on Edit.
    replace(world, with_passes("edit", 0))
    result = judge(world)
    assert set(result["choices"]) == {"edit"}


def test_a_choice_that_frees_an_item_through_its_inclusion_greys_the_one_that_doesnt(world):  # noqa: F811
    # Task on Edit. Passes → Full includes Task → Clean, so under Full nothing is
    # missing, and under Plain it is: true of Plain, as the row stands.
    replace(world, with_passes("clean", 0))
    result = judge(world)
    assert files(result["choices"]["plain"]) == ["missing.ckpt"]
    assert "full" not in result["choices"]


def test_an_included_choice_counts(world):  # noqa: F811
    # Task on Clean; Passes → Full includes Task → Edit, so Full needs what Edit needs.
    replace(world, with_passes("edit", 0))
    choose(world, "clean")
    sdk_templates._availability_cache.clear()
    result = judge(world)
    assert files(result["choices"]["full"]) == ["missing.ckpt"]
    assert result["choices"]["full"]["missing_nodes"] == ["NotInstalledNode"]
    assert "plain" not in result["choices"]


def test_a_choice_that_cant_be_simulated_has_no_entry_and_stays_out_of_the_base(world, monkeypatch):  # noqa: F811
    real = sdk_templates._simulate_choice
    monkeypatch.setattr(sdk_templates, "_simulate_choice",
                        lambda a, s, v, p, identity, **kw: None if identity == "clean" else real(a, s, v, p, identity, **kw))
    # Edit is selected, so only the run is judged: nothing to compare it with.
    assert judge(world)["choices"] == {}
    assert files(judge(world)["run"]) == ["missing.ckpt"]


def test_the_cap_only_ever_leaves_choices_clear(world, monkeypatch):  # noqa: F811
    monkeypatch.setattr(sdk_templates, "MAX_ATTRIBUTIONS", 0)
    result = judge(world)
    assert result["choices"] == {} and files(result["run"]) == ["missing.ckpt"]


def test_an_exception_in_the_choices_leaves_the_run(world, monkeypatch):  # noqa: F811
    monkeypatch.setattr(sdk_templates, "_simulate_choice", lambda *a, **kw: 1 / 0)
    result = judge(world)
    assert result["choices"] == {} and files(result["run"]) == ["missing.ckpt"]


def test_an_ambiguous_row_judges_every_choice(world):  # noqa: F811
    # Two choices that set the same value both match, so neither is "selected".
    prompt = workflow()
    manifest = json.loads(prompt["9"]["inputs"]["manifest"])
    manifest["presets"]["twin"] = {"name": "Twin", "order": 2, "members": {"task": {"value": 0}}}
    manifest["preset_categories"]["tasks"]["choices"] = ["edit", "twin", "clean"]
    prompt["9"]["inputs"]["manifest"] = json.dumps(manifest)
    replace(world, prompt)
    assert set(judge(world)["choices"]) == {"edit", "twin"}


def test_a_choice_naming_an_absent_file_says_so_rather_than_only_repair_it(world):  # noqa: F811
    # A member with a file the Control's options lack: the choice is refused
    # before any simulation, so its needs come from its members.
    prompt = workflow(select=1)
    manifest = json.loads(prompt["9"]["inputs"]["manifest"])
    manifest["bindings"]["model"] = _binding("5", "ckpt_name", label="Model")
    manifest["presets"]["other"] = {"name": "Other", "order": 2, "members": {"model": {"value": "absent.ckpt"}}}
    manifest["preset_categories"]["tasks"]["choices"] = ["edit", "clean", "other"]
    prompt["9"]["inputs"]["manifest"] = json.dumps(manifest)
    replace(world, prompt)
    result = judge(world)
    assert files(result["choices"]["other"]) == ["absent.ckpt"]


def test_a_choice_on_an_absent_class_names_the_class(world):  # noqa: F811
    prompt = workflow(select=1)
    manifest = json.loads(prompt["9"]["inputs"]["manifest"])
    manifest["bindings"]["absent"] = _binding("x", "strength", label="Strength")
    manifest["presets"]["other"] = {"name": "Other", "order": 2, "members": {"absent": {"value": 1}}}
    manifest["preset_categories"]["tasks"]["choices"] = ["edit", "clean", "other"]
    prompt["9"]["inputs"]["manifest"] = json.dumps(manifest)
    replace(world, prompt)
    world.missing_classes = {"NotInstalledNode"}
    # describe() over the registry reads the class as absent, as the App does.
    result = judge(world)
    assert result["choices"]["other"]["missing_nodes"] == ["NotInstalledNode"]


def test_an_unusable_choice_with_no_install_to_name_keeps_its_own_reason(world):  # noqa: F811
    prompt = workflow(select=1)
    manifest = json.loads(prompt["9"]["inputs"]["manifest"])
    manifest["presets"]["other"] = {"name": "Other", "order": 2, "members": {"gone": {"value": 1}}}
    manifest["preset_categories"]["tasks"]["choices"] = ["edit", "clean", "other"]
    prompt["9"]["inputs"]["manifest"] = json.dumps(manifest)
    replace(world, prompt)
    assert "other" not in judge(world)["choices"]


def test_the_shortcut_skips_only_the_simulations(world, monkeypatch):  # noqa: F811
    prompt = workflow(select=1)
    prompt["4"]["inputs"]["ckpt_name"] = "installed.ckpt"
    prompt["x"] = _node("Seeded", {"seed": 1, "model": ["4", 0]})
    replace(world, prompt)
    monkeypatch.setattr(sdk_templates, "_simulate_choice", lambda *a, **kw: pytest.fail("simulated"))
    assert judge(world)["choices"] == {}
    # A switched-off Save is still what Generate would refuse on.
    monkeypatch.setattr(sdk_templates.prompt_graph, "nodes_reaching", lambda *a, **kw: [])
    sdk_templates._availability_cache.clear()
    assert judge(world)["run"]["problems"] == [sdk_templates.SAVE_SWITCHED_OFF]


def test_a_member_naming_a_file_always_takes_the_simulations(world, monkeypatch):  # noqa: F811
    prompt = workflow(select=1)
    prompt["4"]["inputs"]["ckpt_name"] = "installed.ckpt"
    prompt["x"] = _node("Seeded", {"seed": 1, "model": ["4", 0]})
    manifest = json.loads(prompt["9"]["inputs"]["manifest"])
    manifest["bindings"]["model"] = _binding("5", "ckpt_name", label="Model")
    manifest["presets"]["clean"]["members"]["model"] = {"value": "installed.ckpt"}
    prompt["9"]["inputs"]["manifest"] = json.dumps(manifest)
    replace(world, prompt)
    calls = []
    real = sdk_templates._simulate_choice
    monkeypatch.setattr(sdk_templates, "_simulate_choice", lambda *a, **kw: calls.append(a[4]) or real(*a, **kw))
    judge(world)
    assert calls


def test_a_routing_error_is_a_problem_not_a_failed_read(world, monkeypatch):  # noqa: F811
    def broken(prompt, view):
        raise sdk_templates.TemplateError("The route can't be replayed.", "invalid_controls", 409)

    monkeypatch.setattr(sdk_templates, "run_prompt", broken)
    result = judge(world)
    assert result["run"]["problems"] == ["The route can't be replayed."]


def test_a_run_that_cant_be_judged_greys_nothing_for_what_the_row_needs(world, monkeypatch):  # noqa: F811
    # Only the first build fails, as stored switch modes the route can't replay would:
    # the simulations succeed, and with no judged run there is no Base to subtract.
    real, calls = sdk_templates.run_prompt, []

    def first_fails(prompt, view):
        calls.append(1)
        if len(calls) == 1:
            raise sdk_templates.TemplateError("The route can't be replayed.", "invalid_controls", 409)
        return real(prompt, view)

    monkeypatch.setattr(sdk_templates, "run_prompt", first_fails)
    result = judge(world)
    assert result["run"]["problems"] == ["The route can't be replayed."]
    assert result["choices"] == {}


def test_one_choice_raising_something_unexpected_leaves_the_others(world, monkeypatch):  # noqa: F811
    prompt = workflow(select=1)
    manifest = json.loads(prompt["9"]["inputs"]["manifest"])
    manifest["presets"]["twin"] = {"name": "Twin", "order": 2, "members": {"task": {"value": 0}}}
    manifest["preset_categories"]["tasks"]["choices"] = ["edit", "twin", "clean"]
    prompt["9"]["inputs"]["manifest"] = json.dumps(manifest)
    replace(world, prompt)
    real = sdk_templates._simulate_choice

    def twin_breaks(a, s, v, p, identity, **kw):
        if identity == "twin":
            raise AttributeError("surprise")
        return real(a, s, v, p, identity, **kw)

    monkeypatch.setattr(sdk_templates, "_simulate_choice", twin_breaks)
    assert set(judge(world)["choices"]) == {"edit"}


def test_the_same_file_name_in_two_folders_is_two_needs(world):  # noqa: F811
    needs = sdk_templates._needs_of({"missing_nodes": [], "missing_models": [
        {"file": "a.safetensors", "folder": "loras"}, {"file": "a.safetensors", "folder": "checkpoints"},
        {"file": "a.safetensors", "folder": "loras", "node": "second"}]})
    assert len(needs["models"]) == 2


def test_the_answer_is_reused_while_unchanged_and_new_values_move_it(world, monkeypatch):  # noqa: F811
    calls = []
    real = sdk_templates.mode_availability
    monkeypatch.setattr(sdk_templates, "mode_availability", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    node_lookup, class_lookup = lookup_of(world), CLASSES.get  # one object each: the key holds their ids
    ask = lambda **kw: sdk_templates.check_availability(world.template_id, class_lookup=class_lookup, describe=describe,  # noqa: E731
                                                        node_lookup=node_lookup, **kw)
    first = ask()
    assert ask() == first and len(calls) == 1
    # A file may have just been installed: fresh never takes an older answer.
    ask(fresh=True)
    assert len(calls) == 2
    choose(world, "clean")
    moved = ask()
    assert len(calls) == 3 and moved["seq"] != first["seq"] and moved["run"]["missing_models"] == []


def test_concurrent_fresh_reads_share_one_computation(world, monkeypatch):  # noqa: F811
    import threading
    import time

    calls, started, release = [], threading.Event(), threading.Event()
    real = sdk_templates.mode_availability

    def slow(*a, **kw):
        calls.append(1)
        started.set()
        release.wait(5)
        return real(*a, **kw)

    monkeypatch.setattr(sdk_templates, "mode_availability", slow)
    node_lookup, class_lookup = lookup_of(world), CLASSES.get  # one object each: the key holds their ids
    ask = lambda: sdk_templates.check_availability(world.template_id, fresh=True, class_lookup=class_lookup,  # noqa: E731
                                                   describe=describe, node_lookup=node_lookup)
    first = threading.Thread(target=ask)
    first.start()
    assert started.wait(5)
    # These two arrive while it runs. It read the disk before they arrived, so
    # neither may take its answer (a file may have just been installed): one
    # computes again, and the other shares that one.
    time.sleep(0.05)
    waiting = [threading.Thread(target=ask) for _ in range(2)]
    for thread in waiting:
        thread.start()
    time.sleep(0.2)
    assert len(calls) == 1
    release.set()
    for thread in (first, *waiting):
        thread.join(5)
    assert len(calls) == 2


def test_the_run_is_what_generate_refuses_on(world):  # noqa: F811
    async def refused():
        session = await world.open()
        with pytest.raises(SdkError) as caught:
            await world.runner.generate(session, tab_session_id="tab-1")
        return caught.value

    error = asyncio.run(refused())
    result = judge(world)
    assert error.code == "missing_nodes" and error.details["missing_nodes"] == result["run"]["missing_nodes"]
    world.missing_classes = set()
    error = asyncio.run(refused())
    sdk_templates._availability_cache.clear()
    result = judge(world)
    assert error.code == "generate_refused" and result["run"]["missing_nodes"] == []
    # Everything Generate lists, the run lists: files, then any other problem.
    listed = error.details["problems"]
    assert len(listed) == len(result["run"]["missing_models"]) + len(result["run"]["problems"]) == 1
    assert files(result["run"]) == ["missing.ckpt"] and "missing.ckpt" in listed[0]


def test_a_choice_judged_here_matches_a_real_apply(world):  # noqa: F811
    # Each choice's simulation is what a real apply through the runner then reports.
    async def go():
        session = await world.open()
        for name in ("clean", "edit"):
            prompt, _ = sdk_templates.load_template(world.template_id)
            stored = sdk_templates.read_control_values(world.template_id)
            view = sdk_templates.apply_stored_values(prompt, describe(prompt), stored, describe=describe)
            simulated = sdk_templates._simulate_choice(
                sdk_templates.load_template(world.template_id)[0], stored, view, prompt, name, own_only=False, describe=describe,
                node_lookup=lookup_of(world), class_lookup=CLASSES.get)
            assert simulated is not None
            await world.runner.change_controls(session.session_id, session.secret, "tab-1", {f"preset:{name}:apply": True})
            sdk_templates._availability_cache.clear()
            applied = judge(world)["run"]
            assert simulated["missing_nodes"] == applied["missing_nodes"]
            assert [m["file"] for m in simulated["missing_models"]] == files(applied)

    asyncio.run(go())


def test_the_runner_read_passes_its_lookups_and_takes_no_runner_lock(world):  # noqa: F811
    async def go():
        session = await world.open()
        async with world.runner.template_lock(world.template_id):  # a Generate batch holding it
            return await asyncio.wait_for(world.runner.mode_availability(session, fresh=True), timeout=10)

    result = asyncio.run(go())
    assert result["template_id"] == world.template_id
    assert result["run"]["missing_nodes"] == ["NotInstalledNode"]


def test_attribution_is_unchanged_by_the_shared_simulation(world):  # noqa: F811
    result = sdk_templates.check_template(world.template_id, class_lookup=CLASSES.get, describe=describe,
                                          node_lookup=lookup_of(world))
    assert result["missing_nodes"] == [{"class_type": "NotInstalledNode", "needed_by": ["Task → Edit"]}]
    assert [(m["file"], m["needed_by"]) for m in result["missing_models"]] == [("missing.ckpt", ["Task → Edit"])]


def client_for(world, monkeypatch):  # noqa: F811
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from server import sdk_routes

    monkeypatch.setattr(sdk_routes, "get_runner", lambda *a, **kw: world.runner)
    routes = web.RouteTableDef()
    sdk_routes.register_sdk_routes(routes, resolve_project=world.resolve, remember_event_loop=lambda: None,
                                   projects_dir=lambda: "")
    app = web.Application()
    app.add_routes(routes)
    return TestClient(TestServer(app))


def test_the_route_needs_the_session_secret_and_shows_paths_only_on_this_machine(world, monkeypatch):  # noqa: F811
    from server import model_requirements, sdk_routes
    world.runner._node_lookup = lookup_of(world)
    monkeypatch.setattr(model_requirements, "target_path", lambda item: "D:/models/" + item["file"])

    async def go():
        session = await world.open()
        path = f"/sonder-editor/sdk/session/{session.session_id}/availability"
        async with client_for(world, monkeypatch) as client:
            wrong = await client.get(path, headers={sdk_routes.SECRET_HEADER: "wrong"})
            none = await client.get(path)
            right = await client.get(path, headers={sdk_routes.SECRET_HEADER: session.secret})
            other_session = await client.get("/sonder-editor/sdk/session/sdk-nope/availability",
                                             headers={sdk_routes.SECRET_HEADER: session.secret})
            foreign = await client.get(path, headers={sdk_routes.SECRET_HEADER: session.secret,
                                                      "Origin": "http://elsewhere.example", "Host": "127.0.0.1"})
            foreign_body = await foreign.json()
            monkeypatch.setattr(sdk_routes, "is_loopback_request", lambda request: False)
            # Not fresh, so a path a local caller's answer left in the cache would show.
            remote = await client.get(path, headers={sdk_routes.SECRET_HEADER: session.secret})
            return wrong.status, none.status, right.status, await right.json(), await remote.json(), other_session.status, foreign_body

    wrong, none, status, local, remote, other_session, foreign = asyncio.run(go())
    assert wrong != 200 and none != 200 and status == 200 and other_session != 200
    assert "path" not in foreign["run"]["missing_models"][0]
    assert local["ok"] and local["template_id"] == world.template_id
    assert local["run"]["missing_models"][0]["path"] == "D:/models/missing.ckpt"
    assert local["choices"]["edit"]["missing_models"][0]["path"] == "D:/models/missing.ckpt"
    assert "path" not in remote["run"]["missing_models"][0]
    assert remote["run"]["missing_models"][0]["relative"] == "models/checkpoints/missing.ckpt"


def test_the_answer_never_enters_the_controls_view(world, monkeypatch):  # noqa: F811
    async def go():
        session = await world.open()
        return (await world.runner.controls_view(session))["view"]

    view = asyncio.run(go())
    assert "availability" not in view and "choices" not in view and "run" not in view
    states = {row["id"]: row["state"] for row in view["presets"]}
    # Edit is selected, though it can't run: selection stays derived from values alone.
    assert states["edit"] == "match" and states["clean"] != "unavailable"
