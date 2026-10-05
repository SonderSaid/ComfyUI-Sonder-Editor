// Sonder Control on the canvas.
//
// Keeps the hidden `kind` widget in step with the inputs the Control feeds,
// through reroutes and subgraph boundaries, and re-checks it before every Run
// (`beforeQueued`). Copies a seed target's bounds and current value, mints the
// stable `control_id` (re-minted when a paste collides), shows the seed or the
// value widget for its kind, and refuses a new link that the server's describe
// marks `unsupported`, instead of letting ComfyUI fail the run with a raw error.
// The server re-checks the kind at execute; this hook only keeps it current.
import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";
import { commitWidgetVisibility, setWidgetHidden } from "./widget_visibility.js";
import { getGraphLink, getGraphNode } from "./project_source_resolver.js";
import { seedModeWidget } from "./control_widgets.js";

const EXT_NAME = "sonder.control";
const CONTROL_CLASS = "SonderControl";
const NOTE_CLASS = "SonderPanelNote";
const OUTPUT_SIDE = 2;
// Frontend 1.53.10 subgraph boundary node ids; the input id arrives as a string.
const SUBGRAPH_INPUT_ID = -10;
const SUBGRAPH_OUTPUT_ID = -20;
const ANY_KIND = "*";
const CONTROL_KINDS = new Set(["STRING", "INT", "FLOAT", "BOOLEAN", "COMBO", "SEED"]);
const SEED_MODES = new Set(["fixed", "increment", "decrement", "randomize"]);
// The `seed` widget's schema range (nodes/sonder_control.py SEED_MAX).
const SEED_MAX = 0xFFFFFFFFFFFFFFFF;
const VALUE_TYPES = new Set(["INT", "FLOAT", "BOOLEAN", "STRING"]);
const WILDCARD_TYPES = new Set(["*", "COMFY_MATCHTYPE_V3"]);
// The frontend gives these INT inputs a seed control even without the flag.
const SEED_INPUT_NAMES = new Set(["seed", "noise_seed"]);
const ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789";
const ID_LENGTH = 8;
const DESCRIBE_DELAY_MS = 250;
const MAX_WALK_DEPTH = 32;
const DESCRIBE_URL = "/sonder-editor/controls/describe";

const INPUT_SPECS = new Map();
const NODE_STATE = Symbol("sonderControlState");

const nodeTypeName = (node) => String(node?.comfyClass || node?.type || "");
const isControl = (node) => nodeTypeName(node) === CONTROL_CLASS;
const isPanelNote = (node) => nodeTypeName(node) === NOTE_CLASS;
const identityWidgetName = (node) => isControl(node) ? "control_id" : isPanelNote(node) ? "note_id" : null;
const findWidget = (node, name) => (node?.widgets || []).find((widget) => widget?.name === name) || null;
const rootGraph = () => app.rootGraph || app.graph || null;
const graphNodes = (graph) => graph?._nodes || graph?.nodes || [];
const valuesOf = (container) => {
    if (!container) return [];
    if (typeof container.values === "function") return [...container.values()];
    return Object.values(container);
};

function stateOf(node) {
    if (!node[NODE_STATE]) {
        node[NODE_STATE] = { timer: null, pendingLinks: new Set(), installed: false, seedBounds: { min: 0, max: SEED_MAX } };
    }
    return node[NODE_STATE];
}

function captureSpecs(nodeData) {
    const name = String(nodeData?.name || "");
    if (!name) return;
    const specs = {};
    for (const category of ["required", "optional"]) {
        const entries = nodeData?.input?.[category];
        if (!entries || typeof entries !== "object") continue;
        for (const [inputName, spec] of Object.entries(entries)) {
            if (Array.isArray(spec)) specs[inputName] = spec;
        }
    }
    INPUT_SPECS.set(name, specs);
}

// ── Kinds (mirror server/control_descriptors.py) ─────────────────────────────

