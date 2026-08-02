import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = ROOT / "tests" / "data" / "track_layout_golden.json"


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    return subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_lane_registry_reproduces_pre_refactor_track_layout_golden():
    module_url = (ROOT / "web" / "js" / "lane_registry.js").as_uri()
    fixture_path = str(GOLDEN_PATH)
    script = f"""
import fs from "node:fs";
const mod = await import({json.dumps(module_url)});
const golden = JSON.parse(fs.readFileSync({json.dumps(fixture_path)}, "utf8"));
const theme = {{
  palette: ["#5d8aa0", "#7a8e8e", "#8a7fa0", "#6b7280", "#6f8c63", "#8b7f6b"],
  fixed: {{ laneDriver: "#8a7fa0" }},
}};
const cases = golden.inputs.map((testCase) => {{
  const scene = structuredClone(testCase.scene);
  if (scene.video_lane_count === "__NAN__") scene.video_lane_count = Number.NaN;
  const collapsedKeys = testCase.collapsed === null ? null : new Set(testCase.collapsed);
  const rows = mod.buildTrackLayout({{ scene, collapsedKeys, theme }});
  return {{ name: testCase.name, rows, length: rows.length, clampSawAssigned: true }};
}});
console.log(JSON.stringify(cases));
"""
    actual = json.loads(_run_node(script))
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert golden["case_count"] == 215
    assert actual == golden["cases"]


def test_editor_host_binds_real_theme_and_assigns_layout_before_clamping():
    source = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    start = source.index("    _buildTrackLayout() {")
    end = source.index("\n    /** Find layout index for a video lane */", start)
    method = source[start:end]
    assert "theme: { palette: LANE_PALETTE, fixed: COLORS }" in method
    assert method.index("this._trackLayout =") < method.index("this._clampScrollY()")


def test_lane_registry_layout_fallbacks_and_drop_compatibility():
    module_url = (ROOT / "web" / "js" / "lane_registry.js").as_uri()
    script = f"""
const mod = await import({json.dumps(module_url)});
const layout = mod.buildTrackLayout({{
  scene: {{}},
  collapsedKeys: null,
  theme: {{ palette: ["p0", "p1"], fixed: {{ laneDriver: "driver" }} }},
}});
const trackTypes = ["video", "audio", "motion_driver", "guides", "prompt_global", "prompt"];
const assetTypes = ["", "video", "audio", "image", "artifact"];
const cells = {{}};
for (const trackType of trackTypes) {{
  cells[trackType] = {{}};
  for (const assetType of assetTypes) {{
    cells[trackType][assetType || "empty"] = [false, true].map((hasAudio) =>
      mod.laneAcceptsAssetType(trackType, assetType, {{ hasAudio }})
    );
  }}
}}
console.log(JSON.stringify({{
  guideLaneType: mod.laneTypeFor(mod.TRACK_TYPE.GUIDES),
  guideVariableLaneType: mod.variableLaneTypeFor(mod.TRACK_TYPE.GUIDES),
  unknownRoleTrackType: mod.trackTypeForClip({{ role: "foo" }}),
  unknownRoleMutationItems: mod.laneItemsForType(
    {{ clips: [{{ role: "foo", track_index: 0 }}] }}, mod.TRACK_TYPE.VIDEO, 0
  ).length,
  guideFallbackIndex: mod.laneLayoutIndex(layout, mod.TRACK_TYPE.GUIDES, 0),
  audioIndex: mod.laneLayoutIndex(layout, mod.TRACK_TYPE.AUDIO, 0),
  animaticVideoIndex: mod.laneLayoutIndex(
    layout, mod.TRACK_TYPE.VIDEO, 0, {{ animaticMode: true }}
  ),
  reservedOrder: mod.RESERVED_REFERENCE_ORDER,
  driverOrder: mod.descriptorFor(mod.TRACK_TYPE.MOTION_DRIVER).layoutOrder,
  guidesOrder: mod.descriptorFor(mod.TRACK_TYPE.GUIDES).layoutOrder,
  driverColorKey: mod.descriptorFor(mod.TRACK_TYPE.MOTION_DRIVER).color.key,
  cells,
}}));
"""
    result = json.loads(_run_node(script))

    assert result["guideLaneType"] == "guide"
    assert result["guideVariableLaneType"] == ""
    assert result["unknownRoleTrackType"] == "video"
    assert result["unknownRoleMutationItems"] == 0
    assert result["guideFallbackIndex"] == result["audioIndex"]
    assert result["animaticVideoIndex"] is None
    assert result["driverOrder"] < result["reservedOrder"] < result["guidesOrder"]
    assert result["reservedOrder"] == 40
    assert result["driverColorKey"] == "laneDriver"

    reject = ["reject", "reject"]
    accept = ["accept", "accept"]
    expected = {
        "video": {"empty": accept, "video": accept, "audio": reject, "image": reject, "artifact": reject},
        "audio": {
            "empty": accept,
            "video": ["reject", "accept_as_audio"],
            "audio": accept,
            "image": reject,
            "artifact": reject,
        },
        "motion_driver": {"empty": accept, "video": accept, "audio": reject, "image": reject, "artifact": reject},
        "guides": {"empty": reject, "video": reject, "audio": reject, "image": accept, "artifact": reject},
        "prompt_global": {"empty": reject, "video": reject, "audio": reject, "image": reject, "artifact": reject},
        "prompt": {"empty": reject, "video": reject, "audio": reject, "image": reject, "artifact": reject},
    }
    assert result["cells"] == expected


def test_authoritative_drop_keeps_occupancy_and_overlap_checks_out_of_hover():
    source = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    hover_start = source.index("    _resolveDropHoverTarget(rawY) {")
    hover_end = source.index("\n    async _handleAssetDrop(", hover_start)
    drop_start = hover_end
    drop_end = source.index("\n    _firstAvailableLane(", drop_start)
    hover = source[hover_start:hover_end]
    drop = source[drop_start:drop_end]

    assert "_driverClipInLane" in drop and "laneHasOverlap" in drop
    assert "_driverClipInLane" not in hover and "laneHasOverlap" not in hover, (
        "Hover is advisory; only authoritative drop checks occupancy and overlap. "
        "Unifying those paths would change current drag feedback behavior."
    )
