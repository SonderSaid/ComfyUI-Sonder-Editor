"""Manage's Modes section in the real modules: categories, choices, warnings,
the editors, stable DOM and the write queue (plans/controls-modes-phase-b.md §6)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_controls_modes_js import PRELUDE

ROOT = Path(__file__).resolve().parents[1]


def run(script, tmp_path, name):
    node = shutil.which('node')
    if not node:
        pytest.skip('node required for Controls renderer boundary tests')
    urls = {'MARKDOWN_URL': 'panel_markdown.js', 'PANEL_URL': 'editor_controls_panel.js', 'MANAGE_URL': 'controls_modes_manage.js'}
    for placeholder, path in urls.items():
        script = script.replace(placeholder, json.dumps((ROOT / 'web/js' / path).as_uri()))
    script = script.replace('SCENARIO', json.dumps(name))
    path = tmp_path / f'{name}.mjs'
    path.write_text(script, encoding='utf-8')
    result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith('ok'), result.stdout


MANAGE = r'''
const {mountModesManage}=await import(MANAGE_URL);
const member=(binding_id,preview='20')=>({binding_id,preview,status:'ok'});
let view={state:'ready',execution:[],
    bindings:[{binding_id:'steps',label:'Steps',kind:'INT',value:'20',status:'ok'},{binding_id:'seed',label:'Seed',kind:'SEED',value:'3',status:'ok',seed_mode:null}],
    presets:[P('a','match',{name:'Fast',members:[member('steps')]}),P('b','differs',{name:'Slow',members:[member('steps','40')]}),
        P('c','differs',{name:'Warm',members:[member('seed','1')]}),P('h','differs',{name:'Cool',members:[member('seed','2')]}),
        P('loose','differs',{name:'Loose',members:[member('steps','9')]}),P('dup','differs',{name:'Twice',members:[member('seed','5')]})],
    preset_categories:[C('x','Speed',['a','b','dup']),C('y','Look',['c','h'],{origin:'app_copy',
        warnings:[{code:'duplicate',choice:'dup',other:'x',message:'“Twice” is also in “Speed”, where it shows.'},
            {code:'missing',choices:['gone'],message:'1 listed choice no longer exists.'},{code:'linked',count:1,categories:[],message:'Shares Steps with Speed.'}]}),
        C('z','Empty',[],{warnings:[{code:'too_few',message:'Add at least two choices.'}]})]};
const calls=[],notices=[],tracked=[];let flushes=0,confirmed=0,confirmAnswer=true,gate=null,refuse=null;
globalThis.confirm=()=>{confirmed++;return confirmAnswer;};
// The host applies category edits, so a queued edit sees the one before it.
const host={getView:()=>view,freshView:async()=>({view,fresh:true}),
    async applyChanges(changes){calls.push(changes);if(gate)await gate.promise;
        if(refuse)return {ok:false,rejected:[{reason:refuse}]};
        for(const [target,value] of Object.entries(changes)){const [source,id,field]=target.split(':');
            if(source==='preset_category'&&field==='choices')view={...view,preset_categories:view.preset_categories.map(c=>c.id===id?{...c,choices:value}:c)};
            if(source==='preset_category'&&field==='name')view={...view,preset_categories:view.preset_categories.map(c=>c.id===id?{...c,name:value}:c)};}
        return {ok:true,rejected:[]};}};
const manage=mountModesManage(host,{track:(work,changes)=>{tracked.push([work,changes]);return work;},notify:m=>notices.push(m),
    flushPending:async()=>{flushes++;return true;}});
document.body.append(manage.el);manage.render(view);
const block=id=>walk(manage.el).find(n=>n.dataset.controlsModesCategory===id);
const line=id=>walk(manage.el).find(n=>n.dataset.controlsModesChoice===id);
const btn=(within,text)=>walk(within).find(n=>n.tagName==='BUTTON'&&(n.textContent===text||n.attributes['aria-label']===text));
const field=label=>walk(manage.el).find(n=>n.attributes['aria-label']===label);
const editor=()=>walk(manage.el).find(n=>n.dataset.controlsModesEditor==='true');

// The list: categories in order, their counts, origin, warnings with their fixes.
assert.match(manage.el.textContent,/^Modes/);
assert.deepEqual(walk(manage.el).filter(n=>n.dataset.controlsModesCategory).map(n=>n.dataset.controlsModesCategory),['x','y','z']);
assert.match(block('y').textContent,/2 choices · App copy/);
assert.match(block('y').textContent,/“Twice” is also in “Speed”, where it shows\. Editing this category's choices removes it here\./);
assert.match(block('y').textContent,/1 listed choice no longer exists\. Editing this category's choices removes them\./);
assert.match(block('y').textContent,/Shares Steps with Speed\./);assert.match(block('z').textContent,/Add at least two choices/);
assert.match(manage.el.textContent,/Not in a category/);assert.ok(line('loose'));
assert.match(line('a').textContent,/Fast · current values/);
// First Up and last Down are inert, never disabled.
assert.equal(btn(block('x'),'Move category up').attributes['aria-disabled'],'true');assert.equal(btn(block('x'),'Move category up').disabled,undefined);
btn(block('x'),'Move category up').dispatch('click');await settle();assert.equal(calls.length,0);

// A warning's fix writes the displayed list, which holds neither.
btn(block('y'),'Remove missing').dispatch('click');await settle();
assert.deepEqual(calls.at(-1),{'preset_category:y:choices':['c','h']});assert.deepEqual(tracked.at(-1)[1],{},'never changes the run');

// Category moves and choice moves.
btn(block('x'),'Move category down').dispatch('click');await settle();assert.deepEqual(calls.at(-1),{'preset_category:x:move':{before:'z'}});
btn(block('z'),'Move category up').dispatch('click');await settle();assert.deepEqual(calls.at(-1),{'preset_category:z:move':{before:'y'}});
btn(line('a'),'Move choice down').dispatch('click');await settle();assert.deepEqual(calls.at(-1),{'preset_category:x:choices':['b','a','dup']});

// Two quick edits of one list: the second builds on the first.
btn(line('b'),'Move choice down').dispatch('click');btn(line('b'),'Move choice down').dispatch('click');await settle();
assert.deepEqual(calls.slice(-2),[{'preset_category:x:choices':['a','b','dup']},{'preset_category:x:choices':['a','dup','b']}]);

// + Category: a draft, then Create.
btn(manage.el,'+ Category').dispatch('click');assert.match(editor().textContent,/New category/);
assert.equal(document.activeElement,field('Category name'));
btn(editor(),'Create').dispatch('click');await settle();assert.equal(notices.at(-1),'Name the category first.');
field('Category name').value='Pass';field('Category description').value='Run or skip';
btn(editor(),'Create').dispatch('click');btn(editor(),'Create').dispatch('click');await settle();
const [created,fields]=Object.entries(calls.at(-1))[0];
assert.match(created,/^preset_category:[A-Za-z0-9_-]{12}:create$/);assert.deepEqual(fields,{name:'Pass',description:'Run or skip'});
assert.equal(calls.filter(c=>Object.keys(c)[0]===created).length,1,'a second click on the same draft creates nothing');

// Editing a category: name saves on blur; a press right after still lands.
btn(block('x'),'Edit category Speed').dispatch('click');
assert.equal(field('Category name').value,'Speed');assert.equal(btn(editor(),'Update from current values').style.display,'none','a category has nothing to capture');
const down=btn(block('x'),'Move category down');
field('Category name').focus();field('Category name').value='Pace';down.focus();await settle();
assert.equal(btn(block('x'),'Move category down'),down,'the save re-rendered the list in place');
// An equal render rewrites no text and re-attaches nothing (a press would lose its click).
const d=Object.getOwnPropertyDescriptor(El.prototype,'textContent');let writes=0,moves=0;
const pick=line('a').children[0];Object.defineProperty(pick,'textContent',{configurable:true,get(){return d.get.call(this);},set(v){writes++;d.set.call(this,v);}});
const xb=block('x'),remove=xb.remove.bind(xb);xb.remove=()=>{moves++;remove();};
manage.render(view);assert.equal(writes,0);assert.equal(moves,0);
down.dispatch('click');await settle();
assert.deepEqual(calls.slice(-2),[{'preset_category:x:name':'Pace'},{'preset_category:x:move':{before:'z'}}]);
// A blur-save still pending never refuses the next press: + Choice opens its draft.
gate=deferred();field('Category name').focus();field('Category name').value='Tempo';
const addChoice=btn(block('x'),'+ Choice');addChoice.focus();await settle();assert.equal(manage.busy,true,'the name save is queued');
addChoice.dispatch('click');assert.match(editor().textContent,/New choice in “Pace”/);
let idle=false;manage.whenIdle().then(()=>idle=true);await settle();assert.equal(idle,false,'whenIdle waits for the queue');
gate.resolve();gate=null;await settle();assert.equal(idle,true);assert.equal(manage.busy,false);
assert.deepEqual(calls.at(-1),{'preset_category:x:name':'Tempo'});
btn(block('x'),'Edit category Tempo').dispatch('click');
// Escape restores what is stored.
field('Category name').focus();field('Category name').value='Oops';key('Escape');assert.equal(field('Category name').value,'Tempo');
assert.equal(btn(editor(),"Reset to workflow's version").style.display,'none','only an App copy resets');
// Delete asks when choices stay behind.
confirmAnswer=false;btn(editor(),'Delete').dispatch('click');await settle();assert.equal(confirmed,1);assert.notDeepEqual(calls.at(-1),{'preset_category:x:remove':true});
confirmAnswer=true;btn(editor(),'Delete').dispatch('click');await settle();assert.deepEqual(calls.at(-1),{'preset_category:x:remove':true});
btn(block('y'),'Edit category Look').dispatch('click');assert.notEqual(btn(editor(),"Reset to workflow's version").style.display,'none');
btn(editor(),"Reset to workflow's version").dispatch('click');await settle();assert.deepEqual(calls.at(-1),{'preset_category:y:reset':true});
btn(block('z'),'Edit category Empty').dispatch('click');const asked=confirmed;
btn(editor(),'Delete').dispatch('click');await settle();assert.equal(confirmed,asked,'an empty category goes without asking');

// + Choice: one request creates it and lists it.
btn(block('y'),'+ Choice').dispatch('click');assert.match(editor().textContent,/New choice in “Look”/);
field('Choice name').value='Neutral';
const check=manage.memberControl(view.bindings[1]);assert.equal(check.textContent,'In this choice');
assert.equal(walk(check).find(n=>n.tagName==='INPUT').attributes['aria-label'],'Include Seed in this choice');
walk(check).find(n=>n.tagName==='INPUT').checked=true;walk(check).find(n=>n.tagName==='INPUT').dispatch('change');
const before=flushes;btn(editor(),'Create').dispatch('click');await settle();
const create=calls.at(-1),newId=Object.keys(create)[0].split(':')[1];
assert.deepEqual(create,{[`preset:${newId}:create`]:{members:['seed'],name:'Neutral',description:''},'preset_category:y:choices':['c','h',newId]});
assert.equal(flushes,before+1,'capture saves pending Controls first');

// The Category select moves an existing choice in one request.
line('c').children[0].dispatch('click');const select=field('Category');
assert.equal(select.value,'y');select.value='x';select.dispatch('change');await settle();
assert.deepEqual(calls.at(-1),{'preset_category:y:choices':['h',newId],'preset_category:x:choices':['a','dup','b','c']});
select.value='';select.dispatch('change');await settle();assert.deepEqual(calls.at(-1),{'preset_category:x:choices':['a','dup','b']});
line('loose').children[0].dispatch('click');field('Category').value='y';field('Category').dispatch('change');await settle();
assert.deepEqual(calls.at(-1),{'preset_category:y:choices':['h',newId,'loose']});
// A choice two categories list can't move.
view={...view,preset_categories:[...view.preset_categories,C('w','Dup',['dup'],{warnings:[{code:'duplicate',choice:'dup',other:'x',message:'m'}]})]};manage.render(view);
line('dup').children[0].dispatch('click');assert.equal(field('Category').disabled,true);assert.match(field('Category').title,/Listed in two categories/);
// Choice member lines read as choices.
line('a').children[0].dispatch('click');
assert.match(manage.memberControl(view.bindings[0]).textContent,/^In this choice$/);
view={...view,bindings:[{...view.bindings[0],value:'25'},view.bindings[1]]};manage.render(view);
assert.match(manage.memberControl(view.bindings[0]).textContent,/In this choice · Choice: 20/);
// A refusal says why; the queue keeps going.
refuse='This category no longer exists. Open Manage.';btn(block('x'),'Move category down').dispatch('click');await settle();
assert.equal(notices.at(-1),'This category no longer exists. Open Manage.');refuse=null;
// A draft asks before it is dropped.
btn(block('x'),'+ Choice').dispatch('click');field('Choice name').value='Half';confirmAnswer=false;
assert.equal(manage.canLeave(),false);confirmAnswer=true;
manage.destroy();console.log('ok');
'''


def test_modes_manage(tmp_path):
    run(PRELUDE + MANAGE, tmp_path, 'modes-manage')


STALE = r'''
// A list write after one whose view never refreshed would rebuild the list the
// earlier write replaced: it reads again, and refuses while still stale.
const {mountModesManage}=await import(MANAGE_URL);
let view={state:'ready',execution:[],bindings:[],presets:[P('a'),P('b'),P('c')],preset_categories:[C('x','Speed',['a','b','c'])]};
const calls=[],notices=[];let fresh=false,reads=0;
const host={getView:()=>view,freshView:async()=>{reads++;return {view,fresh};},
    // The write lands on the server; the view this host shows does not follow.
    async applyChanges(changes){calls.push(changes);return {ok:true,rejected:[]};}};
const manage=mountModesManage(host,{track:w=>w,notify:m=>notices.push(m),flushPending:async()=>true});
document.body.append(manage.el);manage.render(view);
const down=id=>walk(manage.el).find(n=>n.dataset.controlsModesChoice===id).children.find(n=>n.attributes['aria-label']==='Move choice down');
down('a').dispatch('click');await settle();assert.deepEqual(calls,[{'preset_category:x:choices':['b','a','c']}]);
down('b').dispatch('click');await settle();
assert.equal(calls.length,1,'not built from the pre-write list');assert.equal(notices.at(-1),'Controls are still updating. Try again.');
assert.equal(reads,2,'it read once more before refusing');
// A newer view arrives: list writes build on it again.
view={...view,preset_categories:[C('x','Speed',['b','a','c'])]};manage.render(view);
down('a').dispatch('click');await settle();assert.deepEqual(calls.at(-1),{'preset_category:x:choices':['b','c','a']});
// The read after a write is fresh: no extra read, no refusal.
fresh=true;view={...view,preset_categories:[C('x','Speed',['b','c','a'])]};manage.render(view);reads=0;
down('b').dispatch('click');await settle();down('c').dispatch('click');await settle();
assert.equal(calls.length,4);assert.equal(reads,2,'one read after each write');
manage.destroy();console.log('ok');
'''


def test_modes_manage_refuses_a_list_built_from_a_stale_view(tmp_path):
    run(PRELUDE + STALE, tmp_path, 'modes-manage-stale')


DONE = r'''
// Done while a blur-save is queued: Manage closes once the save lands, on that one click.
let view={state:'ready',notes:[],execution:[],bindings:[],presets:[P('a'),P('b')],preset_categories:[C('x','Speed',['a','b'])]};
const calls=[];let gate=null;
const host={getView:()=>view,subscribe:()=>()=>{},freshView:async()=>({view,fresh:true}),
    async applyChanges(changes){calls.push(changes);if(gate)await gate.promise;
        if('preset_category:x:name' in changes)view={...view,preset_categories:[{...view.preset_categories[0],name:changes['preset_category:x:name']}]};
        return {ok:true,rejected:[]};}};
const box=document.createElement('div');document.body.append(box);const panel=mountControlsPanel(box,host);
const toggle=()=>walk(box).find(n=>n.dataset.controlsManageToggle==='true');
const manageOpen=()=>walk(box).some(n=>n.dataset.controlsModesManage==='true');
toggle().dispatch('click');assert.equal(manageOpen(),true);
walk(box).find(n=>n.tagName==='BUTTON'&&n.attributes['aria-label']==='Edit category Speed').dispatch('click');
const name=walk(box).find(n=>n.attributes['aria-label']==='Category name');
gate=deferred();name.focus();name.value='Pace';toggle().focus();await settle();
toggle().dispatch('click');await settle();assert.equal(manageOpen(),true,'waits for the save');
toggle().dispatch('click');await settle();
gate.resolve();gate=null;await settle();await settle();
assert.equal(manageOpen(),false,'closed on the first click');assert.equal(toggle().textContent,'Manage');
assert.deepEqual(calls,[{'preset_category:x:name':'Pace'}]);
panel.destroy();console.log('ok');
'''


def test_done_waits_for_a_queued_modes_save(tmp_path):
    run(PRELUDE + DONE, tmp_path, 'modes-manage-done')


CAPTURE = r'''
// Real panel: Update from current values commits a focused row, waits for it,
// and refuses a failed save.
let stored=20,refuse=false;const captures=[],writes=[];
let view={state:'ready',notes:[],execution:[],bindings:[{source:'binding',binding_id:'steps',label:'Steps',kind:'INT',value:'20',status:'ok',section:'workflow',order:0,group:'',note:''}],
    presets:[P('p','match',{name:'Fast',members:[{binding_id:'steps',preview:'20',status:'ok'}]}),P('q','differs',{name:'Slow',members:[{binding_id:'steps',preview:'40',status:'ok'}]})],
    preset_categories:[C('speed','Speed',['p','q'])]};
const host={getView:()=>view,subscribe:()=>()=>{},freshView:async()=>({view,fresh:true}),applyChanges:async changes=>{
    writes.push(changes);
    if('binding:steps:value' in changes){if(refuse)return {ok:false,rejected:[{reason:'refused'}]};stored=Number(changes['binding:steps:value']);}
    if('preset:p:capture' in changes)captures.push(stored);
    return {ok:true};
}};
const box=document.createElement('div');document.body.append(box);const panel=mountControlsPanel(box,host);
walk(box).find(n=>n.dataset.controlsManageToggle==='true').dispatch('click');
walk(box).find(n=>n.dataset.controlsModesChoice==='p').children[0].dispatch('click');
const control=walk(box).find(n=>n.dataset.controlTarget==='binding:steps:value');
const field=walk(control).find(n=>n.tagName==='INPUT'&&n.type==='number');field.focus();field.value='40';
const capture=walk(box).find(n=>n.tagName==='BUTTON'&&n.textContent==='Update from current values');
capture.dispatch('mousedown');capture.dispatch('click');await settle();assert.deepEqual(captures,[40]);
assert.equal(await panel.flushPending(),true);
refuse=true;field.focus();field.value='50';capture.dispatch('click');await settle();assert.deepEqual(captures,[40]);
panel.destroy();console.log('ok');
'''


def test_update_from_current_values_saves_the_focused_row_first(tmp_path):
    run(PRELUDE + CAPTURE, tmp_path, 'modes-capture')


LOAD = r'''
// plans/live-workflow-phase-4-controls-modes.md D8 and D9: selecting a saved
// choice loads it before its editor edits it; deliberate saves confirm.
const {mountModesManage}=await import(MANAGE_URL);
const m=(id,preview)=>[{binding_id:id,preview,status:'ok'}];
let view={state:'ready',execution:[],bindings:[{binding_id:'steps',label:'Steps',kind:'INT',value:'20',status:'ok'}],
    presets:[P('a','match',{name:'Refine',members:m('steps','20')}),P('b','differs',{name:'Refine and Upscale',members:m('steps','40')}),
        P('c','differs',{name:'Draft',members:m('steps','8')}),P('u','differs',{name:'Loose',members:m('steps','9')})],
    preset_categories:[C('pass','Passes',['a','b','c'])]};
const calls=[],notices=[],details=[],toasts=[],tracked=[],confirms=[],fresh=[];
let flushOk=true,confirmAnswer=true,applyGate=null,nextResult=null;
globalThis.confirm=message=>{confirms.push(message);return confirmAnswer;};
// Applying a choice makes it the match, as the server's preset_states would.
const host={getView:()=>view,freshView:async()=>{const next=fresh.shift();return next?await next():{view,fresh:true};},
    async applyChanges(changes){calls.push(changes);if(applyGate)await applyGate.promise;
        const result=nextResult;nextResult=null;if(result)return result;
        for(const [target,value] of Object.entries(changes)){const [source,id,field]=target.split(':');
            if(source==='preset'&&field==='apply')view={...view,presets:view.presets.map(p=>({...p,state:p.id===id?'match':'differs'}))};
            if(source==='preset'&&field==='name')view={...view,presets:view.presets.map(p=>p.id===id?{...p,name:value}:p)};
            if(source==='preset_category'&&field==='name')view={...view,preset_categories:view.preset_categories.map(c=>c.id===id?{...c,name:value}:c)};}
        return {ok:true,rejected:[]};}};
const manage=mountModesManage(host,{track:(work,changes)=>{tracked.push([work,changes]);return work;},
    notify:(message,detail)=>{notices.push(message);details.push(detail);},success:message=>toasts.push(message),
    flushPending:async()=>flushOk});
document.body.append(manage.el);manage.render(view);
const pick=id=>walk(manage.el).find(n=>n.dataset.controlsModesChoice===id).children[0];
const status=()=>walk(manage.el).find(n=>n.dataset.controlsModesLoad==='true');
const editor=()=>walk(manage.el).find(n=>n.dataset.controlsModesEditor==='true');
const siblings=()=>walk(manage.el).find(n=>n.dataset.controlsModesSiblings==='true');
const sib=id=>walk(manage.el).find(n=>n.dataset.controlsModesSibling===id);
const btn=(within,text)=>walk(within).find(n=>n.tagName==='BUTTON'&&(n.textContent===text||n.attributes['aria-label']===text));
const field=label=>walk(manage.el).find(n=>n.attributes['aria-label']===label);
const update=()=>btn(editor(),'Update from current values');
const LOADED='Loaded: the Controls show this choice.';

// A fresh match is already loaded: nothing is written, so an advancing seed keeps its number.
pick('a').dispatch('click');
assert.equal(status().textContent,'Loading this choice into the Controls…');assert.equal(editor().attributes['aria-busy'],'true');
assert.equal(manage.busy,true,'a load counts as busy: Done waits and a workflow switch is refused');
await settle();assert.equal(calls.length,0);assert.equal(status().textContent,LOADED);assert.equal(editor().attributes['aria-busy'],undefined);
assert.deepEqual(tracked.at(-1)[1],{'preset:a:apply':true},'a load blocks a run while it runs');assert.equal(await tracked.at(-1)[0],true);
// Choices in its category, in the category's order, the edited one marked.
assert.equal(siblings().children[0].textContent,'Choices in “Passes” (3)');
assert.deepEqual(walk(siblings()).filter(n=>n.dataset.controlsModesSibling).map(n=>n.dataset.controlsModesSibling),['a','b','c']);
assert.equal(sib('a').attributes['aria-current'],'true');assert.equal(sib('b').attributes['aria-current'],undefined);
// A sibling selects like the main list, and a differing choice applies.
const keptSibling=sib('c');sib('b').dispatch('click');await settle();
assert.deepEqual(calls,[{'preset:b:apply':true}]);assert.equal(status().textContent,LOADED);
assert.equal(sib('b').attributes['aria-current'],'true');assert.equal(sib('c'),keptSibling,'kept, updated in place');
// An uncategorized choice loads too, and lists no siblings.
pick('u').dispatch('click');await settle();assert.deepEqual(calls.at(-1),{'preset:u:apply':true});assert.equal(siblings().style.display,'none');

// A stale view decides nothing: its match still applies.
view={...view,presets:view.presets.map(p=>p.id==='c'?{...p,state:'match'}:p)};manage.render(view);
fresh.push(async()=>({view,fresh:false}));pick('c').dispatch('click');await settle();
assert.deepEqual(calls.at(-1),{'preset:c:apply':true});assert.equal(status().textContent,LOADED);

// Refused: Not loaded with the reason, a warning, still editable; a run never refused for it.
nextResult={ok:false,rejected:[{reason:'Steps: too high.'}]};pick('b').dispatch('click');await settle();
assert.equal(status().textContent,'Not loaded: Steps: too high. Controls still show your current settings.');
assert.equal(notices.at(-1),'“Refine and Upscale” was not loaded: Steps: too high.');
assert.equal(await tracked.at(-1)[0],true);assert.equal(field('Choice name').disabled,undefined);
// Update asks first; declined, nothing is captured.
confirmAnswer=false;let n=calls.length;update().dispatch('click');await settle();
assert.equal(calls.length,n);assert.equal(confirms.at(-1),"The Controls may not hold this choice's settings. Update “Refine and Upscale” from them anyway?");
confirmAnswer=true;update().dispatch('click');await settle();assert.deepEqual(calls.at(-1),{'preset:b:capture':{members:['steps']}});
assert.equal(toasts.at(-1),'Choice “Refine and Upscale” updated from current values.');

// A failed flush writes nothing.
flushOk=false;n=calls.length;pick('c').dispatch('click');await settle();flushOk=true;
assert.equal(calls.length,n);assert.equal(status().textContent,'Not loaded: A Control could not be saved. Controls still show your current settings.');
// A lost answer is Not confirmed, never Not loaded, and Update asks too.
nextResult={ok:false,unconfirmed:true,rejected:[{reason:'The canvas reconnected before it confirmed this change.'}]};
pick('b').dispatch('click');await settle();
assert.equal(status().textContent,"Couldn't confirm “Refine and Upscale” loaded. Check the Controls before updating it.");
confirmAnswer=false;n=calls.length;update().dispatch('click');await settle();assert.equal(calls.length,n);confirmAnswer=true;

// A mounted tab's canvas reconnecting mid-load: its view lists nothing for a
// moment; the editor stays, and a lost answer reads Not confirmed.
view={...view,presets:view.presets.map(p=>({...p,state:'differs'}))};manage.render(view);
applyGate=deferred();pick('c').dispatch('click');await settle();const listedView=view;
view={state:'loading',bindings:[],notes:[],error:null};manage.render(view);
assert.notEqual(editor().style.display,'none','an empty view keeps the editor');
nextResult={ok:false,unconfirmed:true,rejected:[{reason:'The canvas closed before it confirmed this change.'}]};
applyGate.resolve();applyGate=null;await settle();view=listedView;manage.render(view);
assert.equal(status().textContent,"Couldn't confirm “Draft” loaded. Check the Controls before updating it.");
// A fresh view listing no presets decides nothing: the host's answer does.
n=calls.length;fresh.push(async()=>({view:{state:'ready',bindings:[]},fresh:true}));pick('u').dispatch('click');await settle();
assert.deepEqual(calls.slice(n),[{'preset:u:apply':true}]);
// Update waiting on a load whose selection moved on runs nowhere, and says so.
view={...view,presets:view.presets.map(p=>({...p,state:'differs'}))};manage.render(view);
applyGate=deferred();pick('c').dispatch('click');await settle();update().dispatch('click');await settle();
sib('b').dispatch('click');applyGate.resolve();applyGate=null;await settle();await settle();
assert.equal(notices.at(-1).startsWith("Update didn't run"),true);
assert.ok(!calls.some(c=>'preset:c:capture' in c&&c===calls.at(-1)),'nothing captured into another choice');
// Update waits for a load still running, then captures without asking.
view={...view,presets:view.presets.map(p=>({...p,state:'differs'}))};manage.render(view);
applyGate=deferred();pick('c').dispatch('click');await settle();
n=calls.length;let asked=confirms.length;update().dispatch('click');await settle();assert.equal(calls.length,n,'waits for the load');
applyGate.resolve();applyGate=null;await settle();
assert.deepEqual(calls.at(-1),{'preset:c:capture':{members:['steps']}});assert.equal(confirms.length,asked,'a Loaded choice never asks');

// Supersession: a load whose selection moved on writes nothing.
let release;fresh.push(()=>new Promise(resolve=>{release=()=>resolve({view,fresh:true});}));
n=calls.length;pick('b').dispatch('click');await settle();
let idle=false;manage.whenIdle().then(()=>idle=true);
pick('a').dispatch('click');await settle();assert.equal(manage.busy,true,'the superseded load still runs');assert.equal(idle,false);
release();await settle();
assert.ok(!calls.slice(n).some(c=>'preset:b:apply' in c),'a superseded load never writes');
assert.deepEqual(calls.at(-1),{'preset:a:apply':true});assert.equal(status().textContent,LOADED);assert.equal(sib('a').attributes['aria-current'],'true');
assert.equal(idle,true,'whenIdle waits for every load');assert.equal(manage.busy,false);

// Unsaved ticks ask before they are dropped, by a selection and by Done.
const tick=walk(manage.memberControl(view.bindings[0])).find(n=>n.tagName==='INPUT');tick.checked=false;tick.dispatch('change');
confirmAnswer=false;pick('c').dispatch('click');assert.equal(confirms.at(-1),'Discard the unsaved setting changes to “Refine”?');
assert.equal(sib('a').attributes['aria-current'],'true','declined: the editor stays');assert.equal(manage.canLeave(),false);
confirmAnswer=true;pick('c').dispatch('click');await settle();assert.equal(status().textContent,LOADED);
// Values tuned since the load ask before another load overwrites them; a category edit doesn't ask.
view={...view,presets:view.presets.map(p=>p.id==='c'?{...p,state:'differs'}:p)};manage.render(view);
confirmAnswer=false;n=calls.length;pick('b').dispatch('click');
assert.equal(confirms.at(-1),"Your Controls changed since loading “Draft” and aren't saved to it. Load “Refine and Upscale” anyway?");
assert.equal(calls.length,n);assert.equal(sib('c').attributes['aria-current'],'true');confirmAnswer=true;
assert.equal(manage.canLeave(),true,'Done overwrites nothing, so it never asks about tuned values');
asked=confirms.length;btn(manage.el,'Edit category Passes').dispatch('click');await settle();assert.equal(confirms.length,asked);
assert.equal(status().textContent,'','a category loads nothing');assert.equal(calls.length,n);
// Editing a category in between never disarms the question.
confirmAnswer=false;pick('b').dispatch('click');
assert.equal(confirms.at(-1),"Your Controls changed since loading “Draft” and aren't saved to it. Load “Refine and Upscale” anyway?");
assert.equal(calls.length,n);confirmAnswer=true;

// Toasts: one per deliberate save once it is accepted.
field('Category name').focus();field('Category name').value='Pass';field('Category name').blur();await settle();
assert.equal(toasts.at(-1),'Renamed to “Pass”.');
field('Category description').focus();field('Category description').value='Which passes run';field('Category description').blur();await settle();
assert.equal(toasts.at(-1),'Description of “Pass” saved.');
btn(manage.el,'+ Category').dispatch('click');field('Category name').value='Look';btn(editor(),'Create').dispatch('click');await settle();
assert.equal(toasts.at(-1),'Category “Look” created.');
btn(manage.el,'+ Choice').dispatch('click');field('Choice name').value='Half';btn(editor(),'Create').dispatch('click');await settle();
assert.equal(toasts.at(-1),'Choice “Half” created in “Pass”.');
// Update with an edited name: one click, one toast.
pick('a').dispatch('click');await settle();let count=toasts.length;
field('Choice name').focus();field('Choice name').value='Refine Only';update().dispatch('mousedown');update().dispatch('click');await settle();
assert.equal(toasts.length,count+1);assert.equal(toasts.at(-1),'Choice “Refine Only” renamed and updated from current values.');
assert.deepEqual(calls.slice(-2),[{'preset:a:name':'Refine Only'},{'preset:a:capture':{members:['steps']}}]);
// A blur-save still queued is not sent (or announced) again by Update.
applyGate=deferred();count=toasts.length;n=calls.length;
field('Choice name').focus();field('Choice name').value='Refine Twice';field('Choice name').blur();await settle();
update().dispatch('click');await settle();applyGate.resolve();applyGate=null;await settle();
assert.equal(calls.slice(n).filter(c=>'preset:a:name' in c).length,1,'one rename');
assert.deepEqual(toasts.slice(count),['Renamed to “Refine Twice”.','Choice “Refine Twice” updated from current values.']);
// A refused save shows only its warning.
count=toasts.length;nextResult={ok:false,rejected:[{reason:'Too many choices.'}]};update().dispatch('click');await settle();
assert.equal(toasts.length,count);assert.equal(notices.at(-1),'Too many choices.');
field('Choice name').focus();field('Choice name').value='Again';nextResult={ok:false,rejected:[{reason:'Name refused.'}]};field('Choice name').blur();await settle();
assert.equal(toasts.length,count,'a refused rename shows no toast');

// Destroyed mid-load: nothing paints, nothing throws.
applyGate=deferred();pick('b').dispatch('click');await settle();const said=notices.length;
manage.destroy();applyGate.resolve();applyGate=null;await settle();assert.equal(notices.length,said);
console.log('ok');
'''


def test_manage_loads_a_choice_before_editing_it(tmp_path):
    run(PRELUDE + LOAD, tmp_path, 'modes-manage-load')


PANEL_LOAD = r'''
// The real panel: a refused load never fails the next selection's flush or a
// run, and Done waits for a load to land.
let view={state:'ready',notes:[],execution:[],bindings:[{source:'binding',binding_id:'steps',label:'Steps',kind:'INT',value:'20',status:'ok',section:'workflow',order:0,group:'',note:''}],
    presets:[P('a','differs',{name:'A',members:[{binding_id:'steps',preview:'1',status:'ok'}]}),P('b','differs',{name:'B',members:[{binding_id:'steps',preview:'2',status:'ok'}]})],
    preset_categories:[C('x','X',['a','b'])]};
const calls=[];let gate=null,refuseA=false;
const host={getView:()=>view,subscribe:()=>()=>{},freshView:async()=>({view,fresh:true}),notify:()=>{},
    async applyChanges(changes){calls.push(changes);if(gate)await gate.promise;
        if(refuseA&&'preset:a:apply' in changes)return {ok:false,rejected:[{reason:'A is broken.'}]};return {ok:true,rejected:[]};}};
const box=document.createElement('div');document.body.append(box);const panel=mountControlsPanel(box,host);
const toggle=()=>walk(box).find(n=>n.dataset.controlsManageToggle==='true');
const manageOpen=()=>walk(box).some(n=>n.dataset.controlsModesManage==='true');
const pick=id=>walk(box).find(n=>n.dataset.controlsModesChoice===id).children[0];
const status=()=>walk(box).find(n=>n.dataset.controlsModesLoad==='true');
toggle().dispatch('click');
// A is refused while B is selected: B's flush still passes and B loads.
gate=deferred();refuseA=true;pick('a').dispatch('click');await settle();
const run=panel.flushPending();pick('b').dispatch('click');await settle();
gate.resolve();gate=null;await settle();await settle();
assert.deepEqual(calls,[{'preset:a:apply':true},{'preset:b:apply':true}]);assert.equal(status().textContent,'Loaded: the Controls show this choice.');
assert.equal(await run,true,'a run waited for the load and was not refused by it');
// Done during a load closes once it lands, on that one click.
gate=deferred();refuseA=false;pick('a').dispatch('click');await settle();
toggle().dispatch('click');await settle();assert.equal(manageOpen(),true,'waits for the load');
gate.resolve();gate=null;await settle();await settle();assert.equal(manageOpen(),false);
panel.destroy();console.log('ok');
'''


def test_a_refused_load_never_blocks_the_next_selection_or_done(tmp_path):
    run(PRELUDE + PANEL_LOAD, tmp_path, 'modes-manage-panel-load')
