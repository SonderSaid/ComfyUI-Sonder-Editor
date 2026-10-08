"""Real descriptor/overlay/HTTP/Generate boundary tests for the SDK Controls API."""

import asyncio
import copy
import json
import sys
import types

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from server import control_descriptors as cd, sdk_routes, sdk_templates, sdk_runs
from server.sdk_runner import SdkError
from test_control_descriptors import describe, _binding, _node, _relay, CLASSES
from test_sdk_runner import World


def manifest(graph):
    return json.loads(graph['8']['inputs']['manifest'])


def prompt(bindings=None, notes=None):
    return {
        '1': _node('SonderEditor', {'project': 'P'}),
        '2': _node('SonderSaveVideo', {'project': ['1', 0], 'mark_queue_complete': False,
                                     'place_audio_on_timeline': True}),
        '6': _node('KSampler', {'seed': 9007199254740993, 'steps': 20}),
        '8': _relay(bindings if bindings is not None else {
            'steps': _binding('6', 'steps', label='Steps'),
            'seed': _binding('6', 'seed', label='Seed', seed_mode='increment', note='Author seed note'),
            'advance': _binding('2', 'mark_queue_complete', label='Advance', section='editor'),
        }, notes=notes if notes is not None else {'intro': {'text': '# Author intro'}}),
    }


def binding_of(view, binding_id):
    return next(b for b in view['bindings'] if b['binding_id'] == binding_id)


def install(world, graph):
    sdk_templates.replace_template(world.template_id, json.dumps(graph).encode(), validate=lambda p: {'ok': True})


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'folder_paths', types.SimpleNamespace(get_user_directory=lambda: str(tmp_path)))
    sdk_runs.reset_for_tests()
    result = World()
    result.runner._describe = describe
    install(result, prompt())
    yield result
    result.runner.close()
    sdk_runs.reset_for_tests()


def client_for(world, monkeypatch):
    monkeypatch.setattr(sdk_routes, 'get_runner', lambda *a, **kw: world.runner)
    routes = web.RouteTableDef()
    sdk_routes.register_sdk_routes(routes, resolve_project=world.resolve, remember_event_loop=lambda: None,
                                  projects_dir=lambda: '')
    app = web.Application()
    app.add_routes(routes)
    return TestClient(TestServer(app))


def headers(session, owner=True):
    return {sdk_routes.SECRET_HEADER: session.secret,
            sdk_routes.TAB_HEADER: 'tab-1' if owner else 'tab-2'}


def test_describe_route_and_sdk_get_share_the_view_and_exact_numbers(world, monkeypatch):
    from server import routes
    actual_describe = cd.describe_bindings
    monkeypatch.setattr(cd, 'describe_bindings', lambda p, **kw: actual_describe(p, **({'class_lookup': CLASSES.get} | kw)))
    async def run():
        session = await world.open()
        async with client_for(world, monkeypatch) as client:
            response = await client.get(f'/sonder-editor/sdk/session/{session.session_id}/controls', headers=headers(session))
            assert response.status == 200
            return await response.json()
    result = asyncio.run(run())
    assert result['view'] == routes._describe_controls_body(json.dumps({'prompt': prompt()}).encode())
    seed = binding_of(result['view'], 'seed')
    assert seed['value'] == '9007199254740993'
    assert seed['max'] == '18446744073709551615' and seed['seed_mode'] == 'increment'


