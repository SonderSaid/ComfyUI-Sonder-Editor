"""SDK sessions, Generate ×N, outcome authority and Cancel (server/sdk_runner.py)."""

import asyncio
import json
import os
import sys
import threading
import types

import pytest

from server import sdk_runner, sdk_runs, sdk_templates
from server.sdk_proxy import ProxyTarget
from server.sdk_runner import (
    CANCELED, ENDED, FAILED, FINISHING, LANDED, QUEUED, RUNNING, UNCONFIRMED, SdkError, SdkRunner,
)

PROJECT = "Testground"
ALIASES = ["canon-id", "Testground"]


@pytest.fixture(autouse=True)
def user_dir(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(get_user_directory=lambda: str(tmp_path)))
    sdk_runs.reset_for_tests()
    yield tmp_path
    sdk_runs.reset_for_tests()


def template_prompt():
    return {
        "1": {"class_type": "SonderEditor", "inputs": {"project": "Other", "project_name": "x", "fps": 24.0,
                                                        "width": 64, "height": 64, "scene_id": "old-scene"}},
        "2": {"class_type": "SonderSaveVideo", "inputs": {"project": ["1", 0], "mark_queue_complete": False}},
        "5": {"class_type": "SonderControl", "inputs": {"seed": 10, "kind": "SEED", "value_text": "", "control_id": "abcd1234"}},
        "6": {"class_type": "KSampler", "inputs": {"seed": ["5", 0]}},
    }


def seed_control(**changes):
    control = {"control_id": "abcd1234", "label": "Seed", "kind": "SEED", "status": "ok", "error": None,
               "keys": ["5"], "seed": 10, "min": 0, "max": 100, "step": 1}
    control.update(changes)
    return control


class FakeJob:
    def __init__(self, job_id, events=None):
        self.id = job_id
        self.status = "queued"
        self.canceled = False
        self._events = events

    async def cancel(self):
        self.canceled = True
        return self

    async def events(self):
        if self._events is None:
            return
        for event in self._events:
            if isinstance(event, Exception):
                raise event
            yield event


class Refused(Exception):
    """Like comfy-sdk's errors: carries the proxy's HTTP status."""

    def __init__(self, message, http_status):
        super().__init__(message)
        self.http_status = http_status


class FakeClient:
    def __init__(self):
        self.workflows = types.SimpleNamespace(from_json=lambda graph: graph)
        self.jobs = types.SimpleNamespace(get=self._get_job)
        self.submitted = []
        self.fail_on = {}
        self.on_submit = None
        self.jobs_fetched = []

    async def _get_job(self, job_id):
        job = FakeJob(job_id)
        self.jobs_fetched.append(job)
        return job

    async def submit(self, graph):
        index = len(self.submitted) + 1
        if index in self.fail_on:
            self.submitted.append(json.loads(json.dumps(graph)))
            raise self.fail_on[index]
        self.submitted.append(json.loads(json.dumps(graph)))
        if self.on_submit:
            result = self.on_submit(graph)
            if asyncio.iscoroutine(result):
                await result
        return FakeJob(f"prompt-{index}")


class FakeQueue:
    def __init__(self):
        self.running, self.queued, self.history = [], [], set()

    def get_current_queue_volatile(self):
        return list(self.running), list(self.queued)

    def get_history(self, prompt_id=None, map_function=None):
        assert map_function is not None  # never deep-copy history entries
        return {prompt_id: True} if prompt_id in self.history else {}


