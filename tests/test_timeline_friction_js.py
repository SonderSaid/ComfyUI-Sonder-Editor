import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    return subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_timeline_header_resize_and_shared_trim_precedence():
    module_url = (ROOT / "web" / "js" / "editor_timeline_canvas.js").as_uri()
    script = f"""
const mod = await import({json.dumps(module_url)});
const clips = [
    {{ clip_id: "left", timeline_start_frame: 0, timeline_end_frame: 10, track_index: 0 }},
    {{ clip_id: "right", timeline_start_frame: 10, timeline_end_frame: 20, track_index: 0 }},
];
let selected = new Set();
const host = {{
    _labelW: 100,
    _scaleTimeline: 1,
    activeScene: {{ clips, audio_tracks: [], prompt_sections: [] }},
    _trackLayout: [{{ type: mod.TRACK_TYPE.VIDEO, laneIndex: 0, collapsed: false }}],
    _layoutIndexFromRawY() {{ return 0; }},
    _clipMatchesTrackEntry(clip, entry) {{ return clip.track_index === entry.laneIndex; }},
    _frameToX(frame) {{ return 100 + frame * 10; }},
    _isSelected(type, id) {{ return selected.has(`${{type}}:${{id}}`); }},
}};
const result = {{
    rulerResize: mod._hitTestHeaderEdge(host, 100, 5),
    laneResize: mod._hitTestHeaderEdge(host, 100, 30),
    frameZero: mod._hitTestEdge(host, 100, 30),
    leftSide: mod._hitTestEdge(host, 199, 30),
    rightSide: mod._hitTestEdge(host, 201, 30),
    exact: mod._hitTestEdge(host, 200, 30),
}};
selected = new Set(["clip:right"]);
result.selectedExact = mod._hitTestEdge(host, 200, 30);
console.log(JSON.stringify(result));
"""
    result = json.loads(_run_node(script))

    assert result["rulerResize"] is True
    assert result["laneResize"] is False
    assert (result["frameZero"]["id"], result["frameZero"]["edge"]) == ("left", "left")
    assert (result["leftSide"]["id"], result["leftSide"]["edge"]) == ("left", "right")
    assert (result["rightSide"]["id"], result["rightSide"]["edge"]) == ("right", "left")
    assert (result["exact"]["id"], result["exact"]["edge"]) == ("left", "right")
    assert (result["selectedExact"]["id"], result["selectedExact"]["edge"]) == ("left", "right")


def test_viewport_empty_and_missing_states_draw_optional_scene_outline():
    module_url = (ROOT / "web" / "js" / "viewport_surface.js").as_uri()
    script = f"""
const {{ createViewportSurface }} = await import({json.dumps(module_url)});
function render(scene, enabled) {{
    const calls = [];
    const ctx = {{
        globalAlpha: 1,
        fillStyle: "",
        font: "",
        textAlign: "",
        textBaseline: "",
        lineWidth: 1,
        strokeStyle: "",
        fillRect(...args) {{ calls.push(["fillRect", ...args]); }},
        fillText(...args) {{ calls.push(["fillText", ...args]); }},
        strokeRect(...args) {{ calls.push(["strokeRect", ...args]); }},
        save() {{}},
        restore() {{}},
    }};
    const canvas = {{ width: 320, height: 180, getContext() {{ return ctx; }} }};
    const surface = createViewportSurface({{
        canvas,
        getScene: () => scene,
        getFrame: () => 0,
        getTotalFrames: () => 24,
        getAssetForSourcePath: () => null,
        isSceneOutlineEnabled: () => enabled,
    }});
    surface.renderFrame();
    surface.destroy();
    return calls.filter((call) => call[0] === "strokeRect").length;
}}
const empty = {{ clips: [], audio_tracks: [], guide_frames: [] }};
const missing = {{
    clips: [{{ clip_id: "missing", source_path: "media/missing.mp4", timeline_start_frame: 0, timeline_end_frame: 10, track_index: 0 }}],
    audio_tracks: [],
    guide_frames: [],
}};
console.log(JSON.stringify({{
    emptyOn: render(empty, true),
    emptyOff: render(empty, false),
    missingOn: render(missing, true),
    missingOff: render(missing, false),
}}));
"""
    result = json.loads(_run_node(script))

    assert result == {"emptyOn": 1, "emptyOff": 0, "missingOn": 1, "missingOff": 0}


def test_editor_widget_wires_linked_unmute_and_clicked_type_consolidation():
    source = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")

    assert "_buildLinkedMuteOperations(unmuteSeeds, false)" in source
    assert "apply_linked: applyLinked" in source
    assert "if (target.hidden)" in source
    assert "this._trackVisibilityState(target) !== \"visible\"" in source
    assert "visibilitySeq !== this._headerVisibilitySeq" in source
    assert "_trimDeltaLimits(edgeHit" in source
    assert "validate_lane_collision: true" in source
    assert "item.maxEnd ?? Number.POSITIVE_INFINITY" in source
    assert "selected?.type !== hit.type" in source
    assert "type: \"consolidate_items\"" in source
    assert "remove_vacated_lanes: true" in source
