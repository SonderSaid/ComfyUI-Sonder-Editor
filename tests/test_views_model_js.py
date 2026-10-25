"""Output Views 3A: the pure pane model (views_model.js) and its settings record."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODEL = (ROOT / "web" / "js" / "views_model.js").as_uri()


def run(body: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    script = f"import assert from 'node:assert/strict';\nconst M = await import({json.dumps(MODEL)});\nconst out = {{}};\n{body}\nconsole.log('RESULT='+JSON.stringify(out));"
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(next(l for l in result.stdout.splitlines() if l.startswith("RESULT="))[7:])


def test_layout_normalizer_default_composite_rule_and_lru():
    out = run("""
out.fresh = M.normalizeLayout(null);
out.isDefault = [M.isDefaultLayout(null), M.isDefaultLayout({layout:'1', panes:[{source:{kind:'composite'}}]}),
    M.isDefaultLayout({layout:'2c'}), M.isDefaultLayout({layout:'1', panes:[{}, {source:{kind:'view', viewId:'v1'}}]})];
// Junk is read tolerantly; Composite sits in one pane at most.
out.junk = M.normalizeLayout({ts:5, layout:'9', panes:[{source:{kind:'composite'}},{source:{kind:'composite'}},
    {source:{kind:'driver', index:2}, item:3, fit:'cover', zoom:{scale:2,x:5,y:-5}},{source:{kind:'view', viewId:'bad id'}, zoom:{scale:99,x:0,y:0}}]});
// The map keeps the newest 64 real records, never the default one.
const map = {};
for (let i = 0; i < 70; i++) map[`c:${i}`] = {ts: i + 1, layout: '2c'};
map['c:default'] = {ts: 999, layout: '1'};
map['c:nots'] = {layout: '2c'};
const kept = M.normalizeLayoutMap(map);
out.kept = [Object.keys(kept).length, 'c:0' in kept, 'c:69' in kept, 'c:default' in kept, 'c:nots' in kept];
out.visible = M.visiblePanes({layout:'3c'}).map(p => p.source.kind);
""")
    assert out["fresh"]["layout"] == "1" and [p["source"]["kind"] for p in out["fresh"]["panes"]] == ["composite", "guides", "guides", "guides"]
    assert out["isDefault"] == [True, True, False, False]
    junk = out["junk"]
    assert junk["layout"] == "1" and [p["source"] for p in junk["panes"]] == [
        {"kind": "composite"}, {"kind": "guides"}, {"kind": "driver", "index": 2}, {"kind": "guides"}]
    assert junk["panes"][2] == {"source": {"kind": "driver", "index": 2}, "item": 3, "fit": "cover", "zoom": {"scale": 2, "x": 5, "y": -5},
                                "link": True}
    assert junk["panes"][3]["zoom"] is None
    assert out["kept"] == [64, False, True, False, False]
    assert out["visible"] == ["composite", "guides", "guides"]


def test_assign_swaps_composite_and_resets_item_and_zoom():
    out = run("""
let r = M.normalizeLayout({ts:1, layout:'2c', panes:[{source:{kind:'composite'}},{source:{kind:'view',viewId:'a'}, item:2, zoom:{scale:3,x:1,y:1}}]});
r = M.assignSource(r, 1, {kind:'composite'});
out.swap = r.panes.slice(0, 2).map(p => [p.source, p.item, p.zoom]);
out.composite = M.compositePane(r);
r = M.assignSource(r, 0, {kind:'driver', index: 1});
out.after = r.panes.slice(0, 2).map(p => p.source);
out.same = M.sameSource({kind:'view',viewId:'a'},{kind:'view',viewId:'a'}) && !M.sameSource({kind:'driver',index:0},{kind:'driver',index:1});
""")
    assert out["swap"] == [[{"kind": "view", "viewId": "a"}, 0, None], [{"kind": "composite"}, 0, None]]
    assert out["composite"] == 1
    assert out["after"] == [{"kind": "driver", "index": 1}, {"kind": "composite"}]
    assert out["same"]


def test_picker_sections_and_outcome_normalizer():
    out = run("""
out.sections = M.pickerSections({
    views: [{view_id:'a', label:'First', status:'ok', participation:'active', path:'9'},
            {view_id:'b', label:'', default_label:'Save', status:'missing', reason:'Gone', path:'12:7'},
            {view_id:'c', label:'Off', status:'ok', participation:'inactive', path:'20'}],
    candidates: [{path:'9', title:'dup'}, {path:'30', title:'Preview'}, {path:'31:4', class_type:'SaveVideo', inactive:{mode:'bypassed', by:'parent', label:'Pass two'}}],
    drivers: [{index:0, label:'Driver 1'}]});
