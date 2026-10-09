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
const clone = x => JSON.parse(JSON.stringify(x));
class Graph {
    constructor(){this._nodes=[];this.links=new Map();this.subgraphs=new Map();this.inputs=[];this.dirty=0;}
    setDirtyCanvas(){this.dirty++;}
    add(n){n.graph=this;this._nodes.push(n);}
    remove(n){this._nodes.splice(this._nodes.indexOf(n),1);n.graph=null;}
    getLink(id){return this.links.get(id)??null;}
}
const root = new Graph();
const widget = (name,value,extra={}) => ({name,value,options:{},callbacks:0,callback(v){this.callbacks++;},...extra});
const add = (g,type,id,widgets=[],inputs=[]) => {const n={id,type,comfyClass:type,widgets,graph:g,mode:0,properties:{},
    inputs:inputs.map(name=>({name,link:null,widget:{name}}))};g._nodes.push(n);return n;};
const editor = add(root,'SonderEditor',1);
const manifestText = () => JSON.stringify({v:1, bindings:{steps:{label:'Steps',order:0,group:''},seed:{label:'Seed'},
    adv:{section:'editor'}}, notes:{intro:{text:'# Intro',order:0,group:''}}, future:{keep:true}});
const relay = add(root,'SonderControlRelay',2,[widget('manifest',manifestText())]);
const mode = widget('control_after_generate','increment');
const sampler = add(root,'KSampler',3,[widget('steps',20),widget('seed',7,{linkedWidgets:[mode]}),widget('cfg',7.5,{type:'number'})],['steps','seed']);
sampler.properties.sonderBindings={steps:'steps',seed:'seed'};
const save = add(root,'SonderSaveVideo',5,[widget('mark_queue_complete',false)],['mark_queue_complete']);
save.properties.sonderBindings={mark_queue_complete:'adv'};
const manifest = () => JSON.parse(relay.widgets[0].value);
let raw = {relay:{status:'ok',key:'2',error:null},
    bindings:[{binding_id:'steps',kind:'INT',status:'ok',value:'20',targets:[{path:'3',input:'steps'}]},
        {binding_id:'seed',kind:'SEED',status:'ok',value:'7',seed_mode:'increment',targets:[{path:'3',input:'seed'}]},
        {binding_id:'adv',kind:'BOOLEAN',status:'ok',value:false,section:'editor',targets:[{path:'5',input:'mark_queue_complete'}]}],
    notes:[{note_id:'intro',status:'ok',text:'# Intro'}],editors:['1']};
let calls=[], captures=0, exports=0, release=null, wait=false, reject=false;
const listeners = new Map();
const api = {addEventListener(t,cb){listeners.set(t,cb);},removeEventListener(t,cb){if(listeners.get(t)===cb)listeners.delete(t);},
    async fetchApi(url,init){
        assert.equal(url,'/sonder-editor/controls/describe');
        const body=JSON.parse(init.body);calls.push(body);
        const answer=clone(raw);
        if(wait)await new Promise(r=>release=r);
        if(body.changes){ answer.ok=!reject;answer.changes=reject?{}:(globalThis.canon?globalThis.canon(body.changes):body.changes);
            answer.rejected=reject?[{target:Object.keys(body.changes)[0],reason:'server refusal'}]:[]; }
        return {ok:true,json:async()=>answer};
    }};
const app = {rootGraph:root,graph:root,canvas:{},
    async graphToPrompt(){exports++;return {output:{'1':{class_type:'SonderEditor'},data:root._nodes.map(n=>n.widgets.map(w=>[w.name,w.value]))}};}};
let settings=[];
const host = createCanvasControlsHost({app,api,getEditorNode:()=>editor,debounceMs:1,
    getChangeTracker:()=>({captureCanvasState(){captures++;}})});
const settle=()=>new Promise(r=>setTimeout(r,15));
const deferred=async()=>{for(let i=0;i<40&&!release;i++)await settle();assert.ok(release);};

