// Launcher host owns state, networking and lifecycle. Views own DOM/listeners.
// ComfyUI imports all web modules: boot only on the marked Launcher page.
import { installComfyApiShim } from './comfy_api_shim.js';
import { injectSonderFontFaces, applyNativeControlTheme } from './editor_theme.js';
import { mountToastStack } from './editor_toast_stack.js';
import { configureNotifications, notifyError, notifySuccess } from './editor_notifications.js';
import { getEditorSettings, updateEditorSettings, notificationCoreConfig, subscribeEditorSettings } from './editor_settings.js';
import { createSonderProject } from './project_create.js';
import { SDK_SECRET_HEADER, readSdkSecret, storeSdkSecret, takeSdkExitFlag } from './sdk_host.js';
import { LAUNCHER_WINDOW_NAME, MACHINE_ONLY_MESSAGE } from './sdk_setup_client.js';
import { register, PRIORITY } from './keyboard_ownership.js';
import { el, button, mountLauncherStyle, date } from './launcher_view.js';
import { mountHeader, environmentState } from './launcher_header.js';
import { downloadCuratedWorkflow } from './launcher_export.js';
import { missingNodes, mountWorkflows, nextExitNotice } from './launcher_workflows.js';
import { mountProjects, thumbnailMode } from './launcher_projects.js';
import { mountOpenBar } from './launcher_open_bar.js';
import { NODE_PACKS_PATH, fetchNodePackMappings } from './node_pack_mappings.js';
import { mountDialog, mountPopup, mountProjectDialog } from './launcher_dialog.js';
import { mountProjectFormatControls } from './project_format_controls.js';

