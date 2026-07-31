import asyncio
import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "video_editor_testpkg"


class _Input:
    kind = "input"

    def __init__(self, id=None, **kwargs):
        self.id = id
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Output(_Input):
    kind = "output"


class _AnyInput(_Input):
    kind = "any"


class _StringInput(_Input):
    kind = "string"


class _AutogrowTemplate:
    def __init__(self, input, prefix, min=1, max=10):
        self.input = input
        self.prefix = prefix
        self.min = min
        self.max = max


class _AutogrowInput(_Input):
    kind = "autogrow"

    def __init__(self, id, template, **kwargs):
        super().__init__(id, template=template, **kwargs)


class _Autogrow:
    Type = dict
    TemplatePrefix = _AutogrowTemplate
    Input = _AutogrowInput


class _CustomType:
    Input = _Input
    Output = _Output


class _Schema:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _NodeReplace(_Schema):
    pass


class _NodeOutput:
    def __init__(self, *values, **kwargs):
        self.values = values
        self.ui = kwargs.get("ui")


class _ComfyNode:
    @classmethod
    def GET_SCHEMA(cls):
        return cls.define_schema()

    @classmethod
    def GET_NODE_INFO_V1(cls):
        schema = cls.GET_SCHEMA()
        return {
            "name": schema.node_id,
            "display_name": schema.display_name,
            "input": {
                "required": {},
                "optional": {},
                "hidden": {
                    str(item): item for item in getattr(schema, "hidden", [])
                },
            },
        }


class _ComfyExtension:
    pass


_REGISTERED_REPLACEMENTS = []


class _ReplacementRegistry:
    async def register(self, replacement):
        _REGISTERED_REPLACEMENTS.append(replacement)


class _ComfyAPI:
    def __init__(self):
        self.node_replacement = _ReplacementRegistry()


def _install_comfy_api_stub(monkeypatch):
    io = types.SimpleNamespace(
        ComfyNode=_ComfyNode,
        Schema=_Schema,
        Custom=lambda _name: _CustomType,
        AnyType=types.SimpleNamespace(Input=_AnyInput),
        String=types.SimpleNamespace(Input=_StringInput),
        Autogrow=_Autogrow,
        Hidden=types.SimpleNamespace(
            prompt="prompt",
            extra_pnginfo="extra_pnginfo",
            unique_id="unique_id",
        ),
        NodeOutput=_NodeOutput,
        NodeReplace=_NodeReplace,
    )
    api_module = types.ModuleType("comfy_api")
    version_module = types.ModuleType("comfy_api.v0_0_2")
    version_module.io = io
    version_module.ComfyAPI = _ComfyAPI
    version_module.ComfyExtension = _ComfyExtension
    api_module.v0_0_2 = version_module
    monkeypatch.setitem(sys.modules, "comfy_api", api_module)
    monkeypatch.setitem(sys.modules, "comfy_api.v0_0_2", version_module)


def _import_v3(monkeypatch):
    _install_comfy_api_stub(monkeypatch)
    if TEST_PACKAGE not in sys.modules:
        pkg = types.ModuleType(TEST_PACKAGE)
        pkg.__path__ = [str(ROOT)]
        sys.modules[TEST_PACKAGE] = pkg
    module_name = f"{TEST_PACKAGE}.nodes.metadata_collector_v3"
    sys.modules.pop(module_name, None)
    importlib.invalidate_caches()
    return importlib.import_module(module_name)


def _load_root_with_fake_v1(
    monkeypatch,
    package_name: str,
    *,
    broken_v3=False,
    schema_failure=False,
    remove_comfy=True,
):
    package_nodes = types.ModuleType(f"{package_name}.nodes")
    package_nodes.__path__ = [str(ROOT / "nodes")]
    monkeypatch.setitem(sys.modules, f"{package_name}.nodes", package_nodes)

    fake_modules = {
        "editor_node": ["SonderEditor"],
        "io_nodes": ["SonderSaveBridge", "SonderSaveVideo", "SonderPreviewVideo"],
        "bridge_nodes": ["SonderGuidesBridgeStart", "SonderGuidesBridgeEnd"],
        "driver_bridge": ["SonderDriverBridge", "SonderDriverSelector"],
        "masks_bridge": ["SonderMasksBridge"],
        "prompt_bridge": ["SonderPromptRelayBridge"],
        "selector": ["SonderSelector"],
    }
    for module_name, class_names in fake_modules.items():
        module = types.ModuleType(f"{package_name}.nodes.{module_name}")
        for class_name in class_names:
            setattr(module, class_name, type(class_name, (), {}))
        monkeypatch.setitem(sys.modules, module.__name__, module)

    if broken_v3:
        monkeypatch.setitem(
            sys.modules,
            f"{package_name}.nodes.metadata_collector_v3",
            types.ModuleType(f"{package_name}.nodes.metadata_collector_v3"),
        )
    elif schema_failure:
        module = types.ModuleType(f"{package_name}.nodes.metadata_collector_v3")
        module.V3_NODE_ID = "SonderMetadataCollectorV3"

        class FailingCollector:
            @classmethod
            def GET_SCHEMA(cls):
                raise RuntimeError("schema finalization failed")

        module.SonderMetadataCollectorV3 = FailingCollector
        monkeypatch.setitem(sys.modules, module.__name__, module)

    if remove_comfy:
        for module_name in ("comfy_api.v0_0_2", "comfy_api.latest", "comfy_api"):
            monkeypatch.delitem(sys.modules, module_name, raising=False)

    spec = importlib.util.spec_from_file_location(
        package_name,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, package_name, module)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_v3_schema_uses_fixed_heterogeneous_autogrow(monkeypatch):
    module = _import_v3(monkeypatch)
    schema = module.SonderMetadataCollectorV3.define_schema()

    assert schema.node_id == "SonderMetadataCollectorV3"
    assert schema.display_name == "Sonder Metadata Collector Nodes 2.0"
    assert schema.hidden == ["prompt", "extra_pnginfo", "unique_id"]
    assert schema.inputs[0].id == "project"
    assert schema.inputs[1].id == "values"
    assert schema.inputs[1].template.prefix == "value_"
    assert schema.inputs[1].template.min == 1
    assert schema.inputs[1].template.max == 32
    assert schema.inputs[1].template.input.kind == "any"
    assert [item.id for item in schema.inputs[2:]] == [
        f"label_{index}" for index in range(32)
    ]


