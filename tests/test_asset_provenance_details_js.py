"""Gallery provenance detail loading and the tracked-search projection.

Both run against the real asset refresh coordinator with a scripted detail transport, so
priority, joining and cancellation are the coordinator's own. What these pin: the selected
asset is read first and alone, the preload window follows the surface's order, the cache
keeps displayed details outside its cap, a detail that no longer matches the listed
revision refreshes the list once and then reports a retryable error, new arrivals preload
only on an additive refresh, and search never answers from a partial projection.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DETAILS_URL = (ROOT / "web" / "js" / "asset_provenance_details.js").as_uri()
COORDINATOR_URL = (ROOT / "web" / "js" / "asset_refresh_coordinator.js").as_uri()

HARNESS = f"""
const details = await import({json.dumps(DETAILS_URL)});
const {{ createAssetRefreshCoordinator }} = await import({json.dumps(COORDINATOR_URL)});
const calls = [];
const pending = [];
const listRefreshes = [];
const heldRefreshes = [];
let holdRefresh = false;
const changes = [];
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const settle = async (n = 6) => {{ for (let i = 0; i < n; i += 1) await tick(); }};
// Server state the scripted transport answers from: id -> revision (absent = deleted).
const server = {{ version: "v1", revisions: {{}}, tracked: {{}} }};
const coordinator = createAssetRefreshCoordinator({{
    getLiveVersion: () => "",
    detailRequest: (demand) => new Promise((resolve, reject) => {{
        calls.push({{ kind: demand.kind, ids: [...demand.assetIds] }});
        pending.push({{ demand, resolve, reject }});
    }}),
}});
const answer = (index = 0) => {{
    const [call] = pending.splice(index, 1);
    const ids = call.demand.assetIds.filter((id) => id in server.revisions);
    call.resolve({{
        modifiedAt: server.version,
        revisions: Object.fromEntries(ids.map((id) => [id, server.revisions[id]])),
        values: Object.fromEntries(ids.map((id) => [id, call.demand.kind === "search"
            ? (server.tracked[id] ?? null)
            : {{ prompt: `p-${{id}}`, editor_export: {{ tracked_metadata: server.tracked[id] ?? [] }} }}])),
    }});
}};
const answerAll = async () => {{ for (let i = 0; i < 40 && pending.length; i += 1) {{ answer(0); await tick(); }} }};
const asset = (id, extra = {{}}) => ({{ asset_id: id, provenance_revision: `r-${{id}}`, has_provenance: true, imported_at: "", ...extra }});
const listOf = (...ids) => ids.map((id) => asset(id));
const makeLoader = (settings = {{}}) => details.createDetailLoader({{
    ownerId: "g",
    requestDetails: coordinator.requestDetails,
    cancelDetails: coordinator.cancelDetails,
    requestListRefresh: (request) => {{ listRefreshes.push(request);
        return holdRefresh ? new Promise((resolve) => heldRefreshes.push(resolve)) : Promise.resolve(); }},
    onChange: (ids) => changes.push(ids),
    settings: {{ preloadFollowingAssets: 50, preloadNewAssets: true, maxCachedProvenanceDetails: 0, ...settings }},
}});
const serve = (assets, version = "v1") => {{
    server.version = version;
    for (const a of assets) server.revisions[a.asset_id] = a.provenance_revision;
}};
"""


def _run(body):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    completed = subprocess.run([node, "--input-type=module", "-e", HARNESS + body],
                               capture_output=True, text=True, check=True)
    return json.loads(completed.stdout)


def test_window_planning_helpers():
    result = _run("""
