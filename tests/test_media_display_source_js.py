"""The display authority for video (``web/js/media_display_source.js``) and its users.

* One resolver picks proxy, original or a notice ("blocked") for a video asset; a
  ready proxy always wins, "manual" never blocks the original, an undecided asset
  falls back to the recorded-codec hint, and a decode failure here blocks at once.
* Proxy state merges by epoch: a late list never undoes an event, and an undecided
  ("pending") decision never replaces a known one. The store is per project:
  copied projects share asset ids.
* Only a proven failure is a codec verdict: a source that failed to load (404, 5xx)
  fails the same loud way, so it is probed before anything is recorded.
* Events reach the store in ``cross_tab_sync.js`` (every mode), and a reconnect
  re-reads ``GET /proxies``; client calls are throttled and never throw.
* The playback source cache keeps an original and a proxy of one path as separate
  entries: a proxy landing never releases a held original, a new proxy rev retires
  only the older proxy, and a blocked path resolves to nothing.
* The viewport treats a blocked clip as a notice layer (other layers still draw),
  pins variants while playing (a pinned proxy only while it is the ready one), and
  grabs guides from the original.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_playback_prebuffer_js import _SURFACE_LOADER

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "web" / "js"
DISPLAY = (JS / "media_display_source.js").as_uri()
CLIENT = (JS / "proxy_client.js").as_uri()
CACHE = (JS / "playback_source_cache.js").as_uri()
SYNC = (JS / "cross_tab_sync.js").as_uri()


def _run_node(script: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    completed = subprocess.run([node, "--input-type=module", "-e", script],
                               capture_output=True, text=True, encoding="utf-8")
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout.strip().splitlines()[-1])


PRELUDE = f"""
globalThis.requestAnimationFrame = (cb) => setTimeout(() => cb(0), 0);
const d = await import({json.dumps(DISPLAY)});
const tick = () => new Promise((r) => setTimeout(r, 5));
"""


def test_the_resolver_prefers_a_ready_proxy_and_blocks_only_what_cannot_play():
    result = _run_node(PRELUDE + r"""
const v = (id, proxy, extra = {}) => ({ asset_id: id, asset_type: "video", path: `media/${id}.mov`, proxy, ...extra });
const urls = { original: (a) => `orig:${a.asset_id}`, proxy: (a, rev) => `proxy:${a.asset_id}:${rev}` };
const out = {};
for (const [name, asset] of Object.entries({
  ready: v("a", { decision: "undecodable", state: "ready", rev: "r1", epoch: 1 }),
  waiting: v("b", { decision: "undecodable", state: "waiting_idle", rev: "", epoch: 1 }),
  decodable: v("c", { decision: "decodable", state: "none", rev: "", epoch: 1 }),
  manualBuilding: v("d", { decision: "manual", state: "building", rev: "", epoch: 1 }),
  manualReady: v("e", { decision: "manual", state: "ready", rev: "r2", epoch: 1 }),
  runtime: v("f", { decision: "runtime", state: "queued", rev: "", epoch: 1 }),
  pendingProres: v("g", { decision: "pending", state: "none", rev: "", epoch: 1 },
                   { generation_summary: { codec: "prores_ks" } }),
  pendingImport: v("h", { decision: "pending", state: "none", rev: "", epoch: 1 }),
  noProxyField: v("i", undefined, { generation_summary: { save_preset: "Lossless FFV1 (RGB)" } }),
  image: { asset_id: "j", asset_type: "image", path: "media/j.png" },
})) {
  const source = d.displayVideoSource("P", asset, urls);
  out[name] = [source.variant, source.url];
}
out.blockedCopy = d.blockedState("P", v("b", { decision: "undecodable", state: "waiting_idle", epoch: 1,
                                          waiting_since: 123 }));
