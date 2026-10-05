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
from test_control_descriptors import describe, _control, _node, CLASSES
from test_sdk_runner import World


def prompt():
    result = {
        '1': _node('SonderEditor', {'project': 'P'}),
        '2': _node('SonderSaveVideo', {'project': ['1', 0], 'mark_queue_complete': False,
                                     'place_audio_on_timeline': True}),
        '3': _control('20', 'INT', control_id='steps'),
        '5': _control('', 'SEED', seed=9007199254740993, control_id='seed'),
        '6': _node('KSampler', {'seed': ['5', 0], 'steps': ['3', 0]}),
        '7': _node('SonderPanelNote', {'note_id': 'intro', 'text': '# Author intro'}),
    }
    result['5']['inputs'].update(seed_mode='increment', panel_note='Author seed note')
    return result


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'folder_paths', types.SimpleNamespace(get_user_directory=lambda: str(tmp_path)))
    sdk_runs.reset_for_tests()
    result = World()
    result.runner._describe = describe
    sdk_templates.replace_template(result.template_id, json.dumps(prompt()).encode(), validate=lambda p: {'ok': True})
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
    actual_describe = cd.describe_controls
    monkeypatch.setattr(cd, 'describe_controls', lambda p, **kw: actual_describe(p, **({'class_lookup': CLASSES.get} | kw)))
    async def run():
        session = await world.open()
        async with client_for(world, monkeypatch) as client:
            response = await client.get(f'/sonder-editor/sdk/session/{session.session_id}/controls', headers=headers(session))
            assert response.status == 200
            return await response.json()
    result = asyncio.run(run())
    assert result['view'] == routes._describe_controls_body(json.dumps({'prompt': prompt()}).encode())
    seed = next(c for c in result['view']['controls'] if c['control_id'] == 'seed')
    assert seed['seed'] == seed['value'] == '9007199254740993'
    assert seed['max'] == '18446744073709551615' and seed['seed_mode'] == 'increment'


def test_http_auth_atomic_refusal_and_saved_values_reach_generate_but_notes_do_not(world, monkeypatch):
    async def run():
        session = await world.open()
        path = f'/sonder-editor/sdk/session/{session.session_id}/controls'
        changes = {'control:steps:value': '31', 'control:seed:seed': '9007199254740995',
                   'control:seed:seed_mode': 'fixed', 'control:seed:note': '**SDK note**',
                   'note:intro:text': '# SDK intro', 'field:SonderSaveVideo/mark_queue_complete': 'true'}
        async with client_for(world, monkeypatch) as client:
            for method in ('get', 'put'):
                response = await getattr(client, method)(path, headers={sdk_routes.SECRET_HEADER: 'wrong'}, json={'changes': changes})
                assert response.status == 404
            response = await client.put(path, headers=headers(session, False), json={'changes': changes})
            assert response.status == 409 and (await response.json())['code'] == 'not_owner'
            before = sdk_templates.read_control_values(world.template_id)
            response = await client.put(path, headers=headers(session), json={'changes': {**changes, 'control:missing:value': 1}})
            refused = await response.json()
            assert response.status == 400 and refused['code'] == 'invalid_change'
            assert refused['rejected'][0]['target'] == 'control:missing:value'
            assert sdk_templates.read_control_values(world.template_id) == before
            response = await client.put(path, headers=headers(session), json={'changes': changes})
            result = await response.json()
            assert response.status == 200 and result['seq'] == before['seq'] + 1
            reloaded = await client.get(path, headers=headers(session))
            assert (await reloaded.json()) == result
            await world.runner.generate(session, count=2)
        return result
    result = asyncio.run(run())
    seed = next(c for c in result['view']['controls'] if c['control_id'] == 'seed')
    assert seed['note'] == '**SDK note**' and seed['seed_mode'] == 'fixed'
    assert result['view']['notes'][0]['text'] == '# SDK intro'
    for graph in world.client.submitted:
        assert graph['3']['inputs']['value_text'] == '31'
        assert graph['5']['inputs']['seed'] == 9007199254740995
        assert graph['5']['inputs']['panel_note'] == 'Author seed note'
        assert graph['7']['inputs']['text'] == '# Author intro'
        assert graph['2']['inputs']['mark_queue_complete'] is True


