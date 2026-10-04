"""Real host and mutation-queue checks for acknowledged Reference framing writes."""
import json

from server.media_helpers import resolve_reference_framing
from test_project_mutation_queue import ROOT, _run_gesture_node, _run_node


def test_reference_framing_browser_mirror_matches_tolerant_backend_defaults():
    values = [None, [], "wrong", 1, {}]
    values += [{"reference_fit_mode": mode, "reference_crop_position": anchor}
               for mode in ("fit", "pad_edge", "cover", "stretch", "bad", None, {})
               for anchor in ("center", "top", "bottom", "left", "right", "bad", [])]
    expected = [{"fitMode": result["fit_mode"], "cropPosition": result["crop_position"]}
                for result in map(resolve_reference_framing, values)]
    url = (ROOT / "web/js/editor_settings.js").as_uri()
    _run_node(f"""
        import assert from 'node:assert/strict';
        globalThis.window = {{localStorage: {{getItem: () => null}}}};
        const {{normalizeReferenceFraming}} = await import({url!r});
        assert.deepEqual({json.dumps(values)}.map(normalizeReferenceFraming), {json.dumps(expected)});
    """)


def test_rapid_framing_writes_are_serialized_per_field_and_adopt_only_after_acknowledgement():
    _run_gesture_node("""
        const w = makeWidget(), calls = [], metadata = {
            reference_fit_mode: 'cover', reference_crop_position: 'center', other: 'kept'
        };
        w._referenceFitMode = 'cover'; w._referenceCropPosition = 'center';
        w._syncSettingsPanelControls = () => {};
        let release;
        const held = new Promise(resolve => { release = resolve; });
        globalThis.fetch = async (url, init) => {
            const body = JSON.parse(init.body); calls.push(body);
            if (calls.length === 1) await held;
            Object.assign(metadata, body.metadata);
            return new Response(JSON.stringify({metadata: {...metadata}}), {status:200});
        };
        const a = w._setReferenceFraming('reference_fit_mode', 'stretch');
        const b = w._setReferenceFraming('reference_crop_position', 'right');
        const c = w._setReferenceFraming('reference_fit_mode', 'fit');
        await new Promise(resolve => setTimeout(resolve, 0));
        assert.equal(calls.length, 1);
        assert.equal(w._referenceFitMode, 'cover');
        assert.equal(w._referenceCropPosition, 'center');
        release(); await Promise.all([a,b,c]);
        assert.deepEqual(calls, [
            {metadata:{reference_fit_mode:'stretch'}},
            {metadata:{reference_crop_position:'right'}},
            {metadata:{reference_fit_mode:'fit'}},
        ]);
        assert.equal(w._referenceFitMode, 'fit');
        assert.equal(w._referenceCropPosition, 'right');
        assert.equal(metadata.other, 'kept');
        assert.equal(starts().length, 3);
    """)


def test_failed_framing_write_keeps_acknowledged_state_and_notifies():
    notifications = (ROOT / "web/js/editor_notifications.js").as_uri()
    _run_gesture_node(f"""
        const {{subscribe}} = await import({notifications!r});
        const messages = [];
        const off = subscribe(message => messages.push(message));
        const w = makeWidget();
        w._referenceFitMode = 'pad_edge'; w._referenceCropPosition = 'bottom';
        w._syncSettingsPanelControls = () => {{}};
        globalThis.fetch = async () => new Response(JSON.stringify({{error:'Refused'}}), {{status:400}});
        await assert.rejects(w._setReferenceFraming('reference_fit_mode', 'stretch'));
        assert.equal(w._referenceFitMode, 'pad_edge');
        assert.equal(w._referenceCropPosition, 'bottom');
        assert.ok(messages.length > 0);
        off();
    """)


