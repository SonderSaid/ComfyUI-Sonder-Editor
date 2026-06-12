const { app } = window.comfyAPI.app;
const { api } = window.comfyAPI.api;

import { EditorNodeController } from "./editor_node_controller.js";
import {
    CUSTOM_AUDIO_CODEC_OPTIONS,
    CUSTOM_CONTAINER_OPTIONS,
    CUSTOM_ENCODER_PRESET_OPTIONS,
    CUSTOM_OUTPUT_KIND_OPTIONS,
    CUSTOM_OUTPUT_KIND_PNG_SEQUENCE,
    CUSTOM_OUTPUT_KIND_VIDEO,
    CUSTOM_PIX_FMT_OPTIONS,
    CUSTOM_VIDEO_CODEC_OPTIONS,
    DEFAULT_SAVE_PRESET,
    SAVE_PRESET_OPTIONS,
    getTemplateById,
    getEditorSettings,
    notificationCoreConfig,
    resolveFrameConstraintForTemplate,
    snapToConstraint,
    subscribeEditorSettings,
} from "./editor_settings.js";
import { FONT, THEME, chromeInputCss, injectSonderFontFaces } from "./editor_theme.js";
import { mountToastStack } from "./editor_toast_stack.js";
import { notifyProgress, configureNotifications } from "./editor_notifications.js";

injectSonderFontFaces();

// ── Encode progress → notification foreground pill ───────────────────────────
// ComfyUI fires a native "progress" event for the executing node (every node
// emits it), so we filter to SonderSaveVideo nodes only and map their progress
// into a Core foreground notification keyed by node id. The tab page never
// receives these (its api shim's addEventListener is a no-op) — canvas only.
const _sonderEncodeNotifs = new Map(); // nodeId(string) -> notification handle

function _sonderSaveNodeInfo(nodeId) {
    try {
        const node = app.graph?.getNodeById?.(Number(nodeId)) || app.graph?._nodes_by_id?.[nodeId];
        if (node && (node.type === "SonderSaveVideo" || node.comfyClass === "SonderSaveVideo")) {
            return { title: node.title || "Save Video" };
        }
    } catch (_) {}
    return null;
}

function _sonderHandleProgressEvent(detail) {
    const nodeId = detail?.node;
    if (nodeId == null) return;
    const info = _sonderSaveNodeInfo(nodeId);
    if (!info) return; // not a Sonder save node
    const key = String(nodeId);
    const max = Number(detail?.max) || 0;
    const value = Number(detail?.value) || 0;
    const progress = max > 0 ? { current: value, total: max, unit: "f" } : null;
    let handle = _sonderEncodeNotifs.get(key);
    if (!handle) {
        handle = notifyProgress({ verb: "Encoding", message: info.title, progress, foreground: true, source: `encode:${key}` });
        _sonderEncodeNotifs.set(key, handle);
    } else {
        handle.update({ progress });
    }
    if (max > 0 && value >= max) {
        handle.resolve({ message: "Encode complete" });
        _sonderEncodeNotifs.delete(key);
    }
}

// Execution moved on (or finished): resolve any encode notif whose node is no
// longer executing — it finished without the WS hitting max.
function _sonderResolveEncodeExcept(activeNodeId) {
    const activeKey = activeNodeId == null ? null : String(activeNodeId);
    for (const [key, handle] of [..._sonderEncodeNotifs]) {
        if (key === activeKey) continue;
        handle.resolve({ message: "Encode complete" });
        _sonderEncodeNotifs.delete(key);
    }
}

function _sonderClearEncodeNotifs({ error = false, message = "" } = {}) {
    for (const [key, handle] of [..._sonderEncodeNotifs]) {
        if (error) handle.resolve({ tier: "error", message: message || "Encode failed." });
        else handle.dismiss();
        _sonderEncodeNotifs.delete(key);
    }
}

function style(el, cssText) {
    el.style.cssText = cssText;
    return el;
}

function snapFpsToTemplate(fps, template) {
    const numeric = Math.max(0, Number(fps) || 0);
    const constraint = template?.constraints?.fps;
    if (template?.id === "free" || !constraint || typeof constraint !== "object") {
        return numeric;
    }
    return Number(snapToConstraint(numeric, constraint).toFixed(3));
}

// ── Widget hide/show helpers ───────────────────────────────────────────
function hideWidget(node, widget) {
    if (widget.hidden) return;
    widget.hidden = true;
    widget._sonder_origComputeSize = widget.computeSize;
    widget.computeSize = () => [0, -4];
}

function showWidget(node, widget) {
    if (!widget.hidden) return;
    widget.hidden = false;
    if (widget._sonder_origComputeSize) {
        widget.computeSize = widget._sonder_origComputeSize;
        delete widget._sonder_origComputeSize;
    } else {
        delete widget.computeSize;
    }
}

// ── Utility: get project directory from project dropdown value ─────────
async function getProjectDir(projectValue) {
    if (!projectValue || projectValue === "+ Create New") return "";
    try {
        const resp = await fetch(api.apiURL("/sonder-editor/projects"));
        if (!resp.ok) return "";

        const data = await resp.json();
        const match = (data.projects || []).find((project) => {
            const dirName = project.path.split(/[/\\]/).pop();
            return dirName === projectValue || project.name === projectValue;
        });
        return match ? match.path : "";
    } catch (e) {
        console.warn("[Sonder] Failed to get project dir:", e);
    }
    return "";
}

async function listProjects() {
    const resp = await fetch(api.apiURL("/sonder-editor/projects"));
    if (!resp.ok) {
        throw new Error(`Failed to list projects: ${resp.status}`);
    }
    const data = await resp.json();
    return data.projects || [];
}

async function syncProjectWidgetChoices(projectWidget) {
    if (!projectWidget) return [];
    const projects = await listProjects();
    const values = ["+ Create New", ...projects.map((project) => project.path.split(/[/\\]/).pop())];
    projectWidget.options = projectWidget.options || {};
    projectWidget.options.values = values;
    return projects;
}

function normalizeFolderValue(value) {
    return String(value || "").trim().replace(/\\/g, "/").replace(/^\/+/, "").replace(/\/+$/, "");
}

function uniqueFolderValues(values) {
    const seen = new Set();
    const result = [];
    for (const value of values || []) {
        const normalized = normalizeFolderValue(value);
        if (seen.has(normalized)) continue;
        seen.add(normalized);
        result.push(normalized);
    }
    return result;
}

function projectIdFromProjectValue(projectValue) {
    const value = String(projectValue || "").trim();
    if (!value || value === "+ Create New") return "";
    return value.split(/[/\\]/).pop();
}

