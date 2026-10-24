"""Output Views 3A: the scene-clock follower's logic (clock_follower.js).

A fake clock, element, frame callbacks and animation frames drive the logic
only; the real decode and painted-frame accuracy are proven live (barcode
Driver clip, Phase 3A live check) and by the 3B fixture.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r"""
globalThis.window = {setTimeout, clearTimeout};
globalThis.document = {visibilityState: 'visible', hidden: false, querySelectorAll: () => []};
const F = await import(FOLLOWER_URL);
const out = {};
let now = 1000;
const frames = [];
const raf = cb => { frames.push(cb); return frames.length; };
const flushRaf = (n = 1) => { for (let i = 0; i < n; i++) { const batch = frames.splice(0); for (const cb of batch) cb(now); } };
const tick = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
const clockState = {frame: 0, playing: false, hold: null, fps: 24, totalFrames: 240, sessionId: 1, anchor: {startFrame: 0, startTimeMs: 1000}};
const listeners = new Set();
const clock = {get: () => ({...clockState}), subscribe: fn => { listeners.add(fn); return () => listeners.delete(fn); }};
const emit = (event = {}) => { for (const fn of [...listeners]) fn(event); };
class Video {
    constructor() { this._t = 0; this.paused = true; this.readyState = 4; this.playbackRate = 1; this.seeks = []; this.listeners = {}; this.callbacks = []; this.plays = 0; }
    get currentTime() { return this._t; }
    set currentTime(v) { this._t = v; this.seeks.push(Math.round(v * 1e4) / 1e4); queueMicrotask(() => (this.listeners.seeked || []).splice(0).forEach(fn => fn())); }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter(x => x !== fn); }
    pause() { this.paused = true; }
    async play() { this.paused = false; this.plays++; }
    requestVideoFrameCallback(cb) { this.callbacks.push(cb); return this.callbacks.length; }
    cancelVideoFrameCallback() { this.callbacks = []; }
    present(mediaTime) { const cbs = this.callbacks.splice(0); for (const cb of cbs) cb(now, {mediaTime, presentationTime: now}); }
}
const video = new Video();
// A clip from scene frame 10 to 100, source in at frame 5, at 24 fps.
const map = x => (x >= 10 && x < 100 ? {inRange: true, sourceTime: (x - 10 + 5) / 24} : {inRange: false});
const states = [];
const follower = F.createClockFollower({clock, element: video, map, fps: 24, onState: s => states.push(s),
    now: () => now, raf, cancelRaf: () => {}, waitFrame: async () => video, documentRef: globalThis.document,
    setTimer: (fn, ms) => setTimeout(fn, Math.min(ms, 5)), clearTimer: clearTimeout});
"""


def run(body: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    url = (ROOT / "web" / "js" / "clock_follower.js").as_uri()
    script = HARNESS.replace("FOLLOWER_URL", json.dumps(url)) + body + "\nconsole.log('RESULT=' + JSON.stringify(out));"
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(next(l for l in result.stdout.splitlines() if l.startswith("RESULT="))[7:])


def test_rate_for_drift_and_clock_frame():
    out = run("""
out.rates = [0, 0.9, -1, 1.5, -2.5, 3, 3.1, -4, NaN].map(F.rateForDrift);
out.frames = [F.clockFrameAt({...clockState, playing: false, frame: 7}, 5000),
    F.clockFrameAt({...clockState, playing: true, hold: 'rebuffer', frame: 7}, 5000),
    F.clockFrameAt({...clockState, playing: true, anchor: {startFrame: 10, startTimeMs: 1000}}, 1500)];
""")
    assert out["rates"][:3] == [1, 1, 1]
    assert out["rates"][3] == pytest.approx(0.955) and out["rates"][4] == pytest.approx(1.075)
    assert out["rates"][5] == pytest.approx(0.92) and out["rates"][6:] == [None, None, None]
    assert out["frames"] == [7, 7, 22]


def test_paused_seeks_are_exact_and_latest_wins_and_outside_pauses():
    out = run("""
clockState.frame = 20; emit({discontinuity: true});
clockState.frame = 21; emit({discontinuity: true});
clockState.frame = 30; emit({discontinuity: true});
await tick(); await new Promise(r => setTimeout(r, 20)); await tick();
out.seeks = video.seeks;
out.state = follower.state();
clockState.frame = 5; emit({discontinuity: true});
await tick();
out.outside = [follower.state(), video.paused];
""")
    # Frame centres: (f + 0.5 - 10 + 5) / 24. The queued middle seek is replaced.
    assert out["seeks"] == [round((20.5 - 5) / 24, 4), round((30.5 - 5) / 24, 4)]
    assert out["state"] == "idle"
    assert out["outside"] == ["outside", True]


def test_playing_follows_corrects_rate_hard_seeks_on_drift_and_stall_and_holds():
    out = run("""
