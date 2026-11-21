/** Ephemeral authoring intents: stable row identity, field ownership, no disk state.
 *
 * A draft row holds RECORDS, not a snapshot. Each record an author touched
 * (a channel document, or one attachment by id) keeps two values:
 *
 * - `base`: the record the author saw when they first touched it. It is the
 *   save's `expected`, and only an acknowledgement for that record or an
 *   explicit author choice ("Use server's version") moves it. A refresh never
 *   does, so a refused record stays refused until the author decides.
 * - `value`: what the author has now.
 *
 * Records nobody touched are never in the row and never sent. Each mount (a
 * Prompt-panel card, a timeline bar) touches only the records it changed, so
 * two mounts on one section cannot overwrite each other's records.
 *
 * An acknowledgement is published to every listener on the drafts map, not to
 * the closure of the render that issued the save, so a rebuilt panel and a bar
 * both learn what the server stored.
 */
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

/** The mutation fields for a draft's record edit; `{}` when it names nothing. */
export function promptEditBody(edit) {
    const documents = edit?.documents || {};
    const attachments = edit?.attachments || {};
    if (!Object.keys(documents).length && !Object.keys(attachments).length) return {};
    return { prompt_edit: clone({ documents, attachments }) };
}

/** The local paint for a record edit: `current`'s records with the edit's
 * values laid over them (a null value removes the record). */
export function promptEditPaint(current, edit, { global = false } = {}) {
    const docs = global ? "global_channel_docs" : "channel_docs";
    const atts = global ? "global_attachments" : "attachments";
    const documents = clone(current?.[docs] || {});
    for (const [key, change] of Object.entries(edit?.documents || {})) {
        if (change.value == null) delete documents[key];
        else documents[key] = clone(change.value);
    }
    const byId = new Map((current?.[atts] || []).map((record) => [record.attachment_id, clone(record)]));
    for (const [id, change] of Object.entries(edit?.attachments || {})) {
        if (change.value == null) byId.delete(id);
        else byId.set(id, clone(change.value));
    }
    return { [docs]: documents, [atts]: [...byId.values()] };
}

export function promptDraftKey(projectId, sceneId, promptId = "global") {
    return JSON.stringify([projectId, sceneId, promptId]);
}

const docsField = (global) => (global ? "global_channel_docs" : "channel_docs");
const attachmentsField = (global) => (global ? "global_attachments" : "attachments");

// A document compared without minting or reading node ids: adjacent text runs
// merged, empty runs dropped. The normalizer mints ids, so normalizing two
// equal raw values could manufacture a difference.
function documentShape(doc) {
    if (!doc || typeof doc !== "object") return null;
    // Not a node document: only its raw content can say what it holds.
    if (!Array.isArray(doc.nodes)) return [["raw", JSON.stringify(doc)]];
    const shape = [];
    for (const node of doc.nodes) {
        if (node?.type === "text") {
            const text = String(node.text ?? "");
            if (!text) continue;
            const last = shape[shape.length - 1];
            if (last?.[0] === "text") last[1] += text;
            else shape.push(["text", text]);
        } else if (node && typeof node === "object") {
            shape.push([String(node.type || ""), String(node.attachment_id ?? ""),
                String(node.capability_id ?? "")]);
        }
    }
    return shape;
}

/** Raw equality first, then the non-minting document shape. Attachments are
 * atomic records: raw equality only. */
export function promptRecordsEquivalent(kind, a, b) {
    if (equal(a ?? null, b ?? null)) return true;
    if (kind !== "documents") return false;
    if ((a == null) !== (b == null)) {
        // An absent document and an empty one read the same to the author.
        const present = a ?? b;
        return equal(documentShape(present), []);
    }
    return equal(documentShape(a), documentShape(b));
}

function newRow(global) {
    return { global: Boolean(global), base: { documents: {}, attachments: {} },
        value: { documents: {}, attachments: {} }, submitted: null,
        // Records another surface replaced while a save of them was in
        // flight: that save's acknowledgement must not move their base.
        detached: { documents: {}, attachments: {} },
        revision: 0, pending: null, error: "", conflict: null };
}

function forgetRecord(row, kind, recordKey) {
    delete row.value[kind][recordKey];
    delete row.base[kind][recordKey];
    if (row.detached) delete row.detached[kind][recordKey];
}

function touchedCount(row) {
    return Object.keys(row?.value?.documents || {}).length
        + Object.keys(row?.value?.attachments || {}).length;
}

