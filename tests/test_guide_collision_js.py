import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_collision_toggle_lives_in_settings_guides_not_render_queue():
    root = Path(__file__).resolve().parents[1]
    panel = (root / "web" / "js" / "editor_settings_panel.js").read_text(encoding="utf-8")
    queue = (root / "web" / "js" / "shared_render_queue.js").read_text(encoding="utf-8")
    widget = (root / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    guides_start = panel.index('"Guides"')
    server_start = panel.index('"Server"', guides_start)
    guides_section = panel[guides_start:server_start]
    assert '"guideCollisionAutoOffset"' in guides_section
    assert '"Collision Auto-Offset (project-wide)"' in guides_section
    assert "guideCollisionAutoOffset" not in queue
    assert "get _guideCollisionAutoOffset()" in widget
    assert "_toggleGuideCollisionAutoOffset: (on)" in widget


def test_js_collision_mirror_matches_anchor_and_driver_collision_cases():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    module_path = Path(__file__).resolve().parents[1] / "web" / "js" / "guide_collision.js"
    encoded = base64.b64encode(module_path.read_bytes()).decode("ascii")
    script = r'''
const mod = await import("data:text/javascript;base64,__MODULE__");
const constraint = {step: 8, offset: 1};
const anchor = mod.resolveGuideCollisions({
  guides: [{guide_id: "g", local_idx: 0}],
  drivers: [{clip_id: "d", lane_index: 0, local_idx: 0, pixel_len: 121}],
  frame_count: 121,
  frame_constraint: constraint,
  auto_offset_enabled: true,
});
const drivers = mod.resolveGuideCollisions({
  guides: [],
  drivers: [
    {clip_id: "a", lane_index: 0, local_idx: 0, pixel_len: 17},
    {clip_id: "b", lane_index: 1, local_idx: 0, pixel_len: 17},
  ],
  frame_count: 121,
  frame_constraint: constraint,
  auto_offset_enabled: true,
});
console.log(JSON.stringify({
  effective: anchor.entries[0].effective_local_idx,
  unresolved: anchor.unresolved_collision_count,
  driver_driver: drivers.driver_driver_collision_count,
  coords: mod.driverOccupiedCoords(0, 121, 8, 1),
}));
'''.replace("__MODULE__", encoded)
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(completed.stdout) == {
        "effective": 2,
        "unresolved": 0,
        "driver_driver": 3,
        "coords": [0, 1, 9, 17, 25, 33, 41, 49, 57, 65, 73, 81, 89, 97, 105, 113],
    }
