"""Regression tests for the Sonder Editor node execute path."""

import importlib
import os
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "video_editor_testpkg"


def _prompt_node(class_type, inputs=None):
    return {
        "class_type": class_type,
        "inputs": inputs or {},
    }


def _prompt_graph(nodes):
    return {str(node_id): data for node_id, data in nodes.items()}


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


def _import_io_nodes(tmp_path, monkeypatch):
    pytest.importorskip("torch")
    pytest.importorskip("cv2")

    temp_dir = tmp_path / "temp"
    input_dir = tmp_path / "input"
    temp_dir.mkdir(exist_ok=True)
    input_dir.mkdir(exist_ok=True)
    folder_paths = types.SimpleNamespace(
        get_output_directory=lambda: str(tmp_path),
        get_temp_directory=lambda: str(temp_dir),
        get_input_directory=lambda: str(input_dir),
        filter_files_content_types=lambda files, content_types: files,
    )
    monkeypatch.setitem(sys.modules, "folder_paths", folder_paths)

    if TEST_PACKAGE not in sys.modules:
        pkg = types.ModuleType(TEST_PACKAGE)
        pkg.__path__ = [str(ROOT)]
        sys.modules[TEST_PACKAGE] = pkg

    importlib.invalidate_caches()
    return importlib.import_module(f"{TEST_PACKAGE}.nodes.io_nodes")


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
        editor_node.SonderEditor,
        "_render_scene_frames",
        lambda self, proj, scene, start, end: torch.zeros(max(1, end - start), 2, 2, 3, dtype=torch.float32),
    )
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_load_scene_audio",
        lambda self, proj, scene, start, end: editor_node._make_silent_audio(1.0),
    )

    node = editor_node.SonderEditor()
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


def test_execution_reaches_terminal_save_only_for_linked_editor(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)

    prompt = _prompt_graph({
        "1": _prompt_node("SonderEditor"),
        "2": _prompt_node("SonderEditor"),
        "3": _prompt_node("SonderSaveVideo", {"project": ["2", 0], "mark_queue_complete": True}),
    })

    node = editor_node.SonderEditor()

    assert node._execution_reaches_terminal_save(prompt, "1") is False
    assert node._execution_reaches_terminal_save(prompt, "2") is True


def test_execution_reaches_terminal_save_requires_toggle(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)

    prompt = _prompt_graph({
        "1": _prompt_node("SonderEditor"),
        "3": _prompt_node("SonderSaveVideo", {"project": ["1", 0], "mark_queue_complete": False}),
    })

    node = editor_node.SonderEditor()

    assert node._execution_reaches_terminal_save(prompt, "1") is False


def test_execution_reaches_terminal_bridge_save(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)

    prompt = _prompt_graph({
        "1": _prompt_node("SonderEditor"),
        "3": _prompt_node("SonderSaveBridge", {"project": ["1", 0], "mark_queue_complete": True}),
    })

    node = editor_node.SonderEditor()

    assert node._execution_reaches_terminal_save(prompt, "1") is True


def test_execute_skips_pending_queue_job_without_downstream_save_video(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")

    class DummyScene:
        scene_id = "scene-1"
        name = "Scene 1"
        duration_frames = 40
        width = 0
        height = 0
        fps = 0
        guide_frames = []

        @staticmethod
        def get_prompt_for_range(start_frame, end_frame):
            return f"live:{start_frame}-{end_frame}"

    queue_job = types.SimpleNamespace(
        job_id="job-1",
        scene_id="scene-1",
        selection_start=10,
        selection_end=30,
        pre_context_frames=4,
        post_context_frames=6,
        context_frames=0,
        prompt="queued prompt",
        status="pending",
        error="",
        progress=0.0,
        params={"snapshot_version": 1},
        guide_frame_snapshots=[],
        scene_width=0,
        scene_height=0,
        scene_fps=0.0,
    )

    class DummyProject:
        def __init__(self):
            self.fps = 24.0
            self.resolution = (768, 512)
            self.project_dir = str(tmp_path)
            self._execution_context = None
            self._scene = DummyScene()
            self.generation_queue = [queue_job]

        def get_scene(self, scene_id):
            return self._scene if scene_id == self._scene.scene_id else None

        @staticmethod
        def get_asset(asset_id):
            return None

    project = DummyProject()
    save_calls = []

    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    monkeypatch.setattr(editor_node, "save_project", lambda proj: save_calls.append("saved"))
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_render_scene_frames",
        lambda self, proj, scene, start, end: torch.zeros(max(1, end - start), 2, 2, 3, dtype=torch.float32),
    )
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_load_scene_audio",
        lambda self, proj, scene, start, end: editor_node._make_silent_audio(1.0),
    )

    node = editor_node.SonderEditor()
    result = node.execute(
        project="Existing Project",
        project_name="Ignored",
        fps=24.0,
        width=768,
        height=512,
        prompt=_prompt_graph({
            "editor-1": _prompt_node("SonderEditor"),
        }),
        unique_id="editor-1",
        scene_id="scene-1",
        selection_start=2,
        selection_end=8,
        pre_context_frames=1,
        post_context_frames=2,
    )

    assert save_calls == []
    assert queue_job.status == "pending"
    assert result[4] == "live:1-10"
    assert result[5] == 9
    assert result[10] == 1
    assert result[11] == 10
    assert result[12] == 2
    assert result[13] == 8
    assert project._execution_context["queue_job_id"] == ""


