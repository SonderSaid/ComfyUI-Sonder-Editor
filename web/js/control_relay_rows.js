// The Sonder Control Relay's rows on the canvas (Phase 0 contract item 9).
//
// Each `ok` binding gets a frontend-only row widget on the Relay (and a mode
// row for a seed) that reads and writes the bound native widget. Rows:
// - never serialize (`serialize` and `options.serialize` false), so they reach
//   neither `widgets_values` nor the prompt;
// - never hold a widget object: every write and option read resolves the native
//   widget by (node id, widget name) through the graph's id map;
// - never capture history and never wrap a native callback. A write sets the
//   value and calls the native callback, exactly like a native edit, so history
//   follows the renderer's own capture (one step per gesture);
// - are rebuilt only when what they show changes, and never while one of them
//   is being edited.
// Pure on import: the caller (control_relay.js) supplies the canvas and graph.
import { CONTROL_SEED_MODES, findControlWidget } from "./control_widgets.js";
import {
    cleanGroup, defaultBindingLabel, effectiveGroupOrder, nodeType, parseManifest, pickerFor, planRestore, relayCensus, resolveAnchor, resolveBindings, sectionOf,
} from "./control_bindings.js";
import { readRows, summary } from "./control_rows.js";
import { parseLaneSelection } from "./reference_bridge_shape.js";

export const ROW_PREFIX = "sonder_row:";
// Rows that are not a binding use `~`, which a binding id (ID_RE) cannot hold.
const HINT = `${ROW_PREFIX}~hint`;
const SEPARATOR = `${ROW_PREFIX}~sep:`;
// A separator reads as a heading in both renderers: the group's name as the
// label and a rule as the read-only value (Phase 4A probe, 2026-10-08).
export const SEPARATOR_RULE = "────────";
const SIGNATURE = Symbol("sonderRelayRows");
const ROW_TYPES = { number: "number", combo: "combo", toggle: "toggle", boolean: "toggle", text: "text",
    string: "text", customtext: "text", textarea: "text" };
const NUMBER_OPTIONS = ["min", "max", "step", "step2", "precision", "round"];
export const EMPTY_HINT = "No Controls yet. Open the Sonder Editor's Controls and choose Manage.";

const isRow = widget => typeof widget?.name === "string" && widget.name.startsWith(ROW_PREFIX);
export const rowWidgets = relay => (relay?.widgets || []).filter(isRow);

function graphNode(root, id) {
    return root?.getNodeById?.(id) ?? (root?._nodes || root?.nodes || []).find(node => String(node?.id) === String(id)) ?? null;
}

// The native widget behind a row, looked up afresh: (node id, widget name),
// or the anchor's resolution for a subgraph host's promoted input.
export function nativeFor(root, ref) {
    const node = graphNode(root, ref.nodeId);
    if (!node) return null;
    const widget = findControlWidget(node, ref.widgetName) || resolveAnchor(node, ref.anchor).widget || null;
    if (!widget) return null;
    if (ref.role !== "mode") return { node, widget };
    const mode = resolveAnchor(node, ref.anchor).modeWidget;
    return mode ? { node, widget: mode } : null;
}

const byOrder = (a, b) => (a.order || 0) - (b.order || 0)
    || String(a.label).localeCompare(String(b.label)) || a.id.localeCompare(b.id);

