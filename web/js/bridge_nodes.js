// Sonder Guides Bridge — frontend extension for paired loop nodes.
// - Hides Start's iteration_index widget (internal loop counter).
// - Autogrows value_i passthrough slots: shows n+1 visible slots where n is
//   the highest connected index, mirrored across the Start/End peer pair.
//   Uses node.addInput/addOutput/removeInput/removeOutput for physical slot
//   management because LiteGraph does not honour `slot.hidden` for layout.
// - Auto-wires Start.project source into End.project on connect.
// - Updates value_i slot labels with inferred types from connected wires;
//   slot type stays literally "*" so any wire still connects.

import { app } from "/scripts/app.js";

const EXT_NAME = "sonder.bridge";
const TARGET_START = "SonderGuidesBridgeStart";
const TARGET_END = "SonderGuidesBridgeEnd";
const MAX_PASSTHROUGH = 8; // mirror SONDER_BRIDGE_MAX_PASSTHROUGH default
const VALUE_RE = /^value_(\d+)$/;
const NODE_STATE = Symbol("sonderBridgeState");

const isStart = (node) => node?.comfyClass === TARGET_START;
const isEnd = (node) => node?.comfyClass === TARGET_END;
const isBridge = (node) => isStart(node) || isEnd(node);

const TYPE_ALIASES = new Map([
    ["AUDIO", "AUDIO"],
    ["CLIP", "CLIP"],
    ["CLIP VISION", "CLIPV"],
    ["CONDITIONING", "COND"],
    ["IMAGE", "IMAGE"],
    ["LATENT", "LATENT"],
    ["MASK", "MASK"],
    ["MODEL", "MODEL"],
    ["SIGMAS", "SIGMAS"],
    ["STRING", "TEXT"],
    ["TEXT", "TEXT"],
    ["VAE", "VAE"],
]);

// ── Widget hide helper (mirrors extension.js) ─────────────────────────
function hideWidget(widget) {
    if (!widget || widget.hidden) return;
    widget.hidden = true;
    widget._sonderBridgeOrigComputeSize = widget.computeSize;
    widget.computeSize = () => [0, -4];
}

// ── Slot helpers ──────────────────────────────────────────────────────
const slotValueIndex = (slot) => {
    if (!slot || typeof slot.name !== "string") return -1;
    const m = VALUE_RE.exec(slot.name);
    return m ? parseInt(m[1], 10) : -1;
};

const slotIsConnected = (slot) => {
    if (!slot) return false;
    if (Array.isArray(slot.links)) return slot.links.length > 0;
    return slot.link != null;
};

const findInputIndex = (node, name) =>
    (node?.inputs || []).findIndex((s) => s?.name === name);
const findOutputIndex = (node, name) =>
    (node?.outputs || []).findIndex((s) => s?.name === name);
const findInputByName = (node, name) => {
    const idx = findInputIndex(node, name);
    return idx >= 0 ? node.inputs[idx] : null;
};
const findOutputByName = (node, name) => {
    const idx = findOutputIndex(node, name);
    return idx >= 0 ? node.outputs[idx] : null;
};
const getGraph = (node) => node?.graph || app.graph || null;

const getLink = (node, linkId) => {
    if (linkId == null) return null;
    const graph = getGraph(node);
    const links = graph?.links;
    if (!links) return null;
    if (typeof links.get === "function") return links.get(linkId) || null;
    return links[linkId] || null;
};

// ── Peer detection (Start.flow_control output ↔ End.flow_control input) ─
const findPeer = (node) => {
    if (!node) return null;
    const graph = getGraph(node);
    if (!graph) return null;

    if (isStart(node)) {
        const flow = findOutputByName(node, "flow_control");
        const linkIds = Array.isArray(flow?.links)
            ? flow.links
            : flow?.link != null
                ? [flow.link]
                : [];
        for (const linkId of linkIds) {
            const link = getLink(node, linkId);
            if (!link) continue;
            const target = graph.getNodeById?.(link.target_id);
            if (isEnd(target)) return target;
        }
        return null;
    }

    if (isEnd(node)) {
        const flow = findInputByName(node, "flow_control");
        if (!flow || flow.link == null) return null;
        const link = getLink(node, flow.link);
        const source = graph.getNodeById?.(link?.origin_id);
        return isStart(source) ? source : null;
    }

    return null;
};

