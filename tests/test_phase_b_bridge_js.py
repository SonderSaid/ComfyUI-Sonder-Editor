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


def test_guide_driver_consumers_retry_click_projection_and_failure(tmp_path):
    stubs = {
        '/scripts/app.js': 'export const app={graph:{setDirtyCanvas(){}},registerExtension(){}};',
        './autogrow_passthrough.js': 'export function refreshAutogrowShape(){}',
        './widget_visibility.js': 'export function commitWidgetVisibility(){}; export function setWidgetHidden(){}',
        './project_source_resolver.js': '''export const resolveProjectSource=n=>({status:'resolved',editor:{_sonderController:n.controller}});
export const projectResolutionStatusText=()=>"unresolved";''',
    }
    source = (ROOT / 'web/js/bridge_nodes.js').read_text(encoding='utf-8')
    for index, (name, contents) in enumerate(stubs.items()):
        path = tmp_path / f'stub{index}.mjs'
        path.write_text(contents, encoding='utf-8')
        source = source.replace(json.dumps(name), json.dumps(path.as_uri()))
    source = source.replace('"./bridge_read_coordinator.js"', json.dumps((ROOT / 'web/js/bridge_read_coordinator.js').as_uri()))
    source += '\nexport {installGuidePanel,installDriverPanel,loadLinkedEditorGuides,loadLinkedEditorDrivers,ensureNodeState,scheduleBridgeGuideRefresh,scheduleBridgeDriverRefresh};\n'
    module = tmp_path / 'bridges.mjs'
    module.write_text(source, encoding='utf-8')
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
