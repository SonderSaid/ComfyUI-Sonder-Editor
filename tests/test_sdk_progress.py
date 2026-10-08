"""SDK-only observation against ComfyUI's executing/progress_state shapes."""
import json
from types import SimpleNamespace
import pytest
from server import sdk_progress as p

@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    p.reset_for_tests()
    monkeypatch.delenv("SONDER_SDK_PROGRESS_CAPTURE", raising=False)
    yield
    p.reset_for_tests()

def test_observer_is_transparent_even_on_failure(monkeypatch):
    calls = []
    server = SimpleNamespace(send_sync=lambda *a, **kw: (calls.append((a, kw)), "sent")[1])
    assert p.install(server) and p.install(server)
    def broken(*a):
        assert len(calls) == 1
        raise RuntimeError("bad observer")
    monkeypatch.setattr(p, "observe", broken)
    assert server.send_sync("progress", {"value": 2}, sid="client") == "sent"
    assert calls == [(("progress", {"value": 2}), {"sid": "client"})]
    assert not p.install(SimpleNamespace())

def test_non_sdk_ignored_and_capture_off_by_default(tmp_path, monkeypatch):
    calls = []
    p.add_listener(calls.append)
    p.observe("executing", {"prompt_id": "canvas", "node": "1"})
    assert calls == []
    p.watch("sdk", "token")
    monkeypatch.setattr("builtins.open", lambda *a, **kw: pytest.fail("capture is off"))
    p.observe("executing", {"prompt_id": "sdk", "node": "1"})
    assert calls[-1]["node_id"] == "1"
    p.unwatch("sdk")
    p.observe("progress", {"prompt_id": "sdk", "node": "1", "value": 1, "max": 10})
    assert len(calls) == 1

def test_expanding_parent_shows_child_count_and_clears_finished_child():
    calls = []
    p.add_listener(calls.append)
    p.watch("p", "t")
    p.observe("executing", {"prompt_id": "p", "node": "12", "display_node": "12"})
    nodes = {
        "12": {"state": "running", "value": 0, "max": 1, "display_node_id": "12"},
        "12.0.0.5": {"state": "running", "value": 2, "max": 8, "parent_node_id": "12", "display_node_id": "12"}}
    p.observe("progress_state", {"prompt_id": "p", "nodes": nodes})
    assert calls[-1]["display_node_id"] == "12"
    assert calls[-1]["count"] == {"value": 2, "max": 8}
    p.observe("progress", {"prompt_id": "p", "node": "12.0.0.5", "value": 3, "max": 8})
    assert calls[-1]["count"] == {"value": 3, "max": 8}
    nodes["12.0.0.5"]["state"] = "finished"
    p.observe("progress_state", {"prompt_id": "p", "nodes": nodes})
    assert calls[-1]["count"] is None
    p.observe("executing", {"prompt_id": "p", "node": None})
    assert calls[-1]["node_id"] == calls[-1]["display_node_id"] == ""
    assert calls[-1]["count"] is None

def test_each_node_starts_its_own_count_and_ignores_unrelated_progress():
    calls = []
    p.add_listener(calls.append)
    p.watch("p", "t")
    for node in ["1", "2:5"]:
        p.observe("executing", {"prompt_id": "p", "node": node})
        assert calls[-1]["count"] is None
        p.observe("progress", {"prompt_id": "p", "node": node, "value": 1, "max": 10})
        assert calls[-1]["count"] == {"value": 1, "max": 10}
    before = len(calls)
    p.observe("progress", {"prompt_id": "p", "node": "1", "value": 9, "max": 10})
    assert len(calls) == before

def test_capture_contains_only_watched_messages(tmp_path, monkeypatch):
    path = tmp_path / "capture.jsonl"
    monkeypatch.setenv("SONDER_SDK_PROGRESS_CAPTURE", str(path))
    p.watch("p", "t")
    for prompt in ["p", "canvas"]:
        p.observe("executing", {"prompt_id": prompt, "node": "5"})
    captured = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(captured) == 1 and captured[0]["at"] > 0
    assert captured[0]["data"]["prompt_id"] == "p"
