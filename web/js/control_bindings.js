// Controls bindings on the canvas graph: anchors, the Relay manifest and the
// one resolver the export, the canvas writer and the Relay's rows share.
// Pure on import: no app, DOM or ComfyUI module, so harnesses load it directly.
//
// A binding is a manifest entry (presentation, keyed by binding_id) plus one
// anchor `node.properties.sonderBindings = {name: binding_id}` on the root node
// or subgraph host that owns the value. Anchors locate; the manifest exposes.
// server/control_manifest.py and server/control_descriptors.py are the authority.
import { getGraphLink, getGraphNode } from "./project_source_resolver.js";
import { findControlWidget, seedModeWidget } from "./control_widgets.js";
import { cleanGroup, DESCRIPTION_MAX, KJ_PREVIEW_CLASS, VIEW_FEED_OUTPUT, VIEW_FEED_SAMPLING, viewFeed } from "./control_targets.js";
import { pluralName, rowFieldGroup, rowGroupOf, rowGroups, rowsLinked, rowWidgetsReady } from "./control_rows.js";

export { DESCRIPTION_MAX, VIEW_FEED_OUTPUT, VIEW_FEED_SAMPLING, cleanGroup, groupNameError, isStructuralTarget, parseTarget, viewFeed } from "./control_targets.js";

export const RELAY_CLASS = "SonderControlRelay";
export const ANCHOR_PROPERTY = "sonderBindings";
// A node switch's anchor: `node.properties.sonderExecution = "<id>"` on a root
// node or subgraph host (one per node). A group is named by its root group.id.
export const EXECUTION_ANCHOR = "sonderExecution";
// An Output View's anchor: `node.properties.sonderViews = {rel: "<id>"}` on a
// root output node (rel "") or on the root subgraph instance holding the
// output (rel = its path inside, "12:7"). The instance owns the identity, so a
// definition shared by two instances is never ambiguous.
export const VIEWS_ANCHOR = "sonderViews";
export const MANIFEST_VERSION = 1;
// Mirrors server/control_manifest.py EXECUTION_* (parity-tested).
export const EXECUTION_VERSION = 1;
export const MAX_EXECUTION = 64;
export const MAX_EXECUTION_MEMBERS = 64;
export const EXECUTION_KINDS = ["node", "instance", "group"];
export const EXECUTION_MODES = ["enabled", "muted", "bypassed"];
// Never a switch, and never inside a group that is one: switching them off
// would turn off the Controls that switch them back.
export const INFRASTRUCTURE_CLASSES = new Set(["SonderEditor", "SonderControlRelay"]);
// Mirrors server/control_manifest.py VIEWS_* (parity-tested).
export const VIEWS_VERSION = 1;
export const MAX_VIEWS = 32;
export const VIEW_STATUSES = ["ok", "missing", "duplicate", "kind", "unsupported"];
const PATH_RE = /^[A-Za-z0-9_-]{1,64}(?::[A-Za-z0-9_-]{1,64}){0,7}$/;
// An execution path (control_manifest.clean_path), or null.
export const cleanPath = value => (typeof value === "string" && PATH_RE.test(value)
    && !value.split(":").some(part => FORBIDDEN_KEYS.has(part)) ? value : null);
// Mirrors server/control_manifest.py (Phase 0 P10 hygiene).
export const MAX_MANIFEST_BYTES = 128 * 1024;
export const MAX_BINDINGS = 256;
export const MAX_NOTES = 256;
export const MAX_SUGGESTIONS = 256;
export const MAX_TARGETS = 64;
export const MAX_GROUPS = 256;
export const MAX_PRESETS = 32;
export const MAX_PRESET_TARGETS = 64;
// Controls Modes: categories of exclusive choices (presets).
export const MAX_PRESET_CATEGORIES = 16;
export const CATEGORY_DESCRIPTION_MAX = 300;
export const MAX_DEPTH = 8;
export const LABEL_MAX = 120;
export const NOTE_MAX = 4000;
export const ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
export const FORBIDDEN_KEYS = new Set(["__proto__", "constructor", "prototype"]);
// Mirrors server/binding_policy.py; hides candidates early, the server decides.
export const DENIED_INPUTS = {
    SonderControlRelay: ["manifest"],
    SonderDriverSelector: ["driver_selector_overrides_json"],
    SonderEditor: ["mask_post_offset", "mask_pre_offset", "post_context_frames", "pre_context_frames", "project",
        "render_cache_max_bytes", "render_queue_active", "scene_id", "selection_end", "selection_start",
        "take_crop_position", "take_fit_mode", "take_keep_padding", "take_placement_linked", "take_placement_mode",
        "take_placement_muted"],
    SonderGuidesBridgeStart: ["bridge_overrides_json", "iteration_index"],
};
// Mirrors server/binding_policy.py PICKERS: "class/input" -> the picker kind.
export const PICKERS = {
    "SonderReferenceSelector/reference_lanes": "reference_lanes",
    "SonderDriverSelector/driver_lane_index": "driver_lane",
    "SonderSaveBridge/target_folder": "asset_folder",
};
export const pickerFor = (classType, input) => PICKERS[`${classType}/${input}`] || null;
export const DENIED_PREFIXES = { SonderMetadataCollector: "label_", SonderMetadataCollectorV3: "label_" };
// Mirrors server/binding_policy.py LEGACY_PRIMITIVE_CLASS: virtual, never in a prompt.
export const LEGACY_PRIMITIVE = "PrimitiveNode";
// Section by node class: mirrors server/control_descriptors.py EDITOR_CLASSES
// (tests/test_control_manifest.py pins the two equal).
export const EDITOR_CLASSES = new Set(["SonderSaveVideo", "SonderSaveBridge", "SonderMasksBridge"]);
// Mirrors server/control_descriptors.py MAX_CANDIDATES.
export const MAX_CANDIDATES = 500;
// Widgets the frontend adds beside a value; they never reach a prompt.
const COMPANION_WIDGETS = new Set(["control_after_generate", "control_filter_list"]);

const MUTED = 2;
const BYPASS = 4;
// Frontend 1.53.10 puts this id, as a string, on a subgraph's input boundary.
const SUBGRAPH_INPUT_ID = -10;
const MAX_NESTING = 8;
const ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-";
const COPY_RULE = Symbol("sonderAnchorCopyRule");

export const nodeType = node => String(node?.comfyClass || node?.type || "");
export const nodesOf = graph => graph?._nodes || graph?.nodes || [];
const valuesOf = map => (map?.values ? [...map.values()] : Object.values(map || {}));
const isHost = node => !!node?.subgraph || (typeof node?.isSubgraphNode === "function" && node.isSubgraphNode());
const isOff = node => node?.mode === MUTED || node?.mode === BYPASS;
// Modes 1 and 3 serialize like 0 (executionUtil.ts skips only 2 and 4).
export const modeName = node => (node?.mode === MUTED ? "muted" : node?.mode === BYPASS ? "bypassed" : "enabled");
export const NATIVE_MODES = { enabled: 0, muted: MUTED, bypassed: BYPASS };
const isPlainObject = value => !!value && typeof value === "object" && !Array.isArray(value);
const utf8Bytes = text => new TextEncoder().encode(text).length;

export function isDenied(classType, name) {
    if ((DENIED_INPUTS[classType] || []).includes(name)) return true;
    const prefix = DENIED_PREFIXES[classType];
    return !!prefix && String(name).startsWith(prefix);
}

export function mintBindingId(length = 12) {
    const bytes = new Uint8Array(length);
    globalThis.crypto.getRandomValues(bytes);
    return Array.from(bytes, byte => ID_ALPHABET[byte % ID_ALPHABET.length]).join("");
}

// Every graph reachable from the root: the root, its subgraph definitions and
// any definition a host points at.
export function graphsOf(root) {
    const seen = new Set(), pending = [root];
    while (pending.length) {
        const graph = pending.pop();
        if (!graph || seen.has(graph)) continue;
        seen.add(graph);
        pending.push(...valuesOf(graph.subgraphs));
        for (const node of nodesOf(graph)) if (node?.subgraph) pending.push(node.subgraph);
    }
    return [...seen];
}

// ComfyUI's DynamicPrompts extension serializes `{a|b}` text as a fresh random
// choice on every export. Controls reads an export it never queues, so it puts
// each widget's template back: otherwise rows, presets and the writer's graph
// hash see a different workflow on every read. Linked and promoted inputs take
// their value from elsewhere and are left alone.
export function restoreDynamicPrompts(graph, output, prefix = "", depth = 0) {
    if (depth > MAX_NESTING || !isPlainObject(output)) return output;
    for (const node of nodesOf(graph)) {
        const key = prefix ? `${prefix}:${node?.id}` : String(node?.id);
        if (isHost(node)) restoreDynamicPrompts(node.subgraph, output, key, depth + 1);
        const inputs = output[key]?.inputs;
        if (!isPlainObject(inputs)) continue;
        for (const widget of node.widgets || []) {
            if (!widget?.dynamicPrompts || typeof widget.value !== "string" || typeof inputs[widget.name] !== "string") continue;
            const linked = (node.inputs || []).some(input => (input?.widget?.name ?? input?.name) === widget.name && input.link != null);
            if (!linked) inputs[widget.name] = widget.value;
        }
    }
    return output;
}

// ── Anchors ──────────────────────────────────────────────────────────────────

// Valid anchors only: workflow JSON is untrusted, so names and ids follow the
// same rules as the manifest.
export function readAnchors(node) {
    const raw = node?.properties?.[ANCHOR_PROPERTY];
    const anchors = {};
    if (!isPlainObject(raw)) return anchors;
    for (const [name, id] of Object.entries(raw)) {
        if (FORBIDDEN_KEYS.has(name) || !name || name.length > 256) continue;
        if (typeof id === "string" && ID_RE.test(id) && !FORBIDDEN_KEYS.has(id)) anchors[name] = id;
    }
    return anchors;
}

export function hasAnchors(node) {
    return isPlainObject(node?.properties?.[ANCHOR_PROPERTY]);
}

// A copied node must not carry anchors: two nodes would claim one binding.
// The drop runs synchronously inside configure, before the copy's history
// capture; load, undo and redo configure the graph with `configuringGraph`
// set, so they keep anchors (Phase 0 contract item 3).
export function installAnchorCopyRule(node, isConfiguring) {
    if (!node || node[COPY_RULE]) return;
    const original = node.onConfigure;
    const chained = function (...args) {
        const result = original?.apply(this, args);
        if (!isConfiguring()) {
            if (hasAnchors(this)) delete this.properties[ANCHOR_PROPERTY];
            if (this.properties && Object.hasOwn(this.properties, EXECUTION_ANCHOR)) delete this.properties[EXECUTION_ANCHOR];
            if (this.properties && Object.hasOwn(this.properties, VIEWS_ANCHOR)) delete this.properties[VIEWS_ANCHOR];
        }
        return result;
    };
    node[COPY_RULE] = chained;
    node.onConfigure = chained;
}

