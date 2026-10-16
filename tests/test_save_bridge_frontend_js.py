"""Focused frontend contracts for Save Bridge restoration and arrival notices."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "web" / "js" / "extension.js"
CONTROLLER = ROOT / "web" / "js" / "editor_node_controller.js"


def _run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _between(source, start, end):
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


def test_bridge_folder_restore_is_widget_authoritative_and_latest_wins():
    source = EXTENSION.read_text(encoding="utf-8")
    normalization = _between(
        source,
        "function normalizeFolderValue",
        "function projectIdFromProjectValue",
    )
    folder_sync = _between(
        source,
        "function buildBridgeFolderOptions",
        "function installBridgeFolderPicker",
    )
    script = f"""
{normalization}

const pendingFolderRequests = [];
function getSaveBridgeProjectId() {{ return "project-1"; }}
function listProjectAssetFolders() {{
    return new Promise((resolve) => pendingFolderRequests.push(resolve));
}}
const document = {{
    createElement() {{ return {{ value: "" }}; }},
}};

{folder_sync}

const folderWidget = {{
    name: "target_folder",
    value: "",
    callback() {{ throw new Error("restore must not call the widget callback"); }},
    options: {{}},
}};
const datalist = {{
    innerHTML: "old",
    values: [],
    appendChild(option) {{ this.values.push(option.value); }},
}};
const input = {{ value: "stale-dom", placeholder: "" }};
const node = {{
    widgets: [folderWidget],
    _sonderBridgeFolderInput: input,
    _sonderBridgeFolderDatalist: datalist,
    _sonderBridgeFolderSyncToken: 0,
}};

const staleSync = syncBridgeTargetFolderWidget(node);
folderWidget.value = "Saved/Folder";
scheduleBridgeTargetFolderRestore(node, folderWidget, input);
await new Promise((resolve) => setTimeout(resolve, 0));

pendingFolderRequests[0](["Stale"]);
await staleSync;
const valueAfterStaleRequest = input.value;

pendingFolderRequests[1](["Saved/Folder", "Other"]);
await new Promise((resolve) => setTimeout(resolve, 0));

console.log(JSON.stringify({{
    valueAfterStaleRequest,
    restoredValue: input.value,
    widgetValue: folderWidget.value,
    choices: folderWidget.options.values,
    suggestions: datalist.values,
    root: buildBridgeFolderOptions([], ""),
    existing: buildBridgeFolderOptions(["Shots", "Other"], "Shots"),
    custom: buildBridgeFolderOptions(["Other"], "Custom/New"),
    requestCount: pendingFolderRequests.length,
}}));
"""

    result = _run_node(script)
    assert result == {
        "valueAfterStaleRequest": "Saved/Folder",
        "restoredValue": "Saved/Folder",
        "widgetValue": "Saved/Folder",
        "choices": ["", "Saved/Folder", "Other"],
        "suggestions": ["Saved/Folder", "Other"],
        "root": [""],
        "existing": ["", "Shots", "Other"],
        "custom": ["", "Custom/New", "Other"],
        "requestCount": 2,
    }


def test_bridge_folder_box_is_a_draft_and_the_widget_stays_the_authority():
    """The installed folder box never writes an old value back over a newer one.

    Typing edits a draft (no per-keystroke widget write); blur or Enter commits
    the normalized draft once; focus and blur without typing re-show the widget;
    an external write (Controls, Relay) shows unless a draft is open; and a
    suggestion sync shows the widget, so Root beats stale box text.
    """
    source = EXTENSION.read_text(encoding="utf-8")
    normalization = _between(source, "function normalizeFolderValue", "function projectIdFromProjectValue")
    picker = _between(source, "function buildBridgeFolderOptions", "const pendingBridgeEditorNodeIds")
    observer = (ROOT / "web" / "js" / "widget_value_observer.js").as_uri()
    script = f"""
