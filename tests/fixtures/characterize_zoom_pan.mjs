// Records what a zoom/pan implementation does for a fixed gesture script.
// Usage: node characterize_zoom_pan.mjs <module-url> <export-name>
// With "gallery-source" as the module url, the function is read from
// shared_asset_gallery.js as it stood before the media_zoom_pan.js extraction
// (the fixture was recorded that way); the test replays the module.
import { readFileSync } from "node:fs";

function fakeElement() {
    const listeners = new Map();
    return {
        style: {},
        listeners,
        addEventListener(type, fn) { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type).add(fn); },
        removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
        dispatch(type, event) { for (const fn of [...(listeners.get(type) || [])]) fn(event); },
        getBoundingClientRect: () => ({ left: 100, top: 50, width: 400, height: 300 }),
        contains: () => true,
    };
}

export async function characterize(attachZoomPan) {
    const windowListeners = fakeElement();
    globalThis.window = windowListeners;
    let now = 1000;
    globalThis.performance = { now: () => now };
    const surface = fakeElement();
    const target = fakeElement();
    const state = { zoomLevel: 1, panX: 0, panY: 0 };
    const out = [];
    const record = label => out.push({ label, zoom: +state.zoomLevel.toFixed(6), panX: +state.panX.toFixed(6), panY: +state.panY.toFixed(6),
        transform: target.style.transform, cursor: surface.style.cursor });
    const ev = extra => ({ preventDefault() {}, stopPropagation() {}, stopImmediatePropagation() {}, button: 0, ...extra });
    const cleanup = attachZoomPan(surface, target, { transformState: state });
    record("attached");
    surface.dispatch("wheel", ev({ deltaY: -1, clientX: 400, clientY: 120 }));
    record("zoom in at right");
    surface.dispatch("wheel", ev({ deltaY: -1, clientX: 150, clientY: 300 }));
    record("zoom in at left");
    surface.dispatch("mousedown", ev({ clientX: 200, clientY: 200 }));
    windowListeners.dispatch("mousemove", ev({ clientX: 202, clientY: 201 }));
    record("below drag threshold");
    windowListeners.dispatch("mousemove", ev({ clientX: 230, clientY: 240 }));
    windowListeners.dispatch("mousemove", ev({ clientX: 260, clientY: 250 }));
    record("dragged");
    windowListeners.dispatch("mouseup", ev({ clientX: 260, clientY: 250 }));
    record("released");
    surface.dispatch("wheel", ev({ deltaY: 1, clientX: 300, clientY: 200 }));
    record("zoom out");
    now += 100;
    surface.dispatch("dblclick", ev({}));
    record("dblclick right after drag (ignored)");
    now += 1000;
    surface.dispatch("dblclick", ev({}));
    record("dblclick reset");
    for (let i = 0; i < 40; i++) surface.dispatch("wheel", ev({ deltaY: -1, clientX: 300, clientY: 200 }));
    record("zoom clamps");
    cleanup();
    record("cleaned");
    out.push({ label: "listeners left", count: [...surface.listeners.values()].reduce((n, set) => n + set.size, 0) });
    return out;
}

async function load(url, name) {
    if (url !== "gallery-source") return (await import(url))[name];
    const source = readFileSync(new URL("../../web/js/shared_asset_gallery.js", import.meta.url), "utf8");
    const start = source.indexOf("    function attachZoomPan(surface, targets, options = {}) {");
    const end = source.indexOf("    function renderWaveformPanel(");
    const body = source.slice(start, end);
    const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
    const factory = new Function("state", "clamp", `${body}\nreturn attachZoomPan;`);
    return factory({ overlayState: {} }, clamp);
}

if (process.argv[2]) {
    const fn = await load(process.argv[2], process.argv[3] || "attachZoomPan");
    console.log(JSON.stringify(await characterize(fn), null, 1));
}