out.outcomes = [
    M.normalizeOutcome({state:'queued', prompt_id:'p1'}, 'canvas'), M.normalizeOutcome({state:'queued'}, 'canvas'),
    M.normalizeOutcome({state:'refused', code:'branch_not_output'}, 'canvas'), M.normalizeOutcome({state:'busy'}, 'canvas'),
    M.normalizeOutcome({state:'claimed'}, 'mounted'), M.normalizeOutcome({state:'unknown'}, 'mounted'),
    M.normalizeOutcome({ok:true, payload:{run_ids:['run-1']}}, 'sdk'), M.normalizeOutcome({ok:false, payload:{run_ids:['run-2'], code:'x'}}, 'sdk'),
    M.normalizeOutcome({ok:false, payload:{code:'branch_not_output', error:'Only an output…'}}, 'sdk'), M.normalizeOutcome(null, 'canvas')];
out.refusal = [M.refusalText({code:'controls_unsaved'}), M.refusalText({code:'zzz'}), M.refusalText({message:'Server said'})];
""")
    sections = out["sections"]
    assert [i["label"] for i in sections["editor"]] == ["Composite", "Guides", "Driver 1"]
    assert [(i["label"], i["detail"]) for i in sections["views"]] == [("First", ""), ("Save", "Unavailable"), ("Off", "Switched off")]
    assert [(i["label"], i["detail"]) for i in sections["add"]] == [("Preview", ""), ("SaveVideo", "Inside Pass two, which is bypassed")]
    assert out["outcomes"] == [
        {"state": "queued", "promptId": "p1"}, {"state": "unconfirmed"}, {"state": "refused", "code": "branch_not_output"},
        {"state": "busy"}, {"state": "pending"}, {"state": "unconfirmed"}, {"state": "queued", "runId": "run-1"},
        {"state": "unconfirmed", "runId": "run-2"}, {"state": "refused", "code": "branch_not_output", "message": "Only an output…"},
        {"state": "refused", "code": "no_answer"}]
    assert out["refusal"][0].startswith("A Controls edit") and out["refusal"][1] == "The output did not run." and out["refusal"][2] == "Server said"


def test_status_table_rows():
    out = run("""
const view = (extra={}) => ({view_id:'a', status:'ok', participation:'active', path:'9', ...extra});
const result = (fresh=true) => ({prompt_id:'p1', fresh, entries:[{kind:'image', file:{filename:'a.png'}}], run:{}});
const s = args => { const r = M.viewPaneStatus(args); return [r.message, r.chip, r.result ? 'result' : null, !!r.unavailable]; };
out.rows = {
    lost: s({view:view(), sessionLost:true}),
    missing: s({view:view({status:'missing', reason:'Gone.'})}),
    inactive_prev: s({view:view({participation:'inactive'}), read:{state:'ok', result:result()}}),
    inactive_none: s({view:view({participation:'inactive'}), read:{state:'none'}}),
    none: s({view:view(), read:{state:'none'}}),
    not_in_run: s({view:view(), read:{state:'not_in_run'}}),
    pending_prev: s({view:view(), read:{state:'pending', result:result()}}),
    pending_none: s({view:view(), read:{state:'pending'}}),
    removed: s({view:view(), read:{state:'ok', result:result()}, run:{removed:true}}),
    fresh: s({view:view(), read:{state:'ok', result:result()}}),
    reused: s({view:view(), read:{state:'ok', result:result(false)}}),
    not_executed: s({view:view(), read:{state:'not_executed', result:result()}}),
    failed: s({view:view(), read:{state:'failed', latest_run:{outcome:'error'}}}),
    canceled: s({view:view(), read:{state:'failed', latest_run:{outcome:'interrupted'}, result:result()}}),
    missing_file: s({view:view(), read:{state:'missing_file'}}),
    unsupported: s({view:view(), read:{state:'unsupported'}}),
    held: s({view:view(), held:true, hold:{state:'ok', entry:{kind:'image'}, run:{}}}),
    held_gone: s({view:view(), held:true, hold:{state:'unavailable'}}),
    no_view: s({view:null}),
};
out.media = ['missing_file','too_large','unsupported','decode','failed'].map(c => M.mediaStatus(c));
out.saved = M.mediaStatus('decode', {takeSaved:true}).message;
out.players = [{kind:'image',file:{filename:'a.png'}},{kind:'image',file:{filename:'a.EXR'}},{kind:'animated_image',file:{filename:'a.webp'}},
    {kind:'video',file:{filename:'a.mp4'}},{kind:'audio',file:{filename:'a.flac'}},{kind:'unsupported'},{kind:'image'}].map(M.entryPlayer);
