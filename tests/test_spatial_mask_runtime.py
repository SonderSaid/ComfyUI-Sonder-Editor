"""Native spatial mask integration without model weights or diffusion."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _comfy_python(comfy):
    """The install's own interpreter, so a foreign pytest Python still runs its native checks."""
    for candidate in (comfy / "venv/Scripts/python.exe", comfy / "venv/bin/python"):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def test_spatial_mask_runtime():
    candidates = [Path(os.environ.get("SONDER_COMFYUI_ROOT", ""))]
    candidates.extend(Path(sys.executable).resolve().parents[:3])
    comfy = next((p for p in candidates if (p / "comfy_api").is_dir()
                  and (p / "execution.py").is_file()), None)
    if comfy is None:
        pytest.skip("Native spatial checks require SONDER_COMFYUI_ROOT or its Python environment")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run(
        [_comfy_python(comfy), "-B", str(ROOT / "tests/helpers/spatial_mask_runtime.py"),
         str(ROOT), str(comfy)],
        cwd=comfy, capture_output=True, text=True, timeout=180, env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # unittest exits 0 on skips; report a skipped native writer check rather than a pass.
    if "skipped=" in result.stderr:
        pytest.skip("Native checks passed with skips:\n" + result.stderr[-2000:])
