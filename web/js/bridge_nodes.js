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
import { refreshAutogrowShape } from "./autogrow_passthrough.js";
import {
    projectResolutionStatusText,
    resolveProjectSource,
} from "./project_source_resolver.js";
import {
    commitWidgetVisibility,
    setWidgetHidden,
} from "./widget_visibility.js";
import {
    bridgePayloadVersion,
    createBridgeRefreshScheduler,
    requestBridgeGuidePayload,
    requestBridgeDriverPayload,
} from "./bridge_read_coordinator.js";
import { onProjectVersionChanged, versionStillAnswers } from "./api_client.js";
import { onEditorRenderWindowChanged } from "./editor_render_window_events.js";
import { graphsOf, nodesOf } from "./control_bindings.js";
import { observeWidgetValue } from "./widget_value_observer.js";
const { api } = window.comfyAPI.api;

// Every node of the open workflow, subgraph definitions included, so a panel
// inside a subgraph follows project edits and scene switches too.
const allGraphNodes = () => graphsOf(app.graph).flatMap((graph) => nodesOf(graph));

const scheduleBridgeGuideRefresh = createBridgeRefreshScheduler({
    getTargets: () => allGraphNodes().filter(isStart),
    dispatch: (node, meta) => refreshBridgeGuidePanel(node, meta),
});
const scheduleBridgeDriverRefresh = createBridgeRefreshScheduler({
    getTargets: () => allGraphNodes().filter(isDriverSelector),
    dispatch: (node, meta) => refreshBridgeDriverPanel(node, meta),
});

// Overrides name guides and lanes that may come back (editor Undo, a scene
// switch back), so automatic refreshes never prune them: only an explicit
// Refresh click does. Until then the panel counts the stale ones.
const isManualDispatch = (dispatch) => String(dispatch?.origin || "").split("+").includes("manual");

const EXT_NAME = "sonder.bridge";
const TARGET_START = "SonderGuidesBridgeStart";
const TARGET_END = "SonderGuidesBridgeEnd";
const TARGET_DRIVER_SELECTOR = "SonderDriverSelector";
const MAX_PASSTHROUGH = 8; // mirror SONDER_BRIDGE_MAX_PASSTHROUGH default
const VALUE_RE = /^value_(\d+)$/;
const NODE_STATE = Symbol("sonderBridgeState");
// The `{url, version}` of the bridge-guides payload a Start node last applied.
// Ephemeral: it only decides whether a project-version notice owes the node a
// re-read, and it is dropped on every dispatch and failed read.
const GUIDE_ACCEPTED = Symbol("sonderGuideAcceptedRead");
const OVERRIDES_WIDGET = "bridge_overrides_json";
const DRIVER_LANE_WIDGET = "driver_lane_index";
const DRIVER_OVERRIDES_WIDGET = "driver_selector_overrides_json";

const isStart = (node) => node?.comfyClass === TARGET_START;
const isEnd = (node) => node?.comfyClass === TARGET_END;
const isDriverSelector = (node) => node?.comfyClass === TARGET_DRIVER_SELECTOR;
const isBridge = (node) => isStart(node) || isEnd(node);
const isAnySonderBridge = (node) => isBridge(node) || isDriverSelector(node);

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

const style = (element, cssText) => {
    element.style.cssText = cssText;
    return element;
};

const projectIdFromDir = (projectDir) =>
    String(projectDir || "").split(/[/\\]/).pop() || "";

const findWidget = (node, name) =>
    (node?.widgets || []).find((widget) => widget?.name === name) || null;

const readGuideOverrides = (node) => {
    const widget = findWidget(node, OVERRIDES_WIDGET);
    const raw = widget?.value || "{}";
    try {
        const parsed = typeof raw === "string" ? JSON.parse(raw || "{}") : raw;
        return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
    } catch (_) {
        return {};
    }
};

const writeGuideOverrides = (node, overrides) => {
    const widget = findWidget(node, OVERRIDES_WIDGET);
    if (!widget) return;
    const compact = {};
    for (const [key, value] of Object.entries(overrides || {})) {
        if (!key || !value || typeof value !== "object" || typeof value.muted !== "boolean") continue;
        compact[key] = { muted: value.muted };
    }
    const serialized = JSON.stringify(compact);
    widget.value = serialized;
    widget.callback?.call(widget, serialized);
    app.graph.setDirtyCanvas?.(true, true);
};