let api, root, views = [], popup = null, popupAnchor = null, dialog = null, exitPollTimer = null, exitFlag = null;
let active = true, refreshSerial = 0, requirementsSerial = 0, thumbTimer = null, thumbPollTimer = null, thumbWanted = [];
let statusCheck = null, refreshing = null, listsSlowTimer = null;
const thumbRequested = new Map(); // folder -> the key already asked for
// A cold ComfyUI answers in seconds, a starved one may not. A first answer later than
// the *_SLOW_MS reads as slow; a call past its timeout fails. The lists get longer:
// a large projects folder on a slow disk is slow, not broken.
const STATUS_SLOW_MS = 15000, STATUS_TIMEOUT_MS = 60000, LISTS_SLOW_MS = 15000, LIST_TIMEOUT_MS = 180000;
const state = {
    // status: null until the first answer (see environmentState).
    status: null, templates: [], projects: [], memory: {}, thumbnails: {},
    // Whether each list has ever loaded, and whether its first load failed: until it
    // loads, the panes say loading or couldn't load, never their empty states.
    // listsSlow: the first load is taking longer than LISTS_SLOW_MS.
    loaded: { templates: false, projects: false }, loadFailed: { templates: false, projects: false }, listsSlow: false,
    projectData: { workflow_order: [], unreadable: [], linked_hidden_count: 0 },
    selectedProject: '', selectedTemplateId: '', projectSearch: '', workflowSearch: '', workflowExpanded: false,
    prefs: null, sort: { key: 'modified_at', direction: -1 }, opening: false, openError: null, importProblems: null, exitNotice: null,
    // The Manager's node → pack mappings: undefined until a workflow with missing
    // nodes is listed, then the payload, or null without the Manager.
    nodePacks: undefined,
};
const json = body => ({ headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
// A call that never reached ComfyUI, or got no answer in time, fails with a written
// sentence and `network`; the browser's own words go to `detail`.
async function call(path, init = {}, timeoutMs = 0) {
    const controller = timeoutMs ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
    let resp;
    try { resp = await fetch(api.apiURL(path), controller ? { ...init, signal: controller.signal } : init); }
    catch (cause) {
        const err = new Error(controller?.signal.aborted ? 'ComfyUI didn’t answer in time.' : 'ComfyUI didn’t answer.');
        err.network = true; err.detail = cause?.message || ''; throw err;
    } finally { clearTimeout(timer); }
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
        template.run_missing_nodes = result.missing_nodes || []; template.run_problems = result.problems || [];
    } catch (err) {
        if (!active || serial !== requirementsSerial || chosenTemplate() !== template) return;
        template.requirements_error = 'Model requirements could not be checked. Check again.';
    }
    render(true);
}
// Fetched once per page, and only when a listed workflow has missing nodes: the
// Manager's mappings are large. Same origin; without the Manager, class names show.
async function loadNodePacks() {
    if (state.nodePacks !== undefined || !state.templates.some(t => missingNodes(t).length)) return;
    state.nodePacks = null;
    state.nodePacks = await fetchNodePackMappings(api.apiURL(NODE_PACKS_PATH));
    if (active && state.nodePacks) render(true);
}
function selectProject(folder, focus = false) {
    const changed = state.selectedProject !== folder;
    state.selectedProject = folder;
    if (changed) { state.selectedTemplateId = chosenProject()?.workflow || ''; void checkRequirements(); }
    state.openError = null; render(true);
    if (focus) Array.from(root.querySelectorAll('[data-project-folder]')).find(n => n.dataset.projectFolder === folder)?.focus();
}
// The status as environmentState reads it, from one settled status call.
export function statusFromResult(result) {
    return result.status === 'fulfilled' ? result.value : { error: result.reason.message, network: !!result.reason.network };
}
// The lists' share of a refresh, applied to `state`: data, load flags and the
// preselection. `results` are the settled templates, projects and Launcher-memory
// calls. A failure keeps the data already shown; one before a list ever loaded
// marks it failed. Returns the failures, for the caller to report.
export function applyRefreshResults(state, [templates, projects, memory]) {
    for (const [key, result] of [['templates', templates], ['projects', projects]]) {
        if (result.status === 'fulfilled') { state.loaded[key] = true; state.loadFailed[key] = false; }
        else if (!state.loaded[key]) state.loadFailed[key] = true;
    }
    if (templates.status === 'fulfilled') state.templates = templates.value.templates || [];
    if (projects.status === 'fulfilled') {
        state.projectData = projects.value; state.projects = projects.value.projects || [];
        state.thumbnails = Object.fromEntries(state.projects.filter(p => p.thumbnail).map(p => [p.folder, p.thumbnail]));
    }
    if (memory.status === 'fulfilled') state.memory = memory.value;
    // Only once both lists are in: a failed first load must not replace the
    // selection the page came back from the editor with.
    if (state.loaded.templates && state.loaded.projects) preselect(state);
    return [templates, projects, memory].filter(r => r.status === 'rejected').map(r => r.reason);
}
function preselect(state) {
    const project = () => state.projects.find(p => p.folder === state.selectedProject);
    const allowed = state.projects.filter(p => state.prefs.showHidden || !p.hidden), previous = state.selectedProject;
    if (!allowed.some(p => p.folder === previous) && !state.projectData.unreadable.some(p => p.folder === previous && (state.prefs.showHidden || !p.hidden))) {
        state.selectedProject = [...allowed].sort((a, b) => String(b.modified_at).localeCompare(String(a.modified_at), undefined, { numeric: true }))[0]?.folder || '';
    }
    const hidden = state.memory.hidden?.workflows || [];
    if (previous !== state.selectedProject || !state.templates.some(t => t.template_id === state.selectedTemplateId && (state.prefs.showHidden || !hidden.includes(t.template_id)))) {
        // As the server's preferred_workflow. A missing node doesn't count against
        // a workflow: its current Modes may not use it, and its row says so if they do.
        const shown = state.projectData.workflow_order.filter(id => !hidden.includes(id));
        state.selectedTemplateId = project()?.workflow || shown[0] || '';
    }
}
// One status call at a time. Overlapping refreshes share the one in flight, so a
// Check again on a slow ComfyUI never throws away the answer already on its way.
// `slow()` fires once when an answer the page is still waiting for (`waiting()`)
// is later than `slowMs`; `apply(status)` takes every answer.
export function statusChecker({ request, apply, slow, waiting, slowMs, timers = globalThis }) {
    let inFlight = null, timer = null;
    const check = () => {
        if (inFlight) return inFlight;
        if (waiting()) timer = timers.setTimeout(() => { timer = null; if (inFlight) slow(); }, slowMs);
        inFlight = Promise.allSettled([request()]).then(([result]) => {
            timers.clearTimeout(timer); timer = null; inFlight = null;
            apply(statusFromResult(result));
        });
        return inFlight;
    };
    check.stop = () => { timers.clearTimeout(timer); timer = null; };
    return check;
}
// The status shows as soon as it answers; the lists wait for each other (the
// workflow order comes with the projects) under the refresh serial. Resolves when
// the lists are in: callers act on them, never on the status.
function refresh() {
    const pending = refreshLists().finally(() => { if (refreshing === pending) refreshing = null; });
    refreshing = pending;
    return pending;
}
async function refreshLists() {
    const serial = ++refreshSerial, current = () => active && serial === refreshSerial;
    void statusCheck();
    clearTimeout(listsSlowTimer);
    if (!state.loaded.templates || !state.loaded.projects) listsSlowTimer = setTimeout(() => { if (current()) { state.listsSlow = true; render(); } }, LISTS_SLOW_MS);
    const lists = await Promise.allSettled([call('/sonder-editor/sdk/templates', {}, LIST_TIMEOUT_MS), call('/sonder-editor/launcher/projects', {}, LIST_TIMEOUT_MS), call('/sonder-editor/launcher/state', {}, LIST_TIMEOUT_MS)]);
    if (!current()) return;
    clearTimeout(listsSlowTimer); state.listsSlow = false;
    for (const err of applyRefreshResults(state, lists)) notifyError(err.message, { source: 'launcher-refresh', detail: err.detail });
    render();
    void checkRequirements();
    void loadNodePacks();
    scheduleThumbnailPoll();
}
// Check again: always asks for the status (joining a call in flight), and joins a
// refresh in flight rather than queueing the same work behind it on a slow ComfyUI.
// An Unreachable pill reads Checking… until the new answer.
function checkAgain() {
    if (state.status?.error) { state.status = null; render(true); }
    void statusCheck();
    return refreshing || refresh();
}
// Thumbnails: the page asks only for tiles in view, newest first, and polls while any
// are queued or rendering. Off asks for nothing.
const THUMB_PENDING = ['queued', 'rendering'], THUMB_BATCH = 50;
const batches = list => Array.from({ length: Math.ceil(list.length / THUMB_BATCH) }, (_, i) => list.slice(i * THUMB_BATCH, (i + 1) * THUMB_BATCH));
// Polling covers the pending tiles in view; others update when they scroll back in.
const visiblePending = () => thumbWanted.filter(f => THUMB_PENDING.includes(state.thumbnails[f]?.state));
function thumbnailsVisible(folders) {
    thumbWanted = folders; clearTimeout(thumbTimer);
    thumbTimer = setTimeout(() => { void run(requestThumbnails); scheduleThumbnailPoll(); }, 250);
}
async function requestThumbnails() {
    if (!active || thumbnailMode(state.prefs) === 'off') return;
    const modified = new Map(state.projects.map(p => [p.folder, String(p.modified_at || '')]));
    const folders = thumbWanted.filter(folder => {
        const thumb = state.thumbnails[folder];
        return thumb && ['stale', 'missing'].includes(thumb.state) && thumbRequested.get(folder) !== thumb.key;
    }).sort((a, b) => modified.get(b).localeCompare(modified.get(a), undefined, { numeric: true }));
    if (!folders.length) return;
    for (const folder of folders) { thumbRequested.set(folder, state.thumbnails[folder].key); state.thumbnails[folder] = { ...state.thumbnails[folder], state: 'queued' }; }
    for (const batch of batches(folders)) await call('/sonder-editor/launcher/thumbnails/request', { method: 'POST', ...json({ folders: batch }) });
    scheduleThumbnailPoll();
}
function scheduleThumbnailPoll() {
    clearTimeout(thumbPollTimer);
    if (active && thumbnailMode(state.prefs) !== 'off' && visiblePending().length) thumbPollTimer = setTimeout(() => void pollThumbnails(), 3000);
}
async function pollThumbnails() {
    const folders = visiblePending();
    if (!active || !folders.length) return;
    try {
        for (const batch of batches(folders)) {
            const payload = await call('/sonder-editor/launcher/thumbnails/status', { method: 'POST', ...json({ folders: batch }) });
            if (!active) return;
            for (const folder of batch) {
                const next = payload.thumbnails?.[folder];
                // A folder that no longer resolves stops polling; the next refresh decides.
                state.thumbnails[folder] = next ? { ...state.thumbnails[folder], state: next.state, shown: next.shown } : { ...state.thumbnails[folder], state: 'missing' };
            }
        }
        views[2]?.sync?.(state);
    } catch (err) { if (!active) return; }
    scheduleThumbnailPoll();
}
function preference(patch) {
    updateEditorSettings({ sdk: { launcher: patch } }); state.prefs = getEditorSettings().sdk.launcher;
    if (!state.prefs.showHidden && chosenProject()?.hidden) state.selectedProject = '';
    // The data doesn't depend on preferences: a refresh in flight renders with them.
    void run(() => refreshing || refresh());
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
        if (payload.template.description_reset) reset.push('description reset');
        if (payload.template.presets_kept) reset.push(`${payload.template.presets_kept} edited choice${payload.template.presets_kept === 1 ? '' : 's'} kept`);
        if (payload.template.categories_kept) reset.push(`${payload.template.categories_kept} edited categor${payload.template.categories_kept === 1 ? 'y' : 'ies'} kept`);
        // App-only Controls survive a replace; say which ones now need a look in Manage.
        const offline = (payload.template.local_missing || 0) + (payload.template.local_shadowed || 0);
        if (offline) reset.push(`${offline} app-only Control${offline === 1 ? '' : 's'} off (see Manage)`);
        notifySuccess(template ? 'Workflow replaced' : 'Workflow imported', {
            source: 'launcher-import', detail: template ? `Control values kept.${reset.length ? ` ${reset.join('; ')}.` : ''}` : undefined,
        });
    } catch (err) { state.importProblems = err.payload?.validation?.errors || [err.message]; render(); }
}
// Favorites come first in the Launcher and in the Sonder App's Workflow menu.
async function setFavorite(id, favorite) {
    await call('/sonder-editor/launcher/favorite', { method: 'PUT', ...json({ id, favorite }) });
    await refresh();
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
        menu.append(button('Export as API', () => { close(); void run(async () => {
            const filename = await downloadCuratedWorkflow(template, path => api.apiURL(path));
            notifySuccess('Workflow exported', { source: 'launcher-export', detail: filename });
        }); }));
        if (!template.starter) {
            menu.append(button('Rename', () => showDialog({ title: 'Rename workflow', initial: template.name, submit: async name => {
                await call(`/sonder-editor/sdk/templates/${template.template_id}`, { method: 'PATCH', ...json({ name }) }); await refresh();
            } })));
            menu.append(button('Replace…', () => { close(); pickFile(file => importFile(file, template)); }));
        }
        const favorite = state.memory.favorites?.includes(template.template_id);
        menu.append(button(favorite ? 'Remove from favorites' : 'Add to favorites', () => { close(); void run(() => setFavorite(template.template_id, !favorite)); }));
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
        menu.append(button('Show in folder', () => { close(); void run(() => call(`/sonder-editor/project/${encodeURIComponent(project.folder)}/reveal`, { method: 'POST' })); }, { disabled: !state.projectData.can_manage, title: state.projectData.can_manage ? 'Show in folder' : MACHINE_ONLY_MESSAGE }));
        menu.append(button(project.hidden ? 'Unhide' : 'Hide', () => { close(); void run(() => setHidden('project', project.folder, !project.hidden)); }));
        if (project.linked) menu.append(button('Unlink…', () => showDialog({ title: 'Unlink project', help: `Remove the link to “${project.name}”? The project’s files stay in place.`, accept: 'Unlink', submit: async () => {
            await call('/sonder-editor/projects/unlink', { method: 'POST', ...json({ project_id: project.folder }) }); await refresh();
        } })));
    });
}
// New project: prefilled from the selected workflow's size and rate, else the editor's
// project defaults; template and channels from the editor's defaults.
function createProject() {
    const workflow = chosenTemplate(), settings = getEditorSettings(), defaults = workflow?.validation?.editor_defaults || {};
    const size = Object.fromEntries(['fps', 'width', 'height'].map(k => [k, Number(defaults[k]) > 0 ? Number(defaults[k]) : settings.projectDefaults[k]]));
    popup?.(); dialog?.();
    const view = mountProjectDialog({
        workflowName: workflow?.name || '', canManage: !!state.projectData.can_manage,
        formatControls: () => mountProjectFormatControls({ settings, initial: { ...size, templateId: settings.projectDefaults.defaultTemplateId, channelTemplateId: settings.projectDefaults.defaultChannelTemplateId } }),
        create: async choice => {
            const { folder } = await createSonderProject({ ...choice, settings, apiURL: path => api.apiURL(path) });
            await refresh(); state.selectedProject = folder; state.selectedTemplateId = workflow?.template_id || state.selectedTemplateId;
            if (state.selectedTemplateId) void openEditor(); else render();
        },
        linksEnabled: async () => (await call('/sonder-editor/server-settings')).allow_external_project_links === true,
        preview: path => call('/sonder-editor/launcher/link-preview', { method: 'POST', ...json({ path }) }),
        link: async (path, turnOn) => {
            if (turnOn) await call('/sonder-editor/server-settings', { method: 'PUT', ...json({ allow_external_project_links: true }) });
            const { project } = await call('/sonder-editor/projects/link', { method: 'POST', ...json({ path }) });
            await refresh(); if (project?.name) selectProject(project.name);
            notifySuccess('Project folder linked', { source: 'launcher-link', detail: 'Its files stay where they are.' });
        },
        onClose: () => { dialog = null; },
    });
    dialog = view.close;
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
        // Without an answer the page can't know what is installed.
        const known = !!state.status?.install;
        for (const [label, info] of [['comfy-sdk', state.status?.install?.sdk], ['comfy-api-proxy', state.status?.install?.proxy]]) menu.append(el('div', 'sl-dim', `${label}: ${!known ? 'unknown' : info?.installed ? info.version : 'not installed'}`));
        menu.append(button('Check again', () => { close(); void run(checkAgain); }));
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
        showExitNotice(nextExitNotice(state.exitNotice, payload, { project: exitFlag.project, workflow: chosenTemplate()?.name }));
        if (state.exitNotice?.generating) exitPollTimer = setTimeout(() => void pollExitSession(), 2000);
    } catch (err) {
        if (!active) return;
        if (err.payload?.code === 'session_ended') showExitNotice(nextExitNotice(state.exitNotice, null));
        else exitPollTimer = setTimeout(() => void pollExitSession(), 5000);
    }
}
function showExitNotice(next) {
    // The first Cancel click must survive an unchanged polling result.
    if (JSON.stringify(next) === JSON.stringify(state.exitNotice)) return;
    state.exitNotice = next; root.querySelector('[data-sonder-exit-notice]')?.replaceWith(exitNotice());
}
const actions = {
    statusMenu, settingsMenu, preference, workflowMenu, projectMenu, create: createProject, thumbnailsVisible, checkAgain: () => void run(checkAgain),
    thumbnailURL: (folder, key, ext) => api.apiURL(`/sonder-editor/launcher/thumbnail/${encodeURIComponent(folder)}/${key}.${ext}`),
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
    retained?.sync?.(state);
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
    statusCheck = statusChecker({
        request: () => call('/sonder-editor/sdk/status', {}, STATUS_TIMEOUT_MS), slowMs: STATUS_SLOW_MS,
        waiting: () => !state.status || !!state.status.slow,
        slow: () => { if (active && (!state.status || state.status.slow)) { state.status = { slow: true }; render(true); } },
        apply: status => { if (active) { state.status = status; render(true); } },
    });
    window.addEventListener('pageshow', event => { if (event.persisted) window.location.reload(); });
    window.addEventListener('pagehide', () => { active = false; clearTimeout(exitPollTimer); statusCheck.stop(); clearTimeout(listsSlowTimer); clearTimeout(thumbTimer); clearTimeout(thumbPollTimer); popup?.(); dialog?.(); offKeys(); unsubscribe(); removeStyle(); for (const view of views) view.cleanup(); }, { once: true });
    document.body.classList.add('ready'); render(); void run(async () => { await refresh(); if (exitFlag) void pollExitSession(); });
}
if (isLauncherPage()) boot();
