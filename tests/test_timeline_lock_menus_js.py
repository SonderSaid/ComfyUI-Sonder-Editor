"""Timeline right-click menus and the lane lock.

Plan: Timeline Lock Menus (`memory/plans/timeline-lock-menus.md`).

The menus are driven through the shipped `contextmenu` listener: a real
``EditorWidget`` (``Object.create(EditorWidget.prototype)``) runs its real
``_setupTimelineEvents`` against a fake canvas that records listeners, and the
test fires the recorded handler. Only geometry is stood in for -- the pointer
coordinates, the header and item hit tests, the row under the pointer, and the
frame -- and each stand-in checks it was asked about the event's pointer. Every
label, lock read, link expansion and selection change is the shipped code's
own. ``_showContextMenu`` records the items instead of drawing, and the actions
a scenario invokes run against spies.

The first group of tests pins each menu as it stood before the lock work began.
Each later phase of the plan edits only the expectations its own change
intends.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WIDGET_URL = (ROOT / "web" / "js" / "editor_widget.js").as_uri()

# One scene. Lanes, by layout index:
#    0 video 0 (unlocked)    1 video 1 (LOCKED)
#    2 audio 0 (unlocked)    3 audio 1 (LOCKED)   4 audio 2 (LOCKED, empty)
#    5 audio 3 (unlocked, empty)
#    6 reference 0   7 reference 1   8 reference 2 (all unlocked)   9 reference 3 (LOCKED)
#   10 guides   11 prompt   12 global prompt   13 motion driver 0 (unlocked)
# Links: c3 (video 0) <-> a1 (audio 0), both unlocked;
#        c4 (video 0) <-> a2 (audio 1, locked);
#        prompt p1 <-> c2 (video 1, locked).
SCENE = {
    "scene_id": "scene",
    "duration_frames": 100,
    "video_lane_count": 2,
    "audio_lane_count": 4,
    "reference_lane_count": 4,
    "motion_driver_lane_count": 1,
    "clips": [
        {"clip_id": "c1", "track_index": 0, "role": "render", "source_path": "media/v1.mp4",
         "timeline_start_frame": 0, "timeline_end_frame": 120, "source_in_frame": 0},
        {"clip_id": "c2", "track_index": 1, "role": "render", "source_path": "media/v1.mp4",
         "timeline_start_frame": 0, "timeline_end_frame": 40, "source_in_frame": 0},
        {"clip_id": "c3", "track_index": 0, "role": "render", "source_path": "media/v1.mp4",
         "timeline_start_frame": 130, "timeline_end_frame": 140, "source_in_frame": 0},
        {"clip_id": "c4", "track_index": 0, "role": "render", "source_path": "media/v1.mp4",
         "timeline_start_frame": 150, "timeline_end_frame": 160, "source_in_frame": 0},
        {"clip_id": "c5", "track_index": 0, "role": "motion_driver", "source_path": "media/v1.mp4",
         "timeline_start_frame": 0, "timeline_end_frame": 40, "source_in_frame": 0},
        {"clip_id": "c6", "track_index": 0, "role": "render", "source_path": "media/still.png",
         "timeline_start_frame": 60, "timeline_end_frame": 70, "source_in_frame": 0},
    ],
    "audio_tracks": [
        {"track_id": "a1", "lane_index": 0, "source_path": "media/au1.wav",
         "timeline_start_frame": 130, "timeline_end_frame": 140},
        {"track_id": "a2", "lane_index": 1, "source_path": "media/au1.wav",
         "timeline_start_frame": 150, "timeline_end_frame": 160},
        {"track_id": "a3", "lane_index": 0, "source_path": "media/au1.wav",
         "timeline_start_frame": 0, "timeline_end_frame": 50},
    ],
    "guide_frames": [
        {"guide_id": "g1", "frame_index": 10, "asset_id": "img1"},
    ],
    "reference_items": [
        {"reference_item_id": "r1", "lane_index": 0, "start_frame": 0, "end_frame": 30},
        {"reference_item_id": "r2", "lane_index": 3, "start_frame": 0, "end_frame": -1},
    ],
    "prompt_sections": [
        {"prompt_id": "p0", "start_frame": 0, "end_frame": 20, "muted": False},
        {"prompt_id": "p1", "start_frame": 30, "end_frame": 40, "muted": False},
    ],
    "linked_item_groups": [
        {"group_id": "L1", "items": [{"type": "clip", "id": "c3"}, {"type": "audio", "id": "a1"}]},
        {"group_id": "L2", "items": [{"type": "clip", "id": "c4"}, {"type": "audio", "id": "a2"}]},
        {"group_id": "L3", "items": [{"type": "prompt", "id": "p1"}, {"type": "clip", "id": "c2"}]},
    ],
}

LAYOUT = [
    {"type": "video", "laneIndex": 0, "locked": False},
    {"type": "video", "laneIndex": 1, "locked": True},
    {"type": "audio", "laneIndex": 0, "locked": False},
    {"type": "audio", "laneIndex": 1, "locked": True},
    {"type": "audio", "laneIndex": 2, "locked": True},
    {"type": "audio", "laneIndex": 3, "locked": False},
    {"type": "reference", "laneIndex": 0, "locked": False},
    {"type": "reference", "laneIndex": 1, "locked": False},
    {"type": "reference", "laneIndex": 2, "locked": False},
    {"type": "reference", "laneIndex": 3, "locked": True},
    {"type": "guides", "laneIndex": 0, "locked": False},
    {"type": "prompt", "laneIndex": 0, "locked": False},
    {"type": "prompt_global", "laneIndex": 0, "locked": False},
    {"type": "motion_driver", "laneIndex": 0, "locked": False},
]

ASSETS = {
    "video": [{"asset_id": "v1", "asset_type": "video", "width": 1344, "height": 768,
               "source_path": "media/v1.mp4"}],
    "image": [{"asset_id": "img1", "asset_type": "image", "width": 1024, "height": 1024},
              {"asset_id": "still", "asset_type": "image", "width": 640, "height": 480,
               "source_path": "media/still.png"}],
    "audio": [{"asset_id": "au1", "asset_type": "audio", "source_path": "media/au1.wav"}],
    "artifact": [],
}

# Each scenario names what is under the pointer and what the editor already
# holds. `hit` is resolved against the live scene by type and id; `lock`
# overrides a layout entry's lock for that scenario only; `frame` is the frame
# under the pointer (default 5); `invoke` runs the actions whose labels start
# with these strings, against spies, after the menu is built.
PRIOR = [["audio", "a3"]]
SCENARIOS = {
    "clip_unlocked": {"hit": ["clip", "c1"], "row": 0},
    "clip_locked": {"hit": ["clip", "c2"], "row": 1, "selected": PRIOR},
    "audio_unlocked": {"hit": ["audio", "a3"], "row": 2},
    "audio_locked": {"hit": ["audio", "a2"], "row": 3, "selected": PRIOR},
    "guide_unlocked": {"hit": ["guide", "g1"], "row": 10},
    "guide_locked": {"hit": ["guide", "g1"], "row": 10, "lock": {"10": True}, "selected": PRIOR},
    "clip_unlocked_guides_locked": {"hit": ["clip", "c1"], "row": 0, "lock": {"10": True}},
    "reference_unlocked": {"hit": ["reference", "r1"], "row": 6},
    "reference_locked": {"hit": ["reference", "r2"], "row": 9, "selected": PRIOR},
    "motion_driver_unlocked": {"hit": ["clip", "c5"], "row": 13},
    "motion_driver_locked": {"hit": ["clip", "c5"], "row": 13, "lock": {"13": True}, "selected": PRIOR},
    "image_clip": {"hit": ["clip", "c6"], "row": 0},
    "multi_selection": {"hit": ["clip", "c1"], "row": 0, "selected": [["clip", "c1"], ["audio", "a3"]]},
    "linked_unlocked": {"hit": ["clip", "c3"], "row": 0},
    "linked_to_locked_partner": {"hit": ["clip", "c4"], "row": 0},
    "empty_row": {"hit": None, "row": 0},
    "prompt_row_no_hit": {"hit": None, "row": 11},
    "prompt_unlocked": {"hit": ["prompt", 0], "row": 11, "invoke": ["Edit Prompt", "Mute Section"]},
    "prompt_locked": {"hit": ["prompt", 0], "row": 11, "lock": {"11": True}, "selected": PRIOR,
                      "invoke": ["Edit Prompt", "Mute Section"]},
    "prompt_linked_to_locked_partner": {"hit": ["prompt", 1], "row": 11, "frame": 35,
                                        "invoke": ["Mute Section"]},
    "prompt_global_unlocked": {"hit": ["prompt_global", 0], "row": 12},
    "prompt_global_locked": {"hit": ["prompt_global", 0], "row": 12, "lock": {"12": True}},
    "header_video_unlocked": {"header": 0, "invoke": ["Rename Lane"]},
    "header_video_locked": {"header": 1},
    "header_audio_empty_locked": {"header": 4},
    "header_audio_empty_unlocked": {"header": 5},
    "header_reference_first": {"header": 6},
    "header_reference_middle": {"header": 7},
    "header_reference_locked": {"header": 9},
    "header_selected_lanes": {"header": 0, "lanes": [["video", 0], ["audio", 1]]},
    "header_guides": {"header": 10, "invoke": ["Open Guide Management"]},
    "header_prompt": {"header": 11},
    "header_prompt_global": {"header": 12},
    "header_motion_driver_locked": {"header": 13, "lock": {"13": True}},
}

# The hit scenarios whose item sits on a locked lane.
LOCKED_HITS = {
    "clip_locked", "audio_locked", "guide_locked", "reference_locked",
    "motion_driver_locked", "prompt_locked", "prompt_global_locked",
}

SCRIPT = r"""
class N {
  constructor(tag) { this.tagName = String(tag).toUpperCase(); this.children = []; this.style = {};
    this.dataset = {}; this._handlers = {}; }
  appendChild(c) { this.children.push(c); return c; }
  append(...cs) { cs.forEach((c) => this.appendChild(c)); }
  addEventListener(t, h) { (this._handlers[t] ||= []).push(h); }
  removeEventListener() {}
  setAttribute() {} getAttribute() { return null; }
  querySelectorAll() { return []; } querySelector() { return null; }
  remove() {}
}
globalThis.document = { createElement: (t) => new N(t), body: new N("body"), activeElement: null,
  addEventListener() {}, removeEventListener() {}, querySelectorAll: () => [] };