const guideKey = (assetId, frameIndex) => `${assetId || ""}:${frameIndex}`;

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
        guidePanel: null,
        guideList: null,
        guideStatus: null,
        guideRefreshToken: 0,
        guideLoading: false,
        // Set by an explicit Refresh; the next linked render prunes, even when
        // an automatic dispatch superseded the manual one.
        guidePrunePending: false,
        driverPrunePending: false,
        driverPanel: null,
        driverSelect: null,
        driverList: null,
        driverStatus: null,
        driverRefreshToken: 0,
        driverLoading: false,
        // The last applied bridge-drivers payload and the URL it answered, so a
        // lane-widget write re-renders without a read.
        driverPayload: null,
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
    refreshAutogrowShape(node, { maxCount: MAX_PASSTHROUGH, peer });
    if (peer) {
        refreshAutogrowShape(peer, { maxCount: MAX_PASSTHROUGH, peer: node });
        peer.setDirtyCanvas?.(true, true);
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

/**
 * Where a Guides Start or Driver Selector reads from: the resolved project,
 * scene and full request URL (`route` is `bridge-guides` or `bridge-drivers`),
 * or `{resolution}` alone while the project source is unresolved.
 */
function bridgeReadContext(node, route) {
    const resolution = resolveProjectSource(node);
    if (resolution.status !== "resolved") return { resolution, url: "" };
    const controllerState = resolution.editor?._sonderController?.state || null;
    const projectDir = String(controllerState?.projectDir || "");
    const projectId = projectIdFromDir(projectDir);
    const sceneId = controllerState?.sceneId || controllerState?.dormantSummary?.active_scene?.scene_id || "";
    const url = projectId && sceneId
        ? api.apiURL(`/sonder-editor/project/${encodeURIComponent(projectId)}/scenes/${encodeURIComponent(sceneId)}/${route}`)
        : "";
    return { resolution, projectDir, projectId, sceneId, url };
}

async function loadLinkedEditorGuides(node, dispatch) {
    const context = bridgeReadContext(node, "bridge-guides");
    if (context.resolution.status !== "resolved") {
        return {
            status: projectResolutionStatusText(context.resolution),
            guides: [],
            linked: false,
        };
    }
    if (!context.projectId) {
        return { status: "Connect a Sonder Editor project.", guides: [], linked: false };
    }
    if (!context.sceneId) {
        return { status: "No active scene.", guides: [], linked: false };
    }

    const payload = await requestBridgeGuidePayload({
        url: context.url, ...dispatch, nodeId: node.id,
    });
    const rows = Array.isArray(payload?.guides) ? payload.guides : [];
    const guides = rows.map((row) => ({
        ...row,
        resolved_frame_index: Math.max(0, parseInt(row.frame_index, 10) || 0),
        muted: !!row.editor_muted,
    }));
    // A running job's snapshot holds only its window's guides, so its key set
    // cannot say what the scene lost: nothing counts as stale against it.
    const allGuideKeys = payload?.source !== "snapshot" && Array.isArray(payload?.all_guide_keys)
        ? payload.all_guide_keys : null;
    const sourceLabel = payload?.source === "snapshot" ? "Snapshot guides for running job" : "Live editor guides";
    const sceneName = payload?.scene_name || "Scene";
    const ws = payload?.window_start ?? 0;
    const we = payload?.window_end ?? 0;
    return {
        status: `${sourceLabel} - ${sceneName} f${ws}-${we}`,
        guides,
        all_guide_keys: allGuideKeys,
        linked: true,
        read: { url: context.url, payload },
    };
}

/**
 * Override keys the scene no longer has, measured against the response's full
 * scene key set, never the visible rows (temporary window exclusion must not
 * delete workflow intent). `keep(key, sceneKeys)` spares keys a reader still
 * honours. Without a key set (older server) nothing is stale.
 */
function staleOverrideKeys(overrides, allKeys, keep = () => false) {
    if (!Array.isArray(allKeys)) return [];
    const sceneKeys = new Set(allKeys.filter(Boolean));
    return Object.keys(overrides).filter((key) => !sceneKeys.has(key) && !keep(key, sceneKeys));
}

function pruneOverrides(node, overrides, stale, write) {
    const cleaned = { ...overrides };
    for (const key of stale) delete cleaned[key];
    write(node, cleaned);
    return cleaned;
}

function appendStaleNotice(list, count) {
    if (!count) return;
    const notice = style(document.createElement("div"), "color:#c9a46a;font-size:10px;padding:3px 0;");
    notice.textContent = `${count} stale override${count === 1 ? "" : "s"} — Refresh to clear`;
    notice.title = "Overrides for guides or lanes no longer in this scene. They stay until you click Refresh, so an Undo that brings them back keeps them.";
    list.appendChild(notice);
}

function renderGuidePanel(node, payload = { status: "", guides: [] }, { prune = false, keepStatus = false } = {}) {
    const state = ensureNodeState(node);
    if (!state.guideList || !state.guideStatus) return;
    if (!keepStatus) state.guideStatus.textContent = payload.status || "";
    state.guideList.innerHTML = "";
    const guides = payload.guides || [];

    let overrides = readGuideOverrides(node);
    let stale = staleOverrideKeys(overrides, payload.all_guide_keys);
    if (prune && stale.length) {
        overrides = pruneOverrides(node, overrides, stale, writeGuideOverrides);
        stale = [];
    }

    if (!guides.length) {
        if (payload.linked) {
            const empty = style(document.createElement("div"), "color:#8b96a3;font-size:10px;padding:3px 0;");
            empty.textContent = "No guides in scene.";
            state.guideList.appendChild(empty);
        }
        appendStaleNotice(state.guideList, stale.length);
        return;
    }
    for (const guide of guides) {
        const row = style(document.createElement("div"), `
            display:grid;
            grid-template-columns:44px 1fr 88px;
            gap:6px;
            align-items:center;
            padding:3px 0;
            border-top:1px solid rgba(255,255,255,0.06);
        `);
        const frame = style(document.createElement("span"), "color:#9db5cf;font-size:10px;font-family:monospace;");
        frame.textContent = `f${guide.resolved_frame_index}`;
        const name = style(document.createElement("span"), "color:#dbe4ed;font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;");
        name.textContent = guide.asset_name || guide.asset_id || "Guide";
        name.title = name.textContent;
        const key = guide.guide_key;
        const override = overrides[key];
        // Tri-state: inherit (auto) | mute (off) | unmute (on). Cycle is independent of editor mute.
        const mode = override && typeof override.muted === "boolean"
            ? (override.muted ? "off" : "on")
            : "auto";
        const inheritedMuted = !!guide.muted;
        const bg = mode === "off"
            ? "rgba(135,52,60,0.85)"
            : mode === "on"
                ? "rgba(38,99,66,0.85)"
                : (inheritedMuted ? "rgba(70,55,60,0.7)" : "rgba(45,70,58,0.7)");
        const btn = style(document.createElement("button"), `
            border:1px solid rgba(126,168,201,0.35);
            border-radius:5px;
            background:${bg};
            color:#f2f7fb;
            font-size:10px;
            padding:2px 5px;
            cursor:pointer;
        `);
        const labels = {
            auto: inheritedMuted ? "Inherit (off)" : "Inherit (on)",
            off: "Bridge mute",
            on: "Bridge on",
        };
        btn.textContent = labels[mode];
        btn.title = "Click to cycle Inherit / Bridge mute / Bridge on for this bridge only";
        btn.addEventListener("click", () => {
            const nextOverrides = readGuideOverrides(node);
            // Cycle: auto -> off -> on -> auto (independent of editor mute)
            if (mode === "auto") {
                nextOverrides[key] = { muted: true };       // Bridge mute
            } else if (mode === "off") {
                nextOverrides[key] = { muted: false };      // Bridge on (force inject)
            } else {
                delete nextOverrides[key];                  // Inherit
            }
            writeGuideOverrides(node, nextOverrides);
            renderGuidePanel(node, payload, { keepStatus: state.guideLoading });
        });
        row.append(frame, name, btn);
        state.guideList.appendChild(row);
    }
    appendStaleNotice(state.guideList, stale.length);
}

function refreshBridgeGuidePanel(node, dispatch) {
    if (!isStart(node)) return;
    const state = ensureNodeState(node);
    if (!state.guidePanel) return;

    // Load-time race fix: when an editor node is wired but its async
    // `updateProject()` has not populated `state.projectDir` yet, schedule a
    // one-shot retry so the panel auto-refreshes once the project resolves.
    // The current load still runs (and will show "Connect..." until the retry).
    const resolution = resolveProjectSource(node);
    const editorNode = resolution.status === "resolved" ? resolution.editor : null;
    const controller = editorNode?._sonderController || null;
    if (controller && !controller.state?.projectDir && typeof controller.whenProjectReady === "function") {
        controller.whenProjectReady(() => scheduleBridgeGuideRefresh({ origin: "project_ready", targets: [node] }));
    }

    const token = ++state.guideRefreshToken;
    // Sticky: a later automatic dispatch that supersedes this one still prunes.
    if (isManualDispatch(dispatch)) state.guidePrunePending = true;
    // Revoked on dispatch, never restored by a late certificate: only the read
    // this dispatch makes can re-establish what the node holds.
    node[GUIDE_ACCEPTED] = null;
    state.guideLoading = true;
    state.guideStatus.textContent = "Loading guides...";
    loadLinkedEditorGuides(node, dispatch)
        .then((payload) => {
            if (token !== state.guideRefreshToken) return;
            state.guideLoading = false;
            if (payload?.read) {
                node[GUIDE_ACCEPTED] = { url: payload.read.url, version: bridgePayloadVersion(payload.read.payload) };
            }
            const prune = !!(state.guidePrunePending && payload?.linked);
            if (prune) state.guidePrunePending = false;
            renderGuidePanel(node, payload, { prune });
        })
        .catch((error) => {
            if (token !== state.guideRefreshToken) return;
            state.guideLoading = false;
            renderGuidePanel(node, {
                status: error?.message || "Guide panel failed.",
                guides: [],
                linked: false,
            });
        });
}

function installGuidePanel(node) {
    if (!isStart(node) || typeof node.addDOMWidget !== "function") return;
    const state = ensureNodeState(node);
    if (state.guidePanel) return;
    const wrapper = style(document.createElement("div"), `
        display:flex;
        flex-direction:column;
        gap:4px;
        width:100%;
        box-sizing:border-box;
        padding-top:2px;
    `);
    const header = style(document.createElement("div"), `
        display:flex;
        justify-content:space-between;
        align-items:center;
        gap:6px;
        color:#cfd7df;
        font-size:10px;
        font-weight:700;
    `);
    const title = document.createElement("span");
    title.textContent = "Bridge Guide Overrides";
    const refreshBtn = style(document.createElement("button"), `
        border:1px solid rgba(126,168,201,0.35);
        border-radius:5px;
        background:rgba(14,19,25,0.92);
        color:#dbe4ed;
        font-size:10px;
        padding:2px 6px;
        cursor:pointer;
    `);
    refreshBtn.textContent = "Refresh";
    refreshBtn.addEventListener("click", () => scheduleBridgeGuideRefresh({ origin: "manual", force: true, targets: [node] }));
    header.append(title, refreshBtn);
    const status = style(document.createElement("div"), "color:#7f8d9b;font-size:10px;line-height:1.25;");
    const list = style(document.createElement("div"), "display:flex;flex-direction:column;max-height:138px;overflow:auto;");
    wrapper.append(header, status, list);
    const domWidget = node.addDOMWidget("sonder_bridge_guide_overrides", "SonderBridgeGuideOverrides", wrapper, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => 84,
        getMaxHeight: () => 188,
        getHeight: () => 164,
    });
    domWidget.computeSize = (width) => [width, 164];
    state.guidePanel = wrapper;
    state.guideStatus = status;
    state.guideList = list;
    scheduleBridgeGuideRefresh({ origin: "lifecycle", targets: [node] });
}

