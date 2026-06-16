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

export const GALLERY_TAB_OPTIONS = [
    { value: "all", label: "All" },
    { value: "video", label: "Videos" },
    { value: "image", label: "Images" },
    { value: "audio", label: "Audio" },
    { value: "artifact", label: "Artifacts" },
];

export const PLAYBACK_RESOLUTION_OPTIONS = [
    { value: "full", label: "Full" },
    { value: "half", label: "Half" },
    { value: "quarter", label: "Quarter" },
];

export const DIRECT_STREAMING_MODE_EXPERT_FLAG = "SONDER_EXPERT_DIRECT_STREAMING";
export const DIRECT_STREAMING_MODE_VISIBLE = false;

const INTERNAL_STREAMING_MODE_OPTIONS = [
    { value: "auto", label: "Auto (recommended)" },
    { value: "direct", label: "Direct streaming (expert)" },
    { value: "blob", label: "Full download (blob)" },
];

export function isDirectStreamingModeEnabled() {
    if (DIRECT_STREAMING_MODE_VISIBLE) return true;
    return safeStorageGet(DIRECT_STREAMING_MODE_EXPERT_FLAG) === "1";
}

export const STREAMING_MODE_OPTIONS = INTERNAL_STREAMING_MODE_OPTIONS.filter(
    (entry) => entry.value !== "direct" || isDirectStreamingModeEnabled()
);

export const CLIP_LABEL_MODE_OPTIONS = [
    { value: "name_duration", label: "Name + Duration" },
    { value: "name_only", label: "Name Only" },
    { value: "hidden", label: "Hidden" },
];

export const TAKE_PLACEMENT_MODE_OPTIONS = [
    { value: "trimmed", label: "Trimmed (default)" },
    { value: "untrimmed", label: "Untrimmed (show context)" },
];

export const SAVE_PRESET_OPTIONS = [
    { value: "Compatible MP4", label: "Compatible MP4", description: "MP4, H.264, yuv420p, AAC 192 kbps, browser preview compatible." },
    { value: "High Quality MP4", label: "High Quality MP4", description: "MP4, H.264 CRF 14, yuv420p, AAC 256 kbps, browser preview compatible." },
    { value: "Editing Master MP4", label: "Editing Master MP4", description: "MP4, H.264 CRF 10, yuv444p, AAC 256 kbps; browser preview may not decode it." },
    { value: "ProRes 422 HQ", label: "ProRes 422 HQ", description: "MOV, ProRes 422 HQ, yuv422p10le, PCM audio; editing handoff file." },
    { value: "Lossless FFV1 (RGB)", label: "Lossless FFV1 (RGB)", description: "MKV, FFV1 lossless RGB, gbrp, FLAC audio; archive/diagnostic output." },
    { value: "Custom", label: "Custom", description: "Expose allowlisted expert controls for video files or PNG image sequences." },
];

export const DEFAULT_SAVE_PRESET = "Compatible MP4";