// ── Type label inference (port from lsv3_lazy_cluster_any_ui.js) ──────
const normalizeTypeString = (value) => {
    if (typeof value !== "string") return null;
    let cleaned = value.trim();
    if (!cleaned || cleaned === "*" || cleaned === "0") return null;
    const pathParts = cleaned.split(/[\\/]/).filter(Boolean);
    cleaned = pathParts.length > 0 ? pathParts[pathParts.length - 1] : cleaned;
    const namespaceParts = cleaned.split(/[:.]/).filter(Boolean);
    cleaned = namespaceParts.length > 0 ? namespaceParts[namespaceParts.length - 1] : cleaned;
    cleaned = cleaned
        .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
        .replace(/[_-]+/g, " ")
        .replace(/\s+/g, " ")
        .trim()
        .toUpperCase();
    if (!cleaned) return null;
    if (TYPE_ALIASES.has(cleaned)) return TYPE_ALIASES.get(cleaned);
    if (cleaned.includes("TEXT") || cleaned.includes("STRING")) return "TEXT";
    return cleaned;
};

const resolveTypeLabel = (...sources) => {
    const queue = [...sources];
    while (queue.length > 0) {
        const candidate = queue.shift();
        if (candidate == null) continue;
        if (Array.isArray(candidate)) {
            queue.unshift(...candidate);
            continue;
        }
        if (typeof candidate === "object") {
            queue.unshift(candidate.type, candidate.name, candidate.label, candidate.localized_name);
            continue;
        }
        const label = normalizeTypeString(candidate);
        if (label) return label;
    }
    return null;
};

const getInputTypeLabel = (node, slot) => {
    if (!slot || slot.link == null) return null;
    const link = getLink(node, slot.link);
    const origin = getGraph(node)?.getNodeById?.(link?.origin_id);
    const originSlot = link?.origin_slot != null ? origin?.outputs?.[link.origin_slot] : null;
    return resolveTypeLabel(link?.type, originSlot?.type, originSlot);
};

const getOutputTypeLabel = (node, slot) => {
    const linkIds = Array.isArray(slot?.links)
        ? slot.links
        : slot?.link != null
            ? [slot.link]
            : [];
    for (const linkId of linkIds) {
        const link = getLink(node, linkId);
        const target = getGraph(node)?.getNodeById?.(link?.target_id);
        const targetSlot = link?.target_slot != null ? target?.inputs?.[link.target_slot] : null;
        const label = resolveTypeLabel(link?.type, targetSlot?.type, targetSlot);
        if (label) return label;
    }
    return null;
};

// Resolve the best type label for a value_i index by looking at this node and its peer.
const resolveValueTypeLabel = (node, peer, index) => {
    const candidates = [node, peer].filter(Boolean);
    for (const n of candidates) {
        const inSlot = (n.inputs || []).find((s) => slotValueIndex(s) === index);
        if (inSlot && inSlot.link != null) {
            const label = getInputTypeLabel(n, inSlot);
            if (label) return label;
        }
    }
    for (const n of candidates) {
        const outSlot = (n.outputs || []).find((s) => slotValueIndex(s) === index);
        if (outSlot && slotIsConnected(outSlot)) {
            const label = getOutputTypeLabel(n, outSlot);
            if (label) return label;
        }
    }
    return null;
};

