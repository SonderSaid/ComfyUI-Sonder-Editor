"""Inspecting View panes (UX pass Phase 6): the session of items and their media
(view_inspect_items.js), the shared waveform helper, the pane bar's Inspect, and
the gallery overlay's external-item mode.

The overlay's rules are pure predicates (inspect_overlay_scope.js), run as
imported. Its session, scope and live-swap functions are sliced from the gallery
onto small stubs; only the renderers are stubbed.
"""
import json
from pathlib import Path
import re
import tempfile

from test_view_panes_js import COORDINATOR, FAKE_DOM, JS, LIVE_CLIENT, VIEW_RESULT, node_run, url

SCOPE = url("inspect_overlay_scope.js")


SESSION = r"""
const { openInspectSession, inspectKind, MAX_PEAKS, NOTE_NEWER, NOTE_OTHER_SOURCE } = await import(ITEMS_URL);
const out = {};
const holders = [], released = [];
const gates = new Map();
// A cache whose claims resolve when told to (`gate`), or at once.
const cache = { acquire(entry, holder) {
    holders.push([entry.file.filename, holder]);
    const ready = gates.get(entry.file.filename)?.promise || Promise.resolve({ url: `blob:${entry.file.filename}` });
    return { ready, release: () => released.push(holder) };
} };
const gate = name => { let resolve; const promise = new Promise(r => { resolve = r; }); gates.set(name, { promise, resolve }); return value => resolve(value); };
const created = [], revoked = [];
let n = 0;
const urls = { createObjectURL: blob => { const u = `blob:live-${++n}`; created.push(u); return u; }, revokeObjectURL: u => revoked.push(u) };
const entry = (name, kind = 'image', extra = {}) => ({ kind, file: { filename: name }, revision: '1', ...extra });
const view = (id, label = id, extra = {}) => ({ view_id: id, label, path: `1:${id}`, class_type: 'PreviewImage', title: 'Preview Image', ...extra });
const output = (index, id, e, extra = {}) => ({ index, source: { kind: 'view', viewId: id }, view: view(id), entry: e, entries: e ? [e] : [],
    itemIndex: 0, loaded: true, condition: null, chip: null, held: false, ...extra });
const sampling = (index, id, frame, extra = {}) => ({ index, source: { kind: 'view', viewId: id }, sampling: true, view: view(id, id, { class_type: 'KSampler', title: 'KSampler' }),
    frame, condition: null, chip: 'Sampling · step 1/4', read: { live: {} }, ...extra });
const frame = (key, source = '6', mime = 'image/jpeg') => ({ key, source, mime, blob: { key } });
const settle = async () => { for (let i = 0; i < 4; i++) await new Promise(r => setTimeout(r, 0)); };
const open = (models, anchorIndex, extra = {}) => openInspectSession({ models, cache, anchorIndex, fps: 24, ...urls, ...extra });
""".replace("ITEMS_URL", url("view_inspect_items.js"))


def run_session(body: str):
    return node_run(SESSION + body + "\nconsole.log('RESULT=' + JSON.stringify(out));")


def test_what_a_pane_can_inspect_and_as_what():
    out = run_session("""
const k = inspectKind;
out.kinds = ['image', 'animated_image', 'video', 'audio'].map(kind => k(output(0, 'a', entry('x', kind))));
out.hidden = [
    k(output(0, 'a', entry('x', 'mesh'))),
    k(output(0, 'a', entry('x.exr'))),
    k(output(0, 'a', entry('x', 'image', { available: false }))),
    k(output(0, 'a', entry('x'), { condition: 'decode' })),
    k(output(0, 'a', entry('x'), { loaded: false })),
    k(output(0, 'a', null)),
    k(sampling(0, 's', null)),
    k(sampling(0, 's', frame('p#1'), { condition: 'too_large' })),
    k(sampling(0, 's', frame('p#1', '6', 'image/gif'))),
    ...['composite', 'guides', 'driver'].map(kind => k({ index: 0, source: { kind }, loaded: true })),
];
out.sampling = ['image/jpeg', 'image/png', 'image/webp', 'video/mp4'].map(mime => k(sampling(0, 's', frame('p#1', '6', mime))));
""")
    assert out["kinds"] == ["image", "image", "video", "audio"]
    assert out["hidden"] == [None] * len(out["hidden"])
    assert out["sampling"] == ["image", "image", "image", "video"]


def test_items_are_panes_in_order_identical_media_merge_and_titles_differ():
    out = run_session("""
const held = output(0, 'a', entry('old.png'), { held: true, chip: 'Held' });
const newest = output(1, 'a', entry('new.png'), { chip: 'Unlinked · From cache', result: { fresh: false }, entries: [entry('new.png'), entry('b.png')] });
let s = open([held, newest, { index: 2, source: { kind: 'composite' } }, sampling(3, 's', frame('p1#2'))], 0, { startTime: 1.5, subgraphNames: { 1: 'Upscale' } });
await s.ready; await settle();
out.rows = s.items.map(i => [i.id, i.title, i.kind, i.startTime, i.live]);
out.details = s.items[1].details;
// A snapshot leaves the pane's own chip (a run, a link, Held) behind.
out.status = s.items.map(i => i.status);
out.place = s.items.map(i => i.place);
s.release();
// The identical file in two panes is one item: the lower pane, or the anchor's own.
s = open([output(0, 'a', entry('x.png')), output(1, 'a', entry('x.png'), { held: true })], 0);
await s.ready; out.lower = s.items.map(i => i.id); s.release();
s = open([output(0, 'a', entry('x.png')), output(1, 'a', entry('x.png'), { held: true })], 1);
await s.ready; out.anchor = [s.items.map(i => i.id), s.anchorId]; s.release();
// Another revision of the same path is another file.
s = open([output(0, 'a', entry('x.png')), output(1, 'a', entry('x.png', 'image', { revision: '2' }))], 0);
await s.ready; await settle(); out.revision = s.items.length; s.release();
// Two panes on one sampler merge; two samplers of one run that reached the same n do not.
s = open([sampling(0, 's', frame('p1#2')), sampling(1, 's', frame('p1#2'))], 0);
out.oneSampler = s.items.length; s.release();
s = open([sampling(0, 's', frame('p1#2', '6')), sampling(1, 't', frame('p1#2', '7'))], 0);
out.twoSamplers = s.items.length; s.release();
""")
    assert out["rows"] == [["pane:0", "a · Pane 1 · Held", "image", 1.5, False], ["pane:1", "a · Pane 2", "image", 0, False],
                           ["pane:3", "s · Pane 4", "image", 0, True]]
    assert out["details"] == [["View", "a"], ["Node", "Upscale › Preview Image (PreviewImage)"], ["Kind", "Image"],
                              ["Status", "From cache"], ["Item", "1/2"]]
    assert out["status"] == ["", "From cache", "Sampling · step 1/4"]
    assert out["place"] == ["Pane 1 · Held", "Pane 2", "Pane 4"]
    assert out["lower"] == ["pane:0"]
    assert out["anchor"] == [["pane:1"], "pane:1"]
    assert out["revision"] == 2 and out["oneSampler"] == 1 and out["twoSamplers"] == 2


def test_frame_rate_prefers_the_media_then_timing_then_kj_then_the_scene():
    out = run_session("""
const rate = async model => { const s = open([model], 0); await s.ready; const value = s.items[0].fps; s.release(); return value; };
out.entry = await rate(output(0, 'a', entry('v', 'video', { fps: 25 }), { view: view('a', 'a', { timing: { fps: 12 } }) }));
out.timing = await rate(output(0, 'a', entry('v', 'video'), { view: view('a', 'a', { timing: { fps: 12 } }) }));
out.editor = await rate(output(0, 'a', entry('v', 'video'), { view: view('a', 'a', { timing: { fps: 'editor' } }) }));
out.kj = await rate(sampling(0, 's', frame('p#1', '6', 'video/mp4'), { read: { live: { fps: 8 } } }));
out.scene = await rate(sampling(0, 's', frame('p#1')));
""")
    assert out == {"entry": 25, "timing": 12, "editor": 24, "kj": 8, "scene": 24}