function inputKind(node, input) {
    const spec = INPUT_SPECS.get(nodeTypeName(node))?.[input?.name] || null;
    const type = spec ? spec[0] : input?.type;
    const options = spec && spec[1] && typeof spec[1] === "object" ? spec[1] : {};
    if (Array.isArray(type) || type === "COMBO") return "COMBO";
    if (type === "INT") {
        // The frontend's own rule: an explicit flag wins over the input name.
        return (options.control_after_generate ?? SEED_INPUT_NAMES.has(String(input?.name))) ? "SEED" : "INT";
    }
    if (VALUE_TYPES.has(type)) return type;
    if (WILDCARD_TYPES.has(type)) return ANY_KIND;
    return null;
}

function deriveKind(kinds) {
    const concrete = new Set(kinds.filter((kind) => kind !== ANY_KIND));
    if (!concrete.size) return ANY_KIND;
    if ([...concrete].every((kind) => kind === "INT" || kind === "SEED")) {
        return concrete.has("SEED") ? "SEED" : "INT";
    }
    return concrete.size === 1 ? [...concrete][0] : null;
}

// ── Targets ──────────────────────────────────────────────────────────────────

function allGraphs() {
    const root = rootGraph();
    if (!root) return [];
    return [root, ...valuesOf(root.subgraphs)];
}

function subgraphInstances(subgraph) {
    const instances = [];
    for (const graph of allGraphs()) {
        for (const node of graphNodes(graph)) {
            if (node?.subgraph === subgraph) instances.push(node);
        }
    }
    return instances;
}

const inputWidget = (node, input) => findWidget(node, input?.widget?.name || input?.name);

// `promoted` is the outermost subgraph widget on the path: a promoted input
// keeps its current value there, not on the inner widget.
function followLink(graph, link, found, depth, promoted = null) {
    if (!link || depth > MAX_WALK_DEPTH) return;
    if (Number(link.target_id) === SUBGRAPH_OUTPUT_ID) {
        // Leaving a subgraph: the value continues from every instance's output.
        for (const instance of subgraphInstances(graph)) {
            for (const linkId of instance.outputs?.[Number(link.target_slot)]?.links || []) {
                followLink(instance.graph, getGraphLink(instance.graph, linkId), found, depth + 1);
            }
        }
        return;
    }
    const target = getGraphNode(graph, link.target_id);
    if (!target) return;
    if (nodeTypeName(target) === "Reroute") {
        for (const linkId of target.outputs?.[0]?.links || []) {
            followLink(graph, getGraphLink(graph, linkId), found, depth + 1, promoted);
        }
        return;
    }
    const input = target.inputs?.[Number(link.target_slot)];
    if (!input) return;
    const inner = target.subgraph;
    if (inner) {
        // Entering a subgraph: follow the links leaving its matching input.
        const index = (inner.inputs || []).findIndex((slot) => slot?.name === input.name);
        if (index < 0) return;
        const via = promoted || inputWidget(target, input);
        for (const innerLink of valuesOf(inner.links)) {
            if (Number(innerLink?.origin_id) === SUBGRAPH_INPUT_ID && Number(innerLink?.origin_slot) === index) {
                followLink(inner, innerLink, found, depth + 1, via);
            }
        }
        return;
    }
    found.push({ node: target, input, promoted });
}

function controlTargets(node, linkIds = null) {
    const found = [];
    const output = node?.outputs?.[0];
    if (!output) return found;
    const ids = linkIds ? [...linkIds] : output.links || [];
    for (const linkId of ids) followLink(node.graph, getGraphLink(node.graph, linkId), found, 0);
    return found;
}

const targetWidget = (target) => inputWidget(target.node, target.input);
const targetValue = (target) => (target.promoted || targetWidget(target))?.value;
const targetSeedMode = (target) => seedModeWidget(target.promoted || targetWidget(target))?.value;

// Every target accepts any type, so nothing tells the Control its kind.
function feedsOnlyWildcards(node) {
    const targets = controlTargets(node);
    return targets.length > 0 && targets.every((t) => inputKind(t.node, t.input) === ANY_KIND);
}

// ── Widgets ──────────────────────────────────────────────────────────────────

function seedControlWidgets(node) {
    const seed = findWidget(node, "seed");
    return Array.isArray(seed?.linkedWidgets) ? seed.linkedWidgets : [];
}

