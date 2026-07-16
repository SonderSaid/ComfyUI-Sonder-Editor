import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_node(script: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_graph_preview_ownership_is_reference_counted_and_diagnostics_intersect_hidden_playback():
    module_url = (ROOT / "web" / "js" / "graph_preview_ownership.js").as_uri()
    script = f"""
const {{
    _resetGraphPreviewOwnershipForTests: reset,
    acquireGraphPreviewSuppression: acquire,
    registerGraphPreviewDiagnostic: register,
    snapshotGraphPreviewDiagnostics: snapshot,
    subscribeGraphPreviewSuppression: subscribe,
}} = await import({json.dumps(module_url)});
reset();
const transitions = [];
const unsubscribe = subscribe((active) => transitions.push(active));
let state = {{
    role: "preview",
    playing: true,
    hidden: false,
    suspended: false,
    hibernated: false,
    suspensionReasons: [],
}};
const unregister = register(() => state);
const initial = snapshot();
const releaseA = acquire({{ id: "a" }});
const releaseB = acquire({{ id: "b" }});
state = {{ ...state, suspended: true, suspensionReasons: ["editor-owner"] }};
const owned = snapshot();
releaseA();
const afterOneRelease = snapshot();
releaseB();
state = {{ ...state, playing: false, suspended: false, hibernated: true, suspensionReasons: [] }};
const hibernated = snapshot();
unregister();
unsubscribe();
const cleaned = snapshot();
console.log(JSON.stringify({{ transitions, initial, owned, afterOneRelease, hibernated, cleaned }}));
"""
    result = _run_node(script)

    assert result["transitions"] == [False, True, False]
    assert result["initial"]["playingHidden"] == 0
    assert result["owned"]["playingHidden"] == 1
    assert result["owned"]["suspensionReasons"] == {"editor-owner": 1}
    assert result["afterOneRelease"]["playingHidden"] == 1
    assert result["hibernated"]["hibernated"] == 1
    assert result["cleaned"]["total"] == 0


def test_blob_loader_cleanup_aborts_fetch_without_direct_url_fallback():
    module_url = (ROOT / "web" / "js" / "shared_asset_gallery.js").as_uri()
    script = f"""
globalThis.window = {{ comfyAPI: {{ api: {{ api: {{}} }}, app: {{ app: {{}} }} }} }};
let aborted = false;
let assigned = [];
globalThis.fetch = (_url, options = {{}}) => new Promise((_resolve, reject) => {{
    options.signal?.addEventListener("abort", () => {{
        aborted = true;
        reject(new DOMException("aborted", "AbortError"));
    }}, {{ once: true }});
}});
const media = {{
    set src(value) {{ assigned.push(value); }},
}};
const {{ loadMediaAsBlob }} = await import({json.dumps(module_url)});
const handle = loadMediaAsBlob("/view?file=test.mp4", media, {{ mode: "blob" }});
handle.cleanup();
await new Promise((resolve) => setTimeout(resolve, 0));
console.log(JSON.stringify({{ aborted, assigned }}));
"""
    result = _run_node(script)
    assert result == {"aborted": True, "assigned": []}


def test_node_preview_lifecycle_suspends_hibernates_rehydrates_and_preserves_play_intent():
    preview_url = (ROOT / "web" / "js" / "node_video_preview.js").as_uri()
    ownership_url = (ROOT / "web" / "js" / "graph_preview_ownership.js").as_uri()
    script = f"""
class FakeElement extends EventTarget {{
    constructor(tag) {{
        super();
        this.tagName = tag.toUpperCase();
        this.style = {{}};
        this.dataset = {{}};
        this.children = [];
        this.attributes = new Map();
        this.parentNode = null;
        this.paused = true;
        this.ended = false;
        this.currentTime = 0;
        this.duration = 10;
        this.readyState = 2;
        this.videoWidth = 1920;
        this.videoHeight = 1080;
        this.loadCount = 0;
    }}
    append(...children) {{ for (const child of children) this.appendChild(child); }}
    appendChild(child) {{ this.children.push(child); child.parentNode = this; return child; }}
    remove() {{ this.parentNode = null; this.removed = true; }}
    setAttribute(name, value) {{ this.attributes.set(name, String(value)); }}
    getAttribute(name) {{ return this.attributes.get(name) ?? null; }}
    removeAttribute(name) {{ this.attributes.delete(name); if (name === "src") this._src = ""; }}
    getClientRects() {{ return [{{ width: 100, height: 100 }}]; }}
    getBoundingClientRect() {{ return {{ left: 0, width: 100 }}; }}
    load() {{ this.loadCount += 1; this.currentTime = 0; }}
    play() {{ this.paused = false; this.dispatchEvent(new Event("play")); return Promise.resolve(); }}
    pause() {{ this.paused = true; this.dispatchEvent(new Event("pause")); }}
    set src(value) {{ this._src = String(value); this.attributes.set("src", this._src); }}
    get src() {{ return this._src || ""; }}
    get currentSrc() {{ return this.src; }}
}}
class FakeDocument extends EventTarget {{
    constructor() {{ super(); this.visibilityState = "visible"; this.focused = true; }}
    createElement(tag) {{ return new FakeElement(tag); }}
    hasFocus() {{ return this.focused; }}
}}
class FakeWindow extends EventTarget {{}}
const document = new FakeDocument();
const window = new FakeWindow();
window.comfyAPI = {{
    api: {{ api: {{ apiURL: (path) => path }} }},
    app: {{ app: {{ graph: {{ setDirtyCanvas() {{}} }} }} }},
}};
window.setTimeout = setTimeout;
window.clearTimeout = clearTimeout;
globalThis.document = document;
globalThis.window = window;

let nextTimer = 1;
const timers = new Map();
globalThis.setTimeout = (callback, delay) => {{
    const id = nextTimer++;
    timers.set(id, {{ callback, delay }});
    return id;
}};
globalThis.clearTimeout = (id) => timers.delete(id);
window.setTimeout = globalThis.setTimeout;
window.clearTimeout = globalThis.clearTimeout;
const runTimer = (delay) => {{
    const match = Array.from(timers.entries()).find(([, timer]) => timer.delay === delay);
    if (!match) return false;
    timers.delete(match[0]);
    match[1].callback();
    return true;
}};

const observers = [];
globalThis.IntersectionObserver = class {{
    constructor(callback) {{ this.callback = callback; this.disconnected = false; observers.push(this); }}
    observe(target) {{ this.target = target; }}
    disconnect() {{ this.disconnected = true; }}
    setVisible(visible) {{ this.callback([{{ target: this.target, isIntersecting: visible, intersectionRatio: visible ? 1 : 0 }}]); }}
}};

let fetchCount = 0;
let revokeCount = 0;
globalThis.fetch = async () => {{ fetchCount += 1; return {{ blob: async () => new Blob(["video"]) }}; }};
const originalRevoke = URL.revokeObjectURL.bind(URL);
URL.revokeObjectURL = (url) => {{ revokeCount += 1; originalRevoke(url); }};

const ownership = await import({json.dumps(ownership_url)});
ownership._resetGraphPreviewOwnershipForTests();
const preview = await import({json.dumps(preview_url)});
const node = {{
    type: "SonderPreviewVideo",
    size: [320, 240],
    widgets: [{{ name: "autoplay_preview", value: true }}],
    addDOMWidget(name, type, element, options) {{
        const widget = {{ name, type, element, options }};
        this.widgets.push(widget);
        return widget;
    }},
    computeSize() {{ return [320, 240]; }},
    setSize(value) {{ this.size = value; }},
}};
const descriptor = {{ filename: "first.mp4", type: "output", has_audio: false }};
preview.mountNodeVideoPreview(node, descriptor);
for (let index = 0; index < 10; index += 1) await Promise.resolve();
const video = node._sonderVideoWidget._sonderVideoEl;
video.dispatchEvent(new Event("loadeddata"));
const initiallyPlaying = !video.paused;
video.currentTime = 4.2;

const releaseA = ownership.acquireGraphPreviewSuppression({{ id: "a" }});
const releaseB = ownership.acquireGraphPreviewSuppression({{ id: "b" }});
const pausedForOwner = video.paused;
releaseA();
const pausedWithSecondOwner = video.paused;
const hibernated = runTimer(preview.NODE_VIDEO_HIBERNATE_MS);
const afterHibernate = ownership.snapshotGraphPreviewDiagnostics();
releaseB();
for (let index = 0; index < 10; index += 1) await Promise.resolve();
video.dispatchEvent(new Event("loadeddata"));
const restoredTime = video.currentTime;
const resumedAfterOwners = !video.paused;

document.visibilityState = "hidden";
document.dispatchEvent(new Event("visibilitychange"));
const pausedWhenHidden = video.paused;
document.visibilityState = "visible";
document.dispatchEvent(new Event("visibilitychange"));
const resumedWhenVisible = !video.paused;
window.dispatchEvent(new Event("blur"));
const pausedWhenBlurred = video.paused;
window.dispatchEvent(new Event("focus"));
const resumedWhenFocused = !video.paused;

observers[0].setVisible(false);
const pausedOffscreen = video.paused;
observers[0].setVisible(true);
const resumedOnscreen = !video.paused;
video.dispatchEvent(new Event("click"));
const explicitlyPaused = video.paused;
const releaseC = ownership.acquireGraphPreviewSuppression({{ id: "c" }});
releaseC();
const stayedPaused = video.paused;

const releaseD = ownership.acquireGraphPreviewSuppression({{ id: "d" }});
const fetchesBeforeReplacement = fetchCount;
preview.mountNodeVideoPreview(node, {{ filename: "second.mp4", type: "output", has_audio: false }});
const replacementDeferred = fetchCount === fetchesBeforeReplacement;
releaseD();
for (let index = 0; index < 10; index += 1) await Promise.resolve();
video.dispatchEvent(new Event("loadeddata"));
const loadedLatest = node._sonderVideoWidget._sonderLoadedUrl.includes("second.mp4");

preview.unmountNodeVideoPreview(node, {{ resize: false }});
const afterCleanup = ownership.snapshotGraphPreviewDiagnostics();
console.log(JSON.stringify({{
    initiallyPlaying,
    pausedForOwner,
    pausedWithSecondOwner,
    hibernated,
    afterHibernate,
    revokeCount,
    restoredTime,
    resumedAfterOwners,
    pausedWhenHidden,
    resumedWhenVisible,
    pausedWhenBlurred,
    resumedWhenFocused,
    pausedOffscreen,
    resumedOnscreen,
    explicitlyPaused,
    stayedPaused,
    replacementDeferred,
    loadedLatest,
    fetchCount,
    afterCleanup,
    observerDisconnected: observers[0].disconnected,
    remainingTimers: timers.size,
}}));
"""
    result = _run_node(script)

    assert result["initiallyPlaying"] is True
    assert result["pausedForOwner"] is True
    assert result["pausedWithSecondOwner"] is True
    assert result["hibernated"] is True
    assert result["afterHibernate"]["hibernated"] == 1
    assert result["afterHibernate"]["playing"] == 0
    assert result["revokeCount"] >= 1
    assert abs(result["restoredTime"] - 4.2) <= 0.1
    assert result["resumedAfterOwners"] is True
    assert result["pausedWhenHidden"] is True
    assert result["resumedWhenVisible"] is True
    assert result["pausedWhenBlurred"] is True
    assert result["resumedWhenFocused"] is True
    assert result["pausedOffscreen"] is True
    assert result["resumedOnscreen"] is True
    assert result["explicitlyPaused"] is True
    assert result["stayedPaused"] is True
    assert result["replacementDeferred"] is True
    assert result["loadedLatest"] is True
    assert result["fetchCount"] == 3
    assert result["afterCleanup"]["total"] == 0
    assert result["observerDisconnected"] is True
    assert result["remainingTimers"] == 0


def test_playback_render_timing_summary_identifies_dominant_and_unattributed_work():
    module_url = (ROOT / "web" / "js" / "viewport_surface.js").as_uri()
    script = f"""
const {{ _summarizePlaybackRenderTiming: summarize }} = await import({json.dumps(module_url)});
console.log(JSON.stringify({{
    video: summarize({{
        syncVideoMs: 19,
        prebufferScheduleMs: 2,
        syncAudioMs: 1,
        compositePreflightMs: 3,
        compositeDrawMs: 5,
    }}, 32),
    unattributed: summarize({{ syncVideoMs: 2, compositeDrawMs: 3 }}, 40),
}}));
"""
    result = _run_node(script)
    assert result["video"]["dominantPhase"] == "syncVideoMs"
    assert result["video"]["unattributedMs"] == 2
    assert result["unattributed"]["dominantPhase"] == "unattributedMs"
    assert result["unattributed"]["unattributedMs"] == 35


def test_fake_clock_emits_slow_viewport_render_and_fixed_fifty_ms_raf_gap():
    module_url = (ROOT / "web" / "js" / "viewport_surface.js").as_uri()
    script = f"""
let now = 1000;
Object.defineProperty(globalThis, "performance", {{ value: {{ now: () => now }} }});
const events = [];
const raf = [];
globalThis.window = {{
    SONDER_DEBUG_SESSION: true,
    __SONDER_CANVAS_DIAG: {{ record: (kind, payload) => events.push({{ kind, ...payload }}) }},
    __SONDER_DIAG_CLEARERS: new Set(),
    setTimeout,
    clearTimeout,
}};
globalThis.document = {{
    visibilityState: "visible",
    hasFocus: () => true,
    querySelectorAll: () => [],
}};
globalThis.requestAnimationFrame = (callback) => {{ raf.push(callback); return raf.length; }};
globalThis.cancelAnimationFrame = () => {{}};
const ctx = new Proxy({{ globalAlpha: 1 }}, {{
    get: (target, key) => {{
        if (key === "fillRect") return () => {{ now += 30; }};
        return key in target ? target[key] : (() => {{}});
    }},
    set: (target, key, value) => {{ target[key] = value; return true; }},
}});
let frame = 0;
const {{ createViewportSurface }} = await import({json.dumps(module_url)});
const surface = createViewportSurface({{
    canvas: {{ width: 320, height: 180, getContext: () => ctx }},
    getScene: () => ({{ clips: [], audio_tracks: [], guide_frames: [] }}),
    getFrame: () => frame,
    setFrame: (value) => {{ frame = value; }},
    getTotalFrames: () => 48,
    getFps: () => 24,
}});
surface.startPlayback();
raf.shift()(1020);
raf.shift()(1075);
surface.stopPlayback();
const slow = events.find((event) => event.kind === "playback_slow_viewport_render");
const gap = events.find((event) => event.kind === "playback_raf_gap");
console.log(JSON.stringify({{
    slow: slow ? {{
        thresholdMs: slow.thresholdMs,
        totalMs: slow.timings.totalMs,
        dominantPhase: slow.timings.dominantPhase,
        unattributedMs: slow.timings.unattributedMs,
    }} : null,
    gap: gap ? {{
        gapMs: gap.gapMs,
        gapThresholdMs: gap.gapThresholdMs,
        sourceCacheEntries: gap.sourceCache?.entryCount,
        sourceFetchesInFlight: gap.sourceCache?.inFlightEntries,
    }} : null,
}}));
"""
    result = _run_node(script)
    assert result["slow"]["thresholdMs"] == 25
    assert result["slow"]["totalMs"] >= 30
    assert result["slow"]["dominantPhase"] in {"compositeDrawMs", "unattributedMs"}
    assert result["slow"]["unattributedMs"] >= 0
    assert result["gap"] == {
        "gapMs": 55,
        "gapThresholdMs": 50,
        "sourceCacheEntries": 0,
        "sourceFetchesInFlight": 0,
    }


def test_production_preview_and_playback_paths_use_the_new_lifecycle_seams():
    preview_source = (ROOT / "web" / "js" / "node_video_preview.js").read_text(encoding="utf-8")
    controller_source = (ROOT / "web" / "js" / "editor_node_controller.js").read_text(encoding="utf-8")
    viewport_source = (ROOT / "web" / "js" / "viewport_surface.js").read_text(encoding="utf-8")

    assert "subscribeGraphPreviewSuppression" in preview_source
    assert "new IntersectionObserver" in preview_source
    assert "NODE_VIDEO_HIBERNATE_MS = 30_000" in preview_source
    assert "widget._sonderSetSource?.(src)" in preview_source
    assert "acquireGraphPreviewSuppression(this)" in controller_source
    assert controller_source.count("this._releaseGraphPreviewSuppression();") >= 3
    assert 'viewportDiagRecord("playback_slow_viewport_render"' in viewport_source
    assert "const gapThresholdMs = 50;" in viewport_source
    assert "playingHidden" in viewport_source
