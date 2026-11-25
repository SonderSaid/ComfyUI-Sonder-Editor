"""The editor names media lanes by id (backlog step 2b, Phase 1)."""
import json
import re

from test_prompt_authoring_ux import ROOT, _method, _run_node, _source

WIDGET = "web/js/editor_widget.js"
REGISTRY = (ROOT / "web/js/lane_registry.js").as_uri()


def _harness(*methods, scene):
    widget = _source(WIDGET)
    body = "\n".join(_method(widget, *pair) for pair in methods)
    return f"""
const {{descriptorFor, descriptorForLaneType}} = await import({json.dumps(REGISTRY)});
class Harness {{
{body}
}}
const h = new Harness();
h.activeScene = {json.dumps(scene)};
"""


LANE_PAINT = (("_newLaneIds", "_applyLocalMoveLane"),
              ("_trimLocalLaneConfigs", "_compactEmptyMediaLaneLocal"),
              ("_defaultLaneConfig", "_trackConfigForFixedType"))


def test_a_new_lane_is_painted_with_the_id_the_operation_offers():
    result = _run_node(_harness(*LANE_PAINT, scene={
        "video_lane_count": 1, "video_lane_configs": [{"lane_id": "v0"}],
        # Kept past the count: adopted, with the id it already has.
        "audio_lane_count": 1, "audio_lane_configs": [{"lane_id": "a0"}, {"lane_id": "kept"}],
        "motion_driver_lane_count": 1,
        "reference_lane_count": 1, "reference_lane_configs": [{}],
        "reference_lane_recipes": [{"lane_id": "r0"}],
    }) + """
const out = {};
for (const [type, count] of [["video", 3], ["audio", 2], ["audio", 3], ["motion_driver", 1],
                             ["reference", 2]]) {
  const ids = h._newLaneIds(type, count);
  h._applyLocalSetLaneCount(type, count, ids);
  const s = h.activeScene;
  out[`${type}${count}`] = { ids, count: s[`${type}_lane_count`],
    painted: s[`${type}_lane_configs`].map((c) => c.lane_id ?? null),
    recipes: (s[`${type}_lane_recipes`] || []).map((r) => r.lane_id) };
}
console.log(JSON.stringify(out));
""")
    assert result["video3"]["painted"] == ["v0", *result["video3"]["ids"]]
    assert len(set(result["video3"]["painted"])) == 3 and result["video3"]["count"] == 3
    assert result["audio2"]["ids"] == [] and result["audio2"]["painted"] == ["a0", "kept"]
    assert result["audio3"]["painted"] == ["a0", "kept", *result["audio3"]["ids"]]
    assert len(result["audio3"]["ids"]) == 1
    # A scene without configs (never saved by this version) names its lanes too.
    assert len(result["motion_driver1"]["ids"]) == 1
    # A Reference lane's id is its recipe's, and the server mints it.
    assert result["reference2"]["ids"] == []
    assert result["reference2"]["painted"] == [None, None]
    assert result["reference2"]["recipes"] == ["r0", ""]
    assert all(re.fullmatch(r"[0-9A-Za-z_-]{8,64}", value)
               for value in result["video3"]["ids"] + result["audio3"]["ids"])


def test_every_lane_the_editor_adds_is_named():
    """A lane painted without an id cannot be followed through an Undo, so a queued edit
    on it is refused. Reference lanes are the server's to name."""
    widget = _source(WIDGET)
    emissions = [widget[match.start() - 4:match.start() + 220]
                 for match in re.finditer(r'type: "set_lane_count"', widget)]
    assert len(emissions) == 7
    for text in emissions:
        operation = text[:text.index("}")]
        assert 'lane_type: "reference"' in operation or "lane_ids" in operation, operation
    # Each paints what it offers.
    paints = re.findall(r"this\._applyLocalSetLaneCount\(([^;]*)\);", widget, re.S)
    unnamed = [call for call in paints if "aneIds" not in call]
    assert len(paints) == 5 and unnamed == ["TRACK_TYPE.REFERENCE, laneIndex + 1"]


