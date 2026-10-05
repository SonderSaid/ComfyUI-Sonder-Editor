"""Canvas adapter lifecycle/writes against graph objects; runtime has live gates."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
import assert from 'node:assert/strict';
const {createCanvasControlsHost} = await import(MODULE_URL);
const {seedModeWidget} = await import(WIDGET_URL);
const clone = x => JSON.parse(JSON.stringify(x));
const root = {_nodes: [], subgraphs: new Map(), dirty:0, setDirtyCanvas(){this.dirty++;}};
const graph = () => ({_nodes: [], links:new Map(), inputs:[]});
const widget = (name,value) => ({name,value, callbacks:0, callback(v){this.callbacks++;}});
const add = (g,type,id,widgets=[]) => {const n={id,type,comfyClass:type,widgets,graph:g,inputs:[]};g._nodes.push(n);return n;};
const editor = add(root,'SonderEditor',1);
const ctl = add(root,'SonderControl',2,[widget('control_id','steps'),widget('value_text','20'),widget('panel_note','')]);
const seed = add(root,'SonderControl',3,[widget('control_id','seed'),widget('seed',7)]);
seed.widgets[1].linkedWidgets=[widget('fixed','increment')];
assert.equal(seedModeWidget(seed.widgets[1]).name,'fixed');
const note = add(root,'SonderPanelNote',4,[widget('note_id','intro'),widget('text','# Intro')]);
const field = add(root,'SonderSaveVideo',5,[widget('mark_queue_complete',false)]);
let raw = {controls:[{key:'2',keys:['2'],control_id:'steps',kind:'INT',status:'ok',value:'20'},
    {key:'3',keys:['3'],control_id:'seed',kind:'SEED',status:'ok',seed:'7'}],
    notes:[{key:'4',keys:['4'],note_id:'intro',status:'ok',text:'# Intro'}],
    fields:[{key:'5',class_type:'SonderSaveVideo',field:'mark_queue_complete',editor_key:'1',status:'ok',kind:'BOOLEAN',value:false}],editors:['1']};
let calls=[], captures=0, exports=0, release=null, wait=false, reject=false;
const listeners = new Map();
const api = {addEventListener(t,cb){listeners.set(t,cb);},removeEventListener(t,cb){if(listeners.get(t)===cb)listeners.delete(t);},
    async fetchApi(url,init){
        assert.equal(url,'/sonder-editor/controls/describe');
        const body=JSON.parse(init.body);calls.push(body);
        const answer=clone(raw);
        if(wait)await new Promise(r=>release=r);
        if(body.changes){ answer.ok=!reject;answer.changes=reject?{}:body.changes;
            answer.rejected=reject?[{target:Object.keys(body.changes)[0],reason:'server refusal'}]:[]; }
        return {ok:true,json:async()=>answer};
    }};
const app = {rootGraph:root,graph:root,canvas:{},
    async graphToPrompt(){exports++;return {output:{'1':{class_type:'SonderEditor'},data:root._nodes.map(n=>n.widgets.map(w=>[w.name,w.value]))}};}};
let settings=[];
const host = createCanvasControlsHost({app,api,getEditorNode:()=>editor,debounceMs:1,
    getChangeTracker:()=>({captureCanvasState(){captures++;}}),setEditorSetting:(k,v)=>settings.push([k,v])});
const settle=()=>new Promise(r=>setTimeout(r,15));
const deferred=async()=>{for(let i=0;i<40&&!release;i++)await settle();assert.ok(release);};

if(SCENARIO==='layout'||SCENARIO==='layout-rollback') {
    ctl.widgets.push(widget('panel_order',0),widget('panel_group',''));
    note.widgets.push(widget('panel_order',0),widget('panel_group',''));
    if(SCENARIO==='layout-rollback')note.widgets.at(-1).callback=()=>{throw Error('callback failed');};
    const result=await host.applyChanges({'control:steps:order':-10,'control:steps:group':'Advanced','note:intro:order':5,'note:intro:group':'Guide'});
    assert.equal(result.ok,SCENARIO==='layout');
    assert.equal(ctl.widgets.at(-2).value,SCENARIO==='layout'?-10:0);
    assert.equal(note.widgets.at(-1).value,SCENARIO==='layout'?'Guide':'');
    if(result.ok)assert.equal(captures,2);
}
if(SCENARIO==='root') {
    const result=await host.applyChanges({'control:steps:value':'31','control:seed:seed':'9007199254740993',
        'control:seed:seed_mode':'decrement','control:steps:note':'**Hint**','note:intro:text':'# New',
        'field:SonderSaveVideo/mark_queue_complete':true});
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(ctl.widgets[1].value,'31');assert.equal(seed.widgets[1].value,'9007199254740993');
    assert.equal(seed.widgets[1].linkedWidgets[0].value,'decrement');
    assert.equal(ctl.widgets[2].value,'**Hint**');assert.equal(note.widgets[1].value,'# New');
    assert.equal(field.widgets[0].value,true);assert.equal(captures,2);assert.equal(root.dirty,1);
    assert.equal(ctl.widgets[1].callbacks,1);
    await host.refresh(true);assert.equal(host.getView().controls[1].seed_mode,'decrement');
    host.setEditorSetting('render.takePlacementMuted',true);assert.deepEqual(settings,[['render.takePlacementMuted',true]]);
}
if(SCENARIO==='shared') {
    root._nodes.splice(root._nodes.indexOf(ctl),1);const def=graph();ctl.graph=def;def._nodes.push(ctl);
    root.subgraphs.set('shared',def);const a=add(root,'subgraph',6),b=add(root,'subgraph',7);a.subgraph=b.subgraph=def;
    raw.controls[0].key='6:2';raw.controls[0].keys=['6:2','7:2'];
    assert.equal((await host.applyChanges({'control:steps:value':'33'})).ok,true);
    assert.equal(ctl.widgets[1].value,'33');assert.equal(ctl.widgets[1].callbacks,1);assert.equal(captures,2);
}
if(SCENARIO==='promoted') {
    root._nodes.splice(root._nodes.indexOf(field),1);const def=graph();field.graph=def;def._nodes.push(field);
    const outer=add(root,'subgraph',6,[widget('advance',false)]);outer.subgraph=def;
    outer.inputs=[{name:'advance',widget:{name:'advance'},link:null}];
    field.inputs=[{name:'mark_queue_complete',widget:{name:'mark_queue_complete'},link:10}];
    def.links.set(10,{origin_id:'-10',origin_slot:0});
    raw.fields[0].key='6:5';
    assert.equal((await host.applyChanges({'field:SonderSaveVideo/mark_queue_complete':true})).ok,true);
    assert.equal(outer.widgets[0].value,true);assert.equal(field.widgets[0].value,false);
    assert.equal(outer.widgets[0].callbacks,1);
}
if(SCENARIO==='promoted-seed') {
    root._nodes.splice(root._nodes.indexOf(seed),1);const def=graph();seed.graph=def;def._nodes.push(seed);
    const outer=add(root,'subgraph',6,[widget('seed',7)]);outer.subgraph=def;
    outer.inputs=[{name:'seed',widget:{name:'seed'},link:null}];
    seed.inputs=[{name:'seed',widget:{name:'seed'},link:10}];def.links.set(10,{origin_id:'-10',origin_slot:0});
    raw.controls[1].key='6:3';raw.controls[1].keys=['6:3'];
    const result=await host.applyChanges({'control:seed:seed':'17','control:seed:seed_mode':'decrement'});
    assert.equal(result.ok,true,JSON.stringify(result));assert.equal(outer.widgets[0].value,17);
    assert.equal(seed.widgets[1].linkedWidgets[0].value,'decrement');
    await host.refresh(true);assert.equal(host.getView().controls[1].seed_mode,'decrement');
}
if(SCENARIO==='hidden-hash') {
    assert.equal(calls.length,0);await host.refresh();assert.equal(exports,0);
    host.setVisible(true);await settle();assert.equal(calls.length,1);assert.equal(listeners.size,2);
    listeners.get('graphChanged')();await settle();assert.equal(calls.length,1);
    ctl.widgets[1].value='40';listeners.get('graphChanged')();await settle();assert.equal(calls.length,2);
    host.setMountedVisible(true);host.setVisible(false);assert.equal(listeners.size,2);
    host.setMountedVisible(false);assert.equal(listeners.size,0);await host.refresh();assert.equal(calls.length,2);
}
if(SCENARIO==='refusal') {
    reject=true;let result=await host.applyChanges({'control:steps:value':'999'});
    assert.equal(result.ok,false);assert.equal(result.rejected[0].reason,'server refusal');
    assert.equal(ctl.widgets[1].value,'20');assert.equal(captures,0);
    reject=false;raw.controls[0].keys=['missing'];result=await host.applyChanges({'control:steps:value':'30'});
    assert.equal(result.ok,false);assert.equal(captures,0);
}
if(SCENARIO==='stale-write') {
    wait=true;const task=host.applyChanges({'control:steps:value':'30'});await deferred();
    ctl.widgets[1].value='45';release();assert.equal((await task).ok,false);assert.equal(ctl.widgets[1].value,'45');assert.equal(captures,0);
}
if(SCENARIO==='stale-view') {
    wait=true;host.setVisible(true);await deferred();host.setVisible(false);release();await settle();
    assert.equal(host.getView().state,'idle');assert.equal(listeners.size,0);
}
if(SCENARIO==='destroy') {
    wait=true;const task=host.applyChanges({'control:steps:value':'30'});await deferred();host.destroy();release();
    assert.equal((await task).ok,false);assert.equal(ctl.widgets[1].value,'20');assert.equal(captures,0);
}
if(SCENARIO==='rollback') {
    note.widgets[1].callback=()=>{throw new Error('callback failed');};
    const result=await host.applyChanges({'control:steps:value':'30','note:intro:text':'new'});
    assert.equal(result.ok,false);assert.equal(ctl.widgets[1].value,'20');assert.equal(note.widgets[1].value,'# Intro');
}
if(SCENARIO==='serial') {
    const results=await Promise.all([host.applyChanges({'control:steps:value':'30'}),host.applyChanges({'control:steps:value':'40'})]);
    assert.ok(results.every(r=>r.ok));assert.equal(ctl.widgets[1].value,'40');assert.equal(captures,4);
}
if(SCENARIO==='history-failure') {
    const failing=createCanvasControlsHost({app,api,getEditorNode:()=>editor,getChangeTracker:()=>({
        captureCanvasState(){if(++captures===2)throw Error('history failed');}})});
    const result=await failing.applyChanges({'control:steps:value':'30'});
    assert.equal(result.ok,false);assert.equal(ctl.widgets[1].value,'20');failing.destroy();
}
if(SCENARIO==='precision-refusal') {
    Object.defineProperty(seed.widgets[1],'value',{get(){return this.raw??7;},set(v){this.raw=Number(v);}});
    const result=await host.applyChanges({'control:steps:value':'30','control:seed:seed':'9007199254740993'});
    assert.equal(result.ok,false);assert.equal(ctl.widgets[1].value,'20');assert.equal(seed.widgets[1].value,7);
}
host.destroy();assert.equal(listeners.size,0);
console.log('ok');
'''

@pytest.mark.parametrize('scenario', ['layout','layout-rollback','root','shared','promoted','promoted-seed','hidden-hash','refusal','stale-write','stale-view','destroy','rollback','serial','history-failure','precision-refusal'])
def test_canvas_adapter(scenario):
    node = shutil.which('node')
    if not node:
        pytest.skip('node required for the canvas adapter harness')
    script = HARNESS.replace('MODULE_URL', json.dumps((ROOT/'web/js/controls_host_canvas.js').as_uri()))
    script = script.replace('WIDGET_URL', json.dumps((ROOT/'web/js/control_widgets.js').as_uri()))
    script = script.replace('SCENARIO', json.dumps(scenario))
    result = subprocess.run([node,'--input-type=module','-e',script],capture_output=True,text=True,timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'
