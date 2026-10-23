"""Run registrations, execution scope, output normalization and the observation store."""

import pytest

from server import output_media, run_outputs, run_scope, sdk_runs

OWNER = ("host", "proj", "host-1")
OTHER = ("host", "proj", "host-2")
R1 = "aaaaaaaa-0000-4000-8000-000000000001"
R2 = "aaaaaaaa-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _clean():
    for module in (sdk_runs, run_outputs):
        module.reset_for_tests()
    run_scope.reset_for_tests({"SonderEditor": False, "Pub": False, "Out": False, "Loop": True,
                               "SonderGuidesBridgeEnd": True})
    yield
    for module in (sdk_runs, run_outputs):
        module.reset_for_tests()
    run_scope.reset_for_tests()


def graph(receipt=R1, token=None):
    meta = {}
    if receipt:
        meta["sonder_receipt"] = receipt
    if token:
        meta["sonder_run"] = token
    return {
        "ed": {"class_type": "SonderEditor", "inputs": {}, "_meta": meta},
        "a": {"class_type": "Pub", "inputs": {"v": 1}},
        "outA": {"class_type": "Out", "inputs": {"v": ["a", 0], "p": ["ed", 0]}},
        "b": {"class_type": "Pub", "inputs": {"v": 2}},
        "outB": {"class_type": "Out", "inputs": {"v": ["b", 0]}},
    }


def queue(prompt_id, prompt, roots=("outA", "outB"), key="1"):
    """What ComfyUI's put and get hooks do for one prompt."""
    sdk_runs.bind_queued(prompt_id, prompt, list(roots), workflow_id="wf")
    sdk_runs.open_release(key, prompt_id, prompt, list(roots))


def executed(prompt_id, node, filename, ran=True, display=None):
    if ran:
        run_outputs.observe("executing", {"node": node, "display_node": display or node, "prompt_id": prompt_id})
    run_outputs.observe("executed", {"node": node, "display_node": display or node, "prompt_id": prompt_id,
                                     "output": {"images": [{"filename": filename, "subfolder": "", "type": "temp"}]}})


def finish(prompt_id, key="1", outcome="success", outputs=None):
    run_outputs.finish(key, prompt_id, {"outputs": outputs or {}, "meta": {}}, outcome)
    sdk_runs.close_release(key)


def read(*sources, owners=(OWNER,), workflow=""):
    return run_outputs.read_outputs(list(owners), workflow, list(sources))["sources"]


# --- Registrations -------------------------------------------------------------------

def test_registration_before_put_binds_once():
    assert sdk_runs.register_receipt(R1, OWNER, ["ed"]) == ("ok", "")
    bound = sdk_runs.bind_queued("p1", graph(), ["outA"])
    assert (bound["owners"], bound["prompt_id"], bound["editors"]) == ([OWNER], "p1", ["ed"])
    # Re-queueing the same prompt (history re-run) carries a used receipt.
    assert sdk_runs.bind_queued("p2", graph(), ["outA"]) is None
    assert sdk_runs.register_receipt(R1, OWNER) == ("duplicate", "")


def test_put_before_registration_is_observed_then_adopted():
    prompt = graph()
    bound = sdk_runs.bind_queued("p1", prompt, ["outA"])
    assert bound["owners"] == []
    hints = []
    run_outputs.add_listener(hints.append)
    sdk_runs.open_release("1", "p1", prompt, ["outA"])
    executed("p1", "a", "pub.png")
    assert hints == []  # nobody to tell yet
    assert sdk_runs.register_receipt(R1, OWNER, ["ed"]) == ("ok", "p1")
    adopted = sdk_runs.bound_run("p1")
    assert adopted["owners"] == [OWNER] and adopted["seq"] == bound["seq"], "a prompt queued first keeps its queue order"
    run_outputs.announce("p1")
    assert hints and hints[-1]["owners"] == [OWNER]
    assert read("a")["a"]["state"] == "ok"


