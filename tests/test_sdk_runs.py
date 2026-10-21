"""Run release records, claim leases and SDK run tokens (server/sdk_runs.py)."""

import types

import pytest

from server import sdk_runs


@pytest.fixture(autouse=True)
def _clean_registry():
    sdk_runs.reset_for_tests()
    yield
    sdk_runs.reset_for_tests()


def test_a_key_is_trusted_only_while_its_release_is_open():
    prompt = {"1": {"class_type": "SonderEditor", "inputs": {}}}
    assert sdk_runs.open_release(4, "prompt-a", prompt)
    assert sdk_runs.is_trusted("4")
    assert sdk_runs.trusted_key_for_prompt(prompt) == "4"
    # An equal but distinct prompt object is not the one the queue handed out.
    assert sdk_runs.trusted_key_for_prompt(dict(prompt)) == ""

    record = sdk_runs.close_release("4")
    assert record["prompt_id"] == "prompt-a"
    assert not sdk_runs.is_trusted("4")
    assert sdk_runs.trusted_key_for_prompt(prompt) == ""
    assert sdk_runs.close_release("4") is None


def test_a_lease_needs_an_open_release_and_a_retry_replaces_it():
    assert not sdk_runs.take_lease("9", "dir-a", "job-1")
    sdk_runs.open_release("9", "prompt-9")
    assert sdk_runs.take_lease("9", "dir-a", "job-1")
    # A conflict retry re-claims (possibly another job) under the same key.
    assert sdk_runs.take_lease("9", "dir-a", "job-2")
    sdk_runs.note_project("9", "dir-a", ["proj", "Folder Name", ""])
    record = sdk_runs.close_release("9")
    assert record["leases"] == [{"project_dir": "dir-a", "job_id": "job-2"}]
    assert record["projects"] == {"dir-a": ["Folder Name", "proj"]}


def test_a_settled_job_leaves_nothing_to_return():
    sdk_runs.open_release("1", "prompt-1")
    sdk_runs.take_lease("1", "dir-a", "job-1")
    assert sdk_runs.settle_lease("dir-a", "job-1")
    assert not sdk_runs.settle_lease("dir-a", "job-1")
    assert sdk_runs.close_release("1")["leases"] == []


def test_tokens_are_single_use_and_bound_to_their_project():
    assert sdk_runs.claim_token("never-registered", ["proj"]) == sdk_runs.TOKEN_UNKNOWN

    sdk_runs.register_token("tok-1", ["proj", "Folder Name"], session_id="s1")
    assert sdk_runs.claim_token("tok-1", ["other"]) == sdk_runs.TOKEN_MISMATCH
    sdk_runs.open_release("3", "prompt-3")
    assert sdk_runs.claim_token("tok-1", ["Folder Name"], key="3") == sdk_runs.TOKEN_OK
    assert sdk_runs.claim_token("tok-1", ["proj"], key="3") == sdk_runs.TOKEN_USED

    sdk_runs.note_token_job("tok-1", "job-7")
    assert sdk_runs.token_state("tok-1")["job_id"] == "job-7"
    record = sdk_runs.close_release("3")
    assert record["token"] == "tok-1"
    assert sdk_runs.token_state("tok-1") is None
    assert sdk_runs.claim_token("tok-1", ["proj"]) == sdk_runs.TOKEN_UNKNOWN


def test_a_first_result_lives_on_its_open_run_and_is_never_replaced():
    marker = object()
    assert not sdk_runs.remember_result("5", "1", marker)
    assert sdk_runs.first_result("5", "1") == (False, None)

    sdk_runs.open_release("5", "prompt-5")
    assert sdk_runs.first_result("5", "1") == (False, None)
    assert sdk_runs.remember_result("5", 1, marker)
    assert sdk_runs.remember_result("5", "1", object())
    assert sdk_runs.first_result("5", "1") == (True, marker)
    # A falsy result is still a result; another node and another run are not.
    assert sdk_runs.remember_result("5", "2", None)
    assert sdk_runs.first_result("5", "2") == (True, None)
    assert sdk_runs.first_result("6", "1") == (False, None)
    assert not sdk_runs.remember_result("5", "", marker)

    assert "results" not in sdk_runs.close_release("5")
    assert sdk_runs.first_result("5", "1") == (False, None)
    # Reopening the key (ComfyUI reusing a queue item id) starts empty.
    sdk_runs.open_release("5", "prompt-5b")
    assert sdk_runs.first_result("5", "1") == (False, None)


