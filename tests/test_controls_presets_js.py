"""Shared preset UI save, draft and selection boundaries in the real modules."""
import json
import subprocess
from pathlib import Path

import pytest
from test_controls_panel_js import SCRIPT

ROOT = Path(__file__).resolve().parents[1]
PRELUDE = SCRIPT[:SCRIPT.index("if(SCENARIO==='markdown')")]
BODY = r'''
const {mountPresets,selectedPreset}=await import(PRESETS_URL);
const {mountPresetsManage}=await import(MANAGE_URL);
const preset=(id,members=['steps'],state='match',extra={})=>({id,name:id,description:'**Preset help**',members:members.map(binding_id=>({binding_id,preview:'20',status:'ok'})),order:0,state,origin:'workflow',...extra});
const p=preset('p'),q=preset('q',['steps','seed'], 'match',{order:1});
assert.equal(selectedPreset([p,q],null).id,'q');assert.equal(selectedPreset([p,q],'p').id,'p');
assert.equal(selectedPreset([p,{...q,members:p.members}],null).id,'p');
assert.equal(selectedPreset([{...p,state:'differs'}],null),null);
let view={state:'ready',description:'**Workflow help**',presets:[p,q],bindings:[{binding_id:'steps',label:'Steps',value:'20',status:'ok',kind:'INT'},{binding_id:'seed',label:'Seed',value:'3',status:'ok',kind:'SEED',seed_mode:null}]};
const calls=[],notices=[],tracked=[];let flushResolve,applyResolve,flushed=false;
const host={getView:()=>view,capabilities:{hide:false},applyChanges:async changes=>{calls.push(changes);return await new Promise(r=>applyResolve=r);}};
const normal=mountPresets(host,{notify:m=>notices.push(m),track:work=>{tracked.push(work);return work;},flushPending:()=>new Promise(r=>flushResolve=r)});
document.body.append(normal.el);normal.render(view);
const select=walk(normal.el).find(n=>n.tagName==='SELECT');
assert.equal(select.value,'q');assert.match(normal.el.textContent,/Workflow help/);
select.value='p';select.dispatch('change');assert.equal(calls.length,0);assert.equal(tracked.length,1,'Generate can already see pending apply');
flushResolve(true);await settle();assert.deepEqual(calls,[{'preset:p:apply':true}]);
applyResolve({ok:true});await tracked[0];assert.equal(select.value,'p');
view={...view,presets:[{...p,state:'differs'},q]};normal.render(view);assert.equal(walk(normal.el).find(n=>n.tagName==='SELECT').value,'q');
view={...view,presets:[{...p,state:'unavailable',reason:'Missing Steps'}, {...q,state:'refused',reason:'Model gone'}]};normal.render(view);
const opts=walk(normal.el).filter(n=>n.tagName==='OPTION');assert.ok(opts.every(n=>n.disabled));assert.equal(opts[1].title,'Missing Steps');
view={...view,presets:[p,q]};normal.render(view);const next=walk(normal.el).find(n=>n.tagName==='SELECT');next.value='p';next.dispatch('change');flushResolve(false);await tracked.at(-1);assert.equal(calls.length,1);
assert.match(notices.at(-1),/not applied/);normal.destroy();

// Manage selection never applies. Missing and unavailable members remain repairable.
view={...view,presets:[preset('p',['steps','gone','seed'],'unavailable',{members:[{binding_id:'steps',preview:'10'},{binding_id:'gone',preview:'old'},{binding_id:'seed',preview:'3',seed_mode:'fixed'}]})]};
let confirmCount=0;globalThis.confirm=()=>{confirmCount++;return false;};
const manage=mountPresetsManage(host,{track:work=>{tracked.push(work);return work;},notify:m=>notices.push(m)});
document.body.append(manage.el);manage.render(view);
walk(manage.el).find(n=>n.tagName==='BUTTON'&&n.textContent.startsWith('p ·')).dispatch('click');assert.equal(calls.length,1);
assert.match(manage.el.textContent,/Members without a Control/);assert.match(manage.el.textContent,/gone · In preset/);
const member=manage.memberControl(view.bindings[0]);assert.match(member.textContent,/Preset: 10/);assert.equal(walk(member).find(n=>n.tagName==='INPUT').checked,true);
assert.match(manage.memberControl(view.bindings[1]).textContent,/mode is skipped/);
const gone=walk(manage.el).find(n=>n.attributes['aria-label']==='Include gone in preset');gone.checked=false;gone.dispatch('change');
walk(manage.el).find(n=>n.tagName==='BUTTON'&&n.textContent==='Update from current values').dispatch('click');await settle();assert.deepEqual(calls.at(-1),{'preset:p:capture':{members:['steps','seed']}});applyResolve({ok:true});await tracked.at(-1);
walk(manage.el).find(n=>n.tagName==='BUTTON'&&n.textContent==='+ Preset').dispatch('click');
const name=walk(manage.el).find(n=>n.attributes['aria-label']==='Preset name');name.value='Draft';assert.equal(manage.canLeave(),false);assert.equal(confirmCount,1);manage.destroy();

// Overlapping independent text saves preserve both edits in request order.
view={...view,presets:[preset('p', ['steps'], 'match', {name:'Original',description:'Old'})]};
const queued=[];let finish;
const slow={getView:()=>view,applyChanges:async changes=>{
    queued.push(changes);await new Promise(r=>finish=r);
    const [target,value]=Object.entries(changes)[0];view={...view,presets:[{...view.presets[0],[target.split(':')[2]]:value}]};return {ok:true};
}};
const edits=[];const manager=mountPresetsManage(slow,{track:p=>{edits.push(p);return p;},notify:()=>{}});
document.body.append(manager.el);manager.render(view);
walk(manager.el).find(n=>n.tagName==='BUTTON'&&n.textContent.startsWith('Original ·')).dispatch('click');
const nm=walk(manager.el).find(n=>n.attributes['aria-label']==='Preset name');
const ds=walk(manager.el).find(n=>n.attributes['aria-label']==='Preset description');
nm.focus();nm.value='Renamed';ds.focus();ds.value='Kept text';ds.blur();await settle();
assert.equal(queued.length,1);assert.equal(ds.value,'Kept text');finish();await edits[0];await settle();
assert.equal(queued.length,2);assert.equal(ds.value,'Kept text');finish();await edits[1];
assert.equal(view.presets[0].name,'Renamed');assert.equal(view.presets[0].description,'Kept text');manager.destroy();

// Real panel: Capture commits a focused row, waits for it, and refuses a failed save.
let stored=20,refuse=false;const captures=[],writes=[];
view={...view,notes:[],bindings:[{source:'binding',binding_id:'steps',label:'Steps',kind:'INT',value:'20',status:'ok',section:'workflow'}]};
const captureHost={getView:()=>view,subscribe:()=>()=>{},applyChanges:async changes=>{
    writes.push(changes);
    if('binding:steps:value' in changes){if(refuse)return {ok:false,rejected:[{reason:'refused'}]};stored=Number(changes['binding:steps:value']);}
    if('preset:p:capture' in changes)captures.push(stored);
    return {ok:true};
}};
const box=document.createElement('div');document.body.append(box);const panel=mountControlsPanel(box,captureHost);
walk(box).find(n=>n.dataset.controlsManageToggle==='true').dispatch('click');
walk(box).find(n=>n.tagName==='BUTTON'&&n.textContent.startsWith('Renamed ·')).dispatch('click');
const control=walk(box).find(n=>n.dataset.controlTarget==='binding:steps:value');
const field=walk(control).find(n=>n.tagName==='INPUT'&&n.type==='number');field.focus();field.value='40';
const capture=walk(box).find(n=>n.tagName==='BUTTON'&&n.textContent==='Update from current values');
capture.dispatch('mousedown');capture.dispatch('click');await settle();assert.deepEqual(captures,[40]);
assert.equal(await panel.flushPending(),true);
refuse=true;field.focus();field.value='50';capture.dispatch('click');await settle();assert.deepEqual(captures,[40]);
panel.destroy();

'''


def test_presets_selection_flush_and_manage(tmp_path):
    script = PRELUDE + BODY
    for name, path in [('MARKDOWN_URL', 'panel_markdown.js'), ('PANEL_URL', 'editor_controls_panel.js'),
                       ('PRESETS_URL', 'controls_presets.js'), ('MANAGE_URL', 'controls_presets_manage.js')]:
        script = script.replace(name, json.dumps((ROOT / 'web/js' / path).as_uri()))
    script = script.replace('SCENARIO', json.dumps('presets'))
    path = tmp_path / 'presets.mjs'
    path.write_text(script, encoding='utf-8')
    result = subprocess.run(['node', str(path)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
