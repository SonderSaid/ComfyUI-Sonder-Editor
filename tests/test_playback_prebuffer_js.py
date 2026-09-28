import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

# Imports viewport_surface.js with extra closure names exported for observation.
# A Windows checkout writes the file CRLF (core.autocrlf, no .gitattributes)
# while the hook anchor is LF, so the source is normalised first and a missed
# anchor fails loudly instead of silently leaving the hook out.
_SURFACE_LOADER = r"""
const {readFileSync} = await import('node:fs');
const SURFACE_HOOK_ANCHOR = '        renderFrame,\n        togglePlayback,';
const loadSurface = async (moduleUrl, extraExports, {crlf = false, dropAnchor = false} = {}) => {
 let source = readFileSync(new URL(moduleUrl), 'utf8');
 if (crlf) source = source.replace(/\r?\n/g, '\r\n');
 source = source.replace(/\r\n/g, '\n');
 if (dropAnchor) source = source.replace(SURFACE_HOOK_ANCHOR, '        renderFrame, togglePlayback,');
 source = source.replaceAll(/from "(\.\/[^"]+)"/g, (_, p) => 'from ' + JSON.stringify(new URL(p, moduleUrl).href));
 const anchors = source.split(SURFACE_HOOK_ANCHOR).length - 1;
 if (anchors === 0) throw new Error('viewport_surface.js test hook anchor not found');
 if (anchors > 1) throw new Error('viewport_surface.js test hook anchor is not unique');
 source = source.replace(SURFACE_HOOK_ANCHOR, '        ' + extraExports + '\n' + SURFACE_HOOK_ANCHOR);
 return import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
};
"""


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


def test_effective_boundary_groups_ignore_raw_hidden_endpoints_and_preserve_real_transitions():
    module_url = (ROOT / "web" / "js" / "viewport_surface.js").as_uri()
    script = f"""
const {{ _planEffectivePlaybackBoundaryGroups: plan }} = await import({json.dumps(module_url)});
const layer = (key, source = `${{key}}.mp4`) => ({{ key, clip: {{ source_path: source }} }});
const main = layer("main");
const cutA = layer("cut-a", "same.mp4");
const cutB = layer("cut-b", "same.mp4");
const upper = layer("upper");
const lower = layer("lower");
const resolver = (table) => (frame) => table.get(frame) || [];
const keys = (groups) => groups.map((group) => ({{
    frame: group.frame,
    keys: group.layers.map((entry) => entry.key),
    loopWrap: group.loopWrap,
}}));

const continuousTable = new Map();
for (const frame of [23, 24, 72, 73, 85, 86, 96, 97, 120, 121]) {{
    continuousTable.set(frame, [main]);
}}

const cutTable = new Map([[9, [cutA]], [10, [cutB]]]);
const revealTable = new Map([[19, [upper]], [20, [lower]]]);
const partialTable = new Map([[29, [upper, lower]], [30, [lower]]]);
const sameSourceTable = new Map([[39, [cutA]], [40, [cutB]]]);
const loopTable = new Map([[5, [main]], [19, [main]]]);

console.log(JSON.stringify({{
    continuous: keys(plan({{
        candidateFrames: [24, 73, 86, 97, 121],
        requiredLayersAtFrame: resolver(continuousTable),
    }})),
    cut: keys(plan({{ candidateFrames: [10], requiredLayersAtFrame: resolver(cutTable) }})),
    reveal: keys(plan({{ candidateFrames: [20], requiredLayersAtFrame: resolver(revealTable) }})),
    partial: keys(plan({{ candidateFrames: [30], requiredLayersAtFrame: resolver(partialTable) }})),
    sameSource: keys(plan({{ candidateFrames: [40], requiredLayersAtFrame: resolver(sameSourceTable) }})),
    loop: keys(plan({{
        candidateFrames: [5],
        requiredLayersAtFrame: resolver(loopTable),
        loopRange: {{ start: 5, end: 20 }},
    }})),
}}));
"""
    result = _run_node(script)

    assert result == {
        "continuous": [],
        "cut": [{"frame": 10, "keys": ["cut-b"], "loopWrap": False}],
        "reveal": [{"frame": 20, "keys": ["lower"], "loopWrap": False}],
        "partial": [],
        "sameSource": [{"frame": 40, "keys": ["cut-b"], "loopWrap": False}],
        "loop": [{"frame": 5, "keys": ["main"], "loopWrap": True}],
    }


def test_rebuffer_entry_classification_preserves_only_safe_work():
    module_url = (ROOT / "web" / "js" / "viewport_surface.js").as_uri()
    script = f"""
const {{ _classifyRebufferPrebufferEntry: classify }} = await import({json.dumps(module_url)});
console.log(JSON.stringify({{
    desired: classify({{ desired: true }}),
    waiting: classify({{ waiting: true }}),
    ready: classify({{ ready: true }}),
    claimed: classify({{ claimed: true }}),
    queued: classify({{}}),
    sourcePending: classify({{}}),
    active: classify({{}}),
    invalidDesired: classify({{ desired: true, valid: false }}),
}}));
"""
    result = _run_node(script)

    assert result == {
        "desired": "preserve",
        "waiting": "preserve",
        "ready": "preserve-ready",
        "claimed": "drop-reference",
        "queued": "cancel",
        "sourcePending": "cancel",
        "active": "cancel",
        "invalidDesired": "cancel",
    }


def test_aborting_active_low_decode_releases_single_limiter_slot_for_current_recovery():
    module_url = (ROOT / "web" / "js" / "viewport_surface.js").as_uri()
    script = f"""
const {{
    _createDecodeConcurrencyLimiter: createLimiter,
    _waitForMediaReady: waitForMediaReady,
}} = await import({json.dumps(module_url)});
globalThis.window = {{ setTimeout, clearTimeout }};
const limiter = createLimiter({{ getMaxConcurrent: () => 1 }});
const controller = new AbortController();
let lowStarted = false;
let highStarted = false;
const media = new EventTarget();
media.readyState = 0;
media.error = null;
const startedAt = performance.now();
const low = limiter.run("low", () => {{
    lowStarted = true;
    return waitForMediaReady(media, 2, 1500, {{ signal: controller.signal }});
}});
await new Promise((resolve) => setTimeout(resolve, 0));
const high = limiter.run("high", () => {{
    highStarted = true;
    return "current-ready";
}});
await new Promise((resolve) => setTimeout(resolve, 0));
const beforeAbort = limiter.snapshotStats();
controller.abort();
const values = await Promise.all([low, high]);
const elapsedMs = performance.now() - startedAt;
await new Promise((resolve) => setTimeout(resolve, 0));
const afterAbort = limiter.snapshotStats();
console.log(JSON.stringify({{ lowStarted, highStarted, beforeAbort, afterAbort, values, elapsedMs }}));
"""
    result = _run_node(script)

    assert result["lowStarted"] is True
    assert result["highStarted"] is True
    assert result["beforeAbort"]["decodeLowActive"] == 1
    assert result["beforeAbort"]["decodeHighQueued"] == 1
    assert result["afterAbort"]["decodeLowActive"] == 0
    assert result["afterAbort"]["decodeHighActive"] == 0
    assert result["afterAbort"]["decodeHighQueued"] == 0
    assert result["values"] == [None, "current-ready"]
    assert result["elapsedMs"] < 500


def test_queued_decode_cancellation_is_synchronous_and_counted():
    module_url = (ROOT / "web" / "js" / "viewport_surface.js").as_uri()
    script = f"""
const {{ _createDecodeConcurrencyLimiter: createLimiter }} = await import({json.dumps(module_url)});
const limiter = createLimiter({{ getMaxConcurrent: () => 1 }});
const controller = new AbortController();
const active = limiter.run("low", () => new Promise((resolve) => {{
    controller.signal.addEventListener("abort", () => resolve(null), {{ once: true }});
}}));
await new Promise((resolve) => setTimeout(resolve, 0));
let queuedJob = null;
const queued = limiter.run("low", () => "should-not-run", {{
    onQueued: (job) => {{ queuedJob = job; }},
}});
const before = limiter.snapshotStats();
const cancelled = limiter.cancelQueued(queuedJob, "test-current-recovery");
const queuedValue = await queued;
const afterCancel = limiter.snapshotStats();
const flushed = limiter.flushStats();
controller.abort();
await active;
console.log(JSON.stringify({{ cancelled, queuedValue, before, afterCancel, flushed }}));
"""
    result = _run_node(script)

    assert result["cancelled"] is True
    assert result["queuedValue"] is None
    assert result["before"]["decodeLowQueued"] == 1
    assert result["afterCancel"]["decodeLowQueued"] == 0
    assert result["flushed"]["decodeLowQueuedCancelled"] == 1


def test_repeated_playback_frame_guard_requires_a_valid_committed_composite():
    module_url = (ROOT / "web" / "js" / "viewport_surface.js").as_uri()
    script = f"""
const {{ _shouldSkipRepeatedPlaybackFrame: shouldSkip }} = await import({json.dumps(module_url)});
const valid = {{
    isPlaying: true,
    playbackCompositeCommitted: true,
    playbackRebuffering: false,
    nextFrame: 12,
    currentSceneFrame: 12,
    playbackLastCommittedFrame: 12,
    playbackSessionId: 3,
    playbackLastCommittedSessionId: 3,
    playbackWarmContentToken: 8,
    playbackLastCommittedContentToken: 8,
    canvasWidth: 320,
    canvasHeight: 180,
    playbackCanvasWidth: 320,
    playbackCanvasHeight: 180,
}};
const changed = (key, value) => shouldSkip({{ ...valid, [key]: value }});
console.log(JSON.stringify({{
    valid: shouldSkip(valid),
    newFrame: changed("nextFrame", 13),
    contentInvalidated: changed("playbackWarmContentToken", 9),
    resized: changed("canvasWidth", 640),
    newSession: changed("playbackSessionId", 4),
    rebuffering: changed("playbackRebuffering", true),
    uncommitted: changed("playbackCompositeCommitted", false),
}}));
"""
    result = _run_node(script)

    assert result == {
        "valid": True,
        "newFrame": False,
        "contentInvalidated": False,
        "resized": False,
        "newSession": False,
        "rebuffering": False,
        "uncommitted": False,
    }


def test_scene_source_cache_coalesces_and_retains_sequential_consumers():
    module_url = (ROOT / "web" / "js" / "playback_source_cache.js").as_uri()
    script = f"""
const {{ createPlaybackSourceCache }} = await import({json.dumps(module_url)});
let fetchCount = 0;
let createCount = 0;
let revokeCount = 0;
const events = [];
const asset = {{ asset_id: "asset-a", media_probe_signature: "12:345", size_bytes: 12 }};
const cache = createPlaybackSourceCache({{
    getAssetForSourcePath: () => asset,
    getLiveSourcePaths: () => ["media/clip.mp4"],
    buildDirectUrl: (path) => `view://${{path}}`,
    fetchMedia: async () => {{
        fetchCount += 1;
        await new Promise((resolve) => setTimeout(resolve, 5));
        return {{ ok: true, blob: async () => new Blob(["payload"]) }};
    }},
    createObjectUrl: () => `blob:test-${{++createCount}}`,
    revokeObjectUrl: () => {{ revokeCount += 1; }},
    recordEvent: (event) => events.push(event),
}});
const pendingA = cache.resolve("media/clip.mp4");
const pendingB = cache.resolve("media/clip.mp4");
const [first, coalesced] = await Promise.all([pendingA, pendingB]);
const holder = {{}};
cache.addHolder(first.cacheKey, holder);
cache.releaseHolder(first.cacheKey, holder);
const sequential = await cache.resolve("media/clip.mp4");
console.log(JSON.stringify({{
    fetchCount,
    createCount,
    revokeCount,
    sameUrl: first.url === coalesced.url && first.url === sequential.url,
    snapshot: cache.snapshot(),
    actions: events.map((event) => event.action),
}}));
"""
    result = _run_node(script)

    assert result["fetchCount"] == 1
    assert result["createCount"] == 1
    assert result["revokeCount"] == 0
    assert result["sameUrl"] is True
    assert result["snapshot"]["entryCount"] == 1
    assert result["snapshot"]["idleEntries"] == 1
    assert result["snapshot"]["retainedBytes"] == len(b"payload")
    assert result["actions"].count("cache_miss") == 1
    assert result["actions"].count("cache_coalesced") == 1
    assert result["actions"].count("cache_hit") == 1