import assert from 'node:assert/strict';
const {{ observeWidgetValue }} = await import({json.dumps(observer)});
{normalization}
const THEME = {{}}, FONT = {{}};
const chromeInputCss = () => "";
const setWidgetHidden = () => false, commitWidgetVisibility = () => {{}};
function style(el) {{ return el; }}
const app = {{ graph: {{ setDirtyCanvas() {{}} }} }};
let folderList = ["Shots"];
function getSaveBridgeProjectId() {{ return "project-1"; }}
async function listProjectAssetFolders() {{ return folderList; }}
class Element {{
  constructor(tag) {{ this.tagName = tag; this.value = ""; this.handlers = {{}}; this.children = []; this.style = {{}}; }}
  append(...c) {{ this.children.push(...c); }}
  appendChild(c) {{ this.children.push(c); }}
  setAttribute() {{}}
  set innerHTML(v) {{ this.children = []; }}
  addEventListener(name, fn) {{ (this.handlers[name] ||= []).push(fn); }}
  fire(name, event = {{}}) {{ for (const fn of this.handlers[name] || []) fn(event); }}
}}
const document = {{ createElement: (tag) => new Element(tag) }};
{picker}
const writes = [];
const folderWidget = {{ name: "target_folder", value: "Old", options: {{}},
  callback(value) {{ writes.push(value); }} }};
const node = {{ widgets: [folderWidget], addDOMWidget() {{ return {{}}; }} }};
installBridgeFolderPicker(node);
const input = node._sonderBridgeFolderInput;
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
await tick();
const log = {{ installed: input.value }};

// An external write while the box is idle shows at once.
folderWidget.value = "From/Controls"; folderWidget.callback("From/Controls");
log.external = input.value;

// Focus then blur without typing keeps the newer widget value.
input.fire("focus"); await tick();
folderWidget.value = "Newer"; folderWidget.callback("Newer");
input.fire("blur");
log.focusBlur = {{ widget: folderWidget.value, box: input.value }};

// Typing is a draft: no widget write per keystroke, external writes do not clobber it.
const before = writes.length;
input.fire("focus");
input.value = " typed\\\\new/ "; input.fire("input");
input.value = " typed\\\\new/folder/ "; input.fire("input");
log.keystrokeWrites = writes.length - before;
log.widgetWhileTyping = folderWidget.value;
folderWidget.value = "Remote"; folderWidget.callback("Remote");
log.draftKept = input.value;
await (async () => {{ input.fire("focus"); await tick(); }})();
log.syncKeptDraft = input.value;
// Enter (change) then blur commit once, normalized.
input.fire("change"); input.fire("blur");
log.committed = {{ widget: folderWidget.value, box: input.value,
  writes: writes.slice(before).filter((value) => value === "typed/new/folder").length }};

// Root beats stale box text: the widget is Root, the box shows stale text, a sync runs.
folderWidget.value = ""; input.value = "stale-text";
await syncBridgeTargetFolderWidget(node);
log.root = {{ widget: folderWidget.value, box: input.value }};