globalThis.window = { addEventListener() {}, removeEventListener() {}, localStorage: null,
  comfyAPI: { api: { api: { apiURL: (path) => path } } }, prompt: () => null };
globalThis.localStorage = { getItem() { return null; }, setItem() {} };
globalThis.location = { href: "http://test/" };
globalThis.requestAnimationFrame = () => 0;

const { EditorWidget } = await import(__WIDGET__);
const SCENE = __SCENE__;
const LAYOUT = __LAYOUT__;
const ASSETS = __ASSETS__;
const SCENARIOS = __SCENARIOS__;

const key = (item) => `${item.type}:${String(item.id)}`;
const out = {};
for (const [name, scenario] of Object.entries(SCENARIOS)) {
  const w = Object.create(EditorWidget.prototype);
  const scene = structuredClone(SCENE);
  const layout = structuredClone(LAYOUT);
  for (const [idx, locked] of Object.entries(scenario.lock || {})) layout[Number(idx)].locked = locked;
  const pathToAsset = {};
  for (const asset of [...ASSETS.video, ...ASSETS.image, ...ASSETS.audio]) {
    if (asset.source_path) pathToAsset[asset.source_path] = asset;
  }
  let shown = null;
  let shownItems = [];
  let shownAt = null;
  const calls = [];
  const bad = [];
  Object.assign(w, {
    activeScene: scene, activeSceneId: "scene", projectDir: "project", totalFrames: 100,
    assets: structuredClone(ASSETS), _pathToAsset: pathToAsset, _trackLayout: layout,
    selectedItems: [], selectedItem: null, _selectedLanes: [], _selectedPromptIdx: null,
    _optimisticSplitHalves: new Set(), playhead: 5,
    _renderTimeline() {},
    _showContextMenu(x, y, items) {
      shownAt = [x, y];
      shownItems = items;
      shown = items.map((item) => ({ label: item.label, disabled: !!item.disabled, danger: !!item.danger }));
    },
    // Spies for the actions a scenario invokes.
    _showPromptEditor(_section, idx) { calls.push(["showPromptEditor", idx]); },
    _toggleSelectedMute() { calls.push(["toggleSelectedMute", w.selectedItems.map(key)]); },
    _showGuideManagementPopup(x, y) { calls.push(["showGuideManagementPopup", x, y]); },
    _startLaneRename(layoutIdx) { calls.push(["startLaneRename", layoutIdx]); },
  });
  const find = (type, id) => {
    if (type === "prompt") {
      const section = scene.prompt_sections[id];
      return section ? { type: "prompt", id, data: section } : null;
    }
    // `_hitTestItem` answers the global prompt lane with id 0.
    if (type === "prompt_global") return { type: "prompt_global", id: 0, data: {} };
    if (type === "guide") {
      const guide = scene.guide_frames.find((g) => g.guide_id === id);
      return guide ? { type: "guide", id: guide.frame_index, data: guide } : null;
    }
    return w._findSceneItemBySelection(type, id);
  };
  w.selectedItems = (scenario.selected || []).map(([type, id]) => find(type, id));
  w.selectedItem = w.selectedItems[0] || null;
  w._selectedLanes = (scenario.lanes || []).map(([type, laneIndex]) => ({ type, laneIndex }));
  const hit = scenario.hit ? find(scenario.hit[0], scenario.hit[1]) : null;
  const headerRow = scenario.header;
  // Geometry stand-ins. Each checks it was asked about the pointer the event
  // carried, so a refactor that mis-wires coordinates fails here.
  const at = (what, ok) => { if (!ok) bad.push(what); };
  Object.assign(w, {
    _canvasMouseCoords: (e) => { at("coords", e.clientX === 10 && e.clientY === 20); return { x: 150, rawY: 40 }; },
    _hitTestTrackHeader: (x, rawY) => { at("header", x === 150 && rawY === 40);
      return headerRow == null ? null : { layoutIdx: headerRow }; },
    _hitTestItem: (x, rawY) => { at("item", x === 150 && rawY === 40); return hit ? { ...hit } : null; },
    _layoutIndexFromRawY: (rawY) => { at("row", rawY === 40); return headerRow == null ? scenario.row : headerRow; },
    _xToFrame: (x) => { at("frame", x === 150); return scenario.frame ?? 5; },
  });
  const canvas = new N("canvas");
  w.timelineCanvas = canvas;
  w._setupTimelineEvents();
  const before = { selected: w.selectedItems.map(key), lanes: w._selectedLanes.length };
  canvas._handlers.contextmenu[0]({ clientX: 10, clientY: 20, preventDefault() {} });
  const after = { selected: w.selectedItems.map(key), lanes: w._selectedLanes.length,
                  promptIdx: w._selectedPromptIdx };
  const invoked = {};
  for (const prefix of scenario.invoke || []) {
    const item = shownItems.find((candidate) => String(candidate.label).startsWith(prefix));
    calls.length = 0;
    item?.action?.();
    invoked[prefix] = { found: !!item, calls: structuredClone(calls),
                        selected: w.selectedItems.map(key), promptIdx: w._selectedPromptIdx };
  }
  out[name] = {
    hit: hit ? key(hit) : null,
    hitLocked: hit ? !!w._isItemLocked(hit) : null,
    menu: shown, shownAt, before, after, invoked, bad,
  };
}
const helper = Object.create(EditorWidget.prototype);
out.moveTarget = [0, 1, 2, 5].map((lane) => helper._laneRemovalMoveTarget(lane));
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def menus():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the timeline menu tests")
    script = (SCRIPT
              .replace("__WIDGET__", json.dumps(WIDGET_URL))
              .replace("__SCENE__", json.dumps(SCENE))
              .replace("__LAYOUT__", json.dumps(LAYOUT))
              .replace("__ASSETS__", json.dumps(ASSETS))
              .replace("__SCENARIOS__", json.dumps(SCENARIOS)))
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _labels(entry):
    """`label`, with ` [disabled]` and ` [danger]` markers, one string per row."""
    rows = []
    for item in entry["menu"] or []:
        row = item["label"]
        if item["disabled"]:
            row += " [disabled]"
        if item["danger"]:
            row += " [danger]"
        rows.append(row)
    return rows


