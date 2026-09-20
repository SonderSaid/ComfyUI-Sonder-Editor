"""Canonical scene-mutation authoring obligations, not a runtime registry.

The architecture.md map is an external, unratcheted projection. The markers in
the widget header and dispatcher docstring are pinned here. Retire an obligation
by removing its row here and both markers together, and refresh the prose map.
Test references prove existence, not semantics; the referenced suites own those.
Discovery covers direct consumers of the shared dispatcher scanner, including
aliases, not arbitrary new scanners or runtime tables. Review remains necessary.
All checks in this file run without Node or a live ComfyUI server.
"""
import ast
import copy
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_scene_mutation_registration as registration

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
REG = "test_scene_mutation_registration.py::"
HISTORY = "test_scene_history_merge.py::"

# id -> (registration site, enforcing tests). An empty tuple means review-only,
# except dispatch, which refuses unknown operations at runtime. No counts in prose.
OBLIGATIONS = {
    "operation.dispatch": ("_apply_scene_mutation_operation", ()),
    "operation.model-helper": ("routes.py model helpers", ()),
    "operation.media-budget": ("_media_io_operation_count", (
        "test_mutation_authoring_contract.py::test_every_operation_has_a_media_budget_disposition",
        "test_mutation_authoring_contract.py::test_media_budget_executes_its_counted_branches")),
    "operation.history-rebase": ("_rebaseSceneMutationIntentForHistory", (
        REG + "test_every_scene_operation_has_a_history_rebase_policy",
        REG + "test_rebase_policy_entries_are_not_stale")),
    "operation.guard": ("GUARD_CONTRACTS", (
        REG + "test_every_dispatcher_operation_declares_a_guard_contract",
        REG + "test_the_guard_contracts_still_match_the_code")),
    "operation.addressing": ("SCENE_MUTATION_ADDRESSING", (
        "test_scene_mutation_retry_policy.py::test_every_dispatcher_operation_is_classified",
        "test_scene_mutation_retry_policy.py::test_the_retryable_set_under_a_best_case_payload_is_pinned")),
    "operation.collapse": ("SCENE_MUTATION_COALESCING", (
        "test_scene_mutation_coalescing_policy.py::test_every_dispatcher_operation_carries_a_decision",)),
    "operation.scene-only": ("_SCENE_ONLY_MUTATIONS (listed operations only)", (
        "test_scene_mutation_no_op_writes.py::test_the_allow_list_only_holds_operations_the_dispatcher_accepts",
        "test_scene_mutation_no_op_writes.py::test_a_scene_only_operation_leaves_the_rest_of_the_project_alone")),
    "gesture.wrapper": ("_withMutationGesture", (
        "test_mutation_gesture_coverage.py::test_editor_writer_boundary_inventory",)),
    "gesture.guard-emission": ("operation literal expected*", (
        REG + "test_every_unguarded_client_payload_is_accounted_for",
        REG + "test_no_emission_sends_a_guard_the_server_discards",
        REG + "test_an_emission_sends_every_key_its_branch_requires")),
    "gesture.coalescing": ("key / coalesce / merge", (
        REG + "test_a_coalescing_gesture_declares_a_merge_or_sends_a_whole_value_payload",
        REG + "test_every_enqueue_that_cannot_coalesce_says_why")),
    "gesture.undo": ("_pushUndo and its microtask claim", ()),
    "gesture.optimistic-apply": ("_applyLocal* / _renderSceneAfterLocalMutation", ()),
    "gesture.surface": ("user surface / shortcut overlay", ()),
    "field.history-write-set": ("MERGED_WRITE_FIELDS and Scene field classification", (
        HISTORY + "test_every_scene_field_has_a_history_classification",
        HISTORY + "test_pass_through_fields_are_unreachable_from_scene_mutations",
        HISTORY + "test_every_declared_write_field_has_a_probe")),
}

