"""Real canvas writer and shared Manage review; no source-string assertions."""
import json
import subprocess
from pathlib import Path

import pytest
from test_controls_host_canvas_js import HARNESS
from test_controls_presets_js import PRELUDE

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("failure", [False, True])
def test_canvas_cleanup_definition_anchors_single_step_and_rollback(failure):
    body = r'''
const b = await import(BINDINGS_URL);
const definition = new Graph(); root.subgraphs.set('d',definition);
const inner = add(definition,'Sampler',99, [widget('steps',4)], ['steps']);
inner.properties.sonderBindings={steps:'lost',future:'definition'};
const orphan=add(root,'Sampler',8,[widget('steps',5)],['steps']); orphan.properties.sonderBindings={steps:'orphan'};
relay.widgets[0].value=JSON.stringify({bindings:{lost:{label:'Lost'},steps:{label:'Steps'}},notes:{},
    presets:{p:{name:'Keep',order:0,members:{lost:{value:3},steps:{value:20}},future:'keep'},q:{name:'Delete',order:1,members:{gone:{value:1}}}}});
const before=relay.widgets[0].value, prior=b.anchorSnapshot(inner), orphanPrior=b.anchorSnapshot(orphan);
if(FAILURE)relay.widgets[0].callback=()=>{throw Error('manifest failed');};
const intent={bindings:['lost'],suggestions:['orphan'],definition_anchors:['definition'],values:[],hidden:[],
    preset_members:[{preset_id:'q',binding_id:'gone'}]};
const result=await host.applyChanges({'workflow:cleanup':intent});
assert.equal(result.ok,!FAILURE,JSON.stringify(result));
if(FAILURE){
    assert.equal(relay.widgets[0].value,before);assert.deepEqual(b.anchorSnapshot(inner),prior);
    assert.deepEqual(b.anchorSnapshot(orphan),orphanPrior);assert.equal(captures,1);
}else{
    assert.equal(captures,2);assert.deepEqual(b.readAnchors(inner),{});assert.deepEqual(b.readAnchors(orphan),{});
    const saved=JSON.parse(relay.widgets[0].value);assert.ok(saved.bindings.steps);assert.equal(saved.bindings.lost,undefined);
    assert.equal(saved.presets.q,undefined);assert.equal(saved.presets.p.members.lost.value,3);assert.equal(saved.presets.p.future,'keep');
    assert.ok(calls[0].definition_anchors.includes('definition'));
    // A now-active binding can never be removed even if a server response were stale.
    const active=await host.applyChanges({'workflow:cleanup':{...intent,bindings:['steps'],suggestions:[],definition_anchors:[],preset_members:[]}});
    assert.equal(active.ok,false);assert.equal(captures,2);
}
'''
    script = HARNESS.replace("host.destroy();assert.equal(listeners.size,0);", body + "\nhost.destroy();assert.equal(listeners.size,0);")
    script = script.replace("MODULE_URL", json.dumps((ROOT / "web/js/controls_host_canvas.js").as_uri()))
    script = script.replace("BINDINGS_URL", json.dumps((ROOT / "web/js/control_bindings.js").as_uri()))
    script = script.replace("SCENARIO", '"cleanup"').replace("FAILURE", str(failure).lower())
    result = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_shared_review_selection_flush_refusal_and_lifecycle():
    body = r'''
const {mountCleanup}=await import(CLEANUP_URL);
let view={state:'ready',relay:{status:'ok'},cleanup:{bindings:[{id:'lost',label:'Lost'}],values:[{id:'old',label:'Old',preview:'42'}],
    preset_members:[{preset_id:'p',binding_id:'lost',label:'Preset · Lost',requires_binding:'lost'},
                    {preset_id:'p',binding_id:'gone',label:'Preset · Gone',requires_binding:null}]}};
const calls=[],tracked=[],notices=[];let flush,apply;
const host={getView:()=>view,applyChanges:changes=>{calls.push(changes);return new Promise(r=>apply=r);}};
const cleanup=mountCleanup(host,{track:p=>{tracked.push(p);return p;},notify:m=>notices.push(m),flushPending:()=>new Promise(r=>flush=r)});
document.body.append(cleanup.el);cleanup.render();
const button=text=>walk(cleanup.el).find(n=>n.tagName==='BUTTON'&&n.textContent===text);
button('Clean up unavailable…').dispatch('click');assert.equal(calls.length,0);assert.equal(cleanup.editing,true);
assert.match(cleanup.el.textContent,/Replace back/);assert.match(cleanup.el.textContent,/last member/);
const checks=walk(cleanup.el).filter(n=>n.tagName==='INPUT');assert.ok(checks.every(c=>c.checked));
checks[0].checked=false;checks[0].dispatch('change');assert.equal(checks[2].disabled,true);
checks[3].checked=false;
button('Clean up').dispatch('click');assert.equal(calls.length,0);assert.equal(cleanup.busy,true);
flush(true);await settle();assert.deepEqual(calls[0]['workflow:cleanup'].values,['old']);
assert.deepEqual(calls[0]['workflow:cleanup'].bindings,[]);assert.deepEqual(calls[0]['workflow:cleanup'].preset_members,[]);
apply({ok:false,rejected:[{reason:'An item is no longer unavailable.'}]});await tracked[0];
assert.equal(cleanup.editing,true);assert.match(notices[0],/no longer unavailable/);
button('Cancel').dispatch('click');assert.equal(cleanup.editing,false);
view={...view,error:'unreadable'};cleanup.render();assert.equal(button('Clean up unavailable…').disabled,true);
view={...view,error:null};cleanup.render();button('Clean up unavailable…').dispatch('click');
button('Clean up').dispatch('click');flush(false);await tracked[1];assert.equal(calls.length,1);
cleanup.destroy();assert.equal(cleanup.el.parentElement,null);
'''
    script = (PRELUDE + body).replace("SCENARIO", '"cleanup"')
    script = script.replace("CLEANUP_URL", json.dumps((ROOT / "web/js/controls_cleanup.js").as_uri()))
    for key, path in [("PANEL_URL", "editor_controls_panel.js"), ("MARKDOWN_URL", "panel_markdown.js"),
                      ("NOTES_URL", "controls_note_editor.js"), ("KEYBOARD_URL", "keyboard_ownership.js")]:
        script = script.replace(key, json.dumps((ROOT / "web/js" / path).as_uri()))
    result = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
