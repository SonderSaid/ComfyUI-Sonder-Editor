// One output entry in an Output View pane: an <img>, <video> or <audio> with
// its own local transport (Play and the shared scrub bar), zoom and pan, and
// the conditions that keep it from showing (missing file, too large, can't be
// decoded). Media is muted unless the page's audio arbiter gives this pane the
// sound. The pane owns the container; this module owns the element, its
// listeners and its blob claim, and returns cleanup.
//
// Linked (Phase 3B, `setLink`): the editor's scene clock drives the element
// through a clock follower instead of the local transport, which hides; the
// element never loops, and outside the generated range it pauses and hides.
import { attachZoomPan } from "./media_zoom_pan.js";
import { createClockFollower } from "./clock_follower.js";
import { mountMediaScrubBar } from "./media_scrub_bar.js";
import { mediaFailedToDecode } from "./media_preview_support.js";
import { entryPlayer } from "./views_model.js";
import { EDITOR_COLORS as COLORS, FONT, SPACE, TYPE, chromeButtonCss } from "./editor_theme.js";

const make = (tag, css = "") => {
    const el = document.createElement(tag);
    if (css) el.style.cssText = css;
    return el;
};

function smallButton(text, title) {
    const button = make("button", chromeButtonCss({ padding: `2px ${SPACE.s2}px`, fontSize: `${TYPE.t11}px` }));
    button.type = "button";
    button.textContent = text;
    button.title = title;
    return button;
}

// A waveform of an audio blob, drawn once (decoded offline, never played).
async function drawWaveform(canvas, url, signal) {
    try {
        const buffer = await (await fetch(url, { signal })).arrayBuffer();
        const Offline = globalThis.OfflineAudioContext || globalThis.webkitOfflineAudioContext;
        if (!Offline || signal.aborted) return;
        const audio = await new Offline(1, 1, 44100).decodeAudioData(buffer);
        if (signal.aborted) return;
        const data = audio.getChannelData(0);
        const ctx = canvas.getContext("2d");
        const { width, height } = canvas;
        ctx.clearRect(0, 0, width, height);
        ctx.fillStyle = COLORS.textDim;
        const step = Math.max(1, Math.floor(data.length / width));
        for (let x = 0; x < width; x++) {
            let peak = 0;
            for (let i = x * step; i < Math.min(data.length, (x + 1) * step); i++) peak = Math.max(peak, Math.abs(data[i]));
            const bar = Math.max(1, peak * height);
            ctx.fillRect(x, (height - bar) / 2, 1, bar);
        }
    } catch (_error) {
        // A waveform is decoration; the transport still plays.
    }
}

// `entry` is a normalized output entry (server output_media). `zoom` is the
// pane's persisted {scale, x, y}|null; `onZoom` stores a change.
// Catching up shows only once it lasts: a quick recovery is not news.
export const CATCHING_UP_DELAY_MS = 250;

