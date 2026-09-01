// Sonder Reference Bridge frontend shape.
// Each backend publishes a fixed homogeneous tuple. This module only decides
// how much of the r/a/p tail LiteGraph displays and how every socket is labelled.
// Three rules hold that safe:
//   1. A connected slot is never removed, whatever the recipe says. It is still
//      marked — see rule 1 in reference_bridge_shape.js for why wiring answers a
//      different question than liveness.
//   2. An unresolved project shows everything — a slow project load must not
//      look like a broken node.
//   3. The workflow widget owns dead-slot emission; the canvas only explains
//      when `nothing` would feed a required consumer.

import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";
import { resolveProjectSource, getGraphLink, getGraphNode } from "./project_source_resolver.js";
import { onProjectVersionChanged } from "./api_client.js";
import { onEditorRenderWindowChanged } from "./editor_render_window_events.js";
import { PRIORITY as KEY_PRIORITY, register as registerKeyboardConsumer } from "./keyboard_ownership.js";
import {
    allocateBridgeReferenceGeneration,
    requestBridgeReferencePayload,
} from "./bridge_reference_coordinator.js";
import {
    MAX_REFERENCE_SLOTS,
    SLOT_NAME_RE,
    canonicalOutputOrder,
    distillInputDefinition,
    inputRequirement,
    mergedBridgeShape,
    parseLaneSelection,
    resolveBridgeOutputs,
    selectorPanelView,
} from "./reference_bridge_shape.js";

const EXT_NAME = "sonder.reference_bridge";
const SELECTOR = "SonderReferenceSelector";
const BRIDGES = new Set([
    "SonderReferenceImageBridge",
    "SonderReferenceAudioBridge",
    "SonderReferencePromptBridge",
]);
const STATE = Symbol("sonderReferenceBridgeState");
const SELECTOR_STATE = Symbol("sonderReferenceSelectorState");
const INPUT_DEFINITIONS = new Map();

const nodeType = (node) => String(node?.comfyClass || node?.type || "");
const findWidget = (node, name) => (node?.widgets || []).find((widget) => widget?.name === name) || null;

function referenceBridgeUrl(projectId, sceneId, controllerState) {
    const params = new URLSearchParams();
    for (const [name, value] of [
        ["selection_start", controllerState?.selectionStart],
        ["selection_end", controllerState?.selectionEnd],
        ["pre_context_frames", controllerState?.preContextFrames],
        ["post_context_frames", controllerState?.postContextFrames],
    ]) {
        const number = Number(value);
        if (Number.isFinite(number)) params.set(name, String(Math.trunc(number)));
    }
    const query = params.toString();
    const path = `/sonder-editor/project/${encodeURIComponent(projectId)}/scenes/${encodeURIComponent(sceneId)}/bridge-references`;
    return query ? `${path}?${query}` : path;
}

function ensureState(node) {
    if (node[STATE]) return node[STATE];
    // Capture every currently materialized output so a later grow restores its
    // original type, label and tooltip. Ordering always comes from the backend
    // tuple contract, never from a possibly shrunk saved node.
    const metadata = new Map();
    for (const slot of node.outputs || []) {
        const name = String(slot?.name || "");
        if (!name) continue;
        metadata.set(name, {
            type: slot.type || "IMAGE",
            label: slot.label,
            localized_name: slot.localized_name,
            tooltip: slot.tooltip,
        });
    }
    node[STATE] = {
        metadata,
        order: canonicalOutputOrder(nodeType(node)),
        initialized: false,
        refreshToken: 0,
    };
    return node[STATE];
}

export function applyReferenceBridgeShape(node, shape = {}) {
    if (!BRIDGES.has(nodeType(node))) return;
    const state = ensureState(node);
    const resolvedShape = {
        ...shape,
        unusedSlots: String(findWidget(node, "unused_slots")?.value || "placeholder"),
        requiredConsumerSlots: requiredConsumerSlotNames(node),
    };
    const changed = resolveBridgeOutputs(
        node,
        resolvedShape,
        { metadata: state.metadata, order: state.order },
    );
    // Project writes are frequent and mostly unrelated to this node; only a real
    // slot change earns a resize and a canvas repaint.
    if (!changed) return;
    node.setSize?.([node.size?.[0] || 280, Math.max(node.computeSize?.()[1] || 0, 90)]);
    app.graph?.setDirtyCanvas?.(true, true);
}