out.run = [M.runAvailability({capability:{ok:false}}), M.runAvailability({view:view(), capability:{ok:true}}),
    M.runAvailability({view:view({participation:'inactive', reason:'Its node is muted.'}), capability:{ok:true}}),
    M.runAvailability({view:view(), capability:{ok:true}, outcome:{settled:false}}),
    M.runAvailability({view:view(), capability:{ok:true}, busy:true})];
out.cancel = [M.cancelAvailability(null), M.cancelAvailability({settled:false}), M.cancelAvailability({settled:false, promptId:'p'})];
""")
    rows = out["rows"]
    assert rows["lost"] == ["Disconnected", None, None, False]
    assert rows["missing"] == ["Gone. Fix in Manage Controls.", "Unavailable", None, False]
    assert rows["inactive_prev"] == [None, "Switched off", "result", False]
    assert rows["inactive_none"] == ["No result yet", "Switched off", None, False]
    assert rows["none"] == rows["not_in_run"] == ["No result yet", None, None, False]
    assert rows["pending_prev"] == [None, "Previous result · Running", "result", False]
    assert rows["pending_none"] == ["Waiting for run", "Running", None, False]
    assert rows["removed"] == [None, "Canceled", "result", False]
    assert rows["fresh"] == [None, None, "result", False] and rows["reused"] == [None, "From cache", "result", False]
    assert rows["not_executed"] == [None, "Last run didn't produce this", "result", False]
    assert rows["failed"] == ["Last run failed", "Last run failed", None, False]
    assert rows["canceled"] == [None, "Last run was canceled", "result", False]
    assert rows["missing_file"] == ["The output file is missing", "Unavailable", None, False]
    assert rows["unsupported"] == ["This output type can't be shown here", "Unsupported", None, False]
    assert rows["held"] == [None, "Held", "result", False]
    assert rows["held_gone"] == ["Held result unavailable (file changed or removed)", "Unavailable", None, True]
    assert rows["no_view"][1] == "Unavailable"
    assert [m["chip"] for m in out["media"]] == ["Unavailable", "Unavailable", "Unsupported", "Can't play", "Unavailable"]
    assert out["media"][1]["message"] == "Too large to load here" and "Assets" in out["saved"]
    assert out["players"] == ["image", "unsupported", "animated", "video", "audio", "unsupported", "unsupported"]
    assert out["run"][0] == {"show": False, "reason": "This ComfyUI can't run one output on its own"}
    assert out["run"][1]["enabled"] is True and out["run"][2]["reason"] == "Its node is muted."
    assert out["run"][3]["enabled"] is False and out["run"][4]["reason"] == "Another output is waiting for ComfyUI"
    assert out["cancel"] == [{"show": False}, {"show": True, "enabled": False, "reason": "Waiting for ComfyUI to accept the run"},
                             {"show": True, "enabled": True, "reason": "Cancel this run"}]


def test_settings_store_the_layout_per_workflow_and_delete_the_default():
    settings = (ROOT / "web" / "js" / "editor_settings.js").as_uri()
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    script = f"""
const store = new Map();
globalThis.localStorage = {{getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: k => store.delete(k)}};
globalThis.window = {{localStorage: globalThis.localStorage}};
const S = await import({json.dumps(settings)});
const out = {{}};
S.updateEditorSettings({{views: {{layoutByWorkflow: {{'c:a': {{ts: 5, layout: '2x2', panes: [{{source: {{kind: 'view', viewId: 'v1'}}}}]}}}}}}}});
S.updateEditorSettings({{views: {{layoutByWorkflow: {{'c:b': {{ts: 6, layout: '2c'}}}}}}}});
out.both = Object.keys(S.getEditorSettings().views.layoutByWorkflow).sort();
// Patching one key never reverts another; null deletes.
S.updateEditorSettings({{views: {{layoutByWorkflow: {{'c:a': null}}}}}});
out.after = Object.keys(S.getEditorSettings().views.layoutByWorkflow);
out.stored = JSON.parse(store.get('sonder-editor-settings')).views.layoutByWorkflow['c:b'].layout;
console.log('RESULT=' + JSON.stringify(out));
"""
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    out = json.loads(next(l for l in result.stdout.splitlines() if l.startswith("RESULT="))[7:])
    assert out == {"both": ["c:a", "c:b"], "after": ["c:b"], "stored": "2c"}