def test_scene_source_cache_resolves_backslash_asset_revision_before_normalizing_identity():
    module_url = (ROOT / "web" / "js" / "playback_source_cache.js").as_uri()
    script = f"""
const {{ createPlaybackSourceCache }} = await import({json.dumps(module_url)});
const assets = new Map([["media\\\\clip.mp4", {{
    asset_id: "asset-a",
    media_probe_signature: "123:456",
}}]]);
const cache = createPlaybackSourceCache({{
    getAssetForSourcePath: (path) => assets.get(path) || null,
    getLiveSourcePaths: () => ["media\\\\clip.mp4"],
    buildDirectUrl: (path) => `view://${{path}}`,
    fetchMedia: async () => ({{ ok: true, blob: async () => new Blob(["payload"]) }}),
    createObjectUrl: () => "blob:revision-safe",
}});
const rawIdentity = cache.identityFor("media\\\\clip.mp4");
const normalizedIdentity = cache.identityFor("media/clip.mp4");
const resolved = await cache.resolve("media\\\\clip.mp4");
cache.reconcile("assets-refresh");
console.log(JSON.stringify({{
    rawIdentity,
    normalizedIdentity,
    resolved,
    snapshot: cache.snapshot(),
}}));
"""
    result = _run_node(script)

    assert result["rawIdentity"]["sourcePath"] == "media/clip.mp4"
    assert result["rawIdentity"]["revision"] == "asset-a|123:456"
    assert result["normalizedIdentity"]["revision"] == "asset-a|123:456"
    assert result["rawIdentity"]["cacheKey"] == result["normalizedIdentity"]["cacheKey"]
    assert result["resolved"]["url"] == "blob:revision-safe"
    assert result["snapshot"]["entryCount"] == 1
    assert result["snapshot"]["pendingEvictionEntries"] == 0


def test_scene_source_cache_replaces_revisions_and_prunes_removed_sources():
    module_url = (ROOT / "web" / "js" / "playback_source_cache.js").as_uri()
    script = f"""
const {{ createPlaybackSourceCache }} = await import({json.dumps(module_url)});
let signature = "10:100";
let livePaths = ["media/clip.mp4"];
let fetchCount = 0;
const revoked = [];
const events = [];
const cache = createPlaybackSourceCache({{
    getAssetForSourcePath: () => ({{ asset_id: "asset-a", media_probe_signature: signature }}),
    getLiveSourcePaths: () => livePaths,
    buildDirectUrl: (path) => `view://${{path}}`,
    fetchMedia: async () => {{
        fetchCount += 1;
        return {{ ok: true, blob: async () => new Blob([signature]) }};
    }},
    createObjectUrl: () => `blob:revision-${{fetchCount}}`,
    revokeObjectUrl: (url) => revoked.push(url),
    recordEvent: (event) => events.push(event),
}});
const first = await cache.resolve("media/clip.mp4");
const holder = {{ cacheKey: first.cacheKey }};
cache.addHolder(first.cacheKey, holder);
signature = "11:200";
const second = await cache.resolve("media/clip.mp4", {{
    releaseHolders: (item) => cache.releaseHolder(item.cacheKey, item),
}});
livePaths = [];
cache.reconcile("local-scene-mutation");
console.log(JSON.stringify({{
    fetchCount,
    differentUrl: first.url !== second.url,
    revoked,
    entryCount: cache.snapshot().entryCount,
    evictionReasons: events.filter((event) => event.action === "evicted").map((event) => event.reason),
}}));
"""
    result = _run_node(script)

    assert result["fetchCount"] == 2
    assert result["differentUrl"] is True
    assert result["revoked"] == ["blob:revision-1", "blob:revision-2"]
    assert result["entryCount"] == 0
    assert result["evictionReasons"] == ["revision-replaced", "local-scene-mutation"]


def test_scene_source_cache_aborts_obsolete_fetch_without_direct_fallback():
    module_url = (ROOT / "web" / "js" / "playback_source_cache.js").as_uri()
    script = f"""
const {{ createPlaybackSourceCache }} = await import({json.dumps(module_url)});
let livePaths = ["media/clip.mp4"];
const events = [];
const cache = createPlaybackSourceCache({{
    getAssetForSourcePath: () => ({{ asset_id: "asset-a", media_probe_signature: "10:100" }}),
    getLiveSourcePaths: () => livePaths,
    buildDirectUrl: (path) => `view://${{path}}`,
    fetchMedia: (_url, options) => new Promise((_resolve, reject) => {{
        options.signal.addEventListener("abort", () => {{
            const error = new Error("aborted");
            error.name = "AbortError";
            reject(error);
        }}, {{ once: true }});
    }}),
    recordEvent: (event) => events.push(event),
}});
const pending = cache.resolve("media/clip.mp4");
await new Promise((resolve) => setTimeout(resolve, 0));
livePaths = [];
cache.reconcile("source-removed");
const result = await pending;
await new Promise((resolve) => setTimeout(resolve, 0));
console.log(JSON.stringify({{
    result,
    entryCount: cache.snapshot().entryCount,
    actions: events.map((event) => event.action),
}}));
"""
    result = _run_node(script)

    assert result["result"] is None
    assert result["entryCount"] == 0
    assert "fetch_aborted" in result["actions"]
    assert "fallback_direct" not in result["actions"]


def test_scene_source_cache_defers_held_eviction_until_final_release():
    module_url = (ROOT / "web" / "js" / "playback_source_cache.js").as_uri()
    script = f"""
const {{ createPlaybackSourceCache }} = await import({json.dumps(module_url)});
let livePaths = ["media/clip.mp4"];
const revoked = [];
const cache = createPlaybackSourceCache({{
    getAssetForSourcePath: () => ({{ asset_id: "asset-a", media_probe_signature: "10:100" }}),
    getLiveSourcePaths: () => livePaths,
    buildDirectUrl: (path) => `view://${{path}}`,
    fetchMedia: async () => ({{ ok: true, blob: async () => new Blob(["payload"]) }}),
    createObjectUrl: () => "blob:held",
    revokeObjectUrl: (url) => revoked.push(url),
}});
const resolved = await cache.resolve("media/clip.mp4");
const holder = {{}};
cache.addHolder(resolved.cacheKey, holder);
livePaths = [];
cache.reconcile("source-removed");
const beforeRelease = cache.snapshot();
cache.releaseHolder(resolved.cacheKey, holder);
console.log(JSON.stringify({{ beforeRelease, afterRelease: cache.snapshot(), revoked }}));
"""
    result = _run_node(script)

    assert result["beforeRelease"]["entryCount"] == 1
    assert result["beforeRelease"]["pendingEvictionEntries"] == 1
    assert result["afterRelease"]["entryCount"] == 0
    assert result["revoked"] == ["blob:held"]


def test_scene_source_cache_full_clear_and_real_failure_fallback_are_bounded():
    module_url = (ROOT / "web" / "js" / "playback_source_cache.js").as_uri()
    script = f"""
const {{ createPlaybackSourceCache }} = await import({json.dumps(module_url)});
let diagnosticsEnabled = false;
const events = [];
const revoked = [];
const cache = createPlaybackSourceCache({{
    getAssetForSourcePath: () => ({{ asset_id: "asset-a", media_probe_signature: "10:100" }}),
    getLiveSourcePaths: () => ["media/clip.mp4"],
    buildDirectUrl: (path) => `view://${{path}}`,
    fetchMedia: async () => {{ throw new Error("network failed"); }},
    revokeObjectUrl: (url) => revoked.push(url),
    isDiagnosticsEnabled: () => diagnosticsEnabled,
    recordEvent: (event) => events.push(event),
}});
const fallback = await cache.resolve("media/clip.mp4");
const silentEventCount = events.length;
diagnosticsEnabled = true;
await cache.resolve("media/clip.mp4");
cache.clear("surface-destroy");
console.log(JSON.stringify({{
    fallbackUrl: fallback.url,
    silentEventCount,
    actions: events.map((event) => event.action),
    entryCount: cache.snapshot().entryCount,
    revoked,
}}));
"""
    result = _run_node(script)

    assert result["fallbackUrl"] == "view://media/clip.mp4"
    assert result["silentEventCount"] == 0
    assert result["actions"] == ["cache_hit", "evicted"]
    assert result["entryCount"] == 0
    assert result["revoked"] == []


def test_fake_raf_deduplicates_display_ticks_and_emits_bounded_perf_summaries():
    module_url = (ROOT / "web" / "js" / "viewport_surface.js").as_uri()
    script = f"""
const {{ createViewportSurface }} = await import({json.dumps(module_url)});
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
    get: (target, key) => key in target ? target[key] : (() => {{}}),
    set: (target, key, value) => {{ target[key] = value; return true; }},
}});
const canvas = {{ width: 320, height: 180, getContext: () => ctx }};
let frame = 0;
let frameCallbacks = 0;
const surface = createViewportSurface({{
    canvas,
    getScene: () => ({{ clips: [], audio_tracks: [], guide_frames: [] }}),
    getFrame: () => frame,
    setFrame: (value) => {{ frame = value; }},
    getTotalFrames: () => 48,
    getFps: () => 24,
    onFrameChange: () => {{
        frameCallbacks += 1;
        return {{ autoScrollMs: 0.1, timelineMs: 0.2, toolbarMs: 0.1, canvasBackingResized: false }};
    }},
}});
surface.startPlayback();
const base = performance.now();
for (let index = 0; index <= 60; index += 1) {{
    raf.shift()(base + index * (1000 / 60));
}}
surface.stopPlayback();
const stop = events.find((event) => event.kind === "playback_run_stop");
const summaries = events.filter((event) => event.kind === "playback_perf_summary");
console.log(JSON.stringify({{
    frame,
    frameCallbacks,
    rafTicks: stop.rafTicks,
    distinctFrames: stop.distinctFrames,
    repeatedFrames: stop.repeatedFrames,
    skippedFrames: stop.skippedFrames,
    viewportRenders: stop.timings.viewportRender.count,
    measuredFrameCallbacks: stop.timings.frameCallback.count,
    summaryCount: summaries.length,
    hasStart: events.some((event) => event.kind === "playback_run_start"),
    hasStop: !!stop,
}}));
"""
    result = _run_node(script)

    assert result == {
        "frame": 24,
        "frameCallbacks": 24,
        "rafTicks": 61,
        "distinctFrames": 24,
        "repeatedFrames": 37,
        "skippedFrames": 37,
        "viewportRenders": 25,
        "measuredFrameCallbacks": 24,
        "summaryCount": 2,
        "hasStart": True,
        "hasStop": True,
    }


def test_same_frame_composite_invalidation_bypasses_the_raf_guard():
    module_url = (ROOT / "web" / "js" / "viewport_surface.js").as_uri()
    script = f"""
const {{ createViewportSurface }} = await import({json.dumps(module_url)});
const events = [];
const raf = [];
globalThis.window = {{
    SONDER_DEBUG_SESSION: true,
    __SONDER_CANVAS_DIAG: {{ record: (kind, payload) => events.push({{ kind, ...payload }}) }},
    __SONDER_DIAG_CLEARERS: new Set(),
    setTimeout,
    clearTimeout,
}};
globalThis.document = {{ visibilityState: "visible", hasFocus: () => true, querySelectorAll: () => [] }};
globalThis.requestAnimationFrame = (callback) => {{ raf.push(callback); return raf.length; }};
globalThis.cancelAnimationFrame = () => {{}};
const ctx = new Proxy({{ globalAlpha: 1 }}, {{
    get: (target, key) => key in target ? target[key] : (() => {{}}),
    set: (target, key, value) => {{ target[key] = value; return true; }},
}});
let frame = 0;
let frameCallbacks = 0;
const surface = createViewportSurface({{
    canvas: {{ width: 320, height: 180, getContext: () => ctx }},
    getScene: () => ({{ clips: [], audio_tracks: [], guide_frames: [] }}),
    getFrame: () => frame,
    setFrame: (value) => {{ frame = value; }},
    getTotalFrames: () => 48,
    getFps: () => 24,
    onFrameChange: () => {{ frameCallbacks += 1; }},
}});
surface.startPlayback();
const base = performance.now();
raf.shift()(base + 10);
surface.invalidatePlaybackComposite();
raf.shift()(base + 20);
surface.stopPlayback();
const stop = events.find((event) => event.kind === "playback_run_stop");
console.log(JSON.stringify({{
    frameCallbacks,
    skippedFrames: stop.skippedFrames,
    viewportRenders: stop.timings.viewportRender.count,
}}));
"""
    result = _run_node(script)
    assert result == {"frameCallbacks": 1, "skippedFrames": 1, "viewportRenders": 2}


def test_playback_perf_diagnostics_are_silent_when_session_diagnostics_are_disabled():
    module_url = (ROOT / "web" / "js" / "viewport_surface.js").as_uri()
    script = f"""
const {{ createViewportSurface }} = await import({json.dumps(module_url)});
const events = [];
const raf = [];
globalThis.window = {{
    SONDER_DEBUG_SESSION: false,
    __SONDER_CANVAS_DIAG: {{ record: (kind, payload) => events.push({{ kind, ...payload }}) }},
    __SONDER_DIAG_CLEARERS: new Set(),
    setTimeout,
    clearTimeout,
}};
globalThis.requestAnimationFrame = (callback) => {{ raf.push(callback); return raf.length; }};
globalThis.cancelAnimationFrame = () => {{}};
const ctx = new Proxy({{ globalAlpha: 1 }}, {{
    get: (target, key) => key in target ? target[key] : (() => {{}}),
    set: (target, key, value) => {{ target[key] = value; return true; }},
}});
let frame = 0;
const surface = createViewportSurface({{
    canvas: {{ width: 320, height: 180, getContext: () => ctx }},
    getScene: () => ({{ clips: [], audio_tracks: [], guide_frames: [] }}),
    getFrame: () => frame,
    setFrame: (value) => {{ frame = value; }},
    getTotalFrames: () => 48,
    getFps: () => 24,
}});
surface.startPlayback();
const base = performance.now();
for (let index = 0; index < 5; index += 1) raf.shift()(base + index * 16);
surface.stopPlayback();
console.log(JSON.stringify({{ eventCount: events.length }}));
"""
    result = _run_node(script)
    assert result == {"eventCount": 0}