// Ctrl+Enter queues from the widget and suppresses the native change: the
// keydown commits first. Suggestions never carry the uncommitted draft.
input.fire("focus");
input.value = "Queued/Now"; input.fire("input");
await syncBridgeTargetFolderWidget(node);
log.draftNotOffered = folderWidget.options.values.includes("Queued/Now");
input.fire("keydown", {{ key: "Enter", ctrlKey: true }});
log.ctrlEnter = folderWidget.value;
// Escape abandons a draft and shows the widget again.
input.value = "Abandoned"; input.fire("input");
input.fire("keydown", {{ key: "Escape" }});
input.fire("blur");
log.escape = {{ widget: folderWidget.value, box: input.value }};
console.log(JSON.stringify(log));
"""
    result = _run_node(script)
    assert result == {
        "installed": "Old",
        "external": "From/Controls",
        "focusBlur": {"widget": "Newer", "box": "Newer"},
        "keystrokeWrites": 0,
        "widgetWhileTyping": "Newer",
        "draftKept": " typed\\new/folder/ ",
        "syncKeptDraft": " typed\\new/folder/ ",
        "committed": {"widget": "typed/new/folder", "box": "typed/new/folder", "writes": 1},
        "root": {"widget": "", "box": ""},
        "draftNotOffered": False,
        "ctrlEnter": "Queued/Now",
        "escape": {"widget": "Queued/Now", "box": "Queued/Now"},
    }


def test_bridge_asset_id_helpers_detect_equal_count_replacement():
    source = CONTROLLER.read_text(encoding="utf-8")
    helpers = _between(
        source,
        "function assetIdsFromPayload",
        "function isVideoLaneHidden",
    )
    script = f"""
{helpers}
const baseline = assetIdsFromPayload({{ assets: [
    {{ asset_id: "a" }},
    {{ asset_id: "b" }},
    {{ asset_id: "" }},
    {{ asset_id: "b" }},
] }});
const replacement = assetIdsFromPayload({{ assets: [
    {{ asset_id: "b" }},
    {{ asset_id: "c" }},
] }});
console.log(JSON.stringify({{
    baseline: [...baseline],
    replacement: newAssetIdsSince(baseline, replacement),
    none: newAssetIdsSince(replacement, replacement),
    invalid: newAssetIdsSince(null, replacement),
}}));
"""

    assert _run_node(script) == {
        "baseline": ["a", "b"],
        "replacement": ["c"],
        "none": [],
        "invalid": [],
    }


def test_bridge_baseline_capture_is_cached_or_one_retained_snapshot():
    source = CONTROLLER.read_text(encoding="utf-8")
    helpers = _between(
        source,
        "function assetIdsFromPayload",
        "function isVideoLaneHidden",
    )
    methods = _between(
        source,
        "    _rememberAssetIds(",
        "    handleNodeExecuted()",
    )
    script = f"""
{helpers}
let snapshotCalls = 0;
let resolveSnapshot;
function buildDormantAssetsUrl(projectDir) {{ return projectDir + "/assets"; }}
function fetchJson(_url, _signal) {{
    snapshotCalls += 1;
    return new Promise((resolve) => {{ resolveSnapshot = resolve; }});
}}
let wave = 0;
function allocateAssetRefreshWave() {{ wave += 1; return `wave-${{wave}}`; }}

class Harness {{
    constructor(moduleCache = {{}}) {{
        this._destroyed = false;
        this.state = {{ projectDir: "project-1" }};
        this.moduleCache = moduleCache;
        this._knownAssetIds = null;
        this._knownAssetIdsProjectDir = "";
        this._bridgeAssetSettleSession = null;
    }}
{methods}
}}

const cached = new Harness({{ assets: {{ assets: [{{ asset_id: "a" }}] }} }});
cached.beginBridgeExecutionTracking();
const cachedSession = cached._bridgeAssetSettleSession;
cached.beginBridgeExecutionTracking();
const cachedAggregated = cached._bridgeAssetSettleSession === cachedSession;

const uncached = new Harness();
uncached.beginBridgeExecutionTracking();
const pendingSession = uncached._bridgeAssetSettleSession;
uncached.beginBridgeExecutionTracking();
const pendingAggregated = uncached._bridgeAssetSettleSession === pendingSession;
resolveSnapshot({{ assets: [{{ asset_id: "before" }}] }});
await pendingSession.baselinePromise;
const capturedIds = [...pendingSession.baselineIds];
uncached.completeBridgeExecutionTracking();

