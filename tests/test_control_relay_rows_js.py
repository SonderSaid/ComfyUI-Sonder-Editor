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
console.log('ok');
'''

SCENARIOS = ['rows', 'editing', 'hint-and-warnings', 'status-changes', 'groups']


@pytest.mark.parametrize('scenario', SCENARIOS)
def test_relay_rows(scenario):
    node = shutil.which('node')
    if not node:
        pytest.skip('node required for the Relay rows harness')
    script = HARNESS.replace('MODULE_URL', json.dumps((ROOT/'web/js/control_relay_rows.js').as_uri()))
    script = script.replace('SCENARIO', json.dumps(scenario))
    result = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'


def test_rows_never_capture_history_or_wrap_callbacks():
    source = (ROOT/'web/js/control_relay_rows.js').read_text(encoding='utf-8')
    code = '\n'.join(line for line in source.splitlines() if not line.lstrip().startswith('//'))
    assert 'captureCanvasState' not in code and 'changeTracker' not in code
    assert '.callback =' not in code and 'callback = function' not in code