def test_diagnostic_clear_resets_playback_perf_counters_without_changing_run_identity():
    module_url = (ROOT / "web" / "js" / "viewport_surface.js").as_uri()
    script = f"""
const {{ createViewportSurface }} = await import({json.dumps(module_url)});
const events = [];
const raf = [];
globalThis.window = {{
    SONDER_DEBUG_SESSION: true,
    __SONDER_CANVAS_DIAG: {{ record: (kind, payload) => events.push({{ kind, ...payload }}) }},
    __SONDER_DIAG_CLEARERS: new Set(),
    setTimeout,
    clearTimeout,
}};
globalThis.document = {{ visibilityState: "visible", hasFocus: () => true, querySelectorAll: () => [] }};
globalThis.requestAnimationFrame = (callback) => {{ raf.push(callback); return raf.length; }};
globalThis.cancelAnimationFrame = () => {{}};
const ctx = new Proxy({{ globalAlpha: 1 }}, {{
    get: (target, key) => key in target ? target[key] : (() => {{}}),
    set: (target, key, value) => {{ target[key] = value; return true; }},
}});
let frame = 0;
const surface = createViewportSurface({{
    canvas: {{ width: 320, height: 180, getContext: () => ctx }},
    getScene: () => ({{ clips: [], audio_tracks: [], guide_frames: [] }}),
    getFrame: () => frame,
    setFrame: (value) => {{ frame = value; }},
    getTotalFrames: () => 48,
    getFps: () => 24,
}});
surface.startPlayback();
const firstStart = events.find((event) => event.kind === "playback_run_start");
const base = performance.now();
for (let index = 0; index < 5; index += 1) raf.shift()(base + index * 16);
events.length = 0;
for (const clear of Array.from(window.__SONDER_DIAG_CLEARERS)) clear();
const resumedStart = events.find((event) => event.kind === "playback_run_start");
for (let index = 5; index < 10; index += 1) raf.shift()(base + index * 16);
surface.stopPlayback();
const stop = events.find((event) => event.kind === "playback_run_stop");
console.log(JSON.stringify({{
    sameRun: firstStart.playbackRunId === resumedStart.playbackRunId,
    resumeReason: resumedStart.reason,
    resumed: resumedStart.resumed,
    rafTicksAfterClear: stop.rafTicks,
}}));
"""
    captured = _presentation_harness(clear=True)
    assert captured["beforeClear"] > 0
    assert captured["sameRun"] is True
    assert captured["stop"]["boundaryStaleDraws"] == 0
    # Only the eight draws after the clear are counted. Their frozen samples
    # are older than the age limit by then, so they are unverified, not stale.
    assert captured["stop"]["verifiedDraws"] + captured["stop"]["unverifiedDraws"] == 8
    result = _run_node(script)
    assert result == {
        "sameRun": True,
        "resumeReason": "diagnostic-clear",
        "resumed": True,
        "rafTicksAfterClear": 5,
    }


def test_canvas_and_controller_diagnostics_share_capture_identity():
    widget_source = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    controller_source = (ROOT / "web" / "js" / "editor_node_controller.js").read_text(encoding="utf-8")

    assert "window.__SONDER_DIAG_CAPTURE_ID = captureId" in widget_source
    assert "capture_id: currentSessionDiagCaptureId()" in widget_source
    assert "capture_id: currentSessionDiagCaptureId()" in controller_source
    assert "_sessionDiagLastRafTs = 0" in widget_source


def test_production_scheduler_and_abort_paths_use_the_tested_seams():
    source = (ROOT / "web" / "js" / "viewport_surface.js").read_text(encoding="utf-8")

    assert "for (const layer of boundaryLayers || effectivePlaybackBoundaryLayers(frame))" in source
    assert source.count("effectivePlaybackBoundaryGroups(candidateFrames)") >= 2
    assert "currentSafetyLayersAtFrame(targetSnapshot, targetFrame, offset)" in source
    assert 'clearDeferredNextBoundaryTargets("rebuffer-reentry")' in source
    assert '_classifyRebufferPrebufferEntry({' in source
    assert 'cancelQueuedPrebufferEntry(entry, "rebuffer-non-desired")' in source
    assert "entry.abortController?.abort?.();" in source
    assert "await waitForMediaReady(video, 2, 1500, { signal });" in source
    assert "signal," in source[source.index("async function loadPrebufferEntry"):source.index("function publishPrebufferEntryReady")]


def _budget_harness(body: str, max_bytes: str = "600"):
    """Source cache wired to a byte budget, with per-path blob sizes.

    Each source yields a 100-byte blob so totals are easy to reason about; the
    default 600-byte budget therefore holds six entries.
    """
    module_url = (ROOT / "web" / "js" / "playback_source_cache.js").as_uri()
    return f"""
const {{ createPlaybackSourceCache }} = await import({json.dumps(module_url)});
const events = [];
let clock = 0;
const assets = new Map();
const assetFor = (path) => {{
    if (!assets.has(path)) assets.set(path, {{ asset_id: path, media_probe_signature: "1:1" }});
    return assets.get(path);
}};
let live = [];
const cache = createPlaybackSourceCache({{
    getAssetForSourcePath: assetFor,
    getLiveSourcePaths: () => live,
    buildDirectUrl: (path) => `view://${{path}}`,
    fetchMedia: async () => ({{ ok: true, blob: async () => ({{ size: 100 }}) }}),
    createObjectUrl: () => `blob:${{Math.random()}}`,
    revokeObjectUrl: () => {{}},
    now: () => ++clock,
    recordEvent: (event) => events.push(event),
    getMaxRetainedBytes: () => {max_bytes},
}});
const holders = new Map();
async function load(path, holderId = path) {{
    live = [...new Set([...live, path])];
    const res = await cache.resolve(path);
    const holder = {{ path }};
    cache.addHolder(res.cacheKey, holder);
    holders.set(`${{path}}|${{holderId}}`, {{ key: res.cacheKey, holder }});
    return res;
}}
function release(path, holderId = path) {{
    const h = holders.get(`${{path}}|${{holderId}}`);
    cache.releaseHolder(h.key, h.holder);
}}
const evictedFor = () => events.filter((e) => e.action === "evicted" && e.reason === "byte-budget")
    .map((e) => e.sourcePath);
{body}
"""


def test_byte_budget_evicts_idle_entries_oldest_first():
    script = _budget_harness("""
for (const n of [1, 2, 3, 4, 5, 6]) { await load(`media/c${n}.mp4`); release(`media/c${n}.mp4`); }
// 600 bytes retained, exactly at budget - nothing evicted yet.
const atBudget = cache.snapshot().retainedBytes;
const evictedAtBudget = evictedFor().length;
await load("media/c7.mp4");
console.log(JSON.stringify({
    atBudget,
    evictedAtBudget,
    evicted: evictedFor(),
    snapshot: cache.snapshot(),
}));
""")
    result = _run_node(script)
    assert result["atBudget"] == 600
    assert result["evictedAtBudget"] == 0
    # c1 is the least recently used idle entry, so it goes first.
    assert result["evicted"] == ["media/c1.mp4"]
    assert result["snapshot"]["retainedBytes"] == 600


def test_byte_budget_never_evicts_a_held_source():
    """The safety invariant: a held blob backs a playing or prebuffering element.

    Freeing it would black-frame the viewport to save memory, so the budget
    reports the overage and stops instead.
    """
    script = _budget_harness("""
for (const n of [1, 2, 3, 4, 5, 6, 7, 8]) { await load(`media/c${n}.mp4`); }
console.log(JSON.stringify({
    evicted: evictedFor(),
    snapshot: cache.snapshot(),
    pressure: events.filter((e) => e.action === "budget_pressure").length > 0,
}));
""")
    result = _run_node(script)
    assert result["evicted"] == [], "a held source was evicted"
    assert result["snapshot"]["retainedBytes"] == 800
    assert result["snapshot"]["overBudgetBytes"] == 200
    assert result["snapshot"]["budgetPending"] is True
    assert result["pressure"] is True


def test_byte_budget_protects_the_entry_the_caller_is_about_to_hold():
    """resolve() returns before addHolder runs, leaving a window where the freshly
    fetched entry is holder-less. Evicting it there would make the fetch waste."""
    script = _budget_harness("""
for (const n of [1, 2, 3, 4, 5, 6]) { await load(`media/c${n}.mp4`); release(`media/c${n}.mp4`); }
const fresh = await cache.resolve("media/c7.mp4");
console.log(JSON.stringify({
    evicted: evictedFor(),
    freshStillPresent: cache.entries.has(fresh.cacheKey),
    canStillHold: cache.addHolder(fresh.cacheKey, {}),
}));
""")
    result = _run_node(script)
    assert result["freshStillPresent"] is True
    assert result["canStillHold"] is True, "the entry the caller was about to claim was evicted"
    assert "media/c7.mp4" not in result["evicted"]


def test_byte_budget_skips_entries_that_were_never_held():
    """Mid-handoff entries look idle but are not.

    Between resolve() resolving and the caller's continuation running addHolder,
    an entry has no holders. Evicting it there strands that caller with
    addHolder === false and no retry.
    """
    script = _budget_harness("""
for (const n of [1, 2, 3, 4, 5, 6]) { await load(`media/c${n}.mp4`); release(`media/c${n}.mp4`); }
// Resolved but never held - simulates a caller whose continuation has not run.
await cache.resolve("media/pending.mp4");
await load("media/c8.mp4");
console.log(JSON.stringify({ evicted: evictedFor() }));
""")
    result = _run_node(script)
    assert "media/pending.mp4" not in result["evicted"]


def test_unlimited_budget_reproduces_pre_budget_behaviour():
    script = _budget_harness("""
for (const n of [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) { await load(`media/c${n}.mp4`); release(`media/c${n}.mp4`); }
console.log(JSON.stringify({
    evicted: evictedFor(),
    snapshot: cache.snapshot(),
}));
""", max_bytes="null")
    result = _run_node(script)
    assert result["evicted"] == []
    assert result["snapshot"]["retainedBytes"] == 1000
    assert result["snapshot"]["budgetBytes"] is None
    assert result["snapshot"]["overBudgetBytes"] == 0
    assert result["snapshot"]["budgetPending"] is False


def test_byte_budget_sweep_tolerates_snapshot_reentrancy():
    """recordEvent calls snapshot() in the real host (viewport_surface records
    cache state on every event) while the sweep emits events mid-iteration."""
    module_url = (ROOT / "web" / "js" / "playback_source_cache.js").as_uri()
    script = f"""
const {{ createPlaybackSourceCache }} = await import({json.dumps(module_url)});
let clock = 0;
const snapshots = [];
let cache;
cache = createPlaybackSourceCache({{
    getAssetForSourcePath: (path) => ({{ asset_id: path, media_probe_signature: "1:1" }}),
    getLiveSourcePaths: () => [],
    buildDirectUrl: (path) => `view://${{path}}`,
    fetchMedia: async () => ({{ ok: true, blob: async () => ({{ size: 100 }}) }}),
    createObjectUrl: () => `blob:${{Math.random()}}`,
    revokeObjectUrl: () => {{}},
    now: () => ++clock,
    recordEvent: () => {{ if (cache) snapshots.push(cache.snapshot().retainedBytes); }},
    getMaxRetainedBytes: () => 300,
}});
for (const n of [1, 2, 3, 4, 5]) {{
    const res = await cache.resolve(`media/c${{n}}.mp4`);
    const holder = {{}};
    cache.addHolder(res.cacheKey, holder);
    cache.releaseHolder(res.cacheKey, holder);
}}
console.log(JSON.stringify({{ completed: true, finalBytes: cache.snapshot().retainedBytes, sampled: snapshots.length > 0 }}));
"""
    result = _run_node(script)
    assert result["completed"] is True
    assert result["sampled"] is True
    assert result["finalBytes"] <= 300


def test_source_cache_presets_survive_normalization_and_default_matches():
    settings_url = (ROOT / "web/js/editor_settings.js").as_uri()
    panel_url = (ROOT / "web/js/editor_settings_panel.js").as_uri()
    result = _run_node(f"""
const {{ DEFAULT_EDITOR_SETTINGS, normalizeEditorSettings }} = await import({json.dumps(settings_url)});
const {{ _PLAYBACK_SOURCE_CACHE_PRESETS: presets }} = await import({json.dumps(panel_url)});
console.log(JSON.stringify({{
    defaultValue: DEFAULT_EDITOR_SETTINGS.playback.sourceCacheMaxBytes,
    presets: presets.map(p => {{
        const value = p.value === "unlimited" ? null : Number(p.value);
        return [value, normalizeEditorSettings({{playback: {{sourceCacheMaxBytes: value}}}}).playback.sourceCacheMaxBytes];
    }}),
}}));
""")
    assert result["defaultValue"] == 1_000_000_000
    assert [result["defaultValue"], result["defaultValue"]] in result["presets"]
    assert all(before == after for before, after in result["presets"])
    panel = (ROOT / "web/js/editor_settings_panel.js").read_text(encoding="utf-8")
    assert 'if (suffix) suffix.style.display = showCustom ? "" : "none";' in panel


