import importlib
import json
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from server import routes
from server.reference_resolution import REFERENCE_OUTPUT_NAMES
from server.timeline_state import REFERENCE_RECIPE_FIELDS, REFERENCE_RECIPE_PRESETS


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_KEYS = {field["key"] for field in REFERENCE_RECIPE_FIELDS}
_CORE_PACKAGE = "reference_panel_testpkg"


def test_reference_panel_recipe_edits_preserve_lane_identity_executably():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Reference lane identity coverage")
    module_url = (ROOT / "web" / "js" / "reference_lane_identity.js").as_uri()
    script = f"""
const {{ preserveLaneRecipeIdentity }} = await import({json.dumps(module_url)});
const cases = [
  preserveLaneRecipeIdentity({{lane_id: "stable"}}, {{recipe_id: "built-in"}}, "minted"),
  preserveLaneRecipeIdentity({{lane_id: "stable"}}, {{lane_id: "churn", media_kind: "audio"}}, "minted"),
  preserveLaneRecipeIdentity({{}}, {{recipe_id: "detached"}}, "minted"),
];
console.log(JSON.stringify(cases));
"""
    values = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)
    assert [value["lane_id"] for value in values] == [
        "stable", "stable", "minted"]


def _import_reference_core(monkeypatch):
    """`nodes/reference_core.py` uses package-relative imports, so it only loads
    under a synthetic package rooted at the repo (same shim as
    tests/test_reference_bridge_v3.py)."""
    if _CORE_PACKAGE not in sys.modules:
        package = types.ModuleType(_CORE_PACKAGE)
        package.__path__ = [str(ROOT)]
        monkeypatch.setitem(sys.modules, _CORE_PACKAGE, package)
    module_name = f"{_CORE_PACKAGE}.nodes.reference_core"
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


def test_recipe_schema_declares_every_key_the_presets_and_assembler_use():
    """The schema is the form, the validator and the documentation at once.

    A key the assembler reads but the schema omits is unreachable from the
    panel; a key the schema declares that nothing reads is a control that
    silently does nothing. Both are caught here.
    """
    preset_keys = set()
    for preset in REFERENCE_RECIPE_PRESETS:
        preset_keys.update(preset["hard"])
        preset_keys.update(preset["soft"])
    assert preset_keys <= SCHEMA_KEYS, f"presets use undeclared keys: {sorted(preset_keys - SCHEMA_KEYS)}"

    core = (ROOT / "nodes" / "reference_core.py").read_text(encoding="utf-8")
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    panel = (ROOT / "web" / "js" / "editor_reference_panel.js").read_text(encoding="utf-8")
    context = (ROOT / "server" / "prompt_context.py").read_text(encoding="utf-8")
    minimax = (ROOT / "server" / "minimax_h3.py").read_text(encoding="utf-8")
    routes_source = (ROOT / "server" / "routes.py").read_text(encoding="utf-8")
    # `resolved` is resolve_pegged_hard's working copy of the hard block, so a
    # key read only while resolving a peg still counts as consumed.
    read_keys = set(re.findall(r'(?:hard(?:_wrapper)?|resolved)\.get\("([a-z_]+)"', core))
    read_keys.update(re.findall(r'soft\.get\("([a-z_]+)"', core))
    read_keys.update(re.findall(r'\b(?:hard|soft)\.([a-z_]+)\b', widget))
    read_keys.update(re.findall(r'\bhard\.([a-z_]+)\b', panel))
    read_keys.update(re.findall(r'soft\.get\("([a-z_]+)"', minimax))
    read_keys.update(re.findall(r'generic_reference\.get\("([a-z_]+)"', context))
    read_keys.update(re.findall(r'soft\.get\("([a-z_]+)"', routes_source))
    assert read_keys & SCHEMA_KEYS, "expected the scan to find real reads"
    assert read_keys <= SCHEMA_KEYS | {"get"}, f"assembler reads undeclared keys: {sorted(read_keys - SCHEMA_KEYS)}"

    consumed = read_keys | preset_keys
    assert SCHEMA_KEYS <= consumed, f"schema declares dead fields: {sorted(SCHEMA_KEYS - consumed)}"


def test_recipe_schema_entries_are_well_formed():
    assemblies = set(next(field for field in REFERENCE_RECIPE_FIELDS if field["key"] == "assembly")["values"])
    for field in REFERENCE_RECIPE_FIELDS:
        assert field["section"] in {"hard", "soft"}
        assert field["type"] in {"enum", "int", "number", "bool", "string", "int_list", "int_pair", "string_list", "output_list"}
        assert field["label"] and field["group"] and field["help"]
        assert set(field["applies_to"]) <= assemblies, field["key"]
        assert not field["requires"] or field["requires"] in SCHEMA_KEYS
        if field["requires_value"]:
            gate = next(entry for entry in REFERENCE_RECIPE_FIELDS if entry["key"] == field["requires"])
            assert gate["type"] == "enum" and field["requires_value"] in gate["values"], field["key"]
        if field["type"] == "enum":
            assert field["default"] in field["values"], field["key"]
        if field["key"] == "live_outputs":
            assert field["values"] == list(REFERENCE_OUTPUT_NAMES)
        # Which option you pick IS the decision, so no value may ship without an
        # explanation. Only enums and the output list have values to describe.
        if field["type"] in {"enum", "output_list"}:
            undocumented = sorted(set(field["values"]) - set(field.get("value_help", {})))
            assert not undocumented, f"{field['key']} values lack help: {undocumented}"
            assert all(field["value_help"][value].strip() for value in field["values"]), field["key"]
        else:
            assert not field.get("value_help"), f"{field['key']} has no values to describe"


def test_custom_recipes_round_trip_every_built_in_and_refuse_bad_authoring():
    for preset in REFERENCE_RECIPE_PRESETS:
        forked = routes._normalize_custom_reference_recipe({
            "name": f"{preset['name']} (custom)",
            "media_kind": preset["media_kind"],
            "hard": dict(preset["hard"]),
            "soft": dict(preset["soft"]),
        })
        # A fork must reproduce the preset exactly or "Edit as custom" quietly
        # changes render behaviour.
        assert forked["hard"] == preset["hard"]
        assert forked["soft"] == preset["soft"]
        assert forked["builtIn"] is False

    refusals = [
        {"hard": {"assemby": "sheet"}},
        {"hard": {"assembly": "collage"}},
        {"hard": {"max_members": "4"}},
        {"hard": {"max_members": 99}},
        {"hard": {"native_aspect": "yes"}},
        {"hard": {"live_outputs": ["reference_frames", "reference_glow"]}},
        {"hard": {"single_member_size": [460, 406, 12]}},
        {"soft": {"suggested_tags": "sonder:face_closeup"}},
        {"soft": {"prompt_prefix": 12}},
    ]
    for fields in refusals:
        with pytest.raises(routes.ProjectMutationRequestError) as refused:
            routes._normalize_custom_reference_recipe({"name": "Custom", **fields})
        assert refused.value.code == "invalid_reference_recipe", fields


def test_recipe_field_schema_is_served_with_the_library():
    payload_source = (ROOT / "server" / "routes.py").read_text(encoding="utf-8")
    assert '"recipe_field_schema": [dict(field) for field in REFERENCE_RECIPE_FIELDS]' in payload_source
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    assert "this._referenceRecipeFieldSchema = Array.isArray(payload?.recipe_field_schema)" in widget


