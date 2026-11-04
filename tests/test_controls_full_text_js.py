"""Hardening Phase 10 in the browser: complete-value Copy for Controls at run, its
cell hint, the Modes at run renderer, older packs reading the new data, and the
seed Control's Cards layout."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from server import controls_snapshot as cs

ROOT = Path(__file__).resolve().parents[1]
COPY_MODULE = ROOT / "web" / "js" / "gallery_copy_values.js"
GALLERY = ROOT / "web" / "js" / "shared_asset_gallery.js"
RENDERERS = ROOT / "web" / "js" / "tracked_metadata_renderers.js"
STYLE = ROOT / "web" / "js" / "controls_panel_style.js"
MARKER = cs.TRUNCATED_MARKER


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required")
    return node


def _run(script):
    result = subprocess.run([_node(), "--input-type=module"], input=script, capture_output=True,
                            text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


def _uri(path):
    return json.dumps(Path(path).as_uri())


FULL = "\U0001F319" + "lyric line\r\n" * 100  # astral first, CRLF throughout
PREVIEW = FULL[:cs.TEXT_VALUE_LIMIT] + MARKER


def _entry(**control):
    return {"label": "Controls at run", "display_type": "controls",
            "fields": {"Lyrics": PREVIEW, "Steps": "12", "Rows": "3 rows: a; b…"},
            "controls": [{"binding_id": "lyricsB", "field": "Lyrics", "kind": "STRING", **control},
                         {"binding_id": "stepsB", "field": "Steps", "kind": "INT"},
                         {"binding_id": "rowsB", "field": "Rows", "kind": "ROWS", "capped": True}]}


def test_full_text_key_and_marker_match_the_server():
    result = _run(f"import {{ CONTROLS_FULL_TEXT_KEY, CONTROLS_TRUNCATED_MARKER }} from {_uri(COPY_MODULE)};"
                  "console.log(JSON.stringify([CONTROLS_FULL_TEXT_KEY, CONTROLS_TRUNCATED_MARKER]));")
    assert result == [cs.FULL_TEXT_KEY, cs.TRUNCATED_MARKER]


def test_copy_resolves_the_kept_text_only_when_it_extends_the_preview():
    script = f"""
import {{ rememberControlsFullText, resolveTrackedFieldCopy, trackedFieldDisplayText, controlsFullText }} from {_uri(COPY_MODULE)};
const entry = {json.dumps(_entry())};
const full = {json.dumps(FULL)};
const out = {{}};
out.before = resolveTrackedFieldCopy(entry, 'Lyrics');
rememberControlsFullText([entry], {{ controls_text: {{ v: 1, values: {{ lyricsB: full }} }} }});
out.kept = resolveTrackedFieldCopy(entry, 'Lyrics');
out.steps = resolveTrackedFieldCopy(entry, 'Steps');
out.rows = resolveTrackedFieldCopy(entry, 'Rows');
out.hint = trackedFieldDisplayText(entry, 'Lyrics', entry.fields.Lyrics);
out.plain = trackedFieldDisplayText(entry, 'Steps', '12');
const other = {json.dumps(_entry())};
rememberControlsFullText([other], {{ controls_text: {{ v: 1, values: {{ lyricsB: 'something else entirely' + 'x'.repeat(900) }} }} }});
out.mismatch = resolveTrackedFieldCopy(other, 'Lyrics');
const short = {json.dumps(_entry())};
rememberControlsFullText([short], {{ controls_text: {{ v: 1, values: {{ lyricsB: full.slice(0, 512) }} }} }});
out.notLonger = resolveTrackedFieldCopy(short, 'Lyrics');
const cleared = {json.dumps(_entry())};
rememberControlsFullText([cleared], {{ controls_text: {{ v: 1, values: {{ lyricsB: full }} }} }});
rememberControlsFullText([cleared], {{}});
out.cleared = resolveTrackedFieldCopy(cleared, 'Lyrics');
const omitted = {json.dumps(_entry(rows_omitted=True))};
out.omitted = resolveTrackedFieldCopy(omitted, 'Lyrics');
out.reader = [controlsFullText({{ values: {{ a: 'x' }} }}, 'a'), controlsFullText({{ values: {{ a: 3 }} }}, 'a'),
  controlsFullText(null, 'a'), controlsFullText({{ values: {{}} }}, 'toString')];
console.log(JSON.stringify(out));
"""
    out = _run(script)
    unavailable = {"kind": "unavailable", "label": "Full value is only in the run's source prompt", "text": ""}
    assert out["before"] == unavailable
    assert out["kept"] == {"kind": "value", "label": "Copy value", "text": FULL}
    assert out["steps"]["text"] == "12"
    assert out["rows"] == unavailable  # a cut row list is never offered as complete
    count = len(FULL)  # code points: the astral moon counts once
    assert out["hint"] == FULL[:cs.TEXT_VALUE_LIMIT] + f"… (Copy value copies all {count:,} characters)"
    assert out["plain"] == "12"
    assert out["mismatch"] == unavailable
    assert out["notLonger"] == unavailable
    assert out["cleared"] == unavailable
    assert out["omitted"]["label"] == "Copy summary"
    assert out["reader"] == ["x", None, None, None]


def _gallery_function(name):
    source = GALLERY.read_text(encoding="utf-8")
    start = source.index(f"function {name}(")
    depth, index = 0, source.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(source[index], 0)
        index += 1
        if depth == 0:
            return source[start:index]


def test_the_gallery_registers_full_text_where_entries_come_from_params():
    """The real wiring, extracted from the gallery: entries made from an asset's
    detail params resolve Copy through that asset's record."""
    script = f"""
import {{ rememberControlsFullText, resolveTrackedFieldCopy }} from {_uri(COPY_MODULE)};
const trackedEntriesMemo = new WeakMap();
{_gallery_function("trackedEntriesFromValue")}
{_gallery_function("trackedEntriesFromParams")}
const params = {{ editor_export: {{ tracked_metadata: [{json.dumps(_entry())}] }},
  controls_text: {{ v: 1, values: {{ lyricsB: {json.dumps(FULL)} }} }} }};
const entries = trackedEntriesFromParams(params);
console.log(JSON.stringify(resolveTrackedFieldCopy(entries[0], 'Lyrics')));
"""
    assert _run(script)["text"] == FULL