def test_the_anchor_opens_first_others_join_failures_drop_and_an_abort_lets_everything_go():
    out = run_session("""
const b = gate('b.png'), c = gate('c.png');
let s = open([output(0, 'a', entry('a.png')), output(1, 'b', entry('b.png')), output(2, 'c', entry('c.png'))], 0);
const events = [];
s.subscribe(e => events.push(e));
out.ready = await s.ready;
out.first = s.items.map(i => i.id);
b({ url: 'blob:b' }); await settle();
c({ error: 'missing_file' }); await settle();
out.joined = [s.items.map(i => i.id), events, released.length];
s.release(); s.release();
out.released = [released.length, s.items.length, s.urlOf('pane:0')];
// An anchor that can't load opens nothing and holds nothing.
released.length = 0;
const bad = gate('bad.png');
s = open([output(0, 'a', entry('bad.png')), output(1, 'b', entry('ok.png'))], 0);
bad({ error: 'failed' });
out.failed = [await s.ready, s.released, released.length];
// Abandoned while the anchor loads: every holder goes, and a late answer changes nothing.
released.length = 0;
const slow = gate('slow.png'), controller = new AbortController();
s = open([output(0, 'a', entry('slow.png')), output(1, 'b', entry('ok.png')), sampling(2, 's', frame('p#1'))], 0, { signal: controller.signal });
controller.abort();
out.aborted = [s.released, released.length, s.items.length];
slow({ url: 'blob:slow' });
out.late = [await s.ready, released.length];
out.nothing = await open([], 0).ready;
out.distinct = new Set(holders.map(h => h[1])).size === holders.length;
""")
    assert out["ready"] == {"ok": True}
    assert out["first"] == ["pane:0"]
    assert out["joined"] == [["pane:0", "pane:1"], [{"type": "joined", "id": "pane:1"}], 1]
    assert out["released"] == [3, 0, ""]
    assert out["failed"] == [{"error": "failed"}, True, 2]
    assert out["aborted"] == [True, 2, 0]
    assert out["late"] == [{"error": "aborted"}, 2]
    assert out["nothing"] == {"error": "nothing"}
    assert out["distinct"]  # every session holds under its own names


def test_an_output_item_is_a_snapshot_that_notes_what_its_pane_does():
    out = run_session("""
const s = open([output(0, 'a', entry('a.png'))], 0);
await s.ready;
const events = [];
s.subscribe(e => events.push(e.type));
const item = s.items[0];
const note = () => [item.note, s.urlOf('pane:0')];
s.refresh([output(0, 'a', entry('a.png'))]);
out.same = note();
s.refresh([output(0, 'a', entry('new.png'), { chip: 'Sampling' })]);
out.newer = note();
s.refresh([output(0, 'a', null)]);
out.disconnected = note();
s.refresh([output(0, 'z', entry('z.png'))]);
out.other = note();
s.refresh([]);
out.closed = note();
s.refresh([output(0, 'a', entry('a.png'))]);
out.back = note();
out.events = events;
out.still = [holders.length, released.length];
s.release();
out.after = released.length;
""")
    url_ = "blob:a.png"
    assert out["same"] == out["disconnected"] == out["back"] == ["", url_]
    assert out["newer"] == ["A newer result is in the pane", url_]
    assert out["other"] == out["closed"] == ["This pane now shows another source", url_]
    assert out["events"] == ["status"] * 4
    assert out["still"] == [1, 0] and out["after"] == 1


def test_a_sampling_item_follows_its_pane_keeps_the_last_picture_and_freezes_when_it_goes():
    out = run_session("""
const s = open([sampling(0, 's', frame('p1#1'))], 0);
await s.ready;
const events = [];
s.subscribe(e => events.push([e.type, e.id]));
const item = s.items[0];
const first = s.urlOf('pane:0');
out.stable = s.urlOf('pane:0') === first;
s.refresh([sampling(0, 's', frame('p1#1'))]);
out.unchanged = events.length;
// A new run before its first picture: the last one stays, the status moves.
s.refresh([sampling(0, 's', null, { chip: 'Running' })]);
out.waiting = [s.urlOf('pane:0') === first, item.status, item.details.find(r => r[0] === 'Status')];
// The next run's first picture, though its n is the same.
s.refresh([sampling(0, 's', frame('p2#1'), { chip: 'Sampling · step 1/4' })]);
out.swapped = [s.urlOf('pane:0') !== first, events.slice(-2)];
// Its View can't be read (the canvas is away): only the status says so.
const named = item.details.filter(r => r[0] !== 'Status');
s.refresh([{ ...sampling(0, 's', null), sampling: undefined, view: null, chip: 'Unavailable' }]);
out.unread = [JSON.stringify(item.details.filter(r => r[0] !== 'Status')) === JSON.stringify(named), item.status, item.note];
// A frame its own pane could not decode is not taken.
const kept = s.urlOf('pane:0');
s.refresh([sampling(0, 's', frame('p2#1b'), { condition: 'decode', chip: 'Sampling · step 1/4' })]);
out.undecodable = s.urlOf('pane:0') === kept;
// A preview type that can't show keeps the last picture too.
const second = s.urlOf('pane:0');
s.refresh([sampling(0, 's', frame('p2#2', '6', 'image/gif'))]);
out.unsupported = s.urlOf('pane:0') === second;
// Images gave way to a video (KJ): the overlay needs another stage.
s.refresh([sampling(0, 's', frame('p2#3', '6', 'video/mp4'))]);
out.kind = [item.kind, events.at(-1)];
// Its pane shows another source: frozen, also once it is back.
const last = s.urlOf('pane:0');
s.refresh([sampling(0, 'other', frame('p9#1'))]);
s.refresh([sampling(0, 's', frame('p3#1', '6', 'video/mp4'), { chip: 'Later' })]);
out.frozen = [s.urlOf('pane:0') === last, item.note, item.status];
s.release();
""")
    assert out["stable"] and out["unchanged"] == 0
    assert out["waiting"] == [True, "Running", ["Status", "Running"]]
    assert out["swapped"] == [True, [["media", "pane:0"], ["status", "pane:0"]]]
    assert out["unread"] == [True, "Unavailable", ""] and out["undecodable"]
    assert out["unsupported"]
    assert out["kind"] == ["video", ["kind", "pane:0"]]
    assert out["frozen"] == [True, "This pane now shows another source", "Sampling · step 1/4"]


def test_a_preview_url_lives_until_a_newer_one_is_on_screen():
    out = run_session("""
const s = open([sampling(0, 's', frame('p1#1'))], 0);
await s.ready;
out.lazy = created.length;  // an item no stage asks for makes no URL
for (const key of ['p1#2', 'p1#3']) s.refresh([sampling(0, 's', frame(key))]);
out.stillLazy = created.length;
const a = s.urlOf('pane:0'); s.markShown('pane:0', a);
s.refresh([sampling(0, 's', frame('p1#4'))]);
const b = s.urlOf('pane:0');
s.refresh([sampling(0, 's', frame('p1#5'))]);
const c = s.urlOf('pane:0');
out.loading = revoked.slice();  // b and c may still be loading, a is on screen
s.markShown('pane:0', b);
out.b = revoked.slice();
s.markShown('pane:0', c);
out.c = revoked.slice();
// One that fails goes back to the last shown and is not taken again.
s.refresh([sampling(0, 's', frame('p1#6'))]);
const d = s.urlOf('pane:0'); s.markFailed('pane:0', d);
out.back = [s.urlOf('pane:0') === c, revoked.at(-1) === d];
s.refresh([sampling(0, 's', frame('p1#6'))]);
out.notAgain = s.urlOf('pane:0') === c;
s.markFailed('pane:0', c); s.markFailed('pane:0', 'blob:x');
out.lastStays = s.urlOf('pane:0') === c;
revoked.pop(); created.splice(created.indexOf(d), 1);
s.markShown('pane:0', 'blob:unknown');
s.markShown('pane:9', c);
out.same = revoked.length;
s.release();
out.all = created.every(u => revoked.includes(u)) && revoked.length === created.length;
""")
    assert out["lazy"] == out["stillLazy"] == 0
    assert out["loading"] == []
    assert out["b"] == ["blob:live-1"]
    assert out["c"] == ["blob:live-1", "blob:live-2"]
    assert out["back"] == [True, True] and out["notAgain"] and out["lastStays"]
    assert out["same"] == 2 and out["all"]