async function listProjectAssetFolders(projectId) {
    if (!projectId) return [];
    const resp = await fetch(api.apiURL(`/sonder-editor/project/${encodeURIComponent(projectId)}/assets/dormant`));
    if (!resp.ok) {
        throw new Error(`Failed to list asset folders: ${resp.status}`);
    }
    const data = await resp.json();
    return uniqueFolderValues(data?.folders || []).filter(Boolean);
}

function showSonderToast(message, duration = 2600) {
    console.info(`[Sonder] ${message}`);
    const doc = globalThis.document;
    if (!doc?.body) return;
    let toast = doc.getElementById("sonder-global-toast");
    if (!toast) {
        toast = doc.createElement("div");
        toast.id = "sonder-global-toast";
        toast.style.cssText = `
            position:fixed;left:50%;bottom:22px;transform:translateX(-50%);
            z-index:100000;padding:8px 12px;border-radius:6px;
            background:${THEME.bg2};border:1px solid ${THEME.line2};
            color:${THEME.fg0};font-family:${FONT.sans};font-size:12px;box-shadow:0 8px 24px rgba(0,0,0,0.35);
            opacity:0;transition:opacity 140ms ease;pointer-events:none;
        `;
        doc.body.appendChild(toast);
    }
    if (toast._sonderTimer) globalThis.clearTimeout(toast._sonderTimer);
    toast.textContent = message;
    toast.style.opacity = "1";
    toast._sonderTimer = globalThis.setTimeout(() => {
        toast.style.opacity = "0";
    }, duration);
}

async function openWorkflowJson(workflow, name = "Sonder Source Workflow") {
    if (!workflow || typeof workflow !== "object") {
        showSonderToast("Source workflow unavailable");
        return false;
    }
    const openWorkflow = app?.workflowManager?.openWorkflow;
    if (typeof openWorkflow === "function") {
        try {
            await openWorkflow.call(app.workflowManager, workflow, { name });
            return true;
        } catch (error) {
            console.warn("[Sonder] workflowManager.openWorkflow failed, falling back to loadGraphData:", error);
        }
    }
    if (typeof app?.loadGraphData !== "function") {
        showSonderToast("Source workflow unavailable");
        return false;
    }
    showSonderToast("Opening workflow will replace current canvas");
    return await withGraphLoadBypass(async () => {
        await app.loadGraphData(workflow);
        return true;
    });
}

async function openSourceWorkflowForAsset(projectDir, asset) {
    const projectId = projectIdFromProjectValue(projectDir);
    const assetId = asset?.asset_id || asset?.id || "";
    if (!projectId || !assetId) {
        showSonderToast("Source workflow unavailable");
        return false;
    }
    try {
        const url = `/sonder-editor/project/${encodeURIComponent(projectId)}/assets/${encodeURIComponent(assetId)}/workflow`;
        const resp = await fetch(api.apiURL(url));
        if (!resp.ok) {
            let reason = "unavailable";
            try {
                const payload = await resp.json();
                reason = payload?.reason || payload?.error || reason;
            } catch (_) { /* ignore */ }
            showSonderToast(`Source workflow ${reason}`);
            return false;
        }
        const payload = await resp.json();
        return await openWorkflowJson(payload?.workflow, asset?.name || "Sonder Source Workflow");
    } catch (error) {
        console.warn("[Sonder] Open Source Workflow failed:", error);
        showSonderToast("Source workflow unavailable");
        return false;
    }
}

if (typeof window !== "undefined") {
    window.__SONDER_OPEN_SOURCE_WORKFLOW__ = openSourceWorkflowForAsset;
}

async function createProjectFromNode(node, projectWidget) {
    const projectNameWidget = node.widgets.find((widget) => widget.name === "project_name");
    const fpsWidget = node.widgets.find((widget) => widget.name === "fps");
    const widthWidget = node.widgets.find((widget) => widget.name === "width");
    const heightWidget = node.widgets.find((widget) => widget.name === "height");
    const settings = getEditorSettings();
    const defaultSceneDuration = Math.max(1, Number(settings?.projectDefaults?.newSceneDuration || 200));
    const templateId = settings.projectDefaults.defaultTemplateId || "free";
    const template = getTemplateById(templateId, settings);

    const projectName = String(projectNameWidget?.value || "").trim();
    if (!projectName) {
        throw new Error("Project name is required");
    }

    const resp = await fetch(api.apiURL("/sonder-editor/project"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            name: projectName,
            fps: snapFpsToTemplate(Number(fpsWidget?.value || 24), template),
            width: Number(widthWidget?.value || 768),
            height: Number(heightWidget?.value || 512),
            template_id: templateId,
            frame_constraint: resolveFrameConstraintForTemplate(templateId, settings),
        }),
    });
    if (!resp.ok) {
        let message = `Project creation failed: ${resp.status}`;
        try {
            const data = await resp.json();
            if (data?.error) message = data.error;
        } catch {}
        throw new Error(message);
    }

    const created = await resp.json();
    const projects = await syncProjectWidgetChoices(projectWidget);
    const createdProject = projects.find((project) => project.project_id === created.project_id)
        || projects.find((project) => project.name === created.name);
    const nextValue = createdProject?.path?.split(/[/\\]/).pop();

    if (!nextValue) {
        throw new Error("Created project was not found after refreshing the project list");
    }

    if ((Number(createdProject.scene_count) || 0) <= 0) {
        const sceneResp = await fetch(api.apiURL(`/sonder-editor/project/${encodeURIComponent(nextValue)}/scenes`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                name: "Scene 1",
                duration_frames: defaultSceneDuration,
            }),
        });
        if (!sceneResp.ok) {
            let message = `Initial scene creation failed: ${sceneResp.status}`;
            try {
                const data = await sceneResp.json();
                if (data?.error) message = data.error;
            } catch {}
            throw new Error(message);
        }
    }

    projectWidget.value = nextValue;
    projectWidget.callback?.(nextValue);
    app.graph.setDirtyCanvas?.(true, true);
}

function applyProjectCreationDefaults(node) {
    if (!node?.widgets) return;
    const settings = getEditorSettings();
    const fpsWidget = node.widgets.find((widget) => widget.name === "fps");
    const widthWidget = node.widgets.find((widget) => widget.name === "width");
    const heightWidget = node.widgets.find((widget) => widget.name === "height");
    const template = getTemplateById(settings.projectDefaults.defaultTemplateId || "free", settings);
    if (fpsWidget) fpsWidget.value = snapFpsToTemplate(settings.projectDefaults.fps, template);
    if (widthWidget) widthWidget.value = settings.projectDefaults.width;
    if (heightWidget) heightWidget.value = settings.projectDefaults.height;
}

