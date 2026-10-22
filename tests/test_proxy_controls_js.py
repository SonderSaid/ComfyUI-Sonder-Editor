"""Proxy controls (Phase 3 of the proxy plan): Settings → Proxies, its maintenance
dialog, the gallery's proxy gestures and the progress of user-started builds.

* The dialog previews what Regenerate and Delete would touch per usage category;
  Delete starts with Unused only, trashed proxies always go, Regenerate never counts
  a video that plays as is.
* The store keeps each project's build-queue summary by epoch, so an event read
  before a request can never end that request's progress.
* User-started builds show one progress toast per project that says when builds
  wait for ComfyUI, offers Cancel and always ends; failures name what failed and
  carry the server's code as detail.
* Settings offers exactly the server's presets and timings.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "web" / "js"
DIALOG = (JS / "proxy_maintenance_dialog.js").as_uri()
ACTIONS = (JS / "proxy_actions.js").as_uri()
DISPLAY = (JS / "media_display_source.js").as_uri()
NOTIFY = (JS / "editor_notifications.js").as_uri()


def _run_node(script: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    completed = subprocess.run([node, "--input-type=module", "-e", script],
                               capture_output=True, text=True, encoding="utf-8")
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout.strip().splitlines()[-1])


OVERVIEW = {
    "items": {
        "s1": {"category": "scenes", "decision": "undecodable", "state": "ready", "bytes": 1000},
        "s2": {"category": "scenes", "decision": "undecodable", "state": "waiting_idle"},
        "s3": {"category": "scenes", "decision": "decodable", "state": "none"},
        "r1": {"category": "references", "decision": "manual", "state": "ready", "bytes": 2048},
        "r2": {"category": "references", "decision": "pending", "state": "none"},
        "u1": {"category": "unused", "decision": "undecodable", "state": "ready", "bytes": 3 * 1024 * 1024},
        "u2": {"category": "unused", "decision": "runtime", "state": "failed"},
        "t1": {"category": "trashed", "decision": "undecodable", "state": "ready", "bytes": 500},
    },
}


def test_the_dialog_counts_what_each_category_would_touch():
    result = _run_node(f"""
globalThis.window = {{}};
const d = await import({json.dumps(DIALOG)});
const overview = {json.dumps(OVERVIEW)};
console.log(JSON.stringify({{
  summary: d.proxyCategorySummary(overview),
  deleteDefault: d.defaultProxyCategories("delete"),
  regenerateDefault: d.defaultProxyCategories("regenerate"),
  deleteUnused: d.proxyActionTotals(overview, "delete", ["unused"]),
  deleteNone: d.proxyActionTotals(overview, "delete", []),
  regenerateAll: d.proxyActionTotals(overview, "regenerate", ["scenes", "references", "unused"]),
  regenerateNone: d.proxyActionTotals(overview, "regenerate", []),
  bytes: [d.formatProxyBytes(0), d.formatProxyBytes(2048), d.formatProxyBytes(3 * 1024 * 1024)],
}}));
""")
    assert result["summary"]["scenes"] == {"proxies": 1, "bytes": 1000, "missing": 1, "unchecked": 0}  # s3 plays as is
    assert result["summary"]["unused"] == {"proxies": 1, "bytes": 3 * 1024 * 1024, "missing": 1, "unchecked": 0}
    # An undecided video is counted: Regenerate has it checked, as the server does.
    assert result["summary"]["references"]["unchecked"] == 1
    assert result["deleteDefault"] == ["unused"]
    assert result["regenerateDefault"] == ["scenes", "references", "unused"]
    # Trashed proxies go with every Delete, checked or not.
    assert result["deleteUnused"] == {"proxies": 2, "bytes": 3 * 1024 * 1024 + 500, "missing": 0, "unchecked": 0,
                                      "any": True}
    # Nothing checked: nothing to send (the server refuses an empty category list).
    assert result["deleteNone"]["any"] is False
    # Regenerate rebuilds three, builds two missing, never touches trashed.
    assert result["regenerateAll"] == {"proxies": 3, "bytes": 1000 + 2048 + 3 * 1024 * 1024, "missing": 2,
                                       "unchecked": 1, "any": True}
    assert result["regenerateNone"]["any"] is False
    assert result["bytes"] == ["0 B", "2 KB", "3.0 MB"]


def test_the_store_keeps_the_newest_queue_summary_per_project():
    result = _run_node(f"""
