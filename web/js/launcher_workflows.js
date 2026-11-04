import { renderPanelMarkdown } from './panel_markdown.js';
import { el, button, component, badge, date, input, modelMessage, listState, listNotice } from './launcher_view.js';
import { isUnfinishedRun } from './sdk_host.js';
import { missingNodePacks } from './node_pack_mappings.js';
export const SAMPLE_PROJECTS_URL = 'https://huggingface.co/datasets/SonderSaid/Sonder-Editor-Projects';
export const MISSING_NODES_HELP_URL = 'https://github.com/SonderSaid/ComfyUI-Sonder-Editor/blob/main/docs/troubleshooting.md#the-launcher-says-missing-node-packs';
export function missingNodes(template) { return template?.validation?.missing_nodes || []; }
// The nodes the selected row's current run needs and this ComfyUI lacks (its
// requirements check); null until that check answered.
export function runMissingNodes(template) {
    return Array.isArray(template?.run_missing_nodes) ? template.run_missing_nodes : null;
}
// " Needed by Passes → Upscale Pass Only." when the server named the Mode choices.
export function neededBy(item) {
    return item?.needed_by?.length ? ` Needed by ${item.needed_by.join('; ')}.` : '';
}
// A row's badge. The workflow's own problems come first; a workflow with none is
// Ready only while this ComfyUI can generate (the header pill's install state).
// A missing node counts only when the current run needs it: what only other Modes
// use is greyed out in the editor, and never stops an Open.
export function workflowBadge(template, status = null) {
    const v = template.validation || {};
    if (runMissingNodes(template)?.length) return ['Missing nodes', 'bad'];
    if (v.errors?.length || v.blocking?.length || template.run_problems?.length) return ['Can’t generate', 'bad'];
    if (template.missing_models?.length) return [`${template.missing_models.length} models missing`, 'warn'];
    if (template.requirements_error) return ['Check failed', 'warn'];
    if (v.warnings?.length) return [`${v.warnings.length} notes`, 'warn'];
    if (!Array.isArray(template.missing_models)) return ['Select to check', ''];
    if (status?.install && !status.install.ready) return ['Generate unavailable', 'warn'];
    return ['Ready', 'good'];
}
// The workflows pane's line when it has no rows to show. Its order comes with the
// projects, so it waits for both lists.
export function workflowsNotice(state, listed, shown) {
    const list = listState(state, 'templates', 'projects');
    if (list === 'loading') return { text: 'Loading workflows…', failed: false };
    if (list === 'slow') return { text: 'Still loading workflows…', failed: false };
    if (list === 'failed') return { text: 'Workflows couldn’t load.', failed: true };
    if (!listed) return { text: 'Export an API workflow from ComfyUI, then drop the JSON here or choose Import.', failed: false };
    return shown ? null : { text: 'No workflows match. Clear the search or show hidden workflows.', failed: false };
}
// The selected workflow's Controls line. Advanced Controls stay out of it (it
// shows the curated menu) but are counted; their warnings still show above.
export function controlsLine(controls) {
    if (!controls.length) return 'No Controls exposed in this workflow.';
    const shown = controls.filter(c => !c.advanced), advanced = controls.length - shown.length;
    return `Controls: ${[...shown.map(c => c.label), ...(advanced ? [`+${advanced} advanced`] : [])].join(', ')}`;
}
// The exit notice's workflow: each take still generating names the workflow it
// was submitted with, since the session may have switched after it.
export function generatingWorkflows(runs, fallback = 'the workflow') {
    const names = [...new Set((runs || []).map(run => run?.template_name).filter(Boolean))];
    return names.length ? names.join(' and ') : fallback;
}
export { missingNodePacks };
export function missingNodesMessage(classes, mappings) {
    const groups = missingNodePacks(classes, mappings);
    if (!groups.some(group => group.pack)) {
        return `Missing nodes: ${(classes || []).join(', ')}. Install the node packs that provide them, restart ComfyUI, then reload this page.`;
    }
    const named = groups.map(group => group.pack ? `${group.pack} (${group.classes.join(', ')})` : `no known pack (${group.classes.join(', ')})`);
    return `Missing node packs: ${named.join(', ')}. Install them, restart ComfyUI, then reload this page.`;
}
// The lines about missing nodes, for the row's details and the Open bar: what the
// current run needs (it can't generate), and what only other Modes use. Nothing
// until the row's check answers, so nothing flashes; if it couldn't answer, the
// list stands alone rather than being lost.
export function missingNodeLines(template, mappings) {
    const all = missingNodes(template), run = runMissingNodes(template);
    if (run === null) return all.length && template?.requirements_error ? [{ kind: 'warn', text: missingNodesMessage(all, mappings) }] : [];
    const needed = run.map(item => item.class_type), lines = [];
    if (needed.length) lines.push({ kind: 'bad', text: missingNodesMessage(needed, mappings) + run.map(neededBy).join('') });
    const others = all.filter(name => !needed.includes(name));
    if (others.length) lines.push({ kind: 'dim', text: `Some Modes of this workflow use nodes that aren’t installed: ${others.join(', ')}. The current Modes don’t need them.` });
    return lines;
}
// Only a line that asks for an install offers help installing.
export function asksForInstall(lines) { return lines.some(line => line.kind !== 'dim'); }
export function missingNodesHelp() {
    const link = el('a', '', 'How to install missing node packs ↗');
    link.href = MISSING_NODES_HELP_URL; link.target = '_blank'; link.rel = 'noopener noreferrer';
    return link;
}
// The exit notice after a status poll of the session left behind. A null payload
// means the session ended (ComfyUI restarted, or a later Open took its takes
// over): nothing is left to show or cancel here.
export function nextExitNotice(previous, payload, { project = '', workflow = 'the workflow' } = {}) {
    if (!payload) return null;
    const unfinished = (payload.runs || []).filter(isUnfinishedRun), generating = unfinished.length;
    if (!generating && !previous) return null;
    return { generating, project: payload.project || project, workflow: generatingWorkflows(unfinished, payload.template_name || workflow) };
}
export function mountWorkflows(state, actions) {
    const sidebar = el('aside', `sl-sidebar${state.workflowExpanded ? ' expanded' : ''}`);
    sidebar.setAttribute('aria-label', 'Workflows');
    const selected = state.templates.find(t => t.template_id === state.selectedTemplateId);
    const picker = button(`Workflow: ${selected?.name || 'Choose…'} ▾`, actions.toggleWorkflows);
    picker.classList.add('sl-mobile-picker'); picker.setAttribute('aria-expanded', String(state.workflowExpanded)); sidebar.append(picker);
    const head = el('div', 'sl-sidebar-head'), title = el('div', 'sl-row');
    title.append(el('h2', 'sl-title', 'Workflows'), button('Import…', actions.import));
    head.append(title, input('Search workflows', state.workflowSearch, actions.searchWorkflows, 'workflow-search'));
    if (state.importProblems?.length) head.append(el('div', 'sl-bad sl-problem', state.importProblems.join('\n')));
    sidebar.append(head);
    const list = el('div', 'sl-workflow-list'); list.dataset.scrollKey = 'workflows';
    const hidden = new Set(state.memory.hidden?.workflows || []), favorites = new Set(state.memory.favorites || []);
    const byId = new Map(state.templates.map(t => [t.template_id, t]));
    const ordered = state.projectData.workflow_order.map(id => byId.get(id)).filter(Boolean);
    const query = state.workflowSearch.toLowerCase();
    for (const starter of [false, true]) {
        const rows = ordered.filter(t => !!t.starter === starter && (state.prefs.showHidden || !hidden.has(t.template_id)) && `${t.name} ${t.source_name}`.toLowerCase().includes(query));
        if (!rows.length) continue;
        list.append(el('div', 'sl-section-label', starter ? 'Starters' : 'Your workflows'));
        for (const t of rows) {
            const chosen = t.template_id === state.selectedTemplateId;
            const row = el('div', `sl-workflow${chosen ? ' selected' : ''}${hidden.has(t.template_id) ? ' hidden' : ''}`);
            row.tabIndex = 0; row.dataset.workflowId = t.template_id; row.setAttribute('role', 'button'); row.setAttribute('aria-pressed', String(chosen));
            const line = el('div', 'sl-row'); line.append(el('div', 'sl-workflow-name', `${favorites.has(t.template_id) ? '★ ' : ''}${t.name}`));
            const menu = button('⋮', e => { e.stopPropagation(); actions.workflowMenu(t, e.currentTarget); }, { title: `Actions for ${t.name}` });
            line.append(menu);
            const sub = el('div', 'sl-workflow-sub');
            const used = state.memory.last_workflow?.by_template?.[t.template_id];
            sub.append(el('span', '', t.starter ? t.model_family : `${used ? 'used' : 'imported'} ${date(used || t.imported_at)}`), badge(...workflowBadge(t, state.status)));
            row.append(line, sub);
            if (t.description) {
                const firstLine = renderPanelMarkdown(t.description.split(/\r?\n/, 1)[0]).textContent;
                const summary = el('div', 'sl-dim', firstLine);
                summary.style.cssText = 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
                row.append(summary);
            }
            if (chosen) {
                const details = el('div', 'sl-workflow-details');
                const nodeLines = missingNodeLines(t, state.nodePacks);
                for (const line of nodeLines) details.append(el('p', `sl-${line.kind}`, line.text));
                if (asksForInstall(nodeLines)) details.append(missingNodesHelp());
                for (const problem of t.run_problems || []) details.append(el('p', 'sl-bad', problem));
                for (const problem of [...(t.validation?.errors || []), ...(t.validation?.blocking || [])]) details.append(el('p', 'sl-bad', problem));
                for (const item of t.missing_models || []) details.append(el('p', 'sl-warn', modelMessage(item)));
                for (const note of t.validation?.warnings || []) details.append(el('p', 'sl-dim', note));
                if (t.description) {
                    const description = el('div', 'sl-workflow-description');
                    description.style.cssText = 'overflow-wrap:anywhere;';
                    description.append(renderPanelMarkdown(t.description));
                    description.addEventListener('click', e => e.stopPropagation());
                    details.append(description);
                }
                // Controls of an absent node read "not installed"; the rest work.
                details.append(el('p', '', controlsLine(t.validation?.controls || [])));
                row.append(details);
            }
            row.addEventListener('click', () => actions.selectWorkflow(t.template_id));
            row.addEventListener('keydown', e => { if (e.target === row && !e.isComposing && ['Enter', ' '].includes(e.key)) { e.preventDefault(); actions.selectWorkflow(t.template_id); } });
            row.addEventListener('contextmenu', e => { e.preventDefault(); actions.workflowMenu(t, e); });
            list.append(row);
        }
    }
    const notice = workflowsNotice(state, ordered.length, list.children.length);
    if (notice) list.append(listNotice(notice.text, notice.failed, actions.checkAgain));
    sidebar.append(list);
    const foot = el('div', 'sl-samples'), link = el('a', '', 'Sample projects ↗');
    link.href = SAMPLE_PROJECTS_URL; link.target = '_blank'; link.rel = 'noopener noreferrer';
    foot.append(link, el('p', '', 'Unpack into your projects folder, or use Link existing in the editor.')); sidebar.append(foot);
    sidebar.addEventListener('dragover', e => { if (e.dataTransfer?.types?.includes('Files')) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } });
    sidebar.addEventListener('drop', e => { e.preventDefault(); const file = e.dataTransfer?.files?.[0]; if (file) actions.importFile(file); });
    return component(sidebar);
}
