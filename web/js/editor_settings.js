const SETTINGS_STORAGE_KEY = "ltx-editor-settings";
const SETTINGS_VERSION = 1;

export const GALLERY_SORT_OPTIONS = [
    { value: "newest", label: "Newest" },
    { value: "oldest", label: "Oldest" },
    { value: "name", label: "Name" },
    { value: "type", label: "Type" },
    { value: "duration", label: "Duration" },
    { value: "resolution", label: "Resolution" },
];

export const GALLERY_THUMBNAIL_SIZE_OPTIONS = [
    { value: "small", label: "Small" },
    { value: "medium", label: "Medium" },
    { value: "large", label: "Large" },
];

export const PLAYBACK_RESOLUTION_OPTIONS = [
    { value: "full", label: "Full" },
    { value: "half", label: "Half" },
    { value: "quarter", label: "Quarter" },
];

export const CLIP_LABEL_MODE_OPTIONS = [
    { value: "name_duration", label: "Name + Duration" },
    { value: "name_only", label: "Name Only" },
    { value: "hidden", label: "Hidden" },
];

export const TIMECODE_MODE_OPTIONS = [
    { value: "frames", label: "Frames" },
    { value: "timecode", label: "Timecode" },
];

export const SNAP_TARGET_OPTIONS = [
    { key: "playhead", label: "Playhead" },
    { key: "selection", label: "Selection Edges" },
    { key: "clipEdges", label: "Video Clip Edges" },
    { key: "audioEdges", label: "Audio Clip Edges" },
    { key: "guides", label: "Guide Frames" },
    { key: "promptSections", label: "Prompt Sections" },
    { key: "sceneBounds", label: "Scene Bounds" },
];

export const DEFAULT_EDITOR_SETTINGS = {
    version: SETTINGS_VERSION,
    meta: {
        legacyGalleryPrefsMigrated: false,
    },
    layout: {
        scaleToolbar: 1.0,
        scaleTrackHeaders: 1.0,
        scaleTimeline: 1.0,
        scaleGallery: 1.0,
        labelWidth: 0,
        labelWidthFullscreen: 0,
        fullscreenSidebarWidth: 0,
        fullscreenTimelineHeight: 0,
    },
    timelineBehavior: {
        snappingEnabled: true,
        snapThreshold: 5,
        snapTargets: {
            playhead: true,
            selection: true,
            clipEdges: true,
            audioEdges: true,
            guides: true,
            promptSections: true,
            sceneBounds: true,
        },
        timecodeMode: "frames",
    },
    playback: {
        loopSelection: false,
        autoScrollPlayhead: true,
        returnToPlaybackStart: false,
        resolution: "full",
    },
    appearance: {
        waveformAccent: "#dcffdc",
        timelineBrightness: 100,
        clipLabelMode: "name_duration",
    },
    projectDefaults: {
        fps: 24,
        width: 768,
        height: 512,
        newSceneDuration: 200,
    },
    gallery: {
        sortMode: "newest",
        inspectorCollapsed: false,
        thumbnailSize: "medium",
    },
};

const VALID_SORT_MODES = new Set(GALLERY_SORT_OPTIONS.map((entry) => entry.value));
const VALID_THUMBNAIL_SIZES = new Set(GALLERY_THUMBNAIL_SIZE_OPTIONS.map((entry) => entry.value));
const VALID_PLAYBACK_RESOLUTIONS = new Set(PLAYBACK_RESOLUTION_OPTIONS.map((entry) => entry.value));
const VALID_CLIP_LABEL_MODES = new Set(CLIP_LABEL_MODE_OPTIONS.map((entry) => entry.value));
const VALID_TIMECODE_MODES = new Set(TIMECODE_MODE_OPTIONS.map((entry) => entry.value));
const VALID_SNAP_TARGETS = new Set(SNAP_TARGET_OPTIONS.map((entry) => entry.key));

const listeners = new Set();

function clone(value) {
    return JSON.parse(JSON.stringify(value));
}

function safeStorageGet(key) {
    try {
        return window.localStorage.getItem(key);
    } catch {
        return null;
    }
}

function safeStorageSet(key, value) {
    try {
        window.localStorage.setItem(key, value);
    } catch {
        // Ignore unavailable storage.
    }
}

function readStoredJson(key, fallback = null) {
    const raw = safeStorageGet(key);
    if (!raw) return fallback;
    try {
        return JSON.parse(raw);
    } catch {
        return fallback;
    }
}

function parseStoredNumber(key) {
    const raw = safeStorageGet(key);
    if (raw == null || raw === "") return null;
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
}

function clampNumber(value, min, max, fallback, round = false) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return fallback;
    const clamped = Math.max(min, Math.min(max, numeric));
    return round ? Math.round(clamped) : clamped;
}

function pickDefined(...values) {
    for (const value of values) {
        if (value !== undefined && value !== null) return value;
    }
    return undefined;
}