def test_panel_field_visibility_follows_assembly_and_requirements():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the panel field-visibility test")
    schema = [dict(field) for field in REFERENCE_RECIPE_FIELDS]
    cases = [
        {"hard": {"assembly": "batch"}, "soft": {}},
        {"hard": {"assembly": "sheet"}, "soft": {}},
        {"hard": {"assembly": "temporal"}, "soft": {}},
        {"hard": {"assembly": "audio"}, "soft": {}},
        {"hard": {"assembly": "slots", "output_size": "native"}, "soft": {}},
        {"hard": {"assembly": "batch", "output_size": "custom"}, "soft": {}},
    ]
    module_url = (ROOT / "web" / "js" / "editor_reference_panel.js").as_uri()
    script = f"""
const mod = await import({json.dumps(module_url)});
const schema = {json.dumps(schema)};
console.log(JSON.stringify({json.dumps(cases)}.map(
  (c) => mod.visibleRecipeFields(schema, c.hard, c.soft).map((f) => f.key),
)));
"""
    visible = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)
    batch, sheet, temporal, audio, native_slots, custom_batch = visible

    # Fields that would present a control changing nothing must not appear.
    assert "layout" not in batch and "layout" in sheet
    assert "loop_frames" in sheet and "loop_frames" not in temporal
    assert "allowed_frame_counts" in temporal and "allowed_frame_counts" not in sheet
    assert "context_slot" not in temporal and "context_slot" not in batch
    assert "frame_rate" in batch and "frame_rate_source" in batch
    assert "frame_rate" not in sheet and "frame_rate_source" not in temporal
    assert "primary_model_position" in batch and "primary_model_position" not in sheet
    assert "recommended_duration_sec" in audio
    assert "output_size" not in audio and "width" not in audio
    # `requires` + `requires_value` gate on a sibling's VALUE, not the assembly:
    # the three Output size modes are mutually exclusive.
    assert "long_edge_max" in native_slots and "width" not in native_slots
    assert "width" in custom_batch and "height" in custom_batch and "long_edge_max" not in custom_batch
    assert "width" not in batch and "long_edge_max" not in batch  # default "scene"
    # Unconditional fields survive every mode.
    for keys in visible:
        assert "assembly" in keys and "max_members" in keys and "live_outputs" in keys


def test_panel_honours_the_overlay_and_mutation_contracts():
    panel = (ROOT / "web" / "js" / "editor_reference_panel.js").read_text(encoding="utf-8")
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")

    assert "priority: KEY_PRIORITY.OVERLAY" in panel
    assert 'if (event.key !== "Escape") return false;' in panel
    assert "unregisterKeyboard();" in panel

    # Every reference-item write is non-coalesced and carries exact prior values.
    # Item writes and item operations are exact, non-coalesced mutations.
    assert panel.count("coalesce: false") == 2
    for block in ("const writeItem =", "const runItemOperation ="):
        body = panel.split(block, 1)[1].split("\n    };", 1)[0]
        assert "coalesce: false" in body
    assert "expected[key] = item[key]" in panel
    assert "expected: { ...item }" in panel
    assert "expected: expectedRecipe(definition)" in panel

    # A built-in recipe is never edited in place.
    assert "const locked = builtIn || laneLocked() || state.busy;" in panel
    assert 'button("Edit as custom"' in panel

    # The host owns mount and teardown.
    assert 'import { mountReferenceLanePanel } from "./editor_reference_panel.js";' in widget
    assert "this._referencePanelHandle = mountReferenceLanePanel(this," in widget
    assert "this._referencePanelHandle?.close?.();" in widget
    assert "this._referencePanelHandle?.refresh?.();" in widget
    assert "_showReferenceLaneMenu" not in widget


def test_panel_uses_catalog_controls_and_progressive_disclosure():
    panel = (ROOT / "web" / "js" / "editor_reference_panel.js").read_text(encoding="utf-8")
    assert '"Works with prompt formats"' in panel
    assert '"Model input"' in panel
    assert '"Prompt parts added"' in panel
    assert '"Per-member options"' in panel
    assert '"Reset to suggestions"' not in panel
    assert "Unsupported:" in panel
    assert 'el("details"' in panel
    assert "disclosureStorageKey" in panel and "rememberDisclosure" in panel
    assert 'block.addEventListener("toggle"' in panel
    assert 'select.dataset.sonderInvalid = "1"' in panel
    assert "Unsupported saved value:" in panel
    assert panel.count("referenceRoleChoices(activeProfile, population)") == 2
    assert "writeMemberAudioIntent" in panel
    assert "role_aliases" not in panel


def test_mounted_panel_populates_roles_and_neutralizes_catalog_failure():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for mounted Reference panel coverage")
    module_url = (ROOT / "web" / "js" / "editor_reference_panel.js").as_uri()
    schema = [{
        "key": key, "section": "soft", "group": "Prompt Context",
        "label": key, "type": "string_list", "default": [],
        "applies_to": [], "requires": "", "requires_value": "", "help": key,
    } for key in ("compatible_profiles", "exposed_capabilities")]
    ready_catalog = {"schema_version": 1, "profiles": [{
        "key": "format@1", "resolved": {
            "profile_id": "format", "version": "1",
            "role_catalogs": {"pictures": [{
                "value": "identity", "label": "Character identity"}]},
            "capabilities": {"reference": {"derived": {
                "definitions": {
                    "order": 1, "label": "Definitions", "fields": {},
                    "channel_key": "visual", "placement": "section_prefix",
                },
                "retention": {
                    "order": 2, "label": "Retention", "channel_key": "visual",
                    "placement": "section_prefix", "fields": {
                        "visual_intent": {"values": [{
                            "value": "preserve", "label": "Fully preserve"}]},
                    },
                },
            }}},
        },
    }]}
    script = """
class N {{
  constructor(tag) {{ this.tagName=String(tag).toUpperCase(); this.children=[];
    this.options=[]; this.style={{cssText:""}}; this.dataset={{}}; this.attributes={{}};
    this.value=""; this.textContent=""; this.title=""; this.disabled=false;
    this.checked=false; this.open=false; this._handlers={{}}; }}
  appendChild(c) {{ if(!c?.tagName) return c; this.children.push(c); c.parentElement=this;
    if(c.tagName==="OPTION") this.options.push(c); return c; }}
  append(...cs) {{ cs.forEach((c)=>this.appendChild(c)); }}
  addEventListener(t,h) {{ (this._handlers[t] ||= []).push(h); }}
  removeEventListener() {{}}
  setAttribute(k,v) {{ this.attributes[k]=String(v); }}
  getAttribute(k) {{ return this.attributes[k] ?? null; }}
  querySelectorAll(sel) {{ const tags=String(sel).split(",").map((v)=>v.trim().toUpperCase());
    const out=[]; const walk=(n)=>n.children.forEach((c)=>{{
      if(tags.includes(c.tagName)) out.push(c); walk(c); }}); walk(this); return out; }}
  querySelector(sel) {{ return this.querySelectorAll(sel)[0] || null; }}
  focus() {{}}
  remove() {{ if(this.parentElement) this.parentElement.children=
    this.parentElement.children.filter((c)=>c!==this); }}
}}
globalThis.document={{createElement:(t)=>new N(t),body:new N("body"),activeElement:null}};
globalThis.window={{addEventListener(){{}},removeEventListener(){{}},localStorage:null}};
globalThis.localStorage={{getItem(){{return null;}},setItem(){{}}}};
const mod=await import(__MODULE_URL__);
const schema=__SCHEMA__;
const recipe={{id:"custom:one",name:"One",builtIn:false,media_kind:"image",
  hard:{{assembly:"batch"}},soft:{{
    compatible_profiles:["format@1"],exposed_capabilities:["definitions"],
    physical_population:"pictures",role_fields:["role","visual_intent"],
  }}}};
const member={{member_id:"member",role:"identity",visual_intent:"preserve"}};
const baseHost=()=>({{
  projectId:"project",activeSceneId:"scene",totalFrames:20,playhead:0,
  _trackLayout:[{{type:"reference",laneIndex:0,customName:"Reference 1"}}],
  activeScene:{{prompt_context_profile_id:"format@1",reference_lane_count:1,
    duration_frames:20,reference_lane_configs:[{{}}],
    reference_lane_recipes:[{{lane_id:"lane",recipe_id:"custom:one",
      media_kind:"image",recipe}}],reference_items:[{{reference_item_id:"item",
      lane_index:0,start_frame:0,end_frame:20,members:[member]}}]}},
  _referenceRecipePresets:[],_customReferenceRecipes:[recipe],
  _referenceRecipeFieldSchema:schema,_promptContextProfiles:[],
  _defaultReferenceLaneRecipe:()=>({{lane_id:"lane",recipe}}),
  _referenceMemberForRef:()=>({{reference:{{name:"Person"}},member:{{
    member_id:"member",name:"Portrait",prompt:"Person",asset_id:"asset"}}}}),
  _findAssetById:()=>null,_referenceAssetPreviewUrl:()=>null,
  _referenceLaneAdvisories:()=>[],_isLaneLocked:()=>false,
  _channelTemplate:()=>({{default_context_profile:"format@1"}}),
}});
const walk=(n,out=[])=>{{out.push(n);n.children.forEach((c)=>walk(c,out));return out;}};
const ready=baseHost();
ready._promptContextCatalog=__READY_CATALOG__;
const readyHandle=mod.mountReferenceLanePanel(ready,{{laneIndex:0}});
const readyNodes=walk(document.body.children.at(-1));
const readyRole=readyNodes.find((n)=>n.tagName==="SELECT" &&
  n.options.some((o)=>o.textContent==="Character identity"));
readyHandle.close();

const failed=baseHost(); failed._promptContextCatalog={{}};
failed._referencesError="offline"; failed._referencesLoading=false;
const failedHandle=mod.mountReferenceLanePanel(failed,{{laneIndex:0}});
const failedNodes=walk(document.body.children.at(-1));
const notice=failedNodes.find((n)=>n.dataset.sonderPromptCatalogState);
const failedRole=failedNodes.find((n)=>n.tagName==="SELECT" &&
  n.options.some((o)=>o.textContent.includes("Saved: identity")));
const labels=failedNodes.filter((n)=>n.tagName==="SPAN").map((n)=>n.textContent);
console.log(JSON.stringify({{
  readyRole:readyRole?.value||"",readyDisabled:readyRole?.disabled,
  failedState:notice?.dataset.sonderPromptCatalogState||"",
  failedRole:failedRole?.value||"",failedDisabled:failedRole?.disabled,
  hasUnsupported:labels.some((value)=>value.startsWith("Unsupported:")),
  unsupportedHeading:failedNodes.some((n)=>n.tagName==="SUMMARY"
    && n.textContent.includes("unsupported")),
  savedProfiles:labels.includes("Saved: format@1"),
  savedCapabilities:labels.includes("Saved: definitions"),
}}));
failedHandle.close();
"""
    script = (script.replace("{{", "{").replace("}}", "}")
              .replace("__MODULE_URL__", json.dumps(module_url))
              .replace("__SCHEMA__", json.dumps(schema))
              .replace("__READY_CATALOG__", json.dumps(ready_catalog)))
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == {
        "readyRole": "identity", "readyDisabled": False,
        "failedState": "error", "failedRole": "identity",
        "failedDisabled": True, "hasUnsupported": False,
        "unsupportedHeading": False,
        "savedProfiles": True, "savedCapabilities": True,
    }