def test_peaks_decode_once_are_capped_and_cover_every_sample():
    out = run_session("""
let decodes = 0, signal = null;
const samples = Float32Array.from({ length: 10000 }, (_, i) => (i === 9999 ? 0.9 : i === 0 ? -0.4 : 0.1));
const decode = async (u, opts) => { decodes++; signal = opts.signal; out.url = u; return samples; };
const s = open([output(0, 'a', entry('a.wav', 'audio')), output(1, 'b', entry('b.png'))], 0, { decode });
await s.ready; await settle();
const [one, two] = await Promise.all([s.peaksOf('pane:0'), s.peaksOf('pane:0')]);
out.once = [decodes, one === two, one.peaks.length === MAX_PEAKS];
out.ends = [one.peaks[0][0], one.peaks.at(-1)[1]].map(v => +v.toFixed(2));
out.image = await s.peaksOf('pane:1');
s.release();
out.aborted = signal.aborted;
out.gone = await s.peaksOf('pane:0');
const t = open([output(0, 'a', entry('a.wav', 'audio'))], 0, { decode: async () => null });
await t.ready;
out.undecodable = await t.peaksOf('pane:0');
t.release();
""")
    assert out["once"] == [1, True, True] and out["url"] == "blob:a.wav"
    assert out["ends"] == [-0.4, 0.9]
    assert out["image"] is None and out["aborted"] and out["gone"] is None and out["undecodable"] is None


# ── The waveform helper ──────────────────────────────────────────────────────

def test_the_panes_bars_are_what_they_were_before_the_helper():
    """The pre-extraction loop, replayed beside the helper the pane now draws from."""
    out = node_run(f"""
const {{ minMaxPeaks }} = await import({url("waveform_peaks.js")});
// output_pane_media.js drawWaveform before the extraction.
const before = (data, width, height) => {{
    const bars = [];
    const step = Math.max(1, Math.floor(data.length / width));
    for (let x = 0; x < width; x++) {{
        let peak = 0;
        for (let i = x * step; i < Math.min(data.length, (x + 1) * step); i++) peak = Math.max(peak, Math.abs(data[i]));
        bars.push(Math.max(1, peak * height));
    }}
    return bars;
}};
const after = (data, width, height) => minMaxPeaks(data, width, Math.max(1, Math.floor(data.length / width)))
    .map(([min, max]) => Math.max(1, Math.max(Math.abs(min), Math.abs(max)) * height));
const wave = n => Float32Array.from({{ length: n }}, (_, i) => Math.sin(i * 1.7) * Math.cos(i / 91) * (i % 7 ? 1 : -0.3));
const out = {{}};
for (const n of [0, 1, 100, 479, 480, 481, 5000, 44100, 100003]) {{
    const data = wave(n);
    out[n] = JSON.stringify(before(data, 480, 120)) === JSON.stringify(after(data, 480, 120));
}}
console.log('RESULT=' + JSON.stringify(out));
""")
    assert set(out.values()) == {True}, out


def test_the_decoder_answers_null_when_it_cannot_decode_or_was_aborted():
    out = node_run(f"""
const {{ decodeChannel }} = await import({url("waveform_peaks.js")});
const fetchImpl = async () => ({{ arrayBuffer: async () => new ArrayBuffer(4) }});
const out = {{}};
out.none = await decodeChannel('blob:x', {{ fetchImpl }});
globalThis.OfflineAudioContext = class {{ async decodeAudioData() {{ return {{ getChannelData: () => [0.5] }}; }} }};
out.ok = await decodeChannel('blob:x', {{ fetchImpl }});
const c = new AbortController(); c.abort();
out.aborted = await decodeChannel('blob:x', {{ fetchImpl, signal: c.signal }});
out.failed = await decodeChannel('blob:x', {{ fetchImpl: async () => {{ throw new Error('gone'); }} }});
console.log('RESULT=' + JSON.stringify(out));
""")
    assert out == {"none": None, "ok": [0.5], "aborted": None, "failed": None}


# ── The pane bar ─────────────────────────────────────────────────────────────

def _panes_script():
    script = (COORDINATOR.replace("COORD_URL", url("views_coordinator.js")).replace("AUDIO_URL", url("audio_ownership.js"))
              .replace("    onOutputsChanged(fn) { client.hint = fn; return () => { client.hint = null; }; },\n", LIVE_CLIENT))
    prefix = ("const liveReads = [], byteCalls = []; let bytesImpl = null;\n"
              "let liveAnswer = { ok: true, sources: {}, seen: [], sampling: { native: true, kj: { state: 'ok' } } };\n")
    script = script.replace("const rafQueue = [];", prefix + "const rafQueue = [];", 1)
    return script + f"""
globalThis.URL.createObjectURL = () => 'blob:x'; globalThis.URL.revokeObjectURL = () => {{}};
controlsView = {{ state: 'ready', views: [
    {{ view_id: 'a', status: 'ok', participation: 'active', path: '9', label: 'Preview' }},
    {{ view_id: 's', feed: 'sampling', status: 'ok', participation: 'active', path: '6', label: 'Pass 1', class_type: 'KSampler' }}],
    view_candidates: [], view_sampling_candidates: [] }};
const {{ mountViewPanes }} = await import({url("view_panes.js")});
const root = document.createElement('div'); const header = document.createElement('div');
document.body.append(root, header);
const sourceHost = {{ driverLanes: () => [{{ index: 0, label: 'Driver 1' }}], driverLabel: i => `Driver ${{i + 1}}`, getScene: () => ({{ guide_frames: [] }}), sceneAspect: () => 16 / 9,
    frame: () => 0, totalFrames: () => 48, isGuideTrackHidden: () => false, clock: {{ subscribe: () => () => {{}}, get: () => ({{}}) }}, getClock: () => ({{}}) }};
let claim = null;
const cache = {{ acquire: e => ({{ ready: new Promise(r => {{ claim = r; }}), release() {{}} }}) }};
const inspected = [], modelCalls = [];
let opening = null;
c.setVisible(true);
const panes = mountViewPanes({{ root, header, coordinator: c, audio, sourceHost, cache, placeComposite: () => {{}},
    onInspect: (index, request) => {{ inspected.push([index, request.startTime, request.models.map(m => m.index)]); return new Promise(r => {{ opening = r; }}); }},
    onModels: models => modelCalls.push(models.map(m => [m.index, m.source.kind, m.loaded])) }});
globalThis.onCoordinatorChange = () => panes.update();
flushRaf();
const cell = i => root.find(el => el.dataset?.sonderPane === String(i));
const bar = i => cell(i).all(el => el.tagName === 'BUTTON').map(el => el.textContent).filter(t => !t.endsWith('▾'));
const inspectOf = i => cell(i).find(el => el.dataset?.sonderPaneInspect === String(i));
"""


def test_inspect_appears_with_loaded_media_and_hands_over_the_pane():
    out = node_run(_panes_script() + f"""
c.setLayout({{ count: 2 }}); c.assign(1, {{ kind: 'view', viewId: 'a' }}); await settle(); flushRaf();
out.empty = bar(1).includes('Inspect');
answer.sources = {VIEW_RESULT};
client.hint(); await settle(); flushRaf();
out.loading = [bar(1), inspectOf(1).getAttribute('aria-disabled'), inspectOf(1).title];
inspectOf(1).click(); out.notYet = inspected.length;
const pressed = cell(1).all(el => el.tagName === 'BUTTON');
claim({{ url: 'blob:a' }}); await settle();
const img = cell(1).find(el => el.tagName === 'IMG');
img.dispatch('load'); flushRaf();  // an image sends no duration: its own load repaints the bar
out.loaded = [bar(1), inspectOf(1).getAttribute('aria-disabled')];
out.sameRow = cell(1).all(el => el.tagName === 'BUTTON').every((el, i) => el === pressed[i]);
out.models = modelCalls.at(-1);
const inspectEl = inspectOf(1); inspectEl.focus();
inspectEl.click(); flushRaf();
out.kept = inspectOf(1) === inspectEl && document.activeElement === inspectEl;
out.call = inspected.slice();
out.busy = [inspectOf(1).getAttribute('aria-disabled'), inspectOf(1).getAttribute('aria-busy'), !!inspectOf(1).disabled];
inspectOf(1).click(); flushRaf();
out.once = inspected.length;
opening(); await settle(); flushRaf();
out.done = inspectOf(1).getAttribute('aria-disabled');
// A decode failure takes the button away.
img.dispatch('error'); flushRaf();
out.failed = [inspectOf(1).getAttribute('aria-disabled'), inspectOf(1).title]; inspectOf(1).click(); out.inert = inspected.length;
// An editor source has none.
for (const source of [{{ kind: 'guides' }}, {{ kind: 'driver', index: 0 }}, {{ kind: 'composite' }}]) {{ c.assign(1, source); flushRaf(); (out.editor ||= []).push(bar(1).includes('Inspect')); }}
panes.destroy();
console.log('RESULT=' + JSON.stringify(out));
""")
    assert out["empty"] is False
    assert out["loading"] == [["Fit", "Inspect", "Hold", "Run"], "true", "Inspect opens once this has loaded"]
    assert out["notYet"] == 0
    # The media loading rebuilds nothing: a press on Hold or Run survives it.
    assert out["loaded"] == [["Fit", "Inspect", "Hold", "Run"], None] and out["sameRow"]
    assert out["kept"]
    assert out["models"] == [[0, "composite", False], [1, "view", True]]
    assert out["call"] == [[1, 0, [0, 1]]]
    assert out["busy"] == ["true", "true", False]  # busy keeps its focus: never `disabled`
    assert out["once"] == 1 and out["done"] is None
    assert out["failed"] == ["true", "This media can't be shown, so it can't be inspected"] and out["inert"] == 1
    assert out["editor"] == [False, False, False]


