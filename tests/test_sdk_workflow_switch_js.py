"""The Sonder App page follows a workflow switch: the editor remounts its Controls
panel on the new host, and the tab's title, re-claim label and exit flag name the
new workflow (web/js/editor_widget.js, web/js/tab_entry.js)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r"""
import assert from 'node:assert/strict';
class N {
  constructor(tag) { this.tagName = String(tag).toUpperCase(); this.children = []; this.style = {}; this.dataset = {}; }
  appendChild(c) { this.children.push(c); return c; } append(...cs) { cs.forEach((c) => this.appendChild(c)); }
  addEventListener() {} removeEventListener() {} setAttribute() {} querySelectorAll() { return []; } querySelector() { return null; }
}
globalThis.document = { createElement: (t) => new N(t), body: new N("body"), activeElement: null,
  addEventListener() {}, removeEventListener() {}, querySelectorAll: () => [] };
globalThis.window = { addEventListener() {}, removeEventListener() {}, localStorage: null,
  comfyAPI: { api: { api: { apiURL: (path) => path } } } };
globalThis.localStorage = { getItem() { return null; }, setItem() {} };
globalThis.location = { href: "http://test/" };
globalThis.requestAnimationFrame = () => 0;
const { EditorWidget } = await import(__WIDGET__);

const events = [];
const oldHost = { setVisible: (v) => events.push(["old visible", v]) };
const newHost = { setVisible: (v) => events.push(["new visible", v]) };
const w = Object.create(EditorWidget.prototype);
Object.assign(w, {
  _destroyed: false, _controlsHost: oldHost,
  _controlsPanelHandle: { destroy: () => events.push(["panel destroyed"]) },
  _controlsPanelEl: { remove: () => events.push(["element removed"]) },
  _settings: { layout: { fullscreenSidebarContent: "controls" } },
  _showFullscreenSidebarContent(value, options) {
    events.push(["show", value, options.persist, this._controlsHost === newHost, this._controlsPanelHandle, this._controlsPanelEl]);
  },
});
w.replaceControlsHost(newHost);
assert.deepEqual(events, [
  ["panel destroyed"], ["element removed"], ["old visible", false],
  ["show", "controls", false, true, null, null],
]);
assert.equal(w._controlsHost, newHost);
// Never mounted (another sidebar tab, or not yet opened): just the host.
events.length = 0;
const later = { setVisible: () => events.push(["later"]) };
w._controlsPanelHandle = null; w._controlsPanelEl = null;
w.replaceControlsHost(later);
assert.deepEqual(events, [["new visible", false]]);
assert.equal(w._controlsHost, later);
console.log("ok");
"""


def test_the_editor_remounts_controls_on_the_new_host():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend tests")
    script = SCRIPT.replace("__WIDGET__", repr((ROOT / "web/js/editor_widget.js").as_uri()))
    result = subprocess.run([node, "--input-type=module"], input=script, capture_output=True, text=True,
                            encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


def test_the_tab_follows_the_switch_for_title_reclaim_and_exit():
    source = (ROOT / "web/js/tab_entry.js").read_text(encoding="utf-8")
    sdk = source[source.index("async function runSdkTab"):source.index("async function main")]
    # Re-claims and the exit flag read the host's current workflow, never the first one.
    assert "workflow_label: host.templateName ?" in sdk
    assert re.search(r"template_id: host\.templateId,\s+template_name: host\.templateName,", sdk)
    # The page-load name seeds only the first title and the host.
    assert sdk.count("stateResp.payload.template_name") == 2
    switch = sdk[sdk.index("host.onWorkflowChanged"):]
    for needle in ("document.title = workflowTitle(change.name)", "editor.replaceControlsHost(host.getControlsHost())",
                   "Switched to", "still generating with", "will render with", "/requirements"):
        assert needle in switch, needle
