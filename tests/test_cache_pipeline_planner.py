"""The cache pipeline's pure parts: eligibility order, closure, M1 planner, apply-last.

Real-executor behaviour (scheduler lazy semantics, cache modes, the hook) lives
in ``test_cache_pipeline_executor.py``. Here a fake runtime stands in for the
executor names the hook hands ``prepare``.
"""

import contextlib
import importlib
import sys
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "sonder_cache_pipeline_test_pkg"


def _module(name):
    if TEST_PACKAGE not in sys.modules:
        package = types.ModuleType(TEST_PACKAGE)
        package.__path__ = [str(ROOT)]
        sys.modules[TEST_PACKAGE] = package
    return importlib.import_module(f"{TEST_PACKAGE}.{name}")


pipeline = _module("server.cache_pipeline")
sdk_runs = _module("server.sdk_runs")
run_scope = _module("server.run_scope")
policy = _module("nodes.cache_policy")


@pytest.fixture(autouse=True)
def _clean():
    sdk_runs.reset_for_tests()
    pipeline.reset_for_tests()
    run_scope.reset_for_tests({name: False for name in (
        "SonderEditor", "Encode", "Sampler", "Save", "Switch", "Combo", "Wild", "Raw", "Out")})
    yield
    sdk_runs.reset_for_tests()
    run_scope.reset_for_tests()


class Interrupt(BaseException):
    pass


class FakeEditor:
    RETURN_TYPES = ("SONDER_PROJECT", "STRING", "INT", "FLOAT", "IMAGE", "BOOLEAN")
    RETURN_NAMES = ("project", "prompt", "width", "fps", "frames", "flag")
    SONDER_CACHE_POLICY = {"project": policy.PER_RUN, "prompt": policy.EXACT, "width": policy.EXACT,
                           "fps": policy.EXACT, "frames": policy.INHERITS("media"), "flag": policy.EXACT}
    hidden = {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"}
    outcome = ("P", "a prompt", 1280, 24.0, "IMAGE", True)
    calls = []

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"project": (["p"],)}, "hidden": dict(cls.hidden)}

    def execute(self, **kwargs):
        # The real Editor's shape: the body runs through the per-prompt memo.
        def body():
            FakeEditor.calls.append(kwargs)
            if isinstance(FakeEditor.outcome, BaseException):
                raise FakeEditor.outcome
            return FakeEditor.outcome

        return _module("nodes.prompt_once").run_once_per_prompt(
            kwargs.get("prompt"), kwargs.get("unique_id"), "FakeEditor", body)


class FakeDynPrompt:
    def __init__(self, prompt):
        self.original_prompt = prompt
        self.node_overrides = {}

    def get_original_prompt(self):
        return self.original_prompt

    def get_node(self, node_id):
        if node_id in self.node_overrides:
            return self.node_overrides[node_id]
        return self.original_prompt[node_id]

    def override_node(self, node_id, node):
        self.node_overrides[node_id] = node

    def get_display_node_id(self, node_id):
        return node_id


# Consumer class -> {input: (type, extra)}
CONSUMERS = {
    "Encode": {"text": ("STRING", {}), "width": ("INT", {}), "fps": ("FLOAT", {}), "flag": ("BOOLEAN", {}),
               "project": ("SONDER_PROJECT", {})},
    "Sampler": {"positive": ("STRING", {}), "seed": ("INT", {})},
    "Save": {"project": ("SONDER_PROJECT", {}), "frames": ("IMAGE", {})},
    "Switch": {"select": ("INT", {}), "a": ("STRING", {"lazy": True}), "b": ("STRING", {"lazy": True})},
    "Combo": {"choice": (["a prompt", "b"], {})},
    "Wild": {"value": ("*", {})},
    "Raw": {"text": ("STRING", {"rawLink": True})},
    "Out": {"value": ("STRING", {})},
}


class Server:
    def __init__(self, client_id="client"):
        self.client_id = client_id
        self.last_node_id = None
        self.sent = []

    def send_sync(self, event, data, sid):
        self.sent.append((event, dict(data), sid))


