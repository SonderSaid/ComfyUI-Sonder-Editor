// Shared inline video player for Sonder node cards (Save Video + Preview Video).
//
// Both nodes emit a uniform `ui.sonder_video = [{ filename, subfolder, type, fps,
// has_audio, poster? }]` descriptor on execute. This module turns that into a single
// `<video>` DOM widget on the node. The media is fetched WHOLE as a blob (via the shared
// `loadMediaAsBlob`) rather than pointing `<video src>` at the raw /view URL — a directly
// streamed source reports a limited seekable range in this DOM-widget context, which clamps
// every scrub seek back to frame 0. Blob loading makes the file fully seekable, matching the
// editor's other playback surfaces (see durable_rules > Technical Traps).
//
// Native `<video controls>` does not work reliably inside a ComfyUI DOM widget — the
// browser's shadow-DOM scrubber never receives a usable drag — so we build our own
// transport (Play/Pause + scrub + Audio toggle) that REUSES the custom scrubber
// `renderDormantMediaScrubBar` (seeks via mousedown + window mousemove/up, and pauses
// playback during a drag so the seek sticks). The control bar mirrors the dormant
// preview transport's text-button style via the shared `buttonStyle`.
//
// Playback: the video opens PAUSED on its first frame. Clicking the video toggles
// play/pause. Autoplay is controlled by the node's own `autoplay_preview` BOOLEAN input
// (default off, declared in INPUT_TYPES like `embed_metadata`) — a first-class node widget
// that is present before any run and persists per-node via the workflow's `widgets_values`.
// The player reads that widget at mount time. Audio is muted by default (required for
// autoplay); the user unmutes via the Audio button.

import { THEME, RADIUS } from "./editor_theme.js";
import { renderDormantMediaScrubBar, buttonStyle } from "./editor_node_controller.js";
import { loadMediaAsBlob } from "./shared_asset_gallery.js";

const MIN_HEIGHT = 128;
const MAX_HEIGHT = 760;
const DEFAULT_ASPECT = 16 / 9;
// Widget height beyond the video itself: controls row + scrub row + container margin/border.
const CONTROL_CHROME_PX = 76;

// Autoplay is the node's `autoplay_preview` BOOLEAN input widget (persisted in the workflow
// via widgets_values, per-node). Read it from the live widget; default off when absent.
function getNodeAutoplay(node) {
    const widget = node?.widgets?.find?.((w) => w.name === "autoplay_preview");
    return widget ? widget.value === true : false;
}

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

// Text buttons matching the dormant preview transport. Re-callable so the Autoplay
// button can swap its variant (active/muted) when toggled.
function styleBarButton(btn, variant = "muted", minWidth = "") {
    btn.style.cssText = buttonStyle(variant, { padding: "5px 10px", fontSize: "10px" });
    btn.style.flex = "0 0 auto";
    if (minWidth) btn.style.minWidth = minWidth;
    return btn;
}

