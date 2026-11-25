"""A step refused for a real conflict is refused once; the second press in a row skips it."""
import re
from pathlib import Path

import pytest
from test_history_optimistic import _run_history_node

ROOT = Path(__file__).resolve().parents[1]
WIDGET = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
BOTH = pytest.mark.parametrize("operation", ["undo", "redo"])


def _run(operation, body):
    """Drive the real press, queue, restore and notification bus.

    `answers` maps a step's target name to the responses its restore gets, in
    order, the last one repeating. `press()` is one press; `stack` and `opposite`
    are the operation's own stacks, read live (a scene switch replaces them).
    """
    bus = (ROOT / "web/js/editor_notifications.js").as_uri()
    _run_history_node("""
        const bus=await import(""" + repr(bus) + """);
        const op=""" + repr(operation) + """, Op=op[0].toUpperCase()+op.slice(1);
        const w=makeHistoryWidget();
        Object.assign(w,{_historyOperationSeq:0,_queuedHistoryOperationCount:0,
            _queuedHistoryWaits:new Set(),_queuedHistoryNotification:null,_historyStackRevision:0,
            _historyCommitRevisionByEntry:new WeakMap(),_refusedHistoryStep:null,
            _maxUndoSteps:50,_projectDirName:()=>'project'});
        const refusals=[];
        w._recordHistoryRefusal=(operation,gate,_d,extra)=>refusals.push({operation,gate,...extra});
        const own=()=>w[op==='undo'?'_undoStack':'_redoStack'];
        const other=()=>w[op==='undo'?'_redoStack':'_undoStack'];
        const labels=(stack)=>stack.map(e=>e.label);
        const step=(label,extra={})=>({sceneId:'scene',label,
            snapshot:{scene_id:'scene',name:`before ${label}`,clips:[]},
            postSnapshot:{scene_id:'scene',name:`after ${label}`,clips:[]},...extra});
        bus.configureNotifications({info:0});
        let shown=[]; bus.subscribe(list=>{shown=list;});
        const notices=()=>shown.map(n=>[n.tier,n.source,n.message,n.count]);
        const conflict=(code='scene_merge_conflict')=>()=>new Response(JSON.stringify(
            {error:'Scene changed elsewhere at video_lane_family[x].name.',code}),{status:409});
        const ok=(target)=>new Response(JSON.stringify({scene:target}));
        const answers={}; const sent=[];
        globalThis.fetch=async(url,init)=>{
            if(url.endsWith('/restore-token')) return new Response(JSON.stringify(
                {restore_token:`t${sent.length}`,merged_write_fields:[],merged_derived_fields:[]}));
            if(init?.method!=="PUT") { if(!/restore-token/.test(url)) return new Response(JSON.stringify(w.activeScene)); return new Response(JSON.stringify(globalThis.receipt||{status:"pending"})); }
            const target=JSON.parse(init.body).target_scene, label=target.name.replace('before ','');
            sent.push(label);
            const list=answers[label]||[];
            const answer=list.length>1?list.shift():list[0];
            return answer?answer(target):ok(target);
        };
        const press=(options)=>w[`_${op}`](options);
        const OFFER=`Scene changed elsewhere at video_lane_family[x].name. Press ${Op} again to skip it.`;
        const PLAIN='Scene changed elsewhere at video_lane_family[x].name.';
        const offered=()=>shown.some(n=>/again to skip/.test(n.message));
    """ + body)


@BOTH
@pytest.mark.parametrize("code", ["scene_merge_conflict", "scene_merge_invalid"])
def test_the_second_press_skips_and_the_step_below_is_reachable(operation, code):
    _run(operation, """
        own().push(step('mute'),step('lane'));
        answers.lane=[conflict(""" + repr(code) + """)];
        await press();
        assert.deepEqual(labels(own()),['mute','lane']);
        assert.deepEqual(notices(),[['warning',`${op}-refused`,OFFER,1]]);
        await press();
        // Dropped, nothing else undone by that press, nothing left to reverse it.
        assert.deepEqual(labels(own()),['mute']);
        assert.deepEqual(labels(other()),[]);
        assert.deepEqual(sent,['lane','lane']);
        assert.deepEqual(notices(),[['info',`${op}-skipped`,'Skipped: lane.',1]]);
        assert.equal(shown[0].detail,`It was not ${op==='undo'?'undone':'redone'}.`);
        assert.deepEqual(refusals.map(r=>[r.gate,r.entry_discarded]),
            [['restore_failed',false],['restore_failed',true]]);
        await press();
        assert.deepEqual(sent,['lane','lane','mute']);
        assert.deepEqual(labels(own()),[]);
        assert.deepEqual(labels(other()),['mute']);
        assert.equal(w._queuedHistoryOperationCount,0);
    """)