const ids = ["a", "b", "c", "d"];
console.log(JSON.stringify({
    list: details.followingIds(ids, "b", 5),
    wrap: details.followingIds(ids, "c", 3, { wrap: true }),
    none: details.followingIds(ids, "zz", 3),
    zero: details.followingIds(ids, "a", 0),
    counts: [
        details.effectivePreloadCount({ preloadFollowingAssets: 50 }),
        details.effectivePreloadCount({ preloadFollowingAssets: 99 }),
        details.effectivePreloadCount({ preloadFollowingAssets: 50, maxCachedProvenanceDetails: 8 }),
        details.effectivePreloadCount({ preloadFollowingAssets: 0, maxCachedProvenanceDetails: 8 }),
    ],
    needs: [
        details.assetNeedsDetail(asset("x")),
        details.assetNeedsDetail(asset("x", { has_provenance: false })),
        details.assetNeedsDetail({ asset_id: "x", provenance_revision: "r" }),
        details.assetNeedsDetail(asset("x", { generation_params: { prompt: "full" } })),
        details.assetNeedsDetail({ asset_id: "x" }),
    ],
}));
""")
    assert result["list"] == ["c", "d"]
    assert result["wrap"] == ["d", "a", "b"]
    assert result["none"] == [] and result["zero"] == []
    assert result["counts"] == [50, 63, 8, 0]
    # A server predating `has_provenance` omits it: that means "may have some", so fetch.
    assert result["needs"] == [True, False, True, False, False]


def test_cache_is_lru_and_holds_displayed_details_outside_its_cap():
    result = _run("""
const cache = details.createDetailCache({ limit: 1 });
cache.setDisplayed(["a", "b"]);
cache.set("a", "r1", 1); cache.set("b", "r1", 2); cache.set("c", "r1", 3); cache.set("d", "r1", 4);
const withCompare = cache.ids();
cache.setDisplayed([]);
const afterRelease = cache.ids();
const lru = details.createDetailCache({ limit: 2 });
lru.set("a", "r", 1); lru.set("b", "r", 2); lru.get("a", "r"); lru.set("c", "r", 3);
const touched = lru.ids();
const stale = lru.get("a", "other");
lru.retain(new Map([["a", "r"], ["c", "moved"]]));
console.log(JSON.stringify({ withCompare, afterRelease, touched, stale: stale === undefined, retained: lru.ids() }));
""")
    # Cap 1: both compare sides stay while shown, plus the one most recent reusable entry.
    assert result["withCompare"] == ["a", "b", "d"]
    assert result["afterRelease"] == ["d"]
    assert result["touched"] == ["a", "c"]
    assert result["stale"] is True
    assert result["retained"] == ["a"]


def test_selected_asset_is_read_alone_first_then_its_window_without_a_list_request():
    result = _run("""
const list = listOf("s", "n1", "n2", "n3");
list.splice(2, 0, asset("bare", { has_provenance: false }));
serve(list);
const loader = makeLoader({ preloadFollowingAssets: 3 });
loader.listChanged({ projectId: "p", assets: list, version: "v1" });
loader.sync({ projectId: "p", displayed: [list[0]], order: list, currentId: "s" });
const before = loader.resolve(list[0]).status;
await settle(1);
await answerAll(); await settle();
const after = loader.resolve(list[0]);
console.log(JSON.stringify({ calls, before, after, bare: loader.resolve(list[2]),
    changes, refreshes: listRefreshes.length, cached: loader._debug().cached }));
""")
    assert result["calls"] == [{"kind": "provenance", "ids": ["s"]},
                               {"kind": "provenance", "ids": ["n1", "n2"]}]
    assert result["before"] == "loading"
    assert result["after"]["status"] == "ready" and result["after"]["params"]["prompt"] == "p-s"
    # No provenance: resolved from the lean record, never requested.
    assert result["bare"] == {"status": "ready", "params": {}}
    assert result["changes"][0] == ["s"]
    assert result["refreshes"] == 0
    assert set(result["cached"]) == {"s", "n1", "n2"}


def test_moving_on_withdraws_the_unstarted_window_and_ranks_the_new_selection_first():
    result = _run("""
const list = listOf(...Array.from({ length: 8 }, (_, i) => `a${i}`));
serve(list);
const loader = makeLoader({ preloadFollowingAssets: 2 });
loader.listChanged({ projectId: "p", assets: list, version: "v1" });
loader.sync({ projectId: "p", displayed: [list[0]], order: list, currentId: "a0" });
await settle(1);
// a0 is in flight; its window [a1, a2] is queued. Jump to a5 before anything answers.
loader.sync({ projectId: "p", displayed: [list[5]], order: list, currentId: "a5" });
await answerAll(); await settle();
console.log(JSON.stringify(calls.map((call) => call.ids)));
""")
    assert result == [["a0"], ["a5"], ["a6", "a7"]]


def test_unchanged_demand_asks_nothing_twice():
    result = _run("""
