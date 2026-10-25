"""Output Views Phase 3B: the page's follower seek budget (aux_media_budget.js)
and the follower's use of it (clock_follower.js): two seeks in flight per page,
latest-wins per holder, none while the editor clock rebuffers, a watchdog for
a grant never given back, cancellation, and no leaked slot across random
acquire/cancel/destroy sequences. Also the follower's audio steering and its
per-attempt hard seeks.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "web" / "js"


def node_run(script: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert result.returncode == 0, result.stderr
    return json.loads(next(l for l in result.stdout.splitlines() if l.startswith("RESULT="))[7:])


HEAD = r"""
globalThis.window = {setTimeout, clearTimeout};
globalThis.document = {visibilityState: 'visible', hidden: false, querySelectorAll: () => []};
const B = await import(BUDGET_URL);
const out = {};
const timers = [];
const setTimer = (fn, ms) => { timers.push({fn, ms, live: true}); return timers.length; };
const clearTimer = id => { if (timers[id - 1]) timers[id - 1].live = false; };
const fire = ms => { for (const t of timers) if (t.live && t.ms === ms) { t.live = false; t.fn(); } };
const tick = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
let hold = null;
const listeners = new Set();
const clock = {get: () => ({hold}), subscribe: fn => { listeners.add(fn); return () => listeners.delete(fn); }};
const emit = () => { for (const fn of [...listeners]) fn({}); };
"""


def run(body: str):
    script = HEAD.replace("BUDGET_URL", json.dumps((JS / "aux_media_budget.js").as_uri()))
    return node_run(script + body + "\nconsole.log('RESULT=' + JSON.stringify(out));")


def test_two_in_flight_latest_wins_rebuffer_gate_watchdog_and_cancel():
    out = run("""
const budget = B.createAuxMediaBudget({setTimer, clearTimer});
const a = budget.request('a'), b = budget.request('b');
const c1 = budget.request('c'), c2 = budget.request('c');   // c's newer request replaces its queued one
const ra = await a.promise, rb = await b.promise;
out.first = [typeof ra, typeof rb, await c1.promise, budget.snapshot().inFlight, budget.snapshot().queued];
ra(); ra();   // idempotent
const rc = await c2.promise;
out.second = [typeof rc, budget.snapshot().inFlight];
// A rebuffering clock waits; its hold ending (a clock event) grants.
hold = 'rebuffer';
const d = budget.request('d', {clock});
rb();
await tick();
out.gated = [budget.snapshot().inFlight, budget.snapshot().queued];
hold = null; emit();
const rd = await d.promise;
out.afterHold = [typeof rd, listeners.size];
// The watchdog takes back a grant nobody released.
fire(B.WATCHDOG_MS);
out.watchdog = [budget.snapshot().inFlight, budget.snapshot().watchdog];
// Cancel: a queued request resolves null; a granted one is released.
const e = budget.request('e'), f = budget.request('f'), g = budget.request('g');
await e.promise; await f.promise;
g.cancel();
out.cancelQueued = [await g.promise, budget.snapshot().queued];
e.cancel(); f.cancel();
out.cancelGranted = budget.snapshot().inFlight;
""")
    assert out["first"] == ["function", "function", None, 2, 1]
    assert out["second"] == ["function", 2]
    assert out["gated"] == [1, 1]
    assert out["afterHold"] == ["function", 0]
    assert out["watchdog"][1] >= 1
    assert out["cancelQueued"] == [None, 0]
    assert out["cancelGranted"] == 0


def test_no_slot_leaks_across_random_sequences():
    out = run("""
