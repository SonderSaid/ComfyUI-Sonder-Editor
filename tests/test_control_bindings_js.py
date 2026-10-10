"""Relay bindings on a fake LiteGraph graph: resolve, export, copy rule, census.

`web/js/control_bindings.js` is pure, so it is imported directly. The Relay
extension (`web/js/control_relay.js`) runs in a Node vm with its imports
injected. Real frontend 1.53.10 ordering (nodeCreated before configure on a
paste) and real gestures are checked live and by maintainer rows; these
fakes prove the module logic only.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const B = await import(BINDINGS_URL);

class Graph {
    constructor() { this._nodes = []; this.links = new Map(); this.subgraphs = new Map(); this.inputs = []; }
    getNodeById(id) { return this._nodes.find(n => String(n.id) === String(id)) || null; }
    getLink(id) { return this.links.get(id) ?? this.links.get(Number(id)) ?? null; }
}
let nextId = 1;
const widget = (name, value, extra = {}) => ({ name, value, options: {}, ...extra });
const add = (graph, node) => { node.graph = graph; graph._nodes.push(node); return node; };
const node = (graph, comfyClass, widgets = [], inputs = [], extra = {}) => add(graph, {
    id: nextId++, comfyClass, type: comfyClass, widgets, properties: {}, mode: 0,
    inputs: inputs.map(name => ({ name, link: null, widget: { name } })), ...extra });
const mode = widget('control_after_generate', 'increment');
const root = new Graph();
const relay = node(root, 'SonderControlRelay', [widget('manifest', '')]);
const sampler = node(root, 'KSampler', [widget('seed', 7, { linkedWidgets: [mode] }), widget('steps', 20),
    widget('sampler_name', 'euler', { options: { values: ['euler', 'lcm'] } })], ['seed', 'steps', 'sampler_name', 'model']);
sampler.inputs[3].link = 99; // `model` is linked
const save = node(root, 'SonderSaveVideo', [widget('mark_queue_complete', false)], ['mark_queue_complete']);
// A subgraph host whose promoted input `steps` feeds two inner samplers.
const def = new Graph();
const innerA = node(def, 'KSampler', [widget('steps', 4)], ['steps']);
const innerB = node(def, 'KSampler', [widget('steps', 4)], ['steps']);
def.inputs = [{ name: 'steps', linkIds: [11, 12] }];
def.links.set(11, { origin_id: '-10', origin_slot: 0, target_id: innerA.id, target_slot: 0 });
def.links.set(12, { origin_id: '-10', origin_slot: 0, target_id: innerB.id, target_slot: 0 });
innerA.inputs[0].link = 11; innerB.inputs[0].link = 12;
root.subgraphs.set('def', def);
const hostWidget = widget('steps', 9);
const host = node(root, 'subgraph-def', [hostWidget], [], { subgraph: def });
host.inputs = [{ name: 'steps', link: null, widget: { name: 'steps' } }];
host.getWidgetFromSlot = input => (input.name === 'steps' ? hostWidget : null);
const anchor = (n, name, id) => { n.properties.sonderBindings = { ...(n.properties.sonderBindings || {}), [name]: id }; };
const saved = bindings => JSON.stringify({ v: 1, bindings, notes: { intro: { text: '# Hi' } }, future: { keep: true } });
"""

