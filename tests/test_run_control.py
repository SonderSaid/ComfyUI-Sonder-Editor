"""Cancel an owned prompt (2B), the mounted run lane, and Sonder's helpers scoped to what runs."""

import asyncio
import types

import pytest

from server import prompt_graph, run_control, run_outputs, run_scope, sdk_runs, session_registry as sr
from test_run_output_routes import CANVAS, HID, OWNER, PID, RECEIPT, TAB, client, prompt, registry, run, setup_hosts  # noqa: F401


class Queue:
    """ComfyUI's PromptQueue cancel surface: atomic delete and interrupt-if-running."""

    def __init__(self, queued=(), running=()):
        self.queue = [(0, pid, {}, {}, [], {}) for pid in queued]
        self.running = list(running)
        self.interrupted = []

    def delete_queue_item(self, function):
        for index, item in enumerate(self.queue):
            if function(item):
                self.queue.pop(index)
                return True
        return False

    def interrupt_if_running(self, prompt_id):
        if prompt_id in self.running:
            self.interrupted.append(prompt_id)
            return True
        return False


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(sr, "_run_requests", {})
    sdk_runs.reset_for_tests()
    run_control.reset_for_tests()
    yield
    sdk_runs.reset_for_tests()
    run_control.reset_for_tests()


# --- Cancel ------------------------------------------------------------------------------

def test_cancel_dequeues_interrupts_or_flags():
    queue = Queue(queued=["q"], running=["r"])
    assert run_control.cancel_prompt("q", queue) == run_control.CANCEL_DEQUEUED and queue.queue == []
    assert run_control.cancel_prompt("r", queue) == run_control.CANCEL_INTERRUPTED and queue.interrupted == ["r"]
    # Finished, or not queued yet: only the flag, which nothing acts on for a finished prompt.
    assert run_control.cancel_prompt("done", queue) == run_control.CANCEL_FLAGGED
    assert sdk_runs.cancel_requested("done") and queue.interrupted == ["r"]


def test_a_cancel_during_validation_is_applied_right_after_put():
    queue = Queue()
    run_control.cancel_prompt("early", queue)  # its /prompt is still validating
    queue.queue.append((0, "early", {}, {}, [], {}))  # then ComfyUI puts it
    run_control.after_put(queue.queue[0], queue)
    assert queue.queue == []
    queue.queue.append((0, "other", {}, {}, [], {}))
    run_control.after_put(queue.queue[0], queue)
    assert len(queue.queue) == 1, "an unflagged prompt stays queued"


def test_a_cancel_between_get_and_the_flag_reset_is_reapplied_at_execution_start():
    queue = Queue(running=["p"])
    sdk_runs.request_cancel("p")
    run_control.observe("executing", {"prompt_id": "p"}, queue)
    assert queue.interrupted == []
    run_control.observe("execution_start", {"prompt_id": "p"}, queue)
    run_control.observe("execution_start", {"prompt_id": "unrelated"}, Queue(running=["unrelated"]))
    assert queue.interrupted == ["p"]


def test_the_put_hook_applies_an_early_cancel(tmp_path, monkeypatch):
    from test_editor_node import _import_io_nodes

    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    puts = []

    class PromptQueue:
        def put(self, item):
            puts.append(item)

        def delete_queue_item(self, function):
            for index, item in enumerate(puts):
                if function(item):
                    puts.pop(index)
                    return True
            return False

        def get(self, *args, **kwargs):
            return None

        def task_done(self, *args, **kwargs):
            return None

    queue = PromptQueue()
    server = types.SimpleNamespace(prompt_queue=queue)
    monkeypatch.setattr(io_nodes, "_BRIDGE_HOOKED_PROMPT_QUEUE_ID", None)
    monkeypatch.setitem(__import__("sys").modules, "server", types.SimpleNamespace(PromptServer=types.SimpleNamespace(instance=server)))
    io_nodes._install_bridge_prompt_queue_hooks()
    io_nodes.sdk_runs.request_cancel("early")
    queue.put((0, "early", {}, {}, [], {}))
    queue.put((0, "kept", {}, {}, [], {}))
    assert [item[1] for item in puts] == ["kept"]


def test_cancel_route_needs_the_owner_and_an_owned_prompt(monkeypatch):
    calls = []
    monkeypatch.setattr(run_control, "cancel_prompt", lambda pid, queue=None: calls.append(pid) or "dequeued")

    async def scenario():
        await setup_hosts()
        sdk_runs.register_receipt(RECEIPT, OWNER, ["ed"])
        sdk_runs.bind_queued("p1", prompt(), ["out"])
        async with client() as http:
            path = f"/sonder-editor/project/{PID}/runs/p1/cancel"
            stranger = await http.post(path, json={"host_id": HID, "session_id": "nobody"})
            unknown = await http.post(f"/sonder-editor/project/{PID}/runs/nope/cancel", json={"host_id": HID, "session_id": TAB})
            owned = await http.post(path, json={"host_id": HID, "session_id": TAB})
            return stranger.status, unknown.status, owned.status, await owned.json()

    stranger, unknown, owned, payload = run(scenario())
    assert (stranger, unknown, owned) == (403, 404, 200) and payload == {"ok": True, "cancel": "dequeued"}
    assert calls == ["p1"]


