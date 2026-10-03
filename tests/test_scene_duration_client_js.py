"""The duration field refuses past the cap, and a past-end Reference end is not trimmed.

Plan "Backlog Cleanup Step 1", Phase 3, adopting "Scene Duration Upper Bound":

* The toolbar duration field parsed, assigned `totalFrames`, snapped and
  refreshed BEFORE anything could object, and its `max` was advisory only
  (`type="text"` in timecode mode silences it entirely). A value past the cap
  now leaves `totalFrames` alone, restores the field, raises one sourced
  toast and sends nothing. The timecode branch multiplies by fps, so the cap
  is applied to the converted frames.
* A template snap that lands above the cap steps down one grid unit, so a
  stored duration is never above it.
* The duration gesture refuses past the cap before it pushes Undo, and both
  the duration and fps gestures say why when the server refuses with
  `duration_limit`, reading `error.payload.code`, where the client keeps it.
* A right-edge trim of a Reference item starting at or past the scene end
  does not start (maintainer decision 2026-10-02): every end it could reach is
  inside the scene and so before the item's start. It used to save the end
  as `-1` and leave the item one frame long.

Each case drives the real `EditorWidget` prototype under Node; only the DOM,
the network and the render calls are stood in for.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WIDGET_URL = (ROOT / "web" / "js" / "editor_widget.js").as_uri()
NOTES_URL = (ROOT / "web" / "js" / "editor_notifications.js").as_uri()

SCRIPT = r"""
globalThis.document = { createElement: () => ({ style: {}, appendChild() {}, addEventListener() {} }),
  body: {}, activeElement: null, addEventListener() {}, removeEventListener() {}, querySelectorAll: () => [] };
globalThis.window = { addEventListener() {}, removeEventListener() {}, localStorage: null,
  comfyAPI: { api: { api: { apiURL: (path) => path } } }, prompt: () => null };
globalThis.localStorage = { getItem() { return null; }, setItem() {} };
globalThis.location = { href: "http://test/" };
globalThis.requestAnimationFrame = () => 0;

const { EditorWidget } = await import(__WIDGET__);
const notes = await import(__NOTES__);
const toasts = [];
const seen = new Set();
let live = [];
notes.subscribe((list) => { live = list; for (const n of list) {
  if (seen.has(n.id)) continue; seen.add(n.id);
  toasts.push({ tier: n.tier, message: n.message, source: n.source || "" }); } });

function widget({ duration = 100, timecode = false, constraint = null } = {}) {
  const w = Object.create(EditorWidget.prototype);
  const calls = [];
  const options = [];
  Object.defineProperty(w, "_effectiveFps", { value: 24 });
  Object.assign(w, {
    activeScene: { scene_id: "scene", duration_frames: duration, reference_items: [] },
    activeSceneId: "scene", projectDir: "project", totalFrames: duration,
    playhead: 0, selectionStart: 0, selectionEnd: 0,
    _timecodeMode: timecode ? "timecode" : "frames",
    durationInput: { value: "", type: "number", inputMode: "" },
    _durLabel: { textContent: "" },
    _getActiveFrameConstraint: () => constraint,
    _pushUndo: () => { calls.push("pushUndo"); return {}; },
    _updateSceneDuration: (frames) => { calls.push(`update:${frames}`); },
    _runSceneMutation: (ops, opts) => { calls.push("run"); options.push(opts); return Promise.resolve({}); },
    _renderTimeline: () => calls.push("render"),
    _renderViewportFrame() {}, _updateToolbar() {}, _updateTransportUI() {},
    _setWidgetValue() {}, _persistActiveTimelineSelection() {},
    _syncSceneFpsControl() {},
  });
  w._refreshDurationInput();
  return { w, calls, options };
}

const out = {};
async function scenario(name, fn) {
  const before = toasts.length;
  out[name] = await fn();
  out[name].toasts = toasts.slice(before);
  // The core coalesces a repeated source into one counted toast, so clear
  // them between scenarios or a later scenario's toast is folded into this one.
  for (const n of [...live]) notes.dismiss(n.id);
}

for (const [name, raw, opts] of [
  ["over_cap_frames", "15351532", {}],
  ["over_cap_timecode", "5000", { timecode: true }],   // 5000 s * 24 = 120000 frames
  ["at_cap", "99999", {}],
  ["below_cap", "250", {}],
  ["snap_steps_down", "99999", { constraint: { step: 8, offset: 1, min: 9 } }],
  ["timecode_within", "10", { timecode: true }],
]) {
  await scenario(name, async () => {
    const { w, calls } = widget(opts);
    w.durationInput.value = raw;
    const result = w._commitSceneDurationInput(raw);
    return { result, calls, totalFrames: w.totalFrames, field: w.durationInput.value };
  });
}