def test_reference_panel_keeps_details_collapsed_and_offers_inspectable_thumbnails():
    panel = (ROOT / "web" / "js" / "editor_reference_panel.js").read_text(
        encoding="utf-8")
    assert 'disclosureOpen("item", item.reference_item_id, false)' in panel
    assert 'disclosureOpen("advisories", laneRecipe().lane_id || state.laneIndex, false)' in panel
    assert 'rememberDisclosure(\n            "advisories"' in panel
    assert "createMemberDraft(resolved?.member || null, asset || null)" in panel
    assert "host._openReferenceMediaEditor?.({ asset, draft, readOnly: true })" in panel
    assert "width:96px;height:64px" in panel
    assert "width:80px; height:60px" in panel


def _removed_client_h3_reference_population_planner_example():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for H3 population planner coverage")
    module_url = (ROOT / "web" / "js" / "editor_prompt_panel.js").as_uri()
    definitions = {
        "picture": {
            "id": "picture-recipe", "name": "Pictures", "kind": "image",
            "setupKey": "picture_lane_ids", "hard": {}, "soft": {},
        },
        "video": {
            "id": "video-recipe", "name": "Videos", "kind": "video",
            "setupKey": "video_lane_ids", "hard": {}, "soft": {},
        },
    }
    script = f"""
const mod = await import({json.dumps(module_url)});
const definitions = {json.dumps(definitions)};
const initial = {{reference_lane_count: 1, reference_lane_recipes: [],
  reference_items: [], minimax_h3_conditioning_setups: [],
  active_minimax_h3_setup_id: ''}};
const picture = mod.planH3ReferencePopulation(initial, definitions.picture,
  {{newLaneId: 'picture-lane', newSetupId: 'setup'}});
const pictureWrapper = picture.operations.find((op) => op.type === 'update_lane_config')
  .fields.reference_recipe;
const pictureSetupFields = picture.operations.find((op) => op.type === 'update_scene_fields').fields;
const afterPicture = {{...initial,
  reference_lane_recipes: [pictureWrapper],
  minimax_h3_conditioning_setups: pictureSetupFields.minimax_h3_conditioning_setups,
  active_minimax_h3_setup_id: pictureSetupFields.active_minimax_h3_setup_id}};
const video = mod.planH3ReferencePopulation(afterPicture, definitions.video,
  {{newLaneId: 'video-lane', newSetupId: 'unused'}});
const videoSetup = video.operations.find((op) => op.type === 'update_scene_fields')
  .fields.minimax_h3_conditioning_setups.find((row) => row.setup_id === 'setup');
const pictureAgain = mod.planH3ReferencePopulation(afterPicture, definitions.picture,
  {{newLaneId: 'unused', newSetupId: 'unused'}});
const detached = mod.planH3ReferencePopulation({{...initial,
  reference_lane_recipes: [{{lane_id: 'stable-detached', media_kind: 'image',
    recipe_id: '', recipe: {{}}}}]}}, definitions.picture,
  {{newLaneId: 'must-not-replace', newSetupId: 'detached-setup'}});
const authored = mod.planH3ReferencePopulation({{...initial,
  reference_lane_recipes: [{{lane_id: 'generic-authored', media_kind: 'image',
    recipe: {{id: 'custom:sheet', name: 'Authored sheet', hard: {{assembly: 'sheet'}}, soft: {{}}}}}}]}},
  definitions.picture, {{newLaneId: 'new-picture', newSetupId: 'authored-setup'}});
const inactiveReference = mod.planH3ReferencePopulation({{
  reference_lane_count: 3,
  reference_lane_recipes: [{{lane_id: 'blank'}},
    {{lane_id: 'video-lane', recipe_id: 'video-recipe'}},
    {{lane_id: 'audio-lane', recipe_id: 'audio-recipe'}}],
  reference_items: [],
  minimax_h3_conditioning_setups: [
    {{setup_id: 'base', mode: 'base', task_mode: 'I2VA'}},
    {{setup_id: 'reference', mode: 'reference', task_mode: 'T2VA',
      picture_lane_ids: [], video_lane_ids: ['video-lane'], audio_lane_ids: ['audio-lane']}}],
  active_minimax_h3_setup_id: 'base'}}, definitions.picture,
  {{newLaneId: 'must-preserve-blank-id', newSetupId: 'must-not-create'}});
const inactiveSetup = inactiveReference.operations.find((op) => op.type === 'update_scene_fields')
  .fields.minimax_h3_conditioning_setups.find((row) => row.setup_id === 'reference');
const activateExisting = mod.planH3ReferencePopulation({{
  reference_lane_count: 1,
  reference_lane_recipes: [{{lane_id: 'video-lane', media_kind: 'video',
    recipe_id: 'video-recipe', recipe: {{id: 'video-recipe'}}}}], reference_items: [],
  minimax_h3_conditioning_setups: [
    {{setup_id: 'base', mode: 'base'}},
    {{setup_id: 'reference', mode: 'reference', picture_lane_ids: [],
      video_lane_ids: ['video-lane'], audio_lane_ids: []}}],
  active_minimax_h3_setup_id: 'base'}}, definitions.video,
  {{newLaneId: 'unused', newSetupId: 'unused'}});
console.log(JSON.stringify({{
  pictureTypes: picture.operations.map((op) => op.type),
  pictureSetup: pictureSetupFields.minimax_h3_conditioning_setups[0],
  videoTypes: video.operations.map((op) => op.type), videoSetup,
  pictureAgain,
  detachedLaneId: detached.laneId,
  authoredLaneIndex: authored.laneIndex,
  authoredLaneId: authored.laneId,
  inactiveSetup,
  activateExisting,
}}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)
    assert result["pictureTypes"] == ["update_lane_config", "update_scene_fields"]
    assert result["pictureSetup"]["picture_lane_ids"] == ["picture-lane"]
    assert result["pictureSetup"]["video_lane_ids"] == []
    assert result["pictureSetup"]["audio_lane_ids"] == []
    assert result["videoTypes"] == [
        "set_lane_count", "update_lane_config", "update_scene_fields"]
    assert result["videoSetup"]["picture_lane_ids"] == ["picture-lane"]
    assert result["videoSetup"]["video_lane_ids"] == ["video-lane"]
    assert result["videoSetup"]["audio_lane_ids"] == []
    assert result["pictureAgain"]["alreadyLinked"] is True
    assert result["pictureAgain"]["operations"] == []
    assert result["detachedLaneId"] == "stable-detached"
    assert result["authoredLaneIndex"] == 1
    assert result["authoredLaneId"] == "new-picture"
    assert result["inactiveSetup"]["picture_lane_ids"] == ["blank"]
    assert result["inactiveSetup"]["video_lane_ids"] == ["video-lane"]
    assert result["inactiveSetup"]["audio_lane_ids"] == ["audio-lane"]
    assert result["activateExisting"]["alreadyLinked"] is True
    assert result["activateExisting"]["operations"] == [{
        "type": "update_scene_fields",
        "fields": {"active_minimax_h3_setup_id": "reference"},
    }]


def test_prompt_panel_keeps_h3_staging_outside_prompting_and_refuses_stale_retry():
    source = (ROOT / "web" / "js" / "editor_prompt_panel.js").read_text(encoding="utf-8")
    assert "retryOnConflict: false" in source
    assert "const populationButtons = []" not in source
    assert 'type: "ensure_minimax_h3_reference_population"' not in source
    assert "planH3ReferencePopulation" not in source


# One fixture set, computed in Python and in node, compared. Not a golden file:
# a drift in either half must fail rather than be re-recorded.
_PROMPT_FIXTURES = [
    {"promptOverride": "", "members": [], "soft": {}},
    {"promptOverride": "  a hand-written override  ", "members": [{"name": "Hero", "prompt": "face"}], "soft": {"prompt_prefix": "Reference sheet:"}},
    {"promptOverride": "", "members": [{"name": "Hero", "prompt": "face closeup"}, {"name": "Sofa", "prompt": ""}], "soft": {}},
    {"promptOverride": "", "members": [{"name": "Hero", "prompt": "face"}, {"name": "Sofa", "prompt": "living room"}], "soft": {"prompt_prefix": "Reference sheet:"}},
    {"promptOverride": "", "members": [{"name": "Hero", "prompt": "face"}, {"name": "Sofa", "prompt": "room"}], "soft": {"prompt_tokens": "image{index}"}},
    {"promptOverride": "", "members": [{"name": "", "prompt": ""}, {"name": "Sofa", "prompt": ""}], "soft": {"prompt_tokens": "image{index}"}},
    {"promptOverride": "", "members": [{"name": "", "prompt": ""}], "soft": {"prompt_tokens": "ref"}},
    # Repeated placeholder: Python replaces every occurrence, JS String.replace
    # with a string literal replaces only the first.
    {"promptOverride": "", "members": [{"name": "Hero", "prompt": "face"}], "soft": {"prompt_tokens": "{index}_of_{index}"}},
    # {n} is 1-based where {index} is 0-based, so both must survive side by side.
    {"promptOverride": "", "members": [{"name": "Hero", "prompt": "face"}, {"name": "Sofa", "prompt": "room"}], "soft": {"prompt_tokens": "n={n} i={index}"}},
    # A pattern carrying {prompt}/{name} composes a whole sentence instead of
    # prefixing a token, and the member text is NOT appended a second time.
    {"promptOverride": "", "members": [{"name": "Chloe", "prompt": "a redhead woman"}, {"name": "Sofa", "prompt": "a couch"}],
     "soft": {"prompt_tokens": "<Subject {n}> is {prompt}, from <Picture {n}>"}},
    {"promptOverride": "", "members": [{"name": "Chloe", "prompt": ""}], "soft": {"prompt_tokens": "<{name} {n}>"}},
    {"promptOverride": "", "members": [{"name": "", "prompt": ""}], "soft": {"prompt_tokens": "<Subject {n}> is {prompt}"}},
    {"promptOverride": "", "members": [{"name": "Hero", "prompt": "  padded  "}], "soft": {"prompt_prefix": "  spaced  ", "prompt_tokens": ""}},
]


def _python_derived_prompt(core, fixture):
    records = [
        {
            "reference": types.SimpleNamespace(name=member["name"]),
            "member": types.SimpleNamespace(prompt=member["prompt"]),
        }
        for member in fixture["members"]
    ]
    return core._assemble_prompt(
        {"prompt_override": fixture["promptOverride"]}, records, {"soft": fixture["soft"]},
    )


def test_derived_prompt_matches_between_python_and_javascript(monkeypatch):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the derived-prompt parity test")
    core = _import_reference_core(monkeypatch)
    expected = [_python_derived_prompt(core, fixture) for fixture in _PROMPT_FIXTURES]

    module_url = (ROOT / "web" / "js" / "reference_resolution.js").as_uri()
    script = f"""
