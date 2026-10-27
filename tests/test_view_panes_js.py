"""Output Views 3A in the browser modules: panes, their state, the hosts and the seams.

- characterization: drawFitted/guideAtFrame and the zoom/pan core replay the
  fixtures recorded from the code before the extraction;
- the surface's clock seam, timeline mute and scene-source revocation;
- the playhead funnel (every write path goes through it);
- the three viewsHost adapters on one fixture;
- the coordinator: reads, holds, keep-list, keepalive, pagehide, session loss,
  workflow switch, View creation, run outcomes;
- the pane DOM: layout control, picker, Composite swap and parking, teardown;
- the publication squeeze keeps Views; the output blob cache.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "web" / "js"
url = lambda name: json.dumps((JS / name).as_uri())


def node_run(script: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert result.returncode == 0, result.stderr
    return json.loads(next(l for l in result.stdout.splitlines() if l.startswith("RESULT="))[7:])


FAKE_DOM = r"""
const rafQueue = [];
globalThis.requestAnimationFrame = cb => { rafQueue.push(cb); return rafQueue.length; };
globalThis.cancelAnimationFrame = () => {};
const flushRaf = () => { for (let i = 0; i < 5 && rafQueue.length; i++) rafQueue.splice(0).forEach(cb => cb(performance.now())); };
const ctxProxy = new Proxy({}, {get: (t, k) => (k in t ? t[k] : () => {}), set: (t, k, v) => { t[k] = v; return true; }});
class El {
    constructor(tag) { this.tagName = String(tag).toUpperCase(); this.children = []; this.parentElement = null; this.style = {cssText: ''};
        this.dataset = {}; this.attributes = {}; this.listeners = {}; this._text = ''; this.width = 300; this.height = 150; }
    get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
    set textContent(v) { this.children = []; this._text = String(v); }
    append(...nodes) { for (const n of nodes) if (n && typeof n === 'object') this.appendChild(n); }
    appendChild(n) { n.parentElement?.removeChild(n); n.parentElement = this; this.children.push(n); return n; }
    removeChild(n) { const i = this.children.indexOf(n); if (i >= 0) this.children.splice(i, 1); n.parentElement = null; }
    insertBefore(n, ref) { n.parentElement?.removeChild(n); const i = ref ? this.children.indexOf(ref) : -1; n.parentElement = this;
        if (i < 0) this.children.push(n); else this.children.splice(i, 0, n); return n; }
    remove() { this.parentElement?.removeChild(this); }
    replaceChildren(...nodes) { for (const c of [...this.children]) this.removeChild(c); this.append(...nodes); }
    get childElementCount() { return this.children.length; }
    get isConnected() { let p = this; while (p.parentElement) p = p.parentElement; return p === document.body; }
    contains(n) { for (let p = n; p; p = p.parentElement) if (p === this) return true; return false; }
    setAttribute(k, v) { this.attributes[k] = String(v); } getAttribute(k) { return this.attributes[k] ?? null; } removeAttribute(k) { delete this.attributes[k]; }
    addEventListener(t, f) { (this.listeners[t] ||= []).push(f); } removeEventListener(t, f) { this.listeners[t] = (this.listeners[t] || []).filter(x => x !== f); }
    dispatch(t, extra = {}) { for (const f of [...(this.listeners[t] || [])]) f({target: this, preventDefault() {}, stopPropagation() {}, ...extra}); }
    click() { this.dispatch('click'); }
    getBoundingClientRect() { return {left: 0, top: 0, right: 400, bottom: 300, width: 400, height: 300}; }
    get offsetWidth() { return 200; } get offsetHeight() { return 100; }
    focus() { document.activeElement = this; } blur() {}
    getContext() { return ctxProxy; }
    find(pred) { if (pred(this)) return this; for (const c of this.children) { const f = c.find(pred); if (f) return f; } return null; }
    all(pred, acc = []) { if (pred(this)) acc.push(this); for (const c of this.children) c.all(pred, acc); return acc; }
}
const docListeners = {};
globalThis.document = {createElement: tag => new El(tag), body: new El('body'), activeElement: null, hidden: false, visibilityState: 'visible',
    addEventListener(t, f) { (docListeners[t] ||= []).push(f); }, removeEventListener(t, f) { docListeners[t] = (docListeners[t] || []).filter(x => x !== f); },
    querySelectorAll: () => []};
const winListeners = {};
globalThis.window = {innerWidth: 1200, innerHeight: 800, setTimeout, clearTimeout, localStorage: {getItem() { return null; }, setItem() {}},
    addEventListener(t, f) { (winListeners[t] ||= []).push(f); }, removeEventListener(t, f) { winListeners[t] = (winListeners[t] || []).filter(x => x !== f); }};
globalThis.localStorage = window.localStorage;
globalThis.Image = class extends El { constructor() { super('img'); this.complete = false; } };
"""


# ── Characterization ─────────────────────────────────────────────────────────

def test_draw_fitted_and_guide_at_frame_match_the_pre_extraction_fixture():
    sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
    from characterize_viewport_draw import record
    recorded = json.loads((ROOT / "tests" / "fixtures" / "viewport_draw_characterization.json").read_text(encoding="utf-8"))
    assert record((JS / "viewport_surface.js").read_text(encoding="utf-8")) == recorded
    source = (JS / "viewport_surface.js").read_text(encoding="utf-8")
    assert "drawFitted(ctx, source, width, height, canvasW, canvasH" in source  # the composite calls the extraction
    assert "return guideAtFrame(scene.guide_frames, frame" in source


def test_zoom_pan_core_matches_the_gallery_fixture_and_the_gallery_delegates():
    fixture = (ROOT / "tests" / "fixtures" / "media_zoom_pan_characterization.json").read_text(encoding="utf-8")
    harness = (ROOT / "tests" / "fixtures" / "characterize_zoom_pan.mjs").as_uri()
    out = node_run(f"""
const {{ characterize }} = await import({json.dumps(harness)});
const mod = await import({url("media_zoom_pan.js")});
console.log('RESULT=' + JSON.stringify(await characterize(mod.attachZoomPan)));
""")
    assert out == json.loads(fixture)
    gallery = (JS / "shared_asset_gallery.js").read_text(encoding="utf-8")
    assert "return attachZoomPanCore(surface, targets, { transformState, onTransform, onClick });" in gallery


# ── Surface seam ─────────────────────────────────────────────────────────────

def test_surface_clock_events_timeline_mute_and_scene_source_revocation():
    out = node_run(FAKE_DOM + f"""
const {{ createViewportSurface }} = await import({url("viewport_surface.js")});
const clocks = [];
let frame = 0;
const audios = [];
document.createElement = tag => {{ const el = new El(tag); if (tag === 'audio' || tag === 'video') {{
    Object.assign(el, {{readyState: 4, paused: true, currentTime: 0, play() {{ this.paused = false; return Promise.resolve(); }}, pause() {{ this.paused = true; }}, load() {{}}}});
    if (tag === 'audio') audios.push(el); }} return el; }};
globalThis.fetch = async () => ({{ ok: true, status: 200, headers: {{ get: () => null }}, blob: async () => new Blob(['x']) }});
const surface = createViewportSurface({{
    canvas: {{ width: 320, height: 180, getContext: () => ctxProxy }},
    getScene: () => ({{ clips: [], guide_frames: [],
        audio_tracks: [{{ track_id: 't1', source_path: 'a.wav', timeline_start_frame: 0, timeline_end_frame: 48, lane_index: 0 }}] }}),
    getFrame: () => frame, setFrame: v => {{ frame = v; }}, getTotalFrames: () => 48, getFps: () => 24,
    getAssetForSourcePath: path => ({{ asset_id: path, path, asset_type: path.endsWith('.wav') ? 'audio' : 'video' }}),
    buildViewUrl: path => 'view://' + path,
    onClockChange: clock => clocks.push([clock.playing, clock.hold, clock.sessionId]),
}});
const out = {{}};
out.idle = surface.getClock();
surface.setTimelineAudioMuted(true);
surface.startPlayback();
flushRaf();
out.playing = surface.getClock();
out.audioMuted = audios.map(a => a.muted);
surface.setTimelineAudioMuted(false);
out.unmuted = audios.map(a => a.muted);
surface.setTimelineAudioMuted(true);
surface.stopPlayback();
surface.startPlayback();  // a new session parks and reuses timeline audio
flushRaf();
out.again = audios.map(a => a.muted);
surface.stopPlayback();
out.clocks = clocks;
// A follower's own element: revoked when the surface clears its sources.
const holder = document.createElement('video');
let revoked = 0;
const source = surface.acquireSceneSource('clip.mp4', {{ holder, onRevoked: () => revoked++ }});
out.ready = !!(await source.ready);
surface.clearMediaCache();
out.revoked = revoked;
const second = surface.acquireSceneSource('clip.mp4', {{ holder, onRevoked: () => revoked++ }});
await second.ready;
second.release();  // its own release never reports a revocation
out.afterRelease = revoked;
console.log('RESULT=' + JSON.stringify(out));
""")
    assert out["idle"]["playing"] is False and out["idle"]["hold"] is None and out["idle"]["fps"] == 24
    assert out["playing"]["playing"] is True and out["playing"]["hold"] in ("first-commit", None)
    assert set(out["playing"]["anchor"]) == {"startFrame", "startTimeMs"}
    assert out["audioMuted"] and all(out["audioMuted"])
    assert not any(out["unmuted"])
    assert out["again"] and all(out["again"])
    # Play opens with the first-commit hold; stop reports not playing; a new session is new.
    playing = [c for c in out["clocks"] if c[0]]
    assert playing[0][1] == "first-commit"
    assert [c for c in out["clocks"] if not c[0]]
    assert len({c[2] for c in out["clocks"] if c[0]}) == 2
    assert out["ready"] and out["revoked"] == 1 and out["afterRelease"] == 1


# ── Playhead funnel ──────────────────────────────────────────────────────────

def test_every_playhead_write_goes_through_the_funnel():
    writes = []
    for path in JS.glob("*.js"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\.playhead\s*=[^=]", line) or re.search(r"\b_playhead\s*=[^=]", line):
                writes.append((path.name, number, line.strip()))
    # Only the funnel itself writes the backing field.
    assert writes == [("editor_widget.js", next(n for f, n, l in writes if "this._playhead = frame" in l), "this._playhead = frame;")], writes
    widget = (JS / "editor_widget.js").read_text(encoding="utf-8")
    reasons = re.findall(r"_setPlayhead\([^;]*?,\s*(\"[a-z-]+\"|meta\.reason \|\| \"surface\")\)", widget)
    assert len(reasons) >= 19 and all(reasons), reasons
    others = [(JS / name).read_text(encoding="utf-8") for name in ("editor_node_controller.js", "editor_top_chrome.js")]
    assert '_setPlayhead(state.selectionStart || 0, "open")' in others[0] and '"frame-input")' in others[1]
    # The surface's own reasons: only its ticks and holds are continuous.
    surface = (JS / "viewport_surface.js").read_text(encoding="utf-8")
    surface_reasons = set(re.findall(r'applyFrame\([^)]*reason: "([a-z-]+)"', surface))
    continuous = set(re.findall(r'"([a-z-]+)"', re.search(r"CONTINUOUS_PLAYHEAD_REASONS = new Set\(\[([^\]]*)\]", widget).group(1)))
    assert continuous == {"playback", "playback-first-commit-hold", "playback-rebuffer-hold"} and continuous <= surface_reasons
    # Behaviour: the accessor routes plain writes; subscribers hear discontinuities.
    start = widget.index("    get playhead() {")
    end = widget.index("    constructor(node, options = {}) {")
    out = node_run(f"""
