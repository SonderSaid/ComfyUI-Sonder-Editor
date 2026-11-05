"""Links to docs/sonder-app-setup.md (0.8 hardening, Phase 12B, B3): only where a
need is something to install, only in the Sonder App's surfaces (its Modes run
line and a grey choice's ? block, the Launcher's missing models), never for a
problem alone or a LoRA row naming no file, and never on a canvas host."""
import json
from pathlib import Path

from test_controls_modes_js import PRELUDE, run

ROOT = Path(__file__).resolve().parents[1]
GUIDE = 'https://github.com/SonderSaid/ComfyUI-Sonder-Editor/blob/main/docs/sonder-app-setup.md'


def urls(script):
    for placeholder, name in {'DOCS_URL': 'docs_links.js', 'WORKFLOWS_URL': 'launcher_workflows.js',
                              'BAR_URL': 'launcher_open_bar.js'}.items():
        script = script.replace(placeholder, json.dumps((ROOT / 'web/js' / name).as_uri()))
    return script.replace('__GUIDE__', json.dumps(GUIDE))


COMMON = r'''
const tick=async()=>{for(let i=0;i<12;i++)await Promise.resolve();await new Promise(r=>setTimeout(r,0));};
const model=(file,extra={})=>({file,folder:'loras',relative:`models/loras/${file}`,node:'Load LoRA',input:'lora_name',...extra});
const answer=(choices={},run={})=>({ok:true,template_id:'T',seq:1,run:{missing_nodes:[],missing_models:[],problems:[],...run},choices});
const shown=n=>!!n&&n.style.display!=='none';
'''

RULE = r'''
const {SETUP_GUIDE_URL,asksForSetup,namesAFile}=await import(DOCS_URL);
assert.equal(SETUP_GUIDE_URL,__GUIDE__);
assert.equal(namesAFile(model('a.safetensors')),true);assert.equal(namesAFile(model('LTX\\sub\\a.safetensors')),true);
assert.equal(namesAFile(model('None')),false,'a LoRA row naming no file');assert.equal(namesAFile({}),false);
assert.equal(asksForSetup({missing_nodes:['N'],missing_models:[]}),true,'a node to install');
assert.equal(asksForSetup({missing_nodes:[],missing_models:[model('a.safetensors')]}),true,'a file to install');
assert.equal(asksForSetup({missing_nodes:[],missing_models:[model('None')]}),false,'fixed in Controls, not by installing');
assert.equal(asksForSetup({missing_nodes:[],missing_models:[model('None'),model('b.pt')]}),true);
assert.equal(asksForSetup({missing_nodes:[],missing_models:[],problems:['Switch a Save on.']}),false,'a problem alone');
assert.equal(asksForSetup(null),false);
const {asksForModels}=await import(WORKFLOWS_URL);
assert.equal(asksForModels({missing_models:[model('a.safetensors')]}),true);assert.equal(asksForModels({missing_models:[model('None')]}),false);
assert.equal(asksForModels({}),false);assert.equal(asksForModels(null),false);
console.log('ok');
'''

MODES = r'''
const {mountModes}=await import(MODES_URL);
let overlay=answer();const listeners=new Set();
const view={state:'ready',presets:[P('a','match',{description:'About A'}),P('b','differs',{description:'About B'}),P('c','differs',{description:'About C'})],preset_categories:[C('x','Task',['a','b','c'])]};
const availability={get:()=>overlay,packs:()=>null,subscribe(cb){listeners.add(cb);return()=>listeners.delete(cb);},refresh:async()=>true};
const modes=mountModes({getView:()=>view,availability,freshView:async()=>({view,fresh:true}),applyChanges:async()=>({ok:true})},
    {flushPending:async()=>true,track:w=>w,notify:()=>{}});
document.body.append(modes.el);
const help=()=>walk(modes.el).find(n=>n.dataset.controlsModesRunHelp==='true');
const line=()=>walk(modes.el).find(n=>n.dataset.controlsModesRun==='true');
const blockHelp=id=>walk(modes.el).find(n=>n.dataset.controlsModeSetupHelp===id);
const paint=next=>{overlay=next;modes.render(view,{});};
// The run line's link: a file or a node to install.
paint(answer({},{missing_models:[model('a.safetensors')]}));
assert.ok(shown(line()));assert.ok(shown(help()));
assert.equal(help().href,__GUIDE__);assert.equal(help().target,'_blank');assert.equal(help().rel,'noopener noreferrer');
assert.equal(help().textContent,'How to install ↗');
paint(answer({},{missing_nodes:['N']}));assert.ok(shown(help()));
// Not for a problem alone, nor a LoRA row that names no file: nothing to install.
paint(answer({},{problems:['Switch a Save on.']}));assert.ok(shown(line()));assert.ok(!shown(help()));
paint(answer({},{missing_models:[model('None')]}));assert.ok(shown(line()));assert.ok(!shown(help()));
paint(answer());assert.ok(!shown(line()));
// Manage and search hide the line, link and all.
paint(answer({},{missing_models:[model('a.safetensors')]}));modes.render(view,{manage:true});assert.ok(!shown(line()));
// A grey choice's ? block: the link under what it needs, only for an install.
paint(answer({b:{missing_nodes:[],missing_models:[model('b.safetensors')]},c:{missing_nodes:[],missing_models:[model('None')]}}));
walk(modes.el).find(n=>n.dataset.controlsModeMore==='true').dispatch('click');
assert.ok(blockHelp('b'),'grey for a missing file: the link');assert.equal(blockHelp('b').href,__GUIDE__);
assert.equal(blockHelp('c'),undefined,'a None row: no link');assert.equal(blockHelp('a'),undefined,'not grey: no link');
// The answer clears: the open block follows, and the link goes.
overlay=answer();for(const cb of listeners)cb();assert.equal(blockHelp('b'),undefined);
modes.destroy();
// A host with no availability (canvas, mounted): no line, no link.
{const plain=mountModes({getView:()=>view},{flushPending:async()=>true,track:w=>w,notify:()=>{}});document.body.append(plain.el);plain.render(view,{});
walk(plain.el).find(n=>n.dataset.controlsModeMore==='true').dispatch('click');
assert.ok(!shown(walk(plain.el).find(n=>n.dataset.controlsModesRunHelp==='true')));
assert.equal(walk(plain.el).find(n=>n.dataset.controlsModeSetupHelp),undefined);plain.destroy();}
console.log('ok');
'''

