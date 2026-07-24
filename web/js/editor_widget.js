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

if (typeof window !== "undefined" && !window.SONDER_DEBUG_PLAYBACK_BOUNDARY) {
    try {
        if (window.localStorage?.getItem?.("SONDER_DEBUG_PLAYBACK_BOUNDARY") === "1") {
            window.SONDER_DEBUG_PLAYBACK_BOUNDARY = true;
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
let _sessionDiagCaptureSeq = 0;

function rotateSessionDiagCaptureId() {
    if (typeof window === "undefined") return "";
    _sessionDiagCaptureSeq += 1;
    const captureId = `${Date.now().toString(36)}-${_sessionDiagCaptureSeq.toString(36)}`;
    window.__SONDER_DIAG_CAPTURE_ID = captureId;
    return captureId;
}

function currentSessionDiagCaptureId() {
    if (typeof window === "undefined") return "";
    return window.__SONDER_DIAG_CAPTURE_ID || rotateSessionDiagCaptureId();
}

function isSessionDiagEnabled() {
    return typeof window !== "undefined" && window.SONDER_DEBUG_SESSION === true;
}

// Console command (`window.SonderClearDiag()`): wipe every in-memory diagnostic
// ring / telemetry set on this page WITHOUT a reload, so the next
// Ctrl+Alt+Shift+D bundle — and the dedup-gated playback decision logs — start
// clean between test runs. Subsystems that own their own rings (viewport
// surfaces, dormant controllers) self-register a reset fn in the shared
// `window.__SONDER_DIAG_CLEARERS` set, mirroring how they share the
// `__SONDER_CANVAS_DIAG` surface. Always exposed (harmless when diag is off).
function getDiagClearerRegistry() {
    if (typeof window === "undefined") return null;
    if (!(window.__SONDER_DIAG_CLEARERS instanceof Set)) {
        window.__SONDER_DIAG_CLEARERS = new Set();
    }
    return window.__SONDER_DIAG_CLEARERS;
}

function clearSessionDiagnostics() {
    let sources = 0;
    const captureId = rotateSessionDiagCaptureId();
    const surface = typeof window !== "undefined" ? window.__SONDER_CANVAS_DIAG : null;
    if (surface && Array.isArray(surface.events)) {
        const boot = {
            kind: "boot",
            t_wall: Date.now(),
            t_mono: performance.now(),
            build_marker: "canvas_page",
            href: typeof location !== "undefined" ? String(location.href || "") : "",
            cleared: true,
            capture_id: captureId,
        };
        // Mutate in place so the surface.record closure keeps the same array.
        surface.events.length = 0;
        surface.events.push(boot);
        surface.boot = boot;
        sources += 1;
    }
    _sessionDiagInFlightMarkerId = "";
    _sessionDiagInFlightKind = "";
    _sessionDiagLastRafTs = 0;
    const registry = getDiagClearerRegistry();
    if (registry) {
        for (const clear of registry) {
            try { clear(); sources += 1; } catch (_) {}
        }
    }
    console.info(`[Sonder Session Diag] Cleared ${sources} diagnostic source(s) without reload.`);
    return sources;
}

if (typeof window !== "undefined") {
    window.SonderClearDiag = clearSessionDiagnostics;
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
        capture_id: currentSessionDiagCaptureId(),
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
                capture_id: currentSessionDiagCaptureId(),
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

import { INSPECT_OVERLAY_SHORTCUTS, mountSharedAssetGallery, getActiveDragAsset } from "./shared_asset_gallery.js";
import { deriveCurrentSceneAssetIds } from "./current_scene_assets.js";
import { notifyInfo, notifySuccess, notifyWarning, notifyError, notifyProgress } from "./editor_notifications.js";
import { normalizeChannels, composeSectionText, composeSectionsDisplayText } from "./prompt_composition.js";
import { mountSharedRenderQueue, queueBatchIds, formatQueueTime } from "./shared_render_queue.js";
import { mountEditorSettingsPanel } from "./editor_settings_panel.js";
import {
    cancelBulkThumbnailRepair,
    cancelThumbnailRepairOwner,
    fetchMissingThumbnailAssets,
    getBulkThumbnailRepairState,
    startBulkThumbnailRepair,
    subscribeThumbnailRepairState,
} from "./thumbnail_repair_manager.js";
import { mountTimelineExportPanel } from "./editor_timeline_export_panel.js";
import { mountPromptManagementPanel } from "./editor_prompt_panel.js";
import { evalNumericExpression } from "./editor_numeric_input.js";
import * as TimelineCanvas from "./editor_timeline_canvas.js";
import { RULER_HEIGHT, TIMELINE_HEIGHT, TRACK_TYPE } from "./editor_timeline_constants.js";
import { createViewportSurface } from "./viewport_surface.js";
import {
    EDITOR_COLORS as COLORS,
    FONT,
    LANE_PALETTE,
    applyNativeControlTheme,
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
    CROP_POSITION_OPTIONS,
    CUSTOM_OUTPUT_KIND_VIDEO,
    DEFAULT_EDITOR_SETTINGS,
    FIT_MODE_OPTIONS,
    RESOLUTION_TIERS,
    VALID_CROP_POSITIONS,
    VALID_FIT_MODES,
    computeResolutionFromTier,
    detectResolutionPresetSelections,
    frameConstraintsEqual,
    getEditorSettings,
    getAllModelTemplates,
    getTemplateById,
    getDimensionConstraint,
    getTemplateFpsValues,
    snapFpsToAllowed,
    templateFpsIsFixed,
    getRecommendedDurationSec,
    getRecommendedResolutions,
    getMaxRes,
    resolveBatchChunkSizes,
    resolveFrameConstraintForTemplate,
    resolutionToolbarSelectionMemory,
    snapResolution,
    snapToConstraint,
    subscribeEditorSettings,
    updateEditorSettings,
} from "./editor_settings.js";
import {
    createStaleReplayGovernor,
    getProjectVersion,
    postProjectJsonWithReconcile,
    resetProjectVersion,
} from "./api_client.js";
import { ProjectMutationQueue } from "./project_mutation_queue.js";
import {
    findConstrainedSelectionEndpoint,
    isSelectionDurationWithinRecommendation,
    resolveSelectionExecutionWindow,
} from "./selection_constraints.js";

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

async function readResponseError(resp, fallback = "Request failed.") {
    try {
        const payload = await resp.clone().json();
        if (payload?.error) return String(payload.error);
        if (payload?.message) return String(payload.message);
    } catch {
        // Fall through to text response.
    }
    try {
        const text = await resp.text();
        if (text) return text;
    } catch {
        // Ignore parse failures and use fallback.
    }
    return fallback;
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
    const dirName = projectDir.split(/[/\\]/).pop();
    const formData = new FormData();
    formData.append("file", file, file.name);
    if (folder) formData.append("folder", folder);
    const importResp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/import`), {
        method: "POST",
        body: formData,
    });

    if (!importResp.ok) {
        const message = await readResponseError(importResp, `Import failed: ${importResp.status}`);
        throw new Error(message);
    }

    return true;
}

export async function replaceAssetInProject(projectDir, assetId, file) {
    if (!projectDir || !assetId || !file) return null;
    const dirName = projectDir.split(/[/\\]/).pop();
    const formData = new FormData();
    formData.append("file", file, file.name);
    const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/assets/${assetId}/replace`), {
        method: "POST",
        body: formData,
    });
    if (!resp.ok) {
        throw new Error(await readResponseError(resp, `Asset replace failed: ${resp.status}`));
    }
    return await resp.json();
}

// ── Constants ──────────────────────────────────────────────────────────
const GALLERY_HEIGHT = 160;
const SCENE_BAR_HEIGHT = 36;
const FULLSCREEN_SIDEBAR_DEFAULT_WIDTH = 240; // fallback only; first-run width is computed proportionally
const FULLSCREEN_SIDEBAR_MIN_WIDTH = 180;
const FULLSCREEN_SIDEBAR_DEFAULT_FRACTION = 0.382; // first-run gallery sidebar ≈ golden-ratio 38.2% of the editor area
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
        this._renderCacheUsage = null;
        this._renderCacheSweepPending = false;
        this._renderCacheSweepSeq = 0;
        this._renderCacheStatusHandler = null;
        this._thumbnailRepairOwnerId = this._keyboardConsumerId("thumbnail-bulk");
        this._thumbnailRepairPreflight = false;
        this._thumbnailRepairUnsubscribe = subscribeThumbnailRepairState(() => this._syncSettingsPanelControls());

        // Scene state
        this.scenes = [];
        this.activeSceneId = "";
        this.activeScene = null;

        // Timeline state
        this.totalFrames = this._settings.projectDefaults.newSceneDuration;
        this.selectionStart = 0;
        this.selectionEnd = 0;
        // UI-only first endpoint for the two-stage manual In/Out workflow.
        // A draft never enters workflow widgets, browser selection memory,
        // saved selections, or queue snapshots.
        this._selectionDraftAnchor = null;
        // Prompt-usage highlight: authored start_frames the live selection
        // window will output (used) vs dropped by the boundary threshold.
        this._promptUsedSections = new Set();
        this._promptDroppedSections = new Set();
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
        this._sceneMutationInvalidationSeq = 0;
        this._queueFetchSeq = 0;
        this._assetFetchSeq = 0;
        this._staleReplayGovernors = new Map();
        this._staleReplayTimers = new Map();
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

        // Selected timeline items: array of { type: "clip"|"guide"|"audio"|"prompt", id: string|number, data: object }
        this.selectedItems = [];
        this.selectedItem = null; // Primary (most recently clicked) — used for properties editor
        this._selectedLanes = [];
        this._dragSelectRect = null;
        this._dragSelectBaseItems = [];
        this._dragSelectBaseLanes = [];

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
        this.sceneWidth = DEFAULT_EDITOR_SETTINGS.projectDefaults.width;
        this.sceneHeight = DEFAULT_EDITOR_SETTINGS.projectDefaults.height;
        this._promptChannelLabels = false;
        this._guideCollisionAutoOffset = true;
        this._promptSectionDelimiter = ".";
        this._promptFrameThreshold = 10;
        this._serverSettings = null;
        this._serverSettingsLoaded = false;
        this._activeProjectLinked = false;

        // Animatic toggle state
        this._animaticMode = false;

        // Project template state
        this._templateId = this._settings.projectDefaults.defaultTemplateId || "free";
        this._customAspectRatioValue = "";
        this._templateFormState = { expanded: false, editId: "" };
        this._resolutionEditAxis = "w";
        this._freeAspectTierDraft = { width: false, height: false };
        this._resolutionSelectionPinned = false;

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
        this._playbackWarmState = null;
        this._playbackWarmRenderRAF = null;
        this._playbackWarmSceneSignature = "";
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
        if (typeof api.addEventListener === "function") {
            this._renderCacheStatusHandler = (event) => {
                const remaining = Number(event?.detail?.exec_info?.queue_remaining);
                if (remaining !== 0 || this._destroyed) return;
                if (this._renderCacheSweepPending) this._sweepRenderCache();
                else if (this._settingsPanelHandle) this._refreshRenderCacheUsage();
            };
            api.addEventListener("status", this._renderCacheStatusHandler);
        }

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
            setSize: (size) => {
                if (!Array.isArray(size)) return;
                const controller = node?._sonderController;
                if (typeof controller?.setNodeSizeProgrammatic === "function") {
                    controller.setNodeSizeProgrammatic(size[0], size[1], { adoptHeight: true });
                    return;
                }
                node?.setSize?.(size);
            },
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
        // A draft anchor is intentionally local and ephemeral. Any externally
        // applied widget state supersedes it, even when the update only changes
        // context or another related workflow field.
        this._selectionDraftAnchor = null;
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
        const result = updateEditorToolbar(this);
        this._scheduleFullscreenToolbarLayoutRefresh();
        return result;
    }

    _scheduleFullscreenToolbarLayoutRefresh() {
        if (!this.isFullscreen || !this._fsBottomRow || this._destroyed) return;
        if (this._toolbarLayoutRefreshRaf) return;
        this._toolbarLayoutRefreshRaf = requestAnimationFrame(() => {
            this._toolbarLayoutRefreshRaf = null;
            if (this._destroyed || !this.isFullscreen || !this._fsBottomRow) return;
            const prevPanelH = parseInt(getComputedStyle(this._fsBottomRow).height, 10)
                || this._defaultFullscreenTimelineHeight();
            const prevTimelineH = this._timelineHeight;
            const clamped = this._applyFullscreenTimelineHeight(prevPanelH);
            const nextTimelineH = Math.max(
                clamped.metrics.visibleCanvasMin,
                clamped.height - clamped.metrics.paddingY - clamped.metrics.chromeH
            );
            if (Math.ceil(clamped.height) !== prevPanelH || Math.ceil(nextTimelineH) !== Math.ceil(prevTimelineH)) {
                this._timelineHeight = nextTimelineH;
                this._clampScrollY();
                this._renderTimeline();
            }
        });
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
            getCurrentSceneAssetIds: () => this._currentSceneAssetIdsForGallery(),
            onImportFiles: async (files, folder) => {
                await this._importFilesWithProgress(Array.from(files || []), folder);
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
            onRefresh: async () => {
                // Indeterminate progress only when the refresh is slow (>500ms);
                // a fast refresh stays silent (result is visible inline).
                let handle = null;
                const timer = window.setTimeout(() => {
                    handle = notifyProgress({ verb: "Refreshing", message: "Refreshing assets…", progress: null, source: "refresh" });
                }, 500);
                try {
                    await this._fetchAssets();
                } finally {
                    window.clearTimeout(timer);
                    if (handle) handle.resolve({ tier: "info", message: "Assets refreshed" });
                }
            },
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

    // Legacy entry point — now a thin forwarder to the notification Core
    // (durable_rules.md > Notification System). Existing call sites pass refused/
    // advisory messages, which map to the `warning` tier. The old single-slot
    // bottom-center DOM is gone; the page-level toast stack renders these.
    _showToast(message) {
        if (!message) return;
        notifyWarning(message);
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
        const mutationSeq = this._sceneMutationInvalidationSeq;
        try {
            const dirName = this.projectDir.split(/[/\\]/).pop();
            // Version-aware apply (mutation-integrity F2): capture the known
            // committed version BEFORE the GET — the page fetch patch records
            // each response's own header version into the shared map before
            // this await resumes, so a post-fetch read would compare a stale
            // header against itself and always pass.
            const knownVersion = getProjectVersion(dirName);
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes`));
            if (resp.ok) {
                this._clearProjectNotFound();
                const data = await resp.json();
                if (fetchSeq !== this._sceneFetchSeq) {
                    sessionDiagRecord("scene_refresh_stale", {
                        reason,
                        fetch_seq: fetchSeq,
                        current_seq: this._sceneFetchSeq,
                    });
                    // A newer scenes fetch superseded this one: latest-wins.
                    // Re-deferring a dispatch-order rejection lets overlapping
                    // chains invalidate one another forever at idle.
                    return;
                }
                if (mutationSeq !== this._sceneMutationInvalidationSeq) {
                    sessionDiagRecord("scene_refresh_mutation_invalidated", {
                        reason,
                        mutation_seq: mutationSeq,
                        current_mutation_seq: this._sceneMutationInvalidationSeq,
                    });
                    // Mutations are the only invalidation source that needs a
                    // replay. The mutation queue drain bounds this path and makes
                    // refreshScenes:false writes converge after their in-flight
                    // pre-mutation payload is discarded.
                    this._deferProjectBackedRefresh(["scenes"], "scene_refresh_mutation_replay");
                    return;
                }
                // Commit-order race guard: a GET served by the backend BEFORE
                // our latest mutation committed can resolve after every client
                // gate has reopened. Discard payloads strictly OLDER than the
                // version we already held when dispatching; equal passes, so
                // the post-drain replay always applies (no starvation).
                const headerProjectId = resp.headers.get("X-Sonder-Project-Id") || "";
                const headerVersion = resp.headers.get("X-Sonder-Project-Modified-At") || "";
                const compareVersion = headerProjectId && headerProjectId !== dirName
                    ? (getProjectVersion(headerProjectId) || knownVersion)
                    : knownVersion;
                if (headerVersion && compareVersion && headerVersion < compareVersion) {
                    sessionDiagRecord("scene_refresh_stale_version", {
                        reason,
                        header_version: headerVersion,
                        known_version: compareVersion,
                    });
                    const accepted = this._governStaleVersionReplay(
                        "scenes",
                        dirName,
                        headerVersion,
                        compareVersion,
                        "scene_refresh_stale_version_replay",
                    );
                    if (!accepted) return;
                }
                if (this._shouldDeferSceneRefresh({ ignoreMutationGate })) {
                    this._deferSceneRefresh(reason, { stage: "apply" });
                    return;
                }
                this._markStaleReplayApplied("scenes", dirName);
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
            } else if (resp.status === 404) {
                this._showProjectNotFound();
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
        const previousWarmSignature = this._playbackWarmSceneSignature;
        if (!isSameScene && hasActiveScene) {
            this._persistActiveTimelineSelection();
        }
        const storedSelection = (!isSameScene && !preservePendingFrameSelection)
            ? this._readStoredTimelineSelection(scene)
            : null;

        if (!isSameScene) {
            this._selectionDraftAnchor = null;
            this._animaticMode = false;
            this._stopPlayback();
            // Clear undo/redo on scene switch (snapshots are scene-specific)
            this._undoStack = [];
            this._redoStack = [];
            // Drop stale prompt-usage highlight (sets are keyed by start_frame
            // and would otherwise falsely match the new scene's sections).
            this._promptUsedSections = new Set();
            this._promptDroppedSections = new Set();
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
        const nextWarmSignature = this._buildPlaybackWarmSceneSignature();
        if (!isSameScene || (previousWarmSignature && previousWarmSignature !== nextWarmSignature)) {
            this._clearPlaybackWarmOverlay(isSameScene ? "scene-content-refresh" : "scene-switch", { render: false });
        } else {
            this._playbackWarmSceneSignature = nextWarmSignature;
        }
        this.totalFrames = scene.duration_frames || DEFAULT_EDITOR_SETTINGS.projectDefaults.newSceneDuration;
        this._refreshDurationInput();
        this._updateSceneIdentity(scene.name || "Untitled Scene");

        // Load scene resolution/fps into inputs
        if (!isSameScene) {
            this._resolutionSelectionPinned = false;
        }
        this._syncSceneResolutionControls({ detectSelections: !isSameScene });
        this._syncSceneFpsControl();
        if (this._timecodeMode === "timecode") {
            this._refreshContextInputs();
        }
        this._updateViewportHeader();

        // Update hidden widgets
        this._setWidgetValue("scene_id", scene.scene_id);

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
        this._assetGallery?.refreshCurrentScene?.();
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
        this._markResolutionSelectionPinned();
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
        this._rememberResolutionSelection();
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
        if (this._fpsUpdatePending) {
            this._syncSceneFpsControl();
            return;
        }
        this._fpsUpdatePending = true;
        fps = Math.max(0, Number(fps) || 0);
        const sceneId = this.activeSceneId;
        const oldEffectiveFps = Math.max(0.001, Number(this._effectiveFps) || 24);
        const newEffectiveFps = Math.max(0.001, fps > 0 ? fps : (this.fps || 24));
        const scale = newEffectiveFps / oldEffectiveFps;
        const previousState = {
            playhead: this.playhead,
            selectionStart: this.selectionStart,
            selectionEnd: this.selectionEnd,
            scrollX: this.scrollX,
            dragStartFrame: this._dragStartFrame,
        };
        const reconcileRetimedScene = (result) => {
            const handled = this._reconcileActiveSceneFromMutation(result, { reason: "scene fps" });
            if (!handled || result?.payload?.scene?.scene_id !== sceneId || this.activeSceneId !== sceneId) {
                return handled;
            }
            this.playhead = Math.round(previousState.playhead * scale);
            this.selectionStart = Math.round(previousState.selectionStart * scale);
            this.selectionEnd = Math.round(previousState.selectionEnd * scale);
            this.scrollX = Math.round(previousState.scrollX * scale);
            this._dragStartFrame = Math.round(previousState.dragStartFrame * scale);
            this.totalFrames = this.activeScene?.duration_frames || DEFAULT_EDITOR_SETTINGS.projectDefaults.newSceneDuration;
            this._clampTimelineStateToDuration();
            this._clampScrollX();
            this._syncSceneFpsControl();
            this._updateViewportHeader();
            this._refreshDurationInput();
            this._refreshContextInputs();
            this._refreshSelectionInputs();
            if (this._itemEditorEl && this.selectedItem) this._showItemEditor();
            this._renderTimeline();
            this._renderViewportFrame();
            this._updateToolbar();
            this._updateTransportUI();
            return true;
        };
        this._pushUndo("change fps");
        try {
            await this._runSceneMutation(
                [{ type: "update_scene_fields", fields: { fps } }],
                {
                    key: `scene:${sceneId}:fps`,
                    label: "scene fps",
                    coalesce: false,
                    reconcileFromResult: reconcileRetimedScene,
                    failureTier: (error) => error?.code === "queue_jobs_pending" ? "warning" : "error",
                    failureMessage: (error) => error?.code === "queue_jobs_pending"
                        ? "Scene FPS change refused."
                        : "Scene FPS change failed — timeline restored.",
                    failureDetail: (error) => error?.code === "queue_jobs_pending"
                        ? "This scene has pending or running queue jobs. Finish or clear them before changing FPS so their frozen frame ranges stay valid."
                        : null,
                }
            );
        } catch (e) {
            this._discardLastUndo("change fps");
            console.warn("[Sonder] Failed to update scene FPS:", e);
        } finally {
            this._fpsUpdatePending = false;
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

    async _renameScene(targetScene = null) {
        const scene = targetScene || this.activeScene;
        if (!scene) return;
        const isActive = scene.scene_id === this.activeSceneId;
        const name = prompt("Scene name:", scene.name);
        if (!name || name === scene.name) return;
        // Undo only snapshots the active scene, so pushing it for a non-active rename would
        // strand an entry that reverts unrelated active state. Gate undo on the active path.
        if (isActive) this._pushUndo("rename scene");

        try {
            await this._runSceneMutation(
                [{ type: "update_scene_fields", fields: { name } }],
                {
                    key: `scene:${scene.scene_id}:name`,
                    label: "rename scene",
                    coalesce: true,
                    refreshScenes: false,
                    sceneId: scene.scene_id,
                }
            );
            scene.name = name;
            if (isActive) this._updateSceneIdentity(name);
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

    async _deleteScene(targetScene = null) {
        const scene = targetScene || this.activeScene;
        if (!scene || !this.projectDir) return;
        if (this.scenes.length <= 1) {
            alert("Cannot delete the last scene.");
            return;
        }
        if (!confirm(`Delete scene "${scene.name}"? This cannot be undone.`)) return;

        const dirName = this.projectDir.split(/[/\\]/).pop();
        try {
            await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${scene.scene_id}`), {
                method: "DELETE",
            });
            // _fetchScenes preserves the active scene when it still exists, so deleting a
            // non-active scene keeps the user in place; deleting the active one falls to scene 0.
            await this._fetchScenes();
        } catch (e) {
            console.warn("[Sonder] Failed to delete scene:", e);
        }
    }

    async _duplicateScene(targetScene = null) {
        const scene = targetScene || this.activeScene;
        if (!scene || !this.projectDir) return;
        const isActive = scene.scene_id === this.activeSceneId;
        const dirName = this.projectDir.split(/[/\\]/).pop();

        try {
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${scene.scene_id}/duplicate`), {
                method: "POST",
            });

            if (!resp.ok) return;
            const newScene = await resp.json();
            this.scenes.push(newScene);
            // Duplicating the active scene jumps to the copy (existing flow); duplicating a
            // non-active scene from the switcher leaves the user on their current scene.
            if (isActive) this._setActiveScene(newScene);
        } catch (e) {
            console.warn("[Sonder] Failed to duplicate scene:", e);
        }
    }

    // ── Asset Management ───────────────────────────────────────────────
    _allProjectAssetsForGallery() {
        return Object.values(this.assets || {}).flatMap((entries) => Array.isArray(entries) ? entries : []);
    }

    _currentSceneAssetIdsForGallery() {
        return deriveCurrentSceneAssetIds(this.activeScene, this._allProjectAssetsForGallery());
    }

    async _fetchAssets({ ignoreMutationGate = false, reason = "assets" } = {}) {
        if (!this.projectDir) return;
        if (!ignoreMutationGate && this._hasPendingProjectMutations()) {
            this._deferProjectBackedRefresh(["assets"], reason);
            return;
        }

        // Single-flight parity with _fetchScenes: a newer asset refresh
        // supersedes this one (latest-wins). Incremented after the mutation gate
        // so deferred no-op calls do not churn the counter.
        const fetchSeq = ++this._assetFetchSeq;
        const dirName = this.projectDir.split(/[/\\]/).pop();
        // Capture the committed version BEFORE the POST for the exhausted-conflict
        // governor comparison (the fetch patch records response versions into the
        // shared map before this await resumes).
        const knownVersion = getProjectVersion(dirName);
        try {
            // /assets/sync is a version-gated POST (the fetch patch stamps
            // If-Match); after a generation commit bumps the version it 409s.
            // Reconcile from the 409 body and retry, instead of silently dropping
            // it and stranding new takes as "Missing".
            const { payload: data, attempts } = await postProjectJsonWithReconcile(
                api.apiURL(`/sonder-editor/project/${dirName}/assets/sync?${this._assetListQueryString()}`),
                { method: "POST" },
                { projectId: dirName },
            );
            if (fetchSeq !== this._assetFetchSeq) {
                // A newer asset refresh superseded this one: latest-wins.
                return;
            }
            if (attempts > 1) {
                sessionDiagRecord("assets_refresh_stale_version", {
                    reason,
                    attempts,
                    known_version: knownVersion,
                });
            }
            this._markStaleReplayApplied("assets", dirName);
            this.assets = { video: [], image: [], audio: [], artifact: [] };
            this._pathToAsset = {};
            for (const asset of (data?.assets || [])) {
                if (this.assets[asset.asset_type]) {
                    this.assets[asset.asset_type].push(asset);
                }
                if (asset.path) this._pathToAsset[asset.path] = asset;
            }
            this._assetGallery?.setData({
                assets: data?.assets || [],
                folders: data?.folders || [],
                currentSceneAssetIds: this._currentSceneAssetIdsForGallery(),
            });
            this._clearPlaybackWarmOverlay("assets-refresh");
        } catch (e) {
            if (e?.code === "project_version_conflict") {
                // Immediate heal-and-retry was exhausted (a second commit raced in
                // between heal and retry). Hand to the shared bounded-retry +
                // stable-server breaker; it re-enters via
                // _deferProjectBackedRefresh(["assets"]).
                sessionDiagRecord("assets_refresh_stale_version", {
                    reason,
                    exhausted: true,
                    header_version: String(e.actualModifiedAt || ""),
                    known_version: knownVersion,
                });
                this._governStaleVersionReplay(
                    "assets",
                    dirName,
                    String(e.actualModifiedAt || ""),
                    knownVersion,
                    "asset_refresh_stale_version_replay",
                );
                return;
            }
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
            const marker = document.createElement("span");
            marker.textContent = collapsed ? "▸" : "▾";
            const label = document.createElement("span");
            label.textContent = `📁 ${folderName} (${folders[folderName].length})`;
            folderHeader.replaceChildren(marker, label);
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

    _staleReplayGovernor(surfaceKey) {
        if (!this._staleReplayGovernors.has(surfaceKey)) {
            this._staleReplayGovernors.set(surfaceKey, createStaleReplayGovernor());
        }
        return this._staleReplayGovernors.get(surfaceKey);
    }

    _clearStaleReplayTimer(surfaceKey) {
        const timer = this._staleReplayTimers.get(surfaceKey);
        if (timer !== undefined) {
            clearTimeout(timer);
            this._staleReplayTimers.delete(surfaceKey);
        }
    }

    _clearStaleReplayState() {
        for (const timer of this._staleReplayTimers.values()) {
            clearTimeout(timer);
        }
        this._staleReplayTimers.clear();
        this._staleReplayGovernors.clear();
    }

    _markStaleReplayApplied(surfaceKey, projectId) {
        this._clearStaleReplayTimer(surfaceKey);
        this._staleReplayGovernors.get(surfaceKey)?.reset(projectId);
    }

    _governStaleVersionReplay(surfaceKey, projectId, headerVersion, knownVersion, reason) {
        const decision = this._staleReplayGovernor(surfaceKey).reject(projectId, headerVersion);
        this._clearStaleReplayTimer(surfaceKey);
        if (decision.action === "retry") {
            const expectedProjectDir = this.projectDir;
            const timer = setTimeout(() => {
                if (this._staleReplayTimers.get(surfaceKey) === timer) {
                    this._staleReplayTimers.delete(surfaceKey);
                }
                if (this._destroyed || this.projectDir !== expectedProjectDir) return;
                this._deferProjectBackedRefresh([surfaceKey], reason);
            }, decision.delayMs);
            this._staleReplayTimers.set(surfaceKey, timer);
            return false;
        }

        resetProjectVersion(projectId, headerVersion);
        sessionDiagRecord("stale_version_breaker_tripped", {
            surface: surfaceKey,
            project_id: projectId,
            header_version: headerVersion,
            known_version: knownVersion,
            rejection_count: decision.rejectionCount,
        });
        console.warn(
            `[Sonder] ${surfaceKey} refresh accepted stable older project version after bounded retries; client version state was reset.`,
        );
        return true;
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
        if (this._destroyed) return;
        if (this._hasPendingProjectMutations()) {
            // Flicker-audit fix #3 (2026-06-11): a mutation enqueued between
            // drain-resolve and replay used to strand the deferred keys with
            // _pendingProjectRefreshDrain stuck true, so no future drain waiter
            // was ever scheduled. Re-arm instead; converges when the queue idles.
            this._projectMutationQueue?.drain?.("deferred_replay_rearm").then(() => this._replayDeferredProjectBackedRefresh());
            return;
        }
        const keys = this._pendingProjectRefreshKeys;
        this._pendingProjectRefreshKeys = null;
        this._pendingProjectRefreshDrain = false;
        if (!keys || keys.size === 0) return;
        if (keys.has("project")) {
            this._fetchProjectSettings({ ignoreMutationGate: true });
        }
        const wantsAssets = keys.has("assets");
        const wantsScenes = keys.has("scenes");
        const wantsQueue = keys.has("queue");
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
        if (wantsQueue) {
            this._fetchRenderQueue({ ignoreMutationGate: true, reason: "project_mutation_deferred_replay" });
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
        return postProjectJsonWithReconcile(api.apiURL(path), init, { projectId, retryOnConflict, maxAttempts });
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
        refreshKeysOnError = null,
        failureMessage = null,
        failureDetail = null,
        failureTier = "error",
        invalidateQueueFetch = false,
    }) {
        // Invalidate any in-flight scenes GET when a mutation is enqueued.
        // Mutation invalidation is deliberately separate from fetch dispatch
        // identity: mutations need one post-drain convergence replay, while a
        // fetch superseded by a newer fetch must simply bail latest-wins.
        this._sceneMutationInvalidationSeq += 1;
        if (invalidateQueueFetch) {
            this._queueFetchSeq += 1;
        }
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
                // Notification-rule compliance (mutation-integrity F6): a finally-
                // dropped mutation is silent data loss without this. Conscious
                // omission of onRetry — failure paths auto-resync to authoritative
                // state, so a retry would re-issue stale intent; source-coalesced
                // so a burst of failures yields one counted toast.
                const message = typeof failureMessage === "function"
                    ? failureMessage(error)
                    : (failureMessage || `${label || "Project change"} failed — timeline restored.`);
                const tier = typeof failureTier === "function" ? failureTier(error) : failureTier;
                const detail = typeof failureDetail === "function" ? failureDetail(error) : failureDetail;
                const notify = tier === "warning" ? notifyWarning : notifyError;
                notify(message, { source: "project-mutation-failed", detail: detail || null });
                if (Array.isArray(refreshKeysOnError) && refreshKeysOnError.length) {
                    this._deferProjectBackedRefresh(refreshKeysOnError, `${label || key}_error`);
                } else if (refreshScenes) {
                    this._deferProjectBackedRefresh(["scenes"], `${label || key}_error`);
                }
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
        sceneId = "",
        failureMessage = null,
        failureDetail = null,
        failureTier = "error",
    } = {}) {
        const context = this._snapshotProjectMutationContext();
        if (!context) return Promise.resolve(null);
        // Scene-scoped mutations default to the active scene, but a caller can target another
        // scene by id (e.g. renaming a non-active scene from the switcher list). The route is
        // scene-scoped, so the override only changes which scene the ops apply to.
        const targetSceneId = sceneId || context.sceneId;
        const intent = {
            ...context,
            sceneId: targetSceneId,
            operations: JSON.parse(JSON.stringify(operations || [])),
        };
        return this._queueProjectMutation({
            key: key || `scene:${targetSceneId}:mutation`,
            label,
            coalesce,
            merge,
            intent,
            refreshScenes,
            reconcileFromResult,
            failureMessage,
            failureDetail,
            failureTier,
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

    _mergeQueueMutationIntents(oldIntent, nextIntent) {
        const operations = [...(oldIntent?.operations || []), ...(nextIntent?.operations || [])];
        if (operations.some((op) => op?.type === "clear_all")) {
            return { ...nextIntent, operations: [{ type: "clear_all" }] };
        }
        const merged = [];
        const seenDeletes = new Set();
        let sawClearCompleted = false;
        for (const op of operations) {
            if (!op || typeof op !== "object") continue;
            if (op.type === "delete_job") {
                const jobId = String(op.job_id || "");
                if (!jobId || seenDeletes.has(jobId)) continue;
                seenDeletes.add(jobId);
                merged.push({ type: "delete_job", job_id: jobId });
            } else if (op.type === "clear_completed") {
                sawClearCompleted = true;
            } else {
                merged.push({ ...op });
            }
        }
        if (sawClearCompleted) {
            merged.push({ type: "clear_completed" });
        }
        return { ...nextIntent, operations: merged };
    }

    _runQueueMutation(operations, {
        key = "project:queue",
        label = "render queue",
        coalesce = true,
        merge = (oldIntent, nextIntent) => this._mergeQueueMutationIntents(oldIntent, nextIntent),
    } = {}) {
        const projectId = this._projectDirName();
        if (!projectId) return Promise.resolve(null);
        const intent = {
            projectId,
            operations: JSON.parse(JSON.stringify(operations || [])),
        };
        return this._queueProjectMutation({
            key,
            label,
            coalesce,
            merge,
            intent,
            refreshScenes: false,
            refreshKeysOnError: ["queue"],
            failureMessage: "Render queue change failed — queue restored.",
            invalidateQueueFetch: true,
            run: async (queuedIntent) => {
                const result = await this._runVersionedProjectMutation(
                    `/sonder-editor/project/${encodeURIComponent(queuedIntent.projectId)}/queue/mutations`,
                    {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ operations: queuedIntent.operations }),
                    },
                    { projectId: queuedIntent.projectId }
                );
                const queue = Array.isArray(result?.payload?.queue) ? result.payload.queue : [];
                this._renderQueue = queue;
                this._applyStoredQueueBatchCollapseState();
                this._renderQueuePanel();
                return result;
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
        this._clearPlaybackWarmOverlay("local-scene-mutation", { render: false });
        this._reconcileSelection();
        this._buildTrackLayout();
        this._renderTimeline();
        if (viewport) this._renderViewportFrame();
    }

    _buildPlaybackWarmSceneSignature() {
        const scene = this.activeScene;
        if (!scene) return "";
        const clipParts = (scene.clips || []).map((clip) => [
            clip.clip_id || "",
            clip.source_path || "",
            clip.timeline_start_frame || 0,
            clip.timeline_end_frame || 0,
            clip.source_in_frame || 0,
            clip.source_out_frame || 0,
            clip.track_index || 0,
            clip.role || "render",
            clip.opacity ?? 1,
            clip.muted ? 1 : 0,
        ]);
        const laneParts = {
            video: (scene.video_lane_configs || []).map((entry) => entry?.hidden ? 1 : 0),
            motion: (scene.motion_driver_lane_configs || []).map((entry) => entry?.hidden ? 1 : 0),
            guide: scene.guide_track_config?.hidden ? 1 : 0,
            animatic: this._animaticMode ? 1 : 0,
        };
        const sourcePaths = Array.from(new Set((scene.clips || []).map((clip) => clip.source_path).filter(Boolean))).sort();
        const assetParts = sourcePaths.map((sourcePath) => {
            const asset = this._getAssetForSourcePath(sourcePath);
            return [
                sourcePath,
                asset?.asset_id || "",
                asset?.path || "",
                asset?.asset_type || "",
                asset?.missing ? 1 : 0,
            ];
        });
        return JSON.stringify({
            sceneId: scene.scene_id || "",
            duration: scene.duration_frames || 0,
            fps: this._effectiveFps,
            clips: clipParts,
            lanes: laneParts,
            assets: assetParts,
        });
    }

    _requestPlaybackWarmTimelineRender() {
        if (this._destroyed || !this.timelineCanvas || this._playbackWarmRenderRAF !== null) return;
        this._playbackWarmRenderRAF = requestAnimationFrame(() => {
            this._playbackWarmRenderRAF = null;
            if (this._destroyed) return;
            this._renderTimeline();
        });
    }

    _clearPlaybackWarmOverlay(reason = "clear", { notifySurface = true, render = true } = {}) {
        if (this._playbackWarmRenderRAF !== null) {
            cancelAnimationFrame(this._playbackWarmRenderRAF);
            this._playbackWarmRenderRAF = null;
        }
        const hadState = !!this._playbackWarmState;
        this._playbackWarmState = null;
        this._playbackWarmSceneSignature = this._buildPlaybackWarmSceneSignature();
        if (notifySurface && this._viewportSurface?.clearPlaybackWarmState) {
            this._viewportSurface.clearPlaybackWarmState(reason);
        }
        if (render && hadState && this.timelineCanvas) {
            this._renderTimeline();
        }
    }

    _handlePlaybackWarmStateChange(payload = {}) {
        if (this._destroyed) return;
        const entries = Array.isArray(payload.entries) ? payload.entries : [];
        const sceneSignature = this._buildPlaybackWarmSceneSignature();
        this._playbackWarmSceneSignature = sceneSignature;
        this._playbackWarmState = entries.length
            ? {
                generation: payload.generation || 0,
                entries,
                reason: payload.reason || "",
                sceneSignature,
            }
            : null;
        if (!this.isPlaying) {
            this._requestPlaybackWarmTimelineRender();
        }
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
        if (!this.activeScene || !["video", "motion_driver", "audio"].includes(laneType)) return false;
        laneIndex = parseInt(laneIndex, 10);
        if (!Number.isFinite(laneIndex)) return false;
        const isVideo = laneType === "video";
        const isDriver = laneType === "motion_driver";
        const currentCount = isVideo
            ? Math.max(1, parseInt(this.activeScene.video_lane_count, 10) || 1)
            : isDriver
                ? Math.max(1, parseInt(this.activeScene.motion_driver_lane_count, 10) || 1)
            : Math.max(1, parseInt(this.activeScene.audio_lane_count, 10) || 1);
        if (currentCount <= 1 || laneIndex < 0 || laneIndex >= currentCount) return false;
        const laneItems = isVideo
            ? (this.activeScene.clips || []).filter((clip) => this._isRenderClip(clip) && (clip.track_index || 0) === laneIndex)
            : isDriver
                ? (this.activeScene.clips || []).filter((clip) => this._isMotionDriverClip(clip) && (clip.track_index || 0) === laneIndex)
            : (this.activeScene.audio_tracks || []).filter((track) => (track.lane_index || 0) === laneIndex);
        if (laneItems.length && itemPolicy === "require_empty") return false;
        if (laneItems.length && itemPolicy === "move_items") {
            const nextTarget = targetLane == null ? (laneIndex > 0 ? laneIndex - 1 : 1) : parseInt(targetLane, 10);
            if (!Number.isFinite(nextTarget) || nextTarget < 0 || nextTarget >= currentCount || nextTarget === laneIndex) return false;
            for (const item of laneItems) {
                if (isVideo || isDriver) item.track_index = nextTarget;
                else item.lane_index = nextTarget;
            }
        } else if (laneItems.length && itemPolicy === "delete_items") {
            if (isVideo || isDriver) {
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
        } else if (isDriver) {
            for (const clip of (this.activeScene.clips || [])) {
                if (this._isMotionDriverClip(clip) && (clip.track_index || 0) > laneIndex) {
                    clip.track_index = Math.max(0, (clip.track_index || 0) - 1);
                }
            }
            this.activeScene.motion_driver_lane_count = currentCount - 1;
            this.activeScene.motion_driver_lane_configs = this._trimLocalLaneConfigs(
                this.activeScene.motion_driver_lane_configs || [],
                laneIndex,
                this.activeScene.motion_driver_lane_count,
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
            guide_id: fields.guide_id ?? guideData?.guide_id ?? this._newLocalItemId("guide"),
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
        this._pruneLocalLinkedGroups();
        return moved;
    }

    _applyLocalCreateGuide(fields = {}) {
        if (!this.activeScene) return null;
        const frameIndex = parseInt(fields.frame_index, 10);
        if (!Number.isFinite(frameIndex)) return null;
        const guide = {
            guide_id: fields.guide_id || this._newLocalItemId("guide"),
            frame_index: frameIndex,
            asset_id: fields.asset_id || "",
            source: fields.source || "asset",
            strength: fields.strength ?? this._defaultGuideStrength(),
            muted: !!fields.muted,
            fit_mode: VALID_FIT_MODES.has(fields.fit_mode) ? fields.fit_mode : this._defaultFitMode(),
            crop_position: VALID_CROP_POSITIONS.has(fields.crop_position) ? fields.crop_position : this._defaultCropPosition(),
        };
        this.activeScene.guide_frames = (this.activeScene.guide_frames || [])
            .filter((current) => current.frame_index !== frameIndex);
        this.activeScene.guide_frames.push(guide);
        this.activeScene.guide_frames.sort((a, b) => (a.frame_index || 0) - (b.frame_index || 0));
        this._pruneLocalLinkedGroups();
        return guide;
    }

    _applyLocalPromptCreate(fields = {}) {
        if (!this.activeScene) return null;
        const channels = normalizeChannels(fields.channels, fields.prompt || "");
        const section = {
            prompt_id: fields.prompt_id || this._newLocalItemId("prompt"),
            start_frame: parseInt(fields.start_frame, 10) || 0,
            end_frame: parseInt(fields.end_frame, 10) || 0,
            channels,
            // Label-free composed mirror, matching backend to_dict
            prompt: composeSectionText(channels, false),
            muted: !!fields.muted,
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
        const { channels, prompt, ...rest } = fields || {};
        Object.assign(section, rest);
        if (channels && typeof channels === "object") {
            section.channels = normalizeChannels(channels);
            section.prompt = composeSectionText(section.channels, false);
        } else if (prompt !== undefined) {
            section.channels = normalizeChannels(null, prompt);
            section.prompt = composeSectionText(section.channels, false);
        }
        if (fields.prompt_id && !section.prompt_id) section.prompt_id = fields.prompt_id;
        this.activeScene.prompt_sections.sort((a, b) => (a.start_frame || 0) - (b.start_frame || 0));
        return section;
    }

    _applyLocalPromptDelete(index) {
        if (!this.activeScene || !Array.isArray(this.activeScene.prompt_sections)) return false;
        if (index < 0 || index >= this.activeScene.prompt_sections.length) return false;
        this.activeScene.prompt_sections.splice(index, 1);
        if (this._selectedPromptIdx === index) this._selectedPromptIdx = null;
        this._pruneLocalLinkedGroups();
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
        this._pruneLocalLinkedGroups();
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

    _mediaTimelineFrames(asset) {
        const fps = Number(this._effectiveFps) || 24;
        const duration = Number(asset?.duration_sec);
        if (Number.isFinite(duration) && duration > 0) {
            return Math.max(1, Math.round(duration * fps));
        }
        const frameCount = Math.max(0, parseInt(asset?.frame_count, 10) || 0);
        const sourceFps = Number(asset?.fps);
        if (frameCount > 0 && Number.isFinite(sourceFps) && sourceFps > 0) {
            return Math.max(1, Math.round(frameCount * fps / sourceFps));
        }
        return Math.max(1, frameCount || 1);
    }

    get _effectiveSceneWidth() {
        const sceneWidth = this.activeScene?.width || 0;
        return sceneWidth > 0 ? sceneWidth : (this.sceneWidth || DEFAULT_EDITOR_SETTINGS.projectDefaults.width);
    }

    get _effectiveSceneHeight() {
        const sceneHeight = this.activeScene?.height || 0;
        return sceneHeight > 0 ? sceneHeight : (this.sceneHeight || DEFAULT_EDITOR_SETTINGS.projectDefaults.height);
    }

    _framesToSeconds(frames) { return frames / this._effectiveFps; }
    _secondsToFrames(seconds) { return Math.round(seconds * this._effectiveFps); }
    _formatPositionInput(frame) { return this._timecodeMode === "timecode" ? this._framesToSeconds(frame).toFixed(2) : String(frame); }
    _parsePositionInput(value) {
        const numeric = evalNumericExpression(value);
        if (!Number.isFinite(numeric)) return NaN;
        if (this._timecodeMode === "timecode") {
            return this._secondsToFrames(numeric);
        }
        return numeric;
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
        if (this._fpsInput) {
            this._fpsInput.value = this._formatSceneFpsValue(this.activeScene?.fps);
            this._fpsInput.placeholder = String(this.fps);
        }
        if (this._fpsSelect && this._fpsSelect.style.display !== "none") {
            const snapped = this._snapSceneFpsToTemplate(this._effectiveFps);
            if (Array.from(this._fpsSelect.options).some((option) => option.value === String(snapped))) {
                this._fpsSelect.value = String(snapped);
            }
        }
    }

    _snapSceneFpsToTemplate(fps, template = this._getActiveTemplate()) {
        const numeric = Math.max(0, Number(fps) || 0);
        const values = getTemplateFpsValues(template);
        if (template?.id === "free" || !values.length) {
            return numeric;
        }
        return Number(snapFpsToAllowed(numeric, values).toFixed(3));
    }

    async _applyActiveTemplateFpsConstraint() {
        if (!this.activeScene) return;
        const template = this._getActiveTemplate();
        const values = getTemplateFpsValues(template);
        if (template.id === "free" || !values.length) return;

        const currentFps = this._effectiveFps;
        const nextFps = this._snapSceneFpsToTemplate(currentFps, template);
        const fixedFps = templateFpsIsFixed(template);
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
        const frameConstraint = this._getActiveFrameConstraint();
        if (!frameConstraint) return numeric;
        const durationConstraint = { ...frameConstraint };
        delete durationConstraint.max;
        return Math.max(1, Math.round(snapToConstraint(numeric, durationConstraint)));
    }

    _snapPreContextFrames(value, { direction = "up" } = {}) {
        // Pre context snaps UP to next G value (= step*k + offset, k>=0). 0 stays 0
        // because pre=0 means no pre context — the +1 then lives in the selection
        // length when the second manual endpoint is resolved from its anchor.
        const numeric = Math.max(0, Math.round(Number(value) || 0));
        if (numeric <= 0) return 0;
        const constraint = this._getActiveFrameConstraint();
        const step = constraint?.step;
        if (!step || step <= 1) return numeric;
        const offset = constraint.offset || 0;
        if (numeric <= offset) return offset;
        const k = (numeric - offset) / step;
        const rounded = direction === "down" ? Math.floor(k) : Math.ceil(k);
        return Math.max(0, offset + rounded * step);
    }

    _snapPostContextFrames(value, { direction = "up" } = {}) {
        // Post context snaps UP to next multiple of step. Post never carries the
        // +1 offset (the leading single frame lives at the start of the tensor,
        // not the tail).
        const numeric = Math.max(0, Math.round(Number(value) || 0));
        if (numeric <= 0) return 0;
        const constraint = this._getActiveFrameConstraint();
        const step = constraint?.step;
        if (!step || step <= 1) return numeric;
        const rounded = direction === "down" ? Math.floor(numeric / step) : Math.ceil(numeric / step);
        return Math.max(0, rounded * step);
    }

    _snapMaskOffset(value, cap, { direction = "up" } = {}) {
        // Mask offset snaps UP to the next multiple of step within [0, cap]. When
        // value >= cap, returns cap (the "full mask" option — needed on the pre
        // side because actual_pre = G value isn't itself a multiple of step; on
        // the post side cap is already a multiple of step so this collapses to
        // the same multiples-of-step rule).
        const numeric = Math.max(0, Math.round(Number(value) || 0));
        const capValue = Math.max(0, Math.round(Number(cap) || 0));
        if (capValue <= 0 || numeric <= 0) return 0;
        if (numeric >= capValue) return capValue;
        const constraint = this._getActiveFrameConstraint();
        const step = constraint?.step;
        if (!step || step <= 1) return Math.min(numeric, capValue);
        const rounded = direction === "down" ? Math.floor(numeric / step) : Math.ceil(numeric / step);
        const snapped = Math.max(0, rounded * step);
        return snapped <= capValue ? snapped : capValue;
    }

    _selectionExecutionWindow(start = this.selectionStart, end = this.selectionEnd) {
        const sceneDuration = Math.max(
            0,
            parseInt(this.activeScene?.duration_frames, 10) || this.totalFrames || 0
        );
        return resolveSelectionExecutionWindow({
            sceneDuration,
            selectionStart: start,
            selectionEnd: end,
            preContextFrames: this._contextFrameValue("pre_context_frames"),
            postContextFrames: this._contextFrameValue("post_context_frames"),
            maskPreOffset: this._contextFrameValue("mask_pre_offset"),
            maskPostOffset: this._contextFrameValue("mask_post_offset"),
            frameConstraint: this._getActiveFrameConstraint(),
        });
    }

    _findManualSelectionEndpoint(edge, anchorFrame, candidateFrame, searchDirection) {
        const sceneDuration = Math.max(
            0,
            parseInt(this.activeScene?.duration_frames, 10) || this.totalFrames || 0
        );
        return findConstrainedSelectionEndpoint({
            edge,
            anchorFrame,
            candidateFrame,
            searchDirection,
            sceneDuration,
            preContextFrames: this._contextFrameValue("pre_context_frames"),
            postContextFrames: this._contextFrameValue("post_context_frames"),
            maskPreOffset: this._contextFrameValue("mask_pre_offset"),
            maskPostOffset: this._contextFrameValue("mask_post_offset"),
            frameConstraint: this._getActiveFrameConstraint(),
        });
    }

    _getActiveTemplate() {
        return getTemplateById(this._templateId, this._settings);
    }

    // Derived {step, offset} frame rule for the active template (null = no rule).
    _getActiveFrameConstraint() {
        return resolveFrameConstraintForTemplate(this._templateId, this._settings);
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

    _detectResolutionPresetSelections(width, height, options = {}) {
        return detectResolutionPresetSelections(width, height, this._getActiveTemplate(), options);
    }

    _findMatchingAspectPreset(width, height, tolerance = 0.03) {
        return this._detectResolutionPresetSelections(width, height, { aspectTolerance: tolerance })?.aspectPreset
            || ASPECT_RATIO_PRESETS.find(
                (preset) => preset.a > 0 && preset.b > 0 && this._matchesAspectRatio(width, height, preset.a, preset.b, tolerance)
            )
            || null;
    }

    _findNearestResolutionTier(width, height, tolerance = 0.10) {
        const snappedDetection = this._detectResolutionPresetSelections(width, height, { tierTolerance: tolerance });
        if (snappedDetection?.tier) return snappedDetection.tier;
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
        this._updateResMaxHint();
    }

    // Non-blocking soft warning: the current resolution exceeds the active template's
    // recommended max-res PIXEL BUDGET (total pixels, orientation-agnostic — same
    // total-pixel basis as the recommended-tier calc). maxRes never clamps.
    _updateResMaxHint() {
        if (!this._resMaxHint) return;
        const template = this._getActiveTemplate();
        const maxRes = getMaxRes(template);
        const { width, height } = this._readResolutionInputs();
        if (template.id === "free" || !maxRes || !(width > 0 && height > 0)) {
            this._resMaxHint.style.display = "none";
            return;
        }
        const budget = maxRes[0] * maxRes[1];
        // Small hidden tolerance (5%): a resolution that only overshoots the budget by a
        // single divisibility-snap step (e.g. 720→736 at ÷32, the recommended res itself)
        // must not warn. A genuinely larger tier is far past this.
        if (width * height <= budget * 1.05) {
            this._resMaxHint.style.display = "none";
            return;
        }
        this._resMaxHint.title = `${template.name}: above recommended max ${maxRes[0]}×${maxRes[1]} (${budget.toLocaleString()} px budget). Higher resolutions may exceed the model's tested range.`;
        this._resMaxHint.style.display = "";
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

    _captureResolutionSelectionState() {
        const selectedAspectOption = this._aspectRatioSelect?.selectedOptions?.[0] || null;
        const selectedIsCustomAspect = selectedAspectOption?.dataset?.sonderCustomAspect === "true";
        return {
            aspectValue: this._aspectRatioSelect?.value || "",
            tierValue: this._resTierSelect?.value || "",
            customAspectValue: selectedIsCustomAspect ? (selectedAspectOption.value || "") : "",
            customAspectLabel: selectedIsCustomAspect ? (selectedAspectOption.textContent || "") : "",
        };
    }

    _rememberResolutionSelection() {
        if (!this.projectDir || !this.activeSceneId) return;
        resolutionToolbarSelectionMemory.write(
            this.projectDir,
            this.activeSceneId,
            this._captureResolutionSelectionState()
        );
    }

    _restoreRememberedResolutionSelection() {
        if (!this._aspectRatioSelect || !this._resTierSelect || !this.projectDir || !this.activeSceneId) {
            return false;
        }
        const state = resolutionToolbarSelectionMemory.read(this.projectDir, this.activeSceneId);
        if (!state) return false;

        const aspectValue = String(state.aspectValue || "");
        const customAspectValue = String(state.customAspectValue || "");
        if (customAspectValue) {
            const { a, b } = this._parseAspectRatioValue(customAspectValue);
            if (a > 0 && b > 0) {
                this._ensureCustomAspectRatioOption(a, b, state.customAspectLabel || `Custom ${a}:${b}`);
            }
        } else {
            this._clearCustomAspectRatioOption();
        }

        if (aspectValue && !Array.from(this._aspectRatioSelect.options).some((option) => option.value === aspectValue)) {
            const { a, b } = this._parseAspectRatioValue(aspectValue);
            if (a > 0 && b > 0) {
                this._ensureCustomAspectRatioOption(a, b, state.customAspectLabel || `Custom ${a}:${b}`);
            }
        }
        if (aspectValue && Array.from(this._aspectRatioSelect.options).some((option) => option.value === aspectValue)) {
            this._aspectRatioSelect.value = aspectValue;
        }

        const tierValue = String(state.tierValue || "custom");
        this._resTierSelect.value = Array.from(this._resTierSelect.options).some((option) => option.value === tierValue)
            ? tierValue
            : "custom";
        this._resolutionSelectionPinned = true;
        return true;
    }

    _markResolutionSelectionPinned() {
        this._resolutionSelectionPinned = true;
        this._rememberResolutionSelection();
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
                height = Math.round(snapToConstraint(height, getDimensionConstraint(template)));
            }
            width = Math.max(1, Math.round(height * a / b));
            if (template.id !== "free") {
                width = Math.round(snapToConstraint(width, getDimensionConstraint(template)));
            }
        } else if (width > 0) {
            if (template.id !== "free") {
                width = Math.round(snapToConstraint(width, getDimensionConstraint(template)));
            }
            height = Math.max(1, Math.round(width * b / a));
            if (template.id !== "free") {
                height = Math.round(snapToConstraint(height, getDimensionConstraint(template)));
            }
        } else if (height > 0) {
            if (template.id !== "free") {
                height = Math.round(snapToConstraint(height, getDimensionConstraint(template)));
            }
            width = Math.max(1, Math.round(height * a / b));
            if (template.id !== "free") {
                width = Math.round(snapToConstraint(width, getDimensionConstraint(template)));
            }
        } else {
            return null;
        }
        return template.id === "free" ? { width, height } : snapResolution(width, height, template);
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

    // Resolution tiers offered for a template: the global RESOLUTION_TIERS plus a
    // template-specific option for any recommended resolution that NO standard tier
    // approximates (within 5% on the total-pixel / geometric-mean basis). This is how
    // an unconventional native res (e.g. CogVideoX 1360×768) becomes directly selectable.
    _templateResolutionTiers(template) {
        const TOL = 0.05;
        const tiers = RESOLUTION_TIERS.map((tier) => ({ value: String(tier.c), label: tier.label, c: tier.c }));
        for (const [w, h] of getRecommendedResolutions(template)) {
            const c = Math.sqrt(w * h);
            if (RESOLUTION_TIERS.some((tier) => Math.abs(tier.c - c) / tier.c <= TOL)) continue;
            const value = String(Math.round(c));
            if (tiers.some((tier) => tier.value === value)) continue;
            tiers.push({ value, label: `${w}×${h}`, c });
        }
        return tiers;
    }

    // Tier value (a number matching a tier's option value) to mark with the ★. Stars
    // the exact template-specific tier when the primary recommended res has no near
    // standard tier; otherwise the nearest standard tier.
    _recommendedTierValue(template) {
        const recommended = getRecommendedResolutions(template);
        if (!recommended.length) return null;
        const [w, h] = recommended[0];
        const c = Math.sqrt(w * h);
        const TOL = 0.05;
        let nearest = null;
        let nearestDiff = Infinity;
        for (const tier of RESOLUTION_TIERS) {
            const diff = Math.abs(tier.c - c);
            if (diff < nearestDiff) {
                nearest = tier.c;
                nearestDiff = diff;
            }
        }
        if (nearest != null && nearestDiff / nearest <= TOL) return nearest;
        return Math.round(c);
    }

    _rebuildResolutionTierOptions(selectedValue = this._resTierSelect?.value || "custom") {
        if (!this._resTierSelect) return;
        const template = this._getActiveTemplate();
        const tiers = this._templateResolutionTiers(template);
        const recommendedValue = this._recommendedTierValue(template);
        const recommendedStr = recommendedValue != null ? String(recommendedValue) : null;
        this._resTierSelect.innerHTML = "";
        for (const tier of tiers) {
            const option = document.createElement("option");
            option.value = tier.value;
            option.textContent = tier.label + (recommendedStr === tier.value ? " ★" : "");
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
        const dimensionConstraint = getDimensionConstraint(template);
        const frameConstraint = this._getActiveFrameConstraint();
        // minDimension / maxRes are SOFT: minDimension is an advisory input floor,
        // and there is no hard max clamp (maxRes only drives warnings). The input
        // `max` stays a high sanity bound, not the template ceiling.
        const minDimension = template?.soft?.minDimension;
        if (this._resWInput) {
            this._resWInput.step = String(dimensionConstraint?.step || 1);
            this._resWInput.min = String(minDimension ?? 0);
            this._resWInput.max = "8192";
        }
        if (this._resHInput) {
            this._resHInput.step = String(dimensionConstraint?.step || 1);
            this._resHInput.min = String(minDimension ?? 0);
            this._resHInput.max = "8192";
        }
        if (this.durationInput) {
            this.durationInput.step = String(frameConstraint?.step || 1);
            this.durationInput.min = "1";
        }
        if (this._fpsInput) {
            this._fpsInput.min = "0";
            this._fpsInput.max = "240";
            this._fpsInput.step = "0.001";
        }
        this._rebuildFpsControl();
        this._updateResMaxHint();
    }

    // fps is a HARD allow-list: constrained templates expose a discrete <select>
    // (single value = effectively locked); `free` keeps the free-entry number input.
    _rebuildFpsControl() {
        const template = this._getActiveTemplate();
        const values = getTemplateFpsValues(template);
        const constrained = template.id !== "free" && values.length > 0;
        if (this._fpsSelect) {
            this._fpsSelect.style.display = constrained ? "" : "none";
            if (constrained) {
                const current = this._snapSceneFpsToTemplate(this._effectiveFps, template);
                this._fpsSelect.innerHTML = "";
                for (const value of values) {
                    const option = document.createElement("option");
                    option.value = String(value);
                    option.textContent = `${value} fps`;
                    this._fpsSelect.appendChild(option);
                }
                this._fpsSelect.value = String(current);
            }
        }
        if (this._fpsInput) {
            this._fpsInput.style.display = constrained ? "none" : "";
        }
    }

    _onTemplateFpsSelected() {
        const value = Number(this._fpsSelect?.value);
        if (!Number.isFinite(value) || value <= 0) return;
        this._updateSceneFps(value);
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
        const previousFrameConstraint = this._getActiveFrameConstraint();
        if (this._selectionDraftAnchor) {
            this._clearTimelineSelection();
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
        // When the frame RULE actually changed (e.g. LTX 8n+1 → Cog 16n+1), the
        // existing selection sits on the old grid — clear it rather than silently
        // re-snapping endpoints the user placed. Same-rule switches (wan → hunyuan,
        // both 4n+1) keep the selection. Context/mask widgets re-snap onto the new
        // grid via _refreshContextInputs (its documented snap-on-reconcile pass);
        // the backend re-snap at execute stays authoritative either way.
        if (!frameConstraintsEqual(previousFrameConstraint, this._getActiveFrameConstraint())) {
            if (this.selectionEnd > this.selectionStart) {
                this._clearTimelineSelection();
            }
            this._refreshContextInputs();
        }
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
        this._markResolutionSelectionPinned();
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
            this._resWInput.placeholder = String(this.sceneWidth || DEFAULT_EDITOR_SETTINGS.projectDefaults.width);
        }
        if (this._resHInput) {
            this._resHInput.value = height || "";
            this._resHInput.placeholder = String(this.sceneHeight || DEFAULT_EDITOR_SETTINGS.projectDefaults.height);
        }
        this._rebuildTemplateOptions();
        this._rebuildResolutionTierOptions();
        this._applyTemplateConstraintMetadata();
        if (detectSelections && !this._resolutionSelectionPinned) {
            if (!this._restoreRememberedResolutionSelection()) {
                const detectedSelections = this._detectResolutionPresetSelections(width, height);
                this._selectAspectRatioForDimensions(width, height, { preferExistingCustom: true });
                if (this._resTierSelect) {
                    this._resTierSelect.value = detectedSelections?.tierValue || "custom";
                }
            }
        }
        this._updateResolutionInputMode();
    }

    _contextFrameValue(name) {
        return Math.max(0, parseInt(this._getWidgetValue(name, 0), 10) || 0);
    }

    _formatContextInputValue(frames) {
        return this._formatPositionInput(Math.max(0, parseInt(frames, 10) || 0));
    }

    _parseContextInputValue(value) {
        const parsed = this._parsePositionInput(value);
        return Number.isFinite(parsed) ? Math.max(0, Math.round(parsed)) : NaN;
    }

    _syncContextInputElement(input, frames, title) {
        if (!input) return;
        input.type = "text";
        input.inputMode = "decimal";
        if (this._timecodeMode === "timecode") {
            input.step = "0.01";
            input.title = `${title} (${Math.max(0, parseInt(frames, 10) || 0)} frames)`;
        } else {
            input.step = "1";
            input.title = title;
        }
        input.min = 0;
        input.max = this._timecodeMode === "timecode" ? this._framesToSeconds(256).toFixed(2) : 256;
        input.value = this._formatContextInputValue(frames);
    }

    _refreshContextInputs() {
        this._syncContextInputElement(
            this._preContextInput,
            this._contextFrameValue("pre_context_frames"),
            "Frames to include before the selected generation range"
        );
        this._syncContextInputElement(
            this._postContextInput,
            this._contextFrameValue("post_context_frames"),
            "Frames to include after the selected generation range"
        );
        this._syncContextInputElement(
            this._maskPreOffsetInput,
            this._contextFrameValue("mask_pre_offset"),
            "Extra pre-context frames excluded from denoise mask start"
        );
        this._syncContextInputElement(
            this._maskPostOffsetInput,
            this._contextFrameValue("mask_post_offset"),
            "Extra post-context frames included in denoise mask end"
        );
        // Re-snap once after external refresh so old projects whose stored values are
        // off-grid under the new policy reconcile display and persisted state on the
        // first paint. _updateContextFrameWidgets handles toolbar update too.
        this._updateContextFrameWidgets();
    }

    _updateContextFrameWidgets(stepDirective = null) {
        const preRaw = this._parseContextInputValue(this._preContextInput?.value);
        const postRaw = this._parseContextInputValue(this._postContextInput?.value);
        const maskPreRaw = this._parseContextInputValue(this._maskPreOffsetInput?.value);
        const maskPostRaw = this._parseContextInputValue(this._maskPostOffsetInput?.value);
        if (![preRaw, postRaw, maskPreRaw, maskPostRaw].every(Number.isFinite)) {
            this._refreshContextInputs();
            return;
        }
        const snapOptions = (field) => (stepDirective?.field === field
            ? { direction: stepDirective.direction }
            : undefined);
        // Four independent snaps mirroring SonderEditor backend: context snaps grow
        // the rendered tensor, mask offsets only choose which frames are masked
        // within the snapped context cap. The mask-offset cap is the post-snap
        // context value so any context change re-snaps the same-side mask offset
        // in the same pass (forward-direction cross-field coupling).
        const pre = this._snapPreContextFrames(preRaw, snapOptions("pre"));
        const post = this._snapPostContextFrames(postRaw, snapOptions("post"));
        const maskPre = this._snapMaskOffset(maskPreRaw, pre, snapOptions("maskPre"));
        const maskPost = this._snapMaskOffset(maskPostRaw, post, snapOptions("maskPost"));
        this._syncContextInputElement(this._preContextInput, pre, "Frames to include before the selected generation range");
        this._syncContextInputElement(this._postContextInput, post, "Frames to include after the selected generation range");
        this._syncContextInputElement(this._maskPreOffsetInput, maskPre, "Extra pre-context frames excluded from denoise mask start");
        this._syncContextInputElement(this._maskPostOffsetInput, maskPost, "Extra post-context frames included in denoise mask end");
        this._setWidgetValue("pre_context_frames", pre);
        this._setWidgetValue("post_context_frames", post);
        this._setWidgetValue("mask_pre_offset", maskPre);
        this._setWidgetValue("mask_post_offset", maskPost);
        this._updateToolbar();
        // Context frames widen the highlight window (selection + context).
        this._refreshPromptUsageHighlight();
        this._renderTimeline();
        this._updateGenReadout();
    }

    _stepContextFrameInput(field, direction) {
        const inputByField = {
            pre: this._preContextInput,
            post: this._postContextInput,
            maskPre: this._maskPreOffsetInput,
            maskPost: this._maskPostOffsetInput,
        };
        const input = inputByField[field];
        if (!input) return;
        const widgetByField = {
            pre: "pre_context_frames",
            post: "post_context_frames",
            maskPre: "mask_pre_offset",
            maskPost: "mask_post_offset",
        };
        const parsed = this._parseContextInputValue(input.value);
        const current = Number.isFinite(parsed) ? parsed : this._contextFrameValue(widgetByField[field]);
        const nextRaw = Math.max(0, current + (direction === "down" ? -1 : 1));
        input.value = this._formatContextInputValue(nextRaw);
        this._updateContextFrameWidgets({ field, direction });
    }

    _stepSelectionInput(edge, direction) {
        const isStart = edge === "start";
        const input = isStart ? this._selectionStartInput : this._selectionEndInput;
        const draft = this._selectionDraftAnchor;
        const fallback = draft && draft.edge !== edge
            ? draft.frame
            : (isStart ? this.selectionStart : this.selectionEnd);
        const parsed = this._parsePositionInput(input?.value);
        const current = Number.isFinite(parsed) ? Math.round(parsed) : fallback;
        const coordinateDirection = direction === "down" ? -1 : 1;
        this._commitManualSelectionEndpoint(edge, current + coordinateDirection, {
            searchDirection: coordinateDirection,
        });
    }

    _refreshSelectionInputs({ force = false } = {}) {
        const draft = this._selectionDraftAnchor;
        const hasSelection = this.selectionStart < this.selectionEnd;
        if (this._selectionStartInput && (force || document.activeElement !== this._selectionStartInput)) {
            const value = draft
                ? (draft.edge === "start" ? this._formatPositionInput(draft.frame) : "")
                : (hasSelection ? this._formatPositionInput(this.selectionStart) : "");
            this._selectionStartInput.value = value;
            this._selectionStartInput.placeholder = !hasSelection && draft?.edge !== "start" ? "Set In" : "";
            this._selectionStartInput.title = draft?.edge === "end"
                ? "Choose an In point to complete the selection"
                : !draft && !hasSelection
                    ? "Set an In point to start a selection"
                    : `Selection in-point: ${this._frameToTimecode(draft?.frame ?? this.selectionStart)}`;
        }
        if (this._selectionEndInput && (force || document.activeElement !== this._selectionEndInput)) {
            const value = draft
                ? (draft.edge === "end" ? this._formatPositionInput(draft.frame) : "")
                : (hasSelection ? this._formatPositionInput(this.selectionEnd) : "");
            this._selectionEndInput.value = value;
            this._selectionEndInput.placeholder = !hasSelection && draft?.edge !== "end" ? "Set Out" : "";
            const duration = Math.max(0, this.selectionEnd - this.selectionStart);
            this._selectionEndInput.title = draft?.edge === "start"
                ? "Choose an Out point to complete the selection"
                : !draft && !hasSelection
                    ? "Set an Out point to start a selection"
                    : `Selection out-point: ${this._frameToTimecode(draft?.frame ?? this.selectionEnd)} (${this._frameToTimecode(duration)})`;
        }
        this._refreshPlayheadInput({ force });
        this._updateGenReadout();
    }

    _refreshPlayheadInput({ force = false } = {}) {
        if (!this._playheadFrameInput) return;
        if (!force && document.activeElement === this._playheadFrameInput) return;
        this._playheadFrameInput.value = this._formatPositionInput(this.playhead);
        this._playheadFrameInput.title = `Playhead frame: ${this._frameToTimecode(this.playhead)}`;
    }

    // Derived generation-window readout: selected frames + source-frame total
    // (selection + context, clamped per-edge like the backend source_frame_count).
    _updateGenReadout() {
        if (!this._genReadout) return;
        const draft = this._selectionDraftAnchor;
        if (draft) {
            const label = draft.edge === "start" ? "In" : "Out";
            const next = draft.edge === "start" ? "Out" : "In";
            this._genReadout.textContent = `${label} ${this._frameToTimecode(draft.frame)} set · Choose ${next}`;
            this._updateGenDurationHint(0);
            return;
        }
        const start = Math.max(0, Math.round(Number(this.selectionStart) || 0));
        const end = Math.max(start, Math.round(Number(this.selectionEnd) || 0));
        const selected = end - start;
        if (selected <= 0) {
            this._genReadout.textContent = "Full scene";
            this._updateGenDurationHint(0);
            return;
        }
        const window = this._selectionExecutionWindow(start, end);
        const source = window.source_frame_count;
        const tensor = window.frame_count;
        const padding = window.frame_count_padding;
        const mode = this._timecodeMode === "timecode" ? "timecode" : "frames";
        const unit = mode === "timecode" ? "" : "f";
        const paddingText = padding > 0 ? ` (+${padding} pad)` : "";
        this._genReadout.textContent = [
            `${formatQueueTime(selected, this._effectiveFps, mode)}${unit} sel`,
            `${formatQueueTime(source, this._effectiveFps, mode)}${unit} src`,
            `${formatQueueTime(tensor, this._effectiveFps, mode)}${unit} tensor${paddingText}`,
        ].join(" · ");
        this._updateGenDurationHint(selected);
    }

    // Non-blocking hint: flag when the SELECTION length falls outside the active
    // template's soft recommended-duration band (evaluated per-selection, not per
    // scene — a scene may mix multiple generation lengths). Advisory only.
    _updateGenDurationHint(selectedFrames) {
        if (!this._genDurationHint) return;
        const template = this._getActiveTemplate();
        const band = getRecommendedDurationSec(template);
        const fps = Number(this._effectiveFps) || 0;
        const frames = Math.max(0, Math.round(Number(selectedFrames) || 0));
        if (template.id === "free" || !band || frames <= 0 || fps <= 0) {
            this._genDurationHint.style.display = "none";
            return;
        }
        if (isSelectionDurationWithinRecommendation({
            frameCount: frames,
            fps,
            minSec: band.minSec,
            maxSec: band.maxSec,
            frameConstraint: this._getActiveFrameConstraint(),
        })) {
            this._genDurationHint.style.display = "none";
            return;
        }
        const fmt = (value) => (Number.isInteger(value) ? String(value) : String(Number(value.toFixed(2))));
        const minFrames = Math.max(1, Math.round(band.minSec * fps));
        const maxFrames = Math.max(minFrames, Math.round(band.maxSec * fps));
        const secLabel = band.minSec === band.maxSec
            ? `${fmt(band.minSec)}s`
            : `${fmt(band.minSec)}–${fmt(band.maxSec)}s`;
        const frameLabel = minFrames === maxFrames ? `${minFrames} frames` : `${minFrames}–${maxFrames} frames`;
        this._genDurationHint.title = `${template.name}: recommended ${secLabel} (${frameLabel} at ${fmt(fps)} fps)`;
        this._genDurationHint.style.display = "";
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

    _setTimelineSelection(start, end, { persist = true, render = true, clearDraft = true } = {}) {
        const maxFrame = Math.max(0, parseInt(this.activeScene?.duration_frames, 10) || this.totalFrames || 0);
        const nextStart = Math.max(0, Math.min(maxFrame, Math.round(Number(start) || 0)));
        const nextEnd = Math.max(0, Math.min(maxFrame, Math.round(Number(end) || 0)));
        if (clearDraft) this._selectionDraftAnchor = null;
        this.selectionStart = Math.min(nextStart, nextEnd);
        this.selectionEnd = Math.max(nextStart, nextEnd);
        this._setWidgetValue("selection_start", this.selectionStart);
        this._setWidgetValue("selection_end", this.selectionEnd);
        if (persist) this._persistActiveTimelineSelection();
        this._refreshSelectionInputs();
        this._refreshPromptUsageHighlight();
        if (render) {
            this._renderTimeline();
            this._updateToolbar();
        }
    }

    _setSelectionDraft(edge, frame) {
        const maxFrame = Math.max(0, parseInt(this.activeScene?.duration_frames, 10) || this.totalFrames || 0);
        const nextEdge = edge === "start" ? "start" : "end";
        const nextFrame = Math.max(0, Math.min(maxFrame, Math.round(Number(frame) || 0)));
        this._setTimelineSelection(0, 0, { render: false, clearDraft: true });
        this._selectionDraftAnchor = { edge: nextEdge, frame: nextFrame };
        this._refreshSelectionInputs({ force: true });
        this._refreshPromptUsageHighlight();
        this._renderTimeline();
        this._updateToolbar();
    }

    _commitManualSelectionEndpoint(edge, frame, { searchDirection = null } = {}) {
        const nextEdge = edge === "start" ? "start" : "end";
        const maxFrame = Math.max(0, parseInt(this.activeScene?.duration_frames, 10) || this.totalFrames || 0);
        const candidate = Math.max(0, Math.min(maxFrame, Math.round(Number(frame) || 0)));
        const hasSelection = this.selectionStart < this.selectionEnd;
        const draft = this._selectionDraftAnchor;

        if (!hasSelection && (!draft || draft.edge === nextEdge)) {
            this._setSelectionDraft(nextEdge, candidate);
            return { status: "draft", endpoint: candidate };
        }

        const anchorFrame = hasSelection
            ? (nextEdge === "start" ? this.selectionEnd : this.selectionStart)
            : draft.frame;
        const usable = nextEdge === "start" ? candidate < anchorFrame : candidate > anchorFrame;
        if (!usable) {
            if (hasSelection) {
                this._setSelectionDraft(nextEdge, candidate);
                return { status: "draft", endpoint: candidate };
            }
            notifyWarning(
                nextEdge === "start"
                    ? "Choose an In point before the anchored Out point."
                    : "Choose an Out point after the anchored In point.",
                { source: "selection-draft-invalid-endpoint" }
            );
            this._refreshSelectionInputs({ force: true });
            return { status: "invalid", endpoint: candidate };
        }

        const direction = searchDirection == null
            ? (nextEdge === "start" ? -1 : 1)
            : (searchDirection < 0 ? -1 : 1);
        const result = this._findManualSelectionEndpoint(nextEdge, anchorFrame, candidate, direction);
        if (!result.valid) {
            notifyWarning("The selected endpoint cannot form a non-empty range.", {
                source: "selection-draft-empty-range",
            });
            this._refreshSelectionInputs({ force: true });
            return { status: "invalid", endpoint: candidate };
        }

        const start = nextEdge === "start" ? result.endpoint : anchorFrame;
        const end = nextEdge === "end" ? result.endpoint : anchorFrame;
        this._setTimelineSelection(start, end);
        return {
            status: "complete",
            endpoint: result.endpoint,
            window: result.window,
            usedPaddingFallback: result.used_padding_fallback,
        };
    }

    _setSelectionStartFrame(frame) {
        this._commitManualSelectionEndpoint("start", frame);
    }

    _setSelectionEndFrame(frame) {
        this._commitManualSelectionEndpoint("end", frame);
    }

    _clearTimelineSelection() {
        this._setTimelineSelection(0, 0);
    }

    _setSelectionToFrameRange(start, end) {
        this._setTimelineSelection(start, end);
    }

    _resolvedGuideFrame(guide) {
        const sceneDuration = Math.max(0, parseInt(this.activeScene?.duration_frames, 10) || this.totalFrames || 0);
        const rawFrame = parseInt(guide?.frame_index, 10);
        if (rawFrame === -1) return Math.max(0, sceneDuration - 1);
        return Math.max(0, Math.round(Number.isFinite(rawFrame) ? rawFrame : 0));
    }

    _guideHoldFrameRange(guide) {
        if (!guide || !this.activeScene) return null;
        const start = this._resolvedGuideFrame(guide);
        const sceneDuration = Math.max(0, parseInt(this.activeScene.duration_frames, 10) || this.totalFrames || 0);
        const nextGuide = (this.activeScene.guide_frames || [])
            .map((candidate) => ({ guide: candidate, frame: this._resolvedGuideFrame(candidate) }))
            .filter((candidate) => candidate.frame > start)
            .sort((a, b) => a.frame - b.frame)[0];
        const end = nextGuide ? nextGuide.frame : sceneDuration;
        return { start, end: Math.max(start, end) };
    }

    _selectionRangeForItem(item) {
        if (!item) return null;
        const data = item.data || {};
        let start = 0;
        let end = 0;
        if (item.type === "clip" || item.type === "audio") {
            start = data.timeline_start_frame;
            end = data.timeline_end_frame;
        } else if (item.type === "prompt") {
            start = data.start_frame;
            end = data.end_frame;
        } else if (item.type === "guide") {
            start = this._resolvedGuideFrame(data);
            end = start + 1;
        } else {
            return null;
        }
        const nextStart = Math.max(0, Math.round(Number(start) || 0));
        const nextEnd = Math.max(nextStart, Math.round(Number(end) || 0));
        return { start: nextStart, end: nextEnd };
    }

    _setSelectionToItems(items = this.selectedItems) {
        const ranges = (items || [])
            .map((item) => this._selectionRangeForItem(item))
            .filter(Boolean);
        if (!ranges.length) return false;
        const start = Math.min(...ranges.map((range) => range.start));
        const end = Math.max(...ranges.map((range) => range.end));
        this._setSelectionToFrameRange(start, end);
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
        return this._isLaneTrackType(type) || type === TRACK_TYPE.GUIDES
            || type === TRACK_TYPE.PROMPT || type === TRACK_TYPE.PROMPT_GLOBAL;
    }

    _defaultLaneConfig(overrides = {}) {
        return { name: "", color: "", locked: false, hidden: false, ...overrides };
    }

    _trackConfigForFixedType(type) {
        if (type === TRACK_TYPE.GUIDES) return this.activeScene?.guide_track_config || this._defaultLaneConfig();
        if (type === TRACK_TYPE.PROMPT) return this.activeScene?.prompt_track_config || this._defaultLaneConfig();
        if (type === TRACK_TYPE.PROMPT_GLOBAL) return this.activeScene?.global_prompt_track_config || this._defaultLaneConfig();
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
        if (this._isLaneVisibilityControlDisabled(entry)) return "hidden";
        const items = this._trackItemsForEntry(entry);
        if (entry.type === TRACK_TYPE.PROMPT || entry.type === TRACK_TYPE.PROMPT_GLOBAL) {
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

    _isGlobalPromptTrackLocked() {
        const idx = this._globalPromptLayoutIdx();
        return idx >= 0 && !!this._trackLayout[idx]?.locked;
    }

    _isGlobalPromptTrackHidden() {
        const idx = this._globalPromptLayoutIdx();
        return idx >= 0 && !!this._trackLayout[idx]?.hidden;
    }

    _isItemLocked(item) {
        if (!item) return false;
        if (item.type === "clip") return this._isLaneLocked(this._clipTrackType(item.data), item.data.track_index || 0);
        if (item.type === "audio") return this._isLaneLocked(TRACK_TYPE.AUDIO, item.data.lane_index || 0);
        if (item.type === "guide") return this._isGuideTrackLocked();
        if (item.type === "prompt") return this._isPromptTrackLocked();
        if (item.type === "prompt_global") return this._isGlobalPromptTrackLocked();
        return false;
    }

    _newLocalItemId(prefix = "item") {
        return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(16).slice(2, 8)}`;
    }

    _linkableItemTypes() {
        return new Set(["clip", "audio", "guide", "prompt"]);
    }

    _selectionItemKey(item) {
        return `${item?.type || ""}:${String(item?.id ?? "")}`;
    }

    _linkRefKey(ref) {
        return `${ref?.type || ""}:${String(ref?.id ?? "")}`;
    }

    _linkRefForItem(item) {
        if (!item || !this._linkableItemTypes().has(item.type)) return null;
        const data = item.data || {};
        if (item.type === "clip") return { type: "clip", id: String(data.clip_id ?? item.id ?? "") };
        if (item.type === "audio") return { type: "audio", id: String(data.track_id ?? item.id ?? "") };
        if (item.type === "guide") return { type: "guide", id: String(data.guide_id ?? item.id ?? data.frame_index ?? "") };
        if (item.type === "prompt") return { type: "prompt", id: String(data.prompt_id ?? item.id ?? "") };
        return null;
    }

    _findSceneItemForLinkRef(ref) {
        if (!this.activeScene || !ref) return null;
        const itemType = String(ref.type || "");
        const itemId = String(ref.id ?? "");
        if (itemType === "clip") {
            const clip = (this.activeScene.clips || []).find((item) => String(item.clip_id) === itemId);
            return clip ? { type: "clip", id: clip.clip_id, data: clip } : null;
        }
        if (itemType === "audio") {
            const track = (this.activeScene.audio_tracks || []).find((item) => String(item.track_id) === itemId);
            return track ? { type: "audio", id: track.track_id, data: track } : null;
        }
        if (itemType === "guide") {
            const guide = (this.activeScene.guide_frames || []).find((item) =>
                String(item.guide_id || "") === itemId || String(item.frame_index) === itemId
            );
            return guide ? { type: "guide", id: guide.frame_index, data: guide } : null;
        }
        if (itemType === "prompt") {
            const sections = this.activeScene.prompt_sections || [];
            const idx = sections.findIndex((item, index) =>
                String(item.prompt_id || "") === itemId || String(index) === itemId
            );
            return idx >= 0 ? { type: "prompt", id: idx, data: sections[idx] } : null;
        }
        return null;
    }

    _linkGroupForItem(item) {
        const ref = this._linkRefForItem(item);
        if (!ref) return null;
        const key = this._linkRefKey(ref);
        return (this.activeScene?.linked_item_groups || []).find((group) =>
            (group?.items || []).some((candidate) => this._linkRefKey(candidate) === key)
        ) || null;
    }

    _isLinkedItem(item) {
        return !!this._linkGroupForItem(item);
    }

    /** Stable display label for a link group: A..Z, AA, AB... by group order. */
    _linkGroupLabel(group) {
        if (!group) return "";
        const groups = this.activeScene?.linked_item_groups || [];
        let idx = groups.indexOf(group);
        if (idx < 0 && group.group_id) {
            idx = groups.findIndex((candidate) => candidate?.group_id === group.group_id);
        }
        if (idx < 0) return "";
        let label = "";
        let n = idx + 1;
        while (n > 0) {
            label = String.fromCharCode(65 + ((n - 1) % 26)) + label;
            n = Math.floor((n - 1) / 26);
        }
        return label;
    }

    /** A link group is effectively locked when any member sits on a locked lane/track. */
    _isLinkGroupLocked(group) {
        if (!group) return false;
        return (group.items || []).some((ref) => {
            const item = this._findSceneItemForLinkRef(ref);
            return item ? this._isItemLocked(item) : false;
        });
    }

    _expandItemsWithLinked(items = this.selectedItems) {
        const expanded = [];
        const seen = new Set();
        const addHit = (hit) => {
            if (!hit) return;
            const key = this._selectionItemKey(hit);
            if (seen.has(key)) return;
            expanded.push(hit);
            seen.add(key);
        };
        for (const item of items || []) {
            const current = this._findSceneItemBySelection(item.type, item.id) || item;
            addHit(current);
            const group = this._linkGroupForItem(current);
            for (const ref of group?.items || []) {
                addHit(this._findSceneItemForLinkRef(ref));
            }
        }
        return expanded;
    }

    _expandedLinkedRefs(items = this.selectedItems) {
        const refs = [];
        const seen = new Set();
        for (const item of this._expandItemsWithLinked(items)) {
            const ref = this._linkRefForItem(item);
            if (!ref) continue;
            const key = this._linkRefKey(ref);
            if (seen.has(key)) continue;
            refs.push(ref);
            seen.add(key);
        }
        return refs;
    }

    _selectedLinkableItems() {
        return (this.selectedItems || []).filter((item) => item && this._linkableItemTypes().has(item.type));
    }

    _mutationItemFromSelection(item) {
        if (!item) return null;
        if (item.type === "guide") {
            return {
                type: "guide",
                id: item.id,
                expected: {
                    frame_index: item.data?.frame_index ?? item.id,
                    asset_id: item.data?.asset_id || "",
                    guide_id: item.data?.guide_id || "",
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
                    prompt_id: item.data?.prompt_id || "",
                },
            };
        }
        if (item.type === "clip" || item.type === "audio") {
            return { type: item.type, id: item.id };
        }
        return null;
    }

    _pruneLocalLinkedGroups() {
        if (!this.activeScene) return;
        const groups = this.activeScene.linked_item_groups || [];
        const next = [];
        for (const group of groups) {
            const items = [];
            const seen = new Set();
            for (const ref of group?.items || []) {
                const hit = this._findSceneItemForLinkRef(ref);
                if (!hit) continue;
                const stableRef = this._linkRefForItem(hit);
                if (!stableRef) continue;
                const key = this._linkRefKey(stableRef);
                if (seen.has(key)) continue;
                items.push(stableRef);
                seen.add(key);
            }
            if (items.length >= 2) next.push({ group_id: group.group_id || this._newLocalItemId("link"), items });
        }
        this.activeScene.linked_item_groups = next;
    }

    _laneRefForEntry(entry) {
        if (!entry || !this._isHeaderControllableTrackType(entry.type)) return null;
        return {
            type: entry.type,
            laneIndex: this._isLaneTrackType(entry.type) ? (entry.laneIndex || 0) : 0,
        };
    }

    _laneRefKey(ref) {
        return `${ref?.type || ""}:${Number(ref?.laneIndex || 0)}`;
    }

    _isLaneSelected(entryOrRef) {
        const ref = entryOrRef?.type ? this._laneRefForEntry(entryOrRef) || entryOrRef : null;
        if (!ref) return false;
        const key = this._laneRefKey(ref);
        return (this._selectedLanes || []).some((candidate) => this._laneRefKey(candidate) === key);
    }

    _clearLaneSelection() {
        this._selectedLanes = [];
    }

    _setSelectedLanes(lanes = []) {
        const next = [];
        const seen = new Set();
        for (const lane of lanes || []) {
            const key = this._laneRefKey(lane);
            if (!lane?.type || seen.has(key)) continue;
            next.push({ type: lane.type, laneIndex: Number(lane.laneIndex || 0) });
            seen.add(key);
        }
        this._selectedLanes = next;
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

    _defaultFitMode() {
        const value = this._settings?.projectDefaults?.defaultFitMode
            ?? DEFAULT_EDITOR_SETTINGS.projectDefaults.defaultFitMode;
        return VALID_FIT_MODES.has(value) ? value : DEFAULT_EDITOR_SETTINGS.projectDefaults.defaultFitMode;
    }

    _defaultCropPosition() {
        const value = this._settings?.projectDefaults?.defaultCropPosition
            ?? DEFAULT_EDITOR_SETTINGS.projectDefaults.defaultCropPosition;
        return VALID_CROP_POSITIONS.has(value) ? value : "center";
    }

    // Stamp browser-local fit defaults onto a create-fields/POST body before it is
    // both optimistically applied and persisted, so the local mirror and saved record
    // agree. This is the ONLY place the browser default is consulted — never a
    // render-time fallback (that's the fixed code constant in the backend from_dict).
    _seedFitDefaults(fields = {}) {
        if (fields.fit_mode == null) fields.fit_mode = this._defaultFitMode();
        if (fields.crop_position == null) fields.crop_position = this._defaultCropPosition();
        return fields;
    }

    _toggleAnimatic() {
        if (!this.activeScene || !this.projectDir) return;
        this._animaticMode = !this._animaticMode;
        this._clearPlaybackWarmOverlay("animatic-toggle", { render: false });
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

        // Driver lanes: below audio, ordered by lane index.
        for (let i = 0; i < motionDriverLanes; i++) {
            const key = TRACK_TYPE.MOTION_DRIVER + ":" + i;
            const cfg = mdConfigs[i] || {};
            layout.push({
                type: TRACK_TYPE.MOTION_DRIVER,
                label: cfg.name || (motionDriverLanes > 1 ? `Driver ${i + 1}` : "Driver"),
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
        const globalPromptCfg = this._trackConfigForFixedType(TRACK_TYPE.PROMPT_GLOBAL);
        layout.push({
            type: TRACK_TYPE.PROMPT_GLOBAL,
            label: "Global",
            customName: "",
            laneIndex: 0,
            collapsed: isStored ? storedCollapsed.has(TRACK_TYPE.PROMPT_GLOBAL + ":0") : false,
            color: "",
            locked: !!globalPromptCfg.locked,
            hidden: !!globalPromptCfg.hidden,
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

    /** Find layout index for a driver lane */
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

    /** Find layout index for the global prompt lane */
    _globalPromptLayoutIdx() {
        return this._trackLayout.findIndex(e => e.type === TRACK_TYPE.PROMPT_GLOBAL);
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

    _renderCacheMaxBytes(settings = this._settings) {
        const rawValue = settings?.render?.maxRenderCacheSizeBytes;
        if (rawValue === null) return null;
        return typeof rawValue === "number" && Number.isSafeInteger(rawValue) && rawValue > 0
            ? rawValue
            : 0;
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

    async _refreshRenderCacheUsage() {
        const dirName = this._projectDirName();
        const sweepSeq = this._renderCacheSweepSeq;
        if (!dirName) {
            this._renderCacheUsage = null;
            this._renderCacheSweepPending = false;
            this._syncSettingsPanelControls();
            return null;
        }

        try {
            const listResp = await fetch(api.apiURL(`/sonder-editor/project/${encodeURIComponent(dirName)}/cache/renders`));
            if (!listResp.ok) return null;
            const data = await listResp.json();
            const entries = Array.isArray(data) ? data : (Array.isArray(data.entries) ? data.entries : []);
            const usage = {
                entry_count: entries.length,
                size_bytes: entries.reduce((sum, entry) => {
                    const size = Number(entry?.size_bytes);
                    return sum + (Number.isFinite(size) && size > 0 ? size : 0);
                }, 0),
                deleted: [],
                deleted_bytes: 0,
                protected: [],
                over_budget_bytes: 0,
                pending: false,
                failures: [],
            };
            if (sweepSeq === this._renderCacheSweepSeq && dirName === this._projectDirName()) {
                this._renderCacheUsage = usage;
                this._renderCacheSweepPending = false;
                this._syncSettingsPanelControls();
            }
            return usage;
        } catch (error) {
            console.warn("[Sonder] Failed to read render cache usage:", error);
            return null;
        }
    }

    async _sweepRenderCache(maxSizeBytes = this._renderCacheMaxBytes()) {
        const dirName = this._projectDirName();
        const seq = ++this._renderCacheSweepSeq;
        if (!dirName) {
            this._renderCacheUsage = null;
            this._renderCacheSweepPending = false;
            this._syncSettingsPanelControls();
            return null;
        }
        try {
            const response = await fetch(
                api.apiURL(`/sonder-editor/project/${encodeURIComponent(dirName)}/cache/renders/sweep`),
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ max_size_bytes: maxSizeBytes }),
                },
            );
            if (!response.ok) {
                console.warn("[Sonder] Render cache sweep skipped:", response.status);
                return null;
            }
            const result = await response.json();
            if (seq === this._renderCacheSweepSeq && dirName === this._projectDirName()) {
                this._renderCacheUsage = result;
                this._renderCacheSweepPending = result?.pending === true;
                this._syncSettingsPanelControls();
            }
            return result;
        } catch (error) {
            console.warn("[Sonder] Failed to sweep render cache:", error);
            return null;
        }
    }

    async _clearRenderCache() {
        return this._sweepRenderCache(0);
    }

    _syncTakePlacementModeWidget(settings = this._settings) {
        const mode = settings?.render?.takePlacementMode === "untrimmed" ? "untrimmed" : "trimmed";
        const configuredFitMode = settings?.projectDefaults?.defaultFitMode;
        const configuredCropPosition = settings?.projectDefaults?.defaultCropPosition;
        const fitMode = VALID_FIT_MODES.has(configuredFitMode)
            ? configuredFitMode
            : DEFAULT_EDITOR_SETTINGS.projectDefaults.defaultFitMode;
        const cropPosition = VALID_CROP_POSITIONS.has(configuredCropPosition)
            ? configuredCropPosition
            : DEFAULT_EDITOR_SETTINGS.projectDefaults.defaultCropPosition;
        this._setWidgetValue("take_placement_mode", mode);
        this._setWidgetValue("take_placement_linked", settings?.render?.linkedTakePlacement !== false);
        this._setWidgetValue("take_placement_muted", !!settings?.render?.takePlacementMuted);
        this._setWidgetValue("take_fit_mode", fitMode);
        this._setWidgetValue("take_crop_position", cropPosition);
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
        const prevRenderCacheLimit = this._renderCacheMaxBytes(this._settings);
        const nextRenderCacheLimit = this._renderCacheMaxBytes(nextSettings);
        const prevTimecodeMode = this._timecodeMode;
        const prevStreamingMode = this._settings?.playback?.streamingMode ?? "auto";
        const nextStreamingMode = nextSettings?.playback?.streamingMode ?? "auto";
        const prevLaneTintSignature = JSON.stringify(this._settings?.appearance?.laneTintOverrides || {});
        const nextLaneTintSignature = JSON.stringify(nextSettings?.appearance?.laneTintOverrides || {});
        const prevClipLabelSignature = JSON.stringify({
            mode: this._settings?.appearance?.clipLabelMode,
            v: this._settings?.appearance?.clipLabelVerticalAlign,
            h: this._settings?.appearance?.clipLabelHorizontalAlign,
        });
        const nextClipLabelSignature = JSON.stringify({
            mode: nextSettings?.appearance?.clipLabelMode,
            v: nextSettings?.appearance?.clipLabelVerticalAlign,
            h: nextSettings?.appearance?.clipLabelHorizontalAlign,
        });
        const prevSceneOutline = this._settings?.appearance?.sceneOutline !== false;
        const nextSceneOutline = nextSettings?.appearance?.sceneOutline !== false;
        const prevMarginSignature = JSON.stringify(this._settings?.appearance?.editorMargins || {});
        const nextMarginSignature = JSON.stringify(nextSettings?.appearance?.editorMargins || {});
        const marginsChanged = prevMarginSignature !== nextMarginSignature;
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
        if (this.activeScene || this._toolbar) {
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
        if (prevClipLabelSignature !== nextClipLabelSignature && this.timelineCanvas) {
            this._renderTimeline();
        }
        if (prevSceneOutline !== nextSceneOutline && !this.isPlaying) {
            this._renderViewportFrame();
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
        if (prevStreamingMode !== nextStreamingMode && !this.isPlaying) {
            this._clearVideoCache();
        }
        if (prevTimecodeMode !== this._timecodeMode) {
            this._refreshContextInputs();
        }
        if (prevTimecodeMode !== this._timecodeMode && this._itemEditorEl && this.selectedItem) {
            this._showItemEditor();
        }
        this._syncSettingsPanelControls();
        if (this._toolbar) {
            this._applyScales();
        }
        if (marginsChanged) {
            this._applyEditorMargins(nextSettings);
        }
        if (this.isFullscreen) {
            if (this._fsSidebar && nextSettings.layout.fullscreenSidebarWidth > 0) {
                const sidebarMax = this._computeFullscreenSidebarMaxWidth();
                this._fsSidebar.style.width = `${Math.max(FULLSCREEN_SIDEBAR_MIN_WIDTH, Math.min(sidebarMax, nextSettings.layout.fullscreenSidebarWidth))}px`;
            }
            if (this._fsBottomRow) {
                const requestedTimelineH = nextSettings.layout.fullscreenTimelineHeight > 0
                    ? nextSettings.layout.fullscreenTimelineHeight
                    : (parseInt(getComputedStyle(this._fsBottomRow).height, 10) || this._defaultFullscreenTimelineHeight());
                this._applyFullscreenTimelineHeight(requestedTimelineH);
            }
            this._recalcFullscreenHeights();
            if (marginsChanged) {
                this._renderTimeline();
            }
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
            get _renderCacheUsage() { return editor._renderCacheUsage; },
            get _renderCacheProjectName() { return editor._projectDirName(); },
            _refreshRenderCacheUsage: () => editor._refreshRenderCacheUsage(),
            _clearRenderCache: () => editor._clearRenderCache(),
            get _thumbnailRepairOwnerId() { return editor._thumbnailRepairOwnerId; },
            get _thumbnailRepairPreflight() { return editor._thumbnailRepairPreflight; },
            get _thumbnailRepairProjectName() { return editor._projectDirName(); },
            _thumbnailBulkRepairState: () => editor._thumbnailBulkRepairState(),
            _toggleThumbnailBulkRepair: () => editor._toggleThumbnailBulkRepair(),
            _guideHoverPreviewSize: () => editor._guideHoverPreviewSize(),
            _hideGuideHoverPreview: () => editor._hideGuideHoverPreview(),
            _hidePromptHoverPreview: () => editor._hidePromptHoverPreview(),
            _deleteCustomModelTemplate: (templateId) => editor._deleteCustomModelTemplate(templateId),
            // Prompts section — project-wide knobs are host-owned versioned
            // project PUTs (not settings writes); getters back their sync
            get _promptChannelLabels() { return editor._promptChannelLabels; },
            get _promptSectionDelimiter() { return editor._promptSectionDelimiter; },
            get _promptFrameThreshold() { return editor._promptFrameThreshold; },
            get _guideCollisionAutoOffset() { return editor._guideCollisionAutoOffset; },
            get _serverSettings() { return editor._serverSettings; },
            get _serverSettingsLoaded() { return editor._serverSettingsLoaded; },
            _loadServerSettings: () => editor._loadServerSettings(),
            _setAllowExternalProjectLinks: (enabled) => editor._setAllowExternalProjectLinks(enabled),
            _togglePromptChannelLabels: (on) => editor._togglePromptChannelLabels(on),
            _toggleGuideCollisionAutoOffset: (on) => editor._toggleGuideCollisionAutoOffset(on),
            _setPromptSectionDelimiter: (value) => editor._setPromptSectionDelimiter(value),
            _setPromptFrameThreshold: (value) => editor._setPromptFrameThreshold(value),
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

    _thumbnailBulkRepairState() {
        return getBulkThumbnailRepairState();
    }

    async _toggleThumbnailBulkRepair() {
        const active = getBulkThumbnailRepairState();
        if (active) {
            if (active.ownerId === this._thumbnailRepairOwnerId) {
                cancelBulkThumbnailRepair({ ownerId: this._thumbnailRepairOwnerId });
            } else {
                notifyWarning(`Thumbnail regeneration is already running for ${active.projectId}.`, {
                    source: "thumbnail-repair",
                });
            }
            return;
        }
        if (!this.projectDir || this._thumbnailRepairPreflight) return;

        const projectDir = this.projectDir;
        const projectName = this._projectDirName();
        this._thumbnailRepairPreflight = true;
        this._syncSettingsPanelControls();
        try {
            const candidates = await fetchMissingThumbnailAssets(projectDir);
            if (this._destroyed || this.projectDir !== projectDir) return;
            if (!candidates.length) {
                notifyInfo("No missing thumbnails need regeneration.", { source: "thumbnail-repair" });
                return;
            }
            const noun = candidates.length === 1 ? "thumbnail" : "thumbnails";
            if (!window.confirm(`Regenerate ${candidates.length} missing ${noun} for "${projectName}"? Existing thumbnails, Trash, filmstrips, and waveforms will not be changed.`)) {
                return;
            }
            this._thumbnailRepairPreflight = false;
            this._syncSettingsPanelControls();
            await startBulkThumbnailRepair({
                ownerId: this._thumbnailRepairOwnerId,
                projectDir,
                assets: candidates,
            });
        } catch (error) {
            notifyError(error?.message || "Thumbnail regeneration failed.", { source: "thumbnail-repair" });
        } finally {
            this._thumbnailRepairPreflight = false;
            this._syncSettingsPanelControls();
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

    _clipLabelVerticalAlign() {
        return this._settings?.appearance?.clipLabelVerticalAlign
            || DEFAULT_EDITOR_SETTINGS.appearance.clipLabelVerticalAlign;
    }

    _clipLabelHorizontalAlign() {
        return this._settings?.appearance?.clipLabelHorizontalAlign
            || DEFAULT_EDITOR_SETTINGS.appearance.clipLabelHorizontalAlign;
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
        const m = this._settings?.appearance?.editorMargins || { sides: 0 };
        const avail = Math.max(0, window.innerWidth - (m.sides || 0) * 2);
        return Math.max(FULLSCREEN_SIDEBAR_MIN_WIDTH, Math.floor(avail * 0.5));
    }

    // First-run / reset gallery-sidebar width: a proportion of the editor area (not a fixed px),
    // clamped to [min, 50% max], so the gallery:viewport split starts ~38:62 (golden ratio) on any screen.
    _defaultFullscreenSidebarWidth() {
        const m = this._settings?.appearance?.editorMargins || { sides: 0 };
        const avail = Math.max(0, window.innerWidth - (m.sides || 0) * 2);
        const target = avail > 0 ? Math.round(avail * FULLSCREEN_SIDEBAR_DEFAULT_FRACTION) : FULLSCREEN_SIDEBAR_DEFAULT_WIDTH;
        return Math.max(FULLSCREEN_SIDEBAR_MIN_WIDTH, Math.min(this._computeFullscreenSidebarMaxWidth(), target));
    }

    _computeFullscreenTimelineMaxHeight() {
        const m = this._settings?.appearance?.editorMargins || { top: 0, bottom: 0 };
        const avail = Math.max(0, window.innerHeight - (m.top || 0) - (m.bottom || 0));
        return Math.max(FULLSCREEN_TIMELINE_MIN_HEIGHT, Math.floor(avail * 0.8));
    }

    _visibleTimelineCanvasMinHeight() {
        const rulerH = this._timelineRulerHeight?.() ?? RULER_HEIGHT;
        return Math.max(100, rulerH + 1);
    }

    _measureFullscreenTimelineChrome() {
        this._reserveToolbarHeight();
        const cs = this.container ? getComputedStyle(this.container) : null;
        const paddingY = cs
            ? (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0)
            : 0;
        let chromeH = 0;
        if (this.container) {
            for (const child of this.container.children) {
                if (child === this.timelineCanvas) continue;
                chromeH += child.offsetHeight || 0;
            }
        }
        const visibleCanvasMin = this._visibleTimelineCanvasMinHeight();
        return {
            chromeH,
            paddingY,
            visibleCanvasMin,
            minBottomH: chromeH + paddingY + visibleCanvasMin,
        };
    }

    _clampFullscreenTimelineHeight(requestedHeight, metrics = null) {
        const resolvedMetrics = metrics || this._measureFullscreenTimelineChrome();
        const minHeight = resolvedMetrics.minBottomH;
        const maxHeight = Math.max(this._computeFullscreenTimelineMaxHeight(), minHeight);
        const numeric = Number(requestedHeight);
        const fallback = this._defaultFullscreenTimelineHeight();
        const requested = Number.isFinite(numeric) ? numeric : fallback;
        return {
            height: Math.max(minHeight, Math.min(maxHeight, requested)),
            maxHeight,
            metrics: resolvedMetrics,
        };
    }

    _applyFullscreenTimelineHeight(requestedHeight, metrics = null) {
        if (!this._fsBottomRow) return this._clampFullscreenTimelineHeight(requestedHeight, metrics);
        const result = this._clampFullscreenTimelineHeight(requestedHeight, metrics);
        this._fsBottomRow.style.maxHeight = `${Math.ceil(result.maxHeight)}px`;
        this._fsBottomRow.style.height = `${Math.ceil(result.height)}px`;
        return result;
    }

    _defaultFullscreenTimelineHeight() {
        // First-run / reset timeline (bottom) height = golden-ratio 38.2% of the window, so the
        // viewport+gallery top area starts at ~61.8%. Clamped to [200, FULLSCREEN_TIMELINE_FALLBACK_MAX_HEIGHT].
        return Math.max(200, Math.min(FULLSCREEN_TIMELINE_FALLBACK_MAX_HEIGHT, Math.round(window.innerHeight * 0.382)));
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
            this._fsSidebar.style.width = `${this._defaultFullscreenSidebarWidth()}px`;
        }
        if (this._fsBottomRow) {
            this._applyFullscreenTimelineHeight(this._defaultFullscreenTimelineHeight());
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
        const measurePlayback = isSessionDiagEnabled() && this.isPlaying;
        const timelineStartedAt = measurePlayback ? performance.now() : 0;
        this._refreshPlayheadInput?.();
        const canvasResult = TimelineCanvas._renderTimeline(this);
        const timelineFinishedAt = measurePlayback ? performance.now() : 0;
        this._updateToolbar();
        if (!measurePlayback) return null;
        const toolbarFinishedAt = performance.now();
        return {
            timelineMs: timelineFinishedAt - timelineStartedAt,
            toolbarMs: toolbarFinishedAt - timelineFinishedAt,
            canvasBackingResized: !!canvasResult?.backingChanged,
        };
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

    _hitTestGlobalPrompt(x, rawY) {
        return TimelineCanvas._hitTestGlobalPrompt(this, x, rawY);
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
            const clip = (this.activeScene.clips || []).find((item) => String(item.clip_id) === String(id));
            return clip ? { type, id, data: clip } : null;
        }
        if (type === "audio") {
            const track = (this.activeScene.audio_tracks || []).find((item) => String(item.track_id) === String(id));
            return track ? { type, id, data: track } : null;
        }
        if (type === "guide") {
            const guide = (this.activeScene.guide_frames || []).find((item) =>
                String(item.frame_index) === String(id) || String(item.guide_id || "") === String(id)
            );
            return guide ? { type, id: guide.frame_index, data: guide } : null;
        }
        if (type === "prompt") {
            const sections = this.activeScene.prompt_sections || [];
            const idx = sections.findIndex((item, index) =>
                String(index) === String(id) || String(item.prompt_id || "") === String(id)
            );
            return idx >= 0 ? { type, id: idx, data: sections[idx] } : null;
        }
        return null;
    }

    /** Detect if the mouse is near the left or right edge of a clip/audio track for trimming.
     *  Returns { type, id, data, edge: "left"|"right" } or null. */
    _hitTestEdge(x, rawY) {
        return TimelineCanvas._hitTestEdge(this, x, rawY);
    }

    _trimDeltaLimits(edgeHit, origStart, origEnd, origSourceIn, origSourceOut, origTotalSourceFrames) {
        let minDelta = -Math.max(0, origSourceIn || 0);
        let maxDelta = Math.max(0, (origTotalSourceFrames || 0) - (origSourceOut || 0));
        const linkedItems = this._isLinkedItem(edgeHit) ? this._expandItemsWithLinked([edgeHit]) : [edgeHit];
        const mediaItems = linkedItems.filter((item) => item?.type === "clip" || item?.type === "audio");
        const excluded = new Set(mediaItems.map((item) => this._selectionItemKey(item)));

        for (const item of mediaItems) {
            const data = item.data || {};
            const start = data.timeline_start_frame || 0;
            const end = data.timeline_end_frame || 0;
            const affected = edgeHit.edge === "left" ? start === origStart : end === origEnd;
            if (!affected) continue;

            const sourceIn = Math.max(0, data.source_in_frame || 0);
            const duration = Math.max(1, end - start);
            const sourceOut = item.type === "clip" && Number.isFinite(data.source_out_frame)
                ? data.source_out_frame
                : sourceIn + duration;
            const totalSource = Number.isFinite(data.total_source_frames) && data.total_source_frames > 0
                ? data.total_source_frames
                : sourceOut + duration;
            minDelta = Math.max(minDelta, -sourceIn);
            maxDelta = Math.min(maxDelta, Math.max(0, totalSource - sourceOut));

            const lane = item.type === "clip" ? (data.track_index || 0) : (data.lane_index || 0);
            const others = item.type === "clip" ? (this.activeScene?.clips || []) : (this.activeScene?.audio_tracks || []);
            for (const other of others) {
                const otherId = item.type === "clip" ? other.clip_id : other.track_id;
                if (excluded.has(this._selectionItemKey({ type: item.type, id: otherId }))) continue;
                const otherLane = item.type === "clip" ? (other.track_index || 0) : (other.lane_index || 0);
                if (otherLane !== lane) continue;
                if (item.type === "clip" && this._clipTrackType(other) !== this._clipTrackType(data)) continue;
                const otherStart = other.timeline_start_frame || 0;
                const otherEnd = other.timeline_end_frame || 0;
                if (edgeHit.edge === "left" && otherEnd <= start) {
                    minDelta = Math.max(minDelta, otherEnd - start);
                } else if (edgeHit.edge === "right" && otherStart >= end) {
                    maxDelta = Math.min(maxDelta, otherStart - end);
                }
            }
        }
        return {
            minStart: Math.max(0, origStart + minDelta),
            maxEnd: origEnd + Math.max(0, maxDelta),
        };
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
        this._clearLaneSelection();
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
        this._clearLaneSelection();
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
        this._clearLaneSelection();
        if (!this._isSelected(hit.type, hit.id)) {
            this.selectedItems.push(hit);
        } else {
            this._refreshSelectedHit(hit);
            return;
        }
        this.selectedItem = hit;
    }

    _dedupeSelectionItems(items = []) {
        const next = [];
        const seen = new Set();
        for (const item of items || []) {
            if (!item) continue;
            const key = this._selectionItemKey(item);
            if (seen.has(key)) continue;
            next.push(item);
            seen.add(key);
        }
        return next;
    }

    _dragSelectContentY(rawY) {
        const rulerH = this._timelineRulerHeight();
        const visibleH = this._visibleTimelineContentHeight();
        const clampedRawY = Math.max(rulerH, Math.min(rulerH + visibleH, Number(rawY) || 0));
        return Math.max(0, Math.min(
            this._totalTracksHeight(),
            clampedRawY - rulerH + this.scrollY,
        ));
    }

    _startItemDragSelect(x, rawY, event) {
        const additive = !!(event?.shiftKey || event?.ctrlKey || event?.metaKey);
        const contentY = this._dragSelectContentY(rawY);
        this.isDragging = true;
        this.dragType = "boxSelect";
        this._dragSelectRect = {
            kind: "items",
            startX: x,
            startFrame: Math.max(0, this._xToFrame(x)),
            startRawY: rawY,
            startContentY: contentY,
            currentX: x,
            currentRawY: rawY,
            currentContentY: contentY,
            additive,
            activated: false,
        };
        this._dragSelectBaseItems = additive ? [...(this.selectedItems || [])] : [];
        this._clearLaneSelection();
        if (!additive) {
            this._clearSelection();
            this._hideItemEditor();
            this._hidePromptEditor();
        }
    }

    _startLaneDragSelect(headerHit, rawY, event) {
        const additive = !!(event?.shiftKey || event?.ctrlKey || event?.metaKey);
        const contentY = this._dragSelectContentY(rawY);
        this.isDragging = true;
        this.dragType = "laneSelect";
        this._dragSelectRect = {
            kind: "lanes",
            startX: 0,
            startRawY: rawY,
            startContentY: contentY,
            currentX: this._labelW,
            currentRawY: rawY,
            currentContentY: contentY,
            additive,
            activated: false,
        };
        this._dragSelectBaseLanes = additive ? [...(this._selectedLanes || [])] : [];
        this._clearSelection();
        this._hideItemEditor();
        this._hidePromptEditor();
        const entry = this._trackLayout[headerHit?.layoutIdx];
        const ref = this._laneRefForEntry(entry);
        this._setSelectedLanes(ref ? [...this._dragSelectBaseLanes, ref] : this._dragSelectBaseLanes);
    }

    _timelineItemsInRect(rect) {
        if (!this.activeScene || !rect) return [];
        const startX = rect.kind === "items" && Number.isFinite(rect.startFrame)
            ? this._frameToX(rect.startFrame)
            : rect.startX;
        const minX = Math.min(startX, rect.currentX);
        const maxX = Math.max(startX, rect.currentX);
        const minY = Math.min(rect.startContentY, rect.currentContentY);
        const maxY = Math.max(rect.startContentY, rect.currentContentY);
        if (maxX < this._labelW) return [];
        const rulerH = this._timelineRulerHeight();
        const intersectsRow = (layoutIdx) => {
            if (layoutIdx < 0 || this._trackLayout[layoutIdx]?.collapsed) return false;
            const top = this._trackY(layoutIdx) - rulerH;
            const bottom = top + this._trackH(layoutIdx);
            return top <= maxY && bottom >= minY;
        };
        const intersectsFrames = (startFrame, endFrame) => {
            const x1 = this._frameToX(startFrame);
            const x2 = this._frameToX(endFrame);
            return x1 <= maxX && x2 >= minX;
        };
        const hits = [];
        for (const clip of (this.activeScene.clips || [])) {
            const layoutIdx = this._isMotionDriverClip(clip)
                ? this._motionDriverLaneLayoutIdx(clip.track_index || 0)
                : this._videoLaneLayoutIdx(clip.track_index || 0);
            if (intersectsRow(layoutIdx) && intersectsFrames(clip.timeline_start_frame || 0, clip.timeline_end_frame || 0)) {
                hits.push({ type: "clip", id: clip.clip_id, data: clip });
            }
        }
        for (const track of (this.activeScene.audio_tracks || [])) {
            const layoutIdx = this._audioLaneLayoutIdx(track.lane_index || 0);
            if (intersectsRow(layoutIdx) && intersectsFrames(track.timeline_start_frame || 0, track.timeline_end_frame || 0)) {
                hits.push({ type: "audio", id: track.track_id, data: track });
            }
        }
        const guideIdx = this._guidesLayoutIdx();
        if (intersectsRow(guideIdx)) {
            for (const guide of (this.activeScene.guide_frames || [])) {
                const frame = guide.frame_index === -1 ? Math.max(0, this.totalFrames - 1) : guide.frame_index;
                const x = this._frameToX(frame);
                if (x + 10 >= minX && x - 10 <= maxX) {
                    hits.push({ type: "guide", id: guide.frame_index, data: guide });
                }
            }
        }
        const promptIdx = this._promptLayoutIdx();
        if (intersectsRow(promptIdx)) {
            const sections = this.activeScene.prompt_sections || [];
            for (let i = 0; i < sections.length; i++) {
                const section = sections[i];
                if (intersectsFrames(section.start_frame || 0, section.end_frame || 0)) {
                    hits.push({ type: "prompt", id: i, data: section });
                }
            }
        }
        // Locked items are not selectable by any gesture (locked-selection rule);
        // box select skips them like background.
        return this._dedupeSelectionItems(hits.filter((hit) => !this._isItemLocked(hit)));
    }

    _lanesInContentRange(rect) {
        if (!rect) return [];
        const minY = Math.min(rect.startContentY, rect.currentContentY);
        const maxY = Math.max(rect.startContentY, rect.currentContentY);
        const rulerH = this._timelineRulerHeight();
        const lanes = [];
        for (let i = 0; i < (this._trackLayout || []).length; i++) {
            const entry = this._trackLayout[i];
            if (!this._isHeaderControllableTrackType(entry.type)) continue;
            const top = this._trackY(i) - rulerH;
            const bottom = top + this._trackH(i);
            if (top <= maxY && bottom >= minY) {
                const ref = this._laneRefForEntry(entry);
                if (ref) lanes.push(ref);
            }
        }
        return lanes;
    }

    _updateDragSelect(x, rawY) {
        const rect = this._dragSelectRect;
        if (!rect) return;
        rect.currentX = x;
        rect.currentRawY = rawY;
        if (rect.kind === "items" || rect.kind === "lanes") {
            rect.currentContentY = this._dragSelectContentY(rawY);
        }
        const verticalDistance = Math.abs(rect.currentContentY - rect.startContentY);
        if (Math.abs(rect.currentX - rect.startX) > 3 || verticalDistance > 3) {
            rect.activated = true;
        }
        if (rect.kind === "lanes") {
            this._setSelectedLanes([...(this._dragSelectBaseLanes || []), ...this._lanesInContentRange(rect)]);
            return;
        }
        if (rect.kind === "items") {
            const hits = rect.activated ? this._timelineItemsInRect(rect) : [];
            const next = rect.additive ? [...(this._dragSelectBaseItems || []), ...hits] : hits;
            this.selectedItems = this._dedupeSelectionItems(next);
            this.selectedItem = this.selectedItems[this.selectedItems.length - 1] || null;
        }
    }

    _finishDragSelect() {
        const rect = this._dragSelectRect;
        if (rect?.kind === "items" && !rect.activated && !rect.additive) {
            this._clearSelection();
        }
        this._dragSelectRect = null;
        this._dragSelectBaseItems = [];
        this._dragSelectBaseLanes = [];
        this._updateToolbar();
    }

    _drawDragSelectOverlay(ctx, width, height) {
        const rect = this._dragSelectRect;
        if (!rect) return;
        const startX = rect.kind === "items" && Number.isFinite(rect.startFrame)
            ? this._frameToX(rect.startFrame)
            : rect.startX;
        const x1 = rect.kind === "lanes" ? 0 : Math.max(this._labelW, Math.min(startX, rect.currentX));
        const x2 = rect.kind === "lanes" ? this._labelW : Math.max(startX, rect.currentX);
        const rulerH = this._timelineRulerHeight();
        const startRawY = rulerH + rect.startContentY - this.scrollY;
        const currentRawY = rulerH + rect.currentContentY - this.scrollY;
        const y1 = Math.min(startRawY, currentRawY);
        const y2 = Math.max(startRawY, currentRawY);
        if (x2 - x1 < 1 || y2 - y1 < 1) return;
        ctx.save();
        ctx.fillStyle = "rgba(99, 179, 237, 0.16)";
        ctx.strokeStyle = COLORS.accent;
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 3]);
        const drawX = Math.max(0, x1);
        const drawY = Math.max(rulerH, y1);
        const drawW = Math.max(0, Math.min(width, x2) - drawX);
        const drawH = Math.max(0, Math.min(height, y2) - drawY);
        if (drawW < 1 || drawH < 1) {
            ctx.restore();
            return;
        }
        ctx.fillRect(drawX, drawY, drawW, drawH);
        ctx.strokeRect(drawX + 0.5, drawY + 0.5, drawW, drawH);
        ctx.restore();
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
        let edgeScrollRAF = 0;
        let edgeScrollLastEvent = null;
        const horizontalEdgeScrollDragTypes = new Set(["boxSelect", "moveItem", "playhead"]);
        const verticalEdgeScrollEnabled = () => this.dragType === "boxSelect"
            || this.dragType === "laneSelect"
            || (this.dragType === "moveItem" && this._dragAnchorType === "clip");
        const edgeScrollEnabled = () => horizontalEdgeScrollDragTypes.has(this.dragType)
            || verticalEdgeScrollEnabled();
        const edgeScrollDeltaFrames = () => {
            if (!edgeScrollLastEvent || !this.isDragging || !horizontalEdgeScrollDragTypes.has(this.dragType)) return 0;
            const { x } = this._canvasMouseCoords(edgeScrollLastEvent);
            const width = canvas.width || canvas.getBoundingClientRect().width || 0;
            if (width <= this._labelW) return 0;
            const threshold = 24;
            const visibleFrames = this._visibleTimelineFrameSpan();
            const maxStep = Math.max(0.75, visibleFrames * 0.025);
            return TimelineCanvas._edgeAutoScrollDelta(x, this._labelW, width, threshold, maxStep);
        };
        const edgeScrollDeltaPixels = () => {
            if (!edgeScrollLastEvent || !this.isDragging || !verticalEdgeScrollEnabled()) return 0;
            const visibleH = this._visibleTimelineContentHeight();
            if (visibleH <= 0 || this._totalTracksHeight() <= visibleH) return 0;
            const { rawY } = this._canvasMouseCoords(edgeScrollLastEvent);
            const rulerH = this._timelineRulerHeight();
            const height = canvas.height || canvas.getBoundingClientRect().height || 0;
            if (height <= rulerH) return 0;
            const threshold = 24;
            const maxStep = Math.max(2, visibleH * 0.025);
            return TimelineCanvas._edgeAutoScrollDelta(rawY, rulerH, height, threshold, maxStep);
        };
        const stopEdgeScroll = () => {
            if (edgeScrollRAF) {
                cancelAnimationFrame(edgeScrollRAF);
                edgeScrollRAF = 0;
            }
            edgeScrollLastEvent = null;
        };
        this._timelineEdgeScrollCleanup = stopEdgeScroll;
        let onMouseMove = null;
        const edgeScrollTick = () => {
            edgeScrollRAF = 0;
            if (this._destroyed) {
                stopEdgeScroll();
                return;
            }
            const deltaFrames = edgeScrollDeltaFrames();
            const deltaPixels = edgeScrollDeltaPixels();
            if (!deltaFrames && !deltaPixels) return;
            const beforeX = this.scrollX;
            const beforeY = this.scrollY;
            this.scrollX += deltaFrames;
            this.scrollY += deltaPixels;
            this._clampScrollX();
            this._clampScrollY();
            if (this.scrollX === beforeX && this.scrollY === beforeY) return;
            if (edgeScrollLastEvent && onMouseMove) {
                onMouseMove(edgeScrollLastEvent);
            }
        };
        const updateEdgeScroll = (event) => {
            edgeScrollLastEvent = event;
            if (!this.isDragging || !edgeScrollEnabled()) {
                stopEdgeScroll();
                return;
            }
            if (!edgeScrollRAF && (edgeScrollDeltaFrames() || edgeScrollDeltaPixels())) {
                edgeScrollRAF = requestAnimationFrame(edgeScrollTick);
            }
        };

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
                // Bulk apply (#18): when the clicked lane is part of the lane
                // selection, collapse/lock/hide apply the clicked lane's NEW
                // state uniformly to every selected lane. Manage/label/rename
                // keep single-lane behavior.
                const bulkEntries = this._isLaneSelected(entry)
                    ? (this._trackLayout || []).filter((candidate) => this._isLaneSelected(candidate))
                    : [entry];
                switch (headerHit.zone) {
                    case "collapse": {
                        const nextCollapsed = !entry.collapsed;
                        for (const target of bulkEntries) target.collapsed = nextCollapsed;
                        this._persistTrackCollapseState();
                        break;
                    }
                    case "lock": {
                        this._pushUndo("toggle track lock");
                        const nextLocked = !entry.locked;
                        for (const target of bulkEntries) target.locked = nextLocked;
                        this._saveLaneConfig(bulkEntries);
                        break;
                    }
                    case "hide":
                        if (this._isLaneVisibilityControlDisabled(entry)) break;
                        {
                            const visibilityEntries = bulkEntries.filter(
                                (target) => !this._isLaneVisibilityControlDisabled(target)
                            );
                            if (!visibilityEntries.length) break;
                            this._pushUndo("toggle track visibility");
                            void this._applyHeaderVisibilityBulk(
                                visibilityEntries,
                                this._trackVisibilityState(entry) === "visible"
                            );
                        }
                        break;
                    case "manage":
                        if (entry.type === TRACK_TYPE.GUIDES) {
                            this._showGuideManagementPopup(e.clientX, e.clientY);
                        } else if (entry.type === TRACK_TYPE.PROMPT || entry.type === TRACK_TYPE.PROMPT_GLOBAL) {
                            this._showPromptManagementPanel();
                        }
                        break;
                    case "label":
                        this._startLaneDragSelect(headerHit, rawY, e);
                        break;
                }
                this._renderTimeline();
                this._renderViewportFrame();
            } else {
                // Selection is set only via I/O shortcuts — no mouse selection drag
                if (this._razorMode) {
                    // Razor mode: split clip or audio at click position
                    const hit = this._hitTestItem(x, rawY);
                    if (hit && (hit.type === "clip" || hit.type === "audio" || hit.type === "prompt")) {
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
                        const trimLimits = isPrompt ? {} : this._trimDeltaLimits(
                            edgeHit,
                            trimOrigStart,
                            trimOrigEnd,
                            trimOrigSourceIn,
                            trimOrigSourceOut,
                            trimOrigTotalSource,
                        );
                        this._trimItem = {
                            ...edgeHit,
                            origStart: trimOrigStart,
                            origEnd: trimOrigEnd,
                            origSourceIn: trimOrigSourceIn,
                            origSourceOut: trimOrigSourceOut,
                            origTotalSourceFrames: trimOrigTotalSource,
                            ...trimLimits,
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
                    // Locked items are not selectable by any gesture (locked-selection
                    // rule): clicking one is a no-op rather than a select-then-refuse.
                    if (hit && hit.type !== "prompt_global" && this._isItemLocked(hit)) {
                        return;
                    }
                    if (hit && hit.type === "prompt_global") {
                        // Global prompt lane: one non-draggable full-width item.
                        // Single click is a no-op (matching section semantics);
                        // double-click opens the inline editor.
                        this._clearSelection();
                        this._hideItemEditor();
                        this._renderTimeline();
                        return;
                    }
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
                        this.selectedItems = this._expandItemsWithLinked(this.selectedItems);
                        this.selectedItem = this._findSceneItemBySelection(hit.type, hit.id) || hit;
                        this._hideItemEditor(); // Will show on mouseup if no drag
                        // Block drag if any selected item is on a locked lane
                        const anyLocked = this.selectedItems.some(s => this._isItemLocked(s));
                        if (anyLocked) return;
                        this._pushUndo("move items"); // Capture BEFORE drag modifies data
                        this.isDragging = true;
                        this.dragType = "moveItem";
                        this._dragStartFrame = frame;
                        this._lastSnappedDelta = 0; // Track snapped delta for commit
                        this._dragLastValidDelta = 0; // Group hold delta (linked collision)
                        this._dragItemOrigStart = hit.data.timeline_start_frame ?? hit.data.start_frame ?? hit.data.frame_index ?? 0;
                        this._dragItemOrigEnd = hit.data.timeline_end_frame ?? hit.data.end_frame ?? this._dragItemOrigStart;
                        // Anchor lane/type for per-item lane-delta calculation (#15)
                        this._dragAnchorType = hit.type;
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
                        // Snapshot ALL prompt section ranges (by object reference —
                        // indices are unstable identity) for no-overlap hold-preview
                        // restore and swap commit `expected` ranges.
                        this._origAllPromptRanges = (this.activeScene?.prompt_sections || []).map((s) => ({
                            data: s,
                            start: s.start_frame || 0,
                            end: s.end_frame || 0,
                        }));
                        this._dragPromptSwap = null;
                        this._dragPromptHold = null;
                    } else {
                        // Click on empty space — deselect all
                        this._startItemDragSelect(x, rawY, e);
                    }
                }
            }
            this._renderTimeline();
        });

        onMouseMove = (e) => {
            const { x, y, rawY } = this._canvasMouseCoords(e);
            if (this.isDragging) {
                updateEdgeScroll(e);
            } else {
                stopEdgeScroll();
            }

            if (!this.isDragging) {
                const guideHit = this._hitTestGuide(x, rawY);
                if (guideHit) {
                    this._showGuideHoverPreview(guideHit.data, e.clientX, e.clientY);
                } else {
                    this._hideGuideHoverPreview();
                }
                // Prompt hover preview (sections + global item) mirrors guides
                const promptHoverHit = guideHit ? null
                    : (this._hitTestPrompt(x, rawY) || this._hitTestGlobalPrompt(x, rawY));
                if (promptHoverHit) {
                    this._showPromptHoverPreview(promptHoverHit, e.clientX, e.clientY);
                } else {
                    this._hidePromptHoverPreview();
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
            this._hidePromptHoverPreview();

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
            if (this.dragType === "boxSelect" || this.dragType === "laneSelect") {
                canvas.style.cursor = this.dragType === "laneSelect" ? "default" : "crosshair";
                this._updateDragSelect(x, rawY);
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
                    // Prompts have no source media — resize freely but clamp to
                    // neighbor boundaries (no-overlap invariant; backend 409 is
                    // the safety net for races)
                    const otherSections = (this.activeScene?.prompt_sections || [])
                        .filter((s) => s !== item.data);
                    if (item.edge === "left") {
                        let leftBound = 0;
                        for (const s of otherSections) {
                            if ((s.end_frame || 0) <= item.origStart) leftBound = Math.max(leftBound, s.end_frame || 0);
                        }
                        item.data.start_frame = Math.max(leftBound, Math.min(item.origEnd - 1, snappedFrame));
                    } else {
                        let rightBound = this.totalFrames;
                        for (const s of otherSections) {
                            if ((s.start_frame || 0) >= item.origEnd) rightBound = Math.min(rightBound, s.start_frame || 0);
                        }
                        item.data.end_frame = Math.max(item.origStart + 1, Math.min(rightBound, snappedFrame));
                    }
                } else {
                    // Clips and audio — clamp to source media bounds
                    if (item.edge === "left") {
                        const delta = Math.max(-item.origSourceIn, snappedFrame - item.origStart);
                        const newStart = Math.max(item.minStart ?? 0, Math.min(item.origEnd - 1, item.origStart + delta));
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
                        const maxEnd = Math.min(item.origEnd + tailRemaining, item.maxEnd ?? Number.POSITIVE_INFINITY);
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
                let frameDelta = snappedStart - this._dragItemOrigStart;
                // Group-bounded delta (linked-collision follow-up): every dragged
                // member shares ONE delta, so a member pinned at a scene bound
                // stops the whole group instead of letting relative offsets drift.
                for (const orig of (this._dragItemsOrig || [])) {
                    if (orig.type === "clip" || orig.type === "audio") {
                        frameDelta = Math.max(frameDelta, -orig.origStart);
                    } else if (orig.type === "guide") {
                        frameDelta = Math.max(frameDelta, -orig.origStart);
                        frameDelta = Math.min(frameDelta, (this.totalFrames - 1) - orig.origStart);
                    } else if (orig.type === "prompt") {
                        frameDelta = Math.max(frameDelta, -orig.origStart);
                        frameDelta = Math.min(frameDelta, this.totalFrames - orig.origEnd);
                    }
                }
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
                // Restore ALL prompt section ranges from the mousedown snapshot
                // (canonical baseline for the no-overlap/swap preview below)
                for (const snap of (this._origAllPromptRanges || [])) {
                    snap.data.start_frame = snap.start;
                    snap.data.end_frame = snap.end;
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
                    this._dragLastValidDelta = frameDelta;
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

                // Linked-collision hold: when clip/audio members are held at the
                // last valid frame, guides/prompts must hold WITH them — and the
                // commit delta must match the held preview, not the live cursor.
                const effectiveDelta = allFitsValid ? frameDelta : (this._dragLastValidDelta ?? 0);
                if (!allFitsValid) this._lastSnappedDelta = effectiveDelta;

                // Non-clip/audio items move per the shared effective delta
                for (const orig of (this._dragItemsOrig || [])) {
                    if (orig.type === "guide") {
                        const newIdx = Math.max(0, Math.min(this.totalFrames - 1, orig.origStart + effectiveDelta));
                        orig.data._previewFrameIndex = newIdx;
                    }
                }
                this._previewPromptDrag(effectiveDelta, x, rawY);
            }

            this._renderTimeline();
        };
        canvas.addEventListener("mousemove", onMouseMove);

        const onMouseUp = (e) => {
            if (!this.isDragging) return;
            stopEdgeScroll();
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
            } else if (wasDragType === "playhead") {
                // Ruler navigation must not finalize or clear an ephemeral In/Out
                // draft; the user may be moving the playhead before pressing the
                // opposite keyboard shortcut.
                canvas.style.cursor = "crosshair";
            } else if (wasDragType === "boxSelect" || wasDragType === "laneSelect") {
                this._finishDragSelect();
                canvas.style.cursor = "crosshair";
            } else if (wasDragType === "trimEdge" && this._trimItem) {
                // Commit trim to server
                commitPromise = this._commitTrim(this._trimItem);
                this._trimItem = null;
                canvas.style.cursor = "crosshair";
            } else if (wasDragType === "moveItem" && this.selectedItems.length > 0) {
                // Use the snapped delta (stored during mousemove), not raw mouse position
                const frameDelta = this._lastSnappedDelta || 0;

                if (frameDelta !== 0 || this._dragLaneChanged || this._dragSwapTarget || this._dragPromptSwap) {
                    commitPromise = this._commitItemMove(frameDelta);
                } else {
                    // Click without drag = show properties editor (single item only)
                    // Remove the undo entry since nothing changed
                    if (this._undoStack.length > 0) this._undoStack.pop();
                    if (this.selectedItems.length === 1) {
                        if (this.selectedItem?.type === "prompt") {
                            // Single click only SELECTS a prompt section —
                            // double-click opens the editor (B7 semantics)
                            const sections = this.activeScene?.prompt_sections || [];
                            const idx = this.selectedItem.id;
                            if (idx >= 0 && idx < sections.length) {
                                this._selectedPromptIdx = idx;
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
                this._dragLastValidDelta = null;
                this._dragAnchorType = null;
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
        canvas.addEventListener("mouseleave", () => {
            this._hideGuideHoverPreview();
            this._hidePromptHoverPreview();
        });

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

        // Drop assets onto timeline. The dragover cursor + lane highlight are a
        // best-effort landing preview: same-page gallery drags are type-aware via
        // getActiveDragAsset() (browsers block getData() during dragover); foreign
        // drags fall back to a generic media-lane highlight. The authoritative
        // accept/refuse stays in _handleAssetDrop.
        canvas.addEventListener("dragover", (e) => {
            e.preventDefault();
            e.stopPropagation(); // Prevent ComfyUI from showing its own drop indicator
            const { rawY } = this._canvasMouseCoords(e);
            const target = this._resolveDropHoverTarget(rawY);
            e.dataTransfer.dropEffect = target && target.kind !== "invalid" ? "copy" : "none";
            const prev = this._dropHoverTarget;
            if (prev?.kind !== target?.kind || prev?.layoutIdx !== target?.layoutIdx) {
                this._dropHoverTarget = target;
                this._renderTimeline();
            }
        });

        canvas.addEventListener("dragleave", () => {
            if (this._dropHoverTarget) {
                this._dropHoverTarget = null;
                this._renderTimeline();
            }
        });

        canvas.addEventListener("drop", (e) => {
            e.preventDefault();
            e.stopPropagation(); // Prevent ComfyUI from also handling this drop
            if (this._dropHoverTarget) {
                this._dropHoverTarget = null;
                this._renderTimeline();
            }
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
            const globalLayoutIdx = this._globalPromptLayoutIdx();
            if (globalLayoutIdx >= 0 && this._layoutIndexFromRawY(rawY) === globalLayoutIdx) {
                if (this._isGlobalPromptTrackLocked()) return;
                this._showGlobalPromptEditor();
                return;
            }
            const promptLayoutIdx = this._promptLayoutIdx();
            if (promptLayoutIdx >= 0 && this._layoutIndexFromRawY(rawY) === promptLayoutIdx) {
                if (this._isPromptTrackLocked()) return;
                // Double-click on an existing section EDITS it; empty lane
                // space creates (B7 — fixes dblclick-on-section warning)
                const sectionHit = this._hitTestPrompt(x, rawY);
                if (sectionHit) {
                    const idx = sectionHit.id;
                    this._selectedPromptIdx = idx;
                    this._showPromptEditor(sectionHit.data, idx);
                    this._renderTimeline();
                    return;
                }
                const frame = Math.max(0, Math.min(this.totalFrames, this._xToFrame(x)));
                this._createPromptSection(frame);
                return;
            }
            // Double-click a clip/audio/guide isolates just that item (ignoring its
            // linked group) so the inline property editor opens. Single-click keeps
            // selecting the whole linked group for group operations. Locked items
            // stay unselectable (no-op), matching the locked-selection rule.
            const itemHit = this._hitTestItem(x, rawY);
            if (
                itemHit
                && (itemHit.type === "clip" || itemHit.type === "audio" || itemHit.type === "guide")
                && !this._isItemLocked(itemHit)
            ) {
                this._selectItem(itemHit);
                this.selectedItem = this._findSceneItemBySelection(itemHit.type, itemHit.id) || itemHit;
                this._selectedPromptIdx = null;
                this._showItemEditor();
                this._renderTimeline();
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
                    // Explicit menu entry — auto-open removed (ux_patterns
                    // follow-up direction); the header ☰ icon is the primary path
                    menuItems.push({ label: "Open Guide Management", action: () => this._showGuideManagementPopup(e.clientX, e.clientY) });
                    this._showContextMenu(e.clientX, e.clientY, menuItems);
                    return;
                }
                if (entry.type === TRACK_TYPE.PROMPT || entry.type === TRACK_TYPE.PROMPT_GLOBAL) {
                    // Explicit menu action (ux_patterns follow-up direction) —
                    // do not copy the guides auto-open.
                    menuItems.push({ label: "Open Prompt Management", action: () => this._showPromptManagementPanel() });
                    if (entry.type === TRACK_TYPE.PROMPT_GLOBAL) {
                        const globalLocked = this._isGlobalPromptTrackLocked();
                        menuItems.push({
                            label: globalLocked ? "Edit Global Prompt (locked)" : "Edit Global Prompt",
                            action: globalLocked ? () => {} : () => this._showGlobalPromptEditor(),
                            disabled: globalLocked,
                        });
                    }
                    this._showContextMenu(e.clientX, e.clientY, menuItems);
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
                    const label = isVideo ? "Video" : (isMotionDriver ? "Driver" : "Audio");

                    menuItems.push({ label: "Rename Lane", action: () => this._startLaneRename(headerHit.layoutIdx) });
                    menuItems.push({ label: `Add ${label} Lane`, action: () => this._addLane(entry.type) });
                    if (this._isLaneSelected(entry) && (this._selectedLanes || []).length > 1) {
                        const selectedLaneDeletes = this._selectedLaneDeleteEntries();
                        if (selectedLaneDeletes.length > 0) {
                            menuItems.push({
                                label: `Delete ${selectedLaneDeletes.length} Selected Lane${selectedLaneDeletes.length === 1 ? "" : "s"}`,
                                action: () => this._deleteSelectedLanesAndItems(entry),
                                danger: true,
                            });
                        }
                    }
                    if (laneCount > 1) {
                        const hasItems = isVideo
                            ? (this.activeScene?.clips || []).some(c => this._isRenderClip(c) && (c.track_index || 0) === entry.laneIndex)
                            : isMotionDriver
                                ? (this.activeScene?.clips || []).some(c => this._isMotionDriverClip(c) && (c.track_index || 0) === entry.laneIndex)
                            : (this.activeScene?.audio_tracks || []).some(a => (a.lane_index || 0) === entry.laneIndex);
                        if (hasItems) {
                            menuItems.push({ label: `Delete ${label} Lane and Move Items`, action: () => this._removeLaneWithItems(entry.type, entry.laneIndex), danger: true });
                            const laneLocked = this._isLaneLocked(entry.type, entry.laneIndex);
                            menuItems.push({
                                label: laneLocked ? `Delete ${label} Lane and Items (locked)` : `Delete ${label} Lane and Items`,
                                action: laneLocked ? () => {} : () => this._removeLaneDeletingItems(entry.type, entry.laneIndex),
                                danger: true,
                                disabled: laneLocked,
                            });
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
            let hit = this._hitTestItem(x, rawY);
            // Locked items are not selectable (locked-selection rule): right-click
            // falls through to the background menu instead of selecting the item.
            // prompt_global keeps its own lock-aware menu branch below.
            if (hit && hit.type !== "prompt_global" && this._isItemLocked(hit)) {
                hit = null;
            }
            if (hit && hit.type === "prompt_global") {
                // Never enters selectedItems (bulk paths don't know the type)
                const globalLocked = this._isGlobalPromptTrackLocked();
                menuItems.push({
                    label: globalLocked ? "Edit Global Prompt (locked)" : "Edit Global Prompt",
                    action: globalLocked ? () => {} : () => this._showGlobalPromptEditor(),
                    disabled: globalLocked,
                });
                menuItems.push({ label: "Open Prompt Management", action: () => this._showPromptManagementPanel() });
                this._showContextMenu(e.clientX, e.clientY, menuItems);
                return;
            }
            if (hit) {
                if (!this._isSelected(hit.type, hit.id)) {
                    this._selectItem(hit);
                } else {
                    this._refreshSelectedHit(hit);
                }
                this._renderTimeline();

                const count = this.selectedItems.length;
                const expandedMenuItems = this._expandItemsWithLinked(this.selectedItems);
                const hasLinkedSelection = this.selectedItems.some((item) => this._isLinkedItem(item));
                const expandedDeleteCount = Math.max(count, expandedMenuItems.length);
                const itemLocked = expandedMenuItems.some((item) => this._isItemLocked(item));
                const linkableCount = this._selectedLinkableItems().length;
                if (linkableCount >= 2) {
                    menuItems.push({ label: "Link Selected Items", action: () => this._createLinkGroupFromSelection() });
                }
                if (hasLinkedSelection) {
                    menuItems.push({ label: "Select Linked Items", action: () => this._selectLinkedItemsForSelection() });
                    menuItems.push({ label: "Unlink Linked Items", action: () => this._unlinkSelectedItems() });
                }
                // Discoverable mirror of the M shortcut (linked-aware via
                // _toggleSelectedMute's own expansion + lock refusal).
                const muteCandidates = expandedMenuItems.filter((item) =>
                    item?.type === "clip" || item?.type === "audio" || item?.type === "guide" || item?.type === "prompt");
                if (muteCandidates.length > 0) {
                    const allMuted = muteCandidates.every((item) => !!item.data?.muted);
                    const muteLabel = `${allMuted ? "Unmute" : "Mute"} Selected (${muteCandidates.length})`;
                    menuItems.push({
                        label: itemLocked ? `${muteLabel} (locked)` : muteLabel,
                        action: itemLocked ? () => {} : () => void this._toggleSelectedMute(),
                        disabled: itemLocked,
                    });
                }
                const consolidateItems = this._selectedConsolidationItems(hit);
                const consolidateTargetLane = hit.type === "clip"
                    ? (hit.data.track_index || 0)
                    : hit.type === "audio" ? (hit.data.lane_index || 0) : -1;
                if (consolidateItems.length >= 2 && consolidateItems.some((item) => {
                    const lane = item.type === "clip" ? (item.data.track_index || 0) : (item.data.lane_index || 0);
                    return lane !== consolidateTargetLane;
                })) {
                    const typeLabel = hit.type === "clip" ? "Clips" : "Audio";
                    menuItems.push({
                        label: `Consolidate Selected ${typeLabel} to This Lane (${consolidateItems.length})`,
                        action: () => void this._consolidateSelectedItemsToLane(hit),
                    });
                }
                if (count > 1) {
                    const rangeItems = expandedMenuItems.filter((item) => this._selectionRangeForItem(item));
                    if (rangeItems.length > 0) {
                        menuItems.push({
                            label: `Set Selection to Selected (${rangeItems.length})`,
                            action: () => this._setSelectionToItems(expandedMenuItems),
                        });
                    }
                    const promptQueueItems = this._promptItemsForQueueBatch(expandedMenuItems);
                    if (promptQueueItems.length > 0) {
                        menuItems.push({
                            label: `Queue Prompt Sections (${promptQueueItems.length})`,
                            action: () => { this._queueSelectedPromptSections(expandedMenuItems).catch(() => {}); },
                        });
                    }
                    const deleteLabel = hasLinkedSelection ? `Delete Linked Items (${expandedDeleteCount})` : `Delete ${count} items`;
                    menuItems.push({ label: itemLocked ? `${deleteLabel} (locked)` : deleteLabel, action: itemLocked ? () => {} : () => this._deleteSelectedItems(), danger: true, disabled: itemLocked });
                } else if (hit.type === "clip") {
                    const clipAsset = this._getAssetForSourcePath(hit.data.source_path);
                    const isMotionDriverClip = this._isMotionDriverClip(hit.data);
                    const canConvertRole = isMotionDriverClip || clipAsset?.asset_type === "video";
                    menuItems.push({
                        label: itemLocked ? "Move to New Lane (locked)" : "Move to New Lane",
                        action: itemLocked ? () => {} : () => this._moveItemToNewLane(hit),
                        disabled: itemLocked,
                    });
                    menuItems.push({
                        label: !canConvertRole && !itemLocked ? "Convert to Driver (video only)" : (isMotionDriverClip ? "Convert to Render Clip" : "Convert to Driver"),
                        action: itemLocked || !canConvertRole
                            ? () => {}
                            : () => this._convertClipRole(hit.data.clip_id, isMotionDriverClip ? "render" : "motion_driver"),
                        disabled: itemLocked || !canConvertRole,
                    });
                    menuItems.push({
                        label: itemLocked ? "Replace clip with… (locked)" : "Replace clip with…",
                        action: itemLocked ? () => {} : () => this._replaceClipSource(hit.data),
                        disabled: itemLocked,
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
                    const deleteLabel = hasLinkedSelection ? `Delete Linked Items (${expandedDeleteCount})` : "Delete Clip";
                    menuItems.push({ label: itemLocked ? `${deleteLabel} (locked)` : deleteLabel, action: itemLocked ? () => {} : () => this._deleteSelectedItems(), danger: true, disabled: itemLocked });
                } else if (hit.type === "audio") {
                    menuItems.push({ label: itemLocked ? "Move to New Lane (locked)" : "Move to New Lane", action: itemLocked ? () => {} : () => this._moveItemToNewLane(hit), disabled: itemLocked });
                    menuItems.push({
                        label: itemLocked ? "Replace audio with… (locked)" : "Replace audio with…",
                        action: itemLocked ? () => {} : () => this._replaceAudioSource(hit.data),
                        disabled: itemLocked,
                    });
                    menuItems.push({
                        label: "Set Selection to Audio",
                        action: () => this._setSelectionToFrameRange(hit.data.timeline_start_frame || 0, hit.data.timeline_end_frame || 0),
                    });
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
                    const deleteLabel = hasLinkedSelection ? `Delete Linked Items (${expandedDeleteCount})` : "Delete Audio Track";
                    menuItems.push({ label: itemLocked ? `${deleteLabel} (locked)` : deleteLabel, action: itemLocked ? () => {} : () => this._deleteSelectedItems(), danger: true, disabled: itemLocked });
                } else if (hit.type === "guide") {
                    const guideAsset = this._getGuideAsset(hit.data);
                    const guidesLocked = this._isGuideTrackLocked();
                    menuItems.push({
                        label: guidesLocked ? "Replace guide with… (locked)" : "Replace guide with…",
                        action: guidesLocked ? () => {} : () => this._replaceGuideImage(hit.data, { refresh: true }),
                        disabled: guidesLocked,
                    });
                    if (guideAsset?.asset_id) {
                        menuItems.push({ label: "Inspect in Gallery", action: () => this._inspectAssetInGallery(guideAsset) });
                    }
                    if ((guideAsset?.width || 0) > 0 && (guideAsset?.height || 0) > 0) {
                        menuItems.push({
                            label: `Set Scene Aspect Ratio (${guideAsset.width}:${guideAsset.height})`,
                            action: () => this._setSceneAspectRatioFromDimensions(guideAsset.width, guideAsset.height),
                        });
                    }
                    const guideRange = this._guideHoldFrameRange(hit.data);
                    if (guideRange) {
                        menuItems.push({
                            label: "Select Guide Range",
                            action: () => this._setSelectionToFrameRange(guideRange.start, guideRange.end),
                        });
                    }
                    const guideFrame = this._resolvedGuideFrame(hit.data);
                    menuItems.push({
                        label: "Set Selection In",
                        action: () => this._setSelectionStartFrame(guideFrame),
                    });
                    menuItems.push({
                        label: "Set Selection Out",
                        action: () => this._setSelectionEndFrame(guideFrame),
                    });
                    const deleteLabel = hasLinkedSelection ? `Delete Linked Items (${expandedDeleteCount})` : "Delete Guide";
                    menuItems.push({ label: itemLocked ? `${deleteLabel} (locked)` : deleteLabel, action: itemLocked ? () => {} : () => this._deleteSelectedItems(), danger: true, disabled: itemLocked });
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
                    menuItems.push({
                        label: "Set Selection to Prompt",
                        action: () => this._setSelectionToFrameRange(sections[idx].start_frame || 0, sections[idx].end_frame || 0),
                    });
                    menuItems.push({
                        label: "Queue Prompt Section",
                        action: () => { this._queuePromptSection(sections[idx]).catch(() => {}); },
                    });
                    menuItems.push({
                        label: sections[idx].muted ? "Unmute Section" : "Mute Section",
                        action: () => {
                            this._selectItem({ type: "prompt", id: idx, data: sections[idx] });
                            void this._toggleSelectedMute();
                        },
                    });
                    menuItems.push({ label: "Open Prompt Management", action: () => this._showPromptManagementPanel() });
                    const promptLocked = this._isPromptTrackLocked();
                    const promptSelected = this.selectedItems.some((item) => item.type === "prompt" && item.id === idx);
                    const promptLinked = promptSelected && this.selectedItems.some((item) => this._isLinkedItem(item));
                    const promptExpanded = promptLinked ? this._expandItemsWithLinked(this.selectedItems) : [];
                    const promptDeleteLocked = promptLinked
                        ? promptExpanded.some((item) => this._isItemLocked(item))
                        : promptLocked;
                    const promptDeleteLabel = promptLinked
                        ? `Delete Linked Items (${Math.max(1, promptExpanded.length)})`
                        : "Delete Prompt";
                    menuItems.push({ label: promptDeleteLocked ? `${promptDeleteLabel} (locked)` : promptDeleteLabel, action: promptDeleteLocked ? () => {} : () => {
                        if (promptLinked) {
                            this._deleteSelectedItems();
                        } else if (confirm("Delete this prompt section?")) {
                            this._deletePromptSection(idx);
                        }
                    }, danger: true, disabled: promptDeleteLocked });
                }
            }

            if (menuItems.length > 0) {
                this._showContextMenu(e.clientX, e.clientY, menuItems);
            }
        });
    }

    /** Landing preview for an in-flight asset drag (zone-model rules).
     *  Type-aware when the same-page gallery stash knows the dragged asset,
     *  generic for foreign drags. Returns {kind:"ruler"} | {kind:"lane",
     *  layoutIdx} | {kind:"invalid"} | null. Advisory only — the authoritative
     *  accept/refuse lives in _handleAssetDrop. */
    _resolveDropHoverTarget(rawY) {
        if (!this.activeScene || rawY === undefined) return null;
        const dragAsset = getActiveDragAsset?.() || null;
        const assetType = dragAsset?.asset_type || "";
        if (assetType === "artifact") return { kind: "invalid" };
        const guidesTarget = () => {
            const gi = this._guidesLayoutIdx();
            if (gi < 0 || this._isGuideTrackLocked() || this._isGuideTrackCollapsed()) {
                return { kind: "invalid" };
            }
            return { kind: "lane", layoutIdx: gi };
        };
        // Images land on Guides no matter where the cursor is.
        if (assetType === "image") return guidesTarget();
        if (rawY < this._timelineRulerHeight()) {
            return { kind: "ruler" };
        }
        const layoutIdx = this._layoutIndexFromRawY(rawY);
        if (layoutIdx < 0) return { kind: "invalid" };
        const entry = this._trackLayout[layoutIdx];
        if (entry.collapsed) return { kind: "invalid" };
        const laneValid = () => !this._isLaneLocked(entry.type, entry.laneIndex || 0);
        if (!assetType) {
            // Foreign drag (cross-window / OS): generic media-lane highlight.
            const mediaLane = entry.type === TRACK_TYPE.VIDEO
                || entry.type === TRACK_TYPE.AUDIO
                || entry.type === TRACK_TYPE.MOTION_DRIVER;
            return mediaLane && laneValid() ? { kind: "lane", layoutIdx } : { kind: "invalid" };
        }
        if (assetType === "video") {
            if (entry.type === TRACK_TYPE.VIDEO || entry.type === TRACK_TYPE.MOTION_DRIVER) {
                return laneValid() ? { kind: "lane", layoutIdx } : { kind: "invalid" };
            }
            if (entry.type === TRACK_TYPE.AUDIO) {
                return dragAsset?.has_audio === true && laneValid()
                    ? { kind: "lane", layoutIdx }
                    : { kind: "invalid" };
            }
            return { kind: "invalid" };
        }
        if (assetType === "audio") {
            return entry.type === TRACK_TYPE.AUDIO && laneValid()
                ? { kind: "lane", layoutIdx }
                : { kind: "invalid" };
        }
        return { kind: "invalid" };
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

        // Zone-model drop targeting (2026-06-11, user-decided rules):
        //   ruler strip  -> ALWAYS a new lane (the only auto-lane-creation path;
        //                   dual drop video+audio only happens here);
        //   lane row     -> that lane, matching asset type only (video on an
        //                   audio lane places its extracted audio);
        //   anything else-> refuse with an informational toast.
        // No implicit lane defaults — placement is always user-expressed.
        let dropZone = "dead"; // "ruler" | "lane" | "dead"
        let dropEntry = null;
        let targetMotionDriverLane = -1;
        if (trackRawY !== undefined) {
            if (trackRawY < this._timelineRulerHeight()) {
                dropZone = "ruler";
            } else {
                const layoutIdx = this._layoutIndexFromRawY(trackRawY);
                if (layoutIdx >= 0) {
                    const entry = this._trackLayout[layoutIdx];
                    // #33: reject drops onto any collapsed non-image destination lane.
                    // Image drops were checked against Guides above; for video/audio/
                    // driver we use the lane the cursor is over.
                    if (asset.asset_type !== "image" && entry.collapsed) {
                        return;
                    }
                    dropZone = "lane";
                    dropEntry = entry;
                    if (entry.type === TRACK_TYPE.MOTION_DRIVER) targetMotionDriverLane = entry.laneIndex;
                }
            }
        }

        if (targetMotionDriverLane >= 0) {
            if (asset.asset_type !== "video") {
                this._showToast("Driver lanes accept video assets only.");
                return;
            }
            if (this._isLaneLocked(TRACK_TYPE.MOTION_DRIVER, targetMotionDriverLane)) {
                this._showToast("Target lane is locked.");
                return;
            }
            if (this._driverClipInLane(targetMotionDriverLane)) {
                this._showToast("Only one driver clip is allowed per driver lane.");
                return;
            }
            this._pushUndo("add driver");
            const assetObj = this._findAssetById(asset.asset_id);
            const dropDuration = assetObj ? this._mediaTimelineFrames(assetObj) : 30;
            const dropEnd = frame + dropDuration;
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
                fit_mode: this._defaultFitMode(),
                crop_position: this._defaultCropPosition(),
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
                        fit_mode: this._defaultFitMode(),
                        crop_position: this._defaultCropPosition(),
                    }),
                });
                if (!resp.ok) {
                    const message = await readResponseError(resp, `Driver clip creation failed: ${resp.status}`);
                    console.warn("[Sonder] Driver clip creation failed:", resp.status, message);
                    notifyError(message, { source: "timeline-drop" });
                    this._discardLastUndo("add driver");
                    await this._fetchScenes({ ignoreMutationGate: true, reason: "drop_motion_driver_error" });
                    return;
                }
                const createdClip = await resp.json();
                const clipIdx = (this.activeScene.clips || []).findIndex((clip) => clip.clip_id === tempClipId);
                if (clipIdx >= 0) this.activeScene.clips[clipIdx] = createdClip;
                this._renderSceneAfterLocalMutation();
                this._deferProjectBackedRefresh(["scenes"], "motion_driver_drop_reconcile");
            } catch (e) {
                this._discardLastUndo("add driver");
                await this._fetchScenes({ ignoreMutationGate: true, reason: "drop_motion_driver_error" });
                console.warn("[Sonder] Failed to drop driver:", e);
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

        // Zone-model target resolution for media drops (images keep their
        // guides-only routing in the POST branches below).
        let targetVideoLane = -1;
        let targetAudioLane = -1;
        let dualDrop = false;
        let audioFromVideo = false;
        const laneCountFields = {};
        if (asset.asset_type === "video" || asset.asset_type === "audio") {
            const assetObjForZone = _findAsset(asset.asset_id);
            const videoHasAudio = asset.asset_type === "video"
                && (assetObjForZone?.has_audio === true || asset?.has_audio === true);
            const dropFps = this._effectiveFps;
            const dropFrames = asset.asset_type === "video"
                ? this._mediaTimelineFrames(assetObjForZone || asset)
                : Math.max(1, Math.round((assetObjForZone?.duration_sec || asset.duration_sec || 1) * dropFps));
            const dropEndFrame = frame + dropFrames;
            const laneHasOverlap = (laneType, laneIndex) => {
                if (laneType === TRACK_TYPE.VIDEO) {
                    return (this.activeScene.clips || []).some(c =>
                        this._isRenderClip(c) && (c.track_index || 0) === laneIndex &&
                        c.timeline_start_frame < dropEndFrame && c.timeline_end_frame > frame);
                }
                return (this.activeScene.audio_tracks || []).some(a =>
                    (a.lane_index || 0) === laneIndex &&
                    a.timeline_start_frame < dropEndFrame && a.timeline_end_frame > frame);
            };

            if (dropZone === "ruler") {
                if (asset.asset_type === "video") {
                    const newVideoCount = (this.activeScene.video_lane_count || 1) + 1;
                    targetVideoLane = newVideoCount - 1;
                    laneCountFields.video_lane_count = newVideoCount;
                    if (videoHasAudio) {
                        const newAudioCount = (this.activeScene.audio_lane_count || 1) + 1;
                        targetAudioLane = newAudioCount - 1;
                        laneCountFields.audio_lane_count = newAudioCount;
                        dualDrop = true;
                    }
                } else {
                    const newAudioCount = (this.activeScene.audio_lane_count || 1) + 1;
                    targetAudioLane = newAudioCount - 1;
                    laneCountFields.audio_lane_count = newAudioCount;
                }
            } else if (dropZone === "lane" && dropEntry?.type === TRACK_TYPE.VIDEO) {
                if (asset.asset_type === "audio") {
                    this._showToast("Audio assets need an audio lane — drop on one, or on the ruler to add a lane.");
                    return;
                }
                if (this._isLaneLocked(TRACK_TYPE.VIDEO, dropEntry.laneIndex)) {
                    this._showToast("Target lane is locked.");
                    return;
                }
                if (laneHasOverlap(TRACK_TYPE.VIDEO, dropEntry.laneIndex)) {
                    this._showToast("Overlaps items on this lane — drop on the ruler to create a new lane.");
                    return;
                }
                targetVideoLane = dropEntry.laneIndex;
                if (videoHasAudio) {
                    notifyInfo("Placed video only — drop on the ruler to also bring its audio.", { source: "timeline-drop-zone" });
                }
            } else if (dropZone === "lane" && dropEntry?.type === TRACK_TYPE.AUDIO) {
                if (this._isLaneLocked(TRACK_TYPE.AUDIO, dropEntry.laneIndex)) {
                    this._showToast("Target lane is locked.");
                    return;
                }
                if (asset.asset_type === "video") {
                    if (!videoHasAudio) {
                        this._showToast("This video has no embedded audio to place.");
                        return;
                    }
                    audioFromVideo = true;
                }
                if (laneHasOverlap(TRACK_TYPE.AUDIO, dropEntry.laneIndex)) {
                    this._showToast("Overlaps items on this lane — drop on the ruler to create a new lane.");
                    return;
                }
                targetAudioLane = dropEntry.laneIndex;
            } else {
                // Guides/Prompt rows and dead space are not media drop targets.
                this._showToast("Drop on a matching lane, or on the ruler to create a new lane.");
                return;
            }
        }

        this._pushUndo("add asset");

        // Drop mutations go through the versioned mutation queue for
        // serialization, fresh If-Match versions, and one-shot 409 retry.
        // Each `run` resolves an {ok, payload|error} sentinel instead of
        // rejecting: the queue's rejection handler would toast and defer its
        // own scenes refresh, but this handler owns drop failure surfacing
        // (incl. the lane PUT's deliberate silent revert) — do not "fix" the
        // sentinel into a rejection or failures will double-toast.
        // Per-drop key token + coalesce:false so rapid drops never coalesce.
        const dropSeq = `${Date.now().toString(36)}-${Math.random().toString(16).slice(2, 8)}`;
        const queueDropMutation = (keySuffix, label, path, init) => this._queueProjectMutation({
            key: `scene:${this.activeSceneId}:drop:${dropSeq}:${keySuffix}`,
            label,
            coalesce: false,
            refreshScenes: false,
            run: async () => {
                try {
                    const result = await this._runVersionedProjectMutation(path, init, { projectId: dirName });
                    return { ok: true, payload: result?.payload };
                } catch (error) {
                    return { ok: false, error };
                }
            },
        });

        const persistSceneLaneCounts = async (fields, reason) => {
            const outcome = await queueDropMutation(
                "lanes",
                "drop lane counts",
                `/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}`,
                {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(fields),
                },
            );
            if (outcome?.ok) return true;
            console.warn("[Sonder] Auto-add lane failed:", outcome?.error?.status, outcome?.error?.message || outcome?.error);
            this._discardLastUndo("add asset");
            await this._fetchScenes({ ignoreMutationGate: true, reason });
            return false;
        };

        // Ruler-zone drops create their new lane(s) optimistically, then persist
        // the counts before item creation (the only auto-lane-creation path).
        if (Object.keys(laneCountFields).length > 0) {
            if (laneCountFields.video_lane_count) {
                this.activeScene.video_lane_count = laneCountFields.video_lane_count;
            }
            if (laneCountFields.audio_lane_count) {
                this.activeScene.audio_lane_count = laneCountFields.audio_lane_count;
            }
            this._buildTrackLayout();
            this._renderTimeline();
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
                const guideFields = this._seedFitDefaults({
                    guide_id: this._newLocalItemId("guide"),
                    frame_index: frame,
                    asset_id: asset.asset_id,
                    source: "asset",
                    strength: this._defaultGuideStrength(),
                });
                this._applyLocalCreateGuide(guideFields);
                this._renderSceneAfterLocalMutation();
                resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}/guides`), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(guideFields),
                });
                if (!resp.ok) {
                    const message = await readResponseError(resp, `Guide creation failed: ${resp.status}`);
                    console.warn("[Sonder] Guide creation failed:", resp.status, message);
                    notifyError(message, { source: "timeline-drop" });
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
            } else if (asset.asset_type === "video" && !audioFromVideo) {
                // Drop video = create clip on target video lane (+ paired audio
                // only for ruler-zone dual drops)
                const assetObj = _findAsset(asset.asset_id);
                droppedVideoHasAudio = dualDrop;
                const frameCount = this._mediaTimelineFrames(assetObj || asset);
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
                    fit_mode: this._defaultFitMode(),
                    crop_position: this._defaultCropPosition(),
                };
                this.activeScene.clips = this.activeScene.clips || [];
                this.activeScene.clips.push(optimisticClip);
                if (dualDrop) {
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
                    audio_lane_index: dualDrop ? targetAudioLane : 0,
                    dual_drop: dualDrop,
                    link_video_audio: this._settings?.timelineBehavior?.linkedVideoAudioDrop !== false,
                    fit_mode: this._defaultFitMode(),
                    crop_position: this._defaultCropPosition(),
                };
                const clipOutcome = await queueDropMutation(
                    "clip",
                    "drop clip",
                    `/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}/clips`,
                    {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(clipBody),
                    },
                );
                if (!clipOutcome?.ok) {
                    const error = clipOutcome?.error;
                    const message = error?.message || `Clip creation failed: ${error?.status || ""}`;
                    console.warn("[Sonder] Clip creation failed:", error?.status, message);
                    notifyError(message, { source: "timeline-drop" });
                    this._discardLastUndo("add asset");
                    await this._fetchScenes({ ignoreMutationGate: true, reason: "drop_clip_error" });
                    return;
                }
                const clipPayload = clipOutcome.payload;
                const { audio_track: createdAudioTrack, ...createdClip } = clipPayload || {};
                const clipIdx = (this.activeScene.clips || []).findIndex((clip) => clip.clip_id === optimisticClipId);
                if (clipIdx >= 0) {
                    this.activeScene.clips[clipIdx] = createdClip;
                }
                if (optimisticAudioId) {
                    if (createdAudioTrack) {
                        const audioIdx = (this.activeScene.audio_tracks || []).findIndex((track) => track.track_id === optimisticAudioId);
                        if (audioIdx >= 0) this.activeScene.audio_tracks[audioIdx] = createdAudioTrack;
                        if (this._settings?.timelineBehavior?.linkedVideoAudioDrop !== false) {
                            this.activeScene.linked_item_groups = (this.activeScene.linked_item_groups || [])
                                .filter((group) => !(group?.group_id || "").startsWith("temp-drop-"));
                            this.activeScene.linked_item_groups.push({
                                group_id: `temp-drop-${Date.now().toString(36)}`,
                                items: [
                                    { type: "clip", id: createdClip.clip_id },
                                    { type: "audio", id: createdAudioTrack.track_id },
                                ],
                            });
                        }
                    } else {
                        this.activeScene.audio_tracks = (this.activeScene.audio_tracks || []).filter((track) => track.track_id !== optimisticAudioId);
                    }
                }
                this._renderSceneAfterLocalMutation();
            } else if (asset.asset_type === "audio" || audioFromVideo) {
                // Drop audio = create audio track on target audio lane.
                // audioFromVideo: a video asset dropped on an audio lane places
                // ONLY its extracted audio (zone-model rule); the backend derives
                // the audio asset from the video.
                const assetObj = _findAsset(asset.asset_id);
                const durationFrames = this._mediaTimelineFrames(assetObj || asset);
                if (audioFromVideo) {
                    droppedVideoHasAudio = true; // extraction registers a derived audio asset
                }
                optimisticAudioId = `temp-audio-${Date.now().toString(36)}-${Math.random().toString(16).slice(2, 8)}`;
                const optimisticAudio = {
                    track_id: optimisticAudioId,
                    source_path: audioFromVideo ? "" : (assetObj?.path || asset.path || ""),
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
                const audioOutcome = await queueDropMutation(
                    "audio",
                    "drop audio track",
                    `/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}/audio_tracks`,
                    {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            asset_id: asset.asset_id,
                            timeline_start_frame: frame,
                            lane_index: targetAudioLane,
                        }),
                    },
                );
                if (!audioOutcome?.ok) {
                    const error = audioOutcome?.error;
                    const message = error?.message || `Audio track creation failed: ${error?.status || ""}`;
                    console.warn("[Sonder] Audio track creation failed:", error?.status, message);
                    notifyError(message, { source: "timeline-drop" });
                    this._discardLastUndo("add asset");
                    await this._fetchScenes({ ignoreMutationGate: true, reason: "drop_audio_error" });
                    return;
                }
                const audioPayload = audioOutcome.payload;
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

    _driverClipInLane(laneIndex, excludeClipId = "") {
        return (this.activeScene?.clips || []).some((clip) =>
            this._isMotionDriverClip(clip)
            && (clip.track_index || 0) === laneIndex
            && (!excludeClipId || clip.clip_id !== excludeClipId)
        );
    }

    _hasDriverLaneCollision() {
        const occupied = new Map();
        for (const clip of (this.activeScene?.clips || [])) {
            if (!this._isMotionDriverClip(clip)) continue;
            const laneIndex = clip.track_index || 0;
            if (occupied.has(laneIndex)) return true;
            occupied.set(laneIndex, clip.clip_id || "");
        }
        return false;
    }

    _firstEmptyUnlockedDriverLane() {
        const count = Math.max(1, parseInt(this.activeScene?.motion_driver_lane_count, 10) || 1);
        for (let i = 0; i < count; i++) {
            if (this._isLaneHidden(TRACK_TYPE.MOTION_DRIVER, i)) continue;
            if (this._isLaneLocked(TRACK_TYPE.MOTION_DRIVER, i)) continue;
            if (!this._driverClipInLane(i)) return i;
        }
        return count;
    }

    async _convertClipRole(clipId, targetRole) {
        if (!this.activeScene || !this.projectDir || !clipId) return;
        const clip = (this.activeScene.clips || []).find(c => c.clip_id === clipId);
        if (!clip) return;
        if (targetRole === "motion_driver") {
            const sourceAsset = this._getAssetForSourcePath(clip.source_path);
            if (sourceAsset?.asset_type !== "video") {
                this._showToast("Driver lanes accept video assets only.");
                return;
            }
        }
        const currentDriverLaneCount = Math.max(1, parseInt(this.activeScene.motion_driver_lane_count, 10) || 1);
        const targetLane = targetRole === "motion_driver"
            ? this._firstEmptyUnlockedDriverLane()
            : (this.activeScene.video_lane_count || 1);
        const nextCount = targetRole === "motion_driver"
            ? Math.max(currentDriverLaneCount, targetLane + 1)
            : targetLane + 1;
        const laneType = targetRole === "motion_driver" ? "motion_driver" : "video";

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
        this._applyLocalSetLaneCount(laneType, nextCount);
        Object.assign(clip, body);
        try {
            await this._runSceneMutation(
                [
                    { type: "set_lane_count", lane_type: laneType, count: nextCount },
                    { type: "update_clip", clip_id: clipId, fields: body },
                ],
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
            await this._fetchScenes({ ignoreMutationGate: true, reason: "convert_clip_role_error" });
            console.warn("[Sonder] Failed to convert clip role:", e);
            notifyError(e?.message || "Failed to convert clip role.");
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
                const isDriver = this._isMotionDriverClip(hit.data);
                const laneType = isDriver ? "motion_driver" : "video";
                const currentCount = isDriver
                    ? (this.activeScene.motion_driver_lane_count || 1)
                    : (this.activeScene.video_lane_count || 1);
                const newCount = currentCount + 1;
                const newLane = newCount - 1;
                operations.push({ type: "set_lane_count", lane_type: laneType, count: newCount });
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
        if (this._animaticMode && type === TRACK_TYPE.VIDEO) return true;
        const idx = type === TRACK_TYPE.VIDEO
            ? this._videoLaneLayoutIdx(laneIndex)
            : type === TRACK_TYPE.MOTION_DRIVER
                ? this._motionDriverLaneLayoutIdx(laneIndex)
                : this._audioLaneLayoutIdx(laneIndex);
        return idx >= 0 && this._trackLayout[idx]?.hidden;
    }

    _muteOperationForItem(item, muted, applyLinked = false) {
        if (!item?.data) return null;
        if (item.type === "clip") {
            return { type: "update_clip", clip_id: item.data.clip_id || item.id, fields: { muted: !!muted }, apply_linked: applyLinked };
        }
        if (item.type === "audio") {
            return { type: "update_audio_track", track_id: item.data.track_id || item.id, fields: { muted: !!muted }, apply_linked: applyLinked };
        }
        if (item.type === "guide") {
            return {
                type: "update_guide",
                frame_index: item.id,
                expected: {
                    frame_index: item.data.frame_index ?? item.id,
                    asset_id: item.data.asset_id || "",
                    guide_id: item.data.guide_id || "",
                },
                fields: { muted: !!muted },
                apply_linked: applyLinked,
            };
        }
        if (item.type === "prompt") {
            return {
                type: "update_prompt_section",
                index: item.id,
                expected: {
                    start_frame: item.data.start_frame,
                    end_frame: item.data.end_frame,
                    prompt_id: item.data.prompt_id || "",
                },
                fields: { muted: !!muted },
                apply_linked: applyLinked,
            };
        }
        return null;
    }

    _buildLinkedMuteOperations(items, muted) {
        const expanded = this._expandItemsWithLinked(items)
            .filter((item) => item?.type === "clip" || item?.type === "audio" || item?.type === "guide" || item?.type === "prompt");
        if (expanded.some((item) => this._isItemLocked(item))) {
            return { operations: [], targets: expanded, locked: true };
        }
        const operations = [];
        const emittedGroups = new Set();
        for (const item of expanded) {
            const group = this._linkGroupForItem(item);
            if (group) {
                const groupKey = group.group_id || this._linkRefKey(this._linkRefForItem(item));
                if (emittedGroups.has(groupKey)) continue;
                emittedGroups.add(groupKey);
            }
            const operation = this._muteOperationForItem(item, muted, !!group);
            if (operation) operations.push(operation);
        }
        return { operations, targets: expanded, locked: false };
    }

    async _toggleHeaderVisibility(entry) {
        if (!entry) return;
        if (this._isLaneVisibilityControlDisabled(entry)) return;
        await this._applyHeaderVisibilityBulk([entry], this._trackVisibilityState(entry) === "visible");
    }

    _isLaneVisibilityControlDisabled(entry) {
        return !!this._animaticMode && entry?.type === TRACK_TYPE.VIDEO;
    }

    /** Apply one uniform header visibility command. Actual lane-hidden state is
     *  lane-local; an inferred muted/partial state performs linked-aware unmute. */
    async _applyHeaderVisibilityBulk(entries, nextHidden) {
        const targets = (entries || []).filter(
            (entry) => entry && !this._isLaneVisibilityControlDisabled(entry)
        );
        if (!targets.length) return;
        const visibilitySeq = (this._headerVisibilitySeq || 0) + 1;
        this._headerVisibilitySeq = visibilitySeq;
        const laneConfigTargets = [];
        const unmuteSeeds = [];
        for (const target of targets) {
            if (nextHidden) {
                if (!target.hidden) laneConfigTargets.push(target);
            } else {
                if (target.hidden) {
                    laneConfigTargets.push(target);
                } else if (this._trackVisibilityState(target) !== "visible") {
                    const type = target.type === TRACK_TYPE.AUDIO ? "audio"
                        : target.type === TRACK_TYPE.GUIDES ? "guide"
                            : target.type === TRACK_TYPE.PROMPT ? "prompt" : "clip";
                    for (const data of this._trackItemsForEntry(target)) {
                        if (!data?.muted) continue;
                        const id = type === "audio" ? data.track_id
                            : type === "guide" ? data.frame_index
                                : type === "prompt" ? (this.activeScene?.prompt_sections || []).indexOf(data)
                                    : data.clip_id;
                        if (id !== undefined && id !== null && id !== -1) unmuteSeeds.push({ type, id, data });
                    }
                }
            }
        }

        const mutePlan = this._buildLinkedMuteOperations(unmuteSeeds, false);
        if (mutePlan.locked) {
            this._discardLastUndo("toggle track visibility");
            notifyWarning("Linked unmute refused because one or more linked items are locked.", { source: "timeline-mute-refused" });
            return;
        }

        const operations = [];
        const sceneRef = this.activeScene;
        for (const target of laneConfigTargets) {
            target.hidden = !!nextHidden;
            const laneType = this._laneTypeForEntry(target);
            if (!laneType) continue;
            const laneIndex = target.laneIndex || 0;
            const fields = {
                name: target.customName || "",
                color: target.color || "",
                locked: !!target.locked,
                hidden: !!target.hidden,
            };
            operations.push({ type: "update_lane_config", lane_type: laneType, lane_index: laneIndex, fields });
            const cfg = { ...fields };
            if (laneType === "guide") sceneRef.guide_track_config = cfg;
            else if (laneType === "prompt") sceneRef.prompt_track_config = cfg;
            else if (laneType === "prompt_global") sceneRef.global_prompt_track_config = cfg;
            else {
                const listKey = laneType === "video" ? "video_lane_configs"
                    : laneType === "motion_driver" ? "motion_driver_lane_configs" : "audio_lane_configs";
                const list = Array.isArray(sceneRef[listKey]) ? sceneRef[listKey] : [];
                while (list.length <= laneIndex) list.push(this._defaultLaneConfig());
                list[laneIndex] = cfg;
                sceneRef[listKey] = list;
            }
        }
        operations.push(...mutePlan.operations);
        if (!operations.length) {
            this._discardLastUndo("toggle track visibility");
            return;
        }

        for (const item of mutePlan.targets) item.data.muted = false;
        this._clearPlaybackWarmOverlay("lane-visibility-change", { render: false });
        try {
            await this._runSceneMutation(operations, {
                key: `scene:${this.activeSceneId}:header-visibility`,
                label: "toggle track visibility",
                coalesce: false,
                reconcileFromResult: (result) => {
                    if (visibilitySeq !== this._headerVisibilitySeq) return true;
                    return this._reconcileActiveSceneFromMutation(result, { reason: "toggle track visibility" });
                },
            });
            this._reconcileSelection();
            this._buildTrackLayout();
        } catch (error) {
            if (visibilitySeq === this._headerVisibilitySeq) {
                this._discardLastUndo("toggle track visibility");
                await this._fetchScenes({ ignoreMutationGate: true, reason: "header_visibility_error" });
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
                        ? ((this.activeScene?.motion_driver_lane_count || 1) > 1 ? `Driver ${entry.laneIndex + 1}` : "Driver")
                        : ((this.activeScene?.audio_lane_count || 1) > 1 ? `A${entry.laneIndex + 1}` : "Audio"));
                this._saveLaneConfig([entry]);
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

    /** Backend lane_type for a _trackLayout entry (note GUIDES is "guides"
     *  frontend-side but "guide" backend-side). */
    _laneTypeForEntry(entry) {
        if (entry?.type === TRACK_TYPE.GUIDES) return "guide";
        if (entry?.type === TRACK_TYPE.PROMPT) return "prompt";
        if (entry?.type === TRACK_TYPE.PROMPT_GLOBAL) return "prompt_global";
        if (entry?.type === TRACK_TYPE.VIDEO || entry?.type === TRACK_TYPE.AUDIO || entry?.type === TRACK_TYPE.MOTION_DRIVER) {
            return entry.type;
        }
        return "";
    }

    /** Persist lane configs (lock/hide/name) for the CHANGED entries only.
     *  Per-lane delta ops (mutation-integrity F1): a user action can only ever
     *  write the lanes it touched, so a transiently-reverted neighbor entry can
     *  never be persisted by an unrelated toggle (the full-snapshot writes were
     *  the rapid-toggle data-loss amplifier). */
    async _saveLaneConfig(changedEntries) {
        if (!this.activeScene || !this.projectDir) return;
        const entries = (Array.isArray(changedEntries) ? changedEntries : [changedEntries]).filter(Boolean);
        if (!entries.length) return;
        const sceneId = this.activeSceneId;
        const sceneRef = this.activeScene;
        const operations = [];
        for (const e of entries) {
            const laneType = this._laneTypeForEntry(e);
            if (!laneType) continue;
            const laneIndex = e.laneIndex || 0;
            const fields = { name: e.customName || "", color: e.color || "", locked: !!e.locked, hidden: !!e.hidden };
            operations.push({ type: "update_lane_config", lane_type: laneType, lane_index: laneIndex, fields });
            // Optimistic per-lane scene write (icon-flicker fix, now scoped):
            // _buildTrackLayout re-derives icon state from scene configs, so any
            // rebuild during the in-flight window must already see the new value.
            if (sceneRef) {
                const cfg = { ...fields };
                if (laneType === "guide") {
                    sceneRef.guide_track_config = cfg;
                } else if (laneType === "prompt") {
                    sceneRef.prompt_track_config = cfg;
                } else if (laneType === "prompt_global") {
                    sceneRef.global_prompt_track_config = cfg;
                } else {
                    const listKey = laneType === "video" ? "video_lane_configs"
                        : laneType === "motion_driver" ? "motion_driver_lane_configs"
                            : "audio_lane_configs";
                    const list = Array.isArray(sceneRef[listKey]) ? sceneRef[listKey] : [];
                    while (list.length <= laneIndex) list.push(this._defaultLaneConfig());
                    list[laneIndex] = cfg;
                    sceneRef[listKey] = list;
                }
            }
        }
        if (!operations.length) return;
        // Coalesce burst toggles by (lane_type, lane_index): each op carries the
        // lane's full 4-field config, so latest-wins per lane and union across
        // lanes is exactly right — no cross-lane loss, no field merging needed.
        const merge = (oldIntent, nextIntent) => {
            const byLane = new Map();
            for (const op of [...(oldIntent?.operations || []), ...(nextIntent?.operations || [])]) {
                byLane.set(`${op.lane_type}:${op.lane_index || 0}`, op);
            }
            return { ...nextIntent, operations: [...byLane.values()] };
        };
        try {
            await this._runSceneMutation(operations, {
                key: `scene:${sceneId}:lane-config`,
                label: "lane config",
                coalesce: true,
                merge,
                refreshScenes: false,
            });
        } catch (e) {
            console.warn("[Sonder] Failed to save lane config:", e);
            await this._fetchScenes({ ignoreMutationGate: true, reason: "lane_config_error" });
            this._buildTrackLayout();
            this._renderTimeline();
        }
    }

    async _addLane(trackType) {
        if (!this.activeScene || !this.projectDir) return;
        const isVideo = trackType === TRACK_TYPE.VIDEO;
        const isDriver = trackType === TRACK_TYPE.MOTION_DRIVER;
        const laneType = isVideo ? "video" : (isDriver ? "motion_driver" : "audio");
        const nextCount = isVideo
            ? (this.activeScene.video_lane_count || 1) + 1
            : isDriver
                ? (this.activeScene.motion_driver_lane_count || 1) + 1
            : (this.activeScene.audio_lane_count || 1) + 1;
        const undoLabel = "add lane";
        this._pushUndo(undoLabel);
        this._applyLocalSetLaneCount(laneType, nextCount);
        this._renderSceneAfterLocalMutation({ viewport: false });
        try {
            await this._runSceneMutation(
                [{ type: "set_lane_count", lane_type: laneType, count: nextCount }],
                {
                    key: `scene:${this.activeSceneId}:${laneType}-lane-count`,
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

    _laneTypeFromTrackType(trackType) {
        if (trackType === TRACK_TYPE.VIDEO) return "video";
        if (trackType === TRACK_TYPE.MOTION_DRIVER) return "motion_driver";
        if (trackType === TRACK_TYPE.AUDIO) return "audio";
        return "";
    }

    _laneLabelFromTrackType(trackType) {
        if (trackType === TRACK_TYPE.VIDEO) return "video";
        if (trackType === TRACK_TYPE.MOTION_DRIVER) return "driver";
        if (trackType === TRACK_TYPE.AUDIO) return "audio";
        return "lane";
    }

    _laneItemsForTrackType(trackType, laneIndex) {
        if (!this.activeScene) return [];
        if (trackType === TRACK_TYPE.VIDEO) {
            return (this.activeScene.clips || []).filter((clip) => this._isRenderClip(clip) && (clip.track_index || 0) === laneIndex);
        }
        if (trackType === TRACK_TYPE.MOTION_DRIVER) {
            return (this.activeScene.clips || []).filter((clip) => this._isMotionDriverClip(clip) && (clip.track_index || 0) === laneIndex);
        }
        if (trackType === TRACK_TYPE.AUDIO) {
            return (this.activeScene.audio_tracks || []).filter((track) => (track.lane_index || 0) === laneIndex);
        }
        return [];
    }

    _mediaItemsOverlap(left, right) {
        return (left?.timeline_start_frame || 0) < (right?.timeline_end_frame || 0)
            && (left?.timeline_end_frame || 0) > (right?.timeline_start_frame || 0);
    }

    _laneMoveItemsRefusal(trackType, sourceLane, targetLane) {
        if (this._isLaneLocked(trackType, sourceLane) || this._isLaneLocked(trackType, targetLane)) {
            return "Move refused because the source or destination lane is locked.";
        }
        const moving = this._laneItemsForTrackType(trackType, sourceLane);
        const destination = this._laneItemsForTrackType(trackType, targetLane);
        for (let index = 0; index < moving.length; index++) {
            for (let otherIndex = index + 1; otherIndex < moving.length; otherIndex++) {
                if (this._mediaItemsOverlap(moving[index], moving[otherIndex])) {
                    return "Move refused because items on the source lane overlap.";
                }
            }
            if (destination.some((item) => this._mediaItemsOverlap(moving[index], item))) {
                return "Move refused because items overlap the destination lane.";
            }
        }
        return "";
    }

    _selectedConsolidationItems(hit) {
        if (!hit || (hit.type !== "clip" && hit.type !== "audio")) return [];
        if (hit.type === "clip" && !this._isRenderClip(hit.data)) return [];
        const seen = new Set();
        const items = [];
        for (const selected of this.selectedItems || []) {
            if (selected?.type !== hit.type) continue;
            const current = this._findSceneItemBySelection(selected.type, selected.id) || selected;
            if (current.type === "clip" && !this._isRenderClip(current.data)) continue;
            const key = String(current.id);
            if (seen.has(key)) continue;
            seen.add(key);
            items.push(current);
        }
        return items;
    }

    _consolidationRefusal(items, hit) {
        if (items.length < 2) return "Select at least two items of the same type.";
        const trackType = hit.type === "clip" ? TRACK_TYPE.VIDEO : TRACK_TYPE.AUDIO;
        const targetLane = hit.type === "clip" ? (hit.data.track_index || 0) : (hit.data.lane_index || 0);
        const itemIds = new Set(items.map((item) => String(item.id)));
        const sourceLanes = new Set(items.map((item) => item.type === "clip"
            ? (item.data.track_index || 0)
            : (item.data.lane_index || 0)));
        if (sourceLanes.size === 1 && sourceLanes.has(targetLane)) {
            return "Selected items are already on this lane.";
        }
        for (const lane of new Set([...sourceLanes, targetLane])) {
            if (this._isLaneLocked(trackType, lane)) {
                return "Consolidation refused because a source or destination lane is locked.";
            }
        }
        for (let index = 0; index < items.length; index++) {
            for (let otherIndex = index + 1; otherIndex < items.length; otherIndex++) {
                if (this._mediaItemsOverlap(items[index].data, items[otherIndex].data)) {
                    return "Selected items overlap and cannot share one lane.";
                }
            }
        }
        const destination = this._laneItemsForTrackType(trackType, targetLane)
            .filter((item) => !itemIds.has(String(item.clip_id || item.track_id)));
        for (const item of items) {
            if (destination.some((other) => this._mediaItemsOverlap(item.data, other))) {
                return "Selected items overlap existing items on this lane.";
            }
        }
        return "";
    }

    async _consolidateSelectedItemsToLane(hit) {
        if (!this.activeScene || !this.projectDir) return;
        const items = this._selectedConsolidationItems(hit);
        const refusal = this._consolidationRefusal(items, hit);
        if (refusal) {
            notifyWarning(refusal, { source: "timeline-consolidate-refused" });
            return;
        }
        const laneType = hit.type === "clip" ? "video" : "audio";
        const targetLane = hit.type === "clip" ? (hit.data.track_index || 0) : (hit.data.lane_index || 0);
        const selectionSnapshot = (this.selectedItems || []).map((item) => ({ type: item.type, id: item.id }));
        const selectedItemSnapshot = this.selectedItem ? { type: this.selectedItem.type, id: this.selectedItem.id } : null;
        const undoLabel = "consolidate items";
        this._pushUndo(undoLabel);
        try {
            await this._runSceneMutation([{
                type: "consolidate_items",
                lane_type: laneType,
                item_ids: items.map((item) => String(item.id)),
                target_lane: targetLane,
                remove_vacated_lanes: true,
            }], {
                key: `scene:${this.activeSceneId}:consolidate:${laneType}:${Date.now()}`,
                label: "consolidate items",
                coalesce: false,
                failureMessage: (error) => error?.message || "Consolidation failed — timeline restored.",
                failureTier: "warning",
            });
            this.selectedItems = selectionSnapshot
                .map((item) => this._findSceneItemBySelection(item.type, item.id))
                .filter(Boolean);
            this.selectedItem = selectedItemSnapshot
                ? this._findSceneItemBySelection(selectedItemSnapshot.type, selectedItemSnapshot.id)
                : (this.selectedItems[this.selectedItems.length - 1] || null);
            this._buildTrackLayout();
            this._renderTimeline();
            this._renderViewportFrame();
        } catch (error) {
            this._discardLastUndo(undoLabel);
        }
    }

    async _removeLaneDeletingItems(trackType, laneIndex) {
        if (!this.activeScene || !this.projectDir) return;
        const laneType = this._laneTypeFromTrackType(trackType);
        if (!laneType) return;
        const label = this._laneLabelFromTrackType(trackType);
        const isVideo = trackType === TRACK_TYPE.VIDEO;
        const isDriver = trackType === TRACK_TYPE.MOTION_DRIVER;
        const currentCount = isVideo
            ? (this.activeScene.video_lane_count || 1)
            : isDriver
                ? (this.activeScene.motion_driver_lane_count || 1)
                : (this.activeScene.audio_lane_count || 1);
        if (currentCount <= 1) {
            this._showToast(`Cannot remove the only ${label} lane.`);
            return;
        }
        if (this._isLaneLocked(trackType, laneIndex)) {
            this._showToast("Lane is locked.");
            return;
        }
        const items = this._laneItemsForTrackType(trackType, laneIndex);
        if (!confirm(`Delete ${label} lane ${laneIndex + 1} and ${items.length} item(s) on it?`)) return;

        const undoLabel = "delete lane and items";
        const operation = {
            type: "remove_lane",
            lane_type: laneType,
            lane_index: laneIndex,
            item_policy: "delete_items",
        };

        try {
            this._pushUndo(undoLabel);
            if (!this._applyLocalRemoveLane(operation.lane_type, laneIndex, operation.item_policy)) {
                throw new Error("Local lane removal refused.");
            }
            this._clearSelection();
            this._hideItemEditor();
            this._renderSceneAfterLocalMutation();
            await this._runSceneMutation(
                [operation],
                {
                    key: `scene:${this.activeSceneId}:${laneType}-delete-lane-and-items:${laneIndex}`,
                    label: "delete lane and items",
                    coalesce: false,
                }
            );
        } catch (e) {
            this._discardLastUndo(undoLabel);
            await this._fetchScenes({ ignoreMutationGate: true, reason: "delete_lane_and_items_error" });
            console.warn("[Sonder] Failed to delete lane and items:", e);
        }
    }

    _selectedLaneDeleteEntries() {
        const selectedEntries = (this._trackLayout || [])
            .filter((entry) => this._isLaneSelected(entry))
            .filter((entry) => entry.type === TRACK_TYPE.VIDEO || entry.type === TRACK_TYPE.AUDIO);
        const seen = new Set();
        const entries = [];
        for (const entry of selectedEntries) {
            const key = `${entry.type}:${entry.laneIndex || 0}`;
            if (seen.has(key)) continue;
            entries.push(entry);
            seen.add(key);
        }
        return entries;
    }

    async _deleteSelectedLanesAndItems(clickedEntry) {
        if (!this.activeScene || !this.projectDir) return;
        if (!clickedEntry || !this._isLaneSelected(clickedEntry) || (this._selectedLanes || []).length <= 1) return;
        const entries = this._selectedLaneDeleteEntries();
        if (!entries.length) {
            this._showToast("No selected video or audio lanes can be deleted.");
            return;
        }
        if (entries.some((entry) => this._isLaneLocked(entry.type, entry.laneIndex))) {
            this._showToast("Delete refused because one or more selected lanes are locked.");
            return;
        }

        const videoCount = Math.max(1, parseInt(this.activeScene.video_lane_count, 10) || 1);
        const audioCount = Math.max(1, parseInt(this.activeScene.audio_lane_count, 10) || 1);
        const selectedVideoCount = entries.filter((entry) => entry.type === TRACK_TYPE.VIDEO).length;
        const selectedAudioCount = entries.filter((entry) => entry.type === TRACK_TYPE.AUDIO).length;
        if (selectedVideoCount >= videoCount || selectedAudioCount >= audioCount) {
            this._showToast("At least one video lane and one audio lane must remain.");
            return;
        }

        const orderedEntries = [
            ...entries
                .filter((entry) => entry.type === TRACK_TYPE.VIDEO)
                .sort((a, b) => (b.laneIndex || 0) - (a.laneIndex || 0)),
            ...entries
                .filter((entry) => entry.type === TRACK_TYPE.AUDIO)
                .sort((a, b) => (b.laneIndex || 0) - (a.laneIndex || 0)),
        ];
        if (!orderedEntries.length) return;
        if (!confirm(`Delete ${orderedEntries.length} selected lane(s) and all items on them?`)) return;

        const operations = orderedEntries.map((entry) => ({
            type: "remove_lane",
            lane_type: this._laneTypeFromTrackType(entry.type),
            lane_index: entry.laneIndex || 0,
            item_policy: "delete_items",
        }));
        const undoLabel = "delete selected lanes";
        try {
            this._pushUndo(undoLabel);
            for (const operation of operations) {
                if (!this._applyLocalRemoveLane(operation.lane_type, operation.lane_index, operation.item_policy)) {
                    throw new Error("Local selected-lane removal refused.");
                }
            }
            this._clearSelection();
            this._clearLaneSelection();
            this._hideItemEditor();
            this._renderSceneAfterLocalMutation();
            await this._runSceneMutation(
                operations,
                {
                    key: `scene:${this.activeSceneId}:delete-selected-lanes:${operations.map((op) => `${op.lane_type}:${op.lane_index}`).join(",")}`,
                    label: "delete selected lanes",
                    coalesce: false,
                }
            );
        } catch (e) {
            this._discardLastUndo(undoLabel);
            await this._fetchScenes({ ignoreMutationGate: true, reason: "delete_selected_lanes_error" });
            console.warn("[Sonder] Failed to delete selected lanes:", e);
        }
    }

    async _removeLaneWithItems(trackType, laneIndex) {
        const isVideo = trackType === TRACK_TYPE.VIDEO;
        const isDriver = trackType === TRACK_TYPE.MOTION_DRIVER;
        const laneType = isVideo ? "video" : (isDriver ? "motion_driver" : "audio");
        const label = isVideo ? "video" : (isDriver ? "driver" : "audio");
        const items = isVideo
            ? (this.activeScene?.clips || []).filter(c => this._isRenderClip(c) && (c.track_index || 0) === laneIndex)
            : isDriver
                ? (this.activeScene?.clips || []).filter(c => this._isMotionDriverClip(c) && (c.track_index || 0) === laneIndex)
            : (this.activeScene?.audio_tracks || []).filter(a => (a.lane_index || 0) === laneIndex);
        const targetLane = laneIndex > 0 ? laneIndex - 1 : 1;
        const currentCount = isVideo
            ? (this.activeScene?.video_lane_count || 1)
            : isDriver
                ? (this.activeScene?.motion_driver_lane_count || 1)
            : (this.activeScene?.audio_lane_count || 1);

        const willMove = currentCount > 1 && targetLane !== laneIndex;
        if (!willMove) {
            notifyWarning(`Cannot move items because there is no destination ${label} lane.`, { source: "timeline-lane-move-refused" });
            return;
        }
        const refusal = this._laneMoveItemsRefusal(trackType, laneIndex, targetLane);
        if (refusal) {
            notifyWarning(refusal, { source: "timeline-lane-move-refused" });
            return;
        }
        const msg = `This ${label} lane has ${items.length} item(s). Move them to lane ${targetLane + 1} and remove this lane?`;
        if (!confirm(msg)) return;

        const undoLabel = "remove lane";
        const operation = {
            type: "remove_lane",
            lane_type: laneType,
            lane_index: laneIndex,
            item_policy: "move_items",
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
                    key: `scene:${this.activeSceneId}:${laneType}-remove-lane:${laneIndex}`,
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
        const isVideo = trackType === TRACK_TYPE.VIDEO;
        const isDriver = trackType === TRACK_TYPE.MOTION_DRIVER;
        const label = isVideo ? "video" : (isDriver ? "driver" : "audio");
        const items = isVideo
            ? (this.activeScene?.clips || []).filter(c => this._isRenderClip(c) && (c.track_index || 0) === laneIndex)
            : isDriver
                ? (this.activeScene?.clips || []).filter(c => this._isMotionDriverClip(c) && (c.track_index || 0) === laneIndex)
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
                type: (isVideo || isDriver) ? "clip" : "audio",
                id: (isVideo || isDriver) ? item.clip_id : item.track_id,
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
                    key: `scene:${this.activeSceneId}:${isVideo ? "video" : (isDriver ? "motion_driver" : "audio")}-delete-lane-items:${laneIndex}`,
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
        const isVideo = trackType === TRACK_TYPE.VIDEO;
        const isDriver = trackType === TRACK_TYPE.MOTION_DRIVER;
        const laneType = isVideo ? "video" : (isDriver ? "motion_driver" : "audio");
        const currentCount = isVideo
            ? (this.activeScene.video_lane_count || 1)
            : isDriver
                ? (this.activeScene.motion_driver_lane_count || 1)
            : (this.activeScene.audio_lane_count || 1);
        if (currentCount <= 1) return;
        const undoLabel = "remove lane";
        const operation = {
            type: "remove_lane",
            lane_type: laneType,
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
                    key: `scene:${this.activeSceneId}:${laneType}-remove-lane:${laneIndex}`,
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

        // Clamp the requested range into the free gap around the click point —
        // sections cannot overlap (backend 409 is the safety net)
        const anchor = Number.isFinite(frame)
            ? Math.max(startFrame, Math.min(endFrame - 1, frame))
            : startFrame;
        const sections = this.activeScene.prompt_sections || [];
        let gapStart = 0;
        let gapEnd = this.totalFrames;
        for (const s of sections) {
            const sStart = s.start_frame || 0;
            const sEnd = s.end_frame || 0;
            if (sStart <= anchor && anchor < sEnd) {
                notifyWarning("Prompt sections cannot overlap — pick a free spot on the lane.", { source: "prompt-create-overlap" });
                return;
            }
            if (sEnd <= anchor) gapStart = Math.max(gapStart, sEnd);
            if (sStart > anchor) gapEnd = Math.min(gapEnd, sStart);
        }
        startFrame = Math.max(startFrame, gapStart);
        endFrame = Math.min(endFrame, gapEnd);
        if (endFrame - startFrame < 1) {
            notifyWarning("No free room for a prompt section at this position.", { source: "prompt-create-overlap" });
            return;
        }

        // Show inline editor for the new prompt section
        this._showPromptCreator(startFrame, endFrame);
    }

    /** Auto-growing prompt textarea for inline bars: Enter commits,
     *  Shift+Enter inserts a newline, Esc cancels. */
    _makePromptTextarea({ value = "", placeholder = "", title = "", flex = 1 }, onEnter, onEscape) {
        const area = document.createElement("textarea");
        // Marks every prompt-editing field so the global key consumer leaves
        // Ctrl+Z to native text undo instead of routing it to timeline undo
        // (keyboard_ownership dispatches at window-capture, before this
        // element's stopPropagation runs). Mirrors the panel boxes.
        area.dataset.sonderPromptBox = "1";
        area.rows = 2;
        area.placeholder = placeholder;
        area.title = title ? `${title} — Enter commits, Shift+Enter inserts a newline` : "Enter commits, Shift+Enter inserts a newline";
        area.value = value;
        area.style.cssText = `flex: ${flex}; min-width: 40px; resize: none; overflow-y: auto; line-height: 1.35; ${chromeInputCss({ fontSize: "11px", padding: "3px 6px", textAlign: "left" })}`;
        const grow = () => {
            area.style.height = "auto";
            const maxPx = Math.round(6 * 15 + 10); // ~6 lines
            area.style.height = `${Math.min(area.scrollHeight, maxPx)}px`;
        };
        area.addEventListener("input", grow);
        setTimeout(grow, 0);
        area.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                onEnter();
            } else if (e.key === "Escape") {
                onEscape();
            }
            e.stopPropagation();
        });
        return area;
    }

    /** Three channel textareas (Visual/Speech/Sounds) for inline prompt bars. */
    _buildChannelInputs(initialChannels, onEnter, onEscape) {
        const channels = normalizeChannels(initialChannels);
        const wrap = document.createElement("div");
        wrap.style.cssText = "display: flex; gap: 4px; flex: 1; min-width: 0; align-items: flex-start;";
        const inputs = {};
        const placeholders = { visual: "Visual…", speech: "Speech…", sounds: "Sounds…" };
        for (const key of ["visual", "speech", "sounds"]) {
            const input = this._makePromptTextarea({
                value: channels[key] || "",
                placeholder: placeholders[key],
                title: `${key[0].toUpperCase()}${key.slice(1)} channel`,
                flex: key === "visual" ? 2 : 1,
            }, onEnter, onEscape);
            inputs[key] = input;
            wrap.appendChild(input);
        }
        const read = () => ({
            visual: inputs.visual.value.trim(),
            speech: inputs.speech.value.trim(),
            sounds: inputs.sounds.value.trim(),
        });
        return { wrap, inputs, read };
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

        const commit = () => {
            const channels = channelInputs.read();
            if (channels.visual || channels.speech || channels.sounds) {
                this._saveNewPromptSection(startFrame, endFrame, channels);
            }
        };
        const channelInputs = this._buildChannelInputs(null, commit, () => this._hidePromptEditor());

        const createBtn = this._makeBtn("Create", "Create prompt section");
        setButtonVariant(createBtn, "primary");
        createBtn.dataset.sonderHoverVariant = "primary";
        createBtn.addEventListener("click", commit);

        const cancelBtn = this._makeBtn("Cancel", "Cancel");
        setButtonVariant(cancelBtn, "subtle");
        cancelBtn.dataset.sonderHoverVariant = "subtle";
        cancelBtn.addEventListener("click", () => this._hidePromptEditor());

        editor.append(label, channelInputs.wrap, createBtn, cancelBtn);
        this.timelineCanvas.parentElement.insertBefore(editor, this.timelineCanvas.nextSibling);
        this._promptEditorEl = editor;
        this._refreshTimelineLayout();

        setTimeout(() => channelInputs.inputs.visual.focus(), 50);
    }

    async _saveNewPromptSection(startFrame, endFrame, channels) {
        if (!this.activeScene || !this.projectDir) return;
        if (this._isPromptTrackLocked()) return;
        const undoLabel = "add prompt";
        const fields = {
            prompt_id: this._newLocalItemId("prompt"),
            start_frame: startFrame,
            end_frame: endFrame,
            channels: normalizeChannels(channels),
            muted: false,
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
            notifyWarning(e?.message || "Prompt section was refused.", { source: "prompt-create-refused" });
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

        const commit = () => {
            this._updatePromptSection(idx, { channels: channelInputs.read() });
            this._hidePromptEditor();
        };
        const channelInputs = this._buildChannelInputs(
            normalizeChannels(section.channels, section.prompt),
            commit,
            () => {
                this._hidePromptEditor();
                this._selectedPromptIdx = null;
                this._renderTimeline();
            }
        );

        const saveBtn = this._makeBtn("Save", "Save prompt");
        setButtonVariant(saveBtn, "primary");
        saveBtn.dataset.sonderHoverVariant = "primary";
        saveBtn.addEventListener("click", commit);

        const deleteBtn = this._makeBtn("Delete", "Delete this prompt section");
        setButtonVariant(deleteBtn, "danger");
        deleteBtn.dataset.sonderHoverVariant = "danger";
        deleteBtn.addEventListener("click", () => {
            if (confirm(`Delete this prompt section?`)) {
                this._deletePromptSection(idx);
            }
        });

        editor.append(label, channelInputs.wrap, saveBtn, deleteBtn);
        // Insert after timeline canvas
        this.timelineCanvas.parentElement.insertBefore(editor, this.timelineCanvas.nextSibling);
        this._promptEditorEl = editor;
        this._refreshTimelineLayout();

        // Focus input
        setTimeout(() => channelInputs.inputs.visual.focus(), 50);
    }

    /** Inline editor bar for the scene-global prompt lane (Scene.prompt).
     *  Auto-commits on Enter/blur (no Save button); Esc cancels — the cancel
     *  path must beat the removal-triggered blur via the suppress flag. */
    _showGlobalPromptEditor() {
        if (!this.activeScene) return;
        if (this._isGlobalPromptTrackLocked()) return;
        this._hidePromptEditor();

        const editor = document.createElement("div");
        editor.style.cssText = `
            display: flex; gap: 4px; padding: 4px 6px;
            background: ${COLORS.panel}; border-top: 1px solid ${COLORS.promptBorder};
            align-items: flex-start;
        `;

        const label = document.createElement("span");
        label.style.cssText = `font-size: 10px; color: ${COLORS.promptBorder}; white-space: nowrap; padding-top: 5px;`;
        label.textContent = "Global:";

        let suppressBlurCommit = false;
        const commit = () => {
            if (suppressBlurCommit) return;
            this._updateScenePrompt(input.value);
        };
        const input = this._makePromptTextarea({
            value: this.activeScene.prompt || "",
            placeholder: "Scene-global prompt (style, identity, location)…",
            title: "Global prompt — auto-commits on Enter or focus loss; Esc cancels",
        }, () => {
            commit();
            this._hidePromptEditor();
        }, () => {
            suppressBlurCommit = true;
            this._hidePromptEditor();
        });
        input.addEventListener("blur", () => {
            commit();
            // Blur from clicking elsewhere closes the bar; the hide itself
            // re-triggers no commit because the element is already detached
            if (this._promptEditorEl === editor) this._hidePromptEditor();
        });

        editor.append(label, input);
        this.timelineCanvas.parentElement.insertBefore(editor, this.timelineCanvas.nextSibling);
        this._promptEditorEl = editor;
        this._refreshTimelineLayout();

        setTimeout(() => input.focus(), 50);
    }

    /** Durable write of the scene-global prompt via the mutation pipeline. */
    async _updateScenePrompt(value) {
        if (!this.activeScene || !this.projectDir) return;
        if (this._isGlobalPromptTrackLocked()) return;
        const sceneRef = this.activeScene;
        const sceneId = this.activeSceneId;
        const next = String(value ?? "");
        const prev = sceneRef.prompt || "";
        if (next === prev) return;
        const undoLabel = "edit global prompt";
        this._pushUndo(undoLabel);
        sceneRef.prompt = next;
        this._renderSceneAfterLocalMutation({ viewport: false });
        try {
            await this._runSceneMutation(
                [{ type: "update_scene_fields", fields: { prompt: next } }],
                {
                    key: `scene:${sceneId}:prompt`,
                    label: "global prompt",
                    coalesce: true,
                    refreshScenes: false,
                }
            );
        } catch (e) {
            this._discardLastUndo(undoLabel);
            if (sceneRef === this.activeScene) sceneRef.prompt = prev;
            notifyWarning(e?.message || "Global prompt edit was refused.", { source: "prompt-global-refused" });
            console.warn("[Sonder] Failed to update global prompt:", e);
            this._renderTimeline();
        }
    }

    /** Open (or refresh) the Prompt Management panel. */
    _showPromptManagementPanel() {
        if (this._promptPanelHandle?.isMounted?.()) {
            this._promptPanelHandle.refresh();
            return;
        }
        this._promptPanelHandle = mountPromptManagementPanel(this);
    }

    /** Project-durable channel-labels toggle.
     *  Deliberately OUTSIDE the ProjectMutationQueue (documented exemption):
     *  project-level metadata — not a scene mutation op — an infrequent single
     *  toggle following the asset_folders precedent; not undo-enrolled. */
    async _togglePromptChannelLabels(on) {
        const dirName = this._projectDirName();
        if (!dirName) return;
        try {
            await this._runVersionedProjectMutation(
                `/sonder-editor/project/${encodeURIComponent(dirName)}`,
                {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ metadata: { prompt_channel_labels: !!on } }),
                },
                { projectId: dirName }
            );
            this._promptChannelLabels = !!on;
        } catch (e) {
            notifyWarning(e?.message || "Failed to update channel-labels setting.", { source: "prompt-labels-refused" });
            throw e;
        }
    }

    /** Project-durable section-seam delimiter (changes model-visible text).
     *  Same documented ProjectMutationQueue exemption as the labels toggle:
     *  project-level metadata, infrequent single control, asset_folders
     *  precedent; not undo-enrolled. */
    async _setPromptSectionDelimiter(value) {
        const dirName = this._projectDirName();
        if (!dirName) return;
        const delimiter = String(value ?? "").trim().slice(0, 8);
        try {
            await this._runVersionedProjectMutation(
                `/sonder-editor/project/${encodeURIComponent(dirName)}`,
                {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ metadata: { prompt_section_delimiter: delimiter } }),
                },
                { projectId: dirName }
            );
            this._promptSectionDelimiter = delimiter;
        } catch (e) {
            notifyWarning(e?.message || "Failed to update section delimiter.", { source: "prompt-delimiter-refused" });
            throw e;
        }
    }

    /** Project-durable boundary-spill threshold (%). Drops a prompt section
     *  from a render window when the selection only clips a small sliver of it
     *  at the window edge (under N% of that section's own length); 0 = off.
     *  Same project-metadata mutation exemption as the delimiter/labels. */
    async _setPromptFrameThreshold(value) {
        const dirName = this._projectDirName();
        if (!dirName) return;
        let pct = parseFloat(value);
        if (!Number.isFinite(pct)) pct = 0;
        pct = Math.max(0, Math.min(100, pct));
        try {
            await this._runVersionedProjectMutation(
                `/sonder-editor/project/${encodeURIComponent(dirName)}`,
                {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ metadata: { prompt_frame_threshold: pct } }),
                },
                { projectId: dirName }
            );
            this._promptFrameThreshold = pct;
            // The threshold changes which sections survive the live selection
            // window — refresh the timeline "used/ignored" highlight.
            this._refreshPromptUsageHighlight();
        } catch (e) {
            notifyWarning(e?.message || "Failed to update prompt threshold.", { source: "prompt-threshold-refused" });
            throw e;
        }
    }

    /** Resolved relay payload preview for the panel (full-scene window). */
    async _fetchPromptPayload() {
        const dirName = this._projectDirName();
        const sceneId = this.activeSceneId;
        if (!dirName || !sceneId) return null;
        try {
            const resp = await fetch(api.apiURL(
                `/sonder-editor/project/${encodeURIComponent(dirName)}/scenes/${encodeURIComponent(sceneId)}/prompt-payload`
            ));
            if (!resp.ok) return null;
            return await resp.json();
        } catch (e) {
            console.warn("[Sonder] Failed to fetch prompt payload:", e);
            return null;
        }
    }

    /** Refresh the timeline "used / ignored-at-boundary" prompt highlight for
     *  the live selection window. Resolves over the same raw selection+context
     *  window the dormant preview uses (the render's grid-snap drift is an
     *  accepted approximation, durable_rules.md). Debounced and stale-guarded:
     *  the route is a filesystem load, so out-of-order responses are dropped. */
    _refreshPromptUsageHighlight() {
        const sameSet = (a, b) => a.size === b.size && [...a].every((v) => b.has(v));
        const setSets = (used, dropped) => {
            const changed = !sameSet(this._promptUsedSections, used)
                || !sameSet(this._promptDroppedSections, dropped);
            this._promptUsedSections = used;
            this._promptDroppedSections = dropped;
            if (changed) this._renderTimeline();
        };
        const ctx = this._selectionContextRange();
        if (!ctx) { setSets(new Set(), new Set()); return; }
        const windowStart = Math.max(0, Math.round(ctx.contextStart));
        const windowEnd = Math.round(ctx.contextEnd);
        if (this._promptHighlightTimer) clearTimeout(this._promptHighlightTimer);
        const token = (this._promptHighlightToken || 0) + 1;
        this._promptHighlightToken = token;
        this._promptHighlightTimer = setTimeout(async () => {
            const payload = await this._fetchPromptUsage(windowStart, windowEnd);
            if (token !== this._promptHighlightToken) return; // a newer request won
            if (!payload) return;
            setSets(
                new Set((payload.used_sections || []).map(Number)),
                new Set((payload.dropped_sections || []).map(Number)),
            );
        }, 120);
    }

    /** Windowed prompt-payload fetch returning the used/dropped section sets
     *  the timeline highlight draws. */
    async _fetchPromptUsage(windowStart, windowEnd) {
        const dirName = this._projectDirName();
        const sceneId = this.activeSceneId;
        if (!dirName || !sceneId) return null;
        try {
            const resp = await fetch(api.apiURL(
                `/sonder-editor/project/${encodeURIComponent(dirName)}/scenes/${encodeURIComponent(sceneId)}/prompt-payload`
                + `?window_start=${windowStart}&window_end=${windowEnd}`
            ));
            if (!resp.ok) return null;
            return await resp.json();
        } catch (e) {
            console.warn("[Sonder] Failed to fetch prompt usage:", e);
            return null;
        }
    }

    /** Queue a prompt-bounded range as the unit of work: set the selection to
     *  the section's range (snap policy applies) and route through the
     *  existing enqueue paths. The sticky `prompts.queueSectionBatch` toggle
     *  picks the path: on → auto-chunked batch (degrades to single when the
     *  range fits one chunk); off → ONE job for the whole range (backend
     *  frame round-up applies — the user's explicit choice). */
    async _queuePromptSection(section) {
        if (!section || !this.activeScene) return;
        const start = section.start_frame || 0;
        const end = section.end_frame || 0;
        if (end <= start) {
            notifyWarning("Prompt section has no frames to queue.", { source: "queue-prompt-section" });
            return;
        }
        this._setSelectionToFrameRange(start, end);
        if (this._settings?.prompts?.queueSectionBatch !== false) {
            await this._addBatchToRenderQueue();
        } else {
            await this._addToRenderQueue();
        }
    }

    _promptItemsForQueueBatch(items = this.selectedItems) {
        const promptItems = [];
        const seen = new Set();
        for (const item of this._expandItemsWithLinked(items || [])) {
            const current = this._findSceneItemBySelection(item?.type, item?.id) || item;
            if (current?.type !== "prompt") continue;
            const section = current.data || {};
            const fallbackKey = current.id != null
                ? String(current.id)
                : `${section.start_frame || 0}:${section.end_frame || 0}:${promptItems.length}`;
            const key = String(section.prompt_id || fallbackKey);
            if (seen.has(key)) continue;
            promptItems.push({ item: current, section });
            seen.add(key);
        }
        return promptItems.sort((a, b) => {
            const aStart = Math.round(Number(a.section?.start_frame) || 0);
            const bStart = Math.round(Number(b.section?.start_frame) || 0);
            if (aStart !== bStart) return aStart - bStart;
            const aEnd = Math.round(Number(a.section?.end_frame) || 0);
            const bEnd = Math.round(Number(b.section?.end_frame) || 0);
            return aEnd - bEnd;
        });
    }

    async _queueSelectedPromptSections(items = this.selectedItems) {
        if (!this.activeScene) return;
        const promptItems = this._promptItemsForQueueBatch(items);
        const queueItems = promptItems.filter(({ section }) => {
            const start = Math.round(Number(section?.start_frame) || 0);
            const end = Math.round(Number(section?.end_frame) || 0);
            return !section?.muted && end > start;
        });
        if (!queueItems.length) {
            notifyWarning("No non-muted prompt sections with frames to queue.", { source: "queue-prompt-sections" });
            return;
        }
        const skipped = promptItems.length - queueItems.length;
        if (skipped > 0) {
            notifyWarning(`Skipped ${skipped} muted or empty prompt section${skipped === 1 ? "" : "s"}.`, { source: "queue-prompt-sections" });
        }

        const projectId = this._projectDirName();
        if (!projectId) return;
        const batchId = globalThis.crypto?.randomUUID?.()
            || `${Date.now().toString(36)}-${Math.random().toString(16).slice(2, 10)}`;

        let snapshots = [];
        let tempJobs = [];
        let tempIds = new Set();

        try {
            snapshots = queueItems.map(({ section }, index) => {
                const start = Math.round(Number(section.start_frame) || 0);
                const end = Math.round(Number(section.end_frame) || 0);
                const snapshot = this._buildQueueSnapshot(start, end);
                if (!snapshot) {
                    throw new Error("Failed to build prompt-section queue snapshot.");
                }
                return {
                    ...snapshot,
                    batch_id: batchId,
                    batch_total: queueItems.length,
                    batch_index: index,
                };
            });
            tempJobs = snapshots.map((snapshot, index) => ({
                ...snapshot,
                job_id: `temp-prompt-batch-${Date.now().toString(36)}-${index}-${Math.random().toString(16).slice(2, 8)}`,
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

            const result = await this._queueProjectMutation({
                key: `project:queue:prompt-sections:${batchId}`,
                label: "add prompt section batch",
                coalesce: false,
                intent: {
                    projectId,
                    snapshots: JSON.parse(JSON.stringify(snapshots)),
                },
                refreshScenes: false,
                refreshKeysOnError: ["queue"],
                failureMessage: "Add prompt section batch failed - queue restored.",
                invalidateQueueFetch: true,
                run: async (queuedIntent) => {
                    return await this._runVersionedProjectMutation(
                        `/sonder-editor/project/${encodeURIComponent(queuedIntent.projectId)}/queue/batch`,
                        {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ jobs: queuedIntent.snapshots }),
                        },
                        { projectId: queuedIntent.projectId }
                    );
                },
            });
            const payload = result?.payload || {};
            const createdJobs = Array.isArray(payload?.jobs) ? payload.jobs : [];
            this._renderQueue = (this._renderQueue || []).map((job) => {
                if (!tempIds.has(job.job_id)) return job;
                const tempIndex = tempJobs.findIndex((temp) => temp.job_id === job.job_id);
                return createdJobs[tempIndex] || job;
            });
            this._flashQueueButton(this._batchQueueBtn || this._queueBtn);
            this._applyStoredQueueBatchCollapseState();
            this._renderQueuePanel();
        } catch (e) {
            console.error("Add prompt section batch failed:", e);
            this._renderQueue = (this._renderQueue || []).filter((job) => !tempIds.has(job.job_id));
            this._renderQueuePanel();
            if (!tempIds.size) {
                notifyError(e?.message || "Add prompt section batch failed.", {
                    onRetry: () => { this._queueSelectedPromptSections(items).catch(() => {}); },
                });
            }
        }
    }

    /** Insert an empty section directly after the given one, filling the gap
     *  to the next section / scene end (min 1 frame; warns when no room). */
    async _addPromptSectionAfter(index) {
        const scene = this.activeScene;
        if (!scene || this._isPromptTrackLocked()) return false;
        const sections = scene.prompt_sections || [];
        const section = sections[index];
        if (!section) return false;
        const duration = scene.duration_frames || this.totalFrames || 0;
        const gapStart = section.end_frame || 0;
        const next = sections[index + 1];
        const gapEnd = Math.min(next ? (next.start_frame || 0) : duration, duration);
        if (gapEnd - gapStart < 1) {
            notifyWarning("No free room after this section.", { source: "prompt-add-section" });
            return false;
        }
        await this._saveNewPromptSection(gapStart, gapEnd, { visual: "", speech: "", sounds: "" });
        return true;
    }

    /** Create an empty section in the FIRST free gap scanning from frame 0
     *  (whole gap, min 1 frame; warns when the lane is full). */
    async _addPromptSectionInFirstGap() {
        const scene = this.activeScene;
        if (!scene || this._isPromptTrackLocked()) return false;
        const duration = scene.duration_frames || this.totalFrames || 0;
        const sections = [...(scene.prompt_sections || [])]
            .sort((a, b) => (a.start_frame || 0) - (b.start_frame || 0));
        let cursor = 0;
        let gap = null;
        for (const section of sections) {
            const start = section.start_frame || 0;
            if (start - cursor >= 1) {
                gap = [cursor, start];
                break;
            }
            cursor = Math.max(cursor, section.end_frame || 0);
        }
        if (!gap && duration - cursor >= 1) gap = [cursor, duration];
        if (!gap) {
            notifyWarning("No free room on the prompt lane.", { source: "prompt-add-section" });
            return false;
        }
        await this._saveNewPromptSection(gap[0], gap[1], { visual: "", speech: "", sounds: "" });
        return true;
    }

    /** Prompt history entries (Prompt Saver, captured server-side at enqueue), newest first. */
    async _fetchPromptHistory() {
        const dirName = this._projectDirName();
        if (!dirName) return [];
        try {
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${encodeURIComponent(dirName)}`));
            if (!resp.ok) return [];
            const data = await resp.json();
            const history = data?.metadata?.prompt_history;
            return Array.isArray(history) ? history.slice().reverse() : [];
        } catch (e) {
            console.warn("[Sonder] Failed to fetch prompt history:", e);
            return [];
        }
    }

    /** Replace the scene's prompt state with a history entry / template:
     *  ONE mutation request (deletes high-index-first, then creates, then the
     *  global text) so the apply is a single save and a single undo step. */
    async _applyPromptSetup({ global: globalText, sections, extendDurationTo = 0, source_fps: sourceFps = 0 } = {}) {
        if (!this.activeScene || !this.projectDir) return;
        if (this._isPromptTrackLocked() || this._isGlobalPromptTrackLocked()) {
            notifyWarning("Prompt track is locked.", { source: "prompt-apply-refused" });
            return;
        }
        const sceneRef = this.activeScene;
        const undoLabel = "apply prompt setup";
        this._pushUndo(undoLabel);
        const current = sceneRef.prompt_sections || [];
        const operations = [];
        for (let i = current.length - 1; i >= 0; i--) {
            operations.push({
                type: "delete_prompt_section",
                index: i,
                expected: { start_frame: current[i].start_frame, end_frame: current[i].end_frame },
            });
        }
        const capturedFps = Number(sourceFps);
        const targetFps = Math.max(0.001, Number(this._effectiveFps) || 24);
        const timeScale = Number.isFinite(capturedFps) && capturedFps > 0
            ? targetFps / capturedFps
            : 1.0;
        let previousEnd = 0;
        const nextSections = (sections || [])
            .filter((s) => (s?.end_frame || 0) > (s?.start_frame || 0))
            .sort((a, b) => (a.start_frame || 0) - (b.start_frame || 0))
            .map((s) => {
                const channels = normalizeChannels(s.channels, s.prompt);
                const startFrame = Math.max(previousEnd, Math.round((s.start_frame || 0) * timeScale));
                const endFrame = Math.max(startFrame + 1, Math.round((s.end_frame || 0) * timeScale));
                previousEnd = endFrame;
                return {
                    prompt_id: s.prompt_id || this._newLocalItemId("prompt"),
                    start_frame: startFrame,
                    end_frame: endFrame,
                    channels,
                    prompt: composeSectionText(channels, false),
                    muted: !!s.muted,
                };
            });
        for (const s of nextSections) {
            operations.push({
                type: "create_prompt_section",
                fields: { prompt_id: s.prompt_id, start_frame: s.start_frame, end_frame: s.end_frame, channels: s.channels, muted: !!s.muted },
            });
        }
        // Optionally grow the scene to fit the new sections (writing-mode
        // "Apply & Extend"). Merge into the SAME scene-fields op so it stays
        // one mutation + one undo — the undo snapshot is a full scene clone,
        // so a single Ctrl+Z reverts both the sections and the duration.
        const curDuration = sceneRef.duration_frames || this.totalFrames || 0;
        const nextDuration = Math.max(0, Math.round(extendDurationTo || 0));
        const willExtend = nextDuration > curDuration;
        const sceneFields = { prompt: String(globalText ?? "") };
        if (willExtend) sceneFields.duration_frames = nextDuration;
        operations.push({ type: "update_scene_fields", fields: sceneFields });

        sceneRef.prompt = String(globalText ?? "");
        sceneRef.prompt_sections = [...nextSections].sort((a, b) => (a.start_frame || 0) - (b.start_frame || 0));
        if (willExtend) {
            sceneRef.duration_frames = nextDuration;
            this.totalFrames = nextDuration;
            this._clampTimelineStateToDuration();
            this._refreshDurationInput();
        }
        this._renderSceneAfterLocalMutation({ viewport: false });
        if (willExtend) {
            this._updateToolbar();
            this._updateTransportUI();
        }
        try {
            await this._runSceneMutation(operations, {
                key: `prompt:${this.activeSceneId}:apply:${Date.now()}`,
                label: "apply prompt setup",
                coalesce: false,
            });
        } catch (e) {
            this._discardLastUndo(undoLabel);
            notifyWarning(e?.message || "Apply prompt setup was refused.", { source: "prompt-apply-refused" });
            await this._fetchScenes({ ignoreMutationGate: true, reason: "apply_prompt_error" });
        }
    }

    /** Browser-local prompt template library (cross-project; applying
     *  materializes concrete text into scene state). */
    _getPromptTemplates() {
        return this._settings?.promptTemplates || [];
    }

    _savePromptTemplate(name) {
        const scene = this.activeScene;
        const trimmed = String(name || "").trim();
        if (!scene || !trimmed) return;
        const template = {
            id: `pt-${Date.now().toString(36)}-${Math.random().toString(16).slice(2, 6)}`,
            name: trimmed,
            global: scene.prompt || "",
            source_fps: Math.max(0.001, Number(this._effectiveFps) || 24),
            sections: (scene.prompt_sections || []).map((s) => ({
                start_frame: s.start_frame || 0,
                end_frame: s.end_frame || 0,
                channels: normalizeChannels(s.channels, s.prompt),
            })),
        };
        this._updateSettings({ promptTemplates: [...this._getPromptTemplates(), template] });
        notifySuccess(`Prompt template "${trimmed}" saved.`);
    }

    _deletePromptTemplate(templateId) {
        this._updateSettings({
            promptTemplates: this._getPromptTemplates().filter((t) => t.id !== templateId),
        });
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
            notifyWarning(e?.message || "Prompt edit was refused.", { source: "prompt-edit-refused" });
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
            notifyWarning(e?.message || "Prompt delete was refused.", { source: "prompt-delete-refused" });
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
            display: flex; gap: 6px; padding: 4px 6px; box-sizing: border-box;
            background: ${COLORS.panel}; border-top: 1px solid ${editorAccent};
            align-items: center; flex-wrap: wrap;
        `;

        const typeLabel = document.createElement("span");
        typeLabel.style.cssText = `font-size: 10px; color: ${editorAccent}; white-space: nowrap; font-weight: bold;`;
        typeLabel.textContent = type === "clip" ? (isMotionDriverClip ? "Driver" : "Video Clip") : type === "audio" ? "Audio Track" : "Guide Frame";
        editor.appendChild(typeLabel);

        if (type === "clip" || type === "audio") {
            const startFrame = data.timeline_start_frame;
            const endFrame = data.timeline_end_frame;
            const duration = endFrame - startFrame;

            // Start frame input
            const startLabel = this._makeEditorLabel(this._timecodeMode === "timecode" ? "Start (s):" : "Start:");
            const startInput = this._makeEditorInput(startFrame, 0, this.totalFrames);
            startInput.type = "text";
            startInput.inputMode = "decimal";
            startInput.value = this._formatPositionInput(startFrame);
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
                this._appendFitModeControls(editor, type, id, data);
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
            frameInput.type = "text";
            frameInput.inputMode = "decimal";
            frameInput.value = this._formatPositionInput(idx);
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
                thumb.style.cssText = "width:32px;height:20px;object-fit:contain;border-radius:3px;border:1px solid rgba(255,255,255,0.18);background:#000;";
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
            this._appendFitModeControls(editor, type, id, data);

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

    // Per-item fit-mode + crop-position controls for the selected clip/guide editor.
    // Crop anchor only shows for `cover`. Persists via the standard update_clip /
    // update_guide property path; the local `data` mirror is updated optimistically.
    _appendFitModeControls(editor, type, id, data) {
        const selectCss = `${chromeInputCss({ fontSize: "10px", padding: "2px 4px", textAlign: "left" })} min-width:96px; cursor:pointer;`;
        const fitLabel = this._makeEditorLabel("Fit:");
        const fitSelect = document.createElement("select");
        fitSelect.style.cssText = selectCss;
        for (const opt of FIT_MODE_OPTIONS) {
            const el = document.createElement("option");
            el.value = opt.value;
            el.textContent = opt.label;
            fitSelect.appendChild(el);
        }
        fitSelect.value = VALID_FIT_MODES.has(data.fit_mode) ? data.fit_mode : "pad_edge";

        const cropLabel = this._makeEditorLabel("Crop:");
        const cropSelect = document.createElement("select");
        cropSelect.style.cssText = selectCss;
        for (const opt of CROP_POSITION_OPTIONS) {
            const el = document.createElement("option");
            el.value = opt.value;
            el.textContent = opt.label;
            cropSelect.appendChild(el);
        }
        cropSelect.value = VALID_CROP_POSITIONS.has(data.crop_position) ? data.crop_position : "center";

        const syncCropVisibility = () => {
            const showCrop = fitSelect.value === "cover";
            cropLabel.style.display = showCrop ? "" : "none";
            cropSelect.style.display = showCrop ? "" : "none";
        };
        syncCropVisibility();

        fitSelect.addEventListener("change", () => {
            const value = VALID_FIT_MODES.has(fitSelect.value) ? fitSelect.value : "pad_edge";
            data.fit_mode = value;
            syncCropVisibility();
            this._updateItemProperty(type, id, { fit_mode: value });
        });
        cropSelect.addEventListener("change", () => {
            const value = VALID_CROP_POSITIONS.has(cropSelect.value) ? cropSelect.value : "center";
            data.crop_position = value;
            this._updateItemProperty(type, id, { crop_position: value });
        });

        editor.append(fitLabel, fitSelect, cropLabel, cropSelect);
    }

    async _moveItemToFrame(type, id, data, newStart) {
        if (!this.activeScene || !this.projectDir) return;
        const hit = { type, id, data };
        const applyLinked = this._isLinkedItem(hit);
        if (applyLinked && this._expandItemsWithLinked([hit]).some((item) => this._isItemLocked(item))) {
            notifyWarning("Move refused because one or more linked items are locked.", { source: "timeline-move-refused" });
            return;
        }
        this._pushUndo("move item");
        const operation = type === "clip"
            ? { type: "update_clip", clip_id: id, fields: { timeline_start_frame: newStart }, apply_linked: applyLinked }
            : { type: "update_audio_track", track_id: id, fields: { timeline_start_frame: newStart }, apply_linked: applyLinked };

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
        // Linked mute propagation (manual-test #7): muting one linked member mutes
        // the whole group atomically. Only `muted` propagates through links —
        // opacity/volume/strength and other per-item fields stay per-item.
        let applyLinked = false;
        if (props && "muted" in props) {
            const anchor = this._findSceneItemBySelection(type, id);
            if (anchor && this._isLinkedItem(anchor)) {
                const members = this._expandItemsWithLinked([anchor]);
                if (members.some((item) => this._isItemLocked(item))) {
                    // Callers flip the anchor's local muted before calling; restore it.
                    if (anchor.data && "muted" in anchor.data) anchor.data.muted = !props.muted;
                    notifyWarning("Linked mute refused because one or more linked items are locked.", { source: "timeline-mute-refused" });
                    if (this._itemEditorEl && this.selectedItem) this._showItemEditor();
                    this._renderTimeline();
                    this._renderViewportFrame();
                    return;
                }
                applyLinked = true;
                for (const member of members) {
                    if (member?.data) member.data.muted = !!props.muted;
                }
            }
        }
        let operation;
        if (type === "clip") {
            operation = { type: "update_clip", clip_id: id, fields: { ...props }, apply_linked: applyLinked };
        } else if (type === "guide") {
            const frameIndex = parseInt(id, 10);
            const guide = (this.activeScene.guide_frames || []).find((g) => (g.frame_index || 0) === frameIndex);
            operation = {
                type: "update_guide",
                frame_index: frameIndex,
                expected: guide ? {
                    frame_index: guide.frame_index,
                    asset_id: guide.asset_id || "",
                    guide_id: guide.guide_id || "",
                } : undefined,
                fields: { ...props },
                apply_linked: applyLinked,
            };
        } else {
            operation = { type: "update_audio_track", track_id: id, fields: { ...props }, apply_linked: applyLinked };
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
        const targets = this._expandItemsWithLinked(this.selectedItems)
            .filter((item) => item?.type === "clip" || item?.type === "audio" || item?.type === "guide" || item?.type === "prompt");
        if (!targets.length) return;
        if (targets.some((item) => this._isItemLocked(item))) {
            notifyWarning("Linked mute refused because one or more selected items are locked.", { source: "timeline-mute-refused" });
            return;
        }

        const nextMuted = !targets.every((item) => !!item.data?.muted);
        this._pushUndo(nextMuted ? "mute items" : "unmute items");
        const operations = [];
        const emittedLinkedGroups = new Set();
        for (const item of targets) {
            item.data.muted = nextMuted;
            const group = this._linkGroupForItem(item);
            const applyLinked = !!group;
            if (group) {
                const groupKey = group.group_id || this._linkRefKey(this._linkRefForItem(item));
                if (emittedLinkedGroups.has(groupKey)) continue;
                emittedLinkedGroups.add(groupKey);
            }
            if (item.type === "clip") {
                operations.push({ type: "update_clip", clip_id: item.data?.clip_id || item.id, fields: { muted: nextMuted }, apply_linked: applyLinked });
            } else if (item.type === "audio") {
                operations.push({ type: "update_audio_track", track_id: item.data?.track_id || item.id, fields: { muted: nextMuted }, apply_linked: applyLinked });
            } else if (item.type === "guide") {
                operations.push({
                    type: "update_guide",
                    frame_index: item.id,
                    expected: {
                        frame_index: item.data?.frame_index ?? item.id,
                        asset_id: item.data?.asset_id || "",
                        guide_id: item.data?.guide_id || "",
                    },
                    fields: { muted: nextMuted },
                    apply_linked: applyLinked,
                });
            } else if (item.type === "prompt") {
                operations.push({
                    type: "update_prompt_section",
                    index: item.id,
                    expected: {
                        start_frame: item.data?.start_frame,
                        end_frame: item.data?.end_frame,
                        prompt_id: item.data?.prompt_id || "",
                    },
                    fields: { muted: nextMuted },
                    apply_linked: applyLinked,
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

    async _createLinkGroupFromSelection() {
        if (!this.activeScene || !this.projectDir) return;
        const items = this._selectedLinkableItems()
            .map((item) => this._mutationItemFromSelection(item))
            .filter(Boolean);
        if (items.length < 2) {
            notifyWarning("Select at least two timeline items to link.", { source: "timeline-link" });
            return;
        }
        this._pushUndo("link items");
        try {
            await this._runSceneMutation(
                [{ type: "create_link_group", items }],
                {
                    key: `scene:${this.activeSceneId}:link-items:${Date.now()}`,
                    label: "link items",
                    coalesce: false,
                }
            );
            this._reconcileSelection();
            this._renderTimeline();
        } catch (e) {
            this._discardLastUndo("link items");
            notifyWarning(e?.message || "Link operation was refused.", { source: "timeline-link-refused" });
            await this._fetchScenes({ ignoreMutationGate: true, reason: "link_items_error" });
        }
    }

    async _unlinkSelectedItems() {
        if (!this.activeScene || !this.projectDir) return;
        const items = this._selectedLinkableItems()
            .filter((item) => this._isLinkedItem(item))
            .map((item) => this._mutationItemFromSelection(item))
            .filter(Boolean);
        if (!items.length) return;
        this._pushUndo("unlink items");
        try {
            await this._runSceneMutation(
                [{ type: "unlink_items", items, entire_group: true }],
                {
                    key: `scene:${this.activeSceneId}:unlink-items:${Date.now()}`,
                    label: "unlink items",
                    coalesce: false,
                }
            );
            this._reconcileSelection();
            this._renderTimeline();
        } catch (e) {
            this._discardLastUndo("unlink items");
            notifyWarning(e?.message || "Unlink operation was refused.", { source: "timeline-unlink-refused" });
            await this._fetchScenes({ ignoreMutationGate: true, reason: "unlink_items_error" });
        }
    }

    _selectLinkedItemsForSelection() {
        const expanded = this._expandItemsWithLinked(this.selectedItems);
        if (!expanded.length) return;
        this._clearLaneSelection();
        this.selectedItems = expanded;
        this.selectedItem = expanded[expanded.length - 1] || null;
        this._hideItemEditor();
        this._renderTimeline();
        this._updateToolbar();
    }

    async _moveGuideToFrame(guideData, newIdx, strength = guideData?.strength ?? 1.0) {
        if (!this.activeScene || !this.projectDir) return;
        if (this._isGuideTrackLocked()) return;
        const undoLabel = "move guide";
        this._pushUndo(undoLabel);
        const oldIdx = guideData.frame_index;
        const fields = {
            guide_id: guideData.guide_id || this._newLocalItemId("guide"),
            asset_id: guideData.asset_id,
            source: guideData.source || "asset",
            strength,
            muted: !!guideData.muted,
        };
        // Preserve fit fields across the delete+recreate move (guides are keyed by
        // frame_index). Only sent when present so the backend falls back to the
        // existing guide's values rather than rejecting an empty string.
        if (guideData.fit_mode != null) fields.fit_mode = guideData.fit_mode;
        if (guideData.crop_position != null) fields.crop_position = guideData.crop_position;
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
                        guide_id: guideData.guide_id || "",
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

    _promptHoverPreviewEnabled() {
        return this._settings?.prompts?.hoverPreviewEnabled !== false;
    }

    _hidePromptHoverPreview() {
        if (this._promptPreviewEl) {
            this._promptPreviewEl.remove();
            this._promptPreviewEl = null;
        }
    }

    /** Full-text hover preview for prompt sections + the global item,
     *  mirroring the guide hover system (reused fixed div, estimate clamp). */
    _showPromptHoverPreview(hit, clientX, clientY) {
        if (!hit || !this._promptHoverPreviewEnabled()) {
            this._hidePromptHoverPreview();
            return;
        }
        const labelsOn = this._promptChannelLabels === true;
        const isGlobal = hit.type === "prompt_global";
        const hidden = isGlobal ? this._isGlobalPromptTrackHidden() : (this._isPromptTrackHidden() || !!hit.data?.muted);
        const hiddenLabel = !isGlobal && hit.data?.muted ? "Muted" : "Hidden";
        const width = 360;

        let tag, rangeText, lines;
        if (isGlobal) {
            tag = "Global";
            rangeText = "scene-wide";
            const text = (this.activeScene?.prompt || "").trim();
            lines = text ? [text] : ["(empty global prompt)"];
        } else {
            const section = hit.data;
            tag = "Prompt";
            const start = section.start_frame || 0;
            // Display the INCLUSIVE last covered frame (ranges are half-open
            // [start, end) in data) so abutting sections never appear to
            // share a frame — f0–f119, f120–f239 instead of f0–f120, f120–f240
            const lastFrame = Math.max(start, (section.end_frame || 0) - 1);
            rangeText = this._timecodeMode === "timecode"
                ? `${this._frameToTimecode(start)}–${this._frameToTimecode(lastFrame)}`
                : `f${start}–f${lastFrame}`;
            const channels = normalizeChannels(section.channels, section.prompt);
            lines = [];
            for (const key of ["visual", "speech", "sounds"]) {
                const text = (channels[key] || "").trim();
                if (text) lines.push(labelsOn ? `[${key.toUpperCase()}]: ${text}` : text);
            }
            if (!lines.length) lines = ["(empty section)"];
        }

        let preview = this._promptPreviewEl;
        if (!preview) {
            preview = document.createElement("div");
            preview.style.cssText = `
                position: fixed; z-index: 10030; pointer-events: none;
                border-radius: 8px; overflow: hidden;
                box-shadow: 0 18px 42px rgba(0,0,0,0.52);
                font-family: ${FONT.sans};
            `;
            document.body.appendChild(preview);
            this._promptPreviewEl = preview;
        }
        preview.innerHTML = "";
        preview.style.width = `${width}px`;
        preview.style.background = hidden ? "rgba(31, 25, 20, 0.98)" : "rgba(15, 19, 24, 0.98)";
        preview.style.border = hidden ? `1px solid ${COLORS.warningBorder}` : `1px solid ${COLORS.borderStrong}`;
        preview.style.opacity = hidden ? "0.88" : "1";

        const meta = document.createElement("div");
        meta.style.cssText = "padding:7px 10px 4px;display:flex;align-items:center;gap:8px;";
        const tagEl = document.createElement("div");
        tagEl.textContent = tag;
        tagEl.style.cssText = `font-size:10px;font-weight:700;color:${COLORS.textMuted};text-transform:uppercase;letter-spacing:0.06em;`;
        const rangeEl = document.createElement("div");
        rangeEl.textContent = rangeText;
        rangeEl.style.cssText = `font-size:11px;color:${COLORS.guideSelected};font-family:${FONT.mono};`;
        meta.append(tagEl, rangeEl);
        if (hidden) {
            const badge = document.createElement("div");
            badge.textContent = hiddenLabel;
            badge.style.cssText = `
                margin-left:auto;padding:2px 7px;border-radius:999px;
                background:rgba(0,0,0,0.68);border:1px solid ${COLORS.warningBorder};
                color:${COLORS.warningText};font-size:10px;font-weight:700;
            `;
            meta.appendChild(badge);
        }
        const bodyEl = document.createElement("div");
        // ~12 lines clamp; long prompts cut off rather than overflow the screen
        bodyEl.style.cssText = `
            padding:4px 10px 9px;display:flex;flex-direction:column;gap:4px;
            max-height:190px;overflow:hidden;
        `;
        for (const line of lines) {
            const lineEl = document.createElement("div");
            lineEl.textContent = line;
            lineEl.style.cssText = `font-size:11px;line-height:1.4;color:${COLORS.text};word-break:break-word;white-space:pre-wrap;`;
            bodyEl.appendChild(lineEl);
        }
        preview.append(meta, bodyEl);

        const margin = 12;
        const estimatedHeight = Math.min(230, 36 + lines.length * 30);
        let left = clientX + 18;
        let top = clientY - estimatedHeight - 14;
        if (left + width + margin > window.innerWidth) left = clientX - width - 18;
        if (top < margin) top = clientY + 18;
        left = Math.max(margin, Math.min(window.innerWidth - width - margin, left));
        top = Math.max(margin, Math.min(window.innerHeight - estimatedHeight - margin, top));
        preview.style.left = `${left}px`;
        preview.style.top = `${top}px`;
        // Wrapped lines can exceed the estimate — re-clamp with the REAL
        // rect after layout (same pattern as the context-menu edge clamp)
        requestAnimationFrame(() => {
            if (this._promptPreviewEl !== preview) return;
            const rect = preview.getBoundingClientRect();
            const clampedLeft = Math.max(margin, Math.min(window.innerWidth - rect.width - margin, rect.left));
            const clampedTop = Math.max(margin, Math.min(window.innerHeight - rect.height - margin, rect.top));
            preview.style.left = `${clampedLeft}px`;
            preview.style.top = `${clampedTop}px`;
        });
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
        const sourceAsset = this._getAssetForSourcePath(clip.source_path);
        const sourceFps = Number(sourceAsset?.fps);
        const rateRatio = Number.isFinite(sourceFps) && sourceFps > 0
            ? sourceFps / Math.max(0.001, Number(this._effectiveFps) || 24)
            : 1.0;
        let backendSourceFrame = Math.floor((sourceFrame + 0.5) * rateRatio);
        const nativeFrameCount = Math.max(0, parseInt(sourceAsset?.frame_count, 10) || 0);
        if (nativeFrameCount > 0) {
            backendSourceFrame = Math.min(nativeFrameCount - 1, Math.max(0, backendSourceFrame));
        }
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
                        frame_index: backendSourceFrame,
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
                    source_frame: backendSourceFrame,
                    scene_source_frame: sourceFrame,
                    target_long_edge: targetLongEdge,
                });
                notifyInfo("Captured via backend (viewport snapshot unavailable)");
            }

            const fields = this._seedFitDefaults({
                guide_id: this._newLocalItemId("guide"),
                frame_index: this.playhead,
                asset_id: asset.asset_id,
                source: "asset",
                strength: this._defaultGuideStrength(),
            });
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
        const expanded = this._expandItemsWithLinked(this.selectedItems);
        if (!expanded.length) return;
        if (expanded.some((item) => this._isItemLocked(item))) {
            notifyWarning("Delete refused because one or more linked/selected items are locked.", { source: "timeline-delete-refused" });
            return;
        }
        const applyLinked = expanded.length > this.selectedItems.length
            || this.selectedItems.some((item) => this._isLinkedItem(item));
        const undoLabel = "delete items";
        this._pushUndo(undoLabel);
        const items = expanded.map((item) => this._mutationItemFromSelection(item)).filter(Boolean);
        this._applyLocalBulkDeleteItems(items);
        this._clearSelection();
        this._hideItemEditor();
        this._renderSceneAfterLocalMutation();

        try {
            await this._runSceneMutation(
                [{ type: "bulk_delete_items", items, apply_linked: applyLinked }],
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

    /** No-overlap prompt-lane drag preview: all-or-nothing linear move with
     *  hold-at-last-valid, plus a duration-preserving threshold swap on
     *  single-item drags. Assumes all section ranges were just restored from
     *  the mousedown snapshot; sections compare by object reference because
     *  array indices are re-sorted identity. */
    _previewPromptDrag(frameDelta, x, rawY) {
        const draggedPrompts = (this._dragItemsOrig || []).filter((o) => o.type === "prompt");
        if (!draggedPrompts.length) return;
        const sections = this.activeScene?.prompt_sections || [];
        const draggedSet = new Set(draggedPrompts.map((o) => o.data));
        const totalFrames = this.totalFrames || 0;

        const overlapsOther = (start, end, extraIgnore = null) => sections.some((s) => {
            if (draggedSet.has(s) || s === extraIgnore) return false;
            return (s.start_frame || 0) < end && (s.end_frame || 0) > start;
        });

        // Swap candidate: exactly one dragged item, cursor over another section
        let swap = null;
        if (draggedPrompts.length === 1 && (this._dragItemsOrig || []).length === 1) {
            const candidate = this._hitTestPrompt(x, rawY);
            if (candidate && !draggedSet.has(candidate.data)) {
                const dragged = draggedPrompts[0];
                const target = candidate.data;
                const dDur = dragged.origEnd - dragged.origStart;
                const tDur = (target.end_frame || 0) - (target.start_frame || 0);
                // Duration-preserving: dragged takes the target's start, target
                // takes the dragged origin. Different durations near other
                // sections can make the swap invalid — then it refuses (hold).
                const dStart = target.start_frame || 0;
                const dEnd = dStart + dDur;
                const tStart = dragged.origStart;
                const tEnd = tStart + tDur;
                const pairOverlap = dStart < tEnd && dEnd > tStart;
                if (!pairOverlap && dEnd <= totalFrames && tEnd <= totalFrames
                    && !overlapsOther(dStart, dEnd, target) && !overlapsOther(tStart, tEnd, target)) {
                    swap = { dragged, target, dStart, dEnd, tStart, tEnd };
                }
            }
        }

        if (swap) {
            swap.dragged.data.start_frame = swap.dStart;
            swap.dragged.data.end_frame = swap.dEnd;
            swap.target.start_frame = swap.tStart;
            swap.target.end_frame = swap.tEnd;
            this._dragPromptSwap = swap;
            this._dragPromptHold = { swap };
            return;
        }

        // Linear move: all-or-nothing across the dragged set so a group drag
        // never splits its relative layout
        const proposals = draggedPrompts.map((orig) => {
            const duration = orig.origEnd - orig.origStart;
            const maxStart = Math.max(0, totalFrames - duration);
            const newStart = Math.max(0, Math.min(maxStart, orig.origStart + frameDelta));
            return { orig, newStart, newEnd: newStart + duration };
        });
        if (proposals.every((p) => !overlapsOther(p.newStart, p.newEnd))) {
            for (const p of proposals) {
                p.orig.data.start_frame = p.newStart;
                p.orig.data.end_frame = p.newEnd;
            }
            this._dragPromptSwap = null;
            this._dragPromptHold = {
                proposals: proposals.map((p) => ({ data: p.orig.data, start: p.newStart, end: p.newEnd })),
            };
            return;
        }

        // Invalid — hold the last valid preview (swap or linear); with no
        // prior valid state the snapshot restore above already shows origins
        const hold = this._dragPromptHold;
        if (hold?.swap) {
            const s = hold.swap;
            s.dragged.data.start_frame = s.dStart;
            s.dragged.data.end_frame = s.dEnd;
            s.target.start_frame = s.tStart;
            s.target.end_frame = s.tEnd;
            this._dragPromptSwap = s;
        } else if (hold?.proposals) {
            for (const p of hold.proposals) {
                p.data.start_frame = p.start;
                p.data.end_frame = p.end;
            }
            this._dragPromptSwap = null;
        } else {
            this._dragPromptSwap = null;
        }
    }

    async _commitItemMove(frameDelta) {
        if (this.selectedItems.length === 0 || !this.activeScene || !this.projectDir) return;
        const sceneId = this.activeSceneId;

        return this._withTimelineMutationCommit("moveItem", async () => {
            try {
                if (this._hasDriverLaneCollision()) {
                    throw new Error("Only one driver clip is allowed per driver lane.");
                }
                const operations = [];
                const dragItemsOrig = this._dragItemsOrig || [];
                const draggedClipIds = new Set(dragItemsOrig.filter(o => o.type === "clip").map(o => o.id));
                const draggedAudioIds = new Set(dragItemsOrig.filter(o => o.type === "audio").map(o => o.id));
                const origClipLanes = this._origAllClipLanes || {};
                const origAudioLanes = this._origAllAudioLanes || {};
                const origClipStarts = this._origAllClipStarts || {};
                const origAudioStarts = this._origAllAudioStarts || {};
                const emittedLinkedGroups = new Set();
                const linkedAwareOperation = (hit, operation) => {
                    const group = this._linkGroupForItem(hit);
                    if (!group) return operation;
                    const anchorType = this._dragAnchorType || "";
                    const anchorId = String(this._dragAnchorId ?? "");
                    const groupHasAnchor = anchorType && (group.items || []).some((ref) => {
                        const anchorHit = this._findSceneItemForLinkRef(ref);
                        return anchorHit?.type === anchorType && String(anchorHit.id) === anchorId;
                    });
                    if (groupHasAnchor && !(hit.type === anchorType && String(hit.id) === anchorId)) {
                        return null;
                    }
                    const groupKey = group.group_id || this._linkRefKey(this._linkRefForItem(hit));
                    if (emittedLinkedGroups.has(groupKey)) return null;
                    emittedLinkedGroups.add(groupKey);
                    return { ...operation, apply_linked: true };
                };

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
                    const operation = linkedAwareOperation(
                        { type: "clip", id: clipId, data: clip },
                        { type: "update_clip", clip_id: clipId, fields }
                    );
                    if (operation) operations.push(operation);
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
                    const operation = linkedAwareOperation(
                        { type: "audio", id: trackId, data: track },
                        { type: "update_audio_track", track_id: trackId, fields }
                    );
                    if (operation) operations.push(operation);
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
                            guide_id: data.guide_id || "",
                            asset_id: data.asset_id,
                            source: data.source || "asset",
                            strength: data.strength ?? 1.0,
                            muted: !!data.muted,
                        };
                        const operation = linkedAwareOperation({ type: "guide", id, data }, {
                            type: "move_guide",
                            from_frame_index: oldIdx,
                            to_frame_index: newIdx,
                            expected: {
                                frame_index: oldIdx,
                                asset_id: data.asset_id || "",
                                guide_id: data.guide_id || "",
                            },
                            ...fields,
                        });
                        if (operation) operations.push(operation);
                        this._applyLocalMoveGuide(oldIdx, newIdx, data, fields);
                        delete data._previewFrameIndex;
                    } else if (type === "prompt") {
                        if (this._isPromptTrackLocked()) continue;
                        const swap = this._dragPromptSwap;
                        if (swap && swap.dragged.data === data) {
                            // Threshold swap commits atomically — two update ops
                            // would go stale on the server-side re-sort and trip
                            // the overlap validation's intermediate state.
                            const sections = this.activeScene?.prompt_sections || [];
                            const indexA = sections.indexOf(data);
                            const indexB = sections.indexOf(swap.target);
                            const targetSnap = (this._origAllPromptRanges || []).find((s) => s.data === swap.target);
                            if (indexA >= 0 && indexB >= 0) {
                                operations.push({
                                    type: "swap_prompt_sections",
                                    index_a: indexA,
                                    index_b: indexB,
                                    expected_a: { start_frame: orig.origStart, end_frame: orig.origEnd },
                                    expected_b: targetSnap
                                        ? { start_frame: targetSnap.start, end_frame: targetSnap.end }
                                        : undefined,
                                    fields_a: { start_frame: data.start_frame, end_frame: data.end_frame },
                                    fields_b: { start_frame: swap.target.start_frame, end_frame: swap.target.end_frame },
                                });
                            }
                        } else {
                            const operation = linkedAwareOperation({ type: "prompt", id, data }, {
                                type: "update_prompt_section",
                                index: id,
                                expected: {
                                    start_frame: orig.origStart,
                                    end_frame: orig.origEnd,
                                    prompt_id: data.prompt_id || "",
                                },
                                fields: {
                                    start_frame: data.start_frame,
                                    end_frame: data.end_frame,
                                },
                            });
                            if (operation) operations.push(operation);
                        }
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
                notifyWarning(e?.message || "Move was refused — timeline restored.", { source: "timeline-move-refused" });
                await this._fetchScenes({ ignoreMutationGate: true, reason: "moveItem_error" });
                this._renderTimeline();
            } finally {
                this._dragPromptSwap = null;
                this._dragPromptHold = null;
            }
        });
    }

    async _commitTrim(trimInfo) {
        if (!this.projectDir || !this.activeScene) return;
        const sceneId = this.activeSceneId;
        const { type, id, data, origStart, origEnd } = trimInfo;
        const applyLinked = this._isLinkedItem(trimInfo);

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
                        apply_linked: applyLinked,
                        validate_lane_collision: true,
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
                        apply_linked: applyLinked,
                        validate_lane_collision: true,
                    });
                } else if (type === "prompt") {
                    if (this._isPromptTrackLocked()) return;
                    operations.push({
                        type: "update_prompt_section",
                        index: id,
                        expected: {
                            start_frame: origStart,
                            end_frame: origEnd,
                            prompt_id: data.prompt_id || "",
                        },
                        fields: {
                            start_frame: data.start_frame,
                            end_frame: data.end_frame,
                        },
                        apply_linked: applyLinked,
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
                notifyWarning(e?.message || "Trim was refused — timeline restored.", { source: "timeline-trim-refused" });
                await this._fetchScenes({ ignoreMutationGate: true, reason: "trim_error" });
                this._renderTimeline();
            }
        });
    }

    /** Split a clip at the given frame (razor tool). */
    async _splitClipAtFrame(hit, frame) {
        if (!this.projectDir || !this.activeScene) return;
        if (hit.type !== "clip" && hit.type !== "audio" && hit.type !== "prompt") return;
        const start = hit.type === "prompt" ? hit.data.start_frame : hit.data.timeline_start_frame;
        const end = hit.type === "prompt" ? hit.data.end_frame : hit.data.timeline_end_frame;
        if (frame <= start || frame >= end) return;
        // Block split on locked lanes
        if (hit.type === "clip" && this._isLaneLocked(this._clipTrackType(hit.data), hit.data.track_index || 0)) return;
        if (hit.type === "audio" && this._isLaneLocked(TRACK_TYPE.AUDIO, hit.data.lane_index || 0)) return;
        if (hit.type === "prompt" && this._isPromptTrackLocked()) return;
        const applyLinked = this._isLinkedItem(hit);
        if (applyLinked && this._expandItemsWithLinked([hit]).some((item) => this._isItemLocked(item))) {
            notifyWarning("Split refused because one or more linked items are locked.", { source: "timeline-split-refused" });
            return;
        }
        const splitTargets = applyLinked ? this._expandItemsWithLinked([hit]) : [hit];
        if (splitTargets.some((item) => item?.type === "clip" && this._isMotionDriverClip(item.data))) {
            notifyWarning("Driver clips cannot be split.", { source: "timeline-split-refused" });
            return;
        }

        this._pushUndo(`split ${hit.type}`);
        const sceneId = this.activeSceneId;
        const operation = hit.type === "clip"
            ? { type: "split_clip", clip_id: hit.id, frame, apply_linked: applyLinked }
            : hit.type === "audio"
                ? { type: "split_audio_track", track_id: hit.id, frame, apply_linked: applyLinked }
                : {
                    type: "split_prompt_section",
                    index: hit.id,
                    frame,
                    apply_linked: applyLinked,
                    expected: {
                        start_frame: hit.data?.start_frame,
                        end_frame: hit.data?.end_frame,
                        prompt_id: hit.data?.prompt_id || "",
                    },
                };

        try {
            await this._runSceneMutation([operation], {
                key: `scene:${sceneId}:split:${hit.type}:${hit.id}:${Date.now()}`,
                label: `split ${hit.type}`,
                coalesce: false,
            });
            this._renderTimeline();
        } catch (e) {
            this._discardLastUndo(`split ${hit.type}`);
            await this._fetchScenes({ ignoreMutationGate: true, reason: "split_item_error" });
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
                img.style.cssText = "width:100%;height:100%;object-fit:contain;display:block;";
                img.title = asset?.name || asset?.path || "Guide asset";
                thumb.appendChild(img);
            }

            // Frame index input
            const frameInput = document.createElement("input");
            frameInput.type = "text";
            frameInput.inputMode = "decimal";
            frameInput.min = "0";
            frameInput.max = this._timecodeMode === "timecode"
                ? this._framesToSeconds(Math.max(0, this.totalFrames - 1)).toFixed(2)
                : String(Math.max(0, this.totalFrames - 1));
            frameInput.value = this._formatPositionInput(frame);
            frameInput.title = "Guide frame index (re-keys on commit)";
            frameInput.style.cssText = `width:54px;${chromeInputCss({ fontSize: "10px", padding: "2px 4px" })}`;
            const commitFrameInput = () => {
                const newIdx = this._parsePositionInput(frameInput.value);
                const nextFrame = Math.round(newIdx);
                if (!Number.isFinite(nextFrame) || nextFrame === frame) return;
                const clamped = Math.max(0, Math.min(this.totalFrames - 1, nextFrame));
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
                        guide_id: guide.guide_id || "",
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
                                guide_id: guide.guide_id || "",
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
                grid-template-columns: 112px 70px 72px minmax(140px, 1fr) 82px 76px minmax(150px, 188px) 58px;
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
                img.style.cssText = "width:100%;height:100%;object-fit:contain;display:block;";
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
            frameInput.type = "text";
            frameInput.inputMode = "decimal";
            frameInput.min = "0";
            frameInput.max = this._timecodeMode === "timecode"
                ? this._framesToSeconds(Math.max(0, this.totalFrames - 1)).toFixed(2)
                : String(Math.max(0, this.totalFrames - 1));
            frameInput.value = this._formatPositionInput(frame);
            frameInput.title = "Guide frame index";
            frameInput.disabled = locked;
            frameInput.style.cssText = `${chromeInputCss({ width: "66px", fontSize: "11px", padding: "5px 7px" })}`;
            const commitFrameInput = async () => {
                if (locked) return;
                const newIdx = this._parsePositionInput(frameInput.value);
                const nextFrame = Math.round(newIdx);
                if (!Number.isFinite(nextFrame) || nextFrame === frame) return;
                const clamped = Math.max(0, Math.min(this.totalFrames - 1, nextFrame));
                await this._moveGuideToFrame(guide, clamped, guide.strength);
                await refreshPanel();
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

            const replaceBtn = this._makeBtn("Replace", "Replace this guide's image with another project image");
            replaceBtn.disabled = locked;
            replaceBtn.addEventListener("click", (event) => {
                event.stopPropagation();
                if (locked) return;
                this._replaceGuideImage(guide, { refresh: false, onDone: refreshPanel });
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
                        guide_id: guide.guide_id || "",
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
                                guide_id: guide.guide_id || "",
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

            row.append(thumb, frameInput, strengthInput, label, muteBtn, replaceBtn, swapWrap, deleteBtn);
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
                // A row may expose its own right-click submenu (e.g. per-scene actions in the
                // scene switcher list). Opening it replaces this menu via _showContextMenu's
                // own _hideContextMenu(), so the native browser menu never appears.
                if (typeof item.onContextMenu === "function") {
                    row.addEventListener("contextmenu", (event) => {
                        event.preventDefault();
                        event.stopPropagation();
                        item.onContextMenu(event);
                    });
                }
            }
            menu.appendChild(row);
        }

        document.body.appendChild(menu);
        this._contextMenuEl = menu;

        // Clamp into the viewport after mount (shortest distance back inside)
        // — same rAF pattern as the gallery's showContextMenu. Fixes menus
        // opened near the right/bottom screen edges getting cropped.
        requestAnimationFrame(() => {
            if (this._contextMenuEl !== menu) return;
            const rect = menu.getBoundingClientRect();
            const clampedX = Math.max(4, Math.min(x, window.innerWidth - rect.width - 4));
            const clampedY = Math.max(4, Math.min(y, window.innerHeight - rect.height - 4));
            menu.style.left = `${clampedX}px`;
            menu.style.top = `${clampedY}px`;
        });

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
                ["I", "Anchor/set In; second endpoint snaps"],
                ["O", "Anchor/set Out; second endpoint snaps"],
                ["X", "Clear selection"],
                ["Drag empty timeline", "Select items in area"],
                ["Drag lane header", "Select lanes in area"],
                ["Click linked item", "Select the whole linked group"],
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
                ["Double-click item", "Open inline editor (isolates a linked member)"],
                ["Del / Backspace", "Delete selected items"],
                ["Ctrl+Z", "Undo"],
                ["Ctrl+Y", "Redo"],
                ["Ctrl+Shift+Z", "Redo"],
            ]) +
            this._shortcutSection("Asset Gallery", [
                ["Arrow keys", "Move asset focus / selection"],
                ["Space", "Open inspect overlay for focused asset"],
                ["Ctrl+A", "Select all visible assets"],
                ["S", "Favorite / unfavorite selected asset"],
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

    // ── Asset replacement picker ─────────────────────────────────────
    // Minimal modal listing one project asset type (name filter + lazy thumbnails),
    // calls onPick(asset_id) when one is chosen. Shared by timeline source replacement.
    _showImagePicker({
        title = "Choose an image",
        currentAssetId = "",
        onPick = () => {},
        assetType = "image",
        assetTypeLabel = "",
    } = {}) {
        this._hideImagePicker();
        const dirName = this._projectDirName();
        const typeLabel = assetTypeLabel || assetType || "asset";
        const images = (this.assets?.[assetType] || []).filter((asset) => asset && !asset.trashed && !asset.trashed_at && !asset.missing);

        const backdrop = document.createElement("div");
        backdrop.style.cssText = `position:fixed;inset:0;z-index:10002;background:rgba(7,10,14,0.80);display:flex;align-items:center;justify-content:center;padding:20px;`;
        const panel = document.createElement("div");
        panel.style.cssText = `${chromeOverlayPanelCss({
            width: "min(760px, calc(100vw - 48px))",
            maxWidth: "760px",
            maxHeight: "min(620px, calc(100vh - 48px))",
            padding: "0",
            fontFamily: "'Segoe UI', Arial, sans-serif",
        })} display:flex;flex-direction:column;`;

        const header = document.createElement("div");
        header.style.cssText = `display:flex;align-items:center;gap:12px;padding:14px 16px;border-bottom:1px solid ${COLORS.border};flex:0 0 auto;`;
        const titleEl = document.createElement("div");
        titleEl.textContent = title;
        titleEl.style.cssText = `font-size:14px;font-weight:700;color:#fff;flex:0 0 auto;`;
        const search = document.createElement("input");
        search.type = "search";
        search.placeholder = `Filter ${typeLabel}s…`;
        search.style.cssText = `${chromeInputCss({ fontSize: "12px", padding: "6px 9px", textAlign: "left" })} flex:1 1 auto;min-width:0;`;
        const closeBtn = document.createElement("button");
        closeBtn.textContent = "Close";
        closeBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "6px 12px", fontSize: "11px", radius: "7px" });
        closeBtn.addEventListener("click", () => this._hideImagePicker());
        header.append(titleEl, search, closeBtn);

        const body = document.createElement("div");
        body.style.cssText = "padding:14px 16px;overflow:auto;flex:1 1 auto;min-height:0;";
        const grid = document.createElement("div");
        grid.style.cssText = "display:grid;grid-template-columns:repeat(auto-fill, minmax(120px, 1fr));gap:10px;";
        body.appendChild(grid);

        const renderGrid = () => {
            const query = (search.value || "").trim().toLowerCase();
            grid.innerHTML = "";
            const matches = images.filter((asset) => {
                if (!query) return true;
                return (asset.name || asset.path || "").toLowerCase().includes(query);
            });
            if (!matches.length) {
                const empty = document.createElement("div");
                empty.textContent = images.length ? `No ${typeLabel}s match the filter.` : `No ${typeLabel} assets in this project.`;
                empty.style.cssText = `grid-column:1/-1;color:${COLORS.textMuted};font-size:12px;padding:24px 0;text-align:center;`;
                grid.appendChild(empty);
                return;
            }
            for (const asset of matches) {
                const isCurrent = asset.asset_id === currentAssetId;
                const card = document.createElement("div");
                card.style.cssText = `display:flex;flex-direction:column;gap:5px;padding:6px;border-radius:8px;border:1px solid ${isCurrent ? COLORS.accent : COLORS.borderSoft};background:${COLORS.panelMuted};cursor:pointer;`;
                const thumb = document.createElement("div");
                thumb.style.cssText = `aspect-ratio:16/10;border-radius:5px;background:#000;border:1px solid rgba(255,255,255,0.12);overflow:hidden;display:flex;align-items:center;justify-content:center;color:${COLORS.textMuted};font-size:10px;`;
                const thumbUrl = (asset.has_thumbnail && dirName)
                    ? api.apiURL(`/sonder-editor/project/${dirName}/thumbnail/${asset.asset_id}`)
                    : (assetType === "image" && asset.path ? this._buildViewURL(asset.path) : null);
                if (thumbUrl) {
                    const img = document.createElement("img");
                    img.src = thumbUrl;
                    img.loading = "lazy";
                    img.decoding = "async";
                    img.draggable = false;
                    img.alt = "";
                    img.style.cssText = "width:100%;height:100%;object-fit:cover;display:block;";
                    thumb.appendChild(img);
                } else {
                    thumb.textContent = assetType === "audio" ? "Audio" : (assetType === "video" ? "Video" : "No image");
                }
                const nameEl = document.createElement("div");
                nameEl.textContent = asset.name || asset.path?.split(/[/\\]/).pop() || asset.asset_id || typeLabel;
                nameEl.title = nameEl.textContent;
                nameEl.style.cssText = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#e5e9ee;font-size:11px;";
                card.append(thumb, nameEl);
                if (isCurrent) {
                    const badge = document.createElement("div");
                    badge.textContent = "Current";
                    badge.style.cssText = `color:${COLORS.accent};font-size:9px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;`;
                    card.appendChild(badge);
                }
                card.addEventListener("mouseenter", () => { card.style.borderColor = COLORS.accent; });
                card.addEventListener("mouseleave", () => { card.style.borderColor = isCurrent ? COLORS.accent : COLORS.borderSoft; });
                card.addEventListener("click", () => {
                    this._hideImagePicker();
                    onPick(asset.asset_id);
                });
                grid.appendChild(card);
            }
        };
        search.addEventListener("input", renderGrid);
        renderGrid();

        panel.append(header, body);
        backdrop.appendChild(panel);
        backdrop.addEventListener("click", (event) => { if (event.target === backdrop) this._hideImagePicker(); });
        document.body.appendChild(backdrop);
        this._imagePickerEl = backdrop;
        this._imagePickerKeyOff = registerKeyboardConsumer({
            id: this._keyboardConsumerId("imgpicker"),
            priority: KEY_PRIORITY.OVERLAY,
            keydown: (e) => {
                if (e.key === "Escape") { this._hideImagePicker(); return true; }
                return false;
            },
        });
        setTimeout(() => { try { search.focus(); } catch { /* ignore */ } }, 0);
    }

    _hideImagePicker() {
        if (this._imagePickerEl) {
            this._imagePickerEl.remove();
            this._imagePickerEl = null;
        }
        if (this._imagePickerKeyOff) {
            this._imagePickerKeyOff();
            this._imagePickerKeyOff = null;
        }
    }

    // Replace a guide frame's image with another project image. Shared by both surfaces.
    // The guide updates in place (same guide_id / frame_index / fit_mode / crop_position);
    // only asset_id + source change. Pass onDone (e.g. the Guide popup's refreshPanel) to
    // re-render that surface; the timeline path relies on the mutation's own scene refresh.
    _replaceGuideImage(guide, { refresh = true, onDone = null } = {}) {
        if (!guide) return;
        if (this._isGuideTrackLocked()) {
            notifyWarning("Guide track is locked.", { source: "guide-replace-locked" });
            return;
        }
        this._showImagePicker({
            title: "Replace guide with…",
            currentAssetId: guide.asset_id || "",
            onPick: async (assetId) => {
                if (!assetId || assetId === guide.asset_id) return;
                this._pushUndo("replace guide");
                try {
                    await this._updateItemProperty(
                        "guide",
                        guide.frame_index,
                        { asset_id: assetId, source: "asset" },
                        { refresh },
                    );
                    if (onDone) await onDone();
                } catch (e) {
                    this._discardLastUndo("replace guide");
                    console.warn("[Sonder] Failed to replace guide:", e);
                }
            },
        });
    }

    _replaceClipSource(clip) {
        if (!clip?.clip_id || !this.activeScene || !this.projectDir) return;
        const hit = this._findSceneItemBySelection("clip", clip.clip_id) || { type: "clip", id: clip.clip_id, data: clip };
        if (this._isItemLocked(hit)) {
            notifyWarning("Clip lane is locked.", { source: "clip-replace-locked" });
            return;
        }
        const currentAsset = this._getAssetForSourcePath(clip.source_path);
        this._showImagePicker({
            title: "Replace clip with…",
            currentAssetId: currentAsset?.asset_id || "",
            assetType: "video",
            assetTypeLabel: "video",
            onPick: async (assetId) => {
                if (!assetId || assetId === currentAsset?.asset_id) return;
                const undoLabel = "replace clip";
                this._pushUndo(undoLabel);
                try {
                    await this._runSceneMutation(
                        [{ type: "replace_clip_source", clip_id: clip.clip_id, asset_id: assetId }],
                        {
                            key: `clip:${clip.clip_id}:replace-source:${Date.now()}`,
                            label: undoLabel,
                            coalesce: false,
                        },
                    );
                    this._renderTimeline();
                    this._renderViewportFrame();
                } catch (e) {
                    this._discardLastUndo(undoLabel);
                    console.warn("[Sonder] Failed to replace clip source:", e);
                }
            },
        });
    }

    _replaceAudioSource(track) {
        if (!track?.track_id || !this.activeScene || !this.projectDir) return;
        const hit = this._findSceneItemBySelection("audio", track.track_id) || { type: "audio", id: track.track_id, data: track };
        if (this._isItemLocked(hit)) {
            notifyWarning("Audio lane is locked.", { source: "audio-replace-locked" });
            return;
        }
        const currentAsset = this._getAssetForSourcePath(track.source_path);
        this._showImagePicker({
            title: "Replace audio with…",
            currentAssetId: currentAsset?.asset_id || "",
            assetType: "audio",
            assetTypeLabel: "audio",
            onPick: async (assetId) => {
                if (!assetId || assetId === currentAsset?.asset_id) return;
                const undoLabel = "replace audio";
                this._pushUndo(undoLabel);
                try {
                    await this._runSceneMutation(
                        [{ type: "replace_audio_source", track_id: track.track_id, asset_id: assetId }],
                        {
                            key: `audio:${track.track_id}:replace-source:${Date.now()}`,
                            label: undoLabel,
                            coalesce: false,
                        },
                    );
                    this._renderTimeline();
                    this._renderViewportFrame();
                } catch (e) {
                    this._discardLastUndo(undoLabel);
                    console.warn("[Sonder] Failed to replace audio source:", e);
                }
            },
        });
    }

    // ── Identity zone (brand › project › scene breadcrumb) ───────────
    // Replaces the old static "Editor — <scene>" title. Lives only in the
    // fullscreen/mounted top toolbar; the EditorWidget is always fullscreen
    // (the dormant card is a separate controller with no scene bar), so the
    // in-bar scene-selector group is hidden in _enterFullscreen.
    _buildFullscreenIdentityZone() {
        const zone = document.createElement("div");
        zone.style.cssText = `display: flex; align-items: center; min-width: 0; flex-shrink: 1; overflow: hidden;`;

        const brand = document.createElement("div");
        brand.style.cssText = `display: flex; flex-direction: column; line-height: 1; margin-right: 14px; flex-shrink: 0; user-select: none;`;
        const brandTop = document.createElement("span");
        brandTop.textContent = "SONDER";
        brandTop.style.cssText = `font-size: 17px; font-weight: 600; letter-spacing: 0.12em; color: ${COLORS.text};`;
        const brandSub = document.createElement("span");
        brandSub.textContent = "EDITOR";
        brandSub.style.cssText = `font-size: 10px; letter-spacing: 0.46em; color: ${COLORS.accent}; margin-top: 3px;`;
        brand.append(brandTop, brandSub);

        const divider = document.createElement("span");
        divider.style.cssText = `width: 1px; height: 20px; background: ${COLORS.border}; margin-right: 12px; flex-shrink: 0;`;

        this._fsProjectPill = document.createElement("span");
        this._fsProjectPill.style.cssText = `
            display: inline-flex; align-items: center; gap: 6px; flex-shrink: 1; min-width: 0;
            background: ${COLORS.panelRaised}; border: 1px solid ${COLORS.border}; border-radius: 7px;
            padding: 4px 9px; font-size: 12px; font-weight: 500; color: ${COLORS.text}; cursor: pointer;
        `;
        this._fsProjectPill.title = "Project options";
        this._fsProjectLabel = document.createElement("span");
        this._fsProjectLabel.style.cssText = `overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0;`;
        this._fsProjectLabel.textContent = this._projectDirName() || "No Project";
        const projectCaret = document.createElement("span");
        projectCaret.textContent = "▾";
        projectCaret.style.cssText = `font-size: 10px; color: ${COLORS.textDim}; flex-shrink: 0;`;
        this._fsProjectPill.append(this._fsProjectLabel, projectCaret);
        const openProjectMenu = (ev) => { ev.preventDefault(); ev.stopPropagation(); this._showProjectMenu(this._fsProjectPill); };
        this._fsProjectPill.addEventListener("click", openProjectMenu);
        this._fsProjectPill.addEventListener("contextmenu", openProjectMenu);

        const chevron = document.createElement("span");
        chevron.textContent = "›";
        chevron.style.cssText = `font-size: 14px; color: ${COLORS.textMuted}; margin: 0 8px; flex-shrink: 0;`;

        const sceneWrap = document.createElement("div");
        sceneWrap.style.cssText = `display: inline-flex; align-items: center; gap: 2px; flex-shrink: 0;`;
        const prev = this._makeBtn("‹", "Previous scene");
        prev.style.cssText += `font-size: 15px; padding: 1px 6px; color: ${COLORS.textDim};`;
        prev.addEventListener("click", () => this._cycleScene(-1));
        const scenePill = document.createElement("span");
        scenePill.style.cssText = `
            display: inline-flex; align-items: center; gap: 6px;
            background: ${COLORS.accentSoft}; border: 1px solid ${COLORS.accentLo}; border-radius: 7px;
            padding: 4px 9px; font-size: 12px; font-weight: 500; color: ${COLORS.text}; cursor: pointer;
            max-width: 240px; overflow: hidden; white-space: nowrap;
        `;
        scenePill.title = "Click to switch scene · Right-click to rename / duplicate / delete";
        this._fsSceneLabel = document.createElement("span");
        this._fsSceneLabel.style.cssText = `overflow: hidden; text-overflow: ellipsis; white-space: nowrap;`;
        this._fsSceneLabel.textContent = this.activeScene?.name || "No Scene";
        const sceneCaret = document.createElement("span");
        sceneCaret.textContent = "▾";
        sceneCaret.style.cssText = `font-size: 10px; color: ${COLORS.accentHi}; flex-shrink: 0;`;
        scenePill.append(this._fsSceneLabel, sceneCaret);
        scenePill.addEventListener("click", () => this._showSceneDropdown(scenePill));
        scenePill.addEventListener("contextmenu", (e) => {
            e.preventDefault(); e.stopPropagation();
            this._showSceneRowMenu(e, this.activeScene);
        });
        const next = this._makeBtn("›", "Next scene");
        next.style.cssText += `font-size: 15px; padding: 1px 6px; color: ${COLORS.textDim};`;
        next.addEventListener("click", () => this._cycleScene(1));
        sceneWrap.append(prev, scenePill, next);

        const addScene = this._makeBtn("+ Scene", "Create new scene");
        addScene.style.cssText += `font-size: 12px; padding: 3px 8px; margin-left: 8px; color: ${COLORS.textDim}; flex-shrink: 0;`;
        addScene.addEventListener("click", () => this._createScene());

        zone.append(brand, divider, this._fsProjectPill, chevron, sceneWrap, addScene);
        return zone;
    }

    _updateSceneIdentity(name) {
        const text = name || "No Scene";
        if (this.sceneLabel) this.sceneLabel.textContent = text;
        if (this._fsSceneLabel) this._fsSceneLabel.textContent = text;
    }

    _updateProjectIdentity() {
        if (this._fsProjectLabel) this._fsProjectLabel.textContent = this._projectDirName() || "No Project";
    }

    _showSceneDropdown(anchorEl) {
        if (!this.scenes?.length) return;
        const rect = anchorEl.getBoundingClientRect();
        const items = this.scenes.map((scene) => {
            const isActive = scene.scene_id === this.activeSceneId;
            return {
                label: `${isActive ? "✓ " : " "}${scene.name || "Untitled Scene"}`,
                action: () => { if (scene.scene_id !== this.activeSceneId) this._setActiveScene(scene); },
                onContextMenu: (event) => this._showSceneRowMenu(event, scene),
            };
        });
        items.push({ type: "separator" });
        items.push({ label: "+ New scene", action: () => this._createScene() });
        this._showContextMenu(rect.left, rect.bottom + 4, items);
    }

    // Per-scene actions, shared by the breadcrumb pill (active scene) and the switcher list
    // rows (any scene). The scene methods accept a target so a non-active scene can be managed
    // without switching to it first.
    _showSceneRowMenu(event, scene) {
        if (!scene) return;
        this._showContextMenu(event.clientX, event.clientY, [
            { label: "Rename Scene", action: () => this._renameScene(scene) },
            { label: "Duplicate Scene", action: () => this._duplicateScene(scene) },
            { label: "Delete Scene", action: () => this._deleteScene(scene), danger: true },
        ]);
    }

    async _showProjectMenu(anchorEl) {
        try {
            const projects = await this._listProjectEntries();
            const activeName = this._projectDirName();
            this._activeProjectLinked = projects.some((project) => (
                this._projectFolderName(project) === activeName && project?.linked === true
            ));
        } catch (_) {
            // Keep the existing project actions available when the list request fails.
        }
        const rect = anchorEl.getBoundingClientRect();
        const items = [
            { label: "Reveal projects folder", action: () => this._revealProjectFolder() },
            { label: "Copy folder path", action: () => this._copyProjectPath() },
            { type: "separator" },
            { label: "Link project folder\u2026", action: () => this._linkProjectFolder() },
        ];
        if (this._activeProjectLinked) {
            items.push({ label: "Unlink this project", action: () => this._unlinkActiveProject(), danger: true });
        }
        this._showContextMenu(rect.left, rect.bottom + 4, items);
    }

    /** Project-durable render-affecting guide collision policy. Snapshot jobs
     * freeze it at enqueue so queued execution stays reproducible. */
    async _toggleGuideCollisionAutoOffset(on) {
        const dirName = this._projectDirName();
        if (!dirName) return;
        try {
            await this._runVersionedProjectMutation(
                `/sonder-editor/project/${encodeURIComponent(dirName)}`,
                {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ metadata: { guide_collision_auto_offset: !!on } }),
                },
                { projectId: dirName }
            );
            this._guideCollisionAutoOffset = !!on;
            this._syncSettingsPanelControls();
        } catch (e) {
            this._syncSettingsPanelControls();
            notifyWarning(e?.message || "Failed to update guide collision auto-offset.", {
                source: "guide-collision-toggle-refused",
            });
            throw e;
        }
    }

    _warnForGuideCollisionPredictions(jobs) {
        const predictions = (Array.isArray(jobs) ? jobs : [jobs])
            .map((job) => job?.params?.guide_collision_prediction)
            .filter((value) => value && value.predicted_unresolved === true);
        if (!predictions.length) return;
        const driverCollisions = predictions.reduce(
            (total, value) => total + (parseInt(value.driver_driver_collision_count, 10) || 0), 0
        );
        const message = driverCollisions > 0
            ? "Multiple drivers share LTX temporal coordinates. Drivers are not moved; disable or reposition one to avoid guide-crop undercount and tail bleed."
            : "A guide shares an LTX temporal coordinate with another injection. Enable guide collision auto-offset or move the guide to avoid tail bleed.";
        notifyWarning(message, { source: "guide-collision-predicted" });
    }

    _projectFolderName(project) {
        return String(project?.path || "").split(/[/\\]/).pop() || "";
    }

    async _listProjectEntries() {
        const resp = await fetch(api.apiURL("/sonder-editor/projects"));
        if (!resp.ok) throw new Error(`Failed to list projects: ${resp.status}`);
        const data = await resp.json();
        return Array.isArray(data?.projects) ? data.projects : [];
    }

    async _refreshProjectChoicesAndSwitch(preferredFolder = "") {
        const projects = await this._listProjectEntries();
        const projectNames = projects.map((project) => this._projectFolderName(project)).filter(Boolean);
        const currentFolder = this._projectDirName();
        this._activeProjectLinked = projects.some((project) => (
            this._projectFolderName(project) === currentFolder && project?.linked === true
        ));

        const projectWidget = this.node?.widgets?.find((widget) => widget.name === "project");
        if (projectWidget) {
            projectWidget.options = projectWidget.options || {};
            projectWidget.options.values = ["+ Create New", ...projectNames];
        }

        const targetFolder = projectNames.includes(preferredFolder)
            ? preferredFolder
            : (projectNames.includes(currentFolder) ? currentFolder : (projectNames[0] || "+ Create New"));
        const targetProject = projects.find((project) => this._projectFolderName(project) === targetFolder) || null;

        if (projectWidget && typeof projectWidget.callback === "function") {
            if (projectWidget.value !== targetFolder) {
                projectWidget.value = targetFolder;
                projectWidget.callback(targetFolder);
            }
        } else if (targetProject?.path && targetProject.path !== this.projectDir) {
            this._clearProjectNotFound();
            this.updateProject(targetProject.path);
        }
        return projects;
    }

    async _loadServerSettings() {
        this._serverSettingsLoaded = false;
        this._syncSettingsPanelControls();
        try {
            const resp = await fetch(api.apiURL("/sonder-editor/server-settings"));
            if (!resp.ok) throw new Error(`Failed to load server settings: ${resp.status}`);
            const data = await resp.json();
            if (typeof data?.allow_external_project_links !== "boolean") {
                throw new Error("Server settings response was invalid");
            }
            this._serverSettings = data;
            this._serverSettingsLoaded = true;
            return data;
        } catch (error) {
            console.warn("[Sonder] Failed to load server settings:", error);
            this._serverSettings = null;
            this._serverSettingsLoaded = false;
            return null;
        } finally {
            this._syncSettingsPanelControls();
        }
    }

    async _setAllowExternalProjectLinks(enabled) {
        const requested = enabled === true;
        try {
            const resp = await fetch(api.apiURL("/sonder-editor/server-settings"), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ allow_external_project_links: requested }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data?.error || "Could not update server setting");
            this._serverSettings = data;
            this._serverSettingsLoaded = true;
            await this._refreshProjectChoicesAndSwitch();
            notifySuccess(requested ? "External project links enabled" : "External project links disabled");
        } catch (error) {
            notifyError(error?.message || "Could not update external project links", { source: "external-project-links" });
        } finally {
            this._syncSettingsPanelControls();
        }
    }

    async _linkProjectFolder() {
        const targetPath = prompt("Paste the path of an existing Sonder project folder:", "");
        if (!targetPath?.trim()) return;
        try {
            const resp = await fetch(api.apiURL("/sonder-editor/projects/link"), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ path: targetPath.trim() }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data?.error || "Could not link project folder");
            const folder = String(data?.project?.name || "");
            await this._refreshProjectChoicesAndSwitch(folder);
            notifySuccess("Project folder linked");
        } catch (error) {
            notifyError(error?.message || "Could not link project folder", { source: "external-project-links" });
        }
    }

    async _unlinkActiveProject() {
        const projectId = this._projectDirName();
        if (!projectId) return;
        try {
            const resp = await fetch(api.apiURL("/sonder-editor/projects/unlink"), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ project_id: projectId }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data?.error || "Could not unlink project folder");
            await this._refreshProjectChoicesAndSwitch();
            notifySuccess("Project link removed; external files were left untouched");
        } catch (error) {
            notifyError(error?.message || "Could not unlink project folder", { source: "external-project-links" });
        }
    }

    async _copyProjectPath() {
        const path = this.projectDir || "";
        if (!path) { notifyWarning("No project folder to copy"); return; }
        try {
            await navigator.clipboard.writeText(path);
            notifyInfo("Folder path copied");
        } catch (e) {
            notifyWarning("Could not copy folder path");
        }
    }

    async _revealProjectFolder() {
        const dirName = this._projectDirName();
        if (!dirName) { notifyWarning("No project folder to open"); return; }
        try {
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${encodeURIComponent(dirName)}/reveal`), { method: "POST" });
            if (!resp.ok) {
                const data = await resp.json().catch(() => ({}));
                notifyWarning(data?.error || "Could not open folder — use Copy folder path");
            }
        } catch (e) {
            notifyWarning("Could not open folder — use Copy folder path");
        }
    }

    // ── Project-not-found recovery (ephemeral session UI) ────────────
    _showProjectNotFound() {
        if (this._projectMissingEl) return;
        this._projectMissing = true;
        const host = this._fsContent || this._fullscreenOverlay || this.container;
        if (!host) return;
        if (!host.style.position || host.style.position === "static") host.style.position = "relative";
        const panel = document.createElement("div");
        panel.style.cssText = `
            position: absolute; inset: 0; z-index: 50;
            display: flex; align-items: center; justify-content: center;
            background: ${COLORS.bg};
        `;
        const card = document.createElement("div");
        card.style.cssText = `
            max-width: 440px; padding: 24px; text-align: center;
            background: ${COLORS.panel}; border: 1px solid ${COLORS.border}; border-radius: 10px;
            color: ${COLORS.text}; font-size: 13px;
        `;
        const title = document.createElement("div");
        title.style.cssText = `font-size: 15px; font-weight: 600; margin-bottom: 8px;`;
        title.textContent = "Project not found";
        const msg = document.createElement("div");
        msg.style.cssText = `color: ${COLORS.textDim}; margin-bottom: 16px; line-height: 1.5;`;
        const boundName = this._projectDirName() || "(unknown)";
        msg.textContent = `The project folder “${boundName}” could not be opened — it may have been renamed or moved. Pick a project to continue:`;
        const pickBtn = this._makeBtn("Choose a project…", "Select an available project");
        pickBtn.style.cssText += `font-size: 13px; padding: 6px 16px;`;
        pickBtn.addEventListener("click", (e) => this._showProjectRecoveryPicker(e.currentTarget));
        card.append(title, msg, pickBtn);
        panel.appendChild(card);
        host.appendChild(panel);
        this._projectMissingEl = panel;
    }

    _clearProjectNotFound() {
        this._projectMissing = false;
        if (this._projectMissingEl) {
            this._projectMissingEl.remove();
            this._projectMissingEl = null;
        }
    }

    async _showProjectRecoveryPicker(anchorEl) {
        let projects = [];
        try {
            const resp = await fetch(api.apiURL("/sonder-editor/projects"));
            if (resp.ok) {
                const data = await resp.json();
                projects = data.projects || [];
            }
        } catch (e) { /* ignore */ }
        const rect = (anchorEl || this._projectMissingEl)?.getBoundingClientRect?.() || { left: 200, bottom: 200 };
        if (!projects.length) {
            this._showContextMenu(rect.left, rect.bottom + 4, [{ label: "No projects found", disabled: true }]);
            return;
        }
        const items = projects.map((p) => ({
            label: String(p.path || "").split(/[/\\]/).pop() || p.name || "(unnamed)",
            action: () => this._recoverToProject(p),
        }));
        this._showContextMenu(rect.left, rect.bottom + 4, items);
    }

    _recoverToProject(project) {
        const folder = String(project?.path || "").split(/[/\\]/).pop();
        if (!folder) return;
        // Canvas node: re-point the `project` widget through its existing combo
        // callback (the same switch path the user uses manually). Fire-and-forget
        // — controller.updateProject calls session.destroy() and rebuilds under
        // the new project, so do NOT touch this.* afterward.
        const widgets = this.node?.widgets;
        if (Array.isArray(widgets)) {
            const projectWidget = widgets.find((w) => w.name === "project");
            if (projectWidget && typeof projectWidget.callback === "function") {
                projectWidget.value = folder;
                projectWidget.callback(folder);
                return;
            }
        }
        // Mounted tab (widget-less stub node): editor-local rebind for recovery.
        if (project?.path) {
            this._clearProjectNotFound();
            this.updateProject(project.path);
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
        applyNativeControlTheme(overlay);

        // Toolbar
        const toolbar = document.createElement("div");
        toolbar.style.cssText = `
            display: flex; align-items: center; padding: 0 12px;
            height: 42px; background: ${COLORS.panel}; border-bottom: 1px solid ${COLORS.border};
            flex-shrink: 0;
        `;

        this._fsTitle = this._buildFullscreenIdentityZone();

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
            width: ${this._defaultFullscreenSidebarWidth()}px; min-width: ${FULLSCREEN_SIDEBAR_MIN_WIDTH}px; max-width: ${this._computeFullscreenSidebarMaxWidth()}px;
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
        this._setupResizeHandle(
            timelineHandle, this._fsBottomRow, "height",
            // Toolbar-aware minimum: the panel cannot be dragged below measured chrome
            // plus the renderer's visible canvas floor.
            () => this._measureFullscreenTimelineChrome().minBottomH,
            () => {
                const metrics = this._measureFullscreenTimelineChrome();
                return Math.max(this._computeFullscreenTimelineMaxHeight(), metrics.minBottomH);
            },
            true, "timeline-height");
        this._fsBottomRow.appendChild(timelineHandle);

        this._fsContent.append(this._fsTopRow, this._fsBottomRow);
        overlay.append(toolbar, this._fsContent);
        document.body.appendChild(overlay);
        this._fullscreenOverlay = overlay;
        this._applyEditorMargins();
    }

    // Browser-local editor margins (appearance.editorMargins): inset the whole
    // fullscreen / mounted-tab shell from the screen edges. The overlay is
    // `inset: 0`, so padding keeps the border-box pinned to the viewport while
    // insetting the toolbar + content; the band shows COLORS.bg. Shared by both
    // surfaces because the tab reuses this same overlay.
    _applyEditorMargins(settings = this._settings) {
        if (!this._fullscreenOverlay) return;
        const m = settings?.appearance?.editorMargins || { top: 0, bottom: 0, sides: 0 };
        this._fullscreenOverlay.style.boxSizing = "border-box";
        this._fullscreenOverlay.style.padding = `${m.top}px ${m.sides}px ${m.bottom}px ${m.sides}px`;
    }

    _setupResizeHandle(handle, target, prop, min, max, invert = false, persistKey = "") {
        let startPos = 0;
        let startSize = 0;

        const onMouseMove = (e) => {
            const maxValue = typeof max === "function" ? max() : max;
            const minValue = typeof min === "function" ? min() : min;
            const delta = prop === "width"
                ? e.clientX - startPos
                : e.clientY - startPos;
            const newSize = invert
                ? Math.max(minValue, Math.min(maxValue, startSize - delta))
                : Math.max(minValue, Math.min(maxValue, startSize + delta));
            if (persistKey === "timeline-height" && target === this._fsBottomRow && prop === "height") {
                this._applyFullscreenTimelineHeight(newSize);
            } else {
                target.style[prop] = newSize + "px";
            }

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
            const sidebarWidth = parseInt(getComputedStyle(this._fsSidebar).width, 10) || this._defaultFullscreenSidebarWidth();
            this._fsSidebar.style.maxWidth = `${sidebarMax}px`;
            this._fsSidebar.style.width = `${Math.max(FULLSCREEN_SIDEBAR_MIN_WIDTH, Math.min(sidebarMax, sidebarWidth))}px`;
        }
        // Keep the toolbar wrapper's reserved height current. CSS transform does not
        // reserve flow space, so the panel clamp must account for measured toolbar chrome.
        const metrics = this._measureFullscreenTimelineChrome();
        const currentH = this._fsBottomRow
            ? (parseInt(getComputedStyle(this._fsBottomRow).height, 10) || this._defaultFullscreenTimelineHeight())
            : this._defaultFullscreenTimelineHeight();
        const clamped = this._applyFullscreenTimelineHeight(currentH, metrics);
        // Clamp the panel to its valid range; this is idempotent during a drag.
        const bottomH = clamped?.height || currentH;
        // Timeline canvas fills the bottom row minus the chrome.
        this._timelineHeight = Math.max(
            metrics.visibleCanvasMin,
            bottomH - metrics.paddingY - metrics.chromeH
        );
        this._clampScrollY();
        // Gallery is in the sidebar now, doesn't need height calc
        this._galleryHeight = GALLERY_HEIGHT; // Not used in fullscreen layout
    }

    _enterFullscreen() {
        // Module-level guard: only one fullscreen at a time
        if (EditorWidget._activeFullscreen && EditorWidget._activeFullscreen !== this) return;

        this._createFullscreenOverlay();
        const savedSidebarWidth = this._readFullscreenPersistValue("sidebar-width");
        if (savedSidebarWidth && this._fsSidebar) {
            const sidebarMax = this._computeFullscreenSidebarMaxWidth();
            this._fsSidebar.style.width = `${Math.max(FULLSCREEN_SIDEBAR_MIN_WIDTH, Math.min(sidebarMax, savedSidebarWidth))}px`;
        }

        // Save position and node size for re-insertion
        this._nodeParent = this.container.parentElement;
        this._nodeSibling = this.container.nextSibling;
        this._savedNodeSize = this.widgetHost?.getSize?.() || null;

        // Reparent: gallery goes to sidebar, rest of container goes to bottom row
        // Save gallery's position in container for restoration
        this._galleryNextSibling = this.galleryEl.nextSibling;

        // Sidebar keeps a stable panel title; project identity lives in the breadcrumb pill.
        if (this._fsSidebarHeader) this._fsSidebarHeader.textContent = "Assets";

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

        // Identity zone owns scene + project; hide the redundant in-bar scene
        // selector (the EditorWidget is always fullscreen — no dormant scene bar).
        if (this._sceneSelectGroup) this._sceneSelectGroup.style.display = "none";
        this._updateProjectIdentity();
        this._updateSceneIdentity(this.activeScene?.name || "No Scene");

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
        this._sweepRenderCache();

        // ENTER must run _applyScales so saved/default panel height is clamped only after
        // the editor is mounted in the visible fullscreen bottom row.
        this._applyScales();

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
            // Prompt-editing fields (panel boxes + inline section/global/draft
            // bars) own their own native Ctrl+Z; never route it to timeline undo.
            const isPromptPanelInput = !!document.activeElement?.closest?.("[data-sonder-prompt-box='1']");
            const debugUndoRouting = (message, extra = {}) => {
                if (!ctrl || (normalizedKey !== "z" && normalizedKey !== "y")) return;
                this._keyboardDebug(message, this._keyboardDebugSnapshot(e, extra));
            };
            if (isInspectOverlayInput) return false;
            if ((tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") && (!isUndo || isPromptPanelInput)) return false;

            // Guard: only handle keys when our editor is focused
            // (fullscreen always focused, node mode only when user clicked inside)
            if (!this.isFullscreen && !this._editorFocused) {
                debugUndoRouting("skip undo routing: editor not focused");
                return false;
            }

            // ── Escape ──
            if (key === "Escape") {
                if (this.isFullscreen) { void this._requestExitFullscreen({ reason: "escape" }); return true; }
                if (this.selectedItems.length > 0 || (this._selectedLanes || []).length > 0) {
                    this.selectedItems = [];
                    this._clearLaneSelection();
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
                    this._deleteSelectedItems();
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
                this._commitManualSelectionEndpoint("start", this.playhead);
                return true;
            }
            if (key === "o" || key === "O") {
                this._commitManualSelectionEndpoint("end", this.playhead);
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

    _ensureFrameVisible(frame, { center = false } = {}) {
        const target = Math.max(0, Math.min(this.totalFrames, Math.round(Number(frame) || 0)));
        const visibleFrames = this._visibleTimelineFrameSpan();
        if (center) {
            this.scrollX = target - (visibleFrames / 2);
            this._clampScrollX();
            return;
        }
        const marginFrames = Math.max(2, Math.floor(visibleFrames * 0.12));
        const leftBound = this.scrollX + marginFrames;
        const rightBound = this.scrollX + visibleFrames - marginFrames;
        if (target < leftBound) {
            this.scrollX = target - marginFrames;
        } else if (target > rightBound) {
            this.scrollX = target - visibleFrames + marginFrames;
        }
        this._clampScrollX();
    }

    _onPlayheadChange() {
        this._ensureFrameVisible(this.playhead);
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

    // Reserve the toolbar's scaled height on its wrapper. `transform:scale` is
    // visual-only in this engine, so the normal-flow wrapper carries the rendered
    // toolbar height that pushes the timeline down.
    // PURE: sets transform/width on the toolbar + height on the wrapper, returns the
    // reserved px. Must NOT call _recalcFullscreenHeights (no recursion).
    _reserveToolbarHeight() {
        if (!this._toolbar || !this._toolbarWrap) return 0;
        const st = this._scaleToolbar;
        if (st === 1.0) {
            this._toolbar.style.transform = "";
            this._toolbar.style.width = "";
            this._toolbarWrap.style.height = "";
            return this._toolbar.offsetHeight;
        }
        this._toolbar.style.transformOrigin = "top left";
        this._toolbar.style.transform = `scale(${st})`;
        this._toolbar.style.width = `${100 / st}%`; // narrower box re-wraps; scale fills width
        // getBoundingClientRect reflects the transform (already scaled) + the re-wrap.
        const h = Math.ceil(this._toolbar.getBoundingClientRect().height);
        this._toolbarWrap.style.height = `${h}px`;
        return h;
    }

    _applyScales() {
        // Toolbar: reserve its scaled height on the wrapper.
        this._reserveToolbarHeight();
        // Asset gallery: CSS zoom.
        if (this.galleryEl) {
            const sg = this._scaleGallery;
            this.galleryEl.style.zoom = sg !== 1.0 ? sg : "";
        }
        // Clamp the saved/default bottom-panel height after toolbar reservation so
        // toolbar scaling can increase the measured minimum without persisting it.
        if (this.isFullscreen && this._fsBottomRow) {
            const userH = this._settings?.layout?.fullscreenTimelineHeight > 0
                ? this._settings.layout.fullscreenTimelineHeight
                : this._defaultFullscreenTimelineHeight();
            this._applyFullscreenTimelineHeight(userH);
        }
        if (this.isFullscreen) this._recalcFullscreenHeights();
        this._renderTimeline();
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
        let label = mode === "duration_only" ? durationText : (mode === "name_only" ? name : `${name} | ${durationText}`);
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
        let label = mode === "duration_only" ? durationText : (mode === "name_only" ? name : `${name} | ${durationText}`);
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
        const draft = this._selectionDraftAnchor;
        const hasSel = !draft && this.selectionStart < this.selectionEnd;
        saveItem.style.cssText = `
            padding: 6px 10px; cursor: ${hasSel ? "pointer" : "default"};
            color: ${hasSel ? lightenColor(COLORS.sceneBtnActive, 0.28) : COLORS.textMuted}; border-bottom: 1px solid ${COLORS.border};
        `;
        saveItem.textContent = hasSel
            ? `💾 Save Selection (${this._frameToTimecode(this.selectionStart)}–${this._frameToTimecode(this.selectionEnd)})`
            : draft
                ? `💾 Save Selection (choose ${draft.edge === "start" ? "Out" : "In"})`
                : "💾 Save Selection (no selection)";
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
                const maskPre = Math.max(0, parseInt(sel.mask_pre_offset, 10) || 0);
                const maskPost = Math.max(0, parseInt(sel.mask_post_offset, 10) || 0);
                const ctxSuffix = (preCtx > 0 || postCtx > 0) ? ` | Ctx -${preCtx}/+${postCtx}` : "";
                const maskSuffix = (maskPre > 0 || maskPost > 0) ? ` | Mask -${maskPre}/+${maskPost}` : "";
                label.textContent = `${sel.name} (${this._frameToTimecode(sel.start)}–${this._frameToTimecode(sel.end)}${ctxSuffix})`;
                if (maskSuffix) {
                    label.textContent = label.textContent.replace(/\)$/, `${maskSuffix})`);
                }
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
        if (this._selectionDraftAnchor) {
            notifyWarning(
                `Choose ${this._selectionDraftAnchor.edge === "start" ? "Out" : "In"} before saving the selection.`,
                { source: "selection-draft-save-guard" }
            );
            return;
        }
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
                    mask_pre_offset: this._contextFrameValue("mask_pre_offset"),
                    mask_post_offset: this._contextFrameValue("mask_post_offset"),
                }),
            });
            if (resp.ok) {
                await this._fetchScenes();
                notifySuccess(`Saved selection "${name.trim()}"`);
            } else {
                notifyError("Failed to save selection.");
            }
        } catch (e) {
            console.error("Save selection failed:", e);
            notifyError("Failed to save selection.");
        }
    }

    _recallSavedSelection(sel) {
        this._setTimelineSelection(sel.start, sel.end, { render: false });
        this._setWidgetValue("pre_context_frames", Math.max(0, parseInt(sel.pre_context_frames, 10) || 0));
        this._setWidgetValue("post_context_frames", Math.max(0, parseInt(sel.post_context_frames, 10) || 0));
        this._setWidgetValue("mask_pre_offset", Math.max(0, parseInt(sel.mask_pre_offset, 10) || 0));
        this._setWidgetValue("mask_post_offset", Math.max(0, parseInt(sel.mask_post_offset, 10) || 0));
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
        if (!this.activeScene || this._selectionDraftAnchor) return null;
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
        // Closing the panel cancels any in-flight job below, so drop a lingering
        // progress notification (completion nulls it first, so this only fires on
        // genuine close/cancel).
        if (this._exportNotif) {
            this._exportNotif.dismiss();
            this._exportNotif = null;
        }
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

    // Determinate frame counter from the export status payload, or null
    // (indeterminate) when the backend hasn't reported frame counts.
    _exportProgressFromData(data) {
        const total = Number(data?.frames_total);
        const done = Number(data?.frames_done);
        if (Number.isFinite(total) && total > 0 && Number.isFinite(done)) {
            return { current: Math.max(0, Math.min(total, done)), total, unit: "f" };
        }
        return null;
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
            // Global progress surface (toast + foreground pill) for the export.
            // The panel's own progressEl stays as the modal-local affordance.
            this._exportNotif = notifyProgress({
                verb: "Exporting",
                message: this._exportPhaseMessage(data),
                progress: this._exportProgressFromData(data),
                foreground: true,
                source: "export",
            });
            ui.progressEl.textContent = this._exportPhaseMessage(data);
            this._pollTimelineExport(ui);
        } catch (error) {
            this._exportStartPending = false;
            if (this._exportPanelToken !== token) return;
            const cancelRequested = this._exportCancelRequested;
            this._exportCancelRequested = false;
            this._exportJobId = "";
            if (cancelRequested) {
                if (this._exportNotif) { this._exportNotif.dismiss(); this._exportNotif = null; }
                this._resetExportControlsAfterCancel(ui);
                return;
            }
            if (this._exportNotif) {
                this._exportNotif.resolve({ tier: "error", message: error?.message || "Export failed." });
                this._exportNotif = null;
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
                this._exportNotif?.update({
                    message: this._exportPhaseMessage(data),
                    progress: this._exportProgressFromData(data),
                });
                if (data.status === "completed") {
                    await this._handleTimelineExportComplete(data);
                    return;
                }
                if (data.status === "failed") {
                    throw new Error(data.error || "Export failed.");
                }
                if (data.status === "cancelled") {
                    this._exportJobId = "";
                    if (this._exportNotif) { this._exportNotif.dismiss(); this._exportNotif = null; }
                    this._resetExportControlsAfterCancel(ui);
                    return;
                }
                this._pollTimelineExport(ui);
            } catch (error) {
                if (this._exportPanelToken !== token) return;
                this._exportJobId = "";
                if (this._exportNotif) {
                    this._exportNotif.resolve({ tier: "error", message: error?.message || "Export failed." });
                    this._exportNotif = null;
                }
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
        const msg = asset?.name ? `Exported ${asset.name}` : "Export complete";
        // Resolve (and detach) the progress handle before hiding the panel so the
        // hide-path's cancel cleanup does not dismiss the success toast.
        if (this._exportNotif) {
            this._exportNotif.resolve({ message: msg });
            this._exportNotif = null;
        } else {
            notifySuccess(msg);
        }
        this._hideExportPanel();
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
        const globalHidden = !!this.activeScene.global_prompt_track_config?.hidden;
        const labelsOn = this._promptChannelLabels === true;
        const scenePrompt = globalHidden ? "" : (this.activeScene.prompt || "");
        const sections = promptHidden ? [] : (this.activeScene.prompt_sections || []);
        // Freeze ALL window-overlapping sections (channel-bearing) — the relay
        // bridge consumes them; the single `prompt` string is the composed
        // selector mirror (global + section covering the window start).
        // Known accepted mismatch: this filter window is selection ± raw
        // context, while backend grid-snap can extend the executed window.
        const promptSections = [];
        for (const s of sections) {
            if (s.start_frame < snapshotEnd && s.end_frame > snapshotStart) {
                promptSections.push({
                    prompt_id: s.prompt_id || "",
                    start_frame: s.start_frame,
                    end_frame: s.end_frame,
                    channels: normalizeChannels(s.channels, s.prompt),
                    muted: !!s.muted,
                    prompt: s.prompt || "",
                });
            }
        }
        // DISPLAY-ONLY best-effort concat for the optimistic temp queue rows.
        // The authoritative frozen prompt is composed SERVER-SIDE at enqueue
        // (multi-segment + delimiter, channel-grouped labels) and returns in
        // the response jobs, which replace the temp rows. A stale delimiter
        // stash is cosmetic only — never block enqueue on it.
        const displaySectionText = composeSectionsDisplayText(
            promptSections, labelsOn, this._promptSectionDelimiter ?? ".");
        const prompt = [scenePrompt.trim(), displaySectionText].filter(Boolean).join(" ");

        const guideFrameSnapshots = [];
        const guideTrackHidden = !!this.activeScene.guide_track_config?.hidden;
        for (const guide of (this.activeScene.guide_frames || [])) {
            let frameIndex = parseInt(guide.frame_index, 10) || 0;
            if (frameIndex === -1) frameIndex = Math.max(0, sceneDuration - 1);
            if (snapshotStart <= frameIndex && frameIndex < snapshotEnd) {
                guideFrameSnapshots.push({
                    guide_id: guide.guide_id || "",
                    frame_index: frameIndex,
                    asset_id: guide.asset_id,
                    source: guide.source || "asset",
                    strength: guide.strength ?? 1.0,
                    muted: guideTrackHidden || !!guide.muted,
                });
            }
        }

        const driverLaneCount = Math.max(1, parseInt(this.activeScene.motion_driver_lane_count, 10) || 1);
        const driverLaneConfigs = [];
        for (let i = 0; i < driverLaneCount; i++) {
            const cfg = (this.activeScene.motion_driver_lane_configs || [])[i] || {};
            driverLaneConfigs.push({
                name: cfg.name || "",
                color: cfg.color || "",
                locked: !!cfg.locked,
                hidden: !!cfg.hidden,
            });
        }
        const driverClipSnapshots = [];
        for (const clip of (this.activeScene.clips || [])) {
            if (!this._isMotionDriverClip(clip)) continue;
            if ((clip.timeline_start_frame || 0) < snapshotEnd && (clip.timeline_end_frame || 0) > snapshotStart) {
                driverClipSnapshots.push({
                    clip_id: clip.clip_id || "",
                    source_path: clip.source_path || "",
                    timeline_start_frame: clip.timeline_start_frame || 0,
                    timeline_end_frame: clip.timeline_end_frame || 0,
                    source_in_frame: clip.source_in_frame || 0,
                    source_out_frame: clip.source_out_frame || 0,
                    total_source_frames: clip.total_source_frames || 0,
                    source_origin_frame: clip.source_origin_frame || 0,
                    opacity: clip.opacity ?? 1.0,
                    track_index: clip.track_index || 0,
                    role: "motion_driver",
                    strength: clip.strength ?? 1.0,
                    muted: !!clip.muted,
                    fit_mode: clip.fit_mode || this._defaultFitMode(),
                    crop_position: clip.crop_position || this._defaultCropPosition(),
                    prompt: clip.prompt || "",
                    is_generated: !!clip.is_generated,
                    generation_params: clip.generation_params || {},
                    takes: Array.isArray(clip.takes) ? clip.takes : [],
                    active_take: clip.active_take || 0,
                    take_metadata: clip.take_metadata || {},
                });
            }
        }

        return {
            scene_id: range.sceneId,
            scene_name: range.sceneName,
            selection_start: clampedStart,
            selection_end: clampedEnd,
            prompt,
            scene_prompt: scenePrompt,
            context_frames: Math.max(preContextFrames, postContextFrames),
            pre_context_frames: preContextFrames,
            post_context_frames: postContextFrames,
            mask_pre_offset: maskPreOffset,
            mask_post_offset: maskPostOffset,
            guide_frame_snapshots: guideFrameSnapshots,
            driver_clip_snapshots: driverClipSnapshots,
            driver_lane_count: driverLaneCount,
            driver_lane_configs: driverLaneConfigs,
            prompt_sections: promptSections,
            scene_width: Math.max(0, parseInt(this.activeScene.width, 10) || 0),
            scene_height: Math.max(0, parseInt(this.activeScene.height, 10) || 0),
            scene_fps: Math.max(0, parseFloat(this.activeScene.fps) || 0),
            template_id: this._templateId || "free",
            frame_constraint: this._resolveFrameConstraintForTemplate(this._templateId),
            take_placement_mode: this._settings?.render?.takePlacementMode ?? "trimmed",
            // take_placement_linked / take_placement_muted intentionally NOT
            // snapshotted: they resolve from live settings/widgets at execution
            // (user decision 2026-06-11).
            // Labels toggle frozen for reproducibility (read by the relay bridge)
            params: { prompt_channel_labels: labelsOn },
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

        if (this._selectionDraftAnchor) {
            this._batchQueueBtn.textContent = "+ Batch";
            this._batchQueueBtn.title = `Choose ${this._selectionDraftAnchor.edge === "start" ? "Out" : "In"} to complete the selection`;
            this._batchQueueBtn.disabled = true;
            return;
        }
        this._batchQueueBtn.disabled = false;

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

    _flashQueueButton(button) {
        if (!button) return;
        setButtonVariant(button, "active");
        window.setTimeout(() => {
            if (!button.isConnected) return;
            setButtonVariant(button, button.dataset.sonderBaseVariant || "primary");
        }, 500);
    }

    async _addToRenderQueue() {
        if (this._selectionDraftAnchor) {
            notifyWarning(
                `Choose ${this._selectionDraftAnchor.edge === "start" ? "Out" : "In"} before queueing.`,
                { source: "selection-draft-queue-guard" }
            );
            return;
        }
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
            const projectId = this._projectDirName();
            const result = await this._queueProjectMutation({
                key: `project:queue:add:${tempId}`,
                label: "add render queue job",
                coalesce: false,
                intent: {
                    projectId,
                    snapshot: JSON.parse(JSON.stringify(snapshot)),
                },
                refreshScenes: false,
                refreshKeysOnError: ["queue"],
                failureMessage: "Add to render queue failed — queue restored.",
                invalidateQueueFetch: true,
                run: async (queuedIntent) => {
                    return await this._runVersionedProjectMutation(
                        `/sonder-editor/project/${encodeURIComponent(queuedIntent.projectId)}/queue`,
                        {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify(queuedIntent.snapshot),
                        },
                        { projectId: queuedIntent.projectId }
                    );
                },
            });
            const createdJob = result?.payload;
            if (createdJob?.job_id) {
                this._warnForGuideCollisionPredictions(createdJob);
                this._renderQueue = (this._renderQueue || []).map((job) => job.job_id === tempId ? createdJob : job);
                this._flashQueueButton(this._queueBtn);
                this._applyStoredQueueBatchCollapseState();
                this._renderQueuePanel();
            } else {
                this._renderQueue = (this._renderQueue || []).filter((job) => job.job_id !== tempId);
                this._renderQueuePanel();
                this._deferProjectBackedRefresh(["queue"], "queue_add_missing_payload");
            }
        } catch (e) {
            this._renderQueue = (this._renderQueue || []).filter((job) => job.job_id !== tempId);
            this._renderQueuePanel();
            console.error("Add to queue failed:", e);
        }
    }

    async _addBatchToRenderQueue() {
        if (this._selectionDraftAnchor) {
            notifyWarning(
                `Choose ${this._selectionDraftAnchor.edge === "start" ? "Out" : "In"} before queueing a batch.`,
                { source: "selection-draft-batch-guard" }
            );
            return;
        }
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

            const result = await this._queueProjectMutation({
                key: `project:queue:batch:${batchId}`,
                label: "add render queue batch",
                coalesce: false,
                intent: {
                    projectId: this._projectDirName(),
                    snapshots: JSON.parse(JSON.stringify(snapshots)),
                },
                refreshScenes: false,
                refreshKeysOnError: ["queue"],
                failureMessage: "Add render batch failed — queue restored.",
                invalidateQueueFetch: true,
                run: async (queuedIntent) => {
                    return await this._runVersionedProjectMutation(
                        `/sonder-editor/project/${encodeURIComponent(queuedIntent.projectId)}/queue/batch`,
                        {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ jobs: queuedIntent.snapshots }),
                        },
                        { projectId: queuedIntent.projectId }
                    );
                },
            });
            const payload = result?.payload || {};
            const createdJobs = Array.isArray(payload?.jobs) ? payload.jobs : [];
            this._warnForGuideCollisionPredictions(createdJobs);
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
            if (!tempIds.size) {
                notifyError(e?.message || "Add batch to queue failed.", {
                    onRetry: () => { this._addBatchToRenderQueue().catch(() => {}); },
                });
            }
        }
    }

    async _fetchRenderQueue({ ignoreMutationGate = false, reason = "queue_refresh" } = {}) {
        const projectId = this._projectDirName();
        if (!projectId) return;
        if (!ignoreMutationGate && this._hasPendingProjectMutations()) {
            this._deferProjectBackedRefresh(["queue"], reason);
            return;
        }
        const fetchSeq = ++this._queueFetchSeq;
        try {
            const knownVersion = getProjectVersion(projectId);
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${encodeURIComponent(projectId)}/queue`));
            if (resp.ok) {
                const data = await resp.json();
                if (fetchSeq !== this._queueFetchSeq) {
                    sessionDiagRecord("queue_refresh_stale", {
                        reason,
                        fetch_seq: fetchSeq,
                        current_seq: this._queueFetchSeq,
                    });
                    // A newer queue fetch superseded this one — bail (latest-wins).
                    // Do NOT re-defer: the seq gate tracks dispatch order, not server
                    // commit order (durable_rules #101), so the winning in-flight fetch
                    // is authoritative and the version/pending re-defers below remain the
                    // correctness net. Re-deferring here self-sustained an idle replay
                    // churn loop (queue_refresh_stale / project_mutation_deferred_replay).
                    return;
                }
                const headerProjectId = resp.headers.get("X-Sonder-Project-Id") || "";
                const headerVersion = resp.headers.get("X-Sonder-Project-Modified-At") || "";
                const compareVersion = headerProjectId && headerProjectId !== projectId
                    ? (getProjectVersion(headerProjectId) || knownVersion)
                    : knownVersion;
                if (headerVersion && compareVersion && headerVersion < compareVersion) {
                    sessionDiagRecord("queue_refresh_stale_version", {
                        reason,
                        header_version: headerVersion,
                        known_version: compareVersion,
                    });
                    const accepted = this._governStaleVersionReplay(
                        "queue",
                        projectId,
                        headerVersion,
                        compareVersion,
                        "queue_refresh_stale_version_replay",
                    );
                    if (!accepted) return;
                }
                if (!ignoreMutationGate && this._hasPendingProjectMutations()) {
                    this._deferProjectBackedRefresh(["queue"], `${reason}_apply_deferred`);
                    return;
                }
                this._markStaleReplayApplied("queue", projectId);
                this._renderQueue = Array.isArray(data) ? data : [];
                this._applyStoredQueueBatchCollapseState();
                this._renderQueuePanel();
            }
        } catch (e) { console.error("Fetch queue failed:", e); }
    }

    async _clearCompletedRenderQueue() {
        if (!this._projectDirName()) return;
        this._renderQueue = (this._renderQueue || []).filter((job) => String(job.status || "").toLowerCase() !== "completed");
        this._renderQueuePanel();
        try {
            await this._runQueueMutation([{ type: "clear_completed" }]);
        } catch (e) {
            console.error("Clear completed renders failed:", e);
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
            this._fetchRenderQueue({ reason: "queue_expand" });
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
        this._renderQueue = (this._renderQueue || []).filter((candidate) => candidate.job_id !== job.job_id);
        this._renderQueuePanel();
        try {
            await this._runQueueMutation([{ type: "delete_job", job_id: job.job_id }]);
        } catch (e) {
            console.error("Delete queue job failed:", e);
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
        this._clearStaleReplayState();
        this._assetGallery?.cancelThumbnailRepairs?.();
        cancelThumbnailRepairOwner(this._thumbnailRepairOwnerId);
        this.projectDir = projectDir;
        this._frameConstraintHealedFor = "";
        this.activeSceneId = "";
        this.activeScene = null;
        this.scenes = [];
        this._queueBatchExpanded = {};
        this._updateSceneIdentity("Loading…");
        this._updateProjectIdentity();

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
            this._fetchRenderQueue({ reason: "load_project" });
        }
        this._renderTimeline();
    }

    refresh(keys = []) {
        const wanted = new Set(keys);
        const wantsAssets = !wanted.size || wanted.has("assets");
        const wantsScenes = !wanted.size || wanted.has("scenes");
        const wantsProject = !wanted.size || wanted.has("project");
        const wantsQueue = !wanted.size || wanted.has("queue");
        if (this._hasPendingProjectMutations() && (wantsProject || wantsAssets || wantsScenes || wantsQueue)) {
            const deferred = [];
            if (wantsProject) deferred.push("project");
            if (wantsAssets) deferred.push("assets");
            if (wantsScenes) deferred.push("scenes");
            if (wantsQueue) deferred.push("queue");
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
            if (wantsQueue) {
                this._fetchRenderQueue({ reason: "external_refresh" });
            }
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
                    this.sceneWidth = data.resolution[0] || DEFAULT_EDITOR_SETTINGS.projectDefaults.width;
                    this.sceneHeight = data.resolution[1] || DEFAULT_EDITOR_SETTINGS.projectDefaults.height;
                }
                this._templateId = getTemplateById(data.template_id, this._settings).id;
                // Project-durable channel-labels toggle (render-affecting; default off)
                this._promptChannelLabels = data.metadata?.prompt_channel_labels === true;
                this._guideCollisionAutoOffset = data.metadata?.guide_collision_auto_offset !== false;
                // Project-durable section-seam delimiter (render-affecting; default ".")
                this._promptSectionDelimiter = String(data.metadata?.prompt_section_delimiter ?? ".");
                // Project-durable boundary-spill threshold % (render-affecting; default 10)
                this._promptFrameThreshold = Number(data.metadata?.prompt_frame_threshold ?? 10) || 0;
                await this._maybeHealFrameConstraint(this.projectDir, dirName, data.frame_constraint);
                this._syncSceneResolutionControls({ detectSelections: false });
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
            getStreamingMode: () => this._settings?.playback?.streamingMode ?? "auto",
            isSceneOutlineEnabled: () => this._settings?.appearance?.sceneOutline !== false,
            isAdaptiveRebufferEnabled: () => this._settings?.playback?.adaptiveRebuffer !== false,
            getRebufferEnterMs: () => this._settings?.playback?.rebufferEnterMs ?? 250,
            getRebufferMaxMs: () => this._settings?.playback?.rebufferMaxMs ?? 4000,
            getPrebufferBoundaryDepth: () => this._settings?.playback?.prebufferBoundaryDepth ?? 2,
            getPrebufferMaxEntries: () => this._settings?.playback?.prebufferMaxEntries ?? 8,
            getDecodeConcurrency: () => this._settings?.playback?.decodeConcurrency ?? 2,
            notifyInfo: (message, opts) => notifyInfo(message, opts),
            notifyWarning: (message, opts) => notifyWarning(message, opts),
            onFrameChange: (frame, meta = {}) => {
                const measurePlayback = isSessionDiagEnabled() && this.isPlaying;
                const autoScrollStartedAt = measurePlayback ? performance.now() : 0;
                this.playhead = Math.max(0, Math.min(this.totalFrames, Math.round(Number(frame) || 0)));
                if (meta.reason === "playback" || meta.reason === "playback-loop" || meta.reason === "playback-stop-return") {
                    this._maybeAutoScrollToPlayhead();
                }
                const autoScrollFinishedAt = measurePlayback ? performance.now() : 0;
                const timelineMetrics = this._renderTimeline();
                if (!measurePlayback) return null;
                return {
                    autoScrollMs: autoScrollFinishedAt - autoScrollStartedAt,
                    ...(timelineMetrics || {}),
                };
            },
            onTransportUpdate: () => this._updateTransportUI(),
            onPlaybackStateChange: (isPlaying) => {
                this.isPlaying = !!isPlaying;
                if (this._vpPlayBtn) {
                    this._vpPlayBtn.textContent = isPlaying ? "Pause" : "Play";
                }
                this._renderTimeline();
            },
            onPlaybackWarmStateChange: (payload) => this._handlePlaybackWarmStateChange(payload),
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
            this._clearPlaybackWarmOverlay("media-cache-clear", { notifySurface: false, render: false });
            return;
        }
        this._clearPlaybackWarmOverlay("media-cache-clear", { notifySurface: false, render: false });
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

        // Legacy fallback path (only runs when viewport-surface creation
        // failed). Kept on blob loading deliberately: /view DOES honor HTTP
        // Range (verified ComfyUI 0.20.1) and the surface streams directly per
        // playback.streamingMode, but this path is not worth the regression
        // surface — see plans/in-this-session-we-twinkly-barto.md Phase 0.
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

        // Legacy fallback path — blob kept deliberately (see _getOrCreateVideo).
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

        if (newFrame === this.playhead) {
            this._playbackRAF = requestAnimationFrame((t) => this._playbackTick(t));
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
        const barsH = 44 * st; // single merged toolbar row (this non-fullscreen branch is near-dead — editor is always fullscreen)
        const timelineH = this._timelineHeight;
        const editorsH = (this._promptEditorEl ? (this._promptEditorEl.offsetHeight || 30) : 0)
            + (this._itemEditorEl ? (this._itemEditorEl.offsetHeight || 30) : 0);
        // Account for the container's vertical padding so the bottom-most inline
        // editor bar isn't clipped when the editor renders as a node DOM widget.
        const ccs = this.container ? getComputedStyle(this.container) : null;
        const containerVPad = ccs ? (parseFloat(ccs.paddingTop) || 0) + (parseFloat(ccs.paddingBottom) || 0) : 8;
        return barsH + timelineH + (this._galleryHeight * sg) + editorsH + containerVPad;
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

    // Import N files with a foreground progress notification (count-based for
    // multi-file, indeterminate for a single file), resolving to success/error.
    async _importFilesWithProgress(files, folder = "") {
        const list = Array.isArray(files) ? files : Array.from(files || []);
        if (!list.length) return;
        const total = list.length;
        const handle = notifyProgress({
            verb: "Importing",
            message: total > 1 ? `0/${total} files` : (list[0]?.name || "1 file"),
            progress: total > 1 ? { current: 0, total, unit: "" } : null,
            foreground: true,
            source: "import",
        });
        let done = 0;
        let imported = 0;
        try {
            const failures = [];
            for (const file of list) {
                try {
                    if (await importFileIntoProject(this.projectDir, file, folder)) imported += 1;
                } catch (error) {
                    failures.push({ file, error });
                    console.warn("[Sonder] Import failed:", file?.name, error);
                }
                done += 1;
                handle.update({
                    message: total > 1 ? `${done}/${total} files` : (file?.name || ""),
                    progress: total > 1 ? { current: done, total, unit: "" } : null,
                });
            }
            if (imported) await this._fetchAssets();
            if (!failures.length && imported === total) {
                handle.resolve({ message: `Imported ${imported} file${imported === 1 ? "" : "s"}` });
            } else if (imported > 0) {
                const first = failures[0]?.error?.message || "one file failed";
                handle.resolve({ tier: "warning", message: `Imported ${imported} of ${total} files. ${first}` });
            } else {
                const first = failures[0]?.error?.message || "No files imported.";
                handle.resolve({ tier: "error", message: first });
            }
        } catch (e) {
            console.error("[Sonder] Import failed:", e);
            handle.resolve({ tier: "error", message: e?.message || "Import failed." });
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
            notifyError(e?.message || "Import failed.", { source: "import" });
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
        this._clearStaleReplayState();

        this._stopPlayback();
        if (this._seekAbort) {
            this._seekAbort();
            this._seekAbort = null;
        }
        if (this._toolbarLayoutRefreshRaf) {
            cancelAnimationFrame(this._toolbarLayoutRefreshRaf);
            this._toolbarLayoutRefreshRaf = null;
        }
        if (this._timelineEdgeScrollCleanup) {
            this._timelineEdgeScrollCleanup();
            this._timelineEdgeScrollCleanup = null;
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
        this._hideImagePicker();
        this._hideGuideHoverPreview();
        this._hidePromptHoverPreview();
        if (this._promptPanelHandle) { this._promptPanelHandle.cleanup(); this._promptPanelHandle = null; }
        if (this._contextMenuMouseOff) { this._contextMenuMouseOff(); this._contextMenuMouseOff = null; }
        if (this._focusHandler) {
            document.removeEventListener("mousedown", this._focusHandler, true);
            this._focusHandler = null;
        }
        if (this._settingsUnsubscribe) {
            this._settingsUnsubscribe();
            this._settingsUnsubscribe = null;
        }
        cancelThumbnailRepairOwner(this._thumbnailRepairOwnerId);
        if (this._thumbnailRepairUnsubscribe) {
            this._thumbnailRepairUnsubscribe();
            this._thumbnailRepairUnsubscribe = null;
        }
        if (this._renderCacheStatusHandler && typeof api.removeEventListener === "function") {
            api.removeEventListener("status", this._renderCacheStatusHandler);
            this._renderCacheStatusHandler = null;
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
        if (this._foregroundPillUnsub) {
            this._foregroundPillUnsub();
            this._foregroundPillUnsub = null;
        }
        if (this._fullscreenPlaceholder) {
            this._fullscreenPlaceholder.remove();
            this._fullscreenPlaceholder = null;
        }
        if (this._assetGallery) {
            this._assetGallery.destroy();
            this._assetGallery = null;
        }
        if (this._playbackWarmRenderRAF !== null) {
            cancelAnimationFrame(this._playbackWarmRenderRAF);
            this._playbackWarmRenderRAF = null;
        }

        if (this._viewportSurface) {
            this._viewportSurface.destroy();
            this._viewportSurface = null;
        }
        if (this._playbackWarmRenderRAF !== null) {
            cancelAnimationFrame(this._playbackWarmRenderRAF);
            this._playbackWarmRenderRAF = null;
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