@pytest.mark.parametrize('changes', [
    {'control:steps:order': 10000}, {'note:intro:order': True}, {'note:intro:order': '1.5'},
    {'control:seed:group': []}, {'field:SonderSaveVideo/mark_queue_complete:order': 2},
    {}, [], {'control:steps:value': 0}, {'control:steps:value': '1.5'},
    {'control:seed:seed': True}, {'control:seed:seed': -1},
    {'control:seed:seed': '18446744073709551616'}, {'control:seed:seed': '1.5'},
    {'control:seed:seed_mode': 'unknown'}, {'control:seed:seed_mode': []},
    {'note:intro:text': 12}, {'note:intro:text': 'x' * 4001},
    {'control:seed:note': 'x' * 4001}, {'field:SonderSaveVideo/mark_queue_complete': 'maybe'},
    {f'control:c{i}:value': 1 for i in range(65)},
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


def test_generate_holds_control_writes_until_the_batch_finishes(world):
    async def run():
        session = await world.open()
        entered, release = asyncio.Event(), asyncio.Event()
        async def submit(graph):
            entered.set()
            await release.wait()
        world.client.on_submit = submit
        generating = asyncio.create_task(world.runner.generate(session, count=2))
        await asyncio.wait_for(entered.wait(), 3)
        saving = asyncio.create_task(world.runner.change_controls(session.session_id, session.secret, 'tab-1',
                                                                 {'control:steps:value': 44}))
        viewing = asyncio.create_task(world.runner.controls_view(session))
        await asyncio.sleep(0.02)
        assert not saving.done() and not viewing.done()
        release.set()
        await asyncio.wait_for(generating, 3)
        result = await asyncio.wait_for(saving, 3)
        await asyncio.wait_for(viewing, 3)
        assert next(c for c in result['view']['controls'] if c['control_id'] == 'steps')['value'] == 44
    asyncio.run(run())
    assert [g['3']['inputs']['value_text'] for g in world.client.submitted] == ['20', '20']
    assert [g['5']['inputs']['seed'] for g in world.client.submitted] == [9007199254740993, 1125899906842624]  # native frontend clamps advances to 2**50


def test_owner_takeover_while_waiting_refuses_the_old_tab(world):
    async def run():
        session = await world.open()
        async with world.runner.template_lock(world.template_id):
            saving = asyncio.create_task(world.runner.change_controls(session.session_id, session.secret, 'tab-1',
                                                                     {'control:steps:value': 44}))
            await asyncio.sleep(0.02)
            world.owners[session.session_id] = {'session_id': 'tab-2'}
        with pytest.raises(SdkError) as exc:
            await saving
        assert exc.value.code == 'not_owner'
        assert sdk_templates.read_control_values(world.template_id)['controls'] == {}
    asyncio.run(run())


def test_seed_mode_fallback_precedence_and_corrupt_stored_seed_revalidated(world):
    async def run():
        session = await world.open()
        assert next(c for c in (await world.runner.controls_view(session))['view']['controls']
                    if c['control_id'] == 'seed')['seed_mode'] == 'increment'
        sdk_templates.update_control_values(world.template_id, lambda d: d['controls'].update(
            seed={'seed_mode': 'invalid', 'seed': -1}))
        seed = next(c for c in (await world.runner.controls_view(session))['view']['controls'] if c['control_id'] == 'seed')
        assert seed['seed_mode'] == 'increment' and seed['error']
        with pytest.raises(SdkError) as exc:
            await world.runner.generate(session)
        assert exc.value.code == 'generate_refused'
    asyncio.run(run())


def test_reimport_clears_notes_and_layout_keeps_values_and_reports_count(world):
    async def run():
        session = await world.open()
        await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {
            'control:steps:value': 33, 'control:seed:seed_mode': 'fixed',
            'control:seed:note': '', 'note:intro:text': 'Session intro',
        })
        sdk_templates.update_control_values(world.template_id, lambda d: d.update(
            layout={'control:steps': {'order': 7}}, future={'keep': True}))
        before = sdk_templates.read_control_values(world.template_id)
        result = sdk_templates.replace_template(world.template_id, json.dumps(prompt()).encode(), validate=lambda p: {'ok': True})
        assert result['notes_reset'] == 2 and result['layout_reset'] == 1
        after = sdk_templates.read_control_values(world.template_id)
        assert after['controls'] == before['controls'] and after['fields'] == before['fields']
        assert after['notes'] == after['layout'] == {} and after['future'] == {'keep': True}
        assert after['seq'] == before['seq'] + 1
        view = (await world.runner.controls_view(session))['view']
        assert view['notes'][0]['text'] == '# Author intro'
        assert next(c for c in view['controls'] if c['control_id'] == 'steps')['value'] == 33
    asyncio.run(run())


@pytest.mark.parametrize('failed_suffix', ['.json', '.controls.json', '.meta.json'])
def test_failed_replace_restores_exact_original_files(world, monkeypatch, failed_suffix):
    tid = world.template_id
    sdk_templates.store_control_changes(tid, {'note:intro:text': 'Keep this note'})
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
    changed = prompt()
    changed['7']['inputs']['text'] = 'New export'
    with pytest.raises(PermissionError):
        sdk_templates.replace_template(tid, json.dumps(changed).encode(), validate=lambda p: {'ok': True})
    assert {p: sdk_templates._read_bytes(p) for p in paths} == before


def test_interrupted_reimport_never_rebinds_old_notes_to_a_new_export(world):
    sdk_templates.store_control_changes(world.template_id, {'note:intro:text': 'Old override'})
    path, _, sidecar = sdk_templates._template_paths(world.template_id)
    before = sdk_templates._read_bytes(sidecar)
    changed = prompt()
    changed['7']['inputs']['text'] = 'New author intro'
    sdk_templates._write_bytes_atomic(path, json.dumps(changed).encode())  # crash after first replacement write
    assert sdk_templates.read_control_values(world.template_id)['notes'] == {}
    assert sdk_templates._read_bytes(sidecar) == before  # reads never rewrite a valid sidecar
    sdk_templates.store_control_changes(world.template_id, {'control:steps:value': 30})
    assert sdk_templates.read_control_values(world.template_id)['notes'] == {}


@pytest.mark.parametrize('identity_case', ['duplicate', 'missing', 'long_note'])
def test_noneditable_identities_and_truncated_notes_cannot_lose_authored_text(world, identity_case):
    graph = prompt()
    target = 'control:steps:value'
    if identity_case == 'duplicate':
        graph['8'] = copy.deepcopy(graph['3'])
        graph['8']['inputs']['value_text'] = '22'
    elif identity_case == 'missing':
        graph['3']['inputs']['control_id'] = ''
        target = 'control::value'
    else:
        graph['7']['inputs']['text'] = 'x' * 4001
        target = 'note:intro:text'
    sdk_templates.replace_template(world.template_id, json.dumps(graph).encode(), validate=lambda p: {'ok': True})
    async def run():
        session = await world.open()
        with pytest.raises(SdkError) as exc:
            await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {target: 'x'})
        assert exc.value.code == 'invalid_change'
    asyncio.run(run())
    assert sdk_templates.load_template(world.template_id)[0] == graph