function requiredConsumerSlotNames(node) {
    if (!findWidget(node, "unused_slots")) return [];
    const graph = node?.graph || app.graph;
    const required = new Set();
    for (const output of node?.outputs || []) {
        const slotName = String(output?.name || "");
        if (!SLOT_NAME_RE.test(slotName)) continue;
        const linkIds = Array.isArray(output?.links)
            ? output.links
            : (output?.link != null ? [output.link] : []);
        for (const linkId of linkIds) {
            const link = getGraphLink(graph, linkId);
            const target = getGraphNode(graph, link?.target_id);
            const targetInput = target?.inputs?.[Number(link?.target_slot)];
            const definition = INPUT_DEFINITIONS.get(nodeType(target));
            if (inputRequirement(definition, targetInput?.name) === "required") {
                required.add(slotName);
                break;
            }
        }
    }
    return [...required];
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
    imageSlotCount: MAX_REFERENCE_SLOTS,
    audioSlotCount: MAX_REFERENCE_SLOTS,
    promptSlotCount: MAX_REFERENCE_SLOTS,
    liveOutputs: null,
    slotLabels: [],
    imageSlotLabels: [],
};

async function referenceShapeForBridge(node, wave) {
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
            controller.whenProjectReady(() => scheduleReferenceRefresh({
                origin: "project_ready",
                targets: [node],
            }));
        }
        return FULL_SHAPE;
    }
    const payload = await requestBridgeReferencePayload({
        url: api.apiURL(referenceBridgeUrl(projectId, sceneId, controllerState)),
        generation: wave.generation,
        origin: wave.origin,
    });
    const selection = parseLaneSelection(findWidget(selector, "reference_lanes")?.value);
    return mergedBridgeShape({
        lanes: Array.isArray(payload?.references) ? payload.references : [],
        laneIndices: selection.laneIndices,
    });
}