// True while the node's own onConfigure is still the copy rule. Another
// extension may wrap it later (Sonder's own editor and Save Video wrappers do);
// a chaining wrapper still runs it, so a live paste is the real proof.
export const copyRuleInPlace = node => !!node?.[COPY_RULE] && node.onConfigure === node[COPY_RULE];

// ── Manifest ─────────────────────────────────────────────────────────────────

export const emptyManifest = () => ({ v: MANIFEST_VERSION, bindings: {}, notes: {}, groups: {}, description: "" });

function checkShape(value, depth = 1) {
    if (depth > MAX_DEPTH) throw new Error("The Controls manifest is nested too deeply.");
    if (Array.isArray(value)) for (const item of value) checkShape(item, depth + 1);
    else if (isPlainObject(value)) {
        for (const key of Object.keys(value)) {
            if (FORBIDDEN_KEYS.has(key)) throw new Error("The Controls manifest uses a reserved key.");
            checkShape(value[key], depth + 1);
        }
    }
}

// The saved manifest object, unknown keys kept. Throws with a user-facing reason.
export function parseManifest(text) {
    if (text == null || (typeof text === "string" && !text.trim())) return emptyManifest();
    if (typeof text !== "string") throw new Error("The Controls manifest must be text.");
    if (utf8Bytes(text) > MAX_MANIFEST_BYTES) throw new Error("The Controls manifest is too large (over 128 KiB).");
    let raw;
    try {
        raw = JSON.parse(text);
    } catch {
        throw new Error("The Controls manifest is not valid JSON.");
    }
    if (!isPlainObject(raw)) throw new Error("The Controls manifest must be an object.");
    checkShape(raw);
    if ((raw.v ?? MANIFEST_VERSION) !== MANIFEST_VERSION) throw new Error("This Controls manifest comes from a newer Sonder version.");
    for (const name of ["bindings", "notes"]) {
        if (raw[name] == null) raw[name] = {};
        if (!isPlainObject(raw[name])) throw new Error("The Controls manifest is malformed.");
    }
    // Cosmetic group damage must not hide valid Controls or break API export.
    if (!isPlainObject(raw.groups)) raw.groups = {};
    if (Object.keys(raw.bindings).length > MAX_BINDINGS) throw new Error(`At most ${MAX_BINDINGS} Controls fit in one workflow.`);
    if (Object.keys(raw.notes).length > MAX_NOTES) throw new Error(`At most ${MAX_NOTES} notes fit in one workflow.`);
    for (const id of [...Object.keys(raw.bindings), ...Object.keys(raw.notes)]) {
        if (!ID_RE.test(id)) throw new Error("A Controls id is invalid.");
    }
    if (raw.description == null) raw.description = "";
    if (typeof raw.description !== "string" || [...raw.description].length > DESCRIPTION_MAX)
        throw new Error("A workflow description must be text of at most 1000 characters.");
    return raw;
}

// The saved text after `mutate` edits a copy of the manifest. Export-only keys
// never reach saved text.
export function editManifest(text, mutate) {
    const manifest = structuredClone(parseManifest(text));
    delete manifest.suggestions;
    delete manifest.error;
    for (const entry of Object.values(manifest.bindings)) {
        if (!isPlainObject(entry)) continue;
        if (entry.restore_targets === undefined && entry.targets?.length) entry.restore_targets = entry.targets;
        for (const key of ["status", "targets", "seed_mode", "rows_unavailable", "participation", "inactive_reason"]) delete entry[key];
    }
    // A newer section is left exactly as saved: its export facts may differ.
    const ours = isOurExecutionSection(manifest.execution);
    for (const entry of Object.values(ours && isPlainObject(manifest.execution.controls) ? manifest.execution.controls : {})) {
        if (!isPlainObject(entry)) continue;
        for (const key of EXECUTION_EXPORT_KEYS) delete entry[key];
    }
    if (ours) delete manifest.execution.routing; // export-only, like each switch's facts
    if (isOurViewsSection(manifest.views) && isPlainObject(manifest.views.items)) {
        for (const entry of Object.values(manifest.views.items)) {
            if (isPlainObject(entry)) for (const key of VIEW_EXPORT_KEYS) delete entry[key];
        }
    }
    mutate(manifest);
    manifest.v = MANIFEST_VERSION;
    return JSON.stringify(manifest);
}

// A row-group Control's preset member: at most 20 flat rows of scalars.
// Mirrors server control_rows.value_shape_ok, counting code points as Python does.
function rowListShapeOk(value) {
    if (!Array.isArray(value) || value.length > 20) return false;
    const points = text => [...text].length;
    return value.every(row => isPlainObject(row) && Object.keys(row).length > 0 && Object.keys(row).length <= 32
        && Object.entries(row).every(([key, item]) => key.length > 0 && points(key) <= 64
            && (item === null || typeof item === "boolean" || (typeof item === "string" && points(item) <= 4096)
                || (typeof item === "number" && Number.isFinite(item)))));
}

// Export-only facts of a node switch (control_manifest._execution_entry).
const EXECUTION_EXPORT_KEYS = ["status", "problem", "title", "member_count", "members"];
// Export-only facts of a View (control_manifest._view_entry).
const VIEW_EXPORT_KEYS = ["status", "path", "class_type", "title", "participation", "inactive_reason"];
const ROOT_PATH_RE = /^[A-Za-z0-9_-]{1,64}$/;
const rootPath = value => (typeof value === "string" && ROOT_PATH_RE.test(value) && !FORBIDDEN_KEYS.has(value) ? value : null);

// A preset's switch member, or null (control_manifest._execution_member).
function executionMember(member) {
    if (!isPlainObject(member)) return null;
    if (Object.hasOwn(member, "modes")) {
        const modes = member.modes;
        if (!isPlainObject(modes)) return null;
        const entries = Object.entries(modes);
        if (entries.length < 1 || entries.length > MAX_EXECUTION_MEMBERS
            || entries.some(([path, mode]) => !rootPath(path) || !EXECUTION_MODES.includes(mode))) return null;
        return {modes: {...modes}};
    }
    const node = member.node;
    if (!EXECUTION_MODES.includes(member.mode) || !isPlainObject(node)) return null;
    const path = rootPath(node.path);
    if (!path || typeof node.class_type !== "string" || !node.class_type || [...node.class_type].length > 256) return null;
    return {mode: member.mode, node: {path, class_type: node.class_type}};
}

// `{path: mode}` a preset's switches set; null when two set one node differently.
export function executionEffects(members) {
    const effects = {};
    for (const member of Object.values(members || {})) {
        const pairs = member.modes ? Object.entries(member.modes) : [[member.node.path, member.mode]];
        for (const [path, mode] of pairs) {
            if (Object.hasOwn(effects, path) && effects[path] !== mode) return null;
            effects[path] = mode;
        }
    }
    return effects;
}

// The saved switches, tolerant like control_manifest.read_execution (parity-
// tested): invalid entries are skipped, at most MAX_EXECUTION are read, and
// only another section version turns the section off, with a reason.
export const isOurExecutionSection = raw =>
    isPlainObject(raw) && (!Object.hasOwn(raw, "v") || raw.v === EXECUTION_VERSION);
export function readExecution(raw) {
    if (!isPlainObject(raw)) return {controls: {}, error: null};
    if (!isOurExecutionSection(raw))
        return {controls: {}, error: "This workflow's node switches come from another Sonder version, so they are off."};
    if (!isPlainObject(raw.controls)) return {controls: {}, error: null};
    const result = {};
    const text = (value, limit) => value == null || (typeof value === "string" && [...value].length <= limit);
    for (const [id, entry] of Object.entries(raw.controls)) {
        if (Object.keys(result).length >= MAX_EXECUTION) break;
        const kind = entry?.target?.kind;
        const groupId = entry?.target?.group_id;
        if (!ID_RE.test(id) || FORBIDDEN_KEYS.has(id) || !isPlainObject(entry) || !EXECUTION_KINDS.includes(kind)
            || (kind === "group" && !(Number.isInteger(groupId) && Math.abs(groupId) <= 2 ** 31))
            || !text(entry.label, LABEL_MAX) || !text(entry.note, NOTE_MAX) || !validOrder(entry.order ?? undefined)
            || (entry.advanced !== undefined && typeof entry.advanced !== "boolean")) continue;
        result[id] = {...entry, target: kind === "group" ? {kind, group_id: groupId} : {kind}};
    }
    return {controls: result, error: null};
}

// Always offered as sampling-preview sources; mirrors control_views.SAMPLING_CLASSES.
export const SAMPLING_CLASSES = new Set(["KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced",
    KJ_PREVIEW_CLASS]);

// The saved Views, tolerant like control_manifest.read_views (parity-tested).
export const isOurViewsSection = raw => isPlainObject(raw) && (!Object.hasOwn(raw, "v") || raw.v === VIEWS_VERSION);
export function readViews(raw) {
    if (!isPlainObject(raw)) return {items: {}, error: null};
    if (!isOurViewsSection(raw)) return {items: {}, error: "This workflow's Views come from another Sonder version, so they are off."};
    if (!isPlainObject(raw.items)) return {items: {}, error: null};
    const result = {};
    for (const [id, entry] of Object.entries(raw.items)) {
        if (Object.keys(result).length >= MAX_VIEWS) break;
        if (!ID_RE.test(id) || FORBIDDEN_KEYS.has(id) || !isPlainObject(entry)
            || !(entry.label == null || (typeof entry.label === "string" && [...entry.label].length <= LABEL_MAX))
            || !validOrder(entry.order ?? undefined)
            || !(entry.feed == null || entry.feed === VIEW_FEED_OUTPUT || entry.feed === VIEW_FEED_SAMPLING)) continue;
        result[id] = {...entry, label: String(entry.label ?? "").trim(), order: entry.order ?? 0};
    }
    return {items: result, error: null};
}

// A name with nothing in it but whitespace by either language's rule: \s here
// also covers U+FEFF, Python's isspace also \x1c-\x1f and \x85.
// Mirrors control_manifest.blank_name.
const BLANK_RE = /^[\s\x1c-\x1f\x85]*$/;
export const blankName = text => BLANK_RE.test(text);

