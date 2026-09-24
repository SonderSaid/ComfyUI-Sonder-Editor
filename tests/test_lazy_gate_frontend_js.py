"""Sonder Gate frontend shape and advisories; live ECS behavior is a manual row."""

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
const consumer = await import(CONSUMER_URL);
const timers = [];
const flush = () => { while (timers.length) timers.shift()(); };
let extension;
const graph = {_nodes: [], links: new Map(), getNodeById(id) {
    return this._nodes.find((n) => String(n.id) === String(id)) || null; }};
const root = IN_SUBGRAPH ? {_nodes: [], subgraphs: new Map([['child', graph]])} : graph;
const app = {graph: root, rootGraph: root, configuringGraph: false,
    registerExtension(e) { extension = e; }};
const source = fs.readFileSync(SOURCE_PATH, 'utf8').replace(/^import .*;\r?\n/gm, '');
vm.runInNewContext(source, {app, window: {setTimeout(fn) { timers.push(fn); }},
    captureInputDefinition: consumer.captureInputDefinition,
    requiredConsumerOutputNames: consumer.requiredConsumerOutputNames});

const L = (i) => String.fromCharCode(65 + i);
const fullInputs = () => Array.from({length: 32}, (_, i) => ({
    name: `${i % 2 ? 'value' : 'when'}_${L(i >> 1)}`, type: i % 2 ? 'SONDER_gate_lane_' + (i >> 1) : '*', link: null}));
const fullOutputs = () => Array.from({length: 16}, (_, i) => ({name: L(i), type: 'SONDER_gate_lane_' + i, links: []}));
let resizes = 0;
const makeGate = (id) => {
    const n = {id, comfyClass: 'SonderGate', graph, inputs: fullInputs(), outputs: fullOutputs(),
        addInput(name, type) { this.inputs.push({name, type, link: null}); },
        addOutput(name, type) { this.outputs.push({name, type, links: []}); },
        removeInput(i) { this.inputs.splice(i, 1); },
        removeOutput(i) { this.outputs.splice(i, 1); },
        computeSize() { return [200, 200]; }, setSize() { resizes++; }};
    graph._nodes.push(n);
    return n;
};
const makeNode = (id, comfyClass, inputs = [], outputs = []) => {
    const n = {id, comfyClass, graph, inputs: inputs.map((name) => ({name, type: 'IMAGE', link: null})),
        outputs: outputs.map((name) => ({name, type: 'IMAGE', links: []}))};
    graph._nodes.push(n);
    return n;
};
let nextLink = 1;
const connect = (from, outName, to, inName) => {
    const originSlot = from.outputs.findIndex((s) => s.name === outName);
    const targetSlot = to.inputs.findIndex((s) => s.name === inName);
    assert.ok(originSlot >= 0 && targetSlot >= 0, `${outName} -> ${inName}`);
    const link = {id: nextLink++, origin_id: from.id, origin_slot: originSlot,
        target_id: to.id, target_slot: targetSlot, type: 'IMAGE'};
    graph.links.set(link.id, link);
    from.outputs[originSlot].links.push(link.id);
    to.inputs[targetSlot].link = link.id;
    for (const [node, kind, slot] of [[from, 2, originSlot], [to, 1, targetSlot]]) {
        node.onConnectionsChange?.(kind, slot, true, link);
    }
    return link;
};
const disconnect = (to, inName) => {
    const slot = to.inputs.findIndex((s) => s.name === inName);
    const link = graph.links.get(to.inputs[slot].link);
    const from = graph.getNodeById(link.origin_id);
    graph.links.delete(link.id);
    to.inputs[slot].link = null;
    const out = from.outputs[link.origin_slot];
    out.links = out.links.filter((id) => id !== link.id);
    for (const [node, kind, index] of [[from, 2, link.origin_slot], [to, 1, slot]]) {
        node.onConnectionsChange?.(kind, index, false, link);
    }
};
const names = (slots) => slots.map((s) => s.name);
const outLabel = (gate, name) => gate.outputs.find((s) => s.name === name).label;