await scenario("gesture_backstop", async () => {
  const { w, calls } = widget();
  await w._updateSceneDurationWithinGesture(150000);
  return { calls, totalFrames: w.totalFrames, stored: w.activeScene.duration_frames };
});

await scenario("gesture_messages", async () => {
  const { w, options } = widget();
  await w._updateSceneDurationWithinGesture(500);
  const opts = options[0] || {};
  const refused = { status: 400, payload: { code: "duration_limit", error: "server words" } };
  const other = { status: 500, payload: { code: "boom" } };
  const read = (key, error) => (typeof opts[key] === "function" ? opts[key](error) : opts[key]) ?? null;
  return {
    message: read("failureMessage", refused), detail: read("failureDetail", refused),
    tier: read("failureTier", refused), otherMessage: read("failureMessage", other),
  };
});

await scenario("fps_messages", async () => {
  const { w, options } = widget();
  await w._updateSceneFpsWithinGesture(48);
  const opts = options[0] || {};
  const refused = { status: 400, payload: { code: "duration_limit" } };
  return {
    message: opts.failureMessage?.(refused) ?? null,
    detail: opts.failureDetail?.(refused) ?? null,
    tier: opts.failureTier?.(refused) ?? null,
  };
});

for (const [name, item, edge] of [
  ["trim_past_end_right", { start_frame: 120, end_frame: 160, lane_index: 0 }, "right"],
  ["trim_past_end_sentinel_right", { start_frame: 100, end_frame: -1, lane_index: 0 }, "right"],
  ["trim_past_end_left", { start_frame: 120, end_frame: 160, lane_index: 0 }, "left"],
  ["trim_straddling_right", { start_frame: 60, end_frame: 160, lane_index: 0 }, "right"],
  ["trim_last_frame_right", { start_frame: 99, end_frame: 160, lane_index: 0 }, "right"],
]) {
  await scenario(name, async () => {
    const { w } = widget();
    const refused = w._refusePastEndReferenceTrim({ type: "reference", id: "r", data: item, edge });
    return { refused };
  });
}