def test_a_sampling_pane_inspects_while_it_shows_a_picture_and_a_lost_session_hides_it():
    out = node_run(_panes_script() + """
c.setLayout({ count: 2 }); c.assign(1, { kind: 'view', viewId: 's' }); await settle(); flushRaf();
out.waiting = bar(1);
liveAnswer.sources = { '6': { state: 'live', live: { prompt_id: 'p1', n: 2, step: 2, total: 6, mime: 'image/jpeg', has_payload: true },
    run: { prompt_id: 'p1', finished: false } } };
client.liveHint(); await settle(); flushRaf();
out.live = bar(1);
c.setMediaCondition(1, 'decode'); flushRaf();
out.condition = bar(1); out.off = inspectOf(1).getAttribute('aria-disabled');
c.setMediaCondition(1, null); flushRaf();
lostCb(); flushRaf();
out.lost = bar(1);
panes.destroy();
console.log('RESULT=' + JSON.stringify(out));
""")
    assert out["waiting"] == [] and out["live"] == ["Fit", "Inspect"]
    assert out["condition"] == ["Fit", "Inspect"] and out["off"] == "true" and out["lost"] == []


def test_focus_inspect_finds_the_panes_button_or_does_nothing():
    source = (JS / "view_panes.js").read_text(encoding="utf-8")
    body = source[source.index("focusInspect(index) {"):source.index("        destroy() {")]
    assert 'cells[index]?.actions.querySelector?.("[data-sonder-pane-inspect]")?.focus?.({ preventScroll: true });' in body
    host = (JS / "editor_widget.js").read_text(encoding="utf-8")
    assert "this._viewPanes?.focusInspect?.(index);" in host


# ── The overlay's external-item rules ────────────────────────────────────────

def run_scope(body: str):
    return node_run(f"const S = await import({SCOPE});\nconst out = {{}};\n" + body + "\nconsole.log('RESULT=' + JSON.stringify(out));")


def test_external_scope_comes_from_the_session_and_never_names_a_file():
    out = run_scope("""
const items = [{ id: 'pane:0', kind: 'image', title: 'A', fps: 24, note: 'n', status: 's', details: [['View', 'A']] },
    { id: 'pane:2', kind: 'video', title: 'B', fps: 12 }];
const scope = S.resolveInspectOverlayScope({ origin: 'external', requestedAssetId: 'pane:2', externalItems: items,
    visibleAssets: [{ asset_id: 'pane:2', asset_type: 'image' }], sortedProjectAssets: [{ asset_id: 'x' }] });
out.scope = [scope.origin, scope.assets.map(a => a.asset_id), scope.asset];
out.missing = S.resolveInspectOverlayScope({ origin: 'external', requestedAssetId: 'pane:9', externalItems: items }).asset;
out.gallery = S.resolveInspectOverlayScope({ requestedAssetId: 'a', visibleAssets: [{ asset_id: 'a' }], externalItems: items }).assets.length;
out.direct = S.resolveInspectOverlayScope({ origin: 'direct', sortedProjectAssets: [{ asset_id: 'a', asset_type: 'image', path: 'p' }] }).origin;
""")
    assert out["scope"][:2] == ["external", ["pane:0", "pane:2"]]
    asset = out["scope"][2]
    assert asset["external"] is True and asset["asset_type"] == "video" and asset["fps"] == 12
    assert (asset["path"], asset["has_thumbnail"], asset["has_audio"], asset["duration_sec"]) == ("", False, False, None)
    assert out["missing"] is None and out["gallery"] == 1 and out["direct"] == "direct"


def test_external_policy_turns_every_project_feature_off():
    out = run_scope("""
const plain = p => Object.fromEntries(Object.entries(p).filter(([, v]) => typeof v === 'boolean'));
const texts = p => [p.chooserTitle('A'), p.chooserHint('B'), p.compareTitle(true), p.compareTitle(false), p.metadataTitle];
out.project = [plain(S.inspectOverlayPolicy(false)), texts(S.inspectOverlayPolicy())];
out.external = [plain(S.inspectOverlayPolicy(true)), texts(S.inspectOverlayPolicy(true))];
""")
    assert set(out["project"][0].values()) == {True} and set(out["external"][0].values()) == {False}
    assert set(out["project"][0]) == set(out["external"][0]) >= {"favorites", "trash", "detailDemand", "chooserSearch", "chooserSort"}
    assert out["project"][1][0] == "Gallery A" and out["project"][1][2:4] == ["Compare (C)", "Compare needs ≥ 2 same-type visible assets"]
    assert out["external"][1][:2] == ["Pane A", "Left click assigns A. Right click assigns B."]
    assert out["external"][1][2] == "Compare with another pane showing the same kind of media (C)"


def test_external_help_drops_the_asset_keys_and_keeps_the_row_shape():
    source = (JS / "shared_asset_gallery.js").read_text(encoding="utf-8")
    rows = re.search(r"export const INSPECT_OVERLAY_SHORTCUTS = Object\.freeze\((\[.*?\])\);", source, re.S)[1]
    out = run_scope(f"""
const rows = {rows};
out.same = S.inspectHelpRows(rows, false) === rows;
const external = S.inspectHelpRows(rows, true);
out.keys = [rows, external].map(list => list.map(r => r[0]));
out.shape = external.every(r => Array.isArray(r) && r.length === 2);
out.arrows = external.find(r => r[0] === 'ArrowUp / ArrowDown')[1];
out.assets = external.filter(r => /asset/i.test(r[1])).map(r => r[0]);
""")
    assert out["same"] and out["shape"]
    assert [k for k in out["keys"][0] if k not in out["keys"][1]] == ["S", "Delete"]
    assert out["arrows"] == "Previous / next pane (compare: cycle the active A/B side)"
    assert out["assets"] == []  # nothing left talks about assets
    editor = (JS / "editor_widget.js").read_text(encoding="utf-8")
    assert "...INSPECT_OVERLAY_SHORTCUTS,\n" in editor  # the main overlay's section still reads the pairs


# ── The overlay's session, sliced ────────────────────────────────────────────

GALLERY = (JS / "shared_asset_gallery.js").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    return re.search(r"^    (?:async )?function " + name + r"\(.*?^    \}\n", GALLERY, re.M | re.S)[0]


_FUNCTIONS = ("isExternalOverlay", "overlayPolicy", "overlayItem", "clearOverlayRuntime", "overlayScope", "overlayAssets",
              "currentOverlayAsset", "sameTypeOverlayAssets", "clearExternalSession", "closeInspectOverlay", "openInspectOverlay",
              "startInspectOverlay", "inspectExternal", "closeExternalInspect", "handleExternalEvent", "setExternalMediaUrl",
              "loadExternalMedia", "overlayMediaState", "swapExternalMedia", "swapExternalVideo", "stillFrameOf", "handleOverlayFavoriteKey",
              "syncOverlayWithAssets", "overlayWatchedAssets", "compareFilteredCandidates", "videoBlocked", "overlayWaveform")

