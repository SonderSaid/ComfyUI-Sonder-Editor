"""Sonder App branch runs: one output node, posted to this ComfyUI with its own prompt id."""

import asyncio
import types

import pytest

from server import model_requirements, run_control, sdk_runs, sdk_templates
from server.sdk_runner import CANCELED, COMPLETED, LANDED, QUEUED, UNCONFIRMED, SdkError
from test_sdk_runner import World, editor_values, stored_seed, user_dir  # noqa: F401 - fixtures

OUTPUTS = {"SonderSaveVideo"}


class Branch:
    """What /prompt answered; records every body posted."""

    def __init__(self, world):
        self.bodies, self.reply, self.on_post = [], None, None
        world.runner._branch_submit = self.post
        world.runner._class_lookup = lambda name: types.SimpleNamespace(OUTPUT_NODE=name in OUTPUTS)

    async def post(self, body):
        self.bodies.append(body)
        if self.on_post:
            self.on_post(body)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply or (200, {"prompt_id": body["prompt_id"], "number": 1, "node_errors": {}})


@pytest.fixture
def world(monkeypatch):
    run_control.reset_for_tests()
    monkeypatch.setattr(run_control, "branch_capability", lambda: {"ok": True, "reason": ""})
    world = World()
    world.branch = Branch(world)
    yield world
    world.runner.close()
    run_control.reset_for_tests()


def branch(world, session, target="2", **kwargs):
    return world.runner.run_branch(session, tab_session_id="tab-1", target=target, **kwargs)


def test_a_branch_posts_its_own_ids_and_targets_and_advances_seeds(world):
    sdk_templates.update_control_values(world.template_id, lambda d: d["bindings"].update(
        {"abcd1234": stored_seed(seed_mode="increment")}))

    async def go():
        session = await world.open()
        first = await branch(world, session, editor_values=editor_values(), editor_values_seq=1)
        second = await branch(world, session)
        return session, first, second

    session, first, second = asyncio.run(go())
    body = world.branch.bodies[0]
    assert body["partial_execution_targets"] == ["2"]
    assert body["prompt_id"] == first["prompt_id"] and body["client_id"] and body["client_id"] != body["prompt_id"]
    assert body["prompt"]["1"]["inputs"]["scene_id"] == "scene-a"
    token = body["prompt"]["1"]["_meta"]["sonder_run"]
    assert sdk_runs.token_state(token)["purpose"] == "branch"
    # Seeds advance per run, as native partial queueing does.
    assert [b["prompt"]["5"]["inputs"]["seed"] for b in world.branch.bodies] == [10, 11]
    run = session.runs[0]
    assert (run.purpose, run.state, run.prompt_id) == ("branch", QUEUED, first["prompt_id"])
    assert run.public()["purpose"] == "branch"
    assert world.client.submitted == []  # never through the proxy


def test_a_branch_notes_its_modes_under_its_token(world, monkeypatch):
    """Phase 3: a branch run's receipt carries the Modes of the view it was built from."""
    from server import sdk_runner
    notes = []
    monkeypatch.setattr(sdk_runner.run_feedback, "view_modes", lambda view: [{"id": "look"}] if isinstance(view, dict) else None)
    monkeypatch.setattr(sdk_runner.run_feedback, "note_modes",
                        lambda rows, prompt_id="", key="": notes.append((rows, key)))

    async def go():
        session = await world.open()
        await branch(world, session)

    asyncio.run(go())
    token = world.branch.bodies[0]["prompt"]["1"]["_meta"]["sonder_run"]
    assert notes == [([{"id": "look"}], token)]


@pytest.mark.parametrize("target", ["5", "9", "", None])
def test_only_an_output_node_in_the_compiled_prompt_can_run(world, target):
    async def go():
        session = await world.open()
        with pytest.raises(SdkError) as raised:
            await branch(world, session, target=target)
        return raised.value

    error = asyncio.run(go())
    assert error.code in {"branch_not_output", "invalid_target"} and world.branch.bodies == []


