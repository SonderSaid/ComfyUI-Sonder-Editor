"""Live sampling previews through ComfyUI's real preview path and KJ's installed wrapper.

Needs the test install: ComfyUI root on PYTHONPATH and KJNodes in its custom_nodes.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _comfy_root() -> Path:
    spec = importlib.util.find_spec("comfy_api")
    if spec is None:
        pytest.skip("real ComfyUI required; add its root to PYTHONPATH")
    return Path(next(iter(spec.submodule_search_locations))).parent


@pytest.fixture(scope="module")
def report():
    comfy = _comfy_root()
    kj_dir = comfy / "custom_nodes" / "comfyui-kjnodes"
    if not (kj_dir / "nodes" / "preview_override_node.py").is_file():
        pytest.skip("KJNodes is not installed in this ComfyUI")
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(ROOT / "tests" / "helpers" / "sampling_previews_pipeline.py"),
         str(comfy), str(ROOT), str(kj_dir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    line = next(line for line in result.stdout.splitlines() if line.startswith("SONDER_SAMPLING_PREVIEWS_REPORT="))
    return json.loads(line.split("=", 1)[1])


def test_native_previews_reach_the_store_through_comfyuis_own_hook(report):
    # A metadata client gets event 4, any other client event 2; both are attributed.
    assert set(report["native_metadata"]["events"]) == {4}
    assert set(report["native_plain"]["events"]) == {2}
    for name in ("native_metadata", "native_plain"):
        live = report[name]["live"]
        assert live["state"] == "live" and live["live"]["publisher"] == "native"
        assert (live["live"]["step"], live["live"]["total"]) == (6, 6)
    assert report["native_bytes"] > 0
    # With previews off ComfyUI sends nothing: the pane keeps the previous run's
    # preview, and the run says why it has none.
    off = report["native_off"]
    assert off["events"] == [] and off["live"]["state"] == "previous"
    assert off["live"]["live"]["prompt_id"] == "n2" and off["preview_method"] == "none"


def test_the_shape_gate_accepts_the_installed_kj_and_installs_once(report):
    # Fails when an installed KJNodes update changes what the adapter relies on.
    assert report["shape"] is None
    assert report["install"] == report["install_again"] == "ok" and report["marked"] is True


def test_the_real_kj_wrapper_publishes_attributed_previews(report):
    for name, mime in (("kj_image", "image/jpeg"), ("kj_video", "image/webp")):
        found = report[name]
        assert found["kj_events"] >= 2, found  # boundary 0 plus per-step payloads
        assert found["state"] == "live" and found["via"] == "s" and found["reattributed"] is False, found
        assert found["mime"] in (mime, "video/mp4"), found
        # KJ's encoder queue drops when full, so the last step is never promised.
        assert 1 <= found["step"] <= 4, found
        # Suppression is on: the sampler's own native previews never arrive, and the
        # native source says where its previews went.
        assert found["suppressed"] is True and found["native_events"] == 0, found
        # (Earlier harness runs published "s", so the pane would keep that older preview.)
        assert found["native_state"] in ("waiting", "previous") and found["suppressed_by"] == "kj", found


def test_a_held_worker_stays_with_its_own_prompt(report):
    held = report["held"]
    assert held["a_record"] == 7 and held["b_record"] is None


def test_without_the_adapter_kj_is_dropped_and_marked_degraded(report):
    assert report["bare_unattributed"] == 1 and report["bare_degraded"] == "degraded"


def test_the_wrap_stays_cheap(report):
    # Generous bounds for a slow CPU: the work is dict lookups, measured per event.
    bench = report["bench_us"]
    for name in ("bound_event2", "bound_event4", "bound_kj"):
        assert bench[name] < 500, bench
    assert bench["unbound_event2"] < 200 and bench["unrelated_event"] < 100, bench
