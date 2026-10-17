"""Controls picker sources: what the lane and folder pickers list.

`web/js/controls_picker_sources.js` reads the Editor host's own project and
scene (plans/bridge-selections-controls.md, C): only while Controls is visible
and a picker of that kind is shown, through the shared coordinators, with the
graph's version gate for Reference (bridge-certified) and none for Drivers.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the picker sources")
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def test_picker_sources_read_only_when_wanted_and_follow_context_versions_and_folders():
    sources = (ROOT / "web/js/controls_picker_sources.js").as_uri()
    client = (ROOT / "web/js/api_client.js").as_uri()
    script = f"""
import assert from 'node:assert/strict';
globalThis.window = {{ setTimeout, clearTimeout }};
const client = await import({json.dumps(client)});
const {{ createControlsPickerSources }} = await import({json.dumps(sources)});
const V = n => `2026-10-16T10:00:${{String(n).padStart(2, "0")}}.000000`;
const reads = [], gates = [];
let served = V(1);
// The real coordinators over a stubbed fetch, so payloads carry their version.
globalThis.fetch = (url, options = {{}}) => new Promise((resolve, reject) => {{
  const kind = String(url).includes("bridge-drivers") ? "drv" : "ref";
  reads.push({{ kind, url: String(url), generation: options.headers?.["X-Sonder-" + (kind === "drv" ? "Driver" : "Reference") + "-Generation"] }});
  const version = served;
  gates.push({{ resolve: (payload) => resolve({{ ok: true, status: 200,
    headers: {{ get: (name) => name === "X-Sonder-Project-Modified-At" ? version : null }},
    json: async () => payload }}), reject }});
}});
const context = {{ projectId: "Proj", sceneId: "s1", selectionStart: 10, selectionEnd: 20, preContextFrames: 0, postContextFrames: 0 }};
let folders = ["Shots"];
const timers = [];
const sources = createControlsPickerSources({{
  getContext: () => context,
  apiURL: path => "/api" + path,
  getFolders: () => folders,
  setTimer: (fn) => {{ timers.push(fn); return timers.length; }},
  clearTimer: () => {{}},
}});
const flush = () => {{ while (timers.length) timers.shift()(); }};
const tick = () => new Promise(r => setTimeout(r, 0));
const run = async () => {{ for (let k = 0; k < 3; k++) {{ flush(); await tick(); }} }};
const settle = async (index, payload) => {{ gates[index].resolve(payload); for (let i = 0; i < 4; i++) await tick(); }};
let notices = 0;
sources.subscribe(() => notices++);
const log = {{}};

// Not visible, or nothing shown: no read.
sources.setInterest(["reference_lanes", "driver_lane", "asset_folder"]);
log.hiddenReads = reads.length;
sources.setVisible(true);
await tick();
log.visibleReads = reads.map(r => [r.kind, r.url]);
log.loading = sources.get("reference_lanes").status;
await settle(0, {{ references: [{{ lane_index: 0, lane_name: "Hero" }}], scene_name: "Saloon" }});
await settle(1, {{ drivers: [{{ lane_index: 0, lane_name: "Walk" }}, {{ lane_index: 1, lane_name: "" }}, {{ lane_index: "x" }}], scene_name: "Saloon" }});
log.lanes = sources.get("reference_lanes");
log.drivers = sources.get("driver_lane");
log.folders = sources.get("asset_folder");

// A window move re-reads Reference after the settle delay; Drivers read the
// full scene, so their URL is unchanged and nothing is read.
context.selectionStart = 12;
sources.contextChanged(); await run();
log.windowReads = reads.slice(2).map(r => r.kind);

// A scene switch: both re-read; the old scene's choices are never listed while it is in flight.
context.sceneId = "s2";
sources.contextChanged(); await run();
log.sceneReads = reads.slice(3).map(r => r.kind);
log.inFlight = [sources.get("reference_lanes").status, sources.get("reference_lanes").lanes];
// A late reply for the previous window is dropped.
await settle(2, {{ references: [{{ lane_index: 9, lane_name: "Stale" }}] }});
log.staleDropped = sources.get("reference_lanes").lanes === undefined;
await settle(3, {{ references: [{{ lane_index: 0, lane_name: "S2 lane" }}], scene_name: "Two" }});
await settle(4, {{ drivers: [], scene_name: "Two" }});
log.afterScene = sources.get("reference_lanes").lanes.map(l => l.lane_name);

