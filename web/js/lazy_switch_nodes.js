import { app } from "/scripts/app.js";

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
    if (/^SONDER_cluster_lane_\d+$/i.test(cleaned)) return null;

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

const ensureNodeShape = (node) => {
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

    node.inputs = (node.inputs || []).slice().sort((left, right) => {
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

app.registerExtension({
    name: EXT_NAME,

    async nodeCreated(node) {
        if (!isTargetNode(node)) return;
        installNodeBehavior(node);
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
