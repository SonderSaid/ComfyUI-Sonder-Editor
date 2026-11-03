"""Run registration and observed-output routes: gate, pairing, reads, holds."""

import asyncio
import copy
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from server import run_output_routes, run_outputs, run_scope, sdk_runs, session_registry as sr

PID, HID, CANVAS, TAB = "ZZ-run-outputs-20261022", "host-A", "canvas-A", "tab-A"
OWNER = ("host", PID, HID)
RECEIPT = "dddddddd-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def registry(monkeypatch, tmp_path):
    for name in ("_sessions", "_canvas_hosts", "_handoffs", "_tab_reclaims", "_widget_state", "_widget_sources", "_controls_relay"):
        monkeypatch.setattr(sr, name, {})
    monkeypatch.setattr(sr, "_lock", asyncio.Lock())
    monkeypatch.setattr(sr, "_sdk_secret_for_host", None)
    monkeypatch.setattr(sr, "schedule_project_event", lambda p, e: None)

    async def broadcast(p, e):
        return None
    monkeypatch.setattr(sr, "broadcast_project_event", broadcast)
    sdk_runs.reset_for_tests()
    run_outputs.reset_for_tests()
    run_scope.reset_for_tests({"SonderEditor": False, "Out": False})
    (tmp_path / "temp").mkdir()
    (tmp_path / "temp" / "a.png").write_bytes(b"png")
    monkeypatch.setattr(run_output_routes, "_file_path",
                        lambda file: str(tmp_path / file["type"] / file["filename"]) if file["type"] == "temp" else None)
    yield tmp_path
    sdk_runs.reset_for_tests()
    run_outputs.reset_for_tests()
    run_scope.reset_for_tests()


async def setup_hosts():
    assert (await sr.register_canvas_host(PID, "editor-1", CANVAS, host_id=HID))["ok"]
    info = {"host_id": HID, "source_node_id": "editor-1"}
    assert (await sr.claim_session(PID, TAB + "-fs", "fullscreen", info))["ok"]
    handoff = await sr.create_handoff(PID, TAB + "-fs", host_id=HID)
    assert (await sr.claim_session(PID, TAB, "tab", info, handoff_token=handoff["token"]))["ok"]


def prompt():
    return {"ed": {"class_type": "SonderEditor", "inputs": {}, "_meta": {"sonder_receipt": RECEIPT}},
            "out": {"class_type": "Out", "inputs": {"p": ["ed", 0]}}}


def run_and_publish(graph, filename="a.png"):
    sdk_runs.bind_queued("p1", graph, ["out"])
    sdk_runs.open_release("1", "p1", graph, ["out"])
    run_outputs.observe("executing", {"node": "out", "prompt_id": "p1"})
    run_outputs.observe("executed", {"node": "out", "display_node": "out", "prompt_id": "p1",
                                     "output": {"images": [{"filename": filename, "type": "temp"}]}})


def client():
    routes = web.RouteTableDef()
    run_output_routes.register_run_output_routes(routes)
    app = web.Application()
    app.add_routes(routes)
    return TestClient(TestServer(app))


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_register_requires_the_canvas_session_and_pairs_with_the_queue():
    async def scenario():
        await setup_hosts()
        async with client() as http:
            body = {"receipt": RECEIPT, "project_id": PID, "host_id": HID, "editors": ["ed"]}
            refused = await http.post("/sonder-editor/runs/register", json={**body, "session_id": TAB})
            assert refused.status == 403  # the tab owner cannot register the canvas's runs
            graph = prompt()
            sdk_runs.bind_queued("p1", graph, ["out"])  # put came first
            ok = await http.post("/sonder-editor/runs/register", json={**body, "session_id": CANVAS})
            assert (await ok.json()) == {"ok": True, "prompt_id": "p1"}
            again = await http.post("/sonder-editor/runs/register", json={**body, "session_id": CANVAS})
            assert again.status == 400 and (await again.json())["code"] == "duplicate"
    run(scenario())


def test_reads_admit_canvas_and_owner_tab_and_mark_availability(registry):
    async def scenario():
        await setup_hosts()
        sdk_runs.register_receipt(RECEIPT, OWNER, ["ed"])
        run_and_publish(prompt())
        async with client() as http:
            path = f"/sonder-editor/project/{PID}/runs/outputs"
            for session in (CANVAS, TAB):
                resp = await http.get(path, params={"host_id": HID, "session_id": session, "sources": "out"})
                payload = await resp.json()
                assert resp.status == 200 and payload["sources"]["out"]["state"] == "ok"
                assert payload["sources"]["out"]["result"]["entries"][0]["available"] is True
            stranger = await http.get(path, params={"host_id": HID, "session_id": "nobody", "sources": "out"})
            assert stranger.status == 403
            other_host = await http.get(path, params={"host_id": "host-B", "session_id": CANVAS, "sources": "out"})
            assert other_host.status == 409
            (registry / "temp" / "a.png").unlink()
            gone = await (await http.get(path, params={"host_id": HID, "session_id": TAB, "sources": "out"})).json()
            assert gone["sources"]["out"]["result"]["entries"][0]["available"] is False
            assert gone["sources"]["out"]["state"] == "missing_file"
            # The owner ran, but never this source; a stranger owner has no runs at all.
            unrelated = await (await http.get(path, params={"host_id": HID, "session_id": TAB, "sources": "elsewhere"})).json()
            assert unrelated["sources"]["elsewhere"]["state"] == "not_in_run"
            one = await http.get(f"/sonder-editor/project/{PID}/runs/p1", params={"host_id": HID, "session_id": TAB})
            assert one.status == 200 and "out" in (await one.json())["sources"]
            missing = await http.get(f"/sonder-editor/project/{PID}/runs/nope", params={"host_id": HID, "session_id": TAB})
            assert missing.status == 404
    run(scenario())


