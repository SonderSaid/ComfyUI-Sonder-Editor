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
    read_keys = set(re.findall(r'hard(?:_wrapper)?\.get\("([a-z_]+)"', core))
    read_keys.update(re.findall(r'soft\.get\("([a-z_]+)"', core))
    read_keys.update(re.findall(r'\b(?:hard|soft)\.([a-z_]+)\b', widget))
    read_keys.update(re.findall(r'\bhard\.([a-z_]+)\b', panel))
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
    assert "context_slot" in temporal and "context_slot" not in batch
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


def test_selector_panel_view_keeps_an_orphaned_lane_visible():
    """The dropdown must never read as a lane other than the INT actually holds."""
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node is required for the selector view test")
    lanes = [
        {"lane_index": 0, "lane_name": "Reference 1", "recipe_name": "Wan VACE Reference Sheet",
         "media_kind": "image", "item_count": 2, "member_count": 3, "hidden": False,
         "live_outputs": ["reference_frames", "reference_prompt", "slots"]},
        {"lane_index": 1, "lane_name": "Voices", "recipe_name": "LTX ID-LoRA Voice Identity",
         "media_kind": "audio", "item_count": 1, "member_count": 1, "hidden": True,
         "live_outputs": ["reference_audio"]},
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
    assert out["first"]["outputs"] == ["reference_frames", "reference_prompt", "r01..r16"]
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


FIXED_OUTPUT_NAMES = [name for name in REFERENCE_OUTPUT_NAMES if name != "slots"]
SLOT_NAMES = [f"r{index:02d}" for index in range(1, 17)]


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
const shape = (node, s) => resolveBridgeOutputs(node, s, {{ metadata, order }});
const describe = (node) => node.outputs.map((slot, i) => [i, slot.name, slot.type, slot.label ?? null]);
const results = {{}};

// The reported bug, exactly: a recipe driving only prompt/names/context/slots.
const lean = makeNode([...FIXED_OUTPUT_NAMES, 'r01']);
shape(lean, {{ slotCount: 1, liveOutputs: ['reference_prompt', 'reference_names', 'context', 'slots'] }});
results.lean = describe(lean);

// The r-block still trims, because it is a tail.
const trimmed = makeNode([...FIXED_OUTPUT_NAMES, 'r01', 'r02', 'r03']);
shape(trimmed, {{ slotCount: 1, liveOutputs: null }});
results.trimmedSlots = trimmed.outputs.filter((s) => /^r\d\d$/.test(s.name)).map((s) => s.name);

// A connected dead output is not even marked — it is in active use.
const wired = makeNode([...FIXED_OUTPUT_NAMES]);
wired.outputs.find((s) => s.name === 'reference_audio').links = [7];
shape(wired, {{ slotCount: 0, liveOutputs: ['reference_frames'] }});
results.wiredLabel = wired.outputs.find((s) => s.name === 'reference_audio').label;

// No declaration: everything present and nothing marked.
const unknown = makeNode([...FIXED_OUTPUT_NAMES]);
shape(unknown, {{ slotCount: 0, liveOutputs: null }});
results.unknownLabels = unknown.outputs.map((s) => s.label);

// Marks clear again when the recipe changes back.
const revived = makeNode([...FIXED_OUTPUT_NAMES]);
shape(revived, {{ slotCount: 0, liveOutputs: ['reference_frames'] }});
const markedCount = revived.outputs.filter((s) => String(s.label).endsWith(UNUSED_SUFFIX)).length;
shape(revived, {{ slotCount: 0, liveOutputs: null }});
results.markCycle = [markedCount, revived.outputs.filter((s) => String(s.label).endsWith(UNUSED_SUFFIX)).length];

// Idempotent.
const stable = makeNode([...FIXED_OUTPUT_NAMES]);
shape(stable, {{ slotCount: 2, liveOutputs: ['reference_frames'] }});
results.secondRunChanged = shape(stable, {{ slotCount: 2, liveOutputs: ['reference_frames'] }});

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

    assert out["trimmedSlots"] == ["r01"]
    assert out["wiredLabel"] == "reference_audio", "a wired output is in use, not unused"
    assert out["unknownLabels"] == FIXED_OUTPUT_NAMES
    assert out["markCycle"] == [6, 0], "marks must clear when liveness is unknown again"
    assert out["secondRunChanged"] is False


def test_bridge_shape_module_stays_free_of_browser_imports():
    """It is a separate module so these rules are testable without a browser."""
    source = (ROOT / "web" / "js" / "reference_bridge_shape.js").read_text(encoding="utf-8")
    assert "/scripts/app.js" not in source and "/scripts/api.js" not in source
    assert not re.search(r"\bdocument\.", source) and not re.search(r"\bwindow\.", source)
    bridge = (ROOT / "web" / "js" / "reference_bridge.js").read_text(encoding="utf-8")
    assert 'from "./reference_bridge_shape.js"' in bridge
    assert bridge.count("FULL_SHAPE") >= 5