def test_a_registered_token_that_never_runs_expires(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(sdk_runs, "time", types.SimpleNamespace(time=lambda: now[0]))
    sdk_runs.register_token("tok-old", ["proj"])
    now[0] += sdk_runs.REGISTERED_TOKEN_TTL_SEC + 1
    assert sdk_runs.claim_token("tok-old", ["proj"]) == sdk_runs.TOKEN_UNKNOWN


def test_a_release_learns_its_token_projects_when_it_opens():
    sdk_runs.register_token("tok-2", ["proj", "Folder"])
    prompt = {"1": {"class_type": "SonderEditor", "inputs": {}, "_meta": {"sonder_run": "tok-2"}}}
    sdk_runs.open_release("6", "prompt-6", prompt)
    assert sdk_runs.close_release("6")["token_project_ids"] == ["Folder", "proj"]
    # An unregistered token names nothing.
    sdk_runs.open_release("7", "prompt-7", {"1": {"_meta": {"sonder_run": "stranger"}}})
    assert sdk_runs.close_release("7")["token_project_ids"] == []


def test_an_empty_token_cannot_be_registered():
    with pytest.raises(ValueError):
        sdk_runs.register_token("  ", ["proj"])


def test_a_run_that_ends_before_its_editor_executes_reports_and_retires_its_token():
    # The editor never ran, so claim_token never set record["token"]; the
    # queue-time hint is what the run reports, and closing retires it so the
    # proxy's stored workflow cannot be replayed with it.
    sdk_runs.register_token("tok-early", ["proj"])
    prompt = {"1": {"class_type": "SonderEditor", "inputs": {}, "_meta": {"sonder_run": "tok-early"}}}
    sdk_runs.open_release("8", "prompt-8", prompt)
    record = sdk_runs.close_release("8")
    assert record["token"] == ""
    assert sdk_runs.run_token_of(record) == "tok-early"
    assert sdk_runs.claim_token("tok-early", ["proj"]) == sdk_runs.TOKEN_UNKNOWN


def test_release_state_tells_never_taken_from_taken(monkeypatch):
    now = [5000.0]
    monkeypatch.setattr(sdk_runs, "time", types.SimpleNamespace(time=lambda: now[0]))
    assert sdk_runs.release_state_for_prompt("prompt-x") == sdk_runs.RELEASE_UNKNOWN
    sdk_runs.open_release("11", "prompt-x")
    assert sdk_runs.release_state_for_prompt("prompt-x") == sdk_runs.RELEASE_OPEN
    sdk_runs.close_release("11")
    assert sdk_runs.release_state_for_prompt("prompt-x") == sdk_runs.RELEASE_CLOSED
    # Closed states are kept for a while, then pruned at a later close.
    now[0] += sdk_runs.CLOSED_RELEASE_RETENTION_SEC + 1
    sdk_runs.open_release("12", "prompt-y")
    sdk_runs.close_release("12")
    assert sdk_runs.release_state_for_prompt("prompt-x") == sdk_runs.RELEASE_UNKNOWN
    assert sdk_runs.release_state_for_prompt("prompt-y") == sdk_runs.RELEASE_CLOSED


def test_the_claimed_job_is_kept_on_the_release_even_after_its_lease_settles():
    sdk_runs.register_token("tok-c", ["proj"])
    sdk_runs.open_release("13", "prompt-13")
    assert sdk_runs.claim_token("tok-c", ["proj"], key="13") == sdk_runs.TOKEN_OK
    sdk_runs.take_lease("13", "dir-a", "job-c")
    sdk_runs.note_token_job("tok-c", "job-c")
    sdk_runs.settle_lease("dir-a", "job-c")  # the save completed it
    record = sdk_runs.close_release("13")
    assert record["leases"] == [] and record["claimed_job_id"] == "job-c"


def test_run_finished_listeners_are_isolated_and_removable():
    seen = []

    def broken(payload):
        raise RuntimeError("listener bug")

    remove_broken = sdk_runs.add_run_finished_listener(broken)
    remove_seen = sdk_runs.add_run_finished_listener(seen.append)
    sdk_runs.notify_run_finished({"prompt_id": "p"})
    assert seen == [{"prompt_id": "p"}]
    remove_broken()
    remove_seen()
    sdk_runs.notify_run_finished({"prompt_id": "q"})
    assert seen == [{"prompt_id": "p"}]
