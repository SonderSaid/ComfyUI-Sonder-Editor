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
import { createViewportSurface } from "./viewport_surface.js";
import {
    _debugListConsumers as debugKeyboardConsumers,
    isKeyboardDebugEnabled,
    register as registerKeyboardConsumer,
    PRIORITY as KEY_PRIORITY,
} from "./keyboard_ownership.js";
import {
    ASPECT_RATIO_PRESETS,
    CLIP_LABEL_MODE_OPTIONS,
    CUSTOM_AUDIO_CODEC_OPTIONS,
    CUSTOM_CONTAINER_OPTIONS,
    CUSTOM_ENCODER_PRESET_OPTIONS,
    CUSTOM_OUTPUT_KIND_VIDEO,
    CUSTOM_PIX_FMT_OPTIONS,
    CUSTOM_VIDEO_CODEC_OPTIONS,
    DEFAULT_SAVE_PRESET,
    DEFAULT_EDITOR_SETTINGS,
    GALLERY_SORT_OPTIONS,
    GALLERY_THUMBNAIL_SIZE_OPTIONS,
    MODEL_TEMPLATE_PARAM_KEYS,
    PLAYBACK_RESOLUTION_OPTIONS,
    RESOLUTION_TIERS,
    SAVE_PRESET_OPTIONS,
    SNAP_TARGET_OPTIONS,
    TAKE_PLACEMENT_MODE_OPTIONS,
    TIMECODE_MODE_OPTIONS,
    computeResolutionFromTier,
    describeConstraintFormula,
    frameConstraintsEqual,
    getEditorSettings,
    getAllModelTemplates,
    getTemplateById,
    previewConstraintValues,
    resolveBatchChunkSize,
    resolveFrameConstraintForTemplate,
    snapResolution,
    snapToConstraint,
    subscribeEditorSettings,
    updateEditorSettings,
} from "./editor_settings.js";

const RENDER_CACHE_ENTRY_PRESETS = [
    { value: "3", label: "3" },
    { value: "5", label: "5" },
    { value: "10", label: "10" },
    { value: "25", label: "25" },
    { value: "unlimited", label: "Unlimited" },
];

const TRASH_SIZE_MB_PRESETS = [
    { value: "250", label: "250 MB" },
    { value: "500", label: "500 MB" },
    { value: "1000", label: "1 GB" },
    { value: "2000", label: "2 GB" },
    { value: "5000", label: "5 GB" },
    { value: "unlimited", label: "Unlimited" },
];

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
const TIMELINE_HEIGHT = 212;
const GALLERY_HEIGHT = 160;
const RULER_HEIGHT = 24;
const TRACK_HEIGHT = 32;
const GUIDE_LABEL_RESERVE = 14;
const SCENE_BAR_HEIGHT = 36;
const FULLSCREEN_SIDEBAR_DEFAULT_WIDTH = 240;
const FULLSCREEN_SIDEBAR_MIN_WIDTH = 180;
const FULLSCREEN_TIMELINE_MIN_HEIGHT = 160;
const FULLSCREEN_TIMELINE_FALLBACK_MAX_HEIGHT = 600;
const TRACK_TYPE = { VIDEO: "video", AUDIO: "audio", MOTION_DRIVER: "motion_driver", GUIDES: "guides", PROMPT: "prompt" };
const TRACK_COLLAPSED_HEIGHT = 8; // Height when a track is collapsed
const LABEL_WIDTH = 55; // px reserved for track labels (node mode)
const LABEL_WIDTH_FS = 70; // px reserved for track labels (fullscreen)
const LANE_PALETTE = [
    "#3a6ea5",  // Blue
    "#6a8e3e",  // Green
    "#a05a2c",  // Orange
    "#7e4a8a",  // Purple
    "#8a6b3e",  // Gold
    "#3a8a7a",  // Teal
];
const COLORS = {
    bg: "#121820",
    panelMuted: "#10161d",
    panel: "#151c24",
    panelRaised: "#1b2430",
    panelRaisedHover: "#25313f",
    ruler: "#25303c",
    rulerText: "#8694a3",
    rulerTick: "#54616f",
    track: "#202934",
    trackBorder: "#33404d",
    clip: "#3a7ca5",
    clipSelected: "#5aacd5",
    motionDriver: "#e8a030",
    motionDriverSelected: "#ffcc44",
    audioClip: "#5a8a5a",
    audioClipSelected: "#7aba7a",
    guide: "#e8a030",
    guideSelected: "#ffcc44",
    selection: "rgba(100, 180, 255, 0.14)",
    selectionBorder: "rgba(100, 180, 255, 0.58)",
    selectionContext: "rgba(100, 180, 255, 0.07)",
    selectionContextBorder: "rgba(100, 180, 255, 0.24)",
    maskOffset: "rgba(255, 190, 80, 0.11)",
    maskOffsetBorder: "rgba(255, 190, 80, 0.34)",
    playhead: "#ff4444",
    promptSection: "rgba(180, 120, 255, 0.2)",
    promptBorder: "rgba(180, 120, 255, 0.5)",
    galleryBg: "#171e26",
    galleryItem: "#202934",
    galleryItemHover: "#2a3644",
    galleryItemBorder: "#3a4a5b",
    galleryText: "#d6dde5",
    galleryLabel: "#8795a3",
    sceneBar: "#171e26",
    sceneBtn: "#243342",
    sceneBtnHover: "#304456",
    sceneBtnActive: "#4a82ad",
    text: "#dbe3ea",
    textDim: "#90a0af",
    textMuted: "#748291",
    border: "#34414d",
    borderSoft: "#28313b",
    borderStrong: "#587089",
    accentSoft: "#263a4d",
    accentSoftHover: "#314961",
    accentBorder: "#6686a3",
    warningSoft: "#45361f",
    warningBorder: "#9a7a42",
    warningText: "#efd79f",
    dangerSoft: "#44292d",
    dangerBorder: "#8f5f66",
    dangerText: "#efc0c4",
};

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

