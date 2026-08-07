// Sonder Reference Bridge frontend shape.
// The backend publishes the full output tuple as an immutable workflow API and
// always returns every value. This module only decides what LiteGraph DISPLAYS:
// the effective r-block, plus the fixed outputs the lane recipe actually drives.
// Three rules hold that safe:
//   1. A connected slot is never removed, whatever the recipe says. It is still
//      marked — see rule 1 in reference_bridge_shape.js for why wiring answers a
//      different question than liveness.
//   2. An unresolved project shows everything — a slow project load must not
//      look like a broken node.
//   3. The backend fallback is untouched; hiding is presentation only.

import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";
import { resolveProjectSource, getGraphLink, getGraphNode } from "./project_source_resolver.js";
import { onProjectVersionChanged } from "./api_client.js";
import {
    MAX_REFERENCE_SLOTS,
    canonicalOutputOrder,
    resolveBridgeOutputs,
    selectorPanelView,
} from "./reference_bridge_shape.js";

const EXT_NAME = "sonder.reference_bridge";
const SELECTOR = "SonderReferenceSelector";
const BRIDGE = "SonderReferenceBridge";
const STATE = Symbol("sonderReferenceBridgeState");
const SELECTOR_STATE = Symbol("sonderReferenceSelectorState");

const nodeType = (node) => String(node?.comfyClass || node?.type || "");
const findWidget = (node, name) => (node?.widgets || []).find((widget) => widget?.name === name) || null;

function ensureState(node) {
    if (node[STATE]) return node[STATE];
    // Capture EVERY output, not just the r-block: a hidden fixed output has to
    // be restorable with its original type, label and tooltip.
    const metadata = new Map();
    const order = [];
    for (const slot of node.outputs || []) {
        const name = String(slot?.name || "");
        if (!name) continue;
        order.push(name);
        metadata.set(name, {
            type: slot.type || "IMAGE",
            label: slot.label,
            localized_name: slot.localized_name,
            tooltip: slot.tooltip,
        });
    }
    node[STATE] = { metadata, order: order.length ? order : canonicalOutputOrder(), initialized: false, refreshToken: 0 };
    return node[STATE];
}

export function applyReferenceBridgeShape(node, shape = {}) {
    if (nodeType(node) !== BRIDGE) return;
    const state = ensureState(node);
    const changed = resolveBridgeOutputs(node, shape, { metadata: state.metadata, order: state.order });
    // Project writes are frequent and mostly unrelated to this node; only a real
    // slot change earns a resize and a canvas repaint.
    if (!changed) return;
    node.setSize?.([node.size?.[0] || 280, Math.max(node.computeSize?.()[1] || 0, 90)]);
    app.graph?.setDirtyCanvas?.(true, true);
}

function upstreamSelector(node) {
    const input = (node.inputs || []).find((slot) => slot?.name === "reference_set") || node.inputs?.[0];
    if (!input || input.link == null) return null;
    const link = getGraphLink(node.graph || app.graph, input.link);
    const source = getGraphNode(node.graph || app.graph, link?.origin_id);
    return nodeType(source) === SELECTOR ? source : null;
}

// "Show everything": used whenever the project cannot be resolved, so a slow or
// unwired editor never presents itself as a node with missing outputs.
const FULL_SHAPE = {
    slotCount: MAX_REFERENCE_SLOTS,
    promptSlotCount: MAX_REFERENCE_SLOTS,
    liveOutputs: null,
};

