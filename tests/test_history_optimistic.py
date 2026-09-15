"""Exercise history's real restore lifecycle with controlled network responses."""
import pytest
from test_project_mutation_queue import _run_gesture_node


def _run_history_node(body):
    _run_gesture_node('''
        globalThis.document = {activeElement:null};
        const capabilities = {merged_write_fields:['name','clips','global_channel_docs'],
            merged_derived_fields:['global_channels','prompt']};
        const entry = () => ({sceneId:'scene', label:'edit',
            snapshot:{scene_id:'scene', name:'before', clips:[]},
            postSnapshot:{scene_id:'scene', name:'after', clips:[]},
            postSnapshotProjectVersion:'v1', postSnapshotVersionSource:'acknowledged_scene'});
        function makeHistoryWidget() {
            const w = makeWidget();
            Object.assign(w, {
                _undoStack:[], _redoStack:[], scenes:[], _authoritativeSceneSeq:0,
                activeScene:structuredClone(entry().postSnapshot),
                _keyboardDebug:()=>{}, _recordHistoryRefusal:()=>{},
                _historyObservedProjectVersion:()=> 'v1',
                _activateGraphUndoSuppression:()=>{},
                _replaceSceneInList:()=>{},
                _setActiveScene(scene, options={}) { this.activeScene=scene; if(!options.optimisticHistory) this._authoritativeSceneSeq++; },
            });
            return w;
        }
    ''' + body)


def test_all_optimistic_gates_and_derived_mirrors():
    _run_history_node('''
        const w=makeHistoryWidget();
        assert.equal(w._historyOptimisticEligibility(entry(), capabilities).would_paint,true);
        for (const [patch,reason] of [
            [{kind:'reference_change'},'typed_entry'],
            [{referenceOperations:[{}]},'auxiliary_operations'],
            [{promptIdentityChange:{}},'auxiliary_operations'],
            [{promptIdentityCreateIntents:[{}]},'auxiliary_operations'],
            [{postSnapshotProjectVersion:null},'no_authoritative_base'],
            [{postSnapshotProjectVersion:'older'},'version_mismatch'],
            [{snapshot:{scene_id:'wrong'}},'scene_mismatch'],
        ]) assert.equal(w._historyOptimisticEligibility({...entry(),...patch},capabilities).skip_reason,reason);
        w.isDragging=true;
        assert.equal(w._historyOptimisticEligibility(entry(),capabilities).skip_reason,'dragging');
        w.isDragging=false;w._timelineMutationDepth=1;
        assert.equal(w._historyOptimisticEligibility(entry(),capabilities).skip_reason,'timeline_mutation');
        w._timelineMutationDepth=0;
        assert.equal(w._historyOptimisticEligibility(entry(),null).skip_reason,'missing_merge_capabilities');
        const derived=entry(); derived.snapshot.prompt='derived';derived.snapshot.global_channels={main:'derived'};
        assert.equal(w._historyOptimisticEligibility(derived,capabilities).would_paint,true);
        derived.snapshot.fps=30;
        assert.equal(w._historyOptimisticEligibility(derived,capabilities).skip_reason,'outside_write_set');
    ''')


@pytest.mark.parametrize('operation', ['Undo', 'Redo'])
def test_plain_history_failure_tombstones_order_context(operation):
    _run_history_node('''
        const w=makeHistoryWidget(), e=entry();
        w._restoreScene=async()=>{throw new Error('refused');};
        const context={scenes:new Map([['scene',e.postSnapshot]])};
        w._''' + operation.lower() + '''Stack.push(e);
        await w._run''' + operation + '''WithinGesture(null,null,null,null,null,context);
        assert.equal(context.scenes.get('scene'),null);
    ''')


def test_paint_occurs_after_token_before_restore():
    _run_history_node('''
        const w=makeHistoryWidget(), e=entry(); let calls=0, adopted=0;
        w._setActiveScene=()=>{adopted++;};
        globalThis.fetch=async(url,init)=>{
            calls++;
            if(calls===1) return new Response(JSON.stringify({restore_token:'token',...capabilities}));
            assert.equal(adopted,1);
            const diagnostics=window.__SONDER_CANVAS_DIAG.events.filter(e=>e.kind==='history_optimistic_paint');
            assert.equal(diagnostics.length,1);
            assert.equal(diagnostics[0].would_paint,true);
            return new Response(JSON.stringify({scene:e.snapshot}));
        };
        await w._restoreScene(e.sceneId,e.snapshot,e.postSnapshot,'',null,{entry:e});
        assert.equal(adopted,2);
    ''')