// `kind` is shown only when the targets cannot set it, so the user picks it.
function refreshVisibility(node, choosesKind = feedsOnlyWildcards(node)) {
    const isSeed = findWidget(node, "kind")?.value === "SEED";
    let changed = false;
    changed = setWidgetHidden(findWidget(node, "kind"), !choosesKind) || changed;
    changed = setWidgetHidden(findWidget(node, "control_id"), true) || changed;
    changed = setWidgetHidden(findWidget(node, "seed_mode"), true) || changed;
    changed = setWidgetHidden(findWidget(node, "value_text"), isSeed) || changed;
    changed = setWidgetHidden(findWidget(node, "seed"), !isSeed) || changed;
    for (const control of seedControlWidgets(node)) {
        changed = setWidgetHidden(control, !isSeed) || changed;
    }
    if (changed) commitWidgetVisibility(node, { resize: !app.configuringGraph });
}

function applySeedTargets(node, targets, enteringSeed) {
    const seed = findWidget(node, "seed");
    if (!seed) return;
    const widgets = targets.map(targetWidget).filter(Boolean);
    const mins = widgets.map((w) => Number(w.options?.min)).filter(Number.isFinite);
    const maxs = widgets.map((w) => Number(w.options?.max)).filter(Number.isFinite);
    // Narrow within the Control's own range: /prompt checks its seed literal
    // against the schema (min 0), whatever the target allows.
    const own = stateOf(node).seedBounds;
    seed.options = seed.options || {};
    seed.options.min = Math.max(own.min, ...mins);
    seed.options.max = Math.min(own.max, ...maxs);
    if (!enteringSeed) return;
    // Take over the seed the target already had, and move it like a seed.
    const current = targets.map(targetValue).find((value) => Number.isFinite(value));
    if (current !== undefined) {
        seed.value = Math.min(Math.max(current, seed.options.min), seed.options.max);
    }
    // Keep the target's own mode (a fixed seed stays fixed); a target without
    // one gets ComfyUI's default.
    const mode = targets.map(targetSeedMode).find(Boolean) || "randomize";
    for (const control of seedControlWidgets(node)) {
        if (control.value === "fixed") control.value = mode;
    }
}

function adoptTargetValue(node, targets) {
    const valueText = findWidget(node, "value_text");
    if (!valueText || String(valueText.value ?? "") !== "") return;
    for (const target of targets) {
        const value = targetValue(target);
        if (value === undefined || value === null || typeof value === "object") continue;
        valueText.value = String(value);
        return;
    }
}

function recomputeKind(node) {
    const kindWidget = findWidget(node, "kind");
    if (!kindWidget || !node.graph) return;
    const targets = controlTargets(node);
    const usable = [];
    for (const target of targets) {
        const kind = inputKind(target.node, target.input);
        if (kind !== null) usable.push({ ...target, kind });
    }
    const derived = deriveKind(usable.map((target) => target.kind));
    if (derived === null || derived === ANY_KIND) {
        refreshVisibility(node, derived === ANY_KIND && targets.length > 0 && usable.length === targets.length);
        return;
    }
    const previous = kindWidget.value;
    if (previous !== derived) kindWidget.value = derived;
    if (derived === "SEED") {
        applySeedTargets(node, usable.filter((t) => t.kind === "SEED" || t.kind === "INT"), previous !== "SEED");
    } else {
        if (previous === "SEED") {
            for (const control of seedControlWidgets(node)) control.value = "fixed";
        }
        adoptTargetValue(node, usable);
    }
    refreshVisibility(node, false);
    if (previous !== derived) node.setDirtyCanvas?.(true, true);
}

// ── Identity ─────────────────────────────────────────────────────────────────

function mintPanelId() {
    const bytes = new Uint8Array(ID_LENGTH);
    globalThis.crypto.getRandomValues(bytes);
    return Array.from(bytes, (byte) => ID_ALPHABET[byte % ID_ALPHABET.length]).join("");
}

function panelIdTaken(node, id) {
    const name = identityWidgetName(node);
    for (const graph of allGraphs()) {
        for (const other of graphNodes(graph)) {
            if (other !== node && identityWidgetName(other) === name && findWidget(other, name)?.value === id) return true;
        }
    }
    return false;
}

