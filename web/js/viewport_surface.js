function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
}

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

function seekMedia(mediaEl, targetTime, { tolerance = 0.02, timeoutMs = 250 } = {}) {
    if (!mediaEl) return Promise.resolve(null);
    return waitForMediaReady(mediaEl, 1).then((element) => {
        if (!element) return null;
        const duration = Number(element.duration);
        const safeTarget = Number.isFinite(duration) && duration > 0
            ? clamp(Number(targetTime) || 0, 0, Math.max(0, duration - 0.001))
            : Math.max(0, Number(targetTime) || 0);
        if ((element.readyState || 0) >= 2 && Math.abs((Number(element.currentTime) || 0) - safeTarget) <= tolerance) {
            return element;
        }
        return new Promise((resolve) => {
            let settled = false;
            const finish = () => {
                if (settled) return;
                settled = true;
                cleanup();
                resolve(element);
            };
            const cleanup = () => {
                window.clearTimeout(timer);
                for (const eventName of ["seeked", "loadeddata", "canplay", "timeupdate", "error"]) {
                    element.removeEventListener(eventName, finish);
                }
            };
            const timer = window.setTimeout(finish, timeoutMs);
            for (const eventName of ["seeked", "loadeddata", "canplay", "timeupdate", "error"]) {
                element.addEventListener(eventName, finish, { once: true });
            }
            try {
                element.currentTime = safeTarget;
            } catch (error) {
                finish();
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
        renderToken: 0,
        sourceUrlCache: new Map(),
        activePlaybackVideos: new Map(),
        activePlaybackAudios: new Map(),
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
    const isVideoLaneHidden = options.isVideoLaneHidden || (() => false);
    const isAudioLaneHidden = options.isAudioLaneHidden || (() => false);
    const buildViewUrl = options.buildViewUrl || (() => null);
    const buildThumbnailUrl = options.buildThumbnailUrl || (() => null);

    function currentFrame() {
        return clamp(Math.round(Number(getFrame()) || 0), 0, totalFrames());
    }

    function totalFrames() {
        return Math.max(0, Math.round(Number(getTotalFrames()) || 0));
    }

    function fps() {
        return Math.max(1, Number(getFps()) || 24);
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
            .filter((clip) => !isVideoLaneHidden(clip.track_index || 0))
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
                return entry.objectUrl;
            })
            .catch((error) => {
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

    function drawGuideLayerSync(snapshot) {
        if (!snapshot?.guide || !snapshot.guideAsset || snapshot.guideAsset.missing) return false;
        const cacheKey = `guide:${snapshot.guide.asset_id}`;
        const src = buildViewUrl(snapshot.guideAsset.path);
        const img = getReadyImage(cacheKey, src, { rerenderOnLoad: true });
        if (!img) return false;
        return drawImageLike(img, { opacity: 1 });
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
        if (state.destroyed || renderToken !== state.renderToken) return;
        if (guideImage) {
            sources.push({ element: guideImage, opacity: 1 });
        }
        for (const layer of snapshot.playableClipLayers) {
            const src = resolvePreviewImageUrl(layer);
            if (!src) continue;
            const cacheKey = `preview:${layer.asset?.asset_id || layer.key}`;
            const img = await loadImage(cacheKey, src);
            if (state.destroyed || renderToken !== state.renderToken) return;
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
        return Math.max(0, sourceFrame / fps());
    }

    function audioSourceTime(layer, frame) {
        const sourceFrame = frame - layer.track.timeline_start_frame + (layer.track.source_in_frame || 0);
        return Math.max(0, sourceFrame / fps());
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
        await seekMedia(element, clipSourceTime(layer, frame));
        return { type: "video", element, opacity: layer.opacity };
    }

    async function renderLiveComposite(snapshot, renderToken) {
        const guideImage = await loadGuideLayer(snapshot);
        if (state.destroyed || renderToken !== state.renderToken) return;
        const renderableLayers = await Promise.all(
            snapshot.playableClipLayers.map((layer) => resolveRenderableLayer(layer, snapshot.frame))
        );
        if (state.destroyed || renderToken !== state.renderToken) return;
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
                const video = getOrCreateVideo(layer);
                if (!video) continue;
                state.activePlaybackVideos.set(layer.key, { layer, video });
                ensureMediaElementSource(video, layer.clip.source_path)
                    .then((element) => waitForMediaReady(element, 2))
                    .then((element) => seekMedia(element, clipSourceTime(layer, snapshot.frame)))
                    .then((element) => {
                        if (!state.isPlaying || !element) return;
                        element.muted = true;
                        element.play().catch(() => {});
                        renderFrame();
                    })
                    .catch(() => {});
                continue;
            }
            const active = state.activePlaybackVideos.get(layer.key);
            active.layer = layer;
            const expectedTime = clipSourceTime(layer, snapshot.frame);
            if (Math.abs((Number(active.video.currentTime) || 0) - expectedTime) > 0.35) {
                active.video.currentTime = expectedTime;
            }
            if (active.video.paused) {
                active.video.play().catch(() => {});
            }
        }
        for (const [key, active] of Array.from(state.activePlaybackVideos.entries())) {
            if (desiredVideoKeys.has(key)) continue;
            active.video.pause();
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
        drawBlack();
        let drewAny = false;
        if (snapshot.guide && snapshot.guideAsset && !snapshot.guideAsset.missing) {
            drewAny = drawGuideLayerSync(snapshot) || drewAny;
        }
        for (const layer of snapshot.playableClipLayers) {
            if (layer.asset?.asset_type === "image") {
                const src = buildViewUrl(layer.asset.path || layer.clip.source_path || "");
                const image = getReadyImage(`live-image:${layer.asset.asset_id || layer.key}`, src, { rerenderOnLoad: true });
                if (!image) continue;
                drewAny = drawImageLike(image, { opacity: layer.opacity }) || drewAny;
                continue;
            }
            const active = state.activePlaybackVideos.get(layer.key);
            if (!active?.video || (active.video.readyState || 0) < 2) continue;
            drewAny = drawImageLike(active.video, { opacity: layer.opacity }) || drewAny;
        }
        if (!drewAny) {
            drawViewportText("Loading preview...", "");
        }
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
            drawViewportText(`Frame ${frame}`);
            return;
        }
        if (!snapshot.playableClipLayers.length && snapshot.missingClipLayers.length) {
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
            syncPlaybackMedia(snapshot);
            drawPlaybackComposite(snapshot);
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
        const nextFrame = applyFrame(state.playbackLoopRange.start, { reason: "playback-loop" });
        state.playbackStartTime = timestamp;
        state.playbackStartFrame = nextFrame;
        const snapshot = buildFrameSnapshot(nextFrame);
        syncPlaybackMedia(snapshot);
        drawPlaybackComposite(snapshot);
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
        syncPlaybackMedia(snapshot);
        drawPlaybackComposite(snapshot);
        state.playbackRAF = requestAnimationFrame(playbackTick);
    }

    function clearActivePlaybackMedia() {
        for (const active of state.activePlaybackVideos.values()) {
            active.video.pause();
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
        updatePlaybackState(true);
        const snapshot = buildFrameSnapshot(startFrame);
        syncPlaybackMedia(snapshot);
        drawPlaybackComposite(snapshot);
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
        clearMediaCache,
        destroy,
        setLiveMediaEnabled,
        isLiveMediaEnabled: () => state.liveMediaEnabled,
        isPlaying: () => state.isPlaying,
    };
}