const CONTINUOUS_PLAYHEAD_REASONS = new Set(["playback", "playback-first-commit-hold", "playback-rebuffer-hold"]);
class W {{
{widget[start:end]}
}}
const w = new W();
const heard = [];
w.playhead = 3;  // no subscribers yet: a plain write still lands
w._clockSubscribers = new Set([e => heard.push([e.frame, e.reason, e.discontinuity])]);
w._setPlayhead(4, "playback"); w._setPlayhead(4, "playback"); w._setPlayhead(9, "step"); w.playhead = 2;
console.log('RESULT=' + JSON.stringify({{ value: w.playhead, heard }}));
""")
    assert out == {"value": 2, "heard": [[4, "playback", False], [9, "step", True], [2, "external", True]]}


# ── Host adapters ────────────────────────────────────────────────────────────

def test_views_host_adapters_share_one_shape():
    out = node_run(FAKE_DOM + f"""
const H = await import({url("views_hosts.js")});
const calls = [];
const client = {{ cancel: async body => (calls.push(['cancel', body]), {{ ok: true }}) }};
const fetchImpl = async () => ({{ json: async () => ({{ branch: {{ ok: true, reason: '' }} }}) }});
const demand = [];
const controls = {{ setViewsDemand: on => demand.push(on) }};
const hosts = {{
    canvas: H.createCanvasViewsHost({{ controller: {{ node: {{ graph: {{ rootGraph: {{ id: 'wf' }} }} }}, runOutputs: client,
        runBranch: async path => (calls.push(['canvas', path]), {{ state: 'queued', prompt_id: 'p1' }}), getControlsHost: () => controls }},
        apiURL: p => p, fetchImpl }}),
    mounted: H.createMountedViewsHost({{ workflowId: 'wf', runOutputs: () => client, controlsHost: () => controls, apiURL: p => p, fetchImpl,
        runRequests: () => ({{ runBranch: async path => (calls.push(['mounted', path]), {{ state: 'queued', prompt_id: 'p1' }}) }}) }}),
    sdk: H.createSdkViewsHost({{ host: {{ templateId: 't1', status: {{ runs: [{{ run_id: 'run-1', prompt_id: 'p1', state: 'canceled' }}] }},
        runBranch: async path => (calls.push(['sdk', path]), {{ ok: true, status: 202, payload: {{ run_ids: ['run-1'] }} }}),
        getControlsHost: () => controls }}, runOutputs: () => client, apiURL: p => p, fetchImpl }}),
}};
const out = {{}};
for (const [mode, host] of Object.entries(hosts)) {{
    const outcome = await host.runBranch('12:7');
    const resolved = host.resolve(outcome);
    const cancel = await host.cancel(resolved);
    host.setViewsDemand(true);
    const events = [];
    host.onSessionLost(() => events.push('lost')); host.onSessionRestored(() => events.push('restored'));
    host.lost?.(); host.lost?.(); host.restored?.();
    out[mode] = {{ key: host.workflowKey(), capability: await host.capability(), outcome, resolved, cancel, events,
        keys: Object.keys(host).filter(k => typeof host[k] === 'function').sort() }};
}}
out.empty = H.createMountedViewsHost({{ workflowId: '', runOutputs: () => null, runRequests: () => null, controlsHost: () => null, apiURL: p => p }}).workflowKey();
// A Sonder App workflow switch: the same adapter now names the new template,
// and a run of the old one no longer resolves (its status left the session).
const sdkHost = hosts.sdk;
const app = {{ templateId: 't1', status: {{ runs: [{{ run_id: 'run-1', prompt_id: 'p1', state: 'queued' }}] }}, runBranch: async () => ({{}}) }};
const switching = H.createSdkViewsHost({{ host: app, runOutputs: () => client, apiURL: p => p, fetchImpl }});
out.switchKeys = [switching.workflowKey()];
app.templateId = 't2'; app.status = {{ runs: [] }};
out.switchKeys.push(switching.workflowKey());
out.switchResolve = switching.resolve({{ state: 'queued', runId: 'run-1' }});
out.calls = calls; out.demand = demand;
console.log('RESULT=' + JSON.stringify(out));
""")
    assert out["canvas"]["keys"] == out["mounted"]["keys"] == out["sdk"]["keys"]
    assert [out[m]["key"] for m in ("canvas", "mounted", "sdk")] == ["c:wf", "c:wf", "a:t1"] and out["empty"] == ""
    assert out["canvas"]["outcome"] == out["mounted"]["outcome"] == {"state": "queued", "promptId": "p1"}
    assert out["sdk"]["outcome"] == {"state": "queued", "runId": "run-1"}
    assert out["sdk"]["resolved"] == {"state": "queued", "runId": "run-1", "promptId": "p1", "finalState": "canceled"}
    assert all(out[m]["cancel"] == {"ok": True, "code": ""} for m in ("canvas", "mounted", "sdk"))
    assert all(out[m]["events"] == ["lost", "restored"] for m in ("canvas", "mounted", "sdk"))
    assert all(out[m]["capability"] == {"ok": True, "reason": ""} for m in ("canvas", "mounted", "sdk"))
    assert ["cancel", {"promptId": "p1"}] in out["calls"] and ["cancel", {"runId": "run-1"}] in out["calls"]
    assert out["demand"] == [True, True, True]
    assert out["switchKeys"] == ["a:t1", "a:t2"]
    assert out["switchResolve"] == {"state": "queued", "runId": "run-1"}


# ── Coordinator ──────────────────────────────────────────────────────────────

COORDINATOR = FAKE_DOM + r"""
const { createViewsCoordinator, KEEPALIVE_MS, POLL_MS } = await import(COORD_URL);
const { createAudioOwnership } = await import(AUDIO_URL);
const out = {};
const reads = [], holds = [], released = [], timers = [];
let answer = { ok: true, sources: {}, runs: [], holds: [] };
const client = {
    async readOutputs(sources, opts) { reads.push([sources, opts.keepHolds]); return JSON.parse(JSON.stringify(answer)); },
    async hold(body) { holds.push(body); return holds.length > 8 ? { ok: false, code: 'too_many' } : { ok: true, hold_id: `h${holds.length}` }; },
    async release(id) { released.push(id); return { ok: true }; },
    async releaseAll(ids, opts) { released.push([ids, !!opts.keepalive]); return { ok: true }; },
    onOutputsChanged(fn) { client.hint = fn; return () => { client.hint = null; }; },
};
let key = 'c:wf';
const demand = [], runs = [];
let lostCb = null, restoredCb = null;
const host = { mode: 'canvas', workflowKey: () => key, capability: async () => ({ ok: true }), runOutputs: () => client,
    runBranch: async path => (runs.push(path), { state: 'queued', promptId: 'p9' }), cancel: async () => ({ ok: true, code: '' }),
    resolve: o => o, setViewsDemand: on => demand.push(on), onSessionLost: cb => { lostCb = cb; return () => {}; },
    onSessionRestored: cb => { restoredCb = cb; return () => {}; } };
let controlsView = { state: 'ready', views: [{ view_id: 'a', status: 'ok', participation: 'active', path: '9', label: 'Preview' }],
    view_candidates: [{ path: '12:7', class_type: 'SaveVideo' }] };
const controlsListeners = new Set();
const applied = [];
const controls = { getView: () => controlsView, subscribe: fn => { controlsListeners.add(fn); return () => controlsListeners.delete(fn); },
    applyChanges: async changes => (applied.push(changes), { ok: true }), refresh: async () => {} };
const stored = new Map();
const layoutStore = { read: k => stored.get(k) || null, write: (k, r) => (r ? stored.set(k, JSON.parse(JSON.stringify(r))) : stored.delete(k)) };
const notes = [];
const audio = createAudioOwnership();
let clock = 1000;
const c = createViewsCoordinator({ host: () => host, controls, layoutStore, audio, notify: m => notes.push(m),
    onChange: () => globalThis.onCoordinatorChange?.(),
    now: () => clock, setTimer: (fn, ms) => (timers.push([ms, fn]), timers.length), clearTimer: () => {}, raf: fn => (fn(), 1),
    documentRef: document });
const settle = async () => { for (let i = 0; i < 6; i++) await new Promise(r => setTimeout(r, 0)); };
c.start();
"""


def run_coordinator(body: str):
    script = COORDINATOR.replace("COORD_URL", url("views_coordinator.js")).replace("AUDIO_URL", url("audio_ownership.js"))
    return node_run(script + body + "\nconsole.log('RESULT=' + JSON.stringify(out));")


def test_coordinator_reads_holds_keepalive_pagehide_and_layout():
    out = run_coordinator("""
c.setVisible(true); await settle();
out.firstReads = reads.length;  // no View pane yet: nothing to read
c.setLayout({count: 2}); c.assign(1, {kind: 'view', viewId: 'a'}); await settle();
out.demand = demand.slice();
out.read = reads.at(-1);
out.stored = stored.get('c:wf')?.count;
answer.sources = { '9': { state: 'ok', result: { prompt_id: 'p1', fresh: true, run: {}, entries: [{ index: 0, kind: 'image', file: { filename: 'a.png' } }] } } };
client.hint(); await settle();
let model = c.paneModel(1);
out.model = [model.chip, model.canHold, model.entry?.file?.filename, model.runnable?.enabled];
// The server lists a hold as soon as it exists.
const heldRecord = id => ({ hold_id: id, state: 'ok', entry: { kind: 'image', file: { filename: 'a.png' } }, run: {} });
answer.holds = [heldRecord('h1')];
await c.hold(1); await settle();
client.hint(); await settle();
out.held = [c.paneModel(1).held, c.paneModel(1).chip, reads.at(-1)[1]];
out.keepalive = timers.some(([ms]) => ms === KEEPALIVE_MS);
// The server lost the hold: the pane reads Released and follows the newest again.
answer.holds = [];
client.hint(); await settle();
out.released = [c.paneModel(1).held, c.paneModel(1).chip];
answer.holds = [heldRecord('h2'), heldRecord('h3')];
await c.hold(1); await settle();
for (const fn of winListeners.pagehide || []) fn();
out.pagehide = released.at(-1);
// Shrinking the layout closes pane 2: its hold ends.
await c.hold(1); await settle();
c.setLayout({count: 1}); await settle();
out.shrunk = released.at(-1);
out.defaultDeleted = stored.has('c:wf') ? stored.get('c:wf').count : null;
""")
    assert out["firstReads"] == 0
    assert out["demand"][-1] is True
    assert out["read"] == [["9"], []]
    assert out["stored"] == 2
    assert out["model"] == [None, True, "a.png", True]
    assert out["held"] == [True, "Held", ["h1"]]
    assert out["keepalive"]
    assert out["released"] == [False, "Released"]
    assert out["pagehide"] == [["h2"], True]
    assert out["shrunk"] == "h3"


def test_coordinator_runs_settle_cancel_session_loss_workflow_switch_and_create():
    out = run_coordinator("""
