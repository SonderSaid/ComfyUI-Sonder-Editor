// A media element that follows the editor's scene clock (Output Views).
//
// The editor clock is authoritative and never waits for a follower. Paused or
// scrubbing, the follower makes a latest-wins exact seek and waits for the
// decoded frame. While the clock holds at play start (first commit) it pauses
// on the hold frame, so it is in place when playback begins; while it
// rebuffers, the page budget defers follower seeks until the hold ends.
// Playing, it plays at rate 1 and reads the presented frame from frame
// callbacks (the surface's painted-frame classifier): a drift of one source
// frame is left alone, up to three is corrected by the playback rate
// (0.92-1.08), and more, or no new frame for max(250 ms, 3 periods) while the
// clock advances, is Catching up: a hard seek to the clock's current time once
// the element can play, then play again. Hard seeks are at most one per
// HARD_SEEK_INTERVAL_MS, except to recover and after a discontinuity (a seek, step, scene switch,
// loop wrap, or the page becoming visible again).
//
// An audio element (`kind: "audio"`), or a video whose sound is heard
// (`audible()`), is steered by currentTime with no rate change (a rate change
// warbles): a drift past AUDIO_DRIFT_SEC is a hard seek. For video the margin
// is at least 1.5 source frames, since its mapping steps at frame centres.
//
// `map(c)` takes a frame centre (scene frame + 0.5, fractional while playing)
// and answers {inRange, sourceTime} (seconds) or null. Every seek goes through
// the page budget (aux_media_budget.js), one at a time per follower.
import { _classifyPresentedFrame, waitForDecodedVideoFrame } from "./viewport_surface.js";
import { pageBudget } from "./aux_media_budget.js";

export const STATES = Object.freeze(["idle", "seeking", "following", "catching_up", "outside", "unavailable"]);
export const DRIFT_IGNORE_FRAMES = 1;
export const DRIFT_RATE_FRAMES = 3;
export const RATE_MIN = 0.92;
export const RATE_MAX = 1.08;
export const STALL_MIN_MS = 250;
export const HARD_SEEK_INTERVAL_MS = 500;
export const AUDIO_DRIFT_SEC = 0.08;
const SEEK_TIMEOUT_MS = 1500;

// The playback rate for a measured drift (source frames, ahead positive), or
// null when the drift calls for a hard seek.
export function rateForDrift(drift) {
    const size = Math.abs(drift);
    if (!Number.isFinite(drift) || size > DRIFT_RATE_FRAMES) return null;
    if (size <= DRIFT_IGNORE_FRAMES) return 1;
    return Math.min(RATE_MAX, Math.max(RATE_MIN, 1 - drift * 0.03));
}

// The scene frame the clock is at now, fractional while it runs.
export function clockFrameAt(clock, nowMs) {
    if (!clock) return 0;
    if (!clock.playing || clock.hold || !Number.isFinite(clock.anchor?.startTimeMs)) return clock.frame;
    return clock.anchor.startFrame + Math.max(0, nowMs - clock.anchor.startTimeMs) / 1000 * clock.fps;
}

let followerSerial = 0;

