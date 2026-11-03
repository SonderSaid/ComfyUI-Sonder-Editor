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
    // Once the row's check answered: what the current Modes need is bad and names the
    // choice; what only other Modes use is a dim note.
    checked: modules.launcher_open_bar.pairProblems({}, {validation: {ok: true, errors: [], missing_nodes: ['VAELoaderKJ', 'Other']},
        run_missing_nodes: [{class_type: 'VAELoaderKJ', needed_by: ['Task → Edit With Words']}], run_problems: ['routing']},
        {}, {}, null).map(p => [p.kind, p.text]),
    checkedBadge: lw.workflowBadge({validation: {missing_nodes: ['Other']}, run_missing_nodes: [], missing_models: []}),
    neededBadge: lw.workflowBadge({validation: {missing_nodes: ['X']}, run_missing_nodes: [{class_type: 'X'}]}),
    neededModel: modules.launcher_view.modelMessage({file: 'a.safetensors', relative: 'models/loras/a.safetensors', node: 'L', needed_by: ['Passes → Upscale Pass Only']}),
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
    # Before the row's check answers, missing nodes warn: the Modes may not use them.
    assert missing['badge'] == ['Missing nodes', 'warn']
    assert missing['problems'][0] == ['warn', 'Missing node packs: KJNodes for ComfyUI (VAELoaderKJ). Install them, restart ComfyUI, then reload this page.']
    assert missing['checked'] == [
        ['bad', 'Missing nodes: VAELoaderKJ. Install the node packs that provide them, restart ComfyUI, then reload this page.'
                ' Needed by Task → Edit With Words.'],
        ['dim', 'Some Modes of this workflow use nodes that aren’t installed: Other. The current Modes don’t need them.'],
        ['bad', 'routing']]
    assert missing['checkedBadge'] == ['Missing nodes', 'warn'] and missing['neededBadge'] == ['Missing nodes', 'bad']
    assert missing['neededModel'] == 'a.safetensors → models/loras/a.safetensors (L) Needed by Passes → Upscale Pass Only.'
    # Missing nodes never disable Open: the workflow's Modes may not use them.
    bar = (JS / 'launcher_open_bar.js').read_text(encoding='utf-8')
    disabled = bar.split('disabled:', 1)[1].splitlines()[0]
    assert 'missingNodes' not in disabled and 'too_new' in disabled
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


