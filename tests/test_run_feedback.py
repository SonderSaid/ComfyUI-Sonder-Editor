"""Run feedback (live-workflow Phase 3A): one record per bound run, delivered as
``run_progress`` with declared stages, native counts, a receipt, and a terminal
record from the run's release (plans/live-workflow-phase-3-job-feedback.md)."""
import json
import threading
import time
import types

import pytest

from server import control_descriptors as cd, run_feedback, run_outputs, sdk_progress, sdk_runner, sdk_runs
from test_control_descriptors import CLASSES, _binding, _node

OWNER = ("host", "proj", "host-1")
RECEIPT = "11111111-2222-4333-8444-555555555555"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    sdk_runs.reset_for_tests()
    sdk_progress.reset_for_tests()
    run_feedback.reset_for_tests()
    run_outputs.reset_for_tests()
    sent = []
    run_feedback._HOOKS["emit"] = lambda alias, event: sent.append((alias, json.loads(json.dumps(event))))
    run_feedback._HOOKS["queued_ids"] = lambda: set()
    monkeypatch.setattr(run_feedback, "REAP_INTERVAL_SEC", 3600.0)
    run_feedback.install()
    yield sent
    run_feedback.reset_for_tests()
    sdk_progress.reset_for_tests()
    sdk_runs.reset_for_tests()


def editor_prompt(receipt=RECEIPT, token=None, start=0, end=48, queue=False):
    meta = {"sonder_receipt": receipt} if receipt else {}
    if token:
        meta["sonder_run"] = token
    return {"1": {"class_type": "SonderEditor", "_meta": meta,
                  "inputs": {"project": "proj", "scene_id": "s1", "selection_start": start, "selection_end": end,
                             "render_queue_active": queue}},
            "2": {"class_type": "SonderSaveVideo", "_meta": {"title": "Save it"}, "inputs": {"project": ["1", 0]}},
            "5": {"class_type": "KSampler", "_meta": {"title": "Sampler"}, "inputs": {}}}


def bind(prompt_id="p1", prompt=None, register_first=True):
    prompt = prompt or editor_prompt()
    if register_first:
        assert sdk_runs.register_receipt(RECEIPT, OWNER)[0] == "ok"
    assert sdk_runs.bind_queued(prompt_id, prompt) is not None
    run_feedback.on_bound(prompt_id, prompt)
    return prompt


def start(prompt_id, prompt, key="key-1"):
    sdk_runs.open_release(key, prompt_id, prompt)
    run_feedback.on_started(prompt_id)


def records(sent, prompt_id="p1"):
    return [event["record"] for _alias, event in sent if event["prompt_id"] == prompt_id]


def progress(prompt_id, node, value, maximum):
    sdk_progress.observe("progress", {"prompt_id": prompt_id, "node": node, "value": value, "max": maximum})


# ── sdk_progress watchers ───────────────────────────────────────────────────────

def test_several_watchers_share_a_prompt_and_each_finds_its_own_token():
    sdk_progress.watch("p", "run:p")
    sdk_progress.watch("p", "sdk-token")
    seen = []
    remove = sdk_progress.add_listener(seen.append)
    sdk_progress.observe("executing", {"prompt_id": "p", "node": "1"})
    remove()
    assert seen[-1]["run_tokens"] == ("run:p", "sdk-token") and seen[-1]["run_token"] == "run:p"
    sdk_progress.unwatch("p", "run:p")
    assert sdk_progress.watching("p") == ("sdk-token",)
    sdk_progress.unwatch("p", "sdk-token")
    assert sdk_progress.watching("p") == ()