def test_unadopted_provisional_runs_expire(monkeypatch):
    sdk_runs.bind_queued("p1", graph(), ["outA"])
    later = sdk_runs.time.time() + sdk_runs.PROVISIONAL_TTL_SEC + 1
    monkeypatch.setattr(sdk_runs.time, "time", lambda: later)
    sdk_runs.bind_queued("p2", graph(R2), ["outA"])  # any later binding evicts
    assert sdk_runs.bound_run("p1") is None


def test_a_reused_prompt_id_is_never_merged_into_another_run():
    sdk_runs.register_receipt(R1, OTHER, ["ed"])
    queue("p1", graph(R1), key="1")
    executed("p1", "outA", "theirs.png")
    finish("p1", key="1")
    sdk_runs.register_receipt(R2, OWNER, ["ed"])
    # ComfyUI accepts a client-chosen prompt id without checking it is new.
    assert sdk_runs.bind_queued("p1", graph(R2), ["outA"]) is None
    assert sdk_runs.bound_run("p1")["owners"] == [OTHER]
    assert run_outputs.read_run([OWNER], "p1") is None
    assert read("outA")["outA"]["state"] == "none"


def test_registration_bounds_and_validation():
    assert sdk_runs.register_receipt("not-a-uuid", OWNER) == ("invalid_receipt", "")
    assert sdk_runs.register_receipt(R1, ()) == ("invalid_owner", "")
    for index in range(sdk_runs.MAX_UNBOUND_PER_OWNER):
        assert sdk_runs.register_receipt(f"bbbbbbbb-0000-4000-8000-{index:012d}", OWNER)[0] == "ok"
    assert sdk_runs.register_receipt(R2, OWNER) == ("too_many", "")
    assert sdk_runs.register_receipt(R2, OTHER)[0] == "ok"


def test_unbound_receipts_expire(monkeypatch):
    assert sdk_runs.register_receipt(R1, OWNER)[0] == "ok"
    later = sdk_runs.time.time() + sdk_runs.RECEIPT_UNBOUND_TTL_SEC + 1
    monkeypatch.setattr(sdk_runs.time, "time", lambda: later)
    assert sdk_runs.bind_queued("p1", graph(), ["outA"])["owners"] == []  # parked, not bound


def test_sdk_token_binds_to_its_run_id_once():
    sdk_runs.register_token("tok", ["proj"], session_id="s1", run_id="run-1", template_id="tpl")
    bound = sdk_runs.bind_queued("p1", graph(receipt=None, token="tok"), ["outA"])
    assert (bound["owners"], bound["workflow_id"], bound["project_ids"]) == ([("sdk", "run-1")], "tpl", ["proj"])
    assert sdk_runs.bind_queued("p9", graph(receipt=None, token="tok"), ["outA"]) is None


def test_seq_is_registration_order_not_arrival():
    sdk_runs.register_receipt(R1, OWNER)
    sdk_runs.register_receipt(R2, OWNER)
    queue("late", graph(R2), key="2")
    queue("early", graph(R1), key="1")
    executed("early", "a", "early.png")
    executed("late", "a", "late.png")
    # "early" published last but was registered first: "late" is the latest.
    assert read("a")["a"]["result"]["prompt_id"] == "late"


# --- Scope ---------------------------------------------------------------------------------

def test_scope_is_the_upstream_closure_of_the_roots():
    prompt = graph()
    assert run_scope.compute(prompt, ("outA",)) == {"outA", "a", "ed"}
    assert run_scope.compute(prompt, None) is None


def test_runtime_graph_nodes_make_the_scope_unknown_except_contained_expanders():
    prompt = graph()
    prompt["loop"] = {"class_type": "Loop", "inputs": {}}
    assert run_scope.compute(prompt, ("outA",)) is None
    prompt["loop"] = {"class_type": "SonderGuidesBridgeEnd", "inputs": {}}
    assert run_scope.compute(prompt, ("outA",)) == {"outA", "a", "ed"}


def test_release_record_memoizes_scope_and_drops_the_prompt_at_close():
    prompt, calls = graph(), []

    def compute(p, roots):
        calls.append(roots)
        return run_scope.compute(p, roots)

    sdk_runs.open_release("1", "p1", prompt, ["outA"])
    assert sdk_runs.execution_scope("1", compute) == {"outA", "a", "ed"}
    assert sdk_runs.execution_scope("1", compute) == {"outA", "a", "ed"}
    assert calls == [("outA",)]
    record = sdk_runs.close_release("1")
    assert "prompt_ref" not in record
    assert sdk_runs.execution_scope("1", compute) is None


