// Tracked-metadata renderer registry.
//
// Sections produced by the SonderMetadataCollector backend can carry an explicit
// `display_type` tag; the gallery dispatches the field-body render and the
// per-field matcher through this map. Generic flat-field sections (no display_type)
// fall through to the gallery's default 2-col grid + JSON.stringify.includes matcher.
//
// To add a new node-pack compat:
//   1. Backend: add an entry to COMPAT_HANDLERS in nodes/metadata_collector.py with
//      a predicate, display_type, and transform.
//   2. Frontend: register here with `registerTrackedRenderer(display_type, { render, matchField })`
//      (or add directly to TRACKED_RENDERERS).
//
// Renderer contract:
//   render(entry, ctx) -> { dom: HTMLElement | null, consumedFields?: string[] } | HTMLElement | null
//     Returns the section's BODY DOM (between the title/source-line chrome and the
//     raw_widget_text details). Returning the bare DOM means "I rendered everything; do
//     not render a generic grid below me." Returning the structured object lets the
//     renderer advertise which `entry.fields` keys it consumed; the gallery renders any
//     remaining field keys in a generic 2-col grid below the renderer body. Null falls
//     back fully to the generic field grid.
//
//   renderField(entry, fieldKey, ctx) -> HTMLElement | null
//     Optional field-specific body for the pinned panel; null uses existing fallback.
//
//   matchField(entry, fieldKey, value) -> boolean | null
//     For the given field-name search token, returns a definitive match decision.
//     Null/undefined means "no opinion — fall through to the generic substring matcher."
//
// ctx (gallery-provided) bag:
//   { style, CHROME, formatGenerationValue, fieldSearchToken,
//     tokenActiveA, tokenActiveB, onFieldClick, onFieldContextMenu }
//
// tokenActiveA / tokenActiveB are compare-mode-aware booleans. Outside compare,
// tokenActiveA reflects the gallery's main search; tokenActiveB is always false.
// Inside compare, A reflects comparePickerQuery and B reflects comparePickerQueryB,
// so the visual highlight tracks the per-side compare filters.

export const TRACKED_RENDERERS = {};

export function registerTrackedRenderer(displayType, handler) {
    if (!displayType || !handler) return;
    TRACKED_RENDERERS[String(displayType)] = handler;
}

// Legacy sniffing: assets saved before the display_type field was introduced still carry the
// magic `fields.power_loras` shape from Phase 6 v1. Match it so they render through the
// registered renderer instead of falling through to the JSON-stringified generic grid. Sniffers
// only fire when entry.display_type is missing; explicit tags always win.
const LEGACY_SNIFFERS = [
    {
        display_type: "power_loras",
        match: (entry) => Array.isArray(entry?.fields?.power_loras),
    },
];

export function resolveDisplayType(entry) {
    if (entry?.display_type) return String(entry.display_type);
    for (const sniffer of LEGACY_SNIFFERS) {
        try {
            if (sniffer.match(entry)) return sniffer.display_type;
        } catch (_err) {
            // ignore and continue
        }
    }
    return null;
}

export function renderTrackedSectionBody(entry, ctx) {
    const displayType = resolveDisplayType(entry);
    const handler = displayType ? TRACKED_RENDERERS[displayType] : null;
    if (!handler || typeof handler.render !== "function") return null;
    try {
        return handler.render(entry, ctx);
    } catch (err) {
        console.warn("[tracked_metadata_renderers] render failed for", displayType, err);
        return null;
    }
}

export function renderTrackedFieldBody(entry, fieldKey, ctx) {
    const handler = TRACKED_RENDERERS[resolveDisplayType(entry)];
    if (typeof handler?.renderField !== "function") return null;
    try {
        return handler.renderField(entry, fieldKey, ctx);
    } catch (err) {
        console.warn("[tracked_metadata_renderers] field render failed", err);
        return null;
    }
}

export function trackedFieldMatchForEntry(entry, fieldKey, value) {
    const displayType = resolveDisplayType(entry);
    const handler = displayType ? TRACKED_RENDERERS[displayType] : null;
    if (!handler || typeof handler.matchField !== "function") return null;
    try {
        const result = handler.matchField(entry, fieldKey, value);
        if (typeof result === "boolean") return result;
    } catch (err) {
        console.warn("[tracked_metadata_renderers] matchField failed for", displayType, err);
    }
    return null;
}

