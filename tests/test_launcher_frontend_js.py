"""Launcher preference tolerance, list projections and all-problem ordering."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / 'web' / 'js'


def test_launcher_projections_and_import_safety():
    if not shutil.which('node'):
        pytest.skip('Node unavailable')
    script = r'''
globalThis.window = { addEventListener() {}, localStorage: {getItem() {return null;}} };
globalThis.localStorage = window.localStorage;
globalThis.document = { body: {dataset: {}}, createElement() {throw Error('DOM touched on import');}, querySelector() {return null;}, addEventListener() {} };
const base = __BASE__;
const modules = {};
for (const name of ['launcher_view', 'launcher_dialog', 'launcher_header', 'launcher_workflows', 'launcher_projects', 'launcher_open_bar', 'sdk_landing']) modules[name] = await import(base + name + '.js');
const settings = await import(base + 'editor_settings.js');
const defaults = settings.normalizeEditorSettings().sdk.launcher;
const invalid = settings.normalizeEditorSettings({sdk:{launcher:{thumbnails:'bad',view:'bad',tileSize:'XL',showHidden:'false'}}}).sdk.launcher;
const valid = settings.normalizeEditorSettings({sdk:{launcher:{thumbnails:'still',view:'list',tileSize:'S',showHidden:true}}}).sdk.launcher;
const problems = modules.launcher_open_bar.pairProblems({too_new:true, format_version:4, written_with:'0.8',upgrade:{from:1,to:2,when:'prompt_history'}}, {validation:{blocking:['blocked'],warnings:['note']},missing_models:[{file:'m.pt',folder:'vae',node:'VAE'}]}, {install:{ready:false}}, {2:'0.4.0'});
const state = {projects:[{folder:'a',name:'Same',modified_at:'2026-10-01',hidden:false},{folder:'b',name:'Same',modified_at:'2026-10-07',hidden:true},{folder:'c',name:'Other',modified_at:'2026-10-05'}], projectData:{unreadable:[{folder:'broken',hidden:false}]},projectSearch:'',prefs:{showHidden:false,view:'grid'},sort:{key:'name',direction:1}};
const visible = modules.launcher_projects.visibleProjects(state).map(p=>p.folder);
state.prefs.showHidden=true;state.projectSearch='b';
const searched = modules.launcher_projects.visibleProjects(state).map(p=>p.folder);
const names = modules.launcher_workflows.generatingWorkflows;
const exitNames = [names([{template_name:'Alpha'},{template_name:'Beta'},{template_name:'Alpha'}], 'Now'), names([{}], 'Now'), names([], 'Now')];
const lw = modules.launcher_workflows, mappings = {
    'comfyui-kjnodes': [['VAELoaderKJ', 'LTXVAudioVideoMask'], {title_aux: 'KJNodes for ComfyUI'}],
    'rgthree-comfy': [['Seed (rgthree)'], {title_aux: 'rgthree-comfy', nodename_pattern: ' \\(rgthree\\)$'}],
    'py-only': [[], {title_aux: 'Python only', nodename_pattern: '(?i)never'}],
    'dup-a': [['Shared'], {title: 'Pack A'}], 'dup-b': [['Shared'], {title_aux: 'Pack B'}],
};
const classes = ['LTXVAudioVideoMask', 'Power Lora Loader (rgthree)', 'Shared', 'VAELoaderKJ', 'Unknown'];
const missingTemplate = {validation: {ok: false, errors: [], missing_nodes: ['VAELoaderKJ']}};
const missingCases = {
    packs: lw.missingNodePacks(classes, mappings),
    withManager: lw.missingNodesMessage(['VAELoaderKJ', 'Power Lora Loader (rgthree)'], mappings),
    withoutManager: lw.missingNodesMessage(['VAELoaderKJ'], null),
    badge: lw.workflowBadge(missingTemplate),
    problems: modules.launcher_open_bar.pairProblems({}, missingTemplate, {}, {}, mappings).map(p => [p.kind, p.text]),
    models: [
        modules.launcher_view.modelMessage({file: 'LTX\\IC Lora\\a.safetensors', relative: 'models/loras/LTX/IC Lora/a.safetensors', path: 'D:\\c\\models\\loras\\LTX\\IC Lora\\a.safetensors', node: 'Load LoRA'}),
        modules.launcher_view.modelMessage({file: 'LTX/a.safetensors', relative: 'models/loras/LTX/a.safetensors', node: 'Load LoRA'}),
        modules.launcher_view.modelMessage({file: 'm.pt', folder: null, relative: null, node: 'Odd', input: 'model'}),
    ],
};
const roots = ['/sonder-editor/app', '/api/sonder-editor/app', '/comfy/api/sonder-editor/app', '/comfy/sonder-editor/app/'].map(modules.launcher_header.comfyRoot);
const next = modules.launcher_workflows.nextExitNotice;
const generating = {runs:[{state:'running',template_name:'Alpha'},{state:'landed',template_name:'Beta'}],project:'P'};
const exitNotices = [next(null, generating), next({generating:1}, {runs:[{state:'landed'}]}, {project:'Q'}), next(null, {runs:[]}), next({generating:2,project:'P',workflow:'Alpha'}, null)];
console.log(JSON.stringify({defaults,invalid,valid,problems,visible,searched,exitNames,exitNotices,missingCases,roots,sample:modules.launcher_workflows.SAMPLE_PROJECTS_URL}));
'''.replace('__BASE__', json.dumps(JS.as_uri() + '/'))
    result = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout.strip().splitlines()[-1])
    assert value['defaults'] == value['invalid'] == {'thumbnails':'animated','view':'grid','tileSize':'M','showHidden':False}
    assert value['valid'] == {'thumbnails':'still','view':'list','tileSize':'S','showHidden':True}
    assert [p['kind'] for p in value['problems']] == ['bad','bad','warn','dim','warn','warn']
    assert 'prompt history' in value['problems'][4]['text'] and '0.4.0' in value['problems'][4]['text']
    assert value['visible'] == ['c','a','broken']
    assert value['searched'] == ['b','broken']
    # The exit notice names each running take's own workflow (a session can switch).
    assert value['exitNames'] == ['Alpha and Beta', 'Now', 'Now']
    # A session that ended (or whose takes another Open absorbed) leaves no stale
    # "still generating… Cancel" notice behind.
    assert value['exitNotices'] == [{'generating': 1, 'project': 'P', 'workflow': 'Alpha'},
                                    {'generating': 0, 'project': 'Q', 'workflow': 'the workflow'}, None, None]
    assert f"({value['sample']})" in (ROOT / 'README.md').read_text(encoding='utf-8')
    # ComfyUI's logos resolve at its root however the Launcher was opened (#29).
    assert value['roots'] == ['/', '/', '/comfy/', '/comfy/']
    missing = value['missingCases']
    # Packs by exact class list, then by a pack's name pattern; a class several
    # packs list names them all; a Python-only pattern is skipped, not fatal.
    assert missing['packs'] == [
        {'pack': 'KJNodes for ComfyUI', 'classes': ['LTXVAudioVideoMask', 'VAELoaderKJ']},
        {'pack': 'rgthree-comfy', 'classes': ['Power Lora Loader (rgthree)']},
        {'pack': 'Pack A or Pack B', 'classes': ['Shared']},
        {'pack': '', 'classes': ['Unknown']},
    ]
    assert missing['withManager'] == ('Missing node packs: KJNodes for ComfyUI (VAELoaderKJ), rgthree-comfy '
                                      '(Power Lora Loader (rgthree)). Install them, restart ComfyUI, then reload this page.')
    assert missing['withoutManager'] == ('Missing nodes: VAELoaderKJ. Install the node packs that provide them, '
                                         'restart ComfyUI, then reload this page.')
    assert missing['badge'] == ['Missing nodes', 'bad']
    assert missing['problems'][0] == ['bad', 'Missing node packs: KJNodes for ComfyUI (VAELoaderKJ). Install them, restart ComfyUI, then reload this page.']
    assert missing['models'] == [
        r'a.safetensors → D:\c\models\loras\LTX\IC Lora\a.safetensors (Load LoRA)',
        'a.safetensors → models/loras/LTX/a.safetensors (Load LoRA)',
        'm.pt → folder unknown (Odd · model)',
    ]
    landing = (JS / 'sdk_landing.js').read_text(encoding='utf-8')
    gate = landing.split('async function loadNodePacks()', 1)[1].split('\n}', 1)[0]
    # Fetched once, and only when a listed workflow has missing nodes.
    assert "state.nodePacks !== undefined || !state.templates.some(t => missingNodes(t).length)" in gate
    assert "'/v2/customnode/getmappings?mode=cache'" in gate