class Progress:
    def __init__(self):
        self.events = []

    def start_progress(self, node_id):
        self.events.append(("start", node_id))

    def finish_progress(self, node_id):
        self.events.append(("finish", node_id))


class Context:
    current = threading.local()

    def __init__(self, prompt_id, node_id, index):
        self.value = types.SimpleNamespace(prompt_id=prompt_id, node_id=node_id, list_index=index)

    def __enter__(self):
        Context.current.value = self.value
        return self

    def __exit__(self, *exc):
        Context.current.value = None


def _runtime(server=None, progress=None, before=None, rearm=None, input_info=None):
    prompt_once = _module("nodes.prompt_once")
    prompt_once.get_executing_context = lambda: getattr(Context.current, "value", None)

    def get_input_data(inputs, class_def, unique_id, execution_list, dynprompt, extra_data):
        assert execution_list is None and extra_data == {}
        data = {name: [value] for name, value in inputs.items()}
        data["prompt"] = [dynprompt.get_original_prompt() if dynprompt is not None else {}]
        data["unique_id"] = [unique_id]
        return data, {}, {}

    def default_input_info(dynprompt, node_id, name):
        node = dynprompt.get_node(node_id)
        spec = CONSUMERS.get(node["class_type"], {}).get(name)
        if spec is None:
            return None, None, None
        return spec[0], "required", spec[1]

    return pipeline.ExecutorRuntime(
        get_input_data=get_input_data,
        node_context=Context,
        progress_state=lambda: progress if progress is not None else Progress(),
        server=lambda: server,
        before_node_execution=before or (lambda: None),
        class_mappings=lambda: {"SonderEditor": FakeEditor},
        input_info=input_info or default_input_info,
        interrupt_type=Interrupt,
        rearm_interrupt=rearm or (lambda: None),
        inference_mode=contextlib.nullcontext,
    )


def _graph(**extra):
    graph = {
        "1": {"class_type": "SonderEditor", "inputs": {"project": "p"}, "_meta": {"title": "Editor"}},
        "2": {"class_type": "Encode", "inputs": {"text": ["1", 1], "width": ["1", 2], "fps": ["1", 3],
                                                  "flag": ["1", 5], "project": ["1", 0]},
              "_meta": {"title": "Encode"}},
        "3": {"class_type": "Save", "inputs": {"project": ["1", 0], "frames": ["1", 4]}},
    }
    graph.update(extra)
    return graph


def _open(graph, roots=("3",), key="k1", prompt_id="p1"):
    sdk_runs.open_release(key, prompt_id, graph, roots=list(roots))
    return FakeDynPrompt(graph)


