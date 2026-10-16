"""Real bridge consumers share reads while preserving per-node panel state."""
import json
from pathlib import Path
import subprocess
from test_bridge_reference_coordinator_js import _run_node as _base_run_node


def _run_node(script):
    try:
        return _base_run_node(script)
    except subprocess.CalledProcessError as exc:
        raise AssertionError(exc.stderr) from None

ROOT = Path(__file__).resolve().parents[1]


def _stage_bridges(tmp_path, app_stub='export const app={graph:{setDirtyCanvas(){}},registerExtension(){}};'):
    stubs = {
        '/scripts/app.js': app_stub,
        './autogrow_passthrough.js': 'export function refreshAutogrowShape(){}',
        './widget_visibility.js': 'export function commitWidgetVisibility(){}; export function setWidgetHidden(){}',
        './project_source_resolver.js': '''export const resolveProjectSource=n=>(n.controller?{status:'resolved',editor:{_sonderController:n.controller}}:{status:'none'});
export const projectResolutionStatusText=()=>"unresolved";''',
    }
    source = (ROOT / 'web/js/bridge_nodes.js').read_text(encoding='utf-8')
    for index, (name, contents) in enumerate(stubs.items()):
        path = tmp_path / f'stub{index}.mjs'
        path.write_text(contents, encoding='utf-8')
        source = source.replace(json.dumps(name), json.dumps(path.as_uri()))
    for name in ('bridge_read_coordinator.js', 'api_client.js', 'editor_render_window_events.js',
                 'control_bindings.js', 'widget_value_observer.js'):
        source = source.replace(json.dumps(f'./{name}'), json.dumps((ROOT / 'web/js' / name).as_uri()))
    source += '\nexport {installGuidePanel,installDriverPanel,installNode,loadLinkedEditorGuides,loadLinkedEditorDrivers,ensureNodeState,scheduleBridgeGuideRefresh,scheduleBridgeDriverRefresh};\n'
    module = tmp_path / 'bridges.mjs'
    module.write_text(source, encoding='utf-8')
    return module


def test_guide_driver_consumers_retry_click_projection_and_failure(tmp_path):
    module = _stage_bridges(tmp_path)
    script = r'''
import assert from 'node:assert/strict';
class Element {
 constructor(tag){this.tagName=tag;this.style={};this.children=[];this.listeners={};this.textContent='';}
 append(...children){this.children.push(...children);}
 appendChild(child){this.children.push(child);}
 set innerHTML(value){this.children=[];}
 addEventListener(name,fn){this.listeners[name]=fn;}
}
globalThis.document={createElement:tag=>new Element(tag)};
globalThis.window={setTimeout,comfyAPI:{api:{api:{apiURL:v=>v}}}};
const calls=[], gates=[];
globalThis.fetch=(url,options)=>new Promise(resolve=>{calls.push({url,options});gates.push(resolve);});
const mod=await import(MODULE);
const tick=()=>new Promise(resolve=>setTimeout(resolve,5));
const raw={scene_name:'Scene',window_start:0,window_end:40,source:'live',
 guides:[{asset_id:'a',frame_index:5,name:'Guide',guide_key:'a:5',muted:false}],
 drivers:[],all_guide_keys:['a:5'],all_driver_keys:[]};
const respond=(i,ok=true)=>gates[i]({ok,status:ok?200:503,json:async()=>raw});
for(const kind of ['Guide','Driver']){
 const base=calls.length, callbacks=[];
 const controller={state:{projectDir:'',sceneId:'scene'},whenProjectReady:fn=>callbacks.push(fn)};
 const nodes=[1,2].map(id=>({id,controller,comfyClass:kind==='Guide'?'SonderGuidesBridgeStart':'SonderDriverSelector',
 widgets:[{name:'bridge_overrides_json',value:id===1?'{}':'{"a:5":{"muted":true}}'}],
 addDOMWidget(){return {};}}));
 for(const node of nodes)mod[`install${kind}Panel`](node);
 await tick();
 assert.equal(calls.length,base); assert.equal(callbacks.length,2);
 controller.state.projectDir='/projects/project';
 for(const callback of callbacks)callback();
 await tick();
 assert.equal(calls.length,base+1,'project-ready callbacks share the automatic wave');
 const state=mod.ensureNodeState(nodes[0]);
 const panel=state[kind.toLowerCase()+'Panel'];
 panel.children[0].children[1].listeners.click();
 await tick(); assert.equal(calls.length,base+1,'manual click queues behind active');
 respond(base); await tick();
 assert.equal(calls.length,base+2);
 const prefix=`X-Sonder-${kind}-`;
 assert.notEqual(calls[base].options.headers[prefix+'Generation'],calls[base+1].options.headers[prefix+'Generation']);
 assert.equal(calls[base+1].options.headers[prefix+'Origin'],'manual');
 respond(base+1); await tick();
 if(kind==='Guide'){
  assert.equal(mod.ensureNodeState(nodes[0]).guideList.children[0].children[2].textContent,'Inherit (on)');
  assert.equal(mod.ensureNodeState(nodes[1]).guideList.children[0].children[2].textContent,'Bridge mute');
  assert.equal(raw.guides[0].resolved_frame_index,undefined,'projection must not mutate shared raw rows');
 }
 mod[`scheduleBridge${kind}Refresh`]({targets:nodes}); await tick();
 respond(base+2,false); await tick();
 for(const node of nodes)assert.equal(mod.ensureNodeState(node)[kind.toLowerCase()+'Status'].textContent,`${kind} bridge fetch failed: 503`);
}
console.log(JSON.stringify({passed:true,calls:calls.length}));
'''.replace('MODULE', json.dumps(module.as_uri()))
    assert _run_node(script) == {'passed': True, 'calls': 6}