def test_the_sdk_runner_still_finds_its_run_when_run_feedback_watched_first():
    """Audit #1: run feedback binds at put, before the runner's watch; the App's
    progress must keep arriving."""
    sdk_runs.register_token("tok", ["proj"], session_id="s", run_id="run-1", template_id="t")
    prompt = editor_prompt(receipt=None, token="tok")
    assert sdk_runs.bind_queued("p9", prompt) is not None
    run_feedback.on_bound("p9", prompt)
    sdk_progress.watch("p9", "tok")  # the runner, after its submit returned
    assert sdk_progress.watching("p9") == ("run:p9", "tok")
    run = types.SimpleNamespace(final=False, prompt_id="", state="queued", position=1, session_id="s")
    runner = types.SimpleNamespace(_runs_by_token={"tok": run}, _sessions={})
    sdk_progress.add_listener(lambda snap: sdk_runner.SdkRunner.apply_progress(runner, snap))
    sdk_progress.observe("executing", {"prompt_id": "p9", "node": "1"})
    progress("p9", "1", 3, 10)
    assert run.state == sdk_runner.RUNNING and run.prompt_id == "p9"


# ── Lifecycle ──────────────────────────────────────────────────────────────────────

def test_a_bound_run_is_announced_queued_with_its_requested_range(clean):
    bind()
    first = records(clean)[0]
    assert first["state"] == "queued" and first["host_ids"] == ["host-1"] and first["epoch"] == run_feedback.EPOCH
    assert first["receipt"]["requested"] == [{"editor": "1", "scene_id": "s1", "selection_start": 0, "selection_end": 48,
                                              "linked": False, "render_queue_active": False}]
    assert {alias for alias, _event in clean} == {"proj"}
    assert sdk_progress.watching("p1") == ("run:p1",)


def test_a_linked_editor_input_is_reported_as_set_by_the_graph(clean):
    prompt = editor_prompt()
    prompt["1"]["inputs"]["selection_end"] = ["9", 0]
    bind(prompt=prompt)
    requested = records(clean)[0]["receipt"]["requested"][0]
    assert requested["linked"] is True and requested["selection_end"] is None


def test_a_registration_after_the_run_ended_still_gets_the_terminal(clean):
    """Audit #7: the canvas registers after ComfyUI's reply; a fast run can finish
    before. The registration route then sends the full record, terminal included."""
    prompt = bind(register_first=False)
    assert records(clean) == []  # provisional: no host to tell yet
    start("p1", prompt)
    run_feedback.on_released("p1", "success", take_saved=True)
    assert records(clean) == []
    _code, prompt_id = sdk_runs.register_receipt(RECEIPT, OWNER)
    assert prompt_id == "p1"
    run_feedback.on_registered("p1")
    last = records(clean)[-1]
    assert last["state"] == "completed" and last["terminal"]["outcome"] == "success" and "receipt" in last


def settle():
    """Let a throttled update's trailing send fire."""
    time.sleep(run_feedback.PERIOD_SEC * 2 + 0.1)


def test_declared_stages_counts_and_the_saving_stage_shows_no_count(clean):
    prompt = bind()
    start("p1", prompt)
    sdk_progress.observe("executing", {"prompt_id": "p1", "node": "1"})
    run_feedback.note_stage(prompt, "1", "rendering")
    progress("p1", "1", 12, 48)
    settle()
    rendering = records(clean)[-1]
    assert rendering["state"] == "running" and rendering["stage"] == "rendering"
    assert rendering["count"] == {"value": 12, "max": 48} and rendering["node_title"] == ""
    run_feedback.note_stage(prompt, "1", "preparing_inputs")
    assert records(clean)[-1]["stage"] == "preparing_inputs" and records(clean)[-1]["count"] is None
    sdk_progress.observe("executing", {"prompt_id": "p1", "node": "5"})
    progress("p1", "5", 4, 20)
    settle()
    sampler = records(clean)[-1]
    assert sampler["stage"] == "" and sampler["node_title"] == "Sampler" and sampler["count"] == {"value": 4, "max": 20}
    sdk_progress.observe("executing", {"prompt_id": "p1", "node": "2"})
    run_feedback.note_stage(prompt, "2", "encoding")
    progress("p1", "2", 30, 48)
    settle()
    assert records(clean)[-1]["stage"] == "encoding" and records(clean)[-1]["count"] == {"value": 30, "max": 48}
    run_feedback.note_stage(prompt, "2", "saving")
    progress("p1", "2", 5, 9)  # the inline proxy's packet count
    settle()
    saving = records(clean)[-1]
    assert saving["stage"] == "saving" and saving["count"] is None