def test_unknown_roots_mean_unknown_scope():
    sdk_runs.open_release("1", "p1", graph(), None)
    assert sdk_runs.execution_scope("1", run_scope.compute) is None


# --- Observation states ----------------------------------------------------------------

def test_out_of_scope_replays_are_ignored_and_cache_hits_are_reused():
    sdk_runs.register_receipt(R1, OWNER)
    queue("p1", graph(), roots=["outA"])
    executed("p1", "a", "pub.png", ran=False)       # cache hit on the path
    executed("p1", "b", "swept.png", ran=False)     # final-sweep replay, not selected
    executed("p1", "outA", "out.png")
    finish("p1")
    sources = run_outputs.read_run([OWNER], "p1")["sources"]
    assert sorted(sources) == ["a", "outA"]
    assert sources["a"]["fresh"] is False and sources["outA"]["fresh"] is True


def test_pending_keeps_the_previous_result_and_terminal_states():
    sdk_runs.register_receipt(R1, OWNER)
    queue("p1", graph(R1), roots=["outA", "outB"], key="1")
    executed("p1", "outA", "one.png")
    finish("p1", key="1")
    sdk_runs.register_receipt(R2, OWNER)
    queue("p2", graph(R2), roots=["outA", "outB"], key="2")
    state = read("outA", "outB")
    assert state["outA"]["state"] == "pending" and state["outA"]["result"]["prompt_id"] == "p1"
    assert state["outB"]["state"] == "pending" and state["outB"]["result"] is None
    finish("p2", key="2", outcome="error")
    state = read("outA", "outB")
    assert state["outA"]["state"] == "failed" and state["outA"]["result"]["prompt_id"] == "p1"


def test_not_executed_after_a_successful_run_that_skipped_the_source():
    sdk_runs.register_receipt(R1, OWNER)
    queue("p1", graph(), roots=["outA", "outB"])
    executed("p1", "outA", "a.png")
    finish("p1")
    state = read("outA", "outB", "nowhere")
    assert state["outA"]["state"] == "ok"
    assert state["outB"]["state"] == "not_executed"


def test_finish_reconciles_outputs_events_missed():
    sdk_runs.register_receipt(R1, OWNER)
    queue("p1", graph(), roots=["outA"])
    finish("p1", outputs={"outA": {"images": [{"filename": "late.png", "type": "output"}]},
                          "b": {"images": [{"filename": "swept.png", "type": "temp"}]}})
    sources = run_outputs.read_run([OWNER], "p1")["sources"]
    assert list(sources) == ["outA"] and sources["outA"]["entries"][0]["file"]["filename"] == "late.png"


def test_owner_and_workflow_isolation():
    sdk_runs.register_receipt(R1, OWNER)
    queue("p1", graph(), roots=["outA"])
    executed("p1", "outA", "a.png")
    assert read("outA", owners=(OTHER,))["outA"]["state"] == "none"
    assert read("outA", workflow="another")["outA"]["state"] == "none"
    assert read("outA", workflow="wf")["outA"]["state"] == "ok"
    assert run_outputs.read_run([OTHER], "p1") is None
    assert run_outputs.read_run([OWNER], "p1", "another") is None
    # A run that names no workflow matches only a read that names none.
    sdk_runs.register_receipt(R2, OWNER)
    sdk_runs.bind_queued("p2", graph(R2), ["outB"])
    sdk_runs.open_release("2", "p2", graph(R2), ["outB"])
    executed("p2", "outB", "b.png")
    assert read("outB", workflow="wf")["outB"]["state"] == "not_in_run"
    assert read("outB")["outB"]["state"] == "ok"