globalThis.requestAnimationFrame = (cb) => setTimeout(() => cb(0), 0);
const d = await import({json.dumps(DISPLAY)});
const heard = [];
d.proxyStore.subscribe((changes) => heard.push([...changes.keys()].sort()));
const first = d.proxyStore.ingestQueue("P", {{ user_pending: 2 }}, 10);
const stale = d.proxyStore.ingestQueue("P", {{ user_pending: 0 }}, 9);
const other = d.proxyStore.ingestQueue("Q", {{ user_pending: 5 }}, 1);
await new Promise((r) => setTimeout(r, 5));
console.log(JSON.stringify({{ first, stale, other, p: d.proxyStore.queue("P"), q: d.proxyStore.queue("Q"),
  heard, assets: [...d.changedAssetIds(new Map([["P", new Set()]]), "P")] }}));
""")
    assert [result["first"], result["stale"], result["other"]] == [True, False, True]
    assert result["p"] == {"user_pending": 2, "epoch": 10}
    assert result["q"]["user_pending"] == 5
    # A queue change reaches listeners as its project, with no asset ids (so the
    # viewport and gallery, which react only to their assets, stay still).
    assert result["heard"] == [["P", "Q"]] and result["assets"] == []


_ACTIONS_PRELUDE = f"""
globalThis.requestAnimationFrame = (cb) => setTimeout(() => cb(0), 0);
const posts = [];
let reply = () => ({{ ok: true, status: 200, json: async () => ({{}}) }});
globalThis.window = {{ comfyAPI: {{ api: {{ api: {{ apiURL: (p) => p }} }} }} }};
globalThis.fetch = async (url, init = {{}}) => {{ posts.push([String(url), init.method || "GET", init.body ? JSON.parse(init.body) : null]);
  return reply(String(url), init); }};
const d = await import({json.dumps(DISPLAY)});
const n = await import({json.dumps(NOTIFY)});
const a = await import({json.dumps(ACTIONS)});
const tick = () => new Promise((r) => setTimeout(r, 5));
"""


def test_user_builds_show_one_progress_that_waits_cancels_and_ends():
    result = _run_node(_ACTIONS_PRELUDE + r"""
const seen = [];
const unsub = n.subscribe((snapshot) => {
  const list = Array.isArray(snapshot) ? snapshot : (snapshot?.notifications || snapshot?.items || []);
  for (const t of list) if (String(t.source || t._key || "").includes("proxy-builds")) seen.push([t.tier, t.message, t.progress?.current ?? null, t.progress?.total ?? null, (t.actions || []).map((x) => x.label)]);
});
reply = (url) => url.endsWith("/proxies/build")
  ? { ok: true, status: 200, json: async () => ({ queued: 2, queue: { user_pending: 2, pending: 2, waiting_idle: 2, building: "", epoch: 100 } }) }
  : { ok: true, status: 200, json: async () => ({ items: {}, queue: { user_pending: 2 }, epoch: 1 }) };
await a.buildProxies("P", ["x", "y"]);
await tick();
// An event read before the request (older epoch) says nothing is pending: ignored.
d.proxyStore.ingestQueue("P", { user_pending: 0, pending: 0, waiting_idle: 0, building: "" }, 99);
await tick();
// One built, one building.
d.proxyStore.ingestItems("P", [{ asset_id: "x", decision: "undecodable", state: "ready", rev: "1111111111111111", epoch: 101 }]);
d.proxyStore.ingestQueue("P", { user_pending: 1, pending: 0, waiting_idle: 0, building: "y" }, 101);
await tick();
d.proxyStore.ingestItems("P", [{ asset_id: "y", decision: "undecodable", state: "ready", rev: "2222222222222222", epoch: 102 }]);
d.proxyStore.ingestQueue("P", { user_pending: 0, pending: 0, waiting_idle: 0, building: "" }, 102);
await tick();
// A second run, cancelled.
reply = (url) => url.endsWith("/proxies/build")
  ? { ok: true, status: 200, json: async () => ({ queued: 1, queue: { user_pending: 1, pending: 1, waiting_idle: 0, building: "z", epoch: 200 } }) }
  : { ok: true, status: 200, json: async () => ({ cancelled: 1, queue: { user_pending: 0, epoch: 201 } }) };
