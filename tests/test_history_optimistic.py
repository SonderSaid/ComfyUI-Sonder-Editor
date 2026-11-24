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
                _replaceSceneInList:()=>{},
                _setActiveScene(scene, options={}) { this.activeScene=scene; if(!options.optimisticHistory) this._authoritativeSceneSeq++; },
            });
            return w;
        }
    ''' + body)


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


def test_without_cached_lists_paint_occurs_after_token_before_restore():
    # The first history action of a session (or after a project switch) has no
    # merge lists yet, so it keeps the post-token paint.
    _run_history_node('''
        const w=makeHistoryWidget(), e=entry(); let calls=0, adopted=0;
        w._setActiveScene=()=>{adopted++;};
        globalThis.fetch=async(url,init)=>{
            calls++;
            if(calls===1) {
                assert.equal(adopted,0,'nothing painted before the token');
                return new Response(JSON.stringify({restore_token:'token',...capabilities}));
            }
            assert.equal(adopted,1);
            const diagnostics=window.__SONDER_CANVAS_DIAG.events.filter(e=>e.kind==='history_optimistic_paint');
            assert.deepEqual(diagnostics.map(d=>[d.stage,d.would_paint]),[['post_token',true]]);
            return new Response(JSON.stringify({scene:e.snapshot}));
        };
        await w._restoreScene(e.sceneId,e.snapshot,e.postSnapshot,'',null,{entry:e});
        assert.equal(adopted,2);
        // The token's lists are kept for the next action.
        assert.deepEqual(w._historyMergeCapabilities,capabilities);
    ''')


# Paint counting: the optimistic `_setActiveScene` calls, which are the paints
# and rollbacks, in order.
_COUNTING = '''
        const shown=[];
        const later=(fn)=>new Promise(r=>setTimeout(()=>r(fn()),0));
        const set=w._setActiveScene.bind(w);
        w._setActiveScene=(scene,options={})=>{shown.push([options.optimisticHistory?'optimistic':'canonical',scene.name]);set(scene,options);};
        const paints=()=>window.__SONDER_CANVAS_DIAG.events.filter(d=>d.kind==='history_optimistic_paint')
            .map(d=>[d.stage,d.would_paint,!!d.pre_painted,!!d.retracted]);
