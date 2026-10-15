"""Row-group Controls on a fake canvas: resolve, list, export, write, Relay text.

The fake LoRA stack mirrors frontend 1.58's dynamicGroupWidget.ts: a hidden,
non-serialized count widget named after the group whose value setter adds or
removes rows at the end, a `<group>.$add` button and `<group>.<i>.<field>`
widgets. Like the real setter it clamps only to the minimum, and a row
removal can fail (``node.lockRemove``). The 1.55 shape has no widgets at all. Real gestures, Undo and both
renderers are checked live on the dummy-LoRA server; these fakes prove the
module logic only. Filenames are synthetic.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from server import control_rows

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
import assert from 'node:assert/strict';
const R = await import(ROWS_URL);
const B = await import(BINDINGS_URL);
const V = await import(VIEW_URL);
const {createCanvasControlsHost} = await import(HOST_URL);
const {planRows, summaryText} = await import(RELAY_ROWS_URL);
const clone = x => JSON.parse(JSON.stringify(x));
const LORAS = ['sonder_test_a.safetensors', 'sonder_test_b.safetensors', 'sonder_test_c.safetensors'];
const FIELDS = ['lora_name', 'strength', 'enabled'];
const DEFAULTS = {lora_name: LORAS[0], strength: 1, enabled: true};
class Graph {
    constructor(){this._nodes=[];this.links=new Map();this.subgraphs=new Map();this.inputs=[];}
    setDirtyCanvas(){}
    getNodeById(id){return this._nodes.find(n=>String(n.id)===String(id))||null;}
    getLink(id){return this.links.get(id)??null;}
}
const widget = (name, value, extra = {}) => ({name, value, options: {}, callbacks: 0, callback(v){this.callbacks++;}, ...extra});
class LoadLoraModel {}
LoadLoraModel.nodeData = {name: 'LoadLoraModel', input: {required: {
    model: ['MODEL'],
    loras: ['COMFY_DYNAMICGROUP_V3', {template: {required: {lora_name: [LORAS], strength: ['FLOAT', {default: 1, min: -100, max: 100, step: 0.01}],
        enabled: ['BOOLEAN', {default: true}]}}, min: 1, max: 20, group_name: 'LoRA'}],
}}};
function loraNode(graph, id, {ready = true, rows = [], fieldExtra = {}} = {}) {
    const node = Object.assign(new LoadLoraModel(), {id, type: 'LoadLoraModel', comfyClass: 'LoadLoraModel', title: 'Load LoRA (Model)',
        widgets: [], inputs: [{name: 'model', link: 5}], properties: {}, mode: 0, graph, size: [300, 0], lockRemove: false,
        setSize(size) { this.size = size; }, computeSize() { return [300, 40 + 60 * count]; }});
    graph._nodes.push(node);
    if (!ready) { node.inputs.push({name: 'loras', link: null}); return node; }
    let count = 0;
    const controller = {name: 'loras', type: 'number', options: {serialize: false}, serialize: false};
    Object.defineProperty(controller, 'value', {get: () => count, set(v) {
        const n = Math.max(1, Math.trunc(v));
        while (count > n) {
            if (node.lockRemove) break; // dynamicGroupWidget removeRow returned false
            count--;
            node.widgets = node.widgets.filter(w => !w.name.startsWith(`loras.${count}.`) && w.name !== `loras.${count}`);
            node.inputs = node.inputs.filter(i => !i.name.startsWith(`loras.${count}.`));
        }
        while (count < n) {
            node.widgets.push(widget(`loras.${count}`, undefined, {type: 'dynamic_group_row', options: {serialize: false}}));
            for (const f of FIELDS) {
                node.widgets.push(widget(`loras.${count}.${f}`, DEFAULTS[f], fieldExtra[f] || {}));
                node.inputs.push({name: `loras.${count}.${f}`, link: null, widget: {name: `loras.${count}.${f}`}});
            }
            count++;
        }
    }});
    node.widgets.push(controller, {name: 'loras.$add', type: 'button', options: {serialize: false}});
    R.writeRows(node, 'loras', FIELDS, rows.length ? rows : [DEFAULTS]);
    return node;
}
const row = (name = LORAS[0], strength = 1, enabled = true) => ({lora_name: name, strength, enabled});

if (CASE === 'parity') {
    const fixtures = PARITY;
    console.log(JSON.stringify(fixtures.map(([rows, spec]) => [R.canonical(rows), R.summary(rows, spec)])));
}

if (CASE === 'resolve') {
    const root = new Graph();
    const ready = loraNode(root, 3, {rows: [row(), row(LORAS[1], 0.5, false)]});
    const old = loraNode(root, 4, {ready: false});
    // 1.58: the group resolves to its rows, never the hidden count widget.
    const live = B.resolveAnchor(ready, 'loras');
    assert.equal(live.status, 'ok');
    assert.equal(live.widget, null);
    assert.deepEqual([live.rows.group, live.rows.ready, live.rows.fieldIds], ['loras', true, FIELDS]);
    assert.deepEqual(R.readRows(ready, 'loras', FIELDS), [row(), row(LORAS[1], 0.5, false)]);
    // 1.55: still the group (from the definition), but no rows to edit.
    const bare = B.resolveAnchor(old, 'loras');
    assert.equal(bare.status, 'ok');
    assert.equal(bare.rows.ready, false);
    // A single row field has no stable identity.
    assert.equal(B.resolveAnchor(ready, 'loras.0.strength').status, 'unsupported');
    // A linked row field makes the group linked.
    ready.inputs.find(i => i.name === 'loras.1.strength').link = 9;
    assert.equal(B.resolveAnchor(ready, 'loras').status, 'linked');
    ready.inputs.find(i => i.name === 'loras.1.strength').link = null;
    // Manage lists each group once and none of its row widgets.
    const {candidates} = B.listCandidates(root);
    assert.deepEqual(candidates.map(c => [c.path, c.input]), [['3', 'loras'], ['4', 'loras']]);
    // The label names the rows; the export flags a canvas without row widgets.
    assert.equal(B.defaultBindingLabel({...live, anchor: {node: ready, name: 'loras'}}), 'Load LoRA (Model) · LoRAs');
    ready.properties.sonderBindings = {loras: 'stack'};
    old.properties.sonderBindings = {loras: 'bare'};
    const manifest = JSON.stringify({v: 1, bindings: {stack: {label: ''}, bare: {label: ''}}, notes: {}});
    const exported = JSON.parse(B.exportManifest(manifest, root));
    assert.equal(exported.bindings.stack.rows_unavailable, undefined);
    assert.equal(exported.bindings.bare.rows_unavailable, true);
    assert.deepEqual(exported.bindings.stack.targets, [{path: '3', input: 'loras'}]);
    // An edit never saves the export-only flag.
    const saved = JSON.parse(B.editManifest(JSON.stringify(exported), () => {}));
    assert.equal(saved.bindings.bare.rows_unavailable, undefined);
    // Restore hints for a group are restorable (its own widget is the count).
    ready.properties = {}; old.properties = {};
    const plan = B.planRestore(root, {bindings: {stack: {restore_targets: [{path: '3', input: 'loras'}]}}});
    assert.deepEqual(plan.restorable, [{binding_id: 'stack', path: '3', input: 'loras'}]);
    // Presets keep a row list member; nothing deeper.
    const presets = B.readPresets({p: {name: 'P', members: {stack: {value: [row()]}, bad: {value: [{a: [1]}]}}}});
    assert.deepEqual(Object.keys(presets.p.members), ['stack']);
}

if (CASE === 'write-rows') {
    const root = new Graph();
    const node = loraNode(root, 3, {rows: [row(), row(LORAS[1]), row(LORAS[2])]});
    const keep = node.widgets.find(w => w.name === 'loras.0.lora_name');
    R.writeRows(node, 'loras', FIELDS, [row(LORAS[2], 0.25, false)]);
    assert.deepEqual(R.readRows(node, 'loras', FIELDS), [row(LORAS[2], 0.25, false)]);
    // Rows are removed at the end: row 0's widget object stays.
    assert.ok(node.widgets.includes(keep));
    R.writeRows(node, 'loras', FIELDS, [row(), row(LORAS[1], 2, true)]);
    assert.deepEqual(R.readRows(node, 'loras', FIELDS), [row(), row(LORAS[1], 2, true)]);
    // The node refits its height, as the frontend's own Add and Remove do.
    assert.deepEqual(node.size, [300, 160]);
    // A removal the frontend refuses leaves more rows than asked: refused.
    node.lockRemove = true;
    assert.throws(() => R.writeRows(node, 'loras', FIELDS, [row()]), /refused this number of rows/);
    node.lockRemove = false;
    const old = loraNode(root, 4, {ready: false});
    assert.throws(() => R.writeRows(old, 'loras', FIELDS, [row()]), /frontend to 1.57/);
}

if (CASE === 'promoted') {
    // A row field promoted to a subgraph input has no stable identity either.
    const inner = new Graph();
    const node = loraNode(inner, 7, {rows: [row()]});
    node.inputs.find(i => i.name === 'loras.0.strength').link = 21;
    inner.links.set(21, {id: 21, target_id: 7, target_slot: node.inputs.findIndex(i => i.name === 'loras.0.strength')});
    inner.inputs = [{name: 'strength', linkIds: [21]}];
    const root = new Graph();
    const host = {id: 9, type: 'sub', comfyClass: 'sub', subgraph: inner, mode: 0, graph: root, properties: {},
        widgets: [widget('strength', 1)], inputs: [{name: 'strength', link: null, widget: {name: 'strength'}}]};
    root._nodes.push(host);
    assert.equal(B.resolveAnchor(host, 'strength').status, 'unsupported');
    assert.deepEqual(B.listCandidates(root).candidates.map(c => c.input), []);
}

if (CASE === 'relay-text' ) {
    const root = new Graph();
    const node = loraNode(root, 3, {rows: [row(), row(LORAS[1], 0.5, false)]});
    const old = loraNode(root, 4, {ready: false});
    node.properties.sonderBindings = {loras: 'stack'};
    old.properties.sonderBindings = {loras: 'bare'};
    const relay = {id: 2, type: 'SonderControlRelay', comfyClass: 'SonderControlRelay', mode: 0, graph: root, properties: {},
        widgets: [{name: 'manifest', value: JSON.stringify({v: 1, bindings: {stack: {label: 'Stack', order: 0}, bare: {label: 'Bare', order: 1}}})}]};
    root._nodes.push(relay);
    const plan = planRows(relay, root);
    assert.deepEqual(plan.rows.map(r => [r.label, r.role, r.type]), [['Stack', 'summary', 'text'], ['Bare', 'summary', 'text']]);
    assert.equal(summaryText(root, plan.rows[0]), '2 LoRAs · 1 on — edit in Controls');
    assert.equal(summaryText(root, plan.rows[1]), 'Needs ComfyUI frontend 1.57 or newer');
    // Search finds a row's file and a field's choice.
    const spec = {kind: 'ROWS', label: 'Stack', value: [row()], fields: [{id: 'lora_name', kind: 'COMBO', options: LORAS}]};
    assert.ok(V.matchEntry(spec, 'test_a'));
    assert.ok(V.matchEntry(spec, 'test_c'));
    assert.ok(!V.matchEntry(spec, 'object'));
}

if (CASE.startsWith('host')) {
    const root = new Graph();
    const editor = {id: 1, type: 'SonderEditor', comfyClass: 'SonderEditor', widgets: [], inputs: [], properties: {}, mode: 0, graph: root};
    root._nodes.push(editor);
    const relay = {id: 2, type: 'SonderControlRelay', comfyClass: 'SonderControlRelay', mode: 0, graph: root, properties: {}, inputs: [],
        widgets: [widget('manifest', JSON.stringify({v: 1, bindings: {stack: {label: 'Stack'}}, notes: {}}))]};
    root._nodes.push(relay);
    const rounding = CASE === 'host-refused' ? {strength: {callback(v) { this.value = Math.round(v); }}} : {};
    const node = loraNode(root, 3, {rows: [row(), row(LORAS[1])], fieldExtra: rounding, ready: CASE !== 'host-no-rows'});
    node.properties.sonderBindings = {loras: 'stack'};
    let captures = 0, describes = 0;
    const answer = {relay: {status: 'ok', key: '2', error: null}, notes: [], editors: ['1'],
        bindings: [{binding_id: 'stack', kind: 'ROWS', status: 'ok', value: [], targets: [{path: '3', input: 'loras'}]}]};
    const api = {addEventListener() {}, removeEventListener() {},
        async fetchApi(url, init) {
            const body = JSON.parse(init.body);
            if (!body.changes) describes++;
            const out = clone(answer);
            // The server checks the base against this fresh export, then
            // returns the rows as its cleaned value (control_rows.coerce_change).
            if (body.changes) {
                const change = body.changes['binding:stack:value'];
                const fresh = R.readRows(node, 'loras', FIELDS);
                if (R.canonical(change.base) !== R.canonical(fresh)) {
                    out.ok = false; out.changes = {};
                    out.rejected = [{target: 'binding:stack:value', reason: 'The LoRAs changed. Your edit was not applied; try again.'}];
                } else { out.ok = true; out.rejected = []; out.changes = {'binding:stack:value': change.rows}; }
            }
            return {ok: true, json: async () => out};
        }};
    const app = {rootGraph: root, graph: root, canvas: {},
        async graphToPrompt() { return {output: {'1': {class_type: 'SonderEditor'}, rows: R.readRows(node, 'loras', FIELDS)}}; }};
    const host = createCanvasControlsHost({app, api, getEditorNode: () => editor, debounceMs: 1,
        getChangeTracker: () => ({captureCanvasState() { captures++; }})});
    const before = R.readRows(node, 'loras', FIELDS);
    const next = [row(LORAS[2], 0.5, false), row(), row(LORAS[1], 2.4, true)];
    // host-stale: a native edit lands after the panel's edit began.
    const base = clone(before);
    if (CASE === 'host-stale') {
        // An open panel (a hidden host never re-reads) whose view predates a native edit.
        host.setVisible(true);
        await new Promise(r => setTimeout(r, 30));
        R.writeRows(node, 'loras', FIELDS, [row(LORAS[1], 0.7, true)]);
    }
    const current = R.readRows(node, 'loras', FIELDS);
    const result = await host.applyChanges({'binding:stack:value': {rows: next, base}});
    if (CASE === 'host') {
        assert.equal(result.ok, true, JSON.stringify(result));
        assert.deepEqual(R.readRows(node, 'loras', FIELDS), next);
        // One history step around the row count and every field write.
        assert.equal(captures, 2);
        assert.ok(node.widgets.find(w => w.name === 'loras.2.strength').callbacks >= 1);
    } else if (CASE === 'host-stale') {
        // The edit is refused and the native edit stands.
        assert.equal(result.ok, false);
        assert.match(result.rejected[0].reason, /changed/);
        assert.deepEqual(R.readRows(node, 'loras', FIELDS), current);
        assert.equal(captures, 0);
        // The refusal re-reads the graph, so the panel's retry starts from it.
        const seen = describes;
        await new Promise(r => setTimeout(r, 30));
        assert.ok(describes > seen, 'a refusal schedules a fresh describe');
    } else if (CASE === 'host-refused') {
        // A callback that changes a value (step rounding) refuses and restores.
        assert.equal(result.ok, false);
        assert.match(result.rejected[0].reason, /changed a row value/);
        assert.deepEqual(R.readRows(node, 'loras', FIELDS), before);
    } else {
        assert.equal(result.ok, false);
        assert.match(result.rejected[0].reason, /frontend to 1.57/);
    }
    host.destroy();
}
console.log('OK');
'''

CASES = ["resolve", "write-rows", "promoted", "relay-text", "host", "host-stale", "host-refused", "host-no-rows"]

PARITY = [
    ([{"lora_name": "sonder_test_a.safetensors", "strength": 1.0, "enabled": True},
      {"enabled": False, "strength": 0.25, "lora_name": "sonder_test_b.safetensors"}],
     {"group_name": "LoRA", "fields": [{"id": "lora_name", "kind": "COMBO"}, {"id": "strength", "kind": "FLOAT"},
                                       {"id": "enabled", "kind": "BOOLEAN"}]}),
    ([], {"group_name": "LoRA", "fields": [{"id": "enabled", "kind": "BOOLEAN"}]}),
    ([{"ü": "é", "a": -3.5, "b": None}], {"group_name": "row", "fields": [{"id": "a", "kind": "FLOAT"}]}),
]


def _run(case, extra=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    urls = {name: (ROOT / "web" / "js" / file).as_uri() for name, file in [
        ("ROWS_URL", "control_rows.js"), ("BINDINGS_URL", "control_bindings.js"), ("VIEW_URL", "controls_panel_view.js"),
        ("HOST_URL", "controls_host_canvas.js"), ("RELAY_ROWS_URL", "control_relay_rows.js")]}
    script = HARNESS.replace("CASE", json.dumps(case)).replace("PARITY", json.dumps(PARITY))
    for name, url in sorted(urls.items(), key=lambda item: -len(item[0])):  # RELAY_ROWS_URL before ROWS_URL
        script = script.replace(name, json.dumps(url))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "harness.mjs"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run([node, str(path)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout


@pytest.mark.parametrize("case", CASES)
def test_canvas_rows(case):
    assert _run(case).strip().endswith("OK")


def test_canonical_and_summary_match_the_server():
    out = json.loads(_run("parity").strip().splitlines()[0])
    expected = [[control_rows.canonical(rows), control_rows.summary(rows, spec)] for rows, spec in PARITY]
    assert out == expected
