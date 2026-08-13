// Prompt composition mirror — compose-only DISPLAY twin of
// server/prompt_payload.py. Keep the channel normalizer and join rules in
// lockstep with the backend module; the channel SET itself now comes from
// prompt_channel_templates.js. Gap-fill / window resolution (resolve_segments)
// AND the frozen queue-job `prompt` are server-only — the frontend uses this
// module for optimistic display values and editor UI text only, never as a
// source of truth for model-visible strings.

import {
    getChannelTemplate,
    globalChannelKeys,
    templateChannelKeys,
    templateLabelsOn,
} from "./prompt_channel_templates.js";

// Legacy three-channel set. Still the fallback key set for any call site that
// has no template in hand, which is what keeps un-updated callers working.
export const CHANNEL_ORDER = Object.freeze(["visual", "speech", "sounds"]);

export const CHANNEL_LABELS = Object.freeze({
    visual: "[VISUAL]:",
    speech: "[SPEECH]:",
    sounds: "[SOUNDS]:",
});

// Mirrors prompt_payload.normalize_channels: a present dict wins, missing
// template keys are filled with "", and keys the template does NOT name are
// PRESERVED rather than dropped. That preservation is what stops every call
// site without template access from becoming a silent truncation.
export function normalizeChannels(raw = null, legacyPrompt = "", keys = null) {
    const templateKeys = keys ? [...keys] : [...CHANNEL_ORDER];
    if (raw && typeof raw === "object" && !Array.isArray(raw)) {
        const out = {};
        for (const key of templateKeys) out[key] = String(raw[key] ?? "");
        for (const [key, value] of Object.entries(raw)) {
            if (!(key in out)) out[key] = String(value ?? "");
        }
        return out;
    }
    const out = {};
    for (const key of templateKeys) out[key] = "";
    if (templateKeys.length) out[templateKeys[0]] = String(legacyPrompt ?? "");
    return out;
}

export function composeSectionText(channels, labelsOn = true, template = null) {
    if (!channels || typeof channels !== "object") return "";
    const resolved = getChannelTemplate(template);
    const labelSeparator = resolved.label_separator ?? " ";
    const parts = [];
    for (const entry of resolved.channels || []) {
        const text = String(channels[entry.key] ?? "").trim();
        if (!text) continue;
        parts.push(labelsOn && entry.label ? `${entry.label}${labelSeparator}${text}` : text);
    }
    return parts.join(resolved.field_separator ?? " ");
}

/**
 * One display line per GLOBAL channel carrying text, in template order.
 *
 * Deliberately NOT `composeSectionText`, which takes no key set and iterates
 * every channel the template names. The scene-global prompt is authored over
 * `globalChannelKeys`, and that collapses to the FIRST key alone when the
 * template turns per-channel globals off — while stored `global_channels` still
 * holds whatever channels 2..n held before the flag flipped, because
 * `normalizeChannels` preserves them on purpose. Composing the whole template
 * would therefore show text on screen that never reaches the model, which is
 * the exact class of lie the global lane label already tells by reading the
 * legacy `Scene.prompt` mirror.
 *
 * Mirrors the backend derivation in `prompt_payload.compose_range_prompt`,
 * which joins the same keys with a single space.
 */
export function globalChannelLines(globalChannels, template = null, labelsOn = false) {
    const resolved = getChannelTemplate(template);
    const showLabels = templateLabelsOn(resolved, labelsOn);
    const separator = resolved.label_separator ?? " ";
    const labels = new Map((resolved.channels || []).map((entry) => [entry.key, entry.label]));
    const lines = [];
    for (const key of globalChannelKeys(resolved)) {
        const text = String((globalChannels || {})[key] ?? "").trim();
        if (!text) continue;
        const label = labels.get(key);
        lines.push(showLabels && label ? `${label}${separator}${text}` : text);
    }
    return lines;
}

// Display twin of the backend's compose_range_prompt section part: labels ON
// groups by channel (one label per channel, segment texts joined in temporal
// order); labels OFF is plain temporal concatenation. Sections must already
// be the window-overlapping set in temporal order.
export function composeSectionsDisplayText(sections, labelsOn = true, delimiter = ".",
                                           template = null) {
    const resolved = getChannelTemplate(template);
    const effectiveLabels = templateLabelsOn(resolved, labelsOn);
    const keys = templateChannelKeys(resolved);
    const seam = String(delimiter ?? "").trim();
    const joiner = seam ? `${seam} ` : " ";
    const list = (sections || [])
        .filter((s) => !s?.muted)
        .map((s) => normalizeChannels(s?.channels, s?.prompt, keys));
    if (effectiveLabels) {
        const labelSeparator = resolved.label_separator ?? " ";
        const parts = [];
        for (const entry of resolved.channels || []) {
            const texts = list.map((c) => String(c[entry.key] ?? "").trim()).filter(Boolean);
            if (!texts.length) continue;
            const body = texts.join(joiner);
            parts.push(entry.label ? `${entry.label}${labelSeparator}${body}` : body);
        }
        return parts.join(resolved.field_separator ?? " ");
    }
    return list
        .map((channels) => composeSectionText(channels, false, resolved))
        .filter(Boolean)
        .join(joiner);
}