console.log(JSON.stringify({{
    cachedIds: [...cachedSession.baselineIds],
    cachedAggregated,
    pendingAggregated,
    snapshotCalls,
    capturedIds,
    knownIdsAfterBaseline: uncached._knownAssetIds,
    reset: uncached._bridgeAssetSettleSession === null,
    abortedOnReset: pendingSession.baselineAborter.signal.aborted,
}}));
"""

    assert _run_node(script) == {
        "cachedIds": ["a"],
        "cachedAggregated": True,
        "pendingAggregated": True,
        "snapshotCalls": 1,
        "capturedIds": ["before"],
        "knownIdsAfterBaseline": None,
        "reset": True,
        "abortedOnReset": True,
    }


def test_bridge_settlement_notifies_once_and_only_after_success():
    source = CONTROLLER.read_text(encoding="utf-8")
    helpers = _between(
        source,
        "function assetIdsFromPayload",
        "function isVideoLaneHidden",
    )
    method = _between(
        source,
        "    async handleBridgeExecutionSettled(",
        "    async handleQueueExecutionSettled(",
    )
    script = f"""
{helpers}
const notices = [];
function notifyInfo(message) {{ notices.push(message); }}

class Harness {{
    constructor(baseline, current, refreshResults) {{
        this._destroyed = false;
        this.state = {{ projectDir: "project-1", dormantSummary: {{ queue_counts: {{}} }} }};
        this._bridgeAssetSettleSession = {{
            projectDir: "project-1",
            baselineIds: new Set(baseline),
            baselinePromise: null,
            arrivalAnnounced: false,
        }};
        this.current = new Set(current);
        this.refreshResults = [...refreshResults];
        this.fullscreenSession = null;
    }}
    syncStateFromWidgets() {{}}
    beginBridgeExecutionTracking() {{ throw new Error("session should already exist"); }}
    async _refreshSummaryThenReloadModules() {{
        const success = this.refreshResults.shift() ?? true;
        return {{ refreshed: true, assetResult: success ? {{ payload: {{}} }} : null }};
    }}
    _cachedAssetIds() {{ return new Set(this.current); }}
    render() {{}}
{method}
}}

const replacement = new Harness(["a", "b"], ["b", "c"], [true, true]);
await replacement.handleBridgeExecutionSettled();
replacement.current.add("d");
await replacement.handleBridgeExecutionSettled();

const failedThenSucceeded = new Harness(["a"], ["a", "b", "c"], [false, true]);
await failedThenSucceeded.handleBridgeExecutionSettled();
const announcedAfterFailure = failedThenSucceeded._bridgeAssetSettleSession.arrivalAnnounced;
await failedThenSucceeded.handleBridgeExecutionSettled();

const none = new Harness(["a"], ["a"], [true]);
await none.handleBridgeExecutionSettled();