def test_the_removal_guard_names_the_lane():
    result = _run_node(_harness(("_laneRemovalGuard", "_applyLocalRemoveLane"),
                                ("_laneIdAt", "_newLaneIds"), scene={
        "video_lane_count": 2, "video_lane_configs": [{"lane_id": "v0"}, {"name": "x"}],
        "reference_lane_count": 1, "reference_lane_configs": [{"lane_id": "not this"}],
        "reference_lane_recipes": [{"lane_id": "r0"}],
    }) + """
console.log(JSON.stringify([h._laneRemovalGuard("video", 0), h._laneRemovalGuard("video", 1),
  h._laneRemovalGuard("audio", 0), h._laneRemovalGuard("reference", 0)]));
""")
    assert [guard.get("lane_id") for guard in result] == ["v0", None, None, "r0"]


def test_every_lane_setting_write_names_its_lane():
    """Lock, rename and hide; bulk and single; media, Reference and fixed. Each sends
    only what its gesture changed, and paints only that."""
    result = _run_node(_harness(
        ("_laneIdAt", "_newLaneIds"),
        ("_saveLaneConfigWithinGesture", "_trackLaneRecipeWrite"),
        ("_applyHeaderVisibilityBulkWithinGesture", "_startLaneRename"),
        ("_defaultLaneConfig", "_trackConfigForFixedType"),
        scene={
            "video_lane_count": 2, "video_lane_configs": [
                {"lane_id": "v0"}, {"lane_id": "v1", "name": "kept", "color": "#kept"}],
            "guide_track_config": {"name": "kept"},
            "audio_lane_count": 1, "audio_lane_configs": [{}],
            "motion_driver_lane_count": 1, "motion_driver_lane_configs": [{"lane_id": "d0"}],
            "reference_lane_count": 1, "reference_lane_configs": [{"lane_id": "not this"}],
            "reference_lane_recipes": [{"lane_id": "r0"}],
        }) + """
globalThis.sessionDiagRecord = () => {};
globalThis.coalesceSceneMutationIntents = () => {};
Object.assign(h, {
  projectDir: "p", activeSceneId: "scene", sent: [],
  _laneTypeForEntry: (e) => descriptorFor(e.type)?.laneType || "",
  _pushUndo: () => ({}), _discardUndoEntry() {}, _reconcileSelection() {}, _updateToolbar() {}, _renderTimeline() {}, _buildTrackLayout() {},
  _renderViewportFrame() {}, _renderSceneAfterLocalMutation() {}, _clearPlaybackWarmOverlay() {},
  _isLaneVisibilityControlDisabled: () => false, _trackVisibilityState: () => "visible",
  _fetchScenes() { throw new Error('the gesture failed'); },
  _buildLinkedMuteOperations: () => ({ operations: [], targets: [] }),
  _planHeaderUnmute: () => ({ operations: [], targets: [] }),
  async _runSceneMutation(operations) { this.sent.push(operations); return { payload: { results: [] } }; },
});
const entry = (type, laneIndex) => ({ type, laneIndex, customName: "n", locked: true, hidden: false,
  color: "#fallback" });
const entries = [entry("video", 1), entry("audio", 0), entry("motion_driver", 0),
                 entry("reference", 0), entry("guides", 0)];
await h._saveLaneConfigWithinGesture(entries, { settings: ["locked"] });
await h._saveLaneConfigWithinGesture(entries, { settings: ["name"] });
const painted = structuredClone([h.activeScene.video_lane_configs[1], h.activeScene.guide_track_config]);
await h._applyHeaderVisibilityBulkWithinGesture(entries, true);
await h._saveLaneConfigWithinGesture([entry("reference", 0)], { recipePatch: null });
console.log(JSON.stringify({ sent: h.sent.map((ops) => ops.map((op) =>
  [op.lane_type, op.lane_index, op.expected?.lane_id ?? null])),
  fields: h.sent.map((ops) => ops.map((op) => op.fields)), painted,
  hidden: [h.activeScene.video_lane_configs[1], h.activeScene.guide_track_config] }));
""")
    expected = [["video", 1, "v1"], ["audio", 0, None], ["motion_driver", 0, "d0"],
                ["reference", 0, "r0"], ["guide", 0, None]]
    assert result["sent"][:3] == [expected] * 3
    assert result["fields"][:3] == [[{"locked": True}] * 5, [{"name": "n"}] * 5, [{"hidden": True}] * 5]
    assert result["fields"][3] == [{}], "a recipe write carries no setting"
    lane = {"lane_id": "v1", "name": "n", "color": "#kept", "locked": True}
    assert result["painted"] == [lane, {"name": "n", "locked": True}]
    assert result["hidden"] == [{**lane, "hidden": True},
                                {"name": "n", "locked": True, "hidden": True}]


