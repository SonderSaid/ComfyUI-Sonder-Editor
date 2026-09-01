const RESERVED_TAG_PREFIX = "sonder:";
const VALID_KINDS = new Set(["character", "location", "prop", "outfit"]);
const VALID_CLASSES = new Set(["subject", "context"]);

function text(value) {
    return String(value ?? "").trim().replace(/\s+/g, " ");
}

function clamp(value, min, max) {
    return Math.min(max, Math.max(min, Number(value) || 0));
}

export function normalizeReferenceCropPercent(crop = null) {
    if (!crop) return { x: 0, y: 0, w: 100, h: 100 };
    const x = clamp(crop.x, 0, 99);
    const y = clamp(crop.y, 0, 99);
    const w = clamp(crop.w, 1, 100 - x);
    const h = clamp(crop.h, 1, 100 - y);
    return { x, y, w, h };
}

export function moveReferenceCrop(crop, dx, dy) {
    const next = normalizeReferenceCropPercent(crop);
    next.x = clamp(next.x + Number(dx || 0), 0, 100 - next.w);
    next.y = clamp(next.y + Number(dy || 0), 0, 100 - next.h);
    return next;
}

export function resizeReferenceCrop(crop, handle, dx, dy, minimum = 1) {
    const current = normalizeReferenceCropPercent(crop);
    let left = current.x;
    let top = current.y;
    let right = current.x + current.w;
    let bottom = current.y + current.h;
    const minSize = clamp(minimum, 0.1, 100);
    if (String(handle).includes("w")) left = clamp(left + Number(dx || 0), 0, right - minSize);
    if (String(handle).includes("e")) right = clamp(right + Number(dx || 0), left + minSize, 100);
    if (String(handle).includes("n")) top = clamp(top + Number(dy || 0), 0, bottom - minSize);
    if (String(handle).includes("s")) bottom = clamp(bottom + Number(dy || 0), top + minSize, 100);
    return { x: left, y: top, w: right - left, h: bottom - top };
}

export function normalizeReferenceTrim(start, end, duration, minimum = 0.01) {
    const total = Math.max(Number(duration) || 0, minimum);
    const minSpan = Math.min(Math.max(Number(minimum) || 0.01, 0.001), total);
    const nextStart = clamp(start, 0, Math.max(0, total - minSpan));
    const rawEnd = end === "" || end == null ? total : Number(end);
    const nextEnd = clamp(Number.isFinite(rawEnd) ? rawEnd : total, nextStart + minSpan, total);
    return { start: nextStart, end: nextEnd };
}

export function shiftReferenceTrim(start, end, duration, delta, minimum = 0.01) {
    const total = Math.max(Number(duration) || 0, minimum);
    const range = normalizeReferenceTrim(start, end, total, minimum);
    const span = range.end - range.start;
    const nextStart = clamp(range.start + Number(delta || 0), 0, Math.max(0, total - span));
    return { start: nextStart, end: nextStart + span };
}

export function referenceMediaWindow(mode, start, end, duration) {
    const total = Math.max(0, Number(duration) || 0);
    if (!(total > 0)) return { start: 0, end: 0, duration: 0 };
    if (mode !== "result") return { start: 0, end: total, duration: total };
    const range = normalizeReferenceTrim(start, end, total);
    return { start: range.start, end: range.end, duration: range.end - range.start };
}

export function sliceReferenceWaveformPeaks(peaks, start, end, duration) {
    if (!Array.isArray(peaks) || !peaks.length) return [];
    const total = Math.max(0, Number(duration) || 0);
    if (!(total > 0)) return [...peaks];
    const left = clamp(start, 0, total) / total;
    const right = clamp(end, 0, total) / total;
    const first = Math.min(peaks.length - 1, Math.floor(left * peaks.length));
    const afterLast = Math.max(first + 1, Math.min(peaks.length, Math.ceil(right * peaks.length)));
    return peaks.slice(first, afterLast);
}

export function referenceCropAspectRatio(sourceWidth, sourceHeight, ratioWidth, ratioHeight) {
    const sw = Number(sourceWidth);
    const sh = Number(sourceHeight);
    const rw = Number(ratioWidth);
    const rh = Number(ratioHeight);
    if (!(sw > 0 && sh > 0 && rw > 0 && rh > 0)) return 0;
    return (rw / rh) / (sw / sh);
}

