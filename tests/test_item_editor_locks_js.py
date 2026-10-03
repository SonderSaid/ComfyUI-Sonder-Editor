"""The item editor on a locked lane, and its Start field (backlog step 1, Phase 4).

Plan "Backlog Cleanup Step 1", Phase 4 (D6, #125):

* **Read-only on a locked item.** `_showItemEditor` read no lock. Locked, every
  value control and Apply now render disabled under "(locked) Unlock the lane
  to edit.", Delete reads "(locked)", and Lane Setup stays (the panel has its
  own lock handling).
* **It follows the lock.** The editor re-renders whenever the lock its item
  derives changes, whichever writer changed it: a rebuilt layout (Undo, a
  scenes read) or the header toggle's in-place write.
* **Backstops before paint.** `_moveItemToFrameWithinGesture` and
  `_updateItemPropertyWithinGesture` refuse an item on a locked lane, and
  discard the Undo entry their caller pushed. The mute, Fit, Crop and
  opacity handlers refuse before they touch `data`. An unlocked item whose
  linked partner is locked stays editable.
* **Start (#125).** A negative or unreadable value was dropped in silence. It
  now refuses with a message and restores the field; frames mode rounds
  half-up. The guide Frame field shares the parse.

Every case drives the real `EditorWidget` prototype under Node over a small
fake DOM; only the canvas paint, the network and Undo storage are stood in for.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WIDGET_URL = (ROOT / "web" / "js" / "editor_widget.js").as_uri()
NOTES_URL = (ROOT / "web" / "js" / "editor_notifications.js").as_uri()

SCENE = {
    "scene_id": "scene", "duration_frames": 100,
    "video_lane_count": 2, "audio_lane_count": 2, "reference_lane_count": 2,
    "motion_driver_lane_count": 1,
    "video_lane_configs": [{"locked": False}, {"locked": True}],
    "audio_lane_configs": [{"locked": False}, {"locked": True}],
    "reference_lane_configs": [{"locked": False}, {"locked": True}],
    "motion_driver_lane_configs": [{"locked": False}],
    "guide_track_config": {"locked": False},
    "clips": [
        {"clip_id": "c0", "track_index": 0, "role": "render", "source_path": "media/v.mp4",
         "timeline_start_frame": 10, "timeline_end_frame": 40, "opacity": 1.0},
        {"clip_id": "c1", "track_index": 1, "role": "render", "source_path": "media/v.mp4",
         "timeline_start_frame": 10, "timeline_end_frame": 40, "opacity": 1.0},
    ],
    "audio_tracks": [
        {"track_id": "a0", "lane_index": 0, "source_path": "media/a.wav",
         "timeline_start_frame": 10, "timeline_end_frame": 40},
        {"track_id": "a1", "lane_index": 1, "source_path": "media/a.wav",
         "timeline_start_frame": 10, "timeline_end_frame": 40},
    ],
    "reference_items": [
        {"reference_item_id": "r0", "lane_index": 0, "start_frame": 0, "end_frame": 30},
        {"reference_item_id": "r1", "lane_index": 1, "start_frame": 0, "end_frame": 30},
    ],
    "guide_frames": [{"guide_id": "g1", "frame_index": 20, "asset_id": "img"}],
    "prompt_sections": [{"prompt_id": "p0", "start_frame": 0, "end_frame": 50, "muted": False}],
    # c0 (unlocked) is linked to a1 (locked audio lane 1).
    "linked_item_groups": [{"group_id": "grp", "items": [
        {"type": "clip", "id": "c0"}, {"type": "audio", "id": "a1"}]}],
}

SCRIPT = r"""
class N {
  constructor(tag) { this.tagName = String(tag).toUpperCase(); this.children = []; this.style = {};
    this.dataset = {}; this._handlers = {}; this.textContent = ""; this.value = ""; this.disabled = false;
    this.parentElement = null; }
  appendChild(c) { this.children.push(c); c.parentElement = this; return c; }
  append(...cs) { cs.forEach((c) => this.appendChild(c)); }
  insertBefore(c) { return this.appendChild(c); }
  addEventListener(t, h) { (this._handlers[t] ||= []).push(h); }
  removeEventListener() {}
  setAttribute(k, v) { this[`attr:${k}`] = v; } getAttribute(k) { return this[`attr:${k}`] ?? null; }
  querySelectorAll() { return []; } querySelector() { return null; }
  remove() { const p = this.parentElement; if (p) p.children = p.children.filter((c) => c !== this); }
  fire(t, ev = {}) { for (const h of this._handlers[t] || []) h({ key: "", stopPropagation() {}, preventDefault() {}, ...ev }); }
}
globalThis.document = { createElement: (t) => new N(t), body: new N("body"), activeElement: null,
  addEventListener() {}, removeEventListener() {}, querySelectorAll: () => [] };
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
  toasts.push({ message: n.message, source: n.source || "" }); } });