@BOTH
def test_the_second_press_applies_the_step_when_the_conflict_is_gone(operation):
    _run(operation, """
        own().push(step('lane'));
        answers.lane=[conflict(),ok];
        await press(); await press();
        assert.deepEqual(labels(own()),[]);
        assert.deepEqual(labels(other()),['lane']);
        assert.deepEqual(notices(),[],'the refusal it answered is gone');
        assert.equal(w._refusedHistoryStep,null);
    """)


@BOTH
def test_two_refused_steps_are_each_warned_about(operation):
    _run(operation, """
        own().push(step('a'),step('b'));
        answers.a=[conflict()]; answers.b=[conflict()];
        await press(); await press(); await press();
        assert.deepEqual(labels(own()),['a'],'the skip of b is not a warning about a');
        await press();
        assert.deepEqual(labels(own()),[]);
    """)


REFUSALS = {
    "other code": "()=>new Response(JSON.stringify({error:'no',code:'identity_mismatch'}),{status:409})",
    "kept changing": "()=>new Response(JSON.stringify({error:'x',code:'project_version_conflict'}),{status:409})",
    "no code": "()=>new Response(JSON.stringify({error:'no'}),{status:400})",
    "not found": "()=>new Response(JSON.stringify({error:'gone'}),{status:404})",
    "lost answer": "()=>{throw new Error('Failed to fetch');}",
    "server error": "()=>new Response(JSON.stringify({error:'x',code:'scene_merge_conflict'}),{status:500})",
}


@BOTH
@pytest.mark.parametrize("kind", REFUSALS)
def test_any_other_failure_never_skips(operation, kind):
    _run(operation, """
        globalThis.setTimeout=((real)=>(fn,_ms)=>real(fn,0))(setTimeout);
        own().push(step('mute'),step('lane'));
        answers.lane=[""" + REFUSALS[kind] + """];
        for(let i=0;i<4;i++) {
            await press();
            assert.deepEqual(labels(own()),['mute','lane']);
            assert.equal(offered(),false);
            assert.equal(w._refusedHistoryStep,null);
        }
        assert.deepEqual([...new Set(sent)],['lane']);
    """)


@BOTH
def test_a_different_failure_between_two_conflicts_warns_again(operation):
    _run(operation, """
        own().push(step('lane'));
        answers.lane=[conflict(),()=>new Response(JSON.stringify({error:'no'}),{status:400}),conflict()];
        await press(); await press(); await press();
        assert.deepEqual(labels(own()),['lane']);
        assert.equal(offered(),true);
        await press();
        assert.deepEqual(labels(own()),[]);
    """)


@BOTH
def test_a_conflict_learned_from_the_receipt_counts(operation):
    _run(operation, """
        globalThis.receipt={status:'refused',error:PLAIN,code:'scene_merge_conflict'};
        own().push(step('lane'));
        answers.lane=[()=>{throw new Error('Failed to fetch');}];
        await press();
        assert.equal(offered(),true);
        await press();
        assert.deepEqual(labels(own()),[]);
    """)


@BOTH
def test_a_new_edit_between_the_presses_warns_again_and_withdraws_the_offer(operation):
    _run(operation, """
        own().push(step('lane'));
        answers.lane=[conflict()];
        await press();
        assert.deepEqual(notices().map(n=>n[2]),[OFFER]);
        // A Redo stack does not survive an edit, so put its step back.
        const kept=own().at(-1);
        w.constructor.prototype._pushUndo.call(w,'edit');
        assert.deepEqual(notices().map(n=>n[2]),[PLAIN],'the notice stays, without the offer');
        w._undoStack=w._undoStack.filter(e=>e.label!=='edit');
        if(op==='redo') w._redoStack=[kept];
        await press();
        assert.deepEqual(labels(own()),['lane']);
        assert.equal(offered(),true);
        await press();
        assert.deepEqual(labels(own()),[]);
    """)