function refreshShape(node, wave) {
    if (!BRIDGES.has(nodeType(node))) return;
    const state = ensureState(node);
    const token = ++state.refreshToken;
    referenceShapeForBridge(node, wave)
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

// Every refresh origin enters this scheduler. It owns logical generations;
// node-local consumers own only application tokens. Automatic calls landing in
// one scheduled turn share a wave, while project versions retain a stable
// identity across duplicate notifications. Explicit refreshes always allocate
// post-click work.
const pendingRefreshWaves = new Map();

function scheduleReferenceRefresh({
    origin = "refresh",
    projectId = "",
    modifiedAt = "",
    force = false,
    targets = null,
    delayMs = 0,
} = {}) {
    const normalizedOrigin = String(origin || "refresh");
    // Version healing emits once for the canonical UUID and once for the
    // folder alias. Both signals refresh every Reference node, and the
    // coordinator already keys physical work by the complete resource URL, so
    // generation identity must not embed the alias spelling. Otherwise one
    // response creates an active request plus an identical trailing request.
    const stableGeneration = !force && modifiedAt
        ? `project-version:${String(modifiedAt)}`
        : "";
    const generation = force
        ? allocateBridgeReferenceGeneration(normalizedOrigin)
        : (stableGeneration || pendingRefreshWaves.get("automatic")?.generation
            || allocateBridgeReferenceGeneration(normalizedOrigin));
    const waveKey = force ? generation : (stableGeneration || "automatic");
    let wave = pendingRefreshWaves.get(waveKey);
    let created = false;
    if (!wave) {
        created = true;
        wave = {
            generation,
            origins: new Set(),
            targets: new Set(),
            all: false,
            timer: null,
        };
        pendingRefreshWaves.set(waveKey, wave);
    }
    wave.origins.add(normalizedOrigin);
    if (targets == null) {
        wave.all = true;
        wave.targets.clear();
    } else if (!wave.all) {
        for (const node of targets) {
            if (node) wave.targets.add(node);
        }
    }
    if (created) {
        wave.timer = window.setTimeout(() => {
            if (pendingRefreshWaves.get(waveKey) !== wave) return;
            pendingRefreshWaves.delete(waveKey);
            const dispatch = {
                generation: wave.generation,
                origin: [...wave.origins].sort().join("+") || "refresh",
            };
            const nodes = wave.all ? [...(app.graph?._nodes || [])] : [...wave.targets];
            for (const node of nodes) {
                if (BRIDGES.has(nodeType(node))) refreshShape(node, dispatch);
                else if (nodeType(node) === SELECTOR) refreshSelectorPanel(node, dispatch);
            }
        }, Math.max(0, Number(delayMs) || 0));
    }
    return wave.generation;
}

function refreshAllBridges(projectId, modifiedAt) {
    scheduleReferenceRefresh({
        origin: "project_version",
        projectId,
        modifiedAt,
        targets: null,
        delayMs: 250,
    });
}

function refreshAllBridgesForWindow() {
    scheduleReferenceRefresh({
        origin: "render_window",
        targets: null,
        delayMs: 250,
    });
}

function downstreamBridges(selector) {
    const graph = selector?.graph || app.graph;
    const output = (selector?.outputs || []).find((slot) => slot?.name === "reference_set") || selector?.outputs?.[0];
    const links = Array.isArray(output?.links) ? output.links : (output?.link != null ? [output.link] : []);
    const bridges = [];
    for (const linkId of links) {
        const link = getGraphLink(graph, linkId);
        const target = getGraphNode(graph, link?.target_id);
        if (BRIDGES.has(nodeType(target))) bridges.push(target);
    }
    return bridges;
}

function selectorRefreshTargets(selector) {
    return [selector, ...downstreamBridges(selector)];
}

// ── Selector lane panel ───────────────────────────────────────────────
// The STRING widget is the sole workflow-embedded authority. Rendering parses
// but never rewrites its authored text; only explicit add/remove actions write
// the canonical sorted selection back. Menu/list state is ephemeral UI state.

const style = (element, css) => { element.style.cssText = css; return element; };
const SELECTOR_PANEL_MIN_HEIGHT = 96;
const SELECTOR_PANEL_MAX_HEIGHT = 260;

function selectorState(node) {
    if (!node[SELECTOR_STATE]) {
        node[SELECTOR_STATE] = {
            installed: false,
            refreshToken: 0,
            panelHeight: SELECTOR_PANEL_MIN_HEIGHT,
            menuOpen: false,
            unregisterKeyboard: null,
            outsidePointer: null,
        };
    }
    return node[SELECTOR_STATE];
}

function setSelectorLanes(node, values) {
    const widget = findWidget(node, "reference_lanes");
    if (!widget) return;
    const laneIndices = [...new Set((Array.isArray(values) ? values : [])
        .map((value) => Number(value))
        .filter((value) => Number.isSafeInteger(value) && value >= 0))]
        .sort((left, right) => left - right);
    const authored = laneIndices.join(", ");
    if (String(widget.value ?? "") === authored) return;
    widget.value = authored;
    widget.callback?.(authored);
    app.graph?.setDirtyCanvas?.(true, true);
}

function closeSelectorMenu(node) {
    const state = selectorState(node);
    if (!state.menuOpen) return;
    state.menuOpen = false;
    if (state.menu) state.menu.style.display = "none";
    state.addButton?.setAttribute?.("aria-expanded", "false");
    state.unregisterKeyboard?.();
    state.unregisterKeyboard = null;
    if (state.outsidePointer) window.removeEventListener("pointerdown", state.outsidePointer, true);
    state.outsidePointer = null;
}

function openSelectorMenu(node) {
    const state = selectorState(node);
    if (!state.menu || state.menuOpen) return;
    state.menuOpen = true;
    state.menu.style.display = "flex";
    state.addButton?.setAttribute?.("aria-expanded", "true");
    state.outsidePointer = (event) => {
        if (state.menu?.contains?.(event.target) || state.addButton?.contains?.(event.target)) return;
        closeSelectorMenu(node);
    };
    window.addEventListener("pointerdown", state.outsidePointer, true);
    state.unregisterKeyboard = registerKeyboardConsumer({
        id: `reference-selector-menu:${node.id ?? "unassigned"}`,
        priority: KEY_PRIORITY.OVERLAY,
        keydown(event) {
            if (event?.isComposing || event?.key !== "Escape") return false;
            closeSelectorMenu(node);
            return true;
        },
    });
}

function toggleSelectorMenu(node) {
    const state = selectorState(node);
    if (state.menuOpen) closeSelectorMenu(node);
    else openSelectorMenu(node);
}

function selectorPanelHeight(view) {
    const rows = Math.max(1, view.rows.length);
    const disclosures = view.disclosures.length;
    const chips = view.outputs.length || view.tags.length ? 24 : 0;
    return Math.max(
        SELECTOR_PANEL_MIN_HEIGHT,
        Math.min(SELECTOR_PANEL_MAX_HEIGHT, 68 + (rows * 34) + (disclosures * 18) + chips),
    );
}

async function selectorLanePayload(node, wave) {
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
            controller.whenProjectReady(() => scheduleReferenceRefresh({
                origin: "project_ready",
                targets: [node],
            }));
        }
        return { status: projectId ? "No active scene." : "Loading project...", lanes: [], linked: false };
    }
    const payload = await requestBridgeReferencePayload({
        url: api.apiURL(referenceBridgeUrl(projectId, sceneId, controllerState)),
        generation: wave.generation,
        origin: wave.origin,
    });
    return {
        status: "",
        lanes: Array.isArray(payload?.references) ? payload.references : [],
        tagPresets: Array.isArray(payload?.tag_presets) ? payload.tag_presets : [],
        tagFamilies: payload?.tag_families && typeof payload.tag_families === "object"
            ? payload.tag_families : {},
        sceneName: String(payload?.scene_name || ""),
        source: String(payload?.source || "live"),
        linked: true,
    };
}