const readDriverOverrides = (node) => {
    const widget = findWidget(node, DRIVER_OVERRIDES_WIDGET);
    const raw = widget?.value || "{}";
    try {
        const parsed = typeof raw === "string" ? JSON.parse(raw || "{}") : raw;
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
        const drivers = parsed.drivers;
        return drivers && typeof drivers === "object" && !Array.isArray(drivers) ? drivers : parsed;
    } catch (_) {
        return {};
    }
};

const writeDriverOverrides = (node, overrides) => {
    const widget = findWidget(node, DRIVER_OVERRIDES_WIDGET);
    if (!widget) return;
    const compact = {};
    for (const [key, value] of Object.entries(overrides || {})) {
        if (!key || !value || typeof value !== "object" || typeof value.muted !== "boolean") continue;
        compact[key] = { muted: value.muted };
    }
    const serialized = JSON.stringify({ drivers: compact });
    widget.value = serialized;
    widget.callback?.call(widget, serialized);
    app.graph.setDirtyCanvas?.(true, true);
};

const driverLaneWidgetValue = (node) => {
    const widget = findWidget(node, DRIVER_LANE_WIDGET);
    const value = parseInt(widget?.value, 10);
    return Number.isFinite(value) && value >= 0 ? value : 0;
};

const setDriverLaneWidgetValue = (node, value) => {
    const widget = findWidget(node, DRIVER_LANE_WIDGET);
    if (!widget) return;
    const next = Math.max(0, parseInt(value, 10) || 0);
    widget.value = next;
    widget.callback?.call(widget, next);
    app.graph.setDirtyCanvas?.(true, true);
};