def test_counts_are_throttled_and_the_trailing_update_carries_the_latest(clean):
    """Audit #5: the trailing update is built when it fires, never frozen."""
    prompt = bind()
    start("p1", prompt)
    sdk_progress.observe("executing", {"prompt_id": "p1", "node": "1"})
    run_feedback.note_stage(prompt, "1", "rendering")
    before = len(records(clean))
    for value in range(1, 30):
        progress("p1", "1", value, 48)
    burst = records(clean)[before:]
    assert len(burst) <= 1
    time.sleep(run_feedback.PERIOD_SEC * 2 + 0.1)
    assert records(clean)[-1]["count"] == {"value": 29, "max": 48}
    seqs = [record["seq"] for record in records(clean)]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_a_trailing_update_never_follows_the_terminal(clean):
    prompt = bind()
    start("p1", prompt)
    sdk_progress.observe("executing", {"prompt_id": "p1", "node": "1"})
    run_feedback.note_stage(prompt, "1", "rendering")
    progress("p1", "1", 5, 48)  # inside the window: schedules a trailing update
    run_feedback.on_released("p1", "interrupted", note="The run was interrupted before this job completed.",
                             returned_job_ids=["job-1"])
    time.sleep(run_feedback.PERIOD_SEC * 2 + 0.1)
    last = records(clean)[-1]
    assert last["state"] == "cancelled" and last["terminal"]["returned_job_ids"] == ["job-1"]
    assert "detail" not in json.dumps(last)
    assert sdk_progress.watching("p1") == ()


def test_window_job_and_claimed_job_are_named_before_the_render(clean):
    prompt = bind(prompt=editor_prompt(queue=True))
    start("p1", prompt)
    job = types.SimpleNamespace(job_id="job-7", scene_name="Feedback", batch_id="b", batch_index=1, batch_total=3,
                                selection_start=48, selection_end=96)
    run_feedback.note_job(prompt, "1", job, "consume")
    run_feedback.note_window(prompt, "1", {"scene_id": "s1", "scene_name": "Feedback", "generation_start": 48,
                                           "generation_end": 96, "frame_count": 49, "frame_count_padding": 1,
                                           "queue": "consume", "ignored": 1}, ["proj", "proj-id"])
    receipt = records(clean)[-1]["receipt"]
    assert receipt["jobs"] == [{"job_id": "job-7", "scene_name": "Feedback", "batch_id": "b", "mode": "consume",
                                "batch_index": 1, "batch_total": 3, "selection_start": 48, "selection_end": 96,
                                "editor": "1"}]
    assert receipt["windows"][0]["generation_end"] == 96 and "ignored" not in receipt["windows"][0]
    assert {alias for alias, _event in clean[-1:]} <= {"proj", "proj-id"}
    # Receipt fields travel only when they change.
    sdk_progress.observe("executing", {"prompt_id": "p1", "node": "1"})
    settle()
    assert "receipt" not in records(clean)[-1]


def test_an_unobserved_prompt_ignores_notes():
    prompt = editor_prompt()
    run_feedback.note_stage(prompt, "1", "rendering")
    run_feedback.note_window(prompt, "1", {"generation_start": 0})
    assert run_feedback.records_for([OWNER]) == []


def test_runs_deleted_from_the_queue_end_at_once(clean):
    bind()
    run_outputs.end_removed({"p1"})
    last = records(clean)[-1]
    assert last["state"] == "removed" and last["terminal"]["outcome"] == "removed"
    assert sdk_progress.watching("p1") == ()


def test_the_reap_ends_a_run_that_left_the_queue_unseen(clean, monkeypatch):
    bind()
    monkeypatch.setattr(sdk_runs, "REMOVED_GRACE_SEC", -1.0)  # Windows' clock ticks coarsely
    run_outputs.reap_removed(set())
    assert records(clean)[-1]["state"] == "removed"


