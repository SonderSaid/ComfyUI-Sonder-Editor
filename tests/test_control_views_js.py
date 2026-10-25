"""Output Views Phase 3A on the canvas: control_bindings.js and the canvas writer.

The JS reader mirrors control_manifest.read_views (parity over the same inputs);
resolveViews locates each View by its anchor (`sonderViews`, on a root node or
the root subgraph instance holding the output); the export carries each View's
source facts and the saved manifest never does; a copied node drops its View
anchor; the writer creates a View (and the first Relay) in one history step,
renames, reorders and removes it, and rolls every anchor back on a failure.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from server import control_manifest as cm

ROOT = Path(__file__).resolve().parents[1]
BINDINGS = (ROOT / "web" / "js" / "control_bindings.js").as_uri()
HOST = (ROOT / "web" / "js" / "controls_host_canvas.js").as_uri()

GRAPH = r"""
import assert from 'node:assert/strict';
const B = await import(BINDINGS_URL);
class Graph { constructor(id){this.id=id;this._nodes=[];this.links=new Map();this.subgraphs=new Map();this.inputs=[];this.dirty=0;}
    setDirtyCanvas(){this.dirty++;} add(n){n.graph=this;this._nodes.push(n);} remove(n){this._nodes.splice(this._nodes.indexOf(n),1);} getLink(id){return this.links.get(id)??null;} }
const OUTPUT = new Set(['PreviewImage','SaveVideo']);
globalThis.LiteGraph = {registered_node_types: Object.fromEntries([...OUTPUT].map(t=>[t,{nodeData:{output_node:true}}]))};
const root = new Graph('root');
const widget = (name,value,extra={}) => ({name,value,options:{},callback(){},...extra});
const add = (g,type,id,widgets=[],extra={}) => {const n={id,type,comfyClass:type,widgets,graph:g,mode:0,properties:{},inputs:[],title:extra.title||type,...extra};g._nodes.push(n);return n;};
const editor = add(root,'SonderEditor',1);
const preview = add(root,'PreviewImage',9,[],{title:'Pass 1 preview'});
const sampler = add(root,'KSampler',6);
const inner = new Graph('def'); const nested = new Graph('def2');
const host = add(root,'SubgraphHost',12,[],{subgraph:inner,title:'Pass two'});
const save = add(inner,'SaveVideo',7,[],{title:'Inner save'});
const nestedHost = add(inner,'SubgraphHost',15,[],{subgraph:nested});
const deep = add(nested,'PreviewImage',3,[],{title:'Deep'});
root.subgraphs.set('def',inner); inner.subgraphs.set('def2',nested);
const manifest = views => ({v:1,bindings:{},notes:{},views:{v:1,items:views}});
"""


def run(body: str, scenario: str = ""):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    script = GRAPH.replace("BINDINGS_URL", json.dumps(BINDINGS)) + f"const SCENARIO={json.dumps(scenario)};\nconst out={{}};\n" + body \
        + "\nconsole.log('RESULT='+JSON.stringify(out));"
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    line = next(line for line in result.stdout.splitlines() if line.startswith("RESULT="))
    return json.loads(line[len("RESULT="):])


def test_read_views_parity_with_python():
    cases = [
        None, [], "x", {"v": None}, {"v": True}, {"v": 1.0, "items": {"a": {"label": "x"}}}, {"v": "1"}, {"v": 2, "items": {}},
        {"items": []}, {"items": {"a": {"label": "  Hi ", "order": 3}, "b": {"label": 5}, "bad id": {}, "constructor": {},
                                  "c": {"order": 99999}, "d": "no", "e": {"label": "x" * 121}, "f": {"order": None}}},
        {"items": {f"v{i}": {} for i in range(40)}},
        # A whole float order reads as that number on both sides (JSON 5.0 is 5 in JS).
        {"items": {"a": {"order": 5.0}, "b": {"order": 5.5}, "c": {"order": -0.0}}},
        # Past the cap both keep the same Views: JS lists index-like keys first.
        {"items": {**{f"v{i}": {} for i in range(30)}, "7": {}, "12": {}, "3": {}, "012": {}}},
    ]
    out = run(f"""
