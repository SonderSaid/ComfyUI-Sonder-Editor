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
        "test_mutation_authoring_contract.py::test_media_budget_executes_its_counted_branches",
        "test_mutation_authoring_contract.py::test_no_media_operations_cannot_reach_ffmpeg")),
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
        "test_mutation_gesture_coverage.py::test_editor_writer_boundary_inventory",
        "test_mutation_gesture_coverage.py::test_editor_writer_exemptions_are_live",)),
    "gesture.guard-emission": ("operation literal expected*", (
        REG + "test_every_unguarded_client_payload_is_accounted_for",
        REG + "test_no_emission_sends_a_guard_the_server_discards",
        REG + "test_an_emission_sends_every_key_its_branch_requires")),
    "gesture.coalescing": ("key / coalesce / merge", (
        REG + "test_a_coalescing_gesture_declares_a_merge_or_sends_a_whole_value_payload",
        REG + "test_every_enqueue_that_cannot_coalesce_says_why",
        REG + "test_no_enqueue_acquires_the_default_key_by_omission")),
    "gesture.undo": ("_pushUndo and its microtask claim", (
        "test_mutation_authoring_contract.py::test_gesture_undo_claims_reach_a_mutation_helper_before_expiry",
        "test_mutation_authoring_contract.py::test_deferred_undo_claims_keep_their_explicit_handoff")),
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
    "swap_guides": "Exchanges two timeline references.",
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

# Direct named calls through module-level routes helpers, not whole-program
# reachability. Dynamic dispatch, aliases and calls into other modules remain
# review work. Pin executable entry points, not today's extraction wrapper.
MEDIA_ENTRY_POINTS = frozenset({"_get_ffmpeg", "get_ffmpeg_path", "get_ffprobe_path"})

def _media_reachable_operations(source):
    tree = ast.parse(source)
    functions = {node.name: node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    def calls(node):
        return {child.func.id for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)}
    def reaches_media(names):
        visited = set()
        pending = list(names)
        while pending:
            name = pending.pop()
            if name in MEDIA_ENTRY_POINTS:
                return True
            if name not in visited:
                visited.add(name)
                if name in functions:
                    pending.extend(calls(functions[name]))
        return False
    dispatcher = functions["_apply_scene_mutation_operation"]
    result = set()
    branches = {}
    for node in ast.walk(dispatcher):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                and test.left.id == "op_type"):
            assert len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq), "Review media dispatch shape"
            value = test.comparators[0]
            assert isinstance(value, ast.Constant) and isinstance(value.value, str)
            branches[node] = value.value
            branch = ast.Module(body=node.body, type_ignores=[])
            if reaches_media(calls(branch)):
                result.add(value.value)
    # Calls outside operation branches run for every operation. Do not let a
    # new common pre-pass silently evade the NO_MEDIA classification.
    common_calls = set()
    def visit_common(node):
        if node in branches:
            for child in node.orelse:
                visit_common(child)
            return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            common_calls.add(node.func.id)
        for child in ast.iter_child_nodes(node):
            visit_common(child)
    visit_common(dispatcher)
    if reaches_media(common_calls):
        result.update(branches.values())
    return result

def _assert_no_media_reachability(source):
    bad = _media_reachable_operations(source) & NO_MEDIA.keys()
    assert not bad, f"NO_MEDIA operations reach ffmpeg: {sorted(bad)}"

def test_no_media_operations_cannot_reach_ffmpeg():
    source = (ROOT / "server/routes.py").read_text(encoding="utf-8")
    _assert_no_media_reachability(source)
    assert _media_reachable_operations(source) == _counted_media_types(source), (
        "Review media reachability or counted-operation drift")

@pytest.mark.parametrize("entry", sorted(MEDIA_ENTRY_POINTS))
def test_media_reachability_rejects_an_existing_no_media_operation(entry, monkeypatch):
    source = (ROOT / "server/routes.py").read_text(encoding="utf-8")
    # A new two-hop path, independent of _prepare_video_audio_asset.
    source = source.replace('if op_type == "update_guide":',
                            'if op_type == "update_guide":\n        _probe_media_path()')
    source += f"\ndef _probe_media_path():\n    _probe_media_leaf()\ndef _probe_media_leaf():\n    {entry}()\n"
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *a, **kw:
                        source if path == ROOT / "server/routes.py" else original(path, *a, **kw))
    with pytest.raises(AssertionError, match="NO_MEDIA operations reach ffmpeg.*update_guide"):
        test_no_media_operations_cannot_reach_ffmpeg()

def test_media_reachability_checks_the_shared_dispatcher_path(monkeypatch):
    source = (ROOT / "server/routes.py").read_text(encoding="utf-8")
    dispatcher = _function(source, "_apply_scene_mutation_operation")
    # Inject after the docstring; every operation now reaches this new chain.
    lines = source.splitlines(keepends=True)
    lines.insert(dispatcher.body[0].end_lineno, "    _probe_common_media()\n")
    source = "".join(lines) + "\ndef _probe_common_media():\n    _get_ffmpeg()\n"
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *a, **kw:
                        source if path == ROOT / "server/routes.py" else original(path, *a, **kw))
    with pytest.raises(AssertionError, match="NO_MEDIA operations reach ffmpeg"):
        test_no_media_operations_cannot_reach_ffmpeg()

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


# Undo claims are gesture-callback scoped, not method scoped: a picker builder
# and its eventual writer have different synchronous lifetimes. This is a lexical
# tripwire, not a JavaScript interpreter or a proof of control flow. It does not
# follow calls into other methods, implicit promise continuations, or arbitrary
# aliases. Existing scanner masking also excludes template interpolations and
# does not tokenize regex literals. Behavioral history tests own runtime claims.
# A nested function body is masked out of the outer synchronous path rather than
# abandoning the callback, because an await inside an unrelated closure is not an
# outer suspension. What the closure CONTAINS is still reported: a reservation or
# a write inside one is deferred work no lexical scan can attribute. A concise
# arrow has no brace to mask, so it is reviewed only when its body could hide one
# of those three events.
_MUTATION_HELPERS = {
    "_runSceneMutation": 1,
    "_queueProjectMutation": 0,
    "_updateItemProperty": None,
}
# Values are the inline options argument that actually forwards historyEntry.
# None means the helper is reachable, NOT that it can transport an explicit claim.
# In particular _updateItemProperty supports a synchronous claim but discards a
# historyEntry option today. Never certify an async gap merely by spelling it.
# Versioned HTTP, Reference, queue and prompt-project helpers do not claim a
# scene candidate: posting is not stamping, and their intents lack sceneId.

UNDO_CLAIM_EXEMPTIONS = {
    ("_toggleHeaderVisibility", "missing-mutation-helper"): (
        "Reserves and hands the entry to _applyHeaderVisibilityBulkWithinGesture "
        "as an argument, which this scan does not follow. The handoff is pinned "
        "instead by DEFERRED_UNDO_CLAIMS['toggle track visibility (header "
        "control)'], link by link. Delete this exception when the scan resolves "
        "argument handoffs, or when the dormant method is deleted."),
    ("assetDrop", "multiple-reservations-review"): (
        "Two reservations, because one drop can write a clip, its extracted "
        "audio and a driver as separate ordered writes. Each is forwarded "
        "explicitly through the local queueDropMutation helper, pinned by "
        "DEFERRED_UNDO_CLAIMS['asset drop']. Delete this exception when the "
        "drop submits one batch carrying one entry."),
    # The three below hold reservations inside event-handler closures, so the
    # outer synchronous path has none to follow. Their value is not today's
    # verdict: a reservation added at the TOP level of any of them changes the
    # finding and fails this catalogue, which is what the exemption buys.
    ("_setupTimelineEvents", "reservation-only-in-nested"): (
        "Every reservation here is inside a pointer or context-menu handler. "
        "Trim and move items are pinned by DEFERRED_UNDO_CLAIMS; the lane menu's "
        "visibility toggle is pinned as 'toggle track visibility (context "
        "menu)'. Delete this exception when the handlers become named methods."),
    ("_showItemEditor", "reservation-only-in-nested"): (
        "Reservations sit in the editor panel's control handlers, each writing "
        "through _updateItemProperty in the same synchronous turn. Delete this "
        "exception when the panel's handlers become named methods."),
    ("_showGuideManagementPopup", "reservation-only-in-nested"): (
        "Reservations sit in the popup's per-guide button handlers. Delete this "
        "exception when the popup's handlers become named methods."),
}