def test_full_combo_options_are_validated_beyond_the_wire_preview(world):
    class Combo:
        @classmethod
        def INPUT_TYPES(cls):
            return {'required': {'choice': ([f'option{i}' for i in range(2100)],)}}
    graph = prompt()
    graph['3']['inputs'].update(kind='COMBO', value_text='option0')
    graph['6'] = _node('Combo', {'choice': ['3', 0]})
    world.runner._describe = lambda p: cd.describe_controls(p, class_lookup={**CLASSES, 'Combo': Combo}.get)
    sdk_templates.replace_template(world.template_id, json.dumps(graph).encode(), validate=lambda p: {'ok': True})
    async def run():
        session = await world.open()
        result = await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {'control:steps:value': 'option2099'})
        assert result['view']['controls'][0]['value'] == 'option2099'
        with pytest.raises(SdkError):
            await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {'control:steps:value': 'no-such-option'})
    asyncio.run(run())


def test_shared_control_preserves_every_instances_bounds_and_errors(world):
    class Limited:
        @classmethod
        def INPUT_TYPES(cls):
            return {'required': {'steps': ('INT', {'min': 1, 'max': 30})}}
    graph = prompt()
    graph['8'] = copy.deepcopy(graph['3'])
    graph['9'] = _node('Limited', {'steps': ['8', 0]})
    world.runner._describe = lambda p: cd.describe_controls(p, class_lookup={**CLASSES, 'Limited': Limited}.get)
    sdk_templates.replace_template(world.template_id, json.dumps(graph).encode(), validate=lambda p: {'ok': True})
    async def run():
        session = await world.open()
        view = (await world.runner.controls_view(session))['view']
        shared = next(c for c in view['controls'] if c['control_id'] == 'steps')
        assert shared['keys'] == ['3', '8'] and shared['max'] == 30
        with pytest.raises(SdkError):
            await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {'control:steps:value': 31})
        sdk_templates.store_control_changes(world.template_id, {'control:steps:value': 35})
        with pytest.raises(SdkError) as exc:
            await world.runner.generate(session)
        assert exc.value.code == 'generate_refused'
        result = await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {'control:steps:value': 30})
        assert next(c for c in result['view']['controls'] if c['control_id'] == 'steps')['error'] is None
        await world.runner.generate(session)
    asyncio.run(run())
    assert world.client.submitted[0]['3']['inputs']['value_text'] == world.client.submitted[0]['8']['inputs']['value_text'] == '30'


