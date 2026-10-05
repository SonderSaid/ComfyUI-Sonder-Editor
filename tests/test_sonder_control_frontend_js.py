"""Sonder Control canvas hook: kind, seed takeover, identity, link refusal.

Runs `web/js/sonder_control.js` in a Node vm against a fake LiteGraph graph.
Live renderer behavior (legacy and Nodes 2.0, real paste and undo) is a
manual row.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
const visibility = await import(VISIBILITY_URL);
const resolver = await import(RESOLVER_URL);

const timers = [];
const flush = () => { while (timers.length) timers.shift()(); };
const settle = async () => { for (let i = 0; i < 5; i++) { flush(); await new Promise((r) => setImmediate(r)); } };

class Graph {
    constructor() { this._nodes = []; this.links = new Map(); this.subgraphs = new Map(); this.inputs = []; }
    getNodeById(id) { return this._nodes.find((n) => String(n.id) === String(id)) || null; }
    removeLink(id) {
        const link = this.links.get(id);
        if (!link) return;
        this.links.delete(id);
        const origin = this.getNodeById(link.origin_id);
        const target = this.getNodeById(link.target_id);
        if (origin) origin.outputs[link.origin_slot].links = origin.outputs[link.origin_slot].links.filter((x) => x !== id);
        if (target) target.inputs[link.target_slot].link = null;
        origin?.onConnectionsChange?.(2, link.origin_slot, false, link);
        target?.onConnectionsChange?.(1, link.target_slot, false, link);
    }
}
const root = new Graph();
let toasts = [];
let describeResponse = null;
const app = {
    rootGraph: root, graph: root, configuringGraph: false,
    registerExtension(e) { extension = e; },
    async graphToPrompt() { return { output: { fake: true } }; },
    extensionManager: { toast: { add(t) { toasts.push(t); } } },
};
const api = { async fetchApi(url, init) {
    assert.equal(url, '/sonder-editor/controls/describe');
    assert.deepEqual(JSON.parse(init.body), { prompt: { fake: true } });
    return { ok: true, async json() { return describeResponse; } };
} };
let extension;
const source = fs.readFileSync(SOURCE_PATH, 'utf8').replace(/^import .*;\r?\n/gm, '');
vm.runInNewContext(source, {
    app, api, console, crypto: webcrypto,
    setWidgetHidden: visibility.setWidgetHidden,
    commitWidgetVisibility: visibility.commitWidgetVisibility,
    getGraphLink: resolver.getGraphLink, getGraphNode: resolver.getGraphNode,
    window: { setTimeout(fn) { timers.push(fn); return timers.length; }, clearTimeout() {} },
});

extension.beforeRegisterNodeDef(null, { name: 'KSampler', input: { required: {
    seed: ['INT', { min: 0, max: 4294967295, control_after_generate: true }],
    steps: ['INT', { min: 1, max: 10000 }],
    cfg: ['FLOAT', { min: 0, max: 100 }],
    sampler_name: [['euler', 'dpmpp_2m']],
    model: ['MODEL', {}],
} } });
extension.beforeRegisterNodeDef(null, { name: 'LoadImage', input: { required: { image: [['a.png'], {}] } } });
extension.beforeRegisterNodeDef(null, { name: 'Collector', input: { optional: { value_1: ['*', {}] } } });

let nextId = 1;
let nextLink = 1;
const widget = (name, value, extra = {}) => ({ name, value, options: {}, ...extra });
const add = (graph, node) => { node.graph = graph; graph._nodes.push(node); return node; };
const makeControl = (graph = root) => {
    const control = widget('control_after_generate', 'fixed');
    const seed = widget('seed', 0, { options: { min: 0, max: 18446744073709551615 }, linkedWidgets: [control] });
    return add(graph, {
        id: nextId++, comfyClass: 'SonderControl', inputs: [], outputs: [{ name: 'value', type: '*', links: [] }],
        widgets: [widget('value_text', ''), seed, control, widget('kind', 'STRING'),
                  widget('panel_order', 0), widget('panel_group', ''), widget('control_id', ''),
                  widget('panel_note', ''), widget('seed_mode', '')],
        setDirtyCanvas() {}, computeSize() { return [200, 100]; }, setSize() {},
    });
};
const makeSampler = (graph = root, values = {}) => add(graph, {
    id: nextId++, comfyClass: 'KSampler', outputs: [],
    inputs: ['seed', 'steps', 'cfg', 'sampler_name', 'model'].map((name) => ({
        name, type: { seed: 'INT', steps: 'INT', cfg: 'FLOAT', sampler_name: 'COMBO', model: 'MODEL' }[name],
        link: null, widget: name === 'model' ? undefined : { name } })),
    widgets: [
        widget('seed', values.seed ?? 1234, { options: { min: 0, max: 4294967295 } }),
        widget('steps', values.steps ?? 20, { options: { min: 1, max: 10000 } }),
        widget('cfg', 7.5, { options: { min: 0, max: 100 } }),
        widget('sampler_name', 'euler', { options: { values: ['euler', 'dpmpp_2m'] } }),
    ],
});
const makeNode = (graph, comfyClass, inputs = [], outputs = []) => add(graph, {
    id: nextId++, comfyClass, type: comfyClass,
    inputs: inputs.map(([name, type]) => ({ name, type, link: null, widget: { name } })),
    outputs: outputs.map(([name, type]) => ({ name, type, links: [] })),
    widgets: [],
});
const connect = (graph, from, outSlot, to, inName, notify = true) => {
    const targetSlot = typeof inName === 'number' ? inName : to.inputs.findIndex((s) => s.name === inName);
    const link = { id: nextLink++, origin_id: from.id, origin_slot: outSlot, target_id: to.id, target_slot: targetSlot };
    graph.links.set(link.id, link);
    from.outputs[outSlot].links.push(link.id);
    if (to.inputs?.[targetSlot]) to.inputs[targetSlot].link = link.id;
    if (notify) from.onConnectionsChange?.(2, outSlot, true, link);
    return link;
};
const kind = (node) => node.widgets.find((w) => w.name === 'kind').value;
const value = (node, name) => node.widgets.find((w) => w.name === name).value;
const hidden = (node, name) => visibility.isWidgetEffectivelyHidden(node.widgets.find((w) => w.name === name));
const disconnectAll = (graph, node) => { for (const id of [...node.outputs[0].links]) graph.removeLink(id); flush(); };

// 1. Creation mints an id and shows the value widget.
const ctl = makeControl();
extension.nodeCreated(ctl);
flush();
assert.match(value(ctl, 'control_id'), /^[a-z0-9]{8}$/);
assert.ok(hidden(ctl, 'kind') && hidden(ctl, 'control_id'));
assert.ok(!hidden(ctl, 'value_text') && hidden(ctl, 'seed') && hidden(ctl, 'control_after_generate'));

// 2. INT target: kind follows and the empty value adopts the target's value.
const sampler = makeSampler();
connect(root, ctl, 0, sampler, 'steps');
flush();
assert.equal(kind(ctl), 'INT');
assert.equal(value(ctl, 'value_text'), '20');

// 3. Rewire to FLOAT keeps a value the user already has.
disconnectAll(root, ctl);
connect(root, ctl, 0, sampler, 'cfg');
flush();
assert.equal(kind(ctl), 'FLOAT');
assert.equal(value(ctl, 'value_text'), '20');

// 4. A seed target: SEED, its bounds and value taken over, mode becomes randomize.
disconnectAll(root, ctl);
connect(root, ctl, 0, sampler, 'seed');
flush();
assert.equal(kind(ctl), 'SEED');
const seedWidget = ctl.widgets.find((w) => w.name === 'seed');
assert.equal(seedWidget.options.max, 4294967295);
assert.equal(seedWidget.value, 1234);
assert.equal(value(ctl, 'control_after_generate'), 'randomize');
assert.ok(hidden(ctl, 'value_text') && !hidden(ctl, 'seed') && !hidden(ctl, 'control_after_generate'));

// 5. Leaving SEED fixes the seed again so it cannot churn the cache.
disconnectAll(root, ctl);
connect(root, ctl, 0, sampler, 'sampler_name');
flush();
assert.equal(kind(ctl), 'COMBO');
assert.equal(value(ctl, 'control_after_generate'), 'fixed');
assert.ok(!hidden(ctl, 'value_text'));

// 6. Mixed targets leave the kind for the server to report.
connect(root, ctl, 0, sampler, 'steps');
flush();
assert.equal(kind(ctl), 'COMBO');

// 7. A wildcard target adds no kind of its own.
disconnectAll(root, ctl);
const collector = makeNode(root, 'Collector', [['value_1', '*']]);
connect(root, ctl, 0, sampler, 'steps');
connect(root, ctl, 0, collector, 'value_1');
flush();
assert.equal(kind(ctl), 'INT');

// 8. Through a Reroute node.
disconnectAll(root, ctl);
const reroute = makeNode(root, 'Reroute', [['', '*']], [['', '*']]);
connect(root, reroute, 0, sampler, 'cfg', false);
connect(root, ctl, 0, reroute, 0);
flush();
assert.equal(kind(ctl), 'FLOAT');

// 9. Into a subgraph through its input.
disconnectAll(root, ctl);
const inner = new Graph();
inner.inputs = [{ name: 'steps_in' }];
root.subgraphs.set('def', inner);
const innerSampler = makeSampler(inner);
inner.links.set(900, { id: 900, origin_id: '-10', origin_slot: 0, target_id: innerSampler.id, target_slot: 1 });
const instance = makeNode(root, 'def-uuid', [['steps_in', 'INT']], [['out', '*']]);
instance.subgraph = inner;
// A promoted input keeps its value on the instance's widget.
instance.widgets.push(widget('steps_in', 33));
ctl.widgets.find((w) => w.name === 'value_text').value = '';
connect(root, ctl, 0, instance, 'steps_in');
flush();
assert.equal(kind(ctl), 'INT');
assert.equal(value(ctl, 'value_text'), '33');

// 10. Out of a subgraph through every instance's output.
const innerCtl = makeControl(inner);
extension.nodeCreated(innerCtl);
flush();
connect(root, instance, 0, sampler, 'cfg', false);
const outLink = { id: nextLink++, origin_id: innerCtl.id, origin_slot: 0, target_id: -20, target_slot: 0 };
inner.links.set(outLink.id, outLink);
innerCtl.outputs[0].links.push(outLink.id);
innerCtl.onConnectionsChange(2, 0, true, outLink);
flush();
assert.equal(kind(innerCtl), 'FLOAT');

// 11. Before every Run the kind is settled, even after a silent rewire.
disconnectAll(root, ctl);
connect(root, ctl, 0, sampler, 'sampler_name', false);
assert.equal(kind(ctl), 'INT');
ctl.widgets.find((w) => w.name === 'kind').beforeQueued({ isPartialExecution: false });
assert.equal(kind(ctl), 'COMBO');

// 12. A pasted copy with the same id is re-minted; a load never re-mints.
const pasted = makeControl();
extension.nodeCreated(pasted);
pasted.widgets.find((w) => w.name === 'control_id').value = value(ctl, 'control_id');
flush();
assert.notEqual(value(pasted, 'control_id'), value(ctl, 'control_id'));
assert.match(value(pasted, 'control_id'), /^[a-z0-9]{8}$/);
const loaded = makeControl();
loaded.widgets.find((w) => w.name === 'control_id').value = value(ctl, 'control_id');
app.configuringGraph = true;
extension.nodeCreated(loaded);
extension.loadedGraphNode(loaded);
app.configuringGraph = false;
flush();
assert.equal(value(loaded, 'control_id'), value(ctl, 'control_id'));
const blank = makeControl();
extension.loadedGraphNode(blank);
assert.match(value(blank, 'control_id'), /^[a-z0-9]{8}$/);

// 13. Connections replayed while a graph loads change nothing.
app.configuringGraph = true;
const loading = makeControl();
extension.loadedGraphNode(loading);
connect(root, loading, 0, sampler, 'steps');
app.configuringGraph = false;
flush();
assert.equal(kind(loading), 'STRING');
assert.equal(timers.length, 0);

// 14. A new link the server marks unsupported is removed and explained; an
//     older good link stays.
disconnectAll(root, ctl);
const loader = makeNode(root, 'LoadImage', [['image', 'COMBO']]);
connect(root, ctl, 0, sampler, 'steps');
await settle();
describeResponse = { controls: [{ control_id: value(ctl, 'control_id'), targets: [
    { class_type: 'KSampler', input: 'steps', status: 'ok' },
    { class_type: 'LoadImage', input: 'image', status: 'unsupported', reason: "This input checks its value before the run, so a Control can't set it.", detail: "AttributeError" },
] }] };
const bad = connect(root, ctl, 0, loader, 'image');
await settle();
assert.equal(root.links.has(bad.id), false);
assert.equal(ctl.outputs[0].links.length, 1);
assert.equal(toasts.length, 1);
assert.match(toasts[0].detail, /can't drive LoadImage image: This input checks its value before the run/);

// 15. A supported new link is kept even while another target is refused.
toasts = [];
const good = connect(root, ctl, 0, makeSampler(), 'steps');
await settle();
assert.equal(root.links.has(good.id), true);
assert.equal(toasts.length, 0);

// 16. Only wildcard targets: the kind stays and its widget is shown to pick.
disconnectAll(root, ctl);
await settle();
assert.ok(hidden(ctl, 'kind'));
const kindBefore = kind(ctl);
connect(root, ctl, 0, collector, 'value_1');
flush();
assert.equal(kind(ctl), kindBefore);
assert.ok(!hidden(ctl, 'kind'));
disconnectAll(root, ctl);
connect(root, ctl, 0, sampler, 'cfg');
flush();
assert.ok(hidden(ctl, 'kind'));

// 17. An explicit `control_after_generate: false` on an input named "seed"
//     is not a seed, as in the frontend's own `??` rule.
extension.beforeRegisterNodeDef(null, { name: 'PlainSeed', input: { required: {
    seed: ['INT', { min: -5, max: 50, control_after_generate: false }] } } });
const plain = makeNode(root, 'PlainSeed', [['seed', 'INT']]);
disconnectAll(root, ctl);
connect(root, ctl, 0, plain, 'seed');
flush();
assert.equal(kind(ctl), 'INT');

// 18. Seed takeover stays inside the Control's own range and keeps the
//     target's mode: a fixed target seed stays fixed.
extension.beforeRegisterNodeDef(null, { name: 'FixedSeed', input: { required: {
    seed: ['INT', { min: -100, max: 100, control_after_generate: 'fixed' }] } } });
const fixedSeed = makeNode(root, 'FixedSeed', [['seed', 'INT']]);
fixedSeed.widgets.push(widget('seed', -7, { options: { min: -100, max: 100 },
    linkedWidgets: [widget('control_after_generate', 'fixed')] }));
const seedCtl = makeControl();
extension.nodeCreated(seedCtl);
flush();
connect(root, seedCtl, 0, fixedSeed, 'seed');
flush();
assert.equal(kind(seedCtl), 'SEED');
const ownSeed = seedCtl.widgets.find((w) => w.name === 'seed');
assert.equal(ownSeed.options.min, 0);
assert.equal(ownSeed.options.max, 100);
assert.equal(ownSeed.value, 0);
assert.equal(value(seedCtl, 'control_after_generate'), 'fixed');

// 19. The server's concrete kind wins over a stale one (targets behind
//     virtual nodes), and a value that does not fit a new target is reported.
toasts = [];
disconnectAll(root, ctl);
await settle();
assert.equal(kind(ctl), 'INT');
describeResponse = { controls: [{ control_id: value(ctl, 'control_id'), status: 'stale_kind', target_kind: 'BOOLEAN',
    targets: [{ class_type: 'Collector', input: 'value_1', status: 'ok' }] }] };
connect(root, ctl, 0, collector, 'value_1');
await settle();
assert.equal(kind(ctl), 'BOOLEAN');
assert.equal(toasts.length, 0);
describeResponse = { controls: [{ control_id: value(ctl, 'control_id'), status: 'ok', label: 'Steps',
    error: '5000 is above the maximum of 4096.', targets: [] }] };
connect(root, ctl, 0, makeSampler(), 'steps');
await settle();
assert.equal(toasts.length, 1);
assert.match(toasts[0].detail, /^Steps: 5000 is above the maximum of 4096\. Change the value/);

// 20. Every serialization (Export (API) included) settles the kind first.
disconnectAll(root, ctl);
await settle();
connect(root, ctl, 0, sampler, 'cfg', false);
assert.equal(kind(ctl), 'INT');
const serialized = await ctl.widgets.find((w) => w.name === 'value_text').serializeValue(ctl, 0);
assert.equal(kind(ctl), 'FLOAT');
assert.equal(serialized, value(ctl, 'value_text'));

// 21. A pasted subgraph node brings a copied definition whose Control was
//     configured with the original's id: it is re-minted, nested ones too.
//     The original keeps its id, and so does a definition shared by two
//     instances (one Control node).
const original = value(innerCtl, 'control_id');
const copyDef = new Graph();
const copiedCtl = makeControl(copyDef);
copiedCtl.widgets.find((w) => w.name === 'control_id').value = original;
const nestedDef = new Graph();
const nestedCtl = makeControl(nestedDef);
nestedCtl.widgets.find((w) => w.name === 'control_id').value = original;
add(copyDef, { id: nextId++, comfyClass: 'nested-uuid', subgraph: nestedDef, inputs: [], outputs: [], widgets: [] });
root.subgraphs.set('copy', copyDef);
root.subgraphs.set('nested', nestedDef);
const pastedInstance = add(root, { id: nextId++, comfyClass: 'copy-uuid', subgraph: copyDef, inputs: [], outputs: [], widgets: [] });
extension.nodeCreated(pastedInstance);
flush();
assert.equal(value(innerCtl, 'control_id'), original);
assert.notEqual(value(copiedCtl, 'control_id'), original);
assert.notEqual(value(nestedCtl, 'control_id'), original);
assert.notEqual(value(nestedCtl, 'control_id'), value(copiedCtl, 'control_id'));
const shared = add(root, { id: nextId++, comfyClass: 'def-uuid', subgraph: inner, inputs: [], outputs: [], widgets: [] });
extension.nodeCreated(shared);
flush();
assert.equal(value(innerCtl, 'control_id'), original);
// Panel Notes mint and retain identities, re-mint colliding pastes and copied definitions.
const makeNote = (graph = root) => add(graph, {
    id: nextId++, comfyClass: 'SonderPanelNote', inputs: [], outputs: [],
    widgets: [widget('text', '# Guide'), widget('panel_order', 0), widget('panel_group', ''), widget('note_id', '')],
    setDirtyCanvas() {}, computeSize() { return [200, 100]; }, setSize() {},
});
const note = makeNote();
extension.nodeCreated(note);
flush();
const noteId = value(note, 'note_id');
assert.match(noteId, /^[a-z0-9]{8}$/);
assert.ok(hidden(note, 'note_id') && !hidden(note, 'text'));
extension.loadedGraphNode(note);
assert.equal(value(note, 'note_id'), noteId);
const pastedNote = makeNote();
extension.nodeCreated(pastedNote);
pastedNote.widgets.find(w => w.name === 'note_id').value = noteId;
flush();
assert.notEqual(value(pastedNote, 'note_id'), noteId);
const innerNote = makeNote(inner);
extension.nodeCreated(innerNote);
flush();
const innerNoteId = value(innerNote, 'note_id');
const copiedNote = makeNote(copyDef);
copiedNote.widgets.find(w => w.name === 'note_id').value = innerNoteId;
app.configuringGraph = true;
extension.nodeCreated(copiedNote);
app.configuringGraph = false;
extension.loadedGraphNode(copiedNote);
assert.equal(value(copiedNote, 'note_id'), innerNoteId);
extension.nodeCreated(pastedInstance);
flush();
assert.notEqual(value(copiedNote, 'note_id'), innerNoteId);
extension.nodeCreated(shared);
flush();
assert.equal(value(innerNote, 'note_id'), innerNoteId);
assert.ok(hidden(copiedNote, 'note_id'));

// Every export sees the actual seed mode, without beforeQueued.
assert.ok(hidden(ctl, 'seed_mode'));
const mirror = ctl.widgets.find(w => w.name === 'seed_mode');
const modeWidget = ctl.widgets.find(w => w.name === 'control_after_generate');
// The real V3 schema names this widget after its initial mode.
modeWidget.name = 'fixed';
for (const mode of ['fixed', 'increment', 'decrement', 'randomize']) {
    modeWidget.value = mode;
    mirror.value = 'stale';
    assert.equal(await mirror.serializeValue(), mode);
    assert.equal(mirror.value, mode);
}
console.log('ok');
"""