const cases = {json.dumps(cases)};
out.js = cases.map(c => {{ const r = B.readViews(c); return [Object.keys(r.items).sort(), r.error ? 'off' : null,
    Object.fromEntries(Object.entries(r.items).map(([k,v])=>[k,[v.label,v.order]]))]; }});
""")
    for case, js in zip(cases, out["js"]):
        items, error = cm.read_views(case)
        assert js[0] == sorted(items), case
        assert js[1] == ("off" if error else None), case
        assert js[2] == {k: [v["label"], v["order"]] for k, v in items.items()}, case


def test_resolve_views_root_instance_nested_statuses_and_participation():
    out = run("""
preview.properties.sonderViews = {'':'a'};
host.properties.sonderViews = {'7':'b','15:3':'c','99':'gone'};
sampler.properties.sonderViews = {'':'k'};
editor.properties.sonderViews = {'':'stale'};  // names no View: ignored
const m = manifest({a:{},b:{},c:{},gone:{},k:{},none:{},dup:{}});
add(root,'PreviewImage',20).properties.sonderViews={'':'dup'}; add(root,'PreviewImage',21).properties.sonderViews={'':'dup'};
let r = B.resolveViews(root, m).views;
out.first = Object.fromEntries(Object.entries(r).map(([k,v])=>[k,[v.status,v.path||null,v.participation||null,v.class_type||null]]));
host.mode = 4;  // bypassing the instance turns off what is inside
r = B.resolveViews(root, m).views;
out.inner = [r.b.participation, r.b.inactiveReason, r.c.participation];
host.mode = 0; save.mode = 2;
out.self = B.resolveViews(root, m).views.b.inactiveReason;
save.mode = 0;
out.paths = [B.resolveExecPath(root,'12:15:3')?.node?.title, B.resolveExecPath(root,'12:15:4'), B.resolveExecPath(root,'9:1')];
""")
    assert out["first"] == {
        "a": ["ok", "9", "active", "PreviewImage"], "b": ["ok", "12:7", "active", "SaveVideo"],
        "c": ["ok", "12:15:3", "active", "PreviewImage"], "gone": ["missing", None, None, None],
        "k": ["kind", "6", None, "KSampler"], "none": ["missing", None, None, None], "dup": ["duplicate", None, None, None],
    }
    assert out["inner"][0] == "inactive" and out["inner"][1] == {"mode": "bypassed", "by": "parent", "label": "Pass two"}
    assert out["inner"][2] == "inactive"
    assert out["self"] == {"mode": "muted", "by": "self", "label": "Inner save"}
    assert out["paths"] == ["Deep", None, None]


def test_export_carries_facts_edit_strips_them_copy_rule_and_definition_drop():
    out = run("""
preview.properties.sonderViews = {'':'a'};
host.properties.sonderViews = {'7':'b'};
const saved = JSON.stringify({...manifest({a:{label:'First',order:1},b:{},x:'damaged'}), future:1});
const exported = JSON.parse(B.exportManifest(saved, root));
out.items = exported.views.items;
out.edited = JSON.parse(B.editManifest(JSON.stringify(exported), m => { m.views.items.a.label = 'Renamed'; })).views.items;
// Another version's section is exported and edited exactly as saved.
const newer = JSON.stringify({v:1,bindings:{},notes:{},views:{v:2,items:{a:{status:'odd'}}}});
out.newer = [JSON.parse(B.exportManifest(newer, root)).views, JSON.parse(B.editManifest(newer, () => {})).views];
// A pasted copy loses the anchor; load, undo and redo keep it.
const copy = {properties:{sonderViews:{'':'a'}}};
let configuring = false;
B.installAnchorCopyRule(copy, () => configuring);
copy.onConfigure();
out.copied = copy.properties.sonderViews ?? null;
copy.properties.sonderViews = {'':'a'}; configuring = true; copy.onConfigure();
out.loaded = copy.properties.sonderViews;
// Remove drops the id inside subgraph definitions too.
save.properties.sonderViews = {'':'b','x':'other'};
out.dropped = B.dropViewAnchors(root, 'b').map(n => n.id);
out.left = [host.properties.sonderViews ?? null, save.properties.sonderViews];
""")
    assert out["items"]["a"] == {"label": "First", "order": 1, "status": "ok", "path": "9", "class_type": "PreviewImage",
                                 "title": "Pass 1 preview", "participation": "active"}
    assert out["items"]["b"]["path"] == "12:7" and out["items"]["x"] == "damaged"
    assert out["edited"]["a"] == {"label": "Renamed", "order": 1} and out["edited"]["b"] == {}
    assert out["newer"] == [{"v": 2, "items": {"a": {"status": "odd"}}}] * 2
    assert out["copied"] is None and out["loaded"] == {"": "a"}
    assert sorted(out["dropped"]) == [7, 12] and out["left"] == [None, {"x": "other"}]
    # The Python reader keeps every exported fact the export wrote.
    parsed = cm.read_views({"v": 1, "items": out["items"]})[0]
    assert parsed["b"]["status"] == "ok" and parsed["b"]["title"] == "Inner save"


def test_inactive_bodies_first_and_last_lists():
    out = run("""