async function loadLinkedEditorDrivers(node, dispatch) {
    const context = bridgeReadContext(node, "bridge-drivers");
    if (context.resolution.status !== "resolved") {
        return {
            status: projectResolutionStatusText(context.resolution),
            drivers: [],
            linked: false,
        };
    }
    if (!context.projectId) {
        return { status: "Connect a Sonder Editor project.", drivers: [], linked: false };
    }
    if (!context.sceneId) {
        return { status: "No active scene.", drivers: [], linked: false };
    }

    const payload = await requestBridgeDriverPayload({
        url: context.url, ...dispatch, nodeId: node.id,
    });
    const rows = Array.isArray(payload?.drivers) ? payload.drivers : [];
    const sourceLabel = payload?.source === "snapshot" ? "Snapshot drivers for running job" : "Live editor drivers";
    const sceneName = payload?.scene_name || "Scene";
    const ws = payload?.window_start ?? 0;
    const we = payload?.window_end ?? 0;
    return {
        status: `${sourceLabel} - ${sceneName} f${ws}-${we}`,
        drivers: rows,
        // A job snapshot predates lanes added since; never judge staleness by it.
        all_driver_keys: payload?.source !== "snapshot" && Array.isArray(payload?.all_driver_keys)
            ? payload.all_driver_keys : null,
        linked: true,
        url: context.url,
    };
}