// The stored end for a range reaching the scene end, and the drag refusal.
{
  const { w } = widget();
  const a = { reference_item_id: "a", lane_index: 0, start_frame: 10, end_frame: 50 };
  const later = { reference_item_id: "b", lane_index: 0, start_frame: 150, end_frame: 170 };
  const other = { reference_item_id: "c", lane_index: 1, start_frame: 150, end_frame: 170 };
  w.activeScene.reference_items = [a, later, other];
  const beside = w._referenceStoredEnd(a, 10, 100);
  w.activeScene.reference_items = [a, other];
  const alone = w._referenceStoredEnd(a, 10, 100);
  const kept = w._referenceStoredEnd(a, 10, 100, { explicit: true });
  const inside = w._referenceStoredEnd(a, 10, 60);
  out.stored_end = { beside, alone, kept, inside };
}
{
  const { w } = widget();
  const ref = (id, start, end) => ({ type: "reference", id, data: { reference_item_id: id, lane_index: 0, start_frame: start, end_frame: end } });
  const clip = { type: "clip", id: "c", data: { clip_id: "c", timeline_start_frame: 0, timeline_end_frame: 10 } };
  out.drag_refusal = {
    alone_fits: w._pastEndReferenceDragRefusal([ref("r", 60, 150)]),
    alone_past_sentinel: w._pastEndReferenceDragRefusal([ref("r", 120, -1)]),
    with_clip: w._pastEndReferenceDragRefusal([ref("r", 60, 150), clip]),
    longer: w._pastEndReferenceDragRefusal([ref("r", 10, 180)]),
    inside_group: w._pastEndReferenceDragRefusal([ref("r", 10, 60), clip]),
    inside_sentinel: w._pastEndReferenceDragRefusal([ref("r", 10, -1)]),
  };
}

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def results():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the duration client tests")
    script = (SCRIPT.replace("__WIDGET__", json.dumps(WIDGET_URL))
              .replace("__NOTES__", json.dumps(NOTES_URL)))
    result = subprocess.run([node, "--input-type=module"], input=script,
                            capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _limit_toasts(case):
    return [toast for toast in case["toasts"] if toast["source"] == "scene-duration-limit"]


@pytest.mark.parametrize("name", ["over_cap_frames", "over_cap_timecode"])
def test_a_value_past_the_cap_is_refused_before_anything_changes(results, name):
    case = results[name]
    assert case["result"]["status"] == "refused"
    assert case["calls"] == [], "nothing may be sent or rendered"
    assert case["totalFrames"] == 100
    expected_field = "4.17" if name == "over_cap_timecode" else "100"
    assert str(case["field"]) == expected_field, "the field shows the kept duration"
    toasts = _limit_toasts(case)
    assert len(toasts) == 1 and toasts[0]["tier"] == "warning"
    assert "99999" in toasts[0]["message"]


def test_a_value_at_the_cap_is_written(results):
    case = results["at_cap"]
    assert case["result"] == {"status": "applied", "frames": 99999}
    assert case["calls"][0] == "update:99999"
    assert case["totalFrames"] == 99999
    assert _limit_toasts(case) == []


def test_an_ordinary_value_is_written_as_before(results):
    case = results["below_cap"]
    assert case["result"] == {"status": "applied", "frames": 250}
    assert case["calls"][0] == "update:250"


def test_a_snap_above_the_cap_steps_down_one_grid_unit(results):
    """99999 snaps to 100001 on an 8k+1 grid; one step down is 99993."""
    case = results["snap_steps_down"]
    assert case["result"] == {"status": "applied", "frames": 99993}
    assert case["calls"][0] == "update:99993"


def test_the_timecode_branch_converts_before_writing(results):
    case = results["timecode_within"]
    assert case["result"] == {"status": "applied", "frames": 240}


def test_the_gesture_refuses_past_the_cap_before_it_pushes_undo(results):
    case = results["gesture_backstop"]
    assert case["calls"] == []
    assert (case["totalFrames"], case["stored"]) == (100, 100)
    assert len(_limit_toasts(case)) == 1


def test_a_server_refusal_of_the_duration_says_why(results):
    case = results["gesture_messages"]
    assert case["tier"] == "warning"
    assert case["message"] == "Scene duration change refused."
    assert "99999" in case["detail"]
    assert case["otherMessage"] == "scene duration failed — timeline restored.", (
        "any other failure keeps the default sentence")


def test_a_server_refusal_of_an_fps_retime_says_why(results):
    case = results["fps_messages"]
    assert case["tier"] == "warning"
    assert "FPS" in case["message"]
    assert "99999" in (case["detail"] or "")


@pytest.mark.parametrize(("name", "refused"), [
    ("trim_past_end_right", True),
    ("trim_past_end_sentinel_right", True),
    ("trim_past_end_left", False),
    ("trim_straddling_right", False),
    ("trim_last_frame_right", False),
])
def test_only_a_right_trim_of_an_item_starting_past_the_end_is_refused(results, name, refused):
    case = results[name]
    assert case["refused"] is refused
    toasts = [toast for toast in case["toasts"] if toast["source"] == "reference-trim-past-end"]
    assert len(toasts) == (1 if refused else 0)


def test_a_range_reaching_the_scene_end_is_a_sentinel_only_where_that_stays_true(results):
    """A later row on the lane, left past the end by a shrink, keeps the end explicit."""
    assert results["stored_end"] == {"beside": 100, "alone": -1, "kept": 100, "inside": 60}


def test_a_drag_past_the_end_is_withheld_unless_the_item_is_alone_and_fits(results):
    refusal = results["drag_refusal"]
    assert refusal["alone_fits"] is None
    assert refusal["alone_past_sentinel"] is None
    assert refusal["inside_group"] is None and refusal["inside_sentinel"] is None
    assert "on its own" in refusal["with_clip"]
    assert "longer than the scene" in refusal["longer"]


def test_the_gestures_reach_these_decisions():
    """The call sites, pinned: removing one would leave the methods above tested
    and the gesture unguarded (audit, Phase 3 #4)."""
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    chrome = (ROOT / "web" / "js" / "editor_top_chrome.js").read_text(encoding="utf-8")
    trim_down = widget.split('this.dragType = "trimEdge";', 1)[0].rsplit("const edgeHit = this._hitTestEdge(", 1)[1]
    assert "this._refusePastEndReferenceTrim(edgeHit)" in trim_down
    assert "this._pastEndReferenceDragRefusal(this.selectedItems)" in widget
    assert 'this.dragType === "moveItem" && this._dragRefusal' in widget
    trim_move = widget.split('this.dragType === "trimEdge" && this._trimItem', 1)[1].split('this.dragType === "moveItem"', 1)[0]
    assert "this._referenceStoredEnd(item.data, item.origStart, nextEnd)" in trim_move
    preview = widget.split("    _previewReferenceDrag(frameDelta) {", 1)[1].split("\n    }\n", 1)[0]
    assert "this._referenceStoredEnd(" in preview and "? -1" not in preview
    move = widget.split("    async _moveItemToFrameWithinGesture(", 1)[1].split("\n    }\n", 1)[0]
    assert "this._pastEndReferenceDragRefusal([hit])" in move and "this._referenceStoredEnd(" in move
    assert "widget._commitSceneDurationInput(widget.durationInput.value)" in chrome
    assert "widget.totalFrames =" not in chrome.split("durationInput.addEventListener", 1)[1].split("});", 1)[0]
