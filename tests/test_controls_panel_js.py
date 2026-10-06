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
    remove(){if(this.parentElement){const a=this.parentElement.children;a.splice(a.indexOf(this),1);this.parentElement=null;}}
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
const events={};globalThis.window={addEventListener(k,f){(events[k]||=[]).push(f);},removeEventListener(k,f){events[k]=(events[k]||[]).filter(x=>x!==f);}};
globalThis.localStorage={getItem(){return null;},setItem(){}};
globalThis.document={createElement:t=>new El(t),createTextNode:t=>new El('#TEXT',String(t)),createDocumentFragment:()=>new El('#FRAGMENT'),body:new El('body'),activeElement:null};
document.activeElement=document.body;
globalThis.Option=class extends El{constructor(text,value){super('option',text);this.value=value;}};
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
    let view={state:'ready',controls:[{source:'control',key:'2',control_id:'steps',label:'Steps',kind:'INT',value:'20',note:'**Recommended**',status:'ok',order:1,group:''},
        {source:'control',key:'3',control_id:'seed',label:'Seed',kind:'SEED',seed:'9',seed_mode:'fixed',note:'',status:'ok',order:3,group:'Advanced'}],
        notes:[{source:'note',key:'4',note_id:'intro',text:'# Intro',status:'ok',order:0,group:''}],
        fields:[{class_type:'Save',field:'advance',label:'Advance',kind:'BOOLEAN',status:'ok',value:false},{class_type:'Save',field:'advance',label:'Advance',kind:'BOOLEAN',status:'ok',value:true}]};
    const calls=[],notices=[],settings={render:{takePlacementMuted:false,linkedTakePlacement:true}},listeners=new Set();
    let refuse=false,defer=false,resolveWrite=null;
    const host={getView:()=>view,subscribe(cb){listeners.add(cb);return()=>listeners.delete(cb);},getEditorSettings:()=>settings,
        setEditorSetting(k,v){settings.render[k.split('.')[1]]=v;},notify:m=>notices.push(m),
        async applyChanges(changes){calls.push(changes);if(defer)await new Promise(r=>resolveWrite=r);return refuse?{ok:false,rejected:[{reason:'server refusal'}]}:{ok:true,rejected:[]};}};
    if(SCENARIO==='initial-error')view={...view,state:'error',error:'cached read failed'};
    const container=new El('div'),panel=mountControlsPanel(container,host);
    const byTarget=t=>walk(container).find(n=>n.dataset.controlTarget===t);
    const input=t=>walk(byTarget(t)).find(n=>n.tagName==='INPUT');
    const note=t=>walk(container).find(n=>n.dataset.noteTarget===t);
    const key=(value,extra={})=>{const e={key:value,keyCode:0,isComposing:false,ctrlKey:false,metaKey:false,prevented:false,stopped:false,target:document.activeElement,
        preventDefault(){this.prevented=true;},stopImmediatePropagation(){this.stopped=true;},...extra};for(const f of events.keydown||[]){f(e);if(e.stopped)break;}return e;};
    const publish=next=>{view=next;for(const cb of listeners)cb(view);};
    if(SCENARIO==='grouping'){
        assert.equal(container.children[0].tagName,'DETAILS');assert.equal(container.children[0].open,true);
        const advanced=walk(container).find(n=>n.tagName==='SUMMARY'&&n.textContent==='Advanced').parentElement;assert.equal(advanced.open,false);
        const items=container.children[2].children.filter(n=>n.dataset.controlTarget);assert.equal(items[0].dataset.controlTarget,'note:intro:text');assert.equal(items[1].dataset.controlTarget,'control:steps:value');
        assert.equal(input('field:Save/advance').indeterminate,true);
        const muted=walk(container.children[0]).find(n=>n.tagName==='INPUT');muted.checked=true;muted.dispatch('change');assert.equal(settings.render.takePlacementMuted,true);
    }
    if(SCENARIO==='focus'){
        const el=input('control:steps:value');el.focus();el.value='35';
        publish({...view,controls:view.controls.map(d=>d.control_id==='steps'?{...d,value:'24',group:'Changed'}:d)});
        assert.equal(input('control:steps:value'),el);assert.equal(document.activeElement,el);assert.equal(el.value,'35');
        const event=key('Enter');assert.ok(event.prevented);await settle();assert.deepEqual(calls,[{'control:steps:value':'35'}]);
    }
    if(SCENARIO==='focus-after-change'){
        const el=input('control:steps:value');el.focus();el.value='30';el.dispatch('change');await settle();el.value='31';
        publish({...view,controls:view.controls.map(d=>d.control_id==='steps'?{...d,value:'30'}:d)});
        assert.equal(document.activeElement,el);assert.equal(el.value,'31');
    }
    if(SCENARIO==='focus-after-refusal'){
        defer=true;refuse=true;const el=input('control:steps:value');el.focus();el.value='30';el.dispatch('change');await settle();
        el.value='31';resolveWrite();await settle();assert.equal(document.activeElement,el);assert.equal(el.value,'31');
    }
    if(SCENARIO==='metadata'){
        publish({...view,controls:view.controls.map(d=>d.control_id==='steps'?{...d,label:'Renamed steps',min:'4',max:'40'}:d)});
        let el=input('control:steps:value');assert.equal(el.min,'4');assert.equal(el.attributes['aria-label'],'Renamed steps');
        publish({...view,controls:view.controls.map(d=>d.control_id==='steps'?{...d,min:null,max:null}:d)});
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
        refuse=SCENARIO!=='timeout';defer=SCENARIO==='timeout';const el=input('control:steps:value');el.focus();el.value='33';
        const flush=panel.flushPending();await settle();if(defer)timeoutCalls.at(-1)();assert.equal(await flush,false);assert.equal(el.value,'20');assert.equal(notices.length,1);resolveWrite?.();
    }
    if(SCENARIO==='flush'){
        const el=input('control:steps:value');el.focus();el.value='32';assert.equal(await panel.flushPending(),true);assert.deepEqual(calls,[{'control:steps:value':'32'}]);
    }
    if(SCENARIO==='mode'){
        const row=byTarget('control:seed:seed'),mode=walk(row).find(n=>n.tagName==='SELECT');mode.value='increment';mode.dispatch('change');await settle();
        assert.deepEqual(calls,[{'control:seed:seed_mode':'increment'}]);assert.equal(input('control:seed:seed').value,'9');
    }
    if(SCENARIO==='cross-target-refusal'){
        let rejectSeed;
        host.applyChanges=async changes=>{
            calls.push(changes);
            if('control:seed:seed' in changes){await new Promise(r=>rejectSeed=r);return{ok:false,rejected:[{target:'control:seed:seed',reason:'invalid seed'}]};}
            publish({...view,controls:view.controls.map(d=>d.control_id==='seed'?{...d,seed_mode:'increment'}:d)});return{ok:true,rejected:[]};
        };
        const el=input('control:seed:seed');el.focus();el.value='bad';el.dispatch('change');await settle();
        const mode=walk(byTarget('control:seed:seed')).find(n=>n.tagName==='SELECT');mode.focus();mode.value='increment';mode.dispatch('change');await settle();
        rejectSeed();await settle();assert.equal(el.value,'9');assert.equal(notices[0],'invalid seed');assert.equal(mode.value,'increment');
        // The row keeps the reason visible; the settled refusal does not block a later run.
        assert.match(byTarget('control:seed:seed').textContent,/Not saved: invalid seed/);assert.equal(await panel.flushPending(),true);
    }
    if(SCENARIO==='refusal-then-generate'){
        refuse=true;const el=input('control:steps:value');el.focus();el.value='99999';key('Enter');await settle();
        assert.equal(el.value,'20');assert.equal(notices.length,1);assert.match(byTarget('control:steps:value').textContent,/Not saved: server refusal/);
        // An edit of another row that saves does not inherit the old refusal.
        refuse=false;const seed=input('control:seed:seed');seed.focus();seed.value='11';assert.equal(await panel.flushPending(),true);
        assert.equal(await panel.flushPending(),true);
        // Fresh stored values retire the warning.
        publish({...view});await settle();assert.doesNotMatch(byTarget('control:steps:value').textContent,/Not saved/);
    }
    if(SCENARIO==='late-success'){
        defer=true;const el=input('control:steps:value');el.focus();el.value='33';key('Enter');await settle();
        timeoutCalls.at(-1)();await settle();assert.match(byTarget('control:steps:value').textContent,/Not saved: The change was not confirmed/);
        // The save lands after its warning: it is saved, and nothing blocks a run.
        resolveWrite();await settle();assert.doesNotMatch(byTarget('control:steps:value').textContent,/Not saved/);
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
            if('control:steps:value' in changes){await new Promise(r=>refuseSteps=r);return{ok:false,rejected:[{reason:'steps refused'}]};}
            await new Promise(r=>saveNote=r);return{ok:true,rejected:[]};};
        const steps=input('control:steps:value');steps.focus();steps.value='33';key('Enter');await settle();
        note('note:intro:text').dispatch('dblclick');walk(note('note:intro:text')).find(n=>n.tagName==='TEXTAREA').value='# Edited';
        const flush=panel.flushPending();await settle();refuseSteps();await settle();saveNote();
        assert.equal(await flush,false);
    }
    if(SCENARIO==='missing-ids'){
        publish({...view,controls:[{source:'control',key:'7',control_id:'',label:'A',kind:'INT',value:'1',status:'missing_id',order:0},
            {source:'control',key:'8',control_id:'',label:'B',kind:'INT',value:'2',status:'missing_id',order:0},
            {source:'control',key:'9',control_id:'dup',label:'C',kind:'INT',value:'3',status:'duplicate_id',order:0},
            {source:'control',key:'10',control_id:'dup',label:'D',kind:'INT',value:'4',status:'duplicate_id',order:0}],
            notes:[{source:'note',key:'11',note_id:'',text:'one',status:'missing_id',order:0},{source:'note',key:'12',note_id:'',text:'two',status:'missing_id',order:0}]});
        const keyed=walk(container).filter(n=>n.dataset.controlTarget?.includes('-key:')).map(n=>n.dataset.controlTarget);
        assert.deepEqual(keyed.sort(),['control-key:10','control-key:7','control-key:8','control-key:9','note-key:11','note-key:12']);
        assert.ok(walk(container).filter(n=>n.dataset.controlTarget?.startsWith('control-key:')).every(row=>walk(row).find(n=>n.tagName==='INPUT').disabled));
    }
    if(SCENARIO==='loading'){
        publish({state:'loading',controls:[],fields:[],notes:[],error:null});assert.ok(container.textContent.includes('Loading controls'));
        assert.ok(!container.textContent.includes('Add a Sonder Control'));
        publish({state:'ready',controls:[],fields:[],notes:[],error:null});assert.ok(container.textContent.includes('Add a Sonder Control'));
    }
    if(SCENARIO==='removed-failure'||SCENARIO==='removed-pending'){
        refuse=true;defer=SCENARIO==='removed-pending';const seed=input('control:seed:seed');seed.focus();seed.value='bad';key('Enter');await settle();
        publish({...view,controls:view.controls.filter(d=>d.control_id!=='seed')});
        if(defer){resolveWrite();await settle();}refuse=false;defer=false;
        const steps=input('control:steps:value');steps.focus();steps.value='31';assert.equal(await panel.flushPending(),true);
    }
    if(SCENARIO==='truncated'){
        publish({...view,notes:view.notes.map(d=>({...d,note_truncated:true}))});note('note:intro:text').dispatch('dblclick');assert.ok(!walk(note('note:intro:text')).some(n=>n.tagName==='TEXTAREA'));assert.equal(calls.length,0);
    }
    if(['layout','layout-refusal','layout-stale','layout-editor'].includes(SCENARIO)){
        const row=byTarget('control:steps:value'),grip=walk(row).find(n=>n.dataset.panelDragHandle==='true');
        const advanced=walk(container).find(n=>n.tagName==='SUMMARY'&&n.textContent==='Advanced');
        const data={};const transfer={setData(k,v){data[k]=v;},getData(k){return data[k];}};
        if(SCENARIO==='layout-editor')input('control:steps:value').focus();
        container.dispatch('dragstart',{target:grip,dataTransfer:transfer});
        if(SCENARIO==='layout-stale')publish({...view,notes:view.notes.map(d=>({...d,order:8}))});
        refuse=SCENARIO==='layout-refusal';container.dispatch('drop',{target:advanced,dataTransfer:transfer});await settle();
        if(SCENARIO==='layout-stale'||SCENARIO==='layout-editor'){assert.equal(calls.length,0);assert.equal(notices.length,1);}
        else{assert.equal(calls.length,1);assert.equal(calls[0]['control:steps:group'],'Advanced');assert.ok(Number.isInteger(calls[0]['control:steps:order']));
            // A refused arrangement leaves the row in place and never blocks a run.
            if(refuse){assert.equal(row.parentElement.dataset.panelDropGroup,'');assert.equal(notices.length,1);assert.equal(await panel.flushPending(),true);}}
    }
    if(SCENARIO==='combo'||SCENARIO==='combo-capped'||SCENARIO==='combo-ime'||SCENARIO==='combo-query'||SCENARIO==='combo-capped-filter'){
        const options=Array.from({length:30},(_,i)=>'option-'+i);
        publish({...view,controls:[{source:'control',control_id:'combo',kind:'COMBO',label:'Long options',options,options_truncated:SCENARIO.startsWith('combo-capped'),value:'option-0',status:'ok',order:0}]});
        const el=input('control:combo:value');el.focus();el.value=SCENARIO==='combo-capped'?'option-999':SCENARIO==='combo-capped-filter'?'27':'option-27';el.dispatch('input');
        if(SCENARIO==='combo-ime'){el.dispatch('compositionstart');const e=key('Enter',{isComposing:true});assert.equal(e.prevented,false);assert.equal(calls.length,0);el.dispatch('compositionend');}
        if(SCENARIO==='combo-query'){
            el.value='option-2';el.dispatch('input');el.dispatch('change');assert.equal(await panel.flushPending(),true);assert.equal(calls.length,0);el.blur();assert.equal(el.value,'option-0');
        }else{
        const list=walk(byTarget('control:combo:value')).find(n=>n.tagName==='SELECT');
        if(SCENARIO!=='combo-capped')assert.deepEqual(list.options.map(o=>o.value),['option-27']);
        key('Enter');await settle();assert.deepEqual(calls,[{'control:combo:value':SCENARIO==='combo-capped'?'option-999':'option-27'}]);
        el.focus();el.value='draft';el.dispatch('input');key('Escape');assert.equal(el.value,SCENARIO==='combo-capped'?'option-999':'option-27');assert.equal(calls.length,1);
        }
    }
    if(SCENARIO==='combo-capped-exact'||SCENARIO==='combo-capped-current'||SCENARIO==='combo-capped-keyboard'){
        const value=SCENARIO==='combo-capped-current'?'model-27.ckpt':'model-0.ckpt';
        publish({...view,controls:[{source:'control',control_id:'combo',kind:'COMBO',label:'Models',options:['model-27.ckpt.backup','model-0.ckpt'],options_truncated:true,value,status:'ok',order:0}]});
        const el=input('control:combo:value');el.focus();
        if(SCENARIO==='combo-capped-current'){key('Enter');await settle();assert.equal(calls.length,0);assert.equal(el.value,'model-27.ckpt');}
        else{el.value='model-27.ckpt';el.dispatch('input');
            const button=walk(byTarget('control:combo:value')).find(n=>n.tagName==='BUTTON'&&n.textContent==='Use typed value');
            if(SCENARIO==='combo-capped-keyboard'){button.focus();assert.equal(el.value,'model-27.ckpt');key('Enter');}else{button.dispatch('mousedown');button.dispatch('click');}await settle();assert.deepEqual(calls,[{'control:combo:value':'model-27.ckpt'}]);}
    }
    if(SCENARIO==='combo-focus-return'){
        publish({...view,controls:[{source:'control',control_id:'combo',kind:'COMBO',label:'Models',options:['model-27.ckpt','model-0.ckpt'],options_truncated:true,value:'model-0.ckpt',status:'ok',order:0}]});
        const el=input('control:combo:value');el.focus();el.value='27';el.dispatch('input');
        const button=walk(byTarget('control:combo:value')).find(n=>n.tagName==='BUTTON');button.focus();el.focus();
        const list=walk(byTarget('control:combo:value')).find(n=>n.tagName==='SELECT');assert.deepEqual(list.options.map(o=>o.value),['model-27.ckpt']);
        key('Enter');await settle();assert.deepEqual(calls,[{'control:combo:value':'model-27.ckpt'}]);
    }
    if(SCENARIO==='combo-select-focus'){
        publish({...view,controls:[{source:'control',control_id:'combo',kind:'COMBO',label:'Models',options:Array.from({length:30},(_,i)=>'model-'+i),value:'model-0',status:'ok',order:0}]});
        const el=input('control:combo:value');el.focus();const list=walk(byTarget('control:combo:value')).find(n=>n.tagName==='SELECT');
        // Browsers blur a focused native list synchronously when it is hidden.
        let hidden=false;Object.defineProperty(list,'hidden',{get:()=>hidden,set(v){hidden=v;if(v&&document.activeElement===list)list.blur();}});
        list.focus();key('ArrowDown');key('Enter');await settle();assert.deepEqual(calls,[{'control:combo:value':'model-1'}]);
    }
    panel.destroy();assert.equal(listeners.size,0);assert.equal(container.children.length,0);
}
console.log('ok');
'''

@pytest.mark.parametrize('scenario',['combo-select-focus','combo-focus-return','combo-capped-keyboard','combo-capped-exact','combo-capped-current','combo-capped-filter','combo-query','layout','layout-refusal','layout-stale','layout-editor','combo','combo-capped','combo-ime','markdown','grouping','focus','focus-after-change','focus-after-refusal','metadata','cancel','note-save','ime','refusal','timeout','initial-error','flush','mode','cross-target-refusal','removed-failure','removed-pending','truncated','refusal-then-generate','late-success','note-refusal','missing-ids','loading','note-then-value'])
def test_controls_panel(scenario):
    node=shutil.which('node')
    if not node:pytest.skip('node required for Controls renderer boundary tests')
    script=SCRIPT.replace('MARKDOWN_URL',json.dumps((ROOT/'web/js/panel_markdown.js').as_uri())).replace('PANEL_URL',json.dumps((ROOT/'web/js/editor_controls_panel.js').as_uri())).replace('SCENARIO',json.dumps(scenario))
    result=subprocess.run([node,'--input-type=module','-e',script],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='ok'