def test_missing_models_count_only_inside_the_branch(world, monkeypatch):
    seen = []
    monkeypatch.setattr(model_requirements, "missing_models",
                        lambda prompt, lookup=None, **kw: seen.append(sorted(prompt)) or [
                            {"file": "x.safetensors", "folder": "checkpoints", "relative": None, "node": "n",
                             "node_id": "1", "input": "ckpt"}])

    async def go():
        session = await world.open()
        with pytest.raises(SdkError) as raised:
            await branch(world, session)
        return raised.value

    error = asyncio.run(go())
    assert error.code == "generate_refused" and seen == [["1", "2"]]  # not the unrelated KSampler


def test_a_refused_branch_forgets_its_token_and_keeps_no_run(world):
    world.branch.reply = (400, {"error": {"type": "prompt_no_outputs", "message": "Prompt has no outputs"},
                                "node_errors": {}})

    async def go():
        session = await world.open()
        with pytest.raises(SdkError) as raised:
            await branch(world, session)
        return session, raised.value

    session, error = asyncio.run(go())
    assert error.code == "prompt_no_outputs" and session.runs == []
    token = world.branch.bodies[0]["prompt"]["1"]["_meta"]["sonder_run"]
    assert sdk_runs.token_state(token) is None


def test_an_unknown_submit_outcome_waits_unconfirmed_and_is_never_retried(world):
    world.branch.reply = ConnectionError("dropped")

    async def go():
        session = await world.open()
        result = await branch(world, session)
        return session, result

    session, result = asyncio.run(go())
    assert result["error"]["code"] == "submit_unconfirmed" and len(world.branch.bodies) == 1
    assert session.runs[0].state == UNCONFIRMED and session.runs[0].prompt_id == ""


def test_a_changed_prompt_id_turns_branch_runs_off(world):
    world.branch.reply = (200, {"prompt_id": "something-else"})

    async def go():
        session = await world.open()
        result = await branch(world, session)
        return session, result

    session, result = asyncio.run(go())
    # The run is tracked under the id ComfyUI used; later branches are refused.
    assert result["prompt_id"] == "something-else" and session.runs[0].prompt_id == "something-else"
    assert run_control._DISABLED_REASON == "ComfyUI replaced a preassigned prompt id"


def test_the_capability_gate_reads_version_queue_and_validate_prompt(monkeypatch):
    import sys
    run_control.reset_for_tests()

    async def validate_prompt(prompt_id, prompt, partial_execution_list):
        return None

    queue = types.SimpleNamespace(interrupt_if_running=lambda pid: False, delete_queue_item=lambda fn: False)
    monkeypatch.setattr(run_control, "_prompt_queue", lambda: queue)
    monkeypatch.setitem(sys.modules, "execution", types.SimpleNamespace(validate_prompt=validate_prompt))
    from server import sdk_proxy
    monkeypatch.setattr(sdk_proxy, "own_upstream_url", lambda *_a: "http://127.0.0.1:8188")
    for version, expected in (("0.42.0", True), ("0.43.1", True), ("0.41.9", False)):
        monkeypatch.setitem(sys.modules, "comfyui_version", types.SimpleNamespace(__version__=version))
        assert run_control.branch_capability()["ok"] is expected, version
    monkeypatch.setitem(sys.modules, "comfyui_version", types.SimpleNamespace(__version__="0.42.0"))
    monkeypatch.setattr(run_control, "_prompt_queue", lambda: types.SimpleNamespace(delete_queue_item=lambda fn: False))
    assert run_control.branch_capability() == {"ok": False, "reason": "no_atomic_cancel"}
    monkeypatch.setattr(run_control, "_prompt_queue", lambda: queue)

    async def old_validate(prompt_id, prompt):
        return None

    monkeypatch.setitem(sys.modules, "execution", types.SimpleNamespace(validate_prompt=old_validate))
    assert run_control.branch_capability() == {"ok": False, "reason": "no_partial_execution"}
    run_control.disable_branch_runs("x")
    assert run_control.branch_capability() == {"ok": False, "reason": "x"}
    run_control.reset_for_tests()


def test_an_unsupported_comfyui_refuses_with_no_fallback(world, monkeypatch):
    monkeypatch.setattr(run_control, "branch_capability", lambda: {"ok": False, "reason": "comfyui_too_old"})

    async def go():
        session = await world.open()
        with pytest.raises(SdkError) as raised:
            await branch(world, session)
        return raised.value

    error = asyncio.run(go())
    assert error.code == "unsupported" and error.details["reason"] == "comfyui_too_old" and world.branch.bodies == []


