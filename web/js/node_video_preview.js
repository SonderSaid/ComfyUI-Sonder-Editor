// Shared inline video player for Sonder node cards (Save Video + Preview Video).
//
// Both nodes emit a uniform `ui.sonder_video = [{ filename, subfolder, type, fps,
// has_audio, poster? }]` descriptor on execute. This module turns that into a single
// `<video>` DOM widget on the node, served via ComfyUI's /view route.
//
// Native `<video controls>` does not work reliably inside a ComfyUI DOM widget — the
// browser's shadow-DOM scrubber never receives a usable drag — so we build our own
// control bar (play/pause + scrub + mute) and REUSE the proven custom scrubber from the
// dormant preview (`renderDormantMediaScrubBar`, which drives seeking via mousedown +
// window mousemove/up). Defaults: autoplay muted + loop (muted is required for browser
// autoplay; the user unmutes via the bar).

import { THEME, RADIUS } from "./editor_theme.js";
import { renderDormantMediaScrubBar } from "./editor_node_controller.js";

const MIN_HEIGHT = 110;
const MAX_HEIGHT = 760;
const DEFAULT_ASPECT = 16 / 9;
// Widget height beyond the video itself: control bar (~34) + container margin/border (~12).
const CONTROL_CHROME_PX = 46;

function comfyApi() {
    return window.comfyAPI?.api?.api || null;
}

function comfyApp() {
    return window.comfyAPI?.app?.app || null;
}

function viewUrl(descriptor) {
    if (!descriptor || !descriptor.filename) return "";
    const params = new URLSearchParams({
        filename: String(descriptor.filename),
        subfolder: String(descriptor.subfolder || ""),
        type: String(descriptor.type || "output"),
    });
    const path = `/view?${params.toString()}`;
    const api = comfyApi();
    return api?.apiURL ? api.apiURL(path) : path;
}

function videoAspect(node) {
    const a = Number(node._sonderVideoAspect);
    return a && isFinite(a) && a > 0 ? a : DEFAULT_ASPECT;
}

// Widget height tracks the node WIDTH by the video's aspect ratio (plus the control bar),
// so the media scales proportionally when the node is resized. computeSize(width) drives
// the DOM widget's height in this frontend.
function widgetHeightForWidth(node, width) {
    const w = Math.max(80, Number(width) || Number(node.size?.[0]) || 240);
    const h = w / videoAspect(node) + CONTROL_CHROME_PX;
    return Math.round(Math.max(MIN_HEIGHT, Math.min(MAX_HEIGHT, h)));
}

function sizeNodeToVideo(node) {
    try {
        const computed = node.computeSize?.();
        if (Array.isArray(computed) && computed.length >= 2) {
            const width = Number(node.size?.[0]) || Number(computed[0]) || 240;
            node.setSize?.([width, Number(computed[1]) || node.size?.[1]]);
        }
        comfyApp()?.graph?.setDirtyCanvas?.(true, true);
    } catch (_) {
        /* node sizing is best-effort */
    }
}

function controlButton(label) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    btn.style.cssText = `
        flex: 0 0 auto;
        width: 28px;
        height: 24px;
        display: flex;
        align-items: center;
        justify-content: center;
        cursor: pointer;
        padding: 0;
        font-size: 12px;
        line-height: 1;
        color: ${THEME.fg0};
        background: ${THEME.bg2};
        border: 1px solid ${THEME.line2};
        border-radius: ${RADIUS.r1}px;
    `;
    return btn;
}

