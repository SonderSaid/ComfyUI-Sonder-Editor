// Shared preset selection, and the Sonder App's Workflow menu above it. The
// host owns writes; the panel owns the save gate.
import { mountComboPicker } from "./controls_combo_picker.js";
import { renderPanelMarkdown } from "./panel_markdown.js";
import { register, PRIORITY, PRESERVE_DEFAULT } from "./keyboard_ownership.js";
import { SPACE, TYPE, EDITOR_COLORS as COLORS, setButtonDisabled, setButtonVariant } from "./editor_theme.js";
import { inputCss, quietLineCss } from "./controls_panel_style.js";

let serial = 0;
export function selectedPreset(presets, lastApplied) {
    const matches = presets.filter(p => p.state === "match");
    return matches.find(p => p.id === lastApplied)
        || matches.sort((a, b) => b.members.length - a.members.length || a.order - b.order)[0]
        || null;
}

// Workflow ▾ (Sonder App only, through the host's `workflows` capability):
// favorites first, a star for the current workflow. Selecting one saves the
// panel's edits, asks about Manage drafts, then lets the host switch; on
// success the editor remounts the panel for the new workflow.
function mountWorkflowMenu(workflows, { flushPending, notify, canSwitch }) {
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
    let listing = null, switching = false, starring = false, destroyed = false, starred = false;
    const current = () => workflows.current();
    const entries = () => {
        const id = current().template_id;
        const list = listing?.workflows || [];
        return list.some(w => w.template_id === id) ? list : [{ template_id: id, name: current().name || "This workflow", ok: true, favorite: false }, ...list];
    };
    function paint() {
        if (destroyed) return;
        const id = current().template_id, list = entries(), here = list.find(w => w.template_id === id);
        select.replaceChildren(...list.map(w => {
            const option = new Option(`${w.favorite ? "★ " : ""}${w.name || "Untitled workflow"}`, w.template_id);
            option.disabled = !w.ok && w.template_id !== id;
            option.title = w.ok ? (w.description || "") : "This workflow can't drive the editor.";
            return option;
        }));
        select.value = id;
        const busy = switching || starring || workflows.busy();
        select.disabled = busy;
        select.title = workflows.busy() && !switching ? "Wait for Generate to answer, then switch." : "";
        const favorite = starred = !!here?.favorite;
        star.textContent = favorite ? "★" : "☆";
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
        switching = true;
        paint();
        try {
            if (!await flushPending()) {
                notify("The workflow was not switched because a Control could not be saved.");
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

export function mountPresets(host, { flushPending, track, notify, canSwitch = () => true }) {
    const el = document.createElement("section"), description = document.createElement("div"),
        more = document.createElement("button"), row = document.createElement("label"),
        label = document.createElement("span"), control = document.createElement("div"),
        detail = document.createElement("div"), body = document.createElement("div");
    el.dataset.controlsPresets = "true";
    el.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;`;
    body.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;`;
    const workflowMenu = host.workflows ? mountWorkflowMenu(host.workflows, { flushPending, notify, canSwitch }) : null;
    row.style.cssText = `display:grid;grid-template-columns:auto minmax(0,1fr);align-items:center;gap:${SPACE.s2}px;`;
    label.textContent = "Preset";
    more.type = "button";
    more.style.cssText = quietLineCss;
    detail.style.cssText = `color:${COLORS.textSecondary};font-size:${TYPE.t11}px;`;
    row.append(label, control);
    body.append(description, more, row, detail);
    el.append(...(workflowMenu ? [workflowMenu.el] : []), body);
    let lastApplied = null, expanded = false, busy = false, destroyed = false, view = {}, picker = null, select = null, key = "", labels = new Map();
    function clamp() {
        description.style.cssText = expanded ? "" : "display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;";
        more.textContent = expanded ? "Less" : "More";
        more.hidden = !view.description;
    }
    more.addEventListener("click", () => { expanded = !expanded; clamp(); });
    async function choose(id) {
        if (busy || destroyed || !id) return;
        const preset = (view.presets || []).find(p => p.id === id);
        if (!preset || ["unavailable", "refused"].includes(preset.state)) { notify(preset?.reason || "This preset is unavailable. Open Manage."); render(view); return; }
        busy = true;
        render(view);
        // Snapshot prior saves before tracking this whole operation, so Generate
        // waits even while this apply is waiting for the previous value save.
        const flushed = flushPending();
        const work = (async () => {
            try {
                if (!await flushed || destroyed) {
                    if (!destroyed) notify("The preset was not applied because a Control could not be saved.");
                    return false;
                }
                const result = await host.applyChanges({[`preset:${id}:apply`]: true});
                if (!result?.ok) notify(result?.rejected?.[0]?.reason || "The preset could not be applied.");
                else lastApplied = id;
                return !!result?.ok;
            } catch (error) { notify(error?.message || "The preset could not be applied."); return false; }
            finally { busy = false; if (!destroyed) render(host.getView()); }
        })();
        await track(work, true);
    }

    function render(next, hidden = false) {
        view = next || {};
        // Manage edits the description and presets itself; the Workflow menu stays.
        body.style.display = hidden ? "none" : "flex";
        el.style.display = hidden && !workflowMenu ? "none" : "flex";
        workflowMenu?.paint();
        description.replaceChildren(renderPanelMarkdown(view.description || ""));
        clamp();
        const presets = view.presets || [], selected = selectedPreset(presets, lastApplied);
        const nextKey = JSON.stringify(presets.map(p => [p.id, p.name, p.state, p.reason]));
        row.style.display = presets.length ? "grid" : "none";
        if (nextKey !== key || (!picker && !select)) {
            key = nextKey;
            picker?.destroy(); picker = null; select = null;
            control.replaceChildren();
            if (presets.length > 20) {
                labels = new Map(presets.map(p => [p.id, presets.some(other => other.id !== p.id && other.name === p.name) ? `${p.name} (${p.id})` : p.name]));
                picker = mountComboPicker({inputCss, choose: text => choose(presets.find(p => labels.get(p.id) === text)?.id), cancel: () => render(view)});
                picker.update({status: "ok", options: [...labels.values()], optionDetails: Object.fromEntries(presets.map(p => [labels.get(p.id), {disabled: ["unavailable", "refused"].includes(p.state), title: p.reason}]))});
                picker.input.setAttribute("aria-label", "Preset");
                control.append(picker.box);
            } else {
                select = document.createElement("select");
                select.style.cssText = inputCss;
                select.setAttribute("aria-label", "Preset");
                const custom = new Option("Custom", ""); custom.disabled = true;
                select.append(custom, ...presets.map(p => {
                    const option = new Option(p.name, p.id);
                    option.disabled = ["unavailable", "refused"].includes(p.state);
                    option.title = p.reason || "";
                    return option;
                }));
                select.addEventListener("change", () => void choose(select.value));
                control.append(select);
            }
        }
        if (select) { select.value = selected?.id || ""; select.disabled = busy; }
        if (picker) { picker.setValue(selected ? labels.get(selected.id) : "Custom"); picker.input.disabled = busy; }
        detail.replaceChildren(renderPanelMarkdown(selected?.description || ""));
    }
    const unregister = register({id: `controls-presets:${++serial}`, priority: PRIORITY.TEXT_EDITOR, keydown: e => {
        if (!el.contains(document.activeElement)) return false;
        if (e.isComposing || e.keyCode === 229) return PRESERVE_DEFAULT;
        if (picker?.contains(document.activeElement) && picker.keydown(e)) return true;
        return PRESERVE_DEFAULT;
    }});
    return {el, render, destroy() { destroyed = true; picker?.destroy(); workflowMenu?.destroy(); unregister(); el.remove(); }};
}
