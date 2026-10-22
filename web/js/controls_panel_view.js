// What the Controls panel shows, and where (SDK v2 Phase 4). DOM-free: the
// panel (editor_controls_panel.js) owns rows, listeners and writes.
//
// Search, Advanced and Show notes only change what is displayed. Every row of the view is
// still built and reconciled, so a save in flight on a row a query hides keeps
// its token and its `Not saved:` line.

export const EDITOR_DISCLOSURE = "editor";
// Disclosure memory keys: one namespace, so a group named "editor" cannot
// collide with the Editor section.
export const disclosureKey = group => (group === null ? EDITOR_DISCLOSURE : `group:${group}`);
// A titled note's open state, in the same namespace.
export const noteDisclosureKey = noteId => `note:${noteId}`;

const text = value => String(value ?? "").toLowerCase();

/** True when a binding or mirror row matches the search: its name, group, choices or value. */
export function matchEntry(spec, query) {
    const q = String(query ?? "").trim().toLowerCase();
    if (!q) return true;
    if (spec.source === "note") return false; // notes are not searched
    const haystack = [spec.label, spec.group, spec.kind === "ROWS" ? "" : spec.value, ...(Array.isArray(spec.options) ? spec.options : [])];
    // A row list matches its rows' values and its fields' choices.
    if (spec.kind === "ROWS") {
        for (const row of Array.isArray(spec.value) ? spec.value : []) haystack.push(...Object.values(row || {}).filter(v => typeof v === "string"));
        for (const field of spec.fields || []) if (Array.isArray(field?.options)) haystack.push(...field.options);
    }
    if (spec.kind === "BOOLEAN") haystack.push(spec.value ? "on" : "off");
    return haystack.some(value => text(value).includes(q));
}

// A setting or a node switch may be Advanced; a note never is.
const isAdvanced = spec => (spec.source === "binding" || spec.source === "execution") && spec.advanced === true;

/** Whether a row is displayed. Manage shows everything; a query shows matches, Advanced included. */
export function entryVisible(spec, { query = "", showAdvanced = false, showNotes = true, manage = false } = {}) {
    if (manage) return true;
    if (spec.hidden) return false; // hidden in the Sonder App: not part of the menu
    if (String(query ?? "").trim()) return matchEntry(spec, query);
    if (spec.source === "note") return showNotes;
    return !isAdvanced(spec) || showAdvanced;
}

/** Whether a displayed Control shows its own note: the same precedence as Advanced. */
export const noteShown = ({ query = "", showNotes = true, manage = false } = {}) =>
    manage || showNotes || !!String(query ?? "").trim();

// Untrimmed, as the panel decides whether a note takes space: a blank-line note still shows.
const hasText = value => !!String(value ?? "");
// The notes Show notes governs, counting a Control's note only while its row is displayed.
const governedNotes = (specs, showAdvanced) => (specs || []).filter(spec => !spec.hidden && (spec.source === "note"
    ? hasText(spec.text)
    : hasText(spec.note) && (!isAdvanced(spec) || showAdvanced)));

/** Notes Show notes off is hiding now: what turning it on reveals. */
export function hiddenNotes(specs, { showNotes = true, showAdvanced = false } = {}) {
    return showNotes ? 0 : governedNotes(specs, showAdvanced).length;
}

export const hasNotes = (specs, { showAdvanced = false } = {}) => governedNotes(specs, showAdvanced).length > 0;

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