await a.buildProxies("P", ["z"], { now: true });
await tick();
await a.cancelProxyBuilds("P");
await tick();
// Regenerate with nothing to do shows no progress.
reply = () => ({ ok: true, status: 200, json: async () => ({ queued: 0, queue: { user_pending: 0, epoch: 300 } }) });
await a.regenerateProxies("P", ["unused"]);
await tick();
a._resetProxyActions();
unsub();
const bodies = posts.filter(([u]) => /proxies\/(build|cancel|regenerate)$/.test(u)).map(([u, , b]) => [u.split("/").pop(), b]);
console.log(JSON.stringify({ seen, bodies }));
""")
    seen = result["seen"]
    progress = [entry for entry in seen if entry[0] == "progress"]
    assert progress and progress[0][1] == "Waiting for ComfyUI to be idle"
    # The stale summary never ended it: it ended only after both were built.
    first_done = next(i for i, entry in enumerate(seen) if entry[0] == "success")
    assert seen[first_done][1] == "2 proxies built"
    # A finished toast keeps no Cancel (it would cancel builds started later).
    assert seen[first_done][4] == [] and progress[0][4] == ["Cancel"]
    assert any(entry[1] == "1 of 2 proxies built" for entry in seen[:first_done])
    assert any(entry[0] == "info" and entry[1] == "Proxy builds cancelled" and entry[4] == [] for entry in seen)
    assert result["bodies"] == [
        ["build", {"asset_ids": ["x", "y"], "mode": "queue", "rebuild": False}],
        ["build", {"asset_ids": ["z"], "mode": "now", "rebuild": False}],
        ["cancel", {}],
        ["regenerate", {"categories": ["unused"]}],
    ]


def test_gallery_gestures_route_and_failures_are_written_sentences():
    result = _run_node(_ACTIONS_PRELUDE + r"""
const errors = [];
n.subscribe((snapshot) => {
  const list = Array.isArray(snapshot) ? snapshot : (snapshot?.notifications || snapshot?.items || []);
  for (const t of list) if (t.tier === "error") errors.push([t.message, t.detail]);
});
reply = () => ({ ok: true, status: 200, json: async () => ({ queued: 0, removed: { count: 1, bytes: 10 }, queue: { user_pending: 0, epoch: 5 } }) });
for (const action of ["create", "rebuild", "now", "remove", "bogus"]) await a.runProxyAction("P", action, ["v1", "v1"]);
reply = () => ({ ok: false, status: 400, json: async () => ({ error: "Only video assets of this project can have proxies", code: "not_a_video_asset" }) });
const outcome = await a.runProxyAction("P", "create", ["img"]);
await tick();
a._resetProxyActions();
const bodies = posts.filter(([u]) => /proxies\/(build|delete)$/.test(u)).map(([u, , b]) => [u.split("/").pop(), b]);
console.log(JSON.stringify({ bodies, outcome, errors }));
""")
    assert result["bodies"][:4] == [
        ["build", {"asset_ids": ["v1"], "mode": "queue", "rebuild": False}],
        ["build", {"asset_ids": ["v1"], "mode": "queue", "rebuild": True}],
        ["build", {"asset_ids": ["v1"], "mode": "now", "rebuild": False}],
        ["delete", {"asset_ids": ["v1"]}],
    ]
    assert len(result["bodies"]) == 5  # "bogus" sent nothing; the failing create did
    assert result["outcome"] is None
    assert ["The proxy could not be queued.", "not_a_video_asset"] in result["errors"]


def test_settings_offers_exactly_the_servers_presets_and_timings():
    panel = (JS / "editor_settings_panel.js").read_text(encoding="utf-8")
    server = (ROOT / "server" / "proxy_media.py").read_text(encoding="utf-8")
    presets = re.search(r"PROXY_PRESET_VALUES = \[(.*?)\]", panel).group(1)
    server_presets = re.search(r"PROXY_PRESETS = \((.*?)\)", server).group(1)
    assert re.findall(r'"([^"]+)"', presets) == re.findall(r'"([^"]+)"', server_presets)
    timings = re.findall(r'\{ value: "(idle|now|manual)"', panel)
    server_timings = re.search(r'TIMING_IDLE, TIMING_NOW, TIMING_MANUAL = (.*)', server).group(1)
    assert timings == re.findall(r'"([^"]+)"', server_timings)
    # The section is reachable by the toast's Settings button.
    assert '"proxies"' in panel and "scrollToSection" in panel


def test_the_toast_says_what_became_of_its_videos_and_gives_up_on_a_lost_project():
    result = _run_node(_ACTIONS_PRELUDE + r"""