function barButton(label, variant = "muted", minWidth = "") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    styleBarButton(btn, variant, minWidth);
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
    video.autoplay = false;
    video.playsInline = true;
    // `auto` buffers a frame so the first frame paints while paused (no autoplay).
    video.preload = "auto";
    // No native controls — we drive playback through the custom bar below. Clicking the
    // video toggles play/pause.
    video.style.cssText = `
        flex: 1 1 auto;
        min-height: 0;
        width: 100%;
        display: block;
        background: ${THEME.bg0};
        object-fit: contain;
        cursor: pointer;
    `;

    // Control bar mirrors the dormant preview transport: a controls row (Play/Pause,
    // Audio, Autoplay) over a scrub row.
    const controlsRow = document.createElement("div");
    controlsRow.style.cssText = `
        flex: 0 0 auto;
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 4px 6px 0;
        flex-wrap: wrap;
    `;
    const scrubRow = document.createElement("div");
    scrubRow.style.cssText = `
        flex: 0 0 auto;
        display: flex;
        align-items: center;
        padding: 4px 6px;
    `;
    const playBtn = barButton("Play", "subtle", "48px");
    const audioBtn = barButton("Audio On", "muted", "64px");
    const scrub = renderDormantMediaScrubBar(video);
    scrub.el.style.flex = "1 1 auto";
    scrub.el.style.minWidth = "0";

    // Whether the current source carries an audio track — set from the descriptor on mount.
    let hasAudio = false;

    const togglePlay = () => {
        if (video.paused) {
            video.play?.().catch(() => { /* play can be deferred/blocked by the browser */ });
        } else {
            video.pause?.();
        }
    };
    const syncPlay = () => { playBtn.textContent = video.paused ? "Play" : "Pause"; };
    const syncMute = () => {
        if (!hasAudio) {
            audioBtn.textContent = "No audio";
            audioBtn.disabled = true;
            audioBtn.style.opacity = "0.55";
            audioBtn.style.cursor = "default";
            return;
        }
        audioBtn.disabled = false;
        audioBtn.style.opacity = "1";
        audioBtn.style.cursor = "pointer";
        audioBtn.textContent = video.muted ? "Muted" : "Audio On";
    };

    playBtn.addEventListener("click", (e) => { e.stopPropagation(); togglePlay(); });
    video.addEventListener("click", (e) => { e.stopPropagation(); togglePlay(); });
    audioBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (!hasAudio) return;
        video.muted = !video.muted;
    });
    // Apply the per-node autoplay choice once the media is actually decodable. Blob loading
    // is async, so we can't decide this synchronously at mount time — `loadeddata` (first
    // frame available) is the reliable moment.
    const applyAutoplay = () => {
        if (getNodeAutoplay(node)) {
            const p = video.play?.();
            if (p?.catch) p.catch(() => { /* play can be deferred/blocked by the browser */ });
        } else {
            try { video.pause?.(); } catch (_) { /* ignore */ }
        }
    };

    video.addEventListener("play", syncPlay);
    video.addEventListener("pause", syncPlay);
    video.addEventListener("volumechange", syncMute);
    video.addEventListener("loadeddata", applyAutoplay);
    syncPlay();
    syncMute();

    controlsRow.append(playBtn, audioBtn);
    scrubRow.append(scrub.el);
    container.append(video, controlsRow, scrubRow);

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
    widget._sonderSetHasAudio = (value) => { hasAudio = !!value; syncMute(); };
    widget._sonderApplyAutoplay = applyAutoplay;
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

    // Reflect this run's audio availability on the bar.
    widget._sonderSetHasAudio?.(descriptor.has_audio === true);

    if (descriptor.poster?.filename) {
        video.poster = viewUrl(descriptor.poster);
    } else {
        video.removeAttribute("poster");
    }

    // Muted is required for autoplay and is the default regardless; keep loop on. The user
    // toggles audio via the Audio button.
    video.muted = true;
    video.loop = true;

    // Load the WHOLE file as a blob (like every other editor playback surface) so the media
    // is fully seekable — a directly-streamed /view src reports a limited/empty seekable
    // range in this DOM-widget context, which clamps every scrub seek back to frame 0.
    // `_sonderLoadedUrl` tracks the source /view URL (not the resulting blob: URL, which
    // never equals `src`) so re-runs with the same file don't refetch.
    if (widget._sonderLoadedUrl !== src) {
        try { widget._sonderMediaCleanup?.(); } catch (_) { /* prior load cleanup is best-effort */ }
        const handle = loadMediaAsBlob(src, video, { mode: "blob" });
        widget._sonderMediaCleanup = handle?.cleanup || null;
        widget._sonderLoadedUrl = src;
        // Autoplay is applied on `loadeddata` once the blob is decodable (see createWidget).
    } else {
        try { video.currentTime = 0; } catch (_) { /* ignore */ }
        widget._sonderApplyAutoplay?.();
    }
}