export const CUSTOM_OUTPUT_KIND_VIDEO = "Video File";
export const CUSTOM_OUTPUT_KIND_PNG_SEQUENCE = "PNG Sequence";
export const CUSTOM_OUTPUT_KIND_OPTIONS = [CUSTOM_OUTPUT_KIND_VIDEO, CUSTOM_OUTPUT_KIND_PNG_SEQUENCE];
export const CUSTOM_CONTAINER_OPTIONS = ["mp4", "mov", "mkv"];
export const CUSTOM_VIDEO_CODEC_OPTIONS = ["libx264", "libx265", "prores_ks", "ffv1"];
export const CUSTOM_PIX_FMT_OPTIONS = ["yuv420p", "yuv444p", "yuv422p10le", "gbrp"];
export const CUSTOM_ENCODER_PRESET_OPTIONS = [
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
];
export const CUSTOM_AUDIO_CODEC_OPTIONS = ["aac", "pcm_s16le", "flac", "none"];

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
        id: "ltx-2.3",
        name: "LTX 2.3",
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
        activeSelectionByProjectScene: {},
        timelinePixelsPerFrame: 3,
        labelWidth: 0,
        labelWidthFullscreen: 0,
        fullscreenSidebarWidth: 0,
        fullscreenTimelineHeight: 0,
    },
    timelineBehavior: {
        linkedVideoAudioDrop: true,
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
        prebufferEnabled: true,
        prebufferLookaheadMs: 5000,
        streamingMode: "auto",
    },
    notifications: {
        toastDurationMs: 4000,
        errorToastDurationMs: 0, // 0 = stay until dismissed
    },
    appearance: {
        waveformAccent: "#dcffdc",
        timelineBrightness: 100,
        clipLabelMode: "name_duration",
        sceneOutline: true,
        laneTintOverrides: {
            video: "",
            audio: "",
            motion_driver: "",
        },
    },
    batchRender: {
        maxFramesPerChunk: 0,
    },
    render: {
        takePlacementMode: "trimmed",
        linkedTakePlacement: true,
        takePlacementMuted: false,
        defaultSavePreset: DEFAULT_SAVE_PRESET,
        maxRenderCacheEntries: 3,
        trashRetentionDays: 30,
        trashMaxSizeMB: null,
        export: {
            lastPreset: DEFAULT_SAVE_PRESET,
            lastCustomEncode: null,
            filenamePrefix: "",
            includeVideo: true,
            includeAudio: true,
            placeAsTake: true,
        },
    },
    guides: {
        guideSnapshotMaxLongEdge: 0,
        hoverPreviewEnabled: true,
        hoverPreviewSize: 180,
    },
    modelTemplates: {
        customTemplates: [],
    },
    promptTemplates: [],
    prompts: {
        queueSectionBatch: true,
        hoverPreviewEnabled: true,
        panelMode: "structured",
        panelChannelBoxHeight: 0,
        panelGlobalBoxHeight: 0,
        panelDraftBoxHeight: 0,
        writingDraftByProjectScene: {},
    },
    projectDefaults: {
        fps: 24,
        width: 768,
        height: 512,
        newSceneDuration: 200,
        defaultGuideStrength: 1.0,
        defaultMotionDriverStrength: 1.0,
        defaultTemplateId: "free",
        defaultFitMode: "pad_edge",
        defaultCropPosition: "center",
    },
    gallery: {
        sortMode: "newest",
        activeTab: "all",
        inspectorCollapsed: false,
        thumbnailSize: "medium",
        artifactInspectorExpanded: false,
    },
    inspector: {
        compareLayout: "divider",
        sideBySideLinkZoom: true,
        audioCompareWaveformLayout: "stacked",
        audioCompareMonitor: "a",
        compareCycleSide: "B",
    },
};

const VALID_SORT_MODES = new Set(GALLERY_SORT_OPTIONS.map((entry) => entry.value));
const VALID_THUMBNAIL_SIZES = new Set(GALLERY_THUMBNAIL_SIZE_OPTIONS.map((entry) => entry.value));
const VALID_GALLERY_TABS = new Set(GALLERY_TAB_OPTIONS.map((entry) => entry.value));
const VALID_PLAYBACK_RESOLUTIONS = new Set(PLAYBACK_RESOLUTION_OPTIONS.map((entry) => entry.value));
const VALID_STREAMING_MODES = new Set(INTERNAL_STREAMING_MODE_OPTIONS.map((entry) => entry.value));
const VALID_CLIP_LABEL_MODES = new Set(CLIP_LABEL_MODE_OPTIONS.map((entry) => entry.value));
const VALID_TIMECODE_MODES = new Set(TIMECODE_MODE_OPTIONS.map((entry) => entry.value));
const VALID_SAVE_PRESETS = new Set(SAVE_PRESET_OPTIONS.map((entry) => entry.value));
const VALID_SNAP_TARGETS = new Set(SNAP_TARGET_OPTIONS.map((entry) => entry.key));
const BUILTIN_MODEL_TEMPLATE_IDS = new Set(BUILTIN_MODEL_TEMPLATES.map((entry) => entry.id));

