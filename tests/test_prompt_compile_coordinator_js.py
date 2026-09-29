"""The paired prompt-preview coordinator (`plans/cut-read-fanout.md` §3).

One physical request per project/scene lane, carrying up to two branches that
settle independently; at most one queued request per branch; recalculation
before dispatch; abort when nothing can use the answer.
"""

import json
import os
import shutil
import subprocess

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "file:///" + os.path.join(ROOT, "web", "js", "prompt_compile_coordinator.js").replace(os.sep, "/")


def _run_node(body):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt compile coordinator coverage")
    script = f"""
import {{createPromptCompileCoordinator, PROMPT_BRANCH_SATISFIED}} from {json.dumps(URL)};
const physical = [];
const coordinator = createPromptCompileCoordinator((request) => new Promise((done, fail) => {{
  const row = {{ ...request, done, fail, aborted: false }};
  request.signal?.addEventListener("abort", () => {{
    row.aborted = true; fail(Object.assign(new Error("aborted"), {{ name: "AbortError" }}));
  }});
  physical.push(row);
}}));
const flush = async () => {{ for (let i = 0; i < 6; i++) await Promise.resolve(); }};
const ok = (value) => ({{ response: {{ ok: true, status: 200 }}, payload: {{ value }} }});
const bad = (status) => ({{ response: {{ ok: false, status }}, payload: {{ code: "x" }} }});
const spec = (key, over = {{}}) => ({{ key, isCurrent: () => true,
  answers: (body) => body.key === key || body[key] === true, ...over }});
const settled = (promise) => promise.then((value) => ({{ value }}), (error) => ({{ error: error.message }}));
{body}
"""
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_one_physical_request_carries_both_branches_and_each_settles_on_its_record():
    result = _run_node("""
const out = coordinator.schedule({ projectId: "p", sceneId: "s", body: { key: "w" },
  branches: { windowed: spec("w"), scene: spec("s") } });
await flush();
const sent = physical.map((row) => row.projections);
physical[0].onRecord("windowed", ok("W"));
const windowed = await out.windowed;          // readable before the scene record
physical[0].onRecord("scene", ok("S"));
physical[0].onRecord("scene", ok("DUPLICATE"));   // one record per projection
physical[0].done();
const scene = await out.scene;
await flush();
console.log(JSON.stringify({ sent, windowed: windowed.payload.value,
  scene: scene.payload.value, state: coordinator.debugState() }));
""")
    assert result == {"sent": [["windowed", "scene"]], "windowed": "W", "scene": "S",
                      "state": {"disposed": False, "lanes": []}}


def test_a_missing_record_or_a_broken_stream_fails_only_unsettled_branches():
    result = _run_node("""
const a = coordinator.schedule({ projectId: "p", sceneId: "s", body: {},
  branches: { windowed: spec("w"), scene: spec("s") } });
await flush();
physical[0].onRecord("windowed", ok("W"));
physical[0].done();                                // EOF without a scene record
const b = coordinator.schedule({ projectId: "p", sceneId: "t", body: {},
  branches: { windowed: spec("w"), scene: spec("s") } });
await flush();
physical[1].onRecord("windowed", ok("W2"));
physical[1].fail(new Error("stream broke"));       // interrupted after one record
console.log(JSON.stringify({
  eof: [await settled(a.windowed), await settled(a.scene)],
  broken: [await settled(b.windowed), await settled(b.scene)] }));
""")
    assert result["eof"][0]["value"]["payload"]["value"] == "W"
    assert result["eof"][1] == {"error": "The prompt preview ended without a result."}
    assert result["broken"][0]["value"]["payload"]["value"] == "W2"
    assert result["broken"][1] == {"error": "stream broke"}