const mod = await import({json.dumps(module_url)});
console.log(JSON.stringify({json.dumps(_PROMPT_FIXTURES)}.map((f) => mod.deriveReferencePrompt(f))));
"""
    actual = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)
    assert actual == expected
    # Guard the fixtures themselves: an all-empty result would pass vacuously.
    assert len([value for value in expected if value]) >= 7


FIXED_OUTPUT_NAMES = [name for name in REFERENCE_OUTPUT_NAMES if name != "slots"]
SLOT_NAMES = [f"r{index:02d}" for index in range(1, 17)]
PROMPT_SLOT_NAMES = [f"p{index:02d}" for index in range(1, 17)]


def test_selector_panel_view_keeps_an_orphaned_lane_visible():
    """The dropdown must never read as a lane other than the INT actually holds."""
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node is required for the selector view test")
    lanes = [
        {"lane_index": 0, "lane_name": "Reference 1", "recipe_name": "Wan VACE Reference Sheet",
         "media_kind": "image", "item_count": 2, "member_count": 3, "hidden": False,
         "live_outputs": ["image_slots", "reference_prompt", "reference_names"],
         "member_tags": ["sonder:face_closeup", "sonder:location"]},
        {"lane_index": 1, "lane_name": "Voices", "recipe_name": "LTX ID-LoRA Voice Identity",
         "media_kind": "audio", "item_count": 1, "member_count": 1, "hidden": True,
         "live_outputs": ["audio_slots"], "member_tags": []},
    ]
    module_url = (ROOT / "web" / "js" / "reference_bridge_shape.js").as_uri()
    script = f"""
