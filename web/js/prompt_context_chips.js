// Shared structured prompt editor used by timeline bars and both Prompt-tool
// modes. Projects store semantic attachment ids; this module never writes
// provider ordinals, timestamps, or generated text into authored documents.

import { REFERENCE_VERDICT_LABEL, resolveReferenceVerdicts } from "./reference_resolution.js";
import { lanePopulation } from "./reference_lane_identity.js";
import { EDITOR_COLORS as COLORS, chromeInputCss, setButtonDisabled,
    setButtonVariant } from "./editor_theme.js";
import { PRESERVE_DEFAULT, PRIORITY as KEY_PRIORITY,
    register as registerKeyboardConsumer } from "./keyboard_ownership.js";
import { promptToken, promptTokenDeclarationsFromProfile } from "./prompt_tokens.js";
import { notifyWarning } from "./editor_notifications.js";
import { openContextMenu } from "./editor_context_menu.js";
import { createDisclosureMemory } from "./disclosure_memory.js";
import {
    declaredFieldChoices,
    orderedReferenceDerived,
    referenceDerivedDeclarations,
    referenceFieldDeclaration,
} from "./prompt_profile_declarations.js";

/**
 * Context-chip role identity: the violet that marks an authored chip as a chip
 * wherever it appears, in prose and in the panels that edit it.
 *
 * These stay literal rather than becoming `editor_theme.js` tokens. The theme's
 * `lanePrompt` is the TIMELINE lane accent, which a durable rule keeps separate
 * from role identity, and no token covers "an inline authored object". Whether
 * this role earns a token is a question for the editor-wide normalization, not
 * for a pass scoped to the Prompt tool — but a single named constant is what
 * lets that pass find every use at once, instead of twelve scattered literals.
 */
const CHIP_PALETTE = Object.freeze({
    border: "#6f62a8",
    background: "#29243b",
    text: "#ded6ff",
    /** Glyph controls carried INSIDE a chip: suppress, unlink, remove. */
    control: "#c9bfff",
    /** The "LINKED" group marker beside a chip label. */
    linked: "#b8a9ef",
});

export const PROMPT_DOCUMENT_SCHEMA = "prompt_document_v1";
export const PROMPT_CONTEXT_FORMAT = "prompt_context_v1";

const AUTHORING_KINDS = ["shot", "timestamp", "reference", "vocal_event", "prompt_link", "prompt_link_scope", "custom"];
export const SCOPE_ONLY_KINDS = ["timestamp", "prompt_link_scope"];
// A Prompt Link resolves only through an inline document anchor, and a Vocal
// Event's document position *is* its place in the spoken order. Authored as
// scope chips they emit nothing and lose chronology respectively, so no scope
// row may offer them; the compiler blocks legacy rows that already exist.
export const INLINE_ONLY_KINDS = ["prompt_link", "vocal_event"];
const SCOPE_KINDS = AUTHORING_KINDS.filter((kind) => !INLINE_ONLY_KINDS.includes(kind));
const LABELS = {
    shot: "Shot", timestamp: "Time", reference: "Reference",
    vocal_event: "Vocal event", prompt_link: "Prompt link",
    prompt_link_scope: "Section Prompt link", custom: "Context",
};
const DIALOGUE_LANGUAGES = ["English", "Spanish", "French", "German", "Italian",
    "Japanese", "Korean", "Chinese", "Portuguese", "Hindi"];
export const REFERENCE_OVERRIDE_FIELDS = Object.freeze([
    "definition", "summary", "task_types", "retention_detail",
    "retention_details", "audio_definition", "audio_relationship", "text",
    "visual_intent", "audio_intent",
]);
const REFERENCE_CAPABILITY_VALUE_FIELDS = Object.freeze({
    definitions: Object.freeze(["definition", "audio_definition"]),
    summary: Object.freeze(["summary", "task_types"]),
    retention: Object.freeze(["retention_detail", "visual_intent", "audio_intent"]),
    mentions: Object.freeze(["text"]),
    audio_relationship: Object.freeze(["audio_relationship"]),
    derived_prompt: Object.freeze([]),
});
const REFERENCE_VALUE_LABELS = Object.freeze({
    definition: "Definition", audio_definition: "Audio definition",
    summary: "Summary", task_types: "Summary task types",
    retention_detail: "Preservation detail", visual_intent: "Visual handling",
    audio_intent: "Audio handling", text: "Inline mention",
    audio_relationship: "Audio relationship",
});
const REFERENCE_VALUE_CAPABILITY = Object.freeze(Object.fromEntries(
    Object.entries(REFERENCE_CAPABILITY_VALUE_FIELDS).flatMap(([kind, fields]) =>
        fields.map((field) => [field, kind]))));
/**
 * Where a resolved value came from, ranked by the documented precedence chain:
 * format defaults < Reference entity < physical member < staged item override <
 * common identity defaults < chip. `tier` collapses all of these into three
 * colour states, so it cannot say which of two "shared" sources is the more
 * specific one — that is what rank is for, and it is why a summary listing
 * several sources can name the most specific instead of counting them.
 */
export const REFERENCE_AUTHORITY_RANK = Object.freeze({
    format: 1, entity: 2, member: 3, staged: 4, identity: 5, chip: 6,
});
/** Build a `fieldSources` record; `kind` is a key of REFERENCE_AUTHORITY_RANK. */
function authoritySource(label, kind) {
    return Object.freeze({
        label,
        tier: kind === "format" ? "format" : (kind === "chip" ? "chip" : "shared"),
        rank: REFERENCE_AUTHORITY_RANK[kind] || REFERENCE_AUTHORITY_RANK.format,
    });
}
// Presentation-only facts about the renderer floor above. They are renderer
// knowledge of its own fixed field set, not provider vocabulary: a format
// renames a field by DECLARING it in `derived[kind].fields`, which always wins.
//
// Expiry: both sets go when the field vocabulary itself becomes
// declaration-derived and the floor is deleted — see the "Declaration-derived
// Reference field vocabulary" execution-queue entry. Until then a floor field
// needs a renderer-side shape, because there is nothing else to ask.
const REFERENCE_MULTILINE_FIELDS = Object.freeze(new Set([
    "definition", "audio_definition", "summary", "retention_detail",
    "audio_relationship",
]));
// Floor fields that are enums rather than prose. They render ONLY when the
// format declares their vocabulary — there is no renderer-side value list to
// fall back on, and a free-text control for an enum would author values the
// compiler must then reject. This preserves the existing `declaredSelect` gate.
const REFERENCE_ENUM_FIELDS = Object.freeze(new Set([
    "task_types", "visual_intent", "audio_intent",
]));
// Enum fields whose blank choice means "follow the tier below" rather than an
// authored empty. Reset and explicit-empty stay distinct, so these cannot use
// the bare declared choice list.
const REFERENCE_INHERIT_CHOICE_FIELDS = Object.freeze(new Set([
    "visual_intent", "audio_intent",
]));

function referenceCapabilityValueFields(profile, kind) {
    const declaration = referenceDerivedDeclarations(profile)?.[String(kind || "")] || {};
    const declared = declaration.fields && typeof declaration.fields === "object"
        ? Object.keys(declaration.fields) : [];
    return [...new Set([
        ...(REFERENCE_CAPABILITY_VALUE_FIELDS[String(kind || "")] || []),
        ...declared,
    ])];
}

let textEditorKeyboardClaimInstalled = false;
function installTextEditorKeyboardClaim() {
    if (textEditorKeyboardClaimInstalled || typeof window === "undefined"
            || typeof window.addEventListener !== "function") return;
    textEditorKeyboardClaimInstalled = true;
    const textEditingTarget = (event) => {
        const candidates = [event?.target, document.activeElement].filter(Boolean);
        for (const candidate of candidates) {
            const promptRoot = candidate?.closest?.("[data-sonder-prompt-box='1']");
            if (promptRoot) return { target: promptRoot, promptRoot };
        }
        return null;
    };
    registerKeyboardConsumer({
        id: "sonder-prompt-text-editor",
        priority: KEY_PRIORITY.TEXT_EDITOR,
        keydown: (event) => {
            const editing = textEditingTarget(event);
            if (!editing) return false;
            return editing.promptRoot?._sonderOwnedKeydown?.(event)
                ? true : PRESERVE_DEFAULT;
        },
        keyup: (event) => textEditingTarget(event) ? PRESERVE_DEFAULT : false,
    });
}
installTextEditorKeyboardClaim();