function applySaveVideoDefaults(node) {
    if (!node?.widgets) return;
    const presetWidget = node.widgets.find((widget) => widget.name === "save_preset");
    if (!presetWidget) return;
    const defaultPreset = getEditorSettings()?.render?.defaultSavePreset || DEFAULT_SAVE_PRESET;
    const validPresets = new Set(SAVE_PRESET_OPTIONS.map((option) => option.value));
    if (!validPresets.has(defaultPreset)) return;
    if (presetWidget.value && validPresets.has(presetWidget.value) && presetWidget.value !== DEFAULT_SAVE_PRESET) return;
    presetWidget.value = defaultPreset;
    presetWidget.callback?.(defaultPreset);
}

const CUSTOM_SAVE_PRESET = "Custom";
const SAVE_VIDEO_CUSTOM_WIDGET_NAMES = [
    "custom_output_kind",
    "custom_container",
    "custom_video_codec",
    "custom_pix_fmt",
    "custom_crf",
    "custom_encoder_preset",
    "custom_audio_codec",
    "custom_audio_bitrate_kbps",
    "custom_png_compression",
];
const CUSTOM_COMBO_OPTIONS = {
    custom_container: CUSTOM_CONTAINER_OPTIONS,
    custom_video_codec: CUSTOM_VIDEO_CODEC_OPTIONS,
    custom_pix_fmt: CUSTOM_PIX_FMT_OPTIONS,
    custom_encoder_preset: CUSTOM_ENCODER_PRESET_OPTIONS,
    custom_audio_codec: CUSTOM_AUDIO_CODEC_OPTIONS,
};

function findWidget(node, name) {
    return node?.widgets?.find((widget) => widget.name === name);
}

function widgetValue(node, name, fallback = "") {
    const widget = findWidget(node, name);
    return widget?.value ?? fallback;
}

function coerceWidgetValue(widget, allowed, fallback) {
    if (!widget || !Array.isArray(allowed) || !allowed.length) return false;
    const nextFallback = allowed.includes(fallback) ? fallback : allowed[0];
    if (allowed.includes(widget.value)) return false;
    widget.value = nextFallback;
    return true;
}

function setComboValues(widget, values) {
    if (!widget || !Array.isArray(values) || !values.length) return false;
    widget.options = widget.options || {};
    const previousValues = Array.isArray(widget.options.values) ? widget.options.values : [];
    const changed = previousValues.length !== values.length || previousValues.some((value, index) => value !== values[index]);
    widget.options.values = values.slice();
    return coerceWidgetValue(widget, values, values[0]) || changed;
}

function setWidgetVisible(node, widget, visible) {
    if (!widget) return false;
    const wasHidden = !!widget.hidden;
    if (visible) {
        showWidget(node, widget);
    } else {
        hideWidget(node, widget);
    }
    return wasHidden === !!visible;
}

function savePresetOption(value) {
    return SAVE_PRESET_OPTIONS.find((option) => option.value === value) || SAVE_PRESET_OPTIONS[0];
}

function customPresetDescription(node) {
    const outputKind = widgetValue(node, "custom_output_kind", CUSTOM_OUTPUT_KIND_VIDEO);
    if (outputKind === CUSTOM_OUTPUT_KIND_PNG_SEQUENCE) {
        const compression = widgetValue(node, "custom_png_compression", 0);
        return `PNG sequence, RGB PNG files, compression ${compression}. Video mode only.`;
    }

    const container = String(widgetValue(node, "custom_container", "mp4")).toUpperCase();
    const codec = widgetValue(node, "custom_video_codec", "libx264");
    const pixFmt = widgetValue(node, "custom_pix_fmt", "yuv420p");
    const audioCodec = widgetValue(node, "custom_audio_codec", "aac");
    const audioBitrate = widgetValue(node, "custom_audio_bitrate_kbps", 192);
    const crf = widgetValue(node, "custom_crf", 18);
    const encoderPreset = widgetValue(node, "custom_encoder_preset", "slow");
    const quality = codec === "libx264" || codec === "libx265"
        ? `, CRF ${crf}, ${encoderPreset} preset`
        : "";
    const audio = audioCodec === "aac" ? `AAC ${audioBitrate} kbps` : audioCodec;
    return `${container}, ${codec}, ${pixFmt}${quality}, ${audio} audio.`;
}

function updateSavePresetHelp(node, helpEl) {
    if (!helpEl) return;
    const presetWidget = findWidget(node, "save_preset");
    const preset = savePresetOption(presetWidget?.value || DEFAULT_SAVE_PRESET);
    const description = preset.value === CUSTOM_SAVE_PRESET ? customPresetDescription(node) : preset.description;
    helpEl.textContent = description || "";
    helpEl.title = description || "";
}

function resizeSaveVideoNode(node) {
    try {
        const nextSize = node.computeSize?.();
        if (Array.isArray(nextSize) && nextSize.length >= 2) {
            const currentWidth = Number(node.size?.[0] || 0);
            node.setSize?.([Math.max(currentWidth, Number(nextSize[0] || 0)), Number(nextSize[1] || 0)]);
        }
    } catch (error) {
        console.warn("[Sonder] Failed to resize save-video node:", error);
    }
    app.graph.setDirtyCanvas?.(true, true);
}