function widget({ timecode = false } = {}) {
  const w = Object.create(EditorWidget.prototype);
  const calls = [];
  const undoStack = [];
  const parent = new N("div");
  Object.defineProperty(w, "_effectiveFps", { value: 24 });
  Object.assign(w, {
    activeScene: structuredClone(__SCENE__), activeSceneId: "scene", projectDir: "project",
    totalFrames: 100, assets: { video: [], image: [], audio: [] }, _pathToAsset: {},
    selectedItems: [], selectedItem: null, _selectedLanes: [],
    _timecodeMode: timecode ? "timecode" : "frames", isFullscreen: false,
    timelineCanvas: { parentElement: parent, nextSibling: null },
    _paintTimelineFrame: () => { calls.push("paint"); return null; },
    _renderViewportFrame() {}, _updateToolbar() {}, _renderSceneAfterLocalMutation() {},
    _getGuideAsset: () => null,
    _pushUndo(label) { const entry = { label }; undoStack.push(entry);
      this._historyPostSnapshotCaptureCandidate = entry; calls.push(`undo:${label}`); return entry; },
    _discardUndoEntry(entry) { const i = undoStack.indexOf(entry); if (i < 0) return false;
      undoStack.splice(i, 1); calls.push("discardUndo"); return true; },
    _runSceneMutation: (ops) => { calls.push(`run:${ops.map((o) => o.type).join(",")}`); return Promise.resolve({}); },
    _moveItemToFrame: (type, id, data, frame) => { calls.push(`move:${type}:${frame}`); },
    _moveGuideToFrame: (data, frame) => { calls.push(`guide-move:${frame}`); },
  });
  w._buildTrackLayout();
  return { w, calls, undoStack, parent };
}

function select(w, type, id) {
  const hit = w._findSceneItemBySelection(type, id);
  w.selectedItem = hit; w.selectedItems = [hit];
  w._showItemEditor();
  return hit;
}

function describe(w) {
  const el = w._itemEditorEl;
  if (!el) return null;
  return el.children.map((c) => ({ tag: c.tagName, text: c.textContent, disabled: !!c.disabled,
    hover: c.dataset?.sonderHoverVariant || null, base: c.dataset?.sonderBaseVariant || null,
    opacity: c.style?.opacity || "", title: c.title || "" }));
}

const find = (w, pred) => w._itemEditorEl.children.find(pred);
const byText = (w, text) => find(w, (c) => c.tagName === "BUTTON" && c.textContent === text);
const startInput = (w) => find(w, (c) => c.tagName === "INPUT" && c.inputMode === "decimal");
const laneEntry = (w, type, lane) => w._trackLayout.find((e) => e.type === type && e.laneIndex === lane);

const out = {};
async function scenario(name, fn) {
  const before = toasts.length;
  out[name] = await fn();
  out[name].toasts = toasts.slice(before).map((t) => t.source);
  for (const n of [...live]) notes.dismiss(n.id);
}

for (const [name, type, id] of [
  ["locked_reference", "reference", "r1"], ["locked_clip", "clip", "c1"],
  ["locked_audio", "audio", "a1"], ["unlocked_clip", "clip", "c0"],
]) {
  await scenario(`render_${name}`, async () => {
    const { w } = widget();
    select(w, type, id);
    return { controls: describe(w) };
  });
}

