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


def test_only_the_tail_slot_coalesces_and_coalesce_target_agrees_with_enqueue():
    """A, X, A behind an active write is three slots; A, A is one.

    `coalesceTarget` is the question the widget asks before it enqueues, so it
    must answer exactly as `enqueue` then acts -- including null for a refused
    merge, a sealing write and an inline owner-token write.
    """
    queue_url = (ROOT / "web" / "js" / "project_mutation_queue.js").as_uri()
    _run_node(f"""
        import assert from 'node:assert/strict';
        import {{ ProjectMutationQueue }} from {queue_url!r};

        const queue = new ProjectMutationQueue();
        const calls = [];
        let releaseActive, ownerToken;
        const active = queue.enqueue({{
            key: 'active',
            run: async (_intent, _diagnostics, token) => {{
                ownerToken = token;
                await new Promise((resolve) => {{ releaseActive = resolve; }});
            }},
        }});
        await Promise.resolve();
        const run = async (intent) => calls.push(intent);
        const writes = [queue.enqueue({{ key: 'A', intent: 'a1', run }})];
        assert.equal(queue.coalesceTarget('A')?.intent, 'a1');
        assert.equal(queue.coalesceTarget('A', {{ coalesce: false }}), null);
        assert.equal(queue.coalesceTarget('A', {{ sealCoalescing: true }}), null);
        assert.equal(queue.coalesceTarget('A', {{ ownerToken }}), null,
            'an inline write never merges');
        writes.push(queue.enqueue({{ key: 'X', intent: 'x', run }}));
        assert.equal(queue.coalesceTarget('A'), null,
            'a pending A that is not the tail is not a merge target');
        writes.push(queue.enqueue({{ key: 'A', intent: 'a2', run }}));
        assert.equal(queue.coalesceTarget('A')?.intent, 'a2');
        writes.push(queue.enqueue({{ key: 'A', intent: 'a3', run }}));
        releaseActive();
        await Promise.all([active, ...writes]);
        assert.deepEqual(calls, ['a1', 'x', 'a3'],
            'A, X, A is three writes in authoring order; the tail A absorbs a3');
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
        // Real entry objects, so `historyEntry`, the slot's surviving entry
        // and the joiner's discard are all live rather than inert.
        // On a stack, as in the widget: a slot is joined only while its entry
        // is on the undo stack with nothing but the joiner's own above it.
        const discarded = [];
        w._undoStack = [];
        w._discardUndoEntry = (target) => {
            discarded.push(target);
            const at = w._undoStack.indexOf(target);
            if (at >= 0) w._undoStack.splice(at, 1);
            return true;
        };
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
            w._undoStack.push(undoEntry);
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
    collapse two undo entries into one: a merged slot keeps only its oldest
    entry, so one Ctrl+Z would revert both lanes.

    So the assertion is two writes, and it is a regression guard rather than a
    performance one: the cost of getting this wrong is a lost Undo step.
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

    The survivor is the pending slot's OLDEST entry, click 2: its before-state
    is the only one taken before any member of the slot painted, so it alone
    reverses the whole merged write. It is also the one stamped from that write.
    """
    _run_gesture_node(_header_visibility_burst(6).replace(
        "const discarded = [];",
        "const discarded = [], stamped = [];\n"
        "        w._stampHistoryPostSnapshot = (entry) => { if (entry) stamped.push(entry); };") + """
        assert.equal(discarded.length, 4,
            `expected four superseded entries, got ${discarded.length}`);
        assert.deepEqual(discarded.map((one) => one.index), [2, 3, 4, 5],
            'the discarded entries must be clicks 3-6 -- click 1 was already '
            + 'dispatched and click 2 opened the pending slot, so it survives');
        // By identity, not by label: every discarded object is the exact entry
        // that gesture authored.
        assert.ok(discarded.every((one, at) => one === entries[at + 2]));
        assert.deepEqual(stamped.map((one) => one.index), [0, 1],
            'each write stamps exactly its own slot entry: click 1, then click 2 '
            + 'from the merged write');
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


# The widget's own undo stack, claim and stamp, so which entry a merged slot
# keeps -- and what it is stamped with -- is the shipped behaviour.
_REAL_HISTORY = """
        Object.assign(w, {
            _pushUndo: EditorWidget.prototype._pushUndo,
            _claimHistoryPostSnapshotCapture:
                EditorWidget.prototype._claimHistoryPostSnapshotCapture,
            _stampHistoryPostSnapshot: EditorWidget.prototype._stampHistoryPostSnapshot,
            _undoStack: [], _redoStack: [], _maxUndoSteps: 50, _historyStackRevision: 0,
            _trimUndoStack() {}, _clearRedoForNewEdit() {},
            _replayDeferredHistoryWidgetStateIfIdle() {},
        });
"""


def _lane_config_burst(body: str) -> str:
    """Lane lock and rename through the real save, behind one in-flight write.

    The server applies each `update_lane_config` in order and answers with the
    whole scene, so every entry is stamped from the write that carried it.
    `assertChain` is the property the wedge broke: each surviving entry's
    before-state is the previous one's after-state, and the last one's
    after-state is what the server holds. Then every Undo reverses exactly its
    own write, and none restores a lane state the server never held -- which is
    what a phantom entry does, and what makes the Undo after it refuse.
    """
    return """
        const w = makeWidget();
    """ + _REAL_HISTORY + """
        const blank = () => ({name:'', color:'', locked:false, hidden:false});
        w.activeScene = {scene_id:'scene', video_lane_count:3,
            video_lane_configs:[blank(), blank(), blank()]};
        w._laneTypeForEntry = () => 'video';
        w._defaultLaneConfig = blank;
        const server = structuredClone(w.activeScene);
        const sent = [];
        let release = null;
        w._runSceneMutation = (operations, options) => w._queueProjectMutation({
            ...options, refreshScenes:false, intent:{sceneId:'scene', operations},
            run: async (intent) => {
                sent.push({key: options.key, operations: intent.operations});
                if (sent.length === 1) await new Promise(r => { release = r; });
                for (const op of intent.operations) {
                    server.video_lane_configs[op.lane_index] = {
                        ...server.video_lane_configs[op.lane_index], ...op.fields};
                }
                return {payload:{scene: structuredClone(server)}};
            },
        });
        const lanes = [0, 1, 2].map((laneIndex) => ({
            type:'video', laneIndex, customName:'', color:'', locked:false, hidden:false}));
        const pending = [];
        // The header's lock case: push, flip, save -- synchronously, so the
        // save claims the entry the way the canvas handler's does.
        const lock = (...targets) => {
            w._pushUndo('toggle track lock');
            const next = !targets[0].locked;
            for (const target of targets) target.locked = next;
            pending.push(w._saveLaneConfigWithinGesture(targets));
        };
        // A lane rename, as `_startLaneRename` saves it: its own Undo step.
        const rename = (target, name) => {
            target.customName = name;
            pending.push(w._saveLaneConfigWithinGesture([target], {undoLabel: 'rename lane'}));
        };
        // A lane-config save that owns no Undo step. No gesture does this since
        // L4c; the queue's handling of one is still pinned below.
        const saveWithoutEntry = (target, name) => {
            target.customName = name;
            pending.push(w._saveLaneConfigWithinGesture([target]));
        };
        const tick = () => new Promise(r => setTimeout(r, 0));
        const settle = async () => {
            await tick();
            release();
            await Promise.all(pending);
            await tick();
        };
        const configs = (scene) => scene.video_lane_configs.map(
            (config) => [config.name || '', !!config.locked]);
        const assertChain = () => {
            const entries = w._undoStack;
            assert.ok(entries.length > 0);
            assert.ok(entries.every((entry) => entry.postSnapshot),
                'every surviving entry must be stamped');
            for (let at = 1; at < entries.length; at += 1) {
                assert.deepEqual(configs(entries[at].snapshot),
                    configs(entries[at - 1].postSnapshot),
                    `entry ${at} must begin where entry ${at - 1} ended`);
            }
            assert.deepEqual(configs(entries.at(-1).postSnapshot), configs(server),
                'the newest entry must end where the server is');
        };
    """ + body


def test_three_lanes_locked_behind_a_write_keep_three_undo_steps():
    """The measured wedge: one scene-wide key merged the locks of two lanes.

    The merged write kept the NEWEST entry, whose before-state already held the
    older lock's paint, so the older lane was changed with no entry naming it
    and the next Undo was refused. Per-lane keys give each lane its own slot.
    """
    _run_gesture_node(_lane_config_burst("""
        lock(lanes[0]);
        await tick();
        lock(lanes[1]);
        lock(lanes[2]);
        await settle();
        assert.equal(sent.length, 3, 'one write per lane');
        assert.deepEqual(sent.map((write) => write.operations.map((op) => op.lane_index)),
            [[0], [1], [2]]);
        assert.equal(new Set(sent.map((write) => write.key)).size, 3,
            'each lane enqueues under its own key');
        assert.equal(w._undoStack.length, 3, 'one Undo step per lane');
        assertChain();
    """))


def test_an_even_count_lock_burst_on_one_lane_leaves_no_phantom_entry():
    """Lock/unlock one lane four times behind a write.

    Four toggles are one slot and one entry. Kept newest, that entry's
    before-state was the lane LOCKED by the third toggle's paint -- a state the
    server never held -- so its Undo would lock the lane. Kept oldest, it begins
    where the in-flight write ends.
    """
    _run_gesture_node(_lane_config_burst("""
        lock(lanes[0]);
        await tick();
        for (let toggle = 0; toggle < 4; toggle += 1) lock(lanes[1]);
        await settle();
        assert.equal(sent.length, 2, 'the four toggles collapse into one write');
        assert.equal(sent[1].operations[0].fields.locked, false,
            'the merged write carries the last toggle');
        assert.equal(w._undoStack.length, 2);
        assertChain();
    """))


def test_an_entryless_save_then_a_lock_on_one_lane_keep_the_locks_undo_step():
    """An entry-less save opens the slot; the lock that joins it owns it."""
    _run_gesture_node(_lane_config_burst("""
        lock(lanes[0]);
        await tick();
        saveWithoutEntry(lanes[1], 'Hero');
        lock(lanes[1]);
        await settle();
        assert.equal(sent.length, 2);
        assert.equal(w._undoStack.length, 2,
            'the lock that joined the rename slot must keep its entry');
        const entry = w._undoStack[1];
        assert.equal(entry.label, 'toggle track lock');
        assert.deepEqual(configs(entry.postSnapshot)[1], ['Hero', true]);
        assert.deepEqual(configs(entry.snapshot)[1], ['Hero', false],
            'the save is not history, so the entry reverses only the lock');
    """))


def test_a_lock_then_an_entryless_save_on_one_lane_keep_the_locks_undo_step():
    """The lock opens the slot; the entry-less save that joins it takes nothing.

    The old per-key map discarded the lock's entry when the save joined, and
    the save brought none, so the lock had no Undo at all. The lock's entry now
    covers the save as well, so its Undo reverts both -- deliberately: kept out
    of the slot, the save would be a lane write no entry reverses, which strands
    the lane family and refuses every earlier lane Undo.
    """
    _run_gesture_node(_lane_config_burst("""
        lock(lanes[0]);
        await tick();
        lock(lanes[1]);
        saveWithoutEntry(lanes[1], 'Hero');
        await settle();
        assert.equal(sent.length, 2);
        assert.equal(w._undoStack.length, 2);
        const entry = w._undoStack[1];
        assert.equal(entry.label, 'toggle track lock');
        assert.deepEqual(configs(entry.snapshot)[1], ['', false]);
        assert.deepEqual(configs(entry.postSnapshot)[1], ['Hero', true],
            'the entry covers the save that joined its slot');
        assertChain();
    """))


def test_a_lane_rename_owns_an_undo_step_so_an_earlier_lane_undo_still_restores():
    """The wedge found live in Phase 4, with no burst at all.

    Lock one lane and let it land, rename another, and the rename's write
    changed the lane-family bundle with no entry reversing it; the lock's Undo
    was then refused `scene_merge_conflict` and stayed on top. With the rename
    owning its step, the chain is unbroken.
    """
    _run_gesture_node(_lane_config_burst("""
        lock(lanes[0]);
        await settle();
        rename(lanes[1], 'Hero');
        await Promise.all(pending);
        assert.deepEqual(w._undoStack.map((entry) => entry.label),
            ['toggle track lock', 'rename lane']);
        assertChain();
        assert.deepEqual(configs(w._undoStack[1].snapshot)[1], ['', false],
            'the rename entry reverses only the rename');
    """))


def test_a_rename_then_a_lock_on_one_lane_are_one_undo_step():
    """Both edits of one lane inside one in-flight window: one write, one step.

    The rename opens the slot with its own entry, and the lock's joins it and
    is discarded, so one Ctrl+Z returns the lane to where it started.
    """
    _run_gesture_node(_lane_config_burst("""
        lock(lanes[0]);
        await tick();
        rename(lanes[1], 'Hero');
        lock(lanes[1]);
        await settle();
        assert.equal(sent.length, 2);
        assert.deepEqual(w._undoStack.map((entry) => entry.label),
            ['toggle track lock', 'rename lane']);
        assert.deepEqual(configs(w._undoStack[1].snapshot)[1], ['', false]);
        assert.deepEqual(configs(w._undoStack[1].postSnapshot)[1], ['Hero', true]);
        assertChain();
    """))


def test_a_lane_config_save_with_nothing_to_write_leaves_no_undo_entry():
    """The entry is pushed only after the early exits, and dropped on the last."""
    _run_gesture_node(_lane_config_burst("""
        w._laneTypeForEntry = () => '';
        await w._saveLaneConfigWithinGesture([lanes[1]], {undoLabel: 'rename lane'});
        await w._saveLaneConfigWithinGesture([], {undoLabel: 'rename lane'});
        assert.equal(w._undoStack.length, 0);
        assert.equal(sent.length, 0);
    """))


def _lane_rename_input(script: str) -> str:
    """`_startLaneRename` against a stub DOM input, recording saves."""
    return """
        const w = makeWidget();
        const listeners = {};
        let removed = 0;
        const input = {style: {}, value: '', placeholder: '',
            addEventListener: (type, handler) => { listeners[type] = handler; },
            focus() {}, select() {},
            remove() { removed += 1; listeners.blur?.(); }};
        globalThis.document = {createElement: () => input, body: {appendChild() {}}};
        w.timelineCanvas = {getBoundingClientRect: () => ({left: 0, top: 0})};
        Object.defineProperty(w, '_labelW', {value: 100});
        w._scaleTrackHeaders = 1; w.scrollY = 0;
        w._trackY = () => 0; w._trackH = () => 20;
        const lane = {type:'video', laneIndex:0, customName:'Old', label:'Old'};
        w._trackLayout = [lane];
        w.activeScene = {scene_id:'scene', video_lane_count:1,
            video_lane_configs:[{name:'Old'}]};
        const saves = [];
        w._saveLaneConfig = (entries, options) => { saves.push([entries[0].customName, options]); };
        w._startLaneRename(0);
        const key = (name) => listeners.keydown({key: name, preventDefault() {},
            stopPropagation() {}});
    """ + script


def test_a_lane_rename_on_enter_saves_once_with_its_own_undo_step():
    """Enter removed the input, and its blur finished the rename a second time."""
    _run_gesture_node(_lane_rename_input("""
        input.value = 'New';
        key('Enter');
        listeners.blur();
        assert.deepEqual(saves, [['New', {undoLabel: 'rename lane'}]]);
        assert.equal(removed, 1, 'the input is removed once');
    """))


def test_a_lane_rename_escape_saves_nothing():
    """Escape used to save through the blur its own removal fired."""
    _run_gesture_node(_lane_rename_input("""
        input.value = 'New';
        key('Escape');
        assert.deepEqual(saves, []);
        assert.equal(lane.customName, 'Old');
    """))


def test_clearing_a_lane_name_still_saves():
    """An empty name is a change back to the default label, not a no-op."""
    _run_gesture_node(_lane_rename_input("""
        input.value = '';
        key('Enter');
        assert.deepEqual(saves, [['', {undoLabel: 'rename lane'}]]);
    """))


def test_a_refused_stale_recipe_save_leaves_no_undo_entry():
    """The Reference panel's stale path: no paint, a 409, and no entry left.

    The panel names the lane it drew; when the lane at that index now has
    another id the save paints nothing and the server refuses it. The entry
    the save pushed must go with the refusal, or Undo meets an unstampable
    entry.
    """
    _run_gesture_node("""
        const w = makeWidget();
    """ + _REAL_HISTORY + """
        w.activeScene = {scene_id:'scene', reference_lane_count:1,
            reference_lane_configs:[{}],
            reference_lane_recipes:[{lane_id:'now', recipe_id:'old'}]};
        w._laneTypeForEntry = () => 'reference';
        w._defaultLaneConfig = () => ({});
        w._defaultReferenceLaneRecipe = () => ({lane_id:'', recipe_id:''});
        w._buildTrackLayout = () => {};
        const sent = [];
        w._runSceneMutation = (operations, options) => w._queueProjectMutation({
            ...options, refreshScenes:false, intent:{sceneId:'scene', operations},
            run: async (intent) => {
                sent.push(intent.operations);
                const error = new Error('identity_mismatch');
                error.status = 409;
                throw error;
            },
        });
        const entry = {type:'reference', laneIndex:0, customName:'', color:'',
            locked:false, hidden:false, referenceRecipe:{lane_id:'drawn', recipe_id:'new'}};
        await w._saveLaneConfigWithinGesture([entry],
            {expectedLaneId:'drawn', undoLabel:'change lane recipe'});
        await new Promise(r => setTimeout(r, 0));
        assert.equal(sent.length, 1);
        assert.equal(sent[0][0].expected.lane_id, 'drawn',
            'the save names the lane the panel drew');
        assert.equal(w.activeScene.reference_lane_recipes[0].recipe_id, 'old',
            'a stale save paints nothing');
        assert.equal(w._undoStack.length, 0, 'and leaves no entry behind');
    """)


def test_a_click_without_drag_discards_its_own_undo_entry_not_the_top_one():
    """Mousedown on an item pushes "move items"; a click that moves nothing
    must remove THAT entry.

    It popped the top instead. Finishing a lane rename by clicking an item runs
    the rename's blur after mousedown, so the rename's entry was on top and was
    the one popped: the rename was left with no Undo step, which strands the
    lane family, and the move entry was left unstamped. The canvas handler is
    inline in `_setupTimelineEvents`, so this pins the shape lexically.
    """
    source = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    start = source.index("    _setupTimelineEvents(")
    body = source[start:source.index("\n    }\n", start)]
    assert "this._undoStack.pop()" not in body, (
        "the timeline's canvas handlers must remove undo entries by object")
    no_move = body[body.index("// Click without drag"):]
    no_move = no_move[:no_move.index("if (this.selectedItems.length === 1)")]
    assert "this._discardUnstampableUndoEntry(this._dragHistoryEntry)" in no_move
    assert "this._dragHistoryEntry = null" in no_move


def test_an_unchanged_lane_name_writes_nothing():
    """No write, so no Undo step that reverses nothing."""
    _run_gesture_node(_lane_rename_input("""
        input.value = '  Old ';
        key('Enter');
        assert.deepEqual(saves, []);
    """))


def _stack_order_burst(body: str) -> str:
    """Same-key writes through the real queue and history, first write held."""
    return """
        const w = makeWidget();
    """ + _REAL_HISTORY + """
        w.activeScene = {scene_id:'scene', value:0, other:0};
        const server = {scene_id:'scene', value:0, other:0};
        const sent = [];
        let release = null;
        const write = (key, field, value) => {
            w.activeScene[field] = value;
            return w._queueProjectMutation({key, refreshScenes:false,
                intent:{sceneId:'scene', field, value},
                run: async (intent) => {
                    sent.push([intent.payload?.field ?? intent.field,
                        intent.payload?.value ?? intent.value]);
                    if (sent.length === 1) await new Promise(r => { release = r; });
                    server[intent.field] = intent.value;
                    return {payload:{scene: structuredClone(server)}};
                }});
        };
        const tick = () => new Promise(r => setTimeout(r, 0));
    """ + body


def test_a_slot_is_not_joined_past_another_gestures_undo_entry():
    """Oldest on the undo STACK, not first enqueued.

    A drag pushes its entry at mousedown and enqueues at mouseup. A same-key
    edit made during the drag must not join the slot opened before it: the
    merged write would land ahead of the drag's, the drag's entry would be
    stamped after that edit, and Undo of the drag would revert it too -- then
    the slot's own entry would reverse nothing.
    """
    _run_gesture_node(_stack_order_burst("""
        const pending = [];
        w._pushUndo('in flight'); pending.push(write('scene:other', 'other', 1));
        await tick();
        w._pushUndo('first'); pending.push(write('scene:value', 'value', 1));
        const drag = w._pushUndo('drag');          // mousedown: entry, no write yet
        w.activeScene.other = 2;                   // the drag paints
        w._pushUndo('second'); pending.push(write('scene:value', 'value', 2));
        // mouseup: the drag's own write, claiming its entry explicitly
        pending.push(w._queueProjectMutation({key:'scene:drag', historyEntry: drag,
            refreshScenes:false, intent:{sceneId:'scene', field:'other', value:2},
            run: async (intent) => {
                sent.push([intent.field, intent.value]);
                server.other = 2;
                return {payload:{scene: structuredClone(server)}};
            }}));
        release();
        await Promise.all(pending);
        assert.deepEqual(w._undoStack.map((entry) => entry.label),
            ['in flight', 'first', 'drag', 'second'],
            'the edit made during the drag keeps its own entry');
        assert.equal(sent.length, 4, 'and its own write');
        assert.ok(w._undoStack.every((entry) => entry.postSnapshot));
        assert.deepEqual(w._undoStack[1].postSnapshot.value, 1,
            "the first edit's entry reverses only the first edit");
    """))


def test_a_slot_whose_entry_left_the_stack_is_not_joined():
    """A scene switch clears the stack; a joiner must keep its live entry.

    Discarding it in favour of the slot's dead entry would leave the joiner's
    change with no Undo at all.
    """
    _run_gesture_node(_stack_order_burst("""
        const pending = [];
        w._pushUndo('in flight'); pending.push(write('scene:other', 'other', 1));
        await tick();
        w._pushUndo('first'); pending.push(write('scene:value', 'value', 1));
        w._undoStack.length = 0;                   // e.g. the scene was switched
        const live = w._pushUndo('second'); pending.push(write('scene:value', 'value', 2));
        release();
        await Promise.all(pending);
        assert.deepEqual(w._undoStack, [live]);
        assert.equal(live.postSnapshot?.value, 2, 'the joiner keeps and stamps its entry');
    """))


def test_a_different_key_between_two_members_keeps_authoring_order():
    """Tail-only: A, X, A is three writes in that order, never A+A then X.

    Merging the second A into the first slot sent it ahead of X, which was
    authored before it. X's entry was then stamped after the second A's change
    and its Undo reverted that change too, and the Undo after X's refused.
    """
    _run_gesture_node(_lane_config_burst("""
        lock(lanes[0]);
        await tick();
        lock(lanes[1]);
        lock(lanes[2]);
        lock(lanes[1]);
        await settle();
        assert.deepEqual(sent.map((write) => write.operations[0].lane_index),
            [0, 1, 2, 1], 'physical write order must equal authoring order');
        assert.equal(w._undoStack.length, 4);
        assertChain();
    """))


def test_a_failed_merged_write_discards_only_its_slots_entry():
    """The slot's entry goes with its write; entries of other slots stay."""
    _run_gesture_node(_lane_config_burst("""
        w._fetchScenes = async () => {};
        w._buildTrackLayout = () => {};
        const run = w._runSceneMutation;
        w._runSceneMutation = (operations, options) => {
            if (operations[0].lane_index !== 1) return run(operations, options);
            return w._queueProjectMutation({...options, refreshScenes:false,
                intent:{sceneId:'scene', operations},
                run: async () => { throw new Error('refused'); }});
        };
        lock(lanes[0]);
        await tick();
        lock(lanes[1]);
        lock(lanes[1]);
        lock(lanes[2]);
        await settle();
        assert.deepEqual(w._undoStack.map((entry) => entry.snapshot.video_lane_configs
            .map((config) => !!config.locked)), [
                [false, false, false],
                [true, false, false],
            ], 'lane 0 and lane 2 keep their entries; lane 1 keeps none');
        assert.ok(w._undoStack.every((entry) => entry.postSnapshot));
    """))


def test_the_inline_owner_token_path_never_discards_a_pending_entry():
    """A nested write owned by the running slot runs inline and merges nothing.

    A pending slot under the same key is not its target. Asking only "is this
    key pending" discarded that slot's entry and told its gesture it had lost
    headship, for a write that never joined it.
    """
    _run_gesture_node("""
        const w = makeWidget();
    """ + _REAL_HISTORY + """
        w.activeScene = {scene_id:'scene', value:0};
        let release = null, superseded = 0;
        let inline = null;
        const outer = w._queueProjectMutation({key:'scene:outer', refreshScenes:false,
            intent:{sceneId:'scene'},
            run: async (_intent, _diagnostics, ownerToken) => {
                await new Promise(r => { release = r; });
                inline = w._queueProjectMutation({key:'scene:value', ownerToken,
                    refreshScenes:false, intent:{sceneId:'scene'},
                    onSupersededByCoalescing: () => { superseded += 1; },
                    run: async () => ({payload:{scene:{scene_id:'scene', value:'inline'}}})});
                await inline;
                return {payload:{scene:{scene_id:'scene', value:'outer'}}};
            }});
        await new Promise(r => setTimeout(r, 0));
        const queued = w._pushUndo('queued edit');
        const pendingWrite = w._queueProjectMutation({key:'scene:value', refreshScenes:false,
            intent:{sceneId:'scene'},
            run: async () => ({payload:{scene:{scene_id:'scene', value:'queued'}}})});
        release();
        await Promise.all([outer, pendingWrite]);
        assert.equal(superseded, 0, 'the inline write joined nothing');
        assert.ok(w._undoStack.includes(queued), 'the pending slot keeps its entry');
        assert.equal(queued.postSnapshot.value, 'queued');
    """)


def test_a_duration_burst_behind_a_write_undoes_to_the_original_in_one_step():
    """A coalescer with no `merge` still keeps and stamps its oldest entry.

    Without the always-wrapped merge the queue replaced the queued value
    wholesale -- the entry with it -- so the slot kept the newest member's
    entry, which the gesture had just discarded. Undo then dropped the burst as
    unstampable and the duration could not be undone at all.
    """
    _run_gesture_node("""
        const w = makeWidget();
    """ + _REAL_HISTORY + """
        w.activeScene = {scene_id:'scene', duration_frames:100};
        w.totalFrames = 100;
        for (const name of ['_clampTimelineStateToDuration', '_refreshDurationInput',
                '_updateToolbar', '_updateTransportUI']) w[name] = () => {};
        const server = {scene_id:'scene', duration_frames:100};
        const sent = [];
        let release = null;
        w._runSceneMutation = (operations, options) => w._queueProjectMutation({
            ...options, refreshScenes:false, intent:{sceneId:'scene', operations},
            run: async (intent) => {
                sent.push(intent.operations);
                if (sent.length === 1) await new Promise(r => { release = r; });
                Object.assign(server, intent.operations[0].fields);
                return {payload:{scene: structuredClone(server)}};
            },
        });
        const pending = [w._updateSceneDurationWithinGesture(120)];
        await new Promise(r => setTimeout(r, 0));
        pending.push(w._updateSceneDurationWithinGesture(140));
        pending.push(w._updateSceneDurationWithinGesture(160));
        release();
        await Promise.all(pending);
        assert.equal(sent.length, 2);
        assert.deepEqual(w._undoStack.map((entry) => [entry.snapshot.duration_frames,
            entry.postSnapshot?.duration_frames]), [[100, 120], [120, 160]],
            'the burst is one step from 120 to 160, stamped from the merged write');
    """)


def test_a_clip_role_burst_behind_a_queued_undo_keeps_every_authored_lane():
    """render -> driver -> render -> driver, then render, behind a queued Undo.

    The conversions share one key and one slot, and the slot keeps each
    member's `set_lane_count`. History rebases each absolute count as a delta
    from the slot entry's before-snapshot. That snapshot is the oldest member's,
    taken before any of the burst painted, so both lanes the burst appended land
    on top of the lane the Undo restored. Measured from the newest member's
    snapshot instead -- the old survivor -- the first appended lane read as
    already present, the count came out one short, and the clip was left on the
    lane the Undo had brought back.
    """
    _run_gesture_node("""
        const w = makeWidget();
    """ + _REAL_HISTORY + """
        w.activeScene = {scene_id:'scene', video_lane_count:1, motion_driver_lane_count:1,
            video_lane_configs:[{}], motion_driver_lane_configs:[{}],
            clips:[{clip_id:'c1', role:'render', track_index:0, strength:1.0}]};
        w._defaultLaneConfig = () => ({});
        w._clearSelection = () => {}; w._hideItemEditor = () => {};
        w._defaultMotionDriverStrength = () => 0.5;
        w._firstEmptyUnlockedDriverLane = () => 0;
        w._getAssetForSourcePath = () => ({asset_type:'video'});
        // What the queued Undo leaves: one more video lane than the author saw.
        const ordered = structuredClone(w.activeScene);
        ordered.video_lane_count = 2;
        ordered.video_lane_configs = [{}, {name:'restored'}];
        let server = null;
        const context = w._beginHistoryOrderContext('undo', 1);
        let releaseUndo = null;
        const undo = w._projectMutationQueue.enqueue({key:'history:undo', coalesce:false,
            sealCoalescing:true, run: async () => {
                await new Promise(r => { releaseUndo = r; });
                server = structuredClone(ordered);
                context.scenes.set('scene', structuredClone(ordered));
            }});
        await new Promise(r => setTimeout(r, 0));
        const sent = [];
        w._runSceneMutation = (operations, options) => w._queueProjectMutation({
            ...options, refreshScenes:false, intent:{sceneId:'scene', operations},
            run: async (intent) => {
                sent.push(intent.operations);
                for (const op of intent.operations) {
                    if (op.type === 'set_lane_count') server[`${op.lane_type}_lane_count`] = op.count;
                    if (op.type === 'update_clip') Object.assign(server.clips[0], op.fields);
                }
                return {payload:{scene: structuredClone(server)}};
            },
        });
        const pending = [];
        for (const role of ['motion_driver', 'render', 'motion_driver', 'render']) {
            pending.push(w._convertClipRoleWithinGesture('c1', role));
        }
        assert.equal(w.activeScene.video_lane_count, 3, 'the burst painted two new lanes');
        releaseUndo();
        await Promise.all([undo, ...pending]);
        assert.equal(sent.length, 1, 'the four conversions are one write');
        assert.equal(server.video_lane_count, 4,
            'the lane the Undo restored plus both lanes the burst appended');
        assert.equal(server.clips[0].role, 'render');
        assert.equal(server.clips[0].track_index, 3,
            'the clip lands on the last lane the burst appended');
        assert.equal(w._undoStack.length, 1, 'one slot, one Undo step');
        assert.equal(w._undoStack[0].postSnapshot.video_lane_count, 4);
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


# ── Split: one path, one gesture (umbrella Phase C stage 2 L2/L3) ────────────
#
# The razor and "Split Here" share `_splitItemsAtFrameWithinGesture`, so these
# drive the real method against a plain-object scene. What they pin is the part
# a manual check cannot see reliably: how many operations one gesture emits, how
# many undo entries it pushes, and whether a refusal reached the bus at all.

_SPLIT_SETUP = """
    const notes = await import('./web/js/editor_notifications.js');
    function splitWidget(scene) {
        const w = makeWidget();
        w.activeScene = scene;
        w.totalFrames = 1000;
        w.undos = [];
        w._pushUndo = (label) => { const e = {label}; w.undos.push(e); return e; };
        w._isItemLocked = () => false;
        w._isMotionDriverClip = () => false;
        w._clipTrackType = () => 'video';
        w._isLaneLocked = () => false;
        w._isPromptTrackLocked = () => false;
        // `_isLinkedItem`, `_expandItemsWithLinked`, `_selectionItemKey`,
        // `_linkRefForItem`, `_linkRefKey`, `_linkGroupForItem` and
        // `_findSceneItemForLinkRef` are all left REAL. The closure key is the
        // thing these tests are about, so stubbing the helpers that build it
        // would make the assertion supply its own answer.
        w._buildTrackLayout = () => {};
        w._refreshPromptContextDependencyConsumers = () => { w.promptRefreshes = (w.promptRefreshes || 0) + 1; };
        w._warnOnSplitReferenceChipBindings = () => {};
        w._findSceneItemBySelection = (type, id) => {
            if (type === 'clip') {
                const clip = (scene.clips || []).find((c) => String(c.clip_id) === String(id));
                return clip ? {type, id: clip.clip_id, data: clip} : null;
            }
            if (type === 'audio') {
                const track = (scene.audio_tracks || []).find((t) => String(t.track_id) === String(id));
                return track ? {type, id: track.track_id, data: track} : null;
            }
            return null;
        };
        w._selectionItemKey = (item) => `${item?.type || ''}:${String(item?.id ?? '')}`;
        w.sent = [];
        w._runSceneMutation = (operations, options) => w._queueProjectMutation({
            ...options, refreshScenes: false, intent: {operations},
            run: async () => { w.sent.push({operations, options}); return {payload: {results: []}}; },
        });
        return w;
    }
    function captureNotes() {
        notes._resetForTest();
        const seen = [];
        notes.subscribe((list) => { for (const item of list) {
            if (!seen.some((v) => v.id === item.id)) seen.push(item);
        } });
        return seen;
    }
    const clipScene = () => ({
        scene_id: 'scene',
        clips: [{clip_id: 'c1', timeline_start_frame: 0, timeline_end_frame: 100,
                 track_index: 0, role: 'render'}],
        audio_tracks: [{track_id: 'a1', timeline_start_frame: 0, timeline_end_frame: 100,
                        lane_index: 0}],
        prompt_sections: [],
    });
"""


def test_a_split_gesture_emits_one_operation_per_link_closure():
    """Two selected members of ONE group are one cut, not two.

    `_apply_split_linked` dissolves the group and moves the left half on its
    first pass, so a second operation over the same closure finds an anchor
    whose bounds it has already changed. Before stage 2 L1 that was silent; it
    is now a 400 that refuses the WHOLE gesture, which is worse for the author
    and exactly the shape the Critical entry describes.
    """
    _run_gesture_node(_SPLIT_SETUP + """
        const scene = clipScene();
        // A REAL link group. `_expandItemsWithLinked` resolves it through
        // `_linkGroupForItem`, so both members must produce the same closure key
        // for the dedupe to fire -- which is the property under test.
        scene.linked_item_groups = [{group_id: 'g1', items: [
            {type: 'clip', id: 'c1'}, {type: 'audio', id: 'a1'}]}];
        const w = splitWidget(scene);
        const keys = [
            w._planItemSplit({type: 'clip', id: 'c1', data: scene.clips[0]}, 50).closureKey,
            w._planItemSplit({type: 'audio', id: 'a1', data: scene.audio_tracks[0]}, 50).closureKey,
        ];
        assert.equal(keys[0], keys[1], 'both members name one closure');
        assert.equal(keys[0], 'audio:a1|clip:c1', 'sorted, so selection order cannot change it');
        await w._splitItemsAtFrame([
            {type: 'clip', id: 'c1', data: scene.clips[0]},
            {type: 'audio', id: 'a1', data: scene.audio_tracks[0]},
        ], 50);
        assert.equal(w.sent.length, 1, 'one write');
        assert.equal(w.sent[0].operations.length, 1, 'one operation for one closure');
        assert.equal(w.sent[0].operations[0].type, 'split_clip');
        assert.equal(w.sent[0].operations[0].apply_linked, true);
        assert.equal(w.undos.length, 1, 'one undo entry');
        assert.equal(w.sent[0].options.historyEntry, w.undos[0],
            'the entry is passed explicitly, not left to the capture candidate');
        assert.equal(w.sent[0].options.coalesce, false);
    """)


def test_unlinked_targets_batch_into_one_gesture_with_one_undo_entry():
    """Several independent cuts are one author action and one history step."""
    _run_gesture_node(_SPLIT_SETUP + """
        const scene = clipScene();
        const w = splitWidget(scene);
        await w._splitItemsAtFrame([
            {type: 'clip', id: 'c1', data: scene.clips[0]},
            {type: 'audio', id: 'a1', data: scene.audio_tracks[0]},
        ], 50);
        assert.equal(w.sent.length, 1);
        assert.deepEqual(w.sent[0].operations.map((op) => op.type),
            ['split_clip', 'split_audio_track']);
        assert.equal(w.undos.length, 1);
        assert.equal(w.undos[0].label, 'split 2 items');
    """)


def test_a_wholly_refused_split_is_history_neutral_and_says_why():
    """Planning before `_pushUndo` is what keeps this true as targets multiply.

    The old single-target path was already history-neutral -- it pushed its entry
    after all five refusal checks -- so this is not a defect being fixed. What
    changed is that a gesture now plans N targets and pushes ONE entry, and the
    only ordering that stays neutral when SOME targets refuse is plan-everything
    first. Pushing per target, or pushing before planning, would strand an entry
    naming cuts that never happened. The message half IS new: most of those old
    refusals returned in silence.
    """
    _run_gesture_node(_SPLIT_SETUP + """
        const scene = clipScene();
        const w = splitWidget(scene);
        const seen = captureNotes();
        // Frame 300 is past the clip's end and outside the audio track too.
        await w._splitItemsAtFrame([
            {type: 'clip', id: 'c1', data: scene.clips[0]},
            {type: 'audio', id: 'a1', data: scene.audio_tracks[0]},
        ], 300);
        assert.equal(w.sent.length, 0, 'nothing written');
        assert.equal(w.undos.length, 0, 'no history entry to strand');
        assert.equal(seen.length, 1, 'one message, not one per target');
        assert.match(seen[0].message, /do not cross the split frame/);
        assert.equal(seen[0].tier, 'warning');
    """)


def test_a_partly_refused_split_cuts_what_qualifies_and_names_the_rest():
    """Split the ones that cross; name the ones that did not."""
    _run_gesture_node(_SPLIT_SETUP + """
        const scene = clipScene();
        scene.audio_tracks[0].timeline_end_frame = 20;
        const w = splitWidget(scene);
        const seen = captureNotes();
        await w._splitItemsAtFrame([
            {type: 'clip', id: 'c1', data: scene.clips[0]},
            {type: 'audio', id: 'a1', data: scene.audio_tracks[0]},
        ], 50);
        assert.equal(w.sent.length, 1);
        assert.deepEqual(w.sent[0].operations.map((op) => op.type), ['split_clip']);
        assert.equal(seen.length, 1);
        // The wording changes with the outcome: "left whole" is a report,
        // "A cut has to fall inside an item" is a failure.
        assert.match(seen[0].message, /left whole/);
    """)


def test_each_refusal_reason_gets_its_own_notification_source():
    """`editor_notifications.js` coalesces on `src:<source>`.

    A shared source would let two different sentences replace one another, so
    the author would be told about one refusal and never the other.
    """
    _run_gesture_node(_SPLIT_SETUP + """
        const scene = clipScene();
        // The audio track still spans the cut, so it reaches the lock check;
        // the clip does not, so it stops at the bounds check. Bounds is tested
        // first -- as it was before this landing -- so a frame outside BOTH
        // would collapse the two into one reason and prove nothing.
        scene.audio_tracks[0].timeline_end_frame = 500;
        const w = splitWidget(scene);
        w._isItemLocked = (item) => item.type === 'audio';
        const seen = captureNotes();
        await w._splitItemsAtFrame([
            {type: 'clip', id: 'c1', data: scene.clips[0]},
            {type: 'audio', id: 'a1', data: scene.audio_tracks[0]},
        ], 300);
        assert.equal(w.sent.length, 0);
        assert.equal(seen.length, 2, 'both refusals reached the bus');
        assert.equal(new Set(seen.map((v) => v.source)).size, 2);
        assert.ok(seen.some((v) => /locked lane/.test(v.message)));
        assert.ok(seen.some((v) => /cross the split frame/.test(v.message)));
    """)


def test_split_here_with_nothing_selected_says_so_and_writes_nothing():
    """The removed fallback's replacement.

    "Split Here" used to cut whatever sat under the playhead when nothing was
    selected, which made it a second razor with different rules -- it could
    never reach a Reference item and silently ignored prompt sections.
    """
    _run_gesture_node(_SPLIT_SETUP + """
        const w = splitWidget(clipScene());
        const seen = captureNotes();
        await w._splitItemsAtFrame([], 50);
        assert.equal(w.sent.length, 0);
        assert.equal(w.undos.length, 0);
        assert.equal(seen.length, 1);
        assert.match(seen[0].message, /Select a clip/);
    """)


def test_a_prompt_split_is_addressed_by_prompt_id_not_the_selection_index():
    """`_split_prompt_object` re-sorts `prompt_sections` on every cut.

    An index captured when the section was selected therefore names a different
    row after any earlier cut, and the wire operation is index-addressed, so the
    translation has nowhere to happen but here. The selection below claims index
    0 while its `prompt_id` now sits at index 1.
    """
    _run_gesture_node(_SPLIT_SETUP + """
        const scene = clipScene();
        scene.prompt_sections = [
            {prompt_id: 'p-early', start_frame: 0, end_frame: 40},
            {prompt_id: 'p-late', start_frame: 40, end_frame: 100},
        ];
        const w = splitWidget(scene);
        await w._splitItemsAtFrame([
            {type: 'prompt', id: 0, data: {prompt_id: 'p-late', start_frame: 40, end_frame: 100}},
        ], 60);
        assert.equal(w.sent.length, 1);
        const op = w.sent[0].operations[0];
        assert.equal(op.type, 'split_prompt_section');
        assert.equal(op.index, 1, 'the index `p-late` currently occupies');
        assert.equal(op.expected.prompt_id, 'p-late');
        // The Prompt panel holds no scene subscription, so a prompt split that
        // does not call this leaves it showing the pre-cut sections.
        assert.equal(w.promptRefreshes, 1);
    """)


def test_follow_up_work_reads_the_whole_closure_not_just_the_anchor():
    """The defect an adversarial audit of this landing found.

    `_apply_split_linked` splits every in-bounds member of the closure, so a cut
    anchored on a CLIP that is linked to a prompt section splits the prompt
    section too. Deriving the follow-up work from the anchor's type meant the
    Prompt panel refresh depended on which member of the group the author
    happened to click or select first -- and the razor, whose anchor is always
    the item under the cursor, never refreshed at all. The panel holds no scene
    subscription, so the author was left looking at the pre-cut sections: exactly
    the defect this landing claims to close.

    Both orders are driven here, because one order passed before the fix.
    """
    _run_gesture_node(_SPLIT_SETUP + """
        function linkedClipAndPrompt() {
            const scene = clipScene();
            scene.prompt_sections = [{prompt_id: 'p1', start_frame: 0, end_frame: 100}];
            scene.linked_item_groups = [{group_id: 'g1', items: [
                {type: 'clip', id: 'c1'}, {type: 'prompt', id: 'p1'}]}];
            return scene;
        }
        for (const anchorFirst of ['clip', 'prompt']) {
            const scene = linkedClipAndPrompt();
            const w = splitWidget(scene);
            const clipHit = {type: 'clip', id: 'c1', data: scene.clips[0]};
            const promptHit = {type: 'prompt', id: 0, data: scene.prompt_sections[0]};
            const order = anchorFirst === 'clip' ? [clipHit, promptHit] : [promptHit, clipHit];
            await w._splitItemsAtFrame(order, 50);
            assert.equal(w.sent.length, 1, `${anchorFirst}: one closure, one operation`);
            assert.equal(w.sent[0].operations.length, 1);
            assert.equal(w.sent[0].operations[0].apply_linked, true);
            // The server splits the prompt member either way, so the panel must
            // be refreshed either way.
            assert.equal(w.promptRefreshes, 1,
                `${anchorFirst} anchor: the Prompt panel must be refreshed`);
            // And the label says the cut covered a group rather than naming one
            // member, which was previously order-dependent too.
            assert.equal(w.undos[0].label, `split linked ${anchorFirst}`);
        }

        // The razor path: ONE hit, always the item under the cursor, linked.
        const scene = linkedClipAndPrompt();
        const w = splitWidget(scene);
        await w._splitItemsAtFrame([{type: 'clip', id: 'c1', data: scene.clips[0]}], 50);
        assert.equal(w.promptRefreshes, 1,
            'a razor cut on a clip linked to a prompt section still splits it');
    """)


def test_a_multi_target_split_failure_says_no_cut_was_applied():
    """One refused target abandons every other cut, and that has to be said.

    The batch is all-or-nothing by design -- it is what makes one undo entry an
    honest claim -- but the previous "Split Here" looped one gesture per target,
    so the others DID land. A server message naming one stale clip would
    otherwise read as though the rest succeeded.
    """
    _run_gesture_node(_SPLIT_SETUP + """
        const scene = clipScene();
        const w = splitWidget(scene);
        w._runSceneMutation = (operations, options) => {
            w.sent.push({operations, options});
            return Promise.reject(new Error("This clip's start frame changed."));
        };
        await w._splitItemsAtFrame([
            {type: 'clip', id: 'c1', data: scene.clips[0]},
            {type: 'audio', id: 'a1', data: scene.audio_tracks[0]},
        ], 50);
        const message = w.sent[0].options.failureMessage(
            new Error("This clip's start frame changed."));
        assert.match(message, /start frame changed/, 'the server wording survives');
        assert.match(message, /None of the 2 cuts/, 'and the batch outcome is disclosed');

        // A single-target gesture says only what the server said -- there is no
        // other cut to report on, and padding every refusal would be noise.
        const solo = splitWidget(clipScene());
        solo._runSceneMutation = (operations, options) => {
            solo.sent.push({operations, options});
            return Promise.reject(new Error('nope'));
        };
        await solo._splitItemsAtFrame(
            [{type: 'clip', id: 'c1', data: solo.activeScene.clips[0]}], 50);
        assert.equal(solo.sent[0].options.failureMessage(new Error('nope')), 'nope');
    """)


def test_a_boundary_miss_auto_dismisses_while_a_real_refusal_stays():
    """Warnings are sticky and the toast stack has no cap.

    In razor mode `_hitTestClip` matches inclusively at both edges and
    `_xToFrame` rounds, so at high zoom a click within half a frame of an edge --
    or on the seam between two abutting clips -- resolves to the boundary. That
    is an aiming miss, and leaving a card the author must dismiss by hand for
    every near-miss is a worse trade than the silence it replaced.
    """
    _run_gesture_node(_SPLIT_SETUP + """
        const policy = splitWidget(clipScene());
        // The policy itself. 0 is what `editor_notifications.js` reads as "no
        // override", so these reasons keep the sticky default their tier gives
        // them -- three of them were already sticky before this landing.
        assert.ok(policy._splitRefusalDurationMs('outside_bounds') > 0);
        for (const reason of ['lane_locked', 'linked_locked', 'driver', 'missing']) {
            assert.equal(policy._splitRefusalDurationMs(reason), 0, reason);
        }

        // And the runner consults it for every refusal it raises, rather than
        // the policy existing beside a call site that ignores it. The subscriber
        // projection drops `_ttl`, so the wiring is not observable from the bus.
        const scene = clipScene();
        // The audio track still spans the cut so it reaches the lock check.
        scene.audio_tracks[0].timeline_end_frame = 500;
        const w = splitWidget(scene);
        w._isItemLocked = (item) => item.type === 'audio';
        const asked = [];
        w._splitRefusalDurationMs = (reason) => { asked.push(reason); return 0; };
        const seen = captureNotes();
        // Frame 100 is exactly the clip's end -- `_hitTestClip` matches
        // inclusively there, so razor mode really can produce it.
        await w._splitItemsAtFrame([
            {type: 'clip', id: 'c1', data: scene.clips[0]},
            {type: 'audio', id: 'a1', data: scene.audio_tracks[0]},
        ], 100);
        assert.deepEqual(asked.sort(), ['lane_locked', 'outside_bounds']);
        assert.equal(seen.filter((v) => /timeline-split-refused/.test(v.source)).length, 2);
    """)


def test_a_target_the_scene_no_longer_holds_refuses_instead_of_throwing():
    """The one silent failure left after "every silent return now notifies".

    A stale selection can name a row the scene no longer holds.
    `_resolveSplitTarget` falls back to the raw hit, so the bounds read went
    through `undefined` and threw a TypeError out of the gesture -- and the razor
    does not await, so it became an unhandled rejection nobody ever saw.
    """
    _run_gesture_node(_SPLIT_SETUP + """
        const w = splitWidget(clipScene());
        const seen = captureNotes();
        await w._splitItemsAtFrame([{type: 'clip', id: 'ghost'}], 50);
        assert.equal(w.sent.length, 0);
        assert.equal(w.undos.length, 0);
        assert.equal(seen.length, 1);
        assert.match(seen[0].message, /no longer on the timeline/);
    """)


# ── Split: the optimistic local apply (umbrella Phase C stage 2 L4/L5) ───────
#
# The tracked Critical defect is not that a stale cut was refused -- stage 2 L1
# made it refuse. It is that the cut was AIMED at stale geometry, because the
# timeline showed pre-cut bounds for the whole write window. These drive the
# real gesture with a runner that never resolves, which is exactly the window
# the defect lives in.

_OPTIMISTIC_SETUP = _SPLIT_SETUP + """
    function pendingWidget(scene) {
        const w = splitWidget(scene);
        w.released = [];
        w._runSceneMutation = (operations, options) => {
            w.sent.push({operations, options});
            return new Promise((resolve, reject) => {
                w.released.push({resolve, reject});
            });
        };
        return w;
    }
"""


def test_a_split_paints_both_halves_before_the_server_answers():
    """The landing's whole purpose, and the write window is where it matters."""
    _run_gesture_node(_OPTIMISTIC_SETUP + """
        const scene = clipScene();
        const w = pendingWidget(scene);
        const pending = w._splitItemsAtFrame(
            [{type: 'clip', id: 'c1', data: scene.clips[0]}], 40);
        await new Promise((r) => setTimeout(r, 0));

        assert.equal(w.sent.length, 1, 'the write is in flight');
        assert.equal(scene.clips.length, 2, 'and both halves are already drawn');
        const left = scene.clips.find((c) => c.clip_id === 'c1');
        const right = scene.clips.find((c) => c.clip_id !== 'c1');
        assert.deepEqual([left.timeline_start_frame, left.timeline_end_frame], [0, 40]);
        assert.deepEqual([right.timeline_start_frame, right.timeline_end_frame], [40, 100]);
        // The painted half carries the id the operation asked the server to use,
        // or the next edit against it would name a row the server does not hold.
        const minted = w.sent[0].operations[0].right_ids['clip:c1'];
        assert.equal(right.clip_id, minted);
        assert.match(minted, /^[a-f0-9]{8}$/, 'the same shape a server id has');

        w.released[0].resolve({payload: {results: [
            {type: 'split_item', split_count: 1,
             right_items: [{type: 'clip', id: minted}]}]}});
        await pending;
    """)


def test_the_second_cut_of_a_burst_aims_at_the_first_cut_s_geometry():
    """The defect itself, stated as a test.

    Measured 2026-09-05: a first cut split a clip to [0,48], and the next two
    cuts -- queued while it was still saving -- were hit-tested against the
    pre-cut bar and aimed at the same clip id, whose bounds the server had
    already changed. Both fell through and did nothing. With the halves painted,
    the second cut resolves against real geometry instead.
    """
    _run_gesture_node(_OPTIMISTIC_SETUP + """
        const scene = clipScene();
        const w = pendingWidget(scene);
        const first = w._splitItemsAtFrame(
            [{type: 'clip', id: 'c1', data: scene.clips[0]}], 40);
        await new Promise((r) => setTimeout(r, 0));

        // Nothing has been saved. A second cut at frame 70 now falls inside the
        // RIGHT half, not inside the clip the author first clicked.
        const right = scene.clips.find((c) => c.clip_id !== 'c1');
        const second = w._splitItemsAtFrame(
            [{type: 'clip', id: right.clip_id, data: right}], 70);
        await new Promise((r) => setTimeout(r, 0));

        assert.equal(w.sent.length, 2, 'both cuts were sent');
        const op = w.sent[1].operations[0];
        assert.equal(op.clip_id, right.clip_id,
            'the second cut names the half it actually landed in');
        assert.equal(op.expected.timeline_start_frame, 40,
            'and guards against that half, not against the original clip');
        assert.equal(scene.clips.length, 3, 'three halves painted, none lost');

        for (const slot of w.released) {
            slot.resolve({payload: {results: [{type: 'split_item', split_count: 1,
                right_items: [{type: 'clip', id: 'anything'}]}]}});
        }
        await Promise.allSettled([first, second]);
    """)


def test_a_closure_holding_a_prompt_section_paints_nothing_at_all():
    """The carve-out, and why it is the whole closure rather than the prompt.

    `clone_for_split` mints attachment ids, rewrites document nodes and drops
    shot and timestamp markers; mirroring it would be a second authority over
    prompt document semantics. Painting only the video half would leave the
    prompt lane disagreeing with the lanes above it for the whole window, which
    is worse than painting nothing.
    """
    _run_gesture_node(_OPTIMISTIC_SETUP + """
        const scene = clipScene();
        scene.prompt_sections = [{prompt_id: 'p1', start_frame: 0, end_frame: 100}];
        scene.linked_item_groups = [{group_id: 'g1', items: [
            {type: 'clip', id: 'c1'}, {type: 'prompt', id: 'p1'}]}];
        const w = pendingWidget(scene);
        const pending = w._splitItemsAtFrame(
            [{type: 'clip', id: 'c1', data: scene.clips[0]}], 40);
        await new Promise((r) => setTimeout(r, 0));

        assert.equal(w.sent.length, 1);
        assert.equal(scene.clips.length, 1, 'nothing painted');
        assert.equal(scene.clips[0].timeline_end_frame, 100, 'and nothing moved');
        assert.deepEqual(w.sent[0].operations[0].right_ids, {},
            'and no id was minted for a half that will not be drawn');

        w.released[0].resolve({payload: {results: [
            {type: 'split_linked_items', split_count: 2, right_items: []}]}});
        await pending;
    """)


def test_a_failed_split_discards_its_entry_and_refetches():
    """The refetch IS the rollback, and it cannot be left to the queue.

    `_queueProjectMutation`'s failure branch toasts, discards the entry and
    DEFERS a refresh, which `_replayDeferredProjectBackedRefresh` holds while a
    drag is live or `_timelineMutationDepth > 0` -- so the optimistic halves
    would stay on screen. Inventing an inverse delta instead would be a second
    authority over what a split undoes.
    """
    _run_gesture_node(_OPTIMISTIC_SETUP + """
        const scene = clipScene();
        const w = pendingWidget(scene);
        const refetches = [];
        w._fetchScenes = async (options) => { refetches.push(options); };
        const discarded = [];
        w._discardUnstampableUndoEntry = (entry) => { discarded.push(entry); };

        const pending = w._splitItemsAtFrame(
            [{type: 'clip', id: 'c1', data: scene.clips[0]}], 40);
        await new Promise((r) => setTimeout(r, 0));
        assert.equal(scene.clips.length, 2, 'painted');

        w.released[0].reject(new Error('refused'));
        await pending;
        assert.deepEqual(discarded, [w.undos[0]], 'the exact entry, by object');
        assert.equal(refetches.length, 1);
        assert.equal(refetches[0].ignoreMutationGate, true);
        assert.equal(refetches[0].reason, 'split_item_error');
    """)


def test_a_split_that_cut_nothing_says_so_only_when_nothing_was_cut():
    """The safety net, kept narrow on purpose.

    The roadmap is explicit that reading `split_count` must not become a refusal
    pattern that punishes a second cut -- that pattern is what the Critical undo
    entry exists to remove. So a partial shortfall is a diagnostic and silence;
    only a wholly ineffective gesture speaks.
    """
    _run_gesture_node(_OPTIMISTIC_SETUP + """
        // Every operation came back having cut nothing.
        const wholly = pendingWidget(clipScene());
        const seen = captureNotes();
        const p1 = wholly._splitItemsAtFrame(
            [{type: 'clip', id: 'c1', data: wholly.activeScene.clips[0]}], 40);
        await new Promise((r) => setTimeout(r, 0));
        wholly.released[0].resolve({payload: {results: [
            {type: 'split_item', split_count: 0, right_items: []}]}});
        await p1;
        assert.equal(seen.filter((v) => v.source === 'timeline-split-no-effect').length, 1);

        // One of two cut nothing: a diagnostic, and NO toast.
        const partial = pendingWidget(clipScene());
        const before = seen.length;
        const p2 = partial._splitItemsAtFrame([
            {type: 'clip', id: 'c1', data: partial.activeScene.clips[0]},
            {type: 'audio', id: 'a1', data: partial.activeScene.audio_tracks[0]},
        ], 40);
        await new Promise((r) => setTimeout(r, 0));
        const ops = partial.sent[0].operations;
        partial.released[0].resolve({payload: {results: [
            {type: 'split_item', split_count: 1,
             right_items: [{type: 'clip', id: ops[0].right_ids['clip:c1']}]},
            {type: 'split_item', split_count: 0, right_items: []},
        ]}});
        await p2;
        assert.equal(seen.slice(before)
            .filter((v) => v.source === 'timeline-split-no-effect').length, 0,
            'a partial shortfall must not punish the cut that landed');
    """)


def test_the_shortfall_check_prefers_the_minted_id_over_split_count():
    """A minted id is the sharper evidence: it names the row.

    `split_count` only says a cut happened somewhere in the closure. The id says
    the server built the half the client is already painting, under that name --
    which is the thing the next edit depends on.
    """
    _run_gesture_node(_OPTIMISTIC_SETUP + """
        const w = splitWidget(clipScene());
        const planned = [{optimistic: [{type: 'clip', id: 'c1', rightId: 'abc12345'}]}];

        // `split_count` is happy, but the server built a DIFFERENT id.
        const wrongId = w._splitOutcomeShortfall({payload: {results: [
            {split_count: 1, right_items: [{type: 'clip', id: 'other999'}]}]}}, planned);
        assert.equal(wrongId.length, 1);
        assert.deepEqual(wrongId[0].missing, ['abc12345']);

        const matched = w._splitOutcomeShortfall({payload: {results: [
            {split_count: 1, right_items: [{type: 'clip', id: 'abc12345'}]}]}}, planned);
        assert.deepEqual(matched, []);

        // With nothing minted, `split_count` is the evidence -- WHERE THE BRANCH
        // SENDS ONE. The prompt branch does. Hand-writing a `split_count` onto a
        // fixture for a branch that omits it is how the Reference defect below
        // survived the first version of this test, so these shapes are copied
        // from the dispatcher rather than invented.
        const coarse = [{optimistic: null}];
        assert.equal(w._splitOutcomeShortfall(
            {payload: {results: [{type: 'split_item', split_count: 0, right_items: []}]}},
            coarse).length, 1);
        assert.deepEqual(w._splitOutcomeShortfall(
            {payload: {results: [{type: 'split_item', split_count: 1,
                                  right_items: [{type: 'prompt', id: 'p2'}]}]}},
            coarse), []);
    """)


def test_a_linked_split_paints_every_member_of_the_closure():
    """`_apply_split_linked` divides them all, so the paint has to as well."""
    _run_gesture_node(_OPTIMISTIC_SETUP + """
        const scene = clipScene();
        scene.linked_item_groups = [{group_id: 'g1', items: [
            {type: 'clip', id: 'c1'}, {type: 'audio', id: 'a1'}]}];
        const w = pendingWidget(scene);
        const pending = w._splitItemsAtFrame(
            [{type: 'clip', id: 'c1', data: scene.clips[0]}], 40);
        await new Promise((r) => setTimeout(r, 0));

        assert.equal(scene.clips.length, 2);
        assert.equal(scene.audio_tracks.length, 2);
        const ids = w.sent[0].operations[0].right_ids;
        assert.deepEqual(Object.keys(ids).sort(), ['audio:a1', 'clip:c1']);
        assert.equal(new Set(Object.values(ids)).size, 2, 'distinct ids');
        assert.ok(scene.audio_tracks.some((t) => t.track_id === ids['audio:a1']));
        // The right half is NOT a group member yet: the partition is the
        // server's, and arrives with the canonical scene.
        assert.deepEqual(scene.linked_item_groups[0].items,
            [{type: 'clip', id: 'c1'}, {type: 'audio', id: 'a1'}]);

        w.released[0].resolve({payload: {results: [
            {type: 'split_linked_items', split_count: 2, right_items: [
                {type: 'clip', id: ids['clip:c1']},
                {type: 'audio', id: ids['audio:a1']}]}]}});
        await pending;
    """)


def test_a_server_that_ignores_the_minted_id_is_not_reported_as_cutting_nothing():
    """A live configuration, not a hypothetical.

    `routes.py` needs a ComfyUI restart while `web/js` reloads on a browser
    refresh, so a pack update leaves new frontend talking to old backend until
    the server is restarted. A server predating stage 2 L4 ignores `right_ids`
    and names the right half itself -- observed exactly that way against the
    test install on 2026-09-19. Reading a missing minted id as "nothing was cut"
    would tell that author something plainly false about a cut they can see on
    the timeline; the canonical response heals the id by itself.
    """
    _run_gesture_node(_OPTIMISTIC_SETUP + """
        const w = splitWidget(clipScene());
        const planned = [{optimistic: [{type: 'clip', id: 'c1', rightId: 'mine1234'}]}];

        // Cut happened, under a name the client did not choose.
        const renamed = w._splitOutcomeShortfall({payload: {results: [
            {split_count: 1, right_items: [{type: 'clip', id: 'theirs99'}]}]}}, planned);
        assert.equal(renamed.length, 1, 'still a shortfall worth recording');
        assert.equal(renamed[0].kind, 'renamed');

        // Nothing cut at all is a different answer.
        const nothing = w._splitOutcomeShortfall({payload: {results: [
            {split_count: 0, right_items: []}]}}, planned);
        assert.equal(nothing[0].kind, 'no_cut');
    """)


def test_only_an_uncut_gesture_raises_the_no_effect_message():
    """The renamed case must stay silent end to end, not just in the classifier."""
    _run_gesture_node(_OPTIMISTIC_SETUP + """
        const w = pendingWidget(clipScene());
        const seen = captureNotes();
        const pending = w._splitItemsAtFrame(
            [{type: 'clip', id: 'c1', data: w.activeScene.clips[0]}], 40);
        await new Promise((r) => setTimeout(r, 0));
        // The server cut, but named the half itself.
        w.released[0].resolve({payload: {results: [
            {type: 'split_item', split_count: 1,
             right_items: [{type: 'clip', id: 'server01'}]}]}});
        await pending;
        assert.equal(
            seen.filter((v) => v.source === 'timeline-split-no-effect').length, 0,
            'a cut that happened must never be reported as no cut at all');
    """)


def test_a_reference_split_is_not_reported_as_having_cut_nothing():
    """The regression an adversarial audit of this landing found.

    `_apply_split_reference_item` returns `reference_item_id`,
    `right_reference_item_id`, `frame` and `bound_attachment_count` -- and no
    `split_count` at all. Reading that silence as a no-op made EVERY successful
    Reference cut raise a false warning, refetch the scene for nothing, and
    return before `_warnOnSplitReferenceChipBindings` -- which is the one thing
    the author has to act on, because an unresolvable chip binding is appended
    to compile ERRORS and every render over the right half is then refused.

    The response literal below is copied from that branch, not invented.
    """
    _run_gesture_node(_OPTIMISTIC_SETUP + """
        const scene = clipScene();
        scene.reference_items = [{reference_item_id: 'r1', lane_index: 0,
                                  start_frame: 0, end_frame: 100}];
        const w = pendingWidget(scene);
        w._findSceneItemBySelection = (type, id) => (type === 'reference'
            ? {type, id: 'r1', data: scene.reference_items[0]} : null);
        let warned = 0;
        w._warnOnSplitReferenceChipBindings = () => { warned += 1; };
        const seen = captureNotes();

        const pending = w._splitItemsAtFrame(
            [{type: 'reference', id: 'r1', data: scene.reference_items[0]}], 40);
        await new Promise((r) => setTimeout(r, 0));
        w.released[0].resolve({payload: {results: [{
            type: 'split_reference_item', reference_item_id: 'r1',
            right_reference_item_id: 'r2', frame: 40, bound_attachment_count: 2,
        }]}});
        await pending;

        assert.equal(seen.filter((v) => v.source === 'timeline-split-no-effect').length, 0,
            'a successful Reference cut must not report cutting nothing');
        assert.equal(warned, 1, 'and the chip-binding warning must still run');
        assert.equal(w.promptRefreshes, 1);
    """)


def test_a_linked_partner_that_does_not_span_the_cut_is_never_painted():
    """The other regression that audit found, and the more dangerous one.

    `_apply_split_linked` divides a member only when `start < split_frame < end`
    and files every other one on whichever side it lies. `_add_link_group`
    imposes no bounds constraint, so a clip linked to a longer music bed is an
    ordinary scene. Painting such a partner rewrote its end to a frame outside
    it -- an INVERTED row plus a phantom half with a negative source window,
    which the next gesture hit-tests and which any later `_pushUndo` snapshots
    into a history entry Undo writes back to disk.
    """
    _run_gesture_node(_OPTIMISTIC_SETUP + """
        const scene = clipScene();
        // The audio partner sits entirely to the right of the clip.
        scene.audio_tracks[0].timeline_start_frame = 200;
        scene.audio_tracks[0].timeline_end_frame = 300;
        scene.linked_item_groups = [{group_id: 'g1', items: [
            {type: 'clip', id: 'c1'}, {type: 'audio', id: 'a1'}]}];
        const w = pendingWidget(scene);

        const plan = w._planItemSplit({type: 'clip', id: 'c1', data: scene.clips[0]}, 40);
        assert.deepEqual(Object.keys(plan.operation.right_ids), ['clip:c1'],
            'no id is minted for a half the server will not build');
        assert.deepEqual(plan.optimistic.map((m) => m.id), ['c1']);

        const pending = w._splitItemsAtFrame(
            [{type: 'clip', id: 'c1', data: scene.clips[0]}], 40);
        await new Promise((r) => setTimeout(r, 0));
        assert.equal(scene.clips.length, 2, 'the clip that crosses is painted');
        assert.equal(scene.audio_tracks.length, 1,
            'the partner that does not is untouched');
        assert.deepEqual(
            [scene.audio_tracks[0].timeline_start_frame,
             scene.audio_tracks[0].timeline_end_frame], [200, 300]);
        // `apply_linked` still goes to the server, which owns the partition.
        assert.equal(plan.operation.apply_linked, true);

        w.released[0].resolve({payload: {results: [{type: 'split_linked_items',
            split_count: 1,
            right_items: [{type: 'clip', id: plan.operation.right_ids['clip:c1']}]}]}});
        await pending;
    """)


def test_a_closure_with_no_crossing_media_member_paints_nothing():
    """The degenerate case of the same rule: the anchor is a Reference item.

    A Reference item is not linkable, so its closure is itself and there is no
    clip or audio member to paint. The gesture must stay non-optimistic rather
    than mint an id for a row it will not draw -- an unmatched minted id would
    read as a shortfall against a response that is perfectly correct.
    """
    _run_gesture_node(_OPTIMISTIC_SETUP + """
        const scene = clipScene();
        scene.reference_items = [{reference_item_id: 'r1', lane_index: 0,
                                  start_frame: 0, end_frame: 100}];
        const w = splitWidget(scene);
        w._findSceneItemBySelection = (type, id) => (type === 'reference'
            ? {type, id: 'r1', data: scene.reference_items[0]} : null);
        const plan = w._planItemSplit(
            {type: 'reference', id: 'r1', data: scene.reference_items[0]}, 40);
        assert.equal(plan.optimistic, null);
        assert.equal(plan.operation.type, 'split_reference_item');
        assert.equal(plan.operation.right_ids, undefined,
            'only the two media operations carry minted ids');
    """)


def test_the_minted_id_generator_terminates_and_looks_like_a_server_id():
    """`Math.random()` may return exactly 0, whose hex slice is the empty string.

    An unguarded accumulate-until-eight loop would then never grow and would
    lock the tab. Vanishingly unlikely, and a hard lock when it happens.
    """
    _run_gesture_node(_OPTIMISTIC_SETUP + """
        const mod = await import('./web/js/scene_split_geometry.js');
        const saved = Math.random;
        try {
            Math.random = () => 0;
            const id = mod.mintSplitHalfId();
            assert.equal(id.length, 8, 'terminates on the degenerate stream');
            assert.match(id, /^[a-f0-9]{8}$/);
        } finally { Math.random = saved; }
        for (let i = 0; i < 200; i += 1) {
            assert.match(mod.mintSplitHalfId(), /^[a-f0-9]{8}$/);
        }
    """)


def test_a_cut_on_a_half_this_gesture_painted_still_asks_about_linkage():
    """The third regression the audit found, and the only one that loses data.

    A painted right half belongs to no group locally, because the partition is
    the server's. `_isLinkedItem` therefore says false for it, and a second cut
    on that half would be sent `apply_linked: false` -- dividing the video and
    leaving its linked audio partner whole, which then persists. Before this
    landing that second cut was refused `invalid_range` for aiming at stale
    geometry, so this would have been a silently wrong result replacing a loud
    refusal.

    The client asserts no membership; it declines to assert that there is none.
    """
    _run_gesture_node(_OPTIMISTIC_SETUP + """
        const scene = clipScene();
        scene.linked_item_groups = [{group_id: 'g1', items: [
            {type: 'clip', id: 'c1'}, {type: 'audio', id: 'a1'}]}];
        const w = pendingWidget(scene);

        const first = w._splitItemsAtFrame(
            [{type: 'clip', id: 'c1', data: scene.clips[0]}], 40);
        await new Promise((r) => setTimeout(r, 0));
        const half = scene.clips.find((c) => c.clip_id !== 'c1');
        assert.ok(half, 'painted');
        assert.equal(w._isLinkedItem({type: 'clip', id: half.clip_id, data: half}), false,
            'and it is genuinely in no group locally -- that is the trap');

        const second = w._splitItemsAtFrame(
            [{type: 'clip', id: half.clip_id, data: half}], 70);
        await new Promise((r) => setTimeout(r, 0));
        assert.equal(w.sent.length, 2);
        assert.equal(w.sent[1].operations[0].apply_linked, true,
            'the server is asked to use whatever group it holds');

        // Settling the first gesture releases its halves, so an UNRELATED row
        // afterwards must not inherit the claim -- the id is durable and the
        // entry would otherwise outlive the window it describes.
        w.released[0].resolve({payload: {results: [{type: 'split_linked_items',
            split_count: 2, right_items: []}]}});
        await first;
        assert.equal(w._optimisticSplitHalves.has(`clip:${half.clip_id}`), false,
            'released when its gesture settled');

        w.released[1].resolve({payload: {results: [{type: 'split_item',
            split_count: 1, right_items: []}]}});
        await second;
        assert.equal(w._optimisticSplitHalves.size, 0);
    """)


def test_a_failed_split_also_releases_the_halves_it_claimed():
    """The other half of the same lifecycle: `finally`, not the success path."""
    _run_gesture_node(_OPTIMISTIC_SETUP + """
        const w = pendingWidget(clipScene());
        w._fetchScenes = async () => true;
        w._discardUnstampableUndoEntry = () => {};
        const pending = w._splitItemsAtFrame(
            [{type: 'clip', id: 'c1', data: w.activeScene.clips[0]}], 40);
        await new Promise((r) => setTimeout(r, 0));
        assert.equal(w._optimisticSplitHalves.size, 1);
        w.released[0].reject(new Error('refused'));
        await pending;
        assert.equal(w._optimisticSplitHalves.size, 0,
            'a refused gesture must not leave a durable id claiming linkage');
    """)


# ── Class A: local applies for move and consolidate (stage 2 L6) ─────────────
#
# These three gestures changed rows that already existed and showed nothing
# until the write returned. Each now paints first. The order that matters is the
# same one the split landing needed: every guard is read BEFORE the apply, or it
# compares the client's own answer against itself.

_CLASS_A_SETUP = _SPLIT_SETUP + """
    function moveWidget(scene) {
        const w = splitWidget(scene);
        w._clearSelection = () => {};
        w._hideItemEditor = () => {};
        w._parsePositionInput = (v) => parseInt(v, 10);
        w.released = [];
        w._runSceneMutation = (operations, options) => {
            w.sent.push({operations, options});
            return new Promise((resolve, reject) => {
                w.released.push({resolve, reject});
            });
        };
        return w;
    }
"""


def test_moving_an_item_to_a_frame_paints_before_the_write():
    """Duration preserved, start clamped, and the operation is the source."""
    _run_gesture_node(_CLASS_A_SETUP + """
        const scene = clipScene();
        const w = moveWidget(scene);
        const clip = scene.clips[0];            // [0, 100]
        const pending = w._moveItemToFrame('clip', 'c1', clip, 250);
        await new Promise((r) => setTimeout(r, 0));

        assert.equal(w.sent.length, 1, 'the write is in flight');
        assert.deepEqual(
            [clip.timeline_start_frame, clip.timeline_end_frame], [250, 350],
            'painted, with the duration preserved by the server rule');
        assert.equal(w.sent[0].operations[0].fields.timeline_start_frame, 250);
        assert.equal(w.sent[0].operations[0].fields.timeline_end_frame, undefined,
            'no end is sent -- preserving the duration is the SERVER answer');
        w.released[0].resolve({payload: {results: []}});
        await pending;
    """)


def test_a_negative_move_target_clamps_the_way_the_server_does():
    """`max(0, int(x))`, not `Math.floor`, and not the raw input."""
    _run_gesture_node(_CLASS_A_SETUP + """
        const scene = clipScene();
        const w = moveWidget(scene);
        scene.clips[0].timeline_start_frame = 40;
        scene.clips[0].timeline_end_frame = 90;
        const pending = w._moveItemToFrame('clip', 'c1', scene.clips[0], -7);
        await new Promise((r) => setTimeout(r, 0));
        assert.deepEqual(
            [scene.clips[0].timeline_start_frame, scene.clips[0].timeline_end_frame],
            [0, 50], 'clamped to zero, duration kept');
        w.released[0].resolve({payload: {results: []}});
        await pending;
    """)


def test_a_linked_move_shifts_every_member_by_the_anchor_s_delta():
    """`_apply_linked_bounds_update`'s pure-move arm, including its clamping.

    The delta is computed ONCE from the anchor and then added to every member.
    Clamping each member independently would compress a group that straddles
    frame zero -- the members would pile up rather than move together.
    """
    _run_gesture_node(_CLASS_A_SETUP + """
        const scene = clipScene();
        scene.audio_tracks[0].timeline_start_frame = 10;
        scene.audio_tracks[0].timeline_end_frame = 60;
        scene.prompt_sections = [{prompt_id: 'p1', start_frame: 20, end_frame: 70}];
        scene.guide_frames = [{guide_id: 'g0', frame_index: 30}];
        scene.clips[0].source_in_frame = 12;
        scene.clips[0].source_out_frame = 112;
        scene.linked_item_groups = [{group_id: 'g1', items: [
            {type: 'clip', id: 'c1'}, {type: 'audio', id: 'a1'},
            {type: 'prompt', id: 'p1'}, {type: 'guide', id: 'g0'}]}];
        const w = moveWidget(scene);
        w._findSceneItemForLinkRef = (ref) => {
            if (ref.type === 'clip') return {type: 'clip', id: 'c1', data: scene.clips[0]};
            if (ref.type === 'audio') return {type: 'audio', id: 'a1', data: scene.audio_tracks[0]};
            if (ref.type === 'prompt') return {type: 'prompt', id: 0, data: scene.prompt_sections[0]};
            if (ref.type === 'guide') return {type: 'guide', id: 30, data: scene.guide_frames[0]};
            return null;
        };
        const pending = w._moveItemToFrame('clip', 'c1', scene.clips[0], 25);
        await new Promise((r) => setTimeout(r, 0));

        assert.equal(w.sent[0].operations[0].apply_linked, true);
        assert.deepEqual([scene.clips[0].timeline_start_frame,
                          scene.clips[0].timeline_end_frame], [25, 125]);
        assert.deepEqual([scene.audio_tracks[0].timeline_start_frame,
                          scene.audio_tracks[0].timeline_end_frame], [35, 85]);
        assert.deepEqual([scene.prompt_sections[0].start_frame,
                          scene.prompt_sections[0].end_frame], [45, 95]);
        // A guide is a single frame: the server writes `frame_index` alone and
        // derives its end, so a mirror must not invent an `end_frame` on it.
        assert.equal(scene.guide_frames[0].frame_index, 55);
        assert.equal('end_frame' in scene.guide_frames[0], false);
        // Source windows are untouched: this is a move, not a trim.
        // `_apply_ref_bounds` shifts them only when the move is NOT pure, so a
        // mirror that adjusted them here would re-trim every linked member of
        // an ordinary move.
        assert.equal(scene.clips[0].source_in_frame, 12);
        assert.equal(scene.clips[0].source_out_frame, 112);
        w.released[0].resolve({payload: {results: []}});
        await pending;
    """)


def test_a_refused_move_refetches_instead_of_leaving_the_item_where_it_was_drawn():
    _run_gesture_node(_CLASS_A_SETUP + """
        const scene = clipScene();
        const w = moveWidget(scene);
        const refetches = [];
        w._fetchScenes = async (options) => { refetches.push(options); return true; };
        const pending = w._moveItemToFrame('clip', 'c1', scene.clips[0], 250);
        await new Promise((r) => setTimeout(r, 0));
        assert.equal(scene.clips[0].timeline_start_frame, 250, 'painted');
        // A refusal this operation really can produce. It does NOT send
        // `validate_lane_collision` -- only the trim commit does -- so it can
        // never be refused for overlapping; a locked destination lane is the
        // refusal an author actually meets.
        w.released[0].reject(new Error('Lane is locked'));
        await pending;
        assert.equal(refetches.length, 1);
        assert.equal(refetches[0].reason, 'move_item_error');
        // And the server's own wording survives rather than the generic toast.
        assert.match(w.sent[0].options.failureMessage(new Error('Lane is locked')),
            /Lane is locked/);
    """)


def test_moving_an_item_to_a_new_lane_paints_the_lane_and_the_item():
    """`_applyLocalSetLaneCount` already mirrored the append; the lane field is
    the only thing that was missing, and the repaint used to run only on
    success."""
    _run_gesture_node(_CLASS_A_SETUP + """
        const scene = clipScene();
        scene.video_lane_count = 2;
        const w = moveWidget(scene);
        w._clipTrackType = () => 'video';
        const pending = w._moveItemToNewLane({type: 'clip', id: 'c1', data: scene.clips[0]});
        await new Promise((r) => setTimeout(r, 0));

        assert.equal(scene.video_lane_count, 3, 'the lane exists immediately');
        assert.equal(scene.clips[0].track_index, 2, 'and the clip is on it');
        assert.deepEqual(w.sent[0].operations.map((op) => op.type),
            ['set_lane_count', 'update_clip']);
        assert.equal(w.sent[0].operations[0].count, 3);
        assert.equal(w.sent[0].operations[1].fields.track_index, 2);
        w.released[0].resolve({payload: {results: []}});
        await pending;
    """)


def test_a_driver_clip_moves_to_a_new_driver_lane_not_a_video_one():
    """`variableLaneTypeFor(this._clipTrackType(...))`, not a hardcoded family.

    Appending to `video` would create the lane in the wrong family and move the
    Driver onto a lane that cannot hold it.
    """
    _run_gesture_node(_CLASS_A_SETUP + """
        const scene = clipScene();
        scene.clips[0].role = 'motion_driver';
        scene.motion_driver_lane_count = 1;
        const w = moveWidget(scene);
        // The shared fixture pins `_clipTrackType` to 'video' for the ordinary
        // cases. That is the very thing under test here, so drop the own
        // property and let the prototype's real one read the clip's role.
        delete w._clipTrackType;
        const pending = w._moveItemToNewLane({type: 'clip', id: 'c1', data: scene.clips[0]});
        await new Promise((r) => setTimeout(r, 0));
        assert.equal(w.sent[0].operations[0].lane_type, 'motion_driver');
        assert.equal(scene.motion_driver_lane_count, 2);
        assert.equal(scene.clips[0].track_index, 1);
        w.released[0].resolve({payload: {results: []}});
        await pending;
    """)


def test_consolidating_moves_the_items_and_compacts_the_lanes_they_empty():
    """The ORDER is the difficulty, and it is why this one is a named helper.

    Move every item first, then remove the vacated lanes in DESCENDING order.
    The destination then lands on the server's `final_target_lane` by itself,
    because the moved items shift down with everything else above a removed
    lane. Computing that index directly would be a second answer.
    """
    _run_gesture_node(_CLASS_A_SETUP + """
        const scene = clipScene();
        scene.video_lane_count = 4;
        scene.clips = [
            {clip_id: 'a', timeline_start_frame: 0, timeline_end_frame: 10, track_index: 0},
            {clip_id: 'b', timeline_start_frame: 20, timeline_end_frame: 30, track_index: 1},
            {clip_id: 'c', timeline_start_frame: 40, timeline_end_frame: 50, track_index: 3},
        ];
        const w = moveWidget(scene);
        // Target lane 3; lanes 0 and 1 empty when their only items move.
        const painted = w._applyLocalConsolidateItems('video', ['a', 'b'], 3);
        assert.equal(painted, true);
        assert.equal(scene.video_lane_count, 2, 'two vacated lanes removed');
        // 3 - 2 removed below = 1, which is what `final_target_lane` reports.
        assert.deepEqual(scene.clips.map((v) => [v.clip_id, v.track_index]),
            [['a', 1], ['b', 1], ['c', 1]]);
    """)


def test_consolidating_leaves_a_source_lane_that_did_not_empty():
    """`_compactEmptyMediaLaneLocal` re-checks emptiness, as the server does."""
    _run_gesture_node(_CLASS_A_SETUP + """
        const scene = clipScene();
        scene.video_lane_count = 3;
        scene.clips = [
            {clip_id: 'a', timeline_start_frame: 0, timeline_end_frame: 10, track_index: 0},
            {clip_id: 'stay', timeline_start_frame: 60, timeline_end_frame: 70, track_index: 0},
            {clip_id: 'c', timeline_start_frame: 40, timeline_end_frame: 50, track_index: 2},
        ];
        const w = moveWidget(scene);
        w._applyLocalConsolidateItems('video', ['a'], 2);
        assert.equal(scene.video_lane_count, 3, 'lane 0 still holds an item');
        assert.deepEqual(scene.clips.map((v) => [v.clip_id, v.track_index]),
            [['a', 2], ['stay', 0], ['c', 2]]);
    """)


def test_a_linked_move_that_would_carry_a_member_negative_paints_nothing():
    """The server refuses the whole move, so the honest paint is none.

    The client cannot clamp its way out: the delta is the anchor's, and clamping
    each member independently piles the group up at frame zero instead of moving
    it together. `_apply_ref_bounds` raises `invalid_range` on the first member
    that would land below zero, so painting would draw a row the project will
    never hold -- and a later `_pushUndo` snapshots whatever is on screen.

    Found by injecting the clamp into the mirror and watching the parity test
    pass, which is what sent me looking for the case it could not reach.
    """
    _run_gesture_node(_CLASS_A_SETUP + """
        const scene = clipScene();
        scene.clips[0].timeline_start_frame = 100;
        scene.clips[0].timeline_end_frame = 200;
        // The partner starts BEFORE the anchor, so a delta that puts the anchor
        // at zero puts this one below it.
        scene.audio_tracks[0].timeline_start_frame = 50;
        scene.audio_tracks[0].timeline_end_frame = 150;
        scene.linked_item_groups = [{group_id: 'g1', items: [
            {type: 'clip', id: 'c1'}, {type: 'audio', id: 'a1'}]}];
        const w = moveWidget(scene);

        const pending = w._moveItemToFrame('clip', 'c1', scene.clips[0], 0);
        await new Promise((r) => setTimeout(r, 0));

        assert.equal(w.sent.length, 1, 'the move is still SENT -- the server decides');
        assert.equal(w.sent[0].operations[0].apply_linked, true);
        // Nothing painted, including the anchor: a half-applied group on screen
        // is worse than an unapplied one, and the server applies none of it.
        assert.deepEqual([scene.clips[0].timeline_start_frame,
                          scene.clips[0].timeline_end_frame], [100, 200]);
        assert.deepEqual([scene.audio_tracks[0].timeline_start_frame,
                          scene.audio_tracks[0].timeline_end_frame], [50, 150]);

        w.released[0].reject(new Error('Clip range is invalid'));
        await pending;
    """)


def test_a_linked_move_that_clears_frame_zero_still_paints():
    """The boundary of the rule above: exactly at zero is allowed."""
    _run_gesture_node(_CLASS_A_SETUP + """
        const scene = clipScene();
        scene.clips[0].timeline_start_frame = 100;
        scene.clips[0].timeline_end_frame = 200;
        scene.audio_tracks[0].timeline_start_frame = 100;
        scene.audio_tracks[0].timeline_end_frame = 150;
        scene.linked_item_groups = [{group_id: 'g1', items: [
            {type: 'clip', id: 'c1'}, {type: 'audio', id: 'a1'}]}];
        const w = moveWidget(scene);
        const pending = w._moveItemToFrame('clip', 'c1', scene.clips[0], 0);
        await new Promise((r) => setTimeout(r, 0));
        assert.deepEqual([scene.clips[0].timeline_start_frame,
                          scene.clips[0].timeline_end_frame], [0, 100]);
        assert.deepEqual([scene.audio_tracks[0].timeline_start_frame,
                          scene.audio_tracks[0].timeline_end_frame], [0, 50]);
        w.released[0].resolve({payload: {results: []}});
        await pending;
    """)


def test_a_refused_lane_move_repaints_and_refetches():
    """The repaint used to sit inside the `try`, AFTER the await.

    So a failure left the timeline unrepainted and refetched nothing at all --
    tolerable when nothing had been drawn, and not tolerable now, because the
    optimistic apply has already added a lane and moved the item onto it. The
    batch is also non-retryable by construction (`fields.track_index` is a lane
    destination), so this path is REACHED rather than replayed whenever a render
    or another editor moves the version underneath it.
    """
    _run_gesture_node(_CLASS_A_SETUP + """
        const scene = clipScene();
        scene.video_lane_count = 2;
        const w = moveWidget(scene);
        w._clipTrackType = () => 'video';
        const refetches = [];
        w._fetchScenes = async (options) => { refetches.push(options); return true; };
        let repaints = 0;
        w._renderSceneAfterLocalMutation = () => { repaints += 1; };

        const pending = w._moveItemToNewLane({type: 'clip', id: 'c1', data: scene.clips[0]});
        await new Promise((r) => setTimeout(r, 0));
        assert.equal(scene.video_lane_count, 3, 'painted');
        const paintedAt = repaints;

        w.released[0].reject(new Error('Lane is locked'));
        await pending;
        assert.equal(refetches.length, 1, 'the optimistic lane is discarded');
        assert.equal(refetches[0].ignoreMutationGate, true);
        assert.equal(refetches[0].reason, 'move_new_lane_error');
        assert.ok(repaints > paintedAt, 'and the timeline is repainted on failure');
    """)


def test_a_refused_consolidation_refetches_what_it_painted():
    """Consolidation moves items between lanes and can REMOVE one.

    Leaving that on screen after a refusal is not a cosmetic delay: the lane is
    gone from the client's scene and every item above it has been reindexed.
    """
    _run_gesture_node(_CLASS_A_SETUP + """
        const scene = clipScene();
        scene.video_lane_count = 3;
        scene.clips = [
            {clip_id: 'a', timeline_start_frame: 0, timeline_end_frame: 10, track_index: 0},
            {clip_id: 'b', timeline_start_frame: 40, timeline_end_frame: 50, track_index: 2},
        ];
        const w = moveWidget(scene);
        const refetches = [];
        w._fetchScenes = async (options) => { refetches.push(options); return true; };
        w._selectedConsolidationItems = () => ([
            {type: 'clip', id: 'a', data: scene.clips[0]},
            {type: 'clip', id: 'b', data: scene.clips[1]},
        ]);
        w._consolidationRefusal = () => '';
        w._mediaItemsOverlap = () => false;
        w._laneItemsForTrackType = () => [];

        const pending = w._consolidateSelectedItemsToLane(
            {type: 'clip', id: 'b', data: scene.clips[1]});
        await new Promise((r) => setTimeout(r, 0));
        assert.equal(scene.video_lane_count, 2, 'the vacated lane is gone already');
        assert.deepEqual(scene.clips.map((c) => c.track_index), [1, 1]);

        w.released[0].reject(new Error('Selected items overlap'));
        await pending;
        assert.equal(refetches.length, 1);
        assert.equal(refetches[0].reason, 'consolidate_error');
        assert.equal(refetches[0].ignoreMutationGate, true);
    """)


def test_consolidation_paints_nothing_when_an_id_is_not_on_the_timeline():
    """`_consolidate_media_items` 404s the WHOLE operation on the first id it
    cannot resolve, so a partial paint would draw an arrangement the server can
    never produce. Unreachable through the live selection path, which is why it
    is pinned rather than left to a comment."""
    _run_gesture_node(_CLASS_A_SETUP + """
        const scene = clipScene();
        scene.video_lane_count = 3;
        scene.clips = [
            {clip_id: 'a', timeline_start_frame: 0, timeline_end_frame: 10, track_index: 0},
        ];
        const w = moveWidget(scene);
        assert.equal(w._applyLocalConsolidateItems('video', ['a', 'ghost'], 2), false);
        assert.equal(scene.clips[0].track_index, 0, 'nothing moved');
        assert.equal(scene.video_lane_count, 3, 'and no lane was compacted');
    """)


def test_a_move_on_an_optimistic_split_half_still_asks_about_linkage():
    """The defect an adversarial audit of this landing found.

    A half painted by an in-flight split is in no group locally, so
    `_isLinkedItem` says false. Sending `apply_linked: false` on that answer is
    not a declined nicety: the server holds the regrouped halves, moves only the
    row named, leaves its partner behind, and returns **200** -- so nothing
    refetches and the client and the disk agree on the wrong state. Every
    `apply_linked` emitter now goes through `_shouldApplyLinked`.
    """
    _run_gesture_node(_CLASS_A_SETUP + """
        const scene = clipScene();
        scene.linked_item_groups = [{group_id: 'g1', items: [
            {type: 'clip', id: 'c1'}, {type: 'audio', id: 'a1'}]}];
        const w = moveWidget(scene);

        const split = w._splitItemsAtFrame(
            [{type: 'clip', id: 'c1', data: scene.clips[0]}], 50);
        await new Promise((r) => setTimeout(r, 0));
        const half = scene.clips.find((c) => c.clip_id !== 'c1');
        assert.equal(w._isLinkedItem({type: 'clip', id: half.clip_id, data: half}), false,
            'genuinely ungrouped locally -- that is the trap');
        assert.equal(w._shouldApplyLinked({type: 'clip', id: half.clip_id, data: half}), true);

        const move = w._moveItemToFrame('clip', half.clip_id, half, 400);
        await new Promise((r) => setTimeout(r, 0));
        assert.equal(w.sent[1].operations[0].apply_linked, true,
            'the server is asked to move whatever group it holds');

        for (const slot of w.released) {
            slot.resolve({payload: {results: [{type: 'split_linked_items',
                split_count: 2, right_items: []}]}});
        }
        await Promise.allSettled([split, move]);
    """)


_LINK_SETUP = """
    function linkWidget() {
        const w = makeWidget();
        w.activeScene = {scene_id: 'scene', clips: [{clip_id: 'c1'}, {clip_id: 'c2'}],
            audio_tracks: [{track_id: 'a1'}], guide_frames: [], prompt_sections: [], linked_item_groups: []};
        w.selectedItems = w.activeScene.clips.map(data => ({type: 'clip', id: data.clip_id, data}));
        w._reconcileSelection = () => {};
        w.paints = [];
        w._renderSceneAfterLocalMutation = () => w.paints.push(structuredClone(w.activeScene.linked_item_groups));
        w.undos = [];
        w._pushUndo = label => {
            const entry = {label, snapshot: structuredClone(w.activeScene)};
            w.undos.push(entry); return entry;
        };
        w.sent = []; w.releases = [];
        w._runSceneMutation = (operations, options) => {
            w.sent.push({operations, options});
            return new Promise((resolve, reject) => w.releases.push({resolve, reject}));
        };
        return w;
    }
"""


def test_link_paints_durable_id_before_save_and_undo_captures_before():
    _run_gesture_node(_LINK_SETUP + """
        const w = linkWidget();
        const pending = w._createLinkGroupFromSelectionWithinGesture();
        assert.equal(w.paints.length, 1);
        const group = w.activeScene.linked_item_groups[0];
        assert.equal(group.items.length, 2);
        assert.equal(group.group_id, w.sent[0].operations[0].group_id);
        assert.deepEqual(w.undos[0].snapshot.linked_item_groups, []);
        assert.equal(w.sent[0].options.historyEntry, w.undos[0]);
        w.releases[0].resolve({}); await pending;
    """)


def test_link_then_unlink_before_either_response_has_immediate_badges():
    _run_gesture_node(_LINK_SETUP + """
        const w = linkWidget();
        const link = w._createLinkGroupFromSelectionWithinGesture();
        const group = structuredClone(w.activeScene.linked_item_groups);
        w.selectedItems = [w.selectedItems[0]];
        const unlink = w._unlinkSelectedItemsWithinGesture();
        assert.deepEqual(w.activeScene.linked_item_groups, []);
        assert.deepEqual(w.undos[1].snapshot.linked_item_groups, group);
        assert.equal(w.sent[1].operations[0].entire_group, true);
        w.releases[0].resolve({}); w.releases[1].resolve({});
        await Promise.all([link, unlink]);
    """)


def test_link_uses_durable_prompt_and_guide_ids_with_prepaint_guards():
    _run_gesture_node(_LINK_SETUP + """
        const w = linkWidget();
        const prompt = {prompt_id: 'prompt-later', start_frame: 20, end_frame: 40};
        const guide = {guide_id: 'guide-later', frame_index: 30, asset_id: 'asset'};
        w.activeScene.prompt_sections = [prompt]; w.activeScene.guide_frames = [guide];
        w.selectedItems = [{type: 'prompt', id: 0, data: prompt}, {type: 'guide', id: 30, data: guide}];
        const pending = w._createLinkGroupFromSelectionWithinGesture();
        const op = w.sent[0].operations[0];
        assert.deepEqual(op.items.map(i => i.id), ['prompt-later', 'guide-later']);
        assert.equal(op.items[0].expected.start_frame, 20);
        assert.equal(op.items[1].expected.frame_index, 30);
        assert.deepEqual(w.activeScene.linked_item_groups[0].items,
            [{type: 'prompt', id: 'prompt-later'}, {type: 'guide', id: 'guide-later'}]);
        w.releases[0].resolve({}); await pending;
    """)


def test_failed_link_restores_groups_without_a_network_read():
    _run_gesture_node(_LINK_SETUP + """
        const w = linkWidget();
        const previous = w.activeScene.linked_item_groups;
        const pending = w._createLinkGroupFromSelectionWithinGesture();
        w.releases[0].reject(new Error('offline')); await pending;
        assert.equal(w.activeScene.linked_item_groups, previous);
        assert.equal(w.paints.length, 2);
    """)


def test_failed_link_cannot_overwrite_a_newer_unlink_or_scene():
    _run_gesture_node(_LINK_SETUP + """
        for (const switchScene of [false, true]) {
            const w = linkWidget();
            const link = w._createLinkGroupFromSelectionWithinGesture();
            const unlink = w._unlinkSelectedItemsWithinGesture();
            if (switchScene) w.activeScene = {scene_id: 'other', linked_item_groups: [{group_id: 'other'}]};
            const current = w.activeScene.linked_item_groups;
            w.releases[0].reject(new Error('offline')); await link;
            assert.equal(w.activeScene.linked_item_groups, current);
            w.releases[1].resolve({}); await unlink;
        }
    """)


def test_failed_unlink_restores_the_whole_group():
    _run_gesture_node(_LINK_SETUP + """
        const w = linkWidget();
        w.activeScene.linked_item_groups = [{group_id: 'old', items:
            [{type: 'clip', id: 'c1'}, {type: 'clip', id: 'c2'}, {type: 'audio', id: 'a1'}]}];
        const previous = w.activeScene.linked_item_groups;
        w.selectedItems = [w.selectedItems[0]];
        const pending = w._unlinkSelectedItemsWithinGesture();
        assert.deepEqual(w.activeScene.linked_item_groups, []);
        w.releases[0].reject(new Error('offline')); await pending;
        assert.equal(w.activeScene.linked_item_groups, previous);
    """)


def test_unlink_on_unreconciled_split_half_reaches_the_server():
    _run_gesture_node(_LINK_SETUP + """
        const w = linkWidget();
        w.selectedItems = [w.selectedItems[0]];
        w._optimisticSplitHalves = new Set(['clip:c1']);
        const pending = w._unlinkSelectedItemsWithinGesture();
        assert.equal(w.sent.length, 1);
        assert.equal(w.sent[0].operations[0].entire_group, true);
        w.releases[0].resolve({}); await pending;
    """)


@pytest.mark.parametrize("first_link", [True, False])
def test_failed_group_edit_cannot_enter_the_next_edits_undo_target(first_link):
    """Real queue/history: canonical pre-state replaces abandoned predictions.

    Only transport and rendering are replaced. Both failure orientations matter:
    Undo must neither resurrect a refused link nor delete a group whose unlink
    failed. The original operation guards must remain authored values.
    """
    _run_gesture_node(_LINK_SETUP + f"const firstLink = {str(first_link).lower()};" + """
        const w = linkWidget();
        delete w._pushUndo; delete w._stampHistoryPostSnapshot; delete w._runSceneMutation;
        w._undoStack = []; w._redoStack = []; w._maxUndoSteps = 100; w._historyStackRevision = 0;
        w._replayDeferredHistoryWidgetStateIfIdle = () => {};
        w._snapshotProjectMutationContext = () => ({projectId: 'project', sceneId: 'scene'});
        if (!firstLink) w.activeScene.linked_item_groups = [{group_id:'old', items:
            [{type:'clip', id:'c1'}, {type:'clip', id:'c2'}]}];
        const server = structuredClone(w.activeScene);
        const original = structuredClone(server.linked_item_groups);
        let reads = 0;
        globalThis.fetch = async () => {
            reads++; return new Response(JSON.stringify(server), {status:200});
        };
        let rejectFirst, calls = 0;
        w._runVersionedProjectMutation = async (_url, init) => {
            calls++;
            if (calls === 1) await new Promise((_resolve, reject) => { rejectFirst = reject; });
            const op = JSON.parse(init.body).operations[0];
            server.linked_item_groups = op.type === 'unlink_items' ? []
                : [{group_id:op.group_id, items:op.items.map(({type,id}) => ({type,id}))}];
            return {payload:{scene:structuredClone(server)}};
        };
        const first = firstLink ? w._createLinkGroupFromSelectionWithinGesture()
            : w._unlinkSelectedItemsWithinGesture();
        while (!rejectFirst) await new Promise(r => setTimeout(r, 1));
        const second = firstLink ? w._unlinkSelectedItemsWithinGesture()
            : w._createLinkGroupFromSelectionWithinGesture();
        rejectFirst(new Error('refused'));
        await Promise.all([first, second]);
        assert.equal(calls, 2);
        assert.equal(reads, 2, 'unknown failure outcome resolves before the next slot');
        assert.equal(w._undoStack.length, 1);
        assert.deepEqual(w._undoStack[0].snapshot.linked_item_groups, original);
        assert.deepEqual(w._undoStack[0].postSnapshot.linked_item_groups, server.linked_item_groups);
    """)


def test_two_failed_group_predictions_return_to_the_pre_burst_groups():
    _run_gesture_node(_LINK_SETUP + """
        const w = linkWidget();
        const before = w.activeScene.linked_item_groups;
        const first = w._createLinkGroupFromSelectionWithinGesture();
        const second = w._unlinkSelectedItemsWithinGesture();
        w.releases[0].reject(new Error('offline')); await first;
        w.releases[1].reject(new Error('offline')); await second;
        assert.equal(w.activeScene.linked_item_groups, before);
    """)


def test_link_ordering_does_not_rewrite_authored_guards():
    _run_gesture_node(_LINK_SETUP + """
        const w = linkWidget();
        delete w._pushUndo; delete w._stampHistoryPostSnapshot; delete w._runSceneMutation;
        w._undoStack=[]; w._redoStack=[]; w._maxUndoSteps=100; w._historyStackRevision=0;
        w._replayDeferredHistoryWidgetStateIfIdle=()=>{};
        w._snapshotProjectMutationContext=()=>({projectId:'project',sceneId:'scene'});
        const guide={guide_id:'g', frame_index:10, asset_id:'asset'};
        w.activeScene.guide_frames=[guide];
        w.selectedItems=[w.selectedItems[0],{type:'guide', id:10,data:guide}];
        const server=structuredClone(w.activeScene); server.guide_frames[0].frame_index=99;
        globalThis.fetch=async()=>new Response(JSON.stringify(server),{status:200});
        w._runVersionedProjectMutation=async(_url,init)=>{
            const op=JSON.parse(init.body).operations[0];
            assert.equal(op.items[1].id,'g');
            assert.equal(op.items[1].expected.frame_index,10,'must refuse drift, not authorize it');
            return {payload:{scene:server}};
        };
        await w._createLinkGroupFromSelectionWithinGesture();
        assert.equal(w._undoStack[0].snapshot.guide_frames[0].frame_index,99);
    """)



def test_unlink_history_reads_the_servers_unknown_split_partition():
    _run_gesture_node(_LINK_SETUP + """
        const w=linkWidget();
        delete w._pushUndo; delete w._stampHistoryPostSnapshot; delete w._runSceneMutation;
        w._undoStack=[]; w._redoStack=[]; w._maxUndoSteps=100; w._historyStackRevision=0;
        w._replayDeferredHistoryWidgetStateIfIdle=()=>{};
        w._snapshotProjectMutationContext=()=>({projectId:'project',sceneId:'scene'});
        w._optimisticSplitHalves=new Set(['clip:c1']);
        w.selectedItems=[w.selectedItems[0]];
        const canonical=structuredClone(w.activeScene);
        canonical.linked_item_groups=[{group_id:'server-right',items:
            [{type:'clip',id:'c1'},{type:'clip',id:'c2'}]}];
        // A context for another scene must not suppress this scene's first read.
        w._latestHistoryOrderContext={rebaseIntents:false,scenes:new Map([['other',{scene_id:'other'}]])};
        let reads=0;
        globalThis.fetch=async()=>{reads++; return new Response(JSON.stringify(canonical),{status:200});};
        w._runVersionedProjectMutation=async()=>({payload:{scene:{...canonical,linked_item_groups:[]}}});
        await w._unlinkSelectedItemsWithinGesture();
        assert.equal(reads,1);
        assert.deepEqual(w._undoStack[0].snapshot.linked_item_groups,canonical.linked_item_groups);
        assert.deepEqual(w._undoStack[0].postSnapshot.linked_item_groups,[]);
    """)
# -- Class C: Reference staging (stage 2 L7) ---------------------------------
#
# Phase C section 3 specified a GEOMETRY-ONLY apply here, to avoid a 409 it
# believed painting `item.members` would create. Probed against the route, the
# opposite holds: the canonical record for a Library drop is byte-identical to
# what `dragPayload` sends, and NOT painting is what loses a drop, because
# `_appendReferenceMembersWithinGesture` reads `priorMembers` before its await.
# `tests/test_reference_geometry_parity.py` holds the route half of that; these
# hold the gesture half.

_CLASS_C_SETUP = _SPLIT_SETUP + """
    function referenceWidget(scene) {
        const w = splitWidget(scene);
        w.projectDir = 'project';
        w.activeSceneId = 'scene';
        w._references = [{reference_id: 'entity-1', name: 'Subject', members: [
            {member_id: 'member-a', asset_id: 'asset-a'},
            {member_id: 'member-b', asset_id: 'asset-b'},
            {member_id: 'member-c', asset_id: 'asset-c'}]}];
        w._findAssetById = (id) => ({asset_id: id, asset_type: 'image', has_audio: false});
        w._referenceLaneRecipe = () => ({media_kind: 'image', recipe: {}});
        w._defaultReferenceLaneRecipe = (o = {}) => ({media_kind: 'image', recipe: {}, ...o});
        w._timelineRulerHeight = () => 0;
        w._layoutIndexFromRawY = () => -1;
        w._trackLayout = [{type: 'reference', laneIndex: 0, collapsed: false}];
        w.paints = 0;
        w._renderSceneAfterLocalMutation = () => { w.paints += 1; };
        w._reconcileActiveSceneFromMutation = () => {};
        w._fetchScenes = async () => {};
        w.released = [];
        w._runSceneMutation = (operations, options) => {
            w.sent.push({operations, options});
            return new Promise((resolve, reject) => {
                w.released.push({resolve, reject});
            });
        };
        return w;
    }
    const referenceScene = () => ({
        scene_id: 'scene',
        duration_frames: 1000,
        clips: [], audio_tracks: [], prompt_sections: [],
        reference_lane_count: 1,
        reference_lane_configs: [{}],
        reference_lane_recipes: [{media_kind: 'image', recipe: {}, lane_id: 'lane-a'}],
        reference_items: [],
    });
    const drag = (id) => ({entity_id: 'entity-1', member_id: id});
"""


def test_staging_a_reference_paints_the_bar_before_the_write():
    """The bar, its members and the id the project will store, all at once."""
    _run_gesture_node(_CLASS_C_SETUP + """
        const scene = referenceScene();
        const w = referenceWidget(scene);
        const pending = w._placeReferencePayload({members: [drag('member-a')]}, 40);
        await new Promise((r) => setTimeout(r, 0));

        assert.equal(w.sent.length, 1, 'the write is in flight');
        assert.equal(scene.reference_items.length, 1, 'and the bar is already drawn');
        const painted = scene.reference_items[0];
        assert.equal(painted.start_frame, 40);
        assert.equal(painted.end_frame, -1, 'the run-to-scene-end sentinel is kept');
        assert.deepEqual(painted.members, [drag('member-a')],
            'the canonical record, not a client-only shape');
        const create = w.sent[0].operations.find((op) => op.type === 'create_reference_item');
        assert.equal(create.fields.reference_item_id, painted.reference_item_id,
            'the painted bar answers to the name the operation asks for');
        assert.ok(w.paints > 0);
        w.released[0].resolve({payload: {results: []}});
        await pending;
    """)


def test_a_second_drop_in_the_same_window_appends_to_the_painted_bar():
    """The whole point of painting: the drop resolver now sees real geometry.

    Without the paint the bar is not in `activeScene` yet, so the second drop
    resolves `create` at an occupied frame and the route refuses it for
    overlapping. With it, the drop resolves `append` against the bar that is
    genuinely there.
    """
    _run_gesture_node(_CLASS_C_SETUP + """
        const scene = referenceScene();
        const w = referenceWidget(scene);
        const first = w._placeReferencePayload({members: [drag('member-a')]}, 40);
        await new Promise((r) => setTimeout(r, 0));
        const staged = scene.reference_items[0];

        // The pointer is over the bar this gesture just painted.
        w._layoutIndexFromRawY = () => 0;
        const second = w._placeReferencePayload({members: [drag('member-b')]}, 50, 10);
        await new Promise((r) => setTimeout(r, 0));

        assert.equal(w.sent.length, 2, 'a second write, not a refusal');
        const update = w.sent[1].operations.find((op) => op.type === 'update_reference_item');
        assert.ok(update, 'the second drop became an append');
        assert.equal(update.reference_item_id, staged.reference_item_id);
        assert.deepEqual(update.expected.members, [drag('member-a')],
            'guarded on what the author saw, which the paint made true');
        assert.deepEqual(update.fields.members, [drag('member-a'), drag('member-b')]);
        assert.deepEqual(staged.members, [drag('member-a'), drag('member-b')],
            'and the bar shows both before either write returns');
        w.released[0].resolve({payload: {results: []}});
        w.released[1].resolve({payload: {results: []}});
        await Promise.all([first, second]);
    """)


def test_an_append_reads_its_guard_before_the_paint():
    """`expected` states what the author saw, not what this gesture just drew."""
    _run_gesture_node(_CLASS_C_SETUP + """
        const scene = referenceScene();
        scene.reference_items = [{reference_item_id: 'ref-1', lane_index: 0,
            start_frame: 40, end_frame: -1, members: [drag('member-a')],
            prompt_override: '', strength: 1.0, sequence_frames: 0, muted: false}];
        const w = referenceWidget(scene);
        const pending = w._appendReferenceMembers('ref-1', {members: [drag('member-b')]});
        await new Promise((r) => setTimeout(r, 0));

        const update = w.sent[0].operations[0];
        assert.deepEqual(update.expected.members, [drag('member-a')],
            'the guard is the PRE-paint list');
        assert.deepEqual(scene.reference_items[0].members,
            [drag('member-a'), drag('member-b')], 'and the paint already happened');
        assert.equal(w.undos.length, 1, 'one undo entry, pushed before the paint');
        w.released[0].resolve({payload: {results: []}});
        await pending;
    """)


def test_an_unpaintable_member_sends_the_payload_and_paints_nothing():
    """A member the project cannot resolve is the route's refusal to make.

    Painting the resolvable part of a batch the server will reject wholesale
    would put members on the bar that the save is about to remove, and
    `_pushUndo` can carry that to disk in between.
    """
    _run_gesture_node(_CLASS_C_SETUP + """
        const scene = referenceScene();
        scene.reference_items = [{reference_item_id: 'ref-1', lane_index: 0,
            start_frame: 40, end_frame: -1, members: [drag('member-a')],
            prompt_override: '', strength: 1.0, sequence_frames: 0, muted: false}];
        const w = referenceWidget(scene);
        const pending = w._appendReferenceMembers(
            'ref-1', {members: [{entity_id: 'entity-1', member_id: 'member-gone'}]});
        await new Promise((r) => setTimeout(r, 0));

        assert.equal(w.sent.length, 1, 'the write still goes, so the route speaks');
        assert.deepEqual(scene.reference_items[0].members, [drag('member-a')],
            'but nothing was painted');
        w.released[0].reject(new Error('Reference member not found: member-gone'));
        await pending;
    """)
def test_the_painted_bar_carries_the_canonical_record_not_the_payload():
    """A stale `entity_id` is the case where the two can be told apart.

    The first staging test cannot distinguish them: a fresh Library drag already
    sends the canonical shape, so pushing the raw payload passes it. `entity_id`
    is re-derived on the route from the member lookup, so a payload naming a
    reference the member does not belong to is silently corrected there -- and a
    paint that trusted it would differ from the stored row, which is how a
    divergence reaches disk through `_pushUndo`.
    """
    _run_gesture_node(_CLASS_C_SETUP + """
        const scene = referenceScene();
        const w = referenceWidget(scene);
        const pending = w._placeReferencePayload(
            {members: [{entity_id: 'entity-stale', member_id: 'member-a'}]}, 40);
        await new Promise((r) => setTimeout(r, 0));

        assert.deepEqual(scene.reference_items[0].members, [drag('member-a')],
            'entity_id re-derived from the reference that owns the member');
        w.released[0].resolve({payload: {results: []}});
        await pending;
    """)


def test_an_unpaintable_row_leaves_no_lane_or_recipe_painted_behind_it():
    """The batch is all-or-nothing, so the paint is too.

    `create_reference_item` travels with its `set_lane_count` and
    `update_lane_config`. Painting the lane and then finding the row unpaintable
    would leave an empty Reference lane the project never accepted -- visible,
    selectable, and gone again the moment anything refetches.

    The case used here is the only one that reaches the planner at all: one
    member named twice in a single payload. `resolveReferenceDropVerdict`
    compares a drag against the members of the bar it would append to, not
    against itself, so a self-duplicating payload passes it and the route
    answers 400. Every OTHER way the planner can decline -- an unresolvable
    member, a missing asset, an inverted range -- is already refused before the
    gesture builds an operation, so the guard is defence rather than a live
    path, and that is worth knowing when reading it.
    """
    _run_gesture_node(_CLASS_C_SETUP + """
        const scene = referenceScene();
        const w = referenceWidget(scene);
        const lanesBefore = scene.reference_lane_count;
        // The ruler zone, so the gesture takes the NEW-LANE branch: this is the
        // arm where a lane would be painted ahead of the row.
        w._timelineRulerHeight = () => 40;
        const pending = w._placeReferencePayload(
            {members: [drag('member-a'), drag('member-a')]}, 40, 10);
        await new Promise((r) => setTimeout(r, 0));

        assert.equal(w.sent.length, 1, 'the write still goes, so the route speaks');
        assert.ok(w.sent[0].operations.some((op) => op.type === 'set_lane_count'),
            'and it really is the new-lane branch');
        assert.equal(scene.reference_items.length, 0, 'no bar');
        assert.equal(scene.reference_lane_count, lanesBefore, 'and no lane');
        assert.equal(w.paints, 0, 'nothing was repainted');
        w.released[0].reject(new Error('Reference item members must be unique'));
        await pending;
    """)


def test_a_refused_stage_repaints_after_discarding_the_optimistic_bar():
    """The refetch discards the paint; the repaint is what makes it visible.

    Both Reference catches used to refetch and draw nothing, which was harmless
    while there was no optimistic state to discard.
    """
    _run_gesture_node(_CLASS_C_SETUP + """
        const scene = referenceScene();
        const w = referenceWidget(scene);
        let refetched = 0;
        w._fetchScenes = async () => {
            refetched += 1;
            scene.reference_items = [];      // what the server actually holds
            return true;
        };
        const pending = w._placeReferencePayload({members: [drag('member-a')]}, 40);
        await new Promise((r) => setTimeout(r, 0));
        assert.equal(scene.reference_items.length, 1, 'painted');
        const paintsBefore = w.paints;

        w.released[0].reject(new Error('Reference placement was refused.'));
        await pending;
        assert.equal(refetched, 1);
        assert.equal(scene.reference_items.length, 0, 'the paint was discarded');
        assert.ok(w.paints > paintsBefore, 'and the discard was drawn');
    """)
def test_an_append_whose_PRIOR_member_cannot_be_resolved_paints_nothing():
    """The refusal the drop resolver structurally cannot see.

    `_apply_update_reference_item` re-canonicalizes the WHOLE list, priors
    included, while `resolveReferenceDropVerdict` only ever examines the dragged
    members. A bar holding a member whose asset was trashed since it was staged
    is therefore refused for a row the drop rules never looked at -- so painting
    the new member would put it on a bar the save is about to reject outright.
    """
    _run_gesture_node(_CLASS_C_SETUP + """
        const scene = referenceScene();
        scene.reference_items = [{reference_item_id: 'ref-1', lane_index: 0,
            start_frame: 40, end_frame: -1,
            members: [{entity_id: 'entity-1', member_id: 'member-gone'}],
            prompt_override: '', strength: 1.0, sequence_frames: 0, muted: false}];
        const w = referenceWidget(scene);
        const pending = w._appendReferenceMembers('ref-1', {members: [drag('member-b')]});
        await new Promise((r) => setTimeout(r, 0));

        assert.equal(w.sent.length, 1, 'the write still goes, so the route speaks');
        assert.deepEqual(scene.reference_items[0].members.map((m) => m.member_id),
            ['member-gone'], 'and the addition was not painted');
        w.released[0].reject(new Error('Reference member asset not found'));
        await pending;
    """)


def test_an_append_paints_over_a_prior_that_carries_a_member_role():
    """A stored role must not cost the bar its optimistic paint.

    The staged form refuses those three fields because it cannot validate them;
    the stored form carries them, because the route's `legacy_members` leniency
    returns them unchanged. Using the strict form for priors would silently
    disable this paint for every bar the author has assigned roles on.
    """
    _run_gesture_node(_CLASS_C_SETUP + """
        const scene = referenceScene();
        const prior = {entity_id: 'entity-1', member_id: 'member-a',
                       visual_intent: 'preserve'};
        scene.reference_items = [{reference_item_id: 'ref-1', lane_index: 0,
            start_frame: 40, end_frame: -1, members: [prior],
            prompt_override: '', strength: 1.0, sequence_frames: 0, muted: false}];
        const w = referenceWidget(scene);
        const pending = w._appendReferenceMembers('ref-1', {members: [drag('member-b')]});
        await new Promise((r) => setTimeout(r, 0));

        assert.deepEqual(scene.reference_items[0].members,
            [prior, drag('member-b')], 'painted, with the role carried verbatim');
        assert.deepEqual(w.sent[0].operations[0].expected.members, [prior],
            'and the guard still describes the pre-paint list');
        w.released[0].resolve({payload: {results: []}});
        await pending;
    """)


def test_a_refused_stage_drops_the_undo_entry_it_created():
    """An entry with no post-snapshot is what wedges Undo.

    `_pushUndo` also clears the Redo stack, so leaving the entry behind costs the
    author their Redo as well as leaving a step that cannot be stamped.
    """
    _run_gesture_node(_CLASS_C_SETUP + """
        const scene = referenceScene();
        const w = referenceWidget(scene);
        const discarded = [];
        w._discardUnstampableUndoEntry = (entry) => { discarded.push(entry); };
        const pending = w._placeReferencePayload({members: [drag('member-a')]}, 40);
        await new Promise((r) => setTimeout(r, 0));
        assert.equal(w.undos.length, 1);

        w.released[0].reject(new Error('Reference placement was refused.'));
        await pending;
        assert.deepEqual(discarded, [w.undos[0]],
            'the entry this gesture pushed, by identity');
    """)