console.log(JSON.stringify({{
    notices,
    replacementAnnounced: replacement._bridgeAssetSettleSession.arrivalAnnounced,
    announcedAfterFailure,
    succeededLater: failedThenSucceeded._bridgeAssetSettleSession.arrivalAnnounced,
    noneAnnounced: none._bridgeAssetSettleSession.arrivalAnnounced,
}}));
"""

    assert _run_node(script) == {
        "notices": ["Bridge asset saved", "2 bridge assets saved"],
        "replacementAnnounced": True,
        "announcedAfterFailure": False,
        "succeededLater": True,
        "noneAnnounced": False,
    }


def test_bridge_tracking_lifecycle_is_wired_to_extension_and_controller():
    extension = EXTENSION.read_text(encoding="utf-8")
    controller = CONTROLLER.read_text(encoding="utf-8")

    tracking_start = extension.index("function trackBridgeExecution")
    tracking_end = extension.index("function getTrackedBridgeEditorNodes", tracking_start)
    assert "beginBridgeExecutionTracking" in extension[tracking_start:tracking_end]
    assert "pendingBridgeEditorNodeIds.set(editorNode.id, projectDir);" in extension[
        tracking_start:tracking_end
    ]

    tracked_start = tracking_end
    tracked_end = extension.index("function schedulePostPromptRefresh", tracked_start)
    tracked_source = extension[tracked_start:tracked_end]
    assert "pendingBridgeEditorNodeIds.entries()" in tracked_source
    assert "state?.projectDir !== projectDir" in tracked_source

    refresh_start = extension.index("function schedulePostPromptRefresh")
    refresh_end = extension.index("app.registerExtension", refresh_start)
    refresh_source = extension[refresh_start:refresh_end]
    # The ladder's last rung ends tracking only for an editor that cannot hear
    # `run_finished`; otherwise the server's announcement does it.
    fallback = refresh_source[refresh_source.index("receivesRunFinished"):]
    assert fallback.index("completeBridgeExecutionTracking") < fallback.index(
        "pendingBridgeEditorNodeIds.delete(editorNode.id);"
    )

    finished = _between(extension, "async function handleRunFinished", "function schedulePostPromptRefresh")
    assert finished.index("handleBridgeExecutionSettled") < finished.index("completeBridgeExecutionTracking")
    assert "pendingBridgeEditorNodeIds.delete(editorNode.id);" in finished
    assert "controller.onRunFinished = () => handleRunFinished(node);" in extension

    destroy_start = controller.index("    destroy()")
    destroy_end = controller.index("    getElement()", destroy_start)
    assert '_resetBridgeAssetTracking({ clearKnown: true })' in controller[destroy_start:destroy_end]
    assert controller.count('_resetBridgeAssetTracking({ clearKnown: true })') == 3


def test_queue_only_settlement_refreshes_summary_without_assets():
    source = CONTROLLER.read_text(encoding="utf-8")
    method = _between(
        source,
        "    async handleQueueExecutionSettled(",
        "    /** Whether `run_finished` can reach",
    )
    script = f"""
class Harness {{
    constructor() {{
        this._destroyed = false;
        this.state = {{ projectDir: "project-1", dormantSummary: {{ queue_counts: {{ pending: 0, running: 0 }} }} }};
        this.summaryCalls = 0;
        this.assetCalls = 0;
        this.fullscreenSession = {{ refresh: (keys) => {{ this.fullscreenKeys = keys; }} }};
    }}
    syncStateFromWidgets() {{}}
    async refreshSummary() {{ this.summaryCalls += 1; return true; }}
    _invalidateModules() {{}}
    _reloadExpandedModuleIfNeeded() {{}}
    async _refreshAssets() {{ this.assetCalls += 1; }}
    render() {{}}
{method}
}}
const harness = new Harness();
const counts = await harness.handleQueueExecutionSettled();
console.log(JSON.stringify({{
    counts,
    summaryCalls: harness.summaryCalls,
    assetCalls: harness.assetCalls,
    fullscreenKeys: harness.fullscreenKeys,
}}));
"""
    assert _run_node(script) == {
        "counts": {"pending": 0, "running": 0},
        "summaryCalls": 1,
        "assetCalls": 0,
        "fullscreenKeys": ["queue"],
    }


def test_canvas_no_longer_resets_running_jobs_itself():
    controller = CONTROLLER.read_text(encoding="utf-8")
    extension = EXTENSION.read_text(encoding="utf-8")
    # The server returns a job its run left running; the browser never PUTs status.
    assert "_rollbackStaleRunningQueueJobs" not in controller
    assert "buildQueueJobUrl" not in controller
    assert "allowRollback" not in controller
    assert "allowRollback" not in extension


def test_run_finished_settles_a_tracked_bridge_editor_then_stops_tracking():
    extension = EXTENSION.read_text(encoding="utf-8")
    finished = _between(extension, "async function handleRunFinished", "function schedulePostPromptRefresh")
    script = f"""