OVERLAY = FAKE_DOM + r"""
const { inspectOverlayPolicy, resolveInspectOverlayScope } = await import(SCOPE_URL);
const out = {};
const log = [];
const DEFAULT_SORT_MODE = 'newest';
const state = { destroyed: false, inspectorCollapsed: true, overlaySession: 0,
    overlayState: { open: false, assetId: '', origin: 'gallery', external: null, externalOff: null, onExternalClosed: null, externalMedia: [],
        externalRefresh: null, externalTextRefreshers: [], initialMediaState: null, cleanupFns: [], mediaSignature: '', overlayEl: null } };
const data = { assets: [{ asset_id: 'x', asset_type: 'image', path: 'x.png' }] };
const options = { onInspectOpenChange: open => log.push(`sound:${open}`) };
const isTrashed = () => false, shownAsset = id => data.assets.find(a => a.asset_id === id);
const activeNavigableAssets = () => data.assets, sortAssets = a => a, sortAssetsByMode = () => { throw new Error('sorted panes'); };
const clearOverlayMediaCache = () => {}, syncDetailDemand = () => {}, resetOverlayTransform = () => {}, renderDetail = () => {}, selectedAsset = () => null;
const galleryVideoSource = () => { throw new Error('proxy read'); };
const mediaFailedToDecode = el => !!el.broken;
const holdVideoOverUnderlay = (layer, still) => { log.push(`hold:${!!still}`); return () => {}; };
let syncing = false;
const compareStageDrawn = () => { if (syncing) throw new Error('asset sync ran'); return false; };
const handleToggleFavorite = () => log.push('favorite'), overlayFavoriteTarget = () => ({ asset_id: 'x' });
const currentProjectDir = () => 'P', buildWaveformUrl = () => 'waveform';
globalThis.fetch = async u => { log.push(`fetch:${u}`); return { ok: true, json: async () => ({ peaks: [[0, 1]] }) }; };
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
let renders = 0;
const carried = [];
const renderInspectOverlay = () => {
    renders++;
    const o = state.overlayState;
    if (!o.open || !currentOverlayAsset()) { closeInspectOverlay(); return; }
    clearOverlayRuntime();
    if (!o.overlayEl) { o.overlayEl = new El('div'); document.body.append(o.overlayEl); }
    carried.push(o.mediaSignature ? null : o.initialMediaState);
    o.initialMediaState = null;
    o.mediaSignature = `s:${o.assetId}`;
};
const session = (items = [{ id: 'pane:0', kind: 'image', title: 'A', startTime: 2.5 }]) => {
    const s = { items, anchorId: items[0]?.id, releases: 0, offs: 0, released: false, shown: [], url: 'blob:1',
        failed: [], markFailed: (id, u) => s.failed.push([id, u]), subscribe(fn) { s.listener = fn; return () => { s.offs++; }; }, urlOf: () => s.url, markShown: (id, u) => s.shown.push([id, u]),
        peaksOf: async id => ({ peaks: [id] }), release() { s.releases++; s.released = true; } };
    return s;
};
""".replace("SCOPE_URL", SCOPE)


def run_overlay(body: str):
    script = OVERLAY + "".join(_fn(name) for name in _FUNCTIONS) + body
    return node_run(script + "\nconsole.log('RESULT=' + JSON.stringify(out));")


def test_the_overlay_opens_a_session_paused_at_the_panes_moment_and_takes_focus():
    out = run_overlay("""
const s = session();
let closed = 0;
out.opened = inspectExternal(s, { onClosed: () => closed++ });
out.state = [state.overlayState.open, state.overlayState.origin, state.overlayState.assetId, isExternalOverlay()];
out.start = carried.slice();
out.focus = document.activeElement === state.overlayState.overlayEl;
renderInspectOverlay();  // a later rebuild has no start of its own
out.later = carried.at(-1);
out.sound = log.slice();
closeInspectOverlay(); closeInspectOverlay();
out.closed = [closed, s.releases, s.offs, isExternalOverlay(), state.overlayState.externalMedia.length, log.at(-1)];
out.refused = [inspectExternal(session([])), inspectExternal(null), (() => { const r = session(); r.release(); return inspectExternal(r); })()];
""")
    assert out["opened"] is True
    assert out["state"] == [True, "external", "pane:0", True]
    assert out["start"] == [{"time": 2.5, "playing": False}] and out["later"] is None
    assert out["focus"] and out["sound"] == ["sound:true"]
    assert out["closed"] == [1, 1, 1, False, 0, "sound:false"]
    assert out["refused"] == [False, False, False]


def test_a_session_is_let_go_once_by_another_inspect_a_gallery_open_and_the_hosts_close():
    out = run_overlay("""
let closed = [];
const first = session(), second = session([{ id: 'pane:1', kind: 'video', title: 'B', startTime: 0 }]);
inspectExternal(first, { onClosed: () => closed.push('first') });
inspectExternal(second, { onClosed: () => closed.push('second') });
out.replaced = [first.releases, second.releases, closed.slice(), state.overlayState.assetId];
openInspectOverlay(data.assets[0]);
out.gallery = [second.releases, closed.slice(), state.overlayState.origin, isExternalOverlay(), state.overlayState.open];
closeExternalInspect();  // a gallery Inspect is not ours to close
out.kept = state.overlayState.open;
closeInspectOverlay();
const third = session();
inspectExternal(third, { onClosed: () => closed.push('third') });
closeExternalInspect();
out.host = [third.releases, closed.at(-1), state.overlayState.open];
""")
    assert out["replaced"] == [1, 0, ["first"], "pane:1"]
    assert out["gallery"] == [1, ["first"], "gallery", False, True]
    assert out["kept"] is True
    assert out["host"] == [1, "third", False]


def test_no_project_path_runs_for_an_external_overlay():
    out = run_overlay("""
const s = session([{ id: 'pane:2', kind: 'audio', title: 'A', startTime: 0 }, { id: 'pane:3', kind: 'audio', title: 'B', startTime: 0 }]);
inspectExternal(s);
syncing = true; syncOverlayWithAssets(['x'], { listArrival: true }); syncing = false;  // throws if it looks further
out.watched = overlayWatchedAssets();
out.s = [handleOverlayFavoriteKey({ repeat: false }), log.includes('favorite')];
out.order = compareFilteredCandidates('A').map(a => a.asset_id);  // throws if sorted
out.blocked = videoBlocked({ asset_type: 'video', external: true });
out.item = [overlayItem('pane:2').name, overlayItem('x')];
out.wave = await overlayWaveform(currentOverlayAsset());
out.fetched = log.filter(l => l.startsWith('fetch'));
closeInspectOverlay();
out.assetWave = await overlayWaveform(data.assets[0]);
""")
    assert out["watched"] == [] and out["s"] == [True, False]
    assert out["order"] == ["pane:2", "pane:3"] and out["blocked"] is False
    assert out["item"] == ["A", None]
    assert out["wave"] == {"peaks": ["pane:2"]} and out["fetched"] == []
    assert out["assetWave"] == {"peaks": [[0, 1]]}


MEDIA = """
const probes = [];
document.createElement = tag => {
    const el = new El(tag);
    if (tag === 'img') el.decode = () => new Promise((resolve, reject) => { probes.push({ el, resolve, reject }); });
    el.removeAttribute = name => { el.dropped = name; };
    if (tag === 'video') { el.load = () => {}; probes.push({ el }); }
    if (tag === 'canvas') el.getContext = () => ({ drawImage() {} });
    return el;
};
const tick = async () => { for (let i = 0; i < 3; i++) await new Promise(r => setTimeout(r, 0)); };
"""