function renderSelectorPanel(node, payload) {
    const state = selectorState(node);
    if (!state.rows) return;
    const selection = parseLaneSelection(findWidget(node, "reference_lanes")?.value);
    const view = selectorPanelView({
        lanes: payload.lanes,
        laneIndices: selection.laneIndices,
        invalidTokens: selection.invalidTokens,
        status: payload.status,
        sceneName: payload.sceneName,
        source: payload.source,
        tagPresets: payload.tagPresets,
        tagFamilies: payload.tagFamilies,
    });
    state.view = view;

    state.rows.replaceChildren();
    if (!view.rows.length) {
        const empty = style(document.createElement("div"), "color:#7f8d9b;font-size:10px;line-height:1.25;padding:3px 1px;");
        empty.textContent = "No Reference lanes selected.";
        state.rows.appendChild(empty);
    }
    for (const row of view.rows) {
        const line = style(document.createElement("div"), `
            display:flex;align-items:flex-start;gap:5px;padding:4px 5px;box-sizing:border-box;
            border:1px solid rgba(126,168,201,0.3);border-radius:5px;background:rgba(14,19,25,0.92);
        `);
        const copy = style(document.createElement("div"), "min-width:0;flex:1;");
        const label = style(document.createElement("div"), "color:#cfd7df;font-size:10px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;");
        label.textContent = row.label;
        label.title = row.label;
        const detail = style(document.createElement("div"), "color:#7f8d9b;font-size:9px;line-height:1.25;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;");
        detail.textContent = row.status;
        detail.title = row.status;
        copy.append(label, detail);
        const remove = style(document.createElement("button"), `
            flex:0 0 auto;border:1px solid rgba(190,125,157,0.42);border-radius:5px;
            background:rgba(14,19,25,0.92);color:#d9bcca;font-size:11px;padding:1px 5px;cursor:pointer;
        `);
        remove.type = "button";
        remove.textContent = "×";
        remove.title = `Remove lane ${row.laneIndex}`;
        remove.addEventListener("click", () => {
            closeSelectorMenu(node);
            setSelectorLanes(node, view.laneIndices.filter((value) => value !== row.laneIndex));
        });
        line.append(copy, remove);
        state.rows.appendChild(line);
    }
    state.status.textContent = view.status;

    state.disclosures.replaceChildren();
    for (const disclosure of view.disclosures) {
        const line = style(document.createElement("div"), "color:#d9bcca;font-size:9px;line-height:1.25;");
        line.textContent = disclosure;
        state.disclosures.appendChild(line);
    }

    state.chips.replaceChildren();
    for (const tag of view.tags) {
        const chip = style(document.createElement("span"), `
            display:inline-block; font-size:9px; padding:1px 5px; margin:1px;
            border:1px solid rgba(190,125,157,0.42); border-radius:8px; color:#d9bcca;
        `);
        chip.textContent = tag;
        chip.title = "Tag on a member staged in this lane";
        state.chips.appendChild(chip);
    }
    for (const name of view.outputs) {
        const chip = style(document.createElement("span"), `
            display:inline-block; font-size:9px; padding:1px 5px; margin:1px;
            border:1px solid rgba(126,168,201,0.3); border-radius:8px; color:#a8b6c4;
        `);
        chip.textContent = name;
        chip.title = "Output this recipe drives";
        state.chips.appendChild(chip);
    }

    state.menu.replaceChildren();
    const entries = view.addable.length ? view.addable : [{
        label: payload.linked ? "No other lanes" : "No lanes available",
        disabled: true,
        reason: payload.status || "There are no additional Reference lanes in this scene.",
    }];
    for (const entry of entries) {
        const option = style(document.createElement("button"), `
            width:100%;box-sizing:border-box;text-align:left;border:0;border-radius:5px;
            background:rgba(14,19,25,0.92);color:#cfd7df;font-size:10px;padding:5px 7px;cursor:pointer;
        `);
        option.type = "button";
        option.textContent = entry.label;
        option.disabled = Boolean(entry.disabled);
        option.title = entry.reason || "Add this Reference lane";
        if (!option.disabled) {
            option.addEventListener("click", () => {
                closeSelectorMenu(node);
                setSelectorLanes(node, [...view.laneIndices, entry.laneIndex]);
            });
        }
        state.menu.appendChild(option);
    }

    state.panelHeight = selectorPanelHeight(view);
    const panelOverhead = 68 + (view.disclosures.length * 18)
        + ((view.outputs.length || view.tags.length) ? 24 : 0);
    const availableRowsHeight = Math.max(34, state.panelHeight - panelOverhead);
    state.rows.style.maxHeight = `${availableRowsHeight}px`;
    state.rows.style.overflowY = view.rows.length * 34 > availableRowsHeight ? "auto" : "hidden";
    const computed = node.computeSize?.();
    if (Array.isArray(computed)) node.setSize?.([node.size?.[0] || computed[0], computed[1]]);
    app.graph?.setDirtyCanvas?.(true, true);
}

