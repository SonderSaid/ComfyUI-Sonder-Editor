import { el, button, component, modelMessage } from './launcher_view.js';
import { missingNodes, missingNodesHelp, missingNodesMessage } from './launcher_workflows.js';

export function pairProblems(project, template, status, formats = {}, nodePacks = null) {
    const result = [], add = (kind, text) => result.push({ kind, text });
    if (missingNodes(template).length) add('bad', missingNodesMessage(missingNodes(template), nodePacks));
    if (project?.too_new) add('bad', `This project uses storage format ${project.format_version}${project.written_with ? `, saved with Sonder ${project.written_with}` : ''}. Update Sonder to open it.`);
    if (project?.unreadable) add('bad', 'This project can’t be read. Check its project.json in the project folder.');
    for (const text of [...(template?.validation?.errors || []), ...(template?.validation?.blocking || [])]) add('bad', text);
    for (const item of template?.missing_models || []) add('warn', modelMessage(item));
    for (const text of template?.validation?.warnings || []) add('dim', text);
    if (template?.requirements_error) add('warn', template.requirements_error);
    if (project?.upgrade) {
        const u = project.upgrade;
        const trigger = u.when === 'generation' ? 'Your first edit that records generation or prompt history' : u.when === 'prompt_history' ? 'Your first edit that records prompt history' : 'Your first edit';
        add('warn', `${trigger} upgrades this project's storage (format ${u.from} → ${u.to}). Sonder versions before ${formats[u.to] || 'the version supporting this format'} can't open it after that.`);
    }
    if (status?.install && !status.install.ready) add('warn', 'Generate needs supported comfy-sdk and comfy-api-proxy packages. Install them in the editor’s Settings → Sonder App.');
    return result;
}
export function mountOpenBar(state, actions) {
    const project = state.projects.find(p => p.folder === state.selectedProject) || state.projectData.unreadable.map(p => ({ ...p, unreadable: true })).find(p => p.folder === state.selectedProject);
    const template = state.templates.find(t => t.template_id === state.selectedTemplateId);
    const bar = el('footer', 'sl-open'), content = el('div', 'sl-open-content');
    content.append(el('div', 'sl-open-summary', project ? `${project.name || project.folder}${project.scene?.name ? ` · ${project.scene.name}` : ''} with ${template?.name || 'a workflow — choose one'}` : 'Choose a project and a workflow.'));
    for (const p of pairProblems(project, template, state.status, state.projectData.format_first_release, state.nodePacks)) content.append(el('div', `sl-problem sl-${p.kind}`, p.text));
    if (missingNodes(template).length) content.append(missingNodesHelp());
    if (state.openError) {
        content.append(el('div', 'sl-problem sl-bad', state.openError.error));
        content.append(button('Details', actions.openDetails));
    }
    const open = button(state.opening ? '' : state.openError ? 'Try again' : 'Open', actions.open, {
        primary: true, title: 'Open the selected project and workflow (Enter)',
        disabled: state.opening || !project || !template || !!project.too_new || !!project.reason_code || !!missingNodes(template).length,
    });
    if (state.opening) { open.append(el('span', 'sl-spinner', '◌'), document.createTextNode(' Opening…')); }
    bar.append(content, open); return component(bar);
}