def _package_modules(tmp_path, monkeypatch):
    """io_nodes and the server modules it imports (the test package's own copies)."""
    import importlib
    from test_editor_node import TEST_PACKAGE, _import_io_nodes
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    rf = importlib.import_module(f"{TEST_PACKAGE}.server.run_feedback")
    runs = importlib.import_module(f"{TEST_PACKAGE}.server.sdk_runs")
    progress_mod = importlib.import_module(f"{TEST_PACKAGE}.server.sdk_progress")
    for module in (runs, progress_mod, rf):
        module.reset_for_tests()
    sent = []
    rf._HOOKS["emit"] = lambda alias, event: sent.append((alias, json.loads(json.dumps(event))))
    monkeypatch.setattr(rf, "REAP_INTERVAL_SEC", 3600.0)
    return io_nodes, rf, runs, sent


def test_a_queue_deletion_hook_ends_only_what_left(tmp_path, monkeypatch):
    io_nodes, rf, runs, sent = _package_modules(tmp_path, monkeypatch)
    queue = types.SimpleNamespace(items=[(0, "p1"), (1, "p2")])
    queue.get_current_queue_volatile = lambda: ([], list(queue.items))
    queue.delete_queue_item = lambda match: (queue.items.remove(next(i for i in queue.items if match(i))) or True)
    io_nodes._wrap_queue_removal(queue, "delete_queue_item")
    for prompt_id, receipt in (("p1", RECEIPT), ("p2", "21111111-2222-4333-8444-555555555555")):
        runs.register_receipt(receipt, OWNER)
        io_nodes._bind_queued_item((0, prompt_id, editor_prompt(receipt=receipt), {}, []))
    queue.delete_queue_item(lambda item: item[1] == "p2")
    assert records(sent, "p2")[-1]["state"] == "removed"
    assert records(sent, "p1")[-1]["state"] == "queued"
    rf.reset_for_tests()


def test_the_release_ends_the_notification_before_run_finished(tmp_path, monkeypatch):
    """Even a canvas run that failed before its Editor executed (no projects on
    its release record) ends: the terminal reaches the bound run's projects."""
    io_nodes, rf, runs, sent = _package_modules(tmp_path, monkeypatch)
    finished = []
    monkeypatch.setattr(io_nodes, "schedule_project_event", lambda alias, event: finished.append(alias))
    runs.register_receipt(RECEIPT, OWNER)
    prompt = editor_prompt()
    io_nodes._bind_queued_item((0, "p1", prompt, {}, []))
    runs.open_release("k1", "p1", prompt)
    rf.on_started("p1")
    runs.note_claimed_job("k1", "job-3")
    order = []
    rf._HOOKS["emit"] = lambda alias, event: (sent.append((alias, json.loads(json.dumps(event)))), order.append(event["type"]))
    monkeypatch.setattr(io_nodes, "schedule_project_event", lambda alias, event: order.append(event["type"]))
    runs.note_project("k1", "C:/proj", ["proj"])
    io_nodes._release_prompt_run("k1", types.SimpleNamespace(status_str="error", messages=[
        ("execution_error", {"node_type": "CheckpointLoaderSimple", "exception_message": "C:/secret/path"})]))
    last = records(sent)[-1]
    assert last["state"] == "failed" and last["terminal"]["outcome"] == "error"
    assert last["terminal"]["node_type"] == "CheckpointLoaderSimple"
    assert last["terminal"]["note"] == ""  # no job was returned: nothing job-shaped to say
    assert last["terminal"]["claimed_job_ids"] == ["job-3"]
    assert "secret" not in json.dumps(sent)
    assert {alias for alias, _event in sent} == {"proj"}
    terminal_at = max(i for i, kind in enumerate(order) if kind == "run_progress")
    assert order.index("run_finished") > terminal_at
    rf.reset_for_tests()


