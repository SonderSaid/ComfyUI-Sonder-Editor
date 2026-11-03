// The top of the Controls panel: the Sonder App's Workflow menu and the
// workflow's description. The host owns writes; the panel owns the save gate.
// The Modes table under it is controls_modes.js.
import { renderPanelMarkdown } from "./panel_markdown.js";
import { register, PRIORITY, PRESERVE_DEFAULT } from "./keyboard_ownership.js";
import { SPACE, TYPE, setButtonDisabled, setButtonVariant } from "./editor_theme.js";
import { inputCss, quietLineCss } from "./controls_panel_style.js";
import { setText } from "./controls_dom_order.js";

let serial = 0;

// Workflow ▾ (Sonder App only, through the host's `workflows` capability):
// favorites first, a star for the current workflow. Selecting one is refused
// while the panel is busy (a choice loading, a Manage save), then saves the
// panel's edits, asks about Manage drafts, then lets the host switch; on
// success the editor remounts the panel for the new workflow.
function mountWorkflowMenu(workflows, { flushPending, notify, canSwitch, busyReason }) {
    const row = document.createElement("div"), label = document.createElement("span"),
        select = document.createElement("select"), star = document.createElement("button");
    row.dataset.controlsWorkflowMenu = "true";
    // Wraps the label above the menu in a narrow sidebar, so a name stays readable.
    row.style.cssText = `display:flex;flex-wrap:wrap;align-items:center;gap:${SPACE.s1}px ${SPACE.s2}px;`;
    label.textContent = "Workflow";
    select.style.cssText = `${inputCss}flex:1 1 110px;min-width:0;`;
    select.setAttribute("aria-label", "Workflow");
    star.type = "button";
    setButtonVariant(star, "secondary", { padding: `2px ${SPACE.s2}px`, fontSize: `${TYPE.t11}px` });
    row.append(label, select, star);
    let listing = null, switching = false, starring = false, destroyed = false, starred = false, optionsKey = null;
    const current = () => workflows.current();
    const entries = () => {
        const id = current().template_id;
        const list = listing?.workflows || [];
        return list.some(w => w.template_id === id) ? list : [{ template_id: id, name: current().name || "This workflow", ok: true, favorite: false }, ...list];
    };
    function paint() {
        if (destroyed) return;
        const id = current().template_id, list = entries(), here = list.find(w => w.template_id === id);
        // Options are rebuilt only when they change: every panel render paints this.
        const options = list.map(w => [w.template_id, `${w.favorite ? "★ " : ""}${w.name || "Untitled workflow"}`,
            !w.ok && w.template_id !== id,
            // Missing nodes don't stop a switch: Generate names them when the
            // chosen Modes need them.
            !w.ok ? "This workflow can't drive the editor."
                : [w.description || "", w.missing_nodes?.length
                    ? `Some Modes use nodes that aren't installed: ${w.missing_nodes.join(", ")}.` : ""].filter(Boolean).join(" ")]);
        const key = JSON.stringify(options);
        if (key !== optionsKey) {
            optionsKey = key;
            select.replaceChildren(...options.map(([value, text, disabled, title]) => {
                const option = new Option(text, value);
                option.disabled = disabled;
                option.title = title;
                return option;
            }));
        }
        if (select.value !== id) select.value = id;
        const busy = switching || starring || workflows.busy();
        select.disabled = busy;
        select.title = workflows.busy() && !switching ? "Wait for Generate to answer, then switch." : "";
        const favorite = starred = !!here?.favorite;
        setText(star, favorite ? "★" : "☆");
        star.setAttribute("aria-pressed", String(favorite));
        star.setAttribute("aria-label", favorite ? "Remove from favorites" : "Add to favorites");
        star.title = favorite ? "Remove from favorites" : "Add to favorites (they come first here and in the Launcher)";
        setButtonDisabled(star, busy || !listing);
    }
    async function load() {
        const result = await workflows.list();
        if (destroyed) return;
        if (result?.ok) listing = result.payload;
        paint();
    }
    select.addEventListener("change", async () => {
        const id = select.value;
        if (switching || !id || id === current().template_id) return;
        // Refused before anything saves: waiting would switch on its own once
        // the load finishes, which the user never asked for.
        const busy = busyReason();
        if (busy) {
            notify(busy);
            paint();
            return;
        }
        switching = true;
        paint();
        try {
            if (!await flushPending()) {
                notify("The workflow was not switched because a Control could not be saved.");
                return;
            }
            const stillBusy = busyReason();
            if (stillBusy) {
                notify(stillBusy);
                return;
            }
            if (!canSwitch()) return;
            const result = await workflows.switchTo(id);
            if (!result?.ok) notify(result?.payload?.error || "The workflow could not be switched.");
        } finally {
            switching = false;
            paint();
        }
    });
    star.addEventListener("click", async () => {
        const id = current().template_id, favorite = !starred;
        starring = true;
        paint();
        try {
            const result = await workflows.setFavorite(id, favorite);
            if (!result?.ok) notify(result?.payload?.error || "The favorite could not be saved.");
            await load();
        } finally {
            starring = false;
            paint();
        }
    });
    paint();
    void load();
    return { el: row, paint, destroy() { destroyed = true; row.remove(); } };
}

export function mountWorkflowHeader(host, { flushPending, notify, canSwitch = () => true, busyReason = () => "" }) {
    const el = document.createElement("section"), description = document.createElement("div"),
        more = document.createElement("button"), body = document.createElement("div");
    el.dataset.controlsWorkflowHeader = "true";
    el.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;`;
    body.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;`;
    const workflowMenu = host.workflows ? mountWorkflowMenu(host.workflows, { flushPending, notify, canSwitch, busyReason }) : null;
    more.type = "button";
    more.style.cssText = quietLineCss;
    body.append(description, more);
    el.append(...(workflowMenu ? [workflowMenu.el] : []), body);
    let expanded = false, view = {}, shownText = null;
    function clamp() {
        description.style.cssText = expanded ? "" : "display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;";
        setText(more, expanded ? "Less" : "More");
        more.hidden = !view.description;
    }
    more.addEventListener("click", () => { expanded = !expanded; clamp(); });

    function render(next, { manage = false } = {}) {
        view = next || {};
        // Manage edits the description itself; the Workflow menu stays.
        const shown = !manage && !!view.description;
        body.style.display = shown ? "flex" : "none";
        el.style.display = shown || workflowMenu ? "flex" : "none";
        workflowMenu?.paint();
        // Rebuilt only when the text changes: a press on a link inside it
        // must survive the re-render a blur-save causes.
        const text = view.description || "";
        if (text !== shownText) {
            shownText = text;
            description.replaceChildren(renderPanelMarkdown(text));
        }
        clamp();
    }
    // Keys on the menu, the star and More stay with them, not the editor's
    // single-key shortcuts; Ctrl/Meta/Alt combinations and Escape go on.
    const unregister = register({id: `controls-workflow-header:${++serial}`, priority: PRIORITY.TEXT_EDITOR, keydown: e => {
        if (!el.contains(document.activeElement)) return false;
        if (e.ctrlKey || e.metaKey || e.altKey || e.key === "Escape") return false;
        return PRESERVE_DEFAULT;
    }});
    return {el, render, destroy() { workflowMenu?.destroy(); unregister(); el.remove(); }};
}