def test_editor_context_follows_the_source_and_reuse_inherits_it():
    sdk_runs.register_receipt(R1, OWNER)
    prompt = graph(R1)
    queue("p1", prompt, roots=["outA"], key="1")
    run_outputs.note_editor_context(prompt, "ed", {"scene_id": "s1", "fps": 24.0})
    executed("p1", "outA", "a.png")
    finish("p1", key="1")
    first = read("outA")["outA"]["result"]
    assert first["editor_context"] == {"ed": {"scene_id": "s1", "fps": 24.0}}
    sdk_runs.register_receipt(R2, OWNER)
    queue("p2", graph(R2), roots=["outA"], key="2")
    executed("p2", "outA", "a.png", ran=False)
    finish("p2", key="2")
    reused = read("outA")["outA"]["result"]
    assert reused["prompt_id"] == "p2" and reused["fresh"] is False
    assert reused["editor_context"] == first["editor_context"]


def test_editor_context_needs_a_trusted_key():
    sdk_runs.register_receipt(R1, OWNER)
    prompt = graph()
    sdk_runs.bind_queued("p1", prompt, ["outA"])
    run_outputs.note_editor_context(dict(prompt), "ed", {"scene_id": "x"})
    run_outputs.note_editor_context(prompt, "ed", {"scene_id": "x"})  # queued, no release yet
    sdk_runs.open_release("1", "p1", prompt, ["outA"])
    run_outputs.note_editor_context(prompt, "ed", {"scene_id": "s1"})
    run_outputs.note_editor_context(prompt, "ed", {"scene_id": "repeat"})
    executed("p1", "outA", "a.png")
    assert read("outA")["outA"]["result"]["editor_context"] == {"ed": {"scene_id": "s1"}}


def test_saved_to_project_and_graph_revision_come_from_the_record():
    sdk_runs.register_receipt(R1, OWNER)
    prompt = graph()
    queue("p1", prompt, roots=["outA"])
    sdk_runs.note_saved_to_project("1", "2026-10-22T10:00:00")
    executed("p1", "outA", "a.png")
    finish("p1")
    result = read("outA")["outA"]["result"]
    assert result["saved_to_project"] is True and result["committed_modified_at"] == "2026-10-22T10:00:00"
    stripped = graph(receipt=None)
    assert result["graph_rev"] == run_outputs.graph_revision(stripped) != ""


def test_finished_runs_are_bounded_and_recoverable_through_tombstones(monkeypatch):
    monkeypatch.setattr(sdk_runs, "MAX_FINISHED_BOUND", 2)
    for index in range(4):
        receipt = f"cccccccc-0000-4000-8000-{index:012d}"
        sdk_runs.register_receipt(receipt, OWNER)
        queue(f"p{index}", graph(receipt), roots=["outA"], key=str(index))
        executed(f"p{index}", "outA", f"{index}.png")
        finish(f"p{index}", key=str(index))
    assert run_outputs.read_run([OWNER], "p0") is None
    assert sorted(run_outputs._DATA) == ["p2", "p3"], "observations go with their run"
    stone = run_outputs.tombstone_for([OWNER], "p0")
    assert stone and stone["scope"] == {"outA", "a", "ed"}
    assert run_outputs.tombstone_for([OTHER], "p0") is None
    recovered = run_outputs.entries_from_history(
        {"outputs": {"outA": {"images": [{"filename": "0.png", "type": "temp"}]},
                     "b": {"images": [{"filename": "x.png", "type": "temp"}]}}, "meta": {}}, stone)
    assert list(recovered["sources"]) == ["outA"] and recovered["recovered"] is True


def test_holds_pin_an_entry_and_are_bounded():
    sdk_runs.register_receipt(R1, OWNER)
    queue("p1", graph(), roots=["outA"])
    executed("p1", "outA", "a.png")
    assert run_outputs.hold([OTHER], OTHER, "p1", "outA", 0, (1, 1)) == ("unknown_run", "")
    assert run_outputs.hold([OWNER], OWNER, "p1", "outA", 5, (1, 1)) == ("unknown_entry", "")
    assert run_outputs.hold([OWNER], OWNER, "p1", "outA", 0, None) == ("missing_file", "")
    ids = [run_outputs.hold([OWNER], OWNER, "p1", "outA", 0, (1, 1))[1] for _ in range(run_outputs.MAX_HOLDS_PER_OWNER)]
    assert all(ids)
    assert run_outputs.hold([OWNER], OWNER, "p1", "outA", 0, (1, 1)) == ("too_many", "")
    assert [h["stat"] for h in run_outputs.holds_for([OWNER])][0] == (1, 1)
    assert run_outputs.release([OTHER], ids[0]) is False
    assert run_outputs.release([OWNER], ids[0]) is True