// Mirrors control_manifest.read_presets; reads never rewrite saved metadata.
export function readPresets(raw) {
    const result = {};
    if (!isPlainObject(raw)) return result;
    const validId = id => ID_RE.test(id) && !FORBIDDEN_KEYS.has(id);
    for (const [id, entry] of Object.entries(raw)) {
        if (!validId(id) || !isPlainObject(entry)) continue;
        const {name, description = "", order = 0} = entry;
        if (typeof name !== "string" || blankName(name) || [...name].length > LABEL_MAX
            || typeof description !== "string" || [...description].length > DESCRIPTION_MAX
            || !Number.isInteger(order) || Math.abs(order) > 9999) continue;
        const members = {};
        for (const [bid, member] of Object.entries(isPlainObject(entry.members) ? entry.members : {})) {
            if (!validId(bid) || !isPlainObject(member) || !Object.hasOwn(member, "value")) continue;
            const value = member.value;
            if (!(value === null || ["string", "boolean"].includes(typeof value) || (typeof value === "number" && Number.isFinite(value))
                || rowListShapeOk(value))) continue;
            if (Object.hasOwn(member, "seed_mode") && !["fixed", "increment", "decrement", "randomize"].includes(member.seed_mode)) continue;
            members[bid] = {value, ...(Object.hasOwn(member, "seed_mode") ? {seed_mode: member.seed_mode} : {})};
        }
        const execution = {};
        for (const [eid, member] of Object.entries(isPlainObject(entry.execution) ? entry.execution : {})) {
            if (!validId(eid)) continue;
            const clean = executionMember(member);
            if (clean) execution[eid] = clean;
        }
        const effects = executionEffects(execution);
        const entries = Object.values(members);
        // Distinct node effects count beside value and seed-mode targets.
        if ((!entries.length && !Object.keys(execution).length) || !effects
            || entries.reduce((n, m) => n + 1 + Number(Object.hasOwn(m, "seed_mode")), 0) + Object.keys(effects).length > MAX_PRESET_TARGETS) continue;
        if (Object.keys(result).length >= MAX_PRESETS) break;
        result[id] = {name, description, order, members, ...(Object.keys(execution).length ? {execution} : {})};
    }
    return result;
}

// Explicit edits alone may normalize a malformed presets map. Preserve every
// untouched entry; assert the edited entry survives the tolerant reader.
export function applyPresetEdit(manifest, id, field, value) {
    if (!isPlainObject(manifest.presets)) manifest.presets = {};
    const presets = manifest.presets;
    // A View is inspection only: never a preset member (server _capture_preset).
    const views = readViews(manifest.views).items;
    if ((field === "create" || field === "capture") && Object.keys(value?.members || {}).some(member => Object.hasOwn(views, member)))
        throw new Error("A View can't be part of a choice.");
    if (field === "create" || field === "capture") presets[id] = structuredClone(value);
    else if (field === "remove") {
        delete presets[id];
        // A deleted choice leaves its categories in the same edit.
        dropChoiceFromCategories(manifest, id);
    }
    else if (field === "name" || field === "description") {
        if (!Object.hasOwn(readPresets(presets), id)) throw new Error("This choice no longer exists.");
        presets[id][field] = value;
    } else if (field === "move") {
        const valid = readPresets(presets);
        const ordered = Object.keys(valid).sort((a, b) => valid[a].order - valid[b].order).filter(key => key !== id);
        if (!valid[id] || (value.before != null && !ordered.includes(value.before))) throw new Error("This choice moved. Refresh Controls.");
        ordered.splice(value.before == null ? ordered.length : ordered.indexOf(value.before), 0, id);
        ordered.forEach((key, order) => { presets[key].order = order; });
    } else throw new Error("This choice edit is unavailable.");
    if (field !== "remove" && !Object.hasOwn(readPresets(presets), id)) throw new Error("The choice would exceed its name, setting or 32-choice cap.");
}

// The saved categories (Controls Modes), tolerant like
// control_manifest.read_preset_categories (parity-tested): an unreadable
// category is skipped, an invalid choice id dropped, duplicates kept, at most
// MAX_PRESET_CATEGORIES valid entries. Whether a choice names a preset is the
// server's to decide.
export function readPresetCategories(raw) {
    const result = {};
    if (!isPlainObject(raw)) return result;
    const validId = id => typeof id === "string" && ID_RE.test(id) && !FORBIDDEN_KEYS.has(id);
    for (const [id, entry] of Object.entries(raw)) {
        if (!validId(id) || !isPlainObject(entry)) continue;
        const {name, description = "", order = 0, choices = []} = entry;
        if (typeof name !== "string" || blankName(name) || [...name].length > LABEL_MAX
            || typeof description !== "string" || [...description].length > CATEGORY_DESCRIPTION_MAX
            || !Number.isInteger(order) || Math.abs(order) > 9999
            || !Array.isArray(choices) || choices.length > MAX_PRESETS) continue;
        if (Object.keys(result).length >= MAX_PRESET_CATEGORIES) break;
        result[id] = {name, description, order, choices: choices.filter(validId)};
    }
    return result;
}

// Category ids by order, then position (control_preset_categories.ordered).
const orderedCategories = valid => Object.keys(valid).sort((a, b) => valid[a].order - valid[b].order);

// Removes a deleted choice from every category listing it
// (control_preset_categories.drop_choice). A damaged section is left as saved.
export function dropChoiceFromCategories(manifest, id) {
    if (!isPlainObject(manifest.preset_categories)) return;
    for (const entry of Object.values(manifest.preset_categories)) {
        if (isPlainObject(entry) && Array.isArray(entry.choices) && entry.choices.includes(id))
            entry.choices = entry.choices.filter(choice => choice !== id);
    }
}

// One validated category edit (control_preset_categories.edit_categories).
// Only an explicit edit may normalize a damaged section; every untouched entry
// is kept as saved, and the edited one must still read afterwards.
export function applyCategoryEdit(manifest, id, field, value) {
    if (typeof id !== "string" || !ID_RE.test(id) || FORBIDDEN_KEYS.has(id)) throw new Error("This category edit is unavailable.");
    if (!isPlainObject(manifest.preset_categories)) {
        if (field === "remove") return; // nothing readable to remove; a damaged section stays as saved
        manifest.preset_categories = {};
    }
    const categories = manifest.preset_categories;
    const valid = readPresetCategories(categories);
    // Own keys only: an id such as `toString` must never reach a prototype.
    const known = Object.hasOwn(valid, id);
    if (field === "create") {
        // An unreadable entry under this id is replaced, as on the server,
        // whose view never sees it. The cap is counted first: an index-like id
        // sorts first and would otherwise push another category past it.
        if (known) throw new Error("This category id is already in use.");
        if (Object.keys(valid).length >= MAX_PRESET_CATEGORIES)
            throw new Error(`At most ${MAX_PRESET_CATEGORIES} categories fit in one workflow.`);
        categories[id] = structuredClone(value);
    } else if (field === "remove") delete categories[id];
    else if (field === "name" || field === "description" || field === "choices") {
        if (!known) throw new Error("This category no longer exists. Refresh Controls.");
        categories[id][field] = structuredClone(value);
    } else if (field === "move") {
        const ordered = orderedCategories(valid).filter(key => key !== id);
        if (!known || (value?.before != null && !ordered.includes(value.before))) throw new Error("This category moved. Refresh Controls.");
        ordered.splice(value.before == null ? ordered.length : ordered.indexOf(value.before), 0, id);
        ordered.forEach((key, order) => { categories[key].order = order; });
    } else throw new Error("This category edit is unavailable.");
    if (field !== "remove" && !Object.hasOwn(readPresetCategories(categories), id))
        throw new Error("This category would be unreadable. Check its name and description.");
}

// ── Resolve ──────────────────────────────────────────────────────────────────

// Why a node does not run: itself muted or bypassed, or under a muted or
// bypassed subgraph host (`parent`, which the native serializer never expands).
const offReason = (node, parent) => (parent || (isOff(node)
    ? { mode: modeName(node), by: "self", label: String(node.title || nodeType(node)) } : null));
const asParent = reason => (reason ? { ...reason, by: "parent" } : null);

// Consumers of a promoted input, through nested hosts: a promoted input may
// feed several inner widgets, each its own API target. An inactive consumer is
// still resolved (its setting stays a Control) and carries `inactive`.
function promotedTargets(host, name, prefix, depth = 0, parent = null) {
    const graph = host?.subgraph, found = [];
    const slot = (graph?.inputs || []).find(input => input?.name === name);
    if (!slot || depth > MAX_NESTING) return found;
    const linkIds = slot.linkIds || [];
    for (const linkId of linkIds) {
        const link = getGraphLink(graph, linkId);
        const inner = link ? getGraphNode(graph, link.target_id) : null;
        const input = inner?.inputs?.[Number(link?.target_slot)];
        if (!inner || !input) continue;
        const path = `${prefix}:${inner.id}`;
        const widgetName = input.widget?.name ?? input.name;
        const inactive = offReason(inner, parent);
        if (isHost(inner)) {
            found.push(...promotedTargets(inner, input.name, path, depth + 1, asParent(inactive)));
        } else if (rowFieldGroup(inner, widgetName) || rowGroupOf(inner, widgetName)) {
            // A promoted row field has no stable identity (a removed row renames
            // later ones); a row group is a Control only on its own node.
            found.push({ path, input: widgetName, status: "unsupported", ...(inactive ? { inactive, node: inner } : {}) });
        } else {
            const widget = findControlWidget(inner, widgetName);
            found.push({ path, input: widgetName, status: widget ? "ok" : "no_widget", node: inner, widget,
                ...(inactive ? { inactive } : {}) });
        }
    }
    return found;
}

// A binding's participation from its targets: all run, none run, or some.
function participationOf(targets) {
    const off = (targets || []).filter(target => target.inactive);
    if (!off.length) return { participation: "active" };
    return { participation: off.length === targets.length ? "inactive" : "partial", inactiveReason: off[0].inactive };
}

function hostInput(host, name) {
    return (host.inputs || []).find(input => input?._subgraphSlot?.name === name || input?.name === name) || null;
}

// One binding on the live graph: its status, its native widget, its API
// targets and its participation. A muted or bypassed node keeps its binding:
// `status` is structural only, and `participation` says whether it runs.
// Never writes.
export function resolveAnchor(node, name) {
    const resolved = resolveStructure(node, name);
    return { ...resolved, ...participationOf(resolved.targets) };
}

function resolveStructure(node, name) {
    if (isHost(node)) {
        const input = hostInput(node, name);
        if (!input) return { status: "missing", targets: [] };
        const targets = promotedTargets(node, name, String(node.id), 0, asParent(offReason(node, null)));
        if (!targets.length) return { status: "missing", targets: [] };
        if (input.link != null) return { status: "linked", targets };
        const widget = node.getWidgetFromSlot?.(input) || findControlWidget(node, input.widget?.name || input.name);
        if (!widget) return { status: "no_widget", targets };
        const bad = targets.find(target => target.status !== "ok");
        if (bad) return { status: bad.status, targets };
        // A promoted seed's value lives on the host; its mode may stay on the
        // definition's widget (frontend 1.53.10), shared by every instance.
        const mode = seedModeWidget(widget) || targets.map(t => seedModeWidget(t.widget)).find(Boolean) || null;
        return { status: "ok", node, widget, modeWidget: mode, targets };
    }
    const inactive = offReason(node, null);
    const targets = [{ path: String(node.id), input: name, ...(inactive ? { inactive } : {}) }];
    // A dynamic row group is one Control; a single row field has no stable
    // identity (removing a row renames every later one), so it is refused.
    const group = rowGroupOf(node, name);
    if (group) {
        if (rowsLinked(node, name)) return { status: "linked", targets };
        return { status: "ok", node, widget: null, modeWidget: null, targets,
            rows: { group: name, ...group, ready: rowWidgetsReady(node, name) } };
    }
    if (rowFieldGroup(node, name)) return { status: "unsupported", targets };
    const linked = (node.inputs || []).some(input => (input?.widget?.name ?? input?.name) === name && input.link != null);
    if (linked) return { status: "linked", targets };
    const widget = findControlWidget(node, name);
    if (!widget) return { status: "no_widget", targets };
    return { status: "ok", node, widget, modeWidget: seedModeWidget(widget) || null, targets };
}