export function fitReferenceCropToAspect(crop, sourceWidth, sourceHeight, ratioWidth, ratioHeight, minimum = 1) {
    const current = normalizeReferenceCropPercent(crop);
    const ratio = referenceCropAspectRatio(sourceWidth, sourceHeight, ratioWidth, ratioHeight);
    const minSize = clamp(minimum, 0.1, 100);
    if (!(ratio > 0)) return { crop: current, error: "Source dimensions are not ready." };
    let width = current.w;
    let height = width / ratio;
    if (height > current.h) {
        height = current.h;
        width = height * ratio;
    }
    if (width < minSize || height < minSize) {
        return { crop: current, error: "That ratio cannot satisfy the minimum crop size for this source." };
    }
    const centerX = current.x + current.w / 2;
    const centerY = current.y + current.h / 2;
    return {
        crop: {
            x: clamp(centerX - width / 2, 0, 100 - width),
            y: clamp(centerY - height / 2, 0, 100 - height),
            w: width,
            h: height,
        },
        error: "",
    };
}

function clampLockedDimensions(width, height, ratio, maxWidth, maxHeight, minimum) {
    let nextWidth = Math.max(0, Number(width) || 0);
    let nextHeight = Math.max(0, Number(height) || 0);
    if (nextWidth / Math.max(nextHeight, 1e-9) !== ratio) {
        nextHeight = nextWidth / ratio;
    }
    const scale = Math.min(1, maxWidth / Math.max(nextWidth, 1e-9), maxHeight / Math.max(nextHeight, 1e-9));
    nextWidth *= scale;
    nextHeight *= scale;
    if (nextWidth < minimum || nextHeight < minimum) return null;
    return { width: nextWidth, height: nextHeight };
}

export function resizeReferenceCropLocked(crop, handle, dx, dy, sourceWidth, sourceHeight, ratioWidth, ratioHeight, minimum = 1) {
    const current = normalizeReferenceCropPercent(crop);
    const ratio = referenceCropAspectRatio(sourceWidth, sourceHeight, ratioWidth, ratioHeight);
    const minSize = clamp(minimum, 0.1, 100);
    const key = String(handle || "");
    if (!(ratio > 0) || !/[nsew]/.test(key)) return current;
    let desiredWidth = current.w;
    let desiredHeight = current.h;
    if ((key.includes("e") || key.includes("w")) && (key.includes("n") || key.includes("s"))) {
        const horizontal = current.w + (key.includes("e") ? Number(dx || 0) : -Number(dx || 0));
        const vertical = current.h + (key.includes("s") ? Number(dy || 0) : -Number(dy || 0));
        if (Math.abs(Number(dx || 0)) / Math.max(current.w, 1) >= Math.abs(Number(dy || 0)) / Math.max(current.h, 1)) {
            desiredWidth = horizontal;
            desiredHeight = desiredWidth / ratio;
        } else {
            desiredHeight = vertical;
            desiredWidth = desiredHeight * ratio;
        }
        const anchorX = key.includes("e") ? current.x : current.x + current.w;
        const anchorY = key.includes("s") ? current.y : current.y + current.h;
        const maxWidth = key.includes("e") ? 100 - anchorX : anchorX;
        const maxHeight = key.includes("s") ? 100 - anchorY : anchorY;
        const size = clampLockedDimensions(desiredWidth, desiredHeight, ratio, maxWidth, maxHeight, minSize);
        if (!size) return current;
        return {
            x: key.includes("e") ? anchorX : anchorX - size.width,
            y: key.includes("s") ? anchorY : anchorY - size.height,
            w: size.width,
            h: size.height,
        };
    }
    if (key.includes("e") || key.includes("w")) {
        desiredWidth = current.w + (key.includes("e") ? Number(dx || 0) : -Number(dx || 0));
        desiredHeight = desiredWidth / ratio;
        const anchorX = key.includes("e") ? current.x : current.x + current.w;
        const centerY = current.y + current.h / 2;
        const size = clampLockedDimensions(
            desiredWidth,
            desiredHeight,
            ratio,
            key.includes("e") ? 100 - anchorX : anchorX,
            2 * Math.min(centerY, 100 - centerY),
            minSize,
        );
        if (!size) return current;
        return { x: key.includes("e") ? anchorX : anchorX - size.width, y: centerY - size.height / 2, w: size.width, h: size.height };
    }
    desiredHeight = current.h + (key.includes("s") ? Number(dy || 0) : -Number(dy || 0));
    desiredWidth = desiredHeight * ratio;
    const anchorY = key.includes("s") ? current.y : current.y + current.h;
    const centerX = current.x + current.w / 2;
    const size = clampLockedDimensions(
        desiredWidth,
        desiredHeight,
        ratio,
        2 * Math.min(centerX, 100 - centerX),
        key.includes("s") ? 100 - anchorY : anchorY,
        minSize,
    );
    if (!size) return current;
    return { x: centerX - size.width / 2, y: key.includes("s") ? anchorY : anchorY - size.height, w: size.width, h: size.height };
}