# Direct scanner consumers that support a listed obligation rather than asserting
# their own coverage. Every reason states when the exception ceases to apply.
DISCOVERY_SUPPORT = {
    "test_mutation_authoring_contract.py::test_contract_tripwires_reject_injected_drift": ("operation.media-budget", "Injection proof; remove when it no longer reads the dispatcher for its fixture."),
    REG + "_scan": ("gesture.guard-emission", "Literal inventory helper; remove when it stops serving the guard scan."),
    REG + "test_the_emitting_module_set_is_closed": ("gesture.guard-emission", "Emitter discovery; remove when the literal scan no longer depends on it."),
    REG + "test_every_scene_operation_is_reachable_through_the_anchor": ("gesture.guard-emission", "Anchor completeness; remove when the literal scanner changes."),
    REG + "_rebase_policy_classes": ("operation.history-rebase", "Classification helper; remove when coverage no longer delegates to it."),
    REG + "_required_expected_key_sets": ("gesture.guard-emission", "Required-key helper; remove when guard coverage no longer delegates to it."),
}

# Explicit, reviewable exemptions, never the complement of the dispatcher set.
# Expiry for every row: delete/reclassify it when that operation can extract or
# create media bytes. A record referring to existing media is not media I/O.
NO_MEDIA = {
    "bulk_delete_items": "Removes scene records and links.",
    "consolidate_items": "Moves existing records between lanes.",
    "create_guide": "References an existing asset.",
    "create_link_group": "Creates membership records only.",
    "create_prompt_section": "Creates authored prompt data.",
    "create_prompt_semantic_unit": "Creates prompt identity data.",
    "create_reference_item": "Stages existing Library members.",
    "delete_audio_track": "Removes a timeline reference.",
    "delete_clip": "Removes a timeline reference.",
    "delete_guide": "Removes a guide reference.",
    "delete_link_group": "Removes group membership.",
    "delete_prompt_section": "Removes prompt data.",
    "delete_prompt_semantic_unit_if_unreferenced": "Removes unused identity data.",
    "delete_reference_item": "Removes staged member references.",
    "import_prompt_context_dependencies": "Imports profile and identity data, not media.",
    "move_guide": "Moves an existing guide reference.",
    "move_lane": "Reorders lane records.",
    "remove_lane": "Removes or relocates existing lane contents.",
    "replace_audio_source": "Repoints at an existing audio asset.",
    "replace_clip_source": "Repoints at an existing clip asset.",
    "replace_prompt_sections": "Replaces authored prompt data.",
    "set_lane_count": "Changes scene lane structure.",
    "split_audio_track": "Splits timeline references, not media bytes.",
    "split_clip": "Splits timeline references, not media bytes.",
    "split_prompt_section": "Splits prompt ranges.",
    "split_reference_item": "Splits staged member ranges.",
    "swap_prompt_sections": "Swaps prompt data.",
    "unlink_items": "Changes group membership.",
    "update_audio_track": "Changes timeline properties.",
    "update_clip": "Changes timeline properties.",
    "update_guide": "Changes guide properties.",
    "update_lane_config": "Changes lane configuration.",
    "update_lane_configs": "Changes lane configuration arrays.",
    "update_prompt_section": "Changes prompt data.",
    "update_reference_item": "Changes staged existing members.",
    "update_scene_fields": "Changes scene metadata and retimes records.",
}

def _function(source, name):
    return next(node for node in ast.parse(source).body
                if isinstance(node, ast.FunctionDef) and node.name == name)

def _header_ids(header):
    return re.findall(r"@mutation-obligation ([\w.-]+)", header)

def _assert_header(header):
    ids = _header_ids(header)
    assert len(ids) == len(set(ids)), "Duplicate authoring obligation marker"
    assert set(ids) == set(OBLIGATIONS), (
        "Scene Mutation Authoring header drift: missing "
        f"{sorted(set(OBLIGATIONS) - set(ids))}; stale {sorted(set(ids) - set(OBLIGATIONS))}")
    assert "tests/test_mutation_authoring_contract.py" in header