def test_v3_replacement_descriptor_is_complete_but_release_gated(monkeypatch):
    module = _import_v3(monkeypatch)
    replacement = module.build_metadata_collector_replacement()

    assert replacement.old_node_id == "SonderMetadataCollector"
    assert replacement.new_node_id == "SonderMetadataCollectorV3"
    assert replacement.old_widget_ids == [
        f"label_{index}" for index in range(32)
    ]
    assert replacement.input_mapping[0] == {
        "new_id": "project",
        "old_id": "project",
    }
    assert replacement.input_mapping[1:33] == [
        {
            "new_id": f"values.value_{index}",
            "old_id": f"value_{index}",
        }
        for index in range(32)
    ]
    assert replacement.input_mapping[33:] == [
        {
            "new_id": f"label_{index}",
            "old_id": f"label_{index}",
        }
        for index in range(32)
    ]
    assert replacement.output_mapping == [{"new_idx": 0, "old_idx": 0}]

    _REGISTERED_REPLACEMENTS.clear()
    asyncio.run(module.SonderMetadataCollectorExtension().on_load())
    assert module.METADATA_COLLECTOR_REPLACEMENT_ENABLED is False
    assert _REGISTERED_REPLACEMENTS == []


def test_v3_hidden_context_drives_execution_and_fingerprint(monkeypatch, tmp_path):
    module = _import_v3(monkeypatch)
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    project = timeline_state.TimelineProject(
        project_dir=str(tmp_path),
        name="V3 Collector Test",
    )
    prompt = {
        "10": {"class_type": "ImageNode", "inputs": {"seed": 3}},
        "20": {
            "class_type": "SonderMetadataCollectorV3",
            "inputs": {"values.value_0": ["10", 0]},
        },
    }
    module.SonderMetadataCollectorV3.hidden = types.SimpleNamespace(
        prompt=prompt,
        extra_pnginfo={"workflow": {}},
        unique_id="20",
    )

    fingerprint = module.SonderMetadataCollectorV3.fingerprint_inputs(
        project=project,
        values={"value_0": object()},
        label_0="Image",
    )
    output = module.SonderMetadataCollectorV3.execute(
        project,
        values={"value_0": object()},
        label_0="Image",
    )

    assert isinstance(fingerprint, str)
    assert output.values == (project,)
    chain = project._execution_context["_tracked_metadata_internal"]["20"]
    assert chain[0]["label"] == "Image"


def test_missing_comfy_api_keeps_all_v1_nodes_and_normal_name(monkeypatch):
    module = _load_root_with_fake_v1(monkeypatch, "video_editor_guard_missing")

    assert set(module.NODE_CLASS_MAPPINGS) == {
        "SonderEditor",
        "SonderMetadataCollector",
        "SonderSaveBridge",
        "SonderSaveVideo",
        "SonderPreviewVideo",
        "SonderGuidesBridgeStart",
        "SonderGuidesBridgeEnd",
        "SonderDriverSelector",
        "SonderDriverBridge",
        "SonderMasksBridge",
        "SonderPromptRelayBridge",
        "SonderSelector",
    }
    assert (
        module.NODE_DISPLAY_NAME_MAPPINGS["SonderMetadataCollector"]
        == "Sonder Metadata Collector"
    )
    assert not hasattr(module, "comfy_entrypoint")


def test_internal_v3_failure_is_distinct_and_keeps_v1_nodes(monkeypatch, caplog):
    caplog.set_level("WARNING")
    module = _load_root_with_fake_v1(
        monkeypatch,
        "video_editor_guard_broken",
        broken_v3=True,
    )

    assert "SonderMetadataCollector" in module.NODE_CLASS_MAPPINGS
    assert (
        module.NODE_DISPLAY_NAME_MAPPINGS["SonderMetadataCollector"]
        == "Sonder Metadata Collector"
    )
    assert "Sonder Metadata Collector V3 unavailable" in caplog.text


