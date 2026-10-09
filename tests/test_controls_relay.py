"""Mounted Controls transport: real coercion, ownership, lifecycle and HTTP."""

import asyncio
import copy
import importlib
import json
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import server
from server import control_descriptors as cd, session_registry as sr
from test_control_descriptors import describe
from test_sdk_controls_routes import prompt

ROOT = Path(__file__).resolve().parents[1]
PID, HID, CANVAS, TAB = 'ZZ-controls-relay-20261005', 'host-A', 'canvas-A', 'tab-A'


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    for name in ('_sessions', '_canvas_hosts', '_handoffs', '_widget_state', '_widget_sources', '_controls_relay'):
        monkeypatch.setattr(sr, name, {})
    monkeypatch.setattr(sr, '_lock', asyncio.Lock())
    monkeypatch.setattr(sr, '_sdk_secret_for_host', None)
    events = []
    monkeypatch.setattr(sr, 'schedule_project_event', lambda p, e: events.append((p, copy.deepcopy(e))))
    async def broadcast(p, e):
        events.append((p, copy.deepcopy(e)))
    monkeypatch.setattr(sr, 'broadcast_project_event', broadcast)
    return events


async def setup(pid=PID, hid=HID, canvas=CANVAS, tab=TAB):
    assert (await sr.register_canvas_host(pid, 'editor-1', canvas, host_id=hid))['ok']
    info = {'host_id': hid, 'source_node_id': 'editor-1'}
    assert (await sr.claim_session(pid, tab + '-fs', 'fullscreen', info))['ok']
    handoff = await sr.create_handoff(pid, tab + '-fs', host_id=hid)
    assert (await sr.claim_session(pid, tab, 'tab', info, handoff_token=handoff['token']))['ok']


def publication(seq=1, applied=None, rejected=None):
    return {'view': describe(prompt()), 'view_seq': seq, 'prompt_hash': 'hash-' + str(seq),
            'applied': applied or [], 'rejected': rejected or []}


async def publish(body=None, canvas=CANVAS):
    return await sr.publish_controls_view(PID, HID, canvas, body or publication())


async def post(changes=None, tab=TAB):
    return await sr.post_control_changes(PID, HID, tab, changes or {'binding:steps:value': '30'})


async def pending():
    return await sr.get_control_changes(PID, HID, CANVAS)


def test_acknowledgement_survives_poll_retry_socket_loss_and_later_publication(registry):
    async def run():
        await setup()
        body = publication()
        assert (await publish(body))['ok']
        body['view']['bindings'].clear()  # published state does not alias the caller
        changes = {'binding:steps:value': '30', 'binding:seed:value': '18446744073709551615',
                   'binding:seed:seed_mode': 'decrement', 'note:intro:text': '# Updated',
                   'binding:advance:value': True, 'binding:seed:label': 'Seed (tab)'}
        rid = (await post(changes))['request_id']
        first = await pending()
        assert first == await pending()  # polling cannot drain before an acknowledgement
        assert first['pending'][0]['changes'] == changes
        first['pending'].clear()
        assert len((await pending())['pending']) == 1
        ack = publication(2, [rid])
        assert (await publish(ack))['ok']
        assert (await publish(ack))['duplicate'] is True
        assert not (await pending())['pending']
        assert (await sr.get_controls_view(PID, HID))['applied'] == [rid]
        assert (await publish(publication(3)))['ok']
        fetched = await sr.get_controls_view(PID, HID)
        assert fetched['applied'] == [rid]  # an outage does not lose the prior outcome
        fetched['view']['bindings'].clear()
        assert (await sr.get_controls_view(PID, HID))['view']['bindings']
    asyncio.run(run())
    announced = [e for p, e in registry if e['type'] == 'controls_changed']
    assert [e['view_seq'] for e in announced] == [1, 2, 3]
    assert all(set(e) == {'type', 'project_id', 'host_id', 'view_seq'} for e in announced)


