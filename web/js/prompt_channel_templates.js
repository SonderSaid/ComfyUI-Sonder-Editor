// Prompt channel templates — mirror of server/prompt_channel_templates.py.
// Keep the preset ids, channel keys, labels, separators and policies in
// lockstep with the backend module; tests/test_prompt_channel_templates_js.py
// asserts that parity.
//
// This module is DISPLAY + authoring metadata for the panel. The backend
// stays authoritative for every model-visible string, exactly as
// prompt_composition.js is a compose-only twin of prompt_payload.py.

export const DEFAULT_CHANNEL_TEMPLATE_ID = "sonder";
// Keep creation policy separate from the legacy/composition fallback above.
export const DEFAULT_NEW_PROJECT_CHANNEL_TEMPLATE_ID = "standard";

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
        "The editor's three-field format: separate visual, speech and sound channels, "
        + "always labelled in composed prompt output.",
        SONDER_CHANNELS, { labels: LABELS_ALWAYS }),
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
function cloneTemplate(value) {
    return {
        ...value,
        channels: (value?.channels || []).map((entry) => ({ ...entry })),
    };
}

export function getChannelTemplate(templateId, catalog = null) {
    if (templateId && typeof templateId === "object" && !Array.isArray(templateId)) {
        if (templateId.builtin === true
            && PROMPT_CHANNEL_TEMPLATE_PRESETS[templateId.id]) {
            return cloneTemplate(PROMPT_CHANNEL_TEMPLATE_PRESETS[templateId.id]);
        }
        return normalizeChannelTemplate(templateId);
    }
    const key = String(templateId ?? "") || DEFAULT_CHANNEL_TEMPLATE_ID;
    const preset = PROMPT_CHANNEL_TEMPLATE_PRESETS[key];
    if (preset) return cloneTemplate(preset);
    const custom = Array.isArray(catalog)
        ? catalog.find((entry) => entry?.id === key)
        : null;
    if (custom) return normalizeChannelTemplate(custom);
    return cloneTemplate(PROMPT_CHANNEL_TEMPLATE_PRESETS[DEFAULT_CHANNEL_TEMPLATE_ID]);
}

export function mergeChannelTemplateCatalog(catalog, activeTemplate) {
    const merged = [];
    const seen = new Set();
    for (const raw of catalog || []) {
        const template = getChannelTemplate(raw);
        if (seen.has(template.id)) continue;
        seen.add(template.id);
        merged.push(template);
    }
    const active = getChannelTemplate(activeTemplate);
    if (!seen.has(active.id)) merged.push(active);
    return merged;
}

export function strictNormalizeChannelTemplate(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    if (!Array.isArray(raw.channels) || !raw.channels.length) return null;

    const channels = [];
    const seen = new Set();
    for (const entry of raw.channels) {
        if (!entry || typeof entry !== "object" || Array.isArray(entry)) return null;
        const key = String(entry.key ?? "").trim();
        if (!key || seen.has(key)) return null;
        seen.add(key);
        channels.push(channel(key, String(entry.label ?? ""), String(entry.description ?? "")));
    }

    let labels = String(raw.labels ?? LABELS_PROJECT);
    if (![LABELS_ALWAYS, LABELS_NEVER, LABELS_PROJECT].includes(labels)) return null;
    let globalMerge = String(raw.global_merge ?? GLOBAL_MERGE_LEADING);
    if (![GLOBAL_MERGE_LEADING, GLOBAL_MERGE_PER_CHANNEL].includes(globalMerge)) {
        return null;
    }
    let shotMarkerChannel = String(raw.shot_marker_channel ?? "");
    if (!seen.has(shotMarkerChannel)) {
        if (shotMarkerChannel) return null;
        shotMarkerChannel = "";
    }

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

export function normalizeChannelTemplate(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
        return cloneTemplate(PROMPT_CHANNEL_TEMPLATE_PRESETS[DEFAULT_CHANNEL_TEMPLATE_ID]);
    }
    const channels = [];
    const seen = new Set();
    for (const entry of raw.channels || []) {
        if (!entry || typeof entry !== "object" || Array.isArray(entry)) continue;
        const key = String(entry.key ?? "").trim();
        if (!key || seen.has(key)) continue;
        seen.add(key);
        channels.push({ ...entry, key });
    }
    let labels = String(raw.labels ?? LABELS_PROJECT);
    if (![LABELS_ALWAYS, LABELS_NEVER, LABELS_PROJECT].includes(labels)) labels = LABELS_PROJECT;
    let globalMerge = String(raw.global_merge ?? GLOBAL_MERGE_LEADING);
    if (![GLOBAL_MERGE_LEADING, GLOBAL_MERGE_PER_CHANNEL].includes(globalMerge)) {
        globalMerge = GLOBAL_MERGE_LEADING;
    }
    let shotMarkerChannel = String(raw.shot_marker_channel ?? "");
    if (!seen.has(shotMarkerChannel)) shotMarkerChannel = "";
    return strictNormalizeChannelTemplate({
        ...raw,
        channels,
        labels,
        global_merge: globalMerge,
        shot_marker_channel: shotMarkerChannel,
    })
        || cloneTemplate(PROMPT_CHANNEL_TEMPLATE_PRESETS[DEFAULT_CHANNEL_TEMPLATE_ID]);
}

function templateDict(resolved) {
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

export function projectTemplateValue(templateOrId, catalog = null) {
    const resolved = (templateOrId && typeof templateOrId === "object" && templateOrId.channels)
        ? templateOrId
        : getChannelTemplate(templateOrId, catalog);
    if (resolved.builtin) return resolved.id;
    return templateDict(resolved);
}

// Queue projection: always whole, including shipped presets. A pending job
// therefore cannot change meaning when a later release edits a preset policy.
export function templateFreezeValue(templateOrId, catalog = null) {
    const resolved = (templateOrId && typeof templateOrId === "object" && templateOrId.channels)
        ? templateOrId
        : getChannelTemplate(templateOrId, catalog);
    return templateDict(resolved);
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

export function channelTemplateKeySetsEqual(left, right, catalog = null) {
    const leftTemplate = (left && typeof left === "object")
        ? left : getChannelTemplate(left, catalog);
    const rightTemplate = (right && typeof right === "object")
        ? right : getChannelTemplate(right, catalog);
    const leftKeys = new Set(templateChannelKeys(leftTemplate));
    const rightKeys = new Set(templateChannelKeys(rightTemplate));
    return leftKeys.size === rightKeys.size
        && [...leftKeys].every((key) => rightKeys.has(key));
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
