"""Frontend reconcile-on-409 coverage for the asset refresh.

Mirrors tests/test_stale_replay_governor_js.py: the api_client.js ES module is
imported in a Node subprocess with globalThis.fetch stubbed. Auto-skips when
node is unavailable.
"""

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


def test_post_with_reconcile_heals_and_retries_with_corrected_if_match():
    module_url = (ROOT / "web" / "js" / "api_client.js").as_uri()
    project_id = "proj-1"
    stale = "2026-07-21T00:09:40"
    actual = "2026-07-21T00:32:33"
    script = f"""
const mod = await import({json.dumps(module_url)});
const projectId = {json.dumps(project_id)};
const stale = {json.dumps(stale)};
const actual = {json.dumps(actual)};
mod.rememberProjectVersion(projectId, stale);

const sentIfMatch = [];
globalThis.fetch = async (input, init = {{}}) => {{
    const headers = init && init.headers ? new Headers(init.headers) : new Headers();
    const ifMatch = headers.get("If-Match") || "";
    sentIfMatch.push(ifMatch);
    const respHeaders = new Headers({{
        "Content-Type": "application/json",
        "X-Sonder-Project-Id": projectId,
        "X-Sonder-Project-Modified-At": actual,
    }});
    if (ifMatch === actual) {{
        const body = JSON.stringify({{
            project_id: projectId,
            modified_at: actual,
            assets: [{{ asset_id: "a1", asset_type: "video", path: "media/take.mp4" }}],
            folders: [],
        }});
        return {{ ok: true, status: 200, headers: respHeaders, text: async () => body }};
    }}
    const body = JSON.stringify({{
        error: "project_version_conflict",
        code: "project_version_conflict",
        expected_modified_at: ifMatch,
        actual_modified_at: actual,
        project: {{ project_id: projectId, modified_at: actual }},
    }});
    return {{ ok: false, status: 409, headers: respHeaders, text: async () => body }};
}};

const url = "http://x/sonder-editor/project/" + projectId + "/assets/sync";
const result = await mod.postProjectJsonWithReconcile(url, {{ method: "POST" }}, {{ projectId }});
console.log(JSON.stringify({{
    attempts: result.attempts,
    assetCount: result.payload.assets.length,
    sentIfMatch,
    finalVersion: mod.getProjectVersion(projectId),
}}));
"""

    result = json.loads(_run_node(script))

    assert result["attempts"] == 2
    assert result["assetCount"] == 1
    # The retry MUST carry the healed If-Match. A broken heal would re-send the
    # stale value, the stub would 409 again, and (maxAttempts=2) the call would
    # throw instead of returning — so this list pins the exact regression.
    assert result["sentIfMatch"] == [stale, actual]
    assert result["finalVersion"] == actual


def test_post_with_reconcile_throws_conflict_after_exhaustion():
    # A server that keeps 409'ing must not loop forever: after maxAttempts the
    # call throws with code=project_version_conflict, which _fetchAssets uses to
    # hand off to the bounded governor/breaker.
    module_url = (ROOT / "web" / "js" / "api_client.js").as_uri()
    project_id = "proj-2"
    actual = "2026-07-21T02:00:00"
    script = f"""
const mod = await import({json.dumps(module_url)});
const projectId = {json.dumps(project_id)};
const actual = {json.dumps(actual)};
mod.rememberProjectVersion(projectId, "2026-07-21T01:00:00");
let calls = 0;
globalThis.fetch = async () => {{
    calls += 1;
    const respHeaders = new Headers({{
        "X-Sonder-Project-Id": projectId,
        "X-Sonder-Project-Modified-At": actual,
    }});
    const body = JSON.stringify({{
        error: "project_version_conflict",
        code: "project_version_conflict",
        expected_modified_at: "",
        actual_modified_at: actual,
        project: {{ project_id: projectId, modified_at: actual }},
    }});
    return {{ ok: false, status: 409, headers: respHeaders, text: async () => body }};
}};
let code = "";
try {{
    await mod.postProjectJsonWithReconcile(
        "http://x/sonder-editor/project/" + projectId + "/assets/sync",
        {{ method: "POST" }},
        {{ projectId }},
    );
}} catch (e) {{
    code = e.code || "";
}}
console.log(JSON.stringify({{ code, calls }}));
"""

    result = json.loads(_run_node(script))

    assert result["code"] == "project_version_conflict"
    assert result["calls"] == 2  # bounded by the default maxAttempts