export function setReferenceCropLockedSize(crop, axis, value, sourceWidth, sourceHeight, ratioWidth, ratioHeight, minimum = 1) {
    const current = normalizeReferenceCropPercent(crop);
    const ratio = referenceCropAspectRatio(sourceWidth, sourceHeight, ratioWidth, ratioHeight);
    const minSize = clamp(minimum, 0.1, 100);
    if (!(ratio > 0)) return current;
    const desiredWidth = axis === "h" ? Number(value) * ratio : Number(value);
    const desiredHeight = axis === "h" ? Number(value) : Number(value) / ratio;
    const centerX = current.x + current.w / 2;
    const centerY = current.y + current.h / 2;
    const size = clampLockedDimensions(
        desiredWidth,
        desiredHeight,
        ratio,
        2 * Math.min(centerX, 100 - centerX),
        2 * Math.min(centerY, 100 - centerY),
        minSize,
    );
    if (!size) return current;
    return { x: centerX - size.width / 2, y: centerY - size.height / 2, w: size.width, h: size.height };
}

export function defaultReferenceClass(kind) {
    return kind === "location" ? "context" : "subject";
}

export function catalogById(catalog = []) {
    return new Map((Array.isArray(catalog) ? catalog : []).map((entry) => [entry.id, entry]));
}

/** Resolve one durable tag id for display without deriving meaning from its id. */
export function formatReferenceTag(tag, {
    catalog = [], families = {}, density = "long",
} = {}) {
    const raw = String(tag ?? "");
    const preset = catalogById(catalog).get(raw);
    if (!preset) return raw;
    const label = String(preset.label || raw);
    const familyId = preset.family;
    if (!familyId) return label;
    const family = families && typeof families === "object" ? families[familyId] : null;
    if (!family || typeof family !== "object") return raw;
    const familyLabel = String(density === "short"
        ? (family.short || family.label || "")
        : (family.label || family.short || ""));
    if (!familyLabel) return raw;
    return density === "short" ? `${familyLabel}·${label}` : `${familyLabel} · ${label}`;
}

export function referenceTagSearchText(tag, options = {}) {
    const raw = String(tag ?? "");
    const label = formatReferenceTag(raw, options);
    return label && label !== raw ? `${raw} ${label}` : raw;
}

export function assetHasReferenceAudio(asset) {
    return asset?.asset_type === "audio"
        || (asset?.asset_type === "video" && asset?.has_audio === true);
}

export function referencePresetAcceptsAsset(preset, asset) {
    if (!preset || !asset?.asset_type) return false;
    if (!Array.isArray(preset.asset_types) || !preset.asset_types.includes(asset.asset_type)) return false;
    return !preset.requires_audio || assetHasReferenceAudio(asset);
}

export function compatibleReferencePresets(catalog = [], asset = null) {
    if (!asset) return [];
    return (Array.isArray(catalog) ? catalog : []).filter((preset) => referencePresetAcceptsAsset(preset, asset));
}

export function incompatibleReferencePresetTags(tags = [], catalog = [], asset = null) {
    if (!asset) return [];
    const presets = catalogById(catalog);
    return (Array.isArray(tags) ? tags : []).filter((tag) => {
        const preset = presets.get(tag);
        return !!preset && !referencePresetAcceptsAsset(preset, asset);
    });
}

