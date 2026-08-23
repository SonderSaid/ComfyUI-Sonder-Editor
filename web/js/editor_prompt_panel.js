// Prompt Management panel — centered overlay for editing the scene-global
// prompt, the segment lane's sections (ranges + channels), the project-durable
// channel-template-aware prompt state and a PromptRelay payload preview.
//
// Module-host contract (fullscreen seam pattern): the host owns state,
// networking, and durable writes; this module owns its DOM/listeners and
// returns a cleanup handle. Host surface used:
//   activeScene, totalFrames, _settings, _updateSettings,
//   _projectDirName(), _timecodeMode/_frameToTimecode,
//   _resolveFrameConstraintForTemplate(_templateId),
//   _updateScenePrompt(text), _updatePromptSection(idx, fields),
//   _deletePromptSection(idx), _setSelectionToFrameRange(start, end),
//   _queuePromptSection(section), _addPromptSectionAfter(idx),
//   _addPromptSectionInFirstGap(), _applyPromptSetup(entry),
//   _fetchPromptPayload()/_fetchPromptHistory() -> Promise,
//   _getPromptTemplates()/_savePromptTemplate()/_deletePromptTemplate(),
//   _isPromptTrackLocked(), _isGlobalPromptTrackLocked()
//
// Improvements over the guide-management template: focus-aware Escape via
// keyboard_ownership at PRIORITY.OVERLAY (first Esc reverts the focused box
// without committing — closing paths suppress blur-commit), and the panel
// stays open after commits (rows refresh in place).

import {
    EDITOR_COLORS as COLORS,
    FONT,
    chromeButtonCss,
    chromeInputCss,
    chromeOverlayPanelCss,
    setButtonDisabled,
    setButtonVariant,
} from "./editor_theme.js";
import {
    register as registerKeyboardConsumer,
    PRIORITY as KEY_PRIORITY,
} from "./keyboard_ownership.js";
import {
    composeSectionText,
    joinChannelHeaders,
    normalizeChannels,
    sectionInheritsGlobal,
    splitChannelHeaders,
} from "./prompt_composition.js";
import {
    defaultDraftChannel,
    globalChannelKeys,
    templateChannelKeys,
} from "./prompt_channel_templates.js";
import {
    configurePromptAttachment,
    attachmentChannelProjectionSignature,
    createAttachmentChannelProjections,
    createPromptProjectionBox,
    createPromptDocumentEditor,
    createScopeChipRow,
    channelContributionRows,
    channelRegionsByNode,
    writingBlockNodeRanges,
    healSeparatorPadding,
    installPromptContextMenu,
    joinWritingSectionDocuments,
    normalizePromptAttachments,
    normalizePromptDocument,
    promptAttachmentAnchoredChannels,
    propagateLinkedPromptAttachment,
    promptDocumentText,
    resolveReferenceAttachmentIdentity,
    sceneWithDraftGlobal,
    sceneWithDraftSection,
    splitPromptDocumentChannels,
    normalizePromptAttachment,
    splitWritingPromptDocument,
    setPromptAttachmentCapabilityEnabled,
} from "./prompt_context_chips.js";
import { getProjectVersion } from "./api_client.js";
import { notifyInfo, notifySuccess, notifyWarning } from "./editor_notifications.js";
import { openContextMenu } from "./editor_context_menu.js";
import { resolvedPromptProfile } from "./prompt_profile_declarations.js";
import { mountPromptFormatDeclarationEditor } from "./prompt_format_editor.js";
import { createModalDraftGuard } from "./modal_draft_guard.js";
import {
    applyPromptIdentityChange,
    createModalRefreshGate,
    mountPromptIdentityPanel,
    promptReferenceAttachment,
    promptIdentityDependents,
    sameIdentitySnapshot,
} from "./prompt_identity_panel.js";
import {
    buildPromptContextDiagnostics,
    promptCandidateVisuallyStale,
    promptContextDiagnosticTitle,
} from "./prompt_context_diagnostics.js";

const WRITING_BREAK = "---";
const WRITING_DRAFT_TEXT_CAP = 20000;
const COPY_CAPABILITY_KINDS = new Set([
    "definitions", "retention", "mentions", "summary", "audio_relationship",
]);

export { promptIdentityDependents as referenceBackedSubjectDependents };

/** Split a writing-mode draft into blocks on `---` marker lines. */
export function splitWritingDraft(draft) {
    const blocks = [];
    let current = [];
    for (const line of String(draft ?? "").split("\n")) {
        if (line.trim() === WRITING_BREAK) {
            blocks.push(current.join("\n").trim());
            current = [];
        } else {
            current.push(line);
        }
    }
    blocks.push(current.join("\n").trim());
    return blocks.filter(Boolean);
}

/** The channel the caret is writing in: the last `key:` header at or above it.
 *
 *  Returns "" when no header precedes the caret, which means the block's
 *  default channel — the caller resolves that, because only it knows the
 *  template's `default_draft_channel` stamp.
 */
export function writingChannelAtCaret(text, offset, channelKeys) {
    const keys = new Set((channelKeys || []).map((value) => String(value)));
    const before = String(text ?? "").slice(0, Math.max(0, offset | 0));
    let active = "";
    for (const line of before.split("\n")) {
        const match = /^([A-Za-z0-9_]+):/.exec(line);
        if (match && keys.has(match[1])) active = match[1];
        // A section break resets to the block default, exactly as
        // `splitWritingDraft` + `splitPromptDocumentChannels` will read it back.
        else if (line.trim() === WRITING_BREAK) active = "";
    }
    return active;
}

/** The text from the caret's block start up to the caret.
 *
 *  `splitWritingDraft` cannot answer this: it `.trim()`s each block and
 *  `.filter(Boolean)`s the list, destroying the offsets needed to locate the
 *  caret. This keeps them, and returns the HEAD of the impending split — what
 *  stays behind — because the tail travels with the author and its channels
 *  are not stranded.
 */
export function writingBlockHeadAt(text, offset) {
    const value = String(text ?? "");
    const caret = Math.max(0, Math.min(value.length, offset | 0));
    const before = value.slice(0, caret);
    const lines = before.split("\n");
    for (let i = lines.length - 1; i >= 0; i -= 1) {
        if (lines[i].trim() === WRITING_BREAK) return lines.slice(i + 1).join("\n");
    }
    return before;
}

/** The channels a split at `caret` would strand — the whole decision.
 *
 *  Exported as one function so the composition (head-of-block, then linkage)
 *  is covered by a test rather than living inline in a click handler where
 *  only a source-string assertion could reach it. Passing the whole draft
 *  instead of the head is the defect this shape prevents.
 */
export function writingSplitDisclosure(draftText, caret, channelKeys) {
    const keys = channelKeys || [];
    const active = writingChannelAtCaret(draftText, caret, keys);
    return writingSplitLinkage(writingBlockHeadAt(draftText, caret), active, keys);
}

/** Which channels a split would strand, and therefore warrant a scope link.
 *
 *  A split continues ONE channel — the one the caret is in. Every other
 *  channel that already has text belongs to the block being left behind, so
 *  the new block starts empty in those channels and quietly loses them. When
 *  none are populated there is nothing to inherit and the split stays silent;
 *  when some are, the author is told, because a section that silently drops
 *  its summary and soundscape compiles to something they never wrote.
 *
 *  Pure: decides WHICH channels are at stake. Binding the actual
 *  `prompt_link_scope` needs a `prompt_id`, and sections do not exist until
 *  Apply, so the binding cannot happen here.
 */
export function writingSplitLinkage(blockText, activeChannel, channelKeys) {
    const keys = (channelKeys || []).map((value) => String(value));
    const active = String(activeChannel || "");
    const populated = [];
    let current = "";
    for (const line of String(blockText ?? "").split("\n")) {
        const match = /^([A-Za-z0-9_]+):(.*)$/.exec(line);
        if (match && keys.includes(match[1])) {
            current = match[1];
            if (match[2].trim() && current !== active && !populated.includes(current)) {
                populated.push(current);
            }
            continue;
        }
        if (line.trim() === WRITING_BREAK) { current = ""; continue; }
        if (!line.trim() || !current || current === active) continue;
        if (!populated.includes(current)) populated.push(current);
    }
    return populated;
}

/** The text a "Split here" inserts so the tail keeps writing where it was.
 *
 *  A bare `---` starts a new block, and a new block begins at its default
 *  channel — so splitting mid-paragraph under a non-default heading silently
 *  moved the tail into a different field. The heading is re-emitted unless the
 *  tail would land in that channel anyway, so the common case stays clean.
 */
export function writingSplitText(activeChannel, defaultChannel) {
    const active = String(activeChannel || "");
    const fallback = String(defaultChannel || "");
    const carry = active && active !== fallback ? `${active}:\n` : "";
    return `\n${WRITING_BREAK}\n${carry}`;
}

/** Min-first proportional allocator (audit F2 — min to every block FIRST so
 *  clamping can never overflow the budget): non-dirty chips share
 *  `total − Σdirty` proportionally by text length with largest-remainder. */
export function allocateWritingBlocks(blocks, total, minLen, existing = []) {
    const count = blocks.length;
    if (!count) return [];
    const result = blocks.map((_, i) => (existing[i]?.dirty
        ? { length: Math.max(minLen, existing[i].length | 0), dirty: true }
        : null));
    const dirtySum = result.reduce((sum, entry) => sum + (entry ? entry.length : 0), 0);
    const freeIdx = result.map((entry, i) => (entry === null ? i : -1)).filter((i) => i >= 0);
    if (!freeIdx.length) return result;
    const budget = Math.max(freeIdx.length * minLen, total - dirtySum);
    const extra = budget - freeIdx.length * minLen;
    const weights = freeIdx.map((i) => Math.max(1, blocks[i].length));
    const weightSum = weights.reduce((a, b) => a + b, 0);
    const raw = weights.map((w) => (extra * w) / weightSum);
    const floors = raw.map(Math.floor);
    let leftover = extra - floors.reduce((a, b) => a + b, 0);
    const byFraction = raw
        .map((value, k) => ({ k, frac: value - floors[k] }))
        .sort((a, b) => b.frac - a.frac);
    for (let n = 0; n < leftover; n += 1) floors[byFraction[n % byFraction.length].k] += 1;
    freeIdx.forEach((blockIdx, k) => {
        result[blockIdx] = { length: minLen + floors[k], dirty: false };
    });
    return result;
}

/** Minimal project-durable definition for the Prompt Format "New" action. */
export function freshPromptFormatDefinition(template = {}) {
    const channelKey = templateChannelKeys(template)[0] || "visual";
    return {
        template_id: String(template?.id || "standard"),
        capabilities: { custom: { channel_key: channelKey, placement: "inline",
            formatter: "{text}" } },
        writing_aids: [],
        separators: { attachment: " ", line: "\n" },
        validators: [],
        role_catalogs: {},
        physical_populations: [],
        identity_kinds: [],
    };
}

/** Suggest a distinct immutable version while keeping non-numeric ids usable. */
export function nextPromptFormatVersion(version) {
    const value = String(version || "1").trim() || "1";
    return /^\d+$/.test(value) ? String(Number(value) + 1) : `${value}.1`;
}

/** Which prompt-format actions a catalog descriptor allows.
 *
 *  Exported so the gating is a testable predicate rather than a grep over the
 *  menu wiring. Built-ins are immutable, so neither action applies to them.
 */
export function promptFormatMenuActions(descriptor) {
    const custom = Boolean(descriptor) && descriptor.builtin === false;
    return { edit: custom, remove: custom };
}

/** What Reference Prompting actually derives from in a candidate payload.
 *
 *  Those rows come from `candidate.setup_manifest`, NOT from `host._references`
 *  — so the section renders every population at (0) until a compile lands, and
 *  the compile's success path only refreshed diagnostics and inline
 *  projections. The section therefore kept its `candidate = null` output until
 *  some unrelated full render happened, which is why closing and reopening the
 *  panel "fixed" it: the cached candidate survives everything except a scene
 *  switch.
 *
 *  Comparing this signature rather than the whole payload is what allows a
 *  landed candidate to rebuild the section WITHOUT rebuilding on every
 *  debounced keystroke — typing changes the compiled text constantly but
 *  leaves the setup and ordinal manifests alone. Rebuilding per keystroke is
 *  the PR-11 reflow this must not reintroduce.
 */
export function identityCandidateSignature(candidate) {
    if (!candidate || typeof candidate !== "object") return "";
    return JSON.stringify([
        String(candidate._candidate_scene_id || ""),
        candidate.setup_manifest ?? null,
        candidate.ordinal_manifest ?? null,
    ]);
}

/** Read compiler-candidate and Relay-shaped payloads through one view seam. */
export function writingCompiledPayload(payload) {
    const source = payload && typeof payload === "object" ? payload : {};
    const prompt = Object.prototype.hasOwnProperty.call(source, "prompt")
        ? source.prompt : source.compiled_prompt;
    const channels = Object.prototype.hasOwnProperty.call(source, "channels")
        ? source.channels : source.compiled_channels;
    return {
        prompt: String(prompt || ""),
        channels: channels && typeof channels === "object" && !Array.isArray(channels)
            ? channels : {},
    };
}

/**
 * The parts of a Writing document that can move or change its read-only
 * contribution blocks.
 *
 * Authored prose is deliberately absent. A normal keystroke changes the text
 * and the compiled preview, but it does not change where a contribution is
 * painted. Channel headings, section breaks, and attachment membership do.
 * Keeping the distinction here prevents the candidate debounce from replacing
 * every contribution DOM node while the author types.
 */
export function writingDecorationDocumentSignature(documentValue, channelKeys,
    { defaultKey = "" } = {}) {
    const document_ = normalizePromptDocument(documentValue);
    const regions = channelRegionsByNode(document_, channelKeys, { defaultKey });
    const ranges = writingBlockNodeRanges(document_).map((range) => ({
        block: range.block,
        firstIndex: range.firstIndex,
    }));
    const blockAttachments = [[]];
    let block = 0;
    for (const node of document_.nodes) {
        if (node.type === "attachment") {
            blockAttachments[block].push(String(node.attachment_id || ""));
            continue;
        }
        for (const line of String(node.text || "").split("\n")) {
            if (line.trim() !== "---") continue;
            block += 1;
            blockAttachments[block] = [];
        }
    }
    return JSON.stringify({ regions, ranges, blockAttachments });
}

export async function materializePromptIdentityOwner(owner, semanticUnits, saveChange, saveOptions = {}) {
    if (owner?.type !== "identity") {
        return { owner, previous: null, next: null };
    }
    const identityId = String(owner?.identityId || "");
    const previous = (semanticUnits || []).find((unit) =>
        String(unit?.semantic_unit_id || "") === identityId);
    if (!previous) throw new Error("The prompt identity no longer exists.");
    const observedHandle = String(owner?.storedHandle || "").trim();
    const currentHandle = String(previous?.handle || "").trim();
    if (observedHandle && observedHandle !== currentHandle) {
        throw new Error("The prompt identity handle changed elsewhere. Reopen and try again.");
    }
    if (currentHandle) {
        // A dialog opened while the identity was blank may outlive a save in
        // another surface. Adopt the durable spelling; never overwrite it with
        // the stale suggestion the dialog captured.
        return {
            owner: { ...owner, handle: currentHandle, storedHandle: currentHandle },
            previous: null, next: null,
        };
    }
    const handle = String(owner?.handle || "").trim();
    if (!handle) throw new Error("The prompt identity handle could not be materialized.");
    const next = { ...structuredClone(previous), handle };
    const saved = await saveChange?.({
        type: "upsert", value: next, expected: structuredClone(previous),
    }, "materialize prompt identity handle", saveOptions);
    if (!saved) throw new Error("The prompt identity handle could not be materialized.");
    return {
        owner: { ...owner, handle, storedHandle: handle },
        previous: structuredClone(previous), next,
    };
}

export async function rollbackPromptIdentityAttachment(host, history = {}) {
    const change = history?.promptIdentityChange;
    if (!change) return true;
    try {
        if (typeof host?._applyPromptIdentityChange !== "function") {
            throw new Error("Prompt identity rollback is unavailable.");
        }
        await host._applyPromptIdentityChange(change,
            "rollback refused prompt identity attachment", { recordUndo: false });
        return true;
    } catch (error) {
        // The handle is still durable, so retain one target-safe recovery
        // action. It can retry after a transient failure and will refuse rather
        // than overwrite an identity that changed elsewhere.
        let targetChanged = error?.code === "prompt_identity_changed_elsewhere"
            || error?.code === "prompt_identity_created_elsewhere";
        if (!targetChanged && typeof host?._fetchReferences === "function") {
            const refreshed = await host._fetchReferences({
                ignoreMutationGate: true, reason: "prompt_identity_rollback_reconcile",
                force: true,
            });
            if (refreshed) {
                const identityId = String(change?.value?.semantic_unit_id || "");
                const current = (host._promptSemanticUnits || []).find((unit) =>
                    String(unit?.semantic_unit_id || "") === identityId) || null;
                // A lost response may report failure after the server already
                // applied the rollback. In that case there is nothing to undo.
                if (sameIdentitySnapshot(current, change.value)) return true;
                targetChanged = !sameIdentitySnapshot(current, change.expected);
            }
        }
        const recoveryRetained = !targetChanged
            && typeof host?._pushPromptIdentityUndo === "function";
        if (recoveryRetained) {
            host._pushPromptIdentityUndo("materialize prompt identity handle",
                change, history?.inversePromptIdentityChange);
        }
        throw new Error(`The prompt Reference attachment was refused; ${targetChanged
            ? "the identity changed elsewhere and was left untouched"
            : recoveryRetained
            ? "the new handle remains undoable"
            : "the new handle could not be added to Undo history"}. ${
            error?.message || "Rollback failed."}`);
    }
}

export async function rollbackPromptPhysicalAttachment(host, history = {}) {
    const operations = history?.referenceOperations || [];
    if (!operations.length) return true;
    try {
        if (typeof host?._mutateReferences !== "function") {
            throw new Error("Physical Reference rollback is unavailable.");
        }
        await host._mutateReferences(operations,
            "rollback refused prompt Reference attachment");
        return true;
    } catch (error) {
        let targetChanged = error?.code === "identity_mismatch";
        if (!targetChanged && typeof host?._fetchReferences === "function") {
            const refreshed = await host._fetchReferences({
                ignoreMutationGate: true, reason: "physical_handle_rollback_reconcile",
                force: true,
            });
            if (refreshed) {
                const operation = operations[0] || {};
                const memberId = String(operation.member_id || "");
                const current = (host._references || [])
                    .flatMap((reference) => reference?.members || [])
                    .find((member) => String(member?.member_id || "") === memberId);
                const currentHandle = String(current?.handle || "");
                const rollbackHandle = String(operation?.fields?.handle || "");
                const materializedHandle = String(operation?.expected?.handle || "");
                if (currentHandle === rollbackHandle) return true;
                targetChanged = currentHandle !== materializedHandle;
            }
        }
        const recoveryRetained = !targetChanged
            && typeof host?._pushReferenceUndo === "function";
        if (recoveryRetained) {
            host._pushReferenceUndo("materialize prompt Reference handle",
                operations, history?.inverseReferenceOperations || []);
        }
        throw new Error(`The prompt Reference attachment was refused; ${targetChanged
            ? "the physical Reference changed elsewhere and was left untouched"
            : recoveryRetained
            ? "the new handle remains undoable"
            : "the new handle could not be added to Undo history"}. ${
            error?.message || "Rollback failed."}`);
    }
}

/**
 * Writing-aid choices as the flat `field=a|b; other=c` authoring form.
 *
 * One spelling is shared by the New-aid row and the per-aid editors, so the two
 * paths cannot disagree about what an aid may declare.
 */
export function formatWritingAidChoices(fields = {}) {
    const declared = fields && typeof fields === "object" ? fields : {};
    return Object.entries(declared)
        .filter(([, declaration]) => declaration && typeof declaration === "object")
        .map(([name, declaration]) => {
            const values = (Array.isArray(declaration.values) ? declaration.values : [])
                .map((entry) => typeof entry === "string"
                    ? entry : String(entry?.value || ""))
                .filter(Boolean);
            return `${name}=${values.join("|")}`;
        })
        .join("; ");
}

/**
 * Parse that flat form back into declarations for the placeholders `text` uses.
 *
 * The flat form has one column and therefore cannot express a value's `label`,
 * `description`, or the field's `optional`/`help` — so any of those already
 * declared are CARRIED FORWARD for values that survive the edit. Without that,
 * opening a format built on `{value: "zooms in", label: "Zoom In"}` and saving
 * it unchanged would silently flatten every label to its raw value.
 */
export function parseWritingAidChoices(rawText, aidText = "", previousFields = {}) {
    const previous = previousFields && typeof previousFields === "object"
        ? previousFields : {};
    const declaredValues = new Map(String(rawText || "").split(";")
        .map((entry) => entry.split("="))
        .filter((parts) => parts.length === 2)
        .map(([field, values]) => [field.trim(), values.split("|")
            .map((value) => value.trim()).filter(Boolean)]));
    const fields = {};
    for (const match of String(aidText || "")
        .matchAll(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g)) {
        const name = match[1];
        if (name === "text" || fields[name]) continue;
        const prior = previous[name] && typeof previous[name] === "object"
            ? previous[name] : {};
        const priorByValue = new Map((Array.isArray(prior.values) ? prior.values : [])
            .map((entry) => typeof entry === "string"
                ? [entry, entry] : [String(entry?.value || ""), entry]));
        const values = (declaredValues.get(name)
            ?? [...priorByValue.keys()]).map((value) => priorByValue.get(value) ?? value);
        fields[name] = {
            ...prior,
            type: String(prior.type || "") === "enum_multi" ? "enum_multi" : "enum",
            values,
        };
    }
    return fields;
}

/** Plain-language reason a format cannot be deleted, from the served usages.
 *
 *  The server owns the usage RULE; this only renders what it reported. Never
 *  recompute which formats are in use here.
 */
function describePromptFormatUsages(usages = []) {
    const scenes = [];
    const recipes = [];
    let template = "";
    for (const usage of usages || []) {
        const type = String(usage?.type || "");
        if (type === "scene") scenes.push(String(usage?.scene_name || usage?.scene_id || "a scene"));
        else if (type === "reference_recipe") recipes.push(String(usage?.scene_id || "a scene"));
        else if (type === "channel_template") template = String(usage?.name || "the channel template");
    }
    const parts = [];
    if (scenes.length) parts.push(`${scenes.length === 1 ? "scene" : "scenes"} ${scenes.join(", ")}`);
    if (template) parts.push(`channel template ${template}`);
    if (recipes.length) {
        parts.push(`a Reference recipe in ${[...new Set(recipes)].join(", ")}`);
    }
    return parts.join("; ");
}

/** Every custom format the ⋯ menu can offer to delete, with its state.
 *
 *  The menu used to act ONLY on the format the picker had selected, and
 *  selecting a format is what puts it in use — so Delete was enabled exactly
 *  when the server was guaranteed to refuse, and no unused format could ever
 *  be reached. Targeting a list is the fix.
 *
 *  Exported and pure because the menu itself needs a whole fullscreen host and
 *  cannot run under node; this is where the behavior is actually testable.
 */
export function promptFormatDeleteTargets(profiles = []) {
    return (profiles || [])
        .filter((row) => row && row.builtin === false && row.key)
        .map((row) => {
            const usages = Array.isArray(row.usages) ? row.usages : [];
            const reason = describePromptFormatUsages(usages);
            return {
                key: String(row.key),
                name: String(row.name || row.key),
                profile_id: String(row.profile_id || ""),
                version: String(row.version || "1"),
                deletable: usages.length === 0,
                usages,
                reason: usages.length
                    ? `In use by ${reason || "this project"}. Move it off them first.`
                    : "",
            };
        })
        .sort((left, right) => left.name.localeCompare(right.name));
}

/** Preserve the source format's channel-template contract when forking. */
export function promptFormatTemplateBinding(descriptor = {}, definition = {},
    universalToken = "*") {
    const sourceTemplates = descriptor?.compatible_templates
        || (Array.isArray(definition?.compatible_templates)
            ? definition.compatible_templates : null)
        || [String(definition?.template_id || "standard")];
    return sourceTemplates.includes(String(universalToken || "*"))
        ? { template_id: "standard" }
        : { template_id: String(sourceTemplates[0] || "standard"),
            ...(sourceTemplates.length > 1
                ? { compatible_templates: [...sourceTemplates] } : {}) };
}