def test_a_result_with_no_media_reads_unsupported(registry):
    async def scenario():
        await setup_hosts()
        sdk_runs.register_receipt(RECEIPT, OWNER, ["ed"])
        graph = prompt()
        sdk_runs.bind_queued("p1", graph, ["out"])
        sdk_runs.open_release("1", "p1", graph, ["out"])
        run_outputs.observe("executed", {"node": "out", "prompt_id": "p1", "output": {"text": ["hello"]}})
        async with client() as http:
            payload = await (await http.get(f"/sonder-editor/project/{PID}/runs/outputs",
                                            params={"host_id": HID, "session_id": CANVAS, "sources": "out"})).json()
            assert payload["sources"]["out"]["state"] == "unsupported"
    run(scenario())


def test_holds_survive_and_report_a_changed_file_unavailable(registry):
    async def scenario():
        await setup_hosts()
        sdk_runs.register_receipt(RECEIPT, OWNER, ["ed"])
        run_and_publish(prompt())
        async with client() as http:
            fields = {"host_id": HID, "session_id": TAB}
            made = await http.post(f"/sonder-editor/project/{PID}/runs/hold",
                                   json={**fields, "prompt_id": "p1", "source": "out", "index": 0})
            hold_id = (await made.json())["hold_id"]
            read = lambda: http.get(f"/sonder-editor/project/{PID}/runs/outputs", params={**fields, "sources": "out"})
            assert (await (await read()).json())["holds"][0]["state"] == "ok"
            (registry / "temp" / "a.png").write_bytes(b"different bytes, same name")
            assert (await (await read()).json())["holds"][0]["state"] == "unavailable"
            released = await http.post(f"/sonder-editor/project/{PID}/runs/release", json={**fields, "hold_id": hold_id})
            assert released.status == 200
            assert (await (await read()).json())["holds"] == []
    run(scenario())


def test_the_gate_refuses_sdk_hosts(monkeypatch):
    async def scenario():
        monkeypatch.setattr(sr, "_sdk_secret_for_host", lambda host_id: "secret")
        code, _owner = await sr.run_owner_gate(PID, "sdk-1", "x")
        assert code == "invalid_host"
    run(scenario())


def test_reads_reap_a_bound_prompt_that_left_the_queue(monkeypatch):
    def receipt(n):
        return {**prompt(), "ed": {**prompt()["ed"], "_meta": {"sonder_receipt": f"dddddddd-0000-4000-8000-00000000000{n}"}}}

    sdk_runs.register_receipt(RECEIPT, OWNER, ["ed"])
    sdk_runs.bind_queued("gone", prompt(), ["out"])
    sdk_runs.bind_queued("waiting", receipt(2), ["out"])
    monkeypatch.setattr(sdk_runs, "REMOVED_GRACE_SEC", -1)
    monkeypatch.setattr(run_outputs, "queued_prompt_ids", lambda queue=None: {"waiting"})
    run_output_routes._reap_removed_runs()
    assert sdk_runs.bound_run("gone")["outcome"] == run_outputs.OUTCOME_REMOVED
    assert not sdk_runs.bound_run("waiting")["finished"]
    # A prompt ComfyUI took is never reaped, even when it is gone from the queue lists.
    sdk_runs.open_release("9", "waiting", prompt(), ["out"])
    monkeypatch.setattr(run_outputs, "queued_prompt_ids", lambda queue=None: set())
    run_output_routes._reap_removed_runs()
    assert not sdk_runs.bound_run("waiting")["finished"]
    # Unknown queue: nothing is reaped.
    monkeypatch.setattr(run_outputs, "queued_prompt_ids", lambda queue=None: None)
    sdk_runs.bind_queued("unknown", receipt(3), ["out"])
    run_output_routes._reap_removed_runs()
    assert not sdk_runs.bound_run("unknown")["finished"]


