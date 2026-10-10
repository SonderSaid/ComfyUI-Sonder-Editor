"""Sonder App workflow switching: one workflow per request, per-run workflows,
favorites and the Workflow menu (server/sdk_runner.py, sdk_routes.py, launcher_*)."""

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from server import launcher_routes, launcher_state, sdk_routes, sdk_runner, sdk_templates
from server.sdk_runner import SdkError
from test_sdk_runner import PROJECT, World, template_prompt, user_dir  # noqa: F401 (fixture)

VALID = {"ok": True, "errors": [], "blocking": [], "warnings": []}


@pytest.fixture
def world():
    world = World()
    world.labels = []

    async def set_label(project, host_id, tab, label):
        world.labels.append((project, host_id, tab, label))
        return True

    world.runner._set_owner_label = set_label
    world.other_id = sdk_templates.import_template(
        json.dumps(template_prompt()).encode(), source_name="detail.json", name="Detailing",
        validate=lambda prompt: VALID)["template_id"]
    yield world
    world.runner.close()


def switch(world, session, template_id, expected=None, tab="tab-1"):
    return world.runner.switch_workflow(session.session_id, session.secret, tab, template_id,
                                        session.template_id if expected is None else expected)


def test_switch_keeps_the_session_and_each_run_names_its_own_workflow(world):
    async def go():
        session = await world.open()
        first = session.template_id
        await world.runner.generate(session, tab_session_id="tab-1", count=1, expected_template_id=first)
        result = await switch(world, session, world.other_id)
        await world.runner.generate(session, tab_session_id="tab-1", count=1, expected_template_id=world.other_id)
        return session, first, result

    session, first, result = asyncio.run(go())
    assert result == {"ok": True, "template_id": world.other_id, "template_name": "Detailing"}
    assert session.template_id == world.other_id and session.project == PROJECT
    runs = session.public("x")["runs"]
    assert [(run["template_id"], run["template_name"]) for run in runs] == [(first, "i2va"), (world.other_id, "Detailing")]
    # The Launcher preselects it next time, and the owner's label follows it.
    assert launcher_state.read_state()["last_workflow"]["by_project"][PROJECT]["template_id"] == world.other_id
    assert world.labels == [(PROJECT, session.session_id, "tab-1", "Sonder App · Detailing")]
    assert world.events[-1][1]["template_name"] == "Detailing"


def test_requests_queued_behind_a_switch_never_land_on_the_new_workflow(world):
    async def go():
        session = await world.open()
        first = session.template_id
        async with world.runner.template_lock(first):
            switching = asyncio.create_task(switch(world, session, world.other_id))
            await asyncio.sleep(0.02)
            generating = asyncio.create_task(world.runner.generate(
                session, tab_session_id="tab-1", count=1, expected_template_id=first))
            saving = asyncio.create_task(world.runner.change_controls(
                session.session_id, session.secret, "tab-1", {"binding:abcd1234:value": 42},
                expected_template_id=first))
            await asyncio.sleep(0.02)
        await switching
        results = await asyncio.gather(generating, saving, return_exceptions=True)
        return session, first, results

    session, first, results = asyncio.run(go())
    assert [getattr(r, "code", r) for r in results] == ["workflow_changed", "workflow_changed"]
    assert not world.client.submitted
    assert "abcd1234" not in sdk_templates.read_control_values(world.other_id)["bindings"]
    assert "abcd1234" not in sdk_templates.read_control_values(first)["bindings"]


def test_a_generate_queued_before_the_switch_runs_on_the_old_workflow(world):
    async def go():
        session = await world.open()
        first = session.template_id
        lock = world.runner.template_lock(first)
        async with lock:
            generating = asyncio.create_task(world.runner.generate(
                session, tab_session_id="tab-1", count=1, expected_template_id=first))
            # Wait until Generate is queued on the lock, not a fixed time.
            for _ in range(500):
                if lock._waiters:
                    break
                await asyncio.sleep(0.005)
            assert lock._waiters
            switching = asyncio.create_task(switch(world, session, world.other_id))
            await asyncio.sleep(0.02)
        result = await generating
        await switching
        return session, first, result

    session, first, result = asyncio.run(go())
    assert result["run_ids"] and session.runs[0].template_id == first
    assert session.template_id == world.other_id


def test_a_page_that_meant_another_workflow_is_refused(world):
    async def go():
        session = await world.open()
        for call in (
            world.runner.generate(session, tab_session_id="tab-1", expected_template_id=world.other_id),
            world.runner.change_controls(session.session_id, session.secret, "tab-1",
                                         {"binding:abcd1234:value": 42}, expected_template_id=world.other_id),
            switch(world, session, world.other_id, expected=world.other_id),
        ):
            with pytest.raises(SdkError) as exc:
                await call
            assert exc.value.code == "workflow_changed"
            assert exc.value.details["template_id"] == session.template_id
        return session

    session = asyncio.run(go())
    assert session.template_id == world.template_id and not world.client.submitted


