"""The cache pipeline's pure parts: eligibility order, closure, M1 planner, apply-last.

Real-executor behaviour (scheduler lazy semantics, cache modes, the hook) lives
in ``test_cache_pipeline_executor.py``. Here a fake runtime stands in for the
executor names the hook hands ``prepare``.
"""

import contextlib
import importlib
import os
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
        "SonderEditor", "Encode", "Sampler", "Save", "Switch", "Combo", "Wild", "Raw", "Out",
        "Relay", "Masks", "Selector", "PromptB", "ImageB", "LazyB", "Num", "Router", "AnyGate",
        "SonderSelector", "PrimitiveInt", "MediaB", "Consumer")})
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


class FakeProject:
    def __init__(self):
        self._execution_context = {"scene_id": "s1"}


class _Bridge:
    """A V1 bridge in the shipped bridges' shape: project in, provenance out."""

    FUNCTION = "execute"
    calls: list = []
    fail = None

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {name: (kind,) for name, (kind, _extra) in CONSUMERS[cls.__name__].items()}}

    @classmethod
    def _record(cls, kwargs):
        _Bridge.calls.append((cls.__name__, dict(kwargs)))
        if isinstance(cls.fail, BaseException):
            raise cls.fail


class Relay(_Bridge):
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("global_prompt", "smart_prompt")
    SONDER_CACHE_POLICY = {"global_prompt": policy.EXACT, "smart_prompt": policy.EXACT}
    SONDER_CACHE_PREPARE = policy.PREPARES(publishes=("prompt_relay",))

    def execute(self, project):
        Relay._record({"project": project})
        project._execution_context["prompt_relay"] = {"global": "relay text"}
        return ("relay text", "smart text")


class Masks(_Bridge):
    RETURN_TYPES = ("FLOAT", "MASK")
    RETURN_NAMES = ("start", "mask")
    SONDER_CACHE_POLICY = {"start": policy.EXACT, "mask": policy.INHERITS("needs latents")}
    SONDER_CACHE_PREPARE = policy.PREPARES(publishes=("masks_bridge",))

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"project": ("SONDER_PROJECT",), "edit_video": ("BOOLEAN",)},
                "optional": {"video_latent": ("LATENT",)}, "hidden": {"unique_id": "UNIQUE_ID"}}

    def execute(self, project, edit_video=True, video_latent=None, unique_id=None):
        Masks._record({"edit_video": edit_video, "unique_id": unique_id})
        return (1.5, "MASK")


class Selector(_Bridge):
    RETURN_TYPES = ("SONDER_REFERENCE_SET", "INT")
    RETURN_NAMES = ("reference_set", "has_reference")
    SONDER_CACHE_POLICY = {"reference_set": policy.HANDLE, "has_reference": policy.EXACT}
    SONDER_CACHE_PREPARE = policy.PREPARES(publishes=())

    def execute(self, project, lanes="0"):
        Selector._record({"lanes": lanes})
        return ({"lanes": lanes, "project": project}, 1)


class PromptB(_Bridge):
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("reference_prompt",)
    SONDER_CACHE_POLICY = {"reference_prompt": policy.EXACT}
    SONDER_CACHE_PREPARE = policy.PREPARES(publishes=())

    def execute(self, reference_set):
        PromptB._record({"reference_set": reference_set})
        return ("prompt for " + reference_set["lanes"],)


class ImageB(_Bridge):
    """A media bridge: takes the handle, is never prepared (no PREPARES)."""

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("r01",)
    SONDER_CACHE_POLICY = {"r01": policy.INHERITS("media")}


class LazyB(Relay):
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"project": ("SONDER_PROJECT", {"lazy": True})}}


class Router:
    """A Sonder router in miniature: ``select`` picks lazy input ``a`` (0) or ``b`` (1)."""

    SONDER_CACHE_LAZY = policy.LAZY_REQUESTS(method="requested_lazy_inputs", deciding=("select",))
    asked: list = []

    @classmethod
    def requested_lazy_inputs(cls, deciding, connected):
        Router.asked.append((dict(deciding), set(connected)))
        key = ("a", "b")[int(deciding["select"])]
        return [key, "not_connected"] if key in connected else ["not_connected"]


class AnyGate(Router):
    """A Gate in miniature: an any-type ``when`` opens lazy ``value``."""

    SONDER_CACHE_LAZY = policy.LAZY_REQUESTS(method="requested_lazy_inputs", deciding=("when",))

    @classmethod
    def requested_lazy_inputs(cls, deciding, connected):
        return ["value"] if deciding.get("when") and "value" in connected else []