def test_stamp_uses_acknowledged_response_version_not_newer_observation():
    _run_history_node("""
        const w=makeHistoryWidget(), e=entry(); delete e.postSnapshot;
        w._undoStack.push(e);
        w._historyObservedProjectVersion=()=> 'v3';
        const stamp=EditorWidget.prototype._stampHistoryPostSnapshot;
        stamp.call(w,e,{scene_id:'scene',name:'v2'},new Response('{}',{
            headers:{'X-Sonder-Project-Modified-At':'v2'}}));
        assert.equal(e.postSnapshotProjectVersion,'v2');
        assert.equal(w._historyOptimisticEligibility(e,capabilities).skip_reason,'version_mismatch');
    """)


def test_restore_stamp_survives_newer_get_during_response_body():
    _run_history_node("""
        const w=makeHistoryWidget(), e=entry(); w._undoStack.push(e);
        let count=0;
        globalThis.fetch=async()=>{
            if(++count===1) return new Response(JSON.stringify({restore_token:'token',...capabilities}));
            return {ok:true,status:200,headers:new Headers({'X-Sonder-Project-Modified-At':'v2'}),
                json:async()=>{w._historyObservedProjectVersion=()=> 'v3';return {scene:e.snapshot};}};
        };
        await w._runUndoWithinGesture();
        assert.equal(w._redoStack.at(-1).postSnapshotProjectVersion,'v2');
    """)


def test_paint_clones_snapshot_and_rollback_rearms_graph_suppression():
    _run_history_node("""
        const w=makeHistoryWidget(), e=entry(), state={entry:e}, arms=[];
        w._activateGraphUndoSuppression=reason=>arms.push(reason);
        w._paintHistoryOptimistically(state);
        assert.notEqual(w.activeScene,e.snapshot);
        w.activeScene.name='local mutation';
        assert.equal(e.snapshot.name,'before');
        w._rollbackHistoryOptimisticPaint(state);
        assert.equal(w.activeScene.name,'after');
        assert.notEqual(w.activeScene,e.postSnapshot);
        assert.deepEqual(arms,['editor-history-optimistic','editor-history-rollback']);
    """)


@pytest.mark.parametrize('mode', ['authoritative', 'drag', 'timeline_mutation', 'project_switch'])
def test_rollback_respects_newer_authority_and_drag_objects(mode):
    _run_history_node("""
        const w=makeHistoryWidget(), e=entry(), state={entry:e};
        w._paintHistoryOptimistically(state);
        const painted=w.activeScene;
    """ + {
        'authoritative': "w._setActiveScene({scene_id:'scene',name:'other author'});",
        'drag': 'w.isDragging=true;',
        'timeline_mutation': 'w._timelineMutationDepth=1;',
        'project_switch': "w.projectDir='other project';",
    }[mode] + """
        const current=w.activeScene;
        w._rollbackHistoryOptimisticPaint(state);
        assert.equal(w.activeScene,current);
    """ + ('assert.equal(w._pendingScenesRefresh,true);' if mode in {'drag','timeline_mutation'} else ''))


@pytest.mark.parametrize('operation', ['Undo', 'Redo'])
@pytest.mark.parametrize('failure', ['plain', 'conflict_refresh', 'ambiguous', 'adopt_throw'])
def test_real_restore_failure_rolls_back_before_notice(operation, failure):
    _run_history_node("""
        const w=makeHistoryWidget(), e=entry(), context={scenes:new Map()};
        w._""" + operation.lower() + """Stack.push(e);
        const failure=""" + repr(failure) + """;
        let calls=0;
        w._fetchScenes=async()=>w._setActiveScene({scene_id:'scene',name:'other author'});
        const paint=w._paintHistoryOptimistically.bind(w);
        w._paintHistoryOptimistically=state=>{
            paint(state);
            if(failure==='adopt_throw') {
                const set=w._setActiveScene.bind(w);
                w._setActiveScene=(scene,options)=>{if(!options?.optimisticHistory)throw new Error('adopt failed');set(scene,options);};
            }
        };
        globalThis.fetch=async(url,init)=>{
            if(url.endsWith('/restore-token')) return new Response(JSON.stringify({restore_token:'token',...capabilities}));
            if(init?.method==='PUT') {
                assert.equal(w.activeScene.name,'before');
                if(failure==='ambiguous') throw new Error('network');
                if(failure==='adopt_throw') return new Response(JSON.stringify({scene:e.snapshot}));
                return new Response(JSON.stringify({error:'refused',code:'scene_merge_conflict'}),{status:failure==='plain'?400:409});
            }
            return new Response(JSON.stringify({status:'pending'}));
        };
        await w._run""" + operation + """WithinGesture(null,null,null,null,null,context);
        assert.equal(w.activeScene.name,failure==='conflict_refresh'?'other author':'after');
        assert.equal(w._historyOrderContextNeedsResolution(context,'scene'),true);
    """)