def test_every_lane_save_says_what_it_changed():
    """A save that names no setting and carries no recipe would write nothing."""
    calls = []
    for path in ("web/js/editor_widget.js", "web/js/editor_reference_panel.js"):
        source = _source(path)
        for match in re.finditer(r"\._saveLaneConfig(?:WithinGesture)?\(", source):
            calls.append(source[match.start():match.start() + 420].split(";")[0])
    calls = [call for call in calls if "(...args)" not in call]
    assert len(calls) == 5
    for call in calls:
        assert "settings: [" in call or "expectedLaneId" in call, call


def test_a_rename_and_a_lane_menu_act_on_the_lane_they_were_opened_on():
    """A refresh moves lanes while the input or the menu is open. Both used to act on
    whatever lane had taken the number, with that lane's own id vouching for it."""
    result = _run_node(_harness(
        ("_laneIdAt", "_newLaneIds"), ("_liveLaneEntry", "_laneTypeForEntry"),
        ("_startLaneRename", "_laneIdentityFailureMessage"),
        ("_laneHeaderContextMenuItems", "_itemContextMenuItems"),
        scene={"video_lane_count": 3, "video_lane_configs": [
            {"lane_id": "a"}, {"lane_id": "b"}, {"lane_id": "c"}]}) + """
const { TRACK_TYPE, laneCountFor, laneItemsForType, laneLabel } = await import(%s);
const inputs = [];
globalThis.document = { body: { appendChild() {} }, createElement: () => {
  const input = { style: {}, on: {}, addEventListener(name, f) { this.on[name] = f; }, focus() {},
                  select() {}, remove() {} };
  inputs.push(input); return input; } };
const out = { saved: [], toasts: [], calls: [] };
Object.assign(h, {
  timelineCanvas: { getBoundingClientRect: () => ({ left: 0, top: 0 }) }, scrollY: 0, _labelW: 10,
  _trackY: () => 0, _trackH: () => 10, _renderTimeline() {}, _selectedLanes: [],
  _showToast: (text) => out.toasts.push(text), _isLaneTrackType: () => true,
  _isLaneLocked: () => false, _isLaneSelected: () => false, _laneRemovalMoveTarget: () => 0,
  _saveLaneConfig: (entries, options) => out.saved.push(
    [entries.map((e) => [e.laneIndex, e.customName]), options.settings]),
});
for (const name of ["_removeLane", "_removeLaneWithItems", "_removeLaneDeletingItems", "_deleteItemsInLane"]) {
  h[name] = (type, index) => out.calls.push([name, index]);
}
const layout = () => { h._trackLayout = h.activeScene.video_lane_configs.map((c, laneIndex) => ({
  type: "video", laneIndex, label: "", customName: "" })); };
const lanes = (...ids) => { h.activeScene.video_lane_count = ids.length;
  h.activeScene.video_lane_configs = ids.map((lane_id) => ({ lane_id })); layout(); };
lanes("a", "b", "c");
const rename = (...after) => {
  h._startLaneRename(1);
  lanes(...after);
  const input = inputs.at(-1);
  input.value = "for b";
  input.on.blur();
  lanes("a", "b", "c");
};
rename("a", "b", "c"); rename("b", "c"); rename("a", "c");
h.activeScene.video_lane_configs.forEach((c) => delete c.lane_id);
rename("", "", "");
const clips = [{ clip_id: "x", track_index: 1, role: "render" }];
for (const filled of [false, true]) for (const after of [["b", "c"], ["a", "c"]]) {
  lanes("a", "b", "c");
  h.activeScene.clips = filled ? clips : [];
  const menu = h._laneHeaderContextMenuItems(1, 0, 0);
  lanes(...after);
  for (const item of menu) if (/Remove|Delete/.test(item.label)) item.action();
}
console.log(JSON.stringify(out));
""" % json.dumps(REGISTRY))
    assert result["saved"] == [
        [[[1, "for b"]], ["name"]],        # nothing moved
        [[[0, "for b"]], ["name"]],        # b moved: followed
        [[[1, "for b"]], ["name"]]]        # no ids known: where it was
    assert result["toasts"].count("That lane is no longer there.") == 1 + 1 + 3
    assert result["calls"] == [["_removeLane", 0], ["_removeLaneWithItems", 0],
                               ["_removeLaneDeletingItems", 0], ["_deleteItemsInLane", 0]]