# Reservations that travel out of the scope that made them. Three TRANSPORT
# FORMS, named rather than blurred, because they fail differently:
#
#   member-storage -- the entry is parked on `this` at pointer-down and read at
#       pointer-up. Genuinely deferred across an asynchronous lifecycle.
#   argument -- the entry is handed to another method in the same synchronous
#       turn. Not deferred in time, but it does cross a scope boundary, so no
#       callback-scoped scan can see both halves.
#   local-helper -- the entry is passed to a closure declared in the SAME
#       method, which forwards it. Producer and consumer are one scope.
#
# What every form owes is identical and is checked for all three: the reserved
# object reaches a mutation helper as an explicit `historyEntry`, because the
# implicit `_historyPostSnapshotCaptureCandidate` expires at a microtask.
DEFERRED_UNDO_CLAIMS = {
    "trim": {
        "form": "member-storage",
        "producer": "_setupTimelineEvents", "consumer": "_commitTrim",
        "storage": "_trimItem", "entry": "trimInfo.historyEntry",
        "reason": "Pointer-down reserves history; pointer-up forwards that exact entry.",
        "expiry": "Remove when trim reserves and queues in one synchronous callback.",
    },
    "move items": {
        "form": "member-storage",
        "producer": "_setupTimelineEvents", "consumer": "_commitItemMove",
        "storage": "_dragHistoryEntry", "entry": "historyEntry",
        "reason": "Pointer-down reserves history; move commit forwards the retained entry.",
        "expiry": "Remove when move reserves and queues in one synchronous callback.",
    },
    "toggle track visibility (context menu)": {
        "form": "argument",
        "producer": "_setupTimelineEvents",
        "consumer": "_applyHeaderVisibilityBulkWithinGesture",
        "label": "toggle track visibility", "binding": "undoEntry",
        "entrypoint": "_applyHeaderVisibilityBulk",
        "parameter": "undoEntry", "entry": "undoEntry",
        "reason": "The lane menu reserves, then hands the entry down rather than "
                  "letting the gesture look it up: a label or top-of-stack match "
                  "can delete a NEWER gesture's entry once the queue interleaves, "
                  "which coalescing on this key makes likelier, not less.",
        "expiry": "Remove when the bulk apply reserves its own entry inside the gesture.",
    },
    "toggle track visibility (header control)": {
        "form": "argument",
        "producer": "_toggleHeaderVisibility",
        "consumer": "_applyHeaderVisibilityBulkWithinGesture",
        "label": "toggle track visibility", "binding": "undoEntry",
        "entrypoint": "_applyHeaderVisibilityBulk",
        "parameter": "undoEntry", "entry": "undoEntry",
        # This path is DORMANT: `_toggleHeaderVisibility` has no caller, and
        # only `tests/test_animatic_visibility_js.py` names it, as a slice
        # anchor. It is pinned because its own comment asks for exactly that --
        # reviving it must not silently reintroduce a label-matched discard --
        # so a severance probe here is not evidence of a live hole.
        "reason": "Dormant single-lane entry point, kept reserving and handing "
                  "down its own entry so a revival cannot reintroduce the "
                  "label-matched discard this surface removed.",
        "expiry": "Remove when a single-lane control calls it, or when the "
                  "method is deleted with its test anchor.",
    },
    "asset drop": {
        "form": "local-helper",
        "producer": "_handleAssetDropWithinGesture",
        "consumer": "_handleAssetDropWithinGesture",
        "helper": "queueDropMutation", "parameter": "historyEntry",
        "bindings": ("driverUndoEntry", "assetUndoEntry"), "entry": "historyEntry",
        "reason": "One drop can create a clip, its extracted audio and a driver "
                  "in separate ordered writes; each carries the reservation its "
                  "own branch made so the composite gesture stamps the right one.",
        "expiry": "Remove when the drop submits one batch that carries one entry.",
    },
}


def _js_code(source):
    mask = registration._code_mask(source)
    return "".join(char if mask[index] else " " for index, char in enumerate(source))


def _js_arguments(source, opener, mask=None):
    """Top-level call arguments, preserving text for a literal gesture name."""
    if mask is None:
        mask = registration._code_mask(source)
    end = registration._match_delimiter(source, opener, "(", ")", mask)
    assert end >= 0, "Undo claim scan: unterminated call"
    args, start, stack = [], opener + 1, []
    pairs = {"(": ")", "[": "]", "{": "}"}
    for index in range(start, end):
        if not mask[index]:
            continue
        char = source[index]
        if char in pairs:
            stack.append(pairs[char])
        elif char in ")]}":
            assert stack and stack.pop() == char, "Undo claim scan: unmatched delimiter"
        elif char == "," and not stack:
            args.append(source[start:index].strip())
            start = index + 1
    assert not stack, "Undo claim scan: unclosed argument"
    args.append(source[start:end].strip())
    return args, end


# The codebase's dominant gesture shape: the wrapper's callback does nothing
# but delegate to a `*WithinGesture` method, which is where the reservation and
# the write actually live. Resolving ONE level of that forwarding is what takes
# the scan from the handful of inline callbacks to the bodies that hold most of
# the reservations. It is one level by design: a callee that itself delegates is
# not followed, and says so as a finding rather than passing quietly.
_FORWARD_RE = re.compile(r"\bthis\.(_[\w$]*WithinGesture)\s*\(")

# Methods that reserve undo without a gesture wrapper above them. Scanned under
# their own name because there is no gesture name to attribute to.
UNWRAPPED_RESERVING_SCOPES = (
    "_setupTimelineEvents", "_showItemEditor", "_toggleHeaderVisibility",
    "_showGuideManagementPopup",
)

# Deliberately not scanned. Each reason is checked by
# `test_the_unscanned_scopes_are_still_the_shapes_their_reasons_describe`, so an
# exclusion cannot outlive the condition that justified it.
UNSCANNED_SCOPES = {
    "_showGuideManagementPopupLegacy":
        "Dead: declared once and called from nowhere in web/. Scanning it would "
        "buy a permanent exemption for code whose fix is deletion. This entry "
        "goes when it gains a caller, and the liveness check fails first.",
    "commitStrength":
        "A local const arrow declared TWICE in this file -- the legacy popup's "
        "does not reserve, the current one does -- so a name-keyed lookup "
        "cannot address either. This entry goes when they become one scope.",
}


# Every unit the scan reserves undo in, pinned so a shrinking scan cannot read
# as full coverage. One entry per scanned UNIT, not per reservation: `assetDrop`
# holds two reservations and appears once, while `deleteGuide` appears twice
# because two separate popup surfaces each wrap their own gesture of that name.
SCANNED_RESERVING_UNITS = (
    "_setupTimelineEvents", "_showGuideManagementPopup", "_showItemEditor",
    "_toggleHeaderVisibility", "addClipFrameToGuides", "addLane",
    "appendReferenceMembers", "applyPromptSetup", "assetDrop",
    "consolidateSelectedItemsToLane", "convertClipRole", "deleteGuide",
    "deleteGuide", "deleteItemsInLane", "deletePromptSection",
    "deleteSelectedItems", "deleteSelectedLanesAndItems", "linkItems",
    "moveGuideToFrame", "moveItemToFrame", "moveItemToNewLane",
    "moveReferenceLane", "placeReferencePayload", "removeLane",
    "removeLaneDeletingItems", "removeLaneWithItems", "renameScene",
    "replaceAudioSource", "replaceClipSource", "replaceGuideImage",
    "saveNewPromptSection", "splitItem", "swapGuides", "toggleMute",
    "unlinkItems", "updateLinkedPromptAttachment", "updatePromptSection",
    "updateSceneDuration", "updateSceneGlobalContext",
)


def _scope_body(source, scopes, name):
    matches = [(start, end) for scope, start, end in scopes if scope == name]
    return source[matches[0][0]:matches[0][1] + 1] if len(matches) == 1 else None


