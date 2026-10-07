import { el, button, input, setButtonDisabled } from './launcher_view.js';
import { register, PRIORITY } from './keyboard_ownership.js';
import { MACHINE_ONLY_MESSAGE } from './sdk_setup_client.js';

export function mountPopup(anchor, build, onClose = () => {}) {
    const node = el('div', 'sl-popup'); node.setAttribute('role', 'dialog');
    const rect = anchor?.getBoundingClientRect?.() || { left: anchor?.clientX || 8, bottom: anchor?.clientY || 8 };
    let disposed = false;
    const close = () => { if (disposed) return; disposed = true; off(); document.removeEventListener('pointerdown', outside, true); node.remove(); onClose(); };
    const outside = e => { if (!node.contains(e.target) && !anchor?.contains?.(e.target)) close(); };
    const off = register({ id: 'launcher-popup', priority: PRIORITY.OVERLAY, keydown(e) {
        if (e.isComposing || e.keyCode === 229) return false;
        if (e.key === 'Escape') { close(); anchor?.focus?.(); return true; } return false;
    } });
    build(node, close); document.body.append(node);
    node.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - node.offsetWidth - 8))}px`;
    node.style.top = `${Math.max(8, Math.min(rect.bottom + 4, window.innerHeight - node.offsetHeight - 8))}px`;
    document.addEventListener('pointerdown', outside, true);
    node.querySelector('button,select')?.focus();
    return close;
}

export function mountDialog({ title, initial = null, label = 'Name', help = '', accept = 'Save', submit, onClose = () => {} }) {
    const node = el('dialog', 'sl-dialog');
    node.setAttribute('aria-label', title);
    node.append(el('h2', '', title));
    if (help) node.append(el('p', '', help));
    let value = initial, busy = false, disposed = false;
    const field = initial === null ? null : input(label, initial, v => { value = v; }, 'dialog-name');
    if (field) { field.maxLength = 120; node.append(field); }
    const error = el('p', 'sl-bad'); node.append(error);
    const close = () => { if (disposed) return; disposed = true; off(); node.close(); node.remove(); onClose(); };
    const accidental = () => { if (!busy && (value === initial || window.confirm('Discard the name you entered?'))) close(); };
    const run = async () => {
        if (busy || (field && !value.trim())) { field?.focus(); return; }
        busy = true; setButtonDisabled(ok, true); setButtonDisabled(cancel, true);
        try { await submit(field ? value.trim() : undefined); close(); }
        catch (err) { error.textContent = String(err?.message || err); }
        finally { busy = false; setButtonDisabled(ok, false); setButtonDisabled(cancel, false); }
    };
    const cancel = button('Cancel', close), ok = button(accept, run, { primary: true });
    const actions = el('div', 'sl-dialog-actions'); actions.append(cancel, ok); node.append(actions);
    const off = register({ id: 'launcher-dialog', priority: PRIORITY.OVERLAY, keydown(e) {
        if (e.isComposing || e.keyCode === 229) return false;
        if (e.key === 'Escape') { accidental(); return true; }
        if (e.key === 'Enter' && e.target === field) { void run(); return true; } return false;
    } });
    node.addEventListener('cancel', e => { e.preventDefault(); accidental(); });
    node.addEventListener('click', e => { if (e.target === node) { const r = node.getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) accidental(); } });
    document.body.append(node); node.showModal(); (field || cancel).focus(); field?.select();
    return close;
}

// New project: Create new (name plus the project-format controls the editor toolbar
// uses) or Link existing (a server folder, previewed read-only before it is linked).
// The host supplies every request; this view owns the DOM, listeners and drafts.
export function mountProjectDialog({ workflowName = '', canManage = false, formatControls, preview, link, create, linksEnabled, onClose = () => {} }) {
    const node = el('dialog', 'sl-dialog sl-project-dialog'); node.setAttribute('aria-label', 'New project');
    node.append(el('h2', '', 'New project'));
    const tabs = el('div', 'sl-tabs'); tabs.setAttribute('role', 'tablist');
    const panels = { create: el('div', 'sl-tab-panel'), link: el('div', 'sl-tab-panel') };
    const tabButtons = {};
    let mode = 'create', busy = false, disposed = false, name = '', path = '', previewed = null, previewSerial = 0, previewTimer = null, enabled = null;
    const error = el('p', 'sl-bad'); error.setAttribute('role', 'alert');
    const cancel = button('Cancel', () => close()), ok = button('', () => void submit(), { primary: true });
    // Create
    const nameField = input('Project name', '', v => { name = v; sync(); }, 'dialog-project-name'); nameField.maxLength = 120;
    const format = formatControls(), initialFormat = JSON.stringify(format.read());
    panels.create.append(nameField, format.element, el('p', 'sl-dim', workflowName ? `Opens in the editor with ${workflowName}.` : 'Choose a workflow in the sidebar to open the editor after creating.'));
    // Link
    const pathField = input('Project folder on the ComfyUI machine', '', v => { path = v; previewed = null; schedulePreview(); sync(); }, 'dialog-link-path');
    const previewBox = el('div', 'sl-link-preview'), trust = el('p', 'sl-warn');
    if (canManage) panels.link.append(el('p', 'sl-dim', 'Paste the path of a Sonder project folder. Linking adds it to your projects; its files stay where they are.'), pathField, previewBox, trust);
    else panels.link.append(el('p', '', `${MACHINE_ONLY_MESSAGE} Linking a folder outside the projects folder can only be done from a browser on that computer.`));
    for (const [key, label] of [['create', 'Create new'], ['link', 'Link existing']]) {
        const b = button(label, () => select(key)); b.setAttribute('role', 'tab'); tabButtons[key] = b; tabs.append(b);
    }
    const actions = el('div', 'sl-dialog-actions'); actions.append(cancel, ok);
    node.append(tabs, panels.create, panels.link, error, actions);
    function select(key) {
        mode = key; error.textContent = '';
        for (const [k, b] of Object.entries(tabButtons)) { b.setAttribute('aria-selected', String(k === key)); b.setAttribute('aria-pressed', String(k === key)); }
        panels.create.hidden = key !== 'create'; panels.link.hidden = key !== 'link';
        if (key === 'link' && canManage && enabled === null) void linksEnabled().then(value => { enabled = value; sync(); }).catch(() => {});
        sync(); (key === 'create' ? nameField : canManage ? pathField : cancel).focus();
    }
    function sync() {
        if (mode === 'create') { ok.textContent = workflowName ? 'Create and open' : 'Create'; setButtonDisabled(ok, busy || !name.trim()); }
        else {
            ok.textContent = enabled === false ? 'Turn on links and link' : 'Link';
            setButtonDisabled(ok, busy || !canManage || !previewed);
            trust.textContent = enabled === false ? 'External project links are off. Turning them on lets this ComfyUI read and write project folders outside its projects folder, for everyone who can reach it. Link only folders you trust.' : '';
        }
        setButtonDisabled(cancel, busy);
    }
    function schedulePreview() {
        clearTimeout(previewTimer); previewBox.replaceChildren(); const serial = ++previewSerial;
        if (!path.trim()) return;
        previewTimer = setTimeout(async () => {
            previewBox.replaceChildren(el('span', 'sl-dim', 'Checking…'));
            try {
                const result = await preview(path.trim());
                if (disposed || serial !== previewSerial) return;
                enabled = result.links_enabled; previewed = result.preview; const p = previewed;
                previewBox.replaceChildren(...(p.readable
                    ? [el('div', 'sl-project-name', p.name), el('div', 'sl-dim', `${p.scene_count} ${p.scene_count === 1 ? 'scene' : 'scenes'} · storage format ${p.format_version}${p.too_new ? ' · 🔒 newer than this Sonder can open' : ''}`)]
                    : [el('div', 'sl-dim', 'Preview unavailable. You can still link it.')]),
                el('div', 'sl-folder', `Adds “${p.folder}” to your projects.`));
            } catch (err) {
                if (disposed || serial !== previewSerial) return;
                previewed = null; previewBox.replaceChildren(el('span', 'sl-bad', err.message));
            }
            sync();
        }, 350);
    }
    async function submit() {
        if (busy) return;
        if (mode === 'create' && !name.trim()) { nameField.focus(); return; }
        if (mode === 'link' && !previewed) return;
        busy = true; error.textContent = ''; sync();
        try {
            if (mode === 'create') await create({ name: name.trim(), ...format.read() });
            else await link(path.trim(), enabled === false);
            close();
        } catch (err) {
            error.textContent = String(err?.message || err);
            // "Turn on links and link" may have turned links on before the link failed.
            if (mode === 'link' && enabled === false) enabled = await linksEnabled().catch(() => enabled);
        }
        finally { busy = false; if (!disposed) sync(); }
    }
    const drafted = () => !!(name.trim() || path.trim()) || JSON.stringify(format.read()) !== initialFormat;
    const accidental = () => { if (!busy && (!drafted() || window.confirm('Discard this new project?'))) close(); };
    function close() { if (disposed) return; disposed = true; clearTimeout(previewTimer); off(); format.cleanup(); node.close(); node.remove(); onClose(); }
    const off = register({ id: 'launcher-project-dialog', priority: PRIORITY.OVERLAY, keydown(e) {
        if (e.isComposing || e.keyCode === 229) return false;
        if (e.key === 'Escape') { accidental(); return true; }
        if (e.key === 'Enter' && (e.target === nameField || e.target === pathField)) { void submit(); return true; } return false;
    } });
    node.addEventListener('cancel', e => { e.preventDefault(); accidental(); });
    node.addEventListener('click', e => { if (e.target === node) { const r = node.getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) accidental(); } });
    document.body.append(node); node.showModal(); select('create');
    return { close, select };
}