export function promptDraftHasChanges(row) {
    return touchedCount(row) > 0;
}

/** Record keys the row holds, as `document <key>` / `attachment <id>`. */
export function promptDraftRecordLabels(row) {
    return [...Object.keys(row?.value?.documents || {}).map((key) => `document ${key}`),
        ...Object.keys(row?.value?.attachments || {}).map((key) => `attachment ${key}`)];
}

/**
 * Record what a mount changed. `changes` is `{ documents: {key: {shown, value}},
 * attachments: {id: {shown, value}} }`: `shown` is the record the mount was
 * displaying before the change (used only on first touch), `value` what it
 * holds now (null = removed). A record whose value returns to its base is
 * dropped, unless it is part of a save in flight (then the revert itself must
 * be sent once the save settles).
 *
 * `from` (optional) is what the mount displayed for the record just before
 * this change. When the row holds a value the mount never displayed (another
 * surface wrote it), the change was not made on top of that value: its base
 * becomes the mount's own `shown`, and an in-flight save of the record is
 * not allowed to move that base. The save then refuses against what this
 * author saw instead of overwriting text they never saw.
 */
export function touchPromptDraft(drafts, key, changes = {}, { global = false } = {}) {
    let row = drafts.get(key);
    const created = !row;
    if (created) row = newRow(global);
    row.detached ||= { documents: {}, attachments: {} };
    let changed = false;
    for (const kind of ["documents", "attachments"]) {
        for (const [recordKey, change] of Object.entries(changes[kind] || {})) {
            const value = clone(change?.value ?? null);
            const touched = Object.hasOwn(row.value[kind], recordKey);
            if (!touched) {
                const shown = clone(change?.shown ?? null);
                if (promptRecordsEquivalent(kind, shown, value)) continue;
                row.base[kind][recordKey] = shown;
                row.value[kind][recordKey] = value;
                changed = true;
                continue;
            }
            if (equal(row.value[kind][recordKey], value)) continue;
            const inFlight = row.submitted && Object.hasOwn(row.submitted[kind] || {}, recordKey);
            if (change && Object.hasOwn(change, "from")
                    && !promptRecordsEquivalent(kind, change.from ?? null, row.value[kind][recordKey])) {
                row.base[kind][recordKey] = clone(change.shown ?? null);
                if (inFlight) row.detached[kind][recordKey] = true;
            }
            row.value[kind][recordKey] = value;
            changed = true;
            if (!inFlight && promptRecordsEquivalent(kind, row.base[kind][recordKey], value))
                forgetRecord(row, kind, recordKey);
        }
    }
    if (changed) row.revision += 1;
    if (!touchedCount(row) && !row.pending && !row.error) {
        if (!created) drafts.delete(key);
        return null;
    }
    if (created && !touchedCount(row)) return null;
    drafts.set(key, row);
    return row;
}

/** The `prompt_edit` body for every touched record. */
export function promptDraftEdit(row) {
    const edit = { documents: {}, attachments: {} };
    for (const kind of ["documents", "attachments"]) {
        for (const [recordKey, value] of Object.entries(row?.value?.[kind] || {})) {
            edit[kind][recordKey] = { expected: clone(row.base[kind][recordKey] ?? null), value: clone(value) };
        }
    }
    return edit;
}

/**
 * What a mount displays: the current server-side view with the row's touched
 * records laid over it. Nothing in the row moves: a refresh is not an
 * acknowledgement, so a touched record keeps the base its author saw.
 */
export function rebasePromptDraft(row, current, { global = false } = {}) {
    const view = clone(current || {});
    if (!row) return view;
    const isGlobal = row.global ?? global;
    const docs = docsField(isGlobal);
    const atts = attachmentsField(isGlobal);
    const documents = { ...(view[docs] || {}) };
    for (const [recordKey, value] of Object.entries(row.value.documents)) {
        if (value == null) delete documents[recordKey];
        else documents[recordKey] = clone(value);
    }
    view[docs] = documents;
    const list = Array.isArray(view[atts]) ? view[atts].map(clone) : [];
    const byId = new Map(list.map((record) => [record.attachment_id, record]));
    for (const [id, value] of Object.entries(row.value.attachments)) {
        if (value == null) byId.delete(id);
        else byId.set(id, clone(value));
    }
    view[atts] = [...byId.values()];
    return view;
}

