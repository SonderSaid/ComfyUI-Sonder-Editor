"""The Editor and Save Video report what a run executed to the run output observer."""

import importlib

import pytest

from test_editor_node import (
    TEST_PACKAGE, _FrameConstraintProject, _fake_encode_video_success, _import_editor_node, _import_io_nodes,
    _patch_render_and_audio, _prompt_graph, _prompt_node,
)

OWNER = ("host", "proj", "host-1")
RECEIPT = "eeeeeeee-0000-4000-8000-000000000001"


def _observed(module, prompt, roots):
    runs = module.sdk_runs
    outputs = importlib.import_module(f"{TEST_PACKAGE}.server.run_outputs")
    runs.reset_for_tests()
    outputs.reset_for_tests()
    runs.register_receipt(RECEIPT, OWNER, ["1"])
    assert runs.bind_queued("prompt-1", prompt, roots) is not None
    runs.open_release("41", "prompt-1", prompt, roots)
    return runs, outputs


def test_editor_records_its_executed_window_once(tmp_path, monkeypatch):
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    project = _FrameConstraintProject(tmp_path, template_id="ltx-2.3", frame_constraint={"step": 8, "offset": 1, "min": 1})
    project.modified_at = "2026-10-22T09:00:00"
    monkeypatch.setattr(editor_node, "load_project", lambda project_dir: project)
    monkeypatch.setattr(editor_node.SonderEditor, "_bind_run", lambda self, proj, prompt, uid: ("41", ""))
    _patch_render_and_audio(editor_node, monkeypatch)
    prompt = _prompt_graph({"1": _prompt_node("SonderEditor"), "2": _prompt_node("Out", {"p": ["1", 0]})})
    prompt["1"]["_meta"] = {"sonder_receipt": RECEIPT}
    runs, outputs = _observed(editor_node, prompt, ["2"])
    kwargs = dict(project="Existing Project", project_name="Ignored", fps=24.0, width=768, height=512,
                  scene_id="scene-1", selection_start=482, selection_end=601, pre_context_frames=48,
                  post_context_frames=0, mask_pre_offset=10, prompt=prompt, unique_id="1")
    editor_node.SonderEditor().execute(**kwargs)
    outputs.observe("executing", {"node": "2", "prompt_id": "prompt-1"})
    outputs.observe("executed", {"node": "2", "prompt_id": "prompt-1",
                                 "output": {"images": [{"filename": "x.png", "type": "temp"}]}})
    context = outputs.read_outputs([OWNER], "", ["2"])["sources"]["2"]["result"]["editor_context"]["1"]
    assert context["scene_id"] == "scene-1" and context["fps"] == 24.0
    assert (context["generation_start"], context["generation_end"]) == (482, 601)
    assert context["actual_pre"] == 49 and context["frame_count_padding"] == 1
    assert context["mask_start_frame"] == 33 and context["queue"] == "live"
    assert context["start_modified_at"] == "2026-10-22T09:00:00"
    runs.reset_for_tests()
    outputs.reset_for_tests()