def test_a_take_drop_compares_settings_and_the_ids_both_reads_know():
    result = _run_node(_harness(("_sameLanes", "_takeRestorationForDrop"), scene={}) + """
const row = { name: "", color: "", locked: false, hidden: false };
const cases = [
  [[row], [{ ...row, lane_id: "a" }], 1],                        // named since
  [[], [{ ...row, lane_id: "a", future: 1 }], 1],                // no config on record
  [[{ ...row, lane_id: "a" }], [{ ...row, lane_id: "a" }], 1],
  [[{ ...row, lane_id: "a" }], [{ ...row, lane_id: "b" }], 1],   // another lane
  [[row], [{ ...row, name: "x" }], 1],
  [[row], [{ ...row, locked: true }], 1],
  [[row], [row, { ...row, name: "past the count" }], 1],
];
console.log(JSON.stringify(cases.map((value) => h._sameLanes(...value))));
""")
    assert result == [True, True, True, False, False, False, True]


def _rebase(operation, ordered, authored):
    widget = _source(WIDGET)
    rebase = _method(widget, "_historyExpectedProjection", "_queueProjectMutation")
    return _run_node(f"""
class Harness {{
{rebase}
}}
const intent = {{ sceneId: "scene", operations: [{json.dumps(operation)}] }};
try {{
  console.log(JSON.stringify(new Harness()._rebaseSceneMutationIntentForHistory(
    intent, {json.dumps(ordered)}, {json.dumps(authored)}).operations[0]));
}} catch (error) {{ console.log(JSON.stringify({{ refused: error.code }})); }}
""")


def _scene(family, *ids):
    return {"scene_id": "scene", f"{family}_lane_count": len(ids),
            f"{family}_lane_configs": [{"lane_id": value} if value else {} for value in ids]}


def test_a_queued_removal_never_reaches_a_takes_lane():
    """The page holds [V0, N]; an unread take makes it [V0, N, T]; Undo of "add N" leaves
    [V0, T]: the same count, other lanes, the same default settings."""
    for family in ("video", "motion_driver", "audio"):
        remove = {"type": "remove_lane", "lane_type": family, "lane_index": 1,
                  "item_policy": "delete_items",
                  "expected": {"lane_count": 2, "config": {}, "lane_id": "N"}}
        assert _rebase(remove, _scene(family, "V0", "T"), _scene(family, "V0", "N")) == {
            "refused": "history_lane_unresolvable"}
        # Painted without an id.
        assert _rebase(remove, _scene(family, "V0", "T"), _scene(family, "V0", "")) == {
            "refused": "history_lane_unresolvable"}


def test_a_queued_edit_follows_its_lane_past_an_unread_take():
    """The edit that used to be lost: default lanes, a take the editor had not read."""
    edit = {"type": "update_lane_config", "lane_type": "video", "lane_index": 1,
            "fields": {"name": "Renamed"}}
    assert _rebase(edit, _scene("video", "a", "b", "T"), _scene("video", "a", "b"))[
        "lane_index"] == 1
    assert _rebase(edit, _scene("video", "new", "a", "b"), _scene("video", "a", "b"))[
        "lane_index"] == 2
    # A lane the batch itself adds keeps its offset from the end.
    drop = {"type": "create_audio_track", "fields": {"lane_index": 2}}
    assert _rebase(drop, _scene("audio", "a", "b", "T"), _scene("audio", "a", "b"))[
        "fields"]["lane_index"] == 3