class World:
    def __init__(self):
        self.client = FakeClient()
        self.queue = FakeQueue()
        self.events = []
        self.owners = {}
        self.now = [1000.0]
        self.controls = [seed_control()]
        self.fields = []
        self.runner = SdkRunner(
            resolve_project=self.resolve,
            client_factory=lambda url, token: self.client,
            ensure_proxy=self.ensure_proxy,
            queue_provider=lambda: self.queue,
            emit=lambda alias, event: self.events.append((alias, event)),
            get_owner=self.get_owner,
            sdk_ready=lambda: True,
            describe=lambda prompt: {"controls": [dict(c) for c in self.controls], "fields": list(self.fields)},
            clock=lambda: self.now[0],
        )
        validation = {"ok": True, "errors": [], "blocking": [], "warnings": []}
        self.template_id = sdk_templates.import_template(
            json.dumps(template_prompt()).encode(), source_name="i2va.json",
            validate=lambda prompt: validation,
        )["template_id"]

    async def resolve(self, project):
        return (f"C:/projects/{project}", list(ALIASES)) if project == PROJECT else ("", [])

    async def ensure_proxy(self):
        return ProxyTarget(url="http://127.0.0.1:8189", token="", managed=True)

    async def get_owner(self, project, host_id=""):
        return self.owners.get(host_id)

    async def open(self):
        result = await self.runner.open_session(self.template_id, PROJECT)
        session = self.runner.session_for(result["session_id"], result["secret"])
        self.owners[session.session_id] = {"session_id": "tab-1", "status": "active"}
        return session


@pytest.fixture
def world():
    world = World()
    yield world
    world.runner.close()


def editor_values():
    return {"scene_id": "scene-a", "selection_start": 40, "selection_end": 79, "pre_context_frames": 17,
            "render_queue_active": True}


# ── Generate ─────────────────────────────────────────────────────────────────

def test_generate_n_submits_n_runs_with_tokens_values_and_advancing_seeds(world):
    sdk_templates.update_control_values(world.template_id, lambda d: d["controls"].update(
        {"abcd1234": {"seed_mode": "increment"}}))

    async def go():
        session = await world.open()
        return session, await world.runner.generate(session, tab_session_id="tab-1", count=3, editor_values=editor_values(), editor_values_seq=1,
                                                    render_cache_max_bytes=-1)

    session, result = asyncio.run(go())
    assert len(result["run_ids"]) == 3 and result["error"] is None
    assert result["seeds_used"] == [{"abcd1234": "10"}, {"abcd1234": "11"}, {"abcd1234": "12"}]
    tokens = set()
    for graph, seed in zip(world.client.submitted, (10, 11, 12)):
        editor = graph["1"]["inputs"]
        assert editor["project"] == PROJECT and editor["render_cache_max_bytes"] == -1
        assert editor["scene_id"] == "scene-a" and editor["selection_start"] == 40
        assert graph["5"]["inputs"]["seed"] == seed
        token = graph["1"]["_meta"]["sonder_run"]
        assert sdk_runs.token_state(token)["project_ids"] == sorted(ALIASES)
        tokens.add(token)
    assert len(tokens) == 3
    assert sdk_templates.read_control_values(world.template_id)["controls"]["abcd1234"]["seed"] == 13
    assert sdk_templates.read_editor_state(PROJECT)["pre_context_frames"] == 17
    assert [run.state for run in session.runs] == [QUEUED] * 3
    assert {alias for alias, _ in world.events} == set(ALIASES)


def test_seeds_default_to_randomize_and_fixed_never_changes(world, monkeypatch):
    modes = []
    real = sdk_runner.control_descriptors.next_seed_value
    monkeypatch.setattr(sdk_runner.control_descriptors, "next_seed_value",
                        lambda mode, value, **kw: modes.append(mode) or real(mode, value, **kw))

    async def go():
        session = await world.open()
        await world.runner.generate(session, tab_session_id="tab-1", count=2)
        sdk_templates.update_control_values(world.template_id, lambda d: d["controls"]["abcd1234"].update(seed_mode="fixed"))
        await world.runner.generate(session, tab_session_id="tab-1", count=2)

    asyncio.run(go())
    assert modes == ["randomize", "randomize"]
    fixed_seeds = [graph["5"]["inputs"]["seed"] for graph in world.client.submitted[2:]]
    assert fixed_seeds[0] == fixed_seeds[1]


@pytest.mark.parametrize("status", ["stale_kind", "duplicate_id", "unsupported", "mixed_targets"])
def test_generate_refuses_a_control_that_is_not_ok(world, status):
    world.controls = [seed_control(status=status)]

    async def go():
        session = await world.open()
        with pytest.raises(SdkError) as exc:
            await world.runner.generate(session, tab_session_id="tab-1")
        return exc.value

    error = asyncio.run(go())
    assert error.code == "generate_refused" and error.details["problems"]
    assert world.client.submitted == []