c.setVisible(true); c.setLayout({count: 2}); c.assign(1, {kind: 'view', viewId: 'a'}); await settle();
await c.run(1); await settle();
out.started = [runs.at(-1), c.paneModel(1).chip, c.paneModel(1).cancellable.enabled];
out.polling = timers.some(([ms]) => ms === POLL_MS);
// ComfyUI dropped it from the queue before it ran: Canceled, not the old result.
answer.runs = [{ prompt_id: 'p9', finished: true, outcome: 'removed' }];
client.hint(); await settle();
out.removed = c.paneModel(1).chip;
// Session loss stops reads; restore reads again.
const before = reads.length;
lostCb(); await settle(); client.hint(); await settle();
out.lost = [c.paneModel(1).message, reads.length === before, demand.at(-1)];
restoredCb(); await settle();
out.restored = reads.length > before;
// A Sonder App workflow switch: holds end, the new workflow's layout loads.
answer.sources = { '9': { state: 'ok', result: { prompt_id: 'p1', fresh: true, run: {}, entries: [{ index: 0, kind: 'image', file: { filename: 'a.png' } }] } } };
answer.holds = [{ hold_id: 'h1', state: 'ok', entry: { kind: 'image', file: { filename: 'a.png' } }, run: {} }];
client.hint(); await settle();
await c.hold(1); await settle();
stored.set('a:t2', { ts: 9, count: 3, panes: [] });
key = 'a:t2'; c.hostChanged(); await settle();
out.switched = [c.layout().count, released.at(-1)];
// Add output: one Controls change, then the pane shows the new View.
await c.createView(0, { path: '12:7' });
const target = Object.keys(applied.at(-1))[0];
out.created = [/^view:[A-Za-z0-9_-]{12}:create$/.test(target), applied.at(-1)[target], c.layout().panes[0].source.kind];
c.destroy();
out.destroyed = demand.at(-1);
""")
    assert out["started"] == ["9", "Running", True]
    assert out["polling"]
    assert out["removed"] == "Canceled"
    assert out["lost"] == ["Disconnected", True, False]
    assert out["restored"]
    assert out["switched"][0] == 3 and out["switched"][1] == [["h1"], False]
    assert out["created"] == [True, {"candidate": {"path": "12:7"}}, "view"]
    assert out["destroyed"] is False


# ── Panes ────────────────────────────────────────────────────────────────────

def test_panes_layout_control_picker_composite_swap_parking_and_teardown():
    script = COORDINATOR.replace("COORD_URL", url("views_coordinator.js")).replace("AUDIO_URL", url("audio_ownership.js")) + f"""
const {{ mountViewPanes }} = await import({url("view_panes.js")});
const root = document.createElement('div'); root.style.cssText = 'flex:1;display:flex;';
const header = document.createElement('div');
document.body.append(root, header);
const placed = [];
const sourceHost = {{ driverLanes: () => [{{ index: 0, label: 'Driver 1' }}], driverLabel: i => `Driver ${{i + 1}}`, getScene: () => ({{ guide_frames: [] }}),
    sceneAspect: () => 16 / 9, frame: () => 0, totalFrames: () => 48, isGuideTrackHidden: () => false, clock: {{ subscribe: () => () => {{}} }} }};
c.setVisible(true);
const panes = mountViewPanes({{ root, header, coordinator: c, audio, sourceHost, cache: {{}},
    placeComposite: (body, opts) => placed.push(body ? body.parentElement?.dataset.sonderPane : (opts?.restore ? 'restore' : 'park')) }});
globalThis.onCoordinatorChange = () => panes.update();
flushRaf();
out.layoutButtons = header.all(el => el.dataset.sonderPaneCount).map(el => el.dataset.sonderPaneCount);
out.singleBarInHeader = !!header.find(el => el.dataset.sonderPaneSource === '0');
out.firstPlaced = placed.at(-1);
header.find(el => el.dataset.sonderPaneCount === '2').click(); flushRaf();
out.cells = root.children.length;
// The picker: sections, Escape closes it; picking Composite in pane 2 swaps.
const button = root.find(el => el.dataset.sonderPaneSource === '1');
button.click(); flushRaf();
const menu = document.body.find(el => el.dataset.sonderPanePicker === 'true');
out.menu = menu ? menu.all(el => el.tagName === 'BUTTON').map(el => el.children[0]?.textContent) : null;
menu.all(el => el.tagName === 'BUTTON').find(el => el.children[0]?.textContent === 'Composite').click(); flushRaf();
out.swapped = [c.layout().panes[0].source.kind, c.layout().panes[1].source.kind, placed.at(-1)];
out.menuGone = !document.body.find(el => el.dataset.sonderPanePicker === 'true');
// Show a View in pane 1 (no Composite on screen): the canvas is parked.
c.assign(1, {{ kind: 'view', viewId: 'a' }}); flushRaf();
out.parked = placed.at(-1);
panes.destroy();
out.teardown = [root.style.cssText, header.children.length, placed.at(-1)];
"""
    out = node_run(script + "\nconsole.log('RESULT=' + JSON.stringify(out));")
    assert out["layoutButtons"] == ["1", "2", "3", "4"]
    assert out["singleBarInHeader"] and out["firstPlaced"] == "0"
    assert out["cells"] == 2
    assert out["menu"][:4] == ["Composite", "Guides", "Driver 1", "Preview"] and "SaveVideo" in out["menu"]
    assert out["swapped"] == ["guides", "composite", "1"]
    assert out["menuGone"]
    assert out["parked"] == "park"
    assert out["teardown"] == ["flex:1;display:flex;", 0, "restore"]


# ── Publication squeeze and the blob cache ──────────────────────────────────

def test_publication_squeeze_drops_view_candidates_first_and_keeps_views():
    out = node_run(f"""
const {{ fitPublicationView }} = await import({url("controls_canvas_relay.js")});
const views = [{{ view_id: 'a', status: 'ok', label: 'Preview', path: '9', extra: 'x'.repeat(10) }}];
const big = 'x'.repeat(600 * 1024);
const envelope = {{ view_seq: 1, prompt_hash: 'h', applied: [], rejected: [] }};
const out = {{}};
const tooMany = fitPublicationView({{ bindings: [], notes: [], views, view_candidates: Array.from({{ length: 128 }}, (_, i) => ({{ path: String(i), title: 'y'.repeat(5000) }})) }}, envelope);
out.shrunk = [tooMany.views.length, tooMany.view_candidates.length < 128, tooMany.view_candidates_truncated];
const hopeless = fitPublicationView({{ bindings: [{{ binding_id: 'b', status: 'ok', label: big }}], notes: [], views, views_error: null }}, envelope);
out.fallback = [hopeless.error?.slice(0, 40), hopeless.views];
console.log('RESULT=' + JSON.stringify(out));
""")
    assert out["shrunk"] == [1, True, True]
    assert out["fallback"][0].startswith("This workflow's Controls are too large")
    assert out["fallback"][1] == [{"view_id": "a", "label": "Preview", "path": "9", "status": "ok"}]


def test_output_media_cache_missing_too_large_holders_eviction_and_abort():
    out = node_run(f"""
const {{ createOutputMediaCache, fileKey }} = await import({url("output_media_cache.js")});
const fetched = [];
const bodies = {{
    'ok.png': () => ({{ ok: true, status: 200, headers: {{ get: k => (k === 'Content-Type' ? 'image/png' : null) }}, body: null, blob: async () => new Blob(['1234']) }}),
    'big.mp4': () => ({{ ok: true, status: 200, headers: {{ get: k => (k === 'Content-Length' ? '999999' : null) }}, blob: async () => new Blob([]) }}),
    'stream.mp4': () => {{ let n = 0; return {{ ok: true, status: 200, headers: {{ get: () => null }}, body: {{ getReader: () => ({{
        read: async () => (n++ < 3 ? {{ done: false, value: new Uint8Array(600) }} : {{ done: true }}) }}) }} }}; }},
    'gone.png': () => ({{ ok: false, status: 404 }}),
}};
let pending = null;
const fetchImpl = (u, init) => {{ fetched.push(u); const name = u.split('/').pop();
    if (name === 'slow.png') return new Promise((res, rej) => {{ pending = res; init.signal.addEventListener('abort', () => rej(Object.assign(new Error('x'), {{ name: 'AbortError' }}))); }});
    return Promise.resolve(bodies[name]()); }};
const revoked = [];
const cache = createOutputMediaCache({{ fetchImpl, urlFor: file => 'view/' + file.filename, fileCeilingBytes: 1000, softCapBytes: 6,
    createObjectURL: () => 'blob:' + Math.random(), revokeObjectURL: u => revoked.push(u) }});