class MediaB(_Bridge):
    """An identified media bridge: a recipe from the handle plus one source file."""

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("r01",)
    SONDER_CACHE_POLICY = {"r01": policy.DESCRIPTOR}
    SONDER_CACHE_INVALIDATION = {"r01": policy.IDENTITY_INVALIDATION}
    SONDER_CACHE_IDENTITY = policy.IDENTIFIES(token_input="reference_set", recipe_inputs=("unused_slots",))
    source = ""
    recipe_fault = None

    @classmethod
    def cache_recipe(cls, reference_set, unused_slots="placeholder"):
        _Bridge.calls.append(("MediaB.cache_recipe", {"lanes": reference_set["lanes"], "unused_slots": unused_slots}))
        if cls.recipe_fault is not None:
            raise cls.recipe_fault
        return {"lanes": reference_set["lanes"], "unused_slots": unused_slots, "sources": [cls.source]}

    def execute(self, reference_set, unused_slots="placeholder"):
        return ("IMAGE",)


BRIDGES = {cls.__name__: cls for cls in (Relay, Masks, Selector, PromptB, ImageB, LazyB, Router, AnyGate, MediaB)}


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
    "Num": {"value": ("FLOAT", {}), "flag": ("INT", {})},
    "Relay": {"project": ("SONDER_PROJECT", {})},
    "Masks": {"project": ("SONDER_PROJECT", {}), "edit_video": ("BOOLEAN", {}), "video_latent": ("LATENT", {})},
    "Selector": {"project": ("SONDER_PROJECT", {}), "lanes": ("STRING", {})},
    "PromptB": {"reference_set": ("SONDER_REFERENCE_SET", {})},
    "ImageB": {"reference_set": ("SONDER_REFERENCE_SET", {})},
    "LazyB": {"project": ("SONDER_PROJECT", {})},
    "Router": {"select": ("INT", {}), "a": ("*", {"lazy": True}), "b": ("*", {"lazy": True})},
    "AnyGate": {"when": ("*", {}), "value": ("*", {"lazy": True})},
    "MediaB": {"reference_set": ("SONDER_REFERENCE_SET", {}), "unused_slots": ("STRING", {})},
    "Consumer": {"image": ("IMAGE", {})},
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


