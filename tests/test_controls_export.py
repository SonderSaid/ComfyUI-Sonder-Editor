"""4C: real descriptions, curated snapshots, storage and HTTP boundaries."""
import asyncio
import copy
import json
import threading
import pytest
from server import control_manifest as cm, control_descriptors as cd, sdk_templates as st
from test_control_descriptors import CLASSES, describe, _node, _binding, _relay
from test_sdk_controls_routes import world, client_for, headers, install, prompt  # noqa: F401
from test_controls_relay import registry, setup, publication, PID, HID, CANVAS, TAB  # noqa: F401
from server import session_registry as sr

@pytest.fixture(autouse=True)
def definitions(monkeypatch):
    monkeypatch.setattr(cd, 'comfy_class_lookup', CLASSES.get)
    monkeypatch.setattr(cd, '_comfy_class_lookup', CLASSES.get)

def effective(identity):
    graph, _ = st.load_template(identity)
    stored = st.read_control_values(identity)
    return graph, st.apply_stored_values(graph, st.describe_stored(graph, stored), stored)

def change(identity, changes):
    graph, view = effective(identity)
    cleaned, refused = cd.validate_binding_changes(view, changes, prompt=graph, structural='sdk')
    assert not refused, refused
    st.store_binding_changes(identity, cleaned, view, graph)

@pytest.mark.parametrize('value,valid', [('', True), ('😀'*1000, True), ('😀'*1001, False), (1, False)])
def test_description_cap_is_code_points(value, valid):
    text = json.dumps({'description': value})
    cleaned, rejected = cd.validate_binding_changes(describe(prompt()), {'workflow:description': value})
    assert bool(rejected) != valid
    if valid:
        assert cm.parse_manifest(text)['description'] == cleaned['workflow:description'] == value
    else:
        with pytest.raises(cm.ManifestError): cm.parse_manifest(text)

def test_description_storage_clear_replace_hash_and_summary(world):
    graph = prompt(); graph['8'] = _relay(description='Author')
    install(world, graph)
    # Real import validation populates the authored Launcher summary.
    validation = st.validate_template(graph, class_lookup=CLASSES.get)
    assert validation['description'] == 'Author'
    for description in ('**SDK** 😀', ''):
        change(world.template_id, {'workflow:description': description})
        assert effective(world.template_id)[1]['description'] == description
        assert next(t for t in st.list_templates() if t['template_id'] == world.template_id)['description'] == description
    stored = st.read_control_values(world.template_id)
    assert st._has_curation(stored)  # an empty overlay overrides authored text
    reset = st._normalized_controls(stored, 'different')
    assert 'description' not in reset
    assert 'description' in st.read_control_values(world.template_id)  # tolerant read never rewrites
    result = st.replace_template(world.template_id, json.dumps(graph).encode(), validate=lambda _: {'ok': True})
    assert result['description_reset'] is True
    assert effective(world.template_id)[1]['description'] == 'Author'

def test_description_http_and_mounted_forwarding(world, monkeypatch, registry):
    async def run():
        session = await world.open()
        async with client_for(world, monkeypatch) as client:
            url = f'/sonder-editor/sdk/session/{session.session_id}/controls'
            r = await client.put(url, headers=headers(session), json={'changes': {'workflow:description': '**Hi**'}})
            assert r.status == 200, await r.text()
            assert (await r.json())['view']['description'] == '**Hi**'
        await setup()
        body = publication(); body['view'] = describe(prompt())
        assert (await sr.publish_controls_view(PID, HID, CANVAS, body))['ok']
        queued = await sr.post_control_changes(PID, HID, TAB, {'workflow:description': 'Mounted'})
        assert queued['ok'], queued
        assert (await sr.get_control_changes(PID, HID, CANVAS))['pending'][0]['changes'] == {'workflow:description': 'Mounted'}
    asyncio.run(run())