async function referenceShapeForBridge(node) {
    const selector = upstreamSelector(node);
    if (!selector) return FULL_SHAPE;
    const resolution = resolveProjectSource(selector);
    if (resolution.status !== "resolved") return FULL_SHAPE;
    const controller = resolution.editor?._sonderController || null;
    const controllerState = controller?.state || null;
    const projectDir = String(controllerState?.projectDir || "");
    const projectId = projectDir.split(/[/\\]/).pop() || "";
    const sceneId = controllerState?.sceneId || controllerState?.dormantSummary?.active_scene?.scene_id || "";
    if (!projectId || !sceneId) {
        // Load-time race: the editor node is wired but its async updateProject()
        // has not resolved a project yet. Retry once when it does, mirroring the
        // guide/driver bridge panels.
        if (controller && !projectDir && typeof controller.whenProjectReady === "function") {
            controller.whenProjectReady(() => refreshShape(node));
        }
        return FULL_SHAPE;
    }
    const response = await fetch(api.apiURL(
        `/sonder-editor/project/${encodeURIComponent(projectId)}/scenes/${encodeURIComponent(sceneId)}/bridge-references`
    ));
    if (!response.ok) throw new Error(`Reference bridge shape fetch failed: ${response.status}`);
    const payload = await response.json();
    const laneIndex = Math.max(0, parseInt(findWidget(selector, "reference_lane_index")?.value, 10) || 0);
    const lane = (payload?.references || []).find((row) => row?.lane_index === laneIndex);
    // A lane index pointing at no lane is an authoring mistake, not a recipe
    // statement, so it shows everything rather than an empty node.
    if (!lane) return FULL_SHAPE;
    return {
        // slot_count is member count gated by the lane recipe's output liveness:
        // recipes that never drive the r-block resolve to zero slots.
        slotCount: Math.max(0, parseInt(lane.slot_count ?? lane.member_count, 10) || 0),
        // The p-block is gated separately, on reference_prompt. Absent must stay
        // UNDEFINED and not collapse through `|| 0`, or a payload from a server
        // that predates the field would mark every live p-slot unused — exactly
        // the bug the separate count exists to fix.
        promptSlotCount: lane.prompt_slot_count === undefined || lane.prompt_slot_count === null
            ? undefined
            : Math.max(0, parseInt(lane.prompt_slot_count, 10) || 0),
        liveOutputs: Array.isArray(lane.live_outputs) ? lane.live_outputs : null,
    };
}

function refreshShape(node) {
    if (nodeType(node) !== BRIDGE) return;
    const state = ensureState(node);
    const token = ++state.refreshToken;
    referenceShapeForBridge(node)
        .then((shape) => {
            if (token === state.refreshToken) applyReferenceBridgeShape(node, shape);
        })
        .catch((error) => {
            console.warn("[Sonder Reference Bridge] shape refresh failed:", error);
            // A failed fetch is not evidence about the recipe, so fall back to
            // showing everything rather than to the connected subset.
            if (token === state.refreshToken) applyReferenceBridgeShape(node, FULL_SHAPE);
        });
}

// Library and timeline mutations change the staged member count without ever
// touching this node, so connection/widget hooks alone leave the slot block
// stale. Every project write moves the durable version, so one page-level
// subscription keeps every placed bridge current. Coalesced because a single
// mutation batch can record the version more than once.
let versionRefreshTimer = null;
function refreshAllBridges() {
    clearTimeout(versionRefreshTimer);
    versionRefreshTimer = setTimeout(() => {
        versionRefreshTimer = null;
        for (const node of app.graph?._nodes || []) {
            if (nodeType(node) === BRIDGE) refreshShape(node);
            else if (nodeType(node) === SELECTOR) refreshSelectorPanel(node);
        }
    }, 250);
}

function downstreamBridges(selector) {
    const graph = selector?.graph || app.graph;
    const output = (selector?.outputs || []).find((slot) => slot?.name === "reference_set") || selector?.outputs?.[0];
    const links = Array.isArray(output?.links) ? output.links : (output?.link != null ? [output.link] : []);
    const bridges = [];
    for (const linkId of links) {
        const link = getGraphLink(graph, linkId);
        const target = getGraphNode(graph, link?.target_id);
        if (nodeType(target) === BRIDGE) bridges.push(target);
    }
    return bridges;
}

function refreshDownstreamBridges(selector) {
    for (const bridge of downstreamBridges(selector)) refreshShape(bridge);
}

// ── Selector lane panel ───────────────────────────────────────────────
// A bare INT tells the user nothing about which lane they picked. This mirrors
// SonderDriverSelector's panel (bridge_nodes.js): a lane dropdown, a status
// line, and the staged items. The INT stays the serialized workflow state — the
// panel only writes it, so the node contract is unchanged.

