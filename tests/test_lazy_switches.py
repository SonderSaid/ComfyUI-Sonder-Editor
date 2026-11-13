"""Tests for Sonder lazy switch node helpers."""

import importlib
import sys
import types
from decimal import Decimal
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "video_editor_testpkg"


def _import_lazy_switches():
    pytest.importorskip("comfy_api")
    if TEST_PACKAGE not in sys.modules:
        pkg = types.ModuleType(TEST_PACKAGE)
        pkg.__path__ = [str(ROOT)]
        sys.modules[TEST_PACKAGE] = pkg

    importlib.invalidate_caches()
    return importlib.import_module(f"{TEST_PACKAGE}.nodes.lazy_switches")


def test_lazy_node_mappings_and_ids():
    lazy = _import_lazy_switches()

    assert set(lazy.LAZY_NODE_CLASS_MAPPINGS) == {
        "SonderLazySwitch",
        "SonderLazyCluster",
        "SonderGate",
        "SonderLazyDebugSleep",
    }
    assert lazy.SonderLazySwitch.GET_SCHEMA().node_id == "SonderLazySwitch"
    assert lazy.SonderLazyCluster.GET_SCHEMA().node_id == "SonderLazyCluster"
    assert lazy.SonderGate.GET_SCHEMA().node_id == "SonderGate"


def test_lazy_switch_requests_only_selected_branch():
    lazy = _import_lazy_switches()

    needed = lazy.SonderLazySwitch.check_lazy_status(
        select=1,
        branches={"item0": object(), "item1": None},
    )

    assert needed == ["branches.item1"]


def test_lazy_cluster_requests_only_selected_branch_lanes():
    lazy = _import_lazy_switches()

    needed = lazy.SonderLazyCluster.check_lazy_status(
        select=1,
        branches=2,
        lanes=2,
        b0_l0=None,
        b0_l1=None,
        b1_l0=None,
        b1_l1="ready",
    )

    assert needed == ["b1_l0"]


def test_lazy_switch_validate_defers_when_select_linked():
    # A linked 'select' (e.g. driven by Sonder Selector) is None at
    # prompt-validation time on newer ComfyUI. Validation must defer, not crash.
    lazy = _import_lazy_switches()

    assert lazy.SonderLazySwitch.validate_inputs(select=None) is True


def test_lazy_switch_validate_still_checks_concrete_select():
    lazy = _import_lazy_switches()

    # Concrete, connected branch → valid.
    assert lazy.SonderLazySwitch.validate_inputs(
        select=1, branches={"item1": object()}
    ) is True
    # Concrete, unconnected selected branch → error string, not True.
    result = lazy.SonderLazySwitch.validate_inputs(
        select=0, branches={"item1": object()}
    )
    assert isinstance(result, str)


def test_lazy_cluster_validate_defers_when_control_linked():
    lazy = _import_lazy_switches()

    assert lazy.SonderLazyCluster.validate_inputs(select=None) is True
    assert lazy.SonderLazyCluster.validate_inputs(select=0, branches=None) is True
    assert lazy.SonderLazyCluster.validate_inputs(select=0, lanes=None) is True


class _TensorLike:
    """Truth-testing a real tensor raises; the gate must never ask."""

    def __bool__(self):
        raise RuntimeError("Boolean value of Tensor with more than one value is ambiguous")


@pytest.mark.parametrize("native", [False, True], ids=["bare", "native-pair"])
@pytest.mark.parametrize("value", [None, False, 0, "", [], {}, (0, "payload"), _TensorLike()])
def test_switch_hook_accepts_executor_pairs_and_bare_values(native, value):
    lazy = _import_lazy_switches()
    # Use a distinct supplied key to prove native metadata owns the request name.
    key = "native.input" if native else "branches.item1"
    entry = (value, key) if native else value
    needed = lazy.SonderLazySwitch.check_lazy_status(
        select=1, branches={"item0": (None, "branches.item0"), "item1": entry},
    )
    assert needed == ([key] if value is None else [])


