import {
    compatibleReferencePresets,
    createMemberDraft,
    createReferenceDraft,
    defaultReferenceClass,
    filterReferences,
    incompatibleReferencePresetTags,
    moveMember,
    normalizeReferenceTags,
    replaceMemberDraftAsset,
    serializeMemberDraft,
    serializeReferenceDraft,
    validateMemberDraft,
    validateReferenceDraft,
} from "./reference_library_model.js";

const css = {
    input: "width:100%;box-sizing:border-box;background:#151a20;border:1px solid #38414b;border-radius:6px;color:#e6ebf0;padding:6px 8px;font:11px 'Segoe UI',sans-serif;",
    button: "border:1px solid #43505c;border-radius:6px;background:#252e37;color:#e6ebf0;padding:5px 8px;font:11px 'Segoe UI',sans-serif;cursor:pointer;",
    label: "display:block;color:#98a5b2;font:10px 'Segoe UI',sans-serif;",
};

let fieldId = 0;
export const SONDER_REFERENCE_MIME = "application/x-sonder-reference";

/** The one rule for which lane kind a Reference member belongs to.
 *
 *  Shared with the timeline's drop path so the two sides cannot drift: a
 *  voice-tagged video counts as audio, everything non-audio counts as image.
 *  Returns `""` when the asset is unknown — that means "cannot classify yet",
 *  never "mixed".
 */
export function referenceMemberMediaKind(member, asset) {
    if (!asset) return "";
    const voiceTagged = (member?.tags || []).includes("sonder:voice_identity");
    return asset.asset_type === "audio"
        || (asset.asset_type === "video" && voiceTagged) ? "audio" : "image";
}

/** True only when RESOLVABLE members span both lane kinds.
 *
 *  Deliberately distinct from "some member is unresolved": a single-kind entity
 *  holding one trashed member is still draggable, and refusing it as mixed
 *  would be both wrong and misleading.
 */
export function referenceDragIsMixedKind(members, assets = []) {
    const byId = new Map((assets || []).map((asset) => [
        String(asset?.asset_id || ""), asset]));
    const kinds = new Set();
    for (const member of members || []) {
        const kind = referenceMemberMediaKind(
            member, byId.get(String(member?.asset_id || "")));
        if (kind) kinds.add(kind);
    }
    return kinds.size > 1;
}

let activeReferenceDrag = null;

export function getActiveReferenceDrag() {
    return activeReferenceDrag;
}

function el(tag, text = "", style = "") {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (style) node.style.cssText = style;
    return node;
}

function assetName(asset) {
    return asset?.name || String(asset?.path || "").split(/[/\\]/).pop() || asset?.asset_id || "Missing asset";
}

function fieldShell(label, help = "") {
    const wrap = el("div", "", `${css.label}display:flex;flex-direction:column;gap:5px;min-width:0;`);
    const heading = el("span", "", "position:relative;display:flex;align-items:center;gap:5px;min-height:16px;");
    heading.appendChild(el("span", label));
    let helpButton = null;
    if (help) {
        const helpId = `sonder-reference-help-${++fieldId}`;
        helpButton = el("button", "?", "width:15px;height:15px;padding:0;border:1px solid #465563;border-radius:50%;background:#1b242c;color:#9eb2c2;font:9px 'Segoe UI',sans-serif;cursor:help;line-height:13px;");
        helpButton.type = "button";
        helpButton.setAttribute("aria-label", `${label}: ${help}`);
        const description = el("span", help, "display:none;position:absolute;left:18px;top:18px;z-index:20;width:min(260px,calc(100vw - 60px));padding:7px 8px;border:1px solid #4a5965;border-radius:6px;background:#202a32;color:#dce5eb;box-shadow:0 8px 22px rgba(0,0,0,.4);font-size:10px;line-height:1.4;white-space:normal;");
        description.id = helpId;
        helpButton.setAttribute("aria-describedby", helpId);
        const showHelp = () => { description.style.display = "block"; };
        const hideHelp = () => { description.style.display = "none"; };
        helpButton.addEventListener("mouseenter", showHelp);
        helpButton.addEventListener("mouseleave", hideHelp);
        helpButton.addEventListener("focus", showHelp);
        helpButton.addEventListener("blur", hideHelp);
        helpButton.addEventListener("click", (event) => { event.preventDefault(); showHelp(); });
        heading.append(helpButton, description);
    }
    wrap.appendChild(heading);
    return { wrap, helpButton };
}

