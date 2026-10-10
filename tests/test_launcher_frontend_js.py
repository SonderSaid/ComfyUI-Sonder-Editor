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
console.log(JSON.stringify({defaults,invalid,valid,problems,visible,searched,exitNames,sample:modules.launcher_workflows.SAMPLE_PROJECTS_URL}));
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
    assert f"({value['sample']})" in (ROOT / 'README.md').read_text(encoding='utf-8')
