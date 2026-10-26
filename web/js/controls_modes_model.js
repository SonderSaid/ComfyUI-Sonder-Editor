// Controls Modes: which choice each category shows, derived from the view
// (plans/controls-modes-phase-b.md). No DOM and no host: the panel renders
// what this returns, and the server's preset `state` is the only match rule.

const unusable = row => row.state === "unavailable" || row.state === "refused";

/**
 * Categories to draw and the choices in none of them. A category whose
 * choices all name no preset row is not drawn. Several matching choices are
 * ambiguous: nothing is picked for the author.
 */
export function deriveModes(view) {
    const presets = Array.isArray(view?.presets) ? view.presets : [];
    const categories = Array.isArray(view?.preset_categories) ? view.preset_categories : [];
    const byId = new Map(presets.map(row => [row.id, row]));
    const claimed = new Set();
    const rows = [];
    for (const category of categories) {
        const choices = (Array.isArray(category.choices) ? category.choices : [])
            .filter(id => byId.has(id) && !claimed.has(id)).map(id => byId.get(id));
        for (const row of choices) claimed.add(row.id);
        if (!choices.length) continue;
        const matches = choices.filter(row => row.state === "match");
        const status = matches.length === 1 ? "selected" : matches.length > 1 ? "ambiguous"
            : choices.every(unusable) ? "unavailable" : "custom";
        rows.push({
            id: category.id, name: category.name || "", description: category.description || "",
            origin: category.origin || "workflow", warnings: Array.isArray(category.warnings) ? category.warnings : [],
            choices, status, selected: status === "selected" ? matches[0] : null, matches,
        });
    }
    return { rows, uncategorized: presets.filter(row => !claimed.has(row.id)) };
}

export const rowOf = (modes, presetId) => modes.rows.find(row => row.choices.some(choice => choice.id === presetId)) || null;

/**
 * What a choice sets: its settings, its switches, and every node those
 * switches cover, so a group switch and a node switch over one node meet.
 */
export function sharedControls(view, presetId) {
    const preset = (view?.presets || []).find(row => row.id === presetId);
    const keys = new Set();
    if (!preset) return keys;
    for (const member of preset.members || []) keys.add(`binding:${member.binding_id}`);
    const switches = new Map((view.execution || []).map(d => [d.execution_id, d]));
    for (const member of preset.execution || []) {
        keys.add(`switch:${member.execution_id}`);
        for (const node of switches.get(member.execution_id)?.members || []) keys.add(`node:${node.path}`);
    }
    return keys;
}

const quoted = text => `“${text}”`;
const listed = items => items.length < 2 ? items.join("") : `${items.slice(0, -1).join(", ")} and ${items.at(-1)}`;
const NOTICE_CLAUSES = 4;

/**
 * One sentence naming the other rows a click changed, or "". Only rows whose
 * choices share a Control with the clicked choice count: anything else that
 * changed meanwhile was not this click.
 */
export function followNotice(before, after, clickedId, view) {
    const clicked = (view?.presets || []).find(row => row.id === clickedId);
    if (!clicked) return "";
    const touched = sharedControls(view, clickedId);
    const shares = row => row.choices.some(choice => [...sharedControls(view, choice.id)].some(key => touched.has(key)));
    const previous = new Map(before.rows.map(row => [row.id, row]));
    const clauses = [];
    for (const row of after.rows) {
        if (row.choices.some(choice => choice.id === clickedId) || !shares(row)) continue;
        const was = previous.get(row.id);
        if (was && was.status === row.status && was.selected?.id === row.selected?.id) continue;
        if (row.status === "selected") clauses.push({ follows: true, text: `${row.name} to ${quoted(row.selected.name)}` });
        else if (row.status === "custom") clauses.push({ text: `${row.name} is now Custom` });
        else if (row.status === "ambiguous") clauses.push({ text: `${row.name} now matches several choices` });
    }
    if (!clauses.length) return "";
    const shown = clauses.slice(0, NOTICE_CLAUSES);
    const follows = shown.filter(clause => clause.follows).map(clause => clause.text);
    const parts = [];
    if (follows.length) parts.push(`${quoted(clicked.name)} also set ${listed(follows)}`);
    parts.push(...shown.filter(clause => !clause.follows).map(clause => clause.text));
    const more = clauses.length - shown.length;
    if (more) parts.push(`and ${more} more ${more === 1 ? "row" : "rows"} changed`);
    // Author names are kept as written, never recapitalized.
    return parts.join("; ") + ".";
}

/** The ambiguity line, naming at most two choices. */
export function ambiguityText(row) {
    const names = row.matches.map(choice => quoted(choice.name));
    if (names.length <= 2) return `${names.join(" and ")} both match. Fix in Manage.`;
    return `${names.slice(0, 2).join(", ")} and ${names.length - 2} more match. Fix in Manage.`;
}