def test_only_the_owner_tab_switches(world):
    async def go():
        session = await world.open()
        with pytest.raises(SdkError) as exc:
            await switch(world, session, world.other_id, tab="tab-2")
        assert exc.value.code == "not_owner"
        return session

    assert asyncio.run(go()).template_id == world.template_id


def test_a_pair_live_in_another_tab_is_refused_and_an_abandoned_one_is_replaced(world):
    async def go():
        session = await world.open()
        other = await world.runner.open_session(world.other_id, PROJECT)
        world.owners[other["session_id"]] = {"session_id": "tab-9", "status": "active"}
        with pytest.raises(SdkError) as exc:
            await switch(world, session, world.other_id)
        assert exc.value.code == "workflow_open_elsewhere"
        world.owners.pop(other["session_id"])  # its tab left
        # Just opened: its page may still be loading, so it keeps the pair a while.
        with pytest.raises(SdkError) as exc:
            await switch(world, session, world.other_id)
        assert "just opened" in str(exc.value)
        world.now[0] += sdk_runner.SWITCH_PAIR_GRACE_SEC + 1
        await switch(world, session, world.other_id)
        reopened = await world.runner.open_session(world.other_id, PROJECT)
        return session, other, reopened

    session, other, reopened = asyncio.run(go())
    assert reopened["session_id"] == session.session_id != other["session_id"]


def test_a_pair_with_takes_still_generating_says_so(world):
    async def go():
        session = await world.open()
        other = world.runner.session_for(**{k: v for k, v in (await world.runner.open_session(world.other_id, PROJECT)).items()
                                            if k in ("session_id", "secret")})
        other.runs.append(sdk_runner.SdkRun(run_id="r", token="t", session_id=other.session_id, index=1, count=1))
        world.now[0] += sdk_runner.SWITCH_PAIR_GRACE_SEC + 1
        with pytest.raises(SdkError) as exc:
            await switch(world, session, world.other_id)
        assert exc.value.code == "workflow_open_elsewhere" and "still generating" in str(exc.value)

    asyncio.run(go())


def test_a_waiting_switch_never_stalls_other_opens(world):
    async def go():
        session = await world.open()
        third = sdk_templates.import_template(json.dumps(template_prompt()).encode(), source_name="third.json",
                                              validate=lambda prompt: VALID)["template_id"]
        async with world.runner.template_lock(session.template_id):  # a long Generate
            switching = asyncio.create_task(switch(world, session, world.other_id))
            await asyncio.sleep(0.02)
            opened = await asyncio.wait_for(world.runner.open_session(third, PROJECT), timeout=2)
            assert not switching.done()
        await switching
        return opened

    assert asyncio.run(go())["ok"]


def test_a_workflow_that_cannot_drive_the_editor_is_refused(world, monkeypatch):
    real = sdk_templates.load_template

    def load(template_id):
        prompt, meta = real(template_id)
        if template_id == world.other_id:
            meta = {**meta, "validation": {"ok": False}}
        return prompt, meta

    monkeypatch.setattr(sdk_templates, "load_template", load)

    async def go():
        session = await world.open()
        with pytest.raises(SdkError) as exc:
            await switch(world, session, world.other_id)
        assert exc.value.code == "invalid_template"
        return session

    assert asyncio.run(go()).template_id == world.template_id


def test_controls_read_and_write_the_new_workflow_and_name_it(world):
    async def go():
        session = await world.open()
        await switch(world, session, world.other_id)
        view = await world.runner.controls_view(session)
        written = await world.runner.change_controls(session.session_id, session.secret, "tab-1",
                                                     {"binding:abcd1234:value": 42},
                                                     expected_template_id=world.other_id)
        return view, written

    view, written = asyncio.run(go())
    assert view["template_id"] == written["template_id"] == world.other_id
    assert sdk_templates.read_control_values(world.other_id)["bindings"]["abcd1234"]["value"] == 42
    assert "abcd1234" not in sdk_templates.read_control_values(world.template_id)["bindings"]


# ── Favorites and the Workflow menu ─────────────────────────────────────────

