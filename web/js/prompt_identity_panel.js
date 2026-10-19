// Reference Prompting + Identity Prompting projection.
// The Prompt panel host owns state, networking, and durable commits. This
// module owns only the derived DOM/listeners and returns a cleanup callback.

import { CHROME_DIM_PLACEHOLDER_CLASS, EDITOR_COLORS as COLORS, chromeInputCss,
    installChromePlaceholderStyles, setButtonDisabled,
    setButtonVariant } from "./editor_theme.js";
import { createReferenceOverrideFieldset,
    normalizePromptAttachment, referencePromptDefaults } from "./prompt_context_chips.js";
import { TASK_TYPE_PREVIEW_LABEL, captureTaskTypePreview,
    createTaskTypeChoiceControl } from "./prompt_task_type_choices.js";
import { createDisclosureMemory } from "./disclosure_memory.js";
import { createModalDraftGuard } from "./modal_draft_guard.js";
import {
    PRIORITY as KEY_PRIORITY,
    register as registerKeyboardConsumer,
} from "./keyboard_ownership.js";
import {
    declaredFieldChoices,
    orderedReferenceDerived,
    referenceFieldDeclaration,
    referenceRoleChoices,
} from "./prompt_profile_declarations.js";
import {
    REFERENCE_VERDICT,
    REFERENCE_VERDICT_LABEL,
    countAttachedReferenceChips,
    deriveReferencePrompt,
    resolveReferenceVerdicts,
} from "./reference_resolution.js";

import {
    PROMPT_HANDLE_RULE,
    contributionValues,
    declarationGuidance,
    derivePromptHandleSuggestion,
    disclosureGroup,
    fieldRow,
    intentValues,
    legacyVoiceBindingRepair,
    makeButton,
    makeSelect,
    identityRoutingProjection,
    openIdentityEditor,
    promptIdentitySourceGroup,
    promptReferencePickerLabel,
    sanitizePromptHandle,
} from "./prompt_identity_editor.js";

// Re-exported so modules and tests that import them from here keep working.
export {
    PROMPT_HANDLE_RULE,
    identityRoutingProjection,
    derivePromptHandleSuggestion,
    legacyVoiceBindingRepair,
    promptIdentitySourceGroup,
    promptReferencePickerLabel,
    sanitizePromptHandle,
};

const uid = () => globalThis.crypto?.randomUUID?.().replaceAll("-", "")
    || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;

// The action column is `minmax(28px,auto)`, not a fixed 28px: identity rows put
// a one-glyph button there and physical rows put a labelled one. A fixed width
// sized for the glyph is what wrapped "Create identity" onto two lines while
// every automated check passed. Glyph rows still resolve to 28px, because those
// buttons carry `min-width:24px`.
const PROMPTING_GRID_COLUMNS = Object.freeze([
    "36px", "minmax(130px,.9fr)", "minmax(150px,1fr)", "auto", "auto", "minmax(28px,auto)",
]);
const PROMPTING_ROW_CSS = `display:grid;grid-template-columns:${PROMPTING_GRID_COLUMNS.join(" ")};gap:6px;align-items:center;padding:5px 6px;border:1px solid ${COLORS.border};border-radius:5px;`;
const PROMPTING_CELL_NAMES = Object.freeze([
    "thumbnail", "name", "status", "attach", "edit", "action",
]);

export function createModalRefreshGate(refresh, {
    schedule = (callback) => queueMicrotask(callback),
} = {}) {
    // Catalog/Reference refreshes are authoritative, but replacing the Prompt
    // panel DOM while this child modal is open would silently discard its
    // unsaved draft. Coalesce them and reconcile immediately after close.
    let modalOpen = false;
    let pending = false;
    return {
        request() {
            if (modalOpen) {
                pending = true;
                return false;
            }
            refresh();
            return true;
        },
        setOpen(open) {
            modalOpen = open === true;
            if (!modalOpen && pending) {
                pending = false;
                schedule(refresh);
            }
        },
        clear() {
            modalOpen = false;
            pending = false;
        },
    };
}

function stableIdentitySnapshot(value) {
    if (Array.isArray(value)) return value.map(stableIdentitySnapshot);
    if (!value || typeof value !== "object") return value;
    return Object.fromEntries(Object.keys(value).sort().map((key) => [
        key, stableIdentitySnapshot(value[key]),
    ]));
}

export function sameIdentitySnapshot(left, right) {
    return JSON.stringify(stableIdentitySnapshot(left ?? null))
        === JSON.stringify(stableIdentitySnapshot(right ?? null));
}

export function applyPromptIdentityChange(currentUnits = [], change = {}) {
    const current = Array.isArray(currentUnits) ? currentUnits : [];
    const type = String(change?.type || "");
    const value = change?.value && typeof change.value === "object"
        ? structuredClone(change.value) : null;
    const identityId = String(value?.semantic_unit_id
        || change?.semantic_unit_id || "");
    if (!identityId || !["upsert", "delete"].includes(type)) {
        throw new Error("Invalid prompt identity change.");
    }
    const index = current.findIndex((unit) =>
        String(unit?.semantic_unit_id || "") === identityId);
    const found = index >= 0 ? current[index] : null;
    const expected = Object.hasOwn(change, "expected") ? change.expected : undefined;
    if (expected === null && found) {
        const error = new Error("Prompt identity was created elsewhere. Reopen and try again.");
        error.code = "prompt_identity_created_elsewhere";
        throw error;
    }
    if (expected && !sameIdentitySnapshot(found, expected)) {
        const error = new Error("Prompt identity changed elsewhere. Reopen it before saving.");
        error.code = "prompt_identity_changed_elsewhere";
        throw error;
    }
    if (type === "delete") {
        if (!found) throw new Error("Prompt identity was already deleted.");
        return current.filter((_, unitIndex) => unitIndex !== index)
            .map((unit) => structuredClone(unit));
    }
    const nextValue = value;
    if (index < 0) nextValue.order = current.length;
    return index >= 0
        ? current.map((unit, unitIndex) => unitIndex === index
            ? nextValue : structuredClone(unit))
        : [...current.map((unit) => structuredClone(unit)), nextValue];
}

function buildPromptingRow(kind, cells) {
    if (PROMPTING_GRID_COLUMNS.length !== PROMPTING_CELL_NAMES.length
            || !Array.isArray(cells) || cells.length !== PROMPTING_CELL_NAMES.length) {
        throw new Error("Prompting rows require exactly six named cells.");
    }
    const row = document.createElement("div");
    row.dataset.promptingRow = kind;
    row.dataset.promptingColumnCount = String(PROMPTING_GRID_COLUMNS.length);
    row.style.cssText = PROMPTING_ROW_CSS;
    cells.forEach((cell, index) => {
        cell.dataset.promptingCell = PROMPTING_CELL_NAMES[index];
    });
    row.append(...cells);
    return row;
}

