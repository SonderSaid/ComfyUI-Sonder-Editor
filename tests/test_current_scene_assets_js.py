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
    ltxHard: ltx.hard,
    ltxFps: mod.getTemplateFpsValues(ltx),
    ltxRecRes: mod.getRecommendedResolutions(ltx),
    ltxFrameConstraint: mod.getFrameConstraint(ltx),
    ltx480p43: mod.computeResolutionFromTier(640, 4, 3, ltx),
    free480p43: mod.computeResolutionFromTier(640, 4, 3, free),
    customDefaultEven: customDefault.hard.evenLatentDimensions,
    customDefault480p43: mod.computeResolutionFromTier(640, 4, 3, customDefault),
    customOffEven: customOff.hard.evenLatentDimensions,
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
    # Templates v2: hard/soft shape. Divisibility + frame rule + fps allow-list are HARD.
    assert result["ltxHard"]["dimensionStep"] == 32
    assert result["ltxHard"]["evenLatentDimensions"] is True
    assert result["ltxFps"] == [24, 25, 48, 50]
    # recommendedRes collapses orientation-swapped duplicates (total-pixel basis).
    assert result["ltxRecRes"] == [[1280, 720]]
    assert result["ltxFrameConstraint"] == {"step": 8, "offset": 1}
    assert result["ltx480p43"] == {"width": 768, "height": 576}
    assert result["free480p43"] == {"width": 739, "height": 554}
    # Legacy custom template with no explicit even flag migrates to the v1 default (true).
    assert result["customDefaultEven"] is True
    assert result["customDefault480p43"] == {"width": 768, "height": 576}
    assert result["customOffEven"] is False
    assert result["customOff480p43"] == {"width": 736, "height": 544}