def _presentation_harness(sampled=True, clear=False, abort=False, audio=False):
    script = _SURFACE_LOADER + r"""
// Observe the actual closure; do not replace its state transitions.
// performance.now follows the synthetic refresh clock from a fixed origin, so
// wall-time allowances and sample ages elapse with the frames the loop
// presents, identically on every run.
let clock=1000;Object.defineProperty(globalThis,'performance',{value:{now:()=>clock,timeOrigin:0},configurable:true,writable:true});
const {createViewportSurface}=await loadSurface(__MODULE_URL__,'_state: state, _sourceCache: sourceCache, _drain: drainPendingReleases, _abortPreRolls: abortPreRolls,');
let fetches=0;globalThis.fetch=async()=>{fetches++;return {ok:true,blob:async()=>new Blob([new Uint8Array(100)])};};
const drawnSourceFrames=[];const events = [], raf = [], videos = [], draws = [], frames=[];
globalThis.window = {SONDER_DEBUG_SESSION:true, __SONDER_CANVAS_DIAG:{record:(kind,payload)=>events.push({kind,...payload})},__SONDER_DIAG_CLEARERS:new Set(),setTimeout,clearTimeout};
class Video extends EventTarget {
 constructor(){super();this.readyState=0;this.videoWidth=320;this.videoHeight=180;this.duration=100;this.paused=true;this.seeking=false;this._time=0;this.callbacks=new Map();this.next=0; videos.push(this);}
 get src(){return this._src;}
 set src(v){this._src=v;this.readyState=4;}
 get currentTime(){return this._time;}
 set currentTime(v){this._time=v;queueMicrotask(()=>this.dispatchEvent(new Event('seeked')));}
 play(){this.paused=false;this.startupTicks=1;this.decodeStarted=false;return Promise.resolve();}
 pause(){this.paused=true;}
 load(){}
 removeAttribute(k){if(k==="src"){delete this._src;this.readyState=0;}else delete this[k];}
 requestVideoFrameCallback(cb){this.callbacks.set(++this.next,cb);return this.next;}
 cancelVideoFrameCallback(id){this.callbacks.delete(id);}
 present(time){for(const [id,cb] of [...this.callbacks]){this.callbacks.delete(id);cb(performance.now(),{mediaTime:time,presentedFrames:1});}}
}
globalThis.document={visibilityState:'visible',hasFocus:()=>true,querySelectorAll:()=>[],createElement:()=>new Video()};
globalThis.requestAnimationFrame=cb=>{raf.push(cb);return raf.length;};globalThis.cancelAnimationFrame=()=>{};
const ctx=new Proxy({globalAlpha:1,drawImage:(el)=>{draws.push(el);if(frame>=8 && frame<=11)drawnSourceFrames.push(Math.floor(el.currentTime*24+0.5+1e-6));}}, {get:(t,k)=>k in t?t[k]:()=>{},set:(t,k,v)=>{t[k]=v;return true;}});
let frame=0;
const scene={clips:[{clip_id:'a',source_path:'a.mp4',timeline_start_frame:0,timeline_end_frame:8,source_in_frame:0},{clip_id:'b',source_path:'b.mp4',timeline_start_frame:8,timeline_end_frame:30,source_in_frame:12}],audio_tracks:[],guide_frames:[]};
const surface=createViewportSurface({canvas:{width:320,height:180,getContext:()=>ctx},getScene:()=>scene,getFrame:()=>frame,setFrame:v=>{frame=v;frames.push(v);},getTotalFrames:()=>30,getFps:()=>24,getAssetForSourcePath:()=>({width:320,height:180,media_kind:'video'}),buildViewUrl:p=>'https://fixture/'+p,getStreamingMode:()=> 'auto',getDecodeConcurrency:()=>8,isAdaptiveRebufferEnabled:()=>false});
const settle=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
surface.startPlayback();await settle();
if (__SAMPLED__) for (const video of videos) video.present(Math.floor(video.currentTime*24)/24);
const firstStart=events.find(e=>e.kind==="playback_run_start");
let beforeClear=null;let abortRestored=null;
if (__AUDIO__) { scene.audio_tracks=[{track_id:'audio-a',source_path:'audio-a.wav',timeline_start_frame:0,timeline_end_frame:8,source_in_frame:0},{track_id:'audio-b',source_path:'audio-b.wav',timeline_start_frame:8,timeline_end_frame:30,source_in_frame:0}]; }
const base=performance.now();
for(let i=0;i<20;i++){
if (__ABORT__ && i===6) {const e=[...surface._state.prebufferCache.values()].find(e=>e.preRollPhase==='rolling');surface._abortPreRolls('test-rebuffer');await settle();abortRestored=!!e && e.preRollPhase==='target' && e.video.paused && Math.abs(e.video.currentTime-e.targetTime)<1e-6;}
if (__CLEAR__ && i===12){beforeClear=events.filter(e=>e.kind==="playback_presentation_mismatch").length;for(const clear of window.__SONDER_DIAG_CLEARERS)clear();}clock=base+i*1000/24+0.1;for(const v of videos)if(!v.paused){if(v.startupTicks)v.startupTicks--;else{v._time+=1/24;
// Sampled videos report the first frame their decoder produces after play(),
// then freeze: the claim gate needs the first, the counters judge the rest.
if(__SAMPLED__&&!v.decodeStarted){v.decodeStarted=true;v.present(Math.floor(v._time*24+1e-6)/24);}}}for(const cb of raf.splice(0))cb(clock);await settle();}
const releasedPassed=!surface._state.videoCache.a && !videos[0].src;
const idleBeforeStop=surface._sourceCache.snapshot().idleEntries;
const fetchesBeforeStop=fetches;
const visibleBeforeStop=surface._state.videoCache.b;
surface.stopPlayback();await settle();
const stopPreserved=surface._state.videoCache.b===visibleBeforeStop && !!visibleBeforeStop.src && fetches===fetchesBeforeStop;
// A live prebuffer that re-adopts a parked element removes pending membership.
const adopted=new Video();adopted.src='adopted';surface._state.prebufferCache.set('audit',{video:adopted,claimedByActive:true});
surface._state.pendingRelease.add(adopted);surface._drain();
const adoptedRetained=adopted.src==='adopted' && !surface._state.pendingRelease.has(adopted);
surface._state.prebufferCache.delete('audit');
const audioReleased=!surface._state.audioCache['audio-a'];
const audioVisible=surface._state.audioCache['audio-b'];
console.log(JSON.stringify({drawnSourceFrames,audioReleased,audioVisibleRetained:!!audioVisible?.src,abortRestored,releasedPassed,idleBeforeStop,stopPreserved,adoptedRetained,beforeClear, sameRun:events.filter(e=>e.kind==="playback_run_start").every(e=>e.playbackRunId===firstStart.playbackRunId), frames, videos:videos.length,draws:draws.length,stop:events.find(e=>e.kind==='playback_run_stop')?.presentation,videoStates:videos.map(v=>({src:v.src,time:v.currentTime,paused:v.paused})),claims:events.filter(e=>e.kind==='playback_prebuffer_claim').length, kinds:[...new Set(events.map(e=>e.kind))]}));
surface.destroy();



"""
    script = script.replace("__MODULE_URL__", json.dumps((ROOT / "web/js/viewport_surface.js").as_uri()))
    return _run_node(script.replace("__SAMPLED__", json.dumps(sampled)).replace("__CLEAR__", json.dumps(clear)).replace("__ABORT__",json.dumps(abort)).replace("__AUDIO__",json.dumps(audio)))


def test_presentation_counters_detect_frozen_and_unsampled_boundary_draws():
    frozen = _presentation_harness()
    assert frozen["claims"] == 1
    assert frozen["stop"]["staleDraws"] > 0
    # The sample freezes on the roll's first decoded frame, behind its target,
    # so every boundary draw (claim frame + 0..3) is judged stale.
    assert frozen["stop"]["boundaryStaleDraws"] == 4
    assert frozen["stop"]["boundaryUnsampledDraws"] == 0
    missing = _presentation_harness(sampled=False)
    assert missing["claims"] == 1
    assert missing["stop"]["boundaryUnsampledDraws"] == 4
    assert missing["stop"]["verifiedDraws"] == 0
    assert missing["stop"]["staleDraws"] == 0


def test_presented_frame_classifier_never_extrapolates_or_verifies_missing_samples():
    module_url = (ROOT / "web/js/viewport_surface.js").as_uri()
    result = _run_node(f"""
const {{ _classifyPresentedFrame: classify }} = await import({json.dumps(module_url)});
const args = {{presentedMediaTime: 10/24, presentedAtMs: 100, nowMs: 120, expectedSourceFrame:12, fps:24, maxSampleAgeMs:250}};
console.log(JSON.stringify({{
    frozen:classify(args), later:classify({{...args,nowMs:200}}),
    old:classify({{...args,nowMs:400}}), unsupported:classify({{...args,supported:false}}),
    missing:classify({{...args,presentedMediaTime:null}}),
}}));
""")
    assert result["frozen"]["deltaFrames"] == -2
    assert result["frozen"]["stale"] is True
    assert result["later"]["presentedSourceFrame"] == result["frozen"]["presentedSourceFrame"]
    assert result["old"]["reason"] == "sample-stale"
    assert result["old"]["verified"] is False
    assert result["old"]["stale"] is False
    assert result["unsupported"]["reason"] == "unsupported"
    assert result["missing"]["verified"] is False


def test_byte_budget_shared_source_retains_other_holder_and_newest_idle_survives():
    result = _run_node(_budget_harness("""
await load('old'); release('old');
await load('shared', 'first'); await load('shared', 'second');
release('shared', 'first');
const sharedHeld = cache.snapshot().heldEntries;
await load('newest'); release('newest');
const idle = cache.snapshot().idleEntries;
await load('pressure');
console.log(JSON.stringify({sharedHeld,idle,evicted:evictedFor(),snapshot:cache.snapshot()}));
""", max_bytes="300"))
    assert result["sharedHeld"] == 1
    assert result["idle"] == 2
    assert result["evicted"] == ["old"]
    assert result["snapshot"]["heldEntries"] == 2


def test_playback_releases_passed_holder_and_preserves_stop_frame_and_readopted_media():
    result = _presentation_harness()
    assert result["releasedPassed"] is True
    assert result["idleBeforeStop"] == 1
    assert result["stopPreserved"] is True
    assert result["adoptedRetained"] is True


