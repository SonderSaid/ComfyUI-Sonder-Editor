import json
import shutil
import sys
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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


def test_a_media_split_intent_survives_the_history_rebase_byte_identical():
    """The half of the split guard that lives on the client.

    `_historyExpectedProjection` overwrites every `expected` key the ordered
    row also carries. Projecting a media split forward would therefore rewrite
    the exact `timeline_start_frame` / `timeline_end_frame` the new server-side
    guard compares, and it could never fire -- the guard would be checking the
    rebase's own answer against itself. The two `case` labels added in umbrella
    Phase C stage 2 L1 do nothing on purpose; this is what makes "does nothing"
    a tested property rather than an accident of falling to `default`.

    The prompt split is asserted alongside as the deliberate asymmetry: its
    target is a positional index history can legitimately move, so it IS
    rebased. A test that only proved the media splits pass through unchanged
    would also pass against a switch that had stopped rebasing anything.
    """
    source = (ROOT / "web" / "js" / "editor_widget.js").read_text(
        encoding="utf-8")
    rebase = _method(source, "_historyExpectedProjection", "_queueProjectMutation")
    _run_node(f"""
        import assert from 'node:assert/strict';
        class Harness {{
        {rebase}
        }}
        const h = new Harness();
        // The ordered scene disagrees with every authored value: a first cut
        // already shortened the clip and the track, and moved the clip's lane.
        const ordered = {{
            scene_id: 'scene', video_lane_count: 2, audio_lane_count: 2,
            clips: [{{clip_id: 'clip-1', timeline_start_frame: 0,
                     timeline_end_frame: 8, track_index: 1, role: 'render'}}],
            audio_tracks: [{{track_id: 'audio-1', timeline_start_frame: 0,
                            timeline_end_frame: 8, lane_index: 1}}],
            prompt_sections: [
                {{prompt_id: 'p-early', start_frame: 0, end_frame: 5}},
                {{prompt_id: 'p-1', start_frame: 5, end_frame: 20}},
            ],
            guide_frames: [], reference_items: [],
        }};
        const authored = {{
            scene_id: 'scene', video_lane_count: 2, audio_lane_count: 2,
            clips: [], audio_tracks: [],
            prompt_sections: [{{prompt_id: 'p-1', start_frame: 0, end_frame: 20}}],
            guide_frames: [], reference_items: [],
        }};

        const splits = [
            {{type: 'split_clip', clip_id: 'clip-1', frame: 10,
              apply_linked: false,
              expected: {{clip_id: 'clip-1', timeline_start_frame: 0,
                         timeline_end_frame: 20, track_index: 0,
                         role: 'render'}}}},
            {{type: 'split_audio_track', track_id: 'audio-1', frame: 10,
              apply_linked: false,
              expected: {{track_id: 'audio-1', timeline_start_frame: 0,
                         timeline_end_frame: 20, lane_index: 0}}}},
        ];
        const before = JSON.stringify(splits);
        const rebased = h._rebaseSceneMutationIntentForHistory(
            {{sceneId: 'scene', operations: JSON.parse(before)}},
            ordered, authored).operations;
        assert.equal(JSON.stringify(rebased), before,
            'a media split intent must reach the server exactly as authored');

        // The asymmetry, asserted so this test cannot pass against an inert
        // switch: the prompt split's positional index IS retargeted, from 0 to
        // the ordered scene's index 1.
        const promptIntent = {{sceneId: 'scene', operations: [{{
            type: 'split_prompt_section', index: 0, frame: 10,
            apply_linked: false,
            expected: {{prompt_id: 'p-1', start_frame: 0, end_frame: 20}},
        }}]}};
        const prompt = h._rebaseSceneMutationIntentForHistory(
            promptIntent, ordered, authored).operations[0];
        assert.equal(prompt.index, 1);
        assert.equal(prompt.expected.start_frame, 5);
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


def _run_gesture_node(body: str, *, debug: bool = True) -> None:
    widget_url = (ROOT / "web/js/editor_widget.js").as_uri()
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    _run_node(f"""
        import assert from 'node:assert/strict';
        globalThis.window = {{
            comfyAPI: {{ api: {{ api: {{ apiURL: (path) => path }} }} }},
            localStorage: {{ getItem: () => null }},
            SONDER_DEBUG_SESSION: {str(debug).lower()},
        }};
        globalThis.location = {{ href: 'http://test/' }};
        globalThis.requestAnimationFrame = () => 0;
        const {{ EditorWidget }} = await import({widget_url!r});
        const {{ ProjectMutationQueue }} = await import({queue_url!r});
        function makeWidget() {{
            const widget = Object.create(EditorWidget.prototype);
            Object.assign(widget, {{
                projectDir: 'project', activeSceneId: 'scene', activeScene: {{scene_id:'scene'}},
                _activeMutationGesture: null, _timelineMutationDepth: 0,
                _sceneMutationInvalidationSeq: 0,
                _projectMutationQueue: new ProjectMutationQueue(),
                _claimHistoryPostSnapshotCapture: () => null,
                _stampHistoryPostSnapshot: () => {{}},
                _reconcileActiveSceneFromMutation: () => true,
                _schedulePostMutationSceneRefresh: () => {{}},
                _deferProjectBackedRefresh: () => {{}},
                _replayDeferredProjectBackedRefresh: () => {{}},
                _fetchAssets: async () => {{}}, _fetchScenes: async () => {{}},
                _fetchRenderQueue: async () => {{}},
                _pushUndo: () => {{}}, _renderSceneAfterLocalMutation: () => {{}},
                _renderTimeline: () => {{}}, _renderViewportFrame: () => {{}},
            }});
            return widget;
        }}
        const events = () => (window.__SONDER_CANVAS_DIAG?.events || [])
            .filter(e => e.kind === 'gesture_start' || e.kind === 'gesture_end');
        const starts = () => events().filter(e => e.kind === 'gesture_start');
        {body}
    """)


def test_clean_mute_gesture_captures_at_enqueue_and_ends_after_settlement():
    _run_gesture_node("""
        const w = makeWidget(), sent = [];
        w.selectedItems = [{type:'clip', id:'c', data:{muted:false}}];
        w._expandItemsWithLinked = items => items;
        w._isItemLocked = () => false;
        w._linkedGroupIdForItem = () => '';
        w._applyLocalItemProperty = () => {};
        let release;
        w._runSceneMutation = (operations, options) => w._queueProjectMutation({
            ...options, refreshScenes:false, intent:{operations},
            run: async (intent, diagnostics) => {
                sent.push({intent, diagnostics});
                await new Promise(resolve => { release = resolve; });
            },
        });
        const pending = w._toggleSelectedMute();
        assert.equal(starts().length, 1);
        assert.equal(w._activeMutationGesture, null);
        await new Promise(resolve => setTimeout(resolve, 0));
        assert.equal(sent.length, 1);
        assert.ok(sent[0].diagnostics.gestureId);
        assert.equal(sent[0].diagnostics.gestureKind, 'toggleMute');
        assert.equal(events().length, 1);
        release(); await pending;
        assert.deepEqual(events().map(e => e.kind), ['gesture_start','gesture_end']);
    """)


@pytest.mark.parametrize("directory", [False, True])
def test_import_fanout_retains_one_gesture_and_unique_physical_request_ids(directory):
    _run_gesture_node("""
        const w = makeWidget(), requests = [];
        const files = ['a.png','b.png','c.png'].map(name => new File(['x'],name));
        w._readDroppedDirectoryFiles = async () => { await Promise.resolve(); return files; };
        globalThis.fetch = async (url, init) => {
            await Promise.resolve();
            requests.push({url, headers:new Headers(init.headers)});
            return new Response('{}', {status:200});
        };
        console.log = () => {};
        """ + ("await w._importDroppedDirectory({name:'folder'});" if directory
                else "await w._importFilesWithProgress(files, 'folder');") + """
        assert.equal(requests.length, 3);
        assert.equal(starts().length, 1);
        assert.equal(events().length, 2);
        const ids = requests.map(r => r.headers.get('X-Sonder-Gesture-Id'));
        assert.ok(ids[0]); assert.equal(new Set(ids).size, 1);
        assert.ok(requests.every(r => r.headers.get('X-Sonder-Gesture-Kind') === 'asset_import'));
        assert.equal(new Set(requests.map(r => r.headers.get('X-Sonder-Request-Id'))).size, 3);
    """)


def test_asset_trash_has_diagnostics_without_altering_body_or_method():
    _run_gesture_node("""
        const w = makeWidget(); let request;
        globalThis.fetch = async (url, init) => {
            request = {url, ...init};
            return new Response('{}', {status:200});
        };
        await w._deleteAsset('asset', true);
        const h = new Headers(request.headers);
        assert.ok(h.get('X-Sonder-Gesture-Id'));
        assert.equal(h.get('X-Sonder-Gesture-Kind'), 'asset_trash');
        assert.equal(h.get('X-Sonder-Mutation-Coalesced-Count'), '1');
        assert.ok(h.get('X-Sonder-Request-Id'));
        assert.equal(request.method, 'DELETE');
        assert.deepEqual(JSON.parse(request.body), {force:true});
    """)


def test_source_picker_attributes_the_pick_not_the_deferred_builder():
    _run_gesture_node("""
        const w = makeWidget(); let picker; const sent = [];
        w._findSceneItemBySelection = () => null;
        w._isItemLocked = () => false;
        w._getAssetForSourcePath = () => ({asset_id:'old'});
        w._showImagePicker = options => { picker = options; };
        w._runSceneMutation = async (operations) => {
            sent.push({operations, diagnostics:w._snapshotMutationDiagnostics()});
        };
        w._replaceClipSource({clip_id:'clip',source_path:'old.mp4'});
        assert.equal(events().length, 0);
        await Promise.resolve();
        await picker.onPick('new');
        assert.equal(starts().length, 1);
        assert.equal(sent.length, 1);
        assert.equal(sent[0].diagnostics.gestureKind, 'replaceClipSource');
        assert.ok(sent[0].diagnostics.gestureId);
    """)


def test_real_lane_config_coalescing_reports_last_gesture_and_three_intents():
    _run_gesture_node("""
        const w = makeWidget(), sent = [];
        w._runVersionedProjectMutation = async (path, init) => {
            sent.push({path, init}); return {payload:{}};
        };
        await Promise.all([false,true,false].map(hidden => w._saveLaneConfig({
            type:'video',laneIndex:0,customName:'V1',hidden,
        })));
        assert.equal(starts().length, 3);
        assert.equal(sent.length, 1);
        const h = new Headers(sent[0].init.headers);
        assert.equal(h.get('X-Sonder-Gesture-Kind'), 'laneConfig');
        assert.equal(h.get('X-Sonder-Mutation-Coalesced-Count'), '3');
        assert.equal(h.get('X-Sonder-Gesture-Id'), starts()[2].marker_id);
        assert.equal(JSON.parse(sent[0].init.body).operations[0].fields.hidden, false);
    """)


def test_async_continuation_never_inherits_an_unrelated_ambient_gesture():
    _run_gesture_node("""
        const w = makeWidget(); let captured, ambient;
        await w._withMutationGesture('threaded', async diagnostics => {
            await Promise.resolve();
            ambient = w._snapshotMutationDiagnostics();
            captured = diagnostics;
        });
        assert.equal(ambient.gestureId, '');
        assert.equal(ambient.gestureKind, 'unscoped');
        assert.ok(captured.gestureId);
        assert.equal(starts().length, 1);
        // An auxiliary writer past its await consumes explicit diagnostics,
        // without minting a new gesture or changing the history owner.
        const owner = {}, order = {};
        w._references = [{members:[{member_id:'m',handle:'old'}]}];
        w._fetchReferences = async () => true;
        let received;
        w._mutateReferences = async (...args) => { received = args; };
        await w._applyReferenceHistoryOperations([{member_id:'m',fields:{handle:'new'}}],
            'undo', captured, owner, order);
        assert.equal(starts().length, 1);
        assert.equal(received[2], captured);
        assert.equal(received[3], owner); assert.equal(received[4], order);
    """)


def test_debug_off_import_mints_no_gesture_but_preserves_request_correlation():
    _run_gesture_node("""
        const w = makeWidget(); let headers;
        globalThis.fetch = async (_url, init) => {
            headers = new Headers(init.headers); return new Response('{}');
        };
        await w._importFilesWithProgress([new File(['x'],'a.png')]);
        assert.equal(events().length, 0);
        assert.equal(headers.get('X-Sonder-Gesture-Id'), '');
        assert.equal(headers.get('X-Sonder-Gesture-Kind'), 'unscoped');
        assert.ok(headers.get('X-Sonder-Request-Id'));
    """, debug=False)


def test_prompt_project_queue_carries_diagnostics_past_before_run_await():
    _run_gesture_node("""
        const w = makeWidget(); let sent;
        w._runVersionedProjectMutation = async (_path, init) => { sent = new Headers(init.headers); };
        await w._withMutationGesture('promptSettings', diagnostics =>
            w._queuePromptProjectWrite({metadata:{x:1}}, {
                diagnostics, beforeRun: async () => { await Promise.resolve(); },
            }));
        assert.ok(sent.get('X-Sonder-Gesture-Id'));
        assert.equal(sent.get('X-Sonder-Gesture-Kind'), 'promptSettings');
    """)


@pytest.mark.parametrize("close_panel", [False, True])
def test_pending_export_cancel_retains_original_gesture(close_panel):
    _run_gesture_node("""
        const w = makeWidget(), requests = [];
        w._exportPanelToken = 7;
        w._resetExportControlsAfterCancel = () => {};
        // The still-mounted cancel now follows terminal status; this test owns
        // gesture attribution only, with polling covered by export UI tests.
        w._pollTimelineExport = () => {};
        let release;
        globalThis.fetch = async (url, init) => {
            requests.push({url, headers:new Headers(init.headers)});
            if (!url.endsWith('/cancel')) {
                await new Promise(resolve => { release = resolve; });
                return new Response(JSON.stringify({job_id:'job'}));
            }
            return new Response('{}');
        };
        const pending = w._startTimelineExport({}, {progressEl:{textContent:''}});
        """ + ("w._hideExportPanel();" if close_panel else "await w._cancelTimelineExport(null);") + """
        assert.equal(starts().length, 2);
        const cancelId = starts()[1].marker_id;
        release(); await pending;
        assert.equal(requests.length, 2);
        assert.equal(requests[1].headers.get('X-Sonder-Gesture-Id'), cancelId);
        assert.equal(requests[1].headers.get('X-Sonder-Gesture-Kind'), 'cancelTimelineExport');
        assert.equal(starts().length, 2);
        assert.equal(w._exportStartDiagnostics, null);
    """)


def test_only_the_head_of_a_coalesced_group_rolls_back_its_scene_edit():
    """Umbrella Phase B / L2: the defect, driven through the real gestures.

    Two same-key scene edits authored while the queue is busy collapse into one
    request, and the queue settles both waiters from the survivor. Every
    collapsed gesture's `catch` therefore runs, so a gesture that restores a
    privately captured `previous` unconditionally leaves the client at the
    *older* edit's optimistic result -- a state the server never held, and one
    nothing refetches because these gestures run `refreshScenes: false`.

    Asserting the exact intermediate matters: 100 (duration) and 1280x720
    (resolution) are the values the unguarded rollback produced, and they are
    the only values that prove the defect is back.
    """
    _run_gesture_node("""
        // Both gestures warn on failure by design, and `_run_node` prefers stderr
        // when it reports, so an un-silenced warn hides the assertion message --
        // which is the whole product of a test like this.
        console.warn = () => {};
        const refuse = async () => {
            throw Object.assign(new Error('refused'), { code: 'refused' });
        };
        const busy = (w) => {
            let release; const blocked = new Promise((r) => { release = r; });
            const done = w._projectMutationQueue.enqueue({
                key: 'unrelated', coalesce: false, run: () => blocked });
            return [done, release];
        };

        // --- duration -------------------------------------------------------
        const d = makeWidget();
        d.activeScene = { scene_id: 'scene', duration_frames: 50 };
        d.totalFrames = 50;
        Object.assign(d, {
            _clampTimelineStateToDuration: () => {}, _refreshDurationInput: () => {},
            _updateToolbar: () => {}, _updateTransportUI: () => {},
            _runVersionedProjectMutation: refuse,
        });
        let [blocked, release] = busy(d);
        let edits = [d._updateSceneDuration(100), d._updateSceneDuration(200)];
        release();
        await Promise.all([blocked, ...edits]);
        assert.notEqual(d.activeScene.duration_frames, 100,
            "restored the first edit's optimistic value: a superseded sibling rolled back");
        assert.equal(d.activeScene.duration_frames, 50,
            'the head of the coalesced group must restore the value the server still has');
        assert.equal(d.totalFrames, 50);

        // --- resolution -----------------------------------------------------
        const r = makeWidget();
        r.activeScene = { scene_id: 'scene', width: 640, height: 360 };
        Object.assign(r, {
            _syncSceneResolutionControls: () => {}, _updateViewportHeader: () => {},
            _resizeViewportCanvas: () => {}, _runVersionedProjectMutation: refuse,
        });
        [blocked, release] = busy(r);
        edits = [r._updateSceneResolution(1280, 720), r._updateSceneResolution(1920, 1080)];
        release();
        await Promise.all([blocked, ...edits]);
        assert.notEqual(r.activeScene.width, 1280,
            "restored the first edit's optimistic width: a superseded sibling rolled back");
        assert.deepEqual([r.activeScene.width, r.activeScene.height], [640, 360],
            'the head of the coalesced group must restore the value the server still has');

        // A single uncoalesced failure still rolls back at once, with no refetch.
        const s = makeWidget();
        s.activeScene = { scene_id: 'scene', duration_frames: 50 };
        s.totalFrames = 50;
        Object.assign(s, {
            _clampTimelineStateToDuration: () => {}, _refreshDurationInput: () => {},
            _updateToolbar: () => {}, _updateTransportUI: () => {},
            _fetchScenes: async () => { throw new Error('the rollback must not need the network'); },
            _runVersionedProjectMutation: refuse,
        });
        await s._updateSceneDuration(400);
        assert.equal(s.activeScene.duration_frames, 50);
    """)


def test_the_queue_resolves_every_coalesced_waiter_with_the_survivor_s_result():
    """Why a private rollback is unsafe at all: losers cannot tell they lost.

    The queue sends one payload and settles every collapsed waiter from it, so a
    losing gesture's `catch`/`then` runs against an outcome that is not its own.
    This is the mechanism behind the test above; pinning it here means a change
    to the queue's settlement contract fails beside the gesture that relies on it.
    """
    _run_node("""
        import assert from 'node:assert/strict';
        const { ProjectMutationQueue } = await import(%r);
        const q = new ProjectMutationQueue();
        const sent = [];
        let release; const blocked = new Promise((r) => { release = r; });
        const blocker = q.enqueue({ key: 'other', coalesce: false, run: () => blocked });
        const run = async (intent) => { sent.push(intent.n); return 'result-of-' + intent.n; };
        const settled = [];
        const a = q.enqueue({ key: 'same', coalesce: true, intent: { n: 1 }, run })
            .then((r) => settled.push(r));
        const b = q.enqueue({ key: 'same', coalesce: true, intent: { n: 2 }, run })
            .then((r) => settled.push(r));
        release();
        await Promise.all([blocker, a, b]);

        assert.deepEqual(sent, [2], 'the older intent was replaced, not merged');
        assert.deepEqual(settled, ['result-of-2', 'result-of-2'],
            'a losing waiter must be seen to settle from the survivor, which is why it '
            + 'cannot use its own captured state to decide what to restore');
    """ % ((ROOT / "web/js/project_mutation_queue.js").as_uri(),))


def test_link_operations_rebase_their_positional_refs_like_bulk_delete():
    """A queued link/unlink must follow a prompt row history reordered.

    `_mutationItemFromSelection` addresses a prompt by LIST INDEX and a guide by
    FRAME INDEX, and `_item_ref_from_selection` on the server resolves both
    positionally. Before umbrella Phase B these two operations reached
    `default: break`, so an Undo that reordered prompt sections while the gesture
    was queued linked the row that inherited the index instead.
    """
    source = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    rebase = _method(source, "_historyExpectedProjection", "_queueProjectMutation")
    _run_node(f"""
        import assert from 'node:assert/strict';
        class Harness {{
        {rebase}
        }}
        const h = new Harness();
        // History inserted a section ahead of the authored one, so the prompt the
        // gesture meant is now at index 1, and the guide it meant moved to 40.
        const ordered = {{
            scene_id: 'scene', video_lane_count: 1, audio_lane_count: 1,
            clips: [{{clip_id: 'c1'}}], audio_tracks: [],
            guide_frames: [{{guide_id: 'g1', frame_index: 40}}],
            prompt_sections: [
                {{prompt_id: 'other', start_frame: 0, end_frame: 5}},
                {{prompt_id: 'p1', start_frame: 10, end_frame: 20}},
            ],
            reference_items: [],
        }};
        const item = (type, id, expected) => ({{type, id, expected}});
        for (const opType of ['create_link_group', 'unlink_items']) {{
            const operations = [{{
                type: opType,
                items: [
                    item('prompt', 0, {{prompt_id: 'p1', start_frame: 1, end_frame: 2}}),
                    item('guide', 12, {{guide_id: 'g1', frame_index: 12, asset_id: ''}}),
                    item('clip', 'c1', undefined),
                ],
            }}];
            const rebased = h._rebaseSceneMutationIntentForHistory(
                {{sceneId: 'scene', operations}}, ordered).operations[0];
            assert.equal(rebased.items[0].id, 1, `${{opType}} prompt index`);
            assert.equal(rebased.items[0].expected.start_frame, 10);
            assert.equal(rebased.items[0].expected.end_frame, 20);
            assert.equal(rebased.items[1].id, 40, `${{opType}} guide frame`);
            // A durable member passes through untouched, and an absent guard is
            // not invented: `_historyExpectedProjection` returns its input.
            assert.equal(rebased.items[2].id, 'c1');
            assert.equal(rebased.items[2].expected, undefined);
            // The authored intent is never mutated in place.
            assert.equal(operations[0].items[0].id, 0);
        }}

        // An unresolvable ref is left alone rather than throwing, so the change
        // can only improve on the pass-through it replaced.
        const orphan = h._rebaseSceneMutationIntentForHistory({{
            sceneId: 'scene',
            operations: [{{type: 'unlink_items', items: [
                item('prompt', 3, {{prompt_id: 'gone', start_frame: 0, end_frame: 1}}),
            ]}}],
        }}, ordered).operations[0];
        assert.equal(orphan.items[0].id, 3);
    """)


def _builder_keys(method_source: str, calls: str, extra_imports: str = "") -> set:
    """Run a client guard builder and report the union of keys it can emit.

    Derived, not asserted: the point is to compare the builder's real output
    against `GUARD_CONTRACTS`, so nothing here may hardcode the key set. An
    earlier version of this helper's callers asserted the same literals on both
    sides, which meant a third key could be added to the server and the client
    together and both halves would still pass.
    """
    with tempfile.TemporaryDirectory() as directory:
        sink = (Path(directory) / "keys.json").as_posix()
        _run_node(f"""
            import assert from 'node:assert/strict';
            import {{ writeFileSync }} from 'node:fs';
            {extra_imports}
            class Harness {{
                constructor(scene) {{ this.activeScene = scene; }}
            {method_source}
            }}
            const keys = new Set();
            const collect = (guard) => {{
                for (const key of Object.keys(guard)) keys.add(key);
                return guard;
            }};
            {calls}
            writeFileSync({sink!r}, JSON.stringify([...keys].sort()));
        """)
        return set(json.loads(Path(sink).read_text(encoding="utf-8")))


def test_the_lane_removal_guard_matches_its_contract():
    """The client builder and the server validator must name the same keys.

    `_laneRemovalGuard` is one builder shared by four gestures, which is why
    those four emissions read as `<opaque>`: there is no literal to scan. The
    trade is only acceptable because this DERIVES the builder's key set and
    compares it to `GUARD_CONTRACTS` — which is what the earlier version claimed
    and did not do.
    """
    import test_scene_mutation_registration as registration

    source = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    guard = _method(source, "_laneRemovalGuard", "_applyLocalRemoveLane")
    registry_url = (ROOT / "web" / "js" / "lane_registry.js").as_uri()
    scene = """
        const scene = {
            video_lane_count: 4, audio_lane_count: 2, reference_lane_count: 3,
            video_lane_configs: [{ name: 'v0' }, { name: 'v1' },
                                 { name: 'v2', locked: true }, {}],
            audio_lane_configs: [{}, {}],
            reference_lane_configs: [{}, {}, {}],
            reference_lane_recipes: [
                { lane_id: 'lane-a' }, { lane_id: 'lane-b' }, { lane_id: '' },
            ],
        };
        const host = new Harness(scene);
    """
    keys = _builder_keys(
        guard,
        scene + """
        collect(host._laneRemovalGuard('video', 2));
        collect(host._laneRemovalGuard('audio', 0));
        collect(host._laneRemovalGuard('reference', 1));
        collect(host._laneRemovalGuard('reference', 2));
        """,
        extra_imports=f"const {{ descriptorForLaneType }} = await import({registry_url!r});")

    kind, honoured, _evidence = registration.GUARD_CONTRACTS["remove_lane"]
    assert kind == registration._FIXED
    assert keys == set(honoured), (
        "`_laneRemovalGuard` emits a different key set than the dispatch branch "
        f"compares: builder={sorted(keys)} contract={sorted(honoured)}")

    # Behaviour, now that the keys are known to line up.
    _run_node(f"""
        import assert from 'node:assert/strict';
        const {{ descriptorForLaneType }} = await import({registry_url!r});
        class Harness {{
            constructor(scene) {{ this.activeScene = scene; }}
        {guard}
        }}
        {scene}
        // The config anchor is what survives a permutation that leaves the
        // count unchanged, so it must carry the lane's real settings.
        assert.deepEqual(host._laneRemovalGuard('video', 2), {{
            lane_count: 4,
            config: {{ name: 'v2', color: '', locked: true, hidden: false }},
        }});
        // An absent config reads as the default, not as undefined.
        assert.deepEqual(host._laneRemovalGuard('audio', 0).config,
            {{ name: '', color: '', locked: false, hidden: false }});
        // Reference carries its durable id as well.
        assert.equal(host._laneRemovalGuard('reference', 1).lane_id, 'lane-b');
        // A blank stored id claims no identity rather than an empty one.
        assert.ok(!('lane_id' in host._laneRemovalGuard('reference', 2)));
        assert.ok(!('lane_id' in host._laneRemovalGuard('reference', 9)));
        // Each removal in a descending batch states the floor IT will see.
        assert.deepEqual(
            [0, 1, 2].map((prior) => host._laneRemovalGuard('video', 3 - prior, prior).lane_count),
            [4, 3, 2]);
        // Looked up by LANE type: a track-type map would miss and degrade to 1.
        assert.equal(host._laneRemovalGuard('video', 0).lane_count, 4);
    """)


def test_a_rebased_lane_removal_keeps_its_guard_true():
    """Retargeting the index without the count would refuse every rebased removal.

    `remove_lane` is one of the operations `_rebaseSceneMutationIntentForHistory`
    retargets. The guard is authored against the pre-history count, so leaving it
    alone turns a retarget that just succeeded into a 409 -- a self-inflicted
    refusal on the exact path the rebase exists to rescue.
    """
    source = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    rebase = _method(source, "_historyExpectedProjection", "_queueProjectMutation")
    _run_node(f"""
        import assert from 'node:assert/strict';
        class Harness {{
        {rebase}
        }}
        const h = new Harness();
        // Named configs on purpose: with a changed lane count `rebaseLaneIndex`
        // falls through to the anchor, which refuses an ambiguous match. Blank
        // configs would make every lane identical and throw before the guard is
        // reached -- a real refusal, but not what this test is about.
        const scene = (video) => ({{
            scene_id: 'scene', video_lane_count: video, audio_lane_count: 1,
            video_lane_configs: Array.from({{ length: video }},
                (_value, index) => ({{ name: `v${{index}}` }})),
            clips: [], audio_tracks: [], prompt_sections: [], guide_frames: [],
            reference_items: [],
        }});

        // History restored two video lanes while the removal was queued.
        const single = h._rebaseSceneMutationIntentForHistory({{
            sceneId: 'scene',
            operations: [{{ type: 'remove_lane', lane_type: 'video', lane_index: 1,
                item_policy: 'require_empty', expected: {{ lane_count: 3 }} }}],
        }}, scene(5), scene(3)).operations[0];
        assert.equal(single.expected.lane_count, 5);

        // A descending batch: each operation still expects one fewer than the last.
        const batch = h._rebaseSceneMutationIntentForHistory({{
            sceneId: 'scene',
            operations: [
                {{ type: 'remove_lane', lane_type: 'video', lane_index: 2,
                   item_policy: 'delete_items', expected: {{ lane_count: 3 }} }},
                {{ type: 'remove_lane', lane_type: 'video', lane_index: 0,
                   item_policy: 'delete_items', expected: {{ lane_count: 2 }} }},
            ],
        }}, scene(5), scene(3)).operations;
        assert.deepEqual(batch.map((op) => op.expected.lane_count), [5, 4]);

        // A durable lane id is authored state and is NOT re-snapshotted: it is
        // what proves the rebase landed on the lane the author meant.
        const reference = h._rebaseSceneMutationIntentForHistory({{
            sceneId: 'scene',
            operations: [{{ type: 'remove_lane', lane_type: 'reference', lane_index: 1,
                item_policy: 'require_empty',
                expected: {{ lane_count: 2, lane_id: 'lane-b' }} }}],
        }}, {{ ...scene(1), reference_lane_count: 3, reference_lane_configs: [{{}}, {{}}, {{}}],
              reference_lane_recipes: [{{ lane_id: 'x' }}, {{ lane_id: 'lane-b' }}, {{ lane_id: 'y' }}] }},
           {{ ...scene(1), reference_lane_count: 2, reference_lane_configs: [{{}}, {{}}],
              reference_lane_recipes: [{{ lane_id: 'x' }}, {{ lane_id: 'lane-b' }}] }}).operations[0];
        assert.equal(reference.expected.lane_id, 'lane-b');
        assert.equal(reference.expected.lane_count, 3);
    """)


def test_the_guide_replacement_guard_reads_the_frame_before_the_local_apply():
    """The builder must name the occupant, and both call sites must read it early.

    `_applyLocalCreateGuide` removes the guide at the frame. Reading the guard
    after it would always produce an empty claim, which is the one value that
    passes on an empty frame and refuses on an occupied one -- a guard that
    inverts itself silently. Order is the whole correctness argument, so it is
    asserted against the source as well as the behaviour.
    """
    import test_scene_mutation_registration as registration

    source = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    guard = _method(source, "_guideReplacementGuard", "_applyLocalCreateGuide")

    # Derived, not asserted on both sides: the builder's real key set against
    # the contract. Six emissions were moved into OPAQUE_GUARD_SITES on the
    # strength of these parity checks, so they have to be real ones.
    keys = _builder_keys(guard, """
        const host = new Harness({ guide_frames: [
            { guide_id: 'occupant', frame_index: 12 },
        ] });
        collect(host._guideReplacementGuard(12));
        collect(host._guideReplacementGuard(99));
    """)
    kind, honoured, _evidence = registration.GUARD_CONTRACTS["create_guide"]
    assert kind == registration._FIXED
    assert keys == set(honoured), (
        "`_guideReplacementGuard` emits a different key set than the dispatch "
        f"branch compares: builder={sorted(keys)} contract={sorted(honoured)}")

    _run_node(f"""
        import assert from 'node:assert/strict';
        class Harness {{
            constructor(scene) {{ this.activeScene = scene; }}
        {guard}
        }}
        const host = new Harness({{ guide_frames: [
            {{ guide_id: 'occupant', frame_index: 12 }},
            {{ guide_id: 'elsewhere', frame_index: 40 }},
        ] }});
        assert.deepEqual(host._guideReplacementGuard(12),
            {{ replaces_guide_id: 'occupant' }});
        // `move_guide` excludes the guide it is moving: a drag that ends on the
        // frame it started from must not name itself as what it replaces.
        assert.deepEqual(host._guideReplacementGuard(12, 'occupant'),
            {{ replaces_guide_id: '' }});
        assert.deepEqual(host._guideReplacementGuard(12, 'someone-else'),
            {{ replaces_guide_id: 'occupant' }});
        assert.deepEqual(host._guideReplacementGuard(13),
            {{ replaces_guide_id: '' }});
        // A string frame from a dataset attribute must not read as "no occupant".
        assert.deepEqual(host._guideReplacementGuard('40'),
            {{ replaces_guide_id: 'elsewhere' }});
        // No scene at all is still a well-formed empty claim, not a crash.
        assert.deepEqual(new Harness(null)._guideReplacementGuard(0),
            {{ replaces_guide_id: '' }});
    """)

    for built, applied in _guide_guard_call_order(source):
        assert built < applied, (
            "a guide guard is built after the local apply has already removed "
            "the occupant it is meant to name")


def _guide_guard_call_order(source: str):
    """Per `create_guide` emission: where its guard was built, and its local apply.

    Anchored on the emission and scanned BACKWARDS, because not every
    `_applyLocalCreateGuide` belongs to an emission -- the asset-drop path calls
    it a third time to adopt the canonical guide out of the write's own response,
    which needs no guard because nothing is being claimed about prior state.
    Pairing the two lists positionally would have mistaken that reconciliation
    for a missing guard.
    """
    def occurrences(needle):
        return [index for index in range(len(source))
                if source.startswith(needle, index)]

    emissions = occurrences('type: "create_guide"')
    assert len(emissions) == 2, f"expected two emissions, found {len(emissions)}"
    builds = occurrences("this._guideReplacementGuard(")
    applies = occurrences("this._applyLocalCreateGuide(")
    # Two for `create_guide`, two more for `move_guide`'s destination claim.
    assert len(builds) == 4, f"expected four guard call sites, found {len(builds)}"

    pairs = []
    for emission in emissions:
        before_build = [index for index in builds if index < emission]
        before_apply = [index for index in applies if index < emission]
        assert before_build, "a create_guide emission builds no replacement guard"
        assert before_apply, "a create_guide emission runs no local apply"
        pairs.append((before_build[-1], before_apply[-1]))
    return pairs


def test_the_reference_and_prompt_guards_match_their_contracts():
    """Both Phase 2b builders, derived and compared — not asserted twice."""
    import test_scene_mutation_registration as registration

    source = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")

    reference = _method(source, "_referenceCreationGuard",
                        "_promptSectionsReplacementGuard")
    keys = _builder_keys(reference, """
        const host = new Harness({ reference_items: [
            { reference_item_id: 'later', lane_index: 0, start_frame: 100 },
            { reference_item_id: 'other-lane', lane_index: 1, start_frame: 20 },
        ] });
        collect(host._referenceCreationGuard(0, 10));
        collect(host._referenceCreationGuard(0, 150));
    """)
    kind, honoured, _evidence = registration.GUARD_CONTRACTS["create_reference_item"]
    assert kind == registration._FIXED
    assert keys == set(honoured), (
        "`_referenceCreationGuard` emits a different key set than the dispatch "
        f"branch compares: builder={sorted(keys)} contract={sorted(honoured)}")

    prompt = _method(source, "_promptSectionsReplacementGuard",
                     "_applyLocalCreateGuide")
    keys = _builder_keys(prompt, """
        const host = new Harness({ prompt_sections: [
            { prompt_id: 'p1', start_frame: 0, end_frame: 10 },
        ] });
        collect(host._promptSectionsReplacementGuard());
    """)
    kind, honoured, _evidence = registration.GUARD_CONTRACTS["replace_prompt_sections"]
    assert kind == registration._FIXED
    assert keys == set(honoured), (
        "`_promptSectionsReplacementGuard` emits a different key set than the "
        f"dispatch branch compares: builder={sorted(keys)} contract={sorted(honoured)}")

    _run_node(f"""
        import assert from 'node:assert/strict';
        class Harness {{
            constructor(scene) {{ this.activeScene = scene; }}
        {reference}
        {prompt}
        }}
        const host = new Harness({{
            reference_items: [
                {{ lane_index: 0, start_frame: 100 }},
                {{ lane_index: 0, start_frame: 40 }},
                {{ lane_index: 1, start_frame: 20 }},
            ],
            prompt_sections: [
                {{ prompt_id: 'p1', start_frame: 0, end_frame: 10 }},
                {{ prompt_id: 'p2', start_frame: 10, end_frame: 20 }},
            ],
        }});
        // The NEXT start after 10 on lane 0 is 40, not 20 (other lane) and not
        // 100 (further away). This is the value `end_frame` is derived from.
        assert.deepEqual(host._referenceCreationGuard(0, 10),
            {{ next_start_frame: 40 }});
        // Strictly after: an item starting exactly at the new start is an
        // overlap, which the server refuses on its own.
        assert.deepEqual(host._referenceCreationGuard(0, 40),
            {{ next_start_frame: 100 }});
        // Nothing later reports the same -1 sentinel both emitters send for
        // "runs to the end of the scene".
        assert.deepEqual(host._referenceCreationGuard(0, 150),
            {{ next_start_frame: -1 }});
        assert.deepEqual(host._referenceCreationGuard(2, 0),
            {{ next_start_frame: -1 }});

        // The prompt guard names the collection being REPLACED, in order.
        assert.deepEqual(host._promptSectionsReplacementGuard(), {{ sections: [
            {{ prompt_id: 'p1', start_frame: 0, end_frame: 10 }},
            {{ prompt_id: 'p2', start_frame: 10, end_frame: 20 }},
        ] }});
        // An empty scene still makes the claim rather than omitting it.
        assert.deepEqual(new Harness({{}})._promptSectionsReplacementGuard(),
            {{ sections: [] }});
    """)


def _js_guard_values(method_sources: str, scene_json: str, calls: str) -> list:
    """Run a client guard builder over a scene fixture and return what it built.

    Values, not key names. `test_the_reference_and_prompt_guards_match_their_contracts`
    already pins the keys; this is the other half — the same fixture through both
    languages, which `agent_workflow.md` requires of an intentional mirror and
    which two guard builders in this landing are.
    """
    with tempfile.TemporaryDirectory() as directory:
        sink = (Path(directory) / "values.json").as_posix()
        _run_node(f"""
            import {{ writeFileSync }} from 'node:fs';
            class Harness {{
                constructor(scene) {{ this.activeScene = scene; }}
            {method_sources}
            }}
            const host = new Harness({scene_json});
            const out = [];
            {calls}
            writeFileSync({sink!r}, JSON.stringify(out));
        """)
        return json.loads(Path(sink).read_text(encoding="utf-8"))


def test_the_reference_next_start_scan_agrees_across_languages():
    """`_next_reference_start_after` mirrors `_referenceCreationGuard`.

    A guard is only worth anything if both sides compute it the same way: a
    client that measures one thing and a server that measures another produces
    either a permanent refusal or a guard that never fires, and both look like
    "the feature is broken" rather than like a mirror that drifted.
    """
    import server.routes as routes
    from server.timeline_state import ReferenceItem, Scene

    source = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    guard = _method(source, "_referenceCreationGuard", "_promptSectionsReplacementGuard")

    fixture = [
        {"reference_item_id": "a", "lane_index": 0, "start_frame": 0},
        {"reference_item_id": "b", "lane_index": 0, "start_frame": 40},
        {"reference_item_id": "c", "lane_index": 0, "start_frame": 100},
        {"reference_item_id": "d", "lane_index": 1, "start_frame": 20},
    ]
    probes = [(0, -5), (0, 0), (0, 10), (0, 40), (0, 99), (0, 100), (0, 500),
              (1, 0), (1, 20), (2, 0)]

    emitted = _js_guard_values(
        guard, json.dumps({"reference_items": fixture}),
        "".join(f"out.push(host._referenceCreationGuard({lane}, {start})"
                f".next_start_frame);\n" for lane, start in probes))

    scene = Scene(scene_id="scene")
    scene.reference_items = [ReferenceItem(**value) for value in fixture]
    expected = [routes._next_reference_start_after(scene, lane, start)
                for lane, start in probes]

    assert emitted == expected, (
        "the client and server disagree about which item a new Reference item's "
        f"extent is measured against: js={emitted} python={expected}")


def test_the_prompt_section_structure_agrees_across_languages():
    """`_prompt_section_structure` mirrors `_promptSectionsReplacementGuard`."""
    import server.routes as routes
    from server.timeline_state import PromptSection, Scene

    source = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    guard = _method(source, "_promptSectionsReplacementGuard", "_applyLocalCreateGuide")

    scene = Scene(scene_id="scene")
    scene.prompt_sections = [PromptSection(start_frame=0, end_frame=10),
                             PromptSection(start_frame=10, end_frame=25),
                             PromptSection(start_frame=25, end_frame=30)]
    for section, prompt_id in zip(scene.prompt_sections, ("p1", "p2", "p3")):
        section.prompt_id = prompt_id
    scene.prompt_sections[1].muted = True

    fixture = [section.to_dict() for section in scene.prompt_sections]
    emitted = _js_guard_values(
        guard, json.dumps({"prompt_sections": fixture}),
        "out.push(host._promptSectionsReplacementGuard().sections);\n")

    assert emitted[0] == routes._prompt_section_structure(scene), (
        "the client and server project a prompt section's structure "
        f"differently: js={emitted[0]} python={routes._prompt_section_structure(scene)}")

    # The empty scene is the case an Apply on a fresh project takes.
    empty = _js_guard_values(guard, json.dumps({"prompt_sections": []}),
                             "out.push(host._promptSectionsReplacementGuard().sections);\n")
    assert empty[0] == routes._prompt_section_structure(Scene(scene_id="empty")) == []


def _header_visibility_burst(clicks: int) -> str:
    """Drive the real lane-header gesture `clicks` times through the real queue.

    Each gesture's mute operation carries a DISTINCT guard value, numbered by
    the order the gesture was authored. That is what makes the assertions below
    able to tell a merge from wholesale replacement: with every gesture reading
    the same value, both produce the same payload and the test proves nothing.
    Faithful to the shape that matters -- `_muteOperationForItem` reads
    `item.data.muted` at BUILD time, so each gesture in a burst sees what the
    previous one's optimistic apply left.
    """
    return """
        const w = makeWidget(), sent = [], settled = [];
        w.activeScene = {scene_id:'scene', video_lane_configs:[{}], video_lane_count:1};
        w._headerVisibilitySeq = 0;
        w._isLaneVisibilityControlDisabled = () => false;
        w._laneTypeForEntry = () => 'video';
        w._defaultLaneConfig = () => ({});
        w._trackItemsForEntry = () => [];
        w._trackVisibilityState = (entry) => entry.hidden ? 'hidden' : 'visible';
        w._clearPlaybackWarmOverlay = () => {};
        w._reconcileSelection = () => {};
        w._buildTrackLayout = () => {};
        w._updateToolbar = () => {};
        // Real entry objects, so `historyEntry`, `_pendingHistoryEntryByMutationKey`
        // and the `willCoalesce` discard are all live rather than inert.
        const discarded = [];
        w._discardUndoEntry = (target) => { discarded.push(target); return true; };
        const fetches = [];
        w._fetchScenes = async (options) => { fetches.push(options || {}); };
        const entries = [];
        let authored = 0;
        w._buildLinkedMuteOperations = () => {
            const seq = authored;
            authored += 1;
            return {operations: [{type:'update_reference_item',
                reference_item_id:'r', expected:{muted: seq}, fields:{muted: seq}}],
                targets: [], locked: false};
        };
        let release = null;
        w._runSceneMutation = (operations, options) => w._queueProjectMutation({
            ...options, refreshScenes:false, intent:{operations},
            run: async (intent) => {
                sent.push(intent);
                if (sent.length === 1) await new Promise(r => { release = r; });
                return {payload:{}};
            },
        });
        const entry = {type:'video', laneIndex:0, hidden:false};
        const pending = [];
        const click = (hidden) => {
            const index = pending.length;
            const undoEntry = { label: 'toggle track visibility', index };
            entries.push(undoEntry);
            pending.push(w._applyHeaderVisibilityBulkWithinGesture([entry], hidden,
                { undoEntry }).then(() => settled.push(index)));
        };
        // Click once and let the queue DISPATCH it, so the rest of the burst
        // arrives while that write is in flight. That is the shape the measured
        // entry describes, and it is why the expectation is two writes and not
        // one: `enqueue` scans `_pending` only and `_pump` shifts the active
        // slot off before awaiting, so the click in flight cannot be collapsed
        // into.
        click(true);
        await new Promise(r => setTimeout(r, 0));
        for (let i = 1; i < CLICKS; i += 1) {
            entry.hidden = i % 2 === 1;
            click(!entry.hidden);
        }
        release();
        await Promise.all(pending);
        await new Promise(r => setTimeout(r, 0));
    """.replace("CLICKS", str(clicks))


def test_a_lane_header_burst_costs_two_writes_not_six():
    """The measured entry, made automatable.

    `sonder_editor_bugs.md` measures six rapid clicks on one lane's hide control
    at 24.2 s, because each paid the ~1,966 ms route floor on its own. Two
    writes, not one: one per in-flight window, plus the first.
    """
    _run_gesture_node(_header_visibility_burst(6) + """
        assert.equal(sent.length, 2,
            `expected two writes for six clicks, got ${sent.length}`);
        const configs = sent[1].operations.filter(op => op.type === 'update_lane_config');
        assert.equal(configs.length, 1,
            'the five collapsed clicks must fold to ONE lane-config operation');
        assert.equal(configs[0].fields.hidden, false,
            'the surviving lane config must carry the LAST click, not the first');
    """)


def test_a_coalesced_header_burst_keeps_the_oldest_before_value_guard():
    """The reason this key needed a merge rather than a flag flip.

    Each gesture reads `expected: { muted }` off state the previous gesture's
    optimistic apply already changed. Wholesale replacement sends the NEWEST
    guard, which describes a state the server has not reached; the merge keeps
    the oldest with the newest `fields`. Both halves are asserted, because a
    test that only checked the newest value would pass on replacement too --
    which is exactly how the first version of this test failed to bite.
    """
    _run_gesture_node(_header_visibility_burst(6) + """
        const mutes = sent[1].operations.filter(
            op => op.type === 'update_reference_item');
        assert.equal(mutes.length, 1, 'the mute operations must fold per row');
        assert.equal(mutes[0].expected.muted, 1,
            'the surviving guard must come from the FIRST gesture of the '
            + 'collapsed group, not the last');
        assert.equal(mutes[0].fields.muted, 5,
            'the surviving value must come from the LAST gesture');
    """)


def test_every_collapsed_header_gesture_settles():
    """No author is left awaiting a write that was merged away.

    The queue resolves every collapsed waiter from the survivor's single result;
    this is the property that makes adopting coalescing safe for a gesture whose
    callers await it.
    """
    _run_gesture_node(_header_visibility_burst(4) + """
        assert.deepEqual(settled.slice().sort(), [0, 1, 2, 3],
            'every click must settle, however few writes were sent');
    """)


def test_two_lanes_do_not_share_a_visibility_write():
    """Two clicks on DIFFERENT lane headers are two edits, not one.

    The key names the lanes, not just the scene. Merging across lanes would
    collapse two undo entries into one, and `_queueProjectMutation`'s merge
    keeps the NEWEST entry -- whose before-state was captured after the first
    click already painted. The survivor's reverse delta would then cover only
    the last lane, and because `video_lane_family` is an atomic bundle in
    `server/scene_history_merge.py` the stranded lane makes the NEXT entry's
    restore raise `SceneMergeConflict` rather than merely not reverse it.

    So the assertion is two writes, and it is a regression guard rather than a
    performance one: the cost of getting this wrong is a blocked Ctrl+Z.
    """
    _run_gesture_node("""
        const w = makeWidget(), sent = [];
        w.activeScene = {scene_id:'scene', video_lane_configs:[{}, {}], video_lane_count:2};
        w._headerVisibilitySeq = 0;
        w._isLaneVisibilityControlDisabled = () => false;
        w._laneTypeForEntry = () => 'video';
        w._defaultLaneConfig = () => ({});
        w._trackItemsForEntry = () => [];
        w._trackVisibilityState = (entry) => entry.hidden ? 'hidden' : 'visible';
        w._clearPlaybackWarmOverlay = () => {};
        w._reconcileSelection = () => {};
        w._buildTrackLayout = () => {};
        w._updateToolbar = () => {};
        w._discardUndoEntry = () => {};
        w._buildLinkedMuteOperations = () => ({operations: [], targets: [], locked: false});
        let release = null;
        w._runSceneMutation = (operations, options) => w._queueProjectMutation({
            ...options, refreshScenes:false, intent:{operations},
            run: async (intent) => {
                sent.push({key: options.key, operations: intent.operations});
                if (sent.length === 1) await new Promise(r => { release = r; });
                return {payload:{}};
            },
        });
        const laneA = {type:'video', laneIndex:0, hidden:false};
        const laneB = {type:'video', laneIndex:1, hidden:false};
        const pending = [w._applyHeaderVisibilityBulkWithinGesture([laneA], true)];
        await new Promise(r => setTimeout(r, 0));
        pending.push(w._applyHeaderVisibilityBulkWithinGesture([laneB], true));
        pending.push(w._applyHeaderVisibilityBulkWithinGesture([laneB], false));
        release();
        await Promise.all(pending);
        await new Promise(r => setTimeout(r, 0));

        assert.equal(sent.length, 2,
            `lane A must not share a write with lane B; got ${sent.length}`);
        assert.notEqual(sent[0].key, sent[1].key,
            'the two lanes must enqueue under different keys');
        // The two clicks on lane B DID coalesce with each other -- the second
        // write carries one operation, not two.
        assert.equal(sent[1].operations.length, 1,
            'repeated clicks on ONE lane must still fold');
        assert.equal(sent[1].operations[0].lane_index, 1);
        assert.equal(sent[1].operations[0].fields.hidden, false,
            'and the fold must carry the LAST click');
    """)


def test_a_bulk_selection_keeps_one_key_for_the_whole_selection():
    """A bulk hide over several lanes is ONE gesture and one edit.

    The lane set is sorted into the key, so repeating the same bulk gesture
    coalesces with itself while a different selection does not. Without the
    sort, the same selection reached in a different order would mint a
    different key and silently stop coalescing.
    """
    _run_gesture_node("""
        const w = makeWidget(), sent = [];
        w.activeScene = {scene_id:'scene', video_lane_configs:[{}, {}], video_lane_count:2};
        w._headerVisibilitySeq = 0;
        w._isLaneVisibilityControlDisabled = () => false;
        w._laneTypeForEntry = () => 'video';
        w._defaultLaneConfig = () => ({});
        w._trackItemsForEntry = () => [];
        w._trackVisibilityState = (entry) => entry.hidden ? 'hidden' : 'visible';
        w._clearPlaybackWarmOverlay = () => {};
        w._reconcileSelection = () => {};
        w._buildTrackLayout = () => {};
        w._updateToolbar = () => {};
        w._discardUndoEntry = () => {};
        w._buildLinkedMuteOperations = () => ({operations: [], targets: [], locked: false});
        let release = null;
        const keys = [];
        w._runSceneMutation = (operations, options) => w._queueProjectMutation({
            ...options, refreshScenes:false, intent:{operations},
            run: async (intent) => {
                keys.push(options.key);
                sent.push(intent.operations);
                if (sent.length === 1) await new Promise(r => { release = r; });
                return {payload:{}};
            },
        });
        const a = {type:'video', laneIndex:0, hidden:false};
        const b = {type:'video', laneIndex:1, hidden:false};
        const pending = [w._applyHeaderVisibilityBulkWithinGesture([a, b], true)];
        await new Promise(r => setTimeout(r, 0));
        // Same selection, opposite order: the sort must make the key identical.
        pending.push(w._applyHeaderVisibilityBulkWithinGesture([b, a], false));
        pending.push(w._applyHeaderVisibilityBulkWithinGesture([a, b], true));
        release();
        await Promise.all(pending);
        await new Promise(r => setTimeout(r, 0));

        assert.equal(sent.length, 2, 'the two later bulk gestures must fold');
        assert.equal(keys[0], keys[1],
            'the same lane set reached in a different order must mint the same key');
        assert.equal(sent[1].length, 2, 'both lanes are written by the survivor');
    """)


def test_a_coalesced_burst_discards_each_superseded_entry_by_exact_object():
    """Two entries survive a six-click burst, and four go by identity.

    This is what makes Ctrl+Z after a burst reach the pre-burst state in two
    steps. It is also the property the landing replaced a label-and-top-of-stack
    match to get: `_discardLastUndo("toggle track visibility")` would pop
    whatever happened to be on top, which after queue interleaving can be a
    newer gesture's entry entirely.
    """
    _run_gesture_node(_header_visibility_burst(6) + """
        assert.equal(discarded.length, 4,
            `expected four superseded entries, got ${discarded.length}`);
        assert.deepEqual(discarded.map((one) => one.index), [1, 2, 3, 4],
            'the discarded entries must be clicks 2-5 -- click 1 was already '
            + 'dispatched and click 6 is the survivor');
        // By identity, not by label: every discarded object is the exact entry
        // that gesture authored.
        assert.ok(discarded.every((one, at) => one === entries[at + 1]));
    """)


def test_a_failed_coalesced_burst_refetches_once():
    """Every waiter's `catch` runs; only the newest gesture refreshes.

    The queue settles all six from the survivor's single rejection, so six
    `catch` blocks run. The `visibilitySeq` gate is what keeps that from
    becoming six full scene refetches -- and this is the test that shows the
    gate is doing something, which the burst tests above never reached because
    they never failed.
    """
    _run_gesture_node(_header_visibility_burst(6).replace(
        "if (sent.length === 1) await new Promise(r => { release = r; });\n"
        "                return {payload:{}};",
        "if (sent.length === 1) await new Promise(r => { release = r; });\n"
        "                throw new Error('refused');") + """
        assert.equal(fetches.length, 1,
            `expected exactly one gated refetch, got ${fetches.length}`);
        assert.equal(fetches[0].reason, 'header_visibility_error');
        assert.deepEqual(settled.slice().sort((a, b) => a - b), [0, 1, 2, 3, 4, 5],
            'every gesture must settle even though the write failed');
    """)


def _clip_role_burst(fail: bool = False) -> str:
    """Two conversions of ONE clip, the only way this key can repeat."""
    return """
        const w = makeWidget(), sent = [], restored = [];
        w.activeScene = {scene_id:'scene', video_lane_count:1, motion_driver_lane_count:1,
            video_lane_configs:[{}], motion_driver_lane_configs:[{}],
            clips:[{clip_id:'c1', role:'render', track_index:0, strength:1.0}]};
        w._applyLocalSetLaneCount = () => {};
        w._clearSelection = () => {}; w._hideItemEditor = () => {};
        const heals = [];
        w._fetchScenes = async (options) => { heals.push(options?.reason); };
        w._defaultMotionDriverStrength = () => 0.5;
        w._firstEmptyUnlockedDriverLane = () => 0;
        w._getAssetForSourcePath = () => ({asset_type:'video'});
        const clip = w.activeScene.clips[0];
        const watch = () => restored.push({role: clip.role, track_index: clip.track_index});
        let release = null;
        w._runSceneMutation = (operations, options) => w._queueProjectMutation({
            ...options, refreshScenes:false, intent:{operations},
            run: async (intent) => {
                sent.push(intent.operations);
                if (sent.length === 1) await new Promise(r => { release = r; });
                if (FAIL) throw new Error('refused');
                return {payload:{}};
            },
        });
        const pending = [w._convertClipRoleWithinGesture('c1', 'motion_driver')];
        await new Promise(r => setTimeout(r, 0));
        pending.push(w._convertClipRoleWithinGesture('c1', 'render'));
        pending.push(w._convertClipRoleWithinGesture('c1', 'motion_driver'));
        release();
        await Promise.allSettled(pending);
        await new Promise(r => setTimeout(r, 0));
        watch();
    """.replace("FAIL", "true" if fail else "false")


def test_two_clip_role_conversions_share_one_write():
    """The key is per clip, so only conversions of ONE clip can collapse.

    And the lane-count operations must keep their order relative to the clip
    writes: `set_lane_count` is preserved, so a fold of the clip write would
    move it in front of a lane count it was authored after.
    """
    _run_gesture_node(_clip_role_burst() + """
        assert.equal(sent.length, 2,
            `expected two writes for three conversions, got ${sent.length}`);
        const types = sent[1].map((op) => op.type);
        assert.deepEqual(types, ['set_lane_count', 'update_clip',
                                 'set_lane_count', 'update_clip'],
            `the merged batch reordered: ${types.join(' | ')}`);
        assert.equal(sent[1].at(-1).fields.role, 'motion_driver',
            'the last conversion authored must be the last applied');
    """)


def test_a_superseded_role_conversion_does_not_roll_back():
    """`oldState` predates the group only for its head.

    Three conversions become TWO writes -- the first dispatches alone, the other
    two collapse -- so there are two heads, one per write, and two restores.
    Without `onSupersededByCoalescing` there would be three: the superseded
    gesture would restore its own `oldState`, which is an earlier sibling's
    optimistic clip rather than anything the server held.

    Two rather than one is the honest number, and the restores are ordered so
    the later write's head wins locally. Each restoring gesture then refetches,
    which is what actually returns the clip to server state; the local restore
    is the fast path, not the authority.
    """
    _run_gesture_node(_clip_role_burst(fail=True) + """
        assert.deepEqual(heals, ['convert_clip_role_error', 'convert_clip_role_error'],
            `expected one heal per WRITE, got ${JSON.stringify(heals)}`);
        // And the clip itself, which is what the guard is FOR. Asserting only
        // the heal count tested the hook's presence, not its placement: moving
        // `if (!isHead) return;` below the `Object.assign` leaves the count at
        // two while a superseded gesture restores an earlier sibling's
        // optimistic clip.
        assert.deepEqual(restored[0], {role: 'motion_driver', track_index: 0},
            `the clip must end at the head-of-group state, got `
            + JSON.stringify(restored[0]));
    """)


def _selected_mute_burst() -> str:
    """Two mute toggles over a selection holding a Reference item."""
    return """
        const w = makeWidget(), sent = [];
        const item = {type:'reference', id:'r', data:{reference_item_id:'r', muted:false}};
        w.selectedItems = [item];
        w.activeScene = {scene_id:'scene'};
        w._expandItemsWithLinked = (items) => items;
        w._isItemLocked = () => false;
        w._linkGroupForItem = () => null;
        w._reconcileSelection = () => {};
        w._updateToolbar = () => {};
        w._itemEditorEl = null;
        let release = null;
        w._runSceneMutation = (operations, options) => w._queueProjectMutation({
            ...options, refreshScenes:false, intent:{operations},
            run: async (intent) => {
                sent.push(intent.operations);
                if (sent.length === 1) await new Promise(r => { release = r; });
                return {payload:{}};
            },
        });
        const pending = [w._toggleSelectedMuteWithinGesture()];
        await new Promise(r => setTimeout(r, 0));
        pending.push(w._toggleSelectedMuteWithinGesture());
        pending.push(w._toggleSelectedMuteWithinGesture());
        release();
        await Promise.all(pending);
        await new Promise(r => setTimeout(r, 0));
    """


def test_a_selected_mute_burst_keeps_the_oldest_reference_guard():
    """Its `expected: { muted: !nextMuted }` is a before-value claim.

    Computed from `item.data.muted`, which the previous toggle already wrote
    locally -- so wholesale replacement would send a guard describing state the
    server has not reached. The oldest guard with the newest value is what makes
    a pair of toggles correctly net to no change.
    """
    _run_gesture_node(_selected_mute_burst() + """
        assert.equal(sent.length, 2,
            `expected two writes for three toggles, got ${sent.length}`);
        const refs = sent[1].filter((op) => op.type === 'update_reference_item');
        assert.equal(refs.length, 1, 'the reference mutes must fold to one row');
        // Toggle 1 muted it. Toggles 2 and 3 unmute then re-mute, so the folded
        // guard is toggle 2's -- what the server held when the burst began --
        // and the value is toggle 3's.
        assert.equal(refs[0].expected.muted, true);
        assert.equal(refs[0].fields.muted, true);
    """)