def test_both_cell_builders_show_the_display_text_and_keep_the_stored_value_for_filters():
    source = GALLERY.read_text(encoding="utf-8")
    cells = re.findall(r"const cellInfo = \{ entry, fieldKey: (?:String\(key\)|key), value: rendered[^\n]*\n\s*const cell = makeMetaCell\(key, ([^,]+\([^)]*\)|rendered),", source)
    assert cells == ["trackedFieldDisplayText(entry, key, rendered)"] * 2


def test_older_pack_reading_new_data_never_offers_the_preview_as_complete(tmp_path):
    old = subprocess.run(["git", "-C", str(ROOT), "show", "v0.8.2:web/js/gallery_copy_values.js"],
                         capture_output=True, text=True, encoding="utf-8")
    if old.returncode:
        pytest.skip("tag v0.8.2 not available")
    old_copy = tmp_path / "gallery_copy_values_082.js"
    old_copy.write_text(old.stdout, encoding="utf-8")
    old_renderers = tmp_path / "tracked_metadata_renderers_082.js"
    old_renderers.write_text(subprocess.run(["git", "-C", str(ROOT), "show", "v0.8.2:web/js/tracked_metadata_renderers.js"],
                                            capture_output=True, text=True, encoding="utf-8", check=True).stdout,
                             encoding="utf-8")
    modes = cs.modes_section({}, [{"id": "t", "name": "Task", "status": "selected", "choice": "Edit", "matches": ["Edit"]}])
    script = f"""
import {{ resolveTrackedFieldCopy }} from {_uri(old_copy)};
import {{ renderTrackedSectionBody, resolveDisplayType }} from {_uri(old_renderers)};
const entry = {json.dumps(_entry())};
console.log(JSON.stringify({{
  lyrics: resolveTrackedFieldCopy(entry, 'Lyrics'),
  modesType: resolveDisplayType({json.dumps(modes)}),
  modesBody: renderTrackedSectionBody({json.dumps(modes)}, {{}}),
  modesCopy: resolveTrackedFieldCopy({json.dumps(modes)}, 'Task'),
}}));
"""
    out = _run(script)
    assert out["lyrics"]["kind"] == "unavailable"
    assert out["modesType"] == "modes" and out["modesBody"] is None  # generic field grid
    assert out["modesCopy"]["text"] == "Edit"


def test_modes_renderer_explains_each_state():
    selected = cs.modes_section({}, [{"id": "t", "name": "Task", "status": "selected", "choice": "Edit", "matches": ["Edit"]},
                                     {"id": "m", "name": "Mask", "status": "custom", "choice": "", "matches": []}])
    script = f"""
class El {{ constructor() {{ this.children = []; this.textContent = ''; this.title = ''; }}
  appendChild(n) {{ this.children.push(n); return n; }}
  get text() {{ return [this.textContent, ...this.children.map((c) => c.text || '')].join(' ').trim(); }} }}
globalThis.document = {{ createElement: () => new El() }};
import {{ renderTrackedSectionBody }} from {_uri(RENDERERS)};
const titles = {{}};
const ctx = {{ style: (el) => el, CHROME: {{ textDim: '#888' }},
  renderFieldGrid: (entry, keys, decorate) => {{ const grid = new El();
    for (const key of keys) {{ const cell = new El(); decorate(cell, key); titles[key] = cell.title; grid.appendChild(cell); }}
    return grid; }} }};
const a = renderTrackedSectionBody({json.dumps(selected)}, ctx);
const b = renderTrackedSectionBody({{ label: 'Modes at run', display_type: 'modes', fields: {{}}, modes: [], status: 'not_recorded' }}, ctx);
console.log(JSON.stringify({{ a: a.dom.text, consumed: a.consumedFields, generic: a.pinnedFieldsGeneric, titles, b: b.dom.text }}));
"""
    out = _run(script)
    assert out["a"].startswith("The Mode choices this run was queued with.")
    assert out["consumed"] == ["Task", "Mask"] and out["generic"] is True
    assert out["titles"]["Task"] == "" and "matches no choice" in out["titles"]["Mask"]
    assert out["b"] == "Modes weren't recorded for this run."


def test_seed_card_css_wraps_the_select_under_the_field_and_leaves_rows_alone():
    script = f"""
import * as S from {_uri(STYLE)};
console.log(JSON.stringify({{
  fieldCards: S.seedFieldCss(true), fieldRows: S.seedFieldCss(false),
  controlCards: S.seedControlCss(true), controlRows: S.seedControlCss(false),
  modeCards: S.seedModeSelectCss(true), modeRows: S.seedModeSelectCss(false), legacyMode: S.modeSelectCss,
}}));
"""
    out = _run(script)
    assert out["fieldRows"] == "display:contents;"
    assert out["controlRows"] == ""
    assert out["modeRows"] == out["legacyMode"]  # Rows unchanged since Phase 5
    # Cards: the field group and the select each take a full line of the card.
    assert "flex:1 1 100%" in out["fieldCards"] and "display:flex" in out["fieldCards"]
    assert "flex-wrap:wrap" in out["controlCards"]
    assert "flex:1 1 100%" in out["modeCards"] and "width:96px" not in out["modeCards"]
