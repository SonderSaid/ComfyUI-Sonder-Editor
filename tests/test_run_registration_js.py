"""The canvas page's run registration wrapper around api.queuePrompt."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(body: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    url = (ROOT / "web" / "js" / "run_registration.js").as_uri()
    script = f"""
const mod = await import({json.dumps(url)});
const controller = (project, host, session) => ({{ _projectId: () => project, _hostId: () => host, _editorSessionId: session }});
const inner = {{ id: 7, _sonderController: controller("P", "page:7", "canvas-s") }};
const root = {{ _nodes: [
    {{ id: 1, _sonderController: controller("P", "page:1", "canvas-s") }},
    {{ id: 5, subgraph: {{ _nodes: [inner] }} }},
    {{ id: 6, subgraph: {{ _nodes: [inner] }} }},
    {{ id: 9 }},
] }};
const out = [];
{body}
console.log("RESULT=" + JSON.stringify(out));
"""
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    line = next(line for line in result.stdout.splitlines() if line.startswith("RESULT="))
    return json.loads(line[len("RESULT="):])


def test_stamps_shallow_copies_per_host_and_maps_subgraph_keys():
    (result,) = _run("""
const data = { output: {
    "1": { class_type: "SonderEditor", inputs: {}, _meta: { title: "Editor" } },
    "5:7": { class_type: "SonderEditor", inputs: {} },
    "6:7": { class_type: "SonderEditor", inputs: {} },
    "9": { class_type: "KSampler", inputs: {} },
}, workflow: { id: "wf" } };
const frozen = JSON.stringify(data);
let n = 0;
const { data: sent, registrations } = mod.stampReceipts(data, root, { partial: true, mint: () => `r${++n}` });
out.push({ untouched: JSON.stringify(data) === frozen, sent, registrations });
""")
    assert result["untouched"] is True
    sent = result["sent"]["output"]
    assert sent["1"]["_meta"] == {"title": "Editor", "sonder_receipt": "r1"}
    # One shared definition behind two instance keys: one host, both keys.
    assert sent["5:7"]["_meta"]["sonder_receipt"] == sent["6:7"]["_meta"]["sonder_receipt"] == "r2"
    assert "_meta" not in sent["9"]
    assert result["registrations"] == [
        {"receipt": "r1", "project_id": "P", "host_id": "page:1", "session_id": "canvas-s", "editors": ["1"], "purpose": "branch"},
        {"receipt": "r2", "project_id": "P", "host_id": "page:7", "session_id": "canvas-s", "editors": ["5:7", "6:7"], "purpose": "branch"},
    ]


def test_wrapper_calls_through_at_once_on_api_and_tolerates_failed_registration():
    (result,) = _run("""
const calls = [], posts = [];
const api = {
    apiURL: (p) => "/api" + p,
    queuePrompt: function (number, data, options) { calls.push({ self: this === api, number, data, options }); return Promise.resolve({ prompt_id: "x" }); },
};
let release;
const fetchImpl = (url, init) => { posts.push({ url, body: JSON.parse(init.body) }); return new Promise((r) => { release = r; }); };
const warnings = [];
mod.installRunRegistration({ api, app: { graph: root }, fetchImpl, warn: (m) => warnings.push(m) });
const again = mod.installRunRegistration({ api, app: { graph: root }, fetchImpl });
const data = { output: { "1": { class_type: "SonderEditor", inputs: {} } } };
// Another wrapper calling through with the wrong receiver.
const answer = await api.queuePrompt.call({ notApi: true }, 0, data, { partialExecutionTargets: [] });
const plain = { output: { "9": { class_type: "KSampler", inputs: {} } } };
await api.queuePrompt(0, plain, {});
release({ ok: false, status: 409 });
await new Promise((r) => setTimeout(r, 10));
out.push({ again, answer, calls: calls.map(c => ({ self: c.self, stamped: !!c.data.output["1"]?._meta?.sonder_receipt, same: c.data === plain })),
           posts: posts.map(p => ({ url: p.url, purpose: p.body.purpose, editors: p.body.editors })), original: !!data.output["1"]._meta, warnings: warnings.length });
