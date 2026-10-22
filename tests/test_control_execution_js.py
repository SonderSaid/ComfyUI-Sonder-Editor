"""Phase 1A canvas side: inactive settings, node switches and their writer.

Pure resolver checks against graph-shaped objects, Python/JS parity for the
shared readers, and the canvas writer's mode writes (one history step, exact
rollback, member drift refused). The native group rule and serializer are
verified live against the installed frontend separately (roadmap rows).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from server import control_manifest as cm

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

GRAPH = r'''
import assert from 'node:assert/strict';
const B = await import(BINDINGS_URL);
const clone = x => JSON.parse(JSON.stringify(x));
class Graph {
    constructor(){this._nodes=[];this.links=new Map();this.subgraphs=new Map();this.inputs=[];this.groups=[];this.dirty=0;}
    setDirtyCanvas(){this.dirty++;}
    getLink(id){return this.links.get(id)??null;}
    getNodeById(id){return this._nodes.find(n=>String(n.id)===String(id))??null;}
}
const widget = (name,value,extra={}) => ({name,value,options:{},callback(){},...extra});
// boundingRect: x, y, w, h; the native rule tests its centre.
const add = (g,type,id,{widgets=[],inputs=[],rect=[0,0,10,10],title,mode=0}={}) => {
    const n={id,type,comfyClass:type,title:title??type,widgets,graph:g,mode,properties:{},boundingRect:rect,
        inputs:inputs.map(name=>({name,link:null,widget:{name}}))};
    g._nodes.push(n);return n;};
const group = (g,id,box,title='Group') => { const gr={id,title,_bounding:box}; g.groups.push(gr); return gr; };
'''


def run(script, tmp_path, name="case.mjs"):
    text = GRAPH.replace("BINDINGS_URL", json.dumps((ROOT / "web/js/control_bindings.js").as_uri())) + script
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    result = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout


def test_group_membership_follows_the_native_centre_rule(tmp_path):
    run(r'''
const root = new Graph();
const inside = add(root,'KSampler',1,{rect:[10,10,20,20]});        // centre 20,20
const edge = add(root,'KSampler',2,{rect:[90,0,20,20]});           // centre 100,10: right edge is open
const left = add(root,'KSampler',3,{rect:[-10,40,20,20]});         // centre 0,50: left edge is closed
const out = add(root,'KSampler',4,{rect:[200,200,10,10]});
const g = group(root,7,[0,0,100,100]);
assert.deepEqual(B.groupMembers(g, root._nodes).map(n=>n.id), [1,3]);
assert.deepEqual(B.groupMembers({}, root._nodes), []);
''', tmp_path)


def test_resolve_execution_for_nodes_instances_and_groups(tmp_path):
    run(r'''
const root = new Graph();
const a = add(root,'KSampler',3,{rect:[10,10,20,20],title:'Pass 1'});
const b = add(root,'KSampler',4,{rect:[40,10,20,20],mode:2});
const reroute = add(root,'Reroute',5,{rect:[60,10,4,4]}); reroute.isVirtualNode = true;
const host = add(root,'uuid-sub',6,{rect:[10,60,20,20],mode:4}); host.subgraph = new Graph(); host.isVirtualNode = true;
const editor = add(root,'SonderEditor',1,{rect:[500,500,10,10]});
const relayNode = add(root,'SonderControlRelay',2,{rect:[520,500,10,10]});
group(root,7,[0,0,100,100],'First pass');
group(root,8,[480,480,100,100],'Infra');
group(root,9,[900,900,10,10],'Empty');
a.properties.sonderExecution = 'node';
host.properties.sonderExecution = 'inst';
editor.properties.sonderExecution = 'infra';
const controls = {
    node:{target:{kind:'node'}}, inst:{target:{kind:'instance'}}, infra:{target:{kind:'node'}},
    grp:{target:{kind:'group',group_id:7}}, ginfra:{target:{kind:'group',group_id:8}}, gempty:{target:{kind:'group',group_id:9}},
    gone:{target:{kind:'group',group_id:99}}, lost:{target:{kind:'node'}},
};
const manifest = {v:1, bindings:{}, notes:{}, execution:{v:1, controls}};
const {controls: r} = B.resolveExecution(root, manifest);
assert.equal(r.node.status,'ok'); assert.equal(r.node.mode,'enabled'); assert.equal(r.node.title,'Pass 1');
assert.equal(r.inst.status,'ok'); assert.equal(r.inst.mode,'bypassed'); assert.equal(r.inst.members[0].kind,'instance');
assert.equal(r.infra.status,'unsupported'); assert.equal(r.infra.problem,'infrastructure');
// Reroutes never run, so they are no group's member; a subgraph host is.
assert.deepEqual(r.grp.members.map(m=>[m.path,m.mode]), [['3','enabled'],['4','muted'],['6','bypassed']]);
assert.equal(r.grp.mode,'mixed');
assert.equal(r.ginfra.problem,'infrastructure'); assert.equal(r.gempty.problem,'empty');
assert.equal(r.gone.status,'missing'); assert.equal(r.lost.status,'missing');
a.properties.sonderExecution = 'dup'; b.properties.sonderExecution = 'dup';
assert.equal(B.resolveExecution(root,{execution:{v:1,controls:{dup:{target:{kind:'node'}}}}}).controls.dup.status,'duplicate');
assert.equal(B.resolveExecution(root,{execution:{v:1,controls:{dup2:{target:{kind:'instance'}}}}}).controls.dup2.status,'missing');
b.properties.sonderExecution = 'kind';
assert.equal(B.resolveExecution(root,{execution:{v:1,controls:{kind:{target:{kind:'instance'}}}}}).controls.kind.problem,'kind');
const many = new Graph();
for (let i=0;i<65;i++) add(many,'KSampler',i+10,{rect:[i,0,1,1]});
group(many,1,[-1,-1,100,100]);
assert.equal(B.resolveExecution(many,{execution:{v:1,controls:{g:{target:{kind:'group',group_id:1}}}}}).controls.g.problem,'too_many');
assert.match(B.resolveExecution(root,{execution:{v:3}}).error, /another Sonder version/);

// Candidates: nodes, instances and groups; infrastructure classes first so the server can refuse them.
const {candidates} = B.listExecutionCandidates(root);
const byKey = Object.fromEntries(candidates.map(c=>[c.kind==='group'?`g${c.group_id}`:c.path, c]));
assert.ok(!byKey['5']); assert.equal(byKey['6'].kind,'instance'); assert.equal(byKey['4'].anchored_by,'kind');
assert.equal(byKey.g8.classes[0].startsWith('Sonder'), true); assert.equal(byKey.g7.member_count, 3);
assert.deepEqual(B.listExecutionCandidates(root,{filter:'first'}).candidates.map(c=>c.group_id), [7]);
// Groups come first, so a large graph's nodes never crowd them out.
assert.equal(candidates[0].kind, 'group');
// An anchor naming no saved switch (a deleted Relay's) never locks its node out.
const known = new Set(['inst']);
const listed = B.listExecutionCandidates(root,{known}).candidates;
assert.equal(listed.find(c=>c.path==='4').anchored_by, null); assert.equal(listed.find(c=>c.path==='6').anchored_by, 'inst');
// A batch sends only the candidates it names.
assert.deepEqual(B.executionCandidatesFor(root, [{kind:'group',group_id:7},{kind:'node',path:'4'},{kind:'node',path:'404'}], {known})
    .map(c=>[c.kind, c.group_id ?? c.path, c.anchored_by ?? null]), [['group',7,null],['node','4',null]]);
''', tmp_path)


def test_export_strip_and_newer_sections(tmp_path):
    run(r'''
const root = new Graph();
const a = add(root,'KSampler',3,{rect:[10,10,20,20],title:'Pass 1',mode:2,widgets:[widget('steps',20)],inputs:['steps']});
a.properties.sonderExecution = 'x';
a.properties.sonderBindings = {steps:'steps'};
const text = JSON.stringify({v:1, bindings:{steps:{label:'Steps'}}, notes:{}, execution:{v:1, controls:{x:{label:'', target:{kind:'node'}, future:1}}}});
const out = JSON.parse(B.exportManifest(text, root));
assert.equal(out.execution.controls.x.status,'ok');
assert.deepEqual(out.execution.controls.x.members, [{path:'3',class_type:'KSampler',mode:'muted',kind:'node',title:'Pass 1'}]);
assert.equal(out.execution.controls.x.future, 1);
assert.equal(out.bindings.steps.participation,'inactive');
// Saving strips export facts, keeping unknown keys.
const saved = JSON.parse(B.editManifest(JSON.stringify(out), () => {}));
assert.deepEqual(saved.execution.controls.x, {label:'', target:{kind:'node'}, future:1});
assert.equal(saved.bindings.steps.participation, undefined);
// A newer section is exported and saved exactly as it is.
const newer = JSON.stringify({v:1, bindings:{}, notes:{}, execution:{v:9, controls:{x:{status:'keep'}}}});
assert.deepEqual(JSON.parse(B.exportManifest(newer, root)).execution, {v:9, controls:{x:{status:'keep'}}});
assert.deepEqual(JSON.parse(B.editManifest(newer, () => {})).execution, {v:9, controls:{x:{status:'keep'}}});
// A pasted copy carries neither kind of anchor.
let configuring = false;
B.installAnchorCopyRule(a, () => configuring);
a.onConfigure();
assert.equal(a.properties.sonderExecution, undefined); assert.equal(a.properties.sonderBindings, undefined);
''', tmp_path)


def test_inactive_bodies_carry_values_links_and_reasons(tmp_path):
    run(r'''
const root = new Graph();
const a = add(root,'KSampler',3,{title:'Pass 1',mode:2,widgets:[widget('steps',20),widget('seed',5),widget('hidden',1,{options:{serialize:false}})],inputs:['steps','seed','model']});
a.inputs[2].link = 11; root.links.set(11,{origin_id:2,origin_slot:0});
a.properties.sonderBindings = {steps:'steps'};
const host = add(root,'uuid-sub',6,{mode:4,title:'Upscale'}); host.isVirtualNode = true;
const inner = new Graph(); host.subgraph = inner;
const consumer = add(inner,'KSampler',2,{widgets:[widget('steps',9)],inputs:['steps','model']});
const loader = add(inner,'Loader',5);
// The native shape: the promoted input is linked from the subgraph's input node.
consumer.inputs[0].link = 21; consumer.inputs[1].link = 22;
inner.inputs = [{name:'steps',linkIds:[21]}];
inner.links.set(21,{origin_id:-10,origin_slot:0,target_id:2,target_slot:0});
inner.links.set(22,{origin_id:5,origin_slot:1,target_id:2,target_slot:1});
host.inputs = [{name:'steps',link:null,_subgraphSlot:{name:'steps'}}];
host.getWidgetFromSlot = () => widget('steps',12);
host.properties.sonderBindings = {steps:'promo'};
const manifest = {v:1, bindings:{steps:{}, promo:{}}, notes:{}};
const {bindings} = B.resolveBindings(root, manifest);
assert.equal(bindings.promo.status,'ok'); assert.equal(bindings.promo.participation,'inactive');
assert.deepEqual(bindings.promo.inactiveReason, {mode:'bypassed', by:'parent', label:'Upscale'});
const bodies = B.inactiveBodies(bindings);
assert.deepEqual(bodies['3'], {class_type:'KSampler', inputs:{steps:20, seed:5, model:['2',0]}, _meta:{title:'Pass 1'},
    mode:'muted', by:'self', label:'Pass 1'});
// The promoted input carries the host's value, as the native serializer
// resolves it; a real inner origin is prefixed like an expanded inner id.
assert.deepEqual(bodies['6:2'].inputs, {steps:12, model:['6:5',1]});
assert.equal(bodies['6:2'].by, 'parent');
// A body that can't be serialized is left out, never an error for the panel.
const cyclic = {}; cyclic.self = cyclic;
a.widgets.push(widget('loop', cyclic));
const kept = B.inactiveBodies(B.resolveBindings(root, manifest).bindings);
assert.equal(kept['3'], undefined); assert.ok(kept['6:2']);
a.widgets.pop();
// Manage offers only settings that run now.
a.mode = 0;
assert.ok(!B.listCandidates(root).candidates.some(c => c.path === '6'));
''', tmp_path)


def test_python_js_reader_parity(tmp_path):
    node = {"mode": "muted", "node": {"path": "6", "class_type": "KSampler"}}
    presets = {
        "only": {"name": "Only", "members": {}, "execution": {"a": node}},
        "both": {"name": "Both", "members": {"s": {"value": "1"}}, "execution": {"a": node, "g": {"modes": {"6": "muted", "8": "enabled"}}}},
        "conflict": {"name": "C", "members": {}, "execution": {"a": node, "g": {"modes": {"6": "bypassed"}}}},
        "junk": {"name": "J", "members": {}, "execution": {"a": {"mode": "x", "node": {"path": "6", "class_type": "K"}},
                                                           "b": {"modes": {"a:b": "muted"}}}},
        "over": {"name": "O", "members": {f"b{i}": {"value": 1} for i in range(63)}, "execution": {"g": {"modes": {"1": "muted", "2": "muted"}}}},
        "empty_exec": {"name": "E", "members": {}, "execution": {}},
    }
    sections = [None, {"v": 2}, "x", {"controls": {"a": {"target": {"kind": "node"}}}},
                {"controls": {"a": {"target": {"kind": "group", "group_id": 1.5}}}},
                {"controls": {"a": {"target": {"kind": "node"}, "advanced": None}}},
                {"controls": {"a": {"target": {"kind": "node"}, "order": 10000}}},
                {"controls": {f"s{i}": {"target": {"kind": "node"}} for i in range(65)}},
                {"v": None}, {"v": True}, {"v": 1.0}, {"v": "1"}, {"controls": []}, {"controls": 0}, {"controls": ""},
                {"controls": {"bad": {"target": {"kind": "planet"}}, "ok": {"target": {"kind": "node"}}}}]
    expected = [[sorted(cm.read_execution(s)[0]), cm.read_execution(s)[1] is not None] for s in sections]
    run(f'''
assert.deepEqual(B.readPresets({json.dumps(presets)}), {json.dumps(cm.read_presets(presets))});
const sections = {json.dumps(sections)};
const got = sections.map(s => {{ const r = B.readExecution(s); return [Object.keys(r.controls).sort(), r.error !== null]; }});
assert.deepEqual(got, {json.dumps(expected)});
''', tmp_path)


HOST = r'''
const {createCanvasControlsHost} = await import(HOST_URL);
const root = new Graph();
const editor = add(root,'SonderEditor',1,{rect:[900,900,10,10]});
const relay = add(root,'SonderControlRelay',2,{rect:[920,900,10,10],widgets:[widget('manifest','')]});
const a = add(root,'KSampler',3,{rect:[10,10,20,20],widgets:[widget('steps',20)],inputs:['steps']});
const b = add(root,'KSampler',4,{rect:[40,10,20,20],widgets:[widget('steps',8)],inputs:['steps'],mode:2});
group(root,7,[0,0,100,100],'First pass');
a.properties.sonderExecution = 'x';
a.properties.sonderBindings = {steps:'steps'};
b.properties.sonderBindings = {steps:'muted_steps'};
const manifestText = JSON.stringify({v:1, bindings:{steps:{label:'Steps'}, muted_steps:{label:'Pass 2 steps'}}, notes:{},
    execution:{v:1, controls:{x:{label:'',group:'',order:0,advanced:false,note:'',target:{kind:'node'}},
                              g:{label:'',group:'',order:1,advanced:false,note:'',target:{kind:'group',group_id:7}}}}});
relay.widgets[0].value = manifestText;
let calls = [], captures = 0, describedMembers = null;
const live = () => B.resolveExecution(root, B.parseManifest(relay.widgets[0].value)).controls;
const api = {addEventListener(){}, removeEventListener(){},
    async fetchApi(url, init){
        const body = JSON.parse(init.body); calls.push(body);
        const r = live();
        const execution = Object.entries(r).map(([id, s]) => ({execution_id:id, status:s.status, kind:s.kind,
            members: describedMembers?.[id] ?? s.members.map(m => ({path:m.path, mode:m.mode}))}));
        const answer = {relay:{status:'ok',key:'2',error:null}, bindings:[{binding_id:'steps',kind:'INT',status:'ok',value:'20',targets:[{path:'3',input:'steps'}]},
            {binding_id:'muted_steps',kind:'INT',status:'ok',value:'8',targets:[{path:'4',input:'steps'}],participation:'inactive'}],
            notes:[], execution, editors:['1']};
        if (body.changes) { answer.ok = true; answer.changes = globalThis.canon ? globalThis.canon(body.changes) : body.changes; answer.rejected = []; }
        return {ok:true, json: async () => answer};
    }};
const app = {rootGraph:root, graph:root, canvas:{},
    async graphToPrompt(){ const output = {}; for (const n of root._nodes) if (![2,4].includes(n.mode)) output[String(n.id)] = {class_type:n.type, inputs:Object.fromEntries(n.widgets.map(w=>[w.name,w.value]))}; return {output}; }};
const host = createCanvasControlsHost({app, api, getEditorNode:()=>editor, debounceMs:1,
    getChangeTracker:()=>({captureCanvasState(){captures++;}})});
'''


def run_host(scenario, tmp_path):
    script = HOST.replace("HOST_URL", json.dumps((ROOT / "web/js/controls_host_canvas.js").as_uri())) + scenario
    run(script, tmp_path, "host.mjs")


def test_writer_switches_one_node_and_sends_inactive_bodies(tmp_path):
    run_host(r'''
const result = await host.applyChanges({'execution:x:mode':'muted'});
assert.equal(result.ok, true, JSON.stringify(result));
assert.equal(a.mode, 2); assert.equal(captures, 2);
// The muted pass-two node's body travels beside the export, never in it.
assert.deepEqual(Object.keys(calls.at(-1).inactive), ['4']);
assert.equal(calls.at(-1).prompt['4'], undefined);
assert.equal(calls.at(-1).inactive['4'].inputs.steps, 8);
assert.equal((await host.applyChanges({'execution:x:mode':'enabled'})).ok, true);
assert.equal(a.mode, 0);
// Enabled leaves a node that already runs in its own enabled mode.
a.mode = 1;
assert.equal((await host.applyChanges({'execution:x:mode':'enabled'})).ok, true);
assert.equal(a.mode, 1);
''', tmp_path)


def test_writer_applies_a_group_and_a_preset_mixed_atomically(tmp_path):
    run_host(r'''
assert.equal((await host.applyChanges({'execution:g:mode':'bypassed'})).ok, true);
assert.deepEqual([a.mode, b.mode], [4, 4]);
// A preset's expansion: exact member modes, plus a value, in one history step.
captures = 0;
const ok = await host.applyChanges({'binding:steps:value':'31', 'execution:g:modes':{'3':'enabled','4':'muted'}});
assert.equal(ok.ok, true, JSON.stringify(ok));
assert.deepEqual([a.mode, b.mode, a.widgets[0].value], [0, 2, 31]); assert.equal(captures, 2);
// A node a group and its own switch set differently is refused before anything changes.
const clash = await host.applyChanges({'execution:x:mode':'muted', 'execution:g:mode':'enabled'});
assert.equal(clash.ok, false); assert.match(clash.rejected[0].reason, /differently/);
assert.deepEqual([a.mode, b.mode], [0, 2]);
// A failing widget callback restores every mode and value.
a.widgets[0].callback = () => { throw Error('callback failed'); };
const failed = await host.applyChanges({'binding:steps:value':'40', 'execution:g:mode':'bypassed'});
assert.equal(failed.ok, false);
assert.deepEqual([a.mode, b.mode, a.widgets[0].value], [0, 2, 31]);
''', tmp_path)


def test_writer_refuses_member_drift_and_manages_anchors(tmp_path):
    run_host(r'''
// The server validated other members than the live graph now has.
describedMembers = {g: [{path:'3', mode:'enabled'}]};
const drift = await host.applyChanges({'execution:g:mode':'muted'});
assert.equal(drift.ok, false); assert.match(drift.rejected[0].reason, /changed/);
assert.deepEqual([a.mode, b.mode], [0, 2]);
describedMembers = null;
// Expose: an anchor and a manifest entry in one step; the server saw the live candidates.
const c = add(root,'KSampler',5,{rect:[300,300,10,10],title:'Preview'});
globalThis.canon = changes => Object.fromEntries(Object.entries(changes).map(([t,v]) => [t, t.endsWith(':expose') ? {...v, label:'', order:4} : v]));
const added = await host.applyChanges({'execution:new:expose':{candidate:{kind:'node', path:'5'}}});
assert.equal(added.ok, true, JSON.stringify(added));
assert.ok(calls.at(-1).execution_candidates.some(x => x.path === '5'));
assert.equal(c.properties.sonderExecution, 'new');
assert.deepEqual(JSON.parse(relay.widgets[0].value).execution.controls.new, {label:'',group:'',order:4,advanced:false,note:'',target:{kind:'node'}});
globalThis.canon = changes => Object.fromEntries(Object.entries(changes).map(([t,v]) => [t, t.endsWith(':remove') ? {remove:v} : v]));
// The panel sends true; the server's cleaned form reaches the writer.
const removed = await host.applyChanges({'execution:new:remove':true});
assert.equal(removed.ok, true);
assert.equal(c.properties.sonderExecution, undefined);
assert.equal(JSON.parse(relay.widgets[0].value).execution.controls.new, undefined);
// Only the named candidate travels with the batch.
assert.deepEqual(calls.findLast(c => c.changes?.['execution:new:expose']).execution_candidates.map(x => x.path ?? x.group_id), ['5']);
''', tmp_path)


def test_relay_rows_summarize_switches_and_mark_inactive_settings(tmp_path):
    rows_url = json.dumps((ROOT / "web/js/control_relay_rows.js").as_uri())
    run(f'''
const R = await import({rows_url});
const root = new Graph();
const relayNode = add(root,'SonderControlRelay',2,{{rect:[900,900,10,10],widgets:[widget('manifest','')]}});
const a = add(root,'KSampler',3,{{rect:[10,10,20,20],widgets:[widget('steps',20,{{type:'number'}})],inputs:['steps'],mode:2}});
const b = add(root,'KSampler',4,{{rect:[40,10,20,20]}});
group(root,7,[0,0,100,100],'First pass');
a.properties.sonderBindings = {{steps:'steps'}};
relayNode.widgets[0].value = JSON.stringify({{v:1, bindings:{{steps:{{label:'Steps',order:0}}}}, notes:{{}},
    execution:{{v:1, controls:{{g:{{label:'Skip pass one',order:1,target:{{kind:'group',group_id:7}}}}}}}}}});
const plan = R.planRows(relayNode, root);
assert.deepEqual(plan.rows.map(r => r.label), ['Steps \u00b7 Muted', 'Skip pass one']);
const sw = plan.rows.find(r => r.switchId === 'g');
assert.equal(R.summaryText(root, sw), 'Mixed (1 of 2 off) \u2014 switch in Controls');
a.mode = 0;
// Each refresh tick re-plans, so the summary follows native mode edits.
assert.equal(R.summaryText(root, R.planRows(relayNode, root).rows.find(r => r.switchId === 'g')), 'Enabled \u2014 switch in Controls');
assert.equal(R.switchSummary({{status:'missing'}}).startsWith('Unavailable'), true);
''', tmp_path)


def test_writer_replaces_orphan_anchors_and_an_absent_section(tmp_path):
    run_host(r"""
