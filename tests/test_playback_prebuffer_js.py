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
    assert captured["stop"]["staleDraws"] == 8
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
    script = r"""
const {readFileSync} = await import('node:fs');
const moduleUrl=__MODULE_URL__;
let source=readFileSync(new URL(moduleUrl),'utf8');
source=source.replaceAll(/from "(\.\/[^"]+)"/g,(_,p)=>'from '+JSON.stringify(new URL(p,moduleUrl).href));
// Observe the actual closure; do not replace its state transitions.
source=source.replace('        renderFrame,\n        togglePlayback,','        _state: state, _sourceCache: sourceCache, _drain: drainPendingReleases, _abortPreRolls: abortPreRolls,\n        renderFrame,\n        togglePlayback,');
const {createViewportSurface}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
let fetches=0;globalThis.fetch=async()=>{fetches++;return {ok:true,blob:async()=>new Blob([new Uint8Array(100)])};};
const events = [], raf = [], videos = [], draws = [], frames=[];
globalThis.window = {SONDER_DEBUG_SESSION:true, __SONDER_CANVAS_DIAG:{record:(kind,payload)=>events.push({kind,...payload})},__SONDER_DIAG_CLEARERS:new Set(),setTimeout,clearTimeout};
class Video extends EventTarget {
 constructor(){super();this.readyState=0;this.videoWidth=320;this.videoHeight=180;this.duration=100;this.paused=true;this.seeking=false;this._time=0;this.callbacks=new Map();this.next=0; videos.push(this);}
 get src(){return this._src;}
 set src(v){this._src=v;this.readyState=4;}
 get currentTime(){return this._time;}
 set currentTime(v){this._time=v;queueMicrotask(()=>this.dispatchEvent(new Event('seeked')));}
 play(){this.paused=false;return Promise.resolve();}
 pause(){this.paused=true;}
 load(){}
 removeAttribute(k){if(k==="src"){delete this._src;this.readyState=0;}else delete this[k];}
 requestVideoFrameCallback(cb){this.callbacks.set(++this.next,cb);return this.next;}
 cancelVideoFrameCallback(id){this.callbacks.delete(id);}
 present(time){for(const [id,cb] of [...this.callbacks]){this.callbacks.delete(id);cb(performance.now(),{mediaTime:time,presentedFrames:1});}}
}
globalThis.document={visibilityState:'visible',hasFocus:()=>true,querySelectorAll:()=>[],createElement:()=>new Video()};
globalThis.requestAnimationFrame=cb=>{raf.push(cb);return raf.length;};globalThis.cancelAnimationFrame=()=>{};
const ctx=new Proxy({globalAlpha:1,drawImage:(el)=>draws.push(el)}, {get:(t,k)=>k in t?t[k]:()=>{},set:(t,k,v)=>{t[k]=v;return true;}});
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
if (__CLEAR__ && i===12){beforeClear=events.filter(e=>e.kind==="playback_presentation_mismatch").length;for(const clear of window.__SONDER_DIAG_CLEARERS)clear();}for(const v of videos)if(!v.paused)v._time+=1/24;for(const cb of raf.splice(0))cb(base+i*1000/24+0.1);await settle();}
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
console.log(JSON.stringify({audioReleased,audioVisibleRetained:!!audioVisible?.src,abortRestored,releasedPassed,idleBeforeStop,stopPreserved,adoptedRetained,beforeClear, sameRun:events.filter(e=>e.kind==="playback_run_start").every(e=>e.playbackRunId===firstStart.playbackRunId), frames, videos:videos.length,draws:draws.length,stop:events.find(e=>e.kind==='playback_run_stop')?.presentation,videoStates:videos.map(v=>({src:v.src,time:v.currentTime,paused:v.paused})),claims:events.filter(e=>e.kind==='playback_prebuffer_claim').length, kinds:[...new Set(events.map(e=>e.kind))]}));
surface.destroy();



"""
    script = script.replace("__MODULE_URL__", json.dumps((ROOT / "web/js/viewport_surface.js").as_uri()))
    return _run_node(script.replace("__SAMPLED__", json.dumps(sampled)).replace("__CLEAR__", json.dumps(clear)).replace("__ABORT__",json.dumps(abort)).replace("__AUDIO__",json.dumps(audio)))


def test_presentation_counters_detect_frozen_and_unsampled_boundary_draws():
    frozen = _presentation_harness()
    assert frozen["claims"] == 1
    assert frozen["stop"]["staleDraws"] > 0
    assert frozen["stop"]["boundaryStaleDraws"] == 3
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
    script = r"""
const {readFileSync} = await import('node:fs');
const moduleUrl=__MODULE_URL__;
let source=readFileSync(new URL(moduleUrl),'utf8');
source=source.replaceAll(/from "(\.\/[^"]+)"/g,(_,p)=>'from '+JSON.stringify(new URL(p,moduleUrl).href));
// Observe the actual closure; do not replace its state transitions.
source=source.replace('        renderFrame,\n        togglePlayback,','        _state: state, _sourceCache: sourceCache, _drain: drainPendingReleases,\n        renderFrame,\n        togglePlayback,');
const {createViewportSurface}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
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
const {{_planPlaybackPreRoll:plan,_rollingPrebufferAtTarget:claim}}=await import({json.dumps(module_url)});
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
 claim:[claim(2-1/24,2,1/24),claim(2,2,1/24),claim(2+2/24,2,1/24)],
}}));
""")
    assert result == {"leads": [3,4,6,2], "horizon": ["hold","park-lead-in","skip","skip"],
        "start":"start-roll", "noRoom":["no-room","no-room"], "short":"no-room",
        "budget":"budget", "single":"budget", "abort":["abort","abort","abort"],
        "hold":"hold", "claim":[False,True,False]}


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
