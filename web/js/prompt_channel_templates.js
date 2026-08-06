// Prompt channel templates — mirror of server/prompt_channel_templates.py.
// Keep the preset ids, channel keys, labels, separators and policies in
// lockstep with the backend module; tests/test_prompt_channel_templates_js.py
// asserts that parity.
//
// This module is DISPLAY + authoring metadata for the panel. The backend
// stays authoritative for every model-visible string, exactly as
// prompt_composition.js is a compose-only twin of prompt_payload.py.

export const DEFAULT_CHANNEL_TEMPLATE_ID = "sonder";

export const LABELS_ALWAYS = "always";
export const LABELS_NEVER = "never";
export const LABELS_PROJECT = "project";

export const GLOBAL_MERGE_LEADING = "leading";
export const GLOBAL_MERGE_PER_CHANNEL = "per_channel";

export const PROJECT_TEMPLATE_KEY = "prompt_channel_template";

const channel = (key, label, description) => ({ key, label, description });

const SONDER_CHANNELS = [
    channel("visual", "[VISUAL]:",
        "What is seen: subjects, action, framing, style and lighting."),
    channel("speech", "[SPEECH]:",
        "What is said or sung, including who says it."),
    channel("sounds", "[SOUNDS]:",
        "What is heard that is not speech: ambience, effects, music."),
];

const MINIMAX_SOUNDSCAPE = channel("overall_soundscape", "overall_soundscape",
    "1-4 English sentences summarizing ambience, physical action sounds and "
    + "non-verbal human sounds across the WHOLE video. Dialogue, singing and "
    + "diegetic music belong in the description field instead.");
const MINIMAX_NON_DIEGETIC = channel("non_diegetic_music", "non_diegetic_music",
    "1-3 English sentences describing background music only the audience "
    + "hears. Give instrumentation, tempo, rhythm and dynamics — not mood words. "
    + "Music the characters can hear is diegetic and belongs in the description.");

const MINIMAX_BASE_CHANNELS = [
    channel("integrated_multimodal_description", "integrated_multimodal_description",
        "The main body. Visuals, actions, shots, speakers, dialogue and "
        + "diegetic audio along the timeline. Open with the overall style "
        + "and initial composition. Shot markers are inserted here."),
    MINIMAX_SOUNDSCAPE,
    MINIMAX_NON_DIEGETIC,
];

const MINIMAX_REF_CHANNELS = [
    channel("subject_definitions", "subject_definitions",
        "One `<Subject N> is ...` line per piece of referenced content, "
        + "saying what the label denotes and the features to follow. Also "
        + "define `<Picture N>`, `<Video N>` and `<Audio N>` here when they "
        + "are tracked separately."),
    channel("summary", "summary",
        "One short paragraph summarizing the target video and its "
        + "reference relationships, opening with a bracketed task-type "
        + "prefix such as `[reference generation + audio reference]`. Use "
        + "the reference labels; do not introduce new ones."),
    channel("retention_analysis", "retention_analysis",
        "One line per reference label saying how it is preserved, using "
        + "the fixed markers fully_preserved, partially_preserved, "
        + "attribute_transfer or weak_reference (audio uses fully_copy, "
        + "partially_copy, reference, weak_reference)."),
    channel("detailed_description", "detailed_description",
        "The main body. Visuals, actions, sound and dialogue shot by shot "
        + "in playback order, inserting `<Subject N>`, `<Picture N>`, "
        + "`<Video N>` and `<Audio N>` where they apply. Shot markers are "
        + "inserted here."),
    MINIMAX_SOUNDSCAPE,
    MINIMAX_NON_DIEGETIC,
];

function template(id, name, description, channels, options = {}) {
    return {
        id,
        name,
        description,
        channels,
        field_separator: options.fieldSeparator ?? " ",
        label_separator: options.labelSeparator ?? " ",
        labels: options.labels ?? LABELS_PROJECT,
        shot_marker_channel: options.shotMarkerChannel ?? "",
        global_merge: options.globalMerge ?? GLOBAL_MERGE_LEADING,
        global_channels_enabled: options.globalChannelsOn !== false,
        builtin: true,
    };
}