'''


def test_with_cached_lists_the_undo_paints_before_the_token_round_trip():
    _run_history_node('''
        const w=makeHistoryWidget(), e=entry();
        w._historyMergeCapabilities=structuredClone(capabilities);
    ''' + _COUNTING + '''
        globalThis.fetch=async(url,init)=>{
            // Sent first so its round trip overlaps the paint; by the time it
            // answers the prediction is on screen.
            if(url.endsWith('/restore-token')) return later(()=>{
                assert.equal(w.activeScene.name,'before','painted before the token answers');
                return new Response(JSON.stringify({restore_token:'token',...capabilities}));
            });
            assert.equal(w.activeScene.name,'before');
            return new Response(JSON.stringify({scene:{...e.snapshot}}));
        };
        const state={entry:e};
        await w._restoreScene(e.sceneId,e.snapshot,e.postSnapshot,'',null,state);
        // Painted once, kept by the recheck, then the canonical scene.
        assert.deepEqual(shown,[['optimistic','before'],['canonical','before']]);
        assert.deepEqual(paints(),[['pre_token',true,false,false],['post_token',true,true,false]]);
    ''')


def test_a_project_change_elsewhere_keeps_the_prediction():
    # Maintainer decision 2026-09-25 (audit #1): the version is project-wide, so a
    # write elsewhere during the token call (a take landing, a gallery favorite)
    # does not take the prediction back. Canonical adopt brings the other change in.
    _run_history_node('''
        const w=makeHistoryWidget(), e=entry();
        w._historyMergeCapabilities=structuredClone(capabilities);
    ''' + _COUNTING + '''
        globalThis.fetch=async(url,init)=>later(()=>{
            if(url.endsWith('/restore-token')) {
                w._historyObservedProjectVersion=()=> 'v2';
                return new Response(JSON.stringify({restore_token:'token',...capabilities}));
            }
            assert.equal(w.activeScene.name,'before','the prediction stands through the restore');
            return new Response(JSON.stringify({scene:{...e.snapshot,name:'merged'}}));
        });
        const state={entry:e};
        await w._restoreScene(e.sceneId,e.snapshot,e.postSnapshot,'',null,state);
        assert.deepEqual(shown,[['optimistic','before'],['canonical','merged']]);
        assert.deepEqual(paints(),[['pre_token',true,false,false],['post_token',false,true,false]]);
    ''')


def test_fresh_lists_that_disprove_the_prediction_roll_it_back_before_restore():
    # The server no longer restores a field the prediction changed: retract,
    # even though the project also moved (the version check ranks first).
    _run_history_node('''
        const w=makeHistoryWidget(), e=entry();
        w._historyMergeCapabilities=structuredClone(capabilities);
    ''' + _COUNTING + '''
        globalThis.fetch=async(url,init)=>later(()=>{
            if(url.endsWith('/restore-token')) {
                w._historyObservedProjectVersion=()=> 'v2';
                return new Response(JSON.stringify({restore_token:'token',...capabilities,
                    merged_write_fields:['clips','global_channel_docs']}));
            }
            assert.equal(init.method,'PUT');
            assert.equal(w.activeScene.name,'after','rolled back before the restore is sent');
            return new Response(JSON.stringify({scene:{...e.snapshot,name:'merged'}}));
        });
        const state={entry:e};
        await w._restoreScene(e.sceneId,e.snapshot,e.postSnapshot,'',null,state);
        assert.deepEqual(shown,[['optimistic','before'],['optimistic','after'],['canonical','merged']]);
        assert.deepEqual(paints(),[['pre_token',true,false,false],['post_token',false,true,true]]);
        assert.equal(state.painted,false);
    ''')


def test_a_gesture_begun_after_the_prediction_does_not_retract_it():
    _run_history_node('''
        const w=makeHistoryWidget(), e=entry();
        w._historyMergeCapabilities=structuredClone(capabilities);
    ''' + _COUNTING + '''
        globalThis.fetch=async(url,init)=>{
            if(url.endsWith('/restore-token')) return later(()=>{
                w.isDragging=true;
                return new Response(JSON.stringify({restore_token:'token',...capabilities}));
            });
            assert.equal(w.activeScene.name,'before','the prediction stands');
            return new Response(JSON.stringify({scene:{...e.snapshot}}));
        };
        await w._restoreScene(e.sceneId,e.snapshot,e.postSnapshot,'',null,{entry:e});
        assert.deepEqual(paints().at(-1),['post_token',false,true,false]);
        // Adopt defers during the drag, as it always has.
        assert.deepEqual(shown,[['optimistic','before']]);
        assert.equal(w._pendingScenesRefresh,true);
    ''')


@pytest.mark.parametrize('operation', ['Undo', 'Redo'])
def test_a_failed_token_after_a_prediction_rolls_back(operation):
    _run_history_node("""
        const w=makeHistoryWidget(), e=entry(), context={scenes:new Map()};
        w._historyMergeCapabilities=structuredClone(capabilities);
        w._""" + operation.lower() + """Stack.push(e);
        let painted=false;
        globalThis.fetch=(url)=>new Promise(r=>setTimeout(()=>{
            painted=w.activeScene.name==='before';
            r(new Response(JSON.stringify({error:'gone'}),{status:404}));
        },0));
        await w._run""" + operation + """WithinGesture(null,null,null,null,null,context);
        assert.equal(painted,true);
        assert.equal(w.activeScene.name,'after','rolled back by the caller');
    """)


def test_a_retried_or_re_entered_restore_never_pre_paints():
    _run_history_node('''
        const w=makeHistoryWidget(), e=entry();
        w._historyMergeCapabilities=structuredClone(capabilities);
    ''' + _COUNTING + '''
        let puts=0;
        globalThis.fetch=async(url,init)=>{
            if(url.endsWith('/restore-token')) {
                assert.equal(shown.length,0,'the re-entry after an expired token did not pre-paint');
                return new Response(JSON.stringify({restore_token:'fresh',...capabilities}));
            }
            if(++puts===1) {
                assert.equal(shown.length,0,'a retry with its token did not pre-paint');
                return new Response(JSON.stringify({error:'expired',code:'scene_restore_token_expired'}),{status:409});
            }
            return new Response(JSON.stringify({scene:{...e.snapshot}}));
        };
        await w._restoreScene(e.sceneId,e.snapshot,e.postSnapshot,'old-token',null,{entry:e});
        assert.deepEqual(paints().map(p=>p[0]),['post_token','post_token']);
        assert.deepEqual(shown,[['optimistic','before'],['canonical','before']]);
    ''')


def test_the_cached_lists_come_from_the_token_and_leave_with_the_project():
    _run_history_node('''
        const w=makeHistoryWidget(), e=entry();
        globalThis.fetch=async(url)=>url.endsWith('/restore-token')
            ? new Response(JSON.stringify({restore_token:'token',merged_write_fields:['name']}))
            : new Response(JSON.stringify({scene:{...e.snapshot}}));
        await w._restoreScene(e.sceneId,e.snapshot,e.postSnapshot,'',null,{entry:e});
        assert.equal(w._historyMergeCapabilities,undefined,'an incomplete answer is not cached');
        globalThis.fetch=async(url)=>url.endsWith('/restore-token')
            ? new Response(JSON.stringify({restore_token:'token',...capabilities}))
            : new Response(JSON.stringify({scene:{...e.snapshot}}));
        await w._restoreScene(e.sceneId,e.snapshot,e.postSnapshot,'',null,{entry:e});
        assert.deepEqual(w._historyMergeCapabilities,capabilities);
        for (const name of ['_clearStaleReplayState','_updateSceneIdentity','_updateProjectIdentity',
            '_stopPlayback','_clearVideoCache','_sweepRenderCache','_fetchProjectSettings',
            '_renderQueuePanel','_fetchReferences','_clearUnconfirmedReferenceRetry']) w[name]=()=>{};
        w._fetchAssets=()=>Promise.resolve();
        w.updateProject('other project');
        assert.equal(w._historyMergeCapabilities,null);
    ''')


API_CLIENT = __import__('pathlib').Path(__file__).resolve().parents[1] / 'web' / 'js' / 'api_client.js'


def test_the_token_response_header_is_what_retracts_a_prediction():
    # The real version map, moved only by the token answer's own header.
    _run_history_node('''
        const apiClient=await import(%r);
        const w=makeHistoryWidget(), e=entry();
        w.projectDir='project';
        w._historyObservedProjectVersion=EditorWidget.prototype._historyObservedProjectVersion;
        apiClient.rememberProjectVersion('project','2026-01-01T00:00:01');
        e.postSnapshotProjectVersion='2026-01-01T00:00:01';
        w._historyMergeCapabilities=structuredClone(capabilities);
    ''' % API_CLIENT.as_uri() + _COUNTING + '''
        globalThis.fetch=(url,init)=>later(()=>url.endsWith('/restore-token')
            ? new Response(JSON.stringify({restore_token:'token',...capabilities}),
                {headers:{'X-Sonder-Project-Modified-At':'2026-01-01T00:00:02'}})
            : new Response(JSON.stringify({scene:{...e.snapshot}})));
        await w._restoreScene(e.sceneId,e.snapshot,e.postSnapshot,'',null,{entry:e});
        const post=window.__SONDER_CANVAS_DIAG.events.filter(d=>d.kind==='history_optimistic_paint').at(-1);
        assert.equal(post.skip_reason,'version_mismatch','the header moved the observed version');
        assert.equal(post.retracted,false,'and a project change elsewhere keeps the prediction');
    ''')


@pytest.mark.parametrize('change', ['version', 'write_set'])
def test_a_drag_does_not_mask_a_write_set_change_at_the_recheck(change):
    # The predicate ranks `dragging` and `version_mismatch` before the write-set
    # check; retraction asks with both set aside so neither can hide it.
    _run_history_node('''
        const w=makeHistoryWidget(), e=entry();
        w._historyMergeCapabilities=structuredClone(capabilities);
        const change=%r;
    ''' % change + _COUNTING + '''
        globalThis.fetch=(url,init)=>later(()=>{
            if(url.endsWith('/restore-token')) {
                w.isDragging=true;
                if(change==='version') w._historyObservedProjectVersion=()=> 'v2';
                const fresh=change==='write_set'
                    ? {...capabilities,merged_write_fields:['clips','global_channel_docs']} : capabilities;
                return new Response(JSON.stringify({restore_token:'token',...fresh}));
            }
            return new Response(JSON.stringify({scene:{...e.snapshot}}));
        });
        const state={entry:e};
        await w._restoreScene(e.sceneId,e.snapshot,e.postSnapshot,'',null,state);
        // A version change keeps the prediction either way; a write-set change
        // retracts it even under a drag (the rollback then waits for mouse-up).
        assert.deepEqual(paints().at(-1),['post_token',false,true,change==='write_set']);
        assert.equal(w._pendingScenesRefresh,true);
    ''')


def test_a_project_switch_while_the_token_is_out_caches_nothing():
    _run_history_node('''
        const w=makeHistoryWidget(), e=entry();
        w.projectDir='project';
        globalThis.fetch=(url)=>later(()=>{
            if(url.endsWith('/restore-token')) {
                w.projectDir='other project';
                return new Response(JSON.stringify({restore_token:'token',...capabilities}));
            }
            return new Response(JSON.stringify({scene:{...e.snapshot}}));
        });
        const later=(fn)=>new Promise(r=>setTimeout(()=>r(fn()),0));
        await w._restoreScene(e.sceneId,e.snapshot,e.postSnapshot,'',null,{entry:e});
        assert.equal(w._historyMergeCapabilities,undefined);
    ''')


def test_a_retry_holding_its_token_does_not_warn_about_missing_lists():
    _run_history_node('''
        const w=makeHistoryWidget(), e=entry();
        const warnings=[]; const warn=console.warn;
        console.warn=(...args)=>{warnings.push(args.join(' '));};
        globalThis.fetch=async()=>new Response(JSON.stringify({scene:{...e.snapshot}}));
        await w._restoreScene(e.sceneId,e.snapshot,e.postSnapshot,'held-token',null,{entry:e});
        console.warn=warn;
        assert.deepEqual(warnings.filter(w=>w.includes('missing_merge_capabilities')),[]);
    ''')


def test_the_token_is_sent_before_the_prediction_is_painted():
    # Its round trip overlaps the paint's clones and render.
    _run_history_node('''
        const w=makeHistoryWidget(), e=entry();
        w._historyMergeCapabilities=structuredClone(capabilities);
    ''' + _COUNTING + '''
        const order=[];
        const paint=w._paintHistoryOptimistically.bind(w);
        w._paintHistoryOptimistically=(state)=>{order.push('paint');paint(state);};
        globalThis.fetch=(url)=>{
            order.push(url.endsWith('/restore-token')?'token':'restore');
            return later(()=>url.endsWith('/restore-token')
                ? new Response(JSON.stringify({restore_token:'token',...capabilities}))
                : new Response(JSON.stringify({scene:{...e.snapshot}})));
        };
        await w._restoreScene(e.sceneId,e.snapshot,e.postSnapshot,'',null,{entry:e});
        assert.deepEqual(order.slice(0,2),['token','paint']);
    ''')


def test_a_second_paint_of_the_same_action_is_refused():
    _run_history_node('''
        const w=makeHistoryWidget(), e=entry(), state={entry:e};
        w._paintHistoryOptimistically(state);
        w.activeScene.name='changed after paint';
        w._paintHistoryOptimistically(state);
        w._rollbackHistoryOptimisticPaint(state);
        assert.equal(w.activeScene.name,'after','rollback restores the scene from before the first paint');
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