def test_both_code_homes_name_the_canonical_obligations():
    widget = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    assert widget.startswith("/**"), "The authoring contract must precede imports"
    _assert_header(widget[:widget.index("*/")])
    routes = (ROOT / "server/routes.py").read_text(encoding="utf-8")
    _assert_header(ast.get_docstring(_function(routes, "_apply_scene_mutation_operation")) or "")

def _named_tests():
    return {ref for _, refs in OBLIGATIONS.values() for ref in refs}

def test_every_named_enforcing_test_exists():
    for ref in sorted(_named_tests()):
        filename, name = ref.split("::")
        path = TESTS / filename
        assert path.is_file(), f"Scene Mutation Authoring lost enforcing suite: {ref}"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert any(isinstance(node, ast.FunctionDef) and node.name == name
                   for node in tree.body), f"Scene Mutation Authoring lost enforcing test: {ref}"

def _dispatcher_consumers(source, filename):
    """Direct calls, resolving import aliases; strings/comments are never code.

    Assignment aliases and a second scanner cannot be discovered this way. A new
    consumer inside an existing helper still shares that helper's review boundary.
    """
    tree = ast.parse(source)
    names, modules = set(), set()
    if filename == "test_scene_mutation_registration.py":
        names.add("_dispatcher_op_types")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "test_scene_mutation_registration":
            names.update(one.asname or one.name for one in node.names if one.name == "_dispatcher_op_types")
        if isinstance(node, ast.Import):
            modules.update(one.asname or one.name for one in node.names if one.name == "test_scene_mutation_registration")
    def visit(node, scope="<module>"):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = node.name if scope == "<module>" else scope + "." + node.name
        if isinstance(node, ast.Call):
            fun = node.func
            if ((isinstance(fun, ast.Name) and fun.id in names) or
                (isinstance(fun, ast.Attribute) and fun.attr == "_dispatcher_op_types"
                 and isinstance(fun.value, ast.Name) and fun.value.id in modules)):
                yield filename + "::" + scope
        for child in ast.iter_child_nodes(node):
            yield from visit(child, scope)
    return set(visit(tree))

def test_every_dispatcher_scanner_consumer_is_represented():
    consumers = set()
    for path in TESTS.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "test_scene_mutation_registration" in source or path.name == "test_scene_mutation_registration.py":
            consumers |= _dispatcher_consumers(source, path.relative_to(TESTS).as_posix())
    _assert_consumers(consumers)
    assert not set(DISCOVERY_SUPPORT) - consumers, "Remove stale scanner-support exemptions"
    for obligation, reason in DISCOVERY_SUPPORT.values():
        assert obligation in OBLIGATIONS and reason.strip()

def _assert_consumers(consumers):
    represented = _named_tests() | set(DISCOVERY_SUPPORT)
    assert not consumers - represented, (
        "Scene Mutation Authoring: new dispatcher-scanner consumer needs an obligation "
        f"or a reasoned support entry: {sorted(consumers - represented)}")

def _counted_media_types(source):
    function = _function(source, "_media_io_operation_count")
    found = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "op_type":
            assert len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq), "Review changed media-budget predicate"
            value = node.comparators[0]
            assert isinstance(value, ast.Constant) and isinstance(value.value, str), "Unresolved media-budget type"
            found.add(value.value)
    assert found, "Media-budget scan found no counted operation types"
    return found

def _assert_media_partition(dispatched, counted):
    assert not counted & NO_MEDIA.keys(), "Media operation still marked as creating no media"
    assert dispatched == counted | NO_MEDIA.keys(), (
        "Scene Mutation Authoring media budget: classify new operations or retire stale entries; "
        f"unclassified {sorted(dispatched - counted - NO_MEDIA.keys())}; "
        f"stale {sorted((counted | NO_MEDIA.keys()) - dispatched)}")
    assert all(reason.strip() for reason in NO_MEDIA.values())

def test_every_operation_has_a_media_budget_disposition():
    source = (ROOT / "server/routes.py").read_text(encoding="utf-8")
    _assert_media_partition(registration._dispatcher_op_types(), _counted_media_types(source))

