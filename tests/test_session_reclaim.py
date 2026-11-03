"""A mounted tab's reclaim across a reload, and what session ids other pages can read
(0.8 hardening Phase 8)."""

import asyncio
import copy
import importlib
import json
from types import SimpleNamespace

import pytest
from aiohttp import web

import server
from server import routes
from server import session_registry as sr

PID, HID, NODE = "ZZ-reclaim", "host-A", "node-1"
SECRET = "s" * 64
OTHER_SECRET = "z" * 64


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    for name in ("_sessions", "_canvas_hosts", "_handoffs", "_tab_reclaims", "_widget_state", "_widget_sources", "_controls_relay"):
        monkeypatch.setattr(sr, name, {})
    monkeypatch.setattr(sr, "_lock", asyncio.Lock())
    monkeypatch.setattr(sr, "_sdk_secret_for_host", None)
    events = []
    monkeypatch.setattr(sr, "schedule_project_event", lambda p, e: events.append((p, copy.deepcopy(e))))

    async def broadcast(p, e):
        events.append((p, copy.deepcopy(e)))

    monkeypatch.setattr(sr, "broadcast_project_event", broadcast)
    return events


def info():
    return {"host_id": HID, "source_node_id": NODE, "browser_instance_id": "sonder-editor-tab-x"}


async def mount(session="tab-1", secret=SECRET):
    """Fullscreen claims and seeds, hands off, and the tab claims."""
    assert (await sr.claim_session(PID, "fs", "fullscreen", {"host_id": HID, "source_node_id": NODE}))["ok"]
    await sr.seed_widget_state(PID, NODE, "fs", {"scene_id": "s1", "selection_start": 4, "selection_end": 9}, host_id=HID)
    token = (await sr.create_handoff(PID, "fs", host_id=HID))["token"]
    claim = await sr.claim_session(PID, session, "tab", info(), handoff_token=token, reclaim_secret=secret)
    assert claim["ok"]
    return token, claim


async def reload_claim(token, session="tab-2", secret=SECRET):
    return await sr.claim_session(PID, session, "tab", info(), handoff_token=token, reclaim_secret=secret)


def run(coro):
    return asyncio.run(coro)


def test_a_reload_reclaims_with_a_new_session_and_its_values():
    async def scenario():
        token, _ = await mount()
        await sr.release_session(PID, "tab-1", host_id=HID)
        # Released as today: no owner and nothing in the shared state.
        assert await sr.get_owner(PID, host_id=HID) is None
        assert (await sr.get_widget_state(PID, NODE, host_id=HID))["state"] == {}
        claim = await reload_claim(token)
        assert claim["ok"] and claim["took_over"] is False
        assert claim["session_tag"] == sr.session_tag("tab-2")
        assert (await sr.get_owner(PID, host_id=HID))["session_tag"] == sr.session_tag("tab-2")
        assert (await sr.get_widget_state(PID, NODE, host_id=HID))["state"]["selection_end"] == 9
        # A second reload works too: the record lives as long as the tab does.
        await sr.release_session(PID, "tab-2", host_id=HID)
        assert (await reload_claim(token, session="tab-3"))["ok"]
    run(scenario())


def test_what_the_canvas_did_during_the_window_wins():
    async def scenario():
        token, _ = await mount()
        await sr.release_session(PID, "tab-1", host_id=HID)
        await sr.register_canvas_host(PID, NODE, "canvas", host_id=HID)
        await sr.update_widget_state(PID, NODE, "canvas", {"selection_end": 40}, host_id=HID)
        assert (await reload_claim(token))["ok"]
        state = (await sr.get_widget_state(PID, NODE, host_id=HID))["state"]
        assert state["selection_end"] == 40 and state["selection_start"] == 4
    run(scenario())


def test_a_canvas_poll_in_the_window_reads_nothing():
    """The kept values never reach the shared state a canvas applies from a poll."""
    async def scenario():
        await mount()
        await sr.release_session(PID, "tab-1", host_id=HID)
        payload = await sr.get_widget_state(PID, NODE, host_id=HID)
        assert payload["state"] == {} and payload["values"] == {}
    run(scenario())