// The server's canonical structural values (control_descriptors._StructuralBatch).
const exposed = (candidate, extra={}) => ({candidate, targets:[{...candidate}], kind:'FLOAT', section:'workflow', label:'Sampler · cfg', order:4, ...extra});
const cfg = {path:'3', input:'cfg'};
if(SCENARIO==='expose'||SCENARIO==='expose-rollback'||SCENARIO==='expose-new-relay'||SCENARIO==='expose-new-relay-rollback') {
    let created=null;
    if(SCENARIO.startsWith('expose-new-relay')){
        root._nodes.splice(root._nodes.indexOf(relay),1);
        raw={...raw,relay:{status:'none',key:null,error:null},bindings:[],notes:[]};
        globalThis.LiteGraph={createNode(type){created={id:9,type,comfyClass:type,mode:0,properties:{},inputs:[],size:[200,40],
            addCustomWidget(w){this.widgets.push(w);return w;},removeWidget(w){this.widgets.splice(this.widgets.indexOf(w),1);},
            computeSize(){return [200,20*this.widgets.length];},setSize(s){this.size=s;},
            widgets:[widget('manifest','',SCENARIO.endsWith('rollback')?{callback(){throw Error('manifest failed');}}:{})]};return created;}};
    }
    if(SCENARIO==='expose-rollback')relay.widgets[0].callback=()=>{throw Error('manifest failed');};
    globalThis.canon=changes=>Object.fromEntries(Object.entries(changes).map(([t,v])=>
        [t, t.endsWith(':expose')?exposed(v.candidate,{section:v.section}):t.endsWith(':create')?{text:v.text,group:'',order:5}:v]));
    const before=JSON.stringify(sampler.properties);
    const result=await host.applyChanges({'binding:newCfg:expose':{candidate:cfg,section:'workflow'},'note:tip:create':{text:'Hi'}});
    const ok=!SCENARIO.endsWith('rollback');
    assert.equal(result.ok,ok,JSON.stringify(result));
    // The server saw exactly the live candidate the writer will anchor.
    assert.deepEqual(calls.at(-1).candidates,[{path:'3',input:'cfg',targets:[cfg],node_title:'KSampler'}]);
    if(ok){
        assert.equal(sampler.properties.sonderBindings.cfg,'newCfg');
        const m=JSON.parse((created||relay).widgets[0].value);
        // No section is stored: the server derives it from the node class.
        assert.deepEqual(m.bindings.newCfg,{label:'Sampler · cfg',group:'',order:4,advanced:false,note:''});
        assert.deepEqual(m.notes.tip,{text:'Hi',group:'',order:5});
        // Anchors, a new Relay and the manifest land in one history step.
        assert.equal(captures,2);
        if(created){assert.ok(root._nodes.includes(created));assert.deepEqual(created.pos,[40,0]);
            // Its rows, and the size they need, exist before the closing capture.
            assert.deepEqual(created.widgets.filter(w=>w.name.startsWith('sonder_row:')).map(w=>[w.name,w.value]),[['sonder_row:newCfg',7.5]]);
            assert.deepEqual(created.size,[280,40]);}
    } else {
        assert.equal(JSON.stringify(sampler.properties),before);
        if(created)assert.ok(!root._nodes.includes(created));
        else assert.equal(relay.widgets[0].value,manifestText());
    }
}
if(SCENARIO==='expose-noop'||SCENARIO==='expose-taken') {
    globalThis.canon=changes=>Object.fromEntries(Object.keys(changes).map(t=>[t,SCENARIO==='expose-noop'?{noop:true}:exposed(cfg)]));
    if(SCENARIO==='expose-taken')sampler.properties.sonderBindings.cfg='other';
    const result=await host.applyChanges({'binding:steps:expose':{candidate:cfg,section:'workflow'}});
    if(SCENARIO==='expose-noop'){assert.equal(result.ok,true);assert.equal(relay.widgets[0].value,manifestText());}
    else{assert.equal(result.ok,false);assert.match(result.rejected[0].reason,/already a Control/);assert.equal(sampler.properties.sonderBindings.cfg,'other');}
}
if(SCENARIO==='remove-rebind') {
    // A binding whose node was deleted, rebound to cfg, keeping its id; steps removed.
    relay.widgets[0].value=JSON.stringify({...JSON.parse(manifestText()),bindings:{...JSON.parse(manifestText()).bindings,lost:{label:'Lost'}}});
    globalThis.canon=changes=>Object.fromEntries(Object.entries(changes).map(([t,v])=>[t,t.endsWith(':rebind')?{candidate:v.candidate,targets:[v.candidate],kind:'FLOAT'}:v.remove?{remove:true}:v]));
    const result=await host.applyChanges({'binding:lost:rebind':{candidate:cfg},'binding:steps:remove':true,'note:intro:remove':true});
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.deepEqual(sampler.properties.sonderBindings,{seed:'seed',cfg:'lost'});
    const m=manifest();assert.ok(!m.bindings.steps&&m.bindings.lost&&!m.notes.intro);assert.equal(captures,2);
    // Rebinding a setting that is on is refused by the writer too.
    const again=await host.applyChanges({'binding:seed:rebind':{candidate:{path:'3',input:'steps'}}});
    assert.equal(again.ok,false);assert.match(again.rejected[0].reason,/off can be rebound/);
}
if(SCENARIO==='filter-during-write') {
    host.setVisible(true);await settle();host.setManage(true,'');await host.refresh(true);
    const hash=await host.getPromptHash();wait=true;
    const saving=host.applyChanges({'binding:steps:value':'31'},{expectedPromptHash:hash});
    await deferred();host.setManage(true,'cfg');
    wait=false;release();release=null;
    assert.equal((await saving).ok,true);assert.equal(sampler.widgets[0].value,31);
}
if(SCENARIO==='manage-listing') {
    host.setVisible(true);await host.refresh(true);
    assert.equal(calls.at(-1).manage,undefined);assert.equal(calls.at(-1).candidates,undefined);
    host.setManage(true,'');await host.refresh(true);
    const listed=calls.at(-1).candidates.map(c=>`${c.path}/${c.input}`);
    // Anchored, denied and Relay widgets are never offered.
    assert.deepEqual(listed,['3/cfg']);assert.equal(calls.at(-1).manage,true);assert.equal(calls.at(-1).candidates_truncated,false);
    host.setManage(true,'nothing');await host.refresh(true);assert.deepEqual(calls.at(-1).candidates,[]);
    host.setManage(false,'');await host.refresh(true);assert.equal(calls.at(-1).candidates,undefined);
    host.setMountedVisible(true);host.setMountedManage(true,'cfg');host.setVisible(false);await host.refresh(true);
    assert.deepEqual(calls.at(-1).candidates.map(c=>c.input),['cfg']);
}
if(SCENARIO==='presentation'||SCENARIO==='presentation-rollback') {
    if(SCENARIO==='presentation-rollback')relay.widgets[0].callback=()=>{throw Error('callback failed');};
    // Many presentation targets become ONE manifest write and one history step.
    const result=await host.applyChanges({'binding:steps:order':-10,'binding:steps:group':'Advanced',
        'binding:steps:label':'Quality','binding:seed:note':'**Hint**','note:intro:order':5,'note:intro:group':'Guide',
        'note:intro:text':'# New','binding:seed:advanced':true});
    assert.equal(result.ok,SCENARIO==='presentation',JSON.stringify(result));
    const m=manifest();
    if(result.ok){
        assert.deepEqual([m.bindings.steps.order,m.bindings.steps.group,m.bindings.steps.label],[-10,'Advanced','Quality']);
        assert.equal(m.bindings.seed.note,'**Hint**');assert.equal(m.bindings.seed.advanced,true);
        assert.deepEqual(m.notes.intro,{text:'# New',order:5,group:'Guide'});
        assert.deepEqual(m.future,{keep:true});
        assert.equal(relay.widgets[0].callbacks,1);assert.equal(captures,2);
    } else assert.equal(relay.widgets[0].value,manifestText());
}
if(SCENARIO==='root') {
    const result=await host.applyChanges({'binding:steps:value':'31','binding:seed:value':'9007199254740991',
        'binding:seed:seed_mode':'decrement','binding:adv:value':true,'binding:steps:label':'Steps!'});
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(sampler.widgets[0].value,31);assert.equal(sampler.widgets[1].value,9007199254740991);
    assert.equal(mode.value,'decrement');assert.equal(save.widgets[0].value,true);
    assert.equal(manifest().bindings.steps.label,'Steps!');
    assert.equal(captures,2);assert.equal(root.dirty,1);assert.equal(sampler.widgets[0].callbacks,1);
    assert.deepEqual(calls.at(-1).relay_census,{active:1,muted:0,bypassed:0,nested:0});
    await host.refresh(true);assert.equal(host.getView().bindings[1].seed_mode,'increment');
    assert.equal(host.setEditorSetting,undefined);
}
if(SCENARIO==='promoted'||SCENARIO==='promoted-seed') {
    // One promoted input feeding two inner consumers: one write to the host widget.
    const def=new Graph();
    const a=add(def,'KSampler',11,[widget('steps',4),widget('seed',1,{linkedWidgets:[widget('control_after_generate','fixed')]})],['steps','seed']);
    const b=add(def,'KSampler',12,[widget('steps',4)],['steps']);
    def.inputs=[{name:'steps',linkIds:[21,22]},{name:'seed',linkIds:[23]}];
    def.links.set(21,{origin_id:'-10',origin_slot:0,target_id:11,target_slot:0});
    def.links.set(22,{origin_id:'-10',origin_slot:0,target_id:12,target_slot:0});
    def.links.set(23,{origin_id:'-10',origin_slot:1,target_id:11,target_slot:1});
    root.subgraphs.set('def',def);
    const hostSteps=widget('steps',9), hostSeed=widget('seed',3);
    const host_=add(root,'subgraph',6,[hostSteps,hostSeed]);host_.subgraph=def;
    host_.inputs=[{name:'steps',link:null,widget:{name:'steps'}},{name:'seed',link:null,widget:{name:'seed'}}];
    host_.getWidgetFromSlot=input=>input.name==='steps'?hostSteps:hostSeed;
    sampler.properties.sonderBindings={};host_.properties.sonderBindings={steps:'steps',seed:'seed'};
    // The server validated the promoted consumers, so the view names them.
    raw.bindings[0].targets=[{path:'6:11',input:'steps'},{path:'6:12',input:'steps'}];
    raw.bindings[1].targets=[{path:'6:11',input:'seed'}];
    if(SCENARIO==='promoted'){
        const result=await host.applyChanges({'binding:steps:value':'33'});
        assert.equal(result.ok,true,JSON.stringify(result));
        assert.equal(hostSteps.value,33);assert.equal(hostSteps.callbacks,1);
        assert.equal(a.widgets[0].value,4);assert.equal(b.widgets[0].value,4);
    } else {
        // A promoted seed's value lives on the host; its mode on the definition.
        const result=await host.applyChanges({'binding:seed:value':'17','binding:seed:seed_mode':'randomize'});
        assert.equal(result.ok,true,JSON.stringify(result));
        assert.equal(hostSeed.value,17);assert.equal(a.widgets[1].linkedWidgets[0].value,'randomize');
    }
}
if(SCENARIO==='no-relay'||SCENARIO==='two-relays') {
    if(SCENARIO==='no-relay')relay.mode=2; else add(root,'SonderControlRelay',8,[widget('manifest','')]);
    const result=await host.applyChanges({'binding:steps:value':'31'});
    assert.equal(result.ok,false);assert.match(result.rejected[0].reason,/exactly one active Sonder Control Relay/);
    assert.equal(sampler.widgets[0].value,20);assert.equal(captures,0);
    if(SCENARIO==='no-relay')assert.deepEqual(calls.at(-1).relay_census,{active:0,muted:1,bypassed:0,nested:0});
}
if(SCENARIO==='linked-or-gone') {
    sampler.inputs[0].link=99;  // the input became linked after describe
    let result=await host.applyChanges({'binding:steps:value':'31'});
    assert.equal(result.ok,false);assert.equal(sampler.widgets[0].value,20);
    sampler.inputs[0].link=null;sampler.properties.sonderBindings={};  // anchor removed (convert/unpack)
    result=await host.applyChanges({'binding:seed:value':'8'});
    assert.equal(result.ok,false);assert.equal(captures,0);
}
if(SCENARIO==='unsafe-seed') {
    // A plain widget that does not coerce: the writer itself refuses, never rounds.
    const result=await host.applyChanges({'binding:seed:value':'9007199254740993'});
    assert.equal(result.ok,false);assert.match(result.rejected[0].reason,/too large for the canvas widget/);
    assert.equal(sampler.widgets[1].value,7);assert.equal(captures,0);
}
if(SCENARIO==='moved') {
    // The anchor moved to another node after the export the server validated.
    const other=add(root,'KSampler',9,[widget('steps',1)],['steps']);
    sampler.properties.sonderBindings={seed:'seed'};other.properties.sonderBindings={steps:'steps'};
    const result=await host.applyChanges({'binding:steps:value':'31'});
    assert.equal(result.ok,false);assert.match(result.rejected[0].reason,/moved/);
    assert.equal(other.widgets[0].value,1);assert.equal(sampler.widgets[0].value,20);
}
if(SCENARIO==='rollback-callbacks') {
    // Rollback replays callbacks so derived state follows the restored value.
    let derived='from-20';sampler.widgets[0].callback=function(v){derived='from-'+v;};
    save.widgets[0].callback=()=>{throw Error('callback failed');};
    const result=await host.applyChanges({'binding:steps:value':'31','binding:adv:value':true});
    assert.equal(result.ok,false);assert.equal(sampler.widgets[0].value,20);assert.equal(derived,'from-20');
}
if(SCENARIO==='manifest-cap') {
    const result=await host.applyChanges({'note:intro:text':'x'.repeat(4000),'binding:steps:note':'y'.repeat(4000),
        ...Object.fromEntries(Array.from({length:30},(_,i)=>[`binding:seed:label`,'z']))});
    assert.equal(result.ok,true);  // well under the cap
    relay.widgets[0].value=JSON.stringify({v:1,bindings:Object.fromEntries(Array.from({length:43},(_,i)=>[`b${i}`,{note:'n'.repeat(3000)}])),notes:{intro:{text:''}}});
    raw.bindings=[];
    const over=await host.applyChanges({'note:intro:text':'x'.repeat(4000)});
    assert.equal(over.ok,false);assert.match(over.rejected[0].reason,/128 KiB/);
}
if(SCENARIO==='hidden-hash') {
    assert.equal(calls.length,0);await host.refresh();assert.equal(exports,0);
    host.setVisible(true);await settle();assert.equal(calls.length,1);assert.equal(listeners.size,2);
    listeners.get('graphChanged')();await settle();assert.equal(calls.length,1);
    sampler.widgets[0].value=40;listeners.get('graphChanged')();await settle();assert.equal(calls.length,2);
    host.setMountedVisible(true);host.setVisible(false);assert.equal(listeners.size,2);
    host.setMountedVisible(false);assert.equal(listeners.size,0);await host.refresh();assert.equal(calls.length,2);
}
if(SCENARIO==='relay-error-view') {
    raw={relay:{status:'error',key:'2',error:'The workflow has more than one Sonder Control Relay. Keep one.'},bindings:[],notes:[],
        error:'The workflow has more than one Sonder Control Relay. Keep one.'};
    host.setVisible(true);await settle();
    assert.equal(host.getView().state,'ready');assert.match(host.getView().error,/more than one/);
}
if(SCENARIO==='refusal') {
    reject=true;let result=await host.applyChanges({'binding:steps:value':'999'});
    assert.equal(result.ok,false);assert.equal(result.rejected[0].reason,'server refusal');
    assert.equal(sampler.widgets[0].value,20);assert.equal(captures,0);
    reject=false;result=await host.applyChanges({'control:steps:value':'30'});
    assert.equal(result.ok,false);assert.equal(captures,0);
}
if(SCENARIO==='stale-write') {
    wait=true;const task=host.applyChanges({'binding:steps:value':'30'});await deferred();
    sampler.widgets[0].value=45;release();assert.equal((await task).ok,false);assert.equal(sampler.widgets[0].value,45);assert.equal(captures,0);
}
if(SCENARIO==='hide-during-write'||SCENARIO==='event-during-write') {
    host.setVisible(true);await settle();
    wait=true;const task=host.applyChanges({'binding:steps:value':'30'});await deferred();
    if(SCENARIO==='hide-during-write')host.setVisible(false);else listeners.get('graphChanged')();
    release();const result=await task;
    assert.equal(result.ok,true,JSON.stringify(result));assert.equal(sampler.widgets[0].value,30);assert.equal(captures,2);
}
if(SCENARIO==='stale-view') {
    wait=true;host.setVisible(true);await deferred();host.setVisible(false);release();await settle();
    assert.equal(host.getView().state,'idle');assert.equal(listeners.size,0);
}
if(SCENARIO==='destroy') {
    wait=true;const task=host.applyChanges({'binding:steps:value':'30'});await deferred();host.destroy();release();
    assert.equal((await task).ok,false);assert.equal(sampler.widgets[0].value,20);assert.equal(captures,0);
}
if(SCENARIO==='rollback') {
    relay.widgets[0].callback=()=>{throw new Error('callback failed');};
    const result=await host.applyChanges({'binding:steps:value':'30','note:intro:text':'new'});
    assert.equal(result.ok,false);assert.equal(sampler.widgets[0].value,20);assert.equal(relay.widgets[0].value,manifestText());
}
if(SCENARIO==='serial') {
    const results=await Promise.all([host.applyChanges({'binding:steps:value':'30'}),host.applyChanges({'binding:steps:value':'40'})]);
    assert.ok(results.every(r=>r.ok));assert.equal(sampler.widgets[0].value,40);assert.equal(captures,4);
}
if(SCENARIO==='history-failure') {
    const failing=createCanvasControlsHost({app,api,getEditorNode:()=>editor,getChangeTracker:()=>({
        captureCanvasState(){if(++captures===2)throw Error('history failed');}})});
    const result=await failing.applyChanges({'binding:steps:value':'30'});
    assert.equal(result.ok,false);assert.equal(sampler.widgets[0].value,20);failing.destroy();
}
if(SCENARIO==='precision-refusal') {
    Object.defineProperty(sampler.widgets[1],'value',{get(){return this.raw??7;},set(v){this.raw=Number(v);}});
    const result=await host.applyChanges({'binding:steps:value':'30','binding:seed:value':'9007199254740993'});
    assert.equal(result.ok,false);assert.equal(sampler.widgets[0].value,20);assert.equal(sampler.widgets[1].value,7);
}
host.destroy();assert.equal(listeners.size,0);
console.log('ok');
'''

SCENARIOS = ['expose', 'expose-rollback', 'expose-new-relay', 'expose-new-relay-rollback', 'expose-noop', 'expose-taken',
             'remove-rebind', 'manage-listing', 'filter-during-write', 'unsafe-seed', 'moved', 'rollback-callbacks', 'presentation', 'presentation-rollback', 'root', 'promoted', 'promoted-seed', 'no-relay', 'two-relays',
             'linked-or-gone', 'manifest-cap', 'hidden-hash', 'relay-error-view', 'refusal', 'stale-write', 'stale-view',
             'destroy', 'rollback', 'serial', 'history-failure', 'precision-refusal', 'hide-during-write',
             'event-during-write']


@pytest.mark.parametrize('scenario', SCENARIOS)
def test_canvas_adapter(scenario):
    node = shutil.which('node')
    if not node:
        pytest.skip('node required for the canvas adapter harness')
    script = HARNESS.replace('MODULE_URL', json.dumps((ROOT/'web/js/controls_host_canvas.js').as_uri()))
    script = script.replace('SCENARIO', json.dumps(scenario))
    result = subprocess.run([node,'--input-type=module','-e',script],capture_output=True,text=True,timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'