def test_execute_consumes_pending_queue_job_snapshot(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")

    guide_dir = tmp_path / "media"
    guide_dir.mkdir(exist_ok=True)
    (guide_dir / "guide.png").write_bytes(b"guide")

    class DummyScene:
        scene_id = "scene-1"
        name = "Scene 1"
        duration_frames = 40
        width = 6
        height = 4
        fps = 24.0
        guide_frames = []

        @staticmethod
        def get_prompt_for_range(start_frame, end_frame):
            return f"live:{start_frame}-{end_frame}"

    queue_job = types.SimpleNamespace(
        job_id="job-1",
        scene_id="scene-1",
        selection_start=10,
        selection_end=30,
        pre_context_frames=4,
        post_context_frames=6,
        context_frames=0,
        prompt="queued prompt",
        status="pending",
        error="",
        progress=0.0,
        params={"snapshot_version": 1},
        guide_frame_snapshots=[
            {"frame_index": 20, "asset_id": "guide-1", "source": "asset", "strength": 1.0},
        ],
        scene_width=8,
        scene_height=6,
        scene_fps=30.0,
    )

    class DummyProject:
        def __init__(self):
            self.fps = 24.0
            self.resolution = (7, 5)
            self.project_dir = str(tmp_path)
            self._execution_context = None
            self._scene = DummyScene()
            self.generation_queue = [queue_job]

        def get_scene(self, scene_id):
            return self._scene if scene_id == self._scene.scene_id else None

        @staticmethod
        def get_asset(asset_id):
            if asset_id != "guide-1":
                return None
            return types.SimpleNamespace(asset_id=asset_id, asset_type="image", path=os.path.join("media", "guide.png"))

    project = DummyProject()
    saved_statuses = []

    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    monkeypatch.setattr(editor_node, "save_project", lambda proj: saved_statuses.append(project.generation_queue[0].status))
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_render_scene_frames",
        lambda self, proj, scene, start, end: torch.zeros(
            max(1, end - start),
            scene.height or proj.resolution[1],
            scene.width or proj.resolution[0],
            3,
            dtype=torch.float32,
        ),
    )
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_load_scene_audio",
        lambda self, proj, scene, start, end: editor_node._make_silent_audio(1.0),
    )
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_load_guide_image",
        lambda self, path, asset_type, target_w, target_h: torch.ones(target_h, target_w, 3, dtype=torch.float32),
    )

    node = editor_node.SonderEditor()
    result = node.execute(
        project="Existing Project",
        project_name="Ignored",
        fps=24.0,
        width=768,
        height=512,
        prompt=_prompt_graph({
            "editor-1": _prompt_node("SonderEditor"),
            "save-1": _prompt_node("SonderSaveVideo", {"project": ["editor-1", 0], "mark_queue_complete": True}),
        }),
        unique_id="editor-1",
        scene_id="widget-scene",
        selection_start=1,
        selection_end=2,
        pre_context_frames=0,
        post_context_frames=0,
    )

    assert saved_statuses == ["running"]
    assert project.generation_queue[0].status == "running"
    assert result[3] == "14"
    assert result[4] == "queued prompt"
    assert result[5] == 30
    assert result[6] == 30.0
    assert result[7] == 8
    assert result[8] == 6
    assert result[10] == 6
    assert result[11] == 36
    assert result[12] == 10
    assert result[13] == 30
    assert project._execution_context["queue_job_id"] == "job-1"


