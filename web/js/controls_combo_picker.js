import { EDITOR_COLORS as COLORS, FONT, RADIUS, SPACE, TYPE } from "./editor_theme.js";
// Search becomes useful once a native select exceeds a short screenful.
export const wantsComboPicker = spec => !!spec.options_truncated || (spec.options?.length || 0) > 20;

// `freeText` (the asset-folder picker): the typed text is itself a valid value.
// Enter and leaving the field commit it (normalized by `normalize`), "Use typed
// value" is always offered, and `labels` give values a display name distinct
// from the value (Root for ""). Without it the picker is unchanged: only a
// listed option or, when the preview is capped, an explicit typed value commits.
export function mountComboPicker({ choose, cancel, onLeave, inputCss, freeText = false,
    normalize = value => String(value ?? ""), labels = {} }) {
    const box = document.createElement("div"),
        input = document.createElement("input"),
        list = document.createElement("select"),
        hint = document.createElement("div"),
        typed = document.createElement("button");
    input.type = "search";
    input.autocomplete = "off";
    input.placeholder = "Filter options…";
    input.style.cssText = inputCss;
    list.size = 6;
    list.hidden = true;
    list.style.cssText = inputCss + `max-height:150px;font:${TYPE.t12}px ${FONT.sans};`;
    hint.style.cssText = `font-size:${TYPE.t11}px;color:${COLORS.textSecondary};padding:${SPACE.s1}px;border-radius:${RADIUS.r1}px;`;
    typed.type = "button";
    typed.textContent = "Use typed value";
    typed.hidden = true;
    typed.setAttribute("aria-label", "Use exact typed option");
    typed.style.cssText = `border:1px solid ${COLORS.border};background:${COLORS.panelMuted};color:${COLORS.text};font:${TYPE.t12}px ${FONT.sans};border-radius:${RADIUS.r2}px;padding:${SPACE.s2}px;cursor:pointer;`;
    box.append(input, list, hint, typed);
    let optionDetails = {};
    let options = [],
        truncated = false,
        query = "",
        current = "",
        open = false;
    const labelOf = o => (Object.hasOwn(labels, String(o)) ? labels[String(o)] : String(o));
    function paint() {
        const matches = options.filter(o => [String(o), labelOf(o)].some(text => text.toLowerCase().includes(query.toLowerCase())));
        list.replaceChildren(...matches.map(o => { const option = new Option(labelOf(o), String(o)); option.disabled = !!optionDetails[o]?.disabled; option.title = optionDetails[o]?.title || ""; return option; }));
        // A free-text picker commits what was typed, so it highlights only an exact
        // match: a highlighted near match would read as the choice Enter makes.
        if (freeText && !matches.some(o => String(o) === input.value)) list.selectedIndex = -1;
        else list.value = matches.some(o => String(o) === input.value) ? input.value : String(matches[0] ?? "");
        list.hidden = !open || !matches.length;
        typed.hidden = !(truncated || (freeText && open));
        hint.textContent = truncated
            ? "Preview limited. Choose a match or use an exact typed value."
            : open && !matches.length
              ? freeText ? "No matching folder. Enter keeps what you typed." : "No matching options."
              : "";
    }
    // A free-text picker's typed text, committed when it differs from the value.
    function commitTyped() {
        if (!freeText || input.value === current) return false;
        // Typing a label ("Root") means its value, never a folder of that name.
        const typedText = input.value.trim().toLowerCase();
        const labelled = Object.keys(labels).find(value => String(labels[value]).toLowerCase() === typedText);
        select(labelled !== undefined ? labelled : normalize(input.value));
        return true;
    }
    function select(value) {
        const selected = String(value);
        if (optionDetails[selected]?.disabled) return;
        input.value = current = selected;
        query = "";
        open = false;
        choose(selected);
        paint();
    }
    const focus = () => {
        open = true;
        query = input.value === current ? "" : input.value;
        paint();
    };
    const filter = () => {
        query = input.value;
        open = true;
        paint();
    };
    const change = () => select(list.value);
    const pointer = e => {
        e.preventDefault();
        if (e.target.tagName === "OPTION") {
            select(e.target.value);
            input.blur();
        }
    };
    const keepFocus = e => e.preventDefault(),
        useTyped = () => {
            select(normalize(input.value));
            if (document.activeElement === typed) typed.blur();
            else input.blur();
        };
    const leave = e => {
        if (!box.contains(e.relatedTarget)) {
            if (commitTyped()) { onLeave?.(); return; }
            input.value = current;
            query = "";
            open = false;
            paint();
            onLeave?.();
        }
    };
    input.addEventListener("focus", focus);
    input.addEventListener("input", filter);
    list.addEventListener("mousedown", pointer);
    list.addEventListener("change", change);
    typed.addEventListener("mousedown", keepFocus);
    typed.addEventListener("click", useTyped);
    box.addEventListener("focusout", leave);
    return {
        box,
        input,
        list,
        freeText,
        commitTyped,
        contains: el => box.contains(el),
        update(spec) {
            options = spec.options || [];
            optionDetails = spec.optionDetails || {};
            truncated = !!spec.options_truncated;
            input.disabled = list.disabled = typed.disabled = !["ok", "unlinked"].includes(spec.status);
            paint();
        },
        setValue(value) {
            current = String(value ?? "");
            input.value = current;
            query = "";
            open = false;
            paint();
        },
        close() {
            input.value = current;
            query = "";
            open = false;
            paint();
        },
        keydown(e) {
            if (e.key === "Escape") {
                input.value = current;
                open = false;
                paint();
                cancel();
                return true;
            }
            if (e.key === "Enter") {
                if (document.activeElement === typed) {
                    useTyped();
                    return true;
                }
                if (freeText) {
                    if (!commitTyped()) { open = false; paint(); }
                    input.blur();
                    return true;
                }
                if (input.value === current) {
                    open = false;
                    paint();
                } else if (options.some(o => String(o) === input.value)) select(input.value);
                else if (list.options.length) select(list.value);
                else if (truncated) select(input.value);
                input.blur();
                return true;
            }
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                open = true;
                paint();
                const opts = [...list.options];
                let index = opts.findIndex(o => o.value === list.value);
                index = Math.max(0, Math.min(opts.length - 1, index + (e.key === "ArrowDown" ? 1 : -1)));
                if (opts[index]) {
                    input.value = list.value = opts[index].value;
                }
                return true;
            }
            return false;
        },
        destroy() {
            input.removeEventListener("focus", focus);
            input.removeEventListener("input", filter);
            list.removeEventListener("mousedown", pointer);
            list.removeEventListener("change", change);
            typed.removeEventListener("mousedown", keepFocus);
            typed.removeEventListener("click", useTyped);
            box.removeEventListener("focusout", leave);
            box.remove();
        },
    };
}