def _gesture_undo_callbacks(source):
    mask = registration._code_mask(source)
    code = "".join(char if mask[index] else " " for index, char in enumerate(source))
    scopes = registration._scopes(source, mask)
    callbacks, claimed = [], {}
    for match in re.finditer(r"\bthis\._withMutationGesture\s*\(", code):
        args, end = _js_arguments(source, match.end() - 1, mask)
        if len(args) < 2:
            continue
        callback, reserves = args[1], re.search(r"\bthis\._pushUndo\s*\(", _js_code(args[1]))
        # Resolved from the MASKED callback. A delegation reachable only from
        # inside a closure does not run in the gesture's synchronous turn, and
        # entering the callee would certify a reservation that happens after
        # the turn has ended. Those are collected separately as findings.
        masked_callback, _ = _mask_nested_functions(callback)
        callees = sorted({one[1] for one in _FORWARD_RE.finditer(masked_callback)})
        forwarded = [one for one in callees
                     if re.search(r"\bthis\._pushUndo\s*\(",
                                  _js_code(_scope_body(source, scopes, one) or ""))]
        if not reserves and not forwarded:
            continue
        name = re.fullmatch(r'''(["'])([\w.-]+)\1''', args[0])
        # A computed gesture name has nothing to attribute a finding to. Two
        # sites pass a variable and neither reserves; that must stay true.
        assert name, ("Undo claim scan: a gesture that reserves undo, directly "
                      "or through a WithinGesture callee, needs a literal name")
        if reserves:
            cb_code = _js_code(callback)
            head = re.match(r"\s*(?:async\s+)?(?:\([^)]*\)|[\w$]+)\s*=>\s*\{", cb_code)
            assert head, f"Undo claim scan: review unsupported callback shape for {name[2]}"
            brace = head.end() - 1
            close = registration._match_delimiter(callback, brace, "{", "}")
            assert close >= 0 and not cb_code[close + 1:].strip(), "Undo claim scan: incomplete callback"
            callbacks.append((name[2], callback[brace + 1:close]))
        for callee in forwarded:
            # One callee reached from two gestures would have its findings
            # attributed to whichever was scanned first. Refuse instead.
            assert claimed.setdefault(callee, name[2]) == name[2], (
                f"Undo claim scan: {callee} reserves undo and is reached from "
                f"both {claimed[callee]} and {name[2]}; one level of forwarding "
                "cannot attribute its findings")
            callbacks.append((name[2], _scope_body(source, scopes, callee)))
    for scope in UNWRAPPED_RESERVING_SCOPES:
        # Absent in a synthetic fixture, which is the normal case for the
        # parametrized probes. `SCANNED_RESERVING_UNITS` is what fails if one
        # of these disappears from the real widget.
        body = _scope_body(source, scopes, scope)
        if body is not None and re.search(r"\bthis\._pushUndo\s*\(", _js_code(body)):
            callbacks.append((scope, body))
    return callbacks


def _object_members(options, allowed_spreads=()):
    """Readable own members, or None if a computed key/spread can overwrite them."""
    code = _js_code(options)
    if not code.startswith("{") or not code.endswith("}"):
        return None
    # Reuse the delimiter-aware argument splitter as an object-member splitter.
    members, _ = _js_arguments("(" + options[1:-1] + ")", 0)
    found = []
    for member in members:
        text = _js_code(member).strip()
        if not text:
            continue
        if text in {"..." + name for name in allowed_spreads}:
            continue
        if re.fullmatch(r"[\w$]+", text):
            found.append((text, text))
            continue
        named = re.match(r"\s*([\w$]+)\s*:", _js_code(member))
        quoted = re.match(r'''\s*(["'])([\w$]+)\1\s*:''', member)
        if named:
            found.append((named[1], member[named.end():].strip()))
        elif quoted:
            found.append((quoted[2], member[quoted.end():].strip()))
        else:
            # Includes computed keys, accessor/method keys, escaped quoted keys
            # and opaque spreads. Their effect on historyEntry needs review.
            return None
    return found


def _explicit_history_entry(call_args, helper, entry):
    position = _MUTATION_HELPERS[helper]
    if entry is None or position is None or len(call_args) <= position:
        return False
    members = _object_members(call_args[position])
    if members is None:
        return False
    return [_js_code(value).strip() for key, value in members if key == "historyEntry"] == [entry]


def _queue_has_scene_claim(call_args):
    if not call_args:
        return False
    members = _object_members(call_args[0])
    if members is None or any(key == "historyEntry" for key, _ in members):
        return False  # an explicit override must be checked against its binding
    intents = [value for key, value in members if key == "intent"]
    if len(intents) != 1:
        return False
    fields = _object_members(intents[0])
    if fields is None:
        return False
    ids = [value.strip() for key, value in fields if key == "sceneId"]
    # An identifier is a lexical witness, not proof it is populated at runtime.
    return len(ids) == 1 and ids[0] not in {"", "null", "undefined", "false", "0", '""', "''"}


_PUSH_RE = re.compile(r"\bthis\._pushUndo\s*\(")
_HELPER_RE = re.compile(r"\bthis\.(" + "|".join(_MUTATION_HELPERS) + r")\s*\(")
_ARROW_HEAD_RE = re.compile(r"=>\s*\{")
# A `function` body is the brace after ITS OWN parameter list. Bounding the head
# this way is load-bearing, not tidiness: `_code_mask` deliberately does not
# tokenize regex literals, so a bare `\bfunction\b` also matches inside
# `/function/` and would then adopt the next `if (...) {` in real code as its
# body -- masking a block that actually runs, and with it any await gap in it.
_FUNCTION_HEAD_RE = re.compile(r"\bfunction\b\s*\*?\s*([\w$]*)\s*\(")
# Object-literal shorthand methods, class methods, getters and setters. Filtered
# through `registration._NOT_A_SCOPE`, the same keyword list `_scopes` uses to
# tell a scope head from a call, so `if (...) {` is not read as a closure. An
# unrecognised closure is worse than a missed one: its body stays on the outer
# synchronous path and is credited to it.
_METHOD_HEAD_RE = re.compile(r"(?:\b(?:get|set|async)\s+)?\*?\s*\b([\w$]+)\s*\(")


def _nested_function_spans(code, source, mask):
    """Every brace-bodied nested function as (brace, close), outermost only.

    `close` is -1 when the body cannot be read. Heads inside an already-covered
    span are skipped, so one outer closure is masked once rather than per
    nested shape that also matches inside it.
    """
    heads = set()
    for match in _ARROW_HEAD_RE.finditer(code):
        heads.add((match.start(), code.index("{", match.start())))
    for pattern in (_FUNCTION_HEAD_RE, _METHOD_HEAD_RE):
        for match in pattern.finditer(code):
            if match.group(1) in registration._NOT_A_SCOPE:
                continue
            paren = registration._match_delimiter(source, match.end() - 1, "(", ")", mask)
            if paren < 0:
                continue
            brace = paren + 1
            while brace < len(code) and code[brace].isspace():
                brace += 1
            if code[brace:brace + 1] == "{":
                heads.add((match.start(), brace))
    spans, covered = [], -1
    for head, brace in sorted(heads):
        if head <= covered:
            continue
        close = registration._match_delimiter(source, brace, "{", "}", mask)
        spans.append((brace, close))
        if close >= 0:
            covered = close
    return spans


def _mask_nested_functions(source):
    """Blank brace-bodied nested function bodies; report what they contained.

    An await inside an unrelated closure is not an outer suspension, so the
    closure is removed from the outer synchronous path rather than abandoning
    the whole callback. A reservation or a write INSIDE one is deferred work and
    is reported, never silently dropped. The returned text is the same LENGTH as
    the input, because callers index the unmasked source with offsets taken from
    the masked code.
    """
    mask = registration._code_mask(source)
    code = "".join(char if mask[index] else " " for index, char in enumerate(source))
    masked, events = list(code), set()
    for brace, close in _nested_function_spans(code, source, mask):
        if close < 0:
            events.add("unreadable-nested")
            continue
        inner = code[brace + 1:close]
        if _PUSH_RE.search(inner):
            events.add("push-in-nested")
        if _HELPER_RE.search(inner):
            events.add("helper-in-nested")
        for position in range(brace + 1, close):
            masked[position] = " "
    return "".join(masked), events


def _concise_arrow_risk(code):
    """What a brace-less arrow body could hide: a write, or a reservation/await.

    A concise arrow has no body to mask, so its expression stays in the scanned
    text and reads as the outer synchronous path. The helper term is
    load-bearing, not decoration: without it
    `const later = () => this._runSceneMutation(ops); await x(); later();`
    reports nothing, because the write still lexically precedes the await.

    The lookahead spans the whitespace deliberately. `=>\\s*(?!\\{)` backtracks
    to zero width and matches a brace arrow too, which would only be harmless
    while masking happens to have blanked that body first.
    """
    for match in re.finditer(r"=>(?!\s*\{)", code):
        depth, index = 0, match.end()
        while index < len(code):
            char = code[index]
            if char in "([{":
                depth += 1
            elif char in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif char in ";," and depth == 0:
                break
            index += 1
        span = code[match.end():index]
        if _HELPER_RE.search(span):
            # Same defect as a write held in a braced closure, so same name:
            # the fix is to reach the helper on the synchronous path, and the
            # arrow's brace style is not what a developer has to change.
            return "deferred-write-review"
        if re.search(r"\bawait\b", span) or _PUSH_RE.search(span):
            return "concise-arrow-review"
    return None


