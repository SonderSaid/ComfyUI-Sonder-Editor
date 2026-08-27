/** Ephemeral authoring intents: stable row identity, field ownership, no disk state. */
const clone = (value) => structuredClone(value);
const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);

export function promptEditFields(before, after, { global = false } = {}) {
    const docsKey = global ? "global_channel_docs" : "channel_docs";
    const attachmentsKey = global ? "global_attachments" : "attachments";
    const channelsKey = global ? "global_channels" : "channels";
    const fields = clone(after);
    const edit = { documents: {}, attachments: {} };
    if (Object.hasOwn(after, docsKey)) {
        for (const key of new Set([...Object.keys(before[docsKey] || {}), ...Object.keys(after[docsKey] || {})])) {
            const expected = before[docsKey]?.[key] ?? null;
            const value = after[docsKey]?.[key] ?? null;
            if (!equal(expected, value)) edit.documents[key] = { expected: clone(expected), value: clone(value) };
        }
        delete fields[docsKey];
        delete fields[channelsKey]; // derived mirrors cannot overwrite sibling channels
    }
    if (Object.hasOwn(after, attachmentsKey)) {
        const previous = new Map((before[attachmentsKey] || []).map((row) => [row.attachment_id, row]));
        const next = new Map((after[attachmentsKey] || []).map((row) => [row.attachment_id, row]));
        for (const key of new Set([...previous.keys(), ...next.keys()])) {
            const expected = previous.get(key) ?? null;
            const value = next.get(key) ?? null;
            if (!equal(expected, value)) edit.attachments[key] = { expected: clone(expected), value: clone(value) };
        }
        delete fields[attachmentsKey];
    }
    if (Object.keys(edit.documents).length || Object.keys(edit.attachments).length) fields.prompt_edit = edit;
    return fields;
}

export function promptDraftKey(projectId, sceneId, promptId = "global") {
    return JSON.stringify([projectId, sceneId, promptId]);
}

function draftGlobal(row, explicit = null) {
    if (explicit !== null) return Boolean(explicit);
    return Object.hasOwn(row?.value || {}, "global_channel_docs")
        || Object.hasOwn(row?.value || {}, "global_attachments");
}

function promptDraftContent(row, global) {
    const keys = global
        ? ["global_channels", "global_channel_docs", "global_attachments"]
        : ["channels", "channel_docs", "attachments"];
    return Object.fromEntries(keys.filter((key) => Object.hasOwn(row.value || {}, key))
        .map((key) => [key, clone(row.value[key])]));
}

export function promptDraftHasChanges(row, { global = null } = {}) {
    if (!row) return false;
    const isGlobal = draftGlobal(row, global);
    return Object.keys(promptEditFields(row.base || {}, promptDraftContent(row, isGlobal), {
        global: isGlobal,
    })).length > 0;
}

export function updatePromptDraft(drafts, key, base, value) {
    const prior = drafts.get(key);
    const row = prior || { base: clone(base), revision: 0, pending: null, error: "", conflict: null };
    if (!equal(row.value, value)) row.revision += 1;
    row.value = clone(value);
    drafts.set(key, row);
    const ownsPromptRecords = ["channels", "channel_docs", "attachments",
        "global_channels", "global_channel_docs", "global_attachments"]
        .some((field) => Object.hasOwn(row.value || {}, field));
    if (ownsPromptRecords && !row.pending && !promptDraftHasChanges(row)) drafts.delete(key);
    return row;
}

export function dismissPromptDraftError(drafts, key) {
    const row = drafts.get(key);
    if (!row) return false;
    row.error = "";
    row.conflict = null;
    return true;
}

export function resolvePromptDraftRecord(drafts, key) {
    const row = drafts.get(key);
    const conflict = row?.conflict;
    if (!row || !conflict || !["document", "attachment"].includes(conflict.record_kind))
        return false;
    const global = draftGlobal(row);
    const recordsKey = conflict.record_kind === "document"
        ? (global ? "global_channel_docs" : "channel_docs")
        : (global ? "global_attachments" : "attachments");
    const before = clone(row.value);
    if (conflict.record_kind === "document") {
        for (const target of [row.base, row.value]) {
            target[recordsKey] ||= {};
            if (conflict.current == null) delete target[recordsKey][conflict.record_key];
            else target[recordsKey][conflict.record_key] = clone(conflict.current);
        }
    } else {
        for (const target of [row.base, row.value]) {
            const records = new Map((target[recordsKey] || [])
                .map((value) => [value.attachment_id, clone(value)]));
            if (conflict.current == null) records.delete(conflict.record_key);
            else records.set(conflict.record_key, clone(conflict.current));
            target[recordsKey] = [...records.values()];
        }
    }
    if (!equal(before, row.value)) row.revision += 1;
    row.error = "";
    row.conflict = null;
    return true;
}

export async function retryPromptDraft(drafts, key) {
    const row = drafts.get(key);
    if (!row || typeof row.retry !== "function") return false;
    return await row.retry();
}

function mergeObjectRecords(current, compareTo, adoptFrom) {
    const result = clone(current || {});
    for (const key of new Set([
        ...Object.keys(compareTo || {}), ...Object.keys(adoptFrom || {}),
        ...Object.keys(current || {}),
    ])) {
        if (!equal(current?.[key], compareTo?.[key])) continue;
        if (Object.hasOwn(adoptFrom || {}, key)) result[key] = clone(adoptFrom[key]);
        else delete result[key];
    }
    return result;
}

