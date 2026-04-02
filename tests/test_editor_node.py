"""Regression tests for the LTX Editor node execute path."""

import importlib
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "video_editor_testpkg"


def _import_editor_node(tmp_path, monkeypatch):
    pytest.importorskip("torch")
    pytest.importorskip("cv2")

    folder_paths = types.SimpleNamespace(get_output_directory=lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "folder_paths", folder_paths)

    if TEST_PACKAGE not in sys.modules:
        pkg = types.ModuleType(TEST_PACKAGE)
        pkg.__path__ = [str(ROOT)]
        sys.modules[TEST_PACKAGE] = pkg

    importlib.invalidate_caches()
    return importlib.import_module(f"{TEST_PACKAGE}.nodes.editor_node")


def test_execute_coerces_context_widgets_to_ints(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")

    class DummyScene:
        scene_id = "scene-1"
        name = "Scene 1"
        duration_frames = 20
        width = 0
        height = 0
        fps = 0
        guide_frames = []

        @staticmethod
        def get_prompt_for_range(start_frame, end_frame):
            return f"prompt:{start_frame}-{end_frame}"

    class DummyProject:
        def __init__(self):
            self.fps = 24.0
            self.resolution = (768, 512)
            self.project_dir = str(tmp_path)
            self._execution_context = None
            self._scene = DummyScene()

        def get_scene(self, scene_id):
            return self._scene if scene_id == self._scene.scene_id else None

        @staticmethod
        def get_asset(asset_id):
            return None

    project = DummyProject()
    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    monkeypatch.setattr(
        editor_node.LTXEditor,
        "_render_scene_frames",
        lambda self, proj, scene, start, end: torch.zeros(max(1, end - start), 2, 2, 3, dtype=torch.float32),
    )
    monkeypatch.setattr(
        editor_node.LTXEditor,
        "_load_scene_audio",
        lambda self, proj, scene, start, end: editor_node._make_silent_audio(1.0),
    )

    node = editor_node.LTXEditor()
    result = node.execute(
        project="Existing Project",
        project_name="Ignored",
        fps=24.0,
        width=768,
        height=512,
        scene_id="scene-1",
        selection_start="5",
        selection_end="12",
        pre_context_frames="3",
        post_context_frames="4",
    )

    assert result[5] == 14
    assert result[10] == 2
    assert result[11] == 16
    assert result[12] == 5
    assert result[13] == 12
    assert result[14] == pytest.approx(3 / 24.0)
    assert result[15] == pytest.approx(10 / 24.0)
    assert project._execution_context["pre_context_frames"] == 3
    assert project._execution_context["post_context_frames"] == 4