def test_http_auth_atomic_refusal_and_saved_values_reach_generate_but_presentation_does_not(world, monkeypatch):
    async def run():
        session = await world.open()
        path = f'/sonder-editor/sdk/session/{session.session_id}/controls'
        changes = {'binding:steps:value': '31', 'binding:seed:value': '9007199254740995',
                   'binding:seed:seed_mode': 'fixed', 'binding:seed:note': '**SDK note**',
                   'binding:seed:label': 'My seed', 'note:intro:text': '# SDK intro',
                   'binding:advance:value': True}
        async with client_for(world, monkeypatch) as client:
            for method in ('get', 'put'):
                response = await getattr(client, method)(path, headers={sdk_routes.SECRET_HEADER: 'wrong'}, json={'changes': changes})
                assert response.status == 404
            response = await client.put(path, headers=headers(session, False), json={'changes': changes})
            assert response.status == 409 and (await response.json())['code'] == 'not_owner'
            before = sdk_templates.read_control_values(world.template_id)
            response = await client.put(path, headers=headers(session), json={'changes': {**changes, 'binding:missing:value': 1}})
            refused = await response.json()
            assert response.status == 400 and refused['code'] == 'invalid_change'
            assert refused['rejected'][0]['target'] == 'binding:missing:value'
            assert sdk_templates.read_control_values(world.template_id) == before
            response = await client.put(path, headers=headers(session), json={'changes': changes})
            result = await response.json()
            assert response.status == 200 and result['seq'] == before['seq'] + 1
            reloaded = await client.get(path, headers=headers(session))
            assert (await reloaded.json()) == result
            await world.runner.generate(session, tab_session_id="tab-1", count=2)
        return result
    result = asyncio.run(run())
    seed = binding_of(result['view'], 'seed')
    assert seed['note'] == '**SDK note**' and seed['seed_mode'] == 'fixed' and seed['label'] == 'My seed'
    assert seed['value'] == '9007199254740995'
    assert result['view']['notes'][0]['text'] == '# SDK intro'
    assert len(world.client.submitted) == 2
    for graph in world.client.submitted:
        assert graph['6']['inputs']['steps'] == 31
        assert graph['6']['inputs']['seed'] == 9007199254740995
        assert graph['2']['inputs']['mark_queue_complete'] is True
        # Labels, notes and layout are view-only: the submitted manifest is the export's.
        assert graph['8'] == prompt()['8']


@pytest.mark.parametrize('changes', [
    {'binding:steps:order': 10000}, {'note:intro:order': True}, {'note:intro:order': '1.5'},
    {'binding:seed:group': []}, {}, [], {'binding:steps:value': 0}, {'binding:steps:value': '1.5'},
    {'binding:seed:value': True}, {'binding:seed:value': -1},
    {'binding:seed:value': '18446744073709551616'}, {'binding:seed:value': '1.5'},
    {'binding:seed:seed_mode': 'unknown'}, {'binding:seed:seed_mode': []},
    {'note:intro:text': 12}, {'note:intro:text': 'x' * 4001}, {'binding:seed:note': 'x' * 4001},
    {'binding:seed:label': 'x' * 121}, {'binding:advance:value': 'maybe'}, {'binding:advance:value': 'true'},
    {'control:steps:value': 1}, {'field:SonderSaveVideo/mark_queue_complete': True},
    {f'binding:c{i}:value': 1 for i in range(65)},
])
def test_invalid_changes_refuse_without_a_partial_write(world, changes):
    async def run():
        session = await world.open()
        before = sdk_templates.read_control_values(world.template_id)
        with pytest.raises(SdkError) as exc:
            await world.runner.change_controls(session.session_id, session.secret, 'tab-1', changes)
        assert exc.value.code == 'invalid_change' and exc.value.status == 400
        assert sdk_templates.read_control_values(world.template_id) == before
    asyncio.run(run())


def test_http_bad_json_and_body_cap(world, monkeypatch):
    async def run():
        session = await world.open()
        path = f'/sonder-editor/sdk/session/{session.session_id}/controls'
        async with client_for(world, monkeypatch) as client:
            for body in (b'{oops', b'[]', b'null'):
                response = await client.put(path, headers=headers(session), data=body)
                assert response.status == 400 and (await response.json())['code'] == 'invalid_change'
            response = await client.put(path, headers=headers(session), data=b'x' * (512 * 1024 + 1))
            assert response.status == 413
    asyncio.run(run())


