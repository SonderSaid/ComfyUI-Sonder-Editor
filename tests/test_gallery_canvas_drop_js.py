import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_node(script_body: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")

    module_url = (ROOT / "web" / "js" / "gallery_canvas_drop.js").as_uri()
    script = f"""
const drop = await import({json.dumps(module_url)});
{script_body}
"""
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_asset_loader_resolution_prefers_video_loaders_without_image_fallback():
    result = _run_node(
        """
console.log(JSON.stringify({
    image: drop.resolveAssetLoader("image", {}),
    videoWithVhs: drop.resolveAssetLoader("video", {
        VHS_LoadVideo: {},
        LoadVideo: {},
    }),
    videoWithCore: drop.resolveAssetLoader("video", { LoadVideo: {} }),
    videoWithoutLoader: drop.resolveAssetLoader("video", {}),
    audio: drop.resolveAssetLoader("audio", { LoadAudio: {} }),
    artifact: drop.resolveAssetLoader("artifact", {}),
}));
"""
    )

    assert result == {
        "image": {"nodeType": "LoadImage", "widgetName": "image"},
        "videoWithVhs": {"nodeType": "VHS_LoadVideo", "widgetName": "video"},
        "videoWithCore": {"nodeType": "LoadVideo", "widgetName": "file"},
        "videoWithoutLoader": None,
        "audio": {"nodeType": "LoadAudio", "widgetName": "audio"},
        "artifact": None,
    }


def test_uploaded_asset_is_added_to_combo_before_assignment():
    result = _run_node(
        """
const events = [];
const widget = {
    name: "image",
    value: "example.png",
    options: { values: ["example.png"] },
    callback(value) {
        events.push({
            kind: "callback",
            value,
            values: [...this.options.values],
        });
    },
};
const node = {
    widgets: [widget],
    onWidgetChanged(name, value, oldValue, changedWidget) {
        events.push({
            kind: "changed",
            name,
            value,
            oldValue,
            sameWidget: changedWidget === widget,
        });
    },
};

const first = drop.assignUploadedAsset(
    node,
    "image",
    "sonder_assets/example.png",
);
const second = drop.assignUploadedAsset(
    node,
    "image",
    "sonder_assets/example.png",
);

console.log(JSON.stringify({
    first,
    second,
    value: widget.value,
    values: widget.options.values,
    events,
}));
"""
    )

    assert result == {
        "first": True,
        "second": True,
        "value": "sonder_assets/example.png",
        "values": ["example.png", "sonder_assets/example.png"],
        "events": [
            {
                "kind": "callback",
                "value": "sonder_assets/example.png",
                "values": ["example.png", "sonder_assets/example.png"],
            },
            {
                "kind": "changed",
                "name": "image",
                "value": "sonder_assets/example.png",
                "oldValue": "example.png",
                "sameWidget": True,
            },
            {
                "kind": "callback",
                "value": "sonder_assets/example.png",
                "values": ["example.png", "sonder_assets/example.png"],
            },
            {
                "kind": "changed",
                "name": "image",
                "value": "sonder_assets/example.png",
                "oldValue": "sonder_assets/example.png",
                "sameWidget": True,
            },
        ],
    }


def test_uploaded_asset_assignment_handles_missing_options_and_widget():
    result = _run_node(
        """
const widget = { name: "file", value: "" };
const node = { widgets: [widget] };
console.log(JSON.stringify({
    assigned: drop.assignUploadedAsset(node, "file", "sonder_assets/clip.mp4"),
    missing: drop.assignUploadedAsset(node, "video", "sonder_assets/clip.mp4"),
    value: widget.value,
    values: widget.options.values,
}));
"""
    )

    assert result == {
        "assigned": True,
        "missing": False,
        "value": "sonder_assets/clip.mp4",
        "values": ["sonder_assets/clip.mp4"],
    }