def test_a_reclaim_over_a_live_or_orphaned_tab_takes_it_over_and_clears_the_relay(registry):
    async def scenario():
        for status in ("active", "orphaned"):
            sr._sessions.clear()
            token, _ = await mount(session=f"tab-{status}")
            sr._sessions[(PID, HID)]["status"] = status
            sr._controls_relay[(PID, HID)] = {"pending": [{"request_id": "r"}]}
            registry.clear()
            claim = await reload_claim(token, session=f"new-{status}")
            assert claim["ok"] and claim["took_over"] is True
            assert (PID, HID) not in sr._controls_relay
            changed = [e for _p, e in registry if e["type"] == "session_changed"]
            assert changed[-1]["reason"] == "taken_over"
            assert changed[-1]["owner"]["host_mode"] == "tab"
    run(scenario())


def test_the_old_pages_late_beacon_cannot_release_the_new_owner():
    async def scenario():
        token, _ = await mount()
        assert (await reload_claim(token))["took_over"] is True
        late = await sr.release_session(PID, "tab-1", host_id=HID)
        assert late["ok"] is False and late["code"] == "locked"
        assert (await sr.get_owner(PID, host_id=HID))["session_tag"] == sr.session_tag("tab-2")
    run(scenario())


@pytest.mark.parametrize("secret", [OTHER_SECRET, "", "short", 12345, None, "é" * 40])
def test_a_wrong_secret_with_a_spent_token_is_refused(secret):
    async def scenario():
        token, _ = await mount()
        await sr.release_session(PID, "tab-1", host_id=HID)
        claim = await reload_claim(token, secret=secret)
        assert claim["ok"] is False and claim["code"] == "invalid_handoff" and claim["owner"] is None
    run(scenario())


def test_a_secret_alone_without_a_record_or_a_token_is_refused():
    async def scenario():
        claim = await sr.claim_session(PID, "tab-1", "tab", info(), reclaim_secret=SECRET)
        assert claim["ok"] is False and claim["code"] == "invalid_handoff"
        assert not sr._tab_reclaims and await sr.get_owner(PID, host_id=HID) is None
    run(scenario())


def test_a_secret_with_no_token_reclaims():
    """A URL can lose its token; the stored secret is what proves the tab."""
    async def scenario():
        await mount()
        await sr.release_session(PID, "tab-1", host_id=HID)
        assert (await sr.claim_session(PID, "tab-2", "tab", info(), reclaim_secret=SECRET))["ok"]
    run(scenario())


def test_a_fresh_handoff_in_a_window_holding_an_old_secret_replaces_the_record():
    async def scenario():
        await mount(secret=OTHER_SECRET)
        await sr.release_session(PID, "tab-1", host_id=HID)
        token, claim = await mount(session="tab-2", secret=SECRET)
        assert claim["ok"]
        await sr.release_session(PID, "tab-2", host_id=HID)
        assert (await reload_claim(token, session="tab-3", secret=OTHER_SECRET))["ok"] is False
        assert (await reload_claim(token, session="tab-4", secret=SECRET))["ok"] is True
    run(scenario())


def test_a_handoff_without_a_secret_leaves_nothing_to_reclaim():
    async def scenario():
        await mount()
        await sr.release_session(PID, "tab-1", host_id=HID)
        token, _ = await mount(session="tab-2", secret="")
        assert (PID, HID) not in sr._tab_reclaims
        await sr.release_session(PID, "tab-2", host_id=HID)
        assert (await reload_claim(token, session="tab-3"))["ok"] is False
    run(scenario())


def test_opening_the_editor_in_the_window_ends_the_record_and_the_refusal_names_it():
    async def scenario():
        token, _ = await mount()
        await sr.release_session(PID, "tab-1", host_id=HID)
        assert (await sr.claim_session(PID, "fs-2", "fullscreen", {"host_id": HID, "source_node_id": NODE}))["ok"]
        assert (PID, HID) not in sr._tab_reclaims
        claim = await reload_claim(token)
        assert claim["ok"] is False and claim["owner"]["host_mode"] == "fullscreen"
    run(scenario())


