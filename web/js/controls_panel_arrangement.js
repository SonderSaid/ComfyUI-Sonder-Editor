import { planPanelMove, comparePanelRows } from "./controls_panel_layout.js";
import { groupDropChanges } from "./controls_panel_groups.js";
import { EDITOR_COLORS as COLORS } from "./editor_theme.js";

const ancestor = (el, test, root) => {
    for (let n = el; n && n !== root; n = n.parentElement) if (test(n)) return n;
    return null;
};
let nextDrag = 0;
// Rows drag by their grip into a position or group; group headers drag by
// theirs to reorder groups (Manage only: the panel shows grips only there).
export function mountPanelArrangement(container, { getEntries, getGroups = () => [], isBusy, apply, notify }) {
    const mime = "application/x-sonder-controls-" + ++nextDrag;
    let moving = null,
        marked = null;
    const stamp = () => JSON.stringify(getEntries().map(d => [d.base, d.order, d.group, d.label]));
    function clear() {
        if (marked) marked.style.outline = "";
        marked = null;
    }
    function finish() {
        clear();
        if (moving) moving.el.style.opacity = "";
        moving = null;
    }
    // A group drop lands before the header it is over, or after it in its lower half.
    function groupDestination(e) {
        const el = ancestor(e.target, n => n.dataset?.controlsGroup !== undefined && n.dataset.controlsSection !== "editor", container);
        const name = el?.dataset.controlsGroup;
        if (!el || name === moving.group) return null;
        const rest = getGroups().filter(group => group !== moving.group);
        const rect = el.getBoundingClientRect?.();
        const after = rect && e.clientY > rect.top + Math.min(rect.height, 32) / 2;
        const index = rest.indexOf(name);
        if (index < 0) return null;
        return { el, before: after ? rest[index + 1] ?? null : name };
    }
    function destination(e) {
        if (moving?.group !== undefined) return groupDestination(e);
        const row = ancestor(e.target, n => n.dataset?.panelBase, container);
        const entries = getEntries();
        if (row) {
            const d = entries.find(d => d.base === row.dataset.panelBase);
            if (!d || d.base === moving?.base) return null;
            const rect = row.getBoundingClientRect?.(),
                after = rect && e.clientY > rect.top + rect.height / 2;
            const list = entries
                .filter(x => (x.group || "") === (d.group || "") && x.base !== moving.base)
                .sort(comparePanelRows);
            return {
                el: row,
                group: d.group || "",
                before: after ? (list[list.findIndex(x => x.base === d.base) + 1]?.base ?? null) : d.base,
            };
        }
        const zone = ancestor(e.target, n => n.dataset?.panelDropGroup !== undefined, container);
        return zone ? { el: zone, group: zone.dataset.panelDropGroup, before: null } : null;
    }
    const start = e => {
        const groupGrip = ancestor(e.target, n => n.dataset?.panelGroupHandle !== undefined, container);
        const grip = groupGrip || ancestor(e.target, n => n.dataset?.panelDragHandle === "true", container);
        if (!grip) return;
        if (isBusy()) {
            e.preventDefault();
            notify("Finish the current edit or save before arranging the panel.");
            return;
        }
        if (groupGrip) {
            const section = ancestor(groupGrip, n => n.dataset?.controlsGroup !== undefined, container);
            const name = groupGrip.dataset.panelGroupHandle;
            moving = { group: name, base: `group:${name}`, el: section || groupGrip, stamp: JSON.stringify(getGroups()) };
            e.dataTransfer.setData(mime, moving.base);
            e.dataTransfer.effectAllowed = "move";
            moving.el.style.opacity = "0.5";
            return;
        }
        const el = ancestor(grip, n => n.dataset?.panelBase, container);
        if (!el) return;
        moving = { base: el.dataset.panelBase, el, stamp: stamp() };
        e.dataTransfer.setData(mime, moving.base);
        e.dataTransfer.effectAllowed = "move";
        el.style.opacity = "0.5";
    };
    const over = e => {
        if (!moving) return;
        const dest = destination(e);
        if (!dest) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        clear();
        marked = dest.el;
        marked.style.outline = `1px solid ${COLORS.accent}`;
    };
    const drop = e => {
        if (!moving || e.dataTransfer.getData(mime) !== moving.base) return;
        const group = moving.group;
        const changed = moving.stamp !== (group !== undefined ? JSON.stringify(getGroups()) : stamp()),
            dest = destination(e),
            base = moving.base;
        finish();
        if (!dest) return;
        e.preventDefault();
        if (changed || isBusy()) {
            notify("The panel changed during this drag. Try again.");
            return;
        }
        if (group !== undefined) {
            const changes = groupDropChanges(getGroups(), group, dest.before);
            if (changes) void apply(changes);
            return;
        }
        try {
            const changes = planPanelMove(getEntries(), base, dest.group, dest.before);
            if (Object.keys(changes).length) void apply(changes);
        } catch (error) {
            notify(error.message);
        }
    };
    container.addEventListener("dragstart", start);
    container.addEventListener("dragover", over);
    container.addEventListener("drop", drop);
    container.addEventListener("dragend", finish);
    return {
        destroy() {
            finish();
            container.removeEventListener("dragstart", start);
            container.removeEventListener("dragover", over);
            container.removeEventListener("drop", drop);
            container.removeEventListener("dragend", finish);
        },
    };
}