const uid = () => {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID().replaceAll("-", "");
    return `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
};

export function textPromptDocument(text = "", nodeId = "") {
    return {
        schema: PROMPT_DOCUMENT_SCHEMA,
        nodes: [{ type: "text", node_id: nodeId || uid(), text: String(text ?? "") }],
    };
}

export function normalizePromptDocument(raw, fallbackText = "") {
    if (typeof raw === "string") return textPromptDocument(raw);
    const nodes = [];
    const seen = new Set();
    for (const value of Array.isArray(raw?.nodes) ? raw.nodes : []) {
        if (!value || !["text", "attachment"].includes(value.type)) continue;
        let nodeId = String(value.node_id || "").trim() || uid();
        while (seen.has(nodeId)) nodeId = uid();
        seen.add(nodeId);
        if (value.type === "text") {
            // Line endings are normalized HERE, not at the paste handler, because
            // this is the choke point every document passes through: the
            // `promptState` and `value` setters, the saved-draft load and every
            // server echo. Normalizing only on paste would leave already-stored
            // drafts broken. A stray CR is not cosmetic: the channel header
            // pattern cannot match a line ending in one, so `channel:` headers
            // stay literal text and all content falls into a single channel.
            nodes.push({ type: "text", node_id: nodeId,
                text: String(value.text ?? "").replace(/\r\n?/g, "\n") });
        } else {
            const attachmentId = String(value.attachment_id || "").trim();
            if (!attachmentId) continue;
            const node = { type: "attachment", node_id: nodeId, attachment_id: attachmentId };
            // Mirrors `normalize_prompt_document`. Dropping it here would make
            // an echoed document hash differently from the server's copy and
            // spuriously 409 identity validation. Parity is pinned by a test.
            if (value.capability_id) node.capability_id = String(value.capability_id);
            nodes.push(node);
        }
    }
    return { schema: PROMPT_DOCUMENT_SCHEMA, nodes: nodes.length ? nodes : textPromptDocument(fallbackText).nodes };
}

export function promptDocumentText(documentValue) {
    return normalizePromptDocument(documentValue).nodes
        .filter((node) => node.type === "text")
        .map((node) => node.text)
        .join("");
}

export function promptAttachmentAnchoredChannels(documents, attachmentId) {
    const target = String(attachmentId || "");
    return Object.entries(documents || {}).filter(([, documentValue]) =>
        normalizePromptDocument(documentValue).nodes.some((node) =>
            node.type === "attachment" && node.attachment_id === target))
        .map(([channelKey]) => channelKey);
}

/**
 * Return a scene snapshot whose one prompt section is overlaid with the mounted
 * editor's live draft.  An explicit array index is authoritative; identity and
 * range matching are fallback resolution only.  Existing section fields are
 * always preserved so accounting and candidate compilation observe the same
 * object without inventing a duplicate during a retime.
 */
export function sceneWithDraftSection(scene, {
    index = null,
    promptId = "",
    startFrame = null,
    endFrame = null,
    attachments = undefined,
    channelDocs = undefined,
    channels = undefined,
    section = null,
} = {}) {
    const snapshot = structuredClone(scene || {});
    const sections = Array.isArray(snapshot.prompt_sections)
        ? snapshot.prompt_sections : [];
    const hasStart = startFrame !== null && startFrame !== undefined
        && Number.isFinite(Number(startFrame));
    const hasEnd = endFrame !== null && endFrame !== undefined
        && Number.isFinite(Number(endFrame));
    let resolved = Number.isInteger(index) && index >= 0 && index < sections.length
        ? index : -1;
    if (resolved < 0 && promptId) {
        resolved = sections.findIndex((value) =>
            String(value?.prompt_id || "") === String(promptId));
    }
    if (resolved < 0 && hasStart && hasEnd) {
        resolved = sections.findIndex((value) =>
            Number(value?.start_frame) === Number(startFrame)
            && Number(value?.end_frame) === Number(endFrame));
    }
    const base = resolved >= 0 ? sections[resolved] : structuredClone(section || {});
    const overlay = { ...base };
    if (promptId) overlay.prompt_id = String(promptId);
    if (hasStart) overlay.start_frame = Number(startFrame);
    if (hasEnd) overlay.end_frame = Number(endFrame);
    if (attachments !== undefined) overlay.attachments = structuredClone(attachments || []);
    if (channelDocs !== undefined) overlay.channel_docs = structuredClone(channelDocs || {});
    if (channels !== undefined) overlay.channels = structuredClone(channels || {});
    if (resolved >= 0) sections[resolved] = overlay;
    else sections.push(overlay);
    snapshot.prompt_sections = sections;
    return snapshot;
}

/** Global drafts are scene-owned and must never be represented as a section. */
export function sceneWithDraftGlobal(scene, {
    attachments = undefined,
    channelDocs = undefined,
    channels = undefined,
} = {}) {
    const snapshot = structuredClone(scene || {});
    if (attachments !== undefined) {
        snapshot.global_attachments = structuredClone(attachments || []);
    }
    if (channelDocs !== undefined) {
        snapshot.global_channel_docs = structuredClone(channelDocs || {});
    }
    if (channels !== undefined) {
        snapshot.global_channels = structuredClone(channels || {});
    }
    return snapshot;
}

export function joinChannelDocuments(channelDocuments, channelKeys) {
    const keys = Array.isArray(channelKeys) ? channelKeys : [];
    const nodes = [];
    const populated = keys.filter((key) => {
        const documentValue = normalizePromptDocument(channelDocuments?.[key]);
        return documentValue.nodes.some((node) => node.type === "attachment")
            || promptDocumentText(documentValue).trim();
    });
    populated.forEach((key, index) => {
        if (keys.length > 1) nodes.push({ type: "text", node_id: uid(), text: `${key}:\n` });
        nodes.push(...normalizePromptDocument(channelDocuments?.[key]).nodes.map((node) => structuredClone(node)));
        if (index < populated.length - 1) nodes.push({ type: "text", node_id: uid(), text: "\n\n" });
    });
    return normalizePromptDocument({ nodes });
}

export function joinWritingSectionDocuments(sections, channelKeys) {
    const nodes = [];
    (sections || []).forEach((section, index) => {
        nodes.push(...joinChannelDocuments(section?.channel_docs || {}, channelKeys).nodes);
        if (index < sections.length - 1) {
            nodes.push({ type: "text", node_id: uid(), text: "\n---\n" });
        }
    });
    return normalizePromptDocument({ nodes });
}

/** Split only separator LINES; attachment nodes remain in their exact block. */
export function splitWritingPromptDocument(documentValue, { keepEmpty = false } = {}) {
    const blocks = [[]];
    // `joinWritingSectionDocuments` writes the separator as "\n---\n", but the
    // line preceding it has already appended its own "\n" suffix below by the
    // time the "---" line is read, so the separator's LEADING newline lands in
    // the block being closed. Left there it is re-emitted alongside a fresh
    // separator on the next join, growing every block but the last by one
    // newline per Apply. Mirrors `trimStructuralSeparator`, which does the same
    // job for channel headers — and like it, removes exactly what the joiner
    // contributed so an authored blank line before a break survives.
    const trimClosingSeparator = () => {
        const current = blocks.at(-1);
        const previous = current.at(-1);
        if (previous?.type !== "text" || !previous.text.endsWith("\n")) return;
        previous.text = previous.text.slice(0, -1);
        if (!previous.text) current.pop();
    };
    for (const node of normalizePromptDocument(documentValue).nodes) {
        if (node.type === "attachment") {
            blocks.at(-1).push(structuredClone(node));
            continue;
        }
        const lines = String(node.text || "").split("\n");
        let reusedSourceId = false;
        lines.forEach((line, index) => {
            if (line.trim() === "---") {
                trimClosingSeparator();
                blocks.push([]);
                return;
            }
            const suffix = index < lines.length - 1 ? "\n" : "";
            const current = blocks.at(-1);
            const previous = current.at(-1);
            if (previous?.type === "text") previous.text += line + suffix;
            else {
                current.push({ type: "text",
                    // A separator split keeps the left block's stable text
                    // identity and gives later fragments new identities. That
                    // is the basis for Writing-mode split/merge reconciliation.
                    node_id: reusedSourceId ? uid() : node.node_id,
                    text: line + suffix });
                reusedSourceId = true;
            }
        });
    }
    const normalized = blocks.map((nodes) => normalizePromptDocument({ nodes }));
    return keepEmpty ? normalized : normalized.filter((block) =>
        promptDocumentText(block).trim()
        || block.nodes.some((node) => node.type === "attachment"));
}

/**
 * Collapse separator padding a channel document accumulated before the
 * `"\n---\n"` asymmetry in `splitWritingPromptDocument` was fixed.
 *
 * That defect added one trailing newline per Apply to every block but the last,
 * and the fix only stops the growth — a channel already holding thirteen keeps
 * thirteen. This is the one-time heal, applied where the draft is being rebuilt
 * from sections anyway and the Reset stash makes it recoverable.
 *
 * ONE trailing newline is preserved, because a single authored blank line
 * before the next channel heading is indistinguishable from one unit of damage
 * and is the reading a user is more likely to have meant. Everything beyond it
 * accumulated mechanically.
 *
 * It is deliberately applied to every channel rather than only the last
 * populated one of a non-final block, which is the only place the defect could
 * reach: the draft is rebuilt from whatever the project holds, and narrowing it
 * would mean re-deriving which channel was last at the time the damage was
 * written, which nothing records.
 *
 * EXPIRY: this repairs data written before the separator fix. It can be deleted
 * once no project in circulation predates that fix — in practice, after the
 * next release that carries it plus one round of opening every project the
 * maintainer still uses. Until then removing it silently leaves damaged
 * projects damaged, since the fix only stops the growth.
 */
export function healSeparatorPadding(documentValue) {
    const nodes = normalizePromptDocument(documentValue).nodes.map(
        (node) => ({ ...node }));
    let removed = 0;
    let runStart = nodes.length;
    for (let index = nodes.length - 1; index >= 0; index -= 1) {
        const node = nodes[index];
        if (node.type !== "text") break;
        const trimmed = node.text.replace(/\n+$/, "");
        removed += node.text.length - trimmed.length;
        node.text = trimmed;
        runStart = index;
        // A node still holding prose ends the trailing run; one that was all
        // newlines does not, so the walk continues through it.
        if (trimmed) break;
    }
    if (removed <= 1) return normalizePromptDocument(documentValue);
    // Drop ONLY the nodes this walk emptied. An empty text node elsewhere is
    // the caret slot `render()` materializes between two adjacent chips, and
    // removing it removes the author's ability to type between them.
    const kept = nodes.filter((node, index) =>
        index < runStart || node.type !== "text" || node.text);
    // The preserved newline belongs at the END of the document. Appending it to
    // the last TEXT node instead puts it on the wrong side of a trailing
    // anchor, which pushes a mention onto its own line — the exact defect the
    // inline handle rendering exists to prevent, reintroduced by the repair.
    const last = kept.at(-1);
    if (last?.type === "text") last.text += "\n";
    else kept.push({ type: "text", node_id: uid(), text: "\n" });
    return normalizePromptDocument({ nodes: kept });
}

/**
 * Parse anchored `key:` header lines without flattening inline chips.
 *
 * `defaultKey` is where text ABOVE the first header lands. Omitted or unknown
 * means the first channel, which is the behavior every caller had before the
 * parameter existed — deliberately, because the template-retargeting collapse
 * (`retargetChannelDocuments`, and its `split_document_channels` mirror in
 * `server/prompt_context.py`) must keep folding into channel 1 byte for byte.
 * Only the Writing draft passes a key, resolved from its template's
 * `default_draft_channel`.
 */
export function splitPromptDocumentChannels(documentValue, channelKeys,
    { defaultKey = "" } = {}) {
    const keys = Array.isArray(channelKeys) && channelKeys.length ? channelKeys : ["visual"];
    const keySet = new Set(keys);
    const output = Object.fromEntries(keys.map((key) => [key, []]));
    let currentKey = keySet.has(defaultKey) ? defaultKey : keys[0];
    const appendText = (text) => {
        if (!text) return;
        const current = output[currentKey];
        const previous = current.at(-1);
        if (previous?.type === "text") previous.text += text;
        else current.push({ type: "text", node_id: uid(), text });
    };
    const trimStructuralSeparator = () => {
        const current = output[currentKey];
        let remaining = 2;
        while (current.length && remaining && current.at(-1)?.type === "text") {
            const previous = current.at(-1);
            let removed = 0;
            while (previous.text.endsWith("\n") && removed < remaining) {
                previous.text = previous.text.slice(0, -1);
                removed += 1;
            }
            remaining -= removed;
            if (previous.text) break;
            current.pop();
        }
    };
    for (const node of normalizePromptDocument(documentValue).nodes) {
        if (node.type === "attachment") {
            output[currentKey].push(structuredClone(node));
            continue;
        }
        const lines = String(node.text || "").split("\n");
        lines.forEach((line, index) => {
            const match = line.match(/^([A-Za-z0-9_]+):[ \t]?(.*)$/);
            const isHeader = !!(match && keySet.has(match[1]));
            if (isHeader) {
                trimStructuralSeparator();
                currentKey = match[1];
                appendText(match[2]);
            } else {
                appendText(line);
            }
            if (index < lines.length - 1 && !isHeader) appendText("\n");
        });
    }
    return Object.fromEntries(keys.map((key) => [key,
        normalizePromptDocument({ nodes: output[key] })]));
}

/**
 * The model node span each `---`-separated block occupies.
 *
 * `channelRegionsByNode` can only report a channel that APPEARS in the draft,
 * and `joinChannelDocuments` writes a heading only for a populated channel — so
 * a channel a chip feeds but the author has never typed in has no region, and
 * therefore nowhere for its contribution to be shown. This gives that case an
 * anchor: the end of the block it belongs to.
 */
export function writingBlockNodeRanges(documentValue) {
    const nodes = normalizePromptDocument(documentValue).nodes;
    const ranges = [{ block: 0, firstIndex: 0, lastIndex: -1 }];
    nodes.forEach((node, index) => {
        if (node.type === "text"
            && String(node.text || "").split("\n").some((line) => line.trim() === "---")) {
            // A break can share a node with the text on either side of it, so
            // the node counts as the tail of the closing block AND the head of
            // the next. Placement is node-granular; this is the same known
            // limit `channelRegionsByNode` documents.
            ranges.at(-1).lastIndex = index;
            ranges.push({ block: ranges.length, firstIndex: index, lastIndex: index });
            return;
        }
        ranges.at(-1).lastIndex = index;
    });
    return ranges;
}

/**
 * Where each channel's text stops in each block, as MODEL NODE INDICES.
 *
 * `splitPromptDocumentChannels` cannot answer this: `appendText` mints a fresh
 * `uid()` for every text node it emits and merges adjacent runs, so nothing in
 * its output maps back to a position in the source document. This walks the
 * same grammar — `key:` headings, and `---` starting a new block — and reports
 * one region per (block, channel) pair with the index of the last node that
 * contributed to it. That is what an inline decoration needs in order to sit
 * after the right region instead of at the end of the draft.
 *
 * Block-aware deliberately: the same channel key appears once per block, so a
 * single index per channel would pile every block's decorations onto the last
 * one.
 *
 * Node granularity is exact for a reconstructed draft, because
 * `joinChannelDocuments` emits each `key:` heading as its own text node. A
 * hand-edited draft can end up with two channels inside one node, and then both
 * report that node. Decorations therefore name their channel rather than
 * relying on position alone.
 */
export function channelRegionsByNode(documentValue, channelKeys, { defaultKey = "" } = {}) {
    const keys = Array.isArray(channelKeys) && channelKeys.length ? channelKeys : [];
    const keySet = new Set(keys);
    const fallback = keySet.has(defaultKey) ? defaultKey : keys[0] || "";
    const regions = [];
    let block = 0;
    let current = fallback;
    const seen = new Map();
    const mark = (index) => {
        if (!current) return;
        const key = `${block}::${current}`;
        const existing = seen.get(key);
        if (existing) { existing.afterIndex = index; return; }
        const region = { block, channelKey: current, afterIndex: index };
        seen.set(key, region);
        regions.push(region);
    };
    normalizePromptDocument(documentValue).nodes.forEach((node, index) => {
        if (node.type === "attachment") { mark(index); return; }
        for (const line of String(node.text || "").split("\n")) {
            if (line.trim() === "---") { block += 1; current = fallback; continue; }
            const match = line.match(/^([A-Za-z0-9_]+):[ \t]?(.*)$/);
            if (match && keySet.has(match[1])) {
                current = match[1];
                mark(index);
                continue;
            }
            if (line) mark(index);
        }
    });
    return regions;
}

/** Retarget channel documents without flattening inline attachment anchors. */
export function retargetChannelDocuments(channelDocuments, sourceKeys, targetKeys,
    fallbackChannels = {}) {
    const source = Array.isArray(sourceKeys) && sourceKeys.length ? sourceKeys : ["visual"];
    const target = Array.isArray(targetKeys) && targetKeys.length ? targetKeys : ["visual"];
    const allKeys = [...new Set([...source, ...Object.keys(channelDocuments || {}),
        ...Object.keys(fallbackChannels || {})])];
    const documents = Object.fromEntries(allKeys.map((key) => [key,
        normalizePromptDocument(channelDocuments?.[key], fallbackChannels?.[key] || "")]));
    if (!Object.values(documents).some((documentValue) => promptDocumentText(documentValue).trim()
        || documentValue.nodes.some((node) => node.type === "attachment"))) {
        return documents;
    }
    if (source.length === target.length && source.every((key) => target.includes(key))) {
        return documents;
    }
    const retargeted = splitPromptDocumentChannels(joinChannelDocuments(documents, source), target);
    Object.entries(documents).forEach(([key, documentValue]) => {
        if (key in retargeted) return;
        const textNode = documentValue.nodes.find((node) => node.type === "text");
        retargeted[key] = normalizePromptDocument({ nodes: [{ type: "text",
            node_id: textNode?.node_id || uid(), text: "" }] });
    });
    return retargeted;
}

export function normalizePromptAttachment(raw = {}) {
    const attachmentId = String(raw.attachment_id || "").trim() || uid();
    const kind = (String(raw.kind || "custom").trim() || "custom").slice(0, 64);
    const config = raw.config && typeof raw.config === "object"
        ? structuredClone(raw.config) : {};
    if (kind === "reference") {
        const overrides = config.overrides && typeof config.overrides === "object"
            && !Array.isArray(config.overrides) ? structuredClone(config.overrides) : {};
        for (const field of REFERENCE_OVERRIDE_FIELDS) {
            if (Object.hasOwn(config, field) && !Object.hasOwn(overrides, field)) {
                overrides[field] = config[field];
            }
            delete config[field];
        }
        config.overrides = overrides;
    }
    return {
        attachment_id: attachmentId,
        emission_group_id: String(raw.emission_group_id || "").trim() || attachmentId,
        kind,
        provider_id: String(raw.provider_id || "generic"),
        provider_version: String(raw.provider_version || "1"),
        enabled: raw.enabled !== false,
        source: raw.source && typeof raw.source === "object" ? structuredClone(raw.source) : {},
        config,
        capabilities: Array.isArray(raw.capabilities) ? structuredClone(raw.capabilities) : [],
        link_exportable: raw.link_exportable === true
            || (["prompt_link", "prompt_link_scope"].includes(kind)
                && raw.link_exportable !== false),
    };
}

export function normalizePromptAttachments(raw) {
    const result = [];
    const seen = new Set();
    for (const value of Array.isArray(raw) ? raw : []) {
        if (!value || typeof value !== "object") continue;
        const attachment = normalizePromptAttachment(value);
        if (seen.has(attachment.attachment_id)) attachment.attachment_id = uid();
        seen.add(attachment.attachment_id);
        result.push(attachment);
    }
    return result;
}

export function reusePromptAttachment(raw) {
    const source = normalizePromptAttachment(raw);
    return normalizePromptAttachment({
        ...structuredClone(source),
        attachment_id: uid(),
        emission_group_id: source.emission_group_id,
    });
}

export function unlinkPromptAttachment(raw) {
    const source = normalizePromptAttachment(raw);
    return normalizePromptAttachment({
        ...structuredClone(source),
        emission_group_id: uid(),
    });
}

export function propagateLinkedPromptAttachment(configuredRaw, targetRaw,
    sourceAttachmentId = "") {
    const configured = normalizePromptAttachment(configuredRaw);
    const target = normalizePromptAttachment(targetRaw);
    if (target.attachment_id === String(sourceAttachmentId || "")) {
        return { ...structuredClone(configured),
            attachment_id: target.attachment_id,
            emission_group_id: target.emission_group_id };
    }
    // Only the flags the target ACTUALLY STATED. Reading `enabled !== false`
    // off every record could not tell "inheriting" from "explicitly on".
    const targetEnabled = new Map();
    for (const value of target.capabilities || []) {
        const capabilityId = String(value?.capability_id || value?.kind || "");
        if (capabilityId && Object.hasOwn(value || {}, "enabled")) {
            targetEnabled.set(capabilityId, value.enabled !== false);
        }
    }
    const propagated = structuredClone(configured);
    propagated.capabilities = (propagated.capabilities || []).map((value) => {
        const capabilityId = String(value?.capability_id || value?.kind || "");
        const next = { ...value };
        // Per-section suppression is LOCAL and never propagates. The previous
        // shape restored the target's flag only when the target already held a
        // record for that capability — true while every attach seeded one, but
        // the moment records became sparse an inheriting target silently
        // adopted the source chip's suppression.
        delete next.enabled;
        if (targetEnabled.has(capabilityId)) {
            next.enabled = targetEnabled.get(capabilityId);
        }
        return next;
    });
    return { ...propagated,
        attachment_id: target.attachment_id,
        emission_group_id: target.emission_group_id };
}

export function setPromptAttachmentCapabilityEnabled(raw, projection, enabled) {
    const attachment = normalizePromptAttachment(raw);
    const capabilityId = String(projection?.capability_id || "");
    if (!capabilityId) return attachment;
    let found = false;
    attachment.capabilities = (attachment.capabilities || []).map((value) => {
        const currentId = String(value?.capability_id || value?.kind || "");
        if (currentId !== capabilityId) return value;
        found = true;
        return { ...value, enabled: Boolean(enabled) };
    });
    if (!found) {
        attachment.capabilities.push({
            capability_id: capabilityId,
            kind: String(projection?.capability_kind || capabilityId),
            // No `placement`: copying the declared value in here froze the
            // format default as if the author had chosen it, so a later format
            // change moved the routing panel while compiled output stayed put
            // and Reset had nothing to clear. Blank inherits.
            enabled: Boolean(enabled),
        });
    }
    return attachment;
}

function referenceMemberLookup(references) {
    const result = new Map();
    for (const reference of references || []) {
        for (const member of reference?.members || []) {
            const memberId = String(member?.member_id || "");
            if (memberId) result.set(memberId, { reference, member });
        }
    }
    return result;
}

function referenceSelectionMemberIds(selected, { scene = null, semanticUnits = [] } = {}) {
    const value = String(selected || "");
    // A physical selection names its member outright. Falling through to the
    // semantic-unit lookup below returned [] for every `physical:` value, which
    // made `resolveReferenceSelectionInheritance` report "no inherited
    // definition is available" while the member's prose sat in its own Defaults
    // panel — and left the physical branch of that notice unreachable.
    //
    // Deliberately NOT gated on setup mode or window verdict, unlike the
    // identity path below: the chip is already bound to this one member, the
    // source picker already disables ineligible options, and this answers
    // "where would this text come from", not "does it apply right now". Gating
    // would reintroduce the same false claim whenever the window moved.
    const physical = value.match(/^physical:[a-z][a-z0-9_]*:(.+)$/);
    if (physical) return [physical[1]];
    const unit = (semanticUnits || []).find((row) =>
        String(row?.semantic_unit_id || "") === value);
    if (!unit) return [];
    const referenceItems = scene?.reference_items || [];
    const recipes = scene?.reference_lane_recipes || [];
    const consumerStart = Number(scene?._context_consumer_start);
    const consumerEnd = Number(scene?._context_consumer_end);
    const hasSelection = Number.isFinite(consumerStart)
        && Number.isFinite(consumerEnd) && consumerEnd > consumerStart;
    const verdicts = resolveReferenceVerdicts({
        referenceItems,
        laneCount: scene?.reference_lane_count || 1,
        sceneDuration: scene?.duration_frames || 0,
        windowStart: hasSelection ? consumerStart : 0,
        windowEnd: hasSelection ? consumerEnd : (scene?.duration_frames || 0),
        laneConfigs: scene?.reference_lane_configs || [],
        frameThresholdPct: Number(scene?._context_reference_frame_threshold || 0),
    }).verdicts;
    const sourceIds = new Set((unit.sources || []).map((row) =>
        String(row?.member_id || "")).filter(Boolean));
    const result = [];
    // Lane membership is the recipe's declared model input, in lane order —
    // there is no setup registration to consult.
    for (const [laneIndex, wrapper] of recipes.entries()) {
        if (!lanePopulation(wrapper)) continue;
        referenceItems.forEach((item, itemIndex) => {
            if (Number(item?.lane_index || 0) !== laneIndex
                    || verdicts.get(itemIndex) !== "winner") return;
            for (const member of item?.members || []) {
                const memberId = String(member?.member_id || "");
                if (memberId && sourceIds.has(memberId) && !result.includes(memberId)) {
                    result.push(memberId);
                }
            }
        });
    }
    return result;
}

export function resolveReferenceSelectionInheritance(selected, {
    scene = null, references = [], semanticUnits = [],
} = {}) {
    const value = String(selected || "");
    const unit = (semanticUnits || []).find((row) =>
        String(row?.semantic_unit_id || "") === value);
    const unitDefinition = String(unit?.definition || "").trim();
    if (unitDefinition) {
        return { value: unitDefinition,
            source: `Subject “${unit?.name || unit?.semantic_unit_id || value}”` };
    }
    const members = referenceMemberLookup(references);
    const prompts = [];
    const sources = [];
    for (const memberId of referenceSelectionMemberIds(value, { scene, semanticUnits })) {
        const row = members.get(memberId);
        const prompt = String(row?.member?.prompt || "").trim();
        if (!prompt || prompts.includes(prompt)) continue;
        prompts.push(prompt);
        const entityName = String(row?.reference?.name || row?.reference?.reference_id || "Reference");
        const memberName = String(row?.member?.name || "").trim();
        sources.push(memberName ? `${entityName} · ${memberName}` : entityName);
    }
    return { value: prompts.join("; "),
        source: prompts.length ? `Library member${prompts.length === 1 ? "" : "s"} ${sources.join(" + ")}` : "" };
}

export function subjectSourceEligibility({ sources = [], referenceItems = [],
    laneRecipes = [], verdicts = new Map(), profileId = "generic@1",
    scope = {} } = {}) {
    const sourceIds = new Set((sources || []).map((source) =>
        String(source?.member_id || "")).filter(Boolean));
    if (!sourceIds.size) {
        return { eligible: true, appliesNow: true, reason: "", suffix: "" };
    }
    const states = [];
    (referenceItems || []).forEach((item, index) => {
        if (!(item?.members || []).some((member) =>
            sourceIds.has(String(member?.member_id || "")))) return;
        const wrapper = laneRecipes[Number(item?.lane_index || 0)] || {};
        const recipe = wrapper.recipe && typeof wrapper.recipe === "object"
            ? wrapper.recipe : wrapper;
        const compatible = recipe?.soft?.compatible_profiles || ["generic@1"];
        states.push({
            verdict: verdicts instanceof Map
                ? (verdicts.get(index) || "outside")
                : (verdicts?.[index] || "outside"),
            profileCompatible: compatible.includes(profileId),
        });
    });
    if (!states.length) {
        return { eligible: false, appliesNow: false,
            reason: "not staged in this scene", suffix: " - not staged in this scene" };
    }
    if (!states.some((state) => state.profileCompatible)) {
        return { eligible: false, appliesNow: false,
            reason: "incompatible prompt format", suffix: " - incompatible prompt format" };
    }
    const compatibleStates = states.filter((state) => state.profileCompatible);
    const appliesNow = compatibleStates.some((state) => state.verdict === "winner");
    const globalScope = Boolean(scope?.globalScope);
    const eligible = globalScope ? compatibleStates.length > 0 : appliesNow;
    if (globalScope) {
        return { eligible, appliesNow, reason: "",
            suffix: scope?.hasSelection && appliesNow ? " - applies now" : "" };
    }
    if (!appliesNow) {
        const verdict = compatibleStates[0]?.verdict || states[0]?.verdict || "outside";
        const reason = REFERENCE_VERDICT_LABEL[verdict] || verdict;
        return { eligible: false, appliesNow, reason, suffix: ` - ${reason}` };
    }
    return { eligible: true, appliesNow, reason: "", suffix: "" };
}

export function referencePromptDefaults(selected, {
    references = [], semanticUnits = [], profile = {}, capabilityKind = "",
    setupManifest = {},
} = {}) {
    const referenceDeclaration = profile?.capabilities?.reference || {};
    const formatName = String(profile?.name || profile?.profile_id || "Prompt Format");
    const formatSource = `Prompt Format default · ${formatName}`;
    const rendererFallbackSource = `Renderer fallback · ${formatName}`;
    const formatValues = {
        ...(referenceDeclaration?.defaults
            && typeof referenceDeclaration.defaults === "object"
            ? structuredClone(referenceDeclaration.defaults) : {}),
        ...(referenceDeclaration?.capability_defaults?.[capabilityKind]
            && typeof referenceDeclaration.capability_defaults[capabilityKind] === "object"
            ? structuredClone(referenceDeclaration.capability_defaults[capabilityKind]) : {}),
    };
    const formatFieldSources = Object.fromEntries(Object.keys(formatValues).map((field) => [
        field, authoritySource(formatSource, "format"),
    ]));
    const stagedRowsFor = (memberIds) => {
        const wanted = new Set((memberIds || []).map(String).filter(Boolean));
        if (!wanted.size) return [];
        return Object.values(setupManifest || {}).flatMap((rows) =>
            Array.isArray(rows) ? rows : []).filter((row) => wanted.has(String(
            row?.member_id || row?.video_member_id || "")));
    };
    const commonStagedValue = (rows, field) => {
        const values = [...new Set((rows || []).map((row) => String(row?.[field] || ""))
            .filter(Boolean))];
        return values.length === 1 ? values[0] : "";
    };
    const withSemanticFallback = (values, fieldSources, field, value, source) => {
        if (String(values[field] ?? "") || !String(value ?? "")) return;
        values[field] = structuredClone(value);
        fieldSources[field] = source;
    };
    const value = String(selected || "");
    const physical = value.match(/^physical:[a-z][a-z0-9_]*:(.+)$/);
    if (physical) {
        const memberId = physical[1];
        const rows = stagedRowsFor([memberId]);
        for (const reference of references || []) {
            const member = (reference?.members || []).find((row) =>
                String(row?.member_id || "") === memberId);
            if (!member) continue;
            const memberSource = `Physical Reference default · @${
                member.handle || member.member_id}`;
            const stagedSource = `Staged Reference default · @${
                member.handle || member.member_id}`;
            const entitySource = `Reference default · ${
                reference.name || reference.reference_id || "Reference"}`;
            const values = { ...formatValues };
            const fieldSources = { ...formatFieldSources };
            // The member's own defaults bag, mirroring the server's
            // `_common_member_attachment_defaults` tier.
            const memberDefaults = member.attachment_defaults
                && typeof member.attachment_defaults === "object"
                ? member.attachment_defaults : {};
            for (const [field, fieldValue] of Object.entries(memberDefaults)) {
                values[field] = structuredClone(fieldValue);
                fieldSources[field] = authoritySource(memberSource, "member");
            }
            // Authored Library prose is the definition of last resort, so a
            // blank chip FOLLOWS the member instead of emitting nothing.
            withSemanticFallback(values, fieldSources, "definition",
                String(member.prompt || "").trim(),
                authoritySource(memberSource, "member"));
            // Intent resolution mirrors the server setup manifest exactly:
            // staged item override, then member, then entity, then the
            // renderer's literal. Reading only the staged value and falling
            // straight to the literal ignored an authored entity default and
            // attributed it to a renderer fallback that never ran.
            for (const [field, literal] of [["visual_intent", "preserve"],
                ["audio_intent", "reference_characteristics"]]) {
                const staged = commonStagedValue(rows, field);
                const resolved = staged || String(member[field] || "")
                    || String(reference[field] || "") || literal;
                let source = authoritySource(rendererFallbackSource, "format");
                if (staged) source = authoritySource(stagedSource, "staged");
                else if (String(member[field] || "")) {
                    source = authoritySource(memberSource, "member");
                } else if (String(reference[field] || "")) {
                    source = authoritySource(entitySource, "entity");
                }
                withSemanticFallback(values, fieldSources, field, resolved, source);
            }
            return {
                values, source: memberSource, formatSource, fieldSources,
            };
        }
    }
    const unit = (semanticUnits || []).find((row) =>
        String(row?.semantic_unit_id || "") === value);
    if (unit) {
        const sharedValues = unit.attachment_defaults
            && typeof unit.attachment_defaults === "object"
            ? structuredClone(unit.attachment_defaults) : {};
        const sharedSource = `Shared identity default · @${
            unit.handle || unit.semantic_unit_id}`;
        // Contributing members sit BELOW the identity, matching the server's
        // `_common_member_attachment_defaults` tier: a field inherits only when
        // every contributing member states it and they agree, so conflicting
        // members fall through rather than one winning by ordering.
        const memberLookup = referenceMemberLookup(references);
        const contributing = (unit.sources || [])
            .map((source) => memberLookup.get(String(source?.member_id || "")))
            .filter(Boolean);
        const memberDefaultValues = {};
        const memberDefaultSources = {};
        if (contributing.length) {
            const bags = contributing.map(({ member }) =>
                (member?.attachment_defaults
                    && typeof member.attachment_defaults === "object")
                    ? member.attachment_defaults : {});
            const first = contributing[0];
            const memberSource = `Physical Reference default · @${
                first.member?.handle || first.member?.member_id || "member"}`;
            for (const field of REFERENCE_OVERRIDE_FIELDS) {
                const stated = bags.filter((bag) => Object.hasOwn(bag, field));
                if (stated.length !== bags.length || !stated.length) continue;
                const [head, ...rest] = stated.map((bag) => bag[field]);
                if (!rest.every((other) =>
                    JSON.stringify(other) === JSON.stringify(head))) continue;
                memberDefaultValues[field] = structuredClone(head);
                memberDefaultSources[field] = authoritySource(memberSource, "member");
            }
        }
        const values = { ...formatValues, ...memberDefaultValues, ...sharedValues };
        const fieldSources = { ...formatFieldSources, ...memberDefaultSources,
            ...Object.fromEntries(Object.keys(sharedValues).map((field) => [
                field, authoritySource(sharedSource, "identity"),
            ])) };
        const stagedRows = stagedRowsFor((unit.sources || []).map((source) =>
            source?.member_id));
        const stagedSource = `Staged Reference default · @${
            unit.handle || unit.semantic_unit_id}`;
        withSemanticFallback(values, fieldSources, "definition", unit.definition || "",
            authoritySource(sharedSource, "identity"));
        const sourceMemberIds = new Set((unit.sources || []).map((source) =>
            String(source?.member_id || "")).filter(Boolean));
        const stagedPrompts = [...new Set((setupManifest?.presentation || [])
            .filter((row) => sourceMemberIds.has(String(
                row?.member_id || row?.video_member_id || "")))
            .map((row) => String(row?.member_prompt || "").trim()).filter(Boolean))];
        withSemanticFallback(values, fieldSources, "definition", stagedPrompts.join("; "),
            authoritySource(stagedSource, "staged"));
        withSemanticFallback(values, fieldSources, "visual_intent",
            commonStagedValue(stagedRows, "visual_intent")
                || unit.visual_intent || "preserve",
            commonStagedValue(stagedRows, "visual_intent")
                ? authoritySource(stagedSource, "staged")
                : (unit.visual_intent
                    ? authoritySource(sharedSource, "identity")
                    : authoritySource(rendererFallbackSource, "format")));
        withSemanticFallback(values, fieldSources, "audio_intent",
            commonStagedValue(stagedRows, "audio_intent")
                || unit.audio_intent || "reference_characteristics",
            commonStagedValue(stagedRows, "audio_intent")
                ? authoritySource(stagedSource, "staged")
                : (unit.audio_intent
                    ? authoritySource(sharedSource, "identity")
                    : authoritySource(rendererFallbackSource, "format")));
        return {
            values,
            source: sharedSource, formatSource,
            fieldSources,
        };
    }
    return { values: formatValues, source: formatSource, formatSource,
        fieldSources: formatFieldSources };
}

/**
 * Shared default for a capability a chip states no opinion on.
 *
 * Mirrors `_inherited_capability_enabled` on the server. Whether a Reference
 * contributes a prompt part at all is a property of the Reference; WHERE that
 * part lands stays per-attachment. Identity beats member, and members are
 * consulted only when they all agree, so a disagreement falls through to
 * enabled rather than one member winning by ordering.
 */
export function resolveInheritedCapabilityEnabled(capabilityId, {
    selected = "", references = [], semanticUnits = [] } = {}) {
    const wanted = String(capabilityId || "");
    if (!wanted) return true;
    const value = String(selected || "");
    const unit = (semanticUnits || []).find((row) =>
        String(row?.semantic_unit_id || "") === value);
    if (unit) {
        return !(unit.disabled_capabilities || []).map(String).includes(wanted);
    }
    const physical = value.match(/^physical:[a-z][a-z0-9_]*:(.+)$/);
    if (!physical) return true;
    const found = referenceMemberLookup(references).get(physical[1]);
    if (!found) return true;
    return !(found.member?.disabled_capabilities || [])
        .map(String).includes(wanted);
}

export function referenceCapabilityInputProjection(capabilityKind, {
    selected = "", profile = {}, references = [], semanticUnits = [],
    overrides = {}, capabilityConfig = {}, setupManifest = {},
} = {}) {
    const kind = String(capabilityKind || "");
    const inherited = referencePromptDefaults(selected, {
        references, semanticUnits, profile, capabilityKind: kind, setupManifest,
    });
    return referenceCapabilityValueFields(profile, kind).map((field) => {
        const declaration = referenceFieldDeclaration(profile, kind, field) || {};
        const capabilityOwns = Object.hasOwn(capabilityConfig || {}, field);
        const chipOwns = Object.hasOwn(overrides || {}, field);
        const source = capabilityOwns || chipOwns
            ? authoritySource("Chip override", "chip")
            : (inherited.fieldSources?.[field]
                || authoritySource(inherited.formatSource, "format"));
        const value = capabilityOwns ? capabilityConfig[field]
            : (chipOwns ? overrides[field] : inherited.values?.[field]);
        const storedEmpty = (capabilityOwns || chipOwns)
            && (Array.isArray(value) ? value.length === 0 : String(value ?? "") === "");
        return {
            field,
            label: String(declaration?.label || REFERENCE_VALUE_LABELS[field] || field),
            value: structuredClone(value ?? ""),
            source: String(source?.label || inherited.formatSource),
            tier: String(source?.tier || "format"),
            authored_empty: storedEmpty,
            ...(storedEmpty ? { stored_empty: true } : {}),
        };
    });
}

export function resolveReferenceAttachmentIdentity(attachment, {
    scene = null, references = [], semanticUnits = [],
} = {}) {
    if (attachment?.kind !== "reference") return "";
    const unitNames = (attachment?.source?.semantic_unit_ids || []).map((id) => {
        const unit = (semanticUnits || []).find((row) =>
            String(row?.semantic_unit_id || "") === String(id));
        const handle = String(unit?.handle || "").trim();
        return handle ? `@${handle}`
            : String(unit?.name || unit?.semantic_unit_id || "").trim();
    }).filter(Boolean);
    if (unitNames.length) return [...new Set(unitNames)].join(" + ");
    const itemId = String(attachment?.source?.reference_item_id || "");
    if (itemId) {
        const item = (scene?.reference_items || []).find((row) =>
            String(row?.reference_item_id || "") === itemId);
        const names = (item?.members || []).map((row) => {
            const reference = (references || []).find((value) =>
                String(value?.reference_id || "") === String(row?.entity_id || ""));
            return String(reference?.name || reference?.reference_id || "").trim();
        }).filter(Boolean);
        if (names.length) return [...new Set(names)].join(" + ");
    }
    const physicalIds = [
        ...(attachment?.source?.picture_ids || []),
        ...(attachment?.source?.video_ids || []),
        ...(attachment?.source?.audio_ids || []),
    ].map(String);
    const members = referenceMemberLookup(references);
    const names = physicalIds.map((id) => {
        const row = members.get(id);
        const handle = String(row?.member?.handle || "").trim();
        if (handle) return `@${handle}`;
        const entityName = String(row?.reference?.name || row?.reference?.reference_id || "").trim();
        const memberName = String(row?.member?.name || "").trim();
        return memberName && entityName ? `${entityName} · ${memberName}` : (entityName || memberName);
    }).filter(Boolean);
    return [...new Set(names)].join(" + ");
}

export function attachmentReuseLabel(attachment, ctx = {}) {
    const kind = String(attachment?.kind || "custom");
    if (kind === "reference") {
        return ctx.attachmentLabelFor?.(attachment)
            || resolveReferenceAttachmentIdentity(attachment, ctx) || "Reference";
    }
    if (kind === "shot") return attachment?.config?.timestamp ? "Shot + time" : "Shot";
    if (kind === "timestamp") return "Time";
    if (kind === "custom") {
        const text = String(attachment?.config?.text || "").trim().replace(/\s+/g, " ");
        return text ? `Custom: ${text.slice(0, 40)}${text.length > 40 ? "…" : ""}` : "Custom";
    }
    if (["prompt_link", "prompt_link_scope"].includes(kind)) {
        if (!ctx.scene || !ctx.template) {
            return attachment?.config?.label || "Linked prompt";
        }
        const promptId = String(attachment?.source?.prompt_id || "");
        const sections = [...(ctx.scene?.prompt_sections || [])].sort((a, b) =>
            Number(a?.start_frame || 0) - Number(b?.start_frame || 0)
            || String(a?.prompt_id || "").localeCompare(String(b?.prompt_id || "")));
        const index = sections.findIndex((value) =>
            String(value?.prompt_id || "") === promptId);
        const section = sections[index];
        const sectionLabel = section
            ? `Section ${index + 1} (${section.start_frame}-${section.end_frame})`
            : "Unavailable section";
        const channelKeys = kind === "prompt_link_scope"
            ? (attachment?.source?.channel_keys || []).map(String)
            : [String(attachment?.source?.channel_key || "")].filter(Boolean);
        const labels = channelKeys.map((channelKey) =>
            (ctx.template?.channels || []).find((value) =>
                String(value?.key || "") === channelKey)?.label || channelKey);
        return `${kind === "prompt_link_scope" ? "Section prompt link" : "Prompt link"} → ${sectionLabel} · ${labels.join(" + ") || "all channels"}`;
    }
    if (kind === "vocal_event") {
        return `Vocal event: ${String(attachment?.config?.event_type || "speech").replaceAll("_", " ")}`;
    }
    return LABELS[kind] || "Context";
}

export function dedupeReusableAttachments(attachments = [], { scene = null } = {}) {
    const occurrenceById = new Map();
    const sections = [...(scene?.prompt_sections || [])].sort((a, b) =>
        Number(a?.start_frame || 0) - Number(b?.start_frame || 0)
        || String(a?.prompt_id || "").localeCompare(String(b?.prompt_id || "")));
    for (const [sectionIndex, section] of sections.entries()) {
        for (const attachment of normalizePromptAttachments(section?.attachments)) {
            occurrenceById.set(attachment.attachment_id, {
                start_frame: Number(section?.start_frame || 0),
                end_frame: Number(section?.end_frame || 0),
                section_index: sectionIndex,
                prompt_id: String(section?.prompt_id || ""),
            });
        }
    }
    const byGroup = new Map();
    for (const attachment of normalizePromptAttachments(attachments)) {
        const groupId = String(attachment.emission_group_id || attachment.attachment_id);
        const occurrence = occurrenceById.get(attachment.attachment_id) || {
            start_frame: Number.MAX_SAFE_INTEGER, end_frame: 0,
            section_index: -1, prompt_id: "",
        };
        const row = { attachment, ...occurrence };
        const existing = byGroup.get(groupId);
        if (!existing) {
            byGroup.set(groupId, { ...row, count: 1 });
            continue;
        }
        existing.count += 1;
        const compare = row.start_frame - existing.start_frame
            || row.prompt_id.localeCompare(existing.prompt_id)
            || attachment.attachment_id.localeCompare(existing.attachment.attachment_id);
        if (compare < 0) {
            existing.attachment = attachment;
            existing.start_frame = row.start_frame;
            existing.end_frame = row.end_frame;
            existing.section_index = row.section_index;
            existing.prompt_id = row.prompt_id;
        }
    }
    return [...byGroup.values()].sort((a, b) =>
        a.start_frame - b.start_frame
        || a.prompt_id.localeCompare(b.prompt_id)
        || a.attachment.attachment_id.localeCompare(b.attachment.attachment_id));
}

export function attachmentLabel(attachment, preview = "", identityLabel = "", context = null) {
    const base = LABELS[attachment?.kind] || "Context";
    if (attachment?.kind === "shot" && attachment?.config?.timestamp) {
        return "Shot + time";
    }
    if (attachment?.kind === "reference") {
        const identity = identityLabel || base;
        return preview ? `${identity} — ${preview}` : identity;
    }
    if (["prompt_link", "prompt_link_scope"].includes(attachment?.kind)) {
        if (context?.scene && context?.template) {
            return attachmentReuseLabel(attachment, context);
        }
        return attachment?.config?.label || "Linked prompt";
    }
    if (attachment?.kind === "vocal_event") {
        return attachment?.config?.event_type || base;
    }
    return preview || attachment?.config?.label || base;
}

/** Project one capability row back onto its stored record.
 *
 *  BOTH routing axes are sparse: `channel_key` and `placement` are written only
 *  when the author picked something other than "Provider default", so a blank
 *  keeps inheriting the format declaration and a format change re-routes every
 *  untouched capability. Writing either eagerly froze the routing at attach
 *  time and made Reset unreachable, because the stored copy was
 *  indistinguishable from a deliberate override.
 *
 *  Exported so this is a tested projection rather than logic buried in a
 *  save handler that no test can reach.
 */
export function sparseCapabilityRecord(current = {}, {
    capabilityId = "", enabled = true, inheritedEnabled = true,
    channelKey = "", placement = "" } = {}) {
    const capability = {
        ...current,
        capability_id: capabilityId,
        kind: current?.kind || capabilityId,
    };
    // `enabled` is as sparse as the routing below it: stored only when it
    // DEVIATES from the shared Reference/identity default. Writing it
    // unconditionally froze `true` into every record, which read as a
    // deliberate override and made a shared default inert — the same failure
    // the routing comment beside it describes.
    if (!!enabled === !!inheritedEnabled) delete capability.enabled;
    else capability.enabled = !!enabled;
    if (channelKey) capability.channel_key = channelKey;
    else delete capability.channel_key;
    if (placement) capability.placement = placement;
    else delete capability.placement;
    return capability;
}

function placementDisplayLabel(value, { renderedAtAnchor = false,
    hasAnchor = false, phaseCatalog = [] } = {}) {
    const placement = String(value || "section_prefix");
    if (placement === "inline") {
        return renderedAtAnchor || hasAnchor ? "Inline at cursor" : "After section prefixes";
    }
    return String((phaseCatalog || []).find((row) =>
        String(row?.value || "") === placement)?.label || "")
        || placement.replaceAll("_", " ").replace(/^./, (char) => char.toUpperCase());
}

function contextChipLabel(label, { inline = false } = {}) {
    const chipLabel = document.createElement("span");
    chipLabel.dataset.sonderContextChipLabel = "1";
    chipLabel.textContent = label;
    // A pill clamps to two lines inside a fixed-width capsule. A handle must
    // not: `-webkit-box` is BLOCK-level, and a block child inside an inline
    // parent forces a line break either side of it, so a handle mid-sentence
    // broke the paragraph it sat in. The clamp has nothing to clamp anyway —
    // a handle renders its identity alone, without the chip preview.
    chipLabel.style.cssText = inline
        ? "display:inline;min-width:0;overflow-wrap:anywhere;white-space:normal;"
        : "display:-webkit-box;min-width:0;overflow:hidden;"
            + "overflow-wrap:anywhere;white-space:normal;line-height:14px;"
            + "-webkit-box-orient:vertical;-webkit-line-clamp:2;";
    return chipLabel;
}

/** A handle reads as prose, not as a token.
 *
 *  Same node, same atomicity, no pill: Writing mode's premise is that the
 *  author sees a sentence, and a bordered capsule mid-clause defeats that. The
 *  element stays `contentEditable=false` with `role="button"` and a tab stop,
 *  so caret movement and keyboard reach are unchanged — only the chrome goes.
 */
function handleCss() {
    // Deliberately the SAME token as a chip's label, not a new colour: a
    // handle is chip text without the capsule, so it is the existing role
    // rendered differently rather than a role of its own. `font:inherit` is
    // what does the prose work — the chip's 10px sans is what made it read as
    // a token, more than the border did.
    return `display:inline;box-sizing:border-box;padding:0;margin:0;border:0;
        background:transparent;color:${CHIP_PALETTE.text};
        font:inherit;vertical-align:baseline;cursor:pointer;user-select:none;
        white-space:normal;`;
}

/** Whether a rendered label is a handle mention rather than a described chip. */
export function isHandleLabel(label) {
    return /^@\S/.test(String(label || ""));
}

function chipCss() {
    return `display:inline-flex;align-items:center;gap:3px;box-sizing:border-box;min-width:0;
        max-width:min(180px,calc(100% - 4px));padding:1px 6px;
        margin:0 2px;border:1px solid ${CHIP_PALETTE.border};border-radius:999px;
        background:${CHIP_PALETTE.background};
        color:${CHIP_PALETTE.text};font:10px/17px system-ui,sans-serif;vertical-align:baseline;cursor:pointer;
        user-select:none;white-space:normal;overflow:hidden;`;
}

/**
 * The one button factory for this module. Four sites hand-rolled near-identical
 * CSS before, which is how they drifted apart and why none of them had hover,
 * active or disabled states — an inline-styled control carries no `:disabled`
 * rule, so a bare `.disabled = true` reads as an enabled button that does
 * nothing. `pill` is the rounded variant the routing and handle controls use.
 */
function chipButton(label, title = "", {
    variant = "secondary", fontSize = "9px", padding = "3px 6px", pill = false,
} = {}) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    if (title) button.title = title;
    setButtonVariant(button, variant, {
        padding, fontSize, lineHeight: "1.3",
        radius: pill ? "999px" : "4px",
    });
    return button;
}

function editorCss(compact) {
    return `box-sizing:border-box;width:100%;min-width:${compact ? "80px" : "140px"};
        flex:0 0 auto;
        min-height:${compact ? "30px" : "48px"};max-height:${compact ? "100px" : "none"};
        overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;padding:5px 7px;
        border:1px solid ${COLORS.border};border-radius:4px;background:${COLORS.panel};color:${COLORS.text};
        font:11px/1.4 system-ui,sans-serif;outline:none;`;
}

function selectionPoint(root) {
    const selection = globalThis.getSelection?.();
    if (!selection?.rangeCount) return null;
    const range = selection.getRangeAt(0);
    if (!root.contains(range.startContainer)) return null;
    return range;
}

function setCaretAfter(node) {
    const selection = globalThis.getSelection?.();
    if (!selection) return;
    const range = document.createRange();
    range.setStartAfter(node);
    range.collapse(true);
    selection.removeAllRanges();
    selection.addRange(range);
}

function setCaretBefore(node) {
    const selection = globalThis.getSelection?.();
    if (!selection) return;
    const range = document.createRange();
    range.setStartBefore(node);
    range.collapse(true);
    selection.removeAllRanges();
    selection.addRange(range);
}

/**
 * A native contenteditable whose direct children are stable text spans and
 * non-editable attachment buttons. DOM normalization is deferred during IME.
 * `onChange` receives `{document, attachments, text}` after each transaction.
 */
export function createPromptDocumentEditor({
    document: initialDocument,
    text = "",
    attachments: initialAttachments = [],
    previews = {},
    attachmentLabelFor = null,
    attachmentContext = null,
    disabled = false,
    compact = false,
    ariaLabel = "Prompt channel",
    onChange = null,
    onActivateAttachment = null,
    getHostSnapshot = null,
    onRestoreHostSnapshot = null,
    // `(model) => [{ afterIndex, element }]`, re-run on every render.
    // Decorations are NOT document content: they are read-only prose the
    // host paints between model nodes, and `readDom` is built to look
    // straight through them. See `renderDecorations` below.
    decorations = null,
} = {}) {
    let model = normalizePromptDocument(initialDocument, text);
    let attachments = normalizePromptAttachments(initialAttachments);
    let composing = false;
    let rendering = false;
    let history = [];
    let future = [];
    const attachmentById = () => new Map(attachments.map((value) => [value.attachment_id, value]));

    const editor = document.createElement("div");
    editor.contentEditable = disabled ? "false" : "true";
    editor.dataset.sonderPromptBox = "1";
    editor.dataset.sonderPromptDocument = "1";
    editor.setAttribute("role", "textbox");
    editor.setAttribute("aria-label", ariaLabel);
    editor.setAttribute("aria-multiline", "true");
    editor.style.cssText = editorCss(compact);

    const directNodeFor = (container) => {
        const element = container?.nodeType === Node.TEXT_NODE
            ? container.parentElement : container;
        const direct = element === editor ? null : element?.closest?.("[data-node-id]");
        return direct && editor.contains(direct) ? direct : null;
    };
    const logicalTextOffset = (textNode, rawOffset) => String(textNode?.nodeValue || "")
        .slice(0, Math.max(0, rawOffset)).replaceAll("\u200b", "").length;
    const selectionBoundary = (container, offset) => {
        const direct = directNodeFor(container);
        if (direct) {
            if (direct.dataset.nodeType === "text") {
                const textNode = direct.firstChild;
                const logicalLength = String(direct.textContent || "").replaceAll("\u200b", "").length;
                const logicalOffset = container?.nodeType === Node.TEXT_NODE
                    ? logicalTextOffset(container, offset)
                    : (Number(offset) > 0 ? logicalLength : 0);
                return { node_id: direct.dataset.nodeId || "",
                    offset: Math.max(0, Math.min(logicalLength, logicalOffset)) };
            }
            return { node_id: direct.dataset.nodeId || "", offset: Number(offset) > 0 ? 1 : 0 };
        }
        if (container !== editor) return null;
        const childOffset = Math.max(0, Math.min(editor.childNodes.length, Number(offset) || 0));
        // Step over decorations in BOTH directions. A caret beside one has to
        // resolve to a real model node: a null bookmark makes `render()` drop
        // the caret, and the bug tracker records what that produced last time
        // — text inserted at position 0, landing inside chip spans.
        const modelSibling = (from, step) => {
            for (let index = from; index >= 0 && index < editor.childNodes.length;
                index += step) {
                const candidate = editor.childNodes[index];
                if (!(candidate instanceof HTMLElement)) continue;
                if (candidate.dataset.sonderDecoration === "1") continue;
                if (candidate.dataset.nodeId) return candidate;
                return null;
            }
            return null;
        };
        const next = modelSibling(childOffset, 1);
        if (next instanceof HTMLElement && next.dataset.nodeId) {
            return { node_id: next.dataset.nodeId, offset: 0 };
        }
        const previous = modelSibling(childOffset - 1, -1);
        if (previous instanceof HTMLElement && previous.dataset.nodeId) {
            const logicalLength = previous.dataset.nodeType === "text"
                ? String(previous.textContent || "").replaceAll("\u200b", "").length : 1;
            return { node_id: previous.dataset.nodeId, offset: logicalLength };
        }
        return null;
    };
    const selectionBookmark = () => {
        const range = selectionPoint(editor);
        if (!range) return null;
        const start = selectionBoundary(range.startContainer, range.startOffset);
        const end = selectionBoundary(range.endContainer, range.endOffset);
        return start && end ? { start, end } : null;
    };
    const resolveBoundary = (boundary) => {
        if (!boundary?.node_id) return null;
        const direct = [...editor.querySelectorAll("[data-node-id]")]
            .find((value) => value.dataset.nodeId === boundary.node_id);
        if (!direct) return null;
        if (direct.dataset.nodeType !== "text") {
            // Index within the LIVE child list, which now includes decorations,
            // so this stays correct without needing to discount them.
            const index = [...editor.childNodes].indexOf(direct);
            return index < 0 ? null : { container: editor,
                offset: index + (Number(boundary.offset) > 0 ? 1 : 0) };
        }
        const textNode = direct.firstChild;
        if (!textNode) return { container: direct, offset: 0 };
        const value = String(textNode.nodeValue || "");
        const wanted = Math.max(0, Number(boundary.offset) || 0);
        let logical = 0;
        let raw = 0;
        while (raw < value.length && logical < wanted) {
            if (value[raw] !== "\u200b") logical += 1;
            raw += 1;
        }
        return { container: textNode, offset: raw };
    };
    const restoreSelection = (bookmark) => {
        if (!bookmark) return false;
        // A modal can outlive the host editor when the prompt surface remounts.
        // Resolving node ids against that detached, internally consistent tree
        // would report success and let a stale editor publish a mutation.
        if (editor.isConnected === false) return false;
        const start = resolveBoundary(bookmark.start);
        const end = resolveBoundary(bookmark.end);
        const selection = globalThis.getSelection?.();
        if (!start || !end || !selection) return false;
        try {
            editor.focus({ preventScroll: true });
            const range = document.createRange();
            range.setStart(start.container, start.offset);
            range.setEnd(end.container, end.offset);
            selection.removeAllRanges();
            selection.addRange(range);
            return true;
        } catch (_error) {
            // A concurrent document replacement wins. Callers must not turn a
            // stale saved caret into an end-of-document insertion.
            return false;
        }
    };

    const snapshot = () => ({ document: structuredClone(model), attachments: structuredClone(attachments),
        selection: selectionBookmark(),
        host_state: typeof getHostSnapshot === "function"
            ? structuredClone(getHostSnapshot()) : undefined });
    const emit = (reason = "document") => onChange?.({
        document: structuredClone(model), attachments: structuredClone(attachments),
        text: promptDocumentText(model), reason,
    });
    const pushHistory = () => {
        history.push(snapshot());
        if (history.length > 100) history.shift();
        future = [];
    };

    // Decorations are the host's read-only prose, painted BETWEEN model nodes
    // and never part of the document. Everything about them is defensive:
    // `readDom` skips them, the text-span read strips them out of any span they
    // end up inside, chip adjacency steps over them, and they refuse focus and
    // selection so nothing an author types can land in one and be silently
    // eaten on the next render.
    let paintedDecorations = new Set();
    const decorationFor = () => {
        if (typeof decorations !== "function") return [];
        try {
            return decorations(structuredClone(model)) || [];
        } catch (_error) {
            // A host that throws while describing decorations must not take the
            // editor down with it: prose the author is typing outranks chrome.
            return [];
        }
    };
    const decorateElement = (element) => {
        element.dataset.sonderDecoration = "1";
        element.contentEditable = "false";
        element.tabIndex = -1;
        // `user-select:none` is what stops a selection dragged across the draft
        // from swallowing decoration text into a copy or a delete.
        element.style.userSelect = "none";
        element.style.webkitUserSelect = "none";
        // A caret could otherwise be placed inside by clicking, and anything
        // typed there would be invisible to `readDom` and erased by the next
        // render — a silent loss of the author's words. Refusing the mousedown
        // leaves the click on the prose behind it.
        if (element.dataset.sonderDecorationArmed !== "1") {
            element.dataset.sonderDecorationArmed = "1";
            element.addEventListener("mousedown", (event) => event.preventDefault());
        }
    };
    let decorationPlan = [];
    let lastPaintedIndex = -1;
    const paintDecorations = (nodeIndex, { trailing = false } = {}) => {
        for (const entry of decorationPlan) {
            const target = Number(entry?.afterIndex);
            const isTrailing = !Number.isFinite(target) || target >= model.nodes.length;
            if (trailing ? !isTrailing : (isTrailing || target !== nodeIndex)) continue;
            if (paintedDecorations.has(entry)) continue;
            paintedDecorations.add(entry);
            const element = entry?.element;
            if (!(element instanceof HTMLElement)) continue;
            decorateElement(element);
            editor.appendChild(element);
            lastPaintedIndex = nodeIndex;
        }
    };

    const render = (bookmark = selectionBookmark()) => {
        const ownedFocus = document.activeElement === editor || editor.contains(document.activeElement);
        rendering = true;
        editor.textContent = "";
        decorationPlan = decorationFor();
        paintedDecorations = new Set();
        const byId = attachmentById();
        model.nodes.forEach((node, nodeIndex) => {
            if (node.type === "text") {
                const span = document.createElement("span");
                span.dataset.nodeId = node.node_id;
                span.dataset.nodeType = "text";
                // The model index is stamped so `refreshDecorations` can place a
                // block exactly where `render()` would. Deriving it from the
                // child list instead drifts the moment a model node emits no
                // element — an attachment whose record is gone, or a bare text
                // node the browser leaves behind at an editor boundary.
                span.dataset.modelIndex = String(nodeIndex);
                // A zero-width character keeps an empty stable text node
                // addressable without entering the normalized projection.
                span.textContent = node.text || "\u200b";
                editor.appendChild(span);
                paintDecorations(nodeIndex);
                return;
            }
            const attachment = byId.get(node.attachment_id);
            if (!attachment) { paintDecorations(nodeIndex); return; }
            const chip = document.createElement("span");
            chip.contentEditable = "false";
            chip.dataset.nodeId = node.node_id;
            chip.dataset.nodeType = "attachment";
            chip.dataset.modelIndex = String(nodeIndex);
            chip.dataset.attachmentId = attachment.attachment_id;
            // The DOM is the round-trip carrier: whatever is not written here
            // is gone the next time `readDom` rebuilds the model from it.
            if (node.capability_id) chip.dataset.capabilityId = String(node.capability_id);
            // The identity WITHOUT the preview decides both the handle test and
            // what a handle renders. The preview is chip chrome — a pill has
            // room to describe itself, prose does not, and `@KWoman — <Subject
            // 1> is the korean woman…` mid-clause is not a mention. The full
            // label still carries the preview into the title and aria name,
            // where describing the chip is the point.
            const identityLabel = attachmentLabel(attachment, "",
                attachmentLabelFor?.(attachment) || "", attachmentContext);
            const label = attachmentLabel(attachment,
                previews?.[attachment.attachment_id] || "",
                attachmentLabelFor?.(attachment) || "", attachmentContext);
            const handle = isHandleLabel(identityLabel);
            chip.style.cssText = handle ? handleCss() : chipCss();
            chip.setAttribute("role", "button");
            chip.tabIndex = 0;
            chip.title = `${label} — dynamic context; activate to configure`;
            chip.setAttribute("aria-label", `${label} context chip; activate to configure`);
            const chipLabel = contextChipLabel(handle ? identityLabel : label,
                { inline: handle });
            // A visible cue that a chip opens an editor. The chip already had
            // `role="button"`, a tab stop, a pointer cursor and a title saying
            // so, but at rest it looked like a static token, and clicking it
            // was the ONLY way to reach the richest surface in the tool.
            const editGlyph = document.createElement("span");
            editGlyph.dataset.sonderChipEditAffordance = "1";
            editGlyph.textContent = "✎";
            editGlyph.setAttribute("aria-hidden", "true");
            editGlyph.style.cssText = "flex:0 0 auto;opacity:.55;font-size:9px;";
            const remove = document.createElement("button");
            remove.type = "button";
            remove.contentEditable = "false";
            remove.textContent = "×";
            remove.title = `Remove ${label} context chip`;
            remove.setAttribute("aria-label", `Remove ${label} context chip`);
            remove.style.cssText = "flex:0 0 auto;border:0;background:transparent;color:inherit;padding:0 1px;cursor:pointer;font:12px/1 system-ui;";
            remove.addEventListener("click", (event) => {
                event.preventDefault();
                event.stopPropagation();
                removeAttachment(attachment.attachment_id);
            });
            if (handle) {
                // `opacity:0` hides paint but keeps layout, so the glyphs left
                // roughly two blank characters after every handle mid-sentence.
                // Taking them out of flow removes that gap while keeping them
                // in the DOM, in the accessibility tree and focusable — which
                // `display:none` and `visibility:hidden` would not.
                const affordanceHost = document.createElement("span");
                affordanceHost.contentEditable = "false";
                affordanceHost.style.cssText = "position:absolute;top:0;left:100%;"
                    + "display:inline-flex;align-items:center;gap:2px;"
                    + "white-space:nowrap;z-index:1;";
                affordanceHost.append(editGlyph, remove);
                chip.style.cssText += "position:relative;";
                chip.append(chipLabel, affordanceHost);
                // Inline styles carry no `:hover` rule, so the reveal is wired
                // by hand. The affordances stay in the DOM and keep their
                // accessible names — only their paint is deferred, so a
                // screen reader and the keyboard path are unaffected.
                //
                // FOCUS ONLY, deliberately not hover. Out of flow they sit ON
                // TOP of the words after the mention, so revealing them on
                // hover put a live Remove button over prose the author was
                // about to click into — measured at 14-18px past the handle,
                // where a caret click lands. A mouse user reaches the same
                // controls by activating the chip, which opens its editor; a
                // keyboard user tabbing here is deliberately in the control and
                // is not about to click the sentence behind it. Reflowing the
                // sentence on hover instead was rejected: the text moving out
                // from under the pointer re-fires hover and flickers.
                const affordances = [editGlyph, remove];
                const reveal = (shown) => {
                    for (const element of affordances) {
                        element.style.opacity = shown ? "" : "0";
                        element.style.pointerEvents = shown ? "" : "none";
                    }
                };
                reveal(false);
                chip.addEventListener("focusin", () => reveal(true));
                chip.addEventListener("focusout", () => reveal(false));
            } else {
                chip.append(chipLabel, editGlyph, remove);
            }
            chip.addEventListener("click", (event) => {
                event.preventDefault();
                event.stopPropagation();
                onActivateAttachment?.(attachment, node);
            });
            chip.addEventListener("keydown", (event) => {
                if (!["Enter", " "].includes(event.key)) return;
                event.preventDefault();
                event.stopPropagation();
                onActivateAttachment?.(attachment, node);
            });
            editor.appendChild(chip);
            paintDecorations(nodeIndex);
        });
        paintDecorations(model.nodes.length - 1, { trailing: true });
        rendering = false;
        if (ownedFocus && bookmark) {
            editor.focus({ preventScroll: true });
            restoreSelection(bookmark);
        }
    };

    // The top-level skip is not enough on its own: `readDom` walks DIRECT
    // children, and a decoration that ends up nested inside a text span —
    // which editing can do — would be read as part of that span's text.
    // Backspace/Delete reach a neighbouring chip by DOM adjacency, and a
    // decoration painted between them is not a neighbour in the model. Left
    // unhandled it makes the chip undeletable from the keyboard and lets the
    // browser default run instead.
    const adjacentModelElement = (element, direction) => {
        let cursor = direction === "previous"
            ? element.previousElementSibling : element.nextElementSibling;
        while (cursor?.dataset?.sonderDecoration === "1") {
            cursor = direction === "previous"
                ? cursor.previousElementSibling : cursor.nextElementSibling;
        }
        return cursor?.dataset?.nodeType === "attachment" ? cursor : null;
    };
    const textWithoutDecorations = (element) => {
        if (!element.querySelector?.("[data-sonder-decoration]")) {
            return String(element.innerText || element.textContent || "")
                .replaceAll("\u200b", "");
        }
        const clone = element.cloneNode(true);
        for (const stray of clone.querySelectorAll("[data-sonder-decoration]")) {
            stray.remove();
        }
        return String(clone.innerText || clone.textContent || "")
            .replaceAll("\u200b", "");
    };
    const readDom = () => {
        if (rendering || composing) return;
        const previouslyAnchored = new Set(model.nodes
            .filter((node) => node.type === "attachment")
            .map((node) => node.attachment_id));
        const next = [];
        for (const child of [...editor.childNodes]) {
            if (child.nodeType === Node.TEXT_NODE) {
                const value = String(child.nodeValue || "").replaceAll("\u200b", "");
                if (value) next.push({ type: "text", node_id: uid(), text: value });
                continue;
            }
            if (!(child instanceof HTMLElement)) continue;
            // Host chrome, not document content. Without this the else-branch
            // below would absorb a whole rendered contribution into the
            // author's prose as text on the very next keystroke.
            if (child.dataset.sonderDecoration === "1") continue;
            if (child.dataset.nodeType === "attachment") {
                const node = { type: "attachment", node_id: child.dataset.nodeId || uid(),
                    attachment_id: child.dataset.attachmentId };
                if (child.dataset.capabilityId) node.capability_id = child.dataset.capabilityId;
                next.push(node);
            } else {
                next.push({ type: "text", node_id: child.dataset.nodeId || uid(),
                    text: textWithoutDecorations(child) });
            }
        }
        model = normalizePromptDocument({ nodes: next });
        const stillAnchored = new Set(model.nodes
            .filter((node) => node.type === "attachment")
            .map((node) => node.attachment_id));
        const removedInline = new Set([...previouslyAnchored]
            .filter((attachmentId) => !stillAnchored.has(attachmentId)));
        if (removedInline.size) attachments = attachments.filter((attachment) =>
            !removedInline.has(attachment.attachment_id));
        emit(removedInline.size ? "attachment" : "document");
    };

    editor.addEventListener("compositionstart", () => {
        if (!composing) pushHistory();
        composing = true;
    });
    editor.addEventListener("compositionend", () => {
        composing = false;
        readDom();
    });
    editor.addEventListener("beforeinput", (event) => {
        if (!composing && !String(event.inputType || "").startsWith("history")) pushHistory();
    });
    editor.addEventListener("input", readDom);
    editor.addEventListener("paste", (event) => {
        event.preventDefault();
        event.stopPropagation();
        // Through `insertText`, not `execCommand`: the same newline loss that
        // made "Split here" inert flattens a pasted draft. `insertText` pushes
        // its own history entry, so pasting stays ONE undo step, and it prunes
        // a chip record whose anchor the paste overwrote.
        const plain = event.clipboardData?.getData("text/plain") || "";
        editor.insertText(plain, { replaceSelection: true });
    });
    const ownedKeyHandlers = [];
    const handleOwnedKeydown = (event) => {
        // While an IME composition is open, Enter/Escape/Backspace belong to the
        // candidate window: Enter accepts a candidate, Escape cancels it, and
        // Backspace edits the reading. Claiming any of them here commits or
        // discards the prompt on a keystroke the user never aimed at it.
        // Returning falsy still yields PRESERVE_DEFAULT at the ownership root,
        // so propagation stops (graph/timeline shortcuts stay isolated) while
        // the browser's native IME default survives.
        if (composing || event.isComposing === true || event.keyCode === 229) {
            return false;
        }
        if (["Enter", " "].includes(event.key)
                && event.target?.dataset?.nodeType === "attachment") {
            event.preventDefault();
            const attachment = attachments.find((value) =>
                value.attachment_id === event.target.dataset.attachmentId);
            const node = model.nodes.find((value) => value.node_id === event.target.dataset.nodeId);
            if (attachment && node) onActivateAttachment?.(attachment, node);
            return true;
        }
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
            event.preventDefault();
            const previous = history.pop();
            if (!previous) return true;
            future.push(snapshot());
            model = previous.document;
            attachments = previous.attachments;
            if (previous.host_state !== undefined) {
                onRestoreHostSnapshot?.(structuredClone(previous.host_state));
            }
            render(previous.selection);
            emit("history");
            return true;
        } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "y") {
            event.preventDefault();
            const next = future.pop();
            if (!next) return true;
            history.push(snapshot());
            model = next.document;
            attachments = next.attachments;
            if (next.host_state !== undefined) {
                onRestoreHostSnapshot?.(structuredClone(next.host_state));
            }
            render(next.selection);
            emit("history");
            return true;
        } else if (["Backspace", "Delete"].includes(event.key)) {
            const range = selectionPoint(editor);
            if (range && !range.collapsed) {
                const selectedChips = [...editor.querySelectorAll(
                    '[data-node-type="attachment"]')].filter((value) => {
                    try { return range.intersectsNode(value); }
                    catch (_error) { return false; }
                });
                if (selectedChips.length) {
                    event.preventDefault();
                    pushHistory();
                    range.deleteContents();
                    readDom();
                    event.stopPropagation();
                    return true;
                }
            }
            let chip = event.target?.dataset?.nodeType === "attachment" ? event.target : null;
            if (!chip && range?.collapsed) {
                const host = range.startContainer.nodeType === Node.TEXT_NODE
                    ? range.startContainer.parentElement : range.startContainer;
                if (host?.dataset?.nodeType === "text") {
                    const length = String(host.textContent || "").replaceAll("\u200b", "").length;
                    if (event.key === "Backspace" && range.startOffset === 0) {
                        chip = adjacentModelElement(host, "previous");
                    } else if (event.key === "Delete" && range.startOffset >= length) {
                        chip = adjacentModelElement(host, "next");
                    }
                }
            }
            if (chip) {
                event.preventDefault();
                const nodeId = chip.dataset.nodeId;
                const attachmentId = chip.dataset.attachmentId;
                pushHistory();
                model.nodes = model.nodes.filter((value) => value.node_id !== nodeId);
                if (!model.nodes.some((value) => value.attachment_id === attachmentId)) {
                    attachments = attachments.filter((value) => value.attachment_id !== attachmentId);
                }
                model = normalizePromptDocument(model);
                render(); emit("attachment");
                return true;
            }
        } else if (["ArrowLeft", "ArrowRight"].includes(event.key)
            && event.target?.dataset?.nodeType === "attachment") {
            event.preventDefault();
            if (event.key === "ArrowLeft") setCaretBefore(event.target);
            else setCaretAfter(event.target);
            return true;
        }
        for (const handler of ownedKeyHandlers) {
            if (handler(event) === true) return true;
        }
        return false;
    };
    editor._sonderOwnedKeydown = handleOwnedKeydown;
    editor.addEventListener("keydown", (event) => {
        handleOwnedKeydown(event);
        event.stopPropagation();
    });

    /** Insert a chip, optionally replacing a run of text in `value` coordinates.
     *
     *  `replaceTextRange` exists for the mention menu: it has to remove the
     *  `@KWo` the author typed and put the chip in its place. Without it the
     *  literal query survives beside the chip — the very thing that made a
     *  typed mention compile as prose. Splicing the text out first is not an
     *  option either: that re-renders, drops the selection, and the chip then
     *  lands at the end of the document.
     */
    const insertAttachment = (rawAttachment, capabilityId = "", { replaceTextRange = null } = {}) => {
        if (disabled) return null;
        pushHistory();
        let replaced = null;
        if (replaceTextRange && Number.isFinite(replaceTextRange.start)
                && Number.isFinite(replaceTextRange.end)
                && replaceTextRange.end > replaceTextRange.start) {
            const from = modelPositionForTextOffset(replaceTextRange.start);
            const to = modelPositionForTextOffset(replaceTextRange.end);
            if (from && to) replaced = deleteModelSpan(from, to);
        }
        const attachment = normalizePromptAttachment(rawAttachment);
        attachments = [...attachments.filter((value) => value.attachment_id !== attachment.attachment_id), attachment];
        const node = { type: "attachment", node_id: uid(), attachment_id: attachment.attachment_id };
        if (capabilityId) node.capability_id = capabilityId;

        // Translate the active caret to a model insertion boundary through the
        // SAME resolver `insertText` uses. The hand-rolled copy this replaces
        // fell through to "append at the end" whenever the caret was anchored
        // on the editor element — the shape `focusFirst()` produces — so a
        // caret-menu attach after a programmatic focus placed the chip at the
        // bottom of the document instead of where the author was.
        const target = replaced
            ? { index: replaced.index, offset: replaced.offset }
            : modelPositionFor(selectionBookmark()?.start);
        let index = target ? target.index : model.nodes.length;
        if (target && model.nodes[target.index]?.type === "text") {
            const current = model.nodes[target.index];
            const offset = target.offset;
            const replacement = [];
            if (current.text.slice(0, offset)) replacement.push({ ...current, text: current.text.slice(0, offset) });
            replacement.push(node);
            if (current.text.slice(offset)) replacement.push({ type: "text", node_id: uid(), text: current.text.slice(offset) });
            model.nodes.splice(target.index, 1, ...replacement);
            render();
            const chip = editor.querySelector(`[data-node-id="${node.node_id}"]`);
            if (chip) setCaretAfter(chip);
            emit("attachment");
            return attachment;
        }
        if (target && target.offset > 0) index += 1;
        model.nodes.splice(index, 0, node);
        render();
        const chip = editor.querySelector(`[data-node-id="${node.node_id}"]`);
        if (chip) setCaretAfter(chip);
        emit("attachment");
        return attachment;
    };

    const removeAttachment = (attachmentId) => {
        pushHistory();
        model.nodes = model.nodes.filter((value) => value.attachment_id !== attachmentId);
        attachments = attachments.filter((value) => value.attachment_id !== attachmentId);
        model = normalizePromptDocument(model);
        render();
        emit("attachment");
    };

    Object.defineProperties(editor, {
        value: {
            get: () => promptDocumentText(model),
            set: (value) => {
                if (model.nodes.some((node) => node.type === "attachment")) {
                    throw new Error("structured_edit_conflict");
                }
                model = textPromptDocument(value, model.nodes[0]?.node_id);
                render();
            },
        },
        promptDocument: { get: () => structuredClone(model) },
        promptAttachments: { get: () => structuredClone(attachments) },
        promptState: {
            get: () => ({ document: structuredClone(model),
                attachments: structuredClone(attachments) }),
            set: (value) => {
                model = normalizePromptDocument(value?.document);
                attachments = normalizePromptAttachments(value?.attachments);
                render();
            },
        },
        promptSelection: {
            get: () => {
                const bookmark = selectionBookmark();
                return bookmark?.start ? structuredClone(bookmark.start) : null;
            },
        },
        // The caret as an offset into `value`, which `promptSelection` cannot
        // give because its offset is relative to one node. Attachment nodes
        // contribute no text, so they add nothing here either — keeping this
        // in step with `promptDocumentText` by construction. Null when the
        // caret is not in this editor.
        promptTextOffset: {
            get: () => {
                const bookmark = selectionBookmark();
                const start = bookmark?.start;
                if (!start) return null;
                let total = 0;
                for (const node of model.nodes) {
                    if (node.node_id === start.node_id) {
                        return total + (node.type === "text"
                            ? Math.max(0, Math.min(node.text.length, start.offset | 0))
                            : 0);
                    }
                    if (node.type === "text") total += node.text.length;
                }
                return null;
            },
        },
        disabled: {
            get: () => editor.contentEditable === "false",
            set: (value) => {
                editor.contentEditable = value ? "false" : "true";
                editor.setAttribute("aria-disabled", value ? "true" : "false");
            },
        },
    });
    editor.insertAttachment = insertAttachment;
    editor.capturePromptSelection = () => selectionBookmark();
    editor.restorePromptSelection = (bookmark) => restoreSelection(bookmark);
    editor.isPromptComposing = () => composing;
    // Deliberately NOT `execCommand("insertText")`. That path silently drops
    // newlines in this contenteditable, so multi-line insertions arrived
    // flattened — which is how "Split here" came to insert an inline `---`
    // that `splitWritingDraft` never recognized, making the button inert.
    // Writing the model directly also keeps insertion in the one
    // representation `readDom` can round-trip.
    /** Model position for a selection boundary produced by `selectionBoundary`.
     *
     *  `selectionBoundary` already resolves the hard cases — a caret anchored
     *  on the editor element itself resolves through the next/previous sibling
     *  rather than falling through to "append at the end", which is the bug
     *  three hand-rolled copies of this logic each carried.
     */
    const modelPositionFor = (boundary) => {
        if (!boundary?.node_id) return null;
        const index = model.nodes.findIndex((node) => node.node_id === boundary.node_id);
        if (index < 0) return null;
        const node = model.nodes[index];
        const offset = node.type === "text"
            ? Math.max(0, Math.min(node.text.length, Number(boundary.offset) || 0))
            : (Number(boundary.offset) > 0 ? 1 : 0);
        return { index, offset };
    };

    /** Model position for an offset into `value`.
     *
     *  Callers that work in text coordinates — the mention menu, which tracks a
     *  query as offsets into `promptDocumentText` — need this to address the
     *  model. Attachment nodes contribute no text, so they consume none of the
     *  offset, keeping this in step with `promptDocumentText` by construction.
     */
    const modelPositionForTextOffset = (offset) => {
        const target = Math.max(0, Number(offset) || 0);
        let seen = 0;
        for (let index = 0; index < model.nodes.length; index += 1) {
            const node = model.nodes[index];
            if (node.type !== "text") continue;
            if (target <= seen + node.text.length) {
                return { index, offset: target - seen };
            }
            seen += node.text.length;
        }
        const last = model.nodes.length - 1;
        return last >= 0 && model.nodes[last].type === "text"
            ? { index: last, offset: model.nodes[last].text.length } : null;
    };

    /** Delete everything between two model positions and report the join point.
     *
     *  An attachment inside the span goes with it, and its record is pruned —
     *  the same reconciliation `readDom` performs for the Backspace path
     *  (`range.deleteContents()` then `readDom()`). Leaving the record behind
     *  would strand it with no inline anchor, which compiles as a blocking
     *  `unanchored_inline_attachment` for inline-only kinds.
     */
    const deleteModelSpan = (from, to) => {
        const head = model.nodes[from.index];
        const tail = model.nodes[to.index];
        const removedIds = [];
        for (let i = from.index; i <= to.index && i < model.nodes.length; i += 1) {
            const node = model.nodes[i];
            if (node.type !== "attachment") continue;
            const wholly = i > from.index && i < to.index;
            if (wholly || (i === from.index && from.offset === 0)
                    || (i === to.index && to.offset > 0)) {
                removedIds.push(node.attachment_id);
            }
        }
        const headText = head?.type === "text" ? head.text.slice(0, from.offset) : "";
        const tailText = tail?.type === "text" ? tail.text.slice(to.offset) : "";
        const merged = { type: "text", node_id: head?.type === "text" ? head.node_id : uid(),
            text: headText + tailText };
        model.nodes.splice(from.index, to.index - from.index + 1, merged);
        if (removedIds.length) {
            const stillAnchored = new Set(model.nodes
                .filter((node) => node.type === "attachment")
                .map((node) => node.attachment_id));
            attachments = attachments.filter((value) =>
                stillAnchored.has(value.attachment_id)
                || !removedIds.includes(value.attachment_id));
        }
        return { index: from.index, offset: headText.length,
            removedAttachment: removedIds.length > 0 };
    };

    // Deliberately NOT `execCommand("insertText")`. That path silently drops
    // newlines in this contenteditable, so multi-line insertions arrived
    // flattened — which is how "Split here" came to insert an inline `---`
    // that `splitWritingDraft` never recognized, making the button inert.
    // Writing the model directly also keeps insertion in the one
    // representation `readDom` can round-trip.
    //
    // `replaceSelection` restores what `execCommand` used to do for free, but
    // only where the caller actually wants it. A writing aid that weaves the
    // selection into its output (`{text}`) wants a wrap; one that does not
    // would otherwise DELETE the author's paragraph and put an unrelated token
    // in its place, so the caller decides rather than this function guessing.
    /**
     * `asOwnNode` splits the text node instead of growing it.
     *
     * Growing is right for ordinary typing — it keeps the id the caret is
     * bookmarked against alive across the re-render. It is wrong for a section
     * break: `channelRegionsByNode` places decorations at node granularity, so
     * a `---` living inside a text node makes the blocks either side of it
     * report the SAME index, and both blocks' contributions stack under the
     * second. `joinWritingSectionDocuments` already emits the separator as its
     * own node, so this makes authoring match reconstruction.
     *
     * Safe because `normalizePromptDocument` does not merge adjacent text nodes
     * and `readDom` preserves each span's id. The caret is bookmarked to the
     * TAIL node explicitly, so it lands after the break rather than before it.
     */
    editor.insertText = (rawText, { replaceSelection = false, asOwnNode = false } = {}) => {
        if (disabled) return;
        const value = String(rawText ?? "");
        if (!value) return;
        editor.focus();
        const bookmark = selectionBookmark();
        const from = modelPositionFor(bookmark?.start);
        const to = modelPositionFor(bookmark?.end);
        pushHistory();
        let target = from;
        let removedAttachment = false;
        if (replaceSelection && from && to
                && (to.index > from.index || to.offset > from.offset)) {
            const result = deleteModelSpan(from, to);
            target = { index: result.index, offset: result.offset };
            removedAttachment = result.removedAttachment;
        }
        let caret = null;
        if (asOwnNode && target && model.nodes[target.index]?.type === "text") {
            const current = model.nodes[target.index];
            const head = current.text.slice(0, target.offset);
            const tail = current.text.slice(target.offset);
            const inserted = { type: "text", node_id: uid(), text: value };
            const tailNode = { type: "text", node_id: uid(), text: tail };
            current.text = head;
            // Head keeps its id so anything bookmarked against it survives; the
            // break and the tail are new nodes, which is the whole point.
            model.nodes.splice(target.index + 1, 0, inserted, tailNode);
            caret = { node_id: tailNode.node_id, offset: 0 };
        } else if (target && model.nodes[target.index]?.type === "text") {
            // Grow the existing text node rather than splitting it, so the id
            // the caret is bookmarked against survives the re-render.
            const current = model.nodes[target.index];
            current.text = current.text.slice(0, target.offset) + value
                + current.text.slice(target.offset);
            caret = { node_id: current.node_id, offset: target.offset + value.length };
        } else {
            const index = target ? target.index + (target.offset > 0 ? 1 : 0)
                : model.nodes.length;
            const node = { type: "text", node_id: uid(), text: value };
            model.nodes.splice(index, 0, node);
            caret = { node_id: node.node_id, offset: value.length };
        }
        model = normalizePromptDocument(model);
        render();
        restoreSelection({ start: caret, end: caret });
        emit(removedAttachment ? "attachment" : "document");
    };
    // Lets a host that mutates the document through `promptState` still
    // enrol the change in this editor's undo stack.
    editor.pushPromptHistory = () => pushHistory();
    editor.removeAttachment = removeAttachment;
    editor.replaceAttachment = (raw) => {
        pushHistory();
        const next = normalizePromptAttachment(raw);
        attachments = attachments.map((value) => value.attachment_id === next.attachment_id ? next : value);
        render();
        emit("attachment");
    };
    // A channel editor owns its document/history, while its host owns the one
    // scene attachment registry shared by every mounted channel.  Synchronize
    // registry changes without replacing the document or creating an undo step.
    editor.syncPromptAttachments = (raw) => {
        attachments = normalizePromptAttachments(raw);
        render();
    };
    editor.transactPromptAttachments = (raw, mutateHostState = null) => {
        pushHistory();
        if (typeof mutateHostState === "function") mutateHostState();
        attachments = normalizePromptAttachments(raw);
        render();
        emit("attachment");
    };
    editor.recordSharedAttachmentTransaction = (raw) => {
        pushHistory();
        attachments = normalizePromptAttachments(raw);
        render();
    };
    editor.addOwnedKeyHandler = (handler) => {
        if (typeof handler !== "function") return () => {};
        ownedKeyHandlers.push(handler);
        return () => {
            const index = ownedKeyHandlers.indexOf(handler);
            if (index >= 0) ownedKeyHandlers.splice(index, 1);
        };
    };
    editor.focusFirst = () => {
        editor.focus();
        const selection = globalThis.getSelection?.();
        if (selection) {
            const range = document.createRange();
            range.selectNodeContents(editor);
            range.collapse(true);
            selection.removeAllRanges();
            selection.addRange(range);
        }
    };
    /** Caret after all content — the safe landing spot for an unknown caret. */
    editor.focusEnd = () => {
        editor.focus();
        const selection = globalThis.getSelection?.();
        if (!selection) return;
        const range = document.createRange();
        range.selectNodeContents(editor);
        range.collapse(false);
        selection.removeAllRanges();
        selection.addRange(range);
    };
    /**
     * Repaint decorations only, leaving the document spans and the caret alone.
     *
     * The whole point of a separate entry: the compiled candidate lands on a
     * debounce while the author is still typing, and routing that through the
     * panel's `render()` would rebuild the draft element under the cursor —
     * destroying the caret and this editor's closure-local undo history. Model
     * children are untouched here; only `[data-sonder-decoration]` is removed
     * and re-inserted at its region.
     */
    editor.refreshDecorations = () => {
        if (rendering || composing) return;
        for (const stale of [...editor.querySelectorAll("[data-sonder-decoration]")]) {
            stale.remove();
        }
        decorationPlan = decorationFor();
        paintedDecorations = new Set();
        const stamped = [...editor.childNodes].filter((child) =>
            child instanceof HTMLElement && child.dataset.modelIndex !== undefined);
        // Same placement rule `render()` uses: a decoration follows the LAST
        // element at or before its model index. Indexing the child list
        // directly drifts whenever a model node emitted no element, and
        // inserting every block against the same anchor reverses two blocks
        // that share one index.
        // Two regions can share one index (a hand-edited draft can put two
        // channels in one node), so the anchor ADVANCES to whatever was last
        // inserted for that index — otherwise each block is inserted against the
        // same element and the pair comes out reversed.
        const cursors = new Map();
        const anchorFor = (target) => {
            if (cursors.has(target)) return cursors.get(target);
            let found = null;
            for (const child of stamped) {
                if (Number(child.dataset.modelIndex) > target) break;
                found = child;
            }
            return found;
        };
        for (const entry of decorationPlan) {
            const element = entry?.element;
            if (!(element instanceof HTMLElement)) continue;
            const target = Number(entry?.afterIndex);
            // `render()` never paints a negative or out-of-range index inline;
            // it falls to the trailing pass. Match that rather than inventing a
            // second rule here.
            const trailing = !Number.isFinite(target) || target < 0
                || target >= model.nodes.length;
            paintedDecorations.add(entry);
            decorateElement(element);
            const after = trailing ? null : anchorFor(target);
            if (after?.nextSibling) editor.insertBefore(element, after.nextSibling);
            else if (after) editor.appendChild(element);
            else if (!trailing && stamped.length) editor.insertBefore(element, stamped[0]);
            else editor.appendChild(element);
            if (!trailing) cursors.set(target, element);
        }
    };
    render();
    return editor;
}