// ── Node-state snapshot (schema metadata so removed slots can be re-added) ─
const ensureNodeState = (node) => {
    if (node[NODE_STATE]) return node[NODE_STATE];
    const inputMeta = new Map();
    const outputMeta = new Map();
    for (const slot of node.inputs || []) {
        if (slotValueIndex(slot) < 0) continue;
        inputMeta.set(slot.name, {
            type: "*",
            tooltip: slot.tooltip || "",
        });
    }
    for (const slot of node.outputs || []) {
        if (slotValueIndex(slot) < 0) continue;
        outputMeta.set(slot.name, {
            type: "*",
            tooltip: slot.tooltip || "",
        });
    }
    const state = {
        inputMeta,
        outputMeta,
        initialized: false,
    };
    node[NODE_STATE] = state;
    return state;
};

// ── Slot management (physical add/remove via LiteGraph APIs) ──────────
const ensureValueInput = (node, index) => {
    const name = `value_${index}`;
    if (findInputByName(node, name)) return;
    const state = ensureNodeState(node);
    const meta = state.inputMeta.get(name) || { type: "*", tooltip: "" };
    node.addInput(name, meta.type, { tooltip: meta.tooltip });
};

const ensureValueOutput = (node, index) => {
    const name = `value_${index}`;
    if (findOutputByName(node, name)) return;
    const state = ensureNodeState(node);
    const meta = state.outputMeta.get(name) || { type: "*", tooltip: "" };
    node.addOutput(name, meta.type, { tooltip: meta.tooltip });
};

const removeValueInputIfUnconnected = (node, index) => {
    const name = `value_${index}`;
    const idx = findInputIndex(node, name);
    if (idx < 0) return false;
    const slot = node.inputs[idx];
    if (slotIsConnected(slot)) return false;
    node.removeInput(idx);
    return true;
};

const removeValueOutputIfUnconnected = (node, index) => {
    const name = `value_${index}`;
    const idx = findOutputIndex(node, name);
    if (idx < 0) return false;
    const slot = node.outputs[idx];
    if (slotIsConnected(slot)) return false;
    node.removeOutput(idx);
    return true;
};

// ── Autogrow rule: visible_count = max_connected_index + 2 (≥1, ≤MAX) ─
const maxConnectedValueIndex = (node) => {
    let max = -1;
    for (const s of node?.inputs || []) {
        const i = slotValueIndex(s);
        if (i >= 0 && slotIsConnected(s)) max = Math.max(max, i);
    }
    for (const s of node?.outputs || []) {
        const i = slotValueIndex(s);
        if (i >= 0 && slotIsConnected(s)) max = Math.max(max, i);
    }
    return max;
};

const computeVisibleCount = (node, peer) => {
    const local = maxConnectedValueIndex(node);
    const peerMax = peer ? maxConnectedValueIndex(peer) : -1;
    const highest = Math.max(local, peerMax);
    // n + 1 visible slots where n = highest connected index + 1 (so always one
    // empty slot ready beyond the last connection). Floor 1, ceiling MAX.
    return Math.min(MAX_PASSTHROUGH, Math.max(1, highest + 2));
};

const applyShape = (node, count) => {
    let mutated = false;
    // Add missing required slots (in increasing index order so they append in canonical order).
    for (let i = 0; i < count; i += 1) {
        if (!findInputByName(node, `value_${i}`)) {
            ensureValueInput(node, i);
            mutated = true;
        }
        if (!findOutputByName(node, `value_${i}`)) {
            ensureValueOutput(node, i);
            mutated = true;
        }
    }
    // Remove trailing unconnected slots above count (high-to-low so indices stay stable).
    for (let i = MAX_PASSTHROUGH - 1; i >= count; i -= 1) {
        if (removeValueInputIfUnconnected(node, i)) mutated = true;
        if (removeValueOutputIfUnconnected(node, i)) mutated = true;
    }
    return mutated;
};