def test_force_release_ends_the_record_with_or_without_an_owner():
    async def scenario():
        token, _ = await mount()
        await sr.release_session(PID, "anything", force=True, host_id=HID)
        assert (PID, HID) not in sr._tab_reclaims
        assert (await reload_claim(token))["ok"] is False
        # A stale card: the tab already released, and Force Release finds no owner.
        token, _ = await mount()
        await sr.release_session(PID, "tab-1", host_id=HID)
        await sr.release_session(PID, "anything", force=True, host_id=HID)
        assert (PID, HID) not in sr._tab_reclaims
        assert (await reload_claim(token))["ok"] is False
    run(scenario())


def test_orphan_expiry_ends_the_record():
    async def scenario():
        token, _ = await mount()
        sr._sessions[(PID, HID)].update(status="orphaned", orphan_expires_at=1.0)
        assert await sr.get_owner(PID, host_id=HID) is None
        assert (PID, HID) not in sr._tab_reclaims
        assert (await reload_claim(token))["ok"] is False
    run(scenario())


def test_the_window_expires_but_a_live_tabs_record_never_does(monkeypatch):
    async def scenario():
        token, _ = await mount()
        now = sr._now()
        monkeypatch.setattr(sr, "_now", lambda: now + 10 * sr.TAB_RECLAIM_TTL_SECONDS)
        sr._sessions[(PID, HID)]["last_seen"] = now + 10 * sr.TAB_RECLAIM_TTL_SECONDS
        await sr.sweep_stale_sessions_once()
        assert (PID, HID) in sr._tab_reclaims
        await sr.release_session(PID, "tab-1", host_id=HID)
        assert (PID, HID) in sr._tab_reclaims
        monkeypatch.setattr(sr, "_now", lambda: now + 30 * sr.TAB_RECLAIM_TTL_SECONDS)
        await sr.sweep_stale_sessions_once()
        assert (PID, HID) not in sr._tab_reclaims
        assert (await reload_claim(token))["ok"] is False
    run(scenario())


def test_an_app_host_never_takes_this_path(monkeypatch):
    monkeypatch.setattr(sr, "_sdk_secret_for_host", lambda host: "app-secret" if host == "sdk-1" else None)

    async def scenario():
        claim = await sr.claim_session(PID, "tab-1", "tab", {"host_id": "sdk-1"}, reclaim_secret=SECRET)
        assert claim["ok"] is False and claim["code"] == "invalid_sdk_session"
        assert not sr._tab_reclaims
        # Text the old comparison raised on, from a crafted request.
        non_ascii = await sr.claim_session(PID, "tab-1", "sdk", {"host_id": "sdk-1"}, sdk_secret="é" * 8)
        assert non_ascii["ok"] is False
    run(scenario())


def _raw_ids_in(payload, *ids):
    text = json.dumps(payload, default=str)
    return [value for value in ids if value in text]