export function promptIdentityAttachmentOwner(unit = {}) {
    const storedHandle = String(unit?.handle || "").trim();
    return {
        type: "identity",
        identityId: String(unit?.semantic_unit_id || ""),
        handle: storedHandle
            || derivePromptHandleSuggestion(unit?.name, "Identity"),
        storedHandle,
        displayName: String(unit?.name || unit?.semantic_unit_id || "Identity"),
    };
}

export function promptIdentityDependents(identityId, scenes = [], identity = null) {
    const counts = {
        reference_chips: 0,
        vocal_events: 0,
        source_relationships: Array.isArray(identity?.sources) ? identity.sources.length : 0,
    };
    const visit = (value) => {
        if (Array.isArray(value)) { value.forEach(visit); return; }
        if (!value || typeof value !== "object") return;
        if (value.kind === "reference"
                && (value.source?.semantic_unit_ids || []).map(String).includes(identityId)) {
            counts.reference_chips += 1;
        }
        if (value.kind === "vocal_event"
                && (value.source?.subject_ids || []).map(String).includes(identityId)) {
            counts.vocal_events += 1;
        }
        Object.values(value).forEach(visit);
    };
    scenes.forEach(visit);
    return counts;
}

function sectionHeading(title, description) {
    const wrapper = document.createElement("div");
    wrapper.style.cssText = `display:flex;flex-direction:column;gap:2px;padding-top:7px;border-top:1px solid ${COLORS.border};`;
    const heading = document.createElement("strong");
    heading.textContent = title;
    heading.style.cssText = `font:600 11px/1.3 system-ui;color:${COLORS.text};`;
    const help = document.createElement("span");
    help.textContent = description;
    help.style.cssText = `font:9px/1.35 system-ui;color:${COLORS.textDim};`;
    wrapper.append(heading, help);
    return wrapper;
}

function memberIndex(references) {
    const result = new Map();
    for (const reference of references || []) {
        for (const member of reference.members || []) {
            result.set(String(member.member_id || ""), { reference, member });
        }
    }
    return result;
}

function declarationLabel(declaration, row) {
    const field = `${String(declaration?.token_kind || "")}_ordinal`;
    const number = Number(row?.[field] || 0);
    return number > 0
        ? String(declaration?.label_template || "").replace("{n}", String(number))
        : "unresolved";
}

export function referencePromptingProjection({ candidate = {}, profile = {},
    references = [], semanticUnits = [], assets = [] } = {}) {
    const members = memberIndex(references);
    const assetsById = new Map((assets || []).map((asset) => [
        String(asset?.asset_id || ""), asset,
    ]));
    const manifest = candidate?.setup_manifest || {};
    const diagnostics = [...(candidate?.errors || []), ...(candidate?.warnings || [])];
    const declarations = Array.isArray(profile?.physical_populations)
        ? profile.physical_populations : [];
    return declarations.map((declaration) => {
        const population = String(declaration?.key || "");
        const rows = (manifest?.[population] || []).map((row) => {
            const memberId = String(row?.member_id || row?.video_member_id || "");
            const owner = members.get(memberId) || {};
            const linkedIdentities = (semanticUnits || []).filter((unit) =>
                (unit?.sources || []).some((source) =>
                    String(source?.member_id || "") === memberId));
            const rowDiagnostics = diagnostics.filter((diagnostic) =>
                (diagnostic?.slot_id && diagnostic.slot_id === row?.slot_id)
                || (diagnostic?.lane_id && diagnostic.lane_id === row?.lane_id));
            return { ...row, memberId, reference: owner.reference || null,
                member: owner.member || null,
                asset: assetsById.get(String(row?.asset_id || owner.member?.asset_id || "")) || null,
                linkedIdentities, diagnostics: rowDiagnostics,
                resolvedLabel: declarationLabel(declaration, row) };
        });
        const unresolved = diagnostics.filter((diagnostic) =>
            diagnostic?.lane_id && !(manifest?.[population] || []).some((row) =>
                row?.lane_id === diagnostic.lane_id));
        return { declaration, population, rows, unresolved };
    });
}

/**
 * What each staged Reference contributes as prompt TEXT, for this window.
 *
 * `referencePromptingProjection` above answers a different question — which
 * physical media a format's declared populations resolve to — and it is driven
 * entirely by `profile.physical_populations`. A recipe-derived prompt is not
 * physical and needs no declaration, so on a profile declaring no populations
 * (`generic@1`, the default) that projection is empty and the section said
 * nothing at all, while a staged item was contributing
 * `Reference sheet: … Generated video:` to the very prompt being compiled.
 *
 * The derived string comes from `deriveReferencePrompt`, the same mirror the
 * lane panel and the Python formatter share, so this is disclosure and never a
 * second authority. The override is applied because this screen reports what an
 * item CONTRIBUTES; the lane panel is where the two states are edited apart.
 */
function derivedPromptMembers(item, members) {
    return (item?.members || []).map((memberRef) => {
        const owner = members.get(String(memberRef?.member_id || "")) || {};
        return {
            entity_name: owner.reference?.name || "",
            member_name: owner.member?.name || "",
            prompt: owner.member?.prompt || "",
        };
    });
}

/** The text one staged item derives from its members, ignoring any override. */
export function derivedPromptForItem({ scene = null, references = [], item = null } = {}) {
    const laneIndex = Math.max(0, parseInt(item?.lane_index, 10) || 0);
    const soft = (scene?.reference_lane_recipes || [])[laneIndex]?.recipe?.soft || {};
    return deriveReferencePrompt({ promptOverride: "",
        members: derivedPromptMembers(item, memberIndex(references)), soft });
}