console.log(JSON.stringify(out));
""")
    assert result["ready"] == ["proxy", "proxy:a:r1"]
    assert result["waiting"] == ["blocked", None]
    assert result["decodable"] == ["original", "orig:c"]
    assert result["manualBuilding"] == ["original", "orig:d"]  # manual never blocks
    assert result["manualReady"] == ["proxy", "proxy:e:r2"]
    assert result["runtime"] == ["blocked", None]
    assert result["pendingProres"] == ["blocked", None]  # the hint is the fallback
    assert result["pendingImport"] == ["original", "orig:h"]
    assert result["noProxyField"] == ["blocked", None]
    assert result["image"] == ["original", "orig:j"]
    assert result["blockedCopy"]["state"] == "waiting_idle"
    assert result["blockedCopy"]["waitingSince"] == 123
    assert "idle" in result["blockedCopy"]["detail"]


def test_proxy_state_merges_by_epoch_and_pending_never_replaces_a_decision():
    result = _run_node(PRELUDE + r"""
const m = d.mergeProxyState;
const known = { decision: "undecodable", state: "building", rev: "", epoch: 10 };
console.log(JSON.stringify({
  older: m(known, { decision: "undecodable", state: "queued", epoch: 9 }),
  tie: m(known, { decision: "undecodable", state: "queued", epoch: 10 }),
  newer: m(known, { decision: "undecodable", state: "ready", rev: "r", epoch: 11 }),
  pending: m(known, { decision: "pending", state: "queued", epoch: 12 }),
  first: m(null, { decision: "pending", state: "none", epoch: 1 }),
}));
""")
    assert result["older"]["state"] == "building"
    assert result["tie"]["state"] == "building"
    assert result["newer"] == {"decision": "undecodable", "state": "ready", "rev": "r", "epoch": 11}
    assert result["pending"] == {"decision": "undecodable", "state": "queued", "epoch": 12}
    assert result["first"]["decision"] == "pending"


def test_the_store_overrides_a_stale_payload_and_notifies_once_per_frame():
    result = _run_node(PRELUDE + r"""
const asset = { asset_id: "a", asset_type: "video", path: "media/a.mov",
                proxy: { decision: "undecodable", state: "queued", rev: "", epoch: 5 } };
const calls = [];
d.proxyStore.subscribe((changes) => calls.push([...d.changedAssetIds(changes, "P")].sort()));
d.proxyStore.ingestItems("P", [{ asset_id: "a", decision: "undecodable", state: "building", rev: "", epoch: 6 }]);
d.proxyStore.ingestItems("P", [{ asset_id: "b", decision: "decodable", state: "none", rev: "", epoch: 6 }]);
d.proxyStore.ingestItems("P", [{ asset_id: "a", decision: "undecodable", state: "ready", rev: "r9", epoch: 7 }]);
// A list read before the events lands late: nothing moves.
const late = d.proxyStore.ingestAssets("P", [{ asset_id: "a", proxy: { decision: "undecodable", state: "queued", epoch: 5 } }]);
// A copy of the project shares the asset id: its state never leaks into this one.
d.proxyStore.ingestItems("Copy", [{ asset_id: "a", decision: "undecodable", state: "ready", rev: "other", epoch: 99 }]);
await tick();
const before = d.displayVideoSource("P", asset);
// The proxy fails to decode here: the asset goes back to a notice at once.
d.proxyStore.noteDecodeFailure("P", asset, "proxy", "r9");
await tick();
// A local failure belongs to those bytes: a Replace (new probe signature) plays again.
const original = { asset_id: "c", asset_type: "video", media_probe_signature: "1:1",
                   proxy: { decision: "decodable", state: "none", epoch: 1 } };
d.proxyStore.noteDecodeFailure("P", original, "original");
const failedOriginal = d.displayVideoSource("P", original).variant;
const replaced = d.displayVideoSource("P", { ...original, media_probe_signature: "2:2" }).variant;
console.log(JSON.stringify({ calls, late, before: [before.variant, before.rev],
  after: d.displayVideoSource("P", asset).variant, blocked: d.blockedState("P", asset).state,
  copy: d.displayVideoSource("Copy", asset).rev, failedOriginal, replaced }));