def test_selector_choice_and_a_new_choice_list_save_and_route_together(world):
    graph = prompt({'choice': _binding('5', 'choice', label='Feature'), 'list': _binding('5', 'choice_list')})
    graph['5'] = _node('SonderSelector', {'choice': 'Base', 'choice_list': 'Base\nDecompression\nDetail'})
    install(world, graph)
    async def run():
        session = await world.open()
        view = (await world.runner.controls_view(session))['view']
        assert binding_of(view, 'choice')['options'] == ['Base', 'Decompression', 'Detail']
        with pytest.raises(SdkError):
            await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {'binding:choice:value': 'Glow'})
        result = await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {
            'binding:list:value': 'Base\nGlow', 'binding:choice:value': 'Glow'})
        assert binding_of(result['view'], 'choice')['options'] == ['Base', 'Glow']
        await world.runner.generate(session, tab_session_id='tab-1')
    asyncio.run(run())
    assert world.client.submitted[0]['5']['inputs'] == {'choice': 'Glow', 'choice_list': 'Base\nGlow'}


def test_generate_holds_control_writes_until_the_batch_finishes(world):
    async def run():
        session = await world.open()
        entered, release = asyncio.Event(), asyncio.Event()
        async def submit(graph):
            entered.set()
            await release.wait()
        world.client.on_submit = submit
        generating = asyncio.create_task(world.runner.generate(session, tab_session_id="tab-1", count=2))
        await asyncio.wait_for(entered.wait(), 3)
        saving = asyncio.create_task(world.runner.change_controls(session.session_id, session.secret, 'tab-1',
                                                                 {'binding:steps:value': 44}))
        viewing = asyncio.create_task(world.runner.controls_view(session))
        await asyncio.sleep(0.02)
        assert not saving.done() and not viewing.done()
        release.set()
        await asyncio.wait_for(generating, 3)
        result = await asyncio.wait_for(saving, 3)
        await asyncio.wait_for(viewing, 3)
        assert binding_of(result['view'], 'steps')['value'] == 44
    asyncio.run(run())
    assert [g['6']['inputs']['steps'] for g in world.client.submitted] == [20, 20]
    assert [g['6']['inputs']['seed'] for g in world.client.submitted] == [9007199254740993, 1125899906842624]  # native frontend clamps advances to 2**50


def test_owner_takeover_while_waiting_refuses_the_old_tab(world):
    async def run():
        session = await world.open()
        async with world.runner.template_lock(world.template_id):
            saving = asyncio.create_task(world.runner.change_controls(session.session_id, session.secret, 'tab-1',
                                                                     {'binding:steps:value': 44}))
            await asyncio.sleep(0.02)
            world.owners[session.session_id] = {'session_id': 'tab-2'}
        with pytest.raises(SdkError) as exc:
            await saving
        assert exc.value.code == 'not_owner'
        assert sdk_templates.read_control_values(world.template_id)['bindings'] == {}
    asyncio.run(run())


def test_seed_mode_fallback_and_a_corrupt_stored_seed_resets_without_blocking(world):
    async def run():
        session = await world.open()
        assert binding_of((await world.runner.controls_view(session))['view'], 'seed')['seed_mode'] == 'increment'
        sig = {'class_type': 'KSampler', 'input': 'seed', 'kind': 'SEED'}
        sdk_templates.update_control_values(world.template_id, lambda d: d['bindings'].update(
            seed={'seed_mode': 'invalid', 'value': -1, 'sig': sig}))
        seed = binding_of((await world.runner.controls_view(session))['view'], 'seed')
        assert seed['seed_mode'] == 'increment' and seed['reset'] is True
        await world.runner.generate(session, tab_session_id="tab-1")
    asyncio.run(run())
    assert world.client.submitted[0]['6']['inputs']['seed'] == 9007199254740993


