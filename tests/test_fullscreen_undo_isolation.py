"""Fullscreen history ownership against ComfyUI's earlier deferred listener."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_node(body):
    if not shutil.which('node'):
        pytest.skip('node required')
    extension = (ROOT / 'web/js/extension.js').read_text(encoding='utf-8')
    guard = extension[extension.index('function shouldSuppressComfyGraphUndo()'):extension.index('function getNodeById(')]
    widget = (ROOT / 'web/js/editor_widget.js').read_text(encoding='utf-8')
    keyboard = widget[widget.index('    _setupKeyboardEvents() {'):widget.index('    _keyboardConsumerId(suffix) {')]
    # Execute the actual setup prefix, so omitting installation from setup fails.
    setup = extension.split('    setup() {', 1)[1].split('// Page-level notification', 1)[0]
    script = '''
import assert from 'node:assert/strict';
const listeners = new Map(), frames = [];
globalThis.window = {
 addEventListener(name,fn){const list=listeners.get(name)||[];list.push(fn);listeners.set(name,list);},
 removeEventListener(name,fn){listeners.set(name,(listeners.get(name)||[]).filter(x=>x!==fn));},
 localStorage:{getItem(){return null;}}
};
globalThis.Element=class {
 constructor(tag,kind=''){this.tagName=tag;this.kind=kind;this.isContentEditable=kind==='content';}
 closest(selector){return (this.kind==='prompt'&&selector.includes('prompt-box')) ||
  (this.kind==='inspect'&&selector.includes('inspect-overlay')) ? this : null;}
};
globalThis.document={activeElement:new Element('BODY'),addEventListener(){}};
const root={_nodes:[],subgraphs:new Map()};
const loads=[];
const app={graph:root,rootGraph:root,loadGraphData:async value=>{loads.push(value);return value;}};
const sonderKeyboardDebug=()=>{};
function controllerNode(open=true){return {type:'SonderEditor',_sonderController:{state:{isFullscreenOpen:open}}};}
class Tracker {
 constructor(){this.undoQueue=[{value:'before'}];this.redoQueue=[{value:'redo'}];this.activeState={value:'now'};this.captures=0;}
 async updateState(source,destination){const state=source.pop();if(state){destination.push(this.activeState);
  await app.loadGraphData(state);this.activeState=state;}}
 async undo(){return this.updateState(this.undoQueue,this.redoQueue);}
 async redo(){return this.updateState(this.redoQueue,this.undoQueue);}
 async undoRedo(e){if((e.ctrlKey||e.metaKey)&&!e.altKey){const key=e.key.toUpperCase();
  if(key==='Y'&&!e.shiftKey||key==='Z'&&e.shiftKey){await this.redo();return true;}
  if(key==='Z'&&!e.shiftKey){await this.undo();return true;}}}
 captureCanvasState(){this.captures++;}
}
const preexisting=new Tracker();
window.comfyAPI={changeTracker:{ChangeTracker:Tracker}};
''' + guard + '\nfunction setupGuard(){' + setup + '\n}\n' + '''
const {register:registerKeyboardConsumer,PRIORITY:KEY_PRIORITY,PRESERVE_DEFAULT} = await import(MODULE_URL);
class Editor {
''' + keyboard + '''
 constructor(){this.isFullscreen=true;this._editorFocused=false;this.undos=0;this.redos=0;this.empty=false;this._setupKeyboardEvents();}
 _undo(){if(!this.empty)this.undos++;} _redo(){if(!this.empty)this.redos++;}
 _keyboardConsumerId(){return 'test-editor';} _keyboardDebug(){} _keyboardDebugSnapshot(){return {};}
 _managementModalMounted(){return false;}
}
function event(tag='BODY',key='z',extra={},kind=''){
 const target=new Element(tag,kind);document.activeElement=target;
 return {target,key,ctrlKey:true,metaKey:false,shiftKey:false,...extra,
  stopImmediatePropagation(){this.stopped=true;},preventDefault(){this.defaultPrevented=true;}};
}
function dispatch(e){for(const fn of listeners.get('keydown')||[]){fn(e);if(e.stopped)break;}}
function state(tracker){return JSON.stringify([tracker.undoQueue,tracker.redoQueue,tracker.activeState]);}
''' + body
    script = script.replace('MODULE_URL', json.dumps((ROOT / 'web/js/keyboard_ownership.js').as_uri()))
    result = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr


def test_setup_guards_actual_exported_prototype_all_graphs_and_lifecycle():
    run_node('''
const originalLoad=app.loadGraphData, originalDispatch=Tracker.prototype.undoRedo;
setupGuard();const guarded=Tracker.prototype.undo;setupGuard();
assert.equal(Tracker.prototype.undo,guarded);assert.equal(Tracker.prototype.undoRedo,originalDispatch);
const child={_nodes:[],subgraphs:new Map()},nested={nodes:[],subgraphs:new Map()};
root.subgraphs.set('child',child);child.subgraphs.set('nested',nested);nested.subgraphs.set('cycle',root);
const a=controllerNode(),b=controllerNode();root._nodes.push(a);nested.nodes.push(b);
for(const tracker of [preexisting,new Tracker()]){
 const before=state(tracker);
 await tracker.undo();await tracker.redo();assert.equal(state(tracker),before);
 a._sonderController.state.isFullscreenOpen=false;
 await tracker.undo();await tracker.redo();assert.equal(state(tracker),before);
 b._sonderController.state.isFullscreenOpen=false;
 await tracker.undo();assert.notEqual(state(tracker),before);
 b._sonderController.state.isFullscreenOpen=true;
 const reopened=state(tracker);await tracker.redo();assert.equal(state(tracker),reopened);
 b._sonderController._destroyed=true;await tracker.redo();assert.equal(state(tracker),before);
 b._sonderController._destroyed=false;a._sonderController.state.isFullscreenOpen=true;
}
assert.equal(app.loadGraphData,originalLoad);
await app.loadGraphData({explicit:'source-workflow'});assert.equal(loads.at(-1).explicit,'source-workflow');
// Expiry must not depend on elapsed time.
const snapshot=state(preexisting);await new Promise(resolve=>setTimeout(resolve,1100));
await preexisting.undo();assert.equal(state(preexisting),snapshot);
''')


def test_unowned_methods_preserve_receiver_arguments_results_and_errors():
    run_node('''
const token={},failure=new Error('original error');
class CustomTracker {
 undo(...args){assert.equal(this,instance);assert.deepEqual(args,['argument']);return token;}
 redo(){throw failure;}
}
const instance=new CustomTracker();window.comfyAPI.changeTracker.ChangeTracker=CustomTracker;setupGuard();
assert.equal(instance.undo('argument'),token);assert.throws(()=>instance.redo(),e=>e===failure);
root._nodes.push(controllerNode());assert.equal(instance.undo('blocked'),undefined);assert.equal(instance.redo(),undefined);
''')


def test_earlier_comfy_callback_cannot_advance_graph_history_after_editor_dispatch():
    run_node('''
const owner=controllerNode();root._nodes.push(owner);setupGuard();
// ComfyUI captures first and schedules work without checking defaultPrevented.
window.addEventListener('keydown',e=>frames.push(async()=>{
 if(!await preexisting.undoRedo(e))preexisting.captureCanvasState();
}),true);
const editor=new Editor(),before=state(preexisting);
for(let i=0;i<100;i++){
 const e=event(i%2?'BUTTON':'BODY',i%2?'y':'z');dispatch(e);
 assert.equal(e.defaultPrevented,true);
 while(frames.length)await frames.shift()();
 assert.equal(state(preexisting),before);
}
assert.equal(editor.undos,50);assert.equal(editor.redos,50);
assert.equal(loads.length,0);assert.equal(preexisting.captures,0);
// Text and overlay owners may decline scene undo; graph history still cannot run.
for(const [tag,kind] of [['INPUT',''],['TEXTAREA',''],['DIV','content'],['DIV','prompt'],['BUTTON','inspect']]){
 dispatch(event(tag,'z',{},kind));while(frames.length)await frames.shift()();
 assert.equal(state(preexisting),before);
}
assert.equal(editor.undos,50);
// Invocation-time ownership: a deferred callback after close is intentionally allowed.
dispatch(event());owner._sonderController.state.isFullscreenOpen=false;editor.isFullscreen=false;
while(frames.length)await frames.shift()();assert.notEqual(state(preexisting),before);
''')


def test_actual_editor_routing_preserves_text_overlay_and_nonhistory_keys():
    run_node('''
const editor=new Editor();
for(const tag of ['BODY','CANVAS','BUTTON'])for(const meta of [false,true]){
 for(const [key,shift,expected] of [['z',false,'undos'],['y',false,'redos'],['z',true,'redos']]){
  const before=editor[expected],e=event(tag,key,{ctrlKey:!meta,metaKey:meta,shiftKey:shift});
  dispatch(e);assert.equal(editor[expected],before+1);assert.equal(e.defaultPrevented,true);
 }
}
editor.empty=true;const empty=event('BUTTON');dispatch(empty);assert.equal(empty.defaultPrevented,true);
for(const [tag,kind] of [['INPUT',''],['TEXTAREA',''],['SELECT',''],['DIV','content'],['BUTTON','prompt'],['BUTTON','inspect']]){
 const e=event(tag,'z',{},kind);dispatch(e);assert.equal(e.defaultPrevented,undefined);
}
for(const key of [' ','Delete','ArrowLeft','Escape','s']){
 const e=event('BUTTON',key,{ctrlKey:false});dispatch(e);assert.equal(e.defaultPrevented,undefined);
}
editor.empty=false;const before=editor.undos;
const off=registerKeyboardConsumer({id:'overlay',priority:KEY_PRIORITY.OVERLAY,keydown:()=>true});
dispatch(event('BUTTON'));assert.equal(editor.undos,before);off();
const textOff=registerKeyboardConsumer({id:'text',priority:KEY_PRIORITY.TEXT_EDITOR,keydown:()=>PRESERVE_DEFAULT});
const text=event('DIV','z',{},'prompt');dispatch(text);assert.equal(text.stopped,true);assert.equal(text.defaultPrevented,undefined);textOff();
editor.isFullscreen=false;const unfocused=event('BUTTON');dispatch(unfocused);assert.equal(unfocused.defaultPrevented,undefined);
editor._editorFocused=true;dispatch(event('BUTTON'));assert.equal(editor.undos,before+1);
''')