def test_failed_paint_rebases_edit_authored_during_restore_window():
    _run_history_node("""
        const w=makeHistoryWidget(), e=entry();
        w._undoStack=[e];w._maxUndoSteps=50;w._clearRedoForNewEdit=()=>{};
        const context=w._beginHistoryOrderContext('undo',1);
        let queued, authored, beforeRebase, seenBaseline, calls=0;
        w._stampHistoryPostSnapshot=EditorWidget.prototype._stampHistoryPostSnapshot;
        globalThis.fetch=async(url,init)=>{
            if(url.endsWith('/restore-token')) return new Response(JSON.stringify({restore_token:'token',...capabilities}));
            if(init?.method==='PUT') {
                assert.equal(w.activeScene.name,'before');
                EditorWidget.prototype._pushUndo.call(w,'following edit');
                authored=w._undoStack.at(-1);
                beforeRebase=structuredClone(authored.snapshot);
                w.activeScene.name='authored while undo pending';
                queued=w._queueProjectMutation({key:'following',coalesce:false,
                    historyEntry:authored,historyOrderContext:context,
                    refreshScenes:false,intent:{projectId:'project',sceneId:'scene',operations:[]},
                    run:async()=>{
                        seenBaseline=structuredClone(authored.snapshot);
                        return {payload:{scene:{...e.postSnapshot,name:'following saved'}},
                            response:new Response('{}',{headers:{'X-Sonder-Project-Modified-At':'v2'}})};
                    }});
                return new Response(JSON.stringify({error:'forced refusal'}),{status:400});
            }
            assert.ok(url.endsWith('/scenes/scene'));
            calls++;
            return new Response(JSON.stringify(e.postSnapshot));
        };
        await w._projectMutationQueue.enqueue({key:'undo',coalesce:false,
            run:()=>w._runUndoWithinGesture(null,null,null,null,null,context)});
        await queued;
        assert.equal(beforeRebase.name,'before');
        assert.equal(seenBaseline.name,'after');
        assert.equal(authored.snapshot.name,'after');
        assert.equal(authored.postSnapshot.name,'following saved');
        assert.equal(calls,1);
        // Later legitimate Undo sends the real baseline, not the painted fiction.
        w._restoreScene=async(id,target,base)=>{
            assert.equal(target.name,'after');assert.equal(base.name,'following saved');return target;
        };
        w._undoStack=[authored];
        await w._runUndoWithinGesture();
        assert.equal(w._redoStack.at(-1).postSnapshot.name,'after');
    """)


def test_canonical_adoption_defers_when_drag_begins_after_paint():
    _run_history_node("""
        const w=makeHistoryWidget(),e=entry();let dragObject;
        globalThis.fetch=async(url,init)=>{
            if(url.endsWith('/restore-token')) return new Response(JSON.stringify({restore_token:'token',...capabilities}));
            dragObject=w.activeScene;w.isDragging=true;
            return new Response(JSON.stringify({scene:{...e.snapshot,name:'canonical'}}));
        };
        const scene=await w._restoreScene('scene',e.snapshot,e.postSnapshot,'',null,{entry:e});
        assert.equal(scene.name,'canonical');
        assert.equal(w.activeScene,dragObject);
        assert.equal(w._pendingScenesRefresh,true);
        w.isDragging=false;
        let refreshes=0;w._fetchScenes=async()=>{refreshes++;};
        w._flushDeferredDragState();
        assert.equal(refreshes,1);
    """)


