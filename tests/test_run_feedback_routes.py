"""Run feedback routes (live-workflow Phase 3A): the register route's record and
Modes capture, the Modes-only route's ownership check, and the gated feedback read."""

import asyncio
import json
import threading

import pytest

from server import run_feedback, run_output_routes, sdk_progress, sdk_runs
from test_run_output_routes import CANVAS, HID, PID, RECEIPT, TAB, client, prompt, registry, run, setup_hosts  # noqa: F401


@pytest.fixture(autouse=True)
def feedback(monkeypatch):
    run_feedback.reset_for_tests()
    sdk_progress.reset_for_tests()
    sent = []
    run_feedback._HOOKS["emit"] = lambda alias, event: sent.append(json.loads(json.dumps(event)))
    run_feedback._HOOKS["queued_ids"] = lambda: set()
    monkeypatch.setattr(run_feedback, "REAP_INTERVAL_SEC", 3600.0)
    monkeypatch.setattr(run_output_routes, "_reap_removed_runs", lambda: None)
    yield sent
    run_feedback.reset_for_tests()
    sdk_progress.reset_for_tests()


def body(**extra):
    return {"receipt": RECEIPT, "project_id": PID, "host_id": HID, "session_id": CANVAS, "editors": ["ed"], **extra}


def test_a_late_registration_sends_the_runs_record_and_replies_before_describing(feedback, monkeypatch):
    gate = threading.Event()
    captured = []

    def slow_capture(controls):
        gate.wait(5)
        captured.append(controls)
        return [{"id": "task", "name": "Task", "status": "custom", "choice": "", "matches": []}]

    monkeypatch.setattr(run_feedback, "capture_canvas_modes", slow_capture)

    async def scenario():
        await setup_hosts()
        graph = prompt()
        sdk_runs.bind_queued("p1", graph, ["out"])  # put before the registration: provisional
        run_feedback.on_bound("p1", graph)
        assert feedback == []
        async with client() as http:
            response = await http.post("/sonder-editor/runs/register", json=body(controls={"prompt": graph}))
            assert response.status == 200 and (await response.json())["prompt_id"] == "p1"
            assert captured == []  # replied while the describe still waits
            assert feedback and feedback[-1]["record"]["state"] == "queued" and "receipt" in feedback[-1]["record"]
            gate.set()
            for _ in range(100):
                if feedback[-1]["record"].get("receipt", {}).get("modes"):
                    break
                await asyncio.sleep(0.02)
        return feedback[-1]["record"]["receipt"]["modes"]

    assert run(scenario()) == [{"id": "task", "name": "Task", "status": "custom", "choice": "", "matches": []}]


def test_modes_only_requires_the_receipts_own_host(feedback, monkeypatch):
    monkeypatch.setattr(run_feedback, "capture_canvas_modes", lambda controls: [])

    async def scenario():
        await setup_hosts()
        sdk_runs.register_receipt(RECEIPT, ("host", PID, "someone-else"))
        async with client() as http:
            refused = await http.post("/sonder-editor/runs/register/modes", json=body(controls={"prompt": {}}))
            unknown = await http.post("/sonder-editor/runs/register/modes",
                                      json=body(receipt="dddddddd-0000-4000-8000-000000000009", controls={"prompt": {}}))
            return refused.status, unknown.status

    assert run(scenario()) == (404, 404)


def test_an_oversized_registration_is_refused_as_too_large(monkeypatch):
    monkeypatch.setattr(run_output_routes, "MAX_REGISTER_BYTES", 64)

    async def scenario():
        await setup_hosts()
        async with client() as http:
            response = await http.post("/sonder-editor/runs/register", json=body(controls={"prompt": {"x": "y" * 200}}))
            return response.status, await response.json()

    assert run(scenario()) == (413, {"ok": False, "code": "body_too_large"})


def test_the_feedback_read_answers_only_the_hosts_sessions(feedback):
    async def scenario():
        await setup_hosts()
        graph = prompt()
        sdk_runs.register_receipt(RECEIPT, ("host", PID, HID))
        sdk_runs.bind_queued("p1", graph, ["out"])
        run_feedback.on_bound("p1", graph)
        async with client() as http:
            url = f"/sonder-editor/project/{PID}/runs/feedback"
            ok = await http.get(url, params={"host_id": HID, "session_id": TAB})
            refused = await http.get(url, params={"host_id": HID, "session_id": "nope"})
            data = await ok.json()
            return ok.status, refused.status, data

    ok, refused, data = run(scenario())
    assert (ok, refused) == (200, 403)
    assert data["epoch"] == run_feedback.EPOCH and [r["prompt_id"] for r in data["records"]] == ["p1"]
    assert data["records"][0]["receipt"]["requested"][0]["editor"] == "ed"