LAUNCHER = r'''
// The Launcher marks its picker with classList, which the panel's fake DOM lacks.
if(!Object.getOwnPropertyDescriptor(El.prototype,'classList'))Object.defineProperty(El.prototype,'classList',{get(){const el=this;return {add:c=>{el.className=`${el.className||''} ${c}`.trim();},remove(){},contains:()=>false,toggle(){}};}});
const {mountOpenBar}=await import(BAR_URL);
const {mountWorkflows}=await import(WORKFLOWS_URL);
const template=(extra={})=>({template_id:'T',name:'W',starter:true,model_family:'LTX',validation:{controls:[]},run_missing_nodes:[],run_problems:[],...extra});
const stateOf=t=>({templates:[t],selectedTemplateId:'T',projects:[{folder:'p',name:'P'}],selectedProject:'p',
    projectData:{unreadable:[],workflow_order:['T'],format_first_release:{}},loaded:{templates:true,projects:true},loadFailed:{},
    status:null,nodePacks:null,memory:{hidden:{},favorites:[],last_workflow:{}},prefs:{showHidden:false},workflowSearch:'',workflowExpanded:false});
const links=root=>walk(root).filter(n=>n.dataset?.setupGuide==='true');
const actions=new Proxy({},{get:()=>()=>{}});
for(const [label,t,expected] of [['a missing file',template({missing_models:[model('a.safetensors')]}),1],
                                 ['a None row only',template({missing_models:[model('None')]}),0],
                                 ['nothing missing',template({missing_models:[]}),0],
                                 ['not checked yet',template({}),0]]){
    const bar=mountOpenBar(stateOf(t),actions),side=mountWorkflows(stateOf(t),actions);
    assert.equal(links(bar.element).length,expected,`Open bar, ${label}`);
    assert.equal(links(side.element).length,expected,`row details, ${label}`);
    for(const link of [...links(bar.element),...links(side.element)]){
        assert.equal(link.href,__GUIDE__);assert.equal(link.target,'_blank');assert.equal(link.rel,'noopener noreferrer');
        assert.equal(link.textContent,'How to install missing models ↗');}
    bar.cleanup();side.cleanup();
}
console.log('ok');
'''


def test_the_rule_for_when_a_need_asks_for_the_guide(tmp_path):
    run(urls(PRELUDE + COMMON + RULE), tmp_path, 'setup-guide-rule')


def test_the_modes_links_show_only_for_an_install_and_only_with_availability(tmp_path):
    run(urls(PRELUDE + COMMON + MODES), tmp_path, 'setup-guide-modes')


def test_the_launcher_links_beside_missing_model_files(tmp_path):
    run(urls(PRELUDE + COMMON + LAUNCHER), tmp_path, 'setup-guide-launcher')


def test_the_canvas_host_still_has_no_availability():
    # The Modes links hang off `host.availability`, which only the Sonder App host has.
    for name in ('controls_host_canvas.js', 'editor_node_controller.js'):
        assert 'availability' not in (ROOT / 'web/js' / name).read_text(encoding='utf-8'), name