@pytest.mark.parametrize("select,branches", [
    (-1, {"item0": None}), (32, {"item32": None}),
    (1, {"item0": None}), (0, {}), (0, None),
])
def test_switch_hook_does_not_request_missing_or_invalid_selection(select, branches, monkeypatch):
    lazy = _import_lazy_switches()
    monkeypatch.setattr(lazy, "MAX_BRANCHES", 32)
    assert lazy.SonderLazySwitch.check_lazy_status(select=select, branches=branches) == []


def test_switch_execute_preserves_tuple_payload_identity():
    lazy = _import_lazy_switches()
    payload = (None, "branches.item1")
    assert lazy.SonderLazySwitch.check_lazy_status(
        select=1, branches={"item1": (payload, "branches.item1")},
    ) == []
    output = lazy.SonderLazySwitch.execute(select=1, branches={"item1": payload})
    assert output.result[0] is payload


def test_gate_condition_rule():
    lazy = _import_lazy_switches()
    gate_open = lazy._gate_open

    assert gate_open() is False  # unwired
    for closed in (None, False, 0, 0.0, Decimal(0), 0j):
        assert gate_open(closed) is False, closed
    for opened in (True, 1, -1, 0.5, "", "false", "0", [], {}, {"waveform": 1}, _TensorLike()):
        assert gate_open(opened) is True, opened


def test_gate_condition_unwraps_numpy_and_tensor_scalars():
    lazy = _import_lazy_switches()
    np = pytest.importorskip("numpy")
    torch = pytest.importorskip("torch")

    for closed in (np.bool_(False), np.int64(0), np.float32(0), torch.tensor(False), torch.tensor(0)):
        assert lazy._gate_open(closed) is False, closed
    for opened in (np.bool_(True), np.int64(2), torch.tensor(1.5), torch.zeros(2, 2), np.zeros(3)):
        assert lazy._gate_open(opened) is True, opened


def test_gate_schema_declares_paired_lanes_and_outputs():
    lazy = _import_lazy_switches()
    schema = lazy.SonderGate.GET_SCHEMA()
    names = [item.id for item in schema.inputs]

    assert names[:4] == ["when_A", "value_A", "when_B", "value_B"]
    assert len(names) == 2 * lazy.MAX_GATE_LANES
    assert [item.display_name for item in schema.outputs][:3] == ["A", "B", "C"]
    assert len(schema.outputs) == lazy.MAX_GATE_LANES
    by_name = {item.id: item for item in schema.inputs}
    assert by_name["value_A"].lazy is True and by_name["value_A"].optional is True
    assert not by_name["when_A"].lazy and by_name["when_A"].optional is True


def test_gate_requests_only_open_unevaluated_lanes():
    lazy = _import_lazy_switches()
    image = _TensorLike()

    needed = lazy.SonderGate.check_lazy_status(
        when_A=image, value_A=None,        # present slot: open
        when_B=None, value_B=None,         # 'nothing' slot: closed
        when_C=1, value_C=None,            # has_reference = 1: open
        when_D=0, value_D=None,            # has_reference = 0: closed
        value_E=None,                      # unwired condition: closed
        when_F=True, value_F="ready",      # already evaluated
        when_G=True,                       # open with no value wired
    )

    assert needed == ["value_A", "value_C"]


def test_gate_execute_emits_value_or_nothing_per_lane():
    lazy = _import_lazy_switches()
    image = _TensorLike()

    result = lazy.SonderGate.execute(
        when_A=image, value_A="a",
        when_B=None, value_B="b",
        when_C=True,
        value_D="d",
    )
    outputs = result.result

    assert len(outputs) == lazy.MAX_GATE_LANES
    assert outputs[:4] == ("a", None, None, None)
    assert set(outputs[4:]) == {None}