def _live_culling_harness(missing=False, failed=False, transparent=False, partial=False, image=False, wrong_dimensions=False):
    script = _SURFACE_LOADER + r"""
// Observe the actual closure; do not replace its state transitions.
const {createViewportSurface}=await loadSurface(__MODULE_URL__,'_state: state, _sourceCache: sourceCache, _drain: drainPendingReleases,');
let fetches=0;globalThis.fetch=async(url)=>{if (__FAILED__ && url.endsWith('b.mp4')) throw new Error('decode unavailable');fetches++;return {ok:true,blob:async()=>new Blob([new Uint8Array(100)])};};
const texts=[];const events = [], raf = [], videos = [], draws = [], frames=[];
globalThis.window = {SONDER_DEBUG_SESSION:true, __SONDER_CANVAS_DIAG:{record:(kind,payload)=>events.push({kind,...payload})},__SONDER_DIAG_CLEARERS:new Set(),setTimeout,clearTimeout};
class Video extends EventTarget {
 constructor(){super();this.readyState=0;this.videoWidth=320;this.videoHeight=180;this.duration=100;this.paused=true;this.seeking=false;this._time=0;this.callbacks=new Map();this.next=0; videos.push(this);}
 get src(){return this._src;}
 set src(v){this._src=v;this.readyState=4;if(__WRONG_DIMENSIONS__ && videos[0]===this){this.videoWidth=180;this.videoHeight=320;}if(__FAILED__ && v.endsWith('b.mp4'))this.error={code:4};}
 get currentTime(){return this._time;}
 set currentTime(v){this._time=v;queueMicrotask(()=>{this.dispatchEvent(new Event('seeked'));queueMicrotask(()=>this.present(v));});}
 play(){this.paused=false;return Promise.resolve();}
 pause(){this.paused=true;}
 load(){}
 removeAttribute(k){if(k==="src"){delete this._src;this.readyState=0;}else delete this[k];}
 requestVideoFrameCallback(cb){this.callbacks.set(++this.next,cb);return this.next;}
 cancelVideoFrameCallback(id){this.callbacks.delete(id);}
 present(time){for(const [id,cb] of [...this.callbacks]){this.callbacks.delete(id);cb(performance.now(),{mediaTime:time,presentedFrames:1});}}
}
globalThis.Image=class {constructor(){this.width=320;this.height=180;this.naturalWidth=320;this.naturalHeight=180;} set src(v){this._src=v;queueMicrotask(()=>this.onload?.());}};
globalThis.document={visibilityState:'visible',hasFocus:()=>true,querySelectorAll:()=>[],createElement:()=>new Video()};
globalThis.requestAnimationFrame=cb=>{raf.push(cb);return raf.length;};globalThis.cancelAnimationFrame=()=>{};
const ctx=new Proxy({globalAlpha:1,fillText:t=>texts.push(t),drawImage:(el)=>draws.push(el)}, {get:(t,k)=>k in t?t[k]:()=>{},set:(t,k,v)=>{t[k]=v;return true;}});
let frame=0;
const scene={clips:[{clip_id:'a',source_path:'a.mp4',timeline_start_frame:0,timeline_end_frame:8,source_in_frame:0},{clip_id:'b',source_path:'b.mp4',timeline_start_frame:0,timeline_end_frame:30,source_in_frame:0,track_index:1}],audio_tracks:[],guide_frames:[]};
if (__TRANSPARENT__)scene.clips.forEach(c=>c.opacity=0);
if (__PARTIAL__)scene.clips[1].opacity=0.5;
if (__WRONG_DIMENSIONS__)scene.clips[1].fit_mode='fit';
const surface=createViewportSurface({canvas:{width:320,height:180,getContext:()=>ctx},getScene:()=>scene,getFrame:()=>frame,setFrame:v=>{frame=v;frames.push(v);},getTotalFrames:()=>30,getFps:()=>24,getAssetForSourcePath:p=>({width:__MISSING__ && p==='b.mp4'?0:320,height:__MISSING__ && p==='b.mp4'?0:180,asset_type:__IMAGE__ && p==='b.mp4'?'image':'video'}),buildViewUrl:p=>'https://fixture/'+p,getStreamingMode:()=> 'auto',getDecodeConcurrency:()=>8,isAdaptiveRebufferEnabled:()=>false});
const settle=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
surface.setLiveMediaEnabled(true);await settle();await new Promise(resolve=>setTimeout(resolve,10));await settle();
const holders=[...surface._sourceCache.entries.values()].map(e=>({path:e.sourcePath,holders:[...e.holders].map(v=>Object.keys(surface._state.videoCache).find(k=>surface._state.videoCache[k]===v))}));
console.log(JSON.stringify({videos:videos.length,fetches,holders,draws:draws.map(v=>Object.keys(surface._state.videoCache).find(k=>surface._state.videoCache[k]===v)),texts}));surface.destroy();
"""
    script=script.replace("__MODULE_URL__",json.dumps((ROOT / "web/js/viewport_surface.js").as_uri()))
    for key,value in [("MISSING",missing),("FAILED",failed),("TRANSPARENT",transparent),("PARTIAL",partial),("IMAGE",image),("WRONG_DIMENSIONS",wrong_dimensions)]:
        script=script.replace("__"+key+"__",json.dumps(value))
    return _run_node(script)


def test_live_preview_culls_only_with_resolved_coverage_and_preserves_draw_order():
    covered = _live_culling_harness()
    assert covered["videos"] == 1
    assert covered["fetches"] == 1
    assert covered["holders"] == [{"path": "b.mp4", "holders": ["b"]}]
    assert covered["draws"] == ["b"]
    missing = _live_culling_harness(missing=True)
    assert missing["videos"] == 2
    assert missing["draws"] == ["a", "b"]
    failed = _live_culling_harness(failed=True)
    assert failed["videos"] == 2
    assert failed["draws"] == ["a"]
    transparent = _live_culling_harness(transparent=True)
    assert "Loading preview..." not in transparent["texts"]
    partial = _live_culling_harness(partial=True)
    assert partial["draws"] == ["a", "b"]


def test_live_preview_does_not_cull_for_alpha_images_or_wrong_decoded_dimensions():
    image = _live_culling_harness(image=True)
    assert image["videos"] == 1
    assert image["draws"] == ["a", None]
    mismatch = _live_culling_harness(wrong_dimensions=True)
    assert mismatch["videos"] == 2
    assert mismatch["fetches"] == 2
    assert mismatch["draws"] == ["a", "b"]


def test_preroll_planner_horizons_budgets_discontinuities_and_one_sided_claim():
    module_url = (ROOT / "web/js/viewport_surface.js").as_uri()
    result = _run_node(f"""
const {{_planPlaybackPreRoll:plan}}=await import({json.dumps(module_url)});
const a={{fps:24,currentFrame:90,targetFrame:100,targetSourceFrame:56,sourceInFrame:56,clipLengthFrames:68,rebuffering:false,decodeConcurrency:8,activePreRolls:0,phase:'target'}};
console.log(JSON.stringify({{
 leads:[24,30,60,12].map(fps=>plan({{...a,fps}}).leadInFrames),
 horizon:[90,94,97,100].map(currentFrame=>plan({{...a,currentFrame}}).action),
 start:plan({{...a,currentFrame:97,phase:'lead-in'}}).action,
 noRoom:[0,2].map(sourceInFrame=>plan({{...a,currentFrame:94,sourceInFrame}}).reason),
 short:plan({{...a,currentFrame:94,clipLengthFrames:3}}).reason,
 budget:plan({{...a,currentFrame:94,activePreRolls:2}}).reason,
 single:plan({{...a,currentFrame:94,decodeConcurrency:1}}).reason,
 abort:[{{rebuffering:true,currentFrame:98}},{{currentFrame:101}},{{currentFrame:94}}].map(v=>plan({{...a,phase:'rolling',...v}}).action),
 hold:plan({{...a,phase:'rolling',currentFrame:100}}).action,
}}));
""")
    assert result == {"leads": [3,4,6,2], "horizon": ["hold","park-lead-in","skip","skip"],
        "start":"start-roll", "noRoom":["no-room","no-room"], "short":"no-room",
        "budget":"budget", "single":"budget", "abort":["abort","abort","abort"],
        "hold":"hold"}


def test_rolling_claim_gate_needs_a_running_decoder_inside_a_one_frame_window():
    module_url = (ROOT / "web/js/viewport_surface.js").as_uri()
    result = _run_node(f"""
const {{_rollingClaimDecision:decide}}=await import({json.dumps(module_url)});
const running={{decoderRunning:true,targetFrame:56}};
console.log(JSON.stringify({{
 idle:[55,56,57,58].map(currentFrame=>decide({{targetFrame:56,currentFrame}})),
 running:[55,56,57,58].map(currentFrame=>decide({{...running,currentFrame}})),
 held:[55,56,57].map(currentFrame=>decide({{...running,currentFrame,holding:true}})),
 heldIdle:decide({{targetFrame:56,currentFrame:57,holding:true}}),
 unknown:[decide({{...running,currentFrame:NaN}}),decide({{decoderRunning:true,currentFrame:56}})],
}}));
""")
    # In the decoder start-up gap the last decoded (lead-in) frame is painted,
    # so an idle decoder is never claimable, even with currentTime on target.
    assert result["idle"] == ["wait", "wait", "wait", "overshoot"]
    # Once the decoder runs, currentTime predicts the painted frame: claim
    # anywhere in [target, target + 1], never before it; past it, overshoot.
    assert result["running"] == ["wait", "claim", "claim", "overshoot"]
    # With the clock held on the target, target + 1 would paint a frame the
    # held timeline has not reached.
    assert result["held"] == ["wait", "claim", "overshoot"]
    assert result["heldIdle"] == "overshoot"
    assert result["unknown"] == ["wait", "wait"]


def test_display_phase_is_estimated_at_the_frames_presentation():
    module_url = (ROOT / "web/js/viewport_surface.js").as_uri()
    result = _run_node(f"""
const {{_displayPhaseEstimate:estimate,_displayPhaseFromSamples:combine}}=await import({json.dumps(module_url)});
// Frame 54 was presented at 1000 ms; read 10 ms later, currentTime has run on
// 0.24 frame from 53.45, where the frame began to paint: phase 0.55.
const read=(o)=>estimate({{mediaTime:54/24,presentationTime:1000,nowMs:1010,currentTime:(53.45+0.24)/24,fps:24,...o}});
const round=v=>v===null?null:Math.round(v*100)/100;
console.log(JSON.stringify({{
 late:round(read({{}})),
 onTime:round(read({{nowMs:1000,currentTime:53.45/24}})),
 negative:round(read({{mediaTime:54/24,currentTime:(54.2+0.24)/24}})),
 unusable:[read({{mediaTime:NaN}}),read({{presentationTime:undefined}}),read({{fps:0}}),read({{currentTime:52/24}})],
 samples:[combine([]),combine([0.3]),combine([0.2,0.4]),combine([0.6,-0.02,0.55]),combine([0.9,0.1,0.2,0.3])].map(round),
}}));
""")
    # currentTime taken back to the presentation removes the callback's delay.
    assert (result["late"], result["onTime"], result["negative"]) == (0.55, 0.55, -0.2)
    # Missing inputs, no frame rate, or a phase of a whole frame are unusable.
    assert result["unusable"] == [None, None, None, None]
    # One sample as is; two averaged; three by median, rejecting an outlier;
    # only the latest three count.
    assert result["samples"] == [None, 0.3, 0.3, 0.55, 0.2]


def test_tail_stop_decision_stops_on_the_last_source_frame():
    module_url = (ROOT / "web/js/viewport_surface.js").as_uri()
    result = _run_node(f"""
const {{_tailStopDecision:decide}}=await import({json.dumps(module_url)});
console.log(JSON.stringify([46,47,48,99].map(currentFrame=>decide({{currentFrame,lastSourceFrame:47}})).concat(
 decide({{currentFrame:47,lastSourceFrame:null}}), decide({{currentFrame:NaN,lastSourceFrame:47}}),
 decide({{currentFrame:40,lastSourceFrame:47,ended:true}}))));
""")
    # Stop on the last frame (it stays painted), and past it as the backstop.
    # Media that ended short of the out point stops too: play() would restart it.
    assert result == ["run", "stop", "stop", "stop", "none", "none", "stop"]


def test_a_tail_stop_holds_only_while_the_element_is_at_its_last_frame():
    # Clear sites cover every known reposition; this pins the invariant that
    # makes a missed one harmless, and the ended-media backstop.
    probe = _lag_harness(tailProbeFrame=30)["tailProbe"]
    assert probe == {"staleRuns": True, "endedStops": True}


def test_preroll_survives_current_safety_reclassification_and_lands():
    result = _presentation_harness()
    assert result["stop"]["preRollStarted"] == 1
    assert result["stop"]["preRollLanded"] == 1
    assert result["stop"]["preRollRollingFrames"] > 0
    assert result["stop"]["preRollAborted"] == 0


def test_preroll_abort_restores_parked_target_without_releasing_its_source():
    result = _presentation_harness(abort=True)
    assert result["abortRestored"] is True
    assert result["stop"]["preRollAborted"] == 1
    assert result["stopPreserved"] is True


def test_audio_departure_releases_holder_and_stop_preserves_visible_audio():
    result = _presentation_harness(audio=True)
    assert result["audioReleased"] is True
    assert result["audioVisibleRetained"] is True


def test_cancelled_acquisitions_expire_budget_protection_without_stranding_coalesced_consumer():
    result = _run_node(_budget_harness("""
for(let i=0;i<8;i++) {
 const path=`cancelled-${i}`;live.push(path);const holder={};
 const result=await cache.resolve(path,{acquisitionHolder:holder});
 cache.releaseHolder(result.cacheKey,holder);
}
const cancelled=cache.snapshot();
live.push('shared');const abandoned={},survivor={};
const one=cache.resolve('shared',{acquisitionHolder:abandoned});
const two=cache.resolve('shared',{acquisitionHolder:survivor});
const a=await one;cache.releaseHolder(a.cacheKey,abandoned);
await load('pressure');release('pressure');
const b=await two;const video={};const adopted=cache.addHolder(b.cacheKey,video);
cache.releaseHolder(b.cacheKey,survivor);
console.log(JSON.stringify({cancelled,adopted,sameUrl:a.url===b.url,sharedHolders:cache.entries.get(b.cacheKey).holders.size}));
"""))
    assert result["cancelled"]["retainedBytes"] <= 600
    assert result["cancelled"]["heldEntries"] == 0
    assert result["cancelled"]["budgetPending"] is False
    assert result["adopted"] is True
    assert result["sameUrl"] is True
    assert result["sharedHolders"] == 1


def test_rolling_draws_target_source_frames_without_half_frame_advance():
    result = _presentation_harness()
    assert result["drawnSourceFrames"] == [12,13,14,15]


def test_pending_acquisition_holder_yields_to_scene_teardown():
    module_url = (ROOT / "web/js/playback_source_cache.js").as_uri()
    result = _run_node(f"""
const {{createPlaybackSourceCache}}=await import({json.dumps(module_url)});
let aborted=false;
const cache=createPlaybackSourceCache({{getLiveSourcePaths:()=>['a'],buildDirectUrl:()=>'/a',fetchMedia:(_,{{signal}})=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>{{aborted=true;reject(new DOMException('Aborted','AbortError'));}}))}});
const holder={{}};const key=cache.cacheKeyFor('a');holder._sonderReleaseSourceRef=()=>cache.releaseHolder(key,holder);
const pending=cache.resolve('a',{{acquisitionHolder:holder}});await Promise.resolve();
cache.reconcile('scene-switch',{{force:true,releaseHolders:h=>h._sonderReleaseSourceRef?.()}});
await pending;
console.log(JSON.stringify({{aborted,entries:cache.snapshot().entryCount}}));
""")
    assert result == {"aborted": True, "entries": 0}


