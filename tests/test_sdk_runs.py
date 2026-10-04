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