// What the Relay should show: rows for `ok` bindings, a warning, or a hint.
export function planRows(relay, root) {
    const { census } = relayCensus(root);
    if (relay.graph && relay.graph !== root) return { warning: "A Sonder Control Relay inside a subgraph is off. Move it to the main graph." };
    if (census.nested) return { warning: "A Sonder Control Relay is inside a subgraph. Move it to the main graph." };
    if (relay.mode === 2 || relay.mode === 4) return { warning: "This Relay is muted or bypassed, so its Controls are off." };
    if (census.active > 1) return { warning: "Keep one Sonder Control Relay: this workflow has more than one." };
    let manifest;
    try {
        manifest = parseManifest((relay.widgets || []).find(w => w?.name === "manifest")?.value ?? "");
    } catch (error) {
        return { warning: error.message };
    }
    const { bindings } = resolveBindings(root, manifest);
    // The panel's order: Editor first, then ungrouped Workflow Controls, then
    // each group under a separator. Advanced rows stay (a personal Show
    // advanced toggle must never resize the saved node) and carry a suffix.
    const editor = [], ungrouped = [], grouped = new Map();
    const entries = Object.entries(manifest.bindings).map(([id, entry]) => ({ id, ...entry }));
    for (const entry of entries.sort(byOrder)) {
        const resolved = bindings[entry.id];
        const type = ROW_TYPES[resolved?.widget?.type];
        if (resolved?.status !== "ok" || (!type && !resolved.rows)) continue;
        const anchor = resolved.anchor;
        const name = entry.label || defaultBindingLabel(resolved);
        const label = entry.advanced ? `${name} · Adv` : name;
        const own = [];
        const picker = pickerOf(resolved);
        if (resolved.rows) {
            // A row list is edited in Controls; the Relay shows its summary.
            own.push({ id: entry.id, nodeId: anchor.node.id, anchor: anchor.name, widgetName: null,
                role: "summary", type: "text", label, options: {} });
        } else if (picker) {
            // Lanes and folders are picked on their node or in Controls, where the
            // project's names are known; the Relay shows what is chosen.
            own.push({ id: entry.id, nodeId: anchor.node.id, anchor: anchor.name, widgetName: resolved.widget.name,
                role: "summary", type: "text", label, options: {}, picker });
        } else {
            const ref = { id: entry.id, nodeId: anchor.node.id, anchor: anchor.name, widgetName: resolved.widget.name };
            own.push({ ...ref, role: "value", type, label, options: rowOptions(resolved.widget, type) });
            if (resolved.modeWidget) own.push({ ...ref, role: "mode", type: "combo", label: "↳ mode" });
        }
        const group = cleanGroup(entry.group);
        if (sectionOf(resolved, entry.section) === "editor") editor.push(...own);
        else if (!group) ungrouped.push(...own);
        else grouped.set(group, [...(grouped.get(group) || []), ...own]);
    }
    const rows = [...editor, ...ungrouped];
    for (const group of effectiveGroupOrder(manifest, bindings)) {
        const own = grouped.get(group);
        if (!own) continue;
        rows.push({ id: group, role: "separator", type: "text", label: group }, ...own);
    }
    const recovery = planRestore(root, manifest);
    const restore = {count: recovery.restorable.length, unmatched: recovery.unmatched.length};
    const missing = Object.values(bindings).filter(b => b.status === "missing").length;
    const result = rows.length ? {rows} : {hint: missing
        ? `${missing} Controls aren't connected to settings. Open the Sonder Editor's Controls → Manage to Rebind or Clean up.` : EMPTY_HINT};
    return restore.count || restore.unmatched ? {...result, restore} : result;
}

function rowOptions(native, type) {
    const options = {};
    if (type === "number") for (const key of NUMBER_OPTIONS) if (native.options?.[key] !== undefined) options[key] = native.options[key];
    if (type === "text" && native.options?.multiline) options.multiline = true;
    return options;
}

// The picker every target of a binding shares, by each RESOLVED target's class:
// a promoted input's anchor is its subgraph host, never the node that owns the
// value. Mixed or none: null.
function pickerOf(resolved) {
    const kinds = (resolved.targets || []).map(target => pickerFor(nodeType(target.node || resolved.node), target.input));
    return kinds.length && kinds.every(kind => kind && kind === kinds[0]) ? kinds[0] : null;
}

const PICKER_SUFFIX = " — edit on the node or in Controls";

/** A picker binding's Relay text from the native value (1-based, as the graph shows). */
export function pickerSummary(kind, value) {
    if (kind === "reference_lanes") {
        const { laneIndices } = parseLaneSelection(value);
        if (!laneIndices.length) return "No lanes";
        return `${laneIndices.length === 1 ? "Lane" : "Lanes"} ${laneIndices.map(index => index + 1).join(", ")}`;
    }
    if (kind === "driver_lane") {
        const lane = Number(value);
        return `Driver lane ${Number.isSafeInteger(lane) && lane >= 0 ? lane + 1 : 1}`;
    }
    if (kind === "asset_folder") return String(value ?? "").trim() || "Root";
    return String(value ?? "");
}

// A row list's Relay text, read live: "3 LoRAs · 2 on — edit in Controls".
export function summaryText(root, row) {
    if (row.picker) {
        const native = nativeFor(root, row);
        return native ? `${pickerSummary(row.picker, native.widget.value)}${PICKER_SUFFIX}` : "";
    }
    const node = graphNode(root, row.nodeId);
    const rows = node ? resolveAnchor(node, row.anchor).rows : null;
    if (!rows) return "";
    if (!rows.ready) return "Needs ComfyUI frontend 1.57 or newer";
    return `${summary(readRows(node, rows.group, rows.fieldIds), {group_name: rows.groupName, fields: rows.fields})} — edit in Controls`;
}