// A pasted subgraph node gets its own copy of the definition (frontend
// 1.53.10), whose Controls are configured rather than created, so they keep the
// copied ids. Re-mint those that now collide; one shared definition holds a
// single Control node, which never collides with itself.
function resolveSubgraphCollisions(instance, depth = 0) {
    if (!instance?.subgraph || depth > MAX_WALK_DEPTH) return;
    for (const inner of graphNodes(instance.subgraph)) {
        if (identityWidgetName(inner)) ensurePanelId(inner, { resolveCollision: true });
        else if (inner?.subgraph) resolveSubgraphCollisions(inner, depth + 1);
    }
}

function ensurePanelId(node, { resolveCollision }) {
    const widget = findWidget(node, identityWidgetName(node));
    if (!widget) return;
    const current = typeof widget.value === "string" ? widget.value.trim() : "";
    if (current && !(resolveCollision && panelIdTaken(node, current))) return;
    let next = mintPanelId();
    while (panelIdTaken(node, next)) next = mintPanelId();
    widget.value = next;
}

function refreshNoteVisibility(node) {
    if (setWidgetHidden(findWidget(node, "note_id"), true)) {
        commitWidgetVisibility(node, { resize: !app.configuringGraph });
    }
}

// ── Unsupported links ────────────────────────────────────────────────────────

function warn(detail) {
    const toast = app.extensionManager?.toast;
    if (typeof toast?.add === "function") {
        toast.add({ severity: "warn", summary: "Sonder Control", detail, life: 8000 });
    } else {
        console.warn("[Sonder Control]", detail);
    }
}

async function checkNewLinks(node) {
    const state = stateOf(node);
    const linkIds = [...state.pendingLinks];
    state.pendingLinks.clear();
    const controlId = findWidget(node, "control_id")?.value;
    if (!controlId || !node.graph || !linkIds.length) return;
    let descriptor = null;
    try {
        const prompt = (await app.graphToPrompt())?.output;
        if (!prompt) return;
        const response = await api.fetchApi(DESCRIBE_URL, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ prompt }),
        });
        if (!response.ok) return;
        const payload = await response.json();
        descriptor = (payload?.controls || []).find((entry) => entry?.control_id === controlId) || null;
    } catch (error) {
        console.warn("[Sonder Control] describe failed:", error);
        return;
    }
    if (!descriptor || !node.graph) return;
    const refused = (descriptor.targets || []).filter((target) => target?.status === "unsupported");
    // Only the links just made are refused; a refused target is matched by its
    // node type and input, since the API key of a subgraph target is ambiguous.
    const graph = node.graph;
    const reasons = [];
    for (const linkId of refused.length ? linkIds : []) {
        if (!getGraphLink(graph, linkId)) continue;
        const reached = controlTargets(node, [linkId]);
        const match = refused.find((target) => reached.some(
            (r) => nodeTypeName(r.node) === target.class_type && r.input?.name === target.input,
        ));
        if (!match) continue;
        graph.removeLink?.(linkId);
        reasons.push(`${match.class_type} ${match.input}: ${match.reason || "not supported"}`);
    }
    if (reasons.length) {
        node.setDirtyCanvas?.(true, true);
        warn(`A Sonder Control can't drive ${reasons.join("; ")}`);
        return;
    }
    // The server sees targets the canvas walk cannot, such as inputs behind a
    // virtual Set/Get pair, so its concrete kind wins over a stale one.
    const kindWidget = findWidget(node, "kind");
    if (descriptor.status === "stale_kind" && CONTROL_KINDS.has(descriptor.target_kind) && kindWidget) {
        kindWidget.value = descriptor.target_kind;
        refreshVisibility(node);
        node.setDirtyCanvas?.(true, true);
        return;
    }
    // A cached Control is not re-checked against a new target (its cache key
    // holds only its own widgets), so say now that the value does not fit.
    if (descriptor.status === "ok" && descriptor.error) {
        warn(`${descriptor.label || "Sonder Control"}: ${descriptor.error} Change the value before the next run.`);
    }
}