function isHexColor(value) {
    return typeof value === "string" && /^#[0-9a-fA-F]{6}$/.test(value);
}

function readStoredSettings() {
    return readStoredJson(SETTINGS_STORAGE_KEY, null);
}

function legacyLayoutSettings() {
    const oldGlobalScale = parseStoredNumber("ltx-editor-ui-scale");
    const readScale = (key) => pickDefined(
        parseStoredNumber(`ltx-editor-scale-${key}`),
        oldGlobalScale,
    );
    return {
        scaleToolbar: readScale("toolbar"),
        scaleTrackHeaders: readScale("trackheaders"),
        scaleTimeline: readScale("timeline"),
        scaleGallery: readScale("gallery"),
        labelWidth: parseStoredNumber("ltx-editor-label-width"),
        labelWidthFullscreen: parseStoredNumber("ltx-editor-label-width-fs"),
        fullscreenSidebarWidth: parseStoredNumber("ltx-editor-fs-sidebar-width"),
        fullscreenTimelineHeight: parseStoredNumber("ltx-editor-fs-timeline-height"),
    };
}

function normalizeSnapTargets(nextValue) {
    const defaults = DEFAULT_EDITOR_SETTINGS.timelineBehavior.snapTargets;
    const source = nextValue && typeof nextValue === "object" ? nextValue : {};
    const normalized = {};
    for (const key of VALID_SNAP_TARGETS) {
        normalized[key] = source[key] == null ? defaults[key] : !!source[key];
    }
    return normalized;
}

function normalizeEditorSettings(source = null) {
    const stored = source && typeof source === "object" ? source : {};
    const legacyLayout = legacyLayoutSettings();
    const defaults = DEFAULT_EDITOR_SETTINGS;
    return {
        version: SETTINGS_VERSION,
        meta: {
            legacyGalleryPrefsMigrated: !!stored?.meta?.legacyGalleryPrefsMigrated,
        },
        layout: {
            scaleToolbar: clampNumber(
                pickDefined(stored?.layout?.scaleToolbar, legacyLayout.scaleToolbar),
                0.7,
                2.0,
                defaults.layout.scaleToolbar,
            ),
            scaleTrackHeaders: clampNumber(
                pickDefined(stored?.layout?.scaleTrackHeaders, legacyLayout.scaleTrackHeaders),
                0.7,
                2.0,
                defaults.layout.scaleTrackHeaders,
            ),
            scaleTimeline: clampNumber(
                pickDefined(stored?.layout?.scaleTimeline, legacyLayout.scaleTimeline),
                0.7,
                2.0,
                defaults.layout.scaleTimeline,
            ),
            scaleGallery: clampNumber(
                pickDefined(stored?.layout?.scaleGallery, legacyLayout.scaleGallery),
                0.7,
                2.0,
                defaults.layout.scaleGallery,
            ),
            labelWidth: clampNumber(
                pickDefined(stored?.layout?.labelWidth, legacyLayout.labelWidth),
                0,
                2000,
                defaults.layout.labelWidth,
                true,
            ),
            labelWidthFullscreen: clampNumber(
                pickDefined(stored?.layout?.labelWidthFullscreen, legacyLayout.labelWidthFullscreen),
                0,
                2000,
                defaults.layout.labelWidthFullscreen,
                true,
            ),
            fullscreenSidebarWidth: clampNumber(
                pickDefined(stored?.layout?.fullscreenSidebarWidth, legacyLayout.fullscreenSidebarWidth),
                0,
                4000,
                defaults.layout.fullscreenSidebarWidth,
                true,
            ),
            fullscreenTimelineHeight: clampNumber(
                pickDefined(stored?.layout?.fullscreenTimelineHeight, legacyLayout.fullscreenTimelineHeight),
                0,
                4000,
                defaults.layout.fullscreenTimelineHeight,
                true,
            ),
        },
        timelineBehavior: {
            snappingEnabled: stored?.timelineBehavior?.snappingEnabled == null
                ? defaults.timelineBehavior.snappingEnabled
                : !!stored.timelineBehavior.snappingEnabled,
            snapThreshold: clampNumber(
                stored?.timelineBehavior?.snapThreshold,
                1,
                60,
                defaults.timelineBehavior.snapThreshold,
                true,
            ),
            snapTargets: normalizeSnapTargets(stored?.timelineBehavior?.snapTargets),
            timecodeMode: VALID_TIMECODE_MODES.has(stored?.timelineBehavior?.timecodeMode)
                ? stored.timelineBehavior.timecodeMode
                : defaults.timelineBehavior.timecodeMode,
        },
        playback: {
            loopSelection: stored?.playback?.loopSelection == null
                ? defaults.playback.loopSelection
                : !!stored.playback.loopSelection,
            autoScrollPlayhead: stored?.playback?.autoScrollPlayhead == null
                ? defaults.playback.autoScrollPlayhead
                : !!stored.playback.autoScrollPlayhead,
            returnToPlaybackStart: stored?.playback?.returnToPlaybackStart == null
                ? defaults.playback.returnToPlaybackStart
                : !!stored.playback.returnToPlaybackStart,
            resolution: VALID_PLAYBACK_RESOLUTIONS.has(stored?.playback?.resolution)
                ? stored.playback.resolution
                : defaults.playback.resolution,
        },
        appearance: {
            waveformAccent: isHexColor(stored?.appearance?.waveformAccent)
                ? stored.appearance.waveformAccent
                : defaults.appearance.waveformAccent,
            timelineBrightness: clampNumber(
                stored?.appearance?.timelineBrightness,
                70,
                130,
                defaults.appearance.timelineBrightness,
                true,
            ),
            clipLabelMode: VALID_CLIP_LABEL_MODES.has(stored?.appearance?.clipLabelMode)
                ? stored.appearance.clipLabelMode
                : defaults.appearance.clipLabelMode,
        },
        projectDefaults: {
            fps: clampNumber(stored?.projectDefaults?.fps, 1, 240, defaults.projectDefaults.fps),
            width: clampNumber(stored?.projectDefaults?.width, 64, 8192, defaults.projectDefaults.width, true),
            height: clampNumber(stored?.projectDefaults?.height, 64, 8192, defaults.projectDefaults.height, true),
            newSceneDuration: clampNumber(
                stored?.projectDefaults?.newSceneDuration,
                1,
                99999,
                defaults.projectDefaults.newSceneDuration,
                true,
            ),
        },
        gallery: {
            sortMode: VALID_SORT_MODES.has(stored?.gallery?.sortMode)
                ? stored.gallery.sortMode
                : defaults.gallery.sortMode,
            inspectorCollapsed: stored?.gallery?.inspectorCollapsed == null
                ? defaults.gallery.inspectorCollapsed
                : !!stored.gallery.inspectorCollapsed,
            thumbnailSize: VALID_THUMBNAIL_SIZES.has(stored?.gallery?.thumbnailSize)
                ? stored.gallery.thumbnailSize
                : defaults.gallery.thumbnailSize,
        },
    };
}