def test_a_newer_branch_supersedes_only_its_own_name_and_queues_one_request_per_branch():
    result = _run_node("""
const first = coordinator.schedule({ projectId: "p", sceneId: "s", body: { key: "w1" },
  branches: { windowed: spec("w1"), scene: spec("s1") } });
await flush();
// Selection burst: newer windowed intents, the scene branch unchanged.
const second = coordinator.schedule({ projectId: "p", sceneId: "s", body: { key: "w2" },
  branches: { windowed: spec("w2") } });
const third = coordinator.schedule({ projectId: "p", sceneId: "s", body: { key: "w3" },
  branches: { windowed: spec("w3") } });
await flush();
const during = { state: coordinator.debugState(), aborted: physical[0].aborted,
  firstWindowedLive: physical[0].isCurrent() };
physical[0].onRecord("windowed", ok("W1"));        // superseded: never applies
physical[0].onRecord("scene", ok("S1"));           // still owned: applies
physical[0].done();
await flush();
physical[1].onRecord("windowed", ok("W3"));
physical[1].done();
console.log(JSON.stringify({ during,
  sent: physical.map((row) => [row.projections, row.body.key]),
  values: [await first.windowed, (await first.scene).payload.value,
           await second.windowed, (await third.windowed).payload.value] }));
""")
    assert result["during"]["aborted"] is False
    assert result["during"]["firstWindowedLive"] is True   # its scene branch still is
    assert result["during"]["state"]["lanes"][0]["trailing"] == [["windowed"]]
    assert result["sent"] == [[["windowed", "scene"], "w1"], [["windowed"], "w3"]]
    assert result["values"] == [None, "S1", None, "W3"]


def test_a_queued_branch_folds_into_a_newer_body_only_when_it_answers_for_it():
    result = _run_node("""
coordinator.schedule({ projectId: "p", sceneId: "s", body: { key: "x" },
  branches: { windowed: spec("x") } });
await flush();
// Queued scene work, then a windowed intent whose body it answers for: one request.
coordinator.schedule({ projectId: "p", sceneId: "s", body: { key: "a", s1: true },
  branches: { scene: spec("s1") } });
coordinator.schedule({ projectId: "p", sceneId: "s", body: { key: "b", s1: true },
  branches: { windowed: spec("b") } });
const folded = coordinator.debugState().lanes[0].trailing;
// A windowed intent whose body the queued scene work does NOT answer for:
coordinator.schedule({ projectId: "p", sceneId: "t", body: { key: "x" },
  branches: { windowed: spec("x") } });
await flush();
coordinator.schedule({ projectId: "p", sceneId: "t", body: { key: "a", s1: true },
  branches: { scene: spec("s1") } });
coordinator.schedule({ projectId: "p", sceneId: "t", body: { key: "c" },
  branches: { windowed: spec("c") } });
const separate = coordinator.debugState().lanes[1].trailing;
console.log(JSON.stringify({ folded, separate }));
""")
    assert result["folded"] == [["windowed", "scene"]]
    assert result["separate"] == [["scene"], ["windowed"]]


def test_trailing_work_a_landed_result_already_answers_is_never_sent():
    result = _run_node("""
let answered = false;
coordinator.schedule({ projectId: "p", sceneId: "s", body: { key: "w" },
  branches: { windowed: spec("w") } });
await flush();
const trailing = coordinator.schedule({ projectId: "p", sceneId: "s", body: { key: "w" },
  branches: { windowed: spec("w", { stillNeeded: () => !answered }) } });
answered = true;                                   // the active result landed
physical[0].onRecord("windowed", ok("W"));
physical[0].done();
const value = await trailing.windowed;
await flush();
console.log(JSON.stringify({ requests: physical.length,
  satisfied: value === PROMPT_BRANCH_SATISFIED }));
""")
    assert result == {"requests": 1, "satisfied": True}


