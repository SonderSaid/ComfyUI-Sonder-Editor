// A sampling-preview View's media (Output Views Phase 4): the newest preview a
// sampler published, as an <img> (JPEG, PNG, WebP) or a muted looping <video>
// (KJNodes' MP4). A new preview loads beside the shown one and replaces it once
// it can paint, so the pane never flashes black between steps; the old element
// goes and its object URL is revoked. At most one shown and one incoming
// element. Zoom and pan as in an output pane. The pane owns the container; this
// module owns the elements, their listeners and object URLs, and returns
// cleanup. The coordinator fetches the bytes (and aborts them).
import { attachZoomPan } from "./media_zoom_pan.js";

const IMAGE_MIMES = new Set(["image/jpeg", "image/png", "image/webp"]);
const VIDEO_MIMES = new Set(["video/mp4"]);

const make = (tag, css = "") => {
    const el = document.createElement(tag);
    if (css) el.style.cssText = css;
    return el;
};

// `frame` (see show) is {key, blob, mime}; `onCondition(null | "decode" | "unsupported")`.
export function mountSamplingMedia({ container, fit = "contain", zoom = null, onZoom = () => {}, onCondition = () => {},
    createObjectURL = blob => URL.createObjectURL(blob), revokeObjectURL = url => URL.revokeObjectURL(url) } = {}) {
    let destroyed = false;
    let shown = null; // {el, url, key, listeners}
    let incoming = null;
    let failedKey = null; // a preview that failed to decode is not tried again
    let fitValue = fit === "cover" ? "cover" : "contain";
    const stage = make("div", "position:relative;flex:1 1 auto;min-height:0;overflow:hidden;background:#000;");
    // The zoom/pan target: the elements inside it change, it does not.
    const layer = make("div", "position:absolute;inset:0;");
    stage.append(layer);
    container.append(stage);
    const state = { zoomLevel: zoom?.scale || 1, panX: zoom?.x || 0, panY: zoom?.y || 0 };
    const zoomCleanup = attachZoomPan(stage, layer, {
        transformState: state,
        onTransform: () => onZoom(state.zoomLevel === 1 && !state.panX && !state.panY ? null : { scale: state.zoomLevel, x: state.panX, y: state.panY }),
    });

    const report = value => { if (!destroyed) onCondition(value); };

    function drop(item) {
        if (!item) return;
        item.listeners.abort();
        const { el } = item;
        try { el.pause?.(); } catch (_error) { /* detached */ }
        el.removeAttribute("src");
        try { el.load?.(); } catch (_error) { /* detached */ }
        el.remove();
        revokeObjectURL(item.url);
    }

    function promote(item) {
        if (item !== incoming || destroyed) return;
        incoming = null;
        drop(shown);
        shown = item;
        item.el.style.visibility = "";
        report(null);
    }

    function show(frame) {
        if (destroyed) return;
        if (!frame?.blob) {
            drop(incoming);
            drop(shown);
            incoming = shown = null;
            return;
        }
        if (frame.key === (incoming || shown)?.key || frame.key === failedKey) return;
        const mime = String(frame.mime || "").toLowerCase();
        const video = VIDEO_MIMES.has(mime);
        if (!video && !IMAGE_MIMES.has(mime)) { report("unsupported"); return; }
        // A newer preview replaces one still loading: only the newest matters.
        drop(incoming);
        incoming = null;
        const el = make(video ? "video" : "img", `position:absolute;inset:0;width:100%;height:100%;object-fit:${fitValue};`
            + "visibility:hidden;user-select:none;");
        const item = { el, url: createObjectURL(frame.blob), key: frame.key, listeners: new AbortController() };
        const signal = item.listeners.signal;
        el.addEventListener(video ? "loadeddata" : "load", () => promote(item), { signal });
        el.addEventListener("error", () => {
            if (item !== incoming) return;
            incoming = null;
            failedKey = item.key;
            drop(item);
            report("decode");
        }, { signal });
        if (video) {
            el.muted = true;
            el.loop = true;
            el.autoplay = true;
            el.playsInline = true;
        } else {
            el.draggable = false;
            el.alt = "";
        }
        incoming = item;
        layer.append(el);
        el.src = item.url;
    }

    return {
        el: stage,
        show,
        setFit(value) {
            fitValue = value === "cover" ? "cover" : "contain";
            for (const item of [shown, incoming]) if (item) item.el.style.objectFit = fitValue;
        },
        destroy() {
            if (destroyed) return;
            destroyed = true;
            drop(incoming);
            drop(shown);
            incoming = shown = null;
            zoomCleanup();
            stage.remove();
        },
    };
}