def test_an_unlinked_control_does_not_block_generate(world):
    world.controls = [seed_control(status="unlinked", kind="INT", control_id="ffff0000")]

    async def go():
        session = await world.open()
        return await world.runner.generate(session, tab_session_id="tab-1")

    assert len(asyncio.run(go())["run_ids"]) == 1


def test_a_refused_submit_stops_the_batch_and_forgets_its_token(world):
    world.client.fail_on = {2: Refused("invalid workflow", 422)}

    async def go():
        session = await world.open()
        return session, await world.runner.generate(session, tab_session_id="tab-1", count=3)

    session, result = asyncio.run(go())
    assert len(result["run_ids"]) == 1 and result["error"]["code"] == "submit_failed"
    assert len(session.runs) == 1
    assert len(world.runner._runs_by_token) == 1
    assert len(sdk_runs._TOKENS) == 1  # only the accepted run's token remains


# ── Outcome authority ────────────────────────────────────────────────────────

def test_run_finished_inside_submit_is_not_lost(world):
    # ComfyUI can fail a prompt before the proxy's reply gets back.
    def finish_early(graph):
        world.runner.apply_run_finished({
            "run_token": graph["1"]["_meta"]["sonder_run"], "prompt_id": "",
            "outcome": "error", "note": "The run failed in KSampler before this job completed.",
            "leased_job_ids": [], "returned_job_ids": [],
        })

    world.client.on_submit = finish_early

    async def go():
        session = await world.open()
        await world.runner.generate(session, tab_session_id="tab-1")
        return session

    session = asyncio.run(go())
    run = session.runs[0]
    assert run.state == FAILED and "KSampler" in run.note and run.prompt_id == "prompt-1"
    assert sdk_runs._TOKENS == {}


def test_the_listener_hands_off_from_the_worker_thread(world):
    async def go():
        session = await world.open()
        await world.runner.generate(session, tab_session_id="tab-1")
        run = session.runs[0]
        payload = {"run_token": run.token, "prompt_id": run.prompt_id, "outcome": "success",
                   "leased_job_ids": ["job-1"], "returned_job_ids": []}
        worker = threading.Thread(target=sdk_runs.notify_run_finished, args=(payload,))
        worker.start()
        worker.join()
        assert run.state == QUEUED  # nothing touched off the loop
        for _ in range(5):
            await asyncio.sleep(0)
        return run

    run = asyncio.run(go())
    assert run.state == LANDED and run.job_id == "job-1"


def test_a_consumed_job_is_reported_from_the_claim_not_the_settled_lease(world):
    async def go():
        session = await world.open()
        await world.runner.generate(session, tab_session_id="tab-1")
        run = session.runs[0]
        world.runner.apply_run_finished({"run_token": run.token, "prompt_id": run.prompt_id, "outcome": "success",
                                         "leased_job_ids": [], "claimed_job_id": "job-7", "returned_job_ids": []})
        return run

    run = asyncio.run(go())
    assert run.state == LANDED and run.job_id == "job-7"


def test_presence_tracks_queue_running_and_finishing_and_ignores_sdk_status(world):
    async def go():
        session = await world.open()
        await world.runner.generate(session, tab_session_id="tab-1", count=2)
        first, second = session.runs
        first.job.status = "expired"  # the proxy's completion race; the runner never reads SDK status
        world.queue.running = [(1, first.prompt_id, {}, {}, [])]
        world.queue.queued = [(2, second.prompt_id, {}, {}, [])]
        await world.runner.presence_tick()
        assert (first.state, second.state, second.position) == (RUNNING, QUEUED, 1)

        # First leaves the queue for history and its release record is open:
        # finishing, even after ComfyUI's history is wiped.
        world.queue.running = []
        sdk_runs.open_release("k1", first.prompt_id)
        world.now[0] += 30
        await world.runner.presence_tick()
        assert first.state == FINISHING
        world.runner.apply_run_finished({"run_token": first.token, "prompt_id": first.prompt_id,
                                         "outcome": "success", "leased_job_ids": [], "returned_job_ids": []})
        assert first.state == LANDED

    asyncio.run(go())


