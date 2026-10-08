import ast
from pathlib import Path

from server import prompt_graph

REPO_ROOT = Path(__file__).resolve().parents[1]
STDLIB_IMPORTS_ALLOWED = {"typing"}


def _node(class_type, inputs=None):
    return {"class_type": class_type, "inputs": dict(inputs or {})}


def test_module_level_imports_are_standard_library_only():
    # The module must stay readable without the editor node's torch/cv2 stack.
    # Importing it through the ``server`` package still loads routes (and torch),
    # so this checks the module's own imports rather than a runtime import.
    tree = ast.parse((REPO_ROOT / "server" / "prompt_graph.py").read_text(encoding="utf-8"))
    imported = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "prompt_graph must not import sibling modules"
            imported.add((node.module or "").split(".")[0])
    assert imported <= STDLIB_IMPORTS_ALLOWED, imported


def test_linked_node_id_reads_links_and_ignores_literals():
    assert prompt_graph.linked_node_id(["12", 0]) == "12"
    assert prompt_graph.linked_node_id((7, 1)) == "7"
    assert prompt_graph.linked_node_id(["733:457", 0]) == "733:457"
    assert prompt_graph.linked_node_id("12") is None
    assert prompt_graph.linked_node_id([]) is None
    assert prompt_graph.linked_node_id([None, 0]) is None
    assert prompt_graph.linked_node_id(5) is None


def test_prompt_bool_accepts_common_spellings_and_defaults_false():
    for truthy in (True, 1, 2.5, "1", "true", " Yes ", "ON"):
        assert prompt_graph.prompt_bool(truthy) is True
    for falsy in (False, 0, 0.0, "0", "false", "off", "", None, ["1", 0], {}):
        assert prompt_graph.prompt_bool(falsy) is False


def test_node_inputs_tolerates_malformed_prompts():
    prompt = {"1": _node("A", {"x": 1}), "2": {"class_type": "B", "inputs": "bad"}, "3": "bad"}
    assert prompt_graph.node_inputs(prompt, "1") == {"x": 1}
    assert prompt_graph.node_inputs(prompt, 1) == {"x": 1}
    assert prompt_graph.node_inputs(prompt, "2") == {}
    assert prompt_graph.node_inputs(prompt, "3") == {}
    assert prompt_graph.node_inputs(prompt, "missing") == {}
    assert prompt_graph.node_inputs(None, "1") == {}


def test_upstream_reaches_walks_chains_and_subgraph_keys():
    prompt = {
        "1": _node("SonderEditor"),
        "733:457": _node("Passthrough", {"project": ["1", 0]}),
        "9": _node("Passthrough", {"value": ["733:457", 0], "literal": 3}),
        "10": _node("Unrelated"),
    }
    assert prompt_graph.upstream_reaches(prompt, "9", "1") is True
    assert prompt_graph.upstream_reaches(prompt, "733:457", 1) is True
    assert prompt_graph.upstream_reaches(prompt, "10", "1") is False
    assert prompt_graph.upstream_reaches(prompt, None, "1") is False
    assert prompt_graph.upstream_reaches(prompt, "1", "1") is True


def test_upstream_reaches_terminates_on_cycles():
    prompt = {
        "1": _node("SonderEditor"),
        "2": _node("Loop", {"a": ["3", 0]}),
        "3": _node("Loop", {"b": ["2", 0]}),
    }
    assert prompt_graph.upstream_reaches(prompt, "2", "1") is False


def test_execution_reaches_save_with_mark_matches_mark_and_editor():
    prompt = {
        "1": _node("SonderEditor"),
        "2": _node("SonderEditor"),
        "3": _node("SonderSaveVideo", {"project": ["2", 0], "mark_queue_complete": True}),
        "4": _node("SonderSaveBridge", {"project": ["1", 0], "mark_queue_complete": "false"}),
        "5": _node("SaveImage", {"project": ["1", 0], "mark_queue_complete": True}),
    }
    reaches = prompt_graph.execution_reaches_save_with_mark
    assert reaches(prompt, "2", True) is True
    assert reaches(prompt, "2", False) is False
    assert reaches(prompt, "1", True) is False
    assert reaches(prompt, "1", False) is True
    assert reaches(prompt, "", True) is False
    assert reaches(prompt, None, True) is False
    assert reaches("not a prompt", "1", True) is False