def _run_module_script(body):
    script = r'''
globalThis.window = { addEventListener() {}, localStorage: {getItem() {return null;}} };
globalThis.localStorage = window.localStorage;
globalThis.document = { body: {dataset: {}}, createElement() {throw Error('DOM touched on import');}, querySelector() {return null;}, addEventListener() {} };
const base = __BASE__;
const m = {};
for (const name of ['launcher_view', 'launcher_header', 'launcher_workflows', 'launcher_projects', 'launcher_open_bar', 'sdk_landing']) m[name] = await import(base + name + '.js');
'''.replace('__BASE__', json.dumps(JS.as_uri() + '/')) + body
    result = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_launcher_status_precedence_and_row_badge():
    """A missing package reads Generate unavailable even after a failed Generate set
    the proxy's error; no answer yet reads Checking, never Unreachable (cold start)."""
    if not shutil.which('node'):
        pytest.skip('Node unavailable')
    value = _run_module_script(r'''
const env = m.launcher_header.environmentState, pkg = (installed, supported) => ({installed, supported, version: installed ? '0.1.0' : ''});
const missing = {install: {sdk: pkg(true, true), proxy: pkg(false, false), ready: false}};
const ready = {install: {sdk: pkg(true, true), proxy: pkg(true, true), ready: true}};
const unsupported = {install: {sdk: pkg(true, false), proxy: pkg(true, true), ready: false}};
const proxyError = {error: 'The comfy-api-proxy package is not installed.'};
const states = {
    pending: env(null), slow: env({slow: true}),
    network: env({error: 'ComfyUI didn’t answer.', network: true}), server: env({error: 'Something went wrong.'}),
    missingAfterGenerate: env({...missing, proxy: proxyError}), unsupportedWithError: env({...unsupported, proxy: proxyError}),
    readyWithError: env({...ready, proxy: {error: 'Port taken.'}}), ready: env(ready),
};
const badge = m.launcher_workflows.workflowBadge, checked = {validation: {}, missing_models: []};
const badges = {
    notReady: badge(checked, missing), unsupported: badge(checked, unsupported), ready: badge(checked, ready),
    unknown: badge(checked, null), unreachable: badge(checked, {error: 'x'}),
    missingNodesWin: badge({validation: {missing_nodes: ['X']}}, missing), modelsWin: badge({validation: {}, missing_models: [{}]}, missing),
    unchecked: badge({validation: {}}, missing),
};
const problems = m.launcher_open_bar.pairProblems({}, checked, missing).map(p => p.text);
const nested = env({error: 'ComfyUI returned an unreadable response (502).'}).message;
console.log(JSON.stringify({states, badges, problems, nested, needed: m.launcher_header.PACKAGES_NEEDED, short: m.launcher_open_bar.PACKAGES_MISSING}));
''')
    s = value['states']
    assert [s['pending']['label'], s['pending']['kind']] == ['Checking…', '']
    assert [s['slow']['label'], s['slow']['kind']] == ['Checking…', 'warn'] and 'Check again' in s['slow']['message']
    assert s['network']['label'] == 'Unreachable' and 'didn’t answer' in s['network']['message'] and 'Check again' in s['network']['message']
    assert s['server']['label'] == 'Unreachable' and 'status: Something went wrong. Click ComfyUI' in s['server']['message']
    # The server's own sentence is quoted, not nested in parentheses.
    assert value['nested'].startswith('ComfyUI couldn’t report its status: ComfyUI returned an unreadable response (502). Click')
    assert [s['missingAfterGenerate']['label'], s['missingAfterGenerate']['kind']] == ['Generate unavailable', 'warn']
    assert s['missingAfterGenerate']['message'] == value['needed']
    # The install sentence names the route, as the docs give it.
    route = 'click ⚙ at the right end of the toolbar above the timeline, then go to Settings ▸ Sonder App (experimental)'
    assert route in value['needed'] and route in s['unsupportedWithError']['message']
    assert s['unsupportedWithError']['label'] == 'Unsupported'
    assert s['readyWithError']['label'] == 'Connection problem'
    assert s['ready'] == {'label': 'Ready', 'kind': 'good', 'message': ''}
    b = value['badges']
    assert b['notReady'] == b['unsupported'] == ['Generate unavailable', 'warn']
    assert b['ready'] == b['unknown'] == b['unreachable'] == ['Ready', 'good']
    assert b['missingNodesWin'] == ['Missing nodes', 'warn'] and b['modelsWin'] == ['1 models missing', 'warn']
    assert b['unchecked'] == ['Select to check models', '']
    # The notice says how to install; the Open bar points back to it.
    assert value['problems'] == [value['short']] and 'see the note at the top' in value['short']
    sources = ''.join((JS / f'{name}.js').read_text(encoding='utf-8') for name in ('launcher_header', 'launcher_open_bar', 'launcher_workflows', 'sdk_landing'))
    assert sources.count('comfy-sdk and comfy-api-proxy packages') == 1