function makeBtn(label, title, variant = "secondary", ariaLabel = "") {
    const btn = document.createElement("button");
    btn.textContent = label;
    btn.title = title || label;
    btn.style.cssText = chromeButtonCss();
    setButtonVariant(btn, variant);
    if (ariaLabel) btn.setAttribute("aria-label", ariaLabel);
    return btn;
}

const PROMPT_SECTION_CONTROL_COLUMNS = Object.freeze([
    "58px", "58px", "1fr", "auto", "auto", "auto", "auto",
]);

/** Shared metrics for the section `+`/`×` pair; see buildPromptSectionControlRow. */
export const SECTION_GLYPH_BUTTON_CSS =
    "min-width:22px;min-height:22px;padding:1px 5px;font-size:10px;font-weight:500;";

export function buildPromptSectionControlRow(cells, actions) {
    if (!Array.isArray(cells) || cells.length !== 6
            || !Array.isArray(actions) || actions.length !== 2) {
        throw new Error("Prompt section controls require six cells and two actions.");
    }
    const row = document.createElement("div");
    row.dataset.promptSectionControlRow = "1";
    row.dataset.promptSectionColumnCount = String(PROMPT_SECTION_CONTROL_COLUMNS.length);
    row.style.cssText = `display:grid;grid-template-columns:${PROMPT_SECTION_CONTROL_COLUMNS.join(" ")};gap:6px;align-items:center;`;
    const sectionActions = document.createElement("div");
    sectionActions.dataset.promptSectionActions = "1";
    sectionActions.style.cssText = "display:inline-flex;align-items:center;gap:4px;flex:0 0 auto;white-space:nowrap;";
    sectionActions.append(...actions);
    row.append(...cells, sectionActions);
    return row;
}

function smallInput({ value = "", placeholder = "", width = "", numeric = false } = {}) {
    const input = document.createElement("input");
    input.type = numeric ? "number" : "text";
    input.value = value;
    input.placeholder = placeholder;
    input.style.cssText = `${chromeInputCss({ padding: "4px 6px" })}; ${width ? `width:${width};` : "flex:1; min-width:40px;"}`;
    return input;
}

// Paragraph-size prompt textarea with a browser-local persisted height
// (one shared height per kind via prompts.panel*BoxHeight). Enter and focus
// loss commit, Shift+Enter inserts a newline — Esc is handled by the panel's focus-aware
// OVERLAY consumer, not here. Native `resize` is OFF — the corner grip
// disappears under the scrollbar once content overflows, so each box kind
// gets an explicit drag grip (makeHeightGrip) below it instead.
//
// `flex: 0 0 auto` is load-bearing for the Writing draft: it is a direct child
// of the panel's column-flex body, where shrink pressure can silently override
// the height the grip writes. The grid-hosted channel boxes also use this
// helper so every persisted height is restored through one seam.
function applyBoxHeight(host, element, heightKey, fallback) {
    const persisted = Number(host._settings?.prompts?.[heightKey]);
    const height = Number.isFinite(persisted) && persisted > 0 ? persisted : fallback;
    element.style.height = `${height}px`;
    element.dataset.sonderBoxKind = heightKey;
    return element;
}

function appendPromptContextHint(host, parent) {
    if (host._settings?.prompts?.contextMenuHintDismissed) return;
    const hint = document.createElement("div");
    hint.style.cssText = `display:flex;gap:6px;align-items:center;font-size:9px;color:${COLORS.textDim};`;
    const text = document.createElement("span");
    text.textContent = "Right-click a prompt box, or press Shift+F10, to insert Context and Writing aids.";
    const dismiss = makeBtn("Got it", "Dismiss this prompt-authoring hint");
    dismiss.addEventListener("mousedown", (event) => event.preventDefault());
    dismiss.addEventListener("click", () => {
        host._updateSettings({ prompts: { contextMenuHintDismissed: true } });
        hint.remove();
    });
    hint.append(text, dismiss);
    parent.appendChild(hint);
}

// Slim drag bar that resizes EVERY textarea of one height kind live and
// persists the shared height on release (prompts.panel*BoxHeight).
function makeHeightGrip(host, scopeEl, heightKey, { min = 28, max = 800, label = "Drag to resize" } = {}) {
    const grip = document.createElement("div");
    grip.title = label;
    grip.textContent = "•••";
    grip.style.cssText = `
        height: 7px; margin: -2px 0 0; border-radius: 3px; cursor: ns-resize;
        background: ${COLORS.promptBorder}; opacity: 0.35; flex: 0 0 auto;
        transition: opacity 0.12s; display:flex; align-items:center;
        justify-content:center; color:${COLORS.text}; font:9px/7px ${FONT.sans};
        letter-spacing:2px; overflow:hidden;
    `;
    grip.addEventListener("mouseenter", () => { grip.style.opacity = "0.8"; });
    grip.addEventListener("mouseleave", () => { grip.style.opacity = "0.35"; });
    grip.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        const boxes = [...scopeEl.querySelectorAll(`[data-sonder-box-kind="${heightKey}"]`)];
        if (!boxes.length) return;
        const startY = e.clientY;
        const startH = boxes[0].offsetHeight || min;
        let nextH = startH;
        grip.setPointerCapture(e.pointerId);
        const onMove = (ev) => {
            nextH = Math.max(min, Math.min(max, Math.round(startH + (ev.clientY - startY))));
            for (const box of boxes) box.style.height = `${nextH}px`;
        };
        const onUp = () => {
            grip.removeEventListener("pointermove", onMove);
            grip.removeEventListener("pointerup", onUp);
            grip.removeEventListener("pointercancel", onUp);
            if (nextH !== (host._settings?.prompts?.[heightKey] || 0)) {
                host._updateSettings({ prompts: { [heightKey]: nextH } });
            }
        };
        grip.addEventListener("pointermove", onMove);
        grip.addEventListener("pointerup", onUp);
        grip.addEventListener("pointercancel", onUp);
    });
    return grip;
}

/**
 * Does this draft hold anything the author would be sorry to lose?
 *
 * The predecessor tested `value?.document` — whether a document OBJECT
 * exists, not whether it holds anything. An emptied draft stores a document
 * of one empty text node, which is truthy, so an empty draft counted as
 * content everywhere this is asked. That made Reset ask permission to
 * discard nothing, made `loadWritingState` restore the emptiness instead of
 * rebuilding from sections (so the panel opened blank), and kept an empty
 * stash behind a "Restore draft" button that restored nothing. A blank
 * panel plus one Apply writes zero sections, and Apply is deliberately
 * unconfirmed — so this predicate can cost a whole lane.
 *
 * Chips count, and they live in THREE places: inline anchors in the
 * document, the editor's attachment registry, and `blockMeta[].attachments`
 * for anything attached with "Attach to this section/scene". A draft whose
 * only content is a section-scoped chip has no text and no registry entry,
 * so testing text alone would discard exactly the case this exists to save.
 */
export const writingDraftHasContent = (value) => {
    if (String(value?.draft || "").trim()) return true;
    const documentValue = normalizePromptDocument(value?.document);
    if (promptDocumentText(documentValue).trim()) return true;
    if (documentValue.nodes.some((node) => node.type === "attachment")) return true;
    if (normalizePromptAttachments(value?.attachments).length) return true;
    return (Array.isArray(value?.blockMeta) ? value.blockMeta : []).some(
        (meta) => normalizePromptAttachments(meta?.attachments).length);
};

function scopedAttachmentIdentity(attachment) {
    const semantic = structuredClone(attachment || {});
    // Split clones deliberately receive distinct authored object ids but share
    // an emission group. They coalesce only while every semantic field still
    // agrees; an edited clone remains present so compilation can surface the
    // conflicting emission instead of silently discarding either choice.
    delete semantic.attachment_id;
    return JSON.stringify(semantic);
}

export function coalesceScopedAttachments(values) {
    const result = [];
    const seen = new Set();
    for (const attachment of normalizePromptAttachments(values)) {
        const identity = scopedAttachmentIdentity(attachment);
        if (seen.has(identity)) continue;
        seen.add(identity); result.push(attachment);
    }
    return result;
}

/**
 * The sections the Writing draft stands for, as a pure projection.
 *
 * Extracted so the Reset-then-Apply parity claim is testable at all. Everything
 * the pivot rests on lives here — channel routing, anchor/scope attachment
 * reconciliation, prompt-id continuity and the frame layout — and while it sat
 * inside `mountPromptManagementPanel`'s closure the only reachable surface was
 * the three document functions, which round-trip cleanly on their own. A test
 * over those alone stays green against a build that relocates every section.
 *
 * ONE projection, two callers: Apply and the compile-candidate preview each
 * carried their own near-identical copy, differing only in how a block with no
 * source id is named. Preview showing sections Apply would not write is the
 * exact disagreement this surface exists to prevent.
 *
 * `blocks` are already split and reconciled: `reconcileWritingBlockMeta`
 * mutates draft state, so it stays with the caller and this takes its output.
 *
 * NOTE the frame layout is deliberately a recompaction from 0, not a copy of
 * the previous bounds — the draft owns lengths, not positions. A lane holding a
 * gap, or not starting at frame 0, does NOT survive a round trip, and that is
 * by construction rather than by defect.
 */
export function writingSectionsFromDraft({
    blocks = [], attachments = [], blockMeta = [], allocations = [],
    channelKeys = [], defaultKey = "", minLen = 1, newId = () => "",
} = {}) {
    // Refuse rather than quietly lose the author's work. With no channel keys
    // every channel comes back empty AND every inline anchor is dropped, which
    // is a silent deletion of chips dressed up as an empty draft. Both callers
    // pass `templateChannelKeys()`, which always yields at least one.
    if (!channelKeys.length) {
        throw new Error("writingSectionsFromDraft needs the template's channel keys");
    }
    const attachmentById = new Map(normalizePromptAttachments(attachments)
        .map((attachment) => [attachment.attachment_id, attachment]));
    let cursor = 0;
    const sections = blocks.map((blockDocument, index) => {
        const length = Math.max(minLen, allocations[index]?.length ?? minLen);
        const channelDocs = splitPromptDocumentChannels(
            blockDocument, channelKeys, { defaultKey });
        const anchorIds = new Set(Object.values(channelDocs)
            .flatMap((documentValue) => documentValue.nodes)
            .filter((node) => node.type === "attachment")
            .map((node) => node.attachment_id));
        const section = {
            prompt_id: blockMeta[index]?.source_prompt_id || newId(index),
            start_frame: cursor,
            end_frame: cursor + length,
            channels: Object.fromEntries(channelKeys.map((key) => [
                key, promptDocumentText(channelDocs[key]).trim(),
            ])),
            channel_docs: channelDocs,
            attachments: coalesceScopedAttachments([
                ...normalizePromptAttachments(blockMeta[index]?.attachments)
                    .filter((value) => !anchorIds.has(value.attachment_id)),
                ...[...anchorIds].map((id) => attachmentById.get(id)).filter(Boolean),
            ]),
            muted: blockMeta[index]?.muted === true,
            global_channel_exceptions: [...new Set(
                (blockMeta[index]?.global_channel_exceptions || []).map(String))],
        };
        cursor += length;
        return section;
    });
    // A merged block absorbs its neighbours' prompt ids, so a link pointing at
    // one of them must follow the survivor or it dangles. Preview previously
    // skipped this and could therefore show links Apply would rebind.
    const promptIdRemap = new Map();
    sections.forEach((section, index) => {
        for (const oldId of blockMeta[index]?.merged_source_prompt_ids || []) {
            if (oldId) promptIdRemap.set(oldId, section.prompt_id);
        }
    });
    for (const section of sections) {
        section.attachments = section.attachments.map((attachment) => {
            if (!["prompt_link", "prompt_link_scope"].includes(attachment.kind)) {
                return attachment;
            }
            const rebound = promptIdRemap.get(String(attachment.source?.prompt_id || ""));
            return rebound ? { ...attachment,
                source: { ...attachment.source, prompt_id: rebound } } : attachment;
        });
    }
    return { sections, totalFrames: cursor };
}

