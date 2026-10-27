// A node switch's row editor in the Controls panel: Enabled / Muted / Bypassed
// for a root node, a subgraph instance or a canvas group (plans/output-views-phase-1a.md).
//
// Module-host contract, the same as controls_rows_editor.js: the panel owns the
// value, writes and refusals and calls `update` and `destroy`; this module owns
// its DOM and listeners. A group whose nodes differ shows Mixed: no segment is
// selected, and choosing one sets every node.
import { smallButton } from "./controls_manage.js";
import { setText } from "./controls_dom_order.js";
import { EDITOR_COLORS as COLORS, SPACE, TYPE, setButtonVariant } from "./editor_theme.js";

export const EXECUTION_MODES = ["enabled", "muted", "bypassed"];
export const MODE_LABELS = { enabled: "Enabled", muted: "Muted", bypassed: "Bypassed" };
// The three choices share one row in equal thirds while each third holds the
// widest label ("Bypassed", 11px with its padding, about 70px); narrower, each
// takes the whole width, stacked. Never two plus one. The basis is negative
// above the threshold (so the three grow equally) and huge below it (so each
// wraps alone). The panel's zoom scales the labels and the threshold together.
const SEGMENT_MIN = 76;
const ROW_THRESHOLD = 3 * SEGMENT_MIN + 2 * SPACE.s1;
export const SEGMENT_FLEX = `1 1 calc((${ROW_THRESHOLD}px - 100%) * 999)`;

const make = (tag, text) => {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    return el;
};

/** `Muted`, or `Mixed (2 of 5 off)` for a group whose nodes differ. */
export function modeSummary(spec, value = spec?.mode) {
    if (value === "mixed") {
        const members = spec?.members || [];
        const off = members.filter(member => member.mode !== "enabled").length;
        return `Mixed (${off} of ${members.length} off)`;
    }
    return MODE_LABELS[value] || "";
}

/** What the switch acts on, for the row's status line. */
export function switchScope(spec) {
    if (spec?.kind === "group") {
        const count = spec.member_count ?? (spec.members || []).length;
        return `Group · ${count} ${count === 1 ? "node" : "nodes"}`;
    }
    return spec?.kind === "instance" ? "Subgraph" : "Node";
}

// A choice commits at once, so there is no draft to protect: a focused button
// never holds the row back from showing a mode set elsewhere (a preset, Undo,
// another tab). Unlike the other row editors it takes no onEditing.
export function mountExecutionRow({ commit }) {
    const el = make("div");
    el.setAttribute("role", "radiogroup");
    el.style.cssText = `display:flex;flex-direction:column;gap:2px;min-width:0;width:100%;align-items:stretch;`;
    const segments = make("div");
    // A definite width: the thirds' basis is measured against it.
    segments.style.cssText = `display:flex;gap:${SPACE.s1}px;flex-wrap:wrap;width:100%;min-width:0;`;
    const mixed = make("div");
    mixed.style.cssText = `font-size:${TYPE.t10}px;color:${COLORS.textDim};`;
    el.append(segments, mixed);
    let value = null, editable = false;
    const buttons = EXECUTION_MODES.map(mode => {
        const button = smallButton(MODE_LABELS[mode]);
        button.dataset.executionMode = mode;
        button.setAttribute("role", "radio");
        button.addEventListener("click", () => {
            if (!editable || mode === value) return;
            const before = value;
            value = mode;
            paint();
            commit(mode, before);
        });
        segments.append(button);
        return button;
    });

    function paint() {
        for (const button of buttons) {
            const chosen = button.dataset.executionMode === value;
            setButtonVariant(button, chosen ? "active" : "secondary", { padding: `2px ${SPACE.s2}px`, fontSize: `${TYPE.t11}px` });
            // After the variant, which rewrites the whole cssText.
            Object.assign(button.style, { flex: SEGMENT_FLEX, minWidth: "0" });
            button.setAttribute("aria-checked", String(chosen));
            button.disabled = !editable;
        }
    }

    return {
        el,
        update(spec, next, { canEdit = true } = {}) {
            value = next ?? spec?.mode ?? null;
            editable = !!canEdit;
            setText(mixed, value === "mixed" ? modeSummary(spec, value) : "");
            mixed.style.display = mixed.textContent ? "" : "none";
            el.setAttribute("aria-label", spec?.label || "Node switch");
            paint();
        },
        // A refused switch shows the stored mode again.
        reset(next) { value = next ?? null; paint(); },
        contains: node => el.contains(node),
        // Buttons keep their native keys; nothing is held to flush, since a
        // choice commits at once.
        keydown: () => false,
        flush() {},
        destroy() { el.remove(); },
    };
}
