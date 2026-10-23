"""Run observation through ComfyUI's real queue and executor; ComfyUI root on PYTHONPATH."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
FULL, PARTIAL, CHANGED = (
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
)


def _report():
    spec = importlib.util.find_spec("comfy_api")
    if spec is None:
        pytest.skip("real ComfyUI required; add its root to PYTHONPATH")
    comfy = Path(next(iter(spec.submodule_search_locations))).parent
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(ROOT / "tests" / "helpers" / "run_outputs_pipeline.py"), str(comfy), str(ROOT)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    line = next(line for line in result.stdout.splitlines() if line.startswith("SONDER_RUN_OUTPUTS_REPORT="))
    return json.loads(line.split("=", 1)[1])


def test_partial_runs_through_the_real_queue_and_executor():
    report = _report()
    for source in ("a", "b", "outA", "outB"):
        assert report["full"][source]["state"] == "ok"
        assert report["full"][source]["result"]["prompt_id"] == FULL
        assert report["full"][source]["result"]["fresh"] is True

    # ComfyUI really replays the cached preview of the unselected branch...
    assert report["swept_b_replayed"] is True
    # ...and the observer drops it: the partial run recorded only its own branch.
    assert report["partial_run_sources"] == ["a", "outA"]
    cached = report["partial_cached"]
    assert cached["b"]["result"]["prompt_id"] == FULL and cached["b"]["result"]["fresh"] is True
    assert cached["outB"]["result"]["prompt_id"] == FULL
    # Cache hits on the selected branch are this run's result, marked reused.
    assert cached["a"]["result"] == {"prompt_id": PARTIAL, "fresh": False, "files": ["pub1.png"]}
    assert cached["outA"]["result"] == {"prompt_id": PARTIAL, "fresh": False, "files": ["out1.png"]}

    changed = report["partial_changed"]
    assert changed["a"]["result"] == {"prompt_id": CHANGED, "fresh": True, "files": ["pub5.png"]}
    assert changed["outA"]["result"] == {"prompt_id": CHANGED, "fresh": True, "files": ["out5.png"]}
    assert changed["b"]["result"]["prompt_id"] == FULL
    assert report["foreign"] == "none"
    assert report["loops_see_runtime_graph"] == {"EndLoop": True, "LoopResult": True, "StartLoop": True}
    assert report["loop_scope"] is None and report["plain_scope"] == ["a", "outA"]
