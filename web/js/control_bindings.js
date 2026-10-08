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

export const RELAY_CLASS = "SonderControlRelay";
export const ANCHOR_PROPERTY = "sonderBindings";
export const MANIFEST_VERSION = 1;
// Mirrors server/control_manifest.py (Phase 0 P10 hygiene).
export const MAX_MANIFEST_BYTES = 128 * 1024;
export const MAX_BINDINGS = 256;
export const MAX_NOTES = 256;
export const MAX_SUGGESTIONS = 256;
export const MAX_TARGETS = 64;
export const MAX_DEPTH = 6;
export const LABEL_MAX = 120;
export const NOTE_MAX = 4000;
export const ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
export const FORBIDDEN_KEYS = new Set(["__proto__", "constructor", "prototype"]);
// Mirrors server/binding_policy.py; hides candidates early, the server decides.
export const DENIED_INPUTS = {
    SonderControlRelay: ["manifest"],
    SonderDriverSelector: ["driver_lane_index", "driver_selector_overrides_json"],
    SonderEditor: ["mask_post_offset", "mask_pre_offset", "post_context_frames", "pre_context_frames", "project",
        "render_cache_max_bytes", "render_queue_active", "scene_id", "selection_end", "selection_start",
        "take_crop_position", "take_fit_mode", "take_placement_linked", "take_placement_mode", "take_placement_muted"],
    SonderGuidesBridgeStart: ["bridge_overrides_json", "iteration_index"],
    SonderReferenceSelector: ["reference_lanes"],
    SonderSaveBridge: ["target_folder"],
};
export const DENIED_PREFIXES = { SonderMetadataCollector: "label_", SonderMetadataCollectorV3: "label_" };

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
        if (!isConfiguring() && hasAnchors(this)) delete this.properties[ANCHOR_PROPERTY];
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

export const emptyManifest = () => ({ v: MANIFEST_VERSION, bindings: {}, notes: {}, groups: {} });

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
    for (const name of ["bindings", "notes", "groups"]) {
        if (raw[name] == null) raw[name] = {};
        if (!isPlainObject(raw[name])) throw new Error("The Controls manifest is malformed.");
    }
    if (Object.keys(raw.bindings).length > MAX_BINDINGS) throw new Error(`At most ${MAX_BINDINGS} Controls fit in one workflow.`);
    if (Object.keys(raw.notes).length > MAX_NOTES) throw new Error(`At most ${MAX_NOTES} notes fit in one workflow.`);
    for (const id of [...Object.keys(raw.bindings), ...Object.keys(raw.notes)]) {
        if (!ID_RE.test(id)) throw new Error("A Controls id is invalid.");
    }
    return raw;
}

// The saved text after `mutate` edits a copy of the manifest. Export-only keys
// never reach saved text.
export function editManifest(text, mutate) {
    const manifest = structuredClone(parseManifest(text));
    delete manifest.suggestions;
    delete manifest.error;
    for (const entry of Object.values(manifest.bindings)) {
        if (isPlainObject(entry)) for (const key of ["status", "targets", "seed_mode"]) delete entry[key];
    }
    mutate(manifest);
    manifest.v = MANIFEST_VERSION;
    return JSON.stringify(manifest);
}

// ── Resolve ──────────────────────────────────────────────────────────────────

// Consumers of a promoted input, through nested hosts: a promoted input may
// feed several inner widgets, each its own API target.
function promotedTargets(host, name, prefix, depth = 0) {
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
        if (isOff(inner)) {
            found.push({ path, input: widgetName, status: "missing" });
        } else if (isHost(inner)) {
            found.push(...promotedTargets(inner, input.name, path, depth + 1));
        } else {
            const widget = findControlWidget(inner, widgetName);
            found.push({ path, input: widgetName, status: widget ? "ok" : "no_widget", node: inner, widget });
        }
    }
    return found;
}

function hostInput(host, name) {
    return (host.inputs || []).find(input => input?._subgraphSlot?.name === name || input?.name === name) || null;
}

// One binding on the live graph: its status, its native widget and its API
// targets. Never writes.
function resolveAnchor(node, name) {
    if (isOff(node)) return { status: "missing", targets: [] };
    if (isHost(node)) {
        const input = hostInput(node, name);
        if (!input) return { status: "missing", targets: [] };
        const targets = promotedTargets(node, name, String(node.id));
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
    const targets = [{ path: String(node.id), input: name }];
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

export const publicTargets = targets =>
    (targets || []).slice(0, MAX_TARGETS).map(({ path, input }) => ({ path, input }));

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
        }
        out.suggestions = suggestions.map(s => ({ binding_id: s.binding_id, targets: publicTargets(s.targets) }));
        return JSON.stringify(out);
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
