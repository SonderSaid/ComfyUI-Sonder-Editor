"""The asset refresh coordinator's detail-only lane.

A detail demand fetches provenance or the tracked-metadata search projection for named
assets without a list request, in priority order (selected > search > prefetch >
background), sharing in-flight reads, letting a new selection withdraw unstarted preload
work, and never letting a read that raced an asset write populate anything.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_URL = (ROOT / "web" / "js" / "asset_refresh_coordinator.js").as_uri()

HARNESS = f"""
const mod = await import({json.dumps(MODULE_URL)});
const calls = [];
const pending = [];
const listCalls = [];
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const coordinator = mod.createAssetRefreshCoordinator({{
    getLiveVersion: () => "",
    request: (demand) => new Promise((resolve) => {{ listCalls.push(demand); pending.push({{ list: true, resolve }}); }}),
    detailRequest: (demand) => new Promise((resolve, reject) => {{
        calls.push({{ kind: demand.kind, ids: [...demand.assetIds] }});
        pending.push({{ demand, resolve, reject }});
    }}),
}});
const answer = (index = 0, {{ missing = [], version = "v1" }} = {{}}) => {{
    const [call] = pending.splice(index, 1);
    const ids = call.demand.assetIds.filter((id) => !missing.includes(id));
    call.resolve({{
        modifiedAt: version,
        revisions: Object.fromEntries(ids.map((id) => [id, `r-${{id}}`])),
        values: Object.fromEntries(ids.map((id) => [id, {{ detail: id }}])),
    }});
}};
const statuses = (map) => Object.fromEntries([...map.entries()].map(([id, result]) => [id, result.status]));
"""


def _run(body):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    completed = subprocess.run([node, "--input-type=module", "-e", HARNESS + body],
                               capture_output=True, text=True, check=True)
    return json.loads(completed.stdout)


def test_selected_goes_alone_and_first_then_prefetch_in_one_batch():
    result = _run("""
const prefetch = coordinator.requestDetails({ projectId: "p", assetIds: ["b", "c", "d"], priority: "prefetch", ownerId: "g" });
const selected = coordinator.requestDetails({ projectId: "p", assetIds: ["a"], priority: "selected", ownerId: "g" });
await tick();
// Same-turn demands are ranked together: the selection requested second dispatches first.
const first = calls.map((call) => call.ids);
answer(0); await tick();
answer(0); await tick();
const [p, s] = await Promise.all([prefetch.promise, selected.promise]);
console.log(JSON.stringify({ first, all: calls.map((call) => call.ids), p: statuses(p), s: statuses(s),
    value: s.get("a").value, revision: s.get("a").revision, list: listCalls.length }));
""")
    assert result["first"] == [["a"]]
    assert result["all"] == [["a"], ["b", "c", "d"]]
    assert result["p"] == {"b": "ok", "c": "ok", "d": "ok"}
    assert result["s"] == {"a": "ok"}
    assert result["value"] == {"detail": "a"} and result["revision"] == "r-a"
    assert result["list"] == 0


def test_queued_selection_outranks_queued_prefetch_and_batches_split_at_64():
    result = _run("""
const blocker = coordinator.requestDetails({ projectId: "p", assetIds: ["x"], priority: "background" });
await tick();
const window = Array.from({ length: 70 }, (_, i) => `w${i}`);
coordinator.requestDetails({ projectId: "p", assetIds: window, priority: "prefetch", ownerId: "g" });
coordinator.requestDetails({ projectId: "p", assetIds: ["t1"], kind: "search", priority: "search", ownerId: "s" });
coordinator.requestDetails({ projectId: "p", assetIds: ["sel"], priority: "selected", ownerId: "g" });
for (let i = 0; i < 5; i += 1) { answer(0); await tick(); }
console.log(JSON.stringify(calls.map((call) => [call.kind, call.ids.length, call.ids[0]])));
""")
    assert result == [
        ["provenance", 1, "x"],
        ["provenance", 1, "sel"],
        ["search", 1, "t1"],
        ["provenance", 64, "w0"],
        ["provenance", 6, "w64"],
    ]


def test_selection_joins_in_flight_read_and_upgrades_queued_one():
    result = _run("""