def _unresolved_forward_findings(source):
    """Delegations this scan sees but deliberately does not follow.

    Two shapes, both reported rather than passed over: a callee reached only
    from inside a closure (it does not run in the gesture's turn), and a callee
    that itself delegates (one level is all this resolver claims).
    """
    mask = registration._code_mask(source)
    code = "".join(char if mask[index] else " " for index, char in enumerate(source))
    scopes = registration._scopes(source, mask)
    findings = []
    for match in re.finditer(r"\bthis\._withMutationGesture\s*\(", code):
        args, _ = _js_arguments(source, match.end() - 1, mask)
        if len(args) < 2:
            continue
        name = re.fullmatch(r'''(["'])([\w.-]+)\1''', args[0])
        if not name:
            continue
        masked_callback, _ = _mask_nested_functions(args[1])
        synchronous = {one[1] for one in _FORWARD_RE.finditer(masked_callback)}
        deferred = {one[1] for one in _FORWARD_RE.finditer(_js_code(args[1]))} - synchronous
        for callee in sorted(deferred):
            body = _scope_body(source, scopes, callee) or ""
            if re.search(r"\bthis\._pushUndo\s*\(", _js_code(body)):
                findings.append((name[2], "forward-only-in-nested"))
        for callee in sorted(synchronous):
            body = _scope_body(source, scopes, callee) or ""
            if not re.search(r"\bthis\._pushUndo\s*\(", _js_code(body)):
                continue
            onward, _ = _mask_nested_functions(body)
            if {one[1] for one in _FORWARD_RE.finditer(onward)} - {callee}:
                findings.append((name[2], "forward-not-followed"))
    return findings


def _undo_claim_findings(source):
    findings = list(_unresolved_forward_findings(source))
    for gesture, body in _gesture_undo_callbacks(source):
        # Masking runs HERE, after selection. `_gesture_undo_callbacks` finds a
        # callback by the reservation in it; masking first would hide a callback
        # whose only reservation sits in a closure instead of reporting it.
        code, nested = _mask_nested_functions(body)
        pushes = list(_PUSH_RE.finditer(code))
        if not pushes:
            # Selection saw a reservation and the outer path does not hold it.
            findings.append((gesture, "reservation-only-in-nested"))
            continue
        if len(pushes) > 1:
            # A second push replaces the first candidate. Reviewing the whole
            # callback is safer than assigning one later helper to both pushes.
            findings.append((gesture, "multiple-reservations-review"))
            continue
        if "unreadable-nested" in nested:
            findings.append((gesture, "nested-function-review"))
            continue
        concise = _concise_arrow_risk(code)
        if concise:
            findings.append((gesture, concise))
            continue
        if "push-in-nested" in nested:
            findings.append((gesture, "reservation-in-nested-review"))
            continue
        if "helper-in-nested" in nested:
            findings.append((gesture, "deferred-write-review"))
            continue
        calls = list(_HELPER_RE.finditer(code))
        for push in pushes:
            _, push_end = _js_arguments(body, push.end() - 1)
            subsequent = [call for call in calls if call.start() > push_end]
            if not subsequent:
                findings.append((gesture, "missing-mutation-helper"))
                continue
            call = subsequent[0]
            args, call_end = _js_arguments(body, call.end() - 1)
            # `await this._runSceneMutation(...)` calls before suspending. A
            # preceding await, or an await while evaluating its arguments, does
            # suspend first. Parenthesized/chained variations require review.
            prefix = code[push_end + 1:call.start()]
            prefix = re.sub(r"\bawait\s*$", "", prefix)
            gap = re.search(r"\bawait\b", prefix + code[call.end():call_end])
            assignment = re.search(r"\b(?:const|let)\s+([\w$]+)\s*=\s*$", code[:push.start()])
            entry = assignment[1] if assignment else None
            # A reassignment of the captured binding is not the reserved object.
            reassigned = entry and re.search(r"\b" + re.escape(entry) + r"\s*=(?!=|>)", code[push_end + 1:call_end])
            explicit = not reassigned and _explicit_history_entry(args, call[1], entry)
            if gap and not explicit:
                findings.append((gesture, "await-before-claim"))
            elif call[1] == "_queueProjectMutation" and not explicit and not _queue_has_scene_claim(args):
                findings.append((gesture, "queue-scene-claim-review"))
    return findings


def _assert_undo_claim_findings(findings):
    assert set(findings) == set(UNDO_CLAIM_EXEMPTIONS), (
        "Scene Mutation Authoring undo claim: new findings "
        f"{sorted(set(findings) - UNDO_CLAIM_EXEMPTIONS.keys())}; "
        f"stale exceptions {sorted(UNDO_CLAIM_EXEMPTIONS.keys() - set(findings))}")
    assert len(findings) == len(set(findings)), "An existing undo exception hides a second site"
    assert all(reason.strip() for reason in UNDO_CLAIM_EXEMPTIONS.values())


def test_gesture_undo_claims_reach_a_mutation_helper_before_expiry():
    source = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    callbacks = _gesture_undo_callbacks(source)
    # A parser that sees nothing must not pass on an empty exception catalogue.
    # Pinned as a MULTISET, not a set of names: `deleteGuide` reserves undo on
    # two separate popup surfaces, so a set cannot see one of them stop doing
    # it, and a name floor would report full coverage of a shrinking scan.
    assert sorted(name for name, _ in callbacks) == sorted(SCANNED_RESERVING_UNITS), (
        "the set of scanned units reserving undo moved. If a gesture "
        "legitimately gained or lost its reservation, update "
        "SCANNED_RESERVING_UNITS deliberately; if the scan simply stopped "
        "seeing one, the exception catalogue no longer covers it. Found: "
        f"{sorted(name for name, _ in callbacks)}")
    _assert_undo_claim_findings(_undo_claim_findings(source))


def test_the_unscanned_scopes_are_still_the_shapes_their_reasons_describe():
    """An exclusion must not outlive the condition that justified it."""
    source = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    assert all(reason.strip() for reason in UNSCANNED_SCOPES.values())
    scopes = registration._scopes(source)
    legacy = "_showGuideManagementPopupLegacy"
    # Counted, not pattern-matched for a call: `this?.x()`, `this['x']()`,
    # `.call(this)` and a stored reference all revive it without matching a
    # call shape, and this codebase uses optional chaining heavily. Exactly one
    # occurrence is the declaration; anything else is a revival or a second
    # declaration, and both end the exclusion.
    occurrences = sum(path.read_text(encoding="utf-8").count(legacy)
                      for path in (ROOT / "web/js").rglob("*.js"))
    assert occurrences == 1, (
        f"{legacy} occurs {occurrences} times in web/js, so it is no longer "
        "dead and excluding it from the undo scan is no longer free")
    assert len([one for one in scopes if one[0] == "commitStrength"]) == 2, (
        "commitStrength no longer resolves to two scopes, so the name-keyed "
        "lookup that this exclusion works around may now be possible")


def test_a_reservation_inside_a_closure_is_reported_rather_than_unseen():
    """Selection must precede masking, and this is what pins the order.

    `_gesture_undo_callbacks` finds a callback by the reservation in it. Masking
    nested bodies before selection would make a callback whose only reservation
    sits in a closure disappear from the scan entirely, which reads as a pass.
    """
    fixture = _claim_fixture(
        'handler(() => { this._pushUndo("edit"); });\nawait this._runSceneMutation([]);')
    assert [name for name, _ in _gesture_undo_callbacks(fixture)] == ["probe"]
    assert _undo_claim_findings(fixture) == [("probe", "reservation-only-in-nested")]
    # Mask the CALLBACK, the way `_undo_claim_findings` does. Masking the whole
    # fixture would blank the gesture's own `async () => {` body on the first
    # span and satisfy this with no inner closure present at all.
    body = _gesture_undo_callbacks(fixture)[0][1]
    masked, events = _mask_nested_functions(body)
    assert "push-in-nested" in events and not _PUSH_RE.search(masked)
    plain = _claim_fixture('this._pushUndo("edit"); await this._runSceneMutation([]);')
    _, without = _mask_nested_functions(_gesture_undo_callbacks(plain)[0][1])
    assert "push-in-nested" not in without, (
        "the assertion above must depend on the closure, not on the gesture's "
        "own callback body being a brace arrow")