""")
    assert result["calls"][0] == ["a", "b"]  # three events, one notification
    assert result["late"] == []
    assert result["before"] == ["proxy", "r9"]
    assert result["copy"] == "other"
    assert result["after"] == "blocked" and result["blocked"] == "failed"
    assert result["failedOriginal"] == "blocked" and result["replaced"] == "original"


def test_client_calls_are_throttled_and_a_failure_is_recorded_and_reported_once():
    result = _run_node(PRELUDE + f"""
const c = await import({json.dumps(CLIENT)});
""" + r"""
const posts = [];
globalThis.window = { comfyAPI: { api: { api: { apiURL: (p) => `http://x${p}` } } } };
globalThis.fetch = async (url, init = {}) => { posts.push([url, init.method || "GET", init.body || ""]);
  return { ok: true, json: async () => ({ items: { z: { decision: "undecodable", state: "ready", rev: "rz", epoch: 99 } } }) }; };
const t0 = 1_000_000;
const first = c.requestProxyReconcile("P", { now: t0 });
const again = c.requestProxyReconcile("P", { now: t0 + 1000 });
const later = c.requestProxyReconcile("P", { now: t0 + 6000 });
const demand1 = c.reportProxyDemand("P", ["a", "b"], { now: t0 });
const demand2 = c.reportProxyDemand("P", ["a", "c"], { now: t0 + 1000 });
const asset = { asset_id: "a", asset_type: "video", proxy: { decision: "decodable", state: "none", epoch: 1 } };
const reported = [c.reportDecodeFailure("P", asset), c.reportDecodeFailure("P", asset)];
const blocked = d.displayVideoSource("P", asset).variant;
// Every socket of a project reconnects at once after a restart: one read serves them.
const [r1, r2] = [c.refreshProxyStates("P"), c.refreshProxyStates("P")];
await r1; await r2;
await tick();
console.log(JSON.stringify({ first, again, later, demand1, demand2, reported, blocked, posts,
  sameRefresh: r1 === r2,
  url: c.proxyFileUrl("My Project", "a b", "0123456789abcdef"), z: d.proxyStore.get("P", "z").state }));
""")
    assert [result["first"], result["again"], result["later"]] == [True, False, True]
    assert result["demand1"] == ["a", "b"] and result["demand2"] == ["c"]
    assert result["reported"] == [True, False] and result["blocked"] == "blocked"
    runtime = [p for p in result["posts"] if p[0].endswith("/proxies/runtime-undecodable")]
    assert len(runtime) == 1 and json.loads(runtime[0][2]) == {"asset_id": "a", "variant": "original"}
    assert result["url"] == "http://x/sonder-editor/project/My%20Project/proxy/a%20b/0123456789abcdef.mp4"
    assert result["z"] == "ready"
    assert result["sameRefresh"] is True
    assert len([p for p in result["posts"] if p[0].endswith("/proxies")]) == 1


def test_sync_feeds_every_proxy_event_to_the_store_and_rereads_after_a_reconnect():
    result = _run_node(PRELUDE + r"""
const sockets = [];
const fetched = [];
globalThis.window = { location: { href: "http://x/" },
  comfyAPI: { api: { api: { apiURL: (p) => `http://x${p}` } } } };
globalThis.fetch = async (url) => { fetched.push(String(url));
  return { ok: true, json: async () => ({ items: {} }) }; };
globalThis.WebSocket = class { constructor(url) { this.url = url; this.readyState = 1; sockets.push(this); }
  send() {} close() {} static get OPEN() { return 1; } };
""" + f"const {{ connectProjectSync }} = await import({json.dumps(SYNC)});" + r"""
const events = [];
const sync = connectProjectSync("P", { onProxyStatus: (e) => events.push(e.items.length) }, { clientId: "c" });
sockets[0].onopen();
sockets[0].onmessage({ data: JSON.stringify({ type: "proxy_status", epoch: 3,
  items: [{ asset_id: "a", decision: "undecodable", state: "building", rev: "", epoch: 3 }], queue: {} }) });