@pytest.mark.parametrize('new_selection', [False, True])
def test_failed_paint_restores_only_its_own_removed_selection(new_selection):
    _run_history_node("""
        const w=makeHistoryWidget(), e=entry();
        e.postSnapshot.clips=[{clip_id:'c'}];w.activeScene=e.postSnapshot;
        w._findSceneItemBySelection=(type,id)=>w.activeScene.clips.find(c=>c.clip_id===id)
            ? {type,id,data:w.activeScene.clips.find(c=>c.clip_id===id)}:null;
        w.selectedItem={type:'clip',id:'c',data:e.postSnapshot.clips[0]};w.selectedItems=[w.selectedItem];
        w._itemEditorEl={};w._hideItemEditor=()=>{w._itemEditorEl=null;};
        w._showItemEditor=()=>{w._itemEditorEl={};};
        w._setActiveScene=(scene)=>{w.activeScene=scene;w._reconcileSelection();};
        const state={entry:e};w._paintHistoryOptimistically(state);
        assert.equal(w.selectedItems.length,0);
    """ + ('w._clearSelection();' if new_selection else '') + """
        w._rollbackHistoryOptimisticPaint(state);
        assert.equal(w.selectedItems.length,""" + ('0' if new_selection else '1') + """);
    """ + ('' if new_selection else "assert.equal(w.selectedItem.id,'c');assert.ok(w._itemEditorEl);") )


def test_redo_failure_rolls_back_displayed_scene_not_pinned_merge_base():
    _run_history_node("""
        const w=makeHistoryWidget(), e=entry();w._undoStack.push(e);
        const concurrent={clip_id:'concurrent',timeline_start_frame:12,timeline_end_frame:20};
        let reject=false;
        globalThis.fetch=async(url,init)=>{
            if(url.endsWith('/restore-token')) return new Response(JSON.stringify({restore_token:'token',...capabilities}));
            if(reject) return new Response(JSON.stringify({error:'refused'}),{status:400});
            return new Response(JSON.stringify({scene:{...e.snapshot,clips:[concurrent]}}),{
                headers:{'X-Sonder-Project-Modified-At':'v1'}});
        };
        await w._runUndoWithinGesture();
        assert.deepEqual(w.activeScene.clips,[concurrent]);
        assert.deepEqual(w._redoStack.at(-1).postSnapshot.clips,[]);
        const displayed=structuredClone(w.activeScene);
        reject=true;
        await w._runRedoWithinGesture();
        assert.deepEqual(w.activeScene,displayed);
        assert.deepEqual(w._redoStack.at(-1).postSnapshot.clips,[]);
    """)


def test_conflict_refresh_keeps_canonical_scene_and_restores_owned_selection():
    _run_history_node("""
        const w=makeHistoryWidget(),e=entry();e.postSnapshot.clips=[{clip_id:'c'}];
        w.activeScene=e.postSnapshot;
        w._findSceneItemBySelection=(type,id)=>w.activeScene.clips.find(c=>c.clip_id===id)
            ? {type,id,data:w.activeScene.clips.find(c=>c.clip_id===id)}:null;
        w.selectedItem={type:'clip',id:'c',data:e.postSnapshot.clips[0]};w.selectedItems=[w.selectedItem];
        w._itemEditorEl={};w._hideItemEditor=()=>{w._itemEditorEl=null;};w._showItemEditor=()=>{w._itemEditorEl={};};
        const set=w._setActiveScene.bind(w);
        w._setActiveScene=(scene,options)=>{set(scene,options);w._reconcileSelection();};
        const state={entry:e};w._paintHistoryOptimistically(state);
        assert.equal(w.selectedItems.length,0);
        const canonical={...e.postSnapshot,name:'other author'};w._setActiveScene(canonical);
        w._rollbackHistoryOptimisticPaint(state);
        assert.equal(w.activeScene,canonical);
        assert.equal(w.selectedItem.id,'c');assert.ok(w._itemEditorEl);
    """)


@pytest.mark.parametrize('switch', ['project', 'scene'])
def test_deferred_selection_cannot_cross_project_or_scene(switch):
    _run_history_node("""
        const w=makeHistoryWidget(),e=entry(),state={entry:e};
        w.selectedItems=[{type:'clip',id:'c'}];w.selectedItem=w.selectedItems[0];
        w._paintHistoryOptimistically(state);
        w.isDragging=true;w._rollbackHistoryOptimisticPaint(state);w.isDragging=false;
        w._findSceneItemBySelection=()=>{throw new Error('Old selection leaked across lifecycle');};
    """ + ("w.projectDir='other';" if switch=='project' else "w.activeSceneId='other';") + """
        w._restoreHistorySelection(state);
    """)
