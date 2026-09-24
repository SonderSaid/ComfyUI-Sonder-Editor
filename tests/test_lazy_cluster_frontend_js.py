"""Cluster restoration guards; real ECS link restoration is checked in-browser."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("in_subgraph", [False, True])
def test_cluster_waits_for_restored_widgets_and_connections(in_subgraph):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend tests")
    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const timers = [];
let extension;
const graph = {_nodes: [], links: new Map(), getNodeById() { return null; }};
const root = IN_SUBGRAPH ? {_nodes: [], subgraphs: new Map([['child', graph]])} : graph;
const app = {graph: root, rootGraph: root, configuringGraph: true,
    registerExtension(e) { extension = e; }};
const source = fs.readFileSync(SOURCE_PATH, 'utf8').replace(/^import .*;\r?\n/gm, '');
// Imports are stripped for vm; the Cluster never consults consumer definitions.
vm.runInNewContext(source, {app, window: {setTimeout(fn) { timers.push(fn); }},
    captureInputDefinition() {}, requiredConsumerOutputNames() { return []; }});
let originalCalls = 0;
let resizeCalls = 0;
const n = {comfyClass: 'SonderLazyCluster', graph,
    inputs: Array.from({length: 24}, (_, i) => ({name: `b${Math.floor(i/8)}_l${i%8}`, type: '*', link: null})),
    outputs: Array.from({length: 8}, (_, i) => ({name: String.fromCharCode(65+i), type: '*', links: []})),
    widgets: [{name:'select',value:0},{name:'branches',value:2},{name:'lanes',value:2}],
    addInput(name,type) { this.inputs.push({name,type,link:null}); },
    addOutput(name,type) { this.outputs.push({name,type,links:[]}); },
    removeInput(i) { this.inputs.splice(i,1); },
    removeOutput(i) { this.outputs.splice(i,1); },
    onConnectionsChange() { originalCalls++; return 'original-result'; },
    computeSize() { return [200,200]; }, setSize() { resizeCalls++; }
};
graph._nodes.push(n);
extension.nodeCreated(n);
// A transitional positional link appears to occupy lane 7 during restore.
n.inputs[7].link = 7;
assert.equal(n.onConnectionsChange(1,7,true), 'original-result');
for (const fn of timers.splice(0)) fn();
assert.equal(originalCalls,1);
assert.equal(n.widgets[2].value,2);
assert.equal(n.inputs.length,24);
assert.equal(resizeCalls,0);
// The host has now restored canonical endpoints and saved widget values.
for (const s of n.inputs) s.link = s.name === 'b2_l1' ? 7 : null;
n.widgets[1].value = 3;
n.widgets[2].value = 2;
app.configuringGraph = false;
extension.afterConfigureGraph();
assert.equal(n.inputs.length,6);
assert.equal(n.outputs.length,2);
assert.equal(n.inputs.find(s=>s.name==='b2_l1').link,7);
assert.equal(resizeCalls,1);
// Ordinary widget edits remain immediate and connected lanes cannot be hidden.
n.widgets[2].value = 3;
n.widgets[2].callback(3);
assert.equal(n.outputs.length,3);
n.widgets[2].value = 1;
n.widgets[2].callback(1);
assert.equal(n.widgets[2].value,2);
assert.equal(n.outputs.length,2);
// Model ECS connectivity by slot position, rejecting occupied destinations.
// A direct array sort (or sequential endpoint swap) would corrupt these links.
n.id = 'cluster';
n.widgets[1].value = 3;
n.widgets[2].value = 1;
n.outputs = [{name:'A',type:'*',links:[]}];
n.inputs = ['b1_l0','b0_l0','b2_l0'].map(name=>({name,type:'*'}));
const originals = new Map();
for (const [index, slot] of n.inputs.entries()) {
    let position = index;
    const link = {id:index+10, get target_slot() { return position; },
        set target_slot(value) {
            assert.ok(![...graph.links.values()].some(l=>l!==this && l.target_slot===value), 'occupied target');
            position = value;
        }};
    graph.links.set(link.id, link);
    originals.set(slot.name, link.id);
    Object.defineProperty(slot, 'link', {get() {
        return [...graph.links.values()].find(l=>l.target_slot===n.inputs.indexOf(slot))?.id ?? null;
    }});
}
graph.floatingLinks = new Map([[99,{target_id:n.id,target_slot:0}]]);
n.widgets[2].callback(1);
assert.deepEqual(n.inputs.map(s=>s.name), ['b0_l0','b1_l0','b2_l0']);
for (const slot of n.inputs) assert.equal(slot.link, originals.get(slot.name));
assert.equal(graph.floatingLinks.get(99).target_slot,1);
console.log('ok');
"""
    script = script.replace("IN_SUBGRAPH", json.dumps(in_subgraph)).replace(
        "SOURCE_PATH", json.dumps(str(ROOT / "web/js/lazy_switch_nodes.js"))
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