const list = listOf("a", "b", "c");
serve(list);
const loader = makeLoader({ preloadFollowingAssets: 2 });
loader.listChanged({ projectId: "p", assets: list, version: "v1" });
for (let i = 0; i < 3; i += 1) loader.sync({ projectId: "p", displayed: [list[0]], order: list, currentId: "a" });
await settle(1);
for (let i = 0; i < 3; i += 1) loader.sync({ projectId: "p", displayed: [list[0]], order: list, currentId: "a" });
await answerAll(); await settle();
loader.sync({ projectId: "p", displayed: [list[0]], order: list, currentId: "a" });
await settle();
console.log(JSON.stringify(calls.map((call) => call.ids)));
""")
    assert result == [["a"], ["b", "c"]]


def test_a_detail_newer_than_the_list_refreshes_it_once_then_errors_retryably():
    result = _run("""
const list = listOf("a");
serve(list, "v2");
server.revisions.a = "r-a-moved";
const loader = makeLoader();
loader.listChanged({ projectId: "p", assets: list, version: "v1" });
const show = () => loader.sync({ projectId: "p", displayed: [list[0]], order: list, currentId: "a" });
show(); await settle(1); await answerAll(); await settle();
const firstRefresh = listRefreshes.slice();
const afterFirst = loader.resolve(list[0]).status;
// The host's refresh found nothing newer (same list): the gallery asks once more.
show(); await settle(1); await answerAll(); await settle();
const second = loader.resolve(list[0]);
show(); await settle(1);
const quiet = pending.length;
loader.retry(list[0]); await settle();
show(); await settle(1);
const retried = pending.length;
console.log(JSON.stringify({ firstRefresh, afterFirst, second, refreshes: listRefreshes.length, quiet, retried }));
""")
    assert result["firstRefresh"] == [{"requiredVersion": "v2", "reason": "gallery_detail_mismatch"}]
    assert result["afterFirst"] == "loading"
    assert result["second"] == {"status": "error", "message": "Metadata changed while loading."}
    assert result["refreshes"] == 1
    assert result["quiet"] == 0
    assert result["retried"] == 1


def test_a_newer_list_supersedes_the_mismatch_and_loads_the_new_revision():
    result = _run("""
const list = listOf("a");
server.version = "v2"; server.revisions.a = "r-a2";
const loader = makeLoader();
loader.listChanged({ projectId: "p", assets: list, version: "v1" });
loader.sync({ projectId: "p", displayed: [list[0]], order: list, currentId: "a" });
await settle(1); await answerAll(); await settle();
const fresh = [asset("a", { provenance_revision: "r-a2" })];
loader.listChanged({ projectId: "p", assets: fresh, version: "v2" });
loader.sync({ projectId: "p", displayed: fresh, order: fresh, currentId: "a" });
await settle(1); await answerAll(); await settle();
console.log(JSON.stringify({ old: loader.resolve(list[0]).status, fresh: loader.resolve(fresh[0]).status, refreshes: listRefreshes.length }));
""")
    assert result == {"old": "loading", "fresh": "ready", "refreshes": 1}


def test_an_older_served_version_is_not_adopted():
    result = _run("""
const list = listOf("a");
serve(list, "v0");
const loader = makeLoader();
loader.listChanged({ projectId: "p", assets: list, version: "v1" });
loader.sync({ projectId: "p", displayed: [list[0]], order: list, currentId: "a" });
await settle(1); await answerAll(); await settle();
console.log(JSON.stringify({ status: loader.resolve(list[0]).status, refreshes: listRefreshes }));
""")
    assert result["status"] == "loading"
    assert result["refreshes"] == [{"requiredVersion": "v0", "reason": "gallery_detail_mismatch"}]


def test_preload_failures_stay_silent_and_do_not_block_selecting_that_asset():
    result = _run("""