// ---------- Power LoRA (rgthree) ----------

function powerLoraRowName(row) {
    return String(row?.name || row?.lora || row?.label || "-");
}

// Strength values are usually numbers but can arrive as strings; normalize so 0.80 and 0.8
// (or 1 and 1.0) compare equal. Non-numeric values pass through lowercased for stable matching.
function normPowerLoraStrength(value) {
    const num = Number(value);
    return Number.isFinite(num) ? String(num) : String(value).toLowerCase();
}

const POWER_LORA_STRENGTH_KEYS = [["strength", "s"], ["model_strength", "m"], ["clip_strength", "c"]];

// Encode a Power LoRA row into the `field:power_loras=` token value so the filter distinguishes
// exact configs (same LoRA at the same strength + on/off), not just the name. Compact JSON survives
// the gallery query parser's decodeURIComponent + lowercase, and encodeURIComponent keeps the token
// whitespace-free so the query tokenizer treats it as one term. The matcher below parses the same shape.
function encodePowerLoraFilterValue(row) {
    const payload = { n: powerLoraRowName(row), e: row?.enabled === false ? 0 : 1 };
    for (const [key, short] of POWER_LORA_STRENGTH_KEYS) {
        if (row && row[key] != null && row[key] !== "") payload[short] = normPowerLoraStrength(row[key]);
    }
    return JSON.stringify(payload);
}

// Parse a structured power_loras filter value (already lowercased by the query parser). Returns the
// decoded object, or null when the value is a plain name (manually-typed `field:power_loras=name`).
function parsePowerLoraFilterValue(value) {
    const raw = String(value || "").trim();
    if (!raw.startsWith("{")) return null;
    try {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === "object" && typeof parsed.n === "string") return parsed;
    } catch (_err) {
        // not structured — fall through to legacy name matching
    }
    return null;
}

