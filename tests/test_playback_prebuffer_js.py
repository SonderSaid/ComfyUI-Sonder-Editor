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
