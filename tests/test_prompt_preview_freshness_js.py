"""Prompt previews re-compile only when their inputs or certified reads moved.

`web/js/prompt_preview_freshness.js` keys a compile request by exactly what the
server takes from it, and `EditorWidget._planPromptPreviewBranches` decides per
branch (windowed, scene-wide) whether a held result or pending work already
answers (`plans/cut-read-fanout.md` §2). These tests pin:

1. **The mirror.** The candidate overlay the key reads is the server's.
2. **The key.** It ignores the precondition and non-overlay fields, and moves
   with every overlay and request input.
3. **The lifecycle**, through the real widget methods: a certified irrelevant
   edit neither marks stale nor requests; a flagged, uncertified or selection
   change re-runs exactly the branches it affects; identical intents join
   pending work; a superseded in-flight result never lands; a failure never
   answers.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from server import routes
from test_prompt_authoring_ux import _method_body

ROOT = Path(__file__).resolve().parents[1]
FRESHNESS = (ROOT / "web/js/prompt_preview_freshness.js").as_uri()
CLIENT = (ROOT / "web/js/api_client.js").as_uri()
COORDINATOR = (ROOT / "web/js/prompt_compile_coordinator.js").as_uri()


def _node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt preview freshness coverage")
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# 1-2. The mirror and the key
# ---------------------------------------------------------------------------

def test_the_candidate_overlay_matches_the_server():
    js = _node(f"""
const mod = await import({json.dumps(FRESHNESS)});
console.log(JSON.stringify({{ fields: [...mod.PROMPT_CANDIDATE_OVERLAY_FIELDS],
    aliases: mod.PROMPT_CANDIDATE_OVERLAY_ALIASES }}));
""")
    assert set(js["fields"]) == set(routes.PROMPT_CANDIDATE_OVERLAY_FIELDS)
    assert len(js["fields"]) == len(set(js["fields"]))
    assert js["aliases"] == routes.PROMPT_CANDIDATE_OVERLAY_ALIASES


def test_the_key_reads_what_the_server_reads_and_nothing_else():
    result = _node(f"""
const {{ promptCompileSemanticKey: key }} = await import({json.dumps(FRESHNESS)});
const body = {{
  base_modified_at: "v1", window_start: 0, window_end: 10, fps: 24, labels_on: false,
  channel_template: "t", prompt_semantic_unit_creates: [],
  scene: {{ prompt_sections: [{{ prompt_id: "p", channels: {{ a: "x" }} }}],
           duration_frames: 10, clips: [{{ clip_id: "c", muted: false }}], name: "S",
           video_lane_configs: [{{ locked: false }}] }},
}};
const base = key(body);
const clone = () => structuredClone(body);
const moved = (fn) => {{ const next = clone(); fn(next); return key(next) !== base; }};
console.log(JSON.stringify({{
  precondition: moved((b) => {{ b.base_modified_at = "v9"; }}),
  clips: moved((b) => {{ b.scene.clips[0].muted = true; }}),
  name: moved((b) => {{ b.scene.name = "T"; }}),
  lanes: moved((b) => {{ b.scene.video_lane_configs[0].locked = true; }}),
  sections: moved((b) => {{ b.scene.prompt_sections[0].channels.a = "y"; }}),
  duration: moved((b) => {{ b.scene.duration_frames = 11; }}),
  window: moved((b) => {{ b.window_end = 9; }}),
  fps: moved((b) => {{ b.fps = 30; }}),
  template: moved((b) => {{ b.channel_template = "u"; }}),
  creates: moved((b) => {{ b.prompt_semantic_unit_creates = [{{ type: "x" }}]; }}),
  alias: key({{ ...clone(), scene: {{ ...clone().scene, prompt_sections: undefined,
      sections: clone().scene.prompt_sections }} }}) === base,
  order: key({{ ...Object.fromEntries(Object.entries(clone()).reverse()),
      scene: Object.fromEntries(Object.entries(clone().scene).reverse()) }}) === base,
}}));
""")
    assert result == {
        "precondition": False, "clips": False, "name": False, "lanes": False,
        "sections": True, "duration": True, "window": True, "fps": True,
        "template": True, "creates": True, "alias": True, "order": True,
    }


def test_reference_lane_presentation_never_moves_the_key():
    """Only `hidden` reaches the compile from a Reference lane config.

    Mirrors `prompt_dependency_projection`; the server-side reverse test in
    `test_change_certificates.py` proves name, lock and colour never move it.
    """
    result = _node(f"""