def test_merged_field_writes_every_instance_and_refuses_a_linked_instance(world):
    graph = prompt()
    graph['10'] = copy.deepcopy(graph['2'])
    sdk_templates.replace_template(world.template_id, json.dumps(graph).encode(), validate=lambda p: {'ok': True})
    async def run():
        session = await world.open()
        await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {'field:SonderSaveVideo/mark_queue_complete': True})
        await world.runner.generate(session)
        graph['10']['inputs']['mark_queue_complete'] = ['6', 0]
        sdk_templates.replace_template(world.template_id, json.dumps(graph).encode(), validate=lambda p: {'ok': True})
        with pytest.raises(SdkError):
            await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {'field:SonderSaveVideo/mark_queue_complete': False})
    asyncio.run(run())
    assert world.client.submitted[0]['2']['inputs']['mark_queue_complete'] is True
    assert world.client.submitted[0]['10']['inputs']['mark_queue_complete'] is True


def test_old_export_without_mode_uses_randomize_and_sidecar_wins(world):
    graph = prompt()
    graph['5']['inputs'].pop('seed_mode')
    sdk_templates.replace_template(world.template_id, json.dumps(graph).encode(), validate=lambda p: {'ok': True})
    async def run():
        session = await world.open()
        seed = next(c for c in (await world.runner.controls_view(session))['view']['controls'] if c['control_id'] == 'seed')
        assert seed['seed_mode'] == 'randomize'
        result = await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {'control:seed:seed_mode': 'decrement'})
        assert next(c for c in result['view']['controls'] if c['control_id'] == 'seed')['seed_mode'] == 'decrement'
    asyncio.run(run())