// Legacy model-template ids that earlier projects/settings may still carry.
// Resolution is frontend-only: the backend stores template_id as an opaque
// string, so normalizing here (the single chokepoint every consumer flows
// through) keeps legacy projects resolving and lets the next save write back
// the canonical id automatically. Must be declared up here with the other
// module-level config consts: the eager `currentSettings` initializer below
// calls normalizeEditorSettings() -> normalizeModelTemplateId() during module
// load, so a late `const` would throw a TDZ ReferenceError and abort the whole
// frontend extension.
const LEGACY_MODEL_TEMPLATE_ID_ALIASES = { "ltxv-2.3": "ltx-2.3" };

export function normalizeModelTemplateId(id) {
    return LEGACY_MODEL_TEMPLATE_ID_ALIASES[id] ?? id;
}
const VALID_COMPARE_LAYOUTS = new Set(["divider", "sideBySide"]);
const VALID_AUDIO_COMPARE_WAVEFORM_LAYOUTS = new Set(["stacked", "overlay"]);
const VALID_AUDIO_COMPARE_MONITORS = new Set(["a", "b", "both", "mute"]);
const VALID_COMPARE_CYCLE_SIDES = new Set(["A", "B"]);
export const VALID_TAKE_PLACEMENT_MODES = new Set(TAKE_PLACEMENT_MODE_OPTIONS.map((entry) => entry.value));

// Per-item fit modes (mirror server/media_helpers.py FIT_MODES / CROP_POSITIONS).
// The defaults below ARE the fixed code constants — they only seed NEW items,
// never act as a render-time fallback (that lives in the backend from_dict).
export const FIT_MODE_OPTIONS = [
    { value: "fit", label: "Fit (black bars)" },
    { value: "pad_edge", label: "Fit (edge pad)" },
    { value: "cover", label: "Fill (crop)" },
    { value: "stretch", label: "Stretch" },
];
export const CROP_POSITION_OPTIONS = [
    { value: "center", label: "Center" },
    { value: "top", label: "Top" },
    { value: "bottom", label: "Bottom" },
    { value: "left", label: "Left" },
    { value: "right", label: "Right" },
];
export const VALID_FIT_MODES = new Set(FIT_MODE_OPTIONS.map((entry) => entry.value));
export const VALID_CROP_POSITIONS = new Set(CROP_POSITION_OPTIONS.map((entry) => entry.value));
export const DEFAULT_FIT_MODE = "pad_edge";
export const DEFAULT_CROP_POSITION = "center";
const VALID_CUSTOM_CONTAINERS = new Set(CUSTOM_CONTAINER_OPTIONS);
const VALID_CUSTOM_VIDEO_CODECS = new Set(CUSTOM_VIDEO_CODEC_OPTIONS);
const VALID_CUSTOM_PIX_FMTS = new Set(CUSTOM_PIX_FMT_OPTIONS);
const VALID_CUSTOM_ENCODER_PRESETS = new Set(CUSTOM_ENCODER_PRESET_OPTIONS);
const VALID_CUSTOM_AUDIO_CODECS = new Set(CUSTOM_AUDIO_CODEC_OPTIONS);

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

// Writing-mode drafts: authoring scratch keyed `project::scene`. Unknown keys
// pass through untouched (same policy as activeSelectionByProjectScene);
// dirty flags must survive cross-tab re-normalization. LRU by `ts`, capped.
// NOTE: defined ABOVE the eager normalizeEditorSettings init (TDZ trap).
const WRITING_DRAFT_MAP_CAP = 40;
const WRITING_DRAFT_TEXT_CAP = 20000;