def test_absent_and_never_taken_means_canceled_after_the_grace(world):
    async def go():
        session = await world.open()
        await world.runner.generate(session, tab_session_id="tab-1")
        run = session.runs[0]
        await world.runner.presence_tick()
        assert run.state == QUEUED  # just absent: grace period
        world.now[0] += sdk_runner.ABSENT_GRACE_SEC + 1
        await world.runner.presence_tick()
        assert run.state == CANCELED and sdk_runs.token_state(run.token) is None
        # A late run_finished still wins over the inference.
        world.runner.apply_run_finished({"run_token": run.token, "prompt_id": run.prompt_id,
                                         "outcome": "success", "leased_job_ids": [], "returned_job_ids": []})
        assert run.state == LANDED

    asyncio.run(go())


def test_cancel_all_requests_cancel_and_an_interrupt_reads_as_canceled(world):
    async def go():
        session = await world.open()
        await world.runner.generate(session, tab_session_id="tab-1", count=2)
        result = await world.runner.cancel(session)
        assert len(result["canceling"]) == 2
        assert all(run.job.canceled and run.cancel_requested for run in session.runs)
        run = session.runs[0]
        world.runner.apply_run_finished({"run_token": run.token, "prompt_id": run.prompt_id,
                                         "outcome": "interrupted", "leased_job_ids": ["job-9"],
                                         "returned_job_ids": ["job-9"], "note": "The run was interrupted."})
        assert run.state == CANCELED and run.job_id == "job-9"
        assert session.runs[1].public()["canceling"] is True

    asyncio.run(go())


def test_missing_progress_observer_keeps_state_only_status(world):
    async def go():
        session = await world.open()
        await world.runner.generate(session, tab_session_id="tab-1")
        run = session.runs[0]
        run.job._events = [RuntimeError("429 too_many_streams")]
        world.queue.running = [(1, run.prompt_id, {}, {}, [])]
        await world.runner.presence_tick()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert run.state == RUNNING and run.progress_count is None
        assert run.progress_node == ""

    asyncio.run(go())


def test_a_submit_with_an_unknown_outcome_waits_and_is_adopted_from_the_queue(world):
    # The proxy's 503 upstream_unreachable: ComfyUI may already hold the prompt.
    world.client.fail_on = {1: Refused("Could not reach ComfyUI", 503)}

    async def go():
        session = await world.open()
        result = await world.runner.generate(session, tab_session_id="tab-1", count=2)
        run = session.runs[0]
        assert result["error"]["code"] == "submit_unconfirmed" and len(session.runs) == 1
        assert run.state == UNCONFIRMED and sdk_runs.token_state(run.token) is not None
        # Its token is found in ComfyUI's queue: tracked like any other run.
        graph = world.client.submitted[0]
        world.queue.queued = [(7, "prompt-late", graph, {}, [])]
        await world.runner.presence_tick()
        return run

    run = asyncio.run(go())
    assert (run.state, run.prompt_id, run.position) == (QUEUED, "prompt-late", 1)


def test_an_unconfirmed_submit_that_never_appears_fails_after_a_grace(world):
    world.client.fail_on = {1: asyncio.TimeoutError()}

    async def go():
        session = await world.open()
        await world.runner.generate(session, tab_session_id="tab-1")
        run = session.runs[0]
        await world.runner.presence_tick()
        assert run.state == UNCONFIRMED
        world.now[0] += sdk_runner.UNCONFIRMED_SUBMIT_GRACE_SEC + 1
        await world.runner.presence_tick()
        return run

    run = asyncio.run(go())
    assert run.state == FAILED and sdk_runs.token_state(run.token) is None


