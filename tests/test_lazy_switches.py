"""Tests for Sonder lazy switch node helpers."""

import importlib
import sys
import types
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
        "SonderLazyDebugSleep",
    }
    assert lazy.SonderLazySwitch.GET_SCHEMA().node_id == "SonderLazySwitch"
    assert lazy.SonderLazyCluster.GET_SCHEMA().node_id == "SonderLazyCluster"


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