# -- the harness itself ---------------------------------------------------------

def test_every_scenario_resolves_its_hit_and_reads_the_events_pointer(menus):
    for name, scenario in SCENARIOS.items():
        entry = menus[name]
        assert entry["bad"] == [], (name, entry["bad"])
        if scenario.get("hit"):
            assert entry["hit"] is not None, name
            assert entry["hitLocked"] is (name in LOCKED_HITS), name
        if entry["menu"] is not None:
            assert entry["shownAt"] == [10, 20], name
        for prefix, result in entry["invoked"].items():
            assert result["found"], (name, prefix)


def test_lane_removal_moves_items_to_the_lane_above_or_lane_1(menus):
    assert menus["moveTarget"] == [1, 0, 1, 4]


# -- characterization: the menus as they stood before the lock work ------------

def test_unlocked_clip_menu(menus):
    assert _labels(menus["clip_unlocked"]) == [
        "Mute Selected (1)",
        "Move to New Lane",
        "Convert to Driver",
        "Replace clip with…",
        "Set Selection to Clip",
        "Add Frame to Guides",
        "Inspect in Gallery",
        "Set Scene Aspect Ratio (1344:768)",
        "Extend Scene to Clip End",
        "Delete Clip [danger]",
    ]
    assert menus["clip_unlocked"]["after"]["selected"] == ["clip:c1"]


