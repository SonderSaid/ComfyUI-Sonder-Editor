"""Subgraph names in the pane picker and Manage's Views list (0.8 hardening, Phase 6B).

The canvas export carries ``subgraph_names`` ({host execution path: name}) for
every subgraph instance, muted ones included; the server reads it tolerantly
and puts it in the view; the picker labels a node inside a subgraph
``Outer › Inner › title``, falling back to "Inside a subgraph" without a name.
"""

import json

from server import control_descriptors as cd, control_manifest as cm, session_registry
from test_control_descriptors import CLASSES, _node
from test_view_panes_js import FAKE_DOM, node_run, url

BINDINGS = url("control_bindings.js")
MODEL = url("views_model.js")


# ── Reading ──────────────────────────────────────────────────────────────────

def test_reader_bounds_and_tolerance():
    assert cm.read_subgraph_names(None) == {} and cm.read_subgraph_names(["3"]) == {}
    raw = {"3132": "  Sampling ", "9400:12": "Detail‮\x07", "bad path": "x", "1:constructor": "x", "5": 7, "6": "  ",
           "7": "é" * 130, "8": "﻿Name﻿"}
    assert cm.read_subgraph_names(raw) == {"3132": "Sampling", "9400:12": "Detail", "7": "é" * 120, "8": "Name"}
    many = {str(i): f"S{i}" for i in range(200)}
    read = cm.read_subgraph_names(many)
    assert len(read) == cm.MAX_SUBGRAPH_NAMES and list(read)[:2] == ["0", "1"]
    parsed = cm.parse_manifest(json.dumps({"v": 1, "subgraph_names": {"3": "A"}}))
    assert parsed["subgraph_names"] == {"3": "A"}
    assert cm.parse_manifest(json.dumps({"v": 1, "subgraph_names": "junk"}))["subgraph_names"] == {}


def _prompt(names):
    manifest = {"v": 1, "bindings": {}, "notes": {}, "subgraph_names": names}
    return {"3132:6": _node("KSampler", {"steps": 20, "seed": 5}),
            "9": _node("SonderControlRelay", {"manifest": json.dumps(manifest)})}


def test_the_view_carries_the_names_from_the_relay_or_the_orphan_manifest():
    view = cd.describe_bindings(_prompt({"3132": "Sampling", "x y": "bad"}), class_lookup=CLASSES.get)
    assert view["subgraph_names"] == {"3132": "Sampling"}
    assert "subgraph_names" not in cd.describe_bindings(_prompt({}), class_lookup=CLASSES.get)
    # No Relay: the canvas posts its export as orphan_manifest.
    prompt = {"3132:6": _node("KSampler", {"steps": 20, "seed": 5})}
    orphan = json.dumps({"v": 1, "bindings": {}, "notes": {}, "subgraph_names": {"3132": "Sampling"}})
    view = cd.describe_bindings(prompt, class_lookup=CLASSES.get, orphan_manifest=orphan)
    assert view["subgraph_names"] == {"3132": "Sampling"}


def _publication(names):
    return {"view_seq": 1, "prompt_hash": "h", "view": {"bindings": [], "notes": [], "subgraph_names": names}}


def test_mounted_publication_drops_a_malformed_map_and_never_refuses():
    assert session_registry._prepare_control_publication(_publication({"3": "A", "bad": 4}))["view"]["subgraph_names"] == {"3": "A"}
    for junk in ("text", ["3"], {"bad key": "x"}, 7):
        assert "subgraph_names" not in session_registry._prepare_control_publication(_publication(junk))["view"]


# ── Export (control_bindings.js) ─────────────────────────────────────────────

GRAPH = r"""
import assert from 'node:assert/strict';
const B = await import(BINDINGS_URL);
const graph = nodes => ({ _nodes: nodes });
const host = (id, sub, extra = {}) => ({ id, type: 'def', subgraph: sub, widgets: [], inputs: [], mode: 0, properties: {}, ...extra });
const out = {};
"""


def run_js(body):
    return node_run(GRAPH.replace("BINDINGS_URL", BINDINGS) + body + "\nconsole.log('RESULT=' + JSON.stringify(out));")