const list = listOf("a", "b");
serve(list);
const loader = makeLoader({ preloadFollowingAssets: 1 });
loader.listChanged({ projectId: "p", assets: list, version: "v1" });
loader.sync({ projectId: "p", displayed: [list[0]], order: list, currentId: "a" });
await settle(1); answer(0); await settle(1);
const [call] = pending.splice(0, 1); call.reject(new Error("preload down")); await settle();
const preloadFailed = loader.resolve(list[1]).status;
const silent = changes.flat().includes("b");
loader.sync({ projectId: "p", displayed: [list[1]], order: list, currentId: "b" });
await settle(1);
const [again] = pending.splice(0, 1); again.reject(new Error("still down")); await settle();
console.log(JSON.stringify({ preloadFailed, silent, calls: calls.map((c) => c.ids), selected: loader.resolve(list[1]) }));
""")
    assert result["preloadFailed"] == "loading" and result["silent"] is False
    assert result["calls"] == [["a"], ["b"], ["b"]]
    assert result["selected"] == {"status": "error", "message": "still down"}


def test_project_switch_resets_and_late_old_project_results_are_ignored():
    result = _run("""
const list = listOf("a");
serve(list);
const loader = makeLoader();
loader.listChanged({ projectId: "p1", assets: list, version: "v1" });
loader.sync({ projectId: "p1", displayed: [list[0]], order: list, currentId: "a" });
await settle(1);
loader.listChanged({ projectId: "p2", assets: list, version: "v9" });
answer(0); await settle();
const afterSwitch = loader.resolve(list[0]).status;
// A stale surface still showing p1 asks nothing of p2.
loader.sync({ projectId: "p1", displayed: [list[0]], order: list, currentId: "a" });
await settle(1);
console.log(JSON.stringify({ afterSwitch, calls: calls.length, debug: loader._debug() }));
""")
    assert result["afterSwitch"] == "loading"
    assert result["calls"] == 1
    assert result["debug"]["cached"] == [] and result["debug"]["projectId"] == "p2"


def test_compare_with_a_cap_of_one_keeps_both_sides():
    result = _run("""
const list = listOf("a", "b", "c", "d");
serve(list);
const loader = makeLoader({ preloadFollowingAssets: 50, maxCachedProvenanceDetails: 1 });
loader.listChanged({ projectId: "p", assets: list, version: "v1" });
loader.sync({ projectId: "p", displayed: [list[0], list[1]], order: list, currentId: "b", wrap: true });
await settle(1); await answerAll(); await settle();
console.log(JSON.stringify({ a: loader.resolve(list[0]).status, b: loader.resolve(list[1]).status,
    calls: calls.map((c) => c.ids), cached: loader._debug().cached }));
""")
    assert result["a"] == "ready" and result["b"] == "ready"
    # The window is bounded by the cap: one following asset, not fifty.
    assert result["calls"] == [["a", "b"], ["c"]]
    assert set(result["cached"]) == {"a", "b", "c"}


def test_new_arrivals_preload_only_on_an_additive_versioned_refresh():
    result = _run("""
const first = listOf("a", "b");
serve(first);
const loader = makeLoader();
// A host placeholder seed has no version: it is not a loaded list.
loader.listChanged({ projectId: "p", assets: [], version: "" });
loader.listChanged({ projectId: "p", assets: first, version: "v1" });
await settle(1);
const initial = calls.length;
const withTake = [...first, asset("take", { imported_at: "2026-09-23T10:00:00" }), asset("bare", { has_provenance: false }),
    asset("older", { imported_at: "2026-09-23T09:00:00" })];
serve(withTake, "v2");
loader.listChanged({ projectId: "p", assets: withTake, version: "v2" });
await settle(1); await answerAll(); await settle();
const preloaded = calls.map((c) => c.ids);
const ready = loader.resolve(withTake[2]).status;
const off = makeLoader({ preloadNewAssets: false });
off.listChanged({ projectId: "p", assets: first, version: "v1" });
off.listChanged({ projectId: "p", assets: withTake, version: "v2" });
await settle(1);
console.log(JSON.stringify({ initial, preloaded, ready, offCalls: calls.length - preloaded.length }));
""")
    assert result["initial"] == 0
    assert result["preloaded"] == [["take", "older"]]
    assert result["ready"] == "ready"
    assert result["offCalls"] == 0


def test_new_arrivals_are_capped_newest_first():
    result = _run("""
const base = listOf("a");
const loader = makeLoader();
loader.listChanged({ projectId: "p", assets: base, version: "v1" });
const burst = Array.from({ length: 70 }, (_, i) => asset(`n${String(i).padStart(2, "0")}`,
    { imported_at: new Date(Date.UTC(2026, 8, 23, 10, 0, i)).toISOString() }));
