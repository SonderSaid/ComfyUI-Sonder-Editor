"""Controls panel fixes from the 0.8 hardening pass, Phase 5 (5A), in the real
modules: the INT input's declared step, the seed's exact up/down steps, a text
box's remembered height, a note's More across a blur, and a workflow switch
refused while a choice loads."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_controls_panel_js import SCRIPT

ROOT = Path(__file__).resolve().parents[1]
PRELUDE = SCRIPT[:SCRIPT.index("if(SCENARIO==='markdown')")]


def run(script, tmp_path, name):
    node = shutil.which('node')
    if not node:
        pytest.skip('node required for Controls renderer boundary tests')
    urls = {'MARKDOWN_URL': 'panel_markdown.js', 'PANEL_URL': 'editor_controls_panel.js',
            'HEADER_URL': 'controls_workflow_header.js', 'STEP_URL': 'controls_seed_step.js',
            'MEMORY_URL': 'disclosure_memory.js'}
    for placeholder, path in urls.items():
        script = script.replace(placeholder, json.dumps((ROOT / 'web/js' / path).as_uri()))
    script = script.replace('SCENARIO', json.dumps(name))
    path = tmp_path / f'{name}.mjs'
    path.write_text(script, encoding='utf-8')
    result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'


# A panel on a host the script shapes: `exact` keeps big integers (the Sonder App).
PANEL = r'''
const store=new Map();
globalThis.localStorage={getItem:k=>store.get(k)??null,setItem(k,v){store.set(k,String(v));}};
const wait=ms=>new Promise(r=>nativeTimeout(r,ms));
const B=(id,label,kind,value,extra={})=>({source:'binding',binding_id:id,label,kind,value,note:'',status:'ok',order:0,group:'',section:'workflow',...extra});
function mount({bindings,exact=false,cards=false}){
    let view={state:'ready',notes:[],bindings};
    const calls=[],listeners=new Set();
    const settings={render:{takePlacementMuted:false,linkedTakePlacement:true},batchRender:{maxFramesPerChunk:121},
        layout:{scaleControls:1},controls:{view:cards?'cards':'rows',showAdvanced:true,showNotes:true}};
    const host={getView:()=>view,subscribe(cb){listeners.add(cb);return()=>listeners.delete(cb);},getEditorSettings:()=>settings,
        setEditorSetting(){},notify(){},async applyChanges(changes){calls.push(changes);return {ok:true,rejected:[]};},
        ...(exact?{capabilities:{hide:true,removeAuthored:false,serverRows:true,exactIntegers:true}}:{})};
    const container=new El('div');document.body.append(container);
    const panel=mountControlsPanel(container,host);
    const row=t=>walk(container).find(n=>n.dataset.controlTarget===t);
    const field=t=>walk(row(t)).find(n=>n.tagName==='INPUT'||n.tagName==='TEXTAREA');
    const step=(t,dir)=>walk(row(t)).find(n=>n.dataset.seedStep===dir);
    const publish=next=>{view={...view,...next};for(const cb of listeners)cb(view);};
    return {panel,container,calls,row,field,step,publish,settings};
}
const key=(value,extra={})=>{const e={key:value,keyCode:0,isComposing:false,ctrlKey:false,metaKey:false,altKey:false,prevented:false,stopped:false,target:document.activeElement,
    preventDefault(){this.prevented=true;},stopImmediatePropagation(){this.stopped=true;},...extra};for(const f of events.keydown||[]){f(e);if(e.stopped)break;}return e;};
'''

SEED = PANEL + r'''
const {stepSeed,seedBounds,intStep,CANVAS_INTEGER_CEILING,SEED_MAX}=await import(STEP_URL);
// The pure arithmetic: exact past 2^53, bounded, never the wrong way.
const app=seedBounds({},null);
assert.equal(stepSeed('9007199254740993',1,app),'9007199254740994');
assert.equal(stepSeed(SEED_MAX,1,app),null);
assert.equal(stepSeed('18446744073709551614',1,app),SEED_MAX);
assert.equal(stepSeed('0',-1,app),null);
assert.equal(stepSeed('abc',1,app),null);assert.equal(stepSeed('',1,app),null);assert.equal(stepSeed('1e5',1,app),null);
const canvas=seedBounds({max:SEED_MAX});
assert.equal(stepSeed(CANVAS_INTEGER_CEILING,1,canvas),null,'a canvas host stops at 2^53 - 1');
assert.equal(stepSeed('9007199254740995',1,canvas),null,'a seed past the ceiling never steps up');
assert.equal(stepSeed('9007199254740995',-1,canvas),CANVAS_INTEGER_CEILING,'it steps back in');
assert.equal(stepSeed('10',1,seedBounds({step:'5',min:'0',max:'12'})),'12');
assert.equal(intStep({step:'32'}),'32');assert.equal(intStep({step:'0'}),'1');assert.equal(intStep({step:'1.5'}),'1');assert.equal(intStep({}),'1');

// A canvas host: the INT input takes the node's declared step.
const c=mount({bindings:[B('tile','Tile size','INT','1024',{min:'64',max:'4096',step:'32'}),
    B('seed','Seed','SEED','9',{seed_mode:'fixed',min:'0',max:SEED_MAX})]});
assert.equal(c.field('binding:tile:value').step,'32');
c.publish({bindings:[B('tile','Tile size','INT','1024',{min:'64',max:'4096',step:'8'}),B('seed','Seed','SEED','9',{seed_mode:'fixed',min:'0',max:SEED_MAX})]});
await settle();assert.equal(c.field('binding:tile:value').step,'8','a new declared step reaches the input');
// Steps show at once; a burst saves once, after the pause.
c.step('binding:seed:value','up').dispatch('click');
c.step('binding:seed:value','up').dispatch('click');
c.step('binding:seed:value','up').dispatch('click');
assert.equal(c.field('binding:seed:value').value,'12');assert.equal(c.calls.length,0);
await wait(450);await settle();
assert.deepEqual(c.calls,[{'binding:seed:value':'12'}]);
assert.equal(c.step('binding:seed:value','up').attributes['aria-label'],'Increase Seed');
// At the canvas ceiling Up is off, Down is on; a field that isn't a whole number turns both off.
c.publish({bindings:[B('seed','Seed','SEED',CANVAS_INTEGER_CEILING,{seed_mode:'fixed',min:'0',max:SEED_MAX})]});await settle();
assert.equal(c.step('binding:seed:value','up').disabled,true);assert.equal(c.step('binding:seed:value','down').disabled,false);
const seedField=c.field('binding:seed:value');
seedField.focus();seedField.value='12ab';seedField.dispatch('input');
assert.equal(c.step('binding:seed:value','up').disabled,true);assert.equal(c.step('binding:seed:value','down').disabled,true);
// ArrowUp in the field steps it (the panel's key handler, not the browser's).
seedField.value='41';seedField.dispatch('input');
let e=key('ArrowUp');assert.ok(e.stopped&&e.prevented);assert.equal(seedField.value,'42');
// A waiting step is saved by flushPending, before any run.
const before=c.calls.length;
assert.equal(await c.panel.flushPending(),true);
assert.deepEqual(c.calls.slice(before),[{'binding:seed:value':'42'}]);
c.panel.destroy();

// The Sonder App keeps all 64 bits.
const a=mount({exact:true,bindings:[B('seed','Seed','SEED','18446744073709551614',{seed_mode:'fixed',min:'0',max:SEED_MAX})]});
a.step('binding:seed:value','up').dispatch('click');
assert.equal(a.field('binding:seed:value').value,SEED_MAX);
assert.equal(a.step('binding:seed:value','up').disabled,true);
await a.panel.flushPending();assert.deepEqual(a.calls,[{'binding:seed:value':SEED_MAX}]);
// A row that can't be edited offers no steps.
a.publish({bindings:[B('seed','Seed','SEED','5',{seed_mode:'fixed',read_only:true})]});await settle();
assert.equal(a.step('binding:seed:value','up').disabled,true);assert.equal(a.step('binding:seed:value','down').disabled,true);
a.panel.destroy();
console.log('ok');
'''

SEED_SAVES = PANEL + r'''
// A step saves against the value stored before its burst, whatever the field's
// focus history (implementation audit HIGH-1), and typing replaces it (MED-2).
const S=v=>[B('seed','Seed','SEED',v,{seed_mode:'fixed',min:'0',max:'18446744073709551615'})];
const s=mount({bindings:S('9')});
const f=()=>s.field('binding:seed:value'),up=()=>s.step('binding:seed:value','up'),down=()=>s.step('binding:seed:value','down');
// Focused once at 9, then the seed moves elsewhere to 10: Decrease to 9 still saves.
f().focus();f().blur();
s.publish({bindings:S('10')});await settle();assert.equal(f().value,'10');
down().dispatch('click');await wait(450);await settle();
assert.deepEqual(s.calls,[{'binding:seed:value':'9'}],'a step back to an old focus value still saves');
// A focus inside the burst, then a run: the step is saved first.
s.publish({bindings:S('9')});await settle();s.calls.length=0;
up().dispatch('click');f().focus();
assert.equal(await s.panel.flushPending(),true);
assert.deepEqual(s.calls,[{'binding:seed:value':'10'}],'Generate never runs the older seed');
f().blur();
// Typing during a burst cancels the timer: no half-typed save.
s.publish({bindings:S('10')});await settle();s.calls.length=0;
up().dispatch('click');f().focus();f().value='1';f().dispatch('input');
await wait(450);await settle();assert.deepEqual(s.calls,[],'the timer never saves half-typed text');
f().value='';f().dispatch('input');f().blur();
// Closing the panel saves a waiting step.
s.publish({bindings:S('20')});await settle();s.calls.length=0;
up().dispatch('click');s.panel.destroy();await settle();
assert.deepEqual(s.calls,[{'binding:seed:value':'21'}]);
console.log('ok');
'''

TEXT_HEIGHT = PANEL + r'''
const {createValueMemory}=await import(MEMORY_URL);
// The value store: per-key patch, newest `limit` kept, junk ignored.
let clock=0;const mem=createValueMemory('k',{limit:2,now:()=>++clock});
mem.remember('a',100);mem.remember('b',200);
const other=createValueMemory('k',{limit:2,now:()=>++clock});other.remember('c',300);
const kept=JSON.parse(store.get('k'));assert.deepEqual(Object.keys(kept).sort(),['b','c'],'the oldest goes; a second store keeps the first one\'s key');
mem.remember('d',Number.NaN);assert.equal(createValueMemory('k').value('d'),null);
store.set('k','{"x":{"v":"tall"}}');assert.equal(createValueMemory('k').value('x'),null);

const prompt=()=>[B('prompt','Prompt','STRING','a cat',{multiline:true})];
const p=mount({bindings:prompt()});
const box=p.field('binding:prompt:value');assert.equal(box.tagName,'TEXTAREA');
assert.equal(box.style.height,'','no remembered height: the default');
// A drag on the box's handle: the browser writes the height, the release saves it.
box.dispatch('pointerdown');box.style.height='240px';
p.panel.render();assert.equal(box.style.height,'240px','a render mid-drag keeps the live height');
for(const f of events.pointerup||[])f({});
assert.equal(JSON.parse(store.get('sonder-controls-text-height-v1'))['text:prompt'].v,240);
p.panel.render();assert.equal(box.style.height,'240px','a render puts it back');
p.panel.destroy();
// A remount (leaving and re-entering the editor) restores it.
const q=mount({bindings:prompt()});
assert.equal(q.field('binding:prompt:value').style.height,'240px');
// Heights are clamped when saved and when read.
const tall=q.field('binding:prompt:value');tall.dispatch('pointerdown');tall.style.height='5000px';for(const f of events.pointerup||[])f({});
assert.equal(JSON.parse(store.get('sonder-controls-text-height-v1'))['text:prompt'].v,1200);
q.panel.destroy();
store.set('sonder-controls-text-height-v1',JSON.stringify({'text:prompt':{v:10,ts:1}}));
const r=mount({bindings:prompt()});assert.equal(r.field('binding:prompt:value').style.height,'60px');
r.panel.destroy();
console.log('ok');
'''

MORE_AFTER_BLUR = PANEL + r'''
// Frames run when the test says so: the bug lived between a render and its measure.
const frames=[];globalThis.requestAnimationFrame=f=>{frames.push(f);return frames.length;};
const runFrames=()=>{while(frames.length)frames.shift()();};
const m=mount({cards:true,bindings:[B('steps','Steps','INT','20',{note:'A long note that runs past two lines of the card.'}),B('cfg','CFG','FLOAT','7')]});
const note=walk(m.row('binding:steps:value')).find(n=>n.dataset.noteTarget==='binding:steps:note');
const more=()=>walk(m.row('binding:steps:value')).find(n=>n.dataset.noteMore==='true');
assert.equal(more().hidden,true,'a new More starts hidden');
note.scrollHeight=60;note.clientHeight=30;runFrames();
assert.equal(more().hidden,false,'shown once the note measures as overflowing');
// Focus a field, then press More: the press blurs the field, which renders the panel.
const cfg=m.field('binding:cfg:value');cfg.focus();cfg.blur();
assert.equal(more().hidden,false,'the blur render leaves More pressable');
more().dispatch('click');assert.equal(more().textContent,'Less');
runFrames();assert.equal(more().hidden,false);
// Leaving Cards hides it at once.
m.settings.controls.view='rows';m.panel.render();assert.equal(more().hidden,true);
m.panel.destroy();
console.log('ok');
'''

SWITCH_BUSY = r'''
const {mountWorkflowHeader}=await import(HEADER_URL);
const calls=[],notices=[];let busy='',flushes=0,confirms=0,leave=true;
const wf={current:{template_id:'A',name:'Alpha'}};
const workflows={current:()=>wf.current,busy:()=>false,
    list:async()=>({ok:true,payload:{current:'A',workflows:[{template_id:'A',name:'Alpha',ok:true},{template_id:'B',name:'Beta',ok:true}]}}),
    switchTo:async id=>{calls.push(['switch',id]);return {ok:true};},setFavorite:async()=>({ok:true})};
const view={state:'ready',description:'',presets:[],bindings:[]};
const section=mountWorkflowHeader({getView:()=>view,workflows},{notify:m=>notices.push(m),busyReason:()=>busy,
    canSwitch:()=>{confirms++;return leave;},flushPending:async()=>{flushes++;return true;}});
document.body.append(section.el);section.render(view);await settle();
const select=walk(section.el).find(n=>n.tagName==='SELECT');
// A choice still loading: refused at once, nothing saved, nothing asked, the menu back on Alpha.
busy='Wait for the choice to finish loading, then switch workflows.';
select.value='B';select.dispatch('change');await settle();
assert.deepEqual(calls,[]);assert.equal(flushes,0);assert.equal(confirms,0);
assert.equal(notices.at(-1),busy);assert.equal(select.value,'A');
// The load ends: the switch goes ahead, with one save and one confirm.
busy='';select.value='B';select.dispatch('change');await settle();
assert.deepEqual(calls,[['switch','B']]);assert.equal(flushes,1);assert.equal(confirms,1);
// A load that starts while the save runs is caught after it.
calls.length=0;flushes=0;confirms=0;
const section2=mountWorkflowHeader({getView:()=>view,workflows},{notify:m=>notices.push(m),busyReason:()=>busy,
    canSwitch:()=>{confirms++;return true;},flushPending:async()=>{flushes++;busy='Wait for the Manage change to save, then switch workflows.';return true;}});
section.destroy();document.body.append(section2.el);section2.render(view);await settle();
const select2=walk(section2.el).find(n=>n.tagName==='SELECT');busy='';
select2.value='B';select2.dispatch('change');await settle();
assert.deepEqual(calls,[]);assert.equal(flushes,1);assert.equal(confirms,0);assert.match(notices.at(-1),/Manage change/);assert.equal(select2.value,'A');
section2.destroy();
console.log('ok');
'''


def test_seed_steps_and_int_step(tmp_path):
    run(PRELUDE + SEED, tmp_path, 'phase5-seed')


def test_seed_steps_save_against_the_stored_value(tmp_path):
    run(PRELUDE + SEED_SAVES, tmp_path, 'phase5-seed-saves')


def test_text_box_height_is_remembered(tmp_path):
    run(PRELUDE + TEXT_HEIGHT, tmp_path, 'phase5-text-height')


def test_note_more_survives_a_blur_render(tmp_path):
    run(PRELUDE + MORE_AFTER_BLUR, tmp_path, 'phase5-more')


def test_workflow_switch_is_refused_while_busy(tmp_path):
    run(PRELUDE + SWITCH_BUSY, tmp_path, 'phase5-switch')