def test_media_budget_executes_its_counted_branches():
    # Execute the real, dependency-light function. The stub isolates counting
    # from the separately tested dual-drop predicate; it does not claim to prove it.
    source = (ROOT / "server/routes.py").read_text(encoding="utf-8")
    function = copy.deepcopy(_function(source, "_media_io_operation_count"))
    function.returns = None
    for arg in function.args.args:
        arg.annotation = None
    namespace = {"_clip_dual_drop_uses_media_io": lambda asset, role, fields: fields.get("dual", False)}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "media-budget", "exec"), namespace)
    counter = namespace["_media_io_operation_count"]
    asset = SimpleNamespace(asset_type="video", has_audio=True)
    project = SimpleNamespace(get_asset=lambda identity: asset if identity == "asset" else None)
    probes = {"create_audio_track": {}, "create_clip": {"dual": True}}
    assert set(probes) == _counted_media_types(source), "New counted branch needs a positive and negative probe"
    operations = []
    for op_type, extra in probes.items():
        operation = {"type": op_type, "fields": {"asset_id": "asset", **extra}}
        assert counter(project, [operation]) == 1, op_type
        assert counter(project, [{"type": op_type, "fields": {}}]) == 0, op_type
        operations.append(operation)
    assert counter(project, operations) == len(operations)
    assert counter(project, [None, {}, {"type": "create_clip", "fields": {"asset_id": "asset"}}]) == 0
    asset.has_audio = False
    assert counter(project, [{"type": "create_audio_track", "fields": {"asset_id": "asset"}}]) == 0

def test_contract_tripwires_reject_injected_drift():
    header = "tests/test_mutation_authoring_contract.py\n" + "\n".join(
        "@mutation-obligation " + key for key in OBLIGATIONS)
    _assert_header(header)
    with pytest.raises(AssertionError, match="header drift"):
        _assert_header(header.replace("@mutation-obligation operation.guard", "removed"))
    source = 'from test_scene_mutation_registration import _dispatcher_op_types as ops\ndef test_new():\n    return ops()\n'
    new = _dispatcher_consumers(source, "test_new_policy.py")
    assert new == {"test_new_policy.py::test_new"}
    with pytest.raises(AssertionError, match="new dispatcher-scanner consumer"):
        _assert_consumers(new)
    with pytest.raises(AssertionError, match="unclassified"):
        _assert_media_partition(registration._dispatcher_op_types() | {"create_new_media"}, {"create_clip", "create_audio_track"})
    source = 'import test_scene_mutation_registration as reg\ndef test_new():\n    return reg._dispatcher_op_types()\n'
    assert _dispatcher_consumers(source, "test_new_policy.py") == new
    assert not _dispatcher_consumers('example = "_dispatcher_op_types()"\n# _dispatcher_op_types()\n', "test_new_policy.py")


def test_a_retired_enforcing_test_cannot_silently_hollow_the_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(__import__(__name__), "TESTS", tmp_path)
    monkeypatch.setattr(__import__(__name__), "OBLIGATIONS", {
        "operation.probe": ("probe", ("test_probe.py::test_enforces_probe",))})
    path = tmp_path / "test_probe.py"
    path.write_text("def test_renamed():\n    pass\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="lost enforcing test"):
        test_every_named_enforcing_test_exists()
    path.unlink()
    with pytest.raises(AssertionError, match="lost enforcing suite"):
        test_every_named_enforcing_test_exists()


def test_a_class_method_cannot_hide_behind_a_represented_top_level_test():
    source = (
        "from test_scene_mutation_registration import _dispatcher_op_types\n"
        "class TestAdditionalPolicy:\n"
        "    def test_every_dispatcher_operation_is_classified(self):\n"
        "        return _dispatcher_op_types()\n")
    found = _dispatcher_consumers(source, "test_scene_mutation_retry_policy.py")
    assert found == {
        "test_scene_mutation_retry_policy.py::TestAdditionalPolicy.test_every_dispatcher_operation_is_classified"}
    with pytest.raises(AssertionError, match="TestAdditionalPolicy"):
        _assert_consumers(found)