function createWidget(node) {
    const container = document.createElement("div");
    container.style.cssText = `
        box-sizing: border-box;
        width: 100%;
        height: 100%;
        margin: 4px 0;
        border: 1px solid ${THEME.line2};
        border-radius: ${RADIUS.r2}px;
        background: ${THEME.bg0};
        overflow: hidden;
        display: flex;
        flex-direction: column;
    `;

    const video = document.createElement("video");
    video.loop = true;
    video.muted = true;
    video.autoplay = true;
    video.playsInline = true;
    video.preload = "metadata";
    // No native controls — we drive playback through the custom bar below.
    video.style.cssText = `
        flex: 1 1 auto;
        min-height: 0;
        width: 100%;
        display: block;
        background: ${THEME.bg0};
        object-fit: contain;
    `;

    // Custom control bar: play/pause + reused scrubber + mute toggle.
    const bar = document.createElement("div");
    bar.style.cssText = `
        flex: 0 0 auto;
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 4px 6px;
    `;
    const playBtn = controlButton("▶"); // ▶
    const muteBtn = controlButton("🔇"); // 🔇
    const scrub = renderDormantMediaScrubBar(video);
    scrub.el.style.flex = "1 1 auto";
    scrub.el.style.minWidth = "0";

    playBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (video.paused) {
            video.play?.().catch(() => {});
        } else {
            video.pause?.();
        }
    });
    muteBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        video.muted = !video.muted;
    });
    const syncPlay = () => { playBtn.textContent = video.paused ? "▶" : "⏸"; }; // ▶ / ⏸
    const syncMute = () => { muteBtn.textContent = video.muted ? "🔇" : "🔊"; }; // 🔇 / 🔊
    video.addEventListener("play", syncPlay);
    video.addEventListener("pause", syncPlay);
    video.addEventListener("volumechange", syncMute);
    syncPlay();
    syncMute();

    bar.append(playBtn, scrub.el, muteBtn);
    container.append(video, bar);

    // Keep clicks/drags on the player from being stolen by LiteGraph's canvas pointer
    // handling (which otherwise moves the node and breaks the controls).
    for (const evt of ["pointerdown", "mousedown"]) {
        container.addEventListener(evt, (e) => e.stopPropagation());
    }

    const widget = node.addDOMWidget("sonder_video", "SonderVideoPreview", container, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => MIN_HEIGHT,
        getMaxHeight: () => widgetHeightForWidth(node, node.size?.[0]),
        getHeight: () => widgetHeightForWidth(node, node.size?.[0]),
    });
    widget.computeSize = (width) => [width, widgetHeightForWidth(node, width)];
    widget._sonderVideoEl = video;
    widget._sonderScrubCleanup = scrub.cleanup;
    node._sonderVideoWidget = widget;

    // Lock the aspect once the real dimensions are known, then grow the node to fit.
    video.addEventListener("loadedmetadata", () => {
        const vw = video.videoWidth || 0;
        const vh = video.videoHeight || 0;
        if (vw > 0 && vh > 0) node._sonderVideoAspect = vw / vh;
        sizeNodeToVideo(node);
    });

    return widget;
}

/**
 * Mount (or update) the inline video player on a node from an emitted descriptor.
 * @param {object} node  The LiteGraph node instance (`this` inside onExecuted).
 * @param {object} descriptor  { filename, subfolder, type, fps, has_audio, poster? }
 */
export function mountNodeVideoPreview(node, descriptor) {
    if (!node || typeof node.addDOMWidget !== "function") return;
    if (!descriptor || !descriptor.filename) return;

    const src = viewUrl(descriptor);
    if (!src) return;

    let widget = node._sonderVideoWidget;
    if (!widget || !widget._sonderVideoEl?.isConnected) {
        widget = createWidget(node);
    }

    const video = widget._sonderVideoEl;
    if (!video) return;

    if (descriptor.poster?.filename) {
        video.poster = viewUrl(descriptor.poster);
    } else {
        video.removeAttribute("poster");
    }

    // Autoplay requires muted; keep loop on. The user toggles audio via the mute button.
    video.muted = true;
    video.loop = true;
    if (video.src !== src) {
        video.src = src;
    } else {
        try { video.currentTime = 0; } catch (_) { /* ignore */ }
    }

    const playPromise = video.play?.();
    if (playPromise?.catch) playPromise.catch(() => { /* autoplay can be deferred by the browser */ });
}
