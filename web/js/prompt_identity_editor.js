// The prompt identity editor, shared by Identity Prompting's + and the
// Attach dialog's "New identity…". The host decides what Save does: the
// identity panel writes the identity at once, while Attach takes it back as
// a pending create and commits it with the chip in one gesture.

import { EDITOR_COLORS as COLORS, chromeInputCss, setButtonDisabled,
    setButtonVariant } from "./editor_theme.js";
import { referencePromptDefaults } from "./prompt_context_chips.js";
import { TASK_TYPE_PREVIEW_LABEL, captureTaskTypePreview,
    createTaskTypeChoiceControl } from "./prompt_task_type_choices.js";
import { createModalDraftGuard } from "./modal_draft_guard.js";
import {
    PRIORITY as KEY_PRIORITY,
    register as registerKeyboardConsumer,
} from "./keyboard_ownership.js";
import {
    declaredFieldChoices,
    orderedReferenceDerived,
    referenceFieldDeclaration,
} from "./prompt_profile_declarations.js";

const uid = () => globalThis.crypto?.randomUUID?.().replaceAll("-", "")
    || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;

export const PROMPT_HANDLE_RULE =
    "Letters and digits only, starting with a letter. Up to 64 characters.";

/** Sanitize a typed handle without reformatting what the author is writing. */
export function sanitizePromptHandle(value) {
    const cleaned = String(value || "").replace(/[^A-Za-z0-9]/g, "");
    const rooted = /^[A-Za-z]/.test(cleaned) || !cleaned
        ? cleaned : `Ref${cleaned}`;
    return rooted.slice(0, 64);
}

/**
 * A typeable handle suggestion.
 *
 * A handle is what an author TYPES while writing a prompt, so the suggestion
 * has to be short enough to retype from memory. Camel-casing every token of a
 * filename produced things like `ChatGPTImageAug112026120009PM` \u2014 technically
 * valid and unusable at the exact moment it matters. Generated tails are
 * dropped and the result is capped at three words.
 */
export function derivePromptHandleSuggestion(value, fallback = "Reference") {
    const words = String(value || "").normalize("NFKD")
        .replace(/[\u0300-\u036f]/g, "")
        .match(/[A-Za-z0-9]+/g) || [];
    // Drop camera/export debris: pure digits, and the date/time fragments that
    // dominate generated filenames.
    const meaningful = words.filter((word) => !/^\d+$/.test(word)
        && !/^(?:19|20)\d{2}$/.test(word)
        && !/^(?:AM|PM)$/i.test(word)
        && !/^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\d*$/i
            .test(word));
    const chosen = (meaningful.length ? meaningful : words).slice(0, 3);
    let handle = chosen.map((word) =>
        word[0]?.toUpperCase() + word.slice(1)).join("");
    if (!handle) handle = String(fallback || "Reference").replace(/[^A-Za-z0-9]/g, "");
    return sanitizePromptHandle(handle) || "Reference";
}

export function promptReferencePickerLabel(reference = {}, member = {}) {
    const name = String(member?.name || reference?.name || "Reference");
    const handle = String(member?.handle || "").trim();
    return handle ? `@${handle} — ${name}` : name;
}

export function promptIdentitySourceGroup(assetType, reference = {}, member = {}) {
    const kind = String(assetType || "");
    if (kind === "audio") return "audio";
    if (kind !== "video") return "appearance";
    const memberAudioIntent = String(member?.audio_intent || "");
    const entityAudioIntent = String(reference?.audio_intent || "");
    return memberAudioIntent || (entityAudioIntent
        && entityAudioIntent !== "reference_characteristics")
        ? "audio" : "appearance";
}

export function legacyVoiceBindingRepair(identity = {}, references = []) {
    const memberId = String(identity?.voice?.member_id || "");
    if (!memberId || (identity?.sources || []).some((source) =>
        String(source?.member_id || "") === memberId)) return null;
    let owner = null;
    let member = null;
    for (const reference of references || []) {
        const found = (reference?.members || []).find((value) =>
            String(value?.member_id || "") === memberId);
        if (found) { owner = reference; member = found; break; }
    }
    if (!owner || !member) return null;
    const next = structuredClone(identity);
    next.sources = [...(next.sources || []), {
        entity_id: String(owner.reference_id || ""),
        member_id: memberId,
        contribution: "",
    }];
    delete next.voice;
    return { identity: next, reference: owner, member };
}