def test_all_three_families_share_scheduler_policy():
    module = (ROOT / 'web/js/bridge_read_coordinator.js').as_uri()
    script = r'''
import assert from 'node:assert/strict';
globalThis.window={setTimeout};
const mod=await import(MODULE);
const results=[];
for(const label of ['Reference','Guide','Driver']){
 const calls=[], gates=[], promises=[];
 const coordinator=mod.createBridgeReadCoordinator({label,request:meta=>new Promise(resolve=>{calls.push(meta);gates.push(resolve);})});
 const scheduler=mod.createBridgeRefreshScheduler({dispatch:(node,meta)=>promises.push(coordinator.request({url:node.url,...meta}))});
 const tick=()=>new Promise(resolve=>setTimeout(resolve,5));
 const a={url:'/same'},b={url:'/same'},c={url:'/other'};
 scheduler({targets:[a],origin:'lifecycle'});scheduler({targets:[b],origin:'project_ready'});scheduler({targets:[c]});
 await tick();assert.equal(calls.length,2);
 scheduler({targets:[a],force:true,origin:'manual'});await tick();assert.equal(calls.length,2);
 gates[0]({raw:true});gates[1]({other:true});await tick();assert.equal(calls.length,3);
 gates[2]({fresh:true});const payloads=await Promise.all(promises);
 assert.equal(payloads[0],payloads[1]);assert.equal(payloads[3].fresh,true);
 results.push(calls.map(c=>c.url));
}
assert.deepEqual(results[0],results[1]);assert.deepEqual(results[1],results[2]);
console.log(JSON.stringify(results));
'''.replace('MODULE', json.dumps(module))
    assert _run_node(script) == [['/same', '/other', '/same']] * 3


