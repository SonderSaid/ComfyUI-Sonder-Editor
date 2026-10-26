// @server-mirror server/take_placement.py::calculate_take_placement
// @server-mirror server/take_placement.py::take_restoration_source
// @server-mirror server/take_placement.py::restore_take_geometry
// @server-mirror server/routes.py::_take_audio_covers
// Scope and parity disposition: tests/test_mutation_authoring_contract.py::MIRRORED_MODULES
// Pure mirror of take restoration: what a dropped take's record resolves to and
// the geometry it restores. The drop paints and checks collisions with it; the
// server recomputes the same values and refuses any difference. Python's int()
// and float() coercions are reproduced because the record is free-form JSON.

export const TAKE_PLACEMENT_POLICY = "authored_v1";

// Why a take drops at the cursor instead. The server's refusal messages are its
// own; these explain a fallback chosen before anything is painted.
export const TAKE_RESTORATION_REASONS = Object.freeze({
    legacy: "This take was saved before Sonder recorded take placement",
    unsupported_policy: "This take's placement was recorded by a newer Sonder",
    missing_fps: "This take's placement frame rate is missing",
    incomplete: "This take's placement record is incomplete",
    empty: "This take has no frames in its placed range",
    collapsed: "This take's range has no frames left at this scene's frame rate",
    before_scene_start: "This take would start before the scene",
    replaced: "This asset's media was replaced after the take was saved",
    missing: "This asset's media file is missing",
    trashed: "This asset is in the trash",
    audio_short: "This audio is shorter than the take's placed range",
});

function isPlainObject(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
}

// Python truthiness: empty containers are false too.
function pyTruthy(value) {
    if (Array.isArray(value)) return value.length > 0;
    if (isPlainObject(value)) return Object.keys(value).length > 0;
    return !!value;
}

const PY_INT_TEXT = /^\s*[+-]?\d+(?:_\d+)*\s*$/;
const PY_FLOAT_TEXT = /^\s*[+-]?(?:\d+(?:_\d+)*(?:\.(?:\d+(?:_\d+)*)?)?|\.\d+(?:_\d+)*)(?:[eE][+-]?\d+(?:_\d+)*)?\s*$/;

// `int(value)`: the whole number, or null where Python raises.
function pyInt(value) {
    if (typeof value === "boolean") return value ? 1 : 0;
    if (typeof value === "number") return Number.isFinite(value) ? Math.trunc(value) : null;
    if (typeof value === "string") {
        return PY_INT_TEXT.test(value) ? Number.parseInt(value.replaceAll("_", "").trim(), 10) : null;
    }
    return null;
}

// `float(value)`: the number, or null where Python raises. Non-finite values
// are returned so the callers' own finiteness checks decide, as in Python.
function pyFloat(value) {
    if (typeof value === "boolean") return value ? 1 : 0;
    if (typeof value === "number") return value;
    if (typeof value === "string") {
        const text = value.trim().toLowerCase();
        if (/^[+-]?(?:inf|infinity)$/.test(text)) return text.startsWith("-") ? -Infinity : Infinity;
        if (/^[+-]?nan$/.test(text)) return Number.NaN;
        return PY_FLOAT_TEXT.test(value) ? Number(value.replaceAll("_", "").trim()) : null;
    }
    return null;
}

function pyStr(value) {
    if (typeof value === "boolean") return value ? "True" : "False";
    return String(value);
}

function halfUp(value) {
    return Math.floor(Number(value) + 0.5);
}

// math.isclose(a, b, rel_tol=1e-9, abs_tol=0)
function isClose(a, b) {
    return Math.abs(a - b) <= 1e-9 * Math.max(Math.abs(a), Math.abs(b));
}

export function calculateTakePlacement(context, encodedFrameCount) {
    const ctx = isPlainObject(context) ? context : {};
    const integer = (key, fallback = 0) => {
        const raw = Object.hasOwn(ctx, key) ? ctx[key] : fallback;
        if (!pyTruthy(raw)) return 0;
        const parsed = pyInt(raw);
        return parsed === null ? fallback : parsed;
    };
    const count = Math.max(0, Math.trunc(Number(encodedFrameCount) || 0));
    const padding = Math.max(0, integer("frame_count_padding"));
    const keepPadding = ctx.take_keep_padding === true && padding > 0
        && integer("mask_end_frame", -1) >= integer("frame_count", count);
    const available = keepPadding ? count : Math.max(0, count - padding);
    const start = integer("selection_start");
    const end = integer("selection_end", start + count);
    const pre = Math.max(0, integer("actual_pre_context_frames",
        integer("pre_context_frames", integer("context_frames"))));
    const post = Math.max(0, integer("actual_post_context_frames",
        integer("post_context_frames", integer("context_frames"))));
    const before = Math.min(pre, Math.max(0, integer("requested_mask_pre_offset", integer("mask_pre_offset"))));
    const after = Math.min(post, Math.max(0, integer("requested_mask_post_offset", integer("mask_post_offset"))));
    let sourceIn;
    let sourceOut;
    let timelineStart;
    if (ctx.take_placement_mode === "untrimmed") {
        sourceIn = 0;
        sourceOut = available;
        timelineStart = start - pre;
    } else {
        sourceIn = Math.min(available, pre - before);
        sourceOut = keepPadding ? available : Math.max(
            sourceIn, Math.min(available, pre + Math.max(0, end - start) + after));
        timelineStart = start - before;
    }
    return {
        source_in_frame: sourceIn,
        source_out_frame: sourceOut,
        total_source_frames: available,
        source_origin_frame: 0,
        timeline_start_frame: timelineStart,
        timeline_end_frame: timelineStart + sourceOut - sourceIn,
    };
}