const {{ promptCompileSemanticKey: key }} = await import({json.dumps(FRESHNESS)});
const body = (config) => ({{ window_start: 0, scene: {{ reference_lane_configs: [
  {{ name: "A", locked: false, color: "", hidden: false }}, config ] }} }});
const base = key(body({{ name: "B", locked: false, color: "", hidden: false }}));
console.log(JSON.stringify({{
  name: key(body({{ name: "Renamed", locked: false, color: "", hidden: false }})) !== base,
  lock: key(body({{ name: "B", locked: true, color: "#f00", hidden: false }})) !== base,
  hidden: key(body({{ name: "B", locked: false, color: "", hidden: true }})) !== base,
  count: key({{ window_start: 0, scene: {{ reference_lane_configs: [{{ hidden: false }}] }} }}) !== base,
}}));
""")
    assert result == {"name": False, "lock": False, "hidden": True, "count": True}


# ---------------------------------------------------------------------------
# 3. The lifecycle, through the real widget methods
# ---------------------------------------------------------------------------

LIFTED = (
    "_beginPromptScenePayload", "_landPromptScenePayload", "_promptSceneRequestBody",
    "_planPromptPreviewBranches", "_promptCompileKey", "_promptBranchAnswered",
    "_promptCarriedResultHolds",
    "_queuePromptPreviewPair", "_previewPromptContextCandidate",
    "_clearPromptStaleVisualTimerIfSettled", "_windowedPromptCandidate",
    "_promptScenePayload", "_failedPromptContextCandidate",
    "_promptProjectionSubset", "_promptCompileRequestBody",
)


def _lifted_methods() -> str:
    widget = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    bodies = []
    for name in LIFTED:
        for prefix in ("", "async "):
            marker = "\n    " + prefix + name + "("
            if marker in widget:
                start = widget.index(marker)
                bodies.append(prefix + _method_body(widget[start:], name))
                break
        else:
            raise AssertionError(f"method {name} not found")
    return "\n".join(bodies)


def _lifecycle(scenario: str) -> dict:
    """The real widget methods and the real coordinator; only the network is fake.

    Each physical request is recorded with the projections it asked for.
    `respond` answers every unanswered one with a record per projection, and
    `fail` picks which projections answer with a failure instead.
    """
    script = f"""
import {{ certifiedUnchanged, getProjectVersion, registerChangeCertificate,
         rememberProjectVersion }} from {json.dumps(CLIENT)};
import {{ pendingPromptWorkAnswers, promptCompileSemanticKey, promptResultAnswers }}
    from {json.dumps(FRESHNESS)};
import {{ createPromptCompileCoordinator }} from {json.dumps(COORDINATOR)};
const PROMPT_STALE_VISUAL_DELAY_MS = 300;
const templateFreezeValue = (value) => value;
const projectErrorMessage = (_error, fallback) => fallback;
const resolvePromptCandidateSelection = (start, end, duration) => (end > start
    ? {{ selectionStart: start, selectionEnd: end }} : {{ selectionStart: 0, selectionEnd: duration }});