def test_reads_refresh_only_the_holds_a_page_still_shows(registry, monkeypatch):
    """Output Views 3A: a closed page's holds age out; pagehide releases a page's holds at once."""
    async def scenario():
        await setup_hosts()
        sdk_runs.register_receipt(RECEIPT, OWNER, ["ed"])
        run_and_publish(prompt())
        async with client() as http:
            fields = {"host_id": HID, "session_id": TAB}
            hold = lambda: http.post(f"/sonder-editor/project/{PID}/runs/hold",
                                     json={**fields, "prompt_id": "p1", "source": "out", "index": 0})
            mine = (await (await hold()).json())["hold_id"]
            orphan = (await (await hold()).json())["hold_id"]
            # The hold store's own clock only: the session registry keeps real time.
            clock = [run_outputs.time.time()]
            monkeypatch.setattr(run_outputs, "time", SimpleNamespace(time=lambda: clock[0]))
            read = lambda keep: http.get(f"/sonder-editor/project/{PID}/runs/outputs",
                                         params={**fields, "sources": "out", "keep_holds": keep})
            for _ in range(3):  # a keepalive read every 20 minutes names only this page's hold
                clock[0] += 20 * 60
                payload = await (await read(mine)).json()
            assert [h["hold_id"] for h in payload["holds"]] == [mine]
            # Entries and holds carry the file's revision, as text (nanoseconds exceed 2^53).
            assert payload["holds"][0]["entry"]["revision"].startswith("3:")
            assert payload["sources"]["out"]["result"]["entries"][0]["revision"] == payload["holds"][0]["entry"]["revision"]
            assert orphan not in {h["hold_id"] for h in payload["holds"]}
            # Hold is not refused for the orphans: they are gone.
            others = [(await (await hold()).json()).get("hold_id") for _ in range(run_outputs.MAX_HOLDS_PER_OWNER - 1)]
            assert all(others)
            # A closing page releases all of its holds in one request.
            gone = await http.post(f"/sonder-editor/project/{PID}/runs/release", json={**fields, "hold_ids": [mine, *others, "nope"]})
            assert (await gone.json()) == {"ok": True, "released": run_outputs.MAX_HOLDS_PER_OWNER}
            assert (await (await read("")).json())["holds"] == []
    run(scenario())


def test_owner_tab_cancels_a_run_its_host_owns(registry, monkeypatch):
    cancelled = []
    monkeypatch.setattr(run_output_routes.run_control, "cancel_prompt", lambda pid, queue=None: cancelled.append(pid) or "deleted")

    async def scenario():
        await setup_hosts()
        sdk_runs.register_receipt(RECEIPT, OWNER, ["ed"])
        run_and_publish(prompt())
        async with client() as http:
            ok = await http.post(f"/sonder-editor/project/{PID}/runs/p1/cancel", json={"host_id": HID, "session_id": TAB})
            assert ok.status == 200 and (await ok.json())["cancel"] == "deleted"
            unknown = await http.post(f"/sonder-editor/project/{PID}/runs/zzz/cancel", json={"host_id": HID, "session_id": TAB})
            assert unknown.status == 404
            stranger = await http.post(f"/sonder-editor/project/{PID}/runs/p1/cancel", json={"host_id": HID, "session_id": "nobody"})
            assert stranger.status == 403
    run(scenario())
    assert cancelled == ["p1"]


def test_a_hold_keeps_its_runs_editor_context_through_the_route(registry):
    """Phase 3B: a held result links like a fresh one, even once its run's data is pruned."""
    async def scenario():
        await setup_hosts()
        sdk_runs.register_receipt(RECEIPT, OWNER, ["ed"])
        run_and_publish(prompt())
        context = {"scene_id": "s1", "fps": 24.0, "render_start": 0, "render_end": 8, "content": {"v": 1, "digest": "d"}}
        with run_outputs._LOCK:
            run_outputs._DATA["p1"]["editor_context"]["ed"] = dict(context)
        async with client() as http:
            fields = {"host_id": HID, "session_id": TAB}
            made = await http.post(f"/sonder-editor/project/{PID}/runs/hold",
                                   json={**fields, "prompt_id": "p1", "source": "out", "index": 0})
            assert made.status == 200
            with run_outputs._LOCK:
                run_outputs._DATA.clear()  # the run's observed data is gone
            read = await http.get(f"/sonder-editor/project/{PID}/runs/outputs", params={**fields, "sources": "out"})
            hold = (await read.json())["holds"][0]
            assert hold["editor_context"] == {"ed": context}
            assert hold["fresh"] is True
    run(scenario())


def test_live_reads_and_bytes_use_the_same_gate(registry):
    async def scenario():
        await setup_hosts()
        async with client() as http:
            refused = await http.get(f"/sonder-editor/project/{PID}/runs/live",
                                     params={"host_id": HID, "session_id": "nobody", "sources": "out"})
            assert refused.status in (403, 409)
            ok = await http.get(f"/sonder-editor/project/{PID}/runs/live",
                                params={"host_id": HID, "session_id": CANVAS, "sources": "out"})
            body = await ok.json()
            assert ok.status == 200 and body["sources"]["out"]["state"] == "none" and body["seen"] == []
            assert "kj" in body["sampling"]
            missing = await http.get(f"/sonder-editor/project/{PID}/runs/live/p1/bytes",
                                     params={"host_id": HID, "session_id": TAB, "source": "out"})
            assert missing.status == 404
            capability = await (await http.get("/sonder-editor/runs/capability")).json()
            assert "branch" in capability and capability["sampling"]["kj"]["state"]
    run(scenario())