export function recipeDerivedPromptProjection({ scene = null, references = [],
    windowStart = 0, windowEnd = 0, frameThresholdPct = 0,
    declaredSources = false, semanticUnits = [] } = {}) {
    const items = scene?.reference_items || [];
    if (!items.length) return [];
    const members = memberIndex(references);
    const recipes = scene?.reference_lane_recipes || [];
    const { verdicts } = resolveReferenceVerdicts({
        referenceItems: items,
        laneCount: Math.max(1, parseInt(scene?.reference_lane_count, 10) || 1),
        sceneDuration: Math.max(0, parseInt(scene?.duration_frames, 10) || 0),
        windowStart, windowEnd,
        laneConfigs: scene?.reference_lane_configs || [],
        frameThresholdPct,
    });
    return items.map((item, itemIndex) => {
        const laneIndex = Math.max(0, parseInt(item?.lane_index, 10) || 0);
        const wrapper = recipes[laneIndex] || {};
        const soft = wrapper?.recipe?.soft || {};
        const resolved = derivedPromptMembers(item, members);
        const override = String(item?.prompt_override || "");
        return {
            itemId: String(item?.reference_item_id || ""),
            laneIndex,
            laneLabel: String(wrapper?.recipe?.name || "").trim()
                || `Lane ${laneIndex + 1}`,
            memberCount: resolved.length,
            overridden: !!override.trim(),
            prefix: String(soft?.prompt_prefix || "").trim(),
            suffix: String(soft?.prompt_suffix || "").trim(),
            derived: deriveReferencePrompt({
                promptOverride: override, members: resolved, soft }),
            verdict: verdicts.get(itemIndex) || "",
            attachedChips: countAttachedReferenceChips({
                scene, referenceItemId: String(item?.reference_item_id || ""),
                declaredSources, semanticUnits }),
            overrideText: override,
        };
    });
}

export function promptReferenceAttachment(owner, profile = {}) {
    const declaration = owner?.declaration || {};
    if (owner?.type !== "identity" && !String(declaration.source_key || "")) {
        throw new Error("This prompt format does not declare a physical source key.");
    }
    const source = owner?.type === "identity"
        ? { semantic_unit_ids: [String(owner.identityId || "")] }
        : { [String(declaration.source_key || "")]: [String(owner?.memberId || "")] };
    const declarations = orderedReferenceDerived(profile);
    if (!declarations.length) {
        throw new Error("This prompt format declares no Reference prompt parts.");
    }
    return normalizePromptAttachment({
        kind: "reference", source, config: { overrides: {} },
        // Routing is left BLANK so it inherits the format declaration. Copying
        // the declared channel and placement in here froze them into the
        // record, so a later format change moved the routing panel while
        // compiled output stayed where the chip was first attached — and the
        // stored copy was indistinguishable from a deliberate override, so
        // Reset had nothing to clear. The compiler resolves a blank through
        // `_resolved_capability` and `_route_for`; only a real deviation is
        // ever stored.
        // `enabled` is omitted for the same reason as the routing above: absent
        // means inherit the Reference/identity default, and seeding `true` here
        // would make every new chip read as an explicit override that pins the
        // capability on however the Reference is later configured.
        capabilities: declarations.map(([capabilityId]) => ({
            capability_id: capabilityId, kind: capabilityId,
            channel_key: "", placement: "", config: {},
        })),
    });
}

function openAttachmentTargetPicker({ scene, owner, onAttach, onClose, onError,
    profile = {}, references = [], semanticUnits = [], setupManifest = {},
    candidate = null, disclosureMemory = null, confirmDismiss = null }) {
    const opener = document.activeElement;
    const backdrop = document.createElement("div");
    backdrop.dataset.promptAttachmentTarget = "1";
    backdrop.style.cssText = "position:fixed;inset:0;z-index:12010;background:rgba(5,8,12,.72);display:flex;align-items:center;justify-content:center;padding:20px;";
    const modal = document.createElement("div");
    modal.style.cssText = `width:min(480px,92vw);padding:14px;border:1px solid ${COLORS.border};border-radius:8px;background:${COLORS.panelRaised};display:flex;flex-direction:column;gap:8px;color:${COLORS.text};font:10px system-ui;`;
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");
    const title = document.createElement("strong");
    title.id = `sonder-prompt-attachment-title-${uid()}`;
    modal.setAttribute("aria-labelledby", title.id);
    title.textContent = owner?.storedHandle
        ? `Attach @${owner.storedHandle}`
        : `Attach ${owner?.displayName || "Reference"} (will create @${
            owner?.handle || "Reference"})`;
    const target = makeSelect([
        ["global", "Scene-wide Context"],
        ...(scene?.prompt_sections || []).map((section, index) => [
            `section:${index}`,
            `Section ${index + 1} - ${section.start_frame || 0}-${section.end_frame || 0}`,
        ]),
    ], "global");
    const hint = document.createElement("span");
    hint.textContent = "The attachment follows current Reference/Identity defaults. Override any of them here, or later from the chip.";
    hint.style.color = COLORS.textDim;

    // Authoring moved here from the chip. The dialog used to ask only WHERE —
    // the least interesting question — at the one moment the author had full
    // context, then went silent while every remaining decision hid behind an
    // unadvertised click on the chip it had just created.
    const selection = owner?.type === "identity"
        ? String(owner.identityId || "")
        : `physical:${String(owner?.declaration?.key
            || owner?.declaration?.ordinal_key || "picture")}:${
            String(owner?.memberId || "")}`;
    const fieldset = createReferenceOverrideFieldset({
        profile, references, semanticUnits, setupManifest,
        overrides: {}, selected: selection,
        // Captured at open; unsaved choices here never move this preview.
        taskTypePreview: captureTaskTypePreview(candidate),
        showScenePreview: true,
        disclosureMemory, disclosureKey: "attach_overrides",
    });
    const fieldsetHost = document.createElement("div");
    fieldsetHost.dataset.sonderAttachFieldset = "1";
    fieldsetHost.style.cssText = "display:flex;flex-direction:column;gap:6px;max-height:46vh;overflow:auto;";
    if (fieldset.fields.length) {
        fieldsetHost.append(fieldset.summaryRow,
            ...fieldset.fields.map((field) => fieldset.row(field)));
    }
    // A speaker binding is resolved against the compiler's window-scoped
    // domain, not per section, so it is deliberately NOT offered here; the chip
    // owns it. Live routing state is likewise unavailable until a compile has
    // seen this attachment_id.
    const footer = document.createElement("div");
    footer.style.cssText = "display:flex;justify-content:flex-end;gap:6px;";
    const cancel = makeButton("Cancel");
    const attach = makeButton("Attach", "Create or reuse this Context attachment", "primary");
    let unregisterKeys = () => {};
    const close = () => {
        unregisterKeys();
        backdrop.remove();
        opener?.focus?.();
        onClose?.();
    };
    const draftGuard = createModalDraftGuard({
        controls: () => [...fieldset.controls.values()],
        confirm: confirmDismiss,
        message: "Discard these attachment overrides? Your unsaved changes will be lost.",
    });
    // Cancel is explicit intent and is never guarded.
    cancel.addEventListener("click", close);
    backdrop.addEventListener("click", (event) => {
        if (event.target === backdrop && draftGuard.confirmDismiss()) close();
    });
    attach.addEventListener("click", async () => {
        if (fieldset.validate()) return;
        setButtonDisabled(attach, true);
        const sectionMatch = String(target.value || "").match(/^section:(\d+)$/);
        try {
            await onAttach?.(owner, sectionMatch
                ? { scope: "section", sectionIndex: Number(sectionMatch[1]) }
                : { scope: "global" }, fieldset.collect());
            close();
        } catch (error) {
            setButtonDisabled(attach, false);
            onError?.(error);
        }
    });
    footer.append(cancel, attach);
    modal.append(title, target, hint, fieldsetHost, footer);
    modal.style.maxHeight = "86vh";
    modal.style.overflow = "auto";
    backdrop.appendChild(modal);
    document.body.appendChild(backdrop);
    unregisterKeys = registerKeyboardConsumer({
        id: `sonder-prompt-attachment-${uid()}`,
        priority: KEY_PRIORITY.OVERLAY,
        keydown: (event) => {
            if (event.isComposing === true || event.keyCode === 229) return false;
            if (event.key !== "Escape") return false;
            // Claim the key either way: returning false after a declined
            // confirm passes Escape to the panel's own OVERLAY consumer, which
            // closes the panel out from under this modal.
            if (draftGuard.confirmDismiss()) close();
            return true;
        },
    });
    return close;
}