def test_hints_name_owners_and_projects_only():
    hints = []
    run_outputs.add_listener(hints.append)
    sdk_runs.register_receipt(R1, OWNER)
    queue("p1", graph(), roots=["outA"])
    executed("p1", "outA", "a.png")
    assert hints[-1] == {"prompt_id": "p1", "seq": hints[-1]["seq"], "owners": [OWNER],
                         "project_ids": ["proj"], "finished": False}


# --- Normalization ----------------------------------------------------------------------

def test_kind_comes_from_the_container_not_the_channel():
    entries, _ = output_media.normalize("3", "3", {
        "images": [{"filename": "clip.mp4", "subfolder": "video", "type": "output"},
                   {"filename": "depth.png", "type": "temp"},
                   {"filename": "anim.webp", "type": "temp"}],
        "animated": (True,),
    })
    assert [(e["kind"], e["mime"]) for e in entries] == [
        ("video", "video/mp4"), ("animated_image", "image/png"), ("animated_image", "image/webp")]
    stills, _ = output_media.normalize("3", "3", {"images": [{"filename": "depth.png", "type": "temp"}]})
    assert stills[0]["kind"] == "image"


def test_audio_vhs_sonder_video_and_unknown_channels():
    entries, _ = output_media.normalize("9", "9:2", {
        "audio": [{"filename": "a.flac", "subfolder": "", "type": "temp"}],
        "gifs": [{"filename": "v.mp4", "subfolder": "", "type": "output", "format": "video/h264-mp4",
                  "frame_rate": 8, "fullpath": "C:/secret/v.mp4"}],
        "sonder_video": [{"filename": "t.mp4", "subfolder": "sonder-projects/P/media", "type": "output", "fps": 24,
                          "has_audio": True, "poster": {"filename": "p.png", "type": "temp"},
                          "scene_map": {"scene_id": "s", "scene_start": 10, "scene_end": 20, "source_offset": 3}}],
        "text": ["hello"],
    })
    kinds = [(e["channel"], e["kind"]) for e in entries]
    assert kinds == [("audio", "audio"), ("gifs", "video"), ("sonder_video", "video"), ("text", "unsupported")]
    assert "fullpath" not in str(entries) and entries[1]["fps"] == 8.0
    assert entries[2]["scene_map"] == {"scene_id": "s", "scene_start": 10, "scene_end": 20, "source_offset": 3}
    assert entries[2]["poster"] == {"filename": "p.png", "subfolder": "", "type": "temp"}
    assert entries[0]["real_node"] == "9:2" and entries[0]["source"] == "9"


@pytest.mark.parametrize("descriptor", [
    {"filename": "../x.png", "type": "output"},
    {"filename": "x.png", "subfolder": "../up", "type": "output"},
    {"filename": "x.png", "subfolder": "/abs", "type": "output"},
    {"filename": "x.png", "subfolder": "C:/abs", "type": "output"},
    {"filename": "x.png", "subfolder": "a%2fb", "type": "output"},
    {"filename": "x\x00.png", "type": "output"},
    {"filename": "x.png", "type": "remote"},
    {"filename": "https://evil/x.png", "type": "output"},
    {"url": "https://evil/x.png"},
])
def test_hostile_descriptors_are_dropped(descriptor):
    entries, _ = output_media.normalize("1", "1", {"images": [descriptor]})
    assert entries == []


def test_entry_caps():
    images = [{"filename": f"{i}.png", "type": "temp"} for i in range(output_media.MAX_ENTRIES_PER_NODE + 5)]
    entries, dropped = output_media.normalize("1", "1", {"images": images})
    assert len(entries) == output_media.MAX_ENTRIES_PER_NODE and dropped == 5