export function mountOutputMedia({ container, entry, cache, paneId, audio, fit = "contain", zoom = null, onZoom = () => {},
    onCondition = () => {}, takeSaved = false, clock = null, link = null, onDuration = () => {}, onFollowerState = () => {} }) {
    const player = entryPlayer(entry);
    const controller = new AbortController();
    let destroyed = false;
    let claim = null;
    let media = null;
    let scrub = null;
    let zoomCleanup = null;
    let unsubscribeAudio = null;
    let currentLink = link && clock ? link : null;
    let follower = null;
    let reportedState = null;
    let catchingTimer = null;
    const stage = make("div", "position:relative;flex:1 1 auto;min-height:0;display:flex;align-items:center;justify-content:center;overflow:hidden;background:#000;");
    const transport = make("div", `display:none;align-items:center;gap:${SPACE.s2}px;padding:${SPACE.s1}px ${SPACE.s2}px;`);
    container.append(stage, transport);

    // A file that failed to decode stops its follower's seeks (it would hold a
    // page seek slot to the timeout and make healthy panes wait).
    let failed = false;
    const condition = value => {
        if (destroyed) return;
        failed = value === "decode";
        follower?.setUnavailable(failed);
        onCondition(value);
    };
    const inert = { destroy() { stage.remove(); transport.remove(); }, setFit() {}, setLink() {}, pause() {} };
    if (player === "unsupported") { condition("unsupported"); return inert; }
    if (entry.available === false) { condition("missing_file"); return inert; }

    const applyFit = value => {
        if (!media) return;
        media.style.objectFit = value === "cover" ? "cover" : "contain";
    };
    const state = { zoomLevel: zoom?.scale || 1, panX: zoom?.x || 0, panY: zoom?.y || 0 };
    const applyAudio = () => {
        if (!media || media.tagName === "IMG") return;
        media.muted = !audio.audible(paneId);
    };

    const report = value => {
        if (destroyed || value === reportedState) return;
        reportedState = value;
        try { onFollowerState(value); } catch (error) { console.warn("[Sonder] pane follower state failed:", error); }
    };
    // Back from outside the range, the element still holds its last frame
    // (from before it left): it stays hidden until a seek lands a fresh one.
    let staleFrame = false;
    function followerState(value) {
        clearTimeout(catchingTimer);
        catchingTimer = null;
        if (value === "outside") staleFrame = true;
        else if (value === "following" || value === "idle") staleFrame = false;
        if (media) media.style.visibility = staleFrame ? "hidden" : "";
        if (value === "catching_up") catchingTimer = setTimeout(() => { catchingTimer = null; report("catching_up"); }, CATCHING_UP_DELAY_MS);
        else report(value);
    }
    // Follow the scene clock while linked; the local transport otherwise.
    function applyLink() {
        if (!media || media.tagName === "IMG" || destroyed) return;
        const linked = !!currentLink;
        media.loop = !linked;
        transport.style.display = linked || !scrub ? "none" : "flex";
        if (linked && !follower) {
            try { media.pause(); } catch (_error) { /* detached */ }
            follower = createClockFollower({
                clock, element: media, kind: player === "audio" ? "audio" : "video",
                // The link is read at each use: a new run's timing needs no new follower.
                map: c => (player === "audio" ? currentLink?.audioMap?.(c) : currentLink?.map?.(c)) || null,
                fps: () => currentLink?.Fm || Number(entry.fps) || 24,
                audible: () => audio.audible(paneId),
                onState: followerState,
            });
            if (failed) follower.setUnavailable(true);
            else follower.sync({ discontinuity: true });
        } else if (!linked && follower) {
            follower.destroy();
            follower = null;
            clearTimeout(catchingTimer);
            catchingTimer = null;
            staleFrame = false;
            media.style.visibility = "";
            try { media.pause(); } catch (_error) { /* detached */ }
            report(null);
        }
    }

    function mountPlayer(url) {
        if (player === "image" || player === "animated") {
            media = make("img", "max-width:100%;max-height:100%;width:100%;height:100%;user-select:none;");
            media.draggable = false;
            media.alt = "";
            media.addEventListener("error", () => condition("decode"), { signal: controller.signal });
            media.addEventListener("load", () => condition(null), { signal: controller.signal });
            media.src = url;
            stage.append(media);
        } else if (player === "video") {
            media = make("video", "width:100%;height:100%;");
            media.playsInline = true;
            media.preload = "auto";
            media.loop = true;
            media.addEventListener("error", () => condition("decode"), { signal: controller.signal });
            media.addEventListener("loadeddata", () => condition(mediaFailedToDecode(media) ? "decode" : null), { signal: controller.signal });
            media.addEventListener("loadedmetadata", () => onDuration(Number(media.duration)), { signal: controller.signal });
            media.src = url;
            stage.append(media);
        } else {
            const wave = make("canvas", "width:100%;height:60%;max-height:160px;");
            wave.width = 480;
            wave.height = 120;
            stage.append(wave);
            media = make("audio");
            media.preload = "auto";
            media.loop = true;
            media.addEventListener("error", () => condition("decode"), { signal: controller.signal });
            media.addEventListener("loadeddata", () => condition(null), { signal: controller.signal });
            media.addEventListener("loadedmetadata", () => onDuration(Number(media.duration)), { signal: controller.signal });
            media.src = url;
            void drawWaveform(wave, url, controller.signal);
        }
        applyFit(fit);
        applyAudio();
        unsubscribeAudio = audio.subscribe(applyAudio);
        if (player !== "audio") {
            zoomCleanup = attachZoomPan(stage, media, {
                transformState: state,
                onTransform: () => onZoom(state.zoomLevel === 1 && !state.panX && !state.panY ? null
                    : { scale: state.zoomLevel, x: state.panX, y: state.panY }),
            });
        }
        if (player === "video" || player === "audio") {
            const play = smallButton("Play", "Play or pause this output");
            const sync = () => { play.textContent = media.paused ? "Play" : "Pause"; };
            play.addEventListener("click", () => {
                if (media.paused) media.play?.().catch(() => {});
                else media.pause();
            }, { signal: controller.signal });
            for (const name of ["play", "pause", "ended"]) media.addEventListener(name, sync, { signal: controller.signal });
            scrub = mountMediaScrubBar(media, { fps: Number(entry.fps) || 0 });
            scrub.el.style.flex = "1 1 auto";
            transport.style.display = "flex";
            transport.append(play, scrub.el);
        }
        applyLink();
    }

    claim = cache.acquire(entry, paneId);
    claim.ready.then(result => {
        if (destroyed) return;
        if (result.error) { if (result.error !== "aborted") condition(result.error); return; }
        mountPlayer(result.url);
    });

    return {
        el: stage,
        setFit(value) { applyFit(value); },
        // A link resolved for this entry ({map, audioMap, Fm, sig}), or null to
        // play on the local transport. A changed mapping re-syncs the follower.
        setLink(next) {
            const value = next && clock ? next : null;
            const changed = (value?.sig || null) !== (currentLink?.sig || null);
            currentLink = value;
            applyLink();
            if (changed && follower) follower.sync({ discontinuity: true });
        },
        pause() { try { media?.pause?.(); } catch (_error) { /* detached */ } },
        destroy() {
            if (destroyed) return;
            follower?.destroy();
            follower = null;
            clearTimeout(catchingTimer);
            destroyed = true;
            controller.abort();
            unsubscribeAudio?.();
            zoomCleanup?.();
            scrub?.cleanup?.();
            if (media) {
                try { media.pause?.(); } catch (_error) { /* detached */ }
                media.removeAttribute("src");
                try { media.load?.(); } catch (_error) { /* detached */ }
            }
            claim?.release();
            stage.remove();
            transport.remove();
        },
    };
}

// The text a pane shows over (or instead of) its media.
export function messageEl(text) {
    const el = make("div", `position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;`
        + `padding:${SPACE.s3}px;color:${COLORS.textDim};font:${TYPE.t12}px ${FONT.sans};pointer-events:none;`);
    el.textContent = text;
    return el;
}

