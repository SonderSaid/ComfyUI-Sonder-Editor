"""Tests for small Sonder selector utility nodes."""

import importlib
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "video_editor_testpkg"


def _import_selector():
    if TEST_PACKAGE not in sys.modules:
        pkg = types.ModuleType(TEST_PACKAGE)
        pkg.__path__ = [str(ROOT)]
        sys.modules[TEST_PACKAGE] = pkg

    importlib.invalidate_caches()
    return importlib.import_module(f"{TEST_PACKAGE}.nodes.selector")


def test_sonder_selector_outputs_selected_string_and_index():
    selector = _import_selector()

    value, index = selector.SonderSelector().execute(
        "Depth",
        "Canny\nDepth\nReference",
    )

    assert value == "Depth"
    assert index == 1


def test_sonder_selector_trims_blank_lines():
    selector = _import_selector()

    value, index = selector.SonderSelector().execute(
        "Reference",
        "\n  Canny  \n\nReference\n",
    )

    assert value == "Reference"
    assert index == 1


@pytest.mark.parametrize(
    ("choice", "choice_list", "message"),
    [
        ("", "", "at least one"),
        ("Canny", "Canny\nCanny", "duplicate"),
        ("Depth", "Canny\nReference", "not in the choice list"),
    ],
)
def test_sonder_selector_validation_errors(choice, choice_list, message):
    selector = _import_selector()

    result = selector.SonderSelector.VALIDATE_INPUTS(choice=choice, choice_list=choice_list)

    assert isinstance(result, str)
    assert message in result