const ends = [];
n.subscribe((list) => { for (const t of list) if (String(t.source || "").includes("proxy-builds") && t.tier !== "progress")
  if (!ends.some((e) => e[1] === t.message)) ends.push([t.tier, t.message]); });
const intervals = [];
globalThis.setInterval = (fn) => { intervals.push(fn); return intervals.length; };
globalThis.clearInterval = () => {};
const start = (ids, epoch) => { reply = () => ({ ok: true, status: 200, json: async () => ({ queued: ids.length,
  queue: { user_pending: ids.length, pending: ids.length, waiting_idle: 0, building: "", epoch } }) }); return a.buildProxies("P", ids); };
// One built, one failed.
await start(["a", "b"], 10);
d.proxyStore.ingestItems("P", [{ asset_id: "a", decision: "undecodable", state: "ready", rev: "aaaaaaaaaaaaaaaa", epoch: 11 },
                               { asset_id: "b", decision: "undecodable", state: "failed", rev: "", epoch: 11 }]);
d.proxyStore.ingestQueue("P", { user_pending: 0, pending: 0, waiting_idle: 0, building: "" }, 11);
await tick(); await tick();
// Cancelled from another tab: nothing built.
await start(["c"], 20);
d.proxyStore.ingestQueue("P", { user_pending: 0, pending: 0, waiting_idle: 0, building: "" }, 21);
await tick(); await tick();
// The project disappears: the reads fail and the toast ends anyway.
await start(["e"], 30);
reply = () => ({ ok: false, status: 404, json: async () => ({ code: "project_not_found" }) });
// Polls run 5 s apart, past the 2 s refetch dedupe: advance the clock between them.
const realNow = Date.now; let skew = 0; Date.now = () => realNow() + skew;
for (let i = 0; i < 6; i++) { skew += 5000; intervals.at(-1)(); await tick(); }
a._resetProxyActions();
console.log(JSON.stringify({ ends }));
""")
    assert result["ends"] == [
        ["warning", "1 of 2 proxies built, 1 failed"],
        ["info", "No proxies built: they were current already, or the builds were stopped"],
        ["info", "Proxy progress is unavailable: the project could not be read"],
    ]


def test_a_read_that_changes_nothing_but_its_epoch_is_not_a_change():
    result = _run_node(f"""
globalThis.requestAnimationFrame = (cb) => setTimeout(() => cb(0), 0);
const d = await import({json.dumps(DISPLAY)});
const heard = [];
d.proxyStore.subscribe((changes) => heard.push([...d.changedAssetIds(changes, "P")]));
const item = (epoch) => ({{ asset_id: "a", decision: "undecodable", state: "ready", rev: "r1", epoch }});
d.proxyStore.ingestItems("P", [item(1)]);
d.proxyStore.ingestQueue("P", {{ user_pending: 0, pending: 0 }}, 1);
await new Promise((r) => setTimeout(r, 5));
const first = heard.length;
// Settings re-reads while open: same state, newer epoch. No listener may hear it,
// or a listener that re-reads would loop.
d.proxyStore.ingestItems("P", [item(2)]);
d.proxyStore.ingestQueue("P", {{ user_pending: 0, pending: 0 }}, 2);
await new Promise((r) => setTimeout(r, 300));
console.log(JSON.stringify({{ first, after: heard.length, kept: d.proxyStore.get("P", "a").epoch }}));
""")
    assert result == {"first": 1, "after": 1, "kept": 2}