def test_unlocked_clip_with_guides_locked_greys_only_add_frame(menus):
    rows = _labels(menus["clip_unlocked_guides_locked"])
    assert "Add Frame to Guides (locked) [disabled]" in rows
    assert [r for r in rows if "Guides" not in r] == [
        r for r in _labels(menus["clip_unlocked"]) if "Guides" not in r]


def test_motion_driver_and_image_clip_menus(menus):
    assert _labels(menus["motion_driver_unlocked"]) == [
        "Mute Selected (1)",
        "Move to New Lane",
        "Convert to Render Clip",
        "Replace clip with…",
        "Set Selection to Clip",
        "Add Frame to Guides",
        "Inspect in Gallery",
        "Set Scene Aspect Ratio (1344:768)",
        "Delete Clip [danger]",
    ]
    assert _labels(menus["image_clip"]) == [
        "Mute Selected (1)",
        "Move to New Lane",
        "Convert to Driver (video only) [disabled]",
        "Replace clip with…",
        "Set Selection to Clip",
        "Add Frame to Guides",
        "Inspect in Gallery",
        "Set Scene Aspect Ratio (640:480)",
        "Delete Clip [danger]",
    ]


def test_unlocked_audio_menu(menus):
    assert _labels(menus["audio_unlocked"]) == [
        "Mute Selected (1)",
        "Move to New Lane",
        "Replace audio with…",
        "Set Selection to Audio",
        "Inspect in Gallery",
        "Delete Audio Track [danger]",
    ]