def test_mark_fed_by_a_primitive_reads_as_its_value():
    # The editor decides consume versus peek from the prompt before the save
    # runs, so a linked advance toggle must read as the value it will carry.
    # A Controls binding is never a link: it sets the literal itself.
    reaches = prompt_graph.execution_reaches_save_with_mark
    for source, on, off in (
        ("PrimitiveBoolean", {"value": True}, {"value": False}),
    ):
        for inputs, expected in ((on, True), (off, False)):
            prompt = {
                "1": _node("SonderEditor"),
                "2": _node(source, inputs),
                "3": _node("SonderSaveVideo", {"project": ["1", 0], "mark_queue_complete": ["2", 0]}),
            }
            assert reaches(prompt, "1", True) is expected, (source, inputs)
            assert reaches(prompt, "1", False) is (not expected), (source, inputs)


def test_mark_fed_by_any_other_link_reads_as_unmarked_known_limitation():
    # Known limitation, not a contract: a value computed at execution cannot be
    # read from the prompt, so the editor peeks (iterates) instead of consuming.
    prompt = {
        "1": _node("SonderEditor"),
        "2": _node("SomeLogicNode", {"a": True}),
        "3": _node("SonderSaveVideo", {"project": ["1", 0], "mark_queue_complete": ["2", 0]}),
    }
    assert prompt_graph.execution_reaches_save_with_mark(prompt, "1", True) is False
    assert prompt_graph.execution_reaches_save_with_mark(prompt, "1", False) is True


def test_resolve_literal_follows_value_sources_only():
    prompt = {
        "1": _node("PrimitiveInt", {"value": 7}),
        "2": _node("PrimitiveInt", {"value": ["1", 0]}),
        "3": _node("SonderControl", {"value_text": "beta"}),  # retired: no longer a value source
        "4": _node("KSampler", {"seed": 1}),
        "5": _node("PrimitiveInt", {"value": ["6", 0]}),
        "6": _node("PrimitiveInt", {"value": ["5", 0]}),
    }
    resolve = prompt_graph.resolve_literal
    assert resolve(prompt, 3) == 3
    assert resolve(prompt, ["1", 0]) == 7
    assert resolve(prompt, ["2", 0]) == 7
    assert resolve(prompt, ["3", 0]) is prompt_graph.UNRESOLVED
    assert resolve(prompt, ["4", 0]) is prompt_graph.UNRESOLVED
    assert resolve(prompt, ["missing", 0]) is prompt_graph.UNRESOLVED
    assert resolve(prompt, ["5", 0]) is prompt_graph.UNRESOLVED  # a cycle ends unresolved


def test_editor_keys_and_nodes_reaching():
    prompt = {
        "1": _node("SonderEditor"),
        "733:2": _node("SonderEditor"),
        "3": _node("SonderMasksBridge", {"project": ["1", 0]}),
        "4": _node("SonderMetadataCollector", {"project": ["1", 0]}),
        "5": _node("SonderSaveVideo", {"project": ["4", 0]}),
        "6": _node("SonderSaveVideo", {"project": ["733:2", 0]}),
        "7": _node("SonderSaveBridge"),
    }
    classes = {"SonderSaveVideo", "SonderSaveBridge", "SonderMasksBridge"}
    assert prompt_graph.editor_keys(prompt) == ["1", "733:2"]
    assert prompt_graph.nodes_reaching(prompt, "1", classes) == ["3", "5"]
    assert prompt_graph.nodes_reaching(prompt, "733:2", classes) == ["6"]
    assert prompt_graph.nodes_reaching(prompt, "", classes) == []
    assert prompt_graph.editor_keys(None) == []