function installSaveVideoPresetUi(node) {
    if (!node?.widgets) return;
    applySaveVideoDefaults(node);

    if (node._sonderSavePresetUiInstalled) {
        node._sonderSyncSavePresetUi?.();
        return;
    }
    node._sonderSavePresetUiInstalled = true;

    const helpEl = document.createElement("div");
    helpEl.style.cssText = `
        box-sizing: border-box;
        width: 100%;
        min-height: 30px;
        padding: 5px 8px;
        color: ${THEME.fg1};
        font-family: ${FONT.sans};
        font-size: 10px;
        line-height: 1.35;
        white-space: normal;
    `;
    const helpWidget = node.addDOMWidget("sonder_save_preset_help", "SonderSavePresetHelp", helpEl, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => 30,
        getMaxHeight: () => 54,
        getHeight: () => 40,
    });
    helpWidget.computeSize = (width) => [width, 40];

    const presetIndex = node.widgets.findIndex((widget) => widget.name === "save_preset");
    const helpIndex = node.widgets.indexOf(helpWidget);
    if (presetIndex >= 0 && helpIndex >= 0 && helpIndex !== presetIndex + 1) {
        node.widgets.splice(helpIndex, 1);
        node.widgets.splice(presetIndex + 1, 0, helpWidget);
    }

    const sync = () => {
        let changed = false;
        const presetWidget = findWidget(node, "save_preset");
        const modeWidget = findWidget(node, "mode");
        const outputKindWidget = findWidget(node, "custom_output_kind");
        const validPresets = SAVE_PRESET_OPTIONS.map((option) => option.value);

        changed = coerceWidgetValue(presetWidget, validPresets, DEFAULT_SAVE_PRESET) || changed;
        for (const [name, values] of Object.entries(CUSTOM_COMBO_OPTIONS)) {
            changed = setComboValues(findWidget(node, name), values) || changed;
        }

        const isCustom = presetWidget?.value === CUSTOM_SAVE_PRESET;
        const isTake = modeWidget?.value === "Take";
        const outputKindValues = isTake ? [CUSTOM_OUTPUT_KIND_VIDEO] : CUSTOM_OUTPUT_KIND_OPTIONS;
        changed = setComboValues(outputKindWidget, outputKindValues) || changed;

        const outputKind = outputKindWidget?.value || CUSTOM_OUTPUT_KIND_VIDEO;
        const codec = widgetValue(node, "custom_video_codec", "libx264");
        const audioCodec = widgetValue(node, "custom_audio_codec", "aac");
        const isPngSequence = outputKind === CUSTOM_OUTPUT_KIND_PNG_SEQUENCE;
        const isCrfCodec = codec === "libx264" || codec === "libx265";

        const visibleNames = new Set();
        if (isCustom) {
            visibleNames.add("custom_output_kind");
            if (isPngSequence) {
                visibleNames.add("custom_png_compression");
            } else {
                visibleNames.add("custom_container");
                visibleNames.add("custom_video_codec");
                visibleNames.add("custom_pix_fmt");
                visibleNames.add("custom_audio_codec");
                if (isCrfCodec) {
                    visibleNames.add("custom_crf");
                    visibleNames.add("custom_encoder_preset");
                }
                if (audioCodec === "aac") {
                    visibleNames.add("custom_audio_bitrate_kbps");
                }
            }
        }

        for (const name of SAVE_VIDEO_CUSTOM_WIDGET_NAMES) {
            changed = setWidgetVisible(node, findWidget(node, name), visibleNames.has(name)) || changed;
        }
        updateSavePresetHelp(node, helpEl);
        if (changed) resizeSaveVideoNode(node);
    };

    node._sonderSyncSavePresetUi = sync;
    for (const name of ["save_preset", "mode", ...SAVE_VIDEO_CUSTOM_WIDGET_NAMES]) {
        const widget = findWidget(node, name);
        if (!widget || widget._sonderSavePresetCallbackWrapped) continue;
        const originalCallback = widget.callback;
        widget.callback = function (...args) {
            const result = originalCallback?.apply(this, args);
            sync();
            return result;
        };
        widget._sonderSavePresetCallbackWrapped = true;
    }

    const originalOnConfigure = node.onConfigure;
    node.onConfigure = function (...args) {
        const result = originalOnConfigure?.apply(this, args);
        window.setTimeout?.(() => this._sonderSyncSavePresetUi?.(), 0);
        return result;
    };

    sync();
}

function getActiveEditorNodes() {
    return (app.graph._nodes || app.graph.nodes || []).filter(
        (node) => node.type === "SonderEditor" && node._sonderController?.state?.projectDir
    );
}

function isSonderKeyboardDebugEnabled() {
    try {
        return window.__SONDER_KEYBOARD_DEBUG__ === true
            || window.localStorage?.getItem("sonder_keyboard_debug") === "1";
    } catch {
        return window.__SONDER_KEYBOARD_DEBUG__ === true;
    }
}

function sonderKeyboardDebug(message, details) {
    if (!isSonderKeyboardDebugEnabled()) return;
    if (details === undefined) {
        console.log("[Sonder][UndoGuard]", message);
        return;
    }
    console.log("[Sonder][UndoGuard]", message, details);
}

function shouldSuppressComfyGraphUndo() {
    return getActiveEditorNodes().some((node) => node._sonderController?.state?.isFullscreenOpen);
}

const sonderGraphUndoSuppression = {
    untilMs: 0,
    reason: "",
    nodeIds: [],
};
let sonderGraphLoadBypassDepth = 0;

function nowMs() {
    if (typeof performance !== "undefined" && typeof performance.now === "function") {
        return performance.now();
    }
    return Date.now();
}

function isGraphUndoSuppressed() {
    return sonderGraphUndoSuppression.untilMs > nowMs();
}

function activateGraphUndoSuppression(reason = "unknown", nodeIds = []) {
    sonderGraphUndoSuppression.untilMs = nowMs() + 1000;
    sonderGraphUndoSuppression.reason = String(reason || "unknown");
    sonderGraphUndoSuppression.nodeIds = Array.isArray(nodeIds) ? nodeIds.slice() : [];
    sonderKeyboardDebug("activated graph undo suppression", {
        reason: sonderGraphUndoSuppression.reason,
        nodeIds: sonderGraphUndoSuppression.nodeIds,
        untilMs: sonderGraphUndoSuppression.untilMs,
    });
}

async function withGraphLoadBypass(callback) {
    sonderGraphLoadBypassDepth += 1;
    try {
        return await callback();
    } finally {
        sonderGraphLoadBypassDepth = Math.max(0, sonderGraphLoadBypassDepth - 1);
    }
}

function installGraphLoadGuard() {
    if (!app || app._sonderGraphLoadGuardInstalled) return;

    const originalLoadGraphData = typeof app.loadGraphData === "function" ? app.loadGraphData.bind(app) : null;
    if (!originalLoadGraphData) return;

    app.loadGraphData = async function (...args) {
        if (sonderGraphLoadBypassDepth <= 0 && isGraphUndoSuppressed() && shouldSuppressComfyGraphUndo()) {
            sonderKeyboardDebug("blocked app.loadGraphData during fullscreen editor undo window", {
                reason: sonderGraphUndoSuppression.reason,
                nodeIds: sonderGraphUndoSuppression.nodeIds,
            });
            return false;
        }
        return await originalLoadGraphData(...args);
    };

    app._sonderGraphLoadGuardInstalled = true;
    if (typeof window !== "undefined") {
        window.__SONDER_SUPPRESS_COMFY_GRAPH_UNDO__ = activateGraphUndoSuppression;
        window.__SONDER_GRAPH_UNDO_GUARD__ = {
            isSuppressed: () => isGraphUndoSuppressed(),
            getState: () => ({
                untilMs: sonderGraphUndoSuppression.untilMs,
                reason: sonderGraphUndoSuppression.reason,
                nodeIds: sonderGraphUndoSuppression.nodeIds.slice(),
            }),
            withBypass: async (callback) => await withGraphLoadBypass(callback),
        };
    }
    sonderKeyboardDebug("installed graph load guard");
}

