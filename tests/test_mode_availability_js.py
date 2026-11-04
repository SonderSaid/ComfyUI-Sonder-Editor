"""Mode availability in the browser (0.8 hardening, Phase 12A): the words, the
reader, the Modes table, and the grey click through the real panel's flush and
track, which is where a re-check could wait for itself."""
import json
from pathlib import Path

from test_controls_modes_js import PRELUDE, run

ROOT = Path(__file__).resolve().parents[1]


def urls(script):
    for placeholder, name in {'AVAIL_URL': 'controls_modes_availability.js', 'READER_URL': 'controls_availability_reader.js',
                              'REMOTE_URL': 'controls_host_remote.js'}.items():
        script = script.replace(placeholder, json.dumps((ROOT / 'web/js' / name).as_uri()))
    return script


COMMON = r'''
const wait=ms=>new Promise(r=>setTimeout(r,ms));
const tick=async()=>{for(let i=0;i<12;i++)await Promise.resolve();await wait(0);};
const model=(file,extra={})=>({file,folder:'loras',relative:`models/loras/${file}`,node:'Load LoRA',input:'lora_name',...extra});
const answer=(choices={},run={},seq=1,template_id='T')=>({ok:true,template_id,seq,run:{missing_nodes:[],missing_models:[],problems:[],...run},choices});
'''

WORDS = r'''
const {needsOf,needsText,needsLines,runSummary,hasEntries,namesANode}=await import(AVAIL_URL);
const packs={'pack-key':[['PromptRelaySmartEncode'],{title_aux:'ComfyUI-PromptRelay'}]};
const needs={missing_nodes:['PromptRelaySmartEncode'],missing_models:[]};
assert.equal(needsText(needs,packs),'Needs the node pack ComfyUI-PromptRelay (PromptRelaySmartEncode).');
assert.equal(needsText(needs,null),'Needs the node PromptRelaySmartEncode.','no Manager: the class alone');
assert.equal(needsText({missing_nodes:['Unknown'],missing_models:[]},packs),'Needs the node Unknown.','a class no pack lists');
assert.equal(needsText({missing_nodes:[],missing_models:[model('a.safetensors')]},null),'Needs a.safetensors → models/loras/a.safetensors (Load LoRA)');
assert.equal(needsText({missing_nodes:[],missing_models:[model('a.safetensors',{path:'D:\\m\\a.safetensors'})]},null),'Needs a.safetensors → D:\\m\\a.safetensors (Load LoRA)','the place on this machine');
assert.match(needsText({missing_nodes:[],missing_models:[model('None')]},null),/^A LoRA row in Load LoRA reads/,'a row naming no file is a whole sentence');
assert.equal(needsText(null,null),'');
const many={missing_nodes:[],missing_models:['a','b','c','d','e','f'].map(n=>model(`${n}.safetensors`))};
assert.equal(needsText(many,null).split('\n').length,5);assert.match(needsText(many,null),/and 2 more$/);
assert.equal(needsOf(answer({x:needs}),'x'),needs);assert.equal(needsOf(answer(),'x'),null);assert.equal(needsOf(null,'x'),null);
// The line above Modes.
assert.equal(runSummary(answer(),null),null,'nothing lacking: no line');assert.equal(runSummary(null,null),null);
const lacking=runSummary(answer({},{missing_models:many.missing_models,problems:['Switch a Save on.']}),null);
assert.equal(lacking.title,"This workflow can't run with the current Modes: 6 files missing.");
assert.equal(lacking.needs.length,6,'the whole list, opened on request');assert.deepEqual(lacking.problems,['Switch a Save on.']);
assert.equal(runSummary(answer({},{missing_nodes:['N'],missing_models:[model('a.safetensors')]}),null).title,"This workflow can't run with the current Modes: 1 file and 1 node missing.");
assert.equal(runSummary(answer({},{problems:['x']}),null,{hasModes:false}).title,"This workflow can't run yet.");
assert.equal(namesANode(answer({x:needs})),true);assert.equal(namesANode(answer({},{missing_nodes:['N']})),true);assert.equal(namesANode(answer()),false);
assert.equal(hasEntries(answer()),false);assert.equal(hasEntries(answer({x:needs})),true);assert.equal(hasEntries(answer({},{problems:['x']})),true);
console.log('ok');
'''

