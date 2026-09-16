"""Exercise the real export polling/status UI without importing a DOM library."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("status,retained", [("failed", True), ("cancelled", True), ("cancelled", False)])
def test_export_poll_reports_retained_files_and_restores_controls(status, retained):
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    data = {"status": status, "phase": status, "error": "Registration could not be confirmed."}
    if retained:
        data.update(retained_path="media/Exports/finished.mp4",
                    retained_paths=["media/Exports/finished.mp4", "media/paired_audio.wav"])
    script = """
        import assert from 'node:assert/strict';
        let poll;
        globalThis.window = {
            comfyAPI: {api: {api: {apiURL: path => path}}},
            localStorage: {getItem: () => null},
            setTimeout: fn => { poll = fn; return 1; }, clearTimeout: () => {},
        };
        globalThis.location = {href:'http://test/'};
        const {EditorWidget} = await import(WIDGET_URL);
        const w = Object.create(EditorWidget.prototype);
        const notices = [];
        Object.assign(w, {
            _exportJobId:'job', _exportPanelToken:1, _projectDirName:()=>'project',
            _exportNotif:{update:()=>{},resolve:value=>notices.push(value),dismiss:()=>notices.push('dismiss')},
        });
        const ui = {controls:[{disabled:true}], exportBtn:{disabled:true},closeBtn:{disabled:true},
            cancelBtn:{},progressEl:{style:{display:'block'},textContent:''},errorEl:{textContent:''}};
        const data = PAYLOAD;
        globalThis.fetch = async () => ({ok:true,json:async()=>data});
        w._pollTimelineExport(ui);
        await poll();
        assert.equal(w._exportJobId,'');
        assert.equal(w._exportNotif,null);
        assert.equal(ui.controls[0].disabled,false);
        assert.equal(ui.exportBtn.disabled,false);
        if (data.retained_path) {
            for (const path of data.retained_paths) assert.ok(ui.errorEl.textContent.includes(path));
            assert.match(ui.errorEl.textContent,/Refresh the gallery/);
            assert.match(ui.errorEl.textContent,/provenance.*cannot be recovered/);
            assert.equal(ui.requestError,ui.errorEl.textContent);
            assert.equal(notices[0].tier,data.status==='cancelled'?'warning':'error');
            assert.equal(notices[0].message,ui.errorEl.textContent);
        } else {
            assert.equal(ui.errorEl.textContent,'');
            assert.deepEqual(notices,['dismiss']);
        }
    """.replace("WIDGET_URL", json.dumps((ROOT / "web/js/editor_widget.js").as_uri())).replace("PAYLOAD", json.dumps(data))
    result = subprocess.run([node, "--input-type=module", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr or result.stdout


def test_cancel_during_pending_start_keeps_polling_retained_outcome():
    # Reuse the widget's real host import/stubs, including gesture attribution.
    from test_project_mutation_queue import _run_gesture_node
    _run_gesture_node(r"""
        const w = makeWidget();
        let releaseStart, poll;
        const calls = [];
        window.setTimeout = fn => { poll = fn; return 1; };
        window.clearTimeout = () => {};
        Object.assign(w, {_exportPanelToken:1, _exportJobId:'', _projectDirName:()=> 'project'});
        const ui = {controls:[{disabled:true}], exportBtn:{disabled:true}, closeBtn:{disabled:true},
            cancelBtn:{}, progressEl:{style:{},textContent:''}, errorEl:{textContent:''}};
        globalThis.fetch = async (url, options = {}) => {
            calls.push([url, options.method || 'GET']);
            if (url.endsWith('/render_timeline')) return await new Promise(resolve => {releaseStart=resolve;});
            return {ok:true,json:async()=>({job_id:'job',status:'cancelled',retained_path:'media/Exports/finished.mp4'})};
        };
        const start = w._startTimelineExportWithinGesture({}, {}, ui);
        await w._cancelTimelineExportWithinGesture({}, ui.progressEl);
        releaseStart({ok:true,json:async()=>({job_id:'job',status:'running'})});
        await start;
        assert.equal(w._exportJobId,'job');
        assert.equal(ui.exportBtn.disabled,true);
        assert.equal(typeof poll,'function');
        await poll();
        assert.equal(calls.length,3);
        assert.equal(calls[2][1],'GET');
        assert.match(ui.errorEl.textContent,/media\/Exports\/finished.mp4/);
        assert.match(ui.errorEl.textContent,/cancelled/);
        assert.equal(w._exportJobId,'');
        assert.equal(ui.exportBtn.disabled,false);
    """)