def test_favorites_normalize_toggle_and_keep_unknown_keys(user_dir):  # noqa: F811
    import os
    path = launcher_state.state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"favorites": ["a" * 16, "a" * 16, "bad", 7, "b" * 16], "future": {"x": 1}}, handle)
    state = launcher_state.read_state()
    assert state["favorites"] == ["a" * 16, "b" * 16] and state["future"] == {"x": 1}
    state = launcher_state.set_favorite("c" * 16, True)
    assert state["favorites"] == ["a" * 16, "b" * 16, "c" * 16]
    state = launcher_state.set_favorite("a" * 16, False)
    assert launcher_state.public_state(state)["favorites"] == ["b" * 16, "c" * 16]
    with pytest.raises(launcher_state.LauncherStateError):
        launcher_state.set_favorite("nope", True)


def test_favorites_lead_the_order_and_the_preselection_fallback(user_dir):  # noqa: F811
    templates = [
        {"template_id": "a" * 16, "name": "Recent", "imported_at": 1},
        {"template_id": "b" * 16, "name": "Starred", "imported_at": 2},
        {"template_id": "c" * 16, "name": "Starter", "starter": True, "order": 0},
        {"template_id": "d" * 16, "name": "Starred starter", "starter": True, "order": 1},
    ]
    launcher_state.record_open(PROJECT, "a" * 16)
    launcher_state.set_favorite("b" * 16, True)
    state = launcher_state.set_favorite("d" * 16, True)
    order = launcher_routes.workflow_order(state, templates)
    assert order == ["b" * 16, "a" * 16, "d" * 16, "c" * 16]
    assert launcher_state.preferred_workflow(state, "Never opened", order) == "a" * 16  # last used anywhere
    state = launcher_state.normalize({**state, "last_workflow": {}})
    assert launcher_state.preferred_workflow(state, "Never opened", order) == "b" * 16  # the top: a favorite


def test_the_workflow_menu_puts_favorites_first_and_hides_hidden_unless_current(world):
    starred, hidden = world.other_id, sdk_templates.import_template(
        json.dumps(template_prompt()).encode(), source_name="hidden.json", name="Hidden",
        validate=lambda prompt: {**VALID, "description": "First line\nSecond line"})["template_id"]
    launcher_state.set_favorite(starred, True)
    launcher_state.set_hidden("workflow", hidden, True)
    listing = sdk_routes._session_workflows(world.template_id)
    ids = [w["template_id"] for w in listing["workflows"]]
    assert ids[0] == starred and world.template_id in ids and hidden not in ids
    assert listing["workflows"][0] == {"template_id": starred, "name": "Detailing", "description": "",
                                       "starter": False, "ok": True, "favorite": True}
    current = sdk_routes._session_workflows(hidden)
    entry = next(w for w in current["workflows"] if w["template_id"] == hidden)
    assert entry["description"] == "First line" and current["current"] == hidden


def client_for(world, monkeypatch, pending=lambda project_dir: 3):
    monkeypatch.setattr(sdk_routes, "get_runner", lambda *a, **kw: world.runner)
    routes = web.RouteTableDef()

    async def pending_jobs(project_dir):
        return pending(project_dir)

    sdk_routes.register_sdk_routes(routes, resolve_project=world.resolve, remember_event_loop=lambda: None,
                                   projects_dir=lambda: "", pending_jobs=pending_jobs)
    app = web.Application()
    app.add_routes(routes)
    return TestClient(TestServer(app))


def test_the_switch_route_reports_queued_jobs_and_the_menu_needs_the_secret(world, monkeypatch):
    async def go():
        session = await world.open()
        headers = {sdk_routes.SECRET_HEADER: session.secret, sdk_routes.TAB_HEADER: "tab-1"}
        async with client_for(world, monkeypatch) as client:
            base = f"/sonder-editor/sdk/session/{session.session_id}"
            refused = await client.get(f"{base}/workflows", headers={sdk_routes.SECRET_HEADER: "wrong"})
            menu = await (await client.get(f"{base}/workflows", headers=headers)).json()
            stale = await client.post(f"{base}/workflow", headers=headers,
                                      json={"template_id": world.other_id, "expected_template_id": world.other_id})
            switched = await client.post(f"{base}/workflow", headers=headers,
                                         json={"template_id": world.other_id, "expected_template_id": world.template_id})
            state = await (await client.get(f"{base}/state", headers=headers)).json()
            return refused.status, menu, stale.status, await stale.json(), switched.status, await switched.json(), state

    refused, menu, stale, stale_body, status, body, state = asyncio.run(go())
    assert refused == 404 and menu["current"] == world.template_id
    assert stale == 409 and stale_body["code"] == "workflow_changed"
    assert status == 200 and body == {"ok": True, "template_id": world.other_id, "template_name": "Detailing",
                                      "pending_jobs": 3}
    assert state["template_id"] == world.other_id and state["template_name"] == "Detailing"
