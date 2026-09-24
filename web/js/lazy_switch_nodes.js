import { app } from "/scripts/app.js";
import { captureInputDefinition, requiredConsumerOutputNames } from "./consumer_input_requirements.js";

const EXT_NAME = "sonder.lazy_cluster";
const TARGET_CLUSTER = "SonderLazyCluster";
const MIN_BRANCHES = 2;
const MIN_LANES = 1;
const NODE_STATE = Symbol("sonderLazyClusterState");

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

const laneFallbackLabel = (lane) => (lane >= 0 && lane < 26 ? String.fromCharCode(65 + lane) : `L${lane + 1}`);
const inputLabel = (branch, lane, laneDisplay) => (lane === 0 ? `${branch}: ${laneDisplay}` : `   ${laneDisplay}`);

const parseInputSlot = (slot) => {
    if (!slot || typeof slot.name !== "string") return null;
    const match = /^b(\d+)_l(\d+)$/.exec(slot.name);
    if (!match) return null;
    return { branch: Number.parseInt(match[1], 10), lane: Number.parseInt(match[2], 10) };
};

const outputName = (lane) => laneFallbackLabel(lane);

const parseOutputSlot = (slot) => {
    if (!slot || typeof slot.name !== "string") return null;
    if (/^[A-Z]$/.test(slot.name)) return { lane: slot.name.charCodeAt(0) - 65 };
    const match = /^L(\d+)$/.exec(slot.name);
    if (!match) return null;
    return { lane: Number.parseInt(match[1], 10) - 1 };
};

const isTargetNode = (node) => node?.comfyClass === TARGET_CLUSTER;
const getWidget = (node, name) => (node.widgets || []).find((widget) => widget?.name === name) || null;
const getInputByName = (node, name) => (node.inputs || []).find((slot) => slot?.name === name) || null;
const getOutputByName = (node, name) => (node.outputs || []).find((slot) => slot?.name === name) || null;
const getGraph = (node) => node?.graph || app.graph || null;
const getGraphNodeById = (node, id) => getGraph(node)?.getNodeById?.(id) || null;

const slotHasOutputLinks = (slot) =>
    Array.isArray(slot?.links) ? slot.links.length > 0 : slot?.link != null;

const clampInt = (value, min, max, fallback) => {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.max(min, Math.min(max, Math.trunc(parsed)));
};

const toIndex = (value) => {
    const parsed = Number(value);
    return Number.isInteger(parsed) ? parsed : null;
};

