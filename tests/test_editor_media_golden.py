"""Golden Editor outputs and side effects, captured before the Phase 4b split.

Execution Cache Pipeline Phase 4b moves the Editor's render and audio mix behind
a recipe (``nodes/editor_media.py``): resolve builds it, a materializer renders
from it. Comparing the new stock path with the new deferred path would pass even
if the move changed what the Editor produces, because both would share the
mistake. These records were captured from ``_execute_once`` as it was before the
move (commit 1900441), on the real media of ``tests/helpers/editor_media_fixtures.py``:
every output (tensors by shape, dtype and bytes), the published execution
context, the Output Views record, the run-feedback notes and the queue job's
state, for live, queued, padded, context-frame and render-cache-on runs, a run
whose audio mix fails on a claimed job and a run cancelled mid-render.

Recapture only for a deliberate change, with ``SONDER_CAPTURE_EDITOR_GOLDEN=1``,
and say why in the commit. The digests depend on the ffmpeg build; with another
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
GOLDEN = ROOT / "tests" / "fixtures" / "editor_media_golden.json"
TEST_PACKAGE = "editor_media_golden_testpkg"

sys.path.insert(0, str(ROOT / "tests" / "helpers"))


def load_modules(tmp_path, monkeypatch, package=TEST_PACKAGE):
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
        media_helpers=load("server.media_helpers"), editor_node=load("nodes.editor_node"))
    monkeypatch.setattr(load("server.external_links"), "is_enabled", lambda: False)
    return modules


def ffmpeg_of(modules):
    try:
        path = modules.media_helpers.get_ffmpeg_path()
    except RuntimeError:
        path = ""
    if not path:
        pytest.skip("ffmpeg is required for the Editor media fixtures")
    version = subprocess.run([path, "-version"], capture_output=True, text=True).stdout.splitlines()[0]
    return path, version


class FakeInterrupt(BaseException):
    pass


class InterruptAfter(types.ModuleType):
    """A ``comfy.model_management`` stand-in whose interrupt trips on the Nth render check."""

    InterruptProcessingException = FakeInterrupt

    def __init__(self, after: int):
        super().__init__("comfy.model_management")
        self.after = after
        self.calls = 0
        self.flag = False

    def processing_interrupted(self):
        self.calls += 1
        if self.calls > self.after:
            self.flag = True
        return self.flag

    def throw_exception_if_processing_interrupted(self):
        if self.flag:
            self.flag = False
            raise FakeInterrupt()


def run_all(modules, tmp_path, ffmpeg, monkeypatch):
    import editor_media_fixtures as fixtures

    records = {}
    for label, build_kwargs, execute_kwargs in fixtures.cases():
        records[label] = fixtures.summary(modules, tmp_path, ffmpeg, label, build_kwargs, execute_kwargs)
    records["failing_audio_consume"] = fixtures.summary(
        modules, tmp_path, ffmpeg, "failing_audio_consume", {"queue": "consume", "corrupt_audio": True},
        dict(render_queue_active=True, render_cache_max_bytes=0, save=True))
    with monkeypatch.context() as patch:
        patch.setattr(modules.editor_node, "_comfy_model_management", lambda: InterruptAfter(8))
        records["cancelled_render_consume"] = fixtures.summary(
            modules, tmp_path, ffmpeg, "cancelled_render_consume", {"queue": "consume"},
            dict(render_queue_active=True, render_cache_max_bytes=0, save=True))
    return records


def test_editor_matches_the_outputs_captured_before_the_split(tmp_path, monkeypatch):
    modules = load_modules(tmp_path, monkeypatch)
    ffmpeg, version = ffmpeg_of(modules)
    records = run_all(modules, tmp_path, ffmpeg, monkeypatch)
    if os.environ.get("SONDER_CAPTURE_EDITOR_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_bytes((json.dumps({"ffmpeg": version, "cases": records}, indent=1, sort_keys=True)
                            + "\n").encode("utf-8"))
        pytest.skip(f"captured {len(records)} golden Editor records")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    if golden["ffmpeg"] != version:
        pytest.skip(f"golden records were captured with {golden['ffmpeg']!r}; this run has {version!r}")
    assert sorted(records) == sorted(golden["cases"])
    for label, expected in golden["cases"].items():
        actual = json.loads(json.dumps(records[label], sort_keys=True))
        assert actual == expected, f"{label}: {first_difference(actual, expected)}"


def test_the_deferred_path_settles_to_the_same_records(tmp_path, monkeypatch):
    """Every successful case, deferred then settled from its recipe, equals the 1900441 record.

    The run-feedback notes are compared as a multiset: a deferred render notes its
    stage when it is settled, after the stages the resolve notes.
    """
    import editor_media_fixtures as fixtures

    modules = load_modules(tmp_path, monkeypatch)
    _ffmpeg, version = ffmpeg_of(modules)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    if golden["ffmpeg"] != version:
        pytest.skip(f"golden records were captured with {golden['ffmpeg']!r}; this run has {version!r}")
    for label, build_kwargs, execute_kwargs in fixtures.cases():
        actual = json.loads(json.dumps(fixtures.summary(modules, tmp_path, _ffmpeg, label, build_kwargs,
                                                        execute_kwargs, deferred=True), sort_keys=True))
        expected = dict(golden["cases"][label])
        notes, expected_notes = actual.pop("notes"), expected.pop("notes")
        assert actual == expected, f"{label}: {first_difference(actual, expected)}"
        assert sorted(map(json.dumps, notes)) == sorted(map(json.dumps, expected_notes)), label


def first_difference(actual, expected, path="$"):
    """The first path where two JSON values differ, with both sides."""
    if type(actual) is not type(expected):
        return f"{path}: {actual!r} != {expected!r}"
    if isinstance(actual, dict):
        for key in sorted(set(actual) | set(expected)):
            if key not in actual or key not in expected:
                return f"{path}.{key}: present only on one side"
            if actual[key] != expected[key]:
                return first_difference(actual[key], expected[key], f"{path}.{key}")
    if isinstance(actual, list):
        if len(actual) != len(expected):
            return f"{path}: length {len(actual)} != {len(expected)}"
        for index, (left, right) in enumerate(zip(actual, expected)):
            if left != right:
                return first_difference(left, right, f"{path}[{index}]")
    return f"{path}: {actual!r} != {expected!r}"