const style = (element, css) => { element.style.cssText = css; return element; };

function selectorState(node) {
    if (!node[SELECTOR_STATE]) node[SELECTOR_STATE] = { installed: false, refreshToken: 0 };
    return node[SELECTOR_STATE];
}

function setSelectorLane(node, value) {
    const widget = findWidget(node, "reference_lane_index");
    if (!widget) return;
    const laneIndex = Math.max(0, parseInt(value, 10) || 0);
    if (widget.value === laneIndex) return;
    widget.value = laneIndex;
    widget.callback?.(laneIndex);
    app.graph?.setDirtyCanvas?.(true, true);
}

async function selectorLanePayload(node) {
    const resolution = resolveProjectSource(node);
    if (resolution.status !== "resolved") {
        return { status: "Connect a Sonder Editor project.", lanes: [], linked: false };
    }
    const controller = resolution.editor?._sonderController || null;
    const controllerState = controller?.state || null;
    const projectDir = String(controllerState?.projectDir || "");
    const projectId = projectDir.split(/[/\\]/).pop() || "";
    const sceneId = controllerState?.sceneId || controllerState?.dormantSummary?.active_scene?.scene_id || "";
    if (!projectId || !sceneId) {
        if (controller && !projectDir && typeof controller.whenProjectReady === "function") {
            controller.whenProjectReady(() => refreshSelectorPanel(node));
        }
        return { status: projectId ? "No active scene." : "Loading project...", lanes: [], linked: false };
    }
    const response = await fetch(api.apiURL(
        `/sonder-editor/project/${encodeURIComponent(projectId)}/scenes/${encodeURIComponent(sceneId)}/bridge-references`
    ));
    if (!response.ok) throw new Error(`Reference lane fetch failed: ${response.status}`);
    const payload = await response.json();
    return {
        status: "",
        lanes: Array.isArray(payload?.references) ? payload.references : [],
        sceneName: String(payload?.scene_name || ""),
        source: String(payload?.source || "live"),
        linked: true,
    };
}

function renderSelectorPanel(node, payload) {
    const state = selectorState(node);
    if (!state.select) return;
    const view = selectorPanelView({
        lanes: payload.lanes,
        laneIndex: findWidget(node, "reference_lane_index")?.value,
        status: payload.status,
        sceneName: payload.sceneName,
        source: payload.source,
    });

    state.select.replaceChildren();
    for (const entry of view.options) {
        const option = document.createElement("option");
        option.value = String(entry.value);
        option.textContent = entry.label;
        state.select.appendChild(option);
    }
    state.select.value = String(view.selectedValue);
    state.select.disabled = view.disabled;
    state.status.textContent = view.status;

    state.list.replaceChildren();
    for (const tag of view.tags) {
        const chip = style(document.createElement("span"), `
            display:inline-block; font-size:9px; padding:1px 5px; margin:1px;
            border:1px solid rgba(190,125,157,0.42); border-radius:8px; color:#d9bcca;
        `);
        chip.textContent = tag;
        chip.title = "Tag on a member staged in this lane";
        state.list.appendChild(chip);
    }
    for (const name of view.outputs) {
        const chip = style(document.createElement("span"), `
            display:inline-block; font-size:9px; padding:1px 5px; margin:1px;
            border:1px solid rgba(126,168,201,0.3); border-radius:8px; color:#a8b6c4;
        `);
        chip.textContent = name;
        chip.title = "Output this recipe drives";
        state.list.appendChild(chip);
    }
}

function refreshSelectorPanel(node) {
    if (nodeType(node) !== SELECTOR) return;
    const state = selectorState(node);
    if (!state.panel) return;
    const token = ++state.refreshToken;
    state.status.textContent = "Loading Reference lanes...";
    selectorLanePayload(node)
        .then((payload) => {
            if (token === state.refreshToken) renderSelectorPanel(node, payload);
        })
        .catch((error) => {
            if (token !== state.refreshToken) return;
            renderSelectorPanel(node, { status: error?.message || "Reference lane panel failed.", lanes: [], linked: false });
        });
}