loader.listChanged({ projectId: "p", assets: [...base, ...burst], version: "v2" });
await settle(1);
console.log(JSON.stringify({ count: calls[0].ids.length, first: calls[0].ids[0], last: calls[0].ids.at(-1) }));
""")
    assert result == {"count": 63, "first": "n69", "last": "n07"}


def test_complete_record_resolves_without_a_request():
    result = _run("""
const full = asset("a", { generation_params: { prompt: "inline" } });
const loader = makeLoader();
loader.listChanged({ projectId: "p", assets: [full], version: "v1" });
loader.sync({ projectId: "p", displayed: [full], order: [full], currentId: "a" });
await settle(1);
console.log(JSON.stringify({ detail: loader.resolve(full), calls: calls.length }));
""")
    assert result == {"detail": {"status": "ready", "params": {"prompt": "inline"}}, "calls": 0}


PROJECTION = """
const changesSearch = [];
const makeProjection = (peek = () => undefined) => details.createSearchProjection({
    ownerId: "s",
    requestDetails: coordinator.requestDetails,
    cancelDetails: coordinator.cancelDetails,
    requestListRefresh: (request) => { listRefreshes.push(request);
        return holdRefresh ? new Promise((resolve) => heldRefreshes.push(resolve)) : Promise.resolve(); },
    onChange: (status) => changesSearch.push(status),
    peekDetail: peek,
});
"""


def test_search_is_loading_until_every_batch_lands_then_reuses_the_projection():
    result = _run(PROJECTION + """
const list = listOf(...Array.from({ length: 70 }, (_, i) => `a${i}`));
list.push(asset("bare", { has_provenance: false }));
serve(list);
server.tracked.a3 = [{ label: "LoRA", fields: { name: "x" } }];
const projection = makeProjection();
const first = projection.ensure({ projectId: "p", assets: list, version: "v1" });
await settle(1);
answer(0); await settle();
const partial = projection.status;
answer(0); await settle();
const done = projection.status;
const again = projection.ensure({ projectId: "p", assets: list, version: "v1" });
await settle(1);
console.log(JSON.stringify({ first, partial, done, again, calls: calls.map((c) => [c.kind, c.ids.length]),
    value: projection.valueFor(list[3]), empty: projection.valueFor(list[4]), bare: projection.valueFor(list[70]) === undefined,
    changesSearch }));
""")
    assert result["first"] == "loading"
    assert result["partial"] == "loading"
    assert result["done"] == "ready" and result["again"] == "ready"
    assert result["calls"] == [["search", 64], ["search", 6]]
    assert result["value"] == [{"label": "LoRA", "fields": {"name": "x"}}]
    assert result["empty"] is None
    assert result["bare"] is True  # not fetched; the gallery answers it from the record
    assert result["changesSearch"] == ["ready"]


def test_search_refetches_only_what_moved_and_uses_held_details():
    result = _run(PROJECTION + """
const list = listOf("a", "b", "c");
serve(list);
const projection = makeProjection((id, revision) => id === "c" && revision === "r-c"
    ? { editor_export: { tracked_metadata: ["held"] } } : undefined);
projection.ensure({ projectId: "p", assets: list, version: "v1" });
await settle(1); await answerAll(); await settle();
const firstCalls = calls.map((c) => c.ids);
const next = [...list, asset("take")];
serve(next, "v2");
const status = projection.ensure({ projectId: "p", assets: next, version: "v2" });
await settle(1); await answerAll(); await settle();
console.log(JSON.stringify({ firstCalls, status, calls: calls.map((c) => c.ids), held: projection.valueFor(list[2]),
    final: projection.status }));
""")
    assert result["firstCalls"] == [["a", "b"]]
    assert result["status"] == "loading"
    assert result["calls"] == [["a", "b"], ["take"]]
    assert result["held"] == ["held"]
    assert result["final"] == "ready"


def test_search_mismatch_refreshes_the_list_and_restarts_once_then_errors():
    result = _run(PROJECTION + """