let seed = 7;
const rand = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648;
const budget = B.createAuxMediaBudget({setTimer, clearTimer});
const live = [];
for (let step = 0; step < 1000; step++) {
    const r = rand();
    if (r < 0.4) {
        const handle = budget.request(`h${Math.floor(rand() * 6)}`, {clock: rand() < 0.3 ? clock : null});
        const entry = {handle, release: null};
        handle.promise.then(release => { entry.release = release; });
        live.push(entry);
    } else if (r < 0.6 && live.length) {
        live.splice(Math.floor(rand() * live.length), 1)[0].handle.cancel();
    } else if (r < 0.85 && live.length) {
        const entry = live[Math.floor(rand() * live.length)];
        entry.release?.();
    } else if (r < 0.92) {
        hold = hold ? null : 'rebuffer'; emit();
    } else fire(B.WATCHDOG_MS);
    await tick();
    const snap = budget.snapshot();
    if (snap.inFlight < 0 || snap.inFlight > B.MAX_IN_FLIGHT) { out.bad = [step, snap]; break; }
}
hold = null; emit();
for (const entry of live) entry.handle.cancel();
fire(B.WATCHDOG_MS);
await tick();
out.end = budget.snapshot();
""")
    assert "bad" not in out
    assert out["end"]["inFlight"] == 0 and out["end"]["queued"] == 0


FOLLOWER = r"""
globalThis.window = {setTimeout, clearTimeout};
globalThis.document = {visibilityState: 'visible', hidden: false, querySelectorAll: () => [], addEventListener() {}, removeEventListener() {}};
const F = await import(FOLLOWER_URL);
const B = await import(BUDGET_URL);
const out = {};
let now = 1000;
const frames = [];
const raf = cb => { frames.push(cb); return frames.length; };
const flushRaf = () => { const batch = frames.splice(0); for (const cb of batch) cb(now); };
const tick = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); await new Promise(r => setTimeout(r, 5)); };
const clockState = {frame: 0, playing: false, hold: null, fps: 24, totalFrames: 240, sessionId: 1, anchor: {startFrame: 0, startTimeMs: 1000}};
const listeners = new Set();
const clock = {get: () => ({...clockState}), subscribe: fn => { listeners.add(fn); return () => listeners.delete(fn); }};
const emit = (event = {}) => { for (const fn of [...listeners]) fn(event); };
class Media {
    constructor() { this._t = 0; this.paused = true; this.readyState = 4; this.playbackRate = 1; this.seeks = []; this.listeners = {}; this.rates = []; }
    get currentTime() { return this._t; }
    set currentTime(v) { this._t = v; this.seeks.push(Math.round(v * 1e4) / 1e4); queueMicrotask(() => (this.listeners.seeked || []).splice(0).forEach(fn => fn())); }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter(x => x !== fn); }
    pause() { this.paused = true; }
    async play() { this.paused = false; }
}
const map = c => (c >= 10.5 && c < 100.5 ? {inRange: true, sourceTime: (c - 10) / 24} : {inRange: false});
const budget = B.createAuxMediaBudget();
const make = (element, extra = {}) => F.createClockFollower({clock, element, map, fps: 24, budget, now: () => now, raf, cancelRaf: () => {},
    waitFrame: async () => element, documentRef: globalThis.document, setTimer: (fn, ms) => setTimeout(fn, Math.min(ms, 5)), clearTimer: clearTimeout, ...extra});
"""


def run_follower(body: str):
    script = (FOLLOWER.replace("FOLLOWER_URL", json.dumps((JS / "clock_follower.js").as_uri()))
              .replace("BUDGET_URL", json.dumps((JS / "aux_media_budget.js").as_uri())))
    return node_run(script + body + "\nconsole.log('RESULT=' + JSON.stringify(out));")


def test_audio_and_audible_video_steer_by_seek_never_by_rate():
    out = run_follower("""
const audio = new Media();
const follower = make(audio, {kind: 'audio'});
clockState.playing = true; clockState.anchor = {startFrame: 20, startTimeMs: 1000};
emit({});
await tick();
const start = audio.seeks.length;
// 50 ms behind: left alone at rate 1.
now = 1500; audio._t = (32 + 0.5 - 10) / 24 - 0.05; flushRaf(); await tick();
out.small = [audio.seeks.length - start, audio.playbackRate, follower.state()];
// 150 ms behind, a second after the start-up seek: one hard seek, never a rate change.
now = 2100; audio._t = (46.4 + 0.5 - 10) / 24 - 0.15; flushRaf(); await tick();
out.large = [audio.seeks.length - start, audio.playbackRate];
follower.destroy();
// A video whose sound is heard: same steering.
const video = new Media();
let heard = true;
const v = make(video, {audible: () => heard});
emit({});
await tick();
now = 2600; video._t = (58.4 + 0.5 - 10) / 24 + 2 / 24; flushRaf(); await tick();
out.heard = [video.playbackRate];
v.destroy();
out.listeners = listeners.size;
out.budget = budget.snapshot().inFlight;
""")
    assert out["small"] == [0, 1, "following"]
    assert out["large"] == [1, 1]
    assert out["heard"] == [1]
    assert out["listeners"] == 0 and out["budget"] == 0


def test_destroy_while_a_seek_waits_for_the_budget_releases_nothing_twice():
    out = run_follower("""
