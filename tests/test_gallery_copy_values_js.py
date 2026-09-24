"""Clipboard payloads for gallery metadata, including old saved sections."""
import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
COPY_MODULE = ROOT / "web" / "js" / "gallery_copy_values.js"
GALLERY = ROOT / "web" / "js" / "shared_asset_gallery.js"
RENDERERS = ROOT / "web" / "js" / "tracked_metadata_renderers.js"


def _run(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for gallery Copy tests")
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


def test_collector_and_gallery_share_the_truncation_marker():
    collector = ast.parse((ROOT / "nodes" / "metadata_collector.py").read_text(encoding="utf-8"))
    python_marker = next(ast.literal_eval(node.value) for node in collector.body
                         if isinstance(node, ast.Assign)
                         and any(isinstance(target, ast.Name) and target.id == "TRUNCATED_MARKER"
                                 for target in node.targets))
    js_marker = _run(f"import {{ TRUNCATED_MARKER }} from {json.dumps(COPY_MODULE.as_uri())};"
                     "console.log(JSON.stringify(TRUNCATED_MARKER));")
    assert js_marker == python_marker


def test_complete_scalar_and_structured_copy_sources():
    script = f"""
import {{ resolveTrackedFieldCopy, resolvePowerLoraRowCopies, resolvePowerLoraNamesCopy, TRUNCATED_MARKER }} from {json.dumps(COPY_MODULE.as_uri())};
const long = String.fromCodePoint(0x1F319) + 'p'.repeat(2500);
const raw = 'prompt: ' + long + ', seed: 7';
const promptPreview = long.slice(0, 2048) + TRUNCATED_MARKER;
const entry = {{ fields: {{ prompt: promptPreview, seed: 7 }},
  raw_widget_text: raw, raw_field_spans: {{ prompt: [8, 8 + long.length], seed: [16 + long.length, raw.length] }},
}};
const payloadEntry = {{ fields: {{ payload: {{ text: promptPreview }} }},
  full_fields: {{ payload: {{ text: long }} }} }};
const rows = {{ fields: {{ power_loras: [null, {{ name: promptPreview, strength: 0.7 }}] }},
  full_fields: {{ power_loras: [null, {{ name: long, strength: 0.7 }}] }} }};
console.log(JSON.stringify({{
  prompt: resolveTrackedFieldCopy(entry, 'prompt'),
  seed: resolveTrackedFieldCopy(entry, 'seed'),
  payload: resolveTrackedFieldCopy(payloadEntry, 'payload'),
  row: resolvePowerLoraRowCopies(rows, 1),
  names: resolvePowerLoraNamesCopy(rows),
}}));
"""
    result = _run(script)
    long = "🌙" + "p" * 2500
    assert result["prompt"] == {"kind": "value", "label": "Copy value", "text": long}
    assert result["seed"]["text"] == "7"
    assert json.loads(result["payload"]["text"]) == {"text": long}
    assert result["row"]["name"]["text"] == long
    assert json.loads(result["row"]["row"]["text"]) == {"name": long, "strength": 0.7}
    assert result["names"]["text"] == long


def test_legacy_and_corrupt_sources_never_copy_a_truncated_preview():
    script = f"""
import {{ resolveTrackedFieldCopy, resolvePowerLoraRowCopies, TRUNCATED_MARKER }} from {json.dumps(COPY_MODULE.as_uri())};
const preview = 'a'.repeat(2048) + TRUNCATED_MARKER;
const complete = 'a'.repeat(2200);
const single = {{ fields: {{ prompt: preview }}, raw_widget_text: 'prompt: ' + complete }};
const multiRaw = 'prompt: ' + complete + ', seed: 7';
const multi = {{ fields: {{ prompt: preview, seed: 7 }}, raw_widget_text: multiRaw,
  raw_field_spans: {{ prompt: [8, 8 + complete.length], seed: [16 + complete.length, multiRaw.length] }} }};
const legacyMulti = {{ fields: multi.fields, raw_widget_text: multiRaw }};
const malformed = {{ ...multi, raw_field_spans: {{ ...multi.raw_field_spans, prompt: [0, 2200] }} }};
const overlongSpan = {{ ...multi, raw_field_spans: {{ ...multi.raw_field_spans, prompt: [8, multiRaw.length] }} }};
const embeddedRaw = 'prompt: ' + 'a'.repeat(2048) + ', seed: hidden, seed: 7';
const embeddedDelimiter = {{ ...multi, raw_widget_text: embeddedRaw,
  raw_field_spans: {{ prompt: [8, 8 + 2048], seed: [embeddedRaw.lastIndexOf(', seed: ') + 8, embeddedRaw.length] }} }};
const numericKeys = {{ fields: {{ '1': preview, '0': 7 }},
  raw_widget_text: '1: ' + complete + ', 0: 7',
  raw_field_spans: {{ '1': [3, 3 + complete.length], '0': [8 + complete.length, 9 + complete.length] }} }};
const linkedRaw = 'prompt: ' + complete + ", clip: ['10', 0], seed: 7";
const clipStart = linkedRaw.indexOf(', clip: ') + 8;
const seedStart = linkedRaw.lastIndexOf(', seed: ') + 8;
const linkedAfter = {{ fields: {{ prompt: preview, clip: ['10', 0], seed: 7 }},
  raw_widget_text: linkedRaw,
  raw_field_spans: {{ prompt: [8, 8 + complete.length], clip: [clipStart, seedStart - 8], seed: [seedStart, linkedRaw.length] }} }};
const floatRaw = 'prompt: ' + complete + ', cfg: 1e-05';
const floatAfter = {{ fields: {{ prompt: preview, cfg: 0.00001 }}, raw_widget_text: floatRaw,
  raw_field_spans: {{ prompt: [8, 8 + complete.length], cfg: [15 + complete.length, floatRaw.length] }} }};
const short = 'a'.repeat(2047);
const shortRaw = 'prompt: ' + short + ', seed: 7';
const shortCapped = {{ fields: {{ prompt: short + TRUNCATED_MARKER, seed: 7 }}, raw_widget_text: shortRaw,
  raw_field_spans: {{ prompt: [8, 8 + short.length], seed: [16 + short.length, shortRaw.length] }} }};
const lora = {{ display_type: 'power_loras', fields: {{ power_loras: [{{ name: preview }}] }}, raw_widget_text: 'lora_1: ' + complete }};
console.log(JSON.stringify({{
  single: resolveTrackedFieldCopy(single, 'prompt'),
  multi: resolveTrackedFieldCopy(multi, 'prompt'),
  legacyMulti: resolveTrackedFieldCopy(legacyMulti, 'prompt'),
  malformed: resolveTrackedFieldCopy(malformed, 'prompt'),
  overlongSpan: resolveTrackedFieldCopy(overlongSpan, 'prompt'),
  embeddedDelimiter: resolveTrackedFieldCopy(embeddedDelimiter, 'prompt'),
  numericKeys: resolveTrackedFieldCopy(numericKeys, '1'),
  linkedAfter: resolveTrackedFieldCopy(linkedAfter, 'prompt'),
  floatAfter: resolveTrackedFieldCopy(floatAfter, 'prompt'),
  shortCapped: resolveTrackedFieldCopy(shortCapped, 'prompt'),
  empty: resolveTrackedFieldCopy({{ fields: {{ prompt: '' }} }}, 'prompt'),
  missing: resolveTrackedFieldCopy({{ fields: {{ prompt: preview }} }}, 'prompt'),
  lora: resolvePowerLoraRowCopies(lora, 0),
}}));
"""
    result = _run(script)
    assert result["single"]["text"] == "a" * 2200
    assert result["multi"]["text"] == "a" * 2200
    assert result["legacyMulti"]["kind"] == "raw"
    assert result["legacyMulti"]["label"] == "Copy raw widget text (whole section)"
    assert result["malformed"] == result["legacyMulti"]
    assert result["overlongSpan"] == result["legacyMulti"]
    assert result["embeddedDelimiter"]["kind"] == "raw"
    assert result["numericKeys"]["text"] == "a" * 2200
    assert result["linkedAfter"]["text"] == "a" * 2200
    assert result["floatAfter"]["text"] == "a" * 2200
    assert result["shortCapped"]["text"] == "a" * 2047
    assert result["empty"]["text"] == ""
    assert result["missing"]["kind"] == "unavailable"
    assert result["lora"]["name"]["kind"] == "raw"
    assert result["lora"]["row"]["kind"] == "raw"


def test_clipboard_failure_is_reported_only_after_both_paths_fail():
    source = GALLERY.read_text(encoding="utf-8")
    functions = "async function copyToClipboardSafe(text) {" + source.split(
        "async function copyToClipboardSafe(text) {", 1)[1].split(
            "    function copyMenuItem(copy)", 1)[0]
    script = """
const errors = [];
const notifyError = (message) => errors.push(message);
let fallbackResult = false;
let fallbackCalls = 0;
let focusRestores = 0;
const copyButton = { isConnected: true, focus() { focusRestores++; document.activeElement = this; } };
globalThis.document = {
  activeElement: copyButton,
  createElement: () => ({ style: {}, select() { document.activeElement = this; }, remove() {} }),
  body: { appendChild() {} },
  execCommand: () => { fallbackCalls++; return fallbackResult; },
};
globalThis.navigator.clipboard = { writeText: async () => { throw Error('denied'); } };
""" + functions + """
const failed = await copyToClipboardSafe('entire prompt');
fallbackResult = true;
const recovered = await copyToClipboardSafe('entire prompt');
globalThis.navigator.clipboard = { writeText: async () => {} };
const direct = await copyToClipboardSafe('entire prompt');
console.log(JSON.stringify({ failed, recovered, direct, errors, fallbackCalls, focusRestores }));
"""
    result = _run(script)
    assert result == {
        "failed": False, "recovered": True, "direct": True,
        "errors": ["Could not copy to clipboard"], "fallbackCalls": 2, "focusRestores": 2,
    }


def test_copy_buttons_keep_field_events_and_lora_index_intact():
    source = GALLERY.read_text(encoding="utf-8")
    helper = "function attachCopyToElement(host, copy, label, onContextMenu = null) {" + source.split(
        "function attachCopyToElement(host, copy, label, onContextMenu = null) {", 1)[1].split(
            "    function makeMetaCell(label, value, options = {})", 1)[0]
    script = f"""
import {{ renderTrackedSectionBody }} from {json.dumps(RENDERERS.as_uri())};
import {{ resolvePowerLoraRowCopies }} from {json.dumps(COPY_MODULE.as_uri())};
class Element {{
  constructor(tag) {{ this.tag = tag; this.style = {{}}; this.children = []; this.handlers = {{}}; this.parent = null; }}
  append(...nodes) {{ for (const node of nodes) {{ node.parent = this; this.children.push(node); }} }}
  appendChild(node) {{ this.append(node); }}
  addEventListener(type, fn) {{ (this.handlers[type] ||= []).push(fn); }}
  setAttribute(name, value) {{ this[name] = value; }}
  contains(node) {{ for (let cur = node; cur; cur = cur.parent) if (cur === this) return true; return false; }}
  fire(type) {{
    const event = {{ type, stopped: false, preventDefault() {{}}, stopPropagation() {{ this.stopped = true; }} }};
    for (let cur = this; cur && !event.stopped; cur = cur.parent) {{
      for (const fn of cur.handlers[type] || []) fn(event);
    }}
    return event;
  }}
}}
globalThis.document = {{ createElement: (tag) => new Element(tag), activeElement: null }};
const style = (element) => element;
const copied = [], menus = [];
const copyToClipboardSafe = (value) => copied.push(value);
const showCopyMenu = () => menus.push('copy');
const compareModeActive = () => false;
{helper}
const host = new Element('div');
let filters = 0, contextFilters = 0;
host.addEventListener('click', () => filters++);
attachCopyToElement(host, {{ kind: 'value', label: 'Copy value', text: 'full' }}, 'prompt',
  (event) => {{ event.preventDefault(); event.stopPropagation(); contextFilters++; }});
host.children[0].fire('click');
host.children[0].fire('contextmenu');
host.fire('click');
const fullName = 'L'.repeat(3000);
const entry = {{ display_type: 'power_loras', fields: {{ power_loras: [null, {{ name: 'preview' }}] }},
  full_fields: {{ power_loras: [null, {{ name: fullName, strength: 0.7 }}] }} }};
let loraFilter = 0, loraContext = 0;
const result = renderTrackedSectionBody(entry, {{ style, CHROME: {{ borderSoft: '#555' }},
  formatGenerationValue: String, fieldSearchToken: () => 'token',
  onFieldClick: () => loraFilter++, onFieldContextMenu: (event) => {{ event.stopPropagation(); loraContext++; }},
  rowCopy: (section, index) => resolvePowerLoraRowCopies(section, index).row,
  onFieldCopy: (copy) => copied.push(copy.text),
}});
const rowWrap = result.dom.children[1];
const [rowButton, copyButton] = rowWrap.children;
copyButton.fire('click');
copyButton.fire('contextmenu');
rowButton.fire('click');
console.log(JSON.stringify({{ copied, filters, contextFilters, menus, loraFilter, loraContext,
  sibling: rowButton.parent === copyButton.parent, rowIndex: JSON.parse(copied[1]).name === fullName }}));
"""
    result = _run(script)
    assert result == {
        "copied": ["full", json.dumps({"name": "L" * 3000, "strength": 0.7}, separators=(",", ":"))],
        "filters": 1, "contextFilters": 1, "menus": [],
        "loraFilter": 1, "loraContext": 1, "sibling": True, "rowIndex": True,
    }

def test_copy_reveals_on_hover_only_in_compare_and_on_focus_everywhere():
    source = GALLERY.read_text(encoding="utf-8")
    helper = "function attachCopyToElement(host, copy, label, onContextMenu = null) {" + source.split(
        "function attachCopyToElement(host, copy, label, onContextMenu = null) {", 1)[1].split(
            "    function makeMetaCell(label, value, options = {})", 1)[0]
    script = f"""
import {{ renderTrackedSectionBody }} from {json.dumps(RENDERERS.as_uri())};
class Element {{
  constructor(tag) {{ this.tag = tag; this.style = {{}}; this.children = []; this.handlers = {{}}; this.parent = null; }}
  append(...nodes) {{ for (const node of nodes) {{ node.parent = this; this.children.push(node); }} }}
  appendChild(node) {{ this.append(node); }}
  addEventListener(type, fn) {{ (this.handlers[type] ||= []).push(fn); }}
  setAttribute(name, value) {{ this[name] = value; }}
  contains(node) {{ for (let cur = node; cur; cur = cur.parent) if (cur === this) return true; return false; }}
  fire(type) {{ for (const fn of this.handlers[type] || []) fn({{ type }}); }}
}}
globalThis.document = {{ createElement: (tag) => new Element(tag), activeElement: null }};
globalThis.queueMicrotask = (fn) => fn();
const style = (element) => element;
const copyToClipboardSafe = () => {{}};
const showCopyMenu = () => {{}};
let compare = false;
const compareModeActive = () => compare;
{helper}
const state = (host) => {{
  const button = host.children[0];
  return {{ opacity: button.style.opacity, clickable: button.style.pointerEvents }};
}};
const make = () => attachCopyToElement(new Element('div'), {{ kind: 'value', label: 'Copy value', text: 'x' }}, 'seed');
const inspector = make();
inspector.fire('mouseenter');
const inspectorHover = state(inspector);
document.activeElement = inspector.children[0];
inspector.fire('focusin');
const inspectorFocus = state(inspector);
document.activeElement = null;
inspector.fire('focusout');
compare = true;
const compared = make();
compared.fire('mouseenter');
const compareHover = state(compared);
compared.fire('mouseleave');
const compareLeave = state(compared);

const loraRow = (onHover) => {{
  const entry = {{ display_type: 'power_loras', fields: {{ power_loras: [{{ name: 'a' }}] }} }};
  const body = renderTrackedSectionBody(entry, {{ style, CHROME: {{ borderSoft: '#555' }},
    formatGenerationValue: String, fieldSearchToken: () => 'token',
    rowCopy: () => ({{ kind: 'value', label: 'Copy row JSON', text: '{{}}' }}),
    copyOnHover: () => onHover }});
  const rowWrap = body.dom.children[1];
  rowWrap.fire('mouseenter');
  return rowWrap.children[1].style.opacity;
}};
console.log(JSON.stringify({{
  inspectorHover, inspectorFocus, compareHover, compareLeave,
  inspectorPadding: inspector.style.paddingRight ?? null, comparePadding: compared.style.paddingRight,
  loraInspector: loraRow(false), loraCompare: loraRow(true),
}}));
"""
    result = _run(script)
    assert result["inspectorHover"] == {"opacity": "0", "clickable": "none"}
    assert result["inspectorFocus"] == {"opacity": "1", "clickable": "auto"}
    assert result["compareHover"] == {"opacity": "1", "clickable": "auto"}
    assert result["compareLeave"] == {"opacity": "0", "clickable": "none"}
    assert result["inspectorPadding"] is None
    assert result["comparePadding"] == "42px"
    assert (result["loraInspector"], result["loraCompare"]) == ("0", "1")