// The Driver Bridge still honours a legacy bare `"N"` key for lane N
// (`nodes/driver_bridge.py`), so it is not stale while `lane:N` exists.
// Expiry: when the Driver Bridge stops reading bare keys.
const keepLegacyDriverKey = (key, sceneKeys) => /^\d+$/.test(key) && sceneKeys.has(`lane:${key}`);

function renderDriverPanel(node, payload = { status: "", drivers: [] }, { prune = false, keepStatus = false } = {}) {
    const state = ensureNodeState(node);
    if (!state.driverList || !state.driverStatus || !state.driverSelect) return;
    if (!keepStatus) state.driverStatus.textContent = payload.status || "";
    state.driverList.innerHTML = "";

    const drivers = payload.drivers || [];
    const selectedLane = driverLaneWidgetValue(node);
    state.driverSelect.innerHTML = "";
    for (const driver of drivers) {
        const option = document.createElement("option");
        option.value = String(driver.lane_index);
        option.textContent = driver.lane_name || `Driver ${driver.lane_index + 1}`;
        state.driverSelect.appendChild(option);
    }
    if (!drivers.some((driver) => driver.lane_index === selectedLane)) {
        const option = document.createElement("option");
        option.value = String(selectedLane);
        option.textContent = `Missing lane ${selectedLane + 1}`;
        state.driverSelect.appendChild(option);
    }
    state.driverSelect.value = String(selectedLane);

    let overrides = readDriverOverrides(node);
    let stale = staleOverrideKeys(overrides, payload.all_driver_keys, keepLegacyDriverKey);
    if (prune && stale.length) {
        overrides = pruneOverrides(node, overrides, stale, writeDriverOverrides);
        stale = [];
    }

    if (!drivers.length) {
        if (payload.linked) {
            const empty = style(document.createElement("div"), "color:#8b96a3;font-size:10px;padding:3px 0;");
            empty.textContent = "No driver lanes in scene.";
            state.driverList.appendChild(empty);
        }
        appendStaleNotice(state.driverList, stale.length);
        return;
    }

    const selectedDriver = drivers.find((driver) => driver.lane_index === selectedLane) || null;
    if (!selectedDriver) {
        const missing = style(document.createElement("div"), "color:#8b96a3;font-size:10px;padding:3px 0;");
        missing.textContent = `Selected Driver ${selectedLane + 1} is not available in this scene.`;
        state.driverList.appendChild(missing);
        appendStaleNotice(state.driverList, stale.length);
        return;
    }

    for (const driver of [selectedDriver]) {
        const row = style(document.createElement("div"), `
            display:grid;
            grid-template-columns:72px 1fr 88px;
            gap:6px;
            align-items:center;
            padding:3px 0;
            border-top:1px solid rgba(255,255,255,0.06);
            cursor:pointer;
        `);
        const lane = style(document.createElement("span"), "color:#9db5cf;font-size:10px;font-family:monospace;");
        lane.textContent = `D${driver.lane_index + 1}`;
        const name = style(document.createElement("span"), "color:#dbe4ed;font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;");
        const clipLabel = driver.has_clip
            ? `${driver.asset_name || "Driver"} f${driver.start_frame}-${driver.end_frame}`
            : "No clip";
        name.textContent = `${driver.lane_name || `Driver ${driver.lane_index + 1}`} - ${clipLabel}`;
        name.title = name.textContent;
        const key = driver.lane_key || `lane:${driver.lane_index}`;
        // Shown as the Driver Bridge resolves it (`_driver_override_state`):
        // `lane:N` first, then the legacy bare `N`. Cycling rewrites `lane:N` and
        // drops the bare key, so a kept legacy key is visible and clearable.
        const legacyKey = String(driver.lane_index);
        const override = [overrides[key], overrides[legacyKey]]
            .find((value) => value && typeof value.muted === "boolean");
        const mode = override ? (override.muted ? "off" : "on") : "auto";
        const inheritedMuted = !!driver.editor_muted || !driver.has_clip;
        const bg = mode === "off"
            ? "rgba(135,52,60,0.85)"
            : mode === "on"
                ? "rgba(38,99,66,0.85)"
                : (inheritedMuted ? "rgba(70,55,60,0.7)" : "rgba(45,70,58,0.7)");
        const btn = style(document.createElement("button"), `
            border:1px solid rgba(126,168,201,0.35);
            border-radius:5px;
            background:${bg};
            color:#f2f7fb;
            font-size:10px;
            padding:2px 5px;
            cursor:pointer;
        `);
        const labels = {
            auto: inheritedMuted ? "Inherit (off)" : "Inherit (on)",
            off: "Bridge mute",
            on: "Bridge on",
        };
        btn.textContent = labels[mode];
        btn.title = "Click to cycle Inherit / Bridge mute / Bridge on for this bridge only";
        btn.addEventListener("click", (event) => {
            event.stopPropagation();
            const nextOverrides = readDriverOverrides(node);
            delete nextOverrides[legacyKey];
            if (mode === "auto") {
                nextOverrides[key] = { muted: true };
            } else if (mode === "off") {
                nextOverrides[key] = { muted: false };
            } else {
                delete nextOverrides[key];
            }
            writeDriverOverrides(node, nextOverrides);
            renderDriverPanel(node, payload, { keepStatus: state.driverLoading });
        });
        row.append(lane, name, btn);
        state.driverList.appendChild(row);
    }
    appendStaleNotice(state.driverList, stale.length);
}