// Two other holders take both slots; the follower's paused seek queues.
const a = budget.request('x'), b = budget.request('y');
const ra = await a.promise, rb = await b.promise;
const video = new Media();
const follower = make(video);
clockState.frame = 30; emit({discontinuity: true});
await tick();
out.queued = [budget.snapshot().queued, video.seeks.length];
follower.destroy();
await tick();
out.afterDestroy = budget.snapshot().queued;
ra(); rb();
await tick();
out.end = [budget.snapshot().inFlight, video.seeks.length];
""")
    assert out["queued"] == [1, 0]
    assert out["afterDestroy"] == 0
    assert out["end"] == [0, 0]


def test_a_seek_finishing_after_the_playhead_left_the_range_stays_outside():
    """Found live (Phase 3B): a paused seek still landing when the playhead moved
    past the range reset the state to idle, and the pane showed again."""
    out = run_follower("""
const video = new Media();
const states = [];
let slow = null;
const follower = make(video, {onState: s => states.push(s), waitFrame: () => new Promise(r => { slow = r; })});
clockState.frame = 40; emit({discontinuity: true});
await tick();
clockState.frame = 41; emit({discontinuity: true});   // queued behind the first
clockState.frame = 120; emit({discontinuity: true});  // outside: drops the queued one
await tick();
slow?.(); await tick(); slow?.(); await tick();
out.state = follower.state();
out.seeks = video.seeks;
follower.destroy();
""")
    assert out["state"] == "outside"
    assert out["seeks"] == [round((40.5 - 10) / 24, 4)]


def test_audible_video_at_a_low_output_rate_keeps_a_frame_quantized_margin():
    """Audit (Phase 3B): an authored map steps at output-frame centres, so a
    heard 8 fps video sat up to 1/8 s off a fixed 0.08 s margin and hard-seeked
    every ~0.6 s. The margin for video is at least 1.5 source frames."""
    out = run_follower("""
const drifted = async (fps, drift) => {
    const video = new Media();
    const f = make(video, {fps, audible: () => true});
    clockState.playing = true; clockState.anchor = {startFrame: 20, startTimeMs: 1000};
    now = 1000; emit({});
    await tick();
    const start = video.seeks.length;
    now = 2600; video.paused = false;
    video._t = map(clockFrameCentre()).sourceTime + drift; flushRaf(); await tick();
    const seeks = video.seeks.length - start;
    f.destroy(); clockState.playing = false;
    return seeks;
};
const clockFrameCentre = () => F.clockFrameAt(clockState, now) + 0.5;
out.low = await drifted(8, 0.11);     // inside 1.5/8 s: left alone
out.lowFar = await drifted(8, 0.25);  // past it: one hard seek
out.high = await drifted(24, 0.11);   // 24 fps keeps the 0.08 s margin
""")
    assert out == {"low": 0, "lowFar": 1, "high": 1}


def test_a_failed_element_never_takes_a_page_seek_slot():
    """Audit (Phase 3B): a pane whose file failed to decode never fires
    `seeked`; its seeks held a page slot to the 1.5 s timeout."""
    out = run_follower("""
const broken = new Media();
broken.error = {code: 3};
const f = make(broken);
clockState.frame = 30; emit({discontinuity: true});
await tick();
out.errored = [budget.snapshot().granted, broken.seeks.length];
f.destroy();
const later = new Media();
const g = make(later);
g.setUnavailable(true);
clockState.frame = 31; emit({discontinuity: true});
await tick();
out.unavailable = [budget.snapshot().granted, later.seeks.length, g.state()];
g.destroy();
""")
    assert out["errored"] == [0, 0]
    assert out["unavailable"] == [0, 0, "unavailable"]