const afterFirstOpen = fetched.length;
sockets[0].onclose();
await new Promise((r) => setTimeout(r, 1150));
sockets[1].onopen();
await tick();
sync.close();
console.log(JSON.stringify({ events, state: d.proxyStore.get("P", "a").state, afterFirstOpen,
  refetched: fetched.filter((u) => u.endsWith("/sonder-editor/project/P/proxies")).length,
  reconciled: fetched.filter((u) => u.endsWith("/sonder-editor/project/P/proxies/reconcile")).length }));
""")
    # A restarted server lost its queue: the reconnect also has it reconcile.
    assert result == {"events": [1], "state": "building", "afterFirstOpen": 0, "refetched": 1, "reconciled": 1}


def test_the_source_cache_keeps_original_and_proxy_apart_and_never_releases_a_held_entry():
    result = _run_node(f"const {{ createPlaybackSourceCache }} = await import({json.dumps(CACHE)});" + r"""
let variant = { kind: "original", rev: "" };
const released = [];
const asset = { asset_id: "a", media_probe_signature: "1:2" };
const cache = createPlaybackSourceCache({
  getAssetForSourcePath: () => asset,
  getLiveSourcePaths: () => ["media/a.mov"],
  getStreamingMode: () => "direct",
  getDisplayVariant: () => variant,
  buildDirectUrl: (path, identity) => identity.variantKind === "proxy" ? `proxy:${identity.proxyRev}` : `view:${path}`,
});
const holder = { id: "playing-original" };
const original = await cache.resolve("media/a.mov");
cache.addHolder(original.cacheKey, holder);
// The proxy lands while the original element plays.
variant = { kind: "proxy", rev: "r1" };
const proxy = await cache.resolve("media/a.mov", { releaseHolders: (h) => released.push(h.id) });
cache.reconcile("proxy-state", { releaseHolders: (h) => released.push(h.id) });
const originalEntry = cache.entries.get(original.cacheKey);
// A guide grab asks for the original whatever is displayed.
const grab = await cache.resolve("media/a.mov", { variant: "original" });
// A rebuild: only the older proxy retires.
variant = { kind: "proxy", rev: "r2" };
const proxy2 = await cache.resolve("media/a.mov");
const keys = [...cache.entries.keys()].sort();
variant = { kind: "blocked", rev: "" };
const blocked = await cache.resolve("media/a.mov");
console.log(JSON.stringify({ original: original.url, proxy: proxy.url, proxy2: proxy2.url, grab: grab.url,
  released, originalHeld: originalEntry?.holders.size || 0, keys, blocked }));
""")
    assert result["original"] == "view:media/a.mov"
    assert result["proxy"] == "proxy:r1" and result["proxy2"] == "proxy:r2"
    assert result["grab"] == "view:media/a.mov"
    assert result["released"] == []  # a proxy landing never releases the held original
    assert result["originalHeld"] == 1
    assert result["keys"] == ["direct:media/a.mov|a|1:2|original", "direct:media/a.mov|a|1:2|proxy:r2"]
    assert result["blocked"] is None


def _viewport_script(body: str) -> str:
    script = _SURFACE_LOADER + f"""