export function promptInsertionBookmark(editor) {
    const bookmark = editor?.capturePromptSelection?.();
    if (!bookmark?.start) return bookmark || null;
    return { start: structuredClone(bookmark.start), end: structuredClone(bookmark.start) };
}

/**
 * What a writing aid inserts over, as distinct from where an attachment lands.
 *
 * An attachment collapses to a caret because a chip must never swallow authored
 * prose. A writing aid is the opposite case: selecting a line and applying
 * Dialogue means "wrap these words", so the live range is kept and its text
 * pre-fills `{text}`.
 *
 * The range is only usable when it stays inside a single text node. A selection
 * spanning a chip would otherwise contribute that chip's rendered label as
 * prose and then be replaced by the insert, destroying the attachment; those
 * fall back to the collapsed caret and insert without wrapping.
 */
export function promptSelectionSnapshot(editor) {
    const bookmark = editor?.capturePromptSelection?.() || null;
    const selection = globalThis.getSelection?.();
    if (!bookmark || !selection?.rangeCount || selection.isCollapsed) {
        return { bookmark: promptInsertionBookmark(editor), text: "" };
    }
    const range = selection.getRangeAt(0);
    const single = range.startContainer === range.endContainer
        && range.startContainer?.nodeType === Node.TEXT_NODE
        && editor?.contains?.(range.startContainer);
    if (!single) return { bookmark: promptInsertionBookmark(editor), text: "" };
    // Chips are rendered with zero-width markers; they are structure, not prose.
    const text = String(range.toString() || "").replaceAll("\u200b", "");
    if (!text) return { bookmark: promptInsertionBookmark(editor), text: "" };
    return { bookmark, text };
}

