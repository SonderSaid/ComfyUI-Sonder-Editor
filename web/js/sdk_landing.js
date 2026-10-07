// Launcher host owns state, networking and lifecycle. Views own DOM/listeners.
// ComfyUI imports all web modules: boot only on the marked Launcher page.
import { installComfyApiShim } from './comfy_api_shim.js';
import { injectSonderFontFaces, applyNativeControlTheme } from './editor_theme.js';
import { mountToastStack } from './editor_toast_stack.js';
import { configureNotifications, notifyError, notifySuccess } from './editor_notifications.js';
import { getEditorSettings, updateEditorSettings, notificationCoreConfig, subscribeEditorSettings } from './editor_settings.js';
import { createSonderProject } from './project_create.js';
import { SDK_SECRET_HEADER, isUnfinishedRun, readSdkSecret, storeSdkSecret, takeSdkExitFlag } from './sdk_host.js';
import { LAUNCHER_WINDOW_NAME } from './sdk_setup_client.js';
import { register, PRIORITY } from './keyboard_ownership.js';
import { el, button, mountLauncherStyle, date } from './launcher_view.js';
import { mountHeader, environmentState } from './launcher_header.js';
import { mountWorkflows } from './launcher_workflows.js';
import { mountProjects } from './launcher_projects.js';
import { mountOpenBar } from './launcher_open_bar.js';
import { mountDialog, mountPopup } from './launcher_dialog.js';