globalThis.requestAnimationFrame = (cb) => setTimeout(() => cb(0), 0);
globalThis.cancelAnimationFrame = () => {{}};
const d = await import({json.dumps(DISPLAY)});
""" + r"""
const {createViewportSurface} = await loadSurface(__MODULE_URL__, '_state: state, _sourceCache: sourceCache,');
const texts = [], videos = [], draws = [], demand = [], failures = [];
globalThis.window = {SONDER_DEBUG_SESSION:false, __SONDER_DIAG_CLEARERS:new Set(), setTimeout, clearTimeout};
globalThis.fetch = async () => ({ok:true, blob: async () => new Blob([new Uint8Array(10)])});
class Video extends EventTarget {
 constructor(){super();this.readyState=0;this.videoWidth=0;this.videoHeight=0;this.duration=100;this.paused=true;this.seeking=false;this._time=0;this.error=null;videos.push(this);}
 get src(){return this._src;}
 set src(v){this._src=v;this.readyState=4;
   if (String(v).includes('loud')) { this.readyState=0; this.error={code:4, message:''};
     queueMicrotask(()=>this.dispatchEvent(new Event('error'))); return; }
   if (String(v).includes('prores')) { this.videoWidth=0; this.videoHeight=0; }
   else { this.videoWidth=320; this.videoHeight=180; }
   queueMicrotask(()=>this.dispatchEvent(new Event('loadeddata')));}
 get currentTime(){return this._time;}
 set currentTime(v){this._time=v;queueMicrotask(()=>{this.dispatchEvent(new Event('seeked'));queueMicrotask(()=>this.present(v));});}
 play(){this.paused=false;return Promise.resolve();} pause(){this.paused=true;} load(){}
 removeAttribute(k){if(k==='src'){delete this._src;this.readyState=0;}}
 requestVideoFrameCallback(cb){this.callbacks=this.callbacks||new Map();this.next=(this.next||0)+1;this.callbacks.set(this.next,cb);return this.next;}
 cancelVideoFrameCallback(id){this.callbacks?.delete(id);}
 present(time){for(const [id,cb] of [...(this.callbacks||new Map())]){this.callbacks.delete(id);cb(performance.now(),{mediaTime:time,presentedFrames:1});}}
}
globalThis.Image = class { set src(v){ this._src=v; queueMicrotask(()=>this.onload?.()); } };
globalThis.document = {visibilityState:'visible', hasFocus:()=>true, querySelectorAll:()=>[], createElement:()=>new Video()};
const ctx = new Proxy({globalAlpha:1, fillText:(t)=>texts.push(t), drawImage:(el)=>draws.push(el._src)},
  {get:(t,k)=>k in t?t[k]:()=>{}, set:(t,k,v)=>{t[k]=v;return true;}});
const scene = {clips:[
  {clip_id:'a', source_path:'media/plain.mp4', timeline_start_frame:0, timeline_end_frame:30, source_in_frame:0, track_index:0, opacity:1, fit_mode:'fit'},
  {clip_id:'b', source_path:'media/prores.mov', timeline_start_frame:0, timeline_end_frame:30, source_in_frame:0, track_index:1, opacity:0.5, fit_mode:'fit'},
], audio_tracks:[], guide_frames:[]};
const assets = {
  'media/plain.mp4': {asset_id:'pa', asset_type:'video', name:'plain.mp4', path:'media/plain.mp4', width:320, height:180,
                      proxy:{decision:'decodable', state:'none', rev:'', epoch:1}},
  'media/prores.mov': {asset_id:'pb', asset_type:'video', name:'prores.mov', path:'media/prores.mov', width:320, height:180,
                       proxy:{decision:'undecodable', state:'waiting_idle', rev:'', epoch:1}},
};
let frame = 0;
const surface = createViewportSurface({canvas:{width:320,height:180,getContext:()=>ctx}, getScene:()=>scene,
  getFrame:()=>frame, setFrame:(v)=>{frame=v;}, getTotalFrames:()=>30, getFps:()=>24,
  getAssetForSourcePath:(p)=>assets[p]||null, buildViewUrl:(p)=>'view:'+p, getProjectId:()=>'P',
  ...(globalThis.__confirm ? {confirmDecodeFailure: globalThis.__confirm} : {}),
  buildProxyUrl:(id, rev)=>`proxy:${id}:${rev}`, getStreamingMode:()=>'direct', getDecodeConcurrency:()=>4,
  isAdaptiveRebufferEnabled:()=>false, initialLiveMediaEnabled:true,
  onBlockedLayersVisible:(list)=>demand.push(list.map((a)=>a.asset_id)),
  onDecodeFailure:(asset, variant, rev)=>failures.push([asset.asset_id, variant, rev])});
const settle = async () => { for (let i = 0; i < 6; i++) { await new Promise((r)=>setTimeout(r, 5)); } };
""" + body
    return script.replace("__MODULE_URL__", json.dumps((JS / "viewport_surface.js").as_uri()))


def test_a_blocked_clip_is_a_notice_layer_and_its_proxy_repaints_a_paused_frame():
    result = _run_node(_viewport_script(r"""
