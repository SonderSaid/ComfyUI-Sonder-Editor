"""Execution preparation through ComfyUI's real validator and executor (CPU).

Run with the ComfyUI root on PYTHONPATH (the test install's venv finds it).
The child process keeps pytest's argv and the repo's nodes/server package
names away from ComfyUI's modules. Missing ComfyUI is an explicit local skip.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_prepared_prompts_through_real_validation_and_execution():
    spec = importlib.util.find_spec("comfy_api")
    if spec is None:
        pytest.skip("real ComfyUI required; add its root to PYTHONPATH")
    comfy = Path(next(iter(spec.submodule_search_locations))).parent
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(ROOT / "tests" / "helpers" / "run_preparation_executor.py"),
         str(comfy), str(ROOT)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    line = next((l for l in result.stdout.splitlines() if l.startswith("SONDER_PREP_REPORT=")), None)
    assert line, result.stdout[-4000:] + result.stderr[-4000:]
    report = json.loads(line.split("=", 1)[1])
    failed = [case for case in report["cases"] if not case["passed"]]
    assert not failed, json.dumps(failed, indent=1)
    assert len(report["cases"]) == 11
    assert result.returncode == 0, result.stderr[-4000:]
