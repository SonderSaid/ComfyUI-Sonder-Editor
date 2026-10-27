"""Controls Modes in the real modules: the derivation model, the Modes table,
its click flow, the workflow header and the no-"preset" wording
(plans/controls-modes-phase-b.md)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_controls_panel_js import SCRIPT

ROOT = Path(__file__).resolve().parents[1]
PRELUDE = SCRIPT[:SCRIPT.index("if(SCENARIO==='markdown')")] + r'''
const key=(value,extra={})=>{const e={key:value,keyCode:0,isComposing:false,ctrlKey:false,metaKey:false,prevented:false,stopped:false,target:document.activeElement,
    preventDefault(){this.prevented=true;},stopImmediatePropagation(){this.stopped=true;},...extra};for(const f of events.keydown||[]){f(e);if(e.stopped)break;}return e;};
// A choice row as the server projects it (control_presets.preset_projection).
const P=(id,state='differs',extra={})=>({id,name:extra.name||id.toUpperCase(),description:'',order:0,state,origin:'workflow',members:[],execution:[],unavailable:[],reason:'',...extra});
const C=(id,name,choices,extra={})=>({id,name,description:'',order:0,origin:'workflow',choices,warnings:[],...extra});
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
'''


def run(script, tmp_path, name):
    node = shutil.which('node')
    if not node:
        pytest.skip('node required for Controls renderer boundary tests')
    urls = {'MARKDOWN_URL': 'panel_markdown.js', 'PANEL_URL': 'editor_controls_panel.js', 'MODEL_URL': 'controls_modes_model.js',
            'MODES_URL': 'controls_modes.js', 'HEADER_URL': 'controls_workflow_header.js'}
    for placeholder, path in urls.items():
        script = script.replace(placeholder, json.dumps((ROOT / 'web/js' / path).as_uri()))
    script = script.replace('SCENARIO', json.dumps(name))
    path = tmp_path / f'{name}.mjs'
    path.write_text(script, encoding='utf-8')
    result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith('ok'), result.stdout


MODEL = r'''
const {deriveModes,sharedControls,followNotice,ambiguityText}=await import(MODEL_URL);
const {panelMarkdownPlainText}=await import(MARKDOWN_URL);
const view={state:'ready',presets:[P('a','match'),P('b'),P('c','match'),P('h','match'),P('d','unavailable',{reason:'gone'}),P('e','refused'),
    P('f'),P('g'),P('loose')],
    preset_categories:[C('x','Task',['a','b']),C('y','Look',['c','h']),C('z','Pass',['d','e']),C('w','Mix',['f','g']),C('v','Empty',[]),C('u','Gone',['nope'])]};
const modes=deriveModes(view);
assert.deepEqual(modes.rows.map(r=>[r.id,r.status,r.selected?.id||null,r.matches.map(m=>m.id)]),
    [['x','selected','a',['a']],['y','ambiguous',null,['c','h']],['z','unavailable',null,[]],['w','custom',null,[]]],'no tie-break; empty and missing-only rows are not drawn');
assert.deepEqual(modes.uncategorized.map(p=>p.id),['loose']);
assert.deepEqual(deriveModes({state:'ready',presets:view.presets}).rows,[],'error and too-large views carry no categories');
// The first category that lists a choice claims it.
assert.deepEqual(deriveModes({presets:[P('a'),P('b')],preset_categories:[C('x','X',['a']),C('y','Y',['a','b'])]}).rows.map(r=>r.choices.map(c=>c.id)),[['a'],['b']]);
assert.equal(ambiguityText(modes.rows[1]),'“C” and “H” both match. Fix in Manage.');
assert.equal(ambiguityText({matches:[P('a'),P('b'),P('c')]}),'“A”, “B” and 1 more match. Fix in Manage.');

// What a choice touches: settings, switches, and the nodes those switches cover.
const sv={presets:[P('s',null,{members:[{binding_id:'steps'}],execution:[{execution_id:'g'}]})],execution:[{execution_id:'g',members:[{path:'3'},{path:'4'}]}]};
assert.deepEqual([...sharedControls(sv,'s')].sort(),['binding:steps','node:3','node:4','switch:g']);
assert.equal(sharedControls(sv,'missing').size,0);

// The follow notice: only rows sharing a Control with the click, one sentence.
const m=(id,name,state,members,extra={})=>P(id,state,{name,members:members.map(binding_id=>({binding_id})),...extra});
const before={state:'ready',presets:[m('rp','Remove People','differs',['task','edit']),m('gen','Generate','differs',['task']),
    m('vo','Video only','differs',['edit']),m('vi','Video + image','match',['edit']),m('warm','Warm','match',['edit','color']),m('cool','Cool','differs',['color']),
    m('o1','One','differs',['other']),m('o2','Two','differs',['other'])],
    preset_categories:[C('task','Task',['rp','gen']),C('edit','Edit',['vo','vi']),C('look','Look',['warm','cool']),C('other','Other',['o1','o2'])]};
const state=(v,changes)=>({...v,presets:v.presets.map(p=>p.id in changes?{...p,state:changes[p.id]}:p)});
const after=state(before,{rp:'match',vo:'match',vi:'differs',warm:'differs',o1:'match'});
assert.equal(followNotice(deriveModes(before),deriveModes(after),'rp',after),'“Remove People” also set Edit to “Video only”; Look is now Custom.');
assert.equal(followNotice(deriveModes(before),deriveModes(before),'rp',before),'','nothing changed');
// Many rows: four clauses, then a count.
const many={state:'ready',presets:[m('k','Key','match',['s'])],preset_categories:[C('kc','Key',['k'])]};
for(let i=0;i<6;i++){many.presets.push(m(`p${i}`,`P${i}`,'differs',['s']),m(`q${i}`,`Q${i}`,'differs',['s']));many.preset_categories.push(C(`c${i}`,`Row ${i}`,[`p${i}`,`q${i}`]));}
const manyAfter=state(many,Object.fromEntries([0,1,2,3,4,5].map(i=>[`p${i}`,'match'])));
assert.equal(followNotice(deriveModes(many),deriveModes(manyAfter),'k',manyAfter),
    '“Key” also set Row 0 to “P0”, Row 1 to “P1”, Row 2 to “P2” and Row 3 to “P3”; and 2 more rows changed.');
const amb=state(before,{rp:'match',vo:'match'});
assert.equal(followNotice(deriveModes(before),deriveModes(amb),'rp',amb),'Edit now matches several choices.');

// Plain text for tooltips: the first paragraph on one line, capped.
assert.equal(panelMarkdownPlainText('**Line** one\nline `two`\n\nSecond paragraph'),'Line one line two');
assert.equal(panelMarkdownPlainText('- first\n- second'),'first second');
assert.equal(panelMarkdownPlainText(''),'');
const long=panelMarkdownPlainText('é'.repeat(300),200);assert.equal([...long].length,200);assert.ok(long.endsWith('…'));
console.log('ok');
'''


def test_modes_model(tmp_path):
    run(PRELUDE + MODEL, tmp_path, 'modes-model')


TABLE = r'''
const {mountModes}=await import(MODES_URL);
const base={state:'ready',bindings:[],execution:[],
    presets:[P('a','match',{name:'Text to video',description:'**Makes** video from text.\n\nMore detail.',members:[{binding_id:'task'}]}),
        P('b','differs',{name:'Image to video',members:[{binding_id:'task'}]}),
        P('c','match',{members:[{binding_id:'look'}]}),P('h','match',{members:[{binding_id:'look'}]}),
        P('d','unavailable',{reason:'Unavailable Controls: Steps. Repair them in Manage.'}),P('e','refused',{reason:'Model gone'}),
        P('loose')],
    preset_categories:[C('a-b','Task',['a','b'],{description:'What to make'}),C('y','Look',['c','h']),C('z','Pass',['d','e'])]};
let view=base;
const calls=[],notices=[],details=[],informs=[],tracked=[],fresh=[];let flushOk=true,manageOpened=0,applyGate=null,applyResult={ok:true};
const host={getView:()=>view,
    freshView:async()=>{const next=fresh.shift();return next?await next():{view,fresh:true};},
    async applyChanges(changes){calls.push(changes);if(applyGate)await applyGate.promise;return applyResult;}};
const modes=mountModes(host,{flushPending:async()=>flushOk,track:(work,blocks)=>{tracked.push([work,blocks]);return work;},
    notify:(m,d)=>{notices.push(m);details.push(d);},inform:m=>informs.push(m),openManage:()=>manageOpened++});
document.body.append(modes.el);modes.render(view,{});
const rowOf=id=>walk(modes.el).find(n=>n.dataset.controlsModeCategory===id);
const choice=id=>walk(modes.el).find(n=>n.dataset.controlsModeChoice===id);
const status=id=>walk(rowOf(id)).find(n=>n.dataset.controlsModeStatus==='true');
// The table: one row per drawn category, selection derived, no tie-break.
assert.deepEqual(walk(modes.el).filter(n=>n.dataset.controlsModeCategory).map(n=>n.dataset.controlsModeCategory),['a-b','y','z']);
assert.equal(choice('a').attributes['aria-pressed'],'true');assert.equal(choice('b').attributes['aria-pressed'],'false');
// Name above the choices; every button fills its grid cell, so a category's
// buttons share one width and the tallest one's height, even after a repaint.
{const cells=walk(rowOf('a-b')).find(n=>n.attributes?.role==='group');
assert.match(cells.style.cssText,/grid-auto-rows:1fr/);assert.match(cells.style.cssText,/flex:1 1 100%/);
for(const id of ['a','b'])assert.equal(choice(id).style.height,'100%');}
assert.equal(choice('a').textContent,'Text to video','a description never joins the button name');
assert.equal(status('y').textContent,'“C” and “H” both match. Fix in Manage.');
assert.equal(choice('c').attributes['aria-pressed'],'false');assert.match(choice('c').style.outline,/1px solid/);assert.equal(choice('b').style.outline,'');
assert.equal(status('z').textContent,'Unavailable');assert.equal(choice('d').style.opacity,'0.42');
const line=walk(modes.el).find(n=>n.dataset.controlsModesUncategorized==='true');
assert.equal(line.textContent,"1 choice isn't in a category · Manage");line.dispatch('click');assert.equal(manageOpened,1);
assert.notEqual(walk(modes.el).find(n=>n.dataset.controlsIndividualHeading==='true').style.display,'none');
const group=walk(rowOf('a-b')).find(n=>n.attributes.role==='group');
assert.equal(walk(rowOf('a-b')).find(n=>n.id===group.attributes['aria-labelledby']).textContent,'Task');
// Ids never come from author ids, so "a-b"+"c" and "a"+"b-c" can't collide.
const ids=walk(modes.el).map(n=>n.id).filter(Boolean);assert.equal(new Set(ids).size,ids.length);assert.ok(ids.every(id=>/^cm\d+\.\d+$/.test(id)));

// Custom when nothing matches.
view={...base,presets:base.presets.map(p=>p.id==='a'?{...p,state:'differs'}:p)};modes.render(view,{});
assert.equal(status('a-b').textContent,'Custom');assert.equal(choice('a').attributes['aria-pressed'],'false');
// Custom and Unavailable are tags; the ambiguity sentence is not.
const tagged=id=>walk(status(id)).find(n=>n.tagName==='SPAN'&&n.textContent)?.style.cssText||'';
assert.match(tagged('a-b'),/uppercase/);assert.match(tagged('z'),/uppercase/);assert.doesNotMatch(tagged('y'),/uppercase/);
view=base;modes.render(view,{});

// Hidden in Manage, during a search, while loading, on errors and without categories.
for(const [v,o] of [[base,{manage:true}],[base,{query:'steps'}],[{...base,state:'loading'},{}],[{...base,error:'x'},{}],[{state:'ready',presets:base.presets},{}]]){
    modes.render(v,o);assert.equal(modes.el.style.display,'none',JSON.stringify(o));}
modes.render(view,{});assert.equal(modes.el.style.display,'flex');

// Short description: hover and focus, described by text outside the button.
const desc=id=>walk(modes.el).find(n=>n.id===choice(id).attributes['aria-describedby']);
assert.equal(desc('a').textContent,'Makes video from text.');assert.equal(desc('d').textContent,'Unavailable Controls: Steps. Repair them in Manage.');
assert.equal(choice('b').attributes['aria-describedby'],undefined,'no text, no description');
const tip=()=>walk(rowOf('a-b')).find(n=>n.dataset.controlsModeTip==='true');
assert.equal(tip().attributes['aria-hidden'],'true');
choice('a').dispatch('mouseenter');assert.equal(tip().style.display,'block');assert.equal(tip().textContent,'Makes video from text.');
choice('a').dispatch('mouseleave');assert.equal(tip().style.display,'none');
choice('a').focus();assert.equal(tip().style.display,'block');
let e=key('Escape');assert.ok(e.prevented&&e.stopped,'Escape closes an open tooltip');assert.equal(tip().style.display,'none');
e=key('Escape');assert.ok(!e.stopped,'closed: Escape goes on to the editor');
e=key('Enter');assert.ok(e.stopped&&!e.prevented,'keys on a choice never reach editor single-key shortcuts');
for(const mod of ['ctrlKey','metaKey','altKey']){e=key('z',{[mod]:true});assert.ok(!e.stopped,`${mod}+Z reaches the editor's Undo`);}
e=key('s',{ctrlKey:true});assert.ok(!e.stopped,'Ctrl+S reaches the editor');
const field=new El('input');document.body.append(field);field.focus();
choice('a').dispatch('mouseenter');e=key('Escape');assert.ok(!e.stopped,'an unrelated field keeps its Escape');
choice('a').dispatch('mouseleave');
// A tooltip left open under a hidden panel neither takes Escape nor stays.
document.body.focus?.();document.activeElement=document.body;
choice('a').dispatch('mouseenter');modes.el.offsetParent=null;e=key('Escape');
assert.ok(!e.stopped,'hidden: Escape goes on');assert.equal(tip().style.display,'none');delete modes.el.offsetParent;
// Both headings are headings to assistive technology.
for(const h of [walk(modes.el)[1],walk(modes.el).find(n=>n.dataset.controlsIndividualHeading==='true')]){
    assert.equal(h.attributes.role,'heading');assert.equal(h.attributes['aria-level'],'3');}
assert.equal(walk(modes.el)[1].textContent,'Modes');

// ? shows the full descriptions, the selected choice marked.
const more=walk(rowOf('a-b')).find(n=>n.dataset.controlsModeMore==='true');
assert.equal(more.attributes['aria-expanded'],'false');assert.equal(more.attributes['aria-label'],'About Task');
more.dispatch('click');const block=walk(rowOf('a-b')).find(n=>n.dataset.controlsModeDetails==='true');
assert.equal(more.attributes['aria-expanded'],'true');assert.equal(block.style.display,'flex');
assert.match(block.textContent,/What to make/);assert.match(block.textContent,/Text to video \(selected\)/);assert.match(block.textContent,/More detail/);
assert.ok(walk(block).some(n=>n.tagName==='STRONG'&&n.textContent==='Makes'),'Markdown renders');
more.dispatch('click');assert.equal(block.style.display,'none');
assert.equal(walk(rowOf('y')).find(n=>n.dataset.controlsModeMore==='true').style.display,'none','nothing to describe');

// Stable DOM: a new view of the same categories updates the same buttons.
const kept=choice('b');modes.render({...base,presets:[...base.presets]},{});assert.equal(choice('b'),kept);
// Nothing under a press is rewritten or re-attached: Chromium drops a click whose
// text node was replaced, or whose row was re-appended, between mousedown and mouseup.
const spyText=el=>{let writes=0;const d=Object.getOwnPropertyDescriptor(El.prototype,'textContent');
    Object.defineProperty(el,'textContent',{configurable:true,get(){return d.get.call(this);},set(v){writes++;d.set.call(this,v);}});return ()=>writes;};
const spyMoves=el=>{let moves=0;const remove=el.remove.bind(el);el.remove=()=>{moves++;remove();};return ()=>moves;};
const labelWrites=spyText(kept.children[0]),nameWrites=spyText(walk(rowOf('a-b')).find(n=>n.id===walk(rowOf('a-b')).find(g=>g.attributes.role==='group').attributes['aria-labelledby'])),rowMoves=spyMoves(rowOf('a-b'));
modes.render({...base,presets:[...base.presets]},{});
assert.equal(labelWrites(),0,'the label text stays');assert.equal(nameWrites(),0,'the name text stays');assert.equal(rowMoves(),0,'the row stays attached');
// A changed choice list rebuilds that row's buttons only.
const keptC=choice('c');
modes.render({...base,preset_categories:base.preset_categories.map(c=>c.id==='y'?{...c,choices:['h','c']}:c)},{});
assert.equal(choice('b'),kept,'other rows keep their buttons');assert.notEqual(choice('c'),keptC,'the changed row is rebuilt');
modes.render(view,{});

// Click flow 1: the fresh view shows the choice as the row's only match: no apply.
await choice('a').dispatch('click');await settle();
assert.equal(calls.length,0);assert.equal(tracked.length,1);assert.equal(tracked[0][1],true);assert.equal(await tracked[0][0],true);

// 2: apply, then one notice naming the rows that share a Control.
const applied={...base,presets:base.presets.map(p=>p.id==='a'?{...p,state:'differs'}:p.id==='b'?{...p,state:'match'}:p)};
fresh.push(async()=>({view,fresh:true}),async()=>({view:applied,fresh:true}));
choice('b').dispatch('click');await settle();
assert.deepEqual(calls,[{'preset:b:apply':true}]);assert.equal(await tracked.at(-1)[0],true);assert.deepEqual(informs,[]);

// 3: one apply in flight; the buttons say busy but are never disabled.
applyGate=deferred();view=base;modes.render(view,{});
choice('b').focus();choice('b').dispatch('click');await settle();
assert.equal(choice('a').attributes['aria-disabled'],'true');assert.equal(choice('b').disabled,undefined);assert.equal(document.activeElement,choice('b'));
assert.equal(walk(rowOf('a-b')).find(n=>n.attributes.role==='group').attributes['aria-busy'],'true');
// The theme's hover reads aria-disabled: a busy button doesn't light up.
El.prototype.getAttribute=function(k){return k in this.attributes?this.attributes[k]:null;};
const resting=choice('a').style.background;choice('a').onmouseenter();assert.equal(choice('a').style.background,resting);
choice('a').dispatch('click');await settle();assert.equal(calls.length,2,'a second click waits for none');assert.equal(tracked.length,3);
applyGate.resolve();applyGate=null;await settle();assert.equal(choice('a').attributes['aria-disabled'],undefined);
assert.equal(walk(rowOf('a-b')).find(n=>n.attributes.role==='group').attributes['aria-busy'],undefined);
choice('a').onmouseenter();assert.notEqual(choice('a').style.background,resting,'idle: hover lights it');choice('a').onmouseleave();
delete El.prototype.getAttribute;

// 4: a failed flush applies nothing and refuses the run.
flushOk=false;choice('b').dispatch('click');await settle();assert.equal(calls.length,2);
assert.equal(await tracked.at(-1)[0],false);assert.match(notices.at(-1),/not applied because a Control could not be saved/);flushOk=true;

// 5: a refused apply is a run failure with its reason.
applyResult={ok:false,rejected:[{reason:'Steps: too high'}]};choice('b').dispatch('click');await settle();
assert.equal(await tracked.at(-1)[0],false);assert.equal(notices.at(-1),'Steps: too high');applyResult={ok:true};
// A thrown error: a plain message, the error's text as its detail.
applyResult=null;const apply=host.applyChanges;host.applyChanges=async()=>{throw Error('socket closed');};
choice('b').dispatch('click');await settle();host.applyChanges=apply;applyResult={ok:true};
assert.equal(await tracked.at(-1)[0],false);assert.equal(notices.at(-1),'The choice could not be applied.');assert.equal(details.at(-1),'socket closed');

// 6: a view that isn't fresh decides from the click and applies; no notice.
const before=calls.length;fresh.push(async()=>({view:{state:'error',error:'x'},fresh:false}));
choice('b').dispatch('click');await settle();assert.equal(calls.length,before+1);assert.equal(await tracked.at(-1)[0],true);

// 7: an already-matching choice in an ambiguous row explains instead of applying.
choice('c').dispatch('click');await settle();assert.equal(calls.length,before+1);assert.equal(notices.at(-1),'“C” and “H” both match. Fix in Manage.');

// 8: an unavailable choice explains and touches nothing.
const t=tracked.length;choice('d').dispatch('click');await settle();
assert.equal(tracked.length,t);assert.equal(notices.at(-1),'Unavailable Controls: Steps. Repair them in Manage.');

// 9: the notice, and none for a row that shares no Control.
const look={...base,presets:[...base.presets,P('w2','match',{name:'Warm',members:[{binding_id:'task'},{binding_id:'tone'}]}),P('k2','differs',{name:'Cool',members:[{binding_id:'tone'}]}),
    P('o1','differs',{name:'One',members:[{binding_id:'other'}]}),P('o2','differs',{name:'Two',members:[{binding_id:'other'}]})],
    preset_categories:[...base.preset_categories,C('tone','Tone',['w2','k2']),C('other','Other',['o1','o2'])]};
view=look;modes.render(view,{});
const landed={...look,presets:look.presets.map(p=>({a:'differs',b:'match',w2:'differs',o1:'match'})[p.id]?{...p,state:({a:'differs',b:'match',w2:'differs',o1:'match'})[p.id]}:p)};
fresh.push(async()=>({view:look,fresh:true}),async()=>({view:landed,fresh:true}));
choice('b').dispatch('click');await settle();assert.deepEqual(informs,['Tone is now Custom.']);

// 10: no notice after destroy.
const gate=deferred();fresh.push(async()=>({view:look,fresh:true}),async()=>{await gate.promise;return {view:landed,fresh:true};});
choice('b').dispatch('click');await settle();modes.destroy();gate.resolve();await settle();assert.equal(informs.length,1);
console.log('ok');
'''


def test_modes_table_and_click_flow(tmp_path):
    run(PRELUDE + TABLE, tmp_path, 'modes-table')


WORKFLOW_SWITCH = r'''
const {mountModes}=await import(MODES_URL);
const view={state:'ready',presets:[P('a','match',{members:[{binding_id:'s'}]}),P('b','differs',{members:[{binding_id:'s'}]}),
    P('c','match',{members:[{binding_id:'s'}]}),P('d','differs',{members:[{binding_id:'s'}]})],
    preset_categories:[C('x','X',['a','b']),C('y','Y',['c','d'])]};
const landed={...view,presets:view.presets.map(p=>({...p,state:{a:'differs',b:'match',c:'differs',d:'differs'}[p.id]}))};
const informs=[];const wf={id:'A'};const gate=deferred();const fresh=[async()=>({view,fresh:true}),async()=>{await gate.promise;return {view:landed,fresh:true};}];
const host={getView:()=>view,freshView:async()=>fresh.shift()(),applyChanges:async()=>({ok:true}),workflows:{current:()=>({template_id:wf.id})}};
const modes=mountModes(host,{flushPending:async()=>true,track:w=>w,notify:()=>{},inform:m=>informs.push(m)});
modes.render(view,{});walk(modes.el).find(n=>n.dataset.controlsModeChoice==='b').dispatch('click');await settle();
wf.id='B';gate.resolve();await settle();assert.deepEqual(informs,[],'the notice is for the workflow clicked');
modes.destroy();console.log('ok');
'''


def test_modes_no_notice_after_a_workflow_switch(tmp_path):
    run(PRELUDE + WORKFLOW_SWITCH, tmp_path, 'modes-switch')


PANEL = r'''
// The real panel: Modes above Individual Controls, a click right after typing
// applies once after the pending save, and only the apply blocks Generate.
let view={state:'ready',notes:[],execution:[],
    bindings:[{source:'binding',binding_id:'steps',label:'Steps',kind:'INT',value:'20',status:'ok',order:1,group:'',section:'workflow',note:''},
        {source:'binding',binding_id:'gone',label:'Gone',kind:'INT',value:'1',status:'missing',order:2,group:'',section:'workflow',note:''}],
    presets:[P('a','match',{name:'Fast',members:[{binding_id:'steps',preview:'20',status:'ok'}]}),P('b','differs',{name:'Slow',members:[{binding_id:'steps',preview:'40',status:'ok'}]}),
        P('u','differs',{name:'Loose',members:[{binding_id:'gone',preview:'1',status:'unavailable'}]})],
    preset_categories:[C('speed','Speed',['a','b'])],relay:{status:'ok'},cleanup:{preset_members:[{preset_id:'u',binding_id:'gone'}]}};
const calls=[],listeners=new Set(),informs=[],notices=[];let stored=20;const saving=deferred();
const publish=next=>{view=next;for(const cb of listeners)cb(view);};
const host={getView:()=>view,subscribe(cb){listeners.add(cb);return()=>listeners.delete(cb);},getEditorSettings:()=>({controls:{view:'rows'},layout:{scaleControls:1}}),
    notify:m=>notices.push(m),inform:m=>informs.push(m),freshView:async()=>({view,fresh:true}),
    async applyChanges(changes){calls.push(changes);
        if('binding:steps:value' in changes){await saving.promise;stored=Number(changes['binding:steps:value']);
            // The save's own refresh re-renders the panel before the click lands.
            publish({...view,bindings:view.bindings.map(b=>b.binding_id==='steps'?{...b,value:String(stored)}:b),
                presets:view.presets.map(p=>p.id==='a'?{...p,state:stored===20?'match':'differs'}:p)});}
        if('preset:b:apply' in changes)publish({...view,presets:view.presets.map(p=>p.id==='b'?{...p,state:'match'}:p.id==='a'?{...p,state:'differs'}:p)});
        return {ok:true,rejected:[]};}};
const container=new El('div');document.body.append(container);const panel=mountControlsPanel(container,host);
const byTarget=t=>walk(container).find(n=>n.dataset.controlTarget===t);
const choice=id=>walk(container).find(n=>n.dataset.controlsModeChoice===id);
const sections=walk(container).filter(n=>n.dataset.controlsModes==='true'||n.dataset.controlsWorkflowHeader==='true');
assert.equal(sections.length,2);
const heading=walk(container).find(n=>n.dataset.controlsIndividualHeading==='true');assert.equal(heading.textContent,'Individual Controls');
// Type a value, then press a choice: the field's blur saves, the save re-renders
// the panel, and the same button still takes the click.
const field=walk(byTarget('binding:steps:value')).find(n=>n.tagName==='INPUT');field.focus();field.value='33';
// The press: the field commits (change, then blur) and its save is still pending.
const slow=choice('b');field.dispatch('change');slow.focus();await settle();
slow.dispatch('click');await settle();
assert.deepEqual(calls,[{'binding:steps:value':'33'}],'the choice waits for the pending save');
saving.resolve();await settle();
assert.equal(choice('b'),slow,'the save re-rendered the panel and kept the pressed button');
assert.deepEqual(calls,[{'binding:steps:value':'33'},{'preset:b:apply':true}],'saved first, then applied once');
assert.equal(choice('b').attributes['aria-pressed'],'true');
assert.equal(await panel.flushPending(),true);

// No "preset" anywhere a user reads: normal view, Manage with a choice open, Cleanup.
const wording=(root,where)=>{for(const n of walk(root)){
    for(const text of [n._text,n.attributes?.['aria-label'],n.title,n.placeholder,n.attributes?.title])
        if(typeof text==='string')assert.doesNotMatch(text,/preset/i,`${where}: ${text}`);}};
wording(container,'normal view');
walk(container).find(n=>n.dataset.controlsModesUncategorized==='true').dispatch('click');await settle();
const manage=walk(container).find(n=>n.dataset.controlsModesManage==='true');assert.ok(manage,'the uncategorized line opens Manage');
walk(manage).find(n=>n.dataset.controlsModesChoice==='a').children[0].dispatch('click');await settle();
assert.ok(walk(container).some(n=>n.attributes?.['aria-label']==='Include Steps in this choice'));
// The In this choice line is kept across repaints, ticked state included: a
// checkbox replaced between mousedown and mouseup gets no click.
{const box=()=>walk(container).find(n=>n.attributes?.['aria-label']==='Include Steps in this choice');
const first=box(),line=first.parentElement;first.checked=!first.checked;first.dispatch('change');const ticked=first.checked;
panel.render();await settle();
assert.equal(box(),first);assert.equal(box().parentElement,line);assert.equal(box().checked,ticked);
first.checked=!first.checked;first.dispatch('change');}
wording(container,'Manage');
walk(container).find(n=>n.tagName==='BUTTON'&&n.textContent==='Clean up unavailable…').dispatch('click');await settle();
assert.match(container.textContent,/Settings in choices/);
wording(container,'Cleanup');
panel.destroy();console.log('ok');
'''


def test_modes_in_the_panel(tmp_path):
    run(PRELUDE + PANEL, tmp_path, 'modes-panel')


CARDS = r'''
// Cards view draws the same Modes table above the Individual Controls.
const view={state:'ready',notes:[],execution:[],
    bindings:[{source:'binding',binding_id:'steps',label:'Steps',kind:'INT',value:'20',status:'ok',order:1,group:'',section:'workflow',note:''}],
    presets:[P('a','match',{name:'Fast',members:[{binding_id:'steps'}]}),P('b','differs',{name:'Slow',members:[{binding_id:'steps'}]})],
    preset_categories:[C('speed','Speed',['a','b'])]};
const host={getView:()=>view,subscribe:()=>()=>{},getEditorSettings:()=>({controls:{view:'cards'},layout:{scaleControls:1}}),
    freshView:async()=>({view,fresh:true}),applyChanges:async()=>({ok:true})};
const container=new El('div');document.body.append(container);const panel=mountControlsPanel(container,host);
const modes=walk(container).find(n=>n.dataset.controlsModes==='true');
assert.equal(modes.style.display,'flex');
assert.equal(walk(modes).find(n=>n.dataset.controlsModeChoice==='a').attributes['aria-pressed'],'true');
const order=walk(container);
assert.ok(order.indexOf(modes)<order.indexOf(walk(container).find(n=>n.dataset.controlTarget==='binding:steps:value')),'above the Controls');
panel.destroy();console.log('ok');
'''


def test_modes_in_cards_view(tmp_path):
    run(PRELUDE + CARDS, tmp_path, 'modes-cards')


WORKFLOW_MENU = r'''
const {mountWorkflowHeader}=await import(HEADER_URL);
const calls=[],notices=[];let flushOk=true,canLeave=true,flushes=0;
const wf={current:{template_id:'A',name:'Alpha'},busy:false};
const workflows={current:()=>wf.current,busy:()=>wf.busy,
    list:async()=>({ok:true,payload:{current:'A',workflows:[
        {template_id:'B',name:'Beta',ok:true,favorite:true,description:'Detailing'},
        {template_id:'A',name:'Alpha',ok:true,favorite:false,description:''},
        {template_id:'C',name:'Broken',ok:false,favorite:false,description:''}]}}),
    switchTo:async id=>{calls.push(['switch',id,flushes]);return {ok:true};},
    setFavorite:async(id,favorite)=>{calls.push(['favorite',id,favorite]);return {ok:true};}};
const view={state:'ready',description:'**Workflow help**',presets:[],bindings:[]};
const host={getView:()=>view,workflows};
const section=mountWorkflowHeader(host,{notify:m=>notices.push(m),canSwitch:()=>canLeave,
    flushPending:async()=>{flushes++;return flushOk;}});
document.body.append(section.el);section.render(view);await settle();
const menu=walk(section.el).find(n=>n.dataset.controlsWorkflowMenu==='true');
const select=walk(menu).find(n=>n.tagName==='SELECT'),star=walk(menu).find(n=>n.tagName==='BUTTON');
// Favorites first (the server's order), the current one selected, a broken one disabled.
assert.deepEqual(select.children.map(o=>[o.value,o.textContent,!!o.disabled]),[['B','★ Beta',false],['A','Alpha',false],['C','Broken',true]]);
assert.equal(select.value,'A');assert.equal(star.textContent,'☆');assert.equal(star.attributes['aria-pressed'],'false');
// Workflow ▾ comes before the workflow's description.
assert.ok(section.el.children.indexOf(menu)===0);assert.match(section.el.textContent,/Workflow help/);
// A failed save refuses the switch; a declined Manage draft cancels it.
flushOk=false;select.value='B';select.dispatch('change');await settle();
assert.deepEqual(calls,[]);assert.match(notices.at(-1),/not switched/);assert.equal(select.value,'A');
flushOk=true;canLeave=false;select.value='B';select.dispatch('change');await settle();assert.deepEqual(calls,[]);
canLeave=true;select.value='B';select.dispatch('change');await settle();
assert.deepEqual(calls,[['switch','B',3]],'switched only after its own flush');
// Disabled while a Generate is between click and response.
wf.busy=true;section.render(view);assert.equal(select.disabled,true);assert.equal(star.disabled,true);
wf.busy=false;section.render(view);assert.equal(select.disabled,false);
// The star stars the current workflow.
star.dispatch('click');await settle();assert.deepEqual(calls.at(-1),['favorite','A',true]);
// Manage hides the description, never the Workflow menu.
section.render(view,{manage:true});assert.equal(section.el.style.display,'flex');assert.notEqual(menu.style.display,'none');
// Keys on the menu stay with it.
select.focus();let e=key('ArrowDown');assert.ok(e.stopped&&!e.prevented);
// Shortcuts and Escape go on to the editor.
e=key('z',{ctrlKey:true});assert.ok(!e.stopped,'Ctrl+Z');e=key('Escape');assert.ok(!e.stopped,'Escape');
// An equal render rebuilds nothing: no options, no description, no label text.
let rebuilt=0;const description=walk(section.el).find(n=>n.tagName==='STRONG'&&n.textContent==='Workflow help').parentElement.parentElement;
for(const el of [select,description]){const rc=el.replaceChildren.bind(el);el.replaceChildren=(...a)=>{rebuilt++;rc(...a);};}
const d=Object.getOwnPropertyDescriptor(El.prototype,'textContent');let writes=0;
const more=walk(section.el).find(n=>n.tagName==='BUTTON'&&n.textContent==='More');
for(const el of [star,more])Object.defineProperty(el,'textContent',{configurable:true,get(){return d.get.call(this);},set(v){writes++;d.set.call(this,v);}});
section.render(view);section.render(view);assert.equal(rebuilt,0);assert.equal(writes,0);
section.render({...view,description:'Other'});assert.equal(rebuilt,1,'a new description is rebuilt');
section.destroy();
// Canvas hosts have no workflows capability: no menu, and nothing to show in Manage.
const canvas=mountWorkflowHeader({getView:()=>view},{notify:()=>{},flushPending:async()=>true});
canvas.render(view);assert.equal(walk(canvas.el).some(n=>n.dataset.controlsWorkflowMenu==='true'),false);
canvas.render(view,{manage:true});assert.equal(canvas.el.style.display,'none');canvas.destroy();
console.log('ok');
'''


def test_the_workflow_menu_saves_asks_and_switches(tmp_path):
    run(PRELUDE + WORKFLOW_MENU, tmp_path, 'workflow-menu')
