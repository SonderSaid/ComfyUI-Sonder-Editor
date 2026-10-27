// The Controls panel's pickers for the Reference Selector's lanes and the
// Driver Selector's lane (plans/bridge-selections-controls.md, D).
//
// Module-host contract, the same as controls_rows_editor.js: the panel owns the
// value, writes and refusals and calls `update`, `keydown`, `reset`, `flush` and
// `destroy`; this module owns its DOM and listeners. Choices come from the host's
// picker sources (`choices` on update): the current project and scene of the
// Editor hosting the panel. Displayed numbers are 1-based like the graph panels;
// stored values stay zero-based. Viewing never rewrites a value: only Add, Remove
// and a choice commit, and the Reference list commits its canonical form.
import { formatLaneSelection, parseLaneSelection, selectorPanelView } from "./reference_bridge_shape.js";
import { smallButton } from "./controls_manage.js";
import * as S from "./controls_panel_style.js";
import { EDITOR_COLORS as COLORS, FONT, RADIUS, SPACE, TYPE } from "./editor_theme.js";

const make = (tag, text) => {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    return el;
};

/** Normalized asset folder label, as Save Bridge registers it ("" is Root). */
export const normalizeAssetFolder = value =>
    String(value ?? "").trim().replace(/\\/g, "/").replace(/^\/+/, "").replace(/\/+$/, "");

// What a picker says about its choices. `held` is true while it still lists the
// last good choices (a revalidation or a failed refresh never blanks them).
export function choicesStatus(choices, noun, held) {
    switch (choices?.status) {
        case "loading": return held ? "" : `Loading ${noun}…`;
        case "error": return held ? `Couldn't refresh the ${noun}.` : `Couldn't read this scene's ${noun}.`;
        case "unavailable": return "Open a project scene to list its lanes.";
        default: return "";
    }
}

// A Retry for a failed read; the host's sources re-read on click.
function retryButton() {
    const button = smallButton("Retry");
    button.style.alignSelf = "flex-start";
    button.style.display = "none";
    return button;
}