function restorePromptInsertion(editor, bookmark) {
    const restored = editor?.restorePromptSelection?.(bookmark);
    if (bookmark && restored === false) {
        globalThis.console?.warn?.(
            "[Sonder] Prompt changed while configuration was open; insertion was cancelled.");
        return false;
    }
    // No bookmark means the caret was never resolvable — an empty box, a click
    // on a chip, or a click in the padding below the text. `insertText` focuses
    // the editor, and a bare focus() collapses to the START of a contenteditable,
    // so returning success here silently inserted at position 0 and, against a
    // stale offset, spliced the text mid-word. Append instead: it is the
    // intuitive result when the caret is unknown, and it can never cut authored
    // prose in half.
    if (!bookmark) editor?.focusEnd?.();
    return true;
}

/**
 * The aids offered in one channel.
 *
 * A format declares `channel_keys` to say where an aid belongs: H3 dialogue and
 * camera prose belong to the timeline description, not to the soundscape,
 * music, definition, summary or retention channels. An aid that declares
 * nothing reaches every channel, so a format predating the key is unchanged,
 * and a caller with no channel of its own — the Writing draft box is one box
 * projecting all channels — sees the whole set.
 */
function promptWritingAids(writingAids, channelKey = "") {
    const aids = (Array.isArray(writingAids) ? writingAids : [])
        .filter((aid) => aid?.id);
    const key = String(channelKey || "");
    if (!key) return aids;
    return aids.filter((aid) => {
        const declared = Array.isArray(aid.channel_keys)
            ? aid.channel_keys.map(String) : [];
        return !declared.length || declared.includes(key);
    });
}

/**
 * The declared fields an aid's text actually substitutes, in declaration order.
 *
 * A declaration the text never references is not a question worth asking, and
 * `{text}` is handled separately because it is free prose rather than a bounded
 * vocabulary.
 */
function writingAidFields(aid) {
    const text = String(aid?.text || "");
    const declared = aid?.fields && typeof aid.fields === "object" ? aid.fields : {};
    return Object.entries(declared).filter(([name, declaration]) =>
        declaration && typeof declaration === "object" && text.includes(`{${name}}`));
}

function writingAidUsesText(aid) {
    return String(aid?.text || "").includes("{text}");
}

/**
 * Writing-aid fields share one declaration shape with `derived[].fields`, so a
 * declared `label` is authoritative. The raw field name is the last-resort
 * spelling, not the first choice.
 */
function writingAidFieldLabel(name, declaration) {
    return String(declaration?.label || "").trim() || String(name).replaceAll("_", " ");
}

function writingAidFieldOptional(declaration) {
    return declaration?.optional === true;
}

/**
 * Substitute declared values into an aid's text.
 *
 * An omitted optional field substitutes empty, which would otherwise leave the
 * doubled spaces and orphaned punctuation of `The camera pushes in  .`, so runs
 * of horizontal whitespace collapse and space before punctuation closes up.
 * Only spaces and tabs collapse: an aid may legitimately contain newlines, and
 * flattening those would rewrite authored prose rather than tidy it.
 */
function buildWritingAidText(aid, values = {}, textValue = "") {
    let value = String(aid?.text || "");
    for (const [name] of writingAidFields(aid)) {
        const chosen = values?.[name];
        const rendered = Array.isArray(chosen)
            ? chosen.filter(Boolean).join(", ")
            : String(chosen ?? "");
        value = value.replaceAll(`{${name}}`, rendered);
    }
    if (writingAidUsesText(aid)) {
        value = value.replaceAll("{text}", String(textValue ?? ""));
    }
    return value.replace(/[ \t]{2,}/g, " ").replace(/[ \t]+([.,;:!?])/g, "$1");
}

/**
 * Bounded writing-aid configuration.
 *
 * A single required enum resolves in the menu itself; this opens only when the
 * aid needs free text or more than one choice. It deliberately reuses the
 * backdrop/panel shape of `configurePromptAttachment`, including the modal
 * marker the prompt boxes' blur guards whitelist, so opening it never commits
 * the box behind it.
 */