export function createClockFollower({ clock, element, map, fps, onState = () => {}, kind = "video", audible = () => false,
    budget = pageBudget, now = () => performance.now(), raf = cb => requestAnimationFrame(cb), cancelRaf = id => cancelAnimationFrame(id),
    waitFrame = waitForDecodedVideoFrame, documentRef = globalThis.document, setTimer = setTimeout, clearTimer = clearTimeout } = {}) {
    if (!clock || !element || typeof map !== "function") throw new TypeError("A clock follower needs a clock, an element and a mapping.");
    const holder = `follower:${++followerSerial}`;
    let state = "idle";
    let destroyed = false;
    let loop = null;
    let frameCallback = null;
    let seekBusy = false;
    let pendingSeek = null;
    let wantOutside = false; // the latest paused request was outside the range
    let lastHardSeekAt = -Infinity;
    let lastPresentedAt = 0;
    let lastPresentedMedia = null;
    // currentTime at the last tick, and when it last moved: without frame
    // callbacks (a hidden page, or a browser that sends none for a detached
    // element) progress and drift are read from currentTime instead.
    let lastMediaTime = null;
    let lastProgressAt = 0;
    // The hard seek in progress, by attempt: a superseded one never clears a
    // newer one's state.
    let catching = 0;
    let attempts = 0;
    let unavailable = false;
    let slot = null; // the budget request in flight
    const isAudio = kind === "audio";
    // `fps` is the source's frame rate, a number or a getter.
    const sourceFps = () => {
        const value = Number(typeof fps === "function" ? fps() : fps);
        return value > 0 ? value : 24;
    };
    const steerBySeek = () => isAudio || !!audible();
    const set = next => {
        if (state === next || destroyed) return;
        state = next;
        try { onState(next); } catch (error) { console.warn("[Sonder] follower state listener failed:", error); }
    };
    const target = c => {
        const mapped = map(c);
        return mapped && mapped.inRange && Number.isFinite(mapped.sourceTime) ? mapped : null;
    };
    const pause = () => { try { element.pause?.(); } catch (_error) { /* detached */ } };
    const setRate = value => { try { element.playbackRate = value; } catch (_error) { /* detached */ } };

    // One seek through the page budget. False when superseded, timed out or gone.
    async function seekTo(time) {
        if (Math.abs((Number(element.currentTime) || 0) - time) < 1e-4 && (element.readyState || 0) >= 2) return true;
        // A failed element never fires `seeked`: it would hold a page slot to the timeout.
        if (unavailable || element.error) return false;
        slot?.cancel();
        const request = budget.request(holder, { clock });
        slot = request;
        const release = await request.promise;
        if (slot === request) slot = null;
        if (!release) return false;
        try {
            if (destroyed) return false;
            return await new Promise(resolve => {
                let timer = null;
                const done = ok => {
                    element.removeEventListener?.("seeked", onSeeked);
                    clearTimer(timer);
                    resolve(ok);
                };
                const onSeeked = () => done(true);
                element.addEventListener?.("seeked", onSeeked);
                timer = setTimer(() => done(false), SEEK_TIMEOUT_MS);
                try { element.currentTime = time; } catch (_error) { done(false); }
            });
        } finally {
            release();
        }
    }

    // Latest wins: a seek asked for while one runs replaces the queued one,
    // and a frame outside the range drops it (the seek in flight ends outside).
    async function exactSeek(frame) {
        const mapped = target(frame + 0.5);
        if (!mapped) {
            pendingSeek = null;
            wantOutside = true;
            pause();
            set("outside");
            return;
        }
        wantOutside = false;
        pendingSeek = mapped.sourceTime;
        if (seekBusy) return;
        seekBusy = true;
        try {
            while (pendingSeek !== null && !destroyed) {
                const time = pendingSeek;
                pendingSeek = null;
                set("seeking");
                pause();
                if (await seekTo(time) && !isAudio) await waitFrame(element, 200);
            }
        } finally {
            seekBusy = false;
        }
        if (!destroyed && !loop) set(unavailable ? "unavailable" : wantOutside ? "outside" : "idle");
    }

    function stopLoop() {
        if (loop !== null) cancelRaf(loop);
        loop = null;
        if (frameCallback !== null) element.cancelVideoFrameCallback?.(frameCallback);
        frameCallback = null;
        catching = 0;
        attempts += 1; // any hard seek in flight is superseded
        setRate(1);
    }

    function watchFrames() {
        if (isAudio || typeof element.requestVideoFrameCallback !== "function" || frameCallback !== null || destroyed) return;
        frameCallback = element.requestVideoFrameCallback((nowMs, metadata = {}) => {
            frameCallback = null;
            if (destroyed || loop === null) return;
            lastPresentedAt = now();
            lastPresentedMedia = Number(metadata.mediaTime);
            const c = clock.get();
            const expected = target(clockFrameAt(c, Number(metadata.presentationTime) || nowMs) + 0.5);
            if (expected && !catching && !steerBySeek()) {
                const rate = sourceFps();
                const sample = _classifyPresentedFrame({
                    presentedMediaTime: lastPresentedMedia, presentedAtMs: lastPresentedAt, nowMs: lastPresentedAt,
                    expectedSourceFrame: Math.floor(expected.sourceTime * rate + 1e-6), fps: rate, maxSampleAgeMs: 1000,
                });
                const next = sample.verified ? rateForDrift(sample.deltaFrames) : 1;
                if (next === null) void hardSeek("drift");
                else {
                    setRate(next);
                    set("following");
                }
            }
            watchFrames();
        });
    }

    async function hardSeek(reason) {
        const t = now();
        const forced = reason === "recovery" || reason === "discontinuity";
        if (catching || (!forced && t - lastHardSeekAt < HARD_SEEK_INTERVAL_MS)) return;
        const attempt = ++attempts;
        catching = attempt;
        const current = () => !destroyed && loop !== null && catching === attempt && attempts === attempt;
        lastHardSeekAt = t;
        set("catching_up");
        pause();
        if ((element.readyState || 0) < 3) {
            await new Promise(resolve => {
                const onCanPlay = () => { clearTimer(timer); resolve(); };
                const timer = setTimer(() => { element.removeEventListener?.("canplay", onCanPlay); resolve(); }, SEEK_TIMEOUT_MS);
                element.addEventListener?.("canplay", onCanPlay, { once: true });
            });
        }
        if (!current()) { if (catching === attempt) catching = 0; return; }
        // Aim where the clock will be once the seek lands.
        const mapped = target(clockFrameAt(clock.get(), now()) + 0.5);
        if (!mapped) { catching = 0; pause(); set("outside"); return; }
        await seekTo(mapped.sourceTime);
        if (!current()) { if (catching === attempt) catching = 0; return; }
        lastPresentedAt = lastProgressAt = now();
        catching = 0;
        setRate(1);
        try { await element.play?.(); } catch (_error) { /* the next tick retries */ }
        set("following");
        watchFrames();
    }

    function tick() {
        loop = null;
        if (destroyed) return;
        const c = clock.get();
        if (!c.playing || c.hold) { stopLoop(); void exactSeek(c.frame); return; }
        loop = raf(tick);
        const t = now();
        const mapped = target(clockFrameAt(c, t) + 0.5);
        if (!mapped) {
            if (state !== "outside") { pause(); set("outside"); }
            return;
        }
        if (state === "outside" || (element.paused && !catching)) { void hardSeek("recovery"); return; }
        const mediaTime = Number(element.currentTime) || 0;
        if (lastMediaTime === null || Math.abs(mediaTime - lastMediaTime) > 1e-6) lastProgressAt = t;
        lastMediaTime = mediaTime;
        if (catching) return;
        const stallMs = Math.max(STALL_MIN_MS, 3000 / sourceFps());
        const lastFrame = isAudio ? lastProgressAt : Math.max(lastPresentedAt, lastProgressAt);
        // Neither a new frame nor any progress while the clock runs: the decoder stalled.
        if (t - lastFrame > stallMs) { void hardSeek("stall"); return; }
        const drift = mediaTime - mapped.sourceTime;
        if (steerBySeek()) {
            const margin = isAudio ? AUDIO_DRIFT_SEC : Math.max(AUDIO_DRIFT_SEC, 1.5 / sourceFps());
            if (Math.abs(drift) > margin) void hardSeek("drift");
            else { setRate(1); set("following"); }
            return;
        }
        // No frame callbacks lately: steer by currentTime.
        if (t - lastPresentedAt > stallMs) {
            const next = rateForDrift(drift * sourceFps());
            if (next === null) void hardSeek("drift");
            else {
                setRate(next);
                set("following");
            }
        }
    }

    function follow() {
        if (loop !== null || destroyed) return;
        lastPresentedAt = lastProgressAt = now();
        lastMediaTime = null;
        loop = raf(tick);
        watchFrames();
        void hardSeek("recovery");
    }

    // The clock funnel's events: a frame (with `discontinuity` for seeks and
    // jumps) or a clock change (play, stop, hold).
    function sync({ discontinuity = false } = {}) {
        if (destroyed || unavailable) return;
        const c = clock.get();
        if (!c.playing || c.hold) {
            stopLoop();
            void exactSeek(c.frame);
            return;
        }
        if (loop === null) follow();
        else if (discontinuity) void hardSeek("discontinuity");
    }

    const unsubscribe = clock.subscribe?.(event => sync(event || {})) || (() => {});
    const onVisible = () => { if (documentRef?.visibilityState === "visible") sync({ discontinuity: true }); };
    documentRef?.addEventListener?.("visibilitychange", onVisible);

    return {
        state: () => state,
        sync,
        // The element lost its source (revoked, failed): silent until available.
        setUnavailable(value) {
            unavailable = !!value;
            if (unavailable) { stopLoop(); slot?.cancel(); pause(); set("unavailable"); } else { set("idle"); sync({ discontinuity: true }); }
        },
        destroy() {
            if (destroyed) return;
            stopLoop();
            destroyed = true;
            slot?.cancel();
            slot = null;
            unsubscribe();
            documentRef?.removeEventListener?.("visibilitychange", onVisible);
            pause();
        },
    };
}