def test_project_switch_does_not_adopt_inflight_response_or_send_queued_old_setting():
    _run_gesture_node("""
        const w = makeWidget(), calls = [];
        w._referenceFitMode = 'cover'; w._referenceCropPosition = 'center';
        w._syncSettingsPanelControls = () => {};
        let release;
        const held = new Promise(resolve => { release = resolve; });
        globalThis.fetch = async (url, init) => {
            calls.push(url); await held;
            return new Response(JSON.stringify({metadata:{reference_fit_mode:'stretch'}}), {status:200});
        };
        const a = w._setReferenceFraming('reference_fit_mode', 'stretch');
        const b = w._setReferenceFraming('reference_crop_position', 'right');
        const done = Promise.allSettled([a,b]);
        await new Promise(resolve => setTimeout(resolve, 0));
        w.projectDir = 'second'; w._referenceFitMode = 'fit'; w._referenceCropPosition = 'top';
        release(); await done;
        assert.equal(calls.length, 1);
        assert.equal(w._referenceFitMode, 'fit');
        assert.equal(w._referenceCropPosition, 'top');
    """)


def test_project_load_resolves_missing_and_malformed_framing_without_saving_it():
    _run_gesture_node("""
        const w = makeWidget(), calls = [];
        for (const name of ['_syncSettingsPanelControls', '_syncSceneResolutionControls',
                '_updateViewportHeader', '_resizeViewportCanvas']) w[name] = () => {};
        // Existing model-constraint healing is independent of framing.
        w._maybeHealFrameConstraint = w._maybeHealDimensionConstraint = async () => {};
        for (const metadata of [{}, {reference_fit_mode:[],reference_crop_position:'invalid'}]) {
            const data={metadata}; const before=JSON.stringify(data);
            globalThis.fetch = async (url, init) => {
                calls.push(init?.method || 'GET');
                return new Response(JSON.stringify(data), {status:200});
            };
            w._referenceFitMode='stretch'; w._referenceCropPosition='right';
            await w._fetchProjectSettings();
            assert.equal(w._referenceFitMode, 'cover');
            assert.equal(w._referenceCropPosition, 'center');
            assert.equal(JSON.stringify(data), before);
        }
        assert.deepEqual(calls, ['GET','GET']);
    """)


def test_project_load_does_not_adopt_settings_from_a_superseded_project():
    _run_gesture_node("""
        const w = makeWidget(); let release;
        const held = new Promise(resolve => { release=resolve; });
        globalThis.fetch = async () => {
            await held;
            return new Response(JSON.stringify({metadata:{reference_fit_mode:'stretch'}}), {status:200});
        };
        const pending=w._fetchProjectSettings();
        w.projectDir='second'; w._referenceFitMode='fit'; w._referenceCropPosition='top';
        release(); await pending;
        assert.equal(w._referenceFitMode, 'fit');
        assert.equal(w._referenceCropPosition, 'top');
    """)


def test_slow_project_read_cannot_replace_a_newer_acknowledged_framing_save():
    _run_gesture_node("""
        const w = makeWidget();
        for (const name of ['_syncSettingsPanelControls', '_syncSceneResolutionControls',
                '_updateViewportHeader', '_resizeViewportCanvas']) w[name] = () => {};
        w._maybeHealFrameConstraint = w._maybeHealDimensionConstraint = async () => {};
        w._referenceFitMode='cover'; w._referenceCropPosition='center';
        let release; const held=new Promise(resolve => { release=resolve; });
        globalThis.fetch = async (url, init) => {
            if (init?.method==='PUT') return new Response(JSON.stringify({
                modified_at:'2026-10-03T12:00:02',
                metadata:{reference_fit_mode:'stretch',reference_crop_position:'center'}
            }),{status:200,headers:{'X-Sonder-Project-Modified-At':'2026-10-03T12:00:02'}});
            await held;
            return new Response(JSON.stringify({modified_at:'2026-10-03T12:00:01',
                metadata:{reference_fit_mode:'pad_edge',reference_crop_position:'top'}
            }),{status:200});
        };
        const read=w._fetchProjectSettings();
        await w._setReferenceFraming('reference_fit_mode','stretch');
        release(); await read;
        assert.equal(w._referenceFitMode,'stretch');
        assert.equal(w._referenceCropPosition,'center');
    """)


def test_invalid_framing_intent_never_enters_transport():
    _run_gesture_node("""
        const w = makeWidget();
        let calls = 0; globalThis.fetch = async () => { calls++; };
        await assert.rejects(w._setReferenceFraming('reference_fit_mode', 'bad'));
        await assert.rejects(w._setReferenceFraming('other', 'center'));
        assert.equal(calls, 0);
    """)
