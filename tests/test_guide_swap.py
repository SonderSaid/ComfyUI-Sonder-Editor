"""Swap applicability parity, atomic refusals, and the actual popup gesture lifecycle."""
import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_phase43_routes import _load_route_module
from server.timeline_state import Scene, GuideFrame, TimelineProject
from test_history_optimistic import _run_history_node
import test_scene_mutation_registration as registration

ROOT = Path(__file__).resolve().parents[1]

def _scene():
    return Scene(scene_id="scene", duration_frames=100, guide_frames=[
        GuideFrame(guide_id="a", frame_index=10, asset_id="asset-a", strength=0.4),
        GuideFrame(guide_id="b", frame_index=20, asset_id="asset-b", strength=0.8),
        GuideFrame(guide_id="end", frame_index=-1, asset_id="asset-end")])

def _op(scene, a=10, b=20):
    def claim(frame):
        guide = next(g for g in scene.guide_frames if g.frame_index == frame)
        return {"guide_id": guide.guide_id, "frame_index": frame, "asset_id": guide.asset_id}
    return {"type": "swap_guides", "frame_index_a": a, "frame_index_b": b,
            "expected_a": claim(a), "expected_b": claim(b)}

@pytest.mark.parametrize("case", ["normal", "sentinel", "locked", "same", "missing",
                                  "stale_a", "stale_b", "legacy", "duplicate_id", "strength", "null_asset"])
def test_guide_swap_geometry_matches_server(case, monkeypatch):
    routes = _load_route_module(monkeypatch)
    scene = _scene()
    operation = _op(scene, -1 if case == "sentinel" else 10)
    if case == "locked": scene.guide_track_config.locked = True
    if case == "same": operation["frame_index_b"] = 10
    if case == "missing": operation["frame_index_b"] = 99
    if case == "stale_a": operation["expected_a"]["guide_id"] = "other"
    if case == "stale_b": operation["expected_b"]["asset_id"] = "other"
    if case == "legacy": scene.guide_frames[0].guide_id = ""; operation["expected_a"]["guide_id"] = ""
    if case == "duplicate_id": scene.guide_frames[1].guide_id = "a"; operation["expected_b"]["guide_id"] = "a"
    if case == "strength": operation["expected_a"]["strength"] = 0.9
    if case == "null_asset":
        scene.guide_frames[0].asset_id = None
        operation["expected_a"]["asset_id"] = ""
    before = scene.to_dict()
    try:
        routes._apply_scene_mutation_operation(TimelineProject(scenes=[scene]), scene, operation)
        applied = True
    except routes.ProjectMutationRequestError:
        applied = False
        assert scene.to_dict() == before, "refusal must be atomic"
    assert applied == (case in {"normal", "sentinel"})
    node = shutil.which("node")
    if not node: pytest.skip("node is required for geometry parity")
    url = (ROOT / "web/js/scene_guide_geometry.js").as_uri()
    code = (f"import {{applyGuideSwap}} from {json.dumps(url)};"
            f"const scene={json.dumps(before)}, op={json.dumps(operation)};"
            "const applied=applyGuideSwap(scene,op);console.log(JSON.stringify({applied,scene}));")
    result = subprocess.run([node, "--input-type=module", "-e", code], capture_output=True,
                            text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"applied": applied, "scene": scene.to_dict()}

_IDENTITY_CASES = [
    ("same", {"guide_id": "a", "frame_index": 10, "asset_id": "asset-a"}),
    ("empty_snapshot", {}),
    ("no_id_key", {"frame_index": 10, "asset_id": "asset-a"}),
    ("other_id", {"guide_id": "b", "frame_index": 10, "asset_id": "asset-a"}),
    ("moved", {"guide_id": "a", "frame_index": 20}),
    ("other_asset", {"guide_id": "a", "asset_id": "asset-b"}),
    ("blank_id_vs_stored", {"guide_id": "", "frame_index": 10}),
    ("strength_close", {"guide_id": "a", "strength": 0.4 + 1e-12}),
    ("muted", {"guide_id": "a", "muted": True}),
]