def curated(world):
    graph = prompt()
    graph['6']['inputs']['cfg'] = 7.0
    graph['8'] = _relay({
        'steps': _binding('6', 'steps', label='Steps', group='Author'),
        'seed': _binding('6', 'seed', label='Seed', seed_mode='increment'),
        'advance': _binding('2', 'mark_queue_complete', label='Advance')},
        notes={'intro': {'text':'Author note', 'group':'Author'}, 'hidden': {'text':'Hide me'}},
        groups={'Author': {'order':0}}, description='Author', future='keep')
    for node in graph.values(): node['_meta'] = {'title':'Keep title', 'sonder_controls':{'v':1}}
    install(world, graph)
    def store(data):
        data['local'] = {
            'app': {'targets':[{'path':'6','input':'cfg','class_type':'KSampler'}], 'label':'App cfg','group':'App','advanced':True,'note':'Local note'},
            'shadow': {'targets':[{'path':'6','input':'steps','class_type':'KSampler'}]},
            'dupe1': {'targets':[{'path':'2','input':'place_audio_on_timeline','class_type':'SonderSaveVideo'}]},
            'dupe2': {'targets':[{'path':'2','input':'place_audio_on_timeline','class_type':'SonderSaveVideo'}]},
            'missing': {'targets':[{'path':'gone','input':'x','class_type':'KSampler'}]}}
        data['hidden'] = {'binding:steps':True, 'note:hidden':True}
        data['local_notes'] = {'appnote': {'text':'**Local**','group':'App','order':5}}
    st.update_control_values(world.template_id, store)
    change(world.template_id, {'binding:seed:value':'9007199254740995', 'binding:seed:seed_mode':'fixed',
        'binding:app:value':12.5, 'binding:seed:label':'Rebranded seed', 'binding:seed:note':'Edited note',
        'binding:seed:advanced':True, 'group:Author:rename':'Renamed', 'workflow:description':'# Curated 😀'})
    return graph

def test_export_round_trip_effective_menu_and_values(world):
    original = curated(world)
    source_before = open(st.template_source_path(world.template_id), 'rb').read()
    stored_before = st.read_control_values(world.template_id)
    raw = st.export_curated(world.template_id)
    graph = st.parse_template_bytes(raw); menu = cm.find_relay(graph)['manifest']
    assert set(menu['bindings']) == {'seed','advance','app'}
    assert set(menu['notes']) == {'intro','appnote'}
    assert menu['description'] == '# Curated 😀'
    assert menu['bindings']['app']['targets'] == [{'path':'6','input':'cfg'}]
    assert menu['bindings']['app']['advanced'] is True
    assert menu['bindings']['seed']['label'] == 'Rebranded seed'
    assert menu['bindings']['seed']['note'] == 'Edited note'
    assert menu['bindings']['seed']['seed_mode'] == 'fixed'
    assert menu['notes']['intro']['group'] == 'Renamed'
    assert 'Author' not in menu['groups'] and 'Renamed' in menu['groups']
    assert graph['6']['inputs']['seed'] == 9007199254740995
    assert graph['6']['inputs']['cfg'] == 12.5
    assert graph['6']['inputs']['steps'] == original['6']['inputs']['steps']
    exported_menu = json.loads(graph['8']['inputs']['manifest'])
    assert exported_menu['future'] == 'keep'
    assert all('status' not in b for b in exported_menu['bindings'].values())
    assert not {'status','suggestions','error'} & exported_menu.keys()
    assert all(n['_meta'] == {'title':'Keep title'} for n in graph.values())
    imported = st.import_template(raw, name='Round trip')
    after = effective(imported['template_id'])[1]; before = effective(world.template_id)[1]
    assert after['description'] == before['description']
    assert after['group_order'] == before['group_order']
    for binding in after['bindings']:
        prior = next(b for b in before['bindings'] if b['binding_id'] == binding['binding_id'])
        for field in ('label','group','order','advanced','note','seed_mode','value'):
            assert binding.get(field) == prior.get(field), field
    assert open(st.template_source_path(world.template_id), 'rb').read() == source_before
    assert st.read_control_values(world.template_id) == stored_before

@pytest.mark.parametrize('keys, expected', [(['6','19:900'], '7'), (['x','2:900'], '1'), (['1','01','99'], '100')])
def test_export_inserts_relay_using_root_numeric_keys(world, keys, expected):
    graph = {key: _node('Unknown', {}) for key in keys}
    install(world, graph)
    change(world.template_id, {'workflow:description':'Description only'})
    exported = st.parse_template_bytes(st.export_curated(world.template_id))
    relay = cm.find_relay(exported)
    assert relay['key'] == expected and relay['manifest']['description'] == 'Description only'

