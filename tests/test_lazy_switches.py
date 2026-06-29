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