export function dismissPromptDraftError(drafts, key) {
    const row = drafts.get(key);
    if (!row) return false;
    row.error = "";
    row.conflict = null;
    if (!touchedCount(row) && !row.pending) drafts.delete(key);
    return true;
}

/**
 * "Use server's version" for the contested record only: base and value both
 * become the server's record, so it leaves the draft. Returns the labels of
 * the records the draft still holds, so the surface can say that a move
 * across records is now split.
 */
export function resolvePromptDraftRecord(drafts, key) {
    const row = drafts.get(key);
    const conflict = row?.conflict;
    if (!row || !conflict || !["document", "attachment"].includes(conflict.record_kind))
        return false;
    const kind = conflict.record_kind === "document" ? "documents" : "attachments";
    forgetRecord(row, kind, conflict.record_key);
    row.revision += 1;
    row.error = "";
    row.conflict = null;
    const remaining = promptDraftRecordLabels(row);
    if (!remaining.length && !row.pending) drafts.delete(key);
    // Every live surface adopts the server's record as shown, without a touch.
    publish(drafts, { key, global: row.global, resolution: {
        kind, recordKey: conflict.record_key, value: clone(conflict.current ?? null) } });
    return { resolved: `${conflict.record_kind} ${conflict.record_key}`, remaining };
}

export async function retryPromptDraft(drafts, key) {
    const row = drafts.get(key);
    if (!row || typeof row.retry !== "function") return false;
    return await row.retry();
}

// ── Mounts ───────────────────────────────────────────────────────────────

const byAttachmentId = (list) => new Map((Array.isArray(list) ? list : [])
    .map((record) => [record.attachment_id, clone(record)]));

/**
 * One surface's bookkeeping for a draft key (a Prompt-panel card, a timeline
 * bar). `shown` is the current record set the surface drew from (the base a
 * first touch records); `displayed` is what it actually shows (the draft
 * view). `record()` touches only the records whose content changed since the
 * surface last recorded or repainted them, so a surface never writes records
 * it did not change. `acknowledge()` folds a published acknowledgement into
 * the surface and says which records to repaint: a record is repainted only
 * when the surface still shows exactly what was submitted.
 */
