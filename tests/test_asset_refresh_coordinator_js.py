import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_URL = (ROOT / "web" / "js" / "asset_refresh_coordinator.js").as_uri()


def _run_node(script):
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


def test_same_wave_joins_and_satisfied_new_wave_collapses():
    script = f"""
const {{ createAssetRefreshCoordinator }} = await import({json.dumps(MODULE_URL)});
const pending = [];
let calls = 0;
const response = (version, assetId) => ({{
    payload: {{ assets: [{{ asset_id: assetId }}], folders: [] }},
    response: {{
        status: 200,
        headers: {{ get(name) {{
            return name === "X-Sonder-Project-Modified-At" ? version : "";
        }} }},
    }},
}});
const coordinator = createAssetRefreshCoordinator({{
    getLiveVersion: () => "v1",
    request: (demand) => {{
        calls += 1;
        return new Promise((resolve) => pending.push({{ demand, resolve }}));
    }},
}});
const first = coordinator.request({{ projectId: "p", waveId: "wave-1", reason: "controller" }});
const joined = coordinator.request({{ projectId: "p", waveId: "wave-1", reason: "fullscreen" }});
const newer = coordinator.request({{
    projectId: "p",
    waveId: "wave-2",
    requiredVersion: "v2",
    reason: "project_updated",
}});
await new Promise((resolve) => setTimeout(resolve, 0));
const callsBeforeResolve = calls;
pending[0].resolve(response("v2", "a2"));
const results = await Promise.all([first, joined, newer]);
await new Promise((resolve) => setTimeout(resolve, 0));
console.log(JSON.stringify({{
    callsBeforeResolve,
    calls,
    ids: results.map((result) => result.payload.assets[0].asset_id),
    requestIds: results.map((result) => result.requestId),
}}));
"""
    result = _run_node(script)
    assert result["callsBeforeResolve"] == 1
    assert result["calls"] == 1
    assert result["ids"] == ["a2", "a2", "a2"]
    assert len(set(result["requestIds"])) == 1


def test_newer_version_and_sync_policy_schedule_single_followup():
    script = f"""
const {{ createAssetRefreshCoordinator }} = await import({json.dumps(MODULE_URL)});
const pending = [];
const calls = [];
const response = (version, id) => ({{
    payload: {{ assets: [{{ asset_id: id }}], folders: [] }},
    response: {{
        status: 200,
        headers: {{ get: (name) => name === "X-Sonder-Project-Modified-At" ? version : "" }},
    }},
}});
const coordinator = createAssetRefreshCoordinator({{
    getLiveVersion: () => "v1",
    request: (demand) => {{
        calls.push({{ mode: demand.mode, policy: demand.policySignature }});
        return new Promise((resolve) => pending.push(resolve));
    }},
}});
const first = coordinator.request({{ projectId: "p", waveId: "wave-1", mode: "read" }});
const newer = coordinator.request({{
    projectId: "p",
    waveId: "wave-2",
    mode: "read",
    requiredVersion: "v3",
}});
const syncA = coordinator.request({{
    projectId: "p",
    waveId: "wave-2",
    mode: "sync",
    policy: {{ retentionDays: 5, maxSizeMB: 100 }},
}});
const syncB = coordinator.request({{
    projectId: "p",
    waveId: "wave-2",
    mode: "sync",
    policy: {{ retentionDays: 9, maxSizeMB: 200 }},
}});
pending[0](response("v2", "old"));
await new Promise((resolve) => setTimeout(resolve, 0));
const callsAfterFirst = calls.length;
pending[1](response("v3", "new"));
const results = await Promise.all([first, newer, syncA, syncB]);
console.log(JSON.stringify({{
    callsAfterFirst,
    calls,
    ids: results.map((result) => result.payload.assets[0].asset_id),
}}));
"""
    result = _run_node(script)
    assert result["callsAfterFirst"] == 2
    assert len(result["calls"]) == 2
    assert result["calls"][0]["mode"] == "read"
    assert result["calls"][1]["mode"] == "sync"
    assert "retention_days=9" in result["calls"][1]["policy"]
    assert "max_size_mb=200" in result["calls"][1]["policy"]
    assert result["ids"] == ["old", "new", "new", "new"]