# --- Phase 3, Part A: one lazy rule, shared with the cache pipeline -----------------------

import copy  # noqa: E402
import itertools  # noqa: E402


def _subsets(names):
    names = list(names)
    return [frozenset(combo) for size in range(len(names) + 1) for combo in itertools.combinations(names, size)]


def _router_states(lazy):
    """(class, deciding, all lazy names) over a grid of router states."""
    switch = [f"branches.item{i}" for i in range(4)]
    for select in (-1, 0, 1, 3, 5):
        yield lazy.SonderLazySwitch, {"select": select}, switch
    for select, branches, lanes in itertools.product((0, 1, 2, 4), (2, 3), (1, 2, 3)):
        cluster = [f"b{b}_l{lane}" for b in range(3) for lane in range(3)]
        yield lazy.SonderLazyCluster, {"select": select, "branches": branches, "lanes": lanes}, cluster
    gate = ["value_A", "value_B", "value_C"]
    for whens in itertools.product((True, False, 0, 2, None, "x", "absent"), repeat=2):
        deciding = {f"when_{lane}": value for lane, value in zip("AB", whens) if value != "absent"}
        yield lazy.SonderGate, deciding, gate


def test_each_router_requests_only_connected_inputs_and_decides_each_alone():
    lazy = _import_lazy_switches()
    checked = 0
    for cls, deciding, names in _router_states(lazy):
        everything = cls.requested_lazy_inputs(deciding, frozenset(names))
        assert set(everything) <= set(names), (cls.__name__, deciding)
        for connected in _subsets(names) if len(names) <= 4 else (frozenset(names[::2]), frozenset(names[1::2]),
                                                                    frozenset(names[:4]), frozenset()):
            requested = cls.requested_lazy_inputs(deciding, connected)
            # (a) only connected names; (b) each input decided from ``deciding`` alone.
            assert set(requested) <= connected, (cls.__name__, deciding, connected)
            assert requested == [name for name in everything if name in connected], (cls.__name__, deciding)
            checked += 1
    assert checked > 100


def test_check_lazy_status_is_the_shared_rule_for_every_router():
    lazy = _import_lazy_switches()
    for cls, deciding, names in _router_states(lazy):
        connected = frozenset(names[::2])
        if cls is lazy.SonderLazySwitch:
            branches = {name.split(".", 1)[1]: ((None, name) if name in connected else (object(), name))
                        for name in names}
            kwargs = {"select": deciding["select"], "branches": branches}
        else:
            kwargs = dict(deciding, **{name: (None if name in connected else object()) for name in names})
        try:
            scheduled = cls.check_lazy_status(**kwargs)
        except (ValueError, TypeError):
            continue  # the scheduler raises; the pipeline then follows nothing
        assert scheduled == cls.requested_lazy_inputs(deciding, connected), (cls.__name__, deciding)


def test_a_branch_the_pipeline_follows_is_never_one_the_sonder_app_cuts():
    lazy = _import_lazy_switches()
    run_preparation = importlib.import_module(f"{TEST_PACKAGE}.server.run_preparation")
    cut_for = {lazy.SonderLazySwitch: run_preparation._cut_switch, lazy.SonderLazyCluster: run_preparation._cut_cluster,
               lazy.SonderGate: run_preparation._cut_gate}
    compared = 0
    for cls, deciding, names in _router_states(lazy):
        inputs = dict(deciding, **{name: [f"src_{name}", 0] for name in names})
        prompt = {"r": {"class_type": cls.__name__, "inputs": inputs}}
        try:
            followed = cls.requested_lazy_inputs(deciding, frozenset(names))
        except (ValueError, TypeError):
            continue
        cut = cut_for[cls](copy.deepcopy(prompt), "r", set())
        assert not set(followed) & set(cut), (cls.__name__, deciding, followed, cut)
        compared += 1
    assert compared > 50
