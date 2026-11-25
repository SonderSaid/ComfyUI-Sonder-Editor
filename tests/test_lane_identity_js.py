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
    result = _run_node(_harness(("_laneRemovalGuard", "_applyLocalRemoveLane"), scene={
        "video_lane_count": 2, "video_lane_configs": [{"lane_id": "v0"}, {"name": "x"}],
        "reference_lane_count": 1, "reference_lane_configs": [{"lane_id": "not this"}],
        "reference_lane_recipes": [{"lane_id": "r0"}],
    }) + """
console.log(JSON.stringify([h._laneRemovalGuard("video", 0), h._laneRemovalGuard("video", 1),
  h._laneRemovalGuard("audio", 0), h._laneRemovalGuard("reference", 0)]));
""")
    assert [guard.get("lane_id") for guard in result] == ["v0", None, None, "r0"]


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
