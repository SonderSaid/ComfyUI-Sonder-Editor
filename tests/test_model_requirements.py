"""Literal model checks and effective-prompt admission, not filename guessing."""
import asyncio
import sys
import types

import pytest

from server import control_descriptors as cd, model_requirements as mr, sdk_templates
from server.sdk_runner import SdkError
from test_sdk_runner import World, PROJECT, template_prompt


def loader(options, combo=False):
    class Loader:
        @classmethod
        def INPUT_TYPES(cls):
            return {"required": {"ckpt_name": ("COMBO", {"options": options}) if combo else (options,)}}
    return Loader


def prompt(value, name="ckpt_name", class_type="Loader"):
    return {"3": {"class_type": class_type, "inputs": {name: value}, "_meta": {"title": "My loader"}}}


@pytest.mark.parametrize("combo", [False, True])
def test_combo_shapes_empty_folder_and_separator_normalization(combo):
    cls = loader([r"nested\good.safetensors"], combo)
    assert mr.missing_models(prompt("nested/good.safetensors"), lambda _: cls) == []
    missing = mr.missing_models(prompt("missing.gguf"), lambda _: cls)
    assert missing == [{"file": "missing.gguf", "folder": "checkpoints", "node": "My loader", "node_id": "3", "input": "ckpt_name"}]
    assert mr.missing_models(prompt("absent.pt"), lambda _: loader([], combo))


@pytest.mark.parametrize("value", ["None", "preset", ["2", 0], None])
def test_links_and_non_model_values_are_not_requirements(value):
    assert mr.missing_models(prompt(value), lambda _: loader([])) == []


def test_custom_validator_owns_its_input():
    cls = loader([])
    cls.VALIDATE_INPUTS = staticmethod(lambda ckpt_name: True)
    assert mr.missing_models(prompt("custom.bin"), lambda _: cls) == []


def test_unknown_folder_requires_unique_nonempty_filename_list(monkeypatch):
    class Other:
        INPUT_TYPES = staticmethod(lambda: {"required": {"model": (["a.pt"],)}})
    fp = types.SimpleNamespace(folder_names_and_paths={"one": (), "two": ()}, get_filename_list=lambda key: ["a.pt"])
    monkeypatch.setitem(sys.modules, "folder_paths", fp)
    assert mr.missing_models(prompt("b.pt", "model"), lambda _: Other)[0]["folder"] == ""
    fp.get_filename_list = lambda key: ["a.pt"] if key == "one" else []
    assert mr.missing_models(prompt("b.pt", "model"), lambda _: Other)[0]["folder"] == "one"


def test_dynamic_v3_finalized_schema_is_used(monkeypatch):
    # Exercise the real V3 path without requiring the whole Comfy runtime.
    class Internal: pass
    class V3(Internal):
        INPUT_TYPES = staticmethod(lambda: {"required": {}})
    internal = types.ModuleType("comfy_api.internal")
    internal._ComfyNodeInternal = Internal
    internal.first_real_override = lambda cls, name: None
    io = types.SimpleNamespace(get_finalized_class_inputs=lambda schema, values: ({"required": {"ckpt_name": ("COMBO", {"options": ["ok.sft"]})}}, {}, {}))
    monkeypatch.setitem(sys.modules, "comfy_api.internal", internal)
    monkeypatch.setitem(sys.modules, "comfy_api.latest", types.SimpleNamespace(_io=io))
    assert mr.missing_models(prompt("no.sft"), lambda _: V3)
    assert not mr.missing_models(prompt("ok.sft"), lambda _: V3)


def test_changed_option_list_invalidates_cached_absence():
    options = []
    cls = loader(options)
    assert mr.missing_models(prompt("new.pth"), lambda _: cls)
    options.append("new.pth")
    assert not mr.missing_models(prompt("new.pth"), lambda _: cls)


def test_generate_checks_effective_prompt_and_fixed_override_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(get_user_directory=lambda: str(tmp_path)))
    cls = loader(["installed.ckpt"])
    output = type('Output', (), {'OUTPUT_NODE': True})
    monkeypatch.setattr(cd, "comfy_class_lookup", lambda name: cls if name == "Loader" else output if name == 'SonderSaveVideo' else None)
    world = World()
    p = template_prompt(); p.update(prompt("missing.ckpt")); p['2']['inputs']['model'] = ['3', 0]
    sdk_templates.replace_template(world.template_id, __import__('json').dumps(p).encode(), validate=lambda p: {"ok": True})
    world.bindings = [{"binding_id": "ckpt", "label": "Checkpoint", "kind": "COMBO", "status": "ok", "error": None,
                       "value": "missing.ckpt", "options": ["installed.ckpt"],
                       "targets": [{"path": "3", "input": "ckpt_name", "class_type": "Loader", "status": "ok"}]}]
    sig = {"class_type": "Loader", "input": "ckpt_name", "kind": "COMBO"}
    async def scenario():
        session = await world.open()
        with pytest.raises(SdkError) as exc:
            await world.runner.generate(session, tab_session_id="tab-1")
        assert "missing.ckpt" in str(exc.value.details)
        assert not world.client.submitted
        sdk_templates.update_control_values(world.template_id, lambda data: data['bindings'].update(
            {'ckpt': {'value': 'installed.ckpt', 'sig': sig}}))
        result = await world.runner.generate(session, tab_session_id="tab-1")
        assert result['run_ids'] and world.client.submitted[0]['3']['inputs']['ckpt_name'] == 'installed.ckpt'
    try: asyncio.run(scenario())
    finally: world.runner.close()


def test_disconnected_missing_model_does_not_refuse_executable_graph():
    cls = loader([])
    output = type('Output', (), {'OUTPUT_NODE': True})
    graph = prompt('missing.ckpt')
    graph['4'] = {'class_type': 'Output', 'inputs': {}}
    lookup = lambda name: output if name == 'Output' else cls
    assert not mr.missing_models(graph, lookup, executable_only=True)
    graph['4']['inputs']['model'] = ['3', 0]
    assert mr.missing_models(graph, lookup, executable_only=True)


def test_launcher_checks_stored_binding_values(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'folder_paths', types.SimpleNamespace(get_user_directory=lambda: str(tmp_path)))
    p = prompt('missing.ckpt')
    meta = sdk_templates.import_template(__import__('json').dumps(p).encode(), validate=lambda p: {'ok': True})
    monkeypatch.setattr(cd, 'comfy_class_lookup', lambda name: loader(['installed.ckpt']))
    binding = {'binding_id': 'ckpt', 'kind': 'COMBO', 'status': 'ok', 'options': ['installed.ckpt'],
               'targets': [{'path': '3', 'input': 'ckpt_name', 'class_type': 'Loader', 'status': 'ok'}]}
    monkeypatch.setattr(cd, 'describe_bindings', lambda p: {'relay': {'status': 'ok'}, 'bindings': [binding], 'notes': []})
    assert sdk_templates.list_templates(requirements=True)[0]['missing_models']
    assert sdk_templates.template_requirements(meta['template_id'])['missing_models']
    sig = {'class_type': 'Loader', 'input': 'ckpt_name', 'kind': 'COMBO'}
    sdk_templates.update_control_values(meta['template_id'], lambda data: data['bindings'].update(
        {'ckpt': {'value': 'installed.ckpt', 'sig': sig}}))
    assert not sdk_templates.list_templates(requirements=True)[0]['missing_models']
    assert not sdk_templates.template_requirements(meta['template_id'])['missing_models']
