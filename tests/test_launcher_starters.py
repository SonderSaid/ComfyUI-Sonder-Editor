"""Bundled source, install-level overlays, and owner-gated departure cancellation."""
import asyncio
import hashlib
import json
import sys
import types

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