preview.mode = 2; host.mode = 4;
const views = B.inactiveOutputs(root);
out.outputs = views.map(v => [v.path, v.inactive.mode, v.inactive.by]);
const bodies = B.inactiveBodies({}, {first: [views[1]], last: views});
out.order = Object.keys(bodies).sort();
out.body = bodies['12:7'];
// A long list of other muted outputs never crowds out a View's own source.
const many = Array.from({length: 300}, (_, i) => ({node: {...preview, id: 1000 + i}, path: String(1000 + i), inactive: views[0].inactive}));
const capped = B.inactiveBodies({}, {first: [views[1]], last: many});
out.capped = [Object.keys(capped).length, '12:7' in capped];
""")
    assert out["outputs"] == [["9", "muted", "self"], ["12:7", "bypassed", "parent"], ["12:15:3", "bypassed", "parent"]]
    assert out["order"] == ["12:15:3", "12:7", "9"]
    assert out["capped"] == [256, True]
    assert out["body"]["class_type"] == "SaveVideo" and out["body"]["mode"] == "bypassed" and out["body"]["by"] == "parent"


WRITER = r"""
const {createCanvasControlsHost} = await import(HOST_URL);
const relay = SCENARIO === 'new-relay' ? null : add(root,'SonderControlRelay',2,[widget('manifest',JSON.stringify(manifest({old:{label:'Old'}})))]);
if (relay) preview.properties.sonderViews = {'':'old'};
let created = null, captures = 0, plan = null, refuse = false;
LiteGraph.createNode = type => (created = {id:50,type,comfyClass:type,mode:0,properties:{},inputs:[],widgets:[widget('manifest','')],
    size:[200,40],addCustomWidget(w){this.widgets.push(w);return w;},removeWidget(w){this.widgets.splice(this.widgets.indexOf(w),1);},computeSize(){return [200,40];},setSize(s){this.size=s;}});
const api = {addEventListener(){},removeEventListener(){},async fetchApi(url,init){
    const body = JSON.parse(init.body); out.bodies = (out.bodies||0)+1; out.inactive = Object.keys(body.inactive||{});
    return {ok:true,json:async()=>({relay:{status:relay?'ok':'none'},bindings:[],notes:[],views:[],
        ...(body.changes ? {ok:!refuse,changes:refuse?{}:plan(body.changes),rejected:refuse?[{target:'x',reason:'no'}]:[]} : {})})};}};
const app = {rootGraph:root,graph:root,canvas:{},async graphToPrompt(){return {output:{'1':{class_type:'SonderEditor'},'9':{class_type:'PreviewImage'}}};}};
const writer = createCanvasControlsHost({app,api,getEditorNode:()=>editor,debounceMs:1,getChangeTracker:()=>({captureCanvasState(){captures++;}})});
const saved = () => JSON.parse((relay || created).widgets[0].value);
"""


def run_writer(body: str, scenario: str = ""):
    return run(WRITER.replace("HOST_URL", json.dumps(HOST)) + body, scenario)


def test_writer_creates_first_relay_and_view_in_one_history_step():
    out = run_writer("""
