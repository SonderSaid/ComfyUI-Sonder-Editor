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
    async refreshSummary() {{ return this.refreshResults.shift() ?? true; }}
    _invalidateModules() {{}}
    _reloadExpandedModuleIfNeeded() {{}}
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
    assert "completeBridgeExecutionTracking" in refresh_source
    assert refresh_source.index("completeBridgeExecutionTracking") < refresh_source.index(
        "pendingBridgeEditorNodeIds.clear();",
        refresh_source.index("completeBridgeExecutionTracking"),
    )

    destroy_start = controller.index("    destroy()")
    destroy_end = controller.index("    getElement()", destroy_start)
    assert '_resetBridgeAssetTracking({ clearKnown: true })' in controller[destroy_start:destroy_end]
    assert controller.count('_resetBridgeAssetTracking({ clearKnown: true })') == 3
    refresh_start = controller.index("    async refreshSummary(")
    refresh_end = controller.index("    async _refreshSummaryThenReloadModules(", refresh_start)
    refresh_source = controller[refresh_start:refresh_end]
    assert "syncedAssetPayload = syncResult?.payload ?? null;" in refresh_source
    assert refresh_source.index("this._rememberAssetIds(syncedAssetPayload, projectDir);") > (
        refresh_source.index("this.state.dormantSummary = await fetchJson(")
    )
    assert refresh_source.index("this._rememberAssetIds(syncedAssetPayload, projectDir);") < (
        refresh_source.index("refreshed = true;")
    )