await scenario("guide_track_locked", async () => {
  const { w } = widget();
  w.activeScene.guide_track_config.locked = true;
  w._buildTrackLayout();
  select(w, "guide", 20);
  return { controls: describe(w) };
});

// A lock arriving through a rebuilt layout (Undo, a scenes read), then an
// unlock through the header toggle's in-place write.
await scenario("follows_the_lock", async () => {
  const { w } = widget();
  select(w, "clip", "c0");
  const opened = describe(w).some((c) => c.disabled);
  w.activeScene.video_lane_configs[0].locked = true;
  w._buildTrackLayout();
  w._renderTimeline();
  const afterRebuild = { note: describe(w).some((c) => c.text.startsWith("(locked)")),
    applyDisabled: !!byText(w, "Apply")?.disabled };
  laneEntry(w, "video", 0).locked = false;
  w._renderTimeline();
  const afterHeader = { note: describe(w).some((c) => c.text.startsWith("(locked)")),
    anyDisabled: describe(w).some((c) => c.disabled) };
  return { opened, afterRebuild, afterHeader };
});

// The editor was built unlocked; the lock lands in place before any render.
const lockInPlace = (w, type) => {
  if (type === "guide") { w._trackLayout.find((e) => e.type === "guides").locked = true; return; }
  laneEntry(w, type === "clip" ? "video" : type, 0).locked = true;
};
for (const [name, type, id, act] of [
  ["clip_mute", "clip", "c0", (w) => byText(w, "Visible").fire("click")],
  ["fit", "clip", "c0", (w) => { const s = find(w, (c) => c.tagName === "SELECT"); s.value = "cover"; s.fire("change"); }],
  ["opacity", "clip", "c0", (w) => { const r = find(w, (c) => c.type === "range"); r.value = 30; r.fire("input"); r.fire("change"); }],
  ["start_apply", "clip", "c0", (w) => { startInput(w).value = "50"; byText(w, "Apply").fire("click"); }],
  ["reference_mute", "reference", "r0", (w) => byText(w, "Active").fire("click")],
  ["reference_strength", "reference", "r0", (w) => { const i = find(w, (c) => c.tagName === "INPUT" && c.step === "0.05"); i.value = "0.3"; i.fire("change"); }],
  ["audio_mute", "audio", "a0", (w) => find(w, (c) => c.tagName === "BUTTON" && /🔊|🔇/u.test(c.textContent)).fire("click")],
  ["audio_volume", "audio", "a0", (w) => { const r = find(w, (c) => c.type === "range"); r.value = 30; r.fire("input"); r.fire("change"); }],
  ["guide_mute", "guide", 20, (w) => byText(w, "Visible").fire("click")],
]) {
  await scenario(`inplace_${name}`, async () => {
    const { w, calls, undoStack } = widget();
    const hit = select(w, type, id);
    const before = JSON.stringify(hit.data);
    lockInPlace(w, type);
    calls.length = 0;
    act(w);
    await new Promise((r) => setTimeout(r, 0));
    return { dataUnchanged: JSON.stringify(hit.data) === before, calls: calls.filter((c) => c !== "paint"),
      undo: undoStack.length, rerenderedLocked: describe(w).some((c) => c.text.startsWith("(locked)")) };
  });
}

// The read-only editor is for c0; a right-click then selects an unlocked
// prompt section without opening an editor. The editor stays c0's.
await scenario("wrong_item", async () => {
  const { w } = widget();
  select(w, "clip", "c0");
  laneEntry(w, "video", 0).locked = true;
  w._renderTimeline();
  const prompt = w._findSceneItemBySelection("prompt", 0);
  w.selectedItem = prompt; w.selectedItems = [prompt];
  w._renderTimeline();
  const label = describe(w)[0].text;
  const stillLocked = describe(w).some((c) => c.text.startsWith("(locked)"));
  w._showItemEditor();
  return { label, stillLocked, promptEditor: describe(w) };
});

await scenario("guide_popup_live_lock", async () => {
  const { w, calls, undoStack } = widget();
  w.activeScene.guide_track_config.locked = true;
  w._buildTrackLayout();
  const live = w.activeScene.guide_frames[0];
  const outcome = await w._writeGuideFieldFromPanel(live, { guide_id: "g1" }, { strength: 0.2 }, "guide strength");
  return { outcome, strength: live.strength ?? null, calls: calls.filter((c) => c !== "paint"), undo: undoStack.length };
});