function configureWritingAid(aid, fields, { needsText = false, text = "" } = {}) {
    return new Promise((resolve) => {
        const backdrop = document.createElement("div");
        backdrop.dataset.sonderPromptContextModal = "1";
        backdrop.style.cssText = "position:fixed;inset:0;z-index:12000;background:rgba(5,8,12,.72);display:flex;align-items:center;justify-content:center;padding:20px;";
        const panel = document.createElement("div");
        panel.style.cssText = `width:min(420px,92vw);max-height:82vh;overflow:auto;padding:14px;border:1px solid ${COLORS.border};border-radius:8px;background:${COLORS.panelRaised};box-shadow:0 18px 60px rgba(0,0,0,.55);display:flex;flex-direction:column;gap:10px;`;
        const heading = document.createElement("div");
        heading.textContent = String(aid?.label || aid?.id || "Writing aid");
        heading.style.cssText = `font:600 13px system-ui;color:${COLORS.text};`;
        const hint = document.createElement("div");
        hint.textContent = "Inserted at the cursor as authored text.";
        hint.style.cssText = `font:10px system-ui;color:${COLORS.textDim};`;
        panel.append(heading, hint);

        const controls = new Map();
        for (const [name, declaration] of fields) {
            const choices = declaredFieldChoices(declaration);
            const multiple = String(declaration?.type || "") === "enum_multi";
            let control;
            if (multiple) {
                control = selectField(choices.map((choice) => [choice.value, choice.label]));
                control.multiple = true;
                control.size = Math.min(6, Math.max(2, choices.length));
                for (const option of control.options) option.selected = false;
            } else {
                control = selectField([
                    ...(writingAidFieldOptional(declaration) ? [["", "— not set —"]] : []),
                    ...choices.map((choice) => [choice.value, choice.label]),
                ]);
            }
            controls.set(name, { control, multiple });
            panel.appendChild(fieldRow(
                writingAidFieldLabel(name, declaration), control,
                String(declaration?.help || "")));
        }
        let textControl = null;
        if (needsText) {
            textControl = textField(text, true);
            panel.appendChild(fieldRow("Text", textControl));
        }

        const actions = document.createElement("div");
        actions.style.cssText = "display:flex;justify-content:flex-end;gap:6px;margin-top:4px;";
        const cancel = chipButton("Cancel", "", { padding: "5px 10px", fontSize: "11px" });
        const confirm = chipButton("Insert", "",
            { variant: "accentSoft", padding: "5px 10px", fontSize: "11px" });
        actions.append(cancel, confirm);
        panel.appendChild(actions);
        backdrop.appendChild(panel);
        document.body.appendChild(backdrop);

        let releaseEscape = () => {};
        const finish = (value) => {
            releaseEscape();
            backdrop.remove();
            resolve(value);
        };
        releaseEscape = ownModalEscape(() => finish(null));
        const collect = () => {
            const values = {};
            for (const [name, entry] of controls) {
                values[name] = entry.multiple
                    ? [...entry.control.selectedOptions].map((option) => option.value)
                    : entry.control.value;
            }
            return { values, text: textControl ? textControl.value : text };
        };
        cancel.addEventListener("click", () => finish(null));
        confirm.addEventListener("click", () => finish(collect()));
        backdrop.addEventListener("mousedown", (event) => {
            if (event.target === backdrop) finish(null);
        });
        // Enter is handled on the panel: every control here is an ordinary form
        // control, which the timeline guards already treat as an editing target,
        // and no OVERLAY consumer claims Enter. Propagation still stops so it
        // does not reach the prompt box or the graph behind. Escape CANNOT live
        // here — see `ownModalEscape` for why an element listener never sees it.
        panel.addEventListener("keydown", (event) => {
            if (event.isComposing || event.keyCode === 229) return;
            if (event.key === "Enter" && event.target?.tagName !== "TEXTAREA") {
                event.preventDefault();
                event.stopPropagation();
                finish(collect());
            }
        });
        (panel.querySelector("select,textarea,input") || confirm)
            .focus?.({ preventScroll: true });
    });
}

/**
 * Own Escape for a modal opened OVER another OVERLAY surface.
 *
 * An element-level `keydown` on the modal is too late. `KeyboardOwnership`
 * listens at window CAPTURE, so a consumer already registered by the surface
 * behind — the Prompt tool, whose Escape closes it unconditionally — consumes
 * the key and calls `stopImmediatePropagation` before it ever reaches the
 * modal's own listener. What the user sees is the panel behind vanishing while
 * the dialog they were actually in stays put. Registering here instead makes
 * the modal the newest OVERLAY consumer, and same-priority dispatch is LIFO.
 *
 * Returns the unregister closure; every close path must call it.
 */
function ownModalEscape(onEscape) {
    return registerKeyboardConsumer({
        id: `sonder-prompt-modal-${uid()}`,
        priority: KEY_PRIORITY.OVERLAY,
        keydown: (event) => {
            if (event.isComposing === true || event.keyCode === 229) return false;
            if (event.key !== "Escape") return false;
            onEscape();
            return true;
        },
    });
}

/**
 * Restore the caret, insert the built text, and report it.
 *
 * The selection is replaced ONLY when the aid weaves it into its output via
 * `{text}` — that is what turns a selection plus `{text}` into a wrap rather
 * than an insertion in front of the words. An aid that ignores `{text}`
 * (scene transition, cutoff, camera motion, the framing aids) must leave the
 * author's prose alone; replacing it there would delete a paragraph and put an
 * unrelated token in its place.
 */
async function commitWritingAid(editor, bookmark, aid, values, textValue, onInserted) {
    const value = buildWritingAidText(aid, values, textValue);
    if (!restorePromptInsertion(editor, bookmark)) return;
    editor?.insertText?.(value, { replaceSelection: writingAidUsesText(aid) });
    await onInserted?.({ type: "writing_aid", text: value });
}

async function insertWritingAid(editor, snapshot, aid, onInserted) {
    const bookmark = snapshot?.bookmark || null;
    const fields = writingAidFields(aid);
    const selected = String(snapshot?.text || "");
    const needsText = writingAidUsesText(aid) && !selected;
    const unsupported = fields.filter(([, declaration]) =>
        !declaredFieldChoices(declaration).length);
    if (unsupported.length) {
        // Previously this returned silently and without restoring the caret, so
        // an aid the format could legally save simply did nothing.
        notifyWarning(
            `"${aid?.label || aid?.id}" declares ${unsupported.length === 1
                ? "a choice" : "choices"} with no values; fix the prompt format to use it.`,
            { source: "prompt-writing-aid" });
        restorePromptInsertion(editor, bookmark);
        return;
    }
    const configured = await configureWritingAid(aid, fields,
        { needsText, text: selected });
    if (!configured) { restorePromptInsertion(editor, bookmark); return; }
    await commitWritingAid(editor, bookmark, aid, configured.values,
        configured.text, onInserted);
}

/** Does (x, y) fall inside the editor's current non-collapsed selection? */
function promptPointInSelection(editor, x, y) {
    const selection = globalThis.getSelection?.();
    if (!selection?.rangeCount || selection.isCollapsed) return false;
    const range = selection.getRangeAt(0);
    if (!editor?.contains?.(range.startContainer)) return false;
    for (const rect of range.getClientRects?.() || []) {
        if (x >= rect.left && x <= rect.right
            && y >= rect.top && y <= rect.bottom) return true;
    }
    return false;
}

function setPromptCaretFromPoint(editor, x, y) {
    let range = document.caretRangeFromPoint?.(x, y) || null;
    if (!range && document.caretPositionFromPoint) {
        const position = document.caretPositionFromPoint(x, y);
        if (position) {
            range = document.createRange();
            range.setStart(position.offsetNode, position.offset);
            range.collapse(true);
        }
    }
    if (!range || !editor.contains(range.startContainer)) return false;
    const selection = globalThis.getSelection?.();
    if (!selection) return false;
    selection.removeAllRanges();
    selection.addRange(range);
    editor.focus({ preventScroll: true });
    return true;
}

function promptCaretRect(editor) {
    const selection = globalThis.getSelection?.();
    if (selection?.rangeCount) {
        const range = selection.getRangeAt(0).cloneRange();
        if (editor.contains(range.startContainer)) {
            range.collapse(true);
            const rect = range.getClientRects?.()[0] || range.getBoundingClientRect?.();
            if (rect && (rect.width || rect.height || rect.left || rect.top)) {
                return { x: rect.left, y: rect.bottom || rect.top };
            }
        }
    }
    const rect = editor.getBoundingClientRect();
    return { x: rect.left + 8, y: rect.top + 8 };
}

/**
 * Kinds a format actually offers, on top of what the surface allows.
 *
 * `custom` is the model-agnostic escape hatch — fixed text plus whatever
 * bounded fields a format declares for it — so it belongs to formats that
 * declare `capabilities.custom` and to no others. None of the three built-ins
 * declares one, so it disappears from Generic and both MiniMax formats while
 * remaining available to any format that wants it.
 *
 * An ABSENT profile is "no opinion", not "declares nothing": the catalog is
 * async and a surface that has not resolved one yet must not silently drop a
 * kind the format really does declare. Every real call site passes a profile.
 *
 * Only `custom` is gated, deliberately. `reference` is in the same formal
 * position — H3 Base declares no `capabilities.reference` yet every surface
 * still offers it — but gating it would hide a kind whose absence is already
 * reported as `undeclared_reference_capability` at compile time, trading a
 * legible diagnostic for a silently missing menu row. Custom has no such
 * diagnostic, which is why the gate is where the gate is.
 */
export function promptContextGatedKinds(kinds, profile = null) {
    const declared = profile && typeof profile === "object"
        ? profile.capabilities : null;
    if (!declared || typeof declared !== "object") return kinds;
    return kinds.filter((kind) => kind !== "custom" || Boolean(declared.custom));
}

export function promptContextAuthoringKinds(allowedKinds = AUTHORING_KINDS,
    profile = null) {
    return promptContextGatedKinds(
        (Array.isArray(allowedKinds) ? allowedKinds : AUTHORING_KINDS)
            .filter((kind) => AUTHORING_KINDS.includes(kind)
                && !SCOPE_ONLY_KINDS.includes(kind)),
        profile);
}

/** The literal text an aid inserts, for the menu's secondary column. */
function writingAidPreview(aid) {
    const text = String(aid?.text || "").replace(/\s+/g, " ").trim();
    return text.length > 42 ? `${text.slice(0, 41)}…` : text;
}

/**
 * One writing aid as a menu row, shaped by how much it still needs to know.
 *
 * Nothing left to ask inserts on the spot; one bounded choice resolves in a
 * submenu, which is what the values were declared for; free text or several
 * choices earns the dialog. A single `enum_multi` also takes the dialog — a
 * menu row cannot express picking two of five.
 */
function writingAidMenuItem(editor, selection, aid, onInserted) {
    const label = String(aid.label || aid.id || "Aid");
    const writingAidId = String(aid.id || "");
    const hint = writingAidPreview(aid);
    const fields = writingAidFields(aid);
    const text = String(selection?.text || "");
    const bookmark = selection?.bookmark || null;
    const needsText = writingAidUsesText(aid) && !text;
    const multiple = fields.some(([, declaration]) =>
        String(declaration?.type || "") === "enum_multi");
    const undeclared = fields.some(([, declaration]) =>
        !declaredFieldChoices(declaration).length);
    if (undeclared || needsText || multiple || fields.length > 1) {
        return { label, writingAidId, hint,
            action: () => insertWritingAid(editor, selection, aid, onInserted) };
    }
    if (!fields.length) {
        return { label, writingAidId, hint,
            action: () => commitWritingAid(
                editor, bookmark, aid, {}, text, onInserted) };
    }
    const [name, declaration] = fields[0];
    const choose = (value) => commitWritingAid(
        editor, bookmark, aid, { [name]: value }, text, onInserted);
    return {
        label, writingAidId, hint,
        submenu: [
            ...(writingAidFieldOptional(declaration)
                ? [{ label: "— not set —", action: () => choose("") }] : []),
            ...declaredFieldChoices(declaration).map((choice) => ({
                label: choice.label,
                hint: String(choice.description || ""),
                action: () => choose(choice.value),
            })),
        ],
    };
}

/**
 * `@KWoman.speaker` → `{handle: "KWoman", qualifier: "speaker"}`.
 *
 * The dotted suffix is the CAPABILITY QUALIFIER: the explicit way to say which
 * declared capability a handle seeds when the channel's own answer is not the
 * one wanted. Splitting on the first dot is unambiguous because a handle cannot
 * contain one (`PROMPT_HANDLE_RE` in `server/prompt_context.py` is
 * `[A-Za-z][A-Za-z0-9_]{0,63}`), so everything after it is the qualifier.
 *
 * Pure text → parts. Whether the qualifier names a real capability is
 * `handleAttachCapabilityKind`'s question, not this one's.
 */
export function parseHandleMention(text) {
    const raw = String(text || "").trim().replace(/^@/, "");
    const dot = raw.indexOf(".");
    if (dot < 0) return { handle: raw, qualifier: "" };
    return { handle: raw.slice(0, dot), qualifier: raw.slice(dot + 1) };
}

/**
 * Mention rows for a typeahead: an attachable source that HAS a spelling.
 *
 * `promptReferenceSourceOptions` is the authority on what may be attached and
 * why, but its rows carry no handle — values are `physical:<pop>:<member_id>`
 * or a semantic unit id, and a handle lives on the member or the unit. This is
 * that join, written explicitly rather than hidden behind "just reuse it".
 *
 * A source with no handle is DROPPED, not shown greyed: a typeahead completes
 * a spelling, and a source without one has nothing to type. Ineligible sources
 * are kept with their reason so the menu explains rather than omits, matching
 * the caret menu — but they cannot be accepted.
 */
export function promptMentionCandidates({ options = null, references = [],
    semanticUnits = [] } = {}) {
    const handleByMember = new Map();
    for (const reference of references || []) {
        for (const member of reference?.members || []) {
            const id = String(member?.member_id || "");
            const handle = String(member?.handle || "");
            if (id && handle) handleByMember.set(id, handle);
        }
    }
    const handleByUnit = new Map();
    for (const unit of semanticUnits || []) {
        const id = String(unit?.semantic_unit_id || "");
        const handle = String(unit?.handle || "");
        if (id && handle) handleByUnit.set(id, handle);
    }
    const rows = [];
    const seen = new Set();
    for (const [value, label, eligible] of [
        ...(options?.unitOptions || []), ...(options?.physicalOptions || [])]) {
        const raw = String(value || "");
        const handle = raw.startsWith("physical:")
            ? handleByMember.get(raw.split(":").slice(2).join(":")) || ""
            : handleByUnit.get(raw) || "";
        if (!handle || seen.has(handle)) continue;
        seen.add(handle);
        rows.push({ handle, label: String(label || handle),
            value: raw, eligible: eligible !== false });
    }
    return rows;
}

/**
 * The `@mention` under the caret, or null when there is none in progress.
 *
 * Bounded by the same grammar the handle itself uses, so a sigil in ordinary
 * prose cannot open a menu: the run after `@` must be handle-shaped, and an
 * `@` with a space or another sigil before the caret is just text. The dotted
 * qualifier is reported separately, so completing `@KWoman.spea` offers
 * capability kinds rather than handles.
 */
export function writingMentionQuery(text, offset) {
    const value = String(text ?? "");
    const caret = Math.max(0, Math.min(value.length, offset | 0));
    const before = value.slice(0, caret);
    const at = before.lastIndexOf("@");
    if (at < 0) return null;
    // A mention starts at a boundary. The test is whether the `@` follows a
    // WORD character — that is what makes `bob@example` an address. Anything
    // else may precede it: prose reaches `@` after a full stop, a quote, a
    // dash or an opening bracket far more often than after a bare space, and
    // an allowlist of openers silently refused most real sentences.
    if (at > 0 && /[A-Za-z0-9_]/.test(before[at - 1])) return null;
    const run = before.slice(at + 1);
    if (!/^[A-Za-z][A-Za-z0-9_]{0,63}(\.[A-Za-z0-9_]{0,63})?$|^$/.test(run)) return null;
    const dot = run.indexOf(".");
    return {
        start: at,
        end: caret,
        handle: dot < 0 ? run : run.slice(0, dot),
        qualifier: dot < 0 ? "" : run.slice(dot + 1),
        completingQualifier: dot >= 0,
    };
}

/**
 * Candidates for an in-progress mention, best first.
 *
 * Prefix matches rank above interior ones so typing the start of a name does
 * what it looks like it does, and ties fall back to the order the host gave —
 * never alphabetical, which would silently reorder a host's own ranking.
 */
export function handleMentionCandidates(query, options = []) {
    const needle = String(query || "").toLowerCase();
    const scored = [];
    (options || []).forEach((option, index) => {
        const value = String(typeof option === "string" ? option : option?.handle || "");
        if (!value) return;
        const hay = value.toLowerCase();
        const position = hay.indexOf(needle);
        if (needle && position < 0) return;
        scored.push({
            // The WHOLE row is carried through. Returning only `{handle,label}`
            // silently dropped `value` — the source id an accept needs to build
            // the attachment — so a completed mention produced a chip whose
            // source was empty and which therefore referenced nothing at all.
            row: typeof option === "string"
                ? { handle: value, label: value }
                : { ...option, handle: value, label: String(option?.label || value) },
            rank: needle ? (position === 0 ? 0 : 1) : 0,
            index,
        });
    });
    return scored
        .sort((a, b) => a.rank - b.rank || a.index - b.index)
        .map(({ row }) => row);
}

/**
 * The capability kind a handle attach seeds, or "" for the format default.
 *
 * A handle is an ATTACHMENT rendered as text, and which declared capability it
 * seeds decides where its output goes. Three authorities, most specific first:
 *
 * 1. **The dotted qualifier** — explicit, and only honoured when the format
 *    actually declares that kind. An undeclared qualifier falls through rather
 *    than seeding a kind nothing can compile.
 * 2. **The channel being written in** — the capability declaring this
 *    `channel_key`, lowest `order` winning a tie (`summary` beats
 *    `audio_relationship`, which share `summary` under H3 Full Reference).
 *    Attaching while writing in `subject_definitions` used to seed `mentions`,
 *    whose declared route is `detailed_description`, so the chip emitted into a
 *    channel other than the one it was placed in.
 * 3. **`mentions`** — the prose default, for a channel no capability claims.
 *
 * Declaration-driven throughout: `generic@1` declares only `derived_prompt`, so
 * a `visual` attach resolves to it by channel and every other channel answers
 * "" and takes the format default.
 */
const MENTION_CAPABILITY_KIND = "mentions";
export function handleAttachCapabilityKind(profile,
    { channelKey = "", qualifier = "" } = {}) {
    const declared = orderedReferenceDerived(profile);
    const kinds = new Set(declared.map(([kind]) => kind));
    if (qualifier && kinds.has(qualifier)) return qualifier;
    const key = String(channelKey || "");
    if (key) {
        // `declared` is already ordered by (order, kind), so the first match IS
        // the lowest-order claimant.
        const claimed = declared.find(([, declaration]) =>
            String(declaration?.channel_key || "") === key);
        if (claimed) return claimed[0];
    }
    return kinds.has(MENTION_CAPABILITY_KIND) ? MENTION_CAPABILITY_KIND : "";
}

/**
 * The `capabilities` array a handle attach stores — usually empty.
 *
 * Records stay SPARSE: the compiler already falls back to the lowest-`order`
 * declared capability (`_default_capability`), so storing that same kind writes
 * an authored deviation where the author deviated from nothing. Only a kind
 * that differs from the format default is worth a record. `capability_id` and
 * `kind` only — no `channel_key`/`placement`, which would freeze this chip's
 * routing at attach time, and no `enabled`, which would resolve the tri-state
 * out of inheriting its Reference or identity default.
 */
export function handleAttachCapabilityRecord(profile, options = {}) {
    const kind = handleAttachCapabilityKind(profile, options);
    if (!kind) return [];
    // Deliberately NOT `orderedReferenceDerived(profile)[0]`. That helper reads
    // a missing `order` as 0, putting such a capability FIRST, while the server's
    // `_default_capability` reads it as MAX_CAPABILITIES and puts it LAST. For
    // display ordering the difference is cosmetic; here it decides whether a
    // record is written at all, and guessing wrong means the chip silently
    // compiles as a capability the author never chose. So mirror the server's
    // rule exactly — `(order ?? last, kind)` — and omit a record only when the
    // two genuinely agree. Storing one is always semantically correct; it is
    // only less sparse, which is the safe direction to err in.
    const MAX_ORDER = Number.MAX_SAFE_INTEGER;
    let defaultKind = "";
    let best = null;
    for (const [candidate, declaration] of orderedReferenceDerived(profile)) {
        const order = Number.isFinite(Number(declaration?.order))
            ? Number(declaration.order) : MAX_ORDER;
        if (best === null || order < best
                || (order === best && candidate < defaultKind)) {
            best = order; defaultKind = candidate;
        }
    }
    if (kind === defaultKind) return [];
    return [{ capability_id: kind, kind }];
}

/**
 * The `Reference` row: attach a handle directly, or open the full dialog.
 *
 * Physical References now carry the same prompt defaults an identity does, so a
 * chip inserted with no overrides resolves to real text — which is what makes a
 * one-click attach worth offering at all. The dialog stays one row away, and
 * remains reachable from the chip itself afterwards, so this demotes it from a
 * toll gate to an edit step rather than removing it.
 *
 * Ineligible sources stay visible and dimmed with the reason the Attach dialog
 * would have given, because "my Reference is missing" is a worse question than
 * "why is it greyed out".
 */
function referenceAttachItem(editor, bookmark, referenceContext, onCreate,
    onInserted, channelKey = "") {
    const openDialog = async () => {
        const attachment = normalizePromptAttachment({ kind: "reference" });
        const configured = onCreate ? await onCreate(attachment) : attachment;
        if (!restorePromptInsertion(editor, bookmark)) return;
        if (configured) {
            editor?.insertAttachment?.(configured);
            await onInserted?.({ type: "attachment", attachment: configured });
        }
    };
    if (!referenceContext) {
        return { label: LABELS.reference, kind: "reference", action: openDialog };
    }
    const { options, usesDeclaredSources, unitOptions, physicalOptions } =
        promptReferenceSourceOptions(referenceContext);
    // Inferred from the channel this menu was opened in. No dotted qualifier is
    // reachable from a menu row — a typed `@handle.capability` is what supplies
    // one — so this is the channel-inferred half of the same resolution.
    const seededCapabilities = handleAttachCapabilityRecord(
        referenceContext.resolvedProfile, { channelKey });
    const attach = (value) => async () => {
        const attachment = applyPromptReferenceSource(
            normalizePromptAttachment({ kind: "reference" }), value);
        // No overrides either, so the chip follows its Reference or identity
        // defaults instead of freezing a copy of them.
        if (seededCapabilities.length) {
            attachment.capabilities = structuredClone(seededCapabilities);
        }
        if (!restorePromptInsertion(editor, bookmark)) return;
        editor?.insertAttachment?.(attachment);
        await onInserted?.({ type: "attachment", attachment });
    };
    const row = ([value, label, eligible]) => ({
        label: String(label),
        disabled: !eligible,
        action: eligible ? attach(value) : undefined,
    });
    // Identities and physical sources stay visibly separate, as they are
    // everywhere else in the tool.
    const grouped = usesDeclaredSources
        ? [...unitOptions.map(row),
            ...(unitOptions.length && physicalOptions.length
                ? [{ type: "separator" }] : []),
            ...physicalOptions.map(row)]
        : options.map(row);
    return {
        label: LABELS.reference,
        kind: "reference",
        submenu: [
            { label: "Configure…", action: openDialog },
            ...(grouped.length ? [{ type: "separator" }, ...grouped] : []),
        ],
    };
}

export function createPromptContextMenuItems({ editor, bookmark = null,
    selection = null, allowedKinds = AUTHORING_KINDS, onCreate = null,
    writingAids = [], channelKey = "", profile = null,
    referenceContext = null, onInserted = null } = {}) {
    const kinds = promptContextAuthoringKinds(allowedKinds, profile);
    const declared = promptWritingAids(writingAids);
    const aids = promptWritingAids(writingAids, channelKey);
    // An attachment always lands at a collapsed caret so a chip cannot swallow
    // authored prose; only a writing aid consults the live range.
    const aidSelection = selection || { bookmark, text: "" };
    return [{
        label: "Insert at cursor",
        submenu: kinds.map((kind) => kind === "reference"
            ? referenceAttachItem(editor, bookmark, referenceContext, onCreate,
                onInserted, channelKey)
            : {
                label: LABELS[kind] || kind,
                kind,
                action: async () => {
                    const attachment = normalizePromptAttachment({ kind });
                    const configured = onCreate ? await onCreate(attachment) : attachment;
                    if (!restorePromptInsertion(editor, bookmark)) return;
                    if (configured) {
                        editor?.insertAttachment?.(configured);
                        await onInserted?.({ type: "attachment", attachment: configured });
                    }
                },
            }),
    }, {
        label: "Writing aid",
        disabled: !aids.length,
        // A row that is empty because this channel declares no aids is a
        // different thing from a format with none at all, and saying which
        // beats a dead disabled row that looks broken either way.
        hint: aids.length || !declared.length ? ""
            : `None for ${channelKey}`,
        submenu: aids.map((aid) =>
            writingAidMenuItem(editor, aidSelection, aid, onInserted)),
    }];
}

/** Install right-click and keyboard Context authoring on one prompt box. */
export function installPromptContextMenu({ editor, allowedKinds = AUTHORING_KINDS,
    onCreate = null, writingAids = [], channelKey = "", profile = null,
    referenceContext = null, onInserted = null } = {}) {
    if (!editor) return () => {};
    let closeMenu = null;
    const open = ({ x, y, pointCaret = false } = {}) => {
        if (editor.isPromptComposing?.()) return false;
        // Right-clicking inside an existing selection must keep it: moving the
        // caret to the click point first would discard the very range a writing
        // aid is about to wrap. Clicking anywhere else still repositions.
        if (pointCaret && !promptPointInSelection(editor, x, y)) {
            setPromptCaretFromPoint(editor, x, y);
        }
        const bookmark = promptInsertionBookmark(editor);
        const selection = promptSelectionSnapshot(editor);
        closeMenu?.();
        closeMenu = openContextMenu({ x, y, items: createPromptContextMenuItems({
            editor, bookmark, selection, allowedKinds, onCreate, writingAids,
            channelKey, profile,
            // Resolved per open, not per install: the window, active setup and
            // Reference set all change under a mounted editor, and a snapshot
            // taken at install time would offer last week's verdicts.
            referenceContext: typeof referenceContext === "function"
                ? referenceContext() : referenceContext,
            onInserted,
        }) });
        return true;
    };
    const onContextMenu = (event) => {
        event.preventDefault();
        event.stopPropagation();
        open({ x: event.clientX, y: event.clientY, pointCaret: true });
    };
    editor.addEventListener("contextmenu", onContextMenu);
    const removeOwned = editor.addOwnedKeyHandler?.((event) => {
        if (editor.isPromptComposing?.() || event.isComposing || event.keyCode === 229) return false;
        if (event.key === "ContextMenu" || (event.key === "F10" && event.shiftKey)) {
            const anchor = promptCaretRect(editor);
            open(anchor);
            return true;
        }
        return false;
    }) || (() => {});
    const cleanup = () => {
        closeMenu?.();
        closeMenu = null;
        editor.removeEventListener("contextmenu", onContextMenu);
        removeOwned();
    };
    editor._sonderPromptContextMenuCleanup = cleanup;
    return cleanup;
}

function fieldRow(label, control, help = "", { visibleHelp = false } = {}) {
    const row = document.createElement("label");
    row.style.cssText = `display:grid;grid-template-columns:130px minmax(0,1fr);gap:8px;align-items:start;font:11px system-ui;color:${COLORS.text};`;
    const title = document.createElement("span");
    title.textContent = label;
    control.setAttribute?.("aria-label", label);
    if (help) {
        title.title = help;
        control.title = help;
        control.setAttribute?.("aria-description", help);
    }
    row.append(title, control);
    if (help && visibleHelp) {
        const description = document.createElement("span");
        description.textContent = help;
        description.style.cssText = `grid-column:2;font:9px/1.35 system-ui;color:${COLORS.textDim};margin-top:-4px;`;
        row.appendChild(description);
    }
    return row;
}