const list = listOf("a", "b");
serve(list, "v3");
server.revisions.b = "r-b-moved";
const projection = makeProjection();
projection.ensure({ projectId: "p", assets: list, version: "v1" });
await settle(1); await answerAll(); await settle();
const afterFirst = { status: projection.status, refreshes: listRefreshes.slice() };
// Same list re-ensured after the host refresh found nothing newer.
projection.ensure({ projectId: "p", assets: list, version: "v1" });
await settle(1); await answerAll(); await settle();
const afterSecond = { status: projection.status, message: projection.message };
projection.ensure({ projectId: "p", assets: list, version: "v1" });
await settle(1);
const quiet = pending.length;
projection.retry(); await settle();
projection.ensure({ projectId: "p", assets: list, version: "v1" });
await settle(1);
console.log(JSON.stringify({ afterFirst, afterSecond, quiet, retried: pending.length, refreshes: listRefreshes.length }));
""")
    # Still loading while the list refresh runs; the restart follows it.
    assert result["afterFirst"] == {"status": "loading", "refreshes": [{"requiredVersion": "v3", "reason": "gallery_search_mismatch"}]}
    assert result["afterSecond"]["status"] == "error"
    assert "changed" in result["afterSecond"]["message"]
    assert result["quiet"] == 0
    assert result["retried"] == 1
    assert result["refreshes"] == 1


def test_search_transport_error_is_retryable_and_project_switch_discards():
    result = _run(PROJECTION + """
const list = listOf("a");
serve(list);
const projection = makeProjection();
projection.ensure({ projectId: "p", assets: list, version: "v1" });
await settle(1);
const [call] = pending.splice(0, 1); call.reject(new Error("offline")); await settle();
const failed = { status: projection.status, message: projection.message };
projection.ensure({ projectId: "p2", assets: list, version: "v5" });
await settle(1);
const switched = projection.status;
server.version = "v5";
answer(0); await settle();
console.log(JSON.stringify({ failed, switched, final: projection.status, calls: calls.length }));
""")
    assert result["failed"] == {"status": "error", "message": "Metadata search unavailable: offline"}
    assert result["switched"] == "loading"
    assert result["final"] == "ready"
    assert result["calls"] == 2


def test_the_second_read_waits_for_the_list_refresh_after_a_mismatch():
    result = _run("""
const list = listOf("a");
serve(list, "v2");
server.revisions.a = "r-a-moved";
holdRefresh = true;
const loader = makeLoader();
loader.listChanged({ projectId: "p", assets: list, version: "v1" });
const show = () => loader.sync({ projectId: "p", displayed: [list[0]], order: list, currentId: "a" });
show(); await settle(1); await answerAll(); await settle();
// Renders and arrivals while the refresh runs must not re-read "a" against the old list.
for (let i = 0; i < 3; i += 1) { show(); await settle(1); }
const during = { calls: calls.length, status: loader.resolve(list[0]).status, refreshing: loader._debug().refreshing };
heldRefreshes.splice(0).forEach((resolve) => resolve()); await settle();
show(); await settle(1);
console.log(JSON.stringify({ during, after: calls.length }));
""")
    assert result["during"] == {"calls": 1, "status": "loading", "refreshing": ["a"]}
    assert result["after"] == 2


def test_new_arrivals_only_use_the_room_a_positive_cap_leaves_the_window():
    result = _run("""
const base = listOf("a", "b", "c");
const tight = makeLoader({ preloadFollowingAssets: 2, maxCachedProvenanceDetails: 2 });
tight.listChanged({ projectId: "p", assets: base, version: "v1" });
tight.listChanged({ projectId: "p", assets: [...base, asset("n1")], version: "v2" });
await settle(1);
const tightCalls = calls.length;
const roomy = makeLoader({ preloadFollowingAssets: 2, maxCachedProvenanceDetails: 4 });
roomy.listChanged({ projectId: "q", assets: base, version: "v1" });
roomy.listChanged({ projectId: "q", assets: [...base, ...listOf("n1", "n2", "n3")], version: "v2" });
await settle(1);
console.log(JSON.stringify({ tightCalls, roomy: calls.map((c) => c.ids.length) }));
""")
    assert result["tightCalls"] == 0
    assert result["roomy"] == [2]


def test_an_unversioned_list_after_a_loaded_one_decides_nothing():
    result = _run("""