function installComfyGraphUndoGuard() {
    const changeTracker = app?.changeTracker;
    if (!changeTracker || changeTracker._sonderUndoGuardInstalled) return;

    const wrap = (methodName) => {
        const original = changeTracker[methodName];
        if (typeof original !== "function") return;
        changeTracker[methodName] = function (...args) {
            if (shouldSuppressComfyGraphUndo()) {
                sonderKeyboardDebug(`blocked changeTracker.${methodName}`, {
                    fullscreenEditorNodeIds: getActiveEditorNodes()
                        .filter((node) => node._sonderController?.state?.isFullscreenOpen)
                        .map((node) => node.id),
                });
                return;
            }
            return original.apply(this, args);
        };
    };

    wrap("undoRedo");
    wrap("undo");
    wrap("redo");
    changeTracker._sonderUndoGuardInstalled = true;
    sonderKeyboardDebug("installed graph undo guard", {
        wrapped: ["undoRedo", "undo", "redo"].filter((name) => typeof changeTracker[name] === "function"),
    });
}

function getGraphLinks() {
    return app.graph?.links || app.graph?._links || {};
}

function getNodeById(nodeId) {
    if (nodeId == null) return null;
    if (typeof app.graph?.getNodeById === "function") {
        return app.graph.getNodeById(nodeId) || null;
    }
    return (app.graph?._nodes || app.graph?.nodes || []).find((node) => node.id === nodeId) || null;
}

function getLinkedNodeFromInput(node, inputName) {
    const input = node?.inputs?.find?.((entry) => entry?.name === inputName);
    const linkId = input?.link;
    if (linkId == null) return null;
    const link = getGraphLinks()?.[linkId];
    if (link?.origin_id == null) return null;
    return getNodeById(link.origin_id);
}

function collectUpstreamEditorNodes(startNode, collected = new Set(), visited = new Set()) {
    if (!startNode || visited.has(startNode.id)) return collected;
    visited.add(startNode.id);
    if (startNode.type === "SonderEditor") {
        collected.add(startNode);
        return collected;
    }
    for (const input of startNode.inputs || []) {
        if (input?.link == null) continue;
        const link = getGraphLinks()?.[input.link];
        if (link?.origin_id == null) continue;
        const upstreamNode = getNodeById(link.origin_id);
        if (upstreamNode) {
            collectUpstreamEditorNodes(upstreamNode, collected, visited);
        }
    }
    return collected;
}

function editorNodeHasQueuedWork(node) {
    const counts = node?._sonderController?.state?.dormantSummary?.queue_counts || {};
    return (counts.pending || 0) > 0 || (counts.running || 0) > 0;
}

function refreshEditorNodes(editorNodes) {
    for (const editorNode of editorNodes || []) {
        editorNode._sonderController.handleSaveVideoExecuted();
    }
}

function getSaveVideoEditorNodes(saveNode) {
    const projectSourceNode = getLinkedNodeFromInput(saveNode, "project");
    if (!projectSourceNode) return [];
    return Array.from(collectUpstreamEditorNodes(projectSourceNode)).filter(
        (node) => node._sonderController?.state?.projectDir
    );
}

function getSaveBridgeEditorNodes(bridgeNode) {
    const projectSourceNode = getLinkedNodeFromInput(bridgeNode, "project");
    if (!projectSourceNode) return [];
    return Array.from(collectUpstreamEditorNodes(projectSourceNode));
}

function getEditorProjectId(editorNode) {
    const projectDir = editorNode?._sonderController?.state?.projectDir || "";
    if (projectDir) return projectDir.split(/[/\\]/).pop();
    const projectWidget = editorNode?.widgets?.find((widget) => widget.name === "project");
    return projectIdFromProjectValue(projectWidget?.value);
}

function getSaveBridgeProjectId(bridgeNode) {
    for (const editorNode of getSaveBridgeEditorNodes(bridgeNode)) {
        const projectId = getEditorProjectId(editorNode);
        if (projectId) return projectId;
    }
    return "";
}

function buildBridgeFolderOptions(folders, currentValue = "") {
    const values = [""];
    const current = normalizeFolderValue(currentValue);
    if (current) values.push(current);
    for (const folder of uniqueFolderValues(folders).filter(Boolean)) {
        if (!values.includes(folder)) {
            values.push(folder);
        }
    }
    return values;
}

function setBridgeFolderWidgetChoices(folderWidget, folders, currentValue = "") {
    if (!folderWidget) return [];
    const values = buildBridgeFolderOptions(folders, currentValue);
    folderWidget.options = folderWidget.options || {};
    folderWidget.options.values = values;
    folderWidget.options.editable = true;
    return values;
}

function renderBridgeFolderSuggestions(node, values) {
    const datalist = node?._sonderBridgeFolderDatalist;
    if (!datalist) return;
    datalist.innerHTML = "";
    for (const folder of values.filter(Boolean)) {
        const option = document.createElement("option");
        option.value = folder;
        datalist.appendChild(option);
    }
}

async function syncBridgeTargetFolderWidget(node) {
    const folderWidget = node?.widgets?.find((widget) => widget.name === "target_folder");
    const input = node?._sonderBridgeFolderInput;
    if (!folderWidget || !input) return [];

    const syncToken = (Number(node._sonderBridgeFolderSyncToken) || 0) + 1;
    node._sonderBridgeFolderSyncToken = syncToken;

    const projectId = getSaveBridgeProjectId(node);
    let folders = [];
    if (projectId) {
        try {
            folders = await listProjectAssetFolders(projectId);
        } catch (error) {
            console.warn("[Sonder] Failed to load bridge folder suggestions:", error);
        }
    }
    if (syncToken !== node._sonderBridgeFolderSyncToken) return [];

    const currentValue = normalizeFolderValue(input.value || folderWidget.value || "");
    const values = setBridgeFolderWidgetChoices(folderWidget, folders, currentValue);
    renderBridgeFolderSuggestions(node, values);
    input.placeholder = projectId
        ? "Root (blank) or folder label"
        : "Connect a Sonder project to load folder suggestions";
    input.value = currentValue;
    return values;
}