def test_a_canvas_run_that_failed_before_its_editor_still_ends(tmp_path, monkeypatch):
    io_nodes, rf, runs, sent = _package_modules(tmp_path, monkeypatch)
    monkeypatch.setattr(io_nodes, "schedule_project_event", lambda alias, event: None)
    runs.register_receipt(RECEIPT, OWNER)
    prompt = editor_prompt()
    io_nodes._bind_queued_item((0, "p1", prompt, {}, []))
    runs.open_release("k1", "p1", prompt)  # the Editor never executes: no projects noted
    io_nodes._release_prompt_run("k1", types.SimpleNamespace(status_str="error", messages=[
        ("execution_error", {"node_type": "CheckpointLoaderSimple"})]))
    assert records(sent)[-1]["state"] == "failed" and {alias for alias, _e in sent} == {"proj"}
    rf.reset_for_tests()


def _fake_queue():
    queue = types.SimpleNamespace(items=[])

    def delete(match):
        for item in list(queue.items):
            if match(item):
                queue.items.remove(item)
                return True
        return False

    queue.get_current_queue_volatile = lambda: ([], list(queue.items))
    queue.delete_queue_item = delete
    queue.wipe_queue = lambda: queue.items.clear()
    queue.interrupt_if_running = lambda prompt_id: False
    return queue


def test_a_wipe_and_a_dequeuing_cancel_end_their_runs(tmp_path, monkeypatch):
    import importlib
    from test_editor_node import TEST_PACKAGE
    io_nodes, rf, runs, sent = _package_modules(tmp_path, monkeypatch)
    control = importlib.import_module(f"{TEST_PACKAGE}.server.run_control")
    queue = _fake_queue()
    for name in ("delete_queue_item", "wipe_queue"):
        io_nodes._wrap_queue_removal(queue, name)
    receipts = {"p1": RECEIPT, "p2": "21111111-2222-4333-8444-555555555555", "p3": "31111111-2222-4333-8444-555555555555"}
    for prompt_id, receipt in receipts.items():
        runs.register_receipt(receipt, OWNER)
        io_nodes._bind_queued_item((0, prompt_id, editor_prompt(receipt=receipt), {}, []))
        queue.items.append((0, prompt_id))
    assert control.cancel_prompt("p1", queue) == control.CANCEL_DEQUEUED
    assert records(sent, "p1")[-1]["state"] == "removed"
    queue.wipe_queue()
    assert records(sent, "p2")[-1]["state"] == "removed" and records(sent, "p3")[-1]["state"] == "removed"
    rf.reset_for_tests()


def test_deleting_the_last_queued_item_ends_it_through_the_nested_wipe(tmp_path, monkeypatch):
    """ComfyUI 0.42's ``delete_queue_item`` wipes the queue when one item is left,
    through the (wrapped) instance attribute: the run ends once, as removed."""
    io_nodes, rf, runs, sent = _package_modules(tmp_path, monkeypatch)
    queue = _fake_queue()

    def delete(match):
        for index, item in enumerate(list(queue.items)):
            if match(item):
                if len(queue.items) == 1:
                    queue.wipe_queue()
                else:
                    queue.items.pop(index)
                return True
        return False

    queue.delete_queue_item = delete
    for name in ("delete_queue_item", "wipe_queue"):
        io_nodes._wrap_queue_removal(queue, name)
    runs.register_receipt(RECEIPT, OWNER)
    io_nodes._bind_queued_item((0, "p1", editor_prompt(), {}, []))
    queue.items.append((0, "p1"))
    assert queue.delete_queue_item(lambda item: item[1] == "p1") is True
    ended = [record for record in records(sent) if record["state"] == "removed"]
    assert len(ended) == 1 and ended[0]["terminal"]["outcome"] == "removed"
    rf.reset_for_tests()