/**
 * The lane widget changed (graph dropdown, Controls, Relay or load). Re-render
 * from the held payload when it answers the node's current URL; otherwise read.
 * Never writes, never bumps the refresh token, never replaces a "Loading" status.
 */
function onDriverLaneChanged(node) {
    const state = ensureNodeState(node);
    if (!state.driverPanel) return;
    const held = state.driverPayload;
    const { url } = bridgeReadContext(node, "bridge-drivers");
    if (held && url && held.url === url) {
        renderDriverPanel(node, held, { keepStatus: state.driverLoading });
        return;
    }
    scheduleBridgeDriverRefresh({ origin: "driver_widget", targets: [node] });
}

function refreshBridgeDriverPanel(node, dispatch) {
    if (!isDriverSelector(node)) return;
    const state = ensureNodeState(node);
    if (!state.driverPanel) return;

    const resolution = resolveProjectSource(node);
    const editorNode = resolution.status === "resolved" ? resolution.editor : null;
    const controller = editorNode?._sonderController || null;
    if (controller && !controller.state?.projectDir && typeof controller.whenProjectReady === "function") {
        controller.whenProjectReady(() => scheduleBridgeDriverRefresh({ origin: "project_ready", targets: [node] }));
    }

    const token = ++state.driverRefreshToken;
    if (isManualDispatch(dispatch)) state.driverPrunePending = true;
    state.driverLoading = true;
    state.driverStatus.textContent = "Loading drivers...";
    loadLinkedEditorDrivers(node, dispatch)
        .then((payload) => {
            if (token !== state.driverRefreshToken) return;
            state.driverLoading = false;
            state.driverPayload = payload?.url ? payload : null;
            const prune = !!(state.driverPrunePending && payload?.linked);
            if (prune) state.driverPrunePending = false;
            renderDriverPanel(node, payload, { prune });
        })
        .catch((error) => {
            if (token !== state.driverRefreshToken) return;
            state.driverLoading = false;
            state.driverPayload = null;
            renderDriverPanel(node, {
                status: error?.message || "Driver panel failed.",
                drivers: [],
                linked: false,
            });
        });
}

