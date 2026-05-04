function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
}

const PLAYBACK_COMMIT_HOLD_MS = 400;
const PLAYBACK_OPAQUE_OPACITY = 0.999;
const PLAYBACK_COVERAGE_EPSILON = 0.75;
const PLAYBACK_PREBUFFER_GUARD_MS = 500;
const PLAYBACK_PREBUFFER_SAFE_LEAD_MS = 2000;

function fitRect(sourceWidth, sourceHeight, targetWidth, targetHeight) {
    const safeSourceWidth = Math.max(1, Number(sourceWidth) || 1);
    const safeSourceHeight = Math.max(1, Number(sourceHeight) || 1);
    const safeTargetWidth = Math.max(1, Number(targetWidth) || 1);
    const safeTargetHeight = Math.max(1, Number(targetHeight) || 1);
    const sourceAspect = safeSourceWidth / safeSourceHeight;
    const targetAspect = safeTargetWidth / safeTargetHeight;
    if (sourceAspect > targetAspect) {
        const width = safeTargetWidth;
        const height = width / sourceAspect;
        return { x: 0, y: (safeTargetHeight - height) / 2, width, height };
    }
    const height = safeTargetHeight;
    const width = height * sourceAspect;
    return { x: (safeTargetWidth - width) / 2, y: 0, width, height };
}

function removeMediaSource(mediaEl) {
    if (!mediaEl) return;
    try {
        mediaEl.pause?.();
    } catch (error) {}
    if (typeof mediaEl.removeAttribute === "function") {
        mediaEl.removeAttribute("src");
    }
    try {
        mediaEl.load?.();
    } catch (error) {}
}

function clearCacheObject(cache) {
    if (!cache || typeof cache !== "object") return;
    for (const key of Object.keys(cache)) {
        delete cache[key];
    }
}

function waitForMediaReady(mediaEl, minReadyState = 1, timeoutMs = 800) {
    if (!mediaEl) return Promise.resolve(null);
    if ((mediaEl.readyState || 0) >= minReadyState) {
        return Promise.resolve(mediaEl);
    }
    return new Promise((resolve) => {
        let settled = false;
        const finish = () => {
            if (settled) return;
            settled = true;
            cleanup();
            resolve(mediaEl);
        };
        const cleanup = () => {
            window.clearTimeout(timer);
            for (const eventName of ["loadedmetadata", "loadeddata", "canplay", "canplaythrough", "error"]) {
                mediaEl.removeEventListener(eventName, finish);
            }
        };
        const timer = window.setTimeout(finish, timeoutMs);
        for (const eventName of ["loadedmetadata", "loadeddata", "canplay", "canplaythrough", "error"]) {
            mediaEl.addEventListener(eventName, finish, { once: true });
        }
    });
}

function clampMediaTargetTime(mediaEl, targetTime) {
    const duration = Number(mediaEl?.duration);
    return Number.isFinite(duration) && duration > 0
        ? clamp(Number(targetTime) || 0, 0, Math.max(0, duration - 0.001))
        : Math.max(0, Number(targetTime) || 0);
}

function isMediaAtTarget(mediaEl, targetTime, tolerance = 0.02) {
    if (!mediaEl) return false;
    return Math.abs((Number(mediaEl.currentTime) || 0) - targetTime) <= tolerance;
}

function waitForDecodedVideoFrame(mediaEl, timeoutMs = 120) {
    if (!mediaEl || typeof mediaEl.requestVideoFrameCallback !== "function") {
        return Promise.resolve(mediaEl);
    }
    return new Promise((resolve) => {
        let settled = false;
        let callbackId = null;
        const finish = () => {
            if (settled) return;
            settled = true;
            window.clearTimeout(timer);
            if (callbackId !== null && typeof mediaEl.cancelVideoFrameCallback === "function") {
                try {
                    mediaEl.cancelVideoFrameCallback(callbackId);
                } catch (error) {}
            }
            resolve(mediaEl);
        };
        const timer = window.setTimeout(finish, timeoutMs);
        try {
            callbackId = mediaEl.requestVideoFrameCallback(finish);
        } catch (error) {
            finish();
        }
    });
}

function waitForDecodedVideoFrameAtTarget(mediaEl, targetTime, tolerance = 0.02, timeoutMs = 200) {
    if (!mediaEl || typeof mediaEl.requestVideoFrameCallback !== "function") {
        return Promise.resolve(isMediaAtTarget(mediaEl, targetTime, tolerance));
    }
    return new Promise((resolve) => {
        let settled = false;
        let callbackId = null;
        const finish = (ok) => {
            if (settled) return;
            settled = true;
            window.clearTimeout(timer);
            if (callbackId !== null && typeof mediaEl.cancelVideoFrameCallback === "function") {
                try {
                    mediaEl.cancelVideoFrameCallback(callbackId);
                } catch (error) {}
            }
            resolve(!!ok);
        };
        const timer = window.setTimeout(() => {
            finish(isMediaAtTarget(mediaEl, targetTime, tolerance));
        }, timeoutMs);
        try {
            callbackId = mediaEl.requestVideoFrameCallback((_now, metadata = {}) => {
                const mediaTime = Number(metadata.mediaTime);
                const decodedAtTarget = Number.isFinite(mediaTime)
                    ? Math.abs(mediaTime - targetTime) <= tolerance
                    : isMediaAtTarget(mediaEl, targetTime, tolerance);
                finish(decodedAtTarget);
            });
        } catch (error) {
            finish(isMediaAtTarget(mediaEl, targetTime, tolerance));
        }
    });
}

function seekMedia(mediaEl, targetTime, {
    tolerance = 0.02,
    timeoutMs = 250,
    requireTarget = false,
    waitForFrame = false,
} = {}) {
    if (!mediaEl) return Promise.resolve(null);
    return waitForMediaReady(mediaEl, 1).then((element) => {
        if (!element) return null;
        const safeTarget = clampMediaTargetTime(element, targetTime);
        const finishWithFrame = () => {
            const candidate = !requireTarget || isMediaAtTarget(element, safeTarget, tolerance)
                ? element
                : null;
            if (!candidate || !waitForFrame) return Promise.resolve(candidate);
            return waitForDecodedVideoFrame(candidate, Math.min(200, Math.max(80, timeoutMs)))
                .then(() => (!requireTarget || isMediaAtTarget(candidate, safeTarget, tolerance) ? candidate : null));
        };
        if ((element.readyState || 0) >= 2 && isMediaAtTarget(element, safeTarget, tolerance)) {
            return finishWithFrame();
        }
        return new Promise((resolve) => {
            let settled = false;
            const onMediaEvent = () => finish(false);
            const finish = (force = false) => {
                if (settled) return;
                const ready = (element.readyState || 0) >= 2;
                const atTarget = isMediaAtTarget(element, safeTarget, tolerance);
                if (!force && (!ready || !atTarget)) return;
                settled = true;
                cleanup();
                if (requireTarget && !atTarget) {
                    resolve(null);
                    return;
                }
                if (!waitForFrame) {
                    resolve(element);
                    return;
                }
                waitForDecodedVideoFrame(element, Math.min(200, Math.max(80, timeoutMs)))
                    .then(() => {
                        resolve(!requireTarget || isMediaAtTarget(element, safeTarget, tolerance) ? element : null);
                    });
            };
            const cleanup = () => {
                window.clearTimeout(timer);
                for (const eventName of ["seeked", "loadeddata", "canplay", "timeupdate", "error"]) {
                    element.removeEventListener(eventName, onMediaEvent);
                }
            };
            const timer = window.setTimeout(() => finish(true), timeoutMs);
            for (const eventName of ["seeked", "loadeddata", "canplay", "timeupdate", "error"]) {
                element.addEventListener(eventName, onMediaEvent, { once: true });
            }
            try {
                element.currentTime = safeTarget;
            } catch (error) {
                finish(true);
            }
        });
    });
}