@pytest.mark.parametrize('changes', [
    {}, [], {'anything': True}, {'binding:steps:value': 'bad'},
    {'binding:steps:value': -1}, {'binding:steps:value': 10001},
    {'binding:seed:value': '18446744073709551616'}, {'binding:seed:value': -1},
    {'binding:seed:seed_mode': 'surprise'}, {'note:intro:text': 'x' * 4001},
    {'binding:steps:note': 2}, {'binding:steps:order': 10000},
    {'binding:advance:value': 'maybe'}, {'binding:advance:value': 'true'},
    {'control:steps:value': '30'}, {'field:SonderSaveVideo/mark_queue_complete': True},
    {f'note:n{i}:text': '' for i in range(65)},
])
def test_invalid_batches_never_partially_enqueue(changes):
    async def run():
        await setup()
        await publish()
        result = await sr.post_control_changes(PID, HID, TAB, changes)
        assert result['code'] == 'invalid_change' and result['rejected']
        assert not (await pending())['pending']
        result = await post({'binding:steps:value': 3, 'unknown': 2})
        assert result['code'] == 'invalid_change'
        assert not (await pending())['pending']
    asyncio.run(run())


def test_queue_bound_is_atomic_under_concurrent_requests():
    async def run():
        await setup()
        await publish()
        results = await asyncio.gather(*(post() for _ in range(40)))
        assert sum(r['ok'] for r in results) == 32
        assert [r['code'] for r in results if not r['ok']] == ['queue_full'] * 8
        queued = (await pending())['pending']
        assert len({r['request_id'] for r in queued}) == 32
        await publish(publication(2, [queued[0]['request_id']]))
        assert (await post())['ok']
        assert len((await pending())['pending']) == 32
    asyncio.run(run())


def test_refused_ack_unknown_target_contradictions_and_sequence_are_atomic():
    async def run():
        await setup()
        await publish()
        rid = (await post())['request_id']
        for body in (publication(2, ['unknown']), publication(2, [rid], [
            {'request_id': rid, 'target': 'binding:steps:value', 'reason': 'refused'}]),
            publication(2, rejected=[{'request_id': rid, 'target': 'other', 'reason': 'refused'}])):
            assert (await publish(body))['code'] == 'invalid_ack'
            assert (await sr.get_controls_view(PID, HID))['view_seq'] == 1
            assert len((await pending())['pending']) == 1
        rows = [{'request_id': rid, 'target': 'binding:steps:value', 'reason': 'Target changed'}]
        assert (await publish(publication(2, rejected=rows)))['ok']
        assert (await sr.get_controls_view(PID, HID))['rejected'] == rows
        assert (await publish(publication(3, [rid])))['code'] == 'invalid_ack'
        assert (await publish(publication(1)))['code'] == 'stale_view'
        body = publication(2, rejected=rows)
        body['prompt_hash'] = 'different'
        assert (await publish(body))['code'] == 'stale_view'
        assert not (await pending())['pending']
    asyncio.run(run())


def test_host_project_owner_and_sdk_boundaries():
    async def run():
        await setup()
        await publish()
        assert (await publish(canvas=TAB))['code'] == 'not_canvas_host'
        assert (await sr.get_control_changes(PID, HID, TAB))['code'] == 'not_canvas_host'
        assert (await post(tab=CANVAS))['code'] == 'not_owner'
        assert (await sr.set_controls_visibility(PID, HID, 'other-tab', True))['code'] == 'not_owner'
        assert (await sr.post_control_changes(PID, 'other-host', TAB, {'x': 2}))['code'] == 'canvas_host_disconnected'
        assert (await sr.get_controls_view('other-project', HID))['view'] is None
        sr.set_sdk_secret_resolver(lambda h: 'secret' if h == HID else None)
        assert (await publish())['code'] == 'invalid_host'
        assert (await post())['code'] == 'invalid_host'
        assert (await pending())['code'] == 'invalid_host'
    asyncio.run(run())


@pytest.mark.parametrize('visible', [None, 1, 'true', [], {}])
def test_visibility_requires_boolean(visible):
    assert asyncio.run(sr.set_controls_visibility(PID, HID, TAB, visible))['code'] == 'invalid_visibility'


