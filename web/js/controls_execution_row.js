// A node switch's row editor in the Controls panel: Enabled / Muted / Bypassed
// for a root node, a subgraph instance or a canvas group (plans/output-views-phase-1a.md).
//
// Module-host contract, the same as controls_rows_editor.js: the panel owns the
// value, writes and refusals and calls `update` and `destroy`; this module owns
// its DOM and listeners. A group whose nodes differ shows Mixed: no segment is
// selected, and choosing one sets every node.
import { smallButton } from "./controls_manage.js";
import { EDITOR_COLORS as COLORS, SPACE, TYPE, setButtonVariant } from "./editor_theme.js";

export const EXECUTION_MODES = ["enabled", "muted", "bypassed"];
export const MODE_LABELS = { enabled: "Enabled", muted: "Muted", bypassed: "Bypassed" };

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

export function mountExecutionRow({ commit, onEditing = () => {} }) {
    const el = make("div");
    el.setAttribute("role", "radiogroup");
    el.style.cssText = `display:flex;flex-direction:column;gap:2px;min-width:0;width:100%;align-items:flex-end;`;
    const segments = make("div");
    segments.style.cssText = `display:inline-flex;gap:${SPACE.s1}px;flex-wrap:wrap;justify-content:flex-end;`;
    const mixed = make("div");
    mixed.style.cssText = `font-size:${TYPE.t10}px;color:${COLORS.textDim};`;
    el.append(segments, mixed);
    let value = null, editable = false;
    const buttons = EXECUTION_MODES.map(mode => {
        const button = smallButton(MODE_LABELS[mode]);
        button.dataset.executionMode = mode;
        button.setAttribute("role", "radio");
        button.addEventListener("focus", () => onEditing(true));
        button.addEventListener("blur", () => onEditing(false));
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
            button.setAttribute("aria-checked", String(chosen));
            button.disabled = !editable;
        }
    }

    return {
        el,
        update(spec, next, { canEdit = true } = {}) {
            value = next ?? spec?.mode ?? null;
            editable = !!canEdit;
            mixed.textContent = value === "mixed" ? modeSummary(spec, value) : "";
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