const prefetch = coordinator.requestDetails({ projectId: "p", assetIds: ["a", "b"], priority: "prefetch", ownerId: "g" });
await tick();
coordinator.requestDetails({ projectId: "p", assetIds: ["q"], priority: "background" });
// "a" is in flight: joining it must not issue a second read.
const joined = coordinator.requestDetails({ projectId: "p", assetIds: ["a"], priority: "selected", ownerId: "g" });
// "q" is only queued: a selected demand upgrades it ahead of anything else queued.
coordinator.requestDetails({ projectId: "p", assetIds: ["z"], priority: "prefetch", ownerId: "other" });
const upgraded = coordinator.requestDetails({ projectId: "p", assetIds: ["q"], priority: "selected", ownerId: "g" });
answer(0); await tick();
const afterFirst = calls.map((call) => call.ids);
answer(0); await tick();
answer(0); await tick();
const [j, u] = await Promise.all([joined.promise, upgraded.promise]);
console.log(JSON.stringify({ afterFirst, calls: calls.map((call) => call.ids), j: statuses(j), u: statuses(u) }));
""")
    assert result["afterFirst"] == [["a", "b"], ["q"]]
    assert result["calls"] == [["a", "b"], ["q"], ["z"]]
    assert result["j"] == {"a": "ok"} and result["u"] == {"q": "ok"}


def test_new_selection_withdraws_unstarted_prefetch_but_in_flight_finishes():
    result = _run("""
const inFlight = coordinator.requestDetails({ projectId: "p", assetIds: ["a"], priority: "prefetch", ownerId: "g" });
await tick();
const stale = coordinator.requestDetails({ projectId: "p", assetIds: ["b", "c"], priority: "prefetch", ownerId: "g" });
const staleSelection = coordinator.requestDetails({ projectId: "p", assetIds: ["s0"], priority: "selected", ownerId: "g" });
const foreign = coordinator.requestDetails({ projectId: "p", assetIds: ["c"], priority: "prefetch", ownerId: "other" });
const newer = coordinator.requestDetails({ projectId: "p", assetIds: ["n"], priority: "background", ownerId: "g" });
coordinator.cancelDetails({ projectId: "p", ownerId: "g", priorities: ["selected", "prefetch"] });
answer(0); await tick();
answer(0); await tick();
answer(0); await tick();
const [i, s, ss, f, n] = await Promise.all([inFlight.promise, stale.promise, staleSelection.promise, foreign.promise, newer.promise]);
console.log(JSON.stringify({ calls: calls.map((call) => call.ids), i: statuses(i), s: statuses(s), ss: statuses(ss), f: statuses(f), n: statuses(n) }));
""")
    # "c" survives because another owner still wants it; "n" is background, not withdrawn.
    assert result["calls"] == [["a"], ["c"], ["n"]]
    assert result["i"] == {"a": "ok"}
    assert result["s"] == {"b": "cancelled", "c": "cancelled"}
    assert result["ss"] == {"s0": "cancelled"}
    assert result["f"] == {"c": "ok"}
    assert result["n"] == {"n": "ok"}


def test_handle_cancel_withdraws_only_its_own_unstarted_ids():
    result = _run("""
coordinator.requestDetails({ projectId: "p", assetIds: ["busy"], priority: "background" });
await tick();
const mine = coordinator.requestDetails({ projectId: "p", assetIds: ["a", "b"], priority: "prefetch", ownerId: "g" });
const theirs = coordinator.requestDetails({ projectId: "p", assetIds: ["b"], priority: "prefetch", ownerId: "g" });
mine.cancel();
answer(0); await tick();
answer(0); await tick();
const [m, t] = await Promise.all([mine.promise, theirs.promise]);
console.log(JSON.stringify({ calls: calls.map((call) => call.ids), m: statuses(m), t: statuses(t) }));
""")
    assert result["calls"] == [["busy"], ["b"]]
    assert result["m"] == {"a": "cancelled", "b": "cancelled"}
    assert result["t"] == {"b": "ok"}


def test_asset_write_during_read_requeues_then_gives_up():
    result = _run("""
const handle = coordinator.requestDetails({ projectId: "p", assetIds: ["a"], priority: "selected" });
await tick();
coordinator.markMutation("p");
answer(0); await tick();
const afterFirst = calls.length;
answer(0); await tick();
const settled = await handle.promise;
const later = coordinator.requestDetails({ projectId: "p", assetIds: ["b"], priority: "selected" });
await tick();
for (let i = 0; i < 3; i += 1) { coordinator.markMutation("p"); answer(0); await tick(); }
const gaveUp = await later.promise;
console.log(JSON.stringify({ afterFirst, first: settled.get("a"), gaveUp: gaveUp.get("b").status,
    code: gaveUp.get("b").error?.code, calls: calls.length }));