def test_the_masker_preserves_offsets_and_reports_an_unreadable_closure():
    """Two properties every caller depends on and no fixture above proves.

    Offsets: `_undo_claim_findings` takes positions from the masked code and
    indexes the UNMASKED body with them, so the two must stay the same length.
    """
    body = 'this._pushUndo("e"); const f = () => { await x(); }; await this._runSceneMutation([]);'
    masked, _ = _mask_nested_functions(body)
    assert len(masked) == len(body)
    assert "await x()" not in masked and "this._runSceneMutation" in masked
    # `unreadable-nested` is covered here rather than through a callback
    # fixture, and deliberately: a callback only reaches the scan once
    # `_gesture_undo_callbacks` has brace-matched it, so an unterminated nested
    # body inside a balanced callback cannot be constructed. The cascade branch
    # that turns this event into `nested-function-review` is defensive, and
    # malformed input fails earlier and louder in the selection parser.
    truncated = 'const f = () => { this._pushUndo("e");'
    unreadable, events = _mask_nested_functions(truncated)
    assert events == {"unreadable-nested"} and len(unreadable) == len(truncated)


def _claim_fixture(body, name="probe"):
    return f'this._withMutationGesture("{name}", async () => {{\n{body}\n}});'


@pytest.mark.parametrize("body, expected", [
    ('this._pushUndo("edit"); await this._runSceneMutation([]);', []),
    ('await prepare(); this._pushUndo("edit"); await this._runSceneMutation([]);', []),
    ('this._pushUndo("edit"); await this._updateItemProperty("guide", 1, {});', []),
    ('this._pushUndo("edit"); await fetch("/raw");', ["missing-mutation-helper"]),
    ('this._pushUndo("edit"); await prepare(); await this._runSceneMutation([]);', ["await-before-claim"]),
    ('this._pushUndo("edit"); await this._runSceneMutation(await prepare());', ["await-before-claim"]),
    ('const e = this._pushUndo("edit"); await prepare(); await this._runSceneMutation([], {historyEntry: e});', []),
    ('const historyEntry = this._pushUndo("edit"); await prepare(); this._runSceneMutation([], {historyEntry});', []),
    ('const e = this._pushUndo("edit"); await prepare(); this._queueProjectMutation({historyEntry: e});', []),
    ('const e = this._pushUndo("edit"); await prepare(); this._runSceneMutation([{historyEntry: e}]);', ["await-before-claim"]),
    ('const e = this._pushUndo("edit"); await prepare(); this._runSceneMutation([], {historyEntry: other});', ["await-before-claim"]),
    ('let e = this._pushUndo("edit"); await prepare(); e = other; this._runSceneMutation([], {historyEntry: e});', ["await-before-claim"]),
    ('const e = this._pushUndo("edit"); await prepare(); this._runSceneMutation([], {historyEntry: e, ...options});', ["await-before-claim"]),
    ('const e = this._pushUndo("edit"); await prepare(); this._updateItemProperty("guide", 1, {}, {historyEntry: e});', ["await-before-claim"]),
    ('this._pushUndo("edit"); const later = () => this._runSceneMutation([]);', ["deferred-write-review"]),
    ('this._pushUndo("edit"); function later() { this._runSceneMutation([]); }', ["deferred-write-review"]),
    # An unrecognised closure would be credited to the outer path, so the
    # shorthand, class-method and accessor forms are masked like the others.
    ('this._pushUndo("edit"); const h = { onDone() { this._runSceneMutation([]); } };'
     ' await fetch("/raw");', ["deferred-write-review"]),
    ('this._pushUndo("edit"); const h = { get later() { this._runSceneMutation([]); } };'
     ' await fetch("/raw");', ["deferred-write-review"]),
    # `_code_mask` does not tokenize regex literals, so an unbounded `function`
    # search would adopt the `if (...)` block below as its body and mask away a
    # real suspension. The head must belong to its own parameter list.
    ('this._pushUndo("edit"); const re = /function/;'
     ' if (stale) { await this._reloadScene(); } await this._runSceneMutation([]);',
     ["await-before-claim"]),
    # Control-flow blocks are not closures; `registration._NOT_A_SCOPE` is what
    # keeps them on the scanned path.
    ('this._pushUndo("edit"); if (ready) { this._runSceneMutation([]); }', []),
    ('this._pushUndo("edit"); for (const one of many) { this._runSceneMutation([]); }', []),
    # A closure is masked out of the outer path, not a reason to abandon it.
    ('this._pushUndo("edit"); void [1].map((n) => n); await this._runSceneMutation([]);', []),
    ('this._pushUndo("edit"); const opts = {onSupersededByCoalescing: () => { isHead = false; }};'
     ' await this._runSceneMutation([], opts);', []),
    ('this._pushUndo("edit"); const f = async () => { await prepare(); };'
     ' await this._runSceneMutation([]);', []),
    ('this._pushUndo("edit"); sorted.sort((a, b) => a.frame - b.frame);'
     ' await this._runSceneMutation([]);', []),
    # A `function` body is the brace after its parameter list, so a default
    # value holding an object literal cannot be masked as the body.
    ('this._pushUndo("edit"); function later(a = {x: 1}) { return a; }'
     ' await this._runSceneMutation([]);', []),
    # The helper term in the concise-arrow rule is load-bearing: the write reads
    # as preceding the await, so dropping it would report nothing here.
    ('this._pushUndo("edit"); const later = () => this._runSceneMutation([]); await x(); later();',
     ["deferred-write-review"]),
    ('this._pushUndo("edit"); const ready = () => await probe(); await this._runSceneMutation([]);',
     ["concise-arrow-review"]),
    # A concise arrow keeps its expression on the scanned path, so a deferred
    # reservation reads as a second one. Either way the callback is reviewed.
    ('this._pushUndo("edit"); const later = () => this._pushUndo("second"); await x();',
     ["multiple-reservations-review"]),
    # Deferred work inside a closure is reported, never credited to the outer path.
    ('handler(() => { this._pushUndo("edit"); }); await this._runSceneMutation([]);',
     ["reservation-only-in-nested"]),
    ('this._pushUndo("edit"); handler(() => { this._pushUndo("second"); });'
     ' await this._runSceneMutation([]);', ["reservation-in-nested-review"]),
    ('this._pushUndo("edit"); const note = "await ghost(); this._runSceneMutation([])"; /* this._runSceneMutation([]); */ await fetch("/raw");', ["missing-mutation-helper"]),
    ('this._pushUndo("edit"); this._runSceneMutation([]); await repaint();', []),
    ('this._pushUndo("first"); this._pushUndo("second"); this._runSceneMutation([]);', ["multiple-reservations-review"]),
    ('const e=this._pushUndo("edit"); await prepare(); this._runSceneMutation([], {historyEntry:e, "historyEntry":other});', ["await-before-claim"]),
    ('const e=this._pushUndo("edit"); await prepare(); this._runSceneMutation([], {historyEntry:e, ["historyEntry"]:other});', ["await-before-claim"]),
    ('const e=this._pushUndo("edit"); await prepare(); this._runSceneMutation([], {historyEntry:e, [key]:other});', ["await-before-claim"]),
    ('const e=this._pushUndo("edit"); await prepare(); this._runSceneMutation([], {"historyEntry":e});', []),
    ('this._pushUndo("edit"); this._queueProjectMutation({run});', ["queue-scene-claim-review"]),
    ('this._pushUndo("edit"); this._queueProjectMutation({intent:{sceneId}, run});', []),
    ('this._pushUndo("edit"); this._queueProjectMutation({intent:{sceneId:""}, run});', ["queue-scene-claim-review"]),
    *[(f'this._pushUndo("edit"); await this.{helper}([]);', ["missing-mutation-helper"])
      for helper in ("_runVersionedProjectMutation", "_runQueueMutation", "_queuePromptProjectWrite", "_mutateReferences")],
])
def test_undo_claim_scanner_distinguishes_suspension_from_awaiting_the_write(body, expected):
    assert _undo_claim_findings(_claim_fixture(body)) == [("probe", reason) for reason in expected]


def test_undo_claim_tripwire_rejects_injected_raw_and_awaiting_gestures():
    source = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    findings = _undo_claim_findings(source)
    # Exactly the reviewed set, compared against the catalogue rather than a
    # second hardcoded copy of it that could drift from the one being enforced.
    assert sorted(findings) == sorted(UNDO_CLAIM_EXEMPTIONS)
    for body in ('this._pushUndo("probe"); await fetch("/raw");',
                 'this._pushUndo("probe"); await prepare(); this._runSceneMutation([]);'):
        with pytest.raises(AssertionError, match="probe"):
            _assert_undo_claim_findings(_undo_claim_findings(source + _claim_fixture(body)))
    # The expansion's own proof: a defect in a `*WithinGesture` CALLEE, which
    # the scan could not see before, reported under its gesture's name.
    marker = "    _isLaneVisibilityControlDisabled(entry) {"
    assert source.count(marker) == 1, "the callee-injection anchor moved"
    injected = (
        '    async _auditProbe() {\n'
        '        return this._withMutationGesture("auditProbe",\n'
        '            () => this._auditProbeWithinGesture());\n'
        '    }\n\n'
        '    async _auditProbeWithinGesture() {\n'
        '        this._pushUndo("probe");\n'
        '        await this._reloadScene();\n'
        '        await this._runSceneMutation([]);\n'
        '    }\n\n')
    with pytest.raises(AssertionError, match="auditProbe.*await-before-claim"):
        _assert_undo_claim_findings(_undo_claim_findings(
            source.replace(marker, injected + marker, 1)))
    with pytest.raises(AssertionError, match="stale exceptions"):
        _assert_undo_claim_findings([])
    with pytest.raises(AssertionError, match="second site"):
        _assert_undo_claim_findings(findings + findings)


