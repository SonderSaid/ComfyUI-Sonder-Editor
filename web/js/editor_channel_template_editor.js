// Channel Template editor — centered overlay for creating and copying catalog
// templates or editing one template's channel keys, headers, per-channel authoring
// guidance, the shot-marker channel, the label policy, the separators between
// fields, and whether the scene-global prompt is authored per channel.
//
// Module-host contract (fullscreen seam pattern): the host owns state,
// networking, and durable writes; this module owns its DOM/listeners and
// returns a cleanup handle. Host surface used:
//   _channelTemplate(), _promptChannelTemplateOptions(),
//   _savePromptChannelTemplate(template, options), _keyboardConsumerId(suffix)
//
// Built-ins are read-only and can only be copied. Catalog edits are browser-local;
// when the edited custom is active, the host also updates the project through the
// same switch transaction so renamed or removed keys cannot strand authored text.

import {
    EDITOR_COLORS as COLORS,
    FONT,
    chromeButtonCss,
    chromeInputCss,
    chromeOverlayPanelCss,
    setButtonVariant,
} from "./editor_theme.js";
import {
    register as registerKeyboardConsumer,
    PRIORITY as KEY_PRIORITY,
} from "./keyboard_ownership.js";
import {
    GLOBAL_MERGE_LEADING,
    GLOBAL_MERGE_PER_CHANNEL,
    LABELS_ALWAYS,
    LABELS_NEVER,
    normalizeChannelTemplate,
} from "./prompt_channel_templates.js";

const CUSTOM_ID_PREFIX = "custom:";

// Separators are whitespace, which no text field can show honestly. These are
// the shapes the built-in templates use, named by what they do to the output.
const FIELD_SEPARATORS = [
    { value: " ", label: "Space — one flowing line" },
    { value: "\n\n", label: "Blank line — one field per paragraph" },
    { value: "\n", label: "Newline — one field per line" },
];
const LABEL_SEPARATORS = [
    { value: " ", label: "Space — [VISUAL]: text" },
    { value: ": ", label: "Colon + space — field: text" },
    { value: ":\n", label: "Colon + newline — field:⏎text" },
];
const LABEL_POLICIES = [
    { value: LABELS_ALWAYS, label: "Always write field names" },
    { value: LABELS_NEVER, label: "Never write field names" },
];

/** A working copy of a template, safe to mutate while the overlay is open. */
function draftFrom(template) {
    return {
        id: String(template.id || ""),
        name: String(template.name || ""),
        description: String(template.description || ""),
        builtin: !!template.builtin,
        channels: (template.channels || []).map((entry) => ({
            key: String(entry.key || ""),
            label: String(entry.label ?? ""),
            description: String(entry.description ?? ""),
        })),
        field_separator: template.field_separator ?? " ",
        label_separator: template.label_separator ?? " ",
        labels: [LABELS_ALWAYS, LABELS_NEVER].includes(template.labels)
            ? template.labels : LABELS_NEVER,
        shot_marker_channel: template.shot_marker_channel ?? "",
        global_merge: template.global_merge ?? GLOBAL_MERGE_LEADING,
        global_channels_enabled: template.global_channels_enabled !== false,
        default_context_profile: String(template.default_context_profile || "generic@1"),
    };
}

/** Errors that must block a save, in the order they should be reported. */
export function validateTemplateDraft(draft) {
    const errors = [];
    if (!String(draft.name || "").trim()) errors.push("The template needs a name.");
    const keys = (draft.channels || []).map((entry) => String(entry.key || "").trim());
    if (!keys.length) errors.push("A template needs at least one channel.");
    if (keys.some((key) => !key)) errors.push("Every channel needs a key.");
    // Keys are the storage identity of a section's text, so a duplicate would
    // silently make one channel unreachable rather than merely look odd.
    const seen = new Set();
    for (const key of keys) {
        if (key && seen.has(key)) { errors.push(`Duplicate channel key: ${key}.`); break; }
        seen.add(key);
    }
    if (keys.some((key) => key && !/^[a-z0-9_]+$/.test(key))) {
        // The writing tool parses `key:` at line start; a key with spaces or
        // punctuation could not be typed back in.
        errors.push("Channel keys use lowercase letters, digits and underscores.");
    }
    if (draft.labels !== LABELS_NEVER
        && (draft.channels || []).some((entry) => !String(entry.label ?? "").trim())) {
        errors.push("A template that writes field names needs a header for every channel.");
    }
    return errors;
}