# --- Mounted run lane ----------------------------------------------------------------------

REQUEST = "aaaaaaaa-1111-4111-8111-000000000001"


def test_the_tab_posts_the_canvas_claims_once_and_retries_answer_the_outcome():
    async def scenario():
        await setup_hosts()
        refused = await sr.post_run_request(PID, HID, CANVAS, REQUEST, "4")
        posted = await sr.post_run_request(PID, HID, TAB, REQUEST, "4")
        again = await sr.post_run_request(PID, HID, TAB, REQUEST, "4")
        conflict = await sr.post_run_request(PID, HID, TAB, REQUEST, "5")
        tab_claim = await sr.claim_run_requests(PID, HID, TAB)
        claimed = await sr.claim_run_requests(PID, HID, CANVAS)
        claimed_again = await sr.claim_run_requests(PID, HID, CANVAS)
        in_flight = await sr.post_run_request(PID, HID, TAB, REQUEST, "4")
        # ComfyUI's put binds the run under the request id, registered at claim.
        receipt = sdk_runs.receipt_state(REQUEST)
        sdk_runs.bind_queued("p9", {"ed": {"class_type": "SonderEditor", "inputs": {}, "_meta": {"sonder_receipt": REQUEST}}}, ["ed"])
        bound = await sr.post_run_request(PID, HID, TAB, REQUEST, "4")
        reported = await sr.report_run_request(PID, HID, CANVAS, REQUEST, {"state": "queued", "prompt_id": "p9"})
        final = await sr.post_run_request(PID, HID, TAB, REQUEST, "4")
        return locals()

    r = run(scenario())
    assert r["refused"]["code"] == "not_owner" and r["tab_claim"]["code"] == "not_canvas_host"
    assert r["posted"]["state"] == "pending" and r["again"]["state"] == "pending"
    assert r["conflict"]["code"] == "request_conflict"
    assert r["claimed"]["requests"] == [{"request_id": REQUEST, "target": "4"}] and r["claimed_again"]["requests"] == []
    assert r["in_flight"]["state"] == "claimed"
    assert r["receipt"]["owner"] == OWNER and r["receipt"]["purpose"] == "branch"
    assert sdk_runs.bound_run("p9")["owners"] == [OWNER] and sdk_runs.bound_run("p9")["purpose"] == "branch"
    assert r["bound"] == {"ok": True, "request_id": REQUEST, "state": "queued", "prompt_id": "p9"}
    assert r["reported"]["ok"] and r["final"]["state"] == "queued"


def test_a_claim_lost_with_its_canvas_reads_unknown_and_is_never_handed_out_again():
    async def scenario():
        await setup_hosts()
        await sr.post_run_request(PID, HID, TAB, REQUEST, "4")
        await sr.claim_run_requests(PID, HID, CANVAS)
        # The canvas reloads before queueing or reporting; nothing bound the receipt.
        sr._run_requests[sr._host_key(PID, HID)][REQUEST]["claimed_at"] -= sr.RUN_REQUEST_CLAIM_GRACE_SEC + 1
        answer = await sr.post_run_request(PID, HID, TAB, REQUEST, "4")
        reclaim = await sr.claim_run_requests(PID, HID, CANVAS)
        return answer, reclaim

    answer, reclaim = run(scenario())
    assert answer["state"] == "unknown" and reclaim["requests"] == []


def test_the_lane_is_bounded_and_validates():
    async def scenario():
        await setup_hosts()
        bad_id = await sr.post_run_request(PID, HID, TAB, "not-an-id", "4")
        bad_target = await sr.post_run_request(PID, HID, TAB, REQUEST, "")
        results = [await sr.post_run_request(PID, HID, TAB, f"aaaaaaaa-1111-4111-8111-00000000000{i}", "4")
                   for i in range(sr.RUN_REQUESTS_MAX_PENDING + 1)]
        bad_report = await sr.report_run_request(PID, HID, CANVAS, REQUEST, {"state": "landed"})
        return bad_id, bad_target, results, bad_report

    bad_id, bad_target, results, bad_report = run(scenario())
    assert bad_id["code"] == "invalid_request" and bad_target["code"] == "invalid_target"
    assert [r.get("code") or r.get("state") for r in results] == ["pending"] * sr.RUN_REQUESTS_MAX_PENDING + ["queue_full"]
    assert bad_report["code"] == "invalid_outcome"


