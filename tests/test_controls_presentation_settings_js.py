"""Controls presentation settings (SDK v2 Phase 4): the browser-local Controls
settings, the typed writer every editor mode's panel uses, and the Launcher's
Controls line. Pure modules, run in Node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r'''
import assert from 'node:assert/strict';
globalThis.localStorage = { getItem() { return null; }, setItem() {} };
const settings = await import(SETTINGS_URL);
const launcher = await import(LAUNCHER_URL);

// Defaults and normalization: Rows, Show advanced off, scale 1.
const defaults = settings.normalizeEditorSettings();
assert.deepEqual(defaults.controls, { view: 'rows', showAdvanced: false });
assert.equal(defaults.layout.scaleControls, 1);
const invalid = settings.normalizeEditorSettings({ controls: { view: 'grid', showAdvanced: 'yes' }, layout: { scaleControls: 9 } });
assert.deepEqual(invalid.controls, { view: 'rows', showAdvanced: false });
assert.equal(invalid.layout.scaleControls, 2);
const valid = settings.normalizeEditorSettings({ controls: { view: 'cards', showAdvanced: true }, layout: { scaleControls: 0.8 } });
assert.deepEqual(valid.controls, { view: 'cards', showAdvanced: true });
assert.equal(valid.layout.scaleControls, 0.8);
// The Controls scale is new: it never inherits the legacy global scale key.
assert.equal(settings.normalizeEditorSettings({}).layout.scaleControls, 1);

// The typed writer: one whitelist, per-key reading, refusal instead of coercion.
const patch = settings.controlsPanelSettingPatch;
assert.deepEqual(patch('render.takePlacementMuted', true), { render: { takePlacementMuted: true } });
assert.deepEqual(patch('controls.showAdvanced', false), { controls: { showAdvanced: false } });
assert.deepEqual(patch('controls.view', 'cards'), { controls: { view: 'cards' } });
assert.deepEqual(patch('batchRender.maxFramesPerChunk', '97'), { batchRender: { maxFramesPerChunk: 97 } });
assert.deepEqual(patch('batchRender.maxFramesPerChunk', 0), { batchRender: { maxFramesPerChunk: 0 } });
assert.deepEqual(patch('batchRender.maxFramesPerChunk', 10000), { batchRender: { maxFramesPerChunk: 10000 } });
for (const bad of ['', ' ', '-1', '1.5', '10001', 'abc', null]) {
    assert.throws(() => patch('batchRender.maxFramesPerChunk', bad), /whole number from 0 to 10000/, String(bad));
}
assert.throws(() => patch('render.takePlacementMuted', 'true'), /on or off/);
assert.throws(() => patch('controls.view', 'grid'), /Rows or Cards/);
for (const key of ['render.trashRetentionDays', 'layout.scaleControls', '__proto__.x', 'constructor', 'x']) {
    assert.throws(() => patch(key, 1), /can't be changed from Controls/, key);
}

// The Launcher's Controls line: Advanced ones are counted, not listed.
assert.equal(launcher.controlsLine([]), 'No Controls exposed in this workflow.');
assert.equal(launcher.controlsLine([{ label: 'Seed' }, { label: 'Model', advanced: true }, { label: 'Steps' }]),
    'Controls: Seed, Steps, +1 advanced');
assert.equal(launcher.controlsLine([{ label: 'Model', advanced: true }]), 'Controls: +1 advanced');
console.log('ok');
'''


def test_controls_presentation_settings_and_launcher_line(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node required")
    script = (SCRIPT.replace("SETTINGS_URL", json.dumps((ROOT / "web/js/editor_settings.js").as_uri()))
              .replace("LAUNCHER_URL", json.dumps((ROOT / "web/js/launcher_workflows.js").as_uri())))
    path = tmp_path / "settings_test.mjs"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
