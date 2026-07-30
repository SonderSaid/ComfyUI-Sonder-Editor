import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
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


def _summary_methods():
    source = CONTROLLER.read_text(encoding="utf-8")
    return _between(
        source,
        "    _currentSummarySignature()",
        "    async _refreshAssets(",
    )


def test_summary_signature_change_queues_one_followup_and_rejects_old_selection():
    methods = _summary_methods()
    script = f"""
function createDeferred() {{
    let resolve;
    const promise = new Promise((onResolve) => {{ resolve = onResolve; }});
    return {{ promise, resolve }};
}}
function buildDormantSummaryUrl(state) {{
    return `summary?scene=${{state.sceneId}}&start=${{state.selectionStart}}&end=${{state.selectionEnd}}&pre=${{state.preContextFrames}}&post=${{state.postContextFrames}}`;
}}
const requests = [];
function fetchJson(url) {{
    return new Promise((resolve) => requests.push({{ url, resolve }}));
}}
class Harness {{
    constructor() {{
        this._destroyed = false;
        this._projectGeneration = 1;
        this._summaryRefreshSeq = 0;
        this._summaryRefreshActive = null;
        this._summaryRefreshPending = null;
        this.state = {{
            projectDir: "p",
            sceneId: "s",
            selectionStart: 0,
            selectionEnd: 5,
            preContextFrames: 1,
            postContextFrames: 2,
            dormantSummary: null,
        }};
        this.appliedInvalidations = [];
    }}
    syncStateFromWidgets() {{}}
    _recordDiagEvent() {{}}
    _getWidgetValue() {{ return "s"; }}
    _setWidgetValue() {{}}
    _invalidateModules(keys) {{ this.appliedInvalidations.push([...keys]); }}
    _reloadExpandedModuleIfNeeded() {{}}
    render() {{}}
{methods}
}}
const harness = new Harness();
const first = harness.refreshSummary({{ invalidationKeys: ["assets"] }});
harness.state.selectionStart = 10;
harness.state.selectionEnd = 20;
const second = harness.refreshSummary({{ invalidationKeys: ["preview"] }});
const callsBefore = requests.length;
requests[0].resolve({{ marker: "old", active_scene: null }});
await new Promise((resolve) => setTimeout(resolve, 0));
const callsAfterOld = requests.length;
requests[1].resolve({{ marker: "new", active_scene: null }});
const results = await Promise.all([first, second]);
console.log(JSON.stringify({{
    callsBefore,
    callsAfterOld,
    urls: requests.map((request) => request.url),
    results,
    marker: harness.state.dormantSummary.marker,
    appliedInvalidations: harness.appliedInvalidations,
}}));
"""
    result = _run_node(script)
    assert result["callsBefore"] == 1
    assert result["callsAfterOld"] == 2
    assert "start=0" in result["urls"][0]
    assert "start=10" in result["urls"][1]
    assert result["results"] == [False, True]
    assert result["marker"] == "new"
    assert result["appliedInvalidations"] == [["preview"]]


def test_identical_summary_callers_join_and_merge_invalidation_keys():
    methods = _summary_methods()
    script = f"""
function createDeferred() {{
    let resolve;
    const promise = new Promise((onResolve) => {{ resolve = onResolve; }});
    return {{ promise, resolve }};
}}
function buildDormantSummaryUrl(state) {{
    return `summary?scene=${{state.sceneId}}&start=${{state.selectionStart}}&end=${{state.selectionEnd}}&pre=${{state.preContextFrames}}&post=${{state.postContextFrames}}`;
}}
const requests = [];
function fetchJson(url) {{
    return new Promise((resolve) => requests.push({{ url, resolve }}));
}}
class Harness {{
    constructor() {{
        this._destroyed = false;
        this._projectGeneration = 4;
        this._summaryRefreshSeq = 0;
        this._summaryRefreshActive = null;
        this._summaryRefreshPending = null;
        this.state = {{
            projectDir: "p",
            sceneId: "s",
            selectionStart: 2,
            selectionEnd: 7,
            preContextFrames: 0,
            postContextFrames: 0,
            dormantSummary: null,
        }};
        this.invalidations = [];
        this.reloads = [];
    }}
    syncStateFromWidgets() {{}}
    _recordDiagEvent() {{}}
    _getWidgetValue() {{ return "s"; }}
    _setWidgetValue() {{}}
    _invalidateModules(keys) {{ this.invalidations.push([...keys]); }}
    _reloadExpandedModuleIfNeeded(keys, options) {{ this.reloads.push({{ keys, options }}); }}
    render() {{}}
{methods}
}}
const harness = new Harness();
const first = harness.refreshSummary({{ invalidationKeys: ["queue"] }});
const joined = harness.refreshSummary({{
    invalidationKeys: ["preview"],
    skipModuleIds: ["assets"],
}});
requests[0].resolve({{ marker: "joined", active_scene: null }});
const results = await Promise.all([first, joined]);
console.log(JSON.stringify({{
    requestCount: requests.length,
    results,
    invalidations: harness.invalidations,
    reloads: harness.reloads,
}}));
"""
    result = _run_node(script)
    assert result["requestCount"] == 1
    assert result["results"] == [True, True]
    assert result["invalidations"] == [["queue", "preview"]]
    assert result["reloads"][0]["options"]["skipModuleIds"] == ["assets"]