def test_bridge_terminal_consumes_queue_job(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")

    class DummyScene:
        scene_id = "scene-1"
        name = "Scene 1"
        duration_frames = 24
        width = 0
        height = 0
        fps = 0
        guide_frames = []

        @staticmethod
        def get_prompt_for_range(start_frame, end_frame):
            return f"live:{start_frame}-{end_frame}"

    queue_job = types.SimpleNamespace(
        job_id="job-1",
        scene_id="scene-1",
        selection_start=4,
        selection_end=12,
        pre_context_frames=0,
        post_context_frames=0,
        context_frames=0,
        prompt="queued prompt",
        status="pending",
        error="",
        progress=0.0,
        params={"snapshot_version": 1},
        guide_frame_snapshots=[],
        scene_width=0,
        scene_height=0,
        scene_fps=0.0,
    )

    class DummyProject:
        def __init__(self):
            self.fps = 24.0
            self.resolution = (768, 512)
            self.project_dir = str(tmp_path)
            self._execution_context = None
            self._scene = DummyScene()
            self.generation_queue = [queue_job]

        def get_scene(self, scene_id):
            return self._scene if scene_id == self._scene.scene_id else None

        @staticmethod
        def get_asset(asset_id):
            return None

    project = DummyProject()
    save_calls = []

    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    monkeypatch.setattr(editor_node, "save_project", lambda proj: save_calls.append(project.generation_queue[0].status))
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_render_scene_frames",
        lambda self, proj, scene, start, end: torch.zeros(max(1, end - start), 2, 2, 3, dtype=torch.float32),
    )
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_load_scene_audio",
        lambda self, proj, scene, start, end: editor_node._make_silent_audio(1.0),
    )

    node = editor_node.SonderEditor()
    result = node.execute(
        project="Existing Project",
        project_name="Ignored",
        fps=24.0,
        width=768,
        height=512,
        prompt=_prompt_graph({
            "editor-1": _prompt_node("SonderEditor"),
            "bridge-1": _prompt_node("SonderSaveBridge", {"project": ["editor-1", 0], "mark_queue_complete": True}),
        }),
        unique_id="editor-1",
    )

    assert save_calls == ["running"]
    assert queue_job.status == "running"
    assert result[4] == "queued prompt"
    assert project._execution_context["queue_job_id"] == "job-1"


def test_execute_marks_missing_queued_scene_failed(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)

    queue_job = types.SimpleNamespace(
        job_id="job-1",
        scene_id="scene-missing",
        selection_start=0,
        selection_end=24,
        pre_context_frames=0,
        post_context_frames=0,
        context_frames=0,
        prompt="queued prompt",
        status="pending",
        error="",
        progress=0.0,
        params={"snapshot_version": 1},
        guide_frame_snapshots=[],
        scene_width=0,
        scene_height=0,
        scene_fps=0.0,
    )

    class DummyProject:
        def __init__(self):
            self.fps = 24.0
            self.resolution = (768, 512)
            self.project_dir = str(tmp_path)
            self.generation_queue = [queue_job]

        @staticmethod
        def get_scene(scene_id):
            return None

    project = DummyProject()
    save_states = []

    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    monkeypatch.setattr(
        editor_node,
        "save_project",
        lambda proj: save_states.append((queue_job.status, queue_job.error)),
    )

    node = editor_node.SonderEditor()
    with pytest.raises(RuntimeError, match="Queued scene not found"):
        node.execute(
            project="Existing Project",
            project_name="Ignored",
            fps=24.0,
            width=768,
            height=512,
            prompt=_prompt_graph({
                "editor-1": _prompt_node("SonderEditor"),
                "save-1": _prompt_node("SonderSaveVideo", {"project": ["editor-1", 0], "mark_queue_complete": True}),
            }),
            unique_id="editor-1",
        )

    assert save_states[0][0] == "running"
    assert save_states[-1][0] == "failed"
    assert "Queued scene not found" in save_states[-1][1]
    assert queue_job.status == "failed"