plan = changes => Object.fromEntries(Object.entries(changes).map(([t,v]) => [t, {candidate:{path:'12:7',class_type:'SaveVideo',host:'12',rel:'7'},label:'',order:3}]));
const result = await writer.applyChanges({'view:n1:create':{candidate:{path:'12:7'}}});
out.ok = result.ok; out.captures = captures; out.added = root._nodes.includes(created);
out.items = saved().views; out.anchor = host.properties.sonderViews;
""", "new-relay")
    assert out["ok"] and out["captures"] == 2 and out["added"]
    assert out["items"] == {"v": 1, "items": {"n1": {"label": "", "order": 3}}}
    assert out["anchor"] == {"7": "n1"}


def test_writer_rename_reorder_remove_rollback_and_refusals():
    out = run_writer("""
plan = changes => changes;
let r = await writer.applyChanges({'view:old:label':'Preview', 'view:old:order':4});
out.renamed = [r.ok, saved().views.items.old];
// A create naming an output that moved is refused before anything lands.
plan = () => ({'view:n2:create':{candidate:{path:'12:8',class_type:'SaveVideo',host:'12',rel:'8'},label:'',order:1}});
r = await writer.applyChanges({'view:n2:create':{candidate:{path:'12:8'}}});
out.moved = [r.ok, r.rejected[0].reason, host.properties.sonderViews ?? null];
// The output already anchors a View (one the manifest names).
host.properties.sonderViews = {'7':'old'};
plan = () => ({'view:n3:create':{candidate:{path:'12:7',class_type:'SaveVideo',host:'12',rel:'7'},label:'',order:1}});
r = await writer.applyChanges({'view:n3:create':{candidate:{path:'12:7'}}});
out.taken = [r.ok, r.rejected[0].reason];
// A stale anchor (an id no View has) does not block a new View.
host.properties.sonderViews = {'7':'ghost'};
r = await writer.applyChanges({'view:n3:create':{candidate:{path:'12:7'}}});
out.stale = [r.ok, host.properties.sonderViews];
// A failing manifest write rolls the anchors back.
relay.widgets[0].callback = () => { throw Error('manifest failed'); };
const before = JSON.stringify(preview.properties);
plan = changes => changes;
r = await writer.applyChanges({'view:old:remove':true});
out.rollback = [r.ok, JSON.stringify(preview.properties) === before];
relay.widgets[0].callback = () => {};
r = await writer.applyChanges({'view:old:remove':true});
out.removed = [r.ok, preview.properties.sonderViews ?? null, Object.keys(saved().views.items)];
// Timing (Phase 3B) is a presentation field: set, then null removes it.
const timing = {mode:'editor_window', anchor:'render_start', source_offset:2, fps:'editor'};
r = await writer.applyChanges({'view:n3:timing':timing});
out.timed = [r.ok, saved().views.items.n3.timing];
r = await writer.applyChanges({'view:n3:timing':null});
out.untimed = [r.ok, 'timing' in saved().views.items.n3];
""")
    assert out["renamed"] == [True, {"label": "Preview", "order": 4}]
    assert out["moved"][0] is False and "moved" in out["moved"][1] and out["moved"][2] is None
    assert out["taken"] == [False, "This output already has a View."]
    assert out["stale"] == [True, {"7": "n3"}]
    assert out["rollback"] == [False, True]
    assert out["removed"] == [True, None, ["n3"]]
    assert out["timed"] == [True, {"mode": "editor_window", "anchor": "render_start", "source_offset": 2, "fps": "editor"}]
    assert out["untimed"] == [True, False]


def test_writer_fingerprint_includes_view_anchors_and_sends_view_bodies():
    out = run_writer("""