function installDriverPanel(node) {
    if (!isDriverSelector(node) || typeof node.addDOMWidget !== "function") return;
    const state = ensureNodeState(node);
    if (state.driverPanel) return;
    const wrapper = style(document.createElement("div"), `
        display:flex;
        flex-direction:column;
        gap:5px;
        width:100%;
        box-sizing:border-box;
        padding-top:2px;
    `);
    const header = style(document.createElement("div"), `
        display:flex;
        justify-content:space-between;
        align-items:center;
        gap:6px;
        color:#cfd7df;
        font-size:10px;
        font-weight:700;
    `);
    const title = document.createElement("span");
    title.textContent = "Driver Selector";
    const refreshBtn = style(document.createElement("button"), `
        border:1px solid rgba(126,168,201,0.35);
        border-radius:5px;
        background:rgba(14,19,25,0.92);
        color:#dbe4ed;
        font-size:10px;
        padding:2px 6px;
        cursor:pointer;
    `);
    refreshBtn.textContent = "Refresh";
    refreshBtn.addEventListener("click", () => scheduleBridgeDriverRefresh({ origin: "manual", force: true, targets: [node] }));
    header.append(title, refreshBtn);

    const select = style(document.createElement("select"), `
        width:100%;
        box-sizing:border-box;
        background:#17202a;
        color:#e6edf3;
        border:1px solid rgba(126,168,201,0.35);
        border-radius:5px;
        padding:3px 5px;
        font-size:10px;
    `);
    // Writes the widget only; its observer re-renders (`onDriverLaneChanged`).
    select.addEventListener("change", () => setDriverLaneWidgetValue(node, select.value));
    const status = style(document.createElement("div"), "color:#7f8d9b;font-size:10px;line-height:1.25;");
    const list = style(document.createElement("div"), "display:flex;flex-direction:column;max-height:138px;overflow:auto;");
    wrapper.append(header, select, status, list);
    const domWidget = node.addDOMWidget("sonder_driver_selector_panel", "SonderDriverSelectorPanel", wrapper, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => 92,
        getMaxHeight: () => 196,
        getHeight: () => 172,
    });
    domWidget.computeSize = (width) => [width, 172];
    state.driverPanel = wrapper;
    state.driverSelect = select;
    state.driverStatus = status;
    state.driverList = list;
    scheduleBridgeDriverRefresh({ origin: "lifecycle", targets: [node] });
}

