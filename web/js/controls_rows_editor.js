// The Controls panel's editor for a row-list Control (kind ROWS): a node's
// whole dynamic row group, such as the LoRA stack's rows, as one value.
//
// Module-host contract: the panel (editor_controls_panel.js) owns the value,
// writes and refusals; this module owns its DOM and listeners and returns a
// cleanup. Collapsed it shows the summary (`3 LoRAs · 2 on`); expanded it shows
// one line per row built from the server's field specs, plus Add and Remove.
// Every edit sends the whole new list; the panel pairs it with the list it was
// made from, so the server refuses an edit of rows that changed meanwhile.
import { mountComboPicker, wantsComboPicker } from "./controls_combo_picker.js";
import { smallButton } from "./controls_manage.js";
import { canonical, pluralName, summary } from "./control_rows.js";
import { VISUALLY_HIDDEN_CSS, describedBy, setText } from "./controls_dom_order.js";
import * as S from "./controls_panel_style.js";
import { EDITOR_COLORS as COLORS, FONT, RADIUS, SPACE, TYPE } from "./editor_theme.js";

export const NO_ROWS_NOTICE = "This ComfyUI frontend can't show these rows yet: it needs frontend 1.57 or newer. "
    + "Saving this workflow here drops its rows.";

const make = (tag, text) => {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    return el;
};
let nextHelp = 0;