// Every manifest binding resolved on the graph, plus anchors without an entry.
export function resolveBindings(root, manifest) {
    const rootAnchors = [], definitionIds = new Set();
    for (const node of nodesOf(root)) {
        for (const [name, id] of Object.entries(readAnchors(node))) rootAnchors.push({ node, name, id });
    }
    for (const graph of graphsOf(root)) {
        if (graph === root) continue;
        for (const node of nodesOf(graph)) for (const id of Object.values(readAnchors(node))) definitionIds.add(id);
    }
    const bindings = Object.create(null);
    for (const id of Object.keys(manifest?.bindings || {})) {
        const hits = rootAnchors.filter(anchor => anchor.id === id);
        if (hits.length > 1) bindings[id] = { status: "duplicate", targets: [] };
        // An anchor left inside a definition (Convert to Subgraph configures the
        // moved nodes while the graph is configuring, so the copy rule keeps it)
        // is not reachable; `missing` lets Manage offer a rebind.
        else if (!hits.length) bindings[id] = { status: "missing", targets: [], insideSubgraph: definitionIds.has(id) };
        else bindings[id] = { ...resolveAnchor(hits[0].node, hits[0].name), anchor: hits[0] };
    }
    const suggestions = [], listed = new Set();
    for (const anchor of rootAnchors) {
        if (Object.hasOwn(bindings, anchor.id) || listed.has(anchor.id) || suggestions.length >= MAX_SUGGESTIONS) continue;
        listed.add(anchor.id);
        suggestions.push({ binding_id: anchor.id, targets: resolveAnchor(anchor.node, anchor.name).targets });
    }
    return { bindings, suggestions };
}

// A resolved binding's section: "editor" when every target node is an editor
// class (server section_for). `fallback` is the stored section, used only when
// a target's node is unknown.
export function sectionOf(resolved, fallback = "workflow") {
    const host = resolved?.anchor?.node || resolved?.node || null;
    const targets = resolved?.targets || [];
    const nodes = targets.map(target => target.node || (host && String(host.id) === String(target.path) ? host : null));
    if (!nodes.length || nodes.some(node => !node)) return fallback === "editor" ? "editor" : "workflow";
    return nodes.every(node => EDITOR_CLASSES.has(nodeType(node))) ? "editor" : "workflow";
}

// Match Python's lexical code-point ordering, not the browser's locale.
const compareCodePoints = (a, b) => {
    const left = Array.from(a, c => c.codePointAt(0)), right = Array.from(b, c => c.codePointAt(0));
    for (let i = 0; i < Math.min(left.length, right.length); i++) {
        if (left[i] !== right[i]) return left[i] - right[i];
    }
    return left.length - right.length;
};

// Mirrors control_descriptors._default_label using the first exported target,
// including the inner consumer of a promoted widget rather than its host.
export function defaultBindingLabel(resolved) {
    const target = resolved?.targets?.[0];
    if (!target) return "Setting";
    const host = resolved?.anchor?.node || resolved?.node;
    const node = target.node || (String(host?.id) === String(target.path) ? host : null);
    const title = node?.title || (node ? nodeType(node) : "");
    const name = resolved?.rows ? pluralName(resolved.rows.groupName) : target.input;
    return title ? `${title} · ${name}` : name;
}

const clampOrder = value => (Number.isInteger(value) ? Math.max(-9999, Math.min(9999, value)) : 0);
const validOrder = value => value === undefined || (Number.isInteger(value) && Math.abs(value) <= 9999);

// The explicit groups as the server parses them: valid names, stripped, the
// first of colliding names kept, an entry with an invalid order dropped.
// Map: name -> [raw key, entry].
function explicitGroups(manifest) {
    const found = new Map();
    for (const [key, entry] of Object.entries(isPlainObject(manifest?.groups) ? manifest.groups : {})) {
        const name = cleanGroup(key);
        if (!name || found.has(name) || !isPlainObject(entry) || !validOrder(entry.order)) continue;
        if (found.size >= MAX_GROUPS) break;
        found.set(name, [key, entry]);
    }
    return found;
}