function refreshSelectorPanel(node, wave) {
    if (nodeType(node) !== SELECTOR) return;
    const state = selectorState(node);
    if (!state.panel) return;
    const token = ++state.refreshToken;
    state.status.textContent = "Loading Reference lanes...";
    selectorLanePayload(node, wave)
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
    const wrapper = style(document.createElement("div"), "display:flex;flex-direction:column;gap:5px;width:100%;box-sizing:border-box;padding-top:2px;position:relative;");
    const header = style(document.createElement("div"), "display:flex;justify-content:space-between;align-items:center;gap:6px;color:#cfd7df;font-size:10px;font-weight:700;");
    const title = document.createElement("span");
    title.textContent = "Reference Selector";
    const actions = style(document.createElement("div"), "display:flex;align-items:center;gap:4px;");
    const addButton = style(document.createElement("button"), `
        border:1px solid rgba(126,168,201,0.35);border-radius:5px;background:rgba(14,19,25,0.92);
        color:#dbe4ed;font-size:11px;padding:1px 7px;cursor:pointer;
    `);
    addButton.type = "button";
    addButton.textContent = "+";
    addButton.title = "Add a Reference lane";
    addButton.setAttribute("aria-haspopup", "menu");
    addButton.setAttribute("aria-expanded", "false");
    addButton.addEventListener("click", () => toggleSelectorMenu(node));
    const refreshBtn = style(document.createElement("button"), `
        border:1px solid rgba(126,168,201,0.35); border-radius:5px; background:rgba(14,19,25,0.92);
        color:#dbe4ed; font-size:10px; padding:2px 6px; cursor:pointer;
    `);
    refreshBtn.textContent = "Refresh";
    refreshBtn.addEventListener("click", () => scheduleReferenceRefresh({
        origin: "selector_manual_refresh",
        force: true,
        targets: [node],
    }));
    actions.append(addButton, refreshBtn);
    header.append(title, actions);

    const rows = style(document.createElement("div"), "display:flex;flex-direction:column;gap:4px;min-height:0;");
    const status = style(document.createElement("div"), "color:#7f8d9b;font-size:10px;line-height:1.25;");
    const disclosures = style(document.createElement("div"), "display:flex;flex-direction:column;gap:2px;");
    const chips = style(document.createElement("div"), "display:flex;flex-wrap:wrap;max-height:48px;overflow:auto;");
    const menu = style(document.createElement("div"), `
        display:none;flex-direction:column;gap:2px;position:absolute;right:0;top:24px;z-index:10;
        width:min(250px,100%);max-height:170px;overflow:auto;padding:4px;box-sizing:border-box;
        border:1px solid rgba(126,168,201,0.35);border-radius:5px;background:#17202a;
    `);
    menu.setAttribute("role", "menu");
    wrapper.append(header, rows, status, disclosures, chips, menu);

    const domWidget = node.addDOMWidget("sonder_reference_selector_panel", "SonderReferenceSelectorPanel", wrapper, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => SELECTOR_PANEL_MIN_HEIGHT,
        getMaxHeight: () => SELECTOR_PANEL_MAX_HEIGHT,
        getHeight: () => state.panelHeight,
    });
    domWidget.computeSize = (width) => [width, state.panelHeight];
    state.panel = wrapper;
    state.domWidget = domWidget;
    state.addButton = addButton;
    state.rows = rows;
    state.status = status;
    state.disclosures = disclosures;
    state.chips = chips;
    state.menu = menu;
    scheduleReferenceRefresh({
        origin: "selector_install",
        targets: [node],
    });
}

