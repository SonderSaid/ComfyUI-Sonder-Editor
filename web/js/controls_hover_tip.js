// One hover and keyboard-focus tip for the Controls panel and its Modes table:
// a node's help on a setting, a choice's short description
// (plans/live-workflow-phase-4-controls-modes.md D6).
//
// Module-host contract: the caller owns the text (`getText`, read on every
// show) and the anchor; this module owns the tip elements and listeners and
// returns cleanup. Text only, never HTML.
//
// Each anchor gets its own tip, inside it and absolutely placed, so the panel's
// zoom and sticky header can't misplace it. The tip is never read by assistive
// technology (aria-hidden): the caller describes the element itself.
import { register as registerKeys, PRIORITY } from "./keyboard_ownership.js";
import { setText } from "./controls_dom_order.js";
import { EDITOR_COLORS as COLORS, RADIUS, SPACE, TYPE } from "./editor_theme.js";

let serial = 0;
const TIP_MAX_HEIGHT = 240;
const editable = el => !!el && (["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName) || el.isContentEditable === true);
// Where Escape edits text: a draft there owns it, never the tip.
const typesText = el => !!el && (el.tagName === "TEXTAREA" || el.isContentEditable === true
    || (el.tagName === "INPUT" && !["checkbox", "radio", "button", "submit", "reset", "range", "color"].includes(el.type)));
// Keyboard focus only, never the focus a press gives. Chromium matches
// :focus-visible on a mouse click into a text field, so a press on the field
// (or its label) just before the focus decides; :focus-visible then rules out
// a clicked button. A press elsewhere never hides a later Tab's tip. One
// listener for the page's lifetime, like the browser's own heuristic.
const PRESS_FOCUS_MS = 1000;
let pressed = null, listening = false;
function listenForPresses() {
    if (listening) return;
    listening = true;
    globalThis.window?.addEventListener?.("pointerdown", e => { pressed = { at: Date.now(), target: e?.target || null }; }, true);
}
function pressFocused(el) {
    if (!pressed || Date.now() - pressed.at >= PRESS_FOCUS_MS) return false;
    const target = pressed.target;
    if (!target) return true;
    return target === el || !!el.contains?.(target) || !!target.contains?.(el) || target.closest?.("label")?.control === el;
}
function focusVisible(el) {
    if (pressFocused(el)) return false;
    try { return typeof el.matches !== "function" || el.matches(":focus-visible"); } catch { return true; }
}
// Keys that move focus or leave the field show the next tip instead of hiding.
const QUIET_KEYS = new Set(["Tab", "Shift", "Control", "Alt", "Meta", "Escape"]);

export function createHoverTip(container, { register = registerKeys, mark = "controlsTip" } = {}) {
    listenForPresses();
    const tips = new WeakMap(); // anchor -> its tip element
    const uses = new WeakMap(); // fixed anchor -> attachments still using its tip
    const made = new Set();
    let shown = null, destroyed = false;

    function tipFor(anchor) {
        let tip = tips.get(anchor);
        if (tip) return tip;
        tip = document.createElement("div");
        tip.setAttribute("aria-hidden", "true");
        tip.dataset[mark] = "true";
        // Wraps at the panel's width less a gutter (place() sets it), keeps
        // the help's own line breaks, and scrolls nothing: a pointer never
        // reaches it, so a long text is cut at the bottom instead.
        tip.style.cssText = `position:absolute;display:none;z-index:2;pointer-events:none;box-sizing:border-box;`
            + `width:max-content;max-height:${TIP_MAX_HEIGHT}px;overflow:hidden;white-space:pre-line;overflow-wrap:anywhere;`
            + `padding:${SPACE.s1}px ${SPACE.s2}px;border:1px solid ${COLORS.border};border-radius:${RADIUS.r2}px;`
            + `background:${COLORS.panelRaised};color:${COLORS.text};font-size:${TYPE.t11}px;line-height:1.4;`;
        anchor.append(tip);
        tips.set(anchor, tip);
        made.add(tip);
        return tip;
    }

    // Below `el`, inside `anchor`, kept within the container's width. Rects are
    // drawn size and offsets layout size; their ratio is the panel's zoom.
    function place(tip, el, anchor) {
        const a = anchor.getBoundingClientRect?.(), r = el.getBoundingClientRect?.(), c = container.getBoundingClientRect?.();
        if (!a || !r || !a.width) {
            tip.style.top = `${(el.offsetTop || 0) + (el.offsetHeight || 0) + 2}px`;
            const room = Math.max(0, (anchor.clientWidth || 0) - (tip.offsetWidth || 0) - SPACE.s2);
            tip.style.left = `${Math.min(el.offsetLeft || 0, room)}px`;
            return;
        }
        const scale = anchor.offsetWidth ? a.width / anchor.offsetWidth : 1;
        const local = px => px / (scale || 1);
        const left0 = a.left + (anchor.clientLeft || 0) * scale, top0 = a.top + (anchor.clientTop || 0) * scale;
        const inner = c ? { left: c.left + SPACE.s2 * scale, right: c.right - SPACE.s2 * scale } : { left: a.left, right: a.right };
        tip.style.maxWidth = `${Math.max(80, local(inner.right - inner.left))}px`;
        tip.style.top = `${local(r.bottom - top0) + 2}px`;
        const width = tip.offsetWidth || 0;
        const min = local(inner.left - left0), max = local(inner.right - left0) - width;
        tip.style.left = `${Math.max(min, Math.min(local(r.left - left0), max))}px`;
    }

    function hide() {
        if (!shown) return;
        shown.tip.style.display = "none";
        shown = null;
    }
    // The last attachment of a fixed anchor gone: its tip goes too, so a
    // dropped row is never kept alive by its tip.
    function release(anchor) {
        const left = (uses.get(anchor) || 1) - 1;
        if (left > 0) { uses.set(anchor, left); return; }
        uses.delete(anchor);
        const tip = tips.get(anchor);
        if (!tip) return;
        if (shown?.tip === tip) hide();
        tip.remove();
        tips.delete(anchor);
        made.delete(tip);
    }
    // `via`: what opened it. A hover tip closes when the pointer leaves, even
    // if a click focused its element; a keyboard-focus tip stays until blur.
    function show(entry, via = shown?.entry === entry ? shown.via : "hover") {
        if (destroyed || entry.suppressed?.()) { if (shown?.entry === entry) hide(); return; }
        const text = String(entry.getText() || "");
        if (!text) { if (shown?.entry === entry) hide(); return; }
        const anchor = entry.anchor(), tip = tipFor(anchor);
        if (shown && shown.tip !== tip) hide();
        setText(tip, text);
        tip.style.display = "block";
        place(tip, entry.el, anchor);
        shown = { entry, tip, via };
    }

    const unregister = register({ id: `controls-hover-tip:${++serial}`, priority: PRIORITY.OVERLAY, keydown: e => {
        if (!shown || e.key !== "Escape" || e.isComposing) return false;
        // A tip left open under a hidden panel (no mouseleave came): drop it.
        if (container.offsetParent === null) { hide(); return false; }
        const active = document.activeElement;
        // A text field's Escape is its draft's (it reverts and blurs, which
        // hides the tip): only nothing that edits, or the tip's own button,
        // select or checkbox, hands Escape to the tip.
        if (editable(active) && !(active === shown.entry.el && !typesText(active))) return false;
        hide();
        return true;
    } });

    return {
        // `getText` is read on every show, so a repaint never needs a reattach.
        // `focusOnly`: the keyboard focus shows it, hovering doesn't (a value
        // field's help; its label carries the hover). Returns detach.
        attach(el, getText, { anchor, focusOnly = false, suppressed = null } = {}) {
            if (destroyed || !el) return () => {};
            const entry = { el, getText, suppressed, anchor: typeof anchor === "function" ? anchor : () => anchor || el.parentElement };
            // A fixed anchor gets its tip now, so it is in place before any show.
            const fixed = anchor && typeof anchor !== "function" ? anchor : null;
            if (fixed) { tipFor(fixed); uses.set(fixed, (uses.get(fixed) || 0) + 1); }
            const mine = () => shown?.entry === entry;
            const listeners = [
                ["focus", () => { if (focusVisible(el)) show(entry, "focus"); }],
                ["blur", () => { if (mine()) hide(); }],
                // Typing, or a key that opens a list, is using the field, not
                // reading its help.
                ["input", () => { if (mine()) hide(); }],
                ["keydown", e => { if (mine() && !QUIET_KEYS.has(e?.key)) hide(); }],
            ];
            if (!focusOnly) {
                listeners.push(["mouseenter", () => show(entry, "hover")],
                    ["mouseleave", () => { if (mine() && (shown.via === "hover" || document.activeElement !== el)) hide(); }]);
            }
            for (const [type, fn] of listeners) el.addEventListener(type, fn);
            let attached = true;
            return () => {
                if (!attached) return;
                attached = false;
                for (const [type, fn] of listeners) el.removeEventListener?.(type, fn);
                if (mine()) hide();
                if (fixed && !destroyed) release(fixed);
            };
        },
        // A repaint changed the open tip's text or position.
        refresh() { if (shown) show(shown.entry, shown.via); },
        hide,
        destroy() {
            destroyed = true;
            hide();
            unregister();
            for (const tip of made) tip.remove();
            made.clear();
        },
    };
}
