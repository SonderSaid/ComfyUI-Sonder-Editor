// @server-mirror server/prompt_context.py::SHOT_LEAD_WHITESPACE
// @server-mirror server/prompt_context.py::shot_plan
// Scope and parity disposition: tests/test_mutation_authoring_contract.py::MIRRORED_MODULES
//
// Whether a Shot OPENS its section (and may carry the section time) or sits IN
// PLACE inside the text. The compiler decides what the prompt says; this copy
// only lets the Shot dialog say up front whether "Include section time" can
// apply, from the editor's own live documents. See `shot_plan` for the rule.

export const SHOT_LEAD_WHITESPACE = " \t\n\r\f\v ​﻿";
const IGNORED_KINDS = new Set(["shot", "timestamp"]);
const BLANK = new Set(SHOT_LEAD_WHITESPACE);

function isBlank(text) {
    for (const char of String(text ?? "")) {
        if (!BLANK.has(char)) return false;
    }
    return true;
}

function attachmentMap(attachments) {
    const byId = new Map();
    for (const value of attachments || []) {
        if (value && typeof value === "object") {
            byId.set(String(value.attachment_id || ""), value);
        }
    }
    return byId;
}

function enabled(attachment) {
    return attachment?.enabled !== false;
}

/** Does this node put content ahead of a later Shot? */
function nodeCounts(node, byId) {
    if (node?.type === "text") return !isBlank(node.text);
    if (node?.type !== "attachment") return false;
    const attachment = byId.get(String(node.attachment_id || ""));
    return Boolean(attachment && enabled(attachment)
        && !IGNORED_KINDS.has(String(attachment.kind || "")));
}

/** Mirror of `shot_plan`: openers, in-place ids in document order, timestamp. */
export function promptShotPlan(attachments, channelDocs, shotChannel) {
    const byId = attachmentMap(attachments);
    const shots = [...byId.entries()]
        .filter(([, value]) => enabled(value) && value.kind === "shot")
        .map(([key]) => key);
    const document = shotChannel ? channelDocs?.[shotChannel] : null;
    const openers = [];
    const inPlace = [];
    const placed = new Set();
    let contentBefore = false;
    for (const node of document?.nodes || []) {
        if (node?.type === "attachment") {
            const attachmentId = String(node.attachment_id || "");
            const attachment = byId.get(attachmentId);
            if (attachment && enabled(attachment) && attachment.kind === "shot") {
                if (!placed.has(attachmentId)) {
                    placed.add(attachmentId);
                    (contentBefore ? inPlace : openers).push(attachmentId);
                }
                continue;
            }
        }
        if (nodeCounts(node, byId)) contentBefore = true;
    }
    openers.push(...shots.filter((key) => !placed.has(key)));
    return {
        openers,
        in_place: inPlace,
        timestamp: openers.some((key) => Boolean(byId.get(key)?.config?.timestamp)),
    };
}

/** Whether one Shot opens its section, from the host's live documents. */
export function promptShotOpensSection({ attachments, channelDocs, shotChannel,
    attachmentId } = {}) {
    return !promptShotPlan(attachments, channelDocs, shotChannel)
        .in_place.includes(String(attachmentId || ""));
}

/**
 * Whether a Shot inserted at `bookmark` would open its section: nothing that
 * counts lies before the caret in `document`. A null bookmark appends at the
 * end (`restorePromptInsertion`), so then the whole document is "before".
 */
export function promptShotOpensAtCaret(document, bookmark, attachments) {
    const byId = attachmentMap(attachments);
    const start = bookmark?.start || null;
    for (const node of document?.nodes || []) {
        if (start && node?.node_id === start.node_id) {
            if (node.type === "text") {
                return isBlank(String(node.text || "").slice(0, Math.max(0, start.offset | 0)));
            }
            return !(Number(start.offset) > 0 && nodeCounts(node, byId));
        }
        if (nodeCounts(node, byId)) return false;
    }
    return true;
}