def test_unlocked_guide_menu(menus):
    assert _labels(menus["guide_unlocked"]) == [
        "Mute Selected (1)",
        "Replace guide with…",
        "Inspect in Gallery",
        "Set Scene Aspect Ratio (1024:1024)",
        "Select Guide Range",
        "Set Selection In",
        "Set Selection Out",
        "Delete Guide [danger]",
    ]


def test_unlocked_reference_menu(menus):
    assert _labels(menus["reference_unlocked"]) == [
        "Mute Selected (1)",
        "Set Selection to Reference",
        "Reference Lane Setup",
        "Delete Reference [danger]",
    ]


def test_multi_selection_menu(menus):
    assert _labels(menus["multi_selection"]) == [
        "Link Selected Items",
        "Mute Selected (2)",
        "Set Selection to Selected (2)",
        "Delete 2 items [danger]",
    ]
    assert menus["multi_selection"]["after"]["selected"] == ["clip:c1", "audio:a3"]


def test_linked_clip_menu(menus):
    assert _labels(menus["linked_unlocked"]) == [
        "Select Linked Items",
        "Unlink Linked Items",
        "Mute Selected (2)",
        "Move to New Lane",
        "Convert to Driver",
        "Replace clip with…",
        "Set Selection to Clip",
        "Add Frame to Guides",
        "Inspect in Gallery",
        "Set Scene Aspect Ratio (1344:768)",
        "Extend Scene to Clip End",
        "Delete Linked Items (2) [danger]",
    ]


