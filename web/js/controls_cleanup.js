// Manage's explicit review. Hosts own reads, validation and the single write.
import { EDITOR_COLORS as COLORS, SPACE, TYPE, setButtonDisabled, setButtonVariant } from "./editor_theme.js";
import { register, PRIORITY } from "./keyboard_ownership.js";

const GROUPS = {
    bindings: "Missing Controls", suggestions: "Exposed before",
    definition_anchors: "Anchors inside subgraphs", values: "Saved values from earlier versions of this workflow",
    hidden: "Hidden items no longer in the workflow", preset_members: "Preset members",
};
let nextId = 0;
const make = (tag, text) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; return el; };
const smallButton = (text, variant = "secondary") => {
    const el = make("button", text); el.type = "button";
    setButtonVariant(el, variant, {padding: `2px ${SPACE.s2}px`, fontSize: `${TYPE.t11}px`}); return el;
};
const ready = view => !view?.error && view?.state === "ready" && view?.relay?.status === "ok";

export function mountCleanup(host, {track = work => work, notify = () => {}, flushPending = async () => true} = {}) {
    const id = ++nextId, el = make("section"), trigger = smallButton("Clean up unavailable…"), review = make("div");
    el.dataset.controlsCleanup = "true";
    el.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s2}px;`;
    review.style.cssText = `display:none;flex-direction:column;gap:${SPACE.s2}px;`;
    trigger.style.alignSelf = "flex-start";
    el.append(trigger, review);
    let opened = false, busy = false, destroyed = false, unregister = null, checkboxes = [], submit = null;
    const close = () => {
        opened = false; unregister?.(); unregister = null; checkboxes = [];
        review.replaceChildren(); review.style.display = "none"; render();
    };
    function selection() {
        const result = Object.fromEntries(Object.keys(GROUPS).map(key => [key, []]));
        for (const {key, row, input} of checkboxes) if (input.checked && !input.disabled)
            result[key].push(key === "preset_members" ? {preset_id: row.preset_id, binding_id: row.binding_id} : row.id);
        return result;
    }
    function sync() {
        const selected = new Set(checkboxes.filter(c => c.key === "bindings" && c.input.checked).map(c => c.row.id));
        for (const {key, row, input} of checkboxes) {
            input.disabled = busy || (key === "preset_members" && !!row.requires_binding && !selected.has(row.requires_binding));
        }
        if (submit) setButtonDisabled(submit, busy || !ready(host.getView()) || !Object.values(selection()).some(rows => rows.length));
        if (submit) submit.title = !ready(host.getView()) ? "Fix the Sonder Control Relay before cleaning up." : "Remove the checked items";
    }
    function render(view = host.getView()) {
        if (destroyed) return;
        const available = Object.values(view.cleanup || {}).some(rows => Array.isArray(rows) && rows.length)
            || !!view.restore?.restorable?.length;
        setButtonDisabled(trigger, busy || !ready(view) || !available);
        trigger.title = !ready(view) ? "Fix the Sonder Control Relay before cleaning up."
            : available ? "Review unavailable Controls and recovery data before removing them" : "Nothing qualifies for cleanup";
        trigger.setAttribute("aria-expanded", String(opened));
        if (opened) sync(); // retain the reviewed snapshot; the writer rechecks it
    }
    function open() {
        if (opened) { close(); return; }
        const view = host.getView();
        if (!ready(view) || busy) return;
        opened = true;
        const intro = make("div", "Only checked items will be removed. Kept preset members remain repairable in Manage. Removing a preset’s last member also deletes that preset.");
        intro.style.cssText = `font-size:${TYPE.t11}px;color:${COLORS.textSecondary};overflow-wrap:anywhere;`;
        const restoreCount = view.restore?.restorable?.length || 0;
        if (restoreCount) intro.textContent += ` ${restoreCount} Controls can be reconnected instead — use Restore Controls.`;
        const list = make("div");
        list.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;max-height:360px;overflow:auto;`;
        for (const [key, title] of Object.entries(GROUPS)) {
            const rows = view.cleanup?.[key] || [];
            if (!rows.length) continue;
            const heading = make("strong", title);
            heading.style.cssText = `font-size:${TYPE.t11}px;margin-top:${SPACE.s2}px;color:${COLORS.text};`;
            list.append(heading);
            if (key === "values") {
                const help = make("div", "These saved values return if you Replace back to an earlier workflow. Cleaning them up permanently forgets them.");
                help.style.cssText = `font-size:${TYPE.t11}px;color:${COLORS.textSecondary};`;
                list.append(help);
            }
            for (const row of rows) {
                const name = key === "preset_members"
                    ? `${view.presets?.find(p => p.id === row.preset_id)?.name || row.preset_id} · ${view.bindings?.find(b => b.binding_id === row.binding_id)?.label || row.binding_id}`
                    : row.label;
                const label = make("label"), input = make("input"), text = make("span", name);
                label.style.cssText = `display:flex;align-items:flex-start;gap:${SPACE.s1}px;color:${COLORS.text};overflow-wrap:anywhere;`;
                text.style.minWidth = "0";
                input.type = "checkbox"; input.checked = true;
                input.setAttribute("aria-label", `Clean up ${name}`);
                input.addEventListener("change", sync);
                if (row.preview) {
                    const preview = make("div", row.preview);
                    preview.style.cssText = `font-size:${TYPE.t11}px;color:${COLORS.textSecondary};white-space:pre-wrap;overflow-wrap:anywhere;`;
                    text.append(preview);
                }
                label.append(input, text); list.append(label); checkboxes.push({key, row, input});
            }
        }
        const actions = make("div"), cancel = smallButton("Cancel");
        submit = smallButton("Clean up", "accentSoft");
        actions.style.cssText = `display:flex;justify-content:flex-end;gap:${SPACE.s2}px;`;
        actions.append(cancel, submit);
        cancel.addEventListener("click", () => { if (!busy) close(); });
        submit.addEventListener("click", () => {
            if (busy || !ready(host.getView())) return;
            const changes = {"workflow:cleanup": selection()};
            busy = true; render(); setButtonDisabled(cancel, true);
            // Track immediately so workflow switching waits for this review's write.
            const work = (async () => {
                try {
                    if (!await flushPending()) throw new Error("Cleanup was not saved. Finish the pending Control edit first.");
                    if (destroyed) return;
                    const result = await host.applyChanges(changes);
                    if (!result?.ok) throw new Error(result?.rejected?.[0]?.reason || "Cleanup could not be saved.");
                    close(); notify("Unavailable Controls cleaned up.");
                } catch (error) { if (!destroyed) notify(error.message); }
                finally { busy = false; if (!destroyed) { setButtonDisabled(cancel, false); render(); } }
            })();
            track(work, changes);
        });
        review.replaceChildren(intro, list, actions); review.style.display = "flex";
        unregister = register({id: `controls-cleanup:${id}`, priority: PRIORITY.OVERLAY,
            keydown: e => { if (e.key !== "Escape" || e.isComposing || e.keyCode === 229) return false; if (!busy) close(); return true; }});
        render(); review.scrollIntoView?.({block: "nearest"});
    }
    trigger.addEventListener("click", open);
    return {el, render, get busy() { return busy; }, get editing() { return opened; },
        destroy() { destroyed = true; unregister?.(); el.remove(); }};
}
