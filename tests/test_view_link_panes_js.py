"""Output Views Phase 3B in the panes: linking, chips, Previous context, the
linked media mode, the hosts' Editor keys and audio ownership under random
pane operations.
"""

import json

from test_view_panes_js import FAKE_DOM, node_run, url

COORD = FAKE_DOM + r"""
const { createViewsCoordinator, PREVIOUS_CONTEXT_PLAYING_MS } = await import(COORD_URL);
const { createAudioOwnership } = await import(AUDIO_URL);
const T = await import(TIMING_URL);
const out = {};
const content = CONTENT;
let answer = { ok: true, sources: {}, runs: [], holds: [] };
const client = {
    async readOutputs() { return JSON.parse(JSON.stringify(answer)); },
    async hold() { return { ok: true, hold_id: 'h1' }; }, async release() { return { ok: true }; },
    async releaseAll() { return { ok: true }; }, onOutputsChanged(fn) { client.hint = fn; return () => {}; },
};
const host = { mode: 'canvas', workflowKey: () => 'c:wf', capability: async () => ({ ok: true }), runOutputs: () => client,
    runBranch: async () => ({ state: 'queued', promptId: 'p9' }), cancel: async () => ({ ok: true }), resolve: o => o,
    setViewsDemand() {}, onSessionLost: () => () => {}, onSessionRestored: () => () => {}, editorKey: () => '5' };
const timing = { mode: 'editor_window', anchor: 'render_start', source_offset: 0, fps: 'editor' };
let controlsView = { state: 'ready', views: [{ view_id: 'a', status: 'ok', participation: 'active', path: '9', label: 'Out', timing }] };
const controls = { getView: () => controlsView, subscribe: () => () => {}, applyChanges: async () => ({ ok: true }), refresh: async () => {} };
const stored = new Map();
const layoutStore = { read: k => stored.get(k) || null, write: (k, r) => (r ? stored.set(k, JSON.parse(JSON.stringify(r))) : stored.delete(k)) };
const audio = createAudioOwnership();
let clock = 1000, playing = false, sceneObj = SCENE;
const scene = { projectId: () => 'P', sceneId: () => 's1', fps: () => 24, size: () => ({ width: 640, height: 360 }),
    scene: () => sceneObj, playing: () => playing, assetsLoaded: () => true, assetForSourcePath: () => null };
const c = createViewsCoordinator({ host: () => host, controls, layoutStore, audio, scene, now: () => clock,
    setTimer: () => 0, clearTimer: () => {}, raf: fn => (fn(), 1), documentRef: document });
const settle = async () => { for (let i = 0; i < 6; i++) await new Promise(r => setTimeout(r, 0)); };
c.start(); c.setVisible(true);
const ctx = { project_folder: 'P', scene_id: 's1', fps: 24, render_start: 10, render_end: 40, generation_start: 14, generation_end: 34,
    source_frame_count: 30, content };
const result = (over = {}) => ({ prompt_id: 'p1', fresh: true, entries: [{ index: 0, kind: 'video', file: { filename: 'o.mp4' }, revision: '1:1' }],
    editor_context: { '5': ctx }, ...over });
"""


def run_coord(body, content, scene):
    script = (COORD.replace("COORD_URL", url("views_coordinator.js")).replace("AUDIO_URL", url("audio_ownership.js"))
              .replace("TIMING_URL", url("view_timing.js")).replace("CONTENT", json.dumps(content)).replace("SCENE", json.dumps(scene)))
    return node_run(script + body + "\nconsole.log('RESULT=' + JSON.stringify(out));")


def _record():
    from test_view_timing import base_scene, live_record
    return live_record(base_scene()), base_scene()