function normalizeWritingDrafts(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
    const entries = [];
    for (const [key, value] of Object.entries(raw)) {
        if (!value || typeof value !== "object") continue;
        entries.push([key, {
            ts: Number(value.ts) || 0,
            draft: String(value.draft ?? "").slice(0, WRITING_DRAFT_TEXT_CAP),
            allocations: Array.isArray(value.allocations)
                ? value.allocations.map((a) => ({
                    length: Math.max(0, parseInt(a?.length, 10) || 0),
                    dirty: !!a?.dirty,
                }))
                : [],
        }]);
    }
    entries.sort((a, b) => a[1].ts - b[1].ts);
    return Object.fromEntries(entries.slice(-WRITING_DRAFT_MAP_CAP));
}

function normalizePromptsSettings(stored, defaults) {
    const raw = stored && typeof stored === "object" ? stored : {};
    const clampHeight = (value) => {
        const n = parseInt(value, 10);
        if (!Number.isFinite(n) || n <= 0) return 0;
        return Math.max(40, Math.min(800, n));
    };
    return {
        queueSectionBatch: raw.queueSectionBatch == null ? defaults.queueSectionBatch : !!raw.queueSectionBatch,
        hoverPreviewEnabled: raw.hoverPreviewEnabled == null ? defaults.hoverPreviewEnabled : !!raw.hoverPreviewEnabled,
        panelMode: raw.panelMode === "writing" ? "writing" : "structured",
        panelChannelBoxHeight: clampHeight(raw.panelChannelBoxHeight),
        panelGlobalBoxHeight: clampHeight(raw.panelGlobalBoxHeight),
        panelDraftBoxHeight: clampHeight(raw.panelDraftBoxHeight),
        writingDraftByProjectScene: normalizeWritingDrafts(raw.writingDraftByProjectScene),
    };
}