function chromeOverlayPanelCss({ width = "90%", maxWidth = "520px", maxHeight = "80vh", padding = "20px 28px", fontFamily = "sans-serif" } = {}) {
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

function lightenColor(hex, amount) {
    if (typeof hex !== "string" || !/^#[0-9a-fA-F]{6}$/.test(hex)) return hex;
    const mix = Math.max(0, Math.min(1, amount));
    const channel = (offset) => {
        const value = parseInt(hex.slice(offset, offset + 2), 16);
        return Math.round(value + (255 - value) * mix);
    };
    return `#${channel(1).toString(16).padStart(2, "0")}${channel(3).toString(16).padStart(2, "0")}${channel(5).toString(16).padStart(2, "0")}`;
}

function scaleColor(hex, factor) {
    if (typeof hex !== "string" || !/^#[0-9a-fA-F]{6}$/.test(hex)) return hex;
    const scale = Math.max(0.2, Math.min(2.0, factor));
    const channel = (offset) => {
        const value = parseInt(hex.slice(offset, offset + 2), 16);
        return Math.round(Math.max(0, Math.min(255, value * scale)));
    };
    return `#${channel(1).toString(16).padStart(2, "0")}${channel(3).toString(16).padStart(2, "0")}${channel(5).toString(16).padStart(2, "0")}`;
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
        this._timelineMutationDepth = 0;
        this._sceneFetchSeq = 0;

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
        this._exportPollTimer = null;
        this._exportJobId = "";
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

    _shouldDeferSceneRefresh({ ignoreMutationGate = false } = {}) {
        return !!(this.isDragging || (!ignoreMutationGate && this._timelineMutationDepth > 0));
    }

    _deferSceneRefresh(reason = "unknown", details = {}) {
        this._pendingScenesRefresh = true;
        sessionDiagRecord("scene_refresh_deferred", {
            reason,
            drag_type: this.dragType || "",
            mutation_depth: this._timelineMutationDepth || 0,
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
            font-family: 'Segoe UI', Arial, sans-serif; font-size: 11px;
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
        const bar = document.createElement("div");
        bar.style.cssText = `
            display: flex; align-items: center; gap: 4px;
            padding: 4px 6px; background: ${COLORS.sceneBar};
            border-bottom: 1px solid ${COLORS.border}; min-height: ${SCENE_BAR_HEIGHT}px;
        `;

        // Prev scene
        const prevBtn = this._makeBtn("◀", "Previous scene");
        prevBtn.addEventListener("click", () => this._cycleScene(-1));

        // Scene label
        this.sceneLabel = document.createElement("span");
        this.sceneLabel.style.cssText = `
            flex: 1; text-align: center; font-size: 12px; font-weight: 600;
            color: ${COLORS.text}; overflow: hidden; text-overflow: ellipsis;
            white-space: nowrap; cursor: pointer;
        `;
        this.sceneLabel.textContent = "No Scene";
        this.sceneLabel.title = "Double-click to rename · Right-click for options";
        this.sceneLabel.addEventListener("dblclick", () => this._renameScene());
        this.sceneLabel.addEventListener("contextmenu", (e) => {
            e.preventDefault();
            e.stopPropagation();
            if (!this.activeScene) return;

            const menuItems = [
                { label: "Rename Scene", action: () => this._renameScene() },
                { label: "Duplicate Scene", action: () => this._duplicateScene() },
                { label: "Delete Scene", action: () => this._deleteScene(), danger: true },
            ];
            this._showContextMenu(e.clientX, e.clientY, menuItems);
        });

        // Next scene
        const nextBtn = this._makeBtn("▶", "Next scene");
        nextBtn.addEventListener("click", () => this._cycleScene(1));

        // Add scene
        const addBtn = this._makeBtn("+", "Create new scene");
        setButtonVariant(addBtn, "primary");
        addBtn.dataset.sonderHoverVariant = "primary";
        addBtn.addEventListener("click", () => this._createScene());

        // Duration input
        this._durLabel = document.createElement("span");
        this._durLabel.style.cssText = `color: ${COLORS.textDim}; font-size: 10px; margin-left: 8px;`;
        this._durLabel.textContent = "Frames:";

        this.durationInput = document.createElement("input");
        this.durationInput.type = "number";
        this.durationInput.min = 1;
        this.durationInput.max = 99999;
        this.durationInput.value = this.totalFrames;
        this.durationInput.style.cssText = `
            ${chromeInputCss({ width: "55px", fontSize: "11px", padding: "2px 4px" })}
        `;
        this.durationInput.addEventListener("change", () => {
            if (this._timecodeMode === "timecode") {
                const sec = parseFloat(this.durationInput.value) || 0;
                this.totalFrames = Math.max(1, this._secondsToFrames(sec));
            } else {
                this.totalFrames = Math.max(1, parseInt(this.durationInput.value) || 200);
            }
            this.totalFrames = this._snapSceneDurationToTemplate(this.totalFrames);
            this._refreshDurationInput();
            if (this.activeScene) {
                this._updateSceneDuration(this.totalFrames);
            }
            this._renderTimeline();
        });

        const ctxLabel = document.createElement("span");
        ctxLabel.style.cssText = `color: ${COLORS.textDim}; font-size: 10px; margin-left: 6px;`;
        ctxLabel.textContent = "Ctx:";

        const ctxInputStyle = `
            ${chromeInputCss({ width: "40px", fontSize: "10px", padding: "2px 3px" })}
        `;

        const preCtxLabel = document.createElement("span");
        preCtxLabel.style.cssText = `color: ${COLORS.textDim}; font-size: 9px;`;
        preCtxLabel.textContent = "Pre";

        this._preContextInput = document.createElement("input");
        this._preContextInput.type = "number";
        this._preContextInput.min = 0;
        this._preContextInput.max = 256;
        this._preContextInput.step = 1;
        this._preContextInput.value = 0;
        this._preContextInput.title = "Frames to include before the selected generation range";
        this._preContextInput.style.cssText = ctxInputStyle;
        this._preContextInput.addEventListener("change", () => this._updateContextFrameWidgets());

        const postCtxLabel = document.createElement("span");
        postCtxLabel.style.cssText = `color: ${COLORS.textDim}; font-size: 9px;`;
        postCtxLabel.textContent = "Post";

        this._postContextInput = document.createElement("input");
        this._postContextInput.type = "number";
        this._postContextInput.min = 0;
        this._postContextInput.max = 256;
        this._postContextInput.step = 1;
        this._postContextInput.value = 0;
        this._postContextInput.title = "Frames to include after the selected generation range";
        this._postContextInput.style.cssText = ctxInputStyle;
        this._postContextInput.addEventListener("change", () => this._updateContextFrameWidgets());

        const maskLabel = document.createElement("span");
        maskLabel.style.cssText = `color: ${COLORS.textDim}; font-size: 10px; margin-left: 4px;`;
        maskLabel.textContent = "Mask Offset:";

        const maskPreLabel = document.createElement("span");
        maskPreLabel.style.cssText = `color: ${COLORS.textDim}; font-size: 9px;`;
        maskPreLabel.textContent = "-";

        this._maskPreOffsetInput = document.createElement("input");
        this._maskPreOffsetInput.type = "number";
        this._maskPreOffsetInput.min = 0;
        this._maskPreOffsetInput.max = 256;
        this._maskPreOffsetInput.step = 1;
        this._maskPreOffsetInput.value = 0;
        this._maskPreOffsetInput.title = "Extra pre-context frames excluded from denoise mask start";
        this._maskPreOffsetInput.style.cssText = ctxInputStyle;
        this._maskPreOffsetInput.addEventListener("change", () => this._updateContextFrameWidgets());

        const maskPostLabel = document.createElement("span");
        maskPostLabel.style.cssText = `color: ${COLORS.textDim}; font-size: 9px;`;
        maskPostLabel.textContent = "+";

        this._maskPostOffsetInput = document.createElement("input");
        this._maskPostOffsetInput.type = "number";
        this._maskPostOffsetInput.min = 0;
        this._maskPostOffsetInput.max = 256;
        this._maskPostOffsetInput.step = 1;
        this._maskPostOffsetInput.value = 0;
        this._maskPostOffsetInput.title = "Extra post-context frames included in denoise mask end";
        this._maskPostOffsetInput.style.cssText = ctxInputStyle;
        this._maskPostOffsetInput.addEventListener("change", () => this._updateContextFrameWidgets());

        // Resolution inputs
        const resLabel = document.createElement("span");
        resLabel.style.cssText = `color: ${COLORS.textDim}; font-size: 10px; margin-left: 6px;`;
        resLabel.textContent = "Res:";

        const inputStyle = chromeInputCss({ width: "48px", fontSize: "10px", padding: "2px 3px" });

        this._resWInput = document.createElement("input");
        this._resWInput.type = "number"; this._resWInput.min = 0; this._resWInput.max = 4096; this._resWInput.step = 8;
        this._resWInput.placeholder = "W";
        this._resWInput.style.cssText = inputStyle;
        this._resWInput.addEventListener("change", () => this._onResolutionChange("w"));

        const xLabel = document.createElement("span");
        xLabel.style.cssText = `color: ${COLORS.textDim}; font-size: 9px;`;
        xLabel.textContent = "x";

        this._resHInput = document.createElement("input");
        this._resHInput.type = "number"; this._resHInput.min = 0; this._resHInput.max = 4096; this._resHInput.step = 8;
        this._resHInput.placeholder = "H";
        this._resHInput.style.cssText = inputStyle;
        this._resHInput.addEventListener("change", () => this._onResolutionChange("h"));

        this._aspectRatioSelect = document.createElement("select");
        this._aspectRatioSelect.style.cssText = `${chromeInputCss({ fontSize: "9px", padding: "1px 4px" })} width: 72px;`;
        for (const preset of ASPECT_RATIO_PRESETS) {
            const opt = document.createElement("option");
            opt.value = this._aspectRatioOptionValue(preset.a, preset.b);
            opt.textContent = preset.label;
            this._aspectRatioSelect.appendChild(opt);
        }
        this._aspectRatioSelect.addEventListener("change", () => {
            this._resetFreeAspectTierDraft();
            this._updateResolutionInputMode();
            this._recalculateResolution();
        });

        this._resTierSelect = document.createElement("select");
        this._resTierSelect.style.cssText = `${chromeInputCss({ fontSize: "9px", padding: "1px 4px" })} width: 92px;`;
        this._resTierSelect.addEventListener("change", () => {
            this._resetFreeAspectTierDraft();
            this._recalculateResolution();
        });

        this._templateSelect = document.createElement("select");
        this._templateSelect.style.cssText = `${chromeInputCss({ fontSize: "9px", padding: "1px 4px" })} width: 108px;`;
        this._templateSelect.addEventListener("change", () => this._handleTemplateSelectionChange());
        this._rebuildTemplateOptions();
        this._rebuildResolutionTierOptions();
        this._applyTemplateConstraintMetadata();
        this._updateResolutionInputMode();

        // FPS input
        const fpsLabel = document.createElement("span");
        fpsLabel.style.cssText = `color: ${COLORS.textDim}; font-size: 10px; margin-left: 6px;`;
        fpsLabel.textContent = "FPS:";

        this._fpsInput = document.createElement("input");
        this._fpsInput.type = "number"; this._fpsInput.min = 0; this._fpsInput.max = 120; this._fpsInput.step = 0.001;
        this._fpsInput.placeholder = String(this.fps);
        this._fpsInput.style.cssText = chromeInputCss({ width: "42px", fontSize: "10px", padding: "2px 3px" });
        this._fpsInput.addEventListener("change", () => {
            const template = this._getActiveTemplate();
            const rawValue = parseFloat(this._fpsInput.value) || 0;
            const val = template.id === "free"
                ? rawValue
                : Number(snapToConstraint(rawValue, template?.constraints?.fps).toFixed(3));
            this._fpsInput.value = val || "";
            if (this.activeScene) {
                this._updateSceneFps(val);
            }
        });

        bar.append(prevBtn, this.sceneLabel, nextBtn, addBtn,
            this._durLabel, this.durationInput,
            ctxLabel, preCtxLabel, this._preContextInput, postCtxLabel, this._postContextInput,
            maskLabel, maskPreLabel, this._maskPreOffsetInput, maskPostLabel, this._maskPostOffsetInput,
            resLabel, this._resWInput, xLabel, this._resHInput, this._aspectRatioSelect, this._resTierSelect, this._templateSelect,
            fpsLabel, this._fpsInput);
        this._sceneBar = bar;
        this.container.appendChild(bar);
    }

    _buildToolbar() {
        this._toolbar = document.createElement("div");
        this._toolbar.style.cssText = `
            display: flex; align-items: center; gap: 2px;
            padding: 3px 6px; background: ${COLORS.panel};
            border-bottom: 1px solid ${COLORS.border}; font-size: 10px; min-height: 26px;
        `;

        // Tool buttons with active states
        const makeToolBtn = (label, shortcut, tooltip, getter, toggle) => {
            const btn = document.createElement("button");
            btn.style.cssText = `${chromeButtonCss({ variant: "muted", padding: "2px 8px", fontSize: "10px", radius: "6px" })} white-space: nowrap;`;
            btn.textContent = label;
            btn.title = `${tooltip} [${shortcut}]`;
            btn.addEventListener("click", toggle);
            return btn;
        };

        this._toolBtnSnap = makeToolBtn("⊞ Snap", "S", "Toggle snapping", () => this.snappingEnabled, () => {
            this._setSnappingEnabled(!this.snappingEnabled);
        });

        this._toolBtnRazor = makeToolBtn("✂ Cut", "C", "Toggle razor/cut mode", () => this._razorMode, () => {
            this._razorMode = !this._razorMode;
            this._updateToolbar();
        });

        const cutHereBtn = makeToolBtn("⌇ Split Here", "", "Split clip/audio at playhead", () => false, async () => {
            const selectedTargets = this.selectedItems
                .filter((item) => (item.type === "clip" || item.type === "audio")
                    && this.playhead > item.data.timeline_start_frame
                    && this.playhead < item.data.timeline_end_frame);
            if (selectedTargets.length) {
                for (const hit of selectedTargets) {
                    await this._splitClipAtFrame(hit, this.playhead);
                }
                return;
            }

            const clip = this._getClipAtFrame(this.playhead) || this._getMotionDriverClipAtFrame(this.playhead);
            if (clip) {
                await this._splitClipAtFrame({ type: "clip", id: clip.clip_id, data: clip }, this.playhead);
            }
            const audio = this._getAudioAtFrame(this.playhead);
            if (audio) {
                await this._splitClipAtFrame({ type: "audio", id: audio.track_id, data: audio }, this.playhead);
            }
        });
        cutHereBtn.title = "Split clip/audio at current playhead position";

        // Separator
        const sep1 = document.createElement("span");
        sep1.style.cssText = chromeDividerCss();

        // Playhead Frame box (navigation only; clamp to scene bounds)
        const frameLabel = document.createElement("span");
        frameLabel.style.cssText = `color: ${COLORS.textDim}; font-size: 9px; margin-left: 2px;`;
        frameLabel.textContent = "F";
        const playheadInputCss = `${chromeInputCss({ width: "58px", fontSize: "10px", padding: "2px 4px", textAlign: "right" })} min-width: 0;`;
        this._playheadFrameInput = document.createElement("input");
        this._playheadFrameInput.type = "text";
        this._playheadFrameInput.inputMode = "decimal";
        this._playheadFrameInput.title = "Playhead frame";
        this._playheadFrameInput.style.cssText = playheadInputCss;
        const applyPlayhead = (value) => {
            const parsed = this._parsePositionInput(value);
            if (!Number.isFinite(parsed)) { this._refreshPlayheadInput(); return; }
            const maxFrame = Math.max(0, this.activeScene?.duration_frames || this.totalFrames);
            this.playhead = Math.max(0, Math.min(maxFrame, Math.round(parsed)));
            if (this.isPlaying) this._stopPlayback();
            this._renderTimeline();
            this._renderViewportFrame();
            this._updateToolbar();
        };
        this._playheadFrameInput.addEventListener("change", () => applyPlayhead(this._playheadFrameInput.value));
        this._playheadFrameInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                applyPlayhead(this._playheadFrameInput.value);
                e.preventDefault();
            } else if (e.key === "Escape") {
                this._refreshPlayheadInput();
            }
            e.stopPropagation();
        });

        // Editable selection (In/Out) controls.
        const inLabel = document.createElement("span");
        inLabel.style.cssText = `color: ${COLORS.textDim}; font-size: 9px; margin-left: 2px;`;
        inLabel.textContent = "In";
        const outLabel = document.createElement("span");
        outLabel.style.cssText = `color: ${COLORS.textDim}; font-size: 9px;`;
        outLabel.textContent = "Out";
        const selectionInputCss = `${chromeInputCss({ width: "58px", fontSize: "10px", padding: "2px 4px", textAlign: "right" })} min-width: 0;`;
        const makeSelectionInput = (title, apply) => {
            const input = document.createElement("input");
            input.type = "text";
            input.inputMode = "decimal";
            input.title = title;
            input.style.cssText = selectionInputCss;
            input.addEventListener("change", () => apply(input.value));
            input.addEventListener("keydown", (e) => {
                if (e.key === "Enter") {
                    apply(input.value);
                    e.preventDefault();
                } else if (e.key === "Escape") {
                    this._refreshSelectionInputs();
                }
                e.stopPropagation();
            });
            return input;
        };
        this._selectionStartInput = makeSelectionInput("Selection in-point", (value) => {
            const frame = this._parsePositionInput(value);
            if (Number.isFinite(frame)) {
                const maxFrame = Math.max(0, this.activeScene?.duration_frames || this.totalFrames);
                this._setSelectionStartFrame(this._snapSelectionFrame(frame, { direction: "up", clampMax: maxFrame }));
            } else this._refreshSelectionInputs();
        });
        this._selectionEndInput = makeSelectionInput("Selection out-point", (value) => {
            const frame = this._parsePositionInput(value);
            if (Number.isFinite(frame)) {
                const maxFrame = Math.max(0, this.activeScene?.duration_frames || this.totalFrames);
                this._setSelectionEndFrame(this._snapSelectionFrame(frame, { direction: "up", clampMax: maxFrame }));
            } else this._refreshSelectionInputs();
        });

        const clearSelBtn = makeToolBtn("✕", "X", "Clear selection", () => false, () => {
            this._clearTimelineSelection();
        });
        clearSelBtn.style.padding = "2px 4px";
        clearSelBtn.style.fontSize = "9px";

        // Separator 2
        const sep2 = document.createElement("span");
        sep2.style.cssText = chromeDividerCss();

        // Fit to view button
        const fitBtn = makeToolBtn("⊞ Fit", "F", "Fit timeline to view", () => false, () => {
            this._fitToView();
        });

        // Timecode toggle button
        this._toolBtnTimecode = makeToolBtn("TC", "T", "Toggle timecode/frame display", () => this._timecodeMode === "timecode", () => {
            this._toggleTimecodeMode();
        });

        // Undo/Redo buttons
        const undoBtn = this._makeBtn("↩", "Undo (Ctrl+Z)");
        undoBtn.style.fontSize = "13px";
        undoBtn.addEventListener("click", () => this._undo());

        const redoBtn = this._makeBtn("↪", "Redo (Ctrl+Y)");
        redoBtn.style.fontSize = "13px";
        redoBtn.addEventListener("click", () => this._redo());

        // Separator 3
        const sep3 = document.createElement("span");
        sep3.style.cssText = chromeDividerCss();

        // Spacer
        const spacer = document.createElement("span");
        spacer.style.flex = "1";

        // Shortcut help button
        const helpBtn = document.createElement("button");
        helpBtn.textContent = "?";
        helpBtn.title = "Keyboard Shortcuts";
        helpBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "1px 7px", fontSize: "10px", radius: "999px" });
        helpBtn.addEventListener("click", () => this._showShortcutOverlay());

        // Settings gear button
        const settingsBtn = document.createElement("button");
        settingsBtn.textContent = "⚙";
        settingsBtn.title = "Editor Settings";
        settingsBtn.style.cssText = `${chromeButtonCss({ variant: "subtle", padding: "1px 7px", fontSize: "12px", radius: "999px" })} margin-left: 4px;`;
        settingsBtn.addEventListener("click", () => this._showSettingsPanel());

        // Saved selections bookmark button
        this._bookmarkBtn = document.createElement("button");
        this._bookmarkBtn.textContent = "🔖";
        this._bookmarkBtn.title = "Saved Selections";
        this._bookmarkBtn.style.cssText = `${chromeButtonCss({ variant: "subtle", padding: "1px 6px", fontSize: "11px", radius: "6px" })} position: relative;`;
        this._bookmarkBtn.addEventListener("click", (e) => this._toggleSavedSelectionsDropdown(e));

        // + Queue button
        this._queueBtn = document.createElement("button");
        this._queueBtn.textContent = "+ Queue";
        this._queueBtn.title = "Add current selection to render queue";
        this._queueBtn.style.cssText = `${chromeButtonCss({ variant: "primary", padding: "2px 8px", fontSize: "10px", radius: "6px" })} white-space: nowrap;`;
        this._queueBtn.addEventListener("click", () => this._addToRenderQueue());

        this._batchQueueBtn = document.createElement("button");
        this._batchQueueBtn.textContent = "+ Batch (1)";
        this._batchQueueBtn.title = "Add the current selection to the render queue as chunked jobs";
        this._batchQueueBtn.style.cssText = `${chromeButtonCss({ variant: "primary", padding: "2px 8px", fontSize: "10px", radius: "6px" })} white-space: nowrap;`;
        this._batchQueueBtn.addEventListener("click", () => this._addBatchToRenderQueue());

        this._queueStatusWrap = document.createElement("div");
        this._queueStatusWrap.style.cssText = `
            display: flex;
            align-items: center;
            gap: 4px;
            margin-left: 4px;
            min-width: 0;
            white-space: nowrap;
        `;
        this._queueStatusWrap.title = "Queue status";

        this._exportBtn = document.createElement("button");
        this._exportBtn.textContent = "Export";
        this._exportBtn.title = "Export the current scene timeline";
        this._exportBtn.style.cssText = `${chromeButtonCss({ variant: "primary", padding: "2px 8px", fontSize: "10px", radius: "6px" })} white-space: nowrap;`;
        this._exportBtn.addEventListener("click", () => this._showExportPanel());

        // Animatic toggle button
        this._toolBtnAnimatic = makeToolBtn("👁 Anim", "A", "Toggle animatic mode (hide all video)", () => this._animaticMode, () => {
            this._toggleAnimatic();
        });

        // Zoom controls + fullscreen (migrated from info bar)
        const zoomOut = this._makeBtn("−", "Zoom out [-]");
        zoomOut.style.fontSize = "13px";
        zoomOut.addEventListener("click", () => this._zoom(-1));

        const zoomIn = this._makeBtn("+", "Zoom in [+]");
        zoomIn.style.fontSize = "13px";
        zoomIn.addEventListener("click", () => this._zoom(1));

        this._fullscreenBtn = this._makeBtn("⛶", "Toggle fullscreen");
        this._fullscreenBtn.style.fontSize = "14px";
        this._fullscreenBtn.addEventListener("click", () => this._toggleFullscreen());

        this._toolbar.append(undoBtn, redoBtn, sep3, this._toolBtnSnap, this._toolBtnRazor, cutHereBtn, sep1, frameLabel, this._playheadFrameInput, inLabel, this._selectionStartInput, outLabel, this._selectionEndInput, clearSelBtn, this._bookmarkBtn, sep2, fitBtn, this._toolBtnTimecode, this._toolBtnAnimatic, this._queueBtn, this._batchQueueBtn, this._queueStatusWrap, this._exportBtn, spacer, zoomOut, zoomIn, this._fullscreenBtn, helpBtn, settingsBtn);
        this.container.appendChild(this._toolbar);
        this._updateToolbar();
    }

    _queueChromeBadges(queue = this._renderQueue) {
        const counts = { running: 0, pending: 0 };
        for (const job of Array.isArray(queue) ? queue : []) {
            const status = String(job?.status || "pending").trim().toLowerCase();
            if (status === "running") {
                counts.running += 1;
            } else if (status === "pending") {
                counts.pending += 1;
            }
        }

        if (this.renderQueueActive === false) {
            return {
                badges: [{ text: "Inactive", background: COLORS.panelRaised, border: COLORS.borderStrong }],
                counts,
            };
        }

        const badges = [];
        if (counts.running > 0) {
            badges.push({ text: `${counts.running} Running`, background: "#264863", border: "#5d8db5" });
        }
        if (counts.pending > 0) {
            badges.push({ text: `${counts.pending} Pending`, background: COLORS.warningSoft, border: COLORS.warningBorder });
        }
        if (!badges.length) {
            badges.push({ text: "Idle", background: "#223128", border: "#4d6a58" });
        }
        return { badges, counts };
    }

    _updateQueueChromeStatus() {
        if (!this._queueStatusWrap) return;
        this._queueStatusWrap.innerHTML = "";

        const label = document.createElement("span");
        label.textContent = "Queue";
        label.style.cssText = `
            color: ${COLORS.textMuted};
            font-size: 9px;
            font-weight: 600;
            letter-spacing: 0.02em;
        `;
        this._queueStatusWrap.appendChild(label);

        const { badges, counts } = this._queueChromeBadges();
        for (const badge of badges) {
            const pill = document.createElement("span");
            pill.style.cssText = `
                padding: 1px 7px;
                border-radius: 999px;
                background: ${badge.background};
                border: 1px solid ${badge.border};
                color: #f5f5f5;
                font-size: 9px;
                font-weight: 600;
                line-height: 1.4;
            `;
            pill.textContent = badge.text;
            this._queueStatusWrap.appendChild(pill);
        }

        const titleParts = [];
        if (counts.running > 0) titleParts.push(`${counts.running} running`);
        if (counts.pending > 0) titleParts.push(`${counts.pending} pending`);
        if (this.renderQueueActive === false) {
            this._queueStatusWrap.title = titleParts.length
                ? `Queue inactive: ${titleParts.join(", ")}`
                : "Queue inactive";
        } else {
            this._queueStatusWrap.title = titleParts.length ? `Queue: ${titleParts.join(", ")}` : "Queue: idle";
        }
    }

    _updateToolbar() {
        if (!this._toolBtnSnap) return;
        setButtonVariant(this._toolBtnSnap, this.snappingEnabled ? "active" : "muted");
        setButtonVariant(this._toolBtnRazor, this._razorMode ? "danger" : "muted");
        if (this._toolBtnTimecode) {
            const isTc = this._timecodeMode === "timecode";
            setButtonVariant(this._toolBtnTimecode, isTc ? "warning" : "muted");
        }

        this._refreshSelectionInputs();

        // Animatic toggle state
        if (this._toolBtnAnimatic) {
            setButtonVariant(this._toolBtnAnimatic, this._animaticMode ? "warning" : "muted");
        }
        this._updateBatchButtonLabel();
        this._updateQueueChromeStatus();
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
        if (this._fpsInput) {
            this._fpsInput.value = scene.fps || "";
            this._fpsInput.placeholder = String(this.fps);
        }
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
        const dirName = this._projectDirName();
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
            await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ width: w, height: h }),
            });
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
        const dirName = this._projectDirName();
        const sceneRef = this.activeScene;
        const sceneId = this.activeSceneId;
        try {
            await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ fps }),
            });
            sceneRef.fps = fps;
            if (this.activeScene === sceneRef && this.activeSceneId === sceneId) {
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

        const dirName = this.projectDir.split(/[/\\]/).pop();
        try {
            await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name }),
            });
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
        const dirName = this._projectDirName();
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
            await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ duration_frames: frames }),
            });
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
            const newId = newScene.scene_id;

            await this._fetchScenes();
            const copied = this.scenes.find(s => s.scene_id === newId);
            if (copied) this._setActiveScene(copied);
        } catch (e) {
            console.warn("[Sonder] Failed to duplicate scene:", e);
        }
    }

    // ── Asset Management ───────────────────────────────────────────────
    async _fetchAssets() {
        if (!this.projectDir) return;

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

    _snapSceneDurationToTemplate(frames) {
        const numeric = Math.max(1, parseInt(frames, 10) || 1);
        const frameConstraint = this._getActiveTemplate()?.constraints?.frames;
        if (!frameConstraint) return numeric;
        const durationConstraint = { ...frameConstraint };
        delete durationConstraint.max;
        return Math.max(1, Math.round(snapToConstraint(numeric, durationConstraint)));
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
            input.style.background = readOnly ? COLORS.panelMuted : "";
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
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ template_id: templateId, frame_constraint: frameConstraint }),
            });
            return !!resp.ok;
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
        this._updateToolbar();
    }

    _updateContextFrameWidgets() {
        const pre = Math.max(0, parseInt(this._preContextInput?.value, 10) || 0);
        const post = Math.max(0, parseInt(this._postContextInput?.value, 10) || 0);
        const maskPre = Math.max(0, parseInt(this._maskPreOffsetInput?.value, 10) || 0);
        const maskPost = Math.max(0, parseInt(this._maskPostOffsetInput?.value, 10) || 0);
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
                color: cfg.color || COLORS.motionDriver,
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

    /** Get y offset of a layout index accounting for collapsed tracks */
    _trackY(layoutIdx) {
        const ts = this._scaleTimeline;
        let y = Math.round(RULER_HEIGHT * ts);
        for (let i = 0; i < layoutIdx; i++) {
            y += Math.round((this._trackLayout[i]?.collapsed ? TRACK_COLLAPSED_HEIGHT : TRACK_HEIGHT) * ts);
        }
        return y;
    }

    /** Get height of a layout index accounting for collapsed state */
    _trackH(layoutIdx) {
        const entry = this._trackLayout[layoutIdx];
        return Math.round((entry?.collapsed ? TRACK_COLLAPSED_HEIGHT : TRACK_HEIGHT) * this._scaleTimeline);
    }

    /** Total height of all tracks */
    _totalTracksHeight() {
        const ts = this._scaleTimeline;
        let h = 0;
        for (const entry of this._trackLayout) {
            h += Math.round((entry.collapsed ? TRACK_COLLAPSED_HEIGHT : TRACK_HEIGHT) * ts);
        }
        return h + this._timelineGuideLabelReserve();
    }

    _timelineRulerHeight() {
        return Math.round(RULER_HEIGHT * this._scaleTimeline);
    }

    _timelineGuideLabelReserve() {
        return this.isFullscreen ? Math.round(GUIDE_LABEL_RESERVE * this._scaleTimeline) : 0;
    }

    _visibleTimelineContentHeight() {
        return Math.max(0, this._timelineHeight - this._timelineRulerHeight());
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
        const maxScroll = Math.max(0, this._totalTracksHeight() - this._visibleTimelineContentHeight());
        this.scrollY = Math.max(0, Math.min(maxScroll, this.scrollY));
    }

    _trackContentYFromRawY(rawY) {
        const rulerH = this._timelineRulerHeight();
        if (rawY < rulerH) return null;
        return rawY - rulerH + this.scrollY;
    }

    _layoutIndexFromRawY(rawY) {
        const contentY = this._trackContentYFromRawY(rawY);
        if (contentY === null) return -1;
        let offset = 0;
        for (let i = 0; i < this._trackLayout.length; i++) {
            const trackH = this._trackH(i);
            if (contentY >= offset && contentY < offset + trackH) {
                return i;
            }
            offset += trackH;
        }
        return -1;
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
        return new Set(
            this._groupRenderQueueJobs(queue || [])
                .filter((entry) => entry.type === "batch")
                .map((entry) => entry.batchId)
                .filter(Boolean)
        );
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

    _setQueueBatchExpanded(batchId, expanded, options = {}) {
        if (!batchId) return;
        const { persist = false, render = true } = options;
        if (expanded) {
            delete this._queueBatchExpanded[batchId];
        } else {
            this._queueBatchExpanded[batchId] = false;
        }
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
        if (!this._settingsPanelControls) return;
        const controls = this._settingsPanelControls;
        if (controls.scaleToolbarLabel) controls.scaleToolbarLabel.textContent = `${Math.round(this._settings.layout.scaleToolbar * 100)}%`;
        if (controls.scaleTrackHeadersLabel) controls.scaleTrackHeadersLabel.textContent = `${Math.round(this._settings.layout.scaleTrackHeaders * 100)}%`;
        if (controls.scaleTimelineLabel) controls.scaleTimelineLabel.textContent = `${Math.round(this._settings.layout.scaleTimeline * 100)}%`;
        if (controls.scaleGalleryLabel) controls.scaleGalleryLabel.textContent = `${Math.round(this._settings.layout.scaleGallery * 100)}%`;
        if (controls.snappingEnabled) controls.snappingEnabled.checked = !!this._settings.timelineBehavior.snappingEnabled;
        if (controls.snapThreshold) controls.snapThreshold.value = String(this._settings.timelineBehavior.snapThreshold);
        for (const option of SNAP_TARGET_OPTIONS) {
            const control = controls[`snapTarget_${option.key}`];
            if (control) control.checked = !!this._settings.timelineBehavior.snapTargets[option.key];
        }
        if (controls.timecodeMode) controls.timecodeMode.value = this._settings.timelineBehavior.timecodeMode;
        if (controls.loopSelection) controls.loopSelection.checked = !!this._settings.playback.loopSelection;
        if (controls.autoScrollPlayhead) controls.autoScrollPlayhead.checked = !!this._settings.playback.autoScrollPlayhead;
        if (controls.returnToPlaybackStart) controls.returnToPlaybackStart.checked = !!this._settings.playback.returnToPlaybackStart;
        if (controls.playbackResolution) controls.playbackResolution.value = this._settings.playback.resolution;
        if (controls.prebufferEnabled) controls.prebufferEnabled.checked = !!this._settings.playback.prebufferEnabled;
        if (controls.prebufferLookaheadMs) controls.prebufferLookaheadMs.value = String(this._settings.playback.prebufferLookaheadMs);
        if (controls.waveformAccent) controls.waveformAccent.value = this._settings.appearance.waveformAccent;
        if (controls.timelineBrightness) controls.timelineBrightness.value = String(this._settings.appearance.timelineBrightness);
        if (controls.timelineBrightnessLabel) controls.timelineBrightnessLabel.textContent = `${this._settings.appearance.timelineBrightness}%`;
        if (controls.clipLabelMode) controls.clipLabelMode.value = this._settings.appearance.clipLabelMode;
        for (const tintKey of ["video", "audio", "motion_driver"]) {
            const tintInput = controls[`laneTintOverride_${tintKey}`];
            if (tintInput) {
                const stored = this._settings.appearance.laneTintOverrides?.[tintKey] || "";
                tintInput.value = stored || "#000000";
                tintInput.dataset.active = stored ? "1" : "0";
            }
        }
        if (controls.takePlacementMode) controls.takePlacementMode.value = this._settings.render?.takePlacementMode ?? "trimmed";
        if (controls.defaultSavePreset) {
            controls.defaultSavePreset.value = this._settings.render?.defaultSavePreset ?? DEFAULT_SAVE_PRESET;
            controls.defaultSavePreset._sonderSyncTitle?.();
        }
        if (controls.guideSnapshotMaxLongEdge) controls.guideSnapshotMaxLongEdge.value = String(this._settings.guides?.guideSnapshotMaxLongEdge ?? 0);
        if (controls.hoverPreviewEnabled) controls.hoverPreviewEnabled.checked = this._settings.guides?.hoverPreviewEnabled ?? true;
        if (controls.hoverPreviewSize) controls.hoverPreviewSize.value = String(this._guideHoverPreviewSize());
        for (const sync of controls._syncPresetNumberControls || []) {
            sync();
        }
        if (controls.trashRetentionDays) controls.trashRetentionDays.value = String(this._trashRetentionDays());
        if (controls.batchRenderMaxFramesPerChunk) controls.batchRenderMaxFramesPerChunk.value = String(this._settings.batchRender.maxFramesPerChunk);
        if (controls.defaultProjectFps) controls.defaultProjectFps.value = String(this._settings.projectDefaults.fps);
        if (controls.defaultProjectWidth) controls.defaultProjectWidth.value = String(this._settings.projectDefaults.width);
        if (controls.defaultProjectHeight) controls.defaultProjectHeight.value = String(this._settings.projectDefaults.height);
        if (controls.defaultSceneDuration) controls.defaultSceneDuration.value = String(this._settings.projectDefaults.newSceneDuration);
        if (controls.defaultGuideStrength) controls.defaultGuideStrength.value = String(this._settings.projectDefaults.defaultGuideStrength);
        if (controls.defaultMotionDriverStrength) controls.defaultMotionDriverStrength.value = String(this._settings.projectDefaults.defaultMotionDriverStrength);
        if (controls.defaultTemplateId) controls.defaultTemplateId.value = this._settings.projectDefaults.defaultTemplateId || "free";
        if (controls.gallerySortMode) controls.gallerySortMode.value = this._settings.gallery.sortMode;
        if (controls.galleryInspectorCollapsed) controls.galleryInspectorCollapsed.checked = !!this._settings.gallery.inspectorCollapsed;
        if (controls.galleryThumbnailSize) controls.galleryThumbnailSize.value = this._settings.gallery.thumbnailSize;
        this._renderModelTemplateSettings?.();
    }

    _timelineBrightnessFactor() {
        return (this._settings?.appearance?.timelineBrightness || DEFAULT_EDITOR_SETTINGS.appearance.timelineBrightness) / 100;
    }

    _timelineColor(hex) {
        return scaleColor(hex, this._timelineBrightnessFactor());
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
        const canvas = this.timelineCanvas;
        const rect = canvas.parentElement?.getBoundingClientRect();
        const width = rect ? Math.floor(rect.width) : 400;
        const rulerH = this._timelineRulerHeight();
        const canvasH = Math.max(rulerH + 1, this._timelineHeight);
        this._clampScrollX();
        this._clampScrollY();
        this._refreshPlayheadInput?.();

        // Canvas at 1:1 — per-section scales handle individual elements
        canvas.width = width;
        canvas.height = canvasH;
        canvas.style.width = width + "px";
        canvas.style.height = canvasH + "px";

        const ctx = canvas.getContext("2d");
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.save();

        // Background
        ctx.fillStyle = this._timelineColor(COLORS.bg);
        ctx.fillRect(0, 0, width, canvasH);

        this._drawRuler(ctx, width);
        this._drawPlayheadTriangle(ctx, width);

        ctx.save();
        ctx.beginPath();
        ctx.rect(0, rulerH, width, Math.max(0, canvasH - rulerH));
        ctx.clip();
        ctx.translate(0, -this.scrollY);
        this._drawTracks(ctx, width);
        this._drawSelection(ctx, width);
        this._drawGuideMarkers(ctx, width);
        this._drawClips(ctx, width);
        this._drawPlayheadLine(ctx, width);
        ctx.restore();

        this._drawSnapIndicator(ctx, width, canvasH);
        this._drawVerticalScrollbar(ctx, width, canvasH);
        ctx.restore();
        this._updateToolbar();
    }

    get _labelW() {
        const userW = this.isFullscreen ? this._labelWidthUserFS : this._labelWidthUser;
        const baseW = userW > 0 ? userW : (this.isFullscreen ? LABEL_WIDTH_FS : LABEL_WIDTH);
        return Math.round(baseW * this._scaleTrackHeaders);
    }

    _frameToX(frame) {
        return this._labelW + (frame - this.scrollX) * this.pixelsPerFrame;
    }

    _xToFrame(x) {
        return Math.round((x - this._labelW) / this.pixelsPerFrame + this.scrollX);
    }

    _clampScrollX() {
        const rect = this.timelineCanvas?.parentElement?.getBoundingClientRect();
        const width = rect ? Math.floor(rect.width) : 400;
        const visibleFrames = (width - this._labelW) / this.pixelsPerFrame;
        const maxScroll = Math.max(0, this.totalFrames - visibleFrames + 5);
        this.scrollX = Math.max(0, Math.min(maxScroll, this.scrollX));
    }

    _drawRuler(ctx, width) {
        const ts = this._scaleTimeline;
        const rulerH = Math.round(RULER_HEIGHT * ts);
        ctx.fillStyle = this._timelineColor(COLORS.ruler);
        ctx.fillRect(0, 0, width, rulerH);

        ctx.strokeStyle = COLORS.rulerTick;
        ctx.fillStyle = COLORS.rulerText;
        ctx.font = `${Math.round(9 * ts)}px monospace`;
        ctx.textAlign = "center";

        // Determine tick spacing based on zoom
        let majorEvery = 10;
        if (this.pixelsPerFrame < 2) majorEvery = 50;
        else if (this.pixelsPerFrame < 5) majorEvery = 25;
        else if (this.pixelsPerFrame > 10) majorEvery = 5;

        // For timecode mode, adjust major ticks to align with seconds
        if (this._timecodeMode === "timecode") {
            const fps = this._effectiveFps;
            if (this.pixelsPerFrame * fps < 80) {
                majorEvery = fps * 5; // every 5 seconds
            } else {
                majorEvery = fps; // every 1 second
            }
        }

        const startFrame = Math.max(0, Math.floor(this.scrollX));
        const endFrame = Math.min(this.totalFrames, Math.ceil(this.scrollX + (width - this._labelW) / this.pixelsPerFrame));

        for (let f = startFrame; f <= endFrame; f++) {
            const x = this._frameToX(f);
            if (x < 0 || x > width) continue;

            if (f % majorEvery === 0) {
                ctx.beginPath();
                ctx.moveTo(x, rulerH - Math.round(12 * ts));
                ctx.lineTo(x, rulerH);
                ctx.stroke();
                ctx.fillText(this._frameToTimecode(f), x, rulerH - Math.round(13 * ts));
            } else if (f % (majorEvery / 5) === 0 && this.pixelsPerFrame > 1.5) {
                ctx.beginPath();
                ctx.moveTo(x, rulerH - Math.round(6 * ts));
                ctx.lineTo(x, rulerH);
                ctx.stroke();
            }
        }
    }

    _drawTracks(ctx, width) {
        const hs = this._scaleTrackHeaders;
        const headerW = this._labelW; // already scaled by _scaleTrackHeaders
        const fs = this.isFullscreen;
        for (let i = 0; i < this._trackLayout.length; i++) {
            const entry = this._trackLayout[i];
            const y = this._trackY(i);
            const h = this._trackH(i);
            const collapsed = entry.collapsed;
            const isLane = this._isLaneTrackType(entry.type);
            const hasHeaderControls = this._isHeaderControllableTrackType(entry.type);

            // Track background: alternating navy base, plus optional per-type tint overlay from settings
            ctx.fillStyle = i % 2 === 0 ? this._timelineColor(COLORS.track) : this._timelineColor(COLORS.bg);
            ctx.fillRect(0, y, width, h);
            if (isLane) {
                const tint = this._resolveLaneTint(entry.type);
                if (tint) {
                    ctx.save();
                    ctx.globalAlpha = 0.18;
                    ctx.fillStyle = tint;
                    ctx.fillRect(0, y, width, h);
                    ctx.restore();
                }
            }

            if (collapsed) {
                // Collapsed: just arrow + short label
                ctx.fillStyle = "#555";
                ctx.font = `${Math.round(7 * hs)}px sans-serif`;
                ctx.textAlign = "left";
                ctx.fillText(`▸ ${entry.label}`, Math.round((fs ? 6 : 3) * hs), y + h / 2 + 2);
            } else {
                // --- Header layout (left to right) ---
                // Positions scale with fullscreen AND _scaleTrackHeaders
                const arrowX = Math.round((fs ? 6 : 3) * hs);
                const iconSize = Math.round((fs ? 14 : 11) * hs);
                let curX = arrowX;

                // 1. Collapse arrow
                ctx.fillStyle = COLORS.textDim;
                ctx.font = `${iconSize}px sans-serif`;
                ctx.textAlign = "left";
                ctx.fillText("▾", curX, y + h / 2 + Math.round((fs ? 5 : 4) * hs));
                curX += iconSize + Math.round(2 * hs);

                if (hasHeaderControls) {
                    // 2. Lock icon — bright red-orange when locked, dim when unlocked
                    if (entry.locked) {
                        // Draw bright background indicator for locked state
                        ctx.fillStyle = "rgba(255, 80, 60, 0.3)";
                        ctx.fillRect(curX - 1, y + 2, iconSize + 1, h - 4);
                    }
                    ctx.fillStyle = entry.locked ? "#ff5544" : "#666";
                    ctx.font = `${iconSize - Math.round(2 * hs)}px sans-serif`;
                    ctx.fillText(entry.locked ? "🔒" : "🔓", curX, y + h / 2 + Math.round((fs ? 4 : 3) * hs));
                    curX += iconSize + Math.round(1 * hs);

                    // 3. Hide/Mute icon
                    const visibilityState = this._trackVisibilityState(entry);
                    const isAudioLike = entry.type === TRACK_TYPE.AUDIO || entry.type === TRACK_TYPE.PROMPT;
                    ctx.fillStyle = visibilityState === "hidden"
                        ? "#e05050"
                        : visibilityState === "partial"
                            ? COLORS.warningText
                            : "#555";
                    const visibleIcon = isAudioLike ? "🔊" : "👁";
                    const hiddenIcon = isAudioLike ? "🔇" : "🚫";
                    ctx.fillText(
                        visibilityState === "partial" ? "◐" : (visibilityState === "hidden" ? hiddenIcon : visibleIcon),
                        curX,
                        y + h / 2 + Math.round((fs ? 4 : 3) * hs)
                    );
                    curX += iconSize + Math.round(1 * hs);

                    // 4. Color bar
                    if (isLane && entry.color) {
                        ctx.fillStyle = entry.color;
                        ctx.fillRect(curX, y + 2, Math.round(4 * hs), h - 4);
                    }
                    curX += Math.round(7 * hs);
                }

                // 5. Label
                ctx.fillStyle = hasHeaderControls && this._trackVisibilityState(entry) === "hidden" ? "#666" : COLORS.textDim;
                ctx.font = `${Math.round((fs ? 10 : 8) * hs)}px sans-serif`;
                ctx.textAlign = "left";
                const labelText = entry.label;
                const maxLabelW = headerW - curX - 2;
                ctx.save();
                ctx.beginPath();
                ctx.rect(curX, y, maxLabelW, h);
                ctx.clip();
                ctx.fillText(labelText, curX, y + h / 2 + Math.round(3 * hs));
                ctx.restore();
            }

            // Border
            ctx.strokeStyle = this._timelineColor(COLORS.trackBorder);
            ctx.beginPath();
            ctx.moveTo(0, y + h);
            ctx.lineTo(width, y + h);
            ctx.stroke();
        }

        // Header/timeline boundary separator (draggable)
        const bx = this._labelW;
        ctx.strokeStyle = "#555";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(bx, 0);
        ctx.lineTo(bx, this._trackY(this._trackLayout.length - 1) + this._trackH(this._trackLayout.length - 1));
        ctx.stroke();
    }

    _drawSelection(ctx, width) {
        const range = this._selectionContextRange();
        if (!range) return;

        const x1 = this._frameToX(range.selectionStart);
        const x2 = this._frameToX(range.selectionEnd);
        const contextX1 = this._frameToX(range.contextStart);
        const contextX2 = this._frameToX(range.contextEnd);
        const y = this._timelineRulerHeight();
        const h = this._totalTracksHeight();

        if (range.hasPreContext) {
            ctx.fillStyle = COLORS.selectionContext;
            ctx.fillRect(contextX1, y, x1 - contextX1, h);
            ctx.strokeStyle = COLORS.selectionContextBorder;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(contextX1, y);
            ctx.lineTo(contextX1, y + h);
            ctx.stroke();
        }

        if (range.hasPostContext) {
            ctx.fillStyle = COLORS.selectionContext;
            ctx.fillRect(x2, y, contextX2 - x2, h);
            ctx.strokeStyle = COLORS.selectionContextBorder;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(contextX2, y);
            ctx.lineTo(contextX2, y + h);
            ctx.stroke();
        }

        if (range.hasMaskPre) {
            const maskX1 = this._frameToX(range.maskStart);
            ctx.fillStyle = COLORS.maskOffset;
            ctx.fillRect(maskX1, y, x1 - maskX1, h);
            ctx.strokeStyle = COLORS.maskOffsetBorder;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(maskX1, y);
            ctx.lineTo(maskX1, y + h);
            ctx.stroke();
        }

        if (range.hasMaskPost) {
            const maskX2 = this._frameToX(range.maskEnd);
            ctx.fillStyle = COLORS.maskOffset;
            ctx.fillRect(x2, y, maskX2 - x2, h);
            ctx.strokeStyle = COLORS.maskOffsetBorder;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(maskX2, y);
            ctx.lineTo(maskX2, y + h);
            ctx.stroke();
        }

        // Fill
        ctx.fillStyle = COLORS.selection;
        ctx.fillRect(x1, y, x2 - x1, h);

        // Border
        ctx.strokeStyle = COLORS.selectionBorder;
        ctx.lineWidth = 1;
        ctx.strokeRect(x1, y, x2 - x1, h);

        // Handle bars
        ctx.fillStyle = COLORS.selectionBorder;
        ctx.fillRect(x1 - 2, y, 4, h);
        ctx.fillRect(x2 - 2, y, 4, h);
    }

    _drawMutedOverlay(ctx, x, y, w, h, label = "Hidden") {
        if (w <= 1 || h <= 1) return;
        ctx.save();
        ctx.beginPath();
        ctx.rect(x, y, w, h);
        ctx.clip();
        ctx.fillStyle = "rgba(0, 0, 0, 0.28)";
        ctx.fillRect(x, y, w, h);
        ctx.strokeStyle = "rgba(230, 230, 230, 0.26)";
        ctx.lineWidth = 1;
        for (let lx = x - h; lx < x + w + h; lx += 8) {
            ctx.beginPath();
            ctx.moveTo(lx, y + h);
            ctx.lineTo(lx + h, y);
            ctx.stroke();
        }
        if (w > 28) {
            ctx.fillStyle = "rgba(0, 0, 0, 0.56)";
            const badgeW = Math.min(w - 4, Math.max(24, label.length * 6 + 8));
            const badgeH = Math.min(14, h - 4);
            ctx.fillRect(x + 3, y + 3, badgeW, badgeH);
            ctx.fillStyle = "#e8edf2";
            ctx.font = `${Math.max(8, Math.round(8 * this._scaleTimeline))}px sans-serif`;
            ctx.textAlign = "left";
            ctx.fillText(label, x + 7, y + 3 + badgeH - 4);
        }
        ctx.restore();
    }

    _drawGuideMarkers(ctx, width) {
        if (!this.activeScene) return;
        const gi = this._guidesLayoutIdx();
        if (gi < 0 || this._trackLayout[gi].collapsed) return;

        const guides = this.activeScene.guide_frames || [];
        const y = this._trackY(gi);
        const h = this._trackH(gi);
        const trackHidden = !!this._trackLayout[gi]?.hidden;

        for (const guide of guides) {
            let idx = guide._previewFrameIndex ?? guide.frame_index;
            if (idx === -1) idx = this.totalFrames - 1;
            const guideAsset = this._getGuideAsset(guide);
            const isMissingGuide = !guideAsset || !!guideAsset.missing;

            const x = this._frameToX(idx);
            if (x < 0 || x > width) continue;

            // Diamond marker
            const isSelectedGuide = this._isSelected("guide", guide.frame_index);
            const guideHidden = trackHidden || !!guide.muted;
            ctx.globalAlpha = guideHidden ? 0.42 : 1.0;
            ctx.fillStyle = isMissingGuide
                ? (isSelectedGuide ? "#ffb18c" : "#c97a59")
                : (isSelectedGuide ? COLORS.guideSelected : COLORS.guide);
            ctx.beginPath();
            ctx.moveTo(x, y + 4);
            ctx.lineTo(x + 8, y + h / 2);
            ctx.lineTo(x, y + h - 4);
            ctx.lineTo(x - 8, y + h / 2);
            ctx.closePath();
            ctx.fill();
            if (guideHidden) {
                ctx.strokeStyle = "rgba(255,255,255,0.45)";
                ctx.lineWidth = 1;
                ctx.stroke();
            }
            ctx.globalAlpha = 1.0;

            // Label
            ctx.fillStyle = guideHidden ? COLORS.textDim : COLORS.text;
            ctx.font = `${Math.round(8 * this._scaleTimeline)}px monospace`;
            ctx.textAlign = "center";
            ctx.fillText(`f${idx}`, x, y + h + Math.round(10 * this._scaleTimeline));
        }
    }

    _drawClips(ctx, width) {
        if (!this.activeScene) return;

        // Helper: draw trimmed-off ghost region during active trim drag
        const drawTrimGhost = (trimItem, trackY, trackH, color) => {
            if (!this._trimItem || this._trimItem.data !== trimItem) return;
            const item = this._trimItem;
            const isPrompt = item.type === "prompt";
            const curStart = isPrompt ? item.data.start_frame : item.data.timeline_start_frame;
            const curEnd = isPrompt ? item.data.end_frame : item.data.timeline_end_frame;
            ctx.globalAlpha = 0.25;
            ctx.fillStyle = color;
            if (item.edge === "left" && curStart > item.origStart) {
                const ghostX1 = this._frameToX(item.origStart);
                const ghostX2 = this._frameToX(curStart);
                ctx.fillRect(ghostX1 + 1, trackY + 2, ghostX2 - ghostX1 - 1, trackH - 4);
            } else if (item.edge === "right" && curEnd < item.origEnd) {
                const ghostX1 = this._frameToX(curEnd);
                const ghostX2 = this._frameToX(item.origEnd);
                ctx.fillRect(ghostX1, trackY + 2, ghostX2 - ghostX1 - 1, trackH - 4);
            }
            ctx.globalAlpha = 1.0;
        };

        // Video and motion-driver clips.
        const allClips = this.activeScene.clips || [];
        for (let _vli = 0; _vli < this._trackLayout.length; _vli++) {
            const _vlEntry = this._trackLayout[_vli];
            if (_vlEntry.type !== TRACK_TYPE.VIDEO && _vlEntry.type !== TRACK_TYPE.MOTION_DRIVER) continue;
            if (_vlEntry.collapsed) continue;
            const videoY = this._trackY(_vli);
            const videoH = this._trackH(_vli);
            const laneHidden = _vlEntry.hidden;
            const isMotionDriverLane = _vlEntry.type === TRACK_TYPE.MOTION_DRIVER;
            const clips = allClips.filter(c => this._clipMatchesTrackEntry(c, _vlEntry));
            for (const clip of clips) {
                const x1 = this._frameToX(clip.timeline_start_frame);
                const x2 = this._frameToX(clip.timeline_end_frame);
                if (x2 < 0 || x1 > width) continue;

                const isSelectedClip = this._isSelected("clip", clip.clip_id);
                const opacity = clip.opacity ?? 1.0;
                const clipMuted = !!clip.muted;
                const baseAlpha = (laneHidden || clipMuted) ? 0.3 : (opacity < 1.0 ? Math.max(0.3, opacity) : 1.0);
                const clipAsset = this._getAssetForSourcePath(clip.source_path);
                const isMissingClip = !clipAsset || !!clipAsset.missing;
                const laneBaseColor = _vlEntry.color || (isMotionDriverLane ? COLORS.motionDriver : COLORS.clip);
                const clipFillColor = isSelectedClip
                    ? (isMotionDriverLane ? COLORS.motionDriverSelected : lightenColor(laneBaseColor, 0.3))
                    : laneBaseColor;
                ctx.globalAlpha = baseAlpha;

                // Draw base fill
                ctx.fillStyle = isMissingClip
                    ? (isSelectedClip ? "#c97a59" : "#6d3f33")
                    : clipFillColor;
                ctx.fillRect(x1 + 1, videoY + 2, x2 - x1 - 2, videoH - 4);

                // Thumbnail strip filmstrip (tiled at natural aspect ratio)
                if (clipAsset && !isMissingClip && (x2 - x1) > 10) {
                    const strip = this._getOrLoadThumbStrip(clipAsset.asset_id);
                    if (strip && strip.loaded && strip.img.naturalWidth > 0) {
                        ctx.save();
                        ctx.beginPath();
                        ctx.rect(x1 + 1, videoY + 2, x2 - x1 - 2, videoH - 4);
                        ctx.clip();
                        ctx.globalAlpha = baseAlpha * 0.55;

                        const destH = videoH - 4;
                        // Scale each strip frame to fill track height, preserving aspect ratio
                        const tileW = Math.max(1, Math.round(strip.frameWidth * destH / strip.img.naturalHeight));
                        const totalSourceFrames = clipAsset.frame_count || 1;
                        const srcIn = clip.source_in_frame || 0;
                        const srcOut = clip.source_out_frame || totalSourceFrames;
                        // Tile frames across the clip width
                        const clipPixelW = x2 - x1 - 2;
                        for (let px = 0; px < clipPixelW; px += tileW) {
                            // Map this pixel position to a source frame, then to a strip column
                            const frac = px / clipPixelW;
                            const sourceFrame = srcIn + frac * (srcOut - srcIn);
                            const col = Math.floor(sourceFrame / totalSourceFrames * strip.numFrames);
                            const clampedCol = Math.min(col, strip.numFrames - 1);
                            const sx = clampedCol * strip.frameWidth;
                            const drawW = Math.min(tileW, clipPixelW - px);
                            const srcDrawW = drawW / tileW * strip.frameWidth;
                            ctx.drawImage(strip.img, sx, 0, srcDrawW, strip.img.naturalHeight,
                                          x1 + 1 + px, videoY + 2, drawW, destH);
                        }

                        // Tint overlay — use lane color if available, fallback to default blue
                        ctx.restore();
                    }
                }
                    // No thumbnail — apply lane color tint directly on base fill
                if (isSelectedClip) {
                    ctx.strokeStyle = isMissingClip ? "#ffd0bc" : lightenColor(laneBaseColor, 0.45);
                    ctx.lineWidth = 1;
                    ctx.strokeRect(x1 + 1, videoY + 2, x2 - x1 - 2, videoH - 4);
                }
                ctx.globalAlpha = baseAlpha;

                // Opacity visual: diagonal hash lines when opacity < 100%
                if (opacity < 1.0) {
                    ctx.save();
                    ctx.beginPath();
                    ctx.rect(x1 + 1, videoY + 2, x2 - x1 - 2, videoH - 4);
                    ctx.clip();
                    ctx.strokeStyle = "rgba(0,0,0,0.4)";
                    ctx.lineWidth = 1;
                    const step = 6;
                    for (let lx = x1 - videoH; lx < x2; lx += step) {
                        ctx.beginPath();
                        ctx.moveTo(lx, videoY + videoH - 2);
                        ctx.lineTo(lx + videoH, videoY + 2);
                        ctx.stroke();
                    }
                    ctx.restore();
                }

                // Clip label
                const label = this._formatClipTimelineLabel(clip, clipAsset, isMissingClip);
                if (label) {
                    ctx.fillStyle = isMissingClip ? "#ffd0bc" : COLORS.text;
                    ctx.font = `${Math.round(9 * this._scaleTimeline)}px sans-serif`;
                    ctx.textAlign = "left";
                    const labelX = isMotionDriverLane ? x1 + Math.round(26 * this._scaleTimeline) : x1 + 4;
                    ctx.fillText(label, labelX, videoY + videoH / 2 + Math.round(3 * this._scaleTimeline));
                }

                if (isMotionDriverLane && (x2 - x1) > 24) {
                    const badgeW = Math.round(18 * this._scaleTimeline);
                    const badgeH = Math.round(12 * this._scaleTimeline);
                    const badgeX = x1 + Math.round(4 * this._scaleTimeline);
                    const badgeY = videoY + Math.round(5 * this._scaleTimeline);
                    ctx.globalAlpha = baseAlpha;
                    ctx.fillStyle = "rgba(0, 0, 0, 0.42)";
                    ctx.fillRect(badgeX, badgeY, badgeW, badgeH);
                    ctx.fillStyle = "#d8ffff";
                    ctx.font = `${Math.round(8 * this._scaleTimeline)}px sans-serif`;
                    ctx.textAlign = "center";
                    ctx.fillText("MD", badgeX + badgeW / 2, badgeY + badgeH - Math.round(3 * this._scaleTimeline));
                }

                if (isMissingClip) {
                    ctx.save();
                    ctx.beginPath();
                    ctx.rect(x1 + 1, videoY + 2, x2 - x1 - 2, videoH - 4);
                    ctx.clip();
                    ctx.strokeStyle = "rgba(255,208,188,0.35)";
                    ctx.lineWidth = 1;
                    for (let lx = x1 - videoH; lx < x2 + videoH; lx += 8) {
                        ctx.beginPath();
                        ctx.moveTo(lx, videoY + videoH - 2);
                        ctx.lineTo(lx + videoH, videoY + 2);
                        ctx.stroke();
                    }
                    ctx.restore();
                }

                if (clipMuted) {
                    this._drawMutedOverlay(ctx, x1 + 1, videoY + 2, x2 - x1 - 2, videoH - 4, "Hidden");
                }

                // Permanent trim ghost
                const clipOrigin = clip.source_origin_frame || 0;
                const clipTotal = clip.total_source_frames || 0;
                if (clipTotal > 0) {
                    const leftTrimmed = (clip.source_in_frame || 0) - clipOrigin;
                    const visibleDur = clip.timeline_end_frame - clip.timeline_start_frame;
                    const rightTrimmed = clipTotal - visibleDur - leftTrimmed;
                    if (leftTrimmed > 0) {
                        const ghostX = this._frameToX(clip.timeline_start_frame - leftTrimmed);
                        ctx.globalAlpha = 0.15;
                        ctx.fillStyle = laneBaseColor;
                        ctx.fillRect(ghostX + 1, videoY + 2, x1 - ghostX - 1, videoH - 4);
                        ctx.globalAlpha = 1.0;
                    }
                    if (rightTrimmed > 0) {
                        const ghostX2 = this._frameToX(clip.timeline_end_frame + rightTrimmed);
                        ctx.globalAlpha = 0.15;
                        ctx.fillStyle = laneBaseColor;
                        ctx.fillRect(x2 - 1, videoY + 2, ghostX2 - x2, videoH - 4);
                        ctx.globalAlpha = 1.0;
                    }
                }

                // Active trim drag ghost (during edge-drag)
                if (this.dragType === "trimEdge") drawTrimGhost(clip, videoY, videoH, laneBaseColor);
            }
            ctx.globalAlpha = 1.0;
        }

        // Audio tracks (all audio lanes)
        const allAudioTracks = this.activeScene.audio_tracks || [];
        for (let _ali = 0; _ali < this._trackLayout.length; _ali++) {
            const _alEntry = this._trackLayout[_ali];
            if (_alEntry.type !== TRACK_TYPE.AUDIO || _alEntry.collapsed) continue;
            const audioY = this._trackY(_ali);
            const audioH = this._trackH(_ali);
            const audioLaneHidden = _alEntry.hidden;
            const audioTracks = allAudioTracks.filter(a => (a.lane_index || 0) === _alEntry.laneIndex);
            for (const track of audioTracks) {
                const x1 = this._frameToX(track.timeline_start_frame);
                const x2 = this._frameToX(track.timeline_end_frame);
                if (x2 < 0 || x1 > width) continue;

                const isSelectedAudio = this._isSelected("audio", track.track_id);
                const vol = track.volume ?? 1.0;
                const audioAsset = this._getAssetForSourcePath(track.source_path);
                const isMissingAudio = !audioAsset || !!audioAsset.missing;
                const audioMuted = audioLaneHidden || !!track.muted;
                const laneBaseColor = _alEntry.color || COLORS.audioClip;
                const audioFillColor = isSelectedAudio ? lightenColor(laneBaseColor, 0.3) : laneBaseColor;
                ctx.globalAlpha = audioMuted ? 0.3 : 1.0;
                ctx.fillStyle = isMissingAudio
                    ? (isSelectedAudio ? "#c97a59" : "#5f4038")
                    : audioFillColor;
                ctx.fillRect(x1 + 1, audioY + 2, x2 - x1 - 2, audioH - 4);

                // Waveform visualization
                if (audioAsset && !isMissingAudio && (x2 - x1) > 6) {
                    const waveform = this._getOrLoadWaveform(audioAsset.asset_id);
                    if (waveform && waveform.loaded && waveform.peaks.length > 0) {
                        ctx.save();
                        ctx.beginPath();
                        ctx.rect(x1 + 1, audioY + 2, x2 - x1 - 2, audioH - 4);
                        ctx.clip();

                        const clipW = x2 - x1 - 2;
                        const centerY = audioY + audioH / 2;
                        const halfH = (audioH - 8) / 2;

                        // Map visible source frames to waveform buckets
                        const totalDurFrames = audioAsset.duration_sec * this._effectiveFps;
                        const srcIn = track.source_in_frame || 0;
                        const visibleFrames = track.timeline_end_frame - track.timeline_start_frame;
                        const startFrac = totalDurFrames > 0 ? srcIn / totalDurFrames : 0;
                        const endFrac = totalDurFrames > 0 ? (srcIn + visibleFrames) / totalDurFrames : 1;
                        const startBucket = Math.floor(startFrac * waveform.numBuckets);
                        const endBucket = Math.ceil(endFrac * waveform.numBuckets);
                        const bucketSpan = Math.max(1, endBucket - startBucket);

                        ctx.strokeStyle = track.muted ? "rgba(180,180,180,0.5)" : this._waveformAccentColor();
                        ctx.lineWidth = 1;
                        ctx.beginPath();
                        for (let px = 0; px < clipW; px++) {
                            const bi = startBucket + Math.floor(px / clipW * bucketSpan);
                            const peak = waveform.peaks[Math.min(bi, waveform.peaks.length - 1)];
                            if (!peak) continue;
                            const y1 = centerY - peak[1] * halfH;
                            const y2 = centerY - peak[0] * halfH;
                            ctx.moveTo(x1 + 1 + px, y1);
                            ctx.lineTo(x1 + 1 + px, y2);
                        }
                        ctx.stroke();
                        ctx.restore();
                    }
                }

                if (isSelectedAudio) {
                    ctx.strokeStyle = isMissingAudio ? "#ffd0bc" : lightenColor(laneBaseColor, 0.45);
                    ctx.lineWidth = 1;
                    ctx.strokeRect(x1 + 1, audioY + 2, x2 - x1 - 2, audioH - 4);
                }

                // Volume indicator: thin bar at bottom of audio clip
                if (vol < 1.0 && !track.muted) {
                    const volBarW = (x2 - x1 - 4) * vol;
                    ctx.fillStyle = "rgba(255,255,255,0.2)";
                    ctx.fillRect(x1 + 2, audioY + audioH - 5, volBarW, 2);
                }

                // Audio label
                if ((x2 - x1) > 30) {
                    const audioLabel = this._formatAudioTimelineLabel(track, audioAsset, isMissingAudio);
                    if (audioLabel) {
                        ctx.fillStyle = isMissingAudio ? "#ffd0bc" : COLORS.text;
                        ctx.font = `${Math.round(8 * this._scaleTimeline)}px sans-serif`;
                        ctx.textAlign = "left";
                        ctx.fillText(audioLabel, x1 + 4, audioY + audioH / 2 + Math.round(3 * this._scaleTimeline));
                    }
                }

                if (isMissingAudio) {
                    ctx.save();
                    ctx.beginPath();
                    ctx.rect(x1 + 1, audioY + 2, x2 - x1 - 2, audioH - 4);
                    ctx.clip();
                    ctx.strokeStyle = "rgba(255,208,188,0.35)";
                    ctx.lineWidth = 1;
                    for (let lx = x1 - audioH; lx < x2 + audioH; lx += 8) {
                        ctx.beginPath();
                        ctx.moveTo(lx, audioY + audioH - 2);
                        ctx.lineTo(lx + audioH, audioY + 2);
                        ctx.stroke();
                    }
                    ctx.restore();
                }

                if (track.muted) {
                    this._drawMutedOverlay(ctx, x1 + 1, audioY + 2, x2 - x1 - 2, audioH - 4, "Muted");
                }

                // Permanent trim ghost for audio
                const audioOrigin = track.source_origin_frame || 0;
                const audioTotal = track.total_source_frames || 0;
                if (audioTotal > 0) {
                    const audioLeftTrim = (track.source_in_frame || 0) - audioOrigin;
                    const audioVisibleDur = track.timeline_end_frame - track.timeline_start_frame;
                    const audioRightTrim = audioTotal - audioVisibleDur - audioLeftTrim;
                    if (audioLeftTrim > 0) {
                        const ghostX = this._frameToX(track.timeline_start_frame - audioLeftTrim);
                        ctx.globalAlpha = 0.15;
                        ctx.fillStyle = laneBaseColor;
                        ctx.fillRect(ghostX + 1, audioY + 2, x1 - ghostX - 1, audioH - 4);
                        ctx.globalAlpha = 1.0;
                    }
                    if (audioRightTrim > 0) {
                        const ghostX2 = this._frameToX(track.timeline_end_frame + audioRightTrim);
                        ctx.globalAlpha = 0.15;
                        ctx.fillStyle = laneBaseColor;
                        ctx.fillRect(x2 - 1, audioY + 2, ghostX2 - x2, audioH - 4);
                        ctx.globalAlpha = 1.0;
                    }
                }

                // Active trim drag ghost
                if (this.dragType === "trimEdge") drawTrimGhost(track, audioY, audioH, laneBaseColor);
            }
            ctx.globalAlpha = 1.0;
        }

        // Prompt sections
        const pi = this._promptLayoutIdx();
        if (pi >= 0 && !this._trackLayout[pi].collapsed) {
            const sections = this.activeScene.prompt_sections || [];
            const promptY = this._trackY(pi);
            const promptH = this._trackH(pi);
            const promptHidden = !!this._trackLayout[pi]?.hidden;
            for (let si = 0; si < sections.length; si++) {
                const section = sections[si];
                const x1 = this._frameToX(section.start_frame);
                const x2 = this._frameToX(section.end_frame);
                if (x2 < 0 || x1 > width) continue;

                const isSelected = this._isSelected("prompt", si) ||
                    (this._selectedPromptIdx !== null &&
                    this._selectedPromptIdx < sections.length &&
                    sections[this._selectedPromptIdx] === section);

                ctx.globalAlpha = promptHidden ? 0.42 : 1.0;
                ctx.fillStyle = isSelected ? "rgba(180, 120, 255, 0.4)" : COLORS.promptSection;
                ctx.fillRect(x1 + 1, promptY + 2, x2 - x1 - 2, promptH - 4);

                ctx.strokeStyle = isSelected ? "rgba(180, 120, 255, 0.9)" : COLORS.promptBorder;
                ctx.lineWidth = 1;
                ctx.strokeRect(x1 + 1, promptY + 2, x2 - x1 - 2, promptH - 4);

                // Prompt text label (truncated)
                if (section.prompt && (x2 - x1) > 20) {
                    ctx.fillStyle = COLORS.text;
                    ctx.font = `${Math.round(9 * this._scaleTimeline)}px sans-serif`;
                    ctx.textAlign = "left";
                    ctx.save();
                    ctx.beginPath();
                    ctx.rect(x1 + 3, promptY + 2, x2 - x1 - 6, promptH - 4);
                    ctx.clip();
                    ctx.fillText(section.prompt, x1 + 4, promptY + promptH / 2 + Math.round(3 * this._scaleTimeline));
                    ctx.restore();
                }
                ctx.globalAlpha = 1.0;
                if (promptHidden) {
                    this._drawMutedOverlay(ctx, x1 + 1, promptY + 2, x2 - x1 - 2, promptH - 4, "Hidden");
                }

                // Trim ghost
                if (this.dragType === "trimEdge") drawTrimGhost(section, promptY, promptH, "rgba(180, 120, 255, 0.5)");
            }
        }
    }

    _drawPlayheadTriangle(ctx, width) {
        const x = this._frameToX(this.playhead);
        if (x < 0 || x > width) return;

        // Triangle at top
        ctx.fillStyle = COLORS.playhead;
        ctx.beginPath();
        ctx.moveTo(x - 6, 0);
        ctx.lineTo(x + 6, 0);
        ctx.lineTo(x, 8);
        ctx.closePath();
        ctx.fill();
    }

    _drawPlayheadLine(ctx, width) {
        const x = this._frameToX(this.playhead);
        if (x < 0 || x > width) return;

        const rulerH = this._timelineRulerHeight();
        const totalH = rulerH + this._totalTracksHeight();
        ctx.strokeStyle = COLORS.playhead;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(x, rulerH);
        ctx.lineTo(x, totalH);
        ctx.stroke();
    }

    _drawSnapIndicator(ctx, width, height = this.timelineCanvas?.height || 0) {
        if (this._snapIndicator === null) return;
        const x = this._frameToX(this._snapIndicator);
        if (x < 0 || x > width) return;

        ctx.strokeStyle = "#ffff00";
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, height);
        ctx.stroke();
        ctx.setLineDash([]);
    }

    _drawVerticalScrollbar(ctx, width, height) {
        const rulerH = this._timelineRulerHeight();
        const visibleH = Math.max(0, height - rulerH);
        const contentH = this._totalTracksHeight();
        if (visibleH <= 0 || contentH <= visibleH) return;

        const trackX = Math.max(this._labelW + 4, width - 8);
        const trackY = rulerH + 2;
        const trackH = Math.max(8, visibleH - 4);
        const thumbH = Math.max(18, Math.round((visibleH / contentH) * trackH));
        const maxThumbOffset = Math.max(0, trackH - thumbH);
        const scrollRatio = this.scrollY / Math.max(1, contentH - visibleH);
        const thumbY = trackY + Math.round(maxThumbOffset * scrollRatio);

        ctx.fillStyle = "rgba(0,0,0,0.32)";
        ctx.fillRect(trackX, trackY, 5, trackH);
        ctx.fillStyle = "rgba(190,205,220,0.58)";
        ctx.fillRect(trackX + 1, thumbY, 3, thumbH);
    }

    // _updateInfoLabel removed — merged into _updateToolbar()

    // ── Hit Testing ──────────────────────────────────────────────────
    /** Hit-test track header area — returns { layoutIdx, zone } or null */
    _hitTestTrackHeader(x, rawY) {
        const headerWidth = this._labelW; // already scaled by _scaleTrackHeaders
        if (x > headerWidth) return null;
        const fs = this.isFullscreen;
        const hs = this._scaleTrackHeaders;
        const iconSize = Math.round((fs ? 14 : 11) * hs);
        const layoutIdx = this._layoutIndexFromRawY(rawY);
        if (layoutIdx < 0) return null;
        const entry = this._trackLayout[layoutIdx];
        const hasHeaderControls = this._isHeaderControllableTrackType(entry.type);
        if (entry.collapsed || !hasHeaderControls) {
            return { layoutIdx, zone: "collapse" };
        }
        // Zone detection (left to right) matching _drawTracks layout
        const arrowX = Math.round((fs ? 6 : 3) * hs);
        let zoneEnd = arrowX + iconSize + Math.round(2 * hs);
        if (x < zoneEnd) return { layoutIdx, zone: "collapse" };
        zoneEnd += iconSize + Math.round(1 * hs);
        if (x < zoneEnd) return { layoutIdx, zone: "lock" };
        zoneEnd += iconSize + Math.round(1 * hs);
        if (x < zoneEnd) return { layoutIdx, zone: "hide" };
        return { layoutIdx, zone: "label" };
    }

    /** Hit-test the header/timeline boundary for drag resize */
    _hitTestHeaderEdge(x, y) {
        const headerW = this._labelW;
        return Math.abs(x - headerW) <= 4 && y >= 0;
    }

    _hitTestClip(x, rawY) {
        if (!this.activeScene) return null;
        const layoutIdx = this._layoutIndexFromRawY(rawY);
        if (layoutIdx < 0) return null;
        const entry = this._trackLayout[layoutIdx];
        if ((entry.type !== TRACK_TYPE.VIDEO && entry.type !== TRACK_TYPE.MOTION_DRIVER) || entry.collapsed) return null;
        const clips = this.activeScene.clips || [];
        for (const clip of clips) {
            if (!this._clipMatchesTrackEntry(clip, entry)) continue;
            const x1 = this._frameToX(clip.timeline_start_frame);
            const x2 = this._frameToX(clip.timeline_end_frame);
            if (x >= x1 && x <= x2) {
                return { type: "clip", id: clip.clip_id, data: clip };
            }
        }
        return null;
    }

    _hitTestAudio(x, rawY) {
        if (!this.activeScene) return null;
        const layoutIdx = this._layoutIndexFromRawY(rawY);
        if (layoutIdx < 0) return null;
        const entry = this._trackLayout[layoutIdx];
        if (entry.type !== TRACK_TYPE.AUDIO || entry.collapsed) return null;
        const tracks = this.activeScene.audio_tracks || [];
        for (const track of tracks) {
            if ((track.lane_index || 0) !== entry.laneIndex) continue;
            const x1 = this._frameToX(track.timeline_start_frame);
            const x2 = this._frameToX(track.timeline_end_frame);
            if (x >= x1 && x <= x2) {
                return { type: "audio", id: track.track_id, data: track };
            }
        }
        return null;
    }

    _hitTestGuide(x, rawY) {
        if (!this.activeScene) return null;
        const gi = this._guidesLayoutIdx();
        if (gi < 0 || this._trackLayout[gi].collapsed || this._layoutIndexFromRawY(rawY) !== gi) return null;
        const guides = this.activeScene.guide_frames || [];

        for (const guide of guides) {
            let idx = guide.frame_index;
            if (idx === -1) idx = this.totalFrames - 1;
            const gx = this._frameToX(idx);
            if (Math.abs(x - gx) <= 10) {
                return { type: "guide", id: guide.frame_index, data: guide };
            }
        }
        return null;
    }

    _hitTestPrompt(x, rawY) {
        if (!this.activeScene) return null;
        const pli = this._promptLayoutIdx();
        if (pli < 0 || this._trackLayout[pli].collapsed || this._layoutIndexFromRawY(rawY) !== pli) return null;
        const sections = this.activeScene.prompt_sections || [];

        for (let i = 0; i < sections.length; i++) {
            const section = sections[i];
            const x1 = this._frameToX(section.start_frame);
            const x2 = this._frameToX(section.end_frame);
            if (x >= x1 && x <= x2) {
                return { type: "prompt", id: i, data: section };
            }
        }
        return null;
    }

    _hitTestItem(x, rawY) {
        return this._hitTestClip(x, rawY) || this._hitTestAudio(x, rawY) || this._hitTestGuide(x, rawY) || this._hitTestPrompt(x, rawY);
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
        const edgePx = 6;
        if (!this.activeScene) return null;
        const layoutIdx = this._layoutIndexFromRawY(rawY);
        if (layoutIdx < 0) return null;
        const entry = this._trackLayout[layoutIdx];

        if ((entry.type === TRACK_TYPE.VIDEO || entry.type === TRACK_TYPE.MOTION_DRIVER) && !entry.collapsed) {
            for (const clip of (this.activeScene.clips || [])) {
                if (!this._clipMatchesTrackEntry(clip, entry)) continue;
                const x1 = this._frameToX(clip.timeline_start_frame);
                const x2 = this._frameToX(clip.timeline_end_frame);
                if (Math.abs(x - x1) < edgePx) return { type: "clip", id: clip.clip_id, data: clip, edge: "left" };
                if (Math.abs(x - x2) < edgePx) return { type: "clip", id: clip.clip_id, data: clip, edge: "right" };
            }
        }

        if (entry.type === TRACK_TYPE.AUDIO && !entry.collapsed) {
            for (const track of (this.activeScene.audio_tracks || [])) {
                if ((track.lane_index || 0) !== entry.laneIndex) continue;
                const x1 = this._frameToX(track.timeline_start_frame);
                const x2 = this._frameToX(track.timeline_end_frame);
                if (Math.abs(x - x1) < edgePx) return { type: "audio", id: track.track_id, data: track, edge: "left" };
                if (Math.abs(x - x2) < edgePx) return { type: "audio", id: track.track_id, data: track, edge: "right" };
            }
        }

        if (entry.type === TRACK_TYPE.PROMPT && !entry.collapsed) {
            const sections = this.activeScene.prompt_sections || [];
            for (let i = 0; i < sections.length; i++) {
                const section = sections[i];
                const x1 = this._frameToX(section.start_frame);
                const x2 = this._frameToX(section.end_frame);
                if (Math.abs(x - x1) < edgePx) return { type: "prompt", id: i, data: section, edge: "left" };
                if (Math.abs(x - x2) < edgePx) return { type: "prompt", id: i, data: section, edge: "right" };
            }
        }

        return null;
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
                    return;
                }
                await this._fetchAssets();
                await this._fetchScenes();
                this._renderTimeline();
                this._renderViewportFrame();
            } catch (e) {
                console.warn("[Sonder] Failed to drop motion driver:", e);
            }
            return;
        }

        this._pushUndo("add asset");

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
                await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ video_lane_count: newCount }),
                });
                this.activeScene.video_lane_count = newCount;
                this._buildTrackLayout();
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
                    await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}`), {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ audio_lane_count: newAudioCount }),
                    });
                    this.activeScene.audio_lane_count = newAudioCount;
                    this._buildTrackLayout();
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
                await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ audio_lane_count: newCount }),
                });
                this.activeScene.audio_lane_count = newCount;
                this._buildTrackLayout();
            }
        }

        let resp;
        try {
            if (asset.asset_type === "image") {
                // Images always create guide frames (regardless of which track they're dropped on)
                resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}/guides`), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        frame_index: frame,
                        asset_id: asset.asset_id,
                        source: "asset",
                        strength: this._defaultGuideStrength(),
                    }),
                });
                if (!resp.ok) {
                    console.warn("[Sonder] Guide creation failed:", resp.status, await resp.text());
                    return;
                }
                console.log("[Sonder] Guide frame created at frame", frame);
            } else if (asset.asset_type === "video") {
                // Drop video = create clip on target video lane (+ audio track if video has audio)
                const assetObj = _findAsset(asset.asset_id);
                const videoHasAudio = assetObj?.has_audio === true || asset?.has_audio === true;
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
                    return;
                }
            } else if (asset.asset_type === "audio") {
                // Drop audio = create audio track on target audio lane
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
                    return;
                }
            }

            // Refresh both assets and scenes because dual-drop can create a new extracted audio asset.
            await this._fetchAssets();
            await this._fetchScenes();
            this._renderTimeline();
        } catch (e) {
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

        const dirName = this.projectDir.split(/[/\\]/).pop();
        const sceneId = this.activeSceneId;
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
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}/clips/${clipId}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            if (!resp.ok) {
                throw new Error(await resp.text());
            }
            await this._fetchScenes();
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
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const sceneId = this.activeSceneId;
        this._pushUndo("move to new lane");

        try {
            if (hit.type === "clip") {
                if (this._isMotionDriverClip(hit.data)) {
                    this._showToast("Motion-driver lanes are single-lane in this phase.");
                    return;
                }
                // Add a new video lane and move clip there
                const newCount = (this.activeScene.video_lane_count || 1) + 1;
                const newLane = newCount - 1;
                await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ video_lane_count: newCount }),
                });
                await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}/clips/${hit.id}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ track_index: newLane }),
                });
            } else if (hit.type === "audio") {
                const newCount = (this.activeScene.audio_lane_count || 1) + 1;
                const newLane = newCount - 1;
                await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ audio_lane_count: newCount }),
                });
                await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}/audio_tracks/${hit.id}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ lane_index: newLane }),
                });
            }
            await this._fetchScenes();
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
            await this._updateItemProperty(type, id, { muted: !!muted }, { refresh: false });
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
        const dirName = this.projectDir.split(/[/\\]/).pop();
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
        try {
            await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    video_lane_configs: videoConfigs,
                    motion_driver_lane_configs: motionDriverConfigs,
                    audio_lane_configs: audioConfigs,
                    guide_track_config: guideTrackConfig,
                    prompt_track_config: promptTrackConfig,
                }),
            });
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
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const isVideo = trackType === TRACK_TYPE.VIDEO;
        const body = {};
        if (isVideo) {
            body.video_lane_count = (this.activeScene.video_lane_count || 1) + 1;
        } else {
            body.audio_lane_count = (this.activeScene.audio_lane_count || 1) + 1;
        }
        try {
            this._pushUndo("add lane");
            await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            await this._fetchScenes();
            this._buildTrackLayout();
            this._renderTimeline();
        } catch (e) {
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

        const dirName = this.projectDir.split(/[/\\]/).pop();
        try {
            if (willMove) {
                // Move items to adjacent lane
                for (const item of items) {
                    const id = isVideo ? item.clip_id : item.track_id;
                    const endpoint = isVideo ? "clips" : "audio_tracks";
                    const field = isVideo ? "track_index" : "lane_index";
                    await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}/${endpoint}/${id}`), {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ [field]: targetLane }),
                    });
                }
            } else {
                // Delete items on this lane
                for (const item of items) {
                    const id = isVideo ? item.clip_id : item.track_id;
                    const endpoint = isVideo ? "clips" : "audio_tracks";
                    await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}/${endpoint}/${id}`), {
                        method: "DELETE",
                    });
                }
            }
            await this._removeLane(trackType, laneIndex);
        } catch (e) {
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

        const dirName = this.projectDir.split(/[/\\]/).pop();
        try {
            this._pushUndo("delete lane items");
            for (const item of items) {
                const id = isVideo ? item.clip_id : item.track_id;
                const endpoint = isVideo ? "clips" : "audio_tracks";
                await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}/${endpoint}/${id}?preserve_lane=1`), {
                    method: "DELETE",
                });
            }
            this._clearSelection();
            this._hideItemEditor();
            await this._fetchScenes();
            this._buildTrackLayout();
            this._renderTimeline();
            this._renderViewportFrame();
        } catch (e) {
            console.warn("[Sonder] Failed to delete lane items:", e);
        }
    }

    async _removeLane(trackType, laneIndex) {
        if (!this.activeScene || !this.projectDir) return;
        if (trackType === TRACK_TYPE.MOTION_DRIVER) {
            this._showToast("Motion-driver lanes are single-lane in this phase.");
            return;
        }
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const isVideo = trackType === TRACK_TYPE.VIDEO;
        const currentCount = isVideo
            ? (this.activeScene.video_lane_count || 1)
            : (this.activeScene.audio_lane_count || 1);
        if (currentCount <= 1) return;

        const cloneLaneConfig = (cfg) => ({
            name: cfg?.name || "",
            color: cfg?.color || "",
            locked: !!cfg?.locked,
            hidden: !!cfg?.hidden,
        });
        const reindexLaneConfigs = (configs) => {
            const nextCount = currentCount - 1;
            const next = (configs || [])
                .filter((_cfg, idx) => idx !== laneIndex)
                .map(cloneLaneConfig)
                .slice(0, nextCount);
            while (next.length < nextCount) next.push(this._defaultLaneConfig());
            return next;
        };

        const body = {};
        if (isVideo) {
            body.video_lane_count = currentCount - 1;
            body.video_lane_configs = reindexLaneConfigs(this.activeScene.video_lane_configs);
        } else {
            body.audio_lane_count = currentCount - 1;
            body.audio_lane_configs = reindexLaneConfigs(this.activeScene.audio_lane_configs);
        }
        try {
            this._pushUndo("remove lane");
            await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            // Shift items on lanes above the removed one
            if (isVideo) {
                for (const clip of (this.activeScene.clips || [])) {
                    if (this._isRenderClip(clip) && (clip.track_index || 0) > laneIndex) {
                        await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}/clips/${clip.clip_id}`), {
                            method: "PUT",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ track_index: clip.track_index - 1 }),
                        });
                    }
                }
            } else {
                for (const track of (this.activeScene.audio_tracks || [])) {
                    if ((track.lane_index || 0) > laneIndex) {
                        await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}/audio_tracks/${track.track_id}`), {
                            method: "PUT",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ lane_index: track.lane_index - 1 }),
                        });
                    }
                }
            }
            await this._fetchScenes();
            this._buildTrackLayout();
            this._renderTimeline();
        } catch (e) {
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
        this._pushUndo("add prompt");
        const dirName = this.projectDir.split(/[/\\]/).pop();

        try {
            await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}/prompt_sections`), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    start_frame: startFrame,
                    end_frame: endFrame,
                    prompt: promptText,
                }),
            });
            this._hidePromptEditor();
            await this._fetchScenes();
            this._renderTimeline();
        } catch (e) {
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
        this._pushUndo("edit prompt");
        const dirName = this.projectDir.split(/[/\\]/).pop();

        try {
            await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}/prompt_sections/${idx}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(updates),
            });
            await this._fetchScenes();
            this._selectedPromptIdx = null;
            this._renderTimeline();
        } catch (e) {
            console.warn("[Sonder] Failed to update prompt section:", e);
        }
    }

    async _deletePromptSection(idx) {
        if (!this.activeScene || !this.projectDir) return;
        if (this._isPromptTrackLocked()) return;
        this._pushUndo("delete prompt");
        const dirName = this.projectDir.split(/[/\\]/).pop();

        try {
            await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}/prompt_sections/${idx}`), {
                method: "DELETE",
            });
            this._selectedPromptIdx = null;
            this._hidePromptEditor();
            await this._fetchScenes();
            this._renderTimeline();
        } catch (e) {
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
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const sceneId = this.activeSceneId;
        const endpoint = type === "clip"
            ? `/sonder-editor/project/${dirName}/scenes/${sceneId}/clips/${id}`
            : `/sonder-editor/project/${dirName}/scenes/${sceneId}/audio_tracks/${id}`;

        try {
            await fetch(api.apiURL(endpoint), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ timeline_start_frame: newStart }),
            });
            await this._fetchScenes();
            this._clearSelection();
            this._hideItemEditor();
            this._renderTimeline();
        } catch (e) {
            console.warn("[Sonder] Failed to move item:", e);
        }
    }

    async _updateItemProperty(type, id, props, { refresh = true } = {}) {
        if (!this.activeScene || !this.projectDir) return;
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const sceneId = this.activeSceneId;
        const endpoint = type === "clip"
            ? `/sonder-editor/project/${dirName}/scenes/${sceneId}/clips/${id}`
            : type === "guide"
                ? `/sonder-editor/project/${dirName}/scenes/${sceneId}/guides/${id}`
                : `/sonder-editor/project/${dirName}/scenes/${sceneId}/audio_tracks/${id}`;
        const method = type === "guide" ? "PATCH" : "PUT";

        try {
            await fetch(api.apiURL(endpoint), {
                method,
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(props),
            });
            if (refresh) {
                await this._fetchScenes();
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
        for (const item of targets) {
            item.data.muted = nextMuted;
            await this._updateItemProperty(item.type, item.id, { muted: nextMuted }, { refresh: false });
        }
        await this._fetchScenes();
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
        this._pushUndo("move guide");
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const sceneId = this.activeSceneId;
        const oldIdx = guideData.frame_index;

        try {
            await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}/guides/${oldIdx}`), {
                method: "DELETE",
            });
            await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}/guides`), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    frame_index: newIdx,
                    asset_id: guideData.asset_id,
                    source: guideData.source || "asset",
                    strength,
                    muted: !!guideData.muted,
                }),
            });
            await this._fetchScenes();
            this._clearSelection();
            this._hideItemEditor();
            this._renderTimeline();
        } catch (e) {
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
                font-family: 'Segoe UI', Arial, sans-serif;
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
        frameLabel.style.cssText = `flex-shrink:0;font-size:11px;color:${COLORS.guideSelected};font-family:monospace;`;
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

            const guideResp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${this.activeSceneId}/guides`), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    frame_index: this.playhead,
                    asset_id: asset.asset_id,
                    source: "asset",
                    strength: 1.0,
                }),
            });
            if (!guideResp.ok) {
                console.warn("[Sonder] Add guide failed:", await guideResp.text());
                return;
            }

            await Promise.all([this._fetchAssets(), this._fetchScenes()]);
        } catch (e) {
            console.warn("[Sonder] Add frame to guides failed:", e);
        }
    }

    async _deleteSelectedItems() {
        if (this.selectedItems.length === 0 || !this.activeScene || !this.projectDir) return;
        this.selectedItems = this.selectedItems.filter((item) => !this._isItemLocked(item));
        if (this.selectedItems.length === 0) return;
        this._pushUndo("delete items");
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const sceneId = this.activeSceneId;

        try {
            for (const item of this.selectedItems) {
                let endpoint;
                if (item.type === "clip") {
                    endpoint = `/sonder-editor/project/${dirName}/scenes/${sceneId}/clips/${item.id}`;
                } else if (item.type === "audio") {
                    endpoint = `/sonder-editor/project/${dirName}/scenes/${sceneId}/audio_tracks/${item.id}`;
                } else if (item.type === "guide") {
                    endpoint = `/sonder-editor/project/${dirName}/scenes/${sceneId}/guides/${item.id}`;
                } else if (item.type === "prompt") {
                    endpoint = `/sonder-editor/project/${dirName}/scenes/${sceneId}/prompt_sections/${item.id}`;
                }
                if (endpoint) {
                    await fetch(api.apiURL(endpoint), { method: "DELETE" });
                }
            }
            this._clearSelection();
            this._hideItemEditor();
            await this._fetchScenes();
            this._renderTimeline();
        } catch (e) {
            console.warn("[Sonder] Failed to delete items:", e);
        }
    }

    async _commitItemMove(frameDelta) {
        if (this.selectedItems.length === 0 || !this.activeScene || !this.projectDir) return;
        // Note: _pushUndo already called at drag start (mousedown)
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const sceneId = this.activeSceneId;

        return this._withTimelineMutationCommit("moveItem", async () => {
            try {
            const dragItemsOrig = this._dragItemsOrig || [];
            const draggedClipIds = new Set(dragItemsOrig.filter(o => o.type === "clip").map(o => o.id));
            const draggedAudioIds = new Set(dragItemsOrig.filter(o => o.type === "audio").map(o => o.id));
            const origClipLanes = this._origAllClipLanes || {};
            const origAudioLanes = this._origAllAudioLanes || {};
            const origClipStarts = this._origAllClipStarts || {};
            const origAudioStarts = this._origAllAudioStarts || {};

            // Persist the exact previewed state. A non-dragged item is committed when its
            // lane OR timeline_start changed — supports full position+lane swap from #35.
            for (const clip of (this.activeScene.clips || [])) {
                const clipId = clip.clip_id;
                const isDragged = draggedClipIds.has(clipId);
                const origLane = origClipLanes[clipId];
                const laneChanged = origLane !== undefined && (clip.track_index || 0) !== origLane;
                const origStart = origClipStarts[clipId];
                const startChanged = origStart !== undefined && (clip.timeline_start_frame || 0) !== origStart;
                if (!isDragged && !laneChanged && !startChanged) continue;
                const putBody = { track_index: clip.track_index || 0 };
                if (isDragged || startChanged) {
                    putBody.timeline_start_frame = clip.timeline_start_frame;
                    putBody.timeline_end_frame = clip.timeline_end_frame;
                }
                await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}/clips/${clipId}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(putBody),
                });
            }

            for (const track of (this.activeScene.audio_tracks || [])) {
                const trackId = track.track_id;
                const isDragged = draggedAudioIds.has(trackId);
                const origLane = origAudioLanes[trackId];
                const laneChanged = origLane !== undefined && (track.lane_index || 0) !== origLane;
                const origStart = origAudioStarts[trackId];
                const startChanged = origStart !== undefined && (track.timeline_start_frame || 0) !== origStart;
                if (!isDragged && !laneChanged && !startChanged) continue;
                const putBody = { lane_index: track.lane_index || 0 };
                if (isDragged || startChanged) {
                    putBody.timeline_start_frame = track.timeline_start_frame;
                    putBody.timeline_end_frame = track.timeline_end_frame;
                }
                await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}/audio_tracks/${trackId}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(putBody),
                });
            }

            for (const orig of dragItemsOrig) {
                const { type, id, data } = orig;
                if (type === "clip" || type === "audio") {
                    continue;
                }
                if (type === "guide") {
                    if (this._isGuideTrackLocked()) continue;
                    const oldIdx = orig.origStart;
                    const previewIdx = Number.isFinite(data._previewFrameIndex) ? data._previewFrameIndex : null;
                    const newIdx = previewIdx ?? Math.max(0, Math.min(this.totalFrames - 1, oldIdx + frameDelta));
                    // Move guide = delete old + create new
                    await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}/guides/${oldIdx}`), {
                        method: "DELETE",
                    });
                    await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}/guides`), {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            frame_index: newIdx,
                            asset_id: data.asset_id,
                            source: data.source || "asset",
                            strength: data.strength ?? 1.0,
                            muted: !!data.muted,
                        }),
                    });
                    delete data._previewFrameIndex;
                } else if (type === "prompt") {
                    if (this._isPromptTrackLocked()) continue;
                    await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}/prompt_sections/${id}`), {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            start_frame: data.start_frame,
                            end_frame: data.end_frame,
                        }),
                    });
                }
            }

            await this._fetchScenes({ ignoreMutationGate: true, reason: "moveItem_commit" });
            this._renderTimeline();
            } catch (e) {
            console.warn("[Sonder] Failed to move items:", e);
            await this._fetchScenes({ ignoreMutationGate: true, reason: "moveItem_error" });
            this._renderTimeline();
            }
        });
    }

    /** Commit a trim operation (edge drag) to the server. */
    async _commitTrim(trimInfo) {
        if (!this.projectDir || !this.activeScene) return;
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const sceneId = this.activeSceneId;
        const { type, id, data } = trimInfo;

        return this._withTimelineMutationCommit("trimEdge", async () => {
            try {
            if (type === "clip") {
                await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}/clips/${id}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        timeline_start_frame: data.timeline_start_frame,
                        timeline_end_frame: data.timeline_end_frame,
                        source_in_frame: data.source_in_frame || 0,
                        source_out_frame: data.source_out_frame,
                    }),
                });
            } else if (type === "audio") {
                await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}/audio_tracks/${id}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        timeline_start_frame: data.timeline_start_frame,
                        timeline_end_frame: data.timeline_end_frame,
                        source_in_frame: data.source_in_frame || 0,
                    }),
                });
            } else if (type === "prompt") {
                if (this._isPromptTrackLocked()) return;
                await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}/prompt_sections/${id}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        start_frame: data.start_frame,
                        end_frame: data.end_frame,
                    }),
                });
            }
            await this._fetchScenes({ ignoreMutationGate: true, reason: "trim_commit" });
            this._renderTimeline();
            } catch (e) {
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
                this._pushUndo("delete guide");
                const dirName = this._projectDirName();
                const sceneId = this.activeSceneId;
                try {
                    await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}/guides/${guide.frame_index}`), {
                        method: "DELETE",
                    });
                    await this._fetchScenes();
                    this._renderTimeline();
                    this._showGuideManagementPopup(x, y);
                } catch (e) {
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
                this._pushUndo("delete guide");
                const dirName = this._projectDirName();
                const sceneId = this.activeSceneId;
                try {
                    await fetch(api.apiURL(`/sonder-editor/project/${dirName}/scenes/${sceneId}/guides/${guide.frame_index}`), {
                        method: "DELETE",
                    });
                    await refreshPanel();
                } catch (e) {
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
            html += `<div style="display:flex;justify-content:space-between;gap:16px;padding:3px 0;"><span style="color:${lightenColor(COLORS.sceneBtnActive, 0.3)};min-width:120px;font-family:monospace;">${key}</span><span style="color:${COLORS.text};">${desc}</span></div>`;
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
        exitBtn.addEventListener("click", () => this._exitFullscreen());

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
            pointer-events: none; font-family: monospace;
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
            font-size: 11px; color: ${COLORS.text}; font-family: monospace;
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
        const guideLabelReserve = this._timelineGuideLabelReserve();

        // Timeline height — canvas renders at 1:1 now (individual elements scale themselves)
        this._timelineHeight = Math.max(100, bottomH - sceneBarH - toolbarH - editorsH - guideLabelReserve);
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
        exitPlaceholderBtn.addEventListener("click", () => this._exitFullscreen());
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
            this._exitFullscreen();
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
                if (this.isFullscreen) { this._exitFullscreen(); return true; }
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
        if (this._exportPanelEl || !this.activeScene || !this.projectDir) return;
        const rangeInfo = this._resolveQueueSelectionRange();
        if (!rangeInfo) return;

        const settings = this._exportSettings();
        const customState = this._defaultCustomExportOptions();
        let sourceMode = rangeInfo.hasSelection ? "selection" : "scene";
        const customDescriptions = {
            custom_container: "Output file wrapper. MP4 is safest for browser playback, MOV suits ProRes/PCM handoff, and MKV suits FFV1/FLAC archival exports.",
            custom_video_codec: "Video encoder. H.264 is most compatible, H.265 is smaller but slower, ProRes is editing-friendly, and FFV1 is lossless archival.",
            custom_pix_fmt: "Pixel format and chroma layout. yuv420p previews broadly, yuv444p keeps more chroma detail, yuv422p10le suits ProRes, and gbrp preserves RGB for FFV1.",
            custom_encoder_preset: "Speed/compression tradeoff for encoders that support presets. Slower presets usually make smaller files but take longer.",
            custom_audio_codec: "Audio stream codec. AAC previews broadly, PCM is editing-friendly in MOV, FLAC is lossless in MKV, and none omits audio.",
            custom_crf: "Quality target for CRF encoders. Lower is higher quality and larger; higher is smaller and more compressed.",
            custom_audio_bitrate_kbps: "AAC bitrate in kilobits per second when AAC audio is selected.",
        };
        const customOptionDescriptions = {
            custom_container: {
                mp4: "Browser-friendly container for H.264/H.265 and AAC.",
                mov: "Editing handoff container, especially for ProRes or PCM audio.",
                mkv: "Flexible archival container, useful for FFV1 and FLAC.",
            },
            custom_video_codec: {
                libx264: "H.264 encoder with the broadest preview and sharing compatibility.",
                libx265: "H.265 encoder for smaller files, with slower encoding and weaker compatibility.",
                prores_ks: "ProRes encoder for editing handoff; use MOV and yuv422p10le.",
                ffv1: "Lossless FFV1 encoder for archival or diagnostic files; use MKV and gbrp.",
            },
            custom_pix_fmt: {
                yuv420p: "Most compatible 8-bit 4:2:0 format for browser playback.",
                yuv444p: "8-bit 4:4:4 format with more chroma detail; preview support varies.",
                yuv422p10le: "10-bit 4:2:2 format used by ProRes handoff files.",
                gbrp: "Planar RGB format used for lossless FFV1 exports.",
            },
            custom_encoder_preset: Object.fromEntries(CUSTOM_ENCODER_PRESET_OPTIONS.map((value) => [
                value,
                `${value}: encoder speed/compression preset. Slower usually means smaller output and longer export time.`,
            ])),
            custom_audio_codec: {
                aac: "Compressed audio for MP4/browser playback.",
                pcm_s16le: "Uncompressed 16-bit PCM audio for editing handoff.",
                flac: "Lossless compressed audio, usually paired with MKV.",
                none: "Do not include an audio stream.",
            },
        };

        const backdrop = document.createElement("div");
        backdrop.style.cssText = `
            position: fixed; inset: 0; z-index: 10001;
            background: rgba(7,10,14,0.78);
            display: flex; align-items: center; justify-content: center;
            padding: 24px;
        `;

        const panel = document.createElement("div");
        panel.style.cssText = `${chromeOverlayPanelCss({ width: "min(620px, 96vw)", maxWidth: "620px", maxHeight: "min(760px, 88vh)", padding: "0", fontFamily: "'Segoe UI', Arial, sans-serif" })}`;
        panel.addEventListener("click", (event) => event.stopPropagation());

        const header = document.createElement("div");
        header.style.cssText = `display:flex;align-items:center;justify-content:space-between;gap:14px;padding:16px 20px 12px;border-bottom:1px solid ${COLORS.border};position:sticky;top:0;background:${COLORS.panel};z-index:1;`;
        const title = document.createElement("div");
        title.innerHTML = `<div style="font-size:15px;font-weight:700;color:#fff;">Export Timeline</div>`;
        const closeBtn = document.createElement("button");
        closeBtn.textContent = "Close";
        closeBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "6px 12px", fontSize: "11px", radius: "7px" });
        closeBtn.addEventListener("click", () => this._hideExportPanel());
        header.append(title, closeBtn);
        panel.appendChild(header);

        const body = document.createElement("div");
        body.style.cssText = "padding:16px 20px 18px;display:flex;flex-direction:column;gap:12px;";
        panel.appendChild(body);

        const makeRow = (labelText, description = "") => {
            const row = document.createElement("label");
            row.style.cssText = "display:grid;grid-template-columns:140px minmax(0,1fr);gap:12px;align-items:center;";
            if (description) row.title = description;
            const label = document.createElement("div");
            label.textContent = labelText;
            label.style.cssText = `font-size:11px;color:${COLORS.textMuted};font-weight:700;`;
            if (description) label.title = description;
            const control = document.createElement("div");
            control.style.cssText = "min-width:0;display:flex;align-items:center;gap:8px;flex-wrap:wrap;";
            if (description) control.title = description;
            row.append(label, control);
            body.appendChild(row);
            return control;
        };

        const sourceWrap = makeRow("Source", "Export either the entire active scene or the current In/Out selection.");
        const sourceRadioName = `${this._keyboardConsumerId("export-source")}`;
        const sceneRadio = document.createElement("input");
        sceneRadio.type = "radio";
        sceneRadio.name = sourceRadioName;
        sceneRadio.value = "scene";
        sceneRadio.title = "Use the full active scene duration.";
        const sceneLabel = document.createElement("span");
        sceneLabel.textContent = "Full scene";
        sceneLabel.style.cssText = "font-size:11px;color:#dbe3ea;";
        sceneLabel.title = sceneRadio.title;
        const selectionRadio = document.createElement("input");
        selectionRadio.type = "radio";
        selectionRadio.name = sourceRadioName;
        selectionRadio.value = "selection";
        selectionRadio.disabled = !rangeInfo.hasSelection;
        selectionRadio.title = rangeInfo.hasSelection ? "Use the current In/Out selection." : "Set an In/Out selection to export only part of the scene.";
        const selectionLabel = document.createElement("span");
        selectionLabel.textContent = "Active selection";
        selectionLabel.style.cssText = `font-size:11px;color:${rangeInfo.hasSelection ? "#dbe3ea" : COLORS.textMuted};`;
        selectionLabel.title = selectionRadio.title;
        sceneRadio.checked = sourceMode === "scene";
        selectionRadio.checked = sourceMode === "selection";
        sourceWrap.append(sceneRadio, sceneLabel, selectionRadio, selectionLabel);

        const prefixInput = document.createElement("input");
        prefixInput.type = "text";
        prefixInput.value = settings.filenamePrefix || "";
        prefixInput.placeholder = "export";
        prefixInput.maxLength = 64;
        prefixInput.style.cssText = `${chromeInputCss({ fontSize: "11px", padding: "6px 8px", textAlign: "left" })} width:100%;`;
        prefixInput.title = "Filename stem used before the scene name and timestamp.";
        makeRow("Filename prefix", prefixInput.title).appendChild(prefixInput);

        const presetSelect = document.createElement("select");
        presetSelect.style.cssText = `${chromeInputCss({ fontSize: "11px", padding: "6px 8px", textAlign: "left" })} min-width:190px;`;
        for (const option of SAVE_PRESET_OPTIONS) {
            const opt = document.createElement("option");
            opt.value = option.value;
            opt.textContent = option.label;
            opt.title = option.description || "";
            presetSelect.appendChild(opt);
        }
        presetSelect.value = SAVE_PRESET_OPTIONS.some((option) => option.value === settings.lastPreset)
            ? settings.lastPreset
            : DEFAULT_SAVE_PRESET;
        const presetWrap = makeRow("Save preset", "Choose the output container, codecs, pixel format, and compatibility target.");
        presetWrap.style.flexDirection = "column";
        presetWrap.style.alignItems = "stretch";
        presetWrap.appendChild(presetSelect);
        const presetHelp = document.createElement("div");
        presetHelp.style.cssText = `font-size:10px;color:${COLORS.textMuted};line-height:1.35;`;
        presetWrap.appendChild(presetHelp);
        const syncPresetDescription = () => {
            const option = SAVE_PRESET_OPTIONS.find((entry) => entry.value === presetSelect.value);
            const description = option?.description || "Custom allowlisted encode settings.";
            presetHelp.textContent = description;
            presetHelp.title = description;
            presetSelect.title = description;
        };

        const customPanel = document.createElement("div");
        customPanel.style.cssText = `display:none;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;padding:10px;border:1px solid ${COLORS.borderSoft};border-radius:8px;background:${COLORS.panelMuted};`;
        body.appendChild(customPanel);

        const customControls = [];
        const makeCustomSelect = (key, labelText, options) => {
            const wrap = document.createElement("label");
            wrap.style.cssText = "display:flex;flex-direction:column;gap:4px;min-width:0;";
            wrap.title = customDescriptions[key] || "";
            const label = document.createElement("span");
            label.textContent = labelText;
            label.style.cssText = `font-size:10px;color:${COLORS.textMuted};`;
            label.title = wrap.title;
            const select = document.createElement("select");
            select.style.cssText = chromeInputCss({ fontSize: "10px", padding: "5px 7px", textAlign: "left" });
            for (const value of options) {
                const opt = document.createElement("option");
                opt.value = value;
                opt.textContent = value;
                opt.title = customOptionDescriptions[key]?.[value] || wrap.title;
                select.appendChild(opt);
            }
            select.value = customState[key];
            const syncTitle = () => {
                select.title = customOptionDescriptions[key]?.[select.value] || wrap.title;
            };
            select.addEventListener("change", () => {
                customState[key] = select.value;
                syncTitle();
            });
            wrap.append(label, select);
            customPanel.appendChild(wrap);
            customControls.push(select);
            syncTitle();
            return select;
        };
        const makeCustomNumber = (key, labelText, min, max) => {
            const wrap = document.createElement("label");
            wrap.style.cssText = "display:flex;flex-direction:column;gap:4px;min-width:0;";
            wrap.title = customDescriptions[key] || "";
            const label = document.createElement("span");
            label.textContent = labelText;
            label.style.cssText = `font-size:10px;color:${COLORS.textMuted};`;
            label.title = wrap.title;
            const input = document.createElement("input");
            input.type = "number";
            input.min = String(min);
            input.max = String(max);
            input.step = "1";
            input.value = String(customState[key]);
            input.style.cssText = chromeInputCss({ fontSize: "10px", padding: "5px 7px", textAlign: "right" });
            input.title = wrap.title;
            input.addEventListener("change", () => {
                const numeric = Number(input.value);
                customState[key] = Number.isFinite(numeric) ? Math.max(min, Math.min(max, Math.round(numeric))) : customState[key];
                input.value = String(customState[key]);
            });
            wrap.append(label, input);
            customPanel.appendChild(wrap);
            customControls.push(input);
            return input;
        };
        makeCustomSelect("custom_container", "Container", CUSTOM_CONTAINER_OPTIONS);
        makeCustomSelect("custom_video_codec", "Video codec", CUSTOM_VIDEO_CODEC_OPTIONS);
        makeCustomSelect("custom_pix_fmt", "Pixel format", CUSTOM_PIX_FMT_OPTIONS);
        makeCustomSelect("custom_encoder_preset", "Encoder preset", CUSTOM_ENCODER_PRESET_OPTIONS);
        makeCustomSelect("custom_audio_codec", "Audio codec", CUSTOM_AUDIO_CODEC_OPTIONS);
        makeCustomNumber("custom_crf", "CRF", 0, 51);
        makeCustomNumber("custom_audio_bitrate_kbps", "AAC kbps", 1, 10000);

        const makeCheckboxRow = (labelText, checked, description = "") => {
            const input = document.createElement("input");
            input.type = "checkbox";
            input.checked = !!checked;
            input.title = description;
            makeRow(labelText, description).appendChild(input);
            return input;
        };
        const includeVideo = makeCheckboxRow("Include video", settings.includeVideo !== false, "Encode the visible timeline video layers into the export.");
        const includeAudio = makeCheckboxRow("Include audio", settings.includeAudio !== false && this._sceneHasAudio(), "Mix unmuted, visible audio lanes into the export when the scene has audio.");
        const placeAsTake = makeCheckboxRow("Place as take", settings.placeAsTake !== false, "After export, place the video result onto a fresh timeline lane in the active scene.");

        const errorEl = document.createElement("div");
        errorEl.style.cssText = `min-height:16px;font-size:11px;color:${COLORS.warningText};`;
        body.appendChild(errorEl);
        const progressEl = document.createElement("div");
        progressEl.style.cssText = `display:none;font-size:11px;color:${COLORS.textMuted};padding:8px 10px;border:1px solid ${COLORS.borderSoft};border-radius:8px;background:${COLORS.panelMuted};`;
        body.appendChild(progressEl);

        const footer = document.createElement("div");
        footer.style.cssText = `display:flex;justify-content:flex-end;gap:8px;padding:12px 20px 16px;border-top:1px solid ${COLORS.border};`;
        const cancelBtn = document.createElement("button");
        cancelBtn.textContent = "Cancel";
        cancelBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "6px 12px", fontSize: "11px", radius: "7px" });
        cancelBtn.onclick = () => this._hideExportPanel();
        const exportBtn = document.createElement("button");
        exportBtn.textContent = "Export";
        exportBtn.style.cssText = chromeButtonCss({ variant: "primary", padding: "6px 12px", fontSize: "11px", radius: "7px" });
        footer.append(cancelBtn, exportBtn);
        panel.appendChild(footer);

        const controls = [sceneRadio, selectionRadio, prefixInput, presetSelect, includeVideo, includeAudio, placeAsTake, ...customControls];
        const syncState = () => {
            syncPresetDescription();
            customPanel.style.display = presetSelect.value === "Custom" ? "grid" : "none";
            includeAudio.disabled = !this._sceneHasAudio();
            if (!this._sceneHasAudio()) includeAudio.checked = false;
            placeAsTake.disabled = !includeVideo.checked;
            if (!includeVideo.checked) placeAsTake.checked = false;
            const valid = includeVideo.checked || includeAudio.checked;
            errorEl.textContent = valid ? "" : "Enable video or audio to export";
            exportBtn.disabled = !valid;
            setButtonVariant(exportBtn, valid ? "primary" : "muted");
        };
        sceneRadio.addEventListener("change", () => { if (sceneRadio.checked) sourceMode = "scene"; });
        selectionRadio.addEventListener("change", () => { if (selectionRadio.checked) sourceMode = "selection"; });
        presetSelect.addEventListener("change", syncState);
        includeVideo.addEventListener("change", syncState);
        includeAudio.addEventListener("change", syncState);
        syncState();

        exportBtn.addEventListener("click", async () => {
            const sceneDuration = Math.max(0, parseInt(this.activeScene?.duration_frames, 10) || 0);
            const useSelection = sourceMode === "selection" && rangeInfo.hasSelection;
            const start = useSelection ? rangeInfo.selStart : 0;
            const end = useSelection ? rangeInfo.selEnd : sceneDuration;
            const customOptions = presetSelect.value === "Custom"
                ? { ...customState, custom_output_kind: CUSTOM_OUTPUT_KIND_VIDEO }
                : null;
            this._updateExportSettings({
                lastPreset: presetSelect.value,
                lastCustomEncode: customOptions,
                filenamePrefix: prefixInput.value,
                includeVideo: includeVideo.checked,
                includeAudio: includeAudio.checked,
                placeAsTake: placeAsTake.checked,
            });
            controls.forEach((control) => { control.disabled = true; });
            exportBtn.disabled = true;
            closeBtn.disabled = true;
            progressEl.style.display = "";
            progressEl.textContent = "Starting export...";
            errorEl.textContent = "";
            cancelBtn.textContent = "Cancel Export";
            cancelBtn.onclick = () => this._cancelTimelineExport(progressEl);
            await this._startTimelineExport({
                scene_id: this.activeScene.scene_id,
                range: { start, end },
                filename_prefix: prefixInput.value,
                save_preset: presetSelect.value,
                custom_options: customOptions,
                include_video: includeVideo.checked,
                include_audio: includeAudio.checked,
                place_as_take: placeAsTake.checked,
            }, { errorEl, progressEl, controls, exportBtn, closeBtn, cancelBtn, syncState });
        });

        backdrop.appendChild(panel);
        backdrop.addEventListener("click", () => {
            if (this._exportJobId) {
                progressEl.textContent = "Export running. Use Cancel Export to stop it.";
                return;
            }
            this._hideExportPanel();
        });
        document.body.appendChild(backdrop);
        this._exportPanelEl = backdrop;
        this._exportPanelKeyOff = registerKeyboardConsumer({
            id: this._keyboardConsumerId("export"),
            priority: KEY_PRIORITY.OVERLAY,
            keydown: (event) => {
                if (event.key !== "Escape") return false;
                if (this._exportJobId) {
                    progressEl.textContent = "Export running. Use Cancel Export to stop it.";
                    return true;
                }
                this._hideExportPanel();
                return true;
            },
        });
    }

    _hideExportPanel() {
        const jobId = this._exportJobId;
        if (jobId && this._projectDirName()) {
            const dirName = encodeURIComponent(this._projectDirName());
            fetch(api.apiURL(`/sonder-editor/project/${dirName}/render_timeline/${jobId}/cancel`), {
                method: "POST",
            }).catch((error) => console.warn("[Sonder] Export cancel failed:", error));
        }
        if (this._exportPollTimer) {
            window.clearTimeout(this._exportPollTimer);
            this._exportPollTimer = null;
        }
        this._exportJobId = "";
        if (this._exportPanelEl) {
            this._exportPanelEl.remove();
            this._exportPanelEl = null;
        }
        if (this._exportPanelKeyOff) {
            this._exportPanelKeyOff();
            this._exportPanelKeyOff = null;
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
            this._exportJobId = data.job_id || "";
            ui.progressEl.textContent = this._exportPhaseMessage(data);
            this._pollTimelineExport(ui);
        } catch (error) {
            ui.errorEl.textContent = error?.message || "Export failed.";
            ui.controls.forEach((control) => { control.disabled = false; });
            ui.exportBtn.disabled = false;
            ui.closeBtn.disabled = false;
            ui.cancelBtn.textContent = "Cancel";
            ui.cancelBtn.onclick = () => this._hideExportPanel();
            ui.syncState();
        }
    }

    _pollTimelineExport(ui) {
        if (!this._exportJobId) return;
        this._exportPollTimer = window.setTimeout(async () => {
            try {
                const dirName = encodeURIComponent(this._projectDirName());
                const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/render_timeline/${this._exportJobId}`));
                if (!resp.ok) {
                    const message = await this._readExportError(resp, `Export status failed: ${resp.status}`);
                    throw new Error(message);
                }
                const data = await resp.json();
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
                    ui.progressEl.style.display = "none";
                    ui.errorEl.textContent = "";
                    ui.controls.forEach((control) => { control.disabled = false; });
                    ui.closeBtn.disabled = false;
                    ui.cancelBtn.textContent = "Cancel";
                    ui.cancelBtn.onclick = () => this._hideExportPanel();
                    ui.syncState();
                    return;
                }
                this._pollTimelineExport(ui);
            } catch (error) {
                this._exportJobId = "";
                ui.errorEl.textContent = error?.message || "Export failed.";
                ui.controls.forEach((control) => { control.disabled = false; });
                ui.closeBtn.disabled = false;
                ui.cancelBtn.textContent = "Cancel";
                ui.cancelBtn.onclick = () => this._hideExportPanel();
                ui.syncState();
            }
        }, 650);
    }

    async _cancelTimelineExport(progressEl) {
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

    _buildBatchQueueRanges(selStart, selEnd, chunkSize) {
        const start = Math.max(0, parseInt(selStart, 10) || 0);
        const end = Math.max(start, parseInt(selEnd, 10) || 0);
        const size = Math.max(1, parseInt(chunkSize, 10) || 1);
        if (end <= start) {
            return [];
        }

        const ranges = [];
        let cursor = start;
        while (cursor < end) {
            const nextEnd = Math.min(cursor + size, end);
            ranges.push({ start: cursor, end: nextEnd });
            cursor = nextEnd;
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
        const chunkSize = resolveBatchChunkSize({
            settings: this._settings,
            template: this._getActiveTemplate(),
        });
        const chunks = this._buildBatchQueueRanges(range.selStart, range.selEnd, chunkSize);
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

        try {
            const dirName = encodeURIComponent(this._projectDirName());
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/queue`), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(snapshot),
            });
            if (resp.ok) {
                this._flashQueueButton(this._queueBtn);
                await this._fetchRenderQueue();
            }
        } catch (e) { console.error("Add to queue failed:", e); }
    }

    async _addBatchToRenderQueue() {
        const range = this._resolveQueueSelectionRange();
        if (!range) return;

        const chunkSize = resolveBatchChunkSize({
            settings: this._settings,
            template: this._getActiveTemplate(),
        });
        const chunks = this._buildBatchQueueRanges(range.selStart, range.selEnd, chunkSize);
        if (chunks.length <= 1) {
            await this._addToRenderQueue();
            return;
        }

        const dirName = encodeURIComponent(this._projectDirName());
        const batchId = globalThis.crypto?.randomUUID?.()
            || `${Date.now().toString(36)}-${Math.random().toString(16).slice(2, 10)}`;

        try {
            for (let index = 0; index < chunks.length; index += 1) {
                const chunk = chunks[index];
                const snapshot = this._buildQueueSnapshot(chunk.start, chunk.end);
                if (!snapshot) {
                    throw new Error("Failed to build batch queue snapshot.");
                }

                const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/queue`), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        ...snapshot,
                        batch_id: batchId,
                        batch_total: chunks.length,
                        batch_index: index,
                    }),
                });

                if (!resp.ok) {
                    const message = await this._readQueueError(resp, `Add batch chunk failed: ${resp.status}`);
                    await this._fetchRenderQueue();
                    alert(message);
                    return;
                }
            }

            this._flashQueueButton(this._batchQueueBtn);
            await this._fetchRenderQueue();
        } catch (e) {
            console.error("Add batch to queue failed:", e);
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
        try {
            const dirName = encodeURIComponent(this._projectDirName());
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}/queue`), { method: "DELETE" });
            if (!resp.ok) {
                const message = await this._readQueueError(resp, `Clear completed renders failed: ${resp.status}`);
                throw new Error(message);
            }
            await this._fetchRenderQueue();
        } catch (e) {
            console.error("Clear completed renders failed:", e);
            this._showToast(e?.message || "Clear completed renders failed.");
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

    _createRenderQueueActiveControl() {
        const row = document.createElement("label");
        row.style.cssText = `
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
            padding: 7px 8px;
            border-bottom: 1px solid ${COLORS.borderSoft};
            color: ${COLORS.textMuted};
            font-size: 10px;
            cursor: pointer;
            user-select: none;
        `;
        row.title = "Toggle whether queued jobs drive editor execution";

        const label = document.createElement("span");
        label.textContent = "Queue Active";
        label.style.cssText = `font-weight: 700; color: ${COLORS.text};`;

        const right = document.createElement("span");
        right.style.cssText = `display: flex; align-items: center; gap: 6px;`;

        const status = document.createElement("span");
        status.textContent = this.renderQueueActive === false ? "Off" : "On";
        status.style.cssText = `color: ${COLORS.textDim}; font-size: 10px;`;

        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = this.renderQueueActive !== false;
        checkbox.title = row.title;
        checkbox.style.cssText = `margin: 0;`;
        checkbox.addEventListener("click", (event) => event.stopPropagation());
        checkbox.addEventListener("change", () => this._setRenderQueueActive(checkbox.checked));

        right.append(status, checkbox);
        row.append(label, right);
        return row;
    }

    _groupRenderQueueJobs(queue) {
        const groups = [];
        let index = 0;
        while (index < queue.length) {
            const job = queue[index];
            const batchId = String(job?.batch_id || "");
            if (!batchId) {
                groups.push({ type: "single", job });
                index += 1;
                continue;
            }

            const jobs = [job];
            index += 1;
            while (index < queue.length && String(queue[index]?.batch_id || "") === batchId) {
                jobs.push(queue[index]);
                index += 1;
            }

            if (jobs.length === 1) {
                groups.push({ type: "single", job });
                continue;
            }
            groups.push({ type: "batch", batchId, jobs });
        }
        return groups;
    }

    _formatQueueStatusLabel(status) {
        const raw = String(status || "pending").trim().toLowerCase();
        if (!raw) return "Pending";
        return raw.charAt(0).toUpperCase() + raw.slice(1);
    }

    _formatQueueSelectionSummary(job) {
        const start = Math.max(0, parseInt(job?.selection_start, 10) || 0);
        const end = Math.max(start, parseInt(job?.selection_end, 10) || 0);
        const duration = end - start;
        const preContext = Math.max(0, parseInt(job?.pre_context_frames, 10) || 0);
        const postContext = Math.max(0, parseInt(job?.post_context_frames, 10) || 0);
        const maskPre = Math.max(0, parseInt(job?.mask_pre_offset, 10) || 0);
        const maskPost = Math.max(0, parseInt(job?.mask_post_offset, 10) || 0);
        return `In: ${this._frameToTimecode(start)} Out: ${this._frameToTimecode(end)} (${this._frameToTimecode(duration)}) | Ctx: -${preContext}/+${postContext} | Mask Offset: -${maskPre}/+${maskPost}`;
    }

    _createQueueRow(job, { title = "", nested = false } = {}) {
        const item = document.createElement("div");
        item.style.cssText = `
            padding: 6px 8px${nested ? " 6px 18px" : ""};
            border-bottom: 1px solid ${COLORS.borderSoft};
            display: grid;
            grid-template-columns: auto minmax(0, 1fr) auto;
            gap: 8px;
            font-size: 10px;
            color: ${COLORS.text};
            background: ${nested ? COLORS.panel : COLORS.panelMuted};
            align-items: start;
        `;

        const badge = document.createElement("span");
        const colors = {
            pending: COLORS.textMuted,
            running: lightenColor(COLORS.sceneBtnActive, 0.15),
            completed: "#68a376",
            failed: "#c66d76",
        };
        badge.style.cssText = `
            width: 8px;
            height: 8px;
            margin-top: 4px;
            border-radius: 50%;
            background: ${colors[job.status] || "#888"};
            flex-shrink: 0;
        `;
        badge.title = job.status;
        item.appendChild(badge);

        const content = document.createElement("div");
        content.style.cssText = "min-width:0;display:flex;flex-direction:column;gap:2px;";
        if (job.prompt) content.title = job.prompt;

        const headingRow = document.createElement("div");
        headingRow.style.cssText = "display:flex;align-items:baseline;justify-content:space-between;gap:8px;min-width:0;";

        const heading = document.createElement("div");
        heading.style.cssText = `
            color: ${COLORS.text};
            font-size: 11px;
            font-weight: 600;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            min-width: 0;
        `;
        heading.textContent = title || job.scene_name || "Scene";

        const statusLabel = document.createElement("div");
        statusLabel.style.cssText = `
            color: ${COLORS.textMuted};
            font-size: 10px;
            flex-shrink: 0;
            white-space: nowrap;
        `;
        statusLabel.textContent = this._formatQueueStatusLabel(job?.status);

        const selectionSummary = document.createElement("div");
        selectionSummary.style.cssText = `
            color: ${COLORS.textDim};
            font-size: 10px;
            line-height: 1.35;
            white-space: normal;
            overflow-wrap: anywhere;
        `;
        selectionSummary.textContent = this._formatQueueSelectionSummary(job);

        headingRow.append(heading, statusLabel);
        content.append(headingRow, selectionSummary);
        item.appendChild(content);

        const delBtn = document.createElement("span");
        delBtn.textContent = "x";
        delBtn.style.cssText = `color: ${COLORS.textMuted}; cursor: pointer; padding: 0 2px; font-size: 9px;`;
        delBtn.addEventListener("click", async () => {
            try {
                const dirName = encodeURIComponent(this._projectDirName());
                await fetch(api.apiURL(`/sonder-editor/project/${dirName}/queue/${job.job_id}`), { method: "DELETE" });
                await this._fetchRenderQueue();
            } catch (e) {
                console.error("Delete queue job failed:", e);
            }
        });
        item.appendChild(delBtn);

        return item;
    }

    _renderQueueBatchGroup(entry) {
        const batchTotal = Math.max(
            entry.jobs.length,
            ...entry.jobs.map((job) => Math.max(0, parseInt(job.batch_total, 10) || 0)),
        );
        const shortId = entry.batchId.slice(0, 8);
        const isOpen = this._queueBatchExpanded[entry.batchId] !== false;

        const group = document.createElement("div");
        group.style.cssText = `border-bottom: 1px solid ${COLORS.borderSoft}; background: ${COLORS.panelMuted};`;

        const header = document.createElement("button");
        header.type = "button";
        header.style.cssText = `
            width: 100%;
            padding: 6px 8px;
            background: ${COLORS.panelMuted};
            border: none;
            border-bottom: 1px solid ${COLORS.borderSoft};
            color: ${COLORS.text};
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
            cursor: pointer;
            font-size: 10px;
            font-weight: 700;
            text-align: left;
        `;

        const countLabel = entry.jobs.length === batchTotal
            ? `${batchTotal} chunk${batchTotal === 1 ? "" : "s"}`
            : `${entry.jobs.length} of ${batchTotal} chunks`;

        const headerLabel = document.createElement("span");
        headerLabel.textContent = `${isOpen ? "v" : ">"} Batch ${shortId} - ${countLabel}`;

        const headerScene = document.createElement("span");
        headerScene.style.cssText = `color:${COLORS.textMuted};font-weight:600;`;
        headerScene.textContent = entry.jobs[0]?.scene_name || "Scene";

        header.append(headerLabel, headerScene);
        header.addEventListener("click", () => {
            this._setQueueBatchExpanded(entry.batchId, !isOpen, { persist: true });
        });
        group.appendChild(header);

        if (isOpen) {
            const nested = document.createElement("div");
            nested.style.cssText = "display:flex;flex-direction:column;";
            entry.jobs.forEach((job, index) => {
                const chunkIndex = Math.max(1, (parseInt(job.batch_index, 10) || index) + 1);
                nested.appendChild(this._createQueueRow(job, {
                    title: `Chunk ${chunkIndex} of ${batchTotal}`,
                    nested: true,
                }));
            });
            group.appendChild(nested);
        }

        return group;
    }

    _renderQueuePanel() {
        if (!this._queueContainer) return;
        this._queueContainer.innerHTML = "";
        this._updateQueueHeaderLabel();
        this._updateQueueChromeStatus();

        const queue = this._renderQueue || [];
        this._queueContainer.appendChild(this._createRenderQueueActiveControl());
        if (queue.length === 0) {
            const emptyEl = document.createElement("div");
            emptyEl.style.cssText = `padding: 10px; color: ${COLORS.textMuted}; font-style: italic; font-size: 10px;`;
            emptyEl.textContent = "Queue empty - use + Queue or + Batch to add jobs";
            this._queueContainer.appendChild(emptyEl);
            return;
        }

        const completedCount = queue.filter((job) => String(job?.status || "").toLowerCase() === "completed").length;
        if (completedCount > 0) {
            const actions = document.createElement("div");
            actions.style.cssText = `
                display: flex;
                justify-content: flex-end;
                align-items: center;
                gap: 6px;
                padding: 6px 8px;
                border-bottom: 1px solid ${COLORS.borderSoft};
                background: ${COLORS.panelMuted};
            `;
            const clearBtn = document.createElement("button");
            clearBtn.type = "button";
            clearBtn.textContent = "Clear Completed Renders";
            clearBtn.title = `Remove ${completedCount} completed render${completedCount === 1 ? "" : "s"} from the queue`;
            clearBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "4px 8px", fontSize: "10px", radius: "6px" });
            clearBtn.addEventListener("click", async (event) => {
                event.preventDefault();
                event.stopPropagation();
                clearBtn.disabled = true;
                clearBtn.style.opacity = "0.65";
                await this._clearCompletedRenderQueue();
            });
            actions.appendChild(clearBtn);
            this._queueContainer.appendChild(actions);
        }

        const groups = this._groupRenderQueueJobs(queue);
        for (const entry of groups) {
            if (entry.type === "single") {
                this._queueContainer.appendChild(this._createQueueRow(entry.job));
                continue;
            }
            this._queueContainer.appendChild(this._renderQueueBatchGroup(entry));
        }
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
        if (!wanted.size || wanted.has("project")) {
            this._fetchProjectSettings();
        }
        const wantsAssets = !wanted.size || wanted.has("assets");
        const wantsScenes = !wanted.size || wanted.has("scenes");
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
        if (!wanted.size || wanted.has("queue")) {
            this._fetchRenderQueue();
        }
    }

    async _fetchProjectSettings() {
        if (!this.projectDir) return;
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
        try {
            const resp = await fetch(api.apiURL(`/sonder-editor/project/${dirName}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ frame_constraint: expected }),
            });
            if (!resp.ok) {
                throw new Error(`Frame-constraint self-heal failed: ${resp.status}`);
            }
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
        ctx.fillStyle = "#120c09";
        ctx.fillRect(0, 0, w, h);
        ctx.strokeStyle = "rgba(255,177,140,0.35)";
        ctx.lineWidth = 2;
        ctx.strokeRect(10, 10, Math.max(0, w - 20), Math.max(0, h - 20));
        ctx.fillStyle = "#ffb18c";
        ctx.font = `${Math.max(16, h / 14)}px sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(title || "Missing asset", w / 2, h / 2 - 12);
        if (subtitle) {
            ctx.fillStyle = "rgba(255,220,204,0.8)";
            ctx.font = `${Math.max(11, h / 24)}px sans-serif`;
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
            ctx.fillStyle = "#000";
            ctx.fillRect(0, 0, w, h);
            ctx.fillStyle = "rgba(255,255,255,0.15)";
            ctx.font = `${Math.max(16, h / 12)}px monospace`;
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
                ctx.fillStyle = "#000";
                ctx.fillRect(0, 0, w, h);
                ctx.fillStyle = "rgba(255,255,255,0.3)";
                ctx.font = "14px sans-serif";
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
            ctx.fillStyle = "#000";
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
        ctx.fillStyle = "#000";
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

        ctx.fillStyle = "#000";
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

        ctx.fillStyle = "#000";
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
        if (this._settingsPanelEl) return;

        const backdrop = document.createElement("div");
        backdrop.style.cssText = `
            position: fixed; inset: 0; z-index: 10001;
            background: rgba(7,10,14,0.78);
            display: flex; align-items: center; justify-content: center;
            padding: 24px;
        `;

        const panel = document.createElement("div");
        panel.style.cssText = `${chromeOverlayPanelCss({ width: "min(860px, 96vw)", maxWidth: "860px", maxHeight: "min(780px, 88vh)", padding: "0", fontFamily: "'Segoe UI', Arial, sans-serif" })}`;

        const header = document.createElement("div");
        header.style.cssText = `
            display: flex; align-items: center; justify-content: space-between;
            padding: 18px 22px 14px;
            border-bottom: 1px solid ${COLORS.border};
            position: sticky; top: 0; background: ${COLORS.panel}; z-index: 1;
        `;
        const titleWrap = document.createElement("div");
        titleWrap.innerHTML = `
            <div style="font-size:15px;font-weight:700;color:#fff;">Editor Settings</div>
            <div style="font-size:11px;color:#909090;margin-top:3px;">Local browser preferences for layout, playback, appearance, project defaults, and gallery behavior.</div>
        `;
        const closeBtn = document.createElement("button");
        closeBtn.textContent = "Close";
        closeBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "6px 12px", fontSize: "11px", radius: "7px" });
        closeBtn.addEventListener("click", () => this._hideSettingsPanel());
        header.append(titleWrap, closeBtn);
        panel.appendChild(header);

        const body = document.createElement("div");
        body.style.cssText = `
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 14px;
            padding: 18px 22px 22px;
        `;
        panel.appendChild(body);

        this._settingsPanelControls = {};
        const controls = this._settingsPanelControls;
        controls._syncPresetNumberControls = [];

        const updateCategory = (category, key, value) => {
            this._updateSettings({
                [category]: {
                    [key]: value,
                },
            });
        };

        const createSection = (title, description) => {
            const section = document.createElement("section");
            section.style.cssText = `
                background: ${COLORS.panelRaised};
                border: 1px solid ${COLORS.border};
                border-radius: 10px;
                padding: 14px 14px 12px;
                display: flex;
                flex-direction: column;
                gap: 8px;
                align-self: start;
            `;
            const heading = document.createElement("div");
            heading.innerHTML = `
                <div style="font-size:12px;font-weight:700;color:#f0f0f0;letter-spacing:0.02em;">${title}</div>
                <div style="font-size:10px;color:#888;margin-top:2px;line-height:1.35;">${description}</div>
            `;
            section.appendChild(heading);
            body.appendChild(section);
            return section;
        };

        const createRow = (section, label, description) => {
            const row = document.createElement("div");
            row.style.cssText = `
                display: flex; align-items: center; justify-content: space-between;
                gap: 12px; padding: 8px 0; border-top: 1px solid ${COLORS.borderSoft};
            `;
            const labelWrap = document.createElement("div");
            labelWrap.style.cssText = "flex: 1; min-width: 0;";
            labelWrap.innerHTML = `
                <div style="color:#ddd;font-size:11px;line-height:1.35;">${label}</div>
                <div style="color:#777;font-size:10px;line-height:1.35;margin-top:1px;">${description}</div>
            `;
            const controlWrap = document.createElement("div");
            controlWrap.style.cssText = "display:flex;align-items:center;gap:8px;flex-shrink:0;";
            row.append(labelWrap, controlWrap);
            section.appendChild(row);
            return controlWrap;
        };

        const createCheckbox = (section, key, label, description, getter, onChange) => {
            const controlWrap = createRow(section, label, description);
            const checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.checked = !!getter();
            checkbox.addEventListener("change", () => onChange(checkbox.checked));
            controlWrap.appendChild(checkbox);
            controls[key] = checkbox;
            return checkbox;
        };

        const createSelect = (section, key, label, description, options, getter, onChange) => {
            const controlWrap = createRow(section, label, description);
            const select = document.createElement("select");
            select.style.cssText = `${chromeInputCss({ fontSize: "11px", padding: "4px 8px", textAlign: "left" })} min-width: 126px;`;
            for (const option of options) {
                const el = document.createElement("option");
                el.value = option.value;
                el.textContent = option.label;
                if (option.description) el.title = option.description;
                select.appendChild(el);
            }
            const syncTitle = () => {
                const selected = options.find((option) => option.value === select.value);
                select.title = selected?.description || description || "";
            };
            select.value = getter();
            syncTitle();
            select._sonderSyncTitle = syncTitle;
            select.addEventListener("change", () => {
                syncTitle();
                onChange(select.value);
            });
            controlWrap.appendChild(select);
            controls[key] = select;
            return select;
        };

        const createNumberInput = (section, key, label, description, config) => {
            const controlWrap = createRow(section, label, description);
            const input = document.createElement("input");
            input.type = "number";
            input.min = String(config.min);
            input.max = String(config.max);
            input.step = String(config.step ?? 1);
            input.value = String(config.getter());
            input.style.cssText = `${chromeInputCss({ width: "86px", fontSize: "11px", padding: "4px 8px", textAlign: "right" })}`;
            input.addEventListener("change", () => {
                const numeric = Number(input.value);
                const fallback = config.getter();
                const nextValue = Number.isFinite(numeric) ? numeric : fallback;
                config.onChange(nextValue);
            });
            controlWrap.appendChild(input);
            controls[key] = input;
            return input;
        };

        const createPresetNumberInput = (section, key, label, description, config) => {
            const controlWrap = createRow(section, label, description);
            controlWrap.style.flexDirection = "column";
            controlWrap.style.alignItems = "flex-end";
            const inputRow = document.createElement("div");
            inputRow.style.cssText = "display:flex;align-items:center;gap:8px;";

            const select = document.createElement("select");
            select.style.cssText = `${chromeInputCss({ fontSize: "11px", padding: "4px 8px", textAlign: "left" })} min-width: 126px;`;
            for (const option of config.options) {
                const el = document.createElement("option");
                el.value = option.value;
                el.textContent = option.label;
                select.appendChild(el);
            }
            const customOption = document.createElement("option");
            customOption.value = "custom";
            customOption.textContent = "Custom";
            select.appendChild(customOption);

            const input = document.createElement("input");
            input.type = "number";
            input.min = String(config.min);
            input.max = String(config.max);
            input.step = String(config.step ?? 1);
            input.style.cssText = `${chromeInputCss({ width: "92px", fontSize: "11px", padding: "4px 8px", textAlign: "right" })}`;

            const coerce = (value) => {
                if (value === null && config.allowNull) return null;
                const numeric = Number(value);
                if (!Number.isFinite(numeric)) return undefined;
                const clamped = Math.max(config.min, Math.min(config.max, numeric));
                return config.integer ? Math.round(clamped) : clamped;
            };
            const formatValue = (value) => {
                if (value === null || value === undefined) return String(config.customDefault ?? config.min);
                return config.integer ? String(Math.round(value)) : String(value);
            };
            const presetValueFor = (value) => {
                if (value === null && config.allowNull) return "unlimited";
                const normalized = coerce(value);
                if (normalized === undefined) return "custom";
                const preset = config.options.find((option) => {
                    if (option.value === "unlimited") return false;
                    const optionValue = coerce(option.value);
                    return optionValue !== undefined && optionValue === normalized;
                });
                return preset ? preset.value : "custom";
            };
            const currentValue = () => {
                const value = config.getter();
                if (value === null && config.allowNull) return null;
                const normalized = coerce(value);
                return normalized === undefined ? coerce(config.customDefault) : normalized;
            };
            const sync = () => {
                const value = currentValue();
                const selectedPreset = presetValueFor(value);
                select.value = selectedPreset;
                const showCustom = selectedPreset === "custom";
                input.style.display = showCustom ? "" : "none";
                input.value = formatValue(value === null ? coerce(config.customDefault) : value);
            };

            select.addEventListener("change", () => {
                if (select.value === "custom") {
                    config.onChange(currentValue() ?? coerce(config.customDefault));
                    sync();
                    return;
                }
                if (select.value === "unlimited") {
                    config.onChange(null);
                    sync();
                    return;
                }
                const nextValue = coerce(select.value);
                if (nextValue !== undefined) {
                    config.onChange(nextValue);
                }
                sync();
            });
            input.addEventListener("change", () => {
                const nextValue = coerce(input.value);
                config.onChange(nextValue === undefined ? (currentValue() ?? coerce(config.customDefault)) : nextValue);
                sync();
            });

            inputRow.append(select, input);
            controlWrap.appendChild(inputRow);
            controls[`${key}Preset`] = select;
            controls[`${key}Custom`] = input;
            controls._syncPresetNumberControls.push(sync);
            sync();
            return { select, input };
        };

        const createScaleRow = (section, key, label, description, getter) => {
            const controlWrap = createRow(section, label, description);
            const downBtn = document.createElement("button");
            downBtn.textContent = "-";
            downBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "3px 9px", fontSize: "13px", radius: "6px", lineHeight: "1" });
            downBtn.addEventListener("click", () => this._setScale(key, getter() - 0.1));

            const valueLabel = document.createElement("span");
            valueLabel.style.cssText = "font-size:11px;color:#cfcfcf;min-width:42px;text-align:center;";
            valueLabel.textContent = `${Math.round(getter() * 100)}%`;
            controls[`scale${key}Label`] = valueLabel;

            const upBtn = document.createElement("button");
            upBtn.textContent = "+";
            upBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "3px 9px", fontSize: "13px", radius: "6px", lineHeight: "1" });
            upBtn.addEventListener("click", () => this._setScale(key, getter() + 0.1));

            controlWrap.append(downBtn, valueLabel, upBtn);
        };

        const setSelectOptions = (select, options, selectedValue) => {
            if (!select) return;
            select.innerHTML = "";
            for (const option of options) {
                const el = document.createElement("option");
                el.value = option.value;
                el.textContent = option.label;
                select.appendChild(el);
            }
            select.value = Array.from(select.options).some((option) => option.value === String(selectedValue))
                ? String(selectedValue)
                : (options[0]?.value ?? "");
        };

        const summarizeConstraint = (constraint, key) => {
            if (!constraint || typeof constraint !== "object") return `${key}: Any`;
            const formula = describeConstraintFormula(constraint);
            if (constraint.min != null || constraint.max != null) {
                const min = constraint.min != null ? constraint.min : "-";
                const max = constraint.max != null ? constraint.max : "-";
                return `${key}: ${formula} [${min}..${max}]`;
            }
            return `${key}: ${formula}`;
        };

        const parseDraftNumber = (value, integer = true) => {
            if (value === "" || value == null) return undefined;
            const numeric = Number(value);
            if (!Number.isFinite(numeric)) return undefined;
            return integer ? Math.round(numeric) : numeric;
        };

        const buildConstraintDraft = (paramKey, fields) => {
            const integer = paramKey !== "fps";
            const constraint = {};
            const step = parseDraftNumber(fields.step.value, integer);
            const offset = parseDraftNumber(fields.offset.value, integer);
            const min = parseDraftNumber(fields.min.value, integer);
            const max = parseDraftNumber(fields.max.value, integer);
            if (step != null && step > 0) constraint.step = step;
            if (offset != null) constraint.offset = offset;
            if (min != null) constraint.min = min;
            if (max != null) constraint.max = max;
            if (constraint.min != null && constraint.max != null && constraint.max < constraint.min) {
                constraint.max = constraint.min;
            }
            return Object.keys(constraint).length ? constraint : null;
        };

        const makeUniqueTemplateId = (name, existingId = "") => {
            const base = String(name || "")
                .trim()
                .toLowerCase()
                .replace(/[^a-z0-9]+/g, "-")
                .replace(/^-+|-+$/g, "") || "custom-template";
            const usedIds = new Set(getAllModelTemplates(this._settings).map((template) => template.id).filter((id) => id !== existingId));
            let candidate = base;
            let suffix = 2;
            while (usedIds.has(candidate)) {
                candidate = `${base}-${suffix}`;
                suffix += 1;
            }
            return candidate;
        };

        const templateOptions = () => getAllModelTemplates(this._settings).map((template) => ({
            value: template.id,
            label: template.name,
        }));

        const modelTemplatesSection = createSection(
            "Model Templates",
            "Manage model-specific parameter constraints and choose which template new projects should start with."
        );
        const templateList = document.createElement("div");
        templateList.style.cssText = "display:flex;flex-direction:column;gap:8px;";
        const templateFormHost = document.createElement("div");
        templateFormHost.style.cssText = "display:flex;flex-direction:column;gap:8px;";
        modelTemplatesSection.append(templateList, templateFormHost);

        this._renderModelTemplateSettings = () => {
            const formState = this._templateFormState || { expanded: false, editId: "" };
            const templates = getAllModelTemplates(this._settings);
            const customTemplates = this._settings.modelTemplates.customTemplates || [];
            templateList.innerHTML = "";
            templateFormHost.innerHTML = "";

            if (controls.defaultTemplateId) {
                setSelectOptions(controls.defaultTemplateId, templateOptions(), this._settings.projectDefaults.defaultTemplateId || "free");
            }

            for (const template of templates) {
                const card = document.createElement("div");
                card.style.cssText = `
                    border: 1px solid ${COLORS.borderSoft};
                    border-radius: 8px;
                    padding: 10px 11px;
                    display: flex;
                    flex-direction: column;
                    gap: 7px;
                    background: ${COLORS.panelMuted};
                `;

                const head = document.createElement("div");
                head.style.cssText = "display:flex;align-items:center;justify-content:space-between;gap:10px;";
                const nameWrap = document.createElement("div");
                const badge = template.builtIn ? "Built-in" : "Custom";
                nameWrap.innerHTML = `
                    <div style="font-size:11px;font-weight:700;color:${COLORS.text};">${template.name}</div>
                    <div style="font-size:10px;color:${COLORS.textMuted};">${badge}${template.id === this._templateId ? " • Active Project Template" : ""}</div>
                `;
                head.appendChild(nameWrap);

                if (!template.builtIn) {
                    const actions = document.createElement("div");
                    actions.style.cssText = "display:flex;gap:6px;";

                    const editBtn = document.createElement("button");
                    editBtn.textContent = "Edit";
                    editBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "4px 9px", fontSize: "10px", radius: "6px" });
                    editBtn.addEventListener("click", () => {
                        this._templateFormState = { expanded: true, editId: template.id };
                        this._renderModelTemplateSettings?.();
                    });

                    const deleteBtn = document.createElement("button");
                    deleteBtn.textContent = "Delete";
                    deleteBtn.style.cssText = chromeButtonCss({ variant: "danger", padding: "4px 9px", fontSize: "10px", radius: "6px" });
                    deleteBtn.addEventListener("click", async () => {
                        const nextCustomTemplates = customTemplates.filter((entry) => entry.id !== template.id);
                        const nextDefaultTemplateId = this._settings.projectDefaults.defaultTemplateId === template.id ? "free" : this._settings.projectDefaults.defaultTemplateId;
                        this._templateFormState = { expanded: false, editId: "" };
                        this._updateSettings({
                            modelTemplates: { customTemplates: nextCustomTemplates },
                            projectDefaults: { defaultTemplateId: nextDefaultTemplateId },
                        });
                        if (this._templateId === template.id) {
                            await this._updateProjectTemplateId("free");
                            this._templateId = "free";
                            this._rebuildTemplateOptions();
                            this._rebuildResolutionTierOptions();
                            this._applyTemplateConstraintMetadata();
                            this._syncSceneResolutionControls({ detectSelections: false });
                            this._updateViewportHeader();
                        }
                    });

                    actions.append(editBtn, deleteBtn);
                    head.appendChild(actions);
                }

                const summary = document.createElement("div");
                summary.style.cssText = "font-size:10px;color:#9ca9b5;line-height:1.45;";
                const summaryParts = MODEL_TEMPLATE_PARAM_KEYS
                    .map((key) => summarizeConstraint(template.constraints?.[key], key));
                if ((template.constraints?.batchMaxFrames || 0) > 0) {
                    summaryParts.push(`batch: ${template.constraints.batchMaxFrames}`);
                }
                summary.textContent = summaryParts.join(" | ");

                card.append(head, summary);
                templateList.appendChild(card);
            }

            const formToggle = document.createElement("button");
            formToggle.textContent = formState.expanded ? "Cancel Template Edit" : "Add Custom Template";
            formToggle.style.cssText = chromeButtonCss({ variant: "subtle", padding: "5px 12px", fontSize: "11px", radius: "7px" });
            formToggle.addEventListener("click", () => {
                this._templateFormState = formState.expanded
                    ? { expanded: false, editId: "" }
                    : { expanded: true, editId: "" };
                this._renderModelTemplateSettings?.();
            });
            templateFormHost.appendChild(formToggle);

            if (!formState.expanded) {
                return;
            }

            const editingTemplate = customTemplates.find((template) => template.id === formState.editId) || null;
            const form = document.createElement("div");
            form.style.cssText = `
                border: 1px solid ${COLORS.border};
                border-radius: 8px;
                padding: 12px;
                display: flex;
                flex-direction: column;
                gap: 10px;
                background: ${COLORS.panelMuted};
            `;

            const title = document.createElement("div");
            title.style.cssText = "font-size:11px;font-weight:700;color:#eef3f8;";
            title.textContent = editingTemplate ? `Edit ${editingTemplate.name}` : "Add Custom Template";
            form.appendChild(title);

            const nameRow = document.createElement("div");
            nameRow.style.cssText = "display:flex;align-items:center;gap:8px;";
            const nameLabel = document.createElement("div");
            nameLabel.style.cssText = "font-size:10px;color:#92a0ad;min-width:90px;";
            nameLabel.textContent = "Template Name";
            const nameInput = document.createElement("input");
            nameInput.type = "text";
            nameInput.value = editingTemplate?.name || "";
            nameInput.placeholder = "My Model";
            nameInput.style.cssText = `${chromeInputCss({ fontSize: "11px", padding: "5px 8px", textAlign: "left" })} flex:1;`;
            nameRow.append(nameLabel, nameInput);
            form.appendChild(nameRow);

            const constraintInputs = {};
            const previewRows = [];
            for (const paramKey of MODEL_TEMPLATE_PARAM_KEYS) {
                const existingConstraint = editingTemplate?.constraints?.[paramKey] || {};
                const row = document.createElement("div");
                row.style.cssText = `
                    display:grid;
                    grid-template-columns: 84px repeat(4, minmax(0, 1fr));
                    gap: 6px;
                    align-items: center;
                `;
                const header = document.createElement("div");
                header.style.cssText = "font-size:10px;color:#d9e1e8;font-weight:700;text-transform:capitalize;";
                header.textContent = paramKey;
                row.appendChild(header);
                const fields = {};
                for (const fieldKey of ["step", "offset", "min", "max"]) {
                    const input = document.createElement("input");
                    input.type = "number";
                    input.step = paramKey === "fps" ? "0.001" : "1";
                    input.value = existingConstraint[fieldKey] ?? "";
                    input.placeholder = fieldKey;
                    input.style.cssText = chromeInputCss({ fontSize: "10px", padding: "4px 6px", textAlign: "right" });
                    fields[fieldKey] = input;
                    row.appendChild(input);
                }
                constraintInputs[paramKey] = fields;
                form.appendChild(row);

                const preview = document.createElement("div");
                preview.style.cssText = "margin-left:84px;font-size:10px;color:#8f9cab;line-height:1.4;";
                form.appendChild(preview);
                previewRows.push({ key: paramKey, el: preview });
            }

            const refreshConstraintPreviews = () => {
                for (const preview of previewRows) {
                    const constraint = buildConstraintDraft(preview.key, constraintInputs[preview.key]);
                    const values = previewConstraintValues(constraint, 5);
                    const formula = describeConstraintFormula(constraint);
                    preview.el.textContent = values.length
                        ? `Formula: ${formula} | Sample: ${values.join(", ")}`
                        : `Formula: ${formula}`;
                }
            };

            for (const paramKey of MODEL_TEMPLATE_PARAM_KEYS) {
                for (const field of Object.values(constraintInputs[paramKey])) {
                    field.addEventListener("input", refreshConstraintPreviews);
                }
            }
            refreshConstraintPreviews();

            const batchRow = document.createElement("div");
            batchRow.style.cssText = "display:flex;align-items:center;gap:8px;";
            const batchLabel = document.createElement("div");
            batchLabel.style.cssText = "font-size:10px;color:#92a0ad;min-width:90px;";
            batchLabel.textContent = "Batch Max Frames";
            const batchInput = document.createElement("input");
            batchInput.type = "number";
            batchInput.min = "1";
            batchInput.step = "1";
            batchInput.value = editingTemplate?.constraints?.batchMaxFrames ?? "";
            batchInput.placeholder = "97";
            batchInput.style.cssText = `${chromeInputCss({ width: "120px", fontSize: "10px", padding: "4px 6px", textAlign: "right" })}`;
            batchRow.append(batchLabel, batchInput);
            form.appendChild(batchRow);

            const batchHelp = document.createElement("div");
            batchHelp.style.cssText = "margin-left:90px;font-size:10px;color:#8f9cab;line-height:1.4;";
            batchHelp.textContent = "Default batch chunk ceiling for this template. The active frame constraint still snaps/clamps the final chunk size.";
            form.appendChild(batchHelp);

            const actionRow = document.createElement("div");
            actionRow.style.cssText = "display:flex;justify-content:flex-end;gap:8px;margin-top:4px;";
            const saveBtn = document.createElement("button");
            saveBtn.textContent = editingTemplate ? "Save Template" : "Create Template";
            saveBtn.style.cssText = chromeButtonCss({ variant: "primary", padding: "6px 12px", fontSize: "11px", radius: "7px" });
            saveBtn.addEventListener("click", () => {
                const name = String(nameInput.value || "").trim();
                if (!name) {
                    alert("Template name is required.");
                    return;
                }
                const nextTemplate = {
                    id: editingTemplate?.id || makeUniqueTemplateId(name),
                    name,
                    constraints: {},
                };
                for (const paramKey of MODEL_TEMPLATE_PARAM_KEYS) {
                    const constraint = buildConstraintDraft(paramKey, constraintInputs[paramKey]);
                    if (constraint) {
                        nextTemplate.constraints[paramKey] = constraint;
                    }
                }
                const batchMaxFrames = Number(batchInput.value);
                if (Number.isFinite(batchMaxFrames) && batchMaxFrames > 0) {
                    nextTemplate.constraints.batchMaxFrames = Math.round(batchMaxFrames);
                }
                const nextCustomTemplates = editingTemplate
                    ? customTemplates.map((template) => template.id === editingTemplate.id ? nextTemplate : template)
                    : [...customTemplates, nextTemplate];
                this._templateFormState = { expanded: false, editId: "" };
                this._updateSettings({
                    modelTemplates: { customTemplates: nextCustomTemplates },
                });
            });

            const cancelBtn = document.createElement("button");
            cancelBtn.textContent = "Cancel";
            cancelBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "6px 12px", fontSize: "11px", radius: "7px" });
            cancelBtn.addEventListener("click", () => {
                this._templateFormState = { expanded: false, editId: "" };
                this._renderModelTemplateSettings?.();
            });

            actionRow.append(cancelBtn, saveBtn);
            form.appendChild(actionRow);
            templateFormHost.appendChild(form);
        };

        const layoutSection = createSection(
            "Layout & UI Scale",
            "Adjust the editor chrome and restore saved fullscreen sizing."
        );
        createScaleRow(layoutSection, "Toolbar", "Toolbar & Bars", "Scene strip, toolbar, and transport controls.", () => this._settings.layout.scaleToolbar);
        createScaleRow(layoutSection, "TrackHeaders", "Track Headers", "Lane labels, icons, and left-side track controls.", () => this._settings.layout.scaleTrackHeaders);
        createScaleRow(layoutSection, "Timeline", "Timeline", "Ruler, track heights, clip blocks, and inline editors.", () => this._settings.layout.scaleTimeline);
        createScaleRow(layoutSection, "Gallery", "Asset Gallery", "Gallery lists, tabs, metadata text, and inspector chrome.", () => this._settings.layout.scaleGallery);
        const resetControls = createRow(layoutSection, "Reset Editor Layout", "Clear saved fullscreen widths, label widths, and UI scale overrides.");
        const resetBtn = document.createElement("button");
        resetBtn.textContent = "Reset";
        resetBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "5px 12px", fontSize: "11px", radius: "6px" });
        resetBtn.addEventListener("click", () => this._resetEditorLayout());
        resetControls.appendChild(resetBtn);

        const timelineSection = createSection(
            "Timeline Behavior",
            "Default snapping behavior, candidate types, and frame/timecode display mode."
        );
        createCheckbox(
            timelineSection,
            "snappingEnabled",
            "Enable Snapping By Default",
            "Use snap guides for drag, trim, and move operations unless toggled off.",
            () => this._settings.timelineBehavior.snappingEnabled,
            (checked) => this._setSnappingEnabled(checked)
        );
        createNumberInput(
            timelineSection,
            "snapThreshold",
            "Snap Threshold",
            "Maximum distance, in frames, before the editor snaps to a candidate.",
            {
                min: 1,
                max: 60,
                step: 1,
                getter: () => this._settings.timelineBehavior.snapThreshold,
                onChange: (value) => updateCategory("timelineBehavior", "snapThreshold", Math.round(value)),
            }
        );
        for (const option of SNAP_TARGET_OPTIONS) {
            createCheckbox(
                timelineSection,
                `snapTarget_${option.key}`,
                option.label,
                "Include this target type when evaluating snap candidates.",
                () => this._settings.timelineBehavior.snapTargets[option.key],
                (checked) => {
                    this._updateSettings({
                        timelineBehavior: {
                            snapTargets: {
                                [option.key]: checked,
                            },
                        },
                    });
                }
            );
        }
        createSelect(
            timelineSection,
            "timecodeMode",
            "Default Time Display",
            "Choose whether editor inputs and readouts default to frame counts or timecode.",
            TIMECODE_MODE_OPTIONS,
            () => this._settings.timelineBehavior.timecodeMode,
            (value) => this._setTimecodeMode(value)
        );

        const playbackSection = createSection(
            "Playback",
            "Controls for timeline playback flow and viewport preview quality."
        );
        createCheckbox(
            playbackSection,
            "loopSelection",
            "Loop Active Selection",
            "If a selection exists, playback repeats within that range.",
            () => this._settings.playback.loopSelection,
            (checked) => updateCategory("playback", "loopSelection", checked)
        );
        createCheckbox(
            playbackSection,
            "autoScrollPlayhead",
            "Auto-Scroll Playhead",
            "Keep the playhead visible as playback or keyboard scrubbing moves forward.",
            () => this._settings.playback.autoScrollPlayhead,
            (checked) => updateCategory("playback", "autoScrollPlayhead", checked)
        );
        createCheckbox(
            playbackSection,
            "returnToPlaybackStart",
            "Return To Start On Stop",
            "Stopping playback jumps back to the frame where playback began.",
            () => this._settings.playback.returnToPlaybackStart,
            (checked) => updateCategory("playback", "returnToPlaybackStart", checked)
        );
        createSelect(
            playbackSection,
            "playbackResolution",
            "Viewport Playback Resolution",
            "Lower preview resolution for smoother playback on heavier scenes.",
            PLAYBACK_RESOLUTION_OPTIONS,
            () => this._settings.playback.resolution,
            (value) => updateCategory("playback", "resolution", value)
        );
        createCheckbox(
            playbackSection,
            "prebufferEnabled",
            "Prebuffer Upcoming Clips",
            "Warm the next viewport video before playback reaches the clip boundary.",
            () => this._settings.playback.prebufferEnabled,
            (checked) => updateCategory("playback", "prebufferEnabled", checked)
        );
        createNumberInput(
            playbackSection,
            "prebufferLookaheadMs",
            "Prebuffer Lookahead",
            "Milliseconds ahead of playback to preroll the next viewport video.",
            {
                min: 100,
                max: 5000,
                step: 100,
                getter: () => this._settings.playback.prebufferLookaheadMs,
                onChange: (value) => updateCategory("playback", "prebufferLookaheadMs", Math.round(value)),
            }
        );

        const renderSection = createSection(
            "Render",
            "Take placement, cache retention, trash cleanup, and batch render defaults."
        );
        createSelect(
            renderSection,
            "takePlacementMode",
            "Take Placement Mode",
            "Trimmed places only the generated portion on the timeline. Untrimmed includes pre/post context frames for seam inspection.",
            TAKE_PLACEMENT_MODE_OPTIONS,
            () => this._settings.render?.takePlacementMode ?? "trimmed",
            (value) => updateCategory("render", "takePlacementMode", value)
        );
        createSelect(
            renderSection,
            "defaultSavePreset",
            "Default Save Preset",
            "Applied to newly inserted Sonder Save Video nodes.",
            SAVE_PRESET_OPTIONS,
            () => this._settings.render?.defaultSavePreset ?? DEFAULT_SAVE_PRESET,
            (value) => updateCategory("render", "defaultSavePreset", value)
        );
        createPresetNumberInput(
            renderSection,
            "maxRenderCacheEntries",
            "Render Cache Entries",
            "Maximum cached render tensors to retain for this project. Older entries are evicted when the editor opens or this cap changes.",
            {
                options: RENDER_CACHE_ENTRY_PRESETS,
                min: 1,
                max: 100000,
                step: 1,
                integer: true,
                allowNull: true,
                customDefault: DEFAULT_EDITOR_SETTINGS.render.maxRenderCacheEntries,
                getter: () => this._settings.render?.maxRenderCacheEntries === null
                    ? null
                    : (this._settings.render?.maxRenderCacheEntries ?? DEFAULT_EDITOR_SETTINGS.render.maxRenderCacheEntries),
                onChange: (value) => updateCategory("render", "maxRenderCacheEntries", value),
            }
        );
        createNumberInput(
            renderSection,
            "trashRetentionDays",
            "Trash Retention Days",
            "Hard-delete trashed assets after this many days during asset refresh.",
            {
                min: 0,
                max: 36500,
                step: 1,
                getter: () => this._trashRetentionDays(),
                onChange: (value) => updateCategory("render", "trashRetentionDays", Math.max(0, Math.round(value))),
            }
        );
        createPresetNumberInput(
            renderSection,
            "trashMaxSizeMB",
            "Trash Max Size",
            "Optional decimal-MB cap for trashed asset source files. Oldest trash is purged first.",
            {
                options: TRASH_SIZE_MB_PRESETS,
                min: 0,
                max: 100000000,
                step: 0.1,
                integer: false,
                allowNull: true,
                customDefault: 250,
                getter: () => this._settings.render?.trashMaxSizeMB ?? null,
                onChange: (value) => updateCategory("render", "trashMaxSizeMB", value),
            }
        );
        createNumberInput(
            renderSection,
            "batchRenderMaxFramesPerChunk",
            "Batch Max Frames",
            "Preferred chunk size before the active template's frame constraint snaps and clamps it. A value of 0 uses the active template's batch ceiling.",
            {
                min: 0,
                max: 10000,
                step: 1,
                getter: () => this._settings.batchRender.maxFramesPerChunk,
                onChange: (value) => updateCategory("batchRender", "maxFramesPerChunk", Math.max(0, Math.round(value))),
            }
        );

        const guidesSection = createSection(
            "Guides",
            "Guide capture defaults for clip-frame extraction."
        );
        createNumberInput(
            guidesSection,
            "guideSnapshotMaxLongEdge",
            "Snapshot Max Long Edge",
            "0 keeps the automatic source/scene long-edge rule. Values above 0 cap captured guide PNGs.",
            {
                min: 0,
                max: 8192,
                step: 64,
                getter: () => this._settings.guides?.guideSnapshotMaxLongEdge ?? 0,
                onChange: (value) => updateCategory("guides", "guideSnapshotMaxLongEdge", Math.max(0, Math.round(value))),
            }
        );
        createCheckbox(
            guidesSection,
            "hoverPreviewEnabled",
            "Guide Hover Preview",
            "Show a larger guide thumbnail when hovering guide markers or guide rows.",
            () => this._settings.guides?.hoverPreviewEnabled ?? true,
            (checked) => {
                updateCategory("guides", "hoverPreviewEnabled", checked);
                if (!checked) this._hideGuideHoverPreview();
            }
        );
        createNumberInput(
            guidesSection,
            "hoverPreviewSize",
            "Hover Preview Size",
            "Maximum preview edge in pixels.",
            {
                min: 96,
                max: 360,
                step: 12,
                getter: () => this._guideHoverPreviewSize(),
                onChange: (value) => updateCategory("guides", "hoverPreviewSize", Math.max(96, Math.min(360, Math.round(value)))),
            }
        );

        const appearanceSection = createSection(
            "Appearance",
            "Non-destructive visual preferences for timeline readability."
        );
        const waveformControls = createRow(appearanceSection, "Waveform Accent Color", "Tint used for the audio waveform overlay.");
        const waveformInput = document.createElement("input");
        waveformInput.type = "color";
        waveformInput.value = this._settings.appearance.waveformAccent;
        waveformInput.style.cssText = "width:44px;height:28px;padding:0;border:none;background:none;cursor:pointer;";
        waveformInput.addEventListener("change", () => updateCategory("appearance", "waveformAccent", waveformInput.value));
        waveformControls.appendChild(waveformInput);
        controls.waveformAccent = waveformInput;

        const brightnessControls = createRow(appearanceSection, "Timeline Background Brightness", "Lighten or darken timeline chrome without changing lane colors.");
        const brightnessInput = document.createElement("input");
        brightnessInput.type = "range";
        brightnessInput.min = "70";
        brightnessInput.max = "130";
        brightnessInput.step = "1";
        brightnessInput.value = String(this._settings.appearance.timelineBrightness);
        brightnessInput.style.width = "120px";
        brightnessInput.addEventListener("input", () => {
            updateCategory("appearance", "timelineBrightness", Math.round(Number(brightnessInput.value) || 100));
        });
        const brightnessLabel = document.createElement("span");
        brightnessLabel.style.cssText = `font-size:11px;color:${COLORS.text};min-width:38px;text-align:right;`;
        brightnessLabel.textContent = `${this._settings.appearance.timelineBrightness}%`;
        brightnessControls.append(brightnessInput, brightnessLabel);
        controls.timelineBrightness = brightnessInput;
        controls.timelineBrightnessLabel = brightnessLabel;

        createSelect(
            appearanceSection,
            "clipLabelMode",
            "Clip Label Content",
            "Choose how much text appears inside clip and audio blocks.",
            CLIP_LABEL_MODE_OPTIONS,
            () => this._settings.appearance.clipLabelMode,
            (value) => updateCategory("appearance", "clipLabelMode", value)
        );

        const laneTintSpecs = [
            { key: "video", label: "Video Lane Tint", description: "Optional subtle color overlay on all video lane backgrounds." },
            { key: "audio", label: "Audio Lane Tint", description: "Optional subtle color overlay on all audio lane backgrounds." },
            { key: "motion_driver", label: "Driver Lane Tint", description: "Optional subtle color overlay on all motion-driver lane backgrounds." },
        ];
        for (const spec of laneTintSpecs) {
            const row = createRow(appearanceSection, spec.label, spec.description);
            const input = document.createElement("input");
            input.type = "color";
            input.style.cssText = "width:44px;height:28px;padding:0;border:none;background:none;cursor:pointer;";
            const current = this._settings.appearance.laneTintOverrides?.[spec.key] || "";
            const displayHex = current || "#000000";
            input.value = displayHex;
            input.dataset.active = current ? "1" : "0";
            const resetBtn = document.createElement("button");
            resetBtn.type = "button";
            resetBtn.textContent = "Reset";
            resetBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "3px 8px", fontSize: "10px", radius: "5px" });
            const applyTint = (hex) => {
                updateCategory("appearance", "laneTintOverrides", {
                    ...(this._settings.appearance.laneTintOverrides || {}),
                    [spec.key]: hex,
                });
            };
            input.addEventListener("change", () => {
                input.dataset.active = "1";
                applyTint(input.value);
            });
            resetBtn.addEventListener("click", () => {
                input.value = "#000000";
                input.dataset.active = "0";
                applyTint("");
            });
            row.append(input, resetBtn);
            controls[`laneTintOverride_${spec.key}`] = input;
        }

        const projectDefaultsSection = createSection(
            "Project Defaults",
            "Values used to prefill new projects and new blank scenes."
        );
        createNumberInput(
            projectDefaultsSection,
            "defaultProjectFps",
            "Default FPS",
            "Used to prefill create-project widgets in SonderEditor.",
            {
                min: 1,
                max: 240,
                step: 1,
                getter: () => this._settings.projectDefaults.fps,
                onChange: (value) => updateCategory("projectDefaults", "fps", value),
            }
        );
        createNumberInput(
            projectDefaultsSection,
            "defaultProjectWidth",
            "Default Project Width",
            "Used as the starting width for newly created projects.",
            {
                min: 64,
                max: 8192,
                step: 8,
                getter: () => this._settings.projectDefaults.width,
                onChange: (value) => updateCategory("projectDefaults", "width", Math.round(value)),
            }
        );
        createNumberInput(
            projectDefaultsSection,
            "defaultProjectHeight",
            "Default Project Height",
            "Used as the starting height for newly created projects.",
            {
                min: 64,
                max: 8192,
                step: 8,
                getter: () => this._settings.projectDefaults.height,
                onChange: (value) => updateCategory("projectDefaults", "height", Math.round(value)),
            }
        );
        createNumberInput(
            projectDefaultsSection,
            "defaultSceneDuration",
            "Default New-Scene Duration",
            "Applies only when creating a new blank scene; duplicated scenes keep their source duration.",
            {
                min: 1,
                max: 99999,
                step: 1,
                getter: () => this._settings.projectDefaults.newSceneDuration,
                onChange: (value) => updateCategory("projectDefaults", "newSceneDuration", Math.round(value)),
            }
        );
        createNumberInput(
            projectDefaultsSection,
            "defaultGuideStrength",
            "Default Guide Strength",
            "Applied to new guide frames created from image drops.",
            {
                min: 0,
                max: 1,
                step: 0.05,
                getter: () => this._settings.projectDefaults.defaultGuideStrength,
                onChange: (value) => updateCategory("projectDefaults", "defaultGuideStrength", value),
            }
        );
        createNumberInput(
            projectDefaultsSection,
            "defaultMotionDriverStrength",
            "Default Motion-Driver Strength",
            "Applied when creating motion-driver clips.",
            {
                min: 0,
                max: 1,
                step: 0.05,
                getter: () => this._settings.projectDefaults.defaultMotionDriverStrength,
                onChange: (value) => updateCategory("projectDefaults", "defaultMotionDriverStrength", value),
            }
        );
        createSelect(
            projectDefaultsSection,
            "defaultTemplateId",
            "Default Template",
            "Used when creating new projects from the editor node.",
            templateOptions(),
            () => this._settings.projectDefaults.defaultTemplateId || "free",
            (value) => updateCategory("projectDefaults", "defaultTemplateId", value)
        );
        body.appendChild(modelTemplatesSection);
        this._renderModelTemplateSettings();

        const gallerySection = createSection(
            "Asset Gallery",
            "Global defaults for gallery ordering and inspector presentation."
        );
        createSelect(
            gallerySection,
            "gallerySortMode",
            "Default Sort Mode",
            "New gallery instances use this order unless changed later.",
            GALLERY_SORT_OPTIONS,
            () => this._settings.gallery.sortMode,
            (value) => updateCategory("gallery", "sortMode", value)
        );
        createCheckbox(
            gallerySection,
            "galleryInspectorCollapsed",
            "Inspector Starts Collapsed",
            "Open the shared asset gallery with the right-side inspector hidden.",
            () => this._settings.gallery.inspectorCollapsed,
            (checked) => updateCategory("gallery", "inspectorCollapsed", checked)
        );
        createSelect(
            gallerySection,
            "galleryThumbnailSize",
            "Thumbnail Size",
            "Controls the default media preview size in gallery lists and trash.",
            GALLERY_THUMBNAIL_SIZE_OPTIONS,
            () => this._settings.gallery.thumbnailSize,
            (value) => updateCategory("gallery", "thumbnailSize", value)
        );
        createCheckbox(
            gallerySection,
            "galleryArtifactInspectorExpanded",
            "Artifact Inspector Expanded",
            "Show the artifact metadata inspector in its expanded state by default.",
            () => this._settings.gallery.artifactInspectorExpanded,
            (checked) => updateCategory("gallery", "artifactInspectorExpanded", checked)
        );

        backdrop.appendChild(panel);
        document.body.appendChild(backdrop);
        this._settingsPanelEl = backdrop;
        this._syncSettingsPanelControls();

        backdrop.addEventListener("click", (e) => {
            if (e.target === backdrop) this._hideSettingsPanel();
        });
        this._settingsPanelKeyOff = registerKeyboardConsumer({
            id: this._keyboardConsumerId("settings"),
            priority: KEY_PRIORITY.OVERLAY,
            keydown: (e) => {
                if (e.key === "Escape") { this._hideSettingsPanel(); return true; }
                return false;
            },
        });
    }

    _hideSettingsPanel() {
        if (this._settingsPanelEl) {
            this._settingsPanelEl.remove();
            this._settingsPanelEl = null;
            this._settingsPanelControls = null;
            this._renderModelTemplateSettings = null;
        }
        if (this._settingsPanelKeyOff) {
            this._settingsPanelKeyOff();
            this._settingsPanelKeyOff = null;
        }
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
        if (this._settingsPanelKeyOff) { this._settingsPanelKeyOff(); this._settingsPanelKeyOff = null; }
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
        if (this._settingsPanelEl) {
            this._settingsPanelEl.remove();
            this._settingsPanelEl = null;
        }
        if (this._exportPanelEl || this._exportJobId || this._exportPollTimer) {
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