def test_deferred_undo_claims_keep_their_explicit_handoff():
    source = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    _assert_deferred_undo_claims(source)


def _forwards_explicit_entry(scope_source, entry):
    """Whether this scope hands `entry` to a helper that transports it.

    Only the helpers whose options argument actually carries `historyEntry`
    count. `_updateItemProperty` is reachable but drops the option, so spelling
    it there would certify a handoff that never happens.
    """
    code = _js_code(scope_source)
    for helper, position in _MUTATION_HELPERS.items():
        if position is None:
            continue
        for call in re.finditer(r"this\." + re.escape(helper) + r"\s*\(", code):
            args, _ = _js_arguments(scope_source, call.end() - 1)
            if _explicit_history_entry(args, helper, entry):
                return True
    return False


def _assert_deferred_undo_claims(source):
    scopes = registration._scopes(source)
    for label, contract in DEFERRED_UNDO_CLAIMS.items():
        assert contract["reason"] and contract["expiry"]
        assert contract["form"] in {"member-storage", "argument", "local-helper"}
        def span(name):
            matches = [(start, end) for scope, start, end in scopes if scope == name]
            assert len(matches) == 1, f"Deferred undo claim lost method {name}"
            return matches[0]
        def body(name):
            start, end = span(name)
            return source[start:end + 1]
        def signature(name):
            # `_scopes` spans start at the body brace, so a destructured
            # parameter lives BEFORE the span and has to be sliced back to.
            start, _ = span(name)
            return source[source.rindex(name, 0, start):start]
        producer, consumer = body(contract["producer"]), body(contract["consumer"])
        if contract["form"] == "argument":
            # Three links, because severing any one of them loses the claim
            # while the other two still read as intact.
            binding, parameter = contract["binding"], contract["parameter"]
            # Searched RAW, not through `_js_code`: the gesture label is a
            # string literal and the code mask blanks its contents, so a masked
            # search would match any reservation in the method.
            assert re.search(r"const\s+" + re.escape(binding) + r'\s*=\s*this\._pushUndo\("'
                             + re.escape(contract["label"]) + r'"\)', producer), (
                f"Deferred undo claim lost the reserved binding for {label}")
            # Tied to the CALL, not merely present in the method: an object
            # literal spelling `{ undoEntry }` somewhere in the body proves
            # nothing, and a refactor that moves it rather than deleting it
            # would leave the handoff severed while this still read as intact.
            handed = False
            for call in re.finditer(r"this\." + re.escape(contract["entrypoint"])
                                    + r"\s*\(", _js_code(producer)):
                arguments, _ = _js_arguments(producer, call.end() - 1)
                for argument in arguments:
                    members = _object_members(argument)
                    handed |= bool(members) and any(
                        key == parameter and _js_code(value).strip() == binding
                        for key, value in members)
            assert handed, (
                f"Deferred undo claim stopped handing the entry to "
                f"{contract['entrypoint']} for {label}")
            assert re.search(r"\{\s*" + re.escape(parameter) + r"\s*[,}=]",
                             signature(contract["consumer"])), (
                f"Deferred undo claim lost the receiving parameter for {label} "
                f"in {contract['consumer']}")
        elif contract["form"] == "local-helper":
            helper, parameter = contract["helper"], contract["parameter"]
            declaration = re.search(r"const\s+" + re.escape(helper) + r"\s*=\s*\(\s*\{",
                                    _js_code(producer))
            assert declaration, f"Deferred undo claim lost the local helper for {label}"
            start = declaration.end() - 1
            end = registration._match_delimiter(producer, start, "{", "}")
            assert end >= 0, f"Deferred undo claim: unreadable helper parameters for {label}"
            # Read as a destructuring PARAMETER list, not through
            # `_object_members`: that parses object literals, and a parameter
            # carrying a default (`historyEntry = null`) is not one, so it
            # would return None and the check would pass on unreadability.
            parameters = _js_code(producer[start:end + 1])
            assert re.search(r"[{,]\s*" + re.escape(parameter)
                             + r"\s*(?:=[^,}]*)?\s*[,}]", parameters), (
                f"Deferred undo claim lost the helper's entry parameter for {label}")
            for reserved in contract["bindings"]:
                assert re.search(r"const\s+" + re.escape(reserved)
                                 + r"\s*=\s*this\._pushUndo\(", _js_code(producer)), (
                    f"Deferred undo claim lost the {reserved} reservation for {label}")
                assert re.search(re.escape(parameter) + r"\s*:\s*" + re.escape(reserved)
                                 + r"\b", _js_code(producer)), (
                    f"Deferred undo claim stopped forwarding {reserved} for {label}")
        elif label == "trim":
            assert re.search(r'const\s+historyEntry\s*=\s*this\._pushUndo\("trim"\)', producer), "Trim lost reserved entry binding"
            code = _js_code(producer)
            starts = list(re.finditer(r"this\._trimItem\s*=\s*\{", code))
            assert len(starts) == 1, "Review trim storage assignments"
            start = starts[0].end() - 1
            end = registration._match_delimiter(producer, start, "{", "}")
            assert end >= 0
            fields = _object_members(producer[start:end + 1], allowed_spreads=("edgeHit", "trimLimits"))
            assert fields is not None, "Review changed trim storage shape"
            assert [value for key, value in fields if key == "historyEntry"] == ["historyEntry"], "Trim lost stored entry"
            assert "this._commitTrim(this._trimItem)" in _js_code(producer)
        else:
            assert re.search(r'this\._dragHistoryEntry\s*=\s*this\._pushUndo\("move items"\)', producer), "Move lost reserved entry storage"
            assert re.search(r"const\s+historyEntry\s*=\s*this\._dragHistoryEntry\s*;", _js_code(consumer)), "Move lost retained entry binding"
        # The one obligation every form shares: whatever the transport, the
        # reserved object must reach a helper that actually carries it.
        assert _forwards_explicit_entry(consumer, contract["entry"]), (
            f"Deferred undo claim lost explicit historyEntry for {label}")
    # This is a lexical handoff pin, not a proof of drag cancellation or runtime
    # entry identity. No source scan reaches across those asynchronous lifecycles.


def test_deferred_handoff_tripwire_rejects_severed_storage_and_bindings():
    source = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    trim = source.index("this._trimItem = {")
    end = registration._match_delimiter(source, source.index("{", trim), "{", "}")
    storage = source[trim:end + 1]
    assert "historyEntry," in storage
    broken = source[:trim] + storage.replace("historyEntry,", "", 1) + source[end + 1:]
    with pytest.raises(AssertionError, match="Trim lost stored entry"):
        _assert_deferred_undo_claims(broken)
    old = "const historyEntry = this._dragHistoryEntry;"
    assert source.count(old) == 1
    with pytest.raises(AssertionError, match="Move lost retained entry binding"):
        _assert_deferred_undo_claims(source.replace(old, "const historyEntry = null;", 1))


# Each argument- and helper-form link, severed on its own. A pin nobody has
# watched fail is documentation; these are the shapes a refactor would produce.
_SEVERANCE_CASES = [
    ('const undoEntry = this._pushUndo("toggle track visibility");',
     'this._pushUndo("toggle track visibility");', 2, "lost the reserved binding"),
    ("{ undoEntry }", "{}", 2, "stopped handing the entry to _applyHeaderVisibilityBulk"),
    ("{ undoEntry = null } = {}", "{} = {}", 1, "lost the receiving parameter"),
    ("historyEntry: undoEntry,", "", 1, "lost explicit historyEntry"),
    ("operation, historyEntry = null,", "operation,", 1,
     "lost the helper's entry parameter"),
    ("historyEntry: driverUndoEntry,", "", 1, "stopped forwarding driverUndoEntry"),
    ("historyEntry: assetUndoEntry,", "", 2, "stopped forwarding assetUndoEntry"),
]


