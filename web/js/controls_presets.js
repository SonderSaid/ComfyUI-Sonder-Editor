// Shared preset selection. The host owns writes; the panel owns the save gate.
import { mountComboPicker } from "./controls_combo_picker.js";
import { renderPanelMarkdown } from "./panel_markdown.js";
import { register, PRIORITY, PRESERVE_DEFAULT } from "./keyboard_ownership.js";
import { SPACE, TYPE, EDITOR_COLORS as COLORS } from "./editor_theme.js";
import { inputCss, quietLineCss } from "./controls_panel_style.js";

let serial = 0;
export function selectedPreset(presets, lastApplied) {
    const matches = presets.filter(p => p.state === "match");
    return matches.find(p => p.id === lastApplied)
        || matches.sort((a, b) => b.members.length - a.members.length || a.order - b.order)[0]
        || null;
}

export function mountPresets(host, { flushPending, track, notify }) {
    const el = document.createElement("section"), description = document.createElement("div"),
        more = document.createElement("button"), row = document.createElement("label"),
        label = document.createElement("span"), control = document.createElement("div"),
        detail = document.createElement("div");
    el.dataset.controlsPresets = "true";
    el.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;`;
    row.style.cssText = `display:grid;grid-template-columns:auto minmax(0,1fr);align-items:center;gap:${SPACE.s2}px;`;
    label.textContent = "Preset";
    more.type = "button";
    more.style.cssText = quietLineCss;
    detail.style.cssText = `color:${COLORS.textSecondary};font-size:${TYPE.t11}px;`;
    row.append(label, control);
    el.append(description, more, row, detail);
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
        el.style.display = hidden ? "none" : "flex";
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
    return {el, render, destroy() { destroyed = true; picker?.destroy(); unregister(); el.remove(); }};
}