export function mountReferenceLanePicker({ commit, onEditing = () => {} }) {
    const el = make("div");
    el.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;min-width:0;width:100%;`;
    const list = make("div");
    list.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;min-width:0;`;
    const status = make("div");
    status.style.cssText = `font-size:${TYPE.t11}px;line-height:1.4;color:${COLORS.textDim};`;
    const disclosures = make("div");
    disclosures.style.cssText = `display:flex;flex-direction:column;gap:2px;font-size:${TYPE.t11}px;color:${COLORS.warningText};`;
    const add = smallButton("+ Add lane");
    add.style.alignSelf = "flex-start";
    add.setAttribute("aria-haspopup", "menu");
    add.setAttribute("aria-expanded", "false");
    const menu = make("div");
    menu.setAttribute("role", "menu");
    menu.style.cssText = `display:none;flex-direction:column;gap:2px;max-height:180px;overflow:auto;padding:${SPACE.s1}px;`
        + `border:1px solid ${COLORS.border};border-radius:${RADIUS.r2}px;background:${COLORS.panelMuted};`;
    const retry = retryButton();
    el.append(list, status, retry, disclosures, add, menu);

    let value = "", spec = null, choices = null, editable = false, open = false, view = null;
    let onRetry = () => {}, painted = null;
    retry.addEventListener("click", () => onRetry());

    const setOpen = next => {
        if (open === next) return;
        open = next;
        menu.style.display = open ? "flex" : "none";
        add.setAttribute("aria-expanded", String(open));
        onEditing(open);
    };
    const outside = e => { if (!el.contains(e.target)) setOpen(false); };
    globalThis.document?.addEventListener?.("pointerdown", outside, true);

    function send(indices) {
        const before = value;
        const next = formatLaneSelection(indices);
        if (next === before) return;
        value = next;
        paint();
        commit(next, before);
    }

    function paint() {
        const selection = parseLaneSelection(value);
        // The last good list stays shown while it is re-read or a refresh fails.
        const ready = Array.isArray(choices?.lanes);
        view = selectorPanelView({
            lanes: ready ? choices.lanes : [],
            laneIndices: selection.laneIndices,
            invalidTokens: selection.invalidTokens,
            sceneName: choices?.sceneName || "",
            source: choices?.source || "live",
        });
        list.replaceChildren(...view.rows.map(row => {
            const line = make("div");
            line.style.cssText = `display:flex;align-items:flex-start;gap:${SPACE.s1}px;min-width:0;`
                + `padding:2px ${SPACE.s1}px;border:1px solid ${COLORS.border};border-radius:${RADIUS.r2}px;`;
            const copy = make("div");
            copy.style.cssText = "min-width:0;flex:1;";
            // Without the scene's lanes a row is only its number.
            const name = make("div", ready ? row.label : `Reference ${row.laneIndex + 1}`);
            name.style.cssText = S.labelTextCss + `color:${row.orphan && ready ? COLORS.warningText : COLORS.text};`;
            name.title = name.textContent;
            copy.append(name);
            if (ready) {
                const detail = make("div", row.status);
                detail.style.cssText = `font-size:${TYPE.t11}px;color:${COLORS.textDim};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`;
                detail.title = row.status;
                copy.append(detail);
            }
            const remove = make("button", "×");
            remove.type = "button";
            remove.title = `Remove Reference ${row.laneIndex + 1}`;
            remove.setAttribute("aria-label", remove.title);
            remove.disabled = !editable;
            remove.style.cssText = `flex:0 0 auto;background:none;border:0;color:${COLORS.textDim};cursor:pointer;`
                + `font:${TYPE.t14}px ${FONT.sans};padding:0 2px;`;
            remove.addEventListener("click", () => {
                setOpen(false);
                send(parseLaneSelection(value).laneIndices.filter(index => index !== row.laneIndex));
            });
            line.append(copy, remove);
            return line;
        }));
        if (!view.rows.length) {
            const empty = make("div", "No Reference lanes selected.");
            empty.style.cssText = `font-size:${TYPE.t11}px;color:${COLORS.textDim};`;
            list.append(empty);
        }
        status.textContent = choicesStatus(choices, "lanes", ready) || (ready && choices.sceneName ? `Lanes in ${choices.sceneName}` : "");
        status.title = choices?.status === "error" ? (choices.error || "") : "";
        status.style.display = status.textContent ? "" : "none";
        retry.style.display = choices?.status === "error" ? "" : "none";
        disclosures.replaceChildren(...view.disclosures.map(text => make("div", text)));
        add.disabled = !editable || !ready;
        add.title = ready ? "Add a Reference lane" : (status.textContent || "Lane names unavailable here.");
        add.style.display = editable ? "" : "none";
        menu.replaceChildren(...(view.addable.length ? view.addable : [{
            label: "No other lanes", disabled: true, reason: "There are no other Reference lanes in this scene.",
        }]).map(entry => {
            const option = make("button", entry.label);
            option.type = "button";
            option.setAttribute("role", "menuitem");
            option.disabled = !!entry.disabled;
            option.title = entry.reason || "Add this Reference lane";
            option.style.cssText = `text-align:left;border:0;border-radius:${RADIUS.r1}px;background:none;`
                + `color:${entry.disabled ? COLORS.textDim : COLORS.text};font:${TYPE.t12}px ${FONT.sans};`
                + `padding:${SPACE.s1}px;cursor:${entry.disabled ? "default" : "pointer"};`;
            if (!entry.disabled) option.addEventListener("click", () => {
                setOpen(false);
                send([...parseLaneSelection(value).laneIndices, entry.laneIndex]);
            });
            // The reason shows inline too: a disabled button shows no tooltip on touch.
            if (entry.disabled && entry.reason) {
                const why = make("div", entry.reason);
                why.style.cssText = `font-size:${TYPE.t11}px;color:${COLORS.textDim};`;
                option.append(why);
            }
            return option;
        }));
    }

    add.addEventListener("click", () => setOpen(!open));

    return {
        el,
        get editing() { return open; },
        get open() { return open; },
        get describeTargets() { return [add]; },
        contains: target => el.contains(target),
        update(nextSpec, nextValue, { canEdit = true, choices: nextChoices = null, retry: nextRetry = () => {} } = {}) {
            spec = nextSpec;
            onRetry = nextRetry;
            // Repaint only on news: a rebuilt list would drop keyboard focus from a Remove.
            const next = [String(nextValue ?? ""), canEdit, nextChoices?.status, nextChoices?.lanes, nextChoices?.sceneName];
            if (painted && next.every((part, index) => part === painted[index])) return;
            painted = next;
            value = next[0];
            choices = nextChoices;
            editable = canEdit;
            if (!editable) setOpen(false);
            paint();
        },
        keydown(e) {
            if (e.key === "Escape" && open) { setOpen(false); add.focus?.(); return true; }
            return false;
        },
        reset(nextValue) {
            value = String(nextValue ?? "");
            painted = null;
            setOpen(false);
            paint();
        },
        flush() {},
        destroy() {
            setOpen(false);
            globalThis.document?.removeEventListener?.("pointerdown", outside, true);
            el.remove();
        },
        // Tests and the panel's rebuild check read the last view.
        get view() { return view; },
        get spec() { return spec; },
    };
}