def test_a_followed_image_swaps_in_place_once_decoded_and_keeps_the_last_picture_otherwise():
    out = run_overlay(MEDIA + """
const s = session();
inspectExternal(s);
const img = new El('img');
const handle = loadExternalMedia(currentOverlayAsset(), img);
out.initial = [img.src, img._sonderGalleryAsset === undefined];
img.dispatch('load');
out.shown = s.shown.slice();
const before = renders;
s.url = 'blob:2'; s.listener({ type: 'media', id: 'pane:0' });
out.decoding = img.src;
probes.at(-1).resolve(); await tick();
out.swapped = [img.src, img.decoding, renders === before];
img.dispatch('load');
out.shown2 = s.shown.at(-1);
// One that can't decode leaves the last picture; one overtaken while decoding is dropped.
s.url = 'blob:3'; s.listener({ type: 'media', id: 'pane:0' });
probes.at(-1).reject(new Error('bad')); await tick();
out.bad = img.src;
s.url = 'blob:4'; s.listener({ type: 'media', id: 'pane:0' });
const overtaken = probes.at(-1);
s.url = 'blob:5'; s.listener({ type: 'media', id: 'pane:0' });
out.dropped = overtaken.el.dropped === 'src';  // the overtaken one stops loading at once
probes.at(-1).resolve(); await tick(); overtaken.resolve(); await tick();
out.newest = img.src;
// Another item, an unchanged URL and a closed stage touch nothing.
const count = probes.length;
s.listener({ type: 'media', id: 'pane:7' }); s.listener({ type: 'media', id: 'pane:0' });
out.idle = probes.length === count;
s.url = 'blob:6'; s.listener({ type: 'media', id: 'pane:0' });
handle.cleanup(); probes.at(-1).resolve(); await tick();
out.gone = img.src;
// Another kind of media needs another stage.
s.listener({ type: 'kind', id: 'pane:0' });
out.kind = renders === before + 1;
let refreshed = [];
state.overlayState.externalRefresh = joined => refreshed.push(joined);
s.listener({ type: 'kind', id: 'pane:4' });  // not on the stage: the stage stays
out.offStage = [renders === before + 1, refreshed];
out.failed = s.failed;
let words = [];
state.overlayState.externalRefresh = joined => words.push(joined);
s.listener({ type: 'joined', id: 'pane:1' }); s.listener({ type: 'status', id: 'pane:0' });
out.words = words;
""")
    assert out["initial"] == ["blob:1", True] and out["shown"] == [["pane:0", "blob:1"]]
    assert out["decoding"] == "blob:1"
    assert out["swapped"] == ["blob:2", "sync", True] and out["shown2"] == ["pane:0", "blob:2"]
    assert out["bad"] == "blob:2" and out["dropped"] and out["newest"] == "blob:5"
    assert out["idle"] and out["gone"] == "blob:5" and out["kind"]
    assert out["offStage"] == [True, [True]]
    assert out["failed"] == [["pane:0", "blob:3"]]  # only the newest failure is reported
    assert out["words"] == [True, False]


def test_a_followed_video_keeps_its_place_and_plays_on_once():
    out = run_overlay(MEDIA + """
const s = session([{ id: 'pane:0', kind: 'video', title: 'A', startTime: 0 }]);
inspectExternal(s);
const video = new El('video'); video.readyState = 4; video.videoWidth = 8; video.videoHeight = 8;
const stage = new El('div'); stage.append(video);
loadExternalMedia(currentOverlayAsset(), video);
const o = state.overlayState;
let media = { time: 1.25, playing: true };
const calls = [];
o.captureMediaState = () => ({ ...media });
o.pausePlayback = () => { calls.push('pause'); media.playing = false; };
o.seekPlayback = t => calls.push(`seek:${t}`);
o.togglePlayback = () => { calls.push('toggle'); media.playing = !media.playing; };
s.url = 'blob:2'; s.listener({ type: 'media', id: 'pane:0' });
out.waits = [video.src, calls.length];
probes.at(-1).el.dispatch('loadeddata');
out.swapped = [video.src, calls.slice(), stage.children[0].tagName, log.at(-1)];
video.dispatch('loadeddata');
out.ready = calls.slice();
// Two in a row: the second keeps the first one's place and playback, and one hold.
media = { time: 0.5, playing: true }; calls.length = 0;
s.url = 'blob:2a'; s.listener({ type: 'media', id: 'pane:0' }); probes.at(-1).el.dispatch('loadeddata');
media = { time: 0, playing: false };  // the element reset under its new src
out.carried = overlayMediaState();     // a rebuild now still carries the place
s.url = 'blob:2b'; s.listener({ type: 'media', id: 'pane:0' }); probes.at(-1).el.dispatch('loadeddata');
video.dispatch('loadeddata');
out.twice = [calls.slice(), video.src, state.overlayState.cleanupFns.length, stage.children.length];
media = { time: 1.25, playing: true };
// A broken one is left out; the last picture stays.
s.url = 'blob:3'; s.listener({ type: 'media', id: 'pane:0' });
probes.at(-1).el.broken = true;
probes.at(-1).el.dispatch('loadeddata');
out.broken = video.src; out.failed = s.failed.slice();
// One a failure took off the stage is rebuilt instead.
video._sonderExternalFailed = true;
const before = renders;
s.url = 'blob:4'; s.listener({ type: 'media', id: 'pane:0' });
out.rebuilt = renders === before + 1;
""")
    assert out["waits"] == ["blob:1", 0]
    assert out["swapped"] == ["blob:2", ["pause"], "CANVAS", "hold:true"]
    assert out["ready"] == ["pause", "seek:1.25", "toggle"]
    assert out["carried"] == {"time": 0.5, "playing": True}
    assert out["twice"] == [["pause", "seek:0.5", "toggle"], "blob:2b", 0, 2]
    assert out["broken"] == "blob:2b" and out["failed"] == [["pane:0", "blob:3"]] and out["rebuilt"]


# ── The overlay's render, sliced ─────────────────────────────────────────────
# The real renderInspectOverlay, key handlers, chips, choosers' rows and metadata
# panel on small DOM stubs; only the two stage renderers are stubbed.

RENDER = FAKE_DOM + r"""
const { inspectHelpRows, inspectOverlayPolicy, planCompareRemoval, resolveCompareSlots, resolveInspectOverlayScope } = await import(SCOPE_URL);
const out = {};
const DEFAULT_SORT_MODE = 'newest', CHROME = {}, FONT = {}, FAVORITE_STAR_COLOR = 'gold', KEY_PRIORITY = { OVERLAY: 1 };
const calls = [], carried = [];
let keys = null;
const registerKeyboardConsumer = consumer => { keys = consumer; return () => {}; };
const consumerId = s => s;
const style = (el, css) => { el.style.cssText = css; return el; };
const makeActionButton = () => document.createElement('button');
const setActionButtonVariant = b => b, setButtonDisabled = (b, v) => { b.disabled = v; };
const chromeScroller = el => el, chromeScrollbarCss = () => '';
const attachCopyToElement = el => el;
const makeMetaCell = (label, value) => { const c = document.createElement('div'); const a = document.createElement('div'), b = document.createElement('div');
    a.textContent = label; b.textContent = value; c.append(a, b); return c; };
const assetDisplayName = a => a?.name || '', assetKindLabel = () => 'Video', assetIsMissing = () => false, isTrashed = () => false, assetFps = () => 24;
const makeFavoriteButton = () => { const b = document.createElement('button'); b.textContent = 'STAR'; return b; };
const formatDuration = () => '', formatResolution = () => '', formatBytes = () => '', formatAssetSizeOnDisk = () => '', workflowStatusLabel = () => '';
const renderTrackedMetadataSection = a => (calls.push(`tracked:${a.asset_id}`), null);
const renderGenerationSection = a => (calls.push(`generation:${a.asset_id}`), document.createElement('div'));
const displaySignature = list => list.map(a => a.asset_id).join();
const syncDetailDemand = () => {}, clearOverlayMediaCache = () => {}, renderDetail = () => {}, selectedAsset = () => null, resetOverlayTransform = () => {};
const ensureCompareDefaults = () => {}, invalidateOverlayMetadata = () => state.overlayMetadataRefresh?.(), handleCompareStarClick = () => {};
const shouldCaptureOverlayShortcut = () => true, showInspectOverlayShortcutHelp = () => {}, persistInspectorSetting = () => {};
const parseAssetSearchQuery = () => ({}), prepareMetadataSearch = () => 'ready', assetMatchesParsedQuery = () => true, sortAssetsByMode = a => a;
const compareQueryRef = () => 'comparePickerQuery', overlayPlaceJustReplaced = () => false;
const shownAsset = id => data.assets.find(a => a.asset_id === id);
const handleToggleFavorite = a => { calls.push(`favorite:${a.asset_id}`); };
const galleryVideoSource = () => { calls.push('proxy'); return { variant: 'original', rev: 0 }; };
const currentProjectDir = () => 'P';
const renderSingleOverlay = (a, host) => { carried.push(['single', a.asset_id, state.overlayState.carriedMediaState]); };
const renderCompareOverlay = (a, host) => { carried.push(['compare', state.overlayState.compareLeftAssetId, state.overlayState.compareRightAssetId, state.overlayState.carriedMediaState]); };
let lastHooks = null;
const handleAssetDelete = async (a, hooks) => { calls.push(`trash:${a.asset_id}`); lastHooks = hooks; return true; };
const options = { onDeleteAsset: () => {}, onUpdateAsset: () => {}, onInspectOpenChange: () => {} };
const activeNavigableAssets = () => data.assets, sortAssets = a => a;
const successorAssetIdAfterRemoval = () => '', notifySuccess = () => null;
const data = { assets: [{ asset_id: 'x', asset_type: 'video', name: 'X', path: 'x.mp4' }, { asset_id: 'y', asset_type: 'video', name: 'Y', path: 'y.mp4' }] };
const state = { destroyed: false, inspectorCollapsed: true, overlaySession: 0, overlayMetadataRefresh: null,
    overlayState: { open: false, assetId: '', origin: 'gallery', external: null, externalOff: null, onExternalClosed: null, externalMedia: [],
        externalRefresh: null, externalTextRefreshers: [], initialMediaState: null, cleanupFns: [], mediaSignature: '', overlayEl: null, compareCycleSide: 'B' } };
const items = kinds => kinds.map((kind, i) => ({ id: `pane:${i}`, kind, title: `V${i} · Pane ${i + 1}`, place: `Pane ${i + 1}`, startTime: i ? 0 : 1.5, fps: 24,
    status: '', note: '', details: [['View', `detail-${i}`]] }));
const session = list => ({ items: list, anchorId: list[0].id, released: false, subscribe: () => () => {}, urlOf: () => 'blob:1', markShown() {}, release() { this.released = true; } });
const press = (key, extra = {}) => keys.keydown({ key, target: {}, ...extra });
const labels = () => state.overlayState.overlayEl.all(el => el.tagName === 'BUTTON').map(el => el.textContent);
Object.defineProperty(El.prototype, 'innerHTML', { set(v) { if (v !== '') throw new Error('markup'); this.replaceChildren(); } });
""".replace("SCOPE_URL", SCOPE)