def test_bad_scene_maps_are_dropped():
    for scene_map in ({"scene_id": "s", "scene_start": 5, "scene_end": 5, "source_offset": 0},
                      {"scene_id": "s", "scene_start": -1, "scene_end": 5, "source_offset": 0},
                      {"scene_start": 1, "scene_end": 5, "source_offset": 0}):
        entries, _ = output_media.normalize("1", "1", {"sonder_video": [
            {"filename": "t.mp4", "type": "output", "scene_map": scene_map}]})
        assert "scene_map" not in entries[0]


# --- Store regressions -----------------------------------------------------------------

def test_an_unsupported_channel_is_one_entry_even_after_finish_reconciles():
    sdk_runs.register_receipt(R1, OWNER, ["ed"])
    queue("p1", graph())
    run_outputs.observe("executing", {"node": "outA", "display_node": "outA", "prompt_id": "p1"})
    run_outputs.observe("executed", {"node": "outA", "display_node": "outA", "prompt_id": "p1", "output": {"text": ["hi"]}})
    run_outputs.finish("1", "p1", {"outputs": {"outA": {"text": ["hi"]}}, "meta": {}}, "success")
    assert len(run_outputs.read_run([OWNER], "p1")["sources"]["outA"]["entries"]) == 1


def test_read_run_inherits_context_only_from_the_same_owner():
    sdk_runs.register_receipt(R1, OTHER, ["ed"])
    queue("p1", graph(R1), key="1")
    executed("p1", "outA", "x.png")
    run_outputs._DATA["p1"]["editor_context"]["ed"] = {"scene_id": "theirs"}
    finish("p1", key="1")
    sdk_runs.register_receipt(R2, OWNER, ["ed"])
    queue("p2", graph(R2), key="2")
    executed("p2", "outA", "x.png", ran=False)
    finish("p2", key="2")
    assert run_outputs.read_run([OWNER], "p2")["sources"]["outA"]["editor_context"] is None
    assert read("outA")["outA"]["result"]["editor_context"] is None


def test_nodes_that_publish_nothing_make_no_slot_and_no_hint():
    hints = []
    run_outputs.add_listener(hints.append)
    sdk_runs.register_receipt(R1, OWNER, ["ed"])
    queue("p1", graph())
    for node in ("ed", "a", "b"):
        run_outputs.observe("executed", {"node": node, "display_node": node, "prompt_id": "p1", "output": None})
    executed("p1", "outA", "x.png")
    executed("p1", "outA", "x.png")  # the same file again is no news
    assert hints and len(hints) == 1 and sorted(run_outputs._DATA["p1"]["nodes"]) == ["outA"]


def test_a_prompt_removed_from_the_queue_is_abandoned_not_pending_forever():
    sdk_runs.register_receipt(R1, OWNER, ["ed"])
    sdk_runs.register_receipt(R2, OWNER, ["ed"])
    sdk_runs.bind_queued("p1", graph(R1), ["outA"])
    sdk_runs.bind_queued("taken", graph(R2), ["outA"])
    sdk_runs.open_release("9", "taken", graph(R2), ["outA"])  # ComfyUI took it: never reaped
    assert read("outA")["outA"]["state"] == "pending"
    run_outputs.reap_removed(None)  # the queue could not say: nothing ends
    # The grace covers a put racing the reap.
    assert sdk_runs.reap_removed_bound(set(), grace=60) == []
    ended = sdk_runs.reap_removed_bound({"other"}, grace=-1)
    assert [bound["prompt_id"] for bound in ended] == ["p1"]
    assert run_outputs.read_run([OWNER], "p1")["outcome"] == run_outputs.OUTCOME_REMOVED
    assert not sdk_runs.bound_run("taken")["finished"]
    sdk_runs.close_release("9")
    # It never ran, so it hides nothing.
    assert read("outA")["outA"]["latest_run"]["prompt_id"] == "taken"


def test_unknown_scope_never_claims_a_source_the_prompt_lacks():
    sdk_runs.register_receipt(R1, OWNER, ["ed"])
    queue("p1", graph(R1), key="1")
    executed("p1", "outB", "b.png")
    finish("p1", key="1")
    g = graph(R2)
    del g["outB"], g["b"]
    g["loop"] = {"class_type": "Loop", "inputs": {}}  # an unallowlisted expander: scope unknown
    sdk_runs.register_receipt(R2, OWNER, ["ed"])
    queue("p2", g, roots=("outA",), key="2")
    assert read("outB")["outB"]["state"] == "ok"  # while p2 is open
    finish("p2", key="2")
    assert read("outB")["outB"]["state"] == "ok"
    assert read("outA")["outA"]["state"] == "not_executed"


