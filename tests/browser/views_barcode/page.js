// Output Views Phase 3B fixture: four panes at mixed rates on the REAL modules.
//
// A real viewport surface plays a 24 fps scene (a barcode composite clip and a
// timeline audio track). Three output panes (12, 25 and 30 fps barcode files)
// are mounted by the real output_pane_media.js with links from the real
// view_timing.js, followed by the real clock_follower.js through the real page
// budget, under the real audio arbiter. Everything is measured by reading
// pixels and audio; results land in window.__fixtureResult for run.mjs.
//
// Not a product frame-accuracy claim: fixture criteria only.

const JS = "/web/js/";
const MEDIA = "./media/";
const SCENE_FPS = 24;
const TOTAL = 120;
const RATES = [12, 25, 30];
const log = text => { document.getElementById("log").textContent += `${text}\n`; };

// The surface's timeline <audio> elements, as they are created.
const timelineAudio = [];
const realCreate = document.createElement.bind(document);
document.createElement = (tag, options) => {
    const el = realCreate(tag, options);
    if (String(tag).toLowerCase() === "audio") timelineAudio.push(el);
    return el;
};

const { createViewportSurface } = await import(`${JS}viewport_surface.js`);
const { mountOutputMedia } = await import(`${JS}output_pane_media.js`);
const { createOutputMediaCache } = await import(`${JS}output_media_cache.js`);
const { createAudioOwnership } = await import(`${JS}audio_ownership.js`);
const { resolveLink } = await import(`${JS}view_timing.js`);
const { clockFrameAt } = await import(`${JS}clock_follower.js`);
const { pageBudget } = await import(`${JS}aux_media_budget.js`);

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const nextFrame = () => new Promise(resolve => requestAnimationFrame(() => resolve()));

// ── The editor clock: the widget's funnel, reduced ───────────────────────────
let frame = 0;
const subscribers = new Set();
const CONTINUOUS = new Set(["playback", "playback-first-commit-hold", "playback-rebuffer-hold"]);
const emit = event => { for (const fn of [...subscribers]) { try { fn(event); } catch (error) { console.error(error); } } };
const holds = [];
function setPlayhead(value, reason = "seek") {
    const previous = frame;
    frame = value;
    const discontinuity = !CONTINUOUS.has(reason);
    if (previous === value && !discontinuity) return;
    emit({ type: "frame", frame: value, reason, discontinuity });
}

const scene = {
    scene_id: "s1", duration_frames: TOTAL, fps: SCENE_FPS,
    clips: [{ clip_id: "c", source_path: "composite.mp4", timeline_start_frame: 0, timeline_end_frame: TOTAL, source_in_frame: 0,
        source_out_frame: TOTAL, total_source_frames: TOTAL, track_index: 0, role: "render", opacity: 1, fit_mode: "fit", muted: false }],
    guide_frames: [],
    audio_tracks: [{ track_id: "a", source_path: "timeline.wav", timeline_start_frame: 0, timeline_end_frame: TOTAL, source_in_frame: 0,
        total_source_frames: TOTAL, lane_index: 0, volume: 1, muted: false }],
};
const assets = {
    "composite.mp4": { asset_id: "v", path: "composite.mp4", asset_type: "video", width: 320, height: 64 },
    "timeline.wav": { asset_id: "w", path: "timeline.wav", asset_type: "audio" },
};
const canvas = realCreate("canvas");
canvas.width = 320;
canvas.height = 120;
document.getElementById("composite").append(canvas);
const surface = createViewportSurface({
    canvas, initialLiveMediaEnabled: true,
    getScene: () => scene, getFrame: () => frame, setFrame: (value, meta = {}) => setPlayhead(value, meta.reason || "surface"),
    getTotalFrames: () => TOTAL, getFps: () => SCENE_FPS,
    getAssetForSourcePath: path => assets[path] || null, buildViewUrl: path => `${MEDIA}${path}`,
    onClockChange: clock => { if (clock.hold) holds.push([performance.now(), clock.hold]); emit({ type: "clock", clock }); },
});
const clock = { get: () => ({ ...surface.getClock(), frame }), subscribe: fn => { subscribers.add(fn); return () => subscribers.delete(fn); } };
const audio = createAudioOwnership({ setTimelineMuted: muted => surface.setTimelineAudioMuted(muted) });
const cache = createOutputMediaCache({ urlFor: file => `${MEDIA}${file.filename}` });
const context = { project_folder: "P", scene_id: "s1", fps: SCENE_FPS, render_start: 0, render_end: TOTAL, source_frame_count: TOTAL,
    generation_start: 0, generation_end: TOTAL };