function inputField(label, value, onInput, { type = "text", placeholder = "", help = "", step = "" } = {}) {
    const { wrap } = fieldShell(label, help);
    const input = el("input", "", css.input);
    input.type = type;
    input.setAttribute("aria-label", label);
    input.value = value ?? "";
    input.placeholder = placeholder;
    if (step !== "") input.step = step;
    input.addEventListener("input", () => onInput(input.value));
    wrap.appendChild(input);
    return { wrap, input };
}

export function mountReferenceLibrary(container, host) {
    const state = {
        query: "",
        selectedReferenceId: "",
        entityDraft: null,
        memberDraft: null,
        memberMode: "",
        manage: false,
        memberNotice: "",
        error: "",
        destroyed: false,
    };
    container.style.cssText = "display:flex;flex-direction:column;min-height:0;overflow:hidden;height:100%;background:#11161b;color:#e6ebf0;";

    const dragPayload = (reference, member = null) => ({
        reference_id: reference.reference_id,
        reference_name: reference.name,
        members: (member ? [member] : (reference.members || [])).map((entry) => ({
            entity_id: reference.reference_id,
            member_id: entry.member_id,
        })),
    });

    const beginReferenceDrag = (event, payload, { assets = [], members = [] } = {}) => {
        if (!payload.members.length) {
            event.preventDefault();
            return;
        }
        // Refuse a genuinely mixed-kind entity here rather than after the drop:
        // a Reference lane's `media_kind` is hard, so no lane can ever accept
        // one, and carrying it to a lane only to be told no is wasted effort.
        // Members whose asset is missing or trashed are NOT mixed — they simply
        // cannot be classified yet, and blocking their drag would strand a
        // perfectly valid single-kind entity behind a wrong explanation.
        if (referenceDragIsMixedKind(members, assets)) {
            event.preventDefault();
            host.notify?.("Stage image/video references separately from voice-reference audio.");
            return;
        }
        activeReferenceDrag = payload;
        event.dataTransfer.effectAllowed = "copy";
        event.dataTransfer.setData(SONDER_REFERENCE_MIME, JSON.stringify(payload));
        event.dataTransfer.setData("text/plain", payload.reference_name || "Reference");
    };

    const endReferenceDrag = () => { activeReferenceDrag = null; };

    const openTimelineMenu = (event, payload) => {
        event.preventDefault();
        event.stopPropagation();
        document.querySelector('[data-reference-timeline-menu="true"]')?.remove();
        const menu = el("div", "", "position:fixed;z-index:10020;padding:4px;border:1px solid #43505c;border-radius:6px;background:#182028;box-shadow:0 8px 22px rgba(0,0,0,.45);");
        menu.dataset.referenceTimelineMenu = "true";
        menu.style.left = `${event.clientX}px`;
        menu.style.top = `${event.clientY}px`;
        const add = el("button", "Add to timeline", `${css.button}border:0;background:transparent;display:block;width:100%;text-align:left;`);
        add.disabled = !payload.members.length;
        add.addEventListener("click", () => { menu.remove(); host.addToTimeline?.(payload); });
        menu.appendChild(add);
        document.body.appendChild(menu);
        setTimeout(() => document.addEventListener("pointerdown", () => menu.remove(), { once: true }), 0);
    };

    const reset = () => {
        state.query = "";
        state.selectedReferenceId = "";
        state.entityDraft = null;
        state.memberDraft = null;
        state.memberMode = "";
        state.manage = false;
        state.memberNotice = "";
        state.error = "";
        render();
    };

    const perform = async (operations) => {
        state.error = "";
        try {
            await host.mutate(operations);
            state.entityDraft = null;
            state.memberDraft = null;
            state.memberMode = "";
            state.memberNotice = "";
        } catch (error) {
            state.error = error?.message || "Reference change failed.";
        }
        render();
    };

    const renderEntityEditor = (body, reference) => {
        const draft = state.entityDraft;
        const editor = el("div", "", "display:flex;flex-direction:column;gap:11px;padding:10px;border:1px solid #303a43;border-radius:8px;background:#151b21;");
        editor.appendChild(el("div", reference ? "Edit Reference" : "New Reference", "font-size:12px;font-weight:700;color:#e4ebf1;margin-bottom:1px;"));
        const name = inputField("Name", draft.name, (value) => { draft.name = value; }, {
            help: "The Library name used to identify this reference.",
        });
        editor.appendChild(name.wrap);
        const { wrap: kindWrap } = fieldShell("Kind", "What this reference represents. Kind controls advisory preset-tag suggestions.");
        const kind = el("select", "", css.input);
        kind.setAttribute("aria-label", "Kind");
        for (const value of ["character", "location", "prop", "outfit"]) {
            const option = el("option", value[0].toUpperCase() + value.slice(1));
            option.value = value;
            option.selected = draft.kind === value;
            kind.appendChild(option);
        }
        kind.addEventListener("change", () => {
            const priorDefault = defaultReferenceClass(draft.kind);
            draft.kind = kind.value;
            if (!reference || draft.reference_class === priorDefault) draft.reference_class = defaultReferenceClass(draft.kind);
            render();
        });
        kindWrap.appendChild(kind);
        editor.appendChild(kindWrap);
        const { wrap: classWrap } = fieldShell("Class", "Subject marks a primary thing; Context marks setting or supporting environmental material. Later recipes may use this distinction.");
        const classSelect = el("select", "", css.input);
        classSelect.setAttribute("aria-label", "Class");
        for (const value of ["subject", "context"]) {
            const option = el("option", value[0].toUpperCase() + value.slice(1));
            option.value = value;
            option.selected = draft.reference_class === value;
            classSelect.appendChild(option);
        }
        classSelect.addEventListener("change", () => { draft.reference_class = classSelect.value; });
        classWrap.appendChild(classSelect);
        editor.appendChild(classWrap);
        const { wrap: descriptionLabel } = fieldShell("Description", "What this Reference is, in one line — so a crowded Library stays readable when the name alone is not enough. Project-only: nothing here is inserted into prompts. Per-member prompt text is what reaches the model.");
        const description = el("textarea", "", `${css.input}min-height:56px;resize:vertical;line-height:1.45;`);
        description.setAttribute("aria-label", "Description");
        description.value = draft.description;
        description.addEventListener("input", () => { draft.description = description.value; });
        descriptionLabel.appendChild(description);
        editor.appendChild(descriptionLabel);
        const intentRow = el("div", "", "display:grid;grid-template-columns:1fr 1fr;gap:8px;");
        const { wrap: visualIntentWrap } = fieldShell("Default visual: what to preserve",
            "Provider-neutral default used by new Subject units and staged members; a setup or chip may override it.");
        const visualIntent = el("select", "", css.input);
        for (const [value, label] of [["preserve", "Preserve"], ["partial", "Partial"],
            ["transfer_attributes", "Transfer attributes"], ["reference_loosely", "Reference loosely"]]) {
            const option = el("option", label); option.value = value;
            option.selected = draft.visual_intent === value; visualIntent.appendChild(option);
        }
        visualIntent.addEventListener("change", () => { draft.visual_intent = visualIntent.value; });
        visualIntentWrap.appendChild(visualIntent);
        const { wrap: audioIntentWrap } = fieldShell("Default audio: what to preserve",
            "Provider-neutral default for copy/reference behavior; physical audio presence never chooses this role.");
        const audioIntent = el("select", "", css.input);
        for (const [value, label] of [["copy_full", "Copy fully"], ["copy_partial", "Copy partially"],
            ["reference_characteristics", "Reference characteristics"], ["reference_loosely", "Reference loosely"]]) {
            const option = el("option", label); option.value = value;
            option.selected = draft.audio_intent === value; audioIntent.appendChild(option);
        }
        audioIntent.addEventListener("change", () => { draft.audio_intent = audioIntent.value; });
        audioIntentWrap.appendChild(audioIntent);
        intentRow.append(visualIntentWrap, audioIntentWrap); editor.appendChild(intentRow);
        const row = el("div", "", "display:flex;gap:6px;margin:3px -10px -10px;padding:9px 10px;border-top:1px solid #303a43;background:#12181d;border-radius:0 0 8px 8px;");
        const save = el("button", "Save", `${css.button}background:#476d88;border-color:#668ca7;`);
        const cancel = el("button", "Cancel", css.button);
        save.addEventListener("click", () => {
            const errors = validateReferenceDraft(draft);
            if (errors.length) { state.error = errors[0]; render(); return; }
            const values = serializeReferenceDraft(draft);
            if (reference) {
                const fields = {};
                const expected = {};
                for (const key of ["name", "kind", "reference_class", "description",
                    "visual_intent", "audio_intent"]) {
                    if (values[key] !== reference[key]) { fields[key] = values[key]; expected[key] = reference[key]; }
                }
                if (!Object.keys(fields).length) { state.entityDraft = null; render(); return; }
                void perform([{ type: "update_reference", reference_id: reference.reference_id, fields, expected }]);
            } else {
                void perform([{ type: "create_reference", fields: values }]);
            }
        });
        cancel.addEventListener("click", () => { state.entityDraft = null; render(); });
        row.append(save, cancel);
        editor.appendChild(row);
        body.appendChild(editor);
    };

    const renderMemberEditor = (body, reference, member) => {
        const draft = state.memberDraft;
        const data = host.getData();
        const assets = data.assets || [];
        const asset = assets.find((entry) => entry.asset_id === draft.asset_id);
        const editor = el("div", "", "display:flex;flex-direction:column;gap:10px;margin-top:8px;padding:9px;border:1px solid #303a43;border-radius:8px;background:#13191f;");
        const assetRow = el("div", "", "display:flex;align-items:center;gap:6px;flex-wrap:wrap;");
        const chosen = el("div", asset ? `${assetName(asset)} (${asset.asset_type})` : (draft.asset_id ? "Unresolved asset" : "No asset selected"), "flex:1;min-width:0;color:#cdd5dc;font-size:11px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;");
        assetRow.appendChild(chosen);
        editor.appendChild(assetRow);
        const mediaChooser = el("div", "", "display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px;");
        for (const assetType of ["image", "audio", "video"]) {
            const typeLabel = assetType[0].toUpperCase() + assetType.slice(1);
            const activeType = draft.asset_type === assetType;
            const choose = el("button", typeLabel, `${css.button}padding:5px 6px;${activeType ? "background:#476d88;border-color:#668ca7;" : ""}`);
            choose.title = `${member ? "Replace with" : "Choose"} ${assetType}`;
            choose.addEventListener("click", () => host.pickAsset({
                assetType,
                currentAssetId: draft.asset_type === assetType ? draft.asset_id : "",
                onPick: (picked) => {
                    const replacement = replaceMemberDraftAsset(draft, picked);
                    state.memberDraft = replacement.draft;
                    state.memberDraft.has_audio = picked.has_audio === true;
                    state.memberNotice = replacement.notices.join(" ");
                    render();
                },
            }));
            mediaChooser.appendChild(choose);
        }
        editor.appendChild(mediaChooser);
        if (state.memberNotice) editor.appendChild(el("div", state.memberNotice, "color:#d5aa70;font-size:10px;line-height:1.4;"));
        const memberName = inputField("Member name", draft.name, (value) => {
            draft.name = value;
        }, {
            placeholder: asset ? assetName(asset).replace(/\.[^.]+$/, "") : "Front, voice, turnaround…",
            help: `Combined with the Reference name for display and prompt tokens, for example “${reference.name} · Front” / “${reference.name.replace(/\s+/g, "_")}_Front”.`,
        });
        editor.appendChild(memberName.wrap);
        if (asset) {
            const mediaActions = el("div", "", "display:flex;gap:5px;flex-wrap:wrap;");
            const inspect = el("button", "Inspect Source", css.button);
            inspect.addEventListener("click", () => host.inspectAsset?.(asset));
            const editLabel = asset.asset_type === "image" ? "Crop & Preview"
                : (asset.asset_type === "audio" ? "Trim & Preview" : "Crop, Trim & Preview");
            const editMedia = el("button", editLabel, css.button);
            editMedia.addEventListener("click", () => host.editMemberMedia?.({ asset, draft, readOnly: false, onApply: (updates) => {
                Object.assign(draft, updates);
                render();
            } }));
            mediaActions.append(inspect, editMedia);
            editor.appendChild(mediaActions);
        }

        const catalog = data.catalog || [];
        const tagLabel = el("div", "Preset tags · suggestions first", css.label);
        editor.appendChild(tagLabel);
        if (!asset) {
            editor.appendChild(el("div", "Choose an asset to see compatible preset tags.", "color:#7f8d99;font-size:10px;line-height:1.4;"));
        }
        const chips = el("div", "", "display:flex;gap:4px;flex-wrap:wrap;");
        const orderedCatalog = compatibleReferencePresets(catalog, asset).sort((left, right) => {
            const leftSuggested = left.suggested_kinds?.includes(reference.kind) ? 0 : 1;
            const rightSuggested = right.suggested_kinds?.includes(reference.kind) ? 0 : 1;
            return leftSuggested - rightSuggested;
        });
        for (const preset of orderedCatalog) {
            const active = draft.tags.includes(preset.id);
            const chip = el("button", preset.label || preset.id, `${css.button}padding:3px 6px;${active ? "background:#476d88;border-color:#668ca7;" : ""}`);
            chip.title = preset.suggested_kinds?.includes(reference.kind)
                ? `Suggested for ${reference.kind}`
                : "Available for any compatible Reference kind";
            chip.addEventListener("click", () => {
                draft.tags = active ? draft.tags.filter((tag) => tag !== preset.id) : [...draft.tags, preset.id];
                render();
            });
            chips.appendChild(chip);
        }
        const incompatible = incompatibleReferencePresetTags(draft.tags, catalog, asset);
        for (const tag of incompatible) {
            const preset = catalog.find((entry) => entry.id === tag);
            const chip = el("button", `${preset?.label || tag} · incompatible`, `${css.button}padding:3px 6px;background:#5a3030;border-color:#a25d5d;color:#ffd8d8;`);
            chip.title = "This preset is incompatible with the selected asset. Remove it before saving.";
            chip.addEventListener("click", () => { draft.tags = draft.tags.filter((entry) => entry !== tag); render(); });
            chips.appendChild(chip);
        }
        if (!asset) {
            for (const tag of draft.tags) {
                const preset = catalog.find((entry) => entry.id === tag);
                if (!preset) continue;
                const chip = el("button", `${preset.label || tag} · awaiting asset`, `${css.button}padding:3px 6px;background:#394857;border-color:#60788b;`);
                chip.title = "Choose an asset to validate this saved preset, or click to remove it.";
                chip.addEventListener("click", () => { draft.tags = draft.tags.filter((entry) => entry !== tag); render(); });
                chips.appendChild(chip);
            }
        }
        if (asset || incompatible.length || chips.childNodes.length) editor.appendChild(chips);
        const presetIds = new Set(catalog.map((preset) => preset.id));
        const custom = inputField("Custom tags (comma separated)", draft.tags.filter((tag) => !presetIds.has(tag)).join(", "), (value) => {
            const presets = draft.tags.filter((tag) => presetIds.has(tag));
            const entered = value.split(",");
            draft.tags = normalizeReferenceTags([...presets, ...entered], catalog, { strict: false }).tags;
        });
        editor.appendChild(custom.wrap);
        const { wrap: promptLabel } = fieldShell("Prompt");
        const prompt = el("textarea", "", `${css.input}min-height:54px;resize:vertical;`);
        prompt.setAttribute("aria-label", "Prompt");
        prompt.value = draft.prompt;
        prompt.addEventListener("input", () => { draft.prompt = prompt.value; });
        promptLabel.appendChild(prompt);
        editor.appendChild(promptLabel);

        if (["image", "video"].includes(draft.asset_type)) {
            const cropTitle = el("div", "Crop (%)", css.label);
            editor.appendChild(cropTitle);
            const crop = draft.crop || { x: 0, y: 0, w: 100, h: 100 };
            const row = el("div", "", "display:grid;grid-template-columns:repeat(4,1fr);gap:4px;");
            for (const key of ["x", "y", "w", "h"]) {
                const field = inputField(key.toUpperCase(), crop[key], (value) => {
                    draft.crop = { ...(draft.crop || crop), [key]: value };
                }, { type: "number" });
                row.appendChild(field.wrap);
            }
            editor.appendChild(row);
            const full = el("button", "Reset to Full Source", `${css.button}margin-top:5px;`);
            full.addEventListener("click", () => { draft.crop = null; render(); });
            editor.appendChild(full);
        }
        if (["audio", "video"].includes(draft.asset_type)) {
            const row = el("div", "", "display:grid;grid-template-columns:1fr 1fr;gap:5px;");
            row.appendChild(inputField("Start seconds", draft.source_start_sec, (value) => { draft.source_start_sec = value; }, { type: "number", step: "0.1" }).wrap);
            row.appendChild(inputField("End seconds", draft.source_end_sec, (value) => { draft.source_end_sec = value; }, { type: "number", step: "0.1", placeholder: "Full remainder" }).wrap);
            editor.appendChild(row);
        }
        const row = el("div", "", "display:flex;gap:6px;margin:2px -9px -9px;padding:9px;border-top:1px solid #303a43;background:#10161b;border-radius:0 0 8px 8px;");
        const save = el("button", "Save", `${css.button}background:#476d88;border-color:#668ca7;`);
        const cancel = el("button", "Cancel", css.button);
        save.addEventListener("click", () => {
            const errors = validateMemberDraft(draft, catalog, asset);
            if (errors.length) { state.error = errors[0]; render(); return; }
            const values = serializeMemberDraft(draft, catalog);
            if (member) {
                const fields = {};
                for (const key of ["asset_id", "name", "tags", "prompt", "crop", "source_start_sec", "source_end_sec"]) {
                    if (JSON.stringify(values[key]) !== JSON.stringify(member[key])) fields[key] = values[key];
                }
                if (!Object.keys(fields).length) { state.memberDraft = null; render(); return; }
                void perform([{ type: "update_member", reference_id: reference.reference_id, member_id: member.member_id, fields, expected: member }]);
            } else {
                void perform([{ type: "create_member", reference_id: reference.reference_id, fields: values }]);
            }
        });
        cancel.addEventListener("click", () => { state.memberDraft = null; state.memberMode = ""; render(); });
        row.append(save, cancel);
        editor.appendChild(row);
        body.appendChild(editor);
    };

    const render = () => {
        if (state.destroyed) return;
        container.innerHTML = "";
        const data = host.getData();
        const toolbar = el("div", "", "padding:8px;border-bottom:1px solid #303841;display:flex;gap:6px;flex:0 0 auto;");
        const search = el("input", "", `${css.input}flex:1;min-width:0;`);
        search.type = "search";
        search.dataset.referenceSearch = "true";
        search.placeholder = "Search references";
        search.value = state.query;
        search.addEventListener("input", () => {
            state.query = search.value;
            const cursor = search.selectionStart ?? state.query.length;
            render();
            const nextSearch = container.querySelector('[data-reference-search="true"]');
            nextSearch?.focus?.();
            nextSearch?.setSelectionRange?.(cursor, cursor);
        });
        const add = el("button", "+", `${css.button}font-size:15px;padding:3px 9px;`);
        add.title = "Create reference";
        add.addEventListener("click", () => { state.selectedReferenceId = ""; state.entityDraft = createReferenceDraft(); state.memberDraft = null; render(); });
        const manage = el("button", "Manage", `${css.button}${state.manage ? "background:#476d88;border-color:#668ca7;" : ""}`);
        manage.title = "Show or hide Reference edit and delete controls";
        manage.setAttribute("aria-pressed", state.manage ? "true" : "false");
        manage.addEventListener("click", () => { state.manage = !state.manage; render(); });
        toolbar.append(search, manage, add);
        container.appendChild(toolbar);
        const body = el("div", "", "flex:1;min-height:0;overflow:auto;padding:8px;box-sizing:border-box;");
        container.appendChild(body);
        if (data.loading) { body.appendChild(el("div", "Loading references…", "color:#98a5b2;padding:18px;text-align:center;font-size:11px;")); return; }
        if (data.error) body.appendChild(el("div", data.error, "color:#e39a9a;margin-bottom:8px;font-size:11px;"));
        if (state.error) body.appendChild(el("div", state.error, "color:#e39a9a;margin-bottom:8px;font-size:11px;"));
        if (state.entityDraft) { renderEntityEditor(body, data.references.find((entry) => entry.reference_id === state.entityDraft.reference_id)); return; }
        const allAssets = data.assets || [];
        const references = filterReferences(data.references, state.query, allAssets);
        if (!references.length) body.appendChild(el("div", data.references.length ? "No references match." : "No references yet.", "color:#788692;padding:18px;text-align:center;font-size:11px;"));
        for (const reference of references) {
            const card = el("section", "", "border:1px solid #303841;border-radius:8px;background:#171d23;margin-bottom:8px;overflow:hidden;");
            const header = el("div", "", "display:flex;flex-direction:column;gap:5px;padding:8px;cursor:pointer;");
            header.draggable = (reference.members || []).length > 0;
            header.addEventListener("dragstart", (event) => beginReferenceDrag(
                event, dragPayload(reference),
                { assets: allAssets, members: reference.members || [] }));
            header.addEventListener("dragend", endReferenceDrag);
            header.addEventListener("contextmenu", (event) => openTimelineMenu(event, dragPayload(reference)));
            const memberAssets = (reference.members || []).map((member) => allAssets.find((asset) => asset.asset_id === member.asset_id));
            const unresolved = memberAssets.filter((asset) => !asset).length;
            const trashed = memberAssets.filter((asset) => asset && (asset.trashed || asset.trashed_at)).length;
            const missingFiles = memberAssets.filter((asset) => asset?.missing).length;
            const topLine = el("div", "", "display:flex;align-items:center;gap:6px;min-width:0;");
            const title = el("div", reference.name, "flex:1;min-width:0;font-size:12px;font-weight:600;overflow:hidden;text-overflow:ellipsis;");
            const count = el("span", `${reference.members?.length || 0}`, `font-size:9px;color:${unresolved || trashed || missingFiles ? "#e2ab68" : "#82909c"};`);
            topLine.append(title, count);
            if (state.manage) {
                const edit = el("button", "Edit", `${css.button}padding:3px 6px;font-size:9px;`);
                const remove = el("button", "Delete", `${css.button}padding:3px 6px;font-size:9px;`);
                edit.addEventListener("click", (event) => { event.stopPropagation(); state.entityDraft = createReferenceDraft(reference); render(); });
                remove.addEventListener("click", (event) => {
                    event.stopPropagation();
                    const unitIds = new Set((data.semanticUnits || [])
                        .filter((unit) => (unit.sources || []).some((value) =>
                            value.entity_id === reference.reference_id))
                        .map((unit) => unit.semantic_unit_id));
                    let staged = 0; let chips = 0;
                    for (const scene of data.scenes || []) {
                        const itemIds = new Set((scene.reference_items || [])
                            .filter((item) => (item.members || []).some((value) =>
                                value.entity_id === reference.reference_id))
                            .map((item) => item.reference_item_id));
                        staged += itemIds.size;
                        const attachments = [...(scene.global_attachments || []),
                            ...(scene.prompt_sections || []).flatMap((section) =>
                                section.attachments || [])];
                        chips += attachments.filter((attachment) =>
                            itemIds.has(attachment?.source?.reference_item_id)
                            || (attachment?.source?.semantic_unit_ids || []).some((id) =>
                                unitIds.has(id))).length;
                    }
                    const usage = staged || chips || unitIds.size
                        ? `\n\nWhere used: ${staged} staged item(s), ${unitIds.size} Subject unit(s), ${chips} Context chip(s). Deletion keeps broken chips visible for repair.`
                        : "";
                    if (!host.confirm(`Delete “${reference.name}” and its ${reference.members?.length || 0} member(s)?${usage}`)) return;
                    void perform([{ type: "delete_reference", reference_id: reference.reference_id, expected: { name: reference.name, kind: reference.kind, reference_class: reference.reference_class, description: reference.description || "", visual_intent: reference.visual_intent || "preserve", audio_intent: reference.audio_intent || "reference_characteristics", member_ids: (reference.members || []).map((member) => member.member_id) } }]);
                });
                topLine.append(edit, remove);
            }
            header.appendChild(topLine);
            const badges = el("div", "", "display:flex;gap:4px;flex-wrap:wrap;");
            for (const value of [reference.kind, reference.reference_class]) {
                badges.appendChild(el("span", value, "padding:2px 5px;border:1px solid #34424d;border-radius:999px;color:#91a5b5;background:#11171c;font-size:9px;text-transform:capitalize;"));
            }
            header.appendChild(badges);
            if (reference.description) header.appendChild(el("div", reference.description, "font-size:10px;line-height:1.35;color:#b7c0c8;white-space:normal;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden;"));
            const statuses = [];
            if (unresolved) statuses.push(`${unresolved} unresolved`);
            if (missingFiles) statuses.push(`${missingFiles} missing file${missingFiles === 1 ? "" : "s"}`);
            if (trashed) statuses.push(`${trashed} trashed`);
            if (statuses.length) header.appendChild(el("div", statuses.join(" · "), "font-size:9px;color:#e2ab68;"));
            header.addEventListener("click", () => { state.selectedReferenceId = state.selectedReferenceId === reference.reference_id ? "" : reference.reference_id; state.memberDraft = null; render(); });
            card.appendChild(header);
            if (state.selectedReferenceId === reference.reference_id) {
                const detail = el("div", "", "padding:0 8px 8px;border-top:1px solid #293039;");
                const actions = el("div", "", "display:flex;gap:5px;margin:7px 0;");
                const addMember = el("button", "+ Member", css.button);
                addMember.addEventListener("click", () => { state.memberDraft = createMemberDraft(); state.memberNotice = ""; state.memberMode = "create"; render(); });
                const addTimeline = el("button", "Add to timeline", css.button);
                addTimeline.disabled = !(reference.members || []).length;
                addTimeline.addEventListener("click", () => host.addToTimeline?.(dragPayload(reference)));
                actions.append(addMember, addTimeline);
                detail.appendChild(actions);
                if (state.memberDraft) {
                    const editing = (reference.members || []).find((member) => member.member_id === state.memberMode);
                    renderMemberEditor(detail, reference, editing);
                } else {
                    for (const member of reference.members || []) {
                        const asset = allAssets.find((entry) => entry.asset_id === member.asset_id);
                        const row = el("div", "", "display:grid;grid-template-columns:40px minmax(0,1fr);gap:7px;padding:7px 0;border-top:1px solid #293039;");
                        row.draggable = true;
                        row.addEventListener("dragstart", (event) => beginReferenceDrag(
                            event, dragPayload(reference, member),
                            { assets: allAssets, members: [member] }));
                        row.addEventListener("dragend", endReferenceDrag);
                        row.addEventListener("contextmenu", (event) => openTimelineMenu(event, dragPayload(reference, member)));
                        const preview = el("button", asset?.asset_type === "audio" ? "Audio" : (asset?.asset_type === "video" ? "Video" : ""), "width:40px;height:34px;padding:0;background:#0b0e12;border:1px solid #333b44;border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:8px;color:#8995a0;overflow:hidden;cursor:pointer;");
                        preview.type = "button";
                        preview.title = "Inspect Reference member";
                        const url = host.assetPreviewUrl(asset);
                        if (url && ["image", "video"].includes(asset?.asset_type)) { const img = el("img"); img.src = url; img.alt = ""; img.style.cssText = "width:100%;height:100%;object-fit:cover;"; preview.textContent = ""; preview.appendChild(img); }
                        const info = el("div", "", "min-width:0;");
                        const status = !asset ? "Missing" : (asset.trashed || asset.trashed_at ? "Trashed" : (asset.missing ? "Missing file" : ""));
                        const nameLine = el("button", `${assetName(asset)}${status ? ` · ${status}` : ""}`, `display:block;width:100%;padding:0;border:0;background:transparent;text-align:left;font-size:10px;color:${status ? "#e2ab68" : "#d7dde2"};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;`);
                        const composite = member.name
                            ? `${reference.name} · ${member.name}` : reference.name;
                        nameLine.textContent = `${composite} — ${assetName(asset)}${status ? ` · ${status}` : ""}`;
                        nameLine.type = "button";
                        nameLine.title = "Inspect Reference member";
                        const inspectMember = () => {
                            const previewDraft = createMemberDraft(member, asset);
                            previewDraft.has_audio = asset?.has_audio === true;
                            host.previewMemberMedia?.({ asset, draft: previewDraft, readOnly: true });
                        };
                        preview.addEventListener("click", inspectMember);
                        nameLine.addEventListener("click", inspectMember);
                        info.appendChild(nameLine);
                        if (member.tags?.length) info.appendChild(el("div", member.tags.join(" · "), "font-size:9px;color:#829fba;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:2px;"));
                        if (member.prompt) info.appendChild(el("div", member.prompt, "font-size:9px;color:#b8c2ca;line-height:1.4;white-space:pre-wrap;overflow-wrap:anywhere;margin-top:3px;"));
                        const controls = el("div", "", "display:flex;gap:3px;margin-top:4px;");
                        for (const [label, handler] of [
                            ["Edit", () => { state.memberDraft = createMemberDraft(member, asset); state.memberDraft.has_audio = asset?.has_audio === true; state.memberNotice = ""; state.memberMode = member.member_id; render(); }],
                            ["Remove", () => {
                                const unitIds = new Set((data.semanticUnits || [])
                                    .filter((unit) => (unit.sources || []).some((value) =>
                                        value.entity_id === reference.reference_id
                                        && value.member_id === member.member_id))
                                    .map((unit) => unit.semantic_unit_id));
                                let staged = 0; let chips = 0;
                                for (const scene of data.scenes || []) {
                                    const itemIds = new Set((scene.reference_items || [])
                                        .filter((item) => (item.members || []).some((value) =>
                                            value.entity_id === reference.reference_id
                                            && value.member_id === member.member_id))
                                        .map((item) => item.reference_item_id));
                                    staged += itemIds.size;
                                    const attachments = [...(scene.global_attachments || []),
                                        ...(scene.prompt_sections || []).flatMap((section) =>
                                            section.attachments || [])];
                                    chips += attachments.filter((attachment) =>
                                        itemIds.has(attachment?.source?.reference_item_id)
                                        || (attachment?.source?.semantic_unit_ids || []).some((id) =>
                                            unitIds.has(id))).length;
                                }
                                const usage = staged || chips || unitIds.size
                                    ? `\n\nWhere used: ${staged} staged item(s), ${unitIds.size} Subject unit(s), ${chips} Context chip(s). Deletion keeps broken chips visible for repair.`
                                    : "";
                                if (host.confirm(`Remove this Library member?${usage}`)) {
                                    void perform([{ type: "delete_member", reference_id: reference.reference_id,
                                        member_id: member.member_id, expected: member }]);
                                }
                            }],
                            ["Up", () => reorder(reference, member, -1)], ["Down", () => reorder(reference, member, 1)],
                        ]) { const button = el("button", label, `${css.button}padding:2px 5px;font-size:9px;`); button.addEventListener("click", handler); controls.appendChild(button); }
                        info.appendChild(controls);
                        row.append(preview, info);
                        detail.appendChild(row);
                    }
                }
                card.appendChild(detail);
            }
            body.appendChild(card);
        }
    };

    const reorder = (reference, member, direction) => {
        const desired = moveMember(reference.members, member.member_id, direction).map((entry) => entry.member_id);
        const expected = reference.members.map((entry) => entry.member_id);
        if (JSON.stringify(expected) !== JSON.stringify(desired)) {
            void perform([{ type: "reorder_members", reference_id: reference.reference_id, expected_member_ids: expected, member_ids: desired }]);
        }
    };

    render();
    return {
        render,
        reset,
        destroy() { state.destroyed = true; activeReferenceDrag = null; container.innerHTML = ""; },
    };
}