def test_cancelled_save_keeps_the_lock_until_its_worker_finishes_before_reimport(world, monkeypatch):
    import threading
    from server.sdk_runner import await_template_io
    entered, release = threading.Event(), threading.Event()
    original_store = sdk_templates.store_control_changes
    def paused_store(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_store(*args, **kwargs)
    monkeypatch.setattr(sdk_templates, 'store_control_changes', paused_store)
    async def run():
        session = await world.open()
        saving = asyncio.create_task(world.runner.change_controls(session.session_id, session.secret, 'tab-1',
                                                                 {'note:intro:text': 'Old edit'}))
        assert await asyncio.to_thread(entered.wait, 5)
        saving.cancel()
        await asyncio.sleep(0.01)
        saving.cancel()  # repeated cancellation cannot detach the worker either
        changed = prompt()
        changed['7']['inputs']['text'] = 'New author intro'
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
        generating = asyncio.create_task(world.runner.generate(session))
        assert await asyncio.to_thread(entered.wait, 5)
        generating.cancel()
        saving = asyncio.create_task(world.runner.change_controls(session.session_id, session.secret, 'tab-1',
                                                                 {'control:seed:seed': 44, 'control:seed:seed_mode': 'fixed'}))
        await asyncio.sleep(0.01)
        try:
            assert not generating.done() and not saving.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await generating
        result = await saving
        assert next(c for c in result['view']['controls'] if c['control_id'] == 'seed')['seed'] == 44
        assert sdk_templates.read_control_values(world.template_id)['controls']['seed']['seed'] == 44
    asyncio.run(run())


def test_layout_is_atomic_view_only_preserves_unknown_storage_and_resets_on_reimport(world, monkeypatch):
    async def run():
        session = await world.open()
        changes = {'control:steps:order': '-5', 'control:steps:group': ' Advanced ',
                   'note:intro:order': 10, 'note:intro:group': 'Instructions'}
        path = f'/sonder-editor/sdk/session/{session.session_id}/controls'
        async with client_for(world, monkeypatch) as client:
            response = await client.put(path, headers=headers(session), json={'changes': changes})
            assert response.status == 200, await response.text()
            view = (await response.json())['view']
            steps = next(c for c in view['controls'] if c['control_id'] == 'steps')
            assert (steps['order'], steps['group']) == (-5, 'Advanced')
            assert (view['notes'][0]['order'], view['notes'][0]['group']) == (10, 'Instructions')
        before = sdk_templates.read_control_values(world.template_id)
        with pytest.raises(SdkError):
            await world.runner.change_controls(session.session_id, session.secret, 'tab-1',
                {'control:steps:order': 8, 'note:intro:order': 10000})
        assert sdk_templates.read_control_values(world.template_id) == before
        def unknown(data):
            data['layout']['control:steps']['future'] = {'keep': True}
            data['layout']['note:removed'] = {'order': 50000, 'group': 'Old'}
        sdk_templates.update_control_values(world.template_id, unknown)
        await world.runner.change_controls(session.session_id, session.secret, 'tab-1', {'control:steps:value': 31})
        assert sdk_templates.read_control_values(world.template_id)['layout']['control:steps']['future'] == {'keep': True}
        await world.runner.generate(session, count=1)
        submitted = world.client.submitted[-1]
        assert submitted['3']['inputs']['panel_order'] == 0 and submitted['3']['inputs']['panel_group'] == ''
        assert 'panel_order' not in submitted['7']['inputs']
        replaced = sdk_templates.replace_template(world.template_id, json.dumps(prompt()).encode(), validate=lambda p: {'ok': True})
        assert replaced['layout_reset'] == 3
        view = (await world.runner.controls_view(session))['view']
        steps = next(c for c in view['controls'] if c['control_id'] == 'steps')
        assert (steps['order'], steps['group'], steps['value']) == (0, '', 31)
        assert sdk_templates.read_control_values(world.template_id)['layout'] == {}
    asyncio.run(run())
