import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT))

from server.guide_collision import resolve_execution_window


def _run_selection_module(script_body: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    module_path = ROOT / "web" / "js" / "selection_constraints.js"
    encoded = base64.b64encode(module_path.read_bytes()).decode("ascii")
    script = (
        'const mod = await import("data:text/javascript;base64,__MODULE__");\n'
        + script_body
    ).replace("__MODULE__", encoded)
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_frontend_execution_window_matches_backend_fixtures():
    fixtures = [
        {
            "sceneDuration": 500,
            "selectionStart": 100,
            "selectionEnd": 221,
            "frameConstraint": {"step": 8, "offset": 1},
        },
        {
            "sceneDuration": 500,
            "selectionStart": 100,
            "selectionEnd": 220,
            "preContextFrames": 25,
            "frameConstraint": {"step": 8, "offset": 1},
        },
        {
            "sceneDuration": 200,
            "selectionStart": 0,
            "selectionEnd": 73,
            "preContextFrames": 25,
            "postContextFrames": 7,
            "frameConstraint": {"step": 8, "offset": 1},
        },
        {
            "sceneDuration": 180,
            "selectionStart": 7,
            "selectionEnd": 151,
            "preContextFrames": 25,
            "postContextFrames": 17,
            "maskPreOffset": 9,
            "maskPostOffset": 5,
            "frameConstraint": {"step": 10, "offset": 3, "min": 13},
        },
        {
            "sceneDuration": 80,
            "selectionStart": 5,
            "selectionEnd": 42,
            "preContextFrames": 9,
            "postContextFrames": 12,
            "maskPreOffset": 4,
            "maskPostOffset": 8,
            "frameConstraint": None,
        },
    ]
    actual = _run_selection_module(
        f"const fixtures = {json.dumps(fixtures)};\n"
        "console.log(JSON.stringify(fixtures.map((item) => mod.resolveSelectionExecutionWindow(item))));"
    )

    expected = []
    for fixture in fixtures:
        expected.append(resolve_execution_window(
            scene_duration=fixture["sceneDuration"],
            selection_start=fixture["selectionStart"],
            selection_end=fixture["selectionEnd"],
            pre_context_frames=fixture.get("preContextFrames", 0),
            post_context_frames=fixture.get("postContextFrames", 0),
            mask_pre_offset=fixture.get("maskPreOffset", 0),
            mask_post_offset=fixture.get("maskPostOffset", 0),
            frame_constraint=fixture.get("frameConstraint"),
        ))
    assert actual == expected


def test_anchor_relative_endpoint_fixtures_cover_order_context_steps_and_padding():
    cases = [
        {
            "name": "in-first-zero-context",
            "input": {"edge": "end", "anchorFrame": 100, "candidateFrame": 220, "searchDirection": 1,
                      "sceneDuration": 500, "frameConstraint": {"step": 8, "offset": 1}},
            "endpoint": 221, "padding": 0, "fallback": False,
        },
        {
            "name": "out-first-zero-context",
            "input": {"edge": "start", "anchorFrame": 220, "candidateFrame": 100, "searchDirection": -1,
                      "sceneDuration": 500, "frameConstraint": {"step": 8, "offset": 1}},
            "endpoint": 99, "padding": 0, "fallback": False,
        },
        {
            "name": "usable-pre-context",
            "input": {"edge": "end", "anchorFrame": 100, "candidateFrame": 220, "searchDirection": 1,
                      "sceneDuration": 500, "preContextFrames": 25,
                      "frameConstraint": {"step": 8, "offset": 1}},
            "endpoint": 220, "padding": 0, "fallback": False,
        },
        {
            "name": "scene-start-unavailable-pre-context",
            "input": {"edge": "end", "anchorFrame": 0, "candidateFrame": 72, "searchDirection": 1,
                      "sceneDuration": 500, "preContextFrames": 25,
                      "frameConstraint": {"step": 8, "offset": 1}},
            "endpoint": 73, "padding": 0, "fallback": False,
        },
        {
            "name": "scene-end-padding-fallback",
            "input": {"edge": "end", "anchorFrame": 100, "candidateFrame": 220, "searchDirection": 1,
                      "sceneDuration": 220, "frameConstraint": {"step": 8, "offset": 1}},
            "endpoint": 220, "padding": 1, "fallback": True,
        },
        {
            "name": "free-template",
            "input": {"edge": "end", "anchorFrame": 100, "candidateFrame": 220, "searchDirection": 1,
                      "sceneDuration": 500, "frameConstraint": None},
            "endpoint": 220, "padding": 0, "fallback": False,
        },
        {
            "name": "custom-offset",
            "input": {"edge": "end", "anchorFrame": 50, "candidateFrame": 170, "searchDirection": 1,
                      "sceneDuration": 500, "frameConstraint": {"step": 10, "offset": 3}},
            "endpoint": 173, "padding": 0, "fallback": False,
        },
        {
            "name": "step-one-hard-minimum",
            "input": {"edge": "end", "anchorFrame": 50, "candidateFrame": 52, "searchDirection": 1,
                      "sceneDuration": 500, "frameConstraint": {"step": 1, "offset": 0, "min": 5}},
            "endpoint": 55, "padding": 0, "fallback": False,
        },
        {
            "name": "stepper-next-valid",
            "input": {"edge": "end", "anchorFrame": 100, "candidateFrame": 222, "searchDirection": 1,
                      "sceneDuration": 500, "frameConstraint": {"step": 8, "offset": 1}},
            "endpoint": 229, "padding": 0, "fallback": False,
        },
        {
            "name": "stepper-previous-valid",
            "input": {"edge": "end", "anchorFrame": 100, "candidateFrame": 220, "searchDirection": -1,
                      "sceneDuration": 500, "frameConstraint": {"step": 8, "offset": 1}},
            "endpoint": 213, "padding": 0, "fallback": False,
        },
        {
            "name": "stepper-crosses-post-context-availability-seam",
            "input": {"edge": "end", "anchorFrame": 0, "candidateFrame": 3, "searchDirection": -1,
                      "sceneDuration": 9, "postContextFrames": 1,
                      "frameConstraint": {"step": 8, "offset": 1}},
            "endpoint": 1, "padding": 0, "fallback": False,
        },
        {
            "name": "reverse-custom-context-seam",
            "input": {"edge": "start", "anchorFrame": 15, "candidateFrame": 14, "searchDirection": -1,
                      "sceneDuration": 15, "preContextFrames": 9,
                      "frameConstraint": {"step": 10, "offset": 3, "min": 3}},
            "endpoint": 11, "padding": 0, "fallback": False,
        },
    ]
    actual = _run_selection_module(
        f"const cases = {json.dumps(cases)};\n"
        "console.log(JSON.stringify(cases.map(({name, input}) => {\n"
        "  const result = mod.findConstrainedSelectionEndpoint(input);\n"
        "  return {name, valid: result.valid, endpoint: result.endpoint, "
        "padding: result.window.frame_count_padding, fallback: result.used_padding_fallback};\n"
        "})));"
    )
    assert actual == [
        {
            "name": case["name"],
            "valid": True,
            "endpoint": case["endpoint"],
            "padding": case["padding"],
            "fallback": case["fallback"],
        }
        for case in cases
    ]


def test_out_at_zero_cannot_form_a_non_empty_reverse_range():
    actual = _run_selection_module(
        "const result = mod.findConstrainedSelectionEndpoint({"
        'edge: "start", anchorFrame: 0, candidateFrame: 0, searchDirection: -1, '
        "sceneDuration: 200, frameConstraint: {step: 8, offset: 1}});\n"
        "console.log(JSON.stringify({valid: result.valid, endpoint: result.endpoint}));"
    )
    assert actual == {"valid": False, "endpoint": 0}


def test_recommendation_tolerance_accepts_only_the_nearest_grid_overshoot():
    actual = _run_selection_module(
        "const check = (frameCount, step) => mod.isSelectionDurationWithinRecommendation({"
        "frameCount, fps: 24, minSec: 2, maxSec: 5, frameConstraint: {step, offset: 1}});\n"
        "console.log(JSON.stringify({"
        "wanNearest: check(121, 4), wanNext: check(125, 4), "
        "ltxNearest: check(121, 8), ltxNext: check(129, 8), belowMin: check(47, 4)"
        "}));"
    )
    assert actual == {
        "wanNearest": True,
        "wanNext": False,
        "ltxNearest": True,
        "ltxNext": False,
        "belowMin": False,
    }