// The group order of a saved manifest: mirrors server control_descriptors.
// group_order (a shared fixture pins them): groups in `groups` first, by order
// then name; then groups only named by Workflow bindings or notes, by first
// appearance. An Editor-section binding's group shows nowhere, so it adds none.
export function effectiveGroupOrder(manifest, resolved = {}, switches = {}) {
    const explicit = explicitGroups(manifest);
    const ordered = [...explicit.keys()]
        .sort((a, b) => clampOrder(explicit.get(a)[1].order) - clampOrder(explicit.get(b)[1].order) || compareCodePoints(a, b));
    const rows = [
        ...Object.entries(manifest?.bindings || {})
            .filter(([id, entry]) => sectionOf(resolved[id], entry?.section) !== "editor")
            .map(([id, entry]) => ({ id, ...entry, label: entry?.label || defaultBindingLabel(resolved[id]) })),
        ...Object.entries(manifest?.notes || {}).map(([id, entry]) => ({ id, ...entry, label: "" })),
        // Node switches name groups too (server group_order counts them).
        ...Object.entries(readExecution(manifest?.execution).controls)
            .map(([id, entry]) => ({ id, ...entry, label: entry.label || switchLabel(switches[id], entry) })),
    ].sort((a, b) => clampOrder(a.order) - clampOrder(b.order)
        || compareCodePoints(String(a.label || "").toLowerCase(), String(b.label || "").toLowerCase())
        || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    for (const row of rows) {
        const name = cleanGroup(row.group);
        if (name && !ordered.includes(name)) ordered.push(name);
    }
    return ordered;
}

// Expand one validated group plan (server _StructuralBatch.group) on a saved
// manifest copy, inside the canvas writer's single editManifest call. Members
// are re-keyed in place; `groups` is rewritten in display order, so implied
// groups keep their place and unknown keys of an entry survive.
export function applyGroupPlan(manifest, name, plan, resolved = {}, switches = {}) {
    const order = effectiveGroupOrder(manifest, resolved, switches);
    // Settings, notes and node switches regroup together; a newer switch
    // section is never rewritten.
    const switchEntries = isOurExecutionSection(manifest.execution) && isPlainObject(manifest.execution.controls)
        ? Object.values(manifest.execution.controls) : [];
    const members = [...Object.values(manifest.bindings || {}), ...Object.values(manifest.notes || {}), ...switchEntries]
        .filter(isPlainObject);
    const regroup = (from, to) => { for (const entry of members) if (cleanGroup(entry.group) === from) entry.group = to; };
    const explicit = explicitGroups(manifest);
    const place = names => {
        // Null prototype: a "__proto__" name becomes an own key the writer's
        // re-parse refuses, never a prototype swap that hides it.
        const groups = Object.create(null);
        names.forEach((group, index) => {
            groups[group] = { ...(explicit.get(group)?.[1] || {}), order: index + 1 };
        });
        manifest.groups = groups;
    };
    if (plan?.create) place([...order.filter(group => group !== name), name]);
    else if (plan?.rename) {
        if (explicit.has(name)) explicit.set(plan.rename, explicit.get(name));
        regroup(name, plan.rename);
        place(order.map(group => (group === name ? plan.rename : group)));
    } else if (plan?.ungroup) {
        regroup(name, "");
        place(order.filter(group => group !== name));
    } else if (plan?.move) {
        const rest = order.filter(group => group !== name);
        if (!order.includes(name) || (plan.before != null && !rest.includes(plan.before))) {
            throw new Error("The group order changed. Refresh the Controls panel.");
        }
        const index = plan.before == null ? rest.length : rest.indexOf(plan.before);
        place([...rest.slice(0, index), name, ...rest.slice(index)]);
    } else throw new Error("This group change is unavailable. Refresh the Controls panel.");
}

// ── Candidates (Manage) ──────────────────────────────────────────────────────

const rawAnchors = node => (isPlainObject(node?.properties?.[ANCHOR_PROPERTY]) ? node.properties[ANCHOR_PROPERTY] : null);
const hostInputName = input => input?._subgraphSlot?.name ?? input?.name;

// What Manage may expose on the live graph: root widgets and subgraph-host
// promoted inputs, each as {path, input} (the anchor's node and name) plus the
// API targets it sets. Anchored, linked, denied, muted and virtual ones are
// left out here; the server checks the rest (server/control_descriptors.py
// candidate_inventory) and never lists what it refuses.
export function listCandidates(root, { filter = "", limit = MAX_CANDIDATES, labels = [] } = {}) {
    const query = String(filter || "").trim().toLowerCase();
    const found = [];
    const recommendedLabels = new Map(labels.map(field => [`${field.path}/${field.input}`, field.label]));
    let truncated = false;
    const push = (node, name, targets, label) => {
        const title = String(node.title || nodeType(node));
        const recommended = targets.map(target => recommendedLabels.get(`${target.path}/${target.input}`) || "");
        if (query && ![title, nodeType(node), name, label || "", ...recommended].some(text => String(text).toLowerCase().includes(query))) return;
        if (found.length >= limit) { truncated = true; return; }
        found.push({ path: String(node.id), input: name, targets: publicTargets(targets), node_title: title });
    };
    for (const node of nodesOf(root)) {
        if (truncated) break;
        if (!node || isRelay(node) || isOff(node) || nodeType(node) === LEGACY_PRIMITIVE) continue;
        const anchored = readAnchors(node);
        // A subgraph host reports isVirtualNode (frontend 1.53.10) yet its
        // promoted inputs reach the prompt through the inner consumers.
        if (isHost(node)) {
            for (const input of node.inputs || []) {
                const name = hostInputName(input);
                if (!name || Object.hasOwn(anchored, name) || input.link != null) continue;
                const resolved = resolveAnchor(node, name);
                // An inactive consumer is still a Control once exposed, but Manage
                // offers only settings that run now: enable the node to expose it.
                if (resolved.status === "ok" && resolved.participation === "active") push(node, name, resolved.targets, input.label);
            }
            continue;
        }
        if (node.isVirtualNode) continue; // Reroute, Note, legacy Primitive: never in a prompt
        const linked = new Set((node.inputs || []).filter(input => input?.link != null).map(input => input.widget?.name ?? input.name));
        // A row group is listed from the node's definition, also when this
        // frontend built no row widgets; its row widgets are never listed.
        for (const group of Object.keys(rowGroups(node))) {
            if (Object.hasOwn(anchored, group) || isDenied(nodeType(node), group)) continue;
            if (resolveAnchor(node, group).status === "ok") push(node, group, [{ path: String(node.id), input: group }], "");
        }
        for (const widget of node.widgets || []) {
            const name = widget?.name;
            if (!name || Object.hasOwn(anchored, name) || linked.has(name) || COMPANION_WIDGETS.has(name)) continue;
            if (rowGroupOf(node, name) || rowFieldGroup(node, name)) continue;
            if (widget.options?.serialize === false || widget.serialize === false || isDenied(nodeType(node), name)) continue;
            push(node, name, [{ path: String(node.id), input: name }], widget.label);
        }
    }
    return { candidates: found, truncated };
}

// One candidate resolved on the live graph, for a structural write: the node
// that will carry the anchor and the targets it sets now.
export function resolveCandidate(root, candidate) {
    const node = nodesOf(root).find(n => String(n?.id) === String(candidate?.path));
    if (!node || !candidate?.input) return null;
    const resolved = resolveAnchor(node, candidate.input);
    if (resolved.status !== "ok" || resolved.participation !== "active") return null;
    return { node, name: candidate.input, targets: publicTargets(resolved.targets),
        node_title: String(node.title || nodeType(node)) };
}

// Hints locate possible recovery only; anchors remain the binding authority.
// No graph walk on the ordinary (no API hints) passive Relay refresh path.
export function planRestore(root, manifest) {
    const result = {restorable: [], unmatched: []};
    const hinted = Object.entries(manifest?.bindings || {}).filter(([, entry]) =>
        Array.isArray(entry?.restore_targets ?? entry?.targets) && (entry.restore_targets ?? entry.targets).length);
    if (!hinted.length) return result;
    const anchored = new Set(), byId = new Map(), flattened = new Map(), claimed = new Set();
    for (const graph of graphsOf(root)) for (const node of nodesOf(graph)) {
        for (const id of Object.values(readAnchors(node))) anchored.add(id);
        if (graph !== root) continue;
        const key = String(node.id), base = key.replace(/_+$/, "");
        if (!byId.has(key)) byId.set(key, []);
        byId.get(key).push(node);
        if (!flattened.has(base)) flattened.set(base, []);
        flattened.get(base).push(node);
    }
    for (const [binding_id, entry] of hinted) {
        if (anchored.has(binding_id)) continue;
        const hints = entry.restore_targets ?? entry.targets;
        const miss = reason => result.unmatched.push({binding_id, reason});
        if (hints.length !== 1) { miss("several_settings"); continue; }
        const {path, input} = hints[0] || {};
        if (typeof path !== "string" || !path || typeof input !== "string" || !input) { miss("unavailable"); continue; }
        const mapped = path.replaceAll(":", "_");
        const candidates = byId.get(path) || (flattened.get(mapped.replace(/_+$/, "")) || [])
            .filter(node => String(node.id) === mapped || (String(node.id).startsWith(mapped) && /^_+$/.test(String(node.id).slice(mapped.length))));
        if (!candidates.length) { miss("not_found"); continue; }
        if (candidates.length !== 1) { miss("ambiguous"); continue; }
        const node = candidates[0], key = `${node.id}/${input}`;
        if (Object.hasOwn(readAnchors(node), input) || claimed.has(key)) { miss("taken"); continue; }
        // resolveCandidate shares the widget/link resolver; policy and virtual
        // inputs are rejected here too, then independently by the server.
        const live = resolveCandidate({_nodes: [node]}, {path: String(node.id), input});
        // A row group's own widget is its hidden, non-serialized row count.
        const widget = rowGroupOf(node, input) ? null : findControlWidget(node, input);
        if (!live || isDenied(nodeType(node), input) || isHost(node) || node.isVirtualNode
            || nodeType(node) === LEGACY_PRIMITIVE || COMPANION_WIDGETS.has(input)
            || widget?.options?.serialize === false || widget?.serialize === false) {
            miss("unavailable"); continue;
        }
        claimed.add(key);
        result.restorable.push({binding_id, path: String(node.id), input});
    }
    return result;
}

// Anchor writes keep any other entry the node carries, valid or not.
export function setAnchor(node, name, id) {
    node.properties ||= {};
    node.properties[ANCHOR_PROPERTY] = { ...(rawAnchors(node) || {}), [name]: id };
}

// Definition-only orphan ids cannot be restored by resolveBindings, but are
// still authored recovery state and must appear in the explicit cleanup review.
export function definitionAnchorIds(root) {
    return [...new Set(graphsOf(root).filter(g => g !== root)
        .flatMap(g => nodesOf(g).flatMap(n => Object.values(readAnchors(n)))))];
}

export function dropAllAnchors(root, id) {
    return graphsOf(root).flatMap(graph => dropAnchors(graph, id));
}

export function applyCleanupPlan(manifest, plan) {
    for (const id of plan.bindings || []) delete manifest.bindings[id];
    for (const id of plan.execution || []) {
        if (isPlainObject(manifest.execution?.controls)) delete manifest.execution.controls[id];
    }
    for (const row of plan.preset_members || []) {
        const preset = manifest.presets?.[row.preset_id];
        const table = row.execution_id ? "execution" : "members";
        if (!isPlainObject(preset?.[table])) throw new Error("The choice changed. Reopen the cleanup review.");
        delete preset[table][row.execution_id || row.binding_id];
        const left = table === "members" ? preset.execution : preset.members;
        if (!Object.keys(preset[table]).length && !(isPlainObject(left) && Object.keys(left).length)) {
            delete manifest.presets[row.preset_id];
            dropChoiceFromCategories(manifest, row.preset_id);
        }
    }
}

// Removes every root anchor carrying `id`; returns the nodes it changed.
export function dropAnchors(root, id) {
    const changed = [];
    for (const node of nodesOf(root)) {
        const raw = rawAnchors(node);
        if (!raw || !Object.values(raw).includes(id)) continue;
        const next = Object.fromEntries(Object.entries(raw).filter(([, value]) => value !== id));
        if (Object.keys(next).length) node.properties[ANCHOR_PROPERTY] = next;
        else delete node.properties[ANCHOR_PROPERTY];
        changed.push(node);
    }
    return changed;
}

// A JSON copy, not structuredClone: Nodes 2.0 makes `node.properties` a
// reactive proxy, which structuredClone refuses. Anchor maps hold only text.
export const anchorSnapshot = node => {
    const raw = rawAnchors(node);
    return raw ? JSON.parse(JSON.stringify(raw)) : undefined;
};

export function restoreAnchors(node, snapshot) {
    if (snapshot === undefined) {
        if (node.properties) delete node.properties[ANCHOR_PROPERTY];
    } else {
        node.properties ||= {};
        node.properties[ANCHOR_PROPERTY] = snapshot;
    }
}

export const publicTargets = targets =>
    (targets || []).slice(0, MAX_TARGETS).map(({ path, input }) => ({ path, input }));

// ── Node switches (execution Controls) ───────────────────────────────────────

export function readExecutionAnchor(node) {
    const id = node?.properties?.[EXECUTION_ANCHOR];
    return typeof id === "string" && ID_RE.test(id) && !FORBIDDEN_KEYS.has(id) ? id : null;
}

export function setExecutionAnchor(node, id) {
    node.properties ||= {};
    node.properties[EXECUTION_ANCHOR] = id;
}

// The raw property, for an exact rollback (it may hold an invalid value).
export const executionAnchorSnapshot = node =>
    (node?.properties && Object.hasOwn(node.properties, EXECUTION_ANCHOR) ? { value: node.properties[EXECUTION_ANCHOR] } : undefined);

export function restoreExecutionAnchor(node, snapshot) {
    if (snapshot === undefined) {
        if (node.properties) delete node.properties[EXECUTION_ANCHOR];
    } else {
        node.properties ||= {};
        node.properties[EXECUTION_ANCHOR] = snapshot.value;
    }
}

// Removes every execution anchor carrying `id`, inside subgraph definitions
// too (one left there would resurface on unpacking); returns the nodes changed.
export function dropExecutionAnchors(root, id) {
    const changed = [];
    for (const node of graphsOf(root).flatMap(nodesOf)) {
        if (node?.properties?.[EXECUTION_ANCHOR] !== id) continue;
        delete node.properties[EXECUTION_ANCHOR];
        changed.push(node);
    }
    return changed;
}

// A canvas group's nodes by the native rule (LGraphGroup.recomputeInsideNodes,
// frontend 1.57.1): a root node whose boundingRect centre lies inside the
// group's bounding (measure.ts containsCentre/isInRect), nested groups' nodes
// included. Computed here because recomputing mutates the group's caches and
// re-sorts graph.groups, which an export must never do.
export function groupMembers(group, nodes) {
    const box = group?._bounding || group?.bounding;
    if (!box || box.length < 4) return [];
    return (nodes || []).filter(node => {
        const rect = node?.boundingRect;
        if (!rect || rect.length < 4) return false;
        const x = rect[0] + rect[2] * 0.5, y = rect[1] + rect[3] * 0.5;
        return x >= box[0] && x < box[0] + box[2] && y >= box[1] && y < box[1] + box[3];
    });
}

// A switch acts on nodes that run: a subgraph host counts (frontend 1.53.10
// reports isVirtualNode on it), a reroute or note does not.
const switchable = node => !!node && (isHost(node) || !node.isVirtualNode);

// The infrastructure classes a node is or holds: an instance whose definition
// (at any depth) has the Editor or the Relay would switch them off too.
function infrastructureOf(node, seen = new Set()) {
    const found = new Set(INFRASTRUCTURE_CLASSES.has(nodeType(node)) ? [nodeType(node)] : []);
    const graph = node?.subgraph;
    if (graph && !seen.has(graph)) {
        seen.add(graph);
        for (const inner of nodesOf(graph)) for (const type of infrastructureOf(inner, seen)) found.add(type);
    }
    return found;
}
const holdsInfrastructure = node => infrastructureOf(node).size > 0;
const rootGroups = root => root?.groups || root?._groups || [];

// A switch's default label (control_execution.default_label): its group's or
// node's title, else the kind.
function switchLabel(resolved, entry) {
    const first = resolved?.members?.[0];
    return resolved?.title || first?.title || first?.class_type || (entry?.target?.kind === "group" ? "Group" : "Node");
}

function switchMember(node) {
    return { path: String(node.id), class_type: nodeType(node), mode: modeName(node),
        kind: isHost(node) ? "instance" : "node", title: String(node.title || nodeType(node)), node };
}

const combinedMode = members => {
    const modes = new Set(members.map(member => member.mode));
    return modes.size === 1 ? [...modes][0] : modes.size ? "mixed" : null;
};

function resolveSwitch(root, entry, hits) {
    const kind = entry.target.kind;
    if (kind === "group") {
        const group = rootGroups(root).find(g => g?.id === entry.target.group_id);
        if (!group) return { status: "missing", kind, members: [] };
        const nodes = groupMembers(group, nodesOf(root)).filter(switchable);
        const base = { kind, group, title: String(group.title || "Group"), member_count: nodes.length,
            members: nodes.slice(0, MAX_EXECUTION_MEMBERS).map(switchMember) };
        if (!nodes.length) return { ...base, status: "unsupported", problem: "empty" };
        if (nodes.some(holdsInfrastructure)) return { ...base, status: "unsupported", problem: "infrastructure" };
        if (nodes.length > MAX_EXECUTION_MEMBERS) return { ...base, status: "unsupported", problem: "too_many" };
        return { ...base, status: "ok", mode: combinedMode(base.members) };
    }
    if (hits.length > 1) return { status: "duplicate", kind, members: [] };
    if (!hits.length) return { status: "missing", kind, members: [] };
    const node = hits[0], member = switchMember(node);
    const base = { kind, node, title: member.title, member_count: 1, members: [member] };
    if ((kind === "instance") !== isHost(node)) return { ...base, status: "unsupported", problem: "kind" };
    if (holdsInfrastructure(node)) return { ...base, status: "unsupported", problem: "infrastructure" };
    if (!switchable(node)) return { ...base, status: "unsupported", problem: "virtual" };
    return { ...base, status: "ok", mode: member.mode };
}

// Every switch of a manifest on the live graph: {status, kind, members:
// [{path, class_type, mode, kind, title, node}], mode, problem}. Never writes.
export function resolveExecution(root, manifest) {
    const { controls, error } = readExecution(manifest?.execution);
    const anchored = new Map();
    for (const node of nodesOf(root)) {
        const id = readExecutionAnchor(node);
        if (!id) continue;
        if (!anchored.has(id)) anchored.set(id, []);
        anchored.get(id).push(node);
    }
    const resolved = Object.create(null);
    for (const [id, entry] of Object.entries(controls)) resolved[id] = resolveSwitch(root, entry, anchored.get(id) || []);
    return { controls: resolved, entries: controls, error };
}

// What Manage may add as a switch: root nodes, root subgraph instances and root
// canvas groups. Infrastructure is sent too, so the server can say why it is
// refused; the server decides (control_execution.manage_candidates).
// `known` holds the manifest's switch ids: an anchor naming no switch (left by
// a deleted Relay) does not take its node, and a new switch may replace it.
function nodeCandidate(node, known) {
    const owner = readExecutionAnchor(node);
    return { kind: isHost(node) ? "instance" : "node", path: String(node.id), title: String(node.title || nodeType(node)),
        classes: [...new Set([nodeType(node), ...infrastructureOf(node)])], member_count: 1, anchored_by: owner && (!known || known.has(owner)) ? owner : null };
}

function groupCandidate(root, group) {
    const members = groupMembers(group, nodesOf(root)).filter(switchable);
    // Infrastructure first: the server's cap must never cut it off.
    const classes = [...new Set(members.flatMap(node => [nodeType(node), ...infrastructureOf(node)]))]
        .sort((a, b) => Number(INFRASTRUCTURE_CLASSES.has(b)) - Number(INFRASTRUCTURE_CLASSES.has(a)))
        .slice(0, MAX_EXECUTION_MEMBERS + 1);
    return { kind: "group", group_id: group.id, title: String(group?.title || "Group"), classes, member_count: members.length, anchored_by: null };
}

// What Manage may add as a switch: root canvas groups (first, as they are few),
// root subgraph instances and root nodes. Infrastructure is sent too, so the
// server can say why it is refused; the server decides
// (control_execution.manage_candidates).
export function listExecutionCandidates(root, { filter = "", limit = MAX_CANDIDATES, known = null } = {}) {
    const query = String(filter || "").trim().toLowerCase();
    const matches = (...texts) => !query || texts.some(text => String(text || "").toLowerCase().includes(query));
    const found = [];
    let truncated = false;
    const push = entry => {
        if (found.length >= limit) { truncated = true; return; }
        found.push(entry);
    };
    for (const group of rootGroups(root)) {
        if (truncated) break;
        if (Number.isInteger(group?.id) && matches(group.title || "Group")) push(groupCandidate(root, group));
    }
    for (const node of nodesOf(root)) {
        if (truncated) break;
        if (switchable(node) && matches(node.title || nodeType(node), nodeType(node))) push(nodeCandidate(node, known));
    }
    return { candidates: found, truncated };
}

// Only the candidates a batch names, so no listing cap can drop one.
export function executionCandidatesFor(root, locators, { known = null } = {}) {
    const found = [];
    for (const locator of locators || []) {
        const live = resolveExecutionCandidate(root, locator);
        if (live?.group) found.push(groupCandidate(root, live.group));
        else if (live?.node) found.push(nodeCandidate(live.node, known));
    }
    return found;
}

// One candidate on the live graph, for a structural write.
export function resolveExecutionCandidate(root, candidate) {
    if (candidate?.kind === "group") {
        const group = rootGroups(root).find(g => g?.id === candidate.group_id);
        return group ? { kind: "group", group } : null;
    }
    const node = nodesOf(root).find(n => String(n?.id) === String(candidate?.path));
    if (!node || !switchable(node) || (candidate.kind === "instance") !== isHost(node)) return null;
    return { kind: candidate.kind, node };
}

// The API-shaped body of a muted or bypassed node, as the native serializer
// would have exported it: widget values (the dynamic-prompt template, as
// restoreDynamicPrompts keeps it) and `[origin, slot]` for linked inputs, the
// origin qualified by `path`'s instance prefix as an execution id is. An input
// linked from the subgraph's input boundary takes its value from the host
// (`promoted`, as ExecutableNodeDTO.resolveInput does), or is left out when
// that value is unknown. Read and validated by the server, never queued.
export function inactiveBody(node, inactive, path = String(node?.id), promoted = {}) {
    const inputs = {};
    const prefix = path.includes(":") ? path.slice(0, path.lastIndexOf(":") + 1) : "";
    for (const widget of node.widgets || []) {
        if (!widget?.name || widget.options?.serialize === false) continue;
        inputs[widget.name] = Array.isArray(widget.value) ? { __value__: widget.value } : widget.value;
    }
    for (const input of node.inputs || []) {
        if (input?.link == null || !input.name) continue;
        const link = getGraphLink(node.graph, input.link);
        const name = input.widget?.name ?? input.name;
        if (link?.originIsIoNode || String(link?.origin_id) === String(SUBGRAPH_INPUT_ID)) {
            if (Object.hasOwn(promoted, name)) inputs[name] = promoted[name];
            else delete inputs[name];
            continue;
        }
        inputs[input.name] = [`${prefix}${link?.origin_id ?? ""}`, Number(link?.origin_slot ?? 0)];
    }
    return { class_type: nodeType(node), inputs, _meta: { title: String(node.title || nodeType(node)) },
        mode: inactive.mode, by: inactive.by, label: inactive.label };
}

export const MAX_INACTIVE_BODIES = 256;

// Bodies for every inactive node a binding targets, keyed by execution path.
// `first` and `last` add other nodes ({node, path, inactive}): a View's own
// source before the settings, every other muted output after them, so a long
// list of muted outputs never crowds out a setting's body. A body whose values
// cannot be serialized is left out: its setting then reads missing instead of
// turning the whole Controls view into an error.
export function inactiveBodies(resolvedBindings, { first = [], last = [] } = {}) {
    const bodies = {};
    const addExtra = list => {
        for (const { node, path, inactive } of list || []) {
            if (!node || !inactive || Object.hasOwn(bodies, path)) continue;
            if (Object.keys(bodies).length >= MAX_INACTIVE_BODIES) return;
            const body = inactiveBody(node, inactive, path);
            try { JSON.stringify(body); } catch { continue; }
            bodies[path] = body;
        }
    };
    addExtra(first);
    for (const resolved of Object.values(resolvedBindings || {})) {
        // A root target's node is its anchor's; a promoted target names its own,
        // and its value is the host's widget (a promoted input).
        const host = resolved?.anchor?.node;
        for (const target of resolved?.targets || []) {
            const node = target.node || (host && String(host.id) === target.path ? host : null);
            if (!target.inactive || !node || Object.hasOwn(bodies, target.path)) continue;
            if (Object.keys(bodies).length >= MAX_INACTIVE_BODIES) return bodies;
            const promoted = target.node && resolved.widget ? { [target.input]: resolved.widget.value } : {};
            const body = inactiveBody(node, target.inactive, target.path, promoted);
            try { JSON.stringify(body); } catch { continue; }
            bodies[target.path] = body;
        }
    }
    addExtra(last);
    return bodies;
}

// ── Output Views ─────────────────────────────────────────────────────────────

// Valid anchors only: `{rel: id}`, rel "" or an execution path inside the
// instance, ids by the manifest's rule.
export function readViewAnchors(node) {
    const raw = node?.properties?.[VIEWS_ANCHOR];
    const anchors = {};
    if (!isPlainObject(raw)) return anchors;
    for (const [rel, id] of Object.entries(raw)) {
        if (rel !== "" && !cleanPath(rel)) continue;
        if (typeof id === "string" && ID_RE.test(id) && !FORBIDDEN_KEYS.has(id)) anchors[rel] = id;
    }
    return anchors;
}

export function setViewAnchor(node, rel, id) {
    node.properties ||= {};
    const raw = isPlainObject(node.properties[VIEWS_ANCHOR]) ? node.properties[VIEWS_ANCHOR] : {};
    node.properties[VIEWS_ANCHOR] = { ...raw, [rel]: id };
}

// A JSON copy of the raw property, for an exact rollback (Nodes 2.0 proxies
// refuse structuredClone).
export const viewAnchorSnapshot = node => (node?.properties && Object.hasOwn(node.properties, VIEWS_ANCHOR)
    ? { value: JSON.parse(JSON.stringify(node.properties[VIEWS_ANCHOR] ?? null)) } : undefined);

export function restoreViewAnchors(node, snapshot) {
    if (snapshot === undefined) {
        if (node.properties) delete node.properties[VIEWS_ANCHOR];
    } else {
        node.properties ||= {};
        node.properties[VIEWS_ANCHOR] = snapshot.value;
    }
}

// Removes every View anchor carrying `id`, inside subgraph definitions too;
// returns the nodes changed.
export function dropViewAnchors(root, id) {
    const changed = [];
    for (const node of graphsOf(root).flatMap(nodesOf)) {
        const raw = node?.properties?.[VIEWS_ANCHOR];
        if (!isPlainObject(raw) || !Object.values(raw).includes(id)) continue;
        const next = Object.fromEntries(Object.entries(raw).filter(([, value]) => value !== id));
        if (Object.keys(next).length) node.properties[VIEWS_ANCHOR] = next;
        else delete node.properties[VIEWS_ANCHOR];
        changed.push(node);
    }
    return changed;
}

// The node an execution path names on the live graph, with the subgraph
// instances on the way (`hosts`, root first). Null when any step is missing.
export function resolveExecPath(root, path) {
    const parts = String(cleanPath(path) || "").split(":").filter(Boolean);
    if (!parts.length) return null;
    let graph = root, node = null;
    const hosts = [];
    for (const [index, part] of parts.entries()) {
        node = nodesOf(graph).find(n => String(n?.id) === part) || null;
        if (!node) return null;
        if (index < parts.length - 1) {
            if (!isHost(node)) return null;
            hosts.push(node);
            graph = node.subgraph;
        }
    }
    return { node, hosts };
}

// Whether a node's class is an output node (LiteGraph nodeData.output_node).
export function isOutputNodeOf(node) {
    const data = node?.constructor?.nodeData ?? globalThis.LiteGraph?.registered_node_types?.[nodeType(node)]?.nodeData;
    return data?.output_node === true;
}

// Why the node on a path does not run: itself, or an instance on the way.
function pathOffReason(node, hosts) {
    for (const host of hosts) if (isOff(host)) return { mode: modeName(host), by: "parent", label: String(host.title || nodeType(host)) };
    return offReason(node, null);
}

// Whether a node path can take a new View on the live graph, for the pane
// picker's "seen" rows: {missing: true} | {inactive: reason} | null (it can).
// The canvas sends bodies only for some switched-off nodes, so the server
// refuses any other one that is off.
export function viewPathState(root, path) {
    const resolved = root ? resolveExecPath(root, path) : null;
    if (!resolved) return { missing: true };
    const reason = pathOffReason(resolved.node, resolved.hosts);
    return reason ? { inactive: reason } : null;
}

// Every View of a manifest on the live graph: {status, path, class_type,
// title, node, host, rel, participation, inactiveReason}. An anchor naming no
// View (a deleted Relay's, an Undo's) is ignored. Never writes.
export function resolveViews(root, manifest, { isOutputNode = isOutputNodeOf } = {}) {
    const { items, error } = readViews(manifest?.views);
    const hits = new Map();
    for (const node of nodesOf(root)) {
        for (const [rel, id] of Object.entries(readViewAnchors(node))) {
            if (!Object.hasOwn(items, id)) continue;
            if (!hits.has(id)) hits.set(id, []);
            hits.get(id).push({ host: node, rel });
        }
    }
    const views = Object.create(null);
    for (const id of Object.keys(items)) {
        const found = hits.get(id) || [];
        if (found.length > 1) { views[id] = { status: "duplicate" }; continue; }
        if (!found.length) { views[id] = { status: "missing" }; continue; }
        const { host, rel } = found[0];
        const path = rel ? `${host.id}:${rel}` : String(host.id);
        const live = resolveExecPath(root, path);
        if (!live) { views[id] = { status: "missing", host, rel }; continue; }
        const { node, hosts } = live;
        const base = { path, class_type: nodeType(node), title: String(node.title || nodeType(node)), node, host, rel };
        if (INFRASTRUCTURE_CLASSES.has(nodeType(node)) || (!isHost(node) && node.isVirtualNode)) { views[id] = { ...base, status: "unsupported" }; continue; }
        // A sampling View may name any node but an output node, which keeps its
        // output View (a sampler, Model Preview Override, a node a recent run saw
        // publish previews); an output View needs an output node.
        const sampling = viewFeed(items[id]) === VIEW_FEED_SAMPLING;
        if (isHost(node) || (sampling ? isOutputNode(node) : !isOutputNode(node))) { views[id] = { ...base, status: "kind" }; continue; }
        const inactive = pathOffReason(node, hosts);
        views[id] = { ...base, status: "ok", participation: inactive ? "inactive" : "active", ...(inactive ? { inactiveReason: inactive } : {}) };
    }
    return { views, items, error };
}

// The View anchors of the whole graph, for the writer's fingerprint.
export const viewAnchorList = root => nodesOf(root).map(node => [node?.id, readViewAnchors(node)]);

// The export form of the Views section: saved presentation plus each View's
// source facts; an entry the reader skips, or another version's section, is
// exported exactly as saved.
function exportViews(saved, root, manifest) {
    const { views, items, error } = resolveViews(root, manifest);
    if (error || !isPlainObject(saved) || !isPlainObject(saved.items)) return saved;
    const out = { ...saved, items: {} };
    for (const [id, entry] of Object.entries(saved.items)) {
        const resolved = views[id];
        if (!resolved || !items[id] || !isPlainObject(entry)) { out.items[id] = entry; continue; }
        const clean = { ...entry };
        for (const key of VIEW_EXPORT_KEYS) delete clean[key];
        out.items[id] = {
            ...clean, status: resolved.status,
            ...(resolved.path ? { path: resolved.path, class_type: resolved.class_type, title: resolved.title.slice(0, EXPORT_TITLE_MAX) } : {}),
            ...(resolved.participation ? { participation: resolved.participation } : {}),
            ...(resolved.inactiveReason ? { inactive_reason: resolved.inactiveReason } : {}),
        };
    }
    return out;
}

// Muted and bypassed output nodes and core sampling-preview sources
// (`SAMPLING_CLASSES`), root and inside instances, with their path and reason:
// the server lists them as View candidates that are off.
export function inactiveOutputs(root, { isOutputNode = isOutputNodeOf, limit = MAX_INACTIVE_BODIES } = {}) {
    const found = [];
    const walk = (graph, prefix, parent, depth) => {
        if (depth > MAX_NESTING) return;
        for (const node of nodesOf(graph)) {
            if (found.length >= limit) return;
            const path = prefix ? `${prefix}:${node?.id}` : String(node?.id);
            if (!cleanPath(path)) continue;
            const own = isOff(node) ? { mode: modeName(node), by: "self", label: String(node.title || nodeType(node)) } : null;
            if (isHost(node)) { walk(node.subgraph, path, parent || (own ? { ...own, by: "parent" } : null), depth + 1); continue; }
            const reason = parent || own;
            if (reason && !node.isVirtualNode && (isOutputNode(node) || SAMPLING_CLASSES.has(nodeType(node)))
                && !INFRASTRUCTURE_CLASSES.has(nodeType(node))) found.push({ node, path, inactive: reason });
        }
    };
    walk(root, "", null, 0);
    return found;
}

// ── Routing capsule (Phase 1B) ───────────────────────────────────────────────
// An API export holds only the nodes that ran, links already resolved, so the
// Sonder App can't tell what a muted or bypassed node would change. The export
// carries this capsule beside it (server/control_routing.py compiles it): each
// switch member's ports, where each of its inputs resolves, and every input of
// every serialized node whose resolution first reaches a member's output.
// Resolution mirrors ExecutableNodeDTO.resolveInput/resolveOutput (frontend
// 1.57.1) over the live graph and its current modes, stopping at a member's
// output, where the app's chosen mode decides.
export const MAX_ROUTING_NODES = 512;
export const MAX_ROUTING_EDGES = 2048;
export const MAX_ROUTING_BYTES = 48 * 1024;
const ROUTING_VERSION = 1;

class RoutingUnsupported extends Error {}

// LiteGraph.isValidConnection without the EVENT/ACTION pair
// (control_routing.is_valid_connection mirrors it).
export function isValidConnection(a, b) {
    const wild = t => t == null || t === "" || t === "*" || t === 0 || t === false;
    if (wild(a) || wild(b) || a === b) return true;
    const x = String(a).toLowerCase(), y = String(b).toLowerCase();
    if (!x.includes(",") && !y.includes(",")) return x === y;
    return x.split(",").some(p => y.split(",").some(q => isValidConnection(p, q)));
}

// ExecutableNodeDTO._getBypassSlotIndex (control_routing.bypass_index).
export function bypassIndex(inputTypes, outputType, slot, type) {
    if (type === "*" || type === "") return inputTypes.length > slot ? slot : 0;
    if (slot < inputTypes.length && isValidConnection(inputTypes[slot], outputType) && isValidConnection(inputTypes[slot], type)) return slot;
    const exact = inputTypes.findIndex(t => t === type);
    if (exact !== -1) return exact;
    return inputTypes.findIndex(t => isValidConnection(t, outputType) && isValidConnection(t, type));
}

const slotType = type => (type == null ? null : String(type));

// A DTO here is {node, hosts}: hosts are the subgraph instances from the root
// down to the one holding `node`, so its execution id is their ids then its own.
function routingResolver(root, members) {
    const graphOf = dto => dto.node.graph || dto.hosts.at(-1)?.subgraph || root;
    const execId = dto => [...dto.hosts.map(h => h.id), dto.node.id].join(":");
    const visit = (visited, key) => {
        if (visited.has(key)) throw new RoutingUnsupported("loop");
        visited.add(key);
    };
    function resolveInput(dto, slot, visited, type) {
        const host = dto.hosts.at(-1);
        visit(visited, `${host?.subgraph?.id}:${dto.node.id}[I]${slot}`);
        const input = dto.node.inputs?.[slot];
        if (!input) throw new RoutingUnsupported("slot");
        if (input.link == null) return null;
        const link = getGraphLink(graphOf(dto), input.link);
        if (!link) throw new RoutingUnsupported("link");
        if (host && (link.originIsIoNode || String(link.origin_id) === String(SUBGRAPH_INPUT_ID))) {
            const hostInput = host.inputs?.[link.origin_slot];
            if (!hostInput) throw new RoutingUnsupported("slot");
            if (hostInput.link == null) {
                // The host's promoted widget value; nothing without a widget.
                const widget = host.getWidgetFromSlot?.(hostInput);
                return widget ? { w: widget.value } : null;
            }
            // Up and out of the subgraph: the host's input, with its own type.
            return resolveInput({ node: host, hosts: dto.hosts.slice(0, -1) }, link.origin_slot, visited);
        }
        const origin = getGraphNode(graphOf(dto), link.origin_id);
        if (!origin) throw new RoutingUnsupported("node");
        return resolveOutput({ node: origin, hosts: dto.hosts }, link.origin_slot, type ?? input.type, visited);
    }
    function resolveOutput(dto, slot, type, visited) {
        visit(visited, `${dto.hosts.at(-1)?.subgraph?.id}:${dto.node.id}[O]${slot}`);
        if (!dto.hosts.length && members.has(String(dto.node.id))) return { m: [String(dto.node.id), slot, slotType(type)] };
        if (dto.node.mode === MUTED) return null;
        if (dto.node.mode === BYPASS) {
            const output = dto.node.outputs?.[slot];
            if (!output) throw new RoutingUnsupported("slot");
            const index = bypassIndex((dto.node.inputs || []).map(i => i?.type), output.type, slot, type);
            return index === -1 ? null : resolveInput(dto, index, visited);
        }
        if (isHost(dto.node)) return resolveSubgraphOutput(dto, slot, type, visited);
        if (dto.node.isVirtualNode) {
            const source = dto.node.resolveVirtualOutput?.(slot);
            if (source) {
                const graph = source.node?.graph;
                const hosts = graph === graphOf(dto) ? dto.hosts : graph === root ? [] : null;
                if (!hosts) throw new RoutingUnsupported("virtual");
                return resolveOutput({ node: source.node, hosts }, source.slot, type, visited);
            }
            // getInputLink may be another node's link (KJNodes' GetNode returns its
            // SetNode's): native resolves on the link's target, as here.
            const link = dto.node.getInputLink?.(slot);
            if (!link) return null;
            const target = getGraphNode(graphOf(dto), link.target_id);
            if (!target) throw new RoutingUnsupported("virtual");
            return resolveInput({ node: target, hosts: dto.hosts }, link.target_slot, visited, type);
        }
        return { a: [execId(dto), slot] };
    }
    function resolveSubgraphOutput(dto, slot, type, visited) {
        if (!dto.node.outputs?.[slot]) throw new RoutingUnsupported("slot");
        const inner = dto.node.resolveSubgraphOutputLink?.(slot);
        if (!inner) return null;
        if (!inner.outputNode) throw new RoutingUnsupported("passthrough");
        return resolveOutput({ node: inner.outputNode, hosts: [...dto.hosts, dto.node] }, inner.link.origin_slot, type, visited);
    }
    // A member host's output as one asking type resolves it (a bypass inside
    // picks its input by that type). A route native would refuse is marked and
    // refused only if a run reaches it.
    function hostOutput(dto, slot, type) {
        try {
            return resolveSubgraphOutput(dto, slot, type, new Set());
        } catch (error) {
            if (error instanceof RoutingUnsupported) return { x: 1 };
            throw error;
        }
    }
    return { resolveInput, hostOutput, execId };
}

// The nodes graphToPrompt writes: root nodes and, inside an instance that
// runs, every inner node (instances nested in it are always expanded).
function* serializedNodes(root) {
    const runs = node => node.mode !== MUTED && node.mode !== BYPASS;
    function* inner(host, hosts) {
        for (const node of nodesOf(host.subgraph)) {
            if (isHost(node)) yield* inner(node, [...hosts, host]);
            else if (!node.isVirtualNode && runs(node)) yield { node, hosts: [...hosts, host] };
        }
    }
    for (const node of nodesOf(root)) {
        if (isHost(node)) {
            if (runs(node)) yield* inner(node, []);
        } else if (!node.isVirtualNode && runs(node)) yield { node, hosts: [] };
    }
}

// What graphToPrompt leaves in an input that resolves to nothing: the value of
// the widget of the same name, boxed as it boxes it.
function fallbackOf(node, name) {
    const widget = (node.widgets || []).findLast(w => w?.name === name && w.options?.serialize !== false);
    if (!widget) return undefined;
    const value = widget.value;
    if (widget.type === "curve" && value != null) return { __type__: "CURVE", __value__: value };
    return Array.isArray(value) ? { __value__: value } : value;
}

export function exportRouting(root, memberPaths) {
    const members = new Set(memberPaths);
    try {
        if (!members.size) return { v: ROUTING_VERSION, nodes: {}, edges: [] };
        const { resolveInput, hostOutput, execId } = routingResolver(root, members);
        const nodes = {}, hosts = new Map();
        // Each (host, output, type) a hop asks for; resolved after every asker is known.
        const asked = new Map(), pending = [];
        const ask = hop => {
            if (!hop?.m || !hosts.has(hop.m[0])) return;
            const key = JSON.stringify(hop.m);
            if (!asked.has(key)) { asked.set(key, null); pending.push(hop.m); }
        };
        for (const node of nodesOf(root)) {
            const path = String(node?.id);
            if (!members.has(path)) continue;
            if (isHost(node)) hosts.set(path, node);
            nodes[path] = {
                i: (node.inputs || []).map((input, slot) => [String(input?.name ?? ""), slotType(input?.type),
                    resolveInput({ node, hosts: [] }, slot, new Set())]),
            };
        }
        if (Object.keys(nodes).length > MAX_ROUTING_NODES) return { v: ROUTING_VERSION, error: "budget" };
        for (const node of Object.values(nodes)) for (const port of node.i) ask(port[2]);
        const edges = [];
        for (const dto of serializedNodes(root)) {
            for (const [slot, input] of (dto.node.inputs || []).entries()) {
                if (input?.link == null) continue;
                const hop = resolveInput(dto, slot, new Set());
                if (!hop?.m) continue;
                if (edges.length >= MAX_ROUTING_EDGES) return { v: ROUTING_VERSION, error: "budget" };
                const fallback = fallbackOf(dto.node, input.name);
                edges.push([execId(dto), String(input.name), ...hop.m, ...(fallback === undefined ? [] : [fallback])]);
                ask(hop);
            }
        }
        while (pending.length) {
            const [path, slot, type] = pending.pop();
            const hop = hostOutput({ node: hosts.get(path), hosts: [] }, slot, type);
            asked.set(JSON.stringify([path, slot, type]), hop);
            ask(hop);
        }
        for (const [path, node] of Object.entries(nodes)) {
            const live = hosts.get(path) || nodesOf(root).find(n => String(n?.id) === path);
            node.o = (live.outputs || []).map((output, slot) => {
                if (!hosts.has(path)) return [slotType(output?.type)];
                const answers = [...asked].map(([key, hop]) => [JSON.parse(key), hop])
                    .filter(([[p, s]]) => p === path && s === slot).map(([[, , type], hop]) => [type, hop]);
                return [slotType(output?.type), answers];
            });
        }
        return { v: ROUTING_VERSION, nodes, edges };
    } catch {
        return { v: ROUTING_VERSION, error: "unsupported" };
    }
}

// The capsule as export text, or its budget refusal.
export function routingText(root, memberPaths) {
    let text = JSON.stringify(exportRouting(root, memberPaths));
    if (utf8Bytes(text) > MAX_ROUTING_BYTES) text = JSON.stringify({ v: ROUTING_VERSION, error: "budget" });
    return text;
}

// The export form of each switch: saved presentation plus its state.
// The exported members are bounded apart from the saved manifest: they travel
// in every export, which the server caps at 128 KiB. A switch past the budget
// exports as unavailable rather than turning the whole Relay into an error.
export const MAX_EXPORTED_MEMBERS = 512;
const EXPORT_TITLE_MAX = 48;

function exportExecution(saved, root, manifest) {
    const { controls, entries, error } = resolveExecution(root, manifest);
    if (error || !isPlainObject(saved) || !isPlainObject(saved.controls)) return saved;
    const out = { ...saved, controls: {} };
    let exported = 0;
    const routed = new Set();
    for (const [id, entry] of Object.entries(saved.controls)) {
        const resolved = controls[id];
        // An entry the reader skips is exported exactly as saved.
        if (!resolved || !entries[id] || !isPlainObject(entry)) { out.controls[id] = entry; continue; }
        const clean = { ...entry };
        for (const key of EXECUTION_EXPORT_KEYS) delete clean[key];
        const title = text => String(text || "").slice(0, EXPORT_TITLE_MAX);
        const members = resolved.members.map(({ path, class_type, mode, kind, title: name }) => ({ path, class_type, mode, kind, title: title(name) }));
        const overBudget = exported + members.length > MAX_EXPORTED_MEMBERS;
        if (!overBudget) exported += members.length;
        out.controls[id] = {
            ...clean,
            status: overBudget ? "unsupported" : resolved.status,
            ...(overBudget ? { problem: "budget" } : resolved.problem ? { problem: resolved.problem } : {}),
            ...(resolved.title ? { title: title(resolved.title) } : {}),
            member_count: resolved.member_count ?? resolved.members.length,
            members: overBudget ? [] : members,
        };
        // Only a switch whose nodes all run now can be switched in the Sonder
        // App: an off node is not in this export to switch back on.
        if (!overBudget && resolved.status === "ok" && members.every(m => m.mode === "enabled")) {
            for (const member of members) routed.add(member.path);
        }
    }
    out.routing = routingText(root, [...routed]);
    return out;
}

// The API export of a manifest: saved presentation plus each binding's
// targets, status and seed mode. Pure: never writes the widget or the graph,
// and never throws, because a throw here would stop every Run of the workflow
// (graphToPrompt awaits serializeValue without a catch).
export function exportManifest(text, root) {
    let manifest;
    try {
        manifest = parseManifest(text);
    } catch {
        return typeof text === "string" ? text : ""; // the server reports why
    }
    try {
        const { bindings, suggestions } = resolveBindings(root, manifest);
        const out = { ...manifest, bindings: {} };
        for (const [id, entry] of Object.entries(manifest.bindings)) {
            const resolved = bindings[id];
            out.bindings[id] = {
                ...(isPlainObject(entry) ? entry : {}),
                status: resolved.status,
                targets: publicTargets(resolved.targets),
                seed_mode: resolved.modeWidget?.value ?? null,
            };
            delete out.bindings[id].restore_targets;
            delete out.bindings[id].rows_unavailable;
            delete out.bindings[id].participation;
            delete out.bindings[id].inactive_reason;
            if (resolved.rows && !resolved.rows.ready) out.bindings[id].rows_unavailable = true;
            // An inactive binding's node is absent from this export; the reason
            // lets an importer say why, since it cannot read a missing body.
            if (resolved.participation && resolved.participation !== "active") {
                out.bindings[id].participation = resolved.participation;
                out.bindings[id].inactive_reason = resolved.inactiveReason;
            }
            if (resolved.status === "missing" && !resolved.anchor && !resolved.insideSubgraph) {
                const hint = entry?.restore_targets ?? entry?.targets;
                if (hint?.length) out.bindings[id].restore_targets = hint;
            }
        }
        out.suggestions = suggestions.map(s => ({ binding_id: s.binding_id, targets: publicTargets(s.targets) }));
        if (manifest.execution !== undefined) out.execution = exportExecution(manifest.execution, root, manifest);
        if (manifest.views !== undefined) out.views = exportViews(manifest.views, root, manifest);
        let exported = JSON.stringify(out);
        // The capsule shares the server's 128 KiB cap with everything else, and
        // a graph edit alone can grow it: past the cap it gives way, never the
        // Relay (its switches then read "too large" in the Sonder App).
        if (utf8Bytes(exported) > MAX_MANIFEST_BYTES && typeof out.execution?.routing === "string") {
            out.execution = { ...out.execution, routing: JSON.stringify({ v: ROUTING_VERSION, error: "budget" }) };
            exported = JSON.stringify(out);
        }
        return exported;
    } catch (error) {
        return JSON.stringify({ ...manifest, error: String(error?.message || error || "Controls export failed.") });
    }
}

// ── Relays ───────────────────────────────────────────────────────────────────

export const isRelay = node => nodeType(node) === RELAY_CLASS;

// What an export cannot show: muted, bypassed and nested Relays.
export function relayCensus(root) {
    const census = { active: 0, muted: 0, bypassed: 0, nested: 0 }, active = [];
    for (const node of nodesOf(root)) {
        if (!isRelay(node)) continue;
        if (node.mode === MUTED) census.muted++;
        else if (node.mode === BYPASS) census.bypassed++;
        else {
            census.active++;
            active.push(node);
        }
    }
    for (const graph of graphsOf(root)) {
        if (graph !== root) census.nested += nodesOf(graph).filter(isRelay).length;
    }
    return { census, active };
}
