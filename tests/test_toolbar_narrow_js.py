"""The editor toolbar stacks its groups on rows when narrow (hardening Phase 5, 5C).

At a 600 px Sonder App window the four `nowrap` groups overlapped, and at the
500 px minimum they clipped. Below a width the toolbar wraps: history and
utilities share the first row, the tools and the scene geometry take a full
row each. It goes back to one row only above a second, wider width, so a width
between the two never flips it back and forth. The toolbar is shared by every
host, so the layout follows the width in fullscreen, mounted and the App.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CHROME = ROOT / "web" / "js" / "editor_top_chrome.js"

SCRIPT = r"""
globalThis.document = { createElement: () => ({ style: {}, appendChild() {}, addEventListener() {} }),
  body: {}, activeElement: null, addEventListener() {}, removeEventListener() {}, querySelectorAll: () => [] };
globalThis.window = { addEventListener() {}, removeEventListener() {}, localStorage: null,
  comfyAPI: { api: { api: { apiURL: (path) => path } } } };
globalThis.localStorage = { getItem() { return null; }, setItem() {} };
globalThis.location = { href: "http://test/" };
const M = await import(__CHROME__);
const walk = (start, widths) => { let narrow = start; return widths.map(w => (narrow = M.toolbarNarrowNext(narrow, w))); };
const part = (style = {}) => ({ style: { ...style } });
// The wide layout as buildEditorToolbar leaves it: the geometry group's basis
// comes from its `flex: 1 0.5 720px` shorthand, its margin pins it right.
const parts = { toolbar: part(), history: part(), tools: part({ flexBasis: "auto" }),
  geometry: part({ flexBasis: "720px", marginLeft: "auto" }), utils: part({ marginLeft: "" }), dividers: [part(), part()] };
M.applyNarrowToolbar(parts, true);
const narrow = JSON.parse(JSON.stringify(parts));
M.applyNarrowToolbar(parts, false);
console.log(JSON.stringify({
  below: M.TOOLBAR_NARROW_BELOW, above: M.TOOLBAR_WIDE_ABOVE,
  down: walk(false, [1200, 800, 761, 760, 759, 600, 500]),
  up: walk(true, [500, 759, 790, 800, 801, 1200]),
  unknown: [M.toolbarNarrowNext(true, 0), M.toolbarNarrowNext(false, NaN)],
  narrow, wide: { ...parts, wide: undefined },
}));
"""


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    return node


def _run():
    script = SCRIPT.replace("__CHROME__", json.dumps(CHROME.as_uri()))
    result = subprocess.run([_node(), "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_switch_has_hysteresis_and_ignores_unmeasured_widths():
    out = _run()
    assert out["below"] < out["above"]
    # Shrinking: narrow only below the lower width.
    assert out["down"] == [False, False, False, False, True, True, True]
    # Growing: wide again only above the upper width.
    assert out["up"] == [True, True, True, True, False, False]
    # A zero or unknown width (a hidden or detached toolbar) keeps the layout.
    assert out["unknown"] == [True, False]


def test_narrow_rows_and_the_wide_layout_restored():
    out = _run()
    narrow, wide = out["narrow"], out["wide"]
    assert narrow["toolbar"]["style"]["flexWrap"] == "wrap"
    assert [narrow[k]["style"]["order"] for k in ("history", "utils", "tools", "geometry")] == ["0", "1", "2", "3"]
    assert narrow["tools"]["style"]["flexBasis"] == "100%"
    assert narrow["geometry"]["style"]["flexBasis"] == "100%"
    assert all(d["style"]["display"] == "none" for d in narrow["dividers"])
    assert wide["toolbar"]["style"]["flexWrap"] == "nowrap"
    assert all(wide[k]["style"]["order"] == "" for k in ("history", "utils", "tools", "geometry"))
    assert wide["geometry"]["style"]["marginLeft"] == "auto", "the wide layout keeps its tools-to-geometry gap"
    # Narrow then wide puts back the group's own basis, not `auto`.
    assert wide["geometry"]["style"]["flexBasis"] == "720px"
    assert wide["tools"]["style"]["flexBasis"] == "auto"
    assert all(d["style"]["display"] == "" for d in wide["dividers"])