def test_a_pending_edit_withdraws_the_offer_when_it_commits():
    _run("undo", """
        own().push(step('lane'));
        answers.lane=[conflict()];
        await press();
        const edit=w.constructor.prototype._pushUndo.call(w,'apply',{pending:true});
        assert.equal(offered(),true,'nothing committed yet');
        w._commitUndoEntry(edit);
        assert.equal(offered(),false);
        assert.equal(w._refusedHistoryStep,null);
    """)


@pytest.mark.parametrize("push", [
    "w._pushProjectDependencyUndo('x',{})", "w._pushPromptIdentityUndo('x',{},{})",
    "w._pushReferenceUndo('x',[],[])"])
def test_every_kind_of_edit_withdraws_the_offer(push):
    _run("undo", """
        own().push(step('lane'));
        answers.lane=[conflict()];
        await press();
        """ + push + """;
        assert.equal(w._refusedHistoryStep,null);
        assert.equal(offered(),false);
    """)


@BOTH
def test_the_other_direction_between_the_presses_warns_again(operation):
    _run(operation, """
        own().push(step('lane')); other().push(step('back'));
        answers.lane=[conflict()];
        await press();
        await w[op==='undo'?'_redo':'_undo']();
        assert.deepEqual(notices().map(n=>n[2]),[PLAIN]);
        // Its reverse entry went on top; take it away to meet the refused step again.
        w[op==='undo'?'_undoStack':'_redoStack']=own().filter(e=>e.label==='lane');
        await press();
        assert.deepEqual(labels(own()),['lane']);
        assert.equal(offered(),true);
    """)


@BOTH
def test_a_press_made_before_the_refusal_is_not_the_skip(operation):
    """Two fast presses claim two steps, as before; only a press after the notice skips."""
    _run(operation, """
        own().push(step('mute'),step('lane'));
        answers.lane=[conflict()];
        await Promise.all([press(),press()]);
        assert.deepEqual(sent,['lane','mute']);
        assert.deepEqual(labels(own()),['lane']);
        assert.deepEqual(labels(other()),['mute']);
        assert.equal(offered(),false,'the second step ran after it');
        await press();
        assert.deepEqual(labels(own()),['lane']);
        assert.equal(offered(),true);
        await press();
        assert.deepEqual(labels(own()),[]);
        assert.deepEqual(labels(other()),['mute']);
    """)


@BOTH
def test_one_steps_refusal_is_not_a_warning_about_the_next(operation):
    """Two fast presses, both refused: neither was warned about, so neither is dropped."""
    _run(operation, """
        own().push(step('a'),step('b'));
        answers.a=[conflict()]; answers.b=[conflict()];
        await Promise.all([press(),press()]);
        assert.deepEqual(sent,['b','a']);
        assert.deepEqual(labels(own()),['a','b']);
        assert.equal(offered(),false,'the next press reaches b, not a');
        await press(); await press();
        assert.deepEqual(labels(own()),['a']);
    """)


@BOTH
def test_a_press_after_the_skip_was_sent_reaches_the_step_below(operation):
    _run(operation, """
        own().push(step('mute'),step('lane'));
        answers.lane=[conflict()];
        await press();
        await Promise.all([press(),press()]);
        assert.deepEqual(sent,['lane','lane','mute']);
        assert.deepEqual(labels(own()),[]);
        assert.deepEqual(labels(other()),['mute']);
    """)


def test_a_redo_queued_behind_a_skip_finds_nothing_to_redo():
    _run("undo", """
        own().push(step('lane'));
        answers.lane=[conflict()];
        await press();
        const skip=press(), redo=w._redo();
        await skip; assert.equal(await redo,false);
        assert.deepEqual(labels(w._undoStack),[]);
        assert.deepEqual(labels(w._redoStack),[]);
        assert.equal(refusals.at(-1).gate,'history_dependency_unresolved');
        assert.equal(w._queuedHistoryOperationCount,0);
    """)