READER = r'''
const {createAvailabilityReader,AVAILABILITY_DEBOUNCE_MS:DEBOUNCE}=await import(READER_URL);
const packsRead=[],reads=[];let reply=()=>answer(),on=true,seq=1,template='T',gate=null;
const reader=createAvailabilityReader({active:()=>on,viewSeq:()=>seq,templateId:()=>template,
    loadPacks:async()=>{packsRead.push(1);return {p:[['N'],{title:'Pack'}]};},
    request:async fresh=>{reads.push(fresh);if(gate)await gate.promise;const r=reply();if(r instanceof Error)throw r;return r;}});
const told=[];reader.subscribe(()=>told.push(reader.get()));
// Debounced: a burst of triggers is one read, and a fresh one stays fresh.
reader.schedule();reader.schedule(true);reader.schedule();await wait(DEBOUNCE+40);
assert.deepEqual(reads,[true]);
assert.equal(told.length,1,'null to an answer is a change');
// The same content again tells nobody, so a repaint never follows it.
reader.schedule();await wait(DEBOUNCE+40);assert.equal(reads.length,2);assert.equal(told.length,1);
// Nobody can see it: no read at all.
on=false;reader.schedule(true);await wait(DEBOUNCE+40);assert.equal(reads.length,2);on=true;
// A newer view than the answer: dropped, and what's shown stays.
reply=()=>answer({x:{missing_nodes:['N'],missing_models:[]}},{},0);seq=1;
assert.equal(await reader.refresh(),false);assert.deepEqual(reader.get().choices,{});
// Another workflow's answer shows nothing.
reply=()=>answer({x:{missing_nodes:['N'],missing_models:[]}},{},1,'OTHER');
assert.equal(await reader.refresh(),false);assert.equal(reader.get(),null);
// A real answer naming a node: pack names load once, and tell subscribers.
reply=()=>answer({x:{missing_nodes:['N'],missing_models:[]}});told.length=0;
assert.equal(await reader.refresh({fresh:true}),true);await tick();
assert.equal(packsRead.length,1);assert.deepEqual(Object.keys(reader.packs()),['p']);assert.equal(told.length,2,'the answer, then the pack names');
await reader.refresh();await tick();assert.equal(packsRead.length,1,'once per page');
// A failed read clears: nothing grey, and the next trigger tries again.
reply=()=>new Error('down');assert.equal(await reader.refresh(),false);assert.equal(reader.get(),null);
// One in flight, one queued: three triggers while reading make one more read.
reply=()=>answer();await reader.refresh();reads.length=0;gate=deferred();
const inFlight=reader.refresh();await tick();assert.equal(reads.length,1);
reader.schedule();await wait(DEBOUNCE+40);reader.schedule();await wait(DEBOUNCE+40);assert.equal(reads.length,1);
const second=gate;gate=null;second.resolve();await inFlight;await tick();assert.equal(reads.length,2);
// A refresh during a read waits for a read begun after it.
reads.length=0;gate=deferred();const early=reader.refresh();await tick();
reply=()=>answer({y:{missing_nodes:[],missing_models:[model('y.safetensors')]}});
const late=reader.refresh({fresh:true});const release=gate;gate=null;release.resolve();
assert.equal(await late,true);assert.deepEqual(reads,[false,true]);assert.deepEqual(Object.keys(reader.get().choices),['y']);await early;
// Out of time: false, and an answer arriving later still lands harmlessly.
gate=deferred();const started=Date.now();assert.equal(await reader.refresh({fresh:true,timeoutMs:30}),false);assert.ok(Date.now()-started<500);
reply=()=>answer();const lateGate=gate;gate=null;lateGate.resolve();await tick();assert.deepEqual(reader.get().choices,{});
// Destroyed: a pending trigger and an answer in flight do nothing.
reads.length=0;reader.schedule(true);gate=deferred();reader.destroy();await wait(DEBOUNCE+40);assert.equal(reads.length,0);
assert.equal(await reader.refresh(),false);
console.log('ok');
'''