const V = (n) => `2026-09-27T10:00:${{String(n).padStart(2, "0")}}.000000`;
const requests = [];
class Subject {{
{_lifted_methods()}
  constructor() {{
    this.activeSceneId = "scene";
    this.activeScene = {{ scene_id: "scene", duration_frames: 100,
      prompt_sections: [{{ prompt_id: "p", start_frame: 0, end_frame: 100, text: "A" }}],
      clips: [{{ clip_id: "c", muted: false }}] }};
    this.totalFrames = 100;
    this.selectionStart = 20; this.selectionEnd = 40;
    this._effectiveFps = 24;
    this._promptContextPreviewToken = 0;
    this._promptContextScenePayloadToken = 0;
    this._promptContextCandidateCache = null;
    this._promptContextScenePayloadCache = null;
    this._promptPanelHandle = {{}};
  }}
  _projectDirName() {{ return "project"; }}
  _selectionContextRange() {{ return {{ contextStart: this.selectionStart, contextEnd: this.selectionEnd }}; }}
  _channelTemplate() {{ return "template"; }}
  _contextFrameValue() {{ return 0; }}
  _getActiveFrameConstraint() {{ return null; }}
  _refreshInlinePromptProjections() {{}}
  _renderTimeline() {{}}
  _requestPromptPreviewPair(request) {{
    return new Promise((done, fail) => {{
      const row = {{ ...request, done, fail, answered: false, aborted: false }};
      request.signal?.addEventListener("abort", () => {{
        row.aborted = true; row.answered = true;
        fail(Object.assign(new Error("aborted"), {{ name: "AbortError" }}));
      }});
      requests.push(row);
    }});
  }}
}}
const tick = () => new Promise((resolve) => setTimeout(resolve, 5));
const textOf = (row) => row.body.scene.prompt_sections[0].text;
const respond = async (filter = () => true, fail = () => false) => {{
  for (const row of requests.filter((one) => !one.answered && filter(one))) {{
    row.answered = true;
    for (const name of row.projections) {{
      row.onRecord(name, fail(name, row) ? {{ response: {{ ok: false, status: 500 }},
        payload: {{ code: "boom" }} }} : {{
        response: {{ ok: true, status: 200 }},
        payload: {{ prompt: textOf(row), projection: name, section_window_states: [],
          candidate_base_modified_at: row.body.base_modified_at }} }});
    }}
    row.done();
  }}
  await tick();
}};
// Every projection asked for, request by request, in dispatch order.
const sent = () => requests.map((one) => one.projections.join("+"));
const cert = (base, next, over = {{}}) => registerChangeCertificate({{
  schema: 1, project_id: "project", scene_id: "scene", base_modified_at: V(base),
  modified_at: V(next), prompt: false, bridge: false, ...over }}, {{ projectId: "project" }});
const subject = new Subject();
const preview = async (patch = {{}}) => {{ subject._previewPromptContextCandidate(patch, 0); await tick(); }};
const state = () => ({{
  windowed: subject._promptContextCandidateCache && {{
    prompt: subject._promptContextCandidateCache.prompt,
    stale: !!subject._promptContextCandidateCache._stale,
    failed: !!subject._promptContextCandidateCache._failed,
    version: subject._promptContextCandidateCache._version || "" }},
  scene: subject._promptContextScenePayloadCache && {{
    stale: !!subject._promptContextScenePayloadCache._stale,
    version: subject._promptContextScenePayloadCache._version || "" }},
}});
const queued = () => subject._promptCompileCoordinator.debugState().lanes
  .flatMap((lane) => lane.trailing);
rememberProjectVersion("project", V(1));
await preview();
await respond();
const out = {{ first: {{ sent: sent(), state: state() }} }};
requests.length = 0;
{scenario}
console.log(JSON.stringify(out));
"""
    return _node(script)


def test_the_first_preview_compiles_both_branches_in_one_request_and_records_what_they_answer():
    out = _lifecycle("")
    assert out["first"]["sent"] == ["windowed+scene"]
    assert out["first"]["state"] == {
        "windowed": {"prompt": "A", "stale": False, "failed": False,
                     "version": "2026-09-27T10:00:01.000000"},
        "scene": {"stale": False, "version": "2026-09-27T10:00:01.000000"},
    }


def test_a_certified_irrelevant_edit_neither_marks_stale_nor_requests():
    out = _lifecycle("""
cert(1, 2);
rememberProjectVersion("project", V(2));
subject.activeScene = { ...structuredClone(subject.activeScene),
  clips: [{ clip_id: "c", muted: true }] };
await preview();
out.irrelevant = { sent: sent(), state: state(), timer: !!subject._promptContextPreviewTimer };
""")
    assert out["irrelevant"]["sent"] == []
    assert out["irrelevant"]["state"] == {
        "windowed": {"prompt": "A", "stale": False, "failed": False,
                     "version": "2026-09-27T10:00:02.000000"},
        "scene": {"stale": False, "version": "2026-09-27T10:00:02.000000"},
    }


def test_another_scenes_certified_edit_is_irrelevant_too():
    out = _lifecycle("""
