// Preset authoring DOM only. Values are captured by the validating host.
import { register, PRIORITY, PRESERVE_DEFAULT } from "./keyboard_ownership.js";
import { EDITOR_COLORS as COLORS, SPACE, TYPE, chromeInputCss, setButtonDisabled, setButtonVariant } from "./editor_theme.js";

let serial = 0;
const make = (tag, text) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; return el; };
const button = text => { const el = make("button", text); el.type = "button"; setButtonVariant(el, "secondary", {padding: `2px ${SPACE.s2}px`, fontSize: `${TYPE.t11}px`}); return el; };

export function mountPresetsManage(host, { track, notify, flushPending = async () => true, changed = () => {} }) {
    const el = make("section"), heading = make("div", "Presets"), list = make("div"), add = button("+ Preset"),
        editor = make("div"), name = make("input"), description = make("textarea"), missing = make("div"),
        actions = make("div"), save = button("Create"), remove = button("Delete"), reset = button("Reset to workflow's version");
    el.dataset.controlsPresetsManage = "true";
    el.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s2}px;`;
    editor.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;`;
    actions.style.cssText = `display:flex;flex-wrap:wrap;gap:${SPACE.s1}px;`;
    list.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;`;
    name.placeholder = "Preset name"; name.setAttribute("aria-label", "Preset name");
    description.dataset.presetDescription = "true";
    description.placeholder = "Preset description"; description.setAttribute("aria-label", "Preset description");
    for (const input of [name, description]) { input.style.cssText = chromeInputCss({minWidth: "0"}) + "width:100%;box-sizing:border-box;"; input.dataset.sonderTextUndo = "true"; }
    actions.append(save, remove, reset);
    editor.append(name, description, missing, actions);
    el.append(heading, list, add, editor);
    let view = {}, selected = null, draft = false, members = new Set(), busy = false, destroyed = false, membershipDirty = false;
    let tail = Promise.resolve(), queued = 0;
    const pendingText = new Map();
    const current = () => (view.presets || []).find(p => p.id === selected);
    const dirty = () => draft && (!!name.value || !!description.value || members.size > 0);
    const canLeave = () => !busy && (!dirty() || globalThis.confirm("Discard this unsaved preset draft?"));
    function select(id, isDraft = false) {
        if (!canLeave()) return false;
        selected = id; draft = isDraft; membershipDirty = false;
        const p = current();
        name.value = p?.name || ""; description.value = p?.description || "";
        members = new Set((p?.members || []).map(m => m.binding_id));
        render(view); changed(); return true;
    }
    add.addEventListener("click", () => {
        const bytes = new Uint8Array(10); crypto.getRandomValues(bytes);
        select("p" + Array.from(bytes, b => b.toString(16).padStart(2, "0")).join(""), true);
        name.focus();
    });
    function write(field, value) {
        if (!selected || destroyed) return Promise.resolve(false);
        const id = selected, target = `preset:${id}:${field}`;
        const textField = field === "name" || field === "description";
        if (textField) pendingText.set(target, value);
        // Snapshot saves before registering capture itself in the panel gate.
        const flushed = ["create", "capture"].includes(field) ? flushPending() : Promise.resolve(true);
        queued++; busy = true; render(view);
        const work = tail.then(async () => {
            try {
                if (!await flushed) { notify("The preset was not captured because a Control could not be saved."); return false; }
                const result = await host.applyChanges({[target]: value});
                if (!result?.ok) notify(result?.rejected?.[0]?.reason || "The preset could not be saved.");
                if (result?.ok && selected === id && ["create", "capture"].includes(field)) { draft = false; membershipDirty = false; }
                if (result?.ok && selected === id && ["remove", "reset"].includes(field)) selected = null;
                return !!result?.ok;
            } catch (error) { notify(error?.message || "The preset could not be saved."); return false; }
            finally {
                if (pendingText.get(target) === value) pendingText.delete(target);
                busy = --queued > 0;
                if (!destroyed) { render(host.getView()); changed(); }
            }
        });
        tail = work;
        return track(work, {[target]: value});
    }
    function saveText(field, input) {
        if (draft || !selected || input.value === (current()?.[field] || "")) return;
        void write(field, input.value);
    }
    name.addEventListener("blur", () => saveText("name", name));
    description.addEventListener("blur", () => saveText("description", description));
    save.addEventListener("mousedown", e => e.preventDefault());
    save.addEventListener("click", () => {
        saveText("name", name); saveText("description", description);
        void write(draft ? "create" : "capture", {members: [...members], ...(draft ? {name: name.value, description: description.value} : {})});
    });
    remove.addEventListener("click", () => {
        if (host.capabilities?.hide && !globalThis.confirm(`Delete preset “${current()?.name || name.value}”?`)) return;
        void write("remove", true);
    });
    reset.addEventListener("click", () => void write("reset", true));
    function memberControl(spec) {
        if (!selected || !spec.binding_id) return null;
        const bid = spec.binding_id, p = current(), stored = p?.members.find(m => m.binding_id === bid);
        const label = make("label"), checkbox = make("input"), text = make("span");
        checkbox.type = "checkbox"; checkbox.checked = members.has(bid);
        checkbox.disabled = busy || ((!stored && !checkbox.checked) && (spec.status !== "ok" || spec.hidden));
        checkbox.setAttribute("aria-label", `Include ${spec.label || bid} in preset`);
        // An absent member has no descriptor to label it, so the line names its id.
        text.textContent = spec.absent ? `${bid} · In preset` : "In preset";
        if (stored && (stored.preview !== String(spec.value) || (stored.seed_mode && stored.seed_mode !== spec.seed_mode)))
            text.textContent += ` · Preset: ${stored.preview}${stored.seed_mode ? ` (${stored.seed_mode})` : ""}`;
        if (stored?.seed_mode && spec.seed_mode == null) text.textContent += " · Seed mode is skipped on this host";
        label.style.cssText = `display:flex;gap:${SPACE.s1}px;align-items:center;color:${COLORS.textSecondary};font-size:${TYPE.t11}px;`;
        checkbox.addEventListener("change", () => { checkbox.checked ? members.add(bid) : members.delete(bid); membershipDirty = true; });
        label.append(checkbox, text);
        return label;
    }
    function render(next = host.getView()) {
        view = next || {};
        const presets = view.presets || [], p = current();
        if (!draft && p && !membershipDirty) members = new Set(p.members.map(m => m.binding_id));
        if (!draft && p) {
            if (document.activeElement !== name && !pendingText.has(`preset:${selected}:name`)) name.value = p.name;
            if (document.activeElement !== description && !pendingText.has(`preset:${selected}:description`)) description.value = p.description;
        }
        list.replaceChildren(...presets.map((item, i) => {
            const row = make("div"), choose = button(`${item.name} · ${item.members.length} · ${item.state} · ${item.origin.replace("_", " ")}`), up = button("↑"), down = button("↓");
            row.style.cssText = `display:flex;gap:${SPACE.s1}px;`; choose.style.flex = "1"; choose.style.minWidth = "0"; choose.style.whiteSpace = "normal"; choose.style.overflowWrap = "anywhere";
            setButtonDisabled(choose, busy);
            choose.title = item.reason || item.description; choose.setAttribute("aria-pressed", String(selected === item.id));
            choose.addEventListener("click", () => select(item.id));
            up.title = "Move preset up"; down.title = "Move preset down";
            setButtonDisabled(up, busy || i === 0); setButtonDisabled(down, busy || i === presets.length - 1);
            up.addEventListener("click", () => { if (select(item.id)) void write("move", {before: presets[i - 1].id}); });
            down.addEventListener("click", () => { if (select(item.id)) void write("move", {before: presets[i + 2]?.id || null}); });
            row.append(choose, up, down); return row;
        }));
        editor.style.display = selected ? "flex" : "none";
        save.textContent = draft ? "Create" : "Update from current values";
        remove.hidden = draft; reset.hidden = draft || p?.origin !== "app_copy";
        for (const b of [save, remove, reset, add]) setButtonDisabled(b, busy);
        const ids = new Set((view.bindings || []).map(b => b.binding_id));
        const absent = (p?.members || []).filter(m => !ids.has(m.binding_id));
        missing.replaceChildren();
        if (absent.length) {
            missing.append(make("div", "Members without a Control"));
            for (const member of absent) missing.append(memberControl({binding_id: member.binding_id, status: "missing", absent: true}));
        }
    }
    const unregister = register({id: `controls-presets-manage:${++serial}`, priority: PRIORITY.TEXT_EDITOR, keydown: e => {
        const input = document.activeElement;
        if (input !== name && input !== description) return false;
        if (e.isComposing || e.keyCode === 229) return PRESERVE_DEFAULT;
        const field = input === name ? "name" : "description";
        if (e.key === "Escape") { input.value = draft ? "" : current()?.[field] || ""; input.blur(); return true; }
        if (e.key === "Enter" && (input === name || e.ctrlKey || e.metaKey)) { input.blur(); return true; }
        return PRESERVE_DEFAULT;
    }});
    return {el, render, memberControl, canLeave, get editing() { return dirty() || [name, description].includes(document.activeElement); }, destroy() { destroyed = true; unregister(); el.remove(); }};
}