TABLE = r'''
const {mountModes}=await import(MODES_URL);
let overlay=answer({b:{missing_nodes:['N'],missing_models:[]}});
let listeners=new Set();
const view={state:'ready',presets:[P('a','match',{description:'About A'}),P('b','differs',{description:'About B'}),P('d','unavailable',{reason:'Repair it in Manage.'})],preset_categories:[C('x','Task',['a','b','d'])]};
const availability={get:()=>overlay,packs:()=>null,subscribe(cb){listeners.add(cb);return()=>listeners.delete(cb);},refresh:async()=>true};
const notices=[];
const host={getView:()=>view,availability,freshView:async()=>({view,fresh:true}),applyChanges:async()=>({ok:true})};
const modes=mountModes(host,{flushPending:async()=>true,track:work=>work,notify:m=>notices.push(m)});
document.body.append(modes.el);modes.render(view,{});
const choice=id=>walk(modes.el).find(n=>n.dataset.controlsModeChoice===id);
const line=()=>walk(modes.el).find(n=>n.dataset.controlsModesRun==='true');
const toggle=()=>walk(modes.el).find(n=>n.dataset.controlsModesRunToggle==='true');
const list=()=>walk(modes.el).find(n=>n.dataset.controlsModesRunList==='true');
// Grey: dimmed, marked, focusable, and not selected or unselected by this.
assert.equal(choice('b').style.opacity,'0.42');assert.equal(choice('b').attributes['aria-disabled'],'true');
assert.equal(choice('b').style.cursor,'not-allowed');assert.notEqual(choice('b').disabled,true);
assert.equal(choice('a').style.opacity,'');assert.equal(choice('a').attributes['aria-disabled'],undefined);
assert.equal(choice('a').attributes['aria-pressed'],'true');
// What it needs comes first in what's read out; its description follows.
const said=id=>walk(modes.el).find(n=>n.id===choice(id).attributes['aria-describedby'])?.textContent;
assert.equal(said('b'),'Needs the node N.\nAbout B');assert.equal(said('a'),'About A');
assert.equal(said('d'),'Repair it in Manage.');
// An unusable choice with an install to name says that first; a click notices both.
overlay=answer({b:{missing_nodes:['N'],missing_models:[]},d:{missing_nodes:['Gone'],missing_models:[]}});modes.render(view,{});
assert.equal(said('d'),'Needs the node Gone.\nRepair it in Manage.');
choice('d').dispatch('click');await tick();assert.deepEqual(notices,['Needs the node Gone. Repair it in Manage.']);
// An open ? block follows the answer.
walk(modes.el).find(n=>n.dataset.controlsModeMore==='true').dispatch('click');
const block=()=>walk(modes.el).find(n=>n.dataset.controlsModeDetails==='true');
assert.match(block().textContent,/Needs the node N\./);
overlay=answer();for(const cb of listeners)cb();
assert.doesNotMatch(block().textContent,/Needs the node/);assert.equal(choice('b').style.opacity,'','the answer alone repaints');
// The line above Modes: only when the run lacks something, and not in Manage or search.
assert.equal(line().style.display,'none');
overlay=answer({},{missing_models:['a','b','c','d','e'].map(n=>model(`${n}.safetensors`))});modes.render(view,{});
assert.equal(line().style.display,'');assert.match(line().textContent,/^This workflow can't run with the current Modes: 5 files missing\./);
// Quiet by default, since paths are long: the table stays in view. The list opens on request.
assert.equal(list().style.display,'none');assert.equal(toggle().textContent,'Show');assert.equal(toggle().attributes['aria-expanded'],'false');
toggle().dispatch('click');assert.equal(list().style.display,'');assert.match(list().textContent,/^Needs a\.safetensors.*\n.*\n.*\n.*\nNeeds e\.safetensors/);
assert.equal(toggle().textContent,'Hide');toggle().dispatch('click');assert.equal(list().style.display,'none');
// A problem is a sentence already, so it shows, with nothing to open.
overlay=answer({},{problems:['Switch a Save on.']});modes.render(view,{});
assert.equal(list().textContent,'Switch a Save on.');assert.equal(toggle().style.display,'none');
overlay=answer({},{missing_models:[model('a.safetensors')]});modes.render(view,{});
modes.render(view,{manage:true});assert.equal(line().style.display,'none');
modes.render(view,{query:'x'});assert.equal(line().style.display,'none');
// No Modes at all: the section still shows the line, with its own words.
const none={state:'ready',presets:[],preset_categories:[]};modes.render(none,{});
assert.equal(modes.el.style.display,'flex');assert.equal(line().style.display,'');assert.match(line().textContent,/^This workflow can't run yet: 1 file missing\./);
overlay=answer();modes.render(none,{});assert.equal(modes.el.style.display,'none');
modes.render(view,{});
modes.destroy();assert.equal(listeners.size,0,'destroy lets go of the host');
// A host with no availability (canvas, mounted) draws as it always did.
{const plain=mountModes({getView:()=>view},{flushPending:async()=>true,track:w=>w,notify:()=>{}});document.body.append(plain.el);plain.render(view,{});
const c=walk(plain.el).find(n=>n.dataset.controlsModeChoice==='b');
assert.equal(c.style.opacity,'');assert.equal(c.attributes['aria-disabled'],undefined);assert.equal(c.style.cursor,'pointer');
assert.equal(walk(plain.el).find(n=>n.dataset.controlsModesRun==='true').style.display,'none');plain.destroy();}
console.log('ok');
'''

