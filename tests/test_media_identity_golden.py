"""Golden outputs of the Driver, Reference and Guides decodes, captured before the Phase 3 split.

Execution Cache Pipeline Phase 3 moves each identified bridge's decode behind a
recipe (``cache_recipe`` / ``decode_recipe``). Comparing the new recipe path with
the new stock path would pass even if the move changed the decode, because both
would share any new mistake. These digests were captured from the decode code as
it was before the move (commit 3a0daa4), on real media built by
``tests/helpers/media_identity_fixtures.py``, so the moved code must reproduce
them exactly, tensor byte for tensor byte.

Recapture only for a deliberate decode change, by running this file with
``SONDER_CAPTURE_MEDIA_GOLDEN=1``, and say why in the commit. The digests
depend on the ffmpeg build that encodes and decodes the fixtures; with another
build the test skips and names both versions.
"""

import importlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "fixtures" / "media_identity_golden.json"
TEST_PACKAGE = "media_identity_golden_testpkg"

sys.path.insert(0, str(ROOT / "tests" / "helpers"))


def load_modules(tmp_path, monkeypatch, package=TEST_PACKAGE):
    """The pack's modules under a stand-in ``folder_paths`` rooted at ``tmp_path``."""
    pytest.importorskip("torch")
    pytest.importorskip("cv2")
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(
        get_output_directory=lambda: str(tmp_path), get_user_directory=lambda: str(tmp_path / "user"),
        get_temp_directory=lambda: str(tmp_path / "temp")))
    pkg = types.ModuleType(package)
    pkg.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, package, pkg)
    for name in list(sys.modules):
        if name.startswith(package + "."):
            monkeypatch.delitem(sys.modules, name)
    importlib.invalidate_caches()
    load = lambda name: importlib.import_module(f"{package}.{name}")  # noqa: E731
    modules = types.SimpleNamespace(
        timeline_state=load("server.timeline_state"), project_manager=load("server.project_manager"),
        media_helpers=load("server.media_helpers"), driver=load("nodes.driver_bridge"),
        reference_core=load("nodes.reference_core"), guides=load("nodes.bridge_nodes"))
    monkeypatch.setattr(load("server.external_links"), "is_enabled", lambda: False)
    return modules


def ffmpeg_of(modules):
    path = modules.media_helpers.get_ffmpeg_path()
    if not path:
        pytest.skip("ffmpeg is required for the media identity fixtures")
    version = subprocess.run([path, "-version"], capture_output=True, text=True).stdout.splitlines()[0]
    return path, version


def test_decodes_match_the_outputs_captured_before_the_split(tmp_path, monkeypatch):
    import media_identity_fixtures as fixtures

    modules = load_modules(tmp_path, monkeypatch)
    ffmpeg, version = ffmpeg_of(modules)
    digests = {}
    for label, _family, build, _spec in fixtures.cases(modules, tmp_path, ffmpeg):
        project, run = build()
        digests[label] = fixtures.digest(run(project))
    if os.environ.get("SONDER_CAPTURE_MEDIA_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_bytes((json.dumps({"ffmpeg": version, "cases": digests}, indent=2) + "\n").encode("utf-8"))
        pytest.skip(f"captured {len(digests)} golden digests")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    if golden["ffmpeg"] != version:
        pytest.skip(f"golden digests were captured with {golden['ffmpeg']!r}; this run has {version!r}")
    assert set(digests) == set(golden["cases"])
    assert {label: value for label, value in digests.items() if value != golden["cases"][label]} == {}
    # The fixtures are distinct, so a case that silently decoded nothing would collide,
    # except where equal output is the point: no driver is one black frame either way,
    # and the frozen Driver decodes its snapshot, which is the live fixture before its edit.
    groups: dict[str, set] = {}
    for label, value in golden["cases"].items():
        groups.setdefault(value, set()).add(label)
    shared = sorted(sorted(labels) for labels in groups.values() if len(labels) > 1)
    assert shared == [["driver absent lane", "driver muted"], ["driver frozen from disk", "driver present"]]