@BOTH
def test_a_held_key_never_skips(operation):
    _run(operation, """
        own().push(step('lane'));
        answers.lane=[conflict()];
        await press();
        for(let i=0;i<5;i++) assert.equal(await press({repeat:true}),false);
        assert.deepEqual(sent,['lane'],'the repeats sent nothing');
        assert.deepEqual(labels(own()),['lane']);
        assert.equal(offered(),true);
        assert.equal(own()[0].claimedBy,undefined);
        await press({repeat:false});
        assert.deepEqual(labels(own()),[]);
    """)


def test_the_shortcuts_say_whether_the_key_is_held():
    assert "void this._undo({ repeat: e.repeat });" in WIDGET
    assert "void this._redo({ repeat: e.repeat });" in WIDGET


@BOTH
def test_an_edit_made_while_the_step_was_out_is_not_offered_away(operation):
    """For Undo the next press would reach the edit; for Redo the edit stands on the
    other stack and the claimed step survives it. Neither may offer a skip."""
    _run(operation, """
        own().push(step('lane'));
        answers.lane=[()=>{w.constructor.prototype._pushUndo.call(w,'newer');return conflict()();}];
        await press();
        assert.equal(offered(),false);
        assert.deepEqual(notices().map(n=>n[2]),[PLAIN]);
        assert.equal(w._refusedHistoryStep,null);
        assert.ok(own().some(e=>e.label==='lane'));
    """)


@BOTH
def test_an_unfinished_edit_above_the_step_is_not_offered_away(operation):
    """A pending edit has not moved the revision; on the Undo stack it is simply above."""
    _run(operation, """
        own().push(step('lane'));
        answers.lane=[()=>{own().push(step('newer',{pending:true}));return conflict()();}];
        await press();
        assert.equal(offered(),false);
    """)


@BOTH
def test_closing_the_editor_while_the_step_was_out_offers_nothing(operation):
    _run(operation, """
        own().push(step('lane'));
        answers.lane=[()=>{w._destroyed=true;return conflict()();}];
        await press();
        assert.equal(offered(),false);
        assert.equal(w._refusedHistoryStep,null);
    """)


@BOTH
def test_a_scene_switch_during_the_second_press_leaves_the_new_stack_alone(operation):
    _run(operation, """
        own().push(step('lane'));
        answers.lane=[conflict(),()=>{w._undoStack=[step('x')];w._redoStack=[step('x')];return conflict()();}];
        await press(); await press();
        assert.deepEqual(labels(own()),['x']);
        assert.equal(offered(),false);
        assert.ok(!notices().some(n=>/Skipped/.test(n[2])));
    """)


@BOTH
def test_a_step_claimed_by_another_press_is_not_dropped(operation):
    _run(operation, """
        const lane=step('lane'); own().push(lane);
        answers.lane=[conflict()];
        await press();
        lane.claimedBy={id:'other'};
        await w[`_run${Op}WithinGesture`](null,lane,{id:'mine'},null,null,null,null);
        assert.deepEqual(labels(own()),['lane']);
    """)


@BOTH
def test_an_edit_queued_behind_a_skip_reads_the_scene_again(operation):
    _run(operation, """
        own().push(step('lane'));
        answers.lane=[conflict()];
        await press();
        const context={scenes:new Map([['scene',{scene_id:'scene'}]])};
        await w[`_run${Op}WithinGesture`](null,own()[0],null,null,null,context,null);
        assert.deepEqual(labels(own()),[]);
        assert.equal(w._historyOrderContextNeedsResolution(context,'scene'),true);
    """)


@BOTH
def test_a_press_refused_at_a_gate_withdraws_the_offer(operation):
    _run(operation, """
        own().push(step('lane'));
        answers.lane=[conflict()];
        await press();
        w._sceneHistoryLifecycleOwner={};
        assert.equal(await press(),false);
        assert.equal(offered(),false);
        w._sceneHistoryLifecycleOwner=null;
        await press();
        assert.deepEqual(labels(own()),['lane'],'warned again first');
        assert.equal(offered(),true);
        w._hasPendingHistoryCommit=()=>true;
        w._undoStack.unshift(step('apply',{pending:true}));
        assert.equal(await press(),false);
        assert.equal(offered(),false,'that press cannot skip');
    """)