surface.renderFrame(); await settle();
const first = { srcs: videos.map((v) => v._src), draws: [...draws], texts: [...texts], demand: [...demand] };
draws.length = 0; texts.length = 0;
// Another project's asset with the same id: this frame neither changes nor repaints.
d.proxyStore.ingestItems('Q', [{asset_id:'pb', decision:'undecodable', state:'ready', rev:'rq', epoch:2}]);
await settle();
const other = { srcs: videos.map((v) => v._src), draws: [...draws] };
// The proxy lands while paused: the frame repaints with it.
d.proxyStore.ingestItems('P', [{asset_id:'pb', decision:'undecodable', state:'ready', rev:'r1', epoch:2}]);
await settle();
const second = { srcs: videos.map((v) => v._src), draws: [...draws], texts: [...texts] };
surface.destroy();
console.log(JSON.stringify({ first, other, second }));
"""))
    first, second = result["first"], result["second"]
    assert all("rq" not in (src or "") for src in result["other"]["srcs"]) and result["other"]["draws"] == []
    # No <video> ever loads the undecodable original; the other layer still draws.
    assert all("prores" not in (src or "") for src in first["srcs"])
    assert first["draws"] == ["view:media/plain.mp4"]
    assert "Proxy waiting for ComfyUI" in first["texts"] and "Loading preview..." not in first["texts"]
    assert first["demand"] and first["demand"][0] == ["pb"]
    assert "proxy:pb:r1" in second["srcs"]
    assert second["draws"] == ["view:media/plain.mp4", "proxy:pb:r1"]
    assert "Proxy waiting for ComfyUI" not in second["texts"]


def test_variants_stay_pinned_while_playing_and_guides_come_from_the_original():
    result = _run_node(_viewport_script(r"""
const cache = surface._sourceCache, state = surface._state;
state.isPlaying = true;
const playing1 = cache.identityFor('media/prores.mov').variantKind;
d.proxyStore.ingestItems('P', [{asset_id:'pb', decision:'undecodable', state:'ready', rev:'r1', epoch:2}]);
await settle();
const playing2 = cache.identityFor('media/prores.mov').variantKind;
state.isPlaying = false; state.pinnedDisplayVariants.clear();
const paused = cache.identityFor('media/prores.mov');
const grab = cache.identityFor('media/prores.mov', { forceBlob: true, variant: 'original' }).variantKind;
// A decode failure overrides a pin: that original never shows a picture.
state.isPlaying = true; state.pinnedDisplayVariants.clear();
const plainPinned = cache.identityFor('media/plain.mp4').variantKind;
d.proxyStore.noteDecodeFailure('P', assets['media/plain.mp4'], 'original');
const plainAfterFailure = cache.identityFor('media/plain.mp4').variantKind;
// A pinned proxy holds only while it is the ready one: the server serves only the
// current rev's file, so a rebuild, a delete or a failure moves the pin.
state.pinnedDisplayVariants.clear();
const pinnedProxy = cache.identityFor('media/prores.mov').proxyRev;
d.proxyStore.ingestItems('P', [{asset_id:'pb', decision:'undecodable', state:'ready', rev:'r2', epoch:3}]);
const afterRebuild = cache.identityFor('media/prores.mov').proxyRev;
d.proxyStore.noteDecodeFailure('P', assets['media/prores.mov'], 'proxy', 'r2');
const afterProxyFailure = cache.identityFor('media/prores.mov').variantKind;
state.isPlaying = false;
surface.destroy();
console.log(JSON.stringify({ playing1, playing2, paused: [paused.variantKind, paused.proxyRev], grab,
  plainPinned, plainAfterFailure, pinnedProxy, afterRebuild, afterProxyFailure }));