def test_a_seed_save_failure_does_not_lose_the_runs(world, monkeypatch):
    def locked(*args, **kwargs):
        raise PermissionError("antivirus holds the file")

    monkeypatch.setattr(sdk_templates, "update_control_values", locked)

    async def go():
        session = await world.open()
        world.events.clear()
        result = await world.runner.generate(session, tab_session_id="tab-1", count=2)
        return session, result

    session, result = asyncio.run(go())
    assert len(result["run_ids"]) == 2 and [run.state for run in session.runs] == [QUEUED, QUEUED]
    assert world.events  # the tab was told


def test_cancel_all_during_a_batch_stops_further_submits_and_cancels_the_accepted_one(world):
    async def go():
        session = await world.open()

        async def cancel_now(graph):
            if len(world.client.submitted) == 1:
                await world.runner.cancel(session)

        world.client.on_submit = cancel_now
        result = await world.runner.generate(session, tab_session_id="tab-1", count=4)
        return session, result

    session, result = asyncio.run(go())
    assert len(world.client.submitted) == 1 and len(result["run_ids"]) == 1
    run = session.runs[0]
    assert run.cancel_requested and run.job.canceled


def test_run_finished_from_another_prompt_with_the_same_token_is_ignored(world):
    async def go():
        session = await world.open()
        await world.runner.generate(session, tab_session_id="tab-1")
        run = session.runs[0]
        world.runner.apply_run_finished({"run_token": run.token, "prompt_id": run.prompt_id, "outcome": "success",
                                         "leased_job_ids": [], "returned_job_ids": []})
        world.runner.apply_run_finished({"run_token": run.token, "prompt_id": "a-requeued-copy",
                                         "outcome": "error", "note": "ended", "leased_job_ids": [],
                                         "returned_job_ids": []})
        return run

    assert asyncio.run(go()).state == LANDED


def test_success_that_returned_its_job_is_not_reported_as_a_landed_take(world):
    async def go():
        session = await world.open()
        await world.runner.generate(session, tab_session_id="tab-1")
        run = session.runs[0]
        world.runner.apply_run_finished({"run_token": run.token, "prompt_id": run.prompt_id, "outcome": "success",
                                         "note": "The run finished without completing this job.",
                                         "leased_job_ids": ["job-1"], "returned_job_ids": ["job-1"]})
        return run

    run = asyncio.run(go())
    assert run.state == ENDED and "without completing" in run.note


def test_a_released_run_whose_run_finished_never_comes_ends_after_a_while(world):
    async def go():
        session = await world.open()
        await world.runner.generate(session, tab_session_id="tab-1")
        run = session.runs[0]
        sdk_runs.open_release("k9", run.prompt_id)
        sdk_runs.close_release("k9")
        await world.runner.presence_tick()
        assert run.state == FINISHING
        world.now[0] += sdk_runner.CLOSED_WITHOUT_FINISH_SEC + 1
        await world.runner.presence_tick()
        return run

    assert asyncio.run(go()).state == ENDED


def test_generate_starts_from_editor_defaults_not_the_authors_position(world):
    async def go():
        session = await world.open()
        await world.runner.generate(session, tab_session_id="tab-1")  # no editor values, no stored position

    asyncio.run(go())
    editor = world.client.submitted[0]["1"]["inputs"]
    assert editor["scene_id"] == "" and editor["selection_start"] == 0


def test_stored_values_and_fields_are_applied_and_rechecked(world):
    describes = []
    world.fields = [{"source": "field", "key": "2", "class_type": "SonderSaveVideo", "field": "mark_queue_complete",
                     "kind": "BOOLEAN", "status": "ok"}]
    real = world.runner._describe
    world.runner._describe = lambda prompt: describes.append(json.loads(json.dumps(prompt))) or real(prompt)
    sdk_templates.update_control_values(world.template_id, lambda d: (
        d["controls"].update({"abcd1234": {"value_text": "ignored for a seed", "seed": 3}}),
        d["fields"].update({"SonderSaveVideo/mark_queue_complete": True}),
    ))

    async def go():
        session = await world.open()
        await world.runner.generate(session, tab_session_id="tab-1", render_cache_max_bytes=-5)  # out of range: Off

    asyncio.run(go())
    graph = world.client.submitted[0]
    assert graph["2"]["inputs"]["mark_queue_complete"] is True
    assert graph["5"]["inputs"]["value_text"] == "ignored for a seed" and graph["5"]["inputs"]["seed"] == 3
    assert graph["1"]["inputs"]["render_cache_max_bytes"] == 0
    assert len(describes) == 2 and describes[1]["5"]["inputs"]["value_text"] == "ignored for a seed"