function renderPowerLoraBody(entry, ctx, heading = "Power LoRAs", fieldKey = "power_loras", counts = false) {
    const rows = Array.isArray(entry?.fields?.[fieldKey])
        ? entry.fields[fieldKey].map((row, index) => ({ row, index }))
            .filter(({ row }) => row && typeof row === "object")
        : [];
    if (!rows.length && !counts) return null;

    const { style, CHROME, formatGenerationValue, fieldSearchToken,
        tokenActiveA, tokenActiveB, onFieldClick, onFieldContextMenu, rowCopy, onFieldCopy, copyOnHover } = ctx;

    const wrap = style(document.createElement("div"), `display:flex;flex-direction:column;gap:5px;min-width:0;`);
    const label = style(document.createElement("div"), `color:${CHROME.textSecondary};font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;`);
    label.textContent = counts
        ? `${heading} · ${rows.length} LoRA${rows.length === 1 ? "" : "s"} · ${rows.filter(({ row }) => row.enabled).length} on`
        : heading;
    const supplied = ctx.controlSupplied?.(entry, fieldKey);
    if (supplied) {
        const chip = style(document.createElement("span"), `margin-left:6px;padding:0 4px;border-radius:3px;border:1px solid rgba(143,192,240,0.4);color:#8fc0f0;font-size:9px;text-transform:none;letter-spacing:0;`);
        chip.textContent = "Control";
        chip.title = `Set by the Control “${supplied.label}”`;
        label.appendChild(chip);
    }
    wrap.appendChild(label);

    for (const { row, index } of rows) {
        const name = powerLoraRowName(row);
        const filterValue = encodePowerLoraFilterValue(row);
        const token = fieldSearchToken("power_loras", filterValue);
        const activeA = tokenActiveA ? tokenActiveA(token) : false;
        const activeB = tokenActiveB ? tokenActiveB(token) : false;
        const enabled = row.enabled !== false;
        // A native stack row the node does not apply (off or strength 0).
        const skipped = row.skipped === true;
        // Two-tone in compare mode (A=blue left half, B=amber right half), single-tone otherwise.
        let bg = "rgba(255,255,255,0.03)";
        let border = CHROME.borderSoft;
        let shadow = "none";
        if (activeA && activeB) {
            bg = "linear-gradient(90deg, rgba(143,192,240,0.16) 0%, rgba(143,192,240,0.16) 50%, rgba(232,184,109,0.18) 50%, rgba(232,184,109,0.18) 100%)";
            border = "rgba(187,176,170,0.55)";
            shadow = "inset 0 0 0 1px rgba(187,176,170,0.22)";
        } else if (activeA) {
            bg = "rgba(143,192,240,0.16)";
            border = "rgba(143,192,240,0.58)";
            shadow = "inset 0 0 0 1px rgba(143,192,240,0.22)";
        } else if (activeB) {
            bg = "rgba(232,184,109,0.16)";
            border = "rgba(232,184,109,0.58)";
            shadow = "inset 0 0 0 1px rgba(232,184,109,0.22)";
        }
        const line = style(document.createElement("button"), `
            appearance:none;text-align:left;width:100%;min-width:0;
            display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:center;
            padding:7px 48px 7px 8px;border-radius:6px;border:1px solid ${border};
            background:${bg};
            color:${enabled && !skipped ? CHROME.text : CHROME.textSecondary};cursor:pointer;
            box-shadow:${shadow};
        `);
        const main = style(document.createElement("div"), `min-width:0;display:flex;flex-direction:column;gap:2px;`);
        const title = style(document.createElement("div"), `font-size:10px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
        title.textContent = `${row.slot != null ? `${row.slot}. ` : ""}${name}`;
        title.title = name;
        line.title = name;
        const parts = [];
        for (const [key, labelText] of [
            ["strength", "strength"],
            ["model_strength", "model"],
            ["clip_strength", "clip"],
        ]) {
            if (row[key] != null && row[key] !== "") parts.push(`${labelText} ${formatGenerationValue(row[key])}`);
        }
        const sub = style(document.createElement("div"), `color:${CHROME.textSecondary};font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
        sub.textContent = parts.length ? parts.join(" | ") : "-";
        main.append(title, sub);
        // Coloured pill so a long stack can be scanned for what applied: green on,
        // red off, amber for a row that is on but applies nothing.
        const tone = !enabled ? "danger" : skipped ? "warning" : "success";
        const status = style(document.createElement("div"), `font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:0.05em;`
            + `padding:1px 5px;border-radius:3px;border:1px solid ${CHROME[`${tone}Border`]};`
            + `background:${CHROME[`${tone}Soft`]};color:${CHROME[`${tone}Text`]};`);
        status.textContent = !enabled ? "Off" : skipped ? "Skipped" : "On";
        line.append(main, status);

        const cellInfo = {
            entry,
            fieldKey,
            filterFieldKey: "power_loras",
            value: filterValue,
            displayKind: "power_lora_row",
            rowMeta: row,
            rowIndex: index,
        };
        line.addEventListener("click", (event) => onFieldClick?.(event, cellInfo));
        line.addEventListener("contextmenu", (event) => onFieldContextMenu?.(event, cellInfo));
        const rowWrap = style(document.createElement("div"), `position:relative;min-width:0;`);
        const copy = rowCopy?.(entry, index, fieldKey);
        const copyButton = style(document.createElement("button"), `appearance:none;position:absolute;right:6px;top:7px;padding:2px 4px;border-radius:4px;border:1px solid ${CHROME.accentBorder};background:${CHROME.panelRaised};color:${CHROME.text};font-size:9px;cursor:pointer;opacity:0;pointer-events:none;transition:opacity 120ms;`);
        copyButton.type = "button";
        copyButton.textContent = "Copy";
        copyButton.title = copy?.label || "Copy row JSON";
        copyButton.setAttribute("aria-label", copy?.kind === "raw" ? "Copy whole raw widget text" : "Copy LoRA row JSON");
        copyButton.disabled = !copy || copy.kind === "unavailable";
        copyButton.addEventListener("mousedown", (event) => event.stopPropagation());
        copyButton.addEventListener("click", (event) => {
            event.preventDefault();
            event.stopPropagation();
            onFieldCopy?.(copy);
        });
        copyButton.addEventListener("contextmenu", (event) => {
            onFieldContextMenu?.(event, cellInfo);
        });
        let hovered = false;
        const syncCopy = () => {
            // Hover reveals the row's Copy only in Compare; right-click copies elsewhere.
            const visible = (hovered && !!copyOnHover?.()) || rowWrap.contains(document.activeElement);
            copyButton.style.opacity = visible ? "1" : "0";
            copyButton.style.pointerEvents = visible ? "auto" : "none";
        };
        rowWrap.addEventListener("mouseenter", () => { hovered = true; syncCopy(); });
        rowWrap.addEventListener("mouseleave", () => { hovered = false; syncCopy(); });
        rowWrap.addEventListener("focusin", syncCopy);
        rowWrap.addEventListener("focusout", () => queueMicrotask(syncCopy));
        rowWrap.append(line, copyButton);
        wrap.appendChild(rowWrap);
    }
    // Tell the gallery we consumed only the structured `power_loras` list; let the generic
    // grid render any sibling count fields (enabled_lora_count, total_lora_count, etc.) so
    // the at-a-glance summary cells don't disappear when the rich rows take over.
    return { dom: wrap, consumedFields: [fieldKey] };
}

function matchPowerLoraField(entry, fieldKey, value) {
    if (fieldKey !== "power_loras") return null; // generic matcher handles enabled_lora_count etc.
    const rows = entry?.fields?.power_loras;
    if (!Array.isArray(rows)) return null;
    const structured = parsePowerLoraFilterValue(value);
    if (structured) {
        // Strict config match: same LoRA name, same on/off state, and every strength present in the
        // token must match. Returning false here intentionally blocks the generic fallback for this
        // entry — the structured matcher is authoritative for the power_loras field.
        for (const row of rows) {
            if (!row) continue;
            if (powerLoraRowName(row).toLowerCase() !== structured.n) continue;
            if ((row.enabled === false ? 0 : 1) !== (structured.e ? 1 : 0)) continue;
            let strengthMatch = true;
            for (const [key, short] of POWER_LORA_STRENGTH_KEYS) {
                if (structured[short] == null) continue;
                const rowVal = (row[key] != null && row[key] !== "") ? normPowerLoraStrength(row[key]) : null;
                if (rowVal !== structured[short]) { strengthMatch = false; break; }
            }
            if (strengthMatch) return true;
        }
        return false;
    }
    // Legacy / manually-typed `field:power_loras=name`: substring match on the LoRA name only.
    const needle = String(value || "").toLowerCase();
    if (!needle) return false;
    for (const row of rows) {
        if (!row) continue;
        const name = powerLoraRowName(row).toLowerCase();
        if (name.includes(needle)) return true;
    }
    return false;
}

registerTrackedRenderer("power_loras", {
    render: renderPowerLoraBody,
    matchField: matchPowerLoraField,
});

// ComfyUI's native LoRA stack (LoadLoraModel / LoadLoraTextEncoder): the same
// rows, search and Copy as Power LoRA, under its own heading.
registerTrackedRenderer("native_lora_stack", {
    render: (entry, ctx) => renderPowerLoraBody(entry, ctx, "LoRAs"),
    matchField: matchPowerLoraField,
});

// A subgraph section's fields are its interface values, keyed by input label.
// The body only says which outputs were tapped; every field stays in the generic
// grid, so pins, Copy and field search work unchanged.
function renderSubgraphBody(entry, ctx) {
    const names = Array.isArray(entry?.subgraph?.output_names) ? entry.subgraph.output_names : [];
    const outputs = names.map((name) => String(name || "").trim()).filter(Boolean);
    const { style, CHROME } = ctx;
    const line = style(document.createElement("div"), `color:${CHROME.textDim};font-size:10px;`);
    line.textContent = outputs.length
        ? `Subgraph · ${outputs.join(", ")} output${outputs.length > 1 ? "s" : ""}`
        : "Subgraph";
    return { dom: line, consumedFields: [] };
}

registerTrackedRenderer("subgraph", {
    render: renderSubgraphBody,
});

// ---------- Controls at run (server/controls_snapshot.py) ----------
//
// One section per asset: the Controls in effect for its run, keyed in `fields` by
// label so search, pins and Copy treat it like any collector section. `controls`
// carries the detail (group, seed mode, binding id) and `supplied` names the
// generic collector fields a Control set: those stay in their own section and
// carry a Control chip (maintainer choice at the Phase 3C gate, 2026-10-08).

function controlsOf(entry) {
    return Array.isArray(entry?.controls)
        ? entry.controls.filter((control) => control && typeof control === "object"
            && typeof control.field === "string")
        : [];
}

// Map<collector entry, Map<field, {binding_id, label}>>. Indices refer to the
// stored tracked_metadata list; an index whose section no longer names the
// target is ignored.
export function controlsSuppliedLookup(entries) {
    const list = Array.isArray(entries) ? entries : [];
    const byEntry = new Map();
    for (const controlsEntry of list) {
        if (controlsEntry?.display_type !== "controls" || !Array.isArray(controlsEntry.supplied)) continue;
        const byId = new Map(controlsOf(controlsEntry).map((control) => [control.binding_id, control]));
        for (const item of controlsEntry.supplied) {
            const target = list[item?.section_index];
            const control = byId.get(item?.binding_id);
            if (!target || target === controlsEntry || !control) continue;
            const field = String(item.field ?? "");
            const fields = target.fields && typeof target.fields === "object" ? target.fields : {};
            if (!Object.prototype.hasOwnProperty.call(fields, field)) continue;
            // A structured section is marked only through its row group (a native
            // LoRA stack's rows); server controls_snapshot._supplied mirrors it.
            const input = target.display_type ? (field === "power_loras" ? target.source_group : null) : field;
            if (!input || !control.targets?.some?.((t) => t?.path === target.source_node_id && t?.input === input)) continue;
            if (!byEntry.has(target)) byEntry.set(target, new Map());
            byEntry.get(target).set(field, {
                binding_id: control.binding_id,
                label: String(control.label || control.field),
            });
        }
    }
    return {
        supplied: (entry, field) => byEntry.get(entry)?.get(String(field)) || null,
    };
}

const SEED_MODE_TEXT = {
    fixed: "fixed",
    increment: "increment",
    decrement: "decrement",
    randomize: "randomize",
};

// A display annotation alone cannot turn arbitrary stored JSON into LoRA rows.
function nativeControlRows(entry, control) {
    const rows = entry?.fields?.[control?.field];
    if (control?.kind !== "ROWS" || control.display_type !== "native_lora_stack" || !Array.isArray(rows)) return null;
    return rows.every((row) => row && typeof row === "object" && typeof row.name === "string"
        && typeof row.enabled === "boolean" && typeof row.strength === "number" && Number.isFinite(row.strength)
        && Number.isSafeInteger(row.slot) && row.slot > 0) ? rows : null;
}

function decorateControl(cell, control, ctx) {
    const { style, CHROME } = ctx;
    const parts = [`Control ${control.binding_id || ""}`.trim()];
    if (control.origin === "local") parts.push("App only");
    if (control.advanced === true) parts.push("Advanced");
    const mode = SEED_MODE_TEXT[control.seed_mode];
    if (mode) parts.push(`seed ${mode}`);
    cell.title = [cell.title, parts.join(" · ")].filter(Boolean).join("\n");
    const titleEl = cell.firstChild;
    if (titleEl && mode) {
        const note = style(document.createElement("span"), `color:${CHROME.textSecondary};margin-left:6px;`);
        note.textContent = `· ${mode}`;
        titleEl.appendChild(note);
    }
    if (titleEl && control.advanced === true) {
        const tag = style(document.createElement("span"),
            `margin-left:6px;padding:0 4px;border:1px solid ${CHROME.border};border-radius:3px;color:${CHROME.textDim};font-size:9px;`);
        tag.textContent = "Adv";
        tag.dataset.controlsAdvanced = "true";
        titleEl.appendChild(tag);
    }
    if (control.rows_omitted === true) {
        const note = style(document.createElement("div"), `color:${CHROME.textSecondary};font-size:10px;line-height:1.4;`);
        note.textContent = "LoRA rows were not recorded because this metadata section exceeded its size limit.";
        cell.appendChild(note);
    }
}

function renderControlField(entry, fieldKey, ctx) {
    const control = controlsOf(entry).find((item) => item.field === fieldKey);
    if (nativeControlRows(entry, control) === null) return null;
    const body = renderPowerLoraBody(entry, ctx, String(control.field), fieldKey, true).dom;
    decorateControl(body, control, ctx);
    return body;
}

function matchControlsField(entry, fieldKey, value) {
    if (fieldKey !== "power_loras") return null;
    let hasRows = false;
    const typedFields = new Set();
    for (const control of controlsOf(entry)) {
        const rows = nativeControlRows(entry, control);
        if (rows === null) continue;
        hasRows = true;
        typedFields.add(control.field);
        if (matchPowerLoraField({ fields: { power_loras: rows } }, fieldKey, value)) return true;
    }
    // An actual scalar Control may use the same name as the virtual row field.
    for (const [key, literal] of Object.entries(entry?.fields || {})) {
        if (key.toLowerCase() !== fieldKey || typedFields.has(key)) continue;
        const text = literal == null || literal === "" ? "-"
            : typeof literal === "object" ? JSON.stringify(literal) : String(literal);
        if (text.toLowerCase().includes(String(value).toLowerCase())) return true;
    }
    return hasRows ? false : null;
}

function renderControlsBody(entry, ctx) {
    const { style, CHROME } = ctx;
    const controls = controlsOf(entry);
    const wrap = style(document.createElement("div"), `display:flex;flex-direction:column;gap:6px;min-width:0;`);
    const help = style(document.createElement("div"), `color:${CHROME.textDim};font-size:10px;line-height:1.4;`);
    const truncated = Number.isSafeInteger(entry?.truncated) && entry.truncated > 0 ? entry.truncated : 0;
    help.textContent = "The Controls' values when this run started. A Control on a connected branch "
        + "that was switched off during the run is still listed."
        + (truncated ? ` ${truncated} more Control${truncated === 1 ? " was" : "s were"} not recorded.` : "");
    wrap.appendChild(help);
    if (!controls.length || typeof ctx.renderFieldGrid !== "function") {
        return { dom: wrap, consumedFields: [] };
    }
    const groups = [];
    const bySection = new Map();
    for (const control of controls) {
        const section = control.section === "editor" ? "Editor" : "";
        const group = String(control.group || "");
        const name = section && group ? `${section} › ${group}` : (section || group);
        if (!bySection.has(name)) {
            bySection.set(name, []);
            groups.push(name);
        }
        bySection.get(name).push(control);
    }
    for (const name of groups) {
        if (name) {
            const heading = style(document.createElement("div"), `color:#8fa4b6;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;margin-top:2px;`);
            heading.textContent = name;
            wrap.appendChild(heading);
        }
        const members = bySection.get(name);
        const byField = new Map(members.map((control) => [control.field, control]));
        let pending = [];
        const flush = () => {
            if (!pending.length) return;
            const grid = ctx.renderFieldGrid(entry, pending.map((control) => control.field), (cell, field) => {
                decorateControl(cell, byField.get(field), ctx);
            });
            if (grid) wrap.appendChild(grid);
            pending = [];
        };
        for (const control of members) {
            const body = renderControlField(entry, control.field, ctx);
            if (body) {
                flush();
                wrap.appendChild(body);
            } else {
                pending.push(control);
            }
        }
        flush();
    }
    // Scalars use gallery cells; native stacks have a field-specific pin body.
    const fields = entry?.fields && typeof entry.fields === "object" ? entry.fields : {};
    const consumed = controls.map((control) => control.field)
        .filter((field) => Object.prototype.hasOwnProperty.call(fields, field));
    return { dom: wrap, consumedFields: consumed, pinnedFieldsGeneric: true };
}

registerTrackedRenderer("controls", {
    render: renderControlsBody,
    renderField: renderControlField,
    matchField: matchControlsField,
});
