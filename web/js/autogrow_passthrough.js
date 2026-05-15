import { app } from "/scripts/app.js";

const VALUE_RE = /^value_(\d+)$/;
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

const STATE = Symbol("sonderAutogrowState");

export const slotValueIndex = (slot) => {
    if (!slot || typeof slot.name !== "string") return -1;
    const match = VALUE_RE.exec(slot.name);
    return match ? parseInt(match[1], 10) : -1;
};

export const slotIsConnected = (slot) => {
    if (!slot) return false;
    if (Array.isArray(slot.links)) return slot.links.length > 0;
    return slot.link != null;
};

export const findInputIndex = (node, name) =>
    (node?.inputs || []).findIndex((slot) => slot?.name === name);
export const findOutputIndex = (node, name) =>
    (node?.outputs || []).findIndex((slot) => slot?.name === name);
export const findInputByName = (node, name) => {
    const index = findInputIndex(node, name);
    return index >= 0 ? node.inputs[index] : null;
};
export const findOutputByName = (node, name) => {
    const index = findOutputIndex(node, name);
    return index >= 0 ? node.outputs[index] : null;
};

const getGraph = (node) => node?.graph || app.graph || null;

const getLink = (node, linkId) => {
    if (linkId == null) return null;
    const links = getGraph(node)?.links;
    if (!links) return null;
    if (typeof links.get === "function") return links.get(linkId) || null;
    return links[linkId] || null;
};

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

const ensureState = (node) => {
    if (node[STATE]) return node[STATE];
    const inputMeta = new Map();
    const outputMeta = new Map();
    for (const slot of node.inputs || []) {
        if (slotValueIndex(slot) < 0) continue;
        inputMeta.set(slot.name, { type: "*", tooltip: slot.tooltip || "" });
    }
    for (const slot of node.outputs || []) {
        if (slotValueIndex(slot) < 0) continue;
        outputMeta.set(slot.name, { type: "*", tooltip: slot.tooltip || "" });
    }
    node[STATE] = { inputMeta, outputMeta };
    return node[STATE];
};

const ensureValueInput = (node, index) => {
    const name = `value_${index}`;
    if (findInputByName(node, name)) return;
    const meta = ensureState(node).inputMeta.get(name) || { type: "*", tooltip: "" };
    node.addInput(name, meta.type, { tooltip: meta.tooltip });
};

const ensureValueOutput = (node, index) => {
    const name = `value_${index}`;
    if (findOutputByName(node, name)) return;
    const meta = ensureState(node).outputMeta.get(name) || { type: "*", tooltip: "" };
    node.addOutput(name, meta.type, { tooltip: meta.tooltip });
};

const removeValueInputIfUnconnected = (node, index) => {
    const idx = findInputIndex(node, `value_${index}`);
    if (idx < 0) return false;
    if (slotIsConnected(node.inputs[idx])) return false;
    node.removeInput(idx);
    return true;
};

const removeValueOutputIfUnconnected = (node, index) => {
    const idx = findOutputIndex(node, `value_${index}`);
    if (idx < 0) return false;
    if (slotIsConnected(node.outputs[idx])) return false;
    node.removeOutput(idx);
    return true;
};

const maxConnectedValueIndex = (node) => {
    let max = -1;
    for (const slot of node?.inputs || []) {
        const index = slotValueIndex(slot);
        if (index >= 0 && slotIsConnected(slot)) max = Math.max(max, index);
    }
    for (const slot of node?.outputs || []) {
        const index = slotValueIndex(slot);
        if (index >= 0 && slotIsConnected(slot)) max = Math.max(max, index);
    }
    return max;
};

const resolveValueTypeLabel = (node, peer, index) => {
    const candidates = [node, peer].filter(Boolean);
    for (const candidate of candidates) {
        const input = (candidate.inputs || []).find((slot) => slotValueIndex(slot) === index);
        if (input?.link != null) {
            const label = getInputTypeLabel(candidate, input);
            if (label) return label;
        }
    }
    for (const candidate of candidates) {
        const output = (candidate.outputs || []).find((slot) => slotValueIndex(slot) === index);
        if (output && slotIsConnected(output)) {
            const label = getOutputTypeLabel(candidate, output);
            if (label) return label;
        }
    }
    return null;
};

const applyShape = (node, count, maxCount, { inputs = true, outputs = true } = {}) => {
    let mutated = false;
    for (let index = 0; index < count; index += 1) {
        if (inputs && !findInputByName(node, `value_${index}`)) {
            ensureValueInput(node, index);
            mutated = true;
        }
        if (outputs && !findOutputByName(node, `value_${index}`)) {
            ensureValueOutput(node, index);
            mutated = true;
        }
    }
    for (let index = maxCount - 1; index >= count; index -= 1) {
        if (inputs && removeValueInputIfUnconnected(node, index)) mutated = true;
        if (outputs && removeValueOutputIfUnconnected(node, index)) mutated = true;
    }
    return mutated;
};

const applyTypeLabels = (node, peer, count) => {
    for (const slot of node?.inputs || []) {
        const index = slotValueIndex(slot);
        if (index < 0 || index >= count) continue;
        const label = resolveValueTypeLabel(node, peer, index) || `value_${index}`;
        slot.label = label;
        slot.localized_name = label;
    }
    for (const slot of node?.outputs || []) {
        const index = slotValueIndex(slot);
        if (index < 0 || index >= count) continue;
        const label = resolveValueTypeLabel(node, peer, index) || `value_${index}`;
        slot.label = label;
        slot.localized_name = label;
    }
};

export function refreshAutogrowShape(node, { maxCount, peer = null, inputs = true, outputs = true } = {}) {
    if (!node || !maxCount) return;
    ensureState(node);
    const local = maxConnectedValueIndex(node);
    const peerMax = peer ? maxConnectedValueIndex(peer) : -1;
    const desired = Math.min(maxCount, Math.max(1, Math.max(local, peerMax) + 2));
    const mutated = applyShape(node, desired, maxCount, { inputs, outputs });
    applyTypeLabels(node, peer, desired);
    if (mutated && typeof node.computeSize === "function" && typeof node.setSize === "function") {
        node.setSize(node.computeSize());
    }
    node.setDirtyCanvas?.(true, true);
}
