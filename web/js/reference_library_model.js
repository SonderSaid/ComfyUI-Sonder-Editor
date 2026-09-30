// @server-mirror server/timeline_state.py::default_reference_class
// @server-mirror server/timeline_state.py::normalize_reference_tags
// @server-mirror server/routes.py::_validated_reference_tags
// @server-mirror server/routes.py::_apply_create_reference
// @server-mirror server/routes.py::_member_from_fields
// @server-mirror server/routes.py::_CLIENT_ID_PATTERN
// @server-mirror server/routes.py::_CLIENT_RECIPE_ID_PATTERN
// @server-mirror server/routes.py::_normalize_custom_reference_recipe
// @server-mirror server/routes.py::_require_prompt_handle_available
// Scope and parity disposition: tests/test_mutation_authoring_contract.py::MIRRORED_MODULES
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

/** A durable id for a new Reference or Library member, minted by the client
 *  so the row it paints carries the id the server will store (32 hex, the
 *  shape `_new_reference_id` mints and `_CLIENT_ID_PATTERN` accepts).
 *
 *  `crypto.getRandomValues` rather than `randomUUID`, which a non-secure
 *  origin (a LAN address over http) does not provide. Uniqueness is not
 *  attempted: the route refuses a taken id with 409 `id_conflict` rather than
 *  re-minting it, and at 128 bits that refusal is vanishingly rare. */