def test_launcher_refresh_loading_failure_and_selection():
    """Until a list has loaded its pane says so; a failed first load says couldn't load
    and keeps the selection the page came back with; later failures keep the data."""
    if not shutil.which('node'):
        pytest.skip('Node unavailable')
    value = _run_module_script(r'''
const apply = m.sdk_landing.applyRefreshResults;
const fresh = extra => ({templates: [], projects: [], memory: {}, thumbnails: {}, projectData: {workflow_order: [], unreadable: []},
    loaded: {templates: false, projects: false}, loadFailed: {templates: false, projects: false},
    prefs: {showHidden: false}, selectedProject: '', selectedTemplateId: '', projectSearch: '', ...extra});
const ok = value => ({status: 'fulfilled', value}), fail = message => ({status: 'rejected', reason: Object.assign(new Error(message), {network: true, detail: 'Failed to fetch'})});
const templates = ok({templates: [{template_id: 't1', name: 'One'}, {template_id: 't2', name: 'Two'}]});
const projects = ok({projects: [{folder: 'old', modified_at: '2026-01-01', workflow: 't1'}, {folder: 'back', modified_at: '2025-01-01', workflow: 't1'}],
    workflow_order: ['t1', 't2'], unreadable: []});
const memory = ok({hidden: {workflows: []}});
const texts = st => ({projects: m.launcher_projects.projectsNotice(st), workflows: m.launcher_workflows.workflowsNotice(st, st.projectData.workflow_order.length, 0),
    summary: m.launcher_open_bar.openSummary(st, null, null), list: m.launcher_view.listState(st, 'templates', 'projects')});
const out = {};
const pending = fresh(); out.pending = texts(pending);
// Templates in, projects failed: the workflows pane must not show its import hint.
const half = fresh({selectedProject: 'back', selectedTemplateId: 't2'});
out.halfFailures = apply(half, [templates, fail('ComfyUI didn’t answer.'), memory]).map(e => [e.message, e.detail]);
out.half = {...texts(half), selected: [half.selectedProject, half.selectedTemplateId]};
// Check again recovers: the selection the page came back with survives.
apply(half, [templates, projects, memory]);
out.recovered = {...texts(half), selected: [half.selectedProject, half.selectedTemplateId], failed: half.loadFailed};
// Loaded, then a later refresh fails: the data stays and nothing reads as loading.
apply(half, [fail('x'), fail('y'), fail('z')]);
out.later = {...texts(half), projects: half.projects.length, templates: half.templates.length, failed: half.loadFailed};
// A first load that succeeds with nothing in it shows the real empty states.
const empty = fresh(); apply(empty, [ok({templates: []}), ok({projects: [], workflow_order: [], unreadable: []}), memory]); out.empty = texts(empty);
// With no selection, the newest project is preselected once both lists are in.
const pick = fresh(); apply(pick, [templates, projects, memory]); out.pick = [pick.selectedProject, pick.selectedTemplateId];
// A first load past LISTS_SLOW_MS says so; only the list that failed is named.
out.slow = texts(fresh({listsSlow: true}));
const templatesOnly = fresh(); apply(templatesOnly, [fail('a'), projects, memory]); out.templatesOnly = openSummaryOf(templatesOnly);
function openSummaryOf(st) { return m.launcher_open_bar.openSummary(st, null, null); }
const status = m.sdk_landing.statusFromResult;
out.status = [status(ok({ok: true})), status(fail('ComfyUI didn’t answer.')), status({status: 'rejected', reason: new Error('Something went wrong.')})];
console.log(JSON.stringify(out));
''')
    assert value['pending'] == {'projects': {'text': 'Loading projects…', 'failed': False},
                                'workflows': {'text': 'Loading workflows…', 'failed': False},
                                'summary': 'Loading projects and workflows…', 'list': 'loading'}
    assert value['halfFailures'] == [['ComfyUI didn’t answer.', 'Failed to fetch']]
    half = value['half']
    assert half['projects'] == {'text': 'Projects couldn’t load.', 'failed': True}
    assert half['workflows'] == {'text': 'Workflows couldn’t load.', 'failed': True}
    assert half['summary'].startswith('Projects couldn’t load.') and 'Check again' in half['summary']
    assert value['templatesOnly'].startswith('Workflows couldn’t load.')
    assert value['slow']['projects']['text'].startswith('Still loading projects.') and value['slow']['list'] == 'slow'
    assert value['slow']['workflows']['text'] == 'Still loading workflows…'
    assert value['slow']['summary'] == 'Still loading projects and workflows…'
    assert half['selected'] == ['back', 't2']
    rec = value['recovered']
    assert rec['projects'] is None and rec['list'] == 'ready' and rec['summary'] == 'Choose a project and a workflow.'
    assert rec['selected'] == ['back', 't2'] and rec['failed'] == {'templates': False, 'projects': False}
    later = value['later']
    assert later['projects'] == 2 and later['templates'] == 2 and later['list'] == 'ready'
    assert later['failed'] == {'templates': False, 'projects': False}
    assert value['empty']['projects'] == {'text': 'Start your first project', 'failed': False}
    assert value['empty']['workflows']['text'].startswith('Export an API workflow')
    assert value['pick'] == ['old', 't1']
    assert value['status'] == [{'ok': True}, {'error': 'ComfyUI didn’t answer.', 'network': True},
                               {'error': 'Something went wrong.', 'network': False}]
    # The panes and the bar draw their empty lines through these helpers, never by
    # testing the arrays themselves (an unloaded list is empty too).
    projects_js = (JS / 'launcher_projects.js').read_text(encoding='utf-8')
    workflows_js = (JS / 'launcher_workflows.js').read_text(encoding='utf-8')
    bar_js = (JS / 'launcher_open_bar.js').read_text(encoding='utf-8')
    assert 'projectsNotice(state)' in projects_js.split('export function mountProjects', 1)[1]
    assert "!state.projects.length && !state.projectData.unreadable.length) scroll.append" not in projects_js
    assert 'workflowsNotice(state, ordered.length, list.children.length)' in workflows_js
    assert 'openSummary(state, project, template)' in bar_js.split('export function mountOpenBar', 1)[1]
    # "No projects match" only once the list has loaded.
    assert "state.projectSearch && !rows.length && listState(state, 'projects') === 'ready'" in projects_js
    landing = (JS / 'sdk_landing.js').read_text(encoding='utf-8')
    lists = landing.split('async function refreshLists()', 1)[1].split('\n}', 1)[0]
    # The lists go through the tested reducer; the status through the shared checker,
    # never awaited by the lists (callers act on the lists only).
    assert 'applyRefreshResults(state, lists)' in lists and 'void statusCheck()' in lists
    assert 'LIST_TIMEOUT_MS' in lists and 'LISTS_SLOW_MS' in lists and 'await status' not in lists
    boot = landing.split('function boot()', 1)[1]
    assert "request: () => call('/sonder-editor/sdk/status', {}, STATUS_TIMEOUT_MS), slowMs: STATUS_SLOW_MS" in boot
    # Every Check again (the panes and the ComfyUI menu) goes through checkAgain.
    assert 'checkAgain: () => void run(checkAgain)' in landing
    assert "button('Check again', () => { close(); void run(checkAgain); })" in landing
    assert landing.count('void run(refresh)') == 0


