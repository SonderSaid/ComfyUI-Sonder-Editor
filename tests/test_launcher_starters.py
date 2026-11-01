"""Bundled source, install-level overlays, and owner-gated departure cancellation."""
import asyncio
import hashlib
import json
import sys
import types
from pathlib import Path

import pytest

from server import sdk_templates as st
from server.sdk_runner import SdkError
from test_sdk_runner import World, PROJECT, template_prompt


@pytest.fixture
def starter(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'folder_paths', types.SimpleNamespace(get_user_directory=lambda: str(tmp_path / 'user')))
    folder = tmp_path / 'pack'; folder.mkdir()
    source = folder / 'sample.json'; source.write_text(json.dumps(template_prompt()), encoding='utf-8')
    (folder / 'sample.meta.json').write_text(json.dumps({'name': 'Starter', 'model_family': 'Test', 'order': 1}), encoding='utf-8')
    monkeypatch.setattr(st, 'STARTERS_DIR', str(folder))
    monkeypatch.setattr(st, 'validate_template', lambda p: {'ok': True, 'errors': [], 'blocking': [], 'warnings': []})
    st._starter_validation_cache.clear()
    return hashlib.sha256(b'sonder-starter:sample').hexdigest()[:16], source


def test_source_and_sidecars_stay_separate_and_pack_update_resets_presentation(starter):
    identity, source = starter
    original = source.read_bytes()
    assert st.list_templates()[0]['starter']
    assert st.load_template(identity)[1]['validation']['ok']
    st.update_control_values(identity, lambda data: data.update(notes={'note:x': 'text'}, layout={'note:x': {'order': 1}}))
    assert st.read_control_values(identity)['notes']
    assert source.read_bytes() == original
    assert sorted(p.name for p in source.parent.iterdir()) == ['sample.json', 'sample.meta.json']
    source.write_bytes(original + b'\n')
    assert not st.read_control_values(identity)['notes']
    assert not st.read_control_values(identity)['layout']


@pytest.mark.parametrize('action', ['rename', 'replace', 'delete'])
def test_starter_mutations_refuse_before_other_validation(starter, action):
    identity, source = starter
    before = source.read_bytes()
    with pytest.raises(st.TemplateError) as exc:
        if action == 'rename': st.rename_template(identity, 'new')
        elif action == 'replace': st.replace_template(identity, b'not JSON')
        else: st.delete_template(identity)
    assert (exc.value.code, exc.value.status) == ('starter_read_only', 409)
    assert source.read_bytes() == before


def test_starter_opens_controls_reads_changes_seed_advances_and_generates(starter):
    identity, source = starter
    before = source.read_bytes(); world = World(); world.template_id = identity
    async def scenario():
        session = await world.open()
        assert session.template_id == identity
        view = world.runner._controls_snapshot(session)
        assert view
        st.store_binding_changes(identity, {'binding:abcd1234:value': 27, 'binding:abcd1234:seed_mode': 'increment'},
                                 view['view'])
        result = await world.runner.generate(session, tab_session_id="tab-1", count=2)
        assert len(result['run_ids']) == 2
        assert [p['5']['inputs']['seed'] for p in world.client.submitted] == [27, 28]
        assert st.read_control_values(identity)['bindings']['abcd1234']['value'] == 29
    try: asyncio.run(scenario())
    finally: world.runner.close()
    assert source.read_bytes() == before


def test_starters_do_not_use_the_import_quota(starter, monkeypatch):
    monkeypatch.setattr(st, 'MAX_TEMPLATES', 1)
    st.import_template(json.dumps(template_prompt()).encode(), validate=lambda prompt: {'ok': True})
    assert len(st.list_templates()) == 2
    with pytest.raises(st.TemplateError) as exc:
        st.import_template(json.dumps(template_prompt()).encode(), validate=lambda prompt: {'ok': True})
    assert exc.value.code == 'too_many_templates'