const getLinkById = (node, linkId) => {
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
    if (/^SONDER_(cluster|gate)_lane_\d+$/i.test(cleaned)) return null;

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

const getInputLaneTypeLabel = (node, slot) => {
    if (!slot || slot.link == null) return null;

    const link = getLinkById(node, slot.link);
    const originNode = getGraphNodeById(node, link?.origin_id);
    const originSlotIndex = toIndex(link?.origin_slot);
    const originSlot = originSlotIndex != null ? originNode?.outputs?.[originSlotIndex] : null;

    return resolveTypeLabel(link?.type, originSlot?.type, originSlot);
};

const getOutputLaneTypeLabel = (node, slot) => {
    const linkIds = Array.isArray(slot?.links) ? slot.links : slot?.link != null ? [slot.link] : [];
    for (const linkId of linkIds) {
        const link = getLinkById(node, linkId);
        const targetNode = getGraphNodeById(node, link?.target_id);
        const targetSlotIndex = toIndex(link?.target_slot);
        const targetSlot = targetSlotIndex != null ? targetNode?.inputs?.[targetSlotIndex] : null;
        const label = resolveTypeLabel(link?.type, targetSlot?.type, targetSlot);
        if (label) return label;
    }
    return null;
};

const getLaneDisplayLabel = (node, lane) => {
    for (const slot of node.inputs || []) {
        const info = parseInputSlot(slot);
        if (!info || info.lane !== lane || slot.link == null) continue;
        const label = getInputLaneTypeLabel(node, slot);
        if (label) return label;
    }

    for (const slot of node.outputs || []) {
        const info = parseOutputSlot(slot);
        if (!info || info.lane !== lane || !slotHasOutputLinks(slot)) continue;
        const label = getOutputLaneTypeLabel(node, slot);
        if (label) return label;
    }

    return laneFallbackLabel(lane);
};

const getLaneDisplayLabels = (node, laneCount) => {
    const labels = new Map();
    for (let lane = 0; lane < laneCount; lane += 1) {
        labels.set(lane, getLaneDisplayLabel(node, lane));
    }
    return labels;
};

const getNodeState = (node) => {
    if (!node[NODE_STATE]) {
        const inputMeta = new Map();
        const outputMeta = new Map();
        let maxBranches = MIN_BRANCHES;
        let maxLanes = MIN_LANES;

        for (const slot of node.inputs || []) {
            const info = parseInputSlot(slot);
            if (!info) continue;
            inputMeta.set(slot.name, { type: slot.type, tooltip: slot.tooltip || "" });
            maxBranches = Math.max(maxBranches, info.branch + 1);
            maxLanes = Math.max(maxLanes, info.lane + 1);
        }

        for (const slot of node.outputs || []) {
            const info = parseOutputSlot(slot);
            if (!info) continue;
            outputMeta.set(slot.name, { type: slot.type, tooltip: slot.tooltip || "" });
            maxLanes = Math.max(maxLanes, info.lane + 1);
        }

        node[NODE_STATE] = {
            inputMeta,
            outputMeta,
            maxBranches,
            maxLanes,
            initialized: false,
        };
    }
    return node[NODE_STATE];
};

const addInputSlot = (node, branch, lane) => {
    const state = getNodeState(node);
    const name = `b${branch}_l${lane}`;
    if (getInputByName(node, name)) return;
    const meta = state.inputMeta.get(name);
    if (!meta) return;
    const laneDisplay = getLaneDisplayLabel(node, lane);
    node.addInput(name, meta.type, {
        display_name: inputLabel(branch, lane, laneDisplay),
        tooltip: meta.tooltip,
    });
};

const addOutputSlot = (node, lane) => {
    const state = getNodeState(node);
    const name = outputName(lane);
    if (getOutputByName(node, name)) return;
    const meta = state.outputMeta.get(name);
    if (!meta) return;
    const laneDisplay = getLaneDisplayLabel(node, lane);
    node.addOutput(name, meta.type, {
        display_name: laneDisplay,
        tooltip: meta.tooltip,
    });
};

const removeInputSlot = (node, name) => {
    const slot = getInputByName(node, name);
    if (!slot) return;
    const index = node.inputs.indexOf(slot);
    if (index >= 0) node.removeInput(index);
};

const removeOutputSlot = (node, name) => {
    const slot = getOutputByName(node, name);
    if (!slot) return;
    const index = node.outputs.indexOf(slot);
    if (index >= 0) node.removeOutput(index);
};

const getConnectedExtents = (node) => {
    let maxConnectedBranch = -1;
    let maxConnectedLane = -1;

    for (const slot of node.inputs || []) {
        const info = parseInputSlot(slot);
        if (!info || slot.link == null) continue;
        maxConnectedBranch = Math.max(maxConnectedBranch, info.branch);
        maxConnectedLane = Math.max(maxConnectedLane, info.lane);
    }

    for (const slot of node.outputs || []) {
        const info = parseOutputSlot(slot);
        if (!info || !slotHasOutputLinks(slot)) continue;
        maxConnectedLane = Math.max(maxConnectedLane, info.lane);
    }

    return { maxConnectedBranch, maxConnectedLane };
};

const updateBranchSelectionWidgets = (node, branchCount) => {
    const maxIndex = Math.max(0, branchCount - 1);
    const widget = getWidget(node, "select");
    if (!widget) return;
    widget.options = widget.options || {};
    widget.options.max = maxIndex;
    widget.value = clampInt(widget.value, 0, maxIndex, 0);
};

const normalizeCounts = (node) => {
    const state = getNodeState(node);
    const branchWidget = getWidget(node, "branches");
    const laneWidget = getWidget(node, "lanes");
    const { maxConnectedBranch, maxConnectedLane } = getConnectedExtents(node);

    const branchCount = Math.max(
        clampInt(branchWidget?.value, MIN_BRANCHES, state.maxBranches, MIN_BRANCHES),
        maxConnectedBranch + 1,
        MIN_BRANCHES,
    );
    const laneCount = Math.max(
        clampInt(laneWidget?.value, MIN_LANES, state.maxLanes, MIN_LANES),
        maxConnectedLane + 1,
        MIN_LANES,
    );

    if (branchWidget) branchWidget.value = branchCount;
    if (laneWidget) laneWidget.value = laneCount;
    updateBranchSelectionWidgets(node, branchCount);

    return { branchCount, laneCount };
};

const sortInputSlots = (node, compare) => {
    const inputs = node.inputs || [];
    const sorted = inputs.slice().sort(compare);
    const moves = inputs.flatMap((slot, index) => {
        const target = sorted.indexOf(slot);
        const link = getLinkById(node, slot.link);
        return link && target !== index ? [{ link, target }] : [];
    });
    // Endpoint setters update ComfyUI's link store. Park moved inputs outside
    // the live range first so swapping occupied positions cannot collide.
    for (const [index, { link }] of moves.entries()) link.target_slot = inputs.length + index;
    for (const link of node.graph?.floatingLinks?.values() || []) {
        if (String(link.target_id) === String(node.id)) {
            const target = sorted.indexOf(inputs[link.target_slot]);
            if (target >= 0) link.target_slot = target;
        }
    }
    node.inputs = sorted;
    for (const { link, target } of moves) link.target_slot = target;
};

const ensureNodeShape = (node) => {
    // During restore, ECS links still address transitional slot positions.
    // Reconcile in afterConfigureGraph, once widgets and link endpoints agree.
    if (app.configuringGraph) return;
    const state = getNodeState(node);
    const { branchCount, laneCount } = normalizeCounts(node);

    for (let lane = 0; lane < laneCount; lane += 1) addOutputSlot(node, lane);
    for (let lane = state.maxLanes - 1; lane >= laneCount; lane -= 1) {
        const name = outputName(lane);
        const slot = getOutputByName(node, name);
        if (slot && !slotHasOutputLinks(slot)) removeOutputSlot(node, name);
    }

    for (let branch = 0; branch < branchCount; branch += 1) {
        for (let lane = 0; lane < laneCount; lane += 1) {
            addInputSlot(node, branch, lane);
        }
    }

    const laneLabels = getLaneDisplayLabels(node, laneCount);
    const removableInputs = [];

    for (const slot of node.inputs || []) {
        const info = parseInputSlot(slot);
        if (!info) continue;
        const label = inputLabel(info.branch, info.lane, laneLabels.get(info.lane) || laneFallbackLabel(info.lane));
        slot.label = label;
        slot.localized_name = label;
        if (info.branch >= branchCount || info.lane >= laneCount) {
            if (slot.link == null) removableInputs.push(slot.name);
        }
    }

    for (const slot of node.outputs || []) {
        const info = parseOutputSlot(slot);
        if (!info) continue;
        const label = laneLabels.get(info.lane) || laneFallbackLabel(info.lane);
        slot.label = label;
        slot.localized_name = label;
    }

    for (const name of removableInputs) removeInputSlot(node, name);

    sortInputSlots(node, (left, right) => {
        const leftInfo = parseInputSlot(left);
        const rightInfo = parseInputSlot(right);
        if (!leftInfo && !rightInfo) return 0;
        if (!leftInfo) return -1;
        if (!rightInfo) return 1;
        if (leftInfo.branch !== rightInfo.branch) return leftInfo.branch - rightInfo.branch;
        return leftInfo.lane - rightInfo.lane;
    });

    node.outputs = (node.outputs || []).slice().sort((left, right) => {
        const leftInfo = parseOutputSlot(left);
        const rightInfo = parseOutputSlot(right);
        if (!leftInfo && !rightInfo) return 0;
        if (!leftInfo) return -1;
        if (!rightInfo) return 1;
        return leftInfo.lane - rightInfo.lane;
    });

    if (typeof node.computeSize === "function" && typeof node.setSize === "function") {
        node.setSize(node.computeSize());
    }
    node.setDirtyCanvas?.(true, true);
};

const installWidgetHook = (node, widgetName) => {
    const widget = getWidget(node, widgetName);
    if (!widget || widget.__sonderLazyWrapped) return;
    widget.__sonderLazyWrapped = true;

    const originalCallback = widget.callback;
    widget.callback = (...args) => {
        const result = originalCallback?.apply(widget, args);
        ensureNodeShape(node);
        return result;
    };
};

const installNodeBehavior = (node) => {
    const state = getNodeState(node);
    if (state.initialized) return;
    state.initialized = true;

    installWidgetHook(node, "branches");
    installWidgetHook(node, "lanes");

    const originalOnConnectionsChange = node.onConnectionsChange;
    node.onConnectionsChange = function (...args) {
        const result = originalOnConnectionsChange?.apply(this, args);
        ensureNodeShape(this);
        return result;
    };

    window.setTimeout(() => {
        if (isTargetNode(node)) ensureNodeShape(node);
    }, 0);
};

// ---------------------------------------------------------------------------
// Sonder Gate: lanes of (when_X, value_X) -> output X, A..P.
//
// The backend tuple is fixed. Lanes auto-extend to the highest connected lane
// plus one spare, and only the canonical tail is ever removed, so no surviving
// slot index shifts (the host resolves links by index). Removal happens only
// for a fresh node or after an explicit disconnect settles: paste, duplicate
// and subgraph convert reconnect links one at a time outside configuringGraph,
// and trimming between those connects would drop links aimed at later lanes.

const TARGET_GATE = "SonderGate";
const MAX_GATE_LANES = 16;
const GATE_STATE = Symbol("sonderGateState");
const GATE_REQUIRED_SUFFIX = " (feeds required input)";
const GATE_NO_CONDITION_SUFFIX = " (no condition)";

const isGateNode = (node) => node?.comfyClass === TARGET_GATE;
const gateWhenName = (lane) => `when_${laneFallbackLabel(lane)}`;
const gateValueName = (lane) => `value_${laneFallbackLabel(lane)}`;

const parseGateInput = (slot) => {
    const match = /^(when|value)_([A-P])$/.exec(String(slot?.name || ""));
    return match ? { kind: match[1], lane: match[2].charCodeAt(0) - 65 } : null;
};

const parseGateOutput = (slot) => {
    const info = parseOutputSlot(slot);
    return info && info.lane < MAX_GATE_LANES ? info : null;
};

const gateInputRank = (slot) => {
    const info = parseGateInput(slot);
    return info ? info.lane * 2 + (info.kind === "value" ? 1 : 0) : -1;
};

const getGateState = (node) => {
    if (!node[GATE_STATE]) {
        // Capture the full schema shape at creation so a regrown slot keeps its
        // declared type and tooltip, whatever a saved node later trims to.
        const meta = new Map();
        for (const slot of node.inputs || []) {
            if (parseGateInput(slot)) meta.set(`in:${slot.name}`, { type: slot.type, tooltip: slot.tooltip || "" });
        }
        for (const slot of node.outputs || []) {
            if (parseGateOutput(slot)) meta.set(`out:${slot.name}`, { type: slot.type, tooltip: slot.tooltip || "" });
        }
        node[GATE_STATE] = {
            meta,
            initialized: false,
            configured: false,
            shrinkPending: false,
            settleQueued: false,
        };
    }
    return node[GATE_STATE];
};

const gateConnectedCeiling = (node) => {
    let ceiling = -1;
    for (const slot of node.inputs || []) {
        const info = parseGateInput(slot);
        if (info && slot.link != null) ceiling = Math.max(ceiling, info.lane);
    }
    for (const slot of node.outputs || []) {
        const info = parseGateOutput(slot);
        if (info && slotHasOutputLinks(slot)) ceiling = Math.max(ceiling, info.lane);
    }
    return ceiling;
};

const gateShownLanes = (node) => {
    let shown = 0;
    for (const slot of node.inputs || []) {
        const info = parseGateInput(slot);
        if (info) shown = Math.max(shown, info.lane + 1);
    }
    for (const slot of node.outputs || []) {
        const info = parseGateOutput(slot);
        if (info) shown = Math.max(shown, info.lane + 1);
    }
    return shown;
};

const addGateInput = (node, name, state) => {
    if (getInputByName(node, name)) return;
    const meta = state.meta.get(`in:${name}`) || { type: "*", tooltip: "" };
    node.addInput(name, meta.type, { tooltip: meta.tooltip });
};

const addGateOutput = (node, name, state) => {
    if (getOutputByName(node, name)) return;
    const meta = state.meta.get(`out:${name}`) || { type: "*", tooltip: "" };
    node.addOutput(name, meta.type, { tooltip: meta.tooltip });
};

const gateLaneTypeLabel = (node, lane) => {
    // The lane's type is its value's type; `when` accepts anything.
    const value = getInputByName(node, gateValueName(lane));
    const fromValue = value?.link != null ? getInputLaneTypeLabel(node, value) : null;
    if (fromValue) return fromValue;
    const output = getOutputByName(node, outputName(lane));
    return output && slotHasOutputLinks(output) ? getOutputLaneTypeLabel(node, output) : null;
};

const setSlotLabel = (slot, label) => {
    // Legacy LiteGraph draws `label`; Nodes 2.0 reads `localized_name`.
    if (slot.label === label && slot.localized_name === label) return false;
    slot.label = label;
    slot.localized_name = label;
    return true;
};

const gateOutputAdvisory = ({ whenWired, laneUsed, feedsRequired }) => {
    if (laneUsed && !whenWired) return GATE_NO_CONDITION_SUFFIX;
    if (feedsRequired) return GATE_REQUIRED_SUFFIX;
    return "";
};

const labelGateSlots = (node) => {
    const required = new Set(requiredConsumerOutputNames(node, {
        getLink: (linkId) => getLinkById(node, linkId),
        getNode: (nodeId) => getGraphNodeById(node, nodeId),
        include: (output) => parseGateOutput(output) !== null,
    }));
    let changed = false;
    for (const slot of node.inputs || []) {
        const info = parseGateInput(slot);
        if (!info) continue;
        const letter = laneFallbackLabel(info.lane);
        const type = info.kind === "value" ? gateLaneTypeLabel(node, info.lane) : null;
        const label = info.kind === "when" ? `when ${letter}` : (type ? `${letter}: ${type}` : letter);
        changed = setSlotLabel(slot, label) || changed;
    }
    for (const slot of node.outputs || []) {
        const info = parseGateOutput(slot);
        if (!info) continue;
        const letter = laneFallbackLabel(info.lane);
        const type = gateLaneTypeLabel(node, info.lane);
        const whenWired = getInputByName(node, gateWhenName(info.lane))?.link != null;
        const laneUsed = getInputByName(node, gateValueName(info.lane))?.link != null || slotHasOutputLinks(slot);
        const suffix = gateOutputAdvisory({ whenWired, laneUsed, feedsRequired: required.has(slot.name) });
        changed = setSlotLabel(slot, `${type ? `${letter}: ${type}` : letter}${suffix}`) || changed;
    }
    return changed;
};

const gateTargetLanes = ({ shown, ceiling, mayShrink }) => {
    const desired = Math.min(MAX_GATE_LANES, Math.max(1, ceiling + 2));
    return mayShrink ? desired : Math.max(shown, desired);
};

const ensureGateShape = (node, { settle = false } = {}) => {
    if (app.configuringGraph) return;
    const state = getGateState(node);
    const shown = gateShownLanes(node);
    const mayShrink = settle && (!state.configured || state.shrinkPending);
    if (settle) state.shrinkPending = false;
    const target = gateTargetLanes({ shown, ceiling: gateConnectedCeiling(node), mayShrink });
    let changed = false;

    // Grow in canonical order: appended slots land at the tail, after every
    // lane below them, so no existing index moves.
    for (let lane = 0; lane < target; lane += 1) {
        const before = (node.inputs?.length || 0) + (node.outputs?.length || 0);
        addGateInput(node, gateWhenName(lane), state);
        addGateInput(node, gateValueName(lane), state);
        addGateOutput(node, outputName(lane), state);
        changed = changed || before !== (node.inputs?.length || 0) + (node.outputs?.length || 0);
    }
    // Shrink the true tail only, high to low. Every lane at or above the target
    // is unconnected, because the target always exceeds the connected ceiling.
    for (let lane = MAX_GATE_LANES - 1; lane >= target; lane -= 1) {
        const output = getOutputByName(node, outputName(lane));
        if (output && !slotHasOutputLinks(output)) { removeOutputSlot(node, output.name); changed = true; }
        for (const name of [gateValueName(lane), gateWhenName(lane)]) {
            const input = getInputByName(node, name);
            if (input && input.link == null) { removeInputSlot(node, name); changed = true; }
        }
    }
    const ordered = (node.inputs || []).every((slot, index, inputs) =>
        index === 0 || gateInputRank(inputs[index - 1]) <= gateInputRank(slot));
    if (!ordered) {
        sortInputSlots(node, (left, right) => gateInputRank(left) - gateInputRank(right));
        changed = true;
    }
    changed = labelGateSlots(node) || changed;

    if (!changed) return;
    if (typeof node.computeSize === "function" && typeof node.setSize === "function") {
        node.setSize(node.computeSize());
    }
    node.setDirtyCanvas?.(true, true);
};

const queueGateSettle = (node) => {
    const state = getGateState(node);
    if (state.settleQueued) return;
    state.settleQueued = true;
    window.setTimeout(() => {
        state.settleQueued = false;
        if (isGateNode(node)) ensureGateShape(node, { settle: true });
    }, 0);
};

const installGateBehavior = (node) => {
    const state = getGateState(node);
    if (state.initialized) return;
    state.initialized = true;

    const originalOnConfigure = node.onConfigure;
    node.onConfigure = function (...args) {
        // A configured node carries a saved or pasted shape; only an explicit
        // disconnect may shrink it from now on.
        state.configured = true;
        return originalOnConfigure?.apply(this, args);
    };

    const originalOnConnectionsChange = node.onConnectionsChange;
    node.onConnectionsChange = function (...args) {
        const result = originalOnConnectionsChange?.apply(this, args);
        if (args[2] === false) state.shrinkPending = true;
        ensureGateShape(this);
        queueGateSettle(this);
        return result;
    };

    queueGateSettle(node);
};

app.registerExtension({
    name: EXT_NAME,

    beforeRegisterNodeDef(_nodeType, nodeData) {
        captureInputDefinition(nodeData);
    },

    async nodeCreated(node) {
        if (isGateNode(node)) {
            installGateBehavior(node);
            return;
        }
        if (!isTargetNode(node)) return;
        installNodeBehavior(node);
    },

    afterConfigureGraph() {
        const root = app.rootGraph || app.graph;
        const graphs = new Set([root, ...(root?.subgraphs?.values() || [])]);
        for (const graph of graphs) {
            for (const node of graph?._nodes || []) {
                if (isTargetNode(node)) ensureNodeShape(node);
                if (isGateNode(node)) ensureGateShape(node);
            }
        }
    },

    getNodeMenuItems(node) {
        if (!isTargetNode(node)) return [];

        installNodeBehavior(node);

        return [
            null,
            {
                content: "Add Branch",
                callback: () => {
                    const state = getNodeState(node);
                    const widget = getWidget(node, "branches");
                    if (!widget) return;
                    widget.value = clampInt(widget.value, MIN_BRANCHES, state.maxBranches, MIN_BRANCHES) + 1;
                    ensureNodeShape(node);
                },
            },
            {
                content: "Remove Branch",
                callback: () => {
                    const widget = getWidget(node, "branches");
                    if (!widget) return;
                    widget.value = clampInt(widget.value, MIN_BRANCHES, getNodeState(node).maxBranches, MIN_BRANCHES) - 1;
                    ensureNodeShape(node);
                },
            },
            {
                content: "Add Lane",
                callback: () => {
                    const state = getNodeState(node);
                    const widget = getWidget(node, "lanes");
                    if (!widget) return;
                    widget.value = clampInt(widget.value, MIN_LANES, state.maxLanes, MIN_LANES) + 1;
                    ensureNodeShape(node);
                },
            },
            {
                content: "Remove Lane",
                callback: () => {
                    const widget = getWidget(node, "lanes");
                    if (!widget) return;
                    widget.value = clampInt(widget.value, MIN_LANES, getNodeState(node).maxLanes, MIN_LANES) - 1;
                    ensureNodeShape(node);
                },
            },
        ];
    },
});