def test_launcher_status_checker_shares_one_call():
    """Check again on a slow ComfyUI joins the status call in flight instead of
    dropping it; the slow state fires once, only while the page is still waiting."""
    if not shutil.which('node'):
        pytest.skip('Node unavailable')
    value = _run_module_script(r'''
const timers = {pending: new Map(), next: 1, setTimeout(fn) { const id = this.next++; this.pending.set(id, fn); return id; }, clearTimeout(id) { this.pending.delete(id); },
    fire() { const fns = [...this.pending.values()]; this.pending.clear(); fns.forEach(fn => fn()); }};
const events = [], resolvers = [];
let waiting = true;
const check = m.sdk_landing.statusChecker({timers, slowMs: 15000, waiting: () => waiting,
    request: () => new Promise((resolve, reject) => resolvers.push({resolve, reject})),
    slow: () => events.push('slow'), apply: status => events.push(['apply', status])});
const a = check(), b = check();
const out = {sameCall: a === b, requests: resolvers.length, timers: timers.pending.size};
timers.fire(); out.afterSlow = [...events];
resolvers[0].resolve({ok: true}); await a;
out.afterAnswer = [...events]; out.timersAfter = timers.pending.size;
// A later check is a new call; with an answer on screen it sets no slow timer.
waiting = false; const c = check(); out.newCall = c !== a && resolvers.length === 2; out.noTimer = timers.pending.size === 0;
resolvers[1].reject(Object.assign(new Error('ComfyUI didn’t answer.'), {network: true})); await c;
out.last = events.at(-1);
// An answer before the slow mark cancels it.
waiting = true; const d = check(); resolvers[2].resolve({ok: 1}); await d; timers.fire(); out.noLateSlow = events.filter(e => e === 'slow').length === 1;
console.log(JSON.stringify(out));
''')
    assert value['sameCall'] is True and value['requests'] == 1 and value['timers'] == 1
    assert value['afterSlow'] == ['slow']
    assert value['afterAnswer'] == ['slow', ['apply', {'ok': True}]] and value['timersAfter'] == 0
    assert value['newCall'] is True and value['noTimer'] is True
    assert value['last'] == ['apply', {'error': 'ComfyUI didn’t answer.', 'network': True}]
    assert value['noLateSlow'] is True
