"""The server and the browser decide a scene batch's re-application policy alike.

`server/scene_mutation_addressing.py` decides the `addressing=` the scene
mutation route commits under; `web/js/scene_mutation_addressing.js` decides
whether the browser re-sends the same batch after a 409. Two copies of one
decision that disagree are worse than one (`roadmap.md`, bare-writes entry), so
this suite holds them to one table:

1. **The tables** carry the same entries, addressing classes, ids and guards.
2. **The decisions** agree on a generated battery of payloads -- every
   dispatcher operation's best case plus each way it can degrade.
3. **Media I/O is never re-applied**: every operation `_media_io_operation_count`
   can count is positional with nothing that could promote it, because a second
   attempt would extract media again against a reloaded document.

And, through the real route, what the twin buys:

4. A header-less or alias-addressed positional batch that loses a race is
   refused, and the competing write survives. Before this landing it committed
   through a bare `save_project` and silently destroyed the other write.
5. An identity batch is re-applied, and every guard re-runs against the
   reloaded document -- a guard that no longer holds refuses on the retry.
"""

import asyncio
import copy
import importlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

import server
from server import routes, scene_mutation_addressing as addressing
from server.project_manager import (ProjectVersionConflict, create_project,
                                    load_project, save_project)
from server.timeline_state import ClipReference, PromptSection, Scene

from test_scene_mutation_registration import _dispatcher_op_types
from test_scene_mutation_retry_policy import (ADDRESSING_JS, CANONICAL_PAYLOAD,
                                              NEVER_RETRYABLE, _entries, _run_node,
                                              _run_widget_node)

ROOT = Path(__file__).resolve().parents[1]
MUTATIONS_PATH = "/sonder-editor/project/{project_id}/scenes/{scene_id}/mutations"


# ---------------------------------------------------------------------------
# 1. The tables
# ---------------------------------------------------------------------------

def test_both_tables_classify_exactly_the_dispatcher_operations():
    dispatcher = set(_dispatcher_op_types())
    assert set(addressing.SCENE_MUTATION_ADDRESSING) == dispatcher
    assert set(_entries()) == dispatcher


def test_the_tables_agree_entry_by_entry():
    js = _entries()
    for op_type, entry in addressing.SCENE_MUTATION_ADDRESSING.items():
        mirror = js[op_type]
        assert mirror["addressing"].lower() == entry.addressing, op_type
        assert tuple(mirror["ids"]) == entry.ids, op_type
        js_refines = mirror["refine"] or "refine:" in mirror["evidence"]
        assert js_refines == bool(entry.refine), op_type
        assert [dict(guard) for guard in mirror["guards"]] == [
            {"bag": guard.bag, "identifying": guard.identifying,
             "required": guard.required, "validator": guard.validator}
            for guard in entry.promoted_by], op_type


def test_the_shared_constants_agree():
    source = ADDRESSING_JS.read_text(encoding="utf-8")
    counts = re.search(r"LANE_COUNT_FIELDS = freeze\(\[(.*?)\]\)", source, re.S)
    assert tuple(re.findall(r'"([^"]+)"', counts.group(1))) == addressing.LANE_COUNT_FIELDS
    dest = re.search(r"LANE_DESTINATION_FIELD = freeze\(\{(.*?)\}\)", source, re.S)
    assert dict(re.findall(r'(\w+): "(\w+)"', dest.group(1))) == addressing.LANE_DESTINATION_FIELD
    per_item = re.search(r"PER_ITEM_IDENTITY = freeze\(\{(.*?)\n\}\);", source, re.S).group(1)
    parsed = {
        name: (durable == "true", tuple(re.findall(r'"([^"]+)"', ids)))
        for name, durable, ids in re.findall(
            r"(\w+): freeze\(\{ durable: (true|false), identifying: freeze\(\[([^\]]*)\]\) \}\)",
            per_item)}
    assert parsed == addressing.PER_ITEM_IDENTITY


# ---------------------------------------------------------------------------
# 2. The decisions
# ---------------------------------------------------------------------------