def test_the_apps_progress_survives_a_bind_through_the_put_hook(tmp_path, monkeypatch):
    """Audit 3A #13: the put hook's own path (bind, run feedback's watch, its
    reconcile) before the runner's watch still leaves the App its progress."""
    import importlib
    from test_editor_node import TEST_PACKAGE
    io_nodes, rf, runs, _sent = _package_modules(tmp_path, monkeypatch)
    progress_mod = importlib.import_module(f"{TEST_PACKAGE}.server.sdk_progress")
    runner_mod = importlib.import_module(f"{TEST_PACKAGE}.server.sdk_runner")
    rf.install()
    runs.register_token("tok", ["proj"], session_id="s", run_id="run-1", template_id="t")
    io_nodes._bind_queued_item((0, "p9", editor_prompt(receipt=None, token="tok"), {}, []))
    progress_mod.watch("p9", "tok")  # the runner, after its submit returned
    assert progress_mod.watching("p9") == ("run:p9", "tok")
    run = types.SimpleNamespace(final=False, prompt_id="", state="queued", position=1, session_id="s")
    runner = types.SimpleNamespace(_runs_by_token={"tok": run}, _sessions={})
    remove = progress_mod.add_listener(lambda snap: runner_mod.SdkRunner.apply_progress(runner, snap))
    progress_mod.observe("executing", {"prompt_id": "p9", "node": "1"})
    progress_mod.observe("progress", {"prompt_id": "p9", "node": "1", "value": 3, "max": 10})
    remove()
    assert run.state == runner_mod.RUNNING and run.prompt_id == "p9"
    rf.reset_for_tests()
    progress_mod.reset_for_tests()


def test_binding_past_the_unfinished_cap_ends_the_oldest_queued_run_at_once(tmp_path, monkeypatch):
    io_nodes, rf, runs, sent = _package_modules(tmp_path, monkeypatch)
    monkeypatch.setattr(runs, "MAX_UNFINISHED_BOUND", 1)
    for prompt_id, receipt in (("p1", RECEIPT), ("p2", "21111111-2222-4333-8444-555555555555")):
        runs.register_receipt(receipt, OWNER)
        io_nodes._bind_queued_item((0, prompt_id, editor_prompt(receipt=receipt), {}, []))
    assert records(sent, "p1")[-1]["state"] == "removed" and records(sent, "p2")[-1]["state"] == "queued"
    rf.reset_for_tests()


def test_a_successful_release_carries_no_returned_job_note(tmp_path, monkeypatch):
    """`_run_outcome` words a note for a job left running; a success that left none
    has nothing to say in its receipt (found live on 8893, 2026-10-26)."""
    io_nodes, rf, runs, sent = _package_modules(tmp_path, monkeypatch)
    monkeypatch.setattr(io_nodes, "schedule_project_event", lambda alias, event: None)
    runs.register_receipt(RECEIPT, OWNER)
    prompt = editor_prompt()
    io_nodes._bind_queued_item((0, "p1", prompt, {}, []))
    runs.open_release("k1", "p1", prompt)
    io_nodes._release_prompt_run("k1", types.SimpleNamespace(status_str="success", messages=[]))
    terminal = records(sent)[-1]["terminal"]
    assert records(sent)[-1]["state"] == "completed" and terminal["note"] == ""
    rf.reset_for_tests()


def test_records_for_reads_the_owners_runs_with_full_receipts(clean):
    bind()
    found = run_feedback.records_for([OWNER])
    assert [record["prompt_id"] for record in found] == ["p1"] and "receipt" in found[0]
    assert run_feedback.records_for([("host", "proj", "someone-else")]) == []


def test_pending_modes_attach_when_the_prompt_binds(clean):
    rows = [{"id": "task", "name": "Task", "status": "selected", "choice": "Alpha", "matches": ["Alpha"]}]
    run_feedback.note_modes(rows, key=RECEIPT)
    bind()
    assert records(clean)[0]["receipt"]["modes"] == rows


def test_execution_end_reads_finishing(clean):
    prompt = bind()
    start("p1", prompt)
    run_feedback.observe("execution_success", {"prompt_id": "p1"})
    assert records(clean)[-1]["state"] == "finishing"


# ── Modes ──────────────────────────────────────────────────────────────────────────