def test_export_walks_every_instance_at_every_depth():
    out = run_js(r"""
const deep = graph([{ id: 1, type: 'KSampler', widgets: [], inputs: [] }]);
deep.name = 'Deep';
const middle = graph([host(12, deep), { id: 3, type: 'KSampler', widgets: [], inputs: [] }]);
middle.name = 'Detail definition';
const root = graph([host(9400, middle, { title: '  Detail  ', mode: 2 }), host(3132, deep, { title: '' }),
    host(77, graph([]), { title: '‮\u0007' })]);
out.names = B.exportSubgraphNames(root);
// The export sets them, and never passes a stale saved map through.
const saved = JSON.stringify({ v: 1, bindings: {}, notes: {}, subgraph_names: { '1': 'Stale' } });
out.exported = JSON.parse(B.exportManifest(saved, root)).subgraph_names;
out.none = 'subgraph_names' in JSON.parse(B.exportManifest(saved, graph([])));
out.edited = 'subgraph_names' in JSON.parse(B.editManifest(saved, () => {}));
// At most 128, and cleaned like the server's reader.
out.capped = Object.keys(B.exportSubgraphNames(graph(Array.from({ length: 140 }, (_, i) => host(i + 1, graph([]), { title: `S${i}` }))))).length;
out.clean = ['  A ', '﻿B﻿', 'x'.repeat(130), 7, '\u0007'].map(B.subgraphName);
""")
    # The muted host 9400 is listed; an empty title falls back to the definition's name.
    assert out["names"] == {"9400": "Detail", "9400:12": "Deep", "3132": "Deep"}
    assert out["exported"] == out["names"]
    assert out["none"] is False and out["edited"] is False
    assert out["capped"] == cm.MAX_SUBGRAPH_NAMES
    assert out["clean"] == [cm.clean_subgraph_name(v) for v in ["  A ", "﻿B﻿", "x" * 130, 7, "\x07"]]


def test_the_names_give_way_first_past_the_manifest_cap():
    out = run_js(r"""
// A manifest just under the cap: the names would push it over, so they go
// and the routing capsule stays.
const notes = {}; for (let i = 0; i < 31; i++) notes[`n${i}`] = { text: 'x'.repeat(4000) };
const hosts = Array.from({ length: 128 }, (_, i) => host(i + 1, graph([]), { title: 'N'.repeat(120) }));
const saved = JSON.stringify({ v: 1, bindings: {}, notes, execution: { v: 1, controls: {} } });
const exported = B.exportManifest(saved, graph(hosts));
out.bytes = new TextEncoder().encode(exported).length;
out.names = 'subgraph_names' in JSON.parse(exported);
out.routing = JSON.parse(exported).execution.routing;
""")
    assert out["bytes"] <= cm.MAX_MANIFEST_BYTES
    assert out["names"] is False
    assert json.loads(out["routing"]) == {"v": 1, "nodes": {}, "edges": []}


# ── Display (views_model.js, controls_views_manage.js) ───────────────────────

def test_picker_labels_name_the_subgraph_chain_and_fall_back():
    out = node_run(f"""
const M = await import({MODEL});
const out = {{}};
const names = {{ '3132': 'Sampling', '9400': 'Upscale', '9400:12': 'Detail', '50': 'Only outer' }};
const s = M.pickerSections({{ subgraphNames: names,
    views: [{{ view_id: 'v', path: '9400:12:7', status: 'ok', label: 'Mine' }}, {{ view_id: 'off', path: '3132:9', status: 'ok', label: 'Off', participation: 'inactive' }}],
    candidates: [{{ path: '3132:8', class_type: 'PreviewImage' }}, {{ path: '50:2:3', class_type: 'SaveVideo' }}, {{ path: '4', class_type: 'SaveImage' }}],
    samplingCandidates: [{{ path: '9400:12:5', class_type: 'KSampler', title: 'Pass' }},
        {{ path: '3132:6', class_type: 'KSampler', inactive: {{ mode: 'muted', by: 'parent', label: 'Sampling' }} }}],
    seen: [{{ source: '3132:44', class_type: 'MySampler', title: 'Custom' }}] }});
out.views = s.views.map(i => [i.label, i.detail]);
out.add = s.add.map(i => [i.label, i.detail]);
out.sampling = s.sampling.map(i => [i.label, i.detail]);
out.none = M.pickerSections({{ candidates: [{{ path: '3132:8', class_type: 'PreviewImage' }}] }}).add.map(i => [i.label, i.detail]);
out.chain = [M.subgraphChain('9400:12:5', names), M.subgraphChain('4', names), M.subgraphChain('constructor:5', {{}}),
    M.subgraphChain('toString:1', {{}}), M.subgraphChain('9400:13:1', names), M.subgraphChain('3132:1', null)];
console.log('RESULT=' + JSON.stringify(out));
""")
    assert out["views"] == [["Mine", "In Upscale › Detail"], ["Off", "Switched off"]]
    # A partial chain (50:2 has no name) falls back to the title and "Inside a subgraph".
    assert out["add"] == [["Sampling › PreviewImage", ""], ["SaveVideo", "Inside a subgraph"], ["SaveImage", ""]]
    assert out["sampling"] == [["Upscale › Detail › Pass", ""], ["Sampling › KSampler", "Inside Sampling, which is muted"],
                               ["Sampling › Custom", "Sent previews in a recent run"]]
    assert out["none"] == [["PreviewImage", "Inside a subgraph"]]
    assert out["chain"] == ["Upscale › Detail", None, None, None, None, None]


