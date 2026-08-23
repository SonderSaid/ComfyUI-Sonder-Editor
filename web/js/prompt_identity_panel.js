// Reference Prompting + Identity Prompting projection.
// The Prompt panel host owns state, networking, and durable commits. This
// module owns only the derived DOM/listeners and returns a cleanup callback.

import { CHROME_DIM_PLACEHOLDER_CLASS, EDITOR_COLORS as COLORS, chromeInputCss,
    installChromePlaceholderStyles, setButtonDisabled,
    setButtonVariant } from "./editor_theme.js";
import { createReferenceOverrideFieldset,
    normalizePromptAttachment } from "./prompt_context_chips.js";
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
        audio_speaker_bindings: 0,
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
        if (String(value.config?.audio_speaker_subject_id || "") === identityId) {
            counts.audio_speaker_bindings += 1;
        }
        Object.values(value).forEach(visit);
    };
    scenes.forEach(visit);
    return counts;
}

function makeButton(label, title = "", variant = "", ariaLabel = "") {
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

function makeSelect(values, selected = "") {
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

function contributionValues(profile, populations = ["*"]) {
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

function intentValues(profile, field, { includeInherited = false } = {}) {
    const values = declaredFieldChoices(referenceFieldDeclaration(
        profile, "retention", field)).map((value) => [value.value, value.label]);
    return includeInherited
        ? [["", "Inherit Reference default"], ...values]
        : values;
}

function fieldRow(label, input, { help = "", required = false } = {}) {
    const row = document.createElement("label");
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

function declarationGuidance(declaration, fallback = "") {
    const parts = [
        declaration?.description,
        declaration?.help,
        declaration?.example ? `Example: ${declaration.example}` : "",
    ].map((value) => String(value || "").trim()).filter(Boolean);
    return parts.join("\n") || fallback;
}

function disclosureGroup({ title, description = "", open = false,
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

function makeMultiSelect(values, selected = []) {
    const saved = new Set((Array.isArray(selected) ? selected : [])
        .map(String).filter(Boolean));
    const options = [...(values || [])];
    for (const value of saved) {
        if (!options.some(([known]) => String(known) === value)) {
            options.push([value, `Unsupported saved value: ${value}`]);
        }
    }
    const select = makeSelect(options, "");
    select.multiple = true;
    select.size = Math.min(6, Math.max(2, options.length));
    [...select.options].forEach((option) => {
        option.selected = saved.has(String(option.value));
    });
    if ([...saved].some((value) => !values.some(([known]) => String(known) === value))) {
        select.dataset.sonderInvalid = "1";
    }
    return select;
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
    disclosureMemory = null, confirmDismiss = null }) {
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

function openIdentityEditor({ identity = null, seedSource = null, profile, references,
    semanticUnits, assets, disclosureMemory = null, placementPhases = [],
    attachmentCount = 0,
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
        String(value.label || value.key)]), identity?.kind || kinds[0]?.key || "");
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
    definition.placeholder = "Describe this identity for prompt use...";
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
    const taskTypesDefault = makeMultiSelect(
        declaredFieldChoices(taskDeclaration).map((value) => [value.value, value.label]),
        attachmentDefaults.task_types || []);
    const sourceMap = new Map((identity?.sources || []).map((source) => [
        `${source.entity_id}:${source.member_id}`, source,
    ]));
    if (seedSource?.reference && seedSource?.member) {
        const key = `${seedSource.reference.reference_id}:${seedSource.member.member_id}`;
        if (!sourceMap.has(key)) sourceMap.set(key, {
            entity_id: seedSource.reference.reference_id,
            member_id: seedSource.member.member_id,
            contribution: "", inherit_description: false,
        });
    }
    const sourceRows = [];
    const sources = document.createElement("div");
    sources.style.cssText = `display:flex;flex-direction:column;gap:4px;max-height:220px;overflow:auto;padding:6px;border:1px solid ${COLORS.border};border-radius:5px;`;
    const sourceSearch = document.createElement("input");
    sourceSearch.type = "search";
    sourceSearch.placeholder = "Search physical References…";
    sourceSearch.style.cssText = chromeInputCss();
    for (const reference of references || []) {
        for (const member of reference.members || []) {
            const key = `${reference.reference_id}:${member.member_id}`;
            const current = sourceMap.get(key) || {};
            const row = document.createElement("div");
            row.style.cssText = "display:grid;grid-template-columns:auto minmax(120px,1fr) minmax(130px,.8fr) auto;gap:5px;align-items:center;";
            const enabled = document.createElement("input"); enabled.type = "checkbox";
            enabled.checked = sourceMap.has(key);
            const label = document.createElement("span");
            label.textContent = promptReferencePickerLabel(reference, member);
            const asset = (assets || []).find((value) =>
                String(value?.asset_id || "") === String(member.asset_id || ""));
            const populationKeys = (profile?.physical_populations || [])
                .filter((value) => (value?.media_kinds || []).includes(asset?.asset_type))
                .map((value) => String(value.key || "")).filter(Boolean);
            const contributions = [["", "Unattributed"],
                ...contributionValues(profile, populationKeys)];
            const contribution = makeSelect(contributions, current.contribution || "");
            const inheritLabel = document.createElement("label");
            const inherit = document.createElement("input"); inherit.type = "checkbox";
            inherit.checked = current.inherit_description === true;
            inheritLabel.append(inherit, " Inherit description");
            row.append(enabled, label, contribution, inheritLabel);
            sources.appendChild(row);
            sourceRows.push({ enabled, contribution, inherit, reference, member,
                element: row, searchText: `${member.handle || ""} ${member.name || ""} ${reference.name || ""}`.toLocaleLowerCase() });
        }
    }
    const resolvedSourceKeys = new Set(sourceRows.map((row) =>
        `${row.reference.reference_id}:${row.member.member_id}`));
    for (const [key, current] of sourceMap) {
        if (resolvedSourceKeys.has(key)) continue;
        const row = document.createElement("div");
        row.style.cssText = "display:grid;grid-template-columns:auto minmax(120px,1fr) minmax(130px,.8fr) auto;gap:5px;align-items:center;";
        const enabled = document.createElement("input");
        enabled.type = "checkbox"; enabled.checked = true;
        const label = document.createElement("span");
        label.textContent = `Missing physical source · ${current.entity_id || "?"}:${current.member_id || "?"}`;
        label.style.color = COLORS.dangerText;
        const contribution = makeSelect([["", "Unattributed"],
            ...contributionValues(profile)], current.contribution || "");
        const inheritLabel = document.createElement("label");
        const inherit = document.createElement("input");
        inherit.type = "checkbox"; inherit.checked = current.inherit_description === true;
        inheritLabel.append(inherit, " Inherit description");
        row.append(enabled, label, contribution, inheritLabel);
        sources.appendChild(row);
        sourceRows.push({ enabled, contribution, inherit,
            reference: { reference_id: current.entity_id || "" },
            member: { member_id: current.member_id || "" },
            element: row, searchText: label.textContent.toLocaleLowerCase() });
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
    const voice = makeSelect([["", "No voice reference"], ...sourceRows
        .map(({ reference, member }) => [String(member.member_id),
            promptReferencePickerLabel(reference, member)])],
    identity?.voice?.member_id || "");
    const requiredNotice = document.createElement("div");
    requiredNotice.dataset.sonderIdentityRequired = "1";
    requiredNotice.style.cssText = `display:none;padding:6px 8px;border:1px solid ${COLORS.dangerBorder};border-radius:5px;color:${COLORS.dangerText};background:${COLORS.dangerSoft};`;
    const footer = document.createElement("div");
    footer.style.cssText = "display:flex;justify-content:flex-end;gap:6px;";
    const cancel = makeButton("Cancel");
    const save = makeButton("Save identity", "Persist this project identity", "primary");
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
        controls: () => [kind, handle, name, definition, voice,
            visual, audio, summaryDefault, retentionDefault,
            audioDefinitionDefault, taskTypesDefault, ...capabilityBoxes.values()],
        confirm: confirmDismiss,
        message: "Discard this prompt identity draft? Your unsaved changes will be lost.",
    });
    // Cancel is explicit intent and is never guarded.
    cancel.addEventListener("click", close);
    backdrop.addEventListener("click", (event) => {
        if (event.target === backdrop && draftGuard.confirmDismiss()) close();
    });
    save.addEventListener("click", async () => {
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
            return;
        }
        requiredNotice.style.display = "none";
        const nextAttachmentDefaults = { ...attachmentDefaults };
        if (derivedDeclarations.summary) {
            nextAttachmentDefaults.summary = summaryDefault.value;
        }
        if (derivedDeclarations.retention) {
            nextAttachmentDefaults.retention_detail = retentionDefault.value;
        }
        if (derivedDeclarations.definitions) {
            nextAttachmentDefaults.audio_definition = audioDefinitionDefault.value;
        }
        if (taskDeclaration) {
            nextAttachmentDefaults.task_types = [
                ...(taskTypesDefault.selectedOptions || []),
            ].map((option) => option.value).filter(Boolean);
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
                inherit_description: row.inherit.checked,
            })),
            voice: { member_id: voice.value || null },
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
        try { await onSave?.(next); close(); } catch (error) { onError?.(error); }
    });
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
                PROMPT_HANDLE_RULE}`,
        }),
        fieldRow("Name", name, { required: true }),
        fieldRow("Definition", definition, {
            help: declarationGuidance(derivedDeclarations.definitions,
                "Description emitted when this identity participates in a prompt."),
        }));

    const physicalGroup = disclosureGroup({
        title: "Physical sources — optional",
        description: "Attach media that contributes appearance, motion, composition, or voice traits.",
        open: Boolean(seedSource || sourceMap.size), memory: disclosureMemory,
        key: "physical_sources",
    });
    physicalGroup.body.append(sourceSearch, sources);

    const voiceGroup = disclosureGroup({
        title: "Voice — optional",
        description: "Bind one physical member as this identity's voice reference.",
        open: Boolean(identity?.voice?.member_id), memory: disclosureMemory,
        key: "voice",
    });
    voiceGroup.body.append(fieldRow("Voice reference", voice));

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
    if (derivedDeclarations.definitions) advancedGroup.body.append(fieldRow(
        "Audio definition default", audioDefinitionDefault,
        { help: declarationGuidance(derivedDeclarations.definitions,
            "Inherited voice/audio definition for chips with no local override.") }));
    if (taskDeclaration) {
        advancedGroup.body.append(fieldRow(
            taskDeclaration.label || "Summary task types", taskTypesDefault, {
                help: declarationGuidance(taskDeclaration,
                    "Format-declared Summary categories inherited by new chips."),
            }));
    }
    const ownedDefaultFields = new Set([
        ...(derivedDeclarations.summary ? ["summary"] : []),
        ...(derivedDeclarations.retention ? ["retention_detail"] : []),
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
        voiceGroup.details, advancedGroup.details, routingGroup.details, footer);
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

    container.appendChild(sectionHeading("Reference Prompting",
        "Physical media resolved from the active setup and effective window. Staging remains in Reference lanes."));
    const referenceBody = document.createElement("div");
    referenceBody.dataset.referencePrompting = "1";
    referenceBody.style.cssText = "display:flex;flex-direction:column;gap:5px;";
    container.appendChild(referenceBody);
    if (!projection.length) {
        const empty = document.createElement("div");
        empty.textContent = "This prompt format declares no physical Reference populations.";
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
        modalCleanup = openAttachmentTargetPicker({
            scene: options.scene, owner,
            profile, references, semanticUnits,
            setupManifest: candidate?.setup_manifest || {},
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
            const commitHandle = async () => {
                if (!row.member || !row.reference
                        || handle.value.trim() === String(row.member.handle || "")) return;
                try {
                    await options.mutateReferences?.([{
                        type: "update_member", reference_id: row.reference.reference_id,
                        member_id: row.member.member_id,
                        fields: { handle: handle.value.trim() },
                        expected: { handle: String(row.member.handle || "") },
                    }], "edit physical Reference handle");
                    rerender();
                } catch (error) { handle.value = row.member.handle || ""; options.onError?.(error); }
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
            defaults.addEventListener("click", () => {
                const existing = groupEl.querySelector(
                    `[data-physical-defaults='${CSS.escape(row.memberId)}']`);
                if (existing) { existing.remove(); return; }
                if (!row.member || !row.reference) return;
                const editor = document.createElement("div");
                editor.dataset.physicalDefaults = row.memberId;
                editor.style.cssText = `display:grid;grid-template-columns:minmax(160px,1fr) auto auto auto;gap:5px;padding:6px;border:1px solid ${COLORS.border};border-radius:5px;background:${COLORS.panelRaised};`;
                const prompt = document.createElement("textarea");
                prompt.rows = 2; prompt.value = row.member.prompt || "";
                prompt.placeholder = "Physical Reference prompt description";
                prompt.style.cssText = chromeInputCss();
                const visual = makeSelect(intentValues(
                    profile, "visual_intent", { includeInherited: true }),
                row.member.visual_intent || "");
                const audio = makeSelect(intentValues(
                    profile, "audio_intent", { includeInherited: true }),
                row.member.audio_intent || "");
                // Which prompt parts this Reference contributes at all is a
                // property of the Reference; WHERE each part lands stays
                // per-attachment and remains in the chip's routing panel.
                const storedDisabled = new Set(
                    (row.member.disabled_capabilities || []).map(String));
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
                    const fields = {
                        prompt: prompt.value,
                        visual_intent: visual.value,
                        audio_intent: audio.value,
                        disabled_capabilities: nextDisabled,
                    };
                    try {
                        await options.mutateReferences?.([{
                            type: "update_member", reference_id: row.reference.reference_id,
                            member_id: row.member.member_id, fields,
                            expected: {
                                prompt: String(row.member.prompt || ""),
                                visual_intent: String(row.member.visual_intent || ""),
                                audio_intent: String(row.member.audio_intent || ""),
                                disabled_capabilities: [...storedDisabled],
                            },
                        }], "edit physical Reference prompt defaults");
                        rerender();
                    } catch (error) { options.onError?.(error); }
                });
                editor.append(prompt, visual, audio, save, parts);
                rowEl.insertAdjacentElement("afterend", editor);
            });
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
            attach.addEventListener("click", () => openAttach({
                type: "physical", memberId: row.memberId,
                referenceId: row.reference?.reference_id || "",
                handle: row.member?.handle || suggestion,
                storedHandle: row.member?.handle || "",
                displayName: row.member?.name || row.reference?.name || "Reference",
                declaration: group.declaration,
            }));
            rowEl = buildPromptingRow("physical", [
                thumbnail, handle, status, attach, defaults, create,
            ]);
            rowEl.dataset.physicalMemberId = row.memberId;
            rowEl.title = [
                `Resolved: ${row.resolvedLabel}`,
                `Window: ${candidate?.window_start ?? 0}-${candidate?.window_end ?? "scene"}`,
                `Role: ${row.role || "none"}`,
                `Status: ${row.diagnostics.map((value) => value.code).join(", ") || "ready"}`,
                `Linked identities: ${row.linkedIdentities.map((value) => `@${value.handle || value.name}`).join(", ") || "none"}`,
            ].join("\n");
            groupEl.appendChild(rowEl);
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