export function mountPromptIdentityPanel(container, options = {}) {
    if (!container) return () => {};
    installChromePlaceholderStyles(container.ownerDocument);
    const cleanups = [];
    const profile = options.profile || {};
    const references = options.references || [];
    const semanticUnits = options.semanticUnits || [];
    const assets = options.assets || [];
    const candidate = options.candidate || {};
    const disclosureMemory = createDisclosureMemory(
        `sonder-prompt-identity-disclosure-v1:${String(options.projectKey || "project")}`);
    const projection = referencePromptingProjection({
        candidate, profile, references, semanticUnits, assets,
    });

    // A format that binds chips to members and identities (H3 Full Reference)
    // ignores an item's prompt override; only the Prompt Bridge reads it there.
    const declaredSources = Boolean(profile?.physical_populations?.length);
    const derivedRows = recipeDerivedPromptProjection({
        scene: options.scene, references,
        windowStart: Number(candidate?.window?.start_frame || 0),
        windowEnd: Number(candidate?.window?.end_frame || 0),
        frameThresholdPct: Number(options.frameThresholdPct || 0),
        declaredSources, semanticUnits,
    });

    // Copy, Edit as override and Clear for one staged item. Writes go through
    // the host's item writer, the one the lane panel used, so the guard,
    // ordering, Undo step and rollback are the host's. A draft is kept by item
    // id because this panel remounts on every render.
    const overrideDrafts = options.overrideDrafts || new Map();
    const writeReferenceItem = options.writeReferenceItem || null;
    const liveItem = (itemId) => (options.scene?.reference_items || []).find((value) =>
        String(value?.reference_item_id || "") === itemId) || null;
    const renderPromptOverrideControls = (row) => {
        const controls = document.createElement("div");
        controls.dataset.sonderPromptOverride = row.itemId;
        controls.style.cssText = "display:flex;flex-direction:column;gap:4px;";
        const actions = document.createElement("div");
        actions.style.cssText = "display:flex;gap:4px;flex-wrap:wrap;align-items:center;";
        const copy = makeButton("Copy", "Copy this item's prompt text");
        copy.addEventListener("click", async () => {
            try { await navigator.clipboard.writeText(row.derived || ""); } catch {}
        });
        actions.appendChild(copy);
        if (declaredSources) {
            const scope = document.createElement("span");
            scope.textContent = "Override: Prompt Bridge / Generic formats only";
            scope.style.cssText = `font:9px system-ui;color:${COLORS.textDim};`;
            actions.appendChild(scope);
        }
        controls.appendChild(actions);
        if (!writeReferenceItem) return controls;
        // A locked lane says so here rather than only refusing the write.
        const locked = !!(options.scene?.reference_lane_configs || [])[row.laneIndex]?.locked;
        const lockedTitle = "(locked) Unlock the lane to edit.";
        if (!row.overridden) {
            const takeOver = makeButton("Edit as override", locked ? lockedTitle
                : "Start from this text and edit it freely");
            setButtonDisabled(takeOver, locked || !row.derived);
            // Derived from the LIVE row when the host acts, after any member
            // edit still saving, never from this drawn copy.
            takeOver.addEventListener("click", () => void writeReferenceItem(row.itemId,
                (live) => {
                    const text = derivedPromptForItem({ scene: options.scene,
                        references, item: live });
                    return text ? { prompt_override: text } : null;
                }, "override reference prompt"));
            actions.insertBefore(takeOver, actions.children[1] || null);
            return controls;
        }
        const box = document.createElement("textarea");
        box.rows = 2;
        box.style.cssText = chromeInputCss({ padding: "4px 6px", fontSize: "11px" })
            + "width:100%;box-sizing:border-box;resize:vertical;min-height:34px;";
        box.dataset.referenceItemId = row.itemId;
        const stored = String(row.overrideText || "");
        box.value = overrideDrafts.has(row.itemId) ? overrideDrafts.get(row.itemId) : stored;
        box.setAttribute("aria-label", `Prompt override for ${row.laneLabel}`);
        box.disabled = locked;
        if (locked) box.title = lockedTitle;
        box.addEventListener("input", () => overrideDrafts.set(row.itemId, box.value));
        // First Escape reverts the box (the panel's own Escape handling); the
        // blur that follows must not save the reverted text as an edit.
        options.registerBoxGuard?.(box, () => String(liveItem(row.itemId)?.prompt_override || ""));
        box.addEventListener("blur", () => {
            if (options.isCommitSuppressed?.()) {
                // Escape put the stored text back: the draft is gone. A panel
                // teardown leaves the typed text, kept for the next mount.
                if (box.value === String(liveItem(row.itemId)?.prompt_override || "")) {
                    overrideDrafts.delete(row.itemId);
                }
                return;
            }
            const value = box.value;
            overrideDrafts.delete(row.itemId);
            if (value === String(liveItem(row.itemId)?.prompt_override || "")) return;
            void writeReferenceItem(row.itemId, { prompt_override: value },
                "edit reference prompt override");
        });
        const clear = makeButton("Clear", locked ? lockedTitle
            : "Go back to the prompt derived from the staged members");
        setButtonDisabled(clear, locked);
        clear.addEventListener("click", () => {
            overrideDrafts.delete(row.itemId);
            void writeReferenceItem(row.itemId, { prompt_override: "" },
                "clear reference prompt override");
        });
        actions.insertBefore(clear, actions.children[1] || null);
        controls.appendChild(box);
        return controls;
    };

    container.appendChild(sectionHeading("Reference Prompting",
        "Physical media resolved from the active setup and effective window, and the prompt text each staged Reference derives from its recipe. Staging remains in Reference lanes."));
    const referenceBody = document.createElement("div");
    referenceBody.dataset.referencePrompting = "1";
    referenceBody.style.cssText = "display:flex;flex-direction:column;gap:5px;";
    container.appendChild(referenceBody);
    if (!projection.length) {
        const empty = document.createElement("div");
        // Stays true and stops being the whole story. On `generic@1` this line
        // was the only thing the section said, which read as "no Reference
        // reaches the prompt" while a staged item was contributing text to it.
        empty.textContent = derivedRows.length
            ? "This prompt format declares no physical Reference populations; staged References still contribute the derived text below."
            : "This prompt format declares no physical Reference populations.";
        empty.style.cssText = `font:10px system-ui;color:${COLORS.textDim};`;
        referenceBody.appendChild(empty);
    }

    let modalCleanup = null;
    const rerender = () => options.onRefresh?.();
    const saveIdentity = async (nextUnit, previousUnit = null) => {
        await options.saveSemanticUnitChange?.({
            type: "upsert", value: nextUnit,
            expected: previousUnit ? structuredClone(previousUnit) : null,
        }, "edit prompt identities");
        rerender();
    };
    const deleteIdentity = async (identity) => {
        const dependents = promptIdentityDependents(identity.semantic_unit_id,
            options.scenes || [], identity);
        const details = Object.entries(dependents).filter(([, count]) => count)
            .map(([key, count]) => `${count} ${key.replaceAll("_", " ")}`);
        const disclosure = details.length ? details.join(", ") : "no dependents";
        if (!options.confirm?.(`Delete prompt identity "${identity.name || identity.semantic_unit_id}"?\n\n${disclosure}. Bound chips and Vocal Events remain visible as broken links for repair; physical sources are detached.`)) return false;
        await options.saveSemanticUnitChange?.({
            type: "delete", semantic_unit_id: identity.semantic_unit_id,
            expected: structuredClone(identity),
        }, "delete prompt identity");
        rerender(); return true;
    };
    const openEditor = (identity = null, seedSource = null) => {
        modalCleanup?.();
        options.onModalStateChange?.(true);
        modalCleanup = openIdentityEditor({ identity, seedSource, profile,
            references, semanticUnits, assets, disclosureMemory,
            candidate: options.currentCandidate?.() || candidate,
            placementPhases: options.catalog?.placement_phases || [],
            // How many attachments actually follow these defaults. The count is
            // what turns an abstract "defaults" group into a statement about
            // this project, and it is the arrow the chip end already draws.
            attachmentCount: identity?.semantic_unit_id
                ? promptIdentityDependents(identity.semantic_unit_id,
                    options.scenes || [], identity).reference_chips : 0,
            onSave: (nextUnit) => saveIdentity(nextUnit, identity),
            onDelete: deleteIdentity,
            onClose: () => {
                modalCleanup = null;
                options.onModalStateChange?.(false);
            }, onError: options.onError });
    };
    const openAttach = (owner) => {
        modalCleanup?.();
        options.onModalStateChange?.(true);
        // One snapshot for the whole dialog: the staged setup its fields
        // inherit from and the task-type preview must describe one compile.
        const openCandidate = options.currentCandidate?.() || candidate;
        modalCleanup = openAttachmentTargetPicker({
            scene: options.scene, owner,
            profile, references, semanticUnits,
            setupManifest: openCandidate?.setup_manifest || {},
            candidate: openCandidate,
            disclosureMemory,
            confirmDismiss: options.confirm,
            onAttach: options.attachReference,
            onClose: () => {
                modalCleanup = null;
                options.onModalStateChange?.(false);
            },
            onError: options.onError,
        });
    };

    for (const group of projection) {
        const groupEl = document.createElement("div");
        groupEl.style.cssText = "display:flex;flex-direction:column;gap:4px;";
        const label = document.createElement("strong");
        label.textContent = `${group.declaration.label || group.population} (${group.rows.length})`;
        label.style.cssText = `font:9px system-ui;color:${COLORS.textDim};text-transform:uppercase;`;
        groupEl.appendChild(label);
        for (const row of group.rows) {
            let rowEl;
            const thumbnail = document.createElement("div");
            thumbnail.style.cssText = `width:34px;height:28px;border:1px solid ${COLORS.border};border-radius:3px;background:${COLORS.bg};overflow:hidden;display:flex;align-items:center;justify-content:center;color:${COLORS.textDim};font:8px system-ui;`;
            const previewUrl = options.assetPreviewUrl?.(row.asset);
            if (previewUrl) {
                const image = document.createElement("img");
                image.src = previewUrl; image.alt = row.display_name || "Reference";
                image.loading = "lazy"; image.draggable = false;
                image.style.cssText = "width:100%;height:100%;object-fit:cover;";
                thumbnail.appendChild(image);
            } else thumbnail.textContent = row.asset?.asset_type || "media";
            const suggestion = derivePromptHandleSuggestion(row.member?.name
                || row.reference?.name || row.display_name, "Reference");
            const handle = document.createElement("input");
            handle.value = row.member?.handle || "";
            handle.placeholder = suggestion;
            handle.classList?.add(CHROME_DIM_PLACEHOLDER_CLASS);
            handle.setAttribute("aria-label", "Physical Reference handle");
            handle.title = `${row.member?.handle
                ? "Durable handle."
                : "Suggested handle; it is stored when edited or first referenced."
            } ${PROMPT_HANDLE_RULE}`;
            // Sanitize while typing. The charset was previously learnable only
            // by submitting and reading the server's refusal, even though the
            // rule was already encoded one function away.
            handle.addEventListener("input", () => {
                const sanitized = sanitizePromptHandle(handle.value);
                if (sanitized === handle.value) return;
                const caret = Math.max(0, (handle.selectionStart ?? sanitized.length)
                    - (handle.value.length - sanitized.length));
                handle.value = sanitized;
                handle.setSelectionRange?.(caret, caret);
            });
            handle.style.cssText = `${chromeInputCss()}font-weight:600;color:${COLORS.text};`;
            // The row as this panel last knew it: the render that redraws it
            // can be deferred (a press in the panel), so the panel's own
            // accepted writes advance it, and a later write's guard is not
            // an older value than the author saw.
            let drawnMember = row.member;
            // Paints at once through the host writer. A no-op is judged against
            // the Library as displayed (a second edit of the same handle may
            // follow an unsaved first), and a handle another Reference or
            // identity already owns is refused here, before anything is sent.
            // A refusal the server answers is said once, by the host, which
            // also drops the paint.
            const commitHandle = async () => {
                if (!drawnMember || !row.reference) return;
                const next = handle.value.trim();
                const shown = String(options.displayedMember?.(row.member.member_id)?.handle
                    ?? row.member.handle ?? "");
                if (next === shown) {
                    handle.value = shown;
                    return;
                }
                const owner = next ? options.handleCollision?.(next, row.member.member_id) : null;
                if (owner) {
                    handle.value = shown;
                    options.onError?.(new Error(`@${next} is already used by another ${
                        owner.kind === "prompt identity" ? "prompt identity" : "Reference"}.`));
                    return;
                }
                const pending = options.writeReferenceMember?.({
                    referenceId: row.reference.reference_id, memberId: row.member.member_id,
                    drawn: { handle: String(drawnMember.handle || "") },
                    fields: { handle: next }, label: "edit physical Reference handle",
                });
                rerender();
                try {
                    await pending;
                    drawnMember = { ...drawnMember, handle: next };
                } catch (_error) { rerender(); }
            };
            handle.addEventListener("change", () => { void commitHandle(); });
            handle.addEventListener("keydown", (event) => {
                if (event.key === "Enter") { event.preventDefault(); handle.blur(); }
            });
            const status = document.createElement("span");
            const identityCount = row.linkedIdentities.length;
            // Roles are declared as {value, label}; printing the raw value gave
            // "first_frame" instead of "First frame" — and, when a format
            // declares a role literally named `identity`, produced the
            // self-contradicting "identity · <Picture 2> · no identity".
            const roleLabel = referenceRoleChoices(profile, group.population)
                .find((choice) => choice.value === String(row.role || ""))?.label
                || String(row.role || "");
            // Three positionally stable segments. `Role:` is always spelled out
            // so the middle segment — the provider's resolved slot label — can
            // never be misread as the role, and the identity clause always says
            // "prompt identity" so it cannot be confused with a declared role
            // that happens to be NAMED identity. No action hint: the button
            // beside it says "+ Identity".
            const identityState = identityCount
                ? `${identityCount} prompt identit${identityCount === 1 ? "y" : "ies"}`
                : "No prompt identity";
            status.textContent = `Role: ${roleLabel || "unset"} · ${
                row.resolvedLabel} · ${identityState}`;
            // Never tinted. A warm status on every row lacking an identity reads
            // as broken forever for a Style Reference that legitimately has
            // none, and the predicate was wrong besides — the real signal is the
            // `unnamed_physical_reference` advisory, which correctly requires no
            // identity AND no prose.
            status.style.cssText = `font:9px system-ui;color:${COLORS.textDim};`;
            const defaults = makeButton("Defaults", "Edit physical prompt text and preservation defaults");
            // The defaults as drawn, in the form the route stores them.
            const drawnDefaults = () => ({
                prompt: String(drawnMember?.prompt || ""),
                visual_intent: String(drawnMember?.visual_intent || ""),
                audio_intent: String(drawnMember?.audio_intent || ""),
                disabled_capabilities: [...(drawnMember?.disabled_capabilities || [])].map(String),
            });
            // `draft`: typed values handed back after a refusal, and the
            // defaults they were first diffed from (which keys they send).
            const openDefaults = (draft = null) => {
                const existing = groupEl.querySelector(
                    `[data-physical-defaults='${CSS.escape(row.memberId)}']`);
                if (existing) {
                    // Closing it dismisses a draft handed back after a refusal.
                    existing.remove();
                    options.defaultsDrafts?.delete(row.memberId);
                    return;
                }
                if (!row.member || !row.reference) return;
                const editor = document.createElement("div");
                editor.dataset.physicalDefaults = row.memberId;
                editor.style.cssText = `display:grid;grid-template-columns:minmax(160px,1fr) auto auto auto;gap:5px;padding:6px;border:1px solid ${COLORS.border};border-radius:5px;background:${COLORS.panelRaised};`;
                const prompt = document.createElement("textarea");
                prompt.rows = 2; prompt.value = draft ? draft.values.prompt : (row.member.prompt || "");
                prompt.placeholder = "Physical Reference prompt description";
                prompt.style.cssText = chromeInputCss();
                const visual = makeSelect(intentValues(
                    profile, "visual_intent", { includeInherited: true }),
                draft ? draft.values.visual_intent : (row.member.visual_intent || ""));
                const audio = makeSelect(intentValues(
                    profile, "audio_intent", { includeInherited: true }),
                draft ? draft.values.audio_intent : (row.member.audio_intent || ""));
                // Which prompt parts this Reference contributes at all is a
                // property of the Reference; WHERE each part lands stays
                // per-attachment and remains in the chip's routing panel.
                const storedDisabled = new Set(draft
                    ? draft.values.disabled_capabilities
                    : (row.member.disabled_capabilities || []).map(String));
                const parts = document.createElement("div");
                parts.dataset.sonderMemberCapabilityDefaults = "1";
                parts.style.cssText = "grid-column:1/-1;display:flex;flex-wrap:wrap;gap:8px;align-items:center;";
                const partsLabel = document.createElement("span");
                partsLabel.textContent = "Contributes";
                partsLabel.style.cssText = `font:9px system-ui;color:${COLORS.textDim};`;
                parts.appendChild(partsLabel);
                const partBoxes = new Map();
                for (const [capabilityId, declaration] of
                    orderedReferenceDerived(profile)) {
                    const item = document.createElement("label");
                    item.style.cssText = "display:flex;gap:3px;align-items:center;font:9px system-ui;";
                    const box = document.createElement("input");
                    box.type = "checkbox";
                    box.checked = !storedDisabled.has(capabilityId);
                    box.setAttribute("aria-label",
                        `Contribute ${declaration?.label || capabilityId} by default`);
                    item.title = String(declaration?.help
                        || "New attachments follow this; a chip may still override it.");
                    partBoxes.set(capabilityId, box);
                    item.append(box, String(declaration?.label || capabilityId));
                    parts.appendChild(item);
                }
                const save = makeButton("Save defaults", "Persist through the Reference mutation batch", "primary");
                save.addEventListener("click", async () => {
                    // Preserve ids this format does not declare: the member may
                    // also be used under another format that owns them.
                    const declaredIds = new Set(partBoxes.keys());
                    const nextDisabled = [
                        ...[...storedDisabled].filter((id) => !declaredIds.has(id)),
                        ...[...partBoxes].filter(([, box]) => !box.checked)
                            .map(([id]) => id),
                    ];
                    // Stored as `normalize_disabled_capabilities` stores it.
                    const disabled = [];
                    for (const id of nextDisabled.map((value) => String(value || "").trim())) {
                        if (id && !disabled.includes(id)) disabled.push(id);
                    }
                    const values = {
                        prompt: prompt.value,
                        visual_intent: visual.value,
                        audio_intent: audio.value,
                        disabled_capabilities: disabled,
                    };
                    // Only the keys that differ from what the author started
                    // from: sending an untouched key would revert a change made
                    // meanwhile elsewhere (the Library's prompt field, say).
                    const origin = draft?.origin || drawnDefaults();
                    const fields = Object.fromEntries(Object.entries(values).filter(([key, value]) =>
                        JSON.stringify(value) !== JSON.stringify(origin[key])));
                    editor.remove();
                    options.defaultsDrafts?.delete(row.memberId);
                    if (!Object.keys(fields).length) return;
                    // A refusal, or a lost answer that turns out unsaved,
                    // reopens the editor with what was typed; the panel
                    // remounts on every render, so the host-owned map holds it.
                    const handBack = () => {
                        if (options.defaultsDrafts?.has(row.memberId)) return;
                        options.defaultsDrafts?.set(row.memberId, { values, origin });
                        rerender();
                    };
                    const pending = options.writeReferenceMember?.({
                        referenceId: row.reference.reference_id, memberId: row.member.member_id,
                        drawn: drawnDefaults(), fields, label: "edit physical Reference prompt defaults",
                        onUnconfirmedResolved: (saved) => { if (!saved) handBack(); },
                    });
                    rerender();
                    try {
                        await pending;
                        drawnMember = { ...drawnMember, ...fields };
                    } catch (error) {
                        if (Number.isInteger(error?.status)) handBack();
                        else rerender();
                    }
                });
                editor.append(prompt, visual, audio, save, parts);
                rowEl.insertAdjacentElement("afterend", editor);
            };
            defaults.addEventListener("click", () => openDefaults());
            // Labelled, not a bare glyph. This is the step that turns staged
            // media into something a prompt can name, and it was the single
            // least discoverable control in the tool. The `+` prefix makes it
            // read as an action; the aria-label carries the full sentence,
            // because two words are thin for a screen reader.
            const create = makeButton("+ Identity",
                "Turn this physical Reference into a named prompt identity. One Reference may feed several.",
                "primary", "Create prompt identity from physical Reference");
            create.addEventListener("click", () => openEditor(null, row));
            const attach = makeButton("Attach...", "Attach this physical Reference to the scene or a section");
            // The stored handle is the server's; the displayed one may be a
            // handle edit still saving, which Attach then waits for rather
            // than materializing a suggestion over it.
            const displayedHandle = String(row.member?.handle || "");
            const storedHandle = options.storedHandleFor
                ? String(options.storedHandleFor(row.memberId) || "") : displayedHandle;
            attach.addEventListener("click", () => openAttach({
                type: "physical", memberId: row.memberId,
                referenceId: row.reference?.reference_id || "",
                handle: displayedHandle || suggestion,
                suggestion,
                // As drawn; the host re-reads both when the attach acts.
                storedHandle,
                handlePending: displayedHandle !== storedHandle,
                displayName: row.member?.name || row.reference?.name || "Reference",
                declaration: group.declaration,
            }));
            rowEl = buildPromptingRow("physical", [
                thumbnail, handle, status, attach, defaults, create,
            ]);
            rowEl.dataset.physicalMemberId = row.memberId;
            const returnedDraft = options.defaultsDrafts?.get(row.memberId);
            rowEl.title = [
                `Resolved: ${row.resolvedLabel}`,
                `Window: ${candidate?.window_start ?? 0}-${candidate?.window_end ?? "scene"}`,
                `Role: ${row.role || "none"}`,
                `Status: ${row.diagnostics.map((value) => value.code).join(", ") || "ready"}`,
                `Linked identities: ${row.linkedIdentities.map((value) => `@${value.handle || value.name}`).join(", ") || "none"}`,
            ].join("\n");
            groupEl.appendChild(rowEl);
            if (returnedDraft) openDefaults(returnedDraft);
        }
        for (const diagnostic of group.unresolved) {
            const unresolved = document.createElement("div");
            unresolved.textContent = `${diagnostic.code}: ${diagnostic.message || "Unresolved setup row"}`;
            unresolved.style.cssText = `padding:4px 6px;border:1px solid ${COLORS.dangerBorder};border-radius:4px;background:${COLORS.dangerSoft};color:${COLORS.dangerText};font:9px system-ui;`;
            groupEl.appendChild(unresolved);
        }
        if (!group.rows.length && !group.unresolved.length) {
            const empty = document.createElement("span");
            empty.textContent = `No ${String(group.declaration.label || group.population).toLowerCase()} references resolve in this window.`;
            empty.style.cssText = `font:9px system-ui;color:${COLORS.textDim};`;
            groupEl.appendChild(empty);
        }
        referenceBody.appendChild(groupEl);
    }

    if (derivedRows.length) {
        const derivedGroup = document.createElement("div");
        derivedGroup.dataset.sonderRecipeDerivedPrompts = "1";
        derivedGroup.style.cssText = "display:flex;flex-direction:column;gap:4px;";
        const label = document.createElement("strong");
        label.textContent = `Recipe-derived prompt (${derivedRows.length})`;
        label.style.cssText = `font:9px system-ui;color:${COLORS.textDim};text-transform:uppercase;`;
        derivedGroup.appendChild(label);
        const intro = document.createElement("span");
        // Words live here; the lane panel shows a read-only preview and the
        // recipe's syntax. Both exits (chip and Prompt Bridge) render this one
        // string, so one place edits it.
        intro.textContent = declaredSources
            ? "The text each staged item derives from its recipe. This format builds its prompt from Subjects and pictures, so an override here reaches only the Prompt Bridge output."
            : "The text each staged item derives from its recipe. Edit as override replaces it for this item; Clear goes back to the derived text.";
        intro.style.cssText = `font:9px/1.35 system-ui;color:${COLORS.textDim};`;
        derivedGroup.appendChild(intro);
        for (const row of derivedRows) {
            const rowEl = document.createElement("div");
            rowEl.dataset.sonderDerivedPromptItem = row.itemId;
            rowEl.style.cssText = `display:flex;flex-direction:column;gap:3px;padding:5px 6px;border:1px solid ${COLORS.border};border-radius:5px;`;
            const head = document.createElement("div");
            head.style.cssText = `display:flex;gap:6px;align-items:baseline;font:9px system-ui;color:${COLORS.textDim};`;
            const name = document.createElement("span");
            name.textContent = row.laneLabel;
            name.style.cssText = `font-weight:600;color:${COLORS.text};`;
            const state = document.createElement("span");
            const verdictLabel = REFERENCE_VERDICT_LABEL[row.verdict] || "";
            const chips = row.attachedChips
                ? `Attached to ${row.attachedChips} prompt chip${row.attachedChips === 1 ? "" : "s"}`
                : "Not attached to any prompt chip";
            state.textContent = [
                verdictLabel,
                `${row.memberCount} member${row.memberCount === 1 ? "" : "s"}`,
                row.overridden ? "overridden" : "",
                chips,
            ].filter(Boolean).join(" · ");
            head.append(name, state);
            rowEl.appendChild(head);
            const text = document.createElement("div");
            text.textContent = row.derived || "No prompt text.";
            text.style.cssText = `font:10px/1.4 system-ui;color:${
                row.derived ? COLORS.text : COLORS.textDim};white-space:pre-wrap;word-break:break-word;`;
            rowEl.appendChild(text);
            // The suffix in particular is invisible in the members it wraps, so
            // an author reading only the assembled line cannot tell which part
            // the recipe contributed and which part they wrote.
            const parts = [
                row.prefix ? `prefix “${row.prefix}”` : "",
                row.suffix ? `suffix “${row.suffix}”` : "",
            ].filter(Boolean);
            if (parts.length && !row.overridden) {
                const recipeNote = document.createElement("span");
                recipeNote.textContent = `Recipe adds ${parts.join(" and ")}.`;
                recipeNote.style.cssText = `font:9px system-ui;color:${COLORS.textDim};`;
                rowEl.appendChild(recipeNote);
            }
            if (row.overridden && parts.length) {
                const overrideNote = document.createElement("span");
                overrideNote.textContent = "An override replaces the whole derivation, so the recipe's prefix and suffix are not added.";
                overrideNote.style.cssText = `font:9px system-ui;color:${COLORS.textDim};`;
                rowEl.appendChild(overrideNote);
            }
            rowEl.appendChild(renderPromptOverrideControls(row));
            derivedGroup.appendChild(rowEl);
        }
        referenceBody.appendChild(derivedGroup);
    }

    container.appendChild(sectionHeading("Identity Prompting",
        "Semantic prompt identities may combine physical references or exist from description alone."));
    const identityBody = document.createElement("div");
    identityBody.dataset.identityPrompting = "1";
    identityBody.style.cssText = "display:flex;flex-direction:column;gap:4px;";
    const identityCreationDisabled = !(profile?.identity_kinds || []).length;
    for (const unit of semanticUnits) {
        const thumbnail = document.createElement("div");
        thumbnail.style.cssText = `width:34px;height:28px;border:1px solid ${COLORS.border};border-radius:3px;background:${COLORS.panelRaised};display:flex;align-items:center;justify-content:center;color:${COLORS.textDim};font:8px system-ui;`;
        const kindDeclaration = (profile?.identity_kinds || []).find((value) =>
            String(value?.key || "") === String(unit.kind || "subject"));
        thumbnail.textContent = String(kindDeclaration?.label || unit.kind || "Identity")
            .slice(0, 1).toUpperCase();
        const name = document.createElement("span");
        const identityName = String(unit.name || unit.semantic_unit_id);
        if (unit.handle) {
            name.textContent = `@${unit.handle} — ${identityName}`;
        } else {
            name.textContent = identityName;
            const noHandle = document.createElement("span");
            noHandle.textContent = " · no handle";
            noHandle.style.cssText = `font:9px system-ui;color:${COLORS.textDim};`;
            name.appendChild(noHandle);
        }
        name.style.cssText = `font:10px system-ui;color:${COLORS.text};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`;
        const speakerIndex = (candidate?.managed_speaker_subject_ids || [])
            .map(String).indexOf(String(unit.semantic_unit_id));
        const status = document.createElement("span");
        const kindStatus = kindDeclaration
            ? `${kindDeclaration.label || unit.kind} · ${(unit.sources || []).length} physical`
            : `${unit.kind || "unknown"} · unsupported by this format`;
        status.textContent = `${kindStatus} · ${speakerIndex >= 0 ? `speaking S${speakerIndex + 1}` : "not speaking"}`;
        status.style.cssText = `font:9px system-ui;color:${
            kindDeclaration ? COLORS.textDim : COLORS.dangerText};`;
        const edit = makeButton("Edit", "Edit identity, sources, contributions, and voice");
        edit.addEventListener("click", () => openEditor(unit));
        const attach = makeButton("Attach...", "Attach this prompt identity to the scene or a section");
        attach.addEventListener("click", () => openAttach(
            promptIdentityAttachmentOwner(unit)));
        const remove = makeButton("×", "Delete with dependency disclosure", "danger",
            "Delete prompt identity");
        remove.style.cssText += "min-width:24px;padding:2px 6px;font-size:12px;";
        remove.addEventListener("click", () => { void deleteIdentity(unit); });
        const row = buildPromptingRow("identity", [
            thumbnail, name, status, attach, edit, remove,
        ]);
        row.dataset.semanticUnitId = unit.semantic_unit_id;
        identityBody.appendChild(row);
    }
    const createHint = document.createElement("span");
    createHint.textContent = identityCreationDisabled
        ? "This prompt format declares no identity kinds."
        : "Create a description-only identity or attach physical References.";
    createHint.style.cssText = `font:9px system-ui;color:${COLORS.textDim};`;
    const createIdentity = makeButton("+", "Create with or without physical references", "primary",
        "Create prompt identity");
    createIdentity.style.cssText += "min-width:24px;padding:2px 6px;font-size:12px;";
    // Through the helper, not the bare flag: an inline-styled button carries no
    // `:disabled` rule, so `.disabled = true` alone left a control that looked
    // fully enabled and silently did nothing.
    setButtonDisabled(createIdentity, identityCreationDisabled);
    if (identityCreationDisabled) {
        createIdentity.title = "This prompt format declares no identity kinds, so there is nothing to create.";
    }
    createIdentity.addEventListener("click", () => openEditor());
    const createRow = buildPromptingRow("identity-create", [
        document.createElement("span"), createHint, document.createElement("span"),
        document.createElement("span"), document.createElement("span"), createIdentity,
    ]);
    identityBody.appendChild(createRow);
    container.appendChild(identityBody);

    cleanups.push(() => modalCleanup?.());
    return () => cleanups.splice(0).forEach((cleanup) => cleanup());
}