# --- Helpers scoped to what runs (B1) -----------------------------------------------------------

def two_pass():
    """An Editor feeding a marked Save Video, plus a first-pass preview of the Editor's frames."""
    return {
        "ed": {"class_type": "SonderEditor", "inputs": {}},
        "pass1": {"class_type": "Sampler", "inputs": {"frames": ["ed", 1]}},
        "preview": {"class_type": "PreviewImage", "inputs": {"images": ["pass1", 0]}},
        "pass2": {"class_type": "Sampler", "inputs": {"frames": ["pass1", 0], "audio": ["ed", 10]}},
        "save": {"class_type": "SonderSaveVideo", "inputs": {"project": ["ed", 0], "frames": ["pass2", 0],
                                                            "mark_queue_complete": True}},
        "bridge": {"class_type": "SonderSaveBridge", "inputs": {"project": ["ed", 0], "mark_queue_complete": True}},
        "external": {"class_type": "SaveImage", "inputs": {"filename_prefix": ["bridge", 0], "images": ["pass2", 0]}},
    }


def test_scope_comes_from_the_open_release_with_no_registration():
    run_scope.reset_for_tests({name: False for name in ("SonderEditor", "Sampler", "PreviewImage", "SonderSaveVideo",
                                                         "SonderSaveBridge", "SaveImage")})
    prompt = two_pass()
    assert run_scope.for_prompt(prompt) is None  # not running: unknown
    sdk_runs.open_release("1", "p1", prompt, ["preview"])
    assert run_scope.for_prompt(prompt) == {"ed", "pass1", "preview"}
    assert run_scope.for_prompt(dict(prompt)) is None, "only the queued prompt object is trusted"
    sdk_runs.open_release("2", "p2", two_pass(), None)
    run_scope.reset_for_tests()


def test_a_preview_branch_reaches_no_save_and_demands_only_what_it_uses(tmp_path, monkeypatch):
    from test_editor_node import _import_editor_node

    editor = _import_editor_node(tmp_path, monkeypatch).SonderEditor
    prompt = two_pass()
    branch = {"ed", "pass1", "preview"}
    full = None
    assert editor._execution_reaches_terminal_save(prompt, "ed", full) is True
    assert editor._execution_reaches_terminal_save(prompt, "ed", branch) is False  # peeks
    assert editor._referenced_output_slots(prompt, "ed", full) == {0, 1, 10}
    assert editor._referenced_output_slots(prompt, "ed", branch) == {1}
    assert editor._execution_reaches_color_correcting_save_video(prompt, "ed", full) is True
    assert editor._execution_reaches_color_correcting_save_video(prompt, "ed", branch) is False
    # The Save Bridge is reached only when an output downstream of it runs.
    with_bridge = branch | {"bridge", "external", "pass2"}
    assert prompt_graph.nodes_reaching(prompt, "ed", prompt_graph.SONDER_SAVE_CLASS_TYPES, with_bridge) == ["bridge"]


def test_save_bridge_provenance_names_only_the_saves_that_run(tmp_path, monkeypatch):
    from test_editor_node import _import_io_nodes

    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    prompt = two_pass()
    prompt["preview2"] = {"class_type": "PreviewImage", "inputs": {"images": ["bridge", 0]}}
    seen = []
    monkeypatch.setattr(io_nodes, "_compact_editor_export",
                        lambda project, context, **kwargs: seen.append(kwargs.get("controls_consumers")) or {})
    monkeypatch.setattr(io_nodes.run_scope, "for_prompt", lambda p: frozenset({"ed", "bridge", "external", "pass1", "pass2"}))
    io_nodes._bridge_generation_params(None, {}, prompt=prompt, consumer_unique_id="bridge")
    monkeypatch.setattr(io_nodes.run_scope, "for_prompt", lambda p: None)
    io_nodes._bridge_generation_params(None, {}, prompt=prompt, consumer_unique_id="bridge")
    assert seen == [["bridge", "external"], ["bridge", "external", "preview2"]]


def test_an_older_comfyui_without_atomic_cancel_is_refused_not_flagged():
    queue = types.SimpleNamespace(delete_queue_item=lambda fn: False)
    assert run_control.cancel_prompt("p", queue) == run_control.CANCEL_UNSUPPORTED
    assert not sdk_runs.cancel_requested("p")


def test_the_canvas_claims_one_request_at_a_time():
    async def scenario():
        await setup_hosts()
        for index in range(3):
            await sr.post_run_request(PID, HID, TAB, f"aaaaaaaa-1111-4111-8111-00000000000{index}", str(index))
        claims = [await sr.claim_run_requests(PID, HID, CANVAS) for _ in range(4)]
        return [claim["requests"] for claim in claims]

    claims = run(scenario())
    assert [[r["target"] for r in requests] for requests in claims] == [["0"], ["1"], ["2"], []]