export function mountPromptManagementPanel(host) {
    const backdrop = document.createElement("div");
    backdrop.style.cssText = `
        position: fixed; inset: 0; z-index: 10000;
        background: rgba(7,10,14,0.70);
        display: flex; align-items: center; justify-content: center;
    `;
    const panel = document.createElement("div");
    panel.style.cssText = chromeOverlayPanelCss({
        width: "min(1320px, 96vw)", maxWidth: "1320px", maxHeight: "86vh", padding: "0",
    }) + "display:flex; flex-direction:column; overflow:hidden;";
    backdrop.appendChild(panel);

    let mounted = true;
    let identityPanelCleanup = () => {};
    let renderNow = () => {};
    let lastIdentityCandidateSignature = null;
    let lastDiagnosticsSignature = null;
    // Set while the Writing draft is mounted so a landed candidate can repaint
    // its contributions WITHOUT going through the panel's `render()`, which
    // rebuilds the draft element and takes the caret and the chip editor's
    // undo history with it.
    let refreshWritingDecorations = null;
    // Rebuilt by every render, called by every landed payload. The dormancy
    // notice derives from a compile, and the panel renders before the first one
    // arrives — leaving it to `render()` meant the notice appeared only when
    // something unrelated forced a rebuild, which is the class of bug
    // `durable_rules.md` calls "the consumer that regressed is rarely the one
    // the payload is named after".
    let dormancyRefreshers = [];
    const identityRefreshGate = createModalRefreshGate(() => renderNow());
    const render = () => identityRefreshGate.request();
    // Esc/blur-commit guard (audit F1): the OVERLAY consumer fires on the
    // window-CAPTURE root BEFORE textarea handlers, and closing removes
    // focused boxes whose blur would otherwise COMMIT a cancelled edit.
    const guard = { suppressBlurCommit: false, focusedBox: null };
    const attachmentLabelFor = (attachment) => resolveReferenceAttachmentIdentity(attachment, {
        scene: host.activeScene,
        references: host._references || [],
        semanticUnits: host._promptSemanticUnits || [],
    });
    const registerPromptBoxGuard = (area, revertValue) => {
        area.addEventListener("focus", () => {
            const structuredState = area.promptState;
            guard.focusedBox = { el: area,
                revert: structuredState || revertValue(),
                structured: !!structuredState };
        });
        area.addEventListener("blur", () => {
            if (guard.focusedBox?.el === area) guard.focusedBox = null;
        });
    };

    const close = () => {
        if (!mounted) return;
        mounted = false;
        identityRefreshGate.clear();
        // Closing mid-edit must never commit via the removal-triggered blur
        guard.suppressBlurCommit = true;
        identityPanelCleanup();
        for (const editor of backdrop.querySelectorAll("[data-sonder-prompt-box='1']")) {
            editor._sonderPromptContextMenuCleanup?.();
        }
        unregisterKeyboard();
        backdrop.remove();
        guard.suppressBlurCommit = false;
        if (host._promptPanelHandle === handle) host._promptPanelHandle = null;
    };

    const unregisterKeyboard = registerKeyboardConsumer({
        id: `sonder-prompt-panel-${Date.now().toString(36)}`,
        priority: KEY_PRIORITY.OVERLAY,
        keydown: (e) => {
            if (e.isComposing === true || e.keyCode === 229) return false;
            if (e.key !== "Escape") return false;
            const focused = guard.focusedBox;
            if (focused && document.activeElement === focused.el) {
                // First Esc: revert the box and drop focus WITHOUT committing;
                // panel stays open. A second Esc (no focused box) closes.
                if (focused.structured) focused.el.promptState = focused.revert;
                else focused.el.value = focused.revert;
                guard.suppressBlurCommit = true;
                focused.el.blur();
                guard.suppressBlurCommit = false;
                guard.focusedBox = null;
                return true;
            }
            close();
            return true;
        },
    });

    backdrop.addEventListener("mousedown", (e) => {
        if (e.target === backdrop) close();
    });

    // ── Writing-mode state (per project+scene; persisted browser-local) ──
    // An EMPTY persisted draft is treated as "no draft" (deep-merge settings
    // updates cannot delete map keys, so clearing stores an empty entry).
    const writingState = {
        key: "", draft: "", document: null, attachments: [],
        allocations: [], blockMeta: [], baseModifiedAt: "",
        // Pre-Reset snapshot. Reset is the ONLY escape offered when Apply is
        // staleness-blocked, so without this the one control a stuck author can
        // reach is also the one that destroys their unapplied work.
        stash: null,
        // Which channel this draft's unheadered text parses into, STAMPED when
        // the draft is built rather than resolved from the template at Apply
        // time. A draft authored before the template declared a draft channel
        // (or before the declaration existed at all) carries no stamp, and the
        // empty value keeps it parsing into channel 1 exactly as it did when
        // its author wrote it. Resolving live instead would silently relocate
        // that text the first time they pressed Apply after an update.
        defaultDraftChannel: "",
    };
    const writingDraftSnapshot = () => ({
        ts: Date.now(),
        draft: writingState.draft,
        document: structuredClone(writingState.document),
        attachments: structuredClone(writingState.attachments),
        baseModifiedAt: writingState.baseModifiedAt,
        blockMeta: structuredClone(writingState.blockMeta),
        allocations: writingState.allocations.map((a) => ({ length: a.length, dirty: !!a.dirty })),
        defaultDraftChannel: writingState.defaultDraftChannel,
    });
    // Where this draft's unheadered text actually goes. One accessor for the
    // hint and both split call sites, so what the panel promises and what Apply
    // does cannot disagree. A stamp naming a channel the template no longer
    // carries falls back the same way an undeclared one does.
    const writingDefaultChannelKey = () => {
        const keys = templateChannelKeys(host._channelTemplate());
        return keys.includes(writingState.defaultDraftChannel)
            ? writingState.defaultDraftChannel : (keys[0] || "");
    };
    // Shape-only block metadata. The load path additionally falls back to the
    // live sections for `muted` / `global_channel_exceptions` when a record
    // predates them; a stash was written by this session and needs no such
    // archaeology, so restoring must not silently re-derive from sections that
    // may have moved on since the snapshot was taken.
    const normalizeWritingBlockMeta = (raw) => (Array.isArray(raw) ? raw : []).map((value) => ({
        block_id: String(value?.block_id || "")
            || (globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`),
        source_prompt_id: String(value?.source_prompt_id || ""),
        merged_source_prompt_ids: Array.isArray(value?.merged_source_prompt_ids)
            ? value.merged_source_prompt_ids.map(String).filter(Boolean)
            : [String(value?.source_prompt_id || "")].filter(Boolean),
        node_ids: Array.isArray(value?.node_ids)
            ? value.node_ids.map(String).filter(Boolean) : [],
        muted: value?.muted === true,
        global_channel_exceptions: [...new Set(
            (Array.isArray(value?.global_channel_exceptions)
                ? value.global_channel_exceptions : []).map(String))],
        attachments: normalizePromptAttachments(value?.attachments),
    }));
    const documentNodeIds = (documentValue) => normalizePromptDocument(documentValue).nodes
        .map((node) => String(node.node_id || "")).filter(Boolean);
    const cloneSplitScopedAttachments = (values) => normalizePromptAttachments(values)
        .filter((value) => !["shot", "timestamp"].includes(value.kind))
        .map((value) => ({ ...structuredClone(value),
            attachment_id: globalThis.crypto?.randomUUID?.().replaceAll("-", "")
                || `${Date.now()}${Math.random()}`.replaceAll(".", "") }));
    const writingDraftKey = () => `${host._projectDirName?.() || ""}::${host.activeSceneId || ""}`;
    // Writing mode is one flat text, and `key:` headers are what let it carry
    // every channel rather than just one. `---` still splits sections; headers
    // split channels within a section. A whole MiniMax-format model output
    // therefore pastes in and arranges itself.
    const reconstructDraftFromSections = () => {
        const sections = host.activeScene?.prompt_sections || [];
        const template = host._channelTemplate();
        const keys = templateChannelKeys(template);
        const normalizedSections = sections.map((section) => {
            const channels = normalizeChannels(section.channels, section.prompt, keys);
            // Heal separator padding on the way in. Reset is the right place:
            // the draft is being rebuilt from sections anyway, the stash makes
            // it recoverable, and nothing durable changes until Apply.
            const channelDocs = Object.fromEntries(keys.map((key) => [
                key, healSeparatorPadding(normalizePromptDocument(
                    section.channel_docs?.[key], channels[key] || "")),
            ]));
            return { ...section, channel_docs: channelDocs };
        });
        writingState.document = joinWritingSectionDocuments(normalizedSections, keys);
        writingState.draft = promptDocumentText(writingState.document);
        writingState.attachments = normalizePromptAttachments(
            sections.flatMap((section) => section.attachments || []));
        writingState.allocations = sections.map((s) => ({
            length: Math.max(1, (s.end_frame || 0) - (s.start_frame || 0)),
            dirty: false,
        }));
        const blockDocuments = splitWritingPromptDocument(
            writingState.document, { keepEmpty: sections.length > 0 });
        writingState.blockMeta = sections.map((section, index) => {
            const anchors = new Set(Object.values(section.channel_docs || {})
                .flatMap((value) => normalizePromptDocument(value).nodes)
                .filter((node) => node.type === "attachment")
                .map((node) => node.attachment_id));
            return {
                block_id: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`,
                source_prompt_id: section.prompt_id || "",
                merged_source_prompt_ids: [section.prompt_id || ""].filter(Boolean),
                node_ids: documentNodeIds(blockDocuments[index]),
                muted: section.muted === true,
                global_channel_exceptions: [...new Set(
                    (section.global_channel_exceptions || []).map(String))],
                attachments: normalizePromptAttachments(section.attachments)
                    .filter((value) => !anchors.has(value.attachment_id)),
            };
        });
        writingState.baseModifiedAt = getProjectVersion(host._projectDirName?.() || "");
        writingState.defaultDraftChannel = defaultDraftChannel(template);
    };
    const loadWritingState = () => {
        const key = writingDraftKey();
        if (writingState.key === key) return;
        writingState.key = key;
        const saved = host._settings?.prompts?.writingDraftByProjectScene?.[key];
        // The one predicate, not a copy of it. This site had its own inlined
        // version of the same expression and is how an emptied draft came to be
        // restored as though it were real work.
        if (saved && writingDraftHasContent(saved)) {
            const currentSectionsById = new Map(
                (host.activeScene?.prompt_sections || []).map((section) => [
                    String(section.prompt_id || ""), section,
                ]));
            writingState.document = normalizePromptDocument(
                saved.document, String(saved.draft || ""));
            writingState.draft = promptDocumentText(writingState.document);
            writingState.attachments = normalizePromptAttachments(saved.attachments);
            writingState.allocations = (saved.allocations || []).map((a) => ({
                length: Math.max(0, parseInt(a?.length, 10) || 0),
                dirty: !!a?.dirty,
            }));
            writingState.blockMeta = (saved.blockMeta || []).map((value) => ({
                block_id: String(value?.block_id || "")
                    || (globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`),
                source_prompt_id: String(value?.source_prompt_id || ""),
                merged_source_prompt_ids: Array.isArray(value?.merged_source_prompt_ids)
                    ? value.merged_source_prompt_ids.map(String).filter(Boolean)
                    : [String(value?.source_prompt_id || "")].filter(Boolean),
                node_ids: Array.isArray(value?.node_ids)
                    ? value.node_ids.map(String).filter(Boolean) : [],
                muted: Object.prototype.hasOwnProperty.call(value || {}, "muted")
                    ? value?.muted === true
                    : currentSectionsById.get(String(value?.source_prompt_id || ""))?.muted === true,
                global_channel_exceptions: [...new Set(
                    (Array.isArray(value?.global_channel_exceptions)
                        ? value.global_channel_exceptions
                        : currentSectionsById.get(String(
                            value?.source_prompt_id || ""))?.global_channel_exceptions || [])
                        .map(String))],
                attachments: normalizePromptAttachments(value?.attachments),
            }));
            writingState.baseModifiedAt = String(saved.baseModifiedAt || "");
            // No stamp means a pre-declaration draft: keep channel 1.
            writingState.defaultDraftChannel = String(saved.defaultDraftChannel || "");
            writingState.stash = saved.stash && writingDraftHasContent(saved.stash)
                ? structuredClone(saved.stash) : null;
        } else {
            writingState.stash = null;
            reconstructDraftFromSections();
        }
    };
    const saveWritingState = () => {
        if (!writingState.key) return;
        if (writingState.draft.length > WRITING_DRAFT_TEXT_CAP) {
            notifyWarning("Writing draft exceeds the 20k browser-draft guidance.", { source: "prompt-writing-draft-cap" });
        }
        // Write ONE key. `mergeIntoSettings` deep-merges plain objects, so a
        // single-key patch lands beside the other drafts instead of replacing
        // the map — spreading `host._settings` here reverted every draft that
        // changed after that snapshot was taken, which is a cross-window loss
        // now that the draft holds authored prose rather than a projection.
        // Cleared records cannot accumulate, because normalization drops them.
        host._updateSettings({ prompts: { writingDraftByProjectScene: {
            [writingState.key]: {
                ...writingDraftSnapshot(),
                stash: writingState.stash ? structuredClone(writingState.stash) : null,
            },
        } } });
    };
    const clearWritingState = ({ keepAsStash = null } = {}) => {
        const applied = keepAsStash ? structuredClone(keepAsStash) : null;
        // Single-key patch for the same reason as `saveWritingState`. Every
        // field is written, so the deep merge leaves nothing stale behind, and
        // normalization then drops the emptied record entirely.
        // The stash becomes the draft that was just applied. Apply is the only
        // irreversible act on this surface — it is what empties the draft — so
        // it should leave a way back rather than dropping the previous stash
        // and offering none. A snapshot from before some earlier Reset would
        // be stale, so it is replaced, not kept.
        host._updateSettings({ prompts: { writingDraftByProjectScene: {
            [writingState.key]: { ts: Date.now(), draft: "", document: null,
                attachments: [], allocations: [], blockMeta: [], baseModifiedAt: "",
                stash: applied, defaultDraftChannel: "" },
        } } });
        writingState.stash = applied;
        writingState.key = ""; // force reload (reconstruct) on next render
    };
    const reconcileWritingBlockMeta = (blockDocuments) => {
        const old = writingState.blockMeta.map((value, index) => ({
            ...value, index, ids: new Set(value.node_ids || []),
        }));
        const claimed = new Set();
        const next = [];
        for (let blockIndex = 0; blockIndex < blockDocuments.length; blockIndex++) {
            const ids = new Set(documentNodeIds(blockDocuments[blockIndex]));
            const candidates = old.map((meta) => ({ meta, score: [...ids]
                .filter((id) => meta.ids.has(id)).length }))
                .filter((value) => value.score > 0)
                .sort((left, right) => right.score - left.score
                    || left.meta.index - right.meta.index);
            let meta;
            if (candidates.length && !claimed.has(candidates[0].meta.index)) {
                const matched = candidates.map((value) => value.meta)
                    .filter((value) => !claimed.has(value.index));
                const primary = matched.sort((left, right) => left.index - right.index)[0];
                matched.forEach((value) => claimed.add(value.index));
                meta = {
                    ...primary,
                    global_channel_exceptions: [...new Set(matched.flatMap(
                        (value) => value.global_channel_exceptions || []))],
                    attachments: coalesceScopedAttachments(
                        matched.flatMap((value) => value.attachments || [])),
                    merged_source_prompt_ids: [...new Set(matched.flatMap((value) =>
                        value.merged_source_prompt_ids?.length
                            ? value.merged_source_prompt_ids
                            : [value.source_prompt_id]).filter(Boolean))],
                    _writing_source_indices: matched.map((value) => value.index),
                };
            } else if (candidates.length) {
                // The same prior block now contributes to more than one block:
                // this is a split. The left claimant retains its identity;
                // later blocks get new identities and cloned applicable scope
                // chips sharing the original emission group.
                const source = candidates[0].meta;
                const clones = cloneSplitScopedAttachments(source.attachments || []);
                writingState.attachments = normalizePromptAttachments([
                    ...writingState.attachments, ...clones,
                ]);
                meta = {
                    block_id: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`,
                    source_prompt_id: `draft:${globalThis.crypto?.randomUUID?.()
                        || `${Date.now()}-${Math.random()}`}`,
                    merged_source_prompt_ids: [],
                    muted: source.muted === true,
                    global_channel_exceptions: [...(source.global_channel_exceptions || [])],
                    attachments: clones,
                    _writing_source_indices: [source.index],
                };
            } else {
                // Legacy drafts predate node-id metadata. A positional match is
                // safe only while that old slot is still unclaimed.
                const positional = old[blockIndex];
                if (positional && !positional.ids.size && !claimed.has(positional.index)) {
                    claimed.add(positional.index); meta = {
                        ...positional, _writing_source_indices: [positional.index],
                    };
                } else {
                    // Text fragments to the right of a newly typed separator
                    // deliberately receive new node ids. When the block count
                    // grew, the preceding block is therefore the only stable
                    // source identity available for a text-only split.
                    const priorIndices = next.at(-1)?._writing_source_indices || [];
                    const splitSource = blockDocuments.length > old.length && priorIndices.length
                        ? old[priorIndices.at(-1)] : null;
                    const clones = splitSource
                        ? cloneSplitScopedAttachments(splitSource.attachments || []) : [];
                    if (clones.length) writingState.attachments = normalizePromptAttachments([
                        ...writingState.attachments, ...clones,
                    ]);
                    meta = {
                        block_id: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`,
                        source_prompt_id: `draft:${globalThis.crypto?.randomUUID?.()
                            || `${Date.now()}-${Math.random()}`}`,
                        merged_source_prompt_ids: [],
                        muted: splitSource?.muted === true,
                        global_channel_exceptions: [...(splitSource?.global_channel_exceptions || [])],
                        attachments: clones,
                        _writing_source_indices: splitSource ? [splitSource.index] : [],
                    };
                }
            }
            delete meta.index; delete meta.ids;
            if (!meta.source_prompt_id) {
                meta.source_prompt_id = `draft:${globalThis.crypto?.randomUUID?.()
                    || `${Date.now()}-${Math.random()}`}`;
            }
            meta.node_ids = [...ids];
            next.push(meta);
        }
        next.forEach((value) => { delete value._writing_source_indices; });
        writingState.blockMeta = next;
        return blockDocuments;
    };

    // ── Header ───────────────────────────────────────────────────────
    const header = document.createElement("div");
    header.style.cssText = `
        display:flex; align-items:center; justify-content:space-between;
        padding: 12px 18px; border-bottom: 1px solid ${COLORS.promptBorder};
        font-family:${FONT.sans}; flex:0 0 auto;
    `;
    const title = document.createElement("div");
    title.style.cssText = `font-size:13px; font-weight:600; color:${COLORS.text};`;
    title.textContent = "Prompt Management";
    const closeBtn = makeBtn("Close", "Close (Esc)", "subtle");
    closeBtn.addEventListener("click", close);
    header.append(title, closeBtn);
    panel.appendChild(header);

    const diagnostics = document.createElement("div");
    diagnostics.dataset.sonderPromptDiagnostics = "1";
    diagnostics.style.cssText = `padding:7px 18px;border-bottom:1px solid ${COLORS.trackBorder};`
        + `background:${COLORS.panelMuted};font:10px/1.35 ${FONT.sans};color:${COLORS.textDim};`
        + "display:flex;flex-direction:column;gap:3px;height:72px;overflow-y:auto;box-sizing:border-box;flex:0 0 auto;";
    panel.appendChild(diagnostics);

    const body = document.createElement("div");
    body.style.cssText = `
        flex:1; min-height:0; overflow-y:auto; padding: 12px 18px; display:flex;
        flex-direction:column; gap: 14px; font-family:${FONT.sans};
    `;
    panel.appendChild(body);

    const currentCandidatePayload = () => {
        const payload = host._promptContextCandidateCache;
        return payload?._candidate_scene_id === host.activeSceneId ? payload : null;
    };
    /**
     * The payload that answers for ONE section's projections.
     *
     * A section outside the render window compiles not at all, so its chips
     * projected nothing and the surface read as "nothing is staged". The
     * scene-wide payload fills those rows in. Never use this for an EDITING
     * surface — `configurePromptAttachment` renders live routing state and a
     * standalone Time's resolved value from the same fields, and would show a
     * compile state the render will not produce with none of the disclosure a
     * projection row carries. Those keep `currentCandidatePayload()`.
     */
    const candidateForSection = (promptId) => {
        const payload = host._promptCandidateForSection?.(String(promptId || ""));
        return payload?._candidate_scene_id === host.activeSceneId ? payload : null;
    };
    /** Why a section contributes nothing here, or null when it contributes. */
    const sectionDormancy = (promptId) =>
        host._promptSectionDormancy?.(String(promptId || "")) || null;
    /**
     * One dim line saying why, and what its numbering means.
     *
     * Once per section, never per pill: under H3 a section can carry six
     * channels and repeating the disclosure on each would bury the rows it is
     * disclosing about.
     */
    const dormancyNotice = (resolve, { live = true } = {}) => {
        const line = document.createElement("div");
        line.style.cssText = `font-size:9px;color:${COLORS.textDim};`
            + "line-height:1.4;opacity:.9;";
        let lastDormancySignature = null;
        const apply = () => {
            const dormancy = resolve();
            const signature = JSON.stringify(dormancy || null);
            if (signature === lastDormancySignature) return false;
            lastDormancySignature = signature;
            line.dataset.sonderPromptDormancy = String(dormancy?.state || "");
            line.textContent = dormancy?.message || "";
            // Collapsed rather than removed: the row is rebuilt by a compile
            // that lands while the author is typing, and removing an element
            // from a card mid-edit reflows everything under the pointer.
            line.style.display = dormancy ? "" : "none";
            return true;
        };
        apply();
        // `live: false` for a caller whose resolver closes over a fixed value --
        // repainting it can never change anything, and registering it anyway is
        // how a list reset only by a full render grows for a whole session.
        if (live) dormancyRefreshers.push({ line, apply });
        return line;
    };
    const currentPromptProfile = (profileId = "") => resolvedPromptProfile({
        profileId: String(profileId || host.activeScene?.prompt_context_profile_id
            || host._channelTemplate?.()?.default_context_profile || "generic@1"),
        candidate: currentCandidatePayload(),
        catalog: host._promptContextCatalog,
        customProfiles: host._promptContextProfiles,
    });
    const promptPlacementPhases = () =>
        host._promptContextCatalog?.placement_phases || [];
    const currentManagedSpeakerSubjectIds = () =>
        currentCandidatePayload()?.managed_speaker_subject_ids || [];
    const refreshWritingCompiled = (payload = currentCandidatePayload()) => {
        const output = panel.querySelector("[data-sonder-writing-compiled]");
        if (!output) return;
        output.textContent = "";
        const status = document.createElement("div");
        status.style.cssText = `font-size:10px;color:${COLORS.textDim};`;
        if (!payload) {
            status.textContent = "Compiling the current Writing draft…";
            output.appendChild(status);
            return;
        }
        if (promptCandidateVisuallyStale(payload)) {
            status.textContent = "Updating compiled output from the current Writing draft…";
            output.appendChild(status);
        } else {
            status.textContent = "Read-only compiler output for the current render window.";
            output.appendChild(status);
        }
        const compiled = writingCompiledPayload(payload);
        const prompt = document.createElement("pre");
        prompt.dataset.sonderWritingCompiledPrompt = "1";
        prompt.style.cssText = `margin:0;padding:9px;border:1px solid ${COLORS.promptBorder};border-radius:5px;`
            + `background:${COLORS.panelMuted};color:${COLORS.text};font:11px/1.45 ${FONT.mono || "monospace"};`
            + "white-space:pre-wrap;overflow-wrap:anywhere;min-height:90px;";
        prompt.textContent = compiled.prompt;
        output.appendChild(prompt);
        const channels = compiled.channels;
        const orderedKeys = [...new Set([
            ...templateChannelKeys(host._channelTemplate()), ...Object.keys(channels),
        ])].filter((key) => Object.prototype.hasOwnProperty.call(channels, key));
        if (orderedKeys.length) {
            const details = document.createElement("details");
            const summary = document.createElement("summary");
            summary.textContent = `Compiled channels (${orderedKeys.length})`;
            summary.style.cssText = `cursor:pointer;font-size:10px;color:${COLORS.textMuted};`;
            details.appendChild(summary);
            for (const key of orderedKeys) {
                const row = document.createElement("pre");
                row.style.cssText = `margin:5px 0 0;padding:6px;border-left:2px solid ${COLORS.promptBorder};`
                    + `color:${COLORS.text};font:10px/1.4 ${FONT.mono || "monospace"};white-space:pre-wrap;`;
                row.textContent = `${key}:\n${String(channels[key] || "")}`;
                details.appendChild(row);
            }
            output.appendChild(details);
        }
    };
    const renderDiagnostics = (payload = currentCandidatePayload()) => {
        const state = buildPromptContextDiagnostics(payload);
        const diagnosticsSignature = JSON.stringify(state);
        if (diagnosticsSignature !== lastDiagnosticsSignature) {
            lastDiagnosticsSignature = diagnosticsSignature;
            diagnostics.style.opacity = state.stale ? "0.62" : "1";
            diagnostics.dataset.sonderPromptDiagnosticsStale = state.stale ? "1" : "0";
            diagnostics.textContent = "";
            const windowLine = document.createElement("div");
            windowLine.textContent = state.windowLabel;
            windowLine.style.color = COLORS.textMuted;
            diagnostics.appendChild(windowLine);
            for (const row of state.general) {
                const line = document.createElement("div");
                const text = document.createElement("span");
                text.textContent = `${row.tier === "error" ? "Error" : "Warning"} ${row.code}: ${row.message}`;
                line.appendChild(text);
                line.style.color = row.tier === "error" ? COLORS.dangerText : COLORS.warningText;
                if (row.tier === "warning"
                        && ["overall_soundscape", "non_diegetic_music"].includes(row.channel_key)) {
                    const addNA = makeBtn("Add N/A", `Write N/A into the global ${row.channel_key} channel`);
                    addNA.style.marginLeft = "6px";
                    addNA.addEventListener("mousedown", (event) => event.preventDefault());
                    addNA.addEventListener("click", () => {
                        const selector = `[data-sonder-prompt-global-channel="${row.channel_key}"]`;
                        const input = panel.querySelector(selector);
                        if (!input) return;
                        input.focus();
                        input.insertText?.("N/A");
                        input.blur();
                    });
                    line.appendChild(addNA);
                }
                diagnostics.appendChild(line);
            }
            if (state.attachmentDiagnosticCount) {
                const line = document.createElement("div");
                line.textContent = `${state.attachmentDiagnosticCount} chip diagnostic${state.attachmentDiagnosticCount === 1 ? " is" : "s are"} marked below.`;
                line.style.color = state.errorCount ? COLORS.dangerText : COLORS.warningText;
                diagnostics.appendChild(line);
            } else if (!state.general.length && payload) {
                const clean = document.createElement("div");
                clean.textContent = "No compile diagnostics in this render window.";
                diagnostics.appendChild(clean);
            }
        }

        // Body renders create new queue badges even when diagnostic state is
        // byte-identical. Keep this consumer outside the diagnostic-line DOM
        // guard so a same-state full render still initializes the new nodes.
        for (const badge of panel.querySelectorAll("[data-sonder-queue-advisory-count]")) {
            badge.textContent = state.warningCount
                ? `${state.warningCount} advisor${state.warningCount === 1 ? "y" : "ies"}` : "";
            badge.style.display = state.warningCount ? "inline" : "none";
            badge.title = state.warningCount
                ? "The current effective render window has non-blocking prompt advisories." : "";
        }

        for (const projections of panel.querySelectorAll(
            "[data-sonder-prompt-channel-projections]")) {
            // No payload: each host resolves the one that answers for its own
            // section. Forwarding this one repainted a dormant section from the
            // windowed compile, which holds no rows for it -- blanking the
            // scene-derived pills under a line still saying they were there.
            projections._sonderRefreshProjection?.();
        }

        for (const chip of panel.querySelectorAll("[data-attachment-id]")) {
            if (!Object.prototype.hasOwnProperty.call(chip.dataset, "sonderDiagnosticBaseTitle")) {
                chip.dataset.sonderDiagnosticBaseTitle = chip.title || "Dynamic prompt context";
            }
            chip.title = chip.dataset.sonderDiagnosticBaseTitle;
            chip.style.boxShadow = "";
            chip.removeAttribute("aria-invalid");
            delete chip.dataset.sonderDiagnosticTier;
            const rows = state.byAttachment[chip.dataset.attachmentId] || [];
            if (!rows.length) continue;
            const hasError = rows.some((row) => row.tier === "error");
            chip.dataset.sonderDiagnosticTier = hasError ? "error" : "warning";
            chip.style.boxShadow = `inset 0 0 0 1px ${hasError ? COLORS.dangerText : COLORS.warningText}`;
            if (hasError) chip.setAttribute("aria-invalid", "true");
            chip.title = `${chip.title}\n\n${promptContextDiagnosticTitle(rows)}`;
        }
        refreshWritingCompiled(payload);
    };

    const sectionTitle = (text) => {
        const el = document.createElement("div");
        el.style.cssText = `font-size:11px; font-weight:600; color:${COLORS.textMuted}; text-transform:uppercase; letter-spacing:0.4px;`;
        el.textContent = text;
        return el;
    };

    // ── Writing mode view: narrative draft + --- splits + allocation strip ──
    const renderWritingView = (bodyEl, sectionsLocked) => {
        loadWritingState();
        const scene = host.activeScene;
        const totalFrames = scene?.duration_frames || host.totalFrames || 0;
        const step = host._resolveFrameConstraintForTemplate?.(host._templateId)?.step;
        const minLen = Math.max(1, parseInt(step, 10) || 1);
        const globalLocked = host._isGlobalPromptTrackLocked();
        const applyBlocked = sectionsLocked || globalLocked;

        bodyEl.appendChild(sectionTitle("Writing Mode — narrative draft"));
        const hint = document.createElement("div");
        hint.style.cssText = `font-size:10px; color:${COLORS.textDim};`;
        hint.textContent = `Write freely; a line containing only ${WRITING_BREAK} splits sections, and a line like "${templateChannelKeys(host._channelTemplate())[0]}:" starts that channel. Unlabelled text goes to ${writingDefaultChannelKey() || "the first channel"}. Apply replaces the lane's sections (undoable).`;
        bodyEl.appendChild(hint);

        const writingBlockDocuments = () => reconcileWritingBlockMeta(
            splitWritingPromptDocument(writingState.document, {
                keepEmpty: writingState.blockMeta.length > 0,
            }));
        // Which prompt id each block compiles under. NOT re-derived: a block
        // keeps its source section's id when it has one and takes `newId`
        // otherwise, and `newId` is a CALLER parameter — preview passes
        // `preview:N`, Apply passes a fresh uuid — so a second copy of that
        // expression would be wrong for Apply by construction. Stamped by the
        // one projection both share. An index with no entry falls back to
        // `selected`, i.e. to the behaviour before any of this.
        let writingBlockPromptIds = [];
        const buildWritingCandidatePatch = (blockDocuments) => {
            const { sections, totalFrames: cursor } = writingSectionsFromDraft({
                blocks: blockDocuments,
                attachments: writingState.attachments,
                blockMeta: writingState.blockMeta,
                allocations: writingState.allocations,
                channelKeys: templateChannelKeys(host._channelTemplate()),
                defaultKey: writingDefaultChannelKey(),
                minLen,
                newId: (index) => `preview:${index}`,
            });
            writingBlockPromptIds = sections.map((value) => value.prompt_id);
            return { prompt_sections: sections,
                duration_frames: Math.max(totalFrames, cursor) };
        };
        /**
         * The prompt id of one block, or "" while the stamp is out of date.
         *
         * `writingBlockPromptIds` is stamped from `writingState.document` on the
         * strip's 300ms debounce, while the decorations index it against ranges
         * derived from the LIVE model on every landed compile. Between typing a
         * `---` and the strip catching up the two disagree by one, and block n
         * would read block n-1's id -- a dormancy line and a payload belonging
         * to a different section. A count mismatch is the cheap, exact signal
         * that the stamp is stale, and refusing falls back to `selected`, which
         * is the behaviour before any of this.
         */
        const writingBlockPromptId = (blockIndex, blockCount) =>
            (writingBlockPromptIds.length === blockCount
                ? writingBlockPromptIds[blockIndex] : "") || "";
        const writingBlockDormancy = (blockIndex, blockCount) =>
            sectionDormancy(writingBlockPromptId(blockIndex, blockCount));
        const previewWritingBlocks = (blockDocuments) => {
            host._previewPromptContextCandidate?.(
                buildWritingCandidatePatch(blockDocuments));
        };

        // This is a browser-local presentation preference. Both tabs read the
        // same canonical draft; Compiled never becomes an authoring authority.
        const writingView = host._settings?.prompts?.writingView === "compiled"
            ? "compiled" : "source";
        const viewRow = document.createElement("div");
        viewRow.style.cssText = "display:flex;gap:4px;align-items:center;";
        for (const [value, label] of [["source", "Source"], ["compiled", "Compiled"]]) {
            const button = makeBtn(label, value === "source"
                ? "Edit the reversible channel-heading and directive projection"
                : "Inspect read-only output from the canonical compiler",
            writingView === value ? "primary" : "subtle");
            button.addEventListener("click", () => {
                if (writingView === value) return;
                if (writingView === "source") saveWritingState();
                host._updateSettings({ prompts: { writingView: value } });
                render();
            });
            viewRow.appendChild(button);
        }
        bodyEl.appendChild(viewRow);
        if (writingView === "compiled") {
            const compiled = document.createElement("div");
            compiled.dataset.sonderWritingCompiled = "1";
            compiled.style.cssText = "display:flex;flex-direction:column;gap:7px;";
            bodyEl.appendChild(compiled);
            const blockDocuments = writingBlockDocuments();
            const blocks = blockDocuments.map(promptDocumentText);
            if (blocks.length !== writingState.allocations.length) {
                writingState.allocations = allocateWritingBlocks(
                    blocks, totalFrames, minLen, writingState.allocations);
            }
            previewWritingBlocks(blockDocuments);
            refreshWritingCompiled();
            return;
        }

        let draftTimer = null;
        let draftArea = null;
        refreshWritingDecorations = null;
        const draftAttachmentContext = (nodeId = "") => {
            const blockDocuments = reconcileWritingBlockMeta(
                splitWritingPromptDocument(writingState.document, {
                    keepEmpty: writingState.blockMeta.length > 0,
                }));
            const selectedNodeId = nodeId || draftArea?.promptSelection?.node_id || "";
            let selectedIndex = blockDocuments.findIndex((documentValue) =>
                documentNodeIds(documentValue).includes(selectedNodeId));
            if (selectedIndex < 0) selectedIndex = 0;
            let cursor = 0;
            const promptSections = blockDocuments.map((documentValue, index) => {
                const length = Math.max(
                    minLen, writingState.allocations[index]?.length ?? minLen);
                const value = {
                    prompt_id: writingState.blockMeta[index]?.source_prompt_id,
                    start_frame: cursor,
                    end_frame: cursor + length,
                    prompt: promptDocumentText(documentValue),
                };
                cursor += length;
                return value;
            });
            const selected = promptSections[selectedIndex] || {
                start_frame: 0, end_frame: minLen,
            };
            return {
                scene: { ...(scene || {}), prompt_sections: promptSections,
                    _context_channel_keys: templateChannelKeys(host._channelTemplate()),
                    _context_consumer_start: selected.start_frame,
                    _context_consumer_end: selected.end_frame,
                    _context_reference_frame_threshold: host._referenceFrameThreshold || 0 },
                blockDocuments,
                selectedIndex,
            };
        };
        const configureDraftAttachment = (attachment, nodeId = "") => {
            const context = draftAttachmentContext(nodeId);
            return configurePromptAttachment(attachment, {
                scene: context.scene,
                references: host._references || [],
                semanticUnits: host._promptSemanticUnits || [],
                profileId: scene?.prompt_context_profile_id
                    || host._channelTemplate().default_context_profile || "generic@1",
                scope: "section",
                anchoredChannels: [context.scene?._context_channel_keys?.[0]].filter(Boolean),
                profile: currentPromptProfile(),
                placementPhases: promptPlacementPhases(),
                managedSpeakerSubjectIds: currentManagedSpeakerSubjectIds(),
                ordinalManifest: currentCandidatePayload()?.ordinal_manifest || {},
                candidate: currentCandidatePayload(),
            });
        };
        // Copy is offered only for the Reference contribution kinds the server
        // can segment. Live spellings stay handles; anything with no live
        // spelling crosses as disclosed plain text — see `copy_capability_plan`.
        /**
         * Put the caret at a draft anchor, given as `{ index, offset }`.
         *
         * `offset: null` means the end of that node, which is what both callers
         * computed by hand. A non-null offset is what a `---` sharing a node
         * with the text before it needs: the block's last position is INSIDE
         * that node, and the node's end is already past the break.
         */
        const placeWritingCaret = ({ index, offset = null } = {}) => {
            const node = normalizePromptDocument(writingState.document)
                .nodes[index];
            if (!node) return null;
            const at = node.type === "text"
                ? (offset === null ? node.text.length
                    : Math.max(0, Math.min(node.text.length, offset)))
                : 1;
            const position = { node_id: node.node_id, offset: at };
            return draftArea.restorePromptSelection({
                start: position, end: position }) ? node : null;
        };
        /**
         * Put a contribution's LIVE SPELLING on the clipboard.
         *
         * The successor to *Convert to prose*, and deliberately smaller. Convert
         * had to write prose AND disable the chip atomically, which meant caret
         * placement, chip minting, heading creation and a group-wide disable —
         * every one of which was a way to get it wrong. Copy hands the author
         * text and lets them decide where it goes; turning the chip off is a
         * separate, visible action beside it.
         *
         * It prefers handles. A derived segment with no live spelling crosses
         * as rendered text with an explicit ordinal/static disclosure, and the
         * paste destination independently warns if an ordinal enters durable
         * authored prose. The plan stays inside compile because the context it
         * needs is injected there.
         */
        const copyContribution = async (attachmentId, capabilityId,
            { sceneWide = false } = {}) => {
            const plan = await host._promptCopyPlan?.(
                attachmentId, capabilityId, buildWritingCandidatePatch(
                    writingBlockDocuments()), { sceneWide });
            if (!plan) {
                notifyWarning("Could not work out what this contribution says.",
                    { source: "prompt-copy" });
                return false;
            }
            if (plan.refused) {
                notifyWarning(plan.refused, { source: "prompt-copy" });
                return false;
            }
            const units = host._promptSemanticUnits || [];
            const members = (host._references || []).flatMap((entity) =>
                (entity?.members || []));
            const handleFor = (part) => {
                const id = String(part?.id || "");
                if (part?.source === "member") {
                    return String((members.find((value) =>
                        String(value?.member_id || "") === id) || {}).handle || "");
                }
                return String((units.find((value) =>
                    String(value?.semantic_unit_id || "") === id) || {}).handle || "");
            };
            const frozenStatic = [...(plan.frozen_static || [])].filter(Boolean);
            const frozenOrdinal = [...(plan.frozen_ordinal || [])].filter(Boolean);
            const lines = [];
            for (const line of plan.lines || []) {
                let text = "";
                for (const part of line.parts || []) {
                    if (part.kind === "text") { text += part.text; continue; }
                    if (part.kind === "handle") {
                        const handle = handleFor(part);
                        if (!handle) {
                            text += String(part.rendered || "");
                            if (part.rendered) frozenOrdinal.push(part.rendered);
                            continue;
                        }
                        text += `@${handle}`;
                        continue;
                    }
                    if (part.kind === "sources") {
                        const names = (part.member_ids || []).map((id) =>
                            String((members.find((value) =>
                                String(value?.member_id || "") === String(id))
                                || {}).handle || ""));
                        // A Subject with no VISUAL source still gets a sources
                        // segment, empty. Falling through wrote " from  and "
                        // into the clipboard, and `some(v => !v)` cannot catch
                        // it because an empty list satisfies every predicate.
                        if (!names.length) continue;
                        if (names.some((value) => !value)) {
                            text += String(part.rendered || "");
                            if (part.rendered) frozenOrdinal.push(part.rendered);
                            continue;
                        }
                        // Mirrors the server exactly: two sources join with
                        // " and ", three or more take an Oxford comma before
                        // the last. A plain `", "` join for 3+ produced text
                        // the chip does not emit, which is the one thing a
                        // clipboard copy must never do.
                        const spelled = names.map((h) => `@${h}`);
                        text += " from " + (spelled.length > 2
                            ? `${spelled.slice(0, -1).join(", ")}, and ${spelled.at(-1)}`
                            : spelled.join(" and "));
                        continue;
                    }
                    // A frozen part has no live spelling but is not an ordinal
                    // either — a retention marker, or a task-type prefix drawn
                    // from staged roles. It copies as written and is disclosed.
                    text += String(part.rendered || part.text || "");
                }
                if (text.trim()) lines.push(text);
            }
            if (!lines.length) {
                notifyWarning("This contribution resolves to nothing right now,"
                    + " so there is nothing to copy.", { source: "prompt-copy" });
                return false;
            }
            const text = lines.join("\n");
            try {
                await navigator.clipboard.writeText(text);
            } catch (_error) {
                notifyWarning("The browser refused clipboard access, so nothing"
                    + " was copied.", { source: "prompt-copy" });
                return false;
            }
            const staticNames = [...new Set(frozenStatic)];
            const ordinalNames = [...new Set(frozenOrdinal)];
            if (staticNames.length || ordinalNames.length) {
                const disclosures = [];
                if (staticNames.length) disclosures.push(
                    `${staticNames.join(", ")} came across as plain text and will stop following staged intent`);
                if (ordinalNames.length) disclosures.push(
                    `${ordinalNames.join(", ")} came across as a number staging can change`);
                notifyInfo(`Copied. ${disclosures.join("; ")}.`,
                    { source: "prompt-copy" });
            } else {
                notifySuccess("Copied with live handles.",
                    { source: "prompt-copy" });
            }
            return true;
        };
        /** Turn one capability on or off, wherever this draft holds the chip. */
        const setContributionEnabled = (attachmentId, capabilityId, enabled) => {
            const apply = (value) => setPromptAttachmentCapabilityEnabled(
                value, { capability_id: capabilityId }, enabled);
            let touched = false;
            // BOTH homes. An inline-anchored chip lives in the editor registry;
            // one attached to the section lives in `blockMeta[i].attachments`,
            // and `writingSectionsFromDraft` reads the unanchored ones there.
            for (const value of [...writingState.attachments]) {
                if (value.attachment_id !== attachmentId) continue;
                draftArea.replaceAttachment(apply(value));
                touched = true;
            }
            for (const meta of writingState.blockMeta || []) {
                (meta.attachments || []).forEach((value, index) => {
                    if (value.attachment_id !== attachmentId) return;
                    meta.attachments[index] = apply(value);
                    touched = true;
                });
            }
            if (!touched) {
                notifyWarning("That Reference is no longer staged here.",
                    { source: "prompt-contribution" });
                return;
            }
            writingState.draft = draftArea.value;
            writingState.document = draftArea.promptDocument;
            writingState.attachments = draftArea.promptAttachments;
            saveWritingState();
            render();
        };
        const writingDecorations = (model) => {
            if (!currentCandidatePayload()) return [];
            const keys = templateChannelKeys(host._channelTemplate());
            const regions = channelRegionsByNode(model, keys,
                { defaultKey: writingDefaultChannelKey() });
            // ALWAYS keepEmpty: `channelRegionsByNode` counts every `---`, so a
            // filtered split would shift every index after a dropped empty
            // block and hand a region the NEXT block's chips — a wrong reading
            // rather than a missing one.
            const blocks = splitWritingPromptDocument(model, { keepEmpty: true });
            const ranges = writingBlockNodeRanges(model);
            const out = [];
            const poolFor = (blockIndex) => {
                const blockDocument = blocks[blockIndex];
                if (!blockDocument) return [];
                const anchored = new Set(normalizePromptDocument(blockDocument).nodes
                    .filter((node) => node.type === "attachment")
                    .map((node) => node.attachment_id));
                // BOTH pools. A chip attached with "Attach to this section"
                // never reaches the editor's registry, and looking it up there
                // alone is what left every one of them labelled "Reference".
                return [
                    ...normalizePromptAttachments(writingState.attachments)
                        .filter((value) => anchored.has(value.attachment_id)),
                    ...normalizePromptAttachments(
                        writingState.blockMeta[blockIndex]?.attachments),
                ];
            };
            const rowsFor = (channelKey, pool, payload) => channelContributionRows({
                channelKey, attachments: pool, candidate: payload,
                attachmentLabelFor,
            })
                // An inline capability compiles where its anchor sits, so it is
                // already in the sentence the author wrote. Printing it again
                // under the heading states one emission twice, in the wrong
                // place. `rendered_at_anchor` is the authority here, not the
                // route table — a DISABLED inline capability publishes no route
                // and read as "show it".
                .filter((contribution) => !contribution.atAnchor)
                .map((contribution) => ({
                    label: contribution.label,
                    state: contribution.state,
                    emitting: contribution.emitting,
                    // Which CAPABILITY this row is. Under H3 both `summary` and
                    // `audio_relationship` route to `summary`, so two rows read
                    // `@KWoman` and only their capability tells them apart.
                    capability: String(contribution.row?.capability_kind
                        || contribution.capabilityId || ""),
                    // `resolved` is the marker's own text when it emits one and
                    // the reason when it does not, so a Shot reads
                    // `[Shot 1] At 00:00.000,` rather than its excuse.
                    text: contribution.emitting ? contribution.resolved : "",
                    reason: contribution.emitting ? "" : contribution.resolved,
                    attachmentId: contribution.attachmentId,
                    capabilityId: contribution.capabilityId,
                }));
            const byBlock = new Map();
            for (const region of regions) {
                if (!byBlock.has(region.block)) byBlock.set(region.block, new Map());
                byBlock.get(region.block).set(region.channelKey, region.afterIndex);
            }
            for (const range of ranges) {
                const written = byBlock.get(range.block) || new Map();
                const pool = poolFor(range.block);
                if (!pool.length && !written.size) continue;
                // A block outside the render window compiles nothing in the
                // windowed payload, so its rows come from the scene-wide one and
                // its numbering is scene-wide with them. The block says so once,
                // under its heading, rather than every row saying it.
                const dormancy = writingBlockDormancy(range.block, ranges.length);
                const payload = candidateForSection(
                    writingBlockPromptId(range.block, ranges.length));
                if (!payload) continue;
                // TEMPLATE ORDER, interleaved with what the author has written.
                // Unwritten channels used to paint at the block tail, so under
                // H3 the three channels the template LEADS with appeared after
                // the last thing typed. Each one now sits after whichever
                // channel precedes it in the template, which is where its
                // heading would be if the author had written it.
                // The BLOCK's leading slot until a written channel is reached,
                // not the document's. `-1` means "before everything", so using
                // it for every block stacked block 2's template-leading
                // channels at the very top of the draft, above block 0.
                // For block 0 the block's leading slot IS -1. After that it is
                // `firstIndex` — the node carrying the `---`, which
                // `joinWritingSectionDocuments` writes as its own node, so
                // painting after it lands between the separator and the block's
                // first content. `firstIndex - 1` would be the previous block's
                // last content node, which reads adjacent and is wrong.
                let anchor = range.block === 0 ? -1 : range.firstIndex;
                for (const channelKey of keys) {
                    const isWritten = written.has(channelKey);
                    if (isWritten) anchor = written.get(channelKey);
                    const lines = rowsFor(channelKey, pool, payload);
                    // A HEADER means this channel has content. A block whose
                    // rows all resolve to nothing would put a heading on a
                    // channel that contributes nothing — six of them under H3 —
                    // and claim the opposite of what it shows. The non-emitting
                    // rows still appear once at least one row emits, because
                    // that is where "why is this one silent" gets answered.
                    // ...OR something here was switched off. Gating on
                    // emitted text alone deleted the whole block the moment the
                    // last contributing capability was disabled, taking the
                    // "Contribute here again" row with it -- the trap the
                    // disabled row exists to avoid, reintroduced one level up.
                    if (!lines.some((line) => line.text
                        || line.state === "disabled")) continue;
                    out.push({ afterIndex: anchor,
                        element: writingContributionBlock(channelKey, lines,
                            { written: isWritten, block: range.block, dormancy }) });
                }
            }
            return out;
        };
        /**
         * One channel's contributions, as read-only prose under its heading.
         *
         * The heading is NOT provisional: this block exists only when the
         * channel resolves emitting content, so a header and content are one
         * condition. Whether a text node backs it is an implementation detail
         * the author is not asked to track — typing here makes it real.
         *
         * No per-row buttons. Up to six stacked inside a read-only block, and
         * two of them had an unstated dependency on each other. A row carries
         * its actions in a menu instead, opened by clicking the row.
         *
         * MOUSE-ONLY, knowingly. Decorations refuse focus so that nothing typed
         * into one can be silently eaten by the next render, and the keyboard
         * menu opens at the caret — which can never be inside a decoration. The
         * buttons this replaces were tab stops, so this is an accessibility
         * regression accepted for the decluttering, not an oversight.
         */
        const writingContributionBlock = (channelKey, lines,
            { written, block, dormancy = null }) => {
            const host_ = document.createElement("div");
            host_.dataset.sonderWritingContribution = String(channelKey);
            host_.style.cssText = `margin:4px 0 6px;padding:5px 8px;border-left:2px solid ${COLORS.promptBorder};`
                + `background:${COLORS.panelMuted};border-radius:0 4px 4px 0;`;
            const heading = document.createElement("div");
            heading.dataset.sonderWritingChannelHeading = written ? "written" : "derived";
            // Named so a row's menu can offer the same action its hint does.
            heading.textContent = `${channelKey}:`;
            heading.style.cssText = "font:11px/1.5 system-ui;"
                + `color:${COLORS.textDim};margin-bottom:2px;`;
            host_.appendChild(heading);
            if (dormancy) {
                const note = dormancyNotice(() => dormancy, { live: false });
                note.style.cssText += "margin-bottom:3px;";
                host_.appendChild(note);
            }
            for (const line of lines) {
                const row = document.createElement("div");
                row.dataset.sonderWritingContributionRow = String(line.capabilityId || "");
                row.style.cssText = `font:11px/1.5 system-ui;color:${line.text ? COLORS.text : COLORS.textDim};`
                    + "white-space:pre-wrap;overflow-wrap:anywhere;"
                    + "border-radius:3px;padding:1px 3px;margin:0 -3px;";
                // A row that resolves to nothing NAMES its capability. Two rows
                // reading `@KWoman: …` with no way to tell which is which is
                // what sent the last pass looking for a bug that was not there.
                row.textContent = line.text
                    || `${line.label} · ${line.capability}: ${line.reason}`;
                // A composer-owned MARKER has no per-capability suppression —
                // the compiler reports `marker` even when a stored flag says
                // disabled — so offering the toggle would write a record that
                // changes nothing and leave the row exactly where it was. The
                // Structured pill guards this the same way.
                const actionable = !applyBlocked && line.capabilityId
                    && line.state !== "marker";
                // A row's own dblclick must not also reach the block's, which
                // would open the menu, reopen it, AND write a heading.
                row.addEventListener("dblclick", (event) => {
                    event.preventDefault();
                    event.stopPropagation();
                });
                if (actionable) {
                    row.style.cursor = "pointer";
                    row.title = "Click for what you can do with this contribution";
                    row.addEventListener("mouseenter", () => {
                        row.style.background = COLORS.panelRaised;
                    });
                    row.addEventListener("mouseleave", () => {
                        row.style.background = "transparent";
                    });
                    row.addEventListener("mousedown", (event) => {
                        // The decoration already refuses mousedown so a caret
                        // cannot land inside it; claim this one for the menu.
                        event.preventDefault();
                        event.stopPropagation();
                        const items = [];
                        // Copy only what the prompt actually carries. A row that
                        // emits nothing has nothing to put on a clipboard, and
                        // offering it hands the author text the render refused.
                        if (line.emitting && COPY_CAPABILITY_KINDS.has(line.capability)) {
                            items.push({ label: "Copy with handles",
                                // `hint`, not `title` — `openContextMenu` reads
                                // `hint`, so anything under another key renders
                                // as a bare label and the explanation is lost.
                                hint: "uses live @handles and discloses any frozen text",
                                action: () => copyContribution(
                                    line.attachmentId, line.capabilityId,
                                    { sceneWide: Boolean(dormancy) }) });
                        }
                        // BOTH directions, always. Turning a contribution off
                        // made its row vanish with no way back inside Writing —
                        // the draft is a browser-local fork, so the Structured
                        // pill that would re-enable it reads the saved scene and
                        // cannot see the change. A one-way action with no
                        // inverse is a trap, not a feature.
                        items.push(line.state === "disabled"
                            ? { label: "Contribute here again",
                                hint: "turn this capability back on",
                                action: () => setContributionEnabled(
                                    line.attachmentId, line.capabilityId, true) }
                            : { label: "Stop contributing here",
                                // The honest half: per-capability `enabled` is
                                // LOCAL suppression that never propagates, and
                                // dedupe promotes a sibling, so on a Reference
                                // staged in several sections the contribution
                                // does not stop — it moves.
                                hint: "on this chip only; a linked section may"
                                    + " then emit it instead",
                                action: () => setContributionEnabled(
                                    line.attachmentId, line.capabilityId, false) });
                        if (!written) {
                            items.push({ type: "separator" });
                            items.push({ label: "Write here too",
                                hint: "add this channel's heading to the draft",
                                action: () => materialiseChannelHeading(
                                    channelKey, block) });
                        }
                        openContextMenu({ x: event.clientX, y: event.clientY, items });
                    });
                }
                host_.appendChild(row);
            }
            if (!written) {
                const hint = document.createElement("div");
                hint.textContent = "Double-click to write here too.";
                hint.style.cssText = `font:10px/1.4 system-ui;color:${COLORS.textDim};`
                    + "margin-top:3px;opacity:.75;";
                hint.dataset.sonderWritingChannelHint = String(block);
                host_.appendChild(hint);
                // On the HINT, not the block: on the block it fired for a
                // double-click anywhere, including on a row that had just
                // opened its own menu.
                hint.addEventListener("dblclick", (event) => {
                    event.preventDefault();
                    materialiseChannelHeading(channelKey, block);
                });
            }
            return host_;
        };
        /**
         * Put a real `channel:` heading in the draft, on request.
         *
         * The only path that writes one. A compile callback must never do it:
         * `insertText` focuses the editor, pushes history and moves the caret,
         * and the candidate lands on a debounce while the author is typing.
         */
        const materialiseChannelHeading = (channelKey, blockIndex) => {
            const range = writingBlockNodeRanges(writingState.document)[blockIndex];
            // The TAIL anchor, not `lastIndex`: a block whose closing `---`
            // shares its last node ends before that break, and the node's end
            // put the heading in the next block.
            if (!range || !placeWritingCaret(
                { index: range.tailIndex, offset: range.tailOffset })) {
                notifyWarning("Could not place the caret in that block.",
                    { source: "prompt-writing-channel" });
                return;
            }
            draftArea.insertText(`\n${channelKey}:\n`);
            writingState.draft = draftArea.value;
            writingState.document = draftArea.promptDocument;
            saveWritingState();
            render();
        };
        draftArea = createPromptDocumentEditor({
            document: writingState.document,
            text: writingState.draft,
            attachments: writingState.attachments,
            previews: currentCandidatePayload()?.attachment_previews || {},
            attachmentLabelFor,
            attachmentContext: { scene, template: host._channelTemplate() },
            disabled: applyBlocked,
            ariaLabel: "Writing mode narrative draft",
            decorations: writingDecorations,
            onChange: ({ document: nextDocument, attachments, text, reason }) => {
                writingState.document = nextDocument;
                writingState.attachments = attachments;
                writingState.draft = text;
                if (["attachment", "history"].includes(reason)) saveWritingState();
                if (draftTimer) clearTimeout(draftTimer);
                draftTimer = setTimeout(() => updateStrip(), 300);
            },
            onActivateAttachment: async (attachment, node) => {
                const configured = await configureDraftAttachment(
                    attachment, String(node?.node_id || ""));
                if (configured) draftArea.replaceAttachment(configured);
            },
            getHostSnapshot: () => writingState.blockMeta,
            onRestoreHostSnapshot: (value) => {
                writingState.blockMeta = Array.isArray(value) ? value : [];
            },
        });
        const decorationCandidateSlice = (candidate) => ({
            projections: (candidate?.attachment_capability_projections || []).map(
                (value) => ({
                    attachment_id: value?.attachment_id,
                    capability_id: value?.capability_id,
                    capability_kind: value?.capability_kind,
                    channel_key: value?.channel_key,
                    state: value?.state,
                    text: value?.text,
                    state_reason: value?.state_reason,
                    rendered_at_anchor: value?.rendered_at_anchor === true,
                    region: value?.region,
                    order: value?.order,
                })),
            section_window_states: candidate?.section_window_states || [],
        });
        const writingDecorationSignature = () => JSON.stringify({
            document: writingDecorationDocumentSignature(
                writingState.document, templateChannelKeys(host._channelTemplate()),
                { defaultKey: writingDefaultChannelKey() }),
            attachments: writingState.attachments,
            block_meta: writingState.blockMeta,
            allocations: writingState.allocations,
            block_prompt_ids: writingBlockPromptIds,
            windowed: decorationCandidateSlice(currentCandidatePayload()),
            scene: decorationCandidateSlice(host._promptScenePayload?.()),
        });
        let lastWritingDecorationSignature = writingDecorationSignature();
        refreshWritingDecorations = () => {
            const signature = writingDecorationSignature();
            if (signature === lastWritingDecorationSignature) return false;
            lastWritingDecorationSignature = signature;
            draftArea?.refreshDecorations?.();
            return true;
        };
        draftArea.style.minHeight = "120px";
        applyBoxHeight(host, draftArea, "panelDraftBoxHeight", 160);
        registerPromptBoxGuard(draftArea, () => writingState.draft);
        draftArea.addEventListener("blur", (event) => {
            if (guard.suppressBlurCommit) return;
            if (event.relatedTarget?.closest?.("[data-sonder-prompt-context-modal='1'],[data-sonder-prompt-context-menu='1']")) return;
            writingState.draft = draftArea.value;
            writingState.document = draftArea.promptDocument;
            writingState.attachments = draftArea.promptAttachments;
            saveWritingState();
        });
        draftArea.addEventListener("keydown", (e) => e.stopPropagation());
        // `@` completion rides with this install; the mention menu is no longer
        // mounted separately. Its own sweep reclaims a menu once its editor has
        // been seen attached and has since gone, which is what a panel re-render
        // produces.
        installPromptContextMenu({
            editor: draftArea,
            // An accepted `@` mention is TEXT, so only the draft and its
            // document move; the attachment registry is untouched, unlike an
            // inserted chip.
            onMentionAccepted: () => {
                writingState.draft = draftArea.value;
                writingState.document = draftArea.promptDocument;
                saveWritingState();
            },
            onInserted: () => {
                writingState.draft = draftArea.value;
                writingState.document = draftArea.promptDocument;
                writingState.attachments = draftArea.promptAttachments;
                saveWritingState();
            },
            onCreate: (attachment) => configureDraftAttachment(attachment),
            profile: host._resolvedPromptContextProfile?.(),
            // Resolved per open, like every other surface. Writing had no
            // reference context at all while its `@` completion was installed
            // separately, so folding the two together silently took completion
            // away from the one surface that had it.
            referenceContext: () => ({
                scene: { ...(scene || {}),
                    _context_reference_frame_threshold:
                        host._referenceFrameThreshold || 0 },
                references: host._references || [],
                semanticUnits: host._promptSemanticUnits || [],
                profileId: scene?.prompt_context_profile_id
                    || host._channelTemplate().default_context_profile || "generic@1",
                scope: "global",
                resolvedProfile: host._resolvedPromptContextProfile?.(),
            }),
            writingAids: host._promptContextWritingAids?.(
                scene?.prompt_context_profile_id
                    || host._channelTemplate().default_context_profile || "generic@1") || [],
            // Deliberately no `channelKey`: Writing Source is one box
            // projecting every channel, so the caret has no single channel to
            // filter by and the full aid set stays available. Structured and
            // the timeline bars are per-channel and do filter.
        });
        bodyEl.appendChild(draftArea);
        bodyEl.appendChild(makeHeightGrip(host, bodyEl, "panelDraftBoxHeight", { min: 120, label: "Drag to resize the draft box (persists)" }));
        appendPromptContextHint(host, bodyEl);

        const toolRow = document.createElement("div");
        toolRow.style.cssText = "display:flex; gap:6px; align-items:center; flex-wrap:wrap;";
        const splitBtn = makeBtn("Split here",
            "Insert a --- section break at the cursor, keeping the field you are writing in");
        // Same guard the neighbouring "Add N/A" carries: without it the click
        // blurs the draft (firing the blur commit) before `focus()` reacquires
        // it, leaving the caret the channel resolver reads up to the browser.
        splitBtn.addEventListener("mousedown", (event) => event.preventDefault());
        splitBtn.addEventListener("click", () => {
            draftArea.focus();
            const keys = templateChannelKeys(host._channelTemplate());
            const caret = draftArea.promptTextOffset ?? draftArea.value.length;
            const active = writingChannelAtCaret(draftArea.value, caret, keys);
            // Computed BEFORE the insert, from the head of the split only.
            // Measuring the whole draft reported channels populated in other
            // sections, so the notice fired for text that was never at risk.
            const stranded = writingSplitDisclosure(draftArea.value, caret, keys);
            // `editor.insertText`, not `execCommand`: the break is multi-line
            // and execCommand drops newlines here, which left an inert inline
            // `---` that produced no section break at all.
            //
            // `asOwnNode`: growing the surrounding text node put the `---`
            // INSIDE it, and decoration placement is node-granular, so both
            // blocks reported the same node and their contributions stacked
            // under the second one.
            draftArea.insertText(writingSplitText(active, writingDefaultChannelKey()),
                { asOwnNode: true });
            // Disclosure, not a silent fix: the split continues only the
            // channel the caret is in, so any other channel with text stays
            // with the block being left behind. The author is told which,
            // because a new section that quietly drops its summary compiles to
            // something nobody wrote. The scope link itself cannot be bound
            // here — it needs a `prompt_id`, and sections exist only after Apply.
            if (stranded.length) {
                notifyInfo(`Split continues ${active || "the default field"} only. `
                    + `${stranded.join(", ")} stay${stranded.length === 1 ? "s" : ""} `
                    + "with the section above; link the new section to inherit "
                    + "them.", { source: "writing-split-linkage" });
            }
            writingState.document = draftArea.promptDocument;
            writingState.draft = draftArea.value;
            saveWritingState();
            updateStrip();
        });
        const equalizeBtn = makeBtn("Equalize", "Reset all lengths to an equal split of the scene");
        const resetBtn = makeBtn("Reset from sections",
            "Rebuild the draft + lengths from the lane's current sections. The draft you have now is kept and can be restored.");
        resetBtn.addEventListener("click", () => {
            // Names the stake rather than asking generically: this one IS
            // recoverable, and a confirmation that does not say so trains the
            // author to dismiss it unread.
            if (writingDraftHasContent(writingDraftSnapshot())
                && !window.confirm("Rebuild the draft from the lane's sections?"
                    + " Your current draft is kept and can be restored.")) return;
            // Snapshot BEFORE reconstructing. Reset is the only escape offered
            // when Apply is staleness-blocked, so it must not also be the thing
            // that destroys the draft the author cannot apply yet.
            const previous = writingDraftSnapshot();
            reconstructDraftFromSections();
            writingState.stash = writingDraftHasContent(previous) ? previous : null;
            saveWritingState();
            render();
        });
        const restoreBtn = writingState.stash
            ? makeBtn("Restore draft", "Bring back the draft that the last Reset replaced")
            : null;
        restoreBtn?.addEventListener("click", () => {
            const stash = writingState.stash;
            if (!stash) return;
            writingState.document = normalizePromptDocument(
                stash.document, String(stash.draft || ""));
            writingState.draft = promptDocumentText(writingState.document);
            writingState.attachments = normalizePromptAttachments(stash.attachments);
            writingState.allocations = (stash.allocations || []).map((a) => ({
                length: Math.max(0, parseInt(a?.length, 10) || 0),
                dirty: !!a?.dirty,
            }));
            writingState.blockMeta = normalizeWritingBlockMeta(stash.blockMeta);
            writingState.baseModifiedAt = String(stash.baseModifiedAt || "");
            // The stamp travels with the draft. Without it a restored
            // pre-declaration draft picks up the CURRENT default and its
            // leading unheadered text silently relocates on the next Apply —
            // the exact relocation the stamp exists to prevent, reintroduced
            // by the one control offered when Apply is staleness-blocked.
            writingState.defaultDraftChannel = String(stash.defaultDraftChannel || "");
            writingState.stash = null;
            saveWritingState();
            render();
        });
        const applyBtn = makeBtn(applyBlocked ? "Apply (locked)" : "Apply", "Replace the lane's sections with the draft blocks (undoable)", "primary");
        toolRow.append(splitBtn, equalizeBtn, resetBtn,
            ...(restoreBtn ? [restoreBtn] : []), applyBtn);
        bodyEl.appendChild(toolRow);

        const readout = document.createElement("div");
        readout.style.cssText = `font-size:10px; color:${COLORS.textDim};`;
        bodyEl.appendChild(readout);
        const strip = document.createElement("div");
        strip.style.cssText = "display:flex; flex-direction:column; gap:4px;";
        bodyEl.appendChild(strip);

        const chipLengths = () => writingState.allocations.reduce((sum, a) => sum + (a?.length || 0), 0);

        // The actual laid-out duration uses max(minLen, allocation) per block,
        // so it can exceed the raw chip sum when an allocation is below the
        // template minimum — this is the value the scene must fit/grow to.
        const laidOutTotal = (blocks) =>
            blocks.reduce((sum, _t, i) =>
                sum + Math.max(minLen, writingState.allocations[i]?.length ?? minLen), 0);

        const updateReadout = (blocks) => {
            const total = chipLengths();
            const requiredTotal = laidOutTotal(blocks);
            const willExtend = blocks.length > 0 && requiredTotal > totalFrames;
            readout.textContent = blocks.length
                ? `Sections: ${blocks.length} — total ${total}f of ${totalFrames}f (${Math.max(0, totalFrames - total)}f remaining; min ${minLen}f per section)`
                : "No sections yet — the whole draft is one block until you add --- lines.";
            if (willExtend) readout.textContent += ` — Apply will extend the scene to ${requiredTotal}f.`;
            // No staleness gate. `modified_at` is a single WHOLE-PROJECT stamp
            // bumped on every save, and there are ~50 save sites — moving a
            // clip, importing an asset, editing another scene. A draft was
            // therefore on a countdown from the moment it was created, and the
            // only offered escape (Reset) applies the rebuilt draft rather than
            // the authored one, so authored work had no path in at all.
            // Apply states intent plainly: make the lane match this draft.
            // Replacing whatever the lane holds now is the feature.
            readout.style.color = COLORS.textDim;
            // Over-budget no longer blocks — Apply extends the scene instead
            // (which also satisfies the per-section minimum). Only a locked
            // lane or an empty draft can block.
            if (applyBlocked) applyBtn.textContent = "Apply (locked)";
            else applyBtn.textContent = willExtend ? `Apply & Extend Scene to ${requiredTotal}f` : "Apply";
            applyBtn.disabled = applyBlocked || !blocks.length;
        };

        const updateStrip = () => {
            const blockDocuments = reconcileWritingBlockMeta(
                splitWritingPromptDocument(writingState.document, {
                    keepEmpty: writingState.blockMeta.length > 0,
                }));
            const blocks = blockDocuments.map(promptDocumentText);
            if (blocks.length !== writingState.allocations.length) {
                writingState.allocations = allocateWritingBlocks(
                    blocks, totalFrames, minLen, writingState.allocations);
            }
            // Stamps `writingBlockPromptIds` as a side effect, which is what the
            // strip rows and the draft decorations both key on. Cheap and pure;
            // the alternative is the strip labelling blocks by an id nothing
            // else agrees with.
            buildWritingCandidatePatch(blockDocuments);
            strip.textContent = "";
            blocks.forEach((text, i) => {
                const blockStart = writingState.allocations.slice(0, i)
                    .reduce((sum, value) => sum + Math.max(minLen, value?.length ?? minLen), 0);
                const blockEnd = blockStart
                    + Math.max(minLen, writingState.allocations[i]?.length ?? minLen);
                const attachmentScene = { ...(scene || {}),
                    _context_channel_keys: templateChannelKeys(host._channelTemplate()),
                    _context_consumer_start: blockStart,
                    _context_consumer_end: blockEnd,
                    _context_reference_frame_threshold: host._referenceFrameThreshold || 0 };
                const blockCard = document.createElement("div");
                blockCard.style.cssText = "display:flex;flex-direction:column;gap:3px;";
                const chip = document.createElement("div");
                chip.style.cssText = `display:flex; gap:8px; align-items:center; font-size:10px; color:${COLORS.text};`;
                const preview = document.createElement("span");
                preview.style.cssText = "flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;";
                preview.textContent = `${i + 1}. ${text}`;
                preview.title = text;
                const lengthInput = smallInput({ value: String(writingState.allocations[i]?.length ?? minLen), numeric: true, width: "64px" });
                lengthInput.min = String(minLen);
                lengthInput.title = `Length in frames (min ${minLen})`;
                const timecodeHint = document.createElement("span");
                timecodeHint.style.cssText = `flex-shrink:0; font-family:${FONT.mono}; color:${COLORS.textDim}; min-width:60px;`;
                const syncHint = () => {
                    const frames = writingState.allocations[i]?.length ?? 0;
                    timecodeHint.textContent = host._timecodeMode === "timecode"
                        ? host._frameToTimecode(frames)
                        : `${frames}f`;
                };
                lengthInput.addEventListener("change", () => {
                    const value = Math.max(minLen, parseInt(lengthInput.value, 10) || minLen);
                    lengthInput.value = String(value);
                    writingState.allocations[i] = { length: value, dirty: true };
                    syncHint();
                    updateReadout(blocks);
                    saveWritingState();
                    previewWritingBlocks(blockDocuments);
                });
                lengthInput.addEventListener("keydown", (e) => e.stopPropagation());
                syncHint();
                chip.append(preview, lengthInput, timecodeHint);
                blockCard.appendChild(chip);
                // The strip is the block-identity surface — it already knows
                // which block is which — so the reason lands here rather than
                // being anchored inside the draft, where node granularity would
                // make it approximate. Its tooltip owns the caveat: a draft's
                // blocks are recompacted from frame 0 and the compile window is
                // still the timeline selection, so the two need not line up.
                const blockNotice = dormancyNotice(
                    () => writingBlockDormancy(i, blocks.length));
                blockNotice.title = "Writing blocks are laid out from frame 0"
                    + " on Apply; the render window still follows the"
                    + " timeline selection, so the two can disagree.";
                blockCard.appendChild(blockNotice);
                const anchorIds = new Set(normalizePromptDocument(blockDocuments[i]).nodes
                    .filter((node) => node.type === "attachment")
                    .map((node) => node.attachment_id));
                const renderBlockScope = () => {
                    const meta = writingState.blockMeta[i];
                    const scoped = normalizePromptAttachments(meta?.attachments)
                        .filter((value) => !anchorIds.has(value.attachment_id));
                    const row = createScopeChipRow({
                        label: `Block ${i + 1} context`,
                        attachments: scoped,
                        previews: candidateForSection(writingBlockPromptId(
                            i, writingState.allocations.length))?.attachment_previews || {},
                        attachmentLabelFor,
                        disabled: applyBlocked,
                        profile: host._resolvedPromptContextProfile?.(),
                        allowedKinds: host._channelTemplate().shot_marker_channel
                            ? ["shot", "timestamp", "prompt_link_scope", "reference", "custom"]
                            : ["prompt_link_scope", "reference", "custom"],
                        reusableAttachments: writingState.attachments,
                        allSceneAttachments: writingState.attachments,
                        reuseContext: { scene: attachmentScene,
                            template: host._channelTemplate() },
                        onReuse: (configured) => {
                            const nextRegistry = normalizePromptAttachments([
                                ...writingState.attachments, configured]);
                            draftArea.transactPromptAttachments?.(nextRegistry, () => {
                                meta.attachments = [...scoped, configured];
                            });
                            saveWritingState(); renderBlockScope();
                            previewWritingBlocks(blockDocuments);
                        },
                        onAdd: async (kind) => {
                            const configured = await configurePromptAttachment({ kind }, {
                                scene: attachmentScene,
                                references: host._references || [],
                                semanticUnits: host._promptSemanticUnits || [],
                                profileId: scene?.prompt_context_profile_id
                                    || host._channelTemplate().default_context_profile || "generic@1",
                                scope: "section",
                                anchoredChannels: [],
                                profile: currentPromptProfile(),
                                placementPhases: promptPlacementPhases(),
                                managedSpeakerSubjectIds: currentManagedSpeakerSubjectIds(),
                                ordinalManifest: currentCandidatePayload()?.ordinal_manifest || {},
                                candidate: currentCandidatePayload(),
                            });
                            if (!configured) return;
                            const nextRegistry = normalizePromptAttachments([
                                ...writingState.attachments, configured]);
                            draftArea.transactPromptAttachments?.(nextRegistry, () => {
                                meta.attachments = [...scoped, configured];
                            });
                            saveWritingState(); renderBlockScope();
                            previewWritingBlocks(blockDocuments);
                        },
                        onActivate: async (attachment) => {
                            const configured = await configurePromptAttachment(attachment, {
                                scene: attachmentScene,
                                references: host._references || [],
                                semanticUnits: host._promptSemanticUnits || [],
                                profileId: scene?.prompt_context_profile_id
                                    || host._channelTemplate().default_context_profile || "generic@1",
                                scope: "section",
                                anchoredChannels: [],
                                profile: currentPromptProfile(),
                                placementPhases: promptPlacementPhases(),
                                managedSpeakerSubjectIds: currentManagedSpeakerSubjectIds(),
                                candidate: currentCandidatePayload(),
                                ordinalManifest: currentCandidatePayload()?.ordinal_manifest || {},
                            });
                            if (!configured) return;
                            const groupId = attachment.emission_group_id;
                            const linked = writingState.attachments.filter((value) =>
                                value.emission_group_id === groupId).length > 1;
                            const nextRegistry = writingState.attachments.map((value) =>
                                linked && value.emission_group_id === groupId
                                    ? propagateLinkedPromptAttachment(
                                        configured, value, attachment.attachment_id)
                                    : (value.attachment_id === configured.attachment_id
                                        ? configured : value));
                            draftArea.transactPromptAttachments?.(nextRegistry, () => {
                                for (const blockMeta of writingState.blockMeta) {
                                    blockMeta.attachments = normalizePromptAttachments(
                                        blockMeta.attachments).map((value) =>
                                        linked && value.emission_group_id === groupId
                                            ? propagateLinkedPromptAttachment(
                                                configured, value, attachment.attachment_id)
                                            : (value.attachment_id === configured.attachment_id
                                                ? configured : value));
                                }
                            });
                            saveWritingState(); renderBlockScope();
                            previewWritingBlocks(blockDocuments);
                        },
                        onUnlink: (configured) => {
                            const nextRegistry = writingState.attachments.map((value) =>
                                value.attachment_id === configured.attachment_id
                                    ? configured : value);
                            draftArea.transactPromptAttachments?.(nextRegistry, () => {
                                meta.attachments = scoped.map((value) =>
                                    value.attachment_id === configured.attachment_id
                                        ? configured : value);
                            });
                            saveWritingState(); renderBlockScope();
                            previewWritingBlocks(blockDocuments);
                        },
                        onRemove: (attachment) => {
                            const nextRegistry = writingState.attachments.filter((value) =>
                                value.attachment_id !== attachment.attachment_id);
                            draftArea.transactPromptAttachments?.(nextRegistry, () => {
                                meta.attachments = scoped.filter((value) =>
                                    value.attachment_id !== attachment.attachment_id);
                            });
                            saveWritingState(); renderBlockScope();
                            previewWritingBlocks(blockDocuments);
                        },
                    });
                    const previous = blockCard.querySelector("[data-writing-scope]");
                    row.dataset.writingScope = "1";
                    if (previous) previous.replaceWith(row); else blockCard.appendChild(row);
                };
                renderBlockScope();
                strip.appendChild(blockCard);
            });
            updateReadout(blocks);
            previewWritingBlocks(blockDocuments);
        };

        equalizeBtn.addEventListener("click", () => {
            const blocks = splitWritingPromptDocument(writingState.document, {
                keepEmpty: writingState.blockMeta.length > 0,
            })
                .map(promptDocumentText);
            writingState.allocations = allocateWritingBlocks(
                blocks.map(() => " "), totalFrames, minLen, []); // equal weights
            saveWritingState();
            updateStrip();
        });

        applyBtn.addEventListener("click", async () => {
            if (applyBtn.disabled) return;
            const blocks = reconcileWritingBlockMeta(
                splitWritingPromptDocument(writingState.document, {
                    keepEmpty: writingState.blockMeta.length > 0,
                }));
            const { sections, totalFrames: cursor } = writingSectionsFromDraft({
                blocks,
                attachments: writingState.attachments,
                blockMeta: writingState.blockMeta,
                allocations: writingState.allocations,
                channelKeys: templateChannelKeys(host._channelTemplate()),
                defaultKey: writingDefaultChannelKey(),
                minLen,
                newId: (index) => globalThis.crypto?.randomUUID?.()
                    || `${Date.now()}-${index}`,
            });
            const sceneDur = host.activeScene?.duration_frames || host.totalFrames || 0;
            const extendDurationTo = cursor > sceneDur ? cursor : 0;
            // The writing tool rewrites SECTIONS only, so the scene-global text
            // has to be handed back untouched. Passing just `global` is not that:
            // it is the label-free mirror over the LEGACY three channels, so it
            // reads empty on any other template, and the flat field is
            // destructive server-side (`Scene.set_global_prompt` clears channels
            // 2..n). Apply would silently erase the whole global bag. Carry the
            // channels themselves, tagged with the template they were authored
            // under so no retarget is attempted.
            const previousCount = (host.activeScene?.prompt_sections || []).length;
            let applied = false;
            try {
                applied = await host._applyPromptSetup({
                    global: host.activeScene?.prompt || "",
                    global_channels: { ...(host.activeScene?.global_channels || {}) },
                    global_channel_docs: structuredClone(host.activeScene?.global_channel_docs || {}),
                    global_attachments: structuredClone(host.activeScene?.global_attachments || []),
                    source_channel_template_id: host._channelTemplate().id,
                    source_channel_template: host._channelTemplate(),
                    sections,
                    extendDurationTo,
                });
            } catch (error) {
                notifyWarning(error?.message || "Writing draft Apply was refused.",
                    { source: "prompt-writing-stale" });
                return;
            }
            // The catch above only ever sees a SYNCHRONOUS refusal. An async
            // one — a 409 conflict, an over-cap attachment set — resolves
            // normally, so without this the draft, its chips, its block
            // metadata and the Restore stash were all discarded and the author
            // was told "Applied N section(s)" while nothing had landed.
            if (!applied) return;
            clearWritingState({ keepAsStash: writingDraftSnapshot() });
            // Disclosure, not a gate. Replacing the lane is exactly what Apply
            // was asked to do, so it is reported, never confirmed.
            const reshaped = previousCount && previousCount !== sections.length
                ? `Replaced ${previousCount} section(s) with ${sections.length}.`
                : `Applied ${sections.length} section(s) from the draft.`;
            notifySuccess(
                extendDurationTo
                    ? `${reshaped} Scene extended to ${extendDurationTo}f.`
                    : reshaped,
                { source: "prompt-writing-apply" });
            render();
        });

        updateStrip();
    };

    const renderContextSettings = (bodyEl, scene) => {
        bodyEl.appendChild(sectionTitle("Prompt Context"));
        const card = document.createElement("div");
        card.style.cssText = `display:flex;flex-direction:column;gap:7px;padding:8px;border:1px solid ${COLORS.promptBorder};border-radius:6px;background:${COLORS.panelRaised};`;
        const profile = document.createElement("select");
        profile.style.cssText = chromeInputCss();
        const activeTemplate = host._channelTemplate();
        const defaultProfile = activeTemplate.default_context_profile || "generic@1";
        // Provider requirements are enforced per channel template, so pairing a
        // format with a template it does not target would let the format claim
        // requirements nothing checks. Incompatible choices are shown disabled
        // rather than hidden, so an existing bad pairing stays visible.
        const catalog = host._promptContextCatalog || {};
        const catalogReady = Number(catalog.schema_version) === 1
            && Array.isArray(catalog.profiles);
        const catalogProfiles = catalogReady ? catalog.profiles : [];
        const universal = String(catalog.universal_template || "*");
        const activeTemplateId = String(activeTemplate.id || "");
        const catalogByKey = new Map(catalogProfiles.map(
            (value) => [String(value?.key || ""), value]));
        const compatible = (key) => {
            if (!key) return true;
            // A template naming a format as its own default has declared the
            // pairing — mirror `profile_matches_template` exactly, or a copied
            // MiniMax template shows its own default disabled while the server
            // happily compiles it.
            if (key === defaultProfile) return true;
            const allowed = catalogByKey.get(key)?.compatible_templates;
            if (!Array.isArray(allowed) || !allowed.length) return false;
            return allowed.includes(universal) || allowed.includes("standard")
                || allowed.includes(activeTemplateId);
        };
        const addProfileOption = (value, label) => {
            const option = document.createElement("option");
            option.value = value;
            option.textContent = compatible(value) ? label
                : `${label} — not available for this channel template`;
            option.disabled = !compatible(value);
            option.title = value || defaultProfile;
            profile.appendChild(option);
            return option;
        };
        const defaultDescriptor = catalogByKey.get(defaultProfile);
        addProfileOption("", `Template default · ${defaultDescriptor?.name || defaultProfile} · ${defaultProfile}`);
        for (const value of catalogProfiles) {
            const key = String(value?.key || "");
            const name = String(value?.name || key);
            if (key) addProfileOption(key, name === key ? key : `${name} · ${key}`);
        }
        const selectedProfileKey = String(scene.prompt_context_profile_id || "");
        if (selectedProfileKey && !catalogByKey.has(selectedProfileKey)) {
            addProfileOption(selectedProfileKey,
                `${selectedProfileKey} - unavailable from catalog`);
        }
        profile.value = selectedProfileKey;
        profile.disabled = !catalogReady;
        const profileRow = document.createElement("label");
        profileRow.style.cssText = `display:flex;align-items:center;gap:8px;font-size:10px;color:${COLORS.textDim};`;
        profileRow.append("Prompt format", profile); card.appendChild(profileRow);
        if (!catalogReady) {
            const state = document.createElement("div");
            state.style.cssText = `font-size:10px;color:${
                host._referencesError ? COLORS.dangerText : COLORS.textDim};`;
            state.textContent = host._referencesError
                ? `Prompt format catalog failed to load: ${host._referencesError}`
                : "Loading the authoritative prompt format catalog...";
            card.appendChild(state);
        }
        const currentDescriptor = () => catalogByKey.get(profile.value || defaultProfile);
        const openProfileEditor = ({ descriptor = null, fresh = false,
            editAsVersion = false } = {}) => {
            if (!catalogReady || (!fresh && !descriptor?.fork_seed)) return;
            const selected = fresh
                ? freshPromptFormatDefinition(activeTemplate)
                : structuredClone(descriptor.fork_seed);
            const backdrop = document.createElement("div");
            backdrop.dataset.sonderPromptContextModal = "1";
            backdrop.style.cssText = "position:fixed;inset:0;z-index:12000;background:rgba(5,8,12,.72);display:flex;align-items:center;justify-content:center;padding:20px;";
            const editor = document.createElement("div");
            editor.dataset.profileEditor = "1";
            editor.style.cssText = `width:min(720px,94vw);max-height:84vh;overflow:auto;padding:14px;border:1px solid ${COLORS.border};border-radius:8px;background:${COLORS.panelRaised};box-shadow:0 18px 60px rgba(0,0,0,.55);display:grid;grid-template-columns:130px minmax(0,1fr);gap:7px;align-items:center;font:10px system-ui;color:${COLORS.text};`;
            const heading = document.createElement("strong");
            heading.style.cssText = `grid-column:1/-1;font-size:13px;color:${COLORS.text};`;
            heading.textContent = fresh ? "New prompt format"
                : editAsVersion ? "Edit as new version" : "Save as custom";
            editor.appendChild(heading);
            const addField = (label, value = "") => {
                const title = document.createElement("span"); title.textContent = label;
                const input = document.createElement("input"); input.value = value;
                input.style.cssText = chromeInputCss(); editor.append(title, input); return input;
            };
            const sourceKey = String(descriptor?.key || "");
            const [sourceId = "", sourceVersion = "1"] = sourceKey.split("@");
            const name = addField("Name", editAsVersion
                ? String(descriptor?.name || sourceId) : fresh
                    ? `New ${activeTemplate.name || "prompt"} format`
                    : `Custom ${descriptor?.name || activeTemplate.name || "Prompt"}`);
            const id = addField("Stable ID", editAsVersion
                ? sourceId : `custom_${Date.now().toString(36)}`);
            id.title = "Durable secondary identity. Name is the user-facing label.";
            const version = addField("Version", editAsVersion
                ? nextPromptFormatVersion(sourceVersion) : "1");
            const customCapability = selected.capabilities?.custom || {};
            const formatter = addField("Custom-chip format",
                customCapability.formatter ?? "{text}");
            const route = addField("Default channel", customCapability.channel_key
                || templateChannelKeys(activeTemplate)[0] || "visual");
            const rawValidators = structuredClone(selected.validators || []);
            const requiredValues = rawValidators.filter((value) =>
                value?.kind === "required_channel" && value?.severity !== "warning");
            const maxValue = rawValidators.find((value) =>
                value?.kind === "max_final_chars" && value?.severity !== "warning");
            const validatorsBase = rawValidators.filter((value) =>
                !requiredValues.includes(value) && value !== maxValue);
            const requiredChannels = addField("Required channels", requiredValues
                .map((value) => value.channel_key).filter(Boolean).join(", "));
            requiredChannels.placeholder = "visual, sound";
            const maxChars = addField("Max prompt chars", maxValue?.limit || "");
            maxChars.type = "number"; maxChars.min = "1"; maxChars.max = "262144";

            const writingAids = structuredClone(selected.writing_aids || []);
            const aidEditor = document.createElement("div");
            aidEditor.style.cssText = `grid-column:1/-1;display:flex;flex-direction:column;gap:4px;padding:7px;border:1px solid ${COLORS.border};border-radius:5px;`;
            const renderWritingAids = () => {
                aidEditor.replaceChildren();
                const title = document.createElement("strong");
                title.textContent = `Writing aids (${writingAids.length})`;
                aidEditor.appendChild(title);
                // The format owns two guidance surfaces and they must not be
                // collapsed into one: a writing aid is prose the author inserts
                // and the model reads, while a capability's description,
                // example, and help only describe what that part emits.
                const division = document.createElement("p");
                division.dataset.promptFormatGuidanceDivision = "1";
                division.style.cssText = `margin:0;font-size:9px;line-height:1.5;color:${COLORS.textDim};`;
                division.textContent = "Writing aids are insertable prose: they land at the cursor and reach the prompt. Reference prompt part help and examples are shown beside the controls only and are never inserted or compiled.";
                aidEditor.appendChild(division);
                writingAids.forEach((aid, index) => {
                    const row = document.createElement("div");
                    row.style.cssText = "display:grid;grid-template-columns:120px minmax(0,1fr) auto;gap:4px;";
                    const labelInput = document.createElement("input");
                    labelInput.value = aid.label || aid.id || "";
                    labelInput.style.cssText = chromeInputCss();
                    labelInput.setAttribute("aria-label", "Writing aid label");
                    const textInput = document.createElement("input");
                    textInput.value = aid.text || "";
                    textInput.style.cssText = chromeInputCss();
                    textInput.setAttribute("aria-label", "Writing aid text");
                    labelInput.addEventListener("input", () => { writingAids[index].label = labelInput.value; });
                    textInput.addEventListener("input", () => { writingAids[index].text = textInput.value; });
                    const remove = makeBtn("Remove", "Remove this writing aid", "danger");
                    remove.addEventListener("click", () => {
                        writingAids.splice(index, 1); renderWritingAids();
                    });
                    row.append(labelInput, textInput, remove); aidEditor.appendChild(row);

                    // Choices and channels are the other two halves of an aid's
                    // declaration and were previously editable only at creation,
                    // so a built-in forked for one tweak could never have its
                    // vocabulary or targeting corrected.
                    const detail = document.createElement("div");
                    detail.style.cssText = "display:grid;grid-template-columns:120px minmax(0,1fr);gap:4px;";
                    const choicesInput = document.createElement("input");
                    choicesInput.value = formatWritingAidChoices(aid.fields);
                    choicesInput.style.cssText = chromeInputCss();
                    choicesInput.placeholder = "motion=pans left|pans right";
                    choicesInput.setAttribute("aria-label", "Writing aid choices");
                    const choicesLabel = document.createElement("span");
                    choicesLabel.textContent = "Choices";
                    choicesLabel.style.cssText = `font-size:9px;color:${COLORS.textDim};align-self:center;`;
                    const channelsInput = document.createElement("input");
                    channelsInput.value = (Array.isArray(aid.channel_keys)
                        ? aid.channel_keys : []).join(", ");
                    channelsInput.style.cssText = chromeInputCss();
                    channelsInput.placeholder = "all channels";
                    channelsInput.setAttribute("aria-label", "Writing aid channels");
                    const channelsLabel = document.createElement("span");
                    channelsLabel.textContent = "Channels";
                    channelsLabel.style.cssText = `font-size:9px;color:${COLORS.textDim};align-self:center;`;
                    // Reconcile on commit, not per keystroke: a half-typed key
                    // would otherwise mint a declaration per character.
                    choicesInput.addEventListener("change", () => {
                        writingAids[index].fields = parseWritingAidChoices(
                            choicesInput.value, writingAids[index].text,
                            writingAids[index].fields);
                    });
                    channelsInput.addEventListener("change", () => {
                        const keys = channelsInput.value.split(",")
                            .map((value) => value.trim()).filter(Boolean);
                        if (keys.length) writingAids[index].channel_keys = keys;
                        else delete writingAids[index].channel_keys;
                    });
                    detail.append(choicesLabel, choicesInput,
                        channelsLabel, channelsInput);
                    aidEditor.appendChild(detail);
                });
            };
            renderWritingAids();
            editor.appendChild(aidEditor);
            const aidLabel = addField("New aid label", "");
            const aidText = addField("New aid text", "");
            const aidChoices = addField("New aid choices", "");
            aidChoices.placeholder = "language=English|Spanish; motion=pans left|pans right";
            const aidChannels = addField("New aid channels", "");
            aidChannels.placeholder = "all channels";
            const aidProblem = document.createElement("span");
            aidProblem.style.cssText = `grid-column:1/-1;font-size:9px;color:${COLORS.dangerText};`;
            editor.appendChild(aidProblem);
            const addAid = makeBtn("Add writing aid", "Add this bounded writing aid");
            addAid.addEventListener("click", () => {
                aidProblem.textContent = "";
                if (!aidLabel.value.trim() || !aidText.value) {
                    aidProblem.textContent = "A writing aid needs a label and text.";
                    return;
                }
                const fields = parseWritingAidChoices(aidChoices.value, aidText.value);
                // An enum with no values is refused by the server, so minting one
                // silently produced a format that could never be saved and said
                // nothing about why. Name the placeholder instead.
                const empty = Object.entries(fields)
                    .filter(([, declaration]) => !declaration.values.length)
                    .map(([name]) => `{${name}}`);
                if (empty.length) {
                    aidProblem.textContent =
                        `Give ${empty.join(", ")} choices, or remove ${
                            empty.length === 1 ? "it" : "them"} from the text.`;
                    return;
                }
                const channelKeys = aidChannels.value.split(",")
                    .map((value) => value.trim()).filter(Boolean);
                writingAids.push({ id: `custom_${Date.now().toString(36)}`,
                    label: aidLabel.value.trim(), text: aidText.value, fields,
                    ...(channelKeys.length ? { channel_keys: channelKeys } : {}) });
                aidLabel.value = ""; aidText.value = ""; aidChoices.value = "";
                aidChannels.value = "";
                renderWritingAids();
            });
            editor.append(document.createElement("span"), addAid);

            // Everything the format declares beyond identity and the custom
            // chip lives in its own module. Forking previously read only the
            // `value` half of each role pair and rebuilt bare strings, so the
            // server's `label = value` fallback collapsed "First frame" to
            // "first_frame"; the declaration editor round-trips both halves.
            const declarations = mountPromptFormatDeclarationEditor({
                definition: selected, template: activeTemplate, catalog,
            });
            editor.appendChild(declarations.element);

            const footer = document.createElement("div");
            footer.style.cssText = "grid-column:1/-1;display:flex;justify-content:flex-end;gap:6px;margin-top:4px;";
            const cancel = makeBtn("Cancel", "Close without saving", "subtle");
            const save = makeBtn("Save prompt format", "Validate and save this immutable version", "primary");
            let closed = false;
            let releaseEscape = () => {};
            const close = () => {
                if (closed) return;
                closed = true;
                releaseEscape();
                declarations.cleanup();
                backdrop.remove();
            };
            // The format editor now holds every declaration group, so an
            // accidental dismissal costs far more than it used to. Cancel is
            // explicit intent and stays unguarded.
            const draftGuard = createModalDraftGuard({
                controls: () => [name, id, version, formatter, route,
                    requiredChannels, maxChars,
                    ...editor.querySelectorAll?.("input, textarea, select") || []],
                message: "Discard this prompt format draft? Your unsaved changes will be lost.",
            });
            // Escape goes through keyboard ownership, not a document-capture
            // listener. Window capture runs FIRST, so this panel's own OVERLAY
            // consumer — which closes the Prompt tool unconditionally — consumed
            // Escape and stopped it before a document listener could ever see
            // it: the format editor stayed open while the tool behind it shut,
            // and this draft guard never ran.
            releaseEscape = registerKeyboardConsumer({
                id: `sonder-prompt-format-editor-${Date.now().toString(36)}`,
                priority: KEY_PRIORITY.OVERLAY,
                keydown: (event) => {
                    if (event.isComposing === true || event.keyCode === 229) return false;
                    if (event.key !== "Escape") return false;
                    if (draftGuard.confirmDismiss()) close();
                    return true;
                },
            });
            cancel.addEventListener("click", close);
            backdrop.addEventListener("click", (event) => {
                if (event.target === backdrop && draftGuard.confirmDismiss()) close();
            });
            save.addEventListener("click", async () => {
                const profileId = id.value.trim().replace(/[^A-Za-z0-9_.-]+/g, "_");
                const versionId = version.value.trim();
                if (!profileId || !versionId) {
                    notifyWarning("Stable ID and version are required.",
                        { source: "prompt-profile-invalid" });
                    return;
                }
                const collisionKey = `${profileId}@${versionId}`;
                const collision = (host._promptContextProfiles || []).some((value) =>
                    `${value.profile_id}@${value.version || "1"}` === collisionKey)
                    || Boolean(catalogByKey.get(collisionKey)?.builtin);
                if (collision) {
                    notifyWarning(`A saved format already uses stable ID and version ${collisionKey}.`,
                        { source: "prompt-profile-immutable" });
                    return;
                }
                const validators = structuredClone(validatorsBase);
                for (const channelKey of requiredChannels.value.split(",")
                    .map((value) => value.trim()).filter(Boolean)) {
                    validators.push({ kind: "required_channel", channel_key: channelKey,
                        severity: "error" });
                }
                if (maxChars.value) validators.push({ kind: "max_final_chars",
                    limit: Number(maxChars.value), severity: "error" });
                // Inherit the SOURCE format's template binding, not whichever
                // template happened to be active while forking. Writing the
                // active id would silently lock a fork made under, say, Visual +
                // Speech + Sound to that one template forever; a fork of a
                // MiniMax format must still stay bound to its own template.
                const universalToken = String(
                    host._promptContextCatalog?.universal_template || "*");
                const forkBinding = promptFormatTemplateBinding(
                    descriptor, selected, universalToken);
                const definition = declarations.collect();
                delete definition.template_id;
                delete definition.compatible_templates;
                Object.assign(definition, forkBinding, {
                    capabilities: {
                        ...definition.capabilities,
                        custom: { channel_key: route.value.trim(),
                            placement: "inline", formatter: formatter.value },
                    },
                    writing_aids: structuredClone(writingAids), validators,
                });
                save.disabled = true;
                try {
                    await host._createPromptContextProfile?.({
                        profile_id: profileId, version: versionId,
                        name: name.value.trim() || profileId, definition,
                    });
                    await commit([{ type: "update_scene_fields", fields: {
                        prompt_context_profile_id: collisionKey,
                    } }], "select custom prompt profile");
                    close();
                } catch (error) {
                    save.disabled = false;
                    notifyWarning(error?.message || "Custom profile was rejected.",
                        { source: "prompt-profile-invalid" });
                }
            });
            footer.append(cancel, save); editor.appendChild(footer);
            backdrop.appendChild(editor); document.body.appendChild(backdrop);
            name.focus(); name.select();
        };

        const actionRow = document.createElement("div");
        actionRow.style.cssText = "display:flex;align-items:center;gap:5px;min-width:0;";
        profileRow.style.flex = "1 1 auto";
        profile.style.minWidth = "160px";
        const saveAs = makeBtn("Save as custom…", "Create an immutable custom format from this format");
        const createNew = makeBtn("New…", "Start a minimally valid prompt format");
        const menuWrap = document.createElement("div");
        menuWrap.style.cssText = "position:relative;flex:0 0 auto;";
        const menuButton = makeBtn("⋯", "Prompt format actions", "subtle",
            "Prompt format actions");
        for (const button of [saveAs, createNew, menuButton]) {
            button.style.flex = "0 0 auto";
            button.disabled = !catalogReady;
        }
        saveAs.addEventListener("click", () => openProfileEditor({
            descriptor: currentDescriptor(), fresh: false }));
        createNew.addEventListener("click", () => openProfileEditor({ fresh: true,
            descriptor: { compatible_templates: [activeTemplateId] } }));
        menuButton.addEventListener("click", () => {
            // Toggle, not rebuild. Removing and immediately re-adding meant the
            // trigger could never close its own menu, and with every action
            // disabled (a built-in format) there was no way to dismiss it.
            const open = menuWrap.querySelector("[data-prompt-format-menu]");
            open?.remove();
            if (open) return;
            const descriptor = currentDescriptor();
            const menu = document.createElement("div");
            menu.dataset.promptFormatMenu = "1";
            menu.dataset.sonderPromptContextMenu = "1";
            menu.style.cssText = `position:absolute;right:0;top:calc(100% + 4px);z-index:12010;min-width:190px;padding:5px;border:1px solid ${COLORS.border};border-radius:6px;background:${COLORS.panelRaised};box-shadow:0 12px 30px rgba(0,0,0,.45);display:flex;flex-direction:column;gap:4px;`;
            const edit = makeBtn("Edit as new version…", "Save changes as another immutable version");
            const actions = promptFormatMenuActions(descriptor);
            setButtonDisabled(edit, !actions.edit);
            // Say WHY an action is unavailable — a built-in is immutable —
            // rather than leaving a dimmed control unexplained.
            const builtinReason = "Built-in formats are immutable. Use Save as custom… first.";
            edit.title = actions.edit
                ? "Save changes as another immutable version" : builtinReason;
            edit.addEventListener("click", () => {
                menu.remove(); openProfileEditor({ descriptor, editAsVersion: true });
            });
            menu.appendChild(edit);

            // Delete targets EVERY custom format, not the selected one. Acting
            // on the selection made Delete reachable only while the format was
            // in use, which is precisely when the server refuses it.
            const deleteHeading = document.createElement("div");
            deleteHeading.textContent = "Delete custom format";
            deleteHeading.style.cssText = `margin-top:2px;padding:2px 2px 0;border-top:1px solid ${COLORS.border};font:9px system-ui;color:${COLORS.textDim};`;
            menu.appendChild(deleteHeading);
            const targets = promptFormatDeleteTargets(host._promptContextCatalog?.profiles);
            if (!targets.length) {
                const empty = document.createElement("div");
                // An empty menu section reads as broken; say what is missing.
                empty.textContent = "This project has no custom formats.";
                empty.style.cssText = `padding:3px 2px;font:9px system-ui;color:${COLORS.textDim};`;
                menu.appendChild(empty);
            }
            for (const target of targets) {
                const row = document.createElement("div");
                row.dataset.promptFormatDeleteRow = target.key;
                row.style.cssText = "display:flex;align-items:center;gap:6px;min-width:0;";
                const label = document.createElement("span");
                label.textContent = target.name;
                label.style.cssText = `flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:10px system-ui;color:${COLORS.text};`;
                label.title = `${target.name} (${target.key})`;
                const removeOne = makeBtn("Delete", "", "danger");
                removeOne.style.flex = "0 0 auto";
                setButtonDisabled(removeOne, !target.deletable);
                removeOne.title = target.deletable
                    ? `Delete ${target.name} (${target.key})`
                    : target.reason;
                removeOne.addEventListener("click", async () => {
                    if (!window.confirm(`Delete prompt format ${target.name} (${target.key})?`)) return;
                    try {
                        // Targeted and identity-checked, like a recipe delete.
                        // The old whole-list PUT deleted by omission, so a
                        // format created in another window — absent from this
                        // browser's copy — was destroyed alongside the target.
                        await host._mutateReferences?.([{
                            type: "delete_prompt_context_profile",
                            profile_key: target.key,
                            expected: {
                                profile_id: target.profile_id,
                                version: target.version,
                                name: target.name,
                            },
                        }], "delete prompt format");
                        menu.remove();
                        notifySuccess(`Deleted prompt format ${target.name}.`,
                            { source: "prompt-profile-delete" });
                        render();
                    } catch (error) {
                        notifyWarning(error?.message || "Prompt format is still in use.",
                            { source: "prompt-profile-delete-refused" });
                    }
                });
                row.append(label, removeOne);
                menu.appendChild(row);
            }
            menuWrap.appendChild(menu);
            // Dismiss on outside pointer or Escape. Deferred one tick so the
            // click that opened the menu does not immediately close it. Escape
            // goes through keyboard ownership rather than a document listener,
            // which window capture beats — this panel's own consumer would
            // otherwise close the whole Prompt tool with the menu still up.
            let releaseMenuEscape = () => {};
            const dismiss = (event) => {
                if (event?.type === "pointerdown"
                        && (menu.contains(event.target) || menuButton === event.target)) return;
                menu.remove();
                releaseMenuEscape();
                document.removeEventListener("pointerdown", dismiss, true);
            };
            setTimeout(() => {
                document.addEventListener("pointerdown", dismiss, true);
                releaseMenuEscape = registerKeyboardConsumer({
                    id: `sonder-prompt-format-menu-${Date.now().toString(36)}`,
                    priority: KEY_PRIORITY.OVERLAY,
                    keydown: (event) => {
                        if (event.key !== "Escape") return false;
                        dismiss();
                        return true;
                    },
                });
            }, 0);
        });
        menuWrap.appendChild(menuButton);
        actionRow.append(profileRow, saveAs, createNew, menuWrap);
        card.insertBefore(actionRow, card.firstChild);
        const commit = async (operations, label, history = {}, lifecycleToken = null) => {
            const supportsLifecycle = typeof host._beginSceneHistoryLifecycle === "function";
            const ownsLifecycle = !lifecycleToken;
            const token = lifecycleToken || host._beginSceneHistoryLifecycle?.(label);
            if (supportsLifecycle && !token) return false;
            try {
                const undoEntry = host._pushUndo?.(label, { ...history, pending: true });
                try {
                    await host._runSceneMutation(operations, {
                        key: `scene:${host.activeSceneId}:prompt-context:${Date.now()}`,
                        label, coalesce: false, refreshScenes: false,
                        // These operations may carry complete setup/recipe arrays.
                        // Replan after a conflict instead of replaying stale arrays.
                        retryOnConflict: false,
                    });
                } catch (error) {
                    if (typeof host._discardUndoEntry === "function") {
                        host._discardUndoEntry(undoEntry);
                    } else {
                        host._discardLastUndo?.(label);
                    }
                    notifyWarning(error?.message || "Prompt Context change was refused.",
                        { source: "prompt-context-refused" });
                    return false;
                }
                host._commitUndoEntry?.(undoEntry);
                try {
                    const refreshApplied = await host._fetchScenes?.({
                        ignoreMutationGate: true,
                        reason: "prompt_context",
                        sceneHistoryLifecycleToken: token,
                    });
                    if (refreshApplied === false) {
                        throw new Error("Prompt Context saved, but its scene refresh did not apply.");
                    }
                    render();
                } catch (error) {
                    // The durable mutation and its history already committed.
                    // Refresh failure may leave this surface stale, but must never
                    // invoke cross-resource compensation against saved state.
                    notifyWarning(error?.message || "Prompt Context saved, but refresh failed.",
                        { source: "prompt-context-refresh-failed" });
                }
                return true;
            } finally {
                if (ownsLifecycle && token) host._endSceneHistoryLifecycle?.(token);
            }
        };
        profile.addEventListener("change", () => commit([{
            type: "update_scene_fields",
            fields: { prompt_context_profile_id: profile.value },
        }], "change prompt context profile"));

        const setups = Array.isArray(scene.minimax_h3_conditioning_setups)
            ? structuredClone(scene.minimax_h3_conditioning_setups) : [];
        const active = setups.find((value) => value.setup_id === scene.active_minimax_h3_setup_id)
            || (setups.length === 1 ? setups[0] : null);
        const uid = () => globalThis.crypto?.randomUUID?.().replaceAll("-", "")
            || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
        const templateId = host._channelTemplate().id;

        if (templateId === "minimax_h3_base") {
            const controls = document.createElement("div");
            controls.style.cssText = "display:flex;gap:6px;align-items:center;flex-wrap:wrap;";
            const task = document.createElement("select"); task.style.cssText = chromeInputCss();
            for (const value of ["T2VA", "I2VA", "FL2VA", "L2VA"]) {
                const option = document.createElement("option"); option.value = value;
                option.textContent = value; task.appendChild(option);
            }
            task.value = active?.mode === "base" ? active.task_mode || "T2VA" : "T2VA";
            const guideSelect = (label, selected) => {
                const select = document.createElement("select"); select.style.cssText = chromeInputCss();
                const none = document.createElement("option"); none.value = "";
                none.textContent = `${label}: none`; select.appendChild(none);
                for (const guide of scene.guide_frames || []) {
                    if (guide.muted) continue;
                    const option = document.createElement("option"); option.value = guide.guide_id || "";
                    option.textContent = `${label}: ${guide.frame_index === -1 ? "last" : `${guide.frame_index}f`}`;
                    select.appendChild(option);
                }
                select.value = selected || ""; return select;
            };
            const first = guideSelect("First frame", active?.mode === "base" ? active.first_guide_id : "");
            const last = guideSelect("Last frame", active?.mode === "base" ? active.last_guide_id : "");
            const save = makeBtn("Use H3 Base setup", "Bind task mode and physical Guide anchors", "primary");
            save.addEventListener("click", () => {
                const setup = { ...(active?.mode === "base" ? active : {}),
                    schema: "minimax_h3_setup_v1", setup_id: active?.mode === "base" ? active.setup_id : uid(),
                    name: "MiniMax H3 Base Setup", mode: "base", task_mode: task.value,
                    first_guide_id: first.value, last_guide_id: last.value };
                commit([{ type: "update_scene_fields", fields: {
                    prompt_context_profile_config: { task_mode: task.value },
                    minimax_h3_conditioning_setups: [...setups.filter((value) => value.setup_id !== setup.setup_id), setup],
                    active_minimax_h3_setup_id: setup.setup_id,
                } }], "configure MiniMax H3 Base");
            });
            controls.append(task, first, last, save); card.appendChild(controls);
        }
        const candidate = currentCandidatePayload() || {};
        lastIdentityCandidateSignature = identityCandidateSignature(candidate);
        const profileKey = profile.value || defaultProfile;
        const identityProfile = currentPromptProfile(profileKey);
        const sameReferenceOwner = (attachment, owner) => {
            if (attachment?.kind !== "reference") return false;
            if (owner?.type === "identity") {
                return (attachment.source?.semantic_unit_ids || []).map(String)
                    .includes(String(owner.identityId || ""));
            }
            const sourceKey = String(owner?.declaration?.source_key || "");
            return sourceKey && (attachment.source?.[sourceKey] || []).map(String)
                .includes(String(owner?.memberId || ""));
        };
        const saveSemanticUnitChange = (change, label, options = {}) => {
            if (typeof host._applyPromptIdentityChange === "function") {
                return host._applyPromptIdentityChange(change, label, options);
            }
            return host._savePromptSemanticUnits?.(applyPromptIdentityChange(
                host._promptSemanticUnits || [], change), label, options);
        };
        const attachReference = async (owner, target, overrides = null) => {
            const supportsLifecycle = typeof host._beginSceneHistoryLifecycle === "function";
            const lifecycleToken = host._beginSceneHistoryLifecycle?.("attach prompt Reference");
            if (supportsLifecycle && !lifecycleToken) return false;
            try {
                const sectionIndex = Number(target?.sectionIndex);
            const section = target?.scope === "section"
                ? scene.prompt_sections?.[sectionIndex] : null;
            const current = normalizePromptAttachments(section
                ? section.attachments : scene.global_attachments);
            const alreadyAttached = current.some((attachment) =>
                sameReferenceOwner(attachment, owner));
            // Validate/build before crossing either durable handle mutation
            // seam. A profile with no Reference parts must not leave a newly
            // stored handle behind when attachment construction refuses.
            const attachment = alreadyAttached
                ? null : promptReferenceAttachment(owner, identityProfile);
            if (attachment && overrides && typeof overrides === "object"
                    && Object.keys(overrides).length) {
                attachment.config = { ...attachment.config, overrides };
            }
            let materializedHandle = "";
            let history = {};
            let identityMaterialization = null;
            if (owner?.type === "physical" && !owner.storedHandle) {
                const physicalMaterialization = await host._materializeReferenceMemberHandle?.({
                    referenceId: owner.referenceId, memberId: owner.memberId,
                    suggestion: owner.handle, expectedHandle: "",
                });
                materializedHandle = String(physicalMaterialization?.handle || "");
                if (!materializedHandle) {
                    throw new Error("The physical Reference handle could not be materialized.");
                }
                if (physicalMaterialization?.ownsHandle) {
                    history = {
                        referenceOperations: [{
                            type: "update_member", reference_id: owner.referenceId,
                            member_id: owner.memberId, fields: { handle: "" },
                            expected: { handle: materializedHandle },
                        }],
                        inverseReferenceOperations: [{
                            type: "update_member", reference_id: owner.referenceId,
                            member_id: owner.memberId, fields: { handle: materializedHandle },
                            expected: { handle: "" },
                        }],
                    };
                }
            } else if (owner?.type === "identity") {
                identityMaterialization = await materializePromptIdentityOwner(
                    owner, host._promptSemanticUnits || [], saveSemanticUnitChange,
                    { recordUndo: false });
                owner = identityMaterialization.owner;
                materializedHandle = owner.storedHandle;
                if (identityMaterialization.next) {
                    // Target-owned expected-state changes preserve unrelated
                    // identities and formats when Undo/Redo rebases onto a
                    // newer project version.
                    history = {
                        ...history,
                        promptIdentityChange: {
                            type: "upsert",
                            value: identityMaterialization.previous,
                            expected: identityMaterialization.next,
                        },
                        inversePromptIdentityChange: {
                            type: "upsert",
                            value: identityMaterialization.next,
                            expected: identityMaterialization.previous,
                        },
                    };
                }
            }
            if (alreadyAttached) {
                // The durable handle materialization is still a user-visible
                // mutation even when the target already owns the attachment.
                // Record a same-scene snapshot so one Undo removes the handle.
                if (history.referenceOperations?.length) {
                    host._pushReferenceUndo?.("materialize prompt Reference handle",
                        history.referenceOperations, history.inverseReferenceOperations);
                }
                if (history.promptIdentityChange) {
                    host._pushPromptIdentityUndo?.("materialize prompt identity handle",
                        history.promptIdentityChange, history.inversePromptIdentityChange);
                }
                notifySuccess(`@${materializedHandle || owner.handle || "Reference"} is already attached here.`,
                    { source: "prompt-reference-reused" });
                return true;
            }
            // Overrides authored in the Attach dialog ride the SAME scene batch
            // as the attachment, so one Undo removes both — and, on the
            // first-use path, the materialized handle with them.
            const next = [...current, attachment];
            const operations = section ? [{
                type: "update_prompt_section", index: sectionIndex,
                expected: { start_frame: section.start_frame, end_frame: section.end_frame },
                fields: { attachments: next },
            }] : [{
                type: "update_scene_fields", fields: { global_attachments: next },
            }];
            const label = "attach prompt Reference";
            const committed = await commit(operations, label, history, lifecycleToken);
            if (!committed && history.referenceOperations?.length) {
                await rollbackPromptPhysicalAttachment(host, history);
            }
            if (!committed && history.promptIdentityChange) {
                await rollbackPromptIdentityAttachment(host, history);
            }
            if (!committed) throw new Error("The prompt Reference attachment was refused.");
            notifySuccess(`Attached @${materializedHandle || owner.handle || "Reference"}.`,
                { source: "prompt-reference-attached" });
            return true;
            } finally {
                if (lifecycleToken) host._endSceneHistoryLifecycle?.(lifecycleToken);
            }
        };
        identityPanelCleanup = mountPromptIdentityPanel(card, {
            candidate,
            scene,
            profile: identityProfile,
            catalog: host._promptContextCatalog || {},
            references: host._references || [],
            assets: host._allProjectAssetsForGallery?.() || [],
            semanticUnits: host._promptSemanticUnits || [],
            projectKey: host._projectDirName?.() || host.projectId || "project",
            scenes: (host.scenes || []).length
                ? host.scenes : [host.activeScene].filter(Boolean),
            saveSemanticUnitChange,
            mutateReferences: (operations, label) =>
                host._mutateReferences?.(operations, label),
            attachReference,
            assetPreviewUrl: (asset) => host._referenceAssetPreviewUrl?.(asset),
            confirm: (message) => globalThis.confirm?.(message) ?? false,
            onRefresh: render,
            onModalStateChange: identityRefreshGate.setOpen,
            onError: (error) => notifyWarning(
                error?.message || "Prompt identity change was refused.",
                { source: "prompt-identity-refused" }),
        });
        bodyEl.appendChild(card);
    };

    renderNow = () => {
        dormancyRefreshers = [];
        if (!mounted) return;
        identityPanelCleanup();
        identityPanelCleanup = () => {};
        for (const editor of body.querySelectorAll("[data-sonder-prompt-box='1']")) {
            editor._sonderPromptContextMenuCleanup?.();
        }
        body.textContent = "";
        const scene = host.activeScene;
        if (!scene) {
            body.appendChild(sectionTitle("No active scene"));
            renderDiagnostics(null);
            return;
        }
        const sectionsLocked = host._isPromptTrackLocked();
        const globalLocked = host._isGlobalPromptTrackLocked();

        renderContextSettings(body, scene);

        // ── Global prompt (auto-commits on Enter/blur; no Save button) ─
        body.appendChild(sectionTitle(`Global Prompt (always-on)${globalLocked ? " — locked" : ""}`));
        // The global prompt is per channel too: under a named-field template a
        // single blob has nowhere correct to go, and MiniMax wants its style
        // opening at the head of the description rather than ahead of the first
        // field name. Which sections take each channel is a per-SECTION choice,
        // shown on the section cards below.
        const globalTemplate = host._channelTemplate();
        // One box per channel, or a single box when the template turns
        // per-channel globals off.
        const globalKeys = globalChannelKeys(globalTemplate);
        const globalChannels = normalizeChannels(
            scene.global_channels, scene.prompt, globalKeys);
        const globalDocuments = { ...(scene.global_channel_docs || {}) };
        let globalAttachments = normalizePromptAttachments(scene.global_attachments);
        const draftGlobalSceneSnapshot = () => sceneWithDraftGlobal(scene, {
            attachments: globalAttachments,
            channelDocs: globalDocuments,
            channels: Object.fromEntries(globalKeys.map((channelKey) => [
                channelKey, promptDocumentText(
                    globalDocuments[channelKey] || normalizePromptDocument(
                        null, globalChannels[channelKey] || "")).trim(),
            ])),
        });
        const globalInputs = {};
        const globalRow = document.createElement("div");
        globalRow.style.cssText = `
            display:grid; gap:6px; align-items:start;
            grid-template-columns: ${globalKeys.map(() => "1fr").join(" ")};
        `;
        for (const key of globalKeys) {
            const channel = (globalTemplate.channels || []).find((c) => c.key === key) || { key };
            const column = document.createElement("div");
            column.style.cssText = "display:flex; flex-direction:column; gap:2px; min-width:0;";
            // Which sections take this text is decided per section now, so the
            // global row is text only.
            const head = document.createElement("div");
            head.style.cssText = `font-size:9px; color:${COLORS.textDim}; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;`;
            head.textContent = key;
            head.title = channel.description
                || `Global ${key}. Each section chooses whether to take it.`;

            const configureGlobalAttachment = (attachment, anchoredChannels = [key]) => configurePromptAttachment(attachment, {
                scene: { ...(scene || {}), _context_channel_keys: globalKeys,
                    _context_reference_frame_threshold: host._referenceFrameThreshold || 0 },
                references: host._references || [], channelKey: key,
                semanticUnits: host._promptSemanticUnits || [],
                profileId: scene.prompt_context_profile_id
                    || globalTemplate.default_context_profile || "generic@1",
                scope: "global",
                anchoredChannels,
                profile: currentPromptProfile(),
                placementPhases: promptPlacementPhases(),
                managedSpeakerSubjectIds: currentManagedSpeakerSubjectIds(),
                ordinalManifest: currentCandidatePayload()?.ordinal_manifest || {},
                candidate: currentCandidatePayload(),
            });
            const input = createPromptDocumentEditor({
                document: normalizePromptDocument(
                    globalDocuments[key], globalChannels[key] || ""),
                attachments: globalAttachments,
                previews: currentCandidatePayload()?.attachment_previews || {},
                attachmentLabelFor,
                attachmentContext: { scene, template: globalTemplate },
                disabled: globalLocked,
                ariaLabel: channel.description || `Global ${key}`,
                onChange: ({ document: nextDocument, attachments, reason }) => {
                    globalDocuments[key] = nextDocument;
                    globalAttachments = attachments;
                    for (const sibling of Object.values(globalInputs)) {
                        if (sibling === input) continue;
                        if (reason === "attachment") {
                            sibling.recordSharedAttachmentTransaction?.(globalAttachments);
                        } else {
                            sibling.syncPromptAttachments?.(globalAttachments);
                        }
                    }
                    const snapshot = draftGlobalSceneSnapshot();
                    host._previewPromptContextCandidate?.({
                        global_channels: snapshot.global_channels,
                        global_channel_docs: snapshot.global_channel_docs,
                        global_attachments: snapshot.global_attachments,
                    });
                    if (["attachment", "history"].includes(reason)) {
                        queueMicrotask(() => {
                            renderGlobalScope();
                            commitGlobal().catch(() => {});
                        });
                    }
                },
                onActivateAttachment: async (attachment) => {
                    const configured = await configureGlobalAttachment(attachment);
                    if (configured) input.replaceAttachment(configured);
                },
            });
            globalInputs[key] = input;
            input.dataset.sonderPromptGlobalChannel = key;
            applyBoxHeight(host, input, "panelGlobalBoxHeight", 72);
            registerPromptBoxGuard(input, () => globalChannels[key] || "");
            const commitGlobal = async () => {
                if (globalLocked || guard.suppressBlurCommit) return;
                globalChannels[key] = input.value;
                await host._updateSceneGlobalContext(
                    globalChannels, globalDocuments, globalAttachments);
            };
            input.addOwnedKeyHandler?.((e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    commitGlobal().catch(() => {});
                    return true;
                }
                return false;
            });
            input.addEventListener("blur", (event) => {
                if (event.relatedTarget?.closest?.("[data-sonder-prompt-context-modal='1'],[data-sonder-prompt-context-menu='1']")) return;
                commitGlobal().catch(() => {});
            });
            installPromptContextMenu({
                editor: input,
                allowedKinds: ["reference", "custom"],
                onInserted: ({ type }) => type === "writing_aid" ? commitGlobal() : null,
                onCreate: configureGlobalAttachment,
                profile: host._resolvedPromptContextProfile?.(),
                referenceContext: () => ({
                    scene: { ...(scene || {}),
                        _context_reference_frame_threshold:
                            host._referenceFrameThreshold || 0 },
                    references: host._references || [],
                    semanticUnits: host._promptSemanticUnits || [],
                    profileId: scene.prompt_context_profile_id
                        || globalTemplate.default_context_profile || "generic@1",
                    scope: "global",
                    resolvedProfile: currentPromptProfile(),
                }),
                writingAids: host._promptContextWritingAids?.(
                    scene.prompt_context_profile_id
                        || globalTemplate.default_context_profile || "generic@1") || [],
                channelKey: key,
            });
            let projectionHosts = null;
            let projectionSignature = null;
            const refreshProjection = (payload = currentCandidatePayload()) => {
                const signature = attachmentChannelProjectionSignature({
                    channelKey: key, attachments: globalAttachments,
                    candidate: payload, attachmentLabelFor,
                    disabled: globalLocked,
                });
                if (projectionHosts && signature === projectionSignature) return;
                const next = createAttachmentChannelProjections({
                    channelKey: key,
                    attachments: globalAttachments,
                    candidate: payload,
                    attachmentLabelFor,
                    onActivate: async (attachment) => {
                        const configured = await configureGlobalAttachment(
                            attachment, promptAttachmentAnchoredChannels(
                                globalDocuments, attachment.attachment_id));
                        if (configured) input.replaceAttachment(configured);
                    },
                    onSetCapabilityEnabled: (attachment, projection, enabled) => {
                        input.replaceAttachment(setPromptAttachmentCapabilityEnabled(
                            attachment, projection, enabled));
                    },
                    onLinkedSuppressionWarning: ({ linkedCount }) => {
                        notifyWarning(
                            `Suppressed here; ${linkedCount} linked chip${linkedCount === 1 ? " can" : "s can"} still emit. Suppress there too, or Unlink first.`,
                            { source: "prompt-linked-suppression" });
                    },
                });
                next.beforeHost._sonderRefreshProjection = refreshProjection;
                projectionHosts?.beforeHost.replaceWith(next.beforeHost);
                projectionHosts?.afterHost.replaceWith(next.afterHost);
                projectionHosts = next;
                projectionSignature = signature;
            };
            refreshProjection();
            column.append(head, createPromptProjectionBox(
                input, projectionHosts.beforeHost, projectionHosts.afterHost));
            if (key === globalKeys[0]) appendPromptContextHint(host, column);
            globalRow.appendChild(column);
        }
        body.appendChild(globalRow);
        const globalScopeHost = document.createElement("div");
        const globalAnchorIds = () => new Set(Object.values(globalDocuments)
            .flatMap((value) => normalizePromptDocument(value).nodes)
            .filter((node) => node.type === "attachment")
            .map((node) => node.attachment_id));
        const configureGlobalScope = (attachment) => configurePromptAttachment(attachment, {
            scene: { ...(scene || {}), _context_channel_keys: globalKeys,
                _context_reference_frame_threshold: host._referenceFrameThreshold || 0 },
            references: host._references || [], channelKey: globalKeys[0] || "visual",
            semanticUnits: host._promptSemanticUnits || [],
            profileId: scene.prompt_context_profile_id
                || globalTemplate.default_context_profile || "generic@1",
            scope: "global",
            anchoredChannels: [],
            profile: currentPromptProfile(),
            placementPhases: promptPlacementPhases(),
            managedSpeakerSubjectIds: currentManagedSpeakerSubjectIds(),
            ordinalManifest: currentCandidatePayload()?.ordinal_manifest || {},
            candidate: currentCandidatePayload(),
        });
        const commitGlobalScope = async () => {
            const nextChannels = Object.fromEntries(globalKeys.map((key) => [
                key, promptDocumentText(globalDocuments[key]).trim(),
            ]));
            await host._updateSceneGlobalContext(
                nextChannels, globalDocuments, globalAttachments);
        };
        const renderGlobalScope = () => {
            const anchored = globalAnchorIds();
            globalScopeHost.replaceChildren(createScopeChipRow({
                label: "Scene context",
                attachments: globalAttachments.filter((value) => !anchored.has(value.attachment_id)),
                previews: currentCandidatePayload()?.attachment_previews || {},
                attachmentLabelFor,
                disabled: globalLocked,
                profile: host._resolvedPromptContextProfile?.(),
                allowedKinds: ["reference", "custom"],
                onAdd: async (kind) => {
                    const configured = await configureGlobalScope({ kind });
                    if (!configured) return;
                    globalInputs[globalKeys[0]]?.transactPromptAttachments?.(
                        [...globalAttachments, configured]);
                },
                onActivate: async (attachment) => {
                    const configured = await configureGlobalScope(attachment);
                    if (!configured) return;
                    globalInputs[globalKeys[0]]?.transactPromptAttachments?.(
                        globalAttachments.map((value) =>
                            value.attachment_id === configured.attachment_id ? configured : value));
                },
                onRemove: async (attachment) => {
                    globalInputs[globalKeys[0]]?.transactPromptAttachments?.(
                        globalAttachments.filter((value) =>
                            value.attachment_id !== attachment.attachment_id));
                },
            }));
        };
        renderGlobalScope();
        body.appendChild(globalScopeHost);
        body.appendChild(makeHeightGrip(host, body, "panelGlobalBoxHeight", { label: "Drag to resize the global prompt box (persists)" }));

        // ── Mode toggle: Structured | Writing (sticky browser-local) ──
        const mode = host._settings?.prompts?.panelMode === "writing" ? "writing" : "structured";
        const modeRow = document.createElement("div");
        modeRow.style.cssText = "display:flex; gap:4px; align-items:center;";
        for (const [value, label] of [["structured", "Structured"], ["writing", "Writing"]]) {
            const btn = makeBtn(label, value === "writing"
                ? "Narrative drafting: write freely, split with --- markers, allocate lengths, Apply"
                : "Per-section editing: ranges, channels, queueing", mode === value ? "primary" : "subtle");
            btn.addEventListener("click", () => {
                if (mode === value) return;
                if (mode === "writing") saveWritingState();
                host._updateSettings({ prompts: { panelMode: value } });
                render();
            });
            modeRow.appendChild(btn);
        }
        body.appendChild(modeRow);

        if (mode === "writing") {
            renderWritingView(body, sectionsLocked);
            return;
        }

        // ── Sections ─────────────────────────────────────────────────
        const sectionsHeader = document.createElement("div");
        sectionsHeader.style.cssText = "display:flex; align-items:center; justify-content:space-between; gap:12px;";
        sectionsHeader.appendChild(sectionTitle(`Prompt Sections (${(scene.prompt_sections || []).length})${sectionsLocked ? " — track locked" : ""}`));
        // Sticky batch toggle for the per-row Queue buttons (browser-local;
        // Settings > Prompts holds the same key as the default)
        const batchToggle = document.createElement("label");
        batchToggle.style.cssText = `display:flex; gap:6px; align-items:center; font-size:10px; color:${COLORS.textMuted}; cursor:pointer;`;
        batchToggle.title = "On: queued sections auto-chunk through the batch path past the chunk budget. Off: one job for the whole range.";
        const batchCheck = document.createElement("input");
        batchCheck.type = "checkbox";
        batchCheck.checked = host._settings?.prompts?.queueSectionBatch !== false;
        batchCheck.addEventListener("change", () => {
            host._updateSettings({ prompts: { queueSectionBatch: batchCheck.checked } });
        });
        const batchText = document.createElement("span");
        batchText.textContent = "Queue as batch";
        batchToggle.append(batchCheck, batchText);
        sectionsHeader.appendChild(batchToggle);
        body.appendChild(sectionsHeader);
        const list = document.createElement("div");
        list.style.cssText = "display:flex; flex-direction:column; gap:6px;";
        const sections = scene.prompt_sections || [];
        if (!sections.length) {
            const empty = document.createElement("div");
            empty.style.cssText = `font-size:11px; color:${COLORS.textDim};`;
            empty.textContent = "No sections. Double-click the Prompt lane to create one.";
            list.appendChild(empty);
        }
        sections.forEach((section, idx) => {
            const template = host._channelTemplate();
            const channelKeys = templateChannelKeys(template);
            // Two rows, not one. Sharing a single grid row with the range
            // inputs and four fixed-width buttons squeezed six channels to
            // ~40px each — one character per line. The channels now own a
            // full-width row of their own beneath the controls.
            const card = document.createElement("div");
            card.style.cssText = `
                display:flex; flex-direction:column; gap:6px; padding:6px 8px;
                background:${COLORS.panel}; border:1px solid ${COLORS.promptBorder}; border-radius:4px;
            `;
            // The body field takes twice the width of the rest; at one channel
            // it simply fills the row.
            const wideKey = template.shot_marker_channel || channelKeys[0];
            const channelRow = document.createElement("div");
            channelRow.style.cssText = `
                display:grid; gap:6px; align-items:start;
                grid-template-columns: ${channelKeys.map((key) => (key === wideKey ? "2fr" : "1fr")).join(" ")};
            `;
            const startInput = smallInput({ value: String(section.start_frame ?? 0), numeric: true, width: "100%" });
            const endInput = smallInput({ value: String(section.end_frame ?? 0), numeric: true, width: "100%" });
            startInput.title = "Start frame";
            endInput.title = "End frame (exclusive)";
            const channels = normalizeChannels(section.channels, section.prompt, channelKeys);
            const channelInputs = {};
            const channelDocuments = { ...(section.channel_docs || {}) };
            let sectionAttachments = normalizePromptAttachments(section.attachments);
            const draftSceneSnapshot = () => sceneWithDraftSection(scene, {
                index: idx,
                promptId: section.prompt_id || "",
                startFrame: Number(startInput.value),
                endFrame: Number(endInput.value),
                section,
                channels: Object.fromEntries(channelKeys.map((channelKey) => [
                    channelKey, promptDocumentText(
                        channelDocuments[channelKey] || normalizePromptDocument(
                            null, channels[channelKey] || "")).trim(),
                ])),
                channelDocs: channelDocuments,
                attachments: sectionAttachments,
            });
            const allSectionAttachments = () => (draftSceneSnapshot().prompt_sections || [])
                .flatMap((value) => normalizePromptAttachments(value.attachments));
            const isLinkedAttachment = (attachment) => allSectionAttachments()
                .filter((value) => value.emission_group_id
                    === attachment.emission_group_id).length > 1;
            for (const channel of template.channels || []) {
                const key = channel.key;
                const column = document.createElement("div");
                column.style.cssText = "display:flex; flex-direction:column; gap:2px; min-width:0;";
                // A standing label, not just a placeholder: the placeholder
                // disappears once a box has text, which is precisely when a
                // six-channel row most needs to say which field is which.
                const caption = document.createElement("div");
                caption.style.cssText = `font-size:9px; color:${COLORS.textDim}; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;`;
                caption.textContent = key;
                caption.title = channel.description || `${key} channel`;
                const configureChannelAttachment = (attachment, anchoredChannels = [key]) => configurePromptAttachment(attachment, {
                    scene: { ...(scene || {}), _context_channel_keys: channelKeys,
                        _context_consumer_start: section.start_frame,
                        _context_consumer_end: section.end_frame,
                        _context_reference_frame_threshold: host._referenceFrameThreshold || 0 },
                    references: host._references || [], channelKey: key,
                    semanticUnits: host._promptSemanticUnits || [],
                    profileId: scene.prompt_context_profile_id
                        || template.default_context_profile || "generic@1",
                    scope: "section",
                    anchoredChannels,
                    profile: currentPromptProfile(),
                    placementPhases: promptPlacementPhases(),
                    managedSpeakerSubjectIds: currentManagedSpeakerSubjectIds(),
                    ordinalManifest: currentCandidatePayload()?.ordinal_manifest || {},
                    candidate: currentCandidatePayload(),
                });
                const input = createPromptDocumentEditor({
                    document: normalizePromptDocument(
                        channelDocuments[key], channels[key] || ""),
                    attachments: sectionAttachments,
                    previews: candidateForSection(section.prompt_id)
                        ?.attachment_previews || {},
                    attachmentLabelFor,
                    attachmentContext: { scene, template },
                    disabled: sectionsLocked,
                    ariaLabel: channel.description || `${key} channel`,
                    onChange: ({ document: nextDocument, attachments, reason }) => {
                        channelDocuments[key] = nextDocument;
                        sectionAttachments = attachments;
                        for (const sibling of Object.values(channelInputs)) {
                            if (sibling === input) continue;
                            if (reason === "attachment") {
                                sibling.recordSharedAttachmentTransaction?.(sectionAttachments);
                            } else {
                                sibling.syncPromptAttachments?.(sectionAttachments);
                            }
                        }
                        const candidateSections = draftSceneSnapshot().prompt_sections;
                        host._previewPromptContextCandidate?.({
                            prompt_sections: candidateSections,
                        });
                        if (["attachment", "history"].includes(reason)) {
                            queueMicrotask(() => {
                                renderScope();
                                commitChannels().catch(() => render());
                            });
                        }
                    },
                    onActivateAttachment: async (attachment) => {
                        const configured = await configureChannelAttachment(attachment);
                        if (!configured) return;
                        if (isLinkedAttachment(attachment)) {
                            await host._updateLinkedPromptAttachment?.(
                                attachment.attachment_id, configured);
                            render();
                            return;
                        }
                        input.replaceAttachment(configured);
                    },
                });
                applyBoxHeight(host, input, "panelChannelBoxHeight", 72);
                registerPromptBoxGuard(input, () => channels[key] || "");
                channelInputs[key] = input;
                installPromptContextMenu({
                    editor: input,
                    onInserted: ({ type }) => type === "writing_aid" ? commitChannels() : null,
                    onCreate: configureChannelAttachment,
                    profile: host._resolvedPromptContextProfile?.(),
                    referenceContext: () => ({
                        scene: { ...(scene || {}),
                            _context_consumer_start: section.start_frame,
                            _context_consumer_end: section.end_frame,
                            _context_reference_frame_threshold:
                                host._referenceFrameThreshold || 0 },
                        references: host._references || [],
                        semanticUnits: host._promptSemanticUnits || [],
                        profileId: scene.prompt_context_profile_id
                            || template.default_context_profile || "generic@1",
                        scope: "section",
                        resolvedProfile: currentPromptProfile(),
                    }),
                    writingAids: host._promptContextWritingAids?.(
                        scene.prompt_context_profile_id
                            || template.default_context_profile || "generic@1") || [],
                    channelKey: key,
                });
                let projectionHosts = null;
                let projectionSignature = null;
                const refreshProjection = (
                    payload = candidateForSection(section.prompt_id)) => {
                    const signature = attachmentChannelProjectionSignature({
                        channelKey: key, attachments: sectionAttachments,
                        candidate: payload, attachmentLabelFor,
                        disabled: sectionsLocked,
                    });
                    if (projectionHosts && signature === projectionSignature) return;
                    const next = createAttachmentChannelProjections({
                        channelKey: key,
                        attachments: sectionAttachments,
                        candidate: payload,
                        attachmentLabelFor,
                        onActivate: async (attachment) => {
                            const configured = await configureChannelAttachment(
                                attachment, promptAttachmentAnchoredChannels(
                                    channelDocuments, attachment.attachment_id));
                            if (!configured) return;
                            if (isLinkedAttachment(attachment)) {
                                await host._updateLinkedPromptAttachment?.(
                                    attachment.attachment_id, configured);
                                render();
                                return;
                            }
                            input.replaceAttachment(configured);
                        },
                        onSetCapabilityEnabled: (attachment, projection, enabled) => {
                            input.replaceAttachment(setPromptAttachmentCapabilityEnabled(
                                attachment, projection, enabled));
                        },
                        onLinkedSuppressionWarning: ({ linkedCount }) => {
                            notifyWarning(
                                `Suppressed here; ${linkedCount} linked chip${linkedCount === 1 ? " can" : "s can"} still emit. Suppress there too, or Unlink first.`,
                                { source: "prompt-linked-suppression" });
                        },
                    });
                    next.beforeHost._sonderRefreshProjection = refreshProjection;
                    projectionHosts?.beforeHost.replaceWith(next.beforeHost);
                    projectionHosts?.afterHost.replaceWith(next.afterHost);
                    projectionHosts = next;
                    projectionSignature = signature;
                };
                refreshProjection();
                column.append(caption, createPromptProjectionBox(
                    input, projectionHosts.beforeHost, projectionHosts.afterHost));
                channelRow.appendChild(column);
            }

            const commitRange = async () => {
                if (guard.suppressBlurCommit) return;
                const start = parseInt(startInput.value, 10);
                const end = parseInt(endInput.value, 10);
                if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
                    render();
                    return;
                }
                if (start === section.start_frame && end === section.end_frame) return;
                await host._updatePromptSection(idx, { start_frame: start, end_frame: end });
                render(); // indices may have re-sorted — rebuild rows
            };
            const commitChannels = async () => {
                if (guard.suppressBlurCommit) return;
                const next = {};
                for (const key of channelKeys) {
                    next[key] = channelInputs[key].value.trim();
                }
                const nextDocuments = Object.fromEntries(
                    channelKeys.map((key) => [key, channelInputs[key].promptDocument]));
                await host._updatePromptSection(idx, {
                    channels: next,
                    channel_docs: nextDocuments,
                    attachments: sectionAttachments,
                });
                // A channel-only save cannot reorder rows. Keep these mounted
                // editors alive so their shared attachment/caret/undo histories
                // remain authoritative across the completed backend write.
            };
            const scopeHost = document.createElement("div");
            const configureScope = (attachment) => configurePromptAttachment(attachment, {
                scene: { ...(scene || {}), _context_channel_keys: channelKeys,
                    _context_consumer_start: section.start_frame,
                    _context_consumer_end: section.end_frame,
                    _context_reference_frame_threshold: host._referenceFrameThreshold || 0 },
                references: host._references || [], channelKey: wideKey,
                semanticUnits: host._promptSemanticUnits || [],
                profileId: scene.prompt_context_profile_id
                    || template.default_context_profile || "generic@1",
                scope: "section",
                anchoredChannels: [],
                profile: currentPromptProfile(),
                placementPhases: promptPlacementPhases(),
                managedSpeakerSubjectIds: currentManagedSpeakerSubjectIds(),
                ordinalManifest: currentCandidatePayload()?.ordinal_manifest || {},
                candidate: currentCandidatePayload(),
            });
            const renderScope = () => {
                const anchored = new Set(Object.values(channelDocuments)
                    .flatMap((value) => normalizePromptDocument(value).nodes)
                    .filter((node) => node.type === "attachment")
                    .map((node) => node.attachment_id));
                scopeHost.replaceChildren(createScopeChipRow({
                    label: `Section ${idx + 1} context`,
                    attachments: sectionAttachments.filter((value) => !anchored.has(value.attachment_id)),
                    previews: candidateForSection(section.prompt_id)
                        ?.attachment_previews || {},
                    attachmentLabelFor,
                    disabled: sectionsLocked,
                    profile: host._resolvedPromptContextProfile?.(),
                    allowedKinds: template.shot_marker_channel
                        ? ["shot", "timestamp", "prompt_link_scope", "reference", "custom"]
                        : ["prompt_link_scope", "reference", "custom"],
                    reusableAttachments: allSectionAttachments(),
                    allSceneAttachments: allSectionAttachments(),
                    reuseContext: { scene: draftSceneSnapshot(), template },
                    onReuse: async (configured) => {
                        channelInputs[wideKey]?.transactPromptAttachments?.(
                            [...sectionAttachments, configured]);
                    },
                    onAdd: async (kind) => {
                        const configured = await configureScope({ kind });
                        if (!configured) return;
                        channelInputs[wideKey]?.transactPromptAttachments?.(
                            [...sectionAttachments, configured]);
                    },
                    onActivate: async (attachment) => {
                        const configured = await configureScope(attachment);
                        if (!configured) return;
                        if (isLinkedAttachment(attachment)) {
                            await host._updateLinkedPromptAttachment?.(
                                attachment.attachment_id, configured);
                            render();
                            return;
                        }
                        channelInputs[wideKey]?.transactPromptAttachments?.(
                            sectionAttachments.map((value) =>
                                value.attachment_id === configured.attachment_id ? configured : value));
                    },
                    onUnlink: async (configured) => {
                        channelInputs[wideKey]?.transactPromptAttachments?.(
                            sectionAttachments.map((value) =>
                                value.attachment_id === configured.attachment_id ? configured : value));
                    },
                    onConvertPromptLinkCopy: async (attachment) => {
                        const source = (scene.prompt_sections || []).find((value) =>
                            String(value?.prompt_id || "")
                                === String(attachment?.source?.prompt_id || ""));
                        if (!source) {
                            notifyWarning("The linked source section no longer exists.",
                                { source: "prompt-link-copy-refused" });
                            return;
                        }
                        const selectedChannels = (attachment?.source?.channel_keys || [])
                            .map(String).filter(Boolean);
                        const copyChannels = selectedChannels.length
                            ? selectedChannels : channelKeys;
                        const nextDocuments = structuredClone(channelDocuments);
                        for (const channelKey of copyChannels) {
                            if (!channelKeys.includes(channelKey)) continue;
                            const sourceDocument = normalizePromptDocument(
                                source.channel_docs?.[channelKey],
                                source.channels?.[channelKey] || "");
                            const text = promptDocumentText(sourceDocument).trim();
                            if (!text) continue;
                            const targetDocument = normalizePromptDocument(
                                nextDocuments[channelKey], channels[channelKey] || "");
                            targetDocument.nodes.unshift({
                                type: "text", node_id: uid(), text: `${text} `,
                            });
                            nextDocuments[channelKey] = targetDocument;
                        }
                        const nextAttachments = sectionAttachments.filter((value) =>
                            value.attachment_id !== attachment.attachment_id);
                        await host._updatePromptSection(idx, {
                            channel_docs: nextDocuments,
                            channels: Object.fromEntries(channelKeys.map((channelKey) => [
                                channelKey, promptDocumentText(nextDocuments[channelKey]).trim(),
                            ])),
                            attachments: nextAttachments,
                        });
                        render();
                    },
                    onRemove: async (attachment) => {
                        channelInputs[wideKey]?.transactPromptAttachments?.(
                            sectionAttachments.filter((value) =>
                                value.attachment_id !== attachment.attachment_id));
                    },
                }));
            };
            renderScope();
            const onEnterBlur = (input, commit) => {
                const handler = (e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        commit().catch(() => render());
                        return true;
                    }
                    return false;
                };
                if (input.addOwnedKeyHandler) input.addOwnedKeyHandler(handler);
                else input.addEventListener("keydown", (e) => {
                    handler(e);
                    e.stopPropagation();
                });
                input.addEventListener("blur", (event) => {
                if (event.relatedTarget?.closest?.("[data-sonder-prompt-context-modal='1'],[data-sonder-prompt-context-menu='1']")) return;
                    commit().catch(() => render());
                });
            };
            for (const el of [startInput, endInput]) { el.disabled = sectionsLocked; onEnterBlur(el, commitRange); }
            for (const key of channelKeys) {
                const el = channelInputs[key];
                el.disabled = sectionsLocked;
                onEnterBlur(el, commitChannels);
            }

            const selectBtn = makeBtn("Select", "Set selection to this section");
            selectBtn.addEventListener("click", () => {
                host._setSelectionToFrameRange(section.start_frame || 0, section.end_frame || 0);
            });
            const queueBtn = makeBtn("Queue", "Queue this prompt section (Batch toggle picks chunked vs single job)");
            queueBtn.addEventListener("click", () => { host._queuePromptSection(section).catch(() => {}); });
            const queueAdvisory = document.createElement("span");
            queueAdvisory.dataset.sonderQueueAdvisoryCount = "1";
            queueAdvisory.style.cssText = `display:none;font-size:9px;color:${COLORS.warningText};white-space:nowrap;`;
            const addAfterBtn = makeBtn("+", "Insert a new section after this one (fills the gap to the next section)",
                "secondary", "Add prompt section after this section");
            addAfterBtn.addEventListener("click", async () => {
                if (sectionsLocked) return;
                const created = await host._addPromptSectionAfter(idx).catch(() => false);
                if (created) render();
            });
            const deleteBtn = makeBtn("✕", "Delete this section", "danger",
                "Delete prompt section");
            // One metric for both glyphs. They sit in a single action cell and
            // must read as a pair, so the size lives in a shared constant
            // rather than on whichever button was styled last.
            for (const glyph of [addAfterBtn, deleteBtn]) {
                glyph.style.cssText += SECTION_GLYPH_BUTTON_CSS;
                setButtonDisabled(glyph, sectionsLocked);
            }
            deleteBtn.addEventListener("click", async () => {
                if (sectionsLocked) return;
                if (!confirm("Delete this prompt section?")) return;
                await host._deletePromptSection(idx);
                render();
            });

            // The 1fr spacer keeps the buttons right-aligned now that the
            // channels no longer occupy the middle of the controls row. The
            // section actions are one atomic grid cell so advisory visibility
            // and narrow layouts cannot split + from ×.
            const spacer = document.createElement("div");
            const row = buildPromptSectionControlRow(
                [startInput, endInput, spacer, selectBtn, queueBtn, queueAdvisory],
                [addAfterBtn, deleteBtn]);
            // Between the controls and the channels, so it reads as a statement
            // about this section rather than about one channel.
            card.append(row, dormancyNotice(() => sectionDormancy(section.prompt_id)),
                channelRow, scopeHost);

            // Which scene-global channels this section takes. One checkbox per
            // global channel, so a template with global channels off shows
            // exactly one. Only channels that actually carry text are offered —
            // a checkbox for an empty global does nothing either way.
            const inheritKeys = globalKeys.filter((key) => (globalChannels[key] || "").trim());
            if (inheritKeys.length) {
                const inheritRow = document.createElement("div");
                inheritRow.style.cssText = `display:flex; gap:10px; align-items:center; flex-wrap:wrap; font-size:9px; color:${COLORS.textDim};`;
                const lead = document.createElement("span");
                lead.textContent = inheritKeys.length > 1 ? "Takes global:" : "Takes global";
                lead.title = "Scene-global text this section takes. Unticked, the"
                    + " global text is dropped from any render that only covers"
                    + " sections which opted out.";
                inheritRow.appendChild(lead);
                for (const key of inheritKeys) {
                    const wrap = document.createElement("label");
                    wrap.style.cssText = `display:flex; align-items:center; gap:3px;${sectionsLocked ? " opacity:0.45;" : ""}`;
                    wrap.title = `Take the scene-global ${key} in this section.`;
                    const box = document.createElement("input");
                    box.type = "checkbox";
                    box.checked = sectionInheritsGlobal(section, key);
                    box.disabled = sectionsLocked;
                    box.addEventListener("keydown", (e) => e.stopPropagation());
                    box.addEventListener("change", () => {
                        host._setSectionGlobalInherit(idx, key, box.checked)
                            .then(() => render())
                            .catch(() => render());
                    });
                    wrap.append(box);
                    if (inheritKeys.length > 1) {
                        const text = document.createElement("span");
                        text.textContent = key;
                        wrap.append(text);
                    }
                    inheritRow.appendChild(wrap);
                }
                card.appendChild(inheritRow);
            }
            list.appendChild(card);
        });
        body.appendChild(list);
        if (sections.length) {
            body.appendChild(makeHeightGrip(host, body, "panelChannelBoxHeight", { label: "Drag to resize all channel boxes (persists)" }));
        }

        const addSectionBtn = makeBtn("Add Section", "Create a section in the first free gap on the lane");
        addSectionBtn.disabled = sectionsLocked;
        addSectionBtn.addEventListener("click", async () => {
            if (sectionsLocked) return;
            const created = await host._addPromptSectionInFirstGap().catch(() => false);
            if (created) render();
        });
        body.appendChild(addSectionBtn);

        // ── Templates (browser-local library) ────────────────────────
        body.appendChild(sectionTitle("Prompt Templates (browser-local)"));
        const templateSaveRow = document.createElement("div");
        templateSaveRow.style.cssText = "display:flex; gap:6px; align-items:center;";
        const templateNameInput = smallInput({ placeholder: "Template name…" });
        const templateSaveBtn = makeBtn("Save Current as Template", "Snapshot the global prompt + sections into the browser-local library");
        templateSaveBtn.addEventListener("click", () => {
            if (!templateNameInput.value.trim()) return;
            host._savePromptTemplate(templateNameInput.value);
            templateNameInput.value = "";
            render();
        });
        templateSaveRow.append(templateNameInput, templateSaveBtn);
        body.appendChild(templateSaveRow);
        const templates = host._getPromptTemplates();
        if (templates.length) {
            const templateList = document.createElement("div");
            templateList.style.cssText = "display:flex; flex-direction:column; gap:4px;";
            for (const template of templates) {
                const row = document.createElement("div");
                row.style.cssText = `display:flex; gap:6px; align-items:center; font-size:11px; color:${COLORS.text};`;
                const label = document.createElement("span");
                label.style.cssText = "flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;";
                label.textContent = `${template.name} — ${template.sections.length} section(s)${template.global ? ", global" : ""}`;
                label.title = template.global || "";
                const applyBtn = makeBtn("Apply", "Replace the scene's prompt state with this template (undoable)");
                applyBtn.addEventListener("click", async () => {
                    await host._applyPromptSetup(template);
                    render();
                });
                const removeBtn = makeBtn("✕", "Delete this template", "danger",
                    "Delete prompt template");
                removeBtn.addEventListener("click", () => {
                    host._deletePromptTemplate(template.id);
                    render();
                });
                row.append(label, applyBtn, removeBtn);
                templateList.appendChild(row);
            }
            body.appendChild(templateList);
        }

        // ── History (Prompt Saver — captured at enqueue) ─────────────
        body.appendChild(sectionTitle("Prompt History (captured at enqueue)"));
        const historyWrap = document.createElement("div");
        historyWrap.style.cssText = "display:flex; flex-direction:column; gap:4px;";
        const historyBtn = makeBtn("Load History", "Fetch the project's enqueue-time prompt history");
        historyBtn.addEventListener("click", async () => {
            historyBtn.disabled = true;
            try {
                const entries = await host._fetchPromptHistory();
                historyWrap.textContent = "";
                if (!entries.length) {
                    const empty = document.createElement("div");
                    empty.style.cssText = `font-size:11px; color:${COLORS.textDim};`;
                    empty.textContent = "No history yet — entries are captured when jobs are queued.";
                    historyWrap.appendChild(empty);
                    return;
                }
                for (const entry of entries) {
                    const item = document.createElement("div");
                    item.style.cssText = "display:flex; flex-direction:column; gap:2px;";
                    const row = document.createElement("div");
                    row.style.cssText = `display:flex; gap:6px; align-items:center; font-size:11px; color:${COLORS.text};`;
                    const when = String(entry.ts || "").replace("T", " ").slice(0, 19);
                    const label = document.createElement("span");
                    label.style.cssText = "flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; cursor:pointer;";
                    const preview = entry.global || entry.sections?.[0]?.channels?.visual || "";
                    label.textContent = `${when} — ${(entry.sections || []).length} section(s) — ${preview}`;
                    label.title = "Click to expand the entry's global + per-section texts";
                    // Expandable detail: global + each section's composed text
                    const detail = document.createElement("div");
                    detail.style.cssText = `display:none; flex-direction:column; gap:2px; padding:4px 8px 6px 14px; border-left:2px solid ${COLORS.promptBorder};`;
                    const addDetailLine = (text) => {
                        const line = document.createElement("div");
                        line.textContent = text;
                        line.style.cssText = `font-size:10px; line-height:1.4; color:${COLORS.textDim}; word-break:break-word; white-space:pre-wrap;`;
                        detail.appendChild(line);
                    };
                    if (entry.global) addDetailLine(`Global: ${entry.global}`);
                    for (const s of entry.sections || []) {
                        const composed = composeSectionText(
                            normalizeChannels(s.channels, s.prompt,
                                              templateChannelKeys(host._channelTemplate())),
                            false, host._channelTemplate());
                        addDetailLine(`[${s.start_frame ?? 0}–${s.end_frame ?? 0}] ${composed || "(empty)"}`);
                    }
                    label.addEventListener("click", () => {
                        detail.style.display = detail.style.display === "none" ? "flex" : "none";
                    });
                    const applyBtn = makeBtn("Apply", "Replace the scene's prompt state with this entry (undoable)");
                    applyBtn.addEventListener("click", async () => {
                        await host._applyPromptSetup(entry);
                        render();
                    });
                    row.append(label, applyBtn);
                    item.append(row, detail);
                    historyWrap.appendChild(item);
                }
            } finally {
                historyBtn.disabled = false;
            }
        });
        body.appendChild(historyBtn);
        body.appendChild(historyWrap);

        // ── Relay payload preview ────────────────────────────────────
        body.appendChild(sectionTitle("PromptRelay Payload Preview"));
        const previewNote = document.createElement("div");
        previewNote.style.cssText = `font-size:10px; color:${COLORS.textDim};`;
        previewNote.textContent = "Full-scene window — execution payloads rebase tags/lengths to the render window, so this preview is structural, not literal.";
        body.appendChild(previewNote);
        const previewWrap = document.createElement("div");
        previewWrap.style.cssText = "display:flex; flex-direction:column; gap:4px;";
        const previewBtn = makeBtn("Refresh Preview", "Fetch the resolved payload from the backend");
        previewBtn.addEventListener("click", async () => {
            previewBtn.disabled = true;
            try {
                const payload = await host._fetchPromptPayload();
                previewWrap.textContent = "";
                if (!payload) {
                    const fail = document.createElement("div");
                    fail.style.cssText = `font-size:11px; color:${COLORS.dangerText};`;
                    fail.textContent = "Failed to fetch payload.";
                    previewWrap.appendChild(fail);
                    return;
                }
                const lines = [
                    ["source", payload.source],
                    ["global_prompt", payload.relay?.global_prompt ?? ""],
                    ["smart_prompt", payload.relay?.smart_prompt ?? ""],
                    ["local_prompts", payload.relay?.local_prompts ?? ""],
                    ["segment_lengths", payload.relay?.segment_lengths ?? ""],
                ];
                for (const [key, value] of lines) {
                    const line = document.createElement("div");
                    line.style.cssText = `font-size:10px; font-family:${FONT.mono || "monospace"}; color:${COLORS.text}; word-break:break-word; white-space:pre-wrap;`;
                    line.textContent = `${key}: ${value}`;
                    previewWrap.appendChild(line);
                }
            } finally {
                previewBtn.disabled = false;
            }
        });
        body.appendChild(previewBtn);
        body.appendChild(previewWrap);
        renderDiagnostics();
    };

    render();
    document.body.appendChild(backdrop);

    const handle = {
        element: backdrop,
        refresh: render,
        refreshDiagnostics: renderDiagnostics,
        // Projections ONLY. The scene-wide payload lands here, and it must not
        // reach diagnostics (blockers stay windowed-only) or `applyCandidate`
        // (Reference Prompting derives from a `setup_manifest` that payload
        // does not carry). Naming a narrower seam is what keeps that true when
        // someone later adds a consumer.
        refreshProjections: () => {
            if (!mounted) return;
            // Drop entries whose element left the document. `updateStrip`
            // runs on a 300ms typing debounce and each pass registers a fresh
            // row, so a list reset only by `renderNow` would grow all session
            // and pin every discarded node it ever built.
            dormancyRefreshers = dormancyRefreshers.filter(
                (entry) => backdrop.contains(entry.line));
            for (const entry of dormancyRefreshers) entry.apply();
            refreshWritingDecorations?.();
            for (const host_ of backdrop.querySelectorAll(
                "[data-sonder-prompt-projection-region='before']")) {
                host_._sonderRefreshProjection?.();
            }
        },
        // A landed candidate must reach every consumer that derives from it,
        // not only diagnostics and inline projections. Routed through `render`
        // so it passes `identityRefreshGate` — calling the identity section
        // directly would discard an open identity-editor draft, which is the
        // loss that gate exists to prevent.
        applyCandidate: (payload) => {
            if (!mounted) return false;
            // Contributions follow the compiled output, which changes on every
            // recompile — including the many that leave identity untouched and
            // return false below. Repaint them first, and directly.
            handle.refreshProjections();
            const next = identityCandidateSignature(payload);
            if (next === lastIdentityCandidateSignature) return false;
            lastIdentityCandidateSignature = next;
            render();
            return true;
        },
        cleanup: close,
        isMounted: () => mounted,
    };
    return handle;
}
