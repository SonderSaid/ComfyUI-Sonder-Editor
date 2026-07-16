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