def test_surface_test_hook_survives_a_crlf_checkout_and_a_missed_anchor_fails_loudly():
    module_url = json.dumps((ROOT / "web/js/viewport_surface.js").as_uri())
    result = _run_node(_SURFACE_LOADER + f"""
const crlf = await loadSurface({module_url}, '_state: state,', {{crlf: true}});
let missed = '';
try {{ await loadSurface({module_url}, '_state: state,', {{dropAnchor: true}}); }} catch (error) {{ missed = error.message; }}
console.log(JSON.stringify({{crlfLoaded: typeof crlf.createViewportSurface === 'function', missed}}));
""")
    assert result == {"crlfLoaded": True, "missed": "viewport_surface.js test hook anchor not found"}


# Presentation-lag harness. One fake clock drives timers, video presentation
# (requestVideoFrameCallback) and requestAnimationFrame, in that order, once
# per display refresh. The fake video follows what Chromium measured on
# 2026-09-28 (plans/playback-presented-frame.md, Phase 0 result):
#   - drawImage paints the frame at floor(currentTime * fps), one frame older
#     on some refreshes (decodeLate), never newer; pausing leaves exactly the
#     currentTime frame on the canvas;
#   - currentTime jitters around the media clock by up to clockJitterMs, so a
#     video whose frame boundaries sit on the redraw tick (the live condition:
#     playhead == currentTime frame on 94% of ticks) flips either side of it;
#   - play() starts the media clock after clockStartupMs, and after play() or a
#     seek of a playing video the decoder paints nothing new for startupMs, so
#     drawImage keeps painting the last decoded frame; that gap is what put a
#     lead-in frame on screen at a cut;
#   - requestVideoFrameCallback reports presentations trailing the painted
#     frame by lagVsyncs, with seeded jitter.
# Each video draws from its own random stream, seeded from its source, once per
# vsync, so neither the number of canvas draws nor the number of elements can
# change another element's outcome. The harness composites ONE lane: a vsync's
# screen is the last drawImage after the last fillRect, black when a fillRect
# has no image after it. The clips form a continuation chain: clip b's source
# frame 24 + k shows timeline frame 48 + k, so every painted picture maps to
# one timeline frame and an out-of-edit frame is detectable.
_LAG_HARNESS = r"""
const cfg = Object.assign({
 fps: 24, hz: 120, startupMs: 60, clockStartupMs: 42, clockJitterMs: 4, lagVsyncs: 2, jitter: 0.35,
 decodeLate: 0.05, flipLag: false, seed: 7, debugSession: false, adaptiveRebuffer: false, rvfc: true,
 clips: [
  {clip_id: 'a', source_path: 'a.mp4', timeline_start_frame: 0, timeline_end_frame: 48, source_in_frame: 0, source_out_frame: 48},
  {clip_id: 'b', source_path: 'b.mp4', timeline_start_frame: 48, timeline_end_frame: 120, source_in_frame: 24, source_out_frame: 96},
 ],
 startFrame: 0, stopFrame: 110, totalFrames: 120,
}, __CONFIG__);
// Display phase per clip id; setting one also makes callbacks report
// presentationTime, which the surface's phase estimate reads.
const phaseModel = cfg.phase !== undefined;
const phaseFor = (clip) => (phaseModel && clip ? (typeof cfg.phase === 'number' ? cfg.phase : (cfg.phase[clip.clip_id] ?? 0)) : 0);

let T = 1000; const vsyncMs = 1000 / cfg.hz;
const timers = []; let timerSeq = 0;
const fakeSetTimeout = (fn, ms = 0) => { const id = ++timerSeq; timers.push({id, at: T + Math.max(0, Number(ms) || 0), fn}); return id; };
const fakeClearTimeout = (id) => { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); };
Object.defineProperty(globalThis, 'performance', {value: {now: () => T, timeOrigin: 0}, configurable: true, writable: true});
const streamFor = (key) => { let h = (2166136261 ^ cfg.seed) >>> 0; for (const ch of key) h = Math.imul(h ^ ch.charCodeAt(0), 16777619) >>> 0; return () => ((h = (Math.imul(h, 1664525) + 1013904223) >>> 0) / 4294967296); };
const events = []; const videos = [];
globalThis.window = {SONDER_DEBUG_SESSION: cfg.debugSession, __SONDER_CANVAS_DIAG: {record: (kind, payload) => events.push({kind, ...payload})}, __SONDER_DIAG_CLEARERS: new Set(), setTimeout: fakeSetTimeout, clearTimeout: fakeClearTimeout};
globalThis.fetch = async () => ({ok: true, blob: async () => new Blob([new Uint8Array(100)])});
let vsyncIndex = 0;
// Vsyncs in which any video was sought, played or paused.
const controlLog = [];
class Video extends EventTarget {
 constructor() { super(); this.readyState = 0; this.videoWidth = 320; this.videoHeight = 180; this.duration = 100; this.paused = true; this.seeking = false; this._time = 0; this._offset = 0; this._decoded = null; this._late = false; this._jitter = 0; this._history = []; this._presented = null; this._presentedFrames = 0; this._clipsSeen = new Set(); this._shownAt = new Map(); this.callbacks = new Map(); this.next = 0; videos.push(this); }
 get src() { return this._src; }
 set src(v) { this._src = v; this.readyState = 4; this._decoded = this._frameAt(this.currentTime); }
 get currentTime() { return Math.max(0, this._time + (this.paused ? 0 : this._offset / 1000)); }
 set currentTime(v) {
  controlLog.push(vsyncIndex);
  this._time = Number(v); this._offset = 0; this.seeking = true; this._history = []; this._shownAt = new Map();
  if (!this.paused) this._decodeHoldUntil = T + cfg.startupMs;
  queueMicrotask(() => { this.seeking = false; if (this.paused) this._decoded = this._frameAt(this._time); this.dispatchEvent(new Event('seeked')); });
 }
 // A negative clockStartupMs models a clock that starts early (ahead).
 play() { if (this.paused) { controlLog.push(vsyncIndex); this.paused = false; if (cfg.clockStartupMs < 0) this._time -= cfg.clockStartupMs / 1000; this._clockHoldUntil = T + Math.max(0, cfg.clockStartupMs); this._decodeHoldUntil = T + cfg.startupMs; } return Promise.resolve(); }
 pause() { if (!this.paused) { controlLog.push(vsyncIndex); this._time = this.currentTime; this._offset = 0; if (T >= (this._decodeHoldUntil || 0)) this._decoded = this._paintAt(this._time); } this.paused = true; }
 load() {}
 removeAttribute(k) { if (k === 'src') { delete this._src; this.readyState = 0; } else delete this[k]; }
 cancelVideoFrameCallback(id) { this.callbacks.delete(id); }
 // syncPreparedVideoPlayback sets muted on every call, so this reveals it.
 get muted() { return true; }
 set muted(v) { controlLog.push(vsyncIndex); }
 _frameAt(t) { return Math.floor(t * cfg.fps + 1e-6); }
 // Where a playing decoder's picture sits: the currentTime frame shifted by
 // this clip's display phase. A seek paints _frameAt (no phase).
 _paintAt(t) { return Math.floor(t * cfg.fps + phaseFor(this.clip) + 1e-6); }
 get clip() { const key = String(this._sonderSourceCacheKey || ''); return cfg.clips.find(c => key.includes(c.source_path)) || null; }
 // The frame drawImage paints right now. With flipLag, the refresh on which
 // currentTime enters a frame still paints the previous one (1920x1088,
 // measured 2026-09-28: 50 of 51 such refreshes; the next one paints it).
 get drawnFrame() {
  if (this.paused || this.seeking || T < (this._decodeHoldUntil || 0)) return this._decoded;
  const settling = cfg.flipLag && this._flipVsync === vsyncIndex;
  return Math.max(this._decoded ?? -Infinity, this._paintAt(this.currentTime) - (this._late || settling ? 1 : 0));
 }
 advance(dt) {
  if (!this._src) return;
  const clip = this.clip; if (clip) this._clipsSeen.add(clip.clip_id);
  const key = clip?.source_path || ''; if (this._streamKey !== key) { this._streamKey = key; this._rand = streamFor(key); }
  this._late = this._rand() < cfg.decodeLate; this._jitter = this._rand() < cfg.jitter ? 1 : 0;
  if (!this.paused && T >= (this._clockHoldUntil || 0)) {
   const before = this._frameAt(this.currentTime); this._time += dt / 1000;
   this._offset = (this._rand() * 2 - 1) * cfg.clockJitterMs;
   if (this._frameAt(this.currentTime) !== before) this._flipVsync = vsyncIndex;
  } else this._offset = (this._rand() * 2 - 1) * cfg.clockJitterMs;
  if (!this.paused && !this.seeking && T >= (this._decodeHoldUntil || 0)) this._decoded = this.drawnFrame;
  if (this._decoded !== null && !this._shownAt.has(this._decoded)) this._shownAt.set(this._decoded, T);
  if (this._shownAt.size > 64) this._shownAt.delete(this._shownAt.keys().next().value);
  this._history.push(this._decoded); if (this._history.length > 16) this._history.shift();
 }
 presentStep() {
  if (!this._src || this.seeking || this._decoded === null) return;
  const h = this._history; const frame = this.paused ? this._decoded : h[Math.max(0, h.length - 1 - cfg.lagVsyncs - this._jitter)];
  if (frame === null || frame === undefined || frame === this._presented) return;
  this._presented = frame; this._presentedFrames += 1;
  const metadata = {mediaTime: frame / cfg.fps, presentedFrames: this._presentedFrames, expectedDisplayTime: T + vsyncMs};
  if (phaseModel) metadata.presentationTime = this._shownAt.get(frame) ?? T;
  for (const [id, cb] of [...this.callbacks]) { this.callbacks.delete(id); cb(T, metadata); }
 }
}
if (cfg.rvfc) Video.prototype.requestVideoFrameCallback = function (cb) { this.callbacks.set(++this.next, cb); return this.next; };
globalThis.document = {visibilityState: 'visible', hasFocus: () => true, querySelectorAll: () => [], createElement: () => new Video()};
// Cancellable, so a stopped playback loop does not keep ticking.
const raf = new Map(); let rafSeq = 0;
globalThis.requestAnimationFrame = cb => { raf.set(++rafSeq, cb); return rafSeq; }; globalThis.cancelAnimationFrame = id => { raf.delete(id); };
const paintLog = [];
// Paints of an element that is not an active playback video: a parked tail,
// or a roll before its claim committed.
let inactivePaints = 0;
const ctx = new Proxy({globalAlpha: 1,
 fillRect: () => paintLog.push({v: vsyncIndex, black: true}),
 drawImage: (el) => {
  if (surface._state.isPlaying && ![...surface._state.activePlaybackVideos.values()].some(a => a.video === el)) inactivePaints++;
  const c = el.clip; const f = el.drawnFrame; paintLog.push({v: vsyncIndex, clip: c?.clip_id || '?', shown: c && f !== null ? c.timeline_start_frame + f - c.source_in_frame : null});
 },
}, {get: (t, k) => k in t ? t[k] : () => {}, set: (t, k, v) => { t[k] = v; return true; }});

const {createViewportSurface} = await loadSurface(__MODULE_URL__, '_state: state, _repaint: repaintPlaybackIfAdvanced, _sync: syncPreparedVideoPlayback,');
let frame = cfg.startFrame; const scene = {clips: cfg.clips, audio_tracks: [], guide_frames: []};
const surface = createViewportSurface({canvas: {width: 320, height: 180, getContext: () => ctx}, getScene: () => scene, getFrame: () => frame, setFrame: v => { frame = v; }, getTotalFrames: () => cfg.totalFrames, getFps: () => cfg.fps, getAssetForSourcePath: () => ({width: 320, height: 180, media_kind: 'video'}), buildViewUrl: p => 'https://fixture/' + p, getStreamingMode: () => 'auto', getDecodeConcurrency: () => 8, isAdaptiveRebufferEnabled: () => cfg.adaptiveRebuffer, getLoopRange: () => cfg.loopRange || null});
const settle = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
const screen = []; let rebufferEntries = 0; let wasRebuffering = false;
const vsync = async () => {
 T += vsyncMs; vsyncIndex += 1;
 for (const t of timers.filter(t => t.at <= T).sort((a, b) => a.at - b.at)) { fakeClearTimeout(t.id); t.fn(); }
 await settle();
 for (const v of videos) v.advance(vsyncMs);
 for (const v of videos) v.presentStep();
 await settle();
 const due = [...raf.values()]; raf.clear(); for (const cb of due) cb(T);
 await settle();
 const mine = paintLog.filter(d => d.v === vsyncIndex);
 // Per lane, the frame the latest composite painted of it (multi-lane runs).
 const lastFill = mine.map(d => !!d.black).lastIndexOf(true);
 if (lastFill >= 0) for (const d of mine.slice(lastFill + 1)) if (!d.black) laneShown[d.clip] = d.shown;
 for (const [clip, shown] of Object.entries(laneShown)) (laneFrames[clip] ||= new Set()).add(shown);
 let now = null;
 if (mine.length) { const lastBlack = mine.map(d => !!d.black).lastIndexOf(true); const image = mine.slice(lastBlack + 1).filter(d => !d.black).pop(); now = image ? image : (lastBlack >= 0 ? {black: true, shown: null} : null); }
 const last = now || (screen.length ? screen[screen.length - 1] : null);
 // The frame the active clip-b element would paint now, as a timeline frame.
 const bVideo = surface._state.activePlaybackVideos.get('b')?.video;
 const bClip = cfg.clips[1];
 const bDrawn = bVideo && bVideo.drawnFrame !== null ? bClip.timeline_start_frame + bVideo.drawnFrame - bClip.source_in_frame : null;
 screen.push(last ? {...last, tl: frame, bDrawn} : {tl: frame, shown: null, bDrawn});
 const rebuffering = !!surface._state.playbackRebuffering; if (rebuffering && !wasRebuffering) rebufferEntries++; wasRebuffering = rebuffering;
 // How far each clip's element ran, in source frames, while it was active.
 for (const [key, active] of surface._state.activePlaybackVideos) {
  const c = cfg.clips.find(x => x.clip_id === key); if (!c || active.video.seeking) continue;
  activeRunFrames[key] = Math.max(activeRunFrames[key] ?? -Infinity, active.video._frameAt(active.video.currentTime));
  if (active.video.drawnFrame !== null) activeDrawnFrames[key] = Math.max(activeDrawnFrames[key] ?? -Infinity, active.video.drawnFrame);
 }
};
const activeRunFrames = {}; const activeDrawnFrames = {}; const laneShown = {}; const laneFrames = {};
surface.startPlayback(); await settle();
// Guard probe: mid-clip, call the real repaint directly with the recorded
// video one frame ahead, once plainly and once under each condition that must
// block it. The run stops after the probe; only its result is meaningful.
const probeGuards = () => {
 const S = surface._state; const entry = S.playbackPaint?.entries?.find(e => e.type === 'video');
 if (!entry) return {error: 'no recorded paint'};
 const el = entry.element; const active = S.activePlaybackVideos.get(entry.layerKey);
 const composites = () => paintLog.filter(d => d.black).length;
 const attempt = (setTime, mutate = () => {}, restore = () => {}) => {
  el._time = setTime(entry.paintedFrame) / cfg.fps; el._offset = 0;
  const before = composites(), controls = controlLog.length;
  mutate(); const did = surface._repaint(); restore();
  return {did, painted: composites() - before, control: controlLog.length - controls};
 };
 const ahead = f => f + 1.5;
 // A second paint in the same refresh must keep the pending confirm.
 const samePaint = () => {
  el._time = (entry.paintedFrame + 1.5) / cfg.fps; el._offset = 0;
  surface._repaint(); const first = S.playbackPaint.entries.find(e => e.type === 'video').confirmPending;
  surface._repaint(); const second = S.playbackPaint.entries.find(e => e.type === 'video').confirmPending;
  return {first, second};
 };
 return {
  sameRefreshConfirm: samePaint(),
  advanced: attempt(ahead),
  rebuffering: attempt(ahead, () => { S.playbackRebuffering = true; }, () => { S.playbackRebuffering = false; }),
  held: attempt(ahead, () => { S.playbackBlockedSinceMs = T; }, () => { S.playbackBlockedSinceMs = null; }),
  swapped: attempt(ahead, () => { active.video = new Video(); }, () => { active.video = el; videos.pop(); }),
  seeking: attempt(ahead, () => { el.seeking = true; }, () => { el.seeking = false; }),
  behind: attempt(f => f - 1.5),
  pastEnd: attempt(() => entry.lastSourceFrame + 1.5),
 };
};
// Tail probe: mid-clip, the per-tick sync must let an element run whose
// stopped marker no clear site removed once it is back before its last frame,
// and must keep an element stopped whose media ended short of the out point.
const probeTail = () => {
 const S = surface._state; const active = S.activePlaybackVideos.get('a'); const el = active?.video;
 if (!el) return {error: 'no active element'};
 surface._sync(active, active.layer, frame);
 const marker = el._sonderTailStop; if (!marker) return {error: 'no marker'};
 marker.stopped = true; el.pause();
 surface._sync(active, active.layer, frame);
 const staleRuns = !el.paused && !marker.stopped;
 el.ended = true;
 surface._sync(active, active.layer, frame);
 const endedStops = el.paused && marker.stopped;
 el.ended = false;
 return {staleRuns, endedStops};
};
let guardProbe = null; let tailProbe = null; let restarted = false;
for (let guard = 0; frame < cfg.stopFrame && guard < (cfg.maxVsyncs || 20000); guard++) {
 await vsync();
 if (cfg.guardProbeFrame && frame >= cfg.guardProbeFrame) { guardProbe = probeGuards(); break; }
 if (cfg.tailProbeFrame && frame >= cfg.tailProbeFrame) { tailProbe = probeTail(); break; }
 // Stop and play again from earlier in the clip, as a user would.
 if (cfg.restart && !restarted && frame >= cfg.restart.at) { restarted = true; surface.stopPlayback(); await settle(); frame = cfg.restart.from; surface.startPlayback(); await settle(); }
}
surface.stopPlayback(); await settle();
// Frames shown per pass; a pass ends where the timeline steps backwards.
const passes = [[]];
for (let i = 0; i < screen.length; i++) {
 if (i && screen[i].tl < screen[i - 1].tl) passes.push([]);
 if (screen[i].shown !== null) passes[passes.length - 1].push(screen[i].shown);
}
const passFrames = passes.map(p => [...new Set(p)].sort((x, y) => x - y));

const seq = [];
for (const s of screen) { const last = seq[seq.length - 1]; if (last && last.shown === s.shown && last.clip === s.clip && !!last.black === !!s.black) last.n++; else seq.push({shown: s.shown, clip: s.clip, black: !!s.black, n: 1, tl: s.tl}); }
const shownVsyncs = (f, clip) => seq.filter(s => s.shown === f && (!clip || s.clip === clip)).reduce((n, s) => n + s.n, 0);
const clipStats = cfg.clips.map(c => {
 const lo = Math.max(cfg.startFrame, c.timeline_start_frame) + 3, hi = Math.min(cfg.stopFrame, c.timeline_end_frame) - 3;
 let missing = 0; for (let f = lo; f < hi; f++) if (!shownVsyncs(f, c.clip_id)) missing++;
 const lags = screen.filter(s => s.clip === c.clip_id && s.shown !== null && s.tl >= lo && s.tl < hi).map(s => s.tl - s.shown).sort((x, y) => x - y);
 return {clip: c.clip_id, frames: hi - lo, missing, medianLagFrames: lags.length ? lags[lags.length >> 1] : null};
});
const shownSteps = seq.filter(s => s.shown !== null);
const backward = []; for (let i = 1; i < shownSteps.length; i++) if (shownSteps[i].shown < shownSteps[i - 1].shown) backward.push([shownSteps[i - 1].shown, shownSteps[i].shown]);
const excluded = shownSteps.filter(s => { const c = cfg.clips.find(x => x.clip_id === s.clip); return c && (s.shown < c.timeline_start_frame || s.shown >= c.timeline_end_frame); }).map(s => s.clip + s.shown);
const cuts = cfg.clips.slice(1).map((c, i) => {
 const B = c.timeline_start_frame, out = cfg.clips[i].clip_id;
 const window = []; for (let f = B - 2; f <= B + 3; f++) window.push({f, vsyncs: shownVsyncs(f, f < B ? out : c.clip_id)});
 return {B, showsBminus1: !!shownVsyncs(B - 1, out), missing: window.filter(w => !w.vsyncs).map(w => w.f),
  maxHoldVsyncs: Math.max(...window.map(w => w.vsyncs)), black: seq.some(s => s.black && s.tl >= B - 2 && s.tl <= B + 3),
  incomingElements: videos.filter(v => v._clipsSeen.has(c.clip_id)).length,
  around: seq.filter(s => s.black || (s.shown !== null && s.shown >= B - 4 && s.shown <= B + 4)).map(s => (s.black ? 'black' : s.clip + s.shown) + 'x' + s.n).join(' ')};
});
// Canvas staleness inside clip b: refreshes where the canvas does not show
// the frame the active element would paint now.
let staleRefreshes = 0;
for (const s of screen) {
 if (s.clip !== 'b' || s.tl < 55 || s.tl >= Math.min(cfg.stopFrame, 117) || s.bDrawn === null) continue;
 if (s.shown !== s.bDrawn) staleRefreshes++;
}
// Elements whose always-on frame sampler recorded a presentation.
const sampled = videos.filter(v => v._sonderPresentationTracker?.sample).length;
console.log(JSON.stringify({vsyncs: vsyncIndex, clipStats, backward, excluded, cuts, rebufferEntries,
 composites: paintLog.filter(d => d.black).length, staleRefreshes, guardProbe, tailProbe, inactivePaints, sampled, passFrames, activeRunFrames, activeDrawnFrames,
 laneFrames: Object.fromEntries(Object.entries(laneFrames).map(([k, s]) => [k, [...s].filter(f => f !== null).sort((x, y) => x - y)]))}));
surface.destroy();
"""