def test_editor_settings_builtin_template_overrides():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")

    module_path = Path(__file__).resolve().parents[1] / "web" / "js" / "editor_settings.js"
    module_url = module_path.as_uri()
    stored_settings = {
        "modelTemplates": {
            "builtinOverrides": {
                # An edit to a built-in: only fps + name changed here.
                "ltx-2.3": {
                    "name": "LTX 2.3 (mine)",
                    "hard": {
                        "dimensionStep": 32, "dimensionOffset": 0, "evenLatentDimensions": True,
                        "frameStep": 8, "frameOffset": 1, "fps": [30],
                    },
                    "soft": {"minDimension": 64, "recommendedRes": [[1280, 720]]},
                },
                # A null override models a "reset to defaults" and must be dropped.
                "wan": None,
            },
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
console.log(JSON.stringify({
    ltxFps: mod.getTemplateFpsValues(mod.getTemplateById("ltx-2.3", settings)),
    ltxName: mod.getTemplateById("ltx-2.3", settings).name,
    ltxBuiltIn: mod.getTemplateById("ltx-2.3", settings).builtIn,
    wanFps: mod.getTemplateFpsValues(mod.getTemplateById("wan", settings)),
    overrideKeys: Object.keys(settings.modelTemplates.builtinOverrides).sort(),
    isBuiltinLtx: mod.isBuiltinModelTemplate("ltx-2.3"),
}));
""".replace("__MODULE_URL__", json.dumps(module_url)).replace("__SETTINGS__", json.dumps(stored_settings))

    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )

    result = json.loads(completed.stdout)
    # Override is applied over the code default, identity/builtIn flag preserved.
    assert result["ltxFps"] == [30]
    assert result["ltxName"] == "LTX 2.3 (mine)"
    assert result["ltxBuiltIn"] is True
    # Null override dropped → wan resolves to its default fps, not present in overrides.
    assert result["wanFps"] == [16]
    assert result["overrideKeys"] == ["ltx-2.3"]
    assert result["isBuiltinLtx"] is True


def test_editor_settings_launch_defaults_and_clip_label_options():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")

    module_path = Path(__file__).resolve().parents[1] / "web" / "js" / "editor_settings.js"
    module_url = module_path.as_uri()
    script = """
globalThis.window = {
    localStorage: {
        getItem() { return null; },
        setItem() {},
    },
    addEventListener() {},
};
const mod = await import(__MODULE_URL__);
const settings = mod.getEditorSettings();
console.log(JSON.stringify({
    defaultSavePreset: mod.DEFAULT_SAVE_PRESET,
    labelModes: mod.CLIP_LABEL_MODE_OPTIONS.map((option) => option.value),
    labelVertical: mod.CLIP_LABEL_VERTICAL_ALIGN_OPTIONS.map((option) => option.value),
    labelHorizontal: mod.CLIP_LABEL_HORIZONTAL_ALIGN_OPTIONS.map((option) => option.value),
    notifications: settings.notifications,
    playback: settings.playback,
    render: {
        defaultSavePreset: settings.render.defaultSavePreset,
        maxRenderCacheEntries: settings.render.maxRenderCacheEntries,
        trashRetentionDays: settings.render.trashRetentionDays,
        trashMaxSizeMB: settings.render.trashMaxSizeMB,
    },
    batchRender: settings.batchRender,
    prompts: settings.prompts,
    appearance: settings.appearance,
    projectDefaults: settings.projectDefaults,
    gallery: settings.gallery,
}));
""".replace("__MODULE_URL__", json.dumps(module_url))

    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )

    result = json.loads(completed.stdout)
    assert result["defaultSavePreset"] == "High Quality MP4"
    assert "duration_only" in result["labelModes"]
    assert result["labelVertical"] == ["top", "middle", "bottom"]
    assert result["labelHorizontal"] == ["start", "middle", "end"]
    assert result["notifications"] == {"toastDurationMs": 4000, "errorToastDurationMs": 0}
    assert result["playback"]["prebufferBoundaryDepth"] == 12
    assert result["playback"]["prebufferMaxEntries"] == 64
    assert result["playback"]["decodeConcurrency"] == 8
    assert result["render"] == {
        "defaultSavePreset": "High Quality MP4",
        "maxRenderCacheEntries": 0,
        "trashRetentionDays": 5,
        "trashMaxSizeMB": 5000,
    }
    assert result["batchRender"] == {"maxFramesPerChunk": 121}
    assert result["prompts"]["queueSectionBatch"] is False
    assert result["appearance"]["waveformAccent"] == "#89a4bc"
    assert result["appearance"]["clipLabelVerticalAlign"] == "middle"
    assert result["appearance"]["clipLabelHorizontalAlign"] == "start"
    assert result["appearance"]["editorMargins"] == {"top": 16, "bottom": 16, "sides": 0}
    assert result["projectDefaults"]["width"] == 1280
    assert result["projectDefaults"]["height"] == 720
    assert result["projectDefaults"]["newSceneDuration"] == 241
    assert result["projectDefaults"]["defaultFitMode"] == "cover"
    assert result["gallery"]["thumbnailSize"] == "small"


def test_editor_settings_detects_snapped_resolution_presets_and_session_memory():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")

    module_path = Path(__file__).resolve().parents[1] / "web" / "js" / "editor_settings.js"
    module_url = module_path.as_uri()
    script = """
globalThis.window = {
    localStorage: {
        getItem() { return null; },
        setItem() {},
    },
    addEventListener() {},
};
const mod = await import(__MODULE_URL__);
const ltx = mod.getTemplateById("ltx-2.3");
const free = mod.getTemplateById("free");
const cases = [
    ["16:9", 16, 9],
    ["21:9", 21, 9],
    ["9:16", 9, 16],
    ["4:3", 4, 3],
    ["3:4", 3, 4],
    ["1:1", 1, 1],
];
const detected = {};
for (const [label, a, b] of cases) {
    const resolved = mod.computeResolutionFromTier(720, a, b, ltx);
    const selection = mod.detectResolutionPresetSelections(resolved.width, resolved.height, ltx);
    detected[label] = {
        resolution: resolved,
        aspectValue: selection?.aspectValue || "",
        tierValue: selection?.tierValue || "",
        source: selection?.source || "",
    };
}
const freeResolution = mod.computeResolutionFromTier(720, 16, 9, free);
const freeSelection = mod.detectResolutionPresetSelections(freeResolution.width, freeResolution.height, free);
const memory = mod.createResolutionToolbarSelectionMemory();
const missing = memory.read("project-a", "scene-a");
const wrote = memory.write("project-a", "scene-a", {
    aspectValue: "0,0",
    tierValue: "custom",
    customAspectValue: "",
    customAspectLabel: "",
});
const remembered = memory.read("project-a", "scene-a");
const isolated = memory.read("project-a", "scene-b");
console.log(JSON.stringify({
    detected,
    free: {
        resolution: freeResolution,
        aspectValue: freeSelection?.aspectValue || "",
        tierValue: freeSelection?.tierValue || "",
    },
    memory: { missing, wrote, remembered, isolated },
}));
""".replace("__MODULE_URL__", json.dumps(module_url))

    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )

    result = json.loads(completed.stdout)
    assert result["detected"]["16:9"] == {
        "resolution": {"width": 960, "height": 576},
        "aspectValue": "16,9",
        "tierValue": "720",
        "source": "snapped",
    }
    assert result["detected"]["21:9"] == {
        "resolution": {"width": 1088, "height": 512},
        "aspectValue": "21,9",
        "tierValue": "720",
        "source": "snapped",
    }
    assert result["detected"]["9:16"] == {
        "resolution": {"width": 576, "height": 960},
        "aspectValue": "9,16",
        "tierValue": "720",
        "source": "snapped",
    }
    assert result["detected"]["4:3"]["aspectValue"] == "4,3"
    assert result["detected"]["3:4"]["aspectValue"] == "3,4"
    assert result["detected"]["1:1"]["aspectValue"] == "1,1"
    assert result["free"] == {
        "resolution": {"width": 960, "height": 540},
        "aspectValue": "16,9",
        "tierValue": "720",
    }
    assert result["memory"]["missing"] is None
    assert result["memory"]["wrote"] is True
    assert result["memory"]["remembered"] == {
        "aspectValue": "0,0",
        "tierValue": "custom",
        "customAspectValue": "",
        "customAspectLabel": "",
    }
    assert result["memory"]["isolated"] is None