@pytest.mark.parametrize("take_saved, state", [(False, COMPLETED), (True, LANDED)])
def test_a_branch_completes_or_lands_by_whether_a_take_was_saved(world, take_saved, state):
    async def go():
        session = await world.open()
        await branch(world, session)
        run = session.runs[0]
        world.runner.apply_run_finished({"run_token": run.token, "prompt_id": run.prompt_id, "outcome": "success",
                                         "leased_job_ids": [], "returned_job_ids": [], "take_saved": take_saved})
        await world.runner.generate(session, tab_session_id="tab-1")
        generate = session.runs[1]
        world.runner.apply_run_finished({"run_token": generate.token, "prompt_id": generate.prompt_id,
                                         "outcome": "success", "leased_job_ids": [], "returned_job_ids": []})
        return run, generate

    run, generate = asyncio.run(go())
    assert run.state == state and (run.note == "Output ready.") is (state == COMPLETED)
    assert generate.state == LANDED  # a Generate is unchanged


def test_cancel_reaches_a_branch_before_comfyuis_reply(world, monkeypatch):
    cancelled = []
    monkeypatch.setattr(run_control, "cancel_prompt", lambda prompt_id, queue=None: cancelled.append(prompt_id) or "flagged")

    async def go():
        session = await world.open()
        world.branch.on_post = lambda body: asyncio.get_event_loop().create_task(world.runner.cancel(session))
        result = await branch(world, session)
        for _ in range(5):
            await asyncio.sleep(0)
        return session, result

    session, result = asyncio.run(go())
    # Canceled while its submit was in flight, by its own preassigned id, then again once accepted.
    assert cancelled and set(cancelled) == {result["prompt_id"]}
    assert session.runs[0].cancel_requested
    world.runner.apply_run_finished({"run_token": session.runs[0].token, "prompt_id": result["prompt_id"],
                                     "outcome": "interrupted", "leased_job_ids": [], "returned_job_ids": []})
    assert session.runs[0].state == CANCELED


def test_a_branch_that_ends_unconfirmed_is_not_told_it_lost_a_take(world):
    async def go():
        session = await world.open()
        await branch(world, session)
        run = session.runs[0]
        sdk_runs.open_release("k", run.prompt_id, {}, None)
        sdk_runs.close_release("k")
        await world.runner.presence_tick()
        world.now[0] += 1000
        await world.runner.presence_tick()
        return run

    run = asyncio.run(go())
    assert "take" not in run.note and "could not confirm" in run.note


def test_a_submit_that_never_left_refuses_with_no_run_and_no_seed(world):
    from server.sdk_runner import BranchNotSent

    sdk_templates.update_control_values(world.template_id, lambda d: d["bindings"].update(
        {"abcd1234": stored_seed(seed_mode="increment")}))
    world.branch.reply = BranchNotSent("Sonder App does not support ComfyUI over HTTPS yet.", "tls_unsupported")

    async def go():
        session = await world.open()
        with pytest.raises(SdkError) as raised:
            await branch(world, session)
        return session, raised.value

    session, error = asyncio.run(go())
    assert (error.code, error.status) == ("tls_unsupported", 503) and session.runs == []
    token = world.branch.bodies[0]["prompt"]["1"]["_meta"]["sonder_run"]
    assert sdk_runs.token_state(token) is None
    assert sdk_templates.read_control_values(world.template_id)["bindings"]["abcd1234"].get("value") in (None, 10)


def test_missing_nodes_anywhere_and_an_unreadable_relay_refuse_as_for_generate(world):
    async def go(missing=(), relay=None):
        world.missing_classes = set()
        session = await world.open()
        # Missing since the session opened (a Replace, an uninstall).
        world.missing_classes = set(missing)
        if relay:
            world.relay = relay
        with pytest.raises(SdkError) as raised:
            await branch(world, session)
        return raised.value

    # The KSampler is outside the branch, but ComfyUI refuses any missing class.
    missing = asyncio.run(go(missing=["KSampler"]))
    assert missing.code == "missing_nodes" and missing.details["missing_nodes"]
    relay = asyncio.run(go(relay={"status": "error", "key": "9", "error": "Two Relays."}))
    assert relay.code == "generate_refused" and world.branch.bodies == []


