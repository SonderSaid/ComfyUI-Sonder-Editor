import json
import shutil
import subprocess
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]

SCRIPT=r'''
import assert from 'node:assert/strict';
const remote=await import(REMOTE_URL),{createCanvasControlsRelay}=await import(RELAY_URL),{SdkWidgetHost}=await import(SDK_URL);
const {createCanvasControlsHost}=await import(CANVAS_URL);
const intervalFns=[];globalThis.setInterval=f=>(intervalFns.push(f),intervalFns.length);globalThis.clearInterval=()=>{};
const settle=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));};
const controlsView={controls:[{source:'control',control_id:'n',kind:'INT',status:'ok',value:'20'}],fields:[],notes:[]};
if(SCENARIO.startsWith('sdk')){
    let seq=0,resolveSave=null,fail=false,stored='20';const calls=[];
    const owner=new SdkWidgetHost({sessionId:'sdk',secret:'secret',projectId:'P',tabSessionId:'tab',fetchImpl:async(path,init={})=>{
        calls.push({path,method:init.method||'GET',headers:init.headers,body:init.body?JSON.parse(init.body):null});
        if(path.endsWith('/controls')){
            if(init.method==='PUT'){
                if(SCENARIO==='sdk-order')await new Promise(r=>resolveSave=r);
                if(fail)return{ok:false,status:400,json:async()=>({code:'invalid_change',rejected:[{target:'control:n:value',reason:'refused'}]})};
                stored=JSON.parse(init.body).changes['control:n:value'];seq++;
            }
            return{ok:true,status:200,json:async()=>({view:{...controlsView,controls:[{...controlsView.controls[0],value:stored}]},seq})};
        }
        return{ok:true,status:202,json:async()=>({run_ids:['run'],observed:stored})};
    }});
    const host=owner.getControlsHost();host.setVisible(true);await settle();assert.equal(host.getView().controls[0].value,'20');
    if(SCENARIO==='sdk-order'){
        const save=host.applyChanges({'control:n:value':'31'});await settle();assert.ok(resolveSave);
        const generate=owner.generate(1);await settle();assert.equal(calls.some(c=>c.path.endsWith('/generate')),false);
        resolveSave();assert.equal((await save).ok,true);const result=await generate;assert.equal(result.payload.observed,'31');
        const put=calls.find(c=>c.method==='PUT');assert.equal(put.headers['X-Sonder-SDK-Session'],'secret');assert.equal(put.headers['X-Sonder-Tab-Session'],'tab');
    }
    if(SCENARIO==='sdk-refusal'){fail=true;assert.equal((await host.applyChanges({'control:n:value':'bad'})).ok,false);
        // A settled refusal saved nothing; Generate runs the stored values instead
        // of repeating that old answer (the panel decides on saves still in flight).
        const result=await owner.generate(1);assert.equal(result.ok,true);assert.equal(result.payload.observed,'20');
        assert.equal(calls.filter(c=>c.path.endsWith('/generate')).length,1);}
    if(SCENARIO==='sdk-flush'){owner.flushControlsPanel=async()=>false;assert.equal((await owner.generate(1)).payload.code,'controls_not_saved');assert.equal(calls.some(c=>c.path.endsWith('/generate')),false);}
    owner.destroy();
}
if(SCENARIO.startsWith('mounted')){
    let seq=1,pending=[],applied=[],rejected=[],savedSeen=[],gets=0,postingResolve=null,delayHide=false,releaseHide=null,remoteVisible=false;
    const host=remote.createMountedControlsHost({projectId:'P',hostId:'h',sessionId:'tab',apiURL:p=>p,canWrite:()=>true,fetchImpl:async(path,init={})=>{
        if(path.includes('/controls?')){gets++;if(controlsView.cleared)return{ok:true,json:async()=>({view:null,view_seq:0,applied:[],rejected:[]})};
            return{ok:true,json:async()=>({view:controlsView,view_seq:seq,applied:[...applied],rejected:[...rejected]})};}
        const body=JSON.parse(init.body);
        if(path.endsWith('/control_changes')){
            if(SCENARIO==='mounted-order'&&body.changes['control:n:value']==='30')await new Promise(r=>postingResolve=r);
            const rid=pending.length?'r2':'r';pending.push({request_id:rid,changes:body.changes});
            if(SCENARIO==='mounted-post-race')await new Promise(r=>postingResolve=r);
            return{ok:true,json:async()=>({request_id:rid})};
        }
        if(delayHide&&!body.visible)await new Promise(r=>releaseHide=r);
        remoteVisible=body.visible;savedSeen.push(...body.seen);applied=applied.filter(r=>!body.seen.includes(r));
        return{ok:true,json:async()=>({ok:true})};
    }});
    host.setVisible(true);await settle();assert.equal(gets,1);
    host.presence({controls_view_seq:1});await settle();assert.equal(gets,1);
    if(SCENARIO==='mounted-abandon-restart'||SCENARIO==='mounted-abandon-cleared'){
        host.presence({host:{session_id:'canvas-1'}});await settle();
        const work=host.applyChanges({'control:n:value':'31'});await settle();
        if(SCENARIO==='mounted-abandon-restart')host.presence({host:{session_id:'canvas-2'}});
        else{controlsView.cleared=true;intervalFns.at(-1)();}
        // The server dropped the relay; the request settles instead of polling forever.
        const result=await work;assert.equal(result.ok,false);assert.match(result.rejected[0].reason,/canvas (restarted|closed)/);
        delete controlsView.cleared;const before=gets;intervalFns.at(-1)();await settle();assert.equal(gets,before);
    }
    if(SCENARIO==='mounted-abandon-recreated'){
        host.presence({host:{session_id:'canvas-1'}});seq=5;host.changed({host_id:'h'});await settle();
        const work=host.applyChanges({'control:n:value':'31'});await settle();
        // Same canvas session, but its relay was cleared and published afresh.
        seq=1;host.changed({host_id:'h'});
        const result=await work;assert.equal(result.ok,false);assert.match(result.rejected[0].reason,/reconnected/);
    }
    if(SCENARIO==='mounted-ack'){
        let finished=false;const work=host.applyChanges({'control:n:value':'31'}).then(r=>{finished=true;return r;});await settle();assert.equal(finished,false);
        applied=['r'];seq=2;host.changed({host_id:'h'});assert.equal((await work).ok,true);await settle();assert.ok(savedSeen.includes('r'));
    }
    if(SCENARIO==='mounted-post-race'){
        const work=host.applyChanges({'control:n:value':'31'});await settle();assert.ok(postingResolve);
        applied=['r'];seq=2;host.changed({host_id:'h'});await settle();assert.equal(savedSeen.includes('r'),false);
        postingResolve();assert.equal((await work).ok,true);await settle();assert.ok(savedSeen.includes('r'));
    }
    if(SCENARIO==='mounted-refusal'){
        const work=host.applyChanges({'control:n:value':'31'});await settle();rejected=[{request_id:'r',target:'control:n:value',reason:'canvas refused'}];seq=2;
        host.changed({host_id:'h'});assert.equal((await work).rejected[0].reason,'canvas refused');
    }
    if(SCENARIO==='mounted-visibility'){
        delayHide=true;host.setVisible(false);host.setVisible(true);await settle();assert.ok(releaseHide);
        releaseHide();await settle();assert.equal(remoteVisible,true);
    }
    if(SCENARIO==='mounted-order'){
        const older=host.applyChanges({'control:n:value':'30'}),newer=host.applyChanges({'control:n:value':'31'});
        await settle();assert.equal(pending.length,0);postingResolve();await settle();
        assert.deepEqual(pending.map(p=>p.changes['control:n:value']),['30','31']);
        applied=['r','r2'];host.changed({host_id:'h'});assert.equal((await older).ok,true);assert.equal((await newer).ok,true);
    }
    host.destroy();
}
if(SCENARIO.startsWith('relay')){
    let queue={ok:true,pending:[{request_id:'r',prompt_hash:'before',changes:{'control:n:value':'31'}}],controls_visible:true,controls_view_seq:1};
    let hash='before',writes=0,puts=0,failPut=['relay-retry','relay-owner-observation'].includes(SCENARIO),published=null;
    const callbacks=new Set();
    const host={getView:()=>controlsView,setMountedVisible(){},refresh:async()=>controlsView,getPromptHash:async()=>hash,
        subscribe(cb){callbacks.add(cb);return()=>callbacks.delete(cb);},async applyChanges(changes,options){assert.equal(await options.canApply(),true);assert.equal(options.expectedPromptHash,hash);writes++;hash='after';return{ok:true,rejected:[]};}};
    if(SCENARIO==='relay-stale')hash='external';
    const relay=createCanvasControlsRelay({host,identity:()=>({projectId:'P',hostId:'h',sessionId:'canvas'}),connected:()=>true,api:{fetchApi:async(path,init)=>{
        assert.ok(path.startsWith('/sonder-editor/project/P/'));
        if(init.method==='GET')return{ok:true,json:async()=>queue};
        puts++;published=JSON.parse(init.body);
        if(failPut){failPut=false;throw Error('lost response');}
        // aiohttp refuses an oversized body with plain text, not JSON.
        if(SCENARIO==='relay-too-large'&&puts===1)return{ok:false,status:413,json:async()=>{throw new SyntaxError('not JSON');}};
        if(SCENARIO==='relay-invalid-view'&&puts===1)return{ok:false,status:400,json:async()=>({ok:false,code:'invalid_view'})};
        queue={...queue,pending:[],controls_view_seq:published.view_seq};return{ok:true,json:async()=>({ok:true})};
    }}});
    // getView needs ready to publish; raw renderer flags are not durable state.
    controlsView.state='ready';await relay.cycle();
    if(SCENARIO==='relay-retry'){assert.equal(writes,1);await relay.cycle();assert.equal(writes,1);assert.equal(puts,2);assert.deepEqual(published.applied,['r']);}
    if(SCENARIO==='relay-owner-observation'){
        const {readFileSync}=await import('node:fs');const source=readFileSync(CONTROLLER_PATH,'utf8');
        const method=source.slice(source.indexOf('    _applyOwnerState('),source.indexOf('    _stopOwnerPolling()'));
        const prototype=new Function('return ({'+method+'})')();
        const controller={state:{activeOwner:null,lastOwnerSignature:''},_editorSessionId:'canvas',_controlsRelay:relay,
            _ownerSignature:o=>JSON.stringify(o),_recordDiagEvent(){},_reconcileOwnerStateEffects(){return false;},render(){}};
        prototype._applyOwnerState.call(controller,{session_id:'new-owner',host_mode:'tab'},'late-poll');await settle();
        assert.equal(writes,1);assert.deepEqual(published.applied,['r']);
    }
    if(SCENARIO==='relay-stale'){assert.equal(writes,0);assert.equal(published.rejected[0].request_id,'r');}
    if(SCENARIO==='relay-ack'){assert.equal(writes,1);assert.deepEqual(published.applied,['r']);assert.equal(published.view_seq,2);}
    if(SCENARIO==='relay-invalid-view'){
        assert.equal(puts,2);assert.deepEqual(published.applied,['r']);assert.match(published.view.error,/could not be shown/);
    }
    if(SCENARIO==='relay-too-large'){
        // The acknowledgement still reaches the tab, with a view saying why.
        assert.equal(puts,2);assert.deepEqual(published.applied,['r']);assert.match(published.view.error,/too large/);
        assert.deepEqual(published.view.controls,[]);
        await relay.cycle();assert.equal(puts,2);
    }
    relay.destroy();assert.equal(callbacks.size,0);
}
if(SCENARIO==='relay-fit'){
    const {fitPublicationView}=await import(RELAY_URL);
    const envelope={host_id:'h',session_id:'s',view_seq:1,prompt_hash:'x',applied:[],rejected:[]};
    const small={controls:[{control_id:'a',status:'ok',kind:'COMBO',options:['x','y'],value:'x'}],fields:[],notes:[]};
    assert.equal(fitPublicationView(small,envelope),small);
    const option=i=>'loras/'+'x'.repeat(80)+i+'.safetensors';
    const combo=id=>({control_id:id,status:'ok',kind:'COMBO',options:Array.from({length:2000},(_,i)=>option(i)),value:option(0)});
    const capped=fitPublicationView({controls:['a','b','c','d','e'].map(combo),fields:[],notes:[]},envelope);
    assert.ok(capped.controls.every(d=>d.options.length===200&&d.options_truncated===true));
    const noted=fitPublicationView({controls:[],fields:[],notes:Array.from({length:140},(_,i)=>({note_id:'n'+i,status:'ok',text:'z'.repeat(4000)}))},envelope);
    assert.ok(noted.notes.every(d=>d.text.length===500&&d.note_truncated===true));
    const huge=fitPublicationView({controls:[{control_id:'s',status:'ok',kind:'STRING',value:'v'.repeat(600000)}],fields:[],notes:[]},envelope);
    assert.match(huge.error,/too large/);
    // Bytes, not characters: 400 notes of 600 three-byte characters keep their
    // controls usable by shortening notes further before giving up.
    const wide=fitPublicationView({controls:[{control_id:'a',status:'ok',kind:'INT',value:'1'}],fields:[],notes:Array.from({length:400},(_,i)=>({note_id:'n'+i,status:'ok',text:'日'.repeat(600)}))},envelope);
    assert.ok(!wide.error);assert.equal(wide.controls.length,1);assert.ok(wide.notes.every(d=>d.text.length===80&&d.note_truncated));
    // 180,000 ASCII characters fit by character count but not once a 3-byte character is involved.
    const ascii={controls:[],fields:[],notes:[{note_id:'x',status:'ok',text:'a'.repeat(180000)}]};
    assert.equal(fitPublicationView(ascii,envelope),ascii);
    const multibyte=fitPublicationView({controls:[],fields:[],notes:[{note_id:'x',status:'ok',text:'日'.repeat(180000)}]},envelope);
    assert.equal(multibyte.notes[0].text.length,500);
}
if(SCENARIO==='canvas-http'||SCENARIO==='relay-error-recovery'||SCENARIO==='canvas-revoked'){
    const editor={id:1,type:'SonderEditor'},ctl={id:2,type:'SonderControl',widgets:[{name:'control_id',value:'n'},{name:'value_text',value:'20'}]};
    const root={_nodes:[editor,ctl]};let describes=0,reads=0,fail=true,revoked=false;
    const api={addEventListener(){},removeEventListener(){},async fetchApi(path,init){
        if(path.endsWith('/controls/describe')){describes++;if(SCENARIO==='relay-error-recovery'&&fail)return{ok:false};
            if(SCENARIO==='canvas-revoked')revoked=true;
            const body=JSON.parse(init.body);return{ok:true,json:async()=>({controls:[],fields:[],notes:[],ok:true,changes:body.changes})};}
        reads++;return{ok:true,json:async()=>({pending:[],controls_visible:true,controls_view_seq:1})};
    }};
    const app={rootGraph:root,graphToPrompt:async()=>({output:{1:{class_type:'SonderEditor'},2:{class_type:'SonderControl',inputs:{value:ctl.widgets[1].value}}}})};
    const host=createCanvasControlsHost({app,api,getEditorNode:()=>editor});
    if(SCENARIO==='canvas-http'){
        Object.defineProperty(globalThis,'crypto',{value:{},configurable:true});const first=await host.getPromptHash();
        assert.equal(await host.getPromptHash(),first);ctl.widgets[1].value='31';assert.notEqual(await host.getPromptHash(),first);assert.ok(first.length<128);
    }
    if(SCENARIO==='canvas-revoked'){
        const result=await host.applyChanges({'control:n:value':'31'},{canApply:async()=>!revoked});
        assert.equal(result.ok,false);assert.equal(ctl.widgets[1].value,'20');assert.match(result.rejected[0].reason,/owns/);
    }
    if(SCENARIO==='relay-error-recovery'){
        const relay=createCanvasControlsRelay({host,api,identity:()=>({projectId:'P',hostId:'h',sessionId:'canvas'}),connected:()=>true});
        await relay.cycle();await settle();assert.ok(describes<=2);assert.ok(reads<=2);assert.equal(host.getView().state,'error');
        fail=false;intervalFns.at(-1)();await settle();assert.equal(host.getView().state,'ready');relay.destroy();
    }
    host.destroy();
}
console.log('ok');
'''
@pytest.mark.parametrize('scenario',['sdk-order','sdk-refusal','sdk-flush','mounted-ack','mounted-post-race','mounted-refusal','mounted-visibility','mounted-order','relay-ack','relay-retry','relay-stale','relay-owner-observation','canvas-http','canvas-revoked','relay-error-recovery','relay-too-large','relay-fit','mounted-abandon-restart','mounted-abandon-cleared','mounted-abandon-recreated','relay-invalid-view'])
def test_controls_transport(scenario):
    node=shutil.which('node')
    if not node:pytest.skip('node required for host adapter tests')
    script=SCRIPT.replace('REMOTE_URL',json.dumps((ROOT/'web/js/controls_host_remote.js').as_uri())).replace('RELAY_URL',json.dumps((ROOT/'web/js/controls_canvas_relay.js').as_uri())).replace('SDK_URL',json.dumps((ROOT/'web/js/sdk_host.js').as_uri())).replace('CANVAS_URL',json.dumps((ROOT/'web/js/controls_host_canvas.js').as_uri())).replace('CONTROLLER_PATH',json.dumps(str(ROOT/'web/js/editor_node_controller.js'))).replace('SCENARIO',json.dumps(scenario))
    result=subprocess.run([node,'--input-type=module','-e',script],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='ok'