def _lag_harness(**config):
    script = _SURFACE_LOADER + _LAG_HARNESS
    script = script.replace("__MODULE_URL__", json.dumps((ROOT / "web/js/viewport_surface.js").as_uri()))
    return _run_node(script.replace("__CONFIG__", json.dumps(config)))


def test_lag_harness_is_deterministic_and_rolled_clips_run_on_the_clock():
    first = _lag_harness()
    assert first == _lag_harness()
    # Measured live: after a rolled claim the element's currentTime frame equals
    # the playhead on 94% of ticks, so the painted frame's median lag is zero.
    assert first["clipStats"][1]["medianLagFrames"] == 0
    # A different seed changes the jitter draws, not the model.
    assert _lag_harness(seed=8)["clipStats"][1]["medianLagFrames"] == 0


# Judder cases: the rolled clip b runs on the clock, so its frame boundaries
# sit on the redraw tick and clock jitter flips them either side (the live
# mechanism). Three seeds at 120 Hz and one at 60 Hz.
_JUDDER_CASES = ({"seed": 1}, {"seed": 2}, {"seed": 3}, {"seed": 1, "hz": 60})


def test_playback_paints_every_frame_inside_a_clip_at_bounded_cost():
    # Includes a browser without requestVideoFrameCallback: the repaint
    # follows currentTime and needs no frame callbacks.
    results = {json.dumps(case): _lag_harness(**case) for case in (*_JUDDER_CASES, {"seed": 1, "rvfc": False})}
    assert {k: r["clipStats"][1]["missing"] for k, r in results.items()} == {k: 0 for k in results}
    # On every refresh the canvas shows the frame the element would paint now;
    # a stale reuse or a late repaint leaves it a refresh behind.
    assert {k: r["staleRefreshes"] for k, r in results.items()} == {k: 0 for k in results}
    # One paint per frame flip plus one confirming paint; the old tick-only
    # paint was one per timeline frame. 110 timeline frames per run.
    assert all(r["composites"] <= 2.1 * 110 for r in results.values()), {k: r["composites"] for k, r in results.items()}


def test_overlapping_lanes_repaint_at_most_once_per_refresh():
    # Each lane's flip (and its confirm) repaints the whole composite, so lanes
    # flipping out of phase multiply paints; one per refresh is the hard bound.
    two_lanes = [
        {"clip_id": "a", "source_path": "a.mp4", "timeline_start_frame": 0, "timeline_end_frame": 120,
         "source_in_frame": 0, "source_out_frame": 120, "track_index": 0},
        {"clip_id": "b", "source_path": "b.mp4", "timeline_start_frame": 0, "timeline_end_frame": 120,
         "source_in_frame": 0, "source_out_frame": 120, "track_index": 1, "opacity": 0.5},
    ]
    for hz in (120, 60):
        result = _lag_harness(clips=two_lanes, hz=hz)
        assert result["composites"] <= result["vsyncs"]
        assert result["composites"] <= 4.2 * 110


def test_repaint_between_ticks_paints_only_and_respects_its_guards():
    probe = _lag_harness(seed=2, guardProbeFrame=70)["guardProbe"]
    # A paint on a new frame asks for a confirming repaint on the next
    # refresh; a second paint within the same refresh must not consume it.
    assert probe.pop("sameRefreshConfirm") == {"first": True, "second": True}
    # One frame ahead, it paints once and touches no video (no seek, play,
    # pause, or the muted write every sync makes).
    assert probe["advanced"] == {"did": True, "painted": 1, "control": 0}
    blocked = {"did": False, "painted": 0, "control": 0}
    assert {k: v for k, v in probe.items() if k != "advanced"} == {
        k: blocked for k in ("rebuffering", "held", "swapped", "seeking", "behind", "pastEnd")}