def _runtime(server=None, progress=None, before=None, rearm=None, input_info=None, input_data=None,
             mappings=None, **v3):
    prompt_once = _module("nodes.prompt_once")
    prompt_once.get_executing_context = lambda: getattr(Context.current, "value", None)

    def get_input_data(inputs, class_def, unique_id, execution_list, dynprompt, extra_data):
        # ComfyUI's shape: with no execution list every link is reported missing.
        assert execution_list is None and extra_data == {}
        data, missing = {}, {}
        for name, value in inputs.items():
            if pipeline.is_link(value):
                data[name], missing[name] = (None,), True
            else:
                data[name] = [value]
        for name, kind in ((class_def.INPUT_TYPES() or {}).get("hidden") or {}).items():
            data[name] = [dynprompt.get_original_prompt() if kind == "PROMPT" else unique_id]
        return data, missing, {"hidden_inputs": {}}

    def default_input_info(dynprompt, node_id, name):
        node = dynprompt.get_node(node_id)
        spec = CONSUMERS.get(node["class_type"], {}).get(name)
        if spec is None:
            return None, None, None
        return spec[0], "required", spec[1]

    return pipeline.ExecutorRuntime(
        get_input_data=input_data or get_input_data,
        node_context=Context,
        progress_state=lambda: progress if progress is not None else Progress(),
        server=lambda: server,
        before_node_execution=before or (lambda: None),
        class_mappings=lambda: dict(BRIDGES, SonderEditor=FakeEditor, **(mappings or {})),
        input_info=input_info or default_input_info,
        interrupt_type=Interrupt,
        rearm_interrupt=rearm or (lambda: None),
        inference_mode=contextlib.nullcontext,
        **{"is_v3": lambda cls: False, **v3},
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

    return pipeline.plan_substitutions(nodes_view, {"1": pipeline.producer_for(FakeEditor, result)}, input_type_of)


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


# --- Phase 2: bridges ------------------------------------------------------------------------


def _bridge_graph(**extra):
    """Editor 1 with a project object; Save 3 per run; bridges and consumers from ``extra``."""
    graph = {
        "1": {"class_type": "SonderEditor", "inputs": {"project": "p"}},
        "3": {"class_type": "Save", "inputs": {"project": ["1", 0], "frames": ["1", 4]}},
    }
    graph.update(extra)
    return graph


def _reset_bridges():
    _Bridge.calls = []
    for cls in BRIDGES.values():
        cls.fail = None
    project = FakeProject()
    _reset_editor((project, "a prompt", 1280, 24.0, "IMAGE", True))
    return project


def _select(graph, closure=None, consumers=CONSUMERS):
    def input_type_of(node_id, name):
        spec = consumers.get(graph[node_id]["class_type"], {}).get(name)
        return None if spec is None else ((spec[0] if isinstance(spec[0], str) else None), spec[1])

    return pipeline.select_bridges(
        graph, set(graph) if closure is None else closure, "1", FakeEditor, dict(BRIDGES).get, input_type_of,
        lambda class_def: pipeline.shape_reason(_runtime(), class_def))


def test_a_prepared_bridge_runs_once_publishes_and_feeds_its_literal_consumers():
    project = _reset_bridges()
    graph = _bridge_graph(**{"4": {"class_type": "Relay", "inputs": {"project": ["1", 0]}},
                             "5": {"class_type": "Out", "inputs": {"value": ["4", 0]}}})
    dynprompt = _open(graph, roots=("3", "5"))
    record = pipeline.prepare(_runtime(), "p1", dynprompt)

    assert record["outcome"] == "prepared" and record["bridges"]["prepared"] == 1
    assert [name for name, _ in _Bridge.calls] == ["Relay"]
    assert _Bridge.calls[0][1]["project"] is project  # the Editor memo's project object
    assert project._execution_context["prompt_relay"] == {"global": "relay text"}
    assert dynprompt.node_overrides["5"]["inputs"] == {"value": "relay text"}
    assert "4" not in dynprompt.node_overrides and graph["5"]["inputs"]["value"] == ["4", 0]
    assert "bridges" in record["timings_ms"]
    # Nothing is stored for a bridge: only the Editor's call key is memoised.
    assert sdk_runs.first_result("k1", "4#0") == (False, None)


def test_bridge_selection_names_why_each_bridge_is_left_alone():
    graph = _bridge_graph(**{
        "4": {"class_type": "Relay", "inputs": {"project": ["1", 0]}},           # no consumer
        "5": {"class_type": "Relay", "inputs": {"project": ["1", 0]}},           # not certain
        "15": {"class_type": "Out", "inputs": {"value": ["5", 0]}},
        "6": {"class_type": "Masks", "inputs": {"project": ["1", 0], "edit_video": True,
                                                 "video_latent": ["90", 0]}},     # linked latent
        "16": {"class_type": "Num", "inputs": {"value": ["6", 0]}},
        "7": {"class_type": "Masks", "inputs": {"project": ["1", 0], "edit_video": ["91", 0]}},
        "17": {"class_type": "Num", "inputs": {"value": ["7", 0]}},
        "8": {"class_type": "Relay", "inputs": {"project": ["1", 1]}},           # an Editor EXACT slot
        "18": {"class_type": "Out", "inputs": {"value": ["8", 0]}},
        "9": {"class_type": "LazyB", "inputs": {"project": ["1", 0]}},           # lazy input
        "19": {"class_type": "Out", "inputs": {"value": ["9", 0]}},
        "10": {"class_type": "Selector", "inputs": {"project": ["1", 0], "lanes": ["92", 0]}},
        "11": {"class_type": "PromptB", "inputs": {"reference_set": ["10", 0]}},  # its producer fails
        "21": {"class_type": "Out", "inputs": {"value": ["11", 0]}},
        "12": {"class_type": "Selector", "inputs": {"project": ["1", 0]}},        # only the handle is used
        "13": {"class_type": "ImageB", "inputs": {"reference_set": ["12", 0]}},
        "90": {"class_type": "Wild", "inputs": {}}, "91": {"class_type": "Wild", "inputs": {}},
        "92": {"class_type": "Wild", "inputs": {}},
    })
    order, _needs, reasons = _select(graph, closure=set(graph) - {"5"})
    assert order == []
    assert reasons == {"4": "unused", "5": "not_certain", "6": "linked_input", "7": "linked_input",
                       "8": "linked_input", "9": "shape", "10": "linked_input", "11": "producer_failed",
                       "12": "unused"}


def test_a_handle_chain_is_prepared_producer_first_and_both_are_useful():
    graph = _bridge_graph(**{
        "10": {"class_type": "Selector", "inputs": {"project": ["1", 0], "lanes": "0,1"}},
        "11": {"class_type": "PromptB", "inputs": {"reference_set": ["10", 0]}},
        "21": {"class_type": "Out", "inputs": {"value": ["11", 0]}},
        "13": {"class_type": "ImageB", "inputs": {"reference_set": ["10", 0]}},
    })
    order, needs, reasons = _select(graph)
    assert order == ["10", "11"] and needs["11"] == {"10"} and reasons == {}

    _reset_bridges()
    dynprompt = _open(graph, roots=("3", "21", "13"))
    record = pipeline.prepare(_runtime(), "p1", dynprompt)
    assert [name for name, _ in _Bridge.calls] == ["Selector", "PromptB"]
    assert _Bridge.calls[1][1]["reference_set"]["lanes"] == "0,1"
    assert dynprompt.node_overrides["21"]["inputs"] == {"value": "prompt for 0,1"}
    assert "13" not in dynprompt.node_overrides  # media consumers keep the handle link
    assert record["bridges"]["prepared"] == 2


def test_a_hidden_unique_id_and_absent_optional_inputs_reach_execute_as_scheduled():
    _reset_bridges()
    graph = _bridge_graph(**{"6": {"class_type": "Masks", "inputs": {"project": ["1", 0], "edit_video": False}},
                             "16": {"class_type": "Num", "inputs": {"value": ["6", 0]}}})
    dynprompt = _open(graph, roots=("3", "16"))
    pipeline.prepare(_runtime(), "p1", dynprompt)
    assert _Bridge.calls == [("Masks", {"edit_video": False, "unique_id": "6"})]
    assert dynprompt.node_overrides["16"]["inputs"] == {"value": 1.5}


def test_a_failing_bridge_is_not_substituted_and_the_editor_plan_still_applies():
    _reset_bridges()
    Selector.fail = RuntimeError("Reference lane indices 0, 1 cannot be co-selected")
    graph = _bridge_graph(**{
        "2": {"class_type": "Encode", "inputs": {"text": ["1", 1]}},
        "10": {"class_type": "Selector", "inputs": {"project": ["1", 0]}},
        "11": {"class_type": "PromptB", "inputs": {"reference_set": ["10", 0]}},
        "20": {"class_type": "Num", "inputs": {"flag": ["10", 1]}},
        "21": {"class_type": "Out", "inputs": {"value": ["11", 0]}},
    })
    dynprompt = _open(graph, roots=("3", "2", "20", "21"))
    record = pipeline.prepare(_runtime(), "p1", dynprompt)
    assert record["bridge_reasons"] == {"bridge_failed": 1, "producer_failed": 1}
    assert set(dynprompt.node_overrides) == {"2"}  # only the Editor's consumer
    assert sdk_runs.first_failure("k1", "10#0") is None  # nothing stored for a bridge


def test_a_consumer_of_editor_and_bridge_values_gets_one_merged_override():
    _reset_bridges()
    graph = _bridge_graph(**{"4": {"class_type": "Relay", "inputs": {"project": ["1", 0]}},
                             "2": {"class_type": "Sampler", "inputs": {"positive": ["4", 1], "seed": ["1", 2]}}})
    dynprompt = _open(graph, roots=("3", "2"))
    pipeline.prepare(_runtime(), "p1", dynprompt)
    assert dynprompt.node_overrides["2"]["inputs"] == {"positive": "smart text", "seed": 1280}


def test_an_interrupt_in_the_bridge_pass_propagates_and_applies_nothing():
    _reset_bridges()
    graph = _bridge_graph(**{"2": {"class_type": "Encode", "inputs": {"text": ["1", 1]}},
                             "4": {"class_type": "Relay", "inputs": {"project": ["1", 0]}},
                             "5": {"class_type": "Out", "inputs": {"value": ["4", 0]}}})
    dynprompt = _open(graph, roots=("3", "2", "5"))

    def before():
        if getattr(Context.current, "value", None).node_id == "4":
            raise Interrupt()

    with pytest.raises(Interrupt):
        pipeline.prepare(_runtime(before=before), "p1", dynprompt)
    assert dynprompt.node_overrides == {} and _Bridge.calls == []
    assert sdk_runs.preparation("k1")["outcome"] == "interrupted"
    assert sdk_runs.first_failure("k1", "1#0") is None  # the Editor itself succeeded


def test_a_fault_in_the_bridge_pass_keeps_the_editor_plan():
    _reset_bridges()
    graph = _bridge_graph(**{"2": {"class_type": "Encode", "inputs": {"text": ["1", 1]}},
                             "4": {"class_type": "Relay", "inputs": {"project": ["1", 0]}},
                             "5": {"class_type": "Out", "inputs": {"value": ["4", 0]}}})
    dynprompt = _open(graph, roots=("3", "2", "5"))
    real = _runtime().get_input_data

    def broken(inputs, class_def, *args):
        if class_def is Relay:
            raise KeyError("pipeline bug")
        return real(inputs, class_def, *args)

    record = pipeline.prepare(_runtime(input_data=broken), "p1", dynprompt)
    assert record["outcome"] == "prepared" and record["bridge_reasons"] == {"bridge_pass_error": 1}
    assert set(dynprompt.node_overrides) == {"2"}


class NodeOutput:
    def __init__(self, *args, expand=None):
        self.args, self.expand, self.block_execution, self.ui = args, expand, None, None


class V3Selector(Selector):
    pass


def test_a_v3_bridge_goes_through_the_executors_v3_call_and_is_unwrapped():
    _reset_bridges()
    calls = []

    def call_v3(class_def, args, v3_data):
        calls.append((class_def, args, v3_data))
        return NodeOutput({"lanes": "0"}, 1)

    v3 = dict(is_v3=lambda cls: cls is V3Selector, call_v3=call_v3, own_lazy_status=lambda cls: False,
              node_output_type=NodeOutput)
    CONSUMERS["V3Selector"] = CONSUMERS["Selector"]
    run_scope.reset_for_tests({name: False for name in ("SonderEditor", "Save", "V3Selector", "Num")})
    graph = _bridge_graph(**{"10": {"class_type": "V3Selector", "inputs": {"project": ["1", 0], "lanes": "0"}},
                             "20": {"class_type": "Num", "inputs": {"flag": ["10", 1]}}})
    dynprompt = _open(graph, roots=("3", "20"))
    try:
        record = pipeline.prepare(_runtime(mappings={"V3Selector": V3Selector}, **v3), "p1", dynprompt)
    finally:
        del CONSUMERS["V3Selector"]
    assert calls and calls[0][1]["lanes"] == "0" and calls[0][2] == {"hidden_inputs": {}}
    assert dynprompt.node_overrides["20"]["inputs"] == {"flag": 1} and record["bridges"]["prepared"] == 1

    # Without the gate's V3 call, a V3 bridge is never prepared; without any way to
    # tell V3 from V1, no bridge is.
    assert pipeline.shape_reason(_runtime(is_v3=lambda cls: True), V3Selector) == "v3_call"
    assert pipeline.shape_reason(_runtime(is_v3=None), Relay) == "v3_unknown"
    # An expanding, blocking or UI-carrying NodeOutput is not a value result, nor a V1 dict.
    with pytest.raises(pipeline._BridgeSkip, match="result_shape"):
        pipeline._bridge_values(_runtime(**v3), V3Selector, NodeOutput({}, 1, expand={"n": {}}))
    with_ui = NodeOutput({}, 1)
    with_ui.ui = {"text": ["x"]}
    with pytest.raises(pipeline._BridgeSkip, match="result_shape"):
        pipeline._bridge_values(_runtime(**v3), V3Selector, with_ui)
    with pytest.raises(pipeline._BridgeSkip, match="result_shape"):
        pipeline._bridge_values(_runtime(**v3), Relay, {"ui": {}, "result": ("a", "b")})


@pytest.mark.parametrize("change, reason", [
    ("lazy_status", "lazy_status"),
    ("hidden", "hidden"),
    ("coroutine", "callable"),
    ("input_list", "input_is_list"),
    ("output_list", "output_is_list"),
])
def test_shape_rejects_what_the_pipeline_cannot_call_as_scheduled(change, reason):
    class Odd(Relay):
        pass

    if change == "lazy_status":
        Odd.check_lazy_status = lambda self, **kwargs: []
    if change == "hidden":
        Odd.INPUT_TYPES = classmethod(lambda cls: {"required": {}, "hidden": {"png": "EXTRA_PNGINFO"}})
    if change == "coroutine":
        async def execute(self, project):
            return ("a", "b")
        Odd.execute = execute
    if change == "input_list":
        Odd.INPUT_IS_LIST = True
    if change == "output_list":
        Odd.OUTPUT_IS_LIST = (False, True)
    assert pipeline.shape_reason(_runtime(), Relay) is None
    assert pipeline.shape_reason(_runtime(), Odd) == reason


# --- Phase 3, Part A: certainty through Sonder routers ------------------------------------


def _requests(router_inputs, prompt=None, lazy=("a", "b")):
    prompt = prompt or {"20": {"class_type": "Router", "inputs": router_inputs}}
    run_preparation = _module("server.run_preparation")
    return pipeline.router_requests(
        Router, router_inputs, lambda name: run_preparation.resolve_input_literal(prompt, "20", name),
        lambda name: name in lazy)


def test_a_router_is_followed_only_on_values_known_before_cache_keys():
    linked = {"a": ["5", 0], "b": ["6", 0]}
    assert _requests({"select": 1, **linked}) == ["b"]          # a literal (or a substituted value)
    assert _requests({"select": 0, **linked}) == ["a"]
    assert _requests({"select": 1, "a": ["5", 0], "b": "literal"}) == []  # nothing unevaluated to request
    primitive = {"20": {"class_type": "Router", "inputs": {"select": ["9", 0], **linked}},
                 "9": {"class_type": "PrimitiveInt", "inputs": {"value": 1}}}
    assert _requests(primitive["20"]["inputs"], primitive) == ["b"]
    selector = {"20": {"class_type": "Router", "inputs": {"select": ["9", 1], **linked}},
                "9": {"class_type": "SonderSelector", "inputs": {"choice": "two", "choice_list": "one\ntwo"}}}
    assert _requests(selector["20"]["inputs"], selector) == ["b"]
    unresolved = {"20": {"class_type": "Router", "inputs": {"select": ["9", 0], **linked}},
                  "9": {"class_type": "Wild", "inputs": {}}}
    assert _requests(unresolved["20"]["inputs"], unresolved) == []
    assert _requests({"select": 7, **linked}) == []             # the rule raises: follow nothing
    assert _requests({**linked}) == []                          # an absent input is absent; the rule raises


def test_the_closure_follows_exactly_what_a_router_requests():
    graph = {"out": {"inputs": {"v": ["sw", 0]}},
             "sw": {"class_type": "Router", "inputs": {"select": 1, "a": ["x", 0], "b": ["y", 0]}},
             "x": {"inputs": {}}, "y": {"inputs": {"up": ["z", 0]}}, "z": {"inputs": {}}}
    lazy = {("sw", "a"), ("sw", "b")}
    closure = pipeline.certain_closure(
        ["out"], graph.__getitem__, lambda node, name: (node, name) in lazy,
        lambda node: _requests(graph[node]["inputs"]) if node == "sw" else [])
    assert closure == {"out", "sw", "y", "z"}


def test_an_editor_behind_a_router_with_a_fixed_choice_is_certain():
    for select, expected in ((0, "prepared"), (1, "skipped")):
        _reset_editor()
        sdk_runs.reset_for_tests()
        graph = _graph(**{"20": {"class_type": "Router", "inputs": {"select": select, "a": ["2", 0], "b": "x"}},
                          "30": {"class_type": "Out", "inputs": {"value": ["20", 0]}}})
        del graph["3"]
        dynprompt = _open(graph, roots=("30",))
        record = pipeline.prepare(_runtime(), "p1", dynprompt)
        assert record["outcome"] == expected, record
        assert (record["reason"] == "not_certain") is (expected == "skipped")


def test_a_bridge_behind_a_router_decided_by_a_prepared_value_is_prepared_in_a_later_round():
    _reset_bridges()
    Router.asked = []
    graph = _bridge_graph(**{
        "10": {"class_type": "Selector", "inputs": {"project": ["1", 0], "lanes": "0"}},
        "20": {"class_type": "Router", "inputs": {"select": ["10", 1], "a": "off", "b": ["5", 0]}},
        "4": {"class_type": "Relay", "inputs": {"project": ["1", 0]}},
        "5": {"class_type": "Out", "inputs": {"value": ["4", 0]}},
        "30": {"class_type": "Out", "inputs": {"value": ["20", 0]}},
    })
    dynprompt = _open(graph, roots=("3", "30"))
    record = pipeline.prepare(_runtime(), "p1", dynprompt)
    # Round 1 prepares the Selector (its has_reference feeds the router's INT select);
    # round 2 knows select = 1, follows "b", and the Relay behind it becomes certain.
    assert [name for name, _ in _Bridge.calls] == ["Selector", "Relay"]
    assert dynprompt.node_overrides["20"]["inputs"]["select"] == 1
    assert dynprompt.node_overrides["5"]["inputs"] == {"value": "relay text"}
    assert record["bridges"]["prepared"] == 2 and record["lazy_followed"] == 1
    # The rule only ever saw linked lazy inputs, and its stray name was dropped.
    assert all(connected <= {"a", "b"} for _deciding, connected in Router.asked)


def test_a_wildcard_deciding_input_is_never_substituted_so_it_is_never_followed():
    _reset_bridges()
    graph = _bridge_graph(**{
        "10": {"class_type": "Selector", "inputs": {"project": ["1", 0], "lanes": "0"}},
        "21": {"class_type": "Num", "inputs": {"value": 1.0, "flag": ["10", 1]}},  # makes the Selector useful
        "20": {"class_type": "AnyGate", "inputs": {"when": ["10", 1], "value": ["5", 0]}},
        "4": {"class_type": "Relay", "inputs": {"project": ["1", 0]}},
        "5": {"class_type": "Out", "inputs": {"value": ["4", 0]}},
        "30": {"class_type": "Out", "inputs": {"value": ["20", 0]}},
    })
    dynprompt = _open(graph, roots=("3", "30", "21"))
    record = pipeline.prepare(_runtime(), "p1", dynprompt)
    assert [name for name, _ in _Bridge.calls] == ["Selector"]
    assert "20" not in dynprompt.node_overrides
    assert record["bridge_reasons"].get("not_certain") == 1 and record["lazy_followed"] == 0


# --- Phase 3, Part B: media identity --------------------------------------------------------


def _media_graph(**extra):
    graph = _bridge_graph(**{
        "10": {"class_type": "Selector", "inputs": {"project": ["1", 0], "lanes": "0"}},
        "13": {"class_type": "MediaB", "inputs": {"reference_set": ["10", 0], "unused_slots": "nothing"}},
        "23": {"class_type": "Consumer", "inputs": {"image": ["13", 0]}},
    })
    graph.update(extra)
    return graph


def _reset_media(tmp_path, content=b"media bytes"):
    project = _reset_bridges()
    MediaB.source = str(tmp_path / "member.png")
    Path(MediaB.source).write_bytes(content)
    MediaB.recipe_fault = None
    return project


def test_a_media_bridge_gets_a_registered_token_and_its_selector_is_prepared_for_it(tmp_path):
    _reset_media(tmp_path)
    graph = _media_graph()
    dynprompt = _open(graph, roots=("3", "23"))
    record = pipeline.prepare(_runtime(identity=True), "p1", dynprompt)
    # The Selector has no literal consumer: it is prepared because its handle feeds MediaB.
    assert [name for name, _ in _Bridge.calls] == ["Selector", "MediaB.cache_recipe"]
    assert _Bridge.calls[1][1] == {"lanes": "0", "unused_slots": "nothing"}
    token = dynprompt.node_overrides["13"]["inputs"]["reference_set"]
    assert token.startswith("sonder-identity:v1:") and dynprompt.node_overrides["13"]["inputs"]["unused_slots"] == "nothing"
    assert graph["13"]["inputs"]["reference_set"] == ["10", 0]  # the submitted graph is untouched
    class_type, recipe_json, snapshots = sdk_runs.identity_for("p1", token)
    assert class_type == "MediaB" and list(snapshots) == [MediaB.source]
    assert record["identities"] == {"identified": 1, "skipped": 0} and record["identity_bytes"] == 11
    assert "identity" in record["timings_ms"]
    # The same graph over the same bytes is the same token; one byte more is another.
    sdk_runs.reset_for_tests()
    _reset_media(tmp_path)
    again = _open(_media_graph(), roots=("3", "23"))
    pipeline.prepare(_runtime(identity=True), "p1", again)
    assert again.node_overrides["13"]["inputs"]["reference_set"] == token
    sdk_runs.reset_for_tests()
    _reset_media(tmp_path, b"media bytez")
    changed = _open(_media_graph(), roots=("3", "23"))
    pipeline.prepare(_runtime(identity=True), "p1", changed)
    assert changed.node_overrides["13"]["inputs"]["reference_set"] != token


def test_without_the_identity_gate_nothing_is_identified_and_the_selector_stays_unused(tmp_path):
    _reset_media(tmp_path)
    dynprompt = _open(_media_graph(), roots=("3", "23"))
    record = pipeline.prepare(_runtime(), "p1", dynprompt)
    assert _Bridge.calls == [] and dynprompt.node_overrides == {}
    assert record["identities"] == {} and record["bridge_reasons"] == {"unused": 1}


@pytest.mark.parametrize("change, reason", [
    ("lazy", "not_certain"),
    ("linked_recipe_input", "linked_input"),
    ("no_consumer", "unused"),
    ("selector_fails", "producer_failed"),
    ("recipe_raises", "recipe_failed"),
    ("missing_source", "source_missing"),
    ("not_json", "recipe_not_json"),
])
def test_a_media_bridge_without_an_identity_keeps_its_stock_path(tmp_path, change, reason):
    _reset_media(tmp_path)
    extra = {}
    roots = ("3", "23")
    if change == "lazy":
        extra = {"20": {"class_type": "Router", "inputs": {"select": ["90", 0], "a": ["23", 0], "b": "x"}},
                 "90": {"class_type": "Wild", "inputs": {}}, "30": {"class_type": "Out", "inputs": {"value": ["20", 0]}}}
        roots = ("3", "30")
    if change == "linked_recipe_input":
        extra = {"13": {"class_type": "MediaB", "inputs": {"reference_set": ["10", 0], "unused_slots": ["90", 0]}},
                 "90": {"class_type": "Wild", "inputs": {}}}
    if change == "no_consumer":
        # Certain (a root of its own) but consumed by nothing: no decode to skip.
        extra = {"23": {"class_type": "Out", "inputs": {"value": "x"}}}
        roots = ("3", "13", "23")
    graph = _media_graph(**extra)
    if change == "selector_fails":
        Selector.fail = RuntimeError("conflict")
    if change == "recipe_raises":
        MediaB.recipe_fault = RuntimeError("member gone")
    if change == "missing_source":
        os.remove(MediaB.source)
    if change == "not_json":
        MediaB.recipe_fault = None
        original = MediaB.cache_recipe
        MediaB.cache_recipe = classmethod(lambda cls, **kw: {"sources": [cls.source], "bad": (1, 2)})
    try:
        dynprompt = _open(graph, roots=roots)
        record = pipeline.prepare(_runtime(identity=True), "p1", dynprompt)
    finally:
        if change == "not_json":
            MediaB.cache_recipe = original
        Selector.fail = None
    assert "13" not in dynprompt.node_overrides
    assert record["identity_reasons"].get(reason) == 1, record
    assert record["outcome"] == "prepared"


def test_a_fault_in_the_identity_pass_keeps_every_value_substitution(tmp_path, monkeypatch):
    _reset_media(tmp_path)
    graph = _media_graph(**{"4": {"class_type": "Relay", "inputs": {"project": ["1", 0]}},
                            "5": {"class_type": "Out", "inputs": {"value": ["4", 0]}}})
    monkeypatch.setattr(pipeline, "select_identified", lambda *a, **k: 1 / 0)
    dynprompt = _open(graph, roots=("3", "23", "5"))
    record = pipeline.prepare(_runtime(identity=True), "p1", dynprompt)
    assert record["identity_reasons"] == {"identity_pass_error": 1} and record["identity_error"] == "ZeroDivisionError"
    assert dynprompt.node_overrides["5"]["inputs"] == {"value": "relay text"}
    assert "13" not in dynprompt.node_overrides


def test_an_interrupt_while_hashing_propagates_and_applies_nothing(tmp_path, monkeypatch):
    _reset_media(tmp_path)
    identity = _module("nodes.cache_identity")
    monkeypatch.setattr(identity, "CHECK_EVERY", 1)
    calls = {"n": 0}

    def before():
        calls["n"] += 1
        if calls["n"] > 3:  # past the Editor's and the bridges' checks: inside the hash
            raise Interrupt()

    dynprompt = _open(_media_graph(), roots=("3", "23"))
    with pytest.raises(Interrupt):
        pipeline.prepare(_runtime(identity=True, before=before), "p1", dynprompt)
    assert dynprompt.node_overrides == {}