# Through the real panel: its flushPending awaits everything tracked, and a choice
# apply flushes before it tracks, so a re-check tracked too early waits for itself.
PANEL = r'''
const view={state:'ready',notes:[],execution:[],relay:{status:'ok'},
    bindings:[{source:'binding',binding_id:'steps',label:'Steps',kind:'INT',value:'20',status:'ok',order:1,group:'',section:'workflow',note:'',min:1,max:100}],
    presets:[P('a','match'),P('b')],preset_categories:[C('x','Task',['a','b'])],groups:[]};
let overlay=answer({b:{missing_nodes:[],missing_models:[model('b.safetensors')]}},{},1);
const calls=[],notices=[],refreshes=[],availabilityListeners=new Set(),viewListeners=new Set();let refreshGate=null,refreshResult=true,nextOverlay=null,saving=null,order=[];
const availability={get:()=>overlay,packs:()=>null,subscribe(cb){availabilityListeners.add(cb);return()=>availabilityListeners.delete(cb);},
    refresh:async opts=>{refreshes.push(opts);order.push('recheck');if(refreshGate)await refreshGate.promise;
        if(nextOverlay!==null){overlay=nextOverlay;for(const cb of availabilityListeners)cb();}return refreshResult;}};
const host={getView:()=>view,availability,subscribe(cb){viewListeners.add(cb);return()=>viewListeners.delete(cb);},
    getEditorSettings:()=>({controls:{view:'rows'},layout:{scaleControls:1}}),notify:m=>notices.push(m),inform:()=>{},
    freshView:async()=>({view,fresh:true}),
    async applyChanges(changes){calls.push(changes);order.push(Object.keys(changes)[0]);if('binding:steps:value' in changes&&saving)await saving.promise;return {ok:true,rejected:[]};}};
const container=new El('div');document.body.append(container);const panel=mountControlsPanel(container,host);
const choice=id=>walk(container).find(n=>n.dataset.controlsModeChoice===id);
// Still grey: nothing written, the notice says why, and Generate waiting on the
// click is refused, as for any choice that didn't apply. No self-wait: this settles.
refreshGate=deferred();choice('b').dispatch('click');await tick();
assert.equal(refreshes.length,1,'re-checked');assert.deepEqual(refreshes[0].fresh,true);assert.ok(refreshes[0].timeoutMs>0);
const waiting=panel.flushPending();await tick();
refreshGate.resolve();refreshGate=null;
assert.equal(await waiting,false,'a Generate waiting on the click is refused');
await tick();assert.deepEqual(calls,[]);assert.equal(notices.at(-1),'Needs b.safetensors → models/loras/b.safetensors (Load LoRA)');
assert.equal(await panel.flushPending(),true,'nothing left waiting, so Generate is free again');
// The answer changed: the file arrived, so the same click applies.
notices.length=0;nextOverlay=answer();choice('b').dispatch('click');await tick();await tick();
assert.deepEqual(calls,[{'preset:b:apply':true}]);assert.deepEqual(notices,[]);nextOverlay=null;
// The read failed or ran out of time: Generate stays the refusal, so the click applies.
calls.length=0;overlay=answer({b:{missing_nodes:['N'],missing_models:[]}});for(const cb of availabilityListeners)cb();
refreshResult=false;choice('b').dispatch('click');await tick();await tick();
assert.deepEqual(calls,[{'preset:b:apply':true}]);refreshResult=true;
// A selected grey choice still says why, though applying it again would do nothing.
calls.length=0;notices.length=0;overlay=answer({a:{missing_nodes:['N'],missing_models:[]}});for(const cb of availabilityListeners)cb();
choice('a').dispatch('click');await tick();await tick();
assert.deepEqual(calls,[]);assert.deepEqual(notices,['Needs the node N.']);
// An edit typed just before the click is saved first, so the re-check sees it.
calls.length=0;order=[];saving=deferred();overlay=answer({b:{missing_nodes:['N'],missing_models:[]}});for(const cb of availabilityListeners)cb();
const field=walk(container).find(n=>n.dataset.controlTarget==='binding:steps:value');const input=walk(field).find(n=>n.tagName==='INPUT');
input.focus();input.value='33';input.dispatch('change');await tick();
choice('b').dispatch('click');await tick();assert.deepEqual(order,['binding:steps:value'],'the re-check waits for the save');
saving.resolve();saving=null;await tick();await tick();assert.deepEqual(order,['binding:steps:value','recheck']);
panel.destroy();assert.equal(availabilityListeners.size,0);console.log('ok');
'''

