import { el, button, input, setButtonDisabled } from './launcher_view.js';
import { register, PRIORITY } from './keyboard_ownership.js';

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