def test_reimport_clears_presentation_keeps_values_and_reports_count(world):
    async def run():
        session = await world.open()
        await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {
            'binding:steps:value': 33, 'binding:seed:seed_mode': 'fixed', 'binding:steps:label': 'Quality',
            'binding:seed:note': '', 'note:intro:text': 'Session intro',
        })
        sdk_templates.update_control_values(world.template_id, lambda d: d.update(
            layout={'binding:steps': {'order': 7}}, future={'keep': True}))
        before = sdk_templates.read_control_values(world.template_id)
        result = sdk_templates.replace_template(world.template_id, json.dumps(prompt()).encode(), validate=lambda p: {'ok': True})
        assert result['notes_reset'] == 2 and result['layout_reset'] == 2
        after = sdk_templates.read_control_values(world.template_id)
        assert after['bindings'] == before['bindings']
        assert after['notes'] == after['layout'] == after['labels'] == {} and after['future'] == {'keep': True}
        assert after['seq'] == before['seq'] + 1
        view = (await world.runner.controls_view(session))['view']
        assert view['notes'][0]['text'] == '# Author intro'
        steps = binding_of(view, 'steps')
        assert steps['value'] == 33 and steps['label'] == 'Steps'
    asyncio.run(run())


@pytest.mark.parametrize('failed_suffix', ['.json', '.controls.json', '.meta.json'])
def test_failed_replace_restores_exact_original_files(world, monkeypatch, failed_suffix):
    tid = world.template_id
    sdk_templates.store_binding_changes(tid, {'note:intro:text': 'Keep this note'}, {'bindings': []})
    paths = sdk_templates._template_paths(tid)
    before = {p: sdk_templates._read_bytes(p) for p in paths}
    original_bytes, original_json = sdk_templates._write_bytes_atomic, sdk_templates.write_json_atomic
    failed = []
    failed_path = paths[0] if failed_suffix == '.json' else next(p for p in paths if p.endswith(failed_suffix))
    def fail_once(path, data, write):
        if path == failed_path and not failed:
            failed.append(True)
            raise PermissionError('Transient storage failure')
        return write(path, data)
    monkeypatch.setattr(sdk_templates, '_write_bytes_atomic', lambda p, d: fail_once(p, d, original_bytes))
    monkeypatch.setattr(sdk_templates, 'write_json_atomic', lambda p, d: fail_once(p, d, original_json))
    changed = prompt(notes={'intro': {'text': 'New export'}})
    with pytest.raises(PermissionError):
        sdk_templates.replace_template(tid, json.dumps(changed).encode(), validate=lambda p: {'ok': True})
    assert {p: sdk_templates._read_bytes(p) for p in paths} == before


def test_interrupted_reimport_never_rebinds_old_notes_to_a_new_export(world):
    sdk_templates.store_binding_changes(world.template_id, {'note:intro:text': 'Old override'}, {'bindings': []})
    path, _, sidecar = sdk_templates._template_paths(world.template_id)
    before = sdk_templates._read_bytes(sidecar)
    changed = prompt(notes={'intro': {'text': 'New author intro'}})
    sdk_templates._write_bytes_atomic(path, json.dumps(changed).encode())  # crash after first replacement write
    assert sdk_templates.read_control_values(world.template_id)['notes'] == {}
    assert sdk_templates._read_bytes(sidecar) == before  # reads never rewrite a valid sidecar
    sdk_templates.store_binding_changes(world.template_id, {'binding:steps:value': 30}, {'bindings': []})
    assert sdk_templates.read_control_values(world.template_id)['notes'] == {}


@pytest.mark.parametrize('case', ['duplicate', 'missing', 'oversized_note'])
def test_problem_bindings_and_an_unreadable_manifest_refuse_writes_and_keep_the_template(world, case):
    target = 'binding:steps:value'
    if case == 'duplicate':
        graph = prompt({'steps': _binding(None, 'steps', status='duplicate')})
    elif case == 'missing':
        graph = prompt({'steps': _binding('77', 'steps')})
    else:
        graph = prompt(notes={'intro': {'text': 'x' * 4001}})
        target = 'note:intro:text'
    install(world, graph)
    async def run():
        session = await world.open()
        with pytest.raises(SdkError) as exc:
            await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {target: 'x'})
        assert exc.value.code == 'invalid_change'
        if case == 'oversized_note':
            view = (await world.runner.controls_view(session))['view']
            assert view['relay']['status'] == 'error' and '4000' in view['error']
            with pytest.raises(SdkError) as refused:
                await world.runner.generate(session, tab_session_id='tab-1')
            assert refused.value.code == 'generate_refused'
    asyncio.run(run())
    assert sdk_templates.load_template(world.template_id)[0] == graph