def test_available_v3_is_directly_mapped_with_nodes_2_name(monkeypatch):
    _install_comfy_api_stub(monkeypatch)
    module = _load_root_with_fake_v1(
        monkeypatch,
        "video_editor_guard_available",
        remove_comfy=False,
    )

    assert "SonderMetadataCollector" in module.NODE_CLASS_MAPPINGS
    assert "SonderMetadataCollectorV3" in module.NODE_CLASS_MAPPINGS
    assert (
        module.NODE_DISPLAY_NAME_MAPPINGS["SonderMetadataCollector"]
        == "Sonder Metadata Collector"
    )
    assert (
        module.NODE_DISPLAY_NAME_MAPPINGS["SonderMetadataCollectorV3"]
        == "Sonder Metadata Collector Nodes 2.0"
    )
    assert not hasattr(module, "comfy_entrypoint")
    assert module.__all__ == [
        "NODE_CLASS_MAPPINGS",
        "NODE_DISPLAY_NAME_MAPPINGS",
        "WEB_DIRECTORY",
    ]


def test_v3_schema_failure_keeps_normal_legacy_registration(monkeypatch, caplog):
    caplog.set_level("WARNING")
    module = _load_root_with_fake_v1(
        monkeypatch,
        "video_editor_guard_schema_failure",
        schema_failure=True,
        remove_comfy=False,
    )

    assert "SonderMetadataCollector" in module.NODE_CLASS_MAPPINGS
    assert "SonderMetadataCollectorV3" not in module.NODE_CLASS_MAPPINGS
    assert (
        module.NODE_DISPLAY_NAME_MAPPINGS["SonderMetadataCollector"]
        == "Sonder Metadata Collector"
    )
    assert "Sonder Metadata Collector V3 unavailable" in caplog.text
    assert not hasattr(module, "comfy_entrypoint")


def test_v3_registration_can_retry_after_dependency_restoration(monkeypatch):
    package_name = "video_editor_guard_retry"
    failed = _load_root_with_fake_v1(
        monkeypatch,
        package_name,
        schema_failure=True,
        remove_comfy=False,
    )
    assert "SonderMetadataCollectorV3" not in failed.NODE_CLASS_MAPPINGS

    monkeypatch.delitem(sys.modules, package_name, raising=False)
    monkeypatch.delitem(
        sys.modules,
        f"{package_name}.nodes.metadata_collector_v3",
        raising=False,
    )
    _install_comfy_api_stub(monkeypatch)
    restored = _load_root_with_fake_v1(
        monkeypatch,
        package_name,
        remove_comfy=False,
    )

    assert "SonderMetadataCollectorV3" in restored.NODE_CLASS_MAPPINGS
    assert (
        restored.NODE_DISPLAY_NAME_MAPPINGS["SonderMetadataCollector"]
        == "Sonder Metadata Collector"
    )


def test_v3_object_info_bridge_exposes_schema_contract(monkeypatch):
    module = _import_v3(monkeypatch)
    schema = module.SonderMetadataCollectorV3.GET_SCHEMA()
    node_info = module.SonderMetadataCollectorV3.GET_NODE_INFO_V1()

    assert schema.node_id == "SonderMetadataCollectorV3"
    assert schema.display_name == "Sonder Metadata Collector Nodes 2.0"
    assert node_info["name"] == "SonderMetadataCollectorV3"
    assert node_info["display_name"] == "Sonder Metadata Collector Nodes 2.0"
    assert set(node_info["input"]["hidden"]) == {
        "prompt",
        "extra_pnginfo",
        "unique_id",
    }


def test_real_comfy_v3_object_info_contract_when_available(monkeypatch):
    try:
        importlib.import_module("comfy_api")
    except ModuleNotFoundError:
        pytest.skip("real comfy_api runtime is not available")

    package_name = "video_editor_real_comfy_contract"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, package_name, package)
    module = importlib.import_module(f"{package_name}.nodes.metadata_collector_v3")

    schema = module.SonderMetadataCollectorV3.GET_SCHEMA()
    node_info = module.SonderMetadataCollectorV3.GET_NODE_INFO_V1()
    autogrow = node_info["input"]["optional"]["values"]

    assert schema.node_id == "SonderMetadataCollectorV3"
    assert node_info["name"] == "SonderMetadataCollectorV3"
    assert node_info["display_name"] == "Sonder Metadata Collector Nodes 2.0"
    assert node_info["input"]["required"]["project"][0] == "SONDER_PROJECT"
    assert set(node_info["input"]["hidden"]) == {
        "prompt",
        "extra_pnginfo",
        "unique_id",
    }
    assert autogrow[0] == "COMFY_AUTOGROW_V3"
    assert autogrow[1]["template"]["prefix"] == "value_"
    assert autogrow[1]["template"]["max"] == 32
    assert (
        autogrow[1]["template"]["input"]["optional"]["value"][0]
        == "*"
    )
