// Pure frontend mirror of server/guide_collision.py execution-window math.
// Selection endpoints are scene coordinates; model frame constraints apply to
// the resolved tensor length, never to either absolute endpoint.

function asInt(value, fallback = 0) {
    const parsed = Number.parseInt(value, 10);
    return Number.isFinite(parsed) ? parsed : fallback;
}

function positiveMod(value, divisor) {
    if (!(divisor > 0)) return 0;
    return ((value % divisor) + divisor) % divisor;
}

function resolveConstraint(frameConstraint) {
    if (!frameConstraint || typeof frameConstraint !== "object") return null;
    const step = Math.max(1, asInt(frameConstraint.step, 1));
    if (step <= 1) return null;
    return { step, offset: asInt(frameConstraint.offset, 0) };
}

function hasFrameCountConstraint(frameConstraint) {
    return !!(
        frameConstraint
        && typeof frameConstraint === "object"
        && Object.prototype.hasOwnProperty.call(frameConstraint, "step")
    );
}

function snapPixel(pixel, frameConstraint, side) {
    const resolved = resolveConstraint(frameConstraint);
    if (!resolved) return pixel;
    const { step, offset } = resolved;
    if (pixel <= 0) return 0;
    if (pixel < offset) return side === "start" ? 0 : offset;
    const ratio = (pixel - offset) / step;
    return offset + (side === "start" ? Math.floor(ratio) : Math.ceil(ratio)) * step;
}

function snapMaskPre(value, actualPre, step) {
    value = Math.max(0, Math.min(asInt(value), actualPre));
    if (value >= actualPre) return actualPre;
    return Math.min(actualPre, Math.ceil(value / step) * step);
}

function snapMaskPost(value, actualPost, step) {
    value = Math.max(0, Math.min(asInt(value), actualPost));
    return Math.min(actualPost, Math.ceil(value / step) * step);
}

export function roundUpSelectionFrameCount(frameCount, frameConstraint) {
    let count = Math.max(0, asInt(frameCount));
    if (count <= 0 || !frameConstraint || typeof frameConstraint !== "object" || !("step" in frameConstraint)) {
        return count;
    }
    const step = Math.max(1, asInt(frameConstraint.step, 1));
    const offset = asInt(frameConstraint.offset, 0);
    const minimum = Math.max(1, asInt(frameConstraint.min, 1));
    count = Math.max(count, minimum);
    if (positiveMod(count - offset, step) === 0) return count;
    return offset + Math.ceil((count - offset) / step) * step;
}

export function isSelectionDurationWithinRecommendation({
    frameCount = 0,
    fps = 0,
    minSec = 0,
    maxSec = 0,
    frameConstraint = null,
} = {}) {
    const frames = Math.max(0, asInt(frameCount));
    const rate = Number(fps) || 0;
    const minimum = Math.max(0, Number(minSec) || 0);
    const maximum = Math.max(minimum, Number(maxSec) || 0);
    if (frames <= 0 || rate <= 0) return false;

    // A manual endpoint at the exact recommended maximum may move to the next
    // valid grid length. Tolerating one grid interval minus one frame hides only
    // that nearest constrained result; the following valid length still warns.
    const step = Math.max(1, asInt(frameConstraint?.step, 1));
    const upperToleranceFrames = Math.max(1, step - 1);
    const seconds = frames / rate;
    const epsilon = 1e-6;
    return seconds >= minimum - epsilon
        && seconds <= maximum + (upperToleranceFrames / rate) + epsilon;
}