def test_manage_views_detail_names_the_subgraph():
    out = node_run(FAKE_DOM + f"""
const {{ mountViewsManage }} = await import({url("controls_views_manage.js")});
const out = {{}};
const row = (id, path, title) => ({{ view_id: id, label: id, default_label: 'Preview', order: 0, origin: 'authored', status: 'ok',
    participation: 'active', path, title, class_type: 'PreviewImage' }});
let view = {{ views: [row('a', '3132:8', 'Preview'), row('b', '4', 'Root preview')], subgraph_names: {{ '3132': 'Sampling' }} }};
const manage = mountViewsManage({{ getView: () => view, capabilities: {{}}, applyChanges: async () => ({{ ok: true }}) }});
const detail = id => manage.el.find(el => el.dataset?.viewId === id).children[1].textContent;
out.first = [detail('a'), detail('b')];
// A rename re-renders the list (its key holds the chain).
view = {{ ...view, subgraph_names: {{ '3132': 'Renamed' }} }};
manage.render(view);
out.renamed = detail('a');
view = {{ views: view.views }};
manage.render(view);
out.none = detail('a');
manage.destroy();
console.log('RESULT=' + JSON.stringify(out));
""")
    assert out["first"] == ["Sampling › Preview", "Root preview"]
    assert out["renamed"] == "Renamed › Preview"
    assert out["none"] == "Preview"


def test_fit_publication_drops_the_names_before_anything_else():
    out = node_run(f"""
const {{ fitPublicationView }} = await import({url("controls_canvas_relay.js")});
const out = {{}};
const envelope = {{ host_id: 'h', session_id: 's', view_seq: 1, prompt_hash: 'x', applied: [], rejected: [] }};
const names = Object.fromEntries(Array.from({{ length: 128 }}, (_, i) => [String(i), 'N'.repeat(120)]));
const small = {{ bindings: [], notes: [], subgraph_names: {{ '1': 'A' }} }};
out.small = fitPublicationView(small, envelope) === small;
const big = {{ bindings: [], notes: [], subgraph_names: names,
    view_candidates: Array.from({{ length: 420 }}, (_, i) => ({{ path: String(i), class_type: 'C'.repeat(1200) }})) }};
const fitted = fitPublicationView(big, envelope);
out.fitted = ['subgraph_names' in fitted, fitted.view_candidates.length];
console.log('RESULT=' + JSON.stringify(out));
""")
    assert out["small"] is True
    # The names went; the candidates the picker needs were kept whole.
    assert out["fitted"] == [False, 420]


def test_fit_publication_blanks_included_names_and_readers_fall_back():
    out = node_run(f"""
const {{ fitPublicationView }} = await import({url("controls_canvas_relay.js")});
const {{ deriveModes, includesText }} = await import({url("controls_modes_model.js")});
const out = {{}};
const envelope = {{ host_id: 'h', session_id: 's', view_seq: 1, prompt_hash: 'x', applied: [], rejected: [] }};
const row = (id, name, extra = {{}}) => ({{ id, name, description: '', order: 0, state: 'differs', origin: 'workflow', members: [{{ binding_id: 'b', preview: 'p'.repeat(200), status: 'ok' }}], execution: [], ...extra }});
const presets = [row('up', 'Upscale', {{ includes: [{{ id: 'off', name: 'Mask Off', status: 'ok' }}] }}), row('off', 'Mask Off')];
const big = {{ bindings: [], notes: [], presets, preset_categories: [{{ id: 'p', name: 'Passes', choices: ['up'], warnings: [] }}, {{ id: 'm', name: 'Mask', choices: ['off'], warnings: [] }}],
    candidates: Array.from({{ length: 430 }}, (_, i) => ({{ path: String(i), input: 'C'.repeat(1200), targets: [] }})) }};
const fitted = fitPublicationView(big, envelope);
out.name = fitted.presets[0].includes[0].name;
out.text = includesText(deriveModes(fitted), fitted.presets[0]);
console.log('RESULT=' + JSON.stringify(out));
""")
    assert out == {"name": "", "text": "Also applies Mask → Mask Off."}