""")
    assert result["afterFirst"] == 2
    assert result["first"]["status"] == "ok" and result["first"]["epoch"] == 1
    assert result["gaveUp"] == "error" and result["code"] == "asset_detail_superseded"
    assert result["calls"] == 5


def test_missing_ids_and_transport_failures_are_per_asset_results():
    result = _run("""
const handle = coordinator.requestDetails({ projectId: "p", assetIds: ["a", "gone"], priority: "prefetch" });
await tick();
answer(0, { missing: ["gone"] }); await tick();
const first = statuses(await handle.promise);
const failing = coordinator.requestDetails({ projectId: "p", assetIds: ["x", "y"], priority: "selected" });
await tick();
const [call] = pending.splice(0, 1);
const error = new Error("boom"); error.status = 404;
call.reject(error); await tick();
const failed = await failing.promise;
const after = coordinator.requestDetails({ projectId: "p", assetIds: ["x"], priority: "selected" });
await tick();
answer(0); await tick();
console.log(JSON.stringify({ first, failed: statuses(failed), status: failed.get("x").error.status, after: statuses(await after.promise) }));
""")
    assert result["first"] == {"a": "ok", "gone": "missing"}
    assert result["failed"] == {"x": "error", "y": "error"}
    assert result["status"] == 404
    # A failure is not remembered by the lane: the next demand reads again.
    assert result["after"] == {"x": "ok"}


def test_detail_lane_never_joins_satisfies_or_is_disturbed_by_list_refresh():
    result = _run("""
const list = coordinator.request({ projectId: "p", waveId: "w", reason: "list" });
const detail = coordinator.requestDetails({ projectId: "p", assetIds: ["a"], priority: "selected" });
await tick();
const order = pending.map((call) => call.list ? "list" : "detail");
// Settle the list first: its lane going idle deletes its per-project state, which must
// not orphan the in-flight detail read or let a second detail read run beside it.
pending.splice(0, 1)[0].resolve({ payload: { assets: [{ asset_id: "a" }] }, response: { status: 200, headers: { get: () => "" } } });
const listResult = await list;
const second = coordinator.requestDetails({ projectId: "p", assetIds: ["b"], priority: "selected" });
await tick();
const concurrent = pending.length;
answer(0); await tick();
answer(0); await tick();
const [d, s] = await Promise.all([detail.promise, second.promise]);
console.log(JSON.stringify({ order, concurrent, listAssets: listResult.payload.assets.length,
    d: statuses(d), s: statuses(s), listCalls: listCalls.length, detailCalls: calls.length }));
""")
    assert result["order"] == ["list", "detail"]
    assert result["concurrent"] == 1
    assert result["listAssets"] == 1
    assert result["d"] == {"a": "ok"} and result["s"] == {"b": "ok"}
    assert result["listCalls"] == 1 and result["detailCalls"] == 2


def test_projects_are_independent_lanes_and_empty_demands_resolve():
    result = _run("""
const a = coordinator.requestDetails({ projectId: "p1", assetIds: ["a"], priority: "prefetch" });
const b = coordinator.requestDetails({ projectId: "p2", assetIds: ["a"], priority: "prefetch" });
await tick();
const inFlight = pending.length;
const empty = await coordinator.requestDetails({ projectId: "p1", assetIds: [] }).promise;
const noProject = await coordinator.requestDetails({ projectId: "", assetIds: ["a"] }).promise;
answer(0); answer(0); await tick();
console.log(JSON.stringify({ inFlight, empty: empty.size, noProject: noProject.size,
    a: statuses(await a.promise), b: statuses(await b.promise) }));
""")
    assert result == {"inFlight": 2, "empty": 0, "noProject": 0, "a": {"a": "ok"}, "b": {"a": "ok"}}


def test_batches_and_joins_never_mix_kinds():
    result = _run("""