export function mintCustomTemplateId(name, usedIds = []) {
    const base = `${CUSTOM_ID_PREFIX}${slug(name)}`;
    const used = new Set(usedIds || []);
    let candidate = base;
    let suffix = 2;
    while (used.has(candidate)) {
        candidate = `${base}-${suffix}`;
        suffix += 1;
    }
    return candidate;
}

/** The template dict a draft saves as. Edits retain ids; copies mint now. */
export function templateFromDraft(draft, {
    fork = false,
    mode = fork ? "copy" : "edit",
    usedIds = [],
} = {}) {
    const retainId = mode === "edit"
        && String(draft.id || "").startsWith(CUSTOM_ID_PREFIX);
    const id = retainId
        ? draft.id
        : mintCustomTemplateId(draft.name, usedIds);
    return normalizeChannelTemplate({
        ...draft,
        id,
        channels: (draft.channels || []).map((entry) => ({
            key: String(entry.key || "").trim(),
            label: String(entry.label ?? "").trim(),
            description: String(entry.description ?? "").trim(),
        })),
    });
}

function blankDraft() {
    return {
        id: "",
        name: "New Channel Template",
        description: "",
        builtin: false,
        channels: [{ key: "prompt", label: "", description: "The prompt text." }],
        field_separator: " ",
        label_separator: " ",
        labels: LABELS_NEVER,
        shot_marker_channel: "",
        global_merge: GLOBAL_MERGE_LEADING,
        global_channels_enabled: false,
        default_context_profile: "generic@1",
    };
}

function slug(name) {
    return String(name || "custom").toLowerCase().replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "") || "custom";
}