def test_consume_resets_stale_running_job(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)

    job_a = types.SimpleNamespace(
        job_id="job-a",
        status="running",
        error="stale",
        progress=0.5,
    )
    job_b = types.SimpleNamespace(
        job_id="job-b",
        status="pending",
        error="queued",
        progress=0.25,
    )
    project = types.SimpleNamespace(generation_queue=[job_a, job_b])
    save_states = []

    monkeypatch.setattr(
        editor_node,
        "save_project",
        lambda proj: save_states.append(
            [(job.job_id, job.status, job.error, job.progress) for job in proj.generation_queue]
        ),
    )

    node = editor_node.SonderEditor()
    consumed = node._consume_queue_job(project)

    assert consumed is job_a
    assert job_a.status == "running"
    assert job_a.error == ""
    assert job_a.progress == 0.0
    assert job_b.status == "pending"
    assert job_b.error == "queued"
    assert job_b.progress == 0.25
    assert save_states == [[
        ("job-a", "running", "", 0.0),
        ("job-b", "pending", "queued", 0.25),
    ]]


def test_consume_resets_sole_stale_running_job(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)

    job_a = types.SimpleNamespace(
        job_id="job-a",
        status="running",
        error="stale",
        progress=0.5,
    )
    project = types.SimpleNamespace(generation_queue=[job_a])
    save_states = []

    monkeypatch.setattr(
        editor_node,
        "save_project",
        lambda proj: save_states.append(
            [(job.job_id, job.status, job.error, job.progress) for job in proj.generation_queue]
        ),
    )

    node = editor_node.SonderEditor()
    consumed = node._consume_queue_job(project)

    assert consumed is job_a
    assert job_a.status == "running"
    assert job_a.error == ""
    assert job_a.progress == 0.0
    assert save_states == [[("job-a", "running", "", 0.0)]]


def test_mark_queue_job_failed_skips_later_batch_siblings(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)

    job_a = types.SimpleNamespace(
        job_id="job-a",
        batch_id="batch-1",
        batch_index=0,
        status="pending",
        error="",
        progress=0.0,
    )
    job_b = types.SimpleNamespace(
        job_id="job-b",
        batch_id="batch-1",
        batch_index=1,
        status="running",
        error="",
        progress=0.5,
    )
    job_c = types.SimpleNamespace(
        job_id="job-c",
        batch_id="batch-1",
        batch_index=2,
        status="pending",
        error="",
        progress=0.0,
    )
    job_d = types.SimpleNamespace(
        job_id="job-d",
        batch_id="other-batch",
        batch_index=0,
        status="pending",
        error="",
        progress=0.0,
    )
    project = types.SimpleNamespace(generation_queue=[job_a, job_b, job_c, job_d])
    save_states = []

    monkeypatch.setattr(
        editor_node,
        "save_project",
        lambda proj: save_states.append(
            [(job.job_id, job.status, job.error, job.progress) for job in proj.generation_queue]
        ),
    )

    node = editor_node.SonderEditor()
    node._mark_queue_job_failed(project, job_b, "render exploded")

    assert job_a.status == "pending"
    assert job_b.status == "failed"
    assert job_b.error == "render exploded"
    assert job_b.progress == 0.0
    assert job_c.status == "failed"
    assert "Skipped after earlier batch failure (job-b)" == job_c.error
    assert job_c.progress == 0.0
    assert job_d.status == "pending"
    assert save_states == [[
        ("job-a", "pending", "", 0.0),
        ("job-b", "failed", "render exploded", 0.0),
        ("job-c", "failed", "Skipped after earlier batch failure (job-b)", 0.0),
        ("job-d", "pending", "", 0.0),
    ]]


