// What the Controls panel shows, and where (SDK v2 Phase 4). DOM-free: the
// panel (editor_controls_panel.js) owns rows, listeners and writes.
//
// Search and Advanced only change what is displayed. Every row of the view is
// still built and reconciled, so a save in flight on a row a query hides keeps
// its token and its `Not saved:` line.

export const EDITOR_DISCLOSURE = "editor";
// Disclosure memory keys: one namespace, so a group named "editor" cannot
// collide with the Editor section.
export const disclosureKey = group => (group === null ? EDITOR_DISCLOSURE : `group:${group}`);

const text = value => String(value ?? "").toLowerCase();

/** True when a binding or mirror row matches the search: its name, group, choices or value. */
export function matchEntry(spec, query) {
    const q = String(query ?? "").trim().toLowerCase();
    if (!q) return true;
    if (spec.source === "note") return false; // notes are not searched
    const haystack = [spec.label, spec.group, spec.value, ...(Array.isArray(spec.options) ? spec.options : [])];
    if (spec.kind === "BOOLEAN") haystack.push(spec.value ? "on" : "off");
    return haystack.some(value => text(value).includes(q));
}

const isAdvanced = spec => spec.source === "binding" && spec.advanced === true;

/** Whether a row is displayed. Manage shows everything; a query shows matches, Advanced included. */
export function entryVisible(spec, { query = "", showAdvanced = false, manage = false } = {}) {
    if (manage) return true;
    if (spec.hidden) return false; // hidden in the Sonder App: not part of the menu
    if (String(query ?? "").trim()) return matchEntry(spec, query);
    return !isAdvanced(spec) || showAdvanced;
}

/** Advanced Controls the panel is not showing, and how many of those differ from the workflow (Sonder App). */
export function hiddenAdvanced(specs, { showAdvanced = false } = {}) {
    if (showAdvanced) return { count: 0, changed: 0 };
    const hidden = (specs || []).filter(spec => isAdvanced(spec) && !spec.hidden);
    return { count: hidden.length, changed: hidden.filter(spec => spec.overridden === true).length };
}

export const hasAdvanced = specs => (specs || []).some(spec => isAdvanced(spec) && !spec.hidden);

/**
 * The workflow groups in display order. The server's `group_order` is the
 * authority; a view without it (an older mounted publication) falls back to
 * first appearance in the sorted rows, which is how groups were ordered before.
 */
export function groupPlan(view, sortedSpecs) {
    const order = Array.isArray(view?.group_order) ? view.group_order.filter(name => typeof name === "string" && name) : [];
    for (const spec of sortedSpecs || []) {
        if (spec.source === "binding" && spec.section === "editor") continue; // shown in Editor, not a group
        const group = typeof spec.group === "string" ? spec.group : "";
        if (group && !order.includes(group)) order.push(group);
    }
    return order;
}