await scenario("backstop_property", async () => {
  const { w, calls, undoStack } = widget();
  w._pushUndo("change opacity");
  const outcome = await w._updateItemPropertyWithinGesture("clip", "c1", { opacity: 0.5 });
  return { outcome, calls: calls.filter((c) => c !== "paint"), undo: undoStack.length };
});

await scenario("backstop_property_guide", async () => {
  const { w, calls, undoStack } = widget();
  w.activeScene.guide_track_config.locked = true;
  w._buildTrackLayout();
  w._pushUndo("toggle guide mute");
  const outcome = await w._updateItemPropertyWithinGesture("guide", 20, { muted: true });
  return { outcome, calls: calls.filter((c) => c !== "paint"), undo: undoStack.length };
});

await scenario("backstop_move", async () => {
  const { w, calls, undoStack } = widget();
  const hit = w._findSceneItemBySelection("reference", "r1");
  await w._moveItemToFrameWithinGesture("reference", "r1", hit.data, 50);
  return { calls: calls.filter((c) => c !== "paint"), undo: undoStack.length,
    row: [hit.data.start_frame, hit.data.end_frame] };
});

await scenario("linked_partner_locked", async () => {
  const { w, calls } = widget();
  w._pushUndo("change opacity");
  const outcome = await w._updateItemPropertyWithinGesture("clip", "c0", { opacity: 0.5 });
  return { outcome, calls: calls.filter((c) => c !== "paint") };
});

for (const [name, raw, timecode, how] of [
  ["start_negative", "-5", false, "apply"], ["start_unreadable", "abc", false, "apply"],
  ["start_enter_negative", "-1", false, "enter"], ["start_rounds_half_up", "12.5", false, "apply"],
  ["start_expression", "10+2", false, "enter"], ["start_timecode_negative", "-0.01", true, "apply"],
  ["start_timecode", "1", true, "apply"],
]) {
  await scenario(name, async () => {
    const { w, calls } = widget({ timecode });
    select(w, "clip", "c0");
    calls.length = 0;
    const input = startInput(w);
    input.value = raw;
    if (how === "apply") byText(w, "Apply").fire("click");
    else input.fire("keydown", { key: "Enter" });
    return { calls: calls.filter((c) => c !== "paint"), field: input.value };
  });
}