const {{ selectorPanelView }} = await import({json.dumps(module_url)});
const lanes = {json.dumps(lanes)};
console.log(JSON.stringify({{
  first: selectorPanelView({{ lanes, laneIndex: 0 }}),
  hiddenAudio: selectorPanelView({{ lanes, laneIndex: 1 }}),
  orphan: selectorPanelView({{ lanes, laneIndex: 7, sceneName: 'Act One' }}),
  frozen: selectorPanelView({{ lanes, laneIndex: 0, source: 'snapshot' }}),
  unresolved: selectorPanelView({{ lanes: [], laneIndex: 0, status: 'Connect a Sonder Editor project.' }}),
}}));
"""
    out = json.loads(subprocess.run(
        [node_bin, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)

    assert out["first"]["options"][0]["label"] == "Reference 1 — Wan VACE Reference Sheet"
    assert out["first"]["status"] == "image · 2 items · 3 members"
    assert out["first"]["outputs"] == [
        "Image Bridge r01..r16",
        "Prompt Bridge aggregate + p01..p16",
        "Prompt Bridge reference_names",
    ]
    # Tags identify WHICH references the lane carries; the namespace is dropped.
    assert out["first"]["tags"] == ["face_closeup", "location"]
    assert out["hiddenAudio"]["tags"] == []
    assert out["orphan"]["tags"] == []
    assert out["hiddenAudio"]["status"] == "audio · 1 item · 1 member · lane hidden"
    # The orphan option keeps the real INT value selectable and says why.
    assert out["orphan"]["options"][-1] == {"value": 7, "label": "Reference 8 — no such lane in this scene", "orphan": True}
    assert out["orphan"]["selectedValue"] == 7
    assert out["orphan"]["status"] == "Lane 7 is not in Act One."
    assert out["frozen"]["status"].endswith("frozen job")
    assert out["unresolved"]["disabled"] is True
    assert out["unresolved"]["status"] == "Connect a Sonder Editor project."


def test_lane_bar_gives_names_priority_over_tags():
    """Tags identify WHICH reference, but a short bar must keep the name.

    Pixel behaviour is a manual row; this pins the yield rule itself so the
    priority cannot be silently inverted.
    """
    canvas = (ROOT / "web" / "js" / "editor_timeline_canvas.js").read_text(encoding="utf-8")
    block = canvas.split("Reference items are source-less timeline scopes", 1)[1].split("export function", 1)[0]
    # The name is drawn unconditionally; tags only after measuring what is left.
    assert "ctx.fillText(label, textX, baseline);" in block
    name_at = block.index("ctx.fillText(label, textX, baseline);")
    tags_at = block.index("shortened.join")
    assert name_at < tags_at, "the member name must be drawn before tags claim space"
    assert "const room = (x2 - x1 - 6) - nameW" in block
    assert "if (room > Math.round(30 * scale))" in block
    assert 'tag.replace(/^sonder:/, "")' in block

    # The recipe rides the lane HEADER, not each bar: it is per-lane and
    # invariant across items, so repeating it would cost the bar its space.
    header = canvas.split("// 5. Label", 1)[1].split("// Border", 1)[0]
    assert "_referenceLaneRecipeLabel" in header and "_referenceLaneRecipeLabel" not in block
    assert "const room = maxLabelW - nameW" in header


def test_reference_item_editor_defers_the_prompt_to_the_lane_panel():
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    editor = widget.split("    _showItemEditor() {", 1)[1]
    block = editor.split('if (type === "reference") {', 1)[1].split('} else if (type === "clip")', 1)[0]
    # A bare inline input could only offer the override half of the contract:
    # it has no way to show the derived text the override replaces.
    assert "prompt_override" not in block
    assert 'this._makeBtn("Lane Setup…"' in block
    assert "_showReferenceLanePanel(laneEntry)" in block
    strength = block.split('this._makeEditorLabel("Strength:")', 1)[1]
    assert 'strengthInput.addEventListener("change"' in strength
    assert "{ strength }, { coalesce: false }" in strength
    assert "data.strength = strength" not in strength


def test_dimension_constraint_is_frozen_and_old_projects_self_heal():
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    assert "dimension_constraint: getDimensionConstraint(this._getActiveTemplate())" in widget
    assert "await this._maybeHealDimensionConstraint(" in widget
    heal = widget.split("async _maybeHealDimensionConstraint", 1)[1].split("_ensureViewportSurface", 1)[0]
    assert "JSON.stringify({ dimension_constraint: expected })" in heal


FIXED_OUTPUT_NAMES = [name for name in REFERENCE_OUTPUT_NAMES if name != "slots"]
SLOT_NAMES = [f"r{index:02d}" for index in range(1, 17)]
PROMPT_SLOT_NAMES = [f"p{index:02d}" for index in range(1, 17)]


def test_bridge_never_removes_a_fixed_output_because_slot_index_is_the_contract():
    """Regression: a dead MIDDLE output must not be removed.

    ComfyUI validates a connection against the static /object_info definition by
    slot index. Removing `reference_audio` (index 3) slid `r01` into index 3, so
    dragging from a slot labelled r01 was refused as AUDIO. Dead fixed outputs
    are relabelled instead; only the r-block, a contiguous tail, shrinks.
    """
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node is required for the bridge shape test")
    module_url = (ROOT / "web" / "js" / "reference_bridge_shape.js").as_uri()
    split_script = rf"""
const {{ resolveBridgeOutputs, canonicalOutputOrder, UNUSED_SUFFIX }} =
  await import({json.dumps(module_url)});
const makeNode = (type) => ({{
  type,
  comfyClass: type,
  outputs: canonicalOutputOrder(type).map((name) => ({{ name, type: type.includes('Audio') ? 'AUDIO' : type.includes('Prompt') ? 'STRING' : 'IMAGE', links: [] }})),
  addOutput(name, slotType, opts) {{ this.outputs.push({{ name, type: slotType, links: [], ...opts }}); }},
  removeOutput(index) {{ this.outputs.splice(index, 1); }},
}});
const image = makeNode('SonderReferenceImageBridge');
image.outputs.find((slot) => slot.name === 'r03').links = [17];
resolveBridgeOutputs(image, {{ imageSlotCount: 2, liveOutputs: ['image_slots'], slotLabels: ['Hero (subject)', 'Prop (subject)'] }});
const saved = image.outputs.map((slot) => slot.name);
const reloaded = makeNode('SonderReferenceImageBridge');
reloaded.outputs.find((slot) => slot.name === 'r03').links = [17];
resolveBridgeOutputs(reloaded, {{ imageSlotCount: 2, liveOutputs: ['image_slots'], slotLabels: ['Hero (subject)', 'Prop (subject)'] }});
const repaired = makeNode('SonderReferenceImageBridge');
repaired.outputs = repaired.outputs.filter((slot) => slot.name === 'r01' || slot.name === 'r03');
resolveBridgeOutputs(repaired, {{ imageSlotCount: 3, liveOutputs: ['image_slots'] }});
const audio = makeNode('SonderReferenceAudioBridge');
resolveBridgeOutputs(audio, {{ audioSlotCount: 3, liveOutputs: ['audio_slots'], slotLabels: ['Voice A', 'Voice B', 'Voice C'] }});
const prompt = makeNode('SonderReferencePromptBridge');
resolveBridgeOutputs(prompt, {{ promptSlotCount: 2, liveOutputs: ['reference_prompt', 'reference_names'], slotLabels: ['Hero', 'Prop'] }});
const promptNamesDead = makeNode('SonderReferencePromptBridge');
resolveBridgeOutputs(promptNamesDead, {{ promptSlotCount: 1, liveOutputs: ['reference_prompt'] }});
console.log(JSON.stringify({{
  imageNames: image.outputs.map((slot) => slot.name),
  imageLabels: image.outputs.map((slot) => [slot.label, slot.localized_name]),
  saved,
  reloadedNames: reloaded.outputs.map((slot) => slot.name),
  repairedNames: repaired.outputs.map((slot) => slot.name),
  audioNames: audio.outputs.map((slot) => slot.name),
  promptNames: prompt.outputs.map((slot) => slot.name),
  deadNamesLabel: promptNamesDead.outputs.find((slot) => slot.name === 'reference_names').label,
  suffix: UNUSED_SUFFIX,
}}));
"""
    split = json.loads(subprocess.run(
        [node_bin, "--input-type=module", "-e", split_script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)
    # The r03 connection pins one ceiling. r02 survives even though it is not
    # connected: no per-slot removal, no hole, no silent r03 -> r02 shift.
    assert split["imageNames"] == ["r01", "r02", "r03"]
    assert split["saved"] == split["reloadedNames"]
    assert split["repairedNames"] == ["r01", "r02", "r03"]
    assert all(label.startswith("r01") and "Hero (subject)" in label for label in split["imageLabels"][0])
    assert split["imageLabels"][2][0].endswith(split["suffix"])
    assert split["audioNames"] == ["a01", "a02", "a03"]
    assert split["promptNames"] == ["reference_prompt", "reference_names", "p01", "p02"]
    assert split["deadNamesLabel"].endswith(split["suffix"])
    return

    script = rf"""
const {{ resolveBridgeOutputs, canonicalOutputOrder, FIXED_OUTPUT_NAMES, UNUSED_SUFFIX }} =
  await import({json.dumps(module_url)});
const order = canonicalOutputOrder();
const types = new Map(order.map((name) => [name, name === 'reference_idx' ? 'INT'
  : name === 'reference_strength' ? 'FLOAT'
  : name === 'reference_audio' ? 'AUDIO'
  : name.startsWith('reference_prompt') || name.startsWith('reference_names') ? 'STRING' : 'IMAGE']));
const metadata = new Map(order.map((name) => [name, {{ type: types.get(name) }}]));
const makeNode = (names) => ({{
  outputs: names.map((name) => ({{ name, type: types.get(name), link: null, links: [] }})),
  addOutput(name, type, opts) {{ this.outputs.push({{ name, type, link: null, links: [], ...opts }}); }},
  removeOutput(index) {{ this.outputs.splice(index, 1); }},
}});
// `meta` is overridable so a test can mimic ensureState() re-capturing metadata
// from already-marked live outputs after a graph reload.
const shape = (node, s, meta = metadata) => resolveBridgeOutputs(node, s, {{ metadata: meta, order }});
const describe = (node) => node.outputs.map((slot, i) => [i, slot.name, slot.type, slot.label ?? null]);
const results = {{}};

// The reported bug, exactly: a recipe driving only prompt/names/context/slots.
const lean = makeNode([...FIXED_OUTPUT_NAMES, 'r01']);
shape(lean, {{ slotCount: 1, liveOutputs: ['reference_prompt', 'reference_names', 'context', 'slots'] }});
results.lean = describe(lean);

// Numbered slots past the staged count are MARKED, never removed: the p-block
// sits behind the r-block, so trimming an r-slot used to slide p01 into an
// r-block position and deliver an image tensor from a STRING socket.
const trimmed = makeNode([...FIXED_OUTPUT_NAMES]);
shape(trimmed, {{ slotCount: 1, promptSlotCount: 1, liveOutputs: ['slots', 'reference_prompt'] }});
results.slotNames = trimmed.outputs.map((s) => s.name);
results.r02Label = trimmed.outputs.find((s) => s.name === 'r02').label;
results.p01Label = trimmed.outputs.find((s) => s.name === 'p01').label;
results.p02Label = trimmed.outputs.find((s) => s.name === 'p02').label;