def test_guides_and_drivers_follow_versions_scenes_and_lane_writes_without_pruning(tmp_path):
    """Phase 1 of plans/bridge-selections-controls.md through the real extension.

    Guides Start re-reads only when certificates cannot vouch for bridge-guides;
    Driver Selector re-reads on every version of its project; both re-read on a
    scene switch of their editor and never for another project. A Driver lane
    write re-renders from the held payload. Automatic refreshes never prune
    overrides (the panel counts the stale ones); an explicit Refresh does, and
    keeps the legacy bare Driver key the bridge still honours. Nodes inside a
    subgraph definition are refreshed too.
    """
    module = _stage_bridges(
        tmp_path, 'export const app={graph:null,registerExtension(e){globalThis.__ext=e;}};')
    client = (ROOT / 'web/js/api_client.js').as_uri()
    events = (ROOT / 'web/js/editor_render_window_events.js').as_uri()
    script = r'''
import assert from 'node:assert/strict';
class Element {
 constructor(tag){this.tagName=String(tag).toUpperCase();this.style={};this.children=[];this.handlers={};this.textContent='';this.title='';this.value='';}
 append(...children){this.children.push(...children);}
 appendChild(child){this.children.push(child);return child;}
 set innerHTML(value){this.children=[];}
 addEventListener(name,fn){(this.handlers[name] ||= []).push(fn);}
 fire(name){for(const fn of this.handlers[name]||[])fn({stopPropagation(){}});}
}
globalThis.document={createElement:tag=>new Element(tag)};
const win=new EventTarget();
win.setTimeout=setTimeout; win.comfyAPI={api:{api:{apiURL:v=>v}}};
globalThis.window=win;
const V=n=>`2026-10-16T10:00:${String(n).padStart(2,'0')}.000000`;
let served=V(1);
const fetches=[];
const guideKeys=['a:5'], driverKeys=['lane:0','lane:1'];
let source='live', holdGuides=false;
const held=[];
globalThis.fetch=async(url)=>{
 fetches.push(String(url));
 const version=served;
 const guides=String(url).includes('bridge-guides');
 if(guides&&holdGuides)await new Promise(resolve=>held.push(resolve));
 return {ok:true,status:200,headers:{get:n=>n==='X-Sonder-Project-Modified-At'?version:null},
  json:async()=>guides
   ?{scene_name:'S',window_start:0,window_end:40,source,
     guides:[{asset_id:'a',frame_index:5,asset_name:'Guide',guide_key:'a:5',editor_muted:false}],all_guide_keys:guideKeys}
   :{scene_name:'S',window_start:0,window_end:40,source,
     drivers:[{lane_key:'lane:0',lane_index:0,lane_name:'Zero',has_clip:false},{lane_key:'lane:1',lane_index:1,lane_name:'One',has_clip:false}],
     all_driver_keys:driverKeys}};
};
const {app}=await import(APP);
const clientMod=await import(CLIENT);
const eventsMod=await import(EVENTS);
const mod=await import(MODULE);
const ext=globalThis.__ext;
const controller={state:{projectDir:'C:/projects/Proj',sceneId:'scene-1'},whenProjectReady(){}};
const widget=(name,value)=>({name,value,callback(){}});
const start={id:1,comfyClass:'SonderGuidesBridgeStart',controller,
 widgets:[widget('iteration_index',0),widget('bridge_overrides_json','{"a:5":{"muted":true},"gone:9":{"muted":true}}')],
 addDOMWidget(){return {};}};
const driver={id:2,comfyClass:'SonderDriverSelector',controller,
 widgets:[widget('driver_lane_index',0),widget('driver_selector_overrides_json','{"drivers":{"lane:0":{"muted":true},"1":{"muted":false},"lane:7":{"muted":true}}}')],
 addDOMWidget(){return {};}};
const elsewhere={id:3,comfyClass:'SonderDriverSelector',controller:{state:{projectDir:'C:/projects/Other',sceneId:'x'},whenProjectReady(){}},
 widgets:[widget('driver_lane_index',0),widget('driver_selector_overrides_json','{}')],addDOMWidget(){return {};}};
const sub={_nodes:[driver],setDirtyCanvas(){}};
const root={_nodes:[start,elsewhere],subgraphs:new Map([['sub',sub]]),setDirtyCanvas(){}};
app.graph=root;
for(const node of [start,driver,elsewhere]){node.graph=node===driver?sub:root;mod.installNode(node);}
ext.setup();
const wait=ms=>new Promise(r=>setTimeout(r,ms));
await wait(80);
const count=()=>({guides:fetches.filter(u=>u.includes('bridge-guides')).length,
 drivers:fetches.filter(u=>u.includes('bridge-drivers')&&u.includes('/Proj/')).length,
 other:fetches.filter(u=>u.includes('/Other/')).length});
const log={installed:count()};
clientMod.rememberProjectVersion('Proj',V(1));
const certify=(b,n,over={})=>clientMod.registerChangeCertificate({schema:1,project_id:'canon',scene_id:'scene-1',
 base_modified_at:V(b),modified_at:V(n),prompt:false,bridge:false,...over},{projectId:'Proj'});
const notice=async(id,v)=>{served=v;clientMod.rememberProjectVersion(id,v);await wait(320);};
await wait(320); log.afterV1=count();
certify(1,2); await notice('Proj',V(2)); log.certified=count();
certify(2,3,{bridge:true}); await notice('Proj',V(3)); log.flagged=count();
await notice('Proj',V(4)); log.uncertified=count();
await notice('Elsewhere',V(9)); log.otherProject=count();
served=V(5); clientMod.rememberProjectVersion('canon',V(5)); clientMod.rememberProjectVersion('Proj',V(5)); await wait(320);
log.aliasPair=count();
eventsMod.emitEditorRenderWindowChanged({field:'selection_start',projectDir:'C:/projects/Proj'}); await wait(320);
log.selection=count();
eventsMod.emitEditorRenderWindowChanged({field:'scene_id',projectDir:'C:/projects/Elsewhere'}); await wait(320);
log.otherScene=count();
controller.state.sceneId='scene-2';
eventsMod.emitEditorRenderWindowChanged({field:'scene_id',projectDir:'C:/projects/Proj'}); await wait(320);
log.sceneSwitch=count();

// Automatic refreshes kept every override and count the stale ones.
const gState=mod.ensureNodeState(start), dState=mod.ensureNodeState(driver);
const text=list=>list.children.map(c=>c.textContent);
log.guideOverridesAuto=JSON.parse(start.widgets[1].value);
log.guideNoticeAuto=text(gState.guideList).at(-1);
log.driverOverridesAuto=JSON.parse(driver.widgets[1].value);
log.driverNoticeAuto=text(dState.driverList).at(-1);

// A lane write from elsewhere (Controls, Relay): re-render from the held payload, no read.
const lane=driver.widgets[0];
lane.value=1; lane.callback(1,null,driver); await wait(320);
log.laneWrite=count();
log.laneSelect=dState.driverSelect.value;
log.laneRow=dState.driverList.children[0].children[1].textContent;
// The graph dropdown writes the widget only; its observer renders.
dState.driverSelect.value='0'; dState.driverSelect.fire('change'); await wait(320);
log.selectWrite={counts:count(),lane:lane.value,row:dState.driverList.children[0].children[1].textContent};
// A held payload for another scene cannot answer: the write reads.
controller.state.sceneId='scene-3';
lane.value=1; lane.callback(1); await wait(320);
log.laneOtherScene=count();

// Explicit Refresh prunes, keeping the legacy bare key whose lane exists.
gState.guidePanel.children[0].children[1].fire('click');
dState.driverPanel.children[0].children[1].fire('click');
await wait(80);
log.guideOverridesManual=JSON.parse(start.widgets[1].value);
log.guideNoticeManual=text(gState.guideList).some(t=>t.includes('stale'));
log.driverOverridesManual=JSON.parse(driver.widgets[1].value);
log.driverNoticeManual=text(dState.driverList).some(t=>t.includes('stale'));

// A repeated scene event for the scene a node already holds reads nothing.
await wait(320);
const beforeRepeat=count();
eventsMod.emitEditorRenderWindowChanged({field:'scene_id',projectDir:'C:/projects/Proj'}); await wait(320);
log.repeatScene=JSON.stringify(count())===JSON.stringify(beforeRepeat);

// A kept legacy bare key shows as the Driver Bridge resolves it, and cycling clears it.
const button=()=>dState.driverList.children[0].children[2];
log.legacyShown=button().textContent;
button().fire('click');
log.legacyCleared=JSON.parse(driver.widgets[1].value);

// A running job's snapshot key set never marks overrides stale or lets Refresh prune them.
start.widgets[1].value='{"a:5":{"muted":true},"gone:9":{"muted":true}}';
source='snapshot';
controller.state.sceneId='scene-4';
eventsMod.emitEditorRenderWindowChanged({field:'scene_id',projectDir:'C:/projects/Proj'}); await wait(320);
log.snapshotNotice=text(gState.guideList).some(t=>t.includes('stale'));
gState.guidePanel.children[0].children[1].fire('click'); await wait(80);
log.snapshotPrune=JSON.parse(start.widgets[1].value);
source='live';

// A manual Refresh superseded by an automatic dispatch still prunes.
holdGuides=true;
gState.guidePanel.children[0].children[1].fire('click'); await wait(40);
controller.state.sceneId='scene-5';
eventsMod.emitEditorRenderWindowChanged({field:'scene_id',projectDir:'C:/projects/Proj'}); await wait(320);
holdGuides=false;
while(held.length){held.shift()(); await wait(40);}
await wait(120);
log.racePrune=JSON.parse(start.widgets[1].value);
console.log(JSON.stringify(log));
'''
    for name, value in (('MODULE', module.as_uri()), ('CLIENT', client), ('EVENTS', events),
                        ('APP', (tmp_path / 'stub0.mjs').as_uri())):
        script = script.replace(name, json.dumps(value))
    result = _run_node(script)
    base = result['installed']
    assert base['guides'] >= 1 and base['drivers'] >= 1 and base['other'] >= 1

    def at(guides, drivers):
        return {'guides': base['guides'] + guides, 'drivers': base['drivers'] + drivers, 'other': base['other']}

    assert result['afterV1'] == at(0, 1), 'Driver is ungated; Guides held no versioned read yet'
    assert result['certified'] == at(0, 2), 'certified bridge-unchanged: Guides keep'
    assert result['flagged'] == at(1, 3)
    assert result['uncertified'] == at(2, 4)
    assert result['otherProject'] == result['uncertified'], 'another project never re-reads'
    assert result['aliasPair'] == at(3, 5), 'alias notices join one wave'
    assert result['selection'] == result['aliasPair'], 'window fields do not touch full-scene reads'
    assert result['otherScene'] == result['aliasPair']
    assert result['sceneSwitch'] == at(4, 6)
    assert result['guideOverridesAuto'] == {'a:5': {'muted': True}, 'gone:9': {'muted': True}}
    assert result['guideNoticeAuto'] == '1 stale override — Refresh to clear'
    assert result['driverOverridesAuto'] == {'drivers': {
        'lane:0': {'muted': True}, '1': {'muted': False}, 'lane:7': {'muted': True}}}
    assert result['driverNoticeAuto'] == '1 stale override — Refresh to clear'
    assert result['laneWrite'] == result['sceneSwitch'], 'a lane write re-renders without a read'
    assert result['laneSelect'] == '1'
    assert result['laneRow'].startswith('One')
    assert result['selectWrite'] == {'counts': result['sceneSwitch'], 'lane': 0, 'row': 'Zero - No clip'}
    assert result['laneOtherScene']['drivers'] == result['sceneSwitch']['drivers'] + 1
    assert result['guideOverridesManual'] == {'a:5': {'muted': True}}
    assert result['guideNoticeManual'] is False
    assert result['driverOverridesManual'] == {'drivers': {'lane:0': {'muted': True}, '1': {'muted': False}}}
    assert result['driverNoticeManual'] is False
    assert result['repeatScene'] is True
    assert result['legacyShown'] == 'Bridge on', 'bare "1" is in force for lane 1'
    assert result['legacyCleared'] == {'drivers': {'lane:0': {'muted': True}}}
    assert result['snapshotNotice'] is False
    assert result['snapshotPrune'] == {'a:5': {'muted': True}, 'gone:9': {'muted': True}}
    assert result['racePrune'] == {'a:5': {'muted': True}}
