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
    "_previewPromptContextScenePayload", "_planPromptPreviewBranches", "_promptCompileKey",
    "_previewPromptContextCandidate", "_clearPromptStaleVisualTimerIfSettled",
    "_windowedPromptCandidate", "_promptScenePayload", "_failedPromptContextCandidate",
    "_promptProjectionSubset", "_promptCompileRequestBody",
)


def _lifted_methods() -> str:
    widget = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    bodies = []
    for name in LIFTED:
        start = widget.index("\n    " + name + "(")
        bodies.append(_method_body(widget[start:], name))
    return "\n".join(bodies)


def _lifecycle(scenario: str) -> dict:
    script = f"""
import {{ certifiedUnchanged, getProjectVersion, registerChangeCertificate,
         rememberProjectVersion }} from {json.dumps(CLIENT)};
import {{ pendingPromptWorkAnswers, promptCompileSemanticKey, promptResultAnswers }}
    from {json.dumps(FRESHNESS)};
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
  _queuePromptContextCompile(purpose, dirName, sceneId, body, isCurrent) {{
    return new Promise((resolve) => requests.push({{ purpose, body, isCurrent, resolve,
      answered: false }}));
  }}
}}
const tick = () => new Promise((resolve) => setTimeout(resolve, 5));
const respond = async (filter = () => true, fail = false) => {{
  for (const request of requests.filter((one) => !one.answered && filter(one))) {{
    request.answered = true;
    request.resolve(fail ? {{ response: {{ ok: false, status: 500 }}, payload: null }} : {{
      response: {{ ok: true, status: 200 }},
      payload: {{ prompt: request.body.scene.prompt_sections[0].text,
        purpose: request.purpose, section_window_states: [],
        candidate_base_modified_at: request.body.base_modified_at }} }});
  }}
  await tick();
}};
const sent = () => requests.map((one) => one.purpose);
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
rememberProjectVersion("project", V(1));
await preview();
await respond();
const out = {{ first: {{ sent: sent(), state: state() }} }};
requests.length = 0;
{scenario}
console.log(JSON.stringify(out));
"""
    return _node(script)


def test_the_first_preview_compiles_both_branches_and_records_what_they_answer():
    out = _lifecycle("")
    assert sorted(out["first"]["sent"]) == ["scene-projection", "windowed-preview"]
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
out.flagged = sent().sort();
await respond();
requests.length = 0;
rememberProjectVersion("project", V(3));
await preview();
out.uncertified = sent().sort();
""")
    assert out["flagged"] == ["scene-projection", "windowed-preview"]
    assert out["uncertified"] == ["scene-projection", "windowed-preview"]


def test_a_selection_change_re_runs_only_the_windowed_branch():
    out = _lifecycle("""
subject.selectionStart = 30; subject.selectionEnd = 60;
await preview();
out.selection = { sent: sent(), sceneStale: subject._promptContextScenePayloadCache._stale };
""")
    assert out["selection"] == {"sent": ["windowed-preview"], "sceneStale": False}


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
out.joined = sent().sort();
await respond();
out.after = state().windowed.prompt;
""")
    assert out["joined"] == ["scene-projection", "windowed-preview"]
    assert out["after"] == "B"


def test_a_superseded_in_flight_result_never_lands():
    out = _lifecycle("""
await preview({ prompt_sections: [{ prompt_id: "p", start_frame: 0, end_frame: 100, text: "B" }] });
out.inFlight = sent().sort();
// Back to "A". The held "A" result was marked stale when "B" was scheduled, so
// "A" is asked again -- and "B", still in flight, must lose its authority.
await preview();
const textOf = (one) => one.body.scene.prompt_sections[0].text;
out.revoked = requests.map((one) => [one.purpose, textOf(one), one.isCurrent()]).sort();
await respond();
out.after = state();
""")
    assert out["inFlight"] == ["scene-projection", "windowed-preview"]
    assert out["revoked"] == [
        ["scene-projection", "A", True], ["scene-projection", "B", False],
        ["windowed-preview", "A", True], ["windowed-preview", "B", False]]
    assert out["after"]["windowed"]["prompt"] == "A"


def test_a_failure_never_answers_a_later_identical_intent():
    out = _lifecycle("""
rememberProjectVersion("project", V(2));
await preview();
await respond((one) => one.purpose === "windowed-preview", true);
await respond();
out.failed = state().windowed.failed;
requests.length = 0;
cert(2, 3);
rememberProjectVersion("project", V(3));
await preview();
out.retried = sent();
""")
    assert out["failed"] is True
    assert out["retried"] == ["windowed-preview"]


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


def test_work_a_scene_switch_orphaned_is_never_joined():
    """Phase 3 audit #1: A (in flight) -> B (no consumers) -> A must ask again."""
    out = _lifecycle(SWITCH_SCENE + """
await preview({ prompt_sections: [{ prompt_id: "p", start_frame: 0, end_frame: 100, text: "B" }] });
out.inFlight = sent().sort();
switchScene("other");          // the Prompt tool is closed there: no preview runs
await respond();               // the orphaned responses land nowhere
switchScene("scene");
requests.length = 0;
await preview({ prompt_sections: [{ prompt_id: "p", start_frame: 0, end_frame: 100, text: "B" }] });
out.afterReturn = sent().sort();
await respond();
out.state = state();
""")
    assert out["inFlight"] == ["scene-projection", "windowed-preview"]
    assert out["afterReturn"] == ["scene-projection", "windowed-preview"]
    assert out["state"]["windowed"]["prompt"] == "B"
    assert out["state"]["scene"] is not None


def test_in_flight_work_is_not_joined_across_a_version_transition():
    """Phase 3 audit #2: joined work that then fails must not strand the branch.

    Once a transition lands, in-flight work sent at the older version is
    re-sent rather than joined, so its failure cannot leave every later intent
    waiting on it.
    """
    out = _lifecycle("""
const patch = { prompt_sections: [{ prompt_id: "p", start_frame: 0, end_frame: 100, text: "B" }] };
await preview(patch);
out.first = sent().sort();
cert(1, 2);
rememberProjectVersion("project", V(2));
await preview(patch);          // same key, one certified irrelevant transition later
out.resent = sent().sort();
const stale = requests.filter((one) => one.body.base_modified_at === V(1));
out.oldRevoked = stale.every((one) => !one.isCurrent());
await respond((one) => one.body.base_modified_at === V(1), true);   // the old ones fail
await respond();
out.state = state();
""")
    assert out["first"] == ["scene-projection", "windowed-preview"]
    assert out["resent"] == ["scene-projection", "scene-projection",
                             "windowed-preview", "windowed-preview"]
    assert out["oldRevoked"] is True
    assert out["state"]["windowed"] == {"prompt": "B", "stale": False, "failed": False,
                                        "version": "2026-09-27T10:00:02.000000"}


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