export const PROMPT_CHANNEL_TEMPLATE_PRESETS = {
    standard: template("standard", "Standard",
        "One plain prompt channel, with no field names.",
        [channel("visual", "", "The whole prompt for this section.")],
        { labels: LABELS_NEVER }),
    // Display name only — the id stays `sonder` so existing projects and frozen
    // jobs keep resolving. This channel split is not ours to claim.
    sonder: template(DEFAULT_CHANNEL_TEMPLATE_ID, "Visual + Speech + Sound",
        "The editor's default: separate visual, speech and sound channels, "
        + "labelled only when the project's channel-label toggle is on.",
        SONDER_CHANNELS),
    minimax_h3_base: template("minimax_h3_base", "MiniMax H3",
        "MiniMax H3's three core fields for text- and keyframe-driven "
        + "generation (T2VA / I2VA / FL2VA / L2VA).",
        MINIMAX_BASE_CHANNELS,
        {
            fieldSeparator: "\n\n", labelSeparator: ": ", labels: LABELS_ALWAYS,
            shotMarkerChannel: "integrated_multimodal_description",
            globalMerge: GLOBAL_MERGE_PER_CHANNEL,
        }),
    minimax_h3_ref: template("minimax_h3_ref", "MiniMax H3 (full reference)",
        "MiniMax H3's six full-reference sections, for prompts that carry "
        + "reference labels for subjects, pictures, video and audio.",
        MINIMAX_REF_CHANNELS,
        {
            fieldSeparator: "\n\n", labelSeparator: ":\n", labels: LABELS_ALWAYS,
            shotMarkerChannel: "detailed_description",
            globalMerge: GLOBAL_MERGE_PER_CHANNEL,
        }),
};

// An unknown id resolves to the default rather than throwing: a project saved
// against a custom template that was later removed must still render.
export function getChannelTemplate(templateId) {
    if (templateId && typeof templateId === "object" && !Array.isArray(templateId)) {
        return normalizeChannelTemplate(templateId);
    }
    const key = String(templateId ?? "") || DEFAULT_CHANNEL_TEMPLATE_ID;
    return PROMPT_CHANNEL_TEMPLATE_PRESETS[key]
        || PROMPT_CHANNEL_TEMPLATE_PRESETS[DEFAULT_CHANNEL_TEMPLATE_ID];
}

export function normalizeChannelTemplate(raw) {
    const fallback = PROMPT_CHANNEL_TEMPLATE_PRESETS[DEFAULT_CHANNEL_TEMPLATE_ID];
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return fallback;

    const channels = [];
    const seen = new Set();
    for (const entry of raw.channels || []) {
        if (!entry || typeof entry !== "object" || Array.isArray(entry)) continue;
        const key = String(entry.key ?? "").trim();
        if (!key || seen.has(key)) continue;
        seen.add(key);
        channels.push(channel(key, String(entry.label ?? ""), String(entry.description ?? "")));
    }
    if (!channels.length) return fallback;

    let labels = String(raw.labels ?? LABELS_PROJECT);
    if (![LABELS_ALWAYS, LABELS_NEVER, LABELS_PROJECT].includes(labels)) labels = LABELS_PROJECT;
    let globalMerge = String(raw.global_merge ?? GLOBAL_MERGE_LEADING);
    if (![GLOBAL_MERGE_LEADING, GLOBAL_MERGE_PER_CHANNEL].includes(globalMerge)) {
        globalMerge = GLOBAL_MERGE_LEADING;
    }
    let shotMarkerChannel = String(raw.shot_marker_channel ?? "");
    if (!seen.has(shotMarkerChannel)) shotMarkerChannel = "";

    return {
        id: String(raw.id ?? "custom"),
        name: String(raw.name ?? "Custom"),
        description: String(raw.description ?? ""),
        channels,
        field_separator: raw.field_separator === undefined ? " " : String(raw.field_separator),
        label_separator: raw.label_separator === undefined ? " " : String(raw.label_separator),
        labels,
        shot_marker_channel: shotMarkerChannel,
        global_merge: globalMerge,
        // Absent means on: a hand-edited or pre-flag template keeps the
        // per-channel global it was authored with.
        global_channels_enabled: raw.global_channels_enabled !== false,
        builtin: false,
    };
}