export function createViewportSurface(options = {}) {
    const state = {
        canvas: options.canvas || null,
        ctx: options.canvas?.getContext?.("2d") || null,
        destroyed: false,
        liveMediaEnabled: !!options.initialLiveMediaEnabled,
        isPlaying: false,
        playbackRAF: null,
        playbackStartTime: 0,
        playbackStartFrame: 0,
        playbackSessionStartFrame: 0,
        playbackLoopRange: null,
        playbackSessionId: 0,
        playbackPrepareToken: 0,
        playbackCompositeCommitted: false,
        playbackBlockedSinceMs: null,
        playbackBlockedSignature: "",
        playbackCanvasWidth: 0,
        playbackCanvasHeight: 0,
        playbackLastCommittedFrame: null,
        playbackLastCommittedSignature: "",
        playbackLastCommittedSessionId: 0,
        renderToken: 0,
        sourceUrlCache: new Map(),
        activePlaybackVideos: new Map(),
        activePlaybackAudios: new Map(),
        prebufferCache: new Map(),
        prebufferSkipLogKeys: new Set(),
        videoCache: options.videoCache || {},
        audioCache: options.audioCache || {},
        imageCache: options.imageCache || {},
    };

    const noop = () => {};
    const getScene = options.getScene || (() => null);
    const getFrame = options.getFrame || (() => 0);
    const setFrame = options.setFrame || noop;
    const getTotalFrames = options.getTotalFrames || (() => 0);
    const getFps = options.getFps || (() => 24);
    const getLoopRange = options.getLoopRange || (() => null);
    const shouldReturnToPlaybackStart = options.shouldReturnToPlaybackStart || (() => false);
    const onFrameChange = options.onFrameChange || noop;
    const onTransportUpdate = options.onTransportUpdate || noop;
    const onPlaybackStateChange = options.onPlaybackStateChange || noop;
    const getAssetForSourcePath = options.getAssetForSourcePath || (() => null);
    const getGuideAsset = options.getGuideAsset || (() => null);
    const includeMotionDrivers = options.includeMotionDrivers || (() => false);
    const isVideoLaneHidden = options.isVideoLaneHidden || (() => false);
    const isMotionDriverLaneHidden = options.isMotionDriverLaneHidden || (() => false);
    const isAudioLaneHidden = options.isAudioLaneHidden || (() => false);
    const buildViewUrl = options.buildViewUrl || (() => null);
    const buildThumbnailUrl = options.buildThumbnailUrl || (() => null);
    const isPrebufferEnabled = options.isPrebufferEnabled || (() => true);
    const getPrebufferLookaheadMs = options.getPrebufferLookaheadMs || (() => 1000);

    function currentFrame() {
        return clamp(Math.round(Number(getFrame()) || 0), 0, totalFrames());
    }

    function totalFrames() {
        return Math.max(0, Math.round(Number(getTotalFrames()) || 0));
    }

    function fps() {
        return Math.max(1, Number(getFps()) || 24);
    }

    function firstDrawTolerance() {
        return Math.max(0.04, 1 / fps());
    }

    function playbackDriftTolerance() {
        return Math.max(0.08, 2 / fps());
    }

    function debugPlaybackBoundary(eventName, details = {}) {
        if (typeof window === "undefined" || !window.SONDER_DEBUG_PLAYBACK_BOUNDARY) return;
        console.debug("[Sonder Playback Boundary]", eventName, details);
    }

    function invalidateAsyncPreviewRenders() {
        state.renderToken += 1;
    }

    function resetPlaybackCompositeState() {
        state.playbackCompositeCommitted = false;
        state.playbackBlockedSinceMs = null;
        state.playbackBlockedSignature = "";
        state.playbackCanvasWidth = 0;
        state.playbackCanvasHeight = 0;
        state.playbackLastCommittedFrame = null;
        state.playbackLastCommittedSignature = "";
        state.playbackLastCommittedSessionId = 0;
        state.prebufferSkipLogKeys.clear();
    }

    function playbackCanvasStillValid() {
        return !!(
            state.playbackCompositeCommitted
            && state.canvas
            && state.playbackCanvasWidth === state.canvas.width
            && state.playbackCanvasHeight === state.canvas.height
        );
    }

    function notifyTransport() {
        onTransportUpdate({
            frame: currentFrame(),
            totalFrames: totalFrames(),
            isPlaying: state.isPlaying,
            liveMediaEnabled: state.liveMediaEnabled,
        });
    }

    function updatePlaybackState(nextValue) {
        if (state.isPlaying === nextValue) {
            notifyTransport();
            return;
        }
        state.isPlaying = nextValue;
        onPlaybackStateChange(nextValue);
        notifyTransport();
    }

    function applyFrame(nextFrame, meta = {}) {
        const clampedFrame = clamp(Math.round(Number(nextFrame) || 0), 0, totalFrames());
        setFrame(clampedFrame, meta);
        onFrameChange(clampedFrame, meta);
        notifyTransport();
        return clampedFrame;
    }

    function getGuideAtFrame(frame) {
        const scene = getScene();
        if (!scene?.guide_frames?.length) return null;
        const lastFrame = Math.max(0, totalFrames() - 1);
        let best = null;
        let bestFrame = -1;
        for (const guide of scene.guide_frames) {
            const frameIndex = guide.frame_index === -1 ? lastFrame : Math.max(0, parseInt(guide.frame_index, 10) || 0);
            if (frameIndex <= frame && frameIndex >= bestFrame) {
                best = guide;
                bestFrame = frameIndex;
            }
        }
        return best;
    }

    function getVisibleClipLayers(frame) {
        const scene = getScene();
        if (!scene?.clips?.length) return [];
        return scene.clips
            .filter((clip) => frame >= clip.timeline_start_frame && frame < clip.timeline_end_frame)
            .filter((clip) => {
                if (!clip.role || clip.role === "render") return true;
                return clip.role === "motion_driver" && includeMotionDrivers();
            })
            .filter((clip) => {
                if (clip.role === "motion_driver") {
                    return !isMotionDriverLaneHidden(clip.track_index || 0);
                }
                return !isVideoLaneHidden(clip.track_index || 0);
            })
            .sort((a, b) => (a.track_index || 0) - (b.track_index || 0))
            .map((clip) => ({
                clip,
                asset: getAssetForSourcePath(clip.source_path) || null,
                key: clip.clip_id || `${clip.source_path}:${clip.timeline_start_frame}:${clip.track_index || 0}`,
                opacity: clamp(Number(clip.opacity ?? 1), 0, 1),
            }));
    }

    function getVisibleAudioLayers(frame) {
        const scene = getScene();
        if (!scene?.audio_tracks?.length) return [];
        return scene.audio_tracks
            .filter((track) => frame >= track.timeline_start_frame && frame < track.timeline_end_frame)
            .filter((track) => !isAudioLaneHidden(track.lane_index || 0))
            .filter((track) => !track.muted)
            .map((track) => ({
                track,
                asset: getAssetForSourcePath(track.source_path) || null,
                key: track.track_id || `${track.source_path}:${track.timeline_start_frame}:${track.lane_index || 0}`,
            }))
            .filter((layer) => layer.asset && !layer.asset.missing);
    }

    function buildFrameSnapshot(frame) {
        const guide = getGuideAtFrame(frame);
        const guideAsset = guide ? getGuideAsset(guide) : null;
        const clipLayers = getVisibleClipLayers(frame);
        const playableClipLayers = clipLayers.filter((layer) => layer.asset && !layer.asset.missing);
        const missingClipLayers = clipLayers.filter((layer) => !layer.asset || !!layer.asset.missing);
        return {
            frame,
            guide,
            guideAsset,
            clipLayers,
            playableClipLayers,
            missingClipLayers,
            audioLayers: getVisibleAudioLayers(frame),
        };
    }

    function getCanvasContext() {
        if (!state.canvas) return null;
        if (!state.ctx) {
            state.ctx = state.canvas.getContext("2d");
        }
        return state.ctx;
    }

    function drawBlack() {
        const ctx = getCanvasContext();
        if (!ctx || !state.canvas) return null;
        ctx.fillStyle = "#000";
        ctx.fillRect(0, 0, state.canvas.width, state.canvas.height);
        return ctx;
    }

    function drawViewportText(title, subtitle = "", palette = {}) {
        const ctx = drawBlack();
        if (!ctx || !state.canvas) return;
        const width = state.canvas.width;
        const height = state.canvas.height;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillStyle = palette.titleColor || "rgba(255,255,255,0.18)";
        ctx.font = `${Math.max(16, height / 12)}px monospace`;
        ctx.fillText(title, width / 2, height / 2 - (subtitle ? 12 : 0));
        if (subtitle) {
            ctx.fillStyle = palette.subtitleColor || "rgba(255,255,255,0.56)";
            ctx.font = `${Math.max(11, height / 24)}px sans-serif`;
            ctx.fillText(subtitle, width / 2, height / 2 + 16);
        }
    }

    function drawImageLike(element, { opacity = 1 } = {}) {
        const ctx = getCanvasContext();
        if (!ctx || !state.canvas || !element) return false;
        const width = element.videoWidth || element.naturalWidth || element.width || state.canvas.width;
        const height = element.videoHeight || element.naturalHeight || element.height || state.canvas.height;
        const rect = fitRect(width, height, state.canvas.width, state.canvas.height);
        const previousAlpha = ctx.globalAlpha;
        ctx.globalAlpha = clamp(Number(opacity) || 0, 0, 1);
        try {
            ctx.drawImage(element, rect.x, rect.y, rect.width, rect.height);
            return true;
        } catch (error) {
            return false;
        } finally {
            ctx.globalAlpha = previousAlpha;
        }
    }

    function imageLikeDrawRect(element) {
        if (!state.canvas || !element) return null;
        const width = Number(element.videoWidth || element.naturalWidth || element.width);
        const height = Number(element.videoHeight || element.naturalHeight || element.height);
        if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return null;
        return fitRect(width, height, state.canvas.width, state.canvas.height);
    }

    function imageLikeCoversCanvas(element, opacity = 1) {
        if (!state.canvas || clamp(Number(opacity) || 0, 0, 1) < PLAYBACK_OPAQUE_OPACITY) return false;
        const rect = imageLikeDrawRect(element);
        if (!rect) return false;
        return (
            rect.x <= PLAYBACK_COVERAGE_EPSILON
            && rect.y <= PLAYBACK_COVERAGE_EPSILON
            && rect.x + rect.width >= state.canvas.width - PLAYBACK_COVERAGE_EPSILON
            && rect.y + rect.height >= state.canvas.height - PLAYBACK_COVERAGE_EPSILON
        );
    }

    function resolvePreviewImageUrl(layer) {
        if (!layer?.asset) return null;
        if (layer.asset.asset_type === "image") {
            return buildViewUrl(layer.asset.path || layer.clip?.source_path || "");
        }
        if (layer.asset.asset_id) {
            return buildThumbnailUrl(layer.asset.asset_id);
        }
        return null;
    }

    function loadImage(cacheKey, src) {
        if (!cacheKey || !src) return Promise.resolve(null);
        const existing = state.imageCache[cacheKey];
        if (existing?.src === src && existing.img) {
            return Promise.resolve(existing.img);
        }
        if (existing?.src === src && existing.promise) {
            return existing.promise;
        }
        const img = new Image();
        img.crossOrigin = "anonymous";
        const promise = new Promise((resolve) => {
            img.onload = () => {
                state.imageCache[cacheKey] = { src, img, promise: null };
                resolve(img);
            };
            img.onerror = () => {
                state.imageCache[cacheKey] = { src, img: null, promise: null };
                resolve(null);
            };
        });
        state.imageCache[cacheKey] = { src, img: null, promise };
        img.src = src;
        return promise;
    }

    function getReadyImage(cacheKey, src, { rerenderOnLoad = false } = {}) {
        if (!cacheKey || !src) return null;
        const existing = state.imageCache[cacheKey];
        if (existing?.src === src && existing.img) {
            return existing.img;
        }
        if (!(existing?.src === src && existing.promise)) {
            loadImage(cacheKey, src).then(() => {
                if (rerenderOnLoad && !state.destroyed) {
                    renderFrame();
                }
            });
        }
        return null;
    }

    function resolveMediaSourceUrl(sourcePath) {
        if (!sourcePath) return Promise.resolve(null);
        const cached = state.sourceUrlCache.get(sourcePath);
        if (cached?.promise) {
            return cached.promise;
        }
        const directUrl = buildViewUrl(sourcePath);
        if (!directUrl) return Promise.resolve(null);
        const entry = {};
        entry.promise = fetch(directUrl)
            .then((response) => {
                if (!response.ok) {
                    throw new Error(`Failed to fetch media: ${response.status}`);
                }
                return response.blob();
            })
            .then((blob) => {
                entry.objectUrl = URL.createObjectURL(blob);
                entry.usesObjectUrl = true;
                if (state.sourceUrlCache.get(sourcePath) !== entry || state.destroyed) {
                    URL.revokeObjectURL(entry.objectUrl);
                    entry.objectUrl = null;
                    return null;
                }
                return entry.objectUrl;
            })
            .catch((error) => {
                if (state.sourceUrlCache.get(sourcePath) !== entry || state.destroyed) {
                    return null;
                }
                console.warn("[Sonder] Failed to load media as blob, falling back to direct URL:", error);
                entry.objectUrl = directUrl;
                entry.usesObjectUrl = false;
                return directUrl;
            });
        state.sourceUrlCache.set(sourcePath, entry);
        return entry.promise;
    }

    function getOrCreateVideo(layer) {
        if (!layer?.key) return null;
        if (!state.videoCache[layer.key]) {
            const video = document.createElement("video");
            video.preload = "auto";
            video.muted = true;
            video.playsInline = true;
            state.videoCache[layer.key] = video;
        }
        return state.videoCache[layer.key];
    }

    function getOrCreateAudio(layer) {
        if (!layer?.key) return null;
        if (!state.audioCache[layer.key]) {
            const audio = document.createElement("audio");
            audio.preload = "auto";
            state.audioCache[layer.key] = audio;
        }
        return state.audioCache[layer.key];
    }

    function createMutedVideoElement() {
        const video = document.createElement("video");
        video.preload = "auto";
        video.muted = true;
        video.playsInline = true;
        return video;
    }

    function isRenderableVideoLayer(layer) {
        return !!(
            layer?.key
            && layer?.clip?.source_path
            && layer.asset
            && !layer.asset.missing
            && layer.asset.asset_type !== "image"
        );
    }

    function prebufferKeyForLayer(layer) {
        if (!isRenderableVideoLayer(layer)) return "";
        return `${layer.key}::${layer.clip.source_path}`;
    }

    function normalizedPrebufferLookaheadMs() {
        const numeric = Number(getPrebufferLookaheadMs());
        return clamp(Number.isFinite(numeric) ? Math.round(numeric) : 1000, 100, 5000);
    }

    function prebufferGuardFrames() {
        return Math.max(2, Math.round((PLAYBACK_PREBUFFER_GUARD_MS / 1000) * fps()));
    }

    function prebufferSafeLeadFrames() {
        return Math.max(
            prebufferGuardFrames() + 1,
            Math.round((PLAYBACK_PREBUFFER_SAFE_LEAD_MS / 1000) * fps()),
        );
    }

    function playbackFrameDistance(fromFrame, targetFrame, endFrame) {
        const from = Math.max(0, Math.round(Number(fromFrame) || 0));
        const target = Math.max(0, Math.round(Number(targetFrame) || 0));
        const loopRange = state.playbackLoopRange;
        if (!loopRange) return target - from;
        const loopStart = Math.max(0, Math.round(Number(loopRange.start) || 0));
        const loopEnd = Math.max(loopStart + 1, Math.round(Number(endFrame) || Number(loopRange.end) || loopStart + 1));
        if (target >= from) return target - from;
        return Math.max(0, loopEnd - from) + Math.max(0, target - loopStart);
    }

    function discardPrebufferEntry(entry) {
        if (!entry) return;
        if (entry.claimedByActive) return;
        entry.cancelled = true;
        removeMediaSource(entry.video);
    }

    function clearPrebufferCache() {
        for (const [key, entry] of Array.from(state.prebufferCache.entries())) {
            state.prebufferCache.delete(key);
            discardPrebufferEntry(entry);
        }
        state.prebufferSkipLogKeys.clear();
    }

    function debugPrebufferSkipOnce(eventName, key, details = {}) {
        const targetFrame = details.targetFrame ?? "";
        const logKey = `${state.playbackSessionId}:${eventName}:${key}:${targetFrame}`;
        if (state.prebufferSkipLogKeys.has(logKey)) return;
        state.prebufferSkipLogKeys.add(logKey);
        debugPlaybackBoundary(eventName, details);
    }

    async function ensureMediaElementSource(mediaEl, sourcePath) {
        if (!mediaEl || !sourcePath) return null;
        const resolvedUrl = await resolveMediaSourceUrl(sourcePath);
        if (!resolvedUrl || state.destroyed) return null;
        if (mediaEl._sonderSourceUrl !== resolvedUrl) {
            mediaEl._sonderSourceUrl = resolvedUrl;
            mediaEl.src = resolvedUrl;
        }
        await waitForMediaReady(mediaEl, 1);
        return mediaEl;
    }

    async function captureSourceFrame(sourcePath, sourceFrame, targetLongEdge) {
        if (!sourcePath || state.destroyed) return null;
        if (typeof OffscreenCanvas !== "function") {
            return null;
        }
        const frameIndex = Math.max(0, Math.round(Number(sourceFrame) || 0));
        const captureFps = fps();
        const tolerance = 0.5 / captureFps;
        const targetTime = (frameIndex + 0.5) / captureFps;
        const cacheKey = `snapshot:${sourcePath}`;
        let video = state.videoCache[cacheKey];
        if (!video) {
            video = createMutedVideoElement();
            video.draggable = false;
            state.videoCache[cacheKey] = video;
        }
        const loaded = await ensureMediaElementSource(video, sourcePath);
        if (!loaded || state.destroyed) return null;
        await waitForMediaReady(video, 2, 1500);
        const sought = await seekMedia(video, targetTime, {
            tolerance,
            timeoutMs: 900,
            requireTarget: true,
            waitForFrame: true,
        });
        if (!sought || (video.readyState || 0) < 2) return null;
        const decodedAtTarget = await waitForDecodedVideoFrameAtTarget(video, targetTime, tolerance, 240);
        if (!decodedAtTarget) return null;

        const sourceWidth = Math.round(Number(video.videoWidth) || 0);
        const sourceHeight = Math.round(Number(video.videoHeight) || 0);
        if (sourceWidth <= 0 || sourceHeight <= 0) return null;
        const sourceLongEdge = Math.max(sourceWidth, sourceHeight);
        const requestedLong = Math.max(1, Math.round(Number(targetLongEdge) || sourceLongEdge));
        const scale = requestedLong / sourceLongEdge;
        const width = Math.max(1, Math.round(sourceWidth * scale));
        const height = Math.max(1, Math.round(sourceHeight * scale));
        const canvas = new OffscreenCanvas(width, height);
        const ctx = canvas.getContext("2d");
        if (!ctx) return null;
        ctx.drawImage(video, 0, 0, width, height);
        const blob = await canvas.convertToBlob({ type: "image/png" });
        if (!blob) return null;
        return {
            blob,
            width,
            height,
            sourceWidth,
            sourceHeight,
            sourceLongEdge,
            targetLongEdge: requestedLong,
            mediaTime: Number(video.currentTime) || targetTime,
        };
    }

    async function loadPrebufferEntry(entry) {
        if (!entry?.video || !entry.sourcePath) return null;
        const resolvedUrl = await resolveMediaSourceUrl(entry.sourcePath);
        if (!resolvedUrl || state.destroyed || entry.cancelled) return null;
        const video = entry.video;
        if (video._sonderSourceUrl !== resolvedUrl) {
            video._sonderSourceUrl = resolvedUrl;
            video.src = resolvedUrl;
        }
        await waitForMediaReady(video, 2, 1500);
        if (state.destroyed || entry.cancelled) return null;
        const targetTime = clipSourceTime(entry.layer, entry.targetFrame);
        entry.targetTime = targetTime;
        const sought = await seekMedia(video, targetTime, {
            tolerance: 0.03,
            timeoutMs: 700,
            requireTarget: true,
            waitForFrame: true,
        });
        if (!sought || state.destroyed || entry.cancelled) return null;
        await waitForMediaReady(video, 2, 500);
        if (state.destroyed || entry.cancelled) return null;
        entry.ready = (video.readyState || 0) >= 2 && isMediaAtTarget(video, targetTime, 0.04);
        if (entry.consumed && state.isPlaying) {
            renderFrame();
        }
        return entry.ready ? video : null;
    }

    function ensurePrebufferedLayer(layer, targetFrame, currentFrame, endFrame) {
        const key = prebufferKeyForLayer(layer);
        if (!key || state.activePlaybackVideos.has(layer.key)) return;
        const leadFrames = playbackFrameDistance(currentFrame, targetFrame, endFrame);
        const safeLeadFrames = prebufferSafeLeadFrames();
        if (leadFrames <= safeLeadFrames) {
            debugPrebufferSkipOnce("skip-unsafe-prebuffer", key, {
                key,
                layerKey: layer.key,
                sourcePath: layer.clip?.source_path || "",
                frame: currentFrame,
                targetFrame,
                leadFrames,
                safeLeadFrames,
                guardFrames: prebufferGuardFrames(),
            });
            return;
        }
        const existing = state.prebufferCache.get(key);
        if (existing) {
            existing.layer = layer;
            existing.targetFrame = targetFrame;
            return;
        }
        const entry = {
            key,
            layer,
            layerKey: layer.key,
            sourcePath: layer.clip.source_path,
            targetFrame,
            targetTime: null,
            video: createMutedVideoElement(),
            ready: false,
            cancelled: false,
            consumed: false,
            claimedByActive: false,
            promise: null,
        };
        entry.promise = loadPrebufferEntry(entry)
            .catch(() => null)
            .then((element) => {
                if (!element && !entry.cancelled && state.prebufferCache.get(key) === entry) {
                    state.prebufferCache.delete(key);
                }
                return element;
            });
        state.prebufferCache.set(key, entry);
    }

    function claimPrebufferedVideo(layer, frame) {
        const key = prebufferKeyForLayer(layer);
        const entry = key ? state.prebufferCache.get(key) : null;
        if (!entry?.video || entry.cancelled) return null;
        if (readyPrebufferEntryForLayerFrame(layer, frame) !== entry) {
            state.prebufferCache.delete(key);
            discardPrebufferEntry(entry);
            return null;
        }
        state.prebufferCache.delete(key);
        entry.claimedByActive = true;
        entry.consumed = true;
        entry.layer = layer;
        const existing = state.videoCache[layer.key];
        if (existing && existing !== entry.video) {
            removeMediaSource(existing);
        }
        state.videoCache[layer.key] = entry.video;
        return { entry, video: entry.video, key };
    }

    function playbackSearchFrame(startFrame, offset, endFrame) {
        const loopRange = state.playbackLoopRange;
        const frame = startFrame + offset;
        if (!loopRange) {
            return frame < endFrame ? frame : null;
        }
        const loopStart = Math.max(0, Math.round(Number(loopRange.start) || 0));
        const loopEnd = Math.max(loopStart + 1, Math.round(Number(loopRange.end) || loopStart + 1));
        const loopLength = Math.max(1, loopEnd - loopStart);
        if (frame < loopEnd) return frame;
        return loopStart + ((frame - loopEnd) % loopLength);
    }

    function findUpcomingPrebufferLayers(snapshot, endFrame) {
        const currentKeys = new Set(
            (snapshot?.playableClipLayers || [])
                .filter(isRenderableVideoLayer)
                .map(prebufferKeyForLayer)
        );
        const startFrame = Math.max(0, Math.round(Number(snapshot?.frame) || 0) + 1);
        const lookaheadFrames = Math.max(1, Math.round((normalizedPrebufferLookaheadMs() / 1000) * fps()));
        if (lookaheadFrames <= prebufferSafeLeadFrames()) {
            return { frame: playbackSearchFrame(startFrame, 0, endFrame) ?? startFrame, layers: [] };
        }
        for (let offset = 0; offset < lookaheadFrames; offset += 1) {
            const frame = playbackSearchFrame(startFrame, offset, endFrame);
            if (frame === null) break;
            const futureSnapshot = buildFrameSnapshot(frame);
            const candidates = futureSnapshot.playableClipLayers
                .filter(isRenderableVideoLayer)
                .filter((layer) => !currentKeys.has(prebufferKeyForLayer(layer)))
                .filter((layer) => !state.activePlaybackVideos.has(layer.key));
            if (candidates.length) {
                return { frame, layers: candidates };
            }
        }
        return { frame: playbackSearchFrame(startFrame, 0, endFrame) ?? startFrame, layers: [] };
    }

    function schedulePlaybackPrebuffer(snapshot) {
        if (!state.isPlaying || !isPrebufferEnabled()) {
            clearPrebufferCache();
            return;
        }
        const playbackEndFrame = state.playbackLoopRange
            ? state.playbackLoopRange.end
            : totalFrames();
        if (!state.playbackLoopRange && playbackEndFrame <= (snapshot?.frame || 0) + 1) {
            clearPrebufferCache();
            return;
        }
        const upcoming = findUpcomingPrebufferLayers(snapshot, playbackEndFrame);
        const safeLeadFrames = prebufferSafeLeadFrames();
        for (const [key, entry] of Array.from(state.prebufferCache.entries())) {
            const leadFrames = playbackFrameDistance(snapshot?.frame || 0, entry?.targetFrame, playbackEndFrame);
            if (entry?.ready || leadFrames > safeLeadFrames) continue;
            state.prebufferCache.delete(key);
            discardPrebufferEntry(entry);
            debugPlaybackBoundary("cancel-unsafe-prebuffer", {
                key,
                layerKey: entry?.layerKey || "",
                sourcePath: entry?.sourcePath || "",
                frame: snapshot?.frame || 0,
                targetFrame: entry?.targetFrame,
                leadFrames,
                safeLeadFrames,
                guardFrames: prebufferGuardFrames(),
            });
        }
        const desiredKeys = new Set([
            ...(snapshot?.playableClipLayers || []).map(prebufferKeyForLayer).filter(Boolean),
            ...upcoming.layers.map(prebufferKeyForLayer).filter(Boolean),
        ]);
        for (const [key, entry] of Array.from(state.prebufferCache.entries())) {
            if (desiredKeys.has(key)) continue;
            if (entry?.claimedByActive) {
                state.prebufferCache.delete(key);
                continue;
            }
            state.prebufferCache.delete(key);
            discardPrebufferEntry(entry);
        }
        for (const layer of upcoming.layers) {
            ensurePrebufferedLayer(layer, upcoming.frame, snapshot?.frame || 0, playbackEndFrame);
        }
    }

    async function loadGuideLayer(snapshot) {
        if (!snapshot?.guide || !snapshot.guideAsset || snapshot.guideAsset.missing) return null;
        const cacheKey = `guide:${snapshot.guide.asset_id}`;
        const src = buildViewUrl(snapshot.guideAsset.path);
        return await loadImage(cacheKey, src);
    }

    async function renderStaticComposite(snapshot, renderToken) {
        const sources = [];
        const guideImage = await loadGuideLayer(snapshot);
        if (state.destroyed || state.isPlaying || renderToken !== state.renderToken) return;
        if (guideImage) {
            sources.push({ element: guideImage, opacity: 1 });
        }
        for (const layer of snapshot.playableClipLayers) {
            const src = resolvePreviewImageUrl(layer);
            if (!src) continue;
            const cacheKey = `preview:${layer.asset?.asset_id || layer.key}`;
            const img = await loadImage(cacheKey, src);
            if (state.destroyed || state.isPlaying || renderToken !== state.renderToken) return;
            if (img) {
                sources.push({ element: img, opacity: layer.opacity });
            }
        }
        drawBlack();
        let drewAny = false;
        for (const source of sources) {
            if (drawImageLike(source.element, { opacity: source.opacity })) {
                drewAny = true;
            }
        }
        if (!drewAny) {
            drawViewportText("Preview unavailable", "Click Load Preview for live media.");
        }
    }

    function clipSourceTime(layer, frame) {
        const sourceFrame = frame - layer.clip.timeline_start_frame + (layer.clip.source_in_frame || 0);
        return Math.max(0, (sourceFrame + 0.5) / fps());
    }

    function clipSourceFrame(layer, frame) {
        const clip = layer?.clip || {};
        return (
            (Math.round(Number(frame) || 0))
            - (Math.round(Number(clip.timeline_start_frame) || 0))
            + (Math.round(Number(clip.source_in_frame) || 0))
        );
    }

    function isPlaybackTailFrame(layer, frame) {
        const clip = layer?.clip;
        if (!clip) return false;
        const timelineEnd = Number(clip.timeline_end_frame);
        const sourceOut = Number(clip.source_out_frame);
        const sourceFrame = clipSourceFrame(layer, frame);
        return (
            (Number.isFinite(timelineEnd) && frame >= timelineEnd - 1)
            || (Number.isFinite(sourceOut) && sourceFrame >= sourceOut - 1)
        );
    }

    function nextPlaybackFrameAfter(frame) {
        const endFrame = state.playbackLoopRange ? state.playbackLoopRange.end : totalFrames();
        return playbackSearchFrame(Math.max(0, Math.round(Number(frame) || 0) + 1), 0, endFrame);
    }

    function readyPrebufferEntryForLayerFrame(layer, frame) {
        const key = prebufferKeyForLayer(layer);
        const entry = key ? state.prebufferCache.get(key) : null;
        if (!entry?.video || entry.cancelled || !entry.ready) return null;
        const targetTime = clipSourceTime(layer, frame);
        if ((entry.video.readyState || 0) < 2 || entry.video.seeking) return null;
        return isMediaAtTarget(entry.video, targetTime, firstDrawTolerance()) ? entry : null;
    }

    function hasReadyNextPlaybackVideo(frame) {
        const nextFrame = nextPlaybackFrameAfter(frame);
        if (nextFrame === null) return false;
        const nextSnapshot = buildFrameSnapshot(nextFrame);
        for (const layer of nextSnapshot.playableClipLayers) {
            if (!isRenderableVideoLayer(layer) || clamp(Number(layer.opacity ?? 1), 0, 1) <= 0) continue;
            const active = state.activePlaybackVideos.get(layer.key);
            if (isActiveVideoDrawable(active, layer, nextFrame)) return true;
            if (readyPrebufferEntryForLayerFrame(layer, nextFrame)) return true;
        }
        return false;
    }

    function readyPlaybackRenderableForLayer(layer, frame) {
        const opacity = clamp(Number(layer?.opacity ?? 1), 0, 1);
        if (!layer?.asset || opacity <= 0) return null;
        if (layer.asset.asset_type === "image") {
            const src = buildViewUrl(layer.asset.path || layer.clip?.source_path || "");
            const image = getReadyImage(`live-image:${layer.asset.asset_id || layer.key}`, src, { rerenderOnLoad: true });
            return image ? { type: "image", element: image, opacity, layer } : null;
        }
        const active = state.activePlaybackVideos.get(layer.key);
        if (!isActiveVideoDrawable(active, layer, frame)) return null;
        return { type: "video", element: active.video, opacity, layer, active };
    }

    function playbackRenderableCoversCanvas(renderable) {
        return imageLikeCoversCanvas(renderable?.element, renderable?.opacity);
    }

    function playbackLayerDebug(layer, reason, extra = {}) {
        return {
            reason,
            layerKey: layer?.key || "",
            sourcePath: layer?.clip?.source_path || "",
            opacity: clamp(Number(layer?.opacity ?? 1), 0, 1),
            ...extra,
        };
    }

    function isLayerCoveredByDrawableUpperLayer(layer, snapshot) {
        const layers = snapshot?.playableClipLayers || [];
        const index = layers.indexOf(layer);
        if (index < 0) return false;
        for (let i = layers.length - 1; i > index; i -= 1) {
            const renderable = readyPlaybackRenderableForLayer(layers[i], snapshot.frame);
            if (renderable && playbackRenderableCoversCanvas(renderable)) return true;
        }
        return false;
    }

    function shouldDeferPlaybackTailPrepare(active, layer, snapshot) {
        if (!active?.firstDrawComplete || !isPlaybackTailFrame(layer, snapshot.frame) || !playbackCanvasStillValid()) {
            return false;
        }
        return isLayerCoveredByDrawableUpperLayer(layer, snapshot) || hasReadyNextPlaybackVideo(snapshot.frame);
    }

    function audioSourceTime(layer, frame) {
        const sourceFrame = frame - layer.track.timeline_start_frame + (layer.track.source_in_frame || 0);
        return Math.max(0, sourceFrame / fps());
    }

    function createActivePlaybackVideoEntry(layer, video, prebufferClaim = null) {
        return {
            layer,
            video,
            sourcePath: layer.clip.source_path,
            layerKey: layer.key,
            prepareToken: 0,
            requestedFrame: null,
            readyForDraw: false,
            firstDrawComplete: false,
            claimedPrebufferKey: prebufferClaim?.key || "",
            claimedPrebufferEntry: prebufferClaim?.entry || null,
            pendingPrepare: null,
            playbackSessionId: state.playbackSessionId,
        };
    }

    function playbackVideoAtFrame(active, layer, frame, tolerance) {
        if (!active?.video || !layer?.clip) return false;
        if (active.layerKey !== layer.key || active.sourcePath !== layer.clip.source_path) return false;
        if (active.video.seeking || (active.video.readyState || 0) < 2) return false;
        return isMediaAtTarget(active.video, clipSourceTime(layer, frame), tolerance);
    }

    function isActiveVideoDrawable(active, layer, frame) {
        if (!active?.readyForDraw) return false;
        const tolerance = active.firstDrawComplete ? playbackDriftTolerance() : firstDrawTolerance();
        return playbackVideoAtFrame(active, layer, frame, tolerance);
    }

    function syncPreparedVideoPlayback(active, layer, frame) {
        if (!active?.video) return;
        active.video.muted = true;
        if (layer && isPlaybackTailFrame(layer, frame)) {
            active.video.pause();
            return;
        }
        if (active.video.paused) {
            active.video.play().catch(() => {});
        }
    }

    function pauseTailPlaybackVideo(active, layer, frame) {
        if (!active?.video || !layer || !isPlaybackTailFrame(layer, frame)) return;
        active.video.pause();
    }

    function prepareActivePlaybackVideo(active, layer, frame, { force = false } = {}) {
        if (!active?.video || !layer?.clip?.source_path) return Promise.resolve(null);
        const sourcePath = layer.clip.source_path;
        const expectedTime = clipSourceTime(layer, frame);
        const targetTolerance = firstDrawTolerance();
        const sessionId = state.playbackSessionId;
        const existingAtTarget = playbackVideoAtFrame(active, layer, frame, targetTolerance);
        if (!force && existingAtTarget) {
            active.layer = layer;
            active.layerKey = layer.key;
            active.sourcePath = sourcePath;
            active.requestedFrame = frame;
            active.readyForDraw = true;
            active.playbackSessionId = sessionId;
            syncPreparedVideoPlayback(active, layer, frame);
            return Promise.resolve(active.video);
        }
        if (
            active.pendingPrepare
            && active.requestedFrame === frame
            && active.layerKey === layer.key
            && active.sourcePath === sourcePath
            && active.playbackSessionId === state.playbackSessionId
        ) {
            return active.pendingPrepare;
        }
        const token = ++state.playbackPrepareToken;
        active.layer = layer;
        active.layerKey = layer.key;
        active.sourcePath = sourcePath;
        active.prepareToken = token;
        active.requestedFrame = frame;
        active.readyForDraw = false;
        active.playbackSessionId = sessionId;

        active.pendingPrepare = ensureMediaElementSource(active.video, sourcePath)
            .then((element) => waitForMediaReady(element, 2))
            .then((element) => seekMedia(element, expectedTime, {
                tolerance: targetTolerance,
                timeoutMs: active.firstDrawComplete ? 300 : 700,
                requireTarget: true,
                waitForFrame: true,
            }))
            .then((element) => {
                const stillActive = (
                    state.isPlaying
                    && !state.destroyed
                    && state.playbackSessionId === sessionId
                    && state.activePlaybackVideos.get(layer.key) === active
                    && active.prepareToken === token
                    && active.layerKey === layer.key
                    && active.sourcePath === sourcePath
                );
                if (!stillActive) {
                    if (state.activePlaybackVideos.get(layer.key) === active && active.prepareToken === token) {
                        active.pendingPrepare = null;
                    }
                    return null;
                }
                active.pendingPrepare = null;
                active.readyForDraw = !!element && playbackVideoAtFrame(active, layer, frame, targetTolerance);
                if (active.readyForDraw) {
                    syncPreparedVideoPlayback(active, layer, frame);
                    renderFrame();
                    return active.video;
                }
                return null;
            })
            .catch(() => {
                if (state.activePlaybackVideos.get(layer.key) === active && active.prepareToken === token) {
                    active.pendingPrepare = null;
                    active.readyForDraw = false;
                }
                return null;
            });
        return active.pendingPrepare;
    }

    function playbackLayerSignature(snapshot) {
        const parts = [];
        if (snapshot?.guide && snapshot.guideAsset && !snapshot.guideAsset.missing) {
            parts.push([
                "guide",
                snapshot.guide.frame_index ?? "",
                snapshot.guide.asset_id || "",
                snapshot.guideAsset.asset_id || "",
                snapshot.guideAsset.path || "",
            ].join(":"));
        } else {
            parts.push("guide:none");
        }
        for (const [index, layer] of (snapshot?.playableClipLayers || []).entries()) {
            if ((Number(layer.opacity ?? 1) || 0) <= 0) continue;
            const clip = layer.clip || {};
            const asset = layer.asset || {};
            parts.push([
                "layer",
                index,
                asset.asset_id || "",
                asset.asset_type || "unknown",
                layer.key || "",
                clip.source_path || "",
                clamp(Number(layer.opacity ?? 1), 0, 1),
                clip.timeline_start_frame ?? "",
                clip.timeline_end_frame ?? "",
                clip.source_in_frame ?? "",
                clip.source_out_frame ?? "",
                clip.role || "",
                clip.track_index ?? "",
            ].join(":"));
        }
        return parts.join("|");
    }

    function shouldReuseCommittedPlaybackFrame(snapshot) {
        if (!state.isPlaying || !playbackCanvasStillValid()) return false;
        if (state.playbackLastCommittedFrame !== snapshot.frame) return false;
        if (state.playbackLastCommittedSessionId !== state.playbackSessionId) return false;
        const signature = playbackLayerSignature(snapshot);
        if (state.playbackLastCommittedSignature !== signature) return false;
        debugPlaybackBoundary("reuse-committed-playback-frame", {
            frame: snapshot.frame,
            signature,
            storedSignature: state.playbackLastCommittedSignature,
            playbackSessionId: state.playbackSessionId,
            canvasWidth: state.canvas?.width || 0,
            canvasHeight: state.canvas?.height || 0,
        });
        return true;
    }

    function playbackBlockDetails(reason, layer, snapshot, active = null, extra = {}) {
        const expectedTime = layer?.clip ? clipSourceTime(layer, snapshot.frame) : null;
        return {
            reason,
            frame: snapshot.frame,
            layerKey: layer?.key || "",
            sourcePath: layer?.clip?.source_path || "",
            expectedTime,
            currentTime: active?.video ? Number(active.video.currentTime) || 0 : null,
            readyState: active?.video ? active.video.readyState || 0 : null,
            seeking: !!active?.video?.seeking,
            readyForDraw: !!active?.readyForDraw,
            prebufferKey: layer ? prebufferKeyForLayer(layer) : "",
            claimedPrebufferKey: active?.claimedPrebufferKey || "",
            prebufferReady: !!active?.claimedPrebufferEntry?.ready,
            ...extra,
        };
    }

    function playbackGuideRenderable(snapshot) {
        if (!snapshot?.guide || !snapshot.guideAsset || snapshot.guideAsset.missing) return { element: null };
        const cacheKey = `guide:${snapshot.guide.asset_id}`;
        const src = buildViewUrl(snapshot.guideAsset.path);
        const image = getReadyImage(cacheKey, src, { rerenderOnLoad: true });
        if (!image) {
            return { blocked: true, details: playbackBlockDetails("guide-image-loading", null, snapshot) };
        }
        return { element: image };
    }

    function preflightPlaybackComposite(snapshot) {
        const renderablesByKey = new Map();
        const skippedLayers = [];
        const topDownLayers = [...(snapshot.playableClipLayers || [])].reverse();
        let coveredByUpper = false;
        let coveringLayer = null;

        for (const layer of topDownLayers) {
            const opacity = clamp(Number(layer.opacity ?? 1), 0, 1);
            if (opacity <= 0) {
                skippedLayers.push(playbackLayerDebug(layer, "transparent"));
                continue;
            }
            if (coveredByUpper) {
                skippedLayers.push(playbackLayerDebug(layer, "covered-by-upper", {
                    coveringLayerKey: coveringLayer?.layer?.key || "",
                    coveringSourcePath: coveringLayer?.layer?.clip?.source_path || "",
                }));
                continue;
            }

            let renderable = null;
            if (layer.asset?.asset_type === "image") {
                const src = buildViewUrl(layer.asset.path || layer.clip.source_path || "");
                const image = getReadyImage(`live-image:${layer.asset.asset_id || layer.key}`, src, { rerenderOnLoad: true });
                if (!image) {
                    return {
                        blocked: true,
                        details: playbackBlockDetails("image-loading", layer, snapshot, null, { skippedLayers }),
                    };
                }
                renderable = { type: "image", element: image, opacity, layer };
            } else {
                const active = state.activePlaybackVideos.get(layer.key);
                if (!isActiveVideoDrawable(active, layer, snapshot.frame)) {
                    if (
                        active?.firstDrawComplete
                        && isPlaybackTailFrame(layer, snapshot.frame)
                        && playbackCanvasStillValid()
                        && hasReadyNextPlaybackVideo(snapshot.frame)
                    ) {
                        return {
                            blocked: true,
                            suppressFallback: true,
                            details: playbackBlockDetails("tail-frame-hold", layer, snapshot, active, {
                                skippedLayers,
                                nextPlaybackFrame: nextPlaybackFrameAfter(snapshot.frame),
                                nextFramePrebufferReady: true,
                            }),
                        };
                    }
                    if (active) prepareActivePlaybackVideo(active, layer, snapshot.frame);
                    return {
                        blocked: true,
                        details: playbackBlockDetails("video-not-drawable", layer, snapshot, active, { skippedLayers }),
                    };
                }
                renderable = { type: "video", element: active.video, opacity, layer, active };
            }

            renderable.coversCanvas = playbackRenderableCoversCanvas(renderable);
            renderablesByKey.set(layer.key, renderable);
            if (renderable.coversCanvas) {
                coveredByUpper = true;
                coveringLayer = renderable;
            }
        }

        const renderables = [];
        if (!coveredByUpper) {
            const guide = playbackGuideRenderable(snapshot);
            if (guide.blocked) {
                return {
                    blocked: true,
                    details: { ...guide.details, skippedLayers },
                };
            }
            if (guide.element) {
                renderables.push({ type: "guide", element: guide.element, opacity: 1 });
            }
        } else if (snapshot?.guide && snapshot.guideAsset && !snapshot.guideAsset.missing) {
            skippedLayers.push({
                reason: "guide-covered-by-upper",
                coveringLayerKey: coveringLayer?.layer?.key || "",
                coveringSourcePath: coveringLayer?.layer?.clip?.source_path || "",
            });
        }

        for (const layer of snapshot.playableClipLayers || []) {
            const renderable = renderablesByKey.get(layer.key);
            if (renderable) renderables.push(renderable);
        }
        return { blocked: false, renderables, skippedLayers };
    }

    function commitPlaybackBlocked(snapshot, details, options = {}) {
        const now = performance.now();
        const signature = playbackLayerSignature(snapshot);
        const canHoldCanvas = playbackCanvasStillValid();
        if (state.playbackBlockedSignature !== signature) {
            state.playbackBlockedSignature = signature;
            state.playbackBlockedSinceMs = canHoldCanvas ? now : null;
        } else if (canHoldCanvas && state.playbackBlockedSinceMs === null) {
            state.playbackBlockedSinceMs = now;
        }

        if (options.suppressFallback && canHoldCanvas) {
            const blockedForMs = state.playbackBlockedSinceMs !== null ? now - state.playbackBlockedSinceMs : 0;
            debugPlaybackBoundary("hold-previous-composite", { ...details, blockedForMs, suppressedFallback: true });
            return false;
        }

        if (canHoldCanvas && state.playbackBlockedSinceMs !== null) {
            const blockedForMs = now - state.playbackBlockedSinceMs;
            if (blockedForMs < PLAYBACK_COMMIT_HOLD_MS) {
                debugPlaybackBoundary("hold-previous-composite", { ...details, blockedForMs });
                return false;
            }
        }

        drawViewportText("Loading preview...", "");
        resetPlaybackCompositeState();
        debugPlaybackBoundary("commit-loading-fallback", details);
        return false;
    }

    async function resolveRenderableLayer(layer, frame) {
        if (!layer?.asset) return null;
        if (layer.asset.asset_type === "image") {
            const src = buildViewUrl(layer.asset.path || layer.clip?.source_path || "");
            if (!src) return null;
            const img = await loadImage(`live-image:${layer.asset.asset_id || layer.key}`, src);
            return img ? { type: "image", element: img, opacity: layer.opacity } : null;
        }
        const video = getOrCreateVideo(layer);
        if (!video) return null;
        const element = await ensureMediaElementSource(video, layer.clip.source_path);
        if (!element) return null;
        await waitForMediaReady(element, 2);
        const sought = await seekMedia(element, clipSourceTime(layer, frame), {
            requireTarget: true,
            waitForFrame: true,
        });
        return sought ? { type: "video", element: sought, opacity: layer.opacity } : null;
    }

    async function renderLiveComposite(snapshot, renderToken) {
        const guideImage = await loadGuideLayer(snapshot);
        if (state.destroyed || state.isPlaying || renderToken !== state.renderToken) return;
        const renderableLayers = await Promise.all(
            snapshot.playableClipLayers.map((layer) => resolveRenderableLayer(layer, snapshot.frame))
        );
        if (state.destroyed || state.isPlaying || renderToken !== state.renderToken) return;
        drawBlack();
        let drewAny = false;
        if (guideImage) {
            drewAny = drawImageLike(guideImage, { opacity: 1 }) || drewAny;
        }
        for (const layer of renderableLayers) {
            if (!layer?.element) continue;
            if (drawImageLike(layer.element, { opacity: layer.opacity })) {
                drewAny = true;
            }
        }
        if (!drewAny) {
            drawViewportText("Loading preview...", "");
        }
    }

    function syncPlaybackMedia(snapshot) {
        const desiredVideoKeys = new Set();
        for (const layer of snapshot.playableClipLayers) {
            if (layer.asset?.asset_type === "image") continue;
            desiredVideoKeys.add(layer.key);
            if (!state.activePlaybackVideos.has(layer.key)) {
                const prebufferClaim = claimPrebufferedVideo(layer, snapshot.frame);
                const video = prebufferClaim?.video || getOrCreateVideo(layer);
                if (!video) continue;
                const active = createActivePlaybackVideoEntry(layer, video, prebufferClaim);
                state.activePlaybackVideos.set(layer.key, active);
                prepareActivePlaybackVideo(active, layer, snapshot.frame);
                continue;
            }
            const active = state.activePlaybackVideos.get(layer.key);
            const sourceChanged = active.layerKey !== layer.key || active.sourcePath !== layer.clip.source_path;
            active.layer = layer;
            if (sourceChanged) {
                active.layerKey = layer.key;
                active.sourcePath = layer.clip.source_path;
                active.readyForDraw = false;
                active.firstDrawComplete = false;
                prepareActivePlaybackVideo(active, layer, snapshot.frame, { force: true });
            } else if (isActiveVideoDrawable(active, layer, snapshot.frame)) {
                syncPreparedVideoPlayback(active, layer, snapshot.frame);
            } else if (shouldDeferPlaybackTailPrepare(active, layer, snapshot)) {
                active.requestedFrame = snapshot.frame;
                active.readyForDraw = false;
                debugPlaybackBoundary("defer-tail-prepare", playbackBlockDetails("tail-prepare-deferred", layer, snapshot, active, {
                    coveredByUpper: isLayerCoveredByDrawableUpperLayer(layer, snapshot),
                    nextFramePrebufferReady: hasReadyNextPlaybackVideo(snapshot.frame),
                    nextPlaybackFrame: nextPlaybackFrameAfter(snapshot.frame),
                }));
            } else {
                prepareActivePlaybackVideo(active, layer, snapshot.frame);
            }
        }
        for (const [key, active] of Array.from(state.activePlaybackVideos.entries())) {
            if (desiredVideoKeys.has(key)) continue;
            active.video.pause();
            active.readyForDraw = false;
            active.pendingPrepare = null;
            state.activePlaybackVideos.delete(key);
        }

        const desiredAudioKeys = new Set();
        for (const layer of snapshot.audioLayers) {
            desiredAudioKeys.add(layer.key);
            if (!state.activePlaybackAudios.has(layer.key)) {
                const audio = getOrCreateAudio(layer);
                if (!audio) continue;
                state.activePlaybackAudios.set(layer.key, { layer, audio });
                ensureMediaElementSource(audio, layer.track.source_path)
                    .then((element) => waitForMediaReady(element, 1))
                    .then((element) => {
                        if (!element || !state.isPlaying) return;
                        element.currentTime = audioSourceTime(layer, snapshot.frame);
                        element.volume = clamp(Number(layer.track.volume ?? 1), 0, 1);
                        element.play().catch(() => {});
                    })
                    .catch(() => {});
                continue;
            }
            const active = state.activePlaybackAudios.get(layer.key);
            active.layer = layer;
            const expectedTime = audioSourceTime(layer, snapshot.frame);
            active.audio.volume = clamp(Number(layer.track.volume ?? 1), 0, 1);
            if (Math.abs((Number(active.audio.currentTime) || 0) - expectedTime) > 0.35) {
                active.audio.currentTime = expectedTime;
            }
            if (active.audio.paused) {
                active.audio.play().catch(() => {});
            }
        }
        for (const [key, active] of Array.from(state.activePlaybackAudios.entries())) {
            if (desiredAudioKeys.has(key)) continue;
            active.audio.pause();
            state.activePlaybackAudios.delete(key);
        }
    }

    function drawPlaybackComposite(snapshot) {
        const preflight = preflightPlaybackComposite(snapshot);
        if (preflight.blocked) {
            commitPlaybackBlocked(snapshot, preflight.details, { suppressFallback: !!preflight.suppressFallback });
            return false;
        }

        drawBlack();
        let drewAny = false;
        for (const renderable of preflight.renderables || []) {
            const didDraw = drawImageLike(renderable.element, { opacity: renderable.opacity });
            if (renderable.type === "video" && renderable.active) {
                pauseTailPlaybackVideo(renderable.active, renderable.layer, snapshot.frame);
            }
            if (didDraw) {
                drewAny = true;
                if (renderable.type === "video" && renderable.active) {
                    renderable.active.firstDrawComplete = true;
                    renderable.active.readyForDraw = true;
                }
            }
        }
        state.playbackCompositeCommitted = true;
        state.playbackBlockedSinceMs = null;
        state.playbackBlockedSignature = "";
        state.playbackCanvasWidth = state.canvas?.width || 0;
        state.playbackCanvasHeight = state.canvas?.height || 0;
        state.playbackLastCommittedFrame = snapshot.frame;
        state.playbackLastCommittedSignature = playbackLayerSignature(snapshot);
        state.playbackLastCommittedSessionId = state.playbackSessionId;
        debugPlaybackBoundary("commit-playback-composite", {
            frame: snapshot.frame,
            drewAny,
            renderables: (preflight.renderables || []).map((entry) => ({
                type: entry.type,
                layerKey: entry.layer?.key || "",
                sourcePath: entry.layer?.clip?.source_path || "",
                opacity: entry.opacity,
                coversCanvas: !!entry.coversCanvas,
            })),
            skippedLayers: preflight.skippedLayers || [],
        });
        return drewAny;
    }

    function renderPlaybackFrame(snapshot) {
        if (shouldReuseCommittedPlaybackFrame(snapshot)) return true;
        syncPlaybackMedia(snapshot);
        schedulePlaybackPrebuffer(snapshot);
        return drawPlaybackComposite(snapshot);
    }

    function renderFrame() {
        if (state.destroyed || !state.canvas || !getCanvasContext()) return;
        notifyTransport();
        const width = state.canvas.width;
        const height = state.canvas.height;
        if (width <= 0 || height <= 0) return;
        const frame = currentFrame();
        const snapshot = buildFrameSnapshot(frame);
        const renderToken = ++state.renderToken;

        if (!snapshot.playableClipLayers.length && !snapshot.missingClipLayers.length && !snapshot.guide) {
            if (state.isPlaying) resetPlaybackCompositeState();
            drawViewportText(`Frame ${frame}`);
            return;
        }
        if (!snapshot.playableClipLayers.length && snapshot.missingClipLayers.length) {
            if (state.isPlaying) resetPlaybackCompositeState();
            const missingLayer = snapshot.missingClipLayers[0];
            const missingSourceName = typeof missingLayer.clip?.source_path === "string"
                ? missingLayer.clip.source_path.split(/[/\\]/).pop()
                : "";
            const missingName = missingLayer.asset?.name
                || missingSourceName
                || "Missing clip";
            drawViewportText("Missing clip", missingName, {
                titleColor: "#ffb18c",
                subtitleColor: "rgba(255,220,204,0.82)",
            });
            return;
        }
        if (state.isPlaying) {
            renderPlaybackFrame(snapshot);
            return;
        }
        if (!state.liveMediaEnabled) {
            renderStaticComposite(snapshot, renderToken).catch((error) => {
                console.warn("[Sonder] Static viewport preview failed:", error);
            });
            return;
        }
        renderLiveComposite(snapshot, renderToken).catch((error) => {
            console.warn("[Sonder] Live viewport render failed:", error);
        });
    }

    function restartPlaybackLoop(timestamp) {
        if (!state.playbackLoopRange) return;
        state.playbackSessionId += 1;
        resetPlaybackCompositeState();
        const nextFrame = applyFrame(state.playbackLoopRange.start, { reason: "playback-loop" });
        state.playbackStartTime = timestamp;
        state.playbackStartFrame = nextFrame;
        const snapshot = buildFrameSnapshot(nextFrame);
        invalidateAsyncPreviewRenders();
        renderPlaybackFrame(snapshot);
        state.playbackRAF = requestAnimationFrame(playbackTick);
    }

    function playbackTick(timestamp) {
        if (state.destroyed || !state.isPlaying) return;
        const elapsedSeconds = (timestamp - state.playbackStartTime) / 1000;
        const nextFrame = state.playbackStartFrame + Math.floor(elapsedSeconds * fps());
        const loopRange = state.playbackLoopRange;
        const endFrame = loopRange ? loopRange.end : totalFrames();
        if (nextFrame >= endFrame) {
            if (loopRange) {
                restartPlaybackLoop(timestamp);
                return;
            }
            applyFrame(totalFrames(), { reason: "playback-end" });
            stopPlayback();
            return;
        }
        applyFrame(nextFrame, { reason: "playback" });
        const snapshot = buildFrameSnapshot(nextFrame);
        invalidateAsyncPreviewRenders();
        renderPlaybackFrame(snapshot);
        state.playbackRAF = requestAnimationFrame(playbackTick);
    }

    function clearActivePlaybackMedia() {
        for (const active of state.activePlaybackVideos.values()) {
            active.video.pause();
            active.readyForDraw = false;
            active.pendingPrepare = null;
        }
        state.activePlaybackVideos.clear();
        for (const active of state.activePlaybackAudios.values()) {
            active.audio.pause();
        }
        state.activePlaybackAudios.clear();
    }

    function stopPlayback({ preservePlayhead = false } = {}) {
        if (state.playbackRAF) {
            cancelAnimationFrame(state.playbackRAF);
            state.playbackRAF = null;
        }
        if (!state.isPlaying) {
            notifyTransport();
            return;
        }
        updatePlaybackState(false);
        clearActivePlaybackMedia();
        clearPrebufferCache();
        resetPlaybackCompositeState();
        if (!preservePlayhead && shouldReturnToPlaybackStart()) {
            applyFrame(state.playbackSessionStartFrame, { reason: "playback-stop-return" });
        }
        state.playbackLoopRange = null;
        renderFrame();
    }

    function startPlayback() {
        if (state.destroyed || state.isPlaying) return;
        if (!state.liveMediaEnabled) {
            state.liveMediaEnabled = true;
        }
        const loopRange = getLoopRange();
        if (loopRange && (currentFrame() < loopRange.start || currentFrame() >= loopRange.end)) {
            applyFrame(loopRange.start, { reason: "playback-start-align" });
        }
        const startFrame = currentFrame();
        state.playbackStartTime = performance.now();
        state.playbackStartFrame = startFrame;
        state.playbackSessionStartFrame = startFrame;
        state.playbackLoopRange = loopRange;
        state.playbackSessionId += 1;
        invalidateAsyncPreviewRenders();
        resetPlaybackCompositeState();
        updatePlaybackState(true);
        const snapshot = buildFrameSnapshot(startFrame);
        renderPlaybackFrame(snapshot);
        state.playbackRAF = requestAnimationFrame(playbackTick);
    }

    function togglePlayback() {
        if (state.isPlaying) {
            stopPlayback();
        } else {
            startPlayback();
        }
    }

    function clearMediaCache() {
        clearActivePlaybackMedia();
        clearPrebufferCache();
        resetPlaybackCompositeState();
        for (const mediaEl of Object.values(state.videoCache)) {
            removeMediaSource(mediaEl);
        }
        for (const mediaEl of Object.values(state.audioCache)) {
            removeMediaSource(mediaEl);
        }
        for (const entry of state.sourceUrlCache.values()) {
            if (entry?.usesObjectUrl && entry.objectUrl) {
                URL.revokeObjectURL(entry.objectUrl);
            }
        }
        state.sourceUrlCache.clear();
        clearCacheObject(state.videoCache);
        clearCacheObject(state.audioCache);
        clearCacheObject(state.imageCache);
    }

    function setLiveMediaEnabled(nextValue) {
        const enabled = !!nextValue;
        if (state.liveMediaEnabled === enabled) {
            notifyTransport();
            return;
        }
        if (!enabled && state.isPlaying) {
            stopPlayback({ preservePlayhead: true });
        }
        state.liveMediaEnabled = enabled;
        notifyTransport();
        renderFrame();
    }

    function destroy() {
        if (state.destroyed) return;
        state.destroyed = true;
        if (state.playbackRAF) {
            cancelAnimationFrame(state.playbackRAF);
            state.playbackRAF = null;
        }
        updatePlaybackState(false);
        clearMediaCache();
    }

    notifyTransport();

    return {
        renderFrame,
        togglePlayback,
        startPlayback,
        stopPlayback,
        captureSourceFrame,
        clearMediaCache,
        destroy,
        setLiveMediaEnabled,
        isLiveMediaEnabled: () => state.liveMediaEnabled,
        isPlaying: () => state.isPlaying,
    };
}