BINDINGS = PRELUDE + r"""
anchor(sampler, 'seed', 'seed1'); anchor(sampler, 'steps', 'steps1'); anchor(sampler, 'model', 'model1');
anchor(save, 'mark_queue_complete', 'adv1'); anchor(host, 'steps', 'promo1');
anchor(innerA, 'steps', 'inner1'); anchor(sampler, 'sampler_name', 'orphan1');
const manifestText = saved({ seed1: { label: 'Seed' }, steps1: { label: 'Steps', order: 3 }, model1: {},
    adv1: { section: 'editor' }, promo1: {}, inner1: {}, gone1: { label: 'Gone' } });
relay.widgets[0].value = manifestText;

// Resolve: statuses, targets, native widgets and seed modes.
const { bindings, suggestions } = B.resolveBindings(root, B.parseManifest(manifestText));
assert.equal(bindings.seed1.status, 'ok');
assert.equal(bindings.seed1.widget, sampler.widgets[0]);
assert.equal(bindings.seed1.modeWidget, mode);
assert.deepEqual(bindings.seed1.targets, [{ path: String(sampler.id), input: 'seed' }]);
assert.equal(bindings.model1.status, 'linked');
assert.equal(bindings.adv1.status, 'ok');
assert.equal(bindings.promo1.status, 'ok');
assert.equal(bindings.promo1.widget, hostWidget);
assert.deepEqual(B.publicTargets(bindings.promo1.targets),
    [{ path: `${host.id}:${innerA.id}`, input: 'steps' }, { path: `${host.id}:${innerB.id}`, input: 'steps' }]);
assert.equal(bindings.inner1.status, 'missing'); // left inside a definition by Convert: rebindable
assert.equal(bindings.inner1.insideSubgraph, true);
assert.equal(bindings.gone1.status, 'missing');
assert.deepEqual(suggestions, [{ binding_id: 'orphan1', targets: [{ path: String(sampler.id), input: 'sampler_name' }] }]);

// Export: pure (never writes the widget), carries targets, statuses, modes,
// suggestions and unknown keys, and never throws.
const exported = JSON.parse(B.exportManifest(manifestText, root));
assert.equal(relay.widgets[0].value, manifestText);
assert.deepEqual(exported.future, { keep: true });
assert.equal(exported.bindings.seed1.seed_mode, 'increment');
assert.equal(exported.bindings.seed1.label, 'Seed');
assert.equal(exported.bindings.steps1.order, 3);
assert.equal(exported.bindings.model1.status, 'linked');
assert.equal(exported.bindings.promo1.targets.length, 2);
assert.equal(exported.suggestions[0].binding_id, 'orphan1');
assert.equal(B.exportManifest('{broken', root), '{broken');
const throwing = new Proxy(root, { get(target, key) { if (key === '_nodes') throw Error('graph exploded'); return target[key]; } });
assert.match(JSON.parse(B.exportManifest(manifestText, throwing)).error, /graph exploded/);

// Duplicate anchors and muted nodes.
const copy = node(root, 'KSampler', [widget('steps', 1)], ['steps']);
anchor(copy, 'steps', 'steps1');
assert.equal(B.resolveBindings(root, B.parseManifest(manifestText)).bindings.steps1.status, 'duplicate');
root._nodes.splice(root._nodes.indexOf(copy), 1);
sampler.mode = 2;
assert.equal(B.resolveBindings(root, B.parseManifest(manifestText)).bindings.seed1.status, 'missing');
sampler.mode = 0;

// Hostile anchors are ignored, not trusted.
const hostile = node(root, 'KSampler', [widget('cfg', 1)], ['cfg']);
hostile.properties.sonderBindings = JSON.parse('{"__proto__": "x", "cfg": "bad id!", "steps": "ok_id", "seed": "constructor"}');
assert.deepEqual(B.readAnchors(hostile), { steps: 'ok_id' });
hostile.properties.sonderBindings = ['cfg'];
assert.deepEqual(B.readAnchors(hostile), {});

// Manifest hygiene mirrors the server.
assert.throws(() => B.parseManifest('{"__proto__": {}}'), /reserved key/);
assert.throws(() => B.parseManifest(JSON.stringify({ bindings: { 'bad id': {} } })), /id is invalid/);
assert.throws(() => B.parseManifest('x'.repeat(B.MAX_MANIFEST_BYTES + 1)), /too large/);
assert.throws(() => B.parseManifest(JSON.stringify({ v: 2 })), /newer Sonder/);
assert.deepEqual(B.parseManifest(''), B.emptyManifest());

// editManifest keeps unknown keys and strips export-only facts.
const edited = JSON.parse(B.editManifest(JSON.stringify(exported), m => { m.bindings.seed1.label = 'New'; }));
assert.equal(edited.bindings.seed1.label, 'New');
assert.deepEqual(edited.future, { keep: true });
assert.ok(!('suggestions' in edited) && !('targets' in edited.bindings.seed1) && !('status' in edited.bindings.seed1));

// Census sees what an export cannot.
const second = node(root, 'SonderControlRelay', [widget('manifest', '')], [], { mode: 4 });
node(def, 'SonderControlRelay', [widget('manifest', '')]);
assert.deepEqual(B.relayCensus(root).census, { active: 1, muted: 0, bypassed: 1, nested: 1 });
assert.equal(B.relayCensus(root).active[0], relay);
second.mode = 0;
assert.equal(B.relayCensus(root).census.active, 2);

// Ids and denylist mirror.
assert.match(B.mintBindingId(), /^[A-Za-z0-9_-]{12}$/);
assert.ok(B.isDenied('SonderEditor', 'selection_start') && B.isDenied('SonderMetadataCollectorV3', 'label_2'));
assert.ok(!B.isDenied('KSampler', 'steps'));
console.log('ok');
"""