def test_presentation_repaint_decision():
    module_url = (ROOT / "web/js/viewport_surface.js").as_uri()
    result = _run_node(f"""
const {{ _presentationRepaintDecision: decide }} = await import({json.dumps(module_url)});
const base = {{paintedFrame: 10, currentFrame: 10, lastSourceFrame: 20, confirmPending: false}};
console.log(JSON.stringify([
 decide(base), decide({{...base, currentFrame: 11}}), decide({{...base, confirmPending: true}}),
 decide({{...base, currentFrame: 9}}), decide({{...base, currentFrame: 9, confirmPending: true}}),
 decide({{...base, currentFrame: 21}}), decide({{...base, currentFrame: 20}}),
 decide({{...base, lastSourceFrame: null, currentFrame: 99}}), decide({{...base, paintedFrame: NaN}}),
]));
""")
    assert result == ["unchanged", "repaint", "repaint", "behind", "behind", "past-end", "repaint", "repaint", "unknown"]


# Cut cases. Ordinary cuts must be seamless with adaptive rebuffer on (the
# product default) and off: B-2..B+3 each painted, no hold longer than one
# frame plus one refresh, no rebuffer, no cold element for the incoming clip.
# flipLag is the 1920x1088 behaviour (the refresh on which currentTime enters
# a frame paints the previous one), where a roll claimed on that refresh would
# paint its lead-in frame. A decoder start-up of 200 ms (the live 840, 841, 840
# case at cut 843) may hold B-1 with the clock frozen, but must still paint
# B-2..B+3 with rebuffer on, from the roll itself (no cold element, so no
# timeout); with rebuffer off only "no backward step, nothing outside the
# edit" holds. No case may paint an element that is not active playback media.
_ORDINARY_CUTS = ({}, {"adaptiveRebuffer": True}, {"clockStartupMs": 0}, {"seed": 2}, {"hz": 60},
                  {"flipLag": True}, {"flipLag": True, "adaptiveRebuffer": True}, {"flipLag": True, "hz": 144})
_SLOW_START = {"startupMs": 200}

# Per-element display phases (measured live 0.28-0.60, -0.2 on heavy content):
# the fake paints floor(ct * fps + phase) and its callbacks report the
# presentation time. Clock jitter 1 ms, the live estimate's spread (<= 0.04
# frame within a run); the default 4 ms per-refresh noise is coarser than live.
_PHASE_CUTS = tuple({"clockJitterMs": 1, "adaptiveRebuffer": True, **case} for case in (
    {"phase": 0.5}, {"phase": {"a": 0.3, "b": 0.6}}, {"phase": {"a": 0.6, "b": 0.2}}, {"phase": 0.5, "hz": 60},
    {"phase": 0.45, "seed": 3}, {"phase": {"a": 0.2, "b": 0.55}, "seed": 2}, {"phase": -0.2}, {"phase": 0.3, "hz": 144},
    # From the Phase 2b audit: an outgoing tail crossing its last frame between
    # 60 Hz ticks, and a low outgoing phase against a high incoming one.
    {"phase": 0.45, "seed": 2, "hz": 60}, {"phase": {"a": 0.1, "b": 0.55}, "seed": 4},
    # The cut grace confirms the outgoing tail only after it was painted once.
    {"phase": {"a": 0.1, "b": 0.45}, "seed": 1}))


def test_a_cut_is_seamless_and_never_shows_a_frame_outside_the_edit():
    failures = []
    hold_limit = lambda hz: -(-hz // 24) + 1  # one frame period, rounded up, plus one refresh
    for case in _ORDINARY_CUTS:
        result = _lag_harness(**case)
        cut = result["cuts"][0]
        if (cut["missing"] or cut["maxHoldVsyncs"] > hold_limit(case.get("hz", 120)) or cut["black"]
                or result["excluded"] or result["backward"] or result["rebufferEntries"] or cut["incomingElements"] != 1
                or result["inactivePaints"] or result["activeRunFrames"]["a"] > 47):
            failures.append(("ordinary", case, cut, result["excluded"], result["backward"], result["rebufferEntries"]))
    for case in ({}, {"flipLag": True}):
        held = _lag_harness(**_SLOW_START, **case, adaptiveRebuffer=True)
        cut = held["cuts"][0]
        if (cut["missing"] or held["excluded"] or held["backward"] or cut["black"] or held["inactivePaints"]
                or cut["incomingElements"] != 1 or cut["maxHoldVsyncs"] * 1000 / 120 > 1000):
            failures.append(("slow, rebuffer on", case, cut, held["excluded"], held["backward"]))
    # With the clock running, the roll keeps serving the frame the playhead has
    # reached: no second (cold) element, and B-1 holds no longer than the
    # decoder start-up plus a frame.
    for hz in (120, 60):
        running = _lag_harness(**_SLOW_START, adaptiveRebuffer=False, hz=hz)
        cut = running["cuts"][0]
        if (running["excluded"] or running["backward"] or running["inactivePaints"] or cut["incomingElements"] != 1
                or cut["maxHoldVsyncs"] * 1000 / hz > _SLOW_START["startupMs"] + 1000 / 24):
            failures.append(("slow, rebuffer off", hz, cut, running["excluded"], running["backward"]))
    assert failures == []


def test_a_cut_follows_each_elements_display_phase():
    # A roll a hair short of B by currentTime but painting B by its phase is
    # claimed on the tick (no hold), and the outgoing tail stops on the frame
    # it paints, never one past its out point. Judged on currentTime alone,
    # these cases skip B or paint the outgoing frame past the out point.
    failures = []
    for case in _PHASE_CUTS:
        result = _lag_harness(**case)
        cut = result["cuts"][0]
        if (cut["missing"] or cut["black"] or result["excluded"] or result["backward"] or result["rebufferEntries"]
                or cut["incomingElements"] != 1 or result["inactivePaints"]):
            failures.append((case, cut, result["excluded"], result["backward"], result["rebufferEntries"]))
    assert failures == []


def test_a_tail_never_paints_past_its_out_point_between_ticks():
    # At 50-75 Hz ticks come two or three refreshes apart and the phase
    # prediction can trail the picture by a refresh, so an element leading the
    # clock crosses its last frame between ticks: the per-refresh watcher stops
    # it in time. Where the outgoing tail trails the tick by a frame and the
    # incoming roll is on it, one of B-1 and B can be lost; B-2..B+3 otherwise
    # all paint.
    cases = [{"phase": phase, "seed": seed, "hz": 60} for phase in (0.3, 0.45, 0.6) for seed in range(1, 9)]
    cases += [{"phase": 0.5, "seed": seed, "hz": hz, "clockStartupMs": -20} for seed in (1, 2, 3) for hz in (50, 75)]
    failures = []
    for case in cases:
        result = _lag_harness(clockJitterMs=1, adaptiveRebuffer=True, **case)
        cut = result["cuts"][0]
        # The element itself, not only the canvas, stops on its last frame:
        # a repaint or commit while it painted past it would show that frame.
        if (result["excluded"] or result["backward"] or not set(cut["missing"]) <= {47, 48} or len(cut["missing"]) > 1
                or result["activeDrawnFrames"]["a"] > 47):
            failures.append((case, cut["around"], result["excluded"], result["activeDrawnFrames"]["a"]))
    assert failures == []


def test_repaint_follows_each_elements_display_phase_inside_a_clip():
    # Every interior frame painted, the repaint following the phase-shifted
    # frame. The canvas can trail a flip by one refresh (the prediction is
    # held below the estimate so it is never ahead), plus one more on the
    # fake's decode-late refreshes (5%); it never skips.
    for case in ({"phase": 0.5}, {"phase": 0.3, "seed": 2, "hz": 60}, {"phase": {"a": 0.2, "b": 0.6}, "seed": 3}, {"phase": -0.2}):
        result = _lag_harness(clockJitterMs=1, **case)
        clip = result["clipStats"][1]
        refreshes_in_clip = clip["frames"] * case.get("hz", 120) / 24
        assert clip["missing"] == 0, case
        assert result["staleRefreshes"] <= clip["frames"] + 0.05 * refreshes_in_clip, case


def test_rolls_that_land_off_the_tick_stay_inside_the_edit():
    # A clock that starts early lands the roll well into its claim window: it
    # is claimed there and kept (seeking it back would restart its decoder
    # and step backward), and the outgoing element, running ahead too, stops
    # on its last frame instead of running past its out point.
    for seed in (7, 1, 2):
        ahead = _lag_harness(clockStartupMs=-25, seed=seed)
        cut = ahead["cuts"][0]
        assert (ahead["excluded"], ahead["backward"], cut["incomingElements"], ahead["activeRunFrames"]["a"]) == ([], [], 1, 47), seed
        assert set(cut["missing"]) <= {48}, seed
        # Kept: the first incoming frame holds under one frame period (5
        # refreshes at 120 Hz); a seek back to the frame centre holds it ~7.
        first_incoming = next(token for token in cut["around"].split() if token.startswith("b"))
        assert int(first_incoming.split("x")[1]) < 5, (seed, cut["around"])
    # A clock that starts late lands the roll just short of its target: the
    # cut still paints B-2..B+3 from the roll, at 120 Hz without a rebuffer;
    # at 60 Hz the clock may freeze for it, which keeps B painted.
    for case in ({}, {"hz": 60}):
        behind = _lag_harness(clockStartupMs=60, adaptiveRebuffer=True, **case)
        cut = behind["cuts"][0]
        assert (cut["missing"], behind["excluded"], behind["backward"], cut["incomingElements"]) == ([], [], [], 1), case
        if not case:
            assert behind["rebufferEntries"] == 0


def test_an_upper_lane_ending_over_a_lower_lane_paints_its_last_frames():
    # The tail stops on the element's own last frame, so a half-transparent
    # upper lane ending mid-scene paints through its last frame, and the lower
    # lane keeps every frame. The old tick-time pause lost the last one or two.
    lanes = [
        {"clip_id": "lo", "source_path": "lo.mp4", "timeline_start_frame": 0, "timeline_end_frame": 120,
         "source_in_frame": 0, "source_out_frame": 120, "track_index": 0},
        {"clip_id": "up", "source_path": "up.mp4", "timeline_start_frame": 0, "timeline_end_frame": 48,
         "source_in_frame": 0, "source_out_frame": 48, "track_index": 1, "opacity": 0.5},
    ]
    for hz in (120, 60):
        result = _lag_harness(clips=lanes, hz=hz)
        assert set(range(40, 48)) <= set(result["laneFrames"]["up"]), hz
        assert set(range(3, 107)) <= set(result["laneFrames"]["lo"]), hz
        assert result["activeRunFrames"]["up"] == 47 and not result["excluded"], hz


def test_cuts_without_frame_callbacks_complete_on_the_start_up_allowance():
    # Without requestVideoFrameCallback the decoder-running gate trusts a
    # bounded start-up allowance: an ordinary cut stays seamless, and a slow
    # start still completes from the roll (no stall, no cold element). A
    # start-up longer than the allowance can then show a lead-in frame; that
    # is the fallback's known limit, counted by claimStartupFallbacks.
    for case in ({}, {"adaptiveRebuffer": True}):
        result = _lag_harness(rvfc=False, **case)
        cut = result["cuts"][0]
        assert (cut["missing"], result["excluded"], result["backward"], result["rebufferEntries"], cut["incomingElements"]) == ([], [], [], 0, 1), case
    slow = _lag_harness(rvfc=False, **_SLOW_START, adaptiveRebuffer=True)
    assert slow["cuts"][0]["incomingElements"] == 1 and not slow["cuts"][0]["black"]
    assert slow["cuts"][0]["maxHoldVsyncs"] < 12


def test_the_frame_sampler_runs_without_session_diagnostics():
    # The claim gate reads the sampler, so it cannot depend on debug flags.
    assert _lag_harness(debugSession=False)["sampled"] > 0


def test_loop_restart_and_stop_restart_clear_the_tail_stop():
    # A loop ending on the cut stops the tail on its last frame every pass;
    # the stop must not survive the wrap and freeze the next pass.
    looped = _lag_harness(loopRange={"start": 30, "end": 48}, maxVsyncs=700)
    full = [list(range(30, 48))]
    assert looped["passFrames"][:-1] == full * (len(looped["passFrames"]) - 1)
    assert len(looped["passFrames"]) >= 4 and not looped["excluded"]
    # Stopped after the tail stop fired, then played again from earlier in the
    # clip: the element must run again and the cut stay seamless.
    # With a display phase, the restart's seek must also clear the phase: the
    # sought element paints its currentTime frame until its next callback.
    for case in ({}, {"phase": 0.5, "clockJitterMs": 1}):
        restarted = _lag_harness(restart={"at": 47, "from": 38}, **case)
        assert restarted["passFrames"][1][:12] == list(range(38, 50)), case
        cut = restarted["cuts"][0]
        assert (cut["missing"], restarted["excluded"], restarted["inactivePaints"]) == ([], [], 0), case