def _modes_prompt():
    presets = {"a": {"name": "Alpha", "description": "", "order": 0, "members": {"steps": {"value": "20"}}},
               "b": {"name": "Beta", "description": "", "order": 1, "members": {"steps": {"value": "30"}}}}
    manifest = {"v": 1, "bindings": {"steps": _binding("6", "steps", label="Steps")}, "notes": {},
                "presets": presets,
                "preset_categories": {"task": {"name": "Task", "description": "", "order": 0, "choices": ["a", "b"]}}}
    # The KSampler is muted: the export leaves it out; the canvas sends its body.
    return {"1": _node("SonderEditor"), "9": _node("SonderControlRelay", {"manifest": json.dumps(manifest)})}


@pytest.fixture
def classes(monkeypatch):
    original = cd.describe_bindings
    monkeypatch.setattr(cd, "describe_bindings",
                        lambda p, relay_census=None, **kw: original(p, relay_census=relay_census, class_lookup=CLASSES.get, **kw))


def test_canvas_modes_read_a_muted_target_as_the_panel_does(classes):
    """Muted is participation, never absence: Alpha matches through the body."""
    from server import routes
    inactive = {"6": {"class_type": "KSampler", "inputs": {"steps": 20, "seed": 5}, "_meta": {"title": "Pass 1"},
                      "mode": "muted", "by": "self", "label": "Pass 1"}}
    controls = {"prompt": _modes_prompt(), "inactive": inactive, "editor_key": "1"}
    rows = run_feedback.capture_canvas_modes(controls)
    assert rows == [{"id": "task", "name": "Task", "status": "selected", "choice": "Alpha", "matches": ["Alpha"]}]
    panel = routes._describe_controls_body(json.dumps(controls).encode())
    from server import control_preset_categories as pc
    assert pc.modes_receipt(panel) == rows
    # Without the authoring snapshot the setting reads missing: no choice can match.
    assert run_feedback.capture_canvas_modes({"prompt": _modes_prompt(), "editor_key": "1"})[0]["status"] != "selected"


def test_modes_capture_is_empty_without_categories_and_none_when_unreadable(classes):
    prompt = {"1": _node("SonderEditor")}
    assert run_feedback.capture_canvas_modes({"prompt": prompt}) == []
    assert run_feedback.capture_canvas_modes({"prompt": "nope"}) is None
    assert run_feedback.capture_canvas_modes({"prompt": _modes_prompt(), "inactive": {"9": {}}}) is None


def test_a_started_run_is_never_reported_removed_before_its_release(clean, monkeypatch):
    """Audit 3A #1: an evicted or reaped bound run that ComfyUI already took is
    ended by its release, never as removed."""
    prompt = bind()
    start("p1", prompt)
    real = sdk_runs.bound_run
    monkeypatch.setattr(sdk_runs, "bound_run", lambda pid: dict(real(pid), finished=True, outcome=sdk_runs.OUTCOME_REMOVED))
    run_feedback.reconcile()
    assert records(clean)[-1]["state"] == "running"


def test_without_native_progress_the_stages_still_read(clean):
    """Degraded mode: no send_sync wrap, so no counts; the declared stages and
    the queue's own start and end still drive the record."""
    prompt = bind()
    start("p1", prompt)
    run_feedback.note_stage(prompt, "1", "rendering")
    assert records(clean)[-1]["stage"] == "rendering" and records(clean)[-1]["count"] is None
    run_feedback.on_task_done("p1")
    assert records(clean)[-1]["state"] == "finishing"


def test_modes_are_captured_once_and_a_read_never_takes_the_receipt_flag(clean):
    rows = [{"id": "a", "name": "A", "status": "custom", "choice": "", "matches": []}]
    prompt = bind()
    run_feedback.note_modes(rows, prompt_id="p1")
    run_feedback.note_modes([{"id": "b"}], prompt_id="p1")
    assert records(clean)[-1]["receipt"]["modes"] == rows
    start("p1", prompt)
    with run_feedback._LOCK:
        run_feedback._DATA["p1"]["receipt_rev"] += 1
    run_feedback.records_for([OWNER])
    run_feedback._publish("p1", force=True)
    assert "receipt" in records(clean)[-1]
