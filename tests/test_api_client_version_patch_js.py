"""Direct coverage for the page-level project-version fetch patch."""

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


def test_fetch_patch_stamps_latest_version_and_preserves_explicit_if_match():
    module_url = (ROOT / "web/js/api_client.js").as_uri()
    script = f"""
const calls=[];
const nativeFetch=async (input,init={{}})=>{{
  calls.push({{url:String(input),ifMatch:new Headers(init.headers||{{}}).get("If-Match")||""}});
  return new Response(JSON.stringify({{ok:true}}),{{status:200,headers:{{
    "Content-Type":"application/json",
    "X-Sonder-Project-Id":"project",
    "X-Sonder-Project-Modified-At":"2026-08-29T12:00:00",
  }}}});
}};
globalThis.window={{fetch:nativeFetch}};
const mod=await import({json.dumps(module_url)});
mod.rememberProjectVersion("project","2026-08-29T11:00:00");
mod.installProjectVersionFetchPatch();
await window.fetch("http://x/sonder-editor/project/project/scenes",{{method:"PUT"}});
await window.fetch("http://x/sonder-editor/project/project/scenes",{{
  method:"PUT",headers:{{"If-Match":"snapshot-version"}},
}});
console.log(JSON.stringify({{calls,version:mod.getProjectVersion("project")}}));
"""

    result = json.loads(_run_node(script))

    assert result == {
        "calls": [
            {"url": "http://x/sonder-editor/project/project/scenes",
             "ifMatch": "2026-08-29T11:00:00"},
            {"url": "http://x/sonder-editor/project/project/scenes",
             "ifMatch": "snapshot-version"},
        ],
        "version": "2026-08-29T12:00:00",
    }


def test_remember_project_version_ignores_late_lower_get_response():
    module_url = (ROOT / "web/js/api_client.js").as_uri()
    script = f"""
let release;
const delayed=new Promise((resolve)=>{{release=resolve;}});
globalThis.window={{fetch:async()=>{{
  await delayed;
  return new Response(JSON.stringify({{project_id:"project",
    modified_at:"2026-08-29T10:00:00"}}),{{status:200,headers:{{
      "Content-Type":"application/json",
      "X-Sonder-Project-Id":"project",
      "X-Sonder-Project-Modified-At":"2026-08-29T10:00:00",
  }}}});
}}}};
const mod=await import({json.dumps(module_url)});
mod.installProjectVersionFetchPatch();
const request=window.fetch("http://x/sonder-editor/project/project/scenes");
mod.rememberProjectVersion("project","2026-08-29T11:00:00");
release();
await request;
await Promise.resolve();
console.log(JSON.stringify({{version:mod.getProjectVersion("project")}}));
"""

    result = json.loads(_run_node(script))

    assert result == {"version": "2026-08-29T11:00:00"}