def test_unfinished_runs_are_capped(monkeypatch):
    monkeypatch.setattr(sdk_runs, "MAX_UNFINISHED_BOUND", 2)
    for index in range(4):
        receipt = f"dddddddd-0000-4000-8000-{index:012d}"
        sdk_runs.register_receipt(receipt, OWNER)
        sdk_runs.bind_queued(f"q{index}", graph(receipt), ["outA"])
    states = [sdk_runs.bound_run(f"q{index}")["outcome"] for index in range(4)]
    assert states == [run_outputs.OUTCOME_REMOVED, run_outputs.OUTCOME_REMOVED, "", ""]


def test_one_busy_owner_does_not_evict_everyone_elses_latest(monkeypatch):
    monkeypatch.setattr(sdk_runs, "MAX_FINISHED_BOUND", 3)
    sdk_runs.register_receipt(R1, OTHER)
    queue("theirs", graph(R1), roots=["outA"], key="t")
    finish("theirs", key="t")
    for index in range(5):
        receipt = f"eeeeeeee-0000-4000-8000-{index:012d}"
        sdk_runs.register_receipt(receipt, OWNER)
        queue(f"mine{index}", graph(receipt), roots=["outA"], key=str(index))
        finish(f"mine{index}", key=str(index))
    assert sdk_runs.bound_run("theirs") is not None
    assert [sdk_runs.bound_run(f"mine{index}") is not None for index in range(5)] == [False, False, False, True, True]


def test_a_running_output_carries_only_its_own_editors_context():
    sdk_runs.register_receipt(R1, OWNER, ["ed", "ed2"])
    prompt = graph(R1)
    prompt["ed2"] = {"class_type": "SonderEditor", "inputs": {}, "_meta": {}}
    prompt["outB"]["inputs"]["p"] = ["ed2", 0]
    queue("p1", prompt, roots=["outA", "outB"])
    run_outputs.note_editor_context(prompt, "ed", {"scene_id": "s1"})
    run_outputs.note_editor_context(prompt, "ed2", {"scene_id": "s2"})
    executed("p1", "outA", "a.png")
    assert read("outA")["outA"]["result"]["editor_context"] == {"ed": {"scene_id": "s1"}}
    finish("p1")
    assert read("outA")["outA"]["result"]["editor_context"] == {"ed": {"scene_id": "s1"}}


def test_v3_and_tuple_hidden_inputs_make_the_scope_unknown(monkeypatch):
    import enum
    import sys
    import types

    class Hidden(str, enum.Enum):
        dynprompt = "DYNPROMPT"

    class V3Style:
        @classmethod
        def INPUT_TYPES(cls):
            return {"required": {}, "hidden": {"dynprompt": ("DYNPROMPT",), "unique_id": ("UNIQUE_ID",)}}

    class EnumStyle:
        @classmethod
        def INPUT_TYPES(cls):
            return {"hidden": {"execution_list": Hidden.dynprompt}}

    class Plain:
        @classmethod
        def INPUT_TYPES(cls):
            return {"hidden": {"unique_id": "UNIQUE_ID", "prompt": ("PROMPT",)}}

    fake = types.ModuleType("nodes")
    fake.NODE_CLASS_MAPPINGS = {"V3Loop": V3Style, "EnumLoop": EnumStyle, "Plain": Plain}
    monkeypatch.setitem(sys.modules, "nodes", fake)
    run_scope.reset_for_tests()
    for class_type, expected in (("V3Loop", True), ("EnumLoop", True), ("Plain", False)):
        assert run_scope._class_sees_runtime_graph(class_type) is expected, class_type
    prompt = graph()
    prompt["x"] = {"class_type": "V3Loop", "inputs": {}}
    assert run_scope.compute(prompt, ("outA",)) is None