def _variants(op_type: str, payload: dict):
    """The best case, then each way a caller can weaken it."""
    base = dict(copy.deepcopy(payload), type=op_type)
    yield base
    for key, value in payload.items():
        if isinstance(value, str) and value:
            yield {**copy.deepcopy(base), key: ""}
            yield {**copy.deepcopy(base), key: "   "}
        if isinstance(value, dict):
            stripped = copy.deepcopy(base)
            del stripped[key]
            yield stripped
            yield {**copy.deepcopy(base), key: {}}
            yield {**copy.deepcopy(base), key: []}
            for inner in value:
                blanked = copy.deepcopy(base)
                blanked[key][inner] = ""
                yield blanked
                removed = copy.deepcopy(base)
                del removed[key][inner]
                yield removed
    fields = dict(base.get("fields") or {}) if isinstance(base.get("fields"), dict) else {}
    for extra in ("track_index", "lane_index", "video_lane_count", "reference_lane_count"):
        yield {**copy.deepcopy(base), "fields": {**fields, extra: 1}}


def _battery():
    cases = []
    for op_type, payload in sorted(CANONICAL_PAYLOAD.items()):
        cases.extend(_variants(op_type, payload))
    for lane_type in ("guide", "prompt", "prompt_global", "video", "audio",
                      "motion_driver", "reference", "bogus", ""):
        for expected in ({"lane_id": "L1"}, {"lane_id": ""}, None):
            case = {"type": "update_lane_config", "lane_type": lane_type,
                    "lane_index": 1, "fields": {"locked": True}}
            if expected is not None:
                case["expected"] = expected
            cases.append(case)
    for items in (
            [], None, "x", [None], ["clip"], [{"type": "clip"}],
            [{"type": "clip", "id": 0}], [{"type": "clip", "id": "  "}],
            [{"type": "guide", "id": 10}],
            [{"type": "guide", "id": 10, "expected": {"guide_id": "g1"}}],
            [{"type": "guide", "id": 10, "expected": {"asset_id": "a"}}],
            [{"type": "prompt", "index": 0, "expected": {"prompt_id": "p"}}],
            [{"type": "prompt", "index": 0, "expected": []}],
            [{"type": "reference", "id": "r1"}, {"type": "audio", "id": "t1"}],
            [{"type": "mystery", "id": "m"}]):
        for op_type in ("bulk_delete_items", "create_link_group", "unlink_items"):
            cases.append({"type": op_type, "items": items})
    # Characters on which JS `trim()` and Python `str.strip()` disagree.
    for blank in ("﻿", "", "", "", " ", "　", "​"):
        cases.append({"type": "update_clip", "clip_id": blank, "fields": {}})
        cases.append({"type": "update_guide", "frame_index": 1, "fields": {},
                      "expected": {"guide_id": blank}})
        cases.append({"type": "bulk_delete_items", "items": [{"type": "clip", "id": blank}]})
    # Names an object literal inherits.
    for name in ("constructor", "toString", "__proto__", "hasOwnProperty"):
        cases.append({"type": name, "clip_id": "c1"})
        for op_type in ("bulk_delete_items", "unlink_items"):
            cases.append({"type": op_type, "items": [{"type": name, "id": "x"}]})
    cases.extend([
        {"type": "reticulate_splines", "id": "x"},
        {"type": ""}, {}, {"type": "update_clip"},
        {"type": "update_clip", "clip_id": 7, "fields": {}},
        {"type": "update_clip", "clip_id": "c", "fields": []},
        {"type": "update_clip", "clip_id": "c", "fields": "track_index"},
        {"type": "create_prompt_semantic_unit", "unit": []},
        {"type": "create_prompt_semantic_unit", "unit": "u1"},
        {"type": "update_scene_fields", "fields": ["video_lane_count"]},
        {"type": "replace_prompt_sections", "expected": {"sections": None}},
        {"type": "move_guide", "expected": {"guide_id": "g", "replaces_guide_id": None}},
    ])
    return cases


