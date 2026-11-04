// Pure Copy payload resolution for Asset Gallery metadata. Stored previews stay the
// search/filter authority; these optional sources only supply complete clipboard text.

export const TRUNCATED_MARKER = "... [truncated, full in raw_widget_text]";
// Controls at run (server/controls_snapshot.py) caps long text values with this
// marker. Since Hardening Phase 10 the asset keeps the complete text beside the
// section (`generation_params.controls_text`), never inside it; older takes have
// only the preview. The stored marker never changes: older packs detect it.
export const CONTROLS_TRUNCATED_MARKER = "… [truncated, full value in the source prompt]";
// Mirrors controls_snapshot.FULL_TEXT_KEY (parity-tested).
export const CONTROLS_FULL_TEXT_KEY = "controls_text";

const hasOwn = (value, key) => !!value && Object.prototype.hasOwnProperty.call(value, key);

// Section entry -> its asset's full-text record. Filled where entries are made
// from an asset's detail params (`shared_asset_gallery.js`), so every Copy and
// cell of that section resolves the same way without new call-site arguments.
const controlsFullTextBySection = new WeakMap();

export function rememberControlsFullText(entries, params) {
    const record = params && typeof params === "object" ? params[CONTROLS_FULL_TEXT_KEY] : undefined;
    for (const entry of Array.isArray(entries) ? entries : []) {
        if (entry?.display_type !== "controls" || typeof entry !== "object") continue;
        if (record && typeof record === "object") controlsFullTextBySection.set(entry, record);
        else controlsFullTextBySection.delete(entry);
    }
}

// The one reader of a full-text record (server: controls_snapshot.controls_full_text).
export function controlsFullText(record, bindingId) {
    const values = record && typeof record === "object" ? record.values : null;
    const value = values && typeof values === "object" && typeof bindingId === "string" && hasOwn(values, bindingId)
        ? values[bindingId] : undefined;
    return typeof value === "string" ? value : null;
}

// The complete text behind a capped Controls preview, or null. It must extend the
// preview (minus the marker) and be longer, so a stale or foreign record is ignored.
function controlsKeptText(entry, key, preview) {
    if (typeof preview !== "string" || !preview.endsWith(CONTROLS_TRUNCATED_MARKER)) return null;
    const control = Array.isArray(entry?.controls) ? entry.controls.find((item) => item?.field === key) : null;
    const text = controlsFullText(controlsFullTextBySection.get(entry), control?.binding_id);
    const head = preview.slice(0, -CONTROLS_TRUNCATED_MARKER.length);
    return text !== null && text.length > head.length && text.startsWith(head) ? text : null;
}

// The cell text for a tracked field. A capped Controls preview whose full text
// is kept says so instead of pointing at the source prompt; everything else is
// shown as rendered. Display only: filters and search read the stored value.
export function trackedFieldDisplayText(entry, fieldKey, rendered) {
    if (entry?.display_type !== "controls" || typeof rendered !== "string") return rendered;
    const key = String(fieldKey);
    const text = controlsKeptText(entry, key, entry?.fields?.[key]);
    if (text === null || !rendered.endsWith(CONTROLS_TRUNCATED_MARKER)) return rendered;
    const count = Array.from(text).length;
    return rendered.slice(0, -CONTROLS_TRUNCATED_MARKER.length)
        + `… (Copy value copies all ${count.toLocaleString("en-US")} characters)`;
}
const full = (text, label = "Copy value") => ({ kind: "value", label, text });
const unavailable = () => ({ kind: "unavailable", label: "Full value unavailable", text: "" });

export function copyTextForValue(value) {
    if (value == null) return "-";
    if (typeof value === "string") return value;
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    try {
        return JSON.stringify(value);
    } catch {
        return String(value);
    }
}

export function containsTruncatedMarker(value) {
    if (typeof value === "string") return value.endsWith(TRUNCATED_MARKER);
    if (Array.isArray(value)) return value.some(containsTruncatedMarker);
    if (value && typeof value === "object") return Object.values(value).some(containsTruncatedMarker);
    return false;
}

function rawSectionCopy(entry) {
    const raw = entry?.raw_widget_text;
    return typeof raw === "string" && raw
        ? { kind: "raw", label: "Copy raw widget text (whole section)", text: raw }
        : unavailable();
}

