import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_derive_current_scene_asset_ids_covers_scene_references():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")

    module_path = Path(__file__).resolve().parents[1] / "web" / "js" / "current_scene_assets.js"
    encoded = base64.b64encode(module_path.read_bytes()).decode("ascii")
    script = """
const mod = await import("data:text/javascript;base64,__MODULE__");
const assets = [
    { asset_id: "clip", path: "media/clip.mp4" },
    { asset_id: "motion", path: "media/motion.mp4" },
    { asset_id: "audio", path: "media/audio.wav" },
    { asset_id: "guide", path: "media/guide.png", trashed_at: "2026-01-01T00:00:00" },
    { asset_id: "unused", path: "media/unused.png" },
];
const scene = {
    clips: [
        { source_path: "media/clip.mp4", muted: true },
        { source_path: "media/motion.mp4", role: "motion_driver", hidden: true },
    ],
    audio_tracks: [
        { source_path: "media/audio.wav", muted: true },
    ],
    guide_frames: [
        { asset_id: "guide", muted: true },
    ],
};
const ids = mod.deriveCurrentSceneAssetIds(scene, assets).sort();
console.log(JSON.stringify(ids));
""".replace("__MODULE__", encoded)

    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(completed.stdout) == ["audio", "clip", "guide", "motion"]


def test_editor_settings_even_latent_template_resolution():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")

    module_path = Path(__file__).resolve().parents[1] / "web" / "js" / "editor_settings.js"
    module_url = module_path.as_uri()
    stored_settings = {
        "modelTemplates": {
            "customTemplates": [
                {
                    "id": "custom-default",
                    "name": "Custom Default",
                    "constraints": {
                        "width": {"step": 32, "min": 64, "max": 4096},
                        "height": {"step": 32, "min": 64, "max": 4096},
                    },
                },
                {
                    "id": "custom-off",
                    "name": "Custom Off",
                    "constraints": {
                        "width": {"step": 32, "min": 64, "max": 4096},
                        "height": {"step": 32, "min": 64, "max": 4096},
                        "evenLatentDimensions": False,
                    },
                },
            ],
        },
    }
    script = """
const storedSettings = __SETTINGS__;
globalThis.window = {
    localStorage: {
        getItem(key) {
            return key === "sonder-editor-settings" ? JSON.stringify(storedSettings) : null;
        },
        setItem() {},
    },
    addEventListener() {},
};
const mod = await import(__MODULE_URL__);
const settings = mod.getEditorSettings();
const ltx = mod.getTemplateById("ltx-2.3", settings);
const free = mod.getTemplateById("free", settings);
const customDefault = mod.getTemplateById("custom-default", settings);
const customOff = mod.getTemplateById("custom-off", settings);
console.log(JSON.stringify({
    ltxConstraints: ltx.constraints,
    ltx480p43: mod.computeResolutionFromTier(640, 4, 3, ltx),
    free480p43: mod.computeResolutionFromTier(640, 4, 3, free),
    customDefaultEven: customDefault.constraints.evenLatentDimensions,
    customDefault480p43: mod.computeResolutionFromTier(640, 4, 3, customDefault),
    customOffEven: customOff.constraints.evenLatentDimensions,
    customOff480p43: mod.computeResolutionFromTier(640, 4, 3, customOff),
}));
""".replace("__MODULE_URL__", json.dumps(module_url)).replace("__SETTINGS__", json.dumps(stored_settings))

    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )

    result = json.loads(completed.stdout)
    assert result["ltxConstraints"]["width"]["max"] == 4096
    assert result["ltxConstraints"]["height"]["max"] == 4096
    assert result["ltxConstraints"]["frames"]["max"] == 481
    assert result["ltxConstraints"]["fps"] == {"min": 24, "max": 50}
    assert result["ltxConstraints"]["batchMaxFrames"] == 121
    assert result["ltxConstraints"]["evenLatentDimensions"] is True
    assert result["ltx480p43"] == {"width": 768, "height": 576}
    assert result["free480p43"] == {"width": 739, "height": 554}
    assert result["customDefaultEven"] is True
    assert result["customDefault480p43"] == {"width": 768, "height": 576}
    assert result["customOffEven"] is False
    assert result["customOff480p43"] == {"width": 736, "height": 544}