def test_link_chips_previous_context_toggle_and_reset():
    record, scene = _record()
    out = run_coord("""
c.assign(0, { kind: 'view', viewId: 'a' });
answer.sources = { '9': { state: 'ok', result: result() } };
client.hint(); await settle();
let m = c.paneModel(0);
out.linked = [m.linkable, m.linked, m.link.kind, m.link.S0, m.link.S1, m.chip];
// No metadata yet: the length is unchecked.
out.linking = [m.link.pending, m.message];
// The scene changes in the range: Previous context, after a paused render.
sceneObj = JSON.parse(JSON.stringify(sceneObj));
sceneObj.clips[0].opacity = 0.5;
m = c.paneModel(0);
out.changed = m.chip;
// Playing: checked at most every PREVIOUS_CONTEXT_PLAYING_MS.
playing = true; sceneObj.clips[0].opacity = 1;
c.paneModel(0);
out.throttled = c.paneModel(0).chip;  // still the last answer within the interval
clock += PREVIOUS_CONTEXT_PLAYING_MS + 1;
out.afterInterval = c.paneModel(0).chip;
playing = false;
// The follower reports: Catching up wins over link chips; outside shows its message.
c.setFollowerState(0, 'catching_up');
out.catching = c.paneModel(0).chip;
c.setFollowerState(0, 'outside');
m = c.paneModel(0);
out.outside = [m.message, m.outside];
c.setFollowerState(0, null);
// Too short once the length is known.
c.setMediaDuration(0, 'output//o.mp4#1:1', 0.5);
out.short = c.paneModel(0).chip;
c.setMediaDuration(0, 'output//o.mp4#1:1', 5);
out.known = c.paneModel(0).message;
// A known non-finite duration links without the check, and is not pending.
c.setMediaDuration(0, 'output//o.mp4#1:1', Infinity);
out.infinite = [c.paneModel(0).linked, c.paneModel(0).message];
c.setMediaDuration(0, 'output//o.mp4#1:1', 5);
// Link off: no link chip, persisted; a new source turns it back on.
c.setPaneOption(0, { link: false });
m = c.paneModel(0);
out.off = [m.linked, m.chip, stored.get('c:wf').panes[0].link];
c.assign(0, { kind: 'guides' }); c.assign(0, { kind: 'view', viewId: 'a' });
out.reset = c.layout().panes[0].link;
// Another scene: unlinked with its reason; a reused result without context.
answer.sources = { '9': { state: 'ok', result: result({ editor_context: { '5': { ...ctx, scene_id: 's2' } } }) } };
client.hint(); await settle();
out.otherScene = c.paneModel(0).chip;
answer.sources = { '9': { state: 'ok', result: result({ fresh: false, editor_context: null }) } };
client.hint(); await settle();
out.reused = c.paneModel(0).chip;
// An image never links and shows no Link chip.
answer.sources = { '9': { state: 'ok', result: result({ entries: [{ index: 0, kind: 'image', file: { filename: 'a.png' } }] }) } };
client.hint(); await settle();
m = c.paneModel(0);
out.image = [m.linkable, m.link, m.chip];
// A held result links with the context its hold kept.
answer.sources = { '9': { state: 'ok', result: result() } };
answer.holds = [{ hold_id: 'h1', state: 'ok', entry: result().entries[0], run: {}, editor_context: { '5': ctx }, fresh: true }];
client.hint(); await settle();
await c.hold(0); await settle(); client.hint(); await settle();
m = c.paneModel(0);
out.held = [m.held, m.linked, m.chip];
c.destroy();
""", record, scene)
    assert out["linked"] == [True, True, "authored", 10, 40, None]
    assert out["linking"] == [True, "Linking…"]
    assert out["known"] is None and out["infinite"] == [True, None]
    assert out["changed"] == "Previous context"
    assert out["throttled"] == "Previous context"
    assert out["afterInterval"] is None
    assert out["catching"] == "Catching up"
    assert out["outside"] == ["Outside generated range", True]
    assert out["short"] == "Unlinked: Shorter than the range it was made for"
    assert out["off"] == [False, None, False]
    assert out["reset"] is True
    assert out["otherScene"] == "Unlinked: Made for another scene"
    assert out["reused"] == "Unlinked: A reused result whose timing isn't known · From cache"
    assert out["image"] == [False, None, None]
    assert out["held"] == [True, True, "Held"]