def test_unlocked_clip_linked_to_a_locked_partner(menus):
    assert _labels(menus["linked_to_locked_partner"]) == [
        "Select Linked Items",
        "Unlink Linked Items",
        "Mute Selected (2) (locked) [disabled]",
        "Move to New Lane (locked) [disabled]",
        "Convert to Driver [disabled]",
        "Replace clip with… (locked) [disabled]",
        "Set Selection to Clip",
        "Add Frame to Guides",
        "Inspect in Gallery",
        "Set Scene Aspect Ratio (1344:768)",
        "Extend Scene to Clip End",
        "Delete Linked Items (2) (locked) [disabled] [danger]",
    ]


def test_a_row_with_no_item_has_no_menu(menus):
    assert menus["empty_row"]["menu"] is None


def test_prompt_row_menus(menus):
    prompt_entries = [
        "Edit Prompt",
        "Set Selection to Prompt",
        "Queue Prompt Section",
        "Mute Section",
        "Open Prompt Management",
        "Delete Prompt [danger]",
    ]
    assert _labels(menus["prompt_row_no_hit"]) == prompt_entries
    entry = menus["prompt_unlocked"]
    assert _labels(entry) == ["Mute Selected (1)", *prompt_entries]
    assert entry["invoked"]["Edit Prompt"]["calls"] == [["showPromptEditor", 0]]
    assert entry["invoked"]["Edit Prompt"]["promptIdx"] == 0
    assert entry["invoked"]["Mute Section"]["calls"] == [["toggleSelectedMute", ["prompt:0"]]]


def test_global_prompt_menu_is_its_own_and_never_selects(menus):
    assert _labels(menus["prompt_global_unlocked"]) == ["Edit Global Prompt", "Open Prompt Management"]
    assert _labels(menus["prompt_global_locked"]) == [
        "Edit Global Prompt (locked) [disabled]", "Open Prompt Management"]
    for name in ("prompt_global_unlocked", "prompt_global_locked"):
        assert menus[name]["after"]["selected"] == [], name


def test_header_unlocked_video_lane_menu(menus):
    entry = menus["header_video_unlocked"]
    assert _labels(entry) == [
        "Rename Lane",
        "Add Video Lane",
        "Delete Video Lane and Move Items [danger]",
        "Delete Video Lane and Items [danger]",
        "Delete Items in Video Lane [danger]",
    ]
    assert entry["invoked"]["Rename Lane"]["calls"] == [["startLaneRename", 0]]


def test_header_empty_unlocked_lane_menu(menus):
    assert _labels(menus["header_audio_empty_unlocked"]) == [
        "Rename Lane",
        "Add Audio Lane",
        "Remove Audio Lane [danger]",
    ]


