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


# Undo claims are gesture-callback scoped, not method scoped: a picker builder
# and its eventual writer have different synchronous lifetimes. This is a lexical
# tripwire, not a JavaScript interpreter or a proof of control flow. It does not
# follow calls into other methods, implicit promise continuations, or arbitrary
# aliases. Existing scanner masking also excludes template interpolations and
# does not tokenize regex literals. Behavioral history tests own runtime claims.
# Nested functions containing relevant events fail for review rather than being
# credited as synchronous work in their outer callback.
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
    ("swapGuides", "missing-mutation-helper"): (
        "Existing raw /guides/swap fetch never claims the undo entry. Bug tracker "
        "owns the defect; delete this exception when the gesture uses a stamping "
        "mutation path and its regression is verified."),
}

DEFERRED_UNDO_CLAIMS = {
    "trim": {
        "producer": "_setupTimelineEvents", "consumer": "_commitTrim",
        "storage": "_trimItem", "entry": "trimInfo.historyEntry",
        "reason": "Pointer-down reserves history; pointer-up forwards that exact entry.",
        "expiry": "Remove when trim reserves and queues in one synchronous callback.",
    },
    "move items": {
        "producer": "_setupTimelineEvents", "consumer": "_commitItemMove",
        "storage": "_dragHistoryEntry", "entry": "historyEntry",
        "reason": "Pointer-down reserves history; move commit forwards the retained entry.",
        "expiry": "Remove when move reserves and queues in one synchronous callback.",
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


def _gesture_undo_callbacks(source):
    mask = registration._code_mask(source)
    code = "".join(char if mask[index] else " " for index, char in enumerate(source))
    callbacks = []
    for match in re.finditer(r"\bthis\._withMutationGesture\s*\(", code):
        args, end = _js_arguments(source, match.end() - 1, mask)
        # A forwarding callback can call a WithinGesture method; this lexical
        # scan deliberately does not claim to inspect those callee bodies.
        if len(args) < 2 or not re.search(r"\bthis\._pushUndo\s*\(", _js_code(args[1])):
            continue
        name = re.fullmatch(r'''(["'])([\w.-]+)\1''', args[0])
        assert name, "Undo claim scan: a callback reserving undo needs a literal gesture name"
        callback = args[1]
        cb_code = _js_code(callback)
        head = re.match(r"\s*(?:async\s+)?(?:\([^)]*\)|[\w$]+)\s*=>\s*\{", cb_code)
        assert head, f"Undo claim scan: review unsupported callback shape for {name[2]}"
        brace = head.end() - 1
        close = registration._match_delimiter(callback, brace, "{", "}")
        assert close >= 0 and not cb_code[close + 1:].strip(), "Undo claim scan: incomplete callback"
        callbacks.append((name[2], callback[brace + 1:close]))
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


def _undo_claim_findings(source):
    findings = []
    helper_re = re.compile(r"\bthis\.(" + "|".join(_MUTATION_HELPERS) + r")\s*\(")
    for gesture, body in _gesture_undo_callbacks(source):
        code = _js_code(body)
        pushes = list(re.finditer(r"\bthis\._pushUndo\s*\(", code))
        if len(pushes) > 1:
            # A second push replaces the first candidate. Reviewing the whole
            # callback is safer than assigning one later helper to both pushes.
            findings.append((gesture, "multiple-reservations-review"))
            continue
        # Conservative around closures: even an await inside an unrelated arrow
        # is not an outer suspension. Force review instead of reasoning across it.
        if re.search(r"=>|\bfunction\b", code):
            findings.append((gesture, "nested-function-review"))
            continue
        calls = list(helper_re.finditer(code))
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
    assert {name for name, _ in callbacks} >= {"replaceGuideImage", "swapGuides"}
    _assert_undo_claim_findings(_undo_claim_findings(source))


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
    ('this._pushUndo("edit"); const later = () => this._runSceneMutation([]);', ["nested-function-review"]),
    ('this._pushUndo("edit"); function later() { this._runSceneMutation([]); }', ["nested-function-review"]),
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
    assert findings == [("swapGuides", "missing-mutation-helper")]
    for body in ('this._pushUndo("probe"); await fetch("/raw");',
                 'this._pushUndo("probe"); await prepare(); this._runSceneMutation([]);'):
        with pytest.raises(AssertionError, match="probe"):
            _assert_undo_claim_findings(_undo_claim_findings(source + _claim_fixture(body)))
    with pytest.raises(AssertionError, match="stale exceptions"):
        _assert_undo_claim_findings([])
    with pytest.raises(AssertionError, match="second site"):
        _assert_undo_claim_findings(findings + findings)


def test_deferred_undo_claims_keep_their_explicit_handoff():
    source = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    _assert_deferred_undo_claims(source)


def _assert_deferred_undo_claims(source):
    scopes = registration._scopes(source)
    for label, contract in DEFERRED_UNDO_CLAIMS.items():
        assert contract["reason"] and contract["expiry"]
        def body(name):
            matches = [(start, end) for scope, start, end in scopes if scope == name]
            assert len(matches) == 1, f"Deferred undo claim lost method {name}"
            start, end = matches[0]
            return source[start:end + 1]
        producer, consumer = body(contract["producer"]), body(contract["consumer"])
        if label == "trim":
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
        forwarded = False
        for call in re.finditer(r"this\._runSceneMutation\s*\(", _js_code(consumer)):
            args, _ = _js_arguments(consumer, call.end() - 1)
            forwarded |= _explicit_history_entry(args, "_runSceneMutation", contract["entry"])
        assert forwarded, f"Deferred undo claim lost explicit historyEntry for {label}"
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


# Declared DOM/host-free leaf mirrors, not discovery of all duplicated logic.
# Each scope names the reproduced transform, not every export in the JS module.
# Embedded UI transforms (e.g. prompt_context_chips.normalizePromptDocument)
# still owe parity under the workflow, but are outside this leaf inventory.
# Coalescing has dispatcher-equivalence tests, not a server coalescer twin;
# addressing has no server twin yet. Neither belongs in this list.
# Header form: // @server-mirror relative/python_file.py::symbol
# Authority citations and test references prove existence, never semantic coverage.
MIRRORED_MODULES = {
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
            names = {node.name for node in body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
            names.update(node.id for stmt in body if isinstance(stmt, (ast.Assign, ast.AnnAssign))
                         for node in ast.walk(stmt) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store))
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
