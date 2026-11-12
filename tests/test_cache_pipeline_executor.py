"""Execution cache pipeline against ComfyUI's real executor, in every cache mode (CPU).

``tests/helpers/cache_pipeline_executor.py`` runs in a subprocess from the
ComfyUI root, once per mode, with the real Editor, Save Video, lazy nodes,
Guides Bridge and native consumers, and records a row per check. Covered per
mode (the helper's ``BY_MODE``):

- classic: generated acceptance and the differential oracle for every EXACT
  slot, fail-open reasons, SDK tokens, a mid-render cancel, pipeline faults and
  Editor errors, graph shapes, real lazy nodes, no client id, a swallowed
  interrupt, inference mode, provenance, a non-Sonder prompt;
- lru: acceptance and cancel; ram: acceptance, cancel, errors, eviction;
- none: acceptance, which asserts the pipeline is bypassed.

Skips without a ComfyUI install (``SONDER_COMFYUI_ROOT`` or the test install's venv).
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "tests" / "helpers" / "cache_pipeline_executor.py"
CASES_BY_MODE = {
    "classic": ["acceptance", "fail_open", "tokens", "cancel", "errors", "shapes", "lazy", "misc"],
    "lru": ["acceptance", "cancel"],
    "ram": ["acceptance", "cancel", "errors", "ram"],
    "none": ["acceptance"],
}


def _comfy_root() -> Path | None:
    candidates = []
    if os.environ.get("SONDER_COMFYUI_ROOT"):
        candidates.append(Path(os.environ["SONDER_COMFYUI_ROOT"]))
    candidates.extend(Path(sys.executable).resolve().parents[:3])
    for root in candidates:
        if (root / "execution.py").is_file() and (root / "comfy_api").is_dir():
            return root
    return None


_RESULTS: dict[str, dict] = {}


def _run(mode: str) -> dict:
    if mode in _RESULTS:
        return _RESULTS[mode]
    comfy_root = _comfy_root()
    if comfy_root is None:
        pytest.skip("needs a ComfyUI install (SONDER_COMFYUI_ROOT or the test install's venv)")
    with tempfile.TemporaryDirectory(prefix="sonder-cache-pipeline-result-") as folder:
        out = Path(folder) / f"{mode}.json"
        completed = subprocess.run(
            [sys.executable, str(HELPER), str(comfy_root), str(REPO_ROOT), mode, str(out)],
            cwd=str(comfy_root), capture_output=True, text=True, timeout=900,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        assert out.is_file(), (completed.stdout[-2000:], completed.stderr[-4000:])
        _RESULTS[mode] = json.loads(out.read_text(encoding="utf-8"))
    return _RESULTS[mode]


@pytest.mark.parametrize("mode, case", [(mode, case) for mode, cases in CASES_BY_MODE.items() for case in cases])
def test_cache_pipeline_on_the_real_executor(mode, case):
    result = _run(mode)
    rows = [row for row in result["checks"] if row["case"] == case]
    assert rows, f"{mode}/{case} recorded no checks"
    failed = [{"check": row["check"], "detail": row["detail"]} for row in rows if not row["ok"]]
    assert failed == []