def test_open_uses_the_folders_own_spelling_and_quotes_the_tab_url(world):
    async def resolve(project):
        return ("C:/projects/My #1 Project", ["canon", "My #1 Project"])

    world.runner._resolve_project = resolve

    async def go():
        return await world.runner.open_session(world.template_id, " my #1 project ")

    result = asyncio.run(go())
    assert result["project"] == "My #1 Project"
    assert result["tab_url"].startswith("/sonder-editor/tab/My%20%231%20Project?mode=sdk&session=")


# ── Sessions ─────────────────────────────────────────────────────────────────

def test_only_the_owner_tab_may_change_a_session(world):
    async def go():
        session = await world.open()
        secret = session.secret
        assert await world.runner.authorized(session.session_id, secret, "tab-1") is session
        with pytest.raises(SdkError) as exc:
            await world.runner.authorized(session.session_id, secret, "tab-2")
        assert exc.value.code == "not_owner"
        with pytest.raises(SdkError) as exc:
            await world.runner.authorized(session.session_id, "wrong", "tab-1")
        assert exc.value.code == "session_ended"
        # Reopening the same workflow and project reattaches.
        again = await world.runner.open_session(world.template_id, PROJECT)
        assert again["session_id"] == session.session_id

    asyncio.run(go())


def test_opening_records_the_launchers_last_workflow_even_on_reattach(world, user_dir, monkeypatch):
    from server import launcher_state

    ticks = iter([100.0, 200.0])
    original = launcher_state.record_open
    monkeypatch.setattr(launcher_state, "record_open",
                        lambda folder, template_id: original(folder, template_id, clock=lambda: next(ticks)))

    async def go():
        await world.open()
        recorded = launcher_state.read_state()["last_workflow"]
        assert recorded["by_project"][PROJECT] == {"template_id": world.template_id, "at": 100.0}
        assert recorded["any"]["template_id"] == world.template_id
        await world.runner.open_session(world.template_id, PROJECT)  # reattach
        assert launcher_state.read_state()["last_workflow"]["by_template"][world.template_id] == 200.0

    asyncio.run(go())


def test_an_open_refused_for_capacity_is_not_recorded(world, monkeypatch):
    from server import launcher_state

    recorded = []
    monkeypatch.setattr(launcher_state, "record_open", lambda *args: recorded.append(args))

    async def refuse():
        raise SdkError("Too many sessions.", "sdk_sessions_busy", 429)

    monkeypatch.setattr(sdk_runner, "MAX_SESSIONS", 0)
    monkeypatch.setattr(world.runner, "_evict_one", refuse)

    async def go():
        with pytest.raises(SdkError):
            await world.runner.open_session(world.template_id, PROJECT)

    asyncio.run(go())
    assert recorded == []


def test_a_failed_launcher_note_never_fails_an_open(world, monkeypatch):
    from server import launcher_state

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(launcher_state, "record_open", broken)

    async def go():
        session = await world.open()
        assert session.project == PROJECT

    asyncio.run(go())


def test_eviction_takes_an_idle_ownerless_session_and_never_a_busy_one(world, monkeypatch):
    monkeypatch.setattr(sdk_runner, "MAX_SESSIONS", 2)

    async def go():
        first = await world.open()
        world.runner._sessions.clear()  # isolate: build two sessions on different templates
        ids = []
        for _ in range(2):
            other = sdk_templates.import_template(json.dumps(template_prompt()).encode(),
                                                  validate=lambda p: {"ok": True, "errors": []})["template_id"]
            result = await world.runner.open_session(other, PROJECT)
            world.owners[result["session_id"]] = {"session_id": "t", "status": "active"}
            ids.append(result["session_id"])
        with pytest.raises(SdkError) as exc:
            await world.runner.open_session(world.template_id, PROJECT)
        assert exc.value.status == 429
        world.owners.pop(ids[0])  # its tab is gone
        result = await world.runner.open_session(world.template_id, PROJECT)
        assert ids[0] not in world.runner._sessions and result["session_id"] in world.runner._sessions

    asyncio.run(go())