def test_sonder_control_canvas_hook():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend tests")
    script = (
        HARNESS.replace("SOURCE_PATH", json.dumps(str(ROOT / "web/js/sonder_control.js")))
        .replace("VISIBILITY_URL", json.dumps((ROOT / "web/js/widget_visibility.js").as_uri()))
        .replace("RESOLVER_URL", json.dumps((ROOT / "web/js/project_source_resolver.js").as_uri()))
    )
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_frontend_kind_rules_mirror_the_server():
    js = (ROOT / "web/js/sonder_control.js").read_text(encoding="utf-8")
    py = (ROOT / "server/control_descriptors.py").read_text(encoding="utf-8")

    def js_set(name):
        return set(re.findall(r'"([^"]+)"', re.search(rf"const {name} = new Set\(\[(.*?)\]\)", js).group(1)))

    def py_set(name):
        return set(re.findall(r'"([^"]+)"', re.search(rf"{name} = frozenset\(\{{(.*?)\}}\)", py).group(1)))

    assert js_set("WILDCARD_TYPES") == py_set("_WILDCARD_TYPES")
    assert js_set("SEED_INPUT_NAMES") == py_set("_SEED_INPUT_NAMES")
    py_kinds = re.search(r"CONTROL_KINDS = \((.*?)\)", py).group(1)
    assert js_set("CONTROL_KINDS") == set(re.findall(r'"([^"]+)"', py_kinds))
    py_modes = re.search(r"SEED_MODES = \((.*?)\)", py).group(1)
    assert js_set("SEED_MODES") == set(re.findall(r'"([^"]+)"', py_modes))
    # Both sides let an explicit control_after_generate win over the name
    # (behavior is pinned by scenario 17 here and the spec test server-side).
    assert "control_after_generate ?? SEED_INPUT_NAMES" in js
    assert re.search(r'const DESCRIBE_URL = "/sonder-editor/controls/describe";', js)