def _reset_editor(outcome=("P", "a prompt", 1280, 24.0, "IMAGE", True)):
    FakeEditor.outcome = outcome
    FakeEditor.calls = []
    FakeEditor.hidden = {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"}


# --- Eligibility ---------------------------------------------------------------------


def test_cheap_exits_fail_open_in_cheapest_first_order():
    two = _graph(**{"9": {"class_type": "SonderEditor", "inputs": {"project": ["2", 0]}}})
    two["2"]["is_changed"] = [1]
    with pytest.raises(pipeline._Skip, match="editor_count"):
        pipeline._cheap_exits(two)
    with pytest.raises(pipeline._Skip, match="no_editor"):
        pipeline._cheap_exits({"2": {"class_type": "Encode", "inputs": {}}})
    flagged = _graph()
    flagged["1"]["inputs"]["project"] = ["2", 0]
    flagged["3"]["is_changed"] = [1]
    with pytest.raises(pipeline._Skip, match="is_changed"):
        pipeline._cheap_exits(flagged)
    del flagged["3"]["is_changed"]
    with pytest.raises(pipeline._Skip, match="linked_editor_input"):
        pipeline._cheap_exits(flagged)
    with pytest.raises(pipeline._Skip, match="untrusted"):
        pipeline._cheap_exits(_graph())
    graph = _graph()
    sdk_runs.open_release("k", "p", graph)  # no roots
    with pytest.raises(pipeline._Skip, match="roots_unknown"):
        pipeline._cheap_exits(graph)


@pytest.mark.parametrize("change, reason", [
    ("scope", "scope_unknown"),
    ("lazy", "not_certain"),
    ("hidden", "editor_hidden"),
])
def test_prepare_fails_open_before_running_the_editor(change, reason):
    _reset_editor()
    graph = _graph()
    roots = ("3",)
    if change == "scope":
        graph["9"] = {"class_type": "Looper", "inputs": {}}
        run_scope.reset_for_tests({"SonderEditor": False, "Encode": False, "Save": False, "Looper": True})
    if change == "lazy":
        graph = _graph(**{"4": {"class_type": "Switch", "inputs": {"select": 0, "a": ["1", 1], "b": "x"}},
                          "5": {"class_type": "Out", "inputs": {"value": ["4", 0]}}})
        del graph["3"]
        roots = ("5",)
    if change == "hidden":
        FakeEditor.hidden = {"prompt": "PROMPT", "unique_id": "UNIQUE_ID", "extra": "EXTRA_PNGINFO"}
    dynprompt = _open(graph, roots)
    record = pipeline.prepare(_runtime(), "p1", dynprompt)
    assert (record["outcome"], record["reason"]) == ("skipped", reason)
    assert FakeEditor.calls == [] and dynprompt.node_overrides == {}
    assert sdk_runs.preparation("k1")["reason"] == reason


def test_closure_follows_non_lazy_links_only():
    graph = {"out": {"inputs": {"v": ["sw", 0]}}, "sw": {"inputs": {"select": ["sel", 0], "a": ["ed", 0]}},
             "sel": {"inputs": {}}, "ed": {"inputs": {}}}
    lazy = {("sw", "a")}
    closure = pipeline.certain_closure(["out"], graph.__getitem__, lambda node, name: (node, name) in lazy)
    assert closure == {"out", "sw", "sel"}
    assert "ed" in pipeline.certain_closure(["out"], graph.__getitem__, lambda node, name: False)


# --- The M1 planner -------------------------------------------------------------------


def _plan(nodes_view, result, consumers=CONSUMERS):
    def input_type_of(node_id, name):
        spec = consumers.get(nodes_view[node_id]["class_type"], {}).get(name)
        if spec is None:
            return None
        return (spec[0] if isinstance(spec[0], str) else None), spec[1]

    return pipeline.plan_substitutions(
        nodes_view, "1", result,
        lambda slot: policy.slot_policy(FakeEditor, slot),
        lambda slot: FakeEditor.RETURN_TYPES[slot],
        input_type_of,
    )


@pytest.mark.parametrize("slot, value, substituted, reason", [
    (1, "text", True, None),
    (2, 1280, True, None),
    (2, True, False, "value_type"),          # bool on INT
    (3, 24, False, "value_type"),            # int on FLOAT
    (3, float("nan"), False, "non_finite"),
    (3, float("inf"), False, "non_finite"),
    (2, None, False, "value_type"),
    (5, True, True, None),
    (5, 1, False, "value_type"),
])
def test_only_exact_python_types_become_literals(slot, value, substituted, reason):
    result = list(FakeEditor.outcome)
    result[slot] = value
    name = {1: "text", 2: "width", 3: "fps", 5: "flag"}[slot]
    nodes_view = {"2": {"class_type": "Encode", "inputs": {name: ["1", slot]}}}
    overrides, skips = _plan(nodes_view, tuple(result))
    if substituted:
        assert overrides["2"]["inputs"][name] == value and type(overrides["2"]["inputs"][name]) is type(value)
        assert skips == []
    else:
        assert overrides == {} and [item["reason"] for item in skips] == [reason]


def test_numpy_scalars_are_never_literals():
    np = pytest.importorskip("numpy")
    result = list(FakeEditor.outcome)
    result[2], result[3] = np.int64(1280), np.float64(24.0)
    nodes_view = {"2": {"class_type": "Encode", "inputs": {"width": ["1", 2], "fps": ["1", 3]}}}
    overrides, skips = _plan(nodes_view, tuple(result))
    assert overrides == {} and {item["reason"] for item in skips} == {"value_type"}


def test_raw_link_wildcard_combo_unknown_and_non_exact_slots_are_skipped():
    nodes_view = {
        "4": {"class_type": "Raw", "inputs": {"text": ["1", 1]}},
        "5": {"class_type": "Wild", "inputs": {"value": ["1", 1]}},
        "6": {"class_type": "Combo", "inputs": {"choice": ["1", 1]}},
        "7": {"class_type": "Unknown", "inputs": {"text": ["1", 1]}},
        "3": {"class_type": "Save", "inputs": {"project": ["1", 0], "frames": ["1", 4]}},
        "8": {"class_type": "Encode", "inputs": {"text": ["1", 99]}},
    }
    overrides, skips = _plan(nodes_view, FakeEditor.outcome)
    assert overrides == {}
    assert {item["node"]: item["reason"] for item in skips} == {
        "4": "raw_link", "5": "consumer_type", "6": "consumer_type", "7": "consumer_unknown", "8": "slot"}


def test_a_wildcard_type_that_compares_equal_to_everything_is_still_skipped():
    # ComfyUI's IO.ANY is a str enum whose != always answers False.
    class AnyType(str):
        def __ne__(self, other):
            return False

        def __eq__(self, other):
            return True

        __hash__ = str.__hash__

    consumers = dict(CONSUMERS, Wild={"value": (AnyType("*"), {})})
    overrides, skips = _plan({"5": {"class_type": "Wild", "inputs": {"value": ["1", 1]}}}, FakeEditor.outcome,
                             consumers)
    assert overrides == {} and [item["reason"] for item in skips] == ["consumer_type"]


def test_substitutions_merge_into_one_copy_of_the_current_node():
    current = {"class_type": "Encode", "_meta": {"title": "Encode"},
               "inputs": {"text": ["1", 1], "width": ["1", 2], "project": ["1", 0], "fps": 7.5}}
    overrides, _ = _plan({"2": current}, FakeEditor.outcome)
    node = overrides["2"]
    assert node is not current and node["inputs"] is not current["inputs"]
    assert node["inputs"] == {"text": "a prompt", "width": 1280, "project": ["1", 0], "fps": 7.5}
    assert node["_meta"] == {"title": "Encode"} and node["_meta"] is not current["_meta"]
    assert current["inputs"]["text"] == ["1", 1]  # the submitted node is untouched


def test_a_float_link_slot_resolves_like_comfyui():
    overrides, skips = _plan({"2": {"class_type": "Encode", "inputs": {"text": ["1", 1.0]}}}, FakeEditor.outcome)
    assert overrides["2"]["inputs"]["text"] == "a prompt" and skips == []
    _, skips = _plan({"2": {"class_type": "Encode", "inputs": {"text": ["1", 1.5]}}}, FakeEditor.outcome)
    assert [item["reason"] for item in skips] == ["slot"]


# --- prepare end to end with a fake runtime -----------------------------------------------


def test_prepare_runs_the_editor_once_and_overrides_only_literal_consumers():
    _reset_editor()
    graph = _graph()
    dynprompt = _open(graph, roots=("2", "3"))
    server, progress = Server(), Progress()
    record = pipeline.prepare(_runtime(server=server, progress=progress), "p1", dynprompt)

    assert record["outcome"] == "prepared" and record["substituted"] == 1
    assert len(FakeEditor.calls) == 1 and FakeEditor.calls[0]["prompt"] is graph
    assert dynprompt.node_overrides["2"]["inputs"] == {
        "text": "a prompt", "width": 1280, "fps": 24.0, "flag": True, "project": ["1", 0]}
    assert "3" not in dynprompt.node_overrides and graph["2"]["inputs"]["text"] == ["1", 1]
    assert progress.events == [("start", "1"), ("finish", "1")]
    assert server.sent == [("executing", {"node": "1", "display_node": "1", "prompt_id": "p1"}, "client")]
    assert server.last_node_id == "1"
    # The memo holds the result under the scheduled call's key.
    assert sdk_runs.first_result("k1", "1#0") == (True, FakeEditor.outcome)
    assert sdk_runs.preparation("k1")["outcome"] == "prepared"
    assert set(record["timings_ms"]) >= {"eligibility", "closure", "editor", "plan", "total"}


def test_without_a_client_id_nothing_is_broadcast():
    _reset_editor()
    dynprompt = _open(_graph())
    server = Server(client_id=None)
    pipeline.prepare(_runtime(server=server), "p1", dynprompt)
    assert server.sent == [] and server.last_node_id is None


def test_an_editor_error_is_stored_for_replay_and_nothing_is_substituted():
    error = RuntimeError("render failed")
    _reset_editor(error)
    dynprompt = _open(_graph())
    progress = Progress()
    record = pipeline.prepare(_runtime(progress=progress), "p1", dynprompt)
    assert record["outcome"] == "editor_failed"
    assert sdk_runs.first_failure("k1", "1#0") is error
    assert dynprompt.node_overrides == {}
    assert progress.events == [("start", "1"), ("finish", "1")]


def test_an_editor_interrupt_stores_a_cancellation_and_is_rearmed():
    _reset_editor(Interrupt())
    rearmed = []
    dynprompt = _open(_graph())
    record = pipeline.prepare(_runtime(rearm=lambda: rearmed.append(True)), "p1", dynprompt)
    assert record["outcome"] == "cancelled" and rearmed == [True]
    assert sdk_runs.first_failure("k1", "1#0") is sdk_runs.CANCELLED
    assert dynprompt.node_overrides == {}


def test_a_fault_in_pipeline_code_stores_nothing_and_propagates_to_the_guard():
    _reset_editor()
    dynprompt = _open(_graph())

    def broken_before():
        raise ValueError("pipeline bug")

    progress = Progress()
    with pytest.raises(ValueError):
        pipeline.prepare(_runtime(before=broken_before, progress=progress), "p1", dynprompt)
    assert FakeEditor.calls == [] and dynprompt.node_overrides == {}
    assert sdk_runs.first_failure("k1", "1#0") is None
    assert sdk_runs.preparation("k1")["outcome"] == "pipeline_error"
    assert progress.events == [("start", "1"), ("finish", "1")]

    # An interrupt outside the Editor run (a cancel before it) propagates too.
    def interrupted():
        raise Interrupt()

    with pytest.raises(Interrupt):
        pipeline.prepare(_runtime(before=interrupted), "p1", dynprompt)
    assert sdk_runs.first_failure("k1", "1#0") is None
    assert sdk_runs.preparation("k1")["outcome"] == "interrupted"


def test_a_planning_fault_leaves_no_partial_overrides():
    _reset_editor()
    graph = _graph(**{"4": {"class_type": "Sampler", "inputs": {"positive": ["1", 1], "seed": threading.Lock()}}})
    dynprompt = _open(graph, roots=("2", "3", "4"))
    with pytest.raises(TypeError):  # deep-copying node 4 fails after node 2 was planned
        pipeline.prepare(_runtime(), "p1", dynprompt)
    assert dynprompt.node_overrides == {}
    assert len(FakeEditor.calls) == 1  # the memo still serves the scheduled Editor


def test_an_existing_override_is_the_base_of_the_merge():
    _reset_editor()
    graph = _graph()
    dynprompt = _open(graph, roots=("2", "3"))
    earlier = {"class_type": "Encode", "_meta": {"title": "Encode", "sonder": "earlier"},
               "inputs": dict(graph["2"]["inputs"], fps=12.0)}
    dynprompt.override_node("2", earlier)
    pipeline.prepare(_runtime(), "p1", dynprompt)
    node = dynprompt.node_overrides["2"]
    assert node["_meta"]["sonder"] == "earlier" and node["inputs"]["fps"] == 12.0
    assert node["inputs"]["text"] == "a prompt"


def test_an_ineligibility_reason_is_logged_once_per_process(caplog):
    graph = _graph()
    caplog.set_level("INFO", logger=pipeline.logger.name)
    for _ in range(3):
        pipeline.prepare(_runtime(), "p1", FakeDynPrompt(graph))  # untrusted
    messages = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert len(messages) == 1 and "untrusted" in messages[0]
    pipeline.prepare(_runtime(), "p1", FakeDynPrompt({"2": {"class_type": "Encode", "inputs": {}}}))
    assert len([r for r in caplog.records if r.levelname == "INFO"]) == 1  # no_editor is quiet