// Consumer definitions arrive through the same hook as every other node type.
extension.beforeRegisterNodeDef(null, {name: 'NeedsImage', input: {required: {image: ['IMAGE', {}]}}});
extension.beforeRegisterNodeDef(null, {name: 'TakesImage', input: {optional: {image: ['IMAGE', {}]}}});

// 1. A fresh node settles to one lane.
const gate = makeGate(1);
extension.nodeCreated(gate);
flush();
assert.deepEqual(names(gate.inputs), ['when_A', 'value_A']);
assert.deepEqual(names(gate.outputs), ['A']);

// 2. Wiring a lane grows one spare; the value's type names the lane, and a lane
//    with a value but no condition says so.
const bridge = makeNode(10, 'SonderReferenceImageBridge', [], ['r01', 'r02']);
const resize = makeNode(11, 'ImageScale', ['image'], ['IMAGE']);
connect(resize, 'IMAGE', gate, 'value_A');
flush();
assert.deepEqual(names(gate.inputs), ['when_A', 'value_A', 'when_B', 'value_B']);
assert.equal(outLabel(gate, 'A'), 'A: IMAGE (no condition)');
assert.equal(gate.inputs[1].label, 'A: IMAGE');
assert.equal(gate.inputs[1].localized_name, 'A: IMAGE');
connect(bridge, 'r01', gate, 'when_A');
flush();
assert.equal(outLabel(gate, 'A'), 'A: IMAGE');

// 3. A proven-required consumer is announced; optional and unknown stay quiet.
const needs = makeNode(12, 'NeedsImage', ['image']);
const takes = makeNode(13, 'TakesImage', ['image']);
const unknown = makeNode(14, 'SomeSubgraphUuid', ['image']);
connect(gate, 'A', takes, 'image');
flush();
assert.equal(outLabel(gate, 'A'), 'A: IMAGE');
connect(gate, 'A', unknown, 'image');
flush();
assert.equal(outLabel(gate, 'A'), 'A: IMAGE');
connect(gate, 'A', needs, 'image');
flush();
assert.equal(outLabel(gate, 'A'), 'A: IMAGE (feeds required input)');
assert.equal(gate.outputs[0].localized_name, 'A: IMAGE (feeds required input)');

// 4. Lanes follow the highest connected lane; an explicit disconnect shrinks
//    only the canonical tail, and no surviving link changes slot.
const resize2 = makeNode(15, 'ImageScale', ['image'], ['IMAGE']);
const resize3 = makeNode(16, 'ImageScale', ['image'], ['IMAGE']);
connect(resize2, 'IMAGE', gate, 'value_B');
connect(resize3, 'IMAGE', gate, 'value_C');
flush();
assert.deepEqual(names(gate.outputs), ['A', 'B', 'C', 'D']);
disconnect(gate, 'value_C');
flush();
assert.deepEqual(names(gate.outputs), ['A', 'B', 'C']);
assert.deepEqual(names(gate.inputs), ['when_A', 'value_A', 'when_B', 'value_B', 'when_C', 'value_C']);
for (const [id, link] of graph.links) {
    if (link.target_id === gate.id) assert.equal(gate.inputs[link.target_slot].link, id);
    if (link.origin_id === gate.id) assert.ok(gate.outputs[link.origin_slot].links.includes(id));
}

// The host's configure merges saved slots into the full schema (it never
// restores the trimmed shape) and copies properties. One frame later it
// refreshes MatchType inputs, firing link-less connected=false events.
const hostConfigure = (n, saved) => {
    n.inputs = fullInputs();
    n.outputs = fullOutputs();
    n.properties = {...(saved.properties || {})};
    n.onConfigure?.(saved);
};
const hostFrameRefresh = (n) => {
    for (const [index, slot] of n.inputs.entries()) {
        if (slot.name.startsWith('value_')) n.onConnectionsChange?.(1, index, slot.link != null, undefined, slot);
    }
};
assert.equal(gate.properties.sonderGateLanes, 3, 'the shown lane count is saved');

