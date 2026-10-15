"""Relay manifest hygiene, the Relay census, the binding denylist and options providers."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from server import binding_policy as bp, control_manifest as cm

ROOT = Path(__file__).resolve().parents[1]


def _relay(manifest_text):
    return {"class_type": "SonderControlRelay", "inputs": {"manifest": manifest_text}}


# ── Parsing and hygiene ──────────────────────────────────────────────────────

def test_empty_and_saved_manifests_parse():
    assert cm.parse_manifest("") == cm.empty_manifest() == cm.parse_manifest(None)
    saved = {"v": 1, "bindings": {"aB-_9": {"label": " Steps ", "group": "Look", "order": -3, "advanced": True,
                                            "note": "**n**", "section": "editor"}},
             "notes": {"n1": {"text": "# Hi", "group": "", "order": 2}}, "groups": {"Look": {"order": 1}}}
    parsed = cm.parse_manifest(json.dumps(saved))
    binding = parsed["bindings"]["aB-_9"]
    assert (binding["label"], binding["section"], binding["advanced"], binding["targets"]) == ("Steps", "editor", True, [])
    assert "status" not in binding and binding["seed_mode"] is None
    assert parsed["notes"]["n1"] == {"text": "# Hi", "group": "", "order": 2}
    assert parsed["groups"] == {"Look": {"order": 1}}


def test_export_facts_are_read_and_an_unknown_status_reads_as_missing():
    exported = {"bindings": {"a": {"status": "ok", "targets": [{"path": "4:2", "input": "seed"}], "seed_mode": "increment"},
                             "b": {"status": "weird"}},
                "suggestions": [{"binding_id": "z", "targets": [{"path": "7", "input": "cfg"}]}]}
    parsed = cm.parse_manifest(json.dumps(exported))
    assert parsed["bindings"]["a"]["targets"] == [{"path": "4:2", "input": "seed"}]
    assert parsed["bindings"]["a"]["seed_mode"] == "increment"
    assert parsed["bindings"]["b"]["status"] == "missing"
    assert parsed["suggestions"] == [{"binding_id": "z", "targets": [{"path": "7", "input": "cfg"}]}]


@pytest.mark.parametrize("text, reason", [
    ("[]", "must be an object"),
    ("{nope", "not valid JSON"),
    ("[" * 50000 + "]" * 50000, "nested too deeply"),
    (json.dumps({"bindings": {"a": {"label": {"x": {"y": {"z": {"w": {"v": 1}}}}}}}}), "nested too deeply"),
    ('{"__proto__": {}}', "reserved key"),
    (json.dumps({"bindings": {"constructor": {}}}), "reserved key"),
    (json.dumps({"bindings": {"bad id!": {}}}), "id is invalid"),
    (json.dumps({"bindings": {"a" * 65: {}}}), "id is invalid"),
    (json.dumps({"notes": {"../x": {}}}), "id is invalid"),
    (json.dumps({"bindings": {"a": {"label": "x" * 121}}}), "longer than 120"),
    (json.dumps({"bindings": {"a": {"note": "x" * 4001}}}), "longer than 4000"),
    (json.dumps({"bindings": {"a": {"order": 10000}}}), "-9999 to 9999"),
    (json.dumps({"bindings": {"a": {"order": True}}}), "-9999 to 9999"),
    (json.dumps({"bindings": {"a": {"advanced": "yes"}}}), "true or false"),
    (json.dumps({"bindings": {"a": {"targets": [{"path": 3, "input": "x"}]}}}), "invalid targets"),
    (json.dumps({"bindings": {"a": {"targets": [{"path": "1", "input": "x"}] * 65}}}), "invalid targets"),
    (json.dumps({"bindings": {f"b{i}": {} for i in range(257)}}), "at most 256"),
    (json.dumps({"notes": {f"n{i}": {} for i in range(257)}}), "at most 256"),
    (json.dumps({"v": 2}), "newer Sonder"),
    ("x" * (cm.MAX_MANIFEST_BYTES + 1), "too large"),
], ids=lambda value: str(value)[:40])
def test_hostile_or_oversized_manifests_are_refused(text, reason):
    with pytest.raises(cm.ManifestError, match=reason):
        cm.parse_manifest(text)


def test_a_stored_section_is_read_tolerantly_because_it_is_derived():
    # Phase 4: section_for derives a binding's section from its nodes; the
    # stored key is only a fallback and never makes the Relay unreadable.
    parsed = cm.parse_manifest(json.dumps({"bindings": {"a": {"section": "sidebar"}, "b": {"section": "editor"}}}))
    assert parsed["bindings"]["a"]["section"] == "workflow"
    assert parsed["bindings"]["b"]["section"] == "editor"


def test_the_byte_cap_counts_utf8_before_parsing():
    text = json.dumps({"notes": {"n": {"text": "é" * 3000}}}, ensure_ascii=False)
    assert len(text) < cm.MAX_MANIFEST_BYTES
    many = json.dumps({"notes": {f"n{i}": {"text": "é" * 3900} for i in range(20)}}, ensure_ascii=False)
    assert len(many) < cm.MAX_MANIFEST_BYTES < len(many.encode("utf-8"))
    with pytest.raises(cm.ManifestError, match="too large"):
        cm.parse_manifest(many)


# ── Census ───────────────────────────────────────────────────────────────────

def test_find_relay_census():
    good = _relay(json.dumps({"bindings": {"a": {}}}))
    assert cm.find_relay({"1": {"class_type": "KSampler", "inputs": {}}})["status"] == "none"
    found = cm.find_relay({"5": good})
    assert (found["status"], found["key"], list(found["manifest"]["bindings"])) == ("ok", "5", ["a"])
    assert "more than one" in cm.find_relay({"5": good, "6": good})["error"]
    assert "inside a subgraph" in cm.find_relay({"2:5": good})["error"]
    broken = cm.find_relay({"5": _relay("{")})
    assert broken["status"] == "error" and broken["key"] == "5" and "not valid JSON" in broken["error"]
    assert "the resolver failed" in cm.find_relay({"5": _relay(json.dumps({"error": "the resolver failed"}))})["error"]


@pytest.mark.parametrize("census, expected", [
    (None, None), ([], None), ({"active": 1}, None),
    ({"active": 2}, "more than one"), ({"active": 1, "nested": 1}, "inside a subgraph"),
    ({"active": 0, "muted": 1}, "muted or bypassed"), ({"bypassed": 2}, "muted or bypassed"),
    ({"active": 1, "muted": 1}, None), ({"active": True, "muted": "1"}, None),
])
def test_census_error(census, expected):
    error = cm.census_error(census)
    assert (error is None) == (expected is None)
    if expected:
        assert expected in error


# ── Denylist and providers ───────────────────────────────────────────────────

@pytest.mark.parametrize("class_type, name", [
    ("SonderEditor", "project"), ("SonderEditor", "selection_start"), ("SonderEditor", "render_cache_max_bytes"),
    ("SonderEditor", "take_placement_muted"), ("SonderGuidesBridgeStart", "iteration_index"),
    ("SonderDriverSelector", "driver_selector_overrides_json"), ("SonderSaveBridge", "target_folder"),
    ("SonderReferenceSelector", "reference_lanes"), ("SonderMetadataCollector", "label_3"),
    ("SonderMetadataCollectorV3", "label_12"), ("SonderControlRelay", "manifest"),
    ("KSampler", None), ("KSampler", 3),
])
def test_denied_inputs(class_type, name):
    assert bp.is_denied(class_type, name)


@pytest.mark.parametrize("class_type, name", [
    ("SonderEditor", "fps"), ("SonderEditor", "width"), ("SonderSaveVideo", "mark_queue_complete"),
    ("SonderSaveBridge", "prefix"), ("SonderMetadataCollector", "capacity"), ("KSampler", "seed"),
    # A real STRING widget named `prompt` (text encoders) is bindable; hidden
    # inputs never reach a prompt and describe as unsupported instead.
    ("TextEncodeQwenImageEdit", "prompt"), ("MinimaxH3TextEncode", "prompt"),
])
def test_allowed_inputs(class_type, name):
    assert not bp.is_denied(class_type, name)


def test_runner_owned_inputs_cover_every_editor_input_the_runner_writes():
    from server.sdk_templates import EDITOR_STATE_FIELDS
    denied = set(bp.denied_inputs()["SonderEditor"])
    assert {"project", "render_cache_max_bytes", *EDITOR_STATE_FIELDS} <= denied


def test_frontend_denylist_mirrors_the_server():
    source = (ROOT / "web/js/control_bindings.js").read_text(encoding="utf-8")
    block = re.search(r"export const DENIED_INPUTS = \{(.*?)\n\};", source, re.S).group(1)
    mirrored = {name: sorted(re.findall(r'"([^"]+)"', values))
                for name, values in re.findall(r"(\w+): \[(.*?)\]", block, re.S)}
    assert mirrored == bp.denied_inputs()
    prefixes = dict(re.findall(r'(\w+): "(\w+)"', re.search(r"DENIED_PREFIXES = \{(.*?)\};", source).group(1)))
    assert prefixes == bp.denied_prefixes()


def test_editor_classes_mirror_the_frontend():
    from server import control_descriptors as cd
    source = (ROOT / "web/js/control_bindings.js").read_text(encoding="utf-8")
    block = re.search(r"export const EDITOR_CLASSES = new Set\(\[(.*?)\]\);", source).group(1)
    assert set(re.findall(r'"([^"]+)"', block)) == set(cd.EDITOR_CLASSES)


GROUP_ORDER_FIXTURES = [
    {"groups": {}, "bindings": {"a": {"label": "A", "order": 2, "group": "Late"},
                                "b": {"label": "B", "order": 1, "group": "Early"}},
     "notes": {"n": {"text": "x", "order": 0, "group": "Notes"}}},
    {"groups": {"Models": {"order": 5}, "Output": {"order": -1}, "Zed": {}},
     "bindings": {"a": {"label": "Steps", "order": 0, "group": "Sampling"},
                  "b": {"label": "Model", "order": 0, "group": "Models"},
                  "c": {"label": "alpha", "order": 0, "group": "First"}}, "notes": {}},
]


@pytest.mark.parametrize("fixture", GROUP_ORDER_FIXTURES)
def test_group_order_mirrors_the_frontend(fixture):
    # One authority for group order: the server's group_order, mirrored by the
    # Relay rows' effectiveGroupOrder (no server call on the canvas).
    from server import control_descriptors as cd
    node = shutil.which("node")
    if not node:
        pytest.skip("node required for the frontend mirror")
    view = {"groups": fixture["groups"],
            "bindings": [{"binding_id": k, **v} for k, v in fixture["bindings"].items()],
            "notes": [{"note_id": k, **v} for k, v in fixture["notes"].items()]}
    script = ("import { effectiveGroupOrder } from " + json.dumps((ROOT / "web/js/control_bindings.js").as_uri())
              + "; console.log(JSON.stringify(effectiveGroupOrder(" + json.dumps(fixture) + ")));")
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == cd.group_order(view)


def test_manifest_caps_mirror_the_frontend():
    source = (ROOT / "web/js/control_bindings.js").read_text(encoding="utf-8")
    def constant(name):
        return re.search(rf"export const {name} = ([^;]+);", source).group(1)
    assert eval(constant("MAX_MANIFEST_BYTES")) == cm.MAX_MANIFEST_BYTES
    for name in ("MAX_BINDINGS", "MAX_NOTES", "MAX_SUGGESTIONS", "MAX_TARGETS", "MAX_DEPTH", "MAX_GROUPS"):
        assert int(constant(name)) == getattr(cm, name), name
    assert int(constant("LABEL_MAX")) == cm.LABEL_MAX and int(constant("NOTE_MAX")) == cm.NOTE_MAX
    assert constant("ID_RE") == "/" + cm.ID_RE.pattern + "/"
    assert constant("RELAY_CLASS") == json.dumps(cm.RELAY_CLASS_TYPE)
    targets = (ROOT / "web/js/control_targets.js").read_text(encoding="utf-8")
    assert int(re.search(r"export const DESCRIPTION_MAX = (\d+);", targets).group(1)) == cm.DESCRIPTION_MAX
    # Manage's candidate mirrors (control_descriptors / binding_policy).
    from server import control_descriptors as cd
    assert constant("LEGACY_PRIMITIVE") == json.dumps(bp.LEGACY_PRIMITIVE_CLASS)
    assert int(constant("MAX_CANDIDATES")) == cd.MAX_CANDIDATES
    manage = (ROOT / "web/js/controls_manage.js").read_text(encoding="utf-8")
    assert int(re.search(r"const MAX_BATCH = (\d+);", manage).group(1)) == cd.MAX_CONTROL_CHANGES
    assert re.search(r'const ID_ALPHABET = "([^"]+)"', manage).group(1) == re.search(
        r'const ID_ALPHABET = "([^"]+)"', source).group(1)


def test_selector_choice_list_parsing_matches_the_node():
    import ast
    tree = ast.parse((ROOT / "nodes/selector.py").read_text(encoding="utf-8"))
    namespace = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_parse_choice_list":
            exec(compile(ast.Module([node], []), "selector", "exec"), namespace)
    for text in ("", "a", "  a  \n\n b\n", None, "x\r\ny", ["linked", 0]):
        if isinstance(text, list):
            continue
        assert bp.parse_choice_list(text) == namespace["_parse_choice_list"](text)


def test_selector_provider_reads_the_effective_list_and_defers_a_linked_one():
    provider = bp.provider_for("SonderSelector", "choice")
    assert provider({"choice_list": "A\n\nB"}) == ["A", "B"]
    assert provider({"choice_list": ["5", 0]}) is None
    assert bp.provider_for("SonderSelector", "choice_list") is None
    assert bp.provider_for("KSampler", "sampler_name") is None


def test_selector_accepts_a_linked_choice_at_prompt_validation():
    import ast
    source = (ROOT / "nodes/selector.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    namespace = {"Counter": __import__("collections").Counter}
    wanted = {"_parse_choice_list", "_validate_choice"}
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    selector = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SonderSelector")
    validate = next(node for node in selector.body if isinstance(node, ast.FunctionDef) and node.name == "VALIDATE_INPUTS")
    validate.decorator_list = []
    exec(compile(ast.Module(body + [validate], []), "selector", "exec"), namespace)
    check = lambda **kw: namespace["VALIDATE_INPUTS"](None, **kw)
    assert check(choice=None, choice_list="A\nB") is True  # linked choice: checked at run
    assert check(choice="A", choice_list=None) is True      # linked list: checked at run
    assert check(choice="A", choice_list="A\nB") is True
    assert "not in the choice list" in check(choice="C", choice_list="A\nB")


def test_group_order_uses_effective_default_labels_and_codepoint_order():
    from server import control_descriptors as cd
    node = shutil.which('node')
    if not node:
        pytest.skip('node required for the frontend mirror')
    titles = ['Z knob', 'A knob', 'é knob', '_ knob', '😀 knob', '\ue000 knob']
    fixture = {'groups': {}, 'bindings': {}, 'notes': {}}
    resolved, prompt, bindings = {}, {}, []
    for i, title in enumerate(titles):
        identity = str(i)
        entry = {'label': '', 'order': 0, 'group': title}
        target = {'path': identity, 'input': 'amount'}
        fixture['bindings'][identity] = entry
        prompt[identity] = {'class_type': 'Knob', '_meta': {'title': title}}
        resolved[identity] = {'node': {'id': identity, 'type': 'Knob', 'title': title}, 'targets': [target]}
        bindings.append({'binding_id': identity, **entry, 'label': cd._default_label(prompt, [target])})
    script = ('import { effectiveGroupOrder } from ' + json.dumps((ROOT / 'web/js/control_bindings.js').as_uri())
              + '; console.log(JSON.stringify(effectiveGroupOrder(' + json.dumps(fixture) + ',' + json.dumps(resolved) + ')));')
    result = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == cd.group_order({'groups': {}, 'bindings': bindings, 'notes': []})


# ── Phase 4B: groups ─────────────────────────────────────────────────────────

def test_group_problems_are_cosmetic_and_never_make_the_relay_unreadable():
    text = json.dumps({"bindings": {"a": {"group": "a:b"}, "b": {"group": " Look "}, "c": {"group": 7},
                                    "d": {"group": "x" * 121}},
                       "notes": {"n": {"group": "Tab\tName"}},
                       "groups": {" Look ": {"order": 4}, "Look": {"order": 1}, "bad:name": {"order": 1},
                                  "Odd": {"order": "high"}, "List": [], "Fine": {}, "constructor-ish": {"order": 2}}})
    parsed = cm.parse_manifest(text)
    assert [parsed["bindings"][k]["group"] for k in "abcd"] == ["", "Look", "", ""]
    assert parsed["notes"]["n"]["group"] == ""
    # Colliding names merge (the first entry's order wins); invalid entries drop.
    assert parsed["groups"] == {"Look": {"order": 4}, "Fine": {"order": 0}, "constructor-ish": {"order": 2}}
    assert cm.parse_manifest(json.dumps({"groups": []}))["groups"] == {}
    many = {f"g{i}": {"order": i % 50} for i in range(cm.MAX_GROUPS + 10)}
    assert len(cm.parse_manifest(json.dumps({"groups": many}))["groups"]) == cm.MAX_GROUPS
    # A prototype key stays a refusal: no writer can produce one.
    with pytest.raises(cm.ManifestError, match="reserved key"):
        cm.parse_manifest(json.dumps({"groups": {"__proto__": {}}}))


GROUP_NAMES = ["Look", " Look ", "", "   ", "a:b", "x" * 120, "x" * 121, "😀" * 120, "😀" * 121, "constructor",
               " prototype ", "Tab\tName", "Bell\x07", "Del\x7f", "C1\x85", "Ünïcode", "\u2028line ", "\u3000Wide\u3000", "Bom\ufeff", "\x1cSep", 5, None]
TARGETS = ["workflow:restore", "binding:aB-_9:value", "note:n1:create", "group:Look and feel:rename", "group:😀:move",
           "workflow:description", "group::rename", "group:a:b:rename", "binding:bad id:value", "binding:a",
           "workflow:other", "control:a:value", "group:Look:order", "binding:a:hide", "note:n:text"]


def test_group_name_rule_and_target_grammar_mirror_the_frontend():
    from server import control_descriptors as cd
    node = shutil.which("node")
    if not node:
        pytest.skip("node required for the frontend mirror")
    module = (ROOT / "web/js/control_targets.js").as_uri()
    script = ("import * as t from " + json.dumps(module) + "; const names = " + json.dumps(GROUP_NAMES)
              + "; const targets = " + json.dumps(TARGETS) + "; console.log(JSON.stringify({"
              + "errors: names.map(n => t.groupNameError(n) === null), clean: names.map(t.cleanGroup),"
              + "parsed: targets.map(x => { const p = t.parseTarget(x); return p && [p.source, p.id, p.field]; }),"
              + "structural: targets.map(t.isStructuralTarget), run: targets.map(t.changesRun), max: t.GROUP_NAME_MAX}));")
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True,
                            encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    js = json.loads(result.stdout)
    assert js["errors"] == [cm.group_name_error(name) is None for name in GROUP_NAMES]
    assert js["clean"] == [cm.clean_group(name) for name in GROUP_NAMES]
    assert js["parsed"] == [list(p) if (p := cd.parse_target(x)) else None for x in TARGETS]
    assert js["structural"] == [cd.is_structural(x) for x in TARGETS]
    run_fields = {"value", "seed_mode", "expose", "remove", "rebind", "hide", "unhide"}
    assert js["run"] == [bool((p := cd.parse_target(x)) and p[0] == "binding" and p[2] in run_fields) for x in TARGETS]
    assert js["max"] == cm.LABEL_MAX


def test_group_order_skips_editor_bindings_and_reads_groups_tolerantly_like_the_frontend():
    from server import control_descriptors as cd
    node = shutil.which("node")
    if not node:
        pytest.skip("node required for the frontend mirror")
    fixture = {"groups": {" Late ": {"order": 3}, "Late": {"order": -5}, "bad:x": {"order": 0}, "Odd": {"order": 1.5}},
               "bindings": {"a": {"label": "A", "order": 0, "group": "Kept"},
                            "e": {"label": "E", "order": -9, "group": "EditorOnly", "section": "editor"},
                            "z": {"label": "Z", "order": 1, "group": " Spaced "}},
               "notes": {"n": {"text": "x", "order": 0, "group": "a:b"}}}
    view = cm.parse_manifest(json.dumps(fixture))
    view = {"groups": view["groups"],
            "bindings": [{"binding_id": k, **v} for k, v in view["bindings"].items()],
            "notes": [{"note_id": k, **v} for k, v in view["notes"].items()]}
    script = ("import { effectiveGroupOrder } from " + json.dumps((ROOT / "web/js/control_bindings.js").as_uri())
              + "; console.log(JSON.stringify(effectiveGroupOrder(" + json.dumps(fixture) + ")));")
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == cd.group_order(view) == ["Late", "Kept", "Spaced"]


def test_max_groups_mirrors_the_frontend():
    source = (ROOT / "web/js/control_bindings.js").read_text(encoding="utf-8")
    assert int(re.search(r"export const MAX_GROUPS = (\d+);", source).group(1)) == cm.MAX_GROUPS
    groups = (ROOT / "web/js/controls_panel_groups.js").read_text(encoding="utf-8")
    from server import control_descriptors as cd
    assert int(re.search(r"export const MAX_GROUP_FLAGS = (\d+);", groups).group(1)) == cd.MAX_CONTROL_CHANGES