def test_export_refuses_relay_error_and_size_caps(world, monkeypatch):
    graph = prompt(); graph['8:9'] = graph.pop('8'); install(world, graph)
    with pytest.raises(st.TemplateError, match='subgraph'): st.export_curated(world.template_id)
    graph = curated(world)
    change(world.template_id, {'binding:seed:note': 'é' * 2000})
    monkeypatch.setattr(cm, 'MAX_MANIFEST_BYTES', 2000)  # source fits, curated UTF-8 union does not
    with pytest.raises(st.TemplateError, match='large'): st.export_curated(world.template_id)
    monkeypatch.setattr(cm, 'MAX_MANIFEST_BYTES', 128*1024)
    source_size = len(open(st.template_source_path(world.template_id), 'rb').read())
    monkeypatch.setattr(st, 'MAX_TEMPLATE_BYTES', source_size+10)
    with pytest.raises(st.TemplateError, match='large'): st.export_curated(world.template_id)

def test_export_skips_full_validation_and_supports_starters(world, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError('Full validation must not run')
    monkeypatch.setattr(st, 'validate_template', forbidden)
    monkeypatch.setattr(cd, 'comfy_class_lookup', lambda _: None)
    monkeypatch.setattr(cd, '_comfy_class_lookup', lambda _: None)
    path = st.template_source_path(world.template_id)
    monkeypatch.setattr(st, 'starter_sources', lambda: {world.template_id:path})
    assert cm.find_relay(st.parse_template_bytes(st.export_curated(world.template_id)))['status'] == 'ok'

def test_export_holds_file_lock_across_snapshot(world, monkeypatch):
    original = st.read_control_values; observed = []
    def read(identity):
        def contender():
            lock = st.template_lock(identity)
            got = lock.acquire(blocking=False)
            observed.append(got)
            if got: lock.release()
        thread = threading.Thread(target=contender); thread.start(); thread.join(timeout=2)
        return original(identity)
    monkeypatch.setattr(st, 'read_control_values', read)
    st.export_curated(world.template_id)
    assert observed == [False]

def test_export_get_origin_and_no_runner_lock(world, monkeypatch):
    async def run():
        async with client_for(world, monkeypatch) as client:
            path = f'/sonder-editor/sdk/templates/{world.template_id}/export'
            async with world.runner.template_lock(world.template_id):
                response = await asyncio.wait_for(client.get(path, headers={'Origin': str(client.make_url('/')).rstrip('/')}), 3)
                assert response.status == 200, await response.text()
                assert 'Content-Disposition' not in response.headers
                assert response.headers['Cache-Control'] == 'no-store'
                assert st.parse_template_bytes(await response.read())
            response = await client.get(path, headers={'Origin':'https://evil.invalid'})
            assert response.status == 403
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['no_widget', 'duplicate'])
def test_export_preserves_off_authored_bindings_and_targets(world, kind):
    graph = prompt()
    entries = {'a': _binding('6','seed',status=kind if kind=='no_widget' else 'ok',seed_mode='increment')}
    if kind == 'duplicate': entries['b'] = _binding('6','seed',seed_mode='increment')
    graph['8'] = _relay(entries); install(world, graph)
    if kind == 'duplicate': st.update_control_values(world.template_id, lambda d: d['hidden'].update({'binding:b':True}))
    before = effective(world.template_id)[1]
    after = describe(st.parse_template_bytes(st.export_curated(world.template_id)))
    assert after['bindings'][0]['status'] == before['bindings'][0]['status'] == kind
    assert after['bindings'][0]['targets'][0]['path'] == '6'
    assert after['bindings'][0]['targets'][0]['input'] == 'seed'

@pytest.mark.parametrize('local', [False,True])
def test_export_keeps_derived_labels_even_past_authored_label_cap(world, local):
    graph = prompt(); graph['6']['_meta'] = {'title':'Very long title '*30}
    graph['8'] = _relay({} if local else {'a': _binding('6','steps')})
    install(world, graph)
    if local: st.update_control_values(world.template_id, lambda d: d['local'].update({'a':{'targets':[{'path':'6','input':'steps','class_type':'KSampler'}], 'label':''}}))
    before = effective(world.template_id)[1]
    after = describe(st.parse_template_bytes(st.export_curated(world.template_id)))
    assert len(after['bindings'][0]['label']) > cm.LABEL_MAX
    assert after['bindings'][0]['label'] == before['bindings'][0]['label']

def test_export_accepts_whitespace_manifest(world):
    graph = prompt(); graph['8']['inputs']['manifest'] = ' \n\t '
    install(world, graph)
    assert cm.find_relay(st.parse_template_bytes(st.export_curated(world.template_id)))['status'] == 'ok'
