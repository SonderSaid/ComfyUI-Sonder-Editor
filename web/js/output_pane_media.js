// One output entry in an Output View pane: an <img>, <video> or <audio> with
// its own local transport (Play and the shared scrub bar), zoom and pan, and
// the conditions that keep it from showing (missing file, too large, can't be
// decoded). Media is muted unless the page's audio arbiter gives this pane the
// sound. The pane owns the container; this module owns the element, its
// listeners and its blob claim, and returns cleanup.
import { attachZoomPan } from "./media_zoom_pan.js";
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
export function mountOutputMedia({ container, entry, cache, paneId, audio, fit = "contain", zoom = null, onZoom = () => {},
    onCondition = () => {}, takeSaved = false }) {
    const player = entryPlayer(entry);
    const controller = new AbortController();
    let destroyed = false;
    let claim = null;
    let media = null;
    let scrub = null;
    let zoomCleanup = null;
    let unsubscribeAudio = null;
    const stage = make("div", "position:relative;flex:1 1 auto;min-height:0;display:flex;align-items:center;justify-content:center;overflow:hidden;background:#000;");
    const transport = make("div", `display:none;align-items:center;gap:${SPACE.s2}px;padding:${SPACE.s1}px ${SPACE.s2}px;`);
    container.append(stage, transport);

    const condition = value => { if (!destroyed) onCondition(value); };
    if (player === "unsupported") { condition("unsupported"); return { destroy() { stage.remove(); transport.remove(); }, setFit() {} }; }
    if (entry.available === false) { condition("missing_file"); return { destroy() { stage.remove(); transport.remove(); }, setFit() {} }; }

    const applyFit = value => {
        if (!media) return;
        media.style.objectFit = value === "cover" ? "cover" : "contain";
    };
    const state = { zoomLevel: zoom?.scale || 1, panX: zoom?.x || 0, panY: zoom?.y || 0 };
    const applyAudio = () => {
        if (!media || media.tagName === "IMG") return;
        media.muted = !audio.audible(paneId);
    };

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
        pause() { try { media?.pause?.(); } catch (_error) { /* detached */ } },
        destroy() {
            if (destroyed) return;
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

