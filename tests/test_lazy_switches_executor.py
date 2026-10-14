"""Real CPU scheduler checks; run with the ComfyUI root on PYTHONPATH.

The child process avoids pytest argv parsing and the repo's nodes/server module
names shadowing ComfyUI's modules. Missing ComfyUI is an explicit local skip.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_lazy_nodes_through_real_executor():
    spec = importlib.util.find_spec("comfy_api")
    if spec is None:
        pytest.skip("real ComfyUI required; add its root to PYTHONPATH")
    comfy = Path(next(iter(spec.submodule_search_locations))).parent
    env = os.environ.copy()
    # Exercise the published default contract, independent of local overrides.
    env["SONDER_LAZY_MAX_BRANCHES"] = "32"
    env["SONDER_LAZY_MAX_LANES"] = "8"
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(ROOT / "tests" / "helpers" / "lazy_switch_executor.py"),
         str(comfy), str(ROOT)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report_line = next(line for line in result.stdout.splitlines() if line.startswith("SONDER_LAZY_REPORT="))
    report = json.loads(report_line.split("=", 1)[1])
    assert len(report["cases"]) == 23, report
    assert all(case["passed"] for case in report["cases"]), report
