import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from server import lane_registry
from server.timeline_state import Scene


ROOT = Path(__file__).resolve().parents[1]


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    return subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_python_lane_registry_is_a_dependency_leaf():
    source = (ROOT / "server" / "lane_registry.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    server_imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            server_imports.extend(alias.name for alias in node.names if alias.name.startswith("server"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level or module.startswith("server"):
                server_imports.append((node.level, module))
    assert server_imports == []


def test_frontend_backend_descriptor_parity_and_classified_fields():
    module_url = (ROOT / "web" / "js" / "lane_registry.js").as_uri()
    script = f"""
const mod = await import({json.dumps(module_url)});
const flattened = mod.LANE_DESCRIPTORS.map((descriptor) => ({{
  trackType: descriptor.trackType,
  laneType: descriptor.laneType,
  variable: descriptor.variable,
  headerControllable: descriptor.headerControllable,
  countField: descriptor.countField,
  configsField: descriptor.configsField,
  recipeAttr: descriptor.recipeAttr,
  fixedConfigField: descriptor.fixedConfigField,
  itemsField: descriptor.itemsSource?.listField || "",
  itemIndexField: descriptor.itemsSource?.indexField || "",
  itemIdField: descriptor.itemsSource?.idField || "",
  mutationPredicate: descriptor.itemsSource?.mutationPredicate || "none",
  maxItemsPerLane: descriptor.maxItemsPerLane,
  supportsMultiLaneDelete: descriptor.supportsMultiLaneDelete,
  supportsCompaction: descriptor.supportsCompaction,
  laneRemovable: descriptor.laneRemovable,
}}));
console.log(JSON.stringify({{
  descriptors: flattened,
  fields: Object.keys(mod.LANE_DESCRIPTORS[0]).sort(),
  variableTrackTypes: mod.VARIABLE_TRACK_TYPES,
}}));
"""
    js = json.loads(_run_node(script))
    field_map = {
        "trackType": "track_type",
        "laneType": "lane_type",
        "variable": "variable",
        "headerControllable": "header_controllable",
        "countField": "count_attr",
        "configsField": "configs_attr",
        "recipeAttr": "recipe_attr",
        "fixedConfigField": "fixed_config_attr",
        "itemsField": "items_attr",
        "itemIndexField": "item_index_attr",
        "itemIdField": "item_id_attr",
        "mutationPredicate": "item_predicate",
        "maxItemsPerLane": "max_items_per_lane",
        "supportsMultiLaneDelete": "supports_multi_lane_delete",
        "supportsCompaction": "supports_compaction",
        "laneRemovable": "lane_removable",
    }
    py = {
        descriptor.track_type: {
            js_name: getattr(descriptor, py_name) for js_name, py_name in field_map.items()
        }
        for descriptor in lane_registry.LANE_DESCRIPTORS
    }
    assert {descriptor["trackType"]: descriptor for descriptor in js["descriptors"]} == py

    shared_top_level = {
        "trackType", "laneType", "variable", "headerControllable", "countField",
        "configsField", "recipeAttr", "fixedConfigField", "maxItemsPerLane",
        "supportsMultiLaneDelete", "supportsCompaction", "laneRemovable",
    }
    js_only = {
        "layoutOrder", "laneOrder", "labelPrefix", "labelSingular", "labelFixed",
        "menuLabel", "logLabel", "color", "accentColorKey", "itemsSource",
        "visibilityIcons", "visibilityMode", "hasManageIcon", "manageAction", "dropAccepts",
    }
    assert set(js["fields"]) - shared_top_level == js_only
    assert set(lane_registry.LaneDescriptor.__dataclass_fields__) - set(field_map.values()) == {
        "snapshot_count_attr", "snapshot_configs_attr"
    }
    assert tuple(lane_registry.VARIABLE_LANE_TYPES) == ("video", "motion_driver", "audio", "reference")
    assert js["variableTrackTypes"] == ["video", "audio", "motion_driver", "reference"]


def test_descriptor_scene_attributes_and_unknown_role_divergence_are_pinned():
    scene = Scene()
    for descriptor in lane_registry.LANE_DESCRIPTORS:
        for attr in (
            descriptor.count_attr,
            descriptor.configs_attr,
            descriptor.fixed_config_attr,
            descriptor.items_attr,
        ):
            if attr:
                assert hasattr(scene, attr), f"{descriptor.lane_type} references missing Scene.{attr}"

    unknown = type("UnknownClip", (), {"role": "foo"})()
    assert lane_registry.clip_lane_type(unknown) == "motion_driver", (
        "Backend unknown roles currently fall through to Driver; harmonizing this "
        "with frontend draw/mutation predicates is a later behavior change."
    )