// Versions: another project never re-reads; a certified bridge-unchanged edit
// keeps Reference but Drivers re-read; a flagged edit re-reads Reference.
client.rememberProjectVersion("Proj", V(1));
await run();
// The first notice of a version the held reads cannot vouch for re-reads both.
for (let i = 5; i < gates.length; i++) await settle(i, String(reads[i].url).includes("drivers") ? {{ drivers: [] }} : {{ references: [] }});
const before = reads.length;
client.rememberProjectVersion("Elsewhere", V(5));
await run();
log.otherProject = reads.length - before;
client.registerChangeCertificate({{ schema: 1, project_id: "canon", scene_id: "s2", base_modified_at: V(1),
  modified_at: V(2), prompt: false, bridge: false }}, {{ projectId: "Proj" }});
client.rememberProjectVersion("Proj", V(2));
await run();
log.certified = reads.slice(before).map(r => [r.kind, r.generation]);
for (let i = before; i < gates.length; i++) await settle(i, {{ drivers: [] }});
client.registerChangeCertificate({{ schema: 1, project_id: "canon", scene_id: "s2", base_modified_at: V(2),
  modified_at: V(3), prompt: false, bridge: true }}, {{ projectId: "Proj" }});
client.rememberProjectVersion("Proj", V(3));
await run();
log.flagged = reads.slice(before + 1).map(r => r.kind).sort();
const answer = async (from) => {{ for (let i = from; i < gates.length; i++) await settle(i, String(reads[i].url).includes("drivers") ? {{ drivers: [] }} : {{ references: [] }}); }};
await answer(before + 1);

// A version read survives a widget write that moves nothing, keeping its generation.
const beforeV4 = reads.length;
client.rememberProjectVersion("Proj", V(4));
sources.contextChanged();
await run();
log.versionThenWrite = reads.slice(beforeV4).map(r => [r.kind, r.generation]).sort();
await answer(beforeV4);

// A selection sweep that comes back to the held window reads nothing.
const beforeSweep = reads.length;
context.selectionStart = 30; sources.contextChanged(); timers.shift()();
context.selectionStart = 12; sources.contextChanged();
await run();
log.sweepBack = reads.length - beforeSweep;

// Folders come from the host's list; a change notifies only while wanted.
folders = ["Shots", "Takes/A", "", "Shots"];
const seen = notices;
sources.foldersChanged();
log.folderNotice = notices - seen;
log.foldersNow = sources.get("asset_folder").folders;
sources.setInterest(["reference_lanes"]);
sources.foldersChanged();
log.folderNoticeUnwanted = notices - seen;

// No scene: unavailable, no read.
context.sceneId = "";
const beforeNoScene = reads.length;
sources.contextChanged(); await run();
log.noScene = [reads.length - beforeNoScene, sources.get("reference_lanes").status];

// Hidden: nothing reads; destroy drops every reply.
sources.setVisible(false);
context.sceneId = "s3";
sources.contextChanged(); await run();
log.hiddenAfter = reads.length - beforeNoScene;
sources.destroy();
console.log(JSON.stringify(log));
"""
    result = _run(script)
    assert result["hiddenReads"] == 0
    assert result["visibleReads"] == [
        ["ref", "/api/sonder-editor/project/Proj/scenes/s1/bridge-references?selection_start=10&selection_end=20&pre_context_frames=0&post_context_frames=0"],
        ["drv", "/api/sonder-editor/project/Proj/scenes/s1/bridge-drivers"],
    ]
    assert result["loading"] == "loading"
    assert result["lanes"]["status"] == "ready" and result["lanes"]["sceneName"] == "Saloon"
    assert result["drivers"]["drivers"] == [{"laneIndex": 0, "name": "Walk"}, {"laneIndex": 1, "name": ""}]
    assert result["folders"] == {"status": "ready", "folders": ["Shots"]}
    assert result["windowReads"] == ["ref"]
    assert result["sceneReads"] == ["ref", "drv"]
    assert result["inFlight"] == ["loading", None]
    assert result["staleDropped"] is True
    assert result["afterScene"] == ["S2 lane"]
    assert result["otherProject"] == 0
    assert result["certified"] == [["drv", f"project-version:2026-10-16T10:00:02.000000"]], \
        "Reference kept by its certificate; Drivers ungated, in the graph's version wave"
    assert result["flagged"] == ["drv", "ref"]
    assert result["versionThenWrite"] == [
        ["drv", "project-version:2026-10-16T10:00:04.000000"],
        ["ref", "project-version:2026-10-16T10:00:04.000000"],
    ], "a following widget write must not cancel or re-generation a version read"
    assert result["sweepBack"] == 0
    assert result["folderNotice"] == 1 and result["foldersNow"] == ["Shots", "Takes/A"]
    assert result["folderNoticeUnwanted"] == 1
    assert result["noScene"] == [0, "unavailable"]
    assert result["hiddenAfter"] == 0


def test_picker_sources_owe_hidden_reads_keep_lists_while_revalidating_and_retry_on_the_timer():
    """Audit 2026-10-16: no stale lanes after hiding, no flicker on a window move,
    a failure keeps the last list and retries through the settle timer, and a
    widget write that moves nothing never re-arms a pending version read."""
    sources = (ROOT / "web/js/controls_picker_sources.js").as_uri()
    client = (ROOT / "web/js/api_client.js").as_uri()
    script = """