_RENDER_FUNCTIONS = ("isExternalOverlay", "overlayPolicy", "overlayItem", "compareModeActive", "compareStageShown", "compareStageDrawn",
                     "compareStageSignature", "resolvedCompareSlots", "compareSideAsset", "overlayFavoriteTarget", "handleOverlayFavoriteKey",
                     "overlayWatchedAssets", "overlayMediaState", "clearOverlayRuntime", "overlayScope", "overlayAssets", "currentOverlayAsset",
                     "sameTypeOverlayAssets", "clearExternalSession", "closeInspectOverlay", "openInspectOverlay", "startInspectOverlay",
                     "inspectExternal", "handleExternalEvent", "refreshCompareInPlace", "compareFilteredCandidates", "cycleCompareSlot",
                     "cycleOverlayAsset", "makeCompareSideChip", "externalDetailsSection", "renderOverlayMetadataPanel",
                     "handleOverlayAssetDelete", "planFavoriteRemovalInOverlay", "planFavoriteRemoval", "renderInspectOverlay")


def run_render(body: str):
    # A file: the sliced render is longer than a Windows command line.
    script = RENDER + "".join(_fn(name) for name in _RENDER_FUNCTIONS) + body + "\nconsole.log('RESULT=' + JSON.stringify(out));"
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "render.mjs"
        path.write_text(script, encoding="utf-8")
        return node_run(f"await import({json.dumps(path.as_uri())});")


def test_an_external_overlay_draws_and_answers_no_project_action():
    out = run_render("""
inspectExternal(session(items(['video', 'video'])));
out.start = carried.slice();
out.single = labels();
out.keys = [press('s'), press('S'), press('Delete')];
renderInspectOverlay();
out.again = carried.at(-1);
press('c');
out.compare = [carried.at(-1), labels()];
out.compareKeys = [press('s'), press('Delete')];
const chip = makeCompareSideChip('A', document.createElement('div')).el;
out.chip = [chip.textContent, chip.all(el => el.tagName === 'BUTTON').length];
state.overlayState.showMetadata = true; renderInspectOverlay();
const text = state.overlayState.overlayEl.textContent;
out.meta = ['detail-0', 'detail-1', 'Pane A', 'Pane B'].map(t => text.includes(t));
out.filter = text.includes('filter A');
out.cleanup = overlayWatchedAssets();
// A favorite leaving the gallery's view behind the overlay is the gallery's matter.
out.plan = planFavoriteRemovalInOverlay('x');
out.placed = planFavoriteRemoval(data.assets[0]) !== null;
out.calls = calls.slice();
// The same gestures on an asset do act, so the silence above means something.
openInspectOverlay(data.assets[0]);
out.asset = labels().slice(0, 2);
press('s'); press('Delete');
out.assetCalls = calls.slice(out.calls.length).filter(c => !c.startsWith('proxy'));
out.assetChip = makeCompareSideChip('A', document.createElement('div')).el.all(el => el.tagName === 'BUTTON').length;
""")
    assert out["start"] == [["single", "pane:0", {"time": 1.5, "playing": False}]]
    assert out["single"] == ["Fit", "Compare", "Metadata", "?", "Close"]
    assert out["keys"] == [True, True, True]
    assert out["again"] == ["single", "pane:0", None]
    assert out["compare"][0] == ["compare", "pane:0", "pane:1", None] and out["compare"][1][0] == "Fit"
    assert out["compareKeys"] == [True, True] and out["chip"] == ["A", 0]
    assert out["meta"] == [True] * 4 and out["filter"] is False
    assert out["cleanup"] == [] and out["plan"] is None and out["placed"]
    assert out["calls"] == []
    assert out["asset"] == ["STAR", "Trash"] and out["assetCalls"] == ["favorite:x", "trash:x"] and out["assetChip"] == 1


def test_external_compare_steps_past_the_other_side_and_stays_put_with_nothing_else():
    out = run_render("""
inspectExternal(session(items(['video', 'video', 'video'])));
press('c');
const draws = () => carried.length;
const pair = () => [state.overlayState.compareLeftAssetId, state.overlayState.compareRightAssetId];
out.first = pair();
press('ArrowDown'); out.down = pair();
press('ArrowDown'); out.wrapped = pair();   // past A, not onto it
press('ArrowUp'); out.up = pair();
closeInspectOverlay();
inspectExternal(session(items(['video', 'video'])));
press('c');
const before = draws();
press('ArrowDown'); press('ArrowUp');
out.two = [pair(), draws() === before];
""")
    assert out["first"] == out["wrapped"] == ["pane:0", "pane:1"]
    assert out["down"] == out["up"] == ["pane:0", "pane:2"]
    assert out["two"] == [["pane:0", "pane:1"], True]


def test_a_gallery_trash_settling_later_never_moves_a_panes_inspect():
    out = run_render("""
openInspectOverlay(data.assets[0]);
let hooks = null;
const realDelete = handleOverlayAssetDelete;
// The trash paints only now, after the author closed it and opened a pane's Inspect.
globalThis.deleteHooks = null;
await (async () => { state.overlayState.compareMode = false; await handleOverlayAssetDelete(data.assets[0]); })();
closeInspectOverlay();
inspectExternal(session(items(['video'])));
lastHooks.onPainted();
out.kept = [state.overlayState.open, state.overlayState.assetId, isExternalOverlay()];
lastHooks.onUnpainted();
out.back = state.overlayState.assetId;
closeInspectOverlay();
// In its own session it moves on, as before.
openInspectOverlay(data.assets[0]);
await handleOverlayAssetDelete(data.assets[0]);
lastHooks.onPainted();
out.closed = state.overlayState.open;
// An external item is refused outright.
out.refused = await handleOverlayAssetDelete({ asset_id: 'pane:0', external: true });
""".replace("let hooks = null;\nconst realDelete = handleOverlayAssetDelete;\n", "").replace("globalThis.deleteHooks = null;\n", ""))
    assert out["kept"] == [True, "pane:0", True] and out["back"] == "pane:0"
    assert out["closed"] is False and out["refused"] is False