// A deleted Relay's switch left its anchor behind: exposing the node replaces it.
const c = add(root,'KSampler',5,{rect:[300,300,10,10],title:'Preview'});
c.properties.sonderExecution = 'ghost';
globalThis.canon = changes => Object.fromEntries(Object.entries(changes).map(([t,v]) => [t, t.endsWith(':expose') ? {...v, label:'', order:4} : v]));
const added = await host.applyChanges({'execution:fresh:expose':{candidate:{kind:'node', path:'5'}}});
assert.equal(added.ok, true, JSON.stringify(added));
assert.equal(c.properties.sonderExecution, 'fresh');
// A live switch's anchor is still refused.
const taken = await host.applyChanges({'execution:other:expose':{candidate:{kind:'node', path:'3'}}});
assert.equal(taken.ok, false);
// A saved null section is replaced by the first switch.
const m = JSON.parse(relay.widgets[0].value); m.execution = null; delete a.properties.sonderExecution; delete c.properties.sonderExecution;
relay.widgets[0].value = JSON.stringify(m);
const first = await host.applyChanges({'execution:one:expose':{candidate:{kind:'node', path:'5'}}});
assert.equal(first.ok, true, JSON.stringify(first));
assert.deepEqual(Object.keys(JSON.parse(relay.widgets[0].value).execution.controls), ['one']);
""", tmp_path)


def test_group_edits_carry_switches_and_their_order_matches_the_server(tmp_path):
    from server import control_descriptors as cd
    manifest = {"v": 1, "groups": {}, "bindings": {"a": {"label": "Zeta", "order": 0, "group": "Settings"}}, "notes": {},
                "execution": {"v": 1, "controls": {"s": {"label": "Alpha", "order": 0, "group": "Switches", "target": {"kind": "node"}},
                                                   "g": {"label": "", "order": 0, "group": "Gate", "target": {"kind": "group", "group_id": 3}}}}}
    view = {"groups": {}, "bindings": [{"binding_id": "a", **manifest["bindings"]["a"]}], "notes": [],
            "execution": [{"execution_id": "s", **manifest["execution"]["controls"]["s"]},
                          {"execution_id": "g", **manifest["execution"]["controls"]["g"], "label": "Group"}]}
    run(f"""