function scheduleLinkCheck(node, linkId) {
    const state = stateOf(node);
    if (linkId != null) state.pendingLinks.add(linkId);
    if (state.timer) window.clearTimeout(state.timer);
    state.timer = window.setTimeout(() => {
        state.timer = null;
        checkNewLinks(node);
    }, DESCRIBE_DELAY_MS);
}

// ── Install ──────────────────────────────────────────────────────────────────

function install(node) {
    if (isPanelNote(node)) {
        refreshNoteVisibility(node);
        return;
    }
    if (!isControl(node)) return;
    const state = stateOf(node);
    if (state.installed) return;
    state.installed = true;

    const seed = findWidget(node, "seed");
    const ownMin = Number(seed?.options?.min);
    const ownMax = Number(seed?.options?.max);
    if (Number.isFinite(ownMin)) state.seedBounds.min = ownMin;
    if (Number.isFinite(ownMax)) state.seedBounds.max = ownMax;

    const valueText = findWidget(node, "value_text");
    if (valueText) {
        const originalSerialize = valueText.serializeValue;
        // Every serialization (Run, Export (API), describe) settles the kind
        // first. `value_text` serializes before `seed` and `kind`, so a seed
        // takeover lands in the same prompt.
        valueText.serializeValue = async function (...args) {
            try {
                recomputeKind(node);
            } catch (error) {
                console.warn("[Sonder Control] kind refresh failed:", error);
            }
            return originalSerialize ? originalSerialize.apply(this, args) : this.value;
        };
    }

    const kindWidget = findWidget(node, "kind");
    if (kindWidget) {
        const originalBeforeQueued = kindWidget.beforeQueued;
        // Every canvas Run serializes the current wiring, so settle the kind first.
        kindWidget.beforeQueued = function (...args) {
            try {
                recomputeKind(node);
            } catch (error) {
                console.warn("[Sonder Control] kind refresh failed:", error);
            }
            return originalBeforeQueued?.apply(this, args);
        };
    }

    const seedMode = findWidget(node, "seed_mode");
    if (seedMode) {
        // Export (API) serializes widgets but does not call beforeQueued.
        // The real ComfyUI seed-mode widget remains the canvas authority.
        seedMode.serializeValue = function () {
            this.value = seedModeWidget(findWidget(node, "seed"))?.value || "fixed";
            return this.value;
        };
    }

    const originalConnections = node.onConnectionsChange;
    node.onConnectionsChange = function (side, slot, connected, linkInfo, ...rest) {
        const result = originalConnections?.call(this, side, slot, connected, linkInfo, ...rest);
        if (side === OUTPUT_SIDE && !app.configuringGraph) {
            window.setTimeout(() => {
                try {
                    recomputeKind(this);
                } catch (error) {
                    console.warn("[Sonder Control] kind refresh failed:", error);
                }
            }, 0);
            if (connected && linkInfo?.id != null) scheduleLinkCheck(this, linkInfo.id);
        }
        return result;
    };

    const originalRemoved = node.onRemoved;
    node.onRemoved = function (...args) {
        if (state.timer) window.clearTimeout(state.timer);
        state.timer = null;
        state.pendingLinks.clear();
        return originalRemoved?.apply(this, args);
    };

    refreshVisibility(node);
}

app.registerExtension({
    name: EXT_NAME,
    beforeRegisterNodeDef(_nodeType, nodeData) {
        captureSpecs(nodeData);
    },
    nodeCreated(node) {
        if (node?.subgraph && !app.configuringGraph) {
            window.setTimeout(() => resolveSubgraphCollisions(node), 0);
            return;
        }
        if (!identityWidgetName(node)) return;
        install(node);
        if (app.configuringGraph) return;
        // A paste or clone configures the node after creation, so its copied
        // id is visible only on the next tick.
        window.setTimeout(() => ensurePanelId(node, { resolveCollision: true }), 0);
    },
    loadedGraphNode(node) {
        if (!identityWidgetName(node)) return;
        install(node);
        ensurePanelId(node, { resolveCollision: false });
        if (isControl(node)) refreshVisibility(node);
        else refreshNoteVisibility(node);
    },
});