def test_the_write_sinks_refuse_an_external_item_whatever_asked():
    out = node_run("""
const calls = [];
const options = { onUpdateAsset: () => {} };
const shownAsset = () => null, planFavoriteRemoval = () => { calls.push('plan'); return {}; };
const writeFavorite = async () => { calls.push('write'); return true; };
const handleAssetDeleteWithinGesture = async () => { calls.push('delete'); return true; };
""" + _fn("handleToggleFavorite") + _fn("handleAssetDelete") + """
const out = [await handleToggleFavorite({ asset_id: 'pane:0', external: true, favorite: true }), await handleAssetDelete({ asset_id: 'pane:0', external: true }), calls.slice(),
    await handleToggleFavorite({ asset_id: 'x' }), await handleAssetDelete({ asset_id: 'x' }), calls.length];
console.log('RESULT=' + JSON.stringify(out));
""")
    assert out == [False, False, [], True, True, 2]


# ── The host, sliced ─────────────────────────────────────────────────────────

HOST_SOURCE = (JS / "editor_widget.js").read_text(encoding="utf-8")


def _method(name: str) -> str:
    return re.search(r"^    (?:async )?" + name + r"\(.*?^    \}\n", HOST_SOURCE, re.M | re.S)[0]


def test_the_host_opens_the_latest_press_and_every_close_lets_the_session_go():
    out = node_run("""
const out = {}, sessions = [], notices = [], opened = [];
const openInspectSession = args => {
    const s = { args, released: 0, refreshes: [], refresh: m => s.refreshes.push(m), release() { s.released++; } };
    s.ready = new Promise(r => { s.settle = r; });
    args.signal.addEventListener('abort', () => { s.aborted = true; });
    sessions.push(s); return s;
};
const notifyWarning = (message, opts) => notices.push([message, opts.source]);
class Host {
""" + _method("_inspectViewPane") + _method("_closeViewInspect") + """
}
const h = new Host();
Object.assign(h, { _outputMediaCache: 'cache', _effectiveFps: 24, _viewsCoordinator: { controlsView: () => ({ subgraph_names: { 1: 'S' } }) },
    _viewPanes: { updates: 0, focused: [], update() { this.updates++; }, focusInspect(i) { this.focused.push(i); } },
    _assetGallery: { accept: true, inspectExternal(s, o) { opened.push([s, o]); return this.accept; }, closes: 0, closeExternalInspect() { this.closes++; } } });
// A second press while the first still loads: the first is abandoned.
const p0 = h._inspectViewPane(0, { startTime: 2, models: ['m'] }), p1 = h._inspectViewPane(1);
out.first = [sessions[0].aborted, sessions[0].args.anchorIndex, sessions[0].args.startTime, sessions[0].args.fps, sessions[0].args.subgraphNames];
sessions[0].settle({ ok: true }); sessions[1].settle({ ok: true }); await p0; await p1;
out.opened = [opened.length, opened[0][0] === sessions[1], h._viewPanes.updates, h._viewInspect.session === sessions[1]];
// Closed from the overlay: forgotten, and focus goes back.
opened[0][1].onClosed();
out.closed = [h._viewInspect, h._viewPanes.focused];
// A scope change while open: the overlay is closed first, then the open is aborted.
const p2 = h._inspectViewPane(2); sessions[2].settle({ ok: true }); await p2;
const closes = h._assetGallery.closes; h._closeViewInspect();
out.scope = [h._assetGallery.closes - closes, sessions[2].aborted, h._viewInspect];
// A scope change while pending: nothing opens, nothing is said.
const p3 = h._inspectViewPane(3); h._closeViewInspect(); sessions[3].settle({ ok: true }); await p3;
out.pending = [sessions[3].aborted, opened.length, notices.length];
h._closeViewInspect();
// A failure, and an overlay that refuses, release and say so once each.
const p4 = h._inspectViewPane(4); sessions[4].settle({ error: 'failed' }); await p4;
h._assetGallery.accept = false;
const p5 = h._inspectViewPane(5); sessions[5].settle({ ok: true }); await p5;
out.failed = [sessions[4].released, sessions[5].released, h._viewInspect, notices];
console.log('RESULT=' + JSON.stringify(out));
""")
    assert out["first"] == [True, 0, 2, 24, {"1": "S"}]
    assert out["opened"] == [1, True, 1, True]
    assert out["closed"] == [None, [1]]
    assert out["scope"] == [1, True, None]
    assert out["pending"] == [True, 2, 0]
    notice = ["This pane's media could not be opened.", "output-views"]
    assert out["failed"] == [1, 1, None, [notice, notice]]


def test_the_panes_models_reach_the_open_session():
    body = _method("_mountViewPanes")
    callback = re.search(r"onModels: (\(models\) => .*),\n", body)[1]
    out = node_run("""
class Host { callback() { return CALLBACK; } }
const h = new Host(), seen = [];
h.callback()(['none']);
h._viewInspect = { session: { refresh: m => seen.push(m) } };
h.callback()(['models']);
console.log('RESULT=' + JSON.stringify(seen));
""".replace("CALLBACK", callback))
    assert out == [["models"]]
    assert "onInspect: (index, request) => this._inspectViewPane(index, request)," in body


def test_a_proxy_event_never_rebuilds_an_external_overlay():
    body = GALLERY[GALLERY.index("const unsubscribeProxyStore"):GALLERY.index("    function destroy() {")]
    out = node_run("""
let renders = 0, listener = null;
const proxyStore = { subscribe: fn => { listener = fn; return () => {}; } };
const changedAssetIds = () => new Set(['x']), projectIdFromDir = () => 'P', currentProjectDir = () => 'P';
const state = { destroyed: false, inspectorCollapsed: true, overlayState: { open: true, external: {}, displaySignature: 'old' } };
const overlayWatchedAssets = () => [{ asset_id: 'x' }], displaySignature = () => 'new', renderInspectOverlay = () => renders++;
const selectedAssetIdsList = () => [], selectedAsset = () => null, renderDetail = () => {};
""" + _fn("isExternalOverlay") + body + """
listener({}); const external = renders;
state.overlayState.external = null; listener({});
console.log('RESULT=' + JSON.stringify([external, renders]));
""")
    assert out == [0, 1]


# ── Seams the host owns ──────────────────────────────────────────────────────

def test_every_scope_change_closes_a_panes_inspect_before_anything_else():
    host = (JS / "editor_widget.js").read_text(encoding="utf-8")

    def method(head: str) -> str:
        start = host.index(head)
        return host[start:host.index("\n    }\n", start)]

    for head, after in (("    updateProject(projectDir) {", "this._viewsCoordinator?.projectChanged();"),
                        ("    setViewsHost(host) {", "this._viewsCoordinator?.hostChanged();"),
                        ("    replaceControlsHost(controlsHost) {", "this._viewsCoordinator?.hostChanged();"),
                        ("    _unmountViewPanes() {", "this._viewsCoordinator?.setVisible(false);")):
        body = method(head)
        assert body.index("this._closeViewInspect();") < body.index(after), head
    inspect = method("    async _inspectViewPane(")
    assert inspect.index("this._closeViewInspect();") < inspect.index("openInspectSession(")  # latest press wins
    assert "if (this._viewInspect !== current || controller.signal.aborted) return;" in inspect


def test_external_media_never_reaches_a_project_route():
    for name, guard in (("loadGalleryMediaAsBlob", "if (asset?.external) return loadExternalMedia(asset, mediaEl);"),
                        ("configureDecodedImage", "if (asset.external) externalHandle = loadExternalMedia(asset, img);")):
        body = _fn(name)
        reads = [body.index(token) for token in ("galleryVideoSource(", "buildAssetViewUrl(") if token in body]
        assert body.index(guard) < min(reads), name
    assert "asset.has_thumbnail" in _fn("renderCompareChooser")  # external adapters say false
    detail = _fn("syncDetailDemand")
    assert "overlay.open && overlayPolicy().detailDemand ? currentOverlayAsset() : null" in detail