await scenario("guide_frame_negative", async () => {
  const { w, calls } = widget();
  select(w, "guide", 20);
  calls.length = 0;
  const input = startInput(w);
  input.value = "-3";
  byText(w, "Apply").fire("click");
  return { calls: calls.filter((c) => c !== "paint"), field: input.value };
});

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def results():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the item editor tests")
    script = (SCRIPT.replace("__WIDGET__", json.dumps(WIDGET_URL))
              .replace("__NOTES__", json.dumps(NOTES_URL))
              .replace("__SCENE__", json.dumps(SCENE)))
    result = subprocess.run([node, "--input-type=module"], input=script,
                            capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _controls(case):
    return [c for c in case["controls"] if c["tag"] in {"INPUT", "SELECT", "BUTTON"}]


@pytest.mark.parametrize("name", ["locked_reference", "locked_clip", "locked_audio"])
def test_a_locked_item_opens_read_only(results, name):
    controls = results[f"render_{name}"]["controls"]
    assert any(c["text"] == "(locked) Unlock the lane to edit." for c in controls)
    for control in _controls(results[f"render_{name}"]):
        if control["text"] == "Lane Setup…":
            assert not control["disabled"], "Lane Setup keeps its own lock handling"
            continue
        assert control["disabled"], control
        if control["tag"] in {"INPUT", "SELECT"}:
            assert control["opacity"] == "0.42", "inline-styled: disabled must also look it"
        assert control["title"] == "Unlock the lane to edit.", "a disabled control says why"
        if control["tag"] == "BUTTON":
            assert control["hover"] == control["base"], "hover must not light a disabled button"
    assert any(c["text"] == "Delete (locked)" for c in controls)


def test_lane_setup_stays_available_on_a_locked_reference(results):
    setup = [c for c in results["render_locked_reference"]["controls"] if c["text"] == "Lane Setup…"]
    assert setup and not setup[0]["disabled"]


def test_a_locked_guide_track_opens_read_only(results):
    case = results["guide_track_locked"]
    assert all(c["disabled"] for c in _controls(case))


def test_an_unlocked_item_is_unchanged(results):
    controls = results["render_unlocked_clip"]["controls"]
    assert not any(c["disabled"] for c in controls)
    assert not any(c["text"].startswith("(locked)") for c in controls)
    assert any(c["text"] == "Delete" for c in controls)


def test_the_editor_follows_the_lock_from_any_writer(results):
    case = results["follows_the_lock"]
    assert case["opened"] is False
    assert case["afterRebuild"] == {"note": True, "applyDisabled": True}
    assert case["afterHeader"] == {"note": False, "anyDisabled": False}


@pytest.mark.parametrize("name", [
    "clip_mute", "fit", "opacity", "start_apply", "reference_mute", "reference_strength",
    "audio_mute", "audio_volume", "guide_mute"])
def test_a_control_on_an_item_locked_in_place_paints_nothing(results, name):
    case = results[f"inplace_{name}"]
    assert case["dataUnchanged"]
    assert case["calls"] == [], "no Undo entry, no write, no move"
    assert case["undo"] == 0
    assert "timeline-item-locked" in case["toasts"]
    assert case["rerenderedLocked"]


@pytest.mark.parametrize("name", ["backstop_property", "backstop_property_guide"])
def test_the_property_backstop_refuses_and_drops_the_callers_undo_entry(results, name):
    case = results[name]
    assert case["outcome"] == "refused"
    assert not any(c.startswith("run:") for c in case["calls"])
    assert case["undo"] == 0
    assert "timeline-item-locked" in case["toasts"]


def test_the_move_backstop_refuses_before_undo_or_paint(results):
    case = results["backstop_move"]
    assert case["calls"] == []
    assert case["undo"] == 0
    assert case["row"] == [0, 30]
    assert "timeline-item-locked" in case["toasts"]


def test_an_unlocked_item_with_a_locked_partner_stays_editable(results):
    case = results["linked_partner_locked"]
    assert case["outcome"] == "ok"
    assert "run:update_clip" in case["calls"]
    assert "timeline-item-locked" not in case["toasts"]


@pytest.mark.parametrize(("name", "source", "field"), [
    ("start_negative", "item-editor-position-negative", "10"),
    ("start_unreadable", "item-editor-position-unreadable", "10"),
    ("start_enter_negative", "item-editor-position-negative", "10"),
    ("start_timecode_negative", "item-editor-position-negative", "0.42"),
    ("guide_frame_negative", "item-editor-position-negative", "20"),
])
def test_a_refused_position_says_why_and_restores_the_field(results, name, source, field):
    case = results[name]
    assert case["calls"] == []
    assert case["toasts"] == [source]
    assert str(case["field"]) == field


@pytest.mark.parametrize(("name", "frame"), [
    ("start_rounds_half_up", 13), ("start_expression", 12), ("start_timecode", 24),
])
def test_a_readable_start_moves_the_item(results, name, frame):
    case = results[name]
    assert case["calls"] == [f"move:clip:{frame}"]
    assert case["toasts"] == []


def test_the_editor_stays_with_the_item_it_was_built_for(results):
    """Audit #1: a selection moved elsewhere must not rebuild the editor around it."""
    case = results["wrong_item"]
    assert case["label"] == "Video Clip" and case["stillLocked"]
    assert case["promptEditor"] is None, "a prompt section has no item editor"


def test_the_guides_popup_reads_the_lock_live(results):
    """Audit #3: refused before its Undo entry (which would clear Redo) and paint."""
    case = results["guide_popup_live_lock"]
    assert case["outcome"] == "refused"
    assert case["strength"] is None
    assert case["calls"] == [] and case["undo"] == 0
    assert case["toasts"] == ["guide-edit-locked"]