cert(1, 2, { scene_id: "other", prompt: true, bridge: true });
rememberProjectVersion("project", V(2));
await preview();
out.other = sent();
""")
    assert out["other"] == []


def test_a_flagged_or_uncertified_version_re_runs_both_branches():
    out = _lifecycle("""
cert(1, 2, { prompt: true });
rememberProjectVersion("project", V(2));
await preview();
out.flagged = sent();
await respond();
requests.length = 0;
rememberProjectVersion("project", V(3));
await preview();
out.uncertified = sent();
""")
    assert out["flagged"] == ["windowed+scene"]
    assert out["uncertified"] == ["windowed+scene"]


def test_a_selection_change_re_runs_only_the_windowed_branch():
    out = _lifecycle("""
subject.selectionStart = 30; subject.selectionEnd = 60;
await preview();
out.selection = { sent: sent(), sceneStale: subject._promptContextScenePayloadCache._stale };
""")
    assert out["selection"] == {"sent": ["windowed"], "sceneStale": False}


def test_an_authoring_change_re_runs_both_and_marks_both_stale_at_once():
    out = _lifecycle("""
subject._previewPromptContextCandidate({ prompt_sections: [
  { prompt_id: "p", start_frame: 0, end_frame: 100, text: "B" }] }, 1000);
out.immediate = state();
""")
    assert out["immediate"]["windowed"]["stale"] is True
    assert out["immediate"]["scene"]["stale"] is True


def test_identical_intents_join_pending_work():
    out = _lifecycle("""
const patch = { prompt_sections: [{ prompt_id: "p", start_frame: 0, end_frame: 100, text: "B" }] };
subject._previewPromptContextCandidate(patch, 50);
subject._previewPromptContextCandidate(patch, 50);   // joins the scheduled work
await new Promise((resolve) => setTimeout(resolve, 80));
await preview(patch);                                  // joins the in-flight work
out.joined = sent();
await respond();
out.after = state().windowed.prompt;
""")
    assert out["joined"] == ["windowed+scene"]
    assert out["after"] == "B"


def test_a_superseded_in_flight_request_never_lands_and_keeps_its_lane():
    out = _lifecycle("""
await preview({ prompt_sections: [{ prompt_id: "p", start_frame: 0, end_frame: 100, text: "B" }] });
out.inFlight = sent();
// Back to "A". The held "A" result was marked stale when "B" was scheduled, so
// "A" is asked again -- and "B", still in flight, loses its authority. It keeps
// the lane until its response ends: the server cannot cancel it.
await preview();
out.during = { sent: sent(), aborted: requests[0].aborted, live: requests[0].isCurrent() };
await respond();               // "B" answers, and lands nowhere
out.afterB = { sent: sent(), windowed: state().windowed.prompt };
await respond();
out.after = state();
""")
    assert out["inFlight"] == ["windowed+scene"]
    assert out["during"] == {"sent": ["windowed+scene"], "aborted": False, "live": False}
    assert out["afterB"] == {"sent": ["windowed+scene", "windowed+scene"], "windowed": "A"}
    assert out["after"]["windowed"]["prompt"] == "A"
    assert out["after"]["windowed"]["stale"] is False


def test_a_failure_never_answers_a_later_identical_intent():
    out = _lifecycle("""