function installSelectorPanel(node) {
    if (nodeType(node) !== SELECTOR || typeof node.addDOMWidget !== "function") return;
    const state = selectorState(node);
    if (state.panel) return;
    const wrapper = style(document.createElement("div"), "display:flex;flex-direction:column;gap:5px;width:100%;box-sizing:border-box;padding-top:2px;");
    const header = style(document.createElement("div"), "display:flex;justify-content:space-between;align-items:center;gap:6px;color:#cfd7df;font-size:10px;font-weight:700;");
    const title = document.createElement("span");
    title.textContent = "Reference Selector";
    const refreshBtn = style(document.createElement("button"), `
        border:1px solid rgba(126,168,201,0.35); border-radius:5px; background:rgba(14,19,25,0.92);
        color:#dbe4ed; font-size:10px; padding:2px 6px; cursor:pointer;
    `);
    refreshBtn.textContent = "Refresh";
    refreshBtn.addEventListener("click", () => refreshSelectorPanel(node));
    header.append(title, refreshBtn);

    const select = style(document.createElement("select"), `
        width:100%; box-sizing:border-box; background:#17202a; color:#e6edf3;
        border:1px solid rgba(126,168,201,0.35); border-radius:5px; padding:3px 5px; font-size:10px;
    `);
    select.addEventListener("change", () => {
        setSelectorLane(node, select.value);
        refreshSelectorPanel(node);
    });
    const status = style(document.createElement("div"), "color:#7f8d9b;font-size:10px;line-height:1.25;");
    const list = style(document.createElement("div"), "display:flex;flex-wrap:wrap;max-height:64px;overflow:auto;");
    wrapper.append(header, select, status, list);

    const domWidget = node.addDOMWidget("sonder_reference_selector_panel", "SonderReferenceSelectorPanel", wrapper, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => 86,
        getMaxHeight: () => 168,
        getHeight: () => 132,
    });
    domWidget.computeSize = (width) => [width, 132];
    state.panel = wrapper;
    state.select = select;
    state.status = status;
    state.list = list;
    refreshSelectorPanel(node);
}

function installSelector(node) {
    if (nodeType(node) !== SELECTOR) return;
    const state = selectorState(node);
    installSelectorPanel(node);
    if (state.installed) return;
    state.installed = true;
    const laneWidget = findWidget(node, "reference_lane_index");
    if (laneWidget) {
        const originalCallback = laneWidget.callback;
        laneWidget.callback = function (...args) {
            const result = originalCallback?.apply(this, args);
            window.setTimeout(() => {
                refreshDownstreamBridges(node);
                refreshSelectorPanel(node);
            }, 0);
            return result;
        };
    }
    const originalConnections = node.onConnectionsChange;
    node.onConnectionsChange = function (...args) {
        const result = originalConnections?.apply(this, args);
        window.setTimeout(() => {
            refreshDownstreamBridges(this);
            // Wiring the project input is what makes the lane list resolvable.
            refreshSelectorPanel(this);
        }, 0);
        return result;
    };
}

function install(node) {
    if (nodeType(node) !== BRIDGE) return;
    const state = ensureState(node);
    if (state.initialized) return;
    state.initialized = true;
    const originalConnections = node.onConnectionsChange;
    node.onConnectionsChange = function (...args) {
        const result = originalConnections?.apply(this, args);
        window.setTimeout(() => refreshShape(this), 0);
        return result;
    };
    const originalMenu = node.getExtraMenuOptions;
    node.getExtraMenuOptions = function (canvas, options) {
        const result = originalMenu?.apply(this, arguments);
        options?.push({
            content: "Refresh reference slots",
            callback: () => refreshShape(this),
        });
        return result;
    };
    window.setTimeout(() => refreshShape(node), 0);
}

app.registerExtension({
    name: EXT_NAME,
    setup() {
        onProjectVersionChanged(refreshAllBridges);
    },
    nodeCreated(node) {
        installSelector(node);
        install(node);
    },
    loadedGraphNode(node) {
        installSelector(node);
        install(node);
    },
});