const entry = (filename, revision = '4:1') => ({{ file: {{ filename, subfolder: '', type: 'output' }}, revision }});
const out = {{}};
out.key = fileKey(entry('ok.png'));
const a = cache.acquire(entry('ok.png'), 'pane:0'); const b = cache.acquire(entry('ok.png'), 'pane:1');
out.shared = [(await a.ready).url === (await b.ready).url, fetched.length];
out.missing = (await cache.acquire(entry('gone.png'), 'p').ready).error;
out.large = (await cache.acquire(entry('big.mp4'), 'p').ready).error;
out.streamed = (await cache.acquire(entry('stream.mp4'), 'p').ready).error;
// A changed revision is another blob, never the old bytes.
const c2 = cache.acquire(entry('ok.png', '5:2'), 'pane:2'); await c2.ready;
out.revisions = cache.snapshot().entries;
a.release(); b.release(); c2.release();
out.evicted = [cache.snapshot().entries, revoked.length];  // over the soft cap: idle blobs go, oldest first
const slow = cache.acquire(entry('slow.png'), 'pane:3');
slow.release();  // nobody waits any more: the fetch is abandoned
out.aborted = (await slow.ready).error;
cache.destroy();
out.destroyed = cache.snapshot().entries;
console.log('RESULT=' + JSON.stringify(out));
""")
    assert out["key"] == "output//ok.png#4:1"
    assert out["shared"] == [True, 1]
    assert out["missing"] == "missing_file" and out["large"] == "too_large" and out["streamed"] == "too_large"
    assert out["revisions"] == 2
    assert out["evicted"][0] <= 1 and out["evicted"][1] >= 1
    assert out["aborted"] == "aborted" and out["destroyed"] == 0


# ── Audit fixes (3A implementation audit, 2026-10-24) ───────────────────────

def run_tracked_coordinator(body: str):
    """The coordinator fixture with cancellable timers and a capability that
    first fails (ComfyUI restarting), then answers."""
    script = (COORDINATOR.replace("COORD_URL", url("views_coordinator.js")).replace("AUDIO_URL", url("audio_ownership.js"))
              .replace("clearTimer: () => {}", "clearTimer: id => { if (id) cleared.add(id); }")
              .replace("capability: async () => ({ ok: true })", "capability: async () => (capCalls++, globalThis.capabilityAnswer)"))
    prefix = "const cleared = new Set(); let capCalls = 0; globalThis.capabilityAnswer = { ok: false, reason: 'unreachable' };\n"
    script = script.replace("const rafQueue = [];", prefix + "const rafQueue = [];", 1)
    return node_run(script + body + "\nconsole.log('RESULT=' + JSON.stringify(out));")


VIEW_RESULT = "{ '9': { state: 'ok', result: { prompt_id: 'p1', fresh: true, run: {}, entries: [{ index: 0, kind: 'image', file: { filename: 'a.png' }, revision: '4:1' }] } } }"


def test_coordinator_audit_fixes_keepalive_unlisted_runs_late_holds_refused_create_and_capability():
    out = run_tracked_coordinator(f"""
const live = ms => timers.map((t, i) => [t, i + 1]).filter(([t, id]) => t[0] === ms && !cleared.has(id));
c.setVisible(true); c.setLayout({{count: 2}}); c.assign(1, {{kind: 'view', viewId: 'a'}}); await settle();
// F14: a failed capability ask is asked again on session restore.
out.capability = [c.paneModel(1).runnable.show, capCalls];
globalThis.capabilityAnswer = {{ ok: true }};
lostCb(); restoredCb(); await settle();
out.capabilityAgain = [c.paneModel(1).runnable.show, capCalls];
// F4: an unchanged pane option (a media mount reporting its zoom) writes nothing.
const writes = [];
const write = layoutStore.write; layoutStore.write = (k, r) => {{ writes.push(k); write(k, r); }};
c.setPaneOption(1, {{ zoom: null }}); c.setPaneOption(1, {{ fit: 'contain' }});
out.unchangedWrites = writes.length;
c.setPaneOption(1, {{ fit: 'cover' }});
out.changedWrites = writes.length;
// F8: a Hold answered after its pane let go is released at once.
answer.sources = {VIEW_RESULT};
client.hint(); await settle();
let answerHold;
const realHold = client.hold;
client.hold = () => new Promise(r => {{ answerHold = r; }});
const holding = c.hold(1);
c.release(1);
answerHold({{ ok: true, hold_id: 'late' }}); await holding; await settle();
out.lateHold = [released.includes('late'), c.paneModel(1).held];
client.hold = realHold;
// F6: a pane's run the read no longer lists (a long queue after it) is asked about alone.
const readRuns = [];
let finished = false;
client.readRun = async pid => (readRuns.push(pid), {{ ok: true, prompt_id: pid, finished, outcome: 'success' }});
await c.run(1); await settle();
out.beforeAsk = c.paneModel(1).runnable.enabled;
finished = true;
client.hint(); await settle();
out.unlisted = [readRuns, c.paneModel(1).runnable.enabled, c.paneModel(1).cancellable.show];
// F5: a hidden page keeps its holds alive; hints while hidden never push the keepalive back.
answer.holds = [{{ hold_id: 'h1', state: 'ok', entry: {{ kind: 'image', file: {{ filename: 'a.png' }}, revision: '4:1' }}, run: {{}} }}];
client.hold = async () => ({{ ok: true, hold_id: 'h1' }});
await c.hold(1); await settle(); client.hint(); await settle();
document.hidden = true;
const armed = live(KEEPALIVE_MS).map(([, id]) => id);
const readsHidden = reads.length;
client.hint(); await settle(); client.hint(); await settle();
out.hidden = [armed.length, JSON.stringify(live(KEEPALIVE_MS).map(([, id]) => id)) === JSON.stringify(armed), reads.length === readsHidden];
live(KEEPALIVE_MS).at(-1)[0][1](); await settle();
out.keepaliveRead = [reads.length - readsHidden, reads.at(-1)[1], c.paneModel(1).held];
document.hidden = false;
// F9: a refused Add output puts back what the pane showed.
controls.applyChanges = async () => ({{ ok: false, rejected: [{{ reason: 'This output moved. Refresh the Controls panel.' }}] }});
c.release(1); c.setLayout({{count: 1}}); await settle();
out.before = c.layout().panes[0].source.kind;
await c.createView(0, {{ path: '12:7' }}); await settle();
out.refused = [c.layout().panes[0].source.kind, notes.at(-1)];
""")
    assert out["capability"] == [False, 1]
    assert out["capabilityAgain"] == [True, 2]
    assert out["unchangedWrites"] == 0 and out["changedWrites"] == 1
    assert out["lateHold"] == [True, False]
    assert out["beforeAsk"] is False
    assert out["unlisted"][0][-1] == "p9" and out["unlisted"][1:] == [True, False]
    assert out["hidden"] == [1, True, True]
    assert out["keepaliveRead"] == [1, ["h1"], True]
    assert out["before"] == "composite"
    assert out["refused"] == ["composite", "This output moved. Refresh the Controls panel."]


PANES = r"""
const { mountViewPanes } = await import(PANES_URL);
const root = document.createElement('div'); root.style.cssText = 'flex:1;display:flex;';
const header = document.createElement('div');
document.body.append(root, header);
let hidden = false, sceneReads = 0, acquires = 0;
const sourceHost = { driverLanes: () => [], driverLabel: i => `Driver ${i + 1}`,
    getScene: () => (sceneReads++, { guide_frames: [{ asset_id: 'g', frame_index: 0 }] }),
    sceneAspect: () => 16 / 9, frame: () => 0, totalFrames: () => 48, isGuideTrackHidden: () => hidden,
    getGuideAsset: () => ({ path: 'g.png' }), buildViewUrl: p => 'view/' + p, clock: { subscribe: () => () => {} } };
const cache = { acquire: () => (acquires++, { ready: new Promise(() => {}), release() {} }) };
c.setVisible(true);
const panes = mountViewPanes({ root, header, coordinator: c, audio, sourceHost, cache, placeComposite: () => {} });
globalThis.onCoordinatorChange = () => panes.update();
const text = el => el.textContent;
const buttons = index => root.find(el => el.dataset?.sonderPane === String(index)).all(el => el.tagName === 'BUTTON').map(text);
flushRaf();
"""


def test_panes_audit_fixes_paused_painters_owner_mute_and_hold_keeps_media():
    script = (COORDINATOR.replace("COORD_URL", url("views_coordinator.js")).replace("AUDIO_URL", url("audio_ownership.js"))
              + PANES.replace("PANES_URL", url("view_panes.js")) + f"""
c.setLayout({{count: 2}}); c.assign(1, {{ kind: 'view', viewId: 'a' }}); flushRaf();
c.assign(0, {{ kind: 'guides' }}); flushRaf();
// F1: the Guide track hidden under a paused clock: the pane says so on the next render.
const readsBefore = sceneReads;
hidden = true; panes.update(); flushRaf();
out.guides = [sceneReads > readsBefore, text(root.children[0]).includes('Guides are hidden')];
// F15: Hold and Release of the shown file keep its media (no remount). The
// server answers only the sources a read names, as run_outputs.read_outputs does.
const everySource = client.readOutputs;
client.readOutputs = async (sources, opts) => {{
    const read = await everySource(sources, opts);
    read.sources = Object.fromEntries(Object.entries(read.sources).filter(([path]) => sources.includes(path)));
    return read;
}};
answer.sources = {VIEW_RESULT};
answer.holds = [{{ hold_id: 'h1', state: 'ok', entry: {{ index: 0, kind: 'image', file: {{ filename: 'a.png' }}, revision: '4:1' }}, run: {{}} }}];
client.hint(); await settle(); flushRaf();
const mounted = acquires;
await c.hold(1); await settle(); client.hint(); await settle(); flushRaf();
out.heldReads = reads.at(-1)[0];
c.release(1); await settle(); flushRaf();
out.hold = [mounted, acquires];
// F7: the sound's owner keeps its Mute when its media goes (here: the file is gone).
answer.sources = {{ '9': {{ state: 'missing_file', result: null }} }};
audio.claim('pane:1');
client.hint(); await settle(); flushRaf();
out.ownerButtons = buttons(1);
root.find(el => el.tagName === 'BUTTON' && el.textContent === 'Mute').click(); flushRaf();
out.afterMute = [audio.owner(), buttons(1).includes('Mute')];
// F13: a Relay error says why in the picker instead of "every output has a View".
controlsView = {{ state: 'ready', error: 'Keep exactly one active Sonder Control Relay in the workflow.', views: [], view_candidates: [] }};
for (const fn of controlsListeners) fn(controlsView);
root.find(el => el.dataset?.sonderPaneSource === '0').click(); flushRaf();
const menu = document.body.find(el => el.dataset?.sonderPanePicker === 'true');
out.picker = [text(menu).includes('Keep exactly one active Sonder Control Relay'), text(menu).includes('already has a View')];
panes.destroy();
""")
    out = node_run(script + "\nconsole.log('RESULT=' + JSON.stringify(out));")
    assert out["guides"] == [True, True]
    assert out["hold"][0] >= 1 and out["hold"][1] == out["hold"][0]
    assert out["heldReads"] == ["9"]  # a held pane's source is still read
    assert "Mute" in out["ownerButtons"]
    assert out["afterMute"] == ["timeline", False]
    assert out["picker"] == [True, False]


def test_driver_painter_paints_on_change_not_every_frame_while_paused():
    out = node_run(FAKE_DOM + f"""
const {{ mountDriverPainter }} = await import({url("editor_source_followers.js")});
const out = {{}};
const make = document.createElement;
document.createElement = tag => {{ const el = make(tag); if (tag === 'video') Object.assign(el, {{ paused: true, readyState: 0, currentTime: 0,
    pause() {{}}, play() {{ return Promise.resolve(); }}, load() {{}} }}); return el; }};
