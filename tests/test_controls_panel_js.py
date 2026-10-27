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
    getAttribute(k){return this.attributes[k]??null;}
    removeAttribute(k){if(k==='title')delete this.title;delete this.attributes[k];}
    closest(sel){for(let n=this;n;n=n.parentElement){for(const part of sel.split(',').map(x=>x.trim())){if(part==='[data-control-target]'?n.dataset?.controlTarget!==undefined:n.tagName===part.toUpperCase())return n;}}return null;}
    addEventListener(k,f){(this.listeners[k]||=[]).push(f);}
    removeEventListener(k,f){this.listeners[k]=(this.listeners[k]||[]).filter(x=>x!==f);}
    dispatch(k,e={}){for(const f of this.listeners[k]||[])f({target:this,preventDefault(){},...e});}
    contains(n){return n===this||this.children.some(c=>c.contains?.(n));}
    focus(){if(document.activeElement!==this){document.activeElement?.blur?.(this);document.activeElement=this;this.dispatch('focus');for(let n=this.parentElement;n;n=n.parentElement)n.dispatch('focusin',{target:this});}}
    blur(next=document.body){if(document.activeElement===this){const parents=[];for(let n=this.parentElement;n;n=n.parentElement)parents.push(n);document.activeElement=document.body;this.dispatch('blur',{relatedTarget:next});for(const p of parents)p.dispatch('focusout',{target:this,relatedTarget:next});}}
}
const events={};globalThis.window={innerWidth:1366,innerHeight:900,addEventListener(k,f){(events[k]||=[]).push(f);},removeEventListener(k,f){events[k]=(events[k]||[]).filter(x=>x!==f);}};
globalThis.requestAnimationFrame=f=>f();
// Collapsed notes are remembered: that scenario reads its own writes back.
const noteStore=new Map();
globalThis.localStorage={getItem:k=>SCENARIO==='note-titles'?noteStore.get(k)??null:null,setItem(k,v){noteStore.set(k,String(v));}};
const docEvents={};
globalThis.document={createElement:t=>new El(t),createTextNode:t=>new El('#TEXT',String(t)),createDocumentFragment:()=>new El('#FRAGMENT'),body:new El('body'),activeElement:null,
    addEventListener(k,f){(docEvents[k]||=[]).push(f);},removeEventListener(k,f){docEvents[k]=(docEvents[k]||[]).filter(x=>x!==f);}};
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
        layout:{scaleControls:1},controls:{view:'rows',showAdvanced:false,showNotes:true}};
    let refuse=false,defer=false,resolveWrite=null,refuseSetting=false;
    const host={getView:()=>view,subscribe(cb){listeners.add(cb);return()=>listeners.delete(cb);},getEditorSettings:()=>settings,
        setEditorSetting(k,v){settingWrites.push([k,v]);if(refuseSetting)throw Error('setting refused');const [s,n]=k.split('.');settings[s][n]=v;},notify:m=>notices.push(m),
        async applyChanges(changes){calls.push(changes);if(defer)await new Promise(r=>resolveWrite=r);return refuse?{ok:false,rejected:[{reason:'server refusal'}]}:{ok:true,rejected:[]};}};
    if(SCENARIO==='initial-error')view={...view,state:'error',error:'cached read failed'};
    // Lane and folder pickers (plans/bridge-selections-controls.md, D): the host's
    // picker sources stand in for controls_picker_sources.js.
    const pickerSubs=new Set();let pickerInterest=null;
    const refLane=(i,kind='image')=>({lane_index:i,lane_name:`Ref ${i+1}`,recipe_id:'r',recipe_name:'Slots',
        recipe:{hard:{assembly:'slots',max_members:16},soft:{}},media_kind:kind,item_count:1,member_count:1,
        reserved_member_span:1,prompt_override:'',strength:1});
    const pickerData={reference_lanes:{status:'ready',sceneName:'Saloon',source:'live',lanes:[refLane(0),refLane(1,'audio'),refLane(2),refLane(3)]},
        driver_lane:{status:'ready',sceneName:'Saloon',drivers:[{laneIndex:0,name:'Zero'},{laneIndex:1,name:'One'}]},
        asset_folder:{status:'ready',folders:['Shots','Takes/A']}};
    if(SCENARIO==='pickers'){
        host.pickers={get:k=>pickerData[k],setInterest(k){pickerInterest=[...k].sort();},
            subscribe(cb){pickerSubs.add(cb);return()=>pickerSubs.delete(cb);}};
        const b=(id,label,kind,value,extra)=>({source:'binding',binding_id:id,label,kind,value,status:'ok',order:0,group:'',section:'workflow',note:'',...extra});
        view={state:'ready',notes:[],bindings:[
            b('lanes','Lanes','STRING','0, bad',{order:1,picker:{kind:'reference_lanes',source:'host'}}),
            b('lane','Driver','INT','1',{order:2,min:'0',max:'999',picker:{kind:'driver_lane',source:'host'}}),
            b('folder','Folder','COMBO','Shots',{order:3,checked_at_run:true,picker:{kind:'asset_folder',source:'host'}}),
            b('other','Other lanes','STRING','1',{order:4,picker:{kind:'reference_lanes',source:'other'}}),
            b('atrun','Free combo','COMBO','x',{order:5,checked_at_run:true})]};
    }
    const container=new El('div'),panel=mountControlsPanel(container,host);
    const byTarget=t=>walk(container).find(n=>n.dataset.controlTarget===t);
    const input=t=>walk(byTarget(t)).find(n=>n.tagName==='INPUT');
    const note=t=>walk(container).find(n=>n.dataset.noteTarget===t);
    const key=(value,extra={})=>{const e={key:value,keyCode:0,isComposing:false,ctrlKey:false,metaKey:false,prevented:false,stopped:false,target:document.activeElement,
        preventDefault(){this.prevented=true;},stopImmediatePropagation(){this.stopped=true;},...extra};for(const f of events.keydown||[]){f(e);if(e.stopped)break;}return e;};
    const publish=next=>{view=next;for(const cb of listeners)cb(view);};
    const toggleManage=()=>walk(container).find(n=>n.dataset.controlsManageToggle==='true').dispatch('click');
    const button=(within,text)=>walk(within).find(n=>n.tagName==='BUTTON'&&n.textContent===text);
    if(['layout','layout-refusal','layout-stale','layout-editor','editor-reorder','cancel','note-save','ime','note-refusal','note-then-value','problem-statuses','truncated'].includes(SCENARIO))toggleManage();
    const group=name=>walk(container).find(n=>n.dataset.controlsGroup===name);
    const mirror=key=>walk(container).find(n=>n.dataset.controlsMirror===key);

    if(SCENARIO==='pickers'){
        await settle();
        assert.deepEqual(pickerInterest,['asset_folder','driver_lane','reference_lanes'],'interest follows the shown pickers');
        // Reference lanes: named rows, the malformed token disclosed, nothing rewritten by viewing.
        const lanesRow=byTarget('binding:lanes:value');
        assert.match(lanesRow.textContent,/Ref 1 — Slots/);
        assert.match(lanesRow.textContent,/Ignored unparseable lane token: bad/);
        assert.equal(calls.length,0);
        const entry=t=>walk(byTarget('binding:lanes:value')).find(n=>n.tagName==='BUTTON'&&n.textContent.startsWith(t));
        button(lanesRow,'+ Add lane').dispatch('click');
        assert.equal(entry('Ref 2').disabled,true);
        assert.match(entry('Ref 2').textContent,/Media kind differs/);
        // Two quick Adds: the second builds on the first, still pending.
        entry('Ref 3').dispatch('click');
        button(byTarget('binding:lanes:value'),'+ Add lane').dispatch('click');entry('Ref 4').dispatch('click');
        await settle();
        assert.deepEqual(calls.slice(-2),[{'binding:lanes:value':'0, 2'},{'binding:lanes:value':'0, 2, 3'}]);
        walk(byTarget('binding:lanes:value')).find(n=>n.title==='Remove Reference 3').dispatch('click');
        await settle();
        assert.deepEqual(calls.at(-1),{'binding:lanes:value':'0, 3'});
        // Driver lane: the scene's names, an integer commit, and a refusal resets the choice.
        const laneSelect=()=>walk(byTarget('binding:lane:value')).find(n=>n.tagName==='SELECT');
        assert.deepEqual(laneSelect().children.map(o=>o.textContent),['Zero','One']);
        assert.equal(laneSelect().value,'1');
        laneSelect().value='0';laneSelect().dispatch('change');await settle();
        assert.deepEqual(calls.at(-1),{'binding:lane:value':0});
        publish({...view,bindings:view.bindings.map(x=>x.binding_id==='lane'?{...x,value:'0'}:x)});
        refuse=true;laneSelect().value='1';laneSelect().dispatch('change');await settle();refuse=false;
        assert.deepEqual(calls.at(-1),{'binding:lane:value':1});
        assert.equal(laneSelect().value,'0','a refused lane shows the stored one again');
        // Folder: typed text is the value, normalized; Root is offered under its label.
        const folderRow=byTarget('binding:folder:value');
        const folderInput=walk(folderRow).find(n=>n.tagName==='INPUT');
        assert.equal(folderInput.placeholder,'Root');
        assert.ok(!folderRow.textContent.includes('Checked by the node'));
        folderInput.focus();folderInput.value=' New\\Folder/ ';folderInput.dispatch('input');key('Enter');await settle();
        assert.deepEqual(calls.at(-1),{'binding:folder:value':'New/Folder'});
        folderInput.focus();folderInput.value='';folderInput.dispatch('input');
        const choices=walk(folderRow).find(n=>n.tagName==='SELECT');
        assert.ok(choices.children.some(o=>o.textContent==='Root'&&o.value===''));
        assert.ok(choices.children.some(o=>o.value==='Takes/A'));
        assert.equal(button(folderRow,'Use typed value').hidden,false,'a folder picker always offers the typed text');
        folderInput.blur();await settle();
        assert.deepEqual(calls.at(-1),{'binding:folder:value':''},'leaving the field keeps what was typed: Root');
        // Generate saves a focused folder draft first.
        folderInput.focus();folderInput.value='Flushed';folderInput.dispatch('input');
        assert.equal(await panel.flushPending(),true);
        assert.deepEqual(calls.at(-1),{'binding:folder:value':'Flushed'});
        // Another Editor's lanes: the plain field, saying why. A combo checked at run with
        // nothing to list is a text field, not an empty list.
        const otherRow=byTarget('binding:other:value');
        assert.ok(walk(otherRow).some(n=>n.tagName==='INPUT'&&n.type==='text'));
        assert.match(otherRow.textContent,/Wired to another Sonder Editor/);
        assert.ok(!button(otherRow,'+ Add lane'));
        const atrun=byTarget('binding:atrun:value');
        assert.ok(walk(atrun).some(n=>n.tagName==='INPUT'&&n.type==='text'));
        assert.ok(!walk(atrun).some(n=>n.tagName==='SELECT'));
        // The sources' news repaints the pickers.
        pickerData.driver_lane={status:'ready',sceneName:'Saloon',drivers:[{laneIndex:0,name:'Renamed'},{laneIndex:1,name:'One'}]};
        for(const cb of pickerSubs)cb();
        assert.equal(laneSelect().children[0].textContent,'Renamed');
        // host -> other rebuilds the row as a plain field, and interest drops the kind.
        publish({...view,bindings:view.bindings.map(x=>x.binding_id==='lanes'?{...x,value:'0, 3',picker:{kind:'reference_lanes',source:'other'}}:x)});
        await settle();
        const rebuilt=byTarget('binding:lanes:value');
        assert.ok(!button(rebuilt,'+ Add lane'));
        assert.match(rebuilt.textContent,/Wired to another Sonder Editor/);
        assert.deepEqual(pickerInterest,['asset_folder','driver_lane']);
        // Without choices: numbered rows, no Add.
        publish({...view,bindings:view.bindings.map(x=>x.binding_id==='lanes'?{...x,picker:{kind:'reference_lanes',source:'host'}}:x)});
        pickerData.reference_lanes={status:'loading'};for(const cb of pickerSubs)cb();
        const loading=byTarget('binding:lanes:value');
        assert.match(loading.textContent,/Reference 1/);assert.match(loading.textContent,/Loading lanes/);
        assert.equal(button(loading,'+ Add lane').disabled,true);
        // No news, no repaint: a focused Remove keeps its element, so keyboard focus stays.
        pickerData.reference_lanes={status:'ready',sceneName:'Saloon',source:'live',lanes:[refLane(0),refLane(1,'audio'),refLane(2),refLane(3)]};
        for(const cb of pickerSubs)cb();
        const lanesNow=()=>byTarget('binding:lanes:value');
        const firstRemove=walk(lanesNow()).find(n=>n.title&&n.title.startsWith('Remove Reference'));
        for(const cb of pickerSubs)cb();
        assert.equal(walk(lanesNow()).find(n=>n.title===firstRemove.title),firstRemove,'an unchanged picker is not repainted');
        // Revalidating keeps the list: a read in flight with lanes held still lists them and adds.
        pickerData.reference_lanes={...pickerData.reference_lanes,status:'loading'};for(const cb of pickerSubs)cb();
        assert.match(lanesNow().textContent,/Ref 1 — Slots/);
        assert.equal(button(lanesNow(),'+ Add lane').disabled,false);
        pickerData.reference_lanes={...pickerData.reference_lanes,status:'ready'};for(const cb of pickerSubs)cb();
        // A view published while the lane menu is open is the base of the next Add.
        walk(lanesNow()).find(n=>n.title==='Remove Reference 4').dispatch('click');await settle();
        button(lanesNow(),'+ Add lane').dispatch('click');
        publish({...view,bindings:view.bindings.map(x=>x.binding_id==='lanes'?{...x,value:'3'}:x)});await settle();
        button(lanesNow(),'+ Add lane').dispatch('click');await settle();
        button(lanesNow(),'+ Add lane').dispatch('click');
        walk(lanesNow()).find(n=>n.tagName==='BUTTON'&&n.textContent.startsWith('Ref 3')).dispatch('click');await settle();
        assert.deepEqual(calls.at(-1),{'binding:lanes:value':'2, 3'},'an external change seen mid-edit is not overwritten');
        // Driver: a failed first read offers a typed lane number and Retry.
        let retried=0;host.pickers.refresh=()=>{retried++;};
        pickerData.driver_lane={status:'error',error:'Driver bridge fetch failed: 503'};for(const cb of pickerSubs)cb();
        const driverRow=byTarget('binding:lane:value');
        const typedLane=walk(driverRow).find(n=>n.tagName==='INPUT'&&n.type==='number');
        assert.equal(typedLane.style.display,'');
        assert.match(driverRow.textContent,/Type a lane number/);
        button(driverRow,'Retry').dispatch('click');assert.equal(retried,1);
        typedLane.value='3';typedLane.dispatch('change');await settle();
        assert.deepEqual(calls.at(-1),{'binding:lane:value':2},'typed 1-based, stored zero-based');
        // Folder: a near match is not highlighted, Escape saves nothing, and typing a
        // label means its value.
        const folderNow=byTarget('binding:folder:value');
        const folderField=walk(folderNow).find(n=>n.tagName==='INPUT');
        const sent=calls.length;
        folderField.focus();folderField.value='Sho';folderField.dispatch('input');
        assert.equal(walk(folderNow).find(n=>n.tagName==='SELECT').selectedIndex,-1);
        key('Escape');await settle();
        assert.equal(calls.length,sent,'Escape abandons the typed text');
        folderField.focus();folderField.value='root';folderField.dispatch('input');key('Enter');await settle();
        assert.deepEqual(calls.at(-1),{'binding:folder:value':''},'"Root" is the Root value, never a folder named Root');
    }
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
        // Editor-section bindings sit in the Editor group after your settings; their drag handle shows only in Manage (#7).
        const adv=byTarget('binding:adv:value');assert.ok(editor.contains(adv));
        const editorRows=walk(editor.children[1]).filter(n=>n.dataset.controlsMirror||n.dataset.controlTarget).map(n=>n.dataset.controlsMirror||n.dataset.controlTarget);
        assert.deepEqual(editorRows,['render.takePlacementMuted','render.linkedTakePlacement','batchRender.maxFramesPerChunk','binding:adv:value']);
        assert.equal(walk(adv).find(n=>n.dataset.panelDragHandle==='true').hidden,true);assert.equal(input('binding:adv:value').checked,false);
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
        const advPill=()=>walk(byTarget('binding:steps:value')).find(n=>n.tagName==='SPAN'&&n._text==='Adv');
        assert.equal(byTarget('binding:steps:value').hidden,false);assert.equal(advPill().hidden,false);
        box.value='';box.dispatch('input');
        line.dispatch('click');assert.deepEqual(settingWrites.at(-1),['controls.showAdvanced',true]);panel.render();
        assert.equal(byTarget('binding:steps:value').hidden,false);assert.match(line.textContent,/Hide advanced settings/);
        // Manage shows everything and flags Advanced with a toggle.
        settings.controls.showAdvanced=false;toggleManage();assert.equal(byTarget('binding:steps:value').hidden,false);assert.equal(line.hidden,true);
        const flag=walk(byTarget('binding:steps:value')).find(n=>n.dataset.controlsAdvancedToggle==='steps');assert.equal(flag.attributes['aria-pressed'],'true');
        flag.dispatch('click');await settle();assert.deepEqual(calls.at(-1),{'binding:steps:advanced':false});
    }
    if(SCENARIO==='notes-hidden'){
        const line=walk(container).find(n=>n.dataset.controlsNotesLine==='true');
        const stepsNote=()=>note('binding:steps:note');
        assert.equal(line.hidden,false);assert.equal(line.textContent,'Hide notes');assert.equal(line.attributes['aria-pressed'],undefined);
        // The lines' row keeps its flex layout through hide and show (live-found).
        assert.equal(line.parentElement.style.display,'flex');
        assert.equal(stepsNote().style.display,'');assert.equal(byTarget('note:intro:text').hidden,false);
        // Cards with notes on: a long note clamps, with More (the mock has no layout, so set it).
        Object.assign(stepsNote(),{scrollHeight:100,clientHeight:20});
        settings.controls.view='cards';panel.render();
        const more=()=>walk(byTarget('binding:steps:value')).find(n=>n.dataset.noteMore==='true');
        assert.equal(stepsNote().style.display,'-webkit-box');assert.equal(more().hidden,false);
        settings.controls.view='rows';panel.render();
        line.dispatch('click');assert.deepEqual(settingWrites.at(-1),['controls.showNotes',false]);panel.render();
        // Both kinds hide; the Control itself stays, and so does its value.
        assert.equal(byTarget('note:intro:text').hidden,true);assert.equal(byTarget('note:intro:text').style.display,'none');
        assert.equal(byTarget('binding:steps:value').hidden,false);assert.equal(input('binding:steps:value').value,'20');
        assert.equal(stepsNote().style.display,'none');
        assert.equal(line.textContent,'2 notes hidden');
        // Cards never clamps a hidden note back into view, nor offers More.
        settings.controls.view='cards';panel.render();
        assert.equal(stepsNote().style.display,'none');assert.equal(more().hidden,true);
        settings.controls.view='rows';panel.render();
        // Search shows a matching Control's note, as it shows a hidden Advanced one;
        // standalone notes are never searched.
        const box=walk(container).find(n=>n.dataset.controlsSearch==='true');box.value='steps';box.dispatch('input');
        assert.equal(stepsNote().style.display,'');assert.equal(byTarget('note:intro:text').hidden,true);
        box.value='';box.dispatch('input');assert.equal(stepsNote().style.display,'none');
        // Manage shows every note and drops the line.
        toggleManage();assert.equal(byTarget('note:intro:text').hidden,false);assert.equal(stepsNote().style.display,'');assert.equal(line.hidden,true);
        assert.equal(line.parentElement.style.display,'none');
        toggleManage();assert.equal(stepsNote().style.display,'none');assert.equal(line.parentElement.style.display,'flex');
        // The count is what turning notes on reveals: not a hidden Advanced row's note,
        // nor a note hidden in the Sonder App.
        publish({...view,bindings:view.bindings.map(d=>d.binding_id==='steps'?{...d,advanced:true}:d)});
        assert.equal(line.textContent,'1 note hidden');
        publish({...view,notes:view.notes.map(d=>({...d,hidden:true}))});
        assert.equal(line.hidden,true);
        settings.controls.showAdvanced=true;panel.render();assert.equal(line.textContent,'1 note hidden');
        line.dispatch('click');assert.deepEqual(settingWrites.at(-1),['controls.showNotes',true]);panel.render();
        assert.equal(stepsNote().style.display,'');assert.equal(line.textContent,'Hide notes');
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
        // Cards in a grid row share one height (#4). A noted card keeps its column;
        // its note clamps to two lines with its text as the tooltip (#11). Manage always uses rows.
        assert.match(container.children.find(n=>n.dataset.panelDropGroup==='').style.cssText,/align-items:stretch/);
        const noted=byTarget('binding:steps:value');assert.doesNotMatch(noted.style.cssText,/grid-column:1 \/ -1/);
        const stepsNote=walk(noted).find(n=>n.dataset.noteTarget==='binding:steps:note');
        assert.equal(stepsNote.style.webkitLineClamp,'2');assert.equal(stepsNote.title,stepsNote.textContent);
        const more=walk(noted).find(n=>n.dataset.noteMore==='true');assert.equal(more.hidden,true);stepsNote.scrollHeight=60;stepsNote.clientHeight=30;panel.render();assert.equal(more.hidden,false);assert.equal(more.attributes['aria-expanded'],'false');more.dispatch('click');assert.equal(stepsNote.style.webkitLineClamp,'');assert.equal(more.textContent,'Less');
        toggleManage();assert.doesNotMatch(byTarget('binding:seed:value').style.cssText,/border-radius/);
        // #5: Manage/Done keeps its place: the search hides without collapsing the row,
        // the toggle sits in a right-aligned slot sized by a hidden "Manage", and the gutter is stable.
        const search=walk(container).find(n=>n.dataset.controlsSearch==='true');assert.equal(search.style.visibility,'hidden');
        const toggle=walk(container).find(n=>n.dataset.controlsManageToggle==='true');assert.equal(toggle.textContent,'Done');
        assert.match(toggle.parentElement.style.cssText,/margin-left:auto/);
        const sizer=toggle.parentElement.children.find(n=>n!==toggle);assert.equal(sizer.textContent,'Manage');assert.match(sizer.style.cssText,/visibility:hidden/);
        assert.match(container.style.cssText,/scrollbar-gutter:stable/);
        settings.layout.scaleControls=1;panel.render();assert.equal(container.style.zoom,'');
    }
    if(SCENARIO==='group-note'){
        toggleManage();const section=walk(container).find(n=>n.dataset.controlsManage==='true');
        walk(group('Advanced').children[0]).find(n=>n.dataset.groupAction==='note').dispatch('click');
        const area=walk(section).find(n=>n.tagName==='TEXTAREA'&&!n.dataset.workflowDescription&&!n.dataset.modesDescription);area.value='Seeds';key('Enter',{ctrlKey:true});await settle();
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
    if(SCENARIO==='note-titles'){
        // The title rule: a first-line heading or bold-only line, with a body after it.
        const {splitPanelMarkdownTitle}=await import(MARKDOWN_URL);
        assert.deepEqual(splitPanelMarkdownTitle('**Before you start**\n\nChoose a **Preset**.'),{title:'Before you start',body:'Choose a **Preset**.'});
        assert.deepEqual(splitPanelMarkdownTitle('\r\n\r\n## Use `seed` [here](https://x.y)\r\nbody\r\n'),{title:'Use seed here',body:'body'});
        assert.deepEqual(splitPanelMarkdownTitle('### Three\n- one'),{title:'Three',body:'- one'});
        for(const text of ['**a** and **b**\nbody','Plain first line\n\nbody','#### Four\nbody','# Title only','**Title only**\n\n',''])
            assert.equal(splitPanelMarkdownTitle(text),null,text);
        // Headings take theme sizes, not the browser's.
        const h2=walk(renderPanelMarkdown('## Head\ntext')).find(n=>n.tagName==='H2');
        assert.match(h2.style.cssText,/font-size:12px/);
        const titled={source:'note',note_id:'intro',text:'**Before you start**\n\nChoose a **Preset**.',status:'ok',order:0,group:''};
        publish({...view,notes:[titled]});
        const header=()=>walk(container).find(n=>n.dataset.noteHeader==='true');
        const toggle=()=>walk(header()).find(n=>n.dataset.noteToggle==='true');
        assert.equal(header().style.display,'flex');
        assert.equal(walk(header()).find(n=>n.tagName==='SPAN').textContent,'Before you start');
        assert.equal(toggle().attributes['aria-expanded'],'true');
        assert.ok(note('note:intro:text').textContent.includes('Preset'));
        assert.ok(!note('note:intro:text').textContent.includes('Before you start'),'the title is not repeated in the body');
        // The header collapses (a double-click's second click is ignored).
        header().dispatch('click',{detail:1});
        assert.equal(note('note:intro:text').style.display,'none');
        assert.equal(toggle().attributes['aria-expanded'],'false');
        header().dispatch('click',{detail:2});
        assert.equal(note('note:intro:text').style.display,'none');
        // A refresh keeps it collapsed, and so does a fresh panel in this browser.
        publish({...view,notes:[{...titled}]});
        assert.equal(note('note:intro:text').style.display,'none');
        assert.deepEqual(JSON.parse(noteStore.get('sonder-controls-disclosure-v1')),{'note:intro':false});
        const other=new El('div'),second=mountControlsPanel(other,host);
        assert.equal(walk(other).find(n=>n.dataset.noteTarget==='note:intro:text').style.display,'none');
        second.destroy();
        // Another panel's write is kept when this one remembers a different key.
        const {createDisclosureMemory}=await import(new URL('./disclosure_memory.js',MARKDOWN_URL));
        const a=createDisclosureMemory('two-panels'),b=createDisclosureMemory('two-panels');
        a.remember('note:x',false);b.remember('note:y',false);
        assert.deepEqual(JSON.parse(noteStore.get('two-panels')),{'note:x':false,'note:y':false});
        // Manage: the header click no longer toggles; the toggle still does.
        toggleManage();
        header().dispatch('click',{detail:1});
        assert.equal(note('note:intro:text').style.display,'none');
        // Double-clicking the title edits the whole text, title line included,
        // in a visible box with the header out of the way.
        walk(header()).find(n=>n.tagName==='SPAN').dispatch('dblclick');
        const area=walk(note('note:intro:text')).find(n=>n.tagName==='TEXTAREA');
        assert.equal(area.value,titled.text);
        assert.equal(note('note:intro:text').hidden,false);
        assert.equal(note('note:intro:text').style.display,'');
        assert.equal(header().style.display,'none');
        key('Escape');await settle();
        assert.equal(header().style.display,'flex');
        assert.equal(note('note:intro:text').style.display,'none');
        toggle().dispatch('click');
        assert.equal(note('note:intro:text').style.display,'');
        assert.equal(calls.length,0);
        // Plain first line: no header, the whole note as before.
        toggleManage();
        publish({...view,notes:[{...titled,text:'Plain words\n\nmore'}]});
        assert.equal(header().style.display,'none');
        assert.ok(note('note:intro:text').textContent.includes('Plain words'));
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
        // "+ Setting" sits with + Note and + Group and opens the picker, whose header carries the counts (#8).
        const bar=walk(section).find(n=>n.dataset.controlsAddSettings==='true');assert.equal(bar.attributes['aria-expanded'],'false');
        assert.equal(bar.textContent,'+ Setting');assert.ok(['+ Note','+ Group'].every(t=>bar.parentElement.children.some(n=>n.textContent===t)));
        const pickerHead=walk(section).find(n=>n.dataset.controlsPickerHeader==='true');assert.equal(pickerHead.style.display,'none');
        bar.dispatch('click');assert.equal(bar.attributes['aria-expanded'],'true');assert.equal(pickerHead.style.display,'flex');
        assert.match(pickerHead.textContent,/3\+ available · 2 recommended/);
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
        button(section,'+ Note').dispatch('click');const area=walk(section).find(n=>n.tagName==='TEXTAREA'&&!n.dataset.workflowDescription&&!n.dataset.modesDescription);area.value='**Tip**';
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
    if(SCENARIO==='value-title'){
        // #9: a clipped value shows in full on hover, and only while clipped.
        const field=input('binding:steps:value');field.value='123456789012';field.scrollWidth=200;field.clientWidth=50;
        container.dispatch('mouseover',{target:field});assert.equal(field.title,'123456789012');
        field.scrollWidth=40;container.dispatch('mouseover',{target:field});assert.equal(field.title,undefined);
        field.title='Its own tooltip';field.scrollWidth=200;container.dispatch('mouseover',{target:field});assert.equal(field.title,'Its own tooltip');
        // A list (the long-option picker's choices) never takes a value tooltip.
        const list=document.createElement('select');list.size=6;byTarget('binding:steps:value').append(list);list.scrollWidth=200;list.clientWidth=50;
        container.dispatch('mouseover',{target:list});assert.equal(list.title,undefined);
    }
    if(SCENARIO==='editor-reorder'){
        // #7: exposed Editor rows reorder among themselves (only :order is written),
        // and never move into a workflow group or between workflow rows.
        const second={source:'binding',binding_id:'adv2',label:'Advance 2',kind:'BOOLEAN',value:false,note:'',status:'ok',order:5,group:'',section:'editor'};
        publish({...view,bindings:[...view.bindings,second]});await settle();
        const data={};const transfer={setData(k,v){data[k]=v;},getData(k){return data[k];}};
        const grip=walk(byTarget('binding:adv2:value')).find(n=>n.dataset.panelDragHandle==='true');assert.equal(grip.hidden,false);
        container.dispatch('dragstart',{target:grip,dataTransfer:transfer});
        container.dispatch('drop',{target:byTarget('binding:adv:value'),dataTransfer:transfer});await settle();
        assert.equal(calls.length,1);assert.ok(Object.keys(calls[0]).length&&Object.keys(calls[0]).every(k=>k.endsWith(':order')));
        const before=calls.length;
        container.dispatch('dragstart',{target:grip,dataTransfer:transfer});
        container.dispatch('drop',{target:group('Advanced').children[0],dataTransfer:transfer});await settle();
        container.dispatch('dragstart',{target:grip,dataTransfer:transfer});
        container.dispatch('drop',{target:byTarget('binding:steps:value'),dataTransfer:transfer});await settle();
        assert.equal(calls.length,before);
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
    if(SCENARIO==='rows'||SCENARIO==='rows-notice'||SCENARIO==='rows-app'){
        // A row-list Control (a LoRA stack): one Control, edited as a whole list.
        const lora=(n,s,e)=>({lora_name:n,strength:s,enabled:e});
        const first=[lora('a.safetensors',1,false),lora('b.safetensors',0.5,true)];
        const stack={source:'binding',binding_id:'stack',label:'Load LoRA (Model) · LoRAs',kind:'ROWS',status:'ok',order:2,group:'',section:'workflow',
            note:'',value:first,group_name:'LoRA',min:1,max:3,new_row:lora('a.safetensors',1,true),
            fields:[{id:'lora_name',label:'lora_name',kind:'COMBO',options:['a.safetensors','b.safetensors']},{id:'strength',label:'strength',kind:'FLOAT',min:-100,max:100,step:0.01},
                {id:'enabled',label:'enabled',kind:'BOOLEAN'}],...(SCENARIO==='rows'?{}:{rows_unavailable:true,error:'Add at least 1 LoRA.',value:[]})};
        if(SCENARIO==='rows-app')host.capabilities={hide:true,removeAuthored:false,serverRows:true};
        publish({...view,bindings:[...view.bindings,stack]});await settle();
        const row=byTarget('binding:stack:value');
        const buttons=()=>walk(row).filter(n=>n.tagName==='BUTTON');
        if(SCENARIO==='rows-notice'){
            // The canvas can't edit rows its frontend never built: a notice, not the editor.
            assert.match(row.textContent,/frontend 1\.57 or newer/);
            assert.ok(!row.textContent.includes('Add at least 1 LoRA'));
        }else{
            assert.ok(!row.textContent.includes('frontend 1.57'));
            const header=buttons().find(n=>/LoRA/.test(n.textContent)&&n.textContent.includes('·')||n.textContent.includes(SCENARIO==='rows'?'2 LoRAs':'0 LoRAs'));
            assert.ok(header,'summary header');
            assert.match(header.textContent,SCENARIO==='rows'?/2 LoRAs · 1 on/:/0 LoRAs · 0 on/);
            header.dispatch('click');await settle();
            if(SCENARIO==='rows'){
                const boxes=walk(row).filter(n=>n.tagName==='INPUT'&&n.type==='checkbox');
                assert.equal(boxes.length,2);
                boxes[0].checked=true;boxes[0].dispatch('change');await settle();
                const turnedOn=[lora('a.safetensors',1,true),lora('b.safetensors',0.5,true)];
                assert.deepEqual(calls.at(-1),{'binding:stack:value':{rows:turnedOn,base:first}});
                // The next edit starts from the optimistic list, not the stale view.
                buttons().find(n=>n.textContent==='+ Add LoRA').dispatch('click');await settle();
                assert.deepEqual(calls.at(-1)['binding:stack:value'],{rows:[...turnedOn,lora('a.safetensors',1,true)],base:turnedOn});
                // At the maximum, Add is disabled; a row's Remove sends the list without it.
                assert.equal(buttons().find(n=>n.textContent==='+ Add LoRA').disabled,true);
                // A click focuses the button first, as a browser does.
                const removeFirst=buttons().find(n=>n.attributes['aria-label']==='Remove LoRA 1');
                removeFirst.focus();removeFirst.dispatch('click');await settle();
                assert.equal(calls.at(-1)['binding:stack:value'].rows.length,2);
                assert.equal(calls.at(-1)['binding:stack:value'].rows[0].lora_name,'b.safetensors');
                // The rows repaint at once (two lines now), and focus stays on a Remove,
                // so a second click removes the row it shows, not a shifted one.
                const removeButtons=()=>buttons().filter(n=>/^Remove LoRA/.test(n.attributes['aria-label']||''));
                assert.equal(removeButtons().length,2);
                assert.equal(document.activeElement,removeButtons()[0]);
                const afterRemove=calls.at(-1)['binding:stack:value'].rows;
                removeButtons()[1].focus();removeButtons()[1].dispatch('click');await settle();
                assert.deepEqual(calls.at(-1)['binding:stack:value'],{rows:[afterRemove[0]],base:afterRemove});
                // Escape reverts a typed strength instead of committing it (a
                // browser fires change on blur when the value changed).
                const strength=()=>walk(row).find(n=>n.tagName==='INPUT'&&n.type==='number');
                const field=strength(),sent=calls.length,shown=field.value;
                field.blur=function(next){if(document.activeElement===this)this.dispatch('change');El.prototype.blur.call(this,next);};
                field.focus();field.value='7';key('Escape');await settle();
                assert.equal(calls.length,sent);
                assert.equal(strength().value,shown);
                // A refused edit resets the list even while a field has focus,
                // so the next edit's base is the stored rows, not the refused ones.
                refuse=true;
                const typed=strength();typed.focus();typed.value='3';typed.dispatch('change');await settle();
                refuse=false;
                assert.equal(strength().value,String(row.dataset.controlTarget&&first[0].strength));
                strength().focus();strength().value='4';strength().dispatch('change');await settle();
                assert.deepEqual(calls.at(-1)['binding:stack:value'].base,first);
                // The App LoRA case: a press on Remove right after editing a field.
                // The field's blur-save re-renders; a repaint replacing the
                // pressed button would lose its click, so it waits for the press.
                {
                    const pressedRemove=buttons().find(n=>n.attributes['aria-label']==='Remove LoRA 1');
                    const list=pressedRemove.parentElement.parentElement;
                    walk(row).filter(n=>n.tagName==='INPUT'&&n.type==='checkbox')[0].focus();
                    list.dispatch('pointerdown');pressedRemove.focus();
                    const stored=calls.at(-1)['binding:stack:value'].rows;
                    publish({...view,bindings:[...view.bindings,{...stack,value:stored.map((r,i)=>i?r:{...r,strength:0.25})}]});await settle();
                    assert.ok(row.contains(pressedRemove),'the pressed Remove stays until the press ends');
                    for(const f of [...(docEvents.pointerup||[])])f({});
                    await new Promise(r=>setTimeout(r,5));await settle();
                    assert.ok(!row.contains(pressedRemove),'then the rows repaint');
                    assert.equal((docEvents.pointerup||[]).length,0);
                    publish({...view,bindings:[...view.bindings,{...stack,value:stored}]});await settle();
                }
            }else{
                // The Sonder App edits rows a frontend never built: Add works from empty.
                buttons().find(n=>n.textContent==='+ Add LoRA').dispatch('click');await settle();
                assert.deepEqual(calls.at(-1),{'binding:stack:value':{rows:[lora('a.safetensors',1,true)],base:[]}});
            }
        }
    }
    if(SCENARIO==='tooltips'){
        // plans/live-workflow-phase-4-controls-modes.md D6: a node's own help on
        // hover of the label and on keyboard focus of the value, once each.
        const help='The number of steps.\nMore is slower.';
        const b=(id,label,extra)=>({source:'binding',binding_id:id,label,kind:'INT',value:'20',status:'ok',order:1,group:'',section:'workflow',note:'',...extra});
        publish({...view,bindings:[b('steps','Steps',{tooltip:help}),b('plain','Plain',{order:2}),
            b('off','Pass 2',{order:3,tooltip:'Its own help.',participation:'inactive',inactive_reason:{mode:'muted',by:'self',label:'Pass 2'}}),
            {source:'binding',binding_id:'stack',label:'LoRAs',kind:'ROWS',status:'ok',order:4,group:'',section:'workflow',note:'',
                value:[{lora_name:'a',strength:1}],group_name:'LoRA',min:0,max:3,new_row:{lora_name:'a',strength:1},tooltip:'The stack.',
                fields:[{id:'lora_name',label:'lora_name',kind:'COMBO',options:['a','b']},{id:'strength',label:'strength',kind:'FLOAT',tooltip:'How strong.'}]}]});
        await settle();
        const tip=t=>walk(byTarget(t)).find(n=>n.dataset.controlsTip==='true');
        const label=t=>walk(byTarget(t)).find(n=>n.tagName==='LABEL');
        assert.equal(label('binding:steps:value').title,undefined,'the help tip replaces the label title');
        assert.equal(label('binding:plain:value').title,'Plain','no help: the label keeps its title');
        // Hover: "<label> — <help>", kept line breaks, never read by a screen reader.
        label('binding:steps:value').dispatch('mouseenter');
        assert.equal(tip('binding:steps:value').style.display,'block');assert.equal(tip('binding:steps:value').textContent,'Steps — '+help);
        assert.equal(tip('binding:steps:value').attributes['aria-hidden'],'true');assert.equal(tip('binding:steps:value').style.whiteSpace,undefined);
        assert.match(tip('binding:steps:value').style.cssText,/white-space:pre-line/);
        label('binding:steps:value').dispatch('mouseleave');assert.equal(tip('binding:steps:value').style.display,'none');
        label('binding:plain:value').dispatch('mouseenter');assert.equal(tip('binding:plain:value')?.style.display??'none','none','no help, no tip');
        // Keyboard focus: the help, without the label; typing hides it.
        const steps=input('binding:steps:value');steps.focus();
        assert.equal(tip('binding:steps:value').style.display,'block');assert.equal(tip('binding:steps:value').textContent,help);
        steps.dispatch('input');assert.equal(tip('binding:steps:value').style.display,'none');
        // A press into a field shows nothing, though Chromium's :focus-visible
        // matches a clicked text field; a clicked button fails :focus-visible.
        steps.blur();for(const f of events.pointerdown||[])f({});steps.focus();assert.equal(tip('binding:steps:value').style.display,'none');
        steps.blur();const now=Date.now;Date.now=()=>now()+2000;
        steps.matches=sel=>sel!==':focus-visible';steps.focus();assert.equal(tip('binding:steps:value').style.display,'none');
        delete steps.matches;steps.blur();steps.focus();assert.equal(tip('binding:steps:value').style.display,'block','the keyboard, later');
        steps.blur(); // the clock stays past the press
        // A press on another element, then Tab: the keyboard's focus shows it.
        Date.now=now;for(const f of events.pointerdown||[])f({target:label('binding:plain:value')});steps.focus();
        assert.equal(tip('binding:steps:value').style.display,'block','a press elsewhere never hides a Tab tip');
        // A key that does something (not Tab) hides it, as typing does.
        steps.dispatch('keydown',{key:'Tab'});assert.equal(tip('binding:steps:value').style.display,'block');
        steps.dispatch('keydown',{key:'ArrowUp'});assert.equal(tip('binding:steps:value').style.display,'none');
        steps.blur();Date.now=()=>now()+2000;
        // A screen reader hears the help; an off setting's reason joins it, in the tip too.
        const ids=el=>(el.getAttribute('aria-describedby')||'').split(' ').filter(Boolean);
        const text=(t,id)=>walk(byTarget(t)).find(n=>n.id===id)?.textContent;
        assert.deepEqual(ids(steps).map(id=>text('binding:steps:value',id)),[help]);
        const off=input('binding:off:value');
        assert.deepEqual(ids(off).map(id=>text('binding:off:value',id)).map(t=>t.slice(0,8)),['Off: its','Its own '],'merged, never replaced');
        off.focus();assert.match(tip('binding:off:value').textContent,/^Its own help\.\nOff: its node is muted/);
        // Escape in a text field is the field's draft, never the tip's.
        off.value='99';const e=key('Escape');assert.equal(off.value,'20','the draft reverted');assert.ok(e.stopped);
        assert.equal(tip('binding:off:value').style.display,'none','its blur hid the tip');
        // An open tip with focus on nothing that edits takes Escape.
        label('binding:steps:value').dispatch('mouseenter');const esc=key('Escape');assert.ok(esc.stopped&&esc.prevented);
        assert.equal(tip('binding:steps:value').style.display,'none');
        // A clipped value still shows in full on hover.
        steps.value='123456789012345';steps.scrollWidth=200;steps.clientWidth=50;container.dispatch('mouseover',{target:steps});
        assert.equal(steps.title,'123456789012345');
        // A row list: its header carries the list's help; a field shows "<field>: <help>" and drops its title.
        const stack=byTarget('binding:stack:value');const header=walk(stack).find(n=>n.tagName==='BUTTON'&&n.attributes['aria-expanded']);
        assert.ok(ids(header).some(id=>text('binding:stack:value',id)==='The stack.'));
        header.dispatch('click');await settle();
        const strength=walk(stack).find(n=>n.attributes['aria-label']==='LoRA 1 strength');
        assert.equal(strength.title,undefined);strength.dispatch('mouseenter');assert.equal(tip('binding:stack:value').textContent,'strength: How strong.');
        const name=walk(stack).find(n=>n.attributes['aria-label']==='LoRA 1 lora_name');assert.equal(name.title,'lora_name','no help: its title stays');
        strength.dispatch('mouseleave');
        assert.ok(ids(strength).some(id=>text('binding:stack:value',id)==='How strong.'),'a screen reader hears the field help');
        // A long option list opens on focus: its help never covers it.
        const many=Array.from({length:25},(_,i)=>'opt'+i);
        publish({...view,bindings:[b('pick','Pick',{kind:'COMBO',value:'opt0',options:many,tooltip:'Pick one.'})]});await settle();
        const pickInput=input('binding:pick:value');pickInput.focus();
        assert.notEqual(tip('binding:pick:value').style.display,'block','suppressed over the open list');pickInput.blur();
        // A detached anchor's tip goes with its last attachment.
        const {createHoverTip}=await import(new URL('controls_hover_tip.js',PANEL_URL).href);
        const box=new El('div'),anchor=new El('div'),button=new El('button');box.append(anchor);anchor.append(button);
        const tips=createHoverTip(box),detachTip=tips.attach(button,()=>'Help',{anchor});
        assert.equal(anchor.children.filter(n=>n.dataset.controlsTip).length,1);detachTip();
        assert.equal(anchor.children.filter(n=>n.dataset.controlsTip).length,0,'no tip keeps a dropped row alive');tips.destroy();
        // The tip is the help's text, never markup.
        publish({...view,bindings:[b('steps','Steps',{tooltip:'<b>bold</b>'})]});await settle();
        label('binding:steps:value').dispatch('mouseenter');assert.equal(tip('binding:steps:value').textContent,'Steps — <b>bold</b>');
        assert.equal(tip('binding:steps:value').children.length,0);
        panel.destroy();assert.equal(walk(container).some(n=>n.dataset.controlsTip==='true'),false);
    }
    if(SCENARIO==='switches'){
        // Node switches and inactive settings (plans/output-views-phase-1a.md).
        publish({...view, bindings:[...view.bindings, {source:'binding',binding_id:'off',label:'Pass 2 steps',kind:'INT',value:'8',status:'ok',
            order:4,group:'',section:'workflow',note:'',participation:'inactive',inactive_reason:{mode:'muted',by:'self',label:'Pass 2'}}],
            execution:[{source:'execution',execution_id:'x',label:'First pass',kind:'group',status:'ok',mode:'mixed',value:'mixed',
                members:[{path:'3',mode:'enabled'},{path:'4',mode:'muted'}],member_count:2,order:5,group:'',advanced:false,note:''}]});
        await settle();
        const seg=t=>walk(byTarget('execution:x:mode')).find(n=>n.dataset.executionMode===t);
        assert.match(byTarget('execution:x:mode').textContent,/Mixed \(1 of 2 off\)/);
        assert.match(byTarget('execution:x:mode').textContent,/Group · 2 nodes/);
        assert.equal(seg('muted').attributes['aria-checked'],'false');
        seg('muted').dispatch('click');await settle();
        assert.deepEqual(calls.at(-1),{'execution:x:mode':'muted'});
        // The clicked button keeps focus; a mode set elsewhere (a preset, Undo)
        // still shows, since a switch holds no draft.
        seg('muted').focus();
        publish({...view, bindings:[...view.bindings], execution:[{source:'execution',execution_id:'x',label:'First pass',kind:'group',status:'ok',mode:'enabled',value:'enabled',
            members:[{path:'3',mode:'enabled'},{path:'4',mode:'enabled'}],member_count:2,order:5,group:'',advanced:false,note:''}]});
        await settle();
        assert.equal(seg('enabled').attributes['aria-checked'],'true');
        // All three or one per line, never two plus one: each button's basis
        // survives the variant restyle on every paint.
        for(const mode of ['enabled','muted','bypassed'])assert.match(seg(mode).style.flex,/^1 1 calc\(\(\d+px - 100%\) \* 999\)$/);
        assert.equal(seg('muted').style.minWidth,'0');
        publish({...view, bindings:[...view.bindings, {source:'binding',binding_id:'off',label:'Pass 2 steps',kind:'INT',value:'8',status:'ok',
            order:4,group:'',section:'workflow',note:'',participation:'inactive',inactive_reason:{mode:'muted',by:'self',label:'Pass 2'}}],
            execution:[{source:'execution',execution_id:'x',label:'First pass',kind:'group',status:'ok',mode:'muted',value:'muted',
                members:[{path:'3',mode:'muted'},{path:'4',mode:'muted'}],member_count:2,order:5,group:'',advanced:false,note:''}]});
        await settle();
        // Generate and preset capture flush every row editor; a switch holds nothing to flush.
        assert.equal(await panel.flushPending(),true);
        // A muted setting keeps its row and stays editable. Its pill says why
        // (title, and a description the value points at); no paragraph repeats it.
        const off=byTarget('binding:off:value');
        const pill=walk(off).find(n=>n.dataset.controlsInactive==='inactive');
        assert.equal(pill.textContent,'Muted');assert.match(pill.title,/Off: its node is muted/);
        const described=input('binding:off:value').getAttribute('aria-describedby');
        assert.match(walk(off).find(n=>n.id===described).textContent,/Off: its node is muted/);
        const statusLine=walk(off).find(n=>n.tagName==='DIV'&&/Off: its node/.test(n.textContent)&&!n.children.length);
        assert.equal(statusLine,undefined);
        // A repaint with the same view keeps the label's pill and text nodes, and
        // writes no text: a node replaced under a press loses its click.
        const label=walk(off).find(n=>n.tagName==='LABEL'),labelText=label.children[0];
        let writes=0;const proto=Object.getPrototypeOf(labelText),setter=Object.getOwnPropertyDescriptor(proto,'textContent').set;
        Object.defineProperty(labelText,'textContent',{get(){return Object.getOwnPropertyDescriptor(proto,'textContent').get.call(this);},set(v){writes++;setter.call(this,v);}});
        publish({...view});await settle();
        assert.equal(walk(byTarget('binding:off:value')).find(n=>n.dataset.controlsInactive==='inactive'),pill);
        assert.equal(walk(byTarget('binding:off:value')).find(n=>n.tagName==='LABEL').children[0],labelText);
        assert.equal(writes,0);
        assert.equal(input('binding:off:value').disabled,false);
        // Search finds a switch by its mode; Manage gives it a setting's actions.
        toggleManage();await settle();
        assert.ok(button(byTarget('execution:x:mode'),'Rename'));
        assert.ok(button(byTarget('execution:x:mode'),'Remove'));
        toggleManage();await settle();
        // The Sonder App shows it read-only for now.
        publish({...view, execution:[{...view.execution[0], mode:'muted', value:'muted', read_only:true, reason:'Switch these on the canvas for now.'}]});
        await settle();
        assert.equal(seg('enabled').disabled,true);
        assert.match(byTarget('execution:x:mode').textContent,/on the canvas/);
        toggleManage();await settle();
        assert.ok(!button(byTarget('execution:x:mode'),'Remove'));
        assert.match(byTarget('execution:x:mode').textContent,/Canvas only/);
    }
    panel.destroy();assert.equal(listeners.size,0);assert.equal(container.children.length,0);
    if(SCENARIO==='pickers'){assert.equal(pickerSubs.size,0);assert.deepEqual(pickerInterest,[]);}
}
console.log('ok');
'''

@pytest.mark.parametrize('scenario',['value-title','editor-reorder','restore-notice','restore-all','description','group-editor-name','editor-focus-refresh','normal-view','manage','manage-sdk','manage-flush','hide-flush','combo-select-focus','combo-focus-return','combo-capped-keyboard','combo-capped-exact','combo-capped-current','combo-capped-filter','combo-query','layout','layout-refusal','layout-stale','layout-editor','combo','combo-capped','combo-ime','markdown','grouping','focus','focus-after-change','focus-after-refusal','metadata','cancel','note-save','ime','refusal','timeout','initial-error','flush','mode','cross-target-refusal','removed-failure','removed-pending','truncated','refusal-then-generate','late-success','note-refusal','problem-statuses','loading','note-then-value','batch-mirror','search','advanced','notes-hidden','advanced-refusal','hidden-pending','cards-scale','group-note','groups-manage','groups-cap','rows','rows-notice','rows-app','pickers','note-titles','switches','tooltips'])
def test_controls_panel(scenario, tmp_path):
    node=shutil.which('node')
    if not node:pytest.skip('node required for Controls renderer boundary tests')
    script=SCRIPT.replace('MARKDOWN_URL',json.dumps((ROOT/'web/js/panel_markdown.js').as_uri())).replace('PANEL_URL',json.dumps((ROOT/'web/js/editor_controls_panel.js').as_uri())).replace('SCENARIO',json.dumps(scenario))
    # A file, not `node -e`: the script is longer than a Windows command line.
    path=tmp_path/'panel_test.mjs';path.write_text(script,encoding='utf-8')
    result=subprocess.run([node,str(path)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='ok'