coordinator.requestDetails({ projectId: "p", assetIds: ["busy"], priority: "background" });
await tick();
coordinator.requestDetails({ projectId: "p", assetIds: ["a"], kind: "search", priority: "prefetch" });
coordinator.requestDetails({ projectId: "p", assetIds: ["b"], priority: "prefetch" });
answer(0); await tick();
// "a" (search) is in flight now: a provenance demand for "a" must not join it.
const provenance = coordinator.requestDetails({ projectId: "p", assetIds: ["a"], priority: "prefetch" });
for (let i = 0; i < 2; i += 1) { answer(0); await tick(); }
console.log(JSON.stringify({ calls: calls.map((call) => [call.kind, call.ids]), p: statuses(await provenance.promise) }));
""")
    # Same rank, different kinds: the search read goes alone, and the provenance read of
    # "a" batches with "b" instead of joining it.
    assert result["calls"] == [["provenance", ["busy"]], ["search", ["a"]], ["provenance", ["b", "a"]]]
    assert result["p"] == {"a": "ok"}


def test_asking_again_from_inside_a_result_callback_starts_a_fresh_read():
    result = _run("""
let inner = null;
const outer = coordinator.requestDetails({ projectId: "p", assetIds: ["a", "b"], priority: "prefetch",
    onResult: (id) => {
        // Synchronously, while the finished batch is still settling its waiters.
        if (id === "b" && !inner) inner = coordinator.requestDetails({ projectId: "p", assetIds: ["a"], priority: "selected" });
    } });
await tick();
answer(0); await tick();
answer(0); await tick();
const settled = await Promise.race([inner.promise.then((m) => statuses(m)), new Promise((r) => setTimeout(() => r("hung"), 50))]);
console.log(JSON.stringify({ settled, calls: calls.map((call) => call.ids), outer: statuses(await outer.promise) }));
""")
    assert result["settled"] == {"a": "ok"}
    assert result["calls"] == [["a", "b"], ["a"]]
    assert result["outer"] == {"a": "ok", "b": "ok"}


def test_a_selection_that_joined_keeps_its_priority_when_the_read_is_redone():
    result = _run("""
coordinator.requestDetails({ projectId: "p", assetIds: ["a"], priority: "prefetch" });
await tick();
coordinator.requestDetails({ projectId: "p", assetIds: ["a"], priority: "selected" });
coordinator.requestDetails({ projectId: "p", assetIds: ["s"], kind: "search", priority: "search" });
coordinator.markMutation("p");
for (let i = 0; i < 3; i += 1) { answer(0); await tick(); }
console.log(JSON.stringify(calls.map((call) => [call.kind, call.ids])));
""")
    # The superseded read of "a" is redone ahead of the queued search, at the selection's rank.
    assert result == [["provenance", ["a"]], ["provenance", ["a"]], ["search", ["s"]]]


def test_a_demand_after_a_mutation_does_not_join_the_overtaken_read_or_inherit_its_budget():
    result = _run("""
const first = coordinator.requestDetails({ projectId: "p", assetIds: ["a"], priority: "selected" });
await tick();
coordinator.markMutation("p");
coordinator.markMutation("p");
const fresh = coordinator.requestDetails({ projectId: "p", assetIds: ["a"], priority: "selected" });
await tick();
const inFlight = pending.length;
for (let i = 0; i < 2; i += 1) { answer(0); await tick(); }
console.log(JSON.stringify({ inFlight, first: statuses(await first.promise), fresh: statuses(await fresh.promise), calls: calls.length }));
""")
    assert result["inFlight"] == 1
    assert result["fresh"] == {"a": "ok"} and result["first"] == {"a": "ok"}
    # The overtaken read is superseded once and absorbed into the fresh entry.
    assert result["calls"] == 2


def test_a_server_error_on_a_batch_is_retried_per_asset_before_anything_is_reported():
    result = _run("""
const handle = coordinator.requestDetails({ projectId: "p", assetIds: ["good", "bad", "fine"], priority: "prefetch" });
await tick();
const [call] = pending.splice(0, 1);
const error = new Error("storage"); error.status = 500;
call.reject(error); await tick();
const isolated = calls.slice(1).map((c) => c.ids);
for (let i = 0; i < 3; i += 1) {
    const next = pending.splice(0, 1)[0];
    if (next.demand.assetIds[0] === "bad") { const e = new Error("storage"); e.status = 500; next.reject(e); }
    else next.resolve({ modifiedAt: "v1", revisions: { [next.demand.assetIds[0]]: "r" }, values: {} });
    await tick();
}
const transport = coordinator.requestDetails({ projectId: "p", assetIds: ["x", "y"], priority: "prefetch" });
await tick();
pending.splice(0, 1)[0].reject(new Error("offline")); await tick();
console.log(JSON.stringify({ isolated, result: statuses(await handle.promise), transport: statuses(await transport.promise), total: calls.length }));
""")
    assert result["isolated"] == [["good"]]
    assert result["result"] == {"good": "ok", "bad": "error", "fine": "ok"}
    # A dropped connection is not isolated: every id reports it at once.
    assert result["transport"] == {"x": "error", "y": "error"}
    assert result["total"] == 5


def test_a_synchronous_transport_throw_is_a_per_asset_error_and_the_lane_recovers():
    result = _run("""
