// The pane picker: a toggled menu under a pane's source button. Sections:
// Editor (Composite, Guides, each Driver lane), Views (with their status) and
// Add output… (output nodes without a View; muted ones say why). It closes
// from its trigger, on an outside pointer and on Escape (keyboard ownership,
// OVERLAY). Owns its DOM and listeners; returns close().
import { register, PRIORITY } from "./keyboard_ownership.js";
import { EDITOR_COLORS as COLORS, FONT, SPACE, TYPE, chromeMenuCss } from "./editor_theme.js";

let serial = 0;

const make = (tag, text, css = "") => {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (css) el.style.cssText = css;
    return el;
};

function heading(text) {
    return make("div", text, `padding:${SPACE.s1}px ${SPACE.s3}px 2px;color:${COLORS.textDim};font:${TYPE.fwBold} ${TYPE.t10}px ${FONT.sans};`
        + "text-transform:uppercase;letter-spacing:0.06em;");
}

function row(label, detail, { selected = false, onPick, title = "" } = {}) {
    const button = make("button", undefined, `display:flex;flex-direction:column;align-items:flex-start;gap:1px;width:100%;box-sizing:border-box;`
        + `padding:${SPACE.s1 + 1}px ${SPACE.s3}px;border:0;background:${selected ? COLORS.accentSoft : "transparent"};cursor:pointer;`
        + `color:${COLORS.text};font:${TYPE.t11}px ${FONT.sans};text-align:left;`);
    button.type = "button";
    if (title) button.title = title;
    button.setAttribute("role", "menuitem");
    if (selected) button.setAttribute("aria-current", "true");
    const name = make("span", label, "overflow-wrap:anywhere;");
    button.append(name);
    if (detail) button.append(make("span", detail, `color:${COLORS.textDim};font-size:${TYPE.t10}px;overflow-wrap:anywhere;`));
    button.addEventListener("mouseenter", () => { if (!selected) button.style.background = COLORS.panelRaisedHover; });
    button.addEventListener("mouseleave", () => { if (!selected) button.style.background = "transparent"; });
    button.addEventListener("click", event => { event.stopPropagation(); onPick(); });
    return button;
}

// `sections` comes from views_model.pickerSections; `current` is the pane's
// source. onPick({source}) or onPick({candidate}).
export function openPanePicker({ anchor, sections, current, onPick, onClose = () => {} }) {
    const menu = make("div", undefined, chromeMenuCss(220) + "max-height:min(70vh,520px);overflow:auto;");
    menu.setAttribute("role", "menu");
    menu.dataset.sonderPanePicker = "true";
    let closed = false;
    const close = () => {
        if (closed) return;
        closed = true;
        unregister();
        document.removeEventListener("pointerdown", outside, true);
        menu.remove();
        onClose();
    };
    const pick = value => { close(); onPick(value); };
    const isCurrent = source => current?.kind === source.kind && current?.index === source.index && current?.viewId === source.viewId;

    // Filled again in place when the Controls view moves on while it is open.
    const fill = next => {
        menu.replaceChildren();
        menu.append(heading("Editor"));
        for (const item of next.editor) menu.append(row(item.label, "", { selected: isCurrent(item.source), onPick: () => pick({ source: item.source }) }));
        menu.append(heading("Views"));
        if (next.viewsError) menu.append(make("div", next.viewsError, `padding:2px ${SPACE.s3}px ${SPACE.s1}px;color:${COLORS.warningText};font-size:${TYPE.t11}px;`));
        else if (!next.views.length) menu.append(make("div", "No Views yet. Add an output below.", `padding:2px ${SPACE.s3}px ${SPACE.s1}px;color:${COLORS.textDim};font-size:${TYPE.t11}px;`));
        for (const item of next.views) {
            menu.append(row(item.label, item.detail, { selected: isCurrent(item.source), title: item.reason, onPick: () => pick({ source: item.source }) }));
        }
        menu.append(heading("Add output…"));
        // The Views are off (a Relay problem, another version): no output can be added either.
        if (next.viewsError) menu.append(make("div", "Outputs can be added once the problem above is fixed.", `padding:2px ${SPACE.s3}px ${SPACE.s1}px;color:${COLORS.textDim};font-size:${TYPE.t11}px;`));
        else if (!next.viewsReady) menu.append(make("div", "Reading the workflow's outputs…", `padding:2px ${SPACE.s3}px ${SPACE.s1}px;color:${COLORS.textDim};font-size:${TYPE.t11}px;`));
        else if (!next.add.length) menu.append(make("div", "Every output node already has a View.", `padding:2px ${SPACE.s3}px ${SPACE.s1}px;color:${COLORS.textDim};font-size:${TYPE.t11}px;`));
        if (!next.viewsError) for (const item of next.add) menu.append(row(item.label, item.detail, { title: "Show this output's results here, as a View", onPick: () => pick({ candidate: item.candidate }) }));
    };
    fill(sections);
    document.body.append(menu);
    const rect = anchor.getBoundingClientRect();
    const width = menu.offsetWidth || 220, height = menu.offsetHeight || 200;
    menu.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))}px`;
    menu.style.top = `${rect.bottom + 4 + height > window.innerHeight ? Math.max(8, rect.top - height - 4) : rect.bottom + 4}px`;

    const outside = event => {
        if (menu.contains(event.target) || anchor.contains(event.target)) return;
        close();
    };
    document.addEventListener("pointerdown", outside, true);
    const unregister = register({
        id: `view-pane-picker:${++serial}`,
        priority: PRIORITY.OVERLAY,
        keydown: event => {
            if (event.key !== "Escape") return false;
            close();
            return true;
        },
    });
    return { close, el: menu, update: next => { if (!closed) fill(next); } };
}
