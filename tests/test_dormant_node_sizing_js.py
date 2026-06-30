import json
import shutil
import subprocess
from pathlib import Path

import pytest


def run_node_helper(script_body):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")

    module_url = (Path(__file__).resolve().parents[1] / "web" / "js" / "dormant_node_sizing.js").as_uri()
    script = f"""
const mod = await import({json.dumps(module_url)});
{script_body}
"""
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_dormant_node_height_round_trips_and_safety_floors():
    result = run_node_helper(
        """
const overhead = mod.computeNodeOverhead([440, 230]);
const widget = mod.widgetHeightFromNodeHeight(430, overhead);
const total = mod.nodeHeightFromWidgetHeight(widget, overhead);
const floorWidget = mod.widgetHeightFromNodeHeight(120, overhead);
const floorTotal = mod.nodeHeightFromWidgetHeight(80, overhead);
const safe = mod.clampNodeSizeToSafety(180, 210, { minHeight: 230 });
console.log(JSON.stringify({ overhead, widget, total, floorWidget, floorTotal, safe }));
"""
    )

    assert result == {
        "overhead": 80,
        "widget": 350,
        "total": 430,
        "floorWidget": 150,
        "floorTotal": 230,
        "safe": [240, 230],
    }


def test_dormant_auto_resize_policy_matches_module_metadata():
    result = run_node_helper(
        """
console.log(JSON.stringify({
    none: mod.shouldAutoResizeDormantModule("", null),
    assets: mod.shouldAutoResizeDormantModule("assets", { hostSizing: "fill", nodeResize: "manual" }),
    queue: mod.shouldAutoResizeDormantModule("queue", { hostSizing: "fill", nodeResize: "manual" }),
    preview: mod.shouldAutoResizeDormantModule("preview", { hostSizing: "fill", nodeResize: "manual" }),
    fillDefault: mod.shouldAutoResizeDormantModule("custom", { hostSizing: "fill" }),
    autoDefault: mod.shouldAutoResizeDormantModule("custom", { hostSizing: "auto" }),
}));
"""
    )

    assert result == {
        "none": False,
        "assets": False,
        "queue": False,
        "preview": False,
        "fillDefault": False,
        "autoDefault": True,
    }


def test_auto_resize_helper_grows_once_and_never_shrinks_saved_size():
    result = run_node_helper(
        """
const first = mod.computeAutoResizeNodeHeight({
    currentNodeHeight: 380,
    measuredWidgetHeight: 340,
    overhead: 80,
});
const second = mod.computeAutoResizeNodeHeight({
    currentNodeHeight: first.nodeHeight,
    measuredWidgetHeight: 340,
    overhead: 80,
});
const savedLarger = mod.computeAutoResizeNodeHeight({
    currentNodeHeight: 560,
    measuredWidgetHeight: 340,
    overhead: 80,
});
console.log(JSON.stringify({ first, second, savedLarger }));
"""
    )

    assert result["first"] == {
        "nodeHeight": 420,
        "widgetHeight": 340,
        "shouldResize": True,
    }
    assert result["second"] == {
        "nodeHeight": 420,
        "widgetHeight": 340,
        "shouldResize": False,
    }
    assert result["savedLarger"] == {
        "nodeHeight": 560,
        "widgetHeight": 480,
        "shouldResize": False,
    }