function textField(value = "", multiline = false) {
    const control = document.createElement(multiline ? "textarea" : "input");
    control.value = String(value ?? "");
    control.style.cssText = `box-sizing:border-box;width:100%;${
        chromeInputCss({ padding: "5px 7px" })}`;
    if (multiline) control.rows = 3;
    return control;
}

/**
 * A bounded multiple choice as checkboxes.
 *
 * `<select multiple>` requires ctrl/shift-click to pick more than one and
 * silently drops the whole selection on a plain click, which is the wrong
 * affordance for "which of these apply". Exposes the same `.value` /
 * `.selectedOptions` shape the previous control did, so readers do not care
 * which one they are holding.
 */
function checkboxListField(options, selected = [], { allLabel = "All" } = {}) {
    const host = document.createElement("div");
    host.style.cssText = `box-sizing:border-box;width:100%;display:flex;flex-direction:column;gap:3px;max-height:150px;overflow:auto;padding:5px 7px;border:1px solid ${COLORS.border};border-radius:5px;background:${COLORS.panel};`;
    const chosen = new Set((selected || []).map(String));
    const boxes = [];
    const actions = document.createElement("div");
    actions.style.cssText = "display:flex;gap:6px;";
    const all = chipButton(allLabel, `Select every ${allLabel.toLowerCase()}`);
    const none = chipButton("None", "Clear the selection");
    actions.append(all, none);
    host.appendChild(actions);
    for (const option of options) {
        const pair = Array.isArray(option) ? option : [option, option];
        const row = document.createElement("label");
        row.style.cssText = `display:flex;align-items:center;gap:6px;font:11px system-ui;color:${COLORS.text};cursor:pointer;`;
        const box = document.createElement("input");
        box.type = "checkbox";
        box.value = String(pair[0]);
        box.checked = chosen.has(String(pair[0]));
        const text = document.createElement("span");
        text.textContent = String(pair[1]);
        row.append(box, text);
        host.appendChild(row);
        boxes.push(box);
    }
    all.addEventListener("click", () => {
        for (const box of boxes) box.checked = true;
    });
    none.addEventListener("click", () => {
        for (const box of boxes) box.checked = false;
    });
    Object.defineProperty(host, "selectedOptions", {
        get: () => boxes.filter((box) => box.checked),
    });
    Object.defineProperty(host, "options", { get: () => boxes });
    // A group of checkboxes needs its own accessible name, and the validation
    // path focuses this control when the selection is empty — a bare div would
    // silently swallow both.
    host.setAttribute("role", "group");
    host.focus = () => boxes[0]?.focus?.({ preventScroll: true });
    return host;
}

function selectField(options, value = "") {
    const select = document.createElement("select");
    select.style.cssText = `box-sizing:border-box;width:100%;cursor:pointer;${
        chromeInputCss({ padding: "5px 7px" })}`;
    for (const optionValue of options) {
        const option = document.createElement("option");
        const pair = Array.isArray(optionValue) ? optionValue : [optionValue, optionValue];
        option.value = String(pair[0]);
        option.textContent = String(pair[1]);
        select.appendChild(option);
    }
    select.value = String(value || select.options[0]?.value || "");
    return select;
}

/**
 * One declaration-driven fieldset for Reference override authoring.
 *
 * Every surface that authors these values builds its rows here, so a format's
 * declared labels, help, and field set cannot be re-declared per surface. Three
 * gates decide whether a row exists at all:
 *
 *  1. the Prompt Format must declare the capability that owns the field, or the
 *     author would fill in a control whose output can never be routed anywhere;
 *  2. the field must be inheritable (`REFERENCE_OVERRIDE_FIELDS`), or it could
 *     only ever be a chip override, never a shared default; and
 *  3. an enum field must have a declared vocabulary, since the renderer has no
 *     value list of its own to offer.
 *
 * The caller composes layout through `rows`, not through a prebuilt container:
 * the chip editor interleaves non-overridable controls (source, speaker, token
 * strip) between these rows, and emitting them as one block would move controls
 * the author already knows the position of.
 */
export function createReferenceOverrideFieldset({
    profile = {}, references = [], semanticUnits = [], setupManifest = {},
    overrides = {}, selected = "", onOverrideChange = null,
    disclosureMemory = null, disclosureKey = "reference_overrides",
} = {}) {
    const working = overrides && typeof overrides === "object"
        ? structuredClone(overrides) : {};
    const overriddenFields = new Set(Object.keys(working));
    let currentSelection = String(selected || "");

    const inheritedFor = (field) => referencePromptDefaults(currentSelection, {
        references, semanticUnits, profile, setupManifest,
        capabilityKind: REFERENCE_VALUE_CAPABILITY[field] || "",
    });
    const effectiveValue = (field, fallback = "") => {
        if (overriddenFields.has(field)) return working[field];
        const values = inheritedFor(field).values || {};
        return Object.hasOwn(values, field) ? values[field] : fallback;
    };
    const setControlValue = (field, control, value) => {
        if (control.multiple) {
            const chosen = new Set((Array.isArray(value) ? value : [])
                .map(String));
            [...control.options].forEach((option) => {
                option.selected = chosen.has(option.value);
            });
            return;
        }
        control.value = String(value ?? "");
    };
    const readControlValue = (field, control) => (control.multiple
        ? [...(control.selectedOptions || [])]
            .map((option) => option.value).filter(Boolean)
        : control.value);

    const buildControl = (field, declaration) => {
        if (REFERENCE_ENUM_FIELDS.has(field)) {
            if (!declaration) return null;
            const inheritLabel = REFERENCE_INHERIT_CHOICE_FIELDS.has(field)
                ? "Inherit staged/entity default" : "";
            const selectedValue = effectiveValue(field,
                declaration.type === "enum_multi" ? [] : "");
            const choices = declaredFieldChoices(declaration)
                .map((entry) => [entry.value, entry.label]);
            const saved = new Set((Array.isArray(selectedValue)
                ? selectedValue : [selectedValue]).map(String).filter(Boolean));
            for (const value of saved) {
                if (!choices.some(([known]) => known === value)) {
                    choices.push([value, `Unsupported saved value: ${value}`]);
                }
            }
            if (inheritLabel) choices.unshift(["", inheritLabel]);
            const control = selectField(choices,
                Array.isArray(selectedValue) ? "" : selectedValue);
            if (declaration.type === "enum_multi") {
                control.multiple = true;
                control.size = Math.min(6, Math.max(2, choices.length));
                setControlValue(field, control, selectedValue);
            }
            return control;
        }
        return textField(effectiveValue(field),
            REFERENCE_MULTILINE_FIELDS.has(field));
    };

    const controls = new Map();
    const rows = new Map();
    const statuses = new Map();
    const fields = [];

    const refreshOverrideStatus = (field) => {
        const status = statuses.get(field);
        const control = controls.get(field);
        if (!status || !control) return;
        const isOverride = overriddenFields.has(field);
        const inherited = inheritedFor(field);
        const source = inherited.fieldSources?.[field]
            || authoritySource(inherited.formatSource, "format");
        status.state.textContent = isOverride ? "Chip override" : source.label;
        status.state.dataset.sonderAuthorityTier = isOverride ? "chip" : source.tier;
        // Authority reads as a greyscale ramp plus weight, never as hue. Accent
        // belongs to interaction and warm/orange belongs to status, so neither
        // is available to say "this field deviates" — and a hue that means
        // neither of those two things is exactly how a permanent warm row tint
        // came to read as a permanent error.
        //
        // TWO steps, not three. A third grey for the format tier measured 1.89:1
        // against its neighbour and 2.81:1 against the panel — indistinguishable
        // and barely readable at 9px. Which default a field follows is carried
        // precisely by the label text ("Physical Reference default · @Anna"), so
        // colour only answers what the label cannot: is this mine, or inherited?
        // `tier` stays three-valued on the dataset for anything reading the DOM.
        //
        // The inherited step is `textSecondary`, not `textDim`. This label sits
        // under EVERY field, so it carries much of the panel's structure, and at
        // fg2 it measured 4.77:1 — half the 9.34:1 the hand-rolled colour it
        // replaced had. Separation from the override step is then only 1.56:1,
        // which is fine: the weight jump does that work, and the two states say
        // different words. Legibility of the label matters more than contrast
        // against a state it cannot be confused with.
        status.state.style.color = isOverride ? COLORS.text : COLORS.textSecondary;
        status.state.style.fontWeight = isOverride ? "600" : "400";
        control.style.opacity = isOverride ? "1" : ".78";
        setButtonDisabled(status.reset, !isOverride);
    };

    for (const [kind, capabilityDeclaration] of orderedReferenceDerived(profile)) {
        for (const field of referenceCapabilityValueFields(profile, kind)) {
            if (!REFERENCE_OVERRIDE_FIELDS.includes(field)) continue;
            if (controls.has(field)) continue;
            const declaration = referenceFieldDeclaration(profile, kind, field);
            const control = buildControl(field, declaration);
            if (!control) continue;
            controls.set(field, control);
            fields.push(field);

            // Declared field label wins, then the renderer's own name for its
            // own floor field. The capability's label names the CAPABILITY and
            // is reused in the routing block, so it is not a field name — but
            // its `help` is declared guidance meant to sit beside a control,
            // and is the only guidance a floor field has.
            const label = String(declaration?.label
                || REFERENCE_VALUE_LABELS[field] || field);
            const help = String(declaration?.help
                || capabilityDeclaration?.help || "");

            const wrapper = document.createElement("div");
            wrapper.style.cssText = "display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px;align-items:start;";
            const reset = document.createElement("button");
            reset.type = "button";
            reset.textContent = "Reset";
            reset.title = "Delete this chip override and follow the current default.";
            setButtonVariant(reset, "secondary",
                { padding: "3px 6px", fontSize: "9px", lineHeight: "1.3" });
            const state = document.createElement("span");
            state.style.cssText = "grid-column:1/-1;font:9px/1.25 system-ui;";
            statuses.set(field, { state, reset });

            const markOverride = () => {
                overriddenFields.add(field);
                refreshOverrideStatus(field);
                applyDisclosure();
                onOverrideChange?.(field);
            };
            control.addEventListener(control.multiple ? "change" : "input", markOverride);
            if (control.tagName === "SELECT" && !control.multiple) {
                control.addEventListener("change", markOverride);
            }
            reset.addEventListener("click", () => {
                overriddenFields.delete(field);
                delete working[field];
                setControlValue(field, control, effectiveValue(field,
                    control.multiple ? [] : ""));
                refreshOverrideStatus(field);
                applyDisclosure();
                onOverrideChange?.(field);
            });
            wrapper.append(control, reset, state);
            rows.set(field, fieldRow(label, wrapper, help, { visibleHelp: true }));
            refreshOverrideStatus(field);
        }
    }

    // Progressive disclosure by TIER STATE, not by category. Rendering every
    // declared field as an equal editable row put ~35 controls in front of an
    // author whose chip usually deviates in none of them, which is what made
    // this the most intimidating surface in the tool. Fields that are following
    // collapse to one line; an actual override always stays visible, because
    // hiding a deviation is how an author loses track of one.
    let expanded = disclosureMemory?.isOpen(disclosureKey, false) ?? false;
    const summaryRow = document.createElement("div");
    summaryRow.dataset.sonderInheritedSummary = "1";
    summaryRow.style.cssText = `grid-column:1/-1;display:flex;gap:6px;align-items:baseline;justify-content:space-between;padding:4px 6px;border:1px dashed ${COLORS.border};border-radius:5px;`;
    const summaryText = document.createElement("span");
    // Same ramp as the per-field status: this line describes fields that are
    // following, which is the inherited rung.
    summaryText.style.cssText = `font:9px/1.35 system-ui;color:${COLORS.textSecondary};min-width:0;`;
    const summaryToggle = chipButton("", "", { padding: "3px 7px" });
    summaryToggle.style.flex = "0 0 auto";
    summaryRow.append(summaryText, summaryToggle);

    const inheritingFields = () => fields.filter(
        (field) => !overriddenFields.has(field));
    const applyDisclosure = () => {
        const following = inheritingFields();
        for (const [field, row] of rows) {
            row.style.display = (expanded || overriddenFields.has(field))
                ? "grid" : "none";
        }
        // Name the MOST SPECIFIC source in play, not a count. Mixed tiers are
        // the normal case for a physical member — format defaults for most
        // fields, a member default for one — and "3 sources" told the author
        // nothing about which of them was theirs. Ranking is why `rank` exists:
        // `tier` collapses member, staged, entity and identity into one value.
        const sources = new Map();
        for (const field of following) {
            const inherited = inheritedFor(field);
            const source = inherited.fieldSources?.[field]
                || authoritySource(inherited.formatSource, "format");
            const label = String(source.label || "");
            if (!label) continue;
            const rank = Number(source.rank) || REFERENCE_AUTHORITY_RANK.format;
            if (rank > (sources.get(label) ?? -1)) sources.set(label, rank);
        }
        const ranked = [...sources].sort((left, right) => right[1] - left[1]);
        const origin = ranked.length
            ? `${ranked[0][0]}${ranked.length > 1 ? ` +${ranked.length - 1} more` : ""}`
            : "its defaults";
        const overrideCount = fields.length - following.length;
        summaryText.textContent = following.length
            ? `${following.length} field${following.length === 1 ? "" : "s"} following ${origin}`
            : "Every field on this attachment is overridden.";
        if (overrideCount && following.length) {
            summaryText.textContent += ` · ${overrideCount} overridden`;
        }
        summaryToggle.textContent = expanded ? "Hide following" : "Override…";
        summaryToggle.title = expanded
            ? "Collapse the fields that are following their default."
            : "Show every declared field so one can be overridden.";
        summaryRow.style.display = following.length ? "flex" : "none";
    };
    summaryToggle.addEventListener("click", () => {
        expanded = !expanded;
        disclosureMemory?.remember(disclosureKey, expanded);
        applyDisclosure();
    });
    applyDisclosure();

    return {
        fields,
        controls,
        rows,
        summaryRow,
        has: (field) => controls.has(field),
        row: (field) => rows.get(field) || null,
        isExpanded: () => expanded,
        setExpanded: (value) => { expanded = !!value; applyDisclosure(); },
        /** Append this field's row to `list` when the format declares it. */
        pushRow: (list, field) => {
            const row = rows.get(field);
            if (row) list.push(row);
            return Boolean(row);
        },
        isOverridden: (field) => overriddenFields.has(field),
        /** Current authored-or-inherited value, for live effective projections. */
        draftValue: (field) => (controls.has(field)
            ? readControlValue(field, controls.get(field))
            : working[field]),
        draftOverrides: () => {
            const draft = { ...working };
            for (const field of overriddenFields) {
                if (controls.has(field)) {
                    draft[field] = readControlValue(field, controls.get(field));
                }
            }
            return draft;
        },
        refresh: (nextSelected = currentSelection) => {
            currentSelection = String(nextSelected || "");
            for (const [field, control] of controls) {
                if (!overriddenFields.has(field)) {
                    setControlValue(field, control, effectiveValue(field,
                        control.multiple ? [] : ""));
                }
                refreshOverrideStatus(field);
            }
            applyDisclosure();
        },
        /**
         * Sparse overrides for the save. Derived from the SAME control map the
         * rows came from — reading a fixed field list here is what let a
         * declaration-gated control be dereferenced when absent.
         */
        collect: () => {
            for (const field of REFERENCE_OVERRIDE_FIELDS) {
                if (overriddenFields.has(field) && controls.has(field)) {
                    working[field] = readControlValue(field, controls.get(field));
                } else if (!overriddenFields.has(field)) {
                    delete working[field];
                }
            }
            return working;
        },
    };
}

/**
 * Which Reference sources this scene offers, and whether each may be attached.
 *
 * The caret menu and the Attach dialog both need this list with the same
 * verdicts, format compatibility and setup membership behind it. Computing it
 * once here is what keeps a handle offered in the menu and a handle offered in
 * the dialog from ever disagreeing.
 *
 * Returns `[value, label, eligible]` triples in the dialog's existing shape;
 * `usesDeclaredSources` says whether the format wants identities and physical
 * slots rather than whole Reference items.
 */
export function promptReferenceSourceOptions({ scene = null, references = [],
    semanticUnits = [], profileId = "generic@1", scope = "",
    resolvedProfile = null } = {}) {
    const usesDeclaredSources = Boolean(
        resolvedProfile?.physical_populations?.length
        || resolvedProfile?.identity_kinds?.length);
    const referenceItems = Array.isArray(scene?.reference_items)
        ? scene.reference_items : [];
    const consumerStart = Number(scene?._context_consumer_start);
    const consumerEnd = Number(scene?._context_consumer_end);
    const globalScope = scope === "global"
        || (scope !== "section" && !Number.isFinite(consumerStart));
    const hasSelection = Number.isFinite(consumerStart)
        && Number.isFinite(consumerEnd) && consumerEnd > consumerStart;
    const verdictResult = resolveReferenceVerdicts({
        referenceItems,
        laneCount: scene?.reference_lane_count || 1,
        sceneDuration: scene?.duration_frames || 0,
        windowStart: hasSelection ? consumerStart : 0,
        windowEnd: hasSelection ? consumerEnd : (scene?.duration_frames || 0),
        laneConfigs: scene?.reference_lane_configs || [],
        frameThresholdPct: Number(scene?._context_reference_frame_threshold || 0),
    });
    const referenceNames = new Map((references || []).map((value) => [
        value.reference_id, value.name || value.reference_id,
    ]));
    const laneRecipes = Array.isArray(scene?.reference_lane_recipes)
        ? scene.reference_lane_recipes : [];
    const itemOptions = referenceItems.map((item, index) => {
        const names = [...new Set((item.members || []).map((value) =>
            referenceNames.get(value.entity_id)).filter(Boolean))];
        const verdict = verdictResult.verdicts.get(index) || "outside";
        const wrapper = laneRecipes[Number(item.lane_index || 0)] || {};
        const recipe = wrapper.recipe && typeof wrapper.recipe === "object"
            ? wrapper.recipe : wrapper;
        const compatible = recipe?.soft?.compatible_profiles || ["generic@1"];
        const profileCompatible = compatible.includes(profileId);
        const verdictLabel = REFERENCE_VERDICT_LABEL[verdict] || verdict;
        const eligibility = globalScope ? profileCompatible
            : verdict === "winner" && profileCompatible;
        const stateLabel = globalScope
            ? (hasSelection && verdict === "winner" ? " — applies now" : "")
            : ` — ${verdictLabel}`;
        return [`item:${item.reference_item_id}`,
            `Lane ${Number(item.lane_index || 0) + 1}: ${names.join(" + ") || "Reference"}${stateLabel}${profileCompatible ? "" : " / incompatible prompt format"}`,
            eligibility];
    });
    const makeUnitOption = (value, sources) => {
        const eligibility = subjectSourceEligibility({
            sources, referenceItems, laneRecipes,
            verdicts: verdictResult.verdicts, profileId,
            scope: { globalScope, hasSelection },
        });
        return [value.semantic_unit_id,
            `Subject: ${value.name || value.semantic_unit_id}${eligibility.suffix}`,
            eligibility.eligible];
    };
    const unitOptions = ((semanticUnits || []).length
        ? semanticUnits.map((value) =>
            makeUnitOption(value, value.sources || []))
        : (references || []).map((value) => makeUnitOption({
                semantic_unit_id: `unit:${value.reference_id}`,
                name: value.name || value.reference_id,
            }, referenceItems.flatMap((item) => (item.members || []).filter((member) =>
                String(member?.entity_id || "") === String(value.reference_id || ""))))));
    const physicalOptions = [];
    referenceItems.forEach((item, index) => {
        const verdict = verdictResult.verdicts.get(index) || "outside";
        const wrapper = laneRecipes[Number(item.lane_index || 0)] || {};
        const recipe = wrapper.recipe && typeof wrapper.recipe === "object"
            ? wrapper.recipe : wrapper;
        // The lane's declared model input is the only membership authority.
        // `lanePopulation` answers in the manifest's plural vocabulary; the chip
        // value below is parsed by singular `physical:(picture|video|audio):`
        // regexes, so the translation stays here.
        const population = ({
            pictures: "picture", videos: "video", standalone_audios: "audio",
        })[lanePopulation(wrapper)] || "";
        if (!population) return;
        const compatible = recipe?.soft?.compatible_profiles || ["generic@1"];
        const profileCompatible = compatible.includes(profileId);
        const eligible = profileCompatible && (globalScope || verdict === "winner");
        let stateSuffix = "";
        if (!profileCompatible) stateSuffix = " - incompatible prompt format";
        else if (globalScope) {
            // The existing compact label already adds the applies-now badge.
            stateSuffix = "";
        } else {
            stateSuffix = ` - ${REFERENCE_VERDICT_LABEL[verdict] || verdict}`;
        }
        for (const member of item.members || []) {
            const memberId = String(member?.member_id || "");
            if (!memberId) continue;
            const name = referenceNames.get(member.entity_id) || memberId;
            physicalOptions.push([`physical:${population}:${memberId}`,
                `${population[0].toUpperCase()}${population.slice(1)} source: ${name}${globalScope && hasSelection && verdict === "winner" ? " — applies now" : ""}`, true]);
            physicalOptions[physicalOptions.length - 1][1] += stateSuffix;
            physicalOptions[physicalOptions.length - 1][2] = eligible;
        }
    });
    return {
        usesDeclaredSources,
        unitOptions,
        physicalOptions,
        itemOptions,
        options: usesDeclaredSources
            ? [...unitOptions, ...physicalOptions] : itemOptions,
    };
}

/** Apply one option value from  to a chip. */
export function applyPromptReferenceSource(attachment, value) {
    const raw = String(value || "");
    const itemMatch = raw.match(/^item:(.+)$/);
    const physicalMatch = raw.match(/^physical:(picture|video|audio):(.+)$/);
    delete attachment.source.picture_ids;
    delete attachment.source.video_ids;
    delete attachment.source.audio_ids;
    if (itemMatch) {
        attachment.source.reference_item_id = itemMatch[1];
        delete attachment.source.semantic_unit_ids;
    } else if (physicalMatch) {
        attachment.source[`${physicalMatch[1]}_ids`] = [physicalMatch[2]];
        delete attachment.source.reference_item_id;
        delete attachment.source.semantic_unit_ids;
    } else {
        attachment.source.semantic_unit_ids = [raw];
        delete attachment.source.reference_item_id;
    }
    return attachment;
}

