import json
import shutil
import subprocess
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=r'''
import assert from 'node:assert/strict';
class El {
    constructor(tag,text=''){this.tagName=tag.toUpperCase();this._text=text;this.children=[];this.parentElement=null;this.style={};this.dataset={};this.listeners={};this.attributes={};this.value='';this.type='';}
    append(...items){for(const n of items){if(n.tagName==='#FRAGMENT'){this.append(...[...n.children]);continue;}n.remove?.();n.parentElement=this;this.children.push(n);}}
    prepend(n){n.remove?.();n.parentElement=this;this.children.unshift(n);}
    appendChild(n){this.append(n);return n;}
    remove(){if(this.parentElement){if((SCENARIO==='editor-focus-refresh'||SCENARIO==='groups-manage')&&this.contains(document.activeElement))document.activeElement.blur();const a=this.parentElement.children;a.splice(a.indexOf(this),1);this.parentElement=null;}}
    after(n){const p=this.parentElement;n.remove?.();n.parentElement=p;p.children.splice(p.children.indexOf(this)+1,0,n);}
    before(n){const p=this.parentElement;n.remove?.();n.parentElement=p;p.children.splice(p.children.indexOf(this),0,n);}
    replaceWith(n){const p=this.parentElement;n.remove?.();n.parentElement=p;p.children.splice(p.children.indexOf(this),1,n);this.parentElement=null;}
    select(){}
    getBoundingClientRect(){return {left:0,top:0,right:0,bottom:0,width:0,height:0};}
    get isConnected(){let n=this;while(n.parentElement)n=n.parentElement;return n===document.body;}
    dispatchEvent(e){this.dispatch(e.type);}
    replaceChildren(...n){for(const c of [...this.children])c.remove();this._text='';this.append(...n);}
    get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
    set textContent(t){this.replaceChildren();this._text=String(t);}
    set innerHTML(t){throw Error('HTML parsing is forbidden');}
    get options(){return this.children;}
    setAttribute(k,v){this.attributes[k]=String(v);}
    addEventListener(k,f){(this.listeners[k]||=[]).push(f);}
    removeEventListener(k,f){this.listeners[k]=(this.listeners[k]||[]).filter(x=>x!==f);}
    dispatch(k,e={}){for(const f of this.listeners[k]||[])f({target:this,preventDefault(){},...e});}
    contains(n){return n===this||this.children.some(c=>c.contains?.(n));}
    focus(){if(document.activeElement!==this){document.activeElement?.blur?.(this);document.activeElement=this;this.dispatch('focus');}}
    blur(next=document.body){if(document.activeElement===this){const parents=[];for(let n=this.parentElement;n;n=n.parentElement)parents.push(n);document.activeElement=document.body;this.dispatch('blur',{relatedTarget:next});for(const p of parents)p.dispatch('focusout',{target:this,relatedTarget:next});}}
}
const events={};globalThis.window={innerWidth:1366,innerHeight:900,addEventListener(k,f){(events[k]||=[]).push(f);},removeEventListener(k,f){events[k]=(events[k]||[]).filter(x=>x!==f);}};
globalThis.requestAnimationFrame=f=>f();
globalThis.localStorage={getItem(){return null;},setItem(){}};
globalThis.document={createElement:t=>new El(t),createTextNode:t=>new El('#TEXT',String(t)),createDocumentFragment:()=>new El('#FRAGMENT'),body:new El('body'),activeElement:null,
    addEventListener(){},removeEventListener(){}};
document.activeElement=document.body;
globalThis.Option=class extends El{constructor(text,value){super('option',text);this.value=value;}};
globalThis.Event=class{constructor(type){this.type=type;}};
const timeoutCalls=[];const nativeTimeout=setTimeout;
globalThis.setTimeout=(f,ms)=>ms===5000?(timeoutCalls.push(f),timeoutCalls.length):nativeTimeout(f,ms);
const {renderPanelMarkdown}=await import(MARKDOWN_URL);
const {mountControlsPanel}=await import(PANEL_URL);
const walk=n=>[n,...n.children.flatMap(walk)];
const settle=async()=>{for(let i=0;i<10;i++)await new Promise(r=>setImmediate(r));};
if(SCENARIO==='markdown'){
    const doc=renderPanelMarkdown('# Heading\n\n## Second\n### Third\nparagraph **bold** *italics* _also_ `code`\nline two\n\n- one\n* two\n\n1. numbered\n\n[safe](https://example.com/a) [bad](javascript:alert(1)) [data](data:text/html,evil) <script>evil</script>');
    const nodes=walk(doc),tags=nodes.map(n=>n.tagName);
    for(const tag of ['H1','H2','H3','P','STRONG','EM','CODE','BR','UL','OL','LI','A'])assert.ok(tags.includes(tag),tag);
    assert.ok(!tags.includes('SCRIPT'));assert.ok(doc.textContent.includes('<script>evil</script>'));
    const links=nodes.filter(n=>n.tagName==='A');assert.equal(links.length,1);assert.equal(links[0].href,'https://example.com/a');assert.equal(links[0].rel,'noopener noreferrer');
}else{
    let view={state:'ready',bindings:[{source:'binding',binding_id:'steps',label:'Steps',kind:'INT',value:'20',note:'**Recommended**',status:'ok',order:1,group:'',section:'workflow'},
        {source:'binding',binding_id:'seed',label:'Seed',kind:'SEED',value:'9',seed_mode:'fixed',note:'',status:'ok',order:3,group:'Advanced',section:'workflow'},
        {source:'binding',binding_id:'adv',label:'Advance',kind:'BOOLEAN',value:false,note:'',status:'ok',order:0,group:'',section:'editor'}],
        notes:[{source:'note',note_id:'intro',text:'# Intro',status:'ok',order:0,group:''}]};
    const calls=[],notices=[],settingWrites=[],listeners=new Set();
    const settings={render:{takePlacementMuted:false,linkedTakePlacement:true},batchRender:{maxFramesPerChunk:121},
        layout:{scaleControls:1},controls:{view:'rows',showAdvanced:false}};
    let refuse=false,defer=false,resolveWrite=null,refuseSetting=false;
    const host={getView:()=>view,subscribe(cb){listeners.add(cb);return()=>listeners.delete(cb);},getEditorSettings:()=>settings,
        setEditorSetting(k,v){settingWrites.push([k,v]);if(refuseSetting)throw Error('setting refused');const [s,n]=k.split('.');settings[s][n]=v;},notify:m=>notices.push(m),
        async applyChanges(changes){calls.push(changes);if(defer)await new Promise(r=>resolveWrite=r);return refuse?{ok:false,rejected:[{reason:'server refusal'}]}:{ok:true,rejected:[]};}};
    if(SCENARIO==='initial-error')view={...view,state:'error',error:'cached read failed'};
    const container=new El('div'),panel=mountControlsPanel(container,host);
    const byTarget=t=>walk(container).find(n=>n.dataset.controlTarget===t);
    const input=t=>walk(byTarget(t)).find(n=>n.tagName==='INPUT');
    const note=t=>walk(container).find(n=>n.dataset.noteTarget===t);
    const key=(value,extra={})=>{const e={key:value,keyCode:0,isComposing:false,ctrlKey:false,metaKey:false,prevented:false,stopped:false,target:document.activeElement,
        preventDefault(){this.prevented=true;},stopImmediatePropagation(){this.stopped=true;},...extra};for(const f of events.keydown||[]){f(e);if(e.stopped)break;}return e;};
    const publish=next=>{view=next;for(const cb of listeners)cb(view);};
    const toggleManage=()=>walk(container).find(n=>n.dataset.controlsManageToggle==='true').dispatch('click');
    const button=(within,text)=>walk(within).find(n=>n.tagName==='BUTTON'&&n.textContent===text);
    if(['layout','layout-refusal','layout-stale','layout-editor','cancel','note-save','ime','note-refusal','note-then-value','problem-statuses','truncated'].includes(SCENARIO))toggleManage();
    const group=name=>walk(container).find(n=>n.dataset.controlsGroup===name);
    const mirror=key=>walk(container).find(n=>n.dataset.controlsMirror===key);

    if(SCENARIO==='restore-notice'){
        publish({...view,relay:{status:'ok'},restore:{restorable:[{binding_id:'steps',path:'3',input:'steps'}],
            unmatched:[{binding_id:'seed',label:'Seed',reason:'ambiguous'}]}});
        const notice=walk(container).find(n=>n.dataset.controlsRestore==='true');
        assert.equal(notice.style.display,'flex');assert.match(notice.textContent,/2 Controls came from an API file/);
        toggleManage();assert.ok(notice.parentElement===container);toggleManage();
        defer=true;button(notice,'Restore Controls').dispatch('click');await settle();
        assert.deepEqual(calls.at(-1),{'workflow:restore':{bindings:['steps']}});
        const flushed=panel.flushPending();resolveWrite();assert.equal(await flushed,true);await settle();
        assert.match(notices.at(-1),/Restored 1 Controls.*1 need Rebind/);
        button(notice,'Rebind').dispatch('click');assert.match(container.textContent,/Choose a new setting for “Seed”/);
        publish({...view,restore:undefined});assert.equal(notice.style.display,'none');
    }
    if(SCENARIO==='restore-all'){
        publish({...view,candidates:[],suggestions:Array.from({length:70},(_,i)=>({binding_id:'old'+i}))});
        toggleManage();const manage=walk(container).find(n=>n.dataset.controlsManage==='true');
        walk(manage).find(n=>n.dataset.controlsAddSettings==='true').dispatch('click');
        button(manage,'Restore 64').dispatch('click');await settle();
        assert.equal(Object.keys(calls.at(-1)).length,64);assert.deepEqual(calls.at(-1)['binding:old0:expose'],{});
        publish({...view,suggestions:[{binding_id:'remaining'}]});button(manage,'Restore all').dispatch('click');await settle();
        assert.deepEqual(calls.at(-1),{'binding:remaining:expose':{}});
    }
    if(SCENARIO==='description'){
        toggleManage();
        const area=walk(container).find(n=>n.dataset.workflowDescription);
        const section=area.parentElement;
        area.focus(); area.value='😀'.repeat(1000);area.dispatch('input');
        key('Enter',{ctrlKey:true,isComposing:true});await settle();assert.equal(calls.length,0);
        key('Enter',{ctrlKey:true});await settle();assert.deepEqual(calls.at(-1),{'workflow:description':'😀'.repeat(1000)});
        area.value='😀'.repeat(1001);area.dispatch('input');key('Enter',{ctrlKey:true});await settle();assert.equal(calls.length,1);
        assert.match(section.textContent,/Not saved/);
        area.value='Draft';area.dispatch('input');publish({...view,description:'Remote'});assert.equal(area.value,'Draft');
        key('Escape');assert.equal(area.value,'Remote');
        area.value='**Safe** <img onerror=boom> [bad](javascript:alert)';area.dispatch('input');
        assert.ok(!walk(section).some(n=>n.tagName==='IMG'));
        assert.ok(!walk(section).some(n=>n.tagName==='A'));
        refuse=true;defer=true;key('Enter',{metaKey:true});await settle();
        const flushed=panel.flushPending();resolveWrite();assert.equal(await flushed,true);
        await settle();assert.match(section.textContent,/Not saved: server refusal/);
        assert.equal(area.value,'**Safe** <img onerror=boom> [bad](javascript:alert)');
        refuse=false;defer=false;key('Enter',{ctrlKey:true});await settle();
        assert.ok(!section.textContent.includes('Not saved'));
        defer=true;area.value='Saved new';area.dispatch('input');key('Enter',{ctrlKey:true});await settle();
        button(section,'Cancel').dispatch('click');publish({...view,description:'Saved new'});resolveWrite();await settle();
        assert.equal(area.value,'Saved new');assert.equal(button(section,'Save description').disabled,true);
    }
    if(SCENARIO==='grouping'){
        // Header: search, then Manage. The Editor group comes first (Phase 4).
        assert.ok(walk(container.children[0]).some(n=>n.dataset.controlsManageToggle==='true'));
        assert.ok(walk(container.children[0]).some(n=>n.dataset.controlsSearch==='true'));
        const editor=container.children.find(n=>n.dataset.controlsGroup==='editor');assert.equal(editor.dataset.controlsGroup,'editor');assert.equal(editor.children[1].hidden,false);
        // A group named Advanced is an ordinary group now: open like the others.
        assert.equal(group('Advanced').children[1].hidden,false);
        const items=container.children.find(n=>n.dataset.panelDropGroup==='').children.filter(n=>n.dataset.controlTarget);assert.equal(items[0].dataset.controlTarget,'note:intro:text');assert.equal(items[1].dataset.controlTarget,'binding:steps:value');
        // Editor-section bindings sit in the Editor group after your settings, without a drag handle.
        const adv=byTarget('binding:adv:value');assert.ok(editor.contains(adv));
        const editorRows=walk(editor.children[1]).filter(n=>n.dataset.controlsMirror||n.dataset.controlTarget).map(n=>n.dataset.controlsMirror||n.dataset.controlTarget);
        assert.deepEqual(editorRows,['render.takePlacementMuted','render.linkedTakePlacement','batchRender.maxFramesPerChunk','binding:adv:value']);
        assert.ok(!walk(adv).some(n=>n.dataset.panelDragHandle==='true'));assert.equal(input('binding:adv:value').checked,false);
        input('binding:adv:value').checked=true;input('binding:adv:value').dispatch('change');await settle();
        assert.deepEqual(calls,[{'binding:adv:value':true}]);
        const muted=walk(mirror('render.takePlacementMuted')).find(n=>n.tagName==='INPUT');muted.checked=true;muted.dispatch('change');
        assert.equal(settings.render.takePlacementMuted,true);assert.deepEqual(settingWrites,[['render.takePlacementMuted',true]]);
        // Collapsing a group is remembered under a prefixed key; the toggle is a real button.
        const header=group('Advanced').children[0];header.dispatch('click');assert.equal(group('Advanced').children[1].hidden,true);
        assert.equal(group('Advanced').children[1].style.display,'none');
        assert.equal(walk(header).find(n=>n.tagName==='BUTTON').attributes['aria-expanded'],'false');
    }
    if(SCENARIO==='editor-focus-refresh'){
        const el=walk(mirror('batchRender.maxFramesPerChunk')).find(n=>n.tagName==='INPUT');
        el.focus();el.value='123';panel.render();
        assert.equal(document.activeElement,el);assert.equal(el.value,'123');
        el.blur();
        publish({...view,bindings:[...view.bindings,{source:'binding',binding_id:'filename',label:'File name',kind:'STRING',value:'saved',status:'ok',section:'editor'}]});
        const filename=input('binding:filename:value');filename.focus();filename.value='draft';
        publish({...view});
        assert.equal(document.activeElement,filename);assert.equal(filename.value,'draft');
        assert.deepEqual(calls,[]);
    }
    if(SCENARIO==='batch-mirror'){
        const el=walk(mirror('batchRender.maxFramesPerChunk')).find(n=>n.tagName==='INPUT');assert.equal(el.value,'121');
        el.focus();el.value='97';el.dispatch('change');assert.deepEqual(settingWrites.at(-1),['batchRender.maxFramesPerChunk','97']);
        el.blur();panel.render();assert.equal(el.value,'97');
        // A refused value warns and shows the stored one again.
        refuseSetting=true;el.focus();el.value='';el.dispatch('change');el.blur();
        assert.equal(notices.at(-1),'setting refused');assert.equal(el.value,'97');
    }
    if(SCENARIO==='search'){
        const box=walk(container).find(n=>n.dataset.controlsSearch==='true');box.focus();box.value='see';box.dispatch('input');
        assert.equal(byTarget('binding:steps:value').hidden,true);assert.equal(byTarget('binding:seed:value').hidden,false);
        // Notes are not searched; a group with a match opens even if collapsed.
        assert.equal(byTarget('note:intro:text').hidden,true);
        group('Advanced').children[0].dispatch('click');assert.equal(group('Advanced').children[1].hidden,false);
        // Search matches group names and choices too, and your own settings.
        box.value='batch';box.dispatch('input');assert.equal(mirror('batchRender.maxFramesPerChunk').hidden,false);assert.equal(mirror('render.takePlacementMuted').hidden,true);
        box.value='zzz';box.dispatch('input');assert.match(container.textContent,/No settings match “zzz”/);
        const e=key('Escape');assert.ok(e.prevented);assert.equal(box.value,'');assert.equal(byTarget('binding:steps:value').hidden,false);
    }
    if(SCENARIO==='advanced'){
        host.capabilities={hide:true,removeAuthored:false};
        publish({...view,bindings:view.bindings.map(d=>d.binding_id==='steps'?{...d,advanced:true,overridden:true}:d)});
        const line=walk(container).find(n=>n.dataset.controlsAdvancedLine==='true');
        assert.equal(byTarget('binding:steps:value').hidden,true);assert.equal(line.hidden,false);
        // An inline display outranks `hidden`, so hiding also sets display (live-found, 4A).
        assert.equal(byTarget('binding:steps:value').style.display,'none');
        assert.match(line.textContent,/1 advanced hidden · 1 changed/);
        // Search still finds a hidden Advanced setting, tagged.
        const box=walk(container).find(n=>n.dataset.controlsSearch==='true');box.value='steps';box.dispatch('input');
        assert.equal(byTarget('binding:steps:value').hidden,false);assert.match(byTarget('binding:steps:value').textContent,/Adv/);
        box.value='';box.dispatch('input');
        line.dispatch('click');assert.deepEqual(settingWrites.at(-1),['controls.showAdvanced',true]);panel.render();
        assert.equal(byTarget('binding:steps:value').hidden,false);assert.match(line.textContent,/Hide advanced settings/);
        // Manage shows everything and flags Advanced with a toggle.
        settings.controls.showAdvanced=false;toggleManage();assert.equal(byTarget('binding:steps:value').hidden,false);assert.equal(line.hidden,true);
        const flag=walk(byTarget('binding:steps:value')).find(n=>n.dataset.controlsAdvancedToggle==='steps');assert.equal(flag.attributes['aria-pressed'],'true');
        flag.dispatch('click');await settle();assert.deepEqual(calls.at(-1),{'binding:steps:advanced':false});
    }
    if(SCENARIO==='advanced-refusal'){
        // A refused Advanced save keeps its reason on the row, even when the
        // row is not displayed while it saves.
        toggleManage();refuse=true;defer=true;
        walk(byTarget('binding:steps:value')).find(n=>n.dataset.controlsAdvancedToggle==='steps').dispatch('click');await settle();
        toggleManage();resolveWrite();await settle();
        assert.match(byTarget('binding:steps:value').textContent,/Not saved: server refusal/);assert.equal(notices.length,1);
        assert.equal(byTarget('binding:steps:value').hidden,false); // the refusal reverted it to not Advanced
    }
    if(SCENARIO==='hidden-pending'){
        // Search hides a row whose save is still in flight; its refusal still lands on it.
        refuse=true;defer=true;const el=input('binding:steps:value');el.focus();el.value='33';key('Enter');await settle();
        const box=walk(container).find(n=>n.dataset.controlsSearch==='true');box.value='seed';box.dispatch('input');
        assert.equal(byTarget('binding:steps:value').hidden,true);resolveWrite();await settle();
        assert.match(byTarget('binding:steps:value').textContent,/Not saved: server refusal/);
    }
    if(SCENARIO==='cards-scale'){
        settings.controls.view='cards';settings.layout.scaleControls=1.2;panel.render();
        assert.equal(container.style.zoom,'1.2');assert.match(byTarget('binding:steps:value').style.cssText,/border-radius/);
        assert.match(container.children.find(n=>n.dataset.panelDropGroup==='').style.cssText,/grid-template-columns/);
        assert.match(group('Advanced').style.cssText,/grid-column:1 \/ -1/);
        // A note makes its card span the row; Manage always uses rows.
        assert.match(byTarget('binding:steps:value').style.cssText,/grid-column:1 \/ -1/);
        toggleManage();assert.doesNotMatch(byTarget('binding:seed:value').style.cssText,/border-radius/);
        settings.layout.scaleControls=1;panel.render();assert.equal(container.style.zoom,'');
    }
    if(SCENARIO==='group-note'){
        toggleManage();const section=walk(container).find(n=>n.dataset.controlsManage==='true');
        walk(group('Advanced').children[0]).find(n=>n.dataset.groupAction==='note').dispatch('click');
        const area=walk(section).find(n=>n.tagName==='TEXTAREA'&&!n.dataset.workflowDescription&&!n.dataset.presetDescription);area.value='Seeds';key('Enter',{ctrlKey:true});await settle();
        const [target,value]=Object.entries(calls.at(-1))[0];assert.match(target,/^note:[A-Za-z0-9_-]{12}:create$/);assert.deepEqual(value,{text:'Seeds',group:'Advanced'});
    }
    if(SCENARIO==='focus'){
        const el=input('binding:steps:value');el.focus();el.value='35';
        publish({...view,bindings:view.bindings.map(d=>d.binding_id==='steps'?{...d,value:'24',group:'Changed'}:d)});
        assert.equal(input('binding:steps:value'),el);assert.equal(document.activeElement,el);assert.equal(el.value,'35');
        const event=key('Enter');assert.ok(event.prevented);await settle();assert.deepEqual(calls,[{'binding:steps:value':'35'}]);
    }
    if(SCENARIO==='focus-after-change'){
        const el=input('binding:steps:value');el.focus();el.value='30';el.dispatch('change');await settle();el.value='31';
        publish({...view,bindings:view.bindings.map(d=>d.binding_id==='steps'?{...d,value:'30'}:d)});
        assert.equal(document.activeElement,el);assert.equal(el.value,'31');
    }
    if(SCENARIO==='focus-after-refusal'){
        defer=true;refuse=true;const el=input('binding:steps:value');el.focus();el.value='30';el.dispatch('change');await settle();
        el.value='31';resolveWrite();await settle();assert.equal(document.activeElement,el);assert.equal(el.value,'31');
    }
    if(SCENARIO==='metadata'){
        publish({...view,bindings:view.bindings.map(d=>d.binding_id==='steps'?{...d,label:'Renamed steps',min:'4',max:'40'}:d)});
        let el=input('binding:steps:value');assert.equal(el.min,'4');assert.equal(el.attributes['aria-label'],'Renamed steps');
        publish({...view,bindings:view.bindings.map(d=>d.binding_id==='steps'?{...d,min:null,max:null}:d)});
        assert.equal(el.min,'');assert.equal(el.max,'');
    }
    if(SCENARIO==='cancel'||SCENARIO==='note-save'||SCENARIO==='ime'){
        note('note:intro:text').dispatch('dblclick');const area=walk(note('note:intro:text')).find(n=>n.tagName==='TEXTAREA');area.value='# Edited';
        if(SCENARIO==='ime'){area.dispatch('compositionstart');const e=key('Escape',{isComposing:true});assert.equal(e.prevented,false);assert.ok(e.stopped);assert.equal(calls.length,0);area.dispatch('compositionend');}
        const e=key(SCENARIO==='cancel'?'Escape':'Enter',{ctrlKey:SCENARIO!=='cancel'});assert.ok(e.prevented);await settle();
        if(SCENARIO==='cancel'){assert.equal(calls.length,0);assert.equal(note('note:intro:text').textContent,'Intro');}
        else assert.deepEqual(calls,[{'note:intro:text':'# Edited'}]);
    }
    if(SCENARIO==='refusal'||SCENARIO==='timeout'||SCENARIO==='initial-error'){
        refuse=SCENARIO!=='timeout';defer=SCENARIO==='timeout';const el=input('binding:steps:value');el.focus();el.value='33';
        const flush=panel.flushPending();await settle();if(defer)timeoutCalls.at(-1)();assert.equal(await flush,false);assert.equal(el.value,'20');assert.equal(notices.length,1);resolveWrite?.();
    }
    if(SCENARIO==='flush'){
        const el=input('binding:steps:value');el.focus();el.value='32';assert.equal(await panel.flushPending(),true);assert.deepEqual(calls,[{'binding:steps:value':'32'}]);
    }
    if(SCENARIO==='mode'){
        const row=byTarget('binding:seed:value'),mode=walk(row).find(n=>n.tagName==='SELECT');mode.value='increment';mode.dispatch('change');await settle();
        assert.deepEqual(calls,[{'binding:seed:seed_mode':'increment'}]);assert.equal(input('binding:seed:value').value,'9');
    }
    if(SCENARIO==='cross-target-refusal'){
        let rejectSeed;
        host.applyChanges=async changes=>{
            calls.push(changes);
            if('binding:seed:value' in changes){await new Promise(r=>rejectSeed=r);return{ok:false,rejected:[{target:'binding:seed:value',reason:'invalid seed'}]};}
            publish({...view,bindings:view.bindings.map(d=>d.binding_id==='seed'?{...d,seed_mode:'increment'}:d)});return{ok:true,rejected:[]};
        };
        const el=input('binding:seed:value');el.focus();el.value='bad';el.dispatch('change');await settle();
        const mode=walk(byTarget('binding:seed:value')).find(n=>n.tagName==='SELECT');mode.focus();mode.value='increment';mode.dispatch('change');await settle();
        rejectSeed();await settle();assert.equal(el.value,'9');assert.equal(notices[0],'invalid seed');assert.equal(mode.value,'increment');
        // The row keeps the reason visible; the settled refusal does not block a later run.
        assert.match(byTarget('binding:seed:value').textContent,/Not saved: invalid seed/);assert.equal(await panel.flushPending(),true);
    }
    if(SCENARIO==='refusal-then-generate'){
        refuse=true;const el=input('binding:steps:value');el.focus();el.value='99999';key('Enter');await settle();
        assert.equal(el.value,'20');assert.equal(notices.length,1);assert.match(byTarget('binding:steps:value').textContent,/Not saved: server refusal/);
        // An edit of another row that saves does not inherit the old refusal.
        refuse=false;const seed=input('binding:seed:value');seed.focus();seed.value='11';assert.equal(await panel.flushPending(),true);
        assert.equal(await panel.flushPending(),true);
        // Fresh stored values retire the warning.
        publish({...view});await settle();assert.doesNotMatch(byTarget('binding:steps:value').textContent,/Not saved/);
    }
    if(SCENARIO==='late-success'){
        defer=true;const el=input('binding:steps:value');el.focus();el.value='33';key('Enter');await settle();
        timeoutCalls.at(-1)();await settle();assert.match(byTarget('binding:steps:value').textContent,/Not saved: The change was not confirmed/);
        // The save lands after its warning: it is saved, and nothing blocks a run.
        resolveWrite();await settle();assert.doesNotMatch(byTarget('binding:steps:value').textContent,/Not saved/);
        assert.equal(await panel.flushPending(),true);
    }
    if(SCENARIO==='note-refusal'){
        note('note:intro:text').dispatch('dblclick');walk(note('note:intro:text')).find(n=>n.tagName==='TEXTAREA').value='# Edited';
        refuse=true;defer=true;const flush=panel.flushPending();await settle();resolveWrite();
        // Notes never reach a run, so a refused note save warns but does not refuse it.
        assert.equal(await flush,true);assert.equal(notices.length,1);
    }
    if(SCENARIO==='note-then-value'){
        // A run save in flight when Generate is asked must decide it, even if it
        // settles while the open note is still saving.
        let refuseSteps,saveNote;
        host.applyChanges=async changes=>{calls.push(changes);
            if('binding:steps:value' in changes){await new Promise(r=>refuseSteps=r);return{ok:false,rejected:[{reason:'steps refused'}]};}
            await new Promise(r=>saveNote=r);return{ok:true,rejected:[]};};
        const steps=input('binding:steps:value');steps.focus();steps.value='33';key('Enter');await settle();
        note('note:intro:text').dispatch('dblclick');walk(note('note:intro:text')).find(n=>n.tagName==='TEXTAREA').value='# Edited';
        const flush=panel.flushPending();await settle();refuseSteps();await settle();saveNote();
        assert.equal(await flush,false);
    }
    if(SCENARIO==='problem-statuses'){
        publish({...view,bindings:[{source:'binding',binding_id:'gone',label:'Gone',kind:null,value:null,status:'missing',reason:'This setting is no longer in the workflow.',order:0},
            {source:'binding',binding_id:'dup',label:'Dup',kind:null,value:null,status:'duplicate',reason:'Two nodes carry this Control.',order:1},
            {source:'binding',binding_id:'run',label:'Folder',kind:'STRING',value:'out',status:'ok',checked_at_run:true,order:2},
            {source:'binding',binding_id:'reset',label:'Steps',kind:'INT',value:'20',status:'ok',reset:true,reason:"Reset to the workflow's value: this setting changed since its value was saved.",order:3},
            {source:'binding',binding_id:'bad'},{source:'binding',label:'no id'}],notes:[]});
        for(const id of ['gone','dup'])assert.ok(walk(byTarget(`binding:${id}:value`)).find(n=>n.tagName==='INPUT').disabled);
        assert.match(byTarget('binding:gone:value').textContent,/no longer in the workflow/);
        assert.match(byTarget('binding:run:value').textContent,/Checked by the node when the run starts/);
        assert.ok(!input('binding:run:value').disabled);
        assert.match(byTarget('binding:reset:value').textContent,/Reset to the workflow's value/);
        // A row without an id is never rendered: it has no target to save.
        assert.equal(walk(container).filter(n=>n.dataset.controlTarget).length,5);
        // Presentation still saves for a binding that is off.
        note('binding:gone:note').dispatch('dblclick');walk(note('binding:gone:note')).find(n=>n.tagName==='TEXTAREA').value='Why it is off';
        key('Enter',{ctrlKey:true});await settle();assert.deepEqual(calls,[{'binding:gone:note':'Why it is off'}]);
    }
    if(SCENARIO==='loading'){
        publish({state:'loading',bindings:[],notes:[],error:null});assert.ok(container.textContent.includes('Loading controls'));
        assert.ok(!container.textContent.includes('No Controls yet'));
        publish({state:'ready',bindings:[],notes:[],error:null});assert.ok(container.textContent.includes('No Controls yet. Choose Manage'));
        publish({state:'ready',bindings:[],notes:[],relay:{status:'error',error:'The workflow has more than one Sonder Control Relay. Keep one.'},error:null});
        assert.ok(container.textContent.includes('more than one Sonder Control Relay'));
        host.emptyMessage='This workflow exposes no Controls.';
        publish({state:'ready',bindings:[],notes:[],error:null});assert.ok(container.textContent.includes('This workflow exposes no Controls.'));
        assert.ok(!container.textContent.includes('No Controls yet'));
    }
    if(SCENARIO==='normal-view'){
        // Normal view: values and rendered notes only (SR-004).
        const grip=walk(byTarget('binding:steps:value')).find(n=>n.dataset.panelDragHandle==='true');assert.equal(grip.hidden,true);
        note('note:intro:text').dispatch('dblclick');assert.ok(!walk(note('note:intro:text')).some(n=>n.tagName==='TEXTAREA'));
        assert.ok(!container.textContent.includes('Double-click'));
        assert.equal(walk(byTarget('binding:steps:value')).find(n=>n.dataset.controlsRowActions).style.display,'none');
        publish({...view,bindings:view.bindings.map(d=>d.binding_id==='seed'?{...d,hidden:true}:d)});
        assert.equal(byTarget('binding:seed:value').hidden,true);
        toggleManage();assert.ok(byTarget('binding:seed:value'));assert.match(byTarget('binding:seed:value').textContent,/Hidden/);
        assert.equal(walk(byTarget('binding:steps:value')).find(n=>n.dataset.panelDragHandle==='true').hidden,false);
    }
    if(SCENARIO==='manage-flush'||SCENARIO==='hide-flush'){
        host.capabilities={hide:true,removeAuthored:false};
        publish({...view,candidates:[{path:'6',input:'cfg',node_title:'Sampler',kind:'FLOAT'}]});
        toggleManage();defer=true;refuse=true;
        if(SCENARIO==='manage-flush'){
            const section=walk(container).find(n=>n.dataset.controlsManage==='true');
            button(section,'Add').dispatch('click');
        }else button(byTarget('binding:steps:value'),'Hide').dispatch('click');
        let done=false;const flushed=panel.flushPending().then(value=>{done=true;return value;});
        await settle();assert.equal(done,false);
        resolveWrite();assert.equal(await flushed,false);
        assert.equal(await panel.flushPending(),true); // a settled refusal no longer blocks
    }
    if(SCENARIO==='manage'||SCENARIO==='manage-sdk'){
        const manageCalls=[];host.setManage=(on,filter)=>manageCalls.push([on,filter]);
        if(SCENARIO==='manage-sdk')host.capabilities={hide:true,removeAuthored:false};
        const candidates=[{path:'2',input:'mark_queue_complete',label:'Advance queue after take',node_title:'Save',kind:'BOOLEAN',recommended:true,section:'editor'},
            {path:'2',input:'place_audio_on_timeline',label:'Place take audio on timeline',node_title:'Save',kind:'BOOLEAN',recommended:true,section:'editor'},
            {path:'6',input:'cfg',label:'Sampler · cfg',node_title:'Sampler',kind:'FLOAT',recommended:false,section:'workflow'}];
        publish({...view,candidates:[],suggestions:[{binding_id:'old',label:'Sampler · denoise',targets:[]}],
            bindings:[...view.bindings,{source:'binding',binding_id:'lost',label:'Lost',kind:null,status:'missing',reason:'gone',order:9,origin:SCENARIO==='manage-sdk'?'local':'authored'}]});
        toggleManage();assert.deepEqual(manageCalls,[[true,'']]);
        const section=walk(container).find(n=>n.dataset.controlsManage==='true');assert.ok(section);
        publish({...view,candidates,suggestions:[{binding_id:'old',label:'Sampler · denoise',targets:[]}],candidates_truncated:true,
            bindings:[...view.bindings,{source:'binding',binding_id:'lost',label:'Lost',kind:null,status:'missing',reason:'gone',order:9,origin:SCENARIO==='manage-sdk'?'local':'authored'}]});
        assert.match(section.textContent,/Recommended/);assert.match(section.textContent,/Narrow the filter/);
        // Add settings stays folded into one bar, with its counts, until opened.
        const bar=walk(section).find(n=>n.dataset.controlsAddSettings==='true');assert.equal(bar.attributes['aria-expanded'],'false');
        assert.match(bar.textContent,/3\+ available · 2 recommended/);bar.dispatch('click');assert.equal(bar.attributes['aria-expanded'],'true');
        // The Sonder App lists top-level settings only, and says so.
        assert.equal(/Settings inside subgraphs are exposed from ComfyUI/.test(section.textContent),SCENARIO==='manage-sdk');
        // Nothing greyed out: every listed candidate has an enabled action.
        for(const row of walk(section).filter(n=>n.dataset.candidate))assert.ok(!walk(row).find(n=>n.tagName==='BUTTON').disabled);
        button(walk(section).find(n=>n.dataset.candidate==='6/cfg'),'Add').dispatch('click');await settle();
        const [exposeTarget,expose]=Object.entries(calls.at(-1))[0];
        // No section is sent: the server derives it from the node's class.
        assert.match(exposeTarget,/^binding:[A-Za-z0-9_-]{12}:expose$/);assert.deepEqual(expose,{candidate:{path:'6',input:'cfg'}});
        button(section,'Add all recommended').dispatch('click');await settle();
        assert.deepEqual(Object.values(calls.at(-1)).map(v=>v.candidate.input),['mark_queue_complete','place_audio_on_timeline']);
        button(section,'Restore').dispatch('click');await settle();assert.deepEqual(calls.at(-1),{'binding:old:expose':{}});
        // Rename in place.
        const steps=byTarget('binding:steps:value');button(steps,'Rename').dispatch('click');
        const rename=walk(steps).find(n=>n.tagName==='INPUT'&&n.attributes['aria-label']==='Control name');rename.value='Sampling steps';
        key('Enter');await settle();assert.deepEqual(calls.at(-1),{'binding:steps:label':'Sampling steps'});
        assert.match(byTarget('binding:steps:value').textContent,/Sampling steps/);
        // Rebind a missing binding, keeping its id.
        button(byTarget('binding:lost:value'),'Rebind').dispatch('click');assert.match(section.textContent,/Choose a new setting for “Lost”/);
        button(walk(section).find(n=>n.dataset.candidate==='6/cfg'),'Use').dispatch('click');await settle();
        assert.deepEqual(calls.at(-1),{'binding:lost:rebind':{candidate:{path:'6',input:'cfg'}}});
        assert.doesNotMatch(section.textContent,/Choose a new setting/);
        if(SCENARIO==='manage'){
            button(byTarget('binding:steps:value'),'Remove').dispatch('click');await settle();assert.deepEqual(calls.at(-1),{'binding:steps:remove':true});
            assert.ok(!button(byTarget('binding:steps:value'),'Hide'));
        }else{
            assert.ok(!button(byTarget('binding:steps:value'),'Remove'));assert.match(byTarget('binding:lost:value').textContent,/App only/);
            button(byTarget('binding:steps:value'),'Hide').dispatch('click');await settle();assert.deepEqual(calls.at(-1),{'binding:steps:hide':true});
            button(byTarget('binding:lost:value'),'Remove').dispatch('click');await settle();assert.deepEqual(calls.at(-1),{'binding:lost:remove':true});
        }
        // A new standalone note.
        button(section,'+ Note').dispatch('click');const area=walk(section).find(n=>n.tagName==='TEXTAREA'&&!n.dataset.workflowDescription&&!n.dataset.presetDescription);area.value='**Tip**';
        key('Enter',{ctrlKey:true});await settle();const [noteTarget,noteValue]=Object.entries(calls.at(-1))[0];
        assert.match(noteTarget,/^note:[A-Za-z0-9_-]{12}:create$/);assert.deepEqual(noteValue,{text:'**Tip**',group:''});
        // The filter reaches the host, debounced; Escape clears it.
        const filter=walk(section).find(n=>n.attributes['aria-label']==='Filter settings to add');filter.focus();filter.value='cfg';filter.dispatch('input');
        await new Promise(r=>nativeTimeout(r,300));assert.deepEqual(manageCalls.at(-1),[true,'cfg']);
        toggleManage();assert.deepEqual(manageCalls.at(-1),[false,'']);assert.ok(!walk(container).some(n=>n.dataset.controlsManage==='true'));
    }
    if(SCENARIO==='removed-failure'||SCENARIO==='removed-pending'){
        refuse=true;defer=SCENARIO==='removed-pending';const seed=input('binding:seed:value');seed.focus();seed.value='bad';key('Enter');await settle();
        publish({...view,bindings:view.bindings.filter(d=>d.binding_id!=='seed')});
        if(defer){resolveWrite();await settle();}refuse=false;defer=false;
        const steps=input('binding:steps:value');steps.focus();steps.value='31';assert.equal(await panel.flushPending(),true);
    }
    if(SCENARIO==='truncated'){
        publish({...view,notes:view.notes.map(d=>({...d,note_truncated:true}))});note('note:intro:text').dispatch('dblclick');assert.ok(!walk(note('note:intro:text')).some(n=>n.tagName==='TEXTAREA'));assert.equal(calls.length,0);
    }
    if(['layout','layout-refusal','layout-stale','layout-editor'].includes(SCENARIO)){
        const row=byTarget('binding:steps:value'),grip=walk(row).find(n=>n.dataset.panelDragHandle==='true');
        const advanced=group('Advanced').children[0];
        const data={};const transfer={setData(k,v){data[k]=v;},getData(k){return data[k];}};
        if(SCENARIO==='layout-editor')input('binding:steps:value').focus();
        container.dispatch('dragstart',{target:grip,dataTransfer:transfer});
        if(SCENARIO==='layout-stale')publish({...view,notes:view.notes.map(d=>({...d,order:8}))});
        refuse=SCENARIO==='layout-refusal';container.dispatch('drop',{target:advanced,dataTransfer:transfer});await settle();
        if(SCENARIO==='layout-stale'||SCENARIO==='layout-editor'){assert.equal(calls.length,0);assert.equal(notices.length,1);}
        else{assert.equal(calls.length,1);assert.equal(calls[0]['binding:steps:group'],'Advanced');assert.ok(Number.isInteger(calls[0]['binding:steps:order']));
            // A refused arrangement leaves the row in place and never blocks a run.
            if(refuse){assert.equal(row.parentElement.dataset.panelDropGroup,'');assert.equal(notices.length,1);assert.equal(await panel.flushPending(),true);}}
    }
    if(SCENARIO==='combo'||SCENARIO==='combo-capped'||SCENARIO==='combo-ime'||SCENARIO==='combo-query'||SCENARIO==='combo-capped-filter'){
        const options=Array.from({length:30},(_,i)=>'option-'+i);
        publish({...view,bindings:[{source:'binding',binding_id:'combo',kind:'COMBO',label:'Long options',options,options_truncated:SCENARIO.startsWith('combo-capped'),value:'option-0',status:'ok',order:0}]});
        const el=input('binding:combo:value');el.focus();el.value=SCENARIO==='combo-capped'?'option-999':SCENARIO==='combo-capped-filter'?'27':'option-27';el.dispatch('input');
        if(SCENARIO==='combo-ime'){el.dispatch('compositionstart');const e=key('Enter',{isComposing:true});assert.equal(e.prevented,false);assert.equal(calls.length,0);el.dispatch('compositionend');}
        if(SCENARIO==='combo-query'){
            el.value='option-2';el.dispatch('input');el.dispatch('change');assert.equal(await panel.flushPending(),true);assert.equal(calls.length,0);el.blur();assert.equal(el.value,'option-0');
        }else{
        const list=walk(byTarget('binding:combo:value')).find(n=>n.tagName==='SELECT');
        if(SCENARIO!=='combo-capped')assert.deepEqual(list.options.map(o=>o.value),['option-27']);
        key('Enter');await settle();assert.deepEqual(calls,[{'binding:combo:value':SCENARIO==='combo-capped'?'option-999':'option-27'}]);
        el.focus();el.value='draft';el.dispatch('input');key('Escape');assert.equal(el.value,SCENARIO==='combo-capped'?'option-999':'option-27');assert.equal(calls.length,1);
        }
    }
    if(SCENARIO==='combo-capped-exact'||SCENARIO==='combo-capped-current'||SCENARIO==='combo-capped-keyboard'){
        const value=SCENARIO==='combo-capped-current'?'model-27.ckpt':'model-0.ckpt';
        publish({...view,bindings:[{source:'binding',binding_id:'combo',kind:'COMBO',label:'Models',options:['model-27.ckpt.backup','model-0.ckpt'],options_truncated:true,value,status:'ok',order:0}]});
        const el=input('binding:combo:value');el.focus();
        if(SCENARIO==='combo-capped-current'){key('Enter');await settle();assert.equal(calls.length,0);assert.equal(el.value,'model-27.ckpt');}
        else{el.value='model-27.ckpt';el.dispatch('input');
            const button=walk(byTarget('binding:combo:value')).find(n=>n.tagName==='BUTTON'&&n.textContent==='Use typed value');
            if(SCENARIO==='combo-capped-keyboard'){button.focus();assert.equal(el.value,'model-27.ckpt');key('Enter');}else{button.dispatch('mousedown');button.dispatch('click');}await settle();assert.deepEqual(calls,[{'binding:combo:value':'model-27.ckpt'}]);}
    }
    if(SCENARIO==='combo-focus-return'){
        publish({...view,bindings:[{source:'binding',binding_id:'combo',kind:'COMBO',label:'Models',options:['model-27.ckpt','model-0.ckpt'],options_truncated:true,value:'model-0.ckpt',status:'ok',order:0}]});
        const el=input('binding:combo:value');el.focus();el.value='27';el.dispatch('input');
        const button=walk(byTarget('binding:combo:value')).find(n=>n.tagName==='BUTTON');button.focus();el.focus();
        const list=walk(byTarget('binding:combo:value')).find(n=>n.tagName==='SELECT');assert.deepEqual(list.options.map(o=>o.value),['model-27.ckpt']);
        key('Enter');await settle();assert.deepEqual(calls,[{'binding:combo:value':'model-27.ckpt'}]);
    }
    if(SCENARIO==='combo-select-focus'){
        publish({...view,bindings:[{source:'binding',binding_id:'combo',kind:'COMBO',label:'Models',options:Array.from({length:30},(_,i)=>'model-'+i),value:'model-0',status:'ok',order:0}]});
        const el=input('binding:combo:value');el.focus();const list=walk(byTarget('binding:combo:value')).find(n=>n.tagName==='SELECT');
        // Browsers blur a focused native list synchronously when it is hidden.
        let hidden=false;Object.defineProperty(list,'hidden',{get:()=>hidden,set(v){hidden=v;if(v&&document.activeElement===list)list.blur();}});
        list.focus();key('ArrowDown');key('Enter');await settle();assert.deepEqual(calls,[{'binding:combo:value':'model-1'}]);
    }
    if(SCENARIO==='group-editor-name'){
        publish({...view,groups:{editor:{order:1},Empty:{order:2}},group_order:['editor','Empty','Advanced']});
        toggleManage();
        const fixed=walk(container).find(n=>n.dataset.controlsSection==='editor');
        const named=walk(container).find(n=>n.dataset.controlsGroup==='editor'&&n.dataset.controlsSection==='workflow');
        assert.ok(fixed&&named&&fixed!==named);
        const grip=walk(group('Empty')).find(n=>n.dataset.panelGroupHandle==='Empty');
        const values=new Map(), transfer={setData:(k,v)=>values.set(k,v),getData:k=>values.get(k)};
        container.dispatch('dragstart',{target:grip,dataTransfer:transfer});
        container.dispatch('drop',{target:fixed.children[0],dataTransfer:transfer,clientY:0});await settle();
        assert.equal(calls.length,0);
        container.dispatch('dragstart',{target:grip,dataTransfer:transfer});
        container.dispatch('drop',{target:named.children[0],dataTransfer:transfer,clientY:0});await settle();
        assert.deepEqual(calls.at(-1),{'group:Empty:move':{before:'editor'}});
    }
    if(SCENARIO==='groups-manage'||SCENARIO==='groups-cap'){
        // An explicit empty group, and Advanced holding seed (Phase 4B).
        publish({...view,groups:{Empty:{order:1}},group_order:['Empty','Advanced']});
        const menuRow=label=>walk(document.body).find(n=>n.attributes?.role==='menuitem'&&n.textContent.startsWith(label));
        const openMenu=name=>walk(group(name).children[0]).find(n=>n.dataset.groupAction==='menu').dispatch('click');
        if(SCENARIO==='groups-cap'){
            const many=Array.from({length:65},(_,i)=>({source:'binding',binding_id:'m'+i,label:'M'+i,kind:'INT',value:'1',status:'ok',order:i,group:'Big',section:'workflow'}));
            publish({...view,bindings:[...view.bindings,...many],group_order:['Advanced','Big']});
            toggleManage();openMenu('Big');menuRow('Mark all Advanced').dispatch('click');await settle();
            assert.equal(calls.length,0);assert.match(notices.at(-1),/more than 64 Controls/);
        }else{
        // Empty groups exist only to be filled: shown in Manage, never in normal view.
        assert.equal(group('Empty'),undefined);
        toggleManage();
        assert.ok(group('Empty'));assert.equal(group('Empty').hidden,false);
        const order=container.children.flatMap(c=>walk(c)).filter(n=>n.dataset.controlsGroup&&n.dataset.controlsGroup!=='editor').map(n=>n.dataset.controlsGroup);
        assert.deepEqual(order,['Empty','Advanced']);
        // Header: a drag grip and a menu; the Editor group has neither.
        assert.ok(walk(group('Advanced').children[0]).some(n=>n.dataset.panelGroupHandle==='Advanced'&&n.draggable));
        assert.ok(!walk(group('editor').children[0]).some(n=>n.dataset.groupAction==='menu'||n.dataset.panelGroupHandle));
        openMenu('Advanced');assert.equal(menuRow('Move down').attributes['aria-disabled'],'true');
        menuRow('Move up').dispatch('click');await settle();
        assert.deepEqual(calls.at(-1),{'group:Advanced:move':{before:'Empty'}});
        openMenu('Advanced');menuRow('Mark all Advanced').dispatch('click');await settle();
        assert.deepEqual(calls.at(-1),{'binding:seed:advanced':true});
        openMenu('Advanced');menuRow('Ungroup').dispatch('click');await settle();
        assert.deepEqual(calls.at(-1),{'group:Advanced:ungroup':true});
        // Rename in place: Enter saves, an invalid name warns, Escape cancels.
        openMenu('Advanced');menuRow('Rename').dispatch('click');
        let field=walk(group('Advanced')).find(n=>n.dataset.groupRename==='Advanced');
        assert.equal(document.activeElement,field);field.value='a:b';key('Enter');await settle();
        assert.equal(calls.length,3);assert.match(notices.at(-1),/colon/);
        openMenu('Advanced');menuRow('Rename').dispatch('click');
        field=walk(group('Advanced')).find(n=>n.dataset.groupRename==='Advanced');field.value='Look';key('Escape');await settle();
        assert.equal(calls.length,3);assert.ok(!walk(container).some(n=>n.dataset.groupRename));
        openMenu('Advanced');menuRow('Rename').dispatch('click');
        field=walk(group('Advanced')).find(n=>n.dataset.groupRename==='Advanced');field.value=' Look ';publish({...view});assert.equal(document.activeElement,field);assert.deepEqual(group('Empty').parentElement.children.map(c=>c.dataset.controlsGroup||c.dataset.controlTarget||c.textContent),['Main section','note:intro:text','binding:steps:value','Empty','Advanced']);key('Enter');await settle();
        assert.deepEqual(calls.at(-1),{'group:Advanced:rename':'Look'});
        // Move to group: an existing group, or a new one in the same batch.
        assert.ok(!walk(byTarget('binding:adv:value')).some(n=>n.dataset.controlsMoveGroup));
        const moveButton=()=>walk(byTarget('binding:steps:value')).find(n=>n.dataset.controlsMoveGroup==='binding:steps');
        moveButton().dispatch('click');assert.ok(menuRow('Empty'));assert.equal(menuRow('Ungrouped'),undefined);
        menuRow('Empty').dispatch('click');await settle();
        assert.deepEqual(calls.at(-1),{'binding:steps:group':'Empty','binding:steps:order':0});
        moveButton().dispatch('click');menuRow('New group').dispatch('click');
        const draft=walk(container).find(n=>n.dataset.controlsNewGroup==='true');assert.equal(document.activeElement,draft);
        draft.value='Fresh';key('Enter');await settle();
        assert.deepEqual(calls.at(-1),{'group:Fresh:create':{},'binding:steps:group':'Fresh','binding:steps:order':0});
        // + Group: an in-panel name field; an existing name is refused and the field stays.
        walk(container).find(n=>n.dataset.controlsAddGroup==='true').dispatch('click');
        const name=walk(container).find(n=>n.dataset.controlsNewGroup==='true');
        name.value='Empty';key('Enter');await settle();assert.match(notices.at(-1),/already exists/);
        assert.equal(walk(container).find(n=>n.dataset.controlsNewGroup==='true'),name);
        name.value='New';key('Enter');await settle();assert.deepEqual(calls.at(-1),{'group:New:create':{}});
        // Dragging a group header onto another reorders groups in one save.
        const data={};const transfer={setData(k,v){data[k]=v;},getData(k){return data[k];}};
        const grip=walk(group('Advanced').children[0]).find(n=>n.dataset.panelGroupHandle==='Advanced');
        container.dispatch('dragstart',{target:grip,dataTransfer:transfer});
        container.dispatch('drop',{target:group('Empty').children[0],dataTransfer:transfer,clientY:0});await settle();
        assert.deepEqual(calls.at(-1),{'group:Advanced:move':{before:'Empty'}});
        // Group saves never block a run.
        assert.equal(await panel.flushPending(),true);
        }
    }
    panel.destroy();assert.equal(listeners.size,0);assert.equal(container.children.length,0);
}
console.log('ok');
'''