def _js_decisions(cases):
    """Cases travel on stdin: the battery is longer than a Windows command line."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    url = ADDRESSING_JS.as_uri()
    script = f"""
        import {{ sceneMutationRetryEvidence }} from {url!r};
        let raw = "";
        for await (const chunk of process.stdin) raw += chunk;
        for (const one of JSON.parse(raw)) {{
            const row = sceneMutationRetryEvidence(one);
            console.log(JSON.stringify([row.addressing, row.retryable]));
        }}
    """
    result = subprocess.run([node, "--input-type=module", "-e", script], cwd=ROOT,
                            input=json.dumps(cases), text=True, capture_output=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr or result.stdout
    return [tuple(json.loads(line)) for line in result.stdout.splitlines() if line.strip()]


def test_every_decision_agrees_across_the_battery():
    cases = _battery()
    assert len(cases) > 300, "the battery lost its variants"
    js = _js_decisions(cases)
    assert len(js) == len(cases)
    mismatches = []
    for case, js_row in zip(cases, js):
        py = addressing.scene_mutation_retry_evidence(case)
        if (py["addressing"], py["retryable"]) != js_row:
            mismatches.append((case, js_row, (py["addressing"], py["retryable"])))
    assert not mismatches, "\n".join(
        f"{json.dumps(case)}: js={js_row} py={py_row}"
        for case, js_row, py_row in mismatches[:20])


def test_the_replay_declined_header_is_spelled_the_same_on_both_sides():
    source = ADDRESSING_JS.read_text(encoding="utf-8")
    assert re.search(r'REPLAY_DECLINED_HEADER = "([^"]+)"', source).group(1)         == addressing.REPLAY_DECLINED_HEADER
    assert re.search(r'REPLAY_DECLINED_VALUE = "([^"]+)"', source).group(1)         == addressing.REPLAY_DECLINED_VALUE


def test_a_declined_replay_can_only_demote():
    identity = [{"type": "update_clip", "clip_id": "c1", "fields": {}}]
    positional = [{"type": "set_lane_count", "lane_type": "video", "count": 2}]
    assert addressing.derive_batch_addressing(identity) == "identity"
    assert addressing.derive_batch_addressing(identity, replay_declined=True) == "positional"
    assert addressing.derive_batch_addressing(positional, replay_declined=False) == "positional"


def test_the_client_sends_the_declined_header_exactly_when_it_will_not_re_send():
    _run_widget_node("""
        const w = makeWidget();
        const identity = [{ type: 'update_clip', clip_id: 'c1', fields: { muted: true } }];
        await w._runSceneMutation(identity, { key: 'a', refreshScenes: false, coalesce: false });
        await w._runSceneMutation(identity, { key: 'b', refreshScenes: false, coalesce: false,
            retryOnConflict: false });
        await w._runSceneMutation([{ type: 'set_lane_count', lane_type: 'video', count: 3 }],
            { key: 'c', refreshScenes: false, coalesce: false });
        const header = (one) => one.headers['X-Sonder-Mutation-Replay'] || '';
        assert.deepEqual(sent.map(header), ['', 'declined', 'declined']);
        assert.deepEqual(sent.map(one => one.options.retryOnConflict), [true, false, false]);
    """)


def test_the_best_case_pin_holds_on_the_server_too():
    denied = {op for op, payload in CANONICAL_PAYLOAD.items()
              if not addressing.scene_mutation_retry_evidence(
                  dict(payload, type=op))["retryable"]}
    assert denied == NEVER_RETRYABLE


def test_a_malformed_operation_is_decided_without_raising():
    for weird in (None, "update_clip", 3, [], ["type"]):
        row = addressing.scene_mutation_retry_evidence(weird)
        assert row["retryable"] is False


def test_batch_addressing_matches_the_browser_batch_policy():
    batches = [
        [],
        [{"type": "update_clip", "clip_id": "c1", "fields": {}}],
        [{"type": "update_clip", "clip_id": "c1", "fields": {}},
         {"type": "set_lane_count", "lane_type": "video", "count": 3}],
        [{"type": "set_lane_count", "lane_type": "video", "count": 3},
         {"type": "update_clip", "clip_id": "c1", "fields": {}}],
        [{"type": "update_scene_fields", "fields": {"width": 1}},
         {"type": "delete_link_group", "group_id": "g"}],
        [{"type": "reticulate_splines"}],
    ]
    url = ADDRESSING_JS.as_uri()
    out = _run_node(f"""
        import {{ deriveRetryOnConflict }} from {url!r};
        for (const batch of {json.dumps(batches)}) console.log(String(deriveRetryOnConflict(batch)));
        console.log(String(deriveRetryOnConflict(null)));
    """)
    js = [line.strip() == "true" for line in out.splitlines() if line.strip()]
    py = [addressing.derive_batch_addressing(batch) == "identity" for batch in batches]
    py.append(addressing.derive_batch_addressing(None) == "identity")
    assert js == py


# ---------------------------------------------------------------------------
# 3. Media I/O is never re-applied
# ---------------------------------------------------------------------------

def _media_budget_operation_types() -> set[str]:
    source = Path(routes.__file__).read_text(encoding="utf-8")
    start = source.index("def _media_io_operation_count(")
    end = source.index("\ndef ", start + 1)
    return set(re.findall(r'op_type == "(\w+)"', source[start:end]))


def test_every_media_io_operation_is_positional_with_no_promotion():
    counted = _media_budget_operation_types()
    assert counted == {"create_audio_track", "create_clip"}, (
        "the media budget counts a new operation; decide its addressing here")
    for op_type in counted:
        entry = addressing.SCENE_MUTATION_ADDRESSING[op_type]
        assert entry.addressing == addressing.POSITIONAL, op_type
        assert not entry.promoted_by and not entry.refine, (
            f"{op_type} could be promoted to a re-applied batch, which would "
            "extract media a second time against a reloaded document")


# ---------------------------------------------------------------------------
# 4-5. Through the real route
# ---------------------------------------------------------------------------

class DummyRequest(dict):
    def __init__(self, *, match_info=None, body=None, method="POST", headers=None):
        super().__init__()
        self.match_info = match_info or {}
        self.query = {}
        self.headers = headers or {}
        self._body = body
        self.method = method
        self.path = "/sonder-editor/project/p"

    async def json(self):
        return self._body


def _section(prompt_id, **kwargs):
    section = PromptSection(0, 50, **kwargs)
    section.prompt_id = prompt_id
    return section


@pytest.fixture
def race(monkeypatch, tmp_path):
    fake_prompt_server = SimpleNamespace(
        instance=SimpleNamespace(routes=web.RouteTableDef(), app=web.Application()))
    monkeypatch.setattr(server, "PromptServer", fake_prompt_server, raising=False)
    route_module = importlib.reload(routes)
    project = create_project("Race", base_dir=str(tmp_path))
    scene = Scene(scene_id="s1", name="Scene", duration_frames=100, width=640, height=480)
    scene.prompt_sections = [_section("p1", channels={"visual": "a"})]
    scene.clips = [ClipReference(clip_id="c1", source_path="media/a.mp4", track_index=0,
                                 timeline_start_frame=0, timeline_end_frame=10)]
    project.scenes = [scene]
    save_project(project, notify=False)
    monkeypatch.setattr(route_module, "_get_base_dir", lambda: str(tmp_path))
    handler = next(route.handler for route in route_module.routes
                   if route.method == "POST" and route.path == MUTATIONS_PATH)
    folder_id = os.path.basename(project.project_dir)
    attempts = []
    original_batch = route_module._apply_scene_mutation_batch

    def arm(competing_write):
        """Run `competing_write(project_dir)` inside the first attempt's window."""
        def hooked(project, scene_id, operations):
            attempts.append(project.modified_at)
            if len(attempts) == 1:
                competing_write(project.project_dir)
            return original_batch(project, scene_id, operations)
        monkeypatch.setattr(route_module, "_apply_scene_mutation_batch", hooked)

    def post(operations, *, project_id=folder_id, if_match="", headers=None):
        request = DummyRequest(
            match_info={"project_id": project_id, "scene_id": "s1"},
            headers={**({"If-Match": if_match} if if_match else {}), **(headers or {})},
            body={"operations": operations})
        response = asyncio.run(handler(request))
        return response.status, json.loads(response.body.decode("utf-8"))

    return SimpleNamespace(project_dir=project.project_dir, folder_id=folder_id,
                           canonical_id=project.project_id, arm=arm, post=post,
                           attempts=attempts)