let api, root, views = [], popup = null, popupAnchor = null, dialog = null, exitPollTimer = null, exitFlag = null;
let active = true, refreshSerial = 0, requirementsSerial = 0;
const state = {
    status: null, templates: [], projects: [], memory: {},
    projectData: { workflow_order: [], unreadable: [], linked_hidden_count: 0 },
    selectedProject: '', selectedTemplateId: '', projectSearch: '', workflowSearch: '', workflowExpanded: false,
    prefs: null, sort: { key: 'modified_at', direction: -1 }, opening: false, openError: null, importProblems: null, exitNotice: null,
};
const json = body => ({ headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
async function call(path, init = {}) {
    const resp = await fetch(api.apiURL(path), init);
    let payload;
    try { payload = await resp.json(); } catch { throw new Error(`ComfyUI returned an unreadable response (${resp.status}).`); }
    if (!resp.ok || payload?.ok === false) {
        const err = new Error(payload.error || `Request failed (${resp.status}).`); err.payload = payload; throw err;
    }
    return payload;
}
function run(action) { return Promise.resolve().then(action).catch(err => notifyError(err.message, { source: 'launcher', detail: err.payload?.problems?.join('\n') })); }
function chosenTemplate() { return state.templates.find(t => t.template_id === state.selectedTemplateId); }
function chosenProject() { return state.projects.find(p => p.folder === state.selectedProject); }
async function checkRequirements() {
    const template = chosenTemplate(), serial = ++requirementsSerial;
    if (!template) return;
    try {
        const result = await call(`/sonder-editor/sdk/templates/${template.template_id}/requirements`);
        if (!active || serial !== requirementsSerial || chosenTemplate() !== template) return;
        template.missing_models = result.missing_models; template.requirements_error = null;
    } catch (err) {
        if (!active || serial !== requirementsSerial || chosenTemplate() !== template) return;
        template.requirements_error = 'Model requirements could not be checked. Check again.';
    }
    render(true);
}
function selectProject(folder, focus = false) {
    const changed = state.selectedProject !== folder;
    state.selectedProject = folder;
    if (changed) { state.selectedTemplateId = chosenProject()?.workflow || ''; void checkRequirements(); }
    state.openError = null; render(true);
    if (focus) Array.from(root.querySelectorAll('[data-project-folder]')).find(n => n.dataset.projectFolder === folder)?.focus();
}
async function refresh() {
    const serial = ++refreshSerial;
    const results = await Promise.allSettled([call('/sonder-editor/sdk/status'), call('/sonder-editor/sdk/templates'), call('/sonder-editor/launcher/projects'), call('/sonder-editor/launcher/state')]);
    if (!active || serial !== refreshSerial) return;
    state.status = results[0].status === 'fulfilled' ? results[0].value : { error: results[0].reason.message };
    if (results[1].status === 'fulfilled') state.templates = results[1].value.templates || [];
    if (results[2].status === 'fulfilled') { state.projectData = results[2].value; state.projects = results[2].value.projects || []; }
    if (results[3].status === 'fulfilled') state.memory = results[3].value;
    for (const result of results.slice(1)) if (result.status === 'rejected') notifyError(result.reason.message, { source: 'launcher-refresh' });
    const allowed = state.projects.filter(p => state.prefs.showHidden || !p.hidden), previous = state.selectedProject;
    if (!allowed.some(p => p.folder === previous) && !state.projectData.unreadable.some(p => p.folder === previous && (state.prefs.showHidden || !p.hidden))) {
        state.selectedProject = [...allowed].sort((a, b) => String(b.modified_at).localeCompare(String(a.modified_at), undefined, { numeric: true }))[0]?.folder || '';
    }
    const hidden = state.memory.hidden?.workflows || [];
    if (previous !== state.selectedProject || !state.templates.some(t => t.template_id === state.selectedTemplateId && (state.prefs.showHidden || !hidden.includes(t.template_id)))) {
        state.selectedTemplateId = chosenProject()?.workflow || state.projectData.workflow_order.find(id => !hidden.includes(id)) || '';
    }
    render();
    void checkRequirements();
}
function preference(patch) {
    updateEditorSettings({ sdk: { launcher: patch } }); state.prefs = getEditorSettings().sdk.launcher;
    if (!state.prefs.showHidden && chosenProject()?.hidden) state.selectedProject = '';
    void run(refresh);
}
function showPopup(anchor, build) {
    const same = popupAnchor === anchor; popup?.(); if (same) return;
    popupAnchor = anchor; popup = mountPopup(anchor, build, () => { popup = null; popupAnchor = null; });
}
function showDialog(options) { popup?.(); dialog?.(); dialog = mountDialog({ ...options, onClose: () => { dialog = null; } }); }
function pickFile(onFile) {
    const input = el('input'); input.type = 'file'; input.accept = '.json,application/json'; input.hidden = true;
    input.addEventListener('cancel', () => input.remove(), { once: true });
    input.addEventListener('change', () => { const file = input.files?.[0]; input.remove(); if (file) void run(() => onFile(file)); }, { once: true });
    document.body.append(input); input.click();
}
async function importFile(file, template = null) {
    try {
        const query = new URLSearchParams({ source_name: file.name, name: file.name.replace(/\.[^.]+$/, '') });
        const payload = await call(`/sonder-editor/sdk/templates${template ? `/${template.template_id}` : ''}?${query}`, {
            method: template ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: await file.arrayBuffer(),
        });
        state.importProblems = null; await refresh(); state.selectedTemplateId = payload.template.template_id; render(); void checkRequirements();
        const reset = [];
        if (payload.template.notes_reset) reset.push(`${payload.template.notes_reset} panel notes reset`);
        if (payload.template.layout_reset) reset.push('layout reset');
        notifySuccess(template ? 'Workflow replaced' : 'Workflow imported', {
            source: 'launcher-import', detail: template ? `Control values kept.${reset.length ? ` ${reset.join('; ')}.` : ''}` : undefined,
        });
    } catch (err) { state.importProblems = err.payload?.validation?.errors || [err.message]; render(); }
}
async function setHidden(kind, id, hidden) {
    await call('/sonder-editor/launcher/hidden', { method: 'PUT', ...json({ kind, id, hidden }) });
    if (hidden && !state.prefs.showHidden) {
        if (kind === 'workflow' && state.selectedTemplateId === id) state.selectedTemplateId = '';
        if (kind === 'project' && state.selectedProject === id) state.selectedProject = '';
    }
    await refresh();
}
function workflowMenu(template, anchor) {
    showPopup(anchor, (menu, close) => {
        if (!template.starter) {
            menu.append(button('Rename', () => showDialog({ title: 'Rename workflow', initial: template.name, submit: async name => {
                await call(`/sonder-editor/sdk/templates/${template.template_id}`, { method: 'PATCH', ...json({ name }) }); await refresh();
            } })));
            menu.append(button('Replace…', () => { close(); pickFile(file => importFile(file, template)); }));
        }
        menu.append(el('div', 'sl-dim', `${template.source_name || ''}${template.imported_at ? ` · ${date(template.imported_at)}` : ''}`));
        const hidden = state.memory.hidden?.workflows?.includes(template.template_id);
        menu.append(button(hidden ? 'Unhide' : 'Hide', () => { close(); void run(() => setHidden('workflow', template.template_id, !hidden)); }));
        if (!template.starter) {
            let confirming = false;
            const del = button('Delete', () => {
                if (!confirming) { confirming = true; del.textContent = 'Delete? Click again'; return; }
                close(); void run(async () => { await call(`/sonder-editor/sdk/templates/${template.template_id}`, { method: 'DELETE' }); await refresh(); });
            }); menu.append(del);
        }
    });
}
function projectMenu(project, anchor) {
    showPopup(anchor, (menu, close) => {
        menu.append(button('Open', () => { close(); selectProject(project.folder); void openEditor(); }, { disabled: project.too_new || project.unreadable }));
        menu.append(button('Open with…', () => {
            menu.replaceChildren(el('div', 'sl-dim', 'Choose a workflow'));
            for (const id of state.projectData.workflow_order) {
                const template = state.templates.find(t => t.template_id === id);
                if (!template || (!state.prefs.showHidden && state.memory.hidden?.workflows?.includes(id))) continue;
                menu.append(button(template.name, () => { close(); state.selectedProject = project.folder; state.selectedTemplateId = id; void openEditor(); }));
            }
        }, { disabled: project.too_new || project.unreadable }));
        menu.append(button('Show in folder', () => { close(); void run(() => call(`/sonder-editor/project/${encodeURIComponent(project.folder)}/reveal`, { method: 'POST' })); }, { disabled: !state.projectData.can_manage, title: state.projectData.can_manage ? 'Show in folder' : 'Change this on the ComfyUI machine.' }));
        menu.append(button(project.hidden ? 'Unhide' : 'Hide', () => { close(); void run(() => setHidden('project', project.folder, !project.hidden)); }));
        if (project.linked) menu.append(button('Unlink…', () => showDialog({ title: 'Unlink project', help: `Remove the link to “${project.name}”? The project’s files stay in place.`, accept: 'Unlink', submit: async () => {
            await call('/sonder-editor/projects/unlink', { method: 'POST', ...json({ project_id: project.folder }) }); await refresh();
        } })));
    });
}
function createProject() {
    const workflow = chosenTemplate(), settings = getEditorSettings(), defaults = workflow?.validation?.editor_defaults || {};
    const size = Object.fromEntries(['fps', 'width', 'height'].map(k => [k, Number(defaults[k]) > 0 ? Number(defaults[k]) : settings.projectDefaults[k]]));
    showDialog({ title: 'New project', initial: '', label: 'Project name', accept: 'Create and open', help: `${size.width}×${size.height} at ${size.fps} fps. ${workflow ? `Opens with ${workflow.name}.` : 'Choose a workflow to open the editor after creation.'}`, submit: async name => {
        const { folder } = await createSonderProject({ name, ...size, settings, apiURL: path => api.apiURL(path) });
        await refresh(); state.selectedProject = folder; state.selectedTemplateId = workflow?.template_id || state.selectedTemplateId;
        if (state.selectedTemplateId) await openEditor(); else render();
    } });
}
async function openEditor() {
    const project = chosenProject(), template = chosenTemplate();
    if (state.opening || !project || !template || project.too_new) return;
    const pair = { template_id: template.template_id, project: project.folder };
    state.opening = true; state.openError = null; render();
    try {
        const payload = await call('/sonder-editor/sdk/session/open', { method: 'POST', ...json(pair) });
        if (!payload.session_id || !payload.tab_url) throw new Error('The server did not return an editor session.');
        if (!storeSdkSecret(payload.session_id, payload.secret)) throw new Error('This browser blocked session storage for the page.');
        window.location.assign(api.apiURL(payload.tab_url));
    } catch (err) { state.opening = false; state.openError = { error: err.message, detail: err.payload }; render(); }
}
function statusMenu(anchor) {
    showPopup(anchor, (menu, close) => {
        const address = state.status?.proxy?.upstream || window.location.origin;
        menu.append(el('div', '', address), button('Copy address', () => void run(() => navigator.clipboard.writeText(address))), el('div', 'sl-dim', 'Bookmark this Launcher for next time.'));
        menu.append(el('div', '', state.status?.proxy?.running ? 'Generation connection: running' : 'Generation connection starts when needed.'));
        for (const [label, info] of [['comfy-sdk', state.status?.install?.sdk], ['comfy-api-proxy', state.status?.install?.proxy]]) menu.append(el('div', 'sl-dim', `${label}: ${info?.installed ? info.version : 'not installed'}`));
        menu.append(button('Check again', () => { close(); void run(refresh); }));
    });
}
function settingsMenu(anchor) {
    showPopup(anchor, menu => {
        for (const [label, key, options] of [['Thumbnails', 'thumbnails', ['off', 'still', 'animated']], ['View', 'view', ['grid', 'list']]]) {
            const row = el('label', '', label), select = el('select'); select.setAttribute('aria-label', label);
            for (const value of options) { const o = el('option', '', value[0].toUpperCase() + value.slice(1)); o.value = value; select.append(o); }
            select.value = state.prefs[key]; select.addEventListener('change', () => preference({ [key]: select.value })); row.append(select); menu.append(row);
        }
        const row = el('label', '', 'Show hidden'), check = el('input'); check.type = 'checkbox'; check.checked = state.prefs.showHidden;
        check.addEventListener('change', () => preference({ showHidden: check.checked })); row.append(check); menu.append(row);
    });
}
function exitNotice() {
    const notice = state.exitNotice, node = el('div'); node.dataset.sonderExitNotice = 'true'; if (!notice) return node;
    node.className = 'sl-notice';
    node.append(el('span', '', notice.generating ? `${notice.generating} ${notice.generating === 1 ? 'take is' : 'takes are'} still generating in ${notice.project} with ${notice.workflow}.` : `The takes have finished in ${notice.project}.`));
    if (notice.generating) {
        let confirm = false;
        const cancel = button('Cancel', () => {
            if (!confirm) { confirm = true; cancel.textContent = 'Cancel takes? Click again'; return; }
            void run(async () => { await call(`/sonder-editor/sdk/session/${encodeURIComponent(exitFlag.session_id)}/cancel`, { method: 'POST', headers: { [SDK_SECRET_HEADER]: readSdkSecret(exitFlag.session_id) } }); await pollExitSession(); });
        }); node.append(cancel);
    }
    return node;
}
async function pollExitSession() {
    clearTimeout(exitPollTimer); if (!active || !exitFlag || !readSdkSecret(exitFlag.session_id)) return;
    try {
        const payload = await call(`/sonder-editor/sdk/session/${encodeURIComponent(exitFlag.session_id)}/status`, { headers: { [SDK_SECRET_HEADER]: readSdkSecret(exitFlag.session_id) } });
        if (!active) return;
        const generating = (payload.runs || []).filter(isUnfinishedRun).length;
        const next = generating || state.exitNotice ? { generating, project: payload.project || exitFlag.project, workflow: payload.template_name || chosenTemplate()?.name || 'the workflow' } : null;
        // The first Cancel click must survive an unchanged polling result.
        if (JSON.stringify(next) !== JSON.stringify(state.exitNotice)) { state.exitNotice = next; root.querySelector('[data-sonder-exit-notice]')?.replaceWith(exitNotice()); }
        if (generating) exitPollTimer = setTimeout(() => void pollExitSession(), 2000);
    } catch (err) { if (active && err.payload?.code !== 'session_ended') exitPollTimer = setTimeout(() => void pollExitSession(), 5000); }
}
const actions = {
    statusMenu, settingsMenu, preference, workflowMenu, projectMenu, create: createProject,
    import: () => pickFile(importFile), importFile: file => void run(() => importFile(file)),
    searchProjects: value => { state.projectSearch = value; render(); }, searchWorkflows: value => { state.workflowSearch = value; render(); },
    toggleWorkflows: () => { state.workflowExpanded = !state.workflowExpanded; render(); },
    selectWorkflow: id => { state.selectedTemplateId = id; state.openError = null; render(); void checkRequirements(); }, selectProject,
    openProject: folder => { selectProject(folder); void openEditor(); }, open: () => void openEditor(),
    openDetails: () => showDialog({ title: 'Couldn’t open the editor', help: [state.openError?.error, ...(state.openError?.detail?.problems || []), ...(state.openError?.detail?.validation?.errors || [])].filter(Boolean).join('\n'), accept: 'Close', submit: async () => {} }),
    sort: key => { state.sort = { key, direction: state.sort.key === key ? -state.sort.direction : 1 }; render(); },
    enableLinks: () => showDialog({ title: 'Allow external project links', help: 'Linked projects let this ComfyUI read and write project folders outside its projects directory. Enable this only for folders you trust.', accept: 'Turn on', submit: async () => { await call('/sonder-editor/server-settings', { method: 'PUT', ...json({ allow_external_project_links: true }) }); await refresh(); } }),
};
function render(preserveProjects = false) {
    if (!root || !active) return;
    const offsets = Array.from(root.querySelectorAll('[data-scroll-key]')).map(n => [n.dataset.scrollKey, n.scrollTop]);
    const focus = document.activeElement, key = focus?.dataset?.focusKey, start = focus?.selectionStart, end = focus?.selectionEnd;
    const retained = preserveProjects ? views[2] : null;
    popup?.(); for (const view of views) if (view !== retained) view.cleanup(); views = [];
    const main = el('main', 'sl-main');
    const header = mountHeader(state, actions), workflows = mountWorkflows(state, actions), projects = retained || mountProjects(state, actions), bar = mountOpenBar(state, actions);
    if (retained) for (const node of retained.element.querySelectorAll('[data-project-folder]')) {
        const selected = node.dataset.projectFolder === state.selectedProject;
        node.classList.toggle('selected', selected); node.setAttribute('aria-selected', String(selected));
    }
    views.push(header, workflows, projects, bar); main.append(workflows.element, projects.element);
    const env = environmentState(state.status), nodes = [header.element];
    if (env.message) nodes.push(el('div', `sl-notice sl-${env.kind}`, env.message));
    nodes.push(exitNotice(), main, bar.element); root.replaceChildren(...nodes);
    for (const [id, top] of offsets) Array.from(root.querySelectorAll('[data-scroll-key]')).find(n => n.dataset.scrollKey === id)?.scrollTo(0, top);
    if (key) { const target = Array.from(root.querySelectorAll('[data-focus-key]')).find(n => n.dataset.focusKey === key); target?.focus({ preventScroll: true }); if (typeof start === 'number') target?.setSelectionRange?.(start, end); }
}
export function isLauncherPage(doc = globalThis.document) { return doc?.body?.dataset?.sonderLauncher === 'true'; }
function boot() {
    window.name = LAUNCHER_WINDOW_NAME; installComfyApiShim(); injectSonderFontFaces(); applyNativeControlTheme(document.documentElement);
    api = window.comfyAPI.api.api; root = document.getElementById('app'); root.className = 'sl';
    const removeStyle = mountLauncherStyle(); state.prefs = getEditorSettings().sdk.launcher;
    configureNotifications(notificationCoreConfig()); mountToastStack(document.body);
    const unsubscribe = subscribeEditorSettings(() => { configureNotifications(notificationCoreConfig()); state.prefs = getEditorSettings().sdk.launcher; });
    const offKeys = register({ id: 'launcher', priority: PRIORITY.EDITOR, keydown(e) {
        if (e.isComposing || e.keyCode === 229 || e.ctrlKey || e.metaKey || e.altKey || dialog || popup) return false;
        if (e.target?.closest?.('input,textarea,select,button,[contenteditable="true"],[data-workflow-id],.sl-new')) return false;
        if (e.key === 'Enter') { void openEditor(); return true; } return false;
    } });
    exitFlag = takeSdkExitFlag(); if (exitFlag) { state.selectedProject = exitFlag.project || ''; state.selectedTemplateId = exitFlag.template_id || ''; }
    window.addEventListener('pageshow', event => { if (event.persisted) window.location.reload(); });
    window.addEventListener('pagehide', () => { active = false; clearTimeout(exitPollTimer); popup?.(); dialog?.(); offKeys(); unsubscribe(); removeStyle(); for (const view of views) view.cleanup(); }, { once: true });
    document.body.classList.add('ready'); render(); void run(async () => { await refresh(); if (exitFlag) void pollExitSession(); });
}
if (isLauncherPage()) boot();