def test_secret_cancel_refuses_live_owner_and_wrong_secret_then_cancels_after_departure(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'folder_paths', types.SimpleNamespace(get_user_directory=lambda: str(tmp_path)))
    world = World()
    async def scenario():
        session = await world.open(); await world.runner.generate(session, tab_session_id="tab-1")
        with pytest.raises(SdkError) as exc:
            await world.runner.cancel_by_secret(session.session_id, session.secret)
        assert exc.value.code == 'not_owner' and not session.runs[0].cancel_requested
        world.owners.clear()
        with pytest.raises(SdkError): await world.runner.cancel_by_secret(session.session_id, 'wrong')
        assert not session.runs[0].cancel_requested
        assert (await world.runner.cancel_by_secret(session.session_id, session.secret))['canceling']
        assert session.runs[0].cancel_requested
    try: asyncio.run(scenario())
    finally: world.runner.close()


def _set_initial_modes(source, modes):
    path = source.with_suffix('.meta.json')
    meta = json.loads(path.read_text(encoding='utf-8'))
    meta['initial_node_modes'] = modes
    path.write_text(json.dumps(meta), encoding='utf-8')


def test_starter_initial_modes_apply_once_and_never_reenable_after_user_change(starter):
    identity, source = starter
    original = source.read_bytes()
    defaults = {'5': {'mode': 'muted', 'class_type': 'Sampler'}}
    _set_initial_modes(source, defaults)
    assert st.read_control_values(identity)['node_modes'] == defaults
    st.update_control_values(identity, lambda data: data['node_modes'].pop('5'))
    assert st.read_control_values(identity)['node_modes'] == {}
    source.write_bytes(original + b'\n')
    assert st.read_control_values(identity)['node_modes'] == {}
    assert sorted(p.name for p in source.parent.iterdir()) == ['sample.json', 'sample.meta.json']


def test_starter_initial_modes_preserve_existing_user_sidecar(starter):
    identity, source = starter
    st.update_control_values(identity, lambda data: data['bindings'].update({'seed': {'value': 99}}))
    _set_initial_modes(source, {'5': {'mode': 'muted', 'class_type': 'Sampler'}})
    values = st.read_control_values(identity)
    assert values['node_modes'] == {}
    assert values['bindings']['seed']['value'] == 99


def test_starter_initial_modes_apply_before_first_setting_write(starter):
    identity, source = starter
    defaults = {'5': {'mode': 'bypassed', 'class_type': 'Sampler'}}
    _set_initial_modes(source, defaults)
    st.update_control_values(identity, lambda data: data['notes'].update({'note:test': 'User note'}))
    values = st.read_control_values(identity)
    assert values['node_modes'] == defaults
    assert values['notes']['note:test'] == 'User note'


def test_starter_initial_modes_ignore_invalid_modes_and_unrelated_metadata(starter):
    identity, source = starter
    _set_initial_modes(source, {
        '5': {'mode': 'muted', 'class_type': 'Sampler'},
        '6': {'mode': 'invalid', 'class_type': 'Sampler'},
        '7:8': {'mode': 'muted', 'class_type': 'Sampler'},
        '9': {'mode': 'muted', 'class_type': 123},
        'bindings': {'seed': {'value': 99}},
    })
    values = st.read_control_values(identity)
    assert values['node_modes'] == {'5': {'mode': 'muted', 'class_type': 'Sampler'}}
    assert values['bindings'] == {}


def test_starter_initial_modes_record_each_switchs_shipped_members(starter):
    identity, source = starter
    prompt = json.loads(source.read_text(encoding='utf-8'))
    controls = {'pass': {'members': [{'path': '5', 'class_type': 'KSampler'}]},
                'other': {'members': [{'path': '2', 'class_type': 'SonderSaveVideo'}, 'bad']}}
    prompt['8'] = {'class_type': 'SonderControlRelay',
                   'inputs': {'manifest': json.dumps({'v': 1, 'bindings': {}, 'execution': {'v': 1, 'controls': controls}})}}
    source.write_text(json.dumps(prompt), encoding='utf-8')
    _set_initial_modes(source, {'5': {'mode': 'muted', 'class_type': 'KSampler'}})
    assert st.read_control_values(identity)['switch_members'] == {'pass': ['5'], 'other': ['2']}