"""))
    assert result["playing1"] == "blocked" and result["playing2"] == "blocked"
    assert result["paused"] == ["proxy", "r1"]
    assert result["grab"] == "original"
    assert result["plainPinned"] == "original" and result["plainAfterFailure"] == "blocked"
    assert [result["pinnedProxy"], result["afterRebuild"], result["afterProxyFailure"]] == ["r1", "r2", "blocked"]


def test_a_runtime_decode_failure_is_reported_and_turns_the_clip_into_a_notice():
    result = _run_node(_viewport_script(r"""
// The server allowed this one, but the browser fails it (silent form: 0x0).
assets['media/prores.mov'].proxy = {decision:'decodable', state:'none', rev:'', epoch:1};
surface.renderFrame(); await settle(); await settle();
const out = { failures: [...failures], blocked: surface._sourceCache.identityFor('media/prores.mov').variantKind,
  texts: [...texts] };
surface.destroy();
console.log(JSON.stringify(out));
"""))
    assert result["failures"] == [["pb", "original", ""]]
    assert result["blocked"] == "blocked"
    assert "Can't play this video here" in result["texts"]


def test_a_loud_failure_is_recorded_only_once_the_source_is_shown_to_answer():
    """`error.code 4` at readyState 0 is also what a 404 or a refused connection
    gives. Without the host's probe nothing is recorded or reported; with a probe that
    finds the file, the clip turns into a notice and the failure is reported once."""
    def run(confirm):
        prefix = "globalThis.__confirm = async () => true;\n" if confirm else ""
        return _run_node(prefix + _viewport_script(r"""
scene.clips[1].source_path = 'media/loud.mov';
assets['media/loud.mov'] = {asset_id:'pl', asset_type:'video', name:'loud.mov', path:'media/loud.mov',
  width:320, height:180, proxy:{decision:'decodable', state:'none', rev:'', epoch:1}};
surface.renderFrame(); await settle(); await settle();
const out = { failures: [...failures], variant: surface._sourceCache.identityFor('media/loud.mov').variantKind,
  srcs: videos.map((v) => v._src) };
surface.destroy();
console.log(JSON.stringify(out));
"""))
    unconfirmed, confirmed = run(False), run(True)
    assert "view:media/loud.mov" in unconfirmed["srcs"]
    assert unconfirmed["failures"] == [] and unconfirmed["variant"] == "original"
    assert confirmed["failures"] == [["pl", "original", ""]] and confirmed["variant"] == "blocked"


def test_confirm_probes_the_source_only_when_the_failure_cannot_tell():
    result = _run_node(PRELUDE + f"""
const c = await import({json.dumps(CLIENT)});
""" + r"""
globalThis.window = { comfyAPI: { api: { api: { apiURL: (p) => p } } } };
const probes = [];
let answer = { ok: false, status: 404 };
globalThis.fetch = async (url, init = {}) => { probes.push([url, init.headers?.Range || ""]); return answer; };
const loud = { error: { code: 4, message: "MEDIA_ELEMENT_ERROR: Format error" }, readyState: 0, src: "http://x/v.mov" };
const missing = await c.confirmDecodeFailure(loud);
answer = { ok: true, status: 206 };
const present = await c.confirmDecodeFailure(loud);
const demuxer = await c.confirmDecodeFailure({ error: { code: 4, message: "DEMUXER_ERROR_NO_SUPPORTED_STREAMS" },
  readyState: 0, src: "http://x/v.mov" });
const silent = await c.confirmDecodeFailure({ error: null, readyState: 4, videoWidth: 0, videoHeight: 0 });
const network = await c.confirmDecodeFailure({ error: { code: 2 }, readyState: 0, src: "http://x/v.mov" });
console.log(JSON.stringify({ missing, present, demuxer, silent, network, probes }));
""")
    assert [result["missing"], result["present"]] == [False, True]
    assert [result["demuxer"], result["silent"], result["network"]] == [True, True, False]
    # Only the ambiguous loud form costs a request: one byte of the source.
    assert result["probes"] == [["http://x/v.mov", "bytes=0-0"], ["http://x/v.mov", "bytes=0-0"]]