def _rename_scene(name):
    def write(project_dir):
        other = load_project(project_dir)
        other.scenes[0].name = name
        save_project(other, notify=False)
    return write


@pytest.mark.parametrize("spelling", ["folder", "canonical"])
def test_a_header_less_positional_batch_no_longer_clobbers_a_concurrent_write(
        race, spelling):
    """The bare-save loss, closed: one attempt, refused, and B's write survives."""
    race.arm(_rename_scene("Writer B"))
    project_id = race.folder_id if spelling == "folder" else race.canonical_id
    with pytest.raises(ProjectVersionConflict):
        race.post([{"type": "set_lane_count", "lane_type": "video", "count": 3}],
                  project_id=project_id)
    assert len(race.attempts) == 1, "a positional batch must never be re-applied"
    stored = load_project(race.project_dir)
    assert stored.scenes[0].name == "Writer B"
    assert stored.scenes[0].video_lane_count != 3


def test_an_identity_batch_is_re_applied_onto_the_competing_write(race):
    race.arm(_rename_scene("Writer B"))
    status, payload = race.post(
        [{"type": "update_clip", "clip_id": "c1", "fields": {"muted": True}}])
    assert status == 200
    assert len(race.attempts) == 2
    assert race.attempts[0] != race.attempts[1], "the retry must reload"
    stored = load_project(race.project_dir)
    assert stored.scenes[0].name == "Writer B"
    assert stored.scenes[0].clips[0].muted is True
    assert payload["scene"]["name"] == "Writer B"