globalThis.window = { setTimeout, clearTimeout };
const client = await import(CLIENT);
const { createControlsPickerSources } = await import(SOURCES);
const V = n => `2026-10-16T11:00:${String(n).padStart(2, "0")}.000000`;
const reads = [];
let fail = false, served = V(1);
globalThis.fetch = (url) => {
  reads.push(String(url).includes("bridge-drivers") ? "drv" : "ref");
  const version = served, failing = fail;
  return Promise.resolve(failing ? { ok: false, status: 503, json: async () => ({}) } : { ok: true, status: 200,
    headers: { get: (name) => name === "X-Sonder-Project-Modified-At" ? version : null },
    json: async () => (String(url).includes("bridge-drivers")
      ? { drivers: [{ lane_index: 0, lane_name: "Walk" }] }
      : { references: [{ lane_index: 0, lane_name: "Hero" }] }) });
};
const context = { projectId: "P2", sceneId: "s1", selectionStart: 0, selectionEnd: 10 };
const timers = [];
const sources = createControlsPickerSources({
  getContext: () => context, apiURL: path => "/api" + path,
  setTimer: (fn) => { timers.push(fn); return timers.length; }, clearTimer: () => {},
});
const tick = () => new Promise(r => setTimeout(r, 0));
const run = async () => { for (let k = 0; k < 4; k++) { while (timers.length) timers.shift()(); await tick(); } };
const log = {};
sources.setInterest(["reference_lanes", "driver_lane"]);
sources.setVisible(true);
await run();
log.initial = reads.splice(0).sort();

// Hidden: a version owes a read, made once shown again.
sources.setVisible(false);
client.rememberProjectVersion("P2", V(1));
served = V(2); client.rememberProjectVersion("P2", V(2));
await run();
log.whileHidden = reads.splice(0).length;
sources.setVisible(true);
await run();
log.onShow = reads.splice(0).sort();

// A window move keeps the scene's lanes listed while the new read is in flight.
context.selectionStart = 5; sources.contextChanged();
for (let k = 0; k < 2; k++) { timers.shift()?.(); }
log.duringWindowRead = [sources.get("reference_lanes").status, sources.get("reference_lanes").lanes?.map(l => l.lane_name)];
await run(); reads.splice(0);

// A failed read keeps the last list and retries through the settle timer only.
fail = true;
sources.refresh("driver_lane"); await run();
log.failed = [sources.get("driver_lane").status, sources.get("driver_lane").drivers?.map(d => d.name)];
reads.splice(0);
sources.contextChanged(); timers.shift()();          // the context tick: reconcile arms a retry
log.retryArmed = [reads.length, timers.length];
fail = false; await run();
log.retried = [reads.splice(0), sources.get("driver_lane").status];

// A pending version read is not re-armed by a write that moves nothing.
served = V(3); client.rememberProjectVersion("P2", V(3));
const armed = timers.length;
sources.contextChanged(); timers.splice(armed, 1)[0]();  // run the context tick alone
log.rearmed = timers.length - armed;
await run();
console.log(JSON.stringify(log));
""".replace("CLIENT", json.dumps(client)).replace("SOURCES", json.dumps(sources))
    result = _run(script)
    assert result["initial"] == ["drv", "ref"]
    assert result["whileHidden"] == 0
    assert result["onShow"] == ["drv", "ref"], "a version seen while hidden is read on show"
    assert result["duringWindowRead"] == ["loading", ["Hero"]]
    assert result["failed"] == ["error", ["Walk"]]
    assert result["retryArmed"] == [0, 1], "an error retries on the settle timer, never at once"
    assert result["retried"] == [["drv"], "ready"]
    assert result["rearmed"] == 0