function rawSpanText(entry, key) {
    // Validate the complete collector partition before using any one offset.
    // Offsets detect inconsistent saved metadata, not coherently forged data.
    const raw = entry?.raw_widget_text;
    const fields = entry?.fields;
    const spans = entry?.raw_field_spans;
    // Subgraph interface sections cap like generic ones: raw text is their fields.
    const spanned = !entry?.display_type || entry.display_type === "subgraph";
    if (!spanned || typeof raw !== "string" || !fields || typeof fields !== "object"
            || !spans || typeof spans !== "object") return null;
    const keys = Object.keys(fields);
    if (!keys.includes(key) || Object.keys(spans).length !== keys.length
            || !keys.every((fieldKey) => hasOwn(spans, fieldKey))) return null;
    const ordered = keys.map((fieldKey) => ({ key: fieldKey, span: spans[fieldKey] }));
    if (ordered.some(({ span }) => !Array.isArray(span) || span.length !== 2
            || !Number.isSafeInteger(span[0]) || !Number.isSafeInteger(span[1])
            || span[0] < 0 || span[1] < span[0] || span[1] > raw.length)) return null;
    ordered.sort((a, b) => a.span[0] - b.span[0]);
    let cursor = 0;
    for (const [index, field] of ordered.entries()) {
        const [start, end] = field.span;
        const prefix = (index ? ", " : "") + field.key + ": ";
        if (raw.slice(cursor, start) !== prefix) return null;
        const candidate = raw.slice(start, end);
        const preview = fields[field.key];
        if (typeof preview === "string") {
            if (containsTruncatedMarker(preview)) {
                if (!candidate.startsWith(preview.slice(0, -TRUNCATED_MARKER.length))) return null;
            } else if (candidate !== preview) return null;
        } else if (typeof preview === "boolean") {
            if (candidate !== (preview ? "True" : "False")) return null;
        } else if (typeof preview === "number" && Number.isFinite(preview)) {
            if (!/^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(candidate)
                    || Number(candidate) !== preview) return null;
        } else if (preview === null && candidate !== "None") {
            return null;
        }
        cursor = end;
    }
    return cursor === raw.length ? raw.slice(...spans[key]) : null;
}

export function resolveTrackedFieldCopy(entry, fieldKey) {
    const key = String(fieldKey);
    const preview = entry?.fields?.[key];
    if (entry?.display_type === "controls") {
        if (preview == null) return unavailable();
        const control = Array.isArray(entry.controls) ? entry.controls.find((item) => item?.field === key) : null;
        if (control?.rows_omitted === true) return full(copyTextForValue(preview), "Copy summary");
        const sourceOnly = { kind: "unavailable", label: "Full value is only in the run's source prompt", text: "" };
        // A row list's text was cut with "…", which can't be told from a value
        // ending in one: new captures mark it (`capped`), so Copy never offers it
        // as complete. Older takes can't be told apart and keep copying the text.
        if (control?.capped === true) return sourceOnly;
        if (typeof preview === "string" && preview.endsWith(CONTROLS_TRUNCATED_MARKER)) {
            const text = controlsKeptText(entry, key, preview);
            return text === null ? sourceOnly : full(text);
        }
        return full(copyTextForValue(preview));
    }
    if (hasOwn(entry?.full_fields, key) && entry.full_fields[key] !== undefined) {
        return full(copyTextForValue(entry.full_fields[key]));
    }
    if (!containsTruncatedMarker(preview)) return full(copyTextForValue(preview));

    if (typeof preview === "string") {
        const fromSpan = rawSpanText(entry, key);
        if (fromSpan !== null) return full(fromSpan);
        // Expiry: old collector sections have no spans. One generic field has an
        // unambiguous raw prefix; multi-field raw text cannot be split safely.
        if (!entry?.display_type && Object.keys(entry?.fields || {}).length === 1) {
            const raw = entry?.raw_widget_text;
            const prefix = key + ": ";
            if (typeof raw === "string" && raw.startsWith(prefix)) {
                const candidate = raw.slice(prefix.length);
                if (candidate.startsWith(preview.slice(0, -TRUNCATED_MARKER.length))) {
                    return full(candidate);
                }
            }
        }
    }
    return rawSectionCopy(entry);
}

function powerLoraRows(entry, fieldKey) {
    const previewRows = entry?.fields?.[fieldKey];
    if (!Array.isArray(previewRows)) return null;
    const completeRows = entry?.full_fields?.[fieldKey];
    if (completeRows === undefined) return previewRows;
    return Array.isArray(completeRows) && completeRows.length === previewRows.length
        ? completeRows : null;
}

export function resolvePowerLoraRowCopies(entry, rowIndex, fieldKey = "power_loras") {
    const rows = powerLoraRows(entry, fieldKey);
    const row = Number.isSafeInteger(rowIndex) && rowIndex >= 0 ? rows?.[rowIndex] : null;
    if (!row || typeof row !== "object") {
        const fallback = rawSectionCopy(entry);
        return { name: fallback, row: fallback };
    }
    if (containsTruncatedMarker(row)) {
        const fallback = rawSectionCopy(entry);
        return { name: fallback, row: fallback };
    }
    const name = String(row.name || row.lora || row.label || "-");
    return {
        name: full(name, "Copy LoRA name"),
        row: full(JSON.stringify(row), "Copy row JSON"),
    };
}

export function resolvePowerLoraNamesCopy(entry, fieldKey = "power_loras") {
    const rows = powerLoraRows(entry, fieldKey);
    if (!rows || rows.some(containsTruncatedMarker)) return rawSectionCopy(entry);
    return full(rows.map((row) => row && (row.name || row.lora || ""))
        .filter(Boolean).join("\n"), "Copy LoRA names");
}
