// Renamed during the Sonder pivot. No fallback read by design.
const SETTINGS_STORAGE_KEY = "sonder-editor-settings";
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

export const MODEL_TEMPLATE_PARAM_KEYS = ["width", "height", "frames", "fps"];

export const BUILTIN_MODEL_TEMPLATES = [
    { id: "free", name: "No Model Template", builtIn: true, constraints: {} },
    {
        id: "ltxv-2.3",
        name: "LTXV 2.3",
        builtIn: true,
        hintTier: 720,
        constraints: {
            width: { step: 32, offset: 0, min: 64, max: 2048 },
            height: { step: 32, offset: 0, min: 64, max: 2048 },
            frames: { step: 8, offset: 1, min: 1, max: 257 },
            fps: { min: 1, max: 120 },
            batchMaxFrames: 97,
        },
    },
];

export const RESOLUTION_TIERS = [
    { label: "~480p", c: 640 },
    { label: "~540p", c: 720 },
    { label: "~720p", c: 960 },
    { label: "~1080p", c: 1440 },
    { label: "~1440p", c: 1920 },
    { label: "~4K", c: 2880 },
];

export const ASPECT_RATIO_PRESETS = [
    { label: "16:9", a: 16, b: 9 },
    { label: "4:3", a: 4, b: 3 },
    { label: "3:4", a: 3, b: 4 },
    { label: "1:1", a: 1, b: 1 },
    { label: "21:9", a: 21, b: 9 },
    { label: "9:16", a: 9, b: 16 },
    { label: "Free", a: 0, b: 0 },
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
        queuePanelExpanded: false,
        queueBatchCollapsedByProject: {},
        trackCollapseByScene: {},
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
        laneTintOverrides: {
            video: "",
            audio: "",
            motion_driver: "",
        },
    },
    batchRender: {
        maxFramesPerChunk: 0,
    },
    modelTemplates: {
        customTemplates: [],
    },
    projectDefaults: {
        fps: 24,
        width: 768,
        height: 512,
        newSceneDuration: 200,
        defaultGuideStrength: 1.0,
        defaultMotionDriverStrength: 1.0,
        defaultTemplateId: "free",
    },
    gallery: {
        sortMode: "newest",
        inspectorCollapsed: false,
        thumbnailSize: "medium",
        artifactInspectorExpanded: false,
    },
};

const VALID_SORT_MODES = new Set(GALLERY_SORT_OPTIONS.map((entry) => entry.value));
const VALID_THUMBNAIL_SIZES = new Set(GALLERY_THUMBNAIL_SIZE_OPTIONS.map((entry) => entry.value));
const VALID_PLAYBACK_RESOLUTIONS = new Set(PLAYBACK_RESOLUTION_OPTIONS.map((entry) => entry.value));
const VALID_CLIP_LABEL_MODES = new Set(CLIP_LABEL_MODE_OPTIONS.map((entry) => entry.value));
const VALID_TIMECODE_MODES = new Set(TIMECODE_MODE_OPTIONS.map((entry) => entry.value));
const VALID_SNAP_TARGETS = new Set(SNAP_TARGET_OPTIONS.map((entry) => entry.key));
const BUILTIN_MODEL_TEMPLATE_IDS = new Set(BUILTIN_MODEL_TEMPLATES.map((entry) => entry.id));

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

function coerceFiniteNumber(value, { integer = false, min = null } = {}) {
    if (value === "" || value === null || value === undefined) return undefined;
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return undefined;
    const nextValue = integer ? Math.round(numeric) : numeric;
    if (min != null && nextValue < min) return undefined;
    return nextValue;
}

function sanitizeTemplateId(value, fallback = "custom-template") {
    if (typeof value !== "string") return fallback;
    const normalized = value
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "");
    return normalized || fallback;
}

function normalizeConstraint(constraint, key) {
    if (!constraint || typeof constraint !== "object" || Array.isArray(constraint)) {
        return undefined;
    }
    const integer = key !== "fps";
    const normalized = {};
    const step = coerceFiniteNumber(constraint.step, { integer, min: integer ? 1 : 0 });
    const offset = coerceFiniteNumber(constraint.offset, { integer });
    const min = coerceFiniteNumber(constraint.min, { integer });
    const max = coerceFiniteNumber(constraint.max, { integer });
    if (step !== undefined && step > 0) normalized.step = step;
    if (offset !== undefined) normalized.offset = offset;
    if (min !== undefined) normalized.min = min;
    if (max !== undefined) normalized.max = max;
    if (normalized.min !== undefined && normalized.max !== undefined && normalized.max < normalized.min) {
        normalized.max = normalized.min;
    }
    return Object.keys(normalized).length ? normalized : undefined;
}