def test_full_combo_options_are_validated_beyond_the_wire_preview(world):
    class Combo:
        @classmethod
        def INPUT_TYPES(cls):
            return {'required': {'choice': ([f'option{i}' for i in range(2100)],)}}
    graph = prompt({'pick': _binding('9', 'choice', label='Pick')})
    graph['9'] = _node('Combo', {'choice': 'option0'})
    world.runner._describe = lambda p: cd.describe_bindings(p, class_lookup={**CLASSES, 'Combo': Combo}.get)
    install(world, graph)
    async def run():
        session = await world.open()
        result = await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {'binding:pick:value': 'option2099'})
        assert binding_of(result['view'], 'pick')['value'] == 'option2099'
        with pytest.raises(SdkError):
            await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {'binding:pick:value': 'no-such-option'})
    asyncio.run(run())


def test_a_promoted_input_keeps_every_consumers_bounds_and_an_out_of_range_value_resets(world):
    class Limited:
        @classmethod
        def INPUT_TYPES(cls):
            return {'required': {'steps': ('INT', {'min': 1, 'max': 30})}}
    graph = prompt({'steps': {'label': 'Steps', 'status': 'ok',
                              'targets': [{'path': '6', 'input': 'steps'}, {'path': '9', 'input': 'steps'}]}})
    graph['9'] = _node('Limited', {'steps': 20})
    world.runner._describe = lambda p: cd.describe_bindings(p, class_lookup={**CLASSES, 'Limited': Limited}.get)
    install(world, graph)
    async def run():
        session = await world.open()
        view = (await world.runner.controls_view(session))['view']
        shared = binding_of(view, 'steps')
        assert len(shared['targets']) == 2 and shared['max'] == 30
        with pytest.raises(SdkError):
            await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {'binding:steps:value': 31})
        sdk_templates.store_binding_changes(world.template_id, {'binding:steps:value': 35}, view)
        assert binding_of((await world.runner.controls_view(session))['view'], 'steps')['reset'] is True
        await world.runner.generate(session, tab_session_id="tab-1")
        result = await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {'binding:steps:value': 30})
        assert binding_of(result['view'], 'steps')['error'] is None
        await world.runner.generate(session, tab_session_id="tab-1")
    asyncio.run(run())
    assert [(g['6']['inputs']['steps'], g['9']['inputs']['steps']) for g in world.client.submitted] == [(20, 20), (30, 30)]


def test_an_editor_binding_writes_only_its_own_instance_and_a_linked_one_is_skipped(world):
    graph = prompt()
    graph['10'] = copy.deepcopy(graph['2'])
    install(world, graph)
    async def run():
        session = await world.open()
        await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {'binding:advance:value': True})
        await world.runner.generate(session, tab_session_id="tab-1")
        graph['2']['inputs']['mark_queue_complete'] = ['6', 0]
        install(world, graph)
        view = (await world.runner.controls_view(session))['view']
        assert binding_of(view, 'advance')['status'] == 'linked'
        with pytest.raises(SdkError):
            await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {'binding:advance:value': False})
        await world.runner.generate(session, tab_session_id="tab-1")
    asyncio.run(run())
    first, second = world.client.submitted
    assert first['2']['inputs']['mark_queue_complete'] is True and first['10']['inputs']['mark_queue_complete'] is False
    assert second['2']['inputs']['mark_queue_complete'] == ['6', 0]


def test_an_export_without_a_mode_uses_randomize_and_the_sidecar_wins(world):
    install(world, prompt({'seed': _binding('6', 'seed', label='Seed')}))
    async def run():
        session = await world.open()
        assert binding_of((await world.runner.controls_view(session))['view'], 'seed')['seed_mode'] == 'randomize'
        result = await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {'binding:seed:seed_mode': 'decrement'})
        assert binding_of(result['view'], 'seed')['seed_mode'] == 'decrement'
    asyncio.run(run())