// 5. Paste: configure restores 16 lanes, links replay one at a time with
//    queued work and frame refreshes running in between. No lane below the
//    saved floor may disappear before its link returns.
const pasted = makeGate(2);
extension.nodeCreated(pasted);
hostConfigure(pasted, {properties: {sonderGateLanes: 5}});
const sources = [20, 21, 22, 23].map((id) => makeNode(id, 'ImageScale', ['image'], ['IMAGE']));
for (const [lane, src] of [[3, sources[3]], [0, sources[0]], [2, sources[2]], [1, sources[1]]]) {
    hostFrameRefresh(pasted);
    flush();
    assert.ok(pasted.outputs.length >= 5, 'never below the saved floor mid-replay');
    connect(src, 'IMAGE', pasted, `value_${L(lane)}`);
}
hostFrameRefresh(pasted);
flush();
assert.deepEqual(names(pasted.outputs), ['A', 'B', 'C', 'D', 'E']);
for (const [lane, src] of [[0, sources[0]], [1, sources[1]], [2, sources[2]], [3, sources[3]]]) {
    const link = graph.links.get(src.outputs[0].links[0]);
    assert.equal(pasted.inputs[link.target_slot].name, `value_${L(lane)}`);
}

// 6. Reload: nothing reshapes while configuringGraph is set; afterConfigureGraph
//    trims the host's 16 lanes back to the saved floor and relabels. Link-less
//    frame refreshes never lower the floor; a real disconnect does.
const restored = makeGate(3);
extension.nodeCreated(restored);
app.configuringGraph = true;
hostConfigure(restored, {properties: {sonderGateLanes: 3}});
connect(sources[0], 'IMAGE', restored, 'value_B');
flush();
assert.equal(restored.outputs.length, 16, 'no reshape while configuring');
app.configuringGraph = false;
extension.afterConfigureGraph();
assert.deepEqual(names(restored.outputs), ['A', 'B', 'C']);
assert.equal(outLabel(restored, 'B'), 'B: IMAGE (no condition)');
hostFrameRefresh(restored);
flush();
assert.deepEqual(names(restored.outputs), ['A', 'B', 'C']);
connect(sources[1], 'IMAGE', restored, 'value_C');
flush();
assert.deepEqual(names(restored.outputs), ['A', 'B', 'C', 'D']);
disconnect(restored, 'value_C');
flush();
assert.deepEqual(names(restored.outputs), ['A', 'B', 'C']);
assert.equal(restored.properties.sonderGateLanes, 3);
console.log('ok');
"""


@pytest.mark.parametrize("in_subgraph", [False, True])
def test_gate_shape_advisories_and_paste_order(in_subgraph):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend tests")
    script = (
        HARNESS.replace("IN_SUBGRAPH", json.dumps(in_subgraph))
        .replace("SOURCE_PATH", json.dumps(str(ROOT / "web/js/lazy_switch_nodes.js")))
        .replace("CONSUMER_URL", json.dumps((ROOT / "web/js/consumer_input_requirements.js").as_uri()))
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_gate_lane_limit_matches_backend():
    python_source = (ROOT / "nodes/lazy_switches.py").read_text(encoding="utf-8")
    js_source = (ROOT / "web/js/lazy_switch_nodes.js").read_text(encoding="utf-8")
    lanes = int(re.search(r"^MAX_GATE_LANES = (\d+)$", python_source, re.M).group(1))

    assert int(re.search(r"^const MAX_GATE_LANES = (\d+);$", js_source, re.M).group(1)) == lanes
    last_letter = chr(ord("A") + lanes - 1)
    assert f"/^(when|value)_([A-{last_letter}])$/" in js_source


def test_consumer_requirement_module_stays_free_of_host_imports():
    source = (ROOT / "web/js/consumer_input_requirements.js").read_text(encoding="utf-8")
    assert not re.search(r"^import ", source, re.M)
    assert not re.search(r"\b(document|window)\.", source)