export function makeButton(label, title = "", variant = "", ariaLabel = "") {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.title = title;
    // Geometry is passed through so the rows keep their exact density; palette,
    // hover/active states and disabled handling come from the theme rather than
    // being re-invented here. `primary` maps to the soft accent ground, not the
    // filled one — a row with three buttons cannot carry a solid fill.
    setButtonVariant(button, variant === "primary" ? "accentSoft"
        : (variant || "secondary"),
    { padding: "3px 7px", fontSize: "10px", lineHeight: "1.3" });
    if (ariaLabel) button.setAttribute("aria-label", ariaLabel);
    return button;
}

export function makeSelect(values, selected = "") {
    const select = document.createElement("select");
    select.style.cssText = chromeInputCss();
    const options = [...(values || [])];
    const selectedValue = String(selected || "");
    if (selectedValue && !options.some(([value]) => String(value) === selectedValue)) {
        options.push([selectedValue, `Unsupported saved value: ${selectedValue}`]);
        select.dataset.sonderInvalid = "1";
        select.title = "Choose a value supported by this Prompt Format before changing it.";
    }
    for (const [value, label] of options) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        select.appendChild(option);
    }
    select.value = selectedValue;
    return select;
}

export function contributionValues(profile, populations = ["*"]) {
    if (Object.hasOwn(profile || {}, "contribution_catalog")) {
        const catalog = profile?.contribution_catalog || {};
        const keys = [...new Set([...(populations || []), "*"])];
        return keys.flatMap((key) => catalog[key] || [])
            .filter((value, index, values) => value?.value
                && values.findIndex((row) => row?.value === value.value) === index)
            .map((value) => [String(value.value), String(value.label || value.value)]);
    }
    return [];
}

export function intentValues(profile, field, { includeInherited = false } = {}) {
    const values = declaredFieldChoices(referenceFieldDeclaration(
        profile, "retention", field)).map((value) => [value.value, value.label]);
    return includeInherited
        ? [["", "Inherit Reference default"], ...values]
        : values;
}

export function fieldRow(label, input, { help = "", required = false, group = false } = {}) {
    // A control holding several inputs sits in a `div`: a `<label>` activates
    // its first labelable descendant, so its caption would press a button.
    const row = document.createElement(group ? "div" : "label");
    row.style.cssText = "display:grid;grid-template-columns:130px minmax(0,1fr);gap:7px;align-items:center;";
    const caption = document.createElement("span");
    caption.textContent = `${label}${required ? " *" : ""}`;
    if (help) {
        caption.title = help;
        input.title = input.title || help;
    }
    row.append(caption, input);
    return row;
}

export function declarationGuidance(declaration, fallback = "") {
    const parts = [
        declaration?.description,
        declaration?.help,
        declaration?.example ? `Example: ${declaration.example}` : "",
    ].map((value) => String(value || "").trim()).filter(Boolean);
    return parts.join("\n") || fallback;
}

export function disclosureGroup({ title, description = "", open = false,
    memory = null, key = "" } = {}) {
    const details = document.createElement("details");
    details.open = memory?.isOpen(key, open) ?? open;
    details.style.cssText = `border:1px solid ${COLORS.border};border-radius:6px;padding:0 8px;`;
    const summary = document.createElement("summary");
    summary.textContent = title;
    summary.style.cssText = "padding:7px 0;cursor:pointer;font-weight:600;";
    if (description) summary.title = description;
    const body = document.createElement("div");
    body.style.cssText = "display:flex;flex-direction:column;gap:7px;padding:0 0 8px;";
    details.addEventListener("toggle", () => memory?.remember(key, details.open));
    details.append(summary, body);
    return { details, summary, body };
}

export function identityRoutingProjection(profile = {}, placementPhases = []) {
    // `placement_label` comes from the served `placement_phases` catalog, not
    // from munging the raw value. The chip editor already renders the catalog
    // label, and two surfaces spelling one phase differently reads as a
    // disagreement between panels that in fact agree.
    const labels = new Map((placementPhases || []).map((row) => [
        String(row?.value || ""), String(row?.label || row?.value || "")]));
    return orderedReferenceDerived(profile).map(([kind, declaration]) => {
        const placement = String(declaration?.placement || "");
        return {
            kind,
            label: String(declaration?.label || kind),
            channel_key: String(declaration?.channel_key || ""),
            placement,
            placement_label: labels.get(placement)
                || placement.replaceAll("_", " "),
            description: String(declaration?.description || ""),
            help: String(declaration?.help || ""),
            example: String(declaration?.example || ""),
        };
    });
}