// ── Panes ────────────────────────────────────────────────────────────────────
const panes = {};
for (const fps of RATES) {
    const entry = { kind: "video", file: { type: "output", filename: `out${fps}.mp4` }, fps, revision: "1" };
    const timing = { mode: "editor_window", anchor: "render_start", source_offset: 0, fps };
    const editor = { projectId: "P", sceneId: "s1", fps: SCENE_FPS, editorKey: "1" };
    const link = resolveLink({ entry, timing, contexts: { 1: context }, editor });
    link.sig = `l${fps}`;
    const states = [];
    const container = document.getElementById(`p${fps}`);
    const handle = mountOutputMedia({ container, entry, cache, paneId: `pane:${fps}`, audio, clock, link,
        onFollowerState: state => states.push([performance.now(), state]) });
    panes[fps] = { fps, link, states, container, handle, video: null };
}
for (let i = 0; i < 200; i++) {
    for (const pane of Object.values(panes)) pane.video ||= pane.container.querySelector("video");
    if (Object.values(panes).every(p => p.video && p.video.readyState >= 2)) break;
    await sleep(25);
}

// ── Pixel reads ──────────────────────────────────────────────────────────────
const scratch = realCreate("canvas");
scratch.width = 320;
scratch.height = 64;
const scratchCtx = scratch.getContext("2d", { willReadFrequently: true });
function decode(data, width, y) {
    let value = 0;
    for (let bit = 0; bit < 16; bit++) {
        const x = bit * 20 + 10;
        value = value * 2 + (data[(y * width + x) * 4] > 127 ? 1 : 0);
    }
    return value;
}
function readVideo(video) {
    scratchCtx.drawImage(video, 0, 0, 320, 64);
    return decode(scratchCtx.getImageData(0, 0, 320, 64).data, 320, 32);
}
const compositeCtx = canvas.getContext("2d", { willReadFrequently: true });
function readComposite() {
    // A 320x64 clip fitted into 320x120: rows 28..92.
    return decode(compositeCtx.getImageData(0, 0, 320, 120).data, 320, 60);
}
const expectedSource = (pane, f) => Math.floor((f + 0.5) * pane.fps / SCENE_FPS + 1e-9);

// ── 1. Paused: exact frames ──────────────────────────────────────────────────
let seed = 20261024;
const rand = n => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed % n; };
const paused = { frames: [], mismatches: {} };
for (const key of ["composite", ...RATES]) paused.mismatches[key] = [];
async function settle(read, expected, timeoutMs = 3000) {
    const end = performance.now() + timeoutMs;
    let value = read();
    while (value !== expected && performance.now() < end) {
        await nextFrame();
        value = read();
    }
    return value;
}
for (let i = 0; i < 32; i++) {
    const f = rand(TOTAL);
    paused.frames.push(f);
    setPlayhead(f, "seek");
    surface.renderFrame();
    const composite = await settle(readComposite, f);
    if (composite !== f) paused.mismatches.composite.push([f, composite]);
    for (const pane of Object.values(panes)) {
        const expected = expectedSource(pane, f);
        const got = await settle(() => readVideo(pane.video), expected);
        if (got !== expected) paused.mismatches[pane.fps].push([f, expected, got]);
    }
}
log(`paused: ${JSON.stringify(paused.mismatches)}`);

// ── 2. Playing: following, stalls, recovery ──────────────────────────────────
const slowest = Math.min(...RATES);
const bound = 2 / slowest;
function errorOf(pane, nowMs) {
    const c = surface.getClock();
    if (!c.playing || c.hold) return null;
    const f = clockFrameAt({ ...c, frame }, nowMs);
    const mapped = pane.link.map(f + 0.5);
    if (!mapped.inRange) return null;
    const s = readVideo(pane.video);
    return Math.abs((s + 0.5) / pane.fps - mapped.sourceTime);
}
async function sample(ms, into) {
    const end = performance.now() + ms;
    while (performance.now() < end) {
        const now = performance.now();
        for (const pane of Object.values(panes)) {
            const error = errorOf(pane, now);
            if (error !== null) into.push([now, pane.fps, error]);
        }
        await sleep(30);
    }
}
function recoveryMs(samples, fps, after) {
    const later = samples.filter(([t, f]) => f === fps && t >= after);
    for (let i = 0; i + 2 < later.length; i++) {
        if (later.slice(i, i + 3).every(([, , e]) => e <= bound)) return later[i][0] - after;
    }
    return Infinity;
}
const p95 = values => { const s = [...values].sort((a, b) => a - b); return s.length ? s[Math.floor(s.length * 0.95)] : null; };