function installBridgeFolderPicker(node) {
    if (!node?.widgets || node._sonderBridgeFolderInput || typeof node.addDOMWidget !== "function") return;
    const folderWidget = node.widgets.find((widget) => widget.name === "target_folder");
    if (!folderWidget) return;

    hideWidget(node, folderWidget);
    setBridgeFolderWidgetChoices(folderWidget, [], folderWidget.value || "");

    const wrapper = style(document.createElement("div"), `
        display:flex;
        flex-direction:column;
        gap:4px;
        width:100%;
        box-sizing:border-box;
        padding-top:2px;
    `);

    const label = style(document.createElement("div"), `
        color:${THEME.fg0};
        font-family:${FONT.sans};
        font-size:10px;
        font-weight:600;
        line-height:1.2;
    `);
    label.textContent = "Target Folder";

    const input = style(document.createElement("input"), `
        width:100%;
        box-sizing:border-box;
        ${chromeInputCss({ padding: "4px 6px", fontSize: "11px" })}
        background:${THEME.bg2};
        outline:none;
    `);
    const datalist = document.createElement("datalist");
    datalist.id = `sonder-bridge-folders-${Math.random().toString(36).slice(2)}`;
    input.setAttribute("list", datalist.id);

    const help = style(document.createElement("div"), `
        color:${THEME.fg2};
        font-family:${FONT.sans};
        font-size:10px;
        line-height:1.25;
    `);
    help.textContent = "Blank = Root. Existing folders appear here; typing a new label creates it on registration.";

    wrapper.append(label, input, datalist, help);

    const folderDomWidget = node.addDOMWidget("sonder_bridge_folder_picker", "SonderBridgeFolderPicker", wrapper, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => 56,
        getMaxHeight: () => 56,
        getHeight: () => 56,
    });
    folderDomWidget.computeSize = (width) => [width, 56];

    const applyValue = (value, { fireCallback = false } = {}) => {
        const normalized = normalizeFolderValue(value);
        folderWidget.value = normalized;
        input.value = normalized;
        if (fireCallback) {
            folderWidget.callback?.call(folderWidget, normalized);
        }
        app.graph.setDirtyCanvas?.(true, true);
    };

    input.value = normalizeFolderValue(folderWidget.value || "");
    input.addEventListener("input", () => {
        folderWidget.value = normalizeFolderValue(input.value);
        app.graph.setDirtyCanvas?.(true, true);
    });
    input.addEventListener("change", () => applyValue(input.value, { fireCallback: true }));
    input.addEventListener("blur", () => applyValue(input.value, { fireCallback: true }));
    input.addEventListener("focus", () => {
        void syncBridgeTargetFolderWidget(node);
    });

    node._sonderBridgeFolderInput = input;
    node._sonderBridgeFolderDatalist = datalist;

    const origOnConnectionsChange = node.onConnectionsChange;
    node.onConnectionsChange = function () {
        const result = origOnConnectionsChange?.apply(this, arguments);
        void syncBridgeTargetFolderWidget(this);
        return result;
    };

    void syncBridgeTargetFolderWidget(node);
}

const pendingBridgeEditorNodeIds = new Set();
let queuedExecutionRefreshToken = 0;

function trackBridgeExecution(bridgeNode) {
    for (const editorNode of getSaveBridgeEditorNodes(bridgeNode)) {
        if (!editorNode?._sonderController?.state?.projectDir) continue;
        pendingBridgeEditorNodeIds.add(editorNode.id);
    }
}

function getTrackedBridgeEditorNodes() {
    const tracked = [];
    for (const nodeId of [...pendingBridgeEditorNodeIds]) {
        const node = getNodeById(nodeId);
        if (!node?._sonderController?.state?.projectDir) {
            pendingBridgeEditorNodeIds.delete(nodeId);
            continue;
        }
        tracked.push(node);
    }
    return tracked;
}

function schedulePostPromptRefresh() {
    const token = ++queuedExecutionRefreshToken;
    const delays = [0, 150, 400, 1000, 2500, 5000];
    delays.forEach((delay, index) => {
        window.setTimeout(async () => {
            if (token !== queuedExecutionRefreshToken) return;
            const bridgeNodes = getTrackedBridgeEditorNodes();
            const bridgeNodeIds = new Set(bridgeNodes.map((node) => node.id));
            const queueNodes = getActiveEditorNodes().filter(editorNodeHasQueuedWork);
            const editorNodes = Array.from(new Map(
                [...bridgeNodes, ...queueNodes].map((node) => [node.id, node])
            ).values());
            if (!editorNodes.length) {
                if (index === delays.length - 1) {
                    pendingBridgeEditorNodeIds.clear();
                }
                if (token === queuedExecutionRefreshToken && !pendingBridgeEditorNodeIds.size) {
                    queuedExecutionRefreshToken += 1;
                }
                return;
            }
            const counts = await Promise.all(editorNodes.map(async (editorNode) => {
                try {
                    if (bridgeNodeIds.has(editorNode.id)) {
                        return await editorNode._sonderController?.handleBridgeExecutionSettled?.({
                            allowRollback: index === delays.length - 1,
                            attemptIndex: index,
                            delay,
                        });
                    }
                    return await editorNode._sonderController?.handleQueueExecutionSettled?.({
                        allowRollback: index === delays.length - 1,
                        attemptIndex: index,
                        delay,
                    });
                } catch (error) {
                    console.warn("[Sonder] Queue reconciliation failed:", error);
                    return null;
                }
            }));
            if (token !== queuedExecutionRefreshToken) return;
            const hasRunning = counts.some((value) => (value?.running || 0) > 0);
            if (index === delays.length - 1) {
                pendingBridgeEditorNodeIds.clear();
            }
            if (!hasRunning && !pendingBridgeEditorNodeIds.size) {
                queuedExecutionRefreshToken += 1;
            }
        }, delay);
    });
}