const signature = plan => JSON.stringify([plan.restore, plan.rows
    ? plan.rows.map(r => [r.id, r.role, r.type, r.label, r.nodeId, r.widgetName, r.options])
    : [plan.warning || plan.hint]]);
const isSeparator = row => row.role === "separator";
const isSummary = row => row.role === "summary";

function addRow(relay, spec) {
    const options = { ...spec.options, serialize: false };
    const widget = relay.addCustomWidget({
        type: spec.type, name: spec.name, label: spec.label, value: spec.value, y: 0,
        serialize: false, disabled: !!spec.disabled, options, callback: spec.callback,
    });
    widget.serialize = false;
    widget.options ||= {};
    widget.options.serialize = false;
    // Combo options are read live from the native widget on every access.
    if (spec.values) Object.defineProperty(widget.options, "values", { get: spec.values, enumerable: true, configurable: true });
    return widget;
}

/**
 * Bring the Relay's rows up to date. `isEditing(relay)` says a row has focus
 * or an open editor; then nothing is rebuilt or overwritten.
 */
export function syncRelayRows(relay, { root, canvas = null, isEditing = () => false, resize = false } = {}) {
    if (!relay?.graph || !root || typeof relay.addCustomWidget !== "function") return false;
    const plan = planRows(relay, root);
    const key = signature(plan);
    const editing = isEditing(relay);
    if (relay[SIGNATURE] === key) {
        if (editing || !plan.rows) return false;
        // Same rows: follow native edits made elsewhere.
        let changed = false;
        for (const widget of rowWidgets(relay)) {
            const row = plan.rows.find(r => widget.name === rowName(r));
            const live = !row || isSeparator(row) ? null
                : isSummary(row) ? {value: summaryText(root, row)} : nativeFor(root, row)?.widget || null;
            if (live && widget.value !== live.value) {
                widget.value = live.value;
                changed = true;
            }
        }
        if (changed) relay.setDirtyCanvas?.(true, true);
        return changed;
    }
    if (editing) return false;
    for (const widget of rowWidgets(relay)) relay.removeWidget?.(widget);
    relay[SIGNATURE] = key;
    if (plan.restore?.count) {
        addRow(relay, {type: "button", name: `${ROW_PREFIX}~restore`,
            label: `Restore ${plan.restore.count} Controls from this file`, value: "",
            callback: () => relay.onRestoreControls?.()});
    } else if (!plan.rows) {
        addRow(relay, { type: "text", name: HINT, label: plan.warning ? "Warning" : "Controls",
            value: plan.warning || plan.hint, disabled: true, callback: () => {} });
    }
    for (const row of plan.rows || []) {
        if (isSeparator(row) || isSummary(row)) {
            addRow(relay, { type: "text", name: rowName(row), label: row.label,
                value: isSummary(row) ? summaryText(root, row) : SEPARATOR_RULE, disabled: true, callback: () => {} });
            continue;
        }
        const native = nativeFor(root, row);
        addRow(relay, {
            ...row,
            name: rowName(row),
            value: native?.widget.value,
            values: row.role === "mode" ? () => [...CONTROL_SEED_MODES]
                : row.type === "combo" ? () => nativeFor(root, row)?.widget.options?.values ?? [] : null,
            callback: value => {
                const target = nativeFor(root, row);
                if (!target || target.widget.value === value) return;
                target.widget.value = value;
                target.widget.callback?.(value, canvas, target.node);
                target.node.setDirtyCanvas?.(true, true);
            },
        });
    }
    // Grow only in an explicit authoring transaction. Passive refresh/load
    // must not dirty the saved workflow or create an Undo step.
    const size = resize ? relay.computeSize?.() : null;
    if (size && relay.size && (relay.size[0] < 280 || relay.size[1] < size[1])) {
        relay.setSize?.([Math.max(relay.size[0], 280), Math.max(relay.size[1], size[1])]);
    }
    relay.setDirtyCanvas?.(true, true);
    return true;
}

const rowName = row => (isSeparator(row) ? `${SEPARATOR}${row.id}`
    : `${ROW_PREFIX}${row.id}${row.role === "mode" ? ":mode" : ""}`);
