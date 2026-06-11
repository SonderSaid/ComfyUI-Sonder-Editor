// Prompt composition mirror — compose-only DISPLAY twin of
// server/prompt_payload.py. Keep CHANNEL_ORDER, label constants, and join
// rules in lockstep with the backend module. Gap-fill / window resolution
// (resolve_segments) AND the frozen queue-job `prompt` are server-only — the
// frontend uses this module for optimistic display values and editor UI text
// only, never as a source of truth for model-visible strings.

export const CHANNEL_ORDER = Object.freeze(["visual", "speech", "sounds"]);

export const CHANNEL_LABELS = Object.freeze({
    visual: "[VISUAL]:",
    speech: "[SPEECH]:",
    sounds: "[SOUNDS]:",
});

export function normalizeChannels(raw = null, legacyPrompt = "") {
    if (raw && typeof raw === "object" && !Array.isArray(raw)) {
        const out = {};
        for (const key of CHANNEL_ORDER) out[key] = String(raw[key] ?? "");
        return out;
    }
    const out = { visual: String(legacyPrompt ?? ""), speech: "", sounds: "" };
    return out;
}

export function composeSectionText(channels, labelsOn = true) {
    if (!channels || typeof channels !== "object") return "";
    const parts = [];
    for (const key of CHANNEL_ORDER) {
        const text = String(channels[key] ?? "").trim();
        if (!text) continue;
        parts.push(labelsOn ? `${CHANNEL_LABELS[key]} ${text}` : text);
    }
    return parts.join(" ");
}

// Display twin of the backend's compose_range_prompt section part: labels ON
// groups by channel (one label per channel, segment texts joined in temporal
// order); labels OFF is plain temporal concatenation. Sections must already
// be the window-overlapping set in temporal order.
export function composeSectionsDisplayText(sections, labelsOn = true, delimiter = ".") {
    const seam = String(delimiter ?? "").trim();
    const joiner = seam ? `${seam} ` : " ";
    const list = (sections || []).map((s) => normalizeChannels(s?.channels, s?.prompt));
    if (labelsOn) {
        const parts = [];
        for (const key of CHANNEL_ORDER) {
            const texts = list.map((c) => String(c[key] ?? "").trim()).filter(Boolean);
            if (texts.length) parts.push(`${CHANNEL_LABELS[key]} ${texts.join(joiner)}`);
        }
        return parts.join(" ");
    }
    return list
        .map((channels) => composeSectionText(channels, false))
        .filter(Boolean)
        .join(joiner);
}

export function composeWindowPrompt(globalText, channelsOrNull, labelsOn = true) {
    const parts = [];
    const globalPart = String(globalText ?? "").trim();
    if (globalPart) parts.push(globalPart);
    if (channelsOrNull) {
        const sectionPart = composeSectionText(channelsOrNull, labelsOn);
        if (sectionPart) parts.push(sectionPart);
    }
    return parts.join(" ");
}
