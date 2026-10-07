"""Shared create controls and the Launcher's Phase 1C client rules.

* ``project_format_controls.js`` reproduces the toolbar's tier, recommended-tier, aspect,
  resolution and fps logic exactly: the fixture was recorded from the toolbar's own
  methods before they moved (``tests/fixtures/characterize_format_controls.mjs``), and
  the toolbar now calls the shared functions.
* ``createSonderProject`` takes a template and channel choice; omitted, nothing changes.
* The machine-only sentence is one string on both sides; thumbnail modes and import
  side effects are pinned.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from server.request_locality import MACHINE_ONLY_MESSAGE

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "web" / "js"
FIXTURES = ROOT / "tests" / "fixtures"


def node():
    found = shutil.which("node")
    if not found:
        pytest.skip("Node unavailable")
    return found


def run_module(script):
    result = subprocess.run([node(), "--input-type=module", "-e", script.replace("__BASE__", json.dumps(JS.as_uri() + "/"))],
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_shared_controls_reproduce_the_recorded_toolbar_behaviour():
    result = subprocess.run([node(), str(FIXTURES / "characterize_format_controls.mjs"), JS.as_uri() + "/", "shared"],
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    recorded = json.loads((FIXTURES / "project_format_controls_characterization.json").read_text(encoding="utf-8"))
    assert json.loads(result.stdout) == recorded
    # Spot checks so the fixture can't silently be empty.
    assert recorded["ltx-2.3"]["recommended"] == 960 and recorded["modes"] == [
        "locked", "aspect-custom", "tier-custom-aspect", "free-custom"]
    assert any(tier["label"] == "1360×768" for tier in recorded["cogvideox-1.5"]["tiers"])


def test_the_toolbar_calls_the_shared_functions():
    widget = (JS / "editor_widget.js").read_text(encoding="utf-8")
    for method, shared in [("_templateResolutionTiers", "templateResolutionTiers"),
                           ("_recommendedTierValue", "recommendedTierValue"),
                           ("_resolveAspectCustomResolution", "resolveAspectCustomResolution"),
                           ("_findMatchingAspectPreset", "findMatchingAspectPreset"),
                           ("_findNearestResolutionTier", "findNearestResolutionTier"),
                           ("_resolutionControlMode", "resolutionControlMode"),
                           ("_snapSceneFpsToTemplate", "snapFpsToTemplate")]:
        body = re.search(rf"\n    {method}\([^\n]*\) \{{\n(.*?)\n    \}}\n", widget, re.S).group(1)
        assert f"{shared}(" in body and body.count("\n") <= 3, method


def test_create_takes_the_dialog_choice_and_keeps_its_defaults_without_one():
    value = run_module(r"""
globalThis.window = { localStorage: { getItem() { return null; }, setItem() {} } };
globalThis.localStorage = window.localStorage;
const pc = await import(__BASE__ + 'project_create.js');
const settings = (await import(__BASE__ + 'editor_settings.js')).normalizeEditorSettings();
async function post(extra) {
    let body = null;
    await pc.createSonderProject({ name: 'A', fps: 30, width: 1280, height: 720, settings, apiURL: p => p, ...extra,
        fetchImpl: async (url, init = {}) => {
            if (url === '/sonder-editor/project') { body = JSON.parse(init.body); return { ok: true, status: 201, json: async () => ({ project_id: 'p', name: 'A' }) }; }
            if (url === '/sonder-editor/projects') return { ok: true, status: 200, json: async () => ({ projects: [{ project_id: 'p', name: 'A', path: '/x/A', scene_count: 1 }] }) };
            throw new Error('unexpected ' + url);
        } });
    return body;
}
console.log(JSON.stringify({ chosen: await post({ templateId: 'ltx-2.3', channelTemplateId: 'single' }), defaults: await post({}),
    defaultTemplate: settings.projectDefaults.defaultTemplateId }));
""")
    chosen, defaults = value["chosen"], value["defaults"]
    assert chosen["template_id"] == "ltx-2.3" and chosen["fps"] == 25  # snapped to LTX's allowed rates
    assert chosen["frame_constraint"] == {"step": 8, "offset": 1}
    assert defaults["template_id"] == value["defaultTemplate"] and defaults["fps"] == 30


def test_machine_only_sentence_thumbnail_modes_and_no_import_side_effects():
    value = run_module(r"""
globalThis.window = { addEventListener() {}, localStorage: { getItem() { return null; } } };
globalThis.localStorage = window.localStorage;
globalThis.document = { body: { dataset: {} }, createElement() { throw Error('DOM touched on import'); }, querySelector() { return null; }, addEventListener() {} };
const fc = await import(__BASE__ + 'project_format_controls.js');
const setup = await import(__BASE__ + 'sdk_setup_client.js');
const projects = await import(__BASE__ + 'launcher_projects.js');
await import(__BASE__ + 'launcher_dialog.js');
await import(__BASE__ + 'sdk_landing.js');
const mode = (thumbnails, reduced) => projects.thumbnailMode({ thumbnails }, reduced);
console.log(JSON.stringify({ message: setup.MACHINE_ONLY_MESSAGE,
    modes: [mode('animated', false), mode('animated', true), mode('still', true), mode('off', false), mode(undefined, false)],
    above: [fc.aboveRecommendedMax(1920, 1088, { id: 'x', soft: { maxRes: [1280, 736] } }), fc.aboveRecommendedMax(1280, 736, { id: 'x', soft: { maxRes: [1280, 720] } })] }));
""")
    assert value["message"] == MACHINE_ONLY_MESSAGE
    assert value["modes"] == ["animated", "still", "still", "off", "animated"]
    assert value["above"] == [[1280, 736], None]