// The five presets that drive per-member TEXT without touching the r-block:
// Ingredients, Best Face ID, VACE, Phantom and SCAIL. The p-block is gated on
// reference_prompt alone, so it must go live while every r-slot stays marked.
const promptOnly = makeNode([...FIXED_OUTPUT_NAMES]);
shape(promptOnly, {{ slotCount: 0, promptSlotCount: 2,
                     liveOutputs: ['reference_frames', 'reference_prompt', 'reference_names'] }});
results.promptOnly = ['p01', 'p02', 'p03', 'r01'].map(
  (n) => promptOnly.outputs.find((s) => s.name === n).label);

// Relaxing the COUNT on an absent promptSlotCount must not relax the LIVENESS.
// A recipe that genuinely omits reference_prompt still marks the whole p-block.
const noPromptLive = makeNode([...FIXED_OUTPUT_NAMES]);
shape(noPromptLive, {{ slotCount: 1, liveOutputs: ['slots', 'reference_names'] }});
results.noPromptLive = ['p01', 'p16'].map(
  (n) => noPromptLive.outputs.find((s) => s.name === n).label);

// A payload with no promptSlotCount at all is "we don't know", not "zero" —
// the old server field, or a response in flight across a version change.
const absentCount = makeNode([...FIXED_OUTPUT_NAMES]);
shape(absentCount, {{ slotCount: 0, liveOutputs: ['reference_prompt'] }});
results.absentCount = ['p01', 'p16', 'r01'].map(
  (n) => absentCount.outputs.find((s) => s.name === n).label);

// The audio lane: routes reports 0 because decode_reference_set's audio branch
// passes no slot_prompts, so the p-block really is empty and must say so.
const audioLane = makeNode([...FIXED_OUTPUT_NAMES]);
shape(audioLane, {{ slotCount: 0, promptSlotCount: 0,
                    liveOutputs: ['reference_audio', 'reference_prompt', 'reference_names'] }});
results.audioLane = ['p01', 'p02'].map(
  (n) => audioLane.outputs.find((s) => s.name === n).label);

// A connected dead FIXED output is still marked. Wiring says the user
// connected something, not that the recipe drives it — the slot emits a
// type-correct fallback into a live link, which is the case most worth
// naming. Only removal is gated on connection.
const wired = makeNode([...FIXED_OUTPUT_NAMES]);
wired.outputs.find((s) => s.name === 'reference_audio').links = [7];
shape(wired, {{ slotCount: 0, liveOutputs: ['reference_frames'] }});
results.wiredLabel = wired.outputs.find((s) => s.name === 'reference_audio').label;

// Reloading a graph captures metadata from the LIVE outputs, so a node saved
// while marked must not accumulate a second suffix on the next shape pass.
const reloaded = makeNode([...FIXED_OUTPUT_NAMES]);
shape(reloaded, {{ slotCount: 0, liveOutputs: ['reference_frames'] }});
const reloadedMeta = new Map(reloaded.outputs.map((s) => [
  s.name, {{ type: s.type, label: s.label, localized_name: s.localized_name }},
]));
shape(reloaded, {{ slotCount: 0, liveOutputs: ['reference_frames'] }}, reloadedMeta);
results.reloadedLabel = reloaded.outputs.find((s) => s.name === 'reference_audio').label;
// And the mark still clears when the recipe revives the output.
shape(reloaded, {{ slotCount: 0, liveOutputs: null }}, reloadedMeta);
results.reloadedRevived = reloaded.outputs.find((s) => s.name === 'reference_audio').label;

// No declaration: everything present and nothing marked.
const unknown = makeNode([...FIXED_OUTPUT_NAMES]);
shape(unknown, {{ slotCount: 0, liveOutputs: null }});
results.unknownLabels = unknown.outputs.slice(0, FIXED_OUTPUT_NAMES.length).map((s) => s.label);
results.unknownSlotLabels = ['r01', 'r16', 'p01', 'p16']
  .map((n) => unknown.outputs.find((s) => s.name === n).label);

// The reported `slots` serve mode: reference_frames is dead and unwired, so it
// must read as unused on BOTH renderers - label for legacy, localized_name for
// Nodes 2.0. Setting only one leaves the other showing the bare name.
const slotsMode = makeNode([...FIXED_OUTPUT_NAMES, 'r01']);
shape(slotsMode, {{ slotCount: 1, liveOutputs: ['slots', 'reference_prompt', 'reference_names'] }});
const frames = slotsMode.outputs.find((s) => s.name === 'reference_frames');
results.slotsMode = [frames.label, frames.localized_name];

// Marks clear again when the recipe changes back.
const revived = makeNode([...FIXED_OUTPUT_NAMES]);
shape(revived, {{ slotCount: 0, liveOutputs: ['reference_frames'] }});
const markedFixed = () => revived.outputs
  .slice(0, FIXED_OUTPUT_NAMES.length)
  .filter((s) => String(s.label).endsWith(UNUSED_SUFFIX)).length;
const markedCount = markedFixed();
shape(revived, {{ slotCount: 0, liveOutputs: null }});
results.markCycle = [markedCount, markedFixed()];

// Idempotent.
const stable = makeNode([...FIXED_OUTPUT_NAMES]);
shape(stable, {{ slotCount: 2, promptSlotCount: 2, liveOutputs: ['reference_frames'] }});
results.secondRunChanged = shape(stable, {{ slotCount: 2, promptSlotCount: 2,
                                            liveOutputs: ['reference_frames'] }});