def test_a_record_less_sidecar_reads_the_starters_previous_membership(starter):
    identity, source = starter
    path = source.with_suffix('.meta.json')
    meta = json.loads(path.read_text(encoding='utf-8'))
    meta['previous_switch_members'] = {'pass': ['5'], 'bad': 'x'}
    path.write_text(json.dumps(meta), encoding='utf-8')
    controls = Path(st._template_paths(identity)[2])
    controls.parent.mkdir(parents=True, exist_ok=True)
    controls.write_text(json.dumps({'node_modes': {'5': {'mode': 'muted', 'class_type': 'KSampler'}}}), encoding='utf-8')
    assert st.read_control_values(identity)['switch_members'] == {'pass': ['5']}
    # A sidecar that already records membership keeps its own.
    controls.write_text(json.dumps({'switch_members': {'pass': ['5', '6']}}), encoding='utf-8')
    assert st.read_control_values(identity)['switch_members'] == {'pass': ['5', '6']}


def test_refine_starter_upgraded_from_0_8_0_defaults_keeps_its_mask_group_off(tmp_path, monkeypatch):
    from server import control_execution as ce, control_manifest as cm, control_routing as cr
    monkeypatch.setitem(sys.modules, 'folder_paths', types.SimpleNamespace(get_user_directory=lambda: str(tmp_path)))
    monkeypatch.setattr(st, 'STARTERS_DIR', str(Path(st.__file__).resolve().parents[1] / 'sdk_starters'))
    slug = 'sonder_ltx_2_5_refine_upscale'
    identity = hashlib.sha256(b'sonder-starter:' + slug.encode()).hexdigest()[:16]
    assert identity in st.starter_sources()
    # The sidecar 0.8.0 seeded: its Mask Off defaults, with no membership record.
    controls = Path(st._template_paths(identity)[2])
    controls.parent.mkdir(parents=True, exist_ok=True)
    old = {path: {'mode': 'muted', 'class_type': cls} for path, cls in [
        ('9462', 'LTXVPreprocessMasks'), ('9508', 'MaskToImage'), ('9509', 'SonderPreviewVideo'),
        ('9510', 'SonderDriverBridge'), ('9511', 'SonderDriverSelector'), ('9512', 'ImageToMask')]}
    controls.write_text(json.dumps({'node_modes': old}), encoding='utf-8')
    stored = st.read_control_values(identity)
    prompt = st.parse_template_bytes(Path(st.starter_sources()[identity]).read_bytes())
    menu = json.loads(prompt[cm.find_relay(prompt)['key']]['inputs']['manifest'])
    view = {'bindings': [], 'execution': [
        {'execution_id': cid, 'status': 'ok', 'members': [{**m, 'mode': 'enabled'} for m in entry['members']]}
        for cid, entry in menu['execution']['controls'].items()]}
    effective = ce.apply_sdk_state(view, prompt, stored, set())
    assert {d['execution_id']: d['mode'] for d in view['execution']}['KAl11_kzAAT_'] == 'muted'
    compiled = cr.compile_modes(prompt, view['_routing'], effective)
    assert '9579' not in compiled and 'spatial_mask' not in compiled['9576']['inputs']


def test_starter_without_initial_modes_does_not_create_controls_sidecar(starter):
    identity, _ = starter
    st.read_control_values(identity)
    assert not Path(st._template_paths(identity)[2]).exists()


@pytest.mark.parametrize('slug, choices', [
    ('sonder_ltx_2_5_edit_vfx', ['Make a New Video', 'Generate New Audio', 'Standard']),
    ('sonder_ltx_2_5_edit_vfx_standard', ['Make a New Video', 'Generate New Audio']),
    ('sonder_ltx_2_5_refine_upscale', ['Refining Pass Only', 'Mask Off']),
    ('sonder_minimax_h3_references_swaps', ['Standard Generation']),
])
def test_shipped_starter_sources_and_initial_choices(slug, choices):
    from server import control_manifest as cm, prompt_graph

    folder = Path(st.__file__).resolve().parents[1] / 'sdk_starters'
    graph = st.parse_template_bytes((folder / (slug + '.json')).read_bytes())
    meta = json.loads((folder / (slug + '.meta.json')).read_text(encoding='utf-8'))
    assert meta['name'] and meta['model_family'] and meta['order'] > 0
    editors = prompt_graph.editor_keys(graph)
    assert len(editors) == 1
    saves = prompt_graph.nodes_reaching(graph, editors[0], prompt_graph.SONDER_SAVE_CLASS_TYPES)
    assert saves and all(graph[key]['inputs']['mark_queue_complete'] is False for key in saves)
    relay = cm.find_relay(graph)
    assert relay['status'] == 'ok'
    menu = json.loads(graph[relay['key']]['inputs']['manifest'])
    assert menu['views']['items']
    assert all(entry.get('status', 'ok') == 'ok' for entry in menu['bindings'].values())
    effects = {}
    for name in choices:
        preset = next(p for p in menu['presets'].values() if p['name'] == name)
        effects.update(cm.execution_effects(preset.get('execution', {})))
        for identity, member in preset['members'].items():
            for target in menu['bindings'][identity]['targets']:
                assert graph[target['path']]['inputs'][target['input']] == member['value']
    expected = {path: mode for path, mode in effects.items() if mode != 'enabled'}
    assert {path: entry['mode'] for path, entry in meta['initial_node_modes'].items()} == expected
    members = {member['path']: member['class_type']
               for switch in menu['execution']['controls'].values() for member in switch['members']}
    assert all(entry['class_type'] == members[path] for path, entry in meta['initial_node_modes'].items())
    seeds = [node['inputs']['seed'] for node in graph.values() if node['class_type'] in ('SeedNode', 'SeedGenerator')]
    assert seeds and set(seeds) == {0}
    assert {entry['seed_mode'] for entry in menu['bindings'].values() if entry.get('seed_mode')} == {'fixed'}