const m = {json.dumps(manifest)};
assert.deepEqual(B.effectiveGroupOrder(m), {json.dumps(cd.group_order(view))});
B.applyGroupPlan(m, 'Switches', {{rename:'Passes'}});
assert.equal(m.execution.controls.s.group, 'Passes');
assert.deepEqual(Object.keys(m.groups), ['Passes','Gate','Settings']);
B.applyGroupPlan(m, 'Gate', {{ungroup:true}});
assert.equal(m.execution.controls.g.group, '');
assert.ok(!B.effectiveGroupOrder(m).includes('Gate'));
// A group holding only switches can move.
B.applyGroupPlan(m, 'Passes', {{move:true, before:null}});
assert.deepEqual(Object.keys(m.groups), ['Settings','Passes']);
// Another version's section is never rewritten.
const newer = {{v:1, bindings:{{}}, notes:{{}}, execution:{{v:9, controls:{{s:{{group:'Old'}}}}}}}};
B.applyGroupPlan(newer, 'Old', {{create:true}});
assert.equal(newer.execution.controls.s.group, 'Old');
""", tmp_path)


def test_exported_switches_stay_inside_the_member_budget(tmp_path):
    run(r"""
const root = new Graph();
for (let i=0;i<64;i++) add(root,'KSampler',i+10,{rect:[i,0,1,1],title:'A very long node title that goes on well past the export limit'});
const controls = {};
for (let g=1; g<=9; g++) { group(root,g,[-1,-1,100,100],`Pass ${g}`); controls[`g${g}`] = {label:'',target:{kind:'group',group_id:g}}; }
const out = JSON.parse(B.exportManifest(JSON.stringify({v:1,bindings:{},notes:{},execution:{v:1,controls}}), root)).execution.controls;
const sizes = Object.values(out).map(c => c.members.length);
assert.equal(sizes.reduce((x,y)=>x+y,0), B.MAX_EXPORTED_MEMBERS);
assert.equal(out.g9.status,'unsupported'); assert.equal(out.g9.problem,'budget'); assert.equal(out.g9.member_count, 64);
assert.ok(out.g1.members.every(m => m.title.length <= 48));
""", tmp_path)
