// Check and repair: what is missing or damaged in a project's stored parts, what a repair
// loses, and the repair itself. The host supplies the requests; this view owns the DOM.
import { el, button, plural, setButtonDisabled } from './launcher_view.js';
import { register, PRIORITY } from './keyboard_ownership.js';

const UNREPAIRABLE = {
    invalid_json: 'project.json can’t be read. Restore it from a backup.',
    too_new: 'A newer Sonder version saved this project. Update Sonder to open it.',
    invalid_storage: 'The storage section of project.json isn’t valid. Restore project.json from a backup.',
    state_unreadable: 'The project’s state folder is missing or can’t be read. Reconnect its drive or restore the folder from a backup, then check again.',
    unavailable: 'Some stored parts can’t be read right now. Close other programs that use the project’s folder, or reconnect its drive, then check again.',
    held_unreadable: 'The list of kept parts, state/held.json, can’t be read. Move it out of the project’s folder, then check again.',
    no_locks: 'This project’s drive doesn’t support file locks, so it can’t be repaired while Share Projects Between ComfyUI Servers is on. Move the project to a local drive and check again. If no other ComfyUI server is running and none of its runs are going, you can instead turn the setting off in the editor’s Settings ▸ Server, then repair.',
};
const NAMES_SHOWN = 5;

function historyLine(item) {
    if (item.outcome === 'restored') return 'Prompt history: restored in full.';
    const kept = item.kept === null || item.kept === undefined ? '' : ` (${plural(item.kept, 'entry', 'entries')}${item.lost ? `; ${item.lost} more can’t be found` : ''})`;
    if (item.outcome === 'entries_dropped') return `Prompt history: ${plural(item.lost, 'entry is', 'entries are')} lost. ${item.kept === 1 ? '1 entry stays' : `${item.kept} entries stay`}.`;
    if (item.outcome === 'previous') return `Prompt history: goes back to the version before${kept}.`;
    if (item.outcome === 'salvaged') return `Prompt history: uses an older copy found in the project’s folder${kept}. It may miss entries, or have some that were never saved.`;
    return 'Prompt history: lost. It starts empty.';
}

/** `{ lines, canRepair }` for a check's answer. No DOM. */
export function repairSummary(report) {
    if (report.status === 'healthy') return { lines: ['Nothing is missing or damaged.'], canRepair: false };
    if (report.status !== 'damaged') return { lines: [UNREPAIRABLE[report.reason] || 'This project can’t be repaired here. Restore it from a backup.'], canRepair: false };
    const of = kind => report.problems.filter(item => item.kind === kind), lines = of('history').map(historyLine);
    const dropped = kind => of(kind).filter(item => item.outcome === 'dropped');
    const details = dropped('details').map(item => item.name), jobs = dropped('job').length, unused = of('unused').length;
    const restored = [...of('details'), ...of('job')].filter(item => item.outcome === 'restored').length;
    const leftover = of('leftover').reduce((sum, item) => sum + item.count, 0);
    if (restored) lines.push(`${plural(restored, 'part is', 'parts are')} restored in full.`);
    if (details.length) {
        const more = details.length - NAMES_SHOWN;
        lines.push(`${plural(details.length, 'asset loses its', 'assets lose their')} generation details: ${details.slice(0, NAMES_SHOWN).join(', ')}${more > 0 ? ` and ${more} more` : ''}. The media and its place on the timeline stay.`);
    }
    if (jobs) lines.push(`${plural(jobs, 'Render Queue job is', 'Render Queue jobs are')} removed, because the settings can’t be found.`);
    if (unused) lines.push(`${plural(unused, 'unused part is', 'unused parts are')} removed.`);
    if (leftover) lines.push(`${plural(leftover, 'damaged unused file is', 'damaged unused files are')} set aside, so ${leftover === 1 ? 'it' : 'they'} can’t stop a save.`);
    return { lines, canRepair: true };
}

export function mountRepairDialog({ name, check, repair, onClose = () => {} }) {
    const node = el('dialog', 'sl-dialog'); node.setAttribute('aria-label', 'Check and repair');
    const body = el('div'), error = el('p', 'sl-bad'), actions = el('div', 'sl-dialog-actions');
    body.style.overflowWrap = 'anywhere'; // a backup's name is one long word
    body.setAttribute('aria-live', 'polite'); error.setAttribute('role', 'alert');
    node.append(el('h2', '', 'Check and repair'), el('p', 'sl-dim', name), body, error, actions);
    let busy = false, repairing = false, disposed = false;
    const close = () => { if (disposed) return; disposed = true; off(); node.close(); node.remove(); onClose(); };
    const show = (lines, buttons) => {
        body.replaceChildren(...lines.map(line => el('p', '', line)));
        actions.replaceChildren(...buttons); buttons[0]?.focus();
    };
    // One request at a time; the buttons of the last answer stay, disabled, meanwhile.
    async function perform(action) {
        if (busy) return;
        busy = true; error.textContent = '';
        for (const node of actions.children) setButtonDisabled(node, repairing || node.textContent !== 'Close');
        try { await action(); }
        catch (err) {
            if (disposed) return;
            error.textContent = UNREPAIRABLE[err?.payload?.reason] || String(err?.message || err);
            // The plan is no longer what was shown: only a new check can be acted on.
            show([], [button('Close', close), button('Check again', runCheck)]);
        }
        finally { busy = repairing = false; }
    }
    const runCheck = () => perform(async () => {
        body.replaceChildren(el('p', 'sl-dim', 'Checking…'));
        const report = await check();
        if (disposed) return;
        const { lines, canRepair } = repairSummary(report);
        const buttons = [button('Close', close)];
        if (canRepair) {
            lines.push('Repair first saves a copy of project.json and keeps every stored part that is still there.');
            buttons.push(button('Repair', () => runRepair(report.digest), { primary: true }));
        } else if (report.status !== 'healthy') buttons.push(button('Check again', runCheck));
        show(lines, buttons);
    });
    const runRepair = digest => { if (busy) return; repairing = true; return perform(async () => {
        const result = await repair(digest);
        if (disposed) return;
        const lines = [`Repaired. The copy of project.json is ${result.backup} in the project’s folder.`], buttons = [button('Close', close)];
        const left = result.left_in_place?.length || 0;
        if (left) lines.push(`${plural(left, 'damaged file', 'damaged files')} couldn’t be set aside. Close other programs that use the project’s folder, then check again.`);
        if (result.reopened === false) lines.push('The project couldn’t be opened afterwards. Check it again.');
        if (left || result.reopened === false) buttons.push(button('Check again', runCheck));
        show(lines, buttons);
    }); };
    const off = register({ id: 'launcher-repair-dialog', priority: PRIORITY.OVERLAY, keydown(e) {
        if (e.isComposing || e.keyCode === 229) return false;
        if (e.key === 'Escape') { if (!repairing) close(); return true; }
        return false;
    } });
    node.addEventListener('cancel', e => { e.preventDefault(); if (!repairing) close(); });
    show([], [button('Close', close)]);
    document.body.append(node); node.showModal(); void runCheck();
    return close;
}