def test_the_capability_gate_refuses_a_tls_comfyui(monkeypatch):
    import sys
    from server import sdk_proxy

    run_control.reset_for_tests()

    async def validate_prompt(prompt_id, prompt, partial_execution_list):
        return None

    queue = types.SimpleNamespace(interrupt_if_running=lambda pid: False, delete_queue_item=lambda fn: False)
    monkeypatch.setattr(run_control, "_prompt_queue", lambda: queue)
    monkeypatch.setitem(sys.modules, "execution", types.SimpleNamespace(validate_prompt=validate_prompt))
    monkeypatch.setitem(sys.modules, "comfyui_version", types.SimpleNamespace(__version__="0.42.0"))

    def tls(*_args):
        raise sdk_proxy.ProxyUnavailable("HTTPS", "tls_unsupported")

    monkeypatch.setattr(sdk_proxy, "own_upstream_url", tls)
    assert run_control.branch_capability() == {"ok": False, "reason": "tls_unsupported"}
    monkeypatch.setattr(sdk_proxy, "own_upstream_url", lambda *_a: "http://127.0.0.1:8188")
    assert run_control.branch_capability() == {"ok": True, "reason": ""}


def test_cancel_run_cancels_one_run_of_this_session_only(world, monkeypatch):
    """Output Views: a pane cancels its own run by run id (D5)."""
    cancelled = []
    monkeypatch.setattr(run_control, "cancel_prompt", lambda prompt_id, queue=None: cancelled.append(prompt_id) or "deleted")

    async def go():
        session = await world.open()
        first = await branch(world, session)
        second = await branch(world, session)
        one = await world.runner.cancel_run(session, first["run_ids"][0])
        with pytest.raises(SdkError) as unknown:
            await world.runner.cancel_run(session, "run-nope")
        return session, first, second, one, unknown.value

    session, first, second, one, unknown = asyncio.run(go())
    assert one == {"ok": True, "canceling": first["run_ids"]}
    assert cancelled == [first["prompt_id"]]  # the other run is left alone
    runs = {run.run_id: run for run in session.runs}
    assert runs[first["run_ids"][0]].cancel_requested and not runs[second["run_ids"][0]].cancel_requested
    assert (unknown.code, unknown.status) == ("unknown_run", 404)
    world.runner.apply_run_finished({"run_token": runs[first["run_ids"][0]].token, "prompt_id": first["prompt_id"],
                                     "outcome": "interrupted", "leased_job_ids": [], "returned_job_ids": []})

    async def again():
        return await world.runner.cancel_run(session, first["run_ids"][0])
    assert asyncio.run(again()) == {"ok": True, "canceling": []}  # a finished run: nothing to do


def test_observed_run_ids_outlive_the_24_kept_runs(world):
    from server import sdk_runner

    async def go():
        session = await world.open()
        made = []
        for _ in range(sdk_runner.FINISHED_RUNS_KEPT + 6):
            result = await branch(world, session)
            run = next(r for r in session.runs if r.run_id == result["run_ids"][0])
            world.runner.apply_run_finished({"run_token": run.token, "prompt_id": result["prompt_id"], "outcome": "success",
                                             "leased_job_ids": [], "returned_job_ids": []})
            made.append(run.run_id)
        return session, made

    session, made = asyncio.run(go())
    # Trimmed when a run is added: the kept runs, then the newest.
    assert len(session.runs) <= sdk_runner.FINISHED_RUNS_KEPT + 1 < len(made)
    assert set(session.observed_run_ids()) == set(made)
    assert len(session.observed_run_ids()) == len(made) and len(set(session.observed_run_ids())) == len(made)


