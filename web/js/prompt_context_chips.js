// Shared structured prompt editor used by timeline bars and both Prompt-tool
// modes. Projects store semantic attachment ids; this module never writes
// provider ordinals, timestamps, or generated text into authored documents.

import { REFERENCE_VERDICT_LABEL, resolveReferenceVerdicts } from "./reference_resolution.js";
import { PRESERVE_DEFAULT, PRIORITY as KEY_PRIORITY,
    register as registerKeyboardConsumer } from "./keyboard_ownership.js";
import { promptToken, promptTokenDeclarationsFromProfile } from "./prompt_tokens.js";
import { openContextMenu } from "./editor_context_menu.js";
import {
    declaredFieldChoices,
    orderedReferenceDerived,
    referenceDerivedDeclarations,
    referenceFieldDeclaration,
} from "./prompt_profile_declarations.js";

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
            nodes.push({ type: "text", node_id: nodeId, text: String(value.text ?? "") });
        } else {
            const attachmentId = String(value.attachment_id || "").trim();
            if (!attachmentId) continue;
            const node = { type: "attachment", node_id: nodeId, attachment_id: attachmentId };
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
    for (const node of normalizePromptDocument(documentValue).nodes) {
        if (node.type === "attachment") {
            blocks.at(-1).push(structuredClone(node));
            continue;
        }
        const lines = String(node.text || "").split("\n");
        let reusedSourceId = false;
        lines.forEach((line, index) => {
            if (line.trim() === "---") {
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

/** Parse anchored `key:` header lines without flattening inline chips. */
export function splitPromptDocumentChannels(documentValue, channelKeys) {
    const keys = Array.isArray(channelKeys) && channelKeys.length ? channelKeys : ["visual"];
    const keySet = new Set(keys);
    const output = Object.fromEntries(keys.map((key) => [key, []]));
    let currentKey = keys[0];
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
    const targetEnabled = new Map((target.capabilities || []).map((value) => [
        String(value?.capability_id || value?.kind || ""), value?.enabled !== false,
    ]));
    const propagated = structuredClone(configured);
    propagated.capabilities = (propagated.capabilities || []).map((value) => {
        const capabilityId = String(value?.capability_id || value?.kind || "");
        return targetEnabled.has(capabilityId)
            ? { ...value, enabled: targetEnabled.get(capabilityId) }
            : value;
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
            placement: String(projection?.declared_placement || "section_prefix"),
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
    const unit = (semanticUnits || []).find((row) =>
        String(row?.semantic_unit_id || "") === value);
    if (!unit) return [];
    const setup = (scene?.minimax_h3_conditioning_setups || []).find((row) =>
        String(row?.setup_id || "") === String(scene?.active_minimax_h3_setup_id || ""));
    if (setup?.mode !== "reference") return [];
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
    for (const laneId of [
        ...(setup.picture_lane_ids || []),
        ...(setup.video_lane_ids || []),
        ...(setup.audio_lane_ids || []),
    ].map(String)) {
        const laneIndex = recipes.findIndex((wrapper) =>
            String(wrapper?.lane_id || "") === laneId);
        if (laneIndex < 0) continue;
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
    setupPopulations = new Map(), requiresSetup = false, scope = {} } = {}) {
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
            setupPopulation: setupPopulations instanceof Map
                ? (setupPopulations.get(String(wrapper.lane_id || "")) || "")
                : (setupPopulations?.[String(wrapper.lane_id || "")] || ""),
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
    if (requiresSetup && !states.some((state) =>
        state.profileCompatible && state.setupPopulation)) {
        return { eligible: false, appliesNow: false,
            reason: "not in active setup", suffix: " - not in active setup" };
    }
    const compatibleStates = states.filter((state) =>
        state.profileCompatible && (!requiresSetup || !!state.setupPopulation));
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
        field, { label: formatSource, tier: "format" },
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
            const stagedSource = `Staged Reference default · @${
                member.handle || member.member_id}`;
            const values = { ...formatValues };
            const fieldSources = { ...formatFieldSources };
            withSemanticFallback(values, fieldSources, "visual_intent",
                commonStagedValue(rows, "visual_intent") || "preserve",
                rows.length
                    ? { label: stagedSource, tier: "shared" }
                    : { label: rendererFallbackSource, tier: "format" });
            withSemanticFallback(values, fieldSources, "audio_intent",
                commonStagedValue(rows, "audio_intent") || "reference_characteristics",
                rows.length
                    ? { label: stagedSource, tier: "shared" }
                    : { label: rendererFallbackSource, tier: "format" });
            return {
                values, source: stagedSource, formatSource, fieldSources,
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
        const values = { ...formatValues, ...sharedValues };
        const fieldSources = { ...formatFieldSources,
            ...Object.fromEntries(Object.keys(sharedValues).map((field) => [
                field, { label: sharedSource, tier: "shared" },
            ])) };
        const stagedRows = stagedRowsFor((unit.sources || []).map((source) =>
            source?.member_id));
        const stagedSource = `Staged Reference default · @${
            unit.handle || unit.semantic_unit_id}`;
        withSemanticFallback(values, fieldSources, "definition", unit.definition || "",
            { label: sharedSource, tier: "shared" });
        const sourceMemberIds = new Set((unit.sources || []).map((source) =>
            String(source?.member_id || "")).filter(Boolean));
        const stagedPrompts = [...new Set((setupManifest?.presentation || [])
            .filter((row) => sourceMemberIds.has(String(
                row?.member_id || row?.video_member_id || "")))
            .map((row) => String(row?.member_prompt || "").trim()).filter(Boolean))];
        withSemanticFallback(values, fieldSources, "definition", stagedPrompts.join("; "),
            { label: stagedSource, tier: "shared" });
        withSemanticFallback(values, fieldSources, "visual_intent",
            commonStagedValue(stagedRows, "visual_intent")
                || unit.visual_intent || "preserve",
            commonStagedValue(stagedRows, "visual_intent")
                ? { label: stagedSource, tier: "shared" }
                : (unit.visual_intent
                    ? { label: sharedSource, tier: "shared" }
                    : { label: rendererFallbackSource, tier: "format" }));
        withSemanticFallback(values, fieldSources, "audio_intent",
            commonStagedValue(stagedRows, "audio_intent")
                || unit.audio_intent || "reference_characteristics",
            commonStagedValue(stagedRows, "audio_intent")
                ? { label: stagedSource, tier: "shared" }
                : (unit.audio_intent
                    ? { label: sharedSource, tier: "shared" }
                    : { label: rendererFallbackSource, tier: "format" }));
        return {
            values,
            source: sharedSource, formatSource,
            fieldSources,
        };
    }
    return { values: formatValues, source: formatSource, formatSource,
        fieldSources: formatFieldSources };
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
            ? { label: "Chip override", tier: "chip" }
            : (inherited.fieldSources?.[field]
                || { label: inherited.formatSource, tier: "format" });
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
    capabilityId = "", enabled = true, channelKey = "", placement = "" } = {}) {
    const capability = {
        ...current,
        capability_id: capabilityId,
        kind: current?.kind || capabilityId,
        enabled: !!enabled,
    };
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

function contextChipLabel(label) {
    const chipLabel = document.createElement("span");
    chipLabel.dataset.sonderContextChipLabel = "1";
    chipLabel.textContent = label;
    chipLabel.style.cssText = "display:-webkit-box;min-width:0;overflow:hidden;"
        + "overflow-wrap:anywhere;white-space:normal;line-height:14px;"
        + "-webkit-box-orient:vertical;-webkit-line-clamp:2;";
    return chipLabel;
}

function chipCss() {
    return `display:inline-flex;align-items:center;gap:3px;box-sizing:border-box;min-width:0;
        max-width:min(180px,calc(100% - 4px));padding:1px 6px;
        margin:0 2px;border:1px solid #6f62a8;border-radius:999px;background:#29243b;
        color:#ded6ff;font:10px/17px system-ui,sans-serif;vertical-align:baseline;cursor:pointer;
        user-select:none;white-space:normal;overflow:hidden;`;
}

function editorCss(compact) {
    return `box-sizing:border-box;width:100%;min-width:${compact ? "80px" : "140px"};
        flex:0 0 auto;
        min-height:${compact ? "30px" : "48px"};max-height:${compact ? "100px" : "none"};
        overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;padding:5px 7px;
        border:1px solid #3b4656;border-radius:4px;background:#131820;color:#d8dee8;
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
        const next = editor.childNodes[childOffset];
        if (next instanceof HTMLElement && next.dataset.nodeId) {
            return { node_id: next.dataset.nodeId, offset: 0 };
        }
        const previous = editor.childNodes[childOffset - 1];
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

    const render = (bookmark = selectionBookmark()) => {
        const ownedFocus = document.activeElement === editor || editor.contains(document.activeElement);
        rendering = true;
        editor.textContent = "";
        const byId = attachmentById();
        for (const node of model.nodes) {
            if (node.type === "text") {
                const span = document.createElement("span");
                span.dataset.nodeId = node.node_id;
                span.dataset.nodeType = "text";
                // A zero-width character keeps an empty stable text node
                // addressable without entering the normalized projection.
                span.textContent = node.text || "\u200b";
                editor.appendChild(span);
                continue;
            }
            const attachment = byId.get(node.attachment_id);
            if (!attachment) continue;
            const chip = document.createElement("span");
            chip.contentEditable = "false";
            chip.dataset.nodeId = node.node_id;
            chip.dataset.nodeType = "attachment";
            chip.dataset.attachmentId = attachment.attachment_id;
            chip.style.cssText = chipCss();
            const label = attachmentLabel(attachment,
                previews?.[attachment.attachment_id] || "",
                attachmentLabelFor?.(attachment) || "", attachmentContext);
            chip.setAttribute("role", "button");
            chip.tabIndex = 0;
            chip.title = `${label} — dynamic context; activate to configure`;
            chip.setAttribute("aria-label", `${label} context chip; activate to configure`);
            const chipLabel = contextChipLabel(label);
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
            chip.append(chipLabel, remove);
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
        }
        rendering = false;
        if (ownedFocus && bookmark) {
            editor.focus({ preventScroll: true });
            restoreSelection(bookmark);
        }
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
            if (child.dataset.nodeType === "attachment") {
                next.push({ type: "attachment", node_id: child.dataset.nodeId || uid(),
                    attachment_id: child.dataset.attachmentId });
            } else {
                next.push({ type: "text", node_id: child.dataset.nodeId || uid(),
                    text: String(child.innerText || child.textContent || "").replaceAll("\u200b", "") });
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
        pushHistory();
        const plain = event.clipboardData?.getData("text/plain") || "";
        document.execCommand?.("insertText", false, plain);
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
                        chip = host.previousElementSibling?.dataset?.nodeType === "attachment"
                            ? host.previousElementSibling : null;
                    } else if (event.key === "Delete" && range.startOffset >= length) {
                        chip = host.nextElementSibling?.dataset?.nodeType === "attachment"
                            ? host.nextElementSibling : null;
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

    const insertAttachment = (rawAttachment, capabilityId = "") => {
        if (disabled) return null;
        pushHistory();
        const attachment = normalizePromptAttachment(rawAttachment);
        attachments = [...attachments.filter((value) => value.attachment_id !== attachment.attachment_id), attachment];
        const node = { type: "attachment", node_id: uid(), attachment_id: attachment.attachment_id };
        if (capabilityId) node.capability_id = capabilityId;

        // Translate the active caret to a model insertion boundary. When the
        // browser selection is inside a text node, split that stable text node.
        const range = selectionPoint(editor);
        let index = model.nodes.length;
        if (range) {
            const hostNode = (range.startContainer.nodeType === Node.TEXT_NODE
                ? range.startContainer.parentElement : range.startContainer);
            const direct = hostNode === editor ? null : hostNode?.closest?.("[data-node-id]");
            if (direct && editor.contains(direct)) {
                index = model.nodes.findIndex((value) => value.node_id === direct.dataset.nodeId);
                if (index < 0) index = model.nodes.length;
                else if (direct.dataset.nodeType === "text") {
                    const current = model.nodes[index];
                    const offset = range.startContainer.nodeType === Node.TEXT_NODE
                        ? Math.min(current.text.length, range.startOffset) : current.text.length;
                    const replacement = [];
                    if (current.text.slice(0, offset)) replacement.push({ ...current, text: current.text.slice(0, offset) });
                    replacement.push(node);
                    if (current.text.slice(offset)) replacement.push({ type: "text", node_id: uid(), text: current.text.slice(offset) });
                    model.nodes.splice(index, 1, ...replacement);
                    render();
                    const chip = editor.querySelector(`[data-node-id="${node.node_id}"]`);
                    if (chip) setCaretAfter(chip);
                    emit("attachment");
                    return attachment;
                } else {
                    index += 1;
                }
            }
        }
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
    editor.insertText = (rawText) => {
        if (disabled) return;
        const value = String(rawText ?? "");
        if (!value) return;
        pushHistory();
        editor.focus();
        if (typeof document.execCommand === "function") {
            document.execCommand("insertText", false, value);
            readDom();
            return;
        }
        const tail = model.nodes.at(-1);
        if (tail?.type === "text") tail.text += value;
        else model.nodes.push({ type: "text", node_id: uid(), text: value });
        render(); emit();
    };
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
    render();
    return editor;
}

export function promptInsertionBookmark(editor) {
    const bookmark = editor?.capturePromptSelection?.();
    if (!bookmark?.start) return bookmark || null;
    return { start: structuredClone(bookmark.start), end: structuredClone(bookmark.start) };
}

function restorePromptInsertion(editor, bookmark) {
    const restored = editor?.restorePromptSelection?.(bookmark);
    if (bookmark && restored === false) {
        globalThis.console?.warn?.(
            "[Sonder] Prompt changed while configuration was open; insertion was cancelled.");
        return false;
    }
    return true;
}

function promptWritingAids(writingAids) {
    return (Array.isArray(writingAids) ? writingAids : [])
        .filter((aid) => aid?.id);
}

async function insertWritingAid(editor, bookmark, aid, onInserted) {
    const cancel = () => restorePromptInsertion(editor, bookmark);
    let value = String(aid?.text || "");
    for (const [fieldName, declaration] of Object.entries(aid?.fields || {})) {
        if (!value.includes(`{${fieldName}}`)) continue;
        const allowed = declaration?.type === "enum" && Array.isArray(declaration.values)
            ? declaration.values.map(String) : [];
        if (!allowed.length) return;
        const selected = globalThis.prompt?.(
            `${String(fieldName).replaceAll("_", " ")} (${allowed.join(", ")})`, allowed[0]);
        if (selected == null) { cancel(); return; }
        if (!allowed.includes(selected)) {
            globalThis.alert?.(`Choose one of: ${allowed.join(", ")}`);
            cancel();
            return;
        }
        value = value.replaceAll(`{${fieldName}}`, selected);
    }
    if (value.includes("{text}")) {
        const text = globalThis.prompt?.("Text", "");
        if (text == null) { cancel(); return; }
        value = value.replaceAll("{text}", text);
    }
    if (!restorePromptInsertion(editor, bookmark)) return;
    editor?.insertText?.(value);
    await onInserted?.({ type: "writing_aid", text: value });
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

export function promptContextAuthoringKinds(allowedKinds = AUTHORING_KINDS) {
    return (Array.isArray(allowedKinds) ? allowedKinds : AUTHORING_KINDS)
        .filter((kind) => AUTHORING_KINDS.includes(kind)
            && !SCOPE_ONLY_KINDS.includes(kind));
}

export function createPromptContextMenuItems({ editor, bookmark = null,
    allowedKinds = AUTHORING_KINDS, onCreate = null, writingAids = [],
    onInserted = null } = {}) {
    const kinds = promptContextAuthoringKinds(allowedKinds);
    const aids = promptWritingAids(writingAids);
    return [{
        label: "Insert at cursor",
        submenu: kinds.map((kind) => ({
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
        })),
    }, {
        label: "Writing aid",
        disabled: !aids.length,
        submenu: aids.map((aid) => ({
            label: String(aid.label || aid.id || "Aid"),
            writingAidId: String(aid.id || ""),
            action: () => insertWritingAid(editor, bookmark, aid, onInserted),
        })),
    }];
}

/** Install right-click and keyboard Context authoring on one prompt box. */
export function installPromptContextMenu({ editor, allowedKinds = AUTHORING_KINDS,
    onCreate = null, writingAids = [], onInserted = null } = {}) {
    if (!editor) return () => {};
    let closeMenu = null;
    const open = ({ x, y, pointCaret = false } = {}) => {
        if (editor.isPromptComposing?.()) return false;
        if (pointCaret) setPromptCaretFromPoint(editor, x, y);
        const bookmark = promptInsertionBookmark(editor);
        closeMenu?.();
        closeMenu = openContextMenu({ x, y, items: createPromptContextMenuItems({
            editor, bookmark, allowedKinds, onCreate, writingAids, onInserted,
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
    row.style.cssText = "display:grid;grid-template-columns:130px minmax(0,1fr);gap:8px;align-items:start;font:11px system-ui;color:#d8dee8;";
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
        description.style.cssText = "grid-column:2;font:9px/1.35 system-ui;color:#8f9bad;margin-top:-4px;";
        row.appendChild(description);
    }
    return row;
}

function textField(value = "", multiline = false) {
    const control = document.createElement(multiline ? "textarea" : "input");
    control.value = String(value ?? "");
    control.style.cssText = "box-sizing:border-box;width:100%;min-width:0;padding:5px 7px;border:1px solid #3b4656;border-radius:4px;background:#131820;color:#d8dee8;font:11px system-ui;";
    if (multiline) control.rows = 3;
    return control;
}

function selectField(options, value = "") {
    const select = document.createElement("select");
    select.style.cssText = "box-sizing:border-box;width:100%;padding:5px 7px;border:1px solid #3b4656;border-radius:4px;background:#131820;color:#d8dee8;font:11px system-ui;";
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
        panel.style.cssText = "width:min(620px,92vw);max-height:82vh;overflow:auto;padding:14px;border:1px solid #465266;border-radius:8px;background:#1a202a;box-shadow:0 18px 60px rgba(0,0,0,.55);display:flex;flex-direction:column;gap:10px;";
        const heading = document.createElement("div");
        heading.textContent = `${LABELS[attachment.kind] || "Context"} attachment`;
        heading.style.cssText = "font:600 13px system-ui;color:#eef2f8;";
        const hint = document.createElement("div");
        hint.textContent = `Stored as semantic intent; ${profileId} resolves provider text at preview/enqueue.`;
        hint.style.cssText = "font:10px system-ui;color:#9da9ba;";
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
            timeNotice.style.cssText = "font:11px/1.45 system-ui;color:#c8d2e0;padding:7px 9px;border:1px solid #39475b;border-radius:5px;background:#141a22;";
            timeNotice.textContent = resolved
                ? `Resolved section time: ${resolved}`
                : "Standalone Time resolves from this section's start/cut position at preview and execution time.";
            panel.appendChild(timeNotice);
        } else if (attachment.kind === "custom") {
            controls.text = textField(attachment.config.text, true);
            panel.append(fieldRow("Fixed text", controls.text));
            const validators = new Set((resolvedProfile?.validators || []).map(String));
            if (validators.has("minimax_base_setup")
                    || validators.has("minimax_reference_setup")) {
                controls.setupRole = selectField([
                    ["", "Text only"],
                    ["first", "Bound to active first-frame Guide"],
                    ["last", "Bound to active last-frame Guide"],
                ], attachment.config.setup_role || "");
                panel.append(fieldRow("Physical Guide binding", controls.setupRole,
                    "Optional capability for text that refers to the active H3 first- or last-frame Guide."));
            }
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
                controls.channel.multiple = true;
                controls.channel.size = Math.min(6, Math.max(2, channelOptions.length));
                const selectedChannels = new Set(
                    (attachment.source.channel_keys || []).map(String));
                [...controls.channel.options].forEach((option) => {
                    option.selected = selectedChannels.size
                        ? selectedChannels.has(option.value) : true;
                });
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
            controls.subjects = selectField((semanticUnits || []).map((value) => [
                value.semantic_unit_id, value.name || value.semantic_unit_id,
            ]), "");
            controls.subjects.multiple = true;
            controls.subjects.size = Math.min(5, Math.max(2, semanticUnits.length));
            const selectedSubjects = new Set(attachment.source.subject_ids || []);
            [...controls.subjects.options].forEach((option) => {
                option.selected = selectedSubjects.has(option.value);
            });
            // A Vocal Event with no Subject and no voice key has nothing to
            // number: the speaker id would be minted from the chip's own id and
            // would name no one in the scene.
            controls.bindingNotice = document.createElement("div");
            controls.bindingNotice.textContent =
                "Choose at least one Subject, or enter a stable Voice ID.";
            controls.bindingNotice.style.cssText =
                "grid-column:1/-1;font:10px system-ui;color:#f2b8a0;display:none;";
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
            const activeSetup = (scene?.minimax_h3_conditioning_setups || []).find(
                (value) => value?.setup_id === scene?.active_minimax_h3_setup_id);
            const setupPopulations = new Map();
            for (const [key, population] of [
                ["picture_lane_ids", "picture"], ["video_lane_ids", "video"],
                ["audio_lane_ids", "audio"],
            ]) {
                for (const laneId of activeSetup?.[key] || []) {
                    setupPopulations.set(String(laneId), population);
                }
            }
            const makeUnitOption = (value, sources) => {
                const eligibility = subjectSourceEligibility({
                    sources, referenceItems, laneRecipes,
                    verdicts: verdictResult.verdicts, profileId, setupPopulations,
                    requiresSetup: usesDeclaredSources,
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
                const declaredPopulation = ({
                    pictures: "picture", videos: "video", standalone_audios: "audio",
                })[String(recipe?.soft?.physical_population || "")] || "";
                const setupPopulation = setupPopulations.get(String(wrapper.lane_id || "")) || "";
                const population = setupPopulation || declaredPopulation;
                if (!population) return;
                const compatible = recipe?.soft?.compatible_profiles || ["generic@1"];
                const profileCompatible = compatible.includes(profileId);
                const setupCompatible = !!setupPopulation
                    && (!declaredPopulation || setupPopulation === declaredPopulation);
                const eligible = profileCompatible && setupCompatible
                    && (globalScope || verdict === "winner");
                let stateSuffix = "";
                if (!profileCompatible) stateSuffix = " - incompatible prompt format";
                else if (!setupCompatible) stateSuffix = " - not in active setup";
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
            const referenceOptions = [["", "Choose a Reference…", true],
                ...(usesDeclaredSources ? [...unitOptions, ...physicalOptions] : itemOptions)];
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
            const overrides = attachment.config.overrides
                && typeof attachment.config.overrides === "object"
                ? structuredClone(attachment.config.overrides) : {};
            const overriddenFields = new Set(Object.keys(overrides));
            controls.referenceOverrides = overrides;
            controls.referenceOverriddenFields = overriddenFields;
            const inherited = (field = "", capabilityKind = "") =>
                referencePromptDefaults(controls.reference.value, {
                    references, semanticUnits, profile: resolvedProfile,
                    setupManifest: candidate?.setup_manifest || {},
                    capabilityKind: capabilityKind
                        || REFERENCE_VALUE_CAPABILITY[field] || "",
                });
            const effectiveValue = (field, fallback = "") =>
                overriddenFields.has(field)
                    ? overrides[field]
                    : (Object.hasOwn(inherited(field).values, field)
                        ? inherited(field).values[field] : fallback);
            controls.definition = textField(effectiveValue("definition"), true);
            const inheritanceNotice = document.createElement("div");
            inheritanceNotice.style.cssText =
                "grid-column:2;font:10px/1.35 system-ui;color:#9fc8bc;margin-top:-4px;white-space:pre-wrap;";
            const updateInheritance = () => {
                const inherited = resolveReferenceSelectionInheritance(controls.reference.value, {
                    scene, references, semanticUnits,
                });
                const isPhysical = /^physical:/.test(controls.reference.value);
                controls.definition.placeholder = inherited.value ||
                    "Describe this Subject for prompt use…";
                inheritanceNotice.textContent = inherited.value
                    ? (isPhysical
                        ? `Available from ${inherited.source}. Enter or edit Definition to author it for this chip.`
                        : `Inherited from ${inherited.source}. Leave Definition blank to keep following it.`)
                    : "No inherited definition is available. Add a Definition here or to the Subject/Library member.";
                inheritanceNotice.style.color = inherited.value ? "#9fc8bc" : "#f2b8a0";
            };
            controls.audioDefinition = textField(effectiveValue("audio_definition"), true);
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
            controls.summary = textField(effectiveValue("summary"), true);
            const tokenStrip = document.createElement("div");
            tokenStrip.style.cssText = "grid-column:2;display:flex;gap:4px;align-items:center;flex-wrap:wrap;margin-top:-3px;";
            const insertSummaryToken = (value) => {
                if (!value) return;
                const start = Number.isInteger(controls.summary.selectionStart)
                    ? controls.summary.selectionStart : controls.summary.value.length;
                controls.summary.setRangeText(value, start, start, "end");
                controls.summary.dispatchEvent(new Event("input", { bubbles: true }));
                controls.summary.focus();
            };
            const renderTokenStrip = () => {
                tokenStrip.textContent = "";
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
                    empty.style.cssText = "font:9px/1.3 system-ui;color:#8792a5;";
                    tokenStrip.appendChild(empty);
                    return;
                }
                const ordinal = Number(ordinalManifest?.[declaration.manifestKey]?.[sourceId] || 0);
                const button = document.createElement("button");
                button.type = "button";
                const resolvedLabel = ordinal > 0
                    ? String(declaration.labelTemplate || "").replace("{n}", String(ordinal))
                    : "not in the current preview";
                button.textContent = storedHandle
                    ? `@${storedHandle} · ${resolvedLabel}`
                    : "Set this handle in Reference Prompting before inserting it";
                button.title = storedHandle
                    ? `Insert ${authoredToken}. The stable id is stored; provider ordinals resolve only during compilation.`
                    : "A suggested handle is presentation-only until the versioned Reference write succeeds.";
                button.disabled = !storedHandle;
                button.style.cssText = "padding:3px 6px;border:1px solid #4c5d73;border-radius:999px;background:#1b2531;color:#bdd8ee;font:9px/1.2 system-ui;cursor:pointer;";
                button.addEventListener("mousedown", (event) => event.preventDefault());
                button.addEventListener("click", () => insertSummaryToken(authoredToken));
                tokenStrip.appendChild(button);
            };
            const declaredSelect = (capabilityKind, fieldName, emptyLabel = "") => {
                const declaration = referenceFieldDeclaration(
                    resolvedProfile, capabilityKind, fieldName);
                if (!declaration) return null;
                const selected = effectiveValue(fieldName,
                    declaration.type === "enum_multi" ? [] : "");
                const choices = declaredFieldChoices(declaration)
                    .map((entry) => [entry.value, entry.label]);
                const saved = new Set((Array.isArray(selected)
                    ? selected : [selected]).map(String).filter(Boolean));
                for (const value of saved) {
                    if (!choices.some(([known]) => known === value)) {
                        choices.push([value, `Unsupported saved value: ${value}`]);
                    }
                }
                if (emptyLabel) choices.unshift(["", emptyLabel]);
                const control = selectField(choices, Array.isArray(selected) ? "" : selected);
                if (declaration.type === "enum_multi") {
                    control.multiple = true;
                    control.size = Math.min(6, Math.max(2, choices.length));
                    [...control.options].forEach((option) => {
                        option.selected = saved.has(option.value);
                    });
                }
                return control;
            };
            controls.taskTypes = declaredSelect("summary", "task_types");
            controls.mention = textField(effectiveValue("text"));
            controls.retentionDetail = textField(
                effectiveValue("retention_detail"), true);
            controls.audioRelationship = textField(
                effectiveValue("audio_relationship"), true);
            controls.visualIntent = declaredSelect(
                "retention", "visual_intent", "Inherit staged/entity default");
            controls.audioIntent = declaredSelect(
                "retention", "audio_intent", "Inherit staged/entity default");
            const referenceFieldRow = (label, control, help) =>
                fieldRow(label, control, help, { visibleHelp: true });
            const overrideControls = new Map([
                ["definition", controls.definition],
                ["audio_definition", controls.audioDefinition],
                ["summary", controls.summary],
                ["task_types", controls.taskTypes],
                ["text", controls.mention],
                ["retention_detail", controls.retentionDetail],
                ["audio_relationship", controls.audioRelationship],
                ["visual_intent", controls.visualIntent],
                ["audio_intent", controls.audioIntent],
            ].filter(([, control]) => control));
            const overrideStatus = new Map();
            const refreshCapabilityEffectiveValues = () => {
                for (const row of controls.capabilityRows?.values?.() || []) {
                    row.refreshEffective?.();
                }
            };
            const setControlValue = (field, control, value) => {
                if (field === "task_types") {
                    const selected = new Set(Array.isArray(value) ? value.map(String) : []);
                    [...control.options].forEach((option) => {
                        option.selected = selected.has(option.value);
                    });
                } else {
                    control.value = String(value ?? "");
                }
            };
            const refreshOverrideStatus = (field) => {
                const status = overrideStatus.get(field);
                const control = overrideControls.get(field);
                if (!status || !control) return;
                const { state, reset } = status;
                const isOverride = overriddenFields.has(field);
                const inheritedState = inherited(field);
                const source = inheritedState.fieldSources?.[field]
                    || { label: inheritedState.formatSource, tier: "format" };
                state.textContent = isOverride ? "Chip override" : source.label;
                state.dataset.sonderAuthorityTier = isOverride ? "chip" : source.tier;
                state.style.color = isOverride ? "#e9b77d"
                    : (source.tier === "shared" ? "#9fc8bc" : "#8792a5");
                state.style.fontWeight = isOverride ? "600" : "400";
                control.style.opacity = isOverride ? "1" : ".78";
                reset.disabled = !isOverride;
            };
            const refreshInheritedFields = () => {
                for (const [field, control] of overrideControls) {
                    const defaults = inherited(field).values;
                    if (!overriddenFields.has(field)) {
                        setControlValue(field, control,
                            Object.hasOwn(defaults, field) ? defaults[field] : "");
                    }
                    refreshOverrideStatus(field);
                }
            };
            const overridableFieldRow = (label, field, help) => {
                const control = overrideControls.get(field);
                const wrapper = document.createElement("div");
                wrapper.style.cssText = "display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px;align-items:start;";
                const reset = document.createElement("button");
                reset.type = "button";
                reset.textContent = "Reset";
                reset.title = "Delete this chip override and follow the current default.";
                reset.style.cssText = "padding:3px 6px;border:1px solid #455166;border-radius:4px;background:#1a212b;color:#b7c1cf;font:9px system-ui;cursor:pointer;";
                const state = document.createElement("span");
                state.style.cssText = "grid-column:1/-1;font:9px/1.25 system-ui;";
                overrideStatus.set(field, { state, reset });
                const markOverride = () => {
                    overriddenFields.add(field);
                    refreshOverrideStatus(field);
                    refreshCapabilityEffectiveValues();
                };
                control.addEventListener(field === "task_types" ? "change" : "input", markOverride);
                if (control.tagName === "SELECT" && field !== "task_types") {
                    control.addEventListener("change", markOverride);
                }
                reset.addEventListener("click", () => {
                    overriddenFields.delete(field);
                    delete overrides[field];
                    const defaults = inherited(field).values;
                    setControlValue(field, control,
                        Object.hasOwn(defaults, field) ? defaults[field] : "");
                    refreshOverrideStatus(field);
                    refreshCapabilityEffectiveValues();
                });
                wrapper.append(control, reset, state);
                const row = referenceFieldRow(label, wrapper, help);
                refreshOverrideStatus(field);
                return row;
            };
            const definitionRow = overridableFieldRow("Definition", "definition",
                "Blank while overridden is deliberately blank; Reset follows the Identity or physical Reference default.");
            controls.reference.title = "Choose a semantic Subject or a physical source from the active conditioning setup.";
            const referenceRows = [referenceFieldRow("Reference", controls.reference,
                "Choose a semantic Subject or a physical source from the active conditioning setup."),
                definitionRow,
                inheritanceNotice,
                overridableFieldRow("Audio definition", "audio_definition",
                    "Required when the Subject unit includes an Audio member."),
                referenceFieldRow("Audio target speaker", controls.audioSpeaker,
                    "Reuses the (Sx) assigned by that Subject's first managed Vocal Event; it never creates speaker order."),
                overridableFieldRow("Summary", "summary",
                    "Optional section summary text. Leave blank to use the provider's generated summary shape."),
                tokenStrip];
            const otherSummaryOwners = [
                ...(scene?.global_attachments || []),
                ...(scene?.prompt_sections || []).flatMap((value) => value?.attachments || []),
            ].filter((value) => value?.kind === "reference"
                && String(value?.attachment_id || "") !== String(attachment.attachment_id || "")
                && String(value?.config?.overrides?.summary || "").trim());
            if (String(effectiveValue("summary") || "").trim() && otherSummaryOwners.length) {
                const summaryOwnerNotice = document.createElement("div");
                summaryOwnerNotice.style.cssText = "grid-column:2;font:9px/1.35 system-ui;color:#e9b77d;margin-top:-4px;";
                summaryOwnerNotice.textContent = "Only one MiniMax Summary owner can emit per compile. Another Reference chip also carries Summary text.";
                referenceRows.push(summaryOwnerNotice);
            }
            if (controls.taskTypes) {
                const taskDeclaration = referenceFieldDeclaration(
                    resolvedProfile, "summary", "task_types") || {};
                referenceRows.push(overridableFieldRow("Summary task types", "task_types",
                    taskDeclaration.help || "Select values only to explicitly override the format default."));
            }
            referenceRows.push(
                overridableFieldRow("Inline mention output", "text",
                    "Leave blank to emit late-bound labels such as <Subject 1>."),
                overridableFieldRow("Preservation detail", "retention_detail",
                    "Required in MiniMax H3 Full Reference; describe exactly what is retained or transferred."),
                overridableFieldRow("Audio relationship", "audio_relationship",
                    "Optional authored relationship between the referenced audio and target event."));
            if (controls.visualIntent) {
                const declaration = referenceFieldDeclaration(
                    resolvedProfile, "retention", "visual_intent") || {};
                referenceRows.push(overridableFieldRow(
                    declaration.label || "Visual handling", "visual_intent",
                    declaration.help || "Override the inherited visual handling."));
            }
            if (controls.audioIntent) {
                const declaration = referenceFieldDeclaration(
                    resolvedProfile, "retention", "audio_intent") || {};
                referenceRows.push(overridableFieldRow(
                    declaration.label || "Audio handling", "audio_intent",
                    declaration.help || "Override the inherited audio handling."));
            }
            if (!profileResolved) {
                // Opened before the format catalog landed, every declared field
                // rendered NO ROW AT ALL and nothing said why — Summary task
                // types and both retention intents simply vanished. The stored
                // overrides are safe (a control that never rendered is excluded
                // from `overrideControls`, so its key never enters the save),
                // but silence reads as "this format has no such fields".
                //
                // Expiry: removable once the prompt format catalog is served
                // synchronously with the panel mount, so a chip can never open
                // ahead of it.
                const notice = document.createElement("div");
                notice.dataset.sonderPromptDeclaredFieldsState = "unresolved";
                notice.style.cssText = "grid-column:2;font:9px/1.35 system-ui;color:#e9b77d;";
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
                    const current = existing.get(capabilityId) || {
                        capability_id: capabilityId, kind: capabilityId,
                        enabled: true,
                    };
                    const row = document.createElement("div");
                    row.className = "sonder-prompt-routing-row";
                    const enabled = document.createElement("label");
                    enabled.className = "sonder-prompt-routing-label";
                    const checkbox = document.createElement("input");
                    checkbox.type = "checkbox";
                    checkbox.checked = current.enabled !== false;
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
                        enabled.style.color = "#f0b6a8";
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
                    state.style.cssText = "min-width:0;padding:2px 6px;border:1px solid #3d4d61;border-radius:999px;color:#a9bfd2;background:#151d27;font:9px/1.3 system-ui;";
                    const routingControls = document.createElement("div");
                    routingControls.className = "sonder-prompt-routing-controls";
                    channel.style.cssText += "min-width:0;";
                    placement.style.cssText += "min-width:0;";
                    routingControls.append(channel, placement, state);
                    const help = document.createElement("div");
                    help.className = "sonder-prompt-routing-help";
                    help.style.cssText = "min-width:0;font:9px/1.35 system-ui;color:#8f9bad;";
                    const updatePlacementHelp = () => {
                        help.textContent = placementHelpFor(placement.value);
                        placement.title = help.textContent;
                    };
                    placement.addEventListener("change", updatePlacementHelp);
                    updatePlacementHelp();
                    const effective = document.createElement("div");
                    effective.className = "sonder-prompt-effective-values";
                    const controlValue = (field) => {
                        const control = overrideControls.get(field);
                        if (!control) return overrides[field];
                        return field === "task_types"
                            ? [...(control.selectedOptions || [])]
                                .map((option) => option.value)
                            : control.value;
                    };
                    const refreshEffective = () => {
                        effective.textContent = "";
                        const compiledLine = document.createElement("div");
                        compiledLine.style.cssText = "display:grid;grid-template-columns:minmax(100px,.55fr) minmax(0,1fr) auto;gap:5px;align-items:baseline;font:9px/1.3 system-ui;";
                        const compiledLabel = document.createElement("span");
                        compiledLabel.textContent = "Last compiled output";
                        compiledLabel.style.color = "#8792a5";
                        const compiledValue = document.createElement("span");
                        compiledValue.textContent = projection
                            ? (String(projection.text || "") || `(${projection.state || "empty"})`)
                            : "Compile to resolve";
                        compiledValue.title = String(projection?.text || "");
                        compiledValue.style.cssText = "min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#cbd3df;";
                        const compiledSource = document.createElement("span");
                        compiledSource.textContent = "Compiler projection";
                        compiledSource.dataset.sonderAuthorityTier = "compiler";
                        compiledSource.style.cssText = "white-space:nowrap;color:#9eb8d8;font-weight:600;";
                        compiledLine.append(compiledLabel, compiledValue, compiledSource);
                        effective.appendChild(compiledLine);
                        const draftOverrides = { ...overrides };
                        for (const field of overriddenFields) {
                            draftOverrides[field] = controlValue(field);
                        }
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
                            note.style.cssText = "font:9px/1.3 system-ui;color:#8792a5;";
                            effective.appendChild(note);
                            return;
                        }
                        for (const value of values) {
                            const line = document.createElement("div");
                            line.style.cssText = "display:grid;grid-template-columns:minmax(100px,.55fr) minmax(0,1fr) auto;gap:5px;align-items:baseline;font:9px/1.3 system-ui;";
                            const label = document.createElement("span");
                            label.textContent = `Input · ${value.label}`;
                            label.style.color = "#8792a5";
                            const rendered = document.createElement("span");
                            const renderedValue = Array.isArray(value.value)
                                ? value.value.join(" + ") : String(value.value || "");
                            rendered.textContent = value.authored_empty
                                ? "(authored empty)"
                                : `${renderedValue || "(empty)"}${value.stored_empty
                                    ? " · stored empty" : ""}`;
                            rendered.title = renderedValue;
                            rendered.style.cssText = "min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#cbd3df;";
                            const source = document.createElement("span");
                            source.textContent = value.source;
                            source.dataset.sonderAuthorityTier = value.tier;
                            source.style.cssText = `white-space:nowrap;color:${
                                value.tier === "chip" ? "#e9b77d"
                                    : (value.tier === "shared" ? "#9fc8bc" : "#8792a5")
                            };font-weight:${value.tier === "chip" ? "600" : "400"};`;
                            line.append(label, rendered, source);
                            effective.appendChild(line);
                        }
                    };
                    row.append(enabled, routingControls, help, effective);
                    capabilityHost.appendChild(row);
                    controls.capabilityRows.set(capabilityId, {
                        current, checkbox, channel, placement, refreshEffective,
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
        const cancel = document.createElement("button");
        cancel.textContent = "Cancel";
        const save = document.createElement("button");
        save.textContent = "Attach";
        for (const button of [cancel, save]) {
            button.type = "button";
            button.style.cssText = "padding:5px 10px;border:1px solid #526079;border-radius:4px;background:#242c38;color:#e4e9f1;font:11px system-ui;cursor:pointer;";
        }
        save.style.background = "#51447d";
        actions.append(cancel, save);
        panel.appendChild(actions);
        backdrop.appendChild(panel);
        document.body.appendChild(backdrop);

        const finish = (value) => { backdrop.remove(); resolve(value); };
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
                if (controls.setupRole) {
                    if (controls.setupRole.value) {
                        attachment.config.setup_role = controls.setupRole.value;
                    } else {
                        delete attachment.config.setup_role;
                    }
                }
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
                const values = {
                    definition: controls.definition.value,
                    audio_definition: controls.audioDefinition.value,
                    summary: controls.summary.value,
                    text: controls.mention.value,
                    retention_detail: controls.retentionDetail.value,
                    audio_relationship: controls.audioRelationship.value,
                };
                if (controls.taskTypes) {
                    values.task_types = [...controls.taskTypes.selectedOptions]
                        .map((option) => option.value).filter(Boolean);
                }
                if (controls.visualIntent) {
                    values.visual_intent = controls.visualIntent.value;
                }
                if (controls.audioIntent) {
                    values.audio_intent = controls.audioIntent.value;
                }
                const savedOverrides = controls.referenceOverrides || {};
                const savedFields = controls.referenceOverriddenFields || new Set();
                for (const [field, value] of Object.entries(values)) {
                    if (savedFields.has(field)) savedOverrides[field] = value;
                    else delete savedOverrides[field];
                }
                attachment.config.overrides = savedOverrides;
                attachment.capabilities = [...(controls.capabilityRows?.entries() || [])]
                    .map(([capabilityId, row]) => sparseCapabilityRecord(row.current, {
                        capabilityId,
                        enabled: row.checkbox.checked,
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
    wrapper.style.cssText = "display:flex;flex-direction:column;min-width:0;border:1px solid #3b4656;border-radius:5px;background:#131820;overflow:hidden;";
    for (const host of [beforeHost, afterHost]) host.style.flex = "0 0 auto";
    editor.style.border = "0";
    editor.style.borderRadius = "0";
    editor.style.background = "transparent";
    wrapper.append(beforeHost, editor, afterHost);
    return wrapper;
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
    const capabilityRows = (candidate?.attachment_capability_projections || [])
        .filter((value) => value?.channel_key === channelKey
            && attachmentById.has(value?.attachment_id));
    if (capabilityRows.length) {
        const regions = splitCapabilityProjectionsByRegion(capabilityRows);
        for (const projectionRow of [...regions.before, ...regions.after]) {
            if (projectionRow.rendered_at_anchor === true) continue;
            const attachment = attachmentById.get(projectionRow.attachment_id);
            if (!attachment) continue;
            const text = String(projectionRow.text || "").trim();
            const identity = attachmentLabelFor?.(attachment) || "";
            const state = String(projectionRow.state || "empty");
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
            const sourceLabel = identity || LABELS[attachment.kind]
                || attachment.kind || "Context";
            const reason = String(projectionRow.state_reason || "No output");
            const isEmitting = state === "emitted" || state === "marker";
            const resolved = isEmitting ? (text || reason) : reason;
            projection.textContent = isEmitting
                ? `${resolved} · ${channelKey} · ${stateLabel}`
                : `${resolved} · ${channelKey}`;
            projection.setAttribute("aria-label",
                `${sourceLabel} to ${channelKey}, ${phaseLabel}, ${stateLabel}. ${resolved}`);
            projection.style.cssText = "min-width:0;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:2px 7px;border:1px solid #415267;border-radius:999px;background:#17212b;color:#aec8dc;font:9px/1.3 system-ui;cursor:pointer;text-align:left;";
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
                suppress.style.cssText = "min-width:16px;min-height:16px;border:0;border-radius:3px;background:transparent;color:#c9bfff;font:12px/16px system-ui;cursor:pointer;padding:0 2px;";
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
            projection.style.cssText = "max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:2px 7px;border:1px solid #415267;border-radius:999px;background:#17212b;color:#aec8dc;font:9px/1.3 system-ui;cursor:pointer;";
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
    onConvertPromptLinkCopy = null,
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
        chip.disabled = disabled;
        chip.dataset.attachmentId = attachment.attachment_id;
        chip.style.cssText = chipCss();
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
            chip.style.borderColor = "#b4603f";
            chip.style.color = "#f2c3ad";
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
            unlinkScope.disabled = disabled;
            unlinkScope.textContent = "Unlink";
            unlinkScope.title = "Remove the live section dependency without copying its text.";
            unlinkScope.style.cssText = "padding:2px 5px;border:1px solid #455166;border-radius:4px;background:#1a212b;color:#c9bfff;font:8px system-ui;cursor:pointer;";
            unlinkScope.addEventListener("click", () => onRemove(attachment));
            holder.appendChild(unlinkScope);
            if (onConvertPromptLinkCopy) {
                const copy = document.createElement("button");
                copy.type = "button";
                copy.disabled = disabled;
                copy.textContent = "Convert to copy";
                copy.title = "Copy the source's current authored channel text here, then remove the live link.";
                copy.style.cssText = unlinkScope.style.cssText;
                copy.addEventListener("click", () => onConvertPromptLinkCopy(attachment));
                holder.appendChild(copy);
            }
        }
        if ((groupCounts.get(attachment.emission_group_id) || 0) > 1) {
            const linked = document.createElement("span");
            linked.dataset.sonderLinkedAttachment = "1";
            linked.textContent = "linked";
            linked.title = "Edits to this configured chip propagate to every linked section.";
            linked.style.cssText = "margin-left:3px;font:8px system-ui;color:#b8a9ef;text-transform:uppercase;letter-spacing:.04em;";
            holder.appendChild(linked);
            if (onUnlink) {
                const unlink = document.createElement("button");
                unlink.type = "button";
                unlink.disabled = disabled;
                unlink.textContent = "Unlink";
                unlink.title = "Give only this chip an independent emission group.";
                unlink.style.cssText = "flex:0 0 auto;min-width:16px;min-height:16px;border:0;border-radius:3px;background:transparent;color:#c9bfff;font:9px/16px system-ui;cursor:pointer;padding:0 3px;outline-offset:1px;";
                unlink.addEventListener("click", () =>
                    onUnlink(unlinkPromptAttachment(attachment), attachment));
                holder.appendChild(unlink);
            }
        }
        if (onRemove) {
            const remove = document.createElement("button");
            remove.type = "button"; remove.disabled = disabled;
            remove.textContent = "×";
            remove.title = `Remove ${label} Context chip`;
            remove.setAttribute("aria-label", remove.title);
            remove.style.cssText = "flex:0 0 auto;min-width:16px;min-height:16px;border:0;border-radius:3px;background:transparent;color:#c9bfff;font:12px/16px system-ui;cursor:pointer;padding:0 3px;outline-offset:1px;";
            remove.addEventListener("click", () => onRemove(attachment));
            holder.appendChild(remove);
        }
        row.appendChild(holder);
    });
    if (values.length > maxVisible) {
        const count = document.createElement("span");
        count.textContent = `+${values.length - maxVisible}`;
        count.title = `${values.length - maxVisible} more Context chips`;
        count.style.cssText = "font:10px system-ui;color:#aeb7c5;";
        row.appendChild(count);
    }
    const kindSelect = document.createElement("select");
    kindSelect.disabled = disabled;
    kindSelect.setAttribute("aria-label", "Scope Context chip type");
    kindSelect.style.cssText = "font:10px system-ui;background:#171c24;color:#ccd3df;border:1px solid #3b4656;border-radius:4px;padding:1px 3px;";
    // Inline-only kinds are filtered here rather than at each caller, so every
    // scope row in Timeline, Structured and Writing agrees.
    for (const kind of allowedKinds.filter((value) => SCOPE_KINDS.includes(value))) {
        const option = document.createElement("option");
        option.value = kind; option.textContent = LABELS[kind] || kind;
        kindSelect.appendChild(option);
    }
    const add = document.createElement("button");
    add.type = "button";
    add.disabled = disabled;
    add.textContent = "Attach to this section/scene";
    add.style.cssText = "font:10px system-ui;background:transparent;color:#c9bfff;border:1px dashed #6f62a8;border-radius:999px;padding:1px 6px;cursor:pointer;";
    add.addEventListener("click", () => onAdd?.(kindSelect.value || "custom"));
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
        reuseSelect.disabled = disabled;
        reuseSelect.setAttribute("aria-label", "Reuse an existing Context chip");
        reuseSelect.style.cssText = kindSelect.style.cssText;
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
        reuse.disabled = disabled;
        reuse.textContent = "Reuse an existing chip";
        reuse.style.cssText = add.style.cssText;
        reuse.addEventListener("click", () => {
            const source = reusableGroups.find((value) =>
                value.attachment.attachment_id === reuseSelect.value)?.attachment;
            if (source) onReuse(reusePromptAttachment(source), source);
        });
        row.append(reuseSelect, reuse);
    }
    return row;
}