async function playRun({ label, stalls }) {
    for (const pane of Object.values(panes)) pane.states.length = 0;
    setPlayhead(0, "seek");
    surface.startPlayback();
    const startAt = performance.now();
    const samples = [];
    await sample(1200, samples);
    const steady = Object.fromEntries(RATES.map(fps => [fps, p95(samples.filter(([t, f]) => f === fps && t - startAt > 600).map(s => s[2]))]));
    const result = { label, steady, stalls: [] };
    if (stalls) {
        const pane = panes[30];
        // Stall A: the decoder freezes, the element's time pinned for 220 ms.
        const pinnedAt = pane.video.currentTime;
        const freezeEnd = performance.now() + 220;
        while (performance.now() < freezeEnd) { pane.video.currentTime = pinnedAt; await sleep(16); }
        const afterA = performance.now();
        await sample(1500, samples);
        result.stalls.push({ kind: "frozen", recovery_ms: recoveryMs(samples, 30, afterA) });
        // Stall B: a seek that takes 220 ms to land, during an external pause.
        const proto = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, "currentTime");
        Object.defineProperty(pane.video, "currentTime", {
            configurable: true, get() { return proto.get.call(this); },
            set(value) { setTimeout(() => proto.set.call(this, value), 220); },
        });
        pane.video.pause();
        const startB = performance.now();
        await sleep(240);
        delete pane.video.currentTime;
        const afterB = performance.now();
        await sample(1500, samples);
        result.stalls.push({ kind: "slow_seek", recovery_ms: recoveryMs(samples, 30, afterB), from_injection_ms: afterB - startB });
    }
    result.catching_up_reports = Object.fromEntries(RATES.map(fps => [fps, panes[fps].states.filter(([, s]) => s === "catching_up").length]));
    result.start_catching_up = Object.fromEntries(RATES.map(fps => [fps, panes[fps].states.filter(([t, s]) => s === "catching_up" && t - startAt < 500).length]));
    surface.stopPlayback();
    await sleep(200);
    return result;
}
const holdsBefore = holds.length;
const playing = await playRun({ label: "composite visible", stalls: true });
playing.rebuffer_holds = holds.slice(holdsBefore).filter(([, h]) => h === "rebuffer").length;
log(`playing: ${JSON.stringify(playing)}`);

// ── 3. D2: the parked composite's cost to panes ──────────────────────────────
async function presentedRate(ms) {
    const counts = Object.fromEntries(RATES.map(fps => [fps, 0]));
    const watch = fps => {
        const video = panes[fps].video;
        const tick = () => { counts[fps] += 1; if (live) video.requestVideoFrameCallback(tick); };
        video.requestVideoFrameCallback(tick);
    };
    let live = true;
    RATES.forEach(watch);
    await sleep(ms);
    live = false;
    return Object.fromEntries(RATES.map(fps => [fps, counts[fps] / (ms / 1000)]));
}
async function rateWithComposite(parked) {
    const park = realCreate("div");
    park.style.cssText = "position:fixed;left:-100000px;top:0;";
    if (parked) { document.body.append(park); park.append(canvas); }
    setPlayhead(0, "seek");
    surface.startPlayback();
    await sleep(500);
    const rates = await presentedRate(2000);
    surface.stopPlayback();
    await sleep(200);
    if (parked) { document.getElementById("composite").append(canvas); park.remove(); }
    return rates;
}
const d2 = { visible: await rateWithComposite(false), parked: await rateWithComposite(true) };
log(`d2: ${JSON.stringify(d2)}`);

// ── 4. One audible source ────────────────────────────────────────────────────
const AudioCtx = window.AudioContext || window.webkitAudioContext;
const audioCtx = new AudioCtx();
await audioCtx.resume();
const sink = audioCtx.createGain();
sink.gain.value = 0;
sink.connect(audioCtx.destination);
const analysers = new Map();
function analyse(el, label) {
    if (analysers.has(el)) return;
    const source = audioCtx.createMediaElementSource(el);
    const analyser = audioCtx.createAnalyser();
    analyser.fftSize = 2048;
    source.connect(analyser);
    analyser.connect(sink);
    analysers.set(el, { label, analyser });
}
const rms = ({ analyser }) => {
    const data = new Float32Array(analyser.fftSize);
    analyser.getFloatTimeDomainData(data);
    return Math.sqrt(data.reduce((sum, v) => sum + v * v, 0) / data.length);
};
for (const pane of Object.values(panes)) analyse(pane.video, `pane:${pane.fps}`);
setPlayhead(0, "seek");
surface.startPlayback();
await sleep(600);
for (const el of timelineAudio) analyse(el, "timeline");
async function audible() {
    await sleep(400);
    const levels = {};
    for (const entry of analysers.values()) levels[entry.label] = Math.max(levels[entry.label] || 0, rms(entry));
    return { heard: Object.entries(levels).filter(([, v]) => v > 0.01).map(([k]) => k).sort(), levels };
}
// Calibration: a muted element reads silent, an unmuted one does not.
const calibration = {};
audio.claim("pane:25");
calibration.claimed = await audible();
const steps = [];
audio.release("pane:25");
steps.push(["timeline", await audible()]);
audio.claim("pane:12");
steps.push(["pane:12", await audible()]);
audio.claim("pane:30");
steps.push(["pane:30", await audible()]);
audio.release("pane:30");
steps.push(["timeline", await audible()]);
surface.stopPlayback();
log(`audio: ${JSON.stringify(steps.map(([want, got]) => [want, got.heard]))}`);

window.__fixtureResult = {
    paused, playing, d2, audio: { calibration, steps, timeline_elements: timelineAudio.length }, bound_s: bound,
    budget: pageBudget.snapshot(), userAgent: navigator.userAgent,
};
window.__fixtureDone = true;