HOST = r'''
const {createSdkControlsHost,createMountedControlsHost}=await import(REMOTE_URL);
const {AVAILABILITY_DEBOUNCE_MS:DEBOUNCE,AVAILABILITY_WRITE_DEBOUNCE_MS:WRITE}=await import(READER_URL);
const paths=[],refusals=[];let seq=1,overlayBody=()=>answer(),availabilityStatus=200,presets=[];
const fetchImpl=async url=>{paths.push(url);
    if(url.startsWith('/s/availability'))return {ok:availabilityStatus===200,status:availabilityStatus,json:async()=>availabilityStatus===200?overlayBody():{code:'session_ended'}};
    return {ok:true,status:200,json:async()=>({ok:true,seq,template_id:'T',view:{bindings:[],notes:[],presets,preset_categories:[]}})};};
const owner={_sessionPath:s=>`/s/${s}`,_fetch:fetchImpl,headers:()=>({}),_noteRefusal:r=>refusals.push(r.payload.code),templateId:'T',canWrite:()=>true};
const host=createSdkControlsHost(owner);
const avail=()=>paths.filter(p=>p.startsWith('/s/availability'));
const views=()=>paths.filter(p=>p.startsWith('/s/controls'));
const settleReads=async()=>{await wait(DEBOUNCE+60);await tick();};
const settleWrite=async()=>{await wait(WRITE+60);await tick();};
assert.ok(host.availability,'the Sonder App has the read');
// Not visible: nothing reads. A pane's demand for the view alone doesn't either.
await settleReads();assert.deepEqual(avail(),[]);
host.setViewsDemand(true);await settleReads();assert.deepEqual(avail(),[],'Views reading the view is no trigger');
host.setViewsDemand(false);
// Shown: one read, not fresh.
host.setVisible(true);await settleReads();assert.deepEqual(avail(),['/s/availability']);
// A new seq reads again; the same seq doesn't.
paths.length=0;await host.refresh(true);await settleReads();assert.deepEqual(avail(),[]);
// It waits for a pause, since each answer is work: a burst of writes is one read.
seq=2;await host.refresh(true);await wait(WRITE/2);seq=3;await host.refresh(true);await wait(WRITE/2);assert.deepEqual(avail(),[]);
await settleWrite();assert.deepEqual(avail(),['/s/availability']);seq=2;
// Manage hides the table: a new seq reads nothing until it closes.
seq=3;paths.length=0;host.setManage(true);seq=4;await host.refresh(true);await settleWrite();assert.deepEqual(avail(),[]);
host.setManage(false);await settleReads();assert.equal(avail().length,1);
// Back at the tab: fresh, and with nothing missing, no extra view read.
paths.length=0;for(const f of events.focus||[])f();await settleReads();
assert.deepEqual(avail(),['/s/availability?fresh=1']);assert.deepEqual(views(),[]);
// Something missing: the view is read too, since an unusable choice's state lives there.
overlayBody=()=>answer({x:{missing_nodes:['N'],missing_models:[]}},{},4);await host.availability.refresh();await tick();
paths.length=0;for(const f of docEvents.visibilitychange||[])f();await settleReads();
assert.deepEqual(avail(),['/s/availability?fresh=1']);assert.equal(views().length,1);
// A refused choice with no install to name still gets its view read: its state lives there.
overlayBody=()=>answer({},{},4);await host.availability.refresh();await tick();
presets=[P('r','refused')];seq=5;await host.refresh(true);overlayBody=()=>answer({},{},5);await settleWrite();
paths.length=0;for(const f of events.focus||[])f();await settleReads();assert.equal(views().length,1,'an unusable choice may have just been freed');
presets=[];seq=6;await host.refresh(true);overlayBody=()=>answer({},{},6);await settleWrite();
// A session that ended says so through this read too.
availabilityStatus=410;assert.equal(await host.availability.refresh(),false);assert.deepEqual(refusals,['session_ended']);assert.equal(host.availability.get(),null);availabilityStatus=200;
// Hidden tab: nothing.
paths.length=0;document.hidden=true;for(const f of docEvents.visibilitychange||[])f();await settleReads();assert.deepEqual(paths,[]);document.hidden=false;
// An overlay change never goes through the main emit, so Views doesn't read for it.
let emitted=0;const off=host.subscribe(()=>emitted++);let told=0;host.availability.subscribe(()=>told++);
overlayBody=()=>answer({x:{missing_nodes:[],missing_models:[model('x.safetensors')]}},{},6);await host.availability.refresh();await tick();assert.equal(told,1);assert.equal(emitted,0);off();
// Destroyed: its listeners are gone, and a retired host never reads again.
const focusBefore=(events.focus||[]).length,visBefore=(docEvents.visibilitychange||[]).length;
host.availability.schedule();host.destroy();
assert.equal((events.focus||[]).length,focusBefore-1);assert.equal((docEvents.visibilitychange||[]).length,visBefore-1);
paths.length=0;await settleReads();assert.deepEqual(paths,[]);
// A mounted host has no such read: on the canvas, ComfyUI's Run validates the graph.
{const mounted=createMountedControlsHost({projectId:'p',hostId:'h',sessionId:'s',apiURL:u=>u,fetchImpl:async()=>({ok:false,status:500,json:async()=>({})})});
assert.equal(mounted.availability,undefined);mounted.destroy();}
console.log('ok');
'''