def test_stale_running_job_recovered_on_second_execute(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")

    class DummyScene:
        scene_id = "scene-1"
        name = "Scene 1"
        duration_frames = 24
        width = 0
        height = 0
        fps = 0
        guide_frames = []

        @staticmethod
        def get_prompt_for_range(start_frame, end_frame):
            return f"live:{start_frame}-{end_frame}"

    queue_job = types.SimpleNamespace(
        job_id="job-1",
        scene_id="scene-1",
        selection_start=4,
        selection_end=12,
        pre_context_frames=0,
        post_context_frames=0,
        context_frames=0,
        prompt="queued prompt",
        status="pending",
        error="",
        progress=0.0,
        params={"snapshot_version": 1},
        guide_frame_snapshots=[],
        scene_width=0,
        scene_height=0,
        scene_fps=0.0,
    )

    class DummyProject:
        def __init__(self):
            self.fps = 24.0
            self.resolution = (768, 512)
            self.project_dir = str(tmp_path)
            self._execution_context = None
            self._scene = DummyScene()
            self.generation_queue = [queue_job]

        def get_scene(self, scene_id):
            return self._scene if scene_id == self._scene.scene_id else None

        @staticmethod
        def get_asset(asset_id):
            return None

    project = DummyProject()
    save_states = []

    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    monkeypatch.setattr(
        editor_node,
        "save_project",
        lambda proj: save_states.append(
            [(job.job_id, job.status, job.error, job.progress) for job in proj.generation_queue]
        ),
    )
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_render_scene_frames",
        lambda self, proj, scene, start, end: torch.zeros(max(1, end - start), 2, 2, 3, dtype=torch.float32),
    )
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_load_scene_audio",
        lambda self, proj, scene, start, end: editor_node._make_silent_audio(1.0),
    )

    node = editor_node.SonderEditor()

    first_result = node.execute(
        project="Existing Project",
        project_name="Ignored",
        fps=24.0,
        width=768,
        height=512,
        prompt=_prompt_graph({
            "editor-1": _prompt_node("SonderEditor"),
            "save-1": _prompt_node("SonderSaveVideo", {"project": ["editor-1", 0], "mark_queue_complete": True}),
        }),
        unique_id="editor-1",
    )
    first_job_id = project._execution_context["queue_job_id"]

    queue_job.error = "stale"
    queue_job.progress = 0.5

    second_result = node.execute(
        project="Existing Project",
        project_name="Ignored",
        fps=24.0,
        width=768,
        height=512,
        prompt=_prompt_graph({
            "editor-1": _prompt_node("SonderEditor"),
            "save-1": _prompt_node("SonderSaveVideo", {"project": ["editor-1", 0], "mark_queue_complete": True}),
        }),
        unique_id="editor-1",
    )
    second_job_id = project._execution_context["queue_job_id"]

    assert first_result[4] == "queued prompt"
    assert second_result[4] == "queued prompt"
    assert first_job_id == "job-1"
    assert second_job_id == "job-1"
    assert queue_job.status == "running"
    assert queue_job.error == ""
    assert queue_job.progress == 0.0
    assert save_states == [
        [("job-1", "running", "", 0.0)],
        [("job-1", "running", "", 0.0)],
    ]


def test_save_video_marks_queue_job_completed(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    thumbnail_service = importlib.import_module(f"{TEST_PACKAGE}.server.thumbnail_service")

    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True, exist_ok=True)
    (project_dir / "cache" / "thumbnails").mkdir(parents=True, exist_ok=True)

    scene = timeline_state.Scene(scene_id="scene-1", name="Scene 1", duration_frames=48, video_lane_count=1)
    queue_job = timeline_state.GenerationJob(job_id="job-1", scene_id="scene-1", status="running")
    project = timeline_state.TimelineProject(
        project_dir=str(project_dir),
        name="Queue Save Test",
        scenes=[scene],
        generation_queue=[queue_job],
    )
    project._execution_context = {
        "scene_id": "scene-1",
        "scene_name": "Scene 1",
        "selection_start": 4,
        "selection_end": 12,
        "pre_context_frames": 2,
        "post_context_frames": 1,
        "queue_job_id": "job-1",
    }

    def fake_ffmpeg(cmd, input=None, capture_output=None, timeout=None):
        Path(cmd[-2]).write_bytes(b"video")
        return types.SimpleNamespace(returncode=0, stderr=b"")

    save_calls = []

    monkeypatch.setattr(io_nodes.subprocess, "run", fake_ffmpeg)
    monkeypatch.setattr(io_nodes, "save_project", lambda project: save_calls.append(project.generation_queue[0].status))
    monkeypatch.setattr(thumbnail_service, "ensure_thumbnail", lambda *args, **kwargs: None)

    node = io_nodes.SonderSaveVideo()
    frames = torch.zeros(5, 2, 2, 3, dtype=torch.float32)
    result = node.save_video(project, frames, filename_prefix="queued", fps=24.0, mode="Take", mark_queue_complete=True)

    assert save_calls == ["completed"]
    assert project.generation_queue[0].status == "completed"
    assert project.generation_queue[0].progress == 1.0
    assert project.generation_queue[0].completed_at
    assert project.generation_queue[0].result_asset_id
    assert len(project.assets) == 1
    assert len(scene.clips) == 1
    assert len(scene.audio_tracks) == 0
    assert scene.clips[0].timeline_start_frame == 4
    assert scene.clips[0].timeline_end_frame == 12
    assert result["result"][0].endswith(".mp4")