def test_cancelled_save_keeps_the_lock_until_its_worker_finishes_before_reimport(world, monkeypatch):
    import threading
    from server.sdk_runner import await_template_io
    entered, release = threading.Event(), threading.Event()
    original_store = sdk_templates.store_binding_changes
    def paused_store(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_store(*args, **kwargs)
    monkeypatch.setattr(sdk_templates, 'store_binding_changes', paused_store)
    async def run():
        session = await world.open()
        saving = asyncio.create_task(world.runner.change_controls(session.session_id, session.secret, 'tab-1',
                                                                 {'note:intro:text': 'Old edit'}))
        assert await asyncio.to_thread(entered.wait, 5)
        saving.cancel()
        await asyncio.sleep(0.01)
        saving.cancel()  # repeated cancellation cannot detach the worker either
        changed = prompt(notes={'intro': {'text': 'New author intro'}})
        async def reimport():
            async with world.runner.template_lock(world.template_id):
                return await await_template_io(sdk_templates.replace_template, world.template_id,
                                               json.dumps(changed).encode(), validate=lambda p: {'ok': True})
        replacing = asyncio.create_task(reimport())
        await asyncio.sleep(0.01)
        try:
            assert not saving.done() and not replacing.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await saving
        result = await replacing
        assert result['notes_reset'] == 1
        assert sdk_templates.read_control_values(world.template_id)['notes'] == {}
    asyncio.run(run())


def test_cancelled_generate_finishes_its_seed_advance_before_a_later_control_save(world, monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()
    original_update = sdk_templates.update_control_values
    paused = []
    def paused_advance(*args, **kwargs):
        if not paused:
            paused.append(True)
            entered.set()
            assert release.wait(5)
        return original_update(*args, **kwargs)
    monkeypatch.setattr(sdk_templates, 'update_control_values', paused_advance)
    async def run():
        session = await world.open()
        generating = asyncio.create_task(world.runner.generate(session, tab_session_id="tab-1"))
        assert await asyncio.to_thread(entered.wait, 5)
        generating.cancel()
        saving = asyncio.create_task(world.runner.change_controls(session.session_id, session.secret, 'tab-1',
                                                                 {'binding:seed:value': 44, 'binding:seed:seed_mode': 'fixed'}))
        await asyncio.sleep(0.01)
        try:
            assert not generating.done() and not saving.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await generating
        result = await saving
        assert binding_of(result['view'], 'seed')['value'] == 44
        assert sdk_templates.read_control_values(world.template_id)['bindings']['seed']['value'] == 44
    asyncio.run(run())


def test_layout_is_atomic_view_only_preserves_unknown_storage_and_resets_on_reimport(world, monkeypatch):
    async def run():
        session = await world.open()
        changes = {'binding:steps:order': '-5', 'binding:steps:group': ' Advanced ',
                   'note:intro:order': 10, 'note:intro:group': 'Instructions'}
        path = f'/sonder-editor/sdk/session/{session.session_id}/controls'
        async with client_for(world, monkeypatch) as client:
            response = await client.put(path, headers=headers(session), json={'changes': changes})
            assert response.status == 200, await response.text()
            view = (await response.json())['view']
            steps = binding_of(view, 'steps')
            assert (steps['order'], steps['group']) == (-5, 'Advanced')
            assert (view['notes'][0]['order'], view['notes'][0]['group']) == (10, 'Instructions')
        before = sdk_templates.read_control_values(world.template_id)
        with pytest.raises(SdkError):
            await world.runner.change_controls(session.session_id, session.secret, 'tab-1',
                {'binding:steps:order': 8, 'note:intro:order': 10000})
        assert sdk_templates.read_control_values(world.template_id) == before
        def unknown(data):
            data['layout']['binding:steps']['future'] = {'keep': True}
            data['layout']['note:removed'] = {'order': 50000, 'group': 'Old'}
        sdk_templates.update_control_values(world.template_id, unknown)
        await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {'binding:steps:value': 31})
        assert sdk_templates.read_control_values(world.template_id)['layout']['binding:steps']['future'] == {'keep': True}
        await world.runner.generate(session, tab_session_id="tab-1", count=1)
        assert world.client.submitted[-1]['8'] == prompt()['8']
        replaced = sdk_templates.replace_template(world.template_id, json.dumps(prompt()).encode(), validate=lambda p: {'ok': True})
        assert replaced['layout_reset'] == 3
        view = (await world.runner.controls_view(session))['view']
        steps = binding_of(view, 'steps')
        assert (steps['order'], steps['group'], steps['value']) == (0, '', 31)
        assert sdk_templates.read_control_values(world.template_id)['layout'] == {}
    asyncio.run(run())


def test_values_route_rejects_older_save_after_server_delay(world, monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()
    original = sdk_templates.write_editor_state
    def delayed(project, values, *, writer):
        if writer["seq"] == 1:
            entered.set()
            assert release.wait(5)
        return original(project, values, writer=writer)
    monkeypatch.setattr(sdk_templates, "write_editor_state", delayed)
    async def go():
        session = await world.open()
        async with client_for(world, monkeypatch) as client:
            url = f"/sonder-editor/sdk/session/{session.session_id}/values"
            old = asyncio.create_task(client.put(url, headers=headers(session), json={"values": {"selection_start": 20}, "seq": 1}))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                new = await client.put(url, headers=headers(session), json={"values": {"selection_start": 40}, "seq": 2})
                assert new.status == 200 and (await new.json())["stale"] is False
            finally:
                release.set()
            result = await old
            assert result.status == 200
            assert (await result.json()) == {"ok": True, "values": {"selection_start": 40}, "stale": True}
    asyncio.run(go())


def test_status_details_only_for_owner_tab(world, monkeypatch):
    async def go():
        session = await world.open()
        await world.runner.generate(session, tab_session_id="tab-1")
        run = session.runs[0]
        run.detail = "ValueError: private/path"
        async with client_for(world, monkeypatch) as client:
            url = f"/sonder-editor/sdk/session/{session.session_id}/status"
            owner = await client.get(url, headers=headers(session))
            assert (await owner.json())["details"] == {run.run_id: run.detail}
            launcher = await client.get(url, headers={sdk_routes.SECRET_HEADER: session.secret})
            assert "private/path" not in await launcher.text()
            displaced = await client.get(url, headers=headers(session, owner=False))
            assert displaced.status == 409 and "private/path" not in await displaced.text()
    asyncio.run(go())


def test_more_than_64_stored_values_apply_and_a_refused_one_resets_alone(world):
    bindings = {f'b{i}': _binding('9', f'v{i}') for i in range(70)}
    graph = prompt(bindings)

    class Wide:
        @classmethod
        def INPUT_TYPES(cls):
            return {'required': {f'v{i}': ('INT', {'min': 0, 'max': 100}) for i in range(70)}}
    graph['9'] = _node('Wide', {f'v{i}': 1 for i in range(70)})
    world.runner._describe = lambda p: cd.describe_bindings(p, class_lookup={**CLASSES, 'Wide': Wide}.get)
    install(world, graph)
    sig = lambda i: {'class_type': 'Wide', 'input': f'v{i}', 'kind': 'INT'}
    sdk_templates.update_control_values(world.template_id, lambda d: d['bindings'].update(
        {f'b{i}': {'value': 500 if i == 3 else i, 'sig': sig(i)} for i in range(70)}))
    async def run():
        session = await world.open()
        view = (await world.runner.controls_view(session))['view']
        assert binding_of(view, 'b3')['reset'] is True and binding_of(view, 'b69')['value'] == 69
        await world.runner.generate(session, tab_session_id='tab-1')
    asyncio.run(run())
    inputs = world.client.submitted[0]['9']['inputs']
    assert inputs['v3'] == 1 and inputs['v69'] == 69 and inputs['v64'] == 64
    assert sdk_templates.list_templates(requirements=True)  # the Launcher still lists it