/** Bounded, deterministic attachment configuration. No provider prose is generated. */
export function configurePromptAttachment(rawAttachment, {
    scene = null, references = [], semanticUnits = [], channelKey = "", profileId = "generic@1",
    scope = "", profile = null, placementPhases = [], managedSpeakerSubjectIds = [],
    ordinalManifest = {}, candidate = null, anchoredChannels = [],
} = {}) {
    const attachment = normalizePromptAttachment(rawAttachment);
    const resolvedProfile = profile && typeof profile === "object"
        ? profile : (candidate?.profile || {});
    // Did a format actually RESOLVE, or is this the empty object the chip gets
    // when the catalog has not arrived? The distinction matters: an unresolved
    // profile must say so, while a resolved format that simply declares no
    // task types or intents must keep rendering nothing at all. Keying this on
    // "the catalog is absent" would show a loading row forever on a legitimate
    // format that declares no such fields.
    const profileResolved = Boolean(resolvedProfile
        && (resolvedProfile.profile_id || resolvedProfile.capabilities));
    const referenceDerived = referenceDerivedDeclarations(resolvedProfile);
    return new Promise((resolve) => {
        const backdrop = document.createElement("div");
        backdrop.dataset.sonderPromptContextModal = "1";
        backdrop.style.cssText = "position:fixed;inset:0;z-index:12000;background:rgba(5,8,12,.72);display:flex;align-items:center;justify-content:center;padding:20px;";
        const panel = document.createElement("div");
        panel.style.cssText = `width:min(620px,92vw);max-height:82vh;overflow:auto;padding:14px;border:1px solid ${COLORS.border};border-radius:8px;background:${COLORS.panelRaised};box-shadow:0 18px 60px rgba(0,0,0,.55);display:flex;flex-direction:column;gap:10px;`;
        const heading = document.createElement("div");
        heading.textContent = `${LABELS[attachment.kind] || "Context"} attachment`;
        heading.style.cssText = `font:600 13px system-ui;color:${COLORS.text};`;
        const hint = document.createElement("div");
        hint.textContent = `Stored as semantic intent; ${profileId} resolves provider text at preview/enqueue.`;
        hint.style.cssText = `font:10px system-ui;color:${COLORS.textDim};`;
        panel.append(heading, hint);
        const controls = {};

        if (attachment.kind === "shot") {
            controls.shotTimestamp = document.createElement("input");
            controls.shotTimestamp.type = "checkbox";
            controls.shotTimestamp.checked = Boolean(attachment.config.timestamp);
            panel.append(fieldRow("Include section time", controls.shotTimestamp,
                "When enabled, this Shot marker also includes the section's start or cut time."));
        } else if (attachment.kind === "timestamp") {
            attachment.config.standalone = true;
            const previewByChannel = candidate?.attachment_channel_previews
                ?.[attachment.attachment_id] || {};
            const resolved = Object.values(previewByChannel).find((value) =>
                String(value || "").trim());
            const timeNotice = document.createElement("div");
            timeNotice.style.cssText = `font:11px/1.45 system-ui;color:${COLORS.text};padding:7px 9px;border:1px solid ${COLORS.border};border-radius:5px;background:${COLORS.panel};`;
            timeNotice.textContent = resolved
                ? `Resolved section time: ${resolved}`
                : "Standalone Time resolves from this section's start/cut position at preview and execution time.";
            panel.appendChild(timeNotice);
        } else if (attachment.kind === "custom") {
            // Custom is the model-agnostic escape hatch: fixed text plus
            // whatever bounded fields a format declares for it. It carried an
            // H3-specific Guide binding until the native MiniMaxH3AddGuide node
            // took over physical keyframe delivery from the Guides Bridge, at
            // which point the binding described nothing the graph did not
            // already do; the picture-alignment line is composed by the
            // compiler from the task mode, never by a chip.
            controls.text = textField(attachment.config.text, true);
            panel.append(fieldRow("Fixed text", controls.text));
        } else if (["prompt_link", "prompt_link_scope"].includes(attachment.kind)) {
            const currentStart = Number(scene?._context_consumer_start ?? Infinity);
            const sectionOptions = [["", "Choose an earlier section…"]];
            for (const section of scene?.prompt_sections || []) {
                if (!section.prompt_id) continue;
                const earlier = Number(section.start_frame || 0) < currentStart;
                sectionOptions.push([section.prompt_id,
                    `${section.start_frame}-${section.end_frame}: ${String(section.prompt || "").slice(0, 70)}`
                        + (earlier ? "" : " - must be earlier"), earlier]);
            }
            controls.promptId = selectField(sectionOptions, attachment.source.prompt_id);
            sectionOptions.forEach((value, index) => {
                if (controls.promptId.options[index]) {
                    controls.promptId.options[index].disabled = value[2] === false;
                }
            });
            const channelOptions = (scene?._context_channel_keys || [channelKey || "visual"])
                .map((key) => [key, key]);
            controls.channel = selectField(channelOptions, attachment.source.channel_key || channelKey);
            if (attachment.kind === "prompt_link_scope") {
                // No stored selection means every channel, which is the
                // documented default rather than an empty link.
                const stored = (attachment.source.channel_keys || []).map(String);
                controls.channel = checkboxListField(channelOptions,
                    stored.length ? stored : channelOptions.map(([key]) => key),
                    { allLabel: "All" });
                panel.append(fieldRow("Source section", controls.promptId),
                    fieldRow("Channels", controls.channel,
                        "All channels are linked by default; select a subset for advanced routing."));
            } else {
                panel.append(fieldRow("Source section", controls.promptId),
                    fieldRow("Source channel", controls.channel));
            }
        } else if (attachment.kind === "vocal_event") {
            controls.eventType = selectField(
                ["dialogue", "singing", "narration", "voiceover", "group_speech"],
                attachment.config.event_type || "dialogue");
            controls.language = selectField(DIALOGUE_LANGUAGES,
                attachment.config.language || "English");
            controls.subjectPhrase = textField(attachment.config.subject_phrase || "");
            controls.text = textField(attachment.config.text, true);
            controls.voice = textField(attachment.source.voice_id || "");
            controls.subjects = checkboxListField(
                (semanticUnits || []).map((value) => [
                    value.semantic_unit_id, value.name || value.semantic_unit_id,
                ]),
                (attachment.source.subject_ids || []).map(String),
                { allLabel: "All" });
            // A Vocal Event with no Subject and no voice key has nothing to
            // number: the speaker id would be minted from the chip's own id and
            // would name no one in the scene.
            controls.bindingNotice = document.createElement("div");
            controls.bindingNotice.textContent =
                "Choose at least one Subject, or enter a stable Voice ID.";
            controls.bindingNotice.style.cssText =
                `grid-column:1/-1;font:10px system-ui;color:${COLORS.dangerText};display:none;`;
            panel.append(fieldRow("Event", controls.eventType),
                fieldRow("Language", controls.language),
                fieldRow("Speaker / on-screen subject", controls.subjectPhrase,
                    "Authored identifying phrase placed before the managed (Sx); required for MiniMax voiceover."),
                fieldRow("Subjects", controls.subjects,
                    "One or more stable Subject units. Speaker IDs are late-bound by first vocal event."),
                fieldRow("Voice ID", controls.voice, "A stable project voice key; speaker numbers are assigned by event order."),
                controls.bindingNotice,
                fieldRow("Words", controls.text));
        } else if (attachment.kind === "reference") {
            const referenceSources = promptReferenceSourceOptions({
                scene, references, semanticUnits, profileId, scope,
                resolvedProfile,
            });
            const usesDeclaredSources = referenceSources.usesDeclaredSources;
            // Still read further down, where a chosen source is mapped back to
            // its lane recipe for the capability rows.
            const referenceItems = Array.isArray(scene?.reference_items)
                ? scene.reference_items : [];
            const laneRecipes = Array.isArray(scene?.reference_lane_recipes)
                ? scene.reference_lane_recipes : [];
            const referenceOptions = [["", "Choose a Reference…", true],
                ...referenceSources.options];
            const selectedPhysical = [
                ["picture", attachment.source.picture_ids],
                ["video", attachment.source.video_ids],
                ["audio", attachment.source.audio_ids],
            ].find((value) => Array.isArray(value[1]) && value[1].length);
            const selectedReference = attachment.source.reference_item_id
                    ? `item:${attachment.source.reference_item_id}`
                    : selectedPhysical
                        ? `physical:${selectedPhysical[0]}:${selectedPhysical[1][0]}`
                        : String(attachment.source.semantic_unit_ids?.[0] || "");
            if (selectedReference && !referenceOptions.some((value) => value[0] === selectedReference)) {
                referenceOptions.push([selectedReference, `Unavailable: ${selectedReference} — rebind source`, false]);
            }
            controls.reference = selectField(referenceOptions, selectedReference);
            referenceOptions.forEach((value, index) => {
                if (controls.reference.options[index]) {
                    controls.reference.options[index].disabled = value[2] === false;
                }
            });
            const referenceFieldset = createReferenceOverrideFieldset({
                profile: resolvedProfile, references, semanticUnits,
                setupManifest: candidate?.setup_manifest || {},
                overrides: attachment.config.overrides,
                selected: controls.reference.value,
                onOverrideChange: () => refreshCapabilityEffectiveValues(),
                // Browser-local presentation state, like every other disclosure
                // in the editor. Nothing about it belongs to the project.
                disclosureMemory: createDisclosureMemory(
                    "sonder.prompt.chipFieldset.v1"),
                disclosureKey: "reference_overrides",
            });
            controls.referenceFieldset = referenceFieldset;
            const definitionControl = referenceFieldset.controls.get("definition") || null;
            const inheritanceNotice = document.createElement("div");
            inheritanceNotice.style.cssText =
                `grid-column:2;font:10px/1.35 system-ui;color:${COLORS.textDim};margin-top:-4px;white-space:pre-wrap;`;
            const updateInheritance = () => {
                const inherited = resolveReferenceSelectionInheritance(controls.reference.value, {
                    scene, references, semanticUnits,
                });
                const isPhysical = /^physical:/.test(controls.reference.value);
                if (definitionControl) {
                    definitionControl.placeholder = inherited.value
                        || "Describe this Subject for prompt use…";
                }
                inheritanceNotice.textContent = inherited.value
                    ? (isPhysical
                        ? `Available from ${inherited.source}. Enter or edit Definition to author it for this chip.`
                        : `Inherited from ${inherited.source}. Leave Definition blank to keep following it.`)
                    : "No inherited definition is available. Add a Definition here or to the Subject/Library member.";
                inheritanceNotice.style.color = inherited.value
                    ? COLORS.textSecondary : COLORS.dangerText;
            };
            const managedSpeakers = new Set((managedSpeakerSubjectIds || []).map(String));
            const currentSpeaker = String(attachment.config.audio_speaker_subject_id || "");
            const speakerOptions = [["", "No target speaker binding", true],
                ...(semanticUnits || []).map((value) => {
                    const unitId = String(value.semantic_unit_id || "");
                    const available = managedSpeakers.has(unitId);
                    return [unitId, `${value.name || unitId}${available
                        ? "" : " — no managed Vocal Event in this window"}`, available];
                })];
            if (currentSpeaker && !speakerOptions.some((row) => row[0] === currentSpeaker)) {
                speakerOptions.push([currentSpeaker,
                    `Unavailable: ${currentSpeaker} — no managed Vocal Event in this window`, false]);
            }
            controls.audioSpeaker = selectField([
                ...speakerOptions,
            ], currentSpeaker);
            speakerOptions.forEach((value, index) => {
                if (controls.audioSpeaker.options[index]) {
                    controls.audioSpeaker.options[index].disabled = value[2] === false;
                }
            });
            const summaryControl = referenceFieldset.controls.get("summary") || null;
            const tokenStrip = document.createElement("div");
            tokenStrip.style.cssText = "grid-column:2;display:flex;gap:4px;align-items:center;flex-wrap:wrap;margin-top:-3px;";
            const insertSummaryToken = (value) => {
                if (!value || !summaryControl) return;
                const start = Number.isInteger(summaryControl.selectionStart)
                    ? summaryControl.selectionStart : summaryControl.value.length;
                summaryControl.setRangeText(value, start, start, "end");
                summaryControl.dispatchEvent(new Event("input", { bubbles: true }));
                summaryControl.focus();
            };
            const renderTokenStrip = () => {
                tokenStrip.textContent = "";
                // The strip inserts into Summary. Without that control there is
                // nothing to insert into, so the format declares no Summary and
                // the strip has no subject.
                if (!summaryControl) return;
                const selected = String(controls.reference.value || "");
                const physical = selected.match(/^physical:(picture|video|audio):(.+)$/);
                let kind = "subject";
                let sourceId = selected;
                const selectedUnit = (semanticUnits || []).find((value) =>
                    String(value?.semantic_unit_id || "") === sourceId);
                let storedHandle = String(selectedUnit?.handle || "");
                if (physical) {
                    kind = physical[1];
                    sourceId = physical[2];
                    storedHandle = String((references || []).flatMap((reference) =>
                        reference?.members || []).find((member) =>
                        String(member?.member_id || "") === sourceId)?.handle || "");
                }
                const declarations = promptTokenDeclarationsFromProfile(
                    candidate?.profile || {});
                const declaration = declarations[kind];
                const authoredToken = promptToken(kind, sourceId, declarations);
                if (!declaration || !authoredToken || selected.startsWith("item:")) {
                    const empty = document.createElement("span");
                    empty.textContent = "Choose a Subject or physical setup source to insert a late-bound token.";
                    empty.style.cssText = `font:9px/1.3 system-ui;color:${COLORS.textDim};`;
                    tokenStrip.appendChild(empty);
                    return;
                }
                const ordinal = Number(ordinalManifest?.[declaration.manifestKey]?.[sourceId] || 0);
                const button = chipButton("", "", { pill: true });
                const resolvedLabel = ordinal > 0
                    ? String(declaration.labelTemplate || "").replace("{n}", String(ordinal))
                    : "not in the current preview";
                button.textContent = storedHandle
                    ? `@${storedHandle} · ${resolvedLabel}`
                    : "Set this handle in Reference Prompting before inserting it";
                button.title = storedHandle
                    ? `Insert ${authoredToken}. The stable id is stored; provider ordinals resolve only during compilation.`
                    : "A suggested handle is presentation-only until the versioned Reference write succeeds.";
                setButtonDisabled(button, !storedHandle);
                button.addEventListener("mousedown", (event) => event.preventDefault());
                button.addEventListener("click", () => insertSummaryToken(authoredToken));
                tokenStrip.appendChild(button);
            };
            const referenceFieldRow = (label, control, help) =>
                fieldRow(label, control, help, { visibleHelp: true });
            const refreshCapabilityEffectiveValues = () => {
                for (const row of controls.capabilityRows?.values?.() || []) {
                    row.refreshEffective?.();
                }
            };
            const refreshInheritedFields = () =>
                referenceFieldset.refresh(controls.reference.value);
            controls.reference.title = "Choose a semantic Subject or a physical source from the active conditioning setup.";
            // Row ORDER is authored here, not by the fieldset, because the
            // non-overridable controls below are interleaved between declared
            // fields. Every `pushRow` is a no-op when the format does not
            // declare that field's capability.
            const referenceRows = [referenceFieldRow("Reference", controls.reference,
                "Choose a semantic Subject or a physical source from the active conditioning setup.")];
            // Name the rung. Without it this fieldset and the identity editor's
            // defaults group looked like two copies of one panel, with nothing
            // saying which was which or which way inheritance ran.
            const overridesHeading = document.createElement("div");
            overridesHeading.textContent = "Overrides for this attachment";
            overridesHeading.title = "Values authored here apply to this attachment only. Everything else follows the Reference or Identity default.";
            overridesHeading.style.cssText = `grid-column:1/-1;font:600 10px system-ui;color:${COLORS.text};margin-top:2px;`;
            referenceRows.push(overridesHeading, referenceFieldset.summaryRow);
            if (referenceFieldset.pushRow(referenceRows, "definition")) {
                referenceRows.push(inheritanceNotice);
            }
            referenceFieldset.pushRow(referenceRows, "audio_definition");
            referenceRows.push(referenceFieldRow("Audio target speaker", controls.audioSpeaker,
                "Reuses the (Sx) assigned by that Subject's first managed Vocal Event; it never creates speaker order."));
            if (referenceFieldset.pushRow(referenceRows, "summary")) {
                referenceRows.push(tokenStrip);
                const otherSummaryOwners = [
                    ...(scene?.global_attachments || []),
                    ...(scene?.prompt_sections || []).flatMap((value) => value?.attachments || []),
                ].filter((value) => value?.kind === "reference"
                    && String(value?.attachment_id || "") !== String(attachment.attachment_id || "")
                    && String(value?.config?.overrides?.summary || "").trim());
                if (String(referenceFieldset.draftValue("summary") || "").trim()
                        && otherSummaryOwners.length) {
                    const summaryOwnerNotice = document.createElement("div");
                    // Warm is legitimate HERE and nowhere else on this surface:
                    // a conflicting Summary owner is a transient compile state,
                    // which is exactly what status colour is for.
                    summaryOwnerNotice.style.cssText = `grid-column:2;font:9px/1.35 system-ui;color:${COLORS.warningText};margin-top:-4px;`;
                    summaryOwnerNotice.textContent = "Only one Summary owner can emit per compile. Another Reference chip also carries Summary text.";
                    referenceRows.push(summaryOwnerNotice);
                }
            }
            for (const field of ["task_types", "text", "retention_detail",
                "audio_relationship", "visual_intent", "audio_intent"]) {
                referenceFieldset.pushRow(referenceRows, field);
            }
            if (!profileResolved) {
                // Opened before the format catalog landed, every declared field
                // rendered NO ROW AT ALL and nothing said why — Summary task
                // types and both retention intents simply vanished. The stored
                // overrides are safe (the fieldset only collects fields it
                // actually built, so an unrendered key never enters the save),
                // but silence reads as "this format has no such fields".
                //
                // Expiry: removable once the prompt format catalog is served
                // synchronously with the panel mount, so a chip can never open
                // ahead of it.
                const notice = document.createElement("div");
                notice.dataset.sonderPromptDeclaredFieldsState = "unresolved";
                notice.style.cssText = `grid-column:2;font:9px/1.35 system-ui;color:${COLORS.warningText};`;
                notice.textContent = "Format-declared fields are still loading, so any task types and handling choices this format declares are not shown yet. Close and reopen this chip once the prompt format catalog arrives; your saved values are untouched.";
                referenceRows.push(notice);
            }
            panel.append(...referenceRows);

            const capabilityDetails = document.createElement("details");
            const capabilitySummary = document.createElement("summary");
            capabilitySummary.textContent = "Prompt parts and routing";
            capabilitySummary.title = "Choose which prompt parts this Reference adds, and where each part is routed.";
            capabilitySummary.style.cssText = "min-width:0;overflow-wrap:anywhere;";
            capabilityDetails.appendChild(capabilitySummary);
            const capabilityHost = document.createElement("div");
            capabilityHost.style.cssText = "display:flex;flex-direction:column;gap:6px;padding:8px 0 0;";
            panel.style.containerType = "inline-size";
            const routingStyle = document.createElement("style");
            routingStyle.textContent = `
                .sonder-prompt-routing-row {
                    display:grid;
                    grid-template-columns:minmax(140px,.8fr) minmax(0,2.2fr);
                    gap:5px 8px;
                    align-items:start;
                    min-width:0;
                }
                .sonder-prompt-routing-label {
                    display:grid;
                    grid-template-columns:auto minmax(0,1fr);
                    gap:4px;
                    align-items:start;
                    min-width:0;
                }
                .sonder-prompt-routing-controls {
                    display:grid;
                    grid-template-columns:repeat(2,minmax(0,1fr));
                    gap:6px;
                    align-items:start;
                    min-width:0;
                }
                .sonder-prompt-routing-state {
                    grid-column:1 / -1;
                    white-space:normal;
                    overflow-wrap:anywhere;
                }
                .sonder-prompt-routing-help { grid-column:2; }
                .sonder-prompt-effective-values {
                    grid-column:2;
                    display:flex;
                    flex-direction:column;
                    gap:3px;
                    min-width:0;
                }
                @container (max-width:600px) {
                    .sonder-prompt-routing-row { grid-template-columns:minmax(0,1fr); }
                    .sonder-prompt-routing-controls {
                        grid-column:1;
                        grid-template-columns:minmax(0,1fr);
                    }
                    .sonder-prompt-routing-state { grid-column:1; }
                    .sonder-prompt-routing-help { grid-column:1; }
                    .sonder-prompt-effective-values { grid-column:1; }
                }
            `;
            capabilityDetails.appendChild(routingStyle);
            capabilityDetails.appendChild(capabilityHost);
            panel.appendChild(capabilityDetails);
            controls.capabilityRows = new Map();
            const declaredCapabilities = orderedReferenceDerived(resolvedProfile);
            const capabilitiesForSelection = (selected) => {
                if (!selected) return declaredCapabilities.map(([kind]) => kind);
                const itemId = String(selected || "").match(/^item:(.+)$/)?.[1] || "";
                if (itemId) {
                    const item = referenceItems.find((value) =>
                        String(value?.reference_item_id || "") === itemId);
                    const wrapper = laneRecipes[Number(item?.lane_index || 0)] || {};
                    const recipe = wrapper.recipe && typeof wrapper.recipe === "object"
                        ? wrapper.recipe : wrapper;
                    const exposed = recipe?.soft?.exposed_capabilities;
                    return (Array.isArray(exposed) && exposed.length
                        ? exposed.map(String).filter((kind) =>
                            Object.hasOwn(referenceDerived, kind))
                        : declaredCapabilities.map(([kind]) => kind));
                }
                return declaredCapabilities.map(([kind]) => kind);
            };
            const renderCapabilities = () => {
                capabilityHost.textContent = "";
                controls.capabilityRows.clear();
                const existing = new Map((attachment.capabilities || []).map((value) => [
                    String(value?.capability_id || value?.kind || ""), structuredClone(value),
                ]));
                const suggested = capabilitiesForSelection(controls.reference.value);
                const capabilityIds = [...new Set([...suggested, ...existing.keys()].filter(Boolean))];
                for (const capabilityId of capabilityIds) {
                    const capabilityDeclaration = referenceDerived[capabilityId] || {};
                    // No `placement` here: a capability with no stored record
                    // inherits the declaration, and seeding it would write the
                    // format default back as if the author had chosen it.
                    // No `enabled` here either: an unstored capability inherits
                    // the Reference/identity default, and seeding it would save
                    // the inherited value back as a deliberate override.
                    const current = existing.get(capabilityId) || {
                        capability_id: capabilityId, kind: capabilityId,
                    };
                    const inheritedEnabled = resolveInheritedCapabilityEnabled(
                        capabilityId, {
                            selected: controls.reference.value,
                            references, semanticUnits,
                        });
                    const row = document.createElement("div");
                    row.className = "sonder-prompt-routing-row";
                    const enabled = document.createElement("label");
                    enabled.className = "sonder-prompt-routing-label";
                    const checkbox = document.createElement("input");
                    checkbox.type = "checkbox";
                    checkbox.checked = Object.hasOwn(current, "enabled")
                        ? current.enabled !== false : inheritedEnabled;
                    if (!Object.hasOwn(current, "enabled")) {
                        checkbox.title = inheritedEnabled
                            ? "Following the Reference default (on)."
                            : "Following the Reference default (off).";
                    }
                    const capabilityName = document.createElement("span");
                    capabilityName.textContent = String(
                        capabilityDeclaration?.label || capabilityId);
                    capabilityName.title = capabilityId;
                    capabilityName.style.cssText = "min-width:0;overflow-wrap:anywhere;";
                    enabled.append(checkbox, capabilityName);
                    if (!suggested.includes(capabilityId)) {
                        enabled.title = Object.hasOwn(referenceDerived, capabilityId)
                            ? "This saved prompt part is not exposed by the selected recipe; disable it or rebind its source."
                            : "This saved prompt part is not declared by the active Prompt Format; disable it or switch formats.";
                        enabled.style.color = COLORS.dangerText;
                    }
                    const routeKey = String(current.kind || capabilityId);
                    const resolvedDefaultChannel = String(
                        referenceDerived?.[routeKey]?.channel_key
                        || resolvedProfile?.capabilities?.reference?.channel_key
                        || channelKey
                        || scene?._context_channel_keys?.[0] || "");
                    const channel = selectField([
                        ["", `Provider default → ${resolvedDefaultChannel || "unresolved"}`],
                        ...(scene?._context_channel_keys || [channelKey]).filter(Boolean)
                            .map((key) => [key, key]),
                    ], current.channel_key || "");
                    // NOT `|| capabilityDeclaration?.placement`: falling through
                    // to the declaration would select a concrete phase, and the
                    // save would then write the format default back as an
                    // override. Blank means inherit, and stays selectable.
                    const selectedPlacement = current.placement || "";
                    const hasAnchor = Array.isArray(anchoredChannels)
                        && anchoredChannels.length > 0;
                    const resolvedDefaultPlacement = placementDisplayLabel(
                        String(capabilityDeclaration?.placement || ""),
                        { hasAnchor, phaseCatalog: placementPhases });
                    const placementOptions = [
                        ["", `Provider default → ${resolvedDefaultPlacement || "unresolved"}`],
                        ...(placementPhases || []).map((row) => [
                            String(row?.value || ""),
                            String(row?.value || "") === "inline"
                                ? placementDisplayLabel("inline", {
                                    hasAnchor, phaseCatalog: placementPhases,
                                })
                                : String(row?.label || row?.value || ""),
                        ]).filter(([value]) => value),
                    ];
                    if (selectedPlacement && !placementOptions.some(
                        ([value]) => value === selectedPlacement)) {
                        placementOptions.push([selectedPlacement,
                            `Unsupported saved placement: ${selectedPlacement}`]);
                    }
                    const placement = selectField(placementOptions, selectedPlacement);
                    const placementHelpFor = (value) => value === "inline"
                        ? (hasAnchor
                            ? "Placed at its caret when this capability resolves to the anchored channel; a rerouted capability appears after section prefixes in its destination channel."
                            : "Placed after section-prefix contributions and before authored text.")
                        : String((placementPhases || []).find((row) =>
                            String(row?.value || "") === value)?.description || "");
                    for (const option of placement.options) {
                        option.title = placementHelpFor(option.value);
                    }
                    const state = document.createElement("span");
                    const projection = (candidate?.attachment_capability_projections || [])
                        .filter((value) => value?.attachment_id === attachment.attachment_id
                            && value?.capability_id === capabilityId)
                        .sort((a, b) => Number(a?.order || 0) - Number(b?.order || 0))[0];
                    const projectionState = projection?.state === "linked_elsewhere"
                        ? "linked" : String(projection?.state || "pending");
                    const phase = placementDisplayLabel(
                        projection?.effective_phase || selectedPlacement, {
                            renderedAtAnchor: projection?.rendered_at_anchor === true,
                            hasAnchor: !projection && hasAnchor,
                            phaseCatalog: placementPhases,
                        });
                    state.textContent = `→ ${projection?.channel_key || resolvedDefaultChannel || "unresolved"} · ${phase} · ${projectionState}`;
                    state.title = projection?.state_reason || "Compile once to see this capability's live state.";
                    state.className = "sonder-prompt-routing-state";
                    state.style.cssText = `min-width:0;padding:2px 6px;border:1px solid ${COLORS.border};border-radius:999px;color:${COLORS.text};background:${COLORS.panel};font:9px/1.3 system-ui;`;
                    const routingControls = document.createElement("div");
                    routingControls.className = "sonder-prompt-routing-controls";
                    channel.style.cssText += "min-width:0;";
                    placement.style.cssText += "min-width:0;";
                    routingControls.append(channel, placement, state);
                    const help = document.createElement("div");
                    help.className = "sonder-prompt-routing-help";
                    help.style.cssText = `min-width:0;font:9px/1.35 system-ui;color:${COLORS.textDim};`;
                    const updatePlacementHelp = () => {
                        help.textContent = placementHelpFor(placement.value);
                        placement.title = help.textContent;
                    };
                    placement.addEventListener("change", updatePlacementHelp);
                    updatePlacementHelp();
                    const effective = document.createElement("div");
                    effective.className = "sonder-prompt-effective-values";
                    const refreshEffective = () => {
                        effective.textContent = "";
                        const compiledLine = document.createElement("div");
                        compiledLine.style.cssText = "display:grid;grid-template-columns:minmax(100px,.55fr) minmax(0,1fr) auto;gap:5px;align-items:baseline;font:9px/1.3 system-ui;";
                        const compiledLabel = document.createElement("span");
                        compiledLabel.textContent = "Last compiled output";
                        compiledLabel.style.color = COLORS.textDim;
                        const compiledValue = document.createElement("span");
                        compiledValue.textContent = projection
                            ? (String(projection.text || "") || `(${projection.state || "empty"})`)
                            : "Compile to resolve";
                        compiledValue.title = String(projection?.text || "");
                        compiledValue.style.cssText = `min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:${COLORS.text};`;
                        const compiledSource = document.createElement("span");
                        compiledSource.textContent = "Compiler projection";
                        compiledSource.dataset.sonderAuthorityTier = "compiler";
                        compiledSource.style.cssText = `white-space:nowrap;color:${COLORS.textDim};font-weight:600;`;
                        compiledLine.append(compiledLabel, compiledValue, compiledSource);
                        effective.appendChild(compiledLine);
                        const draftOverrides = referenceFieldset.draftOverrides();
                        const values = referenceCapabilityInputProjection(
                            capabilityId, {
                                selected: controls.reference.value,
                                profile: resolvedProfile, references, semanticUnits,
                                overrides: draftOverrides,
                                capabilityConfig: current.config || {},
                                setupManifest: candidate?.setup_manifest || {},
                            });
                        if (!values.length) {
                            const note = document.createElement("span");
                            note.textContent = "No authored input fields for this prompt part.";
                            note.style.cssText = `font:9px/1.3 system-ui;color:${COLORS.textDim};`;
                            effective.appendChild(note);
                            return;
                        }
                        for (const value of values) {
                            const line = document.createElement("div");
                            line.style.cssText = "display:grid;grid-template-columns:minmax(100px,.55fr) minmax(0,1fr) auto;gap:5px;align-items:baseline;font:9px/1.3 system-ui;";
                            const label = document.createElement("span");
                            label.textContent = `Input · ${value.label}`;
                            label.style.color = COLORS.textDim;
                            const rendered = document.createElement("span");
                            const renderedValue = Array.isArray(value.value)
                                ? value.value.join(" + ") : String(value.value || "");
                            rendered.textContent = value.authored_empty
                                ? "(authored empty)"
                                : `${renderedValue || "(empty)"}${value.stored_empty
                                    ? " · stored empty" : ""}`;
                            rendered.title = renderedValue;
                            rendered.style.cssText = `min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:${COLORS.text};`;
                            const source = document.createElement("span");
                            source.textContent = value.source;
                            source.dataset.sonderAuthorityTier = value.tier;
                            // Same two-step ramp as the field status above.
                            source.style.cssText = `white-space:nowrap;color:${
                                value.tier === "chip" ? COLORS.text : COLORS.textSecondary
                            };font-weight:${value.tier === "chip" ? "600" : "400"};`;
                            line.append(label, rendered, source);
                            effective.appendChild(line);
                        }
                    };
                    row.append(enabled, routingControls, help, effective);
                    capabilityHost.appendChild(row);
                    controls.capabilityRows.set(capabilityId, {
                        current, checkbox, channel, placement, refreshEffective,
                        inheritedEnabled,
                    });
                    refreshEffective();
                }
            };
            controls.reference.addEventListener("change", () => {
                renderCapabilities();
                updateInheritance();
                refreshInheritedFields();
                renderTokenStrip();
            });
            renderCapabilities();
            updateInheritance();
            refreshInheritedFields();
            renderTokenStrip();
        }

        const actions = document.createElement("div");
        actions.style.cssText = "display:flex;justify-content:flex-end;gap:6px;margin-top:4px;";
        const cancel = chipButton("Cancel", "",
            { padding: "5px 10px", fontSize: "11px" });
        // Accent carries the committing action, which is what accent is for.
        // The violet this used to paint on was chip role identity borrowed as
        // emphasis, and it made Attach read as another chip rather than a verb.
        const save = chipButton("Attach", "",
            { variant: "accentSoft", padding: "5px 10px", fontSize: "11px" });
        actions.append(cancel, save);
        panel.appendChild(actions);
        backdrop.appendChild(panel);
        document.body.appendChild(backdrop);

        let releaseEscape = () => {};
        const finish = (value) => {
            releaseEscape();
            backdrop.remove();
            resolve(value);
        };
        // This dialog owned no Escape at all, so the key fell through to the
        // Prompt tool behind it and closed THAT — the dialog stayed open over an
        // editor that had just shut. Cancelling with Escape now matches the
        // Cancel button and the backdrop click.
        releaseEscape = ownModalEscape(() => finish(null));
        cancel.addEventListener("click", () => finish(null));
        backdrop.addEventListener("mousedown", (event) => {
            if (event.target === backdrop) finish(null);
        });
        save.addEventListener("click", () => {
            if (attachment.kind === "shot") {
                attachment.config.timestamp = controls.shotTimestamp.checked;
            } else if (attachment.kind === "timestamp") {
                attachment.config.standalone = true;
            } else if (attachment.kind === "custom") {
                attachment.config.text = controls.text.value;
            } else if (["prompt_link", "prompt_link_scope"].includes(attachment.kind)) {
                if (!controls.promptId.value) return;
                attachment.source.prompt_id = controls.promptId.value;
                if (attachment.kind === "prompt_link_scope") {
                    attachment.source.channel_keys = [...controls.channel.selectedOptions]
                        .map((option) => option.value).filter(Boolean);
                    delete attachment.source.channel_key;
                } else {
                    attachment.source.channel_key = controls.channel.value;
                    delete attachment.source.channel_keys;
                }
            } else if (attachment.kind === "vocal_event") {
                const subjectIds = [...controls.subjects.selectedOptions]
                    .map((option) => option.value).filter(Boolean);
                const voiceId = controls.voice.value.trim();
                if (!subjectIds.length && !voiceId) {
                    controls.bindingNotice.style.display = "block";
                    controls.subjects.focus();
                    return;
                }
                controls.bindingNotice.style.display = "none";
                attachment.config.event_type = controls.eventType.value;
                attachment.config.language = controls.language.value.trim() || "English";
                attachment.config.subject_phrase = controls.subjectPhrase.value.trim();
                attachment.config.text = controls.text.value;
                attachment.source.voice_id = voiceId;
                attachment.source.subject_ids = subjectIds;
            } else if (attachment.kind === "reference") {
                if (!controls.reference.value) return;
                if (controls.reference.selectedOptions[0]?.disabled) return;
                const itemMatch = controls.reference.value.match(/^item:(.+)$/);
                const physicalMatch = controls.reference.value.match(
                    /^physical:(picture|video|audio):(.+)$/);
                delete attachment.source.picture_ids;
                delete attachment.source.video_ids;
                delete attachment.source.audio_ids;
                if (itemMatch) {
                    attachment.source.reference_item_id = itemMatch[1];
                    delete attachment.source.semantic_unit_ids;
                } else if (physicalMatch) {
                    attachment.source[`${physicalMatch[1]}_ids`] = [physicalMatch[2]];
                    delete attachment.source.reference_item_id;
                    delete attachment.source.semantic_unit_ids;
                } else {
                    attachment.source.semantic_unit_ids = [controls.reference.value];
                    delete attachment.source.reference_item_id;
                }
                attachment.config.audio_speaker_subject_id = controls.audioSpeaker.value;
                // Collected from the SAME control map the rows were built from.
                // A fixed field list here dereferenced controls that a
                // declaration gate had legitimately never created, throwing
                // inside this handler and losing the chip with no message.
                attachment.config.overrides = controls.referenceFieldset.collect();
                attachment.capabilities = [...(controls.capabilityRows?.entries() || [])]
                    .map(([capabilityId, row]) => sparseCapabilityRecord(row.current, {
                        capabilityId,
                        enabled: row.checkbox.checked,
                        inheritedEnabled: row.inheritedEnabled,
                        channelKey: row.channel.value,
                        placement: row.placement.value,
                    }));
            }
            finish(attachment);
        });
    });
}