const applyTypeLabels = (node, peer, count) => {
    for (const s of node?.inputs || []) {
        const i = slotValueIndex(s);
        if (i < 0 || i >= count) continue;
        const label = resolveValueTypeLabel(node, peer, i) || `value_${i}`;
        s.label = label;
        s.localized_name = label;
    }
    for (const s of node?.outputs || []) {
        const i = slotValueIndex(s);
        if (i < 0 || i >= count) continue;
        const label = resolveValueTypeLabel(node, peer, i) || `value_${i}`;
        s.label = label;
        s.localized_name = label;
    }
};

const refreshNodeShape = (node) => {
    if (!isBridge(node)) return;
    ensureNodeState(node);

    const peer = findPeer(node);
    const desired = peer
        ? Math.max(computeVisibleCount(node, peer), computeVisibleCount(peer, node))
        : computeVisibleCount(node, null);

    let mutated = applyShape(node, desired);
    applyTypeLabels(node, peer, desired);

    if (peer) {
        const peerMutated = applyShape(peer, desired);
        applyTypeLabels(peer, node, desired);
        if (peerMutated && typeof peer.computeSize === "function" && typeof peer.setSize === "function") {
            peer.setSize(peer.computeSize());
        }
        peer.setDirtyCanvas?.(true, true);
    }

    if (mutated && typeof node.computeSize === "function" && typeof node.setSize === "function") {
        node.setSize(node.computeSize());
    }
    node.setDirtyCanvas?.(true, true);
};

// ── Project auto-mirror (Start.project → End.project) ────────────────
const mirrorProjectWire = (start) => {
    if (!isStart(start)) return;
    const peer = findPeer(start);
    if (!peer) return;
    const graph = getGraph(start);
    if (!graph) return;

    const startProj = findInputByName(start, "project");
    const endProj = findInputByName(peer, "project");
    if (!startProj || !endProj) return;

    const endProjIndex = peer.inputs.indexOf(endProj);
    if (endProjIndex < 0) return;

    if (startProj.link == null) {
        if (endProj.link != null) {
            try {
                peer.disconnectInput(endProjIndex);
            } catch (_) { /* ignore */ }
        }
        return;
    }

    const link = getLink(start, startProj.link);
    if (!link) return;
    const source = graph.getNodeById?.(link.origin_id);
    if (!source) return;

    if (endProj.link != null) {
        const existing = getLink(peer, endProj.link);
        if (existing && existing.origin_id === link.origin_id && existing.origin_slot === link.origin_slot) {
            return;
        }
        try {
            peer.disconnectInput(endProjIndex);
        } catch (_) { /* ignore */ }
    }

    try {
        source.connect(link.origin_slot, peer, endProjIndex);
    } catch (e) {
        console.warn("[Sonder Bridge] failed to auto-wire project:", e);
    }
};

// ── Per-node install ─────────────────────────────────────────────────
const installNode = (node) => {
    if (!isBridge(node)) return;
    const state = ensureNodeState(node);
    if (state.initialized) return;
    state.initialized = true;

    if (isStart(node)) {
        const widget = (node.widgets || []).find((w) => w?.name === "iteration_index");
        if (widget) hideWidget(widget);
    }

    const original = node.onConnectionsChange;
    node.onConnectionsChange = function (...args) {
        const result = original?.apply(this, args);
        try {
            refreshNodeShape(this);
            if (isStart(this)) {
                mirrorProjectWire(this);
            }
        } catch (e) {
            console.warn("[Sonder Bridge] connection-change handler error:", e);
        }
        return result;
    };

    // Defer initial shape so post-load connection state is settled before measuring.
    window.setTimeout(() => {
        if (!isBridge(node)) return;
        try {
            refreshNodeShape(node);
            if (isStart(node)) mirrorProjectWire(node);
        } catch (e) {
            console.warn("[Sonder Bridge] initial-shape error:", e);
        }
    }, 0);
};

app.registerExtension({
    name: EXT_NAME,
    async nodeCreated(node) {
        if (!isBridge(node)) return;
        installNode(node);
    },
});