COPY_RULE = PRELUDE + r"""
// The copy rule drops anchors inside configure, unless the graph is loading
// (load, undo and redo configure with configuringGraph set).
let configuring = false;
const pasted = node(root, 'KSampler', [widget('steps', 1)], ['steps']);
let ownConfigure = 0;
pasted.onConfigure = function () { ownConfigure++; };
B.installAnchorCopyRule(pasted, () => configuring);
B.installAnchorCopyRule(pasted, () => configuring); // idempotent
assert.ok(B.copyRuleInPlace(pasted));
const configure = (n, info) => { n.properties = structuredClone(info.properties); n.onConfigure?.(info); };
configuring = true;
configure(pasted, { properties: { sonderBindings: { steps: 'steps1' } } });
assert.deepEqual(pasted.properties.sonderBindings, { steps: 'steps1' });
configuring = false;
configure(pasted, { properties: { sonderBindings: { steps: 'steps1' }, other: 1 } });
assert.ok(!('sonderBindings' in pasted.properties) && pasted.properties.other === 1);
assert.equal(ownConfigure, 2); // the node's own onConfigure still runs
pasted.onConfigure = () => {};  // an extension replacing the chain is detectable
assert.ok(!B.copyRuleInPlace(pasted));

// The Relay extension: copy rule first in nodeCreated, hidden manifest,
// pure serializeValue that falls back instead of throwing.
let extension;
const app = { rootGraph: root, graph: root, configuringGraph: false, registerExtension(e) { extension = e; } };
const hidden = [];
const source = fs.readFileSync(RELAY_PATH, 'utf8').replace(/^import [\s\S]*?;\r?\n/gm, '');
const intervals = [];
vm.runInNewContext(source, {
    app, console, Symbol, Set, document: { hidden: false },
    api: { addEventListener() {} },
    setInterval: (fn, ms) => { intervals.push([fn, ms]); return intervals.length; }, clearInterval: () => {},
    syncRelayRows: () => false,
    setWidgetHidden: (w, h) => { hidden.push([w.name, h]); return true; },
    commitWidgetVisibility: () => {},
    exportManifest: B.exportManifest, installAnchorCopyRule: B.installAnchorCopyRule, isRelay: B.isRelay,
});
const fresh = node(root, 'KSampler', [widget('steps', 2)], ['steps']);
extension.nodeCreated(fresh);
assert.ok(B.copyRuleInPlace(fresh));
configure(fresh, { properties: { sonderBindings: { steps: 'x1' } } });
assert.ok(!('sonderBindings' in fresh.properties));
anchor(sampler, 'steps', 'steps1');
relay.widgets[0].value = saved({ steps1: { label: 'Steps' } });
extension.nodeCreated(relay);
assert.deepEqual(hidden, [['manifest', true]]);
const value = relay.widgets[0].serializeValue({ fake: 'dto' }, 0);
assert.equal(relay.widgets[0].value, saved({ steps1: { label: 'Steps' } }));
assert.deepEqual(JSON.parse(value).bindings.steps1.targets, [{ path: String(sampler.id), input: 'steps' }]);
relay.widgets[0].value = 7; // junk reads as an empty manifest, never a throw
assert.deepEqual(JSON.parse(relay.widgets[0].serializeValue()).bindings, {});
extension.loadedGraphNode(relay); // idempotent
assert.deepEqual(hidden, [['manifest', true]]);
// Nodes 2.0 reads the widget's own state: its `hidden` accessor is set too.
class StateWidget { constructor() { this.name = 'manifest'; this.value = ''; this._state = { options: {} }; }
    get hidden() { return this._state.options.hidden; } set hidden(v) { this._state.options.hidden = v; } }
const domManifest = new StateWidget();
const relay2 = node(root, 'SonderControlRelay', [domManifest]);
extension.nodeCreated(relay2);
assert.equal(domManifest._state.options.hidden, true);
console.log('ok');
"""