def test_header_reference_lane_menus(menus):
    assert _labels(menus["header_reference_first"]) == [
        "Reference Lane Setup…",
        "Rename Lane",
        "Add Reference Lane",
        "Move Lane Down",
        "Delete Reference Lane and Move Items [danger]",
        "Delete Reference Lane and Items [danger]",
        "Delete Items in Reference Lane [danger]",
    ]
    assert _labels(menus["header_reference_middle"]) == [
        "Reference Lane Setup…",
        "Rename Lane",
        "Add Reference Lane",
        "Move Lane Up",
        "Move Lane Down",
        "Remove Reference Lane [danger]",
    ]


def test_header_fixed_lane_menus(menus):
    guides = menus["header_guides"]
    assert _labels(guides) == ["Open Guide Management"]
    assert guides["invoked"]["Open Guide Management"]["calls"] == [["showGuideManagementPopup", 10, 20]]
    assert _labels(menus["header_prompt"]) == ["Open Prompt Management"]
    assert _labels(menus["header_prompt_global"]) == ["Open Prompt Management", "Edit Global Prompt"]


# -- characterization of the gaps this plan closes -----------------------------
# These pin TODAY's behavior so the phase that changes it edits them on purpose.

def test_locked_items_have_no_menu_today(menus):
    for name in ("clip_locked", "audio_locked", "guide_locked", "reference_locked",
                 "motion_driver_locked"):
        assert menus[name]["menu"] is None, name
        assert menus[name]["after"]["selected"] == ["audio:a3"], name


def test_locked_prompt_track_menu_today(menus):
    entry = menus["prompt_locked"]
    assert _labels(entry) == [
        "Edit Prompt",
        "Set Selection to Prompt",
        "Queue Prompt Section",
        "Mute Section",
        "Open Prompt Management",
        "Delete Prompt (locked) [disabled] [danger]",
    ]
    assert entry["after"]["selected"] == ["audio:a3"]
    # Edit Prompt marks the locked section current before the editor refuses it.
    assert entry["invoked"]["Edit Prompt"]["promptIdx"] == 0
    # Mute Section selects the locked section, then asks the mute to refuse.
    assert entry["invoked"]["Mute Section"]["calls"] == [["toggleSelectedMute", ["prompt:0"]]]


def test_prompt_linked_to_a_locked_partner_today(menus):
    entry = menus["prompt_linked_to_locked_partner"]
    assert _labels(entry) == [
        "Select Linked Items",
        "Unlink Linked Items",
        "Mute Selected (2) (locked) [disabled]",
        "Edit Prompt",
        "Set Selection to Prompt",
        "Queue Prompt Section",
        "Mute Section",
        "Open Prompt Management",
        "Delete Linked Items (2) (locked) [disabled] [danger]",
    ]
    assert entry["invoked"]["Mute Section"]["calls"] == [["toggleSelectedMute", ["prompt:1"]]]


def test_header_locked_lane_menus_today(menus):
    assert _labels(menus["header_video_locked"]) == [
        "Rename Lane",
        "Add Video Lane",
        "Delete Video Lane and Move Items [danger]",
        "Delete Video Lane and Items (locked) [disabled] [danger]",
        "Delete Items in Video Lane [danger]",
    ]
    assert _labels(menus["header_audio_empty_locked"]) == [
        "Rename Lane",
        "Add Audio Lane",
        "Remove Audio Lane [danger]",
    ]
    assert _labels(menus["header_reference_locked"]) == [
        "Reference Lane Setup…",
        "Rename Lane",
        "Add Reference Lane",
        "Move Lane Up (locked) [disabled]",
        "Delete Reference Lane and Move Items [danger]",
        "Delete Reference Lane and Items (locked) [disabled] [danger]",
        "Delete Items in Reference Lane [danger]",
    ]
    assert _labels(menus["header_motion_driver_locked"]) == ["Rename Lane", "Add Driver Lane"]


def test_header_selected_lanes_menu_today(menus):
    assert _labels(menus["header_selected_lanes"]) == [
        "Rename Lane",
        "Add Video Lane",
        "Delete 2 Selected Lanes [danger]",
        "Delete Video Lane and Move Items [danger]",
        "Delete Video Lane and Items [danger]",
        "Delete Items in Video Lane [danger]",
    ]