@pytest.mark.parametrize('scenario',['restore-notice','restore-all','description','group-editor-name','editor-focus-refresh','normal-view','manage','manage-sdk','manage-flush','hide-flush','combo-select-focus','combo-focus-return','combo-capped-keyboard','combo-capped-exact','combo-capped-current','combo-capped-filter','combo-query','layout','layout-refusal','layout-stale','layout-editor','combo','combo-capped','combo-ime','markdown','grouping','focus','focus-after-change','focus-after-refusal','metadata','cancel','note-save','ime','refusal','timeout','initial-error','flush','mode','cross-target-refusal','removed-failure','removed-pending','truncated','refusal-then-generate','late-success','note-refusal','problem-statuses','loading','note-then-value','batch-mirror','search','advanced','advanced-refusal','hidden-pending','cards-scale','group-note','groups-manage','groups-cap'])
def test_controls_panel(scenario, tmp_path):
    node=shutil.which('node')
    if not node:pytest.skip('node required for Controls renderer boundary tests')
    script=SCRIPT.replace('MARKDOWN_URL',json.dumps((ROOT/'web/js/panel_markdown.js').as_uri())).replace('PANEL_URL',json.dumps((ROOT/'web/js/editor_controls_panel.js').as_uri())).replace('SCENARIO',json.dumps(scenario))
    # A file, not `node -e`: the script is longer than a Windows command line.
    path=tmp_path/'panel_test.mjs';path.write_text(script,encoding='utf-8')
    result=subprocess.run([node,str(path)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='ok'