export function createPromptDraftMount({ drafts, key, global = false,
    shown = {}, displayed = {} } = {}) {
    const shownDocs = clone(shown.documents || {});
    const shownAtts = byAttachmentId(shown.attachments);
    let lastDocs = clone(displayed.documents || {});
    let lastAtts = byAttachmentId(displayed.attachments);
    return {
        key,
        record({ documents = null, attachments = null } = {}) {
            const changes = { documents: {}, attachments: {} };
            for (const [docKey, doc] of Object.entries(documents || {})) {
                // Shape, not raw: a re-normalized document with fresh node ids
                // is not an edit.
                if (promptRecordsEquivalent("documents", doc ?? null, lastDocs[docKey] ?? null)) {
                    lastDocs[docKey] = clone(doc ?? null);
                    continue;
                }
                changes.documents[docKey] = { shown: shownDocs[docKey] ?? null,
                    from: lastDocs[docKey] ?? null, value: doc ?? null };
                lastDocs[docKey] = clone(doc ?? null);
            }
            if (attachments) {
                const next = byAttachmentId(attachments);
                for (const id of new Set([...lastAtts.keys(), ...next.keys()])) {
                    if (equal(lastAtts.get(id) ?? null, next.get(id) ?? null)) continue;
                    changes.attachments[id] = { shown: shownAtts.get(id) ?? null,
                        from: lastAtts.get(id) ?? null, value: next.get(id) ?? null };
                }
                lastAtts = next;
            }
            if (!Object.keys(changes.documents).length && !Object.keys(changes.attachments).length)
                return drafts.get(key) || null;
            return touchPromptDraft(drafts, key, changes, { global });
        },
        /** The record this surface showed, for a narrow change built from it. */
        shownDocument: (docKey) => clone(shownDocs[docKey] ?? null),
        shownAttachment: (id) => clone(shownAtts.get(id) ?? null),
        acknowledge(event, { documents = {}, attachments = [] } = {}) {
            if (!event || event.key !== key) return null;
            const repaint = { documents: {}, attachments: null };
            if (event.resolution) {
                // "Use server's version": the author chose the stored record.
                const { kind, recordKey, value } = event.resolution;
                if (kind === "documents") {
                    shownDocs[recordKey] = clone(value);
                    lastDocs[recordKey] = clone(value);
                    repaint.documents[recordKey] = clone(value);
                } else {
                    const current = byAttachmentId(attachments);
                    if (value == null) { shownAtts.delete(recordKey); current.delete(recordKey); lastAtts.delete(recordKey); }
                    else {
                        shownAtts.set(recordKey, clone(value)); current.set(recordKey, clone(value));
                        lastAtts.set(recordKey, clone(value));
                    }
                    // Only the resolved record counts as drawn: the surface's
                    // other, unrecorded chip edits must still be recorded.
                    repaint.attachments = [...current.values()];
                }
                return repaint;
            }
            if (!event.records) return null; // a refusal: nothing was stored
            const row = drafts.get(key);
            // Does this surface's view now include the stored record? Only
            // then may a later first touch use it as `expected`. A surface that
            // never displayed the submitted value (another surface's save) and
            // has its own unrecorded edit keeps what it saw, so its next save
            // refuses rather than overwriting text its author never saw.
            const seesStored = (kind, recordKey, { repainted, now, last, stored, submitted }) => {
                const same = (a, b) => promptRecordsEquivalent(kind, a ?? null, b ?? null);
                if (repainted || same(now, stored) || same(last, submitted)) return true;
                // This surface wrote the row's newer value on top of the
                // submitted one, and the row's base moved with the ack.
                return Boolean(row && Object.hasOwn(row.value[kind], recordKey)
                    && same(last, row.value[kind][recordKey])
                    && same(row.base[kind][recordKey], stored));
            };
            // A settled record repaints where this surface shows what was
            // submitted, or still shows what it last drew (it never changed
            // the record, so the stored one is simply newer). A surface with
            // its own unrecorded typing keeps it.
            for (const [docKey, stored] of Object.entries(event.records.documents || {})) {
                const submitted = event.submitted.documents?.[docKey] ?? null;
                const now = documents[docKey] ?? null;
                const last = lastDocs[docKey] ?? null;
                const repainted = event.settled.documents.includes(docKey)
                    && (promptRecordsEquivalent("documents", now, submitted)
                        || promptRecordsEquivalent("documents", now, last));
                if (repainted) {
                    repaint.documents[docKey] = clone(stored);
                    lastDocs[docKey] = clone(stored);
                }
                if (seesStored("documents", docKey, { repainted, now, last, stored, submitted }))
                    shownDocs[docKey] = clone(stored);
            }
            const current = byAttachmentId(attachments);
            let touchedList = false;
            for (const [id, stored] of Object.entries(event.records.attachments || {})) {
                const submitted = event.submitted.attachments?.[id] ?? null;
                const now = current.get(id) ?? null;
                const last = lastAtts.get(id) ?? null;
                const repainted = event.settled.attachments.includes(id)
                    && (equal(now, submitted) || equal(now, last));
                if (repainted) {
                    if (stored == null) current.delete(id); else current.set(id, clone(stored));
                    // Per record: folding the whole list into `lastAtts` would
                    // swallow the surface's unrecorded chip insertions and
                    // removals, which would then never be sent.
                    if (stored == null) lastAtts.delete(id); else lastAtts.set(id, clone(stored));
                    touchedList = true;
                }
                if (seesStored("attachments", id, { repainted, now, last, stored, submitted })) {
                    if (stored == null) shownAtts.delete(id); else shownAtts.set(id, clone(stored));
                }
            }
            if (touchedList) repaint.attachments = [...current.values()];
            return repaint;
        },
    };
}

// ── Host-wide acknowledgement ────────────────────────────────────────────
const listeners = new WeakMap();

/** Subscribe to acknowledgements for every row in `drafts`; returns cleanup. */
export function addPromptDraftListener(drafts, listener) {
    let set = listeners.get(drafts);
    if (!set) listeners.set(drafts, set = new Set());
    set.add(listener);
    return () => set.delete(listener);
}

function publish(drafts, event) {
    for (const listener of [...(listeners.get(drafts) || [])]) {
        try { listener(event); } catch (error) { console.warn("[Sonder] Prompt draft listener failed:", error); }
    }
}