export function composeWindowPrompt(globalText, channelsOrNull, labelsOn = true,
                                    template = null) {
    const parts = [];
    const globalPart = String(globalText ?? "").trim();
    if (globalPart) parts.push(globalPart);
    if (channelsOrNull) {
        const sectionPart = composeSectionText(channelsOrNull, labelsOn, template);
        if (sectionPart) parts.push(sectionPart);
    }
    return parts.join(" ");
}

const CHANNEL_HEADER_RE = /^([A-Za-z0-9_]+):[ \t]?(.*)$/;

// Mirrors prompt_payload.join_channel_headers. Template channels first in
// template order, then any other non-empty key, so text belonging to a third
// template is carried rather than dropped across an A -> B -> C switch.
// A template holding exactly ONE channel emits no header: there is nothing to
// tell apart, and that channel's text is itself a collapsed document whenever
// it came from a previous narrowing — re-labelling it would nest one document
// inside another and strand a bare `visual:` line on the way back out.
export function joinChannelHeaders(channels, template = null) {
    const resolved = getChannelTemplate(template);
    const templateKeys = templateChannelKeys(resolved);
    const keys = [...templateKeys];
    const source = (channels && typeof channels === "object") ? channels : {};
    for (const key of Object.keys(source)) if (!keys.includes(key)) keys.push(key);
    const populated = keys
        .map((key) => [key, String(source[key] ?? "").trim()])
        .filter(([, text]) => text);
    if (populated.length === 1 && templateKeys.length === 1) return populated[0][1];
    return populated.map(([key, text]) => `${key}:\n${text}`).join("\n\n");
}

// Mirrors prompt_payload.split_channel_headers. The parse is ANCHORED: a header
// is a line starting with a key of THIS template followed by a colon. A MiniMax
// description legitimately contains `says: <d>[English] ...</d>`, and a looser
// "word followed by a colon" rule would shred it. Text before the first
// recognised header, and any header this template does not know, stays in the
// first channel with its label intact.
export function splitChannelHeaders(text, template = null) {
    const resolved = getChannelTemplate(template);
    const keys = templateChannelKeys(resolved);
    if (!keys.length) return {};
    const known = new Set(keys);
    const buffers = new Map(keys.map((key) => [key, []]));
    let current = keys[0];
    for (const line of String(text ?? "").split("\n")) {
        const match = CHANNEL_HEADER_RE.exec(line);
        if (match && known.has(match[1])) {
            current = match[1];
            if (match[2]) buffers.get(current).push(match[2]);
            continue;
        }
        buffers.get(current).push(line);
    }
    const out = {};
    for (const [key, lines] of buffers) out[key] = lines.join("\n").trim();
    return out;
}

// Mirrors prompt_payload.collapse_channels_for_template. Every outgoing key is
// emitted as an explicit empty string because channel updates MERGE — a key
// left out would keep its old value and the collapsed copy would appear twice.
export function collapseChannelsForTemplate(channels, fromTemplate = null, toTemplate = null) {
    const collapsed = joinChannelHeaders(channels, fromTemplate);
    const patch = {};
    for (const key of templateChannelKeys(getChannelTemplate(fromTemplate))) patch[key] = "";
    if (channels && typeof channels === "object") {
        for (const key of Object.keys(channels)) patch[key] = "";
    }
    return { ...patch, ...splitChannelHeaders(collapsed, toTemplate) };
}

// Mirror of prompt_payload.normalize_channel_exceptions. Sorted rather than
// authored-order: this is a SET of channel keys, so two sections that opted out
// of the same channels must compare equal whatever order they were clicked in.
export function normalizeChannelExceptions(raw) {
    const keys = new Set();
    for (const entry of raw || []) {
        const key = String(entry ?? "").trim();
        if (key) keys.add(key);
    }
    return [...keys].sort();
}

// Default ON: absence from the exceptions set means inherit, which is what keeps
// a channel added to the template later inherited without touching a section.
export function sectionInheritsGlobal(section, key) {
    return !normalizeChannelExceptions(section?.global_channel_exceptions)
        .includes(String(key));
}

// True when any channel the template names carries text. The three-channel
// `visual || speech || sounds` checks it replaces would read as "empty" for a
// section whose text lives only in channels 4-6.
export function hasChannelText(channels, template = null) {
    if (!channels || typeof channels !== "object") return false;
    return templateChannelKeys(getChannelTemplate(template))
        .some((key) => String(channels[key] ?? "").trim());
}
