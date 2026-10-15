// Row-group Controls on the canvas: a node's dynamic row group as one Control.
// Pure on import (no app or DOM), so harnesses load it directly.
//
// ComfyUI's V3 dynamic group (io type COMFY_DYNAMICGROUP_V3; first consumer the
// 0.40 LoRA stack loaders) repeats a template of widgets as rows. Frontend 1.57+
// gives the node a hidden, non-serialized count widget named after the group
// (setting its value adds or removes rows at the end), an `<group>.$add`
// button, a `<group>.<i>` header per row and the fields `<group>.<i>.<field>`.
// Removing a middle row renames every later row, so a Control binds the group,
// never a row field. Frontend 1.55 has none of these widgets: the node shows a
// bare socket and the canvas cannot edit or run its rows.
//
// server/control_rows.py is the authority; this module mirrors only
// `canonical`, `summary` and `pluralName` (tests/test_control_rows_js.py pins
// them equal) and owns the canvas-side read and write.
import { findControlWidget } from "./control_widgets.js";

export const ROWS_KIND = "ROWS";
export const GROUP_TYPE = "COMFY_DYNAMICGROUP_V3";
const isPlainObject = value => !!value && typeof value === "object" && !Array.isArray(value);

// The node's definition as ComfyUI registered it (V1 `input` shape).
export const nodeDefinition = node => node?.constructor?.nodeData || node?.nodeData || null;

// {name: {fieldIds, fields, groupName, min, max}} for every row group the definition declares.
export function rowGroups(node) {
    const groups = {};
    const input = nodeDefinition(node)?.input;
    for (const category of ["required", "optional"]) {
        for (const [name, spec] of Object.entries(isPlainObject(input?.[category]) ? input[category] : {})) {
            if (!Array.isArray(spec) || spec[0] !== GROUP_TYPE) continue;
            const extra = isPlainObject(spec[1]) ? spec[1] : {};
            const template = isPlainObject(extra.template) ? extra.template : {};
            const required = isPlainObject(template.required) ? template.required : {};
            groups[name] = {
                fieldIds: Object.keys(required),
                // Kinds as the server names them; enough for the summary text.
                fields: Object.entries(required).map(([id, field]) => ({
                    id, kind: Array.isArray(field?.[0]) ? "COMBO" : String(field?.[0] ?? "") })),
                groupName: typeof extra.group_name === "string" && extra.group_name ? extra.group_name : name,
                min: Number.isInteger(extra.min) ? extra.min : 0,
                max: Number.isInteger(extra.max) ? extra.max : 20,
            };
        }
    }
    return groups;
}

export const rowGroupOf = (node, name) => rowGroups(node)[name] || null;

// The group a row widget or row field belongs to (`loras.0.strength`, `loras.$add`).
export function rowFieldGroup(node, name) {
    return Object.keys(rowGroups(node)).find(group => String(name).startsWith(`${group}.`)) || null;
}

// Whether this frontend built the group's row widgets (1.57+).
export const rowWidgetsReady = (node, group) =>
    !!findControlWidget(node, group) && !!findControlWidget(node, `${group}.$add`);

// A row field's input slot is linked: the Control can't set the group.
export const rowsLinked = (node, group) =>
    (node?.inputs || []).some(input => String(input?.widget?.name ?? input?.name ?? "").startsWith(`${group}.`) && input.link != null);

// The rows the canvas holds now, from the field widgets of rows 0..count-1.
export function readRows(node, group, fieldIds) {
    const count = Number(findControlWidget(node, group)?.value) || 0;
    const rows = [];
    for (let index = 0; index < count; index++) {
        const row = {};
        for (const field of fieldIds) row[field] = findControlWidget(node, `${group}.${index}.${field}`)?.value ?? null;
        rows.push(row);
    }
    return rows;
}

// Set the canvas rows: the count path adds or removes rows at the end (no
// middle-row renames), then every field of rows 0..n-1 is written and its
// callback replayed. Throws on an unknown widget layout, leaving the caller to
// restore. Never captures history; the canvas writer brackets the batch.
export function writeRows(node, group, fieldIds, rows, callback = () => {}) {
    const controller = findControlWidget(node, group);
    if (!controller || !findControlWidget(node, `${group}.$add`)) {
        throw new Error("This ComfyUI frontend can't show these rows. Update ComfyUI's frontend to 1.57 or newer.");
    }
    controller.value = rows.length;
    if (Number(controller.value) !== rows.length) throw new Error("The node refused this number of rows.");
    // The count path leaves the node's height alone; the frontend's own Add
    // and Remove refit it (dynamicGroupWidget changeRows), so this does too.
    if (node.size?.length >= 2 && typeof node.setSize === "function" && typeof node.computeSize === "function") {
        node.setSize([node.size[0], node.computeSize([...node.size])[1]]);
    }
    rows.forEach((row, index) => {
        for (const field of fieldIds) {
            const widget = findControlWidget(node, `${group}.${index}.${field}`);
            if (!widget) throw new Error("The node's rows have an unexpected layout, so nothing was saved.");
            widget.value = row[field];
            callback(widget, row[field]);
        }
    });
}

function normal(value) {
    if (value === null || typeof value === "boolean" || typeof value === "string") return value;
    if (typeof value === "number") {
        if (!Number.isFinite(value)) return null;
        return value;
    }
    if (Array.isArray(value)) return value.map(normal);
    if (isPlainObject(value)) return Object.fromEntries(Object.entries(value).map(([k, v]) => [String(k), normal(v)]));
    return value === undefined ? null : String(value);
}

// Python json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False).
function dump(value) {
    if (Array.isArray(value)) return `[${value.map(dump).join(",")}]`;
    if (isPlainObject(value)) {
        return `{${Object.keys(value).sort(compareKeys).map(key => `${JSON.stringify(key)}:${dump(value[key])}`).join(",")}}`;
    }
    if (typeof value === "number") return numberText(value);
    return JSON.stringify(value);
}

// Python sorts keys by code point, not UTF-16 unit.
function compareKeys(a, b) {
    const left = Array.from(a, c => c.codePointAt(0)), right = Array.from(b, c => c.codePointAt(0));
    for (let i = 0; i < Math.min(left.length, right.length); i++) if (left[i] !== right[i]) return left[i] - right[i];
    return left.length - right.length;
}

// Python and JS print an integral value (1.0 == 1) and ordinary decimals alike.
// Exponent forms (1e-05 vs 1e-5) differ; the server's form is the authority,
// and this one only compares values on the page.
const numberText = value => String(value);

// The comparison form of a row list; server control_rows.canonical.
export const canonical = rows => dump(normal(rows));

export const pluralName = noun => `${noun}s`;
const plural = (noun, count) => (count === 1 ? noun : pluralName(noun));

// `3 LoRAs · 2 on`; server control_rows.summary.
export function summary(rows, spec) {
    const list = Array.isArray(rows) ? rows : [];
    const noun = spec?.group_name || spec?.groupName || "row";
    let text = `${list.length} ${plural(noun, list.length)}`;
    const fields = spec?.fields || [];
    if (fields.some(field => field?.id === "enabled" && field?.kind === "BOOLEAN")) {
        text += ` · ${list.filter(row => isPlainObject(row) && row.enabled === true).length} on`;
    }
    return text;
}