function acknowledgedRecords(row, submitted, acknowledged) {
    const isGlobal = row.global;
    const records = { documents: {}, attachments: {} };
    const ackDocs = acknowledged?.[docsField(isGlobal)];
    const ackAtts = acknowledged?.[attachmentsField(isGlobal)];
    const ackById = Array.isArray(ackAtts)
        ? new Map(ackAtts.map((record) => [record.attachment_id, record])) : null;
    for (const key of Object.keys(submitted.documents)) {
        records.documents[key] = ackDocs && typeof ackDocs === "object"
            ? clone(ackDocs[key] ?? null) : clone(submitted.documents[key]);
    }
    for (const id of Object.keys(submitted.attachments)) {
        records.attachments[id] = ackById ? clone(ackById.get(id) ?? null)
            : clone(submitted.attachments[id]);
    }
    return records;
}

function normalizedSaveResult(result) {
    if (result === true) return { status: "acknowledged" };
    if (result === false || result == null) return { status: "refused" };
    if (typeof result !== "object") return { status: "refused" };
    return result;
}

/**
 * Save every touched record. `save(edit, row)` returns `{status, value?,
 * message?, conflict?}`; `value` is the stored row (section, or the scene's
 * global fields) the acknowledgement is read from.
 *
 * The row stays authoritative until its records are acknowledged. On success
 * each submitted record's base becomes what the server stored; a record the
 * author has not changed since leaves the row, a newer one stays and is sent
 * on the next save. Listeners hear every acknowledgement.
 */
export async function savePromptDraft(drafts, key, save) {
    const row = drafts.get(key);
    if (!row) return true;
    // The save path is host-level, not render-level: a Retry from any surface
    // goes through the same host gesture and the same publication.
    row.retry = () => savePromptDraft(drafts, key, save);
    if (row.pending) { row.saveAgain = true; return row.pending; }
    row.pending = (async () => {
        let ok = true;
        do {
            row.saveAgain = false;
            if (!touchedCount(row)) {
                // Nothing left to send (the author reverted every refused
                // record): the refusal no longer describes the draft.
                row.error = "";
                row.conflict = null;
                ok = true;
                break;
            }
            const edit = promptDraftEdit(row);
            row.submitted = clone(row.value);
            const submitted = row.submitted;
            try {
                const outcome = normalizedSaveResult(await save(edit, row));
                if (outcome.status === "refused") {
                    row.error = outcome.message
                        || "Save refused. Your draft is kept; copy or discard it before replacing server text.";
                    row.conflict = clone(outcome.conflict || null);
                    ok = false;
                    // Any surface may have saved (a timeline bar): every live
                    // surface hears it, so the panel's recovery bar shows.
                    publish(drafts, { key, global: row.global, refused: true });
                } else {
                    row.error = "";
                    row.conflict = null;
                    ok = true;
                    const records = outcome.status === "no-op"
                        ? clone(submitted) : acknowledgedRecords(row, submitted, outcome.value);
                    const settled = { documents: [], attachments: [] };
                    for (const kind of ["documents", "attachments"]) {
                        for (const [recordKey, stored] of Object.entries(records[kind])) {
                            if (!Object.hasOwn(row.value[kind], recordKey)) continue;
                            if (equal(row.value[kind][recordKey], submitted[kind][recordKey])) {
                                forgetRecord(row, kind, recordKey);
                                settled[kind].push(recordKey);
                            } else if (row.detached?.[kind]?.[recordKey]) {
                                // Another surface replaced this record during
                                // the save without seeing what it submitted:
                                // its base stays what that surface saw.
                                delete row.detached[kind][recordKey];
                            } else {
                                row.base[kind][recordKey] = clone(stored);
                            }
                        }
                    }
                    publish(drafts, { key, global: row.global, records, submitted: clone(submitted), settled });
                }
            } catch (error) {
                row.error = error?.message || "Save failed. Your draft is kept.";
                row.conflict = clone(error?.payload?.conflict || null);
                ok = false;
                publish(drafts, { key, global: row.global, refused: true });
            } finally {
                row.submitted = null;
                // A detachment is relative to the submission it happened
                // under; the next save sends the detached value itself.
                row.detached = { documents: {}, attachments: {} };
            }
            // A save requested behind this one drains after success OR refusal
            // (durable rule); its callers await the whole drain.
        } while (row.saveAgain && drafts.get(key) === row);
        if (!touchedCount(row) && !row.error && drafts.get(key) === row) drafts.delete(key);
        return ok;
    })();
    try {
        return await row.pending;
    } finally {
        row.pending = null;
    }
}