def test_sdk_run_cancel_route_needs_the_session_secret_its_tab_and_its_own_run(world, monkeypatch):
    """POST …/runs/{run_id}/cancel: the SDK headers like every session route, and
    only a run of this session (D5)."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from server import sdk_routes

    cancelled = []
    monkeypatch.setattr(run_control, "cancel_prompt", lambda prompt_id, queue=None: cancelled.append(prompt_id) or "deleted")
    monkeypatch.setattr(sdk_routes, "get_runner", lambda *a, **kw: world.runner)
    routes = web.RouteTableDef()
    sdk_routes.register_sdk_routes(routes, resolve_project=world.resolve, remember_event_loop=lambda: None, projects_dir=lambda: "")
    app = web.Application()
    app.add_routes(routes)

    async def go():
        session = await world.open()
        run = await branch(world, session)
        run_id = run["run_ids"][0]
        mine = {sdk_routes.SECRET_HEADER: session.secret, sdk_routes.TAB_HEADER: "tab-1"}
        base = f"/sonder-editor/sdk/session/{session.session_id}/runs"
        async with TestClient(TestServer(app)) as http:
            secret = await http.post(f"{base}/{run_id}/cancel", headers={**mine, sdk_routes.SECRET_HEADER: "wrong"})
            tab = await http.post(f"{base}/{run_id}/cancel", headers={**mine, sdk_routes.TAB_HEADER: "tab-2"})
            unknown = await http.post(f"{base}/run-elsewhere/cancel", headers=mine)
            before = list(cancelled)
            ok = await http.post(f"{base}/{run_id}/cancel", headers=mine)
            return run, (secret.status, tab.status, unknown.status, before), (ok.status, await ok.json())

    run, refused, (status, body) = asyncio.run(go())
    # A wrong secret reads as no session (its existence is not told); another tab is refused;
    # a run id this session does not have is unknown. None of them canceled anything.
    assert refused == (404, 409, 404, [])
    assert status == 200 and body == {"ok": True, "canceling": run["run_ids"]}
    assert cancelled == [run["prompt_id"]]


def test_sdk_live_routes_need_the_session_secret_and_its_tab(world, monkeypatch):
    """GET …/runs/live and …/runs/live/{prompt_id}/bytes (Phase 4): the SDK headers,
    and only this session's runs' previews."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from server import run_outputs, sdk_routes

    run_outputs.reset_for_tests()
    monkeypatch.setattr(sdk_routes, "get_runner", lambda *a, **kw: world.runner)
    routes = web.RouteTableDef()
    sdk_routes.register_sdk_routes(routes, resolve_project=world.resolve, remember_event_loop=lambda: None, projects_dir=lambda: "")
    app = web.Application()
    app.add_routes(routes)

    async def go():
        session = await world.open()
        run = await branch(world, session)
        body = world.branch.bodies[-1]
        bound = sdk_runs.bind_queued(body["prompt_id"], body["prompt"], body["partial_execution_targets"])
        assert bound and bound["owners"] == [("sdk", run["run_ids"][0])]
        run_outputs.record_live(bound, "2", {"publisher": "kj", "via": "2", "mime": "image/webp"},
                                ("bytes", b"RIFFwebp"), 8)
        mine = {sdk_routes.SECRET_HEADER: session.secret, sdk_routes.TAB_HEADER: "tab-1"}
        base = f"/sonder-editor/sdk/session/{session.session_id}/runs/live"
        async with TestClient(TestServer(app)) as http:
            wrong = await http.get(base, params={"sources": "2"}, headers={**mine, sdk_routes.SECRET_HEADER: "wrong"})
            other_tab = await http.get(base, params={"sources": "2"}, headers={**mine, sdk_routes.TAB_HEADER: "tab-2"})
            read = await (await http.get(base, params={"sources": "2"}, headers=mine)).json()
            raw = await http.get(f"{base}/{body['prompt_id']}/bytes", params={"source": "2"}, headers=mine)
            foreign = await http.get(f"{base}/someone-elses-prompt/bytes", params={"source": "2"}, headers=mine)
            return (wrong.status, other_tab.status, foreign.status), read, (raw.status, await raw.read())

    refused, read, raw = asyncio.run(go())
    assert refused == (404, 409, 404)
    assert read["sources"]["2"]["state"] == "live" and read["seen"][0]["source"] == "2"
    assert raw == (200, b"RIFFwebp")
    run_outputs.reset_for_tests()
