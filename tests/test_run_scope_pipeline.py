"""Scoped execution through ComfyUI's real queue and executor; ComfyUI root on PYTHONPATH."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _report(cache):
    spec = importlib.util.find_spec("comfy_api")
    if spec is None:
        pytest.skip("real ComfyUI required; add its root to PYTHONPATH")
    comfy = Path(next(iter(spec.submodule_search_locations))).parent
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(ROOT / "tests" / "helpers" / "run_scope_pipeline.py"), str(comfy), str(ROOT), cache],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    line = next(line for line in result.stdout.splitlines() if line.startswith("SONDER_RUN_SCOPE_REPORT="))
    return json.loads(line.split("=", 1)[1])


@pytest.mark.parametrize("cache", ["classic", "none"])
def test_a_branch_runs_only_its_closure_and_the_editor_peeks(cache):
    report = _report(cache)
    full, branch = report["full"], report["branch"]
    # Under no cache the lazy switch re-stages ed and pass1 (the --cache-none
    # repeat the real Editor's once-per-prompt memo absorbs); this stand-in has none.
    assert set(full["executed"]) == {"after", "ed", "other", "pass1", "pass2", "pick", "picked", "preview", "save"}
    assert full["decisions"] and all(d["consume"] for d in full["decisions"])
    # Queue selected output nodes on the first-pass preview: pass two, the save
    # and the unrelated output never run, and the Editor peeks.
    assert branch["executed"] == ["ed", "pass1", "preview"]
    assert branch["decisions"] == [{"consume": False, "scope": ["ed", "pass1", "preview"]}]
    # A branch that needs the save runs it, and the Editor consumes.
    after = report["after_save"]
    assert after["executed"] == ["after", "ed", "pass1", "pass2", "save"]
    assert [d["consume"] for d in after["decisions"]] == [True]
    # An unrelated output never touches the Editor (the classic cache may even reuse it).
    unrelated = report["unrelated"]
    assert set(unrelated["executed"]) <= {"other"} and unrelated["decisions"] == []
    # A lazy switch: the static scope over-approximates (pass two is in it), but
    # only the chosen input runs, and with no save in scope the Editor peeks.
    lazy = report["lazy"]
    assert "pass2" not in lazy["executed"] and {"pick", "picked"} <= set(lazy["executed"])
    assert lazy["decisions"] == [{"consume": False, "scope": ["ed", "pass1", "pass2", "pick", "picked"]}]
    # A cancel between ComfyUI's get and its interrupt-flag reset still stops the run.
    cancelled = report["cancelled"]
    assert cancelled["cancel"] == "interrupted" and cancelled["success"] is False
    assert cancelled["executed"] == [] and "execution_interrupted" in cancelled["messages"]