rememberProjectVersion("project", V(2));
await preview();
await respond(() => true, (name) => name === "windowed");
out.failed = state().windowed.failed;
out.sceneFresh = state().scene.stale === false;
requests.length = 0;
cert(2, 3);
rememberProjectVersion("project", V(3));
await preview();
out.retried = sent();
""")
    assert out["failed"] is True
    assert out["sceneFresh"] is True       # one branch failing leaves the other landed
    assert out["retried"] == ["windowed"]


# The scene-switch block of `_setActiveScene` for a real switch, verbatim in effect.
SWITCH_SCENE = """
const switchScene = (sceneId) => {
  subject.activeSceneId = sceneId;
  subject._promptContextCandidateCache = null;
  subject._promptContextScenePayloadCache = null;
  subject._promptContextScenePayloadToken = (subject._promptContextScenePayloadToken || 0) + 1;
  subject._promptContextPreviewToken = (subject._promptContextPreviewToken || 0) + 1;
  if (subject._promptContextPreviewTimer) clearTimeout(subject._promptContextPreviewTimer);
  subject._promptContextPreviewTimer = null;
};
"""


def test_the_scene_switch_block_is_the_one_the_widget_runs():
    widget = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    block = widget[widget.index("        if (!isSameScene) {"):]
    block = block[:block.index("this._selectionDraftAnchor = null;")]
    for line in ("this._promptContextScenePayloadToken =",
                 "this._promptContextPreviewToken = (this._promptContextPreviewToken || 0) + 1;"):
        assert line in block, line


def test_work_a_scene_switch_orphaned_is_never_joined():
    """Phase 3 audit #1: A (in flight) -> B (no consumers) -> A must ask again."""
    out = _lifecycle(SWITCH_SCENE + """
await preview({ prompt_sections: [{ prompt_id: "p", start_frame: 0, end_frame: 100, text: "B" }] });
out.inFlight = sent();
switchScene("other");          // the Prompt tool is closed there: no preview runs
await respond();               // the orphaned responses land nowhere
out.orphanLanded = state();
switchScene("scene");
requests.length = 0;
await preview({ prompt_sections: [{ prompt_id: "p", start_frame: 0, end_frame: 100, text: "B" }] });
out.afterReturn = sent();
await respond();
out.state = state();
""")
    assert out["inFlight"] == ["windowed+scene"]
    assert out["orphanLanded"] == {"windowed": None, "scene": None}
    assert out["afterReturn"] == ["windowed+scene"]
    assert out["state"]["windowed"]["prompt"] == "B"
    assert out["state"]["scene"] is not None


def test_carried_work_that_fails_leaves_its_re_request_to_answer():
    """Phase 3 audit #2 holds: work carried across a transition cannot strand a branch.

    The same request in flight at an older version is carried, not joined and
    not revoked. When it fails, its failure does not land; the re-request queued
    behind it is sent and answers.
    """
    out = _lifecycle("""
const patch = { prompt_sections: [{ prompt_id: "p", start_frame: 0, end_frame: 100, text: "B" }] };
await preview(patch);
out.first = sent();
cert(1, 2);
rememberProjectVersion("project", V(2));
await preview(patch);          // same key, one certified irrelevant transition later
out.carried = { sent: sent(), queued: queued(), live: requests[0].isCurrent() };
await respond(() => true, () => true);   // the carried request fails on both branches
out.afterFailure = { sent: sent(), failed: state().windowed.failed };
await respond();
out.state = state();
""")
    assert out["first"] == ["windowed+scene"]
    assert out["carried"] == {"sent": ["windowed+scene"], "queued": [["windowed", "scene"]],
                              "live": True}
    assert out["afterFailure"] == {"sent": ["windowed+scene", "windowed+scene"],
                                   "failed": False}
    assert out["state"]["windowed"] == {"prompt": "B", "stale": False, "failed": False,
                                        "version": "2026-09-27T10:00:02.000000"}


def test_carried_work_that_lands_satisfies_its_re_request_without_sending_it():
    out = _lifecycle("""
const patch = { prompt_sections: [{ prompt_id: "p", start_frame: 0, end_frame: 100, text: "B" }] };
await preview(patch);
cert(1, 2);
rememberProjectVersion("project", V(2));
await preview(patch);
await respond();               // the carried request lands at V1 ...
out.sent = sent();             // ... and the certificate proves it for V2
out.state = state();
out.pending = subject._promptPreviewPending;
requests.length = 0;
await preview(patch);
out.again = sent();
""")
    assert out["sent"] == ["windowed+scene"]
    assert out["state"]["windowed"] == {"prompt": "B", "stale": False, "failed": False,
                                        "version": "2026-09-27T10:00:01.000000"}
    assert out["state"]["scene"]["stale"] is False
    assert out["pending"] == {"windowed": None, "scene": None}
    assert out["again"] == []


