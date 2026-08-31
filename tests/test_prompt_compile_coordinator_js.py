import json
import os
import shutil
import subprocess

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt compile coordinator coverage")
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


def test_preview_lanes_keep_one_active_and_only_the_latest_trailing_body():
    url = os.path.join(ROOT, "web", "js", "prompt_compile_coordinator.js")
    result = _run_node(f"""
import {{createPromptCompileCoordinator}} from {json.dumps('file:///' + url.replace(os.sep, '/'))};
const calls=[]; const pending=[];
const coordinator=createPromptCompileCoordinator((request)=>{{
  calls.push({{purpose:request.purpose,body:request.body.value,current:request.isCurrent()}});
  return new Promise(resolve=>pending.push({{purpose:request.purpose,
    body:request.body.value,resolve,current:request.isCurrent}}));
}});
const schedule=(purpose,value)=>coordinator.schedule({{purpose,projectId:"p",sceneId:"s",
  body:{{value}},isCurrent:()=>true}});
const a1=schedule("windowed-preview","A1");
const a2=schedule("windowed-preview","A2");
const a3=schedule("windowed-preview","A3");
const s1=schedule("scene-projection","S1");
await Promise.resolve(); await Promise.resolve();
const before={{calls:[...calls],state:coordinator.debugState()}};
pending.find(x=>x.body==="A1").resolve("old");
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
pending.find(x=>x.body==="S1").resolve("scene");
await Promise.resolve(); await Promise.resolve();
pending.find(x=>x.body==="A3").resolve("latest");
const values=await Promise.all([a1,a2,a3,s1]);
await Promise.resolve();
console.log(JSON.stringify({{before,calls,values,state:coordinator.debugState()}}));
""")
    assert result["before"]["calls"] == [
        {"purpose": "windowed-preview", "body": "A1", "current": False},
        {"purpose": "scene-projection", "body": "S1", "current": True},
    ]
    assert [row["body"] for row in result["calls"]] == ["A1", "S1", "A3"]
    assert result["values"] == [None, None, "latest", "scene"]
    assert result["state"] == {"disposed": False, "lanes": []}


def test_dispose_revokes_active_application_and_drops_undispatched_trailing():
    url = os.path.join(ROOT, "web", "js", "prompt_compile_coordinator.js")
    result = _run_node(f"""
import {{createPromptCompileCoordinator}} from {json.dumps('file:///' + url.replace(os.sep, '/'))};
let release; let dispatches=0;
const coordinator=createPromptCompileCoordinator(()=>{{dispatches++;
  return new Promise(resolve=>{{release=resolve;}});}});
const first=coordinator.schedule({{purpose:"windowed-preview",projectId:"p",sceneId:"s",body:{{n:1}}}});
const trailing=coordinator.schedule({{purpose:"windowed-preview",projectId:"p",sceneId:"s",body:{{n:2}}}});
await Promise.resolve(); coordinator.dispose(); release("obsolete");
const values=await Promise.all([first,trailing]);
await Promise.resolve(); await Promise.resolve();
console.log(JSON.stringify({{dispatches,values,state:coordinator.debugState()}}));
""")
    assert result["dispatches"] == 1
    assert result["values"] == [None, None]
    assert result["state"]["disposed"] is True
    assert result["state"]["lanes"] == []