function installSelector(node) {
    if (nodeType(node) !== SELECTOR) return;
    const state = selectorState(node);
    installSelectorPanel(node);
    if (state.installed) return;
    state.installed = true;
    const laneWidget = findWidget(node, "reference_lanes");
    if (laneWidget) {
        const originalCallback = laneWidget.callback;
        laneWidget.callback = function (...args) {
            const result = originalCallback?.apply(this, args);
            scheduleReferenceRefresh({
                origin: "selector_widget",
                targets: selectorRefreshTargets(node),
            });
            return result;
        };
    }
    const originalConnections = node.onConnectionsChange;
    node.onConnectionsChange = function (...args) {
        const result = originalConnections?.apply(this, args);
        // Wiring the project input is what makes the lane list resolvable.
        scheduleReferenceRefresh({
            origin: "selector_connection",
            targets: selectorRefreshTargets(this),
        });
        return result;
    };
    const originalRemoved = node.onRemoved;
    node.onRemoved = function (...args) {
        closeSelectorMenu(this);
        return originalRemoved?.apply(this, args);
    };
}

function install(node) {
    if (!BRIDGES.has(nodeType(node))) return;
    const state = ensureState(node);
    if (state.initialized) return;
    state.initialized = true;
    const originalConnections = node.onConnectionsChange;
    node.onConnectionsChange = function (...args) {
        const result = originalConnections?.apply(this, args);
        scheduleReferenceRefresh({
            origin: "bridge_connection",
            targets: [this],
        });
        return result;
    };
    const unusedSlotsWidget = findWidget(node, "unused_slots");
    if (unusedSlotsWidget) {
        const originalCallback = unusedSlotsWidget.callback;
        unusedSlotsWidget.callback = function (...args) {
            const result = originalCallback?.apply(this, args);
            scheduleReferenceRefresh({
                origin: "bridge_widget",
                targets: [node],
            });
            return result;
        };
    }
    const originalMenu = node.getExtraMenuOptions;
    node.getExtraMenuOptions = function (canvas, options) {
        const result = originalMenu?.apply(this, arguments);
        options?.push({
            content: "Refresh reference slots",
            callback: () => scheduleReferenceRefresh({
                origin: "bridge_manual_refresh",
                force: true,
                targets: [this],
            }),
        });
        return result;
    };
    scheduleReferenceRefresh({
        origin: "bridge_install",
        targets: [node],
    });
}

app.registerExtension({
    name: EXT_NAME,
    beforeRegisterNodeDef(_nodeType, nodeData) {
        const name = String(nodeData?.name || "");
        if (name) INPUT_DEFINITIONS.set(name, distillInputDefinition(nodeData));
    },
    setup() {
        onProjectVersionChanged(refreshAllBridges);
        onEditorRenderWindowChanged(refreshAllBridgesForWindow);
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