def test_carried_work_across_an_unproven_transition_stays_dimmed_until_its_re_request():
    """Audit #1: the key cannot see project-level state, so a carried result sent
    before a transition lands only when that transition is certified away."""
    out = _lifecycle("""
const patch = { prompt_sections: [{ prompt_id: "p", start_frame: 0, end_frame: 100, text: "B" }] };
await preview(patch);
cert(1, 2, { prompt: true });  // e.g. an identity edit: same key, prompt read changed
rememberProjectVersion("project", V(2));
await preview(patch);
out.stale = state().windowed.stale;
await respond();               // the carried V1 result must not land as fresh
out.afterCarried = { sent: sent(), windowed: state().windowed, scene: state().scene };
await respond();
out.state = state();
""")
    assert out["stale"] is True
    assert out["afterCarried"]["sent"] == ["windowed+scene", "windowed+scene"]
    assert out["afterCarried"]["windowed"]["stale"] is True
    assert out["afterCarried"]["windowed"]["prompt"] == "A"
    assert out["afterCarried"]["scene"]["stale"] is True
    assert out["state"]["windowed"] == {"prompt": "B", "stale": False, "failed": False,
                                        "version": "2026-09-27T10:00:02.000000"}
    assert out["state"]["scene"] == {"stale": False, "version": "2026-09-27T10:00:02.000000"}


def test_a_scene_result_landing_during_a_selection_burst_removes_the_queued_scene_work():
    """§3: recalculate before dispatching trailing work."""
    out = _lifecycle("""
const patch = { prompt_sections: [{ prompt_id: "p", start_frame: 0, end_frame: 100, text: "B" }] };
await preview(patch);                                  // authoring: both branches in flight
cert(1, 2);                                            // a lane lock lands meanwhile
rememberProjectVersion("project", V(2));
subject.selectionStart = 30; subject.selectionEnd = 60;
await preview(patch);                                  // a new window; the scene work is carried
subject.selectionStart = 35; subject.selectionEnd = 65;
await preview(patch);
out.queued = queued();
await respond();                                       // the carried scene result lands
await respond();
out.sent = sent();
out.state = state();
""")
    assert out["queued"] == [["windowed", "scene"]]
    # The scene re-request was dropped as satisfied; only the newest window went out.
    assert out["sent"] == ["windowed+scene", "windowed"]
    assert out["state"]["scene"] == {"stale": False, "version": "2026-09-27T10:00:01.000000"}
    assert out["state"]["windowed"]["prompt"] == "B"
    assert out["state"]["windowed"]["stale"] is False


def test_the_hidden_narrowing_matches_the_server_projection():
    """Phase 3 audit #3: keys equal exactly when the server's prompt projections are.

    Derived from `change_certificates.prompt_dependency_projection` rather than
    hard-coded, so the day the compile reads another lane-config field, the
    server projection gains it and this fails until the JS key does too.
    """
    from server import change_certificates
    from server.timeline_state import LaneConfig, Scene
    variants = [
        {"name": "A", "color": "", "locked": False, "hidden": False},
        {"name": "B", "color": "", "locked": False, "hidden": False},
        {"name": "A", "color": "#f00", "locked": True, "hidden": False},
        {"name": "A", "color": "", "locked": False, "hidden": True},
        {"name": "Z", "color": "#0f0", "locked": True, "hidden": True},
    ]

    # One scene, only its lane config varied: a fresh Scene mints random ids.
    scene = Scene(reference_lane_count=1, reference_lane_configs=[LaneConfig()])

    def projection(config):
        scene.reference_lane_configs = [LaneConfig(**config)]
        return change_certificates.prompt_dependency_projection(scene)

    lane_keys = {key for key in projection(variants[0]) if key.startswith("reference_lane")}
    assert lane_keys == {"reference_lane_count", "reference_lane_hidden",
                         "reference_lane_recipes"}, lane_keys
    server = [[projection(a) == projection(b) for b in variants] for a in variants]
    js = _node(f"""
const {{ promptCompileSemanticKey: key }} = await import({json.dumps(FRESHNESS)});
const variants = {json.dumps(variants)};
const keyOf = (config) => key({{ scene: {{ reference_lane_configs: [config] }} }});
console.log(JSON.stringify(variants.map((a) => variants.map((b) => keyOf(a) === keyOf(b)))));
""")
    assert js == server
