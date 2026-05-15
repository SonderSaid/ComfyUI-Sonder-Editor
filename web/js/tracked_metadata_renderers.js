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

function renderPowerLoraBody(entry, ctx) {
    const rows = Array.isArray(entry?.fields?.power_loras)
        ? entry.fields.power_loras.filter((row) => row && typeof row === "object")
        : [];
    if (!rows.length) return null;

    const { style, CHROME, formatGenerationValue, fieldSearchToken,
        tokenActiveA, tokenActiveB, onFieldClick, onFieldContextMenu } = ctx;

    const wrap = style(document.createElement("div"), `display:flex;flex-direction:column;gap:5px;min-width:0;`);
    const label = style(document.createElement("div"), `color:#8fa4b6;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;`);
    label.textContent = "Power LoRAs";
    wrap.appendChild(label);

    for (const row of rows) {
        const name = powerLoraRowName(row);
        const token = fieldSearchToken("power_loras", name);
        const activeA = tokenActiveA ? tokenActiveA(token) : false;
        const activeB = tokenActiveB ? tokenActiveB(token) : false;
        const enabled = row.enabled !== false;
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
            padding:7px 8px;border-radius:6px;border:1px solid ${border};
            background:${bg};
            color:${enabled ? "#e4edf4" : "#9ca8b2"};cursor:pointer;
            box-shadow:${shadow};
        `);
        const main = style(document.createElement("div"), `min-width:0;display:flex;flex-direction:column;gap:2px;`);
        const title = style(document.createElement("div"), `font-size:10px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
        title.textContent = `${row.slot != null ? `${row.slot}. ` : ""}${name}`;
        const parts = [];
        for (const [key, labelText] of [
            ["strength", "strength"],
            ["model_strength", "model"],
            ["clip_strength", "clip"],
        ]) {
            if (row[key] != null && row[key] !== "") parts.push(`${labelText} ${formatGenerationValue(row[key])}`);
        }
        const sub = style(document.createElement("div"), `color:#8fa4b6;font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
        sub.textContent = parts.length ? parts.join(" | ") : "-";
        main.append(title, sub);
        const status = style(document.createElement("div"), `font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:0.05em;color:${enabled ? "#b7e4b4" : "#c0a49a"};`);
        status.textContent = enabled ? "On" : "Off";
        line.append(main, status);

        const cellInfo = {
            entry,
            fieldKey: "power_loras",
            value: name,
            displayKind: "power_lora_row",
            rowMeta: row,
        };
        line.addEventListener("click", (event) => onFieldClick?.(event, cellInfo));
        line.addEventListener("contextmenu", (event) => onFieldContextMenu?.(event, cellInfo));
        wrap.appendChild(line);
    }
    // Tell the gallery we consumed only the structured `power_loras` list; let the generic
    // grid render any sibling count fields (enabled_lora_count, total_lora_count, etc.) so
    // the at-a-glance summary cells don't disappear when the rich rows take over.
    return { dom: wrap, consumedFields: ["power_loras"] };
}

function matchPowerLoraField(entry, fieldKey, value) {
    if (fieldKey !== "power_loras") return null; // generic matcher handles enabled_lora_count etc.
    const rows = entry?.fields?.power_loras;
    if (!Array.isArray(rows)) return null;
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