export function splitCapabilityProjectionsByRegion(rows = []) {
    const ordered = [...(rows || [])].sort((a, b) =>
        Number(a?.order || 0) - Number(b?.order || 0));
    return {
        before: ordered.filter((value) => value?.region !== "after"),
        after: ordered.filter((value) => value?.region === "after"),
    };
}

export function createPromptProjectionBox(editor, beforeHost, afterHost) {
    const wrapper = document.createElement("div");
    wrapper.dataset.sonderPromptProjectionBox = "1";
    wrapper.style.cssText = `display:flex;flex-direction:column;min-width:0;border:1px solid ${COLORS.border};border-radius:5px;background:${COLORS.panel};overflow:hidden;`;
    for (const host of [beforeHost, afterHost]) host.style.flex = "0 0 auto";
    editor.style.border = "0";
    editor.style.borderRadius = "0";
    editor.style.background = "transparent";
    wrapper.append(beforeHost, editor, afterHost);
    return wrapper;
}

/**
 * What each staged chip contributes to one channel — the answer, not a rendering.
 *
 * Two surfaces need this and had two implementations. The pill strip beside a
 * channel box got it right; Writing mode's inline prose got it wrong three
 * ways, because it re-derived the same facts from a narrower source:
 *
 * - It read text from `attachment_channel_previews`, and a Shot marker has NO
 *   entry there — its text lives only on the projection row — so a marker
 *   printed its `state_reason` ("composed by the prompt section composer")
 *   instead of `[Shot 1] At 00:00.000,`.
 * - It labelled anything without a Reference identity "Reference", which is
 *   every Shot AND every section-scoped chip.
 * - It decided "inline" from published routes, but a DISABLED inline capability
 *   publishes no route at all, so it read as "show it".
 *
 * `rendered_at_anchor` is the authority on inline, per the projection rule in
 * the durable rules — not the presence of an anchor, and not the route table.
 *
 * `attachments` must carry EVERY pool the caller knows about. Writing has two
 * (the editor's registry and each block's own `attachments`), and passing only
 * the registry is what made section-scoped chips anonymous.
 */
export function channelContributionRows({ channelKey = "", attachments = [],
    candidate = null, attachmentLabelFor = null } = {}) {
    const byId = new Map(normalizePromptAttachments(attachments)
        .map((value) => [value.attachment_id, value]));
    const rows = (candidate?.attachment_capability_projections || [])
        .filter((value) => value?.channel_key === channelKey
            && byId.has(value?.attachment_id));
    if (!rows.length) return [];
    const regions = splitCapabilityProjectionsByRegion(rows);
    const out = [];
    for (const row of [...regions.before, ...regions.after]) {
        const attachment = byId.get(row.attachment_id);
        if (!attachment) continue;
        const state = String(row.state || "empty");
        // A marker is emitting: its text is the marker itself.
        const emitting = state === "emitted" || state === "marker";
        const text = String(row.text || "").trim();
        const reason = String(row.state_reason || "No output");
        out.push({
            row,
            attachment,
            attachmentId: attachment.attachment_id,
            capabilityId: String(row.capability_id || ""),
            label: attachmentLabelFor?.(attachment) || LABELS[attachment.kind]
                || attachment.kind || "Context",
            state,
            emitting,
            text,
            reason,
            resolved: emitting ? (text || reason) : reason,
            atAnchor: row.rendered_at_anchor === true,
        });
    }
    return out;
}

export function createAttachmentChannelProjections({ channelKey = "", attachments = [],
    candidate = null, attachmentLabelFor = null, onActivate = null,
    onSetCapabilityEnabled = null, onLinkedSuppressionWarning = null } = {}) {
    const makeHost = (region) => {
        const host = document.createElement("div");
        host.dataset.sonderPromptChannelProjections = String(channelKey || "");
        host.dataset.sonderPromptProjectionRegion = region;
        host.style.cssText = "display:flex;gap:4px;align-items:center;flex-wrap:wrap;min-width:0;flex:0 0 auto;padding:2px 4px;";
        return host;
    };
    const beforeHost = makeHost("before");
    const afterHost = makeHost("after");
    const split = candidate?.attachment_channel_previews || {};
    const routes = candidate?.attachment_channel_routes || {};
    const normalized = normalizePromptAttachments(attachments);
    const attachmentById = new Map(normalized.map((value) =>
        [value.attachment_id, value]));
    const contributions = channelContributionRows({
        channelKey, attachments, candidate, attachmentLabelFor });
    if (contributions.length) {
        for (const contribution of contributions) {
            if (contribution.atAnchor) continue;
            const projectionRow = contribution.row;
            const attachment = contribution.attachment;
            const text = contribution.text;
            const state = contribution.state;
            const stateLabel = state === "linked_elsewhere" ? "linked" : state;
            const phaseLabel = placementDisplayLabel(
                projectionRow.effective_phase || "section_prefix", {
                    renderedAtAnchor: projectionRow.rendered_at_anchor === true,
                });
            const holder = document.createElement("span");
            holder.style.cssText = "display:inline-flex;align-items:center;gap:2px;min-width:0;max-width:100%;";
            const projection = document.createElement("button");
            projection.type = "button";
            projection.tabIndex = -1;
            projection.dataset.attachmentId = attachment.attachment_id;
            projection.dataset.capabilityId = String(projectionRow.capability_id || "");
            projection.dataset.sonderPromptProjection = "1";
            const sourceLabel = contribution.label;
            const reason = contribution.reason;
            const isEmitting = contribution.emitting;
            const resolved = contribution.resolved;
            projection.textContent = isEmitting
                ? `${resolved} · ${channelKey} · ${stateLabel}`
                : `${resolved} · ${channelKey}`;
            projection.setAttribute("aria-label",
                `${sourceLabel} to ${channelKey}, ${phaseLabel}, ${stateLabel}. ${resolved}`);
            // Accent text: a projection pill is a button that edits the record
            // it names, so it is interaction, not metadata.
            projection.style.cssText = `min-width:0;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:2px 7px;border:1px solid ${COLORS.border};border-radius:999px;background:${COLORS.panelRaised};color:${COLORS.accentHi};font:9px/1.3 system-ui;cursor:pointer;text-align:left;`;
            projection.title = [
                candidate?._stale
                    ? "Preview is updating; showing the latest scene-matched compile." : "",
                `Source: ${sourceLabel}`, `Destination: ${channelKey}`,
                `Declared placement: ${placementDisplayLabel(
                    projectionRow.declared_placement, {
                        renderedAtAnchor: projectionRow.rendered_at_anchor === true,
                    })}`,
                `Effective phase: ${phaseLabel}`,
                `State: ${state}`, projectionRow.state_reason || "",
                text ? `Resolved contribution:\n${text}` : "",
            ].filter(Boolean).join("\n\n");
            if (state !== "emitted" && state !== "marker") {
                projection.dataset.sonderPromptProjectionMuted = "1";
                projection.style.opacity = "0.58";
            }
            projection.addEventListener("mousedown", (event) => event.preventDefault());
            projection.addEventListener("click", () => {
                if (state === "disabled" && onSetCapabilityEnabled) {
                    onSetCapabilityEnabled(attachment, projectionRow, true);
                } else {
                    onActivate?.(attachment, projectionRow);
                }
            });
            holder.appendChild(projection);
            if (onSetCapabilityEnabled && state !== "disabled" && state !== "marker") {
                const suppress = document.createElement("button");
                suppress.type = "button";
                suppress.tabIndex = -1;
                suppress.textContent = "x";
                suppress.setAttribute("aria-label",
                    `Remove this contribution from ${channelKey}`);
                const linkedIds = new Set((
                    candidate?.attachment_capability_projections || [])
                    .filter((value) =>
                        value?.emission_group_id === projectionRow.emission_group_id
                        && value?.capability_id === projectionRow.capability_id
                        && value?.attachment_id !== projectionRow.attachment_id
                        && value?.state !== "disabled")
                    .map((value) => value.attachment_id));
                const linkedWarning = linkedIds.size
                    ? ` Also enabled on ${linkedIds.size} linked chips - suppress there too, or Unlink first.`
                    : "";
                suppress.title = `Remove this contribution from ${channelKey}.${linkedWarning}`;
                suppress.style.cssText = `min-width:16px;min-height:16px;border:0;border-radius:3px;background:transparent;color:${CHIP_PALETTE.control};font:12px/16px system-ui;cursor:pointer;padding:0 2px;`;
                suppress.addEventListener("mousedown", (event) => event.preventDefault());
                suppress.addEventListener("click", () => {
                    onSetCapabilityEnabled(attachment, projectionRow, false);
                    if (linkedIds.size) {
                        onLinkedSuppressionWarning?.({
                            channelKey, linkedCount: linkedIds.size,
                        });
                    }
                });
                holder.appendChild(suppress);
            }
            (projectionRow.region === "after" ? afterHost : beforeHost)
                .appendChild(holder);
        }
        if (!beforeHost.childElementCount) beforeHost.style.display = "none";
        if (!afterHost.childElementCount) afterHost.style.display = "none";
        if (candidate?._stale) {
            beforeHost.style.opacity = "0.62";
            afterHost.style.opacity = "0.62";
        }
        return { beforeHost, afterHost };
    }
    for (const attachment of normalized) {
        const text = String(split?.[attachment.attachment_id]?.[channelKey] || "").trim();
        const routeValue = routes?.[attachment.attachment_id]?.[channelKey];
        const placements = (Array.isArray(routeValue) ? routeValue : [routeValue])
            .map((value) => String(value || "")).filter(Boolean);
        const visiblePlacements = placements.filter((value) => value !== "inline");
        if (!visiblePlacements.length) continue;
        const identity = attachmentLabelFor?.(attachment) || "";
        const emissions = (candidate?.emissions || []).filter((value) =>
            value?.attachment_id === attachment.attachment_id
                && value?.channel_key === channelKey);
        const origins = [...new Set(emissions.map((value) => String(value.origin || ""))
            .filter(Boolean))];
        const linkedEmission = !text && (candidate?.emissions || []).find((value) =>
            value?.emission_group_id === attachment.emission_group_id
                && value?.attachment_id !== attachment.attachment_id
                && value?.channel_key === channelKey);
        for (const placement of visiblePlacements) {
            const projection = document.createElement("button");
            projection.type = "button";
            projection.dataset.attachmentId = attachment.attachment_id;
            projection.dataset.sonderPromptProjection = "1";
            projection.textContent = `${LABELS[attachment.kind] || attachment.kind}: ${identity || "Context"}${text ? "" : " · no output"}`;
            projection.style.cssText = `max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:2px 7px;border:1px solid ${COLORS.border};border-radius:999px;background:${COLORS.panelRaised};color:${COLORS.accentHi};font:9px/1.3 system-ui;cursor:pointer;`;
            projection.title = [
                candidate?._stale ? "Preview is updating; showing the latest scene-matched compile." : "",
                identity ? `Source: ${identity}` : "Source: Context attachment",
                text ? `Contribution to ${channelKey}:\n${text}`
                    : (linkedEmission
                        ? "Emitted by a linked chip elsewhere."
                        : "This routed attachment currently contributes nothing here."),
                origins.length ? `Origin: ${origins.join(", ")}` : "",
                `Placement: ${placementDisplayLabel(placement)}`,
            ].filter(Boolean).join("\n\n");
            if (!text) {
                projection.dataset.sonderPromptProjectionMuted = "1";
                projection.style.opacity = "0.58";
            }
            projection.addEventListener("mousedown", (event) => event.preventDefault());
            projection.addEventListener("click", () => onActivate?.(attachment));
            const after = ["section_suffix", "channel_suffix"].includes(placement);
            (after ? afterHost : beforeHost).appendChild(projection);
        }
    }
    return { beforeHost, afterHost };
}

export function createScopeChipRow({ attachments = [], previews = {}, disabled = false,
    onAdd = null, onActivate = null, onRemove = null, maxVisible = 6,
    allowedKinds = SCOPE_KINDS, attachmentLabelFor = null,
    reusableAttachments = [], onReuse = null, onUnlink = null,
    onConvertPromptLinkCopy = null, profile = null,
    allSceneAttachments = [], reuseContext = {} } = {}) {
    const row = document.createElement("div");
    row.style.cssText = "display:flex;gap:4px;align-items:center;flex-wrap:wrap;min-width:0;";
    const values = normalizePromptAttachments(attachments);
    const groupCounts = new Map();
    for (const value of normalizePromptAttachments(
        allSceneAttachments.length ? allSceneAttachments : values)) {
        groupCounts.set(value.emission_group_id,
            (groupCounts.get(value.emission_group_id) || 0) + 1);
    }
    values.slice(0, maxVisible).forEach((attachment) => {
        const holder = document.createElement("span");
        holder.style.cssText = "display:flex;align-items:center;gap:3px;flex-wrap:wrap;min-width:0;max-width:100%;";
        const chip = document.createElement("button");
        chip.type = "button";
        chip.dataset.attachmentId = attachment.attachment_id;
        chip.style.cssText = chipCss();
        // After the style, never before: assigning cssText would wipe the
        // dimming, and the flag alone leaves a control that looks live.
        setButtonDisabled(chip, disabled);
        const label = attachmentLabel(attachment,
            previews?.[attachment.attachment_id] || "",
            attachmentLabelFor?.(attachment) || "");
        chip.setAttribute("aria-label", `${label} Context chip`);
        chip.appendChild(contextChipLabel(label));
        const inlineOnly = INLINE_ONLY_KINDS.includes(attachment.kind);
        chip.title = inlineOnly
            ? `${label} — ${LABELS[attachment.kind] || attachment.kind} must be placed inline in the prompt text; re-insert or remove this chip.`
            : `${label} — dynamic prompt context`;
        if (inlineOnly) {
            // A legacy scope row stays visible and reachable so it can be
            // rebound or removed; the compiler refuses the job meanwhile.
            chip.style.borderColor = COLORS.dangerBorder;
            chip.style.color = COLORS.dangerText;
        }
        chip.addEventListener("click", () => onActivate?.(attachment));
        chip.addEventListener("keydown", (event) => {
            if (["Backspace", "Delete"].includes(event.key) && onRemove) {
                event.preventDefault(); onRemove(attachment);
            }
        });
        holder.appendChild(chip);
        if (attachment.kind === "prompt_link_scope" && onRemove) {
            const unlinkScope = document.createElement("button");
            unlinkScope.type = "button";
            unlinkScope.textContent = "Unlink";
            unlinkScope.title = "Remove the live section dependency without copying its text.";
            unlinkScope.style.cssText = `padding:2px 5px;border:1px solid ${COLORS.border};border-radius:4px;background:${COLORS.panelRaised};color:${CHIP_PALETTE.control};font:8px system-ui;cursor:pointer;`;
            setButtonDisabled(unlinkScope, disabled);
            unlinkScope.addEventListener("click", () => onRemove(attachment));
            holder.appendChild(unlinkScope);
            if (onConvertPromptLinkCopy) {
                const copy = document.createElement("button");
                copy.type = "button";
                copy.textContent = "Convert to copy";
                copy.title = "Copy the source's current authored channel text here, then remove the live link.";
                copy.style.cssText = unlinkScope.style.cssText;
                setButtonDisabled(copy, disabled);
                copy.addEventListener("click", () => onConvertPromptLinkCopy(attachment));
                holder.appendChild(copy);
            }
        }
        if ((groupCounts.get(attachment.emission_group_id) || 0) > 1) {
            const linked = document.createElement("span");
            linked.dataset.sonderLinkedAttachment = "1";
            linked.textContent = "linked";
            linked.title = "Edits to this configured chip propagate to every linked section.";
            linked.style.cssText = `margin-left:3px;font:8px system-ui;color:${CHIP_PALETTE.linked};text-transform:uppercase;letter-spacing:.04em;`;
            holder.appendChild(linked);
            if (onUnlink) {
                const unlink = document.createElement("button");
                unlink.type = "button";
                unlink.textContent = "Unlink";
                unlink.title = "Give only this chip an independent emission group.";
                unlink.style.cssText = `flex:0 0 auto;min-width:16px;min-height:16px;border:0;border-radius:3px;background:transparent;color:${CHIP_PALETTE.control};font:9px/16px system-ui;cursor:pointer;padding:0 3px;outline-offset:1px;`;
                setButtonDisabled(unlink, disabled);
                unlink.addEventListener("click", () =>
                    onUnlink(unlinkPromptAttachment(attachment), attachment));
                holder.appendChild(unlink);
            }
        }
        if (onRemove) {
            const remove = document.createElement("button");
            remove.type = "button";
            remove.textContent = "×";
            remove.title = `Remove ${label} Context chip`;
            remove.setAttribute("aria-label", remove.title);
            remove.style.cssText = `flex:0 0 auto;min-width:16px;min-height:16px;border:0;border-radius:3px;background:transparent;color:${CHIP_PALETTE.control};font:12px/16px system-ui;cursor:pointer;padding:0 3px;outline-offset:1px;`;
            setButtonDisabled(remove, disabled);
            remove.addEventListener("click", () => onRemove(attachment));
            holder.appendChild(remove);
        }
        row.appendChild(holder);
    });
    if (values.length > maxVisible) {
        const count = document.createElement("span");
        count.textContent = `+${values.length - maxVisible}`;
        count.title = `${values.length - maxVisible} more Context chips`;
        count.style.cssText = `font:10px system-ui;color:${COLORS.textDim};`;
        row.appendChild(count);
    }
    const kindSelect = document.createElement("select");
    kindSelect.setAttribute("aria-label", "Scope Context chip type");
    kindSelect.style.cssText = `cursor:pointer;${
        chromeInputCss({ padding: "1px 3px", fontSize: "10px" })}`;
    setButtonDisabled(kindSelect, disabled);
    // Inline-only kinds are filtered here rather than at each caller, so every
    // scope row in Timeline, Structured and Writing agrees. The format gate runs
    // in the same place for the same reason: both authoring routes — this row
    // and the caret menu — must offer exactly what the format declares.
    const scopeKinds = promptContextGatedKinds(
        allowedKinds.filter((value) => SCOPE_KINDS.includes(value)), profile);
    for (const kind of scopeKinds) {
        const option = document.createElement("option");
        option.value = kind; option.textContent = LABELS[kind] || kind;
        kindSelect.appendChild(option);
    }
    const add = document.createElement("button");
    add.type = "button";
    add.textContent = "Attach to this section/scene";
    add.style.cssText = `font:10px system-ui;background:transparent;color:${CHIP_PALETTE.control};border:1px dashed ${CHIP_PALETTE.border};border-radius:999px;padding:1px 6px;cursor:pointer;`;
    setButtonDisabled(add, disabled);
    // Falling back to a hardcoded "custom" would attach a kind the format may
    // not declare and this row may not even be offering.
    add.addEventListener("click", () => {
        const kind = kindSelect.value || scopeKinds[0] || "";
        if (kind) onAdd?.(kind);
    });
    row.append(kindSelect, add);
    const reusableKinds = new Set(
        allowedKinds.filter((value) => SCOPE_KINDS.includes(value)));
    const reusable = normalizePromptAttachments(reusableAttachments).filter((value) =>
        reusableKinds.has(value.kind)
        && !values.some((current) => current.attachment_id === value.attachment_id
            || current.emission_group_id === value.emission_group_id));
    const reusableGroups = dedupeReusableAttachments(reusable, reuseContext);
    if (onReuse && reusableGroups.length) {
        const reuseSelect = document.createElement("select");
        reuseSelect.setAttribute("aria-label", "Reuse an existing Context chip");
        reuseSelect.style.cssText = kindSelect.style.cssText;
        setButtonDisabled(reuseSelect, disabled);
        const labels = reusableGroups.map((entry) => attachmentReuseLabel(
            entry.attachment, { ...reuseContext, attachmentLabelFor }));
        const labelCounts = new Map();
        for (const label of labels) {
            labelCounts.set(label, (labelCounts.get(label) || 0) + 1);
        }
        for (const [entryIndex, entry] of reusableGroups.entries()) {
            const attachment = entry.attachment;
            const option = document.createElement("option");
            option.value = attachment.attachment_id;
            const baseLabel = labels[entryIndex];
            const origin = labelCounts.get(baseLabel) > 1 && entry.section_index >= 0
                ? ` — section ${entry.section_index + 1} [${entry.start_frame}-${entry.end_frame}]`
                : "";
            option.textContent = `${LABELS[attachment.kind] || attachment.kind}: ${baseLabel}${origin} — used in ${entry.count} section${entry.count === 1 ? "" : "s"}`;
            reuseSelect.appendChild(option);
        }
        const reuse = document.createElement("button");
        reuse.type = "button";
        reuse.textContent = "Reuse an existing chip";
        reuse.style.cssText = add.style.cssText;
        setButtonDisabled(reuse, disabled);
        reuse.addEventListener("click", () => {
            const source = reusableGroups.find((value) =>
                value.attachment.attachment_id === reuseSelect.value)?.attachment;
            if (source) onReuse(reusePromptAttachment(source), source);
        });
        row.append(reuseSelect, reuse);
    }
    return row;
}