const pendingBridgeEditorNodeIds = new Map();
const calls = [];
const controller = {{
    async handleBridgeExecutionSettled(options) {{ calls.push(["bridge", options.attemptIndex]); }},
    completeBridgeExecutionTracking() {{ calls.push(["complete"]); }},
    async handleQueueExecutionSettled() {{ calls.push(["queue"]); }},
}};
{finished}
pendingBridgeEditorNodeIds.set(7, "project-1");
await handleRunFinished({{ id: 7, _sonderController: controller }});
const afterBridge = [...pendingBridgeEditorNodeIds.keys()];
await handleRunFinished({{ id: 8, _sonderController: controller }});
console.log(JSON.stringify({{ calls, afterBridge }}));
"""
    assert _run_node(script) == {
        "calls": [["bridge", "run_finished"], ["complete"], ["queue"]],
        "afterBridge": [],
    }


def test_bridge_tracking_expires_only_for_the_run_it_was_scheduled_for():
    extension = EXTENSION.read_text(encoding="utf-8")
    expiry = _between(extension, "function scheduleBridgeTrackingExpiry", "function schedulePostPromptRefresh")
    script = f"""
const pendingBridgeEditorNodeIds = new Map();
const bridgeTrackingSerials = new Map();
const RUN_FINISHED_GRACE_MS = 60000;
const timers = [];
const window = {{ setTimeout(fn, ms) {{ timers.push([fn, ms]); }} }};
const completed = [];
const node = {{ id: 3, _sonderController: {{ completeBridgeExecutionTracking() {{ completed.push(3); }} }} }};
{expiry}
pendingBridgeEditorNodeIds.set(3, "p");
bridgeTrackingSerials.set(3, 1);
scheduleBridgeTrackingExpiry(node);
bridgeTrackingSerials.set(3, 2);           // a newer run started tracking the same editor
timers[0][0]();
const afterStale = [...pendingBridgeEditorNodeIds.keys()];
scheduleBridgeTrackingExpiry(node);
timers[1][0]();
console.log(JSON.stringify({{ delays: timers.map((t) => t[1]), afterStale, completed,
    after: [...pendingBridgeEditorNodeIds.keys()] }}));
"""
    assert _run_node(script) == {
        "delays": [60000, 60000],
        "afterStale": [3],
        "completed": [3],
        "after": [],
    }


def test_controller_forwards_run_finished_from_its_sync_socket():
    source = CONTROLLER.read_text(encoding="utf-8")
    sync = (ROOT / "web" / "js" / "cross_tab_sync.js").read_text(encoding="utf-8")
    assert 'event.type === "run_finished") handlers.onRunFinished?.(event)' in sync
    assert "onRunFinished: (event) => this._handleRunFinished(event)," in source
    method = _between(source, "    _handleRunFinished(event) {", "    async importFiles(")
    script = f"""
class Harness {{
    constructor() {{
        this._destroyed = false;
        this.state = {{ projectDir: "project-1" }};
        this.diag = [];
        this.seen = [];
        this.onRunFinished = (event) => this.seen.push(event.outcome);
    }}
    _recordDiagEvent(kind, payload) {{ this.diag.push([kind, payload]); }}
{method}
}}
const harness = new Harness();
harness._handleRunFinished({{ outcome: "error", returned_job_ids: ["job-1"] }});
harness._destroyed = true;
harness._handleRunFinished({{ outcome: "success" }});
console.log(JSON.stringify({{ seen: harness.seen, diag: harness.diag }}));
"""
    assert _run_node(script) == {
        "seen": ["error"],
        "diag": [["run_finished", {"outcome": "error", "returned_jobs": 1}]],
    }


def test_editor_execution_skips_assets_and_save_video_shares_one_read_wave():
    source = CONTROLLER.read_text(encoding="utf-8")
    methods = _between(
        source,
        "    handleNodeExecuted()",
        "    async handleBridgeExecutionSettled(",
    )
    script = f"""
