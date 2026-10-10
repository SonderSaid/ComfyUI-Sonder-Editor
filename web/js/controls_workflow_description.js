// Manage-only description editor. Host owns persistence; this peer owns the
// draft, safe preview, keyboard registration and save result.
import { DESCRIPTION_MAX } from "./control_targets.js";
import { renderPanelMarkdown } from "./panel_markdown.js";
import { register, PRIORITY, PRESERVE_DEFAULT } from "./keyboard_ownership.js";
import { EDITOR_COLORS as COLORS, SPACE, TYPE, chromeInputCss, setButtonVariant, setButtonDisabled } from "./editor_theme.js";
let nextId = 0;
export function mountWorkflowDescription(host, { track = work => work } = {}) {
    const make = (tag, text) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; return el; };
    const el = make("section"), label = make("label", "Workflow description"), area = make("textarea");
    const preview = make("div"), status = make("div"), actions = make("div");
    const save = make("button", "Save description"), cancel = make("button", "Cancel");
    const id = `controls-description-${++nextId}`;
    area.id = id; label.setAttribute("for", id); area.rows = 3;
    area.dataset.workflowDescription = "true"; area.dataset.sonderTextUndo = "true";
    area.placeholder = "Describe this workflow for the Launcher. Markdown supported.";
    area.style.cssText = chromeInputCss({ minWidth: "0" }) + "width:100%;box-sizing:border-box;resize:vertical;";
    el.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;`;
    preview.style.cssText = "overflow-wrap:anywhere;";
    status.style.cssText = `font-size:${TYPE.t11}px;color:${COLORS.textDim};`;
    actions.style.cssText = `display:flex;gap:${SPACE.s2}px;`;
    for (const button of [save, cancel]) { button.type = "button"; setButtonVariant(button, "secondary"); }
    actions.append(save, cancel); el.append(label, area, status, actions, preview);
    let stored = "", dirty = false, busy = false, destroyed = false, composing = false, error = "";
    function paint() {
        const length = [...area.value].length;
        save.textContent = busy ? "Saving…" : "Save description";
        status.textContent = error ? `Not saved: ${error}` : `${length}/${DESCRIPTION_MAX} · Ctrl/⌘+Enter saves · Esc cancels`;
        setButtonDisabled(save, busy || !dirty || length > DESCRIPTION_MAX);
        preview.replaceChildren(renderPanelMarkdown(area.value));
    }
    function render(view = host.getView()) {
        stored = typeof view?.description === "string" ? view.description : "";
        if (!dirty && !busy) area.value = stored;
        paint();
    }
    async function commit() {
        if (busy || destroyed || !dirty) return false;
        if ([...area.value].length > DESCRIPTION_MAX) { error = "Use at most 1000 characters."; paint(); return false; }
        const value = area.value, changes = { "workflow:description": value };
        busy = true; error = ""; paint();
        const work = (async () => {
            let result;
            try { result = await host.applyChanges(changes); }
            catch (e) { result = { ok: false, rejected: [{ reason: e.message }] }; }
            if (destroyed) return !!result?.ok;
            busy = false;
            if (result?.ok) {
                stored = value;
                // Cancel during a save discards the draft, not the accepted write.
                if (!dirty || area.value === value) { area.value = value; dirty = false; }
            }
            else error = result?.rejected?.[0]?.reason || "The description could not be saved.";
            paint(); return !!result?.ok;
        })();
        return track(work, changes);
    }
    function discard() { area.value = stored; dirty = false; error = ""; paint(); }
    area.addEventListener("input", () => { dirty = area.value !== stored; error = ""; paint(); });
    area.addEventListener("compositionstart", () => { composing = true; });
    area.addEventListener("compositionend", () => { composing = false; });
    save.addEventListener("click", () => void commit()); cancel.addEventListener("click", discard);
    const unregister = register({ id, priority: PRIORITY.TEXT_EDITOR, keydown: e => {
        if (document.activeElement !== area) return false;
        if (composing || e.isComposing || e.keyCode === 229) return PRESERVE_DEFAULT;
        if (e.key === "Escape") { discard(); return true; }
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { void commit(); return true; }
        return PRESERVE_DEFAULT;
    }});
    render();
    return { el, render, get editing() { return dirty || document.activeElement === area; },
        destroy() { destroyed = true; unregister(); el.remove(); } };
}