MEDIA = FAKE_DOM + r"""
const { mountOutputMedia, CATCHING_UP_DELAY_MS } = await import(MEDIA_URL);
const { createAudioOwnership } = await import(AUDIO_URL);
const out = {};
class MediaEl extends El {
    constructor(tag) { super(tag); this._t = 0; this.paused = true; this.readyState = 4; this.playbackRate = 1; this.loop = false; this.muted = false; this.duration = 2; }
    get currentTime() { return this._t; }
    set currentTime(v) { this._t = v; queueMicrotask(() => this.dispatch('seeked')); }
    pause() { this.paused = true; } play() { this.paused = false; return Promise.resolve(); } load() {}
}
const create = document.createElement;
document.createElement = tag => (tag === 'video' || tag === 'audio' ? new MediaEl(tag) : create(tag));
const clockState = { frame: 20, playing: false, hold: null, fps: 24, anchor: {} };
const listeners = new Set();
const clock = { get: () => ({ ...clockState }), subscribe: fn => { listeners.add(fn); return () => listeners.delete(fn); } };
const emit = e => { for (const fn of [...listeners]) fn(e || {}); };
const audio = createAudioOwnership();
const cache = { acquire: () => ({ ready: Promise.resolve({ url: 'blob:x' }), release() {} }) };
const container = new El('div');
const states = [];
const link = { map: c => (c >= 10.5 && c < 40.5 ? { inRange: true, sourceTime: (c - 10) / 24 } : { inRange: false }), audioMap: () => null, Fm: 24, sig: 'a' };
const settle = async () => { for (let i = 0; i < 6; i++) await new Promise(r => setTimeout(r, 0)); };
const handle = mountOutputMedia({ container, entry: { kind: 'video', file: { filename: 'o.mp4' }, fps: 24 }, cache, paneId: 'pane:0', audio,
    clock, link, onFollowerState: s => states.push(s) });
await settle();
const video = container.find(el => el.tagName === 'VIDEO');
const transport = container.children[1];
out.linked = [video.loop, transport.style.display, Math.round(video.currentTime * 24 * 100) / 100];
clockState.frame = 50; emit({ discontinuity: true }); await settle();
out.outside = [video.style.visibility, states.at(-1), video.paused];
// Back in range: hidden until the seek lands (the element still holds its old frame).
let hiddenWhileSeeking = null;
const setter = Object.getOwnPropertyDescriptor(MediaEl.prototype, 'currentTime').set;
Object.defineProperty(video, 'currentTime', { configurable: true, get() { return this._t; },
    set(v) { hiddenWhileSeeking = this.style.visibility; setter.call(this, v); } });
clockState.frame = 30; emit({ discontinuity: true }); await settle();
out.back = [hiddenWhileSeeking, video.style.visibility, Math.round(video.currentTime * 24 * 100) / 100];
delete video.currentTime;
handle.setLink(null); await settle();
out.unlinked = [video.loop, transport.style.display, listeners.size];
handle.setLink(link); await settle();
out.relinked = listeners.size;
handle.destroy();
out.destroyed = listeners.size;
"""


def test_linked_media_follows_hides_outside_and_unlinks():
    script = (MEDIA.replace("MEDIA_URL", url("output_pane_media.js")).replace("AUDIO_URL", url("audio_ownership.js")))
    out = node_run(script + "\nconsole.log('RESULT=' + JSON.stringify(out));")
    assert out["linked"] == [False, "none", 10.5]  # frame 20's centre: (20.5 - 10) source frames
    assert out["outside"] == ["hidden", "outside", True]
    assert out["back"] == ["hidden", "", 20.5]
    assert out["unlinked"] == [True, "flex", 0]
    assert out["relinked"] == 1
    assert out["destroyed"] == 0


def test_hosts_name_their_editor():
    out = node_run(FAKE_DOM + f"""
const H = await import({url("views_hosts.js")});
const root = {{ id: 'wf' }};
root.rootGraph = root;
const inner = {{ id: 'sub', rootGraph: root }};
const canvas = node => H.createCanvasViewsHost({{ controller: {{ node, runOutputs: null, runBranch: async () => ({{}}) }}, apiURL: p => p }}).editorKey();
const sdk = editors => H.createSdkViewsHost({{ host: {{ templateId: 't' }}, runOutputs: () => null, apiURL: p => p,
    controlsHost: () => ({{ getView: () => ({{ editors }}) }}) }}).editorKey();
console.log('RESULT=' + JSON.stringify([
    canvas({{ id: 5, graph: root }}), canvas({{ id: 12, graph: inner }}),
    H.createMountedViewsHost({{ workflowId: 'wf', editorKey: '7', runOutputs: () => null, runRequests: () => null, controlsHost: () => null, apiURL: p => p }}).editorKey(),
    sdk(['3']), sdk(['3', '4']), sdk(undefined),
]));
""")
    assert out == ["5", "", "7", "3", "", ""]