export function mintReferenceLibraryId() {
    const bytes = new Uint8Array(16);
    if (typeof globalThis.crypto?.getRandomValues === "function") {
        globalThis.crypto.getRandomValues(bytes);
    } else {
        for (let index = 0; index < bytes.length; index += 1) {
            bytes[index] = Math.floor(Math.random() * 256) & 0xff;
        }
    }
    return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** A durable id for a new custom recipe: the `custom:` namespace the route
 *  mints into, then a library id (`_CLIENT_RECIPE_ID_PATTERN`). */
export function mintReferenceRecipeId() {
    return `custom:${mintReferenceLibraryId()}`;
}

/** The custom recipe `_normalize_custom_reference_recipe` stores, for fields
 *  the route accepts. `schema` is the served `recipe_field_schema`: an `int`
 *  field is truncated and a `number` field made a number, as the route
 *  coerces them; other values are copied. A recipe write's row is painted as
 *  this record, so an Update or Delete guarded by the displayed definition
 *  compares equal once the write has landed. */
export function referenceRecipeRecord(fields = {}, recipeId = "", schema = []) {
    const source = fields && typeof fields === "object" ? fields : {};
    const kinds = new Map((Array.isArray(schema) ? schema : [])
        .map((field) => [`${field?.section}:${field?.key}`, field?.type]));
    const section = (name) => {
        const values = source[name] && typeof source[name] === "object" ? source[name] : {};
        return Object.fromEntries(Object.entries(values).map(([key, value]) => {
            const kind = kinds.get(`${name}:${key}`);
            if (kind === "int" && typeof value === "number") return [key, Math.trunc(value)];
            if (kind === "number" && typeof value === "number") return [key, Number(value)];
            return [key, structuredClone(value)];
        }));
    };
    return {
        id: String(recipeId || source.id || "").trim(),
        name: String(source.name || "").trim(),
        builtIn: false,
        media_kind: String(source.media_kind || "image"),
        hard: section("hard"),
        soft: section("soft"),
    };
}

/** Custom recipes as displayed: acknowledged recipes plus pending recipe
 *  overlays, in authoring order. Pure, like `applyPendingReferenceOverlays`,
 *  and read by the same surfaces through the host's `_referenceRecipesView`.
 *  An update merges its fields into the displayed definition, as
 *  `update_recipe` merges them into the stored one; a delete removes it; a
 *  create whose id is already shown is not painted twice. */
export function applyPendingRecipeOverlays(recipes = [], overlays = [], schema = []) {
    let result = Array.isArray(recipes) ? [...recipes] : [];
    for (const overlay of Array.isArray(overlays) ? overlays : []) {
        if (overlay?.type === "create_recipe") {
            if (!overlay.createdId || result.some((recipe) => String(recipe?.id || "") === overlay.createdId)) continue;
            result.push({ ...referenceRecipeRecord(overlay.fields, overlay.createdId, schema),
                pendingStatus: overlay.status || "saving" });
        } else if (overlay?.type === "update_recipe") {
            const index = result.findIndex((recipe) => String(recipe?.id || "") === overlay.recipe_id);
            if (index < 0) continue;
            const { builtIn: _builtIn, pendingStatus: _status, ...current } = result[index];
            result = [...result];
            result[index] = { ...referenceRecipeRecord({ ...current, ...overlay.fields }, overlay.recipe_id, schema),
                pendingStatus: overlay.status || "saving" };
        } else if (overlay?.type === "delete_recipe") {
            result = result.filter((recipe) => String(recipe?.id || "") !== overlay.recipe_id);
        }
    }
    return result;
}

/** `referenceOverlayReflected` for a recipe overlay, against acknowledged
 *  recipes: a create by its id, a delete once its recipe is gone, an update
 *  never (it waits for a payload that postdates it). */
export function recipeOverlayReflected(recipes = [], overlay = null) {
    const ids = (Array.isArray(recipes) ? recipes : []).map((recipe) => String(recipe?.id || ""));
    if (overlay?.type === "create_recipe") {
        const id = String(overlay.committedId || overlay.createdId || "");
        return !!id && ids.includes(id);
    }
    if (overlay?.type === "delete_recipe") return !ids.includes(overlay.recipe_id);
    return false;
}

/** Who already owns a prompt handle, as `_require_prompt_handle_available`
 *  decides it: Library member handles and prompt identity handles share one
 *  namespace, compared case-insensitively (handles are ASCII, so lower-casing
 *  is the route's `casefold`). Returns `{ kind, id }` for an owner other than
 *  `ownerKind`/`ownerId`, or null when the handle is free. */
export function promptHandleOwner(handle, {
    references = [], semanticUnits = [], ownerKind = "", ownerId = "",
} = {}) {
    const wanted = String(handle ?? "").trim();
    if (!wanted) return null;
    const folded = wanted.toLowerCase();
    const owners = [];
    for (const reference of Array.isArray(references) ? references : []) {
        for (const member of reference?.members || []) {
            owners.push([String(member?.handle ?? "").trim(), "physical reference", String(member?.member_id || "")]);
        }
    }
    for (const unit of Array.isArray(semanticUnits) ? semanticUnits : []) {
        if (!unit || typeof unit !== "object") continue;
        owners.push([String(unit.handle ?? "").trim(), "prompt identity", String(unit.semantic_unit_id || "")]);
    }
    for (const [current, kind, id] of owners) {
        if (!current || current.toLowerCase() !== folded) continue;
        if (kind === ownerKind && id === ownerId) continue;
        return { kind, id };
    }
    return null;
}

/** The Reference `_apply_create_reference` stores, as `to_dict` serves it,
 *  for fields the route accepts. A create's row is painted as this record, so
 *  a guard later read from that row (an Edit's base, a Delete's snapshot) is
 *  what the server holds once the create lands. */
export function referenceCreateRecord(fields = {}, referenceId = "") {
    const source = fields && typeof fields === "object" ? fields : {};
    const kind = source.kind ?? "character";
    return {
        reference_id: String(referenceId ?? ""),
        name: String(source.name || "").trim(),
        kind,
        reference_class: source.reference_class ?? defaultReferenceClass(kind),
        description: String(source.description || ""),
        visual_intent: source.visual_intent ?? "preserve",
        audio_intent: source.audio_intent ?? "reference_characteristics",
        members: [],
    };
}

/** The member `_member_from_fields` builds, as `ReferenceMember.to_dict`
 *  serves it, for fields the route accepts. `delete_member` requires every
 *  `to_dict` key in its guard, so a Remove of a member still saving sends this
 *  whole record. Tags keep the route's whitespace and duplicate
 *  normalization; a preset tag is expected in its canonical spelling, as the
 *  member form sends it (the route would re-case a preset sent otherwise, and
 *  a Remove guarded by the painted spelling would then be refused, loudly). */
export function referenceMemberCreateRecord(fields = {}, memberId = "", order = 0) {
    const source = fields && typeof fields === "object" ? fields : {};
    const tags = normalizeReferenceTags(source.tags).tags;
    const crop = source.crop == null ? null
        : Object.fromEntries(["x", "y", "w", "h"].map((key) => [key, Number(source.crop[key])]));
    const end = source.source_end_sec == null || source.source_end_sec === ""
        ? null : Number(source.source_end_sec);
    const capabilities = [];
    for (const value of Array.isArray(source.disabled_capabilities) ? source.disabled_capabilities : []) {
        const id = String(value || "").trim();
        if (id && !capabilities.includes(id)) capabilities.push(id);
    }
    return {
        member_id: String(memberId ?? ""),
        asset_id: String(source.asset_id || ""),
        name: String(source.name || "").trim(),
        handle: String(source.handle || "").trim(),
        visual_intent: source.visual_intent ? String(source.visual_intent) : "",
        audio_intent: source.audio_intent ? String(source.audio_intent) : "",
        attachment_defaults: source.attachment_defaults && typeof source.attachment_defaults === "object"
            ? structuredClone(source.attachment_defaults) : {},
        disabled_capabilities: capabilities,
        tags,
        prompt: String(source.prompt || ""),
        crop,
        order: Number(order) || 0,
        source_start_sec: Number(source.source_start_sec ?? 0),
        source_end_sec: end,
    };
}

/** Library writes that paint before the server answers: every Library
 *  sidebar write. Each paint is an overlay its own mutation owns, so a refusal
 *  drops it with no network. The guards stay exact-prior-value: a form's
 *  `expected` comes from the record it was opened from, and a button with no
 *  form reads the displayed row at the click. */
export const PAINT_FIRST_REFERENCE_OPERATIONS = Object.freeze(new Set([
    "create_reference", "create_member", "reorder_members",
    "update_reference", "update_member", "delete_reference", "delete_member",
    "create_recipe", "update_recipe", "delete_recipe"]));

/** The custom-recipe writes among them, painted into the recipe view
 *  (`applyPendingRecipeOverlays`) rather than the References. */
export const RECIPE_OVERLAY_TYPES = Object.freeze(new Set([
    "create_recipe", "update_recipe", "delete_recipe"]));

/** Describe one paint-first operation as a display overlay, or null.
 *
 *  A create carries the id the client minted for it (`createdId`, the
 *  operation's own `reference_id` or `member_id`), and its row is painted
 *  under that id, usable at once: every later write naming it is queued behind
 *  the create. Against a server that does not adopt client ids nothing is
 *  minted, and `key` doubles as the row's temporary id (`pending:<n>`), which
 *  no request may name -- that row alone is inert (`pendingInert`). An update
 *  or delete names a real id, so the row it paints stays usable while it saves.
 */
export function referenceOverlayFromOperation(operation, key) {
    const type = String(operation?.type || "");
    if (!PAINT_FIRST_REFERENCE_OPERATIONS.has(type)) return null;
    const createdId = type === "create_reference" ? String(operation?.reference_id || "")
        : (type === "create_member" ? String(operation?.member_id || "")
            : (type === "create_recipe" ? String(operation?.fields?.id || "") : ""));
    return {
        key: String(key),
        type,
        createdId,
        reference_id: String(operation?.reference_id || ""),
        member_id: String(operation?.member_id || ""),
        recipe_id: String(operation?.recipe_id || ""),
        fields: operation?.fields && typeof operation.fields === "object"
            ? structuredClone(operation.fields) : {},
        expected: operation?.expected && typeof operation.expected === "object"
            ? structuredClone(operation.expected) : {},
        member_ids: Array.isArray(operation?.member_ids)
            ? operation.member_ids.map(String) : [],
        status: "saving",
        committedId: "",
    };
}

/** The Library as displayed: acknowledged References plus pending overlays.
 *
 *  Pure. Nothing is written back into `references`, which stay the server's
 *  payload and the only authority; rolling a failed write back is dropping its
 *  overlay. The host's effective view (`_referencesView`) is this, and it is
 *  what surfaces that display or offer Library data read. Paths that describe
 *  server truth read the acknowledged data instead.
 *
 *  Overlays apply in authoring order, which is the queue's send order, so the
 *  displayed order is what the server will hold once they all land. Painted
 *  rows carry `pendingStatus` for their "Saving…" label. A create's row also
 *  carries `pendingCreate` -- a chip picker skips it, because a chip names a
 *  member without the route checking it exists -- and `pendingInert` when it
 *  has no minted id. A create whose id acknowledged data already shows is not
 *  painted twice. Untouched References keep their identity.
 */
export function applyPendingReferenceOverlays(references = [], overlays = []) {
    const result = Array.isArray(references) ? [...references] : [];
    for (const overlay of Array.isArray(overlays) ? overlays : []) {
        if (RECIPE_OVERLAY_TYPES.has(overlay?.type)) continue;
        if (overlay?.type === "create_reference") {
            if (overlay.createdId && result.some((reference) =>
                String(reference?.reference_id || "") === overlay.createdId)) continue;
            result.push({
                ...referenceCreateRecord(overlay.fields, overlay.createdId || overlay.key),
                pendingStatus: overlay.status || "saving",
                pendingCreate: true,
                ...(overlay.createdId ? {} : { pendingInert: true }),
            });
            continue;
        }
        const index = result.findIndex((reference) =>
            String(reference?.reference_id || "") === overlay?.reference_id);
        // The target is gone from acknowledged data (deleted elsewhere): the
        // write will be refused, so there is nothing truthful to paint.
        if (index < 0) continue;
        const reference = result[index];
        const members = Array.isArray(reference.members) ? reference.members : [];
        if (overlay.type === "create_member") {
            if (overlay.createdId && members.some((member) =>
                String(member?.member_id || "") === overlay.createdId)) continue;
            // `order = len(members)`, as `_apply_create_reference_member` assigns.
            result[index] = {
                ...reference,
                members: [...members, {
                    ...referenceMemberCreateRecord(overlay.fields,
                        overlay.createdId || overlay.key, members.length),
                    pendingStatus: overlay.status || "saving",
                    pendingCreate: true,
                    ...(overlay.createdId ? {} : { pendingInert: true }),
                }],
            };
        } else if (overlay.type === "update_reference") {
            result[index] = { ...reference, ...overlay.fields,
                pendingStatus: overlay.status || "saving" };
        } else if (overlay.type === "delete_reference") {
            result.splice(index, 1);
        } else if (overlay.type === "update_member") {
            if (!members.some((member) => String(member?.member_id || "") === overlay.member_id)) continue;
            result[index] = { ...reference, members: members.map((member) =>
                String(member?.member_id || "") === overlay.member_id
                    ? { ...member, ...overlay.fields, pendingStatus: overlay.status || "saving" }
                    : member) };
        } else if (overlay.type === "delete_member") {
            // Dense `order`, as `_apply_delete_reference_member` renumbers.
            const kept = members.filter((member) =>
                String(member?.member_id || "") !== overlay.member_id);
            if (kept.length !== members.length) {
                result[index] = { ...reference, members: denseMemberOrder(kept) };
            }
        } else if (overlay.type === "reorder_members") {
            const byId = new Map(members.map((member) => [String(member?.member_id || ""), member]));
            const desired = overlay.member_ids.filter((id) => byId.has(id));
            const placed = new Set(desired);
            const ordered = [
                ...desired.map((id) => byId.get(id)),
                ...members.filter((member) => !placed.has(String(member?.member_id || ""))),
            ];
            // Dense `order`, as the route rewrites it: a Remove authored against
            // this order is checked by the server after the reorder has landed.
            result[index] = { ...reference, members: denseMemberOrder(ordered) };
        }
    }
    return result;
}

/** True once acknowledged References already show what this overlay painted.
 *
 *  A create counts by its id: the one the server returned, else the one the
 *  client minted. A delete counts once its target is gone. An update or a
 *  reorder never qualifies: a value or an order can match by coincidence — Up
 *  then Down restores an order, and a later edit can restore a value — so a
 *  later write would leave ahead of an earlier one and the display would show
 *  what the server does not hold. Each waits for a payload that postdates it.
 */
export function referenceOverlayReflected(references = [], overlay = null) {
    const list = Array.isArray(references) ? references : [];
    const createdId = String(overlay?.committedId || overlay?.createdId || "");
    if (overlay?.type === "create_reference") {
        return !!createdId && list.some((reference) =>
            String(reference?.reference_id || "") === createdId);
    }
    const reference = list.find((entry) =>
        String(entry?.reference_id || "") === overlay?.reference_id);
    if (overlay?.type === "delete_reference") return !reference;
    const memberIds = (reference?.members || []).map((member) => String(member?.member_id || ""));
    if (overlay?.type === "delete_member") return !memberIds.includes(overlay.member_id);
    if (overlay?.type === "create_member") {
        return !!createdId && memberIds.includes(createdId);
    }
    return false;
}

/** Whether a Library value read back equals the value an update sent, as the
 *  route stores it: names and text as sent (the draft serializers trim as the
 *  route does), tags without regard to case (the route casefolds preset ids),
 *  and numbers as numbers (the route stores floats). Used to decide whether an
 *  update whose answer was lost landed; a false "no" costs a returned draft,
 *  never data. */
export function referenceFieldStoredAs(field, stored, sent) {
    if (field === "tags") {
        const fold = (value) => (Array.isArray(value) ? value : []).map((tag) =>
            String(tag).toLocaleLowerCase());
        return JSON.stringify(fold(stored)) === JSON.stringify(fold(sent));
    }
    if (field === "crop") {
        if (stored == null || sent == null) return stored == null && sent == null;
        return ["x", "y", "w", "h"].every((key) =>
            Math.abs(Number(stored[key]) - Number(sent[key])) < 1e-9);
    }
    if (field === "source_start_sec" || field === "source_end_sec") {
        if (stored == null || sent == null || sent === "") {
            return (stored == null) === (sent == null || sent === "");
        }
        return Math.abs(Number(stored) - Number(sent)) < 1e-9;
    }
    return JSON.stringify(stored ?? null) === JSON.stringify(sent ?? null);
}

export function shouldApplyReferenceResponse({ requestedProject, currentProject, requestGeneration, currentGeneration }) {
    return !!requestedProject
        && requestedProject === currentProject
        && requestGeneration === currentGeneration;
}