const list = listOf("a", "b");
serve(list);
const loader = makeLoader();
loader.listChanged({ projectId: "p", assets: list, version: "v1" });
loader.sync({ projectId: "p", displayed: [list[0]], order: list, currentId: "a" });
await settle(1); await answerAll(); await settle();
const held = loader._debug().cached;
loader.listChanged({ projectId: "p", assets: [], version: "" });
const afterSeed = loader._debug().cached;
loader.listChanged({ projectId: "p", assets: list, version: "v2" });
await settle(1);
console.log(JSON.stringify({ held, afterSeed, arrivalsRequested: calls.length }));
""")
    assert set(result["held"]) == {"a", "b"}
    assert set(result["afterSeed"]) == {"a", "b"}
    assert result["arrivalsRequested"] == 2  # the selected read and its window; no arrivals


def test_only_a_window_or_cap_change_replans_queued_reads():
    result = _run("""
const list = listOf(...Array.from({ length: 6 }, (_, i) => `a${i}`));
serve(list);
const loader = makeLoader({ preloadFollowingAssets: 3 });
loader.listChanged({ projectId: "p", assets: list, version: "v1" });
coordinator.requestDetails({ projectId: "p", assetIds: ["busy"], priority: "background", ownerId: "other" });
await settle(1);
loader.sync({ projectId: "p", displayed: [list[0]], order: list, currentId: "a0" });
const unrelated = loader.applySettings({ preloadFollowingAssets: 3, preloadNewAssets: false, maxCachedProvenanceDetails: 0 });
loader.sync({ projectId: "p", displayed: [list[0]], order: list, currentId: "a0" });
const queuedAfterUnrelated = coordinator._debugDetailState("p").queue.size;
const related = loader.applySettings({ preloadFollowingAssets: 1, preloadNewAssets: false, maxCachedProvenanceDetails: 0 });
loader.sync({ projectId: "p", displayed: [list[0]], order: list, currentId: "a0" });
await answerAll(); await settle();
console.log(JSON.stringify({ unrelated, queuedAfterUnrelated, related, calls: calls.map((c) => c.ids) }));
""")
    assert result["unrelated"] is False and result["related"] is True
    assert result["queuedAfterUnrelated"] == 4  # a0 plus its three-asset window, untouched
    assert result["calls"] == [["busy"], ["a0"], ["a1"]]


def test_destroy_and_an_aba_switch_through_no_project_drop_late_results():
    result = _run("""
const list = listOf("a");
serve(list);
const loader = makeLoader();
loader.listChanged({ projectId: "p", assets: list, version: "v1" });
loader.sync({ projectId: "p", displayed: [list[0]], order: list, currentId: "a" });
await settle(1);
loader.listChanged({ projectId: "", assets: [], version: "" });
loader.listChanged({ projectId: "p", assets: list, version: "v1" });
answer(0); await settle();
const afterAba = { status: loader.resolve(list[0]).status, changes: changes.length };
const doomed = makeLoader();
doomed.listChanged({ projectId: "p", assets: list, version: "v1" });
doomed.sync({ projectId: "p", displayed: [list[0]], order: list, currentId: "a" });
await settle(1);
doomed.destroy();
await answerAll(); await settle();
console.log(JSON.stringify({ afterAba, destroyed: doomed._debug().cached, changes: changes.length }));
""")
    assert result["afterAba"] == {"status": "loading", "changes": 0}
    assert result["destroyed"] == []
    assert result["changes"] == 0


def test_a_search_restart_stays_loading_until_its_list_refresh_settles():
    result = _run(PROJECTION + """