def test_save_video_take_mode_creates_audio_track_when_audio_present(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    thumbnail_service = importlib.import_module(f"{TEST_PACKAGE}.server.thumbnail_service")

    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True, exist_ok=True)
    (project_dir / "cache" / "thumbnails").mkdir(parents=True, exist_ok=True)

    scene = timeline_state.Scene(scene_id="scene-1", name="Scene 1", duration_frames=48, video_lane_count=1, audio_lane_count=1)
    scene.clips.append(timeline_state.ClipReference(
        source_path="media/existing.mp4",
        timeline_start_frame=0,
        timeline_end_frame=4,
        source_out_frame=4,
        total_source_frames=4,
        track_index=0,
    ))
    scene.audio_tracks.append(timeline_state.AudioTrack(
        source_path="media/existing.wav",
        timeline_start_frame=0,
        timeline_end_frame=4,
        total_source_frames=4,
        lane_index=0,
    ))
    project = timeline_state.TimelineProject(
        project_dir=str(project_dir),
        name="Take Audio Test",
        scenes=[scene],
    )
    project._execution_context = {
        "scene_id": "scene-1",
        "scene_name": "Scene 1",
        "selection_start": 8,
        "selection_end": 12,
        "pre_context_frames": 1,
        "post_context_frames": 1,
    }

    saved_audio_paths = []

    def fake_ffmpeg(cmd, input=None, capture_output=None, timeout=None):
        Path(cmd[-2]).write_bytes(b"video")
        return types.SimpleNamespace(returncode=0, stderr=b"")

    def fake_torchaudio_save(path, waveform, sample_rate, *args, **kwargs):
        Path(path).write_bytes(b"audio")
        saved_audio_paths.append(path)

    monkeypatch.setattr(io_nodes.subprocess, "run", fake_ffmpeg)
    monkeypatch.setattr(io_nodes, "save_project", lambda project: None)
    monkeypatch.setattr(thumbnail_service, "ensure_thumbnail", lambda *args, **kwargs: None)
    monkeypatch.setitem(sys.modules, "torchaudio", types.SimpleNamespace(save=fake_torchaudio_save))

    node = io_nodes.SonderSaveVideo()
    frames = torch.zeros(5, 2, 2, 3, dtype=torch.float32)
    sample_rate = 44100
    audio = {
        "waveform": torch.zeros(1, 2, int(round((5 / 24.0) * sample_rate)), dtype=torch.float32),
        "sample_rate": sample_rate,
    }

    node.save_video(project, frames, filename_prefix="take_audio", fps=24.0, mode="Take", audio=audio)

    video_assets = [asset for asset in project.assets if asset.asset_type == "video"]
    audio_assets = [asset for asset in project.assets if asset.asset_type == "audio"]
    assert len(video_assets) == 1
    assert len(audio_assets) == 1
    assert video_assets[0].has_audio is True
    assert audio_assets[0].folder == "Takes/Scene 1"
    assert len(scene.clips) == 2
    assert len(scene.audio_tracks) == 2
    assert scene.clips[-1].track_index == 1
    assert scene.audio_tracks[-1].lane_index == 1
    assert scene.audio_lane_count == 2
    assert scene.audio_tracks[-1].timeline_start_frame == 8
    assert scene.audio_tracks[-1].timeline_end_frame == 12
    assert scene.audio_tracks[-1].source_in_frame == 1
    assert scene.audio_tracks[-1].total_source_frames == 5
    assert any(str(path).endswith("_audio.wav") for path in saved_audio_paths)


