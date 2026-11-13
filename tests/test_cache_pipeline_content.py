"""The cache pipeline's content pass for Editor media (``CONTENT``): planner, apply, rollback, run record.

Pure parts with a fake runtime; the real executor, every cache mode and the hook's
keying live in ``test_cache_pipeline_executor.py``.
"""

import contextlib
import copy
import importlib
import sys
import threading
import types
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")

ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "sonder_cache_content_pipeline_pkg"


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
content = _module("nodes.cache_content")

FRAMES = torch.full((3, 4, 4, 3), 0.25)
GUIDES = torch.full((1, 4, 4, 3), 0.5)
AUDIO = {"waveform": torch.zeros((1, 2, 64)), "sample_rate": 48000}


class FakeEditor:
    RETURN_TYPES = ("SONDER_PROJECT", "IMAGE", "IMAGE", "STRING", "AUDIO")
    RETURN_NAMES = ("project", "rendered_frames", "guide_images", "prompt", "audio")
    SONDER_CACHE_POLICY = {"project": policy.PER_RUN, "rendered_frames": policy.CONTENT,
                           "guide_images": policy.CONTENT, "prompt": policy.EXACT, "audio": policy.CONTENT}
    outcome = None
    runs = 0

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"project": ("STRING", {})}, "hidden": {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"}}

    def execute(self, project, prompt=None, unique_id=None):
        prompt_once = _module("nodes.prompt_once")
        FakeEditor.runs += 1
        return prompt_once.run_once_per_prompt(prompt, unique_id, "SonderEditor", lambda: FakeEditor.outcome)


CONSUMERS = {
    "Encode": {"pixels": ("IMAGE", {}), "vae": ("VAE", {})},
    "Mix": {"frames": ("IMAGE", {}), "audio": ("AUDIO", {}), "text": ("STRING", {})},
    "Saver": {"project": ("SONDER_PROJECT", {}), "frames": ("IMAGE", {})},
    "Raw": {"pixels": ("IMAGE", {"rawLink": True})},
    "Loader": {},
    "Out": {"value": ("*", {})},
}


@pytest.fixture(autouse=True)
def _clean():
    sdk_runs.reset_for_tests()
    pipeline.reset_for_tests()
    run_scope.reset_for_tests({name: False for name in ("SonderEditor", "SonderMediaCarrier", *CONSUMERS)})
    FakeEditor.outcome = ("P", FRAMES, GUIDES, "a prompt", AUDIO)
    FakeEditor.runs = 0
    yield
    sdk_runs.reset_for_tests()
    run_scope.reset_for_tests()


class FakeDynPrompt:
    """ComfyUI's DynamicPrompt, as the pipeline and the attribution use it."""

    def __init__(self, prompt):
        self.original_prompt = prompt
        self.node_overrides = {}
        self.ephemeral_prompt = {}
        self.ephemeral_parents = {}
        self.ephemeral_display = {}

    def get_original_prompt(self):
        return self.original_prompt

    def get_node(self, node_id):
        if node_id in self.node_overrides:
            return self.node_overrides[node_id]
        if node_id in self.ephemeral_prompt:
            return self.ephemeral_prompt[node_id]
        return self.original_prompt[node_id]

    def has_node(self, node_id):
        return node_id in self.original_prompt or node_id in self.ephemeral_prompt

    def override_node(self, node_id, node):
        assert self.has_node(node_id)
        self.node_overrides[node_id] = node

    def get_real_node_id(self, node_id):
        while node_id in self.ephemeral_parents:
            node_id = self.ephemeral_parents[node_id]
        return node_id

    def get_display_node_id(self, node_id):
        while node_id in self.ephemeral_display:
            node_id = self.ephemeral_display[node_id]
        return node_id


class Context:
    current = threading.local()

    def __init__(self, prompt_id, node_id, index):
        self.value = types.SimpleNamespace(prompt_id=prompt_id, node_id=node_id, list_index=index)

    def __enter__(self):
        Context.current.value = self.value
        return self

    def __exit__(self, *exc):
        Context.current.value = None


class Interrupt(BaseException):
    pass


class Progress:
    def start_progress(self, node_id):
        pass

    def finish_progress(self, node_id):
        pass


def _runtime(before=None):
    prompt_once = _module("nodes.prompt_once")
    prompt_once.get_executing_context = lambda: getattr(Context.current, "value", None)

    def get_input_data(inputs, class_def, unique_id, execution_list, dynprompt, extra_data):
        data, missing = {}, {}
        for name, value in inputs.items():
            if pipeline.is_link(value):
                data[name], missing[name] = (None,), True
            else:
                data[name] = [value]
        for name, kind in ((class_def.INPUT_TYPES() or {}).get("hidden") or {}).items():
            data[name] = [dynprompt.get_original_prompt() if kind == "PROMPT" else unique_id]
        return data, missing, {}

    def input_info(dynprompt, node_id, name):
        spec = CONSUMERS.get(dynprompt.get_node(node_id)["class_type"], {}).get(name)
        return (spec[0], "required", spec[1]) if spec else (None, None, None)

    return pipeline.ExecutorRuntime(
        get_input_data=get_input_data, node_context=Context, progress_state=Progress, server=lambda: None,
        before_node_execution=before or (lambda: None), class_mappings=lambda: {"SonderEditor": FakeEditor},
        input_info=input_info, interrupt_type=Interrupt, rearm_interrupt=lambda: None,
        inference_mode=contextlib.nullcontext, is_v3=lambda cls: False)


def _graph():
    return {
        "1": {"class_type": "SonderEditor", "inputs": {"project": "p"}},
        "enc": {"class_type": "Encode", "inputs": {"pixels": ["1", 1], "vae": ["ld", 0]}},
        "ld": {"class_type": "Loader", "inputs": {}},
        "mix": {"class_type": "Mix", "inputs": {"frames": ["1", 1], "audio": ["1", 4], "text": ["1", 3]}},
        "save": {"class_type": "Saver", "inputs": {"project": ["1", 0], "frames": ["1", 2]}},
        "out": {"class_type": "Out", "inputs": {"value": ["enc", 0]}},
        "out2": {"class_type": "Out", "inputs": {"value": ["mix", 0]}},
    }


ROOTS = ("out", "out2", "save")


class Host:
    def __init__(self, reason="", fail=False):
        self.reason = reason
        self.plans = []
        self.fail = fail

    def arm(self, plan):
        self.plans.append(plan)
        if self.fail:
            raise RuntimeError("arming failed")

    def media(self):
        return pipeline.MediaHost(reason=self.reason, arm=self.arm)


def _prepare(graph=None, host=None, before=None, key="k1", prompt_id="p1"):
    graph = graph or _graph()
    sdk_runs.open_release(key, prompt_id, graph, roots=list(ROOTS))
    dynprompt = FakeDynPrompt(graph)
    host = host or Host()
    record = pipeline.prepare(_runtime(before), prompt_id, dynprompt, host.media())
    return record, dynprompt, host


def _input_type_of(nodes_view):
    def input_type_of(node_id, name):
        spec = CONSUMERS.get(nodes_view[node_id]["class_type"], {}).get(name)
        return (spec[0], spec[1]) if spec else None
    return input_type_of


# --- plan_carriers (pure) ----------------------------------------------------------------


def test_the_dry_run_finds_slots_that_can_gain_and_skips_the_rest():
    graph = _graph()
    graph["raw"] = {"class_type": "Raw", "inputs": {"pixels": ["1", 1]}}
    graph["bad"] = {"class_type": "Encode", "inputs": {"pixels": ["1", 9], "vae": ["ld", 0]}}
    graph["unknown"] = {"class_type": "Nope", "inputs": {"x": ["1", 2]}}
    before = copy.deepcopy(graph)
    carriers, merged, restore, gaining, skips = pipeline.plan_carriers(
        graph, {}, "1", FakeEditor, None, _input_type_of(graph))
    assert (carriers, merged, restore) == ({}, {}, {})
    # enc gains frames; mix takes the Editor's prompt by link, so it cannot gain;
    # save takes project; guide_images (slot 2) only feeds save.
    assert gaining == {1}
    assert sorted((item["node"], item["reason"]) for item in skips) == [
        ("bad", "slot"), ("mix", "no_gain"), ("mix", "no_gain"), ("raw", "raw_link"),
        ("save", "no_gain"), ("unknown", "consumer_unknown")]
    assert graph == before


def test_a_consumer_whose_editor_values_were_substituted_gains_and_merges_with_its_override():
    graph = _graph()
    m1 = copy.deepcopy(graph["mix"])
    m1["inputs"]["text"] = "a prompt"  # the Phase 1 literal
    overrides = {"mix": m1}
    before = (copy.deepcopy(graph), copy.deepcopy(overrides))
    tokens = {1: "sonder-media:v1:aa", 4: "sonder-media:v1:bb"}
    carriers, merged, restore, gaining, _skips = pipeline.plan_carriers(
        graph, overrides, "1", FakeEditor, tokens, _input_type_of(graph))
    assert gaining == {1, 4}
    assert carriers == {
        "sonder-media:1:1": {"class_type": "SonderMediaCarrier", "inputs": {"token": "sonder-media:v1:aa"}},
        "sonder-media:1:4": {"class_type": "SonderMediaCarrier", "inputs": {"token": "sonder-media:v1:bb"}}}
    assert merged["mix"]["inputs"] == {"frames": ["sonder-media:1:1", 0], "audio": ["sonder-media:1:4", 0],
                                       "text": "a prompt"}
    assert merged["enc"]["inputs"] == {"pixels": ["sonder-media:1:1", 0], "vae": ["ld", 0]}
    # The restore map is exactly the view without carriers.
    assert restore == {"mix": m1, "enc": None}
    assert (graph, overrides) == before


def test_a_consumer_with_a_slot_left_unhashed_is_left_alone():
    graph = _graph()
    m1 = copy.deepcopy(graph["mix"])
    m1["inputs"]["text"] = "a prompt"
    carriers, merged, restore, _gaining, skips = pipeline.plan_carriers(
        graph, {"mix": m1}, "1", FakeEditor, {1: "sonder-media:v1:aa"}, _input_type_of(graph))
    assert set(merged) == {"enc"} and set(carriers) == {"sonder-media:1:1"}
    assert {(item["node"], item["reason"]) for item in skips} >= {("mix", "no_digest")}


# --- The content pass through prepare -----------------------------------------------------


def test_prepare_carries_each_media_output_with_a_gaining_consumer():
    graph = _graph()
    graph["mix"]["inputs"]["text"] = "literal"  # no other Editor link: mix gains too
    record, dynprompt, host = _prepare(graph)
    assert record["outcome"] == "prepared"
    assert record["content"] == {"carried": 2, "skipped": 1}  # save: no_gain
    assert record["content_reasons"] == {"no_gain": 1}
    assert record["content_bytes"] == FRAMES.numel() * 4 + AUDIO["waveform"].numel() * 4
    assert set(dynprompt.ephemeral_prompt) == {"sonder-media:1:1", "sonder-media:1:4"}
    assert dynprompt.ephemeral_display == {"sonder-media:1:1": "1", "sonder-media:1:4": "1"}
    assert dynprompt.get_node("enc")["inputs"]["pixels"] == ["sonder-media:1:1", 0]
    assert dynprompt.get_node("save")["inputs"] == graph["save"]["inputs"]  # left alone
    # The attribution: an error at a carrier is reported at the Editor.
    assert dynprompt.get_real_node_id("sonder-media:1:1") == "1"
    assert dynprompt.get_real_node_id("enc") == "enc"
    # The submitted prompt never holds a carrier.
    assert set(graph) == set(_graph())
    plan = host.plans[0]
    assert plan.carrier_ids == ["sonder-media:1:1", "sonder-media:1:4"]
    assert plan.payloads["sonder-media:1:1"] is FRAMES and plan.payloads["sonder-media:1:4"] is AUDIO
    # The run record resolves each token to the memo's own object, for this prompt only.
    token = dynprompt.ephemeral_prompt["sonder-media:1:1"]["inputs"]["token"]
    assert content.is_token(token)
    found, value = sdk_runs.media_for("p1", token)
    assert found and value is FRAMES
    assert sdk_runs.media_for("p2", token) == (False, None)
    assert sdk_runs.media_for("p1", content.TOKEN_PREFIX + "0" * 64) == (False, None)
    assert FakeEditor.runs == 1


def test_equal_content_gives_one_token_and_a_change_gives_another():
    tokens = []
    for frames in (FRAMES, FRAMES.clone(), FRAMES + 1):
        sdk_runs.reset_for_tests()
        FakeEditor.outcome = ("P", frames, GUIDES, "a prompt", AUDIO)
        _record, dynprompt, _host = _prepare()
        tokens.append(dynprompt.ephemeral_prompt["sonder-media:1:1"]["inputs"]["token"])
    assert tokens[0] == tokens[1] != tokens[2]


@pytest.mark.parametrize("reason", ["cache_type", "carrier_gate", "set_prompt_armed", "carrier_class_missing"])
def test_a_failed_precondition_plans_no_carrier_and_keeps_the_editor_values(reason):
    record, dynprompt, host = _prepare(host=Host(reason=reason))
    assert record["outcome"] == "prepared" and record["content_reasons"] == {reason: 1}
    assert dynprompt.ephemeral_prompt == {} and host.plans == []
    assert dynprompt.get_node("mix")["inputs"]["text"] == "a prompt"  # the M1 literal still applies
    assert dynprompt.get_node("enc")["inputs"]["pixels"] == ["1", 1]


def test_a_prompt_that_holds_a_carrier_gets_none():
    graph = _graph()
    graph["c"] = {"class_type": "SonderMediaCarrier", "inputs": {"token": "x"}}
    record, dynprompt, _host = _prepare(graph)
    assert record["content_reasons"] == {"carrier_in_prompt": 1} and dynprompt.ephemeral_prompt == {}
    graph = _graph()
    graph["sonder-media:1:1"] = {"class_type": "Loader", "inputs": {}}
    sdk_runs.reset_for_tests()
    record, dynprompt, _host = _prepare(graph)
    assert record["content_reasons"].get("carrier_id_taken") == 1
    assert "sonder-media:1:1" not in dynprompt.ephemeral_prompt


def test_unsupported_media_gets_no_carrier():
    FakeEditor.outcome = ("P", "not a tensor", GUIDES, "a prompt", AUDIO)
    record, dynprompt, _host = _prepare()
    assert record["content_reasons"].get("no_digest") == 1 and dynprompt.ephemeral_prompt == {}


def test_a_content_pass_fault_keeps_every_value_substitution(monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("planner bug")

    monkeypatch.setattr(pipeline, "plan_carriers", broken)
    record, dynprompt, host = _prepare()
    assert record["outcome"] == "prepared" and record["content_reasons"] == {"content_pass_error": 1}
    assert dynprompt.get_node("mix")["inputs"]["text"] == "a prompt"
    assert dynprompt.ephemeral_prompt == {} and host.plans == []


def test_an_interrupt_while_hashing_propagates_and_nothing_is_applied():
    def cancel():
        raise Interrupt()

    with pytest.raises(Interrupt):
        _prepare(before=cancel)
    # (The Editor's own early run calls the check first; the hook re-arms either way.)


def test_an_interrupt_inside_the_digest_propagates(monkeypatch):
    real = content.digest

    def cancelling(socket, value, check=None):
        return real(socket, value, lambda: (_ for _ in ()).throw(Interrupt()))

    monkeypatch.setattr(content, "digest", cancelling)
    graph = _graph()
    sdk_runs.open_release("k1", "p1", graph, roots=list(ROOTS))
    dynprompt = FakeDynPrompt(graph)
    with pytest.raises(Interrupt):
        pipeline.prepare(_runtime(), "p1", dynprompt, Host().media())
    assert dynprompt.node_overrides == {} and dynprompt.ephemeral_prompt == {}


def test_a_failed_arm_rolls_every_carrier_back_and_keeps_the_values():
    graph = _graph()
    graph["mix"]["inputs"]["text"] = "literal"
    record, dynprompt, host = _prepare(graph, host=Host(fail=True))
    assert len(host.plans) == 1 and record["content_reasons"]["carrier_apply_error"] == 1
    assert record["content"]["carried"] == 0
    assert dynprompt.ephemeral_prompt == {} and dynprompt.ephemeral_display == {}
    assert "get_real_node_id" not in vars(dynprompt)
    assert dynprompt.get_node("enc")["inputs"]["pixels"] == ["1", 1]
    assert "enc" not in dynprompt.node_overrides


def test_rollback_restores_the_exact_view_without_carriers():
    record, dynprompt, host = _prepare()
    plan = host.plans[0]
    pipeline.rollback_carriers(dynprompt, plan)
    assert dynprompt.ephemeral_prompt == {} and dynprompt.ephemeral_display == {}
    assert "get_real_node_id" not in vars(dynprompt)
    # mix had a Phase 1 override (its text literal); enc had none.
    assert dynprompt.node_overrides["mix"]["inputs"] == {"frames": ["1", 1], "audio": ["1", 4], "text": "a prompt"}
    assert "enc" not in dynprompt.node_overrides


def test_rollback_restores_another_packs_attribution():
    graph = _graph()
    sdk_runs.open_release("k1", "p1", graph, roots=list(ROOTS))
    dynprompt = FakeDynPrompt(graph)
    theirs = lambda node_id: f"theirs:{node_id}"  # noqa: E731
    dynprompt.get_real_node_id = theirs
    host = Host()
    pipeline.prepare(_runtime(), "p1", dynprompt, host.media())
    assert dynprompt.get_real_node_id("sonder-media:1:1") == "1"
    assert dynprompt.get_real_node_id("enc") == "theirs:enc"
    pipeline.rollback_carriers(dynprompt, host.plans[0])
    assert vars(dynprompt)["get_real_node_id"] is theirs


# --- The run record --------------------------------------------------------------------------


def test_register_media_follows_the_identity_rules():
    graph = {"1": {"class_type": "SonderEditor", "inputs": {}}}
    assert sdk_runs.register_media("k1", "p1", "t", "1#0", 1) == "untrusted"
    sdk_runs.open_release("k1", "p1", graph)
    assert sdk_runs.register_media("k1", "p1", "t", "1#0", 1) == "no_result"
    sdk_runs.remember_result("k1", "1#0", ("P", FRAMES))
    assert sdk_runs.register_media("k1", "p1", "t", "1#0", 2) == "no_result"
    assert sdk_runs.register_media("k1", "p1", "t", "1#0", True) == "no_result"
    assert sdk_runs.register_media("k1", "other", "t", "1#0", 1) == "prompt_id_ambiguous"
    assert sdk_runs.register_media("k1", "p1", "t", "1#0", 1) == ""
    assert sdk_runs.media_for("p1", "t") == (True, FRAMES)
    for index in range(sdk_runs.MEDIA_LIMIT - 1):
        assert sdk_runs.register_media("k1", "p1", f"t{index}", "1#0", 1) == ""
    assert sdk_runs.register_media("k1", "p1", "overflow", "1#0", 1) == "registry_full"
    # Two open runs sharing a prompt id: neither resolves.
    sdk_runs.open_release("k2", "p1", {"1": {"class_type": "SonderEditor", "inputs": {}}})
    assert sdk_runs.media_for("p1", "t") == (False, None)
    sdk_runs.close_release("k2")
    assert sdk_runs.media_for("p1", "t") == (True, FRAMES)
    closed = sdk_runs.close_release("k1")
    assert "media" not in closed and sdk_runs.media_for("p1", "t") == (False, None)
    assert "media" not in (sdk_runs.record_snapshot("k1") or {})