def run(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend tests")
    script = (script.replace("BINDINGS_URL", json.dumps((ROOT / "web/js/control_bindings.js").as_uri()))
              .replace("RELAY_PATH", json.dumps(str(ROOT / "web/js/control_relay.js"))))
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


CANDIDATES = PRELUDE + r"""
// Manage's candidates: root widgets and promoted host inputs, never anchored,
// linked, denied, companion, muted, virtual or legacy-Primitive ones.
anchor(sampler, 'seed', 'seed1');
const editor = node(root, 'SonderEditor', [widget('selection_start', 0), widget('fps', 24)]);
const muted = node(root, 'KSampler', [widget('steps', 1)], [], { mode: 2 });
const primitive = node(root, 'PrimitiveNode', [widget('value', 3)]);
const reroute = node(root, 'Reroute', [widget('x', 1)], [], { isVirtualNode: true });
const frontOnly = node(root, 'KSampler', [widget('preview', 1, { options: { serialize: false } })]);
host.isVirtualNode = true; // as frontend 1.53.10 reports subgraph hosts
const { candidates, truncated } = B.listCandidates(root);
const keys = candidates.map(c => `${c.path}/${c.input}`);
assert.ok(!truncated);
assert.ok(keys.includes(`${sampler.id}/steps`) && keys.includes(`${sampler.id}/sampler_name`));
for (const gone of [`${sampler.id}/seed`, `${sampler.id}/model`, `${sampler.id}/control_after_generate`, `${relay.id}/manifest`,
    `${editor.id}/selection_start`, `${muted.id}/steps`, `${primitive.id}/value`, `${reroute.id}/x`, `${frontOnly.id}/preview`]) {
    assert.ok(!keys.includes(gone), gone);
}
assert.ok(keys.includes(`${editor.id}/fps`));
const promoted = candidates.find(c => c.path === String(host.id));
assert.deepEqual(promoted.targets, [{ path: `${host.id}:${innerA.id}`, input: 'steps' }, { path: `${host.id}:${innerB.id}`, input: 'steps' }]);
assert.deepEqual(B.listCandidates(root, { filter: 'SAMPLER_n' }).candidates.map(c => c.input), ['sampler_name']);
assert.deepEqual(B.listCandidates(root, {filter:'friendly choice', labels:[
    {path:String(sampler.id),input:'sampler_name',label:'Friendly choice'}
]}).candidates.map(c=>c.input),['sampler_name']);
const capped = B.listCandidates(root, { limit: 2 });
assert.equal(capped.candidates.length, 2); assert.ok(capped.truncated);
// A structural write resolves the same candidate on the live graph.
const live = B.resolveCandidate(root, { path: String(host.id), input: 'steps' });
assert.equal(live.node, host); assert.deepEqual(live.targets, promoted.targets);
assert.equal(B.resolveCandidate(root, { path: String(sampler.id), input: 'model' }), null); // linked
// Anchor helpers keep other entries and restore exactly.
sampler.properties.sonderBindings = { seed: 'seed1', junk: 7 };
const snap = B.anchorSnapshot(sampler);
B.setAnchor(sampler, 'steps', 'new1');
assert.deepEqual(sampler.properties.sonderBindings, { seed: 'seed1', junk: 7, steps: 'new1' });
assert.deepEqual(B.dropAnchors(root, 'seed1'), [sampler]);
assert.deepEqual(sampler.properties.sonderBindings, { junk: 7, steps: 'new1' });
B.restoreAnchors(sampler, snap); assert.deepEqual(sampler.properties.sonderBindings, { seed: 'seed1', junk: 7 });
B.restoreAnchors(save, undefined); assert.ok(!('sonderBindings' in save.properties));
// Nodes 2.0 hands out reactive proxies; a snapshot must not structuredClone them.
sampler.properties.sonderBindings = new Proxy({ seed: 'seed1' }, {});
assert.deepEqual(B.anchorSnapshot(sampler), { seed: 'seed1' });
console.log('ok');
"""


def test_resolve_export_hygiene_and_census():
    run(BINDINGS)


def test_manage_candidates_and_anchor_helpers():
    run(CANDIDATES)


def test_copy_rule_and_relay_extension():
    run(COPY_RULE)


def test_description_survives_manifest_edits_and_api_export_with_code_point_cap():
    run(PRELUDE + r"""
const description = '😀'.repeat(1000);
const descriptionSaved = B.editManifest('', m => { m.description = description; });
assert.equal(B.parseManifest(descriptionSaved).description, description);
const edited = B.editManifest(descriptionSaved, m => { m.groups = {Shared:{order:1}}; });
assert.equal(JSON.parse(B.exportManifest(edited, root)).description, description);
assert.throws(() => B.parseManifest(JSON.stringify({description:description+'x'})), /1000/);
assert.throws(() => B.parseManifest(JSON.stringify({description:42})), /text/);
console.log('ok');
""")