@BOTH
def test_a_press_that_could_not_be_queued_withdraws_the_offer(operation):
    _run(operation, """
        own().push(step('lane'));
        answers.lane=[conflict()];
        await press();
        w._projectMutationQueue={enqueue:()=>Promise.reject(new Error('queue closed')),isBusy:()=>false};
        assert.equal(await press(),false);
        assert.equal(offered(),false);
    """)


LISTS = {
    "nothing": "{}",
    "identity change": "{promptIdentityChange:{type:'x'},inversePromptIdentityChange:{type:'x'}}",
    "references": "{referenceOperations:[{type:'x'}],inverseReferenceOperations:[{type:'x'}]}",
    "created identity": "{promptIdentityCreateIntents:[{type:'t',unit:{semantic_unit_id:'u1',name:''}}]}",
}


@BOTH
@pytest.mark.parametrize("wrote", LISTS)
@pytest.mark.parametrize("below", [True, False])
def test_a_step_over_a_whole_list_step_is_skipped_only_if_it_wrote_no_list(operation, wrote, below):
    """`project_dependencies` puts back every Subject as it was before its own step, so it
    must never run while a later step that wrote Subjects is still applied."""
    held = below and wrote != "nothing"
    _run(operation, """
        w._applyReferenceHistoryOperations=async()=>{}; w._applyPromptIdentityChange=async()=>{};
        w._fetchReferences=async()=>true;
        w._promptSemanticUnits=[{semantic_unit_id:'u1',name:'',handle:'u',order:0}];
        own().push(step('mute'));
        if(""" + str(below).lower() + """) own().push(step('edit Subject',{kind:'project_dependencies'}));
        own().push(step('apply',""" + LISTS[wrote] + """));
        answers.apply=[conflict()];
        const held=""" + str(held).lower() + """;
        await press();
        assert.equal(offered(),!held);
        await press();
        assert.equal(own().at(-1).label==='apply',held);
    """)


def test_a_whole_list_step_on_the_other_stack_holds_nothing():
    _run("undo", """
        own().push(step('apply',{promptIdentityCreateIntents:[{type:'t',unit:{semantic_unit_id:'u1'}}]}));
        answers.apply=[conflict()];
        w._redoStack.push(step('edit Subject',{kind:'project_dependencies'}));
        await press(); await press();
        assert.deepEqual(labels(own()),[]);
    """)


@BOTH
def test_a_scene_switch_while_the_step_was_out_drops_the_offer(operation):
    _run(operation, """
        own().push(step('lane'));
        answers.lane=[()=>{w._undoStack=[];w._redoStack=[];return conflict()();}];
        await press();
        assert.equal(offered(),false);
        assert.equal(w._refusedHistoryStep,null);
    """)


def test_every_place_that_empties_the_stacks_or_closes_the_editor_withdraws_the_offer():
    resets = list(re.finditer(
        r"this\._undoStack = \[\];[^\n]*\n\s*this\._redoStack = \[\];\n([^\n]*)", WIDGET))
    assert len(resets) == 3
    # The first is the constructor.
    assert all("this._releaseRefusedHistoryStep?.();" in found.group(1) for found in resets[1:])
    destroy = WIDGET[WIDGET.index("\n    destroy() {"):][:1600]
    assert "this._releaseRefusedHistoryStep?.();" in destroy


LEGS = """
        const applied=[]; let failCompensation=false;
        w._applyReferenceHistoryOperations=async(ops,label)=>{
            if(/^restore failed/.test(label)&&failCompensation) throw new Error('cannot take back');
            applied.push(ops[0].type);
        };
        w._applyPromptIdentityChange=async(change,label)=>{
            if(/^restore failed/.test(label)&&failCompensation) throw new Error('cannot take back');
            applied.push(change.type);
        };
"""


