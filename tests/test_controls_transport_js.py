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
const controlsView={bindings:[{source:'binding',binding_id:'n',kind:'INT',status:'ok',value:'20'}],notes:[]};
if(SCENARIO.startsWith('sdk')){
    // Server side: the session's workflow and each workflow's own Controls sequence.
    let statusSeq=0,current='A',seqs={A:SCENARIO==='sdk-switch'?5:0,B:0},resolveSave=null,resolveGenerate=null,fail=false,stored='20',served=null;const calls=[];
    const owner=new SdkWidgetHost({sessionId:'sdk',secret:'secret',projectId:'P',tabSessionId:'tab',templateId:'A',templateName:'Alpha',fetchImpl:async(path,init={})=>{
        calls.push({path,method:init.method||'GET',headers:init.headers,body:init.body?JSON.parse(init.body):null});
        if(path.endsWith('/controls')){
            if(init.method==='PUT'){
                if(SCENARIO==='sdk-order'||SCENARIO==='sdk-switch')await new Promise(r=>resolveSave=r);
                if(fail)return{ok:false,status:400,json:async()=>({code:'invalid_change',rejected:[{target:'binding:n:value',reason:'refused'}]})};
                stored=JSON.parse(init.body).changes['binding:n:value'];seqs[current]++;
            }
            const template=served||current;
            return{ok:true,status:200,json:async()=>({view:{...controlsView,bindings:[{...controlsView.bindings[0],value:`${stored}@${template}`.replace(/@A$/,'')}]},seq:seqs[template],template_id:template})};
        }
        if(path.endsWith('/workflow')){
            if(SCENARIO==='sdk-switching')await new Promise(r=>resolveSave=r);
            current='B';
            // The server switched, but the answer never reached the page.
            if(SCENARIO==='sdk-lost-answer')throw new TypeError('Failed to fetch');
            return{ok:true,status:200,json:async()=>({ok:true,template_id:'B',template_name:'Beta',pending_jobs:2})};
        }
        if(path.endsWith('/status'))return{ok:true,status:200,json:async()=>({session_id:'sdk',seq:++statusSeq,template_id:current,template_name:current==='B'?'Beta':'Alpha',runs:[]})};
        if(SCENARIO==='sdk-busy')await new Promise(r=>resolveGenerate=r);
        if(current!==JSON.parse(init.body).template_id)return{ok:false,status:409,json:async()=>({ok:false,code:'workflow_changed',template_id:current})};
        return{ok:true,status:202,json:async()=>({run_ids:['run'],observed:stored,template_id:JSON.parse(init.body).template_id})};
    }});
    const host=owner.getControlsHost();host.setVisible(true);await settle();assert.equal(host.getView().bindings[0].value,'20');
    if(SCENARIO==='sdk-order'){
        const save=host.applyChanges({'binding:n:value':'31'});await settle();assert.ok(resolveSave);
        const generate=owner.generate(1);await settle();assert.equal(calls.some(c=>c.path.endsWith('/generate')),false);
        resolveSave();assert.equal((await save).ok,true);const result=await generate;assert.equal(result.payload.observed,'31');
        const put=calls.find(c=>c.method==='PUT');assert.equal(put.headers['X-Sonder-SDK-Session'],'secret');assert.equal(put.headers['X-Sonder-Tab-Session'],'tab');
    }
    if(SCENARIO==='sdk-refusal'){fail=true;assert.equal((await host.applyChanges({'binding:n:value':'bad'})).ok,false);
        // A settled refusal saved nothing; Generate runs the stored values instead
        // of repeating that old answer (the panel decides on saves still in flight).
        const result=await owner.generate(1);assert.equal(result.ok,true);assert.equal(result.payload.observed,'20');
        assert.equal(calls.filter(c=>c.path.endsWith('/generate')).length,1);}
    if(SCENARIO==='sdk-manage'){
        // Candidates are asked for only while Manage is open, with the filter.
        assert.ok(!calls.at(-1).path.includes('manage'));
        host.setManage(true,'cfg & seed');await settle();
        assert.ok(calls.at(-1).path.endsWith('/controls?manage=1&filter=cfg%20%26%20seed'),calls.at(-1).path);
        await host.applyChanges({'binding:n:label':'x'});assert.ok(calls.at(-1).path.includes('manage=1'));
        host.setManage(false,'');await settle();assert.ok(calls.at(-1).path.endsWith('/controls'));
        assert.deepEqual(host.capabilities,{hide:true,removeAuthored:false,serverRows:true});
    }
    if(SCENARIO==='sdk-flush'){owner.flushControlsPanel=async()=>false;assert.equal((await owner.generate(1)).payload.code,'controls_not_saved');assert.equal(calls.some(c=>c.path.endsWith('/generate')),false);}
    if(SCENARIO==='sdk-switch'){
        // An edit made on A, still saving, when Workflow ▾ asks for B.
        const changes=[];owner.onWorkflowChanged(change=>changes.push(change));
        owner.status={seq:1,runs:[{run_id:'r1',state:'running',template_id:'A',template_name:'Alpha'}]};
        const save=host.applyChanges({'binding:n:value':'31'});await settle();
        const switching=owner.switchWorkflow('B');await settle();
        assert.equal(calls.some(c=>c.path.endsWith('/workflow')),false,'the switch waits for the queued save');
        resolveSave();assert.equal((await save).ok,true);const result=await switching;assert.equal(result.ok,true);
        const put=calls.find(c=>c.method==='PUT'),post=calls.find(c=>c.path.endsWith('/workflow'));
        assert.equal(put.body.template_id,'A');assert.deepEqual(post.body,{template_id:'B',expected_template_id:'A'});
        assert.ok(calls.indexOf(put)<calls.indexOf(post));
        assert.deepEqual(changes,[{template_id:'B',name:'Beta',previous:{template_id:'A',name:'Alpha'},pending_jobs:2,still_generating:1}]);
        assert.equal(owner.templateId,'B');assert.equal(owner.templateName,'Beta');
        // Runs keep their place: the run list is the session's, not the workflow's.
        assert.deepEqual(owner.status.runs.map(r=>r.run_id),['r1']);
        // A fresh host: B's sequence (0) is not dropped as older than A's (6).
        const next=owner.getControlsHost();assert.notEqual(next,host);next.setVisible(true);await settle();
        assert.equal(next.getView().state,'ready');assert.equal(next.getView().bindings[0].value,'31@B');
        // The retired host is quiet: no listener sees a later view.
        let heard=0;host.subscribe(()=>heard++);await host.refresh();assert.equal(heard,0);
        const later=next.applyChanges({'binding:n:value':'40'});await settle();resolveSave();await later;
        assert.equal(calls.filter(c=>c.method==='PUT').at(-1).body.template_id,'B');
        next.destroy();
    }
    if(SCENARIO==='sdk-adopt-status'||SCENARIO==='sdk-adopt-refusal'||SCENARIO==='sdk-lost-answer'){
        // The session moved to B without this page's own switch answer: another
        // tab switched it (seen in a status snapshot, or as a refused Generate),
        // or the answer to this page's switch was lost.
        const changes=[];owner.onWorkflowChanged(change=>changes.push(change));
        if(SCENARIO==='sdk-adopt-status'){current='B';assert.equal(owner.applyStatus({session_id:'sdk',seq:5,template_id:'B',template_name:'Beta',runs:[]}),true);}
        if(SCENARIO==='sdk-adopt-refusal'){current='B';const result=await owner.generate(1);assert.equal(result.payload.code,'workflow_changed');await settle();}
        if(SCENARIO==='sdk-lost-answer'){const result=await owner.switchWorkflow('B');assert.equal(result.payload.code,'network');await settle();}
        assert.equal(owner.templateId,'B');assert.equal(owner.templateName,'Beta');
        assert.equal(changes.length,1);assert.equal(changes[0].adopted,true);assert.deepEqual(changes[0].previous,{template_id:'A',name:'Alpha'});
        // A fresh host reads B, and the next Generate is not refused again.
        const next=owner.getControlsHost();assert.notEqual(next,host);next.setVisible(true);await settle();
        assert.equal(next.getView().state,'ready');
        const again=await owner.generate(1);assert.equal(again.ok,true);assert.equal(again.payload.template_id,'B');
        next.destroy();
    }
    if(SCENARIO==='sdk-switching'){
        // While the switch is in flight, edits and Generate are refused, not lost.
        const switching=owner.switchWorkflow('B');await settle();
        assert.equal(owner.switching,true);
        const edit=await host.applyChanges({'binding:n:value':'55'});assert.equal(edit.ok,false);assert.match(edit.rejected[0].reason,/switching/);
        assert.equal((await owner.generate(1)).payload.code,'workflow_switching');
        assert.equal(calls.some(c=>c.method==='PUT'),false);
        resolveSave();assert.equal((await switching).ok,true);assert.equal(owner.switching,false);
        // A status snapshot of the page's own switch adopts nothing twice.
        owner.applyStatus({session_id:'sdk',seq:9,template_id:'B',template_name:'Beta',runs:[]});
    }
    if(SCENARIO==='sdk-hold'){
        // The toolbar holds the workflow from the click: the menu is busy before generate() runs.
        const release=owner.holdWorkflow();assert.equal(owner.busy,true);assert.equal(host.workflows.busy(),true);
        release();release();assert.equal(owner.busy,false);
    }
    if(SCENARIO==='sdk-stale-view'){
        // A view of a workflow this page has switched away from is not adopted.
        owner.templateId='B';served='A';stored='99';await host.refresh();assert.equal(host.getView().bindings[0].value,'20');
        served='B';await host.refresh();assert.equal(host.getView().bindings[0].value,'99@B');
    }
    if(SCENARIO==='sdk-captures'){
        // The click's workflow travels with it, even if the page switches while
        // the panel flushes.
        owner.flushControlsPanel=async()=>{owner.templateId='B';return true;};
        const result=await owner.generate(1);assert.equal(result.payload.template_id,'A');
        owner.flushControlsPanel=null;owner.templateId='A';
        const edit=host.applyChanges({'binding:n:value':'41'});owner.templateId='B';await edit;
        assert.equal(calls.filter(c=>c.method==='PUT').at(-1).body.template_id,'A');
    }
    if(SCENARIO==='sdk-busy'){
        let paints=0;host.subscribe(()=>paints++);
        const generating=owner.generate(1);await settle();
        assert.equal(owner.busy,true);assert.equal(host.workflows.busy(),true);assert.ok(paints>=1,'the panel repaints');
        assert.equal((await owner.switchWorkflow('B')).payload.code,'busy');assert.equal(calls.some(c=>c.path.endsWith('/workflow')),false);
        resolveGenerate();await generating;assert.equal(owner.busy,false);
        assert.deepEqual(host.workflows.current(),{template_id:'A',name:'Alpha'});
    }
    owner.destroy();
}
if(SCENARIO.startsWith('mounted')){
    let seq=1,pending=[],applied=[],rejected=[],savedSeen=[],gets=0,postingResolve=null,delayHide=false,releaseHide=null,remoteVisible=false;
    const host=remote.createMountedControlsHost({projectId:'P',hostId:'h',sessionId:'tab',apiURL:p=>p,canWrite:()=>true,fetchImpl:async(path,init={})=>{
        if(path.includes('/controls?')){gets++;if(controlsView.cleared)return{ok:true,json:async()=>({view:null,view_seq:0,applied:[],rejected:[]})};
            return{ok:true,json:async()=>({view:controlsView,view_seq:seq,applied:[...applied],rejected:[...rejected]})};}
        const body=JSON.parse(init.body);
        if(path.endsWith('/control_changes')){
            if(SCENARIO==='mounted-order'&&body.changes['binding:n:value']==='30')await new Promise(r=>postingResolve=r);
            const rid=pending.length?'r2':'r';pending.push({request_id:rid,changes:body.changes});
            if(SCENARIO==='mounted-post-race')await new Promise(r=>postingResolve=r);
            return{ok:true,json:async()=>({request_id:rid})};
        }
        if(delayHide&&!body.visible)await new Promise(r=>releaseHide=r);
        remoteVisible=body.visible;globalThis.lastReport=body;savedSeen.push(...body.seen);applied=applied.filter(r=>!body.seen.includes(r));
        return{ok:true,json:async()=>({ok:true})};
    }});
    host.setVisible(true);await settle();assert.equal(gets,1);
    host.presence({controls_view_seq:1});await settle();assert.equal(gets,1);
    if(SCENARIO==='mounted-abandon-restart'||SCENARIO==='mounted-abandon-cleared'){
        host.presence({host:{session_id:'canvas-1'}});await settle();
        const work=host.applyChanges({'binding:n:value':'31'});await settle();
        if(SCENARIO==='mounted-abandon-restart')host.presence({host:{session_id:'canvas-2'}});
        else{controlsView.cleared=true;intervalFns.at(-1)();}
        // The server dropped the relay; the request settles instead of polling forever.
        const result=await work;assert.equal(result.ok,false);assert.match(result.rejected[0].reason,/canvas (restarted|closed)/);
        // No answer came: it may have landed, so it is marked not confirmed (D8).
        assert.equal(result.unconfirmed,true);
        delete controlsView.cleared;const before=gets;intervalFns.at(-1)();await settle();assert.equal(gets,before);
    }
    if(SCENARIO==='mounted-abandon-recreated'){
        host.presence({host:{session_id:'canvas-1'}});seq=5;host.changed({host_id:'h'});await settle();
        const work=host.applyChanges({'binding:n:value':'31'});await settle();
        // Same canvas session, but its relay was cleared and published afresh.
        seq=1;host.changed({host_id:'h'});
        const result=await work;assert.equal(result.ok,false);assert.match(result.rejected[0].reason,/reconnected/);assert.equal(result.unconfirmed,true);
    }
    if(SCENARIO==='mounted-restore'){
        controlsView.restore={restorable:[{binding_id:'n',label:'Steps',path:'1',input:'steps'}],unmatched:[]};
        seq++;host.changed({host_id:'h'});await settle();
        assert.deepEqual(host.getView().restore,controlsView.restore);
        const changes={'workflow:restore':{bindings:['n']}};
        const work=host.applyChanges(changes);await settle();assert.deepEqual(pending[0].changes,changes);
        applied=['r'];seq++;host.changed({host_id:'h'});assert.equal((await work).ok,true);
    }
    if(SCENARIO==='mounted-ack'){
        let finished=false;const work=host.applyChanges({'binding:n:value':'31'}).then(r=>{finished=true;return r;});await settle();assert.equal(finished,false);
        applied=['r'];seq=2;host.changed({host_id:'h'});assert.equal((await work).ok,true);await settle();assert.ok(savedSeen.includes('r'));
    }
    if(SCENARIO==='mounted-post-race'){
        const work=host.applyChanges({'binding:n:value':'31'});await settle();assert.ok(postingResolve);
        applied=['r'];seq=2;host.changed({host_id:'h'});await settle();assert.equal(savedSeen.includes('r'),false);
        postingResolve();assert.equal((await work).ok,true);await settle();assert.ok(savedSeen.includes('r'));
    }
    if(SCENARIO==='mounted-refusal'){
        const work=host.applyChanges({'binding:n:value':'31'});await settle();rejected=[{request_id:'r',target:'binding:n:value',reason:'canvas refused'}];seq=2;
        host.changed({host_id:'h'});const refused=await work;assert.equal(refused.rejected[0].reason,'canvas refused');
        assert.equal(refused.unconfirmed,undefined,'a refusal is an answer');
    }
    if(SCENARIO==='mounted-manage'){
        // Manage and its filter travel with the visibility report; the canvas lists.
        host.setManage(true,'seed');await settle();assert.deepEqual([lastReport.manage,lastReport.filter],[true,'seed']);
        host.setManage(false,'');await settle();assert.deepEqual([lastReport.manage,lastReport.filter],[false,'']);
        assert.deepEqual(host.capabilities,{hide:false,removeAuthored:true});
    }
    if(SCENARIO==='mounted-visibility'){
        delayHide=true;host.setVisible(false);host.setVisible(true);await settle();assert.ok(releaseHide);
        releaseHide();await settle();assert.equal(remoteVisible,true);
    }
    if(SCENARIO==='mounted-order'){
        const older=host.applyChanges({'binding:n:value':'30'}),newer=host.applyChanges({'binding:n:value':'31'});
        await settle();assert.equal(pending.length,0);postingResolve();await settle();
        assert.deepEqual(pending.map(p=>p.changes['binding:n:value']),['30','31']);
        applied=['r','r2'];host.changed({host_id:'h'});assert.equal((await older).ok,true);assert.equal((await newer).ok,true);
    }
    host.destroy();
}
if(SCENARIO.startsWith('relay')){
    let queue={ok:true,pending:SCENARIO==='relay-retry-idle'?[]:[{request_id:'r',prompt_hash:'before',changes:{'binding:n:value':'31'}}],controls_visible:true,controls_view_seq:1,
        controls_manage:SCENARIO==='relay-manage',controls_filter:SCENARIO==='relay-manage'?'cfg':''};
    let hash='before',writes=0,puts=0,failPut=['relay-retry','relay-retry-idle','relay-owner-observation'].includes(SCENARIO),published=null;
    const callbacks=new Set();
    const managed=[];
    let canvasView=controlsView;
    const host={getView:()=>canvasView,setMountedVisible(){},setMountedManage(on,filter){managed.push([on,filter]);},refresh:async()=>controlsView,getPromptHash:async()=>hash,
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
    if(SCENARIO==='relay-retry'){assert.equal(writes,1);await relay.cycle();assert.equal(writes,1);assert.equal(puts,2);assert.deepEqual(published.applied,['r']);
        // The retried publication was this view: nothing new to send.
        await relay.cycle();assert.equal(puts,2);}
    if(SCENARIO==='relay-retry-idle'){
        // A retried publication with no write behind it records its hash: the
        // next cycle has nothing to send.
        assert.equal(puts,1);await relay.cycle();await settle();assert.equal(puts,2);
        await relay.cycle();await settle();assert.equal(puts,2);
    }
    if(SCENARIO==='relay-listing'){
        // Published and acknowledged; an unchanged view and hash send nothing.
        assert.equal(puts,1);await relay.cycle();assert.equal(puts,1);
        // Manage opened in the tab: the canvas adopts a view with candidates.
        // The prompt hash leaves Manage out, so only the new view says so.
        canvasView={...controlsView,state:'ready',candidates:[{path:'3',input:'cfg'}]};
        await relay.cycle();assert.equal(puts,2);assert.deepEqual(published.view.candidates,[{path:'3',input:'cfg'}]);
        await relay.cycle();assert.equal(puts,2);
        // A narrower filter is another adoption: it republishes once.
        canvasView={...canvasView,candidates:[]};
        await relay.cycle();assert.equal(puts,3);assert.deepEqual(published.view.candidates,[]);
        await relay.cycle();assert.equal(puts,3);
        // A forced re-read adopts an equal view: it republishes exactly once.
        canvasView={...canvasView};
        await relay.cycle();assert.equal(puts,4);await relay.cycle();assert.equal(puts,4);
    }
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
    if(SCENARIO==='relay-manage'){assert.deepEqual(managed[0],[true,'cfg']);relay.destroy();assert.deepEqual(managed.at(-1),[false,'']);}
    if(SCENARIO==='relay-ack'){assert.equal(writes,1);assert.deepEqual(published.applied,['r']);assert.equal(published.view_seq,2);}
    if(SCENARIO==='relay-invalid-view'){
        assert.equal(puts,2);assert.deepEqual(published.applied,['r']);assert.match(published.view.error,/could not be shown/);
    }
    if(SCENARIO==='relay-too-large'){
        // The acknowledgement still reaches the tab, with a view saying why.
        assert.equal(puts,2);assert.deepEqual(published.applied,['r']);assert.match(published.view.error,/too large/);
        assert.deepEqual(published.view.bindings,[]);
        await relay.cycle();assert.equal(puts,2);
    }
    if(SCENARIO!=='relay-manage')relay.destroy();assert.equal(callbacks.size,0);
}
if(SCENARIO==='relay-fit'){
    const {fitPublicationView}=await import(RELAY_URL);
    const envelope={host_id:'h',session_id:'s',view_seq:1,prompt_hash:'x',applied:[],rejected:[]};
    const small={bindings:[{binding_id:'a',status:'ok',kind:'COMBO',options:['x','y'],value:'x'}],notes:[]};
    assert.equal(fitPublicationView(small,envelope),small);
    // Manage's candidates shrink first, and say so.
    const listing={bindings:[{binding_id:'a',status:'ok',kind:'INT',value:'1'}],notes:[],suggestions:[{binding_id:'s',targets:[]}],
        candidates:Array.from({length:500},(_,i)=>({path:String(i),input:'w'.repeat(1200),targets:[],label:'x'}))};
    const fitted=fitPublicationView(listing,envelope);
    assert.ok(fitted.candidates.length<500&&fitted.candidates.length>0);assert.equal(fitted.candidates_truncated,true);
    assert.deepEqual(fitted.suggestions,listing.suggestions);assert.deepEqual(fitted.bindings,listing.bindings);
    const option=i=>'loras/'+'x'.repeat(80)+i+'.safetensors';
    const combo=id=>({binding_id:id,status:'ok',kind:'COMBO',options:Array.from({length:2000},(_,i)=>option(i)),value:option(0)});
    const capped=fitPublicationView({bindings:['a','b','c','d','e'].map(combo),notes:[]},envelope);
    assert.ok(capped.bindings.every(d=>d.options.length===200&&d.options_truncated===true));
    const noted=fitPublicationView({bindings:[],notes:Array.from({length:140},(_,i)=>({note_id:'n'+i,status:'ok',text:'z'.repeat(4000)}))},envelope);
    assert.ok(noted.notes.every(d=>d.text.length===500&&d.note_truncated===true));
    // A node's help shortens to 80 code points, then goes, before Manage's lists
    // and without the note path; a surrogate pair is never split.
    const helped=n=>({bindings:Array.from({length:n},(_,i)=>({binding_id:'t'+i,status:'ok',kind:'INT',value:'1',tooltip:'\u{1F600}'.repeat(400),
        ...(i%2?{kind:'ROWS',value:[],fields:[{id:'f',kind:'INT',tooltip:'\u{1F600}'.repeat(400)}]}:{})})),notes:[],
        candidates:Array.from({length:40},(_,i)=>({path:String(i),input:'w',targets:[],label:'x'}))});
    const short=fitPublicationView(helped(250),envelope);
    assert.ok(short.bindings.every(d=>Array.from(d.tooltip).length===80&&d.tooltip.endsWith('\u2026')&&!/[\uD800-\uDBFF]\u2026$/.test(d.tooltip)));
    assert.ok(short.bindings.filter(d=>d.fields).every(d=>Array.from(d.fields[0].tooltip).length===80));
    assert.equal(short.tooltips_truncated,undefined);assert.equal(short.candidates.length,40,'Manage keeps its list');
    assert.ok(short.bindings.every(d=>!d.note_truncated));
    const dropped=fitPublicationView(helped(1400),envelope);
    assert.ok(!dropped.error);assert.equal(dropped.tooltips_truncated,true);
    assert.ok(dropped.bindings.every(d=>!('tooltip' in d)&&(d.fields||[]).every(f=>!('tooltip' in f))));
    assert.equal(fitPublicationView(listing,envelope).tooltips_truncated,undefined,'no help, no flag');
    const huge=fitPublicationView({bindings:[{binding_id:'s',status:'ok',kind:'STRING',value:'v'.repeat(600000)}],notes:[]},envelope);
    assert.match(huge.error,/too large/);
    // Bytes, not characters: 400 notes of 600 three-byte characters keep their
    // controls usable by shortening notes further before giving up.
    const wide=fitPublicationView({bindings:[{binding_id:'a',status:'ok',kind:'INT',value:'1'}],notes:Array.from({length:400},(_,i)=>({note_id:'n'+i,status:'ok',text:'日'.repeat(600)}))},envelope);
    assert.ok(!wide.error);assert.equal(wide.bindings.length,1);assert.ok(wide.notes.every(d=>d.text.length===80&&d.note_truncated));
    // 180,000 ASCII characters fit by character count but not once a 3-byte character is involved.
    const ascii={bindings:[],notes:[{note_id:'x',status:'ok',text:'a'.repeat(180000)}]};
    assert.equal(fitPublicationView(ascii,envelope),ascii);
    const multibyte=fitPublicationView({bindings:[],notes:[{note_id:'x',status:'ok',text:'日'.repeat(180000)}]},envelope);
    assert.equal(multibyte.notes[0].text.length,500);
    // R6: maximum count/length UTF-8 group names, metadata and ACKs survive
    // publication fitting; only cosmetic previews may be shortened.
    const names=Array.from({length:256},(_,i)=>String(i).padStart(3,'0')+'😀'.repeat(117));
    const groups=Object.fromEntries(names.map((n,i)=>[n,{order:i}]));
    const grouped={groups,group_order:names,bindings:names.map((n,i)=>({binding_id:'g'+i,group:n,status:'ok',kind:'INT',value:'1'})),
        notes:Array.from({length:100},(_,i)=>({note_id:'n'+i,group:names[i],text:'日'.repeat(4000)}))};
    const ack={...envelope,applied:Array.from({length:32},(_,i)=>'request-'+i)};
    const budgeted=fitPublicationView(grouped,ack);
    assert.ok(!budgeted.error);assert.deepEqual(budgeted.group_order,names);assert.deepEqual(budgeted.groups,groups);
    assert.deepEqual(budgeted.bindings,grouped.bindings);
    assert.ok(new TextEncoder().encode(JSON.stringify({...ack,view:budgeted})).length<512*1024);
    assert.ok(budgeted.notes.every(n=>n.note_truncated));
    // Category warnings are for Manage: they go before Manage's candidates, the rows stay.
    const warning={code:'overlap',message:'m'.repeat(2000),pairs:[['a','b']],count:1};
    const modes={bindings:[{binding_id:'a',status:'ok',kind:'INT',value:'1'}],notes:[],
        preset_categories:Array.from({length:16},(_,i)=>({id:'c'+i,name:'C',description:'',order:i,choices:['a','b'],warnings:Array(10).fill(warning)})),
        candidates:Array.from({length:300},(_,i)=>({path:String(i),input:'w'.repeat(1200),targets:[],label:'x'}))};
    const shrunk=fitPublicationView(modes,envelope);
    assert.ok(shrunk.preset_categories.every(c=>c.warnings.length===0&&c.choices.length===2));
    assert.equal(shrunk.candidates.length,300);assert.ok(!shrunk.candidates_truncated);
}
if(SCENARIO==='canvas-http'||SCENARIO==='relay-error-recovery'||SCENARIO==='canvas-revoked'){
    const editor={id:1,type:'SonderEditor'},ctl={id:2,type:'KSampler',widgets:[{name:'steps',value:'20'}]};
    const root={_nodes:[editor,ctl]};let describes=0,reads=0,fail=true,revoked=false;
    const api={addEventListener(){},removeEventListener(){},async fetchApi(path,init){
        if(path.endsWith('/controls/describe')){describes++;if(SCENARIO==='relay-error-recovery'&&fail)return{ok:false};
            if(SCENARIO==='canvas-revoked')revoked=true;
            const body=JSON.parse(init.body);return{ok:true,json:async()=>({bindings:[],notes:[],ok:true,changes:body.changes})};}
        reads++;return{ok:true,json:async()=>({pending:[],controls_visible:true,controls_view_seq:1})};
    }};
    const app={rootGraph:root,graphToPrompt:async()=>({output:{1:{class_type:'SonderEditor'},2:{class_type:'KSampler',inputs:{value:ctl.widgets[0].value}}}})};
    const host=createCanvasControlsHost({app,api,getEditorNode:()=>editor});
    if(SCENARIO==='canvas-http'){
        Object.defineProperty(globalThis,'crypto',{value:{},configurable:true});const first=await host.getPromptHash();
        assert.equal(await host.getPromptHash(),first);ctl.widgets[0].value='31';assert.notEqual(await host.getPromptHash(),first);assert.ok(first.length<128);
    }
    if(SCENARIO==='canvas-revoked'){
        const result=await host.applyChanges({'binding:n:value':'31'},{canApply:async()=>!revoked});
        assert.equal(result.ok,false);assert.equal(ctl.widgets[0].value,'20');assert.match(result.rejected[0].reason,/owns/);
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
@pytest.mark.parametrize('scenario',['sdk-adopt-status','sdk-adopt-refusal','sdk-lost-answer','sdk-switching','sdk-hold','sdk-switch','sdk-stale-view','sdk-captures','sdk-busy','sdk-manage','mounted-manage','relay-manage','sdk-order','sdk-refusal','sdk-flush','mounted-restore','mounted-ack','mounted-post-race','mounted-refusal','mounted-visibility','mounted-order','relay-ack','relay-retry','relay-stale','relay-owner-observation','canvas-http','canvas-revoked','relay-error-recovery','relay-too-large','relay-fit','mounted-abandon-restart','mounted-abandon-cleared','mounted-abandon-recreated','relay-invalid-view','relay-listing','relay-retry-idle'])
def test_controls_transport(scenario):
    node=shutil.which('node')
    if not node:pytest.skip('node required for host adapter tests')
    script=SCRIPT.replace('REMOTE_URL',json.dumps((ROOT/'web/js/controls_host_remote.js').as_uri())).replace('RELAY_URL',json.dumps((ROOT/'web/js/controls_canvas_relay.js').as_uri())).replace('SDK_URL',json.dumps((ROOT/'web/js/sdk_host.js').as_uri())).replace('CANVAS_URL',json.dumps((ROOT/'web/js/controls_host_canvas.js').as_uri())).replace('CONTROLLER_PATH',json.dumps(str(ROOT/'web/js/editor_node_controller.js'))).replace('SCENARIO',json.dumps(scenario))
    result=subprocess.run([node,'--input-type=module','-e',script],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='ok'
