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