const clip = {{ clip_id: 'c1', role: 'motion_driver', track_index: 0, source_path: 'd.mp4', timeline_start_frame: 0, timeline_end_frame: 48 }};
const host = {{ getScene: () => ({{ motion_driver_lane_count: 1, clips: [clip] }}), frame: () => 0, fps: () => 24, sceneAspect: () => 16 / 9,
    isDriverLaneHidden: () => false, getAssetForSourcePath: () => ({{ asset_id: 'd', path: 'd.mp4', asset_type: 'video' }}), projectId: () => 'P',
    acquireSceneSource: () => ({{ ready: new Promise(() => {{}}), release() {{}} }}),
    clock: {{ get: () => ({{ frame: 0, playing: false, hold: null, fps: 24, anchor: {{}} }}), subscribe: () => () => {{}} }} }};
const canvas = document.createElement('canvas'); const cell = document.createElement('div');
const painter = mountDriverPainter({{ canvas, cell, host, index: 0 }});
flushRaf(); flushRaf();
out.idle = rafQueue.length;
painter.update(); out.afterUpdate = rafQueue.length;
flushRaf(); out.settled = rafQueue.length;
painter.destroy();
console.log('RESULT=' + JSON.stringify(out));
""")
    # Paused: an update asks for one paint, and nothing keeps the loop running.
    assert out == {"idle": 0, "afterUpdate": 1, "settled": 0}


def test_output_media_cache_forgets_a_shown_file_once_it_is_let_go():
    out = node_run(f"""
const {{ createOutputMediaCache }} = await import({url("output_media_cache.js")});
const revoked = [];
const cache = createOutputMediaCache({{ fetchImpl: async () => ({{ ok: true, status: 200, headers: {{ get: () => null }}, body: null, blob: async () => new Blob(['12']) }}),
    urlFor: f => 'view/' + f.filename, createObjectURL: () => 'blob:1', revokeObjectURL: u => revoked.push(u) }});
const entry = {{ file: {{ filename: 'held.png', type: 'output', subfolder: '' }}, revision: '2:9' }};
const claim = cache.acquire(entry, 'pane:0'); await claim.ready;
cache.forget(entry);
const shown = [cache.snapshot().entries, revoked.length];
claim.release();
console.log('RESULT=' + JSON.stringify({{ shown, released: [cache.snapshot().entries, revoked.length] }}));
""")
    assert out == {"shown": [1, 0], "released": [0, 1]}


def test_run_outputs_client_urls_for_reads_holds_and_cancel_in_every_host():
    out = node_run(f"""
const {{ createRunOutputsClient }} = await import({url("run_outputs_client.js")});
const calls = [];
const fetchImpl = async (u, init = {{}}) => (calls.push([u, init.method || 'GET', init.body ? JSON.parse(init.body) : null, !!init.keepalive, init.headers]),
    {{ ok: true, status: 200, json: async () => ({{ ok: true }}) }});
const sdk = createRunOutputsClient({{ mode: 'sdk', apiURL: p => p, fetchImpl,
    identity: () => ({{ projectId: 'P', sessionId: 's1', secret: 'k', tabSessionId: 'tab-1', runIds: () => [], workflowId: 't2' }}) }});
const canvas = createRunOutputsClient({{ mode: 'canvas', apiURL: p => p, fetchImpl,
    identity: () => ({{ projectId: 'P', hostId: 'h1', sessionId: 'e1', workflowId: 'wf' }}) }});
await sdk.readOutputs(['9'], {{ keepHolds: ['h1', 'h2'] }});
await sdk.cancel({{ runId: 'run-1' }});
await sdk.releaseAll(['h1'], {{ keepalive: true }});
await canvas.readOutputs(['9'], {{ keepHolds: [] }});
await canvas.cancel({{ promptId: 'p1' }});
console.log('RESULT=' + JSON.stringify(calls.map(([u, m, b, k]) => [u, m, b, k])));
""")
    assert out[0][0] == "/sonder-editor/sdk/session/s1/runs/outputs?sources=9&workflow_id=t2&keep_holds=h1%2Ch2"
    assert out[1][:2] == ["/sonder-editor/sdk/session/s1/runs/run-1/cancel", "POST"]
    assert out[2] == ["/sonder-editor/sdk/session/s1/runs/release", "POST", {"hold_ids": ["h1"]}, True]
    # A page with no holds says so: the other page's holds age out on their own.
    assert out[3][0] == "/sonder-editor/project/P/runs/outputs?host_id=h1&session_id=e1&sources=9&workflow_id=wf&keep_holds="
    assert out[4][:3] == ["/sonder-editor/project/P/runs/p1/cancel", "POST", {"host_id": "h1", "session_id": "e1"}]


def test_manage_views_keeps_its_buttons_and_drafts_across_renders_and_queues_writes():
    out = node_run(FAKE_DOM + f"""
const {{ mountViewsManage }} = await import({url("controls_views_manage.js")});
const out = {{}};
const row = (id, label, order) => ({{ view_id: id, label, default_label: 'Preview', order, origin: 'authored', status: 'ok', participation: 'active' }});
let view = {{ views: [row('a', 'First', 0), row('b', 'Second', 1)] }};
const applied = [];
const host = {{ getView: () => view, capabilities: {{}}, applyChanges: async changes => {{
    applied.push(changes);
    await new Promise(r => setTimeout(r, 5));
    for (const [target, value] of Object.entries(changes)) {{
        const [, id, field] = target.split(':');
        if (field === 'remove') view = {{ views: view.views.filter(d => d.view_id !== id) }};
        else view = {{ views: view.views.map(d => (d.view_id === id ? {{ ...d, [field]: value }} : d)) }};
    }}
    // The server lists Views by order, as describe_views sorts them.
    view = {{ views: [...view.views].sort((x, y) => x.order - y.order) }};
    return {{ ok: true }};
}} }};
const manage = mountViewsManage(host);
const buttonsOf = id => manage.el.find(el => el.dataset?.viewId === id).all(el => el.tagName === 'BUTTON');
const inputOf = id => manage.el.find(el => el.dataset?.viewId === id).find(el => el.tagName === 'INPUT');
// A render with nothing new keeps every element: a press survives to its click.
const before = buttonsOf('b')[2];
manage.render(view); manage.render(view);
out.kept = buttonsOf('b')[2] === before;
// A name being typed survives a rebuild for another View's change.
const typing = inputOf('a'); typing.focus(); typing.value = 'Half typ';
view = {{ views: [row('a', 'First', 0), row('b', 'Second (renamed elsewhere)', 1)] }};
manage.render(view);
out.draft = [inputOf('a') !== typing, inputOf('a').value, document.activeElement === inputOf('a')];
typing.dispatch('blur');  // the replaced input's last blur commits nothing
await new Promise(r => setTimeout(r, 20));
out.discarded = applied.length;
// The blur of the press that clicks Delete commits the rename, then the Delete runs.
const renaming = inputOf('b'); renaming.focus(); renaming.value = 'Renamed';
const remove = buttonsOf('b')[2];
renaming.dispatch('blur');
remove.click();
await new Promise(r => setTimeout(r, 40));
out.queued = applied.slice();
out.left = view.views.map(d => d.view_id);
// Moves read the Views when their turn comes: two quick presses move twice.
view = {{ views: [row('a', 'A', 0), row('c', 'C', 1), row('d', 'D', 2)] }};
manage.render(view);
buttonsOf('d')[0].click(); buttonsOf('d')[0].click();
await new Promise(r => setTimeout(r, 40));
out.order = [...view.views].sort((x, y) => x.order - y.order).map(d => d.view_id);
manage.destroy();
console.log('RESULT=' + JSON.stringify(out));
""")
    assert out["kept"]
    assert out["draft"] == [True, "Half typ", True]
    assert out["discarded"] == 0
    assert out["queued"] == [{"view:b:label": "Renamed"}, {"view:b:remove": True}]
    assert out["left"] == ["a"]
    assert out["order"] == ["d", "a", "c"]


def test_passive_playhead_writes_that_do_not_move_it_are_no_jump():
    widget = (JS / "editor_widget.js").read_text(encoding="utf-8")
    constants = widget[widget.index("const CONTINUOUS_PLAYHEAD_REASONS"):widget.index("export class EditorWidget {")]
    start = widget.index("    get playhead() {")
    end = widget.index("    constructor(node, options = {}) {")
    out = node_run(f"""
{constants}
class W {{
{widget[start:end]}
}}
const w = new W();
const heard = [];
w._setPlayhead(5, "init");
w._clockSubscribers = new Set([e => heard.push([e.frame, e.reason, e.discontinuity])]);
for (const reason of ["widget-state", "duration-clamp", "external"]) w._setPlayhead(5, reason);  // restated: nothing
w._setPlayhead(5, "scene-switch");   // same frame, another scene: still a jump
w._setPlayhead(3, "duration-clamp");  // clamped down: a jump
console.log('RESULT=' + JSON.stringify(heard));
""")
    assert out == [[5, "scene-switch", True], [3, "duration-clamp", True]]


def test_coordinator_creates_sampling_views_and_never_reads_them_as_outputs():
    out = run_coordinator("""
controlsView = { state: 'ready', views: [
    { view_id: 'a', status: 'ok', participation: 'active', path: '9', label: 'Preview' },
    { view_id: 's', feed: 'sampling', status: 'ok', participation: 'active', path: '6', label: 'Pass 1' }],
    view_candidates: [], view_sampling_candidates: [{ path: '6', class_type: 'KSampler' }] };
for (const fn of controlsListeners) fn(controlsView);
c.setVisible(true); c.setLayout({count: 2}); c.assign(0, {kind: 'view', viewId: 'a'}); c.assign(1, {kind: 'view', viewId: 's'});
await settle();
out.sources = reads.at(-1)?.[0];
await c.createView(0, { path: '31' }, { feed: 'sampling' });
const target = Object.keys(applied.at(-1))[0];
out.created = applied.at(-1)[target];
c.destroy();
""")
    # The sampling View's node is no output: runs/outputs never asks for it (4C reads runs/live).
    assert out["sources"] == ["9"]
    assert out["created"] == {"candidate": {"path": "31"}, "feed": "sampling"}


# ── Live sampling previews (Phase 4C) ───────────────────────────────────────

LIVE_CLIENT = r"""
    async readLive(sources) { liveReads.push([...sources]); return JSON.parse(JSON.stringify(liveAnswer)); },
    fetchLiveBytes(pid, source, opts) {
        byteCalls.push([pid, source]);
        if (bytesImpl) return bytesImpl(pid, source, opts.signal);
        return Promise.resolve({ ok: true, status: 200, n: liveAnswer.sources[source]?.live?.n, mime: 'image/jpeg', blob: { size: 3, pid } });
    },
    onLivePreview(fn) { client.liveHint = fn; return () => { client.liveHint = null; }; },
    onOutputsChanged(fn) { client.hint = fn; return () => { client.hint = null; }; },