def test_an_orphaned_entry_object_does_not_read_as_a_handoff():
    """The severance a text-deletion fixture cannot express.

    A refactor that MOVES the `{ undoEntry }` literal off the call rather than
    deleting it leaves the text present and the handoff gone. Checking the
    literal's existence would pass; checking the call's arguments does not.
    """
    source = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    orphaned = source.replace("{ undoEntry }", "{}").replace(
        'const undoEntry = this._pushUndo("toggle track visibility");',
        'const undoEntry = this._pushUndo("toggle track visibility");'
        "\n        const orphan = { undoEntry };")
    with pytest.raises(AssertionError, match="stopped handing the entry to"):
        _assert_deferred_undo_claims(orphaned)


@pytest.mark.parametrize("old, new, count, message", _SEVERANCE_CASES)
def test_severing_any_single_claim_link_fails_the_contract(old, new, count, message):
    source = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    assert source.count(old) == count, (
        f"the severance fixture no longer describes the source: {old!r}")
    with pytest.raises(AssertionError, match=re.escape(message)):
        _assert_deferred_undo_claims(source.replace(old, new))


# Declared DOM/host-free leaf mirrors, not discovery of all duplicated logic.
# Each scope names the reproduced transform, not every export in the JS module.
# Embedded UI transforms (e.g. prompt_context_chips.normalizePromptDocument)
# still owe parity under the workflow, but are outside this leaf inventory.
# Coalescing has dispatcher-equivalence tests, not a server coalescer twin;
# addressing has no server twin yet. Neither belongs in this list.
# Header form: // @server-mirror relative/python_file.py::symbol
# Authority citations and test references prove existence, never semantic coverage.
MIRRORED_MODULES = {
    "scene_guide_geometry.js": {
        "scope": "Guide-swap applicability and raw frame ordering for optimistic paint.",
        "authorities": ("server/routes.py::_apply_swap_guides",),
        "tests": ("test_guide_swap.py::test_guide_swap_geometry_matches_server",),
    },
    "selection_constraints.js": {
        "scope": "Execution-window snapping, context and padding math.",
        "authorities": ("server/guide_collision.py::resolve_execution_window",),
        "tests": ("test_selection_constraints_js.py::test_frontend_execution_window_matches_backend_fixtures",),
    },
    "guide_collision.js": {
        "scope": "Guide/driver collision display decisions.",
        "authorities": ("server/guide_collision.py::resolve_guide_collisions",),
        "tests": (),
        "exemption": {
            "reason": "test_guide_collision_js uses hand-synced expected literals, not the Python decision.",
            "owner": "Guide/driver collision maintainer (server/guide_collision.py)",
            "expiry": "Remove when one fixture corpus compares JS and Python collision decisions directly.",
        },
    },
    "metadata_collector_shape.js": {
        "scope": "Legacy value_N, V3 values.value_N and label_N slot naming only, not capacity policy.",
        "authorities": ("nodes/metadata_collector.py::collect_metadata",
                        "nodes/metadata_collector.py::SonderMetadataCollector",
                        "nodes/metadata_collector_v3.py::SonderMetadataCollectorV3"),
        "tests": (),
        "exemption": {
            "reason": "Widget visibility tests cover literal slot examples without comparing the backend schema/grammar.",
            "owner": "Metadata collector maintainer (nodes/metadata_collector.py)",
            "expiry": "Remove when generated V1/V3 slot names and rejection cases are compared with the JS parser.",
        },
    },
    "lane_registry.js": {
        "scope": "Shared lane descriptor fields, with frontend-only fields classified separately.",
        "authorities": ("server/lane_registry.py::LANE_DESCRIPTORS",),
        "tests": ("test_lane_registry_parity.py::test_frontend_backend_descriptor_parity_and_classified_fields",),
    },
    "prompt_channel_templates.js": {
        "scope": "Preset catalog, custom normalization, resolution, label/global policy and timecodes.",
        "authorities": ("server/prompt_channel_templates.py::PROMPT_CHANNEL_TEMPLATE_PRESETS",
                        "server/prompt_channel_templates.py::normalize_channel_template",
                        "server/prompt_channel_templates.py::resolve_channel_template",
                        "server/prompt_channel_templates.py::template_labels_on",
                        "server/prompt_channel_templates.py::format_shot_timecode"),
        "tests": tuple("test_prompt_channel_templates_js.py::" + name for name in (
            "test_preset_catalog_matches_between_python_and_javascript",
            "test_global_channel_flag_matches_between_python_and_javascript",
            "test_template_resolution_matches_between_python_and_javascript",
            "test_label_policy_matches_between_python_and_javascript",
            "test_custom_template_normalization_matches_between_python_and_javascript",
            "test_timecode_matches_between_python_and_javascript")),
    },
    "prompt_composition.js": {
        "scope": "Channel normalization, header split/collapse, and compose-only display/join rules; no server gap-fill resolver.",
        "authorities": tuple("server/prompt_payload.py::" + name for name in (
            "normalize_channels", "split_channel_headers", "collapse_channels_for_template",
            "join_channel_headers", "compose_range_prompt")),
        "tests": (
            "test_prompt_channel_templates_js.py::test_channel_normalizer_matches_between_python_and_javascript",
            "test_prompt_channel_collapse.py::test_split_matches_between_python_and_javascript",
            "test_prompt_channel_collapse.py::test_collapse_matches_between_python_and_javascript"),
        "exemption": {
            "reason": "Direct normalizer/split/collapse comparisons do not pin all composition and join rules independently.",
            "owner": "Prompt composition maintainer (server/prompt_payload.py)",
            "expiry": "Remove when compose-only display and join decisions have direct cross-language fixtures.",
        },
    },
    "prompt_tokens.js": {
        "scope": "Token/handle grammar and declared vocabulary, including compiler-owned shot tokens.",
        "authorities": ("server/prompt_tokens.py::_HANDLE_RE", "server/prompt_tokens.py::_TOKEN_RE",
                        "server/prompt_tokens.py::_declarations", "server/prompt_context.py::SHOT_ORDINAL_KEY",
                        "server/prompt_payload.py::SHOT_LABEL_TEMPLATE"),
        "tests": tuple("test_prompt_tokens.py::" + name for name in (
            "test_python_and_javascript_handle_grammars_match",
            "test_python_and_javascript_token_vocabularies_match",
            "test_format_declared_token_kind_has_python_javascript_parity",
            "test_declared_token_grammar_matches_between_python_and_javascript")),
    },
    "reference_library_model.js": {
        "scope": "Default Reference class and tag normalization/preset-asset compatibility intent; input tolerance and case handling are not certified equivalent.",
        "authorities": ("server/timeline_state.py::default_reference_class",
                        "server/timeline_state.py::normalize_reference_tags",
                        "server/routes.py::_validated_reference_tags"),
        "tests": (),
        "exemption": {
            "reason": "Reference Library JS tests assert literals, not backend decisions; normalization/case/input boundaries also need comparison.",
            "owner": "Reference Library maintainer (server/routes.py::_validated_reference_tags)",
            "expiry": "Remove when direct class/tag/asset decision comparisons pin intended equivalence and explain deliberate input-tolerance differences.",
        },
    },
    "reference_lane_identity.js": {
        "scope": "Lane population and member-population compatibility decisions.",
        "authorities": ("server/minimax_h3.py::lane_population", "server/routes.py::member_population_compatible"),
        "tests": (
            "test_reference_timeline.py::test_lane_population_matches_between_python_and_the_browser",
            "test_reference_timeline.py::test_member_population_compatibility_matches_between_python_and_the_browser"),
    },
    "reference_resolution.js": {
        "scope": "Winner/threshold/prose verdicts, output liveness and derived member prompt text.",
        "authorities": ("server/reference_resolution.py::resolve_reference_verdicts",
                        "server/reference_resolution.py::reference_live_outputs",
                        "nodes/reference_core.py::_assemble_prompt",
                        "nodes/reference_core.py::member_prompt_fragment"),
        "tests": (
            "test_reference_timeline.py::test_python_and_browser_reference_resolvers_share_most_specific_semantics",
            "test_reference_timeline.py::test_recipe_output_liveness_mirrors_across_backend_and_frontend",
            "test_reference_panel_js.py::test_derived_prompt_matches_between_python_and_javascript",
            "test_reference_panel_js.py::test_reference_threshold_matches_between_python_and_javascript",
            "test_reference_subject_registry.py::test_member_prompt_fragment_matches_between_python_and_javascript",
            "test_reference_subject_registry.py::test_token_vocabulary_matches_between_python_and_javascript",
            "test_reference_prose_policy.py::test_reference_verdict_python_js_parity"),
    },
    "scene_link_groups.js": {
        "scope": "Route link pruning, explicit link/unlink editing and id-collision refusal.",
        "authorities": tuple("server/routes.py::" + name for name in (
            "_prune_linked_item_groups", "_unlink_refs", "_expand_linked_refs", "_add_link_group")),
        "tests": tuple("test_link_group_parity.py::" + name for name in (
            "test_link_group_normalization_is_identical_in_both_languages",
            "test_a_group_id_collision_is_resolved_the_same_way_in_both_languages",
            "test_explicit_group_edit_matches_server")),
    },
    "scene_move_geometry.js": {
        "scope": "Start-only duration-preserving moves and linked pure-move bounds/refusals, not whole update handlers.",
        "authorities": tuple("server/routes.py::" + name for name in (
            "_apply_update_clip", "_apply_update_audio_track", "_apply_linked_bounds_update", "_apply_ref_bounds")),
        "tests": tuple("test_move_geometry_parity.py::" + name for name in (
            "test_a_clip_move_preserves_its_duration_identically_in_both_languages",
            "test_an_audio_move_preserves_its_duration_identically_in_both_languages",
            "test_a_linked_move_adds_one_delta_to_every_member",
            "test_the_client_declines_exactly_the_linked_moves_the_server_refuses")),
    },
    "scene_reference_geometry.js": {
        "scope": "Reference bounds, canonical member-ref record shape and staged-row defaults, not all route applicability.",
        "authorities": tuple("server/routes.py::" + name for name in (
            "_reference_item_bounds", "_canonical_reference_member_refs", "_apply_create_reference_item")),
        "tests": tuple("test_reference_geometry_parity.py::" + name for name in (
            "test_reference_bounds_agree_in_both_languages",
            "test_an_inverted_range_is_refused_on_both_sides_rather_than_repaired",
            "test_the_painted_member_record_is_the_record_the_route_stores",
            "test_the_painted_row_matches_the_stored_row_field_for_field",
            "test_a_stored_member_round_trips_through_an_append_unchanged")),
    },
    "scene_split_geometry.js": {
        "scope": "Clip/audio split-half geometry and source-duration sentinel semantics, not link repartition.",
        "authorities": ("server/routes.py::_split_clip_object", "server/routes.py::_split_audio_object"),
        "tests": tuple("test_split_geometry_parity.py::" + name for name in (
            "test_clip_split_halves_match_field_for_field", "test_audio_split_halves_match_field_for_field",
            "test_the_split_mirror_reproduces_the_two_meanings_of_total_source_frames")),
    },
}