function normalizeCustomTemplate(template, index = 0) {
    if (!template || typeof template !== "object" || Array.isArray(template)) {
        return null;
    }
    const name = typeof template.name === "string" && template.name.trim()
        ? template.name.trim()
        : `Custom Template ${index + 1}`;
    const id = sanitizeTemplateId(template.id, sanitizeTemplateId(name, `custom-template-${index + 1}`));
    const constraints = {};
    for (const key of MODEL_TEMPLATE_PARAM_KEYS) {
        const normalizedConstraint = normalizeConstraint(template.constraints?.[key], key);
        if (normalizedConstraint) {
            constraints[key] = normalizedConstraint;
        }
    }
    const batchMaxFrames = coerceFiniteNumber(template.constraints?.batchMaxFrames, { integer: true, min: 1 });
    if (batchMaxFrames !== undefined) {
        constraints.batchMaxFrames = batchMaxFrames;
    }
    const hintTier = coerceFiniteNumber(template.hintTier, { integer: true, min: 1 });
    return {
        id,
        name,
        builtIn: false,
        ...(hintTier !== undefined ? { hintTier } : {}),
        constraints,
    };
}

function normalizeCustomTemplates(templates) {
    if (!Array.isArray(templates)) return [];
    const usedIds = new Set(BUILTIN_MODEL_TEMPLATE_IDS);
    const normalized = [];
    for (let index = 0; index < templates.length; index += 1) {
        const template = normalizeCustomTemplate(templates[index], index);
        if (!template) continue;
        if (usedIds.has(template.id)) {
            let suffix = 2;
            let nextId = `${template.id}-${suffix}`;
            while (usedIds.has(nextId)) {
                suffix += 1;
                nextId = `${template.id}-${suffix}`;
            }
            template.id = nextId;
        }
        usedIds.add(template.id);
        normalized.push(template);
    }
    return normalized;
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

// Legacy keys predate the Sonder pivot - not renamed by design.
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

function normalizeLaneTintOverrides(nextValue) {
    const defaults = DEFAULT_EDITOR_SETTINGS.appearance.laneTintOverrides;
    const source = nextValue && typeof nextValue === "object" ? nextValue : {};
    const normalized = {};
    for (const key of Object.keys(defaults)) {
        const candidate = source[key];
        normalized[key] = isHexColor(candidate) ? candidate : "";
    }
    return normalized;
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

function normalizeTrackCollapseByScene(nextValue) {
    if (!nextValue || typeof nextValue !== "object" || Array.isArray(nextValue)) {
        return {};
    }
    const normalized = {};
    for (const [sceneKey, collapsedKeys] of Object.entries(nextValue)) {
        if (!sceneKey || !Array.isArray(collapsedKeys)) continue;
        normalized[sceneKey] = Array.from(new Set(
            collapsedKeys.filter((value) => typeof value === "string" && value)
        ));
    }
    return normalized;
}

function normalizeQueueBatchCollapsedByProject(nextValue) {
    if (!nextValue || typeof nextValue !== "object" || Array.isArray(nextValue)) {
        return {};
    }
    const normalized = {};
    for (const [projectKey, batchIds] of Object.entries(nextValue)) {
        if (!projectKey || !Array.isArray(batchIds)) continue;
        normalized[projectKey] = Array.from(new Set(
            batchIds.filter((value) => typeof value === "string" && value)
        ));
    }
    return normalized;
}

function normalizeEditorSettings(source = null) {
    const stored = source && typeof source === "object" ? source : {};
    const legacyLayout = legacyLayoutSettings();
    const defaults = DEFAULT_EDITOR_SETTINGS;
    const customTemplates = normalizeCustomTemplates(stored?.modelTemplates?.customTemplates);
    const validTemplateIds = new Set([
        ...BUILTIN_MODEL_TEMPLATE_IDS,
        ...customTemplates.map((template) => template.id),
    ]);
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
            queuePanelExpanded: stored?.layout?.queuePanelExpanded == null
                ? defaults.layout.queuePanelExpanded
                : !!stored.layout.queuePanelExpanded,
            queueBatchCollapsedByProject: normalizeQueueBatchCollapsedByProject(stored?.layout?.queueBatchCollapsedByProject),
            trackCollapseByScene: normalizeTrackCollapseByScene(stored?.layout?.trackCollapseByScene),
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
            laneTintOverrides: normalizeLaneTintOverrides(stored?.appearance?.laneTintOverrides),
        },
        batchRender: {
            maxFramesPerChunk: clampNumber(
                stored?.batchRender?.maxFramesPerChunk,
                0,
                10000,
                defaults.batchRender.maxFramesPerChunk,
                true,
            ),
        },
        modelTemplates: {
            customTemplates,
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
            defaultGuideStrength: clampNumber(
                stored?.projectDefaults?.defaultGuideStrength,
                0.0,
                1.0,
                defaults.projectDefaults.defaultGuideStrength,
            ),
            defaultMotionDriverStrength: clampNumber(
                stored?.projectDefaults?.defaultMotionDriverStrength,
                0.0,
                1.0,
                defaults.projectDefaults.defaultMotionDriverStrength,
            ),
            defaultTemplateId: validTemplateIds.has(stored?.projectDefaults?.defaultTemplateId)
                ? stored.projectDefaults.defaultTemplateId
                : defaults.projectDefaults.defaultTemplateId,
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
            artifactInspectorExpanded: stored?.gallery?.artifactInspectorExpanded == null
                ? defaults.gallery.artifactInspectorExpanded
                : !!stored.gallery.artifactInspectorExpanded,
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
            console.warn("[Sonder] Settings listener failed:", error);
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

export function getAllModelTemplates(settings) {
    return [...BUILTIN_MODEL_TEMPLATES, ...(settings?.modelTemplates?.customTemplates || [])];
}

export function getTemplateById(id, settings) {
    return getAllModelTemplates(settings).find((template) => template.id === id) || BUILTIN_MODEL_TEMPLATES[0];
}

export function snapToConstraint(value, constraint) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return 0;
    let snapped = numeric;
    if (constraint?.step) {
        const step = constraint.step;
        const offset = constraint.offset || 0;
        snapped = Math.round((numeric - offset) / step) * step + offset;
    }
    if (constraint?.min != null) snapped = Math.max(constraint.min, snapped);
    if (constraint?.max != null) snapped = Math.min(constraint.max, snapped);
    return snapped;
}

export function computeResolutionFromTier(c, a, b, template) {
    if (a <= 0 || b <= 0) return null;
    const rawW = Number(c) * Math.sqrt(a / b);
    const rawH = Number(c) * Math.sqrt(b / a);
    const width = Math.round(snapToConstraint(rawW, template?.constraints?.width));
    const height = Math.round(snapToConstraint(rawH, template?.constraints?.height));
    return { width, height };
}

export function snapResolution(width, height, template) {
    return {
        width: Math.round(snapToConstraint(width, template?.constraints?.width)),
        height: Math.round(snapToConstraint(height, template?.constraints?.height)),
    };
}

export function resolveBatchChunkSize({ settings, template } = {}) {
    const requested = pickDefined(
        settings?.batchRender?.maxFramesPerChunk > 0 ? settings.batchRender.maxFramesPerChunk : undefined,
        template?.constraints?.batchMaxFrames,
        97,
    );
    const numeric = Math.max(1, Math.round(Number(requested) || 97));
    const frameConstraint = template?.constraints?.frames;
    if (frameConstraint && typeof frameConstraint === "object") {
        return Math.max(1, Math.round(snapToConstraint(numeric, frameConstraint)));
    }
    return numeric;
}

export function describeConstraintFormula(constraint) {
    if (!constraint || !constraint.step) return "Any";
    const step = constraint.step;
    const offset = constraint.offset || 0;
    if (!offset) return `${step}n`;
    return `${step}n ${offset > 0 ? "+" : "-"} ${Math.abs(offset)}`;
}

export function previewConstraintValues(constraint, count = 5) {
    if (!constraint) return [];
    if (!constraint.step) {
        const values = [];
        if (constraint.min != null) values.push(constraint.min);
        if (constraint.max != null && constraint.max !== constraint.min) values.push(constraint.max);
        return values;
    }
    const values = [];
    const offset = constraint.offset || 0;
    const start = constraint.min != null
        ? Math.ceil((constraint.min - offset) / constraint.step)
        : 0;
    for (let index = 0; index < count; index += 1) {
        const value = (start + index) * constraint.step + offset;
        if (constraint.max != null && value > constraint.max) break;
        if (constraint.min != null && value < constraint.min) continue;
        values.push(value);
    }
    return values;
}

// Legacy keys predate the Sonder pivot - not renamed by design.
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
