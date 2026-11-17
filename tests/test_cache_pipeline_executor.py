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

Phase 2 adds the bridges (``bridges*`` cases): generated acceptance for every
bridge ``EXACT`` output with stubbed resolvers and real ``execute`` (every
mode); real bridges on fixture projects, live and frozen, with targeted edits
and saved-take provenance against stock (classic, ram); graph shapes, safety
and a negative control that diverts publication (classic); eviction of a
still-scheduled bridge (ram).

Phase 3 adds media identity (``identity*`` cases): a Driver, a Reference Image
Bridge and a Guides Start whose media consumers hit on an identical job, a
Reference image replaced at the same path, a Driver strength and a guide
strength edit each rerunning exactly their own chain, against stock (every
mode; ``none`` asserts the bypass); a Driver behind a Sonder Cluster selected by
``has_driver``, and a muted one that is never hashed (classic); a forged token,
a source changed after hashing and a cancel while hashing (classic); a RAM
eviction that re-stages an identified bridge (ram).

Phase 4a adds Editor media carried by content (``content*`` cases): frames,
guide images and audio consumers that hit on an identical job, a clip opacity,
a guide move, a guide image and an audio volume edit each rerunning exactly
their own consumers, against stock, with no carrier in hidden ``PROMPT``,
history or the take (every mode; ``none`` asserts the bypass); a negative
control whose digest ignores the last frame (classic); a lazy switch, a
video-to-video Guides Start and a Guides loop body on Editor frames (classic);
a submitted carrier, forged tokens, a keying fault rolled back on the hazard
ordering, a carrier error and cancel reported at the Editor, and a cancel while
hashing (classic, ram).

Phase 4b defers the Editor's render and mix (``deferred*`` cases): an identical
job renders and mixes nothing, a clip edit renders only, an audio edit mixes
only, guide edits do neither, every consumer equals stock (every mode; ``none``
asserts the bypass); a project reader beside a carried consumer, a fractional
clip start (no identity) and a colour-correcting Save Video's drift record on a
hit and after a mask offset change (classic); a render raise and a cancel while
settling, a source changed after its hash, and pipeline faults that end in the
carrier's or the scheduled Editor's net (classic, ram).

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
    "classic": ["acceptance", "fail_open", "tokens", "cancel", "errors", "shapes", "lazy", "misc",
                "bridges", "bridges_real", "bridges_snapshot", "bridges_shapes", "bridges_safety", "bridges_negative",
                "identity", "identity_lazy", "identity_safety", "identity_shapes",
                "content", "content_negative", "content_shapes", "content_safety",
                "deferred", "deferred_shapes", "deferred_safety", "deferred_memory"],
    "lru": ["acceptance", "cancel", "bridges", "identity", "content", "deferred", "deferred_memory"],
    "ram": ["acceptance", "cancel", "errors", "ram", "bridges", "bridges_real", "bridges_snapshot", "bridges_ram",
            "identity", "identity_ram", "content", "content_safety", "deferred", "deferred_safety",
            "deferred_memory"],
    # RAM with main.py's default headroom (the harness default is 0), so the early release
    # frees inactive entries in these cases as it does in production.
    "ram-production": ["cancel", "errors", "content_safety", "deferred", "deferred_safety"],
    "none": ["acceptance", "bridges", "identity", "content", "deferred"],
}
# A pytest arm -> (the helper's cache mode, extra environment).
HELPER_MODE = {"ram-production": ("ram", {"SONDER_CACHE_PIPELINE_RAM_HEADROOM": "production"})}


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
        helper_mode, extra_env = HELPER_MODE.get(mode, (mode, {}))
        only = [",".join(CASES_BY_MODE[mode])] if mode in HELPER_MODE else []
        completed = subprocess.run(
            [sys.executable, str(HELPER), str(comfy_root), str(REPO_ROOT), helper_mode, str(out), *only],
            cwd=str(comfy_root), capture_output=True, text=True, timeout=1800,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **extra_env},
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
