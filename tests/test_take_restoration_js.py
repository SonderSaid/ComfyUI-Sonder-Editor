"""The browser's take restoration mirror (`web/js/take_restoration.js`) against
the server: the shared fixture table, then a seeded differential sweep that
compares both sides on generated records, coercion cases included."""
import base64
import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from server.routes import _take_audio_covers
from server.take_placement import (
    calculate_take_placement, restore_take_geometry, take_restoration_source,
)
from server.timeline_state import Asset

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "tests" / "fixtures" / "take_restoration_cases.json").read_text(encoding="utf-8"))


def _run_module(calls):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    encoded = base64.b64encode((ROOT / "web" / "js" / "take_restoration.js").read_bytes()).decode("ascii")
    script = (
        'const mod = await import("data:text/javascript;base64,__MODULE__");\n'
        'const { readFileSync } = await import("node:fs");\n'
        'const calls = JSON.parse(readFileSync(0, "utf8"));\n'
        'const out = calls.map(([name, args]) => {\n'
        '  if (name === "geometry") return mod.restoreTakeGeometry(args[0], args[1], args[2]);\n'
        '  if (name === "source") return mod.takeRestorationSource(args[0], args[1]);\n'
        '  if (name === "placement") return mod.calculateTakePlacement(args[0], args[1]);\n'
        '  if (name === "covers") return mod.takeAudioCovers(args[0], args[1], args[2]);\n'
        '  throw new Error(name);\n'
        '});\n'
        'console.log(JSON.stringify(out));\n'
    ).replace("__MODULE__", encoded)
    completed = subprocess.run([node, "--input-type=module", "-e", script], input=json.dumps(calls),
                               capture_output=True, text=True, check=True)
    return json.loads(completed.stdout)


def _params(case):
    base = case["params"]
    params = json.loads(json.dumps(CASES["params"][base] if isinstance(base, str) else base))
    for key in case.get("remove", ()):
        params.pop(key, None)
    params.update(json.loads(json.dumps(case.get("override", {}))))
    return params


def _js_source(source):
    """A source as JSON carries it: the context dict and the numbers."""
    return json.loads(json.dumps(source))


def test_the_mirror_matches_the_fixture_table():
    calls = [["source", [_params(case), case["asset_type"]]] for case in CASES["sources"]]
    geometry_cases = CASES["geometry"]
    for case in geometry_cases:
        source = take_restoration_source(_params(case), "video")
        calls.append(["geometry", [_js_source(source), case["encoded"],
                                   {"sameScene": case["same_scene"], "destinationFps": case["fps"],
                                    "cursorFrame": case["cursor"]}]])
    results = _run_module(calls)
    for case, result in zip(CASES["sources"], results):
        for key, value in case["expect"].items():
            assert result.get(key) == value, (case["name"], key, result)
    keys = ("source_in_frame", "source_out_frame", "total_source_frames",
            "timeline_start_frame", "timeline_end_frame")
    for case, result in zip(geometry_cases, results[len(CASES["sources"]):]):
        if "reason" in case:
            assert result == {"reason": case["reason"]}, case["name"]
        else:
            assert [result[key] for key in keys] == case["expect"], case["name"]


_FPS = [12, 23.976, 24, 24000 / 1001, 25, 29.97, 30, 30000 / 1001, 50, 59.94, 60]
_ODD = [None, True, False, "", "17", " 8 ", "3.5", "x", 4.7, -2, 0, [], {}]


def _value(rng, low, high):
    roll = rng.random()
    if roll < 0.12:
        return rng.choice(_ODD)
    if roll < 0.2:
        return rng.randint(low, high) + rng.choice([0.25, 0.5, 0.75])
    return rng.randint(low, high)


def _record(rng):
    start = rng.randint(0, 120)
    params = {
        "take_placement_policy": rng.choice(["authored_v1"] * 8 + [None, "authored_v9"]),
        "placement_fps": rng.choice(_FPS + [0, None, "24"]),
        "scene_id": rng.choice(["s1", "s1", "s2", "", 7]),
        "selection_start": rng.choice([start] * 6 + [start + 0.5, None, str(start)]),
        "selection_end": start + rng.randint(-2, 60),
        "take_placement_mode": rng.choice(["trimmed", "trimmed", "untrimmed", None]),
        "take_keep_padding": rng.choice([True, False, "true", None]),
    }
    for key, low, high in (("actual_pre_context_frames", 0, 40), ("actual_post_context_frames", 0, 30),
                           ("requested_mask_pre_offset", 0, 12), ("requested_mask_post_offset", 0, 12),
                           ("frame_count_padding", 0, 9), ("mask_end_frame", 0, 140),
                           ("frame_count", 0, 140), ("encoded_frame_count", -1, 140)):
        if rng.random() < 0.85:
            params[key] = _value(rng, low, high)
    if rng.random() < 0.15:
        params.pop("selection_start")
        params["editor_export"] = {"scene_id": "s1", "range": {"start": start, "end": start + rng.randint(-1, 50)}}
    if params["take_placement_policy"] is None:
        params.pop("take_placement_policy")
    return params


def test_the_mirror_matches_the_server_on_generated_records():
    rng = random.Random(20261025)
    calls, expected = [], []
    for _ in range(400):
        params = _record(rng)
        asset_type = rng.choice(["video", "video", "audio"])
        encoded = rng.randint(0, 140)
        calls.append(["placement", [params, encoded]])
        expected.append(calculate_take_placement(params, encoded))
        source = take_restoration_source(params, asset_type)
        calls.append(["source", [params, asset_type]])
        expected.append(source)
        if source["status"] == "supported":
            options = {"sameScene": rng.random() < 0.5, "destinationFps": rng.choice(_FPS),
                       "cursorFrame": rng.randint(0, 300)}
            geometry = restore_take_geometry(source, encoded, same_scene=options["sameScene"],
                                             destination_fps=options["destinationFps"],
                                             cursor_frame=options["cursorFrame"])
            calls.append(["geometry", [_js_source(source), encoded, options]])
            expected.append(geometry)
            if "reason" not in geometry:
                duration = rng.choice([0, rng.random() * 8])
                audio = Asset(asset_type="audio", duration_sec=duration)
                calls.append(["covers", [duration, options["destinationFps"], geometry]])
                expected.append(_take_audio_covers(audio, geometry, options["destinationFps"]))
    results = _run_module(calls)
    supported = sum(1 for value in expected if isinstance(value, dict) and value.get("status") == "supported")
    assert supported > 100, "the sweep must exercise restorable records, not only refusals"
    mismatches = [(call, want, got) for call, want, got in zip(calls, json.loads(json.dumps(expected)), results)
                  if want != got]
    assert not mismatches, mismatches[:3]