export function mountChannelTemplateEditor(host, { template = null, mode = "edit" } = {}) {
    const backdrop = document.createElement("div");
    backdrop.style.cssText = `
        position: fixed; inset: 0; z-index: 10001;
        background: rgba(7,10,14,0.70);
        display: flex; align-items: center; justify-content: center;
    `;
    const panel = document.createElement("div");
    panel.style.cssText = chromeOverlayPanelCss({
        width: "min(760px, 92vw)", maxWidth: "760px", maxHeight: "86vh", padding: "0",
    });
    panel.style.display = "flex";
    panel.style.flexDirection = "column";
    panel.style.fontFamily = FONT;
    backdrop.appendChild(panel);

    const source = template || host._channelTemplate();
    const initialDraft = mode === "new" ? blankDraft() : draftFrom(source);
    if (mode === "copy") {
        initialDraft.id = "";
        initialDraft.name = `${initialDraft.name} Copy`;
        initialDraft.builtin = false;
    }
    const state = { draft: initialDraft, mode, error: "", busy: false };
    const isReadOnly = () => state.mode === "edit" && state.draft.builtin;

    const makeBtn = (text, title, variant = "subtle") => {
        const btn = document.createElement("button");
        btn.textContent = text;
        btn.title = title;
        btn.style.cssText = chromeButtonCss({ fontSize: "11px", padding: "4px 9px" });
        setButtonVariant(btn, variant);
        btn.dataset.sonderHoverVariant = variant;
        return btn;
    };
    const label = (text, title = "") => {
        const el = document.createElement("div");
        el.textContent = text;
        el.title = title;
        el.style.cssText = `font-size:10px; color:${COLORS.textDim};`;
        return el;
    };
    const input = (value, { width = "100%", placeholder = "" } = {}) => {
        const el = document.createElement("input");
        el.type = "text";
        el.value = value ?? "";
        el.placeholder = placeholder;
        el.style.cssText = `${chromeInputCss({ padding: "4px 6px", textAlign: "left" })} width:${width};`;
        el.disabled = isReadOnly();
        el.addEventListener("keydown", (e) => e.stopPropagation());
        return el;
    };
    const select = (options, value) => {
        const el = document.createElement("select");
        el.style.cssText = `${chromeInputCss({ padding: "3px 5px", textAlign: "left" })} width:100%; cursor:pointer;`;
        for (const option of options) {
            const opt = document.createElement("option");
            opt.value = option.value;
            opt.textContent = option.label;
            el.appendChild(opt);
        }
        el.value = value;
        el.disabled = isReadOnly();
        el.addEventListener("keydown", (e) => e.stopPropagation());
        return el;
    };

    const header = document.createElement("div");
    header.style.cssText = `
        display:flex; align-items:center; gap:8px; padding:10px 14px;
        border-bottom:1px solid ${COLORS.promptBorder};
    `;
    const title = document.createElement("div");
    title.style.cssText = `flex:1; font-size:12px; color:${COLORS.text};`;
    const closeBtn = makeBtn("✕", "Close without saving", "danger");
    header.append(title, closeBtn);

    const body = document.createElement("div");
    body.style.cssText = `
        flex:1; overflow-y:auto; padding:12px 14px; display:flex;
        flex-direction:column; gap:10px;
    `;
    const footer = document.createElement("div");
    footer.style.cssText = `
        display:flex; align-items:center; gap:6px; padding:9px 14px;
        border-top:1px solid ${COLORS.promptBorder};
    `;
    panel.append(header, body, footer);

    function render() {
        const draft = state.draft;
        const modeLabel = state.mode === "new"
            ? "New Channel Template"
            : (state.mode === "copy" ? "Save as Custom" : `Channel Template — ${draft.name}`);
        title.textContent = draft.builtin ? `${modeLabel} (built-in)` : modeLabel;
        body.replaceChildren();
        footer.replaceChildren();

        if (draft.builtin) {
            const note = document.createElement("div");
            note.style.cssText = `font-size:10px; color:${COLORS.textDim}; line-height:1.45;`;
            note.textContent = "Built-in templates are read-only. Save as Custom creates"
                + " an independent browser-local copy; the shipped preset never changes.";
            body.appendChild(note);
        }
        if (state.error) {
            const err = document.createElement("div");
            err.style.cssText = "font-size:10px; color:#e08b6a; line-height:1.4;";
            err.textContent = state.error;
            body.appendChild(err);
        }

        // ── Identity ──────────────────────────────────────────────────
        const nameRow = document.createElement("div");
        nameRow.style.cssText = "display:grid; grid-template-columns: 1fr 2fr; gap:8px;";
        const nameCol = document.createElement("div");
        nameCol.style.cssText = "display:flex; flex-direction:column; gap:2px;";
        const nameInput = input(draft.name, { placeholder: "Template name" });
        nameInput.addEventListener("input", () => { draft.name = nameInput.value; });
        nameCol.append(label("Name"), nameInput);
        const descCol = document.createElement("div");
        descCol.style.cssText = "display:flex; flex-direction:column; gap:2px;";
        const descInput = input(draft.description, { placeholder: "What this channel set is for" });
        descInput.addEventListener("input", () => { draft.description = descInput.value; });
        descCol.append(label("Description"), descInput);
        nameRow.append(nameCol, descCol);
        body.appendChild(nameRow);

        const profileOptions = [
            { profile_id: "generic@1", name: "Generic" },
            { profile_id: "minimax_h3_base@1", name: "MiniMax H3 Base" },
            { profile_id: "minimax_h3_ref@1", name: "MiniMax H3 Full Reference" },
            ...(Array.isArray(host._promptContextProfiles) ? host._promptContextProfiles : [])
                .map((value) => ({ ...value,
                    profile_id: `${value.profile_id}@${value.version || "1"}` })),
        ].filter((value, index, values) => value?.profile_id
            && values.findIndex((candidate) => candidate?.profile_id === value.profile_id) === index);
        const profileCol = document.createElement("div");
        profileCol.style.cssText = "display:flex;flex-direction:column;gap:2px;";
        const profileSelect = select(profileOptions.map((value) => ({
            value: value.profile_id,
            label: `${value.name || value.profile_id} (${value.profile_id})`,
        })), draft.default_context_profile || "generic@1");
        profileSelect.disabled = isReadOnly();
        profileSelect.addEventListener("change", () => {
            draft.default_context_profile = profileSelect.value || "generic@1";
        });
        profileCol.append(label("Default prompt format",
            "Scenes using this channel template inherit this prompt format unless the scene overrides it."),
        profileSelect);
        body.appendChild(profileCol);

        // ── Channels ──────────────────────────────────────────────────
        const channelsHead = document.createElement("div");
        channelsHead.style.cssText = "display:flex; align-items:center; gap:8px;";
        const channelsTitle = label(`Channels (${draft.channels.length})`,
            "The ordered set of fields every prompt section authors.");
        channelsTitle.style.flex = "1";
        const addBtn = makeBtn("+ Channel", "Append a channel to this template");
        addBtn.disabled = isReadOnly();
        addBtn.addEventListener("click", () => {
            draft.channels.push({ key: "", label: "", description: "" });
            render();
        });
        channelsHead.append(channelsTitle, addBtn);
        body.appendChild(channelsHead);

        const list = document.createElement("div");
        list.style.cssText = "display:flex; flex-direction:column; gap:6px;";
        draft.channels.forEach((channel, index) => {
            const card = document.createElement("div");
            card.style.cssText = `
                display:flex; flex-direction:column; gap:4px; padding:6px 8px;
                background:${COLORS.panel}; border:1px solid ${COLORS.promptBorder}; border-radius:4px;
            `;
            const row = document.createElement("div");
            row.style.cssText = "display:grid; grid-template-columns: 1fr 1fr auto auto auto; gap:6px; align-items:end;";
            const keyCol = document.createElement("div");
            keyCol.style.cssText = "display:flex; flex-direction:column; gap:2px; min-width:0;";
            const keyInput = input(channel.key, { placeholder: "channel_key" });
            keyInput.title = "Storage key and the header the writing tool parses"
                + " (`key:` at the start of a line). Renaming one moves text.";
            keyInput.addEventListener("input", () => { channel.key = keyInput.value.trim(); });
            keyCol.append(label("Key"), keyInput);
            const labelCol = document.createElement("div");
            labelCol.style.cssText = "display:flex; flex-direction:column; gap:2px; min-width:0;";
            const labelInput = input(channel.label, { placeholder: "Header written to the model" });
            labelInput.title = "What the composed prompt writes before this field's text.";
            labelInput.addEventListener("input", () => { channel.label = labelInput.value; });
            labelCol.append(label("Header"), labelInput);

            const upBtn = makeBtn("↑", "Move this channel earlier");
            upBtn.disabled = isReadOnly() || index === 0;
            upBtn.addEventListener("click", () => {
                draft.channels.splice(index - 1, 0, draft.channels.splice(index, 1)[0]);
                render();
            });
            const downBtn = makeBtn("↓", "Move this channel later");
            downBtn.disabled = isReadOnly() || index === draft.channels.length - 1;
            downBtn.addEventListener("click", () => {
                draft.channels.splice(index + 1, 0, draft.channels.splice(index, 1)[0]);
                render();
            });
            const delBtn = makeBtn("✕", "Remove this channel", "danger");
            delBtn.disabled = isReadOnly();
            delBtn.addEventListener("click", () => {
                draft.channels.splice(index, 1);
                render();
            });
            row.append(keyCol, labelCol, upBtn, downBtn, delBtn);

            const guidance = document.createElement("textarea");
            guidance.value = channel.description;
            guidance.placeholder = "Authoring guidance — shown as this field's tooltip";
            guidance.rows = 2;
            guidance.disabled = isReadOnly();
            guidance.style.cssText = `${chromeInputCss({ padding: "4px 6px", textAlign: "left" })} width:100%; resize:vertical; line-height:1.4; font-size:10px;`;
            guidance.addEventListener("input", () => { channel.description = guidance.value; });
            guidance.addEventListener("keydown", (e) => e.stopPropagation());
            card.append(row, guidance);
            list.appendChild(card);
        });
        body.appendChild(list);

        // ── Output shape ──────────────────────────────────────────────
        const shapeRow = document.createElement("div");
        shapeRow.style.cssText = "display:grid; grid-template-columns: 1fr 1fr; gap:8px;";
        const mk = (text, title, options, value, apply) => {
            const col = document.createElement("div");
            col.style.cssText = "display:flex; flex-direction:column; gap:2px; min-width:0;";
            const el = select(options, value);
            el.addEventListener("change", () => { apply(el.value); render(); });
            col.append(label(text, title), el);
            return col;
        };
        shapeRow.append(
            mk("Field names", "Whether composed output always writes each channel's"
                + " header or emits plain field text.",
                LABEL_POLICIES, draft.labels, (v) => { draft.labels = v; }),
            mk("Shot markers in", "The body field that carries [Shot N] and cut times."
                + " No channel means this template places no shot markers.",
                [{ value: "", label: "No shot markers" },
                 ...draft.channels.filter((c) => c.key).map((c) => ({ value: c.key, label: c.key }))],
                draft.shot_marker_channel, (v) => { draft.shot_marker_channel = v; }),
            mk("Between fields", "What separates one field from the next.",
                FIELD_SEPARATORS, draft.field_separator, (v) => { draft.field_separator = v; }),
            mk("After a header", "What separates a field name from its text.",
                LABEL_SEPARATORS, draft.label_separator, (v) => { draft.label_separator = v; }),
        );
        body.appendChild(shapeRow);

        // ── Global prompt ─────────────────────────────────────────────
        const globalWrap = document.createElement("label");
        globalWrap.style.cssText = `display:flex; align-items:flex-start; gap:6px; font-size:10px; color:${COLORS.textDim}; line-height:1.45;`;
        const globalBox = document.createElement("input");
        globalBox.type = "checkbox";
        globalBox.checked = draft.global_channels_enabled;
        globalBox.disabled = isReadOnly();
        globalBox.style.marginTop = "2px";
        globalBox.addEventListener("keydown", (e) => e.stopPropagation());
        globalBox.addEventListener("change", () => {
            draft.global_channels_enabled = globalBox.checked;
            // A per-channel global only makes sense when there are channels to
            // merge into; turning it on with a `leading` merge would author
            // boxes whose text still printed ahead of every field name.
            draft.global_merge = globalBox.checked
                ? GLOBAL_MERGE_PER_CHANNEL : GLOBAL_MERGE_LEADING;
            render();
        });
        const globalText = document.createElement("span");
        globalText.textContent = "Scene-global prompt is per channel."
            + " Off gives one global box for the whole scene, written ahead of"
            + " everything. Switching either way keeps the text: the other"
            + " channels fold into the single box under their own names, and"
            + " turning it back on puts them back.";
        globalWrap.append(globalBox, globalText);
        body.appendChild(globalWrap);

        // ── Footer ────────────────────────────────────────────────────
        const spacer = document.createElement("div");
        spacer.style.flex = "1";
        const cancelBtn = makeBtn("Cancel", "Close without saving");
        cancelBtn.addEventListener("click", () => close());
        const saveLabel = draft.builtin || state.mode === "copy"
            ? "Save as Custom"
            : (state.mode === "new" ? "Create" : "Save");
        const saveBtn = makeBtn(saveLabel, "Save this template to your catalog", "primary");
        saveBtn.disabled = state.busy;
        saveBtn.addEventListener("click", () => { void save(); });
        footer.append(spacer, cancelBtn, saveBtn);
    }

    async function save() {
        state.error = "";
        const errors = validateTemplateDraft(state.draft);
        if (errors.length) { state.error = errors[0]; render(); return; }
        const saveMode = state.draft.builtin ? "copy" : state.mode;
        const usedIds = (host._promptChannelTemplateOptions?.() || [])
            .map((option) => option.id)
            .filter((id) => !(saveMode === "edit" && id === state.draft.id));
        const next = templateFromDraft(state.draft, { mode: saveMode, usedIds });
        state.busy = true;
        render();
        try {
            const ok = await host._savePromptChannelTemplate(next, {
                updateActive: saveMode === "edit",
            });
            if (ok) close();
        } finally {
            state.busy = false;
            if (backdrop.isConnected) render();
        }
    }

    let released = false;
    function close() {
        if (released) return;
        released = true;
        unregisterKeys?.();
        backdrop.remove();
    }

    backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) close(); });
    closeBtn.addEventListener("click", () => close());

    render();
    document.body.appendChild(backdrop);

    const unregisterKeys = registerKeyboardConsumer({
        id: host._keyboardConsumerId?.("channel-template-editor") || "channel-template-editor",
        priority: KEY_PRIORITY.OVERLAY,
        isActive: () => backdrop.isConnected,
        onKeyDown: (e) => {
            if (e.key !== "Escape") return false;
            close();
            return true;
        },
    });

    return { close, refresh: render, element: backdrop };
}
