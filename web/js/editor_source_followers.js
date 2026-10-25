// Editor sources in Output View panes: the scene's Guides and one Driver lane,
// painted onto a pane canvas at the scene's aspect. They read the editor's
// in-memory scene and media authorities and never change what renders: a
// hidden lane or a muted clip shows why, it is not shown anyway.
//
// Each painter is mounted by view_panes.js with a host adapter (scene, clock,
// lane visibility, assets, the surface's scene-source seam) and returns
// {update(), destroy()}; the pane owns the canvas element.
import { drawFitted, fitOptionsFor, guideAtFrame } from "./viewport_surface.js";
import { blockedState, displayVideoSource } from "./media_display_source.js";
import { createClockFollower } from "./clock_follower.js";

const isDriverClip = clip => clip?.role === "motion_driver";

function sizeCanvas(canvas, cell, aspect) {
    const rect = cell.getBoundingClientRect?.() || { width: 0, height: 0 };
    if (!(rect.width > 0 && rect.height > 0 && aspect > 0)) return false;
    let width = rect.width, height = rect.width / aspect;
    if (height > rect.height) { height = rect.height; width = height * aspect; }
    const w = Math.max(1, Math.floor(width)), h = Math.max(1, Math.floor(height));
    if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w;
        canvas.height = h;
        canvas.style.width = `${w}px`;
        canvas.style.height = `${h}px`;
    }
    return true;
}

const clear = canvas => {
    const ctx = canvas.getContext?.("2d");
    if (!ctx) return null;
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    return ctx;
};

const mediaSize = element => ({
    width: element.videoWidth || element.naturalWidth || element.width || 0,
    height: element.videoHeight || element.naturalHeight || element.height || 0,
});

// `onResize` when the pane cell changes size (a layout switch, a window
// resize); cleanup. No ResizeObserver (tests, old browsers): nothing to watch.
function watchCell(cell, onResize) {
    if (typeof ResizeObserver !== "function" || !cell) return () => {};
    const observer = new ResizeObserver(() => onResize());
    observer.observe(cell);
    return () => observer.disconnect();
}

// ── Guides ───────────────────────────────────────────────────────────────────

// The full-scene Guides animatic: the latest unmuted guide at the playhead,
// held to the scene end (the composite's own rule).
export function mountGuidesPainter({ canvas, cell, host, onStatus = () => {} }) {
    let destroyed = false;
    // What was last painted: a frame that shows the same guide at the same
    // size is not drawn again (playback emits every frame).
    let painted = "";
    const images = new Map();
    const image = (assetId, src) => {
        let entry = images.get(assetId);
        if (entry && entry.src === src) return entry.img;
        const img = new Image();
        img.decoding = "async";
        img.onload = () => { if (!destroyed) { painted = ""; update(); } };
        img.src = src;
        images.set(assetId, { img, src });
        if (images.size > 8) images.delete(images.keys().next().value);
        return img;
    };
    const blank = status => {
        const key = `status:${status}:${canvas.width}x${canvas.height}`;
        if (painted !== key) { clear(canvas); painted = key; }
        onStatus(status);
    };
    function update() {
        if (destroyed) return;
        const scene = host.getScene();
        sizeCanvas(canvas, cell, host.sceneAspect());
        if (!scene?.guide_frames?.length) { blank("No guides in this scene"); return; }
        if (host.isGuideTrackHidden()) { blank("Guides are hidden"); return; }
        const guide = guideAtFrame(scene.guide_frames, host.frame(), Math.max(0, host.totalFrames() - 1));
        const asset = guide ? host.getGuideAsset(guide) : null;
        if (!guide || !asset || asset.missing) { blank(guide ? "This guide's image is missing" : "No guide at this frame"); return; }
        onStatus(null);
        const src = host.buildViewUrl(asset.path);
        const img = image(guide.asset_id, src);
        const fit = fitOptionsFor(guide);
        const key = `guide:${guide.asset_id}:${src}:${fit.fitMode}:${fit.cropPosition}:${canvas.width}x${canvas.height}`;
        if (key === painted) return;
        const ctx = clear(canvas);
        painted = "";
        if (!ctx || !img.complete || !img.naturalWidth) return;
        const { width, height } = mediaSize(img);
        drawFitted(ctx, img, width, height, canvas.width, canvas.height, fit);
        painted = key;
    }
    const unsubscribe = host.clock.subscribe(() => update());
    const unwatch = watchCell(cell, () => update());
    update();
    return {
        update,
        destroy() {
            destroyed = true;
            unsubscribe();
            unwatch();
            for (const { img } of images.values()) { img.onload = null; img.removeAttribute?.("src"); }
            images.clear();
        },
    };
}

// ── Driver ───────────────────────────────────────────────────────────────────

// Why a Driver lane shows nothing at `frame`, or its clip.
export function driverAt(scene, index, frame, { isLaneHidden }) {
    const count = Math.max(0, Number(scene?.motion_driver_lane_count) || 0);
    if (index >= count) return { status: `No Driver lane ${index + 1} in this scene` };
    if (isLaneHidden(index)) return { status: `Driver lane ${index + 1} is hidden` };
    const clips = (scene?.clips || []).filter(clip => isDriverClip(clip) && (clip.track_index || 0) === index);
    const clip = clips.find(c => frame >= c.timeline_start_frame && frame < c.timeline_end_frame) || null;
    if (!clip) return { status: "No driver clip at this frame", clips };
    if (clip.muted) return { status: "Driver clip is muted", clip, clips };
    return { status: null, clip, clips };
}