const list = listOf("a", "b");
serve(list, "v3");
server.revisions.b = "r-b-moved";
holdRefresh = true;
const projection = makeProjection();
projection.ensure({ projectId: "p", assets: list, version: "v1" });
await settle(1); await answerAll(); await settle();
// Keystrokes render and re-ensure on the old list while the refresh runs.
const during = [];
for (let i = 0; i < 3; i += 1) { during.push(projection.ensure({ projectId: "p", assets: list, version: "v1" })); await settle(1); }
const readsDuring = calls.length;
// The refreshed list moved "b": the restart builds only what moved and is ready.
server.revisions.b = "r-b2";
const fresh = [list[0], asset("b", { provenance_revision: "r-b2" })];
heldRefreshes.splice(0).forEach((resolve) => resolve()); await settle();
projection.ensure({ projectId: "p", assets: fresh, version: "v3" });
await settle(1); await answerAll(); await settle();
console.log(JSON.stringify({ during, readsDuring, final: projection.status, calls: calls.map((c) => c.ids) }));
""")
    assert result["during"] == ["loading", "loading", "loading"]
    assert result["readsDuring"] == 1
    assert result["final"] == "ready"
    assert result["calls"] == [["a", "b"], ["b"]]


def test_a_withdrawn_batch_never_makes_a_partial_projection_ready():
    result = _run(PROJECTION + """
const list = listOf(...Array.from({ length: 70 }, (_, i) => `a${i}`));
serve(list);
const projection = makeProjection();
projection.ensure({ projectId: "p", assets: list, version: "v1" });
await settle(1);
// Something else withdraws the queued second batch under this projection's owner.
coordinator.cancelDetails({ projectId: "p", ownerId: "s", priorities: ["search"] });
answer(0); await settle();
console.log(JSON.stringify({ status: projection.status, message: projection.message }));
""")
    assert result == {"status": "error", "message": "Metadata search was interrupted."}


def test_search_changes_are_announced_only_for_transitions_not_yet_rendered():
    result = _run(PROJECTION + """
const list = listOf("a", "b");
serve(list);
const projection = makeProjection();
projection.ensure({ projectId: "p", assets: list, version: "v1" });
await settle(1); await answerAll(); await settle();
const afterBuild = changesSearch.slice();
// A list that only removes an asset is satisfied synchronously: the caller already has it.
const returned = projection.ensure({ projectId: "p", assets: [list[0]], version: "v2" });
await settle();
console.log(JSON.stringify({ afterBuild, returned, after: changesSearch.slice() }));
""")
    assert result["afterBuild"] == ["ready"]
    assert result["returned"] == "ready"
    assert result["after"] == ["ready"]


def test_idle_withdraws_a_search_nobody_is_asking_for_and_keeps_what_it_adopted():
    result = _run(PROJECTION + """
const list = listOf(...Array.from({ length: 70 }, (_, i) => `a${i}`));
serve(list);
const projection = makeProjection();
projection.ensure({ projectId: "p", assets: list, version: "v1" });
await settle(1);
// The query is cleared while the first batch is in flight and the second is queued.
projection.idle();
const afterIdle = { status: projection.status, queued: coordinator._debugDetailState("p") };
await answerAll(); await settle();
const readsBeforeResume = calls.length;
projection.ensure({ projectId: "p", assets: list, version: "v1" });
await settle(1); await answerAll(); await settle();
console.log(JSON.stringify({ afterIdle: { status: afterIdle.status, queued: afterIdle.queued ? afterIdle.queued.queue.size : 0 },
    readsBeforeResume, resumed: calls.slice(readsBeforeResume).map((c) => c.ids.length), final: projection.status }));
""")
    assert result["afterIdle"] == {"status": "idle", "queued": 0}
    # The in-flight 64 are still adopted after idle; resuming reads only the 6 withdrawn.
    assert result["readsBeforeResume"] == 1
    assert result["resumed"] == [6]
    assert result["final"] == "ready"


def test_a_new_list_after_a_spent_restart_gets_its_own_restart():
    result = _run(PROJECTION + """
const list = listOf("a");
serve(list, "v3");
server.revisions.a = "moved-1";
const projection = makeProjection();
projection.ensure({ projectId: "p", assets: list, version: "v1" });
await settle(1); await answerAll(); await settle();
projection.ensure({ projectId: "p", assets: list, version: "v1" });
await settle(1); await answerAll(); await settle();
const spent = projection.status;
const next = [asset("a", { provenance_revision: "moved-1" })];
server.revisions.a = "moved-2";
projection.ensure({ projectId: "p", assets: next, version: "v3" });
await settle(1); await answerAll(); await settle();
console.log(JSON.stringify({ spent, afterNewList: projection.status, refreshes: listRefreshes.length }));
""")
    assert result["spent"] == "error"
    assert result["afterNewList"] == "loading"
    assert result["refreshes"] == 2