"""

SAMPLING_VIEWS = r"""
controlsView = { state: 'ready', views: [
    { view_id: 'a', status: 'ok', participation: 'active', path: '9', label: 'Preview' },
    { view_id: 's', feed: 'sampling', status: 'ok', participation: 'active', path: '6', label: 'Pass 1', class_type: 'KSampler' }],
    view_candidates: [], view_sampling_candidates: [] };
for (const fn of controlsListeners) fn(controlsView);
const rec = (n, extra = {}) => ({ prompt_id: 'p1', n, step: n, total: 6, mime: 'image/jpeg', publisher: 'native', has_payload: true, ...extra });
const liveTimers = () => timers.map((t, i) => [t, i + 1]).filter(([t, id]) => t[0] === LIVE_POLL_MS && !cleared.has(id));
"""


def run_live_coordinator(body: str):
    script = (COORDINATOR.replace("COORD_URL", url("views_coordinator.js")).replace("AUDIO_URL", url("audio_ownership.js"))
              .replace("    onOutputsChanged(fn) { client.hint = fn; return () => { client.hint = null; }; },\n", LIVE_CLIENT)
              .replace("clearTimer: () => {}", "clearTimer: id => { if (id) cleared.add(id); }")
              .replace("const { createViewsCoordinator, KEEPALIVE_MS, POLL_MS }", "const { createViewsCoordinator, KEEPALIVE_MS, POLL_MS, LIVE_POLL_MS }"))
    prefix = ("const cleared = new Set(); const liveReads = [], byteCalls = []; let bytesImpl = null;\n"
              "let liveAnswer = { ok: true, sources: {}, seen: [], sampling: { native: true, kj: { state: 'ok' } } };\n")
    script = script.replace("const rafQueue = [];", prefix + "const rafQueue = [];", 1)
    return node_run(script + SAMPLING_VIEWS + body + "\nconsole.log('RESULT=' + JSON.stringify(out));")


def test_coordinator_live_reads_fetch_the_newest_bytes_and_poll_only_while_active():
    out = run_live_coordinator("""
c.setVisible(true); await settle();
out.noPane = liveReads.length;  // no sampling pane, no picker: nothing to read
c.setLayout({count: 2}); c.assign(0, {kind: 'view', viewId: 'a'}); c.assign(1, {kind: 'view', viewId: 's'}); await settle();
out.outputSources = reads.at(-1)[0];
out.liveSources = liveReads.at(-1);
liveAnswer.sources = { '6': { state: 'live', live: rec(4), run: { prompt_id: 'p1', finished: false }, latest_run: { prompt_id: 'p1', in_scope: true } } };
client.liveHint(); await settle();
let m = c.paneModel(1);
out.first = [byteCalls.length, m.frame?.n, m.chip, m.runnable.show, m.canHold, m.linkable, m.message];
out.polling = liveTimers().length;
// The server serves its newest payload: the header's n is what shows.
liveAnswer.sources['6'].live = rec(7);
bytesImpl = async () => ({ ok: true, status: 200, n: 8, mime: 'image/jpeg', blob: {} });
client.liveHint(); await settle();
out.newest = [byteCalls.length, c.paneModel(1).frame.n, c.paneModel(1).frame.key];
// A read naming what the pane already has fetches nothing.
liveAnswer.sources['6'].live = rec(8);
client.liveHint(); await settle();
out.same = byteCalls.length;
// The run finishes (a finish hint): Last preview, the poll stops.
liveAnswer.sources['6'] = { state: 'last', live: rec(8), run: { prompt_id: 'p1', finished: true, outcome: 'success' } };
const before = liveReads.length;
client.hint(); await settle();
out.finish = [liveReads.length - before, c.paneModel(1).chip, liveTimers().length];
// A hidden page reads nothing; visible again, it reads.
document.hidden = true;
const hiddenReads = liveReads.length;
client.liveHint(); await settle();
out.hidden = liveReads.length === hiddenReads;
document.hidden = false;
for (const fn of docListeners.visibilitychange || []) fn();
await settle();
out.visible = liveReads.length > hiddenReads;
c.destroy();
""")
    assert out["noPane"] == 0
    # The sampling View's node never reaches runs/outputs; runs/live reads only it.
    assert out["outputSources"] == ["9"] and out["liveSources"] == ["6"]
    assert out["first"] == [1, 4, "Sampling · step 4/6", False, False, False, None]
    assert out["polling"] >= 1
    assert out["newest"] == [2, 8, "p1#8"]
    assert out["same"] == 2
    assert out["finish"][0] >= 1 and out["finish"][1] == "Last preview · step 8/6" and out["finish"][2] == 0
    assert out["hidden"] and out["visible"]


def test_coordinator_live_conflict_rereads_once_refused_never_twice_abort_and_picker_seen_read():
    out = run_live_coordinator("""
c.setVisible(true); await settle();
// The picker opens with no sampling pane: a seen-only read for its group.
liveAnswer.seen = [{ source: '31', class_type: 'MySampler', title: 'Custom', publisher: 'native' }];
c.setPickerOpen(true); await settle();
out.seenRead = [liveReads.at(-1), c.liveSeen().map(s => s.source)];
c.setPickerOpen(false);
c.setLayout({count: 2}); c.assign(1, {kind: 'view', viewId: 's'}); await settle();
// 409: a newer run took the node. One more read, then no retry loop.
liveAnswer.sources = { '6': { state: 'live', live: rec(3), run: { prompt_id: 'p1', finished: false } } };
bytesImpl = async () => ({ ok: false, status: 409, code: 'superseded' });
const readsBefore = liveReads.length;
client.liveHint(); await settle();
out.conflict = [byteCalls.length, liveReads.length - readsBefore, c.paneModel(1).frame];
client.liveHint(); await settle();
out.noLoop = byteCalls.length;
// Too large on the client: Unavailable, and never asked again.
liveAnswer.sources['6'].live = rec(5);
bytesImpl = async () => ({ ok: false, status: 200, code: 'too_large' });
client.liveHint(); await settle(); client.liveHint(); await settle();
out.tooLarge = [byteCalls.length, c.paneModel(1).chip, c.paneModel(1).message];
// A fetch in flight is aborted when the pane shows something else.
liveAnswer.sources['6'].live = rec(6);
let signal = null;
bytesImpl = (pid, source, s) => { signal = s; return new Promise(() => {}); };
client.liveHint(); await settle();
c.assign(1, {kind: 'guides'}); await settle();
out.aborted = [!!signal?.aborted, byteCalls.length];
// Back on the View: it fetches again; teardown aborts that one too.
const first = signal;
c.assign(1, {kind: 'view', viewId: 's'}); await settle();
const second = signal;
c.destroy();
out.destroyAborted = [!!second && second !== first, !!second?.aborted];
""")
    assert out["seenRead"] == [[], ["31"]]
    assert out["conflict"] == [1, 2, None]  # the hint's read, then one re-read
    assert out["noLoop"] == 1
    assert out["tooLarge"] == [2, "Unavailable", "Too large to show here"]
    assert out["aborted"] == [True, 3]
    assert out["destroyAborted"] == [True, True]


def test_sampling_media_swaps_on_load_revokes_and_tears_down():
    out = node_run(FAKE_DOM + f"""
const {{ mountSamplingMedia }} = await import({url("sampling_pane_media.js")});
const created = [], revoked = [], conditions = [];
let serial = 0;
const container = document.createElement('div');
const media = mountSamplingMedia({{ container, onCondition: c => conditions.push(c),
    createObjectURL: blob => {{ const u = `blob:${{++serial}}`; created.push(u); return u; }}, revokeObjectURL: u => revoked.push(u) }});
const layer = container.children[0].children[0];
const out = {{}};
const frame = (n, mime = 'image/jpeg') => ({{ key: `p1#${{n}}`, mime, blob: {{ n }} }});
media.show(frame(1));
out.incoming = [layer.children.length, layer.children[0].tagName, layer.children[0].style.cssText.includes('visibility:hidden')];
layer.children[0].dispatch('load');
out.shown = [layer.children.length, layer.children[0].style.visibility, conditions.at(-1)];
media.show(frame(1));  // the same preview: nothing new
media.show(frame(2));
media.show(frame(3));  // replaces 2 while it loads: at most one incoming
out.loading = [layer.children.length, revoked.slice()];
layer.children[1].dispatch('load');
out.swapped = [layer.children.length, revoked.slice(), layer.children[0].src];
media.show(frame(4, 'video/mp4'));
const video = layer.children[1];
out.video = [video.tagName, video.muted, video.loop, video.autoplay, video.playsInline];
video.dispatch('error');
out.decode = [conditions.at(-1), layer.children.length, revoked.includes(video.src)];
media.show(frame(5, 'image/gif'));
out.unsupported = conditions.at(-1);
media.show(frame(6));
media.destroy();
out.destroyed = [container.children.length, created.every(u => revoked.includes(u))];
console.log('RESULT=' + JSON.stringify(out));
""")
    assert out["incoming"] == [1, "IMG", True]
    assert out["shown"] == [1, "", None]
    assert out["loading"] == [2, ["blob:2"]]
    assert out["swapped"] == [1, ["blob:2", "blob:1"], "blob:3"]
    assert out["video"] == ["VIDEO", True, True, True, True]
    assert out["decode"] == ["decode", 1, True]
    assert out["unsupported"] == "unsupported"
    assert out["destroyed"] == [0, True]


def test_sampling_pane_dom_picker_group_create_and_fit_only():
    script = (COORDINATOR.replace("COORD_URL", url("views_coordinator.js")).replace("AUDIO_URL", url("audio_ownership.js"))
              .replace("    onOutputsChanged(fn) { client.hint = fn; return () => { client.hint = null; }; },\n", LIVE_CLIENT))
    prefix = ("const liveReads = [], byteCalls = []; let bytesImpl = null;\n"
              "let liveAnswer = { ok: true, sources: {}, seen: [{ source: '31', class_type: 'MySampler', title: 'Custom', publisher: 'native' }], "
              "sampling: { native: true, kj: { state: 'ok' } } };\n")
    script = script.replace("const rafQueue = [];", prefix + "const rafQueue = [];", 1)
    script += f"""
globalThis.URL.createObjectURL = () => 'blob:x'; globalThis.URL.revokeObjectURL = () => {{}};
controlsView = {{ state: 'ready', views: [{{ view_id: 's', feed: 'sampling', status: 'ok', participation: 'active', path: '6', label: 'Pass 1', class_type: 'KSampler',
    run_reason: 'Shows previews while another output runs' }}],
    view_candidates: [], view_sampling_candidates: [{{ path: '7', class_type: 'KSamplerAdvanced', title: 'Refiner' }}] }};