function mergeIntoSettings(base, patch) {
    if (!patch || typeof patch !== "object" || Array.isArray(patch)) return clone(base);
    const output = clone(base);
    const stack = [[output, patch]];
    while (stack.length) {
        const [target, source] = stack.pop();
        for (const [key, value] of Object.entries(source)) {
            if (value && typeof value === "object" && !Array.isArray(value)) {
                if (!target[key] || typeof target[key] !== "object" || Array.isArray(target[key])) {
                    target[key] = {};
                }
                stack.push([target[key], value]);
            } else {
                target[key] = value;
            }
        }
    }
    return output;
}

function persistCurrentSettings() {
    safeStorageSet(SETTINGS_STORAGE_KEY, JSON.stringify(currentSettings));
}

function notifyListeners() {
    const snapshot = getEditorSettings();
    for (const listener of listeners) {
        try {
            listener(snapshot);
        } catch (error) {
            console.warn("[LTX Editor] Settings listener failed:", error);
        }
    }
}

let currentSettings = normalizeEditorSettings(readStoredSettings());

export function getEditorSettings() {
    return clone(currentSettings);
}

export function updateEditorSettings(partial) {
    currentSettings = normalizeEditorSettings(mergeIntoSettings(currentSettings, partial));
    persistCurrentSettings();
    notifyListeners();
    return getEditorSettings();
}

export function subscribeEditorSettings(listener) {
    if (typeof listener !== "function") return () => {};
    listeners.add(listener);
    return () => {
        listeners.delete(listener);
    };
}

export function migrateLegacyGalleryProjectPrefs(projectId = "") {
    if (!projectId || currentSettings.meta.legacyGalleryPrefsMigrated) {
        return getEditorSettings();
    }
    const sortMode = readStoredJson(`ltx-gallery-${projectId}-sort-mode`, null);
    const inspectorCollapsed = readStoredJson(`ltx-gallery-${projectId}-inspector-collapsed`, null);
    if (!VALID_SORT_MODES.has(sortMode) && typeof inspectorCollapsed !== "boolean") {
        return getEditorSettings();
    }
    const nextPatch = {
        meta: {
            legacyGalleryPrefsMigrated: true,
        },
        gallery: {},
    };
    if (VALID_SORT_MODES.has(sortMode)) {
        nextPatch.gallery.sortMode = sortMode;
    }
    if (typeof inspectorCollapsed === "boolean") {
        nextPatch.gallery.inspectorCollapsed = inspectorCollapsed;
    }
    return updateEditorSettings(nextPatch);
}

if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
    window.addEventListener("storage", (event) => {
        if (event.key !== SETTINGS_STORAGE_KEY) return;
        currentSettings = normalizeEditorSettings(readStoredSettings());
        notifyListeners();
    });
}