// A Driver clip's timeline placement is a trusted mapping: frame centre c
// (scene frame + 0.5) -> source seconds, as the composite plays it (sourceFrameTime).
export function driverMapping(clip, sceneFps) {
    const start = Math.round(Number(clip?.timeline_start_frame) || 0);
    const end = Math.round(Number(clip?.timeline_end_frame) || 0);
    const sourceIn = Math.round(Number(clip?.source_in_frame) || 0);
    const rate = Number(sceneFps) > 0 ? Number(sceneFps) : 24;
    return c => (c - 0.5 >= start && c - 0.5 < end ? { inRange: true, sourceTime: Math.max(0, (c - start + sourceIn) / rate) } : { inRange: false });
}

// One Driver lane: its clip at the playhead, followed on the scene clock by a
// muted video of its own (the surface's scene-source seam, display source and
// proxies included), painted at the clip's fit.
export function mountDriverPainter({ canvas, cell, host, index, onStatus = () => {}, onFollowerState = () => {} }) {
    let destroyed = false;
    let current = null; // {clip, video, follower, source, key}
    let paintLoop = null;
    // Nothing is painted while the lane shows a message or the follower is
    // outside its clip: the video keeps its last frame, which is not this one.
    let showing = false;
    let followerState = "idle";
    const createVideo = () => {
        const video = document.createElement("video");
        video.muted = true;
        video.playsInline = true;
        video.preload = "auto";
        return video;
    };
    function teardown() {
        if (!current) return;
        current.listeners.abort();
        current.follower?.destroy();
        current.source?.release();
        try { current.video.removeAttribute("src"); current.video.load?.(); } catch (_error) { /* detached */ }
        current = null;
    }
    // Painted when there is something new: a seek landed, a frame decoded, the
    // follower or the cell changed, and every frame only while the video plays.
    function requestPaint() {
        if (paintLoop === null && !destroyed && current) paintLoop = requestAnimationFrame(paint);
    }
    function paint() {
        paintLoop = null;
        if (destroyed || !current) return;
        const { video, clip } = current;
        sizeCanvas(canvas, cell, host.sceneAspect());
        if (showing && followerState !== "outside" && followerState !== "unavailable" && (video.readyState || 0) >= 2 && video.videoWidth) {
            const ctx = clear(canvas);
            const { width, height } = mediaSize(video);
            if (ctx) drawFitted(ctx, video, width, height, canvas.width, canvas.height, fitOptionsFor(clip));
        }
        if (!video.paused) requestPaint();
    }
    function attach(clip) {
        const key = `${clip.clip_id || ""}|${clip.source_path}|${clip.timeline_start_frame}|${clip.timeline_end_frame}|${clip.source_in_frame || 0}`;
        if (current?.key === key) return;
        teardown();
        const video = createVideo();
        const entry = { clip, video, key, follower: null, source: null, listeners: new AbortController() };
        current = entry;
        for (const name of ["loadeddata", "seeked", "playing", "resize"]) {
            video.addEventListener(name, requestPaint, { signal: entry.listeners.signal });
        }
        const acquire = () => {
            entry.source = host.acquireSceneSource(clip.source_path, {
                holder: video,
                // Cleared, the scene switched or a proxy landed: acquire again.
                onRevoked: () => { if (!destroyed && current === entry) { entry.follower?.setUnavailable(true); queueMicrotask(() => { if (current === entry) acquire(); }); } },
            });
            entry.source.ready.then(result => {
                if (destroyed || current !== entry) return;
                if (!result) { onStatus("This driver clip can't play here"); entry.follower?.setUnavailable(true); return; }
                onStatus(null);
                entry.follower?.setUnavailable(false);
            });
        };
        entry.follower = createClockFollower({
            clock: host.clock, element: video, map: driverMapping(clip, host.fps()), fps: () => host.fps(),
            onState: state => {
                followerState = state;
                if (state === "outside" || state === "unavailable") clear(canvas);
                requestPaint();
                onFollowerState(state);
            },
        });
        acquire();
        requestPaint();
    }
    function update() {
        if (destroyed) return;
        const scene = host.getScene();
        const frame = host.frame();
        const found = driverAt(scene, index, frame, { isLaneHidden: host.isDriverLaneHidden });
        // Between clips the lane's nearest clip stays attached, so playback
        // into it needs no new load; outside every clip shows the message.
        const clip = found.clip && !found.clip.muted ? found.clip : null;
        showing = false;
        if (!clip) {
            sizeCanvas(canvas, cell, host.sceneAspect());
            clear(canvas);
            onStatus(found.status);
            if (found.status !== "No driver clip at this frame" || !found.clips?.length) teardown();
            return;
        }
        const asset = host.getAssetForSourcePath(clip.source_path);
        if (!asset || asset.missing) { teardown(); clear(canvas); onStatus("This driver clip's video is missing"); return; }
        const display = displayVideoSource(host.projectId(), asset);
        if (display.variant === "blocked") {
            teardown();
            clear(canvas);
            const blocked = blockedState(host.projectId(), asset);
            onStatus(`${blocked.title} ${blocked.detail}`);
            return;
        }
        attach(clip);
        showing = true;
        onStatus(null);
        requestPaint();
    }
    const unsubscribe = host.clock.subscribe(() => update());
    const unwatch = watchCell(cell, () => requestPaint());
    update();
    return {
        update,
        destroy() {
            destroyed = true;
            unsubscribe();
            unwatch();
            if (paintLoop !== null) cancelAnimationFrame(paintLoop);
            paintLoop = null;
            teardown();
        },
    };
}