def test_housekeeping_keeps_a_backgrounded_tabs_session_and_drops_an_abandoned_one(world, monkeypatch):
    stopped = []
    monkeypatch.setattr(sdk_runner.sdk_proxy, "stop_owned_proxy", lambda: stopped.append(True))

    async def go():
        session = await world.open()
        world.owners[session.session_id] = {"session_id": "tab-1", "status": "orphaned"}
        world.now[0] += sdk_runner.SESSION_IDLE_SEC + 1
        await world.runner.housekeeping()
        assert session.session_id in world.runner._sessions  # throttled, not gone
        world.owners.pop(session.session_id)
        await world.runner.housekeeping()
        assert session.session_id not in world.runner._sessions
        world.now[0] += sdk_runner.PROXY_IDLE_STOP_SEC + 1
        await world.runner.housekeeping()

    asyncio.run(go())
    assert stopped == [True]


def test_a_session_with_a_run_in_flight_is_never_evicted_even_without_a_tab(world, monkeypatch):
    monkeypatch.setattr(sdk_runner, "MAX_SESSIONS", 1)

    async def go():
        session = await world.open()
        await world.runner.generate(session, tab_session_id="tab-1")
        world.owners.pop(session.session_id)  # its tab is gone, its run is not
        other = sdk_templates.import_template(json.dumps(template_prompt()).encode(),
                                              validate=lambda p: {"ok": True, "errors": []})["template_id"]
        with pytest.raises(SdkError) as exc:
            await world.runner.open_session(other, PROJECT)
        return exc.value

    assert asyncio.run(go()).code == "sdk_sessions_busy"


def test_status_events_are_throttled_and_sequenced(world):
    async def go():
        session = await world.open()
        world.events.clear()
        world.runner._emit(session, force=True)
        world.runner._emit(session)
        world.runner._emit(session)
        immediate = len(world.events)
        await asyncio.sleep(sdk_runner.STATUS_MIN_INTERVAL_SEC + 0.05)
        return immediate, [event["seq"] for _, event in world.events]

    immediate, seqs = asyncio.run(go())
    assert immediate == len(ALIASES)  # one snapshot per alias, the rest deferred
    assert seqs == [1, 1, 2, 2]


# ── The SDK client ───────────────────────────────────────────────────────────

def test_the_client_is_built_without_touching_the_environment(monkeypatch):
    pytest.importorskip("comfy_sdk")
    monkeypatch.setenv("COMFY_BASE_URL", "https://cloud.comfy.org")
    monkeypatch.setenv("COMFY_API_KEY", "ambient-key")
    before = dict(os.environ)
    client = sdk_runner._make_client("http://127.0.0.1:8189", "")
    try:
        assert dict(os.environ) == before
        assert client._low.base_url.startswith("http://127.0.0.1:8189")
        assert client._low.authenticated is False  # the ambient key is not sent
        assert client._http._trust_env is False
    finally:
        asyncio.run(client.aclose())


def test_stale_generate_keeps_newer_position_but_runs_click_snapshot(world):
    async def go():
        session = await world.open()
        sdk_templates.write_editor_state(PROJECT, {"selection_start": 80}, writer={"tab": "tab-1", "seq": 2})
        result = await world.runner.generate(session, tab_session_id="tab-1", editor_values={"selection_start": 40}, editor_values_seq=1)
        assert result["stale"] is True
        assert sdk_templates.read_editor_state(PROJECT)["selection_start"] == 80
        assert world.client.submitted[0]["1"]["inputs"]["selection_start"] == 40
    asyncio.run(go())