def test_mutation_epoch_supersedes_old_response_and_preserves_project_isolation():
    script = f"""
const {{ createAssetRefreshCoordinator }} = await import({json.dumps(MODULE_URL)});
const pending = [];
const calls = [];
const response = (version, id) => ({{
    payload: {{ assets: [{{ asset_id: id }}], folders: [] }},
    response: {{
        status: 200,
        headers: {{ get: (name) => name === "X-Sonder-Project-Modified-At" ? version : "" }},
    }},
}});
const coordinator = createAssetRefreshCoordinator({{
    getLiveVersion: () => "v1",
    request: (demand) => {{
        calls.push({{ project: demand.projectId, epoch: demand.epoch }});
        return new Promise((resolve) => pending.push({{ project: demand.projectId, resolve }}));
    }},
}});
const old = coordinator.request({{ projectId: "p1", waveId: "old" }});
const other = coordinator.request({{ projectId: "p2", waveId: "other" }});
coordinator.markMutation("p1", "delete");
pending.find((entry) => entry.project === "p1").resolve(response("v1", "old"));
pending.find((entry) => entry.project === "p2").resolve(response("v1", "other"));
await new Promise((resolve) => setTimeout(resolve, 0));
const p1Followup = pending.filter((entry) => entry.project === "p1")[1];
p1Followup.resolve(response("v2", "new"));
const results = await Promise.all([old, other]);
console.log(JSON.stringify({{
    calls,
    oldResult: results[0].payload.assets[0].asset_id,
    otherResult: results[1].payload.assets[0].asset_id,
    p1Epoch: coordinator.getMutationEpoch("p1"),
    p2Epoch: coordinator.getMutationEpoch("p2"),
}}));
"""
    result = _run_node(script)
    assert result["oldResult"] == "new"
    assert result["otherResult"] == "other"
    assert result["p1Epoch"] == 1
    assert result["p2Epoch"] == 0
    assert [call["project"] for call in result["calls"]] == ["p1", "p2", "p1"]


def test_lower_version_retries_then_resets_alias_and_unknown_new_wave_runs():
    script = f"""
const {{ createAssetRefreshCoordinator }} = await import({json.dumps(MODULE_URL)});
let calls = 0;
const resets = [];
const waits = [];
const response = {{
    payload: {{ assets: [], folders: [] }},
    response: {{
        status: 200,
        headers: {{ get: (name) => name === "X-Sonder-Project-Modified-At" ? "v1" : "" }},
    }},
}};
const coordinator = createAssetRefreshCoordinator({{
    getLiveVersion: () => "v9",
    resetVersion: (projectId, version) => resets.push([projectId, version]),
    waitForRetry: async (delayMs) => {{ waits.push(delayMs); return true; }},
    retryDelaysMs: [],
    request: async () => {{ calls += 1; return response; }},
}});
const first = await coordinator.request({{ projectId: "p", waveId: "known", requiredVersion: "v9" }});
const second = await coordinator.request({{ projectId: "p", waveId: "unknown-new" }});
console.log(JSON.stringify({{
    calls,
    waits,
    resets,
    firstVersion: first.servedVersion,
    secondVersion: second.servedVersion,
}}));
"""
    result = _run_node(script)
    assert result["calls"] == 8
    assert result["waits"] == [250, 1000, 4000, 250, 1000, 4000]
    assert result["resets"] == [["p", "v1"], ["p", "v1"]]
    assert result["firstVersion"] == "v1"
    assert result["secondVersion"] == "v1"


def test_exhausted_wave_does_not_suppress_distinct_unknown_wave():
    script = f"""
const {{ createAssetRefreshCoordinator }} = await import({json.dumps(MODULE_URL)});
let calls = 0;
const coordinator = createAssetRefreshCoordinator({{
    getLiveVersion: () => "",
    retryDelaysMs: [],
    request: async (demand) => {{
        calls += 1;
        if (demand.waveId === "failed") throw new Error("offline");
        return {{
            payload: {{ assets: [{{ asset_id: "ok" }}], folders: [] }},
            response: {{ status: 200, headers: {{ get: () => "" }} }},
        }};
    }},
}});
let failed = false;
try {{
    await coordinator.request({{ projectId: "p", waveId: "failed" }});
}} catch (_) {{
    failed = true;
}}
const next = await coordinator.request({{ projectId: "p", waveId: "next-unknown" }});
console.log(JSON.stringify({{
    failed,
    calls,
    next: next.payload.assets[0].asset_id,
}}));
"""
    assert _run_node(script) == {"failed": True, "calls": 2, "next": "ok"}


def test_manual_sync_cancels_automatic_backoff_without_aborting_network_work():
    script = f"""
const {{ createAssetRefreshCoordinator }} = await import({json.dumps(MODULE_URL)});
const calls = [];
const coordinator = createAssetRefreshCoordinator({{
    getLiveVersion: () => "",
    request: async (demand) => {{
        calls.push({{ mode: demand.mode, wave: demand.waveId }});
        if (demand.mode === "read") throw new Error("temporary");
        return {{
            payload: {{ assets: [{{ asset_id: "manual" }}], folders: [] }},
            response: {{ status: 200, headers: {{ get: () => "" }} }},
        }};
    }},
}});
const automatic = coordinator.request({{ projectId: "p", waveId: "auto", mode: "read" }});
await new Promise((resolve) => setTimeout(resolve, 0));
const manual = coordinator.request({{
    projectId: "p",
    waveId: "manual",
    mode: "sync",
    manual: true,
    policy: {{ retentionDays: 7, maxSizeMB: 250 }},
}});
const results = await Promise.all([automatic, manual]);
console.log(JSON.stringify({{
    calls,
    ids: results.map((result) => result.payload.assets[0].asset_id),
}}));
"""
    result = _run_node(script)
    assert result["calls"] == [
        {"mode": "read", "wave": "auto"},
        {"mode": "sync", "wave": "manual"},
    ]
    assert result["ids"] == ["manual", "manual"]