// Mirror of prompt_channel_templates.template_freeze_value. A built-in freezes
// as its id; a project-owned fork freezes as its whole dict, because the project
// could edit or delete it before a queued job runs.
export function templateFreezeValue(templateOrId) {
    const resolved = (templateOrId && typeof templateOrId === "object" && templateOrId.channels)
        ? templateOrId
        : getChannelTemplate(templateOrId);
    if (resolved.builtin) return resolved.id;
    return {
        id: resolved.id,
        name: resolved.name,
        description: resolved.description || "",
        channels: (resolved.channels || []).map((entry) => ({ ...entry })),
        field_separator: resolved.field_separator ?? " ",
        label_separator: resolved.label_separator ?? " ",
        labels: resolved.labels ?? LABELS_PROJECT,
        shot_marker_channel: resolved.shot_marker_channel ?? "",
        global_merge: resolved.global_merge ?? GLOBAL_MERGE_LEADING,
        global_channels_enabled: globalChannelsEnabled(resolved),
    };
}

export function resolveChannelTemplate(metadata = null, params = null) {
    for (const source of [params, metadata]) {
        if (source && typeof source === "object" && source[PROJECT_TEMPLATE_KEY]) {
            return getChannelTemplate(source[PROJECT_TEMPLATE_KEY]);
        }
    }
    return PROMPT_CHANNEL_TEMPLATE_PRESETS[DEFAULT_CHANNEL_TEMPLATE_ID];
}

export function templateChannelKeys(templateOrId) {
    const resolved = (templateOrId && typeof templateOrId === "object" && templateOrId.channels)
        ? templateOrId
        : getChannelTemplate(templateOrId);
    return (resolved.channels || []).map((entry) => entry.key);
}

// Whether the scene-global prompt is authored per channel. Off means ONE global
// box for the whole scene, emitted ahead of everything.
export function globalChannelsEnabled(templateOrId) {
    const resolved = (templateOrId && typeof templateOrId === "object" && templateOrId.channels)
        ? templateOrId
        : getChannelTemplate(templateOrId);
    return resolved.global_channels_enabled !== false;
}

// A single global box cannot merge per channel — there is no channel to merge it
// into — so disabling global channels forces `leading` whatever the template says.
export function effectiveGlobalMerge(templateOrId) {
    const resolved = (templateOrId && typeof templateOrId === "object" && templateOrId.channels)
        ? templateOrId
        : getChannelTemplate(templateOrId);
    if (!globalChannelsEnabled(resolved)) return GLOBAL_MERGE_LEADING;
    return resolved.global_merge === GLOBAL_MERGE_PER_CHANNEL
        ? GLOBAL_MERGE_PER_CHANNEL
        : GLOBAL_MERGE_LEADING;
}

// The channel keys the scene-global prompt authors under this template: every
// channel when global channels are on, the first alone when they are off — so
// the single box still has a durable home and the flag is a collapse, not a loss.
export function globalChannelKeys(templateOrId) {
    const resolved = (templateOrId && typeof templateOrId === "object" && templateOrId.channels)
        ? templateOrId
        : getChannelTemplate(templateOrId);
    const keys = templateChannelKeys(resolved);
    if (!keys.length) return keys;
    return globalChannelsEnabled(resolved) ? keys : keys.slice(0, 1);
}

export function templateLabelsOn(templateOrId, projectLabelsOn) {
    const resolved = (templateOrId && typeof templateOrId === "object" && templateOrId.channels)
        ? templateOrId
        : getChannelTemplate(templateOrId);
    if (resolved.labels === LABELS_ALWAYS) return true;
    if (resolved.labels === LABELS_NEVER) return false;
    return !!projectLabelsOn;
}

// `MM:SS.mmm` for a window-local frame offset. Minutes are never wrapped into
// hours; milliseconds are rounded from the exact frame/fps ratio.
export function formatShotTimecode(frames, fps) {
    const frameCount = Number(frames);
    const rate = Number(fps);
    if (!Number.isFinite(frameCount) || !Number.isFinite(rate)) return "";
    if (rate <= 0 || frameCount < 0) return "";
    const totalMs = Math.round((frameCount * 1000) / rate);
    const minutes = Math.floor(totalMs / 60000);
    const seconds = Math.floor((totalMs % 60000) / 1000);
    const milliseconds = totalMs % 1000;
    return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
        + `.${String(milliseconds).padStart(3, "0")}`;
}
