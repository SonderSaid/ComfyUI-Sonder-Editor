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
