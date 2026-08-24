import {
    normalizePromptAttachments,
    normalizePromptDocument,
} from "./prompt_context_chips.js";
import {
    normalizeChannels,
    normalizeChannelExceptions,
} from "./prompt_composition.js";

export function promptIdentityMatchesCreateIntent(unit, intent) {
    if (!unit || !intent?.unit) return false;
    const expectedSnapshot = intent.cleanup_expected || intent.reconciled_expected;
    if (expectedSnapshot) {
        const canonical = (value) => {
            if (Array.isArray(value)) return value.map(canonical);
            if (!value || typeof value !== "object") return value;
            return Object.fromEntries(Object.keys(value).sort().map((key) => [
                key, canonical(value[key]),
            ]));
        };
        return JSON.stringify(canonical(unit))
            === JSON.stringify(canonical(expectedSnapshot));
    }
    const attachmentDefaults = unit.attachment_defaults;
    const disabledCapabilities = unit.disabled_capabilities;
    return String(unit.semantic_unit_id || "")
            === String(intent.unit.semantic_unit_id || "")
        && String(unit.name || "") === String(intent.unit.name || "")
        && String(unit.kind || "subject") === String(intent.unit.kind || "subject")
        && String(unit.definition || "") === String(intent.unit.definition || "")
        && Boolean(String(unit.handle || ""))
        && Number.isFinite(Number(unit.order))
        && !(unit.sources || []).length
        && (!attachmentDefaults || !Object.keys(attachmentDefaults).length)
        && (!disabledCapabilities || !disabledCapabilities.length)
        && !String(unit.visual_intent || "")
        && !String(unit.audio_intent || "");
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
