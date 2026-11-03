"""Literal model checks and effective-prompt admission, not filename guessing."""
import asyncio
import os
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
    assert missing == [{"file": "missing.gguf", "folder": "checkpoints", "relative": "models/checkpoints/missing.gguf",
                        "node": "My loader", "node_id": "3", "input": "ckpt_name"}]
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
    unknown = mr.missing_models(prompt("b.pt", "model"), lambda _: Other)[0]
    assert unknown["folder"] is None and unknown["relative"] is None
    assert mr.missing_model_message(unknown) == "b.pt is missing (folder unknown: My loader · model)"
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
    p['4'] = {'class_type': 'Out', 'inputs': {'model': ['3', 0]}}  # the judge counts only what an output needs
    meta = sdk_templates.import_template(__import__('json').dumps(p).encode(), validate=lambda p: {'ok': True})
    output = type('Out', (), {'OUTPUT_NODE': True, 'INPUT_TYPES': classmethod(lambda cls: {'required': {}})})
    monkeypatch.setattr(cd, 'comfy_class_lookup', lambda name: output if name == 'Out' else loader(['installed.ckpt']))
    binding = {'binding_id': 'ckpt', 'kind': 'COMBO', 'status': 'ok', 'options': ['installed.ckpt'],
               'targets': [{'path': '3', 'input': 'ckpt_name', 'class_type': 'Loader', 'status': 'ok'}]}
    monkeypatch.setattr(cd, 'describe_bindings', lambda p, **kw: {'relay': {'status': 'ok'}, 'bindings': [binding], 'notes': []})
    assert sdk_templates.check_template(meta['template_id'])['missing_models']
    sig = {'class_type': 'Loader', 'input': 'ckpt_name', 'kind': 'COMBO'}
    sdk_templates.update_control_values(meta['template_id'], lambda data: data['bindings'].update(
        {'ckpt': {'value': 'installed.ckpt', 'sig': sig}}))
    assert not sdk_templates.check_template(meta['template_id'])['missing_models']


def test_a_loader_its_input_name_cannot_place_uses_the_class_map():
    class MoGe:
        INPUT_TYPES = staticmethod(lambda: {"required": {"model_name": ([],)}})  # a fresh install lists nothing
    item = mr.missing_models(prompt("moge_2_vitl_normal_fp16.safetensors", "model_name", "LoadMoGeModel"),
                             lambda _: MoGe)[0]
    assert item["folder"] == "geometry_estimation"
    assert item["relative"] == "models/geometry_estimation/moge_2_vitl_normal_fp16.safetensors"


def test_subfolders_are_kept_and_the_category_named_folder_is_preferred(monkeypatch):
    class Unet:
        INPUT_TYPES = staticmethod(lambda: {"required": {"unet_name": ([],)}})
    item = mr.missing_models(prompt(r"LTX\IC Lora\edit.safetensors", "unet_name"), lambda _: Unet)[0]
    assert item["relative"] == "models/diffusion_models/LTX/IC Lora/edit.safetensors"
    assert mr.missing_model_message(item) == "edit.safetensors is missing: put it at models/diffusion_models/LTX/IC Lora/edit.safetensors (My loader)"
    folders = {"diffusion_models": ["/c/models/unet", "/c/models/diffusion_models"], "loras": ["/x/shared/lora-pool"]}
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(get_folder_paths=lambda key: folders.get(key, [])))
    assert mr.target_path(item) == os.path.normpath("/c/models/diffusion_models/LTX/IC Lora/edit.safetensors")
    # No category-named folder: the first one ComfyUI searches.
    assert mr.target_path({**item, "folder": "loras"}) == os.path.normpath("/x/shared/lora-pool/LTX/IC Lora/edit.safetensors")
    assert mr.target_path({**item, "folder": None}) is None
    assert mr.target_path({**item, "folder": "vae"}) is None


