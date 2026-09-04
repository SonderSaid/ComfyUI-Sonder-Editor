import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_node(script: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def _method(source: str, start: str, end: str) -> str:
    begin = source.index(f"    {start}(")
    finish = source.index(f"    {end}(", begin)
    return source[begin:finish]


def test_project_mutation_queue_contract_and_version_headers():
    queue_url = (ROOT / "web" / "js" / "project_mutation_queue.js").as_uri()
    api_url = (ROOT / "web" / "js" / "api_client.js").as_uri()
    _run_node(f"""
        import assert from 'node:assert/strict';
        import {{ ProjectMutationQueue }} from {queue_url!r};
        import {{
            fetchProjectJson,
            getProjectVersion,
            rememberProjectVersion,
        }} from {api_url!r};

        const q1 = new ProjectMutationQueue();
        const calls1 = [];
        const p1 = q1.enqueue({{
            key: 'same',
            intent: {{ value: 1 }},
            run: async (intent) => {{ calls1.push(intent.value); return intent.value; }},
        }});
        const p2 = q1.enqueue({{
            key: 'same',
            intent: {{ value: 2 }},
            run: async (intent) => {{ calls1.push(intent.value); return intent.value; }},
        }});
        assert.deepEqual(await Promise.all([p1, p2]), [2, 2]);
        assert.deepEqual(calls1, [2]);

        const q2 = new ProjectMutationQueue();
        const calls2 = [];
        await Promise.all([
            q2.enqueue({{ key: 'a', intent: 'a', run: async (intent) => {{ calls2.push(intent); }} }}),
            q2.enqueue({{ key: 'b', intent: 'b', run: async (intent) => {{ calls2.push(intent); }} }}),
        ]);
        assert.deepEqual(calls2, ['a', 'b']);

        const q3 = new ProjectMutationQueue();
        const merged = q3.enqueue({{
            key: 'merge',
            intent: {{ fields: {{ opacity: 0.5 }} }},
            merge: (oldIntent, nextIntent) => ({{ fields: {{ ...oldIntent.fields, ...nextIntent.fields }} }}),
            run: async (intent) => intent.fields,
        }});
        const merged2 = q3.enqueue({{
            key: 'merge',
            intent: {{ fields: {{ muted: true }} }},
            merge: (oldIntent, nextIntent) => ({{ fields: {{ ...oldIntent.fields, ...nextIntent.fields }} }}),
            run: async (intent) => intent.fields,
        }});
        assert.deepEqual(await merged, {{ opacity: 0.5, muted: true }});
        assert.deepEqual(await merged2, {{ opacity: 0.5, muted: true }});

        const q4 = new ProjectMutationQueue();
        let releaseActive;
        const active = q4.enqueue({{
            key: 'active',
            run: async () => await new Promise((resolve) => {{ releaseActive = resolve; }}),
        }});
        const drained = q4.drain('test');
        let drainedDone = false;
        drained.then(() => {{ drainedDone = true; }});
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(drainedDone, false);
        releaseActive('ok');
        await active;
        await drained;
        assert.equal(drainedDone, true);

        const q6 = new ProjectMutationQueue();
        let releaseFirst;
        let releaseSecond;
        const first = q6.enqueue({{
            key: 'first',
            run: async () => await new Promise((resolve) => {{ releaseFirst = resolve; }}),
        }});
        const second = first.then(() => q6.enqueue({{
            key: 'second',
            run: async () => await new Promise((resolve) => {{ releaseSecond = resolve; }}),
        }}));
        const stableDrain = q6.drain('chained');
        await new Promise((resolve) => setTimeout(resolve, 0));
        releaseFirst('first');
        await first;
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(q6.isBusy(), true);
        let stableDrainDone = false;
        stableDrain.then(() => {{ stableDrainDone = true; }});
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(stableDrainDone, false);
        releaseSecond('second');
        await second;
        await stableDrain;
        assert.equal(q6.isBusy(), false);

        const q5 = new ProjectMutationQueue();
        const failed = q5.enqueue({{ key: 'fail', run: async () => {{ throw new Error('boom'); }} }});
        const after = q5.enqueue({{ key: 'after', run: async () => 'after' }});
        await assert.rejects(failed, /boom/);
        assert.equal(await after, 'after');

        const requests = [];
        rememberProjectVersion('proj', 'v0');
        globalThis.fetch = async (_url, init = {{}}) => {{
            const headers = new Headers(init.headers || {{}});
            requests.push(headers.get('If-Match') || '');
            const version = `v${{requests.length}}`;
            return new Response(JSON.stringify({{ status: 'ok' }}), {{
                status: 200,
                headers: {{
                    'X-Sonder-Project-Id': 'proj',
                    'X-Sonder-Project-Modified-At': version,
                }},
            }});
        }};
        await fetchProjectJson('/sonder-editor/project/proj/scenes/s/mutations', {{ method: 'POST' }});
        await fetchProjectJson('/sonder-editor/project/proj/scenes/s/mutations', {{ method: 'POST' }});
        assert.deepEqual(requests, ['v0', 'v1']);
        assert.equal(getProjectVersion('proj'), 'v2');
    """)


def test_history_barrier_seals_same_key_coalescing_order():
    queue_url = (ROOT / "web" / "js" / "project_mutation_queue.js").as_uri()
    _run_node(f"""
        import assert from 'node:assert/strict';
        import {{ ProjectMutationQueue }} from {queue_url!r};

        const queue = new ProjectMutationQueue();
        const calls = [];
        let releaseActive;
        const active = queue.enqueue({{
            key: 'active',
            run: async () => {{
                calls.push('active');
                await new Promise((resolve) => {{ releaseActive = resolve; }});
            }},
        }});
        await Promise.resolve();
        const before = queue.enqueue({{
            key: 'scene:X:prompt', intent: 'before',
            run: async (intent) => calls.push(intent),
        }});
        const history = queue.enqueue({{
            key: 'history:undo:1', coalesce: false, sealCoalescing: true,
            run: async () => calls.push('undo'),
        }});
        const after = queue.enqueue({{
            key: 'scene:X:prompt', intent: 'after',
            run: async (intent) => calls.push(intent),
        }});
        assert.equal(queue.hasPendingKey('scene:X:prompt'), true);
        assert.equal(queue.hasPendingKey(
            'scene:X:prompt', {{ currentEpochOnly: true }}), true);
        releaseActive();
        await Promise.all([active, before, history, after]);
        assert.deepEqual(calls, ['active', 'before', 'undo', 'after']);
    """)


def test_owner_token_reentrancy_is_identity_scoped_and_missing_token_deadlocks():
    queue_url = (ROOT / "web" / "js" / "project_mutation_queue.js").as_uri()
    _run_node(f"""
        import assert from 'node:assert/strict';
        import {{ ProjectMutationQueue }} from {queue_url!r};

        const queue = new ProjectMutationQueue();
        const calls = [];
        let capturedToken;
        let foreignPromise;
        const active = queue.enqueue({{
            key: 'outer',
            run: async (_intent, _diagnostics, ownerToken) => {{
                capturedToken = ownerToken;
                await queue.enqueue({{
                    key: 'inline', ownerToken,
                    run: async () => {{
                        calls.push(['inline', queue.isActive(),
                            queue.hasPendingKey('inline')]);
                    }},
                }});
                foreignPromise = queue.enqueue({{
                    key: 'foreign', ownerToken: {{}},
                    run: async () => calls.push(['foreign']),
                }});
                await Promise.resolve();
                assert.deepEqual(calls, [['inline', true, false]]);
            }},
        }});
        await active;
        await foreignPromise;
        assert.deepEqual(calls, [['inline', true, false], ['foreign']]);

        let releaseSecond;
        let staleRan = false;
        const second = queue.enqueue({{
            key: 'second',
            run: async () => new Promise((resolve) => {{ releaseSecond = resolve; }}),
        }});
        await Promise.resolve();
        const stale = queue.enqueue({{
            key: 'stale', ownerToken: capturedToken,
            run: async () => {{ staleRan = true; }},
        }});
        await Promise.resolve();
        assert.equal(staleRan, false);
        assert.equal(queue.hasPendingKey('stale'), true);
        releaseSecond();
        await Promise.all([second, stale]);
        assert.equal(staleRan, true);

        const deadlockQueue = new ProjectMutationQueue();
        let nestedWithoutToken;
        void deadlockQueue.enqueue({{
            key: 'deadlock-outer',
            run: async () => {{
                nestedWithoutToken = deadlockQueue.enqueue({{
                    key: 'deadlock-inner', run: async () => undefined,
                }});
                await nestedWithoutToken;
            }},
        }});
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(deadlockQueue.isActive(), true);
        assert.equal(deadlockQueue.hasPendingKey('deadlock-inner'), true);
    """)


def test_folded_asset_drop_retry_replays_one_batch_without_duplicate_entities():
    api_url = (ROOT / "web" / "js" / "api_client.js").as_uri()
    _run_node(f"""
        import assert from 'node:assert/strict';
        import {{
            postProjectJsonWithReconcile,
            rememberProjectVersion,
        }} from {api_url!r};

        const requests = [];
        const server = {{ video_lane_count: 1, clips: [] }};
        rememberProjectVersion('retry-proj', '2026-09-02T10:00:00');
        globalThis.fetch = async (_url, init = {{}}) => {{
            const headers = new Headers(init.headers || {{}});
            requests.push({{
                gestureId: headers.get('X-Sonder-Gesture-Id'),
                gestureKind: headers.get('X-Sonder-Gesture-Kind'),
                requestId: headers.get('X-Sonder-Request-Id'),
                attempt: headers.get('X-Sonder-Gesture-Attempt'),
                ifMatch: headers.get('If-Match'),
                body: JSON.parse(init.body),
            }});
            if (requests.length === 1) {{
                return new Response(JSON.stringify({{
                    code: 'project_version_conflict',
                    error: 'stale',
                    actual_modified_at: '2026-09-02T10:00:01',
                    project: {{
                        project_id: 'retry-proj',
                        modified_at: '2026-09-02T10:00:01',
                    }},
                }}), {{
                    status: 409,
                    headers: {{
                        'X-Sonder-Project-Id': 'retry-proj',
                        'X-Sonder-Project-Modified-At': '2026-09-02T10:00:01',
                    }},
                }});
            }}
            for (const operation of requests.at(-1).body.operations) {{
                if (operation.type === 'set_lane_count') {{
                    server.video_lane_count = operation.count;
                }} else if (operation.type === 'create_clip') {{
                    server.clips.push({{ clip_id: 'created', ...operation.fields }});
                }}
            }}
            return new Response(JSON.stringify({{ scene: server }}), {{ status: 200 }});
        }};

        const result = await postProjectJsonWithReconcile(
            '/sonder-editor/project/retry-proj/scenes/scene/mutations',
            {{
                method: 'POST',
                headers: {{
                    'Content-Type': 'application/json',
                    'X-Sonder-Gesture-Id': 'gesture-9',
                    'X-Sonder-Gesture-Kind': 'assetDrop',
                }},
                body: JSON.stringify({{ operations: [
                    {{ type: 'set_lane_count', lane_type: 'video', count: 2 }},
                    {{ type: 'create_clip', fields: {{
                        asset_id: 'video', track_index: 1,
                    }} }},
                ] }}),
            }},
            {{ projectId: 'retry-proj', maxAttempts: 2 }},
        );

        assert.equal(result.attempts, 2);
        assert.deepEqual(requests.map((value) => value.gestureId),
            ['gesture-9', 'gesture-9']);
        assert.deepEqual(requests.map((value) => value.gestureKind),
            ['assetDrop', 'assetDrop']);
        assert.notEqual(requests[0].requestId, requests[1].requestId);
        assert.deepEqual(requests.map((value) => value.attempt), ['1', '2']);
        assert.deepEqual(requests.map((value) => value.ifMatch),
            ['2026-09-02T10:00:00', '2026-09-02T10:00:01']);
        assert.deepEqual(requests[0].body, requests[1].body);
        assert.equal(server.video_lane_count, 2);
        assert.equal(server.clips.length, 1);
    """)


def test_asset_drop_append_rebase_keeps_lane_count_and_create_target_aligned():
    source = (ROOT / "web" / "js" / "editor_widget.js").read_text(
        encoding="utf-8")
    rebase = _method(source, "_historyExpectedProjection", "_queueProjectMutation")
    _run_node(f"""
        import assert from 'node:assert/strict';
        class Harness {{
        {rebase}
        }}
        const h = new Harness();
        const scene = (video, audio = 1) => ({{
            scene_id: 'scene', video_lane_count: video,
            audio_lane_count: audio, clips: [], audio_tracks: [],
        }});
        for (const [authoredCount, orderedCount] of [[3, 5], [5, 3], [4, 4]]) {{
            const operations = [
                {{type: 'set_lane_count', lane_type: 'video', count: authoredCount + 1}},
                {{type: 'create_clip', fields: {{track_index: authoredCount}}}},
            ];
            const rebased = h._rebaseSceneMutationIntentForHistory(
                {{sceneId: 'scene', operations}}, scene(orderedCount),
                scene(authoredCount)).operations;
            assert.equal(rebased[0].count, orderedCount + 1);
            assert.equal(rebased[1].fields.track_index, orderedCount);
        }}

        const dual = h._rebaseSceneMutationIntentForHistory({{
            sceneId: 'scene', operations: [
                {{type: 'set_lane_count', lane_type: 'video', count: 3}},
                {{type: 'set_lane_count', lane_type: 'audio', count: 5}},
                {{type: 'create_clip', fields: {{
                    track_index: 2, audio_lane_index: 4, dual_drop: true,
                }}}},
            ],
        }}, scene(5, 1), scene(2, 4)).operations;
        assert.equal(dual[0].count, 6);
        assert.equal(dual[2].fields.track_index, 5);
        assert.equal(dual[1].count, 2);
        assert.equal(dual[2].fields.audio_lane_index, 1);
    """)


def test_mutation_ids_are_unique_across_independent_module_contexts():
    api_url = (ROOT / "web" / "js" / "api_client.js").as_uri()
    widget_url = (ROOT / "web" / "js" / "editor_widget.js").as_uri()
    _run_node(f"""
        import assert from 'node:assert/strict';

        globalThis.window = {{
            comfyAPI: {{ api: {{ api: {{ apiURL: (path) => path }} }} }},
            localStorage: {{ getItem: () => null }},
            SONDER_DEBUG_SESSION: true,
        }};
        globalThis.location = {{ href: 'http://test/' }};
        globalThis.performance = {{ now: () => 0 }};
        globalThis.requestAnimationFrame = () => 0;

        const apiOne = await import({(api_url + '?context=one')!r});
        const apiTwo = await import({(api_url + '?context=two')!r});
        const requestOne = new Headers(
            apiOne.withMutationRequestDiagnostics({{}}, 1).headers);
        const requestTwo = new Headers(
            apiTwo.withMutationRequestDiagnostics({{}}, 1).headers);
        assert.ok(requestOne.get('X-Sonder-Request-Id'));
        assert.notEqual(requestOne.get('X-Sonder-Request-Id'),
            requestTwo.get('X-Sonder-Request-Id'));

        const widgetOneModule = await import({(widget_url + '?context=one')!r});
        const widgetTwoModule = await import({(widget_url + '?context=two')!r});
        const widgetOne = Object.create(widgetOneModule.EditorWidget.prototype);
        const widgetTwo = Object.create(widgetTwoModule.EditorWidget.prototype);
        widgetOne._activeMutationGesture = null;
        widgetTwo._activeMutationGesture = null;
        const gestureOne = widgetOne._withMutationGesture(
            'moveItem', (diagnostics) => diagnostics.gestureId);
        const gestureTwo = widgetTwo._withMutationGesture(
            'moveItem', (diagnostics) => diagnostics.gestureId);
        assert.ok(gestureOne);
        assert.notEqual(gestureOne, gestureTwo);
    """)


def test_widget_gesture_scope_coalescing_unscoped_and_nested_contracts():
    widget_url = (ROOT / "web" / "js" / "editor_widget.js").as_uri()
    queue_url = (ROOT / "web" / "js" / "project_mutation_queue.js").as_uri()
    _run_node(f"""
        import assert from 'node:assert/strict';

        globalThis.window = {{
            comfyAPI: {{ api: {{ api: {{ apiURL: (path) => path }} }} }},
            localStorage: {{ getItem: () => null }},
            SONDER_DEBUG_SESSION: true,
        }};
        globalThis.location = {{ href: 'http://test/' }};
        globalThis.performance = {{ now: () => 0 }};
        globalThis.requestAnimationFrame = () => 0;

        const {{ EditorWidget }} = await import({widget_url!r});
        const {{ ProjectMutationQueue }} = await import({queue_url!r});

        function makeWidget() {{
            const widget = Object.create(EditorWidget.prototype);
            widget._activeMutationGesture = null;
            widget._timelineMutationDepth = 0;
            widget._sceneMutationInvalidationSeq = 0;
            widget._projectMutationQueue = new ProjectMutationQueue();
            widget._claimHistoryPostSnapshotCapture = () => null;
            widget._stampHistoryPostSnapshot = () => {{}};
            widget._reconcileActiveSceneFromMutation = () => true;
            widget._schedulePostMutationSceneRefresh = () => {{}};
            widget._deferProjectBackedRefresh = () => {{}};
            widget._replayDeferredProjectBackedRefresh = () => {{}};
            return widget;
        }}

        const coalescedWidget = makeWidget();
        const sent = [];
        const promises = [];
        for (const [index, gestureId] of ['gesture-a', 'gesture-b', 'gesture-c'].entries()) {{
            coalescedWidget._activeMutationGesture = {{
                gestureId,
                gestureKind: `move-${{index}}`,
            }};
            promises.push(coalescedWidget._queueProjectMutation({{
                key: 'same-resource',
                label: 'coalesced mutation',
                intent: {{ index }},
                refreshScenes: false,
                run: async (intent, diagnostics) => {{
                    sent.push({{
                        intent,
                        headers: coalescedWidget._mutationDiagnosticHeaders(diagnostics),
                    }});
                    return {{ payload: {{}} }};
                }},
            }}));
        }}
        coalescedWidget._activeMutationGesture = null;
        await Promise.all(promises);
        assert.equal(sent.length, 1);
        assert.equal(sent[0].intent.index, 2);
        assert.equal(sent[0].headers['X-Sonder-Gesture-Id'], 'gesture-c');
        assert.equal(sent[0].headers['X-Sonder-Gesture-Kind'], 'move-2');
        assert.equal(sent[0].headers['X-Sonder-Mutation-Coalesced-Count'], '3');

        const compositeWidget = makeWidget();
        const sharedGesture = {{
            gestureId: 'gesture-composite',
            gestureKind: 'assetDrop',
            coalescedCount: 1,
        }};
        const compositeSent = [];
        const queueComposite = (key, index) => compositeWidget._queueProjectMutation({{
            key,
            label: 'composite mutation',
            intent: {{ index }},
            diagnostics: sharedGesture,
            refreshScenes: false,
            run: async (intent, diagnostics) => {{
                compositeSent.push({{
                    key,
                    index: intent.index,
                    count: diagnostics.coalescedCount,
                }});
                return {{ payload: {{}} }};
            }},
        }});
        await Promise.all([
            queueComposite('composite-a', 1),
            queueComposite('composite-a', 2),
            queueComposite('composite-b', 3),
        ]);
        assert.deepEqual(compositeSent, [
            {{ key: 'composite-a', index: 2, count: 2 }},
            {{ key: 'composite-b', index: 3, count: 1 }},
        ]);
        assert.equal(sharedGesture.coalescedCount, 1);

        const unscopedWidget = makeWidget();
        let unscopedHeaders = null;
        await unscopedWidget._queueProjectMutation({{
            key: 'unscoped',
            coalesce: false,
            refreshScenes: false,
            run: async (_intent, diagnostics) => {{
                unscopedHeaders = unscopedWidget._mutationDiagnosticHeaders(diagnostics);
                return {{ payload: {{}} }};
            }},
        }});
        assert.equal(unscopedHeaders['X-Sonder-Gesture-Id'], '');
        assert.equal(unscopedHeaders['X-Sonder-Gesture-Kind'], 'unscoped');

        const nestedWidget = makeWidget();
        const activeIds = [];
        await nestedWidget._withTimelineMutationCommit('moveItem', async () => {{
            activeIds.push(nestedWidget._activeMutationGesture?.gestureId || '');
            await nestedWidget._withTimelineMutationCommit('inner', async () => {{
                activeIds.push(nestedWidget._activeMutationGesture?.gestureId || '');
            }});
        }});
        assert.equal(activeIds.length, 2);
        assert.ok(activeIds[0]);
        assert.equal(activeIds[0], activeIds[1]);
        const gestureEvents = window.__SONDER_CANVAS_DIAG.events
            .filter((event) => event.kind === 'gesture_start'
                || event.kind === 'gesture_end');
        assert.equal(gestureEvents.length, 2);
        assert.deepEqual(gestureEvents.map((event) => event.gesture_kind),
            ['moveItem', 'moveItem']);
        assert.deepEqual(window.__SONDER_CANVAS_DIAG.events
            .filter((event) => event.kind.startsWith('timeline_mutation_commit_'))
            .map((event) => [event.kind, event.mutation_kind, event.mutation_depth]), [
                ['timeline_mutation_commit_start', 'moveItem', 1],
                ['timeline_mutation_commit_start', 'inner', 2],
                ['timeline_mutation_commit_end', 'inner', 1],
                ['timeline_mutation_commit_end', 'moveItem', 0],
            ]);

        const overlappingWidget = makeWidget();
        const overlapDiagnostics = [];
        let releaseMove;
        let releaseTrim;
        const movePromise = overlappingWidget._withTimelineMutationCommit(
            'moveItem', async (diagnostics) => {{
                overlapDiagnostics.push(diagnostics);
                await new Promise((resolve) => {{ releaseMove = resolve; }});
            }});
        const trimPromise = overlappingWidget._withTimelineMutationCommit(
            'trimEdge', async (diagnostics) => {{
                overlapDiagnostics.push(diagnostics);
                await new Promise((resolve) => {{ releaseTrim = resolve; }});
            }});
        assert.equal(overlapDiagnostics.length, 2);
        assert.ok(overlapDiagnostics[0].gestureId);
        assert.notEqual(overlapDiagnostics[0].gestureId,
            overlapDiagnostics[1].gestureId);
        assert.deepEqual(overlapDiagnostics.map((value) => value.gestureKind),
            ['moveItem', 'trimEdge']);
        releaseTrim();
        releaseMove();
        await Promise.all([movePromise, trimPromise]);

        const spanWidget = makeWidget();
        const eventOffset = window.__SONDER_CANVAS_DIAG.events.length;
        let releaseQueuedHistory;
        const historySpan = spanWidget._withMutationGesture('undo', async (diagnostics) => {{
            diagnostics.queueWaitMs = 42;
            await new Promise((resolve) => {{ releaseQueuedHistory = resolve; }});
        }});
        assert.equal(window.__SONDER_CANVAS_DIAG.events.slice(eventOffset)
            .some((event) => event.kind === 'gesture_end'), false);
        releaseQueuedHistory();
        await historySpan;
        const historyEvents = window.__SONDER_CANVAS_DIAG.events.slice(eventOffset)
            .filter((event) => event.kind === 'gesture_start'
                || event.kind === 'gesture_end');
        assert.deepEqual(historyEvents.map((event) => event.gesture_kind),
            ['undo', 'undo']);
        assert.equal(historyEvents[1].queue_wait_ms, 42);
    """)


def test_project_dependency_history_writes_keep_one_gesture_and_fresh_request_ids():
    widget_url = (ROOT / "web" / "js" / "editor_widget.js").as_uri()
    _run_node(f"""
        import assert from 'node:assert/strict';

        globalThis.window = {{
            comfyAPI: {{ api: {{ api: {{ apiURL: (path) => path }} }} }},
            localStorage: {{ getItem: () => null }},
            SONDER_DEBUG_SESSION: true,
        }};
        globalThis.location = {{ href: 'http://test/' }};
        globalThis.performance = {{ now: () => 0 }};
        globalThis.requestAnimationFrame = () => 0;

        const {{ EditorWidget }} = await import({widget_url!r});
        const widget = Object.create(EditorWidget.prototype);
        widget.projectDir = 'project';
        widget._activeMutationGesture = null;
        widget._projectDirName = () => 'project';
        widget._captureProjectDependencies = () => ({{}});
        widget._fetchReferences = async () => true;
        widget._renderTimeline = () => {{}};

        const requests = [];
        globalThis.fetch = async (_url, init = {{}}) => {{
            const headers = new Headers(init.headers || {{}});
            requests.push({{
                gestureId: headers.get('X-Sonder-Gesture-Id'),
                gestureKind: headers.get('X-Sonder-Gesture-Kind'),
                requestId: headers.get('X-Sonder-Request-Id'),
                attempt: headers.get('X-Sonder-Gesture-Attempt'),
            }});
            return new Response(JSON.stringify({{
                prompt_context_profiles: [],
                prompt_semantic_units: [],
            }}), {{ status: 200 }});
        }};

        await widget._withMutationGesture('undo', async (diagnostics) => {{
            await widget._restoreProjectDependencies({{}}, diagnostics);
            await widget._savePromptSemanticUnits([], 'history identity', {{
                recordUndo: false,
                diagnostics,
            }});
        }});

        assert.equal(requests.length, 2);
        assert.ok(requests[0].gestureId);
        assert.deepEqual(requests.map((value) => value.gestureId),
            [requests[0].gestureId, requests[0].gestureId]);
        assert.deepEqual(requests.map((value) => value.gestureKind),
            ['undo', 'undo']);
        assert.ok(requests[0].requestId);
        assert.notEqual(requests[0].requestId, requests[1].requestId);
        assert.deepEqual(requests.map((value) => value.attempt), ['1', '1']);
    """)