clockState.playing = true; clockState.anchor = {startFrame: 20, startTimeMs: 1000};
emit({});
await tick(); await new Promise(r => setTimeout(r, 20)); await tick();
out.started = [video.paused, video.plays, video.seeks.length];
// On time: rate 1, following.
now = 1500;  // the clock is at frame 32
video.present((32 - 10 + 5) / 24 + 0.001);
out.onTime = [video.playbackRate, follower.state()];
// Two frames ahead: slow down a little.
video.present((32 - 10 + 5 + 2) / 24 + 0.001);
out.ahead = video.playbackRate;
// Five frames behind, a second after the start-up seek: catch up with a hard seek.
now = 2100;  // the clock is at frame 46.4
const before = video.seeks.length;
video.present((46 - 10 + 5 - 5) / 24 + 0.001);
await tick(); await new Promise(r => setTimeout(r, 20)); await tick();
out.drift = [video.seeks.length - before, states.includes('catching_up')];
// No new frame while the clock runs, twice within a second: one hard seek.
const stallBefore = video.seeks.length;
now += 1100; flushRaf(); await tick(); await new Promise(r => setTimeout(r, 20)); await tick();
now += 300; flushRaf(); await tick(); await new Promise(r => setTimeout(r, 20)); await tick();
out.stall = video.seeks.length - stallBefore;
// A discontinuity (a seek while playing) always seeks.
const jumpBefore = video.seeks.length;
emit({discontinuity: true});
await tick(); await new Promise(r => setTimeout(r, 20)); await tick();
out.jump = video.seeks.length - jumpBefore;
// The clock holds (rebuffer): pause on the hold frame.
clockState.hold = 'rebuffer'; clockState.frame = 40;
emit({});
await tick(); await new Promise(r => setTimeout(r, 20)); await tick();
out.hold = [video.paused, video.seeks.at(-1) === Math.round((40.5 - 5) / 24 * 1e4) / 1e4];
follower.destroy();
out.destroyed = listeners.size;
""")
    assert out["started"][0] is False and out["started"][1] >= 1 and out["started"][2] >= 1
    assert out["onTime"] == [1, "following"]
    assert out["ahead"] == pytest.approx(0.94)
    assert out["drift"][0] >= 1 and out["drift"][1]
    assert out["stall"] == 1
    assert out["jump"] >= 1
    assert out["hold"] == [True, True]
    assert out["destroyed"] == 0


def test_unavailable_and_visibility_return():
    out = run("""
follower.setUnavailable(true);
clockState.frame = 20; emit({discontinuity: true});
await tick();
out.unavailable = [follower.state(), video.seeks.length];
follower.setUnavailable(false);
await tick(); await new Promise(r => setTimeout(r, 20)); await tick();
out.restored = [video.seeks.length > 0];
""")
    assert out["unavailable"] == ["unavailable", 0]
    assert out["restored"] == [True]


def test_without_frame_callbacks_it_steers_by_current_time_and_seeks_only_on_a_real_stall():
    """A hidden page, or a browser that sends no frame callbacks for a detached element."""
    out = run("""
video.requestVideoFrameCallback = undefined;
clockState.playing = true; clockState.anchor = {startFrame: 20, startTimeMs: 1000};
emit({});
await tick(); await new Promise(r => setTimeout(r, 20)); await tick();
const seeksAfterStart = video.seeks.length;
// The element plays on time for two seconds: currentTime follows the clock.
for (let step = 1; step <= 20; step++) {
    now = 1000 + step * 100;
    video._t = (20 + step * 2.4 - 10 + 5) / 24;
    flushRaf(); await tick();
}
out.onTime = [video.seeks.length - seeksAfterStart, video.playbackRate, follower.state()];
// Two frames ahead: the rate eases it back.
now += 100; video._t += 0.1 + 2 / 24; flushRaf(); await tick();
out.ahead = video.playbackRate;
// currentTime stops moving: a stall, one hard seek.
const before = video.seeks.length;
now += 400; flushRaf(); await tick(); await new Promise(r => setTimeout(r, 20)); await tick();
out.stalled = video.seeks.length - before;
""")
    assert out["onTime"] == [0, 1, "following"]
    assert out["ahead"] == pytest.approx(0.94, abs=0.01)
    assert out["stalled"] == 1