@pytest.mark.parametrize("remote, headers, absolute", [
    ("127.0.0.1", {}, True),
    ("127.0.0.1", {"Origin": "http://evil.example"}, False),
    ("192.168.1.20", {}, False),
])
def test_the_route_shows_absolute_paths_only_to_a_same_origin_page_on_this_machine(tmp_path, monkeypatch, remote, headers, absolute):
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    from server import sdk_routes
    monkeypatch.setattr(sdk_templates, "check_template", lambda template_id: {
        "ok": True, "template_id": template_id,
        "missing_models": [{"file": "a.safetensors", "folder": "vae", "relative": "models/vae/a.safetensors",
                            "node": "VAE", "node_id": "1", "input": "vae_name"}]})
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(
        get_user_directory=lambda: str(tmp_path), get_folder_paths=lambda key: ["/c/models/vae"]))
    routes = web.RouteTableDef()
    sdk_routes.register_sdk_routes(routes, resolve_project=None, remember_event_loop=lambda: None)
    handler = next(r.handler for r in routes if getattr(r, "path", "").endswith("/requirements"))
    request = make_mocked_request("GET", "/sonder-editor/sdk/templates/0123456789abcdef/requirements",
                                  headers={"Host": "127.0.0.1:8188", **headers},
                                  match_info={"template_id": "0123456789abcdef"})
    monkeypatch.setattr(type(request), "remote", property(lambda self: remote))
    body = __import__("json").loads(asyncio.run(handler(request)).body)
    item = body["missing_models"][0]
    assert ("path" in item) is absolute
    if absolute:
        assert item["path"] == os.path.normpath("/c/models/vae/a.safetensors")


@pytest.mark.parametrize("class_type, name, folder", [
    ("CLIPVisionLoader", "clip_name", "clip_vision"),  # not text_encoders, despite clip_name
    ("LatentUpscaleModelLoader", "model_name", "latent_upscale_models"),  # not upscale_models
])
def test_loaders_whose_input_name_misleads_use_their_own_folder(class_type, name, folder):
    class Loader:
        INPUT_TYPES = staticmethod(lambda: {"required": {name: ([],)}})
    assert mr.missing_models(prompt("x.safetensors", name, class_type), lambda _: Loader)[0]["folder"] == folder


def test_the_folder_listing_exactly_the_options_beats_the_input_name(monkeypatch):
    class Loader:
        INPUT_TYPES = staticmethod(lambda: {"required": {"clip_name": (["a.safetensors"],)}})
    fp = types.SimpleNamespace(folder_names_and_paths={"text_encoders": (), "clip_vision": ()},
                               get_filename_list=lambda key: ["a.safetensors"] if key == "clip_vision" else [])
    monkeypatch.setitem(sys.modules, "folder_paths", fp)
    assert mr.missing_models(prompt("b.safetensors", "clip_name", "SomeVisionLoader"), lambda _: Loader)[0]["folder"] == "clip_vision"


def test_a_native_lora_row_naming_no_file_is_a_requirement_with_its_own_message():
    # The App starters' switched-off "None" row is cut before a run; when a run
    # still carries it (turned on, or preparation fell back) it must refuse by name.
    class Stack:
        @classmethod
        def INPUT_TYPES(cls):
            return {"required": {"loras.0.lora_name": (["a.safetensors"],)}}

    graph = {"3": {"class_type": "LoadLoraModel", "inputs": {"loras.0.lora_name": "None"}, "_meta": {"title": "LoRAs"}}}
    missing = mr.missing_models(graph, lambda _: Stack)
    assert [m["file"] for m in missing] == ["None"]
    assert mr.missing_model_message(missing[0]).startswith("A LoRA row in LoRAs reads “None”, not an installed file.")
    other = {"3": {"class_type": "Loader", "inputs": {"ckpt_name": "None"}}}
    assert mr.missing_models(other, lambda _: loader([])) == []
