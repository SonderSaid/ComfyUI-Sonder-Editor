"""Gallery side of Controls at run: renderer, supplied lookup, Copy and search."""
import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
COPY_MODULE = ROOT / "web" / "js" / "gallery_copy_values.js"
RENDERERS = ROOT / "web" / "js" / "tracked_metadata_renderers.js"
GALLERY = ROOT / "web" / "js" / "shared_asset_gallery.js"

FAKE_DOM = r"""
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.style = {}; this.textContent = ''; this.title = ''; }
  get firstChild() { return this.children[0] || null; }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); return node; }
  get text() { return [this.textContent, ...this.children.map((c) => c.text)].join(' ').trim(); }
}
globalThis.document = { createElement: (tag) => new El(tag) };
const style = (el) => el;
const CHROME = { textDim: '#888' };
"""

SECTION = {
    "label": "Controls at run", "display_type": "controls",
    "fields": {"Seed": "18446744073709551615", "Steps": "20", "Prompt": "x" * 512 + "… [truncated, full value in the source prompt]",
               "Frame rate": 24},
    "controls": [
        {"binding_id": "s1", "field": "Seed", "kind": "SEED", "seed_mode": "increment",
         "targets": [{"path": "3", "input": "seed"}]},
        {"binding_id": "st", "field": "Steps", "kind": "INT", "origin": "local", "group": "Sampling",
         "targets": [{"path": "3", "input": "steps"}]},
        {"binding_id": "pr", "field": "Prompt", "kind": "STRING", "targets": [{"path": "2", "input": "text"}]},
        {"binding_id": "fr", "field": "Frame rate", "kind": "FLOAT", "section": "editor",
         "targets": [{"path": "1", "input": "fps"}]},
    ],
    "supplied": [{"section_index": 0, "field": "steps", "binding_id": "st"},
                 {"section_index": 0, "field": "cfg", "binding_id": "missing"},
                 {"section_index": 5, "field": "steps", "binding_id": "st"}],
}
COLLECTOR = {"label": "Sampler", "source_node_id": "3", "display_type": None, "fields": {"steps": 20, "cfg": 7}}


def _run(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for gallery renderer tests")
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


def _imports():
    return (f"import {{ CONTROLS_TRUNCATED_MARKER, resolveTrackedFieldCopy }} from {json.dumps(COPY_MODULE.as_uri())};\n"
            f"import {{ renderTrackedSectionBody, controlsSuppliedLookup }} "
            f"from {json.dumps(RENDERERS.as_uri())};\n")


def test_server_and_gallery_share_the_controls_marker():
    module = ast.parse((ROOT / "server" / "controls_snapshot.py").read_text(encoding="utf-8"))
    python_marker = next(ast.literal_eval(node.value) for node in module.body
                         if isinstance(node, ast.Assign)
                         and any(isinstance(t, ast.Name) and t.id == "TRUNCATED_MARKER" for t in node.targets))
    assert _run(_imports() + "console.log(JSON.stringify(CONTROLS_TRUNCATED_MARKER));") == python_marker


def test_copy_takes_complete_values_and_refuses_a_capped_one():
    result = _run(_imports() + f"""
const entry = {json.dumps(SECTION)};
console.log(JSON.stringify({{
  seed: resolveTrackedFieldCopy(entry, 'Seed'),
  rate: resolveTrackedFieldCopy(entry, 'Frame rate'),
  missing: resolveTrackedFieldCopy({{ ...entry, fields: {{ ...entry.fields, Seed: null }} }}, 'Seed'),
  prompt: resolveTrackedFieldCopy(entry, 'Prompt'),
}}));
""")
    assert result["seed"] == {"kind": "value", "label": "Copy value", "text": "18446744073709551615"}
    assert result["rate"]["text"] == "24"
    assert result["missing"]["kind"] == "unavailable"
    assert result["prompt"]["kind"] == "unavailable" and result["prompt"]["text"] == ""


def test_supplied_lookup_validates_every_reference():
    result = _run(_imports() + f"""
const collector = {json.dumps(COLLECTOR)};
const controls = {json.dumps(SECTION)};
const lookup = controlsSuppliedLookup([collector, controls]);
console.log(JSON.stringify({{
  steps: lookup.supplied(collector, 'steps'),
  cfg: lookup.supplied(collector, 'cfg'),
  none: controlsSuppliedLookup([{{...collector, display_type: 'subgraph'}}, controls]).supplied(collector, 'steps'),
}}));
""")
    assert result["steps"] == {"binding_id": "st", "label": "Steps"}
    assert result["cfg"] is None  # its binding is not recorded
    assert result["none"] is None


def test_renderer_groups_fields_and_decorates_through_gallery_cells():
    result = _run(FAKE_DOM + _imports() + f"""
const entry = {json.dumps(SECTION)};
const grids = [];
const ctx = {{ style, CHROME,
  renderFieldGrid: (e, keys, decorate) => {{
    const grid = document.createElement('div');
    for (const key of keys) {{
      const cell = document.createElement('div');
      if (key === 'Prompt') cell.title = 'Pinned to top';
      cell.appendChild(document.createElement('div'));
      cell.firstChild.textContent = key;
      decorate(cell, key);
      grid.appendChild(cell);
    }}
    grids.push({{ keys, titles: grid.children.map((c) => c.title), heads: grid.children.map((c) => c.firstChild.text) }});
    return grid;
  }},
}};
const tagged = renderTrackedSectionBody(entry, ctx);
const tagGrids = grids.splice(0);
console.log(JSON.stringify({{ consumed: tagged.consumedFields, generic: tagged.pinnedFieldsGeneric,
  text: tagged.dom.text, tagGrids }}));
""")
    assert sorted(result["consumed"]) == ["Frame rate", "Prompt", "Seed", "Steps"]
    assert result["generic"] is True
    assert "switched off" in result["text"]
    assert [grid["keys"] for grid in result["tagGrids"]] == [["Seed", "Prompt"], ["Steps"], ["Frame rate"]]
    assert "SAMPLING" not in result["text"] and "Sampling" in result["text"] and "Editor" in result["text"]
    seed_grid = result["tagGrids"][0]
    assert seed_grid["titles"][0] == "Control s1 · seed increment"
    assert seed_grid["heads"][0] == "Seed · increment"
    assert result["tagGrids"][1]["titles"][0] == "Control st · App only"
    assert result["tagGrids"][0]["titles"][1] == "Pinned to top\nControl pr"


def test_renderer_without_a_grid_host_falls_back_to_the_generic_grid():
    result = _run(FAKE_DOM + _imports() + f"""
const out = renderTrackedSectionBody({json.dumps(SECTION)}, {{ style, CHROME }});
console.log(JSON.stringify({{ consumed: out.consumedFields }}));
""")
    assert result["consumed"] == []


def test_gallery_wires_the_controls_lookup_and_grid():
    source = GALLERY.read_text(encoding="utf-8")
    assert "renderFieldGrid: (entry, keys, decorate) => renderGenericTrackedFields(" in source
    assert source.count("withControlsLookup(trackedRenderContext(surface), entries)") == 2
    assert "bodyResult.pinnedFieldsGeneric" in source
    # Collector fields a Control set keep their cell and gain a Control chip (tag display).
    assert 'chip.textContent = "Control";' in source and "controlsAlsoIn" not in source
