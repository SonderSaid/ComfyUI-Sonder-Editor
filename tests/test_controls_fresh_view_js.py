"""freshView on the three Controls adapters and the relay's stale flag
(plans/controls-modes-phase-b.md §5). A view is fresh only when a read started
after the call adopted it; a read already running never counts; the call is
bounded and never rejects."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_controls_host_canvas_js import HARNESS

ROOT = Path(__file__).resolve().parents[1]


def run(script, tmp_path, name, urls):
    node = shutil.which('node')
    if not node:
        pytest.skip('node required for Controls adapter tests')
    for placeholder, path in urls.items():
        script = script.replace(placeholder, json.dumps((ROOT / 'web/js' / path).as_uri()))
    script = script.replace('SCENARIO', json.dumps(name))
    path = tmp_path / f'{name}.mjs'
    path.write_text(script, encoding='utf-8')
    result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith('ok'), result.stdout


CANVAS = r'''
if(SCENARIO==='fresh-view'){
    host.setVisible(true);await settle();await settle();
    // Nothing running: one describe, adopted, fresh.
    let before=calls.length;let read=await host.freshView();
    assert.equal(read.fresh,true);assert.equal(calls.length,before+1);assert.equal(read.view,host.getView());
    // A read already running when asked is awaited but never counts.
    raw={...raw,mark:'old'};wait=true;const running=host.refresh(true);await deferred();const first=release;release=null;
    raw={...raw,mark:'new'};before=calls.length;const asked=host.freshView();first();await deferred();
    wait=false;release();release=null;read=await asked;await running;
    assert.equal(read.fresh,true);assert.equal(read.view.mark,'new');assert.equal(calls.length,before+1,'its own read, after the running one');
    // The graph moves mid-describe: the unadopted read retries.
    wait=true;before=calls.length;const moved=host.freshView();await deferred();
    listeners.get('graphChanged')();wait=false;release();release=null;read=await moved;
    assert.equal(read.fresh,true);assert.ok(calls.length>=before+2);
    // A read that hangs: not fresh at the bound, and nothing rejects.
    wait=true;const started=Date.now();read=await host.freshView({timeoutMs:120});
    assert.equal(read.fresh,false);assert.ok(Date.now()-started<1000);assert.equal(read.view,host.getView());
    await deferred();wait=false;release();release=null;await settle();
}
'''


def test_canvas_fresh_view(tmp_path):
    ending = "host.destroy();assert.equal(listeners.size,0);\nconsole.log('ok');"
    assert ending in HARNESS
    script = HARNESS.replace(ending, CANVAS + ending)
    run(script, tmp_path, 'fresh-view', {'MODULE_URL': 'controls_host_canvas.js'})


REMOTE = r'''
import assert from 'node:assert/strict';
const {createMountedControlsHost,createSdkControlsHost}=await import(REMOTE_URL);
const settle=()=>new Promise(r=>setTimeout(r,5));
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
const view=(mark,extra={})=>({bindings:[],notes:[],mark,...extra});
for(const kind of ['mounted','sdk']){
    // One fake server: each read answers what `next` says when it is made.
    let next=()=>({ok:true,body:{}}),reads=0;
    const fetchImpl=async(url,init={})=>{
        if((init.method||'GET')!=='GET')return {ok:true,json:async()=>({ok:true})};
        reads++;const answer=await next();
        if(answer.throws)throw Error('offline');
        return {ok:answer.ok!==false,status:answer.ok===false?500:200,json:async()=>answer.body};
    };
    let seq=0;
    const body=(mark,extra)=>kind==='mounted'?{view:view(mark,extra),view_seq:++seq,applied:[],rejected:[]}:{ok:true,view:view(mark,extra),seq:++seq,template_id:'T'};
    const host=kind==='mounted'
        ?createMountedControlsHost({projectId:'P',hostId:'h',sessionId:'tab',apiURL:p=>p,fetchImpl})
        :createSdkControlsHost({_sessionPath:()=>'/s/controls',_fetch:fetchImpl,headers:()=>({}),_noteRefusal(){},templateId:'T',canWrite:()=>true});
    // Not visible: a click still reads.
    next=()=>({body:body('first')});let read=await host.freshView();
    assert.equal(read.fresh,true,kind);assert.equal(read.view.mark,'first');assert.equal(reads,1);
    // A read running when asked never counts; one started after it does.
    const gate=deferred();next=async()=>{await gate.promise;return {body:body('old')};};
    const running=host.refresh(true);await settle();next=()=>({body:body('new')});
    const asked=host.freshView();gate.resolve();read=await asked;await running;
    assert.equal(read.fresh,true,kind);assert.equal(read.view.mark,'new',kind);
    // An error read, a stale publication, a hanging read: not fresh, never a rejection.
    next=()=>({ok:false,body:{error:'x'}});read=await host.freshView();assert.equal(read.fresh,false,kind);
    next=()=>({throws:true});read=await host.freshView();assert.equal(read.fresh,false,kind);
    if(kind==='mounted'){reads=0;next=()=>({body:body('stale',{view_stale:true})});read=await host.freshView();
        assert.equal(read.fresh,false);assert.equal(reads,3,'three tries');}
    const hang=deferred();next=async()=>{await hang.promise;return {body:body('late')};};
    const t=Date.now();read=await host.freshView({timeoutMs:80});assert.equal(read.fresh,false,kind);assert.ok(Date.now()-t<1000);
    hang.resolve();await settle();
    host.destroy();
}
console.log('ok');
'''


def test_mounted_and_app_fresh_view(tmp_path):
    run(REMOTE, tmp_path, 'fresh-view-remote', {'REMOTE_URL': 'controls_host_remote.js'})


RELAY = r'''
import assert from 'node:assert/strict';
const {createCanvasControlsRelay}=await import(RELAY_URL);
for(const fresh of [false,true]){
    let current={state:'ready',bindings:[],notes:[],mark:'before'},hash='before',published=[];
    let queue={ok:true,pending:[{request_id:'r',prompt_hash:'before',changes:{'binding:n:value':'31'}}],controls_visible:true,controls_view_seq:1};
    const callbacks=new Set();
    const host={getView:()=>current,setMountedVisible(){},setMountedManage(){},refresh:async()=>current,getPromptHash:async()=>hash,
        freshView:async()=>({view:current,fresh}),subscribe(cb){callbacks.add(cb);return()=>callbacks.delete(cb);},
        async applyChanges(changes,options){assert.equal(await options.canApply(),true);hash='after';return {ok:true,rejected:[]};}};
    const relay=createCanvasControlsRelay({host,identity:()=>({projectId:'P',hostId:'h',sessionId:'canvas'}),connected:()=>true,api:{fetchApi:async(path,init)=>{
        if(init.method==='GET')return {ok:true,json:async()=>queue};
        const body=JSON.parse(init.body);published.push(body);
        queue={...queue,pending:[],controls_view_seq:body.view_seq};return {ok:true,json:async()=>({ok:true})};
    }}});
    await relay.cycle();
    // The acknowledgement always goes out; it says when its view may predate the write.
    assert.deepEqual(published[0].applied,['r']);
    assert.equal(published[0].view.view_stale,fresh?undefined:true);
    // The view the acknowledgement carried is not sent again.
    for(const cb of callbacks)cb(current);await relay.cycle();assert.equal(published.length,1,'nothing new to say');
    // The canvas then adopts a newer view: a stale publication is replaced, and
    // a fresh one is followed by it once, since its hash alone can't tell
    // (Manage's candidates and filter are outside the hash; Phase 4 D10).
    current={...current,mark:'after'};for(const cb of callbacks)cb(current);await relay.cycle();
    assert.equal(published.length,2);assert.equal(published[1].view.view_stale,undefined);assert.equal(published[1].view.mark,'after');
    await relay.cycle();assert.equal(published.length,2,'and then quiet');
    relay.destroy();
}
console.log('ok');
'''


def test_relay_marks_an_acknowledgement_whose_view_may_be_stale(tmp_path):
    run(RELAY, tmp_path, 'fresh-view-relay', {'RELAY_URL': 'controls_canvas_relay.js'})