export function normalizeReferenceTags(values, catalog = [], { strict = false } = {}) {
    const presets = catalogById(catalog);
    const seen = new Set();
    const tags = [];
    const errors = [];
    for (const raw of Array.isArray(values) ? values : []) {
        const tag = text(raw);
        if (!tag) continue;
        const key = tag.toLocaleLowerCase();
        if (seen.has(key)) continue;
        seen.add(key);
        if (key.startsWith(RESERVED_TAG_PREFIX) && !presets.has(tag)) {
            if (strict) errors.push(`Custom tags cannot begin with ${RESERVED_TAG_PREFIX}`);
            else tags.push(tag);
            continue;
        }
        tags.push(tag);
    }
    return { tags, errors };
}

export function createReferenceDraft(source = null) {
    const kind = VALID_KINDS.has(source?.kind) ? source.kind : "character";
    return {
        reference_id: source?.reference_id || "",
        name: source?.name || "",
        kind,
        reference_class: VALID_CLASSES.has(source?.reference_class)
            ? source.reference_class
            : defaultReferenceClass(kind),
        description: source?.description || "",
        visual_intent: ["preserve", "partial", "transfer_attributes", "reference_loosely"]
            .includes(source?.visual_intent) ? source.visual_intent : "preserve",
        audio_intent: ["copy_full", "copy_partial", "reference_characteristics", "reference_loosely"]
            .includes(source?.audio_intent) ? source.audio_intent : "reference_characteristics",
    };
}

export function createMemberDraft(source = null, asset = null) {
    const crop = source?.crop && typeof source.crop === "object" ? source.crop : null;
    const suggestedName = String(asset?.name || "").replace(/\.[^.]+$/, "").trim();
    return {
        member_id: source?.member_id || "",
        asset_id: source?.asset_id || asset?.asset_id || "",
        name: source?.name || (!source ? suggestedName : ""),
        asset_type: asset?.asset_type || "",
        tags: Array.isArray(source?.tags) ? [...source.tags] : [],
        prompt: source?.prompt || "",
        crop: crop ? { x: crop.x * 100, y: crop.y * 100, w: crop.w * 100, h: crop.h * 100 } : null,
        source_start_sec: Number.isFinite(Number(source?.source_start_sec)) ? Number(source.source_start_sec) : 0,
        source_end_sec: source?.source_end_sec == null ? "" : Number(source.source_end_sec),
    };
}

export function replaceMemberDraftAsset(draft, asset) {
    const priorType = String(draft?.asset_type || "");
    const nextType = String(asset?.asset_type || "");
    const next = {
        ...draft,
        crop: draft?.crop ? { ...draft.crop } : null,
        tags: Array.isArray(draft?.tags) ? [...draft.tags] : [],
        asset_id: asset?.asset_id || "",
        asset_type: nextType,
        name: draft?.name || (!draft?.member_id
            ? String(asset?.name || "").replace(/\.[^.]+$/, "").trim()
            : ""),
    };
    const notices = [];
    const priorVisual = priorType === "image" || priorType === "video";
    const nextVisual = nextType === "image" || nextType === "video";
    if (next.crop != null && !(priorVisual && nextVisual)) {
        next.crop = null;
        notices.push("Crop was reset because the replacement is not visual media.");
    }
    if (nextType === "image" && (Number(next.source_start_sec) !== 0 || next.source_end_sec !== "")) {
        next.source_start_sec = 0;
        next.source_end_sec = "";
        notices.push("Source trim was reset because still images use the full source.");
    }
    return { draft: next, notices };
}

export function validateReferenceDraft(draft) {
    const errors = [];
    if (!text(draft?.name)) errors.push("Name is required.");
    if (!VALID_KINDS.has(draft?.kind)) errors.push("Choose a valid kind.");
    if (!VALID_CLASSES.has(draft?.reference_class)) errors.push("Choose a valid reference class.");
    return errors;
}