def test_carried_work_lands_only_a_success_and_leaves_failure_to_its_re_request():
    result = _run_node("""
const carried = coordinator.schedule({ projectId: "p", sceneId: "s", body: { key: "w" },
  branches: { windowed: spec("w") } });
await flush();
// Same key again (a certified transition later): carried, not revoked.
const again = coordinator.schedule({ projectId: "p", sceneId: "s", body: { key: "w" },
  branches: { windowed: spec("w") } });
await flush();
const live = physical[0].isCurrent();
physical[0].onRecord("windowed", bad(409));        // its failure is not the last word
physical[0].done();
await flush();
physical[1].onRecord("windowed", ok("retried"));
physical[1].done();
console.log(JSON.stringify({ live, carried: await carried.windowed,
  again: (await again.windowed).payload.value, requests: physical.length }));
""")
    assert result == {"live": True, "carried": None, "again": "retried", "requests": 2}


def test_a_superseded_request_keeps_its_lane_until_its_response_ends():
    """Audit #2: the server cannot cancel a running compile, so freeing the lane
    early would only run the next request beside it."""
    result = _run_node("""
const first = coordinator.schedule({ projectId: "p", sceneId: "s", body: { key: "a" },
  branches: { windowed: spec("a"), scene: spec("sa") } });
await flush();
const second = coordinator.schedule({ projectId: "p", sceneId: "s", body: { key: "b" },
  branches: { windowed: spec("b"), scene: spec("sb") } });
await flush();
const during = { requests: physical.length, aborted: physical[0].aborted,
  live: physical[0].isCurrent() };
physical[0].onRecord("windowed", ok("A"));
physical[0].onRecord("scene", ok("SA"));
physical[0].done();
await flush();
const afterFirst = { requests: physical.length, aborted: physical[0].aborted };
physical[1].onRecord("windowed", ok("B"));
physical[1].onRecord("scene", ok("SB"));
physical[1].done();
console.log(JSON.stringify({ during, afterFirst,
  values: [await first.windowed, await first.scene,
           (await second.windowed).payload.value, (await second.scene).payload.value] }));
""")
    assert result == {"during": {"requests": 1, "aborted": False, "live": False},
                      "afterFirst": {"requests": 2, "aborted": False},
                      "values": [None, None, "B", "SB"]}


def test_a_carried_success_lands_only_when_the_host_accepts_its_version():
    """Audit #1: a carried request was sent before a transition its key cannot see."""
    result = _run_node("""
let accept = false;
const carried = coordinator.schedule({ projectId: "p", sceneId: "s", body: { key: "w" },
  branches: { windowed: spec("w", { acceptsCarried: () => accept }) } });
await flush();
coordinator.schedule({ projectId: "p", sceneId: "s", body: { key: "w" },
  branches: { windowed: spec("w") } });
physical[0].onRecord("windowed", ok("stale"));      // refused: its version is unproven
physical[0].done();
await flush();
const refused = await carried.windowed;
const again = coordinator.schedule({ projectId: "p", sceneId: "t", body: { key: "w" },
  branches: { windowed: spec("w", { acceptsCarried: () => accept }) } });
await flush();
coordinator.schedule({ projectId: "p", sceneId: "t", body: { key: "w" },
  branches: { windowed: spec("w") } });
accept = true;
physical[2].onRecord("windowed", ok("proven"));
physical[2].done();
console.log(JSON.stringify({ refused, accepted: (await again.windowed).payload.value }));
""")
    assert result == {"refused": None, "accepted": "proven"}


def test_dispose_revokes_active_application_and_drops_undispatched_trailing():
    result = _run_node("""
const first = coordinator.schedule({ projectId: "p", sceneId: "s", body: { key: "a" },
  branches: { windowed: spec("a") } });
await flush();
const trailing = coordinator.schedule({ projectId: "p", sceneId: "s", body: { key: "b" },
  branches: { windowed: spec("b") } });
coordinator.dispose();
const late = coordinator.schedule({ projectId: "p", sceneId: "s", body: {},
  branches: { windowed: spec("c") } });
await flush();
console.log(JSON.stringify({ dispatches: physical.length, aborted: physical[0].aborted,
  values: [await first.windowed, await trailing.windowed, await late.windowed],
  state: coordinator.debugState() }));
""")
    assert result["dispatches"] == 1
    assert result["aborted"] is True
    assert result["values"] == [None, None, None]
    assert result["state"] == {"disposed": True, "lanes": []}