def _mirror_header(source):
    """Only leading comments declare a mirror; imports/code end the header."""
    return re.match(r"\s*(?:(?://[^\n]*(?:\n|$)|/\*[\s\S]*?\*/)\s*)*", source).group()


def _assert_mirror_markers(sources, inventory):
    marked = {}
    for filename, source in sources.items():
        header = _mirror_header(source)
        if "@server-mirror" not in header:
            continue
        markers = re.findall(r"^// @server-mirror ([\w/]+\.py::\w+)$", header, re.MULTILINE)
        assert len(markers) == header.count("@server-mirror"), f"Malformed mirror marker: {filename}"
        assert len(markers) == len(set(markers)), f"Duplicate mirror marker: {filename}"
        marked[filename] = set(markers)
    assert set(marked) == set(inventory), (
        f"Mirror inventory drift: unlisted {sorted(set(marked) - set(inventory))}; "
        f"unmarked {sorted(set(inventory) - set(marked))}")
    for filename, row in inventory.items():
        assert marked[filename] == set(row["authorities"]), f"Mirror authority drift: {filename}"


def _assert_mirror_obligations(inventory, root):
    trees = {}
    def tree(path):
        assert path.is_file(), f"Missing mirror contract file: {path}"
        if path not in trees:
            trees[path] = ast.parse(path.read_text(encoding="utf-8"))
        return trees[path]
    for filename, row in inventory.items():
        assert row["scope"].strip(), f"Missing mirror scope: {filename}"
        assert row["authorities"], f"Missing mirror authority: {filename}"
        assert row["tests"] or row.get("exemption"), f"No parity disposition: {filename}"
        if "exemption" in row:
            for field in ("reason", "owner", "expiry"):
                assert row["exemption"].get(field, "").strip(), f"Missing exemption {field}: {filename}"
        for ref in row["authorities"]:
            path, name = ref.split("::")
            body = tree(root / path).body
            names = {node.name for node in body
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
            targets = [target for stmt in body if isinstance(stmt, (ast.Assign, ast.AnnAssign))
                       for target in (stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target])]
            names.update(node.id for target in targets for node in ast.walk(target)
                         if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store))
            assert name in names, f"Missing mirror authority: {ref}"
        for ref in row["tests"]:
            path, name = ref.split("::")
            assert name.startswith("test_") and any(
                isinstance(node, ast.FunctionDef) and node.name == name
                for node in tree(root / "tests" / path).body), f"Missing parity test: {ref}"


def test_declared_mirror_headers_match_the_inventory_both_ways():
    sources = {path.relative_to(ROOT / "web/js").as_posix(): path.read_text(encoding="utf-8")
               for path in (ROOT / "web/js").rglob("*.js")}
    _assert_mirror_markers(sources, MIRRORED_MODULES)


def test_every_declared_mirror_has_a_live_parity_disposition():
    _assert_mirror_obligations(MIRRORED_MODULES, ROOT)
    assert sum(len(row["authorities"]) for row in MIRRORED_MODULES.values()) == 44

@pytest.mark.parametrize("name, valid", [("TABLE", True), ("row", False), ("transform", True)])
def test_mirror_authorities_are_module_declarations(tmp_path, name, valid):
    (tmp_path / "probe.py").write_text(
        "TABLE = [row for row in source]\nasync def transform():\n    pass\n", encoding="utf-8")
    inventory = {"probe.js": {
        "scope": "Declaration resolution fixture", "authorities": (f"probe.py::{name}",),
        "tests": (), "exemption": {"reason": "Fixture", "owner": "test", "expiry": "Fixture only"}}}
    if valid:
        _assert_mirror_obligations(inventory, tmp_path)
    else:
        with pytest.raises(AssertionError, match="Missing mirror authority"):
            _assert_mirror_obligations(inventory, tmp_path)


@pytest.mark.parametrize("drift", ["unlisted", "unmarked", "late-marker", "duplicate", "malformed", "wrong-authority"])
def test_mirror_marker_ratchet_rejects_injected_drift(drift):
    source = "// @server-mirror server/guide_collision.py::resolve_execution_window\nexport const x = 1;"
    sources = {"selection_constraints.js": source}
    inventory = {"selection_constraints.js": MIRRORED_MODULES["selection_constraints.js"]}
    if drift == "unlisted":
        sources["new_mirror.js"] = source
    elif drift == "unmarked":
        sources["selection_constraints.js"] = "export const x = 1;"
    elif drift == "late-marker":
        sources["selection_constraints.js"] = "export const x = 1;\n" + source
    elif drift == "duplicate":
        sources["selection_constraints.js"] = source.splitlines()[0] + "\n" + source
    elif drift == "malformed":
        sources["selection_constraints.js"] = source.replace("server/", "./server/")
    else:
        sources["selection_constraints.js"] = source.replace("resolve_execution_window", "wrong")
    with pytest.raises(AssertionError, match="mirror|Mirror"):
        _assert_mirror_markers(sources, inventory)


@pytest.mark.parametrize("drift", ["no-disposition", "missing-test-file", "missing-test", "missing-authority",
                                  "missing-reason", "missing-owner", "missing-expiry"])
def test_mirror_disposition_ratchet_rejects_injected_drift(drift):
    row = copy.deepcopy(MIRRORED_MODULES["selection_constraints.js"])
    if drift == "no-disposition":
        row["tests"] = ()
    elif drift == "missing-test-file":
        row["tests"] = ("test_nonexistent_mirror.py::test_comparison",)
    elif drift == "missing-test":
        row["tests"] = ("test_selection_constraints_js.py::test_nonexistent_comparison",)
    elif drift == "missing-authority":
        row["authorities"] = ("server/guide_collision.py::nonexistent_transform",)
    else:
        row["tests"] = ()
        row["exemption"] = dict(MIRRORED_MODULES["guide_collision.js"]["exemption"])
        row["exemption"][drift.removeprefix("missing-")] = " "
    with pytest.raises(AssertionError, match="parity|Parity|mirror|Mirror|exemption"):
        _assert_mirror_obligations({"new_mirror.js": row}, ROOT)