export function validateMemberDraft(draft, catalog = [], asset = null, families = {}) {
    const errors = [];
    if (!text(draft?.asset_id)) errors.push("Choose an image or audio asset.");
    const media = draft?.asset_type;
    if (media && !["image", "audio", "video"].includes(media)) errors.push("References accept image, audio, and video assets only.");
    const normalized = normalizeReferenceTags(draft?.tags, catalog, { strict: true });
    errors.push(...normalized.errors);
    const presets = catalogById(catalog);
    for (const tag of normalized.tags) {
        const preset = presets.get(tag);
        const selectedAsset = asset || (media ? { asset_type: media, has_audio: draft?.has_audio === true } : null);
        if (preset && selectedAsset && !referencePresetAcceptsAsset(preset, selectedAsset)) {
            errors.push(`${formatReferenceTag(tag, { catalog, families })} does not accept ${media} assets.`);
        }
    }
    const start = Number(draft?.source_start_sec);
    const end = draft?.source_end_sec === "" || draft?.source_end_sec == null ? null : Number(draft.source_end_sec);
    if (!Number.isFinite(start) || start < 0) errors.push("Source start must be a finite non-negative number.");
    if (end != null && (!Number.isFinite(end) || end <= start)) errors.push("Source end must be greater than source start.");
    if (draft?.crop != null) {
        if (media && !["image", "video"].includes(media)) errors.push("Only image and video members can be cropped.");
        const c = draft.crop;
        const values = [c.x, c.y, c.w, c.h].map(Number);
        if (values.some((value) => !Number.isFinite(value)) || values[0] < 0 || values[1] < 0
            || values[2] <= 0 || values[3] <= 0 || values[0] + values[2] > 100 || values[1] + values[3] > 100) {
            errors.push("Crop must have positive dimensions inside the source.");
        }
    }
    return errors;
}

export function serializeReferenceDraft(draft) {
    return {
        name: text(draft.name),
        kind: draft.kind,
        reference_class: draft.reference_class,
        description: String(draft.description ?? "").trim(),
        visual_intent: draft.visual_intent,
        audio_intent: draft.audio_intent,
    };
}

export function serializeMemberDraft(draft, catalog = []) {
    const tags = normalizeReferenceTags(draft.tags, catalog, { strict: true }).tags;
    const end = draft.source_end_sec === "" || draft.source_end_sec == null ? null : Number(draft.source_end_sec);
    return {
        asset_id: text(draft.asset_id),
        name: text(draft.name),
        tags,
        prompt: String(draft.prompt ?? "").trim(),
        crop: draft.crop == null ? null : {
            x: Number(draft.crop.x) / 100,
            y: Number(draft.crop.y) / 100,
            w: Number(draft.crop.w) / 100,
            h: Number(draft.crop.h) / 100,
        },
        source_start_sec: Number(draft.source_start_sec),
        source_end_sec: end,
    };
}

export function denseMemberOrder(members = []) {
    return (Array.isArray(members) ? members : []).map((member, order) => ({ ...member, order }));
}

export function moveMember(members, memberId, direction) {
    const result = denseMemberOrder(members);
    const from = result.findIndex((member) => member.member_id === memberId);
    if (from < 0) return result;
    const to = Math.max(0, Math.min(result.length - 1, from + (direction < 0 ? -1 : 1)));
    if (to === from) return result;
    [result[from], result[to]] = [result[to], result[from]];
    return denseMemberOrder(result);
}

export function filterReferences(references = [], query = "", assets = [], catalog = [], families = {}) {
    const needle = text(query).toLocaleLowerCase();
    if (!needle) return Array.isArray(references) ? references : [];
    const names = new Map((Array.isArray(assets) ? assets : []).map((asset) => [asset.asset_id, asset.name || asset.path || ""]));
    return (Array.isArray(references) ? references : []).filter((reference) => {
        const values = [reference.name, reference.kind, reference.description];
        for (const member of reference.members || []) {
            for (const tag of member.tags || []) {
                values.push(referenceTagSearchText(tag, { catalog, families }));
            }
            values.push(names.get(member.asset_id) || "");
        }
        return values.some((value) => String(value || "").toLocaleLowerCase().includes(needle));
    });
}

export function shouldApplyReferenceResponse({ requestedProject, currentProject, requestGeneration, currentGeneration }) {
    return !!requestedProject
        && requestedProject === currentProject
        && requestGeneration === currentGeneration;
}