// `attachTip(el, getText, options)` shows a field's help on hover and focus and returns detach.
export function mountRowsEditor({ commit, onEditing = () => {}, attachTip = () => () => {} }) {
    const el = make("div");
    el.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;min-width:0;width:100%;`;
    const header = make("button");
    header.type = "button";
    header.style.cssText = `display:flex;align-items:center;gap:${SPACE.s1}px;background:none;border:0;padding:0;`
        + `cursor:pointer;color:${COLORS.text};font:${TYPE.t12}px ${FONT.sans};text-align:left;min-width:0;`;
    const caret = make("span", "▸");
    caret.style.cssText = `color:${COLORS.textDim};flex:0 0 auto;`;
    const summaryText = make("span");
    summaryText.style.cssText = S.labelTextCss;
    header.append(caret, summaryText);
    const list = make("div");
    list.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;min-width:0;`;
    const notice = make("div");
    notice.style.cssText = `font-size:${TYPE.t11}px;line-height:1.4;color:${COLORS.warningText};`;
    const add = smallButton("+ Add");
    add.style.alignSelf = "flex-start";
    el.append(header, notice, list, add);

    let spec = null, value = [], editable = false, expanded = false, editing = false, rendered = "";
    let pickers = [], removes = [], tipDetach = [], cancelling = false;
    // A press inside the list holds repaints until it ends. A save that lands
    // between mousedown and mouseup (a field's blur-save, often) would replace
    // the pressed button, and Chromium then sends it no click.
    let pressed = false, deferred = false;

    const noun = () => spec?.group_name || "row";
    const setEditing = next => {
        if (editing === next) return;
        editing = next;
        onEditing(next);
    };
    // Editing means a row field has focus. A Remove button is not a field: the
    // rows must repaint after a removal, or later clicks hit the wrong row.
    const isField = target => !!target && list.contains(target) && target.tagName !== "BUTTON";
    list.addEventListener("focusin", e => setEditing(isField(e.target)));
    const release = () => {
        document.removeEventListener("pointerup", release, true);
        document.removeEventListener("pointercancel", release, true);
        window.removeEventListener?.("blur", release);
        pressed = false;
        // After the click this press makes, which runs in the same task.
        if (deferred) setTimeout(() => { deferred = false; if (spec) paint(); }, 0);
    };
    list.addEventListener("pointerdown", () => {
        if (pressed) return;
        pressed = true;
        // A native popup (a <select>) or another window can take the pointerup:
        // leaving the list or the window ends the press too.
        document.addEventListener("pointerup", release, true);
        document.addEventListener("pointercancel", release, true);
        window.addEventListener?.("blur", release);
    });
    list.addEventListener("focusout", e => { if (pressed && !list.contains(e.relatedTarget)) release(); });
    list.addEventListener("focusout", e => { if (!isField(e.relatedTarget)) setEditing(false); });

    function send(next) {
        const before = value;
        value = next;
        paintSummary();
        return commit(next, before);
    }
    const changed = (index, field, fieldValue) => {
        if (value[index]?.[field] === fieldValue) return;
        send(value.map((row, i) => (i === index ? { ...row, [field]: fieldValue } : row)));
    };

    // "<field>: <its help>", or nothing: the field's own name stays its title.
    // Screen readers hear the help through a hidden description in the line.
    function fieldTip(target, field, line, suppressed = null) {
        const help = typeof field.tooltip === "string" ? field.tooltip : "";
        if (!help) { target.title = field.label || field.id; return; }
        target.removeAttribute("title");
        const description = make("span", help);
        description.id = `controls-rows-help-${++nextHelp}`;
        description.style.cssText = VISUALLY_HIDDEN_CSS;
        line.append(description);
        describedBy(target, description.id, true);
        const text = `${field.label || field.id}: ${help}`;
        tipDetach.push(attachTip(target, () => text, { suppressed }));
    }

    function fieldInput(field, index, current, line) {
        const label = `${spec.group_name || "Row"} ${index + 1} ${field.label || field.id}`;
        if (field.kind === "COMBO" && wantsComboPicker(field)) {
            const picker = mountComboPicker({
                inputCss: S.inputCss,
                choose: chosen => changed(index, field.id, chosen),
                cancel: () => picker.setValue(value[index]?.[field.id]),
                onLeave: () => {},
            });
            picker.update({ ...field, status: editable ? "ok" : "off" });
            picker.setValue(current);
            picker.input.setAttribute("aria-label", label);
            // Never over its open option list.
            fieldTip(picker.input, field, line, () => picker.open);
            picker.box.style.cssText = "flex:1 1 auto;min-width:0;display:flex;flex-direction:column;gap:2px;";
            pickers.push(picker);
            return picker.box;
        }
        let input;
        if (field.kind === "COMBO") {
            input = make("select");
            const options = (field.options || []).map(String);
            if (!options.includes(String(current ?? ""))) options.unshift(String(current ?? ""));
            input.append(...options.map(option => new Option(option, option)));
            input.value = String(current ?? "");
            input.style.cssText = S.inputCss + "flex:1 1 auto;cursor:pointer;";
            input.addEventListener("change", () => changed(index, field.id, input.value));
        } else if (field.kind === "BOOLEAN") {
            input = make("input");
            input.type = "checkbox";
            input.checked = current === true;
            input.style.cssText = "margin:0;cursor:pointer;flex:0 0 auto;";
            input.addEventListener("change", () => changed(index, field.id, input.checked));
        } else {
            input = make("input");
            const numeric = field.kind === "INT" || field.kind === "FLOAT";
            input.type = numeric ? "number" : "text";
            if (numeric) {
                if (field.min != null) input.min = String(field.min);
                if (field.max != null) input.max = String(field.max);
                input.step = field.kind === "INT" ? "1" : String(field.step || "any");
            }
            input.value = String(current ?? "");
            input.style.cssText = numeric ? S.numberInputCss(false) : S.inputCss + "flex:1 1 auto;";
            input.addEventListener("change", () => {
                if (cancelling) return; // Escape: the repaint restores the row's value
                if (input.value.trim() === "") { input.value = String(value[index]?.[field.id] ?? ""); return; }
                changed(index, field.id, numeric ? Number(input.value) : input.value);
            });
        }
        input.disabled = !editable;
        fieldTip(input, field, line);
        input.setAttribute("aria-label", label);
        return input;
    }

    function paintRows() {
        for (const picker of pickers) picker.destroy();
        for (const detach of tipDetach) detach();
        pickers = [];
        tipDetach = [];
        removes = [];
        const lines = value.map((row, index) => {
            const line = make("div");
            line.style.cssText = `display:flex;align-items:center;gap:${SPACE.s1}px;min-width:0;`
                + `padding:2px ${SPACE.s1}px;border:1px solid ${COLORS.border};border-radius:${RADIUS.r2}px;`;
            // An on/off field leads its row, as a checkbox does in a list.
            const fields = [...(spec.fields || [])].sort((a, b) => (b.kind === "BOOLEAN") - (a.kind === "BOOLEAN"));
            for (const field of fields) line.append(fieldInput(field, index, row?.[field.id], line));
            const remove = make("button", "×");
            remove.type = "button";
            remove.title = `Remove ${noun()} ${index + 1}`;
            remove.setAttribute("aria-label", remove.title);
            remove.disabled = !editable || value.length <= (spec.min ?? 0);
            removes.push(remove);
            remove.style.cssText = `flex:0 0 auto;background:none;border:0;color:${COLORS.textDim};cursor:pointer;`
                + `font:${TYPE.t14}px ${FONT.sans};padding:0 2px;`;
            remove.addEventListener("click", () => {
                send(value.filter((_, i) => i !== index));
                paintRows();
                // Keep keyboard focus on the list: the next row's Remove, else the header.
                const next = removes[Math.min(index, removes.length - 1)];
                (next && !next.disabled ? next : header).focus();
            });
            line.append(remove);
            return line;
        });
        list.replaceChildren(...lines);
        rendered = canonical([value, spec.fields, editable]);
    }

    function paintSummary() {
        setText(summaryText, summary(value, spec));
        header.title = expanded ? `Hide the ${pluralName(noun())}` : `Show the ${pluralName(noun())}`;
    }

    function paint() {
        setText(caret, expanded ? "▾" : "▸");
        header.setAttribute("aria-expanded", String(expanded));
        paintSummary();
        list.style.display = expanded ? "flex" : "none";
        add.style.display = expanded && editable ? "" : "none";
        setText(add, `+ Add ${noun()}`);
        add.disabled = !editable || value.length >= (spec.max ?? 20);
        if (!expanded || isField(document.activeElement) || rendered === canonical([value, spec.fields, editable])) return;
        if (pressed) deferred = true;
        else paintRows();
    }

    header.addEventListener("click", () => { expanded = !expanded; paint(); });
    add.addEventListener("click", () => {
        if (value.length >= (spec.max ?? 20)) return;
        send([...value, { ...(spec.new_row || {}) }]);
        paintRows();
    });

    return {
        el,
        get editing() { return editing; },
        // Where a description of the whole Control (why it is off) belongs.
        get describeTargets() { return [header]; },
        contains: target => el.contains(target),
        update(nextSpec, nextValue, { canEdit = true, unavailable = "" } = {}) {
            spec = nextSpec;
            value = Array.isArray(nextValue) ? nextValue.map(row => ({ ...row })) : [];
            editable = canEdit && !unavailable;
            notice.textContent = unavailable;
            notice.style.display = unavailable ? "" : "none";
            header.style.display = unavailable ? "none" : "flex";
            if (unavailable) { list.style.display = add.style.display = "none"; return; }
            paint();
        },
        keydown(e) {
            const picker = pickers.find(p => p.contains(document.activeElement));
            if (picker) return picker.keydown(e);
            const field = document.activeElement;
            if (e.key === "Enter" && field?.tagName === "INPUT") { field.blur(); return true; }
            if (e.key === "Escape") {
                // Revert, never commit: blur fires the field's change event.
                cancelling = true;
                try { field?.blur?.(); } finally { cancelling = false; }
                rendered = "";
                paint();
                return true;
            }
            return false;
        },
        // A refused edit: show the rows the panel holds now, even while a field
        // has focus, so the next edit starts from them rather than the refused list.
        reset(nextValue) {
            const hadFocus = list.contains(document.activeElement);
            value = Array.isArray(nextValue) ? nextValue.map(row => ({ ...row })) : [];
            rendered = "";
            // A press in the list keeps its button; the repaint follows the press.
            if (pressed) deferred = true;
            else if (expanded) paintRows();
            paintSummary();
            if (hadFocus && !pressed) header.focus();
            setEditing(false);
        },
        // A focused number or text field commits on blur (its change event).
        flush() {
            const field = document.activeElement;
            if (el.contains(field) && field.tagName === "INPUT") field.blur();
        },
        destroy() {
            release();
            deferred = false;
            spec = null;
            for (const picker of pickers) picker.destroy();
            for (const detach of tipDetach) detach();
            pickers = [];
            tipDetach = [];
            el.remove();
        },
    };
}
