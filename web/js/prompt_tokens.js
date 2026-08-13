// Authored prompt tokens store stable ids; provider ordinals are preview-only.

export const PROMPT_TOKEN_KINDS = Object.freeze({
    subject: Object.freeze({ manifestKey: "subjects", label: "Subject" }),
    picture: Object.freeze({ manifestKey: "pictures", label: "Picture" }),
    video: Object.freeze({ manifestKey: "videos", label: "Video" }),
    audio: Object.freeze({ manifestKey: "audios", label: "Audio" }),
});

const TOKEN_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/;
const TOKEN_PATTERN = /@(subject|picture|video|audio)\(([A-Za-z0-9][A-Za-z0-9._:-]*)\)/g;

export function promptToken(kind, sourceId) {
    const normalizedKind = String(kind || "").toLowerCase();
    const normalizedId = String(sourceId || "").trim();
    if (!PROMPT_TOKEN_KINDS[normalizedKind] || !TOKEN_ID.test(normalizedId)) return "";
    return `@${normalizedKind}(${normalizedId})`;
}

function unitLabelOrdinal(kind, sourceId, unitSourceLabels = {}) {
    const label = PROMPT_TOKEN_KINDS[kind]?.label;
    const pattern = new RegExp(`^<${label} (\\d+)>$`);
    const ordinals = [...new Set((unitSourceLabels?.[sourceId] || []).map((value) => {
        const match = String(value).match(pattern);
        return match ? Number(match[1]) : 0;
    }).filter((value) => value > 0))].sort((a, b) => a - b);
    return ordinals.length === 1 ? ordinals[0] : 0;
}

export function resolvePromptTokens(text, ordinalManifest = {}, unitSourceLabels = {}) {
    const unresolvedIds = [];
    const resolvedText = String(text || "").replace(TOKEN_PATTERN, (original, kind, sourceId) => {
        const declaration = PROMPT_TOKEN_KINDS[kind];
        let ordinal = Number(ordinalManifest?.[declaration.manifestKey]?.[sourceId] || 0);
        if (!ordinal && kind !== "subject") {
            ordinal = unitLabelOrdinal(kind, sourceId, unitSourceLabels);
        }
        if (!Number.isInteger(ordinal) || ordinal <= 0) {
            if (!unresolvedIds.includes(sourceId)) unresolvedIds.push(sourceId);
            return original;
        }
        return `<${declaration.label} ${ordinal}>`;
    });
    return { resolvedText, unresolvedIds };
}