# The real host, reader and panel together, over a fake server: only fetch is faked.
INTEGRATION = r'''
const {createSdkControlsHost}=await import(REMOTE_URL);
let missing=true,seq=1;const puts=[],notices=[];
const viewOf=()=>({state:'ready',notes:[],execution:[],relay:{status:'ok'},bindings:[],groups:[],presets:[P('a','match'),P('b')],preset_categories:[C('x','Task',['a','b'])]});
const fetchImpl=async(url,options={})=>{
    if(url.startsWith('/s/availability'))return {ok:true,status:200,json:async()=>answer(missing?{b:{missing_nodes:[],missing_models:[model('b.safetensors')]}}:{},{},seq)};
    if(options.method==='PUT'){puts.push(JSON.parse(options.body).changes);seq++;}
    return {ok:true,status:200,json:async()=>({ok:true,seq,template_id:'T',view:viewOf()})};};
const owner={_sessionPath:s=>`/s/${s}`,_fetch:fetchImpl,headers:()=>({}),_noteRefusal(){},templateId:'T',templateName:'W',canWrite:()=>true,
    listWorkflows:async()=>({ok:true,current:'T',workflows:[]}),switchWorkflow:async()=>({}),setFavorite:async()=>({})};
const host=createSdkControlsHost(owner);
Object.assign(host,{getEditorSettings:()=>({controls:{view:'rows'},layout:{scaleControls:1}}),notify:m=>notices.push(m),inform:()=>{}});
const container=new El('div');document.body.append(container);const panel=mountControlsPanel(container,host);
host.setVisible(true);await wait(400);await tick();
const choice=id=>walk(container).find(n=>n.dataset.controlsModeChoice===id);
assert.equal(choice('b').attributes['aria-disabled'],'true','the answer reached the button through the real reader');
choice('b').dispatch('click');await wait(50);await tick();
assert.deepEqual(puts,[]);assert.match(notices.at(-1),/^Needs b\.safetensors/);assert.equal(await panel.flushPending(),true);
// The file arrives between clicks: the click itself finds out, and applies.
missing=false;choice('b').dispatch('click');await wait(50);await tick();
assert.deepEqual(puts,[{'preset:b:apply':true}]);assert.equal(choice('b').attributes['aria-disabled'],undefined);
panel.destroy();host.destroy();console.log('ok');
'''


def test_the_real_host_reader_and_panel_together(tmp_path):
    run(urls(PRELUDE + COMMON + INTEGRATION), tmp_path, 'availability-integration')


def test_the_words_for_what_a_choice_lacks(tmp_path):
    run(urls(PRELUDE + COMMON + WORDS), tmp_path, 'availability-words')


def test_the_reader(tmp_path):
    run(urls(PRELUDE + COMMON + READER), tmp_path, 'availability-reader')


def test_the_table_draws_the_answer_and_ignores_a_host_without_one(tmp_path):
    run(urls(PRELUDE + COMMON + TABLE), tmp_path, 'availability-table')


def test_a_grey_click_through_the_real_panel(tmp_path):
    run(urls(PRELUDE + COMMON + PANEL), tmp_path, 'availability-panel')


def test_the_sonder_app_host_reads_when_it_should_and_cleans_up(tmp_path):
    run(urls(PRELUDE + COMMON + HOST), tmp_path, 'availability-host')


def test_the_canvas_host_has_no_availability():
    source = (ROOT / 'web/js/controls_host_canvas.js').read_text(encoding='utf-8')
    assert 'availability' not in source