@BOTH
@pytest.mark.parametrize("leg,extra", [
    ("reference", "{referenceOperations:[{type:'do'}],inverseReferenceOperations:[{type:'take back'}]}"),
    ("identity", "{promptIdentityChange:{type:'do'},inversePromptIdentityChange:{type:'take back'}}"),
])
def test_a_step_with_a_leg_is_skipped_only_when_the_leg_was_taken_back(operation, leg, extra):
    _run(operation, LEGS + """
        own().push(step('attach',""" + extra + """));
        answers.attach=[conflict()];
        failCompensation=true;
        for(let i=0;i<3;i++) {
            await press();
            assert.deepEqual(labels(own()),['attach']);
            assert.equal(offered(),false);
        }
        assert.deepEqual(applied,['do'],'a leg left applied is not applied twice');
        failCompensation=false;
        await press();
        assert.deepEqual(labels(own()),['attach']);
        assert.equal(offered(),true);
        await press();
        assert.deepEqual(labels(own()),[]);
        assert.deepEqual(applied,['do','take back','do','take back']);
    """)


@pytest.mark.parametrize("cleanup_fails", [True, False])
def test_a_redo_that_created_identities_is_skipped_only_when_they_were_cleaned_up(cleanup_fails):
    _run("redo", """
        const unit={semantic_unit_id:'u1',name:'A'};
        const intent={type:'create_prompt_semantic_unit',unit};
        w._fetchReferences=async()=>true; w._promptSemanticUnits=[]; w._promptSpeakers=[];
        w._adoptPromptIdentitiesFromMutation=()=>{};
        const runs=[]; let cleanupFails=""" + str(cleanup_fails).lower() + """;
        w._runSceneMutation=async(operations,options)=>{
            runs.push(options.key.split(':')[2]);
            if(options.key.includes('compensation')&&cleanupFails) throw new Error('cannot clean up');
            return {payload:{results:operations.map(o=>({type:o.type,created:true,unit}))}};
        };
        own().push(step('speaker',{promptIdentityCreateIntents:[intent]}));
        answers.speaker=[conflict()];
        await press();
        assert.deepEqual(runs,['identity-redo','identity-redo-compensation']);
        assert.equal(offered(),!cleanupFails);
        await press();
        assert.deepEqual(labels(own()),cleanupFails?['speaker']:[]);
    """)


def test_a_redo_that_only_reused_identities_is_skippable():
    """Resolving creates applied nothing, so it does not hold the step."""
    _run("redo", """
        const unit={semantic_unit_id:'u1',name:'A',handle:'a',order:0};
        w._fetchReferences=async()=>true; w._promptSemanticUnits=[unit]; w._promptSpeakers=[];
        w._runSceneMutation=async()=>{throw new Error('nothing to create');};
        own().push(step('speaker',{promptIdentityCreateIntents:[
            {type:'create_prompt_semantic_unit',unit:{semantic_unit_id:'u1',name:'A'}}]}));
        answers.speaker=[conflict()];
        await press();
        assert.equal(offered(),true);
        await press();
        assert.deepEqual(labels(own()),[]);
    """)


@BOTH
@pytest.mark.parametrize("kind", ["project_dependencies", "prompt_identity", "reference_change"])
def test_the_steps_that_never_reach_the_merge_are_unchanged(operation, kind):
    _run(operation, """
        const refuse=async()=>{const e=new Error('no');e.code='scene_merge_conflict';throw e;};
        w._restoreProjectDependencies=refuse; w._applyPromptIdentityChange=refuse;
        w._applyReferenceHistoryOperations=refuse; w._captureProjectDependencies=()=>({});
        own().push(step('x',{kind:""" + repr(kind) + """,change:{},inverseChange:{},
            operations:[],inverseOperations:[]}));
        for(let i=0;i<3;i++) await press();
        assert.deepEqual(labels(own()),['x']);
        assert.equal(offered(),false);
    """)


@BOTH
def test_a_refusal_without_a_full_stop_still_reads_as_two_sentences(operation):
    _run(operation, """
        own().push(step('lane'));
        answers.lane=[()=>new Response(JSON.stringify({error:'Merged scene data is invalid: x',
            code:'scene_merge_invalid'}),{status:409})];
        await press();
        assert.equal(shown[0].message,`Merged scene data is invalid: x. Press ${Op} again to skip it.`);
    """)