const {{ mountViewPanes }} = await import({url("view_panes.js")});
const root = document.createElement('div'); const header = document.createElement('div');
document.body.append(root, header);
const sourceHost = {{ driverLanes: () => [], driverLabel: i => `Driver ${{i + 1}}`, getScene: () => ({{ guide_frames: [] }}), sceneAspect: () => 16 / 9,
    frame: () => 0, totalFrames: () => 48, isGuideTrackHidden: () => false, clock: {{ subscribe: () => () => {{}} }} }};
c.setVisible(true);
const panes = mountViewPanes({{ root, header, coordinator: c, audio, sourceHost, cache: {{}}, placeComposite: () => {{}} }});
globalThis.onCoordinatorChange = () => panes.update();
flushRaf();
c.setLayout({{count: 2}}); c.assign(1, {{ kind: 'view', viewId: 's' }}); await settle(); flushRaf();
const cell = () => root.find(el => el.dataset?.sonderPane === '1');
const buttons = () => cell().all(el => el.tagName === 'BUTTON').map(el => el.textContent);
out.waiting = [buttons(), cell().textContent.includes('No result yet')];
liveAnswer.sources = {{ '6': {{ state: 'live', live: {{ prompt_id: 'p1', n: 2, step: 2, total: 6, mime: 'image/jpeg', has_payload: true, reattributed: true }},
    run: {{ prompt_id: 'p1', finished: false }} }} }};
client.liveHint(); await settle(); flushRaf();
const chip = cell().find(el => el.dataset?.sonderPaneChip === '1');
out.live = [buttons(), chip.textContent, chip.title, cell().all(el => el.tagName === 'IMG').length];
// The picker: Sampling previews lists the core sampler and the seen node, not the one already shown.
root.find(el => el.dataset?.sonderPaneSource === '0').click(); flushRaf(); await settle(); flushRaf();
const menu = () => document.body.find(el => el.dataset?.sonderPanePicker === 'true');
const labels = menu().all(el => el.tagName === 'BUTTON').map(el => el.children[0]?.textContent);
out.picker = [menu().textContent.includes('Sampling previews'), labels.includes('Refiner'), labels.includes('Custom'), labels.includes('KSampler')];
menu().all(el => el.tagName === 'BUTTON').find(el => el.children[0]?.textContent === 'Custom').click(); await settle();
const target = Object.keys(applied.at(-1))[0];
out.created = applied.at(-1)[target];
panes.destroy();
"""
    out = node_run(script + "\nconsole.log('RESULT=' + JSON.stringify(out));")
    # Before any preview: the message, and no Run, Hold, Link, Fit or sound.
    assert out["waiting"][1] is True
    assert [b for b in out["waiting"][0] if not b.endswith("▾")] == []
    assert [b for b in out["live"][0] if not b.endswith("▾")] == ["Fit"]
    assert out["live"][1:] == ["Sampling · step 2/6", "Sampling · step 2/6 · Matched through the model chain", 1]
    assert out["picker"] == [True, True, True, False]
    assert out["created"] == {"candidate": {"path": "31"}, "feed": "sampling"}


def test_run_outputs_client_live_reads_bytes_and_hints_in_every_host():
    out = node_run(f"""
const C = await import({url("run_outputs_client.js")});
const calls = [];
let body = {{ size: 4 }}, headers = {{ 'X-Sonder-Live-N': '12', 'Content-Type': 'image/webp' }}, status = 200;
const fetchImpl = async (u, init = {{}}) => (calls.push([u, init.headers, init.cache]),
    {{ ok: status === 200, status, headers: {{ get: k => headers[k] ?? null }}, blob: async () => body, json: async () => ({{ ok: false, code: 'superseded' }}) }});
const sdk = C.createRunOutputsClient({{ mode: 'sdk', apiURL: p => p, fetchImpl,
    identity: () => ({{ projectId: 'P', sessionId: 's1', secret: 'k', tabSessionId: 'tab-1', runIds: () => ['run-1'], workflowId: 't2' }}) }});
const canvas = C.createRunOutputsClient({{ mode: 'canvas', apiURL: p => p, fetchImpl,
    identity: () => ({{ projectId: 'P', hostId: 'h1', sessionId: 'e1', workflowId: 'wf' }}) }});
const out = {{}};
await canvas.readLive(['6', '12:5']);
await sdk.readLive([]);
out.canvasBytes = await canvas.fetchLiveBytes('p1', '12:5');
out.sdkBytes = await sdk.fetchLiveBytes('p1', '6');
status = 409;
out.conflict = await canvas.fetchLiveBytes('p1', '6');
status = 200; headers = {{ 'Content-Length': String(17 * 1024 * 1024) }};
out.declared = (await canvas.fetchLiveBytes('p1', '6')).code;
headers = {{}}; body = {{ size: 17 * 1024 * 1024 }};
out.measured = (await canvas.fetchLiveBytes('p1', '6')).code;
out.calls = calls.map(([u, h, cache]) => [u, h?.['X-Sonder-SDK-Session'] || null, cache]);
// Hints: live_preview reaches onLivePreview only; outputs_changed reaches onOutputsChanged only.
const heard = [];
canvas.onLivePreview(e => heard.push(['canvas-live', e.type]));
canvas.onOutputsChanged(e => heard.push(['canvas-out', e.type]));
sdk.onLivePreview(e => heard.push(['sdk-live', e.type]));
C.publishRunEvent('P', {{ type: 'live_preview', host_ids: ['h1'], run_ids: [] }});
C.publishRunEvent('P', {{ type: 'live_preview', host_ids: ['other'], run_ids: ['run-1'] }});
C.publishRunEvent('P', {{ type: 'outputs_changed', host_ids: ['h1'], run_ids: [] }});
C.publishRunEvent('Q', {{ type: 'live_preview', host_ids: ['h1'], run_ids: ['run-1'] }});
out.heard = heard;
out.own = [canvas.isOwnHint({{ type: 'live_preview', host_ids: ['h1'] }}), canvas.isOwnHint({{ type: 'run_request_changed', host_ids: ['h1'] }})];
delete out.canvasBytes.blob; delete out.sdkBytes.blob;
console.log('RESULT=' + JSON.stringify(out));
""")
    assert out["calls"][0] == ["/sonder-editor/project/P/runs/live?host_id=h1&session_id=e1&sources=6%2C12%3A5&workflow_id=wf", None, None]
    assert out["calls"][1] == ["/sonder-editor/sdk/session/s1/runs/live?sources=&workflow_id=t2", "k", None]
    assert out["calls"][2] == ["/sonder-editor/project/P/runs/live/p1/bytes?host_id=h1&session_id=e1&source=12%3A5", None, "no-store"]
    assert out["calls"][3] == ["/sonder-editor/sdk/session/s1/runs/live/p1/bytes?source=6", "k", "no-store"]
    assert out["canvasBytes"] == {"ok": True, "status": 200, "n": 12, "mime": "image/webp"}
    assert out["sdkBytes"]["n"] == 12
    assert out["conflict"] == {"ok": False, "status": 409, "code": "superseded"}
    assert out["declared"] == out["measured"] == "too_large"
    assert out["heard"] == [["canvas-live", "live_preview"], ["sdk-live", "live_preview"], ["canvas-out", "outputs_changed"]]
    assert out["own"] == [True, False]


def test_cross_tab_sync_routes_live_preview_to_the_hint_bus():
    out = node_run(f"""
const sockets = [];
globalThis.window = {{ location: {{ href: "http://x/" }}, comfyAPI: {{ api: {{ api: {{ apiURL: p => `http://x${{p}}` }} }} }} }};
globalThis.fetch = async () => ({{ ok: true, json: async () => ({{ items: {{}} }}) }});
globalThis.WebSocket = class {{ constructor(url) {{ this.url = url; this.readyState = 1; sockets.push(this); }}
    send() {{}} close() {{}} static get OPEN() {{ return 1; }} }};
const {{ connectProjectSync }} = await import({url("cross_tab_sync.js")});
const {{ createRunOutputsClient }} = await import({url("run_outputs_client.js")});
const client = createRunOutputsClient({{ mode: 'canvas', apiURL: p => p, fetchImpl: async () => ({{}}),
    identity: () => ({{ projectId: 'P', hostId: 'h1', sessionId: 'e1' }}) }});
const heard = [];
client.onLivePreview(e => heard.push(['live', e.prompt_id]));
client.onOutputsChanged(e => heard.push(['out', e.prompt_id]));
const sync = connectProjectSync('P', {{}}, {{ clientId: 'c' }});
sockets[0].onopen();
const send = event => sockets[0].onmessage({{ data: JSON.stringify(event) }});
send({{ type: 'live_preview', project_id: 'P', prompt_id: 'p1', seq: 1, host_ids: ['h1'], run_ids: [] }});
send({{ type: 'live_preview', project_id: 'P', prompt_id: 'p2', seq: 2, host_ids: ['other'], run_ids: [] }});
send({{ type: 'outputs_changed', project_id: 'P', prompt_id: 'p1', seq: 1, finished: true, host_ids: ['h1'], run_ids: [] }});
sync.close();
console.log('RESULT=' + JSON.stringify(heard));
""")
    assert out == [["live", "p1"], ["out", "p1"]]


def test_coordinator_an_unreachable_server_is_retried_once_per_read_not_in_a_loop():
    out = run_live_coordinator("""
c.setVisible(true); c.setLayout({count: 2}); c.assign(1, {kind: 'view', viewId: 's'}); await settle();
liveAnswer.sources = { '6': { state: 'live', live: rec(4), run: { prompt_id: 'p1', finished: false } } };
bytesImpl = async () => { throw new TypeError('Failed to fetch'); };
client.liveHint(); await settle(); await settle();
out.afterFirst = byteCalls.length;
client.liveHint(); await settle(); await settle();
out.afterSecond = byteCalls.length;
bytesImpl = null;  // the server is back
client.liveHint(); await settle();
out.recovered = [byteCalls.length, c.paneModel(1).frame?.n];
""")
    assert out["afterFirst"] == 1 and out["afterSecond"] == 2
    assert out["recovered"] == [3, 4]


def test_coordinator_drops_a_live_answer_from_before_a_project_switch():
    out = run_live_coordinator("""
c.setVisible(true); c.setLayout({count: 2}); c.assign(1, {kind: 'view', viewId: 's'}); await settle();
let release;
const realRead = client.readLive;
client.readLive = sources => new Promise(r => { release = () => r({ ok: true, sources: { '6': { state: 'live', live: rec(9),
    run: { prompt_id: 'old', finished: false } } }, seen: [], sampling: {} }); });
client.liveHint(); await settle();
c.projectChanged();
client.readLive = realRead;
release(); await settle(); await settle();
out.after = [c.paneModel(1).read?.live?.n ?? null, byteCalls.length];
""")
    assert out["after"] == [None, 0]


def test_sampling_pane_wording_while_adding_and_when_refused():
    out = run_live_coordinator("""
