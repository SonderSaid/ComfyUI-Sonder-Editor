import {
    normalizePromptAttachments,
    normalizePromptDocument,
} from "./prompt_context_chips.js";
import {
    normalizeChannels,
    normalizeChannelExceptions,
} from "./prompt_composition.js";

// The two operations that mint a Prompt Identity. The speaking-only create
// serves Vocal Event "Other speaker…"; the one with sources serves the Attach
// dialog's "New identity…", which carries every field of the identity editor.
export const PROMPT_IDENTITY_CREATE_TYPES = Object.freeze(new Set([
    "create_prompt_semantic_unit",
    "create_prompt_identity_with_sources",
]));

/** The scene operation one pending create intent sends. */
export function promptIdentityCreateOperation(intent) {
    const type = PROMPT_IDENTITY_CREATE_TYPES.has(intent?.type)
        ? intent.type : "create_prompt_semantic_unit";
    return {
        type,
        handle_suggestion: intent?.handle_suggestion,
        unit: structuredClone(intent?.unit || {}),
    };
}

const canonicalIdentityValue = (value) => {
    if (Array.isArray(value)) return value.map(canonicalIdentityValue);
    if (!value || typeof value !== "object") return value;
    return Object.fromEntries(Object.keys(value).sort().map((key) => [
        key, canonicalIdentityValue(value[key]),
    ]));
};

const sameIdentityField = (left, right) =>
    JSON.stringify(canonicalIdentityValue(left)) === JSON.stringify(canonicalIdentityValue(right));

const identitySources = (value) => (Array.isArray(value) ? value : [])
    .filter((source) => source && typeof source === "object")
    .map((source) => ({ entity_id: String(source.entity_id || ""),
        member_id: String(source.member_id || ""),
        contribution: String(source.contribution || "") }));

export function promptIdentityMatchesCreateIntent(unit, intent) {
    if (!unit || !intent?.unit) return false;
    const expectedSnapshot = intent.cleanup_expected || intent.reconciled_expected;
    if (expectedSnapshot) return sameIdentityField(unit, expectedSnapshot);
    // A replay is the same create only when every authored field the intent
    // decided matches, sources included; the server owns the handle and order.
    // An intent that never set a field asserts it empty, as the speaking-only
    // create does for everything past its definition.
    const wanted = intent.unit;
    return String(unit.semantic_unit_id || "") === String(wanted.semantic_unit_id || "")
        && String(unit.name || "") === String(wanted.name || "")
        && String(unit.kind || "subject") === String(wanted.kind || "subject")
        && String(unit.definition || "") === String(wanted.definition || "")
        && Boolean(String(unit.handle || ""))
        && Number.isFinite(Number(unit.order))
        && sameIdentityField(identitySources(unit.sources), identitySources(wanted.sources))
        && sameIdentityField(unit.attachment_defaults || {}, wanted.attachment_defaults || {})
        && sameIdentityField([...new Set((unit.disabled_capabilities || []).map(String))].sort(),
            [...new Set((wanted.disabled_capabilities || []).map(String))].sort())
        && String(unit.visual_intent || "") === String(wanted.visual_intent || "")
        && String(unit.audio_intent || "") === String(wanted.audio_intent || "");
}

export function promptIdentityCleanupPlan(intents = []) {
    const operations = [];
    const retainedUnprovenIds = [];
    for (const intent of intents || []) {
        const unitId = String(intent?.unit?.semantic_unit_id || "");
        const expected = intent?.cleanup_expected;
        if (unitId && expected && typeof expected === "object"
                && String(expected.semantic_unit_id || "") === unitId) {
            operations.push({
                type: "delete_prompt_semantic_unit_if_unreferenced",
                semantic_unit_id: unitId,
                expected: structuredClone(expected),
            });
        } else if (unitId) {
            retainedUnprovenIds.push(unitId);
        }
    }
    return { operations, retainedUnprovenIds };
}

function canonicalPromptSection(section) {
    const channels = normalizeChannels(section?.channels, section?.prompt);
    const sortedObject = (value) => Object.fromEntries(
        Object.entries(value || {}).sort(([left], [right]) =>
            left < right ? -1 : left > right ? 1 : 0));
    return {
        prompt_id: String(section?.prompt_id || ""),
        start_frame: Number(section?.start_frame || 0),
        end_frame: Number(section?.end_frame || 0),
        channels: sortedObject(channels),
        channel_docs: sortedObject(Object.fromEntries(Object.entries(
            section?.channel_docs || {}).map(([key, value]) => [
                key, normalizePromptDocument(value, channels[key] || ""),
            ]))),
        attachments: normalizePromptAttachments(section?.attachments),
        muted: section?.muted === true,
        global_channel_exceptions:
            normalizeChannelExceptions(section?.global_channel_exceptions),
    };
}

export function promptSectionsMatchApply(expected, actual) {
    return JSON.stringify((expected || []).map(canonicalPromptSection))
        === JSON.stringify((actual || []).map(canonicalPromptSection));
}

export function reconcilePromptIdentityCreateOutcome({
    units = [], intents = [], expectedSections = [], actualSections = [],
} = {}) {
    const byId = new Map((units || []).map((unit) => [
        String(unit?.semantic_unit_id || ""), unit,
    ]));
    const adopted = [];
    const conflicts = [];
    for (const intent of intents || []) {
        const id = String(intent?.unit?.semantic_unit_id || "");
        const current = byId.get(id);
        if (!current) continue;
        if (promptIdentityMatchesCreateIntent(current, intent)) adopted.push(id);
        else conflicts.push(id);
    }
    const sectionsMatch = promptSectionsMatchApply(expectedSections, actualSections);
    return {
        applied: !conflicts.length && adopted.length === (intents || []).length
            && sectionsMatch,
        adopted_prompt_semantic_unit_ids: adopted,
        conflicting_prompt_semantic_unit_ids: conflicts,
        sections_match: sectionsMatch,
    };
}

export function promptIdentityRedoPlan(units = [], intents = []) {
    const byId = new Map((units || []).map((unit) => [
        String(unit?.semantic_unit_id || ""), unit,
    ]));
    const createIntents = [];
    const reusedIds = [];
    const conflictIds = [];
    for (const intent of intents || []) {
        const id = String(intent?.unit?.semantic_unit_id || "");
        const current = byId.get(id);
        if (!current) createIntents.push(intent);
        else if (promptIdentityMatchesCreateIntent(current, intent)) reusedIds.push(id);
        else conflictIds.push(id);
    }
    return { createIntents, reusedIds, conflictIds };
}