let wave = 0;
function allocateAssetRefreshWave(reason) {{ wave += 1; return `${{reason}}:${{wave}}`; }}
class Harness {{
    constructor() {{
        this._destroyed = false;
        this.state = {{ projectDir: "p" }};
        this.controllerCalls = [];
        this.fullscreenCalls = [];
        this.fullscreenSession = {{
            refresh: (keys, options = {{}}) => this.fullscreenCalls.push({{ keys, options }}),
        }};
    }}
    syncStateFromWidgets() {{}}
    _refreshSummaryThenReloadModules(keys, options = {{}}) {{
        this.controllerCalls.push({{ keys, options }});
    }}
{methods}
}}
const harness = new Harness();
harness.handleNodeExecuted();
harness.handleSaveVideoExecuted();
console.log(JSON.stringify({{
    controllerCalls: harness.controllerCalls,
    fullscreenCalls: harness.fullscreenCalls,
}}));
"""
    result = _run_node(script)
    assert result["controllerCalls"][0] == {
        "keys": ["scene", "queue", "preview"],
        "options": {},
    }
    assert result["fullscreenCalls"][0] == {
        "keys": ["scenes", "queue"],
        "options": {},
    }
    controller_asset = result["controllerCalls"][1]["options"]["assetRefresh"]
    fullscreen_asset = result["fullscreenCalls"][1]["options"]["assetRefresh"]
    assert controller_asset == fullscreen_asset
    assert controller_asset["mode"] == "read"
    assert controller_asset["reason"] == "save_video_complete"


def test_audio_completion_notices_keep_headroom_routine_and_failures_sticky():
    source = EXTENSION.read_text(encoding='utf-8')
    notices = []
    marker = '                const descriptor = message?.sonder_video?.[0];'
    for section in source.split(marker)[1:]:
        notices.append(marker + section.split('                if (descriptor) {', 1)[0])
    assert len(notices) == 2
    for code in notices:
        result = _run_node(f"""
const calls = [];
const notifyInfo = (message, options) => calls.push({{tier:'info', message}});
const notifyWarning = (message, options) => calls.push({{tier:'warning', message}});
const callback = function(message) {{ {code} }};
callback.call({{id:7}}, {{sonder_video:[{{warnings:['Audio reduced by 2.8 dB'], alerts:['Saved video without audio']}}]}});
console.log(JSON.stringify(calls));
""")
        assert result == [{'tier': 'info', 'message': 'Audio reduced by 2.8 dB'},
                          {'tier': 'warning', 'message': 'Saved video without audio'}]


def test_timeline_export_notice_summary_precedes_reduction_without_double_periods():
    source = (ROOT / 'web/js/editor_widget.js').read_text(encoding='utf-8')
    method = _between(source, '    async _handleTimelineExportComplete(data)', '    // ── Render Queue')
    result = _run_node(f"""
const calls = [];
const notifyWarning = message => calls.push({{tier:'warning', message}});
const notifySuccess = message => calls.push({{tier:'success', message}});
class Harness {{
{method}
_hideExportPanel() {{}}
async _fetchAssets() {{}}
async _fetchScenes() {{}}
_inspectAssetInGallery() {{}}
}}
const editor = new Harness();
editor._exportNotif = {{resolve: (options) => calls.push({{tier:'resolved', ...options}})}};
await editor._handleTimelineExportComplete({{result:{{asset:{{name:'clip.mp4'}}}}, warnings:['Audio reduced by 2.8 dB.']}});
await editor._handleTimelineExportComplete({{result:{{asset:{{name:'quiet.mp4'}}}}, warnings:[]}});
await editor._handleTimelineExportComplete({{alerts:['Timeline audio could not be placed']}});
console.log(JSON.stringify(calls));
""")
    assert result[0] == {'tier': 'resolved', 'message': 'Exported clip.mp4. Audio reduced by 2.8 dB.'}
    assert result[1] == {'tier': 'success', 'message': 'Exported quiet.mp4.'}
    assert result[2] == {'tier': 'warning', 'message': 'Timeline audio could not be placed'}
    assert result[3] == {'tier': 'success', 'message': 'Export complete.'}