export function resolveSelectionExecutionWindow({
    sceneDuration = 0,
    selectionStart = 0,
    selectionEnd = 0,
    preContextFrames = 0,
    postContextFrames = 0,
    maskPreOffset = 0,
    maskPostOffset = 0,
    frameConstraint = null,
} = {}) {
    const duration = Math.max(0, asInt(sceneDuration));
    const generationStart = Math.max(0, Math.min(duration, asInt(selectionStart)));
    const generationEnd = Math.max(generationStart, Math.min(duration, asInt(selectionEnd)));
    let actualPre = Math.min(Math.max(0, asInt(preContextFrames)), generationStart);
    let actualPost = Math.min(Math.max(0, asInt(postContextFrames)), duration - generationEnd);
    let resolvedMaskPre = asInt(maskPreOffset);
    let resolvedMaskPost = asInt(maskPostOffset);

    const resolved = resolveConstraint(frameConstraint);
    if (resolved) {
        const { step } = resolved;
        if (actualPre > 0) {
            const alignedPre = snapPixel(actualPre, frameConstraint, "end");
            const extension = Math.max(0, alignedPre - actualPre);
            if (extension <= generationStart - actualPre) actualPre += extension;
        }
        const postRemainder = actualPost % step;
        const postExtension = (step - postRemainder) % step;
        if (postExtension <= duration - generationEnd - actualPost) actualPost += postExtension;
        resolvedMaskPre = snapMaskPre(resolvedMaskPre, actualPre, step);
        resolvedMaskPost = snapMaskPost(resolvedMaskPost, actualPost, step);
    } else {
        resolvedMaskPre = Math.max(0, Math.min(resolvedMaskPre, actualPre));
        resolvedMaskPost = Math.max(0, Math.min(resolvedMaskPost, actualPost));
    }

    const renderStart = generationStart - actualPre;
    const renderEnd = generationEnd + actualPost;
    const sourceFrameCount = Math.max(0, renderEnd - renderStart);
    const frameCount = roundUpSelectionFrameCount(sourceFrameCount, frameConstraint);
    return {
        generation_start: generationStart,
        generation_end: generationEnd,
        render_start: renderStart,
        render_end: renderEnd,
        actual_pre: actualPre,
        actual_post: actualPost,
        mask_pre_offset: resolvedMaskPre,
        mask_post_offset: resolvedMaskPost,
        source_frame_count: sourceFrameCount,
        frame_count: frameCount,
        frame_count_padding: Math.max(0, frameCount - sourceFrameCount),
    };
}

function windowForEndpoint(options, endpoint) {
    const edge = options.edge === "start" ? "start" : "end";
    const anchor = asInt(options.anchorFrame);
    const start = edge === "start" ? endpoint : anchor;
    const end = edge === "end" ? endpoint : anchor;
    return resolveSelectionExecutionWindow({
        sceneDuration: options.sceneDuration,
        selectionStart: start,
        selectionEnd: end,
        preContextFrames: options.preContextFrames,
        postContextFrames: options.postContextFrames,
        maskPreOffset: options.maskPreOffset,
        maskPostOffset: options.maskPostOffset,
        frameConstraint: options.frameConstraint,
    });
}

function isUsableEndpoint(edge, endpoint, anchor) {
    return edge === "start" ? endpoint < anchor : endpoint > anchor;
}

/**
 * Find the first constraint-valid endpoint at or beyond candidate in the given
 * scene-coordinate direction. The caller chooses the direction: typed/I-O
 * commits search away from the fixed anchor, while steppers search in their
 * explicit coordinate direction. If the scene edge prevents a valid endpoint,
 * the clamped candidate is returned with its authoritative padding plan.
 */
export function findConstrainedSelectionEndpoint({
    edge = "end",
    anchorFrame = 0,
    candidateFrame = 0,
    searchDirection = 1,
    sceneDuration = 0,
    preContextFrames = 0,
    postContextFrames = 0,
    maskPreOffset = 0,
    maskPostOffset = 0,
    frameConstraint = null,
} = {}) {
    edge = edge === "start" ? "start" : "end";
    const duration = Math.max(0, asInt(sceneDuration));
    const anchor = Math.max(0, Math.min(duration, asInt(anchorFrame)));
    const candidate = Math.max(0, Math.min(duration, asInt(candidateFrame)));
    const direction = searchDirection < 0 ? -1 : 1;
    const options = {
        edge,
        anchorFrame: anchor,
        sceneDuration: duration,
        preContextFrames,
        postContextFrames,
        maskPreOffset,
        maskPostOffset,
        frameConstraint,
    };
    const fallbackWindow = windowForEndpoint(options, candidate);
    if (!isUsableEndpoint(edge, candidate, anchor)) {
        return {
            valid: false,
            endpoint: candidate,
            window: fallbackWindow,
            used_padding_fallback: false,
        };
    }
    if (!hasFrameCountConstraint(frameConstraint) || fallbackWindow.frame_count_padding === 0) {
        return {
            valid: true,
            endpoint: candidate,
            window: fallbackWindow,
            used_padding_fallback: false,
        };
    }

    // Bounded context expansion is piecewise rather than monotonic: near a scene
    // edge, one endpoint step can suddenly make a full aligned context block
    // available. Inspect endpoints in coordinate order so steppers always choose
    // the true next/previous valid endpoint instead of jumping across that seam.
    for (let current = candidate + direction; current >= 0 && current <= duration; current += direction) {
        if (!isUsableEndpoint(edge, current, anchor)) break;
        const currentWindow = windowForEndpoint(options, current);
        if (currentWindow.frame_count_padding === 0) {
            return {
                valid: true,
                endpoint: current,
                window: currentWindow,
                used_padding_fallback: false,
            };
        }
    }

    return {
        valid: true,
        endpoint: candidate,
        window: fallbackWindow,
        used_padding_fallback: fallbackWindow.frame_count_padding > 0,
    };
}