let throwNext = true;
const local = mod.createAssetRefreshCoordinator({ getLiveVersion: () => "",
    detailRequest: (demand) => {
        if (throwNext) { throwNext = false; throw new Error("sync"); }
        return Promise.resolve({ modifiedAt: "v1", revisions: { [demand.assetIds[0]]: "r" }, values: {} });
    } });
const failed = await local.requestDetails({ projectId: "p", assetIds: ["a"], priority: "selected" }).promise;
const recovered = await local.requestDetails({ projectId: "p", assetIds: ["b"], priority: "selected" }).promise;
await tick();
console.log(JSON.stringify({ failed: statuses(failed), recovered: statuses(recovered), idle: local._debugDetailState("p") === null }));
""")
    assert result == {"failed": {"a": "error"}, "recovered": {"b": "ok"}, "idle": True}


def test_cancel_recomputes_the_rank_and_idle_lanes_are_released():
    result = _run("""
coordinator.requestDetails({ projectId: "p", assetIds: ["busy"], priority: "background" });
await tick();
coordinator.requestDetails({ projectId: "p", assetIds: ["a"], priority: "selected", ownerId: "g" });
coordinator.requestDetails({ projectId: "p", assetIds: ["a"], priority: "background", ownerId: "other" });
coordinator.requestDetails({ projectId: "p", assetIds: ["b"], priority: "prefetch", ownerId: "other" });
coordinator.cancelDetails({ projectId: "p", ownerId: "g", priorities: ["selected", "nonsense"] });
coordinator.cancelDetails({ projectId: "p", ownerId: "other", priorities: ["nonsense"] });
for (let i = 0; i < 3; i += 1) { answer(0); await tick(); }
await tick();
console.log(JSON.stringify({ calls: calls.map((call) => call.ids), idle: coordinator._debugDetailState("p") === null }));
""")
    # With the selection withdrawn "a" falls back to background, behind the prefetch of "b";
    # an unknown priority name withdraws nothing.
    assert result["calls"] == [["busy"], ["b"], ["a"]]
    assert result["idle"] is True


def test_a_joiners_recorder_hears_the_outcome():
    result = _run("""
const heard = [];
coordinator.requestDetails({ projectId: "p", assetIds: ["a"], priority: "prefetch" });
await tick();
coordinator.requestDetails({ projectId: "p", assetIds: ["a"], priority: "selected", diagnosticRecorder: (event) => heard.push(event.kind) });
answer(0); await tick();
console.log(JSON.stringify(heard));
""")
    assert result == ["asset_detail_response"]


def test_default_transport_maps_both_routes():
    result = _run("""
const seen = [];
globalThis.window = { comfyAPI: { api: { api: { apiURL: (path) => `http://host${path}` } } } };
globalThis.fetch = async (url) => {
    seen.push(String(url));
    const search = String(url).includes("search-metadata");
    const body = search
        ? { modified_at: "v2", revisions: { a: "r" }, tracked_metadata: { a: [{ label: "L" }] } }
        : { modified_at: "v2", revisions: { a: "r" }, provenance: { a: { prompt: "p" } } };
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
};
const local = mod.createAssetRefreshCoordinator({ getLiveVersion: () => "" });
const p = await local.requestDetails({ projectId: "My Project", assetIds: ["a", "b"], priority: "selected" }).promise;
const s = await local.requestDetails({ projectId: "My Project", assetIds: ["a"], kind: "search", priority: "search" }).promise;
console.log(JSON.stringify({ seen, p: [p.get("a"), p.get("b")], s: s.get("a") }));
""")
    assert result["seen"] == [
        "http://host/sonder-editor/project/My%20Project/assets/provenance?asset_id=a&asset_id=b",
        "http://host/sonder-editor/project/My%20Project/assets/search-metadata?asset_id=a",
    ]
    assert result["p"][0] == {"status": "ok", "revision": "r", "value": {"prompt": "p"}, "modifiedAt": "v2", "epoch": 0}
    assert result["p"][1] == {"status": "missing", "modifiedAt": "v2", "epoch": 0}
    assert result["s"]["value"] == [{"label": "L"}]
