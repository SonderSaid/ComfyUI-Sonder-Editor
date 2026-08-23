// Authored prompt tokens store stable ids; provider ordinals are preview-only.

export const PROMPT_TOKEN_KINDS = Object.freeze({
    subject: Object.freeze({ manifestKey: "subjects", labelTemplate: "<Subject {n}>", physical: false }),
    picture: Object.freeze({ manifestKey: "pictures", labelTemplate: "<Picture {n}>", physical: true }),
    video: Object.freeze({ manifestKey: "videos", labelTemplate: "<Video {n}>", physical: true }),
    audio: Object.freeze({ manifestKey: "audios", labelTemplate: "<Audio {n}>", physical: true }),
});

// Deliberate mirrors of `prompt_context.SHOT_ORDINAL_KEY` and
// `prompt_payload.SHOT_LABEL_TEMPLATE`, held in parity by
// tests/test_prompt_tokens.py. Shot is compiler-owned ordering rather than an
// identity or a physical population, so it enters the grammar from its own
// capability declaration on both sides.
const SHOT_ORDINAL_KEY = "shots";
const SHOT_LABEL_TEMPLATE = "[Shot {n}]";

const TOKEN_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/;
const TOKEN_PATTERN = /@([a-z][a-z0-9_]{0,63})\(([A-Za-z0-9][A-Za-z0-9._:-]*)\)/g;

// Mirror of `prompt_tokens._HANDLE_RE`, held in parity by
// tests/test_prompt_tokens.py. The comment there carries the reasoning for
// both guards and for the absence of a dotted qualifier; keep the two literals
// identical rather than "equivalent".
const HANDLE_PATTERN = /(?<![A-Za-z0-9_])@([A-Za-z][A-Za-z0-9_]{0,63})(?![A-Za-z0-9_(])/g;

/** Every `@handle` run in authored prose, with its span, in source order. */
export function promptHandleMentions(text) {
    const value = String(text ?? "");
    const out = [];
    HANDLE_PATTERN.lastIndex = 0;
    for (let match = HANDLE_PATTERN.exec(value); match;
        match = HANDLE_PATTERN.exec(value)) {
        out.push({ handle: match[1], start: match.index,
            end: match.index + match[0].length });
    }
    return out;
}

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
    const shotKind = String(profile?.capabilities?.shot?.token_kind || "").toLowerCase();
    if (shotKind) {
        result[shotKind] = {
            manifestKey: SHOT_ORDINAL_KEY,
            labelTemplate: SHOT_LABEL_TEMPLATE,
            physical: false,
        };
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