console.log(JSON.stringify(results));
"""
    out = json.loads(subprocess.run(
        [node_bin, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)

    # Every fixed output keeps its canonical index and its declared type.
    for index, name in enumerate(FIXED_OUTPUT_NAMES):
        assert out["lean"][index][0] == index
        assert out["lean"][index][1] == name
    assert out["lean"][3][1:3] == ["reference_audio", "AUDIO"], "index 3 must stay AUDIO"
    assert out["lean"][7][1:3] == ["r01", "IMAGE"], "r01 must sit at index 7, not 3"
    # Dead ones read as unused; live ones read normally.
    assert out["lean"][3][3] == "reference_audio (unused)"
    assert out["lean"][0][3] == "reference_frames (unused)"
    assert out["lean"][4][3] == "reference_prompt"

    # Every declared output is present, in canonical order, at every shape.
    assert out["slotNames"] == FIXED_OUTPUT_NAMES + SLOT_NAMES + PROMPT_SLOT_NAMES
    # One staged member: slot 1 is live, slot 2 reads unused on both blocks.
    assert out["r02Label"] == "r02 (unused)" and out["p02Label"] == "p02 (unused)"
    assert out["p01Label"] == "p01"
    # Rule 2 still holds for the numbered blocks: with no liveness declaration
    # every slot shows unmarked rather than looking broken during a slow load.
    assert out["unknownSlotLabels"] == ["r01", "r16", "p01", "p16"]
    # Wiring gates REMOVAL, not marking. A wired output the recipe does not
    # drive still emits its fallback into that link, so it must say so —
    # `reference_frames` is wired in almost every real workflow and was
    # therefore the one output the old connection guard could never mark.
    assert out["wiredLabel"] == "reference_audio (unused)", "a wired dead output still marks"
    # Re-capturing metadata from marked outputs must not stack suffixes.
    assert out["reloadedLabel"] == "reference_audio (unused)", "no doubled suffix after reload"
    assert out["reloadedRevived"] == "reference_audio", "reload-captured marks still clear"
    assert out["unknownLabels"] == FIXED_OUTPUT_NAMES
    assert out["slotsMode"] == ["reference_frames (unused)", "reference_frames (unused)"]
    assert out["markCycle"] == [6, 0], "marks must clear when liveness is unknown again"
    assert out["secondRunChanged"] is False

    # The p-block is gated on reference_prompt ALONE. Five presets drive
    # per-member text without the r-block; folding both into one count marked
    # real output unused (nodes/reference_core.py fills p01..pN whenever
    # reference_prompt is live, independent of slots).
    assert out["promptOnly"] == ["p01", "p02", "p03 (unused)", "r01 (unused)"]
    # Absent means "we don't know", so only the COUNT relaxes. Liveness is
    # untouched: a recipe omitting reference_prompt still marks the whole block.
    assert out["noPromptLive"] == ["p01 (unused)", "p16 (unused)"]
    assert out["absentCount"] == ["p01", "p16", "r01 (unused)"]
    # An audio lane's p-block genuinely is empty and must keep saying so.
    assert out["audioLane"] == ["p01 (unused)", "p02 (unused)"]


def test_bridge_shape_module_stays_free_of_browser_imports():
    """It is a separate module so these rules are testable without a browser."""
    source = (ROOT / "web" / "js" / "reference_bridge_shape.js").read_text(encoding="utf-8")
    assert "/scripts/app.js" not in source and "/scripts/api.js" not in source
    assert not re.search(r"\bdocument\.", source) and not re.search(r"\bwindow\.", source)
    bridge = (ROOT / "web" / "js" / "reference_bridge.js").read_text(encoding="utf-8")
    assert 'from "./reference_bridge_shape.js"' in bridge
    assert bridge.count("FULL_SHAPE") >= 5


# One fixture set, resolved in Python and in node. A lane of two items where the
# window clips one of them, swept across thresholds.
_THRESHOLD_ITEMS = [
    {"reference_item_id": "wide", "lane_index": 0, "start_frame": 0, "end_frame": 100, "members": [{"member_id": "m"}]},
    {"reference_item_id": "narrow", "lane_index": 0, "start_frame": 40, "end_frame": 60, "members": [{"member_id": "m"}]},
    {"reference_item_id": "other", "lane_index": 1, "start_frame": 0, "end_frame": 10, "members": [{"member_id": "m"}]},
]
_THRESHOLD_CASES = [
    {"windowStart": 40, "windowEnd": 60, "frameThresholdPct": 0},
    {"windowStart": 40, "windowEnd": 60, "frameThresholdPct": 50},
    {"windowStart": 40, "windowEnd": 60, "frameThresholdPct": 100},
    # The window clips only a sliver of both items on lane 0.
    {"windowStart": 55, "windowEnd": 62, "frameThresholdPct": 0},
    {"windowStart": 55, "windowEnd": 62, "frameThresholdPct": 30},
    {"windowStart": 55, "windowEnd": 62, "frameThresholdPct": 90},
    {"windowStart": 0, "windowEnd": 100, "frameThresholdPct": 25},
]


def test_reference_threshold_matches_between_python_and_javascript():
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node is required for the threshold parity test")
    from server.reference_resolution import resolve_effective_references

    expected = []
    for case in _THRESHOLD_CASES:
        winners = resolve_effective_references(
            reference_items=[dict(item) for item in _THRESHOLD_ITEMS],
            lane_count=2, scene_duration=100,
            window_start=case["windowStart"], window_end=case["windowEnd"],
            lane_configs=[], frame_threshold_pct=case["frameThresholdPct"],
        )
        expected.append([
            (winner or {}).get("item", {}).get("reference_item_id") if winner else None
            for winner in winners
        ])

    module_url = (ROOT / "web" / "js" / "reference_resolution.js").as_uri()
    script = f"""
const {{ resolveEffectiveReferences }} = await import({json.dumps(module_url)});
const items = {json.dumps(_THRESHOLD_ITEMS)};
const rows = {json.dumps(_THRESHOLD_CASES)}.map((c) =>
  resolveEffectiveReferences({{
    referenceItems: items, laneCount: 2, sceneDuration: 100,
    windowStart: c.windowStart, windowEnd: c.windowEnd,
    laneConfigs: [], frameThresholdPct: c.frameThresholdPct,
  }}).map((w) => (w ? w.item.reference_item_id : null)));
console.log(JSON.stringify(rows));
"""
    actual = json.loads(subprocess.run(
        [node_bin, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)
    assert actual == expected

    # The fixtures must actually exercise the behaviour, not agree vacuously.
    # Off: most-specific-wins picks the tightly-scoped item.
    assert expected[0][0] == "narrow"
    # A threshold above the clipped coverage empties the lane entirely — the
    # deliberate difference from the prompt rule, which always keeps one.
    assert expected[5][0] is None, "a high threshold must be able to leave a lane with nothing"
    assert any(row[0] is None for row in expected), "expected at least one empty resolution"
    assert any(row[0] is not None for row in expected), "expected at least one live resolution"


def test_reference_threshold_is_project_durable_and_frozen_at_enqueue():
    routes_source = (ROOT / "server" / "routes.py").read_text(encoding="utf-8")
    core = (ROOT / "nodes" / "reference_core.py").read_text(encoding="utf-8")
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    panel = (ROOT / "web" / "js" / "editor_settings_panel.js").read_text(encoding="utf-8")

    # Frozen into job params for reproducibility, exactly like the prompt one.
    assert 'params["reference_frame_threshold"] = reference_threshold' in routes_source
    # A queued job reads the frozen value; live resolution reads the project.
    assert '(getattr(job, "params", {}) or {}).get("reference_frame_threshold"' in core
    assert '(getattr(project, "metadata", {}) or {}).get("reference_frame_threshold"' in core
    assert 'frame_threshold_pct=source["frame_threshold_pct"]' in core
    # Authored in Settings as a project-wide value, not a browser preference.
    assert 'metadata: { reference_frame_threshold: pct }' in widget
    assert "Reference Threshold % (project-wide)" in panel
    # A mid-batch has_reference flip is announced rather than silent. Both
    # causes are announced, under separate sources: a threshold drop is a
    # setting the user probably did not mean to hit, a scope drop is the feature
    # working. Silencing either would hide a mid-batch task-mode change.
    assert "_warnOnReferenceFlipAcrossBatch(chunks)" in widget
    assert 'source: "reference-batch-flip"' in widget
    assert 'source: "reference-batch-scope"' in widget
    # A threshold above the per-chunk coverage drops the lane from EVERY chunk,
    # so nothing flips and the flip check alone stayed silent.
    assert 'source: "reference-batch-silenced"' in widget
    # The remedy is chosen from the lane's own verdicts, never from the global
    # setting — that is what told an out-of-range lane to lower a threshold it
    # had never touched.
    assert "shared.frameThresholdPct > 0" not in widget
    assert "classifyReferenceChunks(chunks, shared)" in widget


def test_reference_verdicts_report_why_each_item_did_or_did_not_resolve():
    """The timeline needs the reason, not just the winner.

    Superseded and below-threshold have different remedies — restage versus
    lower the setting — so one label for both would send the user to the wrong
    fix. `resolveEffectiveReferences` derives its winners from this core, so the
    JS-vs-Python parity test above also guards the scoring here.
    """
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node is required for the verdict test")
    items = [
        # Lane 0: `narrow` sits inside `wide`, so a window over it wins on coverage.
        {"reference_item_id": "wide", "lane_index": 0, "start_frame": 0, "end_frame": 100},
        {"reference_item_id": "narrow", "lane_index": 0, "start_frame": 40, "end_frame": 60},
        {"reference_item_id": "elsewhere", "lane_index": 0, "start_frame": 80, "end_frame": 100},
        {"reference_item_id": "muted", "lane_index": 0, "start_frame": 40, "end_frame": 60, "muted": True},
        # Lane 1 is hidden, so nothing on it participates.
        {"reference_item_id": "on_hidden_lane", "lane_index": 1, "start_frame": 40, "end_frame": 60},
    ]
    module_url = (ROOT / "web" / "js" / "reference_resolution.js").as_uri()
    script = f"""
const {{ resolveReferenceVerdicts, resolveEffectiveReferences, REFERENCE_VERDICT_LABEL }} =
  await import({json.dumps(module_url)});
const items = {json.dumps(items)};
const shared = {{ referenceItems: items, laneCount: 2, sceneDuration: 100, laneConfigs: [{{}}, {{ hidden: true }}] }};
const named = (result) => Object.fromEntries(
  [...result.verdicts].map(([index, verdict]) => [items[index].reference_item_id, verdict]));
