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


def _fake_encode_video_success(io_nodes, calls=None):
    def fake_encode_video(frames_iter, *, preset_id, output_path, fps, audio_path=None, custom_options=None, timeout=90, **kwargs):
        Path(output_path).write_bytes(b"video")
        if calls is not None:
            calls.append({
                "preset_id": preset_id,
                "output_path": output_path,
                "fps": fps,
                "audio_path": audio_path,
                "custom_options": custom_options,
                "timeout": timeout,
                **kwargs,
            })
        return io_nodes.metadata_for_save_preset(preset_id, custom_options)

    return fake_encode_video


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
        def get_prompt_for_range(start_frame, end_frame, labels_on=True, delimiter=".", boundary_threshold_pct=0.0):
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
        mask_pre_offset="2",
        mask_post_offset="1",
        take_placement_linked=False,
        take_placement_muted=True,
    )

    assert result[6] == 14
    assert result[11] == pytest.approx(1 / 24.0)
    assert result[12] == pytest.approx(11 / 24.0)
    assert project._execution_context["pre_context_frames"] == 3
    assert project._execution_context["post_context_frames"] == 4
    assert project._execution_context["mask_pre_offset"] == 2
    assert project._execution_context["mask_post_offset"] == 1
    assert project._execution_context["take_placement_linked"] is False
    assert project._execution_context["take_placement_muted"] is True


class _FrameConstraintScene:
    scene_id = "scene-1"
    name = "Scene 1"
    duration_frames = 601
    width = 0
    height = 0
    fps = 24.0
    guide_frames = []

    @staticmethod
    def get_prompt_for_range(start_frame, end_frame, labels_on=True, delimiter=".", boundary_threshold_pct=0.0):
        return f"prompt:{start_frame}-{end_frame}"


class _FrameConstraintProject:
    def __init__(self, project_dir, *, template_id="free", frame_constraint=None):
        self.fps = 24.0
        self.resolution = (4, 4)
        self.project_dir = str(project_dir)
        self.template_id = template_id
        self.frame_constraint = frame_constraint
        self._execution_context = None
        self._scene = _FrameConstraintScene()

    def get_scene(self, scene_id):
        return self._scene if scene_id == self._scene.scene_id else None

    @staticmethod
    def get_asset(asset_id):
        return None


def _patch_render_and_audio(editor_node, monkeypatch):
    torch = importlib.import_module("torch")
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_render_scene_frames",
        lambda self, proj, scene, start, end: torch.ones(end - start, 4, 4, 3, dtype=torch.float32),
    )
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_load_scene_audio",
        lambda self, proj, scene, start, end: editor_node._make_silent_audio((end - start) / 24.0),
    )


def _execute_constraint_test(editor_node, project, monkeypatch):
    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    _patch_render_and_audio(editor_node, monkeypatch)
    return editor_node.SonderEditor().execute(
        project="Existing Project",
        project_name="Ignored",
        fps=24.0,
        width=768,
        height=512,
        scene_id="scene-1",
        selection_start=482,
        selection_end=601,
        pre_context_frames=48,
        post_context_frames=0,
    )


def test_execute_rounds_ltx_context_frame_count_up_and_pads_outputs(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)

    project = _FrameConstraintProject(
        tmp_path,
        template_id="ltx-2.3",
        frame_constraint={"step": 8, "offset": 1, "min": 1},
    )
    result = _execute_constraint_test(editor_node, project, monkeypatch)

    audio = result[10]
    assert result[6] == 169
    assert tuple(result[1].shape) == (169, 4, 4, 3)
    # Context-first alignment: requested pre=48 sits between LTX boundaries 41 and 49;
    # actual_pre expands by 1 to 49 so the in-point (mask_start) lands on grid without
    # the snap helper extending the mask into already-rendered pre-context.
    # mask_end = 49 + 119 + 0 + padding(1) = 169 is already on the 8n+1 grid.
    assert result[11] == pytest.approx(49 / 24.0)
    assert result[12] == pytest.approx(169 / 24.0)
    assert audio["waveform"].shape[-1] >= int((169 / 24.0) * audio["sample_rate"])
    assert project._execution_context["source_frame_count"] == 168
    assert project._execution_context["frame_count"] == 169
    assert project._execution_context["frame_count_padding"] == 1
    assert project._execution_context["template_id"] == "ltx-2.3"
    assert project._execution_context["actual_pre_context_frames"] == 49
    assert project._execution_context["mask_start_frame"] == 49
    assert project._execution_context["mask_end_frame"] == 169


def test_queue_job_frame_constraint_overrides_project(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)

    project = _FrameConstraintProject(
        tmp_path,
        template_id="ltx-2.3",
        frame_constraint={"step": 8, "offset": 1, "min": 1},
    )
    queue_job = type(
        "DummyQueueJob",
        (),
        {
            "scene_id": "scene-1",
            "selection_start": 482,
            "selection_end": 601,
            "pre_context_frames": 48,
            "post_context_frames": 0,
            "context_frames": 48,
            "template_id": "custom-job-wins",
            "frame_constraint": {"step": 4, "offset": 0, "min": 1},
            "take_placement_mode": "trimmed",
            "params": {},
            "prompt": "queued",
            "scene_name": "Scene 1",
            "status": "pending",
            "job_id": "job-1",
            "batch_id": "",
            "batch_total": 0,
            "batch_index": 0,
            "guide_frame_snapshots": [],
            "prompt_sections": [],
            "scene_width": 0,
            "scene_height": 0,
            "scene_fps": 0.0,
            "error": "",
            "progress": 0.0,
        },
    )()
    project.generation_queue = [queue_job]

    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    monkeypatch.setattr(editor_node, "save_project", lambda proj: None)
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_execution_reaches_terminal_save",
        lambda self, prompt, node_id: True,
    )
    _patch_render_and_audio(editor_node, monkeypatch)

    result = editor_node.SonderEditor().execute(
        project="Existing Project",
        project_name="Ignored",
        fps=24.0,
        width=768,
        height=512,
        scene_id="scene-1",
        selection_start=0,
        selection_end=0,
        pre_context_frames=0,
        post_context_frames=0,
        prompt={"1": {"class_type": "SonderEditor", "inputs": {}}},
        unique_id="1",
    )

    # 167 source frames -> next 4n is 168 (job constraint wins over project's 8n+1=169)
    assert result[6] == 168
    assert tuple(result[1].shape) == (168, 4, 4, 3)
    assert project._execution_context["source_frame_count"] == 167
    assert project._execution_context["frame_count"] == 168
    assert project._execution_context["frame_count_padding"] == 1
    assert project._execution_context["template_id"] == "custom-job-wins"


def test_missing_frame_constraint_no_padding(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)

    project = _FrameConstraintProject(tmp_path, template_id="free", frame_constraint=None)
    result = _execute_constraint_test(editor_node, project, monkeypatch)

    assert result[6] == 167
    assert tuple(result[1].shape) == (167, 4, 4, 3)
    assert project._execution_context["source_frame_count"] == 167
    assert project._execution_context["frame_count"] == 167
    assert project._execution_context["frame_count_padding"] == 0


def test_custom_template_constraint_pads_correctly(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)

    project = _FrameConstraintProject(
        tmp_path,
        template_id="my-custom-template",
        frame_constraint={"step": 10, "offset": 3, "min": 1},
    )
    result = _execute_constraint_test(editor_node, project, monkeypatch)

    # Context-first alignment with step=10/offset=3: desired mask_start = 48,
    # next grid value is 53, so actual_pre expands by 5 to 53.
    # source = 53+119+0 = 172 -> next 10n+3 is 173 -> padding = 1 (was 6 pre-expansion).
    assert result[6] == 173
    assert tuple(result[1].shape) == (173, 4, 4, 3)
    assert project._execution_context["actual_pre_context_frames"] == 53
    assert project._execution_context["frame_count"] == 173
    assert project._execution_context["frame_count_padding"] == 1
    assert project._execution_context["template_id"] == "my-custom-template"