def test_visibility_presence_and_orphan_recovery_preserve_the_queue(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(sr, '_now', lambda: now[0])
    async def run():
        await setup()
        await publish()
        await post()
        assert (await sr.set_controls_visibility(PID, HID, TAB, True))['controls_visible']
        state = await sr.get_widget_state(PID, 'editor-1', host_id=HID)
        assert state['controls_visible'] and state['controls_view_seq'] == 1
        now[0] += 31
        assert not (await sr.get_controls_view(PID, HID))['controls_visible']
        assert (await post())['code'] == 'session_orphaned'
        assert len((await pending())['pending']) == 1
        assert (await sr.heartbeat_session(PID, TAB, host_id=HID))['ok']
        assert (await sr.get_controls_view(PID, HID))['controls_visible']
        assert (await sr.set_controls_visibility(PID, HID, TAB, False))['controls_visible'] is False
    asyncio.run(run())


@pytest.mark.parametrize('cleanup', ['release', 'unregister', 'replace', 'lazy-host-expiry', 'sweep-host-expiry', 'owner-expiry'])
def test_relay_lifecycle_clears_everything_but_not_another_host(cleanup, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(sr, '_now', lambda: now[0])
    async def run():
        await setup()
        await setup(hid='host-B', canvas='canvas-B', tab='tab-B')
        await publish()
        await post()
        await sr.set_controls_visibility(PID, HID, TAB, True)
        if cleanup == 'release':
            await sr.release_session(PID, TAB, host_id=HID)
        elif cleanup == 'unregister':
            await sr.unregister_canvas_host(PID, 'editor-1', CANVAS, host_id=HID)
        elif cleanup == 'replace':
            await sr.register_canvas_host(PID, 'editor-1', 'new-canvas', host_id=HID)
        elif cleanup == 'owner-expiry':
            sr._sessions[(PID, HID)].update(status='orphaned', orphan_expires_at=99)
        else:
            sr._canvas_hosts[(PID, HID)]['last_seen'] = 1
            if cleanup == 'sweep-host-expiry':
                await sr.sweep_stale_sessions_once()
        result = await sr.get_controls_view(PID, HID)
        assert result['view'] is None and result['view_seq'] == 0 and not result['controls_visible']
        assert (PID, HID) not in sr._controls_relay
        assert await sr.get_canvas_host(PID, host_id='host-B')
    asyncio.run(run())


def test_same_host_reconnect_keeps_requests_and_ack_history_is_bounded():
    async def run():
        await setup()
        await publish()
        rid = (await post())['request_id']
        await sr.register_canvas_host(PID, 'editor-1', CANVAS, host_id=HID)
        assert (await pending())['pending'][0]['request_id'] == rid
        await publish(publication(2, [rid]))
        first = rid
        for seq in range(3, 130):
            rid = (await post())['request_id']
            assert (await publish(publication(seq, [rid])))['ok']
        fetched = await sr.get_controls_view(PID, HID)
        assert len(fetched['applied']) == 128 and first in fetched['applied']
        assert (await post())['code'] == 'queue_full'
        assert (await sr.set_controls_visibility(PID, HID, 'wrong-tab', True, fetched['applied']))['code'] == 'not_owner'
        assert (await sr.set_controls_visibility(PID, HID, TAB, True, first))['code'] == 'invalid_receipts'
        assert (await sr.set_controls_visibility(PID, HID, TAB, True, fetched['applied']))['ok']
        assert (await sr.set_controls_visibility(PID, HID, TAB, True, fetched['applied']))['ok']
        assert not (await sr.get_controls_view(PID, HID))['applied']
        rid = (await post())['request_id']
        await sr.set_controls_visibility(PID, HID, TAB, True, [rid])
        assert (await pending())['pending'][0]['request_id'] == rid
        assert (await publish(publication(130, [rid])))['ok']
        assert (await sr.get_controls_view(PID, HID))['applied'] == [rid]
    asyncio.run(run())


@pytest.mark.parametrize('mutation', ['new-view', 'release', 'host-replace'])
def test_validation_worker_does_not_hold_registry_lock_and_cannot_cross_lifecycle(mutation, monkeypatch):
    entered, resume = threading.Event(), threading.Event()
    original = cd.validate_binding_changes
    def slow(*args, **kwargs):
        entered.set()
        assert resume.wait(5)
        return original(*args, **kwargs)
    async def run():
        await setup()
        await publish()
        monkeypatch.setattr(cd, 'validate_binding_changes', slow)
        task = asyncio.create_task(post())
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            assert (await asyncio.wait_for(sr.get_controls_view(PID, HID), 1))['view_seq'] == 1
            if mutation == 'new-view':
                assert (await publish(publication(2)))['ok']
            elif mutation == 'release':
                await sr.release_session(PID, TAB, host_id=HID)
            else:
                await sr.register_canvas_host(PID, 'editor-1', 'new-canvas', host_id=HID)
        finally:
            resume.set()
        result = await task
        if mutation == 'new-view':
            # A publication that lands during validation is revalidated against,
            # never answered with a refusal the tab could not have avoided.
            assert result['ok']
            [request] = sr._controls_relay[(PID, HID)]['pending']
            assert request['request_id'] == result['request_id'] and request['view_seq'] == 2
        else:
            # A cleared relay never accepts the request; the retry sees no view.
            assert result['code'] in ('not_owner', 'view_unavailable')
            assert not sr._controls_relay.get((PID, HID), {}).get('pending')
    asyncio.run(run())


def test_post_revalidation_is_bounded_when_every_attempt_goes_stale(monkeypatch):
    original = cd.validate_binding_changes
    calls = []
    async def run():
        await setup()
        await publish()
        seq = iter(range(2, 10))
        def racing(*args, **kwargs):
            # Each validation sees a newer publication land before it finishes.
            calls.append(1)
            result = original(*args, **kwargs)
            asyncio.run_coroutine_threadsafe(publish(publication(next(seq))), loop).result(3)
            return result
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(cd, 'validate_binding_changes', racing)
        result = await post()
        assert result['code'] == 'stale_view'
        assert len(calls) == sr.CONTROLS_POST_ATTEMPTS
        assert not sr._controls_relay[(PID, HID)]['pending']
    asyncio.run(run())


def test_publication_worker_cannot_cross_host_replacement(monkeypatch):
    entered, resume = threading.Event(), threading.Event()
    original = sr._prepare_control_publication
    def slow(body):
        entered.set()
        assert resume.wait(5)
        return original(body)
    async def run():
        await setup()
        monkeypatch.setattr(sr, '_prepare_control_publication', slow)
        task = asyncio.create_task(publish())
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            await asyncio.wait_for(sr.unregister_canvas_host(PID, 'editor-1', CANVAS, host_id=HID), 1)
            await sr.register_canvas_host(PID, 'editor-1', CANVAS, host_id=HID)
        finally:
            resume.set()
        assert (await task)['code'] == 'host_changed'
        assert (await sr.get_controls_view(PID, HID))['view'] is None
    asyncio.run(run())


@pytest.mark.parametrize('patch', [
    {'view_seq': 0}, {'view_seq': True}, {'view_seq': 2**53}, {'prompt_hash': ''},
    {'view': []}, {'applied': 'rid'}, {'applied': ['r'] * 33}, {'rejected': [{}]},
    {'view': {'bindings': [{}], 'notes': []}},
    {'view': {'bindings': [{'binding_id': 'c', 'status': 'ok'}]}},
    {'view': {'bindings': [{'binding_id': 'c', 'status': 'ok', 'kind': 'COMBO', 'options': 7}], 'notes': []}},
    {'view': {'bindings': [{'binding_id': 'c', 'status': 'ok', 'kind': 'COMBO', 'options': ['a'], 'options_truncated': 'false'}], 'notes': []}},
    {'view': {'bindings': [{'binding_id': 'c', 'status': 'ok', 'targets': 'x'}], 'notes': []}},
    {'view': {'bindings': [], 'notes': [{'note_id': 3, 'status': 'ok'}]}},
    {'view': {'bindings': [], 'notes': [], 'suggestions': 'x'}},
    {'view': {'controls': [{'control_id': 'c', 'status': 'ok'}], 'fields': [], 'notes': []}},
])
def test_malformed_publications_are_refused_without_mutation(patch):
    async def run():
        await setup()
        body = publication()
        body.update(patch)
        assert (await publish(body))['code'] == 'invalid_view'
        assert (await sr.get_controls_view(PID, HID))['view'] is None
    asyncio.run(run())


def client_for(monkeypatch):
    from server import routes
    monkeypatch.setattr(server, 'PromptServer', SimpleNamespace(instance=SimpleNamespace(routes=web.RouteTableDef())), raising=False)
    module = importlib.reload(routes)
    monkeypatch.setattr(module, 'remember_event_loop', lambda: None)
    def disk_read(*args, **kwargs):
        raise AssertionError('Controls relay must not read project storage')
    monkeypatch.setattr(module, 'load_project', disk_read)
    app = web.Application(middlewares=[module._sonder_security_middleware], client_max_size=2 * 1024 * 1024)
    for route in module.routes:
        if route.path in ('/sonder-editor/project/{project_id}/controls',
                          '/sonder-editor/project/{project_id}/control_changes',
                          '/sonder-editor/project/{project_id}/controls_visibility', '/sonder-editor/ws'):
            app.router.add_route(route.method, route.path, route.handler)
    return TestClient(TestServer(app))


def test_real_http_roundtrip_caps_security_and_raw_folder_identity(monkeypatch):
    async def run():
        await setup()
        async with client_for(monkeypatch) as client:
            path = f'/sonder-editor/project/{PID}'
            body = dict(publication(), host_id=HID, session_id=CANVAS)
            response = await client.put(path + '/controls', json=body)
            assert response.status == 200
            response = await client.get(path + '/controls', params={'host_id': HID})
            snapshot = await response.json()
            assert snapshot['project_id'] == PID and snapshot['view'] == cd.json_safe(describe(prompt()))
            assert next(d for d in snapshot['view']['bindings'] if d['binding_id'] == 'seed')['value'] == '9007199254740993'
            response = await client.post(path + '/control_changes', json={'host_id': HID, 'session_id': TAB,
                'changes': {'binding:seed:value': '18446744073709551615'}})
            rid = (await response.json())['request_id']
            response = await client.get(path + '/control_changes', params={'host_id': HID, 'session_id': CANVAS})
            # An exact 64-bit seed crosses the relay as a decimal string, never a rounded number.
            assert (await response.json())['pending'][0]['changes']['binding:seed:value'] == '18446744073709551615'
            body.update(publication(2, [rid]))
            assert (await client.put(path + '/controls', json=body)).status == 200
            assert (await client.put(path + '/controls_visibility', json={'host_id': HID, 'session_id': TAB, 'visible': True, 'seen': [rid]})).status == 200
            snapshot = await (await client.get(path + '/controls', params={'host_id': HID})).json()
            assert not snapshot['applied']
            assert (await client.get(path + '/controls?host_id=host-A&path=X')).status == 400
            assert (await client.put(path + '/controls', json=body, headers={'Origin': 'https://evil.invalid'})).status == 403
            for raw in (b'not JSON', b'[]', b'{'):
                assert (await client.put(path + '/controls', data=raw)).status == 400
            assert (await client.put(path + '/controls', data=b'x' * (sr.CONTROLS_MAX_BYTES + 1))).status == 413
            async def chunks():
                for _ in range(9):
                    yield b'x' * 65536
            assert (await client.put(path + '/controls', data=chunks())).status == 413
    asyncio.run(run())


def test_large_pending_response_encodes_off_loop_without_blocking_other_registry_reads(monkeypatch):
    entered, resume = threading.Event(), threading.Event()
    original = json.dumps
    loop_thread = threading.get_ident()
    threads = []
    def tracked(value, *args, **kwargs):
        if isinstance(value, dict) and 'pending' in value:
            threads.append(threading.get_ident())
            entered.set()
            assert resume.wait(5)
        return original(value, *args, **kwargs)
    async def run():
        await setup()
        body = publication()
        body['view'] = {'bindings': [{'binding_id': 'text', 'kind': 'STRING', 'status': 'ok', 'note': '',
                                      'targets': [{'path': '7', 'input': 'text'}]}], 'notes': []}
        assert (await publish(body))['ok']
        for _ in range(32):
            assert (await post({'binding:text:value': 'x' * (500 * 1024)}))['ok']
        async with client_for(monkeypatch) as client:
            monkeypatch.setattr(json, 'dumps', tracked)
            path = f'/sonder-editor/project/{PID}'
            task = asyncio.create_task(client.get(path + '/control_changes', params={'host_id': HID, 'session_id': CANVAS}))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                response = await asyncio.wait_for(client.get(path + '/controls', params={'host_id': HID}), 1)
                assert response.status == 200
                assert (await response.json())['view_seq'] == 1
            finally:
                resume.set()
            response = await task
            assert len(await response.read()) > 16_000_000
            assert len((await pending())['pending']) == 32
    asyncio.run(run())
    assert threads and all(t != loop_thread for t in threads)


def test_actual_websocket_close_reconnect_and_same_host_subscribe_preserve_requests(monkeypatch):
    async def subscribed(ws, hid=HID):
        await ws.send_json({'type': 'subscribe', 'role': 'canvas_host', 'project_id': PID,
                            'host_id': hid, 'source_node_id': 'editor-1', 'session_id': CANVAS})
        while (await ws.receive_json(timeout=2))['type'] != 'subscribed':
            pass
    async def run():
        await setup()
        async with client_for(monkeypatch) as client:
            ws = await client.ws_connect('/sonder-editor/ws')
            await ws.receive_json(timeout=2)  # native sid
            await subscribed(ws)
            assert (await publish())['ok']
            rid = (await post())['request_id']
            await subscribed(ws)  # a duplicate subscription cannot clear pending writes
            assert (await pending())['pending'][0]['request_id'] == rid
            await ws.close()
            await asyncio.sleep(0)  # server teardown
            assert (await pending())['pending'][0]['request_id'] == rid
            assert (await post())['ok']  # tab can enqueue during the canvas socket outage
            response = await client.get(f'/sonder-editor/project/{PID}/control_changes',
                                        params={'host_id': HID, 'session_id': CANVAS})
            assert len((await response.json())['pending']) == 2
            ws = await client.ws_connect('/sonder-editor/ws')
            await ws.receive_json(timeout=2)
            await subscribed(ws)
            assert len((await pending())['pending']) == 2
            await subscribed(ws, hid='other-host')  # explicit departure clears this host
            assert (await sr.get_controls_view(PID, HID))['view'] is None
            await ws.close()
    asyncio.run(run())


@pytest.mark.parametrize('departure', ['unregister_host', 'going_away', 'reconfigure', 'plain_close'])
def test_deliberate_canvas_departure_unregisters_after_grace_but_a_plain_close_waits_for_ttl(departure, monkeypatch):
    from aiohttp import WSCloseCode
    monkeypatch.setattr(sr, 'CANVAS_HOST_DEPARTURE_GRACE_SECONDS', 0.05)
    async def run():
        await setup()
        async with client_for(monkeypatch) as client:
            ws = await client.ws_connect('/sonder-editor/ws')
            await ws.receive_json(timeout=2)  # native sid
            await ws.send_json({'type': 'subscribe', 'role': 'canvas_host', 'project_id': PID,
                                'host_id': HID, 'source_node_id': 'editor-1', 'session_id': CANVAS})
            while (await ws.receive_json(timeout=2))['type'] != 'subscribed':
                pass
            assert (await publish())['ok']
            assert (await post())['ok']
            if departure in ('unregister_host', 'reconfigure'):
                # cross_tab_sync.close(): node removed, project or workflow switched.
                await ws.send_json({'type': 'unregister_host'})
                await ws.close()
            elif departure == 'going_away':
                # The browser closes with 1001 when the canvas page reloads or closes.
                await ws.close(code=WSCloseCode.GOING_AWAY)
            else:
                await ws.close()  # a close without the departure signal
            await asyncio.sleep(0.01)
            # Within the grace the tab still sees the canvas.
            assert (await sr.get_widget_state(PID, 'editor-1', host_id=HID))['canvas_host_connected'] is True
            if departure == 'reconfigure':
                # Native Undo/Redo or a workflow reload rebuilds the controller,
                # which registers the same host with a new session at once.
                await sr.register_canvas_host(PID, 'editor-1', 'canvas-B', host_id=HID)
            for _ in range(20):
                await asyncio.sleep(0.01)  # grace elapses
            state = await sr.get_widget_state(PID, 'editor-1', host_id=HID)
            if departure in ('plain_close', 'reconfigure'):
                assert state['canvas_host_connected'] is True
                if departure == 'plain_close':
                    assert (await sr.get_controls_view(PID, HID))['view'] is not None
            else:
                assert state['canvas_host_connected'] is False
                assert (await sr.get_controls_view(PID, HID))['view'] is None
    asyncio.run(run())


def test_canvas_publication_budget_mirrors_registry_cap():
    source = (ROOT / 'web/js/controls_canvas_relay.js').read_text(encoding='utf-8')
    match = re.search(r'const MAX_PUBLICATION_BYTES = (\d+) \* (\d+) - (\d+);', source)
    assert match, 'controls_canvas_relay.js must state its publication budget'
    a, b, margin = map(int, match.groups())
    assert a * b == sr.CONTROLS_MAX_BYTES and 0 < margin < 4096


def grouped_publication(seq=1):
    # A group of 70 Controls: renaming it must never meet the 64-change cap.
    view = describe(prompt())
    view['bindings'] += [{'binding_id': f'm{i}', 'status': 'ok', 'kind': 'INT', 'label': f'M{i}', 'order': i,
                          'group': 'Look', 'section': 'workflow', 'targets': []} for i in range(70)]
    view['groups'] = {'Look': {'order': 1}}
    view['group_order'] = cd.group_order(view)
    return {'view': view, 'view_seq': seq, 'prompt_hash': 'hash-' + str(seq), 'applied': [], 'rejected': []}


def test_group_ops_are_forwarded_unexpanded_for_the_canvas_to_revalidate():
    async def run():
        await setup()
        assert (await publish(grouped_publication()))['ok']
        changes = {'group:Look:rename': 'Style', 'group:New:create': {}, 'group:Look2:move': {'before': None}}
        rid = (await post(changes))['request_id']
        forwarded = (await pending())['pending'][0]
        # The original intent, not the validator's plan; one target per op.
        assert forwarded['request_id'] == rid and forwarded['changes'] == changes
        # Shape is still checked here.
        refused = await post({'group:Look:rename': 'a:b'})
        assert refused['code'] == 'invalid_change' and 'colon' in refused['rejected'][0]['reason']
    asyncio.run(run())


def test_a_publication_with_many_groups_fits_the_mounted_budget():
    # R6: 256 groups of 120 characters, in both `groups` and `group_order`.
    body = grouped_publication()
    names = [f'{i:03d}' + 'g' * 117 for i in range(256)]
    body['view']['groups'] = {name: {'order': i} for i, name in enumerate(names)}
    body['view']['group_order'] = names
    encoded = len(json.dumps(body, ensure_ascii=False).encode('utf-8'))
    assert encoded < sr.CONTROLS_MAX_BYTES // 4

    async def run():
        await setup()
        assert (await publish(body))['ok']
        assert (await sr.get_controls_view(PID, HID))['view']['group_order'] == names
    asyncio.run(run())