def test_one_audible_source_under_random_pane_operations():
    record, scene = _record()
    out = run_coord("""
let seed = 11;
const rand = n => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed % n; };
answer.sources = { '9': { state: 'ok', result: result() } };
const layouts = ['1', '2c', '3c', '2x2'];
const sources = [{ kind: 'view', viewId: 'a' }, { kind: 'guides' }, { kind: 'composite' }];
let bad = null;
for (let sequence = 0; sequence < 50 && !bad; sequence++) {
    for (let step = 0; step < 20; step++) {
        const op = rand(6), pane = rand(4);
        if (op === 0) c.setLayout(layouts[rand(4)]);
        else if (op === 1) c.assign(pane, sources[rand(3)]);
        else if (op === 2 && pane < c.visibleCount()) audio.claim(c.paneId(pane));
        else if (op === 3) audio.release(audio.owner());
        else if (op === 4) c.setMediaCondition(pane, rand(2) ? 'missing_file' : null);
        else c.setPaneOption(pane, { link: !!rand(2) });
        const owner = audio.owner();
        const audible = ['timeline', 0, 1, 2, 3].filter(id => audio.audible(id === 'timeline' ? id : c.paneId(id)));
        const ownerIndex = owner === 'timeline' ? -1 : Number(owner.split(':')[1]);
        // One audible source, and a pane owner is a visible pane.
        if (audible.length !== 1 || (ownerIndex >= c.visibleCount())) { bad = { sequence, step, owner, audible }; break; }
    }
}
out.bad = bad;
c.setVisible(false);
out.afterClose = audio.owner();
""", record, scene)
    assert out["bad"] is None
    assert out["afterClose"] == "timeline"


def test_manage_views_timing_writes_one_change_and_refuses_bad_values():
    out = node_run(FAKE_DOM + f"""
const {{ mountViewsManage }} = await import({url("controls_views_manage.js")});
const {{ register }} = await import({url("keyboard_ownership.js")});
const out = {{}};
let view = {{ views: [{{ view_id: 'a', label: 'Out', default_label: 'Save Video', order: 0, origin: 'authored', status: 'ok',
    participation: 'active', timing: null }}] }};
const applied = [], notes = [];
const host = {{ getView: () => view, capabilities: {{}}, applyChanges: async changes => {{
    applied.push(changes);
    for (const [target, value] of Object.entries(changes)) view = {{ views: view.views.map(d => ({{ ...d, timing: value }})) }};
    return {{ ok: true }};
}} }};
const manage = mountViewsManage(host, {{ notify: m => notes.push(m) }});
const row = () => manage.el.find(el => el.dataset?.viewTiming === 'a');
const parts = () => {{ const r = row(); return {{ anchor: r.find(el => el.tagName === 'SELECT'), inputs: r.all(el => el.tagName === 'INPUT') }}; }};
const settle = () => new Promise(r => setTimeout(r, 20));
out.start = [parts().anchor.value, parts().inputs.map(i => i.disabled)];
let p = parts();
p.anchor.value = 'render_start'; p.anchor.dispatch('change'); await settle();
p = parts();
p.inputs[0].value = '3'; p.inputs[1].value = '12'; p.inputs[1].dispatch('blur'); await settle();
p = parts();
p.inputs[1].value = '0'; p.inputs[1].dispatch('blur'); await settle();
out.afterBad = [parts().inputs[1].value, notes.length];
p = parts(); p.anchor.value = ''; p.anchor.dispatch('change'); await settle();
out.applied = applied;
manage.destroy();
console.log('RESULT=' + JSON.stringify(out));
""")
    assert out["start"] == ["", [True, True]]
    assert out["applied"] == [
        {"view:a:timing": {"mode": "editor_window", "anchor": "render_start", "source_offset": 0, "fps": "editor"}},
        {"view:a:timing": {"mode": "editor_window", "anchor": "render_start", "source_offset": 3, "fps": 12}},
        {"view:a:timing": None},
    ]
    assert out["afterBad"] == ["12", 1]


def test_a_linked_pane_that_fails_to_decode_stops_seeking():
    """Audit (Phase 3B): a decode failure left the follower seeking a dead
    element, holding a page seek slot per scrub step."""
    script = (MEDIA.split("const video = container.find")[0]
              .replace("MEDIA_URL", url("output_pane_media.js")).replace("AUDIO_URL", url("audio_ownership.js")))
    out = node_run(script + r"""
const video = container.find(el => el.tagName === 'VIDEO');
let writes = 0;
const setter = Object.getOwnPropertyDescriptor(MediaEl.prototype, 'currentTime').set;
Object.defineProperty(video, 'currentTime', { configurable: true, get() { return this._t; }, set(v) { writes += 1; setter.call(this, v); } });
video.dispatch('error'); await settle();
clockState.frame = 30; emit({ discontinuity: true }); await settle();
out.failed = [writes, states.at(-1)];
video.videoWidth = 320; video.videoHeight = 64; video.dispatch('loadeddata'); await settle();   // a reload that decodes
clockState.frame = 31; emit({ discontinuity: true }); await settle();
out.recovered = writes > 0;
handle.destroy();
console.log('RESULT=' + JSON.stringify(out));
""")
    assert out["failed"] == [0, "unavailable"]
    assert out["recovered"] is True