def test_save_video_maps_audio_and_uses_shorter_timeout(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    thumbnail_service = importlib.import_module(f"{TEST_PACKAGE}.server.thumbnail_service")

    class DummyProject:
        def __init__(self):
            self.project_dir = str(tmp_path)
            self.assets = []

        def add_asset(self, asset):
            self.assets.append(asset)

    captured = {}

    def fake_ffmpeg(cmd, input=None, capture_output=None, timeout=None):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        Path(cmd[-2]).write_bytes(b"video")
        return types.SimpleNamespace(returncode=0, stderr=b"")

    fake_torchaudio = types.SimpleNamespace(save=lambda *args, **kwargs: None)

    monkeypatch.setitem(sys.modules, "torchaudio", fake_torchaudio)
    monkeypatch.setattr(io_nodes.subprocess, "run", fake_ffmpeg)
    monkeypatch.setattr(io_nodes, "save_project", lambda project: None)
    monkeypatch.setattr(thumbnail_service, "ensure_thumbnail", lambda *args, **kwargs: None)

    node = io_nodes.SonderSaveVideo()
    frames = torch.zeros(2, 2, 2, 3, dtype=torch.float32)
    audio = {
        "waveform": torch.zeros(1, 2, 64, dtype=torch.float32),
        "sample_rate": 44100,
    }

    node.save_video(DummyProject(), frames, filename_prefix="audio", fps=24.0, audio=audio)

    assert captured["timeout"] == 90
    assert captured["cmd"].count("-map") == 2
    assert "0:v:0" in captured["cmd"]
    assert "1:a:0" in captured["cmd"]
    assert "-c:a" in captured["cmd"]
    assert "aac" in captured["cmd"]


def test_preview_uses_shorter_timeout(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")

    captured = {}

    def fake_ffmpeg(cmd, input=None, capture_output=None, timeout=None):
        captured["timeout"] = timeout
        Path(cmd[-2]).write_bytes(b"video")
        return types.SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(io_nodes.subprocess, "run", fake_ffmpeg)

    node = io_nodes.SonderPreviewVideo()
    frames = torch.zeros(2, 2, 2, 3, dtype=torch.float32)
    node.preview(frames, fps=24.0)

    assert captured["timeout"] == 90


def test_render_scene_frames_deletes_corrupt_cache(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)

    cache_dir = tmp_path / "cache" / "renders"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "scene-1_hash.pt"
    cache_path.write_bytes(b"corrupt")

    class DummyScene:
        scene_id = "scene-1"
        width = 0
        height = 0
        video_lane_configs = []
        clips = []

        @staticmethod
        def content_hash(render_start, render_end, resolution):
            return "hash"

    project = types.SimpleNamespace(project_dir=str(tmp_path), resolution=(8, 6))
    removed = []

    def fail_load(*args, **kwargs):
        raise RuntimeError("corrupt cache")

    monkeypatch.setattr(editor_node.torch, "load", fail_load)
    monkeypatch.setattr(editor_node.os, "remove", lambda path: removed.append(path))

    node = editor_node.SonderEditor()
    result = node._render_scene_frames(project, DummyScene(), 0, 4)

    assert str(cache_path) in removed
    assert tuple(result.shape) == (4, 6, 8, 3)


def test_load_scene_audio_logs_missing_source_and_silent_fallback(tmp_path, monkeypatch, caplog):
    editor_node = _import_editor_node(tmp_path, monkeypatch)

    fake_torchaudio = types.SimpleNamespace(load=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected load")))
    monkeypatch.setitem(sys.modules, "torchaudio", fake_torchaudio)

    track = types.SimpleNamespace(
        muted=False,
        lane_index=0,
        timeline_start_frame=0,
        timeline_end_frame=12,
        source_path="missing.wav",
        source_in_frame=0,
        volume=1.0,
    )
    scene = types.SimpleNamespace(
        fps=0.0,
        audio_tracks=[track],
        audio_lane_configs=[],
    )
    project = types.SimpleNamespace(
        fps=24.0,
        project_dir=str(tmp_path),
    )

    caplog.set_level("INFO", logger="sonder_editor")

    node = editor_node.SonderEditor()
    audio = node._load_scene_audio(project, scene, 0, 12)

    assert audio["sample_rate"] == 44100
    assert "file not found" in caplog.text
    assert "fell back to silence" in caplog.text