function finitePositive(value) {
    const number = pyFloat(value);
    return number !== null && Number.isFinite(number) && number > 0 ? number : 0;
}

function whole(value) {
    if (typeof value === "boolean") return null;
    const number = pyFloat(value);
    return number !== null && Number.isFinite(number) && number === Math.trunc(number) ? number : null;
}

export function takeRestorationSource(params, assetType) {
    if ((assetType !== "video" && assetType !== "audio") || !isPlainObject(params)) {
        return { status: "not_take" };
    }
    const policy = params.take_placement_policy;
    const exported = params.editor_export;
    const exportRange = isPlainObject(exported) ? exported.range : undefined;
    const savedTake = Object.hasOwn(params, "selection_start") && pyTruthy(params.scene_id);
    if (policy === undefined || policy === null) {
        return savedTake ? { status: "legacy", reason: "legacy" } : { status: "not_take" };
    }
    if (policy !== TAKE_PLACEMENT_POLICY) return { status: "unavailable", reason: "unsupported_policy" };
    const placementFps = finitePositive(params.placement_fps);
    if (!placementFps) return { status: "unavailable", reason: "missing_fps" };
    let sceneId;
    let start;
    let end;
    let context;
    if (savedTake) {
        sceneId = pyTruthy(params.scene_id) ? pyStr(params.scene_id) : "";
        start = whole(params.selection_start);
        end = whole(params.selection_end);
        context = params;
    } else if (isPlainObject(exportRange)) {
        sceneId = pyTruthy(exported.scene_id) ? pyStr(exported.scene_id) : "";
        start = whole(exportRange.start);
        end = whole(exportRange.end);
        context = { selection_start: start, selection_end: end };
    } else {
        return { status: "unavailable", reason: "incomplete" };
    }
    if (!sceneId || start === null || end === null || end <= start) {
        return { status: "unavailable", reason: "incomplete" };
    }
    const source = { status: "supported", scene_id: sceneId, placement_fps: placementFps, context };
    if (assetType === "audio") {
        const encoded = whole(params.encoded_frame_count);
        if (encoded === null || encoded <= 0) return { status: "unavailable", reason: "incomplete" };
        source.encoded_frame_count = encoded;
    }
    return source;
}

export function restoreTakeGeometry(source, encodedFrameCount, { sameScene, destinationFps, cursorFrame }) {
    const geometry = calculateTakePlacement(source.context, encodedFrameCount);
    if (geometry.timeline_end_frame <= geometry.timeline_start_frame || geometry.total_source_frames <= 0) {
        return { reason: "empty" };
    }
    const fps = finitePositive(destinationFps);
    if (!fps) return { reason: "missing_fps" };
    const ratio = fps / source.placement_fps;
    const sameRate = isClose(ratio, 1.0);
    const convert = (frame) => (sameRate ? frame : halfUp(frame * ratio));
    const sourceIn = convert(geometry.source_in_frame);
    let sourceOut = convert(geometry.source_out_frame);
    const available = convert(geometry.total_source_frames);
    if (sourceIn >= available) return { reason: "collapsed" };
    if (sourceOut - sourceIn < 1) sourceOut = sourceIn + 1;
    const start = sameScene ? convert(geometry.timeline_start_frame) : Math.trunc(Number(cursorFrame) || 0);
    if (start < 0) return { reason: "before_scene_start" };
    return {
        source_in_frame: sourceIn,
        source_out_frame: sourceOut,
        total_source_frames: available,
        source_origin_frame: 0,
        timeline_start_frame: start,
        timeline_end_frame: start + sourceOut - sourceIn,
    };
}

// Whether an audio asset reaches the restored source end, within one frame;
// its length in scene frames is `media_timeline_frames` for audio.
export function takeAudioCovers(durationSec, fps, geometry) {
    const seconds = Number(durationSec);
    const rate = Number(fps) > 0 && Number.isFinite(Number(fps)) ? Number(fps) : 24;
    const frames = Number.isFinite(seconds) && seconds > 0 ? Math.max(0, halfUp(seconds * rate)) : 0;
    return frames >= geometry.source_out_frame - 1;
}
