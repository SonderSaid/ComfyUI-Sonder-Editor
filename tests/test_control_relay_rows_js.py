"""Relay canvas rows (web/js/control_relay_rows.js) against fake graph objects.

Live rendering in both renderers is verified on the test install; this pins the
contract: frontend-only rows, fresh native lookups, no history capture, no
wrapped callbacks, and no rebuild while a row is edited.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
import assert from 'node:assert/strict';
const {syncRelayRows, rowWidgets, EMPTY_HINT, nativeFor} = await import(MODULE_URL);
class Graph {
    constructor(){this._nodes=[];this.links=new Map();this.subgraphs=new Map();this.inputs=[];}
    getNodeById(id){return this._nodes.find(n=>String(n.id)===String(id))??null;}
    getLink(id){return this.links.get(id)??null;}
}
const root=new Graph();
let callbacks=0;
const widget=(name,value,extra={})=>({name,value,type:'number',options:{},callback(v){callbacks++;this.seen=v;},...extra});
const node=(type,id,widgets=[],extra={})=>{const n={id,type,comfyClass:type,mode:0,graph:root,properties:{},inputs:[],widgets,size:[200,80],
    addCustomWidget(w){this.widgets.push(w);return w;},removeWidget(w){this.widgets.splice(this.widgets.indexOf(w),1);},
    setDirtyCanvas(){this.dirty=(this.dirty||0)+1;},computeSize(){return [200,30+this.widgets.length*20];},setSize(s){this.size=s;},...extra};
    root._nodes.push(n);return n;};
const mode=widget('control_after_generate','fixed',{type:'combo',options:{values:['fixed','increment','decrement','randomize']}});
const sampler=node('KSampler',3,[widget('steps',20,{options:{min:1,max:150,step2:1,precision:0}}),widget('seed',5,{linkedWidgets:[mode]}),
    widget('sampler_name','euler',{type:'combo',options:{values:['euler','dpm']}}),widget('note','hi',{type:'customtext',options:{multiline:true}})]);
sampler.properties.sonderBindings={steps:'steps',seed:'seed',sampler_name:'sampler',note:'text'};
const manifest=b=>JSON.stringify({v:1,bindings:b,notes:{},groups:{}});
const relay=node('SonderControlRelay',2,[{name:'manifest',type:'text',value:manifest({
    steps:{label:'Steps',order:1},seed:{label:'Seed',order:0},sampler:{label:'',order:2},text:{label:'Text',order:3,section:'editor'},gone:{label:'Gone'}})}]);
const originals=sampler.widgets.map(w=>w.callback);
let editing=false;
const sync=()=>syncRelayRows(relay,{root,canvas:{},isEditing:()=>editing});
const rows=()=>rowWidgets(relay);
const row=name=>rows().find(w=>w.name==='sonder_row:'+name);


if(SCENARIO==='restore'){
    sampler.properties.sonderBindings={};
    relay.widgets[0].value=manifest({steps:{targets:[{path:'3',input:'steps'}]}});
    let restored=0;relay.onRestoreControls=()=>restored++;
    sync();assert.equal(rows().length,1);assert.equal(rows()[0].type,'button');
    assert.equal(rows()[0].label,'Restore 1 Controls from this file');assert.equal(rows()[0].serialize,false);
    rows()[0].callback();assert.equal(restored,1);assert.deepEqual(relay.size,[200,80]);
    sampler.mode=2;sync();assert.equal(rows().length,1);assert.equal(rows()[0].type,'text');
    assert.match(rows()[0].value,/1 Controls aren't connected/);assert.deepEqual(relay.size,[200,80]);
}
if(SCENARIO==='rows'){
    assert.equal(sync(),true);
    // Ok bindings only, in manifest order; a seed gets its mode row. The stored
    // "editor" on a KSampler binding is ignored: section follows the node class.
    assert.deepEqual(rows().map(w=>[w.name,w.type,w.label]),[
        ['sonder_row:seed','number','Seed'],['sonder_row:seed:mode','combo','↳ mode'],['sonder_row:steps','number','Steps'],
        ['sonder_row:sampler','combo','KSampler · sampler_name'],['sonder_row:text','text','Text']]);
    // Frontend-only: neither widgets_values nor the prompt.
    for(const w of rows()){assert.equal(w.serialize,false);assert.equal(w.options.serialize,false);}
    assert.deepEqual([row('steps').options.min,row('steps').options.max,row('steps').options.precision],[1,150,0]);
    assert.equal(row('text').options.multiline,true);
    assert.equal(row('steps').value,20);assert.equal(row('seed:mode').value,'fixed');
    // Writes reach the native widget and its callback, never history.
    row('steps').callback(33);assert.equal(sampler.widgets[0].value,33);assert.equal(sampler.widgets[0].seen,33);
    row('seed:mode').callback('increment');assert.equal(mode.value,'increment');
    // Callbacks are never wrapped.
    assert.deepEqual(sampler.widgets.map(w=>w.callback),originals);
    // Combo options are read live from the native widget.
    sampler.widgets[2].options.values=['euler','dpm','lcm'];assert.deepEqual(row('sampler').options.values,['euler','dpm','lcm']);
    assert.deepEqual(row('seed:mode').options.values,['fixed','increment','decrement','randomize']);
    // A native edit elsewhere is followed on the next refresh, without a rebuild.
    const before=row('steps');sampler.widgets[0].value=12;assert.equal(sync(),true);assert.equal(row('steps'),before);assert.equal(before.value,12);
    assert.equal(sync(),false);
    // A replaced native widget object is found again by (node id, name).
    sampler.widgets[0]={...sampler.widgets[0],value:40};row('steps').callback(41);assert.equal(sampler.widgets[0].value,41);
}
if(SCENARIO==='editing'){
    sync();const first=row('steps');
    editing=true;
    relay.widgets[0].value=manifest({steps:{label:'Renamed'}});
    sampler.widgets[0].value=99;
    assert.equal(sync(),false);assert.equal(row('steps'),first);assert.equal(first.label,'Steps');assert.equal(first.value,20);
    editing=false;assert.equal(sync(),true);assert.equal(row('steps').label,'Renamed');assert.equal(rows().length,1);
    // Passive rows must not change persisted size on load or native Undo.
    assert.deepEqual(relay.size,[200,80]);
    relay.widgets[0].value=manifest({steps:{},seed:{},sampler:{},text:{}});
    syncRelayRows(relay,{root,resize:true});
    assert.deepEqual(relay.size,[280,150]);
}
if(SCENARIO==='hint-and-warnings'){
    relay.widgets[0].value=manifest({});sync();
    assert.deepEqual(rows().map(w=>[w.label,w.value,w.disabled]),[['Controls',EMPTY_HINT,true]]);
    relay.widgets[0].value=manifest({steps:{}});relay.mode=2;sync();
    assert.match(rows()[0].value,/muted or bypassed/);assert.equal(rows().length,1);
    relay.mode=0;node('SonderControlRelay',8,[{name:'manifest',value:''}]);sync();
    assert.match(rows()[0].value,/more than one/);
    root._nodes.pop();
    const inner=new Graph();inner._nodes.push({type:'SonderControlRelay'});
    root.subgraphs.set('nested',inner);sync();assert.match(rows()[0].value,/inside a subgraph/);
    root.subgraphs.clear();relay.widgets[0].value='{not json';sync();assert.match(rows()[0].value,/not valid JSON/);
}
if(SCENARIO==='status-changes'){
    sync();assert.ok(row('steps'));
    // A binding that goes off (linked here) loses its row; one that comes back regains it.
    sampler.inputs=[{name:'steps',link:7,widget:{name:'steps'}}];sync();assert.equal(row('steps'),undefined);
    sampler.inputs=[];sync();assert.ok(row('steps'));
    // A removed node's row writes nothing.
    const r=row('steps');root._nodes.splice(root._nodes.indexOf(sampler),1);r.callback(5);assert.equal(callbacks,0);
    assert.equal(nativeFor(root,{nodeId:3,widgetName:'steps',anchor:'steps'}),null);
}
if(SCENARIO==='groups'){
    // Editor first, then ungrouped, then each group under a separator in group
    // order; Advanced rows stay and carry a suffix (no Show advanced resize).
    const save=node('SonderSaveVideo',9,[widget('filename_prefix','take',{type:'text'}),widget('hint','x',{type:'text'})]);
    save.properties.sonderBindings={filename_prefix:'file',hint:'hint'};
    relay.widgets[0].value=JSON.stringify({v:1,notes:{},groups:{Late:{order:-5}},bindings:{
        steps:{label:'Steps',order:1,group:'Sampling',advanced:true},seed:{label:'Seed',order:0},
        sampler:{label:'Sampler',order:2,group:'Late'},text:{label:'Text',order:3,group:'Sampling'},
        file:{label:'File name',order:9},hint:{label:'Hint',order:10}}});
    assert.equal(sync(),true);
    assert.deepEqual(rows().map(w=>[w.name,w.label]),[
        ['sonder_row:file','File name'],['sonder_row:hint','Hint'],
        ['sonder_row:seed','Seed'],['sonder_row:seed:mode','↳ mode'],
        ['sonder_row:~sep:Late','Late'],['sonder_row:sampler','Sampler'],
        ['sonder_row:~sep:Sampling','Sampling'],['sonder_row:steps','Steps · Adv'],['sonder_row:text','Text']]);
    const sep=rows().find(w=>w.name==='sonder_row:~sep:Late');
    assert.equal(sep.disabled,true);assert.equal(sep.serialize,false);assert.match(String(sep.value),/─/);
    // A binding id "hint" no longer collides with the empty-state row.
    assert.equal(row('hint').label,'Hint');
    // Value-follow skips separators and still follows native edits.
    sampler.widgets[2].value='dpm';assert.equal(sync(),true);assert.equal(row('sampler').value,'dpm');assert.match(String(sep.value),/─/);
    // A passive refresh never resizes the saved node.
    const size=[...relay.size];relay.widgets[0].value=JSON.stringify({v:1,notes:{},groups:{},bindings:{seed:{label:'Seed'}}});
    sync();assert.deepEqual(relay.size,size);
}
if(SCENARIO==='pickers'){
    // Lanes and folders are picked on their node or in Controls: the Relay shows a
    // read-only summary, decided by each RESOLVED target's class (a promoted input's
    // anchor is its subgraph host; its target is the inner Driver Selector).
    const refSel=node('SonderReferenceSelector',7,[widget('reference_lanes','0, 2',{type:'text'})]);
    const drvSel=node('SonderDriverSelector',8,[widget('driver_lane_index',1)]);
    const bridge=node('SonderSaveBridge',9,[widget('target_folder','',{type:'combo',options:{values:['']}})]);
    const inner=new Graph();
    const innerDrv={id:5,type:'SonderDriverSelector',comfyClass:'SonderDriverSelector',mode:0,graph:inner,properties:{},
        inputs:[{name:'driver_lane_index',widget:{name:'driver_lane_index'},link:70}],widgets:[widget('driver_lane_index',2)]};
    inner._nodes.push(innerDrv);inner.links.set(70,{target_id:5,target_slot:0});inner.inputs=[{name:'lane',linkIds:[70]}];
    const host=node('MySubgraph',20,[widget('lane',3)],{subgraph:inner,inputs:[{name:'lane',widget:{name:'lane'},link:null}]});
    refSel.properties.sonderBindings={reference_lanes:'lanes'};drvSel.properties.sonderBindings={driver_lane_index:'lane'};
    bridge.properties.sonderBindings={target_folder:'folder'};host.properties.sonderBindings={lane:'promoted'};
    relay.widgets[0].value=manifest({lanes:{label:'Lanes',order:1},lane:{label:'Driver',order:2},folder:{label:'Folder',order:3},
        promoted:{label:'Promoted',order:4},steps:{label:'Steps',order:5}});
    sync();
    const suffix=' — edit on the node or in Controls';
    const shown=name=>[row(name).type,row(name).value,row(name).disabled];
    assert.deepEqual(shown('lanes'),['text','Lanes 1, 3'+suffix,true]);
    assert.deepEqual(shown('lane'),['text','Driver lane 2'+suffix,true]);
    assert.deepEqual(shown('folder'),['text','Root'+suffix,true]);
    assert.deepEqual(shown('promoted'),['text','Driver lane 4'+suffix,true],'the host value, summarised by the inner target');
    assert.equal(row('steps').type,'number');
    // The follow-native refresh recomputes the summary; nothing wraps a callback.
    const callbacksBefore=[refSel,drvSel,bridge,host].map(n=>n.widgets[0].callback);
    refSel.widgets[0].value='1';bridge.widgets[0].value='Takes/Day 1';
    sync();
    assert.equal(row('lanes').value,'Lane 2'+suffix);
    assert.equal(row('folder').value,'Takes/Day 1'+suffix);
    refSel.widgets[0].value='bad';sync();
    assert.equal(row('lanes').value,'No lanes'+suffix);
    assert.deepEqual([refSel,drvSel,bridge,host].map(n=>n.widgets[0].callback),callbacksBefore);
}
if(SCENARIO==='group-tolerance'){
    for(const [raw, expected] of [[' Look ','Look'],['bad:name',''],['',''],['constructor','']]){
        relay.widgets[0].value=JSON.stringify({bindings:{steps:{label:'Steps',group:raw}},groups:{}});
        sync();assert.equal(row('steps').value,20);
        assert.deepEqual(rows().filter(w=>w.name.startsWith('sonder_row:~sep:')).map(w=>w.label),expected?[expected]:[]);
    }
    for(const groups of [[],42,'bad',null]){
        relay.widgets[0].value=JSON.stringify({bindings:{steps:{label:'Steps'}},groups});
        sync();assert.equal(row('steps').value,20);
        const {exportManifest}=await import(BINDINGS_URL);
        const exported=JSON.parse(exportManifest(relay.widgets[0].value,root));
        assert.deepEqual(exported.bindings.steps.targets,[{path:'3',input:'steps'}]);
        assert.ok(!exported.error);
    }
}
console.log('ok');
'''

SCENARIOS = ['restore', 'group-tolerance', 'rows', 'editing', 'hint-and-warnings', 'status-changes', 'groups', 'pickers']


@pytest.mark.parametrize('scenario', SCENARIOS)
def test_relay_rows(scenario):
    node = shutil.which('node')
    if not node:
        pytest.skip('node required for the Relay rows harness')
    script = HARNESS.replace('MODULE_URL', json.dumps((ROOT/'web/js/control_relay_rows.js').as_uri()))
    script = script.replace('SCENARIO', json.dumps(scenario)).replace('BINDINGS_URL', json.dumps((ROOT/'web/js/control_bindings.js').as_uri()))
    result = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'


def test_rows_never_capture_history_or_wrap_callbacks():
    source = (ROOT/'web/js/control_relay_rows.js').read_text(encoding='utf-8')
    code = '\n'.join(line for line in source.splitlines() if not line.lstrip().startswith('//'))
    assert 'captureCanvasState' not in code and 'changeTracker' not in code
    assert '.callback =' not in code and 'callback = function' not in code