function mergeAttachmentRecords(current, compareTo, adoptFrom) {
    const toMap = (rows) => new Map((rows || []).map((row) => [row.attachment_id, row]));
    const currentMap = toMap(current), compareMap = toMap(compareTo), adoptMap = toMap(adoptFrom);
    const result = new Map([...currentMap].map(([key, value]) => [key, clone(value)]));
    for (const key of new Set([...compareMap.keys(), ...adoptMap.keys(), ...currentMap.keys()])) {
        if (!equal(currentMap.get(key), compareMap.get(key))) continue;
        if (adoptMap.has(key)) result.set(key, clone(adoptMap.get(key)));
        else result.delete(key);
    }
    return [...result.values()];
}

// One merge rule serves both refresh and save acknowledgement. Only records
// still equal to the comparison snapshot adopt server state; newer local intent
// remains over the newly acknowledged baseline.
function mergePromptRecords(row, compareTo, adoptFrom, { global = false } = {}) {
    const docsKey = global ? "global_channel_docs" : "channel_docs";
    const channelsKey = global ? "global_channels" : "channels";
    const attachmentsKey = global ? "global_attachments" : "attachments";
    const previousValue = clone(row.value || {});
    const nextBase = clone(row.base || {});
    for (const key of [docsKey, channelsKey]) {
        if (!Object.hasOwn(previousValue, key)) continue;
        previousValue[key] = mergeObjectRecords(
            previousValue[key], compareTo?.[key], adoptFrom?.[key]);
        if (Object.hasOwn(adoptFrom || {}, key)) nextBase[key] = clone(adoptFrom[key]);
        else delete nextBase[key];
    }
    if (Object.hasOwn(previousValue, attachmentsKey)) {
        previousValue[attachmentsKey] = mergeAttachmentRecords(
            previousValue[attachmentsKey], compareTo?.[attachmentsKey], adoptFrom?.[attachmentsKey]);
        if (Object.hasOwn(adoptFrom || {}, attachmentsKey))
            nextBase[attachmentsKey] = clone(adoptFrom[attachmentsKey]);
        else delete nextBase[attachmentsKey];
    }
    const recordKeys = new Set([docsKey, channelsKey, attachmentsKey]);
    for (const key of Object.keys(previousValue)) {
        if (recordKeys.has(key) || !equal(previousValue[key], compareTo?.[key])) continue;
        if (Object.hasOwn(adoptFrom || {}, key)) previousValue[key] = clone(adoptFrom[key]);
        else delete previousValue[key];
    }
    for (const key of Object.keys(nextBase)) {
        if (recordKeys.has(key) || !Object.hasOwn(previousValue, key)) continue;
        if (Object.hasOwn(adoptFrom || {}, key)) nextBase[key] = clone(adoptFrom[key]);
    }
    row.base = nextBase;
    row.value = previousValue;
    return row;
}

function normalizedSaveResult(result) {
    if (result === true) return { status: "acknowledged" };
    if (result === false || result == null) return { status: "refused" };
    if (typeof result !== "object") return { status: "refused" };
    return result;
}

// The row stays authoritative until its OWN save acknowledges its revision.
// Later keystrokes and refused saves must survive both refresh and scene switches.
export async function savePromptDraft(drafts, key, save, {
    global = null, onAcknowledge = null,
} = {}) {
    const row = drafts.get(key);
    if (!row) return true;
    row.retry = () => savePromptDraft(drafts, key, save, { global, onAcknowledge });
    if (row.pending) { row.saveAgain = true; return row.pending; }
    row.pending = (async () => {
        let ok = false;
        do {
            row.saveAgain = false;
            const value = clone(row.value);
            const base = clone(row.base);
            try {
                const outcome = normalizedSaveResult(await save(value, base));
                if (outcome.status === "refused") {
                    row.error = outcome.message
                        || "Save refused. Your draft is kept; copy or discard it before replacing server text.";
                    row.conflict = clone(outcome.conflict || null);
                    ok = false;
                } else if (outcome.status === "no-op") {
                    row.error = "";
                    row.conflict = null;
                    ok = true;
                    if (!promptDraftHasChanges(row, { global })) drafts.delete(key);
                } else {
                    row.error = "";
                    row.conflict = null;
                    if (outcome.value && typeof outcome.value === "object") {
                        mergePromptRecords(row, value, outcome.value, {
                            global: draftGlobal(row, global),
                        });
                    } else {
                        // Compatibility for successful mutations whose response
                        // omits the row: retain the submitted-shape baseline.
                        row.base = value;
                    }
                    ok = true;
                    onAcknowledge?.({
                        acknowledged: clone(outcome.value || null),
                        submitted: clone(value),
                        baseline: clone(row.base), value: clone(row.value),
                    });
                    if (!promptDraftHasChanges(row, { global })) drafts.delete(key);
                }
            } catch (error) {
                row.error = error?.message || "Save failed. Your draft is kept.";
                row.conflict = clone(error?.payload?.conflict || null);
                ok = false;
            }
        } while (row.saveAgain && drafts.get(key) === row);
        return ok;
    })();
    try {
        return await row.pending;
    } finally {
        row.pending = null;
    }
}

// Adopt unrelated refreshed fields while preserving the original expectation for
// every touched record. Never turn a server refresh into a new local edit.
export function rebasePromptDraft(row, current, { global = false } = {}) {
    if (!row) return current;
    mergePromptRecords(row, clone(row.base), current, { global });
    return { ...current, ...clone(row.value) };
}