@pytest.mark.parametrize("case, expected", _IDENTITY_CASES)
def test_guide_identity_match_matches_server(case, expected, monkeypatch):
    """The host's local applies gate on the same decision the server's guard makes.

    A local apply that disagrees paints a guide the server then refuses to
    touch, or skips one the server writes.
    """
    routes = _load_route_module(monkeypatch)
    guide = _scene().guide_frames[0]
    try:
        routes._validate_guide_identity(guide, expected)
        server = True
    except routes.ProjectMutationRequestError:
        server = False
    node = shutil.which("node")
    if not node: pytest.skip("node is required for identity parity")
    url = (ROOT / "web/js/scene_guide_geometry.js").as_uri()
    code = (f"import {{guideIdentityMatches}} from {json.dumps(url)};"
            f"console.log(JSON.stringify(guideIdentityMatches("
            f"{json.dumps(guide.to_dict())}, {json.dumps(expected)})));")
    result = subprocess.run([node, "--input-type=module", "-e", code], capture_output=True,
                            text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) is server, case


def test_swap_round_trip_restores_identity_fields_and_links(monkeypatch):
    routes = _load_route_module(monkeypatch)
    from server.scene_history_merge import merge_scene_history
    scene = _scene()
    scene.linked_item_groups = [{"group_id": "linked-guides", "items": [
        {"type": "guide", "id": "a"}, {"type": "guide", "id": "b"}]}]
    before = scene.to_dict()
    routes._apply_scene_mutation_operation(TimelineProject(scenes=[scene]), scene, _op(scene))
    after = scene.to_dict()
    restored = merge_scene_history(after, before, after)
    # The shared restore-order defect is deliberately out of scope; identity
    # fields, not array order, are the documented acceptance of this repair.
    def by_id(value): return {g["guide_id"]: g for g in value["guide_frames"]}
    assert by_id(restored) == by_id(before)
    redone = merge_scene_history(before, after, restored)
    assert by_id(redone) == by_id(after)
    assert after["linked_item_groups"] == before["linked_item_groups"]
    assert restored["linked_item_groups"] == before["linked_item_groups"]
    assert redone["linked_item_groups"] == before["linked_item_groups"]

def _swap_callback():
    source = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    start = source.index('async (diagnostics) => {', source.index('"swapGuides"'))
    opening = source.index("{", start)
    end = registration._match_delimiter(source, opening, "{", "}")
    return source[start:end + 1].replace('async (diagnostics) =>', 'async function(diagnostics)', 1)