// Prompt templates: browser-local reusable prompt setups (global text +
// channel-bearing sections). Applying a template materializes concrete text
// into scene state, so project truth never depends on this library.
// NOTE: defined ABOVE the eager normalizeEditorSettings init (TDZ trap —
// durable_rules.md > Technical Traps).
function normalizePromptTemplates(templates) {
    if (!Array.isArray(templates)) return [];
    const normalized = [];
    for (let index = 0; index < templates.length; index += 1) {
        const raw = templates[index];
        if (!raw || typeof raw !== "object") continue;
        const name = String(raw.name || "").trim();
        if (!name) continue;
        const sections = Array.isArray(raw.sections)
            ? raw.sections
                .filter((s) => s && typeof s === "object")
                .map((s) => ({
                    start_frame: Math.max(0, parseInt(s.start_frame, 10) || 0),
                    end_frame: Math.max(0, parseInt(s.end_frame, 10) || 0),
                    channels: {
                        visual: String(s.channels?.visual ?? s.prompt ?? ""),
                        speech: String(s.channels?.speech ?? ""),
                        sounds: String(s.channels?.sounds ?? ""),
                    },
                }))
            : [];
        normalized.push({
            id: String(raw.id || `prompt-template-${index}-${name.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`),
            name,
            global: String(raw.global ?? ""),
            sections,
        });
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

function normalizeActiveSelectionByProjectScene(nextValue) {
    if (!nextValue || typeof nextValue !== "object" || Array.isArray(nextValue)) {
        return {};
    }
    const normalized = {};
    for (const [projectKey, sceneMap] of Object.entries(nextValue)) {
        if (!projectKey || !sceneMap || typeof sceneMap !== "object" || Array.isArray(sceneMap)) continue;
        const normalizedScenes = {};
        for (const [sceneId, selection] of Object.entries(sceneMap)) {
            if (!sceneId || !selection || typeof selection !== "object" || Array.isArray(selection)) continue;
            const start = clampNumber(selection.start, 0, 999999, 0, true);
            const end = clampNumber(selection.end, 0, 999999, 0, true);
            normalizedScenes[sceneId] = {
                start: Math.min(start, end),
                end: Math.max(start, end),
            };
        }
        if (Object.keys(normalizedScenes).length) {
            normalized[projectKey] = normalizedScenes;
        }
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

function sanitizeExportFilenamePrefix(value) {
    return String(value || "")
        .replace(/[\\/:*?"<>|]+/g, " ")
        .replace(/\s+/g, " ")
        .trim()
        .slice(0, 64);
}

function normalizeLastCustomEncode(nextValue) {
    if (!nextValue || typeof nextValue !== "object" || Array.isArray(nextValue)) {
        return null;
    }
    return {
        custom_output_kind: CUSTOM_OUTPUT_KIND_VIDEO,
        custom_container: VALID_CUSTOM_CONTAINERS.has(nextValue.custom_container) ? nextValue.custom_container : "mp4",
        custom_video_codec: VALID_CUSTOM_VIDEO_CODECS.has(nextValue.custom_video_codec) ? nextValue.custom_video_codec : "libx264",
        custom_pix_fmt: VALID_CUSTOM_PIX_FMTS.has(nextValue.custom_pix_fmt) ? nextValue.custom_pix_fmt : "yuv420p",
        custom_crf: clampNumber(nextValue.custom_crf, 0, 51, 18, true),
        custom_encoder_preset: VALID_CUSTOM_ENCODER_PRESETS.has(nextValue.custom_encoder_preset) ? nextValue.custom_encoder_preset : "slow",
        custom_audio_codec: VALID_CUSTOM_AUDIO_CODECS.has(nextValue.custom_audio_codec) ? nextValue.custom_audio_codec : "aac",
        custom_audio_bitrate_kbps: clampNumber(nextValue.custom_audio_bitrate_kbps, 1, 10000, 192, true),
        custom_png_compression: clampNumber(nextValue.custom_png_compression, 0, 9, 0, true),
    };
}

function normalizeRenderExportSettings(nextValue) {
    const defaults = DEFAULT_EDITOR_SETTINGS.render.export;
    const source = nextValue && typeof nextValue === "object" && !Array.isArray(nextValue)
        ? nextValue
        : {};
    return {
        lastPreset: VALID_SAVE_PRESETS.has(source.lastPreset) ? source.lastPreset : defaults.lastPreset,
        lastCustomEncode: normalizeLastCustomEncode(source.lastCustomEncode),
        filenamePrefix: sanitizeExportFilenamePrefix(source.filenamePrefix),
        includeVideo: source.includeVideo == null ? defaults.includeVideo : !!source.includeVideo,
        includeAudio: source.includeAudio == null ? defaults.includeAudio : !!source.includeAudio,
        placeAsTake: source.placeAsTake == null ? defaults.placeAsTake : !!source.placeAsTake,
    };
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
    const storedStreamingMode = stored?.playback?.streamingMode;
    const streamingMode = VALID_STREAMING_MODES.has(storedStreamingMode)
        ? (storedStreamingMode === "direct" && !isDirectStreamingModeEnabled()
            ? defaults.playback.streamingMode
            : storedStreamingMode)
        : defaults.playback.streamingMode;
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
            activeSelectionByProjectScene: normalizeActiveSelectionByProjectScene(stored?.layout?.activeSelectionByProjectScene),
            timelinePixelsPerFrame: clampNumber(
                stored?.layout?.timelinePixelsPerFrame,
                0.2,
                40,
                defaults.layout.timelinePixelsPerFrame,
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
            linkedVideoAudioDrop: stored?.timelineBehavior?.linkedVideoAudioDrop == null
                ? defaults.timelineBehavior.linkedVideoAudioDrop
                : !!stored.timelineBehavior.linkedVideoAudioDrop,
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
            prebufferEnabled: stored?.playback?.prebufferEnabled == null
                ? defaults.playback.prebufferEnabled
                : !!stored.playback.prebufferEnabled,
            prebufferLookaheadMs: clampNumber(
                stored?.playback?.prebufferLookaheadMs,
                100,
                5000,
                defaults.playback.prebufferLookaheadMs,
                true,
            ),
            streamingMode,
        },
        notifications: {
            toastDurationMs: clampNumber(
                stored?.notifications?.toastDurationMs,
                1000,
                30000,
                defaults.notifications.toastDurationMs,
                true,
            ),
            errorToastDurationMs: clampNumber(
                stored?.notifications?.errorToastDurationMs,
                0,
                120000,
                defaults.notifications.errorToastDurationMs,
                true,
            ),
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
            sceneOutline: stored?.appearance?.sceneOutline == null
                ? defaults.appearance.sceneOutline
                : !!stored.appearance.sceneOutline,
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
        render: {
            takePlacementMode: VALID_TAKE_PLACEMENT_MODES.has(stored?.render?.takePlacementMode)
                ? stored.render.takePlacementMode
                : defaults.render.takePlacementMode,
            linkedTakePlacement: stored?.render?.linkedTakePlacement == null
                ? defaults.render.linkedTakePlacement
                : !!stored.render.linkedTakePlacement,
            takePlacementMuted: stored?.render?.takePlacementMuted == null
                ? defaults.render.takePlacementMuted
                : !!stored.render.takePlacementMuted,
            defaultSavePreset: VALID_SAVE_PRESETS.has(stored?.render?.defaultSavePreset)
                ? stored.render.defaultSavePreset
                : defaults.render.defaultSavePreset,
            maxRenderCacheEntries: stored?.render?.maxRenderCacheEntries === null
                ? null
                : clampNumber(
                    stored?.render?.maxRenderCacheEntries,
                    1,
                    100000,
                    defaults.render.maxRenderCacheEntries,
                    true,
                ),
            trashRetentionDays: clampNumber(
                stored?.render?.trashRetentionDays,
                0,
                36500,
                defaults.render.trashRetentionDays,
                true,
            ),
            trashMaxSizeMB: stored?.render?.trashMaxSizeMB === null
                ? null
                : clampNumber(
                    stored?.render?.trashMaxSizeMB,
                    0,
                    100000000,
                    defaults.render.trashMaxSizeMB,
                ),
            export: normalizeRenderExportSettings(stored?.render?.export),
        },
        guides: {
            guideSnapshotMaxLongEdge: clampNumber(
                stored?.guides?.guideSnapshotMaxLongEdge,
                0,
                8192,
                defaults.guides.guideSnapshotMaxLongEdge,
                true,
            ),
            hoverPreviewEnabled: stored?.guides?.hoverPreviewEnabled == null
                ? defaults.guides.hoverPreviewEnabled
                : !!stored.guides.hoverPreviewEnabled,
            hoverPreviewSize: clampNumber(
                stored?.guides?.hoverPreviewSize,
                96,
                360,
                defaults.guides.hoverPreviewSize,
                true,
            ),
        },
        modelTemplates: {
            customTemplates,
        },
        promptTemplates: normalizePromptTemplates(stored?.promptTemplates),
        prompts: normalizePromptsSettings(stored?.prompts, defaults.prompts),
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
            defaultTemplateId: validTemplateIds.has(normalizeModelTemplateId(stored?.projectDefaults?.defaultTemplateId))
                ? normalizeModelTemplateId(stored.projectDefaults.defaultTemplateId)
                : defaults.projectDefaults.defaultTemplateId,
            defaultFitMode: VALID_FIT_MODES.has(stored?.projectDefaults?.defaultFitMode)
                ? stored.projectDefaults.defaultFitMode
                : defaults.projectDefaults.defaultFitMode,
            defaultCropPosition: VALID_CROP_POSITIONS.has(stored?.projectDefaults?.defaultCropPosition)
                ? stored.projectDefaults.defaultCropPosition
                : defaults.projectDefaults.defaultCropPosition,
        },
        gallery: {
            sortMode: VALID_SORT_MODES.has(stored?.gallery?.sortMode)
                ? stored.gallery.sortMode
                : defaults.gallery.sortMode,
            activeTab: VALID_GALLERY_TABS.has(stored?.gallery?.activeTab)
                ? stored.gallery.activeTab
                : defaults.gallery.activeTab,
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
        inspector: {
            compareLayout: VALID_COMPARE_LAYOUTS.has(stored?.inspector?.compareLayout)
                ? stored.inspector.compareLayout
                : defaults.inspector.compareLayout,
            sideBySideLinkZoom: stored?.inspector?.sideBySideLinkZoom == null
                ? defaults.inspector.sideBySideLinkZoom
                : !!stored.inspector.sideBySideLinkZoom,
            audioCompareWaveformLayout: VALID_AUDIO_COMPARE_WAVEFORM_LAYOUTS.has(stored?.inspector?.audioCompareWaveformLayout)
                ? stored.inspector.audioCompareWaveformLayout
                : defaults.inspector.audioCompareWaveformLayout,
            audioCompareMonitor: VALID_AUDIO_COMPARE_MONITORS.has(stored?.inspector?.audioCompareMonitor)
                ? stored.inspector.audioCompareMonitor
                : defaults.inspector.audioCompareMonitor,
            compareCycleSide: VALID_COMPARE_CYCLE_SIDES.has(stored?.inspector?.compareCycleSide)
                ? stored.inspector.compareCycleSide
                : defaults.inspector.compareCycleSide,
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

// Maps browser-local notification settings to the editor_notifications Core
// config shape (ms; 0 = sticky). `toastDurationMs` drives info+success;
// `errorToastDurationMs` drives error (0 = stay until dismissed). Warnings stay
// sticky by design. Callers push this into `configureNotifications()`.
export function notificationCoreConfig(settings = currentSettings) {
    const n = settings?.notifications || DEFAULT_EDITOR_SETTINGS.notifications;
    return {
        info: n.toastDurationMs,
        success: n.toastDurationMs,
        error: n.errorToastDurationMs,
    };
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
    const wanted = normalizeModelTemplateId(id);
    return getAllModelTemplates(settings).find((template) => template.id === wanted) || BUILTIN_MODEL_TEMPLATES[0];
}

export function resolveFrameConstraintForTemplate(templateId, settings) {
    const template = getTemplateById(templateId, settings);
    const frames = template?.constraints?.frames;
    if (!frames || typeof frames !== "object") return null;
    if (!Object.keys(frames).length) return null;
    return frames;
}

export function frameConstraintsEqual(a, b) {
    const normalize = (value) => {
        if (!value || typeof value !== "object") return null;
        const keys = ["step", "offset", "min", "max"];
        const result = {};
        let any = false;
        for (const key of keys) {
            if (value[key] != null) {
                result[key] = value[key];
                any = true;
            }
        }
        return any ? result : null;
    };
    const left = normalize(a);
    const right = normalize(b);
    if (left === null && right === null) return true;
    if (left === null || right === null) return false;
    const leftKeys = Object.keys(left).sort();
    const rightKeys = Object.keys(right).sort();
    if (leftKeys.length !== rightKeys.length) return false;
    for (let i = 0; i < leftKeys.length; i += 1) {
        if (leftKeys[i] !== rightKeys[i]) return false;
        if (left[leftKeys[i]] !== right[leftKeys[i]]) return false;
    }
    return true;
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

export function resolveBatchChunkSize({ settings, template, preContext = 0, postContext = 0 } = {}) {
    // `batchMaxFrames` is a TOTAL rendered-tensor budget per chunk (LTX 2.3 default
    // of 97 = the model's frame ceiling). Selection chunk size = total budget minus
    // context. Mirrors the four-snap policy in editor_node.py / editor_widget.js:
    // pre snaps to G (when > 0), post snaps to multiples of step, total snaps UP
    // (ceil) to next G, selection budget snaps to V (multiples of step when pre>0,
    // G when pre==0).
    const requested = pickDefined(
        settings?.batchRender?.maxFramesPerChunk > 0 ? settings.batchRender.maxFramesPerChunk : undefined,
        template?.constraints?.batchMaxFrames,
        97,
    );
    const numeric = Math.max(1, Math.round(Number(requested) || 97));
    const pre = Math.max(0, Math.round(Number(preContext) || 0));
    const post = Math.max(0, Math.round(Number(postContext) || 0));
    const frameConstraint = template?.constraints?.frames;
    if (!frameConstraint || typeof frameConstraint !== "object" || !frameConstraint.step || frameConstraint.step <= 1) {
        // Free / no-constraint: just subtract context from the requested budget.
        return Math.max(1, numeric - pre - post);
    }
    const step = frameConstraint.step;
    const offset = frameConstraint.offset || 0;
    // Snap pre to G (when > 0); snap post to multiple of step. Both go UP.
    const snappedPre = pre > 0
        ? offset + Math.ceil(Math.max(0, pre - offset) / step) * step
        : 0;
    const snappedPost = post > 0 ? Math.ceil(post / step) * step : 0;
    // Total budget: explicit ceil to next G value (NOT snapToConstraint, which rounds
    // to nearest and would snap DOWN for some inputs, e.g. numeric=92 -> 89).
    const targetTotal = numeric <= offset
        ? Math.max(1, offset)
        : offset + Math.ceil((numeric - offset) / step) * step;
    const selectionBudget = targetTotal - snappedPre - snappedPost;
    if (selectionBudget <= 0) {
        // Context already swallows the budget; emit smallest valid chunk so at least one job lands.
        return snappedPre > 0 ? step : Math.max(1, offset || step);
    }
    if (snappedPre > 0) {
        // Selection ∈ {multiples of step} so out_point = pre + sel ∈ G.
        return Math.max(step, Math.ceil(selectionBudget / step) * step);
    }
    // pre == 0: selection ∈ G (in_point at 0, out_point = sel must be in G).
    if (selectionBudget <= offset) return offset || step;
    return offset + Math.ceil((selectionBudget - offset) / step) * step;
}

export function resolveBatchChunkSizes({ settings, template, preContext = 0, postContext = 0, selectionStart = 0 } = {}) {
    // Returns { chunkSize, firstChunkSize }. chunkSize is the size for non-first chunks.
    // The FIRST chunk carries the template's constant frame (offset) in its gen when it
    // sits at the scene start and therefore has no real frames before it to grab as
    // pre-context: with preContext > 0 the snapped pre is always >= 1, so "no available
    // pre-context" reduces exactly to selectionStart <= 0. Without this, the first chunk
    // total = chunkSize (a bare multiple of step) is off-grid and the backend would
    // tail-pad it with a repeated frame instead of generating the constant as a real
    // frame. `chunkSize + offset` lands the total on G and self-handles offset == 0
    // templates (no-op). Guarded by step > 1 so free / no-constraint templates are
    // unaffected. Subsequent chunks (which grab pre-context from the prior take) stay
    // at chunkSize.
    const chunkSize = resolveBatchChunkSize({ settings, template, preContext, postContext });
    const frameConstraint = template?.constraints?.frames;
    const step = frameConstraint?.step;
    const offset = frameConstraint?.offset || 0;
    const firstChunkLacksPreContext = preContext > 0 && (parseInt(selectionStart, 10) || 0) <= 0;
    const firstChunkSize = (step && step > 1 && firstChunkLacksPreContext)
        ? chunkSize + offset
        : chunkSize;
    return { chunkSize, firstChunkSize };
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