def test_paint_and_rollback_clone_their_snapshots():
    _run_history_node("""
        const w=makeHistoryWidget(), e=entry(), state={entry:e};
        w._paintHistoryOptimistically(state);
        assert.notEqual(w.activeScene,e.snapshot);
        w.activeScene.name='local mutation';
        assert.equal(e.snapshot.name,'before');
        w._rollbackHistoryOptimisticPaint(state);
        assert.equal(w.activeScene.name,'after');
        assert.notEqual(w.activeScene,e.postSnapshot);
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


# Undo across takes, Phase 2 (audit M3): the real step hands its waiting-notice
# token to the queue slot, the paint, the drawn canonical scene and a refusal.
_NOTICE_STEP = """
        const w=makeHistoryWidget(), e=entry(), op=OPERATION, Op=op[0].toUpperCase()+op.slice(1);
        Object.assign(w, {projectDir:'project', activeSceneId:'scene', _historyOperationSeq:0,
            _queuedHistoryOperationCount:0, _queuedHistoryWaits:new Set(), _queuedHistoryNotification:null,
            _historyStackRevision:0, _maxUndoSteps:50, _projectDirName:()=>'project'});
        const tokens=[];
        const begin=w._beginQueuedHistoryWait.bind(w);
        w._beginQueuedHistoryWait=(operation)=>{const t=begin(operation);tokens.push(t);return t;};
        const quiet=w._quietQueuedHistoryWait.bind(w); let quieted=0;
        w._quietQueuedHistoryWait=(t)=>{if(t===tokens[0])quieted++;return quiet(t);};
        const at={};
        const flags=()=>({running:tokens[0].running,painted:tokens[0].painted});
        w[`_${op}Stack`].push(e);
"""


@pytest.mark.parametrize('operation', ['undo', 'redo'])
def test_a_pre_painted_step_marks_its_notice_painted_before_the_restore(operation):
    _run_history_node(_NOTICE_STEP.replace("OPERATION", repr(operation)) + '''
        w._historyMergeCapabilities=structuredClone(capabilities);
        globalThis.fetch=async(url,init)=>{
            if(url.endsWith('/restore-token')) return new Response(JSON.stringify({restore_token:'token',...capabilities}));
            if(init?.method==='PUT') { at.put=flags(); return new Response(JSON.stringify({scene:e.snapshot})); }
            return new Response(JSON.stringify({status:'pending'}));
        };
        await w[`_queue${Op}WithinGesture`](null);
        assert.deepEqual(at.put,{running:true,painted:true});
        assert.equal(tokens[0].active,false);
        assert.equal(w._queuedHistoryOperationCount,0);
        assert.equal(w._queuedHistoryNotification,null);
        assert.equal(quieted,0);
    ''')


@pytest.mark.parametrize('operation', ['undo', 'redo'])
def test_an_unpainted_step_counts_as_painted_once_the_canonical_scene_is_drawn(operation):
    # No cached merge lists and a disproving token: nothing is predicted, so
    # the step is unpainted until its answer is drawn.
    _run_history_node(_NOTICE_STEP.replace("OPERATION", repr(operation)) + '''
        globalThis.fetch=async(url,init)=>{
            if(url.endsWith('/restore-token')) { at.token=flags();
                return new Response(JSON.stringify({restore_token:'token',merged_write_fields:[],merged_derived_fields:[]})); }
            if(init?.method==='PUT') { at.put=flags(); return new Response(JSON.stringify({scene:e.snapshot})); }
            return new Response(JSON.stringify({status:'pending'}));
        };
        const set=w._setActiveScene.bind(w);
        w._setActiveScene=(scene,options={})=>{set(scene,options);if(!options.optimisticHistory)at.drawn=flags();};
        await w[`_queue${Op}WithinGesture`](null);
        assert.deepEqual(at.token,{running:true,painted:false});
        assert.deepEqual(at.put,{running:true,painted:false});
        assert.equal(at.drawn.painted,false,'marked after the render, not before');
        assert.equal(tokens[0].painted,true);
        assert.equal(tokens[0].active,false);
    ''')


@pytest.mark.parametrize('operation', ['undo', 'redo'])
def test_a_refused_step_takes_its_paint_back_and_goes_quiet(operation):
    _run_history_node(_NOTICE_STEP.replace("OPERATION", repr(operation)) + '''
        w._historyMergeCapabilities=structuredClone(capabilities);
        globalThis.fetch=async(url,init)=>{
            if(url.endsWith('/restore-token')) return new Response(JSON.stringify({restore_token:'token',...capabilities}));
            if(init?.method==='PUT') { at.put=flags();
                return new Response(JSON.stringify({error:'refused',code:'scene_merge_conflict'}),{status:400}); }
            return new Response(JSON.stringify({status:'pending'}));
        };
        await w[`_queue${Op}WithinGesture`](null);
        assert.deepEqual(at.put,{running:true,painted:true});
        assert.equal(tokens[0].painted,false,'the rollback took the paint back');
        assert.equal(quieted,1);
        assert.equal(w._queuedHistoryNotification,null);
    ''')


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


# ---------------------------------------------------------------------------
# The skip-reason map is exhaustive, and the warning speaks only for contract
# failures
# ---------------------------------------------------------------------------
def _widget_source():
    from pathlib import Path

    return (Path(__file__).resolve().parents[1]
            / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")


def _eligibility_skip_reasons():
    """Every `skip("…")` literal in `_historyOptimisticEligibility`."""
    import re

    source = _widget_source()
    start = source.index("    _historyOptimisticEligibility(")
    end = source.find("    _warnHistoryOptimisticSkip(", start)
    assert end > start, (
        "could not find the end of _historyOptimisticEligibility. It is bounded "
        "by the next method, _warnHistoryOptimisticSkip; if that was renamed, "
        "update this anchor rather than widening the slice, or reasons from "
        "unrelated methods will be read as this predicate's")
    reasons = set(re.findall(r'skip\(\s*"(\w+)"', source[start:end]))
    assert reasons, "no skip reasons found; the slice missed the predicate"
    # Lexical, so a computed `skip(someVar)` would be invisible here and would
    # silently return to being an unclassified reason. Nothing produces one
    # today; this is the limit of the scan, not a claim about the code.
    assert 'skip(' not in re.sub(r'skip\(\s*"', '', source[start:end]), (
        "a skip() call with a non-literal reason cannot be classified by this "
        "scan; make it a literal or classify it by hand")
    return reasons


def _declared_skip_reasons():
    """The `HISTORY_OPTIMISTIC_SKIP_REASONS` map, as name -> classification."""
    import re

    source = _widget_source()
    start = source.index("const HISTORY_OPTIMISTIC_SKIP_REASONS = Object.freeze({")
    body = source[start:source.index("});", start)]
    declared = dict(re.findall(r"^\s*(\w+):\s*\"(benign|contract)\",", body, re.M))
    assert declared, "the skip-reason map did not parse"
    return declared


def test_every_optimistic_skip_reason_is_classified():
    """A new reason must be decided, not defaulted.

    A deny-list would default it to warning and ship console noise with every
    future carve-out; an allow-list would default it to silence, which is the
    silent default this map exists to remove.
    """
    produced = _eligibility_skip_reasons()
    declared = _declared_skip_reasons()
    assert produced == set(declared), (
        "HISTORY_OPTIMISTIC_SKIP_REASONS and the reasons the predicate can "
        "actually return have diverged. Undeclared (these would be silent): "
        f"{sorted(produced - set(declared))}; declared but unreachable (dead "
        f"entries): {sorted(set(declared) - produced)}")


def test_every_contract_reason_has_advice_and_no_benign_one_does():
    """The warning must be able to say what to do about each reason it speaks for."""
    import re

    declared = _declared_skip_reasons()
    source = _widget_source()
    start = source.index("const HISTORY_OPTIMISTIC_CONTRACT_ADVICE = Object.freeze({")
    advice = set(re.findall(r"^\s*(\w+):", source[start:source.index("});", start)], re.M))
    contract = {name for name, kind in declared.items() if kind == "contract"}
    assert advice == contract, (
        f"advice is missing for {sorted(contract - advice)} and is dead for "
        f"{sorted(advice - contract)}")


def test_the_optimistic_warning_speaks_once_per_claim_and_only_for_contract():
    """Benign reasons are the common case; warning on them would be noise.

    `version_mismatch` alone fires whenever the project moved under a pending
    write, which at the measured route floor is most of the time.
    """
    _run_history_node('''
        const w=makeHistoryWidget();
        const warnings=[]; console.warn=(...a)=>warnings.push(a.join(' '));

        for (const reason of ['typed_entry','auxiliary_operations','dragging',
                'timeline_mutation','version_mismatch','no_authoritative_base']) {
            assert.equal(w._warnHistoryOptimisticSkip({skip_reason:reason}),false,
                `benign reason ${reason} must not warn`);
        }
        assert.equal(warnings.length,0,'benign reasons produced console output');

        assert.equal(w._warnHistoryOptimisticSkip(
            {skip_reason:'outside_write_set',skip_detail:'fps'}),true);
        assert.equal(warnings.length,1);
        assert.ok(warnings[0].includes('[Sonder]'));
        assert.ok(warnings[0].includes('outside_write_set'));
        assert.ok(warnings[0].includes('fps'));

        // Same claim again: silent. A different field is a different claim.
        assert.equal(w._warnHistoryOptimisticSkip(
            {skip_reason:'outside_write_set',skip_detail:'fps'}),false);
        assert.equal(warnings.length,1);
        assert.equal(w._warnHistoryOptimisticSkip(
            {skip_reason:'outside_write_set',skip_detail:'width'}),true);
        assert.equal(warnings.length,2);

        // A successful paint says nothing at all.
        assert.equal(w._warnHistoryOptimisticSkip(
            {would_paint:true,skip_reason:'',skip_detail:''}),false);
        assert.equal(warnings.length,2);
    ''')


def test_skip_detail_names_every_offending_field_and_no_values():
    """It is spread into the diag ring, which users send in.

    Naming the fields is what makes the warning actionable; naming their values
    would move authored text across a trust boundary that nothing downstream
    re-checks.
    """
    _run_history_node('''
        const w=makeHistoryWidget();
        const e=entry();
        // Inserted in an order whose natural key order is NOT alphabetical, so
        // the assertion below fails if the sort is dropped.
        e.snapshot.width=640; e.snapshot.fps=30;
        e.postSnapshot.width=1280; e.postSnapshot.fps=24;
        // An unknown key carrying authored text, the shape a hand-edited or
        // imported project.json produces. Its NAME may be reported; its value
        // must not be, and this one is genuinely an offender so the assertion
        // is not vacuous the way a writable field would make it.
        e.snapshot.note_to_self='ship before friday'; e.postSnapshot.note_to_self='shipped';
        const result=w._historyOptimisticEligibility(e,capabilities);
        assert.equal(result.skip_reason,'outside_write_set');
        assert.equal(result.skip_detail,'fps, note_to_self, width');
        assert.ok(!result.skip_detail.includes('friday'));
        assert.ok(!result.skip_detail.includes('shipped'));
        // Purely additive: the shape every existing consumer reads is unchanged.
        assert.equal(result.would_paint,false);
        assert.equal(w._historyOptimisticEligibility(entry(),capabilities).skip_detail,'');
    ''')


def test_skip_detail_is_bounded_as_well_as_sorted():
    """`Scene.to_dict()` overlays unknown top-level keys from the project file.

    So the offending-key set is not a closed vocabulary, and an old or
    hand-edited document can contribute arbitrarily many names to a string that
    lands in the diagnostic bundle. Names stay safe to show; an unbounded list
    stops being useful.
    """
    _run_history_node('''
        const w=makeHistoryWidget();
        const e=entry();
        for (let i=0;i<40;i++) {
            const key='unknown_future_field_'+String(i).padStart(2,'0');
            e.snapshot[key]=i; e.postSnapshot[key]=i+1;
        }
        const result=w._historyOptimisticEligibility(e,capabilities);
        assert.equal(result.skip_reason,'outside_write_set');
        assert.ok(result.skip_detail.includes('(+28 more)'),
            'expected a summarised tail, got: '+result.skip_detail);
        assert.equal(result.skip_detail.split(',').length,12,
            'twelve names, the last carrying the summary suffix');
        // Sorted, so the same document always produces the same dedup key.
        assert.ok(result.skip_detail.startsWith('unknown_future_field_00, unknown_future_field_01'));
        // Still names only, never values.
        assert.ok(!/\\b\\d+\\b/.test(result.skip_detail.replace(/unknown_future_field_\\d+/g,'')
            .replace(/\\(\\+28 more\\)/,'')));
    ''')


def test_every_declared_reason_is_exercised_against_the_real_predicate():
    """Generated from the source, so prose cannot satisfy it.

    The first version of this sliced this file's own text and looked for the
    reason name in quotes anywhere in a test body. A Python comment saying
    `# 'zzz_fake' is totally covered` passed it, which is the same badge-without-
    work shape the rebase tripwire rejects for an empty `case`. This drives each
    reason through the real predicate instead and asserts it comes back.

    Every reason needs a patch that provokes it. A reason with no patch fails
    here, which is the point: a new carve-out has to be reachable to be declared.
    """
    patches = {
        "typed_entry": "{kind:'reference_change'}",
        "auxiliary_operations": "{referenceOperations:[{}]}",
        "scene_mismatch": "{snapshot:{scene_id:'wrong'}}",
        "no_authoritative_base": "{postSnapshotProjectVersion:null}",
        "version_mismatch": "{postSnapshotProjectVersion:'older'}",
        "missing_post_snapshot": "{postSnapshot:null}",
        "outside_write_set": "{snapshot:{scene_id:'scene',name:'before',fps:30}}",
        # Provoked through the widget or the capabilities argument, not the entry.
        "dragging": None,
        "timeline_mutation": None,
        "missing_merge_capabilities": None,
    }
    declared = _declared_skip_reasons()
    unprovoked = sorted(set(declared) - set(patches))
    assert not unprovoked, (
        "these skip reasons are declared but nothing here provokes them, so "
        "nothing proves the predicate can still produce them. Add a patch that "
        f"reaches the branch: {unprovoked}")
    stale = sorted(set(patches) - set(declared))
    assert not stale, f"patches for reasons that no longer exist: {stale}"

    rows = ",".join(f"[{patch},'{reason}']"
                    for reason, patch in sorted(patches.items()) if patch)
    _run_history_node('''
        const w=makeHistoryWidget();
        assert.equal(w._historyOptimisticEligibility(entry(),capabilities).would_paint,true);
        for (const [patch,reason] of [''' + rows + ''']) {
            assert.equal(
                w._historyOptimisticEligibility({...entry(),...patch},capabilities).skip_reason,
                reason, 'expected ' + reason);
        }
        w.isDragging=true;
        assert.equal(w._historyOptimisticEligibility(entry(),capabilities).skip_reason,'dragging');
        w.isDragging=false; w._timelineMutationDepth=1;
        assert.equal(w._historyOptimisticEligibility(entry(),capabilities).skip_reason,'timeline_mutation');
        w._timelineMutationDepth=0;
        assert.equal(w._historyOptimisticEligibility(entry(),null).skip_reason,
            'missing_merge_capabilities');
    ''')


def test_the_real_restore_path_warns_for_a_contract_skip():
    """The production wiring, not the method in isolation.

    `_warnHistoryOptimisticSkip` had four tests and one caller, and none of the
    four touched the caller -- deleting `this._warnHistoryOptimisticSkip(...)`
    from `_restoreScene` left the whole file green. This drives the real restore
    round trip and asserts the line reaches the console.
    """
    _run_history_node('''
        const w=makeHistoryWidget(), e=entry();
        // fps differs between the entry's two snapshots and is outside the
        // write set, which is the reachable contract skip.
        e.snapshot.fps=30; e.postSnapshot.fps=24;
        const warnings=[]; console.warn=(...a)=>warnings.push(a.join(' '));
        let calls=0;
        globalThis.fetch=async()=>{
            calls++;
            if(calls===1) return new Response(JSON.stringify({restore_token:'token',...capabilities}));
            return new Response(JSON.stringify({scene:e.snapshot}));
        };
        await w._restoreScene(e.sceneId,e.snapshot,e.postSnapshot,'',null,{entry:e});
        assert.equal(warnings.length,1,
            'the restore path did not warn; is the call site still wired?');
        assert.ok(warnings[0].includes('outside_write_set'), warnings[0]);
        assert.ok(warnings[0].includes('fps'), warnings[0]);
        // Says "History", not "Undo": _restoreScene serves Redo too.
        assert.ok(warnings[0].includes('History could not be painted'), warnings[0]);
    ''')


def test_the_warning_emits_one_line_per_field_not_per_combination():
    """`outside_write_set` names every offender at once.

    Keying dedup on the joined set meant each subset that ever raced produced a
    fresh line -- three fields can race as seven distinct combinations. The
    acceptance criterion is one line per reason+field.
    """
    _run_history_node('''
        const w=makeHistoryWidget();
        const warnings=[]; console.warn=(...a)=>warnings.push(a.join(' '));
        const say=(detail)=>w._warnHistoryOptimisticSkip(
            {skip_reason:'outside_write_set',skip_detail:detail});

        assert.equal(say('fps'),true);
        assert.equal(say('width'),true);
        // Every remaining combination of the two is already accounted for.
        assert.equal(say('fps, width'),false);
        assert.equal(say('width'),false);
        assert.equal(say('fps'),false);
        assert.equal(warnings.length,2);

        // A genuinely new field still speaks, and names only itself. Checked on
        // the named-fields segment, because the advice text legitimately
        // mentions fps/width/height as the deliberate exclusions.
        assert.equal(say('fps, height, width'),true);
        assert.equal(warnings.length,3);
        const named=(line)=>line.slice(line.indexOf('): ')+3, line.indexOf('. '));
        assert.equal(named(warnings[2]),'height',
            'already-reported fields repeated: '+named(warnings[2]));
    ''')


# --------------------------------------------------------------------------
# Both sides of a step come from its own commit (Undo Across Takes, Phase 1)
# --------------------------------------------------------------------------

_TAKES = """
        const take={clip_id:'take'};
        const delta=(fields,members={})=>({v:1,fields,absent:[],members});
        const response=(payload,version='v2')=>({ok:true,status:200,
            headers:new Headers({'X-Sonder-Project-Modified-At':version}),json:async()=>payload});
        const sent=[];
        const serve=(...restores)=>{globalThis.fetch=async(url,init)=>url.endsWith('/restore-token')
            ? new Response(JSON.stringify({restore_token:'token',...capabilities}))
            : init?.method==='PUT' ? (sent.push(JSON.parse(init.body)),restores.shift()())
            : new Response(JSON.stringify({status:'pending'}));};
"""


def test_a_write_stamps_the_servers_before_state_not_the_editors():
    _run_history_node(_TAKES + """
        const w=makeHistoryWidget();
        let rebasedAgainst=null;
        Object.assign(w,{_maxUndoSteps:50,_clearRedoForNewEdit:()=>{},
            _stampHistoryPostSnapshot:EditorWidget.prototype._stampHistoryPostSnapshot,
            _rebaseSceneMutationIntentForHistory:(intent,ordered,authored)=>{rebasedAgainst=authored;return intent;}});
        w.activeScene={scene_id:'scene',name:'before',clips:[],paint:'of a write refused later'};
        EditorWidget.prototype._pushUndo.call(w,'edit');
        const pushed=w._undoStack.at(-1);
        w.activeScene.name='painted';
        const after={scene_id:'scene',name:'after',clips:[take]};
        const context=w._beginHistoryOrderContext('undo',1);
        context.scenes.set('scene',{scene_id:'scene',name:'ordered',clips:[]});
        await w._queueProjectMutation({key:'edit',coalesce:false,historyEntry:pushed,refreshScenes:false,
            historyOrderContext:context,intent:{projectId:'project',sceneId:'scene',operations:[]},
            run:async()=>({payload:{scene:after,history_before:delta({name:'stored'})},
                response:response({})})});
        assert.deepEqual(pushed.snapshot,{scene_id:'scene',name:'stored',clips:[take]});
        assert.deepEqual(pushed.postSnapshot,after);
        assert.equal(rebasedAgainst.name,'before','the rebase still reads what the author saw');
        // A later adoption of the response object must not reach the entry.
        after.clips.push({clip_id:'later'});
        assert.equal(pushed.postSnapshot.clips.length,1);
        assert.equal(pushed.snapshot.clips.length,1);
    """)


@pytest.mark.parametrize('history_before', [
    'undefined', 'null', '{v:2}', "{v:1,fields:{},absent:[],members:{other:{}}}"])
def test_without_a_readable_before_state_the_authored_copy_stays(history_before):
    _run_history_node("""
        const w=makeHistoryWidget(), e=entry(); delete e.postSnapshot; w._undoStack.push(e);
        EditorWidget.prototype._stampHistoryPostSnapshot.call(w,e,{scene_id:'scene',name:'after',clips:[]},
            new Response('{}'),""" + history_before + """);
        assert.equal(e.snapshot.name,'before');
        assert.equal(e.postSnapshot.name,'after');
    """)


@pytest.mark.parametrize('operation', ['Undo', 'Redo'])
def test_the_reverse_action_is_what_the_restore_changed(operation):
    _run_history_node(_TAKES + """
        const w=makeHistoryWidget(), e=entry(), operation='""" + operation + """';
        const [stack,opposite]=operation==='Undo'?['_undoStack','_redoStack']:['_redoStack','_undoStack'];
        w[stack].push(e);
        // The take landed after the step: the restore kept it.
        serve(()=>response({scene:{scene_id:'scene',name:'before',clips:[take]},
            history_before:delta({name:'after'})}));
        await w['_run'+operation+'WithinGesture']();
        const reverse=w[opposite].at(-1);
        assert.deepEqual(reverse.snapshot,{scene_id:'scene',name:'after',clips:[take]});
        assert.deepEqual(reverse.postSnapshot,{scene_id:'scene',name:'before',clips:[take]});
        assert.equal(reverse.postSnapshotVersionSource,'acknowledged_restore');
        assert.equal(reverse.postSnapshotProjectVersion,'v2');
        // The editor shows its own copy.
        w.activeScene.clips.length=0;
        assert.equal(reverse.postSnapshot.clips.length,1);
        // And that is what its paint shows.
        w._historyObservedProjectVersion=()=>'v2';
        const shown=[];const set=w._setActiveScene.bind(w);
        w._setActiveScene=(scene,options={})=>{shown.push([!!options.optimisticHistory,scene.name,scene.clips.length]);set(scene,options);};
        serve(()=>response({scene:{scene_id:'scene',name:'after',clips:[take]},
            history_before:delta({name:'before'})}));
        await w['_run'+(operation==='Undo'?'Redo':'Undo')+'WithinGesture']();
        assert.deepEqual(shown,[[true,'after',1],[false,'after',1]]);
        assert.deepEqual([sent.at(-1).base_scene.clips,sent.at(-1).target_scene.clips],[[take],[take]]);
    """)


@pytest.mark.parametrize('answer', [
    "response({scene:e.snapshot})",
    "response({scene:{...e.snapshot,clips:[take]},history_before:delta({})})",
    "response({scene:e.snapshot,history_before:{v:2}})",
])
def test_a_restore_without_a_pair_keeps_the_pinned_base(answer):
    """No pair, a restore that changed nothing, a delta this page cannot read: Redo
    still applies the step it names."""
    _run_history_node(_TAKES + """
        const w=makeHistoryWidget(), e=entry(); w._undoStack.push(e);
        serve(()=>""" + answer + """);
        await w._runUndoWithinGesture();
        const reverse=w._redoStack.at(-1);
        assert.equal(reverse.snapshot.name,'after');
        assert.equal(reverse.postSnapshot.name,'before');
        assert.deepEqual(reverse.postSnapshot.clips,[]);
        assert.equal(reverse.postSnapshotVersionSource,'restore_target');
    """)


def test_a_lost_response_keeps_the_pinned_base():
    _run_history_node(_TAKES + """
        const w=makeHistoryWidget(), e=entry(); w._undoStack.push(e);
        globalThis.fetch=async(url,init)=>{
            if(url.endsWith('/restore-token')) return new Response(JSON.stringify({restore_token:'token',...capabilities}));
            if(init?.method==='PUT') throw new TypeError('lost');
            return new Response(JSON.stringify({status:'committed',dropped_reference_members:2,
                scene:{scene_id:'scene',name:'current',clips:[take]}}));
        };
        const notices=[];w._notifyDroppedReferenceMembers=(payload)=>notices.push(payload.dropped_reference_members);
        await w._runUndoWithinGesture();
        const reverse=w._redoStack.at(-1);
        assert.equal(reverse.postSnapshot.name,'before');
        assert.equal(reverse.postSnapshotVersionSource,'restore_target');
        assert.deepEqual(notices,[2]);
    """)


def test_a_merged_slot_keeps_the_state_before_the_whole_merged_write():
    """Two members of one slot: the oldest entry survives, with the before-state of the
    write that carried both, not of the paint between them."""
    _run_history_node(_TAKES + """
        const w=makeHistoryWidget();
        Object.assign(w,{_maxUndoSteps:50,_clearRedoForNewEdit:()=>{},
            _claimHistoryPostSnapshotCapture:EditorWidget.prototype._claimHistoryPostSnapshotCapture,
            _stampHistoryPostSnapshot:EditorWidget.prototype._stampHistoryPostSnapshot});
        let release;const gate=new Promise(r=>{release=r;});
        const first=w._queueProjectMutation({key:'other',coalesce:false,refreshScenes:false,
            intent:{sceneId:'scene'},run:async()=>{await gate;return {payload:{}};}});
        const edit=(name)=>{
            EditorWidget.prototype._pushUndo.call(w,'edit');
            w.activeScene.name=name;
            return w._queueProjectMutation({key:'name',coalesce:true,refreshScenes:false,
                intent:{projectId:'project',sceneId:'scene',operations:[name]},
                run:async(intent)=>({payload:{scene:{scene_id:'scene',name:intent.operations[0],clips:[take]},
                    history_before:delta({name:'stored'})},response:response({})})});
        };
        const a=edit('one'), b=edit('two');
        release();await Promise.all([first,a,b]);
        assert.equal(w._undoStack.length,1);
        assert.deepEqual(w._undoStack[0].snapshot,{scene_id:'scene',name:'stored',clips:[take]});
        assert.equal(w._undoStack[0].postSnapshot.name,'two');
    """)


def test_a_lost_response_resolved_by_the_next_write_still_says_it_was_partial():
    _run_history_node(_TAKES + """
        const w=makeHistoryWidget(), e=entry();
        const notices=[];w._notifyDroppedReferenceMembers=(payload)=>notices.push(payload.dropped_reference_members);
        const context=w._beginHistoryOrderContext('undo',1);
        e.restoreToken='token';w._undoStack.push(e);
        w._projectDirName=()=>'project';
        w._markHistoryOrderContextAmbiguous(context,'scene','token',{operation:'undo',entry:e,
            sourceStack:w._undoStack,opposite:{sceneId:'scene'},oppositeStack:w._redoStack});
        globalThis.fetch=async()=>new Response(JSON.stringify({status:'committed',
            dropped_reference_members:3,scene:{scene_id:'scene',name:'before',clips:[]}}));
        await w._resolveHistoryOrderContextScene(context,'scene');
        assert.deepEqual(notices,[3]);
    """)


def test_a_drop_that_posts_its_own_request_hands_on_the_before_state():
    """The asset drop answers the queue with `{ok, payload}`: the stamp reads the payload."""
    _run_history_node(_TAKES + """
        const w=makeHistoryWidget(), e=entry(); delete e.postSnapshot; w._undoStack.push(e);
        w._stampHistoryPostSnapshot=EditorWidget.prototype._stampHistoryPostSnapshot;
        await w._queueProjectMutation({key:'drop',coalesce:false,historyEntry:e,refreshScenes:false,
            intent:{projectId:'project',sceneId:'scene',operations:[]},
            run:async()=>({ok:true,payload:{scene:{scene_id:'scene',name:'dropped',clips:[take]},
                history_before:delta({name:'stored'})}})});
        assert.equal(e.snapshot.name,'stored');
        assert.deepEqual(e.snapshot.clips,[take]);
    """)


def test_a_partial_restore_says_so_once():
    _run_history_node(_TAKES + """
        const w=makeHistoryWidget(), e=entry(); w._undoStack.push(e);
        const notices=[];w._notifyDroppedReferenceMembers=(payload)=>notices.push(payload.dropped_reference_members);
        serve(()=>response({scene:e.snapshot,history_before:delta({name:'after'}),dropped_reference_members:1}));
        await w._runUndoWithinGesture();
        assert.deepEqual(notices,[1]);
    """)


def test_the_opposite_claimed_ahead_keeps_its_claim_and_gets_the_pair():
    _run_history_node(_TAKES + """
        const w=makeHistoryWidget(), e=entry(), claim={id:1}, later={id:2};
        e.claimedBy=claim;w._undoStack.push(e);
        const reservation=w._reserveHistoryOpposite('undo',e,claim);
        reservation.claimedBy=later;
        serve(()=>response({scene:{scene_id:'scene',name:'before',clips:[take]},
            history_before:delta({name:'after'})}));
        await w._runUndoWithinGesture(null,e,claim,null,reservation,null);
        assert.equal(w._redoStack.at(-1),reservation);
        assert.equal(reservation.claimedBy,later);
        assert.deepEqual(reservation.snapshot.clips,[take]);
    """)