def test_generate_rechecks_owner_after_template_lock(world):
    async def go():
        session = await world.open()
        async with world.runner.template_lock(session.template_id):
            pending = asyncio.create_task(world.runner.generate(session, tab_session_id="tab-1", editor_values={"selection_start": 40}, editor_values_seq=1))
            await asyncio.sleep(.03)
            world.owners[session.session_id] = {"session_id": "tab-2"}
        with pytest.raises(SdkError) as exc:
            await pending
        assert exc.value.code == "not_owner"
        assert not world.client.submitted
        assert not sdk_templates.read_editor_state(PROJECT)
    asyncio.run(go())


@pytest.mark.parametrize("node,display,expected", [
    ("12:5", "12", "Sampler"), ("12.0.1.inner", "9", "Bridge"),
    ("unknown", "12:5", "Sampler"), ("unknown", "other", "")])
def test_progress_resolves_labels(node, display, expected):
    assert SdkRunner._progress_label({"12:5": "Sampler", "12": "Bridge"}, node, display) == expected


def test_run_detail_is_memory_only_and_progress_ignores_late_events(world):
    async def go():
        session = await world.open()
        await world.runner.generate(session, tab_session_id="tab-1")
        run = session.runs[0]
        snapshot = {"run_token": run.token, "prompt_id": run.prompt_id, "node_id": "6", "display_node_id": "6", "count": {"value": 1, "max": 4}}
        world.runner.apply_progress(snapshot)
        assert run.public()["progress_node"] == "KSampler"
        assert run.public()["progress_count"] == {"value": 1, "max": 4}
        world.runner.apply_run_finished({"run_token": run.token, "prompt_id": run.prompt_id, "outcome": "error", "detail": "ValueError: private/path"})
        assert run.detail == "ValueError: private/path"
        assert "private/path" not in json.dumps(world.runner.status(session))
        assert "private/path" not in json.dumps(world.events)
        world.runner.apply_progress(snapshot)
        assert run.state == FAILED and run.progress_node == ""
    asyncio.run(go())


def test_cancel_while_generate_waits_saves_position_without_submitting(world):
    async def go():
        session = await world.open()
        async with world.runner.template_lock(session.template_id):
            pending = asyncio.create_task(world.runner.generate(session, tab_session_id="tab-1", editor_values={"selection_start": 40}, editor_values_seq=1))
            await asyncio.sleep(.03)
            await world.runner.cancel(session)
        with pytest.raises(SdkError) as exc:
            await pending
        assert exc.value.code == "canceled_before_submit"
        assert sdk_templates.read_editor_state(PROJECT)["selection_start"] == 40
        assert not world.client.submitted
    asyncio.run(go())


@pytest.mark.parametrize("count", [0, 9, -1, True, 1.5, "2", None])
def test_generate_invalid_count(world, count):
    async def go():
        session = await world.open()
        with pytest.raises(SdkError) as exc:
            await world.runner.generate(session, tab_session_id="tab-1", count=count)
        assert exc.value.code == "invalid_count" and not world.client.submitted
    asyncio.run(go())


def test_capacity_across_generate_requests(world):
    async def go():
        session = await world.open()
        for _ in range(2):
            await world.runner.generate(session, tab_session_id="tab-1", count=8)
        with pytest.raises(SdkError) as exc:
            await world.runner.generate(session, tab_session_id="tab-1")
        assert exc.value.code == "too_many_runs" and exc.value.status == 429
        assert len(world.client.submitted) == 16
        world.runner._finish(session.runs[0], LANDED)
        await world.runner.generate(session, tab_session_id="tab-1")
        assert len(world.client.submitted) == 17
    asyncio.run(go())


@pytest.mark.parametrize("meta", [None, "bad", {"title": "   "}, {"title": 42}])
def test_progress_metadata_cannot_prevent_generate(world, meta):
    graph = template_prompt()
    graph["6"]["_meta"] = meta
    sdk_templates.replace_template(world.template_id, json.dumps(graph).encode(), validate=lambda p: {"ok": True})
    async def go():
        session = await world.open()
        result = await world.runner.generate(session, tab_session_id="tab-1")
        assert result["run_ids"]
        assert session.runs[0].node_labels["6"] == "KSampler"
    asyncio.run(go())