// ── Main Extension ─────────────────────────────────────────────────────
app.registerExtension({
    name: "sonder.editor",

    async beforeRegisterNodeDef(nodeType, nodeData, app) {

        // ── Sonder Editor Node ────────────────────────────────────────────
        if (nodeData.name === "SonderEditor") {
            const origOnNodeCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                origOnNodeCreated?.apply(this, arguments);

                const node = this;
                const projectWidget = node.widgets.find((widget) => widget.name === "project");
                const creationWidgetNames = ["project_name", "fps", "width", "height"];
                const hiddenWidgetNames = [
                    "scene_id",
                    "selection_start",
                    "selection_end",
                    "pre_context_frames",
                    "post_context_frames",
                    "mask_pre_offset",
                    "mask_post_offset",
                    "take_placement_mode",
                    "take_placement_linked",
                    "take_placement_muted",
                    "render_queue_active",
                ];

                // Store original types
                for (const widget of node.widgets) {
                    widget._origType = widget.type;
                }

                const controller = new EditorNodeController(node, projectWidget);
                node._sonderController = controller;
                node.resizable = true;
                node.flags = { ...(node.flags || {}), resizable: true };
                controller.render();

                // Mark workflow-loaded nodes so updateVisibility can preserve their
                // saved node.size instead of stomping it with preferred defaults.
                // onConfigure only fires on workflow restore, so its presence is the
                // load signal. Fires after node.configure() has already set node.size.
                const origNodeOnConfigure = node.onConfigure;
                node.onConfigure = function (info) {
                    if (Array.isArray(info?.size) && info.size.length >= 2) {
                        node._sonderLoadedSize = [
                            Number(info.size[0]) || 0,
                            Number(info.size[1]) || 0,
                        ];
                    }
                    return origNodeOnConfigure?.apply(this, arguments);
                };

                const editorDOMWidget = node.addDOMWidget("sonder_editor_ui", "SonderEditorWidget", controller.getElement(), {
                    serialize: false,
                    hideOnZoom: false,
                    getMinHeight: () => 150,
                    getMaxHeight: () => controller.getHeight(),
                    getHeight: () => controller.getHeight(),
                });
                editorDOMWidget.computeSize = (width) => [width, controller.getHeight()];

                // Override node.computeSize to allow shrinking during interactive resize.
                // Widget computeSize returns _height (correct for layout), but node.computeSize
                // replaces the widget's contribution with a fixed 150px floor so LiteGraph
                // doesn't clamp the node at _height + overhead.
                const origNodeComputeSize = node.computeSize.bind(node);
                node.computeSize = function () {
                    const result = origNodeComputeSize();
                    const widgetHeight = controller.getHeight();
                    const overhead = result[1] - widgetHeight;
                    result[1] = 150 + overhead;
                    return result;
                };

                const createButtonWidget = node.addWidget("button", "Create", null, async () => {
                    try {
                        await createProjectFromNode(node, projectWidget);
                    } catch (e) {
                        console.warn("[Sonder] Failed to create project:", e);
                    }
                });
                createButtonWidget.serialize = false;

                const updateVisibility = async () => {
                    const isCreateNew = projectWidget?.value === "+ Create New";
                    for (const widget of node.widgets) {
                        if (creationWidgetNames.includes(widget.name)) {
                            if (isCreateNew) {
                                showWidget(node, widget);
                            } else {
                                hideWidget(node, widget);
                            }
                        }
                        if (hiddenWidgetNames.includes(widget.name)) {
                            hideWidget(node, widget);
                        }
                    }
                    if (isCreateNew) {
                        applyProjectCreationDefaults(node);
                        showWidget(node, createButtonWidget);
                    } else {
                        hideWidget(node, createButtonWidget);
                    }
                    app.graph.setDirtyCanvas?.(true, true);

                    if (isCreateNew) {
                        hideWidget(node, editorDOMWidget);
                        controller.getElement().style.display = "none";
                        await controller.updateProject("", projectWidget?.value || "");
                    } else {
                        showWidget(node, editorDOMWidget);
                        controller.getElement().style.display = "";
                        const dir = await getProjectDir(projectWidget?.value);
                        await controller.updateProject(dir, projectWidget?.value || "");
                    }

                    app.graph.setDirtyCanvas?.(true, true);

                    const nextSize = node.computeSize();
                    const preferredWidth = isCreateNew ? 340 : 440;
                    const modeKey = isCreateNew ? "create" : "existing";
                    const minComputedHeight = nextSize?.[1] || 0;
                    if (!node._sonderInitializedSize) {
                        node._sonderInitializedSize = true;
                        node._sonderPreferredWidthMode = modeKey;
                        if (node._sonderLoadedSize) {
                            // Workflow load: preserve saved node.size; only grow if below
                            // safety floors (240 width, computed min height).
                            controller.adoptLoadedNodeHeight();
                            const loadedW = node.size?.[0] || 0;
                            const loadedH = node.size?.[1] || 0;
                            const safeW = Math.max(loadedW, 240);
                            const safeH = Math.max(loadedH, minComputedHeight);
                            if (safeW !== loadedW || safeH !== loadedH) {
                                controller.setNodeSizeProgrammatic(safeW, safeH);
                                controller.adoptLoadedNodeHeight();
                            }
                        } else {
                            // Menu-created node: apply preferred default size.
                            controller.setNodeSizeProgrammatic(
                                preferredWidth,
                                Math.max(minComputedHeight, node.size?.[1] || 0),
                            );
                        }
                    } else if (node._sonderPreferredWidthMode !== modeKey && (node.size?.[0] || 0) < preferredWidth) {
                        // Mode switch (create ↔ existing) grows a too-narrow node to the
                        // preferred width for the new mode. Height is preserved.
                        node._sonderPreferredWidthMode = modeKey;
                        controller.setNodeSizeProgrammatic(
                            preferredWidth,
                            node.size?.[1] || minComputedHeight || controller.getHeight(),
                        );
                    }

                    if (!isCreateNew) {
                        controller.queueResize();
                    }
                };

                const runUpdateVisibility = () => {
                    Promise.resolve(updateVisibility()).catch((e) => {
                        console.warn("[Sonder] Failed to update node visibility:", e);
                    });
                };

                // Hook dropdown changes
                if (projectWidget) {
                    const origCallback = projectWidget.callback;
                    projectWidget.callback = (value) => {
                        origCallback?.call(projectWidget, value);
                        runUpdateVisibility();
                    };
                    syncProjectWidgetChoices(projectWidget)
                        .then(() => runUpdateVisibility())
                        .catch((e) => {
                            console.warn("[Sonder] Failed to sync project choices:", e);
                        });
                }

                // Initial setup
                runUpdateVisibility();

                // Re-render timeline when node resizes
                const origOnResize = node.onResize;
                node.onResize = function () {
                    origOnResize?.apply(this, arguments);
                    node._sonderController?.handleNodeResize?.();
                };

                const origOnRemoved = node.onRemoved;
                node.onRemoved = function () {
                    node._sonderController?.destroy();
                    origOnRemoved?.apply(this, arguments);
                };

                // Drag-and-drop files onto the node → import to project
                // Must stopPropagation to prevent ComfyUI from intercepting
                node.onDragOver = function (e) {
                    if (e.dataTransfer && e.dataTransfer.items) {
                        const hasFiles = [...e.dataTransfer.items].some(
                            (f) => f.kind === "file"
                        );
                        if (hasFiles) {
                            e.preventDefault?.();
                            e.stopPropagation?.();
                            return true;
                        }
                    }
                    return false;
                };

                node.onDragDrop = async (e) => {
                    if (!node._sonderController?.state?.projectDir) return false;
                    if (!e.dataTransfer?.files?.length) return false;

                    e.preventDefault?.();
                    e.stopPropagation?.();

                    await node._sonderController.importFiles(e.dataTransfer.files);
                    return true;
                };
            };

            // Handle executed results — refresh editor after execution
            const origOnExecuted = nodeType.prototype.onExecuted;
            nodeType.prototype.onExecuted = function () {
                origOnExecuted?.apply(this, arguments);
                this._sonderController?.handleNodeExecuted();
            };
        }

        // ── Sonder Save Video — notify editor nodes to refresh ───────────
        if (nodeData.name === "SonderSaveVideo") {
            const origOnNodeCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                origOnNodeCreated?.apply(this, arguments);
                try {
                    installSaveVideoPresetUi(this);
                } catch (error) {
                    console.warn("[Sonder] Failed to install save-video preset UI:", error);
                }
            };

            const origOnExecuted = nodeType.prototype.onExecuted;
            nodeType.prototype.onExecuted = function () {
                origOnExecuted?.apply(this, arguments);
                const editorNodes = getSaveVideoEditorNodes(this);
                refreshEditorNodes(editorNodes);
            };
        }

        if (nodeData.name === "SonderSaveBridge") {
            const origOnNodeCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                origOnNodeCreated?.apply(this, arguments);
                try {
                    installBridgeFolderPicker(this);
                } catch (error) {
                    console.warn("[Sonder] Failed to install bridge folder picker:", error);
                }
            };

            const origOnExecuted = nodeType.prototype.onExecuted;
            nodeType.prototype.onExecuted = function () {
                origOnExecuted?.apply(this, arguments);
                trackBridgeExecution(this);
            };
        }

    },

    setup() {
        installGraphLoadGuard();
        installComfyGraphUndoGuard();
        // Page-level notification toast stack (canvas page). Mounted once for the
        // page lifetime — visible whether the editor is dormant or fullscreen,
        // since fullscreen is an overlay on this same document.body.
        if (!document.querySelector("[data-sonder-toast-stack]")) {
            mountToastStack(document.body);
        }
        // Push browser-local toast durations into the Core, and keep them synced.
        configureNotifications(notificationCoreConfig());
        subscribeEditorSettings(() => configureNotifications(notificationCoreConfig()));
        if (typeof api.addEventListener === "function") {
            api.addEventListener("status", (event) => {
                const remaining = Number(event?.detail?.exec_info?.queue_remaining);
                if (!Number.isFinite(remaining) || remaining !== 0) return;
                if (!getActiveEditorNodes().some(editorNodeHasQueuedWork) && !pendingBridgeEditorNodeIds.size) return;
                schedulePostPromptRefresh();
            });
            // Map SonderSaveVideo native progress → notification foreground pill.
            api.addEventListener("progress", (event) => {
                try { _sonderHandleProgressEvent(event?.detail); } catch (_) {}
            });
            api.addEventListener("executing", (event) => {
                const d = event?.detail;
                const nodeId = (d && typeof d === "object") ? (d.node ?? null) : (d ?? null);
                _sonderResolveEncodeExcept(nodeId);
            });
            api.addEventListener("execution_error", () => _sonderClearEncodeNotifs({ error: true }));
            api.addEventListener("execution_interrupted", () => _sonderClearEncodeNotifs());
        }
        // ── Global drop interceptor: asset gallery → ComfyUI graph ───────
        // HTML5 drag can't carry File objects, so we intercept drops with our
        // custom MIME type, fetch the actual asset, upload it to ComfyUI's
        // input dir, and create the appropriate loader node.
        document.addEventListener("drop", async (e) => {
            const assetData = e.dataTransfer.getData("application/x-sonder-asset");
            if (!assetData) return; // Not our drag

            // Don't intercept drops on our own editor elements
            if (e.target.closest?.("[data-sonder-editor]")) return;

            e.preventDefault();
            e.stopPropagation();

            try {
                const asset = JSON.parse(assetData);
                const dirName = asset._projectDir;
                if (!dirName) return;
                if (asset.asset_type === "artifact") return;

                const fn = asset.path.split(/[/\\]/).pop();
                const sf = `sonder-projects/${dirName}/${asset.path.split(/[/\\]/).slice(0, -1).join("/")}`;
                const viewUrl = api.apiURL(
                    `/view?filename=${encodeURIComponent(fn)}&subfolder=${encodeURIComponent(sf)}&type=output`
                );

                // Fetch the actual asset file
                const resp = await fetch(viewUrl);
                if (!resp.ok) throw new Error(`Fetch failed: ${resp.status}`);
                const blob = await resp.blob();
                const file = new File([blob], fn, { type: blob.type });

                // Upload to ComfyUI input directory
                const formData = new FormData();
                formData.append("image", file);
                formData.append("subfolder", "sonder_assets");
                const uploadResp = await api.fetchApi("/upload/image", {
                    method: "POST",
                    body: formData,
                });
                if (!uploadResp.ok) throw new Error(`Upload failed: ${uploadResp.status}`);
                const uploadResult = await uploadResp.json();
                const uploadedName = uploadResult.subfolder
                    ? `${uploadResult.subfolder}/${uploadResult.name}`
                    : uploadResult.name;

                // Pick node type based on asset type
                const regTypes = LiteGraph.registered_node_types || {};
                let nodeType, widgetName;
                if (asset.asset_type === "image") {
                    nodeType = "LoadImage";
                    widgetName = "image";
                } else if (asset.asset_type === "video") {
                    nodeType = regTypes["VHS_LoadVideo"] ? "VHS_LoadVideo" : "LoadImage";
                    widgetName = nodeType === "VHS_LoadVideo" ? "video" : "image";
                } else if (asset.asset_type === "audio") {
                    nodeType = regTypes["LoadAudio"] ? "LoadAudio" : null;
                    widgetName = "audio";
                }

                if (!nodeType) {
                    console.warn("[Sonder] No suitable node type for:", asset.asset_type);
                    return;
                }

                // Create node at drop position on the graph
                const graphCanvas = app.canvas;
                const pos = graphCanvas.convertEventToCanvasOffset(e);
                const node = LiteGraph.createNode(nodeType);
                if (!node) {
                    console.warn("[Sonder] Could not create node:", nodeType);
                    return;
                }
                node.pos = [pos[0], pos[1]];
                app.graph.add(node);

                // Set the file widget value
                const widget = node.widgets?.find(w => w.name === widgetName);
                if (widget) {
                    widget.value = uploadedName;
                    widget.callback?.(uploadedName);
                }
            } catch (err) {
                console.warn("[Sonder] ComfyUI graph drop failed:", err);
            }
        }, false); // bubble phase — timeline canvas stopPropagation prevents this from firing for timeline drops
    },
});
