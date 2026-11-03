"""Render Queue count and the half-set selection readout (hardening Phase 5, 5C).

* The Render Queue header read "(0)" on load and "(21)" after the first take:
  the load read the queue only while it was expanded, and every later refresh
  read it collapsed too. The load now reads it either way, and until the list
  is known the header shows no count rather than a false 0.
* A lone In or Out is a draft: Generate still runs what is stored before it
  (the full scene, since a draft clears the stored range), and the readout's
  tooltip says so.

Each case drives the real `EditorWidget` prototype under Node; only the DOM
and the network are stood in for.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WIDGET = ROOT / "web" / "js" / "editor_widget.js"

SCRIPT = r"""
globalThis.document = { createElement: () => ({ style: {}, appendChild() {}, addEventListener() {} }),
  body: {}, activeElement: null, addEventListener() {}, removeEventListener() {}, querySelectorAll: () => [] };
globalThis.window = { addEventListener() {}, removeEventListener() {}, localStorage: null,
  comfyAPI: { api: { api: { apiURL: (path) => path } } }, prompt: () => null };
globalThis.localStorage = { getItem() { return null; }, setItem() {} };
globalThis.location = { href: "http://test/" };
globalThis.requestAnimationFrame = () => 0;

const { EditorWidget } = await import(__WIDGET__);
const out = {};

const queue = Object.create(EditorWidget.prototype);
queue._queueHeaderLabel = { textContent: "" };
queue._queueExpanded = false;
queue._renderQueue = [];
queue._renderQueueKnown = false;
queue._updateQueueHeaderLabel();
out.unknown = queue._queueHeaderLabel.textContent;
queue._renderQueue = Array.from({ length: 21 }, (_, i) => ({ id: i }));
queue._renderQueueKnown = true;
queue._updateQueueHeaderLabel();
out.known = queue._queueHeaderLabel.textContent;
queue._renderQueue = [];
queue._updateQueueHeaderLabel();
out.knownEmpty = queue._queueHeaderLabel.textContent;

function readout(fields) {
  const w = Object.create(EditorWidget.prototype);
  Object.assign(w, {
    _genReadout: { textContent: "", title: "stale" },
    _frameToTimecode: (frame) => `f${frame}`,
    _updateGenDurationHint() {},
    selectionStart: 0, selectionEnd: 0, _selectionDraftAnchor: null,
  }, fields);
  try { w._updateGenReadout(); } catch (_error) { /* past the draft branch: only the title matters */ }
  return { text: w._genReadout.textContent, title: w._genReadout.title };
}
out.loneOut = readout({ _selectionDraftAnchor: { edge: "end", frame: 17 } });
out.loneIn = readout({ _selectionDraftAnchor: { edge: "start", frame: 4 } });
out.full = readout({});
console.log(JSON.stringify(out));
"""


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    return node


def _run():
    script = SCRIPT.replace("__WIDGET__", json.dumps(WIDGET.as_uri()))
    result = subprocess.run([_node(), "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_render_queue_count_shows_only_once_the_list_is_known():
    out = _run()
    assert out["unknown"] == "▸ Render Queue"
    assert out["known"] == "▸ Render Queue (21)"
    assert out["knownEmpty"] == "▸ Render Queue (0)"


def test_the_load_reads_the_queue_even_while_collapsed():
    source = WIDGET.read_text(encoding="utf-8")
    load = source.split('this._fetchRenderQueue({ reason: "load_project" });', 1)[0].rsplit("\n", 4)
    assert "_queueExpanded" not in "\n".join(load[-3:]), "the load read must not wait for the panel to expand"
    assert re.search(r"this\._renderQueue = \[\];\s*this\._renderQueueKnown = false;", source)


def test_a_half_set_selection_says_which_range_generate_uses():
    out = _run()
    assert out["loneOut"]["text"] == "Out f17 set · Choose In"
    assert out["loneOut"]["title"] == "Until you choose In, Generate uses the full scene."
    assert out["loneIn"]["title"] == "Until you choose Out, Generate uses the full scene."
    assert out["full"]["text"] == "Full scene"
    assert out["full"]["title"] == ""
