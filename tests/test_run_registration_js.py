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
mod.publishRunEvent("P", hint(1, {{ host_ids: ["h1"] }}));
mod.publishRunEvent("Q", hint(2, {{ host_ids: ["h1"] }}));
runIds = ["r1"];
mod.publishRunEvent("P", hint(3, {{ run_ids: ["r2"] }}));
mod.publishRunEvent("P", hint(4, {{ run_ids: ["r1"] }}));
stop();
mod.publishRunEvent("P", hint(5, {{ run_ids: ["r1"], host_ids: ["h2"] }}));
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


def _branch(body: str):
    return _run("""
const capPosts = [];
const fetchImpl = (url, init) => {
    if (String(url).includes("/runs/capability")) return Promise.resolve({ ok: true, json: async () => ({ branch: { ok: true, reason: "" } }) });
    capPosts.push({ url, body: JSON.parse(init.body) });
    return Promise.resolve({ ok: true });
};
const output = {
    "1": { class_type: "SonderEditor", inputs: {} },
    "4": { class_type: "PreviewImage", inputs: {} },
    "8": { class_type: "KSampler", inputs: {} },
    "9": { class_type: "SonderControlRelay", inputs: {} },
};
let reply = (data, options) => Promise.resolve({ prompt_id: "pid-" + options.partialExecutionTargets.join(",") });
const api = { apiURL: (p) => p, queuePrompt: (n, data, options) => reply(data, options) };
let deferred = null;
const app = {
    graph: root,
    graphToPrompt: async () => ({ output, workflow: { id: "wf" } }),
    // Native app.queuePrompt: with another queue processing it returns false at once
    // and submits later; otherwise it submits and returns true.
    queuePrompt: async (n, b, ids) => {
        const submit = () => api.queuePrompt(n, { output: { ...output } }, { partialExecutionTargets: ids }).catch(() => {});
        if (deferred) { deferred.push(submit); return false; }
        await submit();
        return true;
    },
};
mod.resetBranchRunsForTests();
mod.installRunRegistration({ api, app, fetchImpl, isOutputNode: (c) => c === "PreviewImage" || c === "SonderControlRelay" });
""" + body)


def test_run_branch_matches_its_queued_prompt_and_refusals():
    (result,) = _branch("""
const queued = await mod.runBranch("4", { hostId: "page:1" });
const notOutput = await mod.runBranch("8");
const relay = await mod.runBranch("9");
const missing = await mod.runBranch("77");
reply = () => Promise.reject(Object.assign(new Error("bad"), { response: { error: { type: "prompt_outputs_failed_validation" }, node_errors: { "4": { errors: [] } } } }));
const refused = await mod.runBranch("4");
await new Promise((r) => setTimeout(r, 10));
out.push({ queued, notOutput, relay, missing, refused, posts: capPosts.map(p => [p.body.purpose, p.body.editors]) });
""")
    assert result["queued"] == {"state": "queued", "prompt_id": "pid-4"}
    assert result["notOutput"] == {"state": "refused", "code": "branch_not_output"}
    assert result["relay"] == {"state": "refused", "code": "branch_not_output"}
    assert result["missing"] == {"state": "refused", "code": "branch_not_in_prompt"}
    assert result["refused"] == {"state": "refused", "code": "prompt_outputs_failed_validation", "node_errors": {"4": {"errors": []}}}
    # Only the accepted branch registered, as a branch.
    assert result["posts"] == [["branch", ["1"]]]


def test_run_branch_waits_behind_another_queue_then_gives_up_unconfirmed():
    (result,) = _branch("""
deferred = [];
const first = mod.runBranch("4");
await new Promise((r) => setTimeout(r, 20));
const busy = await mod.runBranch("4");
// The queue frees up: the deferred submit runs and answers the waiting caller.
await deferred.shift()();
const answered = await first;
// Deferred again, never submitted within the wait: unconfirmed, and the slot frees.
const realTimeout = globalThis.setTimeout;
globalThis.setTimeout = (fn, ms) => realTimeout(fn, ms >= 30000 ? 5 : ms);
const second = await mod.runBranch("4");
globalThis.setTimeout = realTimeout;
const late = deferred.shift();
deferred = null;
await late();
await new Promise((r) => setTimeout(r, 10));
const after = await mod.runBranch("4");
out.push({ busy, answered, second, after, purposes: capPosts.map(p => p.body.purpose) });
""")
    assert result["busy"] == {"state": "busy"}
    assert result["answered"] == {"state": "queued", "prompt_id": "pid-4"}
    assert result["second"] == {"state": "unconfirmed"}
    assert result["after"] == {"state": "queued", "prompt_id": "pid-4"}
    # The late submit still registered (observed), answering no caller.
    assert result["purposes"] == ["branch", "branch", "branch"]


def test_a_mounted_request_runs_under_its_preregistered_receipt():
    (result,) = _branch("""
let stamped = null;
reply = (data, options) => { stamped = data.output["1"]._meta.sonder_receipt; return Promise.resolve({ prompt_id: "p" }); };
const outcome = await mod.runBranch("4", { receipt: "rrrrrrrr-0000-4000-8000-000000000001", hostId: "page:1" });
await new Promise((r) => setTimeout(r, 10));
out.push({ outcome, stamped, posts: capPosts.length });
""")
    assert result["outcome"] == {"state": "queued", "prompt_id": "p"}
    assert result["stamped"] == "rrrrrrrr-0000-4000-8000-000000000001"
    assert result["posts"] == 0  # registered server-side at claim


def test_a_mounted_branch_request_flushes_first_and_reuses_one_id():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    url = (ROOT / "web" / "js" / "run_outputs_client.js").as_uri()
    script = f"""
const mod = await import({json.dumps(url)});
const order = [], bodies = [];
let answers = [];
const fetchImpl = async (u, init) => {{
    const body = JSON.parse(init.body);
    bodies.push(body);
    order.push("post");
    const answer = answers.shift() || {{ ok: true, state: "pending" }};
    return {{ ok: !!answer.ok, status: answer.ok ? 200 : 409, json: async () => answer }};
}};
const make = (flush) => mod.createRunRequestClient({{
    identity: () => ({{ projectId: "P", hostId: "h", sessionId: "tab" }}), apiURL: p => p, fetchImpl,
    flush, pollMs: 1, waitMs: 2000, mint: () => "id-" + bodies.length,
}});
const unsaved = await make(async () => false).runBranch("4");
answers = [{{ ok: true, state: "pending" }}, {{ ok: true, state: "claimed" }}, {{ ok: true, state: "queued", prompt_id: "p1", request_id: "x" }}];
const queued = await make(async () => {{ order.push("flush"); return true; }}).runBranch("4");
const ids = new Set(bodies.map(b => b.request_id));
answers = [{{ ok: false, code: "not_owner" }}];
const refused = await make(async () => true).runBranch("4");
console.log("RESULT=" + JSON.stringify({{ unsaved, queued, ids: [...ids], order, refused, first: bodies[0] }}));
"""
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    out = json.loads(next(l for l in result.stdout.splitlines() if l.startswith("RESULT="))[len("RESULT="):])
    assert out["unsaved"] == {"state": "refused", "code": "controls_unsaved"}
    assert out["queued"] == {"state": "queued", "prompt_id": "p1"}
    assert out["order"][:4] == ["flush", "post", "post", "post"]
    assert out["ids"] == ["id-0"]  # the queued request posted three times under one id
    assert out["first"] == {"host_id": "h", "session_id": "tab", "request_id": "id-0", "target": "4"}
    assert out["refused"] == {"state": "refused", "code": "not_owner"}