plan = changes => changes;
preview.mode = 2;
await writer.refresh(true);
const hash1 = await writer.getPromptHash();
host.properties.sonderViews = {'7':'old'};
const hash2 = await writer.getPromptHash();
out.changed = hash1 !== hash2;
out.views = typeof writer.setViewsDemand;
""")
    assert out["changed"] and out["views"] == "function"
    assert "9" in out["inactive"]  # the muted View source's body went to the describe


def test_view_targets_never_change_what_a_run_submits():
    """The save gate: no view:* target can refuse or delay a Generate."""
    targets = (ROOT / "web" / "js" / "control_targets.js").as_uri()
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    script = f"""
const t = await import({json.dumps(targets)});
const fields = ['create', 'remove', 'label', 'order', 'reset', 'timing'];
console.log('RESULT=' + JSON.stringify({{
    run: fields.map(f => t.changesRun(`view:a:${{f}}`)),
    structural: fields.map(f => t.isStructuralTarget(`view:a:${{f}}`)),
    binding: t.changesRun('binding:a:value'),
}}));
"""
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    out = json.loads(next(line for line in result.stdout.splitlines() if line.startswith("RESULT="))[7:])
    assert out == {"run": [False] * 6, "structural": [True, True, False, False, False, False], "binding": True}


# ── Phase 4: sampling-preview Views (feed) ───────────────────────────────────

def test_read_views_feed_parity_with_python():
    cases = [{"items": {"s": {"feed": "sampling"}, "o": {"feed": "output"}, "n": {"feed": None}, "d": {},
                        "bad": {"feed": "video"}, "num": {"feed": 2}}}]
    out = run(f"""
const cases = {json.dumps(cases)};
out.js = cases.map(c => {{ const r = B.readViews(c);
    return Object.fromEntries(Object.entries(r.items).map(([k,v]) => [k, B.viewFeed(v)])); }});
out.classes = [...B.SAMPLING_CLASSES].sort();
""")
    from server import control_views as cv
    for case, js in zip(cases, out["js"]):
        items, _error = cm.read_views(case)
        assert js == {k: cv.feed_of(v) for k, v in items.items()}, case
    assert out["classes"] == sorted(cv.SAMPLING_CLASSES)


def test_resolve_views_lets_a_sampling_view_name_any_node():
    out = run("""
sampler.properties.sonderViews = {'':'s'};
host.properties.sonderViews = {'':'h'};
const m = manifest({s:{feed:'sampling'}, h:{feed:'sampling'}});
let r = B.resolveViews(root, m).views;
out.ok = [r.s.status, r.s.participation, r.s.class_type];
out.host = r.h.status;  // a subgraph instance has no previews of its own
// As an output View the same node is not an output.
out.output = B.resolveViews(root, manifest({s:{}, h:{}})).views.s.status;
sampler.mode = 2;
r = B.resolveViews(root, m).views;
out.muted = [r.s.status, r.s.participation, r.s.inactiveReason.mode];
// A muted core sampler is a candidate that is off; a muted unknown node is not.
const other = add(root, 'CustomSampler', 33); other.mode = 2;
out.inactive = B.inactiveOutputs(root).map(v => v.path);
// The export keeps the feed and adds the facts.
const exported = JSON.parse(B.exportManifest(JSON.stringify(m), root)).views.items;
out.exported = [exported.s.feed, exported.s.status, exported.s.class_type];
""")
    assert out["ok"] == ["ok", "active", "KSampler"]
    assert out["host"] == "kind" and out["output"] == "kind"
    assert out["muted"] == ["ok", "inactive", "muted"]
    assert "6" in out["inactive"] and "33" not in out["inactive"]
    assert out["exported"] == ["sampling", "ok", "KSampler"]


def test_writer_creates_a_sampling_view_on_a_non_output_node():
    out = run_writer("""
plan = changes => Object.fromEntries(Object.entries(changes).map(([t,v]) =>
    [t, {candidate:{path:'6',class_type:'KSampler',host:'6',rel:''},label:'',order:2,feed:v.feed}]));