""")
    assert result["again"] is False
    assert result["answer"] == {"prompt_id": "x"}
    assert result["calls"] == [{"self": True, "stamped": True, "same": False}, {"self": True, "stamped": False, "same": True}]
    assert result["posts"] == [{"url": "/api/sonder-editor/runs/register", "purpose": "run", "editors": ["1"]}]
    assert result["original"] is False
    assert result["warnings"] == 1


def test_outputs_client_routes_hints_to_its_own_host_and_session():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    url = (ROOT / "web" / "js" / "run_outputs_client.js").as_uri()
    script = f"""
const mod = await import({json.dumps(url)});
const urls = [];
const fetchImpl = (u, init) => {{ urls.push({{ u, headers: init?.headers || {{}} }}); return Promise.resolve({{ ok: true, status: 200, json: async () => ({{ ok: true }}) }}); }};
const canvas = mod.createRunOutputsClient({{ mode: "canvas", apiURL: p => p, fetchImpl,
    identity: () => ({{ projectId: "P", hostId: "h1", sessionId: "s1" }}) }});
let runIds = [];
const sdk = mod.createRunOutputsClient({{ mode: "sdk", apiURL: p => p, fetchImpl,
    identity: () => ({{ projectId: "P", sessionId: "sdk-1", secret: "x", tabSessionId: "t", runIds: () => runIds }}) }});
const seen = [];
canvas.onOutputsChanged(e => seen.push(["canvas", e.seq]));
const stop = sdk.onOutputsChanged(e => seen.push(["sdk", e.seq]));
const hint = (seq, extra) => ({{ type: "outputs_changed", seq, host_ids: [], run_ids: [], ...extra }});
mod.publishOutputsChanged("P", hint(1, {{ host_ids: ["h1"] }}));
mod.publishOutputsChanged("Q", hint(2, {{ host_ids: ["h1"] }}));
runIds = ["r1"];
mod.publishOutputsChanged("P", hint(3, {{ run_ids: ["r2"] }}));
mod.publishOutputsChanged("P", hint(4, {{ run_ids: ["r1"] }}));
stop();
mod.publishOutputsChanged("P", hint(5, {{ run_ids: ["r1"], host_ids: ["h2"] }}));
await canvas.readOutputs(["a", "b"], {{ workflowId: "wf" }});
await sdk.readRun("p 1");
console.log("RESULT=" + JSON.stringify({{ seen, urls }}));
"""
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    out = json.loads(next(l for l in result.stdout.splitlines() if l.startswith("RESULT="))[len("RESULT="):])
    # Hint 1 is the canvas host's (and the SDK, knowing no runs yet, takes any); 2 is
    # another project; 3 is another session's run; 5 arrives after the SDK stopped.
    assert out["seen"] == [["canvas", 1], ["sdk", 1], ["sdk", 4]]
    assert out["urls"][0]["u"] == "/sonder-editor/project/P/runs/outputs?host_id=h1&session_id=s1&sources=a%2Cb&workflow_id=wf"
    assert out["urls"][1]["u"] == "/sonder-editor/sdk/session/sdk-1/runs/p%201?"
    assert out["urls"][1]["headers"] == {"X-Sonder-SDK-Session": "x", "X-Sonder-Tab-Session": "t"}


def test_a_prompt_comfyui_refuses_registers_nothing():
    (result,) = _run("""
const posts = [];
let reply;
const api = { apiURL: (p) => p, queuePrompt: () => reply };
mod.installRunRegistration({ api, app: { graph: root }, fetchImpl: (url, init) => { posts.push(url); return Promise.resolve({ ok: true }); } });
const data = { output: { "1": { class_type: "SonderEditor", inputs: {} } } };
reply = Promise.reject(new Error("prompt_outputs_failed_validation"));
const refused = await api.queuePrompt(0, data, {}).then(() => "resolved", () => "rejected");
reply = Promise.resolve({ error: "no prompt_id" });
await api.queuePrompt(0, data, {});
await new Promise((r) => setTimeout(r, 10));
const before = posts.length;
reply = Promise.resolve({ prompt_id: "p" });
await api.queuePrompt(0, data, {});
await new Promise((r) => setTimeout(r, 10));
out.push({ refused, before, after: posts.length });
""")
    # The caller still sees ComfyUI's refusal; only an accepted prompt registers.
    assert result == {"refused": "rejected", "before": 0, "after": 1}
