import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    return subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_project_version_reset_lowers_canonical_and_folder_aliases():
    module_url = (ROOT / "web" / "js" / "api_client.js").as_uri()
    script = f"""
const mod = await import({json.dumps(module_url)});
mod.rememberProjectVersionFromPayload(
    {{ project_id: "canonical-id", modified_at: "2026-07-14T20:00:00" }},
    "folder-id",
);
mod.rememberProjectVersion("folder-id", "2026-07-14T19:00:00");
const monotonic = mod.getProjectVersion("folder-id");
mod.resetProjectVersion("folder-id", "2026-07-14T18:00:00");
console.log(JSON.stringify({{
    monotonic,
    canonical: mod.getProjectVersion("canonical-id"),
    folder: mod.getProjectVersion("folder-id"),
}}));
"""

    result = json.loads(_run_node(script))

    assert result == {
        "monotonic": "2026-07-14T20:00:00",
        "canonical": "2026-07-14T18:00:00",
        "folder": "2026-07-14T18:00:00",
    }


def test_stale_replay_governor_backs_off_then_accepts_stable_server_version():
    module_url = (ROOT / "web" / "js" / "api_client.js").as_uri()
    script = f"""
const {{ createStaleReplayGovernor }} = await import({json.dumps(module_url)});
const stable = createStaleReplayGovernor();
const stableResults = [
    stable.reject("project-a", "served-v1"),
    stable.reject("project-a", "served-v1"),
    stable.reject("project-a", "served-v1"),
    stable.reject("project-a", "served-v1"),
];

const changing = createStaleReplayGovernor();
const changingResults = [
    changing.reject("project-a", "served-v1"),
    changing.reject("project-a", "served-v2"),
    changing.reject("project-a", "served-v3"),
];

const reset = createStaleReplayGovernor();
reset.reject("project-a", "served-v1");
reset.reject("project-a", "served-v1");
reset.reset("project-a");
const afterAppliedReset = reset.reject("project-a", "served-v1");
reset.reject("project-a", "served-v1");
const afterProjectSwitch = reset.reject("project-b", "served-v1");

console.log(JSON.stringify({{
    stableResults,
    changingResults,
    afterAppliedReset,
    afterProjectSwitch,
}}));
"""

    result = json.loads(_run_node(script))

    assert result["stableResults"] == [
        {"action": "retry", "delayMs": 250, "rejectionCount": 1},
        {"action": "retry", "delayMs": 1000, "rejectionCount": 2},
        {"action": "retry", "delayMs": 4000, "rejectionCount": 3},
        {"action": "accept", "rejectionCount": 4},
    ]
    assert result["changingResults"] == [
        {"action": "retry", "delayMs": 250, "rejectionCount": 1},
        {"action": "retry", "delayMs": 250, "rejectionCount": 1},
        {"action": "retry", "delayMs": 250, "rejectionCount": 1},
    ]
    assert result["afterAppliedReset"] == {
        "action": "retry",
        "delayMs": 250,
        "rejectionCount": 1,
    }
    assert result["afterProjectSwitch"] == {
        "action": "retry",
        "delayMs": 250,
        "rejectionCount": 1,
    }