const plain = resolveReferenceVerdicts({{ ...shared, windowStart: 40, windowEnd: 60 }});
console.log(JSON.stringify({{
  plain: named(plain),
  // Same window, threshold above `wide`'s 20% coverage but under `narrow`'s 100%.
  thresholded: named(resolveReferenceVerdicts({{ ...shared, windowStart: 40, windowEnd: 60, frameThresholdPct: 50 }})),
  // Threshold above every candidate: the lane resolves to nothing at all.
  emptied: named(resolveReferenceVerdicts({{ ...shared, windowStart: 55, windowEnd: 62, frameThresholdPct: 90 }})),
  labels: REFERENCE_VERDICT_LABEL,
  // The wrapper must still publish exactly the winner shape callers expect.
  wrapperShape: resolveEffectiveReferences({{ ...shared, windowStart: 40, windowEnd: 60 }})
    .map((w) => (w ? Object.keys(w).sort().join(",") : null)),
}}));
"""
    out = json.loads(subprocess.run(
        [node_bin, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)

    # All five outcomes, from one window.
    assert out["plain"] == {
        "wide": "superseded",          # overlaps but loses most-specific-wins
        "narrow": "winner",
        "elsewhere": "outside",        # no overlap with the window
        "muted": "excluded",
        "on_hidden_lane": "excluded",  # hidden lane does not participate
    }
    # The threshold removes a candidate before scoring, so its reason changes
    # from "another item won" to "the setting dropped it".
    assert out["thresholded"]["wide"] == "below_threshold"
    assert out["thresholded"]["narrow"] == "winner"
    # Nothing survives: no winner is invented to fill the lane.
    assert set(out["emptied"].values()) <= {"below_threshold", "outside", "excluded"}
    assert "winner" not in out["emptied"].values()

    assert out["labels"]["superseded"] == "Superseded"
    assert out["labels"]["below_threshold"] == "Below threshold"
    assert out["wrapperShape"] == ["item,laneIndex", None]


def test_verdict_marks_share_one_vocabulary_and_need_no_selection_guard_of_their_own():
    """Timeline and panel must read as one concept, resolved the same way."""
    canvas = (ROOT / "web" / "js" / "editor_timeline_canvas.js").read_text(encoding="utf-8")
    panel = (ROOT / "web" / "js" / "editor_reference_panel.js").read_text(encoding="utf-8")
    resolution = (ROOT / "web" / "js" / "reference_resolution.js").read_text(encoding="utf-8")

    # Labels live once, in the resolver, and both surfaces render from them
    # rather than hard-coding their own strings.
    assert '"Superseded"' in resolution and '"Below threshold"' in resolution
    for surface in (canvas, panel):
        assert "REFERENCE_VERDICT_LABEL" in surface
        assert "Superseded" not in surface.replace("REFERENCE_VERDICT_LABEL", "")

    # No selection means no marks, in both surfaces, via the same accessor the
    # prompt-usage highlight uses.
    assert "const range = host._selectionContextRange?.();\n    if (!range) return null;" in canvas
    assert "const range = host._selectionContextRange?.();" in panel
    assert "if (!scene || !range) return byId;" in panel

    # The hatch reuses the shared muted overlay rather than a second bespoke one,
    # and the winner accent mirrors the prompt lane's fillRect bar.
    reference_block = canvas.split("Reference items are source-less timeline scopes", 1)[1]
    assert "host._drawMutedOverlay(" in reference_block
    assert "ctx.fillStyle = COLORS.accent;" in reference_block
    # A muted/hidden item already carries its own overlay; stacking a second
    # would double-darken it.
    assert "} else if (!hidden" in reference_block


def test_pegged_fields_display_what_the_render_will_use():
    panel = (ROOT / "web" / "js" / "editor_reference_panel.js").read_text(encoding="utf-8")
    numeric = panel.split('if (field.type === "int" || field.type === "number") {', 1)[1].split("return input;", 1)[0]
    # The resolved value wins over the stored one, and the control is inert.
    assert "peg?.resolved ?? value ?? field.default" in numeric
    assert "input.readOnly = true;" in panel
    # The authored number is still what the assembler falls back to, so it must
    # remain in the recipe rather than being overwritten by the display.
    assert "fallback: hard[field.key] ?? field.default" in panel


def test_threshold_batch_warnings_name_the_lane_the_count_and_the_right_remedy():
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    # Bounded by the next method's NAME rather than by `\n    async `: that
    # delimiter silently widened the block whenever a non-async method was added
    # after this one, so assertions could pass against a neighbour's source.
    start = widget.index("_warnOnReferenceFlipAcrossBatch(chunks) {")
    block = widget[start:widget.index("_setReferenceFrameThreshold(", start)]
    assert "of ${total}" in block
    assert "dropped from all ${total} chunks" in block
    # Each cause carries its own remedy and its own count.
    assert "Threshold in Settings, or widen the staged item." in block
    assert "Lower it in Settings, or widen the staged item." in block
    assert "does not overlap this batch" in block
    assert "Unmute the item or unhide the " in block
    # Scope drops are announced too, but transiently — they are the feature
    # working, not a setting the user tripped over.
    assert "notifyInfo(" in block
    assert 'source: "reference-batch-scope"' in block


def test_batch_chunk_classifier_separates_threshold_scope_and_excluded():
    """The warning needs the CAUSE, not just "did the lane resolve".

    `resolveEffectiveReferences` answers only the latter, which is why a
    deliberately scoped item and a threshold drop produced the same message.
    This lives in `reference_resolution.js` rather than on the editor class
    because `editor_widget.js` cannot be imported into node — a substring
    assertion cannot reach any of the behaviour below.
    """
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node is required for the classifier test")
    thirds = [{"start": 0, "end": 100}, {"start": 100, "end": 200}, {"start": 200, "end": 300}]
    uneven = [{"start": 0, "end": 150}, {"start": 150, "end": 200}, {"start": 200, "end": 300}]
    cases = {
        # Scoped to the first chunk only: overlaps nothing later, threshold off.
        "scope": (thirds, [{"lane_index": 0, "start_frame": 0, "end_frame": 100}], 0),
        # Spans the whole batch, but uneven chunks cover too little of its span.
        "threshold": (uneven, [{"lane_index": 0, "start_frame": 0, "end_frame": 300}], 40),
        # Both causes on ONE lane: wins chunk 1, thresholded in 2, absent in 3.
        "mixed": (thirds, [{"lane_index": 0, "start_frame": 0, "end_frame": 150}], 40),
        # Deliberate silence: muted everywhere, so it can never flip.
        "muted": (thirds, [{"lane_index": 0, "start_frame": 0, "end_frame": 300, "muted": True}], 0),
        # lane_index spellings the scorer normalizes: undefined -> 0, "2" -> 2, 1.7 -> 1.
        "lanes": (thirds, [
            {"start_frame": 0, "end_frame": 300},
            {"lane_index": "2", "start_frame": 0, "end_frame": 300},
            {"lane_index": 1.7, "start_frame": 0, "end_frame": 300},
        ], 0),
    }
    module_url = (ROOT / "web" / "js" / "reference_resolution.js").as_uri()
    script = f"""
const {{ classifyReferenceChunks }} = await import({json.dumps(module_url)});
const cases = {json.dumps(cases)};
const out = {{}};
for (const [name, [chunks, items, threshold]] of Object.entries(cases)) {{
  out[name] = classifyReferenceChunks(chunks, {{
    referenceItems: items,
    laneCount: name === 'lanes' ? 3 : 1,
    sceneDuration: 300,
    laneConfigs: [],
    frameThresholdPct: threshold,
  }});
}}
console.log(JSON.stringify(out));
"""
    out = json.loads(subprocess.run(
        [node_bin, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)

    scope = out["scope"][0]
    assert scope["resolved"] == 1 and scope["causeCounts"]["outside"] == 2
    assert scope["causeCounts"]["below_threshold"] == 0, "no threshold is involved when it is off"

    threshold = out["threshold"][0]
    assert threshold["resolved"] == 1 and threshold["causeCounts"]["below_threshold"] == 2
    assert threshold["causeCounts"]["outside"] == 0, "it overlaps every chunk; only coverage fails"

    # One lane, two causes, counted separately — a single tally would put a
    # number in a sentence that does not explain it.
    mixed = out["mixed"][0]
    assert mixed["resolved"] == 1
    assert mixed["causeCounts"]["below_threshold"] == 1
    assert mixed["causeCounts"]["outside"] == 1
    assert mixed["dominantCause"] == "below_threshold", "the actionable cause ranks first"

    muted = out["muted"][0]
    assert muted["staged"] is True, "a muted item is still staged; the lane is not empty"
    assert muted["resolved"] == 0 and muted["causeCounts"]["excluded"] == 3
    assert muted["dominantCause"] == "excluded"

    # All three spellings must land where the SCORER put them, or a resolved
    # lane reads unresolved and warns about nothing.
    assert [lane["staged"] for lane in out["lanes"]] == [True, True, True]
    assert [lane["resolved"] for lane in out["lanes"]] == [3, 3, 3]