export function mountDriverLanePicker({ commit, onEditing = () => {} }) {
    const el = make("div");
    el.style.cssText = `display:flex;flex-direction:column;gap:2px;min-width:0;width:100%;`;
    const select = make("select");
    select.style.cssText = S.inputCss + "cursor:pointer;";
    select.setAttribute("aria-label", "Driver lane");
    // Without readable lanes (no scene, a failed read) the lane is typed as a
    // number, 1-based like everywhere it is shown; it stores zero-based.
    const typed = make("input");
    typed.type = "number";
    typed.min = "1";
    typed.step = "1";
    typed.style.cssText = S.numberInputCss(false);
    typed.setAttribute("aria-label", "Driver lane number");
    const status = make("div");
    status.style.cssText = `font-size:${TYPE.t11}px;line-height:1.4;color:${COLORS.textDim};`;
    const retry = retryButton();
    el.append(select, typed, status, retry);
    let value = 0, editing = false, onRetry = () => {};
    retry.addEventListener("click", () => onRetry());

    const toIndex = raw => {
        const number = Number(raw);
        return Number.isSafeInteger(number) && number >= 0 ? number : 0;
    };
    const send = next => {
        const before = value;
        value = next;
        if (value !== before) commit(value, before);
    };
    for (const field of [select, typed]) {
        field.addEventListener("focus", () => { editing = true; onEditing(true); });
        field.addEventListener("blur", () => { editing = false; onEditing(false); });
    }
    select.addEventListener("change", () => send(toIndex(select.value)));
    typed.addEventListener("change", () => {
        const lane = Number(typed.value);
        if (!Number.isSafeInteger(lane) || lane < 1) { typed.value = String(value + 1); return; }
        send(lane - 1);
    });

    function paint(choices, editable) {
        // The last good list stays shown while it is re-read or a refresh fails.
        const held = Array.isArray(choices?.drivers);
        const lanes = held ? choices.drivers : [];
        const fallback = !held && ["error", "unavailable"].includes(choices?.status);
        const options = lanes.map(lane => new Option(lane.name || `Driver ${lane.laneIndex + 1}`, String(lane.laneIndex)));
        if (!lanes.some(lane => lane.laneIndex === value)) {
            // The stored lane is not in this scene (or the lanes are not read yet).
            options.push(new Option(held ? `Missing lane ${value + 1}` : `Driver ${value + 1}`, String(value)));
        }
        select.replaceChildren(...options);
        select.value = String(value);
        select.disabled = !editable || !held;
        select.style.display = fallback ? "none" : "";
        typed.style.display = fallback ? "" : "none";
        typed.disabled = !editable;
        if (document.activeElement !== typed) typed.value = String(value + 1);
        status.textContent = (fallback && choices.status === "error" ? "Couldn't read this scene's Driver lanes. Type a lane number." : "")
            || choicesStatus(choices, "Driver lanes", held)
            || (held && !lanes.some(lane => lane.laneIndex === value) ? `Lane ${value + 1} is not in ${choices.sceneName || "this scene"}.` : "");
        status.title = choices?.status === "error" ? (choices.error || "") : "";
        status.style.display = status.textContent ? "" : "none";
        retry.style.display = choices?.status === "error" ? "" : "none";
    }

    return {
        el,
        get editing() { return editing; },
        get describeTargets() { return [select, typed]; },
        contains: target => el.contains(target),
        update(_spec, nextValue, { canEdit = true, choices = null, retry: nextRetry = () => {} } = {}) {
            onRetry = nextRetry;
            value = toIndex(nextValue);
            paint(choices, canEdit);
        },
        keydown(e) {
            // Enter in the typed lane commits it, as a number field does elsewhere.
            if (e.key === "Enter" && document.activeElement === typed) { typed.blur(); return true; }
            return false;
        },
        reset(nextValue) {
            value = toIndex(nextValue);
            select.value = String(value);
            typed.value = String(value + 1);
        },
        flush() { if (document.activeElement === typed) typed.blur(); },
        destroy() { el.remove(); },
    };
}