def test_a_guard_re_runs_on_the_retry_and_refuses_a_row_that_moved(race):
    """Re-application is not replay: the competing write took the section away."""
    def delete_section(project_dir):
        other = load_project(project_dir)
        other.scenes[0].prompt_sections = [_section("p2")]
        save_project(other, notify=False)
    race.arm(delete_section)
    status, payload = race.post([{
        "type": "update_prompt_section", "index": 0,
        "expected": {"prompt_id": "p1"}, "fields": {"muted": True}}])
    assert status == 409
    assert payload["code"] == "identity_mismatch"
    assert len(race.attempts) == 2
    stored = load_project(race.project_dir)
    assert [section.prompt_id for section in stored.scenes[0].prompt_sections] == ["p2"]
    assert stored.scenes[0].prompt_sections[0].muted is False


def test_a_batch_whose_caller_declined_replay_is_refused_not_re_applied(race):
    """The Prompt tool's whole-array writes decline so a conflict is replanned.

    `replace_prompt_sections`' guard compares ids and bounds, not content, so a
    server-side replay would overwrite a concurrent text edit the 409 protected.
    """
    race.arm(_rename_scene("Writer B"))
    with pytest.raises(ProjectVersionConflict):
        race.post([{"type": "update_clip", "clip_id": "c1", "fields": {"muted": True}}],
                  headers={"X-Sonder-Mutation-Replay": "declined"})
    assert len(race.attempts) == 1
    stored = load_project(race.project_dir)
    assert stored.scenes[0].name == "Writer B"
    assert stored.scenes[0].clips[0].muted is False


def test_no_header_value_acquires_a_replay_for_a_positional_batch(race):
    race.arm(_rename_scene("Writer B"))
    with pytest.raises(ProjectVersionConflict):
        race.post([{"type": "set_lane_count", "lane_type": "video", "count": 3}],
                  headers={"X-Sonder-Mutation-Replay": "allowed"})
    assert len(race.attempts) == 1


def test_a_re_application_the_server_absorbs_is_recorded(race, monkeypatch):
    events = []
    monkeypatch.setattr(routes, "record_diag_event",
                        lambda name, **fields: events.append((name, fields)))
    race.arm(_rename_scene("Writer B"))
    status, _payload = race.post(
        [{"type": "update_clip", "clip_id": "c1", "fields": {"muted": True}}])
    assert status == 200
    reapplied = [fields for name, fields in events if name == "project_versioned_reapply"]
    assert len(reapplied) == 1 and reapplied[0]["attempt"] == 1


def test_a_stale_if_match_is_still_refused_before_any_attempt(race):
    version = load_project(race.project_dir).modified_at
    _rename_scene("Writer B")(race.project_dir)
    race.arm(lambda _project_dir: None)
    with pytest.raises(ProjectVersionConflict):
        race.post([{"type": "update_clip", "clip_id": "c1", "fields": {"muted": True}}],
                  if_match=version)
    assert race.attempts == [], "a stale precondition is refused at load"


def test_an_identity_no_op_that_loses_the_race_answers_from_the_reloaded_document(race):
    race.arm(_rename_scene("Writer B"))
    status, payload = race.post(
        [{"type": "update_scene_fields", "fields": {"width": 640}}])
    assert status == 200
    assert payload["committed"] is False
    assert len(race.attempts) == 2
    assert payload["scene"]["name"] == "Writer B"