c.setVisible(true); c.setLayout({count: 2});
let resolve;
controls.applyChanges = () => new Promise(r => { resolve = r; });
const creating = c.createView(1, { path: '31' }, { feed: 'sampling' });
out.adding = c.paneModel(1).message;
resolve({ ok: false, rejected: [{}] }); await creating; await settle();
out.refused = notes.at(-1);
""")
    assert out["adding"] == "Adding this sampling preview…"
    assert out["refused"] == "The sampling preview could not be added as a View."


def test_sampling_media_never_rebuilds_a_preview_that_failed_to_decode():
    out = node_run(FAKE_DOM + f"""
const {{ mountSamplingMedia }} = await import({url("sampling_pane_media.js")});
let created = 0;
const container = document.createElement('div');
const media = mountSamplingMedia({{ container, createObjectURL: () => `blob:${{++created}}`, revokeObjectURL: () => {{}} }});
const layer = container.children[0].children[0];
const frame = {{ key: 'p1#3', mime: 'image/jpeg', blob: {{}} }};
media.show(frame);
layer.children[0].dispatch('error');
media.show(frame); media.show(frame);
const before = created;
media.show({{ key: 'p1#4', mime: 'image/jpeg', blob: {{}} }});
console.log('RESULT=' + JSON.stringify([before, created, layer.children.length]));
""")
    assert out == [1, 2, 1]


def test_views_hosts_node_state_canvas_reads_the_graph_others_cannot_tell():
    out = node_run(FAKE_DOM + f"""
const H = await import({url("views_hosts.js")});
const states = [];
const canvas = H.createCanvasViewsHost({{ controller: {{ node: {{ graph: {{ rootGraph: {{ id: 'wf' }} }} }} }}, apiURL: p => p,
    nodeState: path => (states.push(path), path === '32' ? {{ inactive: {{ mode: 'muted', by: 'self' }} }} : null) }});
const mounted = H.createMountedViewsHost({{ workflowId: 'wf', runOutputs: () => null, runRequests: () => null, controlsHost: () => null, apiURL: p => p }});
const sdk = H.createSdkViewsHost({{ host: {{ templateId: 't' }}, runOutputs: () => null, apiURL: p => p }});
const out = {{ canvas: [canvas.nodeState('32'), canvas.nodeState('6')], mounted: mounted.nodeState('32'), sdk: sdk.nodeState('32'), states }};
console.log('RESULT=' + JSON.stringify(out));
""")
    assert out == {"canvas": [{"inactive": {"mode": "muted", "by": "self"}}, None], "mounted": None, "sdk": None, "states": ["32", "6"]}


# ── Phase 5: pane count and arrangement ──────────────────────────────────────

def test_coordinator_rearranging_keeps_holds_and_reads_and_an_unchanged_layout_writes_nothing():
    out = run_coordinator("""
c.setVisible(true); c.setLayout({count: 2}); c.assign(1, {kind: 'view', viewId: 'a'}); await settle();
answer.sources = { '9': { state: 'ok', result: { prompt_id: 'p1', fresh: true, run: {}, entries: [{ index: 0, kind: 'image', file: { filename: 'a.png' } }] } } };
answer.holds = [{ hold_id: 'h1', state: 'ok', entry: { kind: 'image', file: { filename: 'a.png' } }, run: {} }];
client.hint(); await settle();
await c.hold(1); await settle();
audio.claim(c.paneId(1));
const before = [reads.length, demand.length, released.length];
clock += 5;
c.setLayout({arrangement: 'column'}); await settle();
out.rearranged = [c.paneModel(1).held, audio.owner() === c.paneId(1), reads.length - before[0], demand.length - before[1],
    released.length - before[2], stored.get('c:wf').arrangement, stored.get('c:wf').count];
// The same layout again (a click on the pressed segment) stores nothing.
const ts = stored.get('c:wf').ts;
clock += 5;
c.setLayout({arrangement: 'column'}); c.setLayout({count: 2}); c.setLayout({count: 2, arrangement: 'column'});
out.unchangedTs = stored.get('c:wf').ts === ts;
// Another tab rearranged: last write wins, nothing released.
stored.set('c:wf', { ...stored.get('c:wf'), arrangement: 'row', ts: ts + 100 });
c.storedLayoutChanged(); await settle();
out.synced = [c.layout().arrangement, c.paneModel(1).held];
// Shrinking the count still ends the hidden pane's hold.
c.setLayout({count: 1}); await settle();
out.shrunk = [released.length - before[2] > 0, audio.owner()];
""")
    assert out["rearranged"] == [True, True, 0, 0, 0, "column", 2]
    assert out["unchangedTs"] is True
    assert out["synced"] == ["row", True]
    assert out["shrunk"] == [True, "timeline"]


def test_panes_count_and_arrangement_bar_grid_focus_picker_and_no_remount():
    script = (COORDINATOR.replace("COORD_URL", url("views_coordinator.js")).replace("AUDIO_URL", url("audio_ownership.js"))
              + PANES.replace("PANES_URL", url("view_panes.js")).replace(
                  "placeComposite: () => {}", "placeComposite: body => placedBodies.push(body)").replace(
                  "let hidden = false", "const placedBodies = [];\nlet hidden = false") + r"""
const seg = (attr, value) => header.find(el => el.dataset?.[attr] === value);
const countSeg = n => seg('sonderPaneCount', String(n));
const arrSeg = a => seg('sonderPaneArrangement', a);
const group = label => header.find(el => el.getAttribute?.('aria-label') === label);
const css = el => el.style.cssText;
// One pane: the count group only; the selected segment stacks above its neighbour.
out.one = [header.all(el => el.dataset?.sonderPaneCount).map(el => el.dataset.sonderPaneCount),
    group('Pane arrangement').style.display, countSeg(1).getAttribute('aria-pressed'),
    css(countSeg(1)).includes('position: relative') && css(countSeg(1)).includes('z-index: 1'),
    css(countSeg(2)).includes('z-index: 0'), css(countSeg(1)).includes('margin-left:-1px'), css(countSeg(2)).includes('margin-left:-1px'),
    arrSeg('row').getAttribute('aria-label')];
// Two panes: Grid hidden, Row pressed for the stored Grid; a click on it keeps Grid.
countSeg(2).click(); flushRaf();
out.two = [group('Pane arrangement').style.display, css(arrSeg('grid')), arrSeg('row').getAttribute('aria-pressed'),
    css(arrSeg('row')).includes('margin-left:-1px'), css(arrSeg('column')).includes('margin-left:-1px'), c.layout().arrangement];
arrSeg('row').click(); flushRaf();
out.rowKept = c.layout().arrangement;
// Bars sit in the cells and wrap there.
out.wrap = root.children.map(cell => [cell.children[0].style.flexWrap, cell.children[0].children.at(-1).style.flexWrap, cell.children[0].children.at(-1).style.flex]);
// Pane 2 shows the Guides: its content survives every arrangement.
c.assign(1, { kind: 'guides' }); flushRaf();
const body1 = root.children[1].children[1];
const content = [root.children[0], root.children[1], body1.children[0]];
const acquired = acquires;
const placedBefore = placedBodies.length;
arrSeg('column').click(); flushRaf();
out.column = [css(root).includes('grid-template-columns:1fr;'), css(root).includes('grid-template-rows:repeat(2, 1fr);'), c.layout().arrangement];
countSeg(3).click(); flushRaf();
arrSeg('grid').click(); flushRaf();
out.grid3 = [css(root).includes('grid-template-columns:2fr 1fr;'), css(root).includes('grid-template-rows:1fr 1fr;'),
    root.children.map(cell => cell.style.gridRow), arrSeg('grid').getAttribute('aria-pressed'), css(arrSeg('grid')).includes('margin-left:-1px')];
arrSeg('row').click(); flushRaf();
out.row3 = [css(root).includes('grid-template-columns:repeat(3, 1fr);'), root.children[0].style.gridRow];
out.kept = [root.children[0] === content[0], root.children[1] === content[1], root.children[1].children[1].children[0] === content[2],
    acquires === acquired, placedBodies.slice(placedBefore).every(b => b === root.children[0].children[1])];
countSeg(4).click(); arrSeg('grid').click(); flushRaf();
out.grid4 = [css(root).includes('grid-template-columns:1fr 1fr;'), root.children.map(cell => cell.style.gridRow)];
// Keyboard focus draws an inset ring at once (no render needed); a mouse
// focus (not :focus-visible) and blur draw none.
countSeg(3).matches = sel => sel === ':focus-visible';
countSeg(3).dispatch('focus');
out.focus = css(countSeg(3)).includes('outline:2px solid') && css(countSeg(3)).includes('outline-offset:-3px');
countSeg(3).dispatch('blur');
out.blur = css(countSeg(3)).includes('outline:none');
countSeg(3).matches = () => false;
countSeg(3).dispatch('focus');
out.mouseFocus = css(countSeg(3)).includes('outline:none');
countSeg(3).dispatch('blur');
// The numbers are named by their text; the icons by their title.
out.names = [countSeg(2).getAttribute('aria-label'), countSeg(2).title, arrSeg('grid').getAttribute('aria-label')];
// An open picker closes when the arrangement changes.
root.find(el => el.dataset?.sonderPaneSource === '0').click(); flushRaf();
out.pickerOpen = !!document.body.find(el => el.dataset?.sonderPanePicker === 'true');
arrSeg('column').click(); flushRaf();
out.pickerClosed = !document.body.find(el => el.dataset?.sonderPanePicker === 'true');
// One pane: the bar moves to the header and does not wrap.
countSeg(1).click(); flushRaf();
out.headerBar = [header.children[0].children[0]?.style.flexWrap, header.children[0].children[0]?.children.at(-1).style.flexWrap];
panes.destroy();
out.teardown = header.children.length;
""")
    out = node_run(script + "\nconsole.log('RESULT=' + JSON.stringify(out));")
    assert out["one"] == [["1", "2", "3", "4"], "none", "true", True, True, False, True, "Row: panes side by side"]
    assert out["two"] == ["flex", "display:none;", "true", False, True, "grid"]
    assert out["rowKept"] == "grid"
    assert out["wrap"] == [["wrap", "wrap", "0 1 auto"], ["wrap", "wrap", "0 1 auto"]]
    assert out["column"] == [True, True, "column"]
    assert out["grid3"] == [True, True, ["1 / 3", "", ""], "true", False]
    assert out["row3"] == [True, ""]
    assert out["kept"] == [True, True, True, True, True]
    assert out["grid4"] == [True, ["", "", "", ""]]
    assert out["focus"] is True and out["blur"] is True and out["mouseFocus"] is True
    assert out["names"] == [None, "Two panes", "Grid: two by two, or one large pane and two stacked"]
    assert out["pickerOpen"] is True and out["pickerClosed"] is True
    assert out["headerBar"] == ["nowrap", "nowrap"]
    assert out["teardown"] == 0