let r = await writer.applyChanges({'view:s1:create':{candidate:{path:'6'},feed:'sampling'}});
out.created = [r.ok, saved().views.items.s1, sampler.properties.sonderViews];
// Without the sampling feed the same node can't be an output View.
r = await writer.applyChanges({'view:s2:create':{candidate:{path:'6'}}});
out.output = [r.ok, r.rejected?.[0]?.reason];
// The node has a View now: a second one (any feed) is refused.
r = await writer.applyChanges({'view:s3:create':{candidate:{path:'6'},feed:'sampling'}});
out.taken = [r.ok, r.rejected?.[0]?.reason];
// Renaming keeps the feed.
plan = changes => changes;
r = await writer.applyChanges({'view:s1:label':'Pass 1 live'});
out.renamed = saved().views.items.s1;
""")
    assert out["created"] == [True, {"label": "", "order": 2, "feed": "sampling"}, {"": "s1"}]
    assert out["output"][0] is False and "moved" in out["output"][1]
    assert out["taken"] == [False, "This node already has a View."]
    assert out["renamed"] == {"label": "Pass 1 live", "order": 2, "feed": "sampling"}


def test_the_mounted_publication_shrinks_sampling_candidates_first_and_keeps_the_feed():
    relay = (ROOT / "web" / "js" / "controls_canvas_relay.js").as_uri()
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    script = f"""
const R = await import({json.dumps(relay)});
const big = 'x'.repeat(4000);
const row = i => ({{path: String(i), class_type: 'KSampler', title: big, host: String(i), rel: ''}});
const view = {{bindings: [], notes: [], views: [{{view_id: 's', feed: 'sampling', status: 'ok', label: 'L', runnable: false,
    run_reason: 'r', path: '6'}}],
    view_sampling_candidates: Array.from({{length: 120}}, (_, i) => row(i)),
    view_candidates: Array.from({{length: 20}}, (_, i) => row(500 + i))}};
const fitted = R.fitPublicationView(view, {{}});
const minimal = R.fitPublicationView({{...view, views: view.views.map(v => ({{...v, bulk: big.repeat(400)}}))}}, {{}});
console.log('RESULT=' + JSON.stringify({{
    sampling: fitted.view_sampling_candidates?.length ?? null, samplingCut: !!fitted.view_sampling_candidates_truncated,
    outputs: fitted.view_candidates?.length ?? null,
    minimal: minimal.views?.[0] ? [minimal.views[0].feed, minimal.views[0].run_reason, 'bulk' in minimal.views[0]] : null,
}}));
"""
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    out = json.loads(next(line for line in result.stdout.splitlines() if line.startswith("RESULT="))[7:])
    # Sampling-preview sources shrink before the output candidates do.
    assert out["samplingCut"] and out["sampling"] < 120 and out["outputs"] == 20
    assert out["minimal"] == ["sampling", "r", False]


def test_a_sampling_view_never_names_an_output_node_and_a_muted_seen_node_sends_its_body():
    out = run("""
preview.properties.sonderViews = {'':'o'};
const custom = add(root, 'CustomSampler', 33); custom.properties.sonderViews = {'':'c'}; custom.mode = 2;
const m = manifest({o:{feed:'sampling'}, c:{feed:'sampling'}});
const r = B.resolveViews(root, m).views;
out.statuses = [r.o.status, r.c.status, r.c.participation];
// The canvas sends a body for a sampling View's own muted source, whatever its class.
const sources = Object.values(r).filter(v => v.inactiveReason && v.node).map(v => ({node: v.node, path: v.path, inactive: v.inactiveReason}));
out.bodies = Object.keys(B.inactiveBodies({}, {first: sources, last: B.inactiveOutputs(root)}));
""")
    assert out["statuses"] == ["kind", "ok", "inactive"]
    assert "33" in out["bodies"]


def test_the_writer_refuses_a_sampling_view_on_an_output_node():
    out = run_writer("""
plan = changes => Object.fromEntries(Object.entries(changes).map(([t,v]) =>
    [t, {candidate:{path:'9',class_type:'PreviewImage',host:'9',rel:''},label:'',order:2,feed:'sampling'}]));
const r = await writer.applyChanges({'view:s9:create':{candidate:{path:'9'},feed:'sampling'}});
out.refused = [r.ok, r.rejected?.[0]?.reason, preview.properties.sonderViews ?? null];
""")
    assert out["refused"][0] is False and "moved" in out["refused"][1]
    assert out["refused"][2] == {"": "old"}