def test_standard_edit_starter_removes_relay_dependency_without_changing_other_tasks():
    from server import control_manifest as cm, control_routing

    folder = Path(st.__file__).resolve().parents[1] / 'sdk_starters'
    full = json.loads((folder / 'sonder_ltx_2_5_edit_vfx.json').read_text(encoding='utf-8'))
    simple = json.loads((folder / 'sonder_ltx_2_5_edit_vfx_standard.json').read_text(encoding='utf-8'))
    assert 'promptrelay' not in json.dumps(simple).lower()
    assert 'prompt relay' not in json.dumps(simple).lower()
    assert 'PromptRelaySmartEncode' in {n['class_type'] for n in full.values()}
    relay = cm.find_relay(full)['key']
    full_menu = json.loads(full[relay]['inputs']['manifest'])
    menu = json.loads(simple[relay]['inputs']['manifest'])
    removed = {'3384', '3385', '3391', '3392', '3396'}
    assert set(simple) == set(full) - removed
    assert full['3392']['inputs']['choice'] == 'Normal Conditioning'
    assert full['3392']['inputs']['choice_list'].splitlines()[0] == 'Normal Conditioning'
    expected_links = {
        ('3035', 'iclora_model'): full['3391']['inputs']['b0_l1'],
        ('3064', 'positive'): full['3391']['inputs']['b0_l0'],
        ('3386', 'model'): full['3391']['inputs']['b0_l1'],
        ('3348', 'string_b'): full['3396']['inputs']['branches.item0'],
    }
    for key, node in simple.items():
        if key == relay:
            continue
        expected = json.loads(json.dumps(full[key]))
        for (consumer, field), value in expected_links.items():
            if consumer == key:
                expected['inputs'][field] = value
        assert node == expected
    assert menu['preset_categories'] == {k: v for k, v in full_menu['preset_categories'].items()
                                         if k != 'edit_vfx_prompt_method'}
    assert menu['presets'] == {k: v for k, v in full_menu['presets'].items()
                              if k not in ('edit_vfx_prompt_standard', 'edit_vfx_prompt_timed')}
    assert menu['bindings'] == {k: v for k, v in full_menu['bindings'].items() if k != '-a4xOoh01dto'}
    assert menu['views'] == full_menu['views']
    routing, error = control_routing.read_routing(menu['execution']['routing'])
    assert error is None
    assert not removed.intersection(routing['nodes'])
    for preset in menu['presets'].values():
        assert set(preset['members']) <= set(menu['bindings'])
        assert set(preset.get('execution', {})) <= set(menu['execution']['controls'])
    for binding in menu['bindings'].values():
        assert all(t['path'] in simple for t in binding['targets'])
    assert all(v['path'] in simple for v in menu['views']['items'].values())
    assert all(m['path'] in simple for s in menu['execution']['controls'].values() for m in s['members'])
    meta = json.loads((folder / 'sonder_ltx_2_5_edit_vfx.meta.json').read_text(encoding='utf-8'))
    assert meta['name'].endswith('Prompt Relay')
