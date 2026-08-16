// Authored prompt tokens store stable ids; provider ordinals are preview-only.

export const PROMPT_TOKEN_KINDS = Object.freeze({
    subject: Object.freeze({ manifestKey: "subjects", labelTemplate: "<Subject {n}>", physical: false }),
    picture: Object.freeze({ manifestKey: "pictures", labelTemplate: "<Picture {n}>", physical: true }),
    video: Object.freeze({ manifestKey: "videos", labelTemplate: "<Video {n}>", physical: true }),
    audio: Object.freeze({ manifestKey: "audios", labelTemplate: "<Audio {n}>", physical: true }),
});

const TOKEN_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/;
const TOKEN_PATTERN = /@([a-z][a-z0-9_]{0,63})\(([A-Za-z0-9][A-Za-z0-9._:-]*)\)/g;

function tokenDeclarations(raw) {
    if (raw == null) return PROMPT_TOKEN_KINDS;
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
    return Object.fromEntries(Object.entries(raw).filter(([kind, value]) =>
        typeof kind === "string" && value && typeof value === "object" && !Array.isArray(value)));
}

export function promptTokenDeclarationsFromProfile(profile = {}) {
    const result = {};
    for (const declaration of profile?.identity_kinds || []) {
        const kind = String(declaration?.token_kind || declaration?.key || "").toLowerCase();
        const manifestKey = String(declaration?.ordinal_key || `${declaration?.key || kind}s`);
        const labelTemplate = String(declaration?.referenced_label_template || "");
        if (!kind || !manifestKey || !labelTemplate) continue;
        result[kind] = { manifestKey, labelTemplate, physical: false };
    }
    for (const declaration of profile?.physical_populations || []) {
        const kind = String(declaration?.token_kind || "").toLowerCase();
        const manifestKey = String(declaration?.ordinal_key || "");
        const labelTemplate = String(declaration?.label_template || "");
        if (!kind || !manifestKey || !labelTemplate) continue;
        result[kind] = { manifestKey, labelTemplate, physical: true };
    }
    return result;
}

export function promptToken(kind, sourceId, declarations = null) {
    const normalizedKind = String(kind || "").toLowerCase();
    const normalizedId = String(sourceId || "").trim();
    if (!tokenDeclarations(declarations)[normalizedKind] || !TOKEN_ID.test(normalizedId)) return "";
    return `@${normalizedKind}(${normalizedId})`;
}

function unitLabelOrdinal(declaration, sourceId, unitSourceLabels = {}) {
    const template = String(declaration?.labelTemplate || "");
    const marker = template.indexOf("{n}");
    if (marker < 0) return 0;
    const prefix = template.slice(0, marker);
    const suffix = template.slice(marker + 3);
    const ordinals = [...new Set((unitSourceLabels?.[sourceId] || []).map((value) => {
        const text = String(value);
        if (!prefix || !text.startsWith(prefix) || !text.endsWith(suffix)) return 0;
        const end = suffix ? text.length - suffix.length : text.length;
        const number = text.slice(prefix.length, end);
        return /^\d+$/.test(number) ? Number(number) : 0;
    }).filter((value) => value > 0))].sort((a, b) => a - b);
    return ordinals.length === 1 ? ordinals[0] : 0;
}

export function resolvePromptTokens(text, ordinalManifest = {}, unitSourceLabels = {}, declarations = null) {
    const declared = tokenDeclarations(declarations);
    const unresolvedIds = [];
    const resolvedText = String(text || "").replace(TOKEN_PATTERN, (original, kind, sourceId) => {
        const declaration = declared[kind];
        if (!declaration) return original;
        let ordinal = Number(ordinalManifest?.[declaration.manifestKey]?.[sourceId] || 0);
        if (!ordinal && declaration.physical === true) {
            ordinal = unitLabelOrdinal(declaration, sourceId, unitSourceLabels);
        }
        if (!Number.isInteger(ordinal) || ordinal <= 0) {
            if (!unresolvedIds.includes(sourceId)) unresolvedIds.push(sourceId);
            return original;
        }
        return String(declaration.labelTemplate || "").replace("{n}", String(ordinal));
    });
    return { resolvedText, unresolvedIds };
}