def test_no_payload_names_a_live_session_or_the_secret(registry):
    tab, canvas, fullscreen = "tab-secret-id", "canvas-secret-id", "fs-secret-id"

    async def scenario():
        await sr.register_canvas_host(PID, NODE, canvas, host_id=HID)
        answers = [await sr.claim_session(PID, fullscreen, "fullscreen",
                                          {"host_id": HID, "source_node_id": NODE, "browser_instance_id": fullscreen})]
        await sr.seed_widget_state(PID, NODE, fullscreen, {"scene_id": "s1"}, host_id=HID)
        token = (await sr.create_handoff(PID, fullscreen, host_id=HID))["token"]
        answers.append(await sr.claim_session(PID, tab, "tab", {**info(), "browser_instance_id": ""}, handoff_token=token, reclaim_secret=SECRET))
        answers.append(await sr.heartbeat_session(PID, tab, host_id=HID))
        answers.append(await sr.get_owner(PID, host_id=HID))
        answers.append(await sr.get_canvas_host(PID, NODE, host_id=HID))
        answers.append(await sr.get_widget_state(PID, NODE, host_id=HID))
        answers.append(await sr.update_widget_state(PID, NODE, tab, {"selection_end": 3}, host_id=HID))
        answers.append(await sr.update_widget_state(PID, NODE, "someone-else", {"selection_end": 4}, host_id=HID))
        answers.append(await sr.heartbeat_session(PID, "someone-else", host_id=HID))
        await sr.publish_controls_view(PID, HID, canvas, {
            "view": {"bindings": [{"binding_id": "b", "status": "ok", "kind": "INT", "value": 1, "targets": []}], "notes": []},
            "view_seq": 1, "prompt_hash": "h", "applied": [], "rejected": []})
        answers.append(await sr.post_control_changes(PID, HID, tab, {"binding:b:label": "New"}))
        pending = await sr.get_control_changes(PID, HID, canvas)
        assert pending["pending"], pending
        answers.append(pending)
        answers.append(await sr.get_controls_view(PID, HID))
        await sr.release_session(PID, tab, host_id=HID)
        return answers

    answers = run(scenario())
    broadcast = [event for _project, event in registry]
    assert {event["type"] for event in broadcast} >= {"session_changed", "host_presence_changed", "widget_state_changed"}
    assert _raw_ids_in(answers, tab, canvas, fullscreen, SECRET) == []
    assert _raw_ids_in(broadcast, tab, canvas, fullscreen, SECRET) == []


def test_a_tag_opens_no_gate():
    async def scenario():
        await sr.register_canvas_host(PID, NODE, "canvas", host_id=HID)
        _, _claim = await mount()
        tag = sr.session_tag("tab-1")
        assert (await sr.heartbeat_session(PID, tag, host_id=HID))["ok"] is False
        assert (await sr.release_session(PID, tag, host_id=HID))["ok"] is False
        assert (await sr.post_control_changes(PID, HID, tag, {"binding:b:label": "x"}))["code"] == "not_owner"
        assert (await sr.get_control_changes(PID, HID, sr.session_tag("canvas")))["ok"] is False
    run(scenario())


def test_only_a_tab_owner_publishes_a_window_name():
    async def scenario():
        owner = await sr.claim_session(PID, "fs", "fullscreen", {"host_id": HID, "browser_instance_id": "fs"})
        assert owner["owner"]["browser_instance_id"] == ""
        token = (await sr.create_handoff(PID, "fs", host_id=HID))["token"]
        tab = await sr.claim_session(PID, "tab-1", "tab", info(), handoff_token=token, reclaim_secret=SECRET)
        assert tab["owner"]["browser_instance_id"] == "sonder-editor-tab-x"
    run(scenario())


class _Request:
    def __init__(self, remote="127.0.0.1", host="127.0.0.1:8188", headers=None):
        self.match_info = {"project_id": PID}
        self.query = {}
        self.remote = remote
        self.headers = {"Host": host, **(headers or {})}


def _handler(monkeypatch, path):
    fake_prompt_server = SimpleNamespace(instance=SimpleNamespace(routes=web.RouteTableDef()))
    monkeypatch.setattr(server, "PromptServer", fake_prompt_server, raising=False)
    for route in importlib.reload(routes).routes:
        if route.method == "GET" and route.path == path:
            return route.handler
    raise AssertionError(f"Route not found: {path}")


@pytest.mark.parametrize("path", ["/sonder-editor/session/{project_id}/debug", "/sonder-editor/session/{project_id}/diag"])
def test_session_diagnostics_stay_on_the_machine(monkeypatch, path):
    handler = _handler(monkeypatch, path)
    assert asyncio.run(handler(_Request())).status == 200
    for refused in (_Request(remote="192.168.1.20"),
                    _Request(headers={"X-Forwarded-For": "192.168.1.20"}),
                    _Request(host="evil.example:8188")):
        response = asyncio.run(handler(refused))
        assert response.status == 403
        assert json.loads(response.body.decode("utf-8"))["code"] == "not_local"