def test_save_video_take_publishes_its_scene_map_and_notes_the_commit(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    thumbnail_service = importlib.import_module(f"{TEST_PACKAGE}.server.thumbnail_service")
    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True, exist_ok=True)
    (project_dir / "cache" / "thumbnails").mkdir(parents=True, exist_ok=True)
    scene = timeline_state.Scene(scene_id="scene-1", name="Scene 1", duration_frames=48, video_lane_count=1)
    project = timeline_state.TimelineProject(project_dir=str(project_dir), name="Map", scenes=[scene])
    project._execution_context = {"scene_id": "scene-1", "scene_name": "Scene 1", "selection_start": 10,
                                  "selection_end": 16, "actual_pre_context_frames": 2, "actual_post_context_frames": 0,
                                  "mask_start_frame": 2, "mask_end_frame": 8}
    monkeypatch.setattr(io_nodes, "encode_video", _fake_encode_video_success(io_nodes))
    monkeypatch.setattr(io_nodes, "save_project", lambda project: None)
    monkeypatch.setattr(thumbnail_service, "ensure_thumbnail", lambda *args, **kwargs: None)
    prompt = _prompt_graph({"1": _prompt_node("SonderEditor"), "7": _prompt_node("SonderSaveVideo", {"project": ["1", 0]})})
    prompt["1"]["_meta"] = {"sonder_receipt": RECEIPT}
    runs, outputs = _observed(io_nodes, prompt, ["7"])
    frames = torch.zeros(8, 2, 2, 3, dtype=torch.float32)
    result = io_nodes.SonderSaveVideo().save_video(project, frames, filename_prefix="map", mode="Take",
                                                   prompt=prompt, unique_id="7")
    descriptor = result["ui"]["sonder_video"][0]
    assert descriptor["scene_map"] == {"scene_id": "scene-1", "scene_start": 10, "scene_end": 16, "source_offset": 2, "fps": 24.0}
    record = runs.record_snapshot("41")
    assert record["saved_to_project"] is True
    entries, _ = outputs.output_media.normalize("7", "7", result["ui"])
    assert entries[0]["scene_map"] == descriptor["scene_map"]
    runs.reset_for_tests()
    outputs.reset_for_tests()


def test_save_video_in_video_mode_has_no_scene_map(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    thumbnail_service = importlib.import_module(f"{TEST_PACKAGE}.server.thumbnail_service")
    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True, exist_ok=True)
    (project_dir / "cache" / "thumbnails").mkdir(parents=True, exist_ok=True)
    project = timeline_state.TimelineProject(project_dir=str(project_dir), name="Plain", scenes=[])
    monkeypatch.setattr(io_nodes, "encode_video", _fake_encode_video_success(io_nodes))
    monkeypatch.setattr(io_nodes, "save_project", lambda project: None)
    monkeypatch.setattr(thumbnail_service, "ensure_thumbnail", lambda *args, **kwargs: None)
    frames = torch.zeros(4, 2, 2, 3, dtype=torch.float32)
    result = io_nodes.SonderSaveVideo().save_video(project, frames, filename_prefix="plain", mode="Video")
    assert "scene_map" not in result["ui"]["sonder_video"][0]


def test_put_binds_and_ends_runs_deleted_from_the_queue_even_without_reads(tmp_path, monkeypatch):
    from types import SimpleNamespace

    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    runs = io_nodes.sdk_runs
    runs.reset_for_tests()
    runs.register_receipt(RECEIPT, OWNER, ["1"])
    prompt = _prompt_graph({"1": _prompt_node("SonderEditor"), "2": _prompt_node("Out", {"p": ["1", 0]})})
    prompt["1"]["_meta"] = {"sonder_receipt": RECEIPT}
    queue = SimpleNamespace(get_current_queue_volatile=lambda: ([], []))
    io_nodes._bind_queued_item((0, "gone", prompt, {"client_id": "c", "extra_pnginfo": {"workflow": {"id": "wf"}}}, ["2"], {}))
    bound = runs.bound_run("gone")
    assert (bound["owners"], bound["workflow_id"], bound["client_id"]) == ([OWNER], "wf", "c")
    monkeypatch.setattr(runs, "REMOVED_GRACE_SEC", -1)
    monkeypatch.setattr(io_nodes, "_last_run_reap", 0.0)
    io_nodes._reap_removed_runs(queue)
    assert runs.bound_run("gone")["outcome"] == runs.OUTCOME_REMOVED
    # Throttled: the next put within the interval does not walk the queue again.
    calls = []
    io_nodes._reap_removed_runs(SimpleNamespace(get_current_queue_volatile=lambda: calls.append(1) or ([], [])))
    assert calls == []
    runs.reset_for_tests()