@pytest.mark.parametrize("refused, queued_redo", [(False, False), (True, False), (True, True)])
def test_swap_gesture_paints_stamps_and_preserves_redo_on_refusal(refused, queued_redo):
    url = (ROOT / "web/js/scene_guide_geometry.js").as_uri()
    _run_history_node(f"const {{applyGuideSwap}}=await import({json.dumps(url)});" + """
        const w=makeHistoryWidget();
        for (const name of ['_pushUndo','_claimHistoryPostSnapshotCapture','_stampHistoryPostSnapshot'])
            w[name]=EditorWidget.prototype[name];
        w._trimUndoStack=()=>{};
        w._historyStackRevision=0;
        w.activeScene=""" + json.dumps(_scene().to_dict()) + ";" + """
        w.scenes=[w.activeScene];
        const before=structuredClone(w.activeScene), priorRedo={label:'prior future',sceneId:'scene',
            snapshot:{...structuredClone(w.activeScene),name:'future'},
            postSnapshot:structuredClone(w.activeScene)};
        let restores=0;
        const refusals=[];
        w._recordHistoryRefusal=(operation,code)=>refusals.push(code);
        w._restoreScene=async(sceneId,target)=>{restores++;return structuredClone(target);};
        w._redoStack.push(priorRedo);
        let paints=0, popups=0, refreshes=0, release;
        w._renderSceneAfterLocalMutation=()=>{paints++;};
        w._showGuideManagementPopup=()=>{popups++;};
        const guides=[...w.activeScene.guide_frames], guide=guides[0], rowGuide=guide;
        const locked=false, swapSelect={value:'20'}, x=1, y=2, event={stopPropagation(){}};
        const refreshPanel=async()=>{refreshes++;w.activeScene=structuredClone(before);};
        globalThis.fetch=async(url, init)=>{
            assert.ok(url.endsWith('/mutations'));
            const sent=JSON.parse(init.body).operations[0];
            assert.equal(sent.expected_a.frame_index,10);
            assert.equal(sent.expected_b.frame_index,20);
            const canonical=structuredClone(before);
            applyGuideSwap(canonical,sent);
            await new Promise(resolve=>{release=resolve;});
            return """ + ("new Response(JSON.stringify({error:'Guide identity mismatch',code:'identity_mismatch'}),{status:409});" if refused else "new Response(JSON.stringify({scene:canonical}),{headers:{'X-Sonder-Project-Modified-At':'v2'}});") + """
        };
        const invoke=""" + _swap_callback() + ";" + """
        // Only the arrow's binding is adapted; its body is the live popup callback.
        const done=invoke.call(w,null);
        await new Promise(resolve=>setTimeout(resolve,0));
        assert.equal(paints,1);assert.equal(popups,1);
        assert.equal(w.activeScene.guide_frames.find(g=>g.guide_id==='a').frame_index,20);
        assert.equal(w._undoStack.length,1);assert.equal(w._undoStack[0].pending,true);
        assert.equal(w._redoStack[0],priorRedo);
        """ + ("const redoDone=w._runRedo();" if queued_redo else "") + """
        release();await done;
        """ + ("""
        await redoDone;
        assert.equal(restores,1);assert.deepEqual(refusals,[]);
        assert.equal(w._redoStack.length,0);
        """ if queued_redo else """
        assert.equal(w._undoStack.length,0);assert.equal(w._redoStack[0],priorRedo);
        assert.equal(refreshes,1);assert.deepEqual(w.activeScene,before);
        """ if refused else """
        assert.equal(w._undoStack.length,1);assert.equal(w._undoStack[0].pending,false);
        assert.deepEqual(w._undoStack[0].snapshot,before);
        assert.equal(w._undoStack[0].postSnapshot.guide_frames.find(g=>g.guide_id==='a').frame_index,20);
        assert.equal(w._redoStack.length,0);
        """))


def test_swap_registration_decisions():
    from server import routes
    import test_scene_mutation_retry_policy as retry
    # Two independent identities permit retry; preserve the operation order,
    # and deliberately leave this operation outside the no-write optimization.
    assert "swap_guides" not in routes._SCENE_ONLY_MUTATIONS
    assert "swap_guides" not in retry.NEVER_RETRYABLE
    assert "swap_guides" in registration.GUARDED_OPERATIONS_NEEDING_A_MESSAGE


def test_history_rebase_retargets_both_guide_claims():
    _run_history_node("""
        const w=makeHistoryWidget();
        const intent={sceneId:'scene',operations:[{type:'swap_guides',
            frame_index_a:10,frame_index_b:20,
            expected_a:{guide_id:'a',frame_index:10,asset_id:'asset-a'},
            expected_b:{guide_id:'b',frame_index:20,asset_id:'asset-b'}}]};
        const authored={scene_id:'scene',guide_frames:[
            {guide_id:'a',frame_index:10,asset_id:'asset-a'},
            {guide_id:'b',frame_index:20,asset_id:'asset-b'}]};
        const ordered={...authored,guide_frames:[
            {guide_id:'a',frame_index:30,asset_id:'asset-a'},
            {guide_id:'b',frame_index:40,asset_id:'asset-b'}]};
        const rebased=w._rebaseSceneMutationIntentForHistory(intent,ordered,authored);
        const op=rebased.operations[0];
        assert.equal(op.frame_index_a,30);assert.equal(op.expected_a.frame_index,30);
        assert.equal(op.frame_index_b,40);assert.equal(op.expected_b.frame_index,40);
        assert.equal(op.expected_a.guide_id,'a');assert.equal(op.expected_b.guide_id,'b');
        assert.equal(intent.operations[0].frame_index_a,10);
        assert.equal(intent.operations[0].frame_index_b,20);
    """)
