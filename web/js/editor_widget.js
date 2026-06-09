/**
 * Sonder Editor Widget — Timeline + Asset Gallery embedded in a ComfyUI node.
 * Uses addDOMWidget pattern (same as VHS/KJNodes).
 */

const { api } = window.comfyAPI.api;


// Session-diagnostic mode (gated by `window.SONDER_DEBUG_SESSION === true`).
// Initialized lazily so the cost is zero when off. The window-global
// `__SONDER_CANVAS_DIAG` surface is read by the canvas controller when the
// user triggers the Ctrl+Alt+Shift+D dump hotkey.
//
// Persistent enable across page reloads: set `localStorage.SONDER_DEBUG_SESSION = "1"`
// once; this bootstrap copies it into the window global on import.
const SESSION_DIAG_RING_MAX = 2048;

if (typeof window !== "undefined" && !window.SONDER_DEBUG_SESSION) {
    try {
        if (window.localStorage?.getItem?.("SONDER_DEBUG_SESSION") === "1") {
            window.SONDER_DEBUG_SESSION = true;
        }
    } catch (_) {}
}

let _sessionDiagInitialized = false;
let _sessionDiagLoadMarkerSeq = 0;
let _sessionDiagInFlightMarkerId = "";
let _sessionDiagInFlightKind = "";
let _sessionDiagRafGapHandle = 0;
let _sessionDiagLastRafTs = 0;
let _sessionDiagLongTaskObserver = null;

function isSessionDiagEnabled() {
    return typeof window !== "undefined" && window.SONDER_DEBUG_SESSION === true;
}

function _sessionDiagInit() {
    if (_sessionDiagInitialized) return window.__SONDER_CANVAS_DIAG;
    _sessionDiagInitialized = true;
    const boot = {
        kind: "boot",
        t_wall: Date.now(),
        t_mono: performance.now(),
        build_marker: "canvas_page",
        href: typeof location !== "undefined" ? String(location.href || "") : "",
    };
    const events = [boot];
    const surface = {
        boot,
        events,
        record(kind, payload = {}) {
            if (events.length >= SESSION_DIAG_RING_MAX) events.shift();
            events.push({
                t_wall: Date.now(),
                t_mono: performance.now(),
                kind,
                ...payload,
            });
        },
    };
    window.__SONDER_CANVAS_DIAG = surface;
    // rAF gap detector — emits when a frame gap exceeds 250 ms
    const rafTick = (now) => {
        if (!isSessionDiagEnabled()) {
            _sessionDiagRafGapHandle = 0;
            _sessionDiagLastRafTs = 0;
            return;
        }
        if (_sessionDiagLastRafTs > 0) {
            const gap = now - _sessionDiagLastRafTs;
            if (gap > 250) {
                surface.record("raf_gap", {
                    gap_ms: gap,
                    in_flight_marker_id: _sessionDiagInFlightMarkerId,
                    in_flight_kind: _sessionDiagInFlightKind,
                });
            }
        }
        _sessionDiagLastRafTs = now;
        _sessionDiagRafGapHandle = requestAnimationFrame(rafTick);
    };
    _sessionDiagRafGapHandle = requestAnimationFrame(rafTick);
    // PerformanceObserver longtask — emits entries over 50 ms
    try {
        if (typeof PerformanceObserver !== "undefined") {
            _sessionDiagLongTaskObserver = new PerformanceObserver((list) => {
                if (!isSessionDiagEnabled()) return;
                for (const entry of list.getEntries()) {
                    if (entry.duration > 50) {
                        surface.record("longtask", {
                            duration_ms: entry.duration,
                            start_time: entry.startTime,
                            entry_type: entry.entryType,
                            in_flight_marker_id: _sessionDiagInFlightMarkerId,
                            in_flight_kind: _sessionDiagInFlightKind,
                        });
                    }
                }
            });
            _sessionDiagLongTaskObserver.observe({ entryTypes: ["longtask"] });
        }
    } catch (_) {
        _sessionDiagLongTaskObserver = null;
    }
    return surface;
}

function sessionDiagRecord(kind, payload) {
    if (!isSessionDiagEnabled()) return;
    const surface = _sessionDiagInit();
    surface.record(kind, payload || {});
}

function sessionDiagBeginLoad(kind, payload) {
    if (!isSessionDiagEnabled()) return "";
    _sessionDiagInit();
    _sessionDiagLoadMarkerSeq += 1;
    const markerId = `${kind}-${_sessionDiagLoadMarkerSeq}`;
    _sessionDiagInFlightMarkerId = markerId;
    _sessionDiagInFlightKind = kind;
    sessionDiagRecord(`${kind}_start`, { marker_id: markerId, ...(payload || {}) });
    return markerId;
}

function sessionDiagEndLoad(kind, markerId, payload) {
    if (!isSessionDiagEnabled() || !markerId) return;
    if (_sessionDiagInFlightMarkerId === markerId) {
        _sessionDiagInFlightMarkerId = "";
        _sessionDiagInFlightKind = "";
    }
    sessionDiagRecord(`${kind}_end`, { marker_id: markerId, ...(payload || {}) });
}

import { INSPECT_OVERLAY_SHORTCUTS, mountSharedAssetGallery } from "./shared_asset_gallery.js";
import { mountSharedRenderQueue, queueBatchIds } from "./shared_render_queue.js";
import { mountEditorSettingsPanel } from "./editor_settings_panel.js";
import { mountTimelineExportPanel } from "./editor_timeline_export_panel.js";
import * as TimelineCanvas from "./editor_timeline_canvas.js";
import { RULER_HEIGHT, TIMELINE_HEIGHT, TRACK_TYPE } from "./editor_timeline_constants.js";
import { createViewportSurface } from "./viewport_surface.js";
import {
    EDITOR_COLORS as COLORS,
    FONT,
    LANE_PALETTE,
    lightenColor,
    scaleColor,
} from "./editor_theme.js";
import {
    buildEditorSceneBar,
    buildEditorToolbar,
    queueChromeBadges,
    updateEditorToolbar,
    updateQueueChromeStatus,
} from "./editor_top_chrome.js";
import {
    _debugListConsumers as debugKeyboardConsumers,
    isKeyboardDebugEnabled,
    register as registerKeyboardConsumer,
    PRIORITY as KEY_PRIORITY,
} from "./keyboard_ownership.js";
import {
    ASPECT_RATIO_PRESETS,
    CUSTOM_OUTPUT_KIND_VIDEO,
    DEFAULT_EDITOR_SETTINGS,
    RESOLUTION_TIERS,
    computeResolutionFromTier,
    frameConstraintsEqual,
    getEditorSettings,
    getAllModelTemplates,
    getTemplateById,
    resolveBatchChunkSizes,
    resolveFrameConstraintForTemplate,
    snapResolution,
    snapToConstraint,
    subscribeEditorSettings,
    updateEditorSettings,
} from "./editor_settings.js";
import { fetchProjectJson, rememberProjectVersionFromPayload } from "./api_client.js";
import { ProjectMutationQueue } from "./project_mutation_queue.js";

function describeKeyboardDebugElement(element) {
    if (!element) return "null";
    const tag = String(element.tagName || element.nodeName || "").toLowerCase();
    if (!tag) return String(element);
    const id = element.id ? `#${element.id}` : "";
    const classes = typeof element.className === "string" && element.className.trim()
        ? `.${element.className.trim().split(/\s+/).slice(0, 3).join(".")}`
        : "";
    return `${tag}${id}${classes}`;
}

function coerceBoolean(value, defaultValue = false) {
    if (typeof value === "boolean") return value;
    if (typeof value === "number") return value !== 0;
    if (typeof value === "string") {
        const normalized = value.trim().toLowerCase();
        if (["1", "true", "yes", "on"].includes(normalized)) return true;
        if (["0", "false", "no", "off"].includes(normalized)) return false;
    }
    return defaultValue;
}

export async function uploadFileToComfyInput(file) {
    if (!file) return "";

    const formData = new FormData();
    formData.append("image", file, file.name);
    formData.append("overwrite", "true");

    const uploadResp = await fetch(api.apiURL("/upload/image"), {
        method: "POST",
        body: formData,
    });

    if (!uploadResp.ok) {
        const message = await uploadResp.text();
        throw new Error(message || `Upload failed: ${uploadResp.status}`);
    }

    const uploadData = await uploadResp.json();
    return uploadData.name || "";
}

export function buildProjectAssetViewURL(projectDir, sourcePath) {
    if (!projectDir || !sourcePath) return null;
    const dirName = projectDir.split(/[/\\]/).pop();
    const fileName = sourcePath.split(/[/\\]/).pop();
    const subPath = sourcePath.split(/[/\\]/).slice(0, -1).join("/");
    const subfolder = `sonder-projects/${dirName}/${subPath}`;
    return api.apiURL(`/view?filename=${encodeURIComponent(fileName)}&subfolder=${encodeURIComponent(subfolder)}&type=output`);
}

export async function importFileIntoProject(projectDir, file, folder = "") {
    if (!projectDir || !file) return false;

    let uploadedName = "";
    try {
        uploadedName = await uploadFileToComfyInput(file);
    } catch (error) {
        console.warn("[Sonder] Upload failed:", error);
        return false;
    }

    const dirName = projectDir.split(/[/\\]/).pop();
    const importResp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/import`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            source_path: uploadedName,
            folder,
        }),
    });

    if (!importResp.ok) {
        console.warn("[Sonder] Import failed:", await importResp.text());
        return false;
    }

    return true;
}

export async function replaceAssetInProject(projectDir, assetId, file) {
    if (!projectDir || !assetId || !file) return null;

    const uploadedName = await uploadFileToComfyInput(file);
    const dirName = projectDir.split(/[/\\]/).pop();
    const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/${assetId}/replace`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_path: uploadedName }),
    });
    if (!resp.ok) {
        let message = `Asset replace failed: ${resp.status}`;
        try {
            const payload = await resp.json();
            if (payload?.error) message = payload.error;
        } catch {
            const text = await resp.text();
            if (text) message = text;
        }
        throw new Error(message);
    }
    return await resp.json();
}

// ── Constants ──────────────────────────────────────────────────────────
const GALLERY_HEIGHT = 160;
const SCENE_BAR_HEIGHT = 36;
const FULLSCREEN_SIDEBAR_DEFAULT_WIDTH = 240;
const FULLSCREEN_SIDEBAR_MIN_WIDTH = 180;
const FULLSCREEN_TIMELINE_MIN_HEIGHT = 160;
const FULLSCREEN_TIMELINE_FALLBACK_MAX_HEIGHT = 600;

const BUTTON_VARIANTS = {
    muted: {
        background: COLORS.sceneBtn,
        hoverBackground: COLORS.sceneBtnHover,
        border: COLORS.borderStrong,
        text: COLORS.textDim,
    },
    subtle: {
        background: COLORS.panelRaised,
        hoverBackground: COLORS.panelRaisedHover,
        border: COLORS.border,
        text: COLORS.textDim,
    },
    primary: {
        background: COLORS.accentSoft,
        hoverBackground: COLORS.accentSoftHover,
        border: COLORS.accentBorder,
        text: "#f7fbff",
    },
    active: {
        background: COLORS.sceneBtnActive,
        hoverBackground: lightenColor(COLORS.sceneBtnActive, 0.08),
        border: lightenColor(COLORS.sceneBtnActive, 0.2),
        text: "#ffffff",
    },
    warning: {
        background: COLORS.warningSoft,
        hoverBackground: lightenColor(COLORS.warningSoft, 0.08),
        border: COLORS.warningBorder,
        text: COLORS.warningText,
    },
    danger: {
        background: COLORS.dangerSoft,
        hoverBackground: lightenColor(COLORS.dangerSoft, 0.08),
        border: COLORS.dangerBorder,
        text: COLORS.dangerText,
    },
};

function buttonPalette(variant = "muted") {
    return BUTTON_VARIANTS[variant] || BUTTON_VARIANTS.muted;
}

function chromeButtonCss({
    variant = "muted",
    padding = "2px 8px",
    fontSize = "12px",
    radius = "6px",
    lineHeight = "1.4",
    fontWeight = "600",
} = {}) {
    const palette = buttonPalette(variant);
    return `
        background: ${palette.background};
        border: 1px solid ${palette.border};
        color: ${palette.text};
        padding: ${padding};
        cursor: pointer;
        border-radius: ${radius};
        font-size: ${fontSize};
        line-height: ${lineHeight};
        font-weight: ${fontWeight};
        transition: background 0.16s ease, border-color 0.16s ease, color 0.16s ease, box-shadow 0.16s ease;
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.04);
    `;
}

function setButtonVariant(btn, variant = "muted", { persist = true } = {}) {
    if (!btn) return;
    const palette = buttonPalette(variant);
    btn.style.background = palette.background;
    btn.style.borderColor = palette.border;
    btn.style.color = palette.text;
    if (persist) {
        btn.dataset.sonderBaseVariant = variant;
    }
}

function chromeInputCss({ width = "auto", fontSize = "11px", padding = "2px 4px", textAlign = "center" } = {}) {
    return `
        width: ${width};
        background: ${COLORS.panelRaised};
        border: 1px solid ${COLORS.borderStrong};
        color: ${COLORS.text};
        padding: ${padding};
        font-size: ${fontSize};
        border-radius: 6px;
        text-align: ${textAlign};
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.03);
    `;
}

function chromeDividerCss(height = 16) {
    return `width: 1px; height: ${height}px; background: ${COLORS.border}; margin: 0 4px;`;
}

function chromeMenuCss(minWidth = 140) {
    return `
        background: ${COLORS.panelRaised};
        border: 1px solid ${COLORS.borderStrong};
        border-radius: 8px;
        box-shadow: 0 12px 28px rgba(0,0,0,0.42);
        min-width: ${minWidth}px;
        padding: 6px 0;
        font-size: 11px;
    `;
}

function chromeOverlayPanelCss({ width = "90%", maxWidth = "520px", maxHeight = "80vh", padding = "20px 28px", fontFamily = FONT.sans } = {}) {
    return `
        background: ${COLORS.panel};
        border: 1px solid ${COLORS.borderStrong};
        border-radius: 12px;
        padding: ${padding};
        width: ${width};
        max-width: ${maxWidth};
        max-height: ${maxHeight};
        overflow-y: auto;
        color: ${COLORS.text};
        font-family: ${fontFamily};
        font-size: 12px;
        box-shadow: 0 24px 60px rgba(0,0,0,0.46);
    `;
}

// ── Editor Widget Class ────────────────────────────────────────────────
export class EditorWidget {
    constructor(node, options = {}) {
        this.node = node;
        this.options = options;
        this.onFullscreenExit = options.onFullscreenExit || null;
        this.onMountInTab = options.onMountInTab || null;
        this.hostMode = options.hostMode || "node";
        this.onWidgetValueChange = options.onWidgetValueChange || null;
        this.widgetHost = options.host || this._createNodeWidgetHost(node);
        this.projectDir = "";
        this.projectId = "";
        this._frameConstraintHealedFor = "";
        this._destroyed = false;
        this._settings = getEditorSettings();
        this._settingsUnsubscribe = null;

        // Scene state
        this.scenes = [];
        this.activeSceneId = "";
        this.activeScene = null;

        // Timeline state
        this.totalFrames = this._settings.projectDefaults.newSceneDuration;
        this.selectionStart = 0;
        this.selectionEnd = 0;
        this.playhead = 0;
        this.scrollX = 0;
        this.scrollY = 0;
        this.pixelsPerFrame = this._settings.layout.timelinePixelsPerFrame;
        this.isDragging = false;
        this.dragType = null; // "selection", "playhead", "selStart", "selEnd"
        this._pendingApplyWidgetState = null;
        this._pendingScenesRefresh = false;
        this._pendingProjectRefreshKeys = null;
        this._pendingProjectRefreshDrain = false;
        this._timelineMutationDepth = 0;
        this._sceneFetchSeq = 0;
        this._projectMutationQueue = new ProjectMutationQueue({
            onIdle: () => this._replayDeferredProjectBackedRefresh(),
        });
        this._projectMutationCloseInProgress = null;

        // Asset state
        this.assets = { video: [], image: [], audio: [], artifact: [] };
        this.selectedAssetType = "video";
        this._collapsedFolders = {};
        this._renderQueue = [];
        this._queueExpanded = !!this._settings.layout.queuePanelExpanded;
        this._queueBatchExpanded = {};
        this._queueHeaderLabel = null;
        this._queueStatusWrap = null;
        this._exportBtn = null;
        this._exportPanelEl = null;
        this._exportPanelKeyOff = null;
        this._exportPanelHandle = null;
        this._exportPollTimer = null;
        this._exportJobId = "";
        this._exportPanelSeq = 0;
        this._exportPanelToken = 0;
        this._exportStartPending = false;
        this._exportCancelRequested = false;
        this.renderQueueActive = coerceBoolean(this._getWidgetValue("render_queue_active", true), true);
        this._savedSelDropdown = null;

        // Prompt section state
        this._selectedPromptIdx = null;

        // Selected timeline items: array of { type: "clip"|"guide"|"audio", id: string|number, data: object }
        this.selectedItems = [];
        this.selectedItem = null; // Primary (most recently clicked) — used for properties editor

        // Razor / trim mode
        this._razorMode = false;
        this._trimItem = null;   // { type, id, data, edge: "left"|"right", origStart, origEnd, origSourceIn, origSourceOut }

        // Drag-to-move state
        this._dragStartFrame = 0;
        this._dragItemOrigStart = 0;
        this._dragItemOrigEnd = 0;

        // Context menu
        this._contextMenuEl = null;
        this._guidePreviewEl = null;

        // Project settings (fetched from server)
        this.fps = 24;
        this.sceneWidth = 768;
        this.sceneHeight = 512;

        // Animatic toggle state
        this._animaticMode = false;
        this._preAnimaticHidden = null;

        // Project template state
        this._templateId = this._settings.projectDefaults.defaultTemplateId || "free";
        this._customAspectRatioValue = "";
        this._templateFormState = { expanded: false, editId: "" };
        this._resolutionEditAxis = "w";
        this._freeAspectTierDraft = { width: false, height: false };

        // Viewport / playback state
        this.isPlaying = false;
        this._playbackRAF = null;
        this._playbackStartTime = 0;
        this._playbackStartFrame = 0;
        this._playbackSessionStartFrame = 0;
        this._playbackLoopRange = null;
        this._videoCache = {};       // source_path → HTMLVideoElement
        this._audioCacheMap = {};    // source_path → HTMLAudioElement
        this._viewportImageCache = {};
        this._viewportSurface = null;
        this._vpCanvas = null;
        this._vpCtx = null;
        this._vpSeekDebounce = null;
        this._activePlaybackVideo = null;
        this._activePlaybackAudios = [];

        // Snapping
        this.snappingEnabled = !!this._settings.timelineBehavior.snappingEnabled;
        this._snapThreshold = this._settings.timelineBehavior.snapThreshold;
        this._snapIndicator = null; // frame number of active snap line, or null

        // Track layout: dynamically built array of { type, label, laneIndex, collapsed }
        this._trackLayout = [];
        this._buildTrackLayout();

        // Timecode display mode: "frames" or "timecode" (HH:MM:SS:FF)
        this._timecodeMode = this._settings.timelineBehavior.timecodeMode;

        // Undo/Redo state
        this._undoStack = [];   // Array of { sceneId, snapshot, label }
        this._redoStack = [];
        this._maxUndoSteps = 50;

        // Thumbnail strip cache: { assetId: { img: Image, frameWidth, numFrames, loaded } }
        this._thumbStripCache = {};
        // Waveform cache: { assetId: { peaks: [[min,max],...], numBuckets, loaded } }
        this._waveformCache = {};
        // Asset path→assetId reverse lookup (rebuilt on asset fetch)
        this._pathToAsset = {};

        // Per-section UI scale factors and layout settings
        this._scaleToolbar = this._settings.layout.scaleToolbar;
        this._scaleTrackHeaders = this._settings.layout.scaleTrackHeaders;
        this._scaleTimeline = this._settings.layout.scaleTimeline;
        this._scaleGallery = this._settings.layout.scaleGallery;
        this._labelWidthUser = this._settings.layout.labelWidth;
        this._labelWidthUserFS = this._settings.layout.labelWidthFullscreen;

        // Editor focus state — true when user last clicked inside the editor
        this._editorFocused = false;

        // Fullscreen state
        this.isFullscreen = false;
        this._timelineHeight = TIMELINE_HEIGHT;
        this._galleryHeight = GALLERY_HEIGHT;
        this._fullscreenOverlay = null;
        this._fullscreenPlaceholder = null;
        this._nodeParent = null;
        this._nodeSibling = null;

        // Build DOM
        this._buildDOM();
        this._setupKeyboardEvents();
        this._refreshContextInputs();
        this._syncTakePlacementModeWidget();

        // Apply initial UI scales (bars + canvas will be scaled on first render)
        this._applyScales();
        this._settingsUnsubscribe = subscribeEditorSettings((settings) => this._handleSettingsChange(settings));

        // Window resize handler for fullscreen
        this._windowResizeHandler = () => {
            if (this.isFullscreen) {
                this._recalcFullscreenHeights();
                this._renderTimeline();
            }
        };
        window.addEventListener("resize", this._windowResizeHandler);

        // ResizeObserver on container to auto-re-render timeline when size changes
        this._containerResizeObserver = new ResizeObserver(() => {
            this._renderTimeline();
        });
    }

    _createNodeWidgetHost(node) {
        return {
            getValue: (name, defaultValue = 0) => {
                const widget = node?.widgets?.find(w => w.name === name);
                return widget ? widget.value : defaultValue;
            },
            setValue: (name, value) => {
                const widget = node?.widgets?.find(w => w.name === name);
                if (!widget) return;
                if (Object.is(widget.value, value)) return;
                widget.value = value;
                this.onWidgetValueChange?.(name, value);
            },
            setValueLocal: (name, value) => {
                const widget = node?.widgets?.find(w => w.name === name);
                if (widget) widget.value = value;
            },
            getNodeId: () => node?.id ?? "anon",
            getSize: () => node?.size ? [...node.size] : null,
            setSize: (size) => node?.setSize?.(size),
            computeSize: () => node?.computeSize?.(),
            markDirty: () => node?.setDirtyCanvas?.(true, true),
        };
    }

    _setHostValueLocal(name, value) {
        if (this.widgetHost?.setValueLocal) {
            this.widgetHost.setValueLocal(name, value);
            return;
        }
        this._setWidgetValue(name, value);
    }

    applyWidgetState(values = {}) {
        if (!values || typeof values !== "object") return;
        const fieldNames = Object.keys(values);
        if (this.isDragging && fieldNames.length > 0) {
            // #36 residual: remote widget_state_changed events can arrive while a
            // drag/trim owns editor object references and scalar selection state.
            // This ephemeral buffer is replayed after mouseup/commit and never
            // persists to project or browser-local state.
            this._pendingApplyWidgetState = {
                ...(this._pendingApplyWidgetState || {}),
                ...values,
            };
            sessionDiagRecord("widget_state_deferred", {
                drag_type: this.dragType || "",
                fields: fieldNames.sort(),
            });
            return;
        }
        this._applyingWidgetState = true;
        try {
            for (const [name, value] of Object.entries(values)) {
                this._setHostValueLocal(name, value);
            }
            if (Object.prototype.hasOwnProperty.call(values, "scene_id")) {
                const sceneId = String(values.scene_id || "");
                if (sceneId && sceneId !== this.activeSceneId) {
                    const scene = this.scenes.find(s => s.scene_id === sceneId);
                    if (scene) {
                        this._setActiveScene(scene);
                    } else {
                        this.activeSceneId = sceneId;
                    }
                }
            }
        } finally {
            this._applyingWidgetState = false;
        }
        if (Object.prototype.hasOwnProperty.call(values, "selection_start")) {
            this.selectionStart = Math.max(0, parseInt(values.selection_start, 10) || 0);
        }
        if (Object.prototype.hasOwnProperty.call(values, "selection_end")) {
            this.selectionEnd = Math.max(0, parseInt(values.selection_end, 10) || 0);
        }
        if (Object.prototype.hasOwnProperty.call(values, "render_queue_active")) {
            this.renderQueueActive = coerceBoolean(values.render_queue_active, true);
        }
        this.selectionStart = Math.min(this.selectionStart, this.selectionEnd);
        this.playhead = Math.max(0, Math.min(this.playhead, this.totalFrames));
        this._refreshContextInputs();
        this._refreshSelectionInputs();
        this._updateToolbar();
        this._renderTimeline();
        this._renderQueuePanel();
    }

    _flushDeferredDragState(commitPromise = null) {
        const replay = () => {
            const pendingWidgetState = this._pendingApplyWidgetState;
            const pendingScenesRefresh = !!this._pendingScenesRefresh;
            this._pendingApplyWidgetState = null;
            this._pendingScenesRefresh = false;

            if (pendingWidgetState && Object.keys(pendingWidgetState).length > 0) {
                sessionDiagRecord("widget_state_replayed", {
                    fields: Object.keys(pendingWidgetState).sort(),
                });
                this.applyWidgetState(pendingWidgetState);
            }
            if (pendingScenesRefresh) {
                this._fetchScenes({ reason: "deferred_drag_replay" });
            }
        };

        if (commitPromise && typeof commitPromise.then === "function") {
            commitPromise.then(replay, replay);
            return;
        }
        replay();
    }

    _shouldDeferSceneRefresh({ ignoreMutationGate = false, ignoreTimelineGate = false } = {}) {
        return !!(
            this.isDragging
            || (!ignoreMutationGate && !ignoreTimelineGate && this._timelineMutationDepth > 0)
            || (!ignoreMutationGate && this._hasPendingProjectMutations())
        );
    }

    _deferSceneRefresh(reason = "unknown", details = {}) {
        this._pendingScenesRefresh = true;
        sessionDiagRecord("scene_refresh_deferred", {
            reason,
                drag_type: this.dragType || "",
                mutation_depth: this._timelineMutationDepth || 0,
                project_mutation_busy: this._hasPendingProjectMutations(),
                ...details,
            });
    }

    async _withTimelineMutationCommit(kind, callback) {
        this._timelineMutationDepth += 1;
        sessionDiagRecord("timeline_mutation_commit_start", {
            kind,
            mutation_depth: this._timelineMutationDepth,
        });
        try {
            return await callback();
        } finally {
            this._timelineMutationDepth = Math.max(0, this._timelineMutationDepth - 1);
            sessionDiagRecord("timeline_mutation_commit_end", {
                kind,
                mutation_depth: this._timelineMutationDepth,
            });
        }
    }

    _buildDOM() {
        // Main container
        this.container = document.createElement("div");
        this.container.dataset.sonderEditor = "1"; // marker for global drop interceptor
        this.container.style.cssText = `
            width: 100%; display: flex; flex-direction: column;
            padding: 4px 8px;
            font-family: ${FONT.sans}; font-size: 11px;
            color: ${COLORS.text}; user-select: none;
            box-sizing: border-box;
            background: linear-gradient(180deg, ${COLORS.panel} 0%, ${COLORS.panelMuted} 100%);
        `;

        // Scene bar
        this._buildSceneBar();

        // Toolbar (snap, cut, split, shortcut hints)
        this._buildToolbar();

        // Timeline canvas
        this.timelineCanvas = document.createElement("canvas");
        this.timelineCanvas.style.cssText = `cursor: crosshair; flex-shrink: 0; display: block;`;
        this.timelineCanvas.height = this._timelineHeight;
        this.container.appendChild(this.timelineCanvas);

        // Asset gallery
        this._buildAssetGallery();

        // Events
        this._setupTimelineEvents();

        // Observe container size changes for auto-resize
        if (this._containerResizeObserver) {
            this._containerResizeObserver.observe(this.container);
        }
    }

    _buildSceneBar() {
        return buildEditorSceneBar(this, { sceneBarHeight: SCENE_BAR_HEIGHT });
    }

    _buildToolbar() {
        return buildEditorToolbar(this);
    }

    _queueChromeBadges(queue = this._renderQueue) {
        return queueChromeBadges(this, queue);
    }

    _updateQueueChromeStatus() {
        return updateQueueChromeStatus(this);
    }

    _updateToolbar() {
        return updateEditorToolbar(this);
    }
    // Info bar removed — zoom/fullscreen controls moved to toolbar.
    // _updateInfoLabel calls replaced with _updateToolbar.
    _buildAssetGallery() {
        const gallery = document.createElement("div");
        gallery.style.cssText = `
            background: ${COLORS.galleryBg}; border-top: 1px solid ${COLORS.border};
            min-height: ${this._galleryHeight}px; overflow: hidden;
            display: flex; flex-direction: column;
        `;

        // Asset grid
        this.assetGrid = document.createElement("div");
        this.assetGrid.style.cssText = `
            flex: 1; min-height: 0; overflow: hidden; padding: 6px;
            box-sizing: border-box;
        `;
        this._assetGallery = mountSharedAssetGallery(this.assetGrid, {
            ownerId: this._keyboardConsumerId("gallery"),
            getProjectDir: () => this.projectDir,
            initialData: { assets: [], folders: [] },
            onImportFiles: async (files, folder) => {
                let importedAny = false;
                for (const file of files) {
                    if (await importFileIntoProject(this.projectDir, file, folder)) {
                        importedAny = true;
                    }
                }
                if (importedAny) await this._fetchAssets();
            },
            onUpdateAsset: async (assetId, updates) => await this._updateAssetMetadata(assetId, updates),
            onGetAssetUsages: async (assetId) => await this._getAssetUsages(assetId),
            onGetBulkAssetUsages: async (assetIds) => await this._getBulkAssetUsages(assetIds),
            onDeleteAsset: async (assetId, force) => await this._deleteAsset(assetId, force),
            onBulkMoveAssets: async (assetIds, folder) => await this._bulkMoveAssets(assetIds, folder),
            onBulkDeleteAssets: async (assetIds, force) => await this._bulkDeleteAssets(assetIds, force),
            onRestoreAsset: async (assetId) => await this._restoreAsset(assetId),
            onBulkRestoreAssets: async (assetIds) => await this._bulkRestoreAssets(assetIds),
            onPermanentDeleteAsset: async (assetId, force) => await this._permanentDeleteAsset(assetId, force),
            onBulkPermanentDeleteAssets: async (assetIds, force) => await this._bulkPermanentDeleteAssets(assetIds, force),
            onEmptyTrash: async () => await this._emptyTrash(),
            onCreateFolder: async (folderName) => await this._createAssetFolder(folderName),
            onRenameFolder: async (folderName, newFolderName) => await this._renameAssetFolder(folderName, newFolderName),
            onDeleteFolder: async (folderName, force) => await this._deleteAssetFolder(folderName, force),
            onReplaceAsset: async (assetId, file) => await this._replaceAsset(assetId, file),
            onSetSceneAspectRatio: (width, height) => this._setSceneAspectRatioFromDimensions(width, height),
            onOpenSourceWorkflow: async (asset) => {
                const handled = await window.__SONDER_OPEN_SOURCE_WORKFLOW__?.(this.projectDir, asset);
                if (!handled) this._showToast?.("Source workflow unavailable");
            },
            onRefresh: async () => await this._fetchAssets(),
        });

        // Render Queue section (collapsible)
        const queueSection = document.createElement("div");
        queueSection.style.cssText = `border-top: 1px solid ${COLORS.border};`;

        const queueHeader = document.createElement("div");
        queueHeader.style.cssText = `
            display: flex; align-items: center; justify-content: space-between;
            padding: 4px 8px; background: ${COLORS.panelRaised}; cursor: pointer; font-size: 10px; color: ${COLORS.textDim};
            border-bottom: 1px solid ${COLORS.borderSoft};
        `;
        this._queueHeaderLabel = document.createElement("span");
        queueHeader.appendChild(this._queueHeaderLabel);
        this._queueContainer = document.createElement("div");
        this._queueContainer.style.cssText = `max-height: 0; overflow: hidden; transition: max-height 0.2s; background: ${COLORS.panelMuted};`;
        this._queueContainer.innerHTML = `<div style="padding: 10px; color: ${COLORS.textMuted}; font-style: italic; font-size: 10px;">Queue empty — use + Queue to add jobs</div>`;

        this._applyQueueExpandedState();
        queueHeader.addEventListener("click", () => {
            this._setQueueExpanded(!this._queueExpanded, { persist: true, fetch: true });
        });

        queueSection.append(queueHeader, this._queueContainer);

        gallery.append(this.assetGrid, queueSection);
        this.container.appendChild(gallery);
        this.galleryEl = gallery;
    }

    // ── Button Helper ──────────────────────────────────────────────────
    _makeBtn(text, title = "") {
        const btn = document.createElement("button");
        btn.textContent = text;
        btn.title = title;
        btn.style.cssText = chromeButtonCss({ variant: "muted", padding: "2px 8px", fontSize: "12px", radius: "6px" });
        setButtonVariant(btn, "muted");
        btn.dataset.sonderHoverVariant = "primary";
        btn.addEventListener("mouseenter", () => setButtonVariant(btn, btn.dataset.sonderHoverVariant || btn.dataset.sonderBaseVariant || "muted", { persist: false }));
        btn.addEventListener("mouseleave", () => setButtonVariant(btn, btn.dataset.sonderBaseVariant || "muted", { persist: false }));
        return btn;
    }

    _showToast(message, { duration = 2200 } = {}) {
        if (!message) return;
        if (this._toastTimer) {
            window.clearTimeout(this._toastTimer);
            this._toastTimer = null;
        }
        if (!this._toastEl) {
            this._toastEl = document.createElement("div");
            this._toastEl.style.cssText = `
                position: fixed;
                left: 50%;
                bottom: 24px;
                transform: translateX(-50%);
                z-index: 10020;
                max-width: min(420px, calc(100vw - 32px));
                padding: 10px 14px;
                border-radius: 10px;
                background: rgba(18, 24, 32, 0.96);
                border: 1px solid ${COLORS.warningBorder};
                color: ${COLORS.warningText};
                box-shadow: 0 16px 34px rgba(0, 0, 0, 0.36);
                font-size: 11px;
                font-weight: 600;
                letter-spacing: 0.01em;
                pointer-events: none;
            `;
            document.body.appendChild(this._toastEl);
        }
        this._toastEl.textContent = message;
        this._toastEl.style.opacity = "1";
        this._toastTimer = window.setTimeout(() => {
            if (!this._toastEl) return;
            this._toastEl.style.opacity = "0";
        }, duration);
    }

    // ── Scene Management ───────────────────────────────────────────────
    async _fetchScenes({ ignoreMutationGate = false, reason = "external" } = {}) {
        if (!this.projectDir) return;

        // #36 mid-drag refresh race: external WS / heartbeat / timer paths can call
        // `_fetchScenes` while the user is mid-drag/mid-trim. `_setActiveScene` replaces
        // `activeScene.clips` and `.audio_tracks` with fresh server-side instances, which
        // orphans every `_dragItemsOrig`/`_trimItem` `data` reference held by the drag
        // handler. The next mousemove tick then mutates ghost objects while the renderer
        // reads the new ones → visual snap-back to mousedown origin every tick.
        // Defer the refresh until the drag/trim completes; mouseup will replay it.
        // #36 follow-up: also gate the commit window, and re-check before apply
        // because a pre-drag fetch can resolve after the next drag has started.
        if (this._shouldDeferSceneRefresh({ ignoreMutationGate })) {
            this._deferSceneRefresh(reason, { stage: "start" });
            return;
        }

        const fetchSeq = ++this._sceneFetchSeq;
        try {
            const dirName = this.projectDir.split(/[/\\]/).pop();
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes`));
            if (resp.ok) {
                const data = await resp.json();
                if (fetchSeq !== this._sceneFetchSeq) {
                    sessionDiagRecord("scene_refresh_stale", {
                        reason,
                        fetch_seq: fetchSeq,
                        current_seq: this._sceneFetchSeq,
                    });
                    return;
                }
                if (this._shouldDeferSceneRefresh({ ignoreMutationGate })) {
                    this._deferSceneRefresh(reason, { stage: "apply" });
                    return;
                }
                this._pendingScenesRefresh = false;
                this.scenes = data.scenes || [];
                if (this.scenes.length > 0) {
                    const widgetSceneId = String(this._getWidgetValue("scene_id", "") || "");
                    const desiredSceneIds = [this.activeSceneId, widgetSceneId].filter((id, idx, arr) => id && arr.indexOf(id) === idx);
                    const scene = desiredSceneIds
                        .map(id => this.scenes.find(s => s.scene_id === id))
                        .find(Boolean);
                    if (scene) {
                        this._setActiveScene(scene);
                    } else {
                        this._setActiveScene(this.scenes[0]);
                    }
                }
            }
        } catch (e) {
            console.warn("[Sonder] Failed to fetch scenes:", e);
        }
    }

    async _createScene() {
        if (!this.projectDir) return;
        const dirName = this.projectDir.split(/[/\\]/).pop();

        try {
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes`), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    name: `Scene ${this.scenes.length + 1}`,
                    duration_frames: Math.max(1, this._defaultNewSceneDuration()),
                }),
            });
            if (resp.ok) {
                const scene = await resp.json();
                this.scenes.push(scene);
                this._setActiveScene(scene);
            }
        } catch (e) {
            console.warn("[Sonder] Failed to create scene:", e);
        }
    }

    _setActiveScene(scene) {
        const hasActiveScene = !!this.activeScene;
        const preservePendingFrameSelection = !hasActiveScene && this.activeSceneId === scene.scene_id;
        const isSameScene = hasActiveScene && this.activeSceneId === scene.scene_id;
        if (!isSameScene && hasActiveScene) {
            this._persistActiveTimelineSelection();
        }
        const storedSelection = (!isSameScene && !preservePendingFrameSelection)
            ? this._readStoredTimelineSelection(scene)
            : null;

        if (!isSameScene) {
            if (this._animaticMode && this._restoreAnimaticState()) {
                this._saveLaneConfig();
            }
            this._animaticMode = false;
            this._preAnimaticHidden = null;
            this._stopPlayback();
            // Clear undo/redo on scene switch (snapshots are scene-specific)
            this._undoStack = [];
            this._redoStack = [];
            this.scrollY = 0;
            this._selectedPromptIdx = null;
            this._hidePromptEditor();
            this._clearSelection();
            this._hideItemEditor();
            if (!preservePendingFrameSelection) {
                this.selectionStart = storedSelection?.start ?? 0;
                this.selectionEnd = storedSelection?.end ?? 0;
                this.playhead = this.selectionStart < this.selectionEnd ? this.selectionStart : 0;
                this._setWidgetValue("selection_start", this.selectionStart);
                this._setWidgetValue("selection_end", this.selectionEnd);
            }
        }

        this.activeScene = scene;
        this.activeSceneId = scene.scene_id;
        if (isSameScene || preservePendingFrameSelection) {
            this._reconcileSelection();
        }
        this._buildTrackLayout();
        this.totalFrames = scene.duration_frames || 200;
        this._refreshDurationInput();
        this.sceneLabel.textContent = scene.name || "Untitled Scene";

        // Load scene resolution/fps into inputs
        this._syncSceneResolutionControls();
        this._syncSceneFpsControl();
        this._updateViewportHeader();

        // Update hidden widgets
        this._setWidgetValue("scene_id", scene.scene_id);

        // Update fullscreen title if in fullscreen
        if (this.isFullscreen && this._fsTitle) {
            this._fsTitle.textContent = `Editor — ${scene.name || "No Scene"}`;
        }

        if (!isSameScene) {
            // Defer auto-fit to next frame so browser reflows after editor hide
            this._renderTimeline(); // Immediate render with new scene data
            requestAnimationFrame(() => this._fitToView());
        } else {
            this._renderTimeline();
        }
        this._resizeViewportCanvas();
        this._renderViewportFrame();
        this._updateToolbar();
    }

    _refreshDurationInput() {
        if (!this.durationInput) return;
        if (this._timecodeMode === "timecode") {
            this.durationInput.type = "text";
            this.durationInput.inputMode = "decimal";
            this.durationInput.value = this._framesToSeconds(this.totalFrames).toFixed(2);
            this._durLabel.textContent = "Duration:";
        } else {
            this.durationInput.type = "number";
            this.durationInput.inputMode = "numeric";
            this.durationInput.value = this.totalFrames;
            this._durLabel.textContent = "Frames:";
        }
    }

    _clampTimelineStateToDuration() {
        const maxFrame = Math.max(0, this.totalFrames);
        this.playhead = Math.min(this.playhead, maxFrame);
        this.selectionStart = Math.min(this.selectionStart, maxFrame);
        this.selectionEnd = Math.min(this.selectionEnd, maxFrame);
        this._setWidgetValue("selection_start", this.selectionStart);
        this._setWidgetValue("selection_end", this.selectionEnd);
        this._persistActiveTimelineSelection();
    }

    _onResolutionChange(axis = "w") {
        this._resolutionEditAxis = axis === "h" ? "h" : "w";
        const mode = this._resolutionControlMode();
        if (mode === "locked") {
            this._syncSceneResolutionControls({ detectSelections: false });
            return;
        }

        if (mode === "aspect-custom") {
            this._resetFreeAspectTierDraft();
            const resolution = this._resolveAspectCustomResolution(this._resolutionEditAxis);
            if (!resolution) return;
            this._setResolutionInputs(resolution.width, resolution.height);
            this._updateSceneResolution(resolution.width, resolution.height, { detectSelections: false });
            return;
        }

        if (mode === "tier-custom-aspect") {
            this._markFreeAspectTierDraft(this._resolutionEditAxis);
            const resolution = this._resolveFreeAspectTierResolution({ requireDraftComplete: true });
            if (!resolution) {
                return;
            }
            this._resetFreeAspectTierDraft();
            this._setResolutionInputs(resolution.width, resolution.height);
            this._updateSceneResolution(resolution.width, resolution.height, { detectSelections: false });
            return;
        }

        this._resetFreeAspectTierDraft();
        let { width, height } = this._readResolutionInputs();
        const template = this._getActiveTemplate();
        if (template.id !== "free") {
            ({ width, height } = snapResolution(width, height, template));
        }
        this._setResolutionInputs(width, height);
        if (this._resTierSelect) {
            this._resTierSelect.value = "custom";
        }
        this._updateSceneResolution(width, height, { detectSelections: false });
    }

    async _updateSceneResolution(w, h, { detectSelections = true } = {}) {
        if (!this.activeScene || !this.projectDir) return;
        w = Math.max(0, parseInt(w, 10) || 0);
        h = Math.max(0, parseInt(h, 10) || 0);
        const sceneRef = this.activeScene;
        const sceneId = this.activeSceneId;
        const prevWidth = sceneRef.width || 0;
        const prevHeight = sceneRef.height || 0;
        sceneRef.width = w;
        sceneRef.height = h;
        if (this.activeScene === sceneRef && this.activeSceneId === sceneId) {
            this._syncSceneResolutionControls({ detectSelections });
            this._updateViewportHeader();
            this._resizeViewportCanvas();
            this._renderViewportFrame();
        }
        try {
            await this._runSceneMutation(
                [{ type: "update_scene_fields", fields: { width: w, height: h } }],
                {
                    key: `scene:${sceneId}:width-height`,
                    label: "scene resolution",
                    coalesce: true,
                    refreshScenes: false,
                }
            );
        } catch (e) {
            sceneRef.width = prevWidth;
            sceneRef.height = prevHeight;
            if (this.activeScene === sceneRef && this.activeSceneId === sceneId) {
                this._syncSceneResolutionControls({ detectSelections });
                this._updateViewportHeader();
                this._resizeViewportCanvas();
                this._renderViewportFrame();
            }
            console.warn("[Sonder] Failed to update scene resolution:", e);
        }
    }

    async _updateSceneFps(fps) {
        if (!this.activeScene || !this.projectDir) return;
        const sceneRef = this.activeScene;
        const sceneId = this.activeSceneId;
        try {
            await this._runSceneMutation(
                [{ type: "update_scene_fields", fields: { fps } }],
                {
                    key: `scene:${sceneId}:fps`,
                    label: "scene fps",
                    coalesce: true,
                    refreshScenes: false,
                }
            );
            sceneRef.fps = fps;
            if (this.activeScene === sceneRef && this.activeSceneId === sceneId) {
                this._syncSceneFpsControl();
                this._updateViewportHeader();
                if (this._timecodeMode === "timecode") {
                    this._refreshDurationInput();
                    if (this._itemEditorEl && this.selectedItem) {
                        this._showItemEditor();
                    }
                }
                this._renderTimeline();
                this._renderViewportFrame();
                this._updateToolbar();
                this._updateTransportUI();
            }
        } catch (e) {
            console.warn("[Sonder] Failed to update scene FPS:", e);
        }
    }

    _cycleScene(dir) {
        if (this.scenes.length === 0) return;
        const idx = this.scenes.findIndex(s => s.scene_id === this.activeSceneId);
        let newIdx = idx + dir;
        if (newIdx < 0) newIdx = this.scenes.length - 1;
        if (newIdx >= this.scenes.length) newIdx = 0;
        this._setActiveScene(this.scenes[newIdx]);
    }

    async _renameScene() {
        if (!this.activeScene) return;
        const name = prompt("Scene name:", this.activeScene.name);
        if (!name || name === this.activeScene.name) return;
        this._pushUndo("rename scene");

        try {
            await this._runSceneMutation(
                [{ type: "update_scene_fields", fields: { name } }],
                {
                    key: `scene:${this.activeSceneId}:name`,
                    label: "rename scene",
                    coalesce: true,
                    refreshScenes: false,
                }
            );
            this.activeScene.name = name;
            this.sceneLabel.textContent = name;
        } catch (e) {
            console.warn("[Sonder] Failed to rename scene:", e);
        }
    }

    async _updateSceneDuration(frames) {
        if (!this.activeScene || !this.projectDir) return;
        frames = Math.max(1, parseInt(frames, 10) || 1);
        this._pushUndo("change duration");
        const sceneRef = this.activeScene;
        const sceneId = this.activeSceneId;
        const prevDuration = sceneRef.duration_frames || this.totalFrames;
        sceneRef.duration_frames = frames;
        if (this.activeScene === sceneRef && this.activeSceneId === sceneId) {
            this.totalFrames = frames;
            this._clampTimelineStateToDuration();
            this._refreshDurationInput();
            this._renderTimeline();
            this._renderViewportFrame();
            this._updateToolbar();
            this._updateTransportUI();
        }
        try {
            await this._runSceneMutation(
                [{ type: "update_scene_fields", fields: { duration_frames: frames } }],
                {
                    key: `scene:${sceneId}:duration`,
                    label: "scene duration",
                    coalesce: true,
                    refreshScenes: false,
                }
            );
        } catch (e) {
            sceneRef.duration_frames = prevDuration;
            if (this.activeScene === sceneRef && this.activeSceneId === sceneId) {
                this.totalFrames = prevDuration;
                this._clampTimelineStateToDuration();
                this._refreshDurationInput();
                this._renderTimeline();
                this._renderViewportFrame();
                this._updateToolbar();
                this._updateTransportUI();
            }
            console.warn("[Sonder] Failed to update scene duration:", e);
        }
    }

    async _deleteScene() {
        if (!this.activeScene || !this.projectDir) return;
        if (this.scenes.length <= 1) {
            alert("Cannot delete the last scene.");
            return;
        }
        if (!confirm(`Delete scene "${this.activeScene.name}"? This cannot be undone.`)) return;

        const dirName = this.projectDir.split(/[/\\]/).pop();
        try {
            await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}`), {
                method: "DELETE",
            });
            await this._fetchScenes();
        } catch (e) {
            console.warn("[Sonder] Failed to delete scene:", e);
        }
    }

    async _duplicateScene() {
        if (!this.activeScene || !this.projectDir) return;
        const dirName = this.projectDir.split(/[/\\]/).pop();

        try {
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}/duplicate`), {
                method: "POST",
            });

            if (!resp.ok) return;
            const newScene = await resp.json();
            this.scenes.push(newScene);
            this._setActiveScene(newScene);
        } catch (e) {
            console.warn("[Sonder] Failed to duplicate scene:", e);
        }
    }

    // ── Asset Management ───────────────────────────────────────────────
    async _fetchAssets({ ignoreMutationGate = false, reason = "assets" } = {}) {
        if (!this.projectDir) return;
        if (!ignoreMutationGate && this._hasPendingProjectMutations()) {
            this._deferProjectBackedRefresh(["assets"], reason);
            return;
        }

        try {
            const dirName = this.projectDir.split(/[/\\]/).pop();
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets?${this._assetListQueryString()}`));
            if (resp.ok) {
                const data = await resp.json();
                this.assets = { video: [], image: [], audio: [], artifact: [] };
                this._pathToAsset = {};
                for (const asset of (data.assets || [])) {
                    if (this.assets[asset.asset_type]) {
                        this.assets[asset.asset_type].push(asset);
                    }
                    if (asset.path) this._pathToAsset[asset.path] = asset;
                }
                this._assetGallery?.setData({
                    assets: data.assets || [],
                    folders: data.folders || [],
                });
            }
        } catch (e) {
            console.warn("[Sonder] Failed to fetch assets:", e);
        }
    }

    async _updateAssetMetadata(assetId, updates) {
        if (!this.projectDir || !assetId) return null;
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/${assetId}`), {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(updates),
        });
        if (!resp.ok) {
            throw new Error(`Asset update failed: ${resp.status}`);
        }
        const updatedAsset = await resp.json();
        await this._fetchAssets();
        return updatedAsset;
    }

    async _getAssetUsages(assetId) {
        if (!this.projectDir || !assetId) return null;
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/${assetId}/usages`));
        if (!resp.ok) {
            throw new Error(`Asset usage fetch failed: ${resp.status}`);
        }
        return await resp.json();
    }

    async _getBulkAssetUsages(assetIds) {
        if (!this.projectDir || !Array.isArray(assetIds) || !assetIds.length) return null;
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/bulk-usages`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ asset_ids: assetIds }),
        });
        if (!resp.ok) {
            throw new Error(`Bulk asset usage fetch failed: ${resp.status}`);
        }
        return await resp.json();
    }

    async _bulkMoveAssets(assetIds, folder = "") {
        if (!this.projectDir || !Array.isArray(assetIds) || !assetIds.length) return { updated: 0 };
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/bulk-move`), {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ asset_ids: assetIds, folder }),
        });
        if (!resp.ok) {
            throw new Error(`Bulk asset move failed: ${resp.status}`);
        }
        return await resp.json();
    }

    async _deleteAsset(assetId, force = false) {
        if (!this.projectDir || !assetId) return { status: "noop" };
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/${assetId}`), {
            method: "DELETE",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ force: !!force }),
        });
        if (resp.status === 409) {
            const payload = await resp.json();
            return { status: "conflict", ...(payload || {}) };
        }
        if (!resp.ok) {
            throw new Error(`Asset delete failed: ${resp.status}`);
        }
        const payload = await resp.json();
        await Promise.all([
            this._fetchAssets(),
            this._fetchRenderQueue(),
        ]);
        return { status: "trashed", ...(payload || {}) };
    }

    async _bulkDeleteAssets(assetIds, force = false) {
        if (!this.projectDir || !Array.isArray(assetIds) || !assetIds.length) return { status: "noop" };
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/bulk-delete`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ asset_ids: assetIds, force: !!force }),
        });
        if (resp.status === 409) {
            const payload = await resp.json();
            return { status: "conflict", ...(payload || {}) };
        }
        if (!resp.ok) {
            throw new Error(`Bulk asset delete failed: ${resp.status}`);
        }
        const payload = await resp.json();
        await Promise.all([
            this._fetchAssets(),
            this._fetchRenderQueue(),
        ]);
        return { status: "trashed", ...(payload || {}) };
    }

    async _restoreAsset(assetId) {
        if (!this.projectDir || !assetId) return { status: "noop" };
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/restore`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ asset_id: assetId }),
        });
        if (!resp.ok) {
            throw new Error(`Asset restore failed: ${resp.status}`);
        }
        const payload = await resp.json();
        await Promise.all([
            this._fetchAssets(),
            this._fetchRenderQueue(),
        ]);
        return { status: "restored", ...(payload || {}) };
    }

    async _bulkRestoreAssets(assetIds) {
        if (!this.projectDir || !Array.isArray(assetIds) || !assetIds.length) return { status: "noop" };
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/bulk-restore`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ asset_ids: assetIds }),
        });
        if (!resp.ok) {
            throw new Error(`Bulk asset restore failed: ${resp.status}`);
        }
        const payload = await resp.json();
        await Promise.all([
            this._fetchAssets(),
            this._fetchRenderQueue(),
        ]);
        return { status: "restored", ...(payload || {}) };
    }

    async _permanentDeleteAsset(assetId, force = false) {
        if (!this.projectDir || !assetId) return { status: "noop" };
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/permanent`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ asset_id: assetId, force: !!force }),
        });
        if (resp.status === 409) {
            const payload = await resp.json();
            return { status: "conflict", ...(payload || {}) };
        }
        if (!resp.ok) {
            throw new Error(`Permanent asset delete failed: ${resp.status}`);
        }
        const payload = await resp.json();
        await Promise.all([
            this._fetchAssets(),
            this._fetchRenderQueue(),
        ]);
        return { status: "deleted", ...(payload || {}) };
    }

    async _bulkPermanentDeleteAssets(assetIds, force = false) {
        if (!this.projectDir || !Array.isArray(assetIds) || !assetIds.length) return { status: "noop" };
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/bulk-permanent-delete`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ asset_ids: assetIds, force: !!force }),
        });
        if (resp.status === 409) {
            const payload = await resp.json();
            return { status: "conflict", ...(payload || {}) };
        }
        if (!resp.ok) {
            throw new Error(`Bulk permanent asset delete failed: ${resp.status}`);
        }
        const payload = await resp.json();
        await Promise.all([
            this._fetchAssets(),
            this._fetchRenderQueue(),
        ]);
        return { status: "deleted", ...(payload || {}) };
    }

    async _emptyTrash() {
        if (!this.projectDir) return { status: "noop" };
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/empty-trash`), {
            method: "POST",
        });
        if (!resp.ok) {
            throw new Error(`Empty trash failed: ${resp.status}`);
        }
        const payload = await resp.json();
        await Promise.all([
            this._fetchAssets(),
            this._fetchRenderQueue(),
        ]);
        return { status: "deleted", ...(payload || {}) };
    }

    async _createAssetFolder(folderName) {
        if (!this.projectDir || !folderName) return [];
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/folders`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ folder: folderName }),
        });
        if (!resp.ok) {
            throw new Error(`Folder create failed: ${resp.status}`);
        }
        const payload = await resp.json();
        await this._fetchAssets();
        return payload.folders || [];
    }

    async _renameAssetFolder(folderName, newFolderName) {
        if (!this.projectDir || !folderName || !newFolderName) return [];
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/folders`), {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ old_folder: folderName, new_folder: newFolderName }),
        });
        if (!resp.ok) {
            throw new Error(`Folder rename failed: ${resp.status}`);
        }
        const payload = await resp.json();
        await this._fetchAssets();
        return payload || { folders: [] };
    }

    async _deleteAssetFolder(folderName, force = false) {
        if (!this.projectDir || !folderName) return { status: "noop" };
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/folders`), {
            method: "DELETE",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ folder: folderName, force: !!force }),
        });
        if (resp.status === 409) {
            const payload = await resp.json();
            return { status: "conflict", ...(payload || {}) };
        }
        if (!resp.ok) {
            throw new Error(`Folder delete failed: ${resp.status}`);
        }
        const payload = await resp.json();
        await this._fetchAssets();
        return { status: "deleted", ...(payload || {}) };
    }

    async _replaceAsset(assetId, file) {
        if (!this.projectDir || !assetId || !file) return null;
        const payload = await replaceAssetInProject(this.projectDir, assetId, file);
        await Promise.all([
            this._fetchAssets(),
            this._fetchScenes(),
            this._fetchRenderQueue(),
        ]);
        return payload?.asset || null;
    }

    _selectAssetTab(type) {
        this.selectedAssetType = type;
        if (!this.tabBtns || !this.emptyMsg) return;
        for (const [t, btn] of Object.entries(this.tabBtns)) {
            const count = (this.assets[t] || []).length;
            const label = t.charAt(0).toUpperCase() + t.slice(1) + "s";
            btn.textContent = count > 0 ? `${label} (${count})` : label;
            btn.style.color = t === type ? COLORS.text : COLORS.galleryLabel;
            btn.style.borderBottomColor = t === type ? COLORS.sceneBtnActive : "transparent";
        }
        this._renderAssetGrid();
    }

    _renderAssetGrid() {
        if (!this.emptyMsg) return;
        this.assetGrid.innerHTML = "";
        const items = this.assets[this.selectedAssetType] || [];

        if (items.length === 0) {
            const msg = this.emptyMsg.cloneNode(true);
            msg.textContent = `No ${this.selectedAssetType}s in project.`;
            this.assetGrid.appendChild(msg);
            return;
        }

        // Group by folder
        const rootItems = [];
        const folders = {};
        for (const asset of items) {
            const folder = asset.folder || "";
            if (folder) {
                if (!folders[folder]) folders[folder] = [];
                folders[folder].push(asset);
            } else {
                rootItems.push(asset);
            }
        }

        // Render root items first
        for (const asset of rootItems) {
            this.assetGrid.appendChild(this._buildAssetItem(asset));
        }

        // Render folder groups
        const sortedFolders = Object.keys(folders).sort();
        for (const folderName of sortedFolders) {
            const folderHeader = document.createElement("div");
            folderHeader.style.cssText = `
                width: 100%; padding: 4px 8px; background: #1a1a2e;
                border-radius: 3px; cursor: pointer; font-size: 10px; color: #8cf;
                display: flex; align-items: center; gap: 4px; margin-top: 4px;
            `;
            const collapsed = this._collapsedFolders?.[folderName] ?? false;
            folderHeader.innerHTML = `<span>${collapsed ? "▸" : "▾"}</span> 📁 ${folderName} (${folders[folderName].length})`;
            folderHeader.addEventListener("click", () => {
                if (!this._collapsedFolders) this._collapsedFolders = {};
                this._collapsedFolders[folderName] = !this._collapsedFolders[folderName];
                this._renderAssetGrid();
            });
            this.assetGrid.appendChild(folderHeader);

            if (!collapsed) {
                for (const asset of folders[folderName]) {
                    this.assetGrid.appendChild(this._buildAssetItem(asset));
                }
            }
        }
    }

    _snapshotProjectMutationContext() {
        if (!this.projectDir || !this.activeSceneId) return null;
        const projectId = this.projectDir.split(/[/\\]/).pop();
        if (!projectId) return null;
        return {
            projectId,
            projectDir: this.projectDir,
            sceneId: this.activeSceneId,
        };
    }

    _hasPendingProjectMutations() {
        return !!this._projectMutationQueue?.isBusy?.();
    }

    _deferProjectBackedRefresh(keys, reason = "project_mutation") {
        if (!this._pendingProjectRefreshKeys) {
            this._pendingProjectRefreshKeys = new Set();
        }
        for (const key of keys || []) {
            this._pendingProjectRefreshKeys.add(key);
        }
        if (!this._pendingProjectRefreshDrain) {
            this._pendingProjectRefreshDrain = true;
            this._projectMutationQueue?.drain?.(reason).then(() => this._replayDeferredProjectBackedRefresh());
        }
    }

    _replayDeferredProjectBackedRefresh() {
        if (this._destroyed || this._hasPendingProjectMutations()) return;
        const keys = this._pendingProjectRefreshKeys;
        this._pendingProjectRefreshKeys = null;
        this._pendingProjectRefreshDrain = false;
        if (!keys || keys.size === 0) return;
        if (keys.has("project")) {
            this._fetchProjectSettings({ ignoreMutationGate: true });
        }
        const wantsAssets = keys.has("assets");
        const wantsScenes = keys.has("scenes");
        if (wantsAssets && wantsScenes) {
            this._fetchAssets({ ignoreMutationGate: true }).then(() => {
                if (!this._destroyed) {
                    this._fetchScenes({ ignoreMutationGate: true, reason: "project_mutation_deferred_replay" });
                }
            });
        } else {
            if (wantsAssets) {
                this._fetchAssets({ ignoreMutationGate: true });
            }
            if (wantsScenes) {
                this._fetchScenes({ ignoreMutationGate: true, reason: "project_mutation_deferred_replay" });
            }
        }
    }

    _schedulePostMutationSceneRefresh(reason = "project_mutation") {
        if (this._destroyed) return;
        if (this._hasPendingProjectMutations() || this.isDragging || this._timelineMutationDepth > 0) {
            this._deferProjectBackedRefresh(["scenes"], reason);
            return;
        }
        this._fetchScenes({ ignoreMutationGate: true, reason });
    }

    async _drainProjectMutations(reason = "drain") {
        await this._projectMutationQueue?.drain?.(reason);
        this._replayDeferredProjectBackedRefresh();
    }

    async _runVersionedProjectMutation(path, init = {}, { projectId = "", retryOnConflict = true, maxAttempts = 2 } = {}) {
        let attempt = 0;
        while (true) {
            attempt += 1;
            try {
                return await fetchProjectJson(api.apiURL(path), init, { projectId });
            } catch (error) {
                if (
                    retryOnConflict
                    && error?.code === "project_version_conflict"
                    && attempt < maxAttempts
                ) {
                    if (error.project) {
                        rememberProjectVersionFromPayload(error.project, projectId);
                    }
                    continue;
                }
                throw error;
            }
        }
    }

    _queueProjectMutation({
        key,
        label,
        coalesce = true,
        merge = null,
        intent = null,
        run,
        refreshScenes = true,
        reconcileFromResult = null,
    }) {
        const promise = this._projectMutationQueue.enqueue({
            key,
            label,
            coalesce,
            merge,
            intent,
            run: async (queuedIntent) => run(queuedIntent),
        });
        promise.then(
            (result) => {
                if (!refreshScenes) return;
                const reconciler = typeof reconcileFromResult === "function"
                    ? reconcileFromResult
                    : (mutationResult) => this._reconcileActiveSceneFromMutation(mutationResult, { reason: label || key });
                const handled = reconciler(result) === true;
                if (!handled) this._schedulePostMutationSceneRefresh(label || key);
            },
            (error) => {
                console.warn(`[Sonder] Project mutation failed (${label || key}):`, error);
                if (refreshScenes) this._deferProjectBackedRefresh(["scenes"], `${label || key}_error`);
            }
        );
        return promise;
    }

    _runSceneMutation(operations, {
        key = "",
        label = "scene mutation",
        coalesce = true,
        merge = null,
        refreshScenes = true,
        reconcileFromResult = null,
    } = {}) {
        const context = this._snapshotProjectMutationContext();
        if (!context) return Promise.resolve(null);
        const intent = {
            ...context,
            operations: JSON.parse(JSON.stringify(operations || [])),
        };
        return this._queueProjectMutation({
            key: key || `scene:${context.sceneId}:mutation`,
            label,
            coalesce,
            merge,
            intent,
            refreshScenes,
            reconcileFromResult,
            run: async (queuedIntent) => {
                return await this._runVersionedProjectMutation(
                    `/sonder-editor/project/${encodeURIComponent(queuedIntent.projectId)}/scenes/${encodeURIComponent(queuedIntent.sceneId)}/mutations`,
                    {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ operations: queuedIntent.operations }),
                    },
                    { projectId: queuedIntent.projectId }
                );
            },
        });
    }

    _replaceSceneInList(scene) {
        if (!scene?.scene_id) return;
        const idx = this.scenes.findIndex((candidate) => candidate.scene_id === scene.scene_id);
        if (idx >= 0) {
            this.scenes[idx] = scene;
        } else {
            this.scenes.push(scene);
        }
    }

    _reconcileActiveSceneFromMutation(result, {
        reason = "mutation_reconcile",
        ignoreMutationGate = false,
        ignoreTimelineGate = false,
    } = {}) {
        const scene = result?.payload?.scene;
        if (!scene || scene.scene_id !== this.activeSceneId) {
            return false;
        }
        if (this._shouldDeferSceneRefresh({ ignoreMutationGate, ignoreTimelineGate })) {
            this._deferProjectBackedRefresh(["scenes"], reason);
            return true;
        }
        this._sceneFetchSeq += 1;
        this._pendingScenesRefresh = false;
        this._replaceSceneInList(scene);
        this._setActiveScene(scene);
        this._renderTimeline();
        this._renderViewportFrame();
        return true;
    }

    _discardLastUndo(label = "") {
        if (!this._undoStack?.length) return false;
        const entry = this._undoStack[this._undoStack.length - 1];
        if (label && entry?.label !== label) return false;
        this._undoStack.pop();
        return true;
    }

    _trimLocalLaneConfigs(configs, removedIndex, targetCount) {
        if (!Array.isArray(configs)) return [];
        if (removedIndex >= 0 && removedIndex < configs.length) {
            configs.splice(removedIndex, 1);
        }
        while (configs.length > targetCount) configs.pop();
        while (configs.length < targetCount) configs.push(this._defaultLaneConfig());
        return configs;
    }

    _compactEmptyMediaLaneLocal(laneType, laneIndex) {
        if (!this.activeScene) return false;
        laneIndex = parseInt(laneIndex, 10);
        if (!Number.isFinite(laneIndex) || laneIndex < 0) return false;
        if (laneType === "video") {
            const laneCount = Math.max(1, parseInt(this.activeScene.video_lane_count, 10) || 1);
            if (laneCount <= 1 || laneIndex >= laneCount) return false;
            if ((this.activeScene.clips || []).some((clip) => this._isRenderClip(clip) && (clip.track_index || 0) === laneIndex)) {
                return false;
            }
            for (const clip of (this.activeScene.clips || [])) {
                if (this._isRenderClip(clip) && (clip.track_index || 0) > laneIndex) {
                    clip.track_index = Math.max(0, (clip.track_index || 0) - 1);
                }
            }
            this.activeScene.video_lane_count = laneCount - 1;
            this.activeScene.video_lane_configs = this._trimLocalLaneConfigs(
                this.activeScene.video_lane_configs || [],
                laneIndex,
                this.activeScene.video_lane_count,
            );
            return true;
        }
        if (laneType === "audio") {
            const laneCount = Math.max(1, parseInt(this.activeScene.audio_lane_count, 10) || 1);
            if (laneCount <= 1 || laneIndex >= laneCount) return false;
            if ((this.activeScene.audio_tracks || []).some((track) => (track.lane_index || 0) === laneIndex)) {
                return false;
            }
            for (const track of (this.activeScene.audio_tracks || [])) {
                if ((track.lane_index || 0) > laneIndex) {
                    track.lane_index = Math.max(0, (track.lane_index || 0) - 1);
                }
            }
            this.activeScene.audio_lane_count = laneCount - 1;
            this.activeScene.audio_lane_configs = this._trimLocalLaneConfigs(
                this.activeScene.audio_lane_configs || [],
                laneIndex,
                this.activeScene.audio_lane_count,
            );
            return true;
        }
        return false;
    }

    _renderSceneAfterLocalMutation({ viewport = true } = {}) {
        this._reconcileSelection();
        this._buildTrackLayout();
        this._renderTimeline();
        if (viewport) this._renderViewportFrame();
    }

    _applyLocalSetLaneCount(laneType, count) {
        if (!this.activeScene) return;
        count = Math.max(1, parseInt(count, 10) || 1);
        if (laneType === "video") {
            this.activeScene.video_lane_count = count;
            this.activeScene.video_lane_configs = this._trimLocalLaneConfigs(this.activeScene.video_lane_configs || [], -1, count);
        } else if (laneType === "audio") {
            this.activeScene.audio_lane_count = count;
            this.activeScene.audio_lane_configs = this._trimLocalLaneConfigs(this.activeScene.audio_lane_configs || [], -1, count);
        } else if (laneType === "motion_driver") {
            this.activeScene.motion_driver_lane_count = count;
            this.activeScene.motion_driver_lane_configs = this._trimLocalLaneConfigs(this.activeScene.motion_driver_lane_configs || [], -1, count);
        }
    }

    _applyLocalRemoveLane(laneType, laneIndex, itemPolicy = "require_empty", targetLane = null) {
        if (!this.activeScene || !["video", "audio"].includes(laneType)) return false;
        laneIndex = parseInt(laneIndex, 10);
        if (!Number.isFinite(laneIndex)) return false;
        const isVideo = laneType === "video";
        const currentCount = isVideo
            ? Math.max(1, parseInt(this.activeScene.video_lane_count, 10) || 1)
            : Math.max(1, parseInt(this.activeScene.audio_lane_count, 10) || 1);
        if (currentCount <= 1 || laneIndex < 0 || laneIndex >= currentCount) return false;
        const laneItems = isVideo
            ? (this.activeScene.clips || []).filter((clip) => this._isRenderClip(clip) && (clip.track_index || 0) === laneIndex)
            : (this.activeScene.audio_tracks || []).filter((track) => (track.lane_index || 0) === laneIndex);
        if (laneItems.length && itemPolicy === "require_empty") return false;
        if (laneItems.length && itemPolicy === "move_items") {
            const nextTarget = targetLane == null ? (laneIndex > 0 ? laneIndex - 1 : 1) : parseInt(targetLane, 10);
            if (!Number.isFinite(nextTarget) || nextTarget < 0 || nextTarget >= currentCount || nextTarget === laneIndex) return false;
            for (const item of laneItems) {
                if (isVideo) item.track_index = nextTarget;
                else item.lane_index = nextTarget;
            }
        } else if (laneItems.length && itemPolicy === "delete_items") {
            if (isVideo) {
                const deleting = new Set(laneItems.map((item) => item.clip_id));
                this.activeScene.clips = (this.activeScene.clips || []).filter((clip) => !deleting.has(clip.clip_id));
            } else {
                const deleting = new Set(laneItems.map((item) => item.track_id));
                this.activeScene.audio_tracks = (this.activeScene.audio_tracks || []).filter((track) => !deleting.has(track.track_id));
            }
        } else if (laneItems.length) {
            return false;
        }

        if (isVideo) {
            for (const clip of (this.activeScene.clips || [])) {
                if (this._isRenderClip(clip) && (clip.track_index || 0) > laneIndex) {
                    clip.track_index = Math.max(0, (clip.track_index || 0) - 1);
                }
            }
            this.activeScene.video_lane_count = currentCount - 1;
            this.activeScene.video_lane_configs = this._trimLocalLaneConfigs(
                this.activeScene.video_lane_configs || [],
                laneIndex,
                this.activeScene.video_lane_count,
            );
        } else {
            for (const track of (this.activeScene.audio_tracks || [])) {
                if ((track.lane_index || 0) > laneIndex) {
                    track.lane_index = Math.max(0, (track.lane_index || 0) - 1);
                }
            }
            this.activeScene.audio_lane_count = currentCount - 1;
            this.activeScene.audio_lane_configs = this._trimLocalLaneConfigs(
                this.activeScene.audio_lane_configs || [],
                laneIndex,
                this.activeScene.audio_lane_count,
            );
        }
        return true;
    }

    _remapSelectedItem(type, oldId, newId, data) {
        const update = (item) => {
            if (item?.type !== type || item.id !== oldId) return item;
            return { type, id: newId, data };
        };
        this.selectedItems = (this.selectedItems || []).map(update);
        if (this.selectedItem?.type === type && this.selectedItem.id === oldId) {
            this.selectedItem = { type, id: newId, data };
        }
    }

    _applyLocalMoveGuide(oldFrame, newFrame, guideData, fields = {}) {
        if (!this.activeScene) return null;
        oldFrame = parseInt(oldFrame, 10);
        newFrame = parseInt(newFrame, 10);
        if (!Number.isFinite(oldFrame) || !Number.isFinite(newFrame)) return null;
        const moved = {
            ...(guideData || {}),
            frame_index: newFrame,
            asset_id: fields.asset_id ?? guideData?.asset_id ?? "",
            source: fields.source ?? guideData?.source ?? "asset",
            strength: fields.strength ?? guideData?.strength ?? 1.0,
            muted: fields.muted ?? guideData?.muted ?? false,
        };
        delete moved._previewFrameIndex;
        this.activeScene.guide_frames = (this.activeScene.guide_frames || [])
            .filter((guide) => guide.frame_index !== oldFrame && guide.frame_index !== newFrame);
        this.activeScene.guide_frames.push(moved);
        this.activeScene.guide_frames.sort((a, b) => (a.frame_index || 0) - (b.frame_index || 0));
        this._remapSelectedItem("guide", oldFrame, newFrame, moved);
        return moved;
    }

    _applyLocalCreateGuide(fields = {}) {
        if (!this.activeScene) return null;
        const frameIndex = parseInt(fields.frame_index, 10);
        if (!Number.isFinite(frameIndex)) return null;
        const guide = {
            frame_index: frameIndex,
            asset_id: fields.asset_id || "",
            source: fields.source || "asset",
            strength: fields.strength ?? this._defaultGuideStrength(),
            muted: !!fields.muted,
        };
        this.activeScene.guide_frames = (this.activeScene.guide_frames || [])
            .filter((current) => current.frame_index !== frameIndex);
        this.activeScene.guide_frames.push(guide);
        this.activeScene.guide_frames.sort((a, b) => (a.frame_index || 0) - (b.frame_index || 0));
        return guide;
    }

    _applyLocalPromptCreate(fields = {}) {
        if (!this.activeScene) return null;
        const section = {
            start_frame: parseInt(fields.start_frame, 10) || 0,
            end_frame: parseInt(fields.end_frame, 10) || 0,
            prompt: String(fields.prompt || ""),
        };
        this.activeScene.prompt_sections = this.activeScene.prompt_sections || [];
        this.activeScene.prompt_sections.push(section);
        this.activeScene.prompt_sections.sort((a, b) => (a.start_frame || 0) - (b.start_frame || 0));
        return section;
    }

    _applyLocalPromptUpdate(index, fields = {}) {
        if (!this.activeScene) return null;
        const section = (this.activeScene.prompt_sections || [])[index];
        if (!section) return null;
        Object.assign(section, fields);
        this.activeScene.prompt_sections.sort((a, b) => (a.start_frame || 0) - (b.start_frame || 0));
        return section;
    }

    _applyLocalPromptDelete(index) {
        if (!this.activeScene || !Array.isArray(this.activeScene.prompt_sections)) return false;
        if (index < 0 || index >= this.activeScene.prompt_sections.length) return false;
        this.activeScene.prompt_sections.splice(index, 1);
        if (this._selectedPromptIdx === index) this._selectedPromptIdx = null;
        return true;
    }

    _applyLocalBulkDeleteItems(items = [], { preserveLanes = false } = {}) {
        if (!this.activeScene || !Array.isArray(items)) return false;
        const videoLanes = [];
        const audioLanes = [];
        const guideFrames = new Set();
        const promptIndexes = [];
        const clipIds = new Set();
        const audioIds = new Set();

        for (const item of items) {
            if (!item || typeof item !== "object") continue;
            const preserveLane = !!(item.preserve_lane || preserveLanes);
            if (item.type === "clip") {
                const clip = (this.activeScene.clips || []).find((candidate) => candidate.clip_id === item.id);
                if (!clip) continue;
                clipIds.add(clip.clip_id);
                if (!preserveLane) videoLanes.push(parseInt(clip.track_index, 10) || 0);
            } else if (item.type === "audio") {
                const track = (this.activeScene.audio_tracks || []).find((candidate) => candidate.track_id === item.id);
                if (!track) continue;
                audioIds.add(track.track_id);
                if (!preserveLane) audioLanes.push(parseInt(track.lane_index, 10) || 0);
            } else if (item.type === "guide") {
                const frameIndex = parseInt(item.id, 10);
                if (Number.isFinite(frameIndex)) guideFrames.add(frameIndex);
            } else if (item.type === "prompt") {
                const idx = parseInt(item.id, 10);
                if (Number.isFinite(idx)) promptIndexes.push(idx);
            }
        }

        if (clipIds.size) {
            this.activeScene.clips = (this.activeScene.clips || []).filter((clip) => !clipIds.has(clip.clip_id));
        }
        if (audioIds.size) {
            this.activeScene.audio_tracks = (this.activeScene.audio_tracks || []).filter((track) => !audioIds.has(track.track_id));
        }
        if (guideFrames.size) {
            this.activeScene.guide_frames = (this.activeScene.guide_frames || [])
                .filter((guide) => !guideFrames.has(parseInt(guide.frame_index, 10)));
        }
        for (const idx of Array.from(new Set(promptIndexes)).sort((a, b) => b - a)) {
            if (idx >= 0 && idx < (this.activeScene.prompt_sections || []).length) {
                this.activeScene.prompt_sections.splice(idx, 1);
            }
        }
        for (const lane of Array.from(new Set(videoLanes)).sort((a, b) => b - a)) {
            this._compactEmptyMediaLaneLocal("video", lane);
        }
        for (const lane of Array.from(new Set(audioLanes)).sort((a, b) => b - a)) {
            this._compactEmptyMediaLaneLocal("audio", lane);
        }
        return true;
    }

    _buildAssetItem(asset) {
        {
            const item = document.createElement("div");
            item.style.cssText = `
                width: 80px; background: ${COLORS.galleryItem};
                border: 1px solid ${COLORS.galleryItemBorder};
                border-radius: 4px; overflow: hidden; cursor: grab;
                transition: background 0.15s;
            `;
            item.addEventListener("mouseenter", () => item.style.background = COLORS.galleryItemHover);
            item.addEventListener("mouseleave", () => item.style.background = COLORS.galleryItem);

            // Thumbnail
            const thumb = document.createElement("div");
            thumb.style.cssText = `
                width: 80px; height: 52px; background: #111;
                display: flex; align-items: center; justify-content: center;
                overflow: hidden;
            `;

            if (asset.has_thumbnail) {
                const dirName = this.projectDir.split(/[/\\]/).pop();
                const img = document.createElement("img");
                img.src = api.apiURL(`/sonder-editor/project/${dirName}/thumbnail/${asset.asset_id}`);
                img.style.cssText = "width: 100%; height: 100%; object-fit: cover;";
                img.draggable = false; // Prevent browser from dragging thumbnail URL
                thumb.appendChild(img);
            } else {
                const icon = document.createElement("span");
                icon.style.cssText = `font-size: 20px; color: ${COLORS.textDim};`;
                icon.textContent = this.selectedAssetType === "video" ? "🎬" :
                                   this.selectedAssetType === "image" ? "🖼️" : "🔊";
                thumb.appendChild(icon);
            }

            // Label
            const label = document.createElement("div");
            label.style.cssText = `
                padding: 2px 4px; font-size: 9px; color: ${COLORS.galleryText};
                overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
                text-align: center;
            `;
            label.textContent = asset.name || "Untitled";
            label.title = asset.name;

            // Metadata line
            const meta = document.createElement("div");
            meta.style.cssText = `
                padding: 0 4px 2px; font-size: 8px; color: ${COLORS.textDim};
                text-align: center;
            `;
            if (asset.asset_type === "video") {
                meta.textContent = `${asset.frame_count}f ${asset.width}×${asset.height}`;
            } else if (asset.asset_type === "image") {
                meta.textContent = `${asset.width}×${asset.height}`;
            } else {
                meta.textContent = `${asset.duration_sec?.toFixed(1) || "?"}s`;
            }

            // Click to preview
            item.addEventListener("click", () => this._previewAsset(asset));

            // Right-click context menu
            item.addEventListener("contextmenu", (e) => {
                e.preventDefault();
                e.stopPropagation();
                this._showContextMenu(e.clientX, e.clientY, [
                    { label: "Rename", action: () => this._startAssetRename(asset, label) },
                ]);
            });

            // Drag support
            item.draggable = true;
            item.addEventListener("dragstart", (e) => {
                // Stash projectDir so the global ComfyUI drop handler can build URLs
                const enrichedAsset = { ...asset, _projectDir: this.projectDir.split(/[/\\]/).pop() };
                e.dataTransfer.setData("application/x-sonder-asset", JSON.stringify(enrichedAsset));
                e.dataTransfer.effectAllowed = "copy";
            });

            item.append(thumb, label, meta);
            return item;
        }
    }

    _previewAsset(asset) {
        // Remove existing preview
        if (this._previewEl) {
            this._previewEl.remove();
            this._previewEl = null;
        }

        const preview = document.createElement("div");
        preview.style.cssText = `
            position: relative; width: 100%; background: #111;
            border: 1px solid #444; border-radius: 4px; margin-top: 4px;
            overflow: hidden;
        `;

        // Close button
        const closeBtn = document.createElement("button");
        closeBtn.textContent = "✕";
        closeBtn.style.cssText = `
            position: absolute; top: 4px; right: 4px; z-index: 10;
            background: rgba(0,0,0,0.6); border: none; color: #fff;
            cursor: pointer; padding: 2px 6px; border-radius: 3px; font-size: 12px;
        `;
        closeBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            preview.remove();
            this._previewEl = null;
        });
        preview.appendChild(closeBtn);

        // Info bar
        const info = document.createElement("div");
        info.style.cssText = `
            padding: 4px 8px; background: #1a1a1a; font-size: 10px;
            color: ${COLORS.textDim}; border-bottom: 1px solid #333;
        `;
        const parts = [asset.name];
        if (asset.asset_type === "video") {
            parts.push(`${asset.frame_count}f @ ${asset.fps?.toFixed(1)}fps`);
            parts.push(`${asset.width}×${asset.height}`);
            parts.push(`${asset.duration_sec?.toFixed(1)}s`);
        } else if (asset.asset_type === "image") {
            parts.push(`${asset.width}×${asset.height}`);
        } else if (asset.asset_type === "audio") {
            parts.push(`${asset.duration_sec?.toFixed(1)}s`);
        }
        if (asset.prompt) parts.push(`Prompt: ${asset.prompt}`);
        info.textContent = parts.join("  |  ");
        preview.appendChild(info);

        const dirName = this.projectDir.split(/[/\\]/).pop();

        // asset.path is like "media/filename.mp4"
        // ComfyUI /view needs: filename=filename.mp4&subfolder=sonder-projects/DirName/media&type=output
        const assetFileName = asset.path.split(/[/\\]/).pop();
        const assetSubfolder = `sonder-projects/${dirName}/${asset.path.split(/[/\\]/).slice(0, -1).join("/")}`;
        const viewParams = `filename=${encodeURIComponent(assetFileName)}&subfolder=${encodeURIComponent(assetSubfolder)}&type=output`;

        if (asset.asset_type === "video") {
            const video = document.createElement("video");
            video.controls = true;
            video.loop = true;
            video.muted = true;
            video.autoplay = true;
            video.style.cssText = "width: 100%; max-height: 200px; display: block;";
            video.src = api.apiURL(`/view?${viewParams}`);
            preview.appendChild(video);
        } else if (asset.asset_type === "image") {
            const img = document.createElement("img");
            img.style.cssText = "width: 100%; max-height: 200px; object-fit: contain; display: block;";
            img.src = api.apiURL(`/view?${viewParams}`);
            preview.appendChild(img);
        } else if (asset.asset_type === "audio") {
            const audio = document.createElement("audio");
            audio.controls = true;
            audio.style.cssText = "width: 100%; display: block; padding: 8px;";
            audio.src = api.apiURL(`/view?${viewParams}`);
            preview.appendChild(audio);
        }

        // Insert preview above the asset grid
        this.assetGrid.parentElement.insertBefore(preview, this.assetGrid);
        this._previewEl = preview;
    }

    // ── Asset Rename ─────────────────────────────────────────────────
    _startAssetRename(asset, labelEl) {
        const original = asset.name || "Untitled";
        const input = document.createElement("input");
        input.type = "text";
        input.value = original;
        input.style.cssText = `
            font-size: 9px; width: 100%; text-align: center;
            background: #333; color: #ddd; border: 1px solid #666;
            outline: none; padding: 1px 4px; box-sizing: border-box;
        `;
        labelEl.textContent = "";
        labelEl.appendChild(input);
        input.select();
        input.focus();

        const commit = () => {
            const newName = input.value.trim();
            labelEl.textContent = newName || original;
            if (newName && newName !== original) {
                this._renameAsset(asset, newName);
            }
        };
        input.addEventListener("blur", commit);
        input.addEventListener("keydown", (e) => {
            e.stopPropagation();
            if (e.key === "Enter") { input.removeEventListener("blur", commit); commit(); input.blur(); }
            if (e.key === "Escape") { input.removeEventListener("blur", commit); labelEl.textContent = original; input.blur(); }
        });
    }

    async _renameAsset(asset, newName) {
        const dirName = this.projectDir.split(/[/\\]/).pop();
        try {
            await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/${asset.asset_id}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name: newName }),
            });
            await this._fetchAssets();
        } catch (e) {
            console.warn("[Sonder] Failed to rename asset:", e);
        }
    }

    // ── FPS & Timecode Helpers ───────────────────────────────────────
    get _effectiveFps() {
        const sceneFps = this.activeScene?.fps || 0;
        return sceneFps > 0 ? sceneFps : (this.fps || 24);
    }

    get _effectiveSceneWidth() {
        const sceneWidth = this.activeScene?.width || 0;
        return sceneWidth > 0 ? sceneWidth : (this.sceneWidth || 768);
    }

    get _effectiveSceneHeight() {
        const sceneHeight = this.activeScene?.height || 0;
        return sceneHeight > 0 ? sceneHeight : (this.sceneHeight || 512);
    }

    _framesToSeconds(frames) { return frames / this._effectiveFps; }
    _secondsToFrames(seconds) { return Math.round(seconds * this._effectiveFps); }
    _formatPositionInput(frame) { return this._timecodeMode === "timecode" ? this._framesToSeconds(frame).toFixed(2) : String(frame); }
    _parsePositionInput(value) {
        if (this._timecodeMode === "timecode") {
            const seconds = parseFloat(value);
            return Number.isFinite(seconds) ? this._secondsToFrames(seconds) : NaN;
        }
        const frames = parseInt(value, 10);
        return Number.isFinite(frames) ? frames : NaN;
    }

    _updateViewportHeader() {
        if (!this._vpHeaderText) return;
        const template = this._getActiveTemplate();
        const templateSuffix = template.id !== "free" ? ` [${template.name}]` : "";
        this._vpHeaderText.textContent = `${this._effectiveSceneWidth}x${this._effectiveSceneHeight} @ ${this._effectiveFps}fps${templateSuffix}`;
    }

    _formatSceneFpsValue(fps) {
        const numeric = Number(fps);
        if (!Number.isFinite(numeric) || numeric <= 0) return "";
        return String(Number(numeric.toFixed(3)));
    }

    _syncSceneFpsControl() {
        if (!this._fpsInput) return;
        this._fpsInput.value = this._formatSceneFpsValue(this.activeScene?.fps);
        this._fpsInput.placeholder = String(this.fps);
    }

    _snapSceneFpsToTemplate(fps, template = this._getActiveTemplate()) {
        const numeric = Math.max(0, Number(fps) || 0);
        const constraint = template?.constraints?.fps;
        if (template?.id === "free" || !constraint || typeof constraint !== "object") {
            return numeric;
        }
        return Number(snapToConstraint(numeric, constraint).toFixed(3));
    }

    async _applyActiveTemplateFpsConstraint() {
        if (!this.activeScene) return;
        const template = this._getActiveTemplate();
        const constraint = template?.constraints?.fps;
        if (template.id === "free" || !constraint || typeof constraint !== "object") return;

        const currentFps = this._effectiveFps;
        const nextFps = this._snapSceneFpsToTemplate(currentFps, template);
        const fixedFps = constraint.min != null && constraint.max != null && constraint.min === constraint.max;
        const changedByConstraint = Math.abs(nextFps - currentFps) > 0.0005;
        if (!fixedFps && !changedByConstraint) return;

        const sceneFps = Number(this.activeScene?.fps) || 0;
        if (Math.abs(sceneFps - nextFps) <= 0.0005) {
            this._syncSceneFpsControl();
            return;
        }
        await this._updateSceneFps(nextFps);
    }

    _snapSceneDurationToTemplate(frames) {
        const numeric = Math.max(1, parseInt(frames, 10) || 1);
        const frameConstraint = this._getActiveTemplate()?.constraints?.frames;
        if (!frameConstraint) return numeric;
        const durationConstraint = { ...frameConstraint };
        delete durationConstraint.max;
        return Math.max(1, Math.round(snapToConstraint(numeric, durationConstraint)));
    }

    _snapPreContextFrames(value) {
        // Pre context snaps UP to next G value (= step*k + offset, k>=0). 0 stays 0
        // because pre=0 means no pre context — the +1 then lives in the selection
        // via the existing _snapSelectionFrame path (selection endpoint snap).
        const numeric = Math.max(0, parseInt(value, 10) || 0);
        if (numeric <= 0) return 0;
        const constraint = this._getActiveTemplate()?.constraints?.frames;
        const step = constraint?.step;
        if (!step || step <= 1) return numeric;
        const offset = constraint.offset || 0;
        if (numeric <= offset) return offset;
        const k = (numeric - offset) / step;
        return offset + Math.ceil(k) * step;
    }

    _snapPostContextFrames(value) {
        // Post context snaps UP to next multiple of step. Post never carries the
        // +1 offset (the leading single frame lives at the start of the tensor,
        // not the tail).
        const numeric = Math.max(0, parseInt(value, 10) || 0);
        if (numeric <= 0) return 0;
        const constraint = this._getActiveTemplate()?.constraints?.frames;
        const step = constraint?.step;
        if (!step || step <= 1) return numeric;
        return Math.ceil(numeric / step) * step;
    }

    _snapMaskOffset(value, cap) {
        // Mask offset snaps UP to the next multiple of step within [0, cap]. When
        // value >= cap, returns cap (the "full mask" option — needed on the pre
        // side because actual_pre = G value isn't itself a multiple of step; on
        // the post side cap is already a multiple of step so this collapses to
        // the same multiples-of-step rule).
        const numeric = Math.max(0, parseInt(value, 10) || 0);
        const capValue = Math.max(0, parseInt(cap, 10) || 0);
        if (capValue <= 0 || numeric <= 0) return 0;
        if (numeric >= capValue) return capValue;
        const constraint = this._getActiveTemplate()?.constraints?.frames;
        const step = constraint?.step;
        if (!step || step <= 1) return Math.min(numeric, capValue);
        const snapped = Math.ceil(numeric / step) * step;
        return snapped <= capValue ? snapped : capValue;
    }

    _snapSelectionFrame(value, { direction = "up", clampMax = null } = {}) {
        const numeric = Math.max(0, Math.round(Number(value) || 0));
        // Frame 0 is always a valid selection endpoint; template constraint snapping must never push it above 0.
        if (numeric <= 0) return 0;
        const constraint = this._getActiveTemplate()?.constraints?.frames;
        if (!constraint?.step) return clampMax != null ? Math.min(numeric, Math.max(0, clampMax)) : numeric;
        const step = constraint.step;
        const offset = constraint.offset || 0;
        const k = (numeric - offset) / step;
        const rounded = direction === "up" ? Math.ceil(k) : Math.floor(k);
        let snapped = rounded * step + offset;
        if (clampMax != null && snapped > clampMax) {
            snapped = Math.floor((clampMax - offset) / step) * step + offset;
        }
        return Math.max(0, snapped);
    }

    _getActiveTemplate() {
        return getTemplateById(this._templateId, this._settings);
    }

    _aspectRatioOptionValue(a, b) {
        return `${a},${b}`;
    }

    _parseAspectRatioValue(value) {
        const [a, b] = String(value || "").split(",").map(Number);
        return {
            a: Number.isFinite(a) ? a : 0,
            b: Number.isFinite(b) ? b : 0,
        };
    }

    _readSelectedAspectRatio() {
        return this._parseAspectRatioValue(this._aspectRatioSelect?.value);
    }

    _readSelectedResolutionTier() {
        const value = this._resTierSelect?.value;
        if (!value || value === "custom") return null;
        const numeric = Number(value);
        return Number.isFinite(numeric) && numeric > 0 ? numeric : null;
    }

    _isCustomTierSelected() {
        return !this._readSelectedResolutionTier();
    }

    _isFreeAspectSelected() {
        const { a, b } = this._readSelectedAspectRatio();
        return !(a > 0 && b > 0);
    }

    _resolutionControlMode() {
        const hasAspectPreset = !this._isFreeAspectSelected();
        const hasResolutionTier = !this._isCustomTierSelected();
        if (hasAspectPreset && hasResolutionTier) return "locked";
        if (hasAspectPreset) return "aspect-custom";
        if (hasResolutionTier) return "tier-custom-aspect";
        return "free-custom";
    }

    _matchesAspectRatio(width, height, a, b, tolerance = 0.01) {
        if (!(width > 0 && height > 0 && a > 0 && b > 0)) return false;
        const actualRatio = width / height;
        const targetRatio = a / b;
        return Math.abs(actualRatio - targetRatio) / targetRatio <= tolerance;
    }

    _findMatchingAspectPreset(width, height, tolerance = 0.03) {
        return ASPECT_RATIO_PRESETS.find(
            (preset) => preset.a > 0 && preset.b > 0 && this._matchesAspectRatio(width, height, preset.a, preset.b, tolerance)
        ) || null;
    }

    _findNearestResolutionTier(width, height, tolerance = 0.10) {
        if (!(width > 0 && height > 0)) return null;
        const c = Math.sqrt(width * height);
        let bestTier = null;
        let bestDiff = Infinity;
        for (const tier of RESOLUTION_TIERS) {
            const diff = Math.abs(tier.c - c) / tier.c;
            if (diff < bestDiff) {
                bestTier = tier;
                bestDiff = diff;
            }
        }
        return bestDiff <= tolerance ? bestTier : null;
    }

    _clearCustomAspectRatioOption() {
        if (!this._aspectRatioSelect) return;
        for (const option of Array.from(this._aspectRatioSelect.options)) {
            if (option.dataset.sonderCustomAspect === "true") {
                option.remove();
            }
        }
        this._customAspectRatioValue = "";
    }

    _ensureCustomAspectRatioOption(a, b, label = `Custom ${a}:${b}`) {
        if (!this._aspectRatioSelect) return "";
        const value = this._aspectRatioOptionValue(a, b);
        this._clearCustomAspectRatioOption();
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        option.dataset.sonderCustomAspect = "true";
        const freeOption = Array.from(this._aspectRatioSelect.options).find(
            (entry) => entry.value === this._aspectRatioOptionValue(0, 0)
        );
        if (freeOption?.parentNode) {
            freeOption.parentNode.insertBefore(option, freeOption);
        } else {
            this._aspectRatioSelect.appendChild(option);
        }
        this._customAspectRatioValue = value;
        this._aspectRatioSelect.value = value;
        return value;
    }

    _selectAspectRatioForDimensions(width, height, { preferExistingCustom = false } = {}) {
        if (!this._aspectRatioSelect) return;
        const preset = this._findMatchingAspectPreset(width, height);
        if (preset) {
            this._clearCustomAspectRatioOption();
            this._aspectRatioSelect.value = this._aspectRatioOptionValue(preset.a, preset.b);
            return;
        }
        if (preferExistingCustom && this._customAspectRatioValue) {
            const { a, b } = this._parseAspectRatioValue(this._customAspectRatioValue);
            if (this._matchesAspectRatio(width, height, a, b)) {
                this._aspectRatioSelect.value = this._customAspectRatioValue;
                return;
            }
        }
        this._clearCustomAspectRatioOption();
        this._aspectRatioSelect.value = this._aspectRatioOptionValue(0, 0);
    }

    _setResolutionInputs(width, height) {
        if (this._resWInput) this._resWInput.value = width || "";
        if (this._resHInput) this._resHInput.value = height || "";
    }

    _readResolutionInputs() {
        return {
            width: Math.max(0, parseInt(this._resWInput?.value, 10) || 0),
            height: Math.max(0, parseInt(this._resHInput?.value, 10) || 0),
        };
    }

    _resetFreeAspectTierDraft() {
        this._freeAspectTierDraft = { width: false, height: false };
    }

    _markFreeAspectTierDraft(axis) {
        if (axis === "w") this._freeAspectTierDraft.width = true;
        if (axis === "h") this._freeAspectTierDraft.height = true;
    }

    _resolveAspectCustomResolution(axis = this._resolutionEditAxis || "w") {
        const { a, b } = this._readSelectedAspectRatio();
        if (!(a > 0 && b > 0)) return null;
        let { width, height } = this._readResolutionInputs();
        const template = this._getActiveTemplate();
        if (axis === "h" && height > 0) {
            if (template.id !== "free") {
                height = Math.round(snapToConstraint(height, template?.constraints?.height));
            }
            width = Math.max(1, Math.round(height * a / b));
            if (template.id !== "free") {
                width = Math.round(snapToConstraint(width, template?.constraints?.width));
            }
        } else if (width > 0) {
            if (template.id !== "free") {
                width = Math.round(snapToConstraint(width, template?.constraints?.width));
            }
            height = Math.max(1, Math.round(width * b / a));
            if (template.id !== "free") {
                height = Math.round(snapToConstraint(height, template?.constraints?.height));
            }
        } else if (height > 0) {
            if (template.id !== "free") {
                height = Math.round(snapToConstraint(height, template?.constraints?.height));
            }
            width = Math.max(1, Math.round(height * a / b));
            if (template.id !== "free") {
                width = Math.round(snapToConstraint(width, template?.constraints?.width));
            }
        } else {
            return null;
        }
        return { width, height };
    }

    _resolveFreeAspectTierResolution({ requireDraftComplete = false } = {}) {
        const tier = this._readSelectedResolutionTier();
        const { width, height } = this._readResolutionInputs();
        if (!(tier && width > 0 && height > 0)) return null;
        if (requireDraftComplete && !(this._freeAspectTierDraft.width && this._freeAspectTierDraft.height)) {
            return null;
        }
        return computeResolutionFromTier(tier, width, height, this._getActiveTemplate());
    }

    _rebuildTemplateOptions() {
        if (!this._templateSelect) return;
        const resolvedTemplate = getTemplateById(this._templateId, this._settings);
        this._templateId = resolvedTemplate.id;
        this._templateSelect.innerHTML = "";
        for (const template of getAllModelTemplates(this._settings)) {
            const option = document.createElement("option");
            option.value = template.id;
            option.textContent = template.name;
            this._templateSelect.appendChild(option);
        }
        this._templateSelect.value = this._templateId;
    }

    _rebuildResolutionTierOptions(selectedValue = this._resTierSelect?.value || "custom") {
        if (!this._resTierSelect) return;
        const template = this._getActiveTemplate();
        this._resTierSelect.innerHTML = "";
        for (const tier of RESOLUTION_TIERS) {
            const option = document.createElement("option");
            option.value = String(tier.c);
            option.textContent = tier.label + (template.hintTier === tier.c ? " (default)" : "");
            this._resTierSelect.appendChild(option);
        }
        const customOption = document.createElement("option");
        customOption.value = "custom";
        customOption.textContent = "Custom";
        this._resTierSelect.appendChild(customOption);
        this._resTierSelect.value = Array.from(this._resTierSelect.options).some((option) => option.value === String(selectedValue))
            ? String(selectedValue)
            : "custom";
    }

    _applyTemplateConstraintMetadata() {
        const template = this._getActiveTemplate();
        const widthConstraint = template?.constraints?.width || null;
        const heightConstraint = template?.constraints?.height || null;
        const frameConstraint = template?.constraints?.frames || null;
        const fpsConstraint = template?.constraints?.fps || null;
        if (this._resWInput) {
            this._resWInput.step = String(widthConstraint?.step || 1);
            this._resWInput.min = String(widthConstraint?.min ?? 0);
            this._resWInput.max = String(widthConstraint?.max ?? 8192);
        }
        if (this._resHInput) {
            this._resHInput.step = String(heightConstraint?.step || 1);
            this._resHInput.min = String(heightConstraint?.min ?? 0);
            this._resHInput.max = String(heightConstraint?.max ?? 8192);
        }
        if (this.durationInput) {
            this.durationInput.step = String(frameConstraint?.step || 1);
            this.durationInput.min = String(frameConstraint?.min ?? 1);
        }
        if (this._fpsInput) {
            this._fpsInput.min = String(fpsConstraint?.min ?? 0);
            this._fpsInput.max = String(fpsConstraint?.max ?? 240);
            this._fpsInput.step = String(fpsConstraint?.step || 0.001);
        }
    }

    _updateResolutionInputMode() {
        const readOnly = this._resolutionControlMode() === "locked";
        for (const input of [this._resWInput, this._resHInput]) {
            if (!input) continue;
            input.readOnly = readOnly;
            input.style.opacity = readOnly ? "0.68" : "1";
            input.style.background = readOnly ? COLORS.panelMuted : COLORS.panelRaised;
            input.style.cursor = readOnly ? "default" : "text";
        }
    }

    _resolveFrameConstraintForTemplate(templateId) {
        return resolveFrameConstraintForTemplate(templateId, this._settings);
    }

    static _frameConstraintsEqual(a, b) {
        return frameConstraintsEqual(a, b);
    }

    async _updateProjectTemplateId(templateId) {
        if (!this.projectDir) return true;
        const dirName = this._projectDirName();
        const frameConstraint = this._resolveFrameConstraintForTemplate(templateId);
        try {
            await this._runVersionedProjectMutation(
                `/sonder-editor/project/${encodeURIComponent(dirName)}`,
                {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ template_id: templateId, frame_constraint: frameConstraint }),
                },
                { projectId: dirName }
            );
            return true;
        } catch (error) {
            console.warn("[Sonder] Failed to update project template:", error);
            return false;
        }
    }

    async _handleTemplateSelectionChange() {
        const nextTemplateId = getTemplateById(this._templateSelect?.value, this._settings).id;
        if (!nextTemplateId || nextTemplateId === this._templateId) {
            this._rebuildTemplateOptions();
            return;
        }
        if (!(await this._updateProjectTemplateId(nextTemplateId))) {
            this._rebuildTemplateOptions();
            return;
        }
        this._templateId = nextTemplateId;
        this._rebuildTemplateOptions();
        this._rebuildResolutionTierOptions();
        this._applyTemplateConstraintMetadata();
        this._resetFreeAspectTierDraft();
        this._syncSceneResolutionControls({ detectSelections: false });
        if (this._resolutionControlMode() === "free-custom") {
            const template = this._getActiveTemplate();
            const width = parseInt(this._resWInput?.value, 10) || this.activeScene?.width || 0;
            const height = parseInt(this._resHInput?.value, 10) || this.activeScene?.height || 0;
            const nextResolution = template.id === "free"
                ? { width, height }
                : snapResolution(width, height, template);
            this._setResolutionInputs(nextResolution.width, nextResolution.height);
            await this._updateSceneResolution(nextResolution.width, nextResolution.height, { detectSelections: false });
        } else {
            await this._recalculateResolution();
        }
        await this._applyActiveTemplateFpsConstraint();
        this._updateViewportHeader();
    }

    async _recalculateResolution() {
        this._resetFreeAspectTierDraft();
        this._updateResolutionInputMode();
        const mode = this._resolutionControlMode();
        let resolution = null;
        if (mode === "locked") {
            const { a, b } = this._readSelectedAspectRatio();
            const tier = this._readSelectedResolutionTier();
            if (!(a > 0 && b > 0 && tier)) return;
            resolution = computeResolutionFromTier(tier, a, b, this._getActiveTemplate());
        } else if (mode === "aspect-custom") {
            resolution = this._resolveAspectCustomResolution(this._resolutionEditAxis || "w");
        } else if (mode === "tier-custom-aspect") {
            resolution = this._resolveFreeAspectTierResolution();
        } else {
            return;
        }
        if (!resolution) return;
        this._setResolutionInputs(resolution.width, resolution.height);
        await this._updateSceneResolution(resolution.width, resolution.height, { detectSelections: false });
    }

    _setSceneAspectRatioFromDimensions(width, height) {
        width = Math.max(0, parseInt(width, 10) || 0);
        height = Math.max(0, parseInt(height, 10) || 0);
        if (!(width > 0 && height > 0) || !this._aspectRatioSelect) return;
        const gcd = (a, b) => {
            let x = Math.abs(a);
            let y = Math.abs(b);
            while (y) {
                const next = x % y;
                x = y;
                y = next;
            }
            return x || 1;
        };
        const divisor = gcd(width, height);
        const a = Math.max(1, Math.round(width / divisor));
        const b = Math.max(1, Math.round(height / divisor));
        const preset = this._findMatchingAspectPreset(width, height);
        if (preset) {
            this._clearCustomAspectRatioOption();
            this._aspectRatioSelect.value = this._aspectRatioOptionValue(preset.a, preset.b);
        } else {
            this._ensureCustomAspectRatioOption(a, b, `Custom ${a}:${b}`);
        }
        this._resetFreeAspectTierDraft();
        this._updateResolutionInputMode();
        this._recalculateResolution();
    }

    _syncSceneResolutionControls({ detectSelections = true } = {}) {
        this._resetFreeAspectTierDraft();
        const width = this.activeScene?.width || 0;
        const height = this.activeScene?.height || 0;
        if (this._resWInput) {
            this._resWInput.value = width || "";
            this._resWInput.placeholder = String(this.sceneWidth || 768);
        }
        if (this._resHInput) {
            this._resHInput.value = height || "";
            this._resHInput.placeholder = String(this.sceneHeight || 512);
        }
        this._rebuildTemplateOptions();
        this._rebuildResolutionTierOptions();
        this._applyTemplateConstraintMetadata();
        if (detectSelections) {
            this._selectAspectRatioForDimensions(width, height, { preferExistingCustom: true });
            const tier = this._findNearestResolutionTier(width, height);
            if (this._resTierSelect) {
                this._resTierSelect.value = tier ? String(tier.c) : "custom";
            }
        }
        this._updateResolutionInputMode();
    }

    _contextFrameValue(name) {
        return Math.max(0, parseInt(this._getWidgetValue(name, 0), 10) || 0);
    }

    _refreshContextInputs() {
        if (this._preContextInput) {
            this._preContextInput.value = this._contextFrameValue("pre_context_frames");
        }
        if (this._postContextInput) {
            this._postContextInput.value = this._contextFrameValue("post_context_frames");
        }
        if (this._maskPreOffsetInput) {
            this._maskPreOffsetInput.value = this._contextFrameValue("mask_pre_offset");
        }
        if (this._maskPostOffsetInput) {
            this._maskPostOffsetInput.value = this._contextFrameValue("mask_post_offset");
        }
        // Re-snap once after external refresh so old projects whose stored values are
        // off-grid under the new policy reconcile display and persisted state on the
        // first paint. _updateContextFrameWidgets handles toolbar update too.
        this._updateContextFrameWidgets();
    }

    _updateContextFrameWidgets() {
        const preRaw = Math.max(0, parseInt(this._preContextInput?.value, 10) || 0);
        const postRaw = Math.max(0, parseInt(this._postContextInput?.value, 10) || 0);
        const maskPreRaw = Math.max(0, parseInt(this._maskPreOffsetInput?.value, 10) || 0);
        const maskPostRaw = Math.max(0, parseInt(this._maskPostOffsetInput?.value, 10) || 0);
        // Four independent snaps mirroring SonderEditor backend: context snaps grow
        // the rendered tensor, mask offsets only choose which frames are masked
        // within the snapped context cap. The mask-offset cap is the post-snap
        // context value so any context change re-snaps the same-side mask offset
        // in the same pass (forward-direction cross-field coupling).
        const pre = this._snapPreContextFrames(preRaw);
        const post = this._snapPostContextFrames(postRaw);
        const maskPre = this._snapMaskOffset(maskPreRaw, pre);
        const maskPost = this._snapMaskOffset(maskPostRaw, post);
        if (this._preContextInput) this._preContextInput.value = pre;
        if (this._postContextInput) this._postContextInput.value = post;
        if (this._maskPreOffsetInput) this._maskPreOffsetInput.value = maskPre;
        if (this._maskPostOffsetInput) this._maskPostOffsetInput.value = maskPost;
        this._setWidgetValue("pre_context_frames", pre);
        this._setWidgetValue("post_context_frames", post);
        this._setWidgetValue("mask_pre_offset", maskPre);
        this._setWidgetValue("mask_post_offset", maskPost);
        this._updateToolbar();
    }

    _refreshSelectionInputs() {
        if (this._selectionStartInput && document.activeElement !== this._selectionStartInput) {
            this._selectionStartInput.value = this._formatPositionInput(this.selectionStart);
            this._selectionStartInput.title = `Selection in-point: ${this._frameToTimecode(this.selectionStart)}`;
        }
        if (this._selectionEndInput && document.activeElement !== this._selectionEndInput) {
            this._selectionEndInput.value = this._formatPositionInput(this.selectionEnd);
            const duration = Math.max(0, this.selectionEnd - this.selectionStart);
            this._selectionEndInput.title = `Selection out-point: ${this._frameToTimecode(this.selectionEnd)} (${this._frameToTimecode(duration)})`;
        }
        this._refreshPlayheadInput();
    }

    _refreshPlayheadInput() {
        if (!this._playheadFrameInput) return;
        if (document.activeElement === this._playheadFrameInput) return;
        this._playheadFrameInput.value = this._formatPositionInput(this.playhead);
        this._playheadFrameInput.title = `Playhead frame: ${this._frameToTimecode(this.playhead)}`;
    }

    _readStoredTimelineSelection(scene = this.activeScene, settings = this._settings) {
        const projectKey = this._projectDirName();
        const sceneId = scene?.scene_id || "";
        const selection = projectKey && sceneId
            ? settings?.layout?.activeSelectionByProjectScene?.[projectKey]?.[sceneId]
            : null;
        if (!selection || typeof selection !== "object") return null;
        const maxFrame = Math.max(0, parseInt(scene?.duration_frames, 10) || this.totalFrames || 0);
        const start = Math.max(0, Math.min(maxFrame, Math.round(Number(selection.start) || 0)));
        const end = Math.max(start, Math.min(maxFrame, Math.round(Number(selection.end) || 0)));
        return { start, end };
    }

    _persistActiveTimelineSelection() {
        const projectKey = this._projectDirName();
        const sceneId = this.activeScene?.scene_id || this.activeSceneId || "";
        if (!projectKey || !sceneId) return;
        const byProject = this._settings?.layout?.activeSelectionByProjectScene || {};
        this._updateSettings({
            layout: {
                activeSelectionByProjectScene: {
                    ...byProject,
                    [projectKey]: {
                        ...(byProject[projectKey] || {}),
                        [sceneId]: {
                            start: Math.max(0, Math.round(Number(this.selectionStart) || 0)),
                            end: Math.max(0, Math.round(Number(this.selectionEnd) || 0)),
                        },
                    },
                },
            },
        });
    }

    _setTimelineSelection(start, end, { persist = true, render = true } = {}) {
        const maxFrame = Math.max(0, parseInt(this.activeScene?.duration_frames, 10) || this.totalFrames || 0);
        const nextStart = Math.max(0, Math.min(maxFrame, Math.round(Number(start) || 0)));
        const nextEnd = Math.max(0, Math.min(maxFrame, Math.round(Number(end) || 0)));
        this.selectionStart = Math.min(nextStart, nextEnd);
        this.selectionEnd = Math.max(nextStart, nextEnd);
        this._setWidgetValue("selection_start", this.selectionStart);
        this._setWidgetValue("selection_end", this.selectionEnd);
        if (persist) this._persistActiveTimelineSelection();
        this._refreshSelectionInputs();
        if (render) {
            this._renderTimeline();
            this._updateToolbar();
        }
    }

    _setSelectionStartFrame(frame) {
        this._setTimelineSelection(frame, Math.max(frame, this.selectionEnd));
    }

    _setSelectionEndFrame(frame) {
        this._setTimelineSelection(Math.min(frame, this.selectionStart), frame);
    }

    _clearTimelineSelection() {
        this._setTimelineSelection(0, 0);
    }

    _setSelectionToFrameRange(start, end) {
        this._setTimelineSelection(start, end);
    }

    _restoreAnimaticState() {
        if (!this._preAnimaticHidden) return false;
        for (const entry of this._trackLayout) {
            if (entry.type === TRACK_TYPE.VIDEO) {
                entry.hidden = !!this._preAnimaticHidden[entry.laneIndex];
            }
        }
        this._preAnimaticHidden = null;
        this._animaticMode = false;
        return true;
    }

    _frameToTimecode(frame) {
        if (this._timecodeMode === "frames") return String(frame);
        const fps = this._effectiveFps;
        const totalSeconds = frame / fps;
        const h = Math.floor(totalSeconds / 3600);
        const m = Math.floor((totalSeconds % 3600) / 60);
        const s = Math.floor(totalSeconds % 60);
        const f = Math.floor(frame % fps);
        if (h > 0) return `${h}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}:${String(f).padStart(2,"0")}`;
        return `${m}:${String(s).padStart(2,"0")}:${String(f).padStart(2,"0")}`;
    }

    _setSnappingEnabled(enabled) {
        this._updateSettings({
            timelineBehavior: {
                snappingEnabled: !!enabled,
            },
        });
    }

    _setTimecodeMode(mode) {
        const nextMode = mode === "timecode" ? "timecode" : "frames";
        if (nextMode === this._timecodeMode) return;
        this._updateSettings({
            timelineBehavior: {
                timecodeMode: nextMode,
            },
        });
    }

    _toggleTimecodeMode() {
        this._setTimecodeMode(this._timecodeMode === "frames" ? "timecode" : "frames");
    }

    _isLaneTrackType(type) {
        return type === TRACK_TYPE.VIDEO || type === TRACK_TYPE.AUDIO || type === TRACK_TYPE.MOTION_DRIVER;
    }

    _isHeaderControllableTrackType(type) {
        return this._isLaneTrackType(type) || type === TRACK_TYPE.GUIDES || type === TRACK_TYPE.PROMPT;
    }

    _defaultLaneConfig(overrides = {}) {
        return { name: "", color: "", locked: false, hidden: false, ...overrides };
    }

    _trackConfigForFixedType(type) {
        if (type === TRACK_TYPE.GUIDES) return this.activeScene?.guide_track_config || this._defaultLaneConfig();
        if (type === TRACK_TYPE.PROMPT) return this.activeScene?.prompt_track_config || this._defaultLaneConfig();
        return this._defaultLaneConfig();
    }

    _trackItemsForEntry(entry) {
        if (!this.activeScene || !entry) return [];
        if (entry.type === TRACK_TYPE.VIDEO || entry.type === TRACK_TYPE.MOTION_DRIVER) {
            return (this.activeScene.clips || []).filter((clip) => this._clipMatchesTrackEntry(clip, entry));
        }
        if (entry.type === TRACK_TYPE.AUDIO) {
            return (this.activeScene.audio_tracks || []).filter((track) => (track.lane_index || 0) === entry.laneIndex);
        }
        if (entry.type === TRACK_TYPE.GUIDES) {
            return this.activeScene.guide_frames || [];
        }
        if (entry.type === TRACK_TYPE.PROMPT) {
            return this.activeScene.prompt_sections || [];
        }
        return [];
    }

    _trackVisibilityState(entry) {
        if (!entry) return "visible";
        const items = this._trackItemsForEntry(entry);
        if (entry.type === TRACK_TYPE.PROMPT) {
            return entry.hidden ? "hidden" : "visible";
        }
        const mutedCount = items.filter((item) => !!item.muted).length;
        if (entry.hidden || (items.length > 0 && mutedCount === items.length)) return "hidden";
        if (mutedCount > 0) return "partial";
        return "visible";
    }

    _isGuideTrackLocked() {
        const idx = this._guidesLayoutIdx();
        return idx >= 0 && !!this._trackLayout[idx]?.locked;
    }

    _isGuideTrackHidden() {
        const idx = this._guidesLayoutIdx();
        return idx >= 0 && !!this._trackLayout[idx]?.hidden;
    }

    _isGuideTrackCollapsed() {
        const idx = this._guidesLayoutIdx();
        return idx >= 0 && !!this._trackLayout[idx]?.collapsed;
    }

    _isPromptTrackLocked() {
        const idx = this._promptLayoutIdx();
        return idx >= 0 && !!this._trackLayout[idx]?.locked;
    }

    _isPromptTrackHidden() {
        const idx = this._promptLayoutIdx();
        return idx >= 0 && !!this._trackLayout[idx]?.hidden;
    }

    _isItemLocked(item) {
        if (!item) return false;
        if (item.type === "clip") return this._isLaneLocked(this._clipTrackType(item.data), item.data.track_index || 0);
        if (item.type === "audio") return this._isLaneLocked(TRACK_TYPE.AUDIO, item.data.lane_index || 0);
        if (item.type === "guide") return this._isGuideTrackLocked();
        if (item.type === "prompt") return this._isPromptTrackLocked();
        return false;
    }

    _isRenderClip(clip) {
        return !clip?.role || clip.role === "render";
    }

    _isMotionDriverClip(clip) {
        return clip?.role === "motion_driver";
    }

    _clipTrackType(clip) {
        return this._isMotionDriverClip(clip) ? TRACK_TYPE.MOTION_DRIVER : TRACK_TYPE.VIDEO;
    }

    _clipMatchesTrackEntry(clip, entry) {
        return entry
            && this._clipTrackType(clip) === entry.type
            && (clip.track_index || 0) === entry.laneIndex;
    }

    _defaultGuideStrength() {
        return this._settings?.projectDefaults?.defaultGuideStrength
            ?? DEFAULT_EDITOR_SETTINGS.projectDefaults.defaultGuideStrength
            ?? 1.0;
    }

    _defaultMotionDriverStrength() {
        return this._settings?.projectDefaults?.defaultMotionDriverStrength
            ?? DEFAULT_EDITOR_SETTINGS.projectDefaults.defaultMotionDriverStrength
            ?? 1.0;
    }

    async _toggleAnimatic() {
        if (!this.activeScene || !this.projectDir) return;

        if (this._animaticMode) {
            if (!this._restoreAnimaticState()) return;
        } else {
            const hiddenByLane = {};
            for (const entry of this._trackLayout) {
                if (entry.type !== TRACK_TYPE.VIDEO) continue;
                hiddenByLane[entry.laneIndex] = !!entry.hidden;
                entry.hidden = true;
            }
            this._preAnimaticHidden = hiddenByLane;
            this._animaticMode = true;
        }

        await this._saveLaneConfig();
        this._renderTimeline();
        this._renderViewportFrame();
        this._updateToolbar();
    }

    /** Build the track layout array from scene lane counts */
    _buildTrackLayout() {
        const layout = [];
        const scene = this.activeScene;
        const videoLanes = scene?.video_lane_count || 1;
        const motionDriverLanes = scene?.motion_driver_lane_count || 1;
        const audioLanes = scene?.audio_lane_count || 1;
        const vConfigs = scene?.video_lane_configs || [];
        const mdConfigs = scene?.motion_driver_lane_configs || [];
        const aConfigs = scene?.audio_lane_configs || [];
        const storedCollapse = this._readStoredTrackCollapseState(scene);
        const isStored = storedCollapse.exists;
        const storedCollapsed = storedCollapse.collapsed;

        // Video lanes: highest index at top (foreground on top)
        for (let i = videoLanes - 1; i >= 0; i--) {
            const key = TRACK_TYPE.VIDEO + ":" + i;
            const cfg = vConfigs[i] || {};
            layout.push({
                type: TRACK_TYPE.VIDEO,
                label: cfg.name || (videoLanes > 1 ? `V${i + 1}` : "Video"),
                customName: cfg.name || "",
                laneIndex: i,
                collapsed: isStored ? storedCollapsed.has(key) : false,
                color: cfg.color || LANE_PALETTE[i % LANE_PALETTE.length],
                locked: cfg.locked || false,
                hidden: cfg.hidden || false,
            });
        }

        // Audio lanes: lowest index at top
        for (let i = 0; i < audioLanes; i++) {
            const key = TRACK_TYPE.AUDIO + ":" + i;
            const cfg = aConfigs[i] || {};
            layout.push({
                type: TRACK_TYPE.AUDIO,
                label: cfg.name || (audioLanes > 1 ? `A${i + 1}` : "Audio"),
                customName: cfg.name || "",
                laneIndex: i,
                collapsed: isStored ? storedCollapsed.has(key) : false,
                color: cfg.color || LANE_PALETTE[i % LANE_PALETTE.length],
                locked: cfg.locked || false,
                hidden: cfg.hidden || false,
            });
        }

        // Motion-driver lanes: single lane in Phase 4.3, below audio.
        for (let i = 0; i < motionDriverLanes; i++) {
            const key = TRACK_TYPE.MOTION_DRIVER + ":" + i;
            const cfg = mdConfigs[i] || {};
            layout.push({
                type: TRACK_TYPE.MOTION_DRIVER,
                label: cfg.name || (motionDriverLanes > 1 ? `MD${i + 1}` : "Driver"),
                customName: cfg.name || "",
                laneIndex: i,
                collapsed: isStored ? storedCollapsed.has(key) : false,
                color: cfg.color || COLORS.laneDriver,
                locked: cfg.locked || false,
                hidden: cfg.hidden || false,
            });
        }

        // Fixed rows share LaneConfig shape for header lock/hide.
        const guideCfg = this._trackConfigForFixedType(TRACK_TYPE.GUIDES);
        layout.push({
            type: TRACK_TYPE.GUIDES,
            label: "Guides",
            customName: "",
            laneIndex: 0,
            collapsed: isStored ? storedCollapsed.has(TRACK_TYPE.GUIDES + ":0") : false,
            color: "",
            locked: !!guideCfg.locked,
            hidden: !!guideCfg.hidden,
        });
        const promptCfg = this._trackConfigForFixedType(TRACK_TYPE.PROMPT);
        layout.push({
            type: TRACK_TYPE.PROMPT,
            label: "Prompt",
            customName: "",
            laneIndex: 0,
            collapsed: isStored ? storedCollapsed.has(TRACK_TYPE.PROMPT + ":0") : false,
            color: "",
            locked: !!promptCfg.locked,
            hidden: !!promptCfg.hidden,
        });

        this._trackLayout = layout;
        this._clampScrollY();
    }

    /** Find layout index for a video lane */
    _videoLaneLayoutIdx(laneIndex) {
        return this._trackLayout.findIndex(
            e => e.type === TRACK_TYPE.VIDEO && e.laneIndex === laneIndex
        );
    }

    /** Find layout index for an audio lane */
    _audioLaneLayoutIdx(laneIndex) {
        return this._trackLayout.findIndex(
            e => e.type === TRACK_TYPE.AUDIO && e.laneIndex === laneIndex
        );
    }

    /** Find layout index for a motion-driver lane */
    _motionDriverLaneLayoutIdx(laneIndex) {
        return this._trackLayout.findIndex(
            e => e.type === TRACK_TYPE.MOTION_DRIVER && e.laneIndex === laneIndex
        );
    }

    /** Find layout index for guides */
    _guidesLayoutIdx() {
        return this._trackLayout.findIndex(e => e.type === TRACK_TYPE.GUIDES);
    }

    /** Find layout index for prompt */
    _promptLayoutIdx() {
        return this._trackLayout.findIndex(e => e.type === TRACK_TYPE.PROMPT);
    }

    _trackY(layoutIdx) {
        return TimelineCanvas._trackY(this, layoutIdx);
    }

    _trackH(layoutIdx) {
        return TimelineCanvas._trackH(this, layoutIdx);
    }

    _totalTracksHeight() {
        return TimelineCanvas._totalTracksHeight(this);
    }

    _timelineRulerHeight() {
        return TimelineCanvas._timelineRulerHeight(this);
    }

    _visibleTimelineContentHeight() {
        return TimelineCanvas._visibleTimelineContentHeight(this);
    }

    _selectionContextRange() {
        if (this.selectionStart >= this.selectionEnd) return null;
        const sceneEnd = Math.max(
            this.selectionEnd,
            Math.max(0, parseInt(this.activeScene?.duration_frames, 10) || this.totalFrames || 0)
        );
        const preContext = this._contextFrameValue("pre_context_frames");
        const postContext = this._contextFrameValue("post_context_frames");
        const contextStart = Math.max(0, this.selectionStart - preContext);
        const contextEnd = Math.min(sceneEnd, this.selectionEnd + postContext);
        const maskPre = Math.min(this._contextFrameValue("mask_pre_offset"), this.selectionStart - contextStart);
        const maskPost = Math.min(this._contextFrameValue("mask_post_offset"), contextEnd - this.selectionEnd);
        const maskStart = Math.max(contextStart, this.selectionStart - maskPre);
        const maskEnd = Math.min(contextEnd, this.selectionEnd + maskPost);
        return {
            selectionStart: this.selectionStart,
            selectionEnd: this.selectionEnd,
            contextStart,
            contextEnd,
            maskStart,
            maskEnd,
            hasPreContext: contextStart < this.selectionStart,
            hasPostContext: contextEnd > this.selectionEnd,
            hasMaskPre: maskStart < this.selectionStart,
            hasMaskPost: maskEnd > this.selectionEnd,
        };
    }

    _clampScrollY() {
        return TimelineCanvas._clampScrollY(this);
    }

    _trackContentYFromRawY(rawY) {
        return TimelineCanvas._trackContentYFromRawY(this, rawY);
    }

    _layoutIndexFromRawY(rawY) {
        return TimelineCanvas._layoutIndexFromRawY(this, rawY);
    }

    _updateSettings(partial) {
        return updateEditorSettings(partial);
    }

    _renderCacheEntryLimit(settings = this._settings) {
        const rawValue = settings?.render?.maxRenderCacheEntries;
        if (rawValue === null) return null;
        const numeric = Number(rawValue);
        if (!Number.isFinite(numeric)) {
            return DEFAULT_EDITOR_SETTINGS.render.maxRenderCacheEntries;
        }
        return Math.max(1, Math.round(numeric));
    }

    _trashRetentionDays(settings = this._settings) {
        const numeric = Number(settings?.render?.trashRetentionDays);
        if (!Number.isFinite(numeric)) {
            return DEFAULT_EDITOR_SETTINGS.render.trashRetentionDays;
        }
        return Math.max(0, Math.round(numeric));
    }

    _trashMaxSizeMB(settings = this._settings) {
        const rawValue = settings?.render?.trashMaxSizeMB;
        if (rawValue === null || rawValue === undefined || rawValue === "") return null;
        const numeric = Number(rawValue);
        return Number.isFinite(numeric) ? Math.max(0, numeric) : null;
    }

    _assetListQueryString() {
        const params = new URLSearchParams();
        params.set("include_trashed", "true");
        params.set("retention_days", String(this._trashRetentionDays()));
        const trashMaxSizeMB = this._trashMaxSizeMB();
        if (trashMaxSizeMB !== null) {
            params.set("max_size_mb", String(trashMaxSizeMB));
        }
        return params.toString();
    }

    async _sweepRenderCache() {
        const maxEntries = this._renderCacheEntryLimit();
        const dirName = this._projectDirName();
        if (maxEntries === null || !dirName) return;

        try {
            const listResp = await fetch(api.apiURL(`/sonder-editor/project/${encodeURIComponent(dirName)}/cache/renders`));
            if (!listResp.ok) return;
            const data = await listResp.json();
            const entries = Array.isArray(data) ? data : (Array.isArray(data.entries) ? data.entries : []);
            const sortedEntries = entries
                .filter((entry) => typeof entry?.filename === "string" && entry.filename)
                .sort((a, b) => {
                    const aTime = Number.isFinite(Number(a.mtime)) ? Number(a.mtime) : 0;
                    const bTime = Number.isFinite(Number(b.mtime)) ? Number(b.mtime) : 0;
                    if (aTime !== bTime) return aTime - bTime;
                    return String(a.filename).localeCompare(String(b.filename));
                });
            const excess = sortedEntries.length - maxEntries;
            if (excess <= 0) return;

            const staleEntries = sortedEntries.slice(0, excess);
            await Promise.all(staleEntries.map(async (entry) => {
                const deleteResp = await fetch(
                    api.apiURL(`/sonder-editor/project/${encodeURIComponent(dirName)}/cache/renders/${encodeURIComponent(entry.filename)}`),
                    { method: "DELETE" },
                );
                if (!deleteResp.ok && deleteResp.status !== 404) {
                    console.warn("[Sonder] Render cache eviction skipped:", entry.filename, deleteResp.status);
                }
            }));
        } catch (error) {
            console.warn("[Sonder] Failed to sweep render cache:", error);
        }
    }

    _syncTakePlacementModeWidget(settings = this._settings) {
        const mode = settings?.render?.takePlacementMode === "untrimmed" ? "untrimmed" : "trimmed";
        this._setWidgetValue("take_placement_mode", mode);
    }

    _trackCollapseSceneKey(scene = this.activeScene) {
        const projectKey = this._projectDirName();
        const sceneId = scene?.scene_id || "";
        if (!projectKey || !sceneId) return "";
        return `${projectKey}:${sceneId}`;
    }

    _trackCollapseKey(entry) {
        if (!entry) return "";
        return `${entry.type}:${entry.laneIndex}`;
    }

    _readStoredTrackCollapseState(scene = this.activeScene, settings = this._settings) {
        const sceneKey = this._trackCollapseSceneKey(scene);
        const collapsedByScene = settings?.layout?.trackCollapseByScene;
        const collapsedKeys = sceneKey && collapsedByScene && typeof collapsedByScene === "object"
            ? collapsedByScene[sceneKey]
            : null;
        if (!Array.isArray(collapsedKeys)) {
            return { exists: false, collapsed: new Set() };
        }
        return {
            exists: true,
            collapsed: new Set(collapsedKeys.filter((value) => typeof value === "string" && value)),
        };
    }

    _activeTrackCollapseSignature(settings = this._settings) {
        const state = this._readStoredTrackCollapseState(this.activeScene, settings);
        if (!state.exists) return "";
        return JSON.stringify(Array.from(state.collapsed).sort());
    }

    _queueBatchCollapseProjectKey() {
        return this._projectDirName();
    }

    _readStoredQueueBatchCollapseState(settings = this._settings) {
        const projectKey = this._queueBatchCollapseProjectKey();
        const collapsedByProject = settings?.layout?.queueBatchCollapsedByProject;
        const collapsedIds = projectKey && collapsedByProject && typeof collapsedByProject === "object"
            ? collapsedByProject[projectKey]
            : null;
        if (!Array.isArray(collapsedIds)) {
            return { exists: false, collapsed: new Set() };
        }
        return {
            exists: true,
            collapsed: new Set(collapsedIds.filter((value) => typeof value === "string" && value)),
        };
    }

    _activeQueueBatchCollapseSignature(settings = this._settings) {
        const state = this._readStoredQueueBatchCollapseState(settings);
        if (!state.exists) return "";
        return JSON.stringify(Array.from(state.collapsed).sort());
    }

    _currentRenderQueueBatchIds(queue = this._renderQueue) {
        return queueBatchIds(queue || []);
    }

    _applyStoredQueueBatchCollapseState(settings = this._settings) {
        const storedState = this._readStoredQueueBatchCollapseState(settings);
        const nextBatchExpanded = {};
        for (const batchId of this._currentRenderQueueBatchIds()) {
            if (storedState.collapsed.has(batchId)) {
                nextBatchExpanded[batchId] = false;
            }
        }
        this._queueBatchExpanded = nextBatchExpanded;
    }

    _persistQueueBatchCollapseState() {
        const projectKey = this._queueBatchCollapseProjectKey();
        if (!projectKey) return;
        const collapsedIds = Array.from(this._currentRenderQueueBatchIds())
            .filter((batchId) => this._queueBatchExpanded[batchId] === false)
            .sort();
        const prevSignature = this._activeQueueBatchCollapseSignature(this._settings);
        const nextSignature = JSON.stringify(collapsedIds);
        if (prevSignature === nextSignature) return;
        this._updateSettings({
            layout: {
                queueBatchCollapsedByProject: {
                    [projectKey]: collapsedIds,
                },
            },
        });
    }

    _setQueueBatchCollapsedIds(collapsedIds, options = {}) {
        const { persist = false, render = false } = options;
        const validBatchIds = this._currentRenderQueueBatchIds();
        const nextBatchExpanded = {};
        for (const batchId of (collapsedIds instanceof Set ? collapsedIds : [])) {
            if (validBatchIds.has(batchId)) {
                nextBatchExpanded[batchId] = false;
            }
        }
        this._queueBatchExpanded = nextBatchExpanded;
        if (persist) {
            this._persistQueueBatchCollapseState();
        }
        if (render) {
            this._renderQueuePanel();
        }
    }

    _persistTrackCollapseState() {
        const sceneKey = this._trackCollapseSceneKey();
        if (!sceneKey) return;
        const collapsedKeys = this._trackLayout
            .filter((entry) => !!entry?.collapsed)
            .map((entry) => this._trackCollapseKey(entry))
            .filter(Boolean);
        this._updateSettings({
            layout: {
                trackCollapseByScene: {
                    [sceneKey]: collapsedKeys,
                },
            },
        });
    }

    _handleSettingsChange(settings) {
        const nextSettings = settings || getEditorSettings();
        const prevTrackCollapseSignature = this._activeTrackCollapseSignature(this._settings);
        const nextTrackCollapseSignature = this._activeTrackCollapseSignature(nextSettings);
        const prevQueueBatchCollapseSignature = this._activeQueueBatchCollapseSignature(this._settings);
        const nextQueueBatchCollapseSignature = this._activeQueueBatchCollapseSignature(nextSettings);
        const prevRenderCacheLimit = this._renderCacheEntryLimit(this._settings);
        const nextRenderCacheLimit = this._renderCacheEntryLimit(nextSettings);
        const prevTimecodeMode = this._timecodeMode;
        const prevLaneTintSignature = JSON.stringify(this._settings?.appearance?.laneTintOverrides || {});
        const nextLaneTintSignature = JSON.stringify(nextSettings?.appearance?.laneTintOverrides || {});
        this._settings = nextSettings;
        this._syncTakePlacementModeWidget(nextSettings);
        const resolvedTemplateId = getTemplateById(this._templateId, nextSettings).id;
        const templateChanged = resolvedTemplateId !== this._templateId;
        this._templateId = resolvedTemplateId;
        this.snappingEnabled = !!nextSettings.timelineBehavior.snappingEnabled;
        this._snapThreshold = nextSettings.timelineBehavior.snapThreshold;
        this._timecodeMode = nextSettings.timelineBehavior.timecodeMode;
        this._scaleToolbar = nextSettings.layout.scaleToolbar;
        this._scaleTrackHeaders = nextSettings.layout.scaleTrackHeaders;
        this._scaleTimeline = nextSettings.layout.scaleTimeline;
        this._scaleGallery = nextSettings.layout.scaleGallery;
        const prevQueueExpanded = !!this._queueExpanded;
        this._queueExpanded = !!nextSettings.layout.queuePanelExpanded;
        this._labelWidthUser = nextSettings.layout.labelWidth;
        this._labelWidthUserFS = nextSettings.layout.labelWidthFullscreen;

        if (!this.activeScene) {
            this.totalFrames = nextSettings.projectDefaults.newSceneDuration;
        }
        if (this.durationInput) {
            this._refreshDurationInput();
        }
        this._rebuildTemplateOptions();
        this._rebuildResolutionTierOptions();
        this._applyTemplateConstraintMetadata();
        if (this.activeScene || this._sceneBar) {
            this._syncSceneResolutionControls({ detectSelections: false });
        }
        if (templateChanged && this.projectDir && resolvedTemplateId === "free") {
            this._updateProjectTemplateId("free");
        }
        if (this.activeScene && prevTrackCollapseSignature !== nextTrackCollapseSignature) {
            this._buildTrackLayout();
            if (this.timelineCanvas) {
                this._renderTimeline();
            }
        }
        if (prevLaneTintSignature !== nextLaneTintSignature && this.timelineCanvas) {
            this._renderTimeline();
        }
        if (this._queueContainer && prevQueueExpanded !== this._queueExpanded) {
            this._applyQueueExpandedState();
            if (this._queueExpanded) {
                this._fetchRenderQueue();
            }
        }
        if (this._queueContainer && prevQueueBatchCollapseSignature !== nextQueueBatchCollapseSignature) {
            this._applyStoredQueueBatchCollapseState(nextSettings);
            this._renderQueuePanel();
        }
        if (prevRenderCacheLimit !== nextRenderCacheLimit) {
            this._sweepRenderCache();
        }
        if (prevTimecodeMode !== this._timecodeMode && this._itemEditorEl && this.selectedItem) {
            this._showItemEditor();
        }
        this._syncSettingsPanelControls();
        if (this._sceneBar) {
            this._applyScales();
        }
        if (this.isFullscreen) {
            if (this._fsSidebar && nextSettings.layout.fullscreenSidebarWidth > 0) {
                const sidebarMax = this._computeFullscreenSidebarMaxWidth();
                this._fsSidebar.style.width = `${Math.max(FULLSCREEN_SIDEBAR_MIN_WIDTH, Math.min(sidebarMax, nextSettings.layout.fullscreenSidebarWidth))}px`;
            }
            if (this._fsBottomRow && nextSettings.layout.fullscreenTimelineHeight > 0) {
                const timelineMax = this._computeFullscreenTimelineMaxHeight();
                this._fsBottomRow.style.height = `${Math.max(FULLSCREEN_TIMELINE_MIN_HEIGHT, Math.min(timelineMax, nextSettings.layout.fullscreenTimelineHeight))}px`;
            }
            this._recalcFullscreenHeights();
            requestAnimationFrame(() => {
                if (this._destroyed || !this.isFullscreen) return;
                this._resizeViewportCanvas();
                this._renderViewportFrame();
            });
        } else if (this._vpCanvas) {
            this._resizeViewportCanvas();
            this._renderViewportFrame();
        }
        this._updateViewportHeader();
        this._updateToolbar();
        this._updateTransportUI();
    }

    _syncSettingsPanelControls() {
        this._settingsPanelHandle?.sync();
    }

    _createSettingsPanelHost() {
        const editor = this;
        return {
            _settingsPanelEl: null,
            _settingsPanelControls: null,
            _settingsPanelKeyOff: null,
            _renderModelTemplateSettings: null,
            get _settings() { return editor._settings; },
            get _templateId() { return editor._templateId; },
            set _templateId(value) { editor._templateId = value; },
            get _templateFormState() { return editor._templateFormState; },
            set _templateFormState(value) { editor._templateFormState = value; },
            _updateSettings: (partial) => editor._updateSettings(partial),
            _setScale: (key, value) => editor._setScale(key, value),
            _resetEditorLayout: () => editor._resetEditorLayout(),
            _setSnappingEnabled: (enabled) => editor._setSnappingEnabled(enabled),
            _setTimecodeMode: (mode) => editor._setTimecodeMode(mode),
            _trashRetentionDays: (...args) => editor._trashRetentionDays(...args),
            _guideHoverPreviewSize: () => editor._guideHoverPreviewSize(),
            _hideGuideHoverPreview: () => editor._hideGuideHoverPreview(),
            _deleteCustomModelTemplate: (templateId) => editor._deleteCustomModelTemplate(templateId),
            _keyboardConsumerId: (suffix) => editor._keyboardConsumerId(suffix),
            _hideSettingsPanel: () => editor._hideSettingsPanel(),
        };
    }

    async _deleteCustomModelTemplate(templateId) {
        const id = String(templateId || "");
        if (!id) return;
        const settings = this._settings || getEditorSettings();
        const customTemplates = settings.modelTemplates?.customTemplates || [];
        const wasActiveTemplate = this._templateId === id;
        const nextCustomTemplates = customTemplates.filter((entry) => entry.id !== id);
        const currentDefaultTemplateId = settings.projectDefaults?.defaultTemplateId || "free";
        const nextDefaultTemplateId = currentDefaultTemplateId === id ? "free" : currentDefaultTemplateId;
        this._templateFormState = { expanded: false, editId: "" };

        if (wasActiveTemplate) {
            this._templateId = "free";
        }

        this._updateSettings({
            modelTemplates: { customTemplates: nextCustomTemplates },
            projectDefaults: { defaultTemplateId: nextDefaultTemplateId },
        });

        if (wasActiveTemplate) {
            await this._updateProjectTemplateId("free");
            this._rebuildTemplateOptions();
            this._rebuildResolutionTierOptions();
            this._applyTemplateConstraintMetadata();
            this._syncSceneResolutionControls({ detectSelections: false });
            this._updateViewportHeader();
        }
    }

    _timelineBrightnessFactor() {
        return (this._settings?.appearance?.timelineBrightness || DEFAULT_EDITOR_SETTINGS.appearance.timelineBrightness) / 100;
    }

    _timelineColor(hex) {
        return scaleColor(hex, this._timelineBrightnessFactor());
    }

    _canvasSansFont(size, weight = 400) {
        return `${weight} ${Math.max(1, Math.round(size))}px ${FONT.sans}`;
    }

    _canvasMonoFont(size, weight = 400) {
        return `${weight} ${Math.max(1, Math.round(size))}px ${FONT.mono}`;
    }

    _timelineLaneAccent(entry) {
        if (entry?.color) return entry.color;
        if (entry?.type === TRACK_TYPE.AUDIO) return COLORS.laneAudio;
        if (entry?.type === TRACK_TYPE.MOTION_DRIVER) return COLORS.laneDriver;
        if (entry?.type === TRACK_TYPE.GUIDES) return COLORS.laneGuide;
        if (entry?.type === TRACK_TYPE.PROMPT) return COLORS.lanePrompt;
        return COLORS.laneVideo;
    }

    _drawTimelineItemRail(ctx, x, y, w, h, color) {
        return TimelineCanvas._drawTimelineItemRail(this, ctx, x, y, w, h, color);
    }

    _waveformAccentColor() {
        return this._settings?.appearance?.waveformAccent || DEFAULT_EDITOR_SETTINGS.appearance.waveformAccent;
    }

    _resolveLaneTint(trackType) {
        const overrides = this._settings?.appearance?.laneTintOverrides
            || DEFAULT_EDITOR_SETTINGS.appearance.laneTintOverrides;
        const candidate = overrides?.[trackType];
        return typeof candidate === "string" && /^#[0-9a-fA-F]{6}$/.test(candidate) ? candidate : null;
    }

    _clipLabelMode() {
        return this._settings?.appearance?.clipLabelMode || DEFAULT_EDITOR_SETTINGS.appearance.clipLabelMode;
    }

    _defaultNewSceneDuration() {
        return this._settings?.projectDefaults?.newSceneDuration || DEFAULT_EDITOR_SETTINGS.projectDefaults.newSceneDuration;
    }

    _fullscreenPersistStorageKey(persistKey) {
        return persistKey ? `sonder-editor-fs-${persistKey}` : "";
    }

    _readFullscreenPersistValue(persistKey) {
        if (persistKey === "sidebar-width") {
            return this._settings.layout.fullscreenSidebarWidth || null;
        }
        if (persistKey === "timeline-height") {
            return this._settings.layout.fullscreenTimelineHeight || null;
        }
        return null;
    }

    _writeFullscreenPersistValue(persistKey, value) {
        if (!Number.isFinite(value)) return;
        if (persistKey === "sidebar-width") {
            this._updateSettings({ layout: { fullscreenSidebarWidth: Math.round(value) } });
        } else if (persistKey === "timeline-height") {
            this._updateSettings({ layout: { fullscreenTimelineHeight: Math.round(value) } });
        }
    }

    _clearFullscreenPersistValue(persistKey) {
        if (persistKey === "sidebar-width") {
            this._updateSettings({ layout: { fullscreenSidebarWidth: 0 } });
        } else if (persistKey === "timeline-height") {
            this._updateSettings({ layout: { fullscreenTimelineHeight: 0 } });
        }
    }

    _computeFullscreenSidebarMaxWidth() {
        return Math.max(FULLSCREEN_SIDEBAR_MIN_WIDTH, Math.floor(window.innerWidth * 0.5));
    }

    _computeFullscreenTimelineMaxHeight() {
        return Math.max(FULLSCREEN_TIMELINE_MIN_HEIGHT, Math.floor(window.innerHeight * 0.8));
    }

    _defaultFullscreenTimelineHeight() {
        return Math.max(200, Math.min(FULLSCREEN_TIMELINE_FALLBACK_MAX_HEIGHT, Math.round(window.innerHeight * 0.4)));
    }

    _resetEditorLayout() {
        this._updateSettings({
            layout: {
                scaleToolbar: 1.0,
                scaleTrackHeaders: 1.0,
                scaleTimeline: 1.0,
                scaleGallery: 1.0,
                queuePanelExpanded: false,
                queueBatchCollapsedByProject: null,
                trackCollapseByScene: null,
                labelWidth: 0,
                labelWidthFullscreen: 0,
                fullscreenSidebarWidth: 0,
                fullscreenTimelineHeight: 0,
            },
        });
        if (this._fsSidebar) {
            this._fsSidebar.style.width = `${FULLSCREEN_SIDEBAR_DEFAULT_WIDTH}px`;
        }
        if (this._fsBottomRow) {
            this._fsBottomRow.style.height = `${this._defaultFullscreenTimelineHeight()}px`;
        }
        if (this.isFullscreen) {
            this._recalcFullscreenHeights();
            this._renderTimeline();
            requestAnimationFrame(() => {
                this._resizeViewportCanvas();
                this._renderViewportFrame();
            });
        }
    }

    /** Fit timeline zoom to show all content */
    _fitToView() {
        // Find the rightmost content edge
        let maxFrame = 0;
        if (this.activeScene) {
            for (const clip of (this.activeScene.clips || [])) {
                maxFrame = Math.max(maxFrame, clip.timeline_end_frame);
            }
            for (const track of (this.activeScene.audio_tracks || [])) {
                maxFrame = Math.max(maxFrame, track.timeline_end_frame);
            }
            for (const g of (this.activeScene.guide_frames || [])) {
                const idx = g.frame_index === -1 ? this.totalFrames - 1 : g.frame_index;
                maxFrame = Math.max(maxFrame, idx + 1);
            }
            for (const s of (this.activeScene.prompt_sections || [])) {
                maxFrame = Math.max(maxFrame, s.end_frame);
            }
        }
        maxFrame = Math.max(maxFrame, this.totalFrames);

        // Calculate ppf to fit all content with margin (label width already handled by _frameToX)
        const canvas = this.timelineCanvas;
        const rect = canvas.parentElement?.getBoundingClientRect();
        const width = rect ? Math.floor(rect.width) : 400;
        const margin = width * 0.03; // 3% margin on right side
        const availableWidth = width - this._labelW - margin;

        if (maxFrame > 0 && availableWidth > 0) {
            // pixelsPerFrame mutation here is ephemeral; persistence happens only via _zoom.
            this.pixelsPerFrame = Math.max(0.2, Math.min(40, availableWidth / maxFrame));
            this.scrollX = 0; // Frame 0 starts at label edge
        }
        this._renderTimeline();
    }

    // ── Timeline Rendering ─────────────────────────────────────────────
    _renderTimeline() {
        this._refreshPlayheadInput?.();
        TimelineCanvas._renderTimeline(this);
        this._updateToolbar();
    }

    get _labelW() {
        return TimelineCanvas._labelW(this);
    }

    _frameToX(frame) {
        return TimelineCanvas._frameToX(this, frame);
    }

    _xToFrame(x) {
        return TimelineCanvas._xToFrame(this, x);
    }

    _clampScrollX() {
        return TimelineCanvas._clampScrollX(this);
    }

    _drawRuler(ctx, width) {
        return TimelineCanvas._drawRuler(this, ctx, width);
    }

    _drawTracks(ctx, width) {
        return TimelineCanvas._drawTracks(this, ctx, width);
    }

    _drawSelection(ctx, width) {
        return TimelineCanvas._drawSelection(this, ctx, width);
    }

    _drawMutedOverlay(ctx, x, y, w, h, label = "Hidden") {
        return TimelineCanvas._drawMutedOverlay(this, ctx, x, y, w, h, label);
    }

    _drawGuideMarkers(ctx, width) {
        return TimelineCanvas._drawGuideMarkers(this, ctx, width);
    }

    _drawClips(ctx, width) {
        return TimelineCanvas._drawClips(this, ctx, width);
    }

    _drawPlayheadTriangle(ctx, width) {
        return TimelineCanvas._drawPlayheadTriangle(this, ctx, width);
    }

    _drawPlayheadLine(ctx, width) {
        return TimelineCanvas._drawPlayheadLine(this, ctx, width);
    }

    _drawSnapIndicator(ctx, width, height = this.timelineCanvas?.height || 0) {
        return TimelineCanvas._drawSnapIndicator(this, ctx, width, height);
    }

    _drawVerticalScrollbar(ctx, width, height) {
        return TimelineCanvas._drawVerticalScrollbar(this, ctx, width, height);
    }

    _hitTestTrackHeader(x, rawY) {
        return TimelineCanvas._hitTestTrackHeader(this, x, rawY);
    }

    _hitTestHeaderEdge(x, y) {
        return TimelineCanvas._hitTestHeaderEdge(this, x, y);
    }

    _hitTestClip(x, rawY) {
        return TimelineCanvas._hitTestClip(this, x, rawY);
    }

    _hitTestAudio(x, rawY) {
        return TimelineCanvas._hitTestAudio(this, x, rawY);
    }

    _hitTestGuide(x, rawY) {
        return TimelineCanvas._hitTestGuide(this, x, rawY);
    }

    _hitTestPrompt(x, rawY) {
        return TimelineCanvas._hitTestPrompt(this, x, rawY);
    }

    _hitTestItem(x, rawY) {
        return TimelineCanvas._hitTestItem(this, x, rawY);
    }

    _findSceneItemBySelection(type, id) {
        if (!this.activeScene) return null;
        if (type === "clip") {
            const clip = (this.activeScene.clips || []).find((item) => item.clip_id === id);
            return clip ? { type, id, data: clip } : null;
        }
        if (type === "audio") {
            const track = (this.activeScene.audio_tracks || []).find((item) => item.track_id === id);
            return track ? { type, id, data: track } : null;
        }
        if (type === "guide") {
            const guide = (this.activeScene.guide_frames || []).find((item) => item.frame_index === id);
            return guide ? { type, id, data: guide } : null;
        }
        if (type === "prompt") {
            const section = (this.activeScene.prompt_sections || [])[id];
            return section ? { type, id, data: section } : null;
        }
        return null;
    }

    /** Detect if the mouse is near the left or right edge of a clip/audio track for trimming.
     *  Returns { type, id, data, edge: "left"|"right" } or null. */
    _hitTestEdge(x, rawY) {
        return TimelineCanvas._hitTestEdge(this, x, rawY);
    }

    /** Check if an item is in the current selection. */
    _isSelected(type, id) {
        return this.selectedItems.some(s => s.type === type && s.id === id);
    }

    _reconcileSelection() {
        if (!this.activeScene || !this.selectedItems.length) return;

        const reconciled = this.selectedItems
            .map((item) => this._findSceneItemBySelection(item.type, item.id))
            .filter(Boolean);

        if (!reconciled.length) {
            this._clearSelection();
            this._hideItemEditor();
            return;
        }

        this.selectedItems = reconciled;
        if (this.selectedItem) {
            this.selectedItem = this._findSceneItemBySelection(this.selectedItem.type, this.selectedItem.id)
                || reconciled[reconciled.length - 1]
                || null;
        } else {
            this.selectedItem = reconciled[reconciled.length - 1] || null;
        }
    }

    /** Clear the entire selection. */
    _clearSelection() {
        this.selectedItems = [];
        this.selectedItem = null;
    }

    /** Select a single item (replaces selection). */
    _selectItem(hit) {
        this.selectedItems = [hit];
        this.selectedItem = hit;
    }

    _refreshSelectedHit(hit) {
        if (!hit) return;
        const idx = this.selectedItems.findIndex((item) => item.type === hit.type && item.id === hit.id);
        if (idx >= 0) {
            this.selectedItems[idx] = hit;
        }
        this.selectedItem = hit;
    }

    /** Toggle an item in the selection (Ctrl+click). */
    _toggleSelectItem(hit) {
        const idx = this.selectedItems.findIndex(s => s.type === hit.type && s.id === hit.id);
        if (idx >= 0) {
            this.selectedItems.splice(idx, 1);
            this.selectedItem = this.selectedItems[this.selectedItems.length - 1] || null;
        } else {
            this.selectedItems.push(hit);
            this.selectedItem = hit;
        }
    }

    /** Add an item to the selection (Shift+click). */
    _addToSelection(hit) {
        if (!this._isSelected(hit.type, hit.id)) {
            this.selectedItems.push(hit);
        } else {
            this._refreshSelectedHit(hit);
            return;
        }
        this.selectedItem = hit;
    }

    /** Snap a frame to nearby edges (clip edges, guide frames, playhead, selection bounds).
     *  Returns the snapped frame, or the original if no snap.
     *  Sets this._snapIndicator for visual feedback. */
    _snapFrame(frame, excludeIds = [], excludeFrames = []) {
        if (!this.snappingEnabled || !this.activeScene) {
            this._snapIndicator = null;
            return frame;
        }

        const threshold = this._snapThreshold;
        const candidates = [];
        const snapTargets = this._settings.timelineBehavior.snapTargets;
        // Snap-back guard (#35): never snap to a frame that exactly equals an excluded frame
        // (the drag's origin positions). Without this, dragging a clip whose origin is
        // adjacent to another clip's edge — or near scene bounds — gets pulled back to
        // origin and frameDelta collapses to 0.
        const excludeSet = excludeFrames.length > 0 ? new Set(excludeFrames) : null;

        // Playhead
        if (snapTargets.playhead) {
            candidates.push(this.playhead);
        }

        // Selection bounds
        if (snapTargets.selection && this.selectionStart < this.selectionEnd) {
            candidates.push(this.selectionStart, this.selectionEnd);
        }

        // Clip edges
        if (snapTargets.clipEdges) {
            for (const clip of (this.activeScene.clips || [])) {
                if (excludeIds.includes(clip.clip_id)) continue;
                candidates.push(clip.timeline_start_frame, clip.timeline_end_frame);
            }
        }

        // Audio track edges
        if (snapTargets.audioEdges) {
            for (const track of (this.activeScene.audio_tracks || [])) {
                if (excludeIds.includes(track.track_id)) continue;
                candidates.push(track.timeline_start_frame, track.timeline_end_frame);
            }
        }

        // Guide frames
        if (snapTargets.guides) {
            for (const g of (this.activeScene.guide_frames || [])) {
                const idx = g.frame_index === -1 ? this.totalFrames - 1 : g.frame_index;
                candidates.push(idx);
            }
        }

        // Prompt section edges
        if (snapTargets.promptSections) {
            for (let i = 0; i < (this.activeScene.prompt_sections || []).length; i++) {
                if (excludeIds.includes(i)) continue;
                const s = this.activeScene.prompt_sections[i];
                candidates.push(s.start_frame, s.end_frame);
            }
        }

        // Frame 0 and last frame
        if (snapTargets.sceneBounds) {
            candidates.push(0, this.totalFrames);
        }

        let best = frame;
        let bestDist = threshold + 1;
        for (const c of candidates) {
            if (excludeSet && excludeSet.has(c)) continue;
            const d = Math.abs(frame - c);
            if (d < bestDist) {
                bestDist = d;
                best = c;
            }
        }

        this._snapIndicator = (bestDist <= threshold) ? best : null;
        return (bestDist <= threshold) ? best : frame;
    }

    // ── Timeline Events ────────────────────────────────────────────────
    _setupTimelineEvents() {
        const canvas = this.timelineCanvas;

        canvas.addEventListener("mousedown", (e) => {
            // Only handle left-click (button 0) for selection/playhead
            if (e.button !== 0) return;

            // Close context menu on any click
            this._hideContextMenu();

            const { x, y, rawY } = this._canvasMouseCoords(e);

            // Header edge resize drag
            if (this._hitTestHeaderEdge(x, y)) {
                this.isDragging = true;
                this.dragType = "headerResize";
                this._headerResizeStartX = x;
                this._headerResizeStartW = this._labelW;
                canvas.style.cursor = "col-resize";
                e.preventDefault();
                return;
            }

            const frame = Math.max(0, Math.min(this.totalFrames, this._xToFrame(x)));

            if (y < Math.round(RULER_HEIGHT * this._scaleTimeline)) {
                // Click on ruler = move playhead
                this.playhead = frame;
                this.isDragging = true;
                this.dragType = "playhead";
                if (this.isPlaying) this._stopPlayback();
                this._renderViewportFrame();
            } else if (this._hitTestTrackHeader(x, rawY)) {
                const headerHit = this._hitTestTrackHeader(x, rawY);
                const entry = this._trackLayout[headerHit.layoutIdx];
                switch (headerHit.zone) {
                    case "collapse":
                        entry.collapsed = !entry.collapsed;
                        this._persistTrackCollapseState();
                        break;
                    case "lock":
                        this._pushUndo("toggle track lock");
                        entry.locked = !entry.locked;
                        this._saveLaneConfig();
                        break;
                    case "hide":
                        this._pushUndo("toggle track visibility");
                        void this._toggleHeaderVisibility(entry);
                        break;
                }
                this._renderTimeline();
                this._renderViewportFrame();
            } else {
                // Selection is set only via I/O shortcuts — no mouse selection drag
                if (this._razorMode) {
                    // Razor mode: split clip or audio at click position
                    const hit = this._hitTestItem(x, rawY);
                    if (hit && (hit.type === "clip" || hit.type === "audio")) {
                        this._splitClipAtFrame(hit, frame);
                    }
                    return;
                } else {
                    // Check if near clip/audio/prompt edges for trimming
                    const edgeHit = this._hitTestEdge(x, rawY);
                    if (edgeHit) {
                        // Block trim on locked lanes
                        if (edgeHit.type === "clip" && this._isLaneLocked(this._clipTrackType(edgeHit.data), edgeHit.data.track_index || 0)) return;
                        if (edgeHit.type === "audio" && this._isLaneLocked(TRACK_TYPE.AUDIO, edgeHit.data.lane_index || 0)) return;
                        if (edgeHit.type === "prompt" && this._isPromptTrackLocked()) return;
                        this._pushUndo("trim");
                        const isPrompt = edgeHit.type === "prompt";
                        const trimOrigStart = isPrompt ? edgeHit.data.start_frame : edgeHit.data.timeline_start_frame;
                        const trimOrigEnd = isPrompt ? edgeHit.data.end_frame : edgeHit.data.timeline_end_frame;
                        const trimOrigSourceIn = edgeHit.data.source_in_frame || 0;
                        const trimOrigSourceOut = edgeHit.data.source_out_frame || (trimOrigEnd - trimOrigStart);
                        // Read total_source_frames from item data (set by backend on placement/split).
                        // Fall back to (sourceOut - sourceIn) + visible duration for legacy data without the field.
                        const trimOrigTotalSource = (typeof edgeHit.data.total_source_frames === "number" && edgeHit.data.total_source_frames > 0)
                            ? edgeHit.data.total_source_frames
                            : (trimOrigSourceOut - trimOrigSourceIn) + (trimOrigEnd - trimOrigStart);
                        this._trimItem = {
                            ...edgeHit,
                            origStart: trimOrigStart,
                            origEnd: trimOrigEnd,
                            origSourceIn: trimOrigSourceIn,
                            origSourceOut: trimOrigSourceOut,
                            origTotalSourceFrames: trimOrigTotalSource,
                        };
                        this.isDragging = true;
                        this.dragType = "trimEdge";
                        this._dragStartFrame = frame;
                        this._selectItem(edgeHit);
                        this._renderTimeline();
                        return;
                    }

                    // Check if clicking on a timeline item
                    const hit = this._hitTestItem(x, rawY);
                    if (hit) {
                        if (e.ctrlKey || e.metaKey) {
                            // Ctrl+click = toggle item in selection
                            this._toggleSelectItem(hit);
                        } else if (e.shiftKey) {
                            // Shift+click = add to selection
                            this._addToSelection(hit);
                        } else if (!this._isSelected(hit.type, hit.id)) {
                            // Plain click on unselected item = select only this
                            this._selectItem(hit);
                        } else {
                            // Plain click on already-selected item = keep selection (for drag)
                            this._refreshSelectedHit(hit);
                        }
                        this._hideItemEditor(); // Will show on mouseup if no drag
                        // Block drag if any selected item is on a locked lane
                        const anyLocked = this.selectedItems.some(s => this._isItemLocked(s));
                        if (anyLocked) return;
                        this._pushUndo("move items"); // Capture BEFORE drag modifies data
                        this.isDragging = true;
                        this.dragType = "moveItem";
                        this._dragStartFrame = frame;
                        this._lastSnappedDelta = 0; // Track snapped delta for commit
                        this._dragItemOrigStart = hit.data.timeline_start_frame ?? hit.data.start_frame ?? hit.data.frame_index ?? 0;
                        this._dragItemOrigEnd = hit.data.timeline_end_frame ?? hit.data.end_frame ?? this._dragItemOrigStart;
                        // Anchor lane/type for per-item lane-delta calculation (#15)
                        this._dragAnchorId = hit.id;
                        this._dragAnchorOrigLane = hit.type === "clip" ? (hit.data.track_index || 0)
                            : (hit.type === "audio" ? (hit.data.lane_index || 0) : 0);
                        this._dragAnchorTrackType = hit.type === "clip" ? this._clipTrackType(hit.data)
                            : (hit.type === "audio" ? TRACK_TYPE.AUDIO : "");
                        // Store original positions + lane info for all selected items (group move)
                        this._dragItemsOrig = this.selectedItems.map(s => ({
                            type: s.type, id: s.id, data: s.data,
                            origStart: s.data.timeline_start_frame ?? s.data.start_frame ?? s.data.frame_index ?? 0,
                            origEnd: s.data.timeline_end_frame ?? s.data.end_frame ?? (s.data.timeline_start_frame ?? s.data.start_frame ?? s.data.frame_index ?? 0),
                            origLane: s.type === "clip" ? (s.data.track_index || 0) : (s.type === "audio" ? (s.data.lane_index || 0) : 0),
                            origTrackType: s.type === "clip" ? this._clipTrackType(s.data) : (s.type === "audio" ? TRACK_TYPE.AUDIO : ""),
                        }));
                        this._dragLaneChanged = false;
                        this._dragSwapTarget = null;
                        this._dragLastValidProposed = null;
                        // Snapshot ALL clip/audio lanes AND positions for swap preview + hold-preview restore (#35)
                        this._origAllClipLanes = {};
                        this._origAllClipStarts = {};
                        this._origAllClipEnds = {};
                        for (const c of (this.activeScene?.clips || [])) {
                            this._origAllClipLanes[c.clip_id] = c.track_index || 0;
                            this._origAllClipStarts[c.clip_id] = c.timeline_start_frame || 0;
                            this._origAllClipEnds[c.clip_id] = c.timeline_end_frame || 0;
                        }
                        this._origAllAudioLanes = {};
                        this._origAllAudioStarts = {};
                        this._origAllAudioEnds = {};
                        for (const a of (this.activeScene?.audio_tracks || [])) {
                            this._origAllAudioLanes[a.track_id] = a.lane_index || 0;
                            this._origAllAudioStarts[a.track_id] = a.timeline_start_frame || 0;
                            this._origAllAudioEnds[a.track_id] = a.timeline_end_frame || 0;
                        }
                    } else {
                        // Click on empty space — deselect all
                        this._clearSelection();
                        this._hideItemEditor();
                    }
                }
            }
            this._renderTimeline();
        });

        canvas.addEventListener("mousemove", (e) => {
            const { x, y, rawY } = this._canvasMouseCoords(e);

            if (!this.isDragging) {
                const guideHit = this._hitTestGuide(x, rawY);
                if (guideHit) {
                    this._showGuideHoverPreview(guideHit.data, e.clientX, e.clientY);
                } else {
                    this._hideGuideHoverPreview();
                }
                // Update cursor based on position
                if (this._hitTestHeaderEdge(x, y)) {
                    canvas.style.cursor = "col-resize";
                } else if (this._razorMode) {
                    canvas.style.cursor = "crosshair";
                } else if (this._hitTestEdge(x, rawY)) {
                    canvas.style.cursor = "ew-resize";
                } else if (this._hitTestItem(x, rawY)) {
                    canvas.style.cursor = "grab";
                } else {
                    canvas.style.cursor = "crosshair";
                }
                return;
            }
            this._hideGuideHoverPreview();

            // Header resize drag
            if (this.dragType === "headerResize") {
                canvas.style.cursor = "col-resize";
                const delta = x - this._headerResizeStartX;
                const newW = Math.max(30, this._headerResizeStartW + delta);
                const hs = this._scaleTrackHeaders;
                const baseW = Math.round(newW / hs); // store unscaled base width
                if (this.isFullscreen) {
                    this._labelWidthUserFS = baseW;
                } else {
                    this._labelWidthUser = baseW;
                }
                this._renderTimeline();
                return;
            }

            const frame = Math.max(0, Math.min(this.totalFrames, this._xToFrame(x)));

            if (this.dragType === "playhead") {
                this.playhead = frame;
                this._renderViewportFrame();
            } else if (this.dragType === "trimEdge" && this._trimItem) {
                canvas.style.cursor = "ew-resize";
                const snappedFrame = this._snapFrame(frame, [this._trimItem.id]);
                const item = this._trimItem;

                if (item.type === "prompt") {
                    // Prompts have no source media — just resize freely
                    if (item.edge === "left") {
                        item.data.start_frame = Math.max(0, Math.min(item.origEnd - 1, snappedFrame));
                    } else {
                        item.data.end_frame = Math.max(item.origStart + 1, Math.min(this.totalFrames, snappedFrame));
                    }
                } else {
                    // Clips and audio — clamp to source media bounds
                    if (item.edge === "left") {
                        const delta = Math.max(-item.origSourceIn, snappedFrame - item.origStart);
                        const newStart = Math.max(0, Math.min(item.origEnd - 1, item.origStart + delta));
                        item.data.timeline_start_frame = newStart;
                        const sourceInDelta = newStart - item.origStart;
                        if (item.type === "clip") {
                            item.data.source_in_frame = Math.max(0, item.origSourceIn + sourceInDelta);
                        } else if (item.type === "audio") {
                            item.data.source_in_frame = Math.max(0, item.origSourceIn + sourceInDelta);
                        }
                    } else {
                        // Right-edge trim ceiling = total_source_frames - source_in_frame
                        // (post-trim tail remaining beyond the current visible end)
                        const tailRemaining = Math.max(0, item.origTotalSourceFrames - item.origSourceOut);
                        const maxEnd = item.origEnd + tailRemaining;
                        const newEnd = Math.max(item.origStart + 1, Math.min(maxEnd, snappedFrame));
                        item.data.timeline_end_frame = newEnd;
                        if (item.type === "clip") {
                            item.data.source_out_frame = item.origSourceOut + (newEnd - item.origEnd);
                        }
                    }
                }
            } else if (this.dragType === "moveItem" && this.selectedItems.length > 0) {
                canvas.style.cursor = "grabbing";
                const rawDelta = frame - this._dragStartFrame;
                const excludeIds = this.selectedItems.map(s => s.id);
                // Snap-back guard (#35): exclude every dragged item's origStart/origEnd so
                // snapping cannot pull primaryNewStart back to where the drag began. Adjacent
                // clip edges and scene bounds remain valid snap targets when they aren't the origin.
                const excludeFrames = [];
                for (const o of (this._dragItemsOrig || [])) {
                    if (typeof o.origStart === "number") excludeFrames.push(o.origStart);
                    if (typeof o.origEnd === "number" && o.origEnd !== o.origStart) excludeFrames.push(o.origEnd);
                }
                const primaryNewStart = Math.max(0, this._dragItemOrigStart + rawDelta);
                const snappedStart = this._snapFrame(primaryNewStart, excludeIds, excludeFrames);
                const frameDelta = snappedStart - this._dragItemOrigStart;
                this._lastSnappedDelta = frameDelta;

                // Detect lane from Y position for cross-lane drag
                let hoverLaneType = null;
                let hoverLaneIndex = -1;
                const hoverLayoutIdx = this._layoutIndexFromRawY(rawY);
                if (hoverLayoutIdx >= 0) {
                    const hoverEntry = this._trackLayout[hoverLayoutIdx];
                    hoverLaneType = hoverEntry.type;
                    hoverLaneIndex = hoverEntry.laneIndex;
                }

                // Restore ALL clips/audio to their original lanes + positions before recomputing.
                // Mid-drag mutations can leave previous-tick state on non-anchor items; the snapshot
                // taken at mousedown is the canonical baseline.
                for (const c of (this.activeScene?.clips || [])) {
                    if (this._origAllClipLanes && this._origAllClipLanes[c.clip_id] !== undefined) {
                        c.track_index = this._origAllClipLanes[c.clip_id];
                    }
                    if (this._origAllClipStarts && this._origAllClipStarts[c.clip_id] !== undefined) {
                        c.timeline_start_frame = this._origAllClipStarts[c.clip_id];
                        c.timeline_end_frame = this._origAllClipEnds[c.clip_id];
                    }
                }
                for (const a of (this.activeScene?.audio_tracks || [])) {
                    if (this._origAllAudioLanes && this._origAllAudioLanes[a.track_id] !== undefined) {
                        a.lane_index = this._origAllAudioLanes[a.track_id];
                    }
                    if (this._origAllAudioStarts && this._origAllAudioStarts[a.track_id] !== undefined) {
                        a.timeline_start_frame = this._origAllAudioStarts[a.track_id];
                        a.timeline_end_frame = this._origAllAudioEnds[a.track_id];
                    }
                }

                const draggedClipIds = new Set((this._dragItemsOrig || []).filter(o => o.type === "clip").map(o => o.id));
                const draggedAudioIds = new Set((this._dragItemsOrig || []).filter(o => o.type === "audio").map(o => o.id));

                // Anchor lane delta (#15): each dragged item's lane = origLane + (hoverLane - anchorOrigLane).
                // Only applies when the hovered lane type matches the anchor's track type.
                let anchorDelta = 0;
                if (hoverLaneType === this._dragAnchorTrackType && hoverLaneIndex >= 0) {
                    anchorDelta = hoverLaneIndex - this._dragAnchorOrigLane;
                }

                const scene = this.activeScene;
                const laneMaxFor = (trackType) => {
                    if (trackType === TRACK_TYPE.AUDIO) return scene?.audio_lane_count || 1;
                    if (trackType === TRACK_TYPE.MOTION_DRIVER) return scene?.motion_driver_lane_count || 1;
                    return scene?.video_lane_count || 1;
                };

                // Validate every dragged item's target lane is in range. If any fails, lane delta
                // collapses to 0 — items keep their origLane while horizontal motion continues (#15 hold-preview).
                let laneFitsAll = true;
                for (const orig of (this._dragItemsOrig || [])) {
                    if (orig.type !== "clip" && orig.type !== "audio") continue;
                    if (orig.origTrackType !== this._dragAnchorTrackType) continue;
                    const targetLane = orig.origLane + anchorDelta;
                    if (targetLane < 0 || targetLane >= laneMaxFor(orig.origTrackType)) {
                        laneFitsAll = false;
                        break;
                    }
                }
                const effectiveLaneDelta = laneFitsAll ? anchorDelta : 0;

                // Swap-target detection (#35): single-item drag where cursor is on a non-dragged
                // same-type clip/audio item. Triggers a full position+lane swap rather than overlap.
                let swapTarget = null;
                const draggedOrig = this._dragItemsOrig || [];
                if (draggedOrig.length === 1 && hoverLaneIndex >= 0) {
                    const anchor = draggedOrig[0];
                    if ((anchor.type === "clip" && hoverLaneType === anchor.origTrackType) ||
                        (anchor.type === "audio" && hoverLaneType === TRACK_TYPE.AUDIO)) {
                        const candidate = this._hitTestItem(x, rawY);
                        if (candidate && candidate.id !== anchor.id && candidate.type === anchor.type) {
                            swapTarget = candidate;
                        }
                    }
                }
                this._dragSwapTarget = swapTarget;

                // Compute proposed positions for all dragged items.
                const proposed = [];
                for (const orig of (this._dragItemsOrig || [])) {
                    if (orig.type !== "clip" && orig.type !== "audio") continue;
                    const duration = orig.origEnd - orig.origStart;
                    let newStart, newLane;
                    if (swapTarget && orig.id === this._dragAnchorId) {
                        newStart = swapTarget.data.timeline_start_frame;
                        newLane = orig.type === "clip"
                            ? (swapTarget.data.track_index || 0)
                            : (swapTarget.data.lane_index || 0);
                    } else {
                        newStart = Math.max(0, orig.origStart + frameDelta);
                        newLane = (orig.origTrackType === this._dragAnchorTrackType)
                            ? orig.origLane + effectiveLaneDelta
                            : orig.origLane;
                    }
                    proposed.push({ orig, newStart, newEnd: newStart + duration, newLane });
                }

                // Overlap validation (#35 lock-lane-merge). Same-lane overlap with a non-dragged
                // item is invalid unless that item is the swap target.
                const ignoreIds = new Set();
                if (swapTarget) ignoreIds.add(swapTarget.id);
                let allFitsValid = true;
                for (const p of proposed) {
                    const others = p.orig.type === "clip"
                        ? (scene?.clips || [])
                        : (scene?.audio_tracks || []);
                    for (const c of others) {
                        const cid = c.clip_id || c.track_id;
                        if (draggedClipIds.has(cid) || draggedAudioIds.has(cid)) continue;
                        if (ignoreIds.has(cid)) continue;
                        const cLane = (c.track_index !== undefined) ? c.track_index : c.lane_index;
                        if (cLane !== p.newLane) continue;
                        if (p.orig.type === "clip" && this._clipTrackType(c) !== p.orig.origTrackType) continue;
                        // Half-open overlap: [a_start, a_end) intersects [b_start, b_end) iff a_start < b_end && a_end > b_start
                        if (p.newStart < (c.timeline_end_frame ?? 0) && p.newEnd > (c.timeline_start_frame ?? 0)) {
                            allFitsValid = false;
                            break;
                        }
                    }
                    if (!allFitsValid) break;
                }

                if (allFitsValid) {
                    // Apply proposed positions/lanes to all dragged items
                    for (const p of proposed) {
                        p.orig.data.timeline_start_frame = p.newStart;
                        p.orig.data.timeline_end_frame = p.newEnd;
                        if (p.orig.type === "clip") {
                            p.orig.data.track_index = p.newLane;
                        } else {
                            p.orig.data.lane_index = p.newLane;
                        }
                    }
                    // Swap target takes anchor's original (position + lane)
                    if (swapTarget) {
                        const anchor = draggedOrig[0];
                        const tDur = (swapTarget.data.timeline_end_frame ?? 0) - (swapTarget.data.timeline_start_frame ?? 0);
                        swapTarget.data.timeline_start_frame = anchor.origStart;
                        swapTarget.data.timeline_end_frame = anchor.origStart + tDur;
                        if (anchor.type === "clip") {
                            swapTarget.data.track_index = anchor.origLane;
                        } else {
                            swapTarget.data.lane_index = anchor.origLane;
                        }
                    }
                    this._dragLastValidProposed = proposed;
                    this._dragLastValidSwapTarget = swapTarget;
                    this._dragLaneChanged = (effectiveLaneDelta !== 0) || swapTarget != null;
                } else if (this._dragLastValidProposed) {
                    // Hold preview at last valid frame — replay it
                    for (const p of this._dragLastValidProposed) {
                        p.orig.data.timeline_start_frame = p.newStart;
                        p.orig.data.timeline_end_frame = p.newEnd;
                        if (p.orig.type === "clip") p.orig.data.track_index = p.newLane;
                        else p.orig.data.lane_index = p.newLane;
                    }
                    if (this._dragLastValidSwapTarget) {
                        const lastTarget = this._dragLastValidSwapTarget;
                        const anchor = draggedOrig[0];
                        const tDur = (lastTarget.data.timeline_end_frame ?? 0) - (lastTarget.data.timeline_start_frame ?? 0);
                        // Reapply swap from snapshot to keep the swap target in anchor's origin
                        lastTarget.data.timeline_start_frame = anchor.origStart;
                        lastTarget.data.timeline_end_frame = anchor.origStart + tDur;
                        if (anchor.type === "clip") lastTarget.data.track_index = anchor.origLane;
                        else lastTarget.data.lane_index = anchor.origLane;
                    }
                }
                // (else no prior valid state — items already restored to origLane/origStart above)

                // Non-clip/audio items move directly per frameDelta
                for (const orig of (this._dragItemsOrig || [])) {
                    if (orig.type === "guide") {
                        const newIdx = Math.max(0, Math.min(this.totalFrames - 1, orig.origStart + frameDelta));
                        orig.data._previewFrameIndex = newIdx;
                    } else if (orig.type === "prompt") {
                        const duration = orig.origEnd - orig.origStart;
                        const newStart = Math.max(0, orig.origStart + frameDelta);
                        orig.data.start_frame = newStart;
                        orig.data.end_frame = newStart + duration;
                    }
                }
            }

            this._renderTimeline();
        });

        const onMouseUp = (e) => {
            if (!this.isDragging) return;
            const wasDragType = this.dragType;
            this.isDragging = false;
            this.dragType = null;
            this._snapIndicator = null;
            let commitPromise = null;

            if (wasDragType === "headerResize") {
                this._updateSettings({
                    layout: {
                        labelWidth: this._labelWidthUser,
                        labelWidthFullscreen: this._labelWidthUserFS,
                    },
                });
                canvas.style.cursor = "crosshair";
                this._renderTimeline();
                this._flushDeferredDragState();
                return;
            } else if (wasDragType === "trimEdge" && this._trimItem) {
                // Commit trim to server
                commitPromise = this._commitTrim(this._trimItem);
                this._trimItem = null;
                canvas.style.cursor = "crosshair";
            } else if (wasDragType === "moveItem" && this.selectedItems.length > 0) {
                // Use the snapped delta (stored during mousemove), not raw mouse position
                const frameDelta = this._lastSnappedDelta || 0;

                if (frameDelta !== 0 || this._dragLaneChanged || this._dragSwapTarget) {
                    commitPromise = this._commitItemMove(frameDelta);
                } else {
                    // Click without drag = show properties editor (single item only)
                    // Remove the undo entry since nothing changed
                    if (this._undoStack.length > 0) this._undoStack.pop();
                    if (this.selectedItems.length === 1) {
                        if (this.selectedItem?.type === "prompt") {
                            // Show prompt text editor for prompt sections
                            const sections = this.activeScene?.prompt_sections || [];
                            const idx = this.selectedItem.id;
                            if (idx >= 0 && idx < sections.length) {
                                this._selectedPromptIdx = idx;
                                this._showPromptEditor(sections[idx], idx);
                            }
                        } else {
                            this._showItemEditor();
                        }
                    }
                }
                canvas.style.cursor = "grab";
                this._dragItemsOrig = null;
                this._origAllClipLanes = {};
                this._origAllClipStarts = {};
                this._origAllClipEnds = {};
                this._origAllAudioLanes = {};
                this._origAllAudioStarts = {};
                this._origAllAudioEnds = {};
                this._lastSnappedDelta = 0;
                this._dragLaneChanged = false;
                this._dragSwapTarget = null;
                this._dragLastValidProposed = null;
                this._dragLastValidSwapTarget = null;
                this._dragAnchorId = null;
                this._dragAnchorOrigLane = 0;
                this._dragAnchorTrackType = "";
            } else {
                // Normalize selection direction
                if (this.selectionStart > this.selectionEnd) {
                    [this.selectionStart, this.selectionEnd] = [this.selectionEnd, this.selectionStart];
                }

                // Update hidden widgets and browser-local selection memory.
                this._setTimelineSelection(this.selectionStart, this.selectionEnd, { render: false });
            }

            this._renderTimeline();

            // #36: replay deferred remote widget/scenes state after move/trim commits
            // settle so a stale refresh cannot pull pre-commit state over the released drag.
            this._flushDeferredDragState(commitPromise);
        };

        canvas.addEventListener("mouseup", onMouseUp);
        canvas.addEventListener("mouseleave", onMouseUp);
        canvas.addEventListener("mouseleave", () => this._hideGuideHoverPreview());

        // Scroll to pan
        canvas.addEventListener("wheel", (e) => {
            e.preventDefault();
            if (e.shiftKey) {
                // Zoom
                this._zoom(e.deltaY < 0 ? 1 : -1);
            } else if (e.ctrlKey) {
                // Horizontal pan
                this.scrollX += e.deltaY / this.pixelsPerFrame * 3;
                this._clampScrollX();
                this._renderTimeline();
            } else {
                // Vertical scroll
                this.scrollY += e.deltaY;
                this._clampScrollY();
                this._renderTimeline();
            }
        }, { passive: false });

        // Drop assets onto timeline
        canvas.addEventListener("dragover", (e) => {
            e.preventDefault();
            e.stopPropagation(); // Prevent ComfyUI from showing its own drop indicator
            // Best-effort cursor cue: reject when the lane under the cursor is collapsed.
            // Browsers restrict `getData()` during dragover so we cannot reliably know
            // the asset type here — image drags over a collapsed non-Guides lane will
            // still show "no-drop" even though the drop would land on Guides; the
            // authoritative reject lives in _handleAssetDrop.
            const { rawY } = this._canvasMouseCoords(e);
            const layoutIdx = this._layoutIndexFromRawY(rawY);
            if (layoutIdx >= 0 && this._trackLayout[layoutIdx]?.collapsed) {
                e.dataTransfer.dropEffect = "none";
                return;
            }
            e.dataTransfer.dropEffect = "copy";
        });

        canvas.addEventListener("drop", (e) => {
            e.preventDefault();
            e.stopPropagation(); // Prevent ComfyUI from also handling this drop
            const assetData = e.dataTransfer.getData("application/x-sonder-asset");
            if (!assetData) {
                // #5 diagnostic: a drop reached the timeline canvas but lacks the asset payload.
                // Most common cause is the gallery being in manage mode (it then sets
                // `application/x-sonder-asset-move` only). Surface the situation rather than
                // silently doing nothing so the next repro identifies the seam.
                const types = Array.from(e.dataTransfer?.types || []);
                if (types.length > 0) {
                    console.debug("[Sonder] Timeline drop received non-asset payload; types:", types);
                    if (types.includes("application/x-sonder-asset-move")) {
                        this._showToast?.("Turn off Manage mode in the gallery to drop assets onto the timeline.");
                    }
                }
                return;
            }

            try {
                const asset = JSON.parse(assetData);
                const { x, rawY } = this._canvasMouseCoords(e);
                const frame = Math.max(0, this._xToFrame(x));

                this._handleAssetDrop(asset, frame, rawY);
            } catch (err) {
                console.warn("[Sonder] Drop failed:", err);
            }
        });

        // Double-click on Prompt track = create prompt section
        canvas.addEventListener("dblclick", (e) => {
            const { x, rawY } = this._canvasMouseCoords(e);
            const promptLayoutIdx = this._promptLayoutIdx();
            if (promptLayoutIdx >= 0 && this._layoutIndexFromRawY(rawY) === promptLayoutIdx) {
                if (this._isPromptTrackLocked()) return;
                const frame = Math.max(0, Math.min(this.totalFrames, this._xToFrame(x)));
                this._createPromptSection(frame);
            }
        });

        // Click on Prompt track = select prompt section
        canvas.addEventListener("click", (e) => {
            // Prompt deselection is handled by the unified mousedown/mouseup system
            // When clicking empty space, deselect prompt editor too
            const { rawY } = this._canvasMouseCoords(e);

            if (this._selectedPromptIdx !== null) {
                const promptLayoutIdx = this._promptLayoutIdx();
                if (promptLayoutIdx >= 0 && this._layoutIndexFromRawY(rawY) !== promptLayoutIdx) {
                    this._selectedPromptIdx = null;
                    this._hidePromptEditor();
                    this._renderTimeline();
                }
            }
        });

        // Right-click on timeline — custom context menu
        canvas.addEventListener("contextmenu", (e) => {
            e.preventDefault();

            const { x, rawY } = this._canvasMouseCoords(e);
            const frame = Math.max(0, this._xToFrame(x));

            const menuItems = [];

            // Check for track header right-click (lane management)
            const headerHit = this._hitTestTrackHeader(x, rawY);
            if (headerHit) {
                const entry = this._trackLayout[headerHit.layoutIdx];
                if (entry.type === TRACK_TYPE.GUIDES) {
                    this._showGuideManagementPopup(e.clientX, e.clientY);
                    return;
                }
                if (this._isLaneTrackType(entry.type)) {
                    const isVideo = entry.type === TRACK_TYPE.VIDEO;
                    const isMotionDriver = entry.type === TRACK_TYPE.MOTION_DRIVER;
                    const laneCount = isVideo
                        ? (this.activeScene?.video_lane_count || 1)
                        : isMotionDriver
                            ? (this.activeScene?.motion_driver_lane_count || 1)
                            : (this.activeScene?.audio_lane_count || 1);
                    const label = isVideo ? "Video" : (isMotionDriver ? "Motion Driver" : "Audio");

                    menuItems.push({ label: "Rename Lane", action: () => this._startLaneRename(headerHit.layoutIdx) });
                    if (!isMotionDriver) {
                        menuItems.push({ label: `Add ${label} Lane`, action: () => this._addLane(entry.type) });
                    }
                    if (!isMotionDriver && laneCount > 1) {
                        const hasItems = isVideo
                            ? (this.activeScene?.clips || []).some(c => this._isRenderClip(c) && (c.track_index || 0) === entry.laneIndex)
                            : (this.activeScene?.audio_tracks || []).some(a => (a.lane_index || 0) === entry.laneIndex);
                        if (hasItems) {
                            menuItems.push({ label: `Delete ${label} Lane and Move Items`, action: () => this._removeLaneWithItems(entry.type, entry.laneIndex), danger: true });
                            menuItems.push({ label: `Delete Items in ${label} Lane`, action: () => this._deleteItemsInLane(entry.type, entry.laneIndex), danger: true });
                        } else {
                            menuItems.push({ label: `Remove ${label} Lane`, action: () => this._removeLane(entry.type, entry.laneIndex), danger: true });
                        }
                    }
                }
                if (menuItems.length > 0) {
                    this._showContextMenu(e.clientX, e.clientY, menuItems);
                }
                return;
            }

            // Check for timeline item hits
            const hit = this._hitTestItem(x, rawY);
            if (hit) {
                if (!this._isSelected(hit.type, hit.id)) {
                    this._selectItem(hit);
                } else {
                    this._refreshSelectedHit(hit);
                }
                this._renderTimeline();

                const count = this.selectedItems.length;
                const itemLocked = this._isItemLocked(hit);
                if (count > 1) {
                    menuItems.push({ label: `Delete ${count} items`, action: itemLocked ? () => {} : () => this._deleteSelectedItems(), danger: true, disabled: itemLocked });
                } else if (hit.type === "clip") {
                    const clipAsset = this._getAssetForSourcePath(hit.data.source_path);
                    const isMotionDriverClip = this._isMotionDriverClip(hit.data);
                    const canConvertRole = isMotionDriverClip || clipAsset?.asset_type === "video";
                    menuItems.push({
                        label: itemLocked || isMotionDriverClip ? "Move to New Lane" + (itemLocked ? " (locked)" : " (unavailable)") : "Move to New Lane",
                        action: itemLocked || isMotionDriverClip ? () => {} : () => this._moveItemToNewLane(hit),
                        disabled: itemLocked || isMotionDriverClip,
                    });
                    menuItems.push({
                        label: !canConvertRole && !itemLocked ? "Convert to Motion Driver (video only)" : (isMotionDriverClip ? "Convert to Render Clip" : "Convert to Motion Driver"),
                        action: itemLocked || !canConvertRole
                            ? () => {}
                            : () => this._convertClipRole(hit.data.clip_id, isMotionDriverClip ? "render" : "motion_driver"),
                        disabled: itemLocked || !canConvertRole,
                    });
                    menuItems.push({
                        label: "Set Selection to Clip",
                        action: () => this._setSelectionToFrameRange(hit.data.timeline_start_frame || 0, hit.data.timeline_end_frame || 0),
                    });
                    const guidesLocked = this._isGuideTrackLocked();
                    menuItems.push({ label: guidesLocked ? "Add Frame to Guides (locked)" : "Add Frame to Guides", action: guidesLocked ? () => {} : () => this._addClipFrameToGuides(hit.data), disabled: guidesLocked });
                    if (clipAsset?.asset_id) {
                        menuItems.push({ label: "Inspect in Gallery", action: () => this._inspectAssetInGallery(clipAsset) });
                    }
                    if ((clipAsset?.width || 0) > 0 && (clipAsset?.height || 0) > 0) {
                        menuItems.push({
                            label: `Set Scene Aspect Ratio (${clipAsset.width}:${clipAsset.height})`,
                            action: () => this._setSceneAspectRatioFromDimensions(clipAsset.width, clipAsset.height),
                        });
                    }
                    // Extend scene to clip end
                    const clipEnd = hit.data.timeline_end_frame || 0;
                    const sceneDur = this.activeScene?.duration_frames || 0;
                    if (clipEnd > sceneDur) {
                        menuItems.push({ label: "Extend Scene to Clip End", action: () => this._updateSceneDuration(clipEnd) });
                    }
                    menuItems.push({ label: itemLocked ? "Delete Clip (locked)" : "Delete Clip", action: itemLocked ? () => {} : () => this._deleteSelectedItems(), danger: true, disabled: itemLocked });
                } else if (hit.type === "audio") {
                    menuItems.push({ label: itemLocked ? "Move to New Lane (locked)" : "Move to New Lane", action: itemLocked ? () => {} : () => this._moveItemToNewLane(hit), disabled: itemLocked });
                    // Extend scene to audio end
                    const audioEnd = hit.data.timeline_end_frame || 0;
                    const audioSceneDur = this.activeScene?.duration_frames || 0;
                    const audioAsset = this._getAssetForSourcePath(hit.data.source_path);
                    if (audioAsset?.asset_id) {
                        menuItems.push({ label: "Inspect in Gallery", action: () => this._inspectAssetInGallery(audioAsset) });
                    }
                    if (audioEnd > audioSceneDur) {
                        menuItems.push({ label: "Extend Scene to Audio End", action: () => this._updateSceneDuration(audioEnd) });
                    }
                    menuItems.push({ label: itemLocked ? "Delete Audio (locked)" : "Delete Audio Track", action: itemLocked ? () => {} : () => this._deleteSelectedItems(), danger: true, disabled: itemLocked });
                } else if (hit.type === "guide") {
                    const guideAsset = this._getGuideAsset(hit.data);
                    if (guideAsset?.asset_id) {
                        menuItems.push({ label: "Inspect in Gallery", action: () => this._inspectAssetInGallery(guideAsset) });
                    }
                    if ((guideAsset?.width || 0) > 0 && (guideAsset?.height || 0) > 0) {
                        menuItems.push({
                            label: `Set Scene Aspect Ratio (${guideAsset.width}:${guideAsset.height})`,
                            action: () => this._setSceneAspectRatioFromDimensions(guideAsset.width, guideAsset.height),
                        });
                    }
                    menuItems.push({ label: itemLocked ? "Delete Guide (locked)" : "Delete Guide", action: itemLocked ? () => {} : () => this._deleteSelectedItems(), danger: true, disabled: itemLocked });
                }
            }

            // Check prompt track
            const _pli3 = this._promptLayoutIdx();
            if (_pli3 >= 0 && this._layoutIndexFromRawY(rawY) === _pli3) {
                const sections = this.activeScene?.prompt_sections || [];
                const idx = sections.findIndex(s => frame >= s.start_frame && frame <= s.end_frame);
                if (idx >= 0) {
                    menuItems.push({ label: "Edit Prompt", action: () => {
                        this._selectedPromptIdx = idx;
                        this._showPromptEditor(sections[idx], idx);
                        this._renderTimeline();
                    }});
                    const promptLocked = this._isPromptTrackLocked();
                    menuItems.push({ label: promptLocked ? "Delete Prompt (locked)" : "Delete Prompt", action: promptLocked ? () => {} : () => {
                        if (confirm("Delete this prompt section?")) this._deletePromptSection(idx);
                    }, danger: true, disabled: promptLocked });
                }
            }

            if (menuItems.length > 0) {
                this._showContextMenu(e.clientX, e.clientY, menuItems);
            }
        });
    }

    async _handleAssetDrop(asset, frame, trackRawY) {
        if (!this.activeScene || !this.projectDir) return;
        if (asset?.asset_type === "artifact") {
            this._showToast("Artifact assets cannot be added to the timeline.");
            return;
        }

        // Take-aware drop: if asset has take_metadata, auto-place at original position
        if (asset.generation_params?.selection_start !== undefined && asset.generation_params?.scene_id === this.activeScene.scene_id) {
            frame = asset.generation_params.selection_start;
        }

        if (asset.asset_type === "image" && this._isGuideTrackLocked()) {
            this._showToast("Guides track is locked");
            return;
        }

        // #33: collapse is layout-only but the asset-drop hit-test should mirror
        // the item hit-tests, which already reject collapsed lanes. Image drops
        // route to Guides regardless of cursor lane, so check Guides collapse
        // for images; everything else is rejected against the cursor lane below.
        // Silent reject (no toast) — the dragover cursor is the user-visible cue.
        if (asset.asset_type === "image" && this._isGuideTrackCollapsed()) {
            return;
        }

        const dirName = this.projectDir.split(/[/\\]/).pop();

        // Determine drop target lane from Y position
        let targetVideoLane = 0;
        let targetAudioLane = 0;
        let targetMotionDriverLane = -1;
        if (trackRawY !== undefined) {
            const layoutIdx = this._layoutIndexFromRawY(trackRawY);
            if (layoutIdx >= 0) {
                const entry = this._trackLayout[layoutIdx];
                // #33: reject drops onto any collapsed non-image destination lane.
                // Image drops were checked against Guides above; for video/audio/
                // motion-driver we use the lane the cursor is over.
                if (asset.asset_type !== "image" && entry.collapsed) {
                    return;
                }
                if (entry.type === TRACK_TYPE.VIDEO) targetVideoLane = entry.laneIndex;
                if (entry.type === TRACK_TYPE.AUDIO) targetAudioLane = entry.laneIndex;
                if (entry.type === TRACK_TYPE.MOTION_DRIVER) targetMotionDriverLane = entry.laneIndex;
            }
        }

        if (targetMotionDriverLane >= 0) {
            if (asset.asset_type !== "video") {
                this._showToast("Motion drivers accept video assets only");
                return;
            }
            if (this._isLaneLocked(TRACK_TYPE.MOTION_DRIVER, targetMotionDriverLane)) return;
            this._pushUndo("add motion driver");
            const assetObj = this._findAssetById(asset.asset_id);
            const dropDuration = assetObj ? Math.max(1, assetObj.frame_count || 1) : 30;
            const dropEnd = frame + dropDuration;
            const hasOverlap = (this.activeScene.clips || []).some(c =>
                this._isMotionDriverClip(c) &&
                (c.track_index || 0) === targetMotionDriverLane &&
                c.timeline_start_frame < dropEnd && c.timeline_end_frame > frame
            );
            if (hasOverlap) {
                this._showToast("Only the earliest overlapping motion driver will be used.");
            }
            const tempClipId = `temp-driver-${Date.now().toString(36)}-${Math.random().toString(16).slice(2, 8)}`;
            this.activeScene.clips = this.activeScene.clips || [];
            this.activeScene.clips.push({
                clip_id: tempClipId,
                source_path: assetObj?.path || asset.path || "",
                timeline_start_frame: frame,
                timeline_end_frame: dropEnd,
                source_in_frame: 0,
                source_out_frame: dropDuration,
                total_source_frames: dropDuration,
                track_index: targetMotionDriverLane,
                role: "motion_driver",
                strength: this._defaultMotionDriverStrength(),
                muted: false,
            });
            this._renderSceneAfterLocalMutation();
            try {
                const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}/clips`), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        asset_id: asset.asset_id,
                        timeline_start_frame: frame,
                        track_index: targetMotionDriverLane,
                        role: "motion_driver",
                        strength: this._defaultMotionDriverStrength(),
                        dual_drop: false,
                    }),
                });
                if (!resp.ok) {
                    console.warn("[Sonder] Motion-driver clip creation failed:", resp.status, await resp.text());
                    this._discardLastUndo("add motion driver");
                    await this._fetchScenes({ ignoreMutationGate: true, reason: "drop_motion_driver_error" });
                    return;
                }
                const createdClip = await resp.json();
                const clipIdx = (this.activeScene.clips || []).findIndex((clip) => clip.clip_id === tempClipId);
                if (clipIdx >= 0) this.activeScene.clips[clipIdx] = createdClip;
                this._renderSceneAfterLocalMutation();
                this._deferProjectBackedRefresh(["scenes"], "motion_driver_drop_reconcile");
            } catch (e) {
                this._discardLastUndo("add motion driver");
                await this._fetchScenes({ ignoreMutationGate: true, reason: "drop_motion_driver_error" });
                console.warn("[Sonder] Failed to drop motion driver:", e);
            }
            return;
        }

        const _findAsset = (id) => {
            for (const type of ["video", "image", "audio", "artifact"]) {
                const found = (this.assets[type] || []).find(a => a.asset_id === id);
                if (found) return found;
            }
            return null;
        };

        // Block drop on locked lanes — only the lane the asset would actually land on.
        // Image drops always go to the Guides track (its own lock checked above at line 5800),
        // so the default targetVideoLane/targetAudioLane lock check must not run for images.
        // (#5/#14b 2026-05-21: image drops were getting blocked when V1 or A1 was locked
        //  because the lane defaults match that lane.)
        if (asset.asset_type === "video") {
            if (this._isLaneLocked(TRACK_TYPE.VIDEO, targetVideoLane)) return;
            const _assetObjForLock = _findAsset(asset.asset_id);
            const _videoHasAudio = _assetObjForLock?.has_audio === true || asset?.has_audio === true;
            if (_videoHasAudio && this._isLaneLocked(TRACK_TYPE.AUDIO, targetAudioLane)) return;
        } else if (asset.asset_type === "audio") {
            if (this._isLaneLocked(TRACK_TYPE.AUDIO, targetAudioLane)) return;
        }

        this._pushUndo("add asset");

        const persistSceneLaneCounts = async (fields, reason) => {
            try {
                const laneResp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(fields),
                });
                if (laneResp.ok) return true;
                const message = await laneResp.text();
                console.warn("[Sonder] Auto-add lane failed:", laneResp.status, message);
            } catch (error) {
                console.warn("[Sonder] Auto-add lane failed:", error);
            }
            this._discardLastUndo("add asset");
            await this._fetchScenes({ ignoreMutationGate: true, reason });
            return false;
        };

        const laneCountFields = {};

        // Auto-add lane if target lane has overlapping items at the drop frame
        if (asset.asset_type === "video") {
            const assetObj = _findAsset(asset.asset_id);
            const videoHasAudio = assetObj?.has_audio === true || asset?.has_audio === true;
            const dropDuration = assetObj ? Math.max(1, assetObj.frame_count || 1) : 30;
            const dropEnd = frame + dropDuration;
            const hasOverlap = (this.activeScene.clips || []).some(c =>
                this._isRenderClip(c) &&
                (c.track_index || 0) === targetVideoLane &&
                c.timeline_start_frame < dropEnd && c.timeline_end_frame > frame
            );
            if (hasOverlap) {
                // Auto-add a new video lane and place clip there
                const newCount = (this.activeScene.video_lane_count || 1) + 1;
                targetVideoLane = newCount - 1; // highest lane = top
                this.activeScene.video_lane_count = newCount;
                this._buildTrackLayout();
                this._renderTimeline();
                laneCountFields.video_lane_count = newCount;
            }
            // Only videos with embedded audio can create paired audio tracks.
            if (videoHasAudio) {
                const audioDuration = dropDuration; // video duration = audio duration
                const audioDropEnd = frame + audioDuration;
                const hasAudioOverlap = (this.activeScene.audio_tracks || []).some(a =>
                    (a.lane_index || 0) === targetAudioLane &&
                    a.timeline_start_frame < audioDropEnd && a.timeline_end_frame > frame
                );
                if (hasAudioOverlap) {
                    const newAudioCount = (this.activeScene.audio_lane_count || 1) + 1;
                    targetAudioLane = newAudioCount - 1;
                    this.activeScene.audio_lane_count = newAudioCount;
                    this._buildTrackLayout();
                    this._renderTimeline();
                    laneCountFields.audio_lane_count = newAudioCount;
                }
            }
        } else if (asset.asset_type === "audio") {
            const fps = this._effectiveFps;
            const assetObj = _findAsset(asset.asset_id);
            const dropDuration = assetObj ? Math.max(1, Math.round((assetObj.duration_sec || 1) * fps)) : 30;
            const dropEnd = frame + dropDuration;
            const hasOverlap = (this.activeScene.audio_tracks || []).some(a =>
                (a.lane_index || 0) === targetAudioLane &&
                a.timeline_start_frame < dropEnd && a.timeline_end_frame > frame
            );
            if (hasOverlap) {
                const newCount = (this.activeScene.audio_lane_count || 1) + 1;
                targetAudioLane = newCount - 1;
                this.activeScene.audio_lane_count = newCount;
                this._buildTrackLayout();
                this._renderTimeline();
                laneCountFields.audio_lane_count = newCount;
            }
        }

        if (Object.keys(laneCountFields).length > 0) {
            const lanesPersisted = await persistSceneLaneCounts(laneCountFields, "drop_lane_count_error");
            if (!lanesPersisted) return;
        }

        let resp;
        let optimisticClipId = "";
        let optimisticAudioId = "";
        let droppedVideoHasAudio = false;
        try {
            if (asset.asset_type === "image") {
                // Images always create guide frames (regardless of which track they're dropped on)
                const guideFields = {
                    frame_index: frame,
                    asset_id: asset.asset_id,
                    source: "asset",
                    strength: this._defaultGuideStrength(),
                };
                this._applyLocalCreateGuide(guideFields);
                this._renderSceneAfterLocalMutation();
                resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}/guides`), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(guideFields),
                });
                if (!resp.ok) {
                    console.warn("[Sonder] Guide creation failed:", resp.status, await resp.text());
                    this._discardLastUndo("add asset");
                    await this._fetchScenes({ ignoreMutationGate: true, reason: "drop_guide_error" });
                    return;
                }
                const guidePayload = await resp.json();
                if (guidePayload?.frame_index !== undefined) {
                    this._applyLocalCreateGuide(guidePayload);
                    this._renderSceneAfterLocalMutation();
                }
                console.log("[Sonder] Guide frame created at frame", frame);
            } else if (asset.asset_type === "video") {
                // Drop video = create clip on target video lane (+ audio track if video has audio)
                const assetObj = _findAsset(asset.asset_id);
                const videoHasAudio = assetObj?.has_audio === true || asset?.has_audio === true;
                droppedVideoHasAudio = videoHasAudio;
                const frameCount = Math.max(1, parseInt(assetObj?.frame_count ?? asset.frame_count ?? 0, 10) || 30);
                optimisticClipId = `temp-clip-${Date.now().toString(36)}-${Math.random().toString(16).slice(2, 8)}`;
                const optimisticClip = {
                    clip_id: optimisticClipId,
                    source_path: assetObj?.path || asset.path || "",
                    timeline_start_frame: frame,
                    timeline_end_frame: frame + frameCount,
                    source_in_frame: 0,
                    source_out_frame: frameCount,
                    total_source_frames: frameCount,
                    track_index: targetVideoLane,
                    role: "render",
                    opacity: 1.0,
                    muted: false,
                };
                this.activeScene.clips = this.activeScene.clips || [];
                this.activeScene.clips.push(optimisticClip);
                if (videoHasAudio) {
                    optimisticAudioId = `temp-audio-${Date.now().toString(36)}-${Math.random().toString(16).slice(2, 8)}`;
                    const optimisticAudio = {
                        track_id: optimisticAudioId,
                        source_path: "",
                        timeline_start_frame: frame,
                        timeline_end_frame: frame + frameCount,
                        source_in_frame: 0,
                        total_source_frames: frameCount,
                        lane_index: targetAudioLane,
                        volume: 1.0,
                        muted: false,
                    };
                    this.activeScene.audio_tracks = this.activeScene.audio_tracks || [];
                    this.activeScene.audio_tracks.push(optimisticAudio);
                }
                this._renderSceneAfterLocalMutation();
                const clipBody = {
                    asset_id: asset.asset_id,
                    timeline_start_frame: frame,
                    track_index: targetVideoLane,
                    audio_lane_index: targetAudioLane,
                    dual_drop: videoHasAudio,
                };
                resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}/clips`), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(clipBody),
                });
                if (!resp.ok) {
                    console.warn("[Sonder] Clip creation failed:", resp.status, await resp.text());
                    this._discardLastUndo("add asset");
                    await this._fetchScenes({ ignoreMutationGate: true, reason: "drop_clip_error" });
                    return;
                }
                const clipPayload = await resp.json();
                const { audio_track: createdAudioTrack, ...createdClip } = clipPayload || {};
                const clipIdx = (this.activeScene.clips || []).findIndex((clip) => clip.clip_id === optimisticClipId);
                if (clipIdx >= 0) {
                    this.activeScene.clips[clipIdx] = createdClip;
                }
                if (optimisticAudioId) {
                    if (createdAudioTrack) {
                        const audioIdx = (this.activeScene.audio_tracks || []).findIndex((track) => track.track_id === optimisticAudioId);
                        if (audioIdx >= 0) this.activeScene.audio_tracks[audioIdx] = createdAudioTrack;
                    } else {
                        this.activeScene.audio_tracks = (this.activeScene.audio_tracks || []).filter((track) => track.track_id !== optimisticAudioId);
                    }
                }
                this._renderSceneAfterLocalMutation();
            } else if (asset.asset_type === "audio") {
                // Drop audio = create audio track on target audio lane
                const assetObj = _findAsset(asset.asset_id);
                const fps = this._effectiveFps;
                const durationFrames = Math.max(1, Math.round((assetObj?.duration_sec || asset.duration_sec || 1) * fps));
                optimisticAudioId = `temp-audio-${Date.now().toString(36)}-${Math.random().toString(16).slice(2, 8)}`;
                const optimisticAudio = {
                    track_id: optimisticAudioId,
                    source_path: assetObj?.path || asset.path || "",
                    timeline_start_frame: frame,
                    timeline_end_frame: frame + durationFrames,
                    source_in_frame: 0,
                    total_source_frames: durationFrames,
                    lane_index: targetAudioLane,
                    volume: 1.0,
                    muted: false,
                };
                this.activeScene.audio_tracks = this.activeScene.audio_tracks || [];
                this.activeScene.audio_tracks.push(optimisticAudio);
                this._renderSceneAfterLocalMutation();
                resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}/audio_tracks`), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        asset_id: asset.asset_id,
                        timeline_start_frame: frame,
                        lane_index: targetAudioLane,
                    }),
                });
                if (!resp.ok) {
                    console.warn("[Sonder] Audio track creation failed:", resp.status, await resp.text());
                    this._discardLastUndo("add asset");
                    await this._fetchScenes({ ignoreMutationGate: true, reason: "drop_audio_error" });
                    return;
                }
                const audioPayload = await resp.json();
                const audioIdx = (this.activeScene.audio_tracks || []).findIndex((track) => track.track_id === optimisticAudioId);
                if (audioIdx >= 0) this.activeScene.audio_tracks[audioIdx] = audioPayload;
                this._renderSceneAfterLocalMutation();
            }

            if (droppedVideoHasAudio) {
                this._deferProjectBackedRefresh(["assets"], "dual_drop_asset_refresh");
            }
            this._deferProjectBackedRefresh(["scenes"], "asset_drop_reconcile");
        } catch (e) {
            this._discardLastUndo("add asset");
            await this._fetchScenes({ ignoreMutationGate: true, reason: "asset_drop_error" });
            console.warn("[Sonder] Failed to drop asset:", e);
        }
    }

    // ── Lane Management ────────────────────────────────────────────────
    _firstAvailableLane(type) {
        const count = type === TRACK_TYPE.VIDEO
            ? (this.activeScene?.video_lane_count || 1)
            : type === TRACK_TYPE.MOTION_DRIVER
                ? (this.activeScene?.motion_driver_lane_count || 1)
                : (this.activeScene?.audio_lane_count || 1);
        let visibleLane = -1;
        for (let i = 0; i < count; i++) {
            if (this._isLaneHidden(type, i)) continue;
            if (visibleLane < 0) visibleLane = i;
            if (!this._isLaneLocked(type, i)) return i;
        }
        return visibleLane;
    }

    async _convertClipRole(clipId, targetRole) {
        if (!this.activeScene || !this.projectDir || !clipId) return;
        const clip = (this.activeScene.clips || []).find(c => c.clip_id === clipId);
        if (!clip) return;
        const targetType = targetRole === "motion_driver" ? TRACK_TYPE.MOTION_DRIVER : TRACK_TYPE.VIDEO;
        if (targetRole === "motion_driver") {
            const sourceAsset = this._getAssetForSourcePath(clip.source_path);
            if (sourceAsset?.asset_type !== "video") {
                this._showToast("Motion drivers accept video assets only");
                return;
            }
        }
        const targetLane = this._firstAvailableLane(targetType);
        if (targetLane < 0 || this._isLaneHidden(targetType, targetLane)) {
            this._showToast(targetRole === "motion_driver" ? "No visible motion-driver lane available." : "No visible video lane available.");
            return;
        }
        if (this._isLaneLocked(targetType, targetLane)) {
            this._showToast("Target lane is locked.");
            return;
        }

        const oldState = {
            role: clip.role || "render",
            track_index: clip.track_index || 0,
            strength: clip.strength ?? 1.0,
        };
        const body = { role: targetRole, track_index: targetLane };
        if (targetRole === "motion_driver") {
            body.strength = this._defaultMotionDriverStrength();
        }

        this._pushUndo("convert clip role");
        Object.assign(clip, body);
        try {
            await this._runSceneMutation(
                [{ type: "update_clip", clip_id: clipId, fields: body }],
                {
                    key: `clip:${clipId}:role`,
                    label: "convert clip role",
                    coalesce: false,
                }
            );
            this._clearSelection();
            this._hideItemEditor();
            this._renderTimeline();
            this._renderViewportFrame();
        } catch (e) {
            Object.assign(clip, oldState);
            console.warn("[Sonder] Failed to convert clip role:", e);
            this._showToast("Failed to convert clip role.");
            this._renderTimeline();
        }
    }

    async _moveItemToNewLane(hit) {
        if (!this.activeScene || !this.projectDir) return;
        const sceneId = this.activeSceneId;
        this._pushUndo("move to new lane");

        try {
            const operations = [];
            if (hit.type === "clip") {
                if (this._isMotionDriverClip(hit.data)) {
                    this._showToast("Motion-driver lanes are single-lane in this phase.");
                    return;
                }
                // Add a new video lane and move clip there
                const newCount = (this.activeScene.video_lane_count || 1) + 1;
                const newLane = newCount - 1;
                operations.push({ type: "set_lane_count", lane_type: "video", count: newCount });
                operations.push({ type: "update_clip", clip_id: hit.id, fields: { track_index: newLane } });
            } else if (hit.type === "audio") {
                const newCount = (this.activeScene.audio_lane_count || 1) + 1;
                const newLane = newCount - 1;
                operations.push({ type: "set_lane_count", lane_type: "audio", count: newCount });
                operations.push({ type: "update_audio_track", track_id: hit.id, fields: { lane_index: newLane } });
            }
            if (operations.length > 0) {
                await this._runSceneMutation(operations, {
                    key: `scene:${sceneId}:move-item-new-lane:${Date.now()}`,
                    label: "move item to new lane",
                    coalesce: false,
                });
            }
            this._buildTrackLayout();
            this._renderTimeline();
        } catch (e) {
            console.warn("[Sonder] Failed to move item to new lane:", e);
        }
    }

    /** Check if a lane is locked */
    _isLaneLocked(type, laneIndex) {
        const idx = type === TRACK_TYPE.VIDEO
            ? this._videoLaneLayoutIdx(laneIndex)
            : type === TRACK_TYPE.MOTION_DRIVER
                ? this._motionDriverLaneLayoutIdx(laneIndex)
                : this._audioLaneLayoutIdx(laneIndex);
        return idx >= 0 && this._trackLayout[idx]?.locked;
    }

    /** Check if a lane is hidden */
    _isLaneHidden(type, laneIndex) {
        const idx = type === TRACK_TYPE.VIDEO
            ? this._videoLaneLayoutIdx(laneIndex)
            : type === TRACK_TYPE.MOTION_DRIVER
                ? this._motionDriverLaneLayoutIdx(laneIndex)
                : this._audioLaneLayoutIdx(laneIndex);
        return idx >= 0 && this._trackLayout[idx]?.hidden;
    }

    async _setTrackItemsMuted(entry, muted) {
        const items = this._trackItemsForEntry(entry).filter((item) => "muted" in item);
        const operations = [];
        for (const item of items) {
            if (!!item.muted === !!muted) continue;
            item.muted = !!muted;
            const type = entry.type === TRACK_TYPE.AUDIO
                ? "audio"
                : entry.type === TRACK_TYPE.GUIDES
                    ? "guide"
                    : "clip";
            const id = type === "audio"
                ? item.track_id
                : type === "guide"
                    ? item.frame_index
                    : item.clip_id;
            if (type === "clip") {
                operations.push({ type: "update_clip", clip_id: id, fields: { muted: !!muted } });
            } else if (type === "audio") {
                operations.push({ type: "update_audio_track", track_id: id, fields: { muted: !!muted } });
            } else {
                operations.push({
                    type: "update_guide",
                    frame_index: id,
                    expected: {
                        frame_index: item.frame_index,
                        asset_id: item.asset_id || "",
                    },
                    fields: { muted: !!muted },
                });
            }
        }
        if (operations.length > 0) {
            await this._runSceneMutation(operations, {
                key: `scene:${this.activeSceneId}:track-items-muted:${entry.type}:${entry.laneIndex ?? "track"}`,
                label: "track item mute",
                coalesce: false,
                refreshScenes: false,
            });
        }
    }

    async _toggleHeaderVisibility(entry) {
        if (!entry) return;
        const state = this._trackVisibilityState(entry);
        if (state === "visible") {
            entry.hidden = true;
            await this._saveLaneConfig();
        } else {
            const itemsMuted = this._trackItemsForEntry(entry).some((item) => !!item.muted);
            entry.hidden = false;
            await this._saveLaneConfig();
            if (itemsMuted && !entry.locked) {
                await this._setTrackItemsMuted(entry, false);
                await this._fetchScenes();
                this._reconcileSelection();
                this._buildTrackLayout();
            }
        }
        this._renderTimeline();
        this._renderViewportFrame();
        this._updateToolbar();
    }

    /** Start inline rename for a lane header */
    _startLaneRename(layoutIdx) {
        const entry = this._trackLayout[layoutIdx];
        const canvas = this.timelineCanvas;
        const rect = canvas.getBoundingClientRect();
        const headerW = this._labelW; // already scaled by _scaleTrackHeaders
        const ty = this._trackY(layoutIdx);
        const th = this._trackH(layoutIdx);

        const input = document.createElement("input");
        input.type = "text";
        input.value = entry.customName || "";
        input.placeholder = entry.label;
        input.style.cssText = `
            position: fixed;
            left: ${rect.left + 2}px;
            top: ${rect.top + ty + 1 - this.scrollY}px;
            width: ${headerW - 4}px;
            height: ${th - 2}px;
            font-size: ${Math.round(10 * this._scaleTrackHeaders)}px;
            background: #333;
            color: #fff;
            border: 1px solid #5af;
            padding: 0 3px;
            z-index: 10001;
            outline: none;
            box-sizing: border-box;
        `;

        const finish = (save) => {
            if (save) {
                const newName = input.value.trim();
                entry.customName = newName;
                entry.label = newName || (entry.type === TRACK_TYPE.VIDEO
                    ? ((this.activeScene?.video_lane_count || 1) > 1 ? `V${entry.laneIndex + 1}` : "Video")
                    : entry.type === TRACK_TYPE.MOTION_DRIVER
                        ? ((this.activeScene?.motion_driver_lane_count || 1) > 1 ? `MD${entry.laneIndex + 1}` : "Driver")
                        : ((this.activeScene?.audio_lane_count || 1) > 1 ? `A${entry.laneIndex + 1}` : "Audio"));
                this._saveLaneConfig();
                this._renderTimeline();
            }
            input.remove();
        };

        input.addEventListener("keydown", (e) => {
            if (e.key === "Enter") { e.preventDefault(); finish(true); }
            else if (e.key === "Escape") { e.preventDefault(); finish(false); }
            e.stopPropagation();
        });
        input.addEventListener("blur", () => finish(true));

        document.body.appendChild(input);
        input.focus();
        input.select();
    }

    /** Persist lane configs (lock/hide/name/color) to server from current _trackLayout */
    async _saveLaneConfig() {
        if (!this.activeScene || !this.projectDir) return;
        const sceneId = this.activeSceneId;
        const sceneRef = this.activeScene;
        const videoConfigs = [];
        const motionDriverConfigs = [];
        const audioConfigs = [];
        let guideTrackConfig = this._defaultLaneConfig();
        let promptTrackConfig = this._defaultLaneConfig();
        for (const e of this._trackLayout) {
            if (e.type === TRACK_TYPE.VIDEO) {
                videoConfigs[e.laneIndex] = { name: e.customName || "", color: e.color || "", locked: e.locked, hidden: e.hidden };
            } else if (e.type === TRACK_TYPE.MOTION_DRIVER) {
                motionDriverConfigs[e.laneIndex] = { name: e.customName || "", color: e.color || "", locked: e.locked, hidden: e.hidden };
            } else if (e.type === TRACK_TYPE.AUDIO) {
                audioConfigs[e.laneIndex] = { name: e.customName || "", color: e.color || "", locked: e.locked, hidden: e.hidden };
            } else if (e.type === TRACK_TYPE.GUIDES) {
                guideTrackConfig = { name: "", color: "", locked: !!e.locked, hidden: !!e.hidden };
            } else if (e.type === TRACK_TYPE.PROMPT) {
                promptTrackConfig = { name: "", color: "", locked: !!e.locked, hidden: !!e.hidden };
            }
        }
        // Fill any sparse gaps
        for (let i = 0; i < videoConfigs.length; i++) if (!videoConfigs[i]) videoConfigs[i] = { name: "", color: "", locked: false, hidden: false };
        for (let i = 0; i < motionDriverConfigs.length; i++) if (!motionDriverConfigs[i]) motionDriverConfigs[i] = { name: "", color: "", locked: false, hidden: false };
        for (let i = 0; i < audioConfigs.length; i++) if (!audioConfigs[i]) audioConfigs[i] = { name: "", color: "", locked: false, hidden: false };
        const fields = {
            video_lane_configs: videoConfigs,
            motion_driver_lane_configs: motionDriverConfigs,
            audio_lane_configs: audioConfigs,
            guide_track_config: guideTrackConfig,
            prompt_track_config: promptTrackConfig,
        };
        try {
            await this._runSceneMutation(
                [{ type: "update_lane_configs", fields }],
                {
                    key: `scene:${sceneId}:lane-config`,
                    label: "lane config",
                    coalesce: true,
                    refreshScenes: false,
                }
            );
            // Update local scene data
            if (sceneRef) {
                sceneRef.video_lane_configs = videoConfigs;
                sceneRef.motion_driver_lane_configs = motionDriverConfigs;
                sceneRef.audio_lane_configs = audioConfigs;
                sceneRef.guide_track_config = guideTrackConfig;
                sceneRef.prompt_track_config = promptTrackConfig;
            }
        } catch (e) {
            console.warn("[Sonder] Failed to save lane config:", e);
        }
    }

    async _addLane(trackType) {
        if (!this.activeScene || !this.projectDir) return;
        if (trackType === TRACK_TYPE.MOTION_DRIVER) {
            this._showToast("Motion-driver lanes are single-lane in this phase.");
            return;
        }
        const isVideo = trackType === TRACK_TYPE.VIDEO;
        const nextCount = isVideo
            ? (this.activeScene.video_lane_count || 1) + 1
            : (this.activeScene.audio_lane_count || 1) + 1;
        const undoLabel = "add lane";
        this._pushUndo(undoLabel);
        this._applyLocalSetLaneCount(isVideo ? "video" : "audio", nextCount);
        this._renderSceneAfterLocalMutation({ viewport: false });
        try {
            await this._runSceneMutation(
                [{ type: "set_lane_count", lane_type: isVideo ? "video" : "audio", count: nextCount }],
                {
                    key: `scene:${this.activeSceneId}:${isVideo ? "video" : "audio"}-lane-count`,
                    label: "add lane",
                    coalesce: false,
                }
            );
            this._buildTrackLayout();
            this._renderTimeline();
        } catch (e) {
            this._discardLastUndo(undoLabel);
            await this._fetchScenes({ ignoreMutationGate: true, reason: "add_lane_error" });
            console.warn("[Sonder] Failed to add lane:", e);
        }
    }

    async _removeLaneWithItems(trackType, laneIndex) {
        if (trackType === TRACK_TYPE.MOTION_DRIVER) {
            this._showToast("Motion-driver lanes are single-lane in this phase.");
            return;
        }
        const isVideo = trackType === TRACK_TYPE.VIDEO;
        const label = isVideo ? "video" : "audio";
        const items = isVideo
            ? (this.activeScene?.clips || []).filter(c => this._isRenderClip(c) && (c.track_index || 0) === laneIndex)
            : (this.activeScene?.audio_tracks || []).filter(a => (a.lane_index || 0) === laneIndex);
        const targetLane = laneIndex > 0 ? laneIndex - 1 : 1;
        const currentCount = isVideo
            ? (this.activeScene?.video_lane_count || 1)
            : (this.activeScene?.audio_lane_count || 1);

        // If target lane would be the same (only 1 lane) or no valid target, delete items instead
        const willMove = currentCount > 1 && targetLane !== laneIndex;
        const msg = willMove
            ? `This ${label} lane has ${items.length} item(s). Move them to lane ${targetLane} and remove this lane?`
            : `This ${label} lane has ${items.length} item(s). Delete them and remove this lane?`;
        if (!confirm(msg)) return;

        const undoLabel = "remove lane";
        const operation = {
            type: "remove_lane",
            lane_type: isVideo ? "video" : "audio",
            lane_index: laneIndex,
            item_policy: willMove ? "move_items" : "delete_items",
            target_lane: targetLane,
        };

        try {
            this._pushUndo(undoLabel);
            this._applyLocalRemoveLane(operation.lane_type, laneIndex, operation.item_policy, targetLane);
            this._clearSelection();
            this._hideItemEditor();
            this._renderSceneAfterLocalMutation();
            await this._runSceneMutation(
                [operation],
                {
                    key: `scene:${this.activeSceneId}:${isVideo ? "video" : "audio"}-remove-lane:${laneIndex}`,
                    label: "remove lane",
                    coalesce: false,
                }
            );
        } catch (e) {
            this._discardLastUndo(undoLabel);
            await this._fetchScenes({ ignoreMutationGate: true, reason: "remove_lane_error" });
            console.warn("[Sonder] Failed to remove lane with items:", e);
        }
    }

    async _deleteItemsInLane(trackType, laneIndex) {
        if (!this.activeScene || !this.projectDir) return;
        if (trackType === TRACK_TYPE.MOTION_DRIVER) {
            this._showToast("Motion-driver lanes are single-lane in this phase.");
            return;
        }
        const isVideo = trackType === TRACK_TYPE.VIDEO;
        const label = isVideo ? "video" : "audio";
        const items = isVideo
            ? (this.activeScene?.clips || []).filter(c => this._isRenderClip(c) && (c.track_index || 0) === laneIndex)
            : (this.activeScene?.audio_tracks || []).filter(a => (a.lane_index || 0) === laneIndex);
        if (!items.length) {
            this._showToast("Lane is already empty.");
            return;
        }
        if (!confirm(`Delete ${items.length} ${label} item(s) on lane ${laneIndex + 1}? The lane will remain.`)) return;

        const undoLabel = "delete lane items";
        const operation = {
            type: "bulk_delete_items",
            preserve_lanes: true,
            items: items.map((item) => ({
                type: isVideo ? "clip" : "audio",
                id: isVideo ? item.clip_id : item.track_id,
                preserve_lane: true,
            })),
        };

        try {
            this._pushUndo(undoLabel);
            this._applyLocalBulkDeleteItems(operation.items, { preserveLanes: true });
            this._clearSelection();
            this._hideItemEditor();
            this._renderSceneAfterLocalMutation();
            await this._runSceneMutation(
                [operation],
                {
                    key: `scene:${this.activeSceneId}:${isVideo ? "video" : "audio"}-delete-lane-items:${laneIndex}`,
                    label: "delete lane items",
                    coalesce: false,
                }
            );
        } catch (e) {
            this._discardLastUndo(undoLabel);
            await this._fetchScenes({ ignoreMutationGate: true, reason: "delete_lane_items_error" });
            console.warn("[Sonder] Failed to delete lane items:", e);
        }
    }

    async _removeLane(trackType, laneIndex) {
        if (!this.activeScene || !this.projectDir) return;
        if (trackType === TRACK_TYPE.MOTION_DRIVER) {
            this._showToast("Motion-driver lanes are single-lane in this phase.");
            return;
        }
        const isVideo = trackType === TRACK_TYPE.VIDEO;
        const currentCount = isVideo
            ? (this.activeScene.video_lane_count || 1)
            : (this.activeScene.audio_lane_count || 1);
        if (currentCount <= 1) return;
        const undoLabel = "remove lane";
        const operation = {
            type: "remove_lane",
            lane_type: isVideo ? "video" : "audio",
            lane_index: laneIndex,
            item_policy: "require_empty",
        };
        try {
            this._pushUndo(undoLabel);
            if (!this._applyLocalRemoveLane(operation.lane_type, laneIndex, operation.item_policy)) {
                this._discardLastUndo(undoLabel);
                return;
            }
            this._renderSceneAfterLocalMutation({ viewport: false });
            await this._runSceneMutation(
                [operation],
                {
                    key: `scene:${this.activeSceneId}:${isVideo ? "video" : "audio"}-remove-lane:${laneIndex}`,
                    label: "remove lane",
                    coalesce: false,
                }
            );
        } catch (e) {
            this._discardLastUndo(undoLabel);
            await this._fetchScenes({ ignoreMutationGate: true, reason: "remove_empty_lane_error" });
            console.warn("[Sonder] Failed to remove lane:", e);
        }
    }

    // ── Prompt Section Management ─────────────────────────────────────
    async _createPromptSection(frame) {
        if (!this.activeScene || !this.projectDir) return;

        // Use current selection if it exists, otherwise fill entire timeline
        let startFrame, endFrame;
        if (this.selectionStart < this.selectionEnd) {
            startFrame = this.selectionStart;
            endFrame = this.selectionEnd;
        } else {
            startFrame = 0;
            endFrame = this.totalFrames;
        }

        // Show inline editor for the new prompt section
        this._showPromptCreator(startFrame, endFrame);
    }

    _showPromptCreator(startFrame, endFrame) {
        if (this._isPromptTrackLocked()) return;
        this._hidePromptEditor();

        const editor = document.createElement("div");
        editor.style.cssText = `
            display: flex; gap: 4px; padding: 4px 6px;
            background: ${COLORS.panel}; border-top: 1px solid ${COLORS.promptBorder};
            align-items: center;
        `;

        const label = document.createElement("span");
        label.style.cssText = `font-size: 10px; color: ${COLORS.promptBorder}; white-space: nowrap;`;
        label.textContent = `New [${startFrame}-${endFrame}]:`;

        const input = document.createElement("input");
        input.type = "text";
        input.placeholder = "Enter prompt for this section...";
        input.style.cssText = `flex: 1; ${chromeInputCss({ fontSize: "11px", padding: "3px 6px", textAlign: "left" })}`;
        input.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && input.value.trim()) {
                this._saveNewPromptSection(startFrame, endFrame, input.value.trim());
            } else if (e.key === "Escape") {
                this._hidePromptEditor();
            }
            e.stopPropagation();
        });

        const createBtn = this._makeBtn("Create", "Create prompt section");
        setButtonVariant(createBtn, "primary");
        createBtn.dataset.sonderHoverVariant = "primary";
        createBtn.addEventListener("click", () => {
            if (input.value.trim()) {
                this._saveNewPromptSection(startFrame, endFrame, input.value.trim());
            }
        });

        const cancelBtn = this._makeBtn("Cancel", "Cancel");
        setButtonVariant(cancelBtn, "subtle");
        cancelBtn.dataset.sonderHoverVariant = "subtle";
        cancelBtn.addEventListener("click", () => this._hidePromptEditor());

        editor.append(label, input, createBtn, cancelBtn);
        this.timelineCanvas.parentElement.insertBefore(editor, this.timelineCanvas.nextSibling);
        this._promptEditorEl = editor;
        this._refreshTimelineLayout();

        setTimeout(() => input.focus(), 50);
    }

    async _saveNewPromptSection(startFrame, endFrame, promptText) {
        if (!this.activeScene || !this.projectDir) return;
        if (this._isPromptTrackLocked()) return;
        const undoLabel = "add prompt";
        const fields = {
            start_frame: startFrame,
            end_frame: endFrame,
            prompt: promptText,
        };
        this._pushUndo(undoLabel);
        this._applyLocalPromptCreate(fields);
        this._hidePromptEditor();
        this._renderSceneAfterLocalMutation({ viewport: false });

        try {
            await this._runSceneMutation(
                [{ type: "create_prompt_section", fields }],
                {
                    key: `prompt:${this.activeSceneId}:create:${Date.now()}`,
                    label: "add prompt",
                    coalesce: false,
                }
            );
        } catch (e) {
            this._discardLastUndo(undoLabel);
            await this._fetchScenes({ ignoreMutationGate: true, reason: "add_prompt_error" });
            console.warn("[Sonder] Failed to create prompt section:", e);
        }
    }

    _showPromptEditor(section, idx) {
        if (this._isPromptTrackLocked()) return;
        this._hidePromptEditor();

        const editor = document.createElement("div");
        editor.style.cssText = `
            display: flex; gap: 4px; padding: 4px 6px;
            background: ${COLORS.panel}; border-top: 1px solid ${COLORS.promptBorder};
            align-items: center;
        `;

        const label = document.createElement("span");
        label.style.cssText = `font-size: 10px; color: ${COLORS.promptBorder}; white-space: nowrap;`;
        label.textContent = `Prompt [${section.start_frame}-${section.end_frame}]:`;

        const input = document.createElement("input");
        input.type = "text";
        input.value = section.prompt;
        input.style.cssText = `flex: 1; ${chromeInputCss({ fontSize: "11px", padding: "3px 6px", textAlign: "left" })}`;
        input.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                this._updatePromptSection(idx, { prompt: input.value });
                this._hidePromptEditor();
            } else if (e.key === "Escape") {
                this._hidePromptEditor();
                this._selectedPromptIdx = null;
                this._renderTimeline();
            }
            e.stopPropagation();
        });

        const saveBtn = this._makeBtn("Save", "Save prompt");
        setButtonVariant(saveBtn, "primary");
        saveBtn.dataset.sonderHoverVariant = "primary";
        saveBtn.addEventListener("click", () => {
            this._updatePromptSection(idx, { prompt: input.value });
            this._hidePromptEditor();
        });

        const deleteBtn = this._makeBtn("Delete", "Delete this prompt section");
        setButtonVariant(deleteBtn, "danger");
        deleteBtn.dataset.sonderHoverVariant = "danger";
        deleteBtn.addEventListener("click", () => {
            if (confirm(`Delete this prompt section?`)) {
                this._deletePromptSection(idx);
            }
        });

        editor.append(label, input, saveBtn, deleteBtn);
        // Insert after timeline canvas
        this.timelineCanvas.parentElement.insertBefore(editor, this.timelineCanvas.nextSibling);
        this._promptEditorEl = editor;
        this._refreshTimelineLayout();

        // Focus input
        setTimeout(() => input.focus(), 50);
    }

    _hidePromptEditor() {
        if (this._promptEditorEl) {
            this._promptEditorEl.remove();
            this._promptEditorEl = null;
            this._refreshTimelineLayout();
        }
    }

    async _updatePromptSection(idx, updates) {
        if (!this.activeScene || !this.projectDir) return;
        if (this._isPromptTrackLocked()) return;
        const undoLabel = "edit prompt";
        this._pushUndo(undoLabel);
        const section = (this.activeScene.prompt_sections || [])[idx];
        const expected = section ? {
            start_frame: section.start_frame,
            end_frame: section.end_frame,
        } : undefined;
        this._applyLocalPromptUpdate(idx, { ...updates });
        this._selectedPromptIdx = null;
        this._renderSceneAfterLocalMutation({ viewport: false });

        try {
            await this._runSceneMutation(
                [{
                    type: "update_prompt_section",
                    index: idx,
                    expected,
                    fields: { ...updates },
                }],
                {
                    key: `prompt:${this.activeSceneId}:${idx}:fields:${Object.keys(updates || {}).sort().join("-")}`,
                    label: "edit prompt",
                    coalesce: true,
                    merge: (oldIntent, nextIntent) => {
                        if (!oldIntent?.operations?.[0] || !nextIntent?.operations?.[0]) return nextIntent;
                        return {
                            ...nextIntent,
                            operations: [{
                                ...nextIntent.operations[0],
                                fields: {
                                    ...(oldIntent.operations[0].fields || {}),
                                    ...(nextIntent.operations[0].fields || {}),
                                },
                            }],
                        };
                    },
                }
            );
        } catch (e) {
            this._discardLastUndo(undoLabel);
            await this._fetchScenes({ ignoreMutationGate: true, reason: "edit_prompt_error" });
            console.warn("[Sonder] Failed to update prompt section:", e);
        }
    }

    async _deletePromptSection(idx) {
        if (!this.activeScene || !this.projectDir) return;
        if (this._isPromptTrackLocked()) return;
        const undoLabel = "delete prompt";
        this._pushUndo(undoLabel);
        const section = (this.activeScene.prompt_sections || [])[idx];
        this._applyLocalPromptDelete(idx);
        this._selectedPromptIdx = null;
        this._hidePromptEditor();
        this._renderSceneAfterLocalMutation({ viewport: false });

        try {
            await this._runSceneMutation(
                [{
                    type: "delete_prompt_section",
                    index: idx,
                    expected: section ? {
                        start_frame: section.start_frame,
                        end_frame: section.end_frame,
                    } : undefined,
                }],
                {
                    key: `prompt:${this.activeSceneId}:${idx}:delete`,
                    label: "delete prompt",
                    coalesce: false,
                }
            );
        } catch (e) {
            this._discardLastUndo(undoLabel);
            await this._fetchScenes({ ignoreMutationGate: true, reason: "delete_prompt_error" });
            console.warn("[Sonder] Failed to delete prompt section:", e);
        }
    }

    // ── Item Properties Editor ──────────────────────────────────────────
    _showItemEditor() {
        this._hideItemEditor();
        if (!this.selectedItem) return;

        const { type, id, data } = this.selectedItem;
        const isMotionDriverClip = type === "clip" && this._isMotionDriverClip(data);
        const editorAccent = type === "clip"
            ? (isMotionDriverClip ? COLORS.motionDriverSelected : COLORS.clipSelected)
            : type === "audio"
                ? COLORS.audioClipSelected
                : COLORS.guideSelected;

        const editor = document.createElement("div");
        editor.style.cssText = `
            display: flex; gap: 6px; padding: 4px 6px;
            background: ${COLORS.panel}; border-top: 1px solid ${editorAccent};
            align-items: center; flex-wrap: wrap;
        `;

        const typeLabel = document.createElement("span");
        typeLabel.style.cssText = `font-size: 10px; color: ${editorAccent}; white-space: nowrap; font-weight: bold;`;
        typeLabel.textContent = type === "clip" ? (isMotionDriverClip ? "Motion Driver" : "Video Clip") : type === "audio" ? "Audio Track" : "Guide Frame";
        editor.appendChild(typeLabel);

        if (type === "clip" || type === "audio") {
            const startFrame = data.timeline_start_frame;
            const endFrame = data.timeline_end_frame;
            const duration = endFrame - startFrame;

            // Start frame input
            const startLabel = this._makeEditorLabel(this._timecodeMode === "timecode" ? "Start (s):" : "Start:");
            const startInput = this._makeEditorInput(startFrame, 0, this.totalFrames);
            if (this._timecodeMode === "timecode") {
                startInput.type = "text";
                startInput.inputMode = "decimal";
                startInput.value = this._formatPositionInput(startFrame);
            }
            editor.append(startLabel, startInput);

            // Duration display
            const durLabel = this._makeEditorLabel(`Duration: ${this._frameToTimecode(duration)}`);
            durLabel.style.color = COLORS.textDim;
            editor.appendChild(durLabel);

            // Opacity (clips) or Volume (audio)
            if (type === "clip") {
                if (isMotionDriverClip) {
                    const strengthLabel = this._makeEditorLabel("Strength:");
                    const strengthInput = this._makeEditorInput((data.strength ?? 1.0).toFixed(2), 0, 1);
                    strengthInput.step = "0.05";
                    strengthInput.addEventListener("change", () => {
                        const strength = Math.max(0, Math.min(1, parseFloat(strengthInput.value)));
                        if (Number.isFinite(strength)) {
                            data.strength = strength;
                            strengthInput.value = strength.toFixed(2);
                            this._updateItemProperty(type, id, { strength });
                        }
                    });
                    editor.append(strengthLabel, strengthInput);
                } else {
                    const opLabel = this._makeEditorLabel("Opacity:");
                    const opInput = document.createElement("input");
                    opInput.type = "range";
                    opInput.min = 0; opInput.max = 100; opInput.step = 5;
                    opInput.value = Math.round((data.opacity ?? 1.0) * 100);
                    opInput.style.cssText = `width: 60px; height: 14px; cursor: pointer; accent-color: ${COLORS.clip};`;
                    opInput.title = `Opacity: ${opInput.value}%`;
                    const opVal = this._makeEditorLabel(`${opInput.value}%`);
                    opVal.style.minWidth = "28px";
                    opInput.addEventListener("input", () => {
                        opVal.textContent = `${opInput.value}%`;
                        opInput.title = `Opacity: ${opInput.value}%`;
                        data.opacity = parseInt(opInput.value) / 100;
                        this._renderTimeline();
                    });
                    opInput.addEventListener("change", () => {
                        this._updateItemProperty(type, id, { opacity: parseInt(opInput.value) / 100 });
                    });
                    editor.append(opLabel, opInput, opVal);
                }
                const clipMuteBtn = this._makeBtn(data.muted ? "Hidden" : "Visible", "Toggle clip visibility");
                clipMuteBtn.addEventListener("click", () => {
                    this._pushUndo("toggle clip mute");
                    data.muted = !data.muted;
                    clipMuteBtn.textContent = data.muted ? "Hidden" : "Visible";
                    this._updateItemProperty(type, id, { muted: data.muted });
                    this._renderTimeline();
                    this._renderViewportFrame();
                });
                editor.appendChild(clipMuteBtn);
            } else {
                // Volume slider for audio
                const volLabel = this._makeEditorLabel("Vol:");
                const volInput = document.createElement("input");
                volInput.type = "range";
                volInput.min = 0; volInput.max = 100; volInput.step = 5;
                volInput.value = Math.round((data.volume ?? 1.0) * 100);
                volInput.style.cssText = `width: 60px; height: 14px; cursor: pointer; accent-color: ${COLORS.audioClip};`;
                volInput.title = `Volume: ${volInput.value}%`;
                const volVal = this._makeEditorLabel(`${volInput.value}%`);
                volVal.style.minWidth = "28px";
                volInput.addEventListener("input", () => {
                    volVal.textContent = `${volInput.value}%`;
                    volInput.title = `Volume: ${volInput.value}%`;
                    data.volume = parseInt(volInput.value) / 100;
                    this._renderTimeline();
                });
                volInput.addEventListener("change", () => {
                    this._updateItemProperty(type, id, { volume: parseInt(volInput.value) / 100 });
                });

                // Mute toggle
                const muteBtn = this._makeBtn(data.muted ? "🔇" : "🔊", "Toggle mute");
                muteBtn.addEventListener("click", () => {
                    this._pushUndo("toggle mute");
                    data.muted = !data.muted;
                    muteBtn.textContent = data.muted ? "🔇" : "🔊";
                    this._updateItemProperty(type, id, { muted: data.muted });
                    this._renderTimeline();
                    this._renderViewportFrame();
                });
                editor.append(volLabel, volInput, volVal, muteBtn);
            }

            // Apply button
            const applyBtn = this._makeBtn("Apply", "Apply guide changes");
            applyBtn.addEventListener("click", () => {
                const newStart = this._parsePositionInput(startInput.value);
                if (!isNaN(newStart) && newStart >= 0) {
                    this._moveItemToFrame(type, id, data, newStart);
                }
            });
            editor.appendChild(applyBtn);

            // Enter key in input
            startInput.addEventListener("keydown", (e) => {
                if (e.key === "Enter") {
                    const newStart = this._parsePositionInput(startInput.value);
                    if (!isNaN(newStart) && newStart >= 0) {
                        this._moveItemToFrame(type, id, data, newStart);
                    }
                } else if (e.key === "Escape") {
                    this._hideItemEditor();
                    this._clearSelection();
                    this._renderTimeline();
                }
                e.stopPropagation();
            });

        } else if (type === "guide") {
            let idx = data.frame_index;
            if (idx === -1) idx = this.totalFrames - 1;

            // Frame index input
            const frameLabel = this._makeEditorLabel(this._timecodeMode === "timecode" ? "Frame (s):" : "Frame:");
            const frameInput = this._makeEditorInput(idx, 0, this.totalFrames - 1);
            if (this._timecodeMode === "timecode") {
                frameInput.type = "text";
                frameInput.inputMode = "decimal";
                frameInput.value = this._formatPositionInput(idx);
            }
            editor.append(frameLabel, frameInput);

            const strengthLabel = this._makeEditorLabel("Strength:");
            const strengthInput = this._makeEditorInput((data.strength ?? 1.0).toFixed(2), 0, 1);
            strengthInput.step = "0.05";
            editor.append(strengthLabel, strengthInput);

            const guideAsset = this._getGuideAsset(data);
            const thumbUrl = guideAsset && !guideAsset.missing ? this._buildViewURL(guideAsset.path) : null;
            if (thumbUrl) {
                const thumb = document.createElement("img");
                thumb.src = thumbUrl;
                thumb.alt = "";
                thumb.title = guideAsset.name || "Guide asset";
                thumb.style.cssText = "width:32px;height:20px;object-fit:cover;border-radius:3px;border:1px solid rgba(255,255,255,0.18);background:#000;";
                editor.appendChild(thumb);
            }

            const guideMuteBtn = this._makeBtn(data.muted ? "Hidden" : "Visible", "Toggle guide visibility");
            guideMuteBtn.addEventListener("click", () => {
                this._pushUndo("toggle guide mute");
                data.muted = !data.muted;
                guideMuteBtn.textContent = data.muted ? "Hidden" : "Visible";
                this._updateItemProperty(type, id, { muted: data.muted });
                this._renderTimeline();
                this._renderViewportFrame();
            });
            editor.appendChild(guideMuteBtn);

            const applyGuideEdit = () => {
                const newIdx = this._parsePositionInput(frameInput.value);
                const strength = Math.max(0, Math.min(1, parseFloat(strengthInput.value)));
                if (!isNaN(newIdx) && newIdx >= 0 && !isNaN(strength)) {
                    this._moveGuideToFrame(data, newIdx, strength);
                }
            };

            // Apply button
            const applyBtn = this._makeBtn("Apply", "Apply guide changes");
            applyBtn.addEventListener("click", applyGuideEdit);
            editor.appendChild(applyBtn);

            // Enter key in input
            frameInput.addEventListener("keydown", (e) => {
                if (e.key === "Enter") {
                    applyGuideEdit();
                } else if (e.key === "Escape") {
                    this._hideItemEditor();
                    this._clearSelection();
                    this._renderTimeline();
                }
                e.stopPropagation();
            });
            strengthInput.addEventListener("keydown", (e) => {
                if (e.key === "Enter") {
                    applyGuideEdit();
                } else if (e.key === "Escape") {
                    this._hideItemEditor();
                    this._clearSelection();
                    this._renderTimeline();
                }
                e.stopPropagation();
            });
        }

        // Delete button (always present)
        const deleteBtn = this._makeBtn("Delete", "Delete this item");
        setButtonVariant(deleteBtn, "danger");
        deleteBtn.dataset.sonderHoverVariant = "danger";
        deleteBtn.addEventListener("click", () => this._deleteSelectedItems());
        editor.appendChild(deleteBtn);

        // Insert after timeline canvas
        this.timelineCanvas.parentElement.insertBefore(editor, this.timelineCanvas.nextSibling);
        this._itemEditorEl = editor;
        this._refreshTimelineLayout();
    }

    _hideItemEditor() {
        if (this._itemEditorEl) {
            this._itemEditorEl.remove();
            this._itemEditorEl = null;
            this._refreshTimelineLayout();
        }
    }

    _refreshTimelineLayout() {
        if (this.isFullscreen) {
            this._recalcFullscreenHeights();
        }
        this._renderTimeline();
    }

    _makeEditorLabel(text) {
        const label = document.createElement("span");
        label.style.cssText = `font-size: 10px; color: ${COLORS.text}; white-space: nowrap;`;
        label.textContent = text;
        return label;
    }

    _makeEditorInput(value, min, max) {
        const input = document.createElement("input");
        input.type = "number";
        input.value = value;
        input.min = min;
        input.max = max;
        input.style.cssText = chromeInputCss({ width: "60px", fontSize: "11px", padding: "2px 4px", textAlign: "right" });
        return input;
    }

    async _moveItemToFrame(type, id, data, newStart) {
        if (!this.activeScene || !this.projectDir) return;
        this._pushUndo("move item");
        const operation = type === "clip"
            ? { type: "update_clip", clip_id: id, fields: { timeline_start_frame: newStart } }
            : { type: "update_audio_track", track_id: id, fields: { timeline_start_frame: newStart } };

        try {
            await this._runSceneMutation([operation], {
                key: `${type}:${id}:timeline`,
                label: "move item",
                coalesce: false,
            });
            this._clearSelection();
            this._hideItemEditor();
            this._renderTimeline();
        } catch (e) {
            console.warn("[Sonder] Failed to move item:", e);
        }
    }

    async _updateItemProperty(type, id, props, { refresh = true } = {}) {
        if (!this.activeScene || !this.projectDir) return;
        let operation;
        if (type === "clip") {
            operation = { type: "update_clip", clip_id: id, fields: { ...props } };
        } else if (type === "guide") {
            const frameIndex = parseInt(id, 10);
            const guide = (this.activeScene.guide_frames || []).find((g) => (g.frame_index || 0) === frameIndex);
            operation = {
                type: "update_guide",
                frame_index: frameIndex,
                expected: guide ? {
                    frame_index: guide.frame_index,
                    asset_id: guide.asset_id || "",
                } : undefined,
                fields: { ...props },
            };
        } else {
            operation = { type: "update_audio_track", track_id: id, fields: { ...props } };
        }
        const fieldNames = Object.keys(props || {}).sort();
        const key = fieldNames.length === 1
            ? `${type}:${id}:field:${fieldNames[0]}`
            : `${type}:${id}:fields:${fieldNames.join("-")}`;
        const merge = (oldIntent, nextIntent) => {
            if (!oldIntent?.operations?.[0] || !nextIntent?.operations?.[0]) return nextIntent;
            return {
                ...nextIntent,
                operations: [{
                    ...nextIntent.operations[0],
                    fields: {
                        ...(oldIntent.operations[0].fields || {}),
                        ...(nextIntent.operations[0].fields || {}),
                    },
                }],
            };
        };

        try {
            await this._runSceneMutation([operation], {
                key,
                label: "item property",
                coalesce: true,
                merge,
                refreshScenes: refresh,
            });
            if (refresh) {
                this._renderTimeline();
                this._renderViewportFrame();
            }
        } catch (e) {
            console.warn("[Sonder] Failed to update item property:", e);
        }
    }

    async _toggleSelectedMute() {
        const targets = this.selectedItems
            .filter((item) => item?.type === "clip" || item?.type === "audio" || item?.type === "guide")
            .filter((item) => !this._isItemLocked(item));
        if (!targets.length) return;

        const nextMuted = !targets.every((item) => !!item.data?.muted);
        this._pushUndo(nextMuted ? "mute items" : "unmute items");
        const operations = [];
        for (const item of targets) {
            item.data.muted = nextMuted;
            if (item.type === "clip") {
                operations.push({ type: "update_clip", clip_id: item.id, fields: { muted: nextMuted } });
            } else if (item.type === "audio") {
                operations.push({ type: "update_audio_track", track_id: item.id, fields: { muted: nextMuted } });
            } else if (item.type === "guide") {
                operations.push({
                    type: "update_guide",
                    frame_index: item.id,
                    expected: {
                        frame_index: item.data?.frame_index ?? item.id,
                        asset_id: item.data?.asset_id || "",
                    },
                    fields: { muted: nextMuted },
                });
            }
        }
        await this._runSceneMutation(operations, {
            key: `scene:${this.activeSceneId}:selected-mute`,
            label: nextMuted ? "mute items" : "unmute items",
            coalesce: false,
        });
        this._reconcileSelection();
        if (this._itemEditorEl && this.selectedItem) {
            this._showItemEditor();
        }
        this._renderTimeline();
        this._renderViewportFrame();
        this._updateToolbar();
    }

    async _moveGuideToFrame(guideData, newIdx, strength = guideData?.strength ?? 1.0) {
        if (!this.activeScene || !this.projectDir) return;
        if (this._isGuideTrackLocked()) return;
        const undoLabel = "move guide";
        this._pushUndo(undoLabel);
        const oldIdx = guideData.frame_index;
        const fields = {
            asset_id: guideData.asset_id,
            source: guideData.source || "asset",
            strength,
            muted: !!guideData.muted,
        };
        this._applyLocalMoveGuide(oldIdx, newIdx, guideData, fields);
        this._clearSelection();
        this._hideItemEditor();
        this._renderSceneAfterLocalMutation();

        try {
            await this._runSceneMutation(
                [{
                    type: "move_guide",
                    from_frame_index: oldIdx,
                    to_frame_index: newIdx,
                    expected: {
                        frame_index: oldIdx,
                        asset_id: guideData.asset_id || "",
                    },
                    ...fields,
                }],
                {
                    key: `guide:${this.activeSceneId}:${oldIdx}:move`,
                    label: "move guide",
                    coalesce: false,
                }
            );
        } catch (e) {
            this._discardLastUndo(undoLabel);
            await this._fetchScenes({ ignoreMutationGate: true, reason: "move_guide_error" });
            console.warn("[Sonder] Failed to move guide:", e);
        }
    }

    // ── Item Delete / Move ──────────────────────────────────────────────
    _guideSnapshotMaxLongEdge() {
        const value = Number(this._settings?.guides?.guideSnapshotMaxLongEdge ?? 0);
        return Number.isFinite(value) ? Math.max(0, Math.round(value)) : 0;
    }

    _guideHoverPreviewEnabled() {
        return this._settings?.guides?.hoverPreviewEnabled ?? true;
    }

    _guideHoverPreviewSize() {
        const value = Number(this._settings?.guides?.hoverPreviewSize ?? DEFAULT_EDITOR_SETTINGS.guides.hoverPreviewSize);
        if (!Number.isFinite(value)) return DEFAULT_EDITOR_SETTINGS.guides.hoverPreviewSize;
        return Math.max(96, Math.min(360, Math.round(value)));
    }

    _hideGuideHoverPreview() {
        if (this._guidePreviewEl) {
            this._guidePreviewEl.remove();
            this._guidePreviewEl = null;
        }
    }

    _showGuideHoverPreview(guide, clientX, clientY) {
        if (!guide || !this._guideHoverPreviewEnabled()) {
            this._hideGuideHoverPreview();
            return;
        }

        const size = this._guideHoverPreviewSize();
        const frame = guide.frame_index === -1 ? Math.max(0, this.totalFrames - 1) : guide.frame_index;
        const asset = this._getGuideAsset(guide);
        const hidden = this._isGuideTrackHidden() || !!guide.muted;
        const url = asset && !asset.missing && asset.path ? this._buildViewURL(asset.path) : "";
        const name = asset?.name || asset?.path?.split(/[/\\]/).pop() || guide.asset_id || "Guide";

        let preview = this._guidePreviewEl;
        if (!preview) {
            preview = document.createElement("div");
            preview.style.cssText = `
                position: fixed; z-index: 10030; pointer-events: none;
                border-radius: 8px; overflow: hidden;
                box-shadow: 0 18px 42px rgba(0,0,0,0.52);
                font-family: ${FONT.sans};
            `;
            document.body.appendChild(preview);
            this._guidePreviewEl = preview;
        }

        preview.innerHTML = "";
        preview.style.width = `${size}px`;
        preview.style.background = hidden ? "rgba(31, 25, 20, 0.98)" : "rgba(15, 19, 24, 0.98)";
        preview.style.border = hidden ? `1px solid ${COLORS.warningBorder}` : `1px solid ${COLORS.borderStrong}`;
        preview.style.opacity = hidden ? "0.88" : "1";

        const imageWrap = document.createElement("div");
        imageWrap.style.cssText = `
            width: ${size}px; height: ${Math.round(size * 0.62)}px;
            background: #05070a; display: flex; align-items: center; justify-content: center;
            position: relative;
        `;
        if (url) {
            const img = document.createElement("img");
            img.src = url;
            img.alt = "";
            img.style.cssText = "max-width:100%;max-height:100%;object-fit:contain;display:block;";
            imageWrap.appendChild(img);
        } else {
            const missing = document.createElement("div");
            missing.textContent = asset?.missing ? "Missing asset" : "No thumbnail";
            missing.style.cssText = `font-size:11px;color:${COLORS.textMuted};`;
            imageWrap.appendChild(missing);
        }
        if (hidden) {
            const badge = document.createElement("div");
            badge.textContent = "Hidden";
            badge.style.cssText = `
                position:absolute;left:8px;top:8px;padding:3px 7px;border-radius:999px;
                background:rgba(0,0,0,0.68);border:1px solid ${COLORS.warningBorder};
                color:${COLORS.warningText};font-size:10px;font-weight:700;
            `;
            imageWrap.appendChild(badge);
        }

        const meta = document.createElement("div");
        meta.style.cssText = "padding:8px 10px;display:flex;align-items:center;justify-content:space-between;gap:10px;";
        const label = document.createElement("div");
        label.textContent = name;
        label.style.cssText = `min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;color:${COLORS.text};`;
        const frameLabel = document.createElement("div");
        frameLabel.textContent = `f${frame}`;
        frameLabel.style.cssText = `flex-shrink:0;font-size:11px;color:${COLORS.guideSelected};font-family:${FONT.mono};`;
        meta.append(label, frameLabel);

        preview.append(imageWrap, meta);

        const margin = 12;
        const estimatedHeight = Math.round(size * 0.62) + 38;
        let left = clientX + 18;
        let top = clientY - estimatedHeight - 14;
        if (left + size + margin > window.innerWidth) left = clientX - size - 18;
        if (top < margin) top = clientY + 18;
        left = Math.max(margin, Math.min(window.innerWidth - size - margin, left));
        top = Math.max(margin, Math.min(window.innerHeight - estimatedHeight - margin, top));
        preview.style.left = `${left}px`;
        preview.style.top = `${top}px`;
    }

    _resolveGuideSnapshotTargetLongEdge(clip) {
        const asset = this._getAssetForSourcePath(clip?.source_path || "");
        const sourceLong = Math.max(
            0,
            Math.round(Number(asset?.width) || 0),
            Math.round(Number(asset?.height) || 0)
        );
        const sceneLong = Math.max(
            1,
            Math.round(Number(this.activeScene?.width || this.sceneWidth) || 0),
            Math.round(Number(this.activeScene?.height || this.sceneHeight) || 0)
        );
        let targetLong = Math.max(sourceLong, sceneLong);
        const override = this._guideSnapshotMaxLongEdge();
        if (override > 0) {
            targetLong = Math.min(targetLong, override);
        }
        return Math.max(1, targetLong);
    }

    async _addClipFrameToGuides(clip) {
        if (!this.activeScene || !this.projectDir || !clip) return;
        if (this._isGuideTrackLocked()) {
            this._showToast("Guides track is locked");
            return;
        }
        const dirName = this._projectDirName();
        const sourceFrame = Math.max(0, this.playhead - clip.timeline_start_frame + (clip.source_in_frame || 0));
        const targetLongEdge = this._resolveGuideSnapshotTargetLongEdge(clip);

        try {
            let asset = null;
            const viewportSurface = this._ensureViewportSurface();
            if (viewportSurface?.captureSourceFrame) {
                try {
                    const snapshot = await viewportSurface.captureSourceFrame(clip.source_path, sourceFrame, targetLongEdge);
                    if (snapshot?.blob) {
                        const formData = new FormData();
                        formData.append("file", snapshot.blob, `snapshot_f${sourceFrame}.png`);
                        formData.append("metadata", JSON.stringify({
                            source_path: clip.source_path,
                            source_frame_index: sourceFrame,
                            timeline_frame_index: this.playhead,
                            extraction_mode: "viewport_snapshot",
                            snapshot_long_edge: snapshot.targetLongEdge || targetLongEdge,
                            snapshot_source_long_edge: snapshot.sourceLongEdge || Math.max(snapshot.sourceWidth || 0, snapshot.sourceHeight || 0),
                        }));
                        const snapshotResp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/viewport_snapshot`), {
                            method: "POST",
                            body: formData,
                        });
                        if (snapshotResp.ok) {
                            asset = await snapshotResp.json();
                            console.debug?.("[Sonder] Guide captured via viewport snapshot", {
                                source_path: clip.source_path,
                                source_frame: sourceFrame,
                                target_long_edge: snapshot.targetLongEdge || targetLongEdge,
                            });
                        } else {
                            console.warn("[Sonder] Viewport snapshot registration failed:", await snapshotResp.text());
                        }
                    }
                } catch (snapshotError) {
                    console.warn("[Sonder] Viewport snapshot capture failed:", snapshotError);
                }
            }

            if (!asset) {
                const extractResp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/extract_frame`), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        source_path: clip.source_path,
                        frame_index: sourceFrame,
                        target_long_edge: targetLongEdge,
                    }),
                });
                if (!extractResp.ok) {
                    console.warn("[Sonder] Extract frame failed:", await extractResp.text());
                    return;
                }
                asset = await extractResp.json();
                console.debug?.("[Sonder] Guide captured via backend fallback", {
                    source_path: clip.source_path,
                    source_frame: sourceFrame,
                    target_long_edge: targetLongEdge,
                });
                this._showToast("Captured via backend (viewport snapshot unavailable)");
            }

            const fields = {
                frame_index: this.playhead,
                asset_id: asset.asset_id,
                source: "asset",
                strength: 1.0,
            };
            this._pushUndo("add guide");
            this._applyLocalCreateGuide(fields);
            this._renderSceneAfterLocalMutation();
            this._deferProjectBackedRefresh(["assets"], "guide_snapshot_asset");
            await this._runSceneMutation(
                [{ type: "create_guide", fields }],
                {
                    key: `guide:${this.activeSceneId}:${this.playhead}:create`,
                    label: "add guide",
                    coalesce: false,
                }
            );
        } catch (e) {
            this._discardLastUndo("add guide");
            await this._fetchScenes({ ignoreMutationGate: true, reason: "add_guide_error" });
            console.warn("[Sonder] Add frame to guides failed:", e);
        }
    }

    async _deleteSelectedItems() {
        if (this.selectedItems.length === 0 || !this.activeScene || !this.projectDir) return;
        this.selectedItems = this.selectedItems.filter((item) => !this._isItemLocked(item));
        if (this.selectedItems.length === 0) return;
        const undoLabel = "delete items";
        this._pushUndo(undoLabel);
        const items = this.selectedItems.map((item) => {
            if (item.type === "guide") {
                return {
                    type: "guide",
                    id: item.id,
                    expected: {
                        frame_index: item.data?.frame_index ?? item.id,
                        asset_id: item.data?.asset_id || "",
                    },
                };
            }
            if (item.type === "prompt") {
                return {
                    type: "prompt",
                    id: item.id,
                    expected: {
                        start_frame: item.data?.start_frame,
                        end_frame: item.data?.end_frame,
                    },
                };
            }
            return {
                type: item.type,
                id: item.id,
            };
        });
        this._applyLocalBulkDeleteItems(items);
        this._clearSelection();
        this._hideItemEditor();
        this._renderSceneAfterLocalMutation();

        try {
            await this._runSceneMutation(
                [{ type: "bulk_delete_items", items }],
                {
                    key: `scene:${this.activeSceneId}:delete-selected:${Date.now()}`,
                    label: "delete items",
                    coalesce: false,
                }
            );
        } catch (e) {
            this._discardLastUndo(undoLabel);
            await this._fetchScenes({ ignoreMutationGate: true, reason: "delete_items_error" });
            console.warn("[Sonder] Failed to delete items:", e);
        }
    }

    async _commitItemMove(frameDelta) {
        if (this.selectedItems.length === 0 || !this.activeScene || !this.projectDir) return;
        const sceneId = this.activeSceneId;

        return this._withTimelineMutationCommit("moveItem", async () => {
            try {
                const operations = [];
                const dragItemsOrig = this._dragItemsOrig || [];
                const draggedClipIds = new Set(dragItemsOrig.filter(o => o.type === "clip").map(o => o.id));
                const draggedAudioIds = new Set(dragItemsOrig.filter(o => o.type === "audio").map(o => o.id));
                const origClipLanes = this._origAllClipLanes || {};
                const origAudioLanes = this._origAllAudioLanes || {};
                const origClipStarts = this._origAllClipStarts || {};
                const origAudioStarts = this._origAllAudioStarts || {};

                for (const clip of (this.activeScene.clips || [])) {
                    const clipId = clip.clip_id;
                    const isDragged = draggedClipIds.has(clipId);
                    const origLane = origClipLanes[clipId];
                    const laneChanged = origLane !== undefined && (clip.track_index || 0) !== origLane;
                    const origStart = origClipStarts[clipId];
                    const startChanged = origStart !== undefined && (clip.timeline_start_frame || 0) !== origStart;
                    if (!isDragged && !laneChanged && !startChanged) continue;
                    const fields = { track_index: clip.track_index || 0 };
                    if (isDragged || startChanged) {
                        fields.timeline_start_frame = clip.timeline_start_frame;
                        fields.timeline_end_frame = clip.timeline_end_frame;
                    }
                    operations.push({ type: "update_clip", clip_id: clipId, fields });
                }

                for (const track of (this.activeScene.audio_tracks || [])) {
                    const trackId = track.track_id;
                    const isDragged = draggedAudioIds.has(trackId);
                    const origLane = origAudioLanes[trackId];
                    const laneChanged = origLane !== undefined && (track.lane_index || 0) !== origLane;
                    const origStart = origAudioStarts[trackId];
                    const startChanged = origStart !== undefined && (track.timeline_start_frame || 0) !== origStart;
                    if (!isDragged && !laneChanged && !startChanged) continue;
                    const fields = { lane_index: track.lane_index || 0 };
                    if (isDragged || startChanged) {
                        fields.timeline_start_frame = track.timeline_start_frame;
                        fields.timeline_end_frame = track.timeline_end_frame;
                    }
                    operations.push({ type: "update_audio_track", track_id: trackId, fields });
                }

                for (const orig of dragItemsOrig) {
                    const { type, id, data } = orig;
                    if (type === "clip" || type === "audio") continue;
                    if (type === "guide") {
                        if (this._isGuideTrackLocked()) continue;
                        const oldIdx = orig.origStart;
                        const previewIdx = Number.isFinite(data._previewFrameIndex) ? data._previewFrameIndex : null;
                        const newIdx = previewIdx ?? Math.max(0, Math.min(this.totalFrames - 1, oldIdx + frameDelta));
                        const fields = {
                            asset_id: data.asset_id,
                            source: data.source || "asset",
                            strength: data.strength ?? 1.0,
                            muted: !!data.muted,
                        };
                        operations.push({
                            type: "move_guide",
                            from_frame_index: oldIdx,
                            to_frame_index: newIdx,
                            expected: {
                                frame_index: oldIdx,
                                asset_id: data.asset_id || "",
                            },
                            ...fields,
                        });
                        this._applyLocalMoveGuide(oldIdx, newIdx, data, fields);
                        delete data._previewFrameIndex;
                    } else if (type === "prompt") {
                        if (this._isPromptTrackLocked()) continue;
                        operations.push({
                            type: "update_prompt_section",
                            index: id,
                            expected: {
                                start_frame: orig.origStart,
                                end_frame: orig.origEnd,
                            },
                            fields: {
                                start_frame: data.start_frame,
                                end_frame: data.end_frame,
                            },
                        });
                    }
                }

                if (operations.length > 0) {
                    const result = await this._runSceneMutation(operations, {
                        key: `scene:${sceneId}:move-commit:${Date.now()}`,
                        label: "move item commit",
                        coalesce: false,
                        refreshScenes: false,
                    });
                    this._reconcileActiveSceneFromMutation(result, { reason: "moveItem_commit", ignoreTimelineGate: true });
                }
                this._renderTimeline();
            } catch (e) {
                this._discardLastUndo("move items");
                console.warn("[Sonder] Failed to move items:", e);
                await this._fetchScenes({ ignoreMutationGate: true, reason: "moveItem_error" });
                this._renderTimeline();
            }
        });
    }

    async _commitTrim(trimInfo) {
        if (!this.projectDir || !this.activeScene) return;
        const sceneId = this.activeSceneId;
        const { type, id, data, origStart, origEnd } = trimInfo;

        return this._withTimelineMutationCommit("trimEdge", async () => {
            try {
                const operations = [];
                if (type === "clip") {
                    operations.push({
                        type: "update_clip",
                        clip_id: id,
                        fields: {
                            timeline_start_frame: data.timeline_start_frame,
                            timeline_end_frame: data.timeline_end_frame,
                            source_in_frame: data.source_in_frame || 0,
                            source_out_frame: data.source_out_frame,
                        },
                    });
                } else if (type === "audio") {
                    operations.push({
                        type: "update_audio_track",
                        track_id: id,
                        fields: {
                            timeline_start_frame: data.timeline_start_frame,
                            timeline_end_frame: data.timeline_end_frame,
                            source_in_frame: data.source_in_frame || 0,
                        },
                    });
                } else if (type === "prompt") {
                    if (this._isPromptTrackLocked()) return;
                    operations.push({
                        type: "update_prompt_section",
                        index: id,
                        expected: {
                            start_frame: origStart,
                            end_frame: origEnd,
                        },
                        fields: {
                            start_frame: data.start_frame,
                            end_frame: data.end_frame,
                        },
                    });
                }
                if (operations.length > 0) {
                    const result = await this._runSceneMutation(operations, {
                        key: `scene:${sceneId}:trim-commit:${Date.now()}`,
                        label: "trim commit",
                        coalesce: false,
                        refreshScenes: false,
                    });
                    this._reconcileActiveSceneFromMutation(result, { reason: "trim_commit", ignoreTimelineGate: true });
                }
                this._renderTimeline();
            } catch (e) {
                this._discardLastUndo("trim");
                console.warn("[Sonder] Failed to commit trim:", e);
                await this._fetchScenes({ ignoreMutationGate: true, reason: "trim_error" });
                this._renderTimeline();
            }
        });
    }

    /** Split a clip at the given frame (razor tool). */
    async _splitClipAtFrame(hit, frame) {
        if (!this.projectDir || !this.activeScene) return;
        if (hit.type !== "clip" && hit.type !== "audio") return;
        if (frame <= hit.data.timeline_start_frame || frame >= hit.data.timeline_end_frame) return;
        // Block split on locked lanes
        if (hit.type === "clip" && this._isLaneLocked(this._clipTrackType(hit.data), hit.data.track_index || 0)) return;
        if (hit.type === "audio" && this._isLaneLocked(TRACK_TYPE.AUDIO, hit.data.lane_index || 0)) return;

        this._pushUndo(`split ${hit.type}`);
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const sceneId = this.activeSceneId;

        try {
            const endpoint = hit.type === "clip"
                ? `/sonder-editor/project/${dirName}/scenes/${sceneId}/clips/${hit.id}/split`
                : `/sonder-editor/project/${dirName}/scenes/${sceneId}/audio_tracks/${hit.id}/split`;
            await fetch(api.apiURL(endpoint), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ frame }),
            });
            await this._fetchScenes();
            this._renderTimeline();
        } catch (e) {
            console.warn(`[Sonder] Failed to split ${hit.type}:`, e);
        }
    }

    // ── Context Menu ──────────────────────────────────────────────────
    _showGuideManagementPopupLegacy(x, y) {
        this._hideGuideManagementPopup();
        const popup = document.createElement("div");
        popup.style.cssText = `
            position: fixed; left: ${x}px; top: ${y}px; z-index: 10000;
            min-width: 320px; max-width: 420px; max-height: 360px; overflow: auto;
            background: ${COLORS.panel}; border: 1px solid ${COLORS.borderStrong};
            border-radius: 6px; box-shadow: 0 12px 28px rgba(0,0,0,0.45);
            padding: 8px; color: ${COLORS.text}; font-size: 11px;
        `;
        const title = document.createElement("div");
        title.textContent = "Guides";
        title.style.cssText = `font-weight: 700; color: ${COLORS.guideSelected}; margin-bottom: 6px;`;
        popup.appendChild(title);

        const guides = (this.activeScene?.guide_frames || [])
            .slice()
            .sort((a, b) => {
                const af = a.frame_index === -1 ? this.totalFrames - 1 : a.frame_index;
                const bf = b.frame_index === -1 ? this.totalFrames - 1 : b.frame_index;
                return af - bf;
            });
        if (!guides.length) {
            const empty = document.createElement("div");
            empty.textContent = "No guides in this scene.";
            empty.style.cssText = `color:${COLORS.textMuted}; padding:4px 0;`;
            popup.appendChild(empty);
        }
        for (const guide of guides) {
            const frame = guide.frame_index === -1 ? this.totalFrames - 1 : guide.frame_index;
            const asset = this._getGuideAsset(guide);
            const row = document.createElement("div");
            row.style.cssText = "display:flex;align-items:center;gap:6px;padding:4px 0;border-top:1px solid rgba(255,255,255,0.06);";

            // Thumbnail (cached only)
            const thumbUrl = asset && !asset.missing && asset.path ? this._buildViewURL(asset.path) : null;
            const thumb = document.createElement("div");
            thumb.style.cssText = "width:36px;height:22px;flex-shrink:0;border-radius:3px;border:1px solid rgba(255,255,255,0.18);background:#000;overflow:hidden;";
            if (thumbUrl) {
                const img = document.createElement("img");
                img.src = thumbUrl;
                img.alt = "";
                img.style.cssText = "width:100%;height:100%;object-fit:cover;display:block;";
                img.title = asset?.name || asset?.path || "Guide asset";
                thumb.appendChild(img);
            }

            // Frame index input
            const frameInput = document.createElement("input");
            frameInput.type = "number";
            frameInput.min = "0";
            frameInput.max = String(Math.max(0, this.totalFrames - 1));
            frameInput.value = String(frame);
            frameInput.title = "Guide frame index (re-keys on commit)";
            frameInput.style.cssText = `width:54px;${chromeInputCss({ fontSize: "10px", padding: "2px 4px" })}`;
            const commitFrameInput = () => {
                const newIdx = parseInt(frameInput.value, 10);
                if (!Number.isFinite(newIdx) || newIdx === frame) return;
                const clamped = Math.max(0, Math.min(this.totalFrames - 1, newIdx));
                this._moveGuideToFrame(guide, clamped, guide.strength);
                this._hideGuideManagementPopup();
            };
            frameInput.addEventListener("change", commitFrameInput);
            frameInput.addEventListener("keydown", (e) => {
                if (e.key === "Enter") { commitFrameInput(); e.preventDefault(); }
                e.stopPropagation();
            });

            // Strength input
            const strengthInput = document.createElement("input");
            strengthInput.type = "number";
            strengthInput.min = "0";
            strengthInput.max = "1";
            strengthInput.step = "0.05";
            strengthInput.value = (Number(guide.strength ?? 1.0)).toFixed(2);
            strengthInput.title = "Guide strength (0.0-1.0)";
            strengthInput.style.cssText = `width:50px;${chromeInputCss({ fontSize: "10px", padding: "2px 4px" })}`;
            const commitStrength = () => {
                const next = Math.max(0, Math.min(1, parseFloat(strengthInput.value)));
                if (!Number.isFinite(next) || next === guide.strength) return;
                guide.strength = next;
                this._updateItemProperty("guide", guide.frame_index, { strength: next });
            };
            strengthInput.addEventListener("change", commitStrength);
            strengthInput.addEventListener("keydown", (e) => {
                if (e.key === "Enter") { commitStrength(); e.preventDefault(); }
                e.stopPropagation();
            });

            // Asset name label (truncated)
            const label = document.createElement("div");
            label.style.cssText = "flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
            const name = asset?.name || asset?.path?.split(/[/\\]/).pop() || guide.asset_id || "Guide";
            label.textContent = name;
            label.title = name;

            const muteBtn = this._makeBtn(guide.muted ? "Hidden" : "Visible", "Toggle guide visibility");
            muteBtn.addEventListener("click", async (event) => {
                event.stopPropagation();
                this._pushUndo("toggle guide mute");
                guide.muted = !guide.muted;
                await this._updateItemProperty("guide", guide.frame_index, { muted: guide.muted });
                this._showGuideManagementPopup(x, y);
            });

            const deleteBtn = this._makeBtn("✕", "Delete guide");
            deleteBtn.style.color = COLORS.dangerText;
            deleteBtn.addEventListener("click", async (event) => {
                event.stopPropagation();
                const undoLabel = "delete guide";
                this._pushUndo(undoLabel);
                this._applyLocalBulkDeleteItems([{
                    type: "guide",
                    id: guide.frame_index,
                    expected: {
                        frame_index: guide.frame_index,
                        asset_id: guide.asset_id || "",
                    },
                }]);
                this._renderSceneAfterLocalMutation();
                this._showGuideManagementPopup(x, y);
                try {
                    await this._runSceneMutation(
                        [{
                            type: "delete_guide",
                            frame_index: guide.frame_index,
                            expected: {
                                frame_index: guide.frame_index,
                                asset_id: guide.asset_id || "",
                            },
                        }],
                        {
                            key: `guide:${this.activeSceneId}:${guide.frame_index}:delete`,
                            label: "delete guide",
                            coalesce: false,
                            refreshScenes: false,
                        }
                    );
                } catch (e) {
                    this._discardLastUndo(undoLabel);
                    await this._fetchScenes({ ignoreMutationGate: true, reason: "delete_guide_error" });
                    this._showGuideManagementPopup(x, y);
                    console.warn("[Sonder] Failed to delete guide:", e);
                }
            });

            row.append(thumb, frameInput, strengthInput, label, muteBtn, deleteBtn);
            popup.appendChild(row);
        }

        document.body.appendChild(popup);
        this._guideManagerEl = popup;
        this._guideManagerMouseOff = (event) => {
            if (!popup.contains(event.target)) this._hideGuideManagementPopup();
        };
        window.setTimeout(() => document.addEventListener("mousedown", this._guideManagerMouseOff, true), 0);
    }

    _showGuideManagementPopup(x, y) {
        this._hideGuideManagementPopup();
        this._hideGuideHoverPreview();

        const backdrop = document.createElement("div");
        backdrop.style.cssText = `
            position: fixed; inset: 0; z-index: 10000;
            background: rgba(7,10,14,0.70);
            display: flex; align-items: center; justify-content: center;
            padding: 24px;
        `;

        const panel = document.createElement("div");
        panel.style.cssText = `${chromeOverlayPanelCss({
            width: "min(900px, calc(100vw - 48px))",
            maxWidth: "900px",
            maxHeight: "min(720px, calc(100vh - 48px))",
            padding: "0",
            fontFamily: "'Segoe UI', Arial, sans-serif",
        })}`;

        const locked = this._isGuideTrackLocked();
        const header = document.createElement("div");
        header.style.cssText = `
            position: sticky; top: 0; z-index: 1;
            display: flex; align-items: center; justify-content: space-between;
            gap: 12px; padding: 16px 18px 12px;
            background: ${COLORS.panel}; border-bottom: 1px solid ${COLORS.border};
        `;
        const titleWrap = document.createElement("div");
        titleWrap.innerHTML = `
            <div style="font-size:15px;font-weight:700;color:#fff;">Guides</div>
            <div style="font-size:11px;color:${locked ? COLORS.warningText : COLORS.textMuted};margin-top:3px;">${locked ? "Guide track locked" : "Frame guides and bridge visibility"}</div>
        `;
        const closeBtn = document.createElement("button");
        closeBtn.textContent = "Close";
        closeBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "6px 12px", fontSize: "11px", radius: "7px" });
        closeBtn.addEventListener("click", () => this._hideGuideManagementPopup());
        header.append(titleWrap, closeBtn);
        panel.appendChild(header);

        const body = document.createElement("div");
        body.style.cssText = "padding:14px 18px 18px;display:flex;flex-direction:column;gap:10px;";

        const guides = (this.activeScene?.guide_frames || [])
            .slice()
            .sort((a, b) => {
                const af = a.frame_index === -1 ? this.totalFrames - 1 : a.frame_index;
                const bf = b.frame_index === -1 ? this.totalFrames - 1 : b.frame_index;
                return af - bf;
            });

        const refreshPanel = async () => {
            await this._fetchScenes();
            this._renderTimeline();
            this._renderViewportFrame();
            this._showGuideManagementPopup(x, y);
        };

        if (!guides.length) {
            const empty = document.createElement("div");
            empty.textContent = "No guides in this scene.";
            empty.style.cssText = `color:${COLORS.textMuted}; padding:18px 0; text-align:center;`;
            body.appendChild(empty);
        }

        for (const guide of guides) {
            const frame = guide.frame_index === -1 ? this.totalFrames - 1 : guide.frame_index;
            const asset = this._getGuideAsset(guide);
            const rowHidden = this._isGuideTrackHidden() || !!guide.muted;
            const row = document.createElement("div");
            row.style.cssText = `
                display:grid;
                grid-template-columns: 112px 70px 72px minmax(140px, 1fr) 82px minmax(150px, 188px) 58px;
                align-items:center; gap:10px;
                padding:10px; border:1px solid ${rowHidden ? COLORS.warningBorder : COLORS.borderSoft};
                border-radius:8px; background:${rowHidden ? "rgba(48,36,20,0.28)" : COLORS.panelMuted};
                opacity:${rowHidden ? "0.78" : "1"};
            `;
            row.addEventListener("mousemove", (event) => this._showGuideHoverPreview(guide, event.clientX, event.clientY));
            row.addEventListener("mouseleave", () => this._hideGuideHoverPreview());

            const thumbUrl = asset && !asset.missing && asset.path ? this._buildViewURL(asset.path) : null;
            const thumb = document.createElement("div");
            thumb.style.cssText = `
                width:96px;height:54px;flex-shrink:0;border-radius:5px;
                border:1px solid rgba(255,255,255,0.18);background:#000;
                overflow:hidden;display:flex;align-items:center;justify-content:center;
                position:relative;
            `;
            if (thumbUrl) {
                const img = document.createElement("img");
                img.src = thumbUrl;
                img.alt = "";
                img.style.cssText = "width:100%;height:100%;object-fit:cover;display:block;";
                img.title = asset?.name || asset?.path || "Guide asset";
                thumb.appendChild(img);
            } else {
                const missing = document.createElement("span");
                missing.textContent = asset?.missing ? "Missing" : "No image";
                missing.style.cssText = `font-size:10px;color:${COLORS.textMuted};`;
                thumb.appendChild(missing);
            }
            if (rowHidden) {
                const hiddenBadge = document.createElement("div");
                hiddenBadge.textContent = "Hidden";
                hiddenBadge.style.cssText = `
                    position:absolute;left:6px;top:6px;padding:2px 6px;border-radius:999px;
                    background:rgba(0,0,0,0.68);border:1px solid ${COLORS.warningBorder};
                    color:${COLORS.warningText};font-size:9px;font-weight:700;
                `;
                thumb.appendChild(hiddenBadge);
            }

            const frameInput = document.createElement("input");
            frameInput.type = "number";
            frameInput.min = "0";
            frameInput.max = String(Math.max(0, this.totalFrames - 1));
            frameInput.value = String(frame);
            frameInput.title = "Guide frame index";
            frameInput.disabled = locked;
            frameInput.style.cssText = `${chromeInputCss({ width: "66px", fontSize: "11px", padding: "5px 7px" })}`;
            const commitFrameInput = () => {
                if (locked) return;
                const newIdx = parseInt(frameInput.value, 10);
                if (!Number.isFinite(newIdx) || newIdx === frame) return;
                const clamped = Math.max(0, Math.min(this.totalFrames - 1, newIdx));
                this._moveGuideToFrame(guide, clamped, guide.strength);
                this._hideGuideManagementPopup();
            };
            frameInput.addEventListener("change", commitFrameInput);
            frameInput.addEventListener("keydown", (event) => {
                if (event.key === "Enter") { commitFrameInput(); event.preventDefault(); }
                event.stopPropagation();
            });

            const strengthInput = document.createElement("input");
            strengthInput.type = "number";
            strengthInput.min = "0";
            strengthInput.max = "1";
            strengthInput.step = "0.05";
            strengthInput.value = (Number(guide.strength ?? 1.0)).toFixed(2);
            strengthInput.title = "Guide strength";
            strengthInput.disabled = locked;
            strengthInput.style.cssText = `${chromeInputCss({ width: "68px", fontSize: "11px", padding: "5px 7px" })}`;
            const commitStrength = async () => {
                if (locked) return;
                const next = Math.max(0, Math.min(1, parseFloat(strengthInput.value)));
                if (!Number.isFinite(next) || next === guide.strength) return;
                this._pushUndo("change guide strength");
                guide.strength = next;
                await this._updateItemProperty("guide", guide.frame_index, { strength: next }, { refresh: false });
                await refreshPanel();
            };
            strengthInput.addEventListener("change", commitStrength);
            strengthInput.addEventListener("keydown", (event) => {
                if (event.key === "Enter") { commitStrength(); event.preventDefault(); }
                event.stopPropagation();
            });

            const label = document.createElement("div");
            label.style.cssText = "min-width:0;display:flex;flex-direction:column;gap:3px;overflow:hidden;";
            const name = asset?.name || asset?.path?.split(/[/\\]/).pop() || guide.asset_id || "Guide";
            label.title = name;
            const nameLine = document.createElement("div");
            nameLine.textContent = name;
            nameLine.style.cssText = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#e5e9ee;font-size:12px;font-weight:600;";
            const metaLine = document.createElement("div");
            metaLine.textContent = `${guide.source || "asset"} | f${frame}`;
            metaLine.style.cssText = `overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:${COLORS.textMuted};font-size:10px;`;
            label.append(nameLine, metaLine);

            const muteBtn = this._makeBtn(guide.muted ? "Show" : "Hide", "Toggle guide visibility");
            muteBtn.disabled = locked;
            muteBtn.addEventListener("click", async (event) => {
                event.stopPropagation();
                if (locked) return;
                this._pushUndo("toggle guide mute");
                guide.muted = !guide.muted;
                await this._updateItemProperty("guide", guide.frame_index, { muted: guide.muted }, { refresh: false });
                await refreshPanel();
            });

            const swapWrap = document.createElement("div");
            swapWrap.style.cssText = "display:flex;align-items:center;gap:6px;min-width:0;";
            const swapSelect = document.createElement("select");
            swapSelect.disabled = locked || guides.length < 2;
            swapSelect.style.cssText = `${chromeInputCss({ fontSize: "11px", padding: "5px 7px", textAlign: "left" })} min-width:0;flex:1;`;
            for (const other of guides) {
                if (other === guide) continue;
                const otherFrame = other.frame_index === -1 ? this.totalFrames - 1 : other.frame_index;
                const option = document.createElement("option");
                option.value = String(other.frame_index);
                option.textContent = `f${otherFrame}`;
                swapSelect.appendChild(option);
            }
            const swapBtn = this._makeBtn("Swap", "Swap guide frames");
            swapBtn.disabled = locked || guides.length < 2;
            swapBtn.addEventListener("click", async (event) => {
                event.stopPropagation();
                if (locked || !swapSelect.value) return;
                this._pushUndo("swap guides");
                const dirName = this._projectDirName();
                const sceneId = this.activeSceneId;
                try {
                    await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}/guides/swap`), {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            frame_a: guide.frame_index,
                            frame_b: Number(swapSelect.value),
                        }),
                    });
                    await refreshPanel();
                } catch (e) {
                    console.warn("[Sonder] Failed to swap guides:", e);
                }
            });
            swapWrap.append(swapSelect, swapBtn);

            const deleteBtn = this._makeBtn("Del", "Delete guide");
            deleteBtn.disabled = locked;
            deleteBtn.style.color = COLORS.dangerText;
            deleteBtn.addEventListener("click", async (event) => {
                event.stopPropagation();
                if (locked) return;
                const undoLabel = "delete guide";
                this._pushUndo(undoLabel);
                this._applyLocalBulkDeleteItems([{
                    type: "guide",
                    id: guide.frame_index,
                    expected: {
                        frame_index: guide.frame_index,
                        asset_id: guide.asset_id || "",
                    },
                }]);
                this._renderSceneAfterLocalMutation();
                this._showGuideManagementPopup(x, y);
                try {
                    await this._runSceneMutation(
                        [{
                            type: "delete_guide",
                            frame_index: guide.frame_index,
                            expected: {
                                frame_index: guide.frame_index,
                                asset_id: guide.asset_id || "",
                            },
                        }],
                        {
                            key: `guide:${this.activeSceneId}:${guide.frame_index}:delete`,
                            label: "delete guide",
                            coalesce: false,
                            refreshScenes: false,
                        }
                    );
                } catch (e) {
                    this._discardLastUndo(undoLabel);
                    await refreshPanel();
                    console.warn("[Sonder] Failed to delete guide:", e);
                }
            });

            row.append(thumb, frameInput, strengthInput, label, muteBtn, swapWrap, deleteBtn);
            body.appendChild(row);
        }

        panel.appendChild(body);
        backdrop.appendChild(panel);
        backdrop.addEventListener("click", (event) => {
            if (event.target === backdrop) this._hideGuideManagementPopup();
        });
        document.body.appendChild(backdrop);
        this._guideManagerEl = backdrop;
        this._guideManagerMouseOff = null;
    }

    _hideGuideManagementPopup() {
        this._hideGuideHoverPreview();
        if (this._guideManagerEl) {
            this._guideManagerEl.remove();
            this._guideManagerEl = null;
        }
        if (this._guideManagerMouseOff) {
            document.removeEventListener("mousedown", this._guideManagerMouseOff, true);
            this._guideManagerMouseOff = null;
        }
    }

    _showContextMenu(x, y, items) {
        this._hideContextMenu();

        const menu = document.createElement("div");
        menu.style.cssText = `
            position: fixed; left: ${x}px; top: ${y}px; z-index: 10000;
            ${chromeMenuCss(150)}
        `;

        for (const item of items) {
            if (item?.type === "separator") {
                const separator = document.createElement("div");
                separator.style.cssText = `height: 1px; margin: 4px 8px; background: ${COLORS.borderSoft};`;
                menu.appendChild(separator);
                continue;
            }
            const row = document.createElement("div");
            row.textContent = item.label;
            const isDisabled = item.disabled;
            row.style.cssText = `
                padding: 6px 14px; cursor: ${isDisabled ? "default" : "pointer"};
                color: ${isDisabled ? COLORS.textMuted : (item.danger ? COLORS.dangerText : COLORS.text)};
            `;
            if (!isDisabled) {
                row.addEventListener("mouseenter", () => row.style.background = COLORS.panelRaisedHover);
                row.addEventListener("mouseleave", () => row.style.background = "transparent");
                row.addEventListener("click", () => {
                    this._hideContextMenu();
                    item.action();
                });
            }
            menu.appendChild(row);
        }

        document.body.appendChild(menu);
        this._contextMenuEl = menu;

        // Close on outside click or Escape (Escape is owned by KeyboardOwnership
        // OVERLAY consumer so it beats LiteGraph and the EDITOR consumer).
        const closeHandler = (e) => {
            if (!menu.contains(e.target)) this._hideContextMenu();
        };
        this._contextMenuKeyOff = registerKeyboardConsumer({
            id: this._keyboardConsumerId("ctxmenu"),
            priority: KEY_PRIORITY.OVERLAY,
            keydown: (e) => {
                if (e.key === "Escape") { this._hideContextMenu(); return true; }
                return false;
            },
        });
        this._contextMenuMouseOff = () => document.removeEventListener("mousedown", closeHandler);
        // Delay listener registration to avoid catching the current right-click
        setTimeout(() => {
            document.addEventListener("mousedown", closeHandler);
        }, 10);
    }

    _hideContextMenu() {
        if (this._contextMenuEl) {
            this._contextMenuEl.remove();
            this._contextMenuEl = null;
        }
        if (this._contextMenuKeyOff) {
            this._contextMenuKeyOff();
            this._contextMenuKeyOff = null;
        }
        if (this._contextMenuMouseOff) {
            this._contextMenuMouseOff();
            this._contextMenuMouseOff = null;
        }
    }

    // ── Keyboard Shortcut Overlay ────────────────────────────────────
    _showShortcutOverlay() {
        if (this._shortcutOverlayEl) return;
        const backdrop = document.createElement("div");
        backdrop.style.cssText = `position:fixed;inset:0;z-index:10001;background:rgba(7,10,14,0.78);display:flex;align-items:center;justify-content:center;padding:20px;`;
        const panel = document.createElement("div");
        panel.style.cssText = chromeOverlayPanelCss({ width: "min(560px, 100%)", maxWidth: "560px", maxHeight: "80vh", padding: "20px 24px", fontFamily: "'Segoe UI', Arial, sans-serif" });
        panel.innerHTML = `<h3 style="margin:0 0 14px;color:#fff;font-size:15px;letter-spacing:0.02em;">Keyboard Shortcuts</h3>` +
            this._shortcutSection("Playback", [
                ["Space", "Play / Pause (fullscreen)"],
                ["\u2190 / \u2192", "Frame back / forward"],
                ["Shift+\u2190 / \u2192", "10 frames back / forward"],
                ["Home / End", "Go to first / last frame"],
            ]) +
            this._shortcutSection("Selection", [
                ["I", "Set in-point"],
                ["O", "Set out-point"],
                ["X", "Clear selection"],
            ]) +
            this._shortcutSection("Tools", [
                ["C", "Toggle razor / cut mode"],
                ["M", "Mute / Hide selected asset(s)"],
                ["S", "Toggle snapping"],
                ["T", "Toggle timecode display"],
                ["F", "Fit timeline to view"],
                ["Shift+F", "Zoom to selection"],
            ]) +
            this._shortcutSection("Edit", [
                ["Del / Backspace", "Delete selected items"],
                ["Ctrl+Z", "Undo"],
                ["Ctrl+Y", "Redo"],
                ["Ctrl+Shift+Z", "Redo"],
            ]) +
            this._shortcutSection("Asset Gallery", [
                ["Arrow keys", "Move asset focus / selection"],
                ["Space", "Open inspect overlay for focused asset"],
                ["Ctrl+A", "Select all visible assets"],
                ["Delete", "Trash or permanently delete selection (when gallery focused)"],
                ["Esc", "Clear or reduce gallery selection"],
            ]) +
            this._shortcutSection("Inspect Overlay", INSPECT_OVERLAY_SHORTCUTS) +
            this._shortcutSection("View", [
                ["Wheel", "Vertical lane scroll"],
                ["Ctrl+Wheel", "Horizontal timeline pan"],
                ["Shift+Wheel", "Timeline zoom"],
                ["+ / -", "Zoom in / out"],
                ["Esc", "Exit fullscreen / dismiss overlay / clear selection"],
                ["?", "Show this overlay"],
                ["Gear", "Editor Settings (toolbar button)"],
            ]);

        backdrop.appendChild(panel);
        document.body.appendChild(backdrop);
        this._shortcutOverlayEl = backdrop;

        backdrop.addEventListener("click", (e) => { if (e.target === backdrop) this._hideShortcutOverlay(); });
        this._shortcutOverlayKeyOff = registerKeyboardConsumer({
            id: this._keyboardConsumerId("atlas"),
            priority: KEY_PRIORITY.OVERLAY,
            keydown: (e) => {
                if (e.key === "Escape") { this._hideShortcutOverlay(); return true; }
                return false;
            },
        });
    }

    _shortcutSection(title, shortcuts) {
        let html = `<div style="margin-bottom:12px;"><div style="color:${COLORS.textDim};font-size:10px;margin-bottom:6px;text-transform:uppercase;letter-spacing:0.08em;">${title}</div>`;
        for (const [key, desc] of shortcuts) {
            html += `<div style="display:flex;justify-content:space-between;gap:16px;padding:3px 0;"><span style="color:${lightenColor(COLORS.sceneBtnActive, 0.3)};min-width:120px;font-family:${FONT.mono};">${key}</span><span style="color:${COLORS.text};">${desc}</span></div>`;
        }
        return html + `</div>`;
    }

    _hideShortcutOverlay() {
        if (this._shortcutOverlayEl) {
            this._shortcutOverlayEl.remove();
            this._shortcutOverlayEl = null;
        }
        if (this._shortcutOverlayKeyOff) {
            this._shortcutOverlayKeyOff();
            this._shortcutOverlayKeyOff = null;
        }
    }

    // ── Fullscreen Mode ──────────────────────────────────────────────
    _createFullscreenOverlay() {
        if (this._fullscreenOverlay) return;

        const overlay = document.createElement("div");
        overlay.style.cssText = `
            position: fixed; inset: 0; z-index: 9999;
            background: ${COLORS.bg}; display: none;
            flex-direction: column;
        `;

        // Toolbar
        const toolbar = document.createElement("div");
        toolbar.style.cssText = `
            display: flex; align-items: center; padding: 0 12px;
            height: 42px; background: ${COLORS.panel}; border-bottom: 1px solid ${COLORS.border};
            flex-shrink: 0;
        `;

        this._fsTitle = document.createElement("span");
        this._fsTitle.style.cssText = `font-size: 13px; color: ${COLORS.text}; font-weight: 600;`;
        this._fsTitle.textContent = "Sonder Editor";

        const spacer = document.createElement("span");
        spacer.style.flex = "1";

        const mountTabBtn = this.onMountInTab ? this._makeBtn("Mount in Tab", "Move this editor to a persistent browser tab") : null;
        if (mountTabBtn) {
            mountTabBtn.style.cssText += `font-size: 12px; padding: 4px 12px; color: ${COLORS.textDim}; margin-right: 8px;`;
            mountTabBtn.addEventListener("click", () => this.onMountInTab?.());
        }

        const exitBtn = this._makeBtn("✕ Exit", "Exit fullscreen");
        exitBtn.style.cssText += `font-size: 12px; padding: 4px 12px; color: ${COLORS.textDim};`;
        exitBtn.addEventListener("click", () => void this._requestExitFullscreen({ reason: "toolbar" }));

        const toolbarButtons = document.createElement("div");
        toolbarButtons.dataset.fsToolbarButtons = "true";
        toolbarButtons.style.cssText = `display: flex; align-items: center; flex-shrink: 0;`;
        if (mountTabBtn) {
            toolbarButtons.append(mountTabBtn, exitBtn);
        } else {
            toolbarButtons.append(exitBtn);
        }
        toolbar.append(this._fsTitle, spacer, toolbarButtons);

        // Content area — three-panel layout
        this._fsContent = document.createElement("div");
        this._fsContent.style.cssText = `
            flex: 1; display: flex; flex-direction: column; overflow: hidden;
        `;

        // Top row: assets sidebar + viewport
        this._fsTopRow = document.createElement("div");
        this._fsTopRow.style.cssText = `
            flex: 1; display: flex; overflow: hidden; min-height: 0;
        `;

        // Assets sidebar (left)
        this._fsSidebar = document.createElement("div");
        this._fsSidebar.style.cssText = `
            width: ${FULLSCREEN_SIDEBAR_DEFAULT_WIDTH}px; min-width: ${FULLSCREEN_SIDEBAR_MIN_WIDTH}px; max-width: ${this._computeFullscreenSidebarMaxWidth()}px;
            background: ${COLORS.galleryBg}; border-right: 1px solid ${COLORS.border};
            display: flex; flex-direction: column; overflow: hidden;
            flex-shrink: 0; position: relative;
        `;

        // Sidebar header with project name
        this._fsSidebarHeader = document.createElement("div");
        this._fsSidebarHeader.style.cssText = `
            padding: 8px 12px; background: ${COLORS.panel}; border-bottom: 1px solid ${COLORS.border};
            font-size: 12px; color: ${COLORS.text}; font-weight: 600;
            flex-shrink: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        `;
        this._fsSidebarHeader.textContent = "Assets";
        this._fsSidebar.appendChild(this._fsSidebarHeader);

        // Sidebar resize handle
        const sidebarHandle = document.createElement("div");
        sidebarHandle.style.cssText = `
            position: absolute; right: -3px; top: 0; bottom: 0; width: 6px;
            cursor: ew-resize; z-index: 2;
        `;
        this._setupResizeHandle(sidebarHandle, this._fsSidebar, "width", FULLSCREEN_SIDEBAR_MIN_WIDTH, () => this._computeFullscreenSidebarMaxWidth(), false, "sidebar-width");
        this._fsSidebar.appendChild(sidebarHandle);

        // Viewport panel (center)
        this._fsViewport = document.createElement("div");
        this._fsViewport.style.cssText = `
            flex: 1; display: flex; flex-direction: column;
            background: ${COLORS.panelMuted}; overflow: hidden; min-width: 0;
        `;

        // Viewport header
        const vpHeader = document.createElement("div");
        vpHeader.style.cssText = `
            padding: 6px 12px; background: ${COLORS.panel}; border-bottom: 1px solid ${COLORS.border};
            font-size: 11px; color: ${COLORS.textDim}; flex-shrink: 0;
            display: flex; justify-content: space-between; align-items: center;
        `;
        this._vpHeaderText = document.createElement("span");
        this._updateViewportHeader();
        vpHeader.appendChild(this._vpHeaderText);

        // Viewport content area — canvas for video preview
        this._fsViewportContent = document.createElement("div");
        this._fsViewportContent.style.cssText = `
            flex: 1; display: flex; align-items: center; justify-content: center;
            min-height: 0; position: relative; overflow: hidden;
        `;

        // Viewport canvas
        this._vpCanvas = document.createElement("canvas");
        this._vpCanvas.style.cssText = `display: block; background: #000;`;
        this._vpCtx = this._vpCanvas.getContext("2d");
        this._fsViewportContent.appendChild(this._vpCanvas);

        // Viewport frame overlay (shows frame number when no clip)
        this._vpFrameLabel = document.createElement("div");
        this._vpFrameLabel.style.cssText = `
            position: absolute; bottom: 8px; right: 8px;
            font-size: 11px; color: rgba(255,255,255,0.5);
            pointer-events: none; font-family: ${FONT.mono};
        `;
        this._fsViewportContent.appendChild(this._vpFrameLabel);

        // ResizeObserver to recalc viewport canvas dimensions
        this._vpResizeObserver = new ResizeObserver(() => this._resizeViewportCanvas());
        this._vpResizeObserver.observe(this._fsViewportContent);

        // Transport bar
        const transport = document.createElement("div");
        transport.style.cssText = `
            height: 38px; flex-shrink: 0; background: ${COLORS.panel};
            border-top: 1px solid ${COLORS.border}; display: flex; align-items: center;
            padding: 0 12px; gap: 10px;
        `;

        // Play/Pause button
        this._vpPlayBtn = document.createElement("button");
        this._vpPlayBtn.textContent = "Play";
        this._vpPlayBtn.style.cssText = `${chromeButtonCss({ variant: "primary", padding: "4px 10px", fontSize: "12px", radius: "6px" })} min-width: 36px;`;
        this._vpPlayBtn.addEventListener("click", () => this._togglePlayback());

        // Frame counter
        this._vpFrameCounter = document.createElement("span");
        this._vpFrameCounter.style.cssText = `
            font-size: 11px; color: ${COLORS.text}; font-family: ${FONT.mono};
        `;
        this._vpFrameCounter.textContent = "Frame 0 / 0";

        // Progress bar
        const progressWrap = document.createElement("div");
        progressWrap.style.cssText = `
            flex: 1; height: 6px; background: ${COLORS.panelRaised}; border-radius: 999px;
            cursor: pointer; position: relative;
        `;
        this._vpProgressFill = document.createElement("div");
        this._vpProgressFill.style.cssText = `
            height: 100%; background: ${lightenColor(COLORS.sceneBtnActive, 0.08)}; border-radius: 999px;
            width: 0%; pointer-events: none;
        `;
        progressWrap.appendChild(this._vpProgressFill);
        progressWrap.addEventListener("click", (e) => {
            const rect = progressWrap.getBoundingClientRect();
            const pct = (e.clientX - rect.left) / rect.width;
            this.playhead = Math.round(pct * this.totalFrames);
            this._onPlayheadChange();
        });

        transport.append(this._vpPlayBtn, this._vpFrameCounter, progressWrap);
        this._fsViewport.append(vpHeader, this._fsViewportContent, transport);
        this._fsTopRow.append(this._fsSidebar, this._fsViewport);

        // Bottom area: timeline (will hold this.container minus gallery)
        this._fsBottomRow = document.createElement("div");
        const _defaultTimelineH = this._defaultFullscreenTimelineHeight();
        this._fsBottomRow.style.cssText = `
            height: ${_defaultTimelineH}px; min-height: ${FULLSCREEN_TIMELINE_MIN_HEIGHT}px; max-height: ${this._computeFullscreenTimelineMaxHeight()}px;
            border-top: 1px solid ${COLORS.border}; display: flex; flex-direction: column;
            overflow: hidden; flex-shrink: 0; position: relative;
        `;

        // Timeline resize handle (top edge)
        const timelineHandle = document.createElement("div");
        timelineHandle.style.cssText = `
            position: absolute; top: -3px; left: 0; right: 0; height: 6px;
            cursor: ns-resize; z-index: 2;
        `;
        this._setupResizeHandle(timelineHandle, this._fsBottomRow, "height", FULLSCREEN_TIMELINE_MIN_HEIGHT, () => this._computeFullscreenTimelineMaxHeight(), true, "timeline-height");
        this._fsBottomRow.appendChild(timelineHandle);

        this._fsContent.append(this._fsTopRow, this._fsBottomRow);
        overlay.append(toolbar, this._fsContent);
        document.body.appendChild(overlay);
        this._fullscreenOverlay = overlay;
    }

    _setupResizeHandle(handle, target, prop, min, max, invert = false, persistKey = "") {
        let startPos = 0;
        let startSize = 0;

        const onMouseMove = (e) => {
            const maxValue = typeof max === "function" ? max() : max;
            const delta = prop === "width"
                ? e.clientX - startPos
                : e.clientY - startPos;
            const newSize = invert
                ? Math.max(min, Math.min(maxValue, startSize - delta))
                : Math.max(min, Math.min(maxValue, startSize + delta));
            target.style[prop] = newSize + "px";

            // Recalc timeline height to fill remaining space
            if (this.isFullscreen) {
                this._recalcFullscreenHeights();
                this._renderTimeline();
            }
        };

        const onMouseUp = () => {
            document.removeEventListener("mousemove", onMouseMove);
            document.removeEventListener("mouseup", onMouseUp);
            document.body.style.cursor = "";
            document.body.style.userSelect = "";
            if (persistKey) {
                const currentSize = parseInt(getComputedStyle(target)[prop], 10);
                this._writeFullscreenPersistValue(persistKey, currentSize);
            }
        };

        handle.addEventListener("mousedown", (e) => {
            e.preventDefault();
            startPos = prop === "width" ? e.clientX : e.clientY;
            startSize = parseInt(getComputedStyle(target)[prop]);
            document.body.style.cursor = prop === "width" ? "ew-resize" : "ns-resize";
            document.body.style.userSelect = "none";
            document.addEventListener("mousemove", onMouseMove);
            document.addEventListener("mouseup", onMouseUp);
        });
    }

    _recalcFullscreenHeights() {
        if (this._fsSidebar) {
            const sidebarMax = this._computeFullscreenSidebarMaxWidth();
            const sidebarWidth = parseInt(getComputedStyle(this._fsSidebar).width, 10) || FULLSCREEN_SIDEBAR_DEFAULT_WIDTH;
            this._fsSidebar.style.maxWidth = `${sidebarMax}px`;
            this._fsSidebar.style.width = `${Math.max(FULLSCREEN_SIDEBAR_MIN_WIDTH, Math.min(sidebarMax, sidebarWidth))}px`;
        }
        if (this._fsBottomRow) {
            const timelineMax = this._computeFullscreenTimelineMaxHeight();
            const timelineHeight = parseInt(getComputedStyle(this._fsBottomRow).height, 10) || this._defaultFullscreenTimelineHeight();
            this._fsBottomRow.style.maxHeight = `${timelineMax}px`;
            this._fsBottomRow.style.height = `${Math.max(FULLSCREEN_TIMELINE_MIN_HEIGHT, Math.min(timelineMax, timelineHeight))}px`;
        }

        // In three-panel layout, timeline height is based on the bottom row height
        const bottomH = this._fsBottomRow ? parseInt(getComputedStyle(this._fsBottomRow).height) || 280 : 280;
        const st = this._scaleToolbar;
        const sceneBarH = SCENE_BAR_HEIGHT * st;
        const toolbarH = 24 * st;
        const editorsH = ((this._promptEditorEl ? 30 : 0) + (this._itemEditorEl ? 30 : 0)) * st;
        // Timeline height — canvas renders at 1:1 now (individual elements scale themselves)
        this._timelineHeight = Math.max(100, bottomH - sceneBarH - toolbarH - editorsH);
        this._clampScrollY();
        // Gallery is in the sidebar now, doesn't need height calc
        this._galleryHeight = GALLERY_HEIGHT; // Not used in fullscreen layout
    }

    _enterFullscreen() {
        // Module-level guard: only one fullscreen at a time
        if (EditorWidget._activeFullscreen && EditorWidget._activeFullscreen !== this) return;

        this._createFullscreenOverlay();
        const savedSidebarWidth = this._readFullscreenPersistValue("sidebar-width");
        const savedTimelineHeight = this._readFullscreenPersistValue("timeline-height");
        if (savedSidebarWidth && this._fsSidebar) {
            const sidebarMax = this._computeFullscreenSidebarMaxWidth();
            this._fsSidebar.style.width = `${Math.max(FULLSCREEN_SIDEBAR_MIN_WIDTH, Math.min(sidebarMax, savedSidebarWidth))}px`;
        }
        if (savedTimelineHeight && this._fsBottomRow) {
            const timelineMax = this._computeFullscreenTimelineMaxHeight();
            this._fsBottomRow.style.height = `${Math.max(FULLSCREEN_TIMELINE_MIN_HEIGHT, Math.min(timelineMax, savedTimelineHeight))}px`;
        }

        // Save position and node size for re-insertion
        this._nodeParent = this.container.parentElement;
        this._nodeSibling = this.container.nextSibling;
        this._savedNodeSize = this.widgetHost?.getSize?.() || null;

        // Reparent: gallery goes to sidebar, rest of container goes to bottom row
        // Save gallery's position in container for restoration
        this._galleryNextSibling = this.galleryEl.nextSibling;

        // Update sidebar header
        const dirName = this.projectDir ? this.projectDir.split(/[/\\]/).pop() : "Assets";
        if (this._fsSidebarHeader) this._fsSidebarHeader.textContent = dirName;

        // Move gallery to sidebar (keep gallery zoom for scale)
        this._fsSidebar.appendChild(this.galleryEl);
        const sg = this._scaleGallery;
        this.galleryEl.style.zoom = sg !== 1.0 ? sg : "";
        this.galleryEl.style.flex = "1";
        this.galleryEl.style.minHeight = "0";
        this.galleryEl.style.overflow = "hidden";
        this.galleryEl.style.display = "flex";
        this.galleryEl.style.flexDirection = "column";
        this.assetGrid.style.maxHeight = "none";
        this.assetGrid.style.flex = "1";
        this.assetGrid.style.overflow = "hidden";
        this.assetGrid.style.minHeight = "0";
        this.assetGrid.style.gridTemplateColumns = "";

        // Move timeline container (without gallery) to bottom row
        this._fsBottomRow.appendChild(this.container);
        this.container.style.flex = "1";
        this.container.style.overflow = "hidden";

        // Show overlay
        this._fullscreenOverlay.style.display = "flex";

        // Update toolbar title
        this._fsTitle.textContent = `Editor — ${this.activeScene?.name || "No Scene"}`;

        // Insert placeholder into node
        this._fullscreenPlaceholder = document.createElement("div");
        this._fullscreenPlaceholder.style.cssText = `
            text-align: center; padding: 16px; color: ${COLORS.textDim};
            background: ${COLORS.panel}; border: 1px solid ${COLORS.border}; border-radius: 8px; font-size: 12px;
        `;
        this._fullscreenPlaceholder.innerHTML = `Editor is in fullscreen mode`;
        const exitPlaceholderBtn = this._makeBtn("Exit Fullscreen", "Return editor to node");
        exitPlaceholderBtn.style.cssText += `display: block; margin: 8px auto 0;`;
        exitPlaceholderBtn.addEventListener("click", () => void this._requestExitFullscreen({ reason: "placeholder" }));
        this._fullscreenPlaceholder.appendChild(exitPlaceholderBtn);

        if (this._nodeParent) {
            this._nodeParent.insertBefore(this._fullscreenPlaceholder, this._nodeSibling);
        }

        // Set fullscreen state + recalc
        this.isFullscreen = true;
        EditorWidget._activeFullscreen = this;
        this._fullscreenBtn.textContent = "⛶";
        this._fullscreenBtn.title = "Exit fullscreen";
        this._sweepRenderCache();

        this._recalcFullscreenHeights();
        this._renderTimeline();

        // Render viewport after layout settles
        requestAnimationFrame(() => {
            this._resizeViewportCanvas();
            this._renderViewportFrame();
        });

        // Collapse node
        this.widgetHost?.setSize?.(this.widgetHost?.computeSize?.());
    }

    async _requestExitFullscreen({ reason = "user" } = {}) {
        if (!this.isFullscreen) return;
        if (this._projectMutationCloseInProgress) {
            return this._projectMutationCloseInProgress;
        }
        this._projectMutationCloseInProgress = (async () => {
            this._stopPlayback();
            await this._drainProjectMutations(`fullscreen_exit:${reason}`);
            if (!this._destroyed && this.isFullscreen) {
                this._exitFullscreen();
            }
        })();
        try {
            await this._projectMutationCloseInProgress;
        } finally {
            this._projectMutationCloseInProgress = null;
        }
    }

    _exitFullscreen() {
        if (!this.isFullscreen) return;

        // Stop playback before exiting
        this._stopPlayback();
        this._clearVideoCache();

        // Remove placeholder
        if (this._fullscreenPlaceholder) {
            this._fullscreenPlaceholder.remove();
            this._fullscreenPlaceholder = null;
        }

        // Move gallery back into container (before its original next sibling)
        if (this.galleryEl) {
            this.galleryEl.style.flex = "";
            this.galleryEl.style.minHeight = GALLERY_HEIGHT + "px";
            this.galleryEl.style.overflow = "";
            this.galleryEl.style.display = "";
            this.galleryEl.style.flexDirection = "";
            this.assetGrid.style.maxHeight = "";
            this.assetGrid.style.flex = "";
            this.assetGrid.style.overflow = "hidden";
            this.assetGrid.style.minHeight = "0";
            this.assetGrid.style.gridTemplateColumns = "";
            this.container.insertBefore(this.galleryEl, this._galleryNextSibling || null);
        }

        // Reparent container back to node
        if (this._nodeParent) {
            this._nodeParent.insertBefore(this.container, this._nodeSibling);
        }
        this.container.style.flex = "";
        this.container.style.overflow = "";

        // Hide overlay
        this._fullscreenOverlay.style.display = "none";

        // Restore heights
        this.isFullscreen = false;
        EditorWidget._activeFullscreen = null;
        this._timelineHeight = TIMELINE_HEIGHT;
        this._galleryHeight = GALLERY_HEIGHT;
        this._fullscreenBtn.textContent = "⛶";
        this._fullscreenBtn.title = "Toggle fullscreen";

        // Reapply per-section scales (gallery transform, etc.)
        this._applyScales();

        // Restore node size to what it was before fullscreen
        if (this._savedNodeSize) {
            this.widgetHost?.setSize?.(this._savedNodeSize);
            this._savedNodeSize = null;
        } else {
            this.widgetHost?.setSize?.(this.widgetHost?.computeSize?.());
        }

        this.onFullscreenExit?.();
    }

    _toggleFullscreen() {
        if (this.isFullscreen) {
            void this._requestExitFullscreen({ reason: "toggle" });
        } else {
            this._enterFullscreen();
        }
    }

    // ── Keyboard Events ──────────────────────────────────────────────
    _setupKeyboardEvents() {
        // EDITOR consumer (priority 10). Window-capture root in
        // keyboard_ownership.js dispatches consumers highest-priority first;
        // returning `true` consumes the event, returning `false` lets dispatch
        // continue and (if no consumer claims it) the event reaches LiteGraph.
        // Every non-consume branch must `return false` — silent fall-through
        // would starve LiteGraph by accident.
        this._editorKeyConsumer = (e) => {
            const key = e.key;
            const normalizedKey = String(key || "").toLowerCase();
            const ctrl = e.ctrlKey || e.metaKey;
            const shift = e.shiftKey;

            // Guard: don't fire when typing in inputs (except Ctrl+Z/Y for undo/redo)
            const tag = document.activeElement?.tagName;
            const isUndo = ctrl && (normalizedKey === "z" || normalizedKey === "y");
            const isInspectOverlayInput = !!document.activeElement?.closest?.("[data-sonder-inspect-overlay='1']");
            const debugUndoRouting = (message, extra = {}) => {
                if (!ctrl || (normalizedKey !== "z" && normalizedKey !== "y")) return;
                this._keyboardDebug(message, this._keyboardDebugSnapshot(e, extra));
            };
            if (isInspectOverlayInput) return false;
            if ((tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") && !isUndo) return false;

            // Guard: only handle keys when our editor is focused
            // (fullscreen always focused, node mode only when user clicked inside)
            if (!this.isFullscreen && !this._editorFocused) {
                debugUndoRouting("skip undo routing: editor not focused");
                return false;
            }

            // ── Escape ──
            if (key === "Escape") {
                if (this.isFullscreen) { void this._requestExitFullscreen({ reason: "escape" }); return true; }
                if (this.selectedItems.length > 0) {
                    this.selectedItems = [];
                    this._renderTimeline();
                    this._updateToolbar();
                    return true;
                }
                return false;
            }

            // ── Undo / Redo ──
            if (ctrl && normalizedKey === "z" && !shift) {
                debugUndoRouting("consume undo", { branch: "undo" });
                this._activateGraphUndoSuppression("editor-undo");
                void this._undo();
                return true;
            }
            if (ctrl && (normalizedKey === "y" || (normalizedKey === "z" && shift))) {
                debugUndoRouting("consume redo", {
                    branch: normalizedKey === "y" ? "redo-y" : "redo-shift-z",
                });
                this._activateGraphUndoSuppression(
                    normalizedKey === "y" ? "editor-redo-y" : "editor-redo-shift-z"
                );
                void this._redo();
                return true;
            }

            // ── Delete ──
            if (key === "Delete" || key === "Backspace") {
                // Defer to gallery if it owns the current selection focus.
                if (this._assetGallery?.hasSelectionOwnership?.()) return false;
                if (this.selectedItems.length > 0) {
                    // Filter out locked-lane items before delete
                    this.selectedItems = this.selectedItems.filter(s => !this._isItemLocked(s));
                    if (this.selectedItems.length > 0) this._deleteSelectedItems();
                }
                return true; // consume even when nothing was deletable, so ComfyUI does not delete the node
            }

            // ── Space = play/pause ──
            if (key === " ") {
                if (this.isFullscreen) this._togglePlayback();
                return true;
            }

            // ── Arrow keys: frame navigation ──
            if (key === "ArrowLeft") {
                const step = shift ? 10 : 1;
                this.playhead = Math.max(0, this.playhead - step);
                this._onPlayheadChange();
                return true;
            }
            if (key === "ArrowRight") {
                const step = shift ? 10 : 1;
                this.playhead = Math.min(this.totalFrames, this.playhead + step);
                this._onPlayheadChange();
                return true;
            }

            // ── Home / End ──
            if (key === "Home") {
                this.playhead = 0;
                this._onPlayheadChange();
                return true;
            }
            if (key === "End") {
                this.playhead = this.totalFrames;
                this._onPlayheadChange();
                return true;
            }

            // ── I / O = set in/out points (selection) ──
            if (key === "i" || key === "I") {
                const maxFrame = Math.max(0, this.activeScene?.duration_frames || this.totalFrames);
                this._setSelectionStartFrame(this._snapSelectionFrame(this.playhead, { direction: "up", clampMax: maxFrame }));
                return true;
            }
            if (key === "o" || key === "O") {
                const maxFrame = Math.max(0, this.activeScene?.duration_frames || this.totalFrames);
                this._setSelectionEndFrame(this._snapSelectionFrame(this.playhead, { direction: "up", clampMax: maxFrame }));
                return true;
            }

            // ── X = clear selection (in/out points) ──
            if (key === "x" || key === "X") {
                this._clearTimelineSelection();
                return true;
            }

            // ── C = toggle razor mode ──
            if (key === "c" || key === "C") {
                this._razorMode = !this._razorMode;
                this._updateToolbar();
                return true;
            }

            // ── S = toggle snapping ──
            if (key === "s" || key === "S") {
                this._setSnappingEnabled(!this.snappingEnabled);
                return true;
            }
            if (key === "m" || key === "M") {
                void this._toggleSelectedMute();
                return true;
            }

            // ── T = toggle timecode ──
            if (key === "t" || key === "T") { this._toggleTimecodeMode(); this._updateToolbar(); return true; }
            if (key === "a" || key === "A") { this._toggleAnimatic(); return true; }

            // ── F = fit to view, Shift+F = zoom to selection ──
            if (key === "f" || key === "F") {
                if (e.shiftKey && this.selectionStart < this.selectionEnd) {
                    const canvas = this.timelineCanvas;
                    const rect = canvas.parentElement?.getBoundingClientRect();
                    const width = rect ? Math.floor(rect.width) : 400;
                    const margin = width * 0.03;
                    const availableWidth = width - this._labelW - margin;
                    const range = this.selectionEnd - this.selectionStart;
                    if (range > 0 && availableWidth > 0) {
                        // pixelsPerFrame mutation here is ephemeral; persistence happens only via _zoom.
                        this.pixelsPerFrame = Math.max(0.2, Math.min(40, availableWidth / range));
                        this.scrollX = this.selectionStart;
                    }
                    this._renderTimeline();
                } else {
                    this._fitToView();
                }
                return true;
            }

            // ── ? = shortcut overlay ──
            if (key === "?") { this._showShortcutOverlay(); return true; }

            // ── Zoom: +/- ──
            if (key === "=" || key === "+") { this._zoom(1); return true; }
            if (key === "-" || key === "_") { this._zoom(-1); return true; }

            return false;
        };
        this._editorKeyOff = registerKeyboardConsumer({
            id: this._keyboardConsumerId("editor"),
            priority: KEY_PRIORITY.EDITOR,
            keydown: this._editorKeyConsumer,
        });

        // Track editor focus: set when clicking inside editor, clear when clicking outside
        this._focusHandler = (e) => {
            if (this.isFullscreen) {
                // In fullscreen, editor always has focus
                this._editorFocused = true;
            } else {
                // In node mode, check if click is inside our container
                this._editorFocused = !!(this.container?.contains(e.target));
            }
        };
        document.addEventListener("mousedown", this._focusHandler, true);
    }

    _keyboardConsumerId(suffix) {
        const nodeId = this.widgetHost?.getNodeId?.() ?? this.node?.id ?? "anon";
        return `sonder-editor-${nodeId}:${suffix}`;
    }

    _keyboardDebugSnapshot(event, extra = {}) {
        return {
            key: event?.key ?? "",
            normalizedKey: String(event?.key || "").toLowerCase(),
            ctrl: !!(event?.ctrlKey || event?.metaKey),
            shift: !!event?.shiftKey,
            alt: !!event?.altKey,
            isFullscreen: !!this.isFullscreen,
            editorFocused: !!this._editorFocused,
            activeElement: describeKeyboardDebugElement(document.activeElement),
            consumers: debugKeyboardConsumers(),
            ...extra,
        };
    }

    _keyboardDebug(message, details) {
        if (!isKeyboardDebugEnabled()) return;
        if (details === undefined) {
            console.debug(`[Sonder][EditorKeyboard][${this._keyboardConsumerId("editor")}] ${message}`);
            return;
        }
        console.debug(`[Sonder][EditorKeyboard][${this._keyboardConsumerId("editor")}] ${message}`, details);
    }

    _activateGraphUndoSuppression(reason) {
        const suppress = typeof window !== "undefined"
            ? window.__SONDER_SUPPRESS_COMFY_GRAPH_UNDO__
            : null;
        if (typeof suppress !== "function") return;
        const nodeId = this.widgetHost?.getNodeId?.() ?? this.node?.id;
        suppress(reason, nodeId == null ? [] : [nodeId]);
        this._keyboardDebug("requested graph undo suppression", {
            reason,
            nodeId: nodeId ?? null,
        });
    }

    /** Called whenever the playhead changes position (arrow keys, etc.). */
    _visibleTimelineFrameSpan() {
        const rect = this.timelineCanvas?.parentElement?.getBoundingClientRect();
        const width = rect ? Math.floor(rect.width) : (this.timelineCanvas?.width || 400);
        return Math.max(1, (width - this._labelW) / Math.max(0.2, this.pixelsPerFrame));
    }

    _maybeAutoScrollToPlayhead() {
        if (!this._settings?.playback?.autoScrollPlayhead) return;
        const visibleFrames = this._visibleTimelineFrameSpan();
        const marginFrames = Math.max(2, Math.floor(visibleFrames * 0.12));
        const leftBound = this.scrollX + marginFrames;
        const rightBound = this.scrollX + visibleFrames - marginFrames;
        if (this.playhead < leftBound) {
            this.scrollX = Math.max(0, this.playhead - marginFrames);
        } else if (this.playhead > rightBound) {
            this.scrollX = Math.max(0, this.playhead - visibleFrames + marginFrames);
        }
        this._clampScrollX();
    }

    _onPlayheadChange() {
        this._maybeAutoScrollToPlayhead();
        this._renderTimeline();
        if (this.isFullscreen) this._renderViewportFrame();
        this._updateToolbar();
    }

    // ── Zoom ───────────────────────────────────────────────────────────
    _zoom(dir) {
        const oldPPF = this.pixelsPerFrame;
        this.pixelsPerFrame = Math.max(0.2, Math.min(40, this.pixelsPerFrame + dir * 0.5));
        if (this.pixelsPerFrame !== oldPPF) {
            updateEditorSettings({ layout: { timelinePixelsPerFrame: this.pixelsPerFrame } });
            this._renderTimeline();
        }
    }

    // ── UI Scale (per-section) ───────────────────────────────────────
    _setScale(key, value) {
        const clamped = Math.round(Math.max(0.7, Math.min(2.0, value)) * 10) / 10;
        const keyMap = {
            Toolbar: "scaleToolbar",
            TrackHeaders: "scaleTrackHeaders",
            Timeline: "scaleTimeline",
            Gallery: "scaleGallery",
        };
        const layoutKey = keyMap[key];
        if (!layoutKey) return;
        this._updateSettings({
            layout: {
                [layoutKey]: clamped,
            },
        });
    }

    _applyScales() {
        // A. Toolbar & Bars — CSS transform
        const st = this._scaleToolbar;
        for (const el of [this._sceneBar, this._toolbar, this._infoBar]) {
            if (!el) continue;
            el.style.transform = st !== 1.0 ? `scale(${st})` : "";
            el.style.transformOrigin = "top left";
            el.style.width = st !== 1.0 ? `${100 / st}%` : "";
        }
        // D. Asset Gallery — CSS zoom (not transform, because transform doesn't affect layout/scroll)
        if (this.galleryEl) {
            const sg = this._scaleGallery;
            this.galleryEl.style.zoom = sg !== 1.0 ? sg : "";
        }
        this._renderTimeline();
        // Notify ComfyUI that our size changed
        this.widgetHost?.markDirty?.();
    }

    _canvasMouseCoords(e) {
        const canvas = this.timelineCanvas;
        const rect = canvas.getBoundingClientRect();
        const rulerH = this._timelineRulerHeight();
        const sx = rect.width > 0 ? canvas.width / rect.width : 1;
        const sy = rect.height > 0 ? canvas.height / rect.height : 1;
        const rawY = (e.clientY - rect.top) * sy;
        return {
            x: (e.clientX - rect.left) * sx,
            rawY,
            y: rawY >= rulerH ? rawY + this.scrollY : rawY,
        };
    }

    // ── Helpers ───────────────────────────────────────────────────────
    _projectDirName() {
        if (!this.projectDir) return "";
        return this.projectDir.split(/[/\\]/).pop();
    }

    _assetDisplayName(asset, sourcePath = "") {
        if (asset?.name) return asset.name;
        if (sourcePath) {
            const fileName = sourcePath.split(/[/\\]/).pop();
            if (fileName) return fileName;
        }
        return "Untitled";
    }

    _formatClipTimelineLabel(clip, asset, isMissingClip) {
        const mode = this._clipLabelMode();
        if (mode === "hidden") return "";
        const name = this._assetDisplayName(asset, clip?.source_path || "");
        const dur = Math.max(1, (clip?.timeline_end_frame || 0) - (clip?.timeline_start_frame || 0));
        const durationText = this._timecodeMode === "timecode" ? this._frameToTimecode(dur) : `${dur}f`;
        let label = mode === "name_only" ? name : `${name} | ${durationText}`;
        if (isMissingClip) {
            label = `Missing | ${label}`;
        }
        const opacity = clip?.opacity ?? 1.0;
        if (opacity < 1.0) {
            label += ` | ${Math.round(opacity * 100)}%`;
        }
        return label;
    }

    _formatAudioTimelineLabel(track, asset, isMissingAudio) {
        const mode = this._clipLabelMode();
        if (mode === "hidden") return "";
        const name = this._assetDisplayName(asset, track?.source_path || "");
        const dur = Math.max(1, (track?.timeline_end_frame || 0) - (track?.timeline_start_frame || 0));
        const durationText = this._timecodeMode === "timecode" ? this._frameToTimecode(dur) : `${dur}f`;
        let label = mode === "name_only" ? name : `${name} | ${durationText}`;
        if (isMissingAudio) {
            label = `Missing | ${label}`;
        }
        if (track?.muted) {
            label += " | M";
        } else if ((track?.volume ?? 1.0) < 1.0) {
            label += ` | ${Math.round((track.volume ?? 1.0) * 100)}%`;
        }
        return label;
    }

    // ── Saved Selections ──────────────────────────────────────────────
    _toggleSavedSelectionsDropdown(e) {
        // Close if already open
        if (this._savedSelDropdown) {
            this._savedSelDropdown.remove();
            this._savedSelDropdown = null;
            return;
        }

        const rect = this._bookmarkBtn.getBoundingClientRect();
        const dd = document.createElement("div");
        dd.style.cssText = `
            position: fixed; left: ${rect.left}px; top: ${rect.bottom + 2}px;
            background: ${COLORS.panelRaised}; border: 1px solid ${COLORS.borderStrong}; border-radius: 8px;
            min-width: 200px; max-height: 300px; overflow-y: auto;
            z-index: 10002; font-size: 11px; box-shadow: 0 12px 28px rgba(0,0,0,0.42);
        `;

        // Save current selection option
        const saveItem = document.createElement("div");
        const hasSel = this.selectionStart < this.selectionEnd;
        saveItem.style.cssText = `
            padding: 6px 10px; cursor: ${hasSel ? "pointer" : "default"};
            color: ${hasSel ? lightenColor(COLORS.sceneBtnActive, 0.28) : COLORS.textMuted}; border-bottom: 1px solid ${COLORS.border};
        `;
        saveItem.textContent = hasSel ? `💾 Save Selection (${this._frameToTimecode(this.selectionStart)}–${this._frameToTimecode(this.selectionEnd)})` : "💾 Save Selection (no selection)";
        if (hasSel) {
            saveItem.addEventListener("mouseenter", () => { saveItem.style.background = COLORS.panelRaisedHover; });
            saveItem.addEventListener("mouseleave", () => { saveItem.style.background = ""; });
            saveItem.addEventListener("click", () => {
                this._saveCurrentSelection();
                dd.remove();
                this._savedSelDropdown = null;
            });
        }
        dd.appendChild(saveItem);

        // List saved selections
        const selections = this.activeScene?.saved_selections || [];
        if (selections.length === 0) {
            const empty = document.createElement("div");
            empty.style.cssText = `padding: 8px 10px; color: ${COLORS.textMuted}; font-style: italic;`;
            empty.textContent = "No saved selections";
            dd.appendChild(empty);
        } else {
            selections.forEach((sel, idx) => {
                const item = document.createElement("div");
                item.style.cssText = `padding: 5px 10px; cursor: pointer; color: ${COLORS.text}; display: flex; justify-content: space-between; align-items: center;`;
                item.addEventListener("mouseenter", () => { item.style.background = COLORS.panelRaisedHover; });
                item.addEventListener("mouseleave", () => { item.style.background = ""; });

                const label = document.createElement("span");
                const preCtx = Math.max(0, parseInt(sel.pre_context_frames, 10) || 0);
                const postCtx = Math.max(0, parseInt(sel.post_context_frames, 10) || 0);
                const ctxSuffix = (preCtx > 0 || postCtx > 0) ? ` | Ctx -${preCtx}/+${postCtx}` : "";
                label.textContent = `${sel.name} (${this._frameToTimecode(sel.start)}–${this._frameToTimecode(sel.end)}${ctxSuffix})`;
                item.appendChild(label);

                const delBtn = document.createElement("span");
                delBtn.textContent = "✕";
                delBtn.style.cssText = `color: ${COLORS.textMuted}; cursor: pointer; padding: 0 4px; font-size: 10px;`;
                delBtn.title = "Delete saved selection";
                delBtn.addEventListener("click", (ev) => {
                    ev.stopPropagation();
                    this._deleteSavedSelection(idx);
                    dd.remove();
                    this._savedSelDropdown = null;
                });
                item.appendChild(delBtn);

                item.addEventListener("click", () => {
                    this._recallSavedSelection(sel);
                    dd.remove();
                    this._savedSelDropdown = null;
                });

                // Right-click to rename
                item.addEventListener("contextmenu", (ev) => {
                    ev.preventDefault();
                    ev.stopPropagation();
                    const newName = prompt("Rename selection:", sel.name);
                    if (newName && newName.trim()) {
                        this._renameSavedSelection(idx, newName.trim());
                        dd.remove();
                        this._savedSelDropdown = null;
                    }
                });

                dd.appendChild(item);
            });
        }

        // Close on outside click
        const closeHandler = (ev) => {
            if (!dd.contains(ev.target) && ev.target !== this._bookmarkBtn) {
                dd.remove();
                this._savedSelDropdown = null;
                document.removeEventListener("mousedown", closeHandler, true);
            }
        };
        setTimeout(() => document.addEventListener("mousedown", closeHandler, true), 0);

        document.body.appendChild(dd);
        this._savedSelDropdown = dd;
    }

    async _saveCurrentSelection() {
        if (this.selectionStart >= this.selectionEnd || !this.activeScene) return;
        const name = prompt("Selection name:", `Sel ${(this.activeScene.saved_selections?.length || 0) + 1}`);
        if (!name || !name.trim()) return;

        try {
            const dirName = encodeURIComponent(this._projectDirName());
            const sceneId = this.activeScene.scene_id;
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}/saved_selections`), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    name: name.trim(),
                    start: this.selectionStart,
                    end: this.selectionEnd,
                    pre_context_frames: this._contextFrameValue("pre_context_frames"),
                    post_context_frames: this._contextFrameValue("post_context_frames"),
                }),
            });
            if (resp.ok) {
                await this._fetchScenes();
            }
        } catch (e) { console.error("Save selection failed:", e); }
    }

    _recallSavedSelection(sel) {
        this._setTimelineSelection(sel.start, sel.end, { render: false });
        this._setWidgetValue("pre_context_frames", Math.max(0, parseInt(sel.pre_context_frames, 10) || 0));
        this._setWidgetValue("post_context_frames", Math.max(0, parseInt(sel.post_context_frames, 10) || 0));
        this._refreshContextInputs();
        this._renderTimeline();
        this._updateToolbar();
    }

    async _deleteSavedSelection(idx) {
        if (!this.activeScene) return;
        try {
            const dirName = encodeURIComponent(this._projectDirName());
            const sceneId = this.activeScene.scene_id;
            await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}/saved_selections/${idx}`), { method: "DELETE" });
            await this._fetchScenes();
        } catch (e) { console.error("Delete saved selection failed:", e); }
    }

    async _renameSavedSelection(idx, newName) {
        if (!this.activeScene) return;
        try {
            const dirName = encodeURIComponent(this._projectDirName());
            const sceneId = this.activeScene.scene_id;
            await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}/saved_selections/${idx}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name: newName }),
            });
            await this._fetchScenes();
        } catch (e) { console.error("Rename saved selection failed:", e); }
    }

    // -- Timeline Export --------------------------------------------------
    _sceneHasAudio(scene = this.activeScene) {
        return Array.isArray(scene?.audio_tracks) && scene.audio_tracks.length > 0;
    }

    _exportSettings() {
        return {
            ...DEFAULT_EDITOR_SETTINGS.render.export,
            ...(this._settings?.render?.export || {}),
        };
    }

    _defaultCustomExportOptions() {
        return {
            custom_output_kind: CUSTOM_OUTPUT_KIND_VIDEO,
            custom_container: "mp4",
            custom_video_codec: "libx264",
            custom_pix_fmt: "yuv420p",
            custom_crf: 18,
            custom_encoder_preset: "slow",
            custom_audio_codec: "aac",
            custom_audio_bitrate_kbps: 192,
            custom_png_compression: 0,
            ...(this._exportSettings().lastCustomEncode || {}),
        };
    }

    _updateExportSettings(partial) {
        this._updateSettings({
            render: {
                export: partial,
            },
        });
    }

    _resolveQueueSelectionRange() {
        if (!this.activeScene) return null;
        const sceneDuration = Math.max(0, parseInt(this.activeScene.duration_frames, 10) || 0);
        const hasSelection = this.selectionStart < this.selectionEnd;
        return {
            sceneId: this.activeScene.scene_id,
            sceneName: this.activeScene.name,
            sceneDuration,
            selStart: hasSelection ? this.selectionStart : 0,
            selEnd: hasSelection ? this.selectionEnd : sceneDuration,
            hasSelection,
        };
    }

    _showExportPanel() {
        if (this._exportPanelHandle || this._exportPanelEl) return;
        if (!this.activeScene || !this.projectDir) return;
        const handle = mountTimelineExportPanel(this);
        if (!handle) return;
        this._exportPanelHandle = handle;
        this._exportPanelEl = handle.element;
        this._exportPanelKeyOff = handle.unregisterKeyboard || null;
        this._exportPanelToken = handle.token;
        this._exportStartPending = false;
        this._exportCancelRequested = false;
    }

    _postExportCancel(jobId) {
        if (!jobId || !this._projectDirName()) return;
        const dirName = encodeURIComponent(this._projectDirName());
        fetch(api.apiURL(`/sonder-editor/project/${dirName}/render_timeline/${jobId}/cancel`), {
            method: "POST",
        }).catch((error) => console.warn("[Sonder] Export cancel failed:", error));
    }

    _restoreExportControls(ui) {
        if (!ui) return;
        ui.controls.forEach((control) => { control.disabled = false; });
        if (ui.exportBtn) ui.exportBtn.disabled = false;
        if (ui.closeBtn) ui.closeBtn.disabled = false;
        if (ui.cancelBtn) {
            ui.cancelBtn.textContent = "Cancel";
            ui.cancelBtn.onclick = () => this._hideExportPanel();
        }
        ui.syncState?.();
    }

    _resetExportControlsAfterCancel(ui) {
        if (!ui) return;
        if (ui.progressEl) ui.progressEl.style.display = "none";
        if (ui.errorEl) ui.errorEl.textContent = "";
        this._restoreExportControls(ui);
    }

    _hideExportPanel() {
        const jobId = this._exportJobId;
        if (jobId) this._postExportCancel(jobId);
        if (this._exportPollTimer) {
            window.clearTimeout(this._exportPollTimer);
            this._exportPollTimer = null;
        }
        this._exportJobId = "";
        this._exportStartPending = false;
        this._exportCancelRequested = false;
        // Invalidate the mounted token so any in-flight start/poll callbacks
        // (which captured the prior token) bail instead of touching torn-down UI.
        this._exportPanelToken = 0;
        if (this._exportPanelKeyOff) {
            this._exportPanelKeyOff();
            this._exportPanelKeyOff = null;
        }
        if (this._exportPanelHandle) {
            this._exportPanelHandle.cleanup();
            this._exportPanelHandle = null;
        }
        if (this._exportPanelEl) {
            this._exportPanelEl.remove();
            this._exportPanelEl = null;
        }
    }

    async _readExportError(resp, fallback) {
        try {
            const payload = await resp.json();
            return payload?.error || payload?.message || fallback;
        } catch {
            return fallback;
        }
    }

    _exportPhaseMessage(payload) {
        if (payload?.message) return payload.message;
        const phase = String(payload?.phase || "");
        if (phase === "compositing") return "Compositing frames...";
        if (phase === "mixing_audio") return "Mixing audio...";
        if (phase === "encoding") return "Encoding...";
        if (phase === "registering") return "Registering asset...";
        if (phase === "placing_take") return "Placing take...";
        if (phase === "cancelling") return "Cancelling...";
        return "Exporting...";
    }

    async _startTimelineExport(payload, ui) {
        const token = this._exportPanelToken;
        this._exportStartPending = true;
        this._exportCancelRequested = false;
        try {
            const dirName = encodeURIComponent(this._projectDirName());
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/render_timeline`), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (!resp.ok) {
                const message = await this._readExportError(resp, `Export failed: ${resp.status}`);
                throw new Error(message);
            }
            const data = await resp.json();
            const jobId = data.job_id || "";
            this._exportStartPending = false;
            // Panel torn down, or the user pressed Cancel before the job id
            // arrived: do not adopt the job into (possibly stale) UI; cancel it
            // on the backend so it cannot run orphaned, and reset if still open.
            if (this._exportPanelToken !== token || this._exportCancelRequested) {
                const cancelRequested = this._exportCancelRequested;
                this._exportCancelRequested = false;
                if (jobId) this._postExportCancel(jobId);
                if (cancelRequested && this._exportPanelToken === token) {
                    this._exportJobId = "";
                    this._resetExportControlsAfterCancel(ui);
                }
                return;
            }
            if (!jobId) {
                // A 200 with no job id cannot be polled; fail loud so controls
                // re-enable instead of leaving the panel wedged on "Starting...".
                throw new Error("Export did not start.");
            }
            this._exportJobId = jobId;
            ui.progressEl.textContent = this._exportPhaseMessage(data);
            this._pollTimelineExport(ui);
        } catch (error) {
            this._exportStartPending = false;
            if (this._exportPanelToken !== token) return;
            const cancelRequested = this._exportCancelRequested;
            this._exportCancelRequested = false;
            this._exportJobId = "";
            if (cancelRequested) {
                this._resetExportControlsAfterCancel(ui);
                return;
            }
            ui.errorEl.textContent = error?.message || "Export failed.";
            this._restoreExportControls(ui);
        }
    }

    _pollTimelineExport(ui) {
        if (!this._exportJobId) return;
        const token = this._exportPanelToken;
        this._exportPollTimer = window.setTimeout(async () => {
            if (this._exportPanelToken !== token || !this._exportJobId) return;
            try {
                const dirName = encodeURIComponent(this._projectDirName());
                const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/render_timeline/${this._exportJobId}`));
                if (this._exportPanelToken !== token) return;
                if (!resp.ok) {
                    const message = await this._readExportError(resp, `Export status failed: ${resp.status}`);
                    throw new Error(message);
                }
                const data = await resp.json();
                if (this._exportPanelToken !== token) return;
                ui.progressEl.textContent = this._exportPhaseMessage(data);
                if (data.status === "completed") {
                    await this._handleTimelineExportComplete(data);
                    return;
                }
                if (data.status === "failed") {
                    throw new Error(data.error || "Export failed.");
                }
                if (data.status === "cancelled") {
                    this._exportJobId = "";
                    this._resetExportControlsAfterCancel(ui);
                    return;
                }
                this._pollTimelineExport(ui);
            } catch (error) {
                if (this._exportPanelToken !== token) return;
                this._exportJobId = "";
                ui.errorEl.textContent = error?.message || "Export failed.";
                this._restoreExportControls(ui);
            }
        }, 650);
    }

    async _cancelTimelineExport(progressEl) {
        // Cancel pressed before the backend returned a job id: flag it so the
        // pending start cancels the late job and resets the panel.
        if (this._exportStartPending && !this._exportJobId) {
            this._exportCancelRequested = true;
            if (progressEl) progressEl.textContent = "Cancelling...";
            return;
        }
        if (!this._exportJobId) {
            this._hideExportPanel();
            return;
        }
        try {
            const dirName = encodeURIComponent(this._projectDirName());
            await fetch(api.apiURL(`/sonder-editor/project/${dirName}/render_timeline/${this._exportJobId}/cancel`), {
                method: "POST",
            });
            if (progressEl) progressEl.textContent = "Cancelling...";
        } catch (error) {
            console.warn("[Sonder] Export cancel failed:", error);
        }
    }

    async _handleTimelineExportComplete(data) {
        const asset = data?.result?.asset || null;
        this._exportJobId = "";
        this._hideExportPanel();
        this._showToast(asset?.name ? `Exported ${asset.name}` : "Export complete");
        await Promise.all([this._fetchAssets(), this._fetchScenes()]);
        if (asset?.asset_id) {
            this._inspectAssetInGallery(asset.asset_id);
        }
    }

    // ── Render Queue ─────────────────────────────────────────────────
    _buildQueueSnapshot(selStart, selEnd) {
        const range = this._resolveQueueSelectionRange();
        if (!range) return null;

        const sceneDuration = range.sceneDuration;
        const clampedStart = Math.max(0, Math.min(sceneDuration, parseInt(selStart, 10) || 0));
        const clampedEnd = Math.max(clampedStart, Math.min(sceneDuration, parseInt(selEnd, 10) || 0));
        const preContextFrames = this._contextFrameValue("pre_context_frames");
        const postContextFrames = this._contextFrameValue("post_context_frames");
        const maskPreOffset = this._contextFrameValue("mask_pre_offset");
        const maskPostOffset = this._contextFrameValue("mask_post_offset");
        const snapshotStart = Math.max(0, clampedStart - preContextFrames);
        const snapshotEnd = Math.min(sceneDuration, clampedEnd + postContextFrames);

        const promptHidden = !!this.activeScene.prompt_track_config?.hidden;
        let prompt = promptHidden ? "" : (this.activeScene.prompt || "");
        const sections = promptHidden ? [] : (this.activeScene.prompt_sections || []);
        const promptSections = [];
        for (const s of sections) {
            if (s.start_frame < snapshotEnd && s.end_frame > snapshotStart) {
                promptSections.push({
                    start_frame: s.start_frame,
                    end_frame: s.end_frame,
                    prompt: s.prompt,
                });
            }
            if (s.start_frame <= snapshotStart && s.end_frame >= snapshotEnd) { prompt = s.prompt; break; }
            if (s.start_frame < snapshotEnd && s.end_frame > snapshotStart) { prompt = s.prompt; break; }
        }

        const guideFrameSnapshots = [];
        const guideTrackHidden = !!this.activeScene.guide_track_config?.hidden;
        for (const guide of (this.activeScene.guide_frames || [])) {
            let frameIndex = parseInt(guide.frame_index, 10) || 0;
            if (frameIndex === -1) frameIndex = Math.max(0, sceneDuration - 1);
            if (snapshotStart <= frameIndex && frameIndex < snapshotEnd) {
                guideFrameSnapshots.push({
                    frame_index: frameIndex,
                    asset_id: guide.asset_id,
                    source: guide.source || "asset",
                    strength: guide.strength ?? 1.0,
                    muted: guideTrackHidden || !!guide.muted,
                });
            }
        }

        return {
            scene_id: range.sceneId,
            scene_name: range.sceneName,
            selection_start: clampedStart,
            selection_end: clampedEnd,
            prompt,
            context_frames: Math.max(preContextFrames, postContextFrames),
            pre_context_frames: preContextFrames,
            post_context_frames: postContextFrames,
            mask_pre_offset: maskPreOffset,
            mask_post_offset: maskPostOffset,
            guide_frame_snapshots: guideFrameSnapshots,
            prompt_sections: promptSections,
            scene_width: Math.max(0, parseInt(this.activeScene.width, 10) || 0),
            scene_height: Math.max(0, parseInt(this.activeScene.height, 10) || 0),
            scene_fps: Math.max(0, parseFloat(this.activeScene.fps) || 0),
            template_id: this._templateId || "free",
            frame_constraint: this._resolveFrameConstraintForTemplate(this._templateId),
            take_placement_mode: this._settings?.render?.takePlacementMode ?? "trimmed",
        };
    }

    _buildBatchQueueRanges(selStart, selEnd, chunkSize, firstChunkSize = chunkSize) {
        const start = Math.max(0, parseInt(selStart, 10) || 0);
        const end = Math.max(start, parseInt(selEnd, 10) || 0);
        const size = Math.max(1, parseInt(chunkSize, 10) || 1);
        const firstSize = Math.max(1, parseInt(firstChunkSize, 10) || size);
        if (end <= start) {
            return [];
        }

        const ranges = [];
        let cursor = start;
        let isFirst = true;
        while (cursor < end) {
            const thisSize = isFirst ? firstSize : size;
            const nextEnd = Math.min(cursor + thisSize, end);
            ranges.push({ start: cursor, end: nextEnd });
            cursor = nextEnd;
            isFirst = false;
        }
        return ranges;
    }

    _updateBatchButtonLabel() {
        if (!this._batchQueueBtn) return;

        const range = this._resolveQueueSelectionRange();
        if (!range) {
            this._batchQueueBtn.textContent = "+ Batch";
            this._batchQueueBtn.title = "Add the current selection to the render queue as chunked jobs";
            return;
        }

        const preContextFrames = this._contextFrameValue("pre_context_frames");
        const postContextFrames = this._contextFrameValue("post_context_frames");
        const { chunkSize, firstChunkSize } = resolveBatchChunkSizes({
            settings: this._settings,
            template: this._getActiveTemplate(),
            preContext: preContextFrames,
            postContext: postContextFrames,
            selectionStart: range.selStart,
        });
        const chunks = this._buildBatchQueueRanges(range.selStart, range.selEnd, chunkSize, firstChunkSize);
        const chunkCount = Math.max(1, chunks.length);
        const scopeLabel = range.hasSelection ? "selection" : "scene";
        const modeLabel = preContextFrames > 0 || postContextFrames > 0
            ? "Progressive via queued context"
            : "Independent with zero context";

        this._batchQueueBtn.textContent = `+ Batch (${chunkCount})`;
        this._batchQueueBtn.title = `Add current ${scopeLabel} to render queue as ${chunkCount} chunk${chunkCount === 1 ? "" : "s"} — ${modeLabel}`;
    }

    async _readQueueError(resp, fallback) {
        try {
            const payload = await resp.json();
            if (payload?.error) {
                return payload.error;
            }
        } catch {
            // Fall through to raw text.
        }

        try {
            const text = await resp.text();
            if (text) {
                return text;
            }
        } catch {
            // Ignore text-read failures.
        }
        return fallback;
    }

    _flashQueueButton(button) {
        if (!button) return;
        setButtonVariant(button, "active");
        window.setTimeout(() => {
            if (!button.isConnected) return;
            setButtonVariant(button, button.dataset.sonderBaseVariant || "primary");
        }, 500);
    }

    async _addToRenderQueue() {
        const range = this._resolveQueueSelectionRange();
        if (!range) return;

        const snapshot = this._buildQueueSnapshot(range.selStart, range.selEnd);
        if (!snapshot) return;

        const tempId = `temp-queue-${Date.now().toString(36)}-${Math.random().toString(16).slice(2, 8)}`;
        const optimisticJob = {
            ...snapshot,
            job_id: tempId,
            status: "pending",
            progress: 0,
            error: "",
            created_at: new Date().toISOString(),
            completed_at: "",
            result_asset_id: "",
        };
        this._renderQueue = [...(this._renderQueue || []), optimisticJob];
        this._applyStoredQueueBatchCollapseState();
        this._renderQueuePanel();

        try {
            const dirName = encodeURIComponent(this._projectDirName());
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/queue`), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(snapshot),
            });
            if (resp.ok) {
                const createdJob = await resp.json();
                this._renderQueue = (this._renderQueue || []).map((job) => job.job_id === tempId ? createdJob : job);
                this._flashQueueButton(this._queueBtn);
                this._applyStoredQueueBatchCollapseState();
                this._renderQueuePanel();
            } else {
                this._renderQueue = (this._renderQueue || []).filter((job) => job.job_id !== tempId);
                this._renderQueuePanel();
                await this._fetchRenderQueue();
            }
        } catch (e) {
            this._renderQueue = (this._renderQueue || []).filter((job) => job.job_id !== tempId);
            this._renderQueuePanel();
            await this._fetchRenderQueue();
            console.error("Add to queue failed:", e);
        }
    }

    async _addBatchToRenderQueue() {
        const range = this._resolveQueueSelectionRange();
        if (!range) return;

        const { chunkSize, firstChunkSize } = resolveBatchChunkSizes({
            settings: this._settings,
            template: this._getActiveTemplate(),
            preContext: this._contextFrameValue("pre_context_frames"),
            postContext: this._contextFrameValue("post_context_frames"),
            selectionStart: range.selStart,
        });
        const chunks = this._buildBatchQueueRanges(range.selStart, range.selEnd, chunkSize, firstChunkSize);
        if (chunks.length <= 1) {
            await this._addToRenderQueue();
            return;
        }

        const dirName = encodeURIComponent(this._projectDirName());
        const batchId = globalThis.crypto?.randomUUID?.()
            || `${Date.now().toString(36)}-${Math.random().toString(16).slice(2, 10)}`;

        let snapshots = [];
        let tempJobs = [];
        let tempIds = new Set();

        try {
            snapshots = chunks.map((chunk, index) => {
                const snapshot = this._buildQueueSnapshot(chunk.start, chunk.end);
                if (!snapshot) {
                    throw new Error("Failed to build batch queue snapshot.");
                }
                return {
                    ...snapshot,
                    batch_id: batchId,
                    batch_total: chunks.length,
                    batch_index: index,
                };
            });
            tempJobs = snapshots.map((snapshot, index) => ({
                ...snapshot,
                job_id: `temp-batch-${Date.now().toString(36)}-${index}-${Math.random().toString(16).slice(2, 8)}`,
                status: "pending",
                progress: 0,
                error: "",
                created_at: new Date().toISOString(),
                completed_at: "",
                result_asset_id: "",
            }));
            tempIds = new Set(tempJobs.map((job) => job.job_id));
            this._renderQueue = [...(this._renderQueue || []), ...tempJobs];
            this._applyStoredQueueBatchCollapseState();
            this._renderQueuePanel();

            const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/queue/batch`), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ jobs: snapshots }),
            });
            if (!resp.ok) {
                const message = await this._readQueueError(resp, `Add batch failed: ${resp.status}`);
                this._renderQueue = (this._renderQueue || []).filter((job) => !tempIds.has(job.job_id));
                this._renderQueuePanel();
                await this._fetchRenderQueue();
                alert(message);
                return;
            }
            const payload = await resp.json();
            const createdJobs = Array.isArray(payload?.jobs) ? payload.jobs : [];
            this._renderQueue = (this._renderQueue || []).map((job) => {
                if (!tempIds.has(job.job_id)) return job;
                const tempIndex = tempJobs.findIndex((temp) => temp.job_id === job.job_id);
                return createdJobs[tempIndex] || job;
            });
            this._flashQueueButton(this._batchQueueBtn);
            this._applyStoredQueueBatchCollapseState();
            this._renderQueuePanel();
        } catch (e) {
            console.error("Add batch to queue failed:", e);
            this._renderQueue = (this._renderQueue || []).filter((job) => !tempIds.has(job.job_id));
            this._renderQueuePanel();
            await this._fetchRenderQueue();
            alert(e?.message || "Add batch to queue failed.");
        }
    }

    async _fetchRenderQueue() {
        if (!this._projectDirName()) return;
        try {
            const dirName = encodeURIComponent(this._projectDirName());
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/queue`));
            if (resp.ok) {
                this._renderQueue = await resp.json();
                this._applyStoredQueueBatchCollapseState();
                this._renderQueuePanel();
            }
        } catch (e) { console.error("Fetch queue failed:", e); }
    }

    async _clearCompletedRenderQueue() {
        if (!this._projectDirName()) return;
        const previousQueue = [...(this._renderQueue || [])];
        this._renderQueue = previousQueue.filter((job) => String(job.status || "").toLowerCase() !== "completed");
        this._renderQueuePanel();
        try {
            const dirName = encodeURIComponent(this._projectDirName());
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/queue`), { method: "DELETE" });
            if (!resp.ok) {
                const message = await this._readQueueError(resp, `Clear completed renders failed: ${resp.status}`);
                throw new Error(message);
            }
        } catch (e) {
            this._renderQueue = previousQueue;
            this._renderQueuePanel();
            await this._fetchRenderQueue();
            console.error("Clear completed renders failed:", e);
            this._showToast(e?.message || "Clear completed renders failed.");
            throw e;
        }
    }

    _updateQueueHeaderLabel() {
        if (!this._queueHeaderLabel) return;
        this._queueHeaderLabel.textContent = `${this._queueExpanded ? "▾" : "▸"} Render Queue (${(this._renderQueue || []).length})`;
    }

    _applyQueueExpandedState() {
        if (!this._queueContainer) return;
        this._queueContainer.style.maxHeight = this._queueExpanded ? "200px" : "0";
        this._queueContainer.style.overflowY = this._queueExpanded ? "auto" : "hidden";
        this._updateQueueHeaderLabel();
    }

    _setQueueExpanded(expanded, options = {}) {
        const { persist = false, fetch = false } = options;
        const nextExpanded = !!expanded;
        const changed = this._queueExpanded !== nextExpanded;
        this._queueExpanded = nextExpanded;
        this._applyQueueExpandedState();
        if (persist && this._settings.layout.queuePanelExpanded !== nextExpanded) {
            this._updateSettings({
                layout: {
                    queuePanelExpanded: nextExpanded,
                },
            });
        }
        if ((changed || fetch) && nextExpanded) {
            this._fetchRenderQueue();
        }
    }

    _setRenderQueueActive(active, options = {}) {
        const { syncWidget = true } = options;
        const nextActive = !!active;
        const changed = this.renderQueueActive !== nextActive;
        this.renderQueueActive = nextActive;
        if (syncWidget) {
            this._setWidgetValue("render_queue_active", nextActive);
        }
        if (changed) {
            this._updateQueueChromeStatus();
            this._renderQueuePanel();
        }
    }

    async _deleteRenderQueueJob(job) {
        if (!this._projectDirName() || !job?.job_id) return;
        const previousQueue = [...(this._renderQueue || [])];
        this._renderQueue = previousQueue.filter((candidate) => candidate.job_id !== job.job_id);
        this._renderQueuePanel();
        try {
            const dirName = encodeURIComponent(this._projectDirName());
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/queue/${job.job_id}`), { method: "DELETE" });
            if (!resp.ok) {
                const message = await this._readQueueError(resp, `Delete queue job failed: ${resp.status}`);
                throw new Error(message);
            }
        } catch (e) {
            this._renderQueue = previousQueue;
            this._renderQueuePanel();
            await this._fetchRenderQueue();
            console.error("Delete queue job failed:", e);
            this._showToast(e?.message || "Delete queue job failed.");
            throw e;
        }
    }

    _renderQueuePanel() {
        if (!this._queueContainer) return;
        this._queueContainer.innerHTML = "";
        this._updateQueueHeaderLabel();
        this._updateQueueChromeStatus();
        const collapsedIds = new Set(
            Array.from(this._currentRenderQueueBatchIds())
                .filter((batchId) => this._queueBatchExpanded[batchId] === false)
        );
        mountSharedRenderQueue(this._queueContainer, {
            jobs: this._renderQueue || [],
            queueActive: this.renderQueueActive !== false,
            surface: "fullscreen",
            projectKey: this._queueBatchCollapseProjectKey(),
            timecodeMode: this._timecodeMode,
            fallbackFps: this._effectiveFps,
            collapsedBatchIds: collapsedIds,
            emptyText: "Queue empty - use + Queue or + Batch to add jobs",
            showDeleteJob: true,
            showClearCompleted: true,
            onSetQueueActive: (active) => this._setRenderQueueActive(active),
            onDeleteJob: async (job) => await this._deleteRenderQueueJob(job),
            onClearCompleted: async () => await this._clearCompletedRenderQueue(),
            onBatchCollapsedChange: (nextCollapsedIds) => {
                this._setQueueBatchCollapsedIds(nextCollapsedIds, { persist: true });
            },
        });
    }

    // ── Undo / Redo ───────────────────────────────────────────────────

    // ── Thumbnail Strip & Waveform Caches ───────────────────────────────

    _getOrLoadThumbStrip(assetId) {
        const entry = this._thumbStripCache[assetId];
        if (entry) return entry.loaded ? entry : null;

        // Start loading
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const cache = { img: new Image(), frameWidth: 0, numFrames: 0, loaded: false };
        this._thumbStripCache[assetId] = cache;

        const markerId = sessionDiagBeginLoad("thumb_strip", { asset_id: assetId, project_dir: dirName });

        // Fetch info first
        fetch(api.apiURL(`/sonder-editor/project/${dirName}/thumbnail_strip/${assetId}?info=1`))
            .then(r => r.ok ? r.json() : null)
            .then(info => {
                if (!info) {
                    sessionDiagEndLoad("thumb_strip", markerId, { asset_id: assetId, ok: false, reason: "no_info" });
                    return;
                }
                cache.frameWidth = info.frame_width;
                cache.numFrames = info.num_frames;
                cache.img.onload = () => {
                    cache.loaded = true;
                    sessionDiagEndLoad("thumb_strip", markerId, {
                        asset_id: assetId,
                        ok: true,
                        num_frames: cache.numFrames,
                        frame_width: cache.frameWidth,
                    });
                    this._renderTimeline();
                };
                cache.img.src = api.apiURL(`/sonder-editor/project/${dirName}/thumbnail_strip/${assetId}`);
            })
            .catch((err) => {
                sessionDiagEndLoad("thumb_strip", markerId, {
                    asset_id: assetId, ok: false, reason: "error",
                    error: String(err && err.message ? err.message : err),
                });
            });

        return null;
    }

    _getOrLoadWaveform(assetId) {
        const entry = this._waveformCache[assetId];
        if (entry) return entry.loaded ? entry : null;

        const dirName = this.projectDir.split(/[/\\]/).pop();
        const cache = { peaks: [], numBuckets: 0, loaded: false };
        this._waveformCache[assetId] = cache;

        const markerId = sessionDiagBeginLoad("waveform", { asset_id: assetId, project_dir: dirName });

        fetch(api.apiURL(`/sonder-editor/project/${dirName}/waveform/${assetId}`))
            .then(r => r.ok ? r.json() : null)
            .then(data => {
                if (!data) {
                    sessionDiagEndLoad("waveform", markerId, { asset_id: assetId, ok: false, reason: "no_data" });
                    return;
                }
                cache.peaks = data.peaks;
                cache.numBuckets = data.num_buckets;
                cache.loaded = true;
                sessionDiagEndLoad("waveform", markerId, {
                    asset_id: assetId, ok: true, num_buckets: cache.numBuckets,
                });
                this._renderTimeline();
            })
            .catch((err) => {
                sessionDiagEndLoad("waveform", markerId, {
                    asset_id: assetId, ok: false, reason: "error",
                    error: String(err && err.message ? err.message : err),
                });
            });

        return null;
    }

    // ── Undo / Redo ──────────────────────────────────────────────────────

    /** Capture a snapshot of the active scene BEFORE a mutation. */
    _pushUndo(label = "edit") {
        if (!this.activeScene || !this.activeSceneId) return;
        // Deep-clone the scene dict as snapshot
        const snapshot = JSON.parse(JSON.stringify(this.activeScene));
        this._undoStack.push({
            sceneId: this.activeSceneId,
            snapshot,
            label,
        });
        // Trim to max size
        if (this._undoStack.length > this._maxUndoSteps) {
            this._undoStack.shift();
        }
        // Any new action clears the redo stack
        this._redoStack = [];
    }

    async _undo() {
        if (this._undoStack.length === 0) {
            this._keyboardDebug("undo skipped: empty stack", {
                activeSceneId: this.activeSceneId || "",
                undoDepth: this._undoStack.length,
                redoDepth: this._redoStack.length,
            });
            return;
        }
        const entry = this._undoStack.pop();
        this._keyboardDebug("undo start", {
            entrySceneId: entry.sceneId,
            label: entry.label,
            activeSceneId: this.activeSceneId || "",
            undoDepthAfterPop: this._undoStack.length,
            redoDepthBeforePush: this._redoStack.length,
            editorFocused: !!this._editorFocused,
            activeElement: describeKeyboardDebugElement(document.activeElement),
        });

        // Save current state to redo stack before restoring
        if (this.activeScene && this.activeSceneId === entry.sceneId) {
            this._redoStack.push({
                sceneId: this.activeSceneId,
                snapshot: JSON.parse(JSON.stringify(this.activeScene)),
                label: entry.label,
            });
        }

        await this._restoreScene(entry.sceneId, entry.snapshot);
        this._keyboardDebug("undo complete", {
            activeSceneId: this.activeSceneId || "",
            undoDepth: this._undoStack.length,
            redoDepth: this._redoStack.length,
            editorFocused: !!this._editorFocused,
            activeElement: describeKeyboardDebugElement(document.activeElement),
        });
    }

    async _redo() {
        if (this._redoStack.length === 0) {
            this._keyboardDebug("redo skipped: empty stack", {
                activeSceneId: this.activeSceneId || "",
                undoDepth: this._undoStack.length,
                redoDepth: this._redoStack.length,
            });
            return;
        }
        const entry = this._redoStack.pop();
        this._keyboardDebug("redo start", {
            entrySceneId: entry.sceneId,
            label: entry.label,
            activeSceneId: this.activeSceneId || "",
            undoDepthBeforePush: this._undoStack.length,
            redoDepthAfterPop: this._redoStack.length,
            editorFocused: !!this._editorFocused,
            activeElement: describeKeyboardDebugElement(document.activeElement),
        });

        // Save current state to undo stack before restoring
        if (this.activeScene && this.activeSceneId === entry.sceneId) {
            this._undoStack.push({
                sceneId: this.activeSceneId,
                snapshot: JSON.parse(JSON.stringify(this.activeScene)),
                label: entry.label,
            });
        }

        await this._restoreScene(entry.sceneId, entry.snapshot);
        this._keyboardDebug("redo complete", {
            activeSceneId: this.activeSceneId || "",
            undoDepth: this._undoStack.length,
            redoDepth: this._redoStack.length,
            editorFocused: !!this._editorFocused,
            activeElement: describeKeyboardDebugElement(document.activeElement),
        });
    }

    async _restoreScene(sceneId, snapshot) {
        if (!this.projectDir) return;
        const dirName = this.projectDir.split(/[/\\]/).pop();
        this._keyboardDebug("restore start", {
            sceneId,
            activeSceneId: this.activeSceneId || "",
            projectDir: dirName || "",
            snapshotKeys: snapshot ? Object.keys(snapshot) : [],
        });

        try {
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}/restore`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(snapshot),
            });
            this._keyboardDebug("restore response", {
                sceneId,
                status: resp.status,
                ok: resp.ok,
            });
            if (resp.ok) {
                // If we're restoring a different scene, switch to it
                if (this.activeSceneId !== sceneId) {
                    this.activeSceneId = sceneId;
                }
                await this._fetchScenes();
                this._keyboardDebug("restore fetched scenes", {
                    sceneId,
                    activeSceneId: this.activeSceneId || "",
                    sceneCount: this.scenes.length,
                    activeElement: describeKeyboardDebugElement(document.activeElement),
                });
                this._renderTimeline();
                this._renderViewportFrame();
                this._keyboardDebug("restore render complete", {
                    sceneId,
                    activeSceneId: this.activeSceneId || "",
                    editorFocused: !!this._editorFocused,
                    activeElement: describeKeyboardDebugElement(document.activeElement),
                });
            }
        } catch (e) {
            this._keyboardDebug("restore failed", {
                sceneId,
                error: e?.message || String(e),
            });
            console.warn("[Sonder] Undo/redo restore failed:", e);
        }
    }

    // ── Widget Value Helpers ───────────────────────────────────────────
    _setWidgetValue(name, value) {
        if (this._applyingWidgetState) {
            this._setHostValueLocal(name, value);
            return;
        }
        if (this.widgetHost?.setValue) {
            this.widgetHost.setValue(name, value);
            return;
        }
        const widget = this.node?.widgets?.find(w => w.name === name);
        if (widget) widget.value = value;
    }

    _getWidgetValue(name, defaultValue = 0) {
        if (this.widgetHost?.getValue) {
            return this.widgetHost.getValue(name, defaultValue);
        }
        const widget = this.node?.widgets?.find(w => w.name === name);
        return widget ? widget.value : defaultValue;
    }

    // ── Public API ─────────────────────────────────────────────────────
    updateProject(projectDir) {
        if (projectDir === this.projectDir) return;
        this.projectDir = projectDir;
        this._frameConstraintHealedFor = "";
        this.activeSceneId = "";
        this.activeScene = null;
        this.scenes = [];
        this._queueBatchExpanded = {};
        this.sceneLabel.textContent = "Loading...";

        // Stop playback and clear video cache on project change
        this._stopPlayback();
        this._clearVideoCache();
        this._sweepRenderCache();

        // Fetch project settings (fps, resolution)
        this._fetchProjectSettings();

        this._renderQueue = [];
        this._renderQueuePanel();

        // Fetch assets first (triggers audio duration repair), then scenes
        this._fetchAssets().then(() => this._fetchScenes());
        if (this._queueExpanded) {
            this._fetchRenderQueue();
        }
        this._renderTimeline();
    }

    refresh(keys = []) {
        const wanted = new Set(keys);
        const wantsAssets = !wanted.size || wanted.has("assets");
        const wantsScenes = !wanted.size || wanted.has("scenes");
        const wantsProject = !wanted.size || wanted.has("project");
        if (this._hasPendingProjectMutations() && (wantsProject || wantsAssets || wantsScenes)) {
            const deferred = [];
            if (wantsProject) deferred.push("project");
            if (wantsAssets) deferred.push("assets");
            if (wantsScenes) deferred.push("scenes");
            this._deferProjectBackedRefresh(deferred, "external_refresh");
        } else {
            if (wantsProject) {
                this._fetchProjectSettings();
            }
            if (wantsAssets && wantsScenes) {
                this._fetchAssets().then(() => this._fetchScenes());
            } else {
                if (wantsAssets) {
                    this._fetchAssets();
                }
                if (wantsScenes) {
                    this._fetchScenes();
                }
            }
        }
        if (!wanted.size || wanted.has("queue")) {
            this._fetchRenderQueue();
        }
    }

    async _fetchProjectSettings({ ignoreMutationGate = false, reason = "project_settings" } = {}) {
        if (!this.projectDir) return;
        if (!ignoreMutationGate && this._hasPendingProjectMutations()) {
            this._deferProjectBackedRefresh(["project"], reason);
            return;
        }
        const dirName = this.projectDir.split(/[/\\]/).pop();
        try {
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}`));
            if (resp.ok) {
                const data = await resp.json();
                this.fps = data.fps || 24;
                if (data.resolution) {
                    this.sceneWidth = data.resolution[0] || 768;
                    this.sceneHeight = data.resolution[1] || 512;
                }
                this._templateId = getTemplateById(data.template_id, this._settings).id;
                await this._maybeHealFrameConstraint(this.projectDir, dirName, data.frame_constraint);
                this._syncSceneResolutionControls();
                this._updateViewportHeader();
                this._resizeViewportCanvas();
            }
        } catch (e) {
            console.warn("[Sonder] Failed to fetch project settings:", e);
        }
    }

    async _maybeHealFrameConstraint(projectDir, dirName, persistedConstraint) {
        if (!projectDir || this._frameConstraintHealedFor === projectDir) return;
        const expected = this._resolveFrameConstraintForTemplate(this._templateId);
        if (EditorWidget._frameConstraintsEqual(expected, persistedConstraint)) {
            this._frameConstraintHealedFor = projectDir;
            return;
        }
        if (this._hasPendingProjectMutations()) {
            this._deferProjectBackedRefresh(["project"], "frame_constraint_heal");
            return;
        }
        try {
            await this._runVersionedProjectMutation(
                `/sonder-editor/project/${encodeURIComponent(dirName)}`,
                {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ frame_constraint: expected }),
                },
                { projectId: dirName }
            );
            this._frameConstraintHealedFor = projectDir;
        } catch (error) {
            console.warn("[Sonder] Frame-constraint self-heal threw:", error);
        }
    }

    _ensureViewportSurface() {
        if (this._viewportSurface || !this._vpCanvas) return this._viewportSurface;
        this._viewportSurface = createViewportSurface({
            canvas: this._vpCanvas,
            initialLiveMediaEnabled: true,
            videoCache: this._videoCache,
            audioCache: this._audioCacheMap,
            imageCache: this._viewportImageCache,
            getScene: () => this.activeScene,
            getFrame: () => this.playhead,
            setFrame: (frame) => {
                this.playhead = Math.max(0, Math.min(this.totalFrames, Math.round(Number(frame) || 0)));
            },
            getTotalFrames: () => this.totalFrames,
            getFps: () => this._effectiveFps,
            getLoopRange: () => this._resolvePlaybackLoopRange(),
            shouldReturnToPlaybackStart: () => !!this._settings?.playback?.returnToPlaybackStart,
            isPrebufferEnabled: () => !!this._settings?.playback?.prebufferEnabled,
            getPrebufferLookaheadMs: () => this._settings?.playback?.prebufferLookaheadMs ?? 1000,
            onFrameChange: (frame, meta = {}) => {
                this.playhead = Math.max(0, Math.min(this.totalFrames, Math.round(Number(frame) || 0)));
                if (meta.reason === "playback" || meta.reason === "playback-loop" || meta.reason === "playback-stop-return") {
                    this._maybeAutoScrollToPlayhead();
                }
                this._renderTimeline();
                this._updateToolbar();
                this._updateTransportUI();
            },
            onTransportUpdate: () => this._updateTransportUI(),
            onPlaybackStateChange: (isPlaying) => {
                this.isPlaying = !!isPlaying;
                if (this._vpPlayBtn) {
                    this._vpPlayBtn.textContent = isPlaying ? "Pause" : "Play";
                }
                this._renderTimeline();
                this._updateToolbar();
            },
            getAssetForSourcePath: (sourcePath) => this._getAssetForSourcePath(sourcePath),
            getGuideAsset: (guide) => this._getGuideAsset(guide),
            includeMotionDrivers: () => !!this._animaticMode,
            isVideoLaneHidden: (trackIndex) => this._isLaneHidden(TRACK_TYPE.VIDEO, trackIndex || 0),
            isMotionDriverLaneHidden: (trackIndex) => this._isLaneHidden(TRACK_TYPE.MOTION_DRIVER, trackIndex || 0),
            isAudioLaneHidden: (laneIndex) => this._isLaneHidden(TRACK_TYPE.AUDIO, laneIndex || 0),
            isGuideTrackHidden: () => this._isGuideTrackHidden(),
            buildViewUrl: (sourcePath) => this._buildViewURL(sourcePath),
            buildThumbnailUrl: (assetId) => this.projectDir
                ? api.apiURL(`/sonder-editor/project/${this.projectDir.split(/[/\\]/).pop()}/thumbnail/${assetId}`)
                : null,
        });
        return this._viewportSurface;
    }

    _clearVideoCache() {
        if (this._viewportSurface) {
            this._viewportSurface.clearMediaCache();
            return;
        }
        for (const key of Object.keys(this._videoCache)) {
            const v = this._videoCache[key];
            if (v.pause) v.pause();
            if (v._blobUrl) URL.revokeObjectURL(v._blobUrl);
            if (v.removeAttribute) v.removeAttribute("src");
            if (v.load) v.load();
            delete this._videoCache[key];
        }
        for (const key of Object.keys(this._audioCacheMap)) {
            const a = this._audioCacheMap[key];
            a.pause();
            if (a._blobUrl) URL.revokeObjectURL(a._blobUrl);
            a.removeAttribute("src");
            delete this._audioCacheMap[key];
        }
        for (const key of Object.keys(this._viewportImageCache)) {
            delete this._viewportImageCache[key];
        }
    }

    // ── Viewport Rendering ──────────────────────────────────────────

    _resizeViewportCanvas() {
        if (!this._vpCanvas || !this._fsViewportContent) return;
        const rect = this._fsViewportContent.getBoundingClientRect();
        const containerW = rect.width;
        const containerH = rect.height;
        if (containerW <= 0 || containerH <= 0) return;

        const sceneWidth = this._effectiveSceneWidth;
        const sceneHeight = this._effectiveSceneHeight;
        if (sceneWidth <= 0 || sceneHeight <= 0) return;

        const aspect = sceneWidth / sceneHeight;
        let canvasW, canvasH;
        if (containerW / containerH > aspect) {
            // Container is wider than scene aspect — fit to height
            canvasH = Math.floor(containerH);
            canvasW = Math.floor(canvasH * aspect);
        } else {
            // Container is taller — fit to width
            canvasW = Math.floor(containerW);
            canvasH = Math.floor(canvasW / aspect);
        }

        const resolutionScale = this._settings.playback.resolution === "quarter"
            ? 0.25
            : this._settings.playback.resolution === "half"
                ? 0.5
                : 1;
        const targetBufferW = Math.max(1, Math.floor(canvasW * resolutionScale));
        const targetBufferH = Math.max(1, Math.floor(canvasH * resolutionScale));
        const targetStyleW = `${canvasW}px`;
        const targetStyleH = `${canvasH}px`;
        const backingChanged = this._vpCanvas.width !== targetBufferW || this._vpCanvas.height !== targetBufferH;
        const styleChanged = this._vpCanvas.style.width !== targetStyleW || this._vpCanvas.style.height !== targetStyleH;

        if (!backingChanged && !styleChanged) {
            return false;
        }

        if (backingChanged) {
            this._vpCanvas.width = targetBufferW;
            this._vpCanvas.height = targetBufferH;
            this._viewportSurface?.invalidatePlaybackComposite?.();
        }
        if (styleChanged) {
            this._vpCanvas.style.width = targetStyleW;
            this._vpCanvas.style.height = targetStyleH;
        }

        this._renderViewportFrame();
        return true;
    }

    _getClipAtFrame(frame) {
        if (!this.activeScene?.clips) return null;
        let best = null;
        for (const clip of this.activeScene.clips) {
            if (!this._isRenderClip(clip)) continue;
            if (frame >= clip.timeline_start_frame && frame < clip.timeline_end_frame) {
                if (this._isLaneHidden(TRACK_TYPE.VIDEO, clip.track_index || 0)) continue;
                if (!best || (clip.track_index || 0) > (best.track_index || 0)) {
                    best = clip;
                }
            }
        }
        return best;
    }

    _getMotionDriverClipAtFrame(frame) {
        if (!this.activeScene?.clips) return null;
        let best = null;
        for (const clip of this.activeScene.clips) {
            if (!this._isMotionDriverClip(clip)) continue;
            if (frame >= clip.timeline_start_frame && frame < clip.timeline_end_frame) {
                if (this._isLaneHidden(TRACK_TYPE.MOTION_DRIVER, clip.track_index || 0)) continue;
                if (!best || (clip.track_index || 0) > (best.track_index || 0)) {
                    best = clip;
                }
            }
        }
        return best;
    }

    /** Get all non-hidden clips at a given frame, sorted bottom-up (lowest track_index first) */
    _getClipsAtFrame(frame) {
        if (!this.activeScene?.clips) return [];
        return this.activeScene.clips
            .filter(clip => frame >= clip.timeline_start_frame && frame < clip.timeline_end_frame)
            .filter(clip => !clip.muted)
            .filter(clip => this._isRenderClip(clip) || (this._animaticMode && this._isMotionDriverClip(clip)))
            .filter(clip => !this._isLaneHidden(this._clipTrackType(clip), clip.track_index || 0))
            .sort((a, b) => (a.track_index || 0) - (b.track_index || 0));
    }

    _getAssetForSourcePath(sourcePath) {
        return sourcePath ? (this._pathToAsset[sourcePath] || null) : null;
    }

    _findAssetById(assetId) {
        if (!assetId) return null;
        for (const type of ["video", "image", "audio", "artifact"]) {
            const found = (this.assets?.[type] || []).find(asset => asset.asset_id === assetId);
            if (found) return found;
        }
        return null;
    }

    _isMissingSourcePath(sourcePath) {
        const asset = this._getAssetForSourcePath(sourcePath);
        return !asset || !!asset.missing;
    }

    _getGuideAsset(guide) {
        return guide ? (this.assets.image?.find((asset) => asset.asset_id === guide.asset_id) || null) : null;
    }

    _inspectAssetInGallery(asset) {
        const assetId = typeof asset === "string" ? asset : asset?.asset_id;
        if (!assetId) return;
        this._assetGallery?.revealAsset?.(assetId, {
            focusList: true,
            scrollIntoView: true,
            openInspector: true,
        });
    }

    _getAudioAtFrame(frame) {
        if (!this.activeScene?.audio_tracks) return null;
        for (const track of this.activeScene.audio_tracks) {
            if (frame >= track.timeline_start_frame && frame < track.timeline_end_frame) {
                return track;
            }
        }
        return null;
    }

    _getAudioTracksAtFrame(frame) {
        if (!this.activeScene?.audio_tracks) return [];
        return this.activeScene.audio_tracks.filter(
            a => frame >= a.timeline_start_frame && frame < a.timeline_end_frame
                && !this._isLaneHidden(TRACK_TYPE.AUDIO, a.lane_index || 0)
                && !a.muted
                && !this._isMissingSourcePath(a.source_path)
        );
    }

    _buildViewURL(sourcePath) {
        return buildProjectAssetViewURL(this.projectDir, sourcePath);
    }

    _drawMissingViewportPlaceholder(title, subtitle = "") {
        if (!this._vpCtx || !this._vpCanvas) return;
        const ctx = this._vpCtx;
        const w = this._vpCanvas.width;
        const h = this._vpCanvas.height;
        ctx.fillStyle = COLORS.bg;
        ctx.fillRect(0, 0, w, h);
        ctx.strokeStyle = COLORS.missingMediaBorder;
        ctx.lineWidth = 2;
        ctx.strokeRect(10, 10, Math.max(0, w - 20), Math.max(0, h - 20));
        ctx.fillStyle = COLORS.missingMediaText;
        ctx.font = this._canvasSansFont(Math.max(16, h / 14), 600);
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(title || "Missing asset", w / 2, h / 2 - 12);
        if (subtitle) {
            ctx.fillStyle = COLORS.textDim;
            ctx.font = this._canvasSansFont(Math.max(11, h / 24), 400);
            ctx.fillText(subtitle, w / 2, h / 2 + 14);
        }
    }

    _getOrCreateVideo(sourcePath) {
        if (this._videoCache[sourcePath]) return this._videoCache[sourcePath];

        // LRU eviction: max 8 cached videos (increased for multi-layer compositing)
        const keys = Object.keys(this._videoCache).filter(k => !k.startsWith("guide_"));
        if (keys.length >= 8) {
            const oldest = keys[0];
            const old = this._videoCache[oldest];
            old.pause();
            // Revoke blob URL to free memory
            if (old._blobUrl) URL.revokeObjectURL(old._blobUrl);
            old.removeAttribute("src");
            old.load();
            delete this._videoCache[oldest];
        }

        const url = this._buildViewURL(sourcePath);
        if (!url) return null;

        // Create video element — load as blob for proper seeking support
        // (ComfyUI's /view endpoint doesn't support HTTP Range requests,
        //  so browser can't seek streaming video. Blob URLs fix this.)
        const video = document.createElement("video");
        video.preload = "auto";
        video.muted = true;
        video.playsInline = true;

        const markerId = sessionDiagBeginLoad("video_blob", { source_path: sourcePath });

        fetch(url)
            .then(resp => resp.blob())
            .then(blob => {
                const blobUrl = URL.createObjectURL(blob);
                video._blobUrl = blobUrl;
                video.src = blobUrl;
                sessionDiagEndLoad("video_blob", markerId, {
                    source_path: sourcePath, ok: true, blob_size: blob.size, mime: blob.type,
                });
            })
            .catch(err => {
                console.warn("[Sonder] Failed to load video as blob, falling back to direct URL:", err);
                video.crossOrigin = "anonymous";
                video.src = url;
                sessionDiagEndLoad("video_blob", markerId, {
                    source_path: sourcePath, ok: false, reason: "blob_fetch_failed",
                    error: String(err && err.message ? err.message : err),
                });
            });

        this._videoCache[sourcePath] = video;
        return video;
    }

    _getOrCreateAudio(sourcePath) {
        if (this._audioCacheMap[sourcePath]) return this._audioCacheMap[sourcePath];
        const url = this._buildViewURL(sourcePath);
        if (!url) return null;

        // Load audio as blob for proper seeking support
        const audio = document.createElement("audio");
        audio.preload = "auto";

        const markerId = sessionDiagBeginLoad("audio_blob", { source_path: sourcePath });

        fetch(url)
            .then(resp => resp.blob())
            .then(blob => {
                const blobUrl = URL.createObjectURL(blob);
                audio._blobUrl = blobUrl;
                audio.src = blobUrl;
                sessionDiagEndLoad("audio_blob", markerId, {
                    source_path: sourcePath, ok: true, blob_size: blob.size, mime: blob.type,
                });
            })
            .catch(err => {
                console.warn("[Sonder] Failed to load audio as blob, falling back to direct URL:", err);
                audio.src = url;
                sessionDiagEndLoad("audio_blob", markerId, {
                    source_path: sourcePath, ok: false, reason: "blob_fetch_failed",
                    error: String(err && err.message ? err.message : err),
                });
            });

        this._audioCacheMap[sourcePath] = audio;
        return audio;
    }

    _renderViewportFrame() {
        const viewportSurface = this._ensureViewportSurface();
        if (viewportSurface) {
            viewportSurface.renderFrame();
            return;
        }
        if (!this._vpCanvas || !this._vpCtx) return;
        const ctx = this._vpCtx;
        const w = this._vpCanvas.width;
        const h = this._vpCanvas.height;
        if (w <= 0 || h <= 0) return;

        // Update transport UI
        this._updateTransportUI();

        // Animatic: guides render BENEATH video (as holdframe reference)
        const guide = this._getGuideAtFrame(this.playhead);
        const guideAsset = guide ? this._getGuideAsset(guide) : null;
        const clips = this._getClipsAtFrame(this.playhead);
        const playableClips = clips.filter((clip) => !this._isMissingSourcePath(clip.source_path));
        const missingClips = clips.filter((clip) => this._isMissingSourcePath(clip.source_path));

        if (playableClips.length === 0 && missingClips.length === 0 && !guide) {
            // No clip and no guide — show frame number centered
            ctx.fillStyle = COLORS.bg;
            ctx.fillRect(0, 0, w, h);
            ctx.fillStyle = COLORS.textMuted;
            ctx.font = this._canvasMonoFont(Math.max(16, h / 12), 400);
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            ctx.fillText(`Frame ${this.playhead}`, w / 2, h / 2);
            return;
        }

        if (playableClips.length === 0 && missingClips.length > 0) {
            const missingClip = missingClips[0];
            const missingAsset = this._pathToAsset[missingClip.source_path];
            this._drawMissingViewportPlaceholder("Missing clip", missingAsset?.name || missingClip.source_path.split(/[/\\]/).pop() || "");
            return;
        }

        if (playableClips.length === 0 && guide) {
            // Guide only — show as animatic reference (no video to cover it)
            if (this._seekAbort) {
                this._seekAbort();
                this._seekAbort = null;
            }
            if (!guideAsset) {
                this._drawMissingViewportPlaceholder("Missing guide", "Guide asset entry not found.");
                return;
            }
            if (guideAsset.missing) {
                this._drawMissingViewportPlaceholder("Missing guide", guideAsset.name || guideAsset.path.split(/[/\\]/).pop() || "");
                return;
            }
            this._drawGuideToViewport(guide);
            return;
        }

        // Video clips exist — they take visual priority over guides

        // Single clip: use existing fast path
        if (playableClips.length === 1) {
            const clip = playableClips[0];
            const sourceFrame = this.playhead - clip.timeline_start_frame + (clip.source_in_frame || 0);
            const sourceTime = (sourceFrame + 0.5) / this._effectiveFps;
            this._viewportClipOpacity = clip.opacity ?? 1.0;

            const video = this._getOrCreateVideo(clip.source_path);
            if (!video) return;

            if (video.readyState >= 2) {
                if (this.isPlaying) {
                    this._drawVideoToCanvas(video);
                } else {
                    this._seekVideoAndDraw(video, sourceTime);
                }
            } else {
                ctx.fillStyle = COLORS.bg;
                ctx.fillRect(0, 0, w, h);
                ctx.fillStyle = COLORS.textDim;
                ctx.font = this._canvasSansFont(14, 500);
                ctx.textAlign = "center";
                ctx.textBaseline = "middle";
                ctx.fillText("Loading video...", w / 2, h / 2);

                const onReady = () => {
                    video.removeEventListener("canplay", onReady);
                    video.removeEventListener("loadeddata", onReady);
                    this._renderViewportFrame();
                };
                video.addEventListener("canplay", onReady);
                video.addEventListener("loadeddata", onReady);
            }
            return;
        }

        // Multi-layer compositing: multiple clips at this frame
        if (this.isPlaying) {
            // During playback — draw all layers using current video frame positions
            ctx.fillStyle = COLORS.bg;
            ctx.fillRect(0, 0, w, h);
            for (const clip of clips) {
                const video = this._getOrCreateVideo(clip.source_path);
                if (!video || video.readyState < 2) continue;
                const opacity = clip.opacity ?? 1.0;
                if (opacity <= 0) continue;
                this._drawVideoToCanvasRaw(video, ctx, w, h, opacity);
            }
        } else {
            // Scrubbing — seek all videos then composite
            this._renderViewportComposite(this.playhead, playableClips);
        }
    }

    /** Multi-layer composite rendering (scrub mode) */
    async _renderViewportComposite(frame, clips) {
        const ctx = this._vpCtx;
        const w = this._vpCanvas.width;
        const h = this._vpCanvas.height;

        // Cancel any previous seek
        if (this._seekAbort) {
            this._seekAbort();
            this._seekAbort = null;
        }

        let cancelled = false;
        this._seekAbort = () => { cancelled = true; };

        // Pre-seek all videos in parallel
        const seekPromises = clips.map(clip => {
            const video = this._getOrCreateVideo(clip.source_path);
            if (!video || video.readyState < 2) return Promise.resolve(null);
            const sourceFrame = frame - clip.timeline_start_frame + (clip.source_in_frame || 0);
            const targetTime = (sourceFrame + 0.5) / this._effectiveFps;
            if (Math.abs(video.currentTime - targetTime) < 0.02) return Promise.resolve(video);
            return new Promise(resolve => {
                const handler = () => { video.removeEventListener("seeked", handler); resolve(video); };
                video.addEventListener("seeked", handler);
                video.currentTime = targetTime;
            });
        });

        const videos = await Promise.all(seekPromises);
        if (cancelled) return;
        this._seekAbort = null;

        // Clear + draw all layers
        ctx.fillStyle = COLORS.bg;
        ctx.fillRect(0, 0, w, h);

        for (let i = 0; i < clips.length; i++) {
            const video = videos[i];
            if (!video) continue;
            const opacity = clips[i].opacity ?? 1.0;
            if (opacity <= 0) continue;
            this._drawVideoToCanvasRaw(video, ctx, w, h, opacity);
        }
    }

    _seekVideoAndDraw(video, targetTime) {
        // Cancel any previous pending seek
        if (this._seekAbort) {
            this._seekAbort();
            this._seekAbort = null;
        }

        // Skip if already at the right time
        if (Math.abs(video.currentTime - targetTime) < 0.005) {
            this._drawVideoToCanvas(video);
            return;
        }

        // Pause before seeking
        video.pause();

        let cancelled = false;
        this._seekAbort = () => { cancelled = true; };

        const prevTime = video.currentTime;

        // Set up multiple ways to detect seek completion
        const drawOnce = (method) => {
            if (cancelled) return;
            this._seekAbort = null;
            console.log(`[Sonder Scrub] Drew frame via ${method}: target=${targetTime.toFixed(3)}, actual=${video.currentTime.toFixed(3)}, prev=${prevTime.toFixed(3)}, readyState=${video.readyState}, seekable=${video.seekable.length > 0 ? video.seekable.start(0).toFixed(1) + '-' + video.seekable.end(0).toFixed(1) : 'none'}`);
            this._drawVideoToCanvas(video);
        };

        // Method 1: seeked event
        const onSeeked = () => {
            video.removeEventListener("seeked", onSeeked);
            clearTimeout(fallbackTimer);
            drawOnce("seeked");
        };
        video.addEventListener("seeked", onSeeked);

        // Method 2: fallback polling timer (in case seeked event doesn't fire)
        const fallbackTimer = setTimeout(() => {
            video.removeEventListener("seeked", onSeeked);
            console.warn(`[Sonder Scrub] seeked event did not fire in 150ms, using fallback. target=${targetTime.toFixed(3)}, actual=${video.currentTime.toFixed(3)}`);
            drawOnce("fallback");
        }, 150);

        // Initiate seek
        video.currentTime = targetTime;
    }

    _getGuideAtFrame(frame) {
        if (!this.activeScene?.guide_frames) return null;
        if (this._isGuideTrackHidden()) return null;
        // Animatic behavior: find the latest guide at or before this frame (holds until next guide)
        let closest = null;
        let closestIdx = -1;
        for (const g of this.activeScene.guide_frames) {
            if (g.muted) continue;
            const idx = g.frame_index === -1 ? this.totalFrames - 1 : g.frame_index;
            if (idx <= frame && idx > closestIdx) {
                closest = g;
                closestIdx = idx;
            }
        }
        return closest;
    }

    _drawGuideToViewport(guide) {
        if (!this._vpCtx || !this._vpCanvas) return;
        // Find the asset for this guide
        const asset = this._getGuideAsset(guide);
        if (!asset) {
            this._drawMissingViewportPlaceholder("Missing guide", "Guide asset entry not found.");
            return;
        }
        if (asset.missing) {
            this._drawMissingViewportPlaceholder("Missing guide", asset.name || asset.path.split(/[/\\]/).pop() || "");
            return;
        }

        const url = this._buildViewURL(asset.path);
        if (!url) {
            this._drawMissingViewportPlaceholder("Missing guide", asset.name || "");
            return;
        }

        // Use a cached image element
        const cacheKey = `guide_${guide.asset_id}`;
        if (!this._videoCache[cacheKey]) {
            const img = new Image();
            img.crossOrigin = "anonymous";
            img.onload = () => {
                this._videoCache[cacheKey] = img;
                this._drawImageToCanvas(img);
            };
            img.src = url;
            return;
        }
        this._drawImageToCanvas(this._videoCache[cacheKey]);
    }

    _drawImageToCanvas(img) {
        if (!this._vpCtx || !this._vpCanvas) return;
        const ctx = this._vpCtx;
        const cw = this._vpCanvas.width;
        const ch = this._vpCanvas.height;

        const iw = img.naturalWidth || img.width || cw;
        const ih = img.naturalHeight || img.height || ch;
        const iAspect = iw / ih;
        const cAspect = cw / ch;

        let dx, dy, dw, dh;
        if (iAspect > cAspect) {
            dw = cw; dh = cw / iAspect; dx = 0; dy = (ch - dh) / 2;
        } else {
            dh = ch; dw = ch * iAspect; dx = (cw - dw) / 2; dy = 0;
        }

        ctx.fillStyle = COLORS.bg;
        ctx.fillRect(0, 0, cw, ch);
        try { ctx.drawImage(img, dx, dy, dw, dh); } catch (e) {}
    }

    _drawVideoToCanvas(video) {
        if (!this._vpCtx || !this._vpCanvas) return;
        const ctx = this._vpCtx;
        const cw = this._vpCanvas.width;
        const ch = this._vpCanvas.height;

        // Draw video maintaining aspect ratio within canvas
        const vw = video.videoWidth || cw;
        const vh = video.videoHeight || ch;
        const vAspect = vw / vh;
        const cAspect = cw / ch;

        let dx, dy, dw, dh;
        if (vAspect > cAspect) {
            dw = cw;
            dh = cw / vAspect;
            dx = 0;
            dy = (ch - dh) / 2;
        } else {
            dh = ch;
            dw = ch * vAspect;
            dx = (cw - dw) / 2;
            dy = 0;
        }

        ctx.fillStyle = COLORS.bg;
        ctx.fillRect(0, 0, cw, ch);
        const opacity = this._viewportClipOpacity ?? 1.0;
        if (opacity < 1.0) ctx.globalAlpha = opacity;
        try {
            ctx.drawImage(video, dx, dy, dw, dh);
        } catch (e) {
            // Video may not be ready yet
        }
        if (opacity < 1.0) ctx.globalAlpha = 1.0;
    }

    /** Draw video to canvas WITHOUT clearing — for multi-layer compositing */
    _drawVideoToCanvasRaw(video, ctx, cw, ch, opacity) {
        const vw = video.videoWidth || cw;
        const vh = video.videoHeight || ch;
        const vAspect = vw / vh;
        const cAspect = cw / ch;

        let dx, dy, dw, dh;
        if (vAspect > cAspect) {
            dw = cw; dh = cw / vAspect; dx = 0; dy = (ch - dh) / 2;
        } else {
            dh = ch; dw = ch * vAspect; dx = (cw - dw) / 2; dy = 0;
        }

        if (opacity < 1.0) ctx.globalAlpha = opacity;
        try { ctx.drawImage(video, dx, dy, dw, dh); } catch (e) {}
        if (opacity < 1.0) ctx.globalAlpha = 1.0;
    }

    _updateTransportUI() {
        if (this._vpFrameCounter) {
            if (this._timecodeMode === "timecode") {
                this._vpFrameCounter.textContent = `${this._frameToTimecode(this.playhead)} / ${this._frameToTimecode(this.totalFrames)}`;
            } else {
                this._vpFrameCounter.textContent = `Frame ${this.playhead} / ${this.totalFrames}`;
            }
        }
        if (this._vpProgressFill && this.totalFrames > 0) {
            this._vpProgressFill.style.width = `${(this.playhead / this.totalFrames) * 100}%`;
        }
        if (this._vpFrameLabel) {
            this._vpFrameLabel.textContent = this._timecodeMode === "timecode" ? this._frameToTimecode(this.playhead) : `f${this.playhead}`;
        }
    }

    // ── Playback ──────────────────────────────────────────────────────

    _togglePlayback() {
        const viewportSurface = this._ensureViewportSurface();
        if (viewportSurface) {
            viewportSurface.togglePlayback();
            return;
        }
        if (this.isPlaying) {
            this._stopPlayback();
        } else {
            this._startPlayback();
        }
    }

    _clearActivePlaybackMedia() {
        if (this._activePlaybackVideos) {
            for (const video of this._activePlaybackVideos) {
                video.pause();
            }
            this._activePlaybackVideos = [];
        }
        if (this._activePlaybackVideo) {
            this._activePlaybackVideo.pause();
            this._activePlaybackVideo = null;
        }
        for (const audio of this._activePlaybackAudios) {
            audio.pause();
        }
        this._activePlaybackAudios = [];
    }

    _resolvePlaybackLoopRange() {
        if (!this._settings?.playback?.loopSelection) return null;
        if (this.selectionStart < this.selectionEnd) {
            return {
                start: this.selectionStart,
                end: this.selectionEnd,
            };
        }
        return null;
    }

    _startPlaybackMedia(frame) {
        this._clearActivePlaybackMedia();

        const visibleClips = this._getClipsAtFrame(frame)
            .filter((clip) => !this._isMissingSourcePath(clip.source_path));
        this._activePlaybackVideos = [];
        for (const clip of visibleClips) {
            const video = this._getOrCreateVideo(clip.source_path);
            if (!video) continue;
            const sourceFrame = frame - clip.timeline_start_frame + (clip.source_in_frame || 0);
            const sourceTime = (sourceFrame + 0.5) / this._effectiveFps;
            video.muted = true;

            const startVideoPlayback = (element) => {
                const onSeeked = () => {
                    element.removeEventListener("seeked", onSeeked);
                    element.play().catch(() => {});
                };
                if (Math.abs(element.currentTime - sourceTime) > 0.01) {
                    element.addEventListener("seeked", onSeeked);
                    element.currentTime = sourceTime;
                    setTimeout(() => {
                        element.removeEventListener("seeked", onSeeked);
                        element.play().catch(() => {});
                    }, 200);
                } else {
                    element.play().catch(() => {});
                }
            };

            if (video.readyState >= 2) {
                startVideoPlayback(video);
            } else {
                const onReady = () => {
                    video.removeEventListener("canplay", onReady);
                    video.removeEventListener("loadeddata", onReady);
                    startVideoPlayback(video);
                };
                video.addEventListener("canplay", onReady);
                video.addEventListener("loadeddata", onReady);
            }
            this._activePlaybackVideos.push(video);
        }
        this._activePlaybackVideo = this._activePlaybackVideos.length > 0
            ? this._activePlaybackVideos[this._activePlaybackVideos.length - 1]
            : null;

        const audioTracks = this._getAudioTracksAtFrame(frame);
        this._activePlaybackAudios = [];
        for (const track of audioTracks) {
            if (track.muted) continue;
            const audio = this._getOrCreateAudio(track.source_path);
            if (!audio) continue;
            const audioFrame = frame - track.timeline_start_frame + (track.source_in_frame || 0);
            audio.currentTime = audioFrame / this._effectiveFps;
            audio.volume = track.volume ?? 1.0;
            audio.play().catch(() => {});
            this._activePlaybackAudios.push(audio);
        }
    }

    _restartPlaybackLoop(timestamp) {
        if (!this._playbackLoopRange) return;
        this.playhead = this._playbackLoopRange.start;
        this._playbackStartTime = timestamp;
        this._playbackStartFrame = this.playhead;
        this._maybeAutoScrollToPlayhead();
        this._startPlaybackMedia(this.playhead);
        this._renderViewportFrame();
        this._renderTimeline();
        this._playbackRAF = requestAnimationFrame((nextTimestamp) => this._playbackTick(nextTimestamp));
    }

    _startPlayback() {
        const viewportSurface = this._ensureViewportSurface();
        if (viewportSurface) {
            viewportSurface.startPlayback();
            return;
        }
        if (this.isPlaying) return;
        const loopRange = this._resolvePlaybackLoopRange();
        if (loopRange && (this.playhead < loopRange.start || this.playhead >= loopRange.end)) {
            this.playhead = loopRange.start;
        }
        this.isPlaying = true;
        if (this._vpPlayBtn) this._vpPlayBtn.textContent = "Pause";

        this._playbackStartTime = performance.now();
        this._playbackStartFrame = this.playhead;
        this._playbackSessionStartFrame = this.playhead;
        this._playbackLoopRange = loopRange;

        // Cancel any pending scrub seek
        if (this._seekAbort) {
            this._seekAbort();
            this._seekAbort = null;
        }

        this._maybeAutoScrollToPlayhead();
        this._startPlaybackMedia(this.playhead);
        this._renderTimeline();
        this._renderViewportFrame();
        this._playbackRAF = requestAnimationFrame((t) => this._playbackTick(t));
    }

    _playbackTick(timestamp) {
        if (!this.isPlaying) return;

        const elapsed = (timestamp - this._playbackStartTime) / 1000;
        const newFrame = this._playbackStartFrame + Math.floor(elapsed * this._effectiveFps);
        const loopRange = this._playbackLoopRange;
        const playbackEndFrame = loopRange ? loopRange.end : this.totalFrames;

        if (newFrame >= playbackEndFrame) {
            if (loopRange) {
                this._restartPlaybackLoop(timestamp);
                return;
            }
            this.playhead = this.totalFrames;
            this._stopPlayback();
            return;
        }

        const prevFrame = this.playhead;
        this.playhead = newFrame;
        this._maybeAutoScrollToPlayhead();

        // Detect clip boundary crossing (multi-layer aware)
        const prevClips = this._getClipsAtFrame(prevFrame)
            .filter((clip) => !this._isMissingSourcePath(clip.source_path));
        const currClips = this._getClipsAtFrame(newFrame)
            .filter((clip) => !this._isMissingSourcePath(clip.source_path));
        const prevPaths = new Set(prevClips.map(c => c.source_path));
        const currPaths = new Set(currClips.map(c => c.source_path));

        // Stop videos no longer visible
        if (this._activePlaybackVideos) {
            this._activePlaybackVideos = this._activePlaybackVideos.filter(v => {
                const srcPath = Object.keys(this._videoCache).find(k => this._videoCache[k] === v);
                if (srcPath && !currPaths.has(srcPath)) { v.pause(); return false; }
                return true;
            });
        }

        // Start newly visible clip videos
        for (const clip of currClips) {
            if (!prevPaths.has(clip.source_path)) {
                const video = this._getOrCreateVideo(clip.source_path);
                if (video && video.readyState >= 2) {
                    const sf = newFrame - clip.timeline_start_frame + (clip.source_in_frame || 0);
                    video.currentTime = (sf + 0.5) / this._effectiveFps;
                    video.muted = true;
                    video.play().catch(() => {});
                    if (!this._activePlaybackVideos) this._activePlaybackVideos = [];
                    this._activePlaybackVideos.push(video);
                }
            }
        }
        this._activePlaybackVideo = this._activePlaybackVideos?.length > 0 ? this._activePlaybackVideos[this._activePlaybackVideos.length - 1] : null;

        // Draw current frame
        this._renderViewportFrame();
        this._renderTimeline();

        this._playbackRAF = requestAnimationFrame((t) => this._playbackTick(t));
    }

    _stopPlayback({ preservePlayhead = false } = {}) {
        const viewportSurface = this._viewportSurface;
        if (viewportSurface) {
            viewportSurface.stopPlayback({ preservePlayhead });
            return;
        }
        if (this._playbackRAF) {
            cancelAnimationFrame(this._playbackRAF);
            this._playbackRAF = null;
        }
        this.isPlaying = false;
        if (this._vpPlayBtn) this._vpPlayBtn.textContent = "Play";

        this._clearActivePlaybackMedia();
        if (!preservePlayhead && this._settings?.playback?.returnToPlaybackStart) {
            this.playhead = this._playbackSessionStartFrame;
            this._maybeAutoScrollToPlayhead();
        }
        this._playbackLoopRange = null;
        this._renderViewportFrame();
        this._renderTimeline();
    }

    getElement() {
        return this.container;
    }

    getHeight() {
        if (this.isFullscreen) return 60;
        const st = this._scaleToolbar;
        const sg = this._scaleGallery;
        const barsH = (SCENE_BAR_HEIGHT + 24) * st; // scene bar + toolbar
        const timelineH = this._timelineHeight;
        const editorsH = ((this._promptEditorEl ? 30 : 0) + (this._itemEditorEl ? 30 : 0)) * st;
        return barsH + timelineH + (this._galleryHeight * sg) + editorsH;
    }

    // ── File Import (drag-and-drop files from OS onto node) ────────────
    _readDroppedDirectoryFiles(dirEntry) {
        return new Promise((resolve, reject) => {
            const reader = dirEntry.createReader();
            const entries = [];

            const readNext = () => {
                reader.readEntries((batch) => {
                    if (!batch.length) {
                        Promise.all(entries
                            .filter((entry) => entry.isFile)
                            .map((entry) => new Promise((fileResolve) => {
                                entry.file((file) => fileResolve(file), () => fileResolve(null));
                            })))
                            .then((files) => resolve(files.filter(Boolean)))
                            .catch(reject);
                        return;
                    }
                    entries.push(...batch);
                    readNext();
                }, reject);
            };

            readNext();
        });
    }

    async _importDroppedDirectory(dirEntry) {
        const files = await this._readDroppedDirectoryFiles(dirEntry);
        for (const file of files) {
            await this._importFile(file, dirEntry.name || "");
        }
    }

    async _importFile(file, folder = "") {
        if (!this.projectDir) return;
        try {
            if (await importFileIntoProject(this.projectDir, file, folder)) {
                console.log("[Sonder] Imported:", file.name);
                await this._fetchAssets();
            }
        } catch (e) {
            console.warn("[Sonder] File import error:", e);
        }
    }

    _showSettingsPanel() {
        if (this._settingsPanelHandle) return;
        const host = this._createSettingsPanelHost();
        const handle = mountEditorSettingsPanel(host);
        this._settingsPanelHost = host;
        this._settingsPanelHandle = handle;
        this._settingsPanelEl = handle.element;
        this._settingsPanelControls = handle.controls;
        this._renderModelTemplateSettings = handle.renderModelTemplateSettings;
        this._settingsPanelKeyOff = handle.unregisterKeyboard || null;
    }

    _hideSettingsPanel() {
        const hadHandle = !!this._settingsPanelHandle;
        if (this._settingsPanelHandle) {
            this._settingsPanelHandle.cleanup();
        } else if (this._settingsPanelEl) {
            this._settingsPanelEl.remove();
        }
        if (!hadHandle && this._settingsPanelKeyOff) {
            this._settingsPanelKeyOff();
        }
        this._settingsPanelHost = null;
        this._settingsPanelHandle = null;
        this._settingsPanelEl = null;
        this._settingsPanelControls = null;
        this._renderModelTemplateSettings = null;
        this._settingsPanelKeyOff = null;
    }

    destroy() {
        if (this._destroyed) return;
        this._destroyed = true;

        this._stopPlayback();
        if (this._seekAbort) {
            this._seekAbort();
            this._seekAbort = null;
        }

        if (this._containerResizeObserver) {
            this._containerResizeObserver.disconnect();
            this._containerResizeObserver = null;
        }
        if (this._vpResizeObserver) {
            this._vpResizeObserver.disconnect();
            this._vpResizeObserver = null;
        }
        if (this._windowResizeHandler) {
            window.removeEventListener("resize", this._windowResizeHandler);
            this._windowResizeHandler = null;
        }
        // Unregister keyboard consumers BEFORE the rest of teardown so the
        // KeyboardOwnership refcount can detach its window listeners cleanly.
        if (this._editorKeyOff) { this._editorKeyOff(); this._editorKeyOff = null; }
        this._editorKeyConsumer = null;
        if (this._shortcutOverlayKeyOff) { this._shortcutOverlayKeyOff(); this._shortcutOverlayKeyOff = null; }
        this._hideSettingsPanel();
        if (this._exportPanelKeyOff) { this._exportPanelKeyOff(); this._exportPanelKeyOff = null; }
        if (this._contextMenuKeyOff) { this._contextMenuKeyOff(); this._contextMenuKeyOff = null; }
        this._hideGuideManagementPopup();
        this._hideGuideHoverPreview();
        if (this._contextMenuMouseOff) { this._contextMenuMouseOff(); this._contextMenuMouseOff = null; }
        if (this._focusHandler) {
            document.removeEventListener("mousedown", this._focusHandler, true);
            this._focusHandler = null;
        }
        if (this._settingsUnsubscribe) {
            this._settingsUnsubscribe();
            this._settingsUnsubscribe = null;
        }

        if (this._previewEl) {
            for (const media of this._previewEl.querySelectorAll("video, audio")) {
                media.pause?.();
                media.removeAttribute?.("src");
                media.load?.();
            }
            this._previewEl.remove();
            this._previewEl = null;
        }
        if (this._savedSelDropdown) {
            this._savedSelDropdown.remove();
            this._savedSelDropdown = null;
        }
        if (this._contextMenuEl) {
            this._contextMenuEl.remove();
            this._contextMenuEl = null;
        }
        if (this._shortcutOverlayEl) {
            this._shortcutOverlayEl.remove();
            this._shortcutOverlayEl = null;
        }
        if (this._exportPanelHandle || this._exportPanelEl || this._exportJobId || this._exportPollTimer || this._exportStartPending) {
            this._hideExportPanel();
        }
        if (this._toastTimer) {
            window.clearTimeout(this._toastTimer);
            this._toastTimer = null;
        }
        if (this._toastEl) {
            this._toastEl.remove();
            this._toastEl = null;
        }
        if (this._fullscreenPlaceholder) {
            this._fullscreenPlaceholder.remove();
            this._fullscreenPlaceholder = null;
        }
        if (this._assetGallery) {
            this._assetGallery.destroy();
            this._assetGallery = null;
        }

        if (this._viewportSurface) {
            this._viewportSurface.destroy();
            this._viewportSurface = null;
        }
        this._clearVideoCache();

        if (this._fullscreenOverlay) {
            this._fullscreenOverlay.remove();
            this._fullscreenOverlay = null;
        }
        if (this.container?.parentElement) {
            this.container.remove();
        }

        this.isFullscreen = false;
        if (EditorWidget._activeFullscreen === this) {
            EditorWidget._activeFullscreen = null;
        }
    }
}

// Module-level guard: only one editor can be fullscreen at a time
EditorWidget._activeFullscreen = null;