def test_snap_pixel_to_constraint_helper_covers_boundary_cases(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    snap = editor_node.SonderEditor._snap_pixel_to_constraint
    ltx = {"step": 8, "offset": 1, "min": 1}

    # No constraint / empty / step<=1 -> no-op
    assert snap(48, None, "start") == 48
    assert snap(48, {}, "end") == 48
    assert snap(48, {"step": 1, "offset": 0}, "start") == 48

    # LTX boundary set: 0, 1, 9, 17, 25, 33, 41, 49, ...
    # Exact boundary -> no change either side
    assert snap(41, ltx, "start") == 41
    assert snap(41, ltx, "end") == 41
    assert snap(49, ltx, "start") == 49
    assert snap(49, ltx, "end") == 49
    # Inside latent 6 (pixels 41..48 belong to one latent) -> start floors to 41, end ceils to 49
    assert snap(48, ltx, "start") == 41
    assert snap(48, ltx, "end") == 49
    assert snap(45, ltx, "start") == 41
    assert snap(45, ltx, "end") == 49
    # Pixel 0 -> 0 either side; negative -> 0
    assert snap(0, ltx, "start") == 0
    assert snap(0, ltx, "end") == 0
    assert snap(-5, ltx, "start") == 0
    assert snap(-5, ltx, "end") == 0

    # Custom constraint {step:10, offset:3} -> boundary set 0, 3, 13, 23, 33, 43, 53, ...
    custom = {"step": 10, "offset": 3, "min": 1}
    assert snap(48, custom, "start") == 43
    assert snap(48, custom, "end") == 53
    # Below offset (pixel > 0 but < offset) -> 0 for start, offset for end
    assert snap(2, custom, "start") == 0
    assert snap(2, custom, "end") == 3


def test_execute_expands_pre_context_to_align_mask_with_ltx_boundary(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    project = _FrameConstraintProject(
        tmp_path,
        template_id="ltx-2.3",
        frame_constraint={"step": 8, "offset": 1, "min": 1},
    )
    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    _patch_render_and_audio(editor_node, monkeypatch)
    result = editor_node.SonderEditor().execute(
        project="Existing Project",
        project_name="Ignored",
        fps=24.0,
        width=768,
        height=512,
        scene_id="scene-1",
        selection_start=482,
        selection_end=601,
        pre_context_frames=48,
        post_context_frames=0,
        mask_pre_offset=10,
        mask_post_offset=0,
    )

    # Independent snaps with mask_pre_offset=10: actual_pre(48) snaps to next G = 49
    # (independent of mask offset). mask_pre_offset(10) snaps to the valid set within
    # actual_pre=49 (= {0, 8, 16, 24, 32, 40, 48, 49}), next >= 10 is 16.
    # mask_start = 49 - 16 = 33; source = 49+119+0 = 168; target = 169; padding = 1;
    # mask_end = 49+119+0+1 = 169 (on grid). No snap-helper fallback fires.
    assert result[11] == pytest.approx(33 / 24.0)
    assert result[12] == pytest.approx(169 / 24.0)
    assert project._execution_context["actual_pre_context_frames"] == 49
    assert project._execution_context["mask_pre_offset"] == 16
    assert project._execution_context["frame_count_padding"] == 1
    assert project._execution_context["mask_start_frame"] == 33
    assert project._execution_context["mask_end_frame"] == 169


def test_execute_expands_both_sides_for_ltx_alignment(tmp_path, monkeypatch):
    # Exercises pre- AND post-side context expansion in the same render. With pre=8,
    # post=5, mask_pre=0, mask_post=0 on LTX 8n+1: actual_pre grows 8 -> 9
    # (desired mask_start=8, next grid 9); actual_post grows 5 -> 8 ((post-mask)%8=5,
    # so extension=3). source = 9+100+8 = 117; target = 121; padding = 4;
    # mask_start = 9, mask_end = 9+100+0+4 = 113 (both on grid, no snap fires).
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    project = _FrameConstraintProject(
        tmp_path,
        template_id="ltx-2.3",
        frame_constraint={"step": 8, "offset": 1, "min": 1},
    )
    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    _patch_render_and_audio(editor_node, monkeypatch)
    result = editor_node.SonderEditor().execute(
        project="Existing Project",
        project_name="Ignored",
        fps=24.0,
        width=768,
        height=512,
        scene_id="scene-1",
        selection_start=100,
        selection_end=200,
        pre_context_frames=8,
        post_context_frames=5,
        mask_pre_offset=0,
        mask_post_offset=0,
    )

    assert result[11] == pytest.approx(9 / 24.0)
    assert result[12] == pytest.approx(113 / 24.0)
    assert project._execution_context["actual_pre_context_frames"] == 9
    assert project._execution_context["actual_post_context_frames"] == 8
    assert project._execution_context["frame_count"] == 121
    assert project._execution_context["frame_count_padding"] == 4
    assert project._execution_context["mask_start_frame"] == 9
    assert project._execution_context["mask_end_frame"] == 113


def test_execute_falls_back_to_floor_snap_at_scene_start(tmp_path, monkeypatch):
    # Scene-edge fallback: selection_start=3 leaves only 3 frames of pre-context
    # available. Requested pre=24 is clamped to 3; desired mask_start=3 needs to
    # reach grid value 9 (extension 6), but available=0 -> expansion is skipped
    # (all-or-nothing) and the floor snap fires as a fallback so mask_start lands
    # on grid by extending the mask back into the available pre-context (the leak
    # the new policy avoids when expansion is possible).
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    project = _FrameConstraintProject(
        tmp_path,
        template_id="ltx-2.3",
        frame_constraint={"step": 8, "offset": 1, "min": 1},
    )
    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    _patch_render_and_audio(editor_node, monkeypatch)
    result = editor_node.SonderEditor().execute(
        project="Existing Project",
        project_name="Ignored",
        fps=24.0,
        width=768,
        height=512,
        scene_id="scene-1",
        selection_start=3,
        selection_end=100,
        pre_context_frames=24,
        post_context_frames=0,
        mask_pre_offset=0,
        mask_post_offset=0,
    )

    # actual_pre clamped to 3 (scene start); no expansion possible; snap floor 3 -> 1.
    assert project._execution_context["actual_pre_context_frames"] == 3
    assert result[11] == pytest.approx(1 / 24.0)
    assert project._execution_context["mask_start_frame"] == 1


def test_execute_snaps_mask_offsets_independently_of_context_frames(tmp_path, monkeypatch):
    # The core fix: setting a mask offset must NOT inflate the rendered tensor.
    # Inputs pre=24, mask_pre=12, post=24, mask_post=12 -> actual_pre snaps 24 -> 25
    # (independent of mask_pre); actual_post stays 24 (already multiple of step);
    # mask_pre_offset snaps 12 -> 16 (within {0,8,16,24,25}); mask_post_offset
    # snaps 12 -> 16 (within {0,8,16,24}). source = 25+120+24 = 169 (already in G,
    # padding=0). mask_start = 9, mask_end = 161. Crucially: rendered tensor is 169
    # frames, NOT inflated to e.g. 177 (the pre-fix coupled-expansion behavior).
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    project = _FrameConstraintProject(
        tmp_path,
        template_id="ltx-2.3",
        frame_constraint={"step": 8, "offset": 1, "min": 1},
    )
    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    _patch_render_and_audio(editor_node, monkeypatch)
    result = editor_node.SonderEditor().execute(
        project="Existing Project",
        project_name="Ignored",
        fps=24.0,
        width=768,
        height=512,
        scene_id="scene-1",
        selection_start=100,
        selection_end=220,
        pre_context_frames=24,
        post_context_frames=24,
        mask_pre_offset=12,
        mask_post_offset=12,
    )

    assert result[6] == 169
    assert project._execution_context["actual_pre_context_frames"] == 25
    assert project._execution_context["actual_post_context_frames"] == 24
    assert project._execution_context["mask_pre_offset"] == 16
    assert project._execution_context["mask_post_offset"] == 16
    assert project._execution_context["frame_count"] == 169
    assert project._execution_context["frame_count_padding"] == 0
    assert project._execution_context["mask_start_frame"] == 9
    assert project._execution_context["mask_end_frame"] == 161
    # Sanity vs the post-side bug: if mask_post=0 instead, frame_count should be the
    # same 169. Verifies mask_post does not inflate the tensor.


def test_execute_snaps_stored_off_grid_mask_offsets_from_queue_job(tmp_path, monkeypatch):
    # Backward-compat regression: queued GenerationJob snapshots persisted before
    # the independent-snap rewrite carry raw off-grid mask offsets (e.g. 10). On
    # consume, the new rules snap them up to the nearest valid value (16 for LTX).
    # Verifies the consume path matches the widget-input path.
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    project = _FrameConstraintProject(
        tmp_path,
        template_id="ltx-2.3",
        frame_constraint={"step": 8, "offset": 1, "min": 1},
    )
    queue_job = type(
        "DummyQueueJob",
        (),
        {
            "scene_id": "scene-1",
            "selection_start": 482,
            "selection_end": 601,
            "pre_context_frames": 48,
            "post_context_frames": 0,
            "context_frames": 48,
            "mask_pre_offset": 10,  # off-grid; pre-existing queue snapshot
            "mask_post_offset": 0,
            "template_id": "ltx-2.3",
            "frame_constraint": {"step": 8, "offset": 1, "min": 1},
            "take_placement_mode": "trimmed",
            "params": {},
            "prompt": "queued",
            "scene_name": "Scene 1",
            "status": "pending",
            "job_id": "job-1",
            "batch_id": "",
            "batch_total": 0,
            "batch_index": 0,
            "guide_frame_snapshots": [],
            "prompt_sections": [],
            "scene_width": 0,
            "scene_height": 0,
            "scene_fps": 0.0,
            "error": "",
            "progress": 0.0,
        },
    )()
    project.generation_queue = [queue_job]

    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    monkeypatch.setattr(editor_node, "save_project", lambda proj: None)
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_execution_reaches_terminal_save",
        lambda self, prompt, node_id: True,
    )
    _patch_render_and_audio(editor_node, monkeypatch)

    editor_node.SonderEditor().execute(
        project="Existing Project",
        project_name="Ignored",
        fps=24.0,
        width=768,
        height=512,
        scene_id="scene-1",
        selection_start=0,
        selection_end=0,
        pre_context_frames=0,
        post_context_frames=0,
        prompt={"1": {"class_type": "SonderEditor", "inputs": {}}},
        unique_id="1",
    )

    # Same end-state as the widget-input path: actual_pre 48 -> 49, mask_pre 10 -> 16.
    assert project._execution_context["actual_pre_context_frames"] == 49
    assert project._execution_context["mask_pre_offset"] == 16
    assert project._execution_context["mask_start_frame"] == 33
    assert project._execution_context["mask_end_frame"] == 169


def test_execute_first_batch_chunk_with_offset_grown_gen_renders_clean(tmp_path, monkeypatch):
    # Backend side of the frontend's first-chunk fix (resolveBatchChunkSizes): a batch's
    # first chunk sits at scene frame 0, so it has no pre-context to carry the LTX +1.
    # The frontend grows that chunk's gen by `offset` (72 -> 73) so the total lands on G
    # WITHOUT the backend tail-padding a repeated frame. This verifies the grown chunk
    # (selection [0, 73), pre=25) renders as a clean 73-frame tensor with zero padding.
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    project = _FrameConstraintProject(
        tmp_path,
        template_id="ltx-2.3",
        frame_constraint={"step": 8, "offset": 1, "min": 1},
    )
    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    _patch_render_and_audio(editor_node, monkeypatch)
    result = editor_node.SonderEditor().execute(
        project="Existing Project",
        project_name="Ignored",
        fps=24.0,
        width=768,
        height=512,
        scene_id="scene-1",
        selection_start=0,
        selection_end=73,
        pre_context_frames=25,
        post_context_frames=0,
        mask_pre_offset=0,
        mask_post_offset=0,
    )

    # actual_pre clamps to 0 (no frames before scene start); gen=73 is already in G,
    # so total=73 with NO padding (contrast: un-grown gen=72 would round up to 73 with
    # padding=1, a repeated tail frame).
    assert result[6] == 73
    assert project._execution_context["actual_pre_context_frames"] == 0
    assert project._execution_context["frame_count"] == 73
    assert project._execution_context["frame_count_padding"] == 0
    assert project._execution_context["mask_start_frame"] == 0
    assert project._execution_context["mask_end_frame"] == 73


def test_execute_first_batch_chunk_without_offset_grown_gen_needs_padding(tmp_path, monkeypatch):
    # Companion contrast: the SAME chunk un-grown (gen=72) at scene start needs 1 frame
    # of tail padding to satisfy the constraint — which is exactly what the frontend's
    # +offset growth avoids. Locks in the motivation for the first-chunk fix.
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    project = _FrameConstraintProject(
        tmp_path,
        template_id="ltx-2.3",
        frame_constraint={"step": 8, "offset": 1, "min": 1},
    )
    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    _patch_render_and_audio(editor_node, monkeypatch)
    result = editor_node.SonderEditor().execute(
        project="Existing Project",
        project_name="Ignored",
        fps=24.0,
        width=768,
        height=512,
        scene_id="scene-1",
        selection_start=0,
        selection_end=72,
        pre_context_frames=25,
        post_context_frames=0,
        mask_pre_offset=0,
        mask_post_offset=0,
    )

    assert result[6] == 73
    assert project._execution_context["frame_count_padding"] == 1


def test_execute_mask_times_unsnapped_without_frame_constraint(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    project = _FrameConstraintProject(tmp_path, template_id="free", frame_constraint=None)
    result = _execute_constraint_test(editor_node, project, monkeypatch)
    # No constraint -> snap is a no-op; raw values pass through.
    # source = 167 (no padding); mask_start_pixel = 48, mask_end_pixel = 167.
    assert result[11] == pytest.approx(48 / 24.0)
    assert result[12] == pytest.approx(167 / 24.0)
    assert project._execution_context["mask_start_frame"] == 48
    assert project._execution_context["mask_end_frame"] == 167


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
    assert node._execution_targets_unmarked_save(prompt, "1") is True


def test_execution_targets_unmarked_save_only_for_linked_editor(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)

    prompt = _prompt_graph({
        "1": _prompt_node("SonderEditor"),
        "2": _prompt_node("SonderEditor"),
        "3": _prompt_node("SonderSaveVideo", {"project": ["2", 0], "mark_queue_complete": False}),
    })

    node = editor_node.SonderEditor()

    assert node._execution_targets_unmarked_save(prompt, "1") is False
    assert node._execution_targets_unmarked_save(prompt, "2") is True


def test_execution_reaches_terminal_bridge_save(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)

    prompt = _prompt_graph({
        "1": _prompt_node("SonderEditor"),
        "3": _prompt_node("SonderSaveBridge", {"project": ["1", 0], "mark_queue_complete": True}),
    })

    node = editor_node.SonderEditor()

    assert node._execution_reaches_terminal_save(prompt, "1") is True


def test_empty_execute_result_matches_output_contract(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")

    expected_names = (
        "project", "rendered_frames", "guide_images", "guide_idx", "guide_strengths",
        "prompt", "frame_count", "fps", "width", "height", "audio",
        "mask_start_time", "mask_end_time",
    )
    assert editor_node.SonderEditor.RETURN_NAMES == expected_names
    assert len(editor_node.SonderEditor.RETURN_TYPES) == 13
    assert len(editor_node.SonderEditor.OUTPUT_TOOLTIPS) == 13

    project = timeline_state.TimelineProject(project_dir=str(tmp_path), resolution=(8, 6))
    result = editor_node.SonderEditor._empty_execute_result(project, 24.0, 8, 6)

    assert len(result) == 13
    assert result[0] is project
    assert tuple(result[1].shape) == (1, 6, 8, 3)
    assert tuple(result[2].shape) == (1, 6, 8, 3)
    assert result[3] == ""
    assert result[4] == ""
    assert result[5] == ""
    assert result[6] == 0
    assert result[7] == 24.0
    assert result[8] == 8
    assert result[9] == 6
    assert "waveform" in result[10]
    assert result[11] == 0.0
    assert result[12] == 0.0


def test_execute_emits_guide_strengths_and_empty_csvs(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")

    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    (project_dir / "media" / "guide.png").write_bytes(b"guide")

    scene = timeline_state.Scene(scene_id="scene-1", name="Scene", duration_frames=12)
    scene.guide_frames = [
        timeline_state.GuideFrame(frame_index=1, asset_id="guide-a", strength=0.25),
        timeline_state.GuideFrame(frame_index=4, asset_id="guide-b", strength=0.9, muted=True),
    ]
    project = timeline_state.TimelineProject(project_dir=str(project_dir), name="Guides", scenes=[scene])
    project.assets = [
        timeline_state.Asset(asset_id="guide-a", asset_type="image", path=os.path.join("media", "guide.png")),
        timeline_state.Asset(asset_id="guide-b", asset_type="image", path=os.path.join("media", "guide.png")),
    ]

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
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_load_guide_image",
        lambda self, path, asset_type, target_w, target_h, **_kwargs: torch.ones(target_h, target_w, 3, dtype=torch.float32),
    )

    result = editor_node.SonderEditor().execute(
        project="Existing Project",
        project_name="Ignored",
        fps=24.0,
        width=768,
        height=512,
        scene_id="scene-1",
        selection_start=0,
        selection_end=8,
    )

    assert result[3] == "1"
    assert result[4] == "0.2500"
    assert tuple(result[2].shape) == (1, 512, 768, 3)

    scene.guide_track_config = timeline_state.LaneConfig(hidden=True)
    hidden_track_result = editor_node.SonderEditor().execute(
        project="Existing Project",
        project_name="Ignored",
        fps=24.0,
        width=768,
        height=512,
        scene_id="scene-1",
        selection_start=0,
        selection_end=8,
    )

    assert hidden_track_result[3] == ""
    assert hidden_track_result[4] == ""
    assert tuple(hidden_track_result[2].shape) == (1, 512, 768, 3)

    scene.guide_track_config = timeline_state.LaneConfig()
    scene.guide_frames = []
    empty_result = editor_node.SonderEditor().execute(
        project="Existing Project",
        project_name="Ignored",
        fps=24.0,
        width=768,
        height=512,
        scene_id="scene-1",
        selection_start=0,
        selection_end=8,
    )

    assert empty_result[3] == ""
    assert empty_result[4] == ""
    assert tuple(empty_result[2].shape) == (1, 512, 768, 3)


def test_guide_image_output_letterboxes_to_project_canvas(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    cv2 = importlib.import_module("cv2")
    np = importlib.import_module("numpy")

    image_path = tmp_path / "portrait.png"
    portrait_bgr = np.full((4, 2, 3), 255, dtype=np.uint8)
    assert cv2.imwrite(str(image_path), portrait_bgr)

    tensor = editor_node.SonderEditor()._load_guide_image(
        str(image_path),
        "image",
        4,
        4,
        fit_mode="fit",
    )

    assert tuple(tensor.shape) == (4, 4, 3)
    np_tensor = tensor.numpy()
    assert np.allclose(np_tensor[:, 0, :], 0.0)
    assert np.allclose(np_tensor[:, 3, :], 0.0)
    assert np.allclose(np_tensor[:, 1:3, :], 1.0)


def test_render_scene_frames_excludes_motion_driver_clips(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    cv2 = importlib.import_module("cv2")
    np = importlib.import_module("numpy")

    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    (project_dir / "media" / "render.mp4").write_bytes(b"render")
    (project_dir / "media" / "driver.mp4").write_bytes(b"driver")
    project = timeline_state.TimelineProject(project_dir=str(project_dir), name="Render")
    scene = timeline_state.Scene(scene_id="scene-1", duration_frames=1)
    scene.video_lane_configs = [timeline_state.LaneConfig()]
    scene.clips = [
        timeline_state.ClipReference(
            source_path=os.path.join("media", "render.mp4"),
            timeline_start_frame=0,
            timeline_end_frame=1,
            role="render",
        ),
        timeline_state.ClipReference(
            source_path=os.path.join("media", "driver.mp4"),
            timeline_start_frame=0,
            timeline_end_frame=1,
            role="motion_driver",
        ),
    ]

    class FakeCapture:
        def __init__(self, path):
            self.path = path

        def isOpened(self):
            return True

        def set(self, prop, value):
            pass

        def read(self):
            color = [255, 0, 0] if self.path.endswith("render.mp4") else [0, 0, 255]
            return True, np.full((2, 2, 3), color, dtype=np.uint8)

        def release(self):
            pass

    monkeypatch.setattr(editor_node.cv2, "VideoCapture", FakeCapture)

    tensor = editor_node.SonderEditor()._render_scene_frames(project, scene, 0, 1)

    # Render source is blue after BGR->RGB conversion; red would mean the driver leaked in.
    assert float(tensor[..., 2].max()) == pytest.approx(1.0)
    assert float(tensor[..., 0].max()) == pytest.approx(0.0)


def test_render_scene_frames_uses_take_source_in_without_repeating_seam_frame(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    cv2 = importlib.import_module("cv2")
    np = importlib.import_module("numpy")

    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    (media_dir / "chunk1.mp4").write_bytes(b"chunk1")
    (media_dir / "chunk2.mp4").write_bytes(b"chunk2")

    project = timeline_state.TimelineProject(project_dir=str(project_dir), resolution=(1, 1))
    scene = timeline_state.Scene(scene_id="scene-1", duration_frames=10)
    scene.video_lane_configs = [timeline_state.LaneConfig(), timeline_state.LaneConfig()]
    scene.clips = [
        timeline_state.ClipReference(
            source_path=os.path.join("media", "chunk1.mp4"),
            timeline_start_frame=0,
            timeline_end_frame=6,
            source_in_frame=0,
            source_out_frame=6,
            total_source_frames=6,
            track_index=0,
        ),
        timeline_state.ClipReference(
            source_path=os.path.join("media", "chunk2.mp4"),
            timeline_start_frame=6,
            timeline_end_frame=10,
            source_in_frame=2,
            source_out_frame=6,
            total_source_frames=6,
            track_index=1,
        ),
    ]

    class NumberedCapture:
        def __init__(self, path):
            self.path = path
            self.pos = 0

        def isOpened(self):
            return True

        def set(self, prop, value):
            if prop == cv2.CAP_PROP_POS_FRAMES:
                self.pos = int(value)

        def read(self):
            base = 4 if self.path.endswith("chunk2.mp4") else 0
            value = base + self.pos
            return True, np.full((1, 1, 3), value, dtype=np.uint8)

        def release(self):
            pass

    monkeypatch.setattr(editor_node.cv2, "VideoCapture", NumberedCapture)

    tensor = editor_node.SonderEditor()._render_scene_frames(project, scene, 4, 8)
    values = [round(float(tensor[i, 0, 0, 0]) * 255) for i in range(tensor.shape[0])]

    assert values == [4, 5, 6, 7]


def test_execute_peeks_pending_queue_job_without_downstream_save(tmp_path, monkeypatch):
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
        def get_prompt_for_range(start_frame, end_frame, labels_on=True, delimiter=".", boundary_threshold_pct=0.0):
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
        take_placement_mode="untrimmed",
    )

    # render_queue_active defaults True and a job is pending; with no save node the
    # editor PEEKS the queued snapshot (renders its range) without consuming it. The
    # no-mutation invariants below prove peek != consume.
    assert save_calls == []
    assert queue_job.status == "pending"
    # Queued snapshot 10-30 with pre=4/post=6 → gen window [6, 36) = 30 frames,
    # overriding the live 2-8 selection passed to execute(); mask spans 4/24 → 24/24.
    assert result[5] == "queued prompt"
    assert result[6] == 30
    assert result[11] == pytest.approx(4 / 24.0)
    assert result[12] == pytest.approx(24 / 24.0)
    assert project._execution_context["queue_job_id"] == ""
    # Snapshot reference for read-only consumers (prompt relay bridge) IS set on peek
    assert project._execution_context["queue_job_ref_id"] == "job-1"
    assert project._execution_context["take_placement_mode"] == "untrimmed"


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
        def get_prompt_for_range(start_frame, end_frame, labels_on=True, delimiter=".", boundary_threshold_pct=0.0):
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
        take_placement_mode="untrimmed",
        take_placement_linked=False,
        take_placement_muted=True,
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
        lambda self, path, asset_type, target_w, target_h, **_kwargs: torch.ones(target_h, target_w, 3, dtype=torch.float32),
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
    assert result[4] == "1.0000"
    assert result[5] == "queued prompt"
    assert result[6] == 30
    assert result[7] == 30.0
    assert result[8] == 8
    assert result[9] == 6
    assert result[11] == pytest.approx(4 / 30.0)
    assert result[12] == pytest.approx(24 / 30.0)
    assert project._execution_context["queue_job_id"] == "job-1"
    # Consume also carries the read-only snapshot reference
    assert project._execution_context["queue_job_ref_id"] == "job-1"
    assert project._execution_context["take_placement_mode"] == "untrimmed"
    # take_placement_linked / take_placement_muted are NOT frozen per job
    # (2026-06-11): the stored job values (False/True above) must be ignored
    # in favor of the live widget inputs (defaults True/False here).
    assert project._execution_context["take_placement_linked"] is True
    assert project._execution_context["take_placement_muted"] is False


def test_consumed_queue_job_renders_snapshot_range(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")

    class DummyScene:
        scene_id = "scene-1"
        name = "Scene 1"
        duration_frames = 96
        width = 0
        height = 0
        fps = 0
        guide_frames = []

        @staticmethod
        def get_prompt_for_range(start_frame, end_frame, labels_on=True, delimiter=".", boundary_threshold_pct=0.0):
            return f"live:{start_frame}-{end_frame}"

    queue_job = types.SimpleNamespace(
        job_id="job-1",
        scene_id="scene-1",
        selection_start=24,
        selection_end=48,
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
    render_ranges = []

    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    monkeypatch.setattr(editor_node, "save_project", lambda proj: None)
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_render_scene_frames",
        lambda self, proj, scene, start, end: (
            render_ranges.append((start, end))
            or torch.zeros(max(1, end - start), 2, 2, 3, dtype=torch.float32)
        ),
    )
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_load_scene_audio",
        lambda self, proj, scene, start, end: editor_node._make_silent_audio(1.0),
    )

    result = editor_node.SonderEditor().execute(
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
        scene_id="scene-1",
        selection_start=0,
        selection_end=0,
    )

    assert render_ranges == [(24, 48)]
    assert result[5] == "queued prompt"
    assert result[6] == 24
    assert project._execution_context["queue_job_id"] == "job-1"


@pytest.mark.parametrize("queue_status", ["pending", "running"])
def test_unmarked_save_with_active_queue_peeks_without_completion(tmp_path, monkeypatch, queue_status):
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
        def get_prompt_for_range(start_frame, end_frame, labels_on=True, delimiter=".", boundary_threshold_pct=0.0):
            return f"live:{start_frame}-{end_frame}"

    queue_job = types.SimpleNamespace(
        job_id="job-1",
        scene_id="scene-1",
        selection_start=10,
        selection_end=30,
        pre_context_frames=0,
        post_context_frames=0,
        context_frames=0,
        prompt="queued prompt",
        status=queue_status,
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

    project = DummyProject()
    save_calls = []
    render_ranges = []

    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    monkeypatch.setattr(editor_node, "save_project", lambda proj: save_calls.append(queue_job.status))
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_render_scene_frames",
        lambda self, proj, scene, start, end: (
            render_ranges.append((start, end))
            or torch.zeros(max(1, end - start), 2, 2, 3, dtype=torch.float32)
        ),
    )
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_load_scene_audio",
        lambda self, proj, scene, start, end: editor_node._make_silent_audio(1.0),
    )

    result = editor_node.SonderEditor().execute(
        project="Existing Project",
        project_name="Ignored",
        fps=24.0,
        width=768,
        height=512,
        prompt=_prompt_graph({
            "editor-1": _prompt_node("SonderEditor"),
            "save-1": _prompt_node("SonderSaveVideo", {"project": ["editor-1", 0], "mark_queue_complete": False}),
        }),
        unique_id="editor-1",
        scene_id="scene-1",
        selection_start=0,
        selection_end=0,
    )

    assert queue_job.status == queue_status
    assert save_calls == []
    assert render_ranges == [(10, 30)]
    assert result[5] == "queued prompt"
    assert result[6] == 20
    assert project._execution_context["selection_start"] == 10
    assert project._execution_context["selection_end"] == 30
    assert project._execution_context["queue_job_id"] == ""


def test_render_queue_inactive_ignores_terminal_save_queue(tmp_path, monkeypatch):
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
        def get_prompt_for_range(start_frame, end_frame, labels_on=True, delimiter=".", boundary_threshold_pct=0.0):
            return f"live:{start_frame}-{end_frame}"

    queue_job = types.SimpleNamespace(
        job_id="job-1",
        scene_id="scene-1",
        selection_start=10,
        selection_end=30,
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
    render_ranges = []

    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    monkeypatch.setattr(editor_node, "save_project", lambda proj: save_calls.append(queue_job.status))
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_render_scene_frames",
        lambda self, proj, scene, start, end: (
            render_ranges.append((start, end))
            or torch.zeros(max(1, end - start), 2, 2, 3, dtype=torch.float32)
        ),
    )
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_load_scene_audio",
        lambda self, proj, scene, start, end: editor_node._make_silent_audio(1.0),
    )

    result = editor_node.SonderEditor().execute(
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
        scene_id="scene-1",
        selection_start=2,
        selection_end=8,
        render_queue_active=False,
    )

    assert queue_job.status == "pending"
    assert save_calls == []
    assert render_ranges == [(2, 8)]
    assert result[5] == "live:2-8"
    assert result[6] == 6
    assert project._execution_context["queue_job_id"] == ""


def test_consumed_queue_job_zero_range_raises(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)

    class DummyScene:
        scene_id = "scene-1"
        name = "Scene 1"
        duration_frames = 40
        width = 0
        height = 0
        fps = 0
        guide_frames = []

    queue_job = types.SimpleNamespace(
        job_id="job-1",
        scene_id="scene-1",
        selection_start=10,
        selection_end=10,
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
        batch_id="",
        batch_index=0,
    )

    class DummyProject:
        def __init__(self):
            self.fps = 24.0
            self.resolution = (768, 512)
            self.project_dir = str(tmp_path)
            self._scene = DummyScene()
            self.generation_queue = [queue_job]

        def get_scene(self, scene_id):
            return self._scene if scene_id == self._scene.scene_id else None

    project = DummyProject()
    save_states = []

    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    monkeypatch.setattr(
        editor_node,
        "save_project",
        lambda proj: save_states.append((queue_job.status, queue_job.error)),
    )

    with pytest.raises(RuntimeError, match="zero or invalid range"):
        editor_node.SonderEditor().execute(
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
    assert "zero or invalid range" in save_states[-1][1]
    assert queue_job.status == "failed"


def test_no_active_queue_runs_full_scene(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")

    class DummyScene:
        scene_id = "scene-1"
        name = "Scene 1"
        duration_frames = 18
        width = 0
        height = 0
        fps = 0
        guide_frames = []

        @staticmethod
        def get_prompt_for_range(start_frame, end_frame, labels_on=True, delimiter=".", boundary_threshold_pct=0.0):
            return f"live:{start_frame}-{end_frame}"

    class DummyProject:
        def __init__(self):
            self.fps = 24.0
            self.resolution = (768, 512)
            self.project_dir = str(tmp_path)
            self._execution_context = None
            self._scene = DummyScene()
            self.generation_queue = []

        def get_scene(self, scene_id):
            return self._scene if scene_id == self._scene.scene_id else None

        @staticmethod
        def get_asset(asset_id):
            return None

    project = DummyProject()
    render_ranges = []

    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    monkeypatch.setattr(editor_node, "save_project", lambda project: None)
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_render_scene_frames",
        lambda self, proj, scene, start, end: (
            render_ranges.append((start, end))
            or torch.zeros(max(1, end - start), 2, 2, 3, dtype=torch.float32)
        ),
    )
    monkeypatch.setattr(
        editor_node.SonderEditor,
        "_load_scene_audio",
        lambda self, proj, scene, start, end: editor_node._make_silent_audio(1.0),
    )

    result = editor_node.SonderEditor().execute(
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
        selection_start=0,
        selection_end=0,
    )

    assert render_ranges == [(0, 18)]
    assert result[5] == "live:0-18"
    assert result[6] == 18


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
        def get_prompt_for_range(start_frame, end_frame, labels_on=True, delimiter=".", boundary_threshold_pct=0.0):
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
    assert result[5] == "queued prompt"
    assert project._execution_context["queue_job_id"] == "job-1"


def test_editor_to_bridge_marks_queue_complete_round_trip(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    project_manager = importlib.import_module(f"{TEST_PACKAGE}.server.project_manager")
    thumbnail_service = importlib.import_module(f"{TEST_PACKAGE}.server.thumbnail_service")
    torch = importlib.import_module("torch")
    from PIL import Image

    monkeypatch.setattr(thumbnail_service, "ensure_thumbnail", lambda *args, **kwargs: None)

    base_dir = tmp_path / "sonder-projects"
    base_dir.mkdir(exist_ok=True)
    project = project_manager.create_project("Bridge_RT", base_dir=str(base_dir))
    project.add_scene(timeline_state.Scene(scene_id="scene-1", name="Scene 1", duration_frames=24))
    queue_job = timeline_state.GenerationJob(
        job_id="job-1",
        scene_id="scene-1",
        selection_start=0,
        selection_end=24,
        status="pending",
        prompt="queued prompt",
    )
    project.generation_queue.append(queue_job)
    project_manager.save_project(project)
    project_basename = os.path.basename(project.project_dir)

    monkeypatch.setattr(editor_node, "_get_projects_base_dir", lambda: str(base_dir))
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

    with io_nodes._BRIDGE_REGISTRY_LOCK:
        io_nodes._BRIDGE_REGISTRY.clear()
        io_nodes._BRIDGE_PROMPT_WATCHERS.clear()
        io_nodes._BRIDGE_PROMPT_KEY_BY_OBJECT_ID.clear()
        io_nodes._BRIDGE_PROMPT_OBJECT_IDS_BY_KEY.clear()
    io_nodes._BRIDGE_HOOKED_PROMPT_QUEUE_ID = None
    monkeypatch.setattr(io_nodes, "_ensure_prompt_bridge_watcher", lambda *args, **kwargs: None)

    prompt = _prompt_graph({
        "editor-1": _prompt_node("SonderEditor"),
        "bridge-1": _prompt_node("SonderSaveBridge", {"project": ["editor-1", 0], "mark_queue_complete": True}),
    })

    editor_result = editor_node.SonderEditor().execute(
        project=project_basename,
        project_name="Ignored",
        fps=24.0,
        width=768,
        height=512,
        prompt=prompt,
        unique_id="editor-1",
    )
    proj_after_editor = editor_result[0]
    assert proj_after_editor._execution_context["queue_job_id"] == "job-1"

    bridge = io_nodes.SonderSaveBridge()
    output_dir, _ = bridge.prepare_output(
        proj_after_editor,
        mark_queue_complete=True,
        prompt=prompt,
        unique_id="bridge-1",
    )
    output_path = Path(output_dir) / "result.png"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2, 2), color=(10, 20, 30)).save(output_path)

    prompt_key = next(iter(io_nodes._BRIDGE_REGISTRY.keys()))[0]
    io_nodes._finalize_prompt_bridges(prompt_key)

    restored = project_manager.load_project(proj_after_editor.project_dir)
    assert len(restored.assets) == 1
    assert restored.generation_queue[0].status == "completed"
    assert restored.generation_queue[0].result_asset_id == restored.assets[0].asset_id


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
        lambda proj, **_kwargs: save_states.append(
            [(job.job_id, job.status, job.error, job.progress) for job in proj.generation_queue]
        ),
    )

    node = editor_node.SonderEditor()
    project, consumed = node._consume_queue_job(project)

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
        lambda proj, **_kwargs: save_states.append(
            [(job.job_id, job.status, job.error, job.progress) for job in proj.generation_queue]
        ),
    )

    node = editor_node.SonderEditor()
    project, consumed = node._consume_queue_job(project)

    assert consumed is job_a
    assert job_a.status == "running"
    assert job_a.error == ""
    assert job_a.progress == 0.0
    assert save_states == [[("job-a", "running", "", 0.0)]]


def test_consume_queue_job_uses_pre_claim_version(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)

    job = types.SimpleNamespace(job_id="job-a", status="pending", error="", progress=0.0)
    project = types.SimpleNamespace(
        project_dir="project-dir",
        modified_at="base-version",
        generation_queue=[job],
    )
    save_kwargs = []

    def fake_save(proj, **kwargs):
        save_kwargs.append(kwargs)
        proj.modified_at = "post-claim-version"

    monkeypatch.setattr(editor_node, "save_project", fake_save)

    node = editor_node.SonderEditor()
    project, consumed = node._consume_queue_job(project)

    assert consumed is job
    assert job.base_modified_at == "base-version"
    assert save_kwargs == [{"expected_modified_at": "base-version"}]


def test_consume_queue_job_retries_conflict_instead_of_live_fallback(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)

    first_job = types.SimpleNamespace(job_id="job-a", status="pending", error="", progress=0.0)
    first_project = types.SimpleNamespace(
        project_dir="project-dir",
        modified_at="base-version",
        generation_queue=[first_job],
    )
    retry_job = types.SimpleNamespace(job_id="job-a", status="pending", error="", progress=0.0)
    retry_project = types.SimpleNamespace(
        project_dir="project-dir",
        modified_at="new-version",
        generation_queue=[retry_job],
    )
    calls = {"save": 0, "load": 0}

    def fake_save(proj, **kwargs):
        calls["save"] += 1
        if calls["save"] == 1:
            raise editor_node.ProjectVersionConflict(
                project_dir="project-dir",
                expected_modified_at=kwargs.get("expected_modified_at", ""),
                actual_modified_at="new-version",
            )
        proj.modified_at = "post-claim-version"

    def fake_load(project_dir):
        calls["load"] += 1
        assert project_dir == "project-dir"
        return retry_project

    monkeypatch.setattr(editor_node, "save_project", fake_save)
    monkeypatch.setattr(editor_node, "load_project", fake_load)

    node = editor_node.SonderEditor()
    project, consumed = node._consume_queue_job(first_project)

    assert project is retry_project
    assert consumed is retry_job
    assert retry_job.status == "running"
    assert retry_job.base_modified_at == "new-version"
    assert calls == {"save": 2, "load": 1}


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
        lambda proj, **_kwargs: save_states.append(
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
        def get_prompt_for_range(start_frame, end_frame, labels_on=True, delimiter=".", boundary_threshold_pct=0.0):
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

    assert first_result[5] == "queued prompt"
    assert second_result[5] == "queued prompt"
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

    save_calls = []

    monkeypatch.setattr(io_nodes, "encode_video", _fake_encode_video_success(io_nodes))
    monkeypatch.setattr(io_nodes, "save_project", lambda project: save_calls.append(project.generation_queue[0].status))
    monkeypatch.setattr(thumbnail_service, "ensure_thumbnail", lambda *args, **kwargs: None)

    node = io_nodes.SonderSaveVideo()
    # selection=[4,12) plus pre=2 plus post=1 -> 11 source frames in the rendered tensor.
    frames = torch.zeros(11, 2, 2, 3, dtype=torch.float32)
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


def test_save_video_take_placement_mode_controls_trimmed_vs_untrimmed(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    thumbnail_service = importlib.import_module(f"{TEST_PACKAGE}.server.thumbnail_service")

    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True, exist_ok=True)
    (project_dir / "cache" / "thumbnails").mkdir(parents=True, exist_ok=True)

    scene = timeline_state.Scene(scene_id="scene-1", name="Scene 1", duration_frames=48)
    project = timeline_state.TimelineProject(
        project_dir=str(project_dir),
        name="Take Placement Test",
        scenes=[scene],
    )

    monkeypatch.setattr(io_nodes, "encode_video", _fake_encode_video_success(io_nodes))
    monkeypatch.setattr(io_nodes, "save_project", lambda project: None)
    monkeypatch.setattr(thumbnail_service, "ensure_thumbnail", lambda *args, **kwargs: None)

    node = io_nodes.SonderSaveVideo()

    project._execution_context = {
        "scene_id": "scene-1",
        "scene_name": "Scene 1",
        "selection_start": 10,
        "selection_end": 14,
        "actual_pre_context_frames": 2,
        "actual_post_context_frames": 1,
        "frame_count_padding": 2,
        "take_placement_mode": "trimmed",
    }
    node.save_video(project, torch.zeros(9, 2, 2, 3, dtype=torch.float32), filename_prefix="trimmed", fps=24.0, mode="Take")
    trimmed = scene.clips[-1]

    assert trimmed.timeline_start_frame == 10
    assert trimmed.timeline_end_frame == 14
    assert trimmed.source_in_frame == 2
    assert trimmed.source_out_frame == 6
    assert trimmed.source_origin_frame == 0
    assert trimmed.total_source_frames == 7

    project._execution_context = {
        "scene_id": "scene-1",
        "scene_name": "Scene 1",
        "selection_start": 20,
        "selection_end": 24,
        "actual_pre_context_frames": 2,
        "actual_post_context_frames": 1,
        "take_placement_mode": "untrimmed",
    }
    node.save_video(project, torch.zeros(8, 2, 2, 3, dtype=torch.float32), filename_prefix="untrimmed", fps=24.0, mode="Take")
    untrimmed = scene.clips[-1]

    assert untrimmed.timeline_start_frame == 18
    assert untrimmed.timeline_end_frame == 26
    assert untrimmed.source_in_frame == 0
    assert untrimmed.source_out_frame == 8
    assert untrimmed.source_origin_frame == 0
    assert untrimmed.total_source_frames == 8


def test_save_video_take_trimmed_with_mask_offsets(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    thumbnail_service = importlib.import_module(f"{TEST_PACKAGE}.server.thumbnail_service")

    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True, exist_ok=True)
    (project_dir / "cache" / "thumbnails").mkdir(parents=True, exist_ok=True)

    scene = timeline_state.Scene(scene_id="scene-1", name="Scene 1", duration_frames=48)
    project = timeline_state.TimelineProject(
        project_dir=str(project_dir),
        name="Take Mask Offset Test",
        scenes=[scene],
    )

    monkeypatch.setattr(io_nodes, "encode_video", _fake_encode_video_success(io_nodes))
    monkeypatch.setattr(io_nodes, "save_project", lambda project: None)
    monkeypatch.setattr(thumbnail_service, "ensure_thumbnail", lambda *args, **kwargs: None)

    node = io_nodes.SonderSaveVideo()

    # actual_pre=2, actual_post=2, selection=[10,15), padding=2 -> total_frames=11
    # mask_pre=1, mask_post=1
    # Expected:
    #   source_in     = max(0, 2 - 1) = 1
    #   hidden_tail   = (2 - 1) + 2 = 3
    #   source_out    = 11 - 3 = 8
    #   timeline_start = 10 - 1 = 9
    #   timeline_end   = 15 + 1 = 16
    #   source_origin = 0
    #   total_source  = 11 - 2 = 9
    project._execution_context = {
        "scene_id": "scene-1",
        "scene_name": "Scene 1",
        "selection_start": 10,
        "selection_end": 15,
        "actual_pre_context_frames": 2,
        "actual_post_context_frames": 2,
        "mask_pre_offset": 1,
        "mask_post_offset": 1,
        "frame_count_padding": 2,
        "take_placement_mode": "trimmed",
    }
    node.save_video(project, torch.zeros(11, 2, 2, 3, dtype=torch.float32), filename_prefix="mask", fps=24.0, mode="Take")
    clip = scene.clips[-1]

    assert clip.timeline_start_frame == 9
    assert clip.timeline_end_frame == 16
    assert clip.source_in_frame == 1
    assert clip.source_out_frame == 8
    assert clip.source_origin_frame == 0
    assert clip.total_source_frames == 9


def test_save_video_take_trimmed_honors_snapped_mask_frames(tmp_path, monkeypatch):
    # Mirrors the LTX 8n+1 snap from test_execute_snaps_mask_pre_offset_below_ltx_boundary:
    # the renderer floors mask_start from 38 -> 33 to include the straddling latent, so the
    # take must extend 5 frames earlier than the un-snapped mask_pre_offset would imply.
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    thumbnail_service = importlib.import_module(f"{TEST_PACKAGE}.server.thumbnail_service")

    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True, exist_ok=True)
    (project_dir / "cache" / "thumbnails").mkdir(parents=True, exist_ok=True)

    scene = timeline_state.Scene(scene_id="scene-1", name="Scene 1", duration_frames=700)
    project = timeline_state.TimelineProject(
        project_dir=str(project_dir),
        name="Take Snap Test",
        scenes=[scene],
    )

    monkeypatch.setattr(io_nodes, "encode_video", _fake_encode_video_success(io_nodes))
    monkeypatch.setattr(io_nodes, "save_project", lambda project: None)
    monkeypatch.setattr(thumbnail_service, "ensure_thumbnail", lambda *args, **kwargs: None)

    project._execution_context = {
        "scene_id": "scene-1",
        "scene_name": "Scene 1",
        "selection_start": 482,
        "selection_end": 601,
        "actual_pre_context_frames": 48,
        "actual_post_context_frames": 0,
        "mask_pre_offset": 10,
        "mask_post_offset": 0,
        "mask_start_frame": 33,
        "mask_end_frame": 169,
        "frame_count_padding": 2,
        "take_placement_mode": "trimmed",
    }
    node = io_nodes.SonderSaveVideo()
    node.save_video(project, torch.zeros(169, 2, 2, 3, dtype=torch.float32), filename_prefix="snap", fps=24.0, mode="Take")
    clip = scene.clips[-1]

    # context_start_scene_frame = 482 - 48 = 434; mask_start_frame=33 -> timeline 434+33=467
    # (un-snapped placement would have used sel_start - mask_pre_offset = 472).
    assert clip.timeline_start_frame == 467
    # mask_end_frame=169 minus padding=2 -> visible_source_end=167; timeline 434+167=601.
    assert clip.timeline_end_frame == 601
    assert clip.source_in_frame == 33
    assert clip.source_out_frame == 167
    assert clip.total_source_frames == 167


def test_save_video_take_trimmed_pre_context_does_not_create_tail_ghost(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    thumbnail_service = importlib.import_module(f"{TEST_PACKAGE}.server.thumbnail_service")

    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True, exist_ok=True)
    (project_dir / "cache" / "thumbnails").mkdir(parents=True, exist_ok=True)

    scene = timeline_state.Scene(scene_id="scene-1", name="Scene 1", duration_frames=48)
    project = timeline_state.TimelineProject(
        project_dir=str(project_dir),
        name="Take No Tail Ghost Test",
        scenes=[scene],
    )

    monkeypatch.setattr(io_nodes, "encode_video", _fake_encode_video_success(io_nodes))
    monkeypatch.setattr(io_nodes, "save_project", lambda project: None)
    monkeypatch.setattr(thumbnail_service, "ensure_thumbnail", lambda *args, **kwargs: None)

    project._execution_context = {
        "scene_id": "scene-1",
        "scene_name": "Scene 1",
        "selection_start": 10,
        "selection_end": 14,
        "actual_pre_context_frames": 2,
        "actual_post_context_frames": 0,
        "frame_count_padding": 0,
        "take_placement_mode": "trimmed",
    }

    node = io_nodes.SonderSaveVideo()
    node.save_video(project, torch.zeros(6, 2, 2, 3, dtype=torch.float32), filename_prefix="pre_only", fps=24.0, mode="Take")
    clip = scene.clips[-1]

    visible_duration = clip.timeline_end_frame - clip.timeline_start_frame
    left_trimmed = clip.source_in_frame - clip.source_origin_frame
    right_trimmed = clip.total_source_frames - visible_duration - left_trimmed

    assert clip.timeline_start_frame == 10
    assert clip.timeline_end_frame == 14
    assert clip.source_in_frame == 2
    assert clip.source_origin_frame == 0
    assert clip.total_source_frames == 6
    assert left_trimmed == 2
    assert right_trimmed == 0


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
        "take_placement_linked": True,
        "take_placement_muted": True,
    }

    saved_audio_paths = []

    def fake_write_audio_wav(path, samples, sample_rate):
        Path(path).write_bytes(b"audio")
        saved_audio_paths.append(path)

    monkeypatch.setattr(io_nodes, "encode_video", _fake_encode_video_success(io_nodes))
    monkeypatch.setattr(io_nodes, "save_project", lambda project: None)
    monkeypatch.setattr(thumbnail_service, "ensure_thumbnail", lambda *args, **kwargs: None)
    monkeypatch.setattr(io_nodes, "write_audio_wav", fake_write_audio_wav)

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
    assert scene.clips[-1].muted is True
    assert scene.audio_tracks[-1].lane_index == 1
    assert scene.audio_tracks[-1].muted is True
    assert scene.audio_lane_count == 2
    assert scene.audio_tracks[-1].timeline_start_frame == 8
    assert scene.audio_tracks[-1].timeline_end_frame == 12
    assert scene.audio_tracks[-1].source_in_frame == 1
    assert scene.audio_tracks[-1].source_origin_frame == 0
    assert scene.audio_tracks[-1].total_source_frames == 5
    assert scene.linked_item_groups[-1]["items"] == [
        {"type": "clip", "id": scene.clips[-1].clip_id},
        {"type": "audio", "id": scene.audio_tracks[-1].track_id},
    ]
    assert any(str(path).endswith("_audio.wav") for path in saved_audio_paths)


