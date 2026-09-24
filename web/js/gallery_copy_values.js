// Pure Copy payload resolution for Asset Gallery metadata. Stored previews stay the
// search/filter authority; these optional sources only supply complete clipboard text.

export const TRUNCATED_MARKER = "... [truncated, full in raw_widget_text]";

const hasOwn = (value, key) => !!value && Object.prototype.hasOwnProperty.call(value, key);
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
    if (entry?.display_type || typeof raw !== "string" || !fields || typeof fields !== "object"
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

function powerLoraRows(entry) {
    const previewRows = entry?.fields?.power_loras;
    if (!Array.isArray(previewRows)) return null;
    const completeRows = entry?.full_fields?.power_loras;
    if (completeRows === undefined) return previewRows;
    return Array.isArray(completeRows) && completeRows.length === previewRows.length
        ? completeRows : null;
}

export function resolvePowerLoraRowCopies(entry, rowIndex) {
    const rows = powerLoraRows(entry);
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

export function resolvePowerLoraNamesCopy(entry) {
    const rows = powerLoraRows(entry);
    if (!rows || rows.some(containsTruncatedMarker)) return rawSectionCopy(entry);
    return full(rows.map((row) => row && (row.name || row.lora || ""))
        .filter(Boolean).join("\n"), "Copy LoRA names");
}