export function openIdentityEditor({ identity = null, seedSource = null, profile, references,
    semanticUnits, assets, disclosureMemory = null, placementPhases = [],
    attachmentCount = 0, candidate = null, initialKind = "",
    // Opened from Attach: Save hands the identity back instead of writing it,
    // and the chip's Attach commits both in one gesture.
    pendingCreate = false,
    confirmDismiss = null, onSave, onDelete, onClose, onError }) {
    const opener = document.activeElement;
    const backdrop = document.createElement("div");
    backdrop.dataset.promptIdentityEditor = "1";
    backdrop.style.cssText = "position:fixed;inset:0;z-index:12010;background:rgba(5,8,12,.72);display:flex;align-items:center;justify-content:center;padding:20px;";
    const modal = document.createElement("div");
    modal.style.cssText = `width:min(720px,94vw);max-height:86vh;overflow:auto;padding:14px;border:1px solid ${COLORS.border};border-radius:8px;background:${COLORS.panelRaised};display:flex;flex-direction:column;gap:8px;color:${COLORS.text};font:10px system-ui;`;
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");
    const title = document.createElement("strong");
    title.id = `sonder-prompt-identity-title-${uid()}`;
    modal.setAttribute("aria-labelledby", title.id);
    title.textContent = identity ? "Edit prompt identity" : "Create prompt identity";
    title.style.fontSize = "13px";
    const kinds = (profile?.identity_kinds || []).filter((value) => value?.key);
    const kind = makeSelect(kinds.map((value) => [String(value.key),
        String(value.label || value.key)]), identity?.kind || initialKind || kinds[0]?.key || "");
    const handle = document.createElement("input");
    handle.style.cssText = chromeInputCss();
    handle.placeholder = "Unique handle";
    const name = document.createElement("input");
    name.style.cssText = chromeInputCss();
    name.placeholder = "Identity name";
    const seededName = seedSource?.member?.handle || seedSource?.row?.display_name
        || seedSource?.reference?.name || "";
    name.value = identity?.name || seededName;
    handle.value = identity?.handle || derivePromptHandleSuggestion(name.value, "Identity");
    // Seeding once meant the description-only path — no physical source, so no
    // name yet at open — kept the literal fallback "Identity" no matter what
    // was typed. Track Name until the author touches the handle themselves.
    let handleFollowsName = !identity?.handle;
    handle.addEventListener("input", () => {
        handleFollowsName = false;
        const sanitized = sanitizePromptHandle(handle.value);
        if (sanitized !== handle.value) {
            const caret = Math.max(0, (handle.selectionStart ?? sanitized.length)
                - (handle.value.length - sanitized.length));
            handle.value = sanitized;
            handle.setSelectionRange?.(caret, caret);
        }
    });
    name.addEventListener("input", () => {
        if (!handleFollowsName) return;
        handle.value = derivePromptHandleSuggestion(name.value, "Identity");
    });
    const definition = document.createElement("textarea");
    definition.rows = 3;
    definition.style.cssText = chromeInputCss();
    // A blank definition follows its sources' Library prose, so a new identity
    // seeded from a member shows that prose rather than an empty prompt.
    const seededProse = !identity ? String(seedSource?.member?.prompt || "").trim() : "";
    definition.placeholder = seededProse
        ? `${seededProse} (from the Library member; leave blank to follow it)`
        : "Describe this identity for prompt use...";
    definition.value = identity?.definition || "";
    const derivedDeclarations = Object.fromEntries(orderedReferenceDerived(profile));
    const visualDeclaration = referenceFieldDeclaration(
        profile, "retention", "visual_intent");
    const audioDeclaration = referenceFieldDeclaration(
        profile, "retention", "audio_intent");
    const visual = visualDeclaration ? makeSelect(
        intentValues(profile, "visual_intent"), identity?.visual_intent || "preserve") : null;
    const audio = audioDeclaration ? makeSelect(
        intentValues(profile, "audio_intent"),
        identity?.audio_intent || "reference_characteristics") : null;
    const attachmentDefaults = identity?.attachment_defaults
        && typeof identity.attachment_defaults === "object"
        ? identity.attachment_defaults : {};
    const summaryDefault = document.createElement("textarea");
    summaryDefault.rows = 2; summaryDefault.style.cssText = chromeInputCss();
    summaryDefault.placeholder = "Default section summary (optional)";
    summaryDefault.value = attachmentDefaults.summary || "";
    const retentionDefault = document.createElement("textarea");
    retentionDefault.rows = 2; retentionDefault.style.cssText = chromeInputCss();
    retentionDefault.placeholder = "Default preservation detail (optional)";
    retentionDefault.value = attachmentDefaults.retention_detail || "";
    const audioRetentionDefault = document.createElement("textarea");
    audioRetentionDefault.rows = 2;
    audioRetentionDefault.style.cssText = chromeInputCss();
    audioRetentionDefault.placeholder = "Default audio preservation detail (optional)";
    audioRetentionDefault.value = attachmentDefaults.audio_retention_detail || "";
    const audioDefinitionDefault = document.createElement("textarea");
    audioDefinitionDefault.rows = 2; audioDefinitionDefault.style.cssText = chromeInputCss();
    audioDefinitionDefault.placeholder = "Default audio definition (optional)";
    audioDefinitionDefault.value = attachmentDefaults.audio_definition || "";
    // Which prompt parts this identity contributes at all. Sparse: an empty
    // list is the ordinary "everything the format declares".
    const storedDisabledCapabilities = new Set(
        (identity?.disabled_capabilities || []).map(String));
    const capabilityBoxes = new Map();
    const capabilityParts = document.createElement("div");
    capabilityParts.dataset.sonderIdentityCapabilityDefaults = "1";
    capabilityParts.style.cssText = "display:flex;flex-wrap:wrap;gap:8px;align-items:center;";
    for (const [capabilityId, declaration] of orderedReferenceDerived(profile)) {
        const item = document.createElement("label");
        item.style.cssText = "display:flex;gap:4px;align-items:center;";
        const box = document.createElement("input");
        box.type = "checkbox";
        box.checked = !storedDisabledCapabilities.has(capabilityId);
        box.setAttribute("aria-label",
            `Contribute ${declaration?.label || capabilityId} by default`);
        item.title = String(declaration?.help
            || "New attachments follow this; a chip may still override it.");
        capabilityBoxes.set(capabilityId, box);
        item.append(box, String(declaration?.label || capabilityId));
        capabilityParts.appendChild(item);
    }
    const taskDeclaration = referenceFieldDeclaration(
        profile, "summary", "task_types");
    const sourceMap = new Map((identity?.sources || []).map((source) => [
        `${source.entity_id}:${source.member_id}`, source,
    ]));
    if (seedSource?.reference && seedSource?.member) {
        const key = `${seedSource.reference.reference_id}:${seedSource.member.member_id}`;
        if (!sourceMap.has(key)) sourceMap.set(key, {
            entity_id: seedSource.reference.reference_id,
            member_id: seedSource.member.member_id,
            contribution: "",
        });
    }
    const sourceRows = [];
    const sources = document.createElement("div");
    sources.style.cssText = `display:flex;flex-direction:column;gap:4px;max-height:220px;overflow:auto;padding:6px;border:1px solid ${COLORS.border};border-radius:5px;`;
    const sourceGroup = (key, title, description) => {
        const group = document.createElement("div");
        group.dataset.sonderIdentitySourceGroup = key;
        group.style.cssText = `display:flex;flex-direction:column;gap:4px;padding:5px;border:1px solid ${COLORS.border};border-radius:5px;`;
        const heading = document.createElement("strong");
        heading.textContent = title;
        const help = document.createElement("span");
        help.textContent = description;
        help.style.cssText = `font:9px/1.3 system-ui;color:${COLORS.textDim};`;
        const body = document.createElement("div");
        body.style.cssText = "display:flex;flex-direction:column;gap:4px;";
        group.append(heading, help, body);
        sources.appendChild(group);
        return body;
    };
    const appearanceSources = sourceGroup(
        "appearance", "Appearance sources",
        "Images and ordinary videos declare appearance, motion, and composition.");
    const audioSources = sourceGroup(
        "audio", "Voice / audio sources",
        "Audio sources declare this character's voice.");
    const missingSources = sourceGroup(
        "missing", "Missing saved sources",
        "Saved relationships whose physical Reference is no longer available.");
    const sourceSearch = document.createElement("input");
    sourceSearch.type = "search";
    sourceSearch.placeholder = "Search physical References…";
    sourceSearch.style.cssText = chromeInputCss();
    for (const reference of references || []) {
        for (const member of reference.members || []) {
            const key = `${reference.reference_id}:${member.member_id}`;
            const current = sourceMap.get(key) || {};
            const row = document.createElement("div");
            row.style.cssText = "display:grid;grid-template-columns:auto minmax(120px,1fr) minmax(130px,.8fr);gap:5px;align-items:center;";
            const enabled = document.createElement("input"); enabled.type = "checkbox";
            enabled.checked = sourceMap.has(key);
            const label = document.createElement("span");
            label.textContent = promptReferencePickerLabel(reference, member);
            const asset = (assets || []).find((value) =>
                String(value?.asset_id || "") === String(member.asset_id || ""));
            const groupBody = promptIdentitySourceGroup(
                asset?.asset_type, reference, member) === "audio"
                ? audioSources : appearanceSources;
            const populationKeys = (profile?.physical_populations || [])
                .filter((value) => (value?.media_kinds || []).includes(asset?.asset_type))
                .map((value) => String(value.key || "")).filter(Boolean);
            const contributions = [["", "Unattributed"],
                ...contributionValues(profile, populationKeys)];
            const contribution = makeSelect(contributions, current.contribution || "");
            row.append(enabled, label, contribution);
            groupBody.appendChild(row);
            sourceRows.push({ enabled, contribution, reference, member,
                element: row, searchText: `${member.handle || ""} ${member.name || ""} ${reference.name || ""}`.toLocaleLowerCase() });
        }
    }
    const resolvedSourceKeys = new Set(sourceRows.map((row) =>
        `${row.reference.reference_id}:${row.member.member_id}`));
    for (const [key, current] of sourceMap) {
        if (resolvedSourceKeys.has(key)) continue;
        const row = document.createElement("div");
        row.style.cssText = "display:grid;grid-template-columns:auto minmax(120px,1fr) minmax(130px,.8fr);gap:5px;align-items:center;";
        const enabled = document.createElement("input");
        enabled.type = "checkbox"; enabled.checked = true;
        const label = document.createElement("span");
        label.textContent = `Missing physical source · ${current.entity_id || "?"}:${current.member_id || "?"}`;
        label.style.color = COLORS.dangerText;
        const contribution = makeSelect([["", "Unattributed"],
            ...contributionValues(profile)], current.contribution || "");
        row.append(enabled, label, contribution);
        missingSources.appendChild(row);
        sourceRows.push({ enabled, contribution,
            reference: { reference_id: current.entity_id || "" },
            member: { member_id: current.member_id || "" },
            element: row, searchText: label.textContent.toLocaleLowerCase() });
    }
    if (!missingSources.children.length) {
        missingSources.parentElement.style.display = "none";
    }
    if (!sourceRows.length) {
        const empty = document.createElement("span");
        empty.textContent = "No physical references exist. This identity can still use its authored description.";
        empty.style.color = COLORS.textDim;
        sources.appendChild(empty);
    }
    const filterSources = () => {
        const query = sourceSearch.value.trim().toLocaleLowerCase();
        for (const row of sourceRows) {
            row.element.style.display = row.enabled.checked
                || (query && row.searchText.includes(query)) ? "grid" : "none";
        }
    };
    sourceSearch.addEventListener("input", filterSources);
    sourceRows.forEach((row) => row.enabled.addEventListener("change", filterSources));
    filterSources();
    // Sparse like the chip: an identity with no stored list follows its
    // members or the format, else staged roles, and saving it untouched keeps
    // it that way. The automatic checks are the CURRENT scene's staged roles,
    // since an identity is project-wide and roles are staged per window.
    const taskTypeChoices = taskDeclaration ? createTaskTypeChoiceControl({
        declaration: taskDeclaration,
        label: String(taskDeclaration.label || "Summary task types"),
        local: Object.hasOwn(attachmentDefaults, "task_types")
            ? { present: true, value: attachmentDefaults.task_types }
            : { present: false },
        preview: captureTaskTypePreview(candidate),
        automaticLabel: TASK_TYPE_PREVIEW_LABEL,
        localLabel: "this identity",
        inherited: () => {
            // The tier below the identity: its contributing members, then
            // the format. Resolved through the same browser mirror the chip
            // uses, with this identity's own stored list set aside.
            // Sources are the ones ticked in THIS dialog, so "Following" and
            // "Returns to" track an unsaved source edit, and a new identity
            // seeded from a member already inherits from it.
            const unitId = String(identity?.semantic_unit_id || "draft-identity");
            const below = [{
                ...(identity || {}),
                semantic_unit_id: unitId,
                attachment_defaults: Object.fromEntries(Object.entries(
                    attachmentDefaults).filter(([field]) => field !== "task_types")),
                sources: sourceRows.filter((row) => row.enabled.checked)
                    .map((row) => ({ entity_id: row.reference.reference_id,
                        member_id: row.member.member_id })),
            }];
            const inherited = referencePromptDefaults(unitId, {
                references, semanticUnits: below, profile,
                capabilityKind: "summary",
            });
            return {
                value: inherited.values?.task_types,
                label: String(inherited.fieldSources?.task_types?.label
                    || inherited.formatSource || ""),
            };
        },
    }) : null;
    const taskTypesDefault = taskTypeChoices?.element || null;
    sourceRows.forEach((row) => row.enabled.addEventListener("change",
        () => taskTypeChoices?.refresh()));
    // Temporary repair UI for project data authored before voice became a
    // normal physical source. Remove with the server tolerance after this
    // batch has been in use and this affordance stops appearing.
    const legacyVoiceRepair = identity
        ? legacyVoiceBindingRepair(identity, references) : null;
    const requiredNotice = document.createElement("div");
    requiredNotice.dataset.sonderIdentityRequired = "1";
    requiredNotice.style.cssText = `display:none;padding:6px 8px;border:1px solid ${COLORS.dangerBorder};border-radius:5px;color:${COLORS.dangerText};background:${COLORS.dangerSoft};`;
    const footer = document.createElement("div");
    footer.style.cssText = "display:flex;justify-content:flex-end;gap:6px;";
    const cancel = makeButton("Cancel");
    const save = pendingCreate
        ? makeButton("Use for this attachment",
            "Back to Attach. The identity is created when you click Attach there.", "primary")
        : makeButton("Save identity", "Persist this project identity", "primary");
    const remove = identity ? makeButton("Delete", "Delete after dependency disclosure", "danger") : null;
    let unregisterKeys = () => {};
    // `close` is ALSO returned as this modal's programmatic cleanup handle and
    // invoked during teardown — when another editor opens, or when the panel
    // unmounts. The dirty guard therefore lives on the two accidental
    // dismissal gestures below, never inside `close` itself.
    const close = () => {
        unregisterKeys();
        backdrop.remove();
        opener?.focus?.();
        onClose?.();
    };
    const draftGuard = createModalDraftGuard({
        controls: () => [kind, handle, name, definition,
            visual, audio, summaryDefault, retentionDefault,
            audioRetentionDefault, audioDefinitionDefault, taskTypesDefault,
            ...capabilityBoxes.values()],
        confirm: confirmDismiss,
        message: "Discard this prompt identity draft? Your unsaved changes will be lost.",
    });
    // Cancel is explicit intent and is never guarded.
    cancel.addEventListener("click", close);
    backdrop.addEventListener("click", (event) => {
        if (event.target === backdrop && draftGuard.confirmDismiss()) close();
    });
    const persistIdentity = async ({ repairLegacy = false } = {}) => {
        const missing = [
            [kind, "Kind", String(kind.value || "").trim()],
            [handle, "Handle", handle.value.trim()],
            [name, "Name", name.value.trim()],
        ].filter(([, , value]) => !value);
        for (const control of [kind, handle, name]) control.removeAttribute?.("aria-invalid");
        if (missing.length) {
            requiredNotice.textContent = `Complete the required fields: ${missing
                .map(([, label]) => label).join(", ")}.`;
            requiredNotice.style.display = "block";
            missing.forEach(([control]) => control.setAttribute("aria-invalid", "true"));
            missing[0][0].focus?.();
            return false;
        }
        requiredNotice.style.display = "none";
        const taskTypeRefusal = taskTypeChoices?.validate() || "";
        taskTypeChoices?.showError(taskTypeRefusal);
        if (taskTypeRefusal) {
            // The group lives in a collapsed disclosure by default; a refusal
            // the author cannot see reads as a Save button that does nothing.
            advancedGroup.details.open = true;
            return false;
        }
        const nextAttachmentDefaults = { ...attachmentDefaults };
        if (derivedDeclarations.summary) {
            nextAttachmentDefaults.summary = summaryDefault.value;
        }
        if (derivedDeclarations.retention) {
            nextAttachmentDefaults.retention_detail = retentionDefault.value;
            nextAttachmentDefaults.audio_retention_detail =
                audioRetentionDefault.value;
        }
        if (derivedDeclarations.definitions) {
            nextAttachmentDefaults.audio_definition = audioDefinitionDefault.value;
        }
        // Untouched, the stored key stays exactly as loaded — absent, empty or
        // holding unsupported values — via the spread above.
        if (taskTypeChoices?.isTouched()) {
            if (taskTypeChoices.hasLocal()) {
                nextAttachmentDefaults.task_types = taskTypeChoices.value();
            } else {
                delete nextAttachmentDefaults.task_types;
            }
        }
        const next = {
            ...(identity || {}),
            semantic_unit_id: identity?.semantic_unit_id || `identity:${uid()}`,
            handle: handle.value.trim(), name: name.value.trim(), kind: kind.value,
            order: identity?.order ?? semanticUnits.length,
            definition: definition.value,
            attachment_defaults: nextAttachmentDefaults,
            sources: sourceRows.filter((row) => row.enabled.checked).map((row) => ({
                entity_id: row.reference.reference_id,
                member_id: row.member.member_id,
                contribution: row.contribution.value,
            })),
            // Preserve ids this format does not declare: the identity may also
            // be used under another format that owns them.
            disabled_capabilities: [
                ...[...storedDisabledCapabilities].filter(
                    (id) => !capabilityBoxes.has(id)),
                ...[...capabilityBoxes].filter(([, box]) => !box.checked)
                    .map(([id]) => id),
            ],
        };
        if (visual) next.visual_intent = visual.value;
        if (audio) next.audio_intent = audio.value;
        if (repairLegacy) delete next.voice;
        try {
            await onSave?.(next);
            close();
            return true;
        } catch (error) {
            onError?.(error);
            return false;
        }
    };
    save.addEventListener("click", () => persistIdentity());
    remove?.addEventListener("click", async () => {
        try { if (await onDelete?.(identity)) close(); } catch (error) { onError?.(error); }
    });
    footer.append(cancel); if (remove) footer.append(remove); footer.append(save);
    const core = document.createElement("div");
    core.dataset.sonderIdentityGroup = "core";
    core.style.cssText = `display:flex;flex-direction:column;gap:7px;border:1px solid ${COLORS.border};border-radius:6px;padding:8px;`;
    const coreTitle = document.createElement("strong");
    coreTitle.textContent = "Core identity";
    const kindDescription = document.createElement("span");
    kindDescription.style.cssText = `font:9px/1.35 system-ui;color:${COLORS.textDim};`;
    const refreshKindDescription = () => {
        const declaration = kinds.find((value) =>
            String(value?.key || "") === String(kind.value || ""));
        kindDescription.textContent = String(declaration?.description
            || "Choose how this Prompt Format names and emits the identity.");
    };
    kind.addEventListener("change", refreshKindDescription);
    refreshKindDescription();
    core.append(coreTitle,
        fieldRow("Kind", kind, { required: true }), kindDescription,
        fieldRow("Handle", handle, {
            required: true,
            help: `User-language @handle used while authoring prompts. ${
                PROMPT_HANDLE_RULE}${pendingCreate
                ? " A suggestion until you Attach: if another Reference or identity already uses it, a number is added."
                : ""}`,
        }),
        fieldRow("Name", name, { required: true }),
        fieldRow("Definition", definition, {
            help: `${declarationGuidance(derivedDeclarations.definitions,
                "Description emitted when this identity participates in a prompt.")}${
                (profile?.physical_populations || []).length
                    ? "\nSource labels such as <Picture 1> are added for you; describe only what it is."
                    : ""}`,
        }));

    const physicalGroup = disclosureGroup({
        title: "Physical sources — optional",
        description: "Attach media that contributes appearance, motion, composition, or voice traits.",
        open: Boolean(seedSource || sourceMap.size || legacyVoiceRepair),
        memory: disclosureMemory,
        key: "physical_sources",
    });
    if (legacyVoiceRepair) physicalGroup.details.open = true;
    physicalGroup.body.append(sourceSearch, sources);
    if (legacyVoiceRepair) {
        const repair = document.createElement("div");
        repair.dataset.sonderLegacyVoiceRepair = "1";
        repair.style.cssText = `display:flex;align-items:center;justify-content:space-between;gap:8px;padding:6px 8px;border:1px solid ${COLORS.border};border-radius:5px;`;
        const repairText = document.createElement("span");
        repairText.textContent = `Legacy voice binding for @${
            identity.handle || identity.name || identity.semantic_unit_id} — add ${
            promptReferencePickerLabel(legacyVoiceRepair.reference,
                legacyVoiceRepair.member)} as a physical source.`;
        const repairButton = makeButton("Add physical source", "Repair this retired binding in one save", "accentSoft");
        repairButton.addEventListener("click", async () => {
            setButtonDisabled(repairButton, true);
            const sourceRow = sourceRows.find((row) =>
                String(row.member?.member_id || "")
                    === String(legacyVoiceRepair.member?.member_id || ""));
            if (sourceRow) {
                sourceRow.enabled.checked = true;
                sourceRow.contribution.value = "";
            }
            if (!await persistIdentity({ repairLegacy: true })) {
                setButtonDisabled(repairButton, false);
            }
        });
        repair.append(repairText, repairButton);
        physicalGroup.body.prepend(repair);
    }

    // Name the rung and say who consumes it. This group and the chip's own
    // fieldset are the same declared fields at two levels of one ladder, but
    // only the chip end was labelled, so the pair read as two copies of one
    // panel with no arrow between them.
    const advancedGroup = disclosureGroup({
        title: attachmentCount
            ? `Defaults for all attachments (${attachmentCount} following)`
            : "Defaults for all attachments",
        description: "Every attachment of this identity starts from these; a chip stores only what it deviates on.",
        open: false, memory: disclosureMemory, key: "attachment_defaults",
    });
    if (capabilityBoxes.size) advancedGroup.body.append(fieldRow(
        "Contributes", capabilityParts, {
            help: "Prompt parts new attachments inherit; a chip may still override one.",
        }));
    if (visual) advancedGroup.body.append(fieldRow(
        visualDeclaration.label || "Visual default", visual, {
            help: declarationGuidance(visualDeclaration,
                "Default visual preservation intent."),
        }));
    if (audio) advancedGroup.body.append(fieldRow(
        audioDeclaration.label || "Audio default", audio, {
            help: declarationGuidance(audioDeclaration,
                "Default audio preservation intent."),
        }));
    if (derivedDeclarations.summary) advancedGroup.body.append(fieldRow(
        "Summary default", summaryDefault,
        { help: declarationGuidance(derivedDeclarations.summary,
            "Inherited Summary prose for chips with no local override.") }));
    if (derivedDeclarations.retention) advancedGroup.body.append(fieldRow(
        "Retention default", retentionDefault,
        { help: declarationGuidance(derivedDeclarations.retention,
            "Inherited preservation detail for chips with no local override.") }));
    if (derivedDeclarations.retention) advancedGroup.body.append(fieldRow(
        "Audio retention default", audioRetentionDefault,
        { help: declarationGuidance(derivedDeclarations.retention,
            "Inherited audio preservation detail for chips with no local override.") }));
    if (derivedDeclarations.definitions) advancedGroup.body.append(fieldRow(
        "Audio definition default", audioDefinitionDefault,
        { help: declarationGuidance(derivedDeclarations.definitions,
            "Inherited voice/audio definition for chips with no local override.") }));
    if (taskTypesDefault) {
        advancedGroup.body.append(fieldRow(
            taskDeclaration.label || "Summary task types", taskTypesDefault, {
                help: declarationGuidance(taskDeclaration,
                    "Format-declared Summary categories inherited by new chips."),
                group: true,
            }));
    }
    const ownedDefaultFields = new Set([
        ...(derivedDeclarations.summary ? ["summary"] : []),
        ...(derivedDeclarations.retention
            ? ["retention_detail", "audio_retention_detail"] : []),
        ...(derivedDeclarations.definitions ? ["audio_definition"] : []),
        ...(taskDeclaration ? ["task_types"] : []),
    ]);
    const preservedDefaults = Object.keys(attachmentDefaults).filter((field) =>
        !ownedDefaultFields.has(field));
    if (preservedDefaults.length) {
        const warning = document.createElement("span");
        warning.dataset.sonderPreservedIdentityDefaults = "1";
        warning.textContent = `Saved defaults not used by this Prompt Format are preserved: ${
            preservedDefaults.join(", ")}.`;
        warning.style.cssText = `font:9px/1.35 system-ui;color:${COLORS.dangerText};`;
        advancedGroup.body.appendChild(warning);
    }

    const routingGroup = disclosureGroup({
        title: "Format defaults — derived output routing",
        description: "Read-only Prompt Format defaults. Individual chips may override them.",
        open: false, memory: disclosureMemory, key: "derived_routing",
    });
    const routingIntro = document.createElement("span");
    routingIntro.textContent = `Prompt Format default${profile?.name ? ` · ${profile.name}` : ""}; these are not chip overrides.`;
    routingIntro.style.cssText = `font:9px/1.35 system-ui;color:${COLORS.textDim};`;
    routingGroup.body.appendChild(routingIntro);
    const routing = identityRoutingProjection(profile, placementPhases);
    for (const row of routing) {
        const line = document.createElement("div");
        line.dataset.sonderIdentityRoutingKind = row.kind;
        line.style.cssText = "display:grid;grid-template-columns:minmax(110px,.7fr) minmax(0,1fr);gap:8px;";
        const label = document.createElement("span");
        label.textContent = row.label;
        const destination = document.createElement("span");
        destination.textContent = `→ ${row.channel_key || "unresolved"} · ${
            row.placement_label || "unresolved"}`;
        destination.style.color = COLORS.textDim;
        line.title = declarationGuidance(row,
            "This Prompt Format declares the destination and placement.");
        line.append(label, destination);
        routingGroup.body.appendChild(line);
    }
    if (!routing.length) {
        routingGroup.body.appendChild(document.createTextNode(
            "This Prompt Format declares no Reference-derived outputs."));
    }

    modal.append(title, requiredNotice, core, physicalGroup.details,
        advancedGroup.details, routingGroup.details, footer);
    backdrop.appendChild(modal);
    document.body.appendChild(backdrop);
    unregisterKeys = registerKeyboardConsumer({
        id: `sonder-prompt-identity-${uid()}`,
        priority: KEY_PRIORITY.OVERLAY,
        keydown: (event) => {
            if (event.isComposing === true || event.keyCode === 229) return false;
            if (event.key !== "Escape") return false;
            // Claim the key either way. Returning false after a cancelled
            // confirm would pass Escape to the Prompt panel's own OVERLAY
            // consumer, closing the panel out from under this modal — the
            // opposite of what the user just asked for.
            if (draftGuard.confirmDismiss()) close();
            return true;
        },
    });
    return close;
}