def test_save_video_passes_audio_and_computed_timeout_to_encoder(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    thumbnail_service = importlib.import_module(f"{TEST_PACKAGE}.server.thumbnail_service")

    class DummyProject:
        def __init__(self):
            self.project_dir = str(tmp_path)
            self.assets = []

        def add_asset(self, asset):
            self.assets.append(asset)

    captured = []

    monkeypatch.setattr(io_nodes, "encode_video", _fake_encode_video_success(io_nodes, captured))
    monkeypatch.setattr(io_nodes, "save_video_encode_timeout_seconds", lambda *args, **kwargs: 1234)
    monkeypatch.setattr(io_nodes, "save_project", lambda project: None)
    monkeypatch.setattr(thumbnail_service, "ensure_thumbnail", lambda *args, **kwargs: None)
    monkeypatch.setattr(io_nodes, "write_audio_wav", lambda *args, **kwargs: None)

    node = io_nodes.SonderSaveVideo()
    frames = torch.zeros(2, 2, 2, 3, dtype=torch.float32)
    audio = {
        "waveform": torch.zeros(1, 2, 64, dtype=torch.float32),
        "sample_rate": 44100,
    }

    node.save_video(DummyProject(), frames, filename_prefix="audio", fps=24.0, audio=audio)

    assert captured[-1]["timeout"] == 1234
    assert captured[-1]["audio_path"]
    assert captured[-1]["preset_id"] == "Compatible MP4"


def test_preview_routes_through_shared_encoder(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")

    captured = []
    monkeypatch.setattr(io_nodes, "encode_video", _fake_encode_video_success(io_nodes, captured))
    monkeypatch.setattr(io_nodes, "save_video_encode_timeout_seconds", lambda *args, **kwargs: 1234)

    node = io_nodes.SonderPreviewVideo()
    frames = torch.zeros(2, 2, 2, 3, dtype=torch.float32)
    result = node.preview(frames, fps=24.0)

    # Preview now shares SonderSaveVideo's streaming encoder + computed timeout instead of
    # building a whole raw-video payload with a fixed 90s timeout.
    assert captured[-1]["timeout"] == 1234
    assert captured[-1]["preset_id"] == "Compatible MP4"
    assert not captured[-1]["audio_path"]

    descriptor = result["ui"]["sonder_video"][0]
    assert descriptor["type"] == "temp"
    assert descriptor["has_audio"] is False
    assert descriptor["filename"].endswith(".mp4")


def test_preview_muxes_audio_into_player(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")

    captured = []
    monkeypatch.setattr(io_nodes, "encode_video", _fake_encode_video_success(io_nodes, captured))
    monkeypatch.setattr(io_nodes, "write_audio_wav", lambda *args, **kwargs: None)

    node = io_nodes.SonderPreviewVideo()
    frames = torch.zeros(2, 2, 2, 3, dtype=torch.float32)
    audio = {"waveform": torch.zeros(1, 2, 64, dtype=torch.float32), "sample_rate": 44100}
    result = node.preview(frames, fps=24.0, audio=audio)

    assert captured[-1]["audio_path"]
    assert result["ui"]["sonder_video"][0]["has_audio"] is True


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


def test_load_scene_audio_caps_track_to_timeline_trim(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")

    media_dir = tmp_path / "media"
    media_dir.mkdir()
    audio_path = media_dir / "audio.wav"
    audio_path.write_bytes(b"audio")

    sample_rate = 44100
    fps = 44100.0
    waveform = (torch.arange(200, dtype=torch.float32) / 1000.0).unsqueeze(0)
    stereo_samples = editor_node.np.vstack([waveform.numpy(), waveform.numpy()])

    def fake_decode_audio_samples(path, *, sample_rate, channels, mix_to_mono=True, **kwargs):
        assert channels == 2
        assert mix_to_mono is False
        return stereo_samples.copy(), sample_rate

    monkeypatch.setattr(editor_node, "decode_audio_samples", fake_decode_audio_samples)

    track = types.SimpleNamespace(
        muted=False,
        lane_index=0,
        timeline_start_frame=10,
        timeline_end_frame=20,
        source_path=os.path.join("media", "audio.wav"),
        source_in_frame=50,
        volume=1.0,
    )
    scene = types.SimpleNamespace(
        fps=fps,
        audio_tracks=[track],
        audio_lane_configs=[],
    )
    project = types.SimpleNamespace(
        fps=fps,
        project_dir=str(tmp_path),
    )

    node = editor_node.SonderEditor()
    audio = node._load_scene_audio(project, scene, 0, 40)

    mixed = audio["waveform"][0, 0]
    assert mixed.shape[-1] == 40
    assert torch.allclose(mixed[:10], torch.zeros(10))
    assert torch.allclose(mixed[10:20], waveform[0, 50:60])
    assert torch.allclose(mixed[20:], torch.zeros(20))


def test_load_scene_audio_logs_missing_source_and_silent_fallback(tmp_path, monkeypatch, caplog):
    editor_node = _import_editor_node(tmp_path, monkeypatch)

    def fail_decode_audio_samples(*args, **kwargs):
        raise AssertionError("unexpected decode")

    monkeypatch.setattr(editor_node, "decode_audio_samples", fail_decode_audio_samples)

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


def test_load_scene_audio_skips_failed_track_and_keeps_good_mix(tmp_path, monkeypatch, caplog):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")

    media_dir = tmp_path / "media"
    media_dir.mkdir()
    bad_path = media_dir / "bad.wav"
    good_path = media_dir / "good.wav"
    bad_path.write_bytes(b"bad")
    good_path.write_bytes(b"good")

    sample_rate = 44100
    fps = 44100.0
    good_samples = editor_node.np.ones((2, 10), dtype=editor_node.np.float32) * 0.25

    def fake_decode_audio_samples(path, *, sample_rate, channels, mix_to_mono=True, **kwargs):
        assert channels == 2
        assert mix_to_mono is False
        if str(path).endswith("bad.wav"):
            raise RuntimeError("decode failed")
        return good_samples.copy(), sample_rate

    monkeypatch.setattr(editor_node, "decode_audio_samples", fake_decode_audio_samples)

    bad_track = types.SimpleNamespace(
        muted=False,
        lane_index=0,
        timeline_start_frame=0,
        timeline_end_frame=10,
        source_path=os.path.join("media", "bad.wav"),
        source_in_frame=0,
        volume=1.0,
    )
    good_track = types.SimpleNamespace(
        muted=False,
        lane_index=0,
        timeline_start_frame=0,
        timeline_end_frame=10,
        source_path=os.path.join("media", "good.wav"),
        source_in_frame=0,
        volume=1.0,
    )
    scene = types.SimpleNamespace(
        fps=fps,
        audio_tracks=[bad_track, good_track],
        audio_lane_configs=[],
    )
    project = types.SimpleNamespace(
        fps=fps,
        project_dir=str(tmp_path),
    )

    caplog.set_level("WARNING", logger="sonder_editor")

    node = editor_node.SonderEditor()
    audio = node._load_scene_audio(project, scene, 0, 10)

    mixed = audio["waveform"][0]
    assert audio["sample_rate"] == sample_rate
    assert torch.allclose(mixed, torch.from_numpy(good_samples))
    assert "Failed to decode/mix scene audio track" in caplog.text
    assert "bad.wav" in caplog.text