// ── Per-node install ─────────────────────────────────────────────────
const installNode = (node) => {
    if (!isAnySonderBridge(node)) return;
    const state = ensureNodeState(node);
    if (state.initialized) return;
    state.initialized = true;
    let visibilityChanged = false;

    if (isStart(node)) {
        const widget = (node.widgets || []).find((w) => w?.name === "iteration_index");
        visibilityChanged = setWidgetHidden(widget, true) || visibilityChanged;
        const overridesWidget = (node.widgets || []).find((w) => w?.name === OVERRIDES_WIDGET);
        visibilityChanged = setWidgetHidden(overridesWidget, true) || visibilityChanged;
        installGuidePanel(node);
    }

    if (isDriverSelector(node)) {
        const laneWidget = (node.widgets || []).find((w) => w?.name === DRIVER_LANE_WIDGET);
        visibilityChanged = setWidgetHidden(laneWidget, true) || visibilityChanged;
        const overridesWidget = (node.widgets || []).find((w) => w?.name === DRIVER_OVERRIDES_WIDGET);
        visibilityChanged = setWidgetHidden(overridesWidget, true) || visibilityChanged;
        installDriverPanel(node);
        observeWidgetValue(node, DRIVER_LANE_WIDGET, () => onDriverLaneChanged(node));
    }

    if (visibilityChanged) commitWidgetVisibility(node, { resize: false });

    const original = node.onConnectionsChange;
    node.onConnectionsChange = function (...args) {
        const result = original?.apply(this, args);
        try {
            if (isBridge(this)) refreshNodeShape(this);
            if (isStart(this)) {
                mirrorProjectWire(this);
                scheduleBridgeGuideRefresh({ origin: "lifecycle", targets: [this] });
            }
            if (isDriverSelector(this)) scheduleBridgeDriverRefresh({ origin: "lifecycle", targets: [this] });
        } catch (e) {
            console.warn("[Sonder Bridge] connection-change handler error:", e);
        }
        return result;
    };

    // Defer initial shape so post-load connection state is settled before measuring.
    window.setTimeout(() => {
        if (!isAnySonderBridge(node)) return;
        try {
            if (isBridge(node)) refreshNodeShape(node);
            if (isStart(node)) {
                mirrorProjectWire(node);
                scheduleBridgeGuideRefresh({ origin: "lifecycle", targets: [node] });
            }
            if (isDriverSelector(node)) scheduleBridgeDriverRefresh({ origin: "lifecycle", targets: [node] });
        } catch (e) {
            console.warn("[Sonder Bridge] initial-shape error:", e);
        }
    }, 0);
};

/**
 * A project version moved. Guides Start nodes of that project re-read unless
 * change certificates prove `bridge-guides` unchanged since the payload they
 * hold (it reads only `bridge`-projected scene fields plus asset names, and an
 * asset rename is never certified). Driver Selectors of that project always
 * re-read: clips and Driver lanes are outside both certificate projections.
 * Unresolved nodes wait for their own project-ready retry.
 */
function refreshBridgesForVersion(projectId, modifiedAt) {
    const notice = { projectId, modifiedAt };
    const guides = allGraphNodes().filter(isStart).filter((node) => {
        const context = bridgeReadContext(node, "bridge-guides");
        if (!context.url) return false;
        const { refresh, accepted } = versionStillAnswers(node[GUIDE_ACCEPTED], context, notice, "bridge");
        node[GUIDE_ACCEPTED] = accepted;
        return refresh;
    });
    const drivers = allGraphNodes().filter(isDriverSelector).filter((node) => {
        const context = bridgeReadContext(node, "bridge-drivers");
        return !!context.url && versionStillAnswers(null, context, notice, "").refresh;
    });
    const wave = { origin: "project_version", modifiedAt, delayMs: 250 };
    if (guides.length) scheduleBridgeGuideRefresh({ ...wave, targets: guides });
    if (drivers.length) scheduleBridgeDriverRefresh({ ...wave, targets: drivers });
}

// A scene switch is a widget change, not a project version, and both routes
// read the active scene. Only nodes reading the switching editor's project.
function refreshBridgesForScene(detail = {}) {
    if (detail.field !== "scene_id") return;
    const projectDir = String(detail.projectDir || "");
    if (!projectDir) return;
    // A node already holding a settled read of its current URL is unaffected:
    // another Editor on the same project switched, or a remote widget-state
    // replay repeated the scene. A read in flight always re-reads, so a quick
    // A -> B -> A switch cannot leave B's late result on screen.
    const needs = (node, route, heldUrl) => {
        const context = bridgeReadContext(node, route);
        return context.projectDir === projectDir && !(heldUrl && context.url && heldUrl === context.url);
    };
    const guides = allGraphNodes().filter((node) => isStart(node)
        && needs(node, "bridge-guides", node[GUIDE_ACCEPTED]?.url));
    const drivers = allGraphNodes().filter((node) => {
        if (!isDriverSelector(node)) return false;
        const state = ensureNodeState(node);
        return needs(node, "bridge-drivers", state.driverLoading ? "" : state.driverPayload?.url);
    });
    if (guides.length) scheduleBridgeGuideRefresh({ origin: "scene", targets: guides, delayMs: 250 });
    if (drivers.length) scheduleBridgeDriverRefresh({ origin: "scene", targets: drivers, delayMs: 250 });
}

app.registerExtension({
    name: EXT_NAME,
    setup() {
        onProjectVersionChanged(refreshBridgesForVersion);
        onEditorRenderWindowChanged(refreshBridgesForScene);
    },
    async nodeCreated(node) {
        if (!isAnySonderBridge(node)) return;
        installNode(node);
    },
});
